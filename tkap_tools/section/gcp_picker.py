"""Picking control points on a photo, and the fit that comes out of it.

Pulled out of the setup dialog so that *re-linking* a saved section's backdrop
can reuse it. That is not a tidiness argument. A re-link happens to a section
that is already drawn, so the one thing it must not do is change the section:
the trace, the flip, the datum and the vertical extent are all baked into the
polygons on disk, and moving any of them silently invalidates the drawing.

The setup dialog can change all of those, and does -- a fit landing there fans
out into the flip warning, the vertical extent, the widened drawing surface and
the re-seeding of every unit. Guarding each of those against a relink would mean
being right about eleven separate paths every time this file is edited. So the
guarantee is structural instead: this widget reads the line it is given and
never writes to it, and everything that does write lives in the dialog on the
other side of :attr:`GcpPickerWidget.fitChanged`.

:attr:`GcpPickerWidget.extra_layout` is an empty slot in the right-hand column,
where the setup dialog puts its datum controls and a relink puts a read-only
statement of the datum it is stuck with.
"""

from __future__ import annotations

from qgis.PyQt.QtCore import Qt, pyqtSignal
from qgis.PyQt.QtGui import QColor
from qgis.PyQt.QtWidgets import (
    QAbstractItemView,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QSplitter,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from .gcp_view import PhotoView, PlacedPreview
from .photo import (
    DEFAULT_WORKING_DATUM,
    ControlPoint,
    Fit,
    FitModel,
    HeightDatum,
    best_model_for,
    fit_transform,
    load_emlid_csv,
    select_for_section,
    suggest_separation,
)
from .section_geom import SectionLine

GOOD = QColor(60, 160, 60)
WARN = QColor(200, 130, 0)
BAD = QColor(200, 50, 50)

#: What a section photo can be opened from. Shared so the setup dialog and a
#: relink offer exactly the same thing.
PHOTO_FILTER = "Images (*.tif *.tiff *.jpg *.jpeg *.png *.vrt);;All files (*)"


def lonlat_transformer():
    """(lon, lat) -> EPSG:32636, for Emlid files with empty projected columns."""
    from osgeo import osr

    osr.UseExceptions()
    src = osr.SpatialReference(); src.ImportFromEPSG(4326)
    src.SetAxisMappingStrategy(osr.OAMS_TRADITIONAL_GIS_ORDER)
    dst = osr.SpatialReference(); dst.ImportFromEPSG(32636)
    dst.SetAxisMappingStrategy(osr.OAMS_TRADITIONAL_GIS_ORDER)
    tr = osr.CoordinateTransformation(src, dst)

    def convert(lon: float, lat: float) -> tuple[float, float]:
        e, n, _ = tr.TransformPoint(lon, lat)
        return e, n

    return convert


class GcpPickerWidget(QWidget):
    """The photo, the control-point table, and the fit between them."""

    #: A new fit, or None when there is not enough to make one.
    fitChanged = pyqtSignal(object)
    #: A photo was loaded or cleared. Carries the path.
    photoChanged = pyqtSignal(str)
    #: What the control-point file had to say about itself, for the host to
    #: show wherever it keeps its notes.
    notesChanged = pyqtSignal(str)
    #: An ellipsoidal file implies a geoid separation. Offered rather than
    #: applied, because only the host knows whether it is free to change it.
    separationSuggested = pyqtSignal(float)

    def __init__(self, line: SectionLine, parent=None) -> None:
        super().__init__(parent)
        #: Read, never written. See the module docstring.
        self.line = line

        self.control_points: list[ControlPoint] = []
        self.fit: Fit | None = None
        self.photo_path: str | None = None
        self.image_size: tuple[int, int] = (0, 0)
        self._picking_row: int | None = None
        self._datum: HeightDatum = DEFAULT_WORKING_DATUM
        self._separation: float = 0.0

        self._build_ui()

    # -------------------------------------------------------------- building --

    def _build_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)

        row = QHBoxLayout()
        self.photo_edit = QLineEdit()
        self.photo_edit.setPlaceholderText("Section photo or orthophoto...")
        self.photo_edit.setReadOnly(True)
        browse = QPushButton("Load photo...")
        browse.clicked.connect(self._browse_photo)
        row.addWidget(QLabel("Photo"))
        row.addWidget(self.photo_edit, 1)
        row.addWidget(browse)
        layout.addLayout(row)

        splitter = QSplitter(Qt.Horizontal)

        # Left column, two clearly separated jobs so the two images are never
        # confused: the TOP box is the working photo you click control points
        # on; the BOTTOM box is a read-only preview of how that photo will end
        # up in the drawing. Titled, colour-accented group boxes keep "where I
        # click" and "what I'll get" distinct.
        left_split = QSplitter(Qt.Vertical)

        pick_group = QGroupBox(
            "Working photo  -  click your control points here"
        )
        pick_group.setStyleSheet(
            "QGroupBox { font-weight: bold; } "
            "QGroupBox::title { color: #1565c0; }"
        )
        pick_layout = QVBoxLayout(pick_group)
        pick_layout.setContentsMargins(6, 6, 6, 6)
        self.photo_view = PhotoView()
        self.photo_view.pointClicked.connect(self._on_photo_clicked)
        pick_layout.addWidget(self.photo_view)
        left_split.addWidget(pick_group)

        preview_group = QGroupBox(
            "Preview  -  how the photo will sit in the drawing (read-only)"
        )
        preview_group.setStyleSheet(
            "QGroupBox { font-weight: bold; } "
            "QGroupBox::title { color: #2e7d32; }"
        )
        preview_layout = QVBoxLayout(preview_group)
        preview_layout.setContentsMargins(6, 6, 6, 6)
        self.preview_caption = QLabel(
            "You do not draw here - this only shows the result. It updates as "
            "you add control points; you trace the units on the main map after "
            "Start drawing."
        )
        self.preview_caption.setWordWrap(True)
        self.preview_caption.setStyleSheet("color: grey; font-size: 10px;")
        preview_layout.addWidget(self.preview_caption)
        self.placed_preview = PlacedPreview()
        preview_layout.addWidget(self.placed_preview, 1)
        left_split.addWidget(preview_group)

        left_split.setStretchFactor(0, 3)
        left_split.setStretchFactor(1, 2)
        splitter.addWidget(left_split)

        right = QWidget()
        rlayout = QVBoxLayout(right)

        gcp_row = QHBoxLayout()
        load_gcp = QPushButton("Load control points...")
        load_gcp.clicked.connect(self._browse_gcps)
        gcp_row.addWidget(load_gcp)
        self.pick_button = QPushButton("Click points on the photo")
        self.pick_button.setToolTip(
            "Pick a point in the list, then click where it is in the photo."
        )
        self.pick_button.setCheckable(True)
        self.pick_button.toggled.connect(self._on_pick_toggled)
        gcp_row.addWidget(self.pick_button)
        clear = QPushButton("Undo this point")
        clear.setToolTip("Forget where the selected point was clicked.")
        clear.clicked.connect(self._clear_current_pick)
        gcp_row.addWidget(clear)
        rlayout.addLayout(gcp_row)

        #: Whatever the host needs between the pick buttons and the table: the
        #: datum controls when setting a section up, a read-only statement of
        #: them when re-linking one that is already drawn.
        self.extra_layout = QVBoxLayout()
        rlayout.addLayout(self.extra_layout)

        self.gcp_table = QTableWidget(0, 6)
        self.gcp_table.setHorizontalHeaderLabels(
            ["Use", "Name", "Height", "Off the wall", "Clicked at", "Error"]
        )
        self.gcp_table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.gcp_table.setSelectionMode(QAbstractItemView.SingleSelection)
        self.gcp_table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.gcp_table.verticalHeader().setVisible(False)
        self.gcp_table.itemSelectionChanged.connect(self._on_gcp_selection)
        self.gcp_table.itemChanged.connect(self._on_gcp_check_changed)
        rlayout.addWidget(self.gcp_table, 1)

        fit_row = QHBoxLayout()
        fit_row.addWidget(QLabel("How to place it"))
        self.model_combo = QComboBox()
        self.model_combo.addItem("Choose for me", None)
        for m in FitModel:
            self.model_combo.addItem(m.label, m)
        self.model_combo.currentIndexChanged.connect(self.refit)
        fit_row.addWidget(self.model_combo, 1)
        rlayout.addLayout(fit_row)

        self.fit_label = QLabel("No fit yet.")
        self.fit_label.setWordWrap(True)
        rlayout.addWidget(self.fit_label)

        splitter.addWidget(right)
        splitter.setStretchFactor(0, 3)
        splitter.setStretchFactor(1, 2)
        layout.addWidget(splitter, 1)

    # ------------------------------------------------------ host -> the widget --

    def set_datum(self, datum: HeightDatum, separation: float) -> None:
        """Say which datum to convert control points onto, and refit."""
        self._datum = datum
        self._separation = float(separation)
        self._refresh_gcp_table()
        self.refit()

    def set_control_points(self, points: list[ControlPoint]) -> None:
        """Fill the table from points restored with a saved section."""
        self.control_points = list(points)
        self._picking_row = None
        self._refresh_gcp_table()
        self.refit()

    def load_photo(self, path: str) -> None:
        """Open ``path`` in the picking view. Raises if it cannot be read."""
        self.image_size = self.photo_view.load(path)
        self.photo_path = path
        self.photo_edit.setText(path)
        self._redraw_markers()
        self._update_placement_preview()
        self.photoChanged.emit(path)

    def line_changed(self) -> None:
        """The host changed the trace: everything measured against it moves."""
        self._refresh_gcp_table()
        self.refit()

    # ---------------------------------------------------------------- photo --

    def _browse_photo(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self, "Section photo", self.photo_path or "", PHOTO_FILTER
        )
        if not path:
            return
        try:
            self.load_photo(path)
        except Exception as exc:
            QMessageBox.critical(self, "Could not open the photo", str(exc))
            return
        self.refit()

    def _browse_gcps(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self, "Control points", "", "CSV (*.csv);;All files (*)"
        )
        if not path:
            return
        try:
            points, notes = load_emlid_csv(path, transform_lonlat=lonlat_transformer())
        except Exception as exc:
            QMessageBox.critical(self, "Could not read the control points", str(exc))
            return

        on, off = select_for_section(points, self.line)
        self.control_points = on
        self._picking_row = None

        message = " ".join(notes)
        if off:
            message += (
                f" {len(off)} of {len(points)} points are more than "
                f"{self.line.buffer:.2f} m off this trace and were left out - "
                "a day's survey usually covers several walls."
            )
        self.notesChanged.emit(message)

        # Only an ellipsoidal file can measure the gap between the datums, and
        # the gap is worth having either way -- it carries the points across
        # when the drawing is orthometric, and the SU altitudes across when it
        # is ellipsoidal. Offered, not applied: a section that is already drawn
        # is stuck with the gap it was drawn on.
        if points and points[0].datum is HeightDatum.ELLIPSOIDAL:
            guess = suggest_separation(points)
            if guess:
                self.separationSuggested.emit(guess)

        self._refresh_gcp_table()
        self.refit()

    # -------------------------------------------------------- the point table --

    def _refresh_gcp_table(self) -> None:
        table = self.gcp_table
        table.blockSignals(True)
        table.setRowCount(len(self.control_points))
        sep = self._separation
        datum = self._datum
        for row, p in enumerate(self.control_points):
            check = QTableWidgetItem()
            check.setFlags(Qt.ItemIsUserCheckable | Qt.ItemIsEnabled | Qt.ItemIsSelectable)
            check.setCheckState(Qt.Checked if p.enabled else Qt.Unchecked)
            check.setData(Qt.UserRole, row)
            table.setItem(row, 0, check)
            table.setItem(row, 1, QTableWidgetItem(p.name))
            table.setItem(row, 2, QTableWidgetItem(f"{p.height_in(datum, sep):.3f}"))
            off = p.offset_from(self.line)
            off_item = QTableWidgetItem(f"{off*100:+.1f} cm")
            if abs(off) > self.line.buffer * 0.75:
                off_item.setForeground(WARN)
            table.setItem(row, 3, off_item)
            table.setItem(
                row, 4,
                QTableWidgetItem(
                    f"{p.pixel_x:.0f}, {p.pixel_y:.0f}" if p.is_picked
                    else "not yet"
                ),
            )
            res = QTableWidgetItem(f"{p.residual*1000:.1f} mm" if p.residual is not None else "")
            if p.residual is not None:
                res.setForeground(GOOD if p.residual < 0.02 else WARN if p.residual < 0.05 else BAD)
            table.setItem(row, 5, res)
        table.blockSignals(False)
        self._redraw_markers()

    def _on_gcp_check_changed(self, item) -> None:
        if item.column() != 0:
            return
        idx = item.data(Qt.UserRole)
        if idx is None or idx >= len(self.control_points):
            return
        self.control_points[idx].enabled = item.checkState() == Qt.Checked
        self.refit()

    def _on_gcp_selection(self) -> None:
        rows = self.gcp_table.selectionModel().selectedRows()
        self._picking_row = rows[0].row() if rows else None
        self._redraw_markers()
        if self._picking_row is not None:
            p = self.control_points[self._picking_row]
            if p.is_picked:
                self.photo_view.centre_on_pixel(p.pixel_x, p.pixel_y)

    def _on_pick_toggled(self, on: bool) -> None:
        self.photo_view.set_picking(on)

    def _on_photo_clicked(self, px: float, py: float) -> None:
        if self._picking_row is None:
            QMessageBox.information(
                self, "Pick a control point",
                "Select the control point in the table first, then click where "
                "it sits in the photo.",
            )
            return
        point = self.control_points[self._picking_row]
        point.pixel_x, point.pixel_y = px, py

        # Advance to the next unpicked row, so a run of points can be clicked
        # without going back to the table between each.
        nxt = next(
            (i for i in range(self._picking_row + 1, len(self.control_points))
             if not self.control_points[i].is_picked),
            None,
        )
        self._refresh_gcp_table()
        if nxt is not None:
            self.gcp_table.selectRow(nxt)
        self.refit()

    def _clear_current_pick(self) -> None:
        if self._picking_row is None:
            return
        p = self.control_points[self._picking_row]
        p.pixel_x = p.pixel_y = None
        p.residual = None
        self._refresh_gcp_table()
        self.refit()

    def _redraw_markers(self) -> None:
        if not self.photo_view.is_loaded:
            return
        self.photo_view.clear_markers()
        for i, p in enumerate(self.control_points):
            if p.is_picked:
                self.photo_view.set_marker(
                    p.name, p.pixel_x, p.pixel_y, highlight=(i == self._picking_row)
                )

    # ------------------------------------------------------------- the fit --

    def refit(self) -> None:
        """Recompute the placement and say so. Never touches the line.

        Every exit emits :attr:`fitChanged`, including the ones that could not
        make a fit, so a host that acts on a placement is told when there stops
        being one as well as when there starts being one.
        """
        picked = [p for p in self.control_points if p.enabled and p.is_picked]
        chosen = self.model_combo.currentData()
        if not self.photo_view.is_loaded or not picked:
            self._fail("No fit yet - load a photo and pick control points.", plain=True)
            return

        model = chosen or best_model_for(len(picked))
        if len(picked) < model.min_points:
            self._fail(
                f"{model.label} needs {model.min_points} points; {len(picked)} picked."
            )
            return

        try:
            self.fit = fit_transform(
                self.control_points, self.line,
                separation=self._separation,
                image_height=self.image_size[1],
                image_width=self.image_size[0],
                model=chosen,
                datum=self._datum,
            )
        except Exception as exc:
            self._fail(str(exc))
            return

        exact = len(picked) == model.min_points
        parts = [
            f"<b>{model.label}</b> on {len(picked)} points - "
            f"RMS <b>{self.fit.rms*1000:.1f} mm</b>",
            f"scale {self.fit.scale*1000:.4f} mm/px, rotation {self.fit.rotation_deg:+.3f} deg",
        ]
        if self.fit.worst:
            parts.append(f"worst: {self.fit.worst[0]} at {self.fit.worst[1]*1000:.1f} mm")
        if exact:
            parts.append(
                "<span style='color:#b06000'>The error reads zero only because "
                "there are just enough points. Click one more to get a real "
                "figure.</span>"
            )
        if self.fit.is_mirrored:
            parts.append(
                "<span style='color:#c03000'><b>The photo is back to front.</b> "
                f"{self.mirror_advice}</span>"
            )
        self.fit_label.setText("<br>".join(parts))
        self.fit_label.setStyleSheet("")

        self._refresh_gcp_table()
        self._update_placement_preview()
        self.fitChanged.emit(self.fit)

    #: What to tell someone whose photo comes out mirrored. The setup dialog has
    #: a button for it; a relink cannot offer one, because flipping the section
    #: would move every polygon already drawn on it.
    mirror_advice = "Use the red button at the top to fix it."

    def _fail(self, message: str, *, plain: bool = False) -> None:
        self.fit = None
        self.fit_label.setText(message)
        self.fit_label.setStyleSheet("" if plain else "color: #c03000;")
        self._update_placement_preview()
        self.fitChanged.emit(None)

    def _update_placement_preview(self) -> None:
        """Refresh the placement thumbnail to match the current fit.

        Called from every branch of ``refit`` and when a photo loads, so the
        preview is never stale: no fit yet shows the photo upright, and a fit
        shows it oriented exactly as it will be placed -- mirror included."""
        if not self.photo_view.is_loaded:
            self.placed_preview.clear()
            return
        self.placed_preview.set_placement(
            self.photo_view.display_pixmap,
            self.photo_view.preview_scale,
            self.image_size[1],
            self.fit,
        )


class RelinkPhotoDialog(QDialog):
    """Put a saved section's backdrop back by picking control points again.

    For sections drawn before the placement was recorded, where there is nothing
    to re-place the photograph with. Everything about the section itself is
    fixed and shown read-only: the trace, the flip, the datum and the gap are
    all baked into polygons that already exist, and the whole point of picking
    again is to move the *photo* onto the drawing, not the drawing onto the
    photo.
    """

    def __init__(self, session, initial_path: str = "", parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Re-link the section photo")
        self.resize(1180, 780)
        self.session = session

        layout = QVBoxLayout(self)

        heading = QLabel(
            f"<b>{session.line.name}</b> - pick control points on the photo to "
            "place it against the units that are already drawn."
        )
        heading.setWordWrap(True)
        layout.addWidget(heading)

        self.picker = GcpPickerWidget(session.line)
        # A relink cannot offer the flip: reversing the trace would move every
        # polygon already drawn against it.
        self.picker.mirror_advice = (
            "The control points place it back to front. Check they are on the "
            "right wall and clicked on the right features."
        )
        self.picker.preview_caption.setText(
            "You do not draw here - this only shows where the photo will land "
            "under the units you have already drawn."
        )

        datum = HeightDatum(getattr(session.line, "height_datum", None)
                            or DEFAULT_WORKING_DATUM.value)
        separation = float(getattr(session, "photo_separation", 0.0) or 0.0)
        fixed = QLabel(
            f"Heights are <b>{datum.value}</b>, gap <b>{separation:.3f} m</b> - "
            "fixed, because the units are already drawn against them."
        )
        fixed.setWordWrap(True)
        fixed.setStyleSheet("color: grey; font-size: 10px;")
        self.picker.extra_layout.addWidget(fixed)
        layout.addWidget(self.picker, 1)

        self.note = QLabel()
        self.note.setWordWrap(True)
        layout.addWidget(self.note)

        self.buttons = QDialogButtonBox(
            QDialogButtonBox.Ok | QDialogButtonBox.Cancel
        )
        self.buttons.button(QDialogButtonBox.Ok).setText("Place the photo")
        self.buttons.accepted.connect(self.accept)
        self.buttons.rejected.connect(self.reject)
        layout.addWidget(self.buttons)

        # Connected only now that everything a handler touches exists. PyQt
        # aborts the process on an unhandled exception inside a slot, so a fit
        # arriving mid-construction takes QGIS down rather than raising.
        self.picker.fitChanged.connect(lambda _fit: self._update_ok_state())
        self.picker.notesChanged.connect(self.note.setText)

        self.picker.set_datum(datum, separation)
        # Whatever was picked last time, so a section being re-linked to the
        # same photograph only needs the points confirmed, not re-clicked.
        points = list(getattr(session, "photo_points", []) or [])
        if points:
            self.picker.set_control_points(points)
        if initial_path:
            try:
                self.picker.load_photo(initial_path)
                self.picker.refit()
            except Exception:
                # Not worth a dialog: the user can still choose one by hand.
                pass
        self._update_ok_state()

    def _update_ok_state(self) -> None:
        self.buttons.button(QDialogButtonBox.Ok).setEnabled(
            self.picker.fit is not None and bool(self.picker.photo_path)
        )

    # The results, named the way the setup dialog names its own.
    def result_photo(self) -> str:
        return self.picker.photo_path or ""

    def result_fit(self) -> Fit | None:
        return self.picker.fit

    def result_points(self) -> list[ControlPoint]:
        return list(self.picker.control_points)
