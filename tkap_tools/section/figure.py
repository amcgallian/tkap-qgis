"""The two drawing outputs, as real QGIS print layouts.

A print layout rather than hand-rolled SVG, because the elevation axis is the
one piece of cartography that comes free: y *is* elevation in section space, so
a map grid with a y interval and left annotations is literally an elevation
scale. Legends, scale bars and atlas export come along with it.

Two products, from one builder:

* **clean** -- filled, labelled SU polygons over nothing, with a legend
* **wireframe** -- the same outlines over the rectified photo

They share a title, a scale and an elevation axis so the two can be laid
side by side and read as the same drawing.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from qgis.core import (
    QgsFillSymbol,
    QgsLayoutExporter,
    QgsLayoutItemLabel,
    QgsLayoutItemLegend,
    QgsLayoutItemMap,
    QgsLayoutItemMapGrid,
    QgsLayoutItemScaleBar,
    QgsLayoutMeasurement,
    QgsLayoutPoint,
    QgsLayoutSize,
    QgsLayoutItemPage,
    QgsLineSymbol,
    QgsPrintLayout,
    QgsProject,
    QgsReadWriteContext,
    QgsRenderContext,
    QgsSingleSymbolRenderer,
    QgsTextFormat,
    QgsUnitTypes,
    QgsVectorLayerSimpleLabeling,
)
from qgis.PyQt.QtCore import QRectF
from qgis.PyQt.QtGui import QColor, QFont
from qgis.PyQt.QtXml import QDomDocument

from .session import section_label_settings

#: Scales an archaeologist would accept on a section drawing, finest first.
#: Finer-grained than the usual 10/20/50 ladder because jumping straight from
#: 1:10 to 1:20 halves the drawing and leaves half the sheet empty.
PREFERRED_SCALES = (5, 10, 12.5, 15, 20, 25, 30, 40, 50, 75, 100, 150, 200)

PAGE_MARGIN_MM = 15.0
TITLE_HEIGHT_MM = 16.0
LEGEND_WIDTH_MM = 45.0
SCALEBAR_HEIGHT_MM = 12.0

#: Named page sizes, width x height in mm, portrait.
PAGE_SIZES = {
    "A5": (148.0, 210.0),
    "A4": (210.0, 297.0),
    "A3": (297.0, 420.0),
    "A2": (420.0, 594.0),
    "A1": (594.0, 841.0),
}

#: Scale bar: 3 m overall, in the classic split form. The leftmost metre is
#: subdivided into quarters so 0.25 m and 0.50 m can be read directly; the rest
#: runs in whole metres. Twelve quarter-metre segments across the full 3 m would
#: be unreadable, and whole metres alone would lose the fine measure.
SCALEBAR_SEGMENT_M = 1.0
SCALEBAR_SEGMENTS_RIGHT = 2        # whole metres to the right of zero
SCALEBAR_SUBDIVISIONS_LEFT = 4     # the left metre, quartered
SCALEBAR_TOTAL_M = SCALEBAR_SEGMENT_M * (1 + SCALEBAR_SEGMENTS_RIGHT)


def _colour(spec_value: str, fallback: QColor) -> QColor:
    """Parse an 'r,g,b' or 'r,g,b,a' string into a QColor, or fall back.

    Colours travel through FigureSpec as strings because that is the form
    QgsFillSymbol.createSimple wants and the form settings persist as; a bad
    one should soften the drawing, not stop the export.
    """
    try:
        parts = [int(p) for p in str(spec_value).split(",")]
    except (TypeError, ValueError):
        return fallback
    if len(parts) == 3:
        parts.append(255)
    if len(parts) != 4 or any(not 0 <= p <= 255 for p in parts):
        return fallback
    return QColor(*parts)


def scalebar_layout(section_length_m: float) -> tuple[float, int, int]:
    """(metres per segment, whole segments right, subdivisions of the left one).

    Three metres by default. A bar longer than the section it sits under would
    run off the sheet, so on a short wall it steps down -- always keeping the
    quarter-metre subdivisions, which are the point of the thing.
    """
    for total, right in ((3.0, 2), (2.0, 1), (1.0, 0)):
        if section_length_m >= total:
            return (1.0, right, 4)
    # Under a metre: a half-metre bar still divided into quarters.
    return (0.5, 0, 2)


#: Internal keys for the two outputs, and what they are called on screen.
DIGITIZED = "digitized"
WIREFRAME = "wireframe"

KIND_LABELS = {
    DIGITIZED: "Digitized drawing",
    WIREFRAME: "Wireframe drawing",
}


@dataclass
class FigureSpec:
    title: str
    graticule: float = 0.25
    kind: str = DIGITIZED
    page_width: float = 297.0           # A4 landscape by default
    page_height: float = 210.0
    margin: float = PAGE_MARGIN_MM
    #: None means "work out the scale that fills the page".
    scale_denominator: float | None = None
    #: Round a fitted scale up to the next standard one. Off means the drawing
    #: fills the sheet exactly at whatever odd scale that takes -- fine on
    #: screen, less good if someone wants to measure off the print.
    snap_scale: bool = True
    show_legend: bool = True
    show_scalebar: bool = True
    show_frame: bool = True
    dpi: int = 300

    #: Labels. *What* each unit says is chosen per unit, in the panel; the
    #: figure only decides whether they are drawn at all and how big.
    show_labels: bool = True
    label_size: float = 8.0

    #: The wireframe's colours. Yellow on near-black suits most wall photos and
    #: is unreadable over a pale sunlit one, which is why they are settable.
    outline_colour: str = "255,255,0,255"
    outline_width: float = 0.6
    background_colour: str = "20,20,20"

    #: The line under the title. None means the generated one (facing and
    #: scale); anything else is used verbatim, and "" leaves it off.
    caption: str | None = None

    @property
    def kind_label(self) -> str:
        return KIND_LABELS.get(self.kind, self.kind)

    def available_area(self) -> tuple[float, float]:
        """Millimetres left for the drawing once furniture is subtracted."""
        width = self.page_width - 2 * self.margin
        if self.show_legend and self.kind == DIGITIZED:
            width -= LEGEND_WIDTH_MM
        height = self.page_height - 2 * self.margin - TITLE_HEIGHT_MM
        if self.show_scalebar:
            height -= SCALEBAR_HEIGHT_MM
        return (max(width, 10.0), max(height, 10.0))


def fit_scale(width_m: float, height_m: float, spec: FigureSpec) -> float:
    """The scale that makes the section fill the page.

    Returns a denominator. The exact fit is whichever axis runs out of room
    first; snapping then rounds *up* to the next standard scale, because
    rounding down would push the drawing off the sheet.
    """
    avail_w, avail_h = spec.available_area()
    if width_m <= 0 or height_m <= 0:
        return PREFERRED_SCALES[1]

    # 1 m at 1:d is 1000/d mm on paper, so d = 1000 * m / mm.
    exact = max(1000.0 * width_m / avail_w, 1000.0 * height_m / avail_h)
    if not spec.snap_scale:
        return exact
    for denom in PREFERRED_SCALES:
        if denom >= exact:
            return float(denom)
    return float(PREFERRED_SCALES[-1])


def drawing_size_mm(width_m: float, height_m: float, denom: float) -> tuple[float, float]:
    """How big the section comes out on paper at this scale."""
    mm_per_m = 1000.0 / denom
    return (width_m * mm_per_m, height_m * mm_per_m)


def fit_report(width_m: float, height_m: float, spec: FigureSpec) -> dict:
    """Everything the export dialog needs to show how well the page is used."""
    denom = spec.scale_denominator or fit_scale(width_m, height_m, spec)
    draw_w, draw_h = drawing_size_mm(width_m, height_m, denom)
    avail_w, avail_h = spec.available_area()
    used = (draw_w * draw_h) / (avail_w * avail_h) if avail_w and avail_h else 0.0
    return {
        "denominator": denom,
        "drawing_mm": (draw_w, draw_h),
        "available_mm": (avail_w, avail_h),
        "fill": max(0.0, min(used, 1.0)),
        "overflows": draw_w > avail_w + 0.5 or draw_h > avail_h + 0.5,
    }


def page_for_drawing(
    width_m: float, height_m: float, denom: float, spec: FigureSpec
) -> tuple[float, float]:
    """Page size that exactly contains this drawing plus its furniture.

    A section is long and shallow -- the F1 Space 70 south wall is 2.56:1 --
    while A4's drawing area is about 1.46:1. Fitting one to the other leaves a
    third of the sheet blank no matter how well the scale is chosen. Since these
    figures are dropped into reports rather than printed as standalone plates,
    it is the sheet that should give way, not the drawing.
    """
    draw_w, draw_h = drawing_size_mm(width_m, height_m, denom)
    page_w = draw_w + 2 * spec.margin
    if spec.show_legend and spec.kind == DIGITIZED:
        page_w += LEGEND_WIDTH_MM
    page_h = draw_h + 2 * spec.margin + TITLE_HEIGHT_MM
    if spec.show_scalebar:
        page_h += SCALEBAR_HEIGHT_MM
    return (round(page_w, 1), round(page_h, 1))


def choose_scale(width_m: float, height_m: float, spec: FigureSpec) -> float:
    """Backwards-compatible alias for :func:`fit_scale`."""
    return fit_scale(width_m, height_m, spec)


def _labelling_for(spec: FigureSpec, kind: str):
    """The labelling a figure should carry, or None when labels are off."""
    if not spec.show_labels:
        return None
    return _print_labelling(kind, spec.label_size)


def _outline_only_style(layer, spec: FigureSpec | None = None) -> str:
    """QML for the wireframe pass: outlines and labels, no fill.

    Returned as a style-override string so the layer itself is never restyled --
    the user's editing view keeps whatever they were looking at.
    """
    spec = spec or FigureSpec(title="", kind=WIREFRAME)
    original = layer.renderer().clone()
    original_opacity = layer.opacity()
    original_labeling = layer.labeling().clone() if layer.labeling() else None
    original_labels_enabled = layer.labelsEnabled()
    # Set explicitly rather than relying on whatever the layer happens to carry,
    # so the wireframe comes out labelled exactly as asked for.
    labelling = _labelling_for(spec, WIREFRAME)
    if labelling is None:
        layer.setLabelsEnabled(False)
    else:
        layer.setLabeling(labelling)
        layer.setLabelsEnabled(True)

    symbol = QgsFillSymbol.createSimple({
        "color": "0,0,0,0",
        "outline_color": spec.outline_colour,
        "outline_width": str(spec.outline_width),
        "outline_width_unit": "Point",
    })
    # A whole new renderer, not a symbol swap: the layer may be carrying a
    # categorised renderer, which has no single symbol to set.
    layer.setRenderer(QgsSingleSymbolRenderer(symbol))
    # The outlines have to be crisp over the photo; the see-through opacity used
    # while drawing would wash them out.
    layer.setOpacity(1.0)
    doc = QDomDocument()
    layer.exportNamedStyle(doc, QgsReadWriteContext())
    layer.setRenderer(original)
    layer.setOpacity(original_opacity)
    if original_labeling is not None:
        layer.setLabeling(original_labeling)
    layer.setLabelsEnabled(original_labels_enabled)
    return doc.toString()


#: Source field name -> the field the section layer carries it in. The section
#: layer copies these verbatim, so most of the time no remapping is needed at
#: all -- this only catches sources that spell them differently, such as the
#: file GDB's Type_3_Fir.
_ATTRIBUTE_ALIASES = {
    "subsubtype": "subsubtype",
    "sub_sub_type": "subsubtype",
    "type_4": "subsubtype",
    "type_3_fir": "subsubtype",
    "subtype": "subtype",
    "sub_type": "subtype",
    "type_3": "subtype",
    "type": "su_type",
    "type_2": "su_type",
    "su_type": "su_type",
}


def adopt_source_renderer(layer, source_layer) -> bool:
    """Put the SU layer's own symbology on the section layer. True if it took.

    The project styles SUs on ``subsubtype``, and the section layer carries that
    field, so a categorised renderer usually transfers untouched. Two fallbacks
    handle the rest: remapping a differently-spelled class attribute, and
    accepting any renderer (rule-based included) whose referenced fields all
    exist on the section layer.
    """
    if source_layer is None or source_layer.renderer() is None:
        return False

    try:
        cloned = source_layer.renderer().clone()
    except Exception:
        return False

    available = {n.lower() for n in layer.fields().names()}

    # Categorised or graduated: remap the class attribute if we need to.
    if hasattr(cloned, "classAttribute") and hasattr(cloned, "setClassAttribute"):
        # QGIS returns the class attribute exactly as stored, which is often
        # quoted ("subsubtype") and may be a whole expression. Strip the quoting
        # before trying to match, or an ordinary categorised layer looks
        # unrecognisable and silently falls through to the plain fill.
        source_attr = (cloned.classAttribute() or "").strip()
        bare = source_attr.strip('"').strip("'").strip().lower()

        target = _ATTRIBUTE_ALIASES.get(bare)
        if target is None and bare in available:
            target = bare
        if target and target.lower() in available:
            try:
                cloned.setClassAttribute(target)
                layer.setRenderer(cloned)
                return True
            except Exception:
                return False
        # Not a bare field name -- an expression, most likely. If everything it
        # reads exists here under the same name, it can be adopted untouched.
        if _reads_only_available(cloned, available):
            try:
                layer.setRenderer(cloned)
                return True
            except Exception:
                return False
        # Classified on something the section layer does not carry (square,
        # phase, an expression over geometry...). Better a plain fill than a
        # renderer that puts everything in the "no value" bucket.
        return False

    # Rule-based and friends: safe to adopt only if every field it reads exists.
    if not _reads_only_available(cloned, available):
        return False
    try:
        layer.setRenderer(cloned)
        return True
    except Exception:
        return False


def _reads_only_available(renderer, available: set[str]) -> bool:
    """True when every attribute the renderer needs exists on the target."""
    try:
        used = {a.lower() for a in renderer.usedAttributes(QgsRenderContext())}
    except Exception:
        return False
    return bool(used) and used.issubset(available)


def _frame_style(layer, kind: str) -> str:
    """QML for the section limits on paper.

    The on-screen frame is cyan and dashed so it stands out against a photo
    while drawing. On a finished plate that would read as a feature of the
    section, so it becomes a plain hairline -- dark on the clean drawing, pale
    over the photo.
    """
    original = layer.renderer().clone()
    layer.setRenderer(QgsSingleSymbolRenderer(QgsFillSymbol.createSimple({
        "color": "0,0,0,0",
        "outline_color": "255,255,255,220" if kind == "wireframe" else "30,30,30,255",
        "outline_width": "0.3",
        "outline_width_unit": "MM",
        "outline_style": "solid",
    })))
    doc = QDomDocument()
    layer.exportNamedStyle(doc, QgsReadWriteContext())
    layer.setRenderer(original)
    return doc.toString()


def _print_labelling(kind: str, size: float = 8.0):
    """SU numbers for paper, in whichever polarity reads on that background.

    Both outputs are labelled, and both use ``su_number``. The digitized drawing
    has pale fills, so the numbers go dark on a white halo; the wireframe sits
    on a photograph, so they stay white on a dark halo. Placement is identical
    either way, and both are built from the same on-screen settings so a change
    to placement cannot apply to one output and not the other.
    """
    settings = section_label_settings()
    text = settings.format()
    text.setSize(size)
    buf = text.buffer()
    buf.setEnabled(True)
    if kind == WIREFRAME:
        text.setColor(QColor(255, 255, 255))
        buf.setSize(1.0)
        buf.setColor(QColor(0, 0, 0, 220))
    else:
        text.setColor(QColor(20, 20, 20))
        buf.setSize(0.8)
        buf.setColor(QColor(255, 255, 255, 230))
    text.setBuffer(buf)
    settings.setFormat(text)
    return QgsVectorLayerSimpleLabeling(settings)


def _filled_style(layer, source_layer=None, spec: FigureSpec | None = None) -> str:
    """QML for the clean pass.

    Cloning the SU layer's symbology means the section drawing comes out
    matching the plan drawings the project already produces, and the legend
    names the same categories.
    """
    spec = spec or FigureSpec(title="", kind=DIGITIZED)
    original = layer.renderer().clone()
    original_labeling = layer.labeling().clone() if layer.labeling() else None
    original_opacity = layer.opacity()
    original_labels_enabled = layer.labelsEnabled()
    labelling = _labelling_for(spec, DIGITIZED)
    if labelling is None:
        layer.setLabelsEnabled(False)
    else:
        layer.setLabeling(labelling)
        layer.setLabelsEnabled(True)
    # The drawing layer is held semi-transparent so the photo shows through
    # while tracing. A finished clean drawing has no photo, so it goes solid.
    layer.setOpacity(1.0)

    if not adopt_source_renderer(layer, source_layer):
        layer.setRenderer(QgsSingleSymbolRenderer(QgsFillSymbol.createSimple({
            "color": "220,215,200,255",
            "outline_color": "40,40,40,255",
            "outline_width": "0.35",
            "outline_width_unit": "Point",
        })))

    doc = QDomDocument()
    layer.exportNamedStyle(doc, QgsReadWriteContext())
    layer.setRenderer(original)
    layer.setOpacity(original_opacity)
    if original_labeling is not None:
        layer.setLabeling(original_labeling)
    layer.setLabelsEnabled(original_labels_enabled)
    return doc.toString()


def build_layout(session, spec: FigureSpec, source_layer=None) -> QgsPrintLayout:
    """Assemble the layout. Caller owns it and should remove it when done."""
    project = QgsProject.instance()
    layout = QgsPrintLayout(project)
    layout.initializeDefaults()
    layout.setName(f"{spec.title} ({spec.kind})")

    page = layout.pageCollection().page(0)
    page.setPageSize(QgsLayoutSize(spec.page_width, spec.page_height,
                                   QgsUnitTypes.LayoutMillimeters))

    line = session.line
    # The drawing surface, which takes in the photo, the control points and any
    # SU that runs past the end of the trace -- not just the trace itself.
    width_m = line.drawing_width
    height_m = (line.z_max or 0) - (line.z_min or 0)
    denom = spec.scale_denominator or fit_scale(width_m, height_m, spec)
    draw_w, draw_h = drawing_size_mm(width_m, height_m, denom)

    # --- title -------------------------------------------------------------
    title = QgsLayoutItemLabel(layout)
    title.setText(spec.title)
    font = QFont()
    font.setPointSize(16)
    font.setBold(True)
    title.setFont(font)
    title.attemptMove(QgsLayoutPoint(spec.margin, spec.margin * 0.5,
                                     QgsUnitTypes.LayoutMillimeters))
    title.attemptResize(QgsLayoutSize(spec.page_width - 2 * spec.margin,
                                      TITLE_HEIGHT_MM, QgsUnitTypes.LayoutMillimeters))
    layout.addLayoutItem(title)

    # The viewing direction and the scale, and nothing else. This line used to
    # restate the trace -- name, azimuth in degrees, length -- which is data
    # about how the drawing was made rather than anything read off it. Which way
    # you are looking at the wall is the one fact a section caption genuinely
    # has to carry, and it was the one thing missing.
    subtitle = QgsLayoutItemLabel(layout)
    subtitle.setText(
        f"Looking {line.facing_name} · 1:{denom:g}" if spec.caption is None
        else spec.caption
    )
    sfont = QFont()
    sfont.setPointSize(8)
    subtitle.setFont(sfont)
    subtitle.setFontColor(QColor(90, 90, 90))
    subtitle.attemptMove(QgsLayoutPoint(spec.margin, spec.margin * 0.5 + 8.0,
                                        QgsUnitTypes.LayoutMillimeters))
    subtitle.attemptResize(QgsLayoutSize(spec.page_width - 2 * spec.margin, 6.0,
                                         QgsUnitTypes.LayoutMillimeters))
    layout.addLayoutItem(subtitle)

    # --- map ---------------------------------------------------------------
    # The frame is sized to the drawing rather than to the page. Stretching it
    # to the full available area is what made sections look lost in white space:
    # the map fills the sheet but the section only occupies a band across the
    # middle of it. Sized to the content and centred, the sheet is used.
    map_item = QgsLayoutItemMap(layout)
    avail_w, avail_h = spec.available_area()
    map_w = min(draw_w, avail_w)
    map_h = min(draw_h, avail_h)
    area_x = spec.margin
    area_y = spec.margin + TITLE_HEIGHT_MM
    map_item.attemptMove(QgsLayoutPoint(
        area_x + max(0.0, (avail_w - map_w) / 2.0),
        area_y + max(0.0, (avail_h - map_h) / 2.0),
        QgsUnitTypes.LayoutMillimeters))
    map_item.attemptResize(QgsLayoutSize(map_w, map_h, QgsUnitTypes.LayoutMillimeters))
    map_item.setCrs(session.crs)

    layers = []
    overrides = {}
    if session.polygon_layer is not None:
        layers.append(session.polygon_layer)
        overrides[session.polygon_layer.id()] = (
            _outline_only_style(session.polygon_layer, spec)
            if spec.kind == WIREFRAME
            else _filled_style(session.polygon_layer, source_layer, spec)
        )
    # The section limits, drawn under the units so it reads as the edge of the
    # surface rather than as another unit boundary.
    frame = getattr(session, "frame_layer", None) if spec.show_frame else None
    if frame is not None:
        layers.append(frame)
        overrides[frame.id()] = _frame_style(frame, spec.kind)
    if spec.kind == WIREFRAME and session.photo_layer is not None:
        layers.append(session.photo_layer)

    map_item.setLayers(layers)
    map_item.setLayerStyleOverrides(overrides)
    map_item.setFollowVisibilityPreset(False)
    map_item.setKeepLayerSet(True)
    map_item.setKeepLayerStyles(True)

    # No padding: the frame is already exactly the size of the section at this
    # scale, so any pad would shrink the drawing inside its own border.
    map_item.setExtent(session.section_rectangle(pad=0.0))
    map_item.setScale(denom)
    map_item.setFrameEnabled(True)
    map_item.setFrameStrokeColor(QColor(40, 40, 40))
    map_item.setFrameStrokeWidth(
        QgsLayoutMeasurement(0.3, QgsUnitTypes.LayoutMillimeters)
    )
    if spec.kind == WIREFRAME:
        map_item.setBackgroundColor(_colour(spec.background_colour, QColor(20, 20, 20)))
    layout.addLayoutItem(map_item)

    _add_elevation_grid(map_item, spec)

    # --- legend ------------------------------------------------------------
    if spec.show_legend and spec.kind == DIGITIZED:
        legend = QgsLayoutItemLegend(layout)
        legend.setTitle("Stratigraphic units")
        legend.setLinkedMap(map_item)
        legend.setLegendFilterByMapEnabled(True)
        legend.setResizeToContents(True)
        legend.attemptMove(QgsLayoutPoint(
            spec.page_width - spec.margin - LEGEND_WIDTH_MM + 4.0,
            spec.margin + TITLE_HEIGHT_MM,
            QgsUnitTypes.LayoutMillimeters))
        legend.attemptResize(QgsLayoutSize(LEGEND_WIDTH_MM - 6.0, avail_h,
                                           QgsUnitTypes.LayoutMillimeters))
        layout.addLayoutItem(legend)

    # --- scale bar ---------------------------------------------------------
    if not spec.show_scalebar:
        return layout

    bar = QgsLayoutItemScaleBar(layout)
    bar.setLinkedMap(map_item)
    # Double box: alternating fills make each quarter individually countable,
    # which a single continuous box does not.
    bar.setStyle("Double Box")
    bar.setUnits(QgsUnitTypes.DistanceMeters)
    bar.setUnitLabel("m")
    # Three metres overall: the left metre quartered so 0.25 and 0.50 read
    # directly, then whole metres out to 3.
    per_segment, right, left = scalebar_layout(width_m)
    bar.setUnitsPerSegment(per_segment)
    bar.setNumberOfSegments(right)
    bar.setNumberOfSegmentsLeft(left)
    bar.setHeight(2.5)
    try:
        bar_text = QgsTextFormat()
        bar_text.setSize(6)
        bar.setTextFormat(bar_text)
    except AttributeError:
        # Older builds only expose the font accessor.
        small = QFont()
        small.setPointSize(6)
        bar.setFont(small)
    bar.update()
    bar.attemptMove(QgsLayoutPoint(
        spec.margin,
        spec.page_height - spec.margin - SCALEBAR_HEIGHT_MM + 2.0,
        QgsUnitTypes.LayoutMillimeters))
    layout.addLayoutItem(bar)

    return layout


def _add_elevation_grid(map_item: QgsLayoutItemMap, spec: FigureSpec) -> None:
    """The elevation graticule: horizontal lines only.

    Elevation is annotated on the left, with no conversion, because the y axis
    already *is* elevation. There is deliberately no x axis -- chainage along a
    wall is not a quantity anyone reads off a section drawing, and the scale bar
    already carries horizontal measure. Vertical grid lines are suppressed by
    setting an interval wider than any section.
    """
    grid = QgsLayoutItemMapGrid("Elevation", map_item)
    map_item.grids().addGrid(grid)
    grid.setEnabled(True)
    grid.setIntervalX(1.0e6)
    grid.setIntervalY(spec.graticule)
    grid.setStyle(QgsLayoutItemMapGrid.Solid)

    symbol = QgsLineSymbol.createSimple({
        "color": "80,80,80,90",
        "width": "0.15",
        "width_unit": "MM",
    })
    grid.setLineSymbol(symbol)

    grid.setAnnotationEnabled(True)
    grid.setAnnotationPrecision(2)
    grid.setAnnotationFrameDistance(1.2)
    font = QFont()
    font.setPointSize(7)
    grid.setAnnotationFont(font)
    grid.setAnnotationFontColor(QColor(30, 30, 30))

    # Elevation down the left only. Every other edge is silent.
    grid.setAnnotationDisplay(QgsLayoutItemMapGrid.LatitudeOnly,
                              QgsLayoutItemMapGrid.Left)
    grid.setAnnotationPosition(QgsLayoutItemMapGrid.OutsideMapFrame,
                               QgsLayoutItemMapGrid.Left)
    for side in (QgsLayoutItemMapGrid.Right,
                 QgsLayoutItemMapGrid.Top,
                 QgsLayoutItemMapGrid.Bottom):
        grid.setAnnotationDisplay(QgsLayoutItemMapGrid.HideAll, side)


def export_figure(session, spec: FigureSpec, out_path: str | Path,
                  source_layer=None) -> tuple[bool, str]:
    """Build and export a figure. Returns (ok, message).

    The layout is registered with the project so the user can open it in the
    Layout Manager and keep tweaking it -- the export is a starting point, not
    a dead end.
    """
    out_path = Path(out_path)
    layout = build_layout(session, spec, source_layer)

    manager = QgsProject.instance().layoutManager()
    existing = manager.layoutByName(layout.name())
    if existing is not None:
        manager.removeLayout(existing)
    manager.addLayout(layout)

    exporter = QgsLayoutExporter(layout)
    suffix = out_path.suffix.lower()

    if suffix == ".pdf":
        settings = QgsLayoutExporter.PdfExportSettings()
        settings.dpi = spec.dpi
        result = exporter.exportToPdf(str(out_path), settings)
    elif suffix == ".svg":
        settings = QgsLayoutExporter.SvgExportSettings()
        settings.dpi = spec.dpi
        settings.exportAsLayers = True
        result = exporter.exportToSvg(str(out_path), settings)
    else:
        settings = QgsLayoutExporter.ImageExportSettings()
        settings.dpi = spec.dpi
        # No world file: section-local coordinates are not much use to another
        # program, and it is one less thing to keep in step with the drawing.
        #
        # Note that QGIS still attempts to georeference the output afterwards
        # because the layout has a reference map, and GDAL logs
        # "PNG driver does not support update access" when it cannot. That is
        # harmless -- the image is fully written before it happens -- and the
        # flag above does not suppress it.
        settings.generateWorldFile = False
        result = exporter.exportToImage(str(out_path), settings)

    if result == QgsLayoutExporter.Success:
        return True, (
            f"Written to {out_path}. The layout '{layout.name()}' is in the "
            "Layout Manager if you want to adjust it."
        )
    return False, f"Export failed ({result}) writing {out_path}"
