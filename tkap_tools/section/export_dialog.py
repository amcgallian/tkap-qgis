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

from qgis.PyQt.QtCore import QSettings, QSize, Qt, QTimer, pyqtSignal
from qgis.PyQt.QtGui import QColor, QPixmap
from qgis.PyQt.QtWidgets import (
    QCheckBox,
    QColorDialog,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QDoubleSpinBox,
    QFormLayout,
    QFrame,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QRadioButton,
    QScrollArea,
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

#: Where the last-used export settings live. Keyed by output kind, because a
#: clean drawing and a wireframe are wanted at different sizes and colours and
#: sharing one set of remembered values would make each undo the other.
SETTINGS_PREFIX = "TKAP/section/export"


def _colour_to_text(colour: QColor) -> str:
    return "{},{},{},{}".format(
        colour.red(), colour.green(), colour.blue(), colour.alpha()
    )


def _colour_from_text(text: str, fallback: QColor) -> QColor:
    try:
        parts = [int(p) for p in str(text).split(",")]
    except (TypeError, ValueError):
        return fallback
    if len(parts) == 3:
        parts.append(255)
    if len(parts) != 4 or any(not 0 <= p <= 255 for p in parts):
        return fallback
    return QColor(*parts)


class _ColourButton(QPushButton):
    """A button showing its colour, opening a picker when pressed.

    QgsColorButton would do this, but it drags in the whole QGIS colour-scheme
    machinery -- recent colours, project palettes, drag and drop -- for what is
    two swatches on one dialog.
    """

    #: So the preview can redraw when a colour is picked. QPushButton has no
    #: such signal of its own, and `clicked` fires before the choice is made.
    colourChanged = pyqtSignal()

    def __init__(self, colour: QColor, parent=None) -> None:
        super().__init__(parent)
        self._colour = QColor(colour)
        self.setFlat(True)
        self.setMinimumHeight(24)
        self.setAutoFillBackground(True)
        self.clicked.connect(self._choose)
        self._refresh()

    def colour(self) -> QColor:
        return QColor(self._colour)

    def set_colour(self, colour: QColor) -> None:
        if QColor(colour) == self._colour:
            return
        self._colour = QColor(colour)
        self._refresh()
        self.colourChanged.emit()

    def _refresh(self) -> None:
        # A readable name on the swatch, in whichever of black or white stands
        # out against it, so the button says what it is as well as showing it.
        luma = (0.299 * self._colour.red() + 0.587 * self._colour.green()
                + 0.114 * self._colour.blue())
        ink = "#000000" if luma > 140 else "#ffffff"
        self.setStyleSheet(
            f"background-color: {self._colour.name()}; color: {ink}; "
            "border: 1px solid palette(mid);"
        )
        self.setText(self._colour.name())

    def _choose(self) -> None:
        chosen = QColorDialog.getColor(
            self._colour, self, "Choose a colour",
            QColorDialog.ShowAlphaChannel,
        )
        if chosen.isValid():
            self.set_colour(chosen)


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
        # Settings first, then the page handler: restoring a remembered page
        # size has to be in place before the handler reads it to work out the
        # scale and refresh the report.
        self.load_settings()
        # After the settings load, so restoring them does not queue a redraw
        # per control before the dialog is even on screen.
        self._connect_preview()
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
        layout.addWidget(self._build_labels_box())
        layout.addWidget(self._build_caption_box())
        if self.kind != DIGITIZED:
            layout.addWidget(self._build_wireframe_box())

        self.report = QLabel()
        self.report.setWordWrap(True)
        self.report.setTextFormat(Qt.RichText)
        self.report.setStyleSheet(
            "border: 1px solid palette(mid); padding: 8px;"
        )
        layout.addWidget(self.report)

        layout.addStretch(1)

        # The controls outgrew the window once labels, caption and colours were
        # added, so the column scrolls and the buttons sit outside it -- the
        # same arrangement, and the same reason, as the Survey Points dialog.
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.NoFrame)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        scroll.setWidget(left)
        scroll.setMinimumWidth(left.sizeHint().width() + 24)

        column = QWidget()
        column_layout = QVBoxLayout(column)
        column_layout.setContentsMargins(0, 0, 0, 0)
        column_layout.addWidget(scroll, 1)

        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.button(QDialogButtonBox.Ok).setText("Export...")
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        column_layout.addWidget(buttons)

        outer.addWidget(column, 0)
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

    def _connect_preview(self) -> None:
        """Make every control redraw the preview.

        One list rather than a connection next to each widget, because the
        controls added over time were each wired to whatever seemed relevant at
        the time and half of them ended up redrawing nothing -- the frame box,
        the graticule, all the label and caption controls, and every wireframe
        colour. The preview is meant to be the answer to "what will this do",
        so anything that changes the drawing belongs here.

        Resolution is deliberately absent: the preview renders at its own DPI,
        so changing the export DPI cannot alter it and a redraw would only look
        like something happened.
        """
        signals = [
            self.graticule_spin.valueChanged,
            self.frame_check.toggled,
            self.labels_check.toggled,
            self.label_size_spin.valueChanged,
            self.auto_caption.toggled,
            self.caption_edit.textChanged,
        ]
        if self.kind != DIGITIZED:
            signals += [
                self.outline_button.colourChanged,
                self.outline_width_spin.valueChanged,
                self.background_button.colourChanged,
                self.photo_fade_spin.valueChanged,
            ]
        for signal in signals:
            signal.connect(self._schedule_preview)

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

    def _build_labels_box(self):
        """Whether units are labelled at all, and how big.

        *What* each one says is chosen per unit in the panel, not here. A
        section usually wants numbers throughout with two or three units named
        and a thin lens left blank, which no single setting can express -- so
        the only whole-plate controls are the master switch and the type size.
        """
        box = QGroupBox("Unit labels")
        form = QFormLayout(box)

        self.labels_check = QCheckBox("Label the units")
        self.labels_check.setChecked(True)
        self.labels_check.setToolTip(
            "Off gives a clean unlabelled plate, for when the numbers are "
            "carried in a caption instead. What each unit says is set in the "
            "panel's Shows column."
        )
        self.labels_check.toggled.connect(self._on_labels_toggled)
        form.addRow("", self.labels_check)

        self.label_size_spin = QDoubleSpinBox()
        self.label_size_spin.setRange(3.0, 40.0)
        self.label_size_spin.setDecimals(1)
        self.label_size_spin.setSingleStep(0.5)
        self.label_size_spin.setSuffix(" pt")
        self.label_size_spin.setValue(8.0)
        form.addRow("Label size", self.label_size_spin)
        return box

    def _build_caption_box(self):
        box = QGroupBox("Caption")
        layout = QVBoxLayout(box)

        self.auto_caption = QCheckBox("Use the automatic caption")
        self.auto_caption.setChecked(True)
        self.auto_caption.setToolTip(
            "Which way the section is viewed, and the scale - e.g. "
            "\"Looking north · 1:20\"."
        )
        self.auto_caption.toggled.connect(self._on_caption_toggled)
        layout.addWidget(self.auto_caption)

        self.caption_edit = QLineEdit()
        self.caption_edit.setPlaceholderText(self._auto_caption_text())
        self.caption_edit.setEnabled(False)
        layout.addWidget(self.caption_edit)
        return box

    def _build_wireframe_box(self):
        """Colours for the over-photo drawing.

        Yellow on near-black suits most excavation photographs and disappears
        entirely over a pale sunlit wall, which is the whole reason these are
        not simply constants.
        """
        box = QGroupBox("Wireframe colours")
        form = QFormLayout(box)

        self.outline_button = _ColourButton(QColor(255, 255, 0))
        form.addRow("Outlines", self.outline_button)

        self.outline_width_spin = QDoubleSpinBox()
        self.outline_width_spin.setRange(0.1, 5.0)
        self.outline_width_spin.setDecimals(2)
        self.outline_width_spin.setSingleStep(0.1)
        self.outline_width_spin.setSuffix(" pt")
        self.outline_width_spin.setValue(0.6)
        form.addRow("Outline width", self.outline_width_spin)

        self.background_button = _ColourButton(QColor(20, 20, 20))
        self.background_button.setToolTip(
            "What sits behind the photo. Raise the fade below to let it "
            "through - a placed ortho fills the frame, so with the photo at "
            "full strength there is nothing behind it to see."
        )
        form.addRow("Backdrop", self.background_button)

        self.photo_fade_spin = QSpinBox()
        self.photo_fade_spin.setRange(0, 100)
        self.photo_fade_spin.setSingleStep(5)
        self.photo_fade_spin.setSuffix(" %")
        self.photo_fade_spin.setValue(0)
        self.photo_fade_spin.setToolTip(
            "Knocks the photo back towards the backdrop colour, so the "
            "outlines read over a bright wall. 0% leaves it untouched."
        )
        form.addRow("Fade the photo", self.photo_fade_spin)
        return box

    def _auto_caption_text(self) -> str:
        line = self.session.line
        return f"Looking {line.facing_name} · 1:{self._current_denominator():g}"

    def _on_labels_toggled(self, on: bool) -> None:
        self.label_size_spin.setEnabled(on)

    def _on_caption_toggled(self, on: bool) -> None:
        self.caption_edit.setEnabled(not on)
        self.caption_edit.setPlaceholderText(self._auto_caption_text())

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

    def _current_denominator(self) -> float:
        """The scale the drawing would come out at as things stand."""
        if self.fixed_radio.isChecked():
            fixed = self._fixed_denominator()
            if fixed:
                return fixed
        from .figure import fit_scale

        return fit_scale(self._width_m, self._height_m, self._base_spec())

    def _base_spec(self) -> FigureSpec:
        """Enough of a spec to work out the scale, without recursing into it."""
        return FigureSpec(
            title=self.title_edit.text(),
            kind=self.kind,
            page_width=self.width_spin.value(),
            page_height=self.height_spin.value(),
            margin=self.margin_spin.value(),
            snap_scale=self.snap_check.isChecked(),
            show_legend=self.legend_check.isChecked(),
            show_scalebar=self.scalebar_check.isChecked(),
        )

    def spec(self) -> FigureSpec:
        denom = None
        if self.fixed_radio.isChecked():
            denom = self._fixed_denominator()

        caption = None
        if not self.auto_caption.isChecked():
            # Deliberately allows "": an empty custom caption means no line at
            # all, which is different from None asking for the generated one.
            caption = self.caption_edit.text().strip()

        wireframe = self.kind != DIGITIZED
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
            show_labels=self.labels_check.isChecked(),
            label_size=self.label_size_spin.value(),
            caption=caption,
            outline_colour=(
                _colour_to_text(self.outline_button.colour()) if wireframe
                else FigureSpec.outline_colour
            ),
            outline_width=(
                self.outline_width_spin.value() if wireframe
                else FigureSpec.outline_width
            ),
            background_colour=(
                _colour_to_text(self.background_button.colour()) if wireframe
                else FigureSpec.background_colour
            ),
            photo_fade=(
                self.photo_fade_spin.value() / 100.0 if wireframe
                else FigureSpec.photo_fade
            ),
        )

    # ------------------------------------------------------------- settings --

    def _settings_key(self, name: str) -> str:
        return f"{SETTINGS_PREFIX}/{self.kind}/{name}"

    def save_settings(self) -> None:
        """Remember the controls, so the next export starts where this left off.

        Title and caption are deliberately not remembered: they name *this*
        section, and carrying them onto the next one would silently mislabel it.
        """
        settings = QSettings()
        values = {
            "page": self.page_combo.currentData(),
            "portrait": self.portrait.isChecked(),
            "auto_orient": self.auto_orient.isChecked(),
            "width": self.width_spin.value(),
            "height": self.height_spin.value(),
            "margin": self.margin_spin.value(),
            "fixed_scale": self.fixed_radio.isChecked(),
            "scale": self.scale_combo.currentText(),
            "snap": self.snap_check.isChecked(),
            "graticule": self.graticule_spin.value(),
            "legend": self.legend_check.isChecked(),
            "scalebar": self.scalebar_check.isChecked(),
            "frame": self.frame_check.isChecked(),
            "dpi": self.dpi_spin.value(),
            "labels": self.labels_check.isChecked(),
            "label_size": self.label_size_spin.value(),
        }
        if self.kind != DIGITIZED:
            values["outline_colour"] = _colour_to_text(self.outline_button.colour())
            values["outline_width"] = self.outline_width_spin.value()
            values["background_colour"] = _colour_to_text(
                self.background_button.colour()
            )
            values["photo_fade"] = self.photo_fade_spin.value()
        for name, value in values.items():
            settings.setValue(self._settings_key(name), value)

    def load_settings(self) -> None:
        """Restore the remembered controls. Anything missing keeps its default.

        Every read is guarded: a settings file written by a different version,
        or edited by hand, must not stop someone exporting a drawing.
        """
        settings = QSettings()

        def value(name, default, kind=None):
            got = settings.value(self._settings_key(name), default)
            if kind is bool:
                # QSettings hands booleans back as the strings "true"/"false"
                # on some platforms and as bool on others.
                if isinstance(got, str):
                    return got.lower() == "true"
                return bool(got)
            if kind in (int, float):
                try:
                    return kind(got)
                except (TypeError, ValueError):
                    return default
            return got

        page = value("page", None)
        if page is not None:
            index = self.page_combo.findData(page)
            if index >= 0:
                self.page_combo.setCurrentIndex(index)

        self.auto_orient.setChecked(value("auto_orient", self.auto_orient.isChecked(), bool))
        if value("portrait", self.portrait.isChecked(), bool):
            self.portrait.setChecked(True)
        else:
            self.landscape.setChecked(True)
        self.width_spin.setValue(value("width", self.width_spin.value(), float))
        self.height_spin.setValue(value("height", self.height_spin.value(), float))
        self.margin_spin.setValue(value("margin", self.margin_spin.value(), float))

        if value("fixed_scale", self.fixed_radio.isChecked(), bool):
            self.fixed_radio.setChecked(True)
        else:
            self.fit_radio.setChecked(True)
        remembered_scale = value("scale", None)
        if remembered_scale:
            index = self.scale_combo.findText(str(remembered_scale))
            if index >= 0:
                self.scale_combo.setCurrentIndex(index)
            elif self.scale_combo.isEditable():
                self.scale_combo.setEditText(str(remembered_scale))
        self.snap_check.setChecked(value("snap", self.snap_check.isChecked(), bool))

        self.graticule_spin.setValue(
            value("graticule", self.graticule_spin.value(), float))
        if self.legend_check.isEnabled():
            self.legend_check.setChecked(
                value("legend", self.legend_check.isChecked(), bool))
        self.scalebar_check.setChecked(
            value("scalebar", self.scalebar_check.isChecked(), bool))
        self.frame_check.setChecked(value("frame", self.frame_check.isChecked(), bool))
        self.dpi_spin.setValue(value("dpi", self.dpi_spin.value(), int))

        self.labels_check.setChecked(value("labels", self.labels_check.isChecked(), bool))
        self.label_size_spin.setValue(
            value("label_size", self.label_size_spin.value(), float))
        self._on_labels_toggled(self.labels_check.isChecked())

        if self.kind != DIGITIZED:
            self.outline_button.set_colour(_colour_from_text(
                value("outline_colour", ""), self.outline_button.colour()))
            self.outline_width_spin.setValue(
                value("outline_width", self.outline_width_spin.value(), float))
            self.background_button.set_colour(_colour_from_text(
                value("background_colour", ""), self.background_button.colour()))
            self.photo_fade_spin.setValue(
                value("photo_fade", self.photo_fade_spin.value(), int))

    def accept(self) -> None:
        self.save_settings()
        super().accept()

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
