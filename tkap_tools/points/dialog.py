# -*- coding: utf-8 -*-
"""
dialog.py

The tool's Qt dialog, built entirely in code (no .ui file, so there is no
resource/pyuic compile step). It gathers the parameters, calls into core.py to
do the work, and echoes every message into an on-screen log pane.

One run handles both kinds of outline point in the export: SU_ points go to the
SU layer and F_ points to the Features layer, each with its own target and its
own number field, either of which can be switched off. They are separate layers
with independent numbering, so they are never mixed — and the two targets are
checked against each other before anything is written, since pointing both at
the same layer would file feature numbers as SU numbers.

Workflow reminder: replacing geometry does NOT save to disk by default. The
target layers are left in an edit session with the new outlines staged in their
buffers so they can be reviewed on the map first; the user then Saves Layer
Edits to keep them or discards them.
"""

import traceback

from qgis.PyQt import QtWidgets
from qgis.gui import QgsMapLayerComboBox, QgsFieldComboBox, QgsFileWidget
from qgis.core import (
    QgsProject,
    QgsMapLayerProxyModel,
    QgsFieldProxyModel,
    QgsCoordinateTransform,
    Qgis,
)

from . import core

TITLE = "Survey Points to Polygons"


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


class TargetRow(object):
    """
    The three widgets that make up one point type's row in the Point types box:
    the on/off checkbox, the target polygon layer, and the field on it holding
    the number. Grouped so the run code can ask a type for its target without
    caring which widgets that came from.
    """

    def __init__(self, point_type):
        self.point_type = point_type
        self.check = QtWidgets.QCheckBox(
            "{} points ({}_)".format(point_type.label, point_type.prefix)
        )
        self.check.setChecked(True)
        self.layer_combo = QgsMapLayerComboBox()
        self.layer_combo.setFilters(QgsMapLayerProxyModel.PolygonLayer)
        # Empty is allowed and is what you get when no layer in the project
        # carries this type's number field: better an obvious blank than a
        # confident guess at the wrong layer.
        self.layer_combo.setAllowEmptyLayer(True)
        self.field_combo = QgsFieldComboBox()
        self.field_combo.setFilters(QgsFieldProxyModel.AllTypes)

    def layer(self):
        return self.layer_combo.currentLayer()

    def field(self):
        return self.field_combo.currentField()

    def enabled(self):
        return self.check.isChecked()

    def set_target_enabled(self, on):
        self.layer_combo.setEnabled(on)
        self.field_combo.setEnabled(on)


class SurveyPointsDialog(QtWidgets.QDialog):
    def __init__(self, iface, parent=None):
        super(SurveyPointsDialog, self).__init__(parent)
        self.iface = iface
        self.setWindowTitle(TITLE)
        self.setMinimumWidth(620)
        self._build_ui()
        self._wire_events()
        self._sync_enabled_states()
        for row in self.target_rows:
            self._preselect_layer(row)

    # ------------------------------------------------------------------ UI ---
    def _build_ui(self):
        layout = QtWidgets.QVBoxLayout(self)

        # --- What to do: replace geometry on existing layers, or just build
        # the polygons onto new temporary layers ---
        mode_box = QtWidgets.QGroupBox("What to do")
        mode_layout = QtWidgets.QVBoxLayout(mode_box)
        self.rb_replace = QtWidgets.QRadioButton(
            "Replace geometry on existing layer(s)"
        )
        self.rb_temp = QtWidgets.QRadioButton(
            "Build polygons on new temporary layers (connect the dots; no target)"
        )
        self.rb_replace.setChecked(True)
        mode_layout.addWidget(self.rb_replace)
        mode_layout.addWidget(self.rb_temp)

        temp_name_row = QtWidgets.QHBoxLayout()
        temp_name_row.addWidget(QtWidgets.QLabel("Temporary layer name"))
        self.temp_name_edit = QtWidgets.QLineEdit("polygons (temp)")
        self.temp_name_edit.setToolTip(
            "Each temporary layer is named after its point type, e.g. "
            "'SU polygons (temp)' and 'Feature polygons (temp)'."
        )
        temp_name_row.addWidget(self.temp_name_edit)
        mode_layout.addLayout(temp_name_row)
        layout.addWidget(mode_box)

        # --- Survey point source: file(s), or a layer already in the project ---
        src_box = QtWidgets.QGroupBox("Survey points")
        src_form = QtWidgets.QGridLayout(src_box)

        self.rb_file = QtWidgets.QRadioButton("From CSV / Excel file(s)")
        self.rb_layer = QtWidgets.QRadioButton("From a layer loaded in the project")
        self.rb_file.setChecked(True)

        self.file_widget = QgsFileWidget()
        # Allow selecting several exports at once; they are combined and grouped
        # across all of them.
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

        # --- Which point types to read, and where each one writes ---
        types_box = QtWidgets.QGroupBox("Point types and targets")
        types_grid = QtWidgets.QGridLayout(types_box)

        self.target_rows = []
        rownum = 0
        for point_type in core.POINT_TYPES:
            row = TargetRow(point_type)
            types_grid.addWidget(row.check, rownum, 0, 1, 2)
            types_grid.addWidget(QtWidgets.QLabel("Target layer"), rownum + 1, 0)
            types_grid.addWidget(row.layer_combo, rownum + 1, 1)
            types_grid.addWidget(
                QtWidgets.QLabel("{} field".format(point_type.label)), rownum + 2, 0
            )
            types_grid.addWidget(row.field_combo, rownum + 2, 1)
            self.target_rows.append(row)
            rownum += 3

        types_note = QtWidgets.QLabel(
            "SU_ and F_ points are numbered separately and go to separate layers, "
            "so give each its own target. Every other coded point in the export "
            "(E_, O_, P_, GCP_, S_, SEC_) is ignored."
        )
        types_note.setWordWrap(True)
        types_note.setStyleSheet("color: gray;")
        types_grid.addWidget(types_note, rownum, 0, 1, 2)
        layout.addWidget(types_box)

        # --- What to do when a matched record already has a shape ---
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
            "This tool only replaces geometry on records that already exist in the "
            "target layer, matched by number alone. It never adds new rows and "
            "never changes attributes. Numbers with no matching record, or matching "
            "more than one record, are skipped."
        )
        note.setWordWrap(True)
        note.setStyleSheet("color: gray;")
        rule_layout.addWidget(note)

        self.commit_check = QtWidgets.QCheckBox(
            "Save edits to disk immediately (skip the review step)"
        )
        self.commit_check.setChecked(False)
        self.commit_check.setToolTip(
            "Off (recommended): the new outlines are staged in each layer's edit "
            "buffer so you can review them on the map, then Save Layer Edits to "
            "keep them or discard by toggling editing off without saving.\n"
            "On: the geometry is written straight to disk."
        )
        rule_layout.addWidget(self.commit_check)
        layout.addWidget(rule_box)

        # --- Crossover check (always on) + optional QC point layers ---
        qc_box = QtWidgets.QGroupBox("Checks")
        qc_form = QtWidgets.QGridLayout(qc_box)

        cross_note = QtWidgets.QLabel(
            "An outline that crosses itself — points shot out of perimeter order — "
            "is reordered to uncross it before the polygon is built. Every fix is "
            "listed in the log below with the increments involved."
        )
        cross_note.setWordWrap(True)
        cross_note.setStyleSheet("color: gray;")

        self.qc_check = QtWidgets.QCheckBox(
            "Also create a QC point layer per outline (raw vertices, as recorded)"
        )
        self.qc_prefix_edit = QtWidgets.QLineEdit("QC")
        self.qc_folder_widget = QgsFileWidget()
        self.qc_folder_widget.setStorageMode(QgsFileWidget.GetDirectory)

        qc_form.addWidget(cross_note, 0, 0, 1, 2)
        qc_form.addWidget(self.qc_check, 1, 0, 1, 2)
        qc_form.addWidget(QtWidgets.QLabel("Layer name prefix"), 2, 0)
        qc_form.addWidget(self.qc_prefix_edit, 2, 1)
        qc_form.addWidget(
            QtWidgets.QLabel("Output folder (optional; blank = temporary layers)"), 3, 0
        )
        qc_form.addWidget(self.qc_folder_widget, 3, 1)
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
        for row in self.target_rows:
            row.check.toggled.connect(self._sync_enabled_states)
            # Bind the row into the slot: a plain reference to `row` would close
            # over the loop variable and every combo would refresh the last row.
            row.layer_combo.layerChanged.connect(
                lambda _layer, r=row: self._refresh_fields(r)
            )
        self.run_btn.clicked.connect(self.run)
        self.close_btn.clicked.connect(self.close)

    def _sync_enabled_states(self):
        # Mode: replace on existing layers vs. build new temporary layers.
        replace_mode = self.rb_replace.isChecked()
        self.rule_box.setEnabled(replace_mode)
        self.temp_name_edit.setEnabled(not replace_mode)
        self.run_btn.setText("Replace geometry" if replace_mode else "Build polygons")

        # The type checkboxes pick which points are read, which matters in both
        # modes; only the target layer/field are replace-mode business.
        for row in self.target_rows:
            row.set_target_enabled(replace_mode and row.enabled())

        from_file = self.rb_file.isChecked()
        self.file_widget.setEnabled(from_file)
        self.layer_combo.setEnabled(not from_file)
        qc_on = self.qc_check.isChecked()
        self.qc_prefix_edit.setEnabled(qc_on)
        self.qc_folder_widget.setEnabled(qc_on)

    def _refresh_fields(self, row):
        layer = row.layer()
        row.field_combo.setLayer(layer)
        if layer is None:
            return
        field_names = [f.name() for f in layer.fields()]
        # Preselect the standardized number field when the layer has it.
        for name in field_names:
            if name.lower() == row.point_type.default_field.lower():
                row.field_combo.setField(name)
                return

    def _preselect_layer(self, row):
        """
        Point a target row at the first polygon layer carrying this type's number
        field — the SU layer has an `SU` field and the Features layer a `Feature`
        one, so this lands on the right layer of the two without being asked.
        Left empty when nothing matches, rather than defaulting to whichever
        polygon layer happens to be first and quietly offering the wrong target.

        Walks the combo's own entries, so only layers it would accept are
        considered and they are visited in the order shown.
        """
        wanted = row.point_type.default_field.lower()
        for i in range(row.layer_combo.count()):
            layer = row.layer_combo.layer(i)
            if layer is None:  # the "empty" entry
                continue
            if wanted in [f.name().lower() for f in layer.fields()]:
                row.layer_combo.setLayer(layer)
                self._refresh_fields(row)
                return
        row.layer_combo.setLayer(None)

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
                TITLE, str(exc), level=Qgis.Critical, duration=8
            )
        finally:
            self.run_btn.setEnabled(True)

    def _active_rows(self):
        active = [row for row in self.target_rows if row.enabled()]
        if not active:
            raise ValueError(
                "Tick at least one point type to read (SU points, Feature points, "
                "or both)."
            )
        return active

    def _validate_targets(self, active):
        """Check every enabled type has a usable, distinct target before writing."""
        seen = {}
        for row in active:
            label = row.point_type.label
            layer = row.layer()
            if layer is None:
                raise ValueError(
                    "Select a target polygon layer for {} points, or untick "
                    "them.".format(label)
                )
            field = row.field()
            if not field:
                raise ValueError(
                    "Select the field holding the number on the {} target layer "
                    "'{}'.".format(label, layer.name())
                )
            if field not in [f.name() for f in layer.fields()]:
                raise ValueError(
                    "Field '{}' was not found on the {} target layer '{}'.".format(
                        field, label, layer.name()
                    )
                )
            # Both types pointing at one layer would file feature numbers as SU
            # numbers (or the reverse) in whichever type ran second.
            if layer.id() in seen:
                raise ValueError(
                    "{} and {} points are both pointed at layer '{}'. They are "
                    "numbered separately and belong on separate layers — pick a "
                    "different target for one of them, or untick it.".format(
                        seen[layer.id()], label, layer.name()
                    )
                )
            seen[layer.id()] = label

    def _execute(self, messages):
        project = QgsProject.instance()
        active = self._active_rows()
        replace_mode = self.rb_replace.isChecked()
        if replace_mode:
            self._validate_targets(active)

        # --- Resolve the point source: one or more files, or a loaded layer ---
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
                "Reading survey points from layer: {}".format(source_layer.name())
            )
            rows, columns = core.read_rows_from_layer(source_layer)

        # --- Parse into groups per point type (in the Emlid CRS) ---
        emlid_sr = core.emlid_crs()
        ct_wgs_to_emlid = QgsCoordinateTransform(core.wgs84_crs(), emlid_sr, project)
        groups, all_points = core.parse_point_groups(
            rows, columns, messages, ct_wgs_to_emlid
        )

        # Drop the types that were not asked for, before uncrossing spends time
        # on them and before their counts muddy the summary.
        wanted = {row.point_type.key for row in active}
        for key in list(groups.keys()):
            if key not in wanted and groups[key]:
                messages.addMessage(
                    "Ignoring {} {} group(s): that point type is unticked.".format(
                        len(groups[key]), core.POINT_TYPES_BY_KEY[key].label
                    )
                )
                groups[key] = {}

        if not any(groups[row.point_type.key] for row in active):
            messages.addWarningMessage(
                "No valid {} outline(s) were found in the input. Nothing to "
                "do.".format(
                    " or ".join(row.point_type.label for row in active)
                )
            )
            return

        # --- The crossover check, before anything is built from the points ---
        fixes_by_key, _cross_stats = core.uncross_groups(groups, messages)

        if replace_mode:
            self._run_replace_mode(active, groups, emlid_sr, project, messages)
        else:
            self._run_temp_mode(active, groups, fixes_by_key, emlid_sr, messages)

        self._maybe_create_qc(all_points, wanted, emlid_sr, messages)

    def _run_temp_mode(self, active, groups, fixes_by_key, emlid_sr, messages):
        """Build the polygons onto new temporary layers — no target, no matching."""
        base = self.temp_name_edit.text().strip() or "polygons (temp)"
        built = []
        for row in active:
            by_number = groups[row.point_type.key]
            if not by_number:
                messages.addWarningMessage(
                    "No {} outlines in the input — no {} layer was built.".format(
                        row.point_type.label, row.point_type.label
                    )
                )
                continue
            layer, stats = core.create_polygon_layer(
                row.point_type,
                by_number,
                fixes_by_key,
                emlid_sr,
                "{} {}".format(row.point_type.label, base),
                messages,
            )
            built.append((layer, stats))

        self.iface.messageBar().pushMessage(
            TITLE,
            "Built {} polygon(s) on {} temporary layer(s).".format(
                sum(s["created"] for _, s in built), len(built)
            ),
            level=Qgis.Success, duration=6,
        )

    def _run_replace_mode(self, active, groups, emlid_sr, project, messages):
        """Replace geometry on the existing target layers, matched by number."""
        replace_regardless = (self.rule_combo.currentIndex() == 0)
        commit = self.commit_check.isChecked()
        total = 0

        for row in active:
            point_type = row.point_type
            by_number = groups[point_type.key]
            target = row.layer()
            if not by_number:
                messages.addWarningMessage(
                    "No {} outlines in the input — '{}' was left "
                    "untouched.".format(point_type.label, target.name())
                )
                continue

            messages.addMessage(
                "--- {} points -> '{}' ({} outline(s)) ---".format(
                    point_type.label, target.name(), len(by_number)
                )
            )

            # --- Reproject to the target CRS only if it differs from the Emlid CRS ---
            target_crs = target.crs()
            needs_reproject = target_crs.isValid() and target_crs != emlid_sr
            ct_to_target = (
                QgsCoordinateTransform(emlid_sr, target_crs, project)
                if needs_reproject else None
            )
            if needs_reproject:
                messages.addMessage(
                    "Target layer CRS ({}) differs from the survey data CRS "
                    "(EPSG:{}). Polygons will be reprojected.".format(
                        target_crs.authid(), core.EMLID_WKID
                    )
                )

            # --- Match to existing records (by number) and plan the updates ---
            existing = core.build_existing_index(target, row.field())
            updates, stats = core.plan_geometry_updates(
                point_type, by_number, existing, replace_regardless,
                emlid_sr, ct_to_target, messages
            )

            succeeded = core.apply_geometry_updates(
                target, updates, messages, commit=commit
            )
            total += succeeded

            messages.addMessage(
                "{}: replaced {} record(s); skipped {} with no match, {} that "
                "already had geometry, {} ambiguous (multiple matches), {} group(s) "
                "with fewer than 3 points.".format(
                    point_type.label, succeeded, stats["no_match"],
                    stats["has_geometry"], stats["ambiguous"], stats["too_few"]
                )
            )

        if commit:
            self.iface.messageBar().pushMessage(
                TITLE,
                "Replaced and saved geometry on {} record(s).".format(total),
                level=Qgis.Success, duration=6,
            )
        else:
            self.iface.messageBar().pushMessage(
                TITLE,
                "Staged {} geometry replacement(s) for review — Save Layer Edits to "
                "keep them.".format(total),
                level=Qgis.Info, duration=8,
            )

    def _maybe_create_qc(self, all_points, type_keys, emlid_sr, messages):
        if self.qc_check.isChecked():
            prefix = self.qc_prefix_edit.text().strip() or "QC"
            out_dir = self.qc_folder_widget.filePath().strip()
            core.create_qc_layers(
                all_points, emlid_sr, prefix, out_dir or None, messages,
                type_keys=type_keys,
            )
