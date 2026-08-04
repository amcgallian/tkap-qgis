"""Zoomable photo view for clicking control points onto the section image.

The image is read through GDAL rather than Qt: the test ortho is a 33 MB
4-band TIFF at 5875x2323, and going through GDAL means anything GDAL can open
works, including formats Qt has no plugin for.

Two layers of image are on screen at once. A downsampled **overview** covers the
whole photo and loads instantly at any ortho size. Over it, once you zoom past
the point where the overview runs out of pixels, sits a **detail tile**: the
actual full-resolution pixels for the region in view, read straight from GDAL on
demand and replaced as you pan.

That second layer is what makes the control points clickable. The features being
picked are nailheads a few pixels across, and an overview alone can never show
them however far you zoom -- the detail simply is not in it. Streaming the tile
rather than decoding the whole photo keeps memory flat regardless of how large
the ortho is, which matters because these run to hundreds of megapixels.

Every reported coordinate is in **full-resolution pixels**, because that is what
the fit needs. The overview's scale factor is tracked so a click on a 1/4-size
preview still yields an exact full-res pixel, and the detail tile changes only
what is drawn -- never the coordinate maths.
"""

from __future__ import annotations

import numpy as np
from qgis.PyQt.QtCore import QPointF, QRectF, Qt, QTimer, pyqtSignal
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

#: Longest edge of the whole-image overview. Beyond this it is downsampled --
#: the detail tile supplies the missing resolution where it is being looked at.
MAX_PREVIEW_EDGE = 4000

#: Longest edge of one detail tile. Bounds both the GDAL read and the pixmap, so
#: the cost of a detail refresh does not depend on the size of the ortho.
MAX_DETAIL_EDGE = 4096

#: Read the detail tile a little beyond the viewport, so small pans are already
#: covered and do not each trigger a fresh read.
DETAIL_MARGIN = 1.15

#: Settle time after a zoom or pan before the tile is re-read, in milliseconds.
#: Long enough that a continuous drag issues one read rather than dozens.
DETAIL_DELAY_MS = 120

#: Z order within the scene. Explicit because all three are plain items added to
#: one scene, and markers have to stay clickable-looking above both images.
Z_OVERVIEW = 0.0
Z_DETAIL = 1.0
Z_MARKER = 2.0


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

        self._path: str | None = None
        self._detail_item = None
        #: Full-res pixel window the current tile covers, so a pan that stays
        #: inside it costs nothing.
        self._detail_window: tuple[int, int, int, int] | None = None
        self._detail_scale = 0.0     # full-res px per tile px, of that tile
        #: Per-band display stretch fixed from the overview, so tiles match it.
        self._stretch: list = []
        self._detail_timer = QTimer(self)
        self._detail_timer.setSingleShot(True)
        self._detail_timer.timeout.connect(self._refresh_detail)

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

        # The display stretch is decided once, here, from the whole image, and
        # every detail tile is then held to it. Restretching each tile from its
        # own percentiles would make the photo change brightness as you panned,
        # and a control point would be picked against a different rendering from
        # the one it was found in.
        self._stretch = [self._band_range(b) for b in bands]
        stack = [self._to_byte(b, rng) for b, rng in zip(bands, self._stretch)]
        if len(stack) == 1:                       # greyscale -> RGB
            stack = stack * 3
            self._stretch = self._stretch * 3
        rgb = np.dstack(stack).copy(order="C")

        image = QImage(
            rgb.data, out_w, out_h, 3 * out_w, QImage.Format_RGB888
        ).copy()                                   # copy: numpy buffer is transient

        self._scene.clear()
        self._markers.clear()
        self._detail_item = None
        self._detail_window = None
        self._path = str(path)
        self._pixmap_item = self._scene.addPixmap(QPixmap.fromImage(image))
        self._pixmap_item.setZValue(Z_OVERVIEW)
        self._scene.setSceneRect(QRectF(0, 0, out_w, out_h))
        self.resetTransform()
        self.fitInView(self._scene.sceneRect(), Qt.KeepAspectRatio)
        self._schedule_detail()
        return self.image_width, self.image_height

    @staticmethod
    def _band_range(band: np.ndarray) -> tuple[float, float] | None:
        """The display stretch for a band, or None when it needs none (uint8)."""
        if band.dtype == np.uint8:
            return None
        arr = band.astype(np.float32)
        finite = arr[np.isfinite(arr)]
        if finite.size == 0:
            return (0.0, 0.0)
        lo, hi = np.percentile(finite, [2, 98])
        if hi <= lo:
            lo, hi = float(finite.min()), float(finite.max())
        return (float(lo), float(hi))

    @staticmethod
    def _to_byte(band: np.ndarray, rng: tuple[float, float] | None = None) -> np.ndarray:
        """Scale a band into 0-255 for display, using ``rng`` when given.

        Passing the range in is how a detail tile is made to match the overview
        it sits on: the same numbers map to the same greys in both.
        """
        if band.dtype == np.uint8:
            return band
        if rng is None:
            rng = PhotoView._band_range(band)
        lo, hi = rng
        if hi <= lo:
            return np.zeros(band.shape, dtype=np.uint8)
        arr = band.astype(np.float32)
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

    # -------------------------------------------------------------- detail --

    def _schedule_detail(self) -> None:
        """Ask for a detail refresh once the view stops moving."""
        if self.is_loaded:
            self._detail_timer.start(DETAIL_DELAY_MS)

    def _zoom(self) -> float:
        """Device pixels per scene (overview) pixel at the current transform."""
        return abs(self.transform().m11()) or 1.0

    def _clear_detail(self) -> None:
        if self._detail_item is not None:
            self._scene.removeItem(self._detail_item)
            self._detail_item = None
        self._detail_window = None

    def _refresh_detail(self) -> None:
        """Put the full-resolution pixels for the visible region on screen.

        Skipped entirely when there is nothing to gain: an image small enough to
        have loaded whole is already at full resolution, and while zoomed out the
        overview holds more pixels than the screen can show.
        """
        if not self.is_loaded or self._path is None:
            return
        if self._scale <= 1.0 + 1e-6:
            return                      # the overview *is* the full resolution

        zoom = self._zoom()
        if zoom <= 1.0:
            self._clear_detail()        # overview is still finer than the screen
            return

        window = self._visible_window()
        if window is None:
            self._clear_detail()
            return

        # Aim for one tile pixel per device pixel, never finer than the source
        # and never larger than one tile's worth.
        x0, y0, win_w, win_h = window
        target = max(1, int(round(win_w * zoom / self._scale)))
        buf_w = min(win_w, target, MAX_DETAIL_EDGE)
        buf_h = max(1, int(round(win_h * buf_w / win_w)))
        buf_h = min(win_h, buf_h, MAX_DETAIL_EDGE)
        detail_scale = win_w / buf_w

        # Already covered at this resolution or better, and the view has not
        # left the tile: nothing to do.
        if self._detail_window is not None and self._covers(window, detail_scale):
            return

        image = self._read_window(x0, y0, win_w, win_h, buf_w, buf_h)
        if image is None:
            return

        if self._detail_item is None:
            self._detail_item = self._scene.addPixmap(QPixmap.fromImage(image))
            self._detail_item.setZValue(Z_DETAIL)
            self._detail_item.setTransformationMode(Qt.SmoothTransformation)
        else:
            self._detail_item.setPixmap(QPixmap.fromImage(image))

        # Place the tile in scene (overview-pixel) space: it starts at the
        # window's origin and each tile pixel spans detail_scale full-res
        # pixels, which is detail_scale / self._scale scene units.
        unit = detail_scale / self._scale
        self._detail_item.setTransform(
            QTransform().translate(x0 / self._scale, y0 / self._scale)
                        .scale(unit, unit)
        )
        self._detail_window = (x0, y0, win_w, win_h)
        self._detail_scale = detail_scale

    def _covers(self, window, detail_scale: float) -> bool:
        """True when the tile in hand already answers this request."""
        if self._detail_window is None:
            return False
        hx0, hy0, hw, hh = self._detail_window
        x0, y0, w, h = window
        inside = (x0 >= hx0 and y0 >= hy0
                  and x0 + w <= hx0 + hw and y0 + h <= hy0 + hh)
        # Re-read once the request wants meaningfully finer pixels than the tile
        # was built at; a small margin stops a nudge of the wheel re-reading.
        fine_enough = self._detail_scale <= detail_scale * 1.2
        return inside and fine_enough

    def _visible_window(self) -> tuple[int, int, int, int] | None:
        """The visible region as an integer full-res pixel window, with margin."""
        view_rect = self.mapToScene(self.viewport().rect()).boundingRect()
        rect = view_rect.intersected(self._scene.sceneRect())
        if rect.isEmpty():
            return None

        # Grow about the centre so a small pan stays inside the tile.
        cx, cy = rect.center().x(), rect.center().y()
        half_w = rect.width() * DETAIL_MARGIN / 2.0
        half_h = rect.height() * DETAIL_MARGIN / 2.0
        x0 = int(max(0.0, (cx - half_w) * self._scale))
        y0 = int(max(0.0, (cy - half_h) * self._scale))
        x1 = int(min(float(self.image_width), (cx + half_w) * self._scale + 1))
        y1 = int(min(float(self.image_height), (cy + half_h) * self._scale + 1))
        if x1 - x0 < 1 or y1 - y0 < 1:
            return None
        return (x0, y0, x1 - x0, y1 - y0)

    def _read_window(self, x0, y0, win_w, win_h, buf_w, buf_h) -> QImage | None:
        """Read one window of the source at the requested buffer size."""
        from osgeo import gdal

        try:
            gdal.UseExceptions()
            ds = gdal.Open(self._path)
            if ds is None:
                return None
            count = min(ds.RasterCount, 3)
            bands = [
                ds.GetRasterBand(i + 1).ReadAsArray(
                    x0, y0, win_w, win_h, buf_xsize=buf_w, buf_ysize=buf_h
                )
                for i in range(count)
            ]
            ds = None
        except Exception:
            # A detail tile is an enhancement; if the read fails for any reason
            # the overview is still on screen and still clickable.
            return None

        if any(b is None for b in bands):
            return None
        ranges = self._stretch or [None] * len(bands)
        stack = [
            self._to_byte(b, ranges[i] if i < len(ranges) else None)
            for i, b in enumerate(bands)
        ]
        if len(stack) == 1:
            stack = stack * 3
        rgb = np.dstack(stack).copy(order="C")
        return QImage(
            rgb.data, buf_w, buf_h, 3 * buf_w, QImage.Format_RGB888
        ).copy()

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
        self._schedule_detail()

    def scrollContentsBy(self, dx: int, dy: int) -> None:
        # The hook that catches panning: ScrollHandDrag moves the scrollbars
        # rather than the transform, so this fires and wheelEvent does not.
        super().scrollContentsBy(dx, dy)
        self._schedule_detail()

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        self._schedule_detail()

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
        # Above both images: a detail tile is drawn over the overview, and a
        # marker underneath it would disappear exactly when you zoomed in to
        # check it.
        dot.setZValue(Z_MARKER)
        self._scene.addItem(dot)

        text = QGraphicsSimpleTextItem(name)
        text.setBrush(QBrush(colour))
        text.setPos(x + radius + 2, y - radius)
        text.setZValue(Z_MARKER)
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
        self._schedule_detail()

    def zoom_to_fit(self) -> None:
        if self.is_loaded:
            self.fitInView(self._scene.sceneRect(), Qt.KeepAspectRatio)
            self._schedule_detail()


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
