"""Hand-coded dialog for the TKAP Stratigraphic Phasing plugin.

Written in code rather than a ``.ui`` file so there is no resource-compilation
step in the build.
"""

from __future__ import annotations

import os
from datetime import datetime

from qgis.core import QgsProject, QgsSettings
from qgis.gui import QgsFieldComboBox, QgsMapLayerComboBox

# These two proxy models live in qgis.core on current QGIS but were exposed via
# qgis.gui on older builds; import from wherever this install has them.
try:
    from qgis.core import QgsFieldProxyModel, QgsMapLayerProxyModel
except ImportError:  # pragma: no cover - depends on QGIS build
    from qgis.gui import QgsFieldProxyModel, QgsMapLayerProxyModel

from qgis.PyQt.QtCore import Qt
from qgis.PyQt.QtWidgets import (
    QApplication,
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPlainTextEdit,
    QProgressBar,
    QPushButton,
    QRadioButton,
    QVBoxLayout,
)

from .dialog_layout import fit_to_screen, scrollable_body
from .phasing_runner import (
    MODE_FILTERED,
    MODE_GEOPACKAGE,
    MODE_MEMORY,
    PhasingParams,
    run_phasing,
    scan,
    summarise,
)

SETTINGS_PREFIX = "tkap_phasing"

#: Best guesses for the field-designator column, in priority order. `area` is
#: the confirmed TKAP name; the rest are fallbacks for other exports.
FIELD_CANDIDATES = ("area", "field", "field_id", "field_name", "trench")
SPACE_PHASE_CANDIDATES = ("space_phase", "space_phas", "spacephase", "phase")


class PhasingDialog(QDialog):
    def __init__(self, iface, parent=None):
        super().__init__(parent)
        self.iface = iface
        self._last_result = None
        self.setWindowTitle("TKAP Stratigraphic Phasing")
        # Width only. A minimum height would defeat the scroll area below.
        self.setMinimumWidth(520)
        self.setSizeGripEnabled(True)
        self._build_ui()
        self._connect()
        self._restore_settings()
        self._on_layer_changed()
        fit_to_screen(self, 620, 760)

    # -- construction ------------------------------------------------------

    def _build_ui(self):
        layout, footer = scrollable_body(self)

        # --- source -------------------------------------------------------
        source_box = QGroupBox("Source")
        source_form = QFormLayout(source_box)

        self.layer_combo = QgsMapLayerComboBox()
        self.layer_combo.setFilters(QgsMapLayerProxyModel.PolygonLayer)
        source_form.addRow("SU layer:", self.layer_combo)

        self.space_phase_combo = QgsFieldComboBox()
        self.space_phase_combo.setFilters(QgsFieldProxyModel.String)
        source_form.addRow("space_phase field:", self.space_phase_combo)

        self.area_field_combo = QgsFieldComboBox()
        self.area_field_combo.setAllowEmptyFieldName(True)
        source_form.addRow("Field designator:", self.area_field_combo)

        value_row = QHBoxLayout()
        self.area_value_combo = QComboBox()
        self.area_value_combo.setEditable(True)
        self.area_value_combo.setMinimumWidth(200)
        value_row.addWidget(self.area_value_combo, 1)
        self.preview_button = QPushButton("Preview")
        value_row.addWidget(self.preview_button)
        source_form.addRow("Field value:", value_row)

        layout.addWidget(source_box)

        # --- preview ------------------------------------------------------
        self.summary = QLabel("")
        self.summary.setWordWrap(True)
        self.summary.setTextFormat(Qt.PlainText)
        self.summary.setStyleSheet(
            "QLabel { background: palette(alternate-base); padding: 8px; "
            "border-radius: 4px; }"
        )
        self.summary.setMinimumHeight(64)
        layout.addWidget(self.summary)

        self.warnings_box = QPlainTextEdit()
        self.warnings_box.setReadOnly(True)
        self.warnings_box.setMaximumHeight(110)
        self.warnings_box.setVisible(False)
        layout.addWidget(self.warnings_box)

        # --- naming -------------------------------------------------------
        naming_box = QGroupBox("Output naming")
        naming_form = QFormLayout(naming_box)

        self.prefix_edit = QLineEdit("Field")
        naming_form.addRow("Label prefix:", self.prefix_edit)

        self.include_name_check = QCheckBox("Include phase name")
        self.include_name_check.setChecked(True)
        naming_form.addRow("", self.include_name_check)

        self.pad_check = QCheckBox("Zero-pad phase numbers (Phase01)")
        self.pad_check.setToolTip("For alphabetical sorting in other tools.")
        naming_form.addRow("", self.pad_check)

        self.provenance_check = QCheckBox("Add ph_space / ph_num / ph_name columns")
        self.provenance_check.setChecked(True)
        self.provenance_check.setToolTip(
            "On live filtered layers these are virtual fields held in the "
            "project -- nothing is written to PostGIS."
        )
        naming_form.addRow("", self.provenance_check)

        self.style_check = QCheckBox("Inherit symbology from the source layer")
        self.style_check.setChecked(True)
        naming_form.addRow("", self.style_check)

        self.example_label = QLabel("")
        self.example_label.setStyleSheet("QLabel { color: palette(mid); }")
        naming_form.addRow("Example:", self.example_label)

        layout.addWidget(naming_box)

        # --- output -------------------------------------------------------
        output_box = QGroupBox("Output")
        output_layout = QVBoxLayout(output_box)

        self.filtered_radio = QRadioButton("Live filtered layers -- no data copied")
        self.filtered_radio.setChecked(True)
        self.filtered_radio.setToolTip(
            "Each phase layer points at the same source with an editable filter, "
            "e.g.  \"area\" = '6' AND \"space_phase\" LIKE '%Sp.34.1 (%'\n"
            "Edit it later via Layer Properties -> Source -> Query Builder."
        )
        output_layout.addWidget(self.filtered_radio)

        self.memory_radio = QRadioButton("Temporary scratch layers -- frozen copy")
        self.memory_radio.setToolTip("In-memory copy. Lost when QGIS closes.")
        output_layout.addWidget(self.memory_radio)

        self.gpkg_radio = QRadioButton("GeoPackage -- one table per phase")
        output_layout.addWidget(self.gpkg_radio)

        gpkg_row = QHBoxLayout()
        self.gpkg_edit = QLineEdit()
        self.gpkg_edit.setPlaceholderText("Path to .gpkg")
        self.gpkg_edit.setEnabled(False)
        self.gpkg_button = QPushButton("Browse...")
        self.gpkg_button.setEnabled(False)
        gpkg_row.addWidget(self.gpkg_edit, 1)
        gpkg_row.addWidget(self.gpkg_button)
        output_layout.addLayout(gpkg_row)

        self.add_to_project_check = QCheckBox("Add result to project")
        self.add_to_project_check.setChecked(True)
        output_layout.addWidget(self.add_to_project_check)

        layout.addWidget(output_box)
        layout.addStretch(1)

        # Outside the scroll area: these stay put no matter how short the
        # window is dragged.
        self.progress = QProgressBar()
        self.progress.setVisible(False)
        footer.addWidget(self.progress)

        self.buttons = QDialogButtonBox(
            QDialogButtonBox.Ok | QDialogButtonBox.Cancel
        )
        self.buttons.button(QDialogButtonBox.Ok).setText("Run")
        footer.addWidget(self.buttons)

    def _connect(self):
        self.layer_combo.layerChanged.connect(self._on_layer_changed)
        self.area_field_combo.fieldChanged.connect(self._on_area_field_changed)
        self.area_value_combo.currentTextChanged.connect(self._on_value_changed)
        self.preview_button.clicked.connect(self._refresh_preview)
        self.gpkg_radio.toggled.connect(self._on_mode_changed)
        self.gpkg_button.clicked.connect(self._pick_gpkg)
        for widget in (self.include_name_check, self.pad_check):
            widget.toggled.connect(self._update_example)
        self.prefix_edit.textChanged.connect(self._update_example)
        self.buttons.accepted.connect(self._run)
        self.buttons.rejected.connect(self.reject)

    # -- state -------------------------------------------------------------

    def _settings(self):
        return QgsSettings()

    def _restore_settings(self):
        settings = self._settings()
        self.prefix_edit.setText(
            settings.value(f"{SETTINGS_PREFIX}/prefix", "Field")
        )
        self.gpkg_edit.setText(settings.value(f"{SETTINGS_PREFIX}/gpkg", ""))
        mode = settings.value(f"{SETTINGS_PREFIX}/mode", MODE_FILTERED)
        if mode == MODE_GEOPACKAGE:
            self.gpkg_radio.setChecked(True)
        elif mode == MODE_MEMORY:
            self.memory_radio.setChecked(True)
        else:
            self.filtered_radio.setChecked(True)

    def _store_settings(self):
        settings = self._settings()
        settings.setValue(f"{SETTINGS_PREFIX}/prefix", self.prefix_edit.text())
        settings.setValue(f"{SETTINGS_PREFIX}/gpkg", self.gpkg_edit.text())
        settings.setValue(f"{SETTINGS_PREFIX}/mode", self._mode())

    def _mode(self):
        if self.gpkg_radio.isChecked():
            return MODE_GEOPACKAGE
        if self.memory_radio.isChecked():
            return MODE_MEMORY
        return MODE_FILTERED

    def current_layer(self):
        return self.layer_combo.currentLayer()

    def _params(self):
        return PhasingParams(
            layer=self.current_layer(),
            area_value=self.area_value_combo.currentText() or None,
            area_field=self.area_field_combo.currentField(),
            space_phase_field=self.space_phase_combo.currentField(),
            mode=self._mode(),
            gpkg_path=self.gpkg_edit.text().strip(),
            include_phase_name=self.include_name_check.isChecked(),
            pad_phase=2 if self.pad_check.isChecked() else 0,
            add_provenance_fields=self.provenance_check.isChecked(),
            inherit_style=self.style_check.isChecked(),
            # No opt-out: live layers over the source are always read-only.
            read_only_filtered=True,
            field_prefix=self.prefix_edit.text().strip() or "Field",
            add_to_project=self.add_to_project_check.isChecked(),
        )

    # -- reactions ---------------------------------------------------------

    def _guess_field(self, layer, candidates, combo):
        names = [f.name() for f in layer.fields()]
        lowered = {n.lower(): n for n in names}
        for candidate in candidates:
            if candidate in lowered:
                combo.setField(lowered[candidate])
                return True
        return False

    def _on_layer_changed(self):
        layer = self.current_layer()
        self.space_phase_combo.setLayer(layer)
        self.area_field_combo.setLayer(layer)
        self.area_value_combo.clear()
        self.summary.setText("")
        self.warnings_box.setVisible(False)
        if layer is None:
            return
        self._guess_field(layer, SPACE_PHASE_CANDIDATES, self.space_phase_combo)
        self._guess_field(layer, FIELD_CANDIDATES, self.area_field_combo)
        self._on_area_field_changed()
        self._update_example()

    def _on_area_field_changed(self):
        layer = self.current_layer()
        field = self.area_field_combo.currentField()
        self.area_value_combo.blockSignals(True)
        self.area_value_combo.clear()
        if layer is not None and field:
            index = layer.fields().lookupField(field)
            if index >= 0:
                values = []
                for value in layer.uniqueValues(index):
                    text = "" if value is None else str(value).strip()
                    if text and text.upper() != "NULL":
                        values.append(text)
                # Numeric-looking values ('1'..'10') sort numerically, the rest
                # alphabetically after them.
                values.sort(
                    key=lambda v: (not v.isdigit(), int(v) if v.isdigit() else 0, v)
                )
                self.area_value_combo.addItems(values)
        self.area_value_combo.blockSignals(False)
        self._on_value_changed()

    def _on_value_changed(self):
        self._update_example()
        # Auto-preview is cheap: the scan requests only the columns it reads and
        # skips geometry entirely.
        self._refresh_preview()

    def _on_mode_changed(self, checked):
        self.gpkg_edit.setEnabled(checked)
        self.gpkg_button.setEnabled(checked)
        if checked and not self.gpkg_edit.text().strip():
            self._suggest_gpkg_path()

    def _suggest_gpkg_path(self):
        base = QgsProject.instance().homePath() or os.path.expanduser("~")
        stamp = datetime.now().strftime("%Y%m%d_%H%M")
        label = self.prefix_edit.text().strip() or "Field"
        value = self.area_value_combo.currentText() or "all"
        self.gpkg_edit.setText(
            os.path.join(base, f"TKAP_{label}{value}_phases_{stamp}.gpkg")
        )

    def _pick_gpkg(self):
        start = self.gpkg_edit.text().strip() or (
            QgsProject.instance().homePath() or os.path.expanduser("~")
        )
        path, _ = QFileDialog.getSaveFileName(
            self, "Output GeoPackage", start, "GeoPackage (*.gpkg)"
        )
        if path:
            if not path.lower().endswith(".gpkg"):
                path += ".gpkg"
            self.gpkg_edit.setText(path)

    def _update_example(self):
        from .phasing_core import PhaseKey, field_label, layer_name

        label = field_label(
            self.area_value_combo.currentText() or "6",
            self.prefix_edit.text().strip() or "Field",
        )
        self.example_label.setText(
            layer_name(
                label,
                PhaseKey(34, 1),
                "Construction",
                include_phase_name=self.include_name_check.isChecked(),
                pad_phase=2 if self.pad_check.isChecked() else 0,
            )
        )

    def _refresh_preview(self):
        layer = self.current_layer()
        if layer is None or not self.space_phase_combo.currentField():
            self.summary.setText("")
            return
        QApplication.setOverrideCursor(Qt.WaitCursor)
        try:
            params = self._params()
            result = scan(params)
        except Exception as exc:  # pragma: no cover - defensive UI guard
            self.summary.setText(f"Preview failed: {exc}")
            return
        finally:
            QApplication.restoreOverrideCursor()

        self.summary.setText(
            summarise(result, params.area_value or "", params.field_prefix)
        )
        self._show_warnings(result)

    def _show_warnings(self, result):
        if not result.warnings:
            self.warnings_box.setVisible(False)
            self.warnings_box.clear()
            return
        lines = [str(w) for w in result.warnings[:50]]
        if len(result.warnings) > 50:
            lines.append(f"... and {len(result.warnings) - 50} more.")
        self.warnings_box.setPlainText("\n".join(lines))
        self.warnings_box.setVisible(True)

    def _log_warnings(self, result):
        """Mirror warnings into the QGIS Log Messages panel for the record."""
        if result is None or not result.warnings:
            return
        from qgis.core import Qgis, QgsMessageLog

        for warning in result.warnings:
            QgsMessageLog.logMessage(str(warning), "TKAP Phasing", Qgis.Warning)

    # -- run ---------------------------------------------------------------

    def _report_progress(self, done, total, name):
        self.progress.setVisible(True)
        self.progress.setMaximum(total)
        self.progress.setValue(done)
        self.progress.setFormat(f"%v / %m  {name}")
        QApplication.processEvents()

    def _run(self):
        params = self._params()

        if params.layer is None:
            QMessageBox.warning(self, "No layer", "Choose a source SU layer.")
            return
        if not params.space_phase_field:
            QMessageBox.warning(
                self, "No field", "Choose the attribute holding space_phase values."
            )
            return
        if params.mode == MODE_GEOPACKAGE:
            if not params.gpkg_path:
                QMessageBox.warning(
                    self, "No path", "Choose an output GeoPackage path."
                )
                return
            if os.path.exists(params.gpkg_path):
                reply = QMessageBox.question(
                    self,
                    "Overwrite GeoPackage?",
                    f"{os.path.basename(params.gpkg_path)} already exists.\n\n"
                    "Running will overwrite the whole file, including any phase "
                    "layers already in it. Continue?",
                    QMessageBox.Yes | QMessageBox.No,
                    QMessageBox.No,
                )
                if reply != QMessageBox.Yes:
                    return

        QApplication.setOverrideCursor(Qt.WaitCursor)
        try:
            outcome = run_phasing(params, progress=self._report_progress)
        except Exception as exc:  # pragma: no cover - defensive UI guard
            QApplication.restoreOverrideCursor()
            self.progress.setVisible(False)
            QMessageBox.critical(self, "Phasing failed", str(exc))
            return
        finally:
            QApplication.restoreOverrideCursor()

        self.progress.setVisible(False)

        result = outcome.result
        self._last_result = result
        self._log_warnings(result)

        if outcome.errors:
            QMessageBox.critical(
                self, "Phasing failed", "\n".join(outcome.errors)
            )
            return

        if result is not None:
            self.summary.setText(
                summarise(result, params.area_value or "", params.field_prefix)
            )
            self._show_warnings(result)

        if result is None or not result.buckets:
            QMessageBox.information(
                self,
                "Nothing to do",
                "No SUs in that field carry a phase, so no layers were created.",
            )
            return

        message = [
            f"Created {len(outcome.layers)} phase layer(s) in group "
            f"'{outcome.group_name}'."
        ]
        if params.mode == MODE_FILTERED:
            sample = next(iter(outcome.subsets.values()), "")
            if sample:
                message.append(f"Filter applied, e.g.:\n{sample}")
        else:
            message.append(
                f"{result.total_output_features} features written from "
                f"{result.phased} phased SUs."
            )
        if params.mode == MODE_GEOPACKAGE:
            message.append(f"Written to {params.gpkg_path}")
        if result.warnings:
            message.append(
                f"{len(result.warnings)} warning(s) -- see the dialog and the "
                "'TKAP Phasing' tab of the Log Messages panel."
            )
        QMessageBox.information(self, "Done", "\n\n".join(message))

        self._store_settings()
        self.accept()
