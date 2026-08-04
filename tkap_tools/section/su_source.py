"""Finding which SUs belong on a section, and how high to seed them.

Two ways to build the candidate list:

* **spatial** -- SUs whose plan-view polygon meets the buffered section trace.
  Works against any vector layer: PostGIS, file GDB, shapefile.
* **relational** -- SUs linked to a space through ``space_stratigraphical_unit``.
  Needs a live PostGIS connection, so it is offered only when the SU layer is a
  postgres layer.

Field names differ between sources (the PostGIS view says ``sunumber`` and
``altitude_min``; the file GDB says ``SU`` and has no elevation at all), so
everything goes through :class:`FieldMap`, which sniffs the layer once.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Iterable, Sequence

from qgis.core import (
    Qgis,
    QgsCoordinateTransform,
    QgsCoordinateReferenceSystem,
    QgsFeature,
    QgsFeatureRequest,
    QgsGeometry,
    QgsPointXY,
    QgsProject,
    QgsProviderRegistry,
    QgsVectorLayer,
    QgsWkbTypes,
)

from ..common import SITE_CRS_AUTHID
from .section_geom import SectionLine, Span, merge_spans

SITE_CRS = SITE_CRS_AUTHID

#: Elevations outside this band are data-entry errors, not measurements. The
#: site sits around 1029-1032 m; the dump contains a stray 2047 m.
PLAUSIBLE_Z = (900.0, 1100.0)


class SeedSource(Enum):
    """Where a seed box's vertical extent came from. Shown in the SU table so a
    guess is never mistaken for a measurement."""

    MEASURED = "measured"        # altitude_min and altitude_max both present
    HALF_MEASURED = "half"       # only one altitude; other end inferred or full
    INFERRED = "inferred"        # floor taken from the SU this one is above
    FULL_HEIGHT = "full"         # no evidence; column spanning the section

    @property
    def label(self) -> str:
        return {
            SeedSource.MEASURED: "measured",
            SeedSource.HALF_MEASURED: "part measured",
            SeedSource.INFERRED: "inferred from strat",
            SeedSource.FULL_HEIGHT: "no elevation data",
        }[self]


@dataclass
class FieldMap:
    """Resolved attribute names for one SU layer."""

    su_number: str | None = None
    pk: str | None = None
    alt_min: str | None = None
    alt_max: str | None = None
    su_type: str | None = None
    subtype: str | None = None
    subsubtype: str | None = None
    area: str | None = None
    square: str | None = None

    # Candidate names in priority order. First match wins, case-insensitively.
    # Matching is on the whole name, so "subtype" never shadows "subsubtype".
    _CANDIDATES = {
        "su_number": ("sunumber", "su_number", "su", "sunum"),
        "pk": ("id", "objectid", "fid"),
        "alt_min": ("altitude_min", "alt_min", "z_min", "elev_min", "min_elev"),
        "alt_max": ("altitude_max", "alt_max", "z_max", "elev_max", "max_elev"),
        "su_type": ("type", "type_2", "su_type"),
        "subtype": ("subtype", "type_3", "type_3_fir", "sub_type"),
        # The project's symbology is driven by this one, so it has to survive
        # the trip into the section layer or the clean drawing loses its colours.
        "subsubtype": ("subsubtype", "sub_sub_type", "type_4", "type_3_fir"),
        "area": ("area", "field", "field1"),
        "square": ("square",),
    }

    @classmethod
    def sniff(cls, layer: QgsVectorLayer) -> "FieldMap":
        present = {f.name().lower(): f.name() for f in layer.fields()}
        resolved: dict[str, str | None] = {}
        for attr, options in cls._CANDIDATES.items():
            resolved[attr] = next(
                (present[o] for o in options if o in present), None
            )
        return cls(**resolved)

    @property
    def has_altitudes(self) -> bool:
        return self.alt_min is not None and self.alt_max is not None


def _num(value) -> float | None:
    """Coerce an attribute to float, tolerating NULL and the string-typed
    numerics that come out of the Emlid shapefiles."""
    if value is None:
        return None
    try:
        f = float(value)
    except (TypeError, ValueError):
        return None
    # NaN never equals itself.
    return None if f != f else f


def _plausible(z: float | None) -> float | None:
    if z is None:
        return None
    return z if PLAUSIBLE_Z[0] <= z <= PLAUSIBLE_Z[1] else None


@dataclass
class SUCandidate:
    """One SU that should appear on the section."""

    su_id: int                       # source layer feature id, the stable handle
    su_number: str                   # what the excavator calls it
    spans: list[Span] = field(default_factory=list)
    #: The seed box's vertical extent. Overwritten by the seeding step.
    alt_min: float | None = None
    alt_max: float | None = None
    #: What the database recorded, kept separate so the table can still show it
    #: after seeding has replaced alt_min/alt_max with a full-height column.
    recorded_min: float | None = None
    recorded_max: float | None = None
    su_type: str | None = None
    subtype: str | None = None
    subsubtype: str | None = None
    area: str | None = None
    square: str | None = None
    seed_source: SeedSource = SeedSource.FULL_HEIGHT
    #: False when the SU was only caught by the buffer, never crossing the trace.
    on_trace: bool = True
    include: bool = True             # toggled from the SU table

    @property
    def label(self) -> str:
        return f"SU {self.su_number}"

    @property
    def x_min(self) -> float:
        return min(s.x_min for s in self.spans) if self.spans else 0.0

    @property
    def x_max(self) -> float:
        return max(s.x_max for s in self.spans) if self.spans else 0.0

    @property
    def width(self) -> float:
        return self.x_max - self.x_min

    def describe_type(self) -> str:
        parts = [p for p in (self.su_type, self.subtype, self.subsubtype) if p]
        return " / ".join(parts) if parts else ""


# --------------------------------------------------------------- discovery --


def _transform_to_site(layer: QgsVectorLayer) -> QgsCoordinateTransform | None:
    """Transform from the layer's CRS into EPSG:32636, or None if already there.

    Chainage is a real-world distance, so the projection maths has to happen in
    the metric site CRS regardless of how the layer is stored.
    """
    site = QgsCoordinateReferenceSystem(SITE_CRS)
    if layer.crs() == site:
        return None
    return QgsCoordinateTransform(layer.crs(), site, QgsProject.instance())


def buffer_trace(
    geom: QgsGeometry, distance: float, *, left: bool = True
) -> QgsGeometry:
    """Buffer a trace on one side only.

    A section trace normally runs along a baulk with excavated space on both
    sides. A two-sided buffer would sweep in the SUs belonging to the far face
    as well, which is wrong: those units are not visible in this wall. So the
    buffer only extends behind the face being drawn.

    Kept in one place because the trace tool previews the same buffer and the
    preview must not lie about what will be caught.
    """
    side = Qgis.BufferSide.Left if left else Qgis.BufferSide.Right
    return geom.singleSidedBuffer(
        distance, 8, side, Qgis.JoinStyle.Round, 2.0
    )


def _trace_geometries(line: SectionLine) -> tuple[QgsGeometry, QgsGeometry]:
    """The section trace as a line, and its one-sided buffer, in site CRS."""
    trace = QgsGeometry.fromPolylineXY(
        [QgsPointXY(*line.p0), QgsPointXY(*line.p1)]
    )
    return trace, buffer_trace(trace, line.buffer, left=line.looks_left)


def _spans_for(
    geom: QgsGeometry, line: SectionLine, trace: QgsGeometry, buf: QgsGeometry
) -> list[Span]:
    """Chainage runs over which this polygon is present on the wall.

    Prefers the polygon's intersection with the trace itself, which gives the
    true horizontal extent of the SU on the wall face and correctly yields two
    runs when the wall crosses the same SU twice. Falls back to the buffer
    intersection for SUs that sit alongside the trace without touching it.
    """
    hit = geom.intersection(trace)
    on_trace = not hit.isEmpty()
    if not on_trace:
        hit = geom.intersection(buf)
        if hit.isEmpty():
            return []

    spans: list[Span] = []
    # Walk each part separately so a two-crossing SU stays two runs.
    parts = hit.asGeometryCollection() if hit.isMultipart() else [hit]
    for part in parts:
        chainages = [
            line.chainage(v.x(), v.y()) for v in part.vertices()
        ]
        if not chainages:
            continue
        spans.append(Span(min(chainages), max(chainages), on_trace))

    # Runs from adjacent parts that touch get merged; genuinely separate
    # crossings stay separate.
    return merge_spans(spans, gap=1e-6)


def _candidate_from_feature(
    feat: QgsFeature, fmap: FieldMap, spans: list[Span]
) -> SUCandidate:
    def attr(name: str | None):
        return feat[name] if name and name in feat.fields().names() else None

    number = attr(fmap.su_number)
    lo = _plausible(_num(attr(fmap.alt_min)))
    hi = _plausible(_num(attr(fmap.alt_max)))
    return SUCandidate(
        su_id=feat.id(),
        su_number=str(number) if number is not None else f"?{feat.id()}",
        spans=spans,
        alt_min=lo,
        alt_max=hi,
        # Captured here, at read time, and never written again. Deriving it
        # later from alt_* cannot distinguish "not captured yet" from
        # "captured, and the SU genuinely had no elevation".
        recorded_min=lo,
        recorded_max=hi,
        su_type=attr(fmap.su_type),
        subtype=attr(fmap.subtype),
        subsubtype=attr(fmap.subsubtype),
        area=attr(fmap.area),
        square=attr(fmap.square),
        on_trace=any(s.on_trace for s in spans),
    )


def candidates_from_features(features) -> list[SUCandidate]:
    """Rebuild the SU roster from saved section polygons.

    A reopened section has no SU layer to rediscover from -- and should not
    need one, or a drawing could not be revisited without the database to hand.
    Everything the panel shows is already on the polygons.
    """
    by_id: dict[int, SUCandidate] = {}
    for feat in features:
        try:
            su_id = int(feat["su_id"])
        except (TypeError, ValueError, KeyError):
            continue
        cand = by_id.get(su_id)
        if cand is None:
            cand = SUCandidate(
                su_id=su_id,
                su_number=str(feat["su_number"] or f"?{su_id}"),
                su_type=feat["su_type"],
                subtype=feat["subtype"],
            )
            names = feat.fields().names()
            if "subsubtype" in names:
                cand.subsubtype = feat["subsubtype"]
            if "seed_source" in names:
                try:
                    cand.seed_source = SeedSource(feat["seed_source"])
                except ValueError:
                    pass
            by_id[su_id] = cand

        # Chainage extent comes back from the geometry, which is the drawing as
        # it now stands rather than as it was first seeded.
        if feat.hasGeometry():
            box = feat.geometry().boundingBox()
            cand.spans.append(Span(box.xMinimum(), box.xMaximum()))
            lo, hi = box.yMinimum(), box.yMaximum()
            cand.alt_min = lo if cand.alt_min is None else min(cand.alt_min, lo)
            cand.alt_max = hi if cand.alt_max is None else max(cand.alt_max, hi)

    return sorted(by_id.values(), key=lambda c: (c.x_min, c.su_number))


def build_candidate(
    feat: QgsFeature, fmap: FieldMap, spans: list[Span]
) -> SUCandidate:
    """Make a candidate from a feature and a chainage extent you supply.

    Used when an SU is added to a session by hand and so has no extent derived
    from the trace -- the caller decides where on the wall to put it.
    """
    return _candidate_from_feature(feat, fmap, spans)


def discover_spatial(
    layer: QgsVectorLayer,
    line: SectionLine,
    *,
    restrict_to: Iterable[int] | None = None,
    clip_to: tuple[float, float] | None = None,
) -> list[SUCandidate]:
    """SUs meeting the buffered trace, with their chainage extents.

    ``restrict_to`` optionally limits the search to a set of feature ids, which
    is how the relational mode narrows to one space before measuring extents.

    ``clip_to`` is the chainage range a unit has to reach into to count,
    defaulting to the trace itself (0..length). The session passes the frame's
    range instead when re-deciding the roster after a resize, so cropping the
    frame drops the units it no longer covers and extending it picks up units
    that run past the end of the trace.
    """
    trace, buf = _trace_geometries(line)
    xform = _transform_to_site(layer)
    fmap = FieldMap.sniff(layer)

    # Request in the layer's own CRS, so push the search box back through the
    # transform rather than transforming every feature in the layer.
    search = QgsGeometry(buf)
    if xform is not None:
        search.transform(xform, QgsCoordinateTransform.ReverseTransform)

    req = QgsFeatureRequest().setFilterRect(search.boundingBox())
    if restrict_to is not None:
        ids = list(restrict_to)
        if not ids:
            return []
        req = QgsFeatureRequest().setFilterFids(ids)

    out: list[SUCandidate] = []
    for feat in layer.getFeatures(req):
        geom = feat.geometry()
        if geom is None or geom.isEmpty():
            continue
        if xform is not None:
            geom = QgsGeometry(geom)
            geom.transform(xform)
        lo, hi = clip_to if clip_to is not None else (0.0, line.length)
        spans = _spans_for(geom, line, trace, buf)
        spans = [c for c in (s.clipped_between(lo, hi) for s in spans) if c]
        if not spans:
            continue
        out.append(_candidate_from_feature(feat, fmap, spans))

    out.sort(key=lambda c: (c.x_min, c.su_number))
    return out


# ------------------------------------------------------- postgis relational --


def postgis_connection(layer: QgsVectorLayer):
    """A QgsAbstractDatabaseProviderConnection for this layer, or None.

    Returns None for any non-postgres layer, which is the signal to grey out the
    relational options in the UI.
    """
    if layer.providerType() != "postgres":
        return None
    try:
        md = QgsProviderRegistry.instance().providerMetadata("postgres")
        return md.createConnection(layer.source(), {})
    except Exception:
        return None


def list_spaces(layer: QgsVectorLayer) -> list[tuple[int, str]]:
    """(space_id, display) for spaces that actually have SUs linked."""
    conn = postgis_connection(layer)
    if conn is None:
        return []
    sql = """
        select s.id, s.space, count(ssu.stratigraphical_unit_id) as n
        from space s
        join space_stratigraphical_unit ssu on ssu.space_id = s.id
        group by s.id, s.space
        order by s.space
    """
    try:
        rows = conn.executeSql(sql)
    except Exception:
        return []
    return [(int(r[0]), f"Space {r[1]} ({r[2]} SUs)") for r in rows]


def su_ids_for_space(layer: QgsVectorLayer, space_id: int) -> list[int]:
    """``stratigraphical_unit.id`` values linked to a space.

    These are database ids. The caller still has to match them to layer feature
    ids -- see :func:`feature_ids_for_su_ids`.
    """
    conn = postgis_connection(layer)
    if conn is None:
        return []
    sql = (
        "select stratigraphical_unit_id from space_stratigraphical_unit "
        f"where space_id = {int(space_id)}"
    )
    try:
        return [int(r[0]) for r in conn.executeSql(sql)]
    except Exception:
        return []


def feature_ids_for_su_ids(
    layer: QgsVectorLayer, su_ids: Sequence[int]
) -> list[int]:
    """Map database ``id`` values to QGIS feature ids on this layer."""
    if not su_ids:
        return []
    joined = ",".join(str(int(i)) for i in su_ids)
    req = QgsFeatureRequest().setFilterExpression(f'"id" IN ({joined})')
    req.setSubsetOfAttributes([])
    req.setFlags(QgsFeatureRequest.NoGeometry)
    return [f.id() for f in layer.getFeatures(req)]


def strat_floors(
    layer: QgsVectorLayer, su_ids: Sequence[int]
) -> dict[int, float]:
    """For each SU, the highest ``altitude_max`` among the SUs it lies above.

    That value is the best available guess at where the SU bottoms out, and it
    is what powers the ``INFERRED`` rung of the seeding cascade. Covers far more
    SUs than ``altitude_min`` does -- 1270 SUs carry strat relations against 450
    with both altitudes.
    """
    conn = postgis_connection(layer)
    if conn is None or not su_ids:
        return {}
    joined = ",".join(str(int(i)) for i in su_ids)
    sql = f"""
        select r.su_from_id, max(su.altitude_max)
        from surelation r
        join topology_type t on t.id = r.relation_id
        join stratigraphical_unit su on su.id = r.su_to_id
        where r.su_from_id in ({joined})
          and lower(t.title) in ('above', 'cuts')
          and su.altitude_max is not null
        group by r.su_from_id
    """
    try:
        rows = conn.executeSql(sql)
    except Exception:
        return {}
    out: dict[int, float] = {}
    for su_id, z in rows:
        val = _plausible(_num(z))
        if val is not None:
            out[int(su_id)] = val
    return out


# ----------------------------------------------------------------- seeding --


#: How far a made-up column stops short of the section edge, as a fraction of
#: the section's height. A column drawn hard against the top and bottom of the
#: drawing has its edges sitting on the photo boundary, where they are awkward
#: to grab with the vertex tool and read as if they meant something. Inset, it
#: is obviously provisional and every edge is reachable.
COLUMN_INSET_FRACTION = 0.10

#: Never inset by more than this, so a tall section still gets a usable column.
MAX_COLUMN_INSET = 0.30


def column_bounds(line: SectionLine) -> tuple[float, float]:
    """Vertical extent of a made-up seed column: inset from the section edges."""
    height = line.z_max - line.z_min
    inset = min(height * COLUMN_INSET_FRACTION, MAX_COLUMN_INSET)
    return (line.z_min + inset, line.z_max - inset)


def apply_seed_cascade(
    candidates: Sequence[SUCandidate],
    line: SectionLine,
    *,
    strat_floor: dict[int, float] | None = None,
    use_recorded_elevations: bool = False,
    min_box_height: float = 0.05,
) -> None:
    """Set each candidate's vertical extent for seeding.

    By default every SU is seeded as a **full-height column** across the
    section, positioned only by its chainage extent along the wall. The
    excavator then drags the top and bottom to where the photo says they belong.
    This is the honest default: only 30% of SU polygons carry both altitudes,
    those that do are on mixed vertical datums, and a box drawn from bad
    elevations looks authoritative while being wrong.

    Setting ``use_recorded_elevations`` opts into the richer cascade for the
    minority of SUs with trustworthy data:

    1. both altitudes present and sane -> a measured box
    2. one altitude -> box down to the strat floor, or to the section edge
    3. nothing -> full-height column

    Mutates the candidates in place. ``line`` must already have its vertical
    extent set. ``alt_min``/``alt_max`` end up holding the *seed box*, so the
    values as recorded are kept in ``recorded_min``/``recorded_max`` for display.
    """
    if line.z_min is None or line.z_max is None:
        raise ValueError("Set the section's vertical extent before seeding")
    floors = strat_floor or {}

    # Where a made-up edge goes. Real measurements are used as recorded; only
    # invented ends get pulled inside the section.
    guess_lo, guess_hi = column_bounds(line)

    for c in candidates:
        # recorded_* was captured at read time and is never written here, so
        # this function is idempotent and safe to re-run when the user toggles
        # the mode.
        if not use_recorded_elevations:
            c.alt_min, c.alt_max = guess_lo, guess_hi
            c.seed_source = SeedSource.FULL_HEIGHT
            continue

        lo, hi = c.recorded_min, c.recorded_max

        if lo is not None and hi is not None:
            if hi < lo:
                lo, hi = hi, lo
            c.seed_source = SeedSource.MEASURED
        elif hi is not None:
            floor = floors.get(c.su_id)
            lo = floor if floor is not None and floor < hi else guess_lo
            c.seed_source = (
                SeedSource.INFERRED if floor is not None and floor < hi
                else SeedSource.HALF_MEASURED
            )
        elif lo is not None:
            hi = guess_hi
            c.seed_source = SeedSource.HALF_MEASURED
        else:
            lo, hi = guess_lo, guess_hi
            c.seed_source = SeedSource.FULL_HEIGHT

        # A single-shot SU (min == max) would seed as a zero-height sliver that
        # cannot be grabbed with the vertex tool. Give it something to hold.
        if hi - lo < min_box_height:
            hi = lo + min_box_height

        c.alt_min, c.alt_max = lo, hi
