"""The dock panel that stays open while a section is being drawn.

The main canvas is doing the drawing, so this panel only holds what the canvas
cannot: the SU roster, the state of each polygon, and the way out (the two
figure outputs and finishing the session).
"""

from __future__ import annotations

from qgis.core import Qgis, QgsMapLayerProxyModel
from qgis.gui import QgsCollapsibleGroupBox, QgsMapLayerComboBox
from qgis.PyQt.QtCore import Qt, pyqtSignal
from qgis.PyQt.QtGui import QColor
from qgis.PyQt.QtWidgets import (
    QAbstractItemView,
    QComboBox,
    QCheckBox,
    QDockWidget,
    QDoubleSpinBox,
    QFormLayout,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from .session import LABEL_MODES
from .su_source import SeedSource

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


#: Starting width of each roster column, in pixels: Show, SU, Label, Shows,
#: Type, Base, Top. Only a starting point -- every column can be dragged, and
#: the last one takes whatever is left over.
COLUMN_WIDTHS = (46, 52, 130, 110, 120, 74, 74)


def _list_sus(numbers, limit: int = 6) -> str:
    """'SU 12, SU 15 and 3 more' -- a roster change named, not just counted."""
    numbers = [str(n) for n in numbers]
    if len(numbers) <= limit:
        head = ", ".join(f"SU {n}" for n in numbers)
        return head
    shown = ", ".join(f"SU {n}" for n in numbers[:limit])
    return f"{shown} and {len(numbers) - limit} more"


class SectionPanel(QDockWidget):
    """SU roster and outputs for the open section."""

    finished = pyqtSignal()
    exportDigitized = pyqtSignal(str)         # title; the rest is set in the
    exportWireframe = pyqtSignal(str)         # export dialog
    saveRequested = pyqtSignal()
    #: The section has been repointed at a different SU layer (or none).
    sourceLayerChanged = pyqtSignal(object)

    def __init__(self, session, iface, source_layer=None, parent=None) -> None:
        super().__init__("Section drawing", parent)
        self.session = session
        self.iface = iface
        #: The SU layer the candidates came from, needed to add more later.
        self.source_layer = source_layer
        #: Set once the section is on its way out, so the close handler does not
        #: prompt again when the plugin removes the dock during teardown.
        self._finishing = False
        #: The frame-resize map tool, built the first time it is asked for.
        self._frame_tool = None
        #: What was on the canvas before the resize tool took it, so Escape
        #: hands digitising back rather than leaving the canvas toolless.
        self._saved_map_tool = None
        #: Guards the frame spin boxes against re-entering their own handler
        #: while they are being filled in from the session.
        self._frame_updating = False
        #: Same, for the SU layer combo.
        self._source_updating = False
        #: Set while the roster table is being rebuilt. The table's own
        #: blockSignals does not reach cell widgets, so the per-row label combos
        #: would otherwise fire on creation and write their defaults back.
        self._populating = False
        self.setObjectName("TkapSectionPanel")
        self.setAllowedAreas(Qt.LeftDockWidgetArea | Qt.RightDockWidgetArea)

        self._build_ui()
        self.refresh()

    def _build_ui(self) -> None:
        root = QWidget()
        layout = QVBoxLayout(root)

        self.summary_label = QLabel()
        self.summary_label.setWordWrap(True)
        layout.addWidget(self.summary_label)

        # Every section of the panel collapses. The roster is the one that has
        # to be able to grow, so it is the only one given stretch, and it starts
        # open -- collapsing it would leave the panel with nothing in it.
        units_box = QgsCollapsibleGroupBox("Stratigraphic units")
        units_box.setObjectName("TkapSectionUnitsBox")
        units_layout = QVBoxLayout(units_box)
        layout.addWidget(units_box, 1)

        self.table = QTableWidget(0, 7)
        self.table.setHorizontalHeaderLabels(
            ["Show", "SU", "Label", "Shows", "Type", "Base", "Top"]
        )
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        # Base and Top are typed straight into the cell; everything else is
        # read-only, guarded per-item in _refresh_row.
        self.table.setEditTriggers(
            QAbstractItemView.DoubleClicked | QAbstractItemView.SelectedClicked
            | QAbstractItemView.EditKeyPressed
        )
        self.table.verticalHeader().setVisible(False)
        header = self.table.horizontalHeader()
        # Every column is draggable. Stretch and ResizeToContents both look
        # tidy and both refuse to be dragged, which on a seven-column table in
        # a narrow dock means whichever column you need to read is the one you
        # cannot widen. Interactive everywhere, sized sensibly to start with,
        # and the last column takes up the slack.
        for column in range(self.table.columnCount()):
            header.setSectionResizeMode(column, QHeaderView.Interactive)
        for column, width in enumerate(COLUMN_WIDTHS):
            header.resizeSection(column, width)
        header.setStretchLastSection(True)
        header.setMinimumSectionSize(28)
        header.setSectionsMovable(True)
        self.table.itemSelectionChanged.connect(self._on_selection)
        self.table.itemChanged.connect(self._on_item_changed)
        units_layout.addWidget(self.table, 1)

        hint = QLabel(
            "Type a Base or Top value to move a unit to that height. Type a "
            "Label to name a unit something other than its number, and use "
            "Shows to choose what the drawing writes on it."
        )
        hint.setWordWrap(True)
        hint.setStyleSheet("color: grey; font-size: 10px;")
        units_layout.addWidget(hint)

        vis_row = QHBoxLayout()
        for label, tip, slot in (
            ("Show all", "Show every unit.",
             lambda: self._set_all_visible(True)),
            ("Hide all", "Hide every unit. They can still be snapped to.",
             lambda: self._set_all_visible(False)),
            ("Show only this", "Hide everything except the selected unit.",
             self._isolate_selected),
        ):
            b = QPushButton(label)
            b.setToolTip(tip)
            b.clicked.connect(slot)
            vis_row.addWidget(b)
        units_layout.addLayout(vis_row)

        row = QHBoxLayout()
        for label, tip, slot in (
            ("Zoom to", "Zoom to the selected unit.", self._zoom_to_selected),
            ("Add unit...", "Add a unit that was not picked up.", self._add_sus),
            ("Re-seed", "Replace the selected unit's polygon with a fresh box.",
             self._reseed_selected),
            ("Remove", "Take the selected unit off the drawing entirely.",
             self._remove_selected),
        ):
            b = QPushButton(label)
            b.setToolTip(tip)
            b.clicked.connect(slot)
            row.addWidget(b)
        units_layout.addLayout(row)

        layout.addWidget(self._build_sources_box())
        layout.addWidget(self._build_frame_box())
        layout.addWidget(self._build_output_box())

        save = QPushButton("Save section...")
        save.setToolTip("Save the drawing so it can be opened and edited later.")
        save.clicked.connect(lambda: self.saveRequested.emit())
        layout.addWidget(save)

        finish = QPushButton("Finish")
        finish.setToolTip("Close the section and put your project back.")
        finish.clicked.connect(self._on_finish)
        layout.addWidget(finish)

        self.setWidget(root)

    def _build_sources_box(self) -> QWidget:
        """Which SU layer this section reads from, and a way to change it.

        A section only carries its *drawing*; the SU layer stays in the project.
        Reopening one in a fresh QGIS therefore had nothing to add units from,
        and Add unit... dead-ended. The layer is now recorded when the section is
        saved and looked up on reopen, but a project that has been rebuilt, or a
        layer that has been renamed or moved, still needs pointing by hand --
        which is what this is for.
        """
        box = QgsCollapsibleGroupBox("Data sources")
        # Named so the collapsed state is remembered between sessions.
        box.setObjectName("TkapSectionSourcesBox")
        layout = QVBoxLayout(box)

        form = QFormLayout()
        self.source_combo = QgsMapLayerComboBox()
        self.source_combo.setFilters(QgsMapLayerProxyModel.PolygonLayer)
        self.source_combo.setAllowEmptyLayer(True)
        self.source_combo.setToolTip(
            "The SU layer this section reads units from. Its symbology is also "
            "what the exported drawing is styled with."
        )
        self.source_combo.layerChanged.connect(self._on_source_combo_changed)
        form.addRow("SU layer", self.source_combo)
        layout.addLayout(form)

        restyle = QPushButton("Re-apply symbology")
        restyle.setToolTip(
            "Take the section's colours from the SU layer again, or from a "
            ".qml style file."
        )
        restyle.clicked.connect(self._restyle)
        layout.addWidget(restyle)

        self.source_note = QLabel()
        self.source_note.setWordWrap(True)
        self.source_note.setStyleSheet("color: grey; font-size: 10px;")
        layout.addWidget(self.source_note)

        # Collapsed by default when the layer is already resolved -- there is
        # nothing to do here in the normal case. Left open when it is not, so a
        # reopened section that needs pointing says so where the fix is.
        box.setCollapsed(self.source_layer is not None)
        self._sources_box = box
        self._sync_source_widgets()
        return box

    def _sync_source_widgets(self) -> None:
        """Put the current source layer into the combo and describe the state."""
        self._source_updating = True
        try:
            self.source_combo.setLayer(self.source_layer)
        finally:
            self._source_updating = False
        if self.source_layer is None:
            self.source_note.setText(
                "No SU layer. Units cannot be added and the drawing will export "
                "with a plain fill until one is chosen."
            )
        else:
            self.source_note.setText(
                f"Adding units from '{self.source_layer.name()}'."
            )

    def _on_source_combo_changed(self, layer) -> None:
        if getattr(self, "_source_updating", False):
            return
        self.set_source_layer(layer)

    def set_source_layer(self, layer) -> None:
        """Repoint the section at an SU layer, telling everything that cares."""
        self.source_layer = layer
        self.session.source_layer = layer
        self._sync_source_widgets()
        # The plugin holds its own reference for export; keep it in step.
        self.sourceLayerChanged.emit(layer)

    def _build_frame_box(self) -> QWidget:
        """The drawing surface's extent, which is exactly what gets exported.

        Two ways in, because they suit different moments. Dragging the handles
        is how you crop against what you can see -- the usual case, where an
        ortho covers more wall than this section. The numbers are for when the
        section has to end at a stated chainage or elevation.

        Collapsible, and collapsed by default: cropping is a thing you do once
        per section at most, and the four spin boxes were taking up panel height
        that the SU roster wants.
        """
        box = QgsCollapsibleGroupBox("Section frame")
        # Named so the collapsed state is remembered between sessions.
        box.setObjectName("TkapSectionFrameBox")
        layout = QVBoxLayout(box)

        form = QFormLayout()
        self.frame_spins = {}
        for key, label, decimals, step in (
            ("x_min", "Chainage from", 3, 0.1),
            ("x_max", "Chainage to", 3, 0.1),
            ("z_min", "Elevation base", 3, 0.1),
            ("z_max", "Elevation top", 3, 0.1),
        ):
            spin = QDoubleSpinBox()
            spin.setDecimals(decimals)
            spin.setSingleStep(step)
            # Wide enough for an absolute elevation and for chainage cropped
            # past either end of the trace, which is allowed and is the point.
            spin.setRange(-10000.0, 10000.0)
            spin.setSuffix(" m")
            spin.setKeyboardTracking(False)     # fire once, not per keystroke
            spin.valueChanged.connect(self._on_frame_typed)
            self.frame_spins[key] = spin
            form.addRow(label, spin)
        layout.addLayout(form)

        self.resize_btn = QPushButton("Resize on canvas")
        self.resize_btn.setCheckable(True)
        self.resize_btn.setToolTip(
            "Drag the frame's corners and edges on the map. Press Escape or "
            "click again to go back to the normal tools."
        )
        self.resize_btn.toggled.connect(self._on_resize_toggled)
        layout.addWidget(self.resize_btn)

        btns = QHBoxLayout()
        for label, tip, slot in (
            ("Fit to photo", "Snap the frame to the placed photo's extent.",
             self._fit_frame_to_photo),
            # Named for what it does, not for what "reset" might suggest:
            # there is no earlier vertical extent to go back to, since the
            # elevations came from the photo placement or were typed in.
            ("Reset chainage",
             "Back to the full chainage of the trace you drew. The elevation "
             "limits are left as they are - use Fit to photo for those.",
             self._reset_frame),
        ):
            b = QPushButton(label)
            b.setToolTip(tip)
            b.clicked.connect(slot)
            btns.addWidget(b)
        layout.addLayout(btns)

        hint = QLabel(
            "The frame is what the exported drawing covers, so cropping it "
            "here crops the figure - no need to cut down the ortho."
        )
        hint.setWordWrap(True)
        hint.setStyleSheet("color: grey; font-size: 10px;")
        layout.addWidget(hint)
        box.setCollapsed(True)
        return box

    # ----------------------------------------------------------------- frame --

    def refresh_frame(self) -> None:
        """Put the line's current extent into the spin boxes."""
        line = self.session.line
        if line.z_min is None or line.z_max is None:
            return
        values = {
            "x_min": line.x_min, "x_max": line.x_max,
            "z_min": line.z_min, "z_max": line.z_max,
        }
        self._frame_updating = True
        try:
            for key, spin in self.frame_spins.items():
                spin.setValue(values[key])
        finally:
            self._frame_updating = False

    def _on_frame_typed(self) -> None:
        """Apply typed extents, silently ignoring the ones that make no box."""
        if getattr(self, "_frame_updating", False):
            return
        try:
            self.session.set_frame(
                self.frame_spins["x_min"].value(),
                self.frame_spins["z_min"].value(),
                self.frame_spins["x_max"].value(),
                self.frame_spins["z_max"].value(),
            )
        except ValueError:
            # Half-typed values pass through here constantly (a "to" below a
            # "from" while the second number is still being entered), so this is
            # not worth a dialog. The frame simply does not move until the pair
            # makes a box.
            return
        if self._frame_tool is not None:
            self._frame_tool.sync_from_session()
        self._resync_units()
        self.iface.mapCanvas().refresh()

    def _resync_units(self) -> None:
        """Re-decide the roster for the frame as it now stands, and report it.

        Run after every route that moves the frame, because which units the
        section holds is a consequence of what it covers. Silent when nothing
        changed, which is the common case once a section has settled.
        """
        changes = self.session.resync_to_frame(self.source_layer)
        if not any(changes.values()):
            return
        self.refresh()

        parts = []
        if changes["added"]:
            parts.append(f"added {_list_sus(changes['added'])}")
        if changes["removed"]:
            parts.append(f"removed {_list_sus(changes['removed'])}")
        message = f"Frame changed: {', '.join(parts)}." if parts else ""
        if changes["kept"]:
            # Worth saying out loud. These are outside the frame, so they will
            # not appear in the export, but they are still in the section and
            # still hold whatever was drawn on them.
            message += (
                f" {_list_sus(changes['kept'])} now sit outside the frame but "
                "were kept because they have been drawn on."
            )
        if message.strip():
            self.iface.messageBar().pushMessage(
                "TKAP Section", message.strip(),
                level=Qgis.Info if not changes["kept"] else Qgis.Warning,
                duration=9,
            )

    def _on_resize_toggled(self, on: bool) -> None:
        canvas = self.iface.mapCanvas()
        if not on:
            if self._frame_tool is not None and canvas.mapTool() is self._frame_tool:
                canvas.unsetMapTool(self._frame_tool)
            return

        from .frame_tool import FrameResizeTool

        if self._frame_tool is None:
            self._frame_tool = FrameResizeTool(canvas, self.session)
            self._frame_tool.frameChanged.connect(self._on_frame_dragged)
            # QgsMapTool's own signal, so the button also unticks when QGIS
            # switches tools on its own -- picking the vertex tool, say.
            self._frame_tool.deactivated.connect(self._on_tool_deactivated)
        self._saved_map_tool = canvas.mapTool()
        canvas.setMapTool(self._frame_tool)

    def _on_tool_deactivated(self) -> None:
        if self.resize_btn.isChecked():
            self.resize_btn.setChecked(False)
        # Hand the canvas back to whatever was in use, so Escape returns to
        # digitising rather than to no tool at all.
        if self._saved_map_tool is not None:
            canvas = self.iface.mapCanvas()
            if canvas.mapTool() is None:
                canvas.setMapTool(self._saved_map_tool)
            self._saved_map_tool = None

    def _on_frame_dragged(self, x_min, z_min, x_max, z_max) -> None:
        self.refresh_frame()
        self._resync_units()

    def _fit_frame_to_photo(self) -> None:
        if not self.session.fit_frame_to_photo():
            QMessageBox.information(
                self, "No photo",
                "This section has no placed photo to fit the frame to.",
            )
            return
        self._after_frame_change()

    def _reset_frame(self) -> None:
        self.session.reset_frame()
        self._after_frame_change()

    def _after_frame_change(self) -> None:
        self.refresh_frame()
        if self._frame_tool is not None:
            self._frame_tool.sync_from_session()
        self._resync_units()
        self.iface.mapCanvas().refresh()

    def release_frame_tool(self) -> None:
        """Give the canvas back and drop the tool. Called as the section ends."""
        if self._frame_tool is None:
            return
        canvas = self.iface.mapCanvas()
        if canvas.mapTool() is self._frame_tool:
            canvas.unsetMapTool(self._frame_tool)
        self._frame_tool.cleanup()
        self._frame_tool = None

    def _build_output_box(self) -> QWidget:
        box = QgsCollapsibleGroupBox("Export")
        box.setObjectName("TkapSectionExportBox")
        layout = QVBoxLayout(box)

        self.title_edit = QLineEdit(self.session.default_title())
        title_form = QFormLayout()
        title_form.addRow("Title", self.title_edit)
        layout.addLayout(title_form)

        digitized = QPushButton("Digitized drawing...")
        digitized.setToolTip(
            "Coloured, numbered units with a legend. No photo."
        )
        digitized.clicked.connect(lambda: self.exportDigitized.emit(self.title_edit.text()))
        layout.addWidget(digitized)

        wire = QPushButton("Wireframe drawing...")
        wire.setToolTip("Unit outlines and numbers drawn over the photo.")
        wire.clicked.connect(lambda: self.exportWireframe.emit(self.title_edit.text()))
        layout.addWidget(wire)

        hint = QLabel("You can set the page and scale in the next window.")
        hint.setWordWrap(True)
        hint.setStyleSheet("color: grey; font-size: 10px;")
        layout.addWidget(hint)
        return box

    # ---------------------------------------------------------------- table --

    def refresh(self) -> None:
        cands = self.session.candidates
        note = (
            "" if cands else
            "<br><i>No units yet. Use Add unit... to bring some in, or draw "
            "straight onto the photo.</i>"
        )
        self.summary_label.setText(
            f"<b>{self.session.line.name}</b><br>{self.session.summary()}{note}"
        )
        # Repopulating fires itemChanged for every cell; without this the
        # handlers would run against half-built rows.
        self.table.blockSignals(True)
        self._populating = True
        try:
            self.table.setRowCount(len(cands))
            for row, cand in enumerate(cands):
                self._fill_row(row, cand)
        finally:
            self._populating = False
            self.table.blockSignals(False)
        self.refresh_frame()

    def _fill_row(self, row: int, cand) -> None:
        present = self.session.has_polygon_for(cand.su_id)

        show = QTableWidgetItem()
        show.setFlags(
            Qt.ItemIsUserCheckable | Qt.ItemIsEnabled | Qt.ItemIsSelectable
        )
        show.setCheckState(
            Qt.Unchecked if present and self.session.is_hidden(cand.su_id)
            else Qt.Checked
        )
        show.setData(Qt.UserRole, cand.su_id)
        self.table.setItem(row, 0, show)

        for col, text in ((1, cand.su_number), (4, cand.describe_type())):
            item = QTableWidgetItem(text)
            item.setFlags(Qt.ItemIsEnabled | Qt.ItemIsSelectable)
            if not present:
                item.setForeground(BAD)
            self.table.setItem(row, col, item)

        # The unit's own name for the drawing, free text and editable.
        label_item = QTableWidgetItem(self.session.label_for(cand.su_id))
        if present:
            label_item.setFlags(
                Qt.ItemIsEnabled | Qt.ItemIsSelectable | Qt.ItemIsEditable
            )
        else:
            label_item.setFlags(Qt.ItemIsEnabled | Qt.ItemIsSelectable)
        label_item.setData(Qt.UserRole, cand.su_id)
        label_item.setToolTip(
            "A name for this unit on the drawing. Leave empty to use its "
            "number, and set Shows to decide which is used."
        )
        self.table.setItem(row, 2, label_item)

        # What this one unit's label says. A combo per row rather than one
        # setting for the drawing: a section usually wants numbers throughout
        # with two or three units named, and a thin lens left blank.
        combo = QComboBox()
        for mode, text in LABEL_MODES:
            combo.addItem(text, mode)
        current = self.session.label_mode_for(cand.su_id)
        index = combo.findData(current)
        if index >= 0:
            combo.setCurrentIndex(index)
        combo.setEnabled(present)
        combo.setToolTip(
            "What the drawing writes on this unit. \"Label\" falls back to the "
            "number when no label has been typed."
        )
        combo.currentIndexChanged.connect(
            lambda _i, c=combo, su_id=cand.su_id: self._on_label_mode_changed(c, su_id)
        )
        self.table.setCellWidget(row, 3, combo)

        lo, hi = self._current_extent(cand)
        for col, value in ((5, lo), (6, hi)):
            item = QTableWidgetItem("" if value is None else f"{value:.3f}")
            if present:
                item.setFlags(
                    Qt.ItemIsEnabled | Qt.ItemIsSelectable | Qt.ItemIsEditable
                )
            else:
                item.setFlags(Qt.ItemIsEnabled | Qt.ItemIsSelectable)
            item.setData(Qt.UserRole, cand.su_id)
            self.table.setItem(row, col, item)

    def _current_extent(self, cand):
        """The polygon's actual top and bottom, which is what should be shown.

        Reading the geometry rather than the candidate means the cells track
        what has been dragged on the canvas, not just what was typed here.
        """
        layer = self.session.polygon_layer
        if layer is None:
            return (cand.alt_min, cand.alt_max)
        boxes = [f.geometry().boundingBox() for f in layer.getFeatures()
                 if f["su_id"] == cand.su_id and f.hasGeometry()]
        if not boxes:
            return (None, None)
        return (min(b.yMinimum() for b in boxes),
                max(b.yMaximum() for b in boxes))

    # -------------------------------------------------------------- editing --

    def _on_item_changed(self, item) -> None:
        if item.column() == 0:
            su_id = item.data(Qt.UserRole)
            if su_id is not None:
                self.session.set_hidden(su_id, item.checkState() != Qt.Checked)
            return
        if item.column() == 2:
            su_id = item.data(Qt.UserRole)
            if su_id is not None:
                self.session.set_label(su_id, item.text())
            return
        if item.column() in (5, 6):
            self._on_extent_typed(item)

    def _on_label_mode_changed(self, combo, su_id) -> None:
        """Apply a per-unit label choice, unless the table is being rebuilt.

        Cell widgets are not covered by the table's blockSignals, so populating
        a row fires this; without the guard, refreshing the roster would write
        every combo's default back over what was chosen.
        """
        if self._populating:
            return
        mode = combo.currentData()
        if mode:
            self.session.set_label_mode_for(su_id, mode)

    def _on_extent_typed(self, item) -> None:
        """Apply a typed Base or Top to the unit's polygon."""
        row = item.row()
        cands = self.session.candidates
        if row >= len(cands):
            return
        cand = cands[row]

        lo_item = self.table.item(row, 5)
        hi_item = self.table.item(row, 6)
        if lo_item is None or hi_item is None:
            return
        try:
            lo = float(lo_item.text())
            hi = float(hi_item.text())
        except ValueError:
            self._revert_row(row, cand)
            return
        if hi <= lo:
            # Silently reverting would look like the typing did nothing, so say
            # why, once, and put the old values back.
            QMessageBox.warning(
                self, "Check the values",
                f"The top ({hi:.3f} m) must be above the base ({lo:.3f} m).",
            )
            self._revert_row(row, cand)
            return

        self.session.set_elevations(cand, lo, hi)
        self._revert_row(row, cand)      # redraw from the geometry that resulted

    def _revert_row(self, row: int, cand) -> None:
        self.table.blockSignals(True)
        self._populating = True
        try:
            self._fill_row(row, cand)
        finally:
            self._populating = False
            self.table.blockSignals(False)

    def _set_all_visible(self, visible: bool) -> None:
        self.session.set_all_hidden(not visible)
        self.refresh()

    def _isolate_selected(self) -> None:
        """Show only the selected unit. Everything else stays snappable."""
        cand = self._selected_candidate()
        if cand is None:
            QMessageBox.information(
                self, "Nothing selected", "Select a unit in the table first."
            )
            return
        self.session.isolate(cand.su_id)
        self.refresh()

    # -------------------------------------------------------------- symbology --

    def _restyle(self) -> None:
        """Re-apply the project symbology without restarting the section.

        Offers the SU layer first, then a style file, because the usual cause of
        an unstyled section is the SU layer itself not having been styled yet
        when the section was opened.
        """
        from qgis.PyQt.QtWidgets import QFileDialog, QInputDialog

        choices = []
        if self.source_layer is not None:
            choices.append(f"From the SU layer ({self.source_layer.name()})")
        choices.append("From a style file (.qml)...")

        choice, ok = QInputDialog.getItem(
            self, "Re-apply symbology", "Take the section's symbology:",
            choices, 0, False,
        )
        if not ok:
            return

        qml = None
        if choice.startswith("From a style file"):
            from .setup_dialog import STYLE_SETTING_KEY
            from qgis.PyQt.QtCore import QSettings

            start = QSettings().value(STYLE_SETTING_KEY, "", type=str)
            qml, _ = QFileDialog.getOpenFileName(
                self, "Layer style", start,
                "QGIS layer style (*.qml);;All files (*)",
            )
            if not qml:
                return
            QSettings().setValue(STYLE_SETTING_KEY, qml)
        else:
            # Clear any remembered file so the layer really is the source.
            self.session.style_qml = None

        styled = self.session.restyle(
            source_layer=self.source_layer, style_qml=qml
        )
        if styled:
            QMessageBox.information(
                self, "Symbology applied",
                "The section units now use the project symbology.",
            )
        else:
            QMessageBox.warning(
                self, "Nothing to inherit",
                "No categorised symbology was found, so the section is using a "
                "plain fill.\n\nApply the project's style to the SU layer, or "
                "point this at the .qml directly.",
            )

    # ---------------------------------------------------------------- adding --

    def _add_sus(self) -> None:
        """Bring SUs into the session that were not picked up at setup."""
        if self.source_layer is None and not self._ask_for_source_layer():
            return

        from .add_su_dialog import AddSUDialog

        dialog = AddSUDialog(
            self.session, self.source_layer, self
        )
        if dialog.exec_() != dialog.Accepted:
            return
        chosen = dialog.chosen()
        if not chosen:
            return
        added = self.session.add_candidates(chosen)
        self.refresh()
        self.iface.messageBar().pushMessage(
            "TKAP Section",
            f"Added {added} SU{'s' if added != 1 else ''} to the section.",
            duration=6,
        )

    def _ask_for_source_layer(self) -> bool:
        """Offer to pick an SU layer right here. True if one was chosen.

        Asked at the point of use rather than left as a refusal: someone who has
        just reopened a saved section and pressed Add unit... wants the layer
        chosen, not a message telling them there isn't one.
        """
        from qgis.PyQt.QtWidgets import QInputDialog
        from qgis.core import QgsProject, QgsWkbTypes

        choices = [
            layer for layer in QgsProject.instance().mapLayers().values()
            if hasattr(layer, "geometryType")
            and layer.geometryType() == QgsWkbTypes.PolygonGeometry
        ]
        if not choices:
            QMessageBox.information(
                self, "No SU layer",
                "This section has no SU layer to add units from, and there are "
                "no polygon layers loaded to choose one from.\n\nLoad the SU "
                "layer into the project, then pick it under Data sources in "
                "this panel.",
            )
            return False

        names = sorted(layer.name() for layer in choices)
        name, ok = QInputDialog.getItem(
            self, "Which SU layer?",
            "This section has no SU layer to add units from.\n"
            "Pick the layer the units should come from:",
            names, 0, False,
        )
        if not ok:
            return False
        for layer in choices:
            if layer.name() == name:
                self.set_source_layer(layer)
                return True
        return False

    def _selected_candidate(self):
        rows = self.table.selectionModel().selectedRows()
        if not rows:
            return None
        idx = rows[0].row()
        cands = self.session.candidates
        return cands[idx] if idx < len(cands) else None

    def _on_selection(self) -> None:
        cand = self._selected_candidate()
        if cand is None or self.session.polygon_layer is None:
            return
        layer = self.session.polygon_layer
        ids = [f.id() for f in layer.getFeatures() if f["su_id"] == cand.su_id]
        layer.selectByIds(ids)

    def _zoom_to_selected(self) -> None:
        cand = self._selected_candidate()
        if cand is not None and not self.session.zoom_to_su(cand.su_id):
            QMessageBox.information(
                self, "Nothing to zoom to",
                f"SU {cand.su_number} has no polygon in this session.",
            )

    def _remove_selected(self) -> None:
        """Take the selected unit out of the section: its polygon and its row."""
        cand = self._selected_candidate()
        if cand is None:
            QMessageBox.information(
                self, "Nothing selected", "Select a unit in the table first."
            )
            return
        cand.include = False
        self.session.remove_candidate(cand.su_id)
        self.refresh()

    def _reseed_selected(self) -> None:
        cand = self._selected_candidate()
        if cand is None:
            return
        if self.session.has_polygon_for(cand.su_id):
            answer = QMessageBox.question(
                self, "Replace the polygon?",
                f"SU {cand.su_number} already has a polygon. Replace it with a "
                "fresh seed box? Any editing done to it will be lost.",
                QMessageBox.Yes | QMessageBox.No, QMessageBox.No,
            )
            if answer != QMessageBox.Yes:
                return
            self.session.remove_su(cand.su_id)
        self.session.reseed_one(cand)
        self.refresh()

    # -------------------------------------------------------------- editing --

    def _on_finish(self) -> None:
        self._request_finish(save=False)

    def _request_finish(self, *, save: bool) -> bool:
        """Commit-check, optionally save, then finish. Returns whether finishing.

        Shared by the Finish button and by closing the panel so both routes end
        the section the same way -- and, crucially, both run the restore path
        that puts the project CRS, layer visibility and snapping back. Closing
        the dock used to skip all of that and leave the project in section CRS.
        """
        layer = self.session.polygon_layer
        if layer is not None and layer.isEditable() and layer.isModified():
            if save:
                # Save is about to read the layer, so fold the edits in first.
                layer.commitChanges()
            else:
                answer = QMessageBox.question(
                    self, "Unsaved edits",
                    "The section layer has uncommitted edits.\n\n"
                    "Commit them before finishing?",
                    QMessageBox.Yes | QMessageBox.No | QMessageBox.Cancel,
                    QMessageBox.Yes,
                )
                if answer == QMessageBox.Cancel:
                    return False
                if answer == QMessageBox.Yes:
                    layer.commitChanges()
        self._finishing = True
        # Before anything else tears down: the tool holds rubber bands on the
        # canvas and a reference to the session, neither of which should outlive
        # the section.
        self.release_frame_tool()
        if save:
            self.saveRequested.emit()
        self.finished.emit()
        return True

    def closeEvent(self, event) -> None:
        """Closing the panel tears the section down, so make sure the project is
        put back rather than left in section-local CRS.

        People close the dock instead of pressing Finish, then wonder why their
        project is in a strange coordinate system. This intercepts the close,
        asks what they meant, and routes every answer through the same restore
        path as Finish.
        """
        if self._finishing:
            # Already on the way out (Finish button, or the plugin removing the
            # dock during teardown): let it close without a second prompt.
            event.accept()
            return

        box = QMessageBox(self)
        box.setWindowTitle("Close the section?")
        box.setIcon(QMessageBox.Question)
        box.setText("Are you done with this section?")
        box.setInformativeText(
            "Closing it restores your project's coordinate system, layer "
            "visibility and snapping. That only happens when the section is "
            "finished - leaving the drawing open keeps the project in section "
            "coordinates."
        )
        save_btn = box.addButton("Save and finish", QMessageBox.AcceptRole)
        box.addButton("Finish without saving", QMessageBox.DestructiveRole)
        keep_btn = box.addButton("Keep working", QMessageBox.RejectRole)
        box.setDefaultButton(keep_btn)
        box.exec_()
        clicked = box.clickedButton()

        if clicked is keep_btn:
            event.ignore()
            return
        if self._request_finish(save=clicked is save_btn):
            event.accept()
        else:
            # The commit prompt was cancelled: keep the panel open.
            event.ignore()
