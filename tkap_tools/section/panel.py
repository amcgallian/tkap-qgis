"""The dock panel that stays open while a section is being drawn.

The main canvas is doing the drawing, so this panel only holds what the canvas
cannot: the SU roster, the state of each polygon, and the way out (the two
figure outputs and finishing the session).
"""

from __future__ import annotations

from qgis.PyQt.QtCore import Qt, pyqtSignal
from qgis.PyQt.QtGui import QColor
from qgis.PyQt.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
    QDockWidget,
    QDoubleSpinBox,
    QFormLayout,
    QGroupBox,
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


class SectionPanel(QDockWidget):
    """SU roster and outputs for the open section."""

    finished = pyqtSignal()
    exportDigitized = pyqtSignal(str)         # title; the rest is set in the
    exportWireframe = pyqtSignal(str)         # export dialog
    saveRequested = pyqtSignal()

    def __init__(self, session, iface, source_layer=None, parent=None) -> None:
        super().__init__("Section drawing", parent)
        self.session = session
        self.iface = iface
        #: The SU layer the candidates came from, needed to add more later.
        self.source_layer = source_layer
        #: Set once the section is on its way out, so the close handler does not
        #: prompt again when the plugin removes the dock during teardown.
        self._finishing = False
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

        self.table = QTableWidget(0, 5)
        self.table.setHorizontalHeaderLabels(
            ["Show", "SU", "Type", "Base", "Top"]
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
        header.setSectionResizeMode(0, QHeaderView.ResizeToContents)
        header.setSectionResizeMode(2, QHeaderView.Stretch)
        self.table.itemSelectionChanged.connect(self._on_selection)
        self.table.itemChanged.connect(self._on_item_changed)
        layout.addWidget(self.table, 1)

        hint = QLabel("Type a Base or Top value to move a unit to that height.")
        hint.setWordWrap(True)
        hint.setStyleSheet("color: grey; font-size: 10px;")
        layout.addWidget(hint)

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
        layout.addLayout(vis_row)

        row = QHBoxLayout()
        for label, tip, slot in (
            ("Zoom to", "Zoom to the selected unit.", self._zoom_to_selected),
            ("Add unit...", "Add a unit that was not picked up.", self._add_sus),
            ("Remove", "Take the selected unit off the drawing.",
             self._remove_selected),
        ):
            b = QPushButton(label)
            b.setToolTip(tip)
            b.clicked.connect(slot)
            row.addWidget(b)
        layout.addLayout(row)

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

    def _build_output_box(self) -> QWidget:
        box = QGroupBox("Export")
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
        self.table.setRowCount(len(cands))
        for row, cand in enumerate(cands):
            self._fill_row(row, cand)
        self.table.blockSignals(False)

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

        for col, text in ((1, cand.su_number), (2, cand.describe_type())):
            item = QTableWidgetItem(text)
            item.setFlags(Qt.ItemIsEnabled | Qt.ItemIsSelectable)
            if not present:
                item.setForeground(BAD)
            self.table.setItem(row, col, item)

        lo, hi = self._current_extent(cand)
        for col, value in ((3, lo), (4, hi)):
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
        if item.column() in (3, 4):
            self._on_extent_typed(item)

    def _on_extent_typed(self, item) -> None:
        """Apply a typed Base or Top to the unit's polygon."""
        row = item.row()
        cands = self.session.candidates
        if row >= len(cands):
            return
        cand = cands[row]

        lo_item = self.table.item(row, 3)
        hi_item = self.table.item(row, 4)
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
        self._fill_row(row, cand)
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
        if self.source_layer is None:
            QMessageBox.information(
                self, "No SU layer",
                "This session has no source SU layer to add from.",
            )
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
        cand = self._selected_candidate()
        if cand is None:
            return
        removed = self.session.remove_su(cand.su_id)
        if removed:
            cand.include = False
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
