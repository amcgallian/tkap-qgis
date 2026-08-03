"""Getting a section photo into section-local coordinates.

Three separable problems, deliberately kept apart:

1. **Reading control points.** The Emlid CSV export leaves Easting/Northing/
   Elevation empty when the receiver is left on "Global CS", filling only
   Longitude/Latitude/Ellipsoidal height. Both layouts occur, so the loader
   handles either.
2. **Getting onto the right vertical datum.** The DB records orthometric
   heights; a Global-CS export gives ellipsoidal. At TKAP those differ by about
   36 m, so mixing them silently would put the photo 36 m above the SUs. PROJ
   cannot help here -- no geoid grid is installed, and it passes the height
   through unchanged rather than failing -- so the separation is an explicit,
   calibratable number.
3. **Fitting a transform.** A Metashape ortho of a wall arrives already metric
   and 1:1, so a similarity (or even a translation) fits it, and the result can
   be written as a plain geotransform with no resampling at all. A raw handheld
   frame needs the full projective fit. The model is chosen by how many points
   the user has picked, and residuals are always reported.
"""

from __future__ import annotations

import csv
import math
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path

import numpy as np

from .section_geom import SectionLine

#: Geoid-ellipsoid separation at Turkmen-Karahoyuk (37.62 N, 33.03 E).
#: EGM2008 over the Konya plain is about +36 m; the value implied by comparing
#: this site's ellipsoidal GCPs against its orthometric SU altitudes is 35.9.
#: Only used as a starting guess -- see :func:`calibrate_separation`.
DEFAULT_GEOID_SEPARATION = 35.9


class HeightDatum(Enum):
    ORTHOMETRIC = "orthometric"    # what the SU altitude_* columns hold
    ELLIPSOIDAL = "ellipsoidal"    # what a Global-CS Emlid export holds

    @property
    def label(self) -> str:
        return {
            HeightDatum.ORTHOMETRIC: "orthometric (matches SU altitudes)",
            HeightDatum.ELLIPSOIDAL: "ellipsoidal (raw GNSS)",
        }[self]


class FitModel(Enum):
    """Transform families, cheapest first. ``min_points`` is the exact-fit
    count; more points give a least-squares solution and real residuals."""

    TRANSLATION = ("translation", 1)
    RIGID = ("rigid (rotate + shift)", 2)
    SIMILARITY = ("similarity (+ uniform scale)", 2)
    AFFINE = ("affine", 3)
    PROJECTIVE = ("projective (homography)", 4)

    def __init__(self, label: str, min_points: int) -> None:
        self.label = label
        self.min_points = min_points

    @property
    def is_affine(self) -> bool:
        """Affine and simpler models can be baked into a geotransform, which
        means the raster is placed without being resampled at all."""
        return self is not FitModel.PROJECTIVE


# ------------------------------------------------------------ control points --


@dataclass
class ControlPoint:
    """A surveyed point, optionally tied to a pixel in the photo."""

    name: str
    easting: float
    northing: float
    height: float                       # in whatever datum ``datum`` says
    datum: HeightDatum = HeightDatum.ELLIPSOIDAL
    #: Pixel location, origin top-left, row increasing downward -- the
    #: convention the user is clicking in. None until picked.
    pixel_x: float | None = None
    pixel_y: float | None = None
    enabled: bool = True
    residual: float | None = None       # metres, filled in after a fit

    @property
    def is_picked(self) -> bool:
        return self.pixel_x is not None and self.pixel_y is not None

    def orthometric(self, separation: float) -> float:
        """Height on the datum the SU table uses."""
        if self.datum is HeightDatum.ORTHOMETRIC:
            return self.height
        return self.height - separation

    def section_xy(self, line: SectionLine, separation: float) -> tuple[float, float]:
        """Where this point lands in section space.

        The plan position is projected onto the trace, which is exactly the
        "smush the wall onto a plane" step -- perpendicular wander is discarded.
        Use :meth:`offset_from` to see how much was discarded.
        """
        return (
            line.chainage(self.easting, self.northing),
            self.orthometric(separation),
        )

    def offset_from(self, line: SectionLine) -> float:
        """Signed perpendicular distance from the trace, in metres."""
        return line.offset(self.easting, self.northing)


def _f(row: dict, *names: str) -> float | None:
    """First populated numeric field among ``names``."""
    for n in names:
        raw = (row.get(n) or "").strip()
        if raw:
            try:
                return float(raw)
            except ValueError:
                continue
    return None


def load_emlid_csv(
    path: str | Path, *, transform_lonlat=None
) -> tuple[list[ControlPoint], list[str]]:
    """Read an Emlid Reach survey export.

    Returns the points and a list of human-readable notes about what had to be
    inferred -- which datum was found, whether coordinates came from the
    projected or the geographic columns. Those notes are surfaced in the dialog
    rather than buried, because getting the datum wrong is a 36 m error that
    still looks plausible on screen.

    ``transform_lonlat`` is a callable (lon, lat) -> (easting, northing) used
    only when the projected columns are empty. The caller supplies it so this
    module needs no GDAL import.
    """
    path = Path(path)
    points: list[ControlPoint] = []
    notes: list[str] = []
    used_geographic = False
    datum = HeightDatum.ORTHOMETRIC

    with path.open(newline="", encoding="utf-8-sig") as fh:
        for row in csv.DictReader(fh):
            name = (row.get("Name") or "").strip()
            if not name:
                continue

            e = _f(row, "Easting")
            n = _f(row, "Northing")
            h = _f(row, "Elevation")

            if e is None or n is None:
                lon = _f(row, "Longitude")
                lat = _f(row, "Latitude")
                if lon is None or lat is None:
                    continue
                if transform_lonlat is None:
                    raise ValueError(
                        f"{path.name} has no Easting/Northing and no transform "
                        "was supplied to convert its Longitude/Latitude"
                    )
                e, n = transform_lonlat(lon, lat)
                used_geographic = True

            if h is None:
                h = _f(row, "Ellipsoidal height")
                if h is None:
                    continue
                datum = HeightDatum.ELLIPSOIDAL

            points.append(
                ControlPoint(name=name, easting=e, northing=n, height=h, datum=datum)
            )

    # Datum is decided per file, not per row: an export is one receiver setup.
    for p in points:
        p.datum = datum

    if used_geographic:
        notes.append(
            "Easting/Northing were empty; coordinates converted from "
            "Longitude/Latitude."
        )
    if datum is HeightDatum.ELLIPSOIDAL:
        notes.append(
            "Heights are ELLIPSOIDAL (the 'Elevation' column was empty, so the "
            "receiver was on Global CS). They must be reduced by the geoid "
            "separation before they will agree with SU altitudes."
        )
    else:
        notes.append("Heights read from the 'Elevation' column as orthometric.")
    notes.append(f"{len(points)} control points read from {path.name}.")
    return points, notes


#: Floor for control-point selection. The SU buffer can be tightened to a few
#: centimetres without harm, but control points are shot *on* the wall face and
#: wander by that much anyway -- 12 cm on the TKAP test wall -- so squeezing
#: their tolerance to match would start discarding good points.
MIN_GCP_TOLERANCE = 0.30


def select_for_section(
    points: list[ControlPoint], line: SectionLine, *, tolerance: float | None = None
) -> tuple[list[ControlPoint], list[ControlPoint]]:
    """Split points into those on this wall and those elsewhere.

    A day's survey file covers every wall of a trench, not one section: the
    TKAP test file holds three walls in one CSV. Selecting by perpendicular
    distance to the trace is what separates them.

    Unlike the SU search, this test is **symmetric** about the trace. SU
    polygons genuinely lie behind one face, so their buffer is one-sided; a
    control point sits on the plane itself and which side of the line it falls
    is survey noise, not meaning.
    """
    tol = max(line.buffer, MIN_GCP_TOLERANCE) if tolerance is None else tolerance
    on, off = [], []
    for p in points:
        chain = line.chainage(p.easting, p.northing)
        near = abs(p.offset_from(line)) <= tol and -tol <= chain <= line.length + tol
        (on if near else off).append(p)
    return on, off


def calibrate_separation(
    points: list[ControlPoint], known_orthometric_top: float
) -> float:
    """Geoid separation implied by a known orthometric elevation.

    The practical calibration: take the highest control point on the wall, which
    is the top of the section, and compare it against the SU altitude the
    database already records for the top of that wall.
    """
    if not points:
        raise ValueError("No control points to calibrate against")
    highest = max(p.height for p in points)
    return highest - known_orthometric_top


def suggest_separation(points: list[ControlPoint]) -> float | None:
    """A separation guess from the heights alone.

    Rounds the gap between the observed heights and the site's known
    orthometric band to the nearest 0.1 m. Crude, and only used to pre-fill the
    spinbox so the user has something sane to adjust.
    """
    if not points:
        return None
    if all(p.datum is HeightDatum.ORTHOMETRIC for p in points):
        return 0.0
    top = max(p.height for p in points)
    # TKAP orthometric ground surface runs about 1029-1035 m.
    return round(top - 1032.0, 1) if top > 1050 else 0.0


# ------------------------------------------------------------------- fitting --


@dataclass
class Fit:
    """A fitted pixel -> section transform, with its error statistics."""

    model: FitModel
    #: 3x3 homogeneous matrix mapping (px, py_up, 1) -> (x, y, w).
    matrix: np.ndarray
    residuals: dict[str, float] = field(default_factory=dict)
    rms: float = 0.0
    worst: tuple[str, float] | None = None
    image_height: int = 0

    @property
    def scale(self) -> float:
        """Approximate metres per pixel, from the linear part."""
        a = self.matrix[:2, :2]
        return float(math.sqrt(abs(np.linalg.det(a))))

    @property
    def is_mirrored(self) -> bool:
        """True when the placement flips the photo left-to-right.

        A negative determinant means the section frame runs the opposite way
        along the wall to the camera's view of it: what is on the right of the
        photo lands on the left of the drawing. That is almost always the trace
        having been drawn in the other direction, and it is fixed by ticking
        Flip rather than by anything about the control points.
        """
        return bool(np.linalg.det(self.matrix[:2, :2]) < 0)

    @property
    def rotation_deg(self) -> float:
        """Rotation of the placement, in degrees.

        For a mirrored fit this is the rotation of the mirrored frame, so read
        it together with :attr:`is_mirrored`.
        """
        return math.degrees(math.atan2(self.matrix[1, 0], self.matrix[0, 0]))

    def apply(self, px: float, py_up: float) -> tuple[float, float]:
        v = self.matrix @ np.array([px, py_up, 1.0])
        return (float(v[0] / v[2]), float(v[1] / v[2]))

    def geotransform(self) -> tuple[float, ...] | None:
        """GDAL geotransform placing the image in section space, or None if the
        model is projective and genuinely needs resampling.

        Converts from the y-up pixel frame used for fitting back to GDAL's
        y-down row indexing.
        """
        if not self.model.is_affine:
            return None
        m = self.matrix
        h = self.image_height
        return (
            float(m[0, 2] + m[0, 1] * h),   # x origin
            float(m[0, 0]),                 # x per column
            float(-m[0, 1]),                # x per row
            float(m[1, 2] + m[1, 1] * h),   # y origin
            float(m[1, 0]),                 # y per column
            float(-m[1, 1]),                # y per row
        )


def _to_y_up(points: list[ControlPoint], image_height: int) -> np.ndarray:
    """Pixel coords with the origin at bottom-left, y increasing upward.

    Fitting in a right-handed frame means the image-to-section mapping is a
    plain rotation rather than a reflection, so the similarity and rigid models
    behave the way their names suggest.
    """
    return np.array(
        [[p.pixel_x, image_height - p.pixel_y] for p in points], dtype=float
    )


def _fit_similarity(src: np.ndarray, dst: np.ndarray, *, fixed_scale: bool) -> np.ndarray:
    """Least-squares similarity (Umeyama). ``fixed_scale`` gives a rigid fit.

    Reflections are **allowed**. Forbidding one does not prevent a mirrored
    photo; it just prevents the fit from describing it, and least squares then
    settles on a rotation that is wrong everywhere instead. A reflection here
    carries real meaning -- the section frame is looking at the wall from the
    opposite side to the camera -- so it is fitted honestly and reported through
    :attr:`Fit.is_mirrored`, where the dialog can say so.
    """
    src_c = src.mean(axis=0)
    dst_c = dst.mean(axis=0)
    s = src - src_c
    d = dst - dst_c
    cov = d.T @ s / len(src)
    u, sv, vt = np.linalg.svd(cov)
    r = u @ vt
    if fixed_scale:
        scale = 1.0
    else:
        var = (s ** 2).sum() / len(src)
        scale = float(sv.sum() / var) if var > 0 else 1.0
    t = dst_c - scale * r @ src_c
    m = np.eye(3)
    m[:2, :2] = scale * r
    m[:2, 2] = t
    return m


def _fit_translation(src: np.ndarray, dst: np.ndarray) -> np.ndarray:
    m = np.eye(3)
    m[:2, 2] = (dst - src).mean(axis=0)
    return m


def _fit_affine(src: np.ndarray, dst: np.ndarray) -> np.ndarray:
    a = np.hstack([src, np.ones((len(src), 1))])
    sol, *_ = np.linalg.lstsq(a, dst, rcond=None)
    m = np.eye(3)
    m[:2, :] = sol.T
    return m


def _fit_projective(src: np.ndarray, dst: np.ndarray) -> np.ndarray:
    """Direct linear transform, the numpy stand-in for cv2.findHomography.

    Points are normalised before solving (Hartley conditioning); skipping that
    makes the system badly scaled when coordinates are pixel-sized in one axis
    and 1030-ish in the other, which is exactly our case.
    """
    def normalise(pts: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
        c = pts.mean(axis=0)
        d = np.sqrt(((pts - c) ** 2).sum(axis=1)).mean()
        s = math.sqrt(2) / d if d > 0 else 1.0
        t = np.array([[s, 0, -s * c[0]], [0, s, -s * c[1]], [0, 0, 1]])
        h = np.hstack([pts, np.ones((len(pts), 1))])
        return (h @ t.T)[:, :2], t

    ns, ts = normalise(src)
    nd, td = normalise(dst)

    rows = []
    for (x, y), (u, v) in zip(ns, nd):
        rows.append([-x, -y, -1, 0, 0, 0, u * x, u * y, u])
        rows.append([0, 0, 0, -x, -y, -1, v * x, v * y, v])
    _, _, vt = np.linalg.svd(np.array(rows))
    h = vt[-1].reshape(3, 3)
    m = np.linalg.inv(td) @ h @ ts
    return m / m[2, 2]


_FITTERS = {
    FitModel.TRANSLATION: _fit_translation,
    FitModel.RIGID: lambda s, d: _fit_similarity(s, d, fixed_scale=True),
    FitModel.SIMILARITY: lambda s, d: _fit_similarity(s, d, fixed_scale=False),
    FitModel.AFFINE: _fit_affine,
    FitModel.PROJECTIVE: _fit_projective,
}


def best_model_for(n_points: int) -> FitModel:
    """Richest model that ``n_points`` can support without overfitting.

    Deliberately conservative: a projective fit needs a point to spare beyond
    its four, otherwise it interpolates the corners exactly and reports zero
    residual while being wrong everywhere in between.
    """
    if n_points >= 5:
        return FitModel.PROJECTIVE
    if n_points >= 4:
        return FitModel.AFFINE
    if n_points >= 3:
        return FitModel.AFFINE
    if n_points >= 2:
        return FitModel.SIMILARITY
    return FitModel.TRANSLATION


def fit_transform(
    points: list[ControlPoint],
    line: SectionLine,
    *,
    separation: float,
    image_height: int,
    model: FitModel | None = None,
) -> Fit:
    """Fit pixel -> section space and measure how well it worked.

    Residuals are reported in metres in section space, which is the unit the
    archaeologist actually cares about: a 3 cm residual is a 3 cm error on the
    drawing.
    """
    usable = [p for p in points if p.enabled and p.is_picked]
    chosen = model or best_model_for(len(usable))
    if len(usable) < chosen.min_points:
        raise ValueError(
            f"{chosen.label} needs at least {chosen.min_points} picked control "
            f"points; {len(usable)} available"
        )

    src = _to_y_up(usable, image_height)
    dst = np.array([p.section_xy(line, separation) for p in usable], dtype=float)
    matrix = _FITTERS[chosen](src, dst)

    fit = Fit(model=chosen, matrix=matrix, image_height=image_height)
    total = 0.0
    for p, (px, py) in zip(usable, src):
        want = np.array(p.section_xy(line, separation))
        got = np.array(fit.apply(px, py))
        err = float(np.hypot(*(got - want)))
        p.residual = err
        fit.residuals[p.name] = err
        total += err * err
        if fit.worst is None or err > fit.worst[1]:
            fit.worst = (p.name, err)
    fit.rms = math.sqrt(total / len(usable)) if usable else 0.0

    # Points excluded from the fit still get a residual so the table can show
    # what including them would cost.
    for p in points:
        if p not in usable and p.is_picked:
            px, py = p.pixel_x, image_height - p.pixel_y
            want = np.array(p.section_xy(line, separation))
            p.residual = float(np.hypot(*(np.array(fit.apply(px, py)) - want)))

    return fit


# ------------------------------------------------------------------ warping --


def write_placed_raster(
    source_path: str | Path,
    out_path: str | Path,
    fit: Fit,
    crs_wkt: str,
    *,
    resolution: float | None = None,
) -> str:
    """Write the photo into section space and return the output path.

    For affine and simpler models this only rewrites the geotransform and CRS --
    the pixels are copied untouched, so a 33 MP ortho is placed in under a
    second with no resampling loss. Only a projective fit actually warps.
    """
    from osgeo import gdal, osr

    gdal.UseExceptions()
    source_path = str(source_path)
    out_path = str(out_path)

    srs = osr.SpatialReference()
    srs.SetFromUserInput(crs_wkt)

    gt = fit.geotransform()
    if gt is not None and not fit.is_mirrored:
        src = gdal.Open(source_path)
        driver = gdal.GetDriverByName("GTiff")
        dst = driver.CreateCopy(
            out_path, src, strict=0,
            options=["TILED=YES", "COMPRESS=DEFLATE", "PHOTOMETRIC=RGB"]
            if src.RasterCount >= 3 else ["TILED=YES", "COMPRESS=DEFLATE"],
        )
        dst.SetGeoTransform(gt)
        dst.SetProjection(srs.ExportToWkt())
        dst.FlushCache()
        dst = None
        src = None
        return out_path

    # Two cases land here. A genuinely projective fit (gt is None) has to be
    # warped. So does a *mirrored* affine fit: a left-right reflection written
    # only into the geotransform is silently dropped by QGIS's raster renderer --
    # the photo comes back the wrong way round on the canvas even though the fit
    # says it should be flipped -- so when the placement mirrors the image we
    # bake the reflection into the pixels here instead of trusting the header.
    #
    # Either way: hand GDAL a grid of correspondences sampled from the fitted
    # transform and let it warp. Sampling the model rather than passing the
    # original control points keeps the warp faithful to the fit the user
    # reviewed, instead of silently refitting to a different model.
    src = gdal.Open(source_path)
    w, h = src.RasterXSize, src.RasterYSize
    gcps = []
    for col in np.linspace(0, w, 5):
        for row in np.linspace(0, h, 5):
            x, y = fit.apply(float(col), float(h - row))
            gcps.append(gdal.GCP(x, y, 0.0, float(col), float(row)))

    tmp = gdal.Translate("", src, format="VRT", GCPs=gcps, outputSRS=srs.ExportToWkt())
    res = resolution or fit.scale
    warped = gdal.Warp(
        out_path, tmp,
        dstSRS=srs.ExportToWkt(),
        xRes=res, yRes=res,
        tps=True,
        resampleAlg="cubic",
        creationOptions=["TILED=YES", "COMPRESS=DEFLATE"],
        dstAlpha=True,
    )
    warped.FlushCache()
    warped = None
    tmp = None
    src = None
    return out_path
