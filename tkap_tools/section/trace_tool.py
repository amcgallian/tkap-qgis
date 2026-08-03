"""The two-click map tool for drawing a section trace in plan view.

A line rather than a box, because a box gives an area but no direction: chainage
needs an origin and a heading, and knowing which face of the wall is being drawn
needs a side. Two clicks give all three.
"""

from __future__ import annotations

import math

from qgis.core import (
    QgsCoordinateReferenceSystem,
    QgsCoordinateTransform,
    QgsGeometry,
    QgsPointLocator,
    QgsPointXY,
    QgsProject,
    QgsWkbTypes,
)
from qgis.gui import QgsMapTool, QgsRubberBand, QgsSnapIndicator
from qgis.PyQt.QtCore import Qt, pyqtSignal
from qgis.PyQt.QtGui import QColor

from ..common import SITE_CRS_AUTHID
from .su_source import buffer_trace

SITE_CRS = SITE_CRS_AUTHID


class SectionTraceTool(QgsMapTool):
    """Click a start point, click an end point, emit the trace.

    Right-click or Escape cancels. The rubber band previews the line and its
    buffer while the second point is being placed, so the user can see which SUs
    the buffer is about to sweep up before committing.
    """

    #: (start, end) in site CRS, plus whether the face is on the right.
    traceDrawn = pyqtSignal(object, object, bool)
    cancelled = pyqtSignal()
    sideChanged = pyqtSignal(bool)

    def __init__(self, canvas, buffer_width: float = 0.25) -> None:
        super().__init__(canvas)
        self.canvas = canvas
        self.buffer_width = buffer_width
        self._start: QgsPointXY | None = None
        self._last: QgsPointXY | None = None
        #: Which face of the wall is being drawn. False = left of the direction
        #: of travel, the default convention. Toggled with Space or F while
        #: drawing, because the side decides both which SUs are collected and
        #: whether the photo comes out mirrored -- and it is far easier to see
        #: which is right while the buffer preview is on screen than to work it
        #: out afterwards from a reversed drawing.
        self.flipped = False

        self._line_band = QgsRubberBand(canvas, QgsWkbTypes.LineGeometry)
        self._line_band.setColor(QColor(255, 60, 0, 220))
        self._line_band.setWidth(3)

        self._buffer_band = QgsRubberBand(canvas, QgsWkbTypes.PolygonGeometry)
        self._buffer_band.setColor(QColor(255, 60, 0, 50))
        self._buffer_band.setStrokeColor(QColor(255, 60, 0, 120))
        self._buffer_band.setWidth(1)

        # Chainage direction. Which end is 0 and which way x grows decides
        # whether the finished drawing reads left-to-right the way the wall
        # does, so it is shown rather than left to be discovered later.
        self._arrow_band = QgsRubberBand(canvas, QgsWkbTypes.LineGeometry)
        self._arrow_band.setColor(QColor(255, 230, 0, 240))
        self._arrow_band.setWidth(2)

        self._origin_band = QgsRubberBand(canvas, QgsWkbTypes.PointGeometry)
        self._origin_band.setColor(QColor(255, 230, 0, 240))
        self._origin_band.setIcon(QgsRubberBand.ICON_CIRCLE)
        self._origin_band.setIconSize(11)
        self._origin_band.setWidth(2)

        self._snap = QgsSnapIndicator(canvas)

    # ------------------------------------------------------------------ CRS --

    def _to_site(self, point: QgsPointXY) -> QgsPointXY:
        """Canvas CRS -> EPSG:32636, which is what the section maths expects."""
        canvas_crs = self.canvas.mapSettings().destinationCrs()
        site = QgsCoordinateReferenceSystem(SITE_CRS)
        if canvas_crs == site:
            return point
        xform = QgsCoordinateTransform(canvas_crs, site, QgsProject.instance())
        return xform.transform(point)

    # -------------------------------------------------------------- events --

    def canvasMoveEvent(self, event) -> None:
        match = self.canvas.snappingUtils().snapToMap(event.pos())
        self._snap.setMatch(match)
        point = match.point() if match.isValid() else self.toMapCoordinates(event.pos())
        self._last = point
        if self._start is None:
            return
        self._draw_preview(self._start, point)

    def canvasPressEvent(self, event) -> None:
        if event.button() == Qt.RightButton:
            self.reset()
            self.cancelled.emit()
            return

        match = self.canvas.snappingUtils().snapToMap(event.pos())
        point = match.point() if match.isValid() else self.toMapCoordinates(event.pos())

        if self._start is None:
            self._start = point
            return

        # Reject a degenerate trace rather than letting SectionLine raise later.
        if point.distance(self._start) < 1e-6:
            return

        start_site = self._to_site(self._start)
        end_site = self._to_site(point)
        flipped = self.flipped
        try:
            self.reset()
        finally:
            # The trace is the whole point of this tool: emit it even if
            # clearing the rubber bands goes wrong, rather than silently
            # swallowing the user's two clicks.
            self.traceDrawn.emit(start_site, end_site, flipped)

    def keyPressEvent(self, event) -> None:
        if event.key() == Qt.Key_Escape:
            self.reset()
            self.cancelled.emit()
        elif event.key() in (Qt.Key_Space, Qt.Key_F):
            self.toggle_side()

    def toggle_side(self) -> None:
        """Swap which face of the wall is being drawn, updating the preview."""
        self.flipped = not self.flipped
        if self._start is not None and self._last is not None:
            self._draw_preview(self._start, self._last)
        self.sideChanged.emit(self.flipped)

    def deactivate(self) -> None:
        self.reset()
        super().deactivate()

    # -------------------------------------------------------------- drawing --

    def _draw_preview(self, start: QgsPointXY, end: QgsPointXY) -> None:
        line = QgsGeometry.fromPolylineXY([start, end])
        self._line_band.setToGeometry(line, None)
        # The same buffer helper the SU search uses, on the same side, so the
        # shaded area is exactly what will be collected.
        self._buffer_band.setToGeometry(
            buffer_trace(line, self.buffer_width, left=not self.flipped), None
        )
        # Chainage runs from the circled end towards the arrowhead, and that is
        # the direction the drawing will read left-to-right.
        self._origin_band.setToGeometry(QgsGeometry.fromPointXY(start), None)
        self._arrow_band.setToGeometry(self._arrow_head(start, end), None)

    def _arrow_head(self, start: QgsPointXY, end: QgsPointXY) -> QgsGeometry:
        """Two barbs at the far end, sized in screen terms so they stay legible
        whatever the zoom."""
        dx, dy = end.x() - start.x(), end.y() - start.y()
        length = math.hypot(dx, dy)
        if length < 1e-9:
            return QgsGeometry()
        ux, uy = dx / length, dy / length

        # About 14 screen pixels, converted to map units at the current scale.
        size = min(self.canvas.mapUnitsPerPixel() * 14.0, length * 0.4)
        # Barbs at +/- 30 degrees behind the tip.
        cos_a, sin_a = math.cos(math.radians(150)), math.sin(math.radians(150))
        barbs = []
        for s in (sin_a, -sin_a):
            bx = ux * cos_a - uy * s
            by = ux * s + uy * cos_a
            barbs.append([
                end,
                QgsPointXY(end.x() + bx * size, end.y() + by * size),
            ])
        return QgsGeometry.fromMultiPolylineXY(barbs)

    def set_buffer_width(self, width: float) -> None:
        self.buffer_width = width

    def reset(self) -> None:
        self._start = None
        self._last = None
        self._line_band.reset(QgsWkbTypes.LineGeometry)
        self._buffer_band.reset(QgsWkbTypes.PolygonGeometry)
        self._arrow_band.reset(QgsWkbTypes.LineGeometry)
        self._origin_band.reset(QgsWkbTypes.PointGeometry)
        # An empty Match clears the indicator. Passing None raises -- the
        # binding takes a Match by value, not an optional pointer.
        self._snap.setMatch(QgsPointLocator.Match())
