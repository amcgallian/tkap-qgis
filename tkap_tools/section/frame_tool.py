"""Drag the section frame's edges to crop the drawing surface.

The frame is the extent the figure exports at, so cropping it is how you cut a
section down to the part of the wall you actually want without going back to
crop the ortho. An ortho routinely covers three walls of a trench; only one of
them is this section.

Eight handles, the ones every graphics program has: four corners move both axes,
four edge midpoints move one. The frame layer itself stays read-only throughout
-- letting the vertex tool at it would allow a rectangle to stop being a
rectangle, and there would then be two disagreeing ideas of where the section
ends. This tool is the only writer, and it writes through
``SectionSession.set_frame`` so the line, the layer on screen and the export
extent can never drift apart.
"""

from __future__ import annotations

from qgis.core import QgsPointXY, QgsRectangle, QgsWkbTypes
from qgis.gui import QgsMapTool, QgsRubberBand
from qgis.PyQt.QtCore import Qt, pyqtSignal
from qgis.PyQt.QtGui import QColor

from .section_geom import MIN_SECTION_SPAN

#: How close the cursor has to be to a handle to grab it, in screen pixels.
GRAB_RADIUS_PX = 11

#: Half the drawn size of a handle, in screen pixels.
HANDLE_HALF_PX = 5

HANDLE_COLOUR = QColor(0, 180, 255)
LIVE_COLOUR = QColor(255, 190, 0)

#: (ix, iy) over left/centre/right and bottom/centre/top, minus the middle,
#: which would be a move handle and is deliberately not offered -- sliding the
#: whole frame is what panning the canvas is for, and an accidental move is much
#: harder to notice than an accidental resize.
_HANDLES = tuple(
    (ix, iy)
    for iy in (0, 1, 2)
    for ix in (0, 1, 2)
    if not (ix == 1 and iy == 1)
)

#: Which Qt cursor reads as "this drags that way".
_CURSORS = {
    (0, 0): Qt.SizeBDiagCursor, (2, 2): Qt.SizeBDiagCursor,
    (2, 0): Qt.SizeFDiagCursor, (0, 2): Qt.SizeFDiagCursor,
    (1, 0): Qt.SizeVerCursor,   (1, 2): Qt.SizeVerCursor,
    (0, 1): Qt.SizeHorCursor,   (2, 1): Qt.SizeHorCursor,
}


class FrameResizeTool(QgsMapTool):
    """Map tool that resizes the section frame by dragging its handles."""

    #: Emitted with (x_min, z_min, x_max, z_max) as the drag is committed, so
    #: the panel's numeric boxes track the canvas. QgsMapTool already provides
    #: ``deactivated``, which the panel uses to untick its toggle when QGIS
    #: switches tools behind our back.
    frameChanged = pyqtSignal(float, float, float, float)

    def __init__(self, canvas, session) -> None:
        super().__init__(canvas)
        self.canvas = canvas
        self.session = session

        self._band = QgsRubberBand(canvas, QgsWkbTypes.PolygonGeometry)
        self._band.setColor(QColor(LIVE_COLOUR.red(), LIVE_COLOUR.green(),
                                   LIVE_COLOUR.blue(), 40))
        self._band.setStrokeColor(LIVE_COLOUR)
        self._band.setWidth(2)

        # Handles are drawn as their own little rubber bands rather than as
        # QgsVertexMarkers so they can be sized in screen pixels at any zoom.
        self._handle_bands = []
        for _ in _HANDLES:
            band = QgsRubberBand(canvas, QgsWkbTypes.PolygonGeometry)
            band.setColor(QColor(255, 255, 255, 200))
            band.setStrokeColor(HANDLE_COLOUR)
            band.setWidth(2)
            self._handle_bands.append(band)

        self._rect: QgsRectangle | None = None
        self._dragging: tuple[int, int] | None = None
        self._start_rect: QgsRectangle | None = None

        # Handles are sized in screen pixels, so their map-unit size changes
        # with every zoom. Without this they stay the size they were drawn at
        # and drift away from where the cursor can actually grab them.
        self.canvas.extentsChanged.connect(self._on_canvas_extents)

    # ------------------------------------------------------------ lifecycle --

    def activate(self) -> None:
        super().activate()
        self._rect = self.session.frame_rectangle()
        self._redraw()

    def deactivate(self) -> None:
        self._dragging = None
        self._band.reset(QgsWkbTypes.PolygonGeometry)
        for band in self._handle_bands:
            band.reset(QgsWkbTypes.PolygonGeometry)
        self.canvas.setCursor(Qt.ArrowCursor)
        # QgsMapTool.deactivate() emits deactivated(); the panel listens to that
        # rather than to anything of ours, so the toggle also unticks when QGIS
        # switches tools on its own.
        super().deactivate()

    def _on_canvas_extents(self) -> None:
        if self.isActive():
            self._redraw()

    def cleanup(self) -> None:
        """Take the rubber bands off the canvas. Call before dropping the tool."""
        try:
            self.canvas.extentsChanged.disconnect(self._on_canvas_extents)
        except (TypeError, RuntimeError):
            pass
        for band in [self._band, *self._handle_bands]:
            try:
                self.canvas.scene().removeItem(band)
            except Exception:
                pass
        self._handle_bands = []

    # --------------------------------------------------------------- drawing --

    def _map_units_per_pixel(self) -> float:
        return self.canvas.mapSettings().mapUnitsPerPixel()

    @staticmethod
    def _handle_point(rect: QgsRectangle, ix: int, iy: int) -> QgsPointXY:
        xs = (rect.xMinimum(), rect.center().x(), rect.xMaximum())
        ys = (rect.yMinimum(), rect.center().y(), rect.yMaximum())
        return QgsPointXY(xs[ix], ys[iy])

    def _redraw(self) -> None:
        if self._rect is None:
            return
        rect = self._rect
        self._band.reset(QgsWkbTypes.PolygonGeometry)
        for pt in (
            QgsPointXY(rect.xMinimum(), rect.yMinimum()),
            QgsPointXY(rect.xMaximum(), rect.yMinimum()),
            QgsPointXY(rect.xMaximum(), rect.yMaximum()),
            QgsPointXY(rect.xMinimum(), rect.yMaximum()),
        ):
            self._band.addPoint(pt, False)
        self._band.closePoints()
        self._band.show()

        half = HANDLE_HALF_PX * self._map_units_per_pixel()
        for band, (ix, iy) in zip(self._handle_bands, _HANDLES):
            centre = self._handle_point(rect, ix, iy)
            band.reset(QgsWkbTypes.PolygonGeometry)
            for dx, dy in ((-1, -1), (1, -1), (1, 1), (-1, 1)):
                band.addPoint(
                    QgsPointXY(centre.x() + dx * half, centre.y() + dy * half),
                    False,
                )
            band.closePoints()
            band.show()

    # -------------------------------------------------------------- picking --

    def _handle_at(self, point: QgsPointXY) -> tuple[int, int] | None:
        """The handle within grabbing distance of a map point, or None."""
        if self._rect is None:
            return None
        tolerance = GRAB_RADIUS_PX * self._map_units_per_pixel()
        best = None
        best_d = tolerance
        for ix, iy in _HANDLES:
            h = self._handle_point(self._rect, ix, iy)
            d = max(abs(h.x() - point.x()), abs(h.y() - point.y()))
            if d <= best_d:
                best, best_d = (ix, iy), d
        return best

    def _resized(self, handle: tuple[int, int], point: QgsPointXY) -> QgsRectangle:
        """The start rectangle with the dragged edge(s) moved to ``point``."""
        ix, iy = handle
        base = self._start_rect
        x0, x1 = base.xMinimum(), base.xMaximum()
        y0, y1 = base.yMinimum(), base.yMaximum()
        if ix == 0:
            x0 = point.x()
        elif ix == 2:
            x1 = point.x()
        if iy == 0:
            y0 = point.y()
        elif iy == 2:
            y1 = point.y()
        # Dragging an edge past its opposite number flips the box rather than
        # inverting it; normalising here means the rest of the code never has to
        # wonder which corner is which.
        lo_x, hi_x = min(x0, x1), max(x0, x1)
        lo_y, hi_y = min(y0, y1), max(y0, y1)
        # Refuse to collapse: hold the minimum span open against the edge that
        # is not moving, so the frame stops rather than turning inside out.
        if hi_x - lo_x < MIN_SECTION_SPAN:
            if ix == 0:
                lo_x = hi_x - MIN_SECTION_SPAN
            else:
                hi_x = lo_x + MIN_SECTION_SPAN
        if hi_y - lo_y < MIN_SECTION_SPAN:
            if iy == 0:
                lo_y = hi_y - MIN_SECTION_SPAN
            else:
                hi_y = lo_y + MIN_SECTION_SPAN
        return QgsRectangle(lo_x, lo_y, hi_x, hi_y)

    # ---------------------------------------------------------------- events --

    def canvasPressEvent(self, event) -> None:
        if event.button() != Qt.LeftButton or self._rect is None:
            return
        point = self.toMapCoordinates(event.pos())
        handle = self._handle_at(point)
        if handle is None:
            return
        self._dragging = handle
        self._start_rect = QgsRectangle(self._rect)

    def canvasMoveEvent(self, event) -> None:
        if self._rect is None:
            return
        point = self.toMapCoordinates(event.pos())
        if self._dragging is None:
            handle = self._handle_at(point)
            self.canvas.setCursor(
                _CURSORS.get(handle, Qt.ArrowCursor) if handle else Qt.ArrowCursor
            )
            return
        self._rect = self._resized(self._dragging, point)
        self._redraw()

    def canvasReleaseEvent(self, event) -> None:
        if self._dragging is None or self._rect is None:
            return
        self._dragging = None
        self._commit()

    def keyPressEvent(self, event) -> None:
        if event.key() != Qt.Key_Escape:
            return
        if self._dragging is not None:
            # Abandon the drag in progress, keeping the frame as it was.
            self._dragging = None
            self._rect = QgsRectangle(self._start_rect)
            self._redraw()
        else:
            self.canvas.unsetMapTool(self)

    def _commit(self) -> None:
        rect = self._rect
        try:
            self.session.set_frame(
                rect.xMinimum(), rect.yMinimum(),
                rect.xMaximum(), rect.yMaximum(),
            )
        except ValueError:
            # Rejected by the line: put the handles back on the extent that is
            # actually in force rather than leaving them somewhere it refused.
            self._rect = self.session.frame_rectangle()
            self._redraw()
            return
        self._redraw()
        self.frameChanged.emit(
            rect.xMinimum(), rect.yMinimum(), rect.xMaximum(), rect.yMaximum()
        )

    def sync_from_session(self) -> None:
        """Pick up a frame changed elsewhere (the panel's numeric boxes)."""
        self._rect = self.session.frame_rectangle()
        self._redraw()
