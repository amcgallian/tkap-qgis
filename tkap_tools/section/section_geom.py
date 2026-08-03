"""Section-local coordinate space.

A section is defined by a line in plan view (two endpoints in the site CRS,
EPSG:32636). Everything drawn on that section lives in a 2D space where:

    x = chainage, distance along the section line, in metres
    y = absolute elevation, in metres

Both axes are metres at 1:1, so there is no vertical exaggeration and a
horizontal line in section space is a real elevation contour.

This module is deliberately free of any QGIS import so the maths can be tested
with plain Python. Anything needing geometry intersection lives in su_source.py.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Iterable, Sequence

# Reprojecting a section vertex back to (E, N, Z) is only meaningful if the
# vertex really sits on the plane. Vertices are stored in section space, so the
# round-trip is exact by construction -- but a caller can pass an off-plane
# offset explicitly if it ever needs to model a stepped wall.
Coord2 = tuple[float, float]
Coord3 = tuple[float, float, float]


@dataclass
class SectionLine:
    """The plan-view trace of a section, plus the section-local frame it induces.

    ``p0`` -> ``p1`` sets the direction of increasing chainage. Which face of
    the wall you are looking at decides whether that reads left-to-right or
    mirrored in the finished drawing, hence ``flipped``.
    """

    p0: Coord2
    p1: Coord2
    name: str = "Section"
    #: How far back from the trace to look for SUs. Kept tight: a section trace
    #: usually runs along a baulk with excavated space on both sides, and a wide
    #: two-sided buffer would sweep in the units belonging to the far face.
    buffer: float = 0.25
    flipped: bool = False
    # Vertical extent of the drawing surface. Set once the photo is placed or
    # entered by hand; None means "not yet known".
    z_min: float | None = None
    z_max: float | None = None

    _ux: float = field(init=False, repr=False)
    _uy: float = field(init=False, repr=False)
    _length: float = field(init=False, repr=False)

    def __post_init__(self) -> None:
        dx = self.p1[0] - self.p0[0]
        dy = self.p1[1] - self.p0[1]
        self._length = math.hypot(dx, dy)
        if self._length < 1e-9:
            raise ValueError("Section line endpoints are coincident")
        self._ux = dx / self._length
        self._uy = dy / self._length

    # ---------------------------------------------------------------- frame --

    @property
    def length(self) -> float:
        """Length of the trace in metres; also the full chainage range."""
        return self._length

    @property
    def unit(self) -> Coord2:
        """Unit vector along the trace, in the direction of increasing chainage."""
        return (self._ux, self._uy)

    @property
    def normal(self) -> Coord2:
        """Unit normal, 90 degrees left of the direction of travel.

        Positive perpendicular offset is on this side. Used to tell which face
        of the wall a point sits on.
        """
        return (-self._uy, self._ux)

    @property
    def azimuth(self) -> float:
        """Grid azimuth of the trace in degrees clockwise from grid north."""
        return math.degrees(math.atan2(self._ux, self._uy)) % 360.0

    @property
    def midpoint(self) -> Coord2:
        return ((self.p0[0] + self.p1[0]) / 2.0, (self.p0[1] + self.p1[1]) / 2.0)

    @property
    def looks_left(self) -> bool:
        """True when the wall face being drawn is left of p0 -> p1.

        The drawing convention is: walk from p0 to p1 and the face you are
        looking at is on your left. Flipping means you walked the same line but
        are looking at the opposite face, so the material moves to the right.
        """
        return not self.flipped

    # ----------------------------------------------------------- projection --

    def chainage(self, e: float, n: float) -> float:
        """Distance of (e, n) along the trace, measured from p0.

        Values outside [0, length] mean the point projects beyond an endpoint,
        which is normal for an SU that runs past the end of the section.
        """
        raw = (e - self.p0[0]) * self._ux + (n - self.p0[1]) * self._uy
        return self._length - raw if self.flipped else raw

    def offset(self, e: float, n: float) -> float:
        """Signed perpendicular distance of (e, n) from the trace.

        Positive is to the left of p0 -> p1. Sign is preserved under ``flipped``
        being toggled would mirror it, so flip it too, keeping "positive means
        the same physical side" true in both orientations.
        """
        nx, ny = self.normal
        raw = (e - self.p0[0]) * nx + (n - self.p0[1]) * ny
        return -raw if self.flipped else raw

    def to_section(self, e: float, n: float, z: float) -> Coord2:
        """Project a real-world point onto the section plane. Drops the offset."""
        return (self.chainage(e, n), z)

    def to_world(self, x: float, y: float, offset: float = 0.0) -> Coord3:
        """Inverse of :meth:`to_section`: section-local -> (E, N, Z).

        ``offset`` pushes the result perpendicular to the plane, which is what
        you would use to model a wall face that stands proud of the trace.
        """
        chain = self._length - x if self.flipped else x
        nx, ny = self.normal
        sign = -1.0 if self.flipped else 1.0
        e = self.p0[0] + chain * self._ux + sign * offset * nx
        n = self.p0[1] + chain * self._uy + sign * offset * ny
        return (e, n, y)

    def world_at_chainage(self, x: float) -> Coord2:
        """Plan-view (E, N) of a given chainage. Handy for drawing tick marks."""
        e, n, _ = self.to_world(x, 0.0)
        return (e, n)

    # --------------------------------------------------------------- extent --

    #: Extra chainage the drawing covers beyond the trace, at each end. Set from
    #: whatever the section actually contains -- control points and photo edges
    #: routinely fall a little outside the line that was drawn, and an SU can
    #: run past the end of it. Kept as an override rather than folded into the
    #: length so chainage 0 still means the start of the trace.
    x_min_override: float | None = None
    x_max_override: float | None = None

    @property
    def x_min(self) -> float:
        return 0.0 if self.x_min_override is None else self.x_min_override

    @property
    def x_max(self) -> float:
        return self._length if self.x_max_override is None else self.x_max_override

    def extend_to(self, x_min: float, x_max: float, *, pad: float = 0.0) -> None:
        """Widen the drawing surface to take in ``x_min``..``x_max``.

        Only ever grows: calling it for the photo, then the control points, then
        the SUs leaves a box that holds all three. Never shrinks below the trace
        itself, so the section always covers what the user drew.
        """
        lo = min(self.x_min, x_min - pad, 0.0)
        hi = max(self.x_max, x_max + pad, self._length)
        self.x_min_override = lo
        self.x_max_override = hi

    def reset_extent(self) -> None:
        """Back to exactly the drawn trace."""
        self.x_min_override = None
        self.x_max_override = None

    @property
    def drawing_width(self) -> float:
        """Chainage span of the drawing surface, which is at least the trace."""
        return self.x_max - self.x_min

    def section_extent(self, pad: float = 0.0) -> tuple[float, float, float, float]:
        """(xmin, ymin, xmax, ymax) of the drawing surface in section space."""
        if self.z_min is None or self.z_max is None:
            raise ValueError("Section vertical extent is not set yet")
        return (self.x_min - pad, self.z_min - pad,
                self.x_max + pad, self.z_max + pad)

    def set_vertical_extent(self, z_min: float, z_max: float) -> None:
        if z_max <= z_min:
            raise ValueError(f"z_max ({z_max}) must exceed z_min ({z_min})")
        self.z_min = float(z_min)
        self.z_max = float(z_max)


# --------------------------------------------------------------------- CRS --

# QGIS needs a real CRS to give the canvas 1:1 metre axes.
#
# An ENGCRS is the honest description of a section plane -- a bare Cartesian
# plane in metres with no tie to the ellipsoid -- and QGIS does accept it. But
# PROJ cannot export one to a proj string, so in practice every render emits
# "Object type not exportable to PROJ", the log fills up, and GeoTIFF encoding
# of the CRS is unreliable.
#
# A degenerate transverse mercator is used instead: lat_0 = lon_0 = 0, unit
# scale, no false origin. Our coordinates (x a few metres, y about 1030) are
# perfectly ordinary values in that CRS, PROJ is content, and GDAL writes it to
# a GeoTIFF without complaint. Nothing is ever reprojected between this and the
# site CRS -- plan layers are hidden for the duration of a session -- so the
# fact that it describes a spot off the Gulf of Guinea never matters.
#
# The trace parameters ride along in the REMARK, so a layer or raster carrying
# this CRS can always be tied back to real-world coordinates by hand.
_SECTION_CRS_TEMPLATE = """PROJCRS["{name}",
    BASEGEOGCRS["WGS 84",
        DATUM["World Geodetic System 1984",
            ELLIPSOID["WGS 84",6378137,298.257223563,LENGTHUNIT["metre",1]]],
        PRIMEM["Greenwich",0,ANGLEUNIT["degree",0.0174532925199433]]],
    CONVERSION["Section plane",
        METHOD["Transverse Mercator",ID["EPSG",9807]],
        PARAMETER["Latitude of natural origin",0,ANGLEUNIT["degree",0.0174532925199433]],
        PARAMETER["Longitude of natural origin",0,ANGLEUNIT["degree",0.0174532925199433]],
        PARAMETER["Scale factor at natural origin",1,SCALEUNIT["unity",1]],
        PARAMETER["False easting",0,LENGTHUNIT["metre",1]],
        PARAMETER["False northing",0,LENGTHUNIT["metre",1]]],
    CS[Cartesian,2],
        AXIS["chainage (x)",east,ORDER[1],LENGTHUNIT["metre",1]],
        AXIS["elevation (y)",north,ORDER[2],LENGTHUNIT["metre",1]],
    REMARK["{remark}"]]"""


def section_crs_remark(line: SectionLine) -> str:
    """Human-readable description of how to get back to EPSG:32636."""
    return (
        f"TKAP section '{line.name}'; "
        f"x=chainage from ({line.p0[0]:.3f},{line.p0[1]:.3f}) "
        f"to ({line.p1[0]:.3f},{line.p1[1]:.3f}) in EPSG:32636; "
        f"y=elevation (m, orthometric); azimuth={line.azimuth:.2f}; "
        f"length={line.length:.3f}; flipped={int(line.flipped)}"
    )


def section_crs_wkt(line: SectionLine) -> str:
    """Build the per-section CRS that gives the canvas 1:1 metre axes."""
    return _SECTION_CRS_TEMPLATE.format(
        name=f"TKAP Section {line.name}",
        remark=section_crs_remark(line).replace('"', "'"),
    )


# ------------------------------------------------------------------- spans --


@dataclass
class Span:
    """A contiguous run of chainage over which an SU is present on the wall."""

    x_min: float
    x_max: float
    #: True when the SU polygon actually crosses the trace, False when it was
    #: only caught by the buffer. Buffer-only hits are weaker evidence and the
    #: UI shows them differently.
    on_trace: bool = True

    @property
    def width(self) -> float:
        return self.x_max - self.x_min

    def clipped_to(self, length: float) -> "Span | None":
        """Trim to the section's own chainage range, or None if fully outside."""
        lo = max(self.x_min, 0.0)
        hi = min(self.x_max, length)
        if hi - lo <= 1e-6:
            return None
        return Span(lo, hi, self.on_trace)


def spans_from_chainages(
    values: Iterable[float], *, gap: float = 0.05, on_trace: bool = True
) -> list[Span]:
    """Group sorted chainage values into runs, splitting where a gap appears.

    Used when an SU is sampled as a set of projected vertices rather than as a
    clean intersection: consecutive samples closer than ``gap`` metres are
    treated as one run.
    """
    vals = sorted(values)
    if not vals:
        return []
    spans: list[Span] = []
    start = prev = vals[0]
    for v in vals[1:]:
        if v - prev > gap:
            spans.append(Span(start, prev, on_trace))
            start = v
        prev = v
    spans.append(Span(start, prev, on_trace))
    return spans


def merge_spans(spans: Sequence[Span], *, gap: float = 0.0) -> list[Span]:
    """Merge overlapping or near-touching spans, keeping the strongest evidence."""
    if not spans:
        return []
    ordered = sorted(spans, key=lambda s: s.x_min)
    out = [Span(ordered[0].x_min, ordered[0].x_max, ordered[0].on_trace)]
    for s in ordered[1:]:
        last = out[-1]
        if s.x_min - last.x_max <= gap:
            last.x_max = max(last.x_max, s.x_max)
            last.on_trace = last.on_trace or s.on_trace
        else:
            out.append(Span(s.x_min, s.x_max, s.on_trace))
    return out


# ------------------------------------------------------------------ ticks --


def elevation_ticks(z_min: float, z_max: float, interval: float) -> list[float]:
    """Elevation values for the graticule, snapped to whole multiples."""
    if interval <= 0:
        raise ValueError("interval must be positive")
    first = math.ceil(z_min / interval) * interval
    ticks: list[float] = []
    # Integer stepping so 0.1 and 0.25 intervals do not drift.
    k = 0
    while True:
        v = first + k * interval
        if v > z_max + 1e-9:
            break
        ticks.append(round(v, 6))
        k += 1
    return ticks
