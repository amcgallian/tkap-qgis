"""The setup dialog: everything that has to be settled before drawing starts.

Modelled on the Georeferencer, and it ends the same way the Georeferencer does --
by producing a transform -- but instead of writing a warped raster and stopping,
it hands the main canvas a section-local coordinate space and a set of seeded
SU polygons to edit.

The dialog is deliberately opinionated about the two mistakes that produce a
plausible-looking but wrong drawing:

* mixing the two vertical datums, a 36 m error at this site, so which one the
  drawing works in is chosen in words and the offset between them is explicit
* fitting a projective transform to exactly four points, which reports zero
  residual while being wrong everywhere between them, so the model is chosen
  from the point count and residuals are always on screen
"""

from __future__ import annotations

from pathlib import Path  # noqa: F401  -- used by the style-file checks

from qgis.core import QgsMapLayerProxyModel, QgsProject, QgsVectorLayer
from qgis.gui import QgsMapLayerComboBox
from qgis.PyQt.QtCore import QSettings, Qt

#: Where the chosen .qml is remembered, so it is picked once per machine
#: rather than once per section.
STYLE_SETTING_KEY = "TkapSection/styleQml"

#: Breathing room, in metres, beyond the outermost control point or SU. Without
#: it the extreme markers sit exactly on the frame and read as clipped.
EXTENT_PAD = 0.10
from qgis.PyQt.QtGui import QColor, QFont
from qgis.PyQt.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QDoubleSpinBox,
    QFileDialog,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QRadioButton,
    QTableWidget,
    QTableWidgetItem,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from .gcp_picker import GcpPickerWidget
from .photo import (
    DEFAULT_WORKING_DATUM,
    ControlPoint,
    Fit,
    HeightDatum,
    calibrate_separation,
)
from .section_geom import SectionLine
from .su_source import (
    SUCandidate,
    SeedSource,
    apply_seed_cascade,
    discover_spatial,
    feature_ids_for_su_ids,
    list_spaces,
    postgis_connection,
    strat_floors,
    su_ids_for_space,
)

GOOD = QColor(60, 160, 60)
WARN = QColor(200, 130, 0)
BAD = QColor(200, 50, 50)
#: Full-height columns are the expected default, not a problem to flag.
NEUTRAL = QColor(110, 110, 110)

SEED_COLOURS = {
    SeedSource.MEASURED: GOOD,
    SeedSource.INFERRED: WARN,
    SeedSource.HALF_MEASURED: WARN,
    SeedSource.FULL_HEIGHT: NEUTRAL,
}


class SectionSetupDialog(QDialog):
    """Collects the SU list, the photo placement, and the vertical extent."""

    def __init__(self, line: SectionLine, iface, parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Set up the section")
        self.resize(1180, 780)
        self.iface = iface

        self.line = line
        self.candidates: list[SUCandidate] = []
        #: What the last control-point file had to say about itself, kept so the
        #: note under the datum controls can be rebuilt when the datum or the
        #: correction changes rather than only when a file is loaded.
        self._gcp_notes = ""
        self._floors_key: tuple | None = None
        self._floors: dict[int, float] = {}

        self._build_ui()
        self.line.height_datum = self._working_datum().value
        # Only once the whole dialog exists: the picker answers with a fit, and
        # acting on one reaches into the extent box, which is built after the
        # tabs.
        self.picker.set_datum(self._working_datum(), self.separation_spin.value())
        self._refresh_trace_labels()
        self._on_style_source_changed()
        # There is no photo yet when the dialog opens, so the "take it from the
        # photo" option has to start disabled -- otherwise it sits ticked doing
        # nothing with the height boxes greyed out beside it.
        self._sync_extent_source()
        self.refresh_candidates()

    # The picking half of this dialog lives in GcpPickerWidget, so that
    # re-linking a saved section's backdrop reuses it rather than growing a
    # second copy. These delegate, so everything below reads the same as it did
    # when the fields were local.

    @property
    def control_points(self) -> list[ControlPoint]:
        return self.picker.control_points

    @property
    def fit(self) -> Fit | None:
        return self.picker.fit

    @property
    def photo_path(self) -> str | None:
        return self.picker.photo_path

    @property
    def _image_size(self) -> tuple[int, int]:
        return self.picker.image_size

    # -------------------------------------------------------------- building --

    def _build_ui(self) -> None:
        outer = QVBoxLayout(self)

        outer.addWidget(self._build_header())

        self.tabs = QTabWidget()
        # Photo first: it is what the drawing is traced over, and placing it is
        # what sets the top and bottom of the section, so choosing units before
        # there is a photo means choosing them against a guessed height range.
        # No ampersand in the labels: Qt reads it as a keyboard shortcut marker.
        self.tabs.addTab(self._build_photo_tab(), "1. Photo")
        self.tabs.addTab(self._build_su_tab(), "2. Units")
        outer.addWidget(self.tabs, 1)

        outer.addWidget(self._build_extent_box())

        self.buttons = QDialogButtonBox(
            QDialogButtonBox.Ok | QDialogButtonBox.Cancel
        )
        self.buttons.button(QDialogButtonBox.Ok).setText("Start drawing")
        self.buttons.accepted.connect(self._on_accept)
        self.buttons.rejected.connect(self.reject)
        outer.addWidget(self.buttons)
        self._update_ok_state()

    def _build_header(self) -> QWidget:
        box = QGroupBox("The line you drew")
        grid = QHBoxLayout(box)

        form = QFormLayout()
        self.name_edit = QLineEdit(self.line.name)
        self.name_edit.textChanged.connect(self._on_name_changed)
        form.addRow("Name", self.name_edit)

        self.trace_label = QLabel()
        form.addRow("Size", self.trace_label)
        grid.addLayout(form, 2)

        form2 = QFormLayout()
        self.buffer_spin = QDoubleSpinBox()
        self.buffer_spin.setRange(0.01, 50.0)
        self.buffer_spin.setDecimals(2)
        self.buffer_spin.setSingleStep(0.05)
        self.buffer_spin.setSuffix(" m")
        self.buffer_spin.setValue(self.line.buffer)
        self.buffer_spin.setToolTip(
            "How far behind the wall to look for units. Only searches the side "
            "you are looking at, so units in the far half of a baulk are left "
            "out. Widen it (up to 50 m) to sweep in units set back from the "
            "face; tighten it to keep the far face out."
        )
        self.buffer_spin.valueChanged.connect(self._on_buffer_changed)
        form2.addRow("Search width", self.buffer_spin)

        self.flip_check = QCheckBox("Looking at the other side")
        self.flip_check.setToolTip(
            "The wall you are looking at is normally on the left of the "
            "direction you drew. Tick this if it is on the right."
        )
        self.flip_check.setChecked(self.line.flipped)
        self.flip_check.toggled.connect(self._on_flip_changed)
        form2.addRow("", self.flip_check)

        # Only appears when the fit actually comes out mirrored, so it reads as
        # a specific diagnosis rather than general advice.
        self.mirror_warning = QPushButton("Photo is back to front - click to fix")
        self.mirror_warning.setStyleSheet(
            "background: #c03000; color: white; font-weight: bold; padding: 4px;"
        )
        self.mirror_warning.setToolTip(
            "The control points put the photo left-right reversed. Clicking "
            "this flips the section the right way round."
        )
        self.mirror_warning.setVisible(False)
        self.mirror_warning.clicked.connect(
            lambda: self.flip_check.setChecked(not self.flip_check.isChecked())
        )
        form2.addRow("", self.mirror_warning)
        grid.addLayout(form2, 1)

        return box

    def _build_su_tab(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)

        controls = QHBoxLayout()
        controls.addWidget(QLabel("SU layer"))
        self.su_layer_combo = QgsMapLayerComboBox()
        self.su_layer_combo.setFilters(QgsMapLayerProxyModel.PolygonLayer)
        self.su_layer_combo.layerChanged.connect(self._on_su_layer_changed)
        controls.addWidget(self.su_layer_combo, 2)

        self.mode_spatial = QRadioButton("Crossing the line")
        self.mode_spatial.setToolTip("Every unit the line you drew passes through.")
        self.mode_spatial.setChecked(True)
        self.mode_spatial.toggled.connect(self.refresh_candidates)
        self.mode_space = QRadioButton("In a space")
        self.mode_space.setToolTip(
            "Every unit recorded as belonging to a space. Only works when the "
            "units come from the database."
        )
        controls.addWidget(self.mode_spatial)
        controls.addWidget(self.mode_space)

        self.space_combo = QComboBox()
        self.space_combo.setMinimumWidth(180)
        self.space_combo.currentIndexChanged.connect(self.refresh_candidates)
        controls.addWidget(self.space_combo, 1)

        refresh = QPushButton("Refresh")
        refresh.clicked.connect(self.refresh_candidates)
        controls.addWidget(refresh)
        layout.addLayout(controls)

        style_row = QHBoxLayout()
        style_row.addWidget(QLabel("Colours"))
        self.style_combo = QComboBox()
        self.style_combo.addItem("Same as the SU layer", "layer")
        self.style_combo.addItem("From a style file", "qml")
        self.style_combo.currentIndexChanged.connect(self._on_style_source_changed)
        style_row.addWidget(self.style_combo)
        self.style_edit = QLineEdit()
        self.style_edit.setPlaceholderText("Path to a .qml...")
        self.style_edit.setReadOnly(True)
        style_row.addWidget(self.style_edit, 1)
        self.style_browse = QPushButton("Browse...")
        self.style_browse.clicked.connect(self._browse_style)
        style_row.addWidget(self.style_browse)
        layout.addLayout(style_row)

        self.style_note = QLabel()
        self.style_note.setWordWrap(True)
        self.style_note.setStyleSheet("font-size: 10px;")
        layout.addWidget(self.style_note)

        self.use_elevations = QCheckBox("Start units at their recorded heights")
        self.use_elevations.setToolTip(
            "Off: every unit starts as a tall box you drag into place.\n"
            "On: units with a recorded height start at that height."
        )
        self.use_elevations.toggled.connect(self.refresh_candidates)
        layout.addWidget(self.use_elevations)

        self.su_table = QTableWidget(0, 6)
        self.su_table.setHorizontalHeaderLabels(
            ["Use", "SU", "Type", "Along the wall (m)", "Base", "Top"]
        )
        self.su_table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.su_table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.su_table.verticalHeader().setVisible(False)
        header = self.su_table.horizontalHeader()
        header.setSectionResizeMode(0, QHeaderView.ResizeToContents)
        header.setSectionResizeMode(1, QHeaderView.ResizeToContents)
        header.setSectionResizeMode(2, QHeaderView.Stretch)
        self.su_table.itemChanged.connect(self._on_su_check_changed)
        layout.addWidget(self.su_table, 1)

        buttons = QHBoxLayout()
        for label, slot in (
            ("Select all", lambda: self._set_all_included(True)),
            ("Select none", lambda: self._set_all_included(False)),
        ):
            b = QPushButton(label)
            b.clicked.connect(slot)
            buttons.addWidget(b)
        buttons.addStretch(1)
        self.su_summary = QLabel()
        buttons.addWidget(self.su_summary)
        layout.addLayout(buttons)
        return page

    def _build_photo_tab(self) -> QWidget:
        # The photo, the control-point table and the fit are all
        # GcpPickerWidget's; what stays here is everything that decides
        # something about the *section* rather than about the placement.
        self.picker = GcpPickerWidget(self.line)
        self.picker.fitChanged.connect(self._on_fit_changed)
        self.picker.notesChanged.connect(self._on_gcp_notes)
        self.picker.separationSuggested.connect(self.separation_spin_set)
        self.picker.photoChanged.connect(lambda _path: self._update_ok_state())

        self.datum_label = QLabel()
        self.datum_label.setWordWrap(True)
        self.picker.extra_layout.addWidget(self.datum_label)

        datum_form = QFormLayout()

        # Which datum the finished drawing is on. Ellipsoidal by default: it is
        # what the receiver measures, where an orthometric height is only as
        # good as the geoid model behind it.
        self.datum_combo = QComboBox()
        for option in (HeightDatum.ELLIPSOIDAL, HeightDatum.ORTHOMETRIC):
            self.datum_combo.addItem(option.label, option)
        # Signals blocked for the opening selection: the handler reaches into
        # the units tab, which is built after this one. Harmless while the
        # default sits at index 0 and the combo is already there, but changing
        # the default would otherwise fire it mid-build.
        self.datum_combo.blockSignals(True)
        self.datum_combo.setCurrentIndex(
            self.datum_combo.findData(DEFAULT_WORKING_DATUM)
        )
        self.datum_combo.blockSignals(False)
        self.datum_combo.setToolTip(
            "Which heights the elevation axis is labelled with, and what every "
            "control point and recorded unit altitude is converted to.\n\n"
            "Ellipsoidal is the raw GNSS height and depends on no geoid model. "
            "Orthometric matches the altitudes in the SU table.\n\n"
            "Either way the drawing is internally consistent; the choice is "
            "which numbers end up down the side of the figure."
        )
        self.datum_combo.currentIndexChanged.connect(self._on_datum_changed)
        datum_form.addRow("Heights on the drawing", self.datum_combo)

        self.separation_spin = QDoubleSpinBox()
        self.separation_spin.setRange(-200.0, 200.0)
        self.separation_spin.setDecimals(3)
        self.separation_spin.setSingleStep(0.1)
        self.separation_spin.setSuffix(" m")
        # Default to no correction: it is only needed to cross between the two
        # datums, and a file already on the drawing's datum crosses nothing. A
        # file on the other one gets a suggested value when it loads (see
        # _browse_gcps), and "Work it out from a known height..." solves for it.
        self.separation_spin.setValue(0.0)
        self.separation_spin.setToolTip(
            "How far ellipsoidal heights sit above orthometric ones here: about "
            "36 m at this site, 0 if you never cross between the two.\n\n"
            "It is applied only to heights that arrive on the other datum from "
            "the one above - control points from a differently configured "
            "receiver, and the SU table's altitudes when the drawing works in "
            "ellipsoidal heights."
        )
        self.separation_spin.valueChanged.connect(self._on_separation_changed)
        datum_form.addRow("Gap between the two", self.separation_spin)

        calibrate = QPushButton("Work it out from a known height...")
        calibrate.setToolTip(
            "Enter the real height of the top of the wall and the gap "
            "is calculated for you."
        )
        calibrate.clicked.connect(self._calibrate_separation)
        datum_form.addRow("", calibrate)
        self.picker.extra_layout.addLayout(datum_form)
        return self.picker

    def separation_spin_set(self, value: float) -> None:
        """Take the separation a loaded control-point file implies.

        A slot rather than a direct connection to ``setValue`` so the intent is
        named: the setup dialog is free to accept the suggestion, where a
        re-link is not -- its section is already drawn against a gap.
        """
        self.separation_spin.setValue(value)

    def _on_gcp_notes(self, message: str) -> None:
        # Recorded before anything else runs: setting the spin box re-runs the
        # note, and it must find the new text already in place.
        self._gcp_notes = message
        self._refresh_datum_note()
        self._update_ok_state()

    def _on_fit_changed(self, fit) -> None:
        """A new placement (or none). Everything the *section* takes from it.

        The picker deliberately does none of this: it computes a fit and stops.
        Changing the vertical extent, re-seeding the units and offering the flip
        are all things that must not happen when an already-drawn section has
        its backdrop re-linked.
        """
        self.mirror_warning.setVisible(fit is not None and fit.is_mirrored)
        self._sync_extent_source()
        if fit is not None and self.auto_extent.isChecked():
            self._extent_from_fit()
        self._update_ok_state()

    def _build_extent_box(self) -> QWidget:
        box = QGroupBox("Top and bottom of the drawing")
        row = QHBoxLayout(box)

        self.auto_extent = QCheckBox("Take from the photo")
        self.auto_extent.setChecked(True)
        self.auto_extent.toggled.connect(self._on_auto_extent_toggled)
        row.addWidget(self.auto_extent)

        row.addWidget(QLabel("Bottom"))
        self.zmin_spin = QDoubleSpinBox()
        self.zmin_spin.setRange(-500.0, 9000.0)
        self.zmin_spin.setDecimals(3)
        self.zmin_spin.setSuffix(" m")
        self.zmin_spin.setValue(self.line.z_min or 1029.0)
        self.zmin_spin.valueChanged.connect(self._on_extent_edited)
        row.addWidget(self.zmin_spin)

        row.addWidget(QLabel("Top"))
        self.zmax_spin = QDoubleSpinBox()
        self.zmax_spin.setRange(-500.0, 9000.0)
        self.zmax_spin.setDecimals(3)
        self.zmax_spin.setSuffix(" m")
        self.zmax_spin.setValue(self.line.z_max or 1032.0)
        self.zmax_spin.valueChanged.connect(self._on_extent_edited)
        row.addWidget(self.zmax_spin)

        self.extent_note = QLabel()
        row.addWidget(self.extent_note, 1)
        self._on_auto_extent_toggled(True)
        return box

    # ---------------------------------------------------------------- trace --

    def _refresh_trace_labels(self) -> None:
        self.trace_label.setText(
            f"{self.line.length:.2f} m long, azimuth {self.line.azimuth:.1f} deg, "
            f"from ({self.line.p0[0]:.2f}, {self.line.p0[1]:.2f}) "
            f"to ({self.line.p1[0]:.2f}, {self.line.p1[1]:.2f})"
        )

    def _on_name_changed(self, text: str) -> None:
        self.line.name = text or "Section"

    def _on_buffer_changed(self, value: float) -> None:
        self.line.buffer = value
        self.refresh_candidates()
        self.picker.line_changed()

    def _on_flip_changed(self, flipped: bool) -> None:
        self.line.flipped = flipped
        self.refresh_candidates()
        self.picker.line_changed()

    # ------------------------------------------------------------------ SUs --

    # ------------------------------------------------------------ symbology --

    def _on_style_source_changed(self, *_args) -> None:
        use_qml = self.style_combo.currentData() == "qml"
        self.style_edit.setEnabled(use_qml)
        self.style_browse.setEnabled(use_qml)
        if use_qml and not self.style_edit.text():
            remembered = QSettings().value(STYLE_SETTING_KEY, "", type=str)
            if remembered and Path(remembered).exists():
                self.style_edit.setText(remembered)
        self._refresh_style_note()

    def _browse_style(self) -> None:
        start = self.style_edit.text() or QSettings().value(
            STYLE_SETTING_KEY, "", type=str
        )
        path, _ = QFileDialog.getOpenFileName(
            self, "Layer style", start, "QGIS layer style (*.qml);;All files (*)"
        )
        if not path:
            return
        self.style_edit.setText(path)
        QSettings().setValue(STYLE_SETTING_KEY, path)
        self._refresh_style_note()

    def style_qml(self) -> str | None:
        if self.style_combo.currentData() != "qml":
            return None
        path = self.style_edit.text().strip()
        return path or None

    def separation(self) -> float:
        """The geoid separation the fit was computed with.

        Handed to the session so a saved section can redo its placement later.
        Without it, control points restored from a file would be refitted with a
        separation of zero, which on an ellipsoidal survey puts the photo 36 m
        off -- the same silent error :meth:`_datum_mismatch` warns about.
        """
        return float(self.separation_spin.value())

    def _refresh_style_note(self) -> None:
        """Say plainly whether the section will come out styled.

        A layer that has not had the project QML applied yet carries a plain
        single symbol, and inheriting that produces an unstyled section --
        which looks like the plugin ignoring the symbology rather than there
        being nothing to inherit.
        """
        if self.style_combo.currentData() == "qml":
            path = self.style_qml()
            if not path:
                self.style_note.setText(
                    "<span style='color:#b06000'>Choose a .qml, or the section "
                    "will use a plain fill.</span>"
                )
            elif not Path(path).exists():
                self.style_note.setText(
                    "<span style='color:#c03000'>That file is not there.</span>"
                )
            else:
                self.style_note.setText(
                    f"Section units will be styled from {Path(path).name}."
                )
            return

        layer = self.su_layer_combo.currentLayer()
        renderer = layer.renderer() if isinstance(layer, QgsVectorLayer) else None
        name = type(renderer).__name__ if renderer is not None else ""
        if name in ("QgsCategorizedSymbolRenderer", "QgsGraduatedSymbolRenderer",
                    "QgsRuleBasedRenderer"):
            detail = ""
            if hasattr(renderer, "classAttribute"):
                detail = f" on {renderer.classAttribute()}"
            self.style_note.setText(
                f"<span style='color:#2e7d32'>Will inherit the layer's "
                f"{name.replace('Qgs', '').replace('Renderer', '')} symbology"
                f"{detail}.</span>"
            )
        else:
            self.style_note.setText(
                "<span style='color:#b06000'>This layer has no colours to "
                "copy. Style it first, or choose 'From a style file'.</span>"
            )

    def _on_su_layer_changed(self, layer) -> None:
        has_pg = isinstance(layer, QgsVectorLayer) and postgis_connection(layer) is not None
        self.mode_space.setEnabled(has_pg)
        self.space_combo.setEnabled(has_pg)
        self.space_combo.clear()
        if has_pg:
            for space_id, label in list_spaces(layer):
                self.space_combo.addItem(label, space_id)
        else:
            if self.mode_space.isChecked():
                self.mode_spatial.setChecked(True)
        self._refresh_style_note()
        self.refresh_candidates()

    def refresh_candidates(self) -> None:
        layer = self.su_layer_combo.currentLayer()
        if not isinstance(layer, QgsVectorLayer) or not layer.isValid():
            self.candidates = []
            self._refresh_su_table()
            self._refresh_datum_note()
            return

        restrict = None
        if self.mode_space.isChecked() and self.space_combo.currentData() is not None:
            db_ids = su_ids_for_space(layer, self.space_combo.currentData())
            restrict = feature_ids_for_su_ids(layer, db_ids)

        try:
            self.candidates = discover_spatial(layer, self.line, restrict_to=restrict)
        except Exception as exc:
            QMessageBox.warning(self, "SU search failed", str(exc))
            self.candidates = []
        # Rediscovery is the one moment worth asking the database again.
        self._floors_key = None

        self._apply_seeding(layer)
        self._refresh_su_table()
        # Whether the recorded altitudes are in use is one of the things the
        # datum note complains about, and this is the path that changes it.
        self._refresh_datum_note()

    def _strat_floors(self, layer) -> dict[int, float]:
        """Strat floors for the current candidates, fetched at most once.

        A database round trip, and seeding is now re-run on every nudge of the
        gap spinner, so the answer is held against the candidate set it was
        fetched for rather than asked for again on each one. Cleared whenever
        the candidates are rediscovered, so Refresh still goes back to the
        database.
        """
        key = (layer.id(), tuple(c.su_id for c in self.candidates))
        if self._floors_key == key:
            return self._floors
        floors = {}
        if postgis_connection(layer) is not None and self.candidates:
            try:
                floors = strat_floors(layer, [c.su_id for c in self.candidates])
            except Exception:
                floors = {}
        self._floors_key, self._floors = key, floors
        return floors

    def _apply_seeding(self, layer) -> None:
        if self.line.z_min is None or self.line.z_max is None:
            self.line.set_vertical_extent(self.zmin_spin.value(), self.zmax_spin.value())
        try:
            apply_seed_cascade(
                self.candidates, self.line,
                strat_floor=self._strat_floors(layer),
                use_recorded_elevations=self.use_elevations.isChecked(),
                height_offset=self._height_offset(),
            )
        except ValueError:
            pass

    def _reseed(self) -> None:
        """Re-run the seeding after something the seed boxes depend on moved.

        Cheap and idempotent -- ``recorded_*`` is never written by seeding -- so
        it can be called on every change to the datum or the gap between datums
        without accumulating error.
        """
        layer = self.su_layer_combo.currentLayer()
        if self.candidates and layer is not None:
            self._apply_seeding(layer)
            self._refresh_su_table()

    def _refresh_su_table(self) -> None:
        table = self.su_table
        table.blockSignals(True)
        table.setRowCount(len(self.candidates))
        for row, cand in enumerate(self.candidates):
            check = QTableWidgetItem()
            check.setFlags(Qt.ItemIsUserCheckable | Qt.ItemIsEnabled | Qt.ItemIsSelectable)
            check.setCheckState(Qt.Checked if cand.include else Qt.Unchecked)
            check.setData(Qt.UserRole, row)
            table.setItem(row, 0, check)

            table.setItem(row, 1, QTableWidgetItem(cand.su_number))
            table.setItem(row, 2, QTableWidgetItem(cand.describe_type()))
            spans = ", ".join(f"{s.x_min:.2f}-{s.x_max:.2f}" for s in cand.spans)
            table.setItem(row, 3, QTableWidgetItem(spans))
            # The seed box, which is what will actually be drawn.
            for col, value in ((4, cand.alt_min), (5, cand.alt_max)):
                item = QTableWidgetItem(
                    "" if value is None else f"{value:.3f}"
                )
                if cand.seed_source is not SeedSource.FULL_HEIGHT:
                    item.setForeground(GOOD)
                table.setItem(row, col, item)
        table.blockSignals(False)
        self._update_su_summary()

    def _on_su_check_changed(self, item) -> None:
        if item.column() != 0:
            return
        idx = item.data(Qt.UserRole)
        if idx is None or idx >= len(self.candidates):
            return
        self.candidates[idx].include = item.checkState() == Qt.Checked
        self._update_su_summary()

    def _set_all_included(self, value: bool) -> None:
        for cand in self.candidates:
            cand.include = value
        self._refresh_su_table()

    def _update_su_summary(self) -> None:
        included = sum(1 for c in self.candidates if c.include)
        self.su_summary.setText(
            f"{included} of {len(self.candidates)} selected"
        )
        self._update_ok_state()

    # ----------------------------------------------------------- the datum --

    def _working_datum(self) -> HeightDatum:
        """The datum the drawing is on: what everything is converted to."""
        return self.datum_combo.currentData() or DEFAULT_WORKING_DATUM

    def _height_offset(self) -> float:
        """What to add to the SU table's altitudes to reach the drawing.

        The table is orthometric, so it is already there when the drawing is
        orthometric too, and needs the separation added when the drawing works
        in ellipsoidal heights.
        """
        if self._working_datum() is HeightDatum.ELLIPSOIDAL:
            return self.separation_spin.value()
        return 0.0

    def _on_datum_changed(self, _index: int) -> None:
        # Recorded on the line, because the line is what carries the section's
        # CRS -- whose remark states the datum -- and what gets saved.
        self.line.height_datum = self._working_datum().value
        self._refresh_datum_note()
        self._reseed()
        # Refreshes the point table and refits, both of which read the datum.
        self.picker.set_datum(self._working_datum(), self.separation_spin.value())

    def _on_separation_changed(self, _value: float) -> None:
        self._refresh_datum_note()
        # The SU table's altitudes cross the gap too when the drawing is
        # ellipsoidal, so the seed boxes move with it.
        self._reseed()
        self.picker.set_datum(self._working_datum(), self.separation_spin.value())

    def _datum_mismatch(self) -> str | None:
        """Complaint about heights being used on a datum they are not on.

        The silent 36 m errors the datum choice leaves open, both of the same
        shape: heights arriving from the other datum with no gap set to carry
        them across. Nothing moves, everything still fits, and the drawing is
        out by the geoid. Two ways in -- the control points, and the SU table's
        recorded altitudes -- so both are checked.
        """
        if self.separation_spin.value():
            return None
        working = self._working_datum()

        if self.control_points:
            theirs = self.control_points[0].datum
            if theirs is not working:
                return (
                    f"These control points are {theirs.value} but the drawing "
                    f"is working in {working.value} heights, and the gap "
                    "between the two is 0 - so they will be used exactly as "
                    f"they are and the elevation axis will be labelled "
                    f"{working.value} when it is not. Set the gap, or switch "
                    "the drawing to the datum the points are already on."
                )

        # The SU table is orthometric, so an ellipsoidal drawing needs the gap
        # to reach it -- but only when those altitudes are actually being used.
        # Unticked, every unit seeds as a full-height column off the section's
        # own extent, which is already on the drawing's datum.
        if (
            working is HeightDatum.ELLIPSOIDAL
            and self.use_elevations.isChecked()
            and any(
                c.include and (c.recorded_min is not None or c.recorded_max is not None)
                for c in self.candidates
            )
        ):
            return (
                "Units are being seeded from their recorded altitudes, which "
                "the database holds as orthometric, but the drawing is working "
                "in ellipsoidal heights with the gap between the two set to 0 "
                "- so those boxes will seed about 36 m below the photo. Set "
                "the gap, or untick \"Start units at their recorded heights\"."
            )
        return None

    def _refresh_datum_note(self) -> None:
        """The line under the datum controls: what was read, and what is wrong."""
        mismatch = self._datum_mismatch()
        parts = [p for p in (self._gcp_notes, mismatch) if p]
        self.datum_label.setText(" ".join(parts))
        self.datum_label.setStyleSheet("color: #b06000;" if mismatch else "")

    def _calibrate_separation(self) -> None:
        """Derive the separation from a known orthometric elevation.

        In practice: the top of the wall, whose orthometric elevation the SU
        table already records. Whatever the highest control point reads, the
        difference is the separation.
        """
        if not self.control_points:
            QMessageBox.information(self, "No control points", "Load control points first.")
            return
        # The recorded altitudes, which are what the answer is measured against,
        # rather than the seed boxes -- those already carry the offset.
        tops = [c.recorded_max for c in self.candidates if c.recorded_max is not None]
        suggestion = max(tops) if tops else 1031.0

        from qgis.PyQt.QtWidgets import QInputDialog

        value, ok = QInputDialog.getDouble(
            self, "Calibrate the vertical datum",
            "Known ORTHOMETRIC elevation of the top of this section (m).\n"
            "The highest control point will be made to equal it.",
            suggestion, -500.0, 9000.0, 3,
        )
        if not ok:
            return
        try:
            self.separation_spin.setValue(
                calibrate_separation(self.control_points, value)
            )
        except ValueError as exc:
            QMessageBox.information(self, "Nothing to work out", str(exc))

    # --------------------------------------------------------------- extent --

    def _extent_from_fit(self) -> None:
        if self.fit is None or not self._image_size[0]:
            return
        w, h = self._image_size
        corners = [self.fit.apply(x, y) for x, y in
                   ((0, 0), (w, 0), (0, h), (w, h))]
        zs = [c[1] for c in corners]
        self.zmin_spin.blockSignals(True)
        self.zmax_spin.blockSignals(True)
        self.zmin_spin.setValue(min(zs))
        self.zmax_spin.setValue(max(zs))
        self.zmin_spin.blockSignals(False)
        self.zmax_spin.blockSignals(False)
        self._commit_extent()
        self.extent_note.setText(
            f"Drawing spans {self.line.x_min:.2f} to {self.line.x_max:.2f} m "
            f"({self.line.drawing_width:.2f} m wide; trace is "
            f"{self.line.length:.2f} m)"
        )

    def _on_auto_extent_toggled(self, auto: bool) -> None:
        self.zmin_spin.setEnabled(not auto)
        self.zmax_spin.setEnabled(not auto)
        if auto:
            self._extent_from_fit()
        else:
            self.extent_note.setText(
                "Set by hand - these become the top and bottom of the section "
                "frame, with the trace you drew as its left and right."
            )

    def _sync_extent_source(self) -> None:
        """Only offer 'take it from the photo' when there is a placed photo.

        Without one the checkbox would sit ticked and do nothing, leaving the
        spin boxes greyed out and no obvious way to say how tall the section is.
        """
        has_fit = self.fit is not None
        self.auto_extent.setEnabled(has_fit)
        if not has_fit and self.auto_extent.isChecked():
            # Unticking runs _on_auto_extent_toggled, which enables the spin
            # boxes and updates the note.
            self.auto_extent.setChecked(False)

    def _on_extent_edited(self, _value: float) -> None:
        self._commit_extent()

    def _widen_to_content(self) -> None:
        """Grow the drawing surface to hold everything that will be on it.

        The trace is where chainage starts, not the limit of the drawing.
        Control points sit a few centimetres off the ends, a photo usually
        overlaps both, and an SU can run past the end of the line. Anything
        outside the box would be clipped from the figure, so the box takes in
        all of it, plus a margin.
        """
        self.line.reset_extent()

        # Control points, in the order they were surveyed along the wall.
        picked = [p for p in self.control_points if p.enabled]
        if picked:
            sep = self.separation_spin.value()
            datum = self._working_datum()
            xs = [p.section_xy(self.line, sep, datum)[0] for p in picked]
            self.line.extend_to(min(xs), max(xs), pad=EXTENT_PAD)

        # The placed photo's own footprint.
        if self.fit is not None and self._image_size[0]:
            w, h = self._image_size
            xs = [self.fit.apply(x, y)[0]
                  for x, y in ((0, 0), (w, 0), (0, h), (w, h))]
            self.line.extend_to(min(xs), max(xs), pad=0.0)

        # Every SU that will be drawn.
        included = [c for c in self.candidates if c.include and c.spans]
        if included:
            self.line.extend_to(
                min(c.x_min for c in included),
                max(c.x_max for c in included),
                pad=EXTENT_PAD,
            )

    def _commit_extent(self) -> None:
        lo, hi = self.zmin_spin.value(), self.zmax_spin.value()
        if hi <= lo:
            # Returning here without saying anything left the section holding
            # its previous heights while the boxes showed the new ones, and
            # Start drawing still enabled. Refuse instead.
            self._extent_ok = False
            self.extent_note.setText(
                "<span style='color:#c03000'>The top must be above the "
                "bottom.</span>"
            )
            self._update_ok_state()
            return
        self._extent_ok = True
        self.line.set_vertical_extent(lo, hi)
        self._widen_to_content()
        layer = self.su_layer_combo.currentLayer()
        if self.candidates and layer is not None:
            self._apply_seeding(layer)
            self._refresh_su_table()
        self._update_ok_state()

    # ---------------------------------------------------------------- accept --

    def _update_ok_state(self) -> None:
        # No requirement to have picked any units. Starting empty and adding
        # them from the panel as they are recognised on the photo is a
        # perfectly reasonable way to work, and it is the only way to start at
        # all when the units have not been recorded yet. All that is genuinely
        # needed is a drawing surface with a top and a bottom.
        ok = (
            getattr(self, "_extent_ok", True)
            and self.line.z_min is not None
            and self.line.z_max is not None
            and self.line.z_max > self.line.z_min
        )
        self.buttons.button(QDialogButtonBox.Ok).setEnabled(bool(ok))

    def _on_accept(self) -> None:
        """A single pre-flight window over the things people forget.

        Rather than a chain of separate yes/no prompts, everything that decides
        whether the drawing starts correct -- how the photo is placed, where its
        top and bottom came from, whether any units or control points were
        missed -- is gathered into one checklist. Anything wrong is in orange and
        the default falls on Go back; when it is all green the default is Start.
        """
        html, has_warning = self._preflight_summary()
        box = QMessageBox(self)
        box.setWindowTitle("Before you start drawing")
        box.setIcon(QMessageBox.Warning if has_warning else QMessageBox.Information)
        box.setTextFormat(Qt.RichText)
        box.setText(
            "Here is what the drawing will start with. Anything in orange is "
            "worth a look before you commit:"
        )
        box.setInformativeText(html)
        start = box.addButton("Start drawing", QMessageBox.AcceptRole)
        go_back = box.addButton("Go back", QMessageBox.RejectRole)
        box.setDefaultButton(go_back if has_warning else start)
        box.exec_()
        if box.clickedButton() is start:
            self.accept()

    def _preflight_summary(self) -> tuple[str, bool]:
        """Build the pre-flight checklist as (html, has_warning)."""
        items: list[tuple[bool, str]] = []

        # How the photo will be placed and which way round it will land.
        if self.photo_path and self.fit is None:
            items.append((False,
                "A photo is loaded but has not been placed - the drawing will "
                "have no backdrop. Pick control points, or clear the photo."))
        elif self.fit is not None and self.fit.is_mirrored:
            items.append((False,
                "The photo is mirrored (back to front): the right of the image "
                "will land on the left of the drawing. Use the red flip button "
                "before starting."))
        elif self.fit is not None and self.fit.rms > 0.10:
            items.append((False,
                f"The photo is placed, but the fit error is "
                f"{self.fit.rms*100:.1f} cm - that will be visible on the "
                "drawing."))
        elif self.fit is not None:
            items.append((True,
                f"Photo placed cleanly ({self.fit.model.label}, RMS "
                f"{self.fit.rms*1000:.0f} mm)."))
        else:
            items.append((True,
                "No photo - you'll draw over a blank section frame."))

        # Where the top and bottom of the drawing came from.
        zmin, zmax = self.zmin_spin.value(), self.zmax_spin.value()
        if self.fit is not None and self.auto_extent.isChecked():
            items.append((True,
                f"Top and bottom taken from the photo: {zmin:.2f} to "
                f"{zmax:.2f} m."))
        elif self.fit is not None:
            items.append((False,
                f"Top and bottom are typed by hand ({zmin:.2f} to {zmax:.2f} m) "
                "and override the photo - make sure that is intended."))
        else:
            items.append((True,
                f"Top and bottom set by hand: {zmin:.2f} to {zmax:.2f} m."))

        # Units, and whether the control points that were loaded got used.
        n_units = sum(1 for c in self.candidates if c.include)
        if n_units == 0:
            items.append((False,
                "No units are selected - you'll add and draw them by hand once "
                "you can see them on the photo."))
        else:
            items.append((True, f"{n_units} unit(s) selected to seed."))

        if self.control_points:
            picked = sum(1 for p in self.control_points if p.is_picked)
            if picked == 0:
                items.append((False,
                    "Control points are loaded but none were clicked on the "
                    "photo, so the photo cannot be placed."))

        # Which datum every height on the drawing is on. Always stated, because
        # it is what the numbers down the side of the finished figure mean.
        mismatch = self._datum_mismatch()
        if mismatch:
            items.append((False, mismatch))
        else:
            items.append((True, f"Heights are {self._working_datum().label}."))

        has_warning = any(not ok for ok, _ in items)
        rows = "".join(
            f"<li style='color:{'#2e7d32' if ok else '#c03000'}; "
            f"margin-bottom:4px'>{'&#10003;' if ok else '&#9888;'} {text}</li>"
            for ok, text in items
        )
        return f"<ul style='margin-left:-20px'>{rows}</ul>", has_warning

    # ------------------------------------------------------------- results --

    def result_candidates(self) -> list[SUCandidate]:
        return [c for c in self.candidates if c.include]

    def space_number(self) -> str | None:
        if self.mode_space.isChecked() and self.space_combo.currentText():
            text = self.space_combo.currentText()
            return text.split()[1] if text.startswith("Space ") else None
        return None
