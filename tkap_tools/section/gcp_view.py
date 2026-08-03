"""Zoomable photo view for clicking control points onto the section image.

The image is read through GDAL rather than Qt: the test ortho is a 33 MB
4-band TIFF at 5875x2323, and going through GDAL means anything GDAL can open
works, including formats Qt has no plugin for.

Display is downsampled to keep a large ortho responsive, but every reported
coordinate is in **full-resolution pixels**, because that is what the fit needs.
The scale factor is tracked so a click on a 1/4-size preview still yields an
exact full-res pixel.
"""

from __future__ import annotations

import numpy as np
from qgis.PyQt.QtCore import QPointF, QRectF, Qt, pyqtSignal
from qgis.PyQt.QtGui import (
    QBrush,
    QColor,
    QImage,
    QPainter,
    QPen,
    QPixmap,
    QTransform,
)
from qgis.PyQt.QtWidgets import (
    QGraphicsEllipseItem,
    QGraphicsScene,
    QGraphicsSimpleTextItem,
    QGraphicsView,
)

#: Longest display edge. Beyond this the preview is downsampled.
MAX_PREVIEW_EDGE = 4000


class PhotoView(QGraphicsView):
    """A pannable, zoomable photo that reports clicks in full-res pixel space."""

    pointClicked = pyqtSignal(float, float)      # full-resolution pixel x, y

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self._scene = QGraphicsScene(self)
        self.setScene(self._scene)
        self.setRenderHints(QPainter.Antialiasing | QPainter.SmoothPixmapTransform)
        self.setDragMode(QGraphicsView.ScrollHandDrag)
        self.setTransformationAnchor(QGraphicsView.AnchorUnderMouse)
        self.setResizeAnchor(QGraphicsView.AnchorUnderMouse)

        self._pixmap_item = None
        self._scale = 1.0            # full-res pixels per preview pixel
        self._markers: dict[str, tuple] = {}
        self._picking = False
        self.image_width = 0
        self.image_height = 0

    # ------------------------------------------------------------- loading --

    def load(self, path: str) -> tuple[int, int]:
        """Load an image and return its full (width, height) in pixels."""
        from osgeo import gdal

        gdal.UseExceptions()
        ds = gdal.Open(str(path))
        if ds is None:
            raise RuntimeError(f"GDAL could not open {path}")

        self.image_width = ds.RasterXSize
        self.image_height = ds.RasterYSize

        longest = max(self.image_width, self.image_height)
        self._scale = max(1.0, longest / MAX_PREVIEW_EDGE)
        out_w = int(round(self.image_width / self._scale))
        out_h = int(round(self.image_height / self._scale))
        # Recompute from the actual output size so a click maps back exactly
        # rather than accumulating the rounding error.
        self._scale = self.image_width / out_w

        count = min(ds.RasterCount, 3)
        bands = [
            ds.GetRasterBand(i + 1).ReadAsArray(
                buf_xsize=out_w, buf_ysize=out_h
            )
            for i in range(count)
        ]
        ds = None

        stack = [self._to_byte(b) for b in bands]
        if len(stack) == 1:                       # greyscale -> RGB
            stack = stack * 3
        rgb = np.dstack(stack).copy(order="C")

        image = QImage(
            rgb.data, out_w, out_h, 3 * out_w, QImage.Format_RGB888
        ).copy()                                   # copy: numpy buffer is transient

        self._scene.clear()
        self._markers.clear()
        self._pixmap_item = self._scene.addPixmap(QPixmap.fromImage(image))
        self._scene.setSceneRect(QRectF(0, 0, out_w, out_h))
        self.resetTransform()
        self.fitInView(self._scene.sceneRect(), Qt.KeepAspectRatio)
        return self.image_width, self.image_height

    @staticmethod
    def _to_byte(band: np.ndarray) -> np.ndarray:
        """Scale any band dtype into 0-255 for display."""
        if band.dtype == np.uint8:
            return band
        arr = band.astype(np.float32)
        finite = arr[np.isfinite(arr)]
        if finite.size == 0:
            return np.zeros(band.shape, dtype=np.uint8)
        lo, hi = np.percentile(finite, [2, 98])
        if hi <= lo:
            lo, hi = float(finite.min()), float(finite.max())
        if hi <= lo:
            return np.zeros(band.shape, dtype=np.uint8)
        return np.clip((arr - lo) * 255.0 / (hi - lo), 0, 255).astype(np.uint8)

    @property
    def is_loaded(self) -> bool:
        return self._pixmap_item is not None

    @property
    def display_pixmap(self) -> QPixmap | None:
        """The downsampled pixmap on screen, for building a placement preview."""
        return self._pixmap_item.pixmap() if self._pixmap_item is not None else None

    @property
    def preview_scale(self) -> float:
        """Full-resolution pixels per preview pixel, so a preview built from the
        display pixmap can be mapped back to full-res coordinates."""
        return self._scale

    # ------------------------------------------------------------- picking --

    def set_picking(self, on: bool) -> None:
        """Arm the next click to report a control point instead of panning."""
        self._picking = on
        self.setDragMode(
            QGraphicsView.NoDrag if on else QGraphicsView.ScrollHandDrag
        )
        self.setCursor(Qt.CrossCursor if on else Qt.ArrowCursor)

    def mousePressEvent(self, event) -> None:
        if self._picking and event.button() == Qt.LeftButton and self.is_loaded:
            scene_pos = self.mapToScene(event.pos())
            if self._scene.sceneRect().contains(scene_pos):
                self.pointClicked.emit(
                    scene_pos.x() * self._scale, scene_pos.y() * self._scale
                )
                return
        super().mousePressEvent(event)

    def wheelEvent(self, event) -> None:
        if not self.is_loaded:
            return
        factor = 1.25 if event.angleDelta().y() > 0 else 1 / 1.25
        self.scale(factor, factor)

    # ------------------------------------------------------------- markers --

    def set_marker(self, name: str, px: float, py: float, *, highlight: bool = False) -> None:
        """Place or move a named marker, given full-resolution pixel coords."""
        self.clear_marker(name)
        x = px / self._scale
        y = py / self._scale
        radius = 7.0

        colour = QColor(0, 200, 255) if highlight else QColor(255, 200, 0)
        dot = QGraphicsEllipseItem(x - radius, y - radius, radius * 2, radius * 2)
        dot.setPen(QPen(colour, 2))
        dot.setBrush(QBrush(QColor(0, 0, 0, 60)))
        dot.setFlag(QGraphicsEllipseItem.ItemIgnoresTransformations, False)
        self._scene.addItem(dot)

        text = QGraphicsSimpleTextItem(name)
        text.setBrush(QBrush(colour))
        text.setPos(x + radius + 2, y - radius)
        self._scene.addItem(text)

        self._markers[name] = (dot, text)

    def clear_marker(self, name: str) -> None:
        items = self._markers.pop(name, None)
        if items:
            for item in items:
                self._scene.removeItem(item)

    def clear_markers(self) -> None:
        for name in list(self._markers):
            self.clear_marker(name)

    def centre_on_pixel(self, px: float, py: float) -> None:
        self.centerOn(QPointF(px / self._scale, py / self._scale))

    def zoom_to_fit(self) -> None:
        if self.is_loaded:
            self.fitInView(self._scene.sceneRect(), Qt.KeepAspectRatio)


class PlacedPreview(QGraphicsView):
    """Shows how the photo will sit in the finished drawing.

    The picking view shows the raw photo, because points have to be clicked in
    image space. This view applies the *fitted* transform to that same
    downsampled thumbnail, so as the control points go on the user can see --
    before committing -- whether the photo will land upright, rotated or
    mirrored. It is orientation only: no resampling and no GCP maths, just the
    fit's own transform on a thumbnail, which makes it cheap enough to redraw on
    every click.
    """

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self._scene = QGraphicsScene(self)
        self.setScene(self._scene)
        self.setRenderHints(QPainter.Antialiasing | QPainter.SmoothPixmapTransform)
        self.setBackgroundBrush(QBrush(QColor(40, 40, 44)))
        self.setMinimumHeight(120)
        self._item = None
        self._content_rect = None

    def clear(self) -> None:
        self._scene.clear()
        self._item = None
        self._content_rect = None

    def set_placement(self, pixmap, scale: float, image_height: int, fit) -> None:
        """Redraw the thumbnail oriented by ``fit`` (upright when ``fit`` is None)."""
        self.clear()
        if pixmap is None:
            return
        item = self._scene.addPixmap(pixmap)
        item.setTransform(self._transform_for(scale, image_height, fit))
        self._item = item
        rect = item.sceneBoundingRect()
        self._content_rect = rect
        self._scene.setSceneRect(rect)
        self.fitInView(rect, Qt.KeepAspectRatio)

    @staticmethod
    def _transform_for(scale: float, image_height: int, fit) -> QTransform:
        if fit is None:
            return QTransform()          # upright: the raw thumbnail as-is
        # Compose downsampled pixel (u, v) -> full-res (px, py-up) -> section
        # space, then flip elevation so up reads up on screen. Perspective terms
        # are carried through, so a projective fit previews with its true
        # keystone rather than a flattened approximation.
        m = fit.matrix
        h = float(image_height)
        to_full = np.array([[scale, 0.0, 0.0],
                            [0.0, -scale, h],
                            [0.0, 0.0, 1.0]])
        p = m @ to_full
        return QTransform(
            p[0, 0], -p[1, 0], p[2, 0],
            p[0, 1], -p[1, 1], p[2, 1],
            p[0, 2], -p[1, 2], p[2, 2],
        )

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        if self._content_rect is not None:
            self.fitInView(self._content_rect, Qt.KeepAspectRatio)
