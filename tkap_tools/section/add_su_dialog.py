"""Add SUs to a section that is already open.

The roster is not settled when the drawing window opens. A unit can be missed
because the buffer was tight, or only become recognisable once the rectified
photo is on screen, or turn out to be visible in the wall despite its plan-view
polygon sitting slightly off the trace. All of those need to be addable without
throwing the section away and starting again.

Two lists, because the two cases are different:

* **Near the trace** -- found by widening the search buffer. These arrive with a
  real chainage extent measured from their plan geometry.
* **Anywhere in the layer** -- found by SU number. These have no extent on this
  wall, so they are seeded across the middle of the section and flagged as
  placed by hand.
"""

from __future__ import annotations

from dataclasses import replace

from qgis.core import QgsFeatureRequest
from qgis.PyQt.QtCore import Qt
from qgis.PyQt.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
    QDialog,
    QDialogButtonBox,
    QHeaderView,
    QLabel,
    QLineEdit,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
)

from .section_geom import Span
from .su_source import FieldMap, SUCandidate, build_candidate, discover_spatial

#: How much to widen the trace buffer when hunting for units that were missed.
NEARBY_BUFFER_FACTOR = 4.0
NEARBY_BUFFER_MIN = 1.0

#: Where a hand-placed SU is seeded along the wall, as fractions of the length.
HAND_PLACED_SPAN = (0.40, 0.60)


class AddSUDialog(QDialog):
    """Pick SUs to bring into an open section."""

    def __init__(self, session, source_layer, parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Add units")
        self.resize(680, 520)

        self.session = session
        self.layer = source_layer
        self._rows: list[SUCandidate] = []

        self._build_ui()
        self.reload()

    def _build_ui(self) -> None:
        layout = QVBoxLayout(self)

        self.info = QLabel()
        self.info.setWordWrap(True)
        layout.addWidget(self.info)

        self.filter_edit = QLineEdit()
        self.filter_edit.setPlaceholderText("Filter by SU number or type...")
        self.filter_edit.textChanged.connect(self._apply_filter)
        layout.addWidget(self.filter_edit)

        self.show_all = QCheckBox("Show every unit in the layer")
        self.show_all.setToolTip(
            "Units away from this wall are placed in the middle of the drawing "
            "for you to move into position."
        )
        self.show_all.toggled.connect(self.reload)
        layout.addWidget(self.show_all)

        self.table = QTableWidget(0, 5)
        self.table.setHorizontalHeaderLabels(
            ["Add", "SU", "Type", "Along the wall (m)", "Where from"]
        )
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.table.verticalHeader().setVisible(False)
        header = self.table.horizontalHeader()
        header.setSectionResizeMode(0, QHeaderView.ResizeToContents)
        header.setSectionResizeMode(2, QHeaderView.Stretch)
        layout.addWidget(self.table, 1)

        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.button(QDialogButtonBox.Ok).setText("Add selected")
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    # ---------------------------------------------------------------- loading --

    def reload(self) -> None:
        line = self.session.line
        already = {c.su_id for c in self.session.candidates}

        wide = replace(
            line,
            buffer=max(line.buffer * NEARBY_BUFFER_FACTOR, NEARBY_BUFFER_MIN),
        )
        try:
            nearby = [
                c for c in discover_spatial(self.layer, wide)
                if c.su_id not in already
            ]
        except Exception:
            nearby = []
        for c in nearby:
            c.include = False
        found = {c.su_id for c in nearby}

        distant: list[SUCandidate] = []
        if self.show_all.isChecked():
            fmap = FieldMap.sniff(self.layer)
            lo = line.length * HAND_PLACED_SPAN[0]
            hi = line.length * HAND_PLACED_SPAN[1]
            req = QgsFeatureRequest()
            req.setFlags(QgsFeatureRequest.NoGeometry)
            for feat in self.layer.getFeatures(req):
                if feat.id() in already or feat.id() in found:
                    continue
                cand = build_candidate(
                    feat, fmap, [Span(lo, hi, on_trace=False)]
                )
                cand.include = False
                cand.on_trace = False
                distant.append(cand)
            distant.sort(key=lambda c: c.su_number)

        self._rows = nearby + distant
        self.info.setText(
            f"<b>{len(nearby)}</b> unit(s) near this wall that are not in the "
            "drawing yet"
            + (f", plus <b>{len(distant)}</b> elsewhere in the layer."
               if self.show_all.isChecked() else ".")
        )
        self._populate()

    def _populate(self) -> None:
        self.table.setRowCount(len(self._rows))
        for row, cand in enumerate(self._rows):
            add = QTableWidgetItem()
            add.setFlags(
                Qt.ItemIsUserCheckable | Qt.ItemIsEnabled | Qt.ItemIsSelectable
            )
            add.setCheckState(Qt.Checked if cand.include else Qt.Unchecked)
            add.setData(Qt.UserRole, row)
            self.table.setItem(row, 0, add)

            self.table.setItem(row, 1, QTableWidgetItem(cand.su_number))
            self.table.setItem(row, 2, QTableWidgetItem(cand.describe_type()))
            spans = ", ".join(f"{s.x_min:.2f}-{s.x_max:.2f}" for s in cand.spans)
            self.table.setItem(row, 3, QTableWidgetItem(spans))
            self.table.setItem(
                row, 4,
                QTableWidgetItem(
                    "on this wall" if cand.on_trace
                    else "elsewhere - move it into place"
                ),
            )
        self._apply_filter(self.filter_edit.text())

    def _apply_filter(self, text: str) -> None:
        needle = (text or "").strip().lower()
        for row, cand in enumerate(self._rows):
            haystack = f"{cand.su_number} {cand.describe_type()}".lower()
            self.table.setRowHidden(row, bool(needle) and needle not in haystack)

    # ---------------------------------------------------------------- result --

    def chosen(self) -> list[SUCandidate]:
        """Ticked candidates, with their vertical extent seeded.

        Seeded here rather than by the caller because the caller has no way to
        know whether a row came with a measured extent or was placed by hand.
        """
        from .su_source import apply_seed_cascade

        picked = []
        for row, cand in enumerate(self._rows):
            item = self.table.item(row, 0)
            if item is not None and item.checkState() == Qt.Checked:
                cand.include = True
                picked.append(cand)
        if picked:
            apply_seed_cascade(picked, self.session.line)
        return picked
