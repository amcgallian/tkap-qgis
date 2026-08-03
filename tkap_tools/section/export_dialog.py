"""Export options, with a running report of how well the sheet is used.

The automatic fit is usually right, but "usually" is not good enough for a
figure that goes into a report: page size, orientation and margins all change
what scale the section can be drawn at, and only the person making the plate
knows whether 1:23 is acceptable or it has to be a round 1:25.

So every control shows its consequence immediately -- the chosen scale, the
drawing's size in millimetres, and how much of the available area it fills --
rather than making the user export, look, and come back.
"""

from __future__ import annotations

from qgis.PyQt.QtCore import QSize, Qt, QTimer
from qgis.PyQt.QtGui import QPixmap
from qgis.PyQt.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QDoubleSpinBox,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QRadioButton,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from .figure import (
    DIGITIZED,
    KIND_LABELS,
    PAGE_SIZES,
    PREFERRED_SCALES,
    FigureSpec,
    build_layout,
    fit_report,
    page_for_drawing,
)

#: Preview render resolution. High enough to judge composition, low enough to
#: redraw while a spin box is being dragged.
PREVIEW_DPI = 96

#: Sentinel for the page-size combo: size the sheet to the drawing.
FIT_TO_DRAWING = "__fit__"


class ExportDialog(QDialog):
    """Collects a :class:`FigureSpec` for one export."""

    def __init__(self, session, kind: str, default_title: str,
                 source_layer=None, parent=None) -> None:
        super().__init__(parent)
        self.session = session
        self.kind = kind
        self.source_layer = source_layer
        self.setWindowTitle(f"Export - {KIND_LABELS.get(kind, kind)}")
        self.resize(1020, 700)

        line = session.line
        self._width_m = line.drawing_width
        self._height_m = (line.z_max or 0.0) - (line.z_min or 0.0)

        self._preview_timer = QTimer(self)
        self._preview_timer.setSingleShot(True)
        self._preview_timer.timeout.connect(self._render_preview)

        self._build_ui(default_title)
        # Once, after every widget exists: the page handler reads the scale and
        # content controls, which are built after the page box.
        self._on_page_changed()

    # -------------------------------------------------------------- building --

    def _build_ui(self, default_title: str) -> None:
        outer = QHBoxLayout(self)

        # Controls on the left, the actual rendered page on the right. The
        # preview is the layout itself, not a mock-up, so anything that looks
        # wrong here will be wrong in the file.
        left = QWidget()
        layout = QVBoxLayout(left)
        layout.setContentsMargins(0, 0, 0, 0)

        heading = QLabel(f"<b>{KIND_LABELS.get(self.kind, self.kind)}</b>")
        layout.addWidget(heading)
        blurb = QLabel(
            "Filled units with a legend, over nothing." if self.kind == DIGITIZED
            else "Unit outlines and numbers over the rectified photograph."
        )
        blurb.setStyleSheet("color: grey;")
        blurb.setWordWrap(True)
        layout.addWidget(blurb)

        form = QFormLayout()
        self.title_edit = QLineEdit(default_title)
        self.title_edit.textChanged.connect(self._schedule_preview)
        form.addRow("Title", self.title_edit)
        layout.addLayout(form)

        layout.addWidget(self._build_page_box())
        layout.addWidget(self._build_scale_box())
        layout.addWidget(self._build_content_box())

        self.report = QLabel()
        self.report.setWordWrap(True)
        self.report.setTextFormat(Qt.RichText)
        self.report.setStyleSheet(
            "border: 1px solid palette(mid); padding: 8px;"
        )
        layout.addWidget(self.report)

        layout.addStretch(1)
        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.button(QDialogButtonBox.Ok).setText("Export...")
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

        outer.addWidget(left, 0)
        outer.addWidget(self._build_preview_box(), 1)

    def _build_preview_box(self) -> QWidget:
        box = QGroupBox("Preview")
        col = QVBoxLayout(box)

        self.preview_label = QLabel("Rendering...")
        self.preview_label.setAlignment(Qt.AlignCenter)
        self.preview_label.setMinimumSize(420, 340)
        self.preview_label.setStyleSheet(
            "background: palette(mid); border: 1px solid palette(dark);"
        )
        col.addWidget(self.preview_label, 1)

        self.preview_note = QLabel()
        self.preview_note.setStyleSheet("color: grey; font-size: 10px;")
        self.preview_note.setWordWrap(True)
        col.addWidget(self.preview_note)
        return box

    # ---------------------------------------------------------------- preview --

    def _schedule_preview(self, *_args) -> None:
        """Coalesce rapid changes into one render.

        Every spin box and checkbox asks for a redraw, and rendering the layout
        is not free, so dragging a margin spinner would otherwise queue dozens
        of full renders.
        """
        self._preview_timer.start(180)

    def _render_preview(self) -> None:
        from qgis.core import QgsLayoutExporter, QgsProject

        spec = self.spec()
        layout = None
        try:
            layout = build_layout(self.session, spec, self.source_layer)
            exporter = QgsLayoutExporter(layout)
            # Modest DPI: this is for judging composition, not detail, and it
            # has to keep up with a spin box.
            image = exporter.renderPageToImage(
                0, QSize(), PREVIEW_DPI
            )
        except Exception as exc:
            self.preview_label.setText(f"Preview failed:\n{exc}")
            self.preview_note.setText("")
            return
        finally:
            if layout is not None:
                QgsProject.instance().layoutManager().removeLayout(layout)

        if image is None or image.isNull():
            self.preview_label.setText("Preview unavailable")
            return

        scaled = QPixmap.fromImage(image).scaled(
            self.preview_label.size(), Qt.KeepAspectRatio, Qt.SmoothTransformation
        )
        self.preview_label.setPixmap(scaled)
        self.preview_note.setText(
            f"{spec.page_width:.0f} x {spec.page_height:.0f} mm at "
            f"{spec.dpi} dpi - about "
            f"{int(spec.page_width / 25.4 * spec.dpi)} x "
            f"{int(spec.page_height / 25.4 * spec.dpi)} px when exported."
        )

    def resizeEvent(self, event):      # noqa: N802 -- Qt naming
        super().resizeEvent(event)
        self._schedule_preview()

    def _build_page_box(self):
        box = QGroupBox("Page")
        form = QFormLayout(box)

        self.page_combo = QComboBox()
        # Default. A section is long and shallow and these figures go into
        # reports, so sizing the sheet to the drawing wastes nothing, whereas
        # forcing a 2.5:1 section onto A4 leaves a third of it blank.
        self.page_combo.addItem("Fit page to drawing", FIT_TO_DRAWING)
        for name in PAGE_SIZES:
            self.page_combo.addItem(name, name)
        self.page_combo.addItem("Custom", None)
        self.page_combo.currentIndexChanged.connect(self._on_page_changed)
        form.addRow("Size", self.page_combo)

        orient = QHBoxLayout()
        self.landscape = QRadioButton("Landscape")
        self.landscape.setChecked(True)
        self.landscape.toggled.connect(self._on_page_changed)
        self.portrait = QRadioButton("Portrait")
        orient.addWidget(self.landscape)
        orient.addWidget(self.portrait)
        form.addRow("Orientation", orient)

        self.auto_orient = QCheckBox("Choose the better one for me")
        self.auto_orient.setChecked(True)
        self.auto_orient.setToolTip(
            "Matches the page to the shape of the section."
        )
        self.auto_orient.toggled.connect(self._on_page_changed)
        form.addRow("", self.auto_orient)

        size_row = QHBoxLayout()
        self.width_spin = QDoubleSpinBox()
        self.width_spin.setRange(50.0, 2000.0)
        self.width_spin.setSuffix(" mm")
        self.width_spin.setValue(297.0)
        self.width_spin.valueChanged.connect(self._update_report)
        self.height_spin = QDoubleSpinBox()
        self.height_spin.setRange(50.0, 2000.0)
        self.height_spin.setSuffix(" mm")
        self.height_spin.setValue(210.0)
        self.height_spin.valueChanged.connect(self._update_report)
        size_row.addWidget(self.width_spin)
        size_row.addWidget(QLabel("x"))
        size_row.addWidget(self.height_spin)
        form.addRow("Dimensions", size_row)

        self.margin_spin = QDoubleSpinBox()
        self.margin_spin.setRange(0.0, 60.0)
        self.margin_spin.setSuffix(" mm")
        self.margin_spin.setValue(15.0)
        self.margin_spin.valueChanged.connect(self._update_report)
        form.addRow("Margin", self.margin_spin)
        return box

    def _build_scale_box(self):
        box = QGroupBox("Scale")
        form = QFormLayout(box)

        self.fit_radio = QRadioButton("As big as fits the page")
        self.fit_radio.setChecked(True)
        self.fit_radio.toggled.connect(self._update_report)
        form.addRow(self.fit_radio)

        self.snap_check = QCheckBox("Use a round scale")
        self.snap_check.setChecked(True)
        self.snap_check.setToolTip(
            "On: the nearest round scale that fits, so the print can be "
            "measured.\nOff: fills the page exactly, at any scale."
        )
        self.snap_check.toggled.connect(self._update_report)
        form.addRow("", self.snap_check)

        self.fixed_radio = QRadioButton("Fixed scale")
        self.fixed_radio.toggled.connect(self._update_report)
        self.scale_combo = QComboBox()
        self.scale_combo.setEditable(True)
        for denom in PREFERRED_SCALES:
            text = f"1:{denom:g}"
            self.scale_combo.addItem(text, float(denom))
        self.scale_combo.setCurrentText("1:20")
        self.scale_combo.currentTextChanged.connect(self._update_report)
        form.addRow(self.fixed_radio, self.scale_combo)
        return box

    def _build_content_box(self):
        box = QGroupBox("Content")
        form = QFormLayout(box)

        self.graticule_spin = QDoubleSpinBox()
        self.graticule_spin.setRange(0.01, 5.0)
        self.graticule_spin.setDecimals(2)
        self.graticule_spin.setSingleStep(0.05)
        self.graticule_spin.setSuffix(" m")
        self.graticule_spin.setValue(0.25)
        self.graticule_spin.setToolTip(
            "Spacing of the horizontal height lines down the left-hand side."
        )
        form.addRow("Height lines every", self.graticule_spin)

        self.legend_check = QCheckBox("Legend")
        self.legend_check.setChecked(self.kind == DIGITIZED)
        self.legend_check.setEnabled(self.kind == DIGITIZED)
        self.legend_check.toggled.connect(self._update_report)
        form.addRow("", self.legend_check)

        self.scalebar_check = QCheckBox("Scale bar")
        self.scalebar_check.setToolTip(
            "A 3 m bar, with the first metre split into quarters."
        )
        self.scalebar_check.setChecked(True)
        self.scalebar_check.toggled.connect(self._update_report)
        form.addRow("", self.scalebar_check)

        self.frame_check = QCheckBox("Box around the drawing")
        self.frame_check.setChecked(True)
        form.addRow("", self.frame_check)

        self.dpi_spin = QSpinBox()
        self.dpi_spin.setRange(72, 1200)
        self.dpi_spin.setSingleStep(50)
        self.dpi_spin.setValue(300)
        form.addRow("Resolution", self.dpi_spin)
        return box

    # ------------------------------------------------------------- behaviour --

    def _on_page_changed(self, *_args) -> None:
        name = self.page_combo.currentData()
        fitting = name == FIT_TO_DRAWING
        custom = name is None

        self.width_spin.setEnabled(custom)
        self.height_spin.setEnabled(custom)
        orient_relevant = not fitting and not self.auto_orient.isChecked()
        self.landscape.setEnabled(orient_relevant)
        self.portrait.setEnabled(orient_relevant)
        self.auto_orient.setEnabled(not fitting and not custom)

        if fitting:
            # The page follows the drawing, so the scale has to be decided
            # first -- there is no page for a fit-to-page scale to fill.
            if self.fit_radio.isChecked():
                self.fixed_radio.setChecked(True)
            self.fit_radio.setEnabled(False)
            self._resize_page_to_drawing()
        else:
            self.fit_radio.setEnabled(True)
            if not custom:
                w, h = PAGE_SIZES[name]
                if self._want_landscape():
                    w, h = h, w
                self._set_page(w, h)
        self._update_report()

    def _set_page(self, width: float, height: float) -> None:
        for spin, value in ((self.width_spin, width), (self.height_spin, height)):
            spin.blockSignals(True)
            spin.setValue(value)
            spin.blockSignals(False)

    def _resize_page_to_drawing(self) -> None:
        denom = self._fixed_denominator()
        if denom is None:
            return
        spec = self.spec()
        w, h = page_for_drawing(self._width_m, self._height_m, denom, spec)
        self._set_page(w, h)

    def _want_landscape(self) -> bool:
        if self.auto_orient.isChecked():
            # Match the sheet to the section: wider than tall goes landscape.
            return self._width_m >= self._height_m
        return self.landscape.isChecked()

    def _fixed_denominator(self) -> float | None:
        """Read the denominator from "1:25", "25", or anything in between.

        Split on the colon rather than stripping it: ``"1:10".lstrip("1:")``
        removes *characters*, not a prefix, and eats the leading 1 of the
        denominator too, leaving "0".
        """
        text = self.scale_combo.currentText().strip()
        if ":" in text:
            text = text.split(":")[-1]
        try:
            value = float(text.strip())
        except ValueError:
            return None
        return value if value > 0 else None

    def spec(self) -> FigureSpec:
        denom = None
        if self.fixed_radio.isChecked():
            denom = self._fixed_denominator()
        return FigureSpec(
            title=self.title_edit.text(),
            graticule=self.graticule_spin.value(),
            kind=self.kind,
            page_width=self.width_spin.value(),
            page_height=self.height_spin.value(),
            margin=self.margin_spin.value(),
            scale_denominator=denom,
            snap_scale=self.snap_check.isChecked(),
            show_legend=self.legend_check.isChecked(),
            show_scalebar=self.scalebar_check.isChecked(),
            show_frame=self.frame_check.isChecked(),
            dpi=self.dpi_spin.value(),
        )

    def _update_report(self, *_args) -> None:
        self.snap_check.setEnabled(
            self.fit_radio.isChecked() and self.fit_radio.isEnabled()
        )
        self.scale_combo.setEnabled(self.fixed_radio.isChecked())

        # In fit-to-drawing mode the page follows every other control, so it has
        # to be recomputed before the report is drawn. _set_page blocks signals,
        # so this cannot loop.
        if self.page_combo.currentData() == FIT_TO_DRAWING:
            self._resize_page_to_drawing()

        spec = self.spec()
        info = fit_report(self._width_m, self._height_m, spec)
        draw_w, draw_h = info["drawing_mm"]

        # Only the one thing that needs acting on. How well the page is filled
        # is visible in the preview alongside, so a percentage just added noise.
        warning = (
            "<br><span style='color:#c03000'><b>Too big for the page.</b> "
            "Use a smaller scale or a bigger page.</span>"
            if info["overflows"] else ""
        )
        self.report.setText(
            f"Section is <b>{self._width_m:.2f} x {self._height_m:.2f} m</b>, "
            f"printed at <b>1:{info['denominator']:.4g}</b> as "
            f"<b>{draw_w:.0f} x {draw_h:.0f} mm</b>{warning}"
        )
        self._schedule_preview()
