# -*- coding: utf-8 -*-
"""
emlid_to_su_dialog.py

The plugin's Qt dialog, built entirely in code (no .ui file, so there is no
resource/pyuic compile step). It gathers the parameters, calls into core.py to
do the work, and echoes every message into an on-screen log pane.

Workflow reminder: replacing geometry does NOT save to disk by default. The
target layer is left in an edit session with the new outlines staged in its
buffer so they can be reviewed on the map first; the user then Saves Layer
Edits to keep them or discards them.
"""

import os
import traceback

from qgis.PyQt import QtWidgets, QtCore
from qgis.gui import QgsMapLayerComboBox, QgsFieldComboBox, QgsFileWidget
from qgis.core import (
    QgsProject,
    QgsMapLayerProxyModel,
    QgsFieldProxyModel,
    QgsCoordinateTransform,
    Qgis,
)

from . import core


class DialogMessages(core.Messages):
    """core.Messages that also echoes each line into the dialog's log pane."""

    def __init__(self, log_widget):
        super(DialogMessages, self).__init__()
        self._log = log_widget

    def _emit(self, level, text):
        self._log.appendPlainText("[{}] {}".format(level, text))
        # Keep the newest line visible.
        self._log.verticalScrollBar().setValue(
            self._log.verticalScrollBar().maximum()
        )
        QtWidgets.QApplication.processEvents()


class EmlidToSuDialog(QtWidgets.QDialog):
    def __init__(self, iface, parent=None):
        super(EmlidToSuDialog, self).__init__(parent)
        self.iface = iface
        self.setWindowTitle("Emlid to SU")
        self.setMinimumWidth(560)
        self._build_ui()
        self._wire_events()
        self._sync_enabled_states()
        self._refresh_target_fields()

    # ------------------------------------------------------------------ UI ---
    def _build_ui(self):
        layout = QtWidgets.QVBoxLayout(self)

        # --- What to do: replace geometry on an existing layer, or just build
        # the polygons onto a new temporary layer ---
        mode_box = QtWidgets.QGroupBox("What to do")
        mode_layout = QtWidgets.QVBoxLayout(mode_box)
        self.rb_replace = QtWidgets.QRadioButton(
            "Replace geometry on an existing SU layer"
        )
        self.rb_temp = QtWidgets.QRadioButton(
            "Build polygons on a new temporary layer (connect the dots; no target)"
        )
        self.rb_replace.setChecked(True)
        mode_layout.addWidget(self.rb_replace)
        mode_layout.addWidget(self.rb_temp)

        temp_name_row = QtWidgets.QHBoxLayout()
        temp_name_row.addWidget(QtWidgets.QLabel("Temporary layer name"))
        self.temp_name_edit = QtWidgets.QLineEdit("SU polygons (temp)")
        temp_name_row.addWidget(self.temp_name_edit)
        mode_layout.addLayout(temp_name_row)
        layout.addWidget(mode_box)

        # --- Emlid point source: file(s), or a layer already in the project ---
        src_box = QtWidgets.QGroupBox("Emlid points")
        src_form = QtWidgets.QGridLayout(src_box)

        self.rb_file = QtWidgets.QRadioButton("From CSV / Excel file(s)")
        self.rb_layer = QtWidgets.QRadioButton("From a layer loaded in the project")
        self.rb_file.setChecked(True)

        self.file_widget = QgsFileWidget()
        # Allow selecting several exports at once; they are combined and grouped
        # by SU across all of them.
        self.file_widget.setStorageMode(QgsFileWidget.GetMultipleFiles)
        self.file_widget.setFilter(
            "Emlid exports (*.csv *.xlsx *.xls);;All files (*.*)"
        )

        self.layer_combo = QgsMapLayerComboBox()
        # Point layers (e.g. a loaded delimited-text with geometry) and plain
        # tables (delimited text loaded with no geometry) are both valid sources.
        self.layer_combo.setFilters(
            QgsMapLayerProxyModel.PointLayer | QgsMapLayerProxyModel.NoGeometry
        )
        self.layer_combo.setAllowEmptyLayer(True)

        src_form.addWidget(self.rb_file, 0, 0)
        src_form.addWidget(self.file_widget, 0, 1)
        src_form.addWidget(self.rb_layer, 1, 0)
        src_form.addWidget(self.layer_combo, 1, 1)
        layout.addWidget(src_box)

        # --- Target SU polygon layer + which fields carry SU / FIELD ---
        tgt_box = QtWidgets.QGroupBox("Target SU polygon layer")
        self.tgt_box = tgt_box
        tgt_form = QtWidgets.QFormLayout(tgt_box)

        self.target_combo = QgsMapLayerComboBox()
        self.target_combo.setFilters(QgsMapLayerProxyModel.PolygonLayer)

        self.su_field_combo = QgsFieldComboBox()
        self.su_field_combo.setFilters(QgsFieldProxyModel.AllTypes)

        tgt_form.addRow("Polygon layer", self.target_combo)
        tgt_form.addRow("SU field", self.su_field_combo)
        layout.addWidget(tgt_box)

        # --- What to do when the matched SU already has a shape ---
        rule_box = QtWidgets.QGroupBox("Replacement rule")
        self.rule_box = rule_box
        rule_layout = QtWidgets.QVBoxLayout(rule_box)
        self.rule_combo = QtWidgets.QComboBox()
        self.rule_combo.addItem(
            "Replace geometry regardless (overwrite existing shapes)"
        )
        self.rule_combo.addItem(
            "Only fill empty / missing geometry (leave existing shapes alone)"
        )
        rule_layout.addWidget(self.rule_combo)

        note = QtWidgets.QLabel(
            "This tool only replaces geometry on SU records that already exist in "
            "the target layer, matched by SU number alone. It never adds new SU "
            "rows and never changes attributes. SUs with no matching record, or "
            "matching more than one record, are skipped."
        )
        note.setWordWrap(True)
        note.setStyleSheet("color: gray;")
        rule_layout.addWidget(note)

        self.commit_check = QtWidgets.QCheckBox(
            "Save edits to disk immediately (skip the review step)"
        )
        self.commit_check.setChecked(False)
        self.commit_check.setToolTip(
            "Off (recommended): the new outlines are staged in the layer's edit "
            "buffer so you can review them on the map, then Save Layer Edits to "
            "keep them or discard by toggling editing off without saving.\n"
            "On: the geometry is written straight to disk."
        )
        rule_layout.addWidget(self.commit_check)
        layout.addWidget(rule_box)

        # --- Optional QC point layers ---
        qc_box = QtWidgets.QGroupBox("QC")
        qc_form = QtWidgets.QGridLayout(qc_box)
        self.qc_check = QtWidgets.QCheckBox(
            "Also create a QC point layer for each SU (raw vertices)"
        )
        self.qc_prefix_edit = QtWidgets.QLineEdit("QC_SU")
        self.qc_folder_widget = QgsFileWidget()
        self.qc_folder_widget.setStorageMode(QgsFileWidget.GetDirectory)

        qc_form.addWidget(self.qc_check, 0, 0, 1, 2)
        qc_form.addWidget(QtWidgets.QLabel("Layer name prefix"), 1, 0)
        qc_form.addWidget(self.qc_prefix_edit, 1, 1)
        qc_form.addWidget(QtWidgets.QLabel("Output folder (optional; blank = temporary layers)"), 2, 0)
        qc_form.addWidget(self.qc_folder_widget, 2, 1)
        layout.addWidget(qc_box)

        # --- Log pane ---
        self.log = QtWidgets.QPlainTextEdit()
        self.log.setReadOnly(True)
        self.log.setMinimumHeight(160)
        self.log.setPlaceholderText("Run output will appear here…")
        layout.addWidget(self.log, 1)

        # --- Buttons ---
        btn_row = QtWidgets.QHBoxLayout()
        btn_row.addStretch(1)
        self.run_btn = QtWidgets.QPushButton("Replace geometry")
        self.close_btn = QtWidgets.QPushButton("Close")
        btn_row.addWidget(self.run_btn)
        btn_row.addWidget(self.close_btn)
        layout.addLayout(btn_row)

    def _wire_events(self):
        self.rb_replace.toggled.connect(self._sync_enabled_states)
        self.rb_file.toggled.connect(self._sync_enabled_states)
        self.qc_check.toggled.connect(self._sync_enabled_states)
        self.target_combo.layerChanged.connect(self._refresh_target_fields)
        self.run_btn.clicked.connect(self.run)
        self.close_btn.clicked.connect(self.close)

    def _sync_enabled_states(self):
        # Mode: replace on an existing layer vs. build a new temporary layer.
        replace_mode = self.rb_replace.isChecked()
        self.tgt_box.setEnabled(replace_mode)
        self.rule_box.setEnabled(replace_mode)
        self.temp_name_edit.setEnabled(not replace_mode)
        self.run_btn.setText("Replace geometry" if replace_mode else "Build polygons")

        from_file = self.rb_file.isChecked()
        self.file_widget.setEnabled(from_file)
        self.layer_combo.setEnabled(not from_file)
        qc_on = self.qc_check.isChecked()
        self.qc_prefix_edit.setEnabled(qc_on)
        self.qc_folder_widget.setEnabled(qc_on)

    def _refresh_target_fields(self):
        layer = self.target_combo.currentLayer()
        self.su_field_combo.setLayer(layer)
        if layer is None:
            return
        field_names = [f.name() for f in layer.fields()]
        # Preselect the standardized SU field when present.
        self._preselect(self.su_field_combo, field_names, core.SU_FIELD_NAME)

    @staticmethod
    def _preselect(combo, field_names, wanted):
        for name in field_names:
            if name.lower() == wanted.lower():
                combo.setField(name)
                return

    # --------------------------------------------------------------- Run -----
    def run(self):
        self.log.clear()
        messages = DialogMessages(self.log)
        self.run_btn.setEnabled(False)
        try:
            self._execute(messages)
        except Exception as exc:  # noqa: BLE001 - surface any failure to the user
            messages.addErrorMessage(str(exc))
            self.log.appendPlainText("")
            self.log.appendPlainText(traceback.format_exc())
            self.iface.messageBar().pushMessage(
                "Emlid to SU", str(exc), level=Qgis.Critical, duration=8
            )
        finally:
            self.run_btn.setEnabled(True)

    def _execute(self, messages):
        project = QgsProject.instance()

        # --- Resolve the Emlid point source: one or more files, or a loaded layer ---
        if self.rb_file.isChecked():
            raw = self.file_widget.filePath()
            paths = QgsFileWidget.splitFilePaths(raw) if raw else []
            if not paths:
                raise ValueError(
                    "Choose one or more CSV/Excel files, or switch to a loaded layer."
                )
            messages.addMessage("Reading {} file(s)…".format(len(paths)))
            rows, columns = core.read_rows_from_files(paths, messages)
        else:
            source_layer = self.layer_combo.currentLayer()
            if source_layer is None:
                raise ValueError("Choose a loaded point/table layer, or switch to files.")
            messages.addMessage(
                "Reading Emlid points from layer: {}".format(source_layer.name())
            )
            rows, columns = core.read_rows_from_layer(source_layer)

        # --- Parse into SU groups (in the Emlid CRS) ---
        emlid_sr = core.emlid_crs()
        ct_wgs_to_emlid = QgsCoordinateTransform(core.wgs84_crs(), emlid_sr, project)
        groups, all_points = core.parse_su_groups(rows, columns, messages, ct_wgs_to_emlid)
        if not groups:
            messages.addWarningMessage("No valid SU groups were found. Nothing to do.")
            return

        if self.rb_temp.isChecked():
            self._run_temp_mode(groups, all_points, emlid_sr, messages)
        else:
            self._run_replace_mode(groups, all_points, emlid_sr, project, messages)

    def _run_temp_mode(self, groups, all_points, emlid_sr, messages):
        """Build the SU polygons onto a new temporary layer — no target, no matching."""
        layer_name = self.temp_name_edit.text().strip() or "SU polygons (temp)"
        layer, stats = core.create_polygon_layer(groups, emlid_sr, layer_name, messages)
        self._maybe_create_qc(all_points, emlid_sr, messages)
        self.iface.messageBar().pushMessage(
            "Emlid to SU",
            "Built {} SU polygon(s) on temporary layer '{}'.".format(
                stats["created"], layer.name()
            ),
            level=Qgis.Success, duration=6,
        )

    def _run_replace_mode(self, groups, all_points, emlid_sr, project, messages):
        """Replace geometry on an existing SU layer, matched by SU number."""
        target = self.target_combo.currentLayer()
        if target is None:
            raise ValueError("Select a target SU polygon layer.")
        su_field = self.su_field_combo.currentField()
        if not su_field:
            raise ValueError("Select the SU field on the target layer.")
        target_field_names = [f.name() for f in target.fields()]
        if su_field not in target_field_names:
            raise ValueError(
                "SU field '{}' was not found on target layer '{}'.".format(
                    su_field, target.name()
                )
            )

        # --- Reproject to the target CRS only if it differs from the Emlid CRS ---
        target_crs = target.crs()
        needs_reproject = target_crs.isValid() and target_crs != emlid_sr
        ct_to_target = (
            QgsCoordinateTransform(emlid_sr, target_crs, project) if needs_reproject else None
        )
        if needs_reproject:
            messages.addMessage(
                "Target layer CRS ({}) differs from Emlid data CRS (EPSG:{}). "
                "Polygons will be reprojected.".format(target_crs.authid(), core.EMLID_WKID)
            )

        # --- Match to existing records (by SU number) and plan the updates ---
        existing = core.build_existing_index(target, su_field)
        replace_regardless = (self.rule_combo.currentIndex() == 0)
        updates, stats = core.plan_geometry_updates(
            groups, existing, replace_regardless,
            emlid_sr, ct_to_target, messages
        )

        commit = self.commit_check.isChecked()
        succeeded = core.apply_geometry_updates(target, updates, messages, commit=commit)

        messages.addMessage(
            "Summary: replaced {} record(s); skipped {} with no match, {} that "
            "already had geometry, {} ambiguous (multiple matches), {} group(s) "
            "with fewer than 3 points.".format(
                succeeded, stats["no_match"], stats["has_geometry"],
                stats["ambiguous"], stats["too_few"]
            )
        )

        self._maybe_create_qc(all_points, emlid_sr, messages)

        if commit:
            self.iface.messageBar().pushMessage(
                "Emlid to SU",
                "Replaced and saved geometry on {} SU record(s).".format(succeeded),
                level=Qgis.Success, duration=6,
            )
        else:
            self.iface.messageBar().pushMessage(
                "Emlid to SU",
                "Staged {} geometry replacement(s) for review — Save Layer Edits to "
                "keep them.".format(succeeded),
                level=Qgis.Info, duration=8,
            )

    def _maybe_create_qc(self, all_points, emlid_sr, messages):
        if self.qc_check.isChecked():
            prefix = self.qc_prefix_edit.text().strip() or "QC_SU"
            out_dir = self.qc_folder_widget.filePath().strip()
            core.create_qc_layers(all_points, emlid_sr, prefix, out_dir or None, messages)
