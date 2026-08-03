# -*- coding: utf-8 -*-
"""
core.py

Pure logic for the "Survey Points to Polygons" QGIS tool, ported from the ArcGIS
Pro Python Toolbox (EmlidToSU.pyt). arcpy is replaced by PyQGIS (qgis.core);
this module holds everything that does not touch the Qt dialog, so it can be
read/tested independently of the UI.

Two kinds of outline point are read from the same export (see POINT_TYPES):

    SU_{field}.{su}-{seq}        stratigraphic unit outlines  -> the SU layer
    F_{field}.{feature}-{seq}    feature outlines             -> the Features layer

They are numbered independently — SU 55 and Feature 55 are unrelated — so each
kind is grouped, matched, and written against its own target layer, and a single
run can do both. Every other coded point in an Emlid export (E_ elevations, O_
finds, P_ pails, GCP_ control, S_ samples, SEC_ section refs) is ignored.

The tool offers two output modes:

  1. Replace geometry on an existing target layer (plan_geometry_updates +
     apply_geometry_updates). This ONLY replaces the geometry of polygon records
     that already exist in the target layer — it never inserts new rows and never
     edits attributes. Two matching behaviours:
       * replace_regardless=True  -> overwrite the geometry of any match,
                                     whether or not it already had a shape.
       * replace_regardless=False -> only fill records whose geometry is
                                     currently empty/NULL (the ArcGIS "Replace
                                     Empty" rule); a record that already has a
                                     shape is left alone.
     In both, a number that matches more than one target record is skipped as
     ambiguous, and one with no matching record is skipped (never inserted).

  2. Build polygons on a new temporary layer (create_polygon_layer): a simple
     "connect the dots" mode with no target and no matching — one temporary
     layer per point type, carrying just the number, vertex count, and how many
     crossings had to be undone.

Input can be one file, several files combined (read_rows_from_files, grouped
across all of them), or a layer already loaded in the project.

Matching is on the number alone. Point names may be either form:
    SU_{field}.{su}-{seq}   (proper form, e.g. SU_6.1355-1)
    SU_{su}-{seq}           (shorthand people sometimes use, e.g. SU_1355-1)
Both read as the same SU (the number before the increment); any leading field
number is ignored. The increment ({seq}) orders the outline points.

Outlines are uncrossed before they are built — see uncross_ring. Points shot out
of perimeter order produce a self-intersecting "bowtie" ring, and the recorded
order is reordered as little as possible to undo it.
"""

import os
import re

from qgis.core import (
    QgsVectorLayer,
    QgsFeature,
    QgsGeometry,
    QgsPointXY,
    QgsCoordinateReferenceSystem,
    QgsProject,
    QgsVectorFileWriter,
    NULL,
)

from ..common import (
    FEATURE_FIELD_POINTS_TARGET,
    SITE_CRS_EPSG,
    SU_FIELD_POINTS_TARGET,
    WGS84_EPSG,
)

# Outline point names carry a number and an increment: the increment is the LAST
# integer in the name and the number is the integer right before it. Anything
# before that (a leading field/trench number, as in SU_{field}.{su}-{seq}) is
# ignored — matching is on the SU or feature number alone. So SU_1355-1 and
# SU_6.1355-1 both read as SU 1355, increment 1.
#
# The match is deliberately lenient about separators and zero-padding: the parts
# may be divided by any run of non-alphanumeric characters (underscore, dot,
# dash, comma, slash, colon, space, ...), so the variants that really turn up in
# TKAP exports all parse — F_2.55-1, F-1.25-9 and F_1.19.9 are all read the same
# way, as are data-entry slips like SU_1355 1, SU_01355-1, SU#1355:1. What it
# does NOT tolerate is a letter sitting where only the number-and-separator
# structure belongs (e.g. SUB_5-1 or SU_13x55-1) — those are rejected so
# genuinely garbled names still fail rather than being silently misread. The
# name must contain 2 numbers (number, increment) or 3 (field, number,
# increment); see parse_point_name().
_NAME_INTS = re.compile(r'\d+')

# Emlid points are collected/exported in this CRS regardless of what the target
# layer happens to be in. Geometry is built here and then reprojected to the
# target's CRS if they differ.
#   WGS 1984 UTM Zone 36N / EPSG:32636
EMLID_WKID = SITE_CRS_EPSG

# Some Emlid exports leave the Easting/Northing columns blank (no local grid set
# in the project) and only populate Longitude/Latitude instead. Those are
# geographic coordinates in this CRS and get reprojected into EMLID_WKID
# per-point so downstream code can keep treating every point uniformly as
# already being in the Emlid UTM CRS.
#   WGS 1984 / EPSG:4326
WGS84_WKID = WGS84_EPSG


class PointType(object):
    """
    One kind of outline point: which name prefix claims it, what it is called,
    and which attribute field on the target layer holds its number.

    ``prefix`` is matched case-insensitively and must be followed by something
    that is not a letter, so SU claims SU_1355-1 but not SURFACE_1, and F claims
    F_2.55-1 but not Field_2. The two prefixes here cannot both claim a name.
    """

    def __init__(self, key, label, prefix, tag, default_field):
        self.key = key                      # internal id, also the groups key
        self.label = label                  # how it reads in messages and the UI
        self.prefix = prefix                # the name prefix that claims a point
        self.tag = tag                      # short form used in QC layer names
        self.default_field = default_field  # preselected target field
        self.claim_re = re.compile(
            r'^{}(?![A-Za-z])'.format(prefix), re.IGNORECASE
        )
        # ^SU(?![A-Za-z]) : starts with the prefix, not followed by a letter
        # [^A-Za-z]+$     : the remainder is digits + non-letter separators only
        self.name_re = re.compile(
            r'^{}(?![A-Za-z])[^A-Za-z]+$'.format(prefix), re.IGNORECASE
        )

    def __repr__(self):
        return "PointType({})".format(self.key)


SU_TYPE = PointType(
    key="su",
    label="SU",
    prefix="SU",
    tag="SU",
    default_field=SU_FIELD_POINTS_TARGET,
)
FEATURE_TYPE = PointType(
    key="feature",
    label="Feature",
    prefix="F",
    tag="F",
    default_field=FEATURE_FIELD_POINTS_TARGET,
)

#: Order matters only for how a run is reported; the prefixes are disjoint.
POINT_TYPES = (SU_TYPE, FEATURE_TYPE)
POINT_TYPES_BY_KEY = {t.key: t for t in POINT_TYPES}


class Messages(object):
    """
    Minimal message sink mirroring the ArcGIS `messages` object
    (addMessage/addWarningMessage/addErrorMessage). Collects everything into
    lists; the dialog subclasses this to also echo each line into its log pane.
    """

    def __init__(self):
        self.messages = []
        self.warnings = []
        self.errors = []

    def addMessage(self, text):
        self.messages.append(text)
        self._emit("INFO", text)

    def addWarningMessage(self, text):
        self.warnings.append(text)
        self._emit("WARNING", text)

    def addErrorMessage(self, text):
        self.errors.append(text)
        self._emit("ERROR", text)

    def _emit(self, level, text):
        """Override to surface a line somewhere (dialog log, console, ...)."""
        pass


def emlid_crs():
    return QgsCoordinateReferenceSystem.fromEpsgId(EMLID_WKID)


def wgs84_crs():
    return QgsCoordinateReferenceSystem.fromEpsgId(WGS84_WKID)


def normalize_number_value(value):
    """
    Normalize an SU/feature number to a stable string key for matching.

    QGIS fields are typed, so the same number can arrive as an int (322), a
    double (322.0), or text ("322") depending on the target layer's schema.
    Collapsing integral numerics to their plain integer string ("322") lets a
    parsed name ("322") match regardless of how the target stores it. Non-
    numeric values are compared as trimmed strings.
    """
    if value is None:
        return None
    text = str(value).strip()
    if text == "" or text.upper() == "NULL":
        return None
    try:
        f = float(text)
        if f.is_integer():
            return str(int(f))
    except (TypeError, ValueError):
        pass
    return text


def qc_fc_name(prefix, point_type, number):
    """
    Build a valid layer/file name (letters/digits/underscore only) for one
    outline's QC layer, e.g. QC_SU_1355 or QC_F_55. The type tag is in the name
    because SU 55 and Feature 55 are different things and would otherwise
    collide.
    """
    safe = re.sub(r'[^0-9A-Za-z]+', '_', str(number))
    name = "{}_{}_{}".format(prefix, point_type.tag, safe)
    if name and name[0].isdigit():
        name = "_" + name
    return name


def _clean_float(value):
    """Parse value as a float, treating None/blank/whitespace/'NULL' as missing (None)."""
    if value is None:
        return None
    text = str(value).strip()
    if not text or text.upper() == "NULL":
        return None
    try:
        return float(text)
    except ValueError:
        return None


def load_table_as_layer(path):
    """
    Load a CSV or Excel Emlid export as a (non-spatial) QgsVectorLayer via
    GDAL/OGR, so both formats are read with the libraries QGIS already bundles
    — no pandas/openpyxl dependency. Falls back to the delimited-text provider
    for CSV if the OGR CSV driver refuses the file.
    """
    if not path:
        raise ValueError("No input file was provided.")
    if not os.path.exists(path):
        raise ValueError("Input file not found: {}".format(path))

    ext = os.path.splitext(path)[1].lower()
    layer = QgsVectorLayer(path, os.path.basename(path), "ogr")

    if not layer.isValid() and ext == ".csv":
        uri = "file:///{}?type=csv&detectTypes=yes&geomType=none".format(
            path.replace("\\", "/")
        )
        layer = QgsVectorLayer(uri, os.path.basename(path), "delimitedtext")

    if not layer.isValid():
        raise ValueError(
            "Could not read input table '{}'. Expected a readable CSV or "
            "Excel (.xlsx/.xls) file.".format(path)
        )
    return layer


def read_rows_from_layer(vlayer):
    """
    Read a QgsVectorLayer (a loaded point/table layer, or one produced by
    load_table_as_layer) into (rows, columns): a list of {column: value} dicts
    and the list of column names. Attribute values come back as native Python
    types; NULLs surface as None or the string 'NULL' and are handled by the
    float/name parsing downstream.
    """
    if vlayer is None or not vlayer.isValid():
        raise ValueError(
            "The selected survey point source could not be read (invalid or "
            "empty layer)."
        )
    columns = [f.name() for f in vlayer.fields()]
    rows = []
    for feat in vlayer.getFeatures():
        row = {}
        for name in columns:
            val = feat[name]
            # QGIS represents a NULL attribute with the NULL sentinel; normalize
            # it (and Python None) to None so dict.get()/str() behave predictably
            # in the parsing helpers.
            row[name] = None if (val is None or val == NULL) else val
        rows.append(row)
    return rows, columns


def read_rows_from_files(paths, messages):
    """
    Read one or more CSV/Excel Emlid exports and combine them into a single
    (rows, columns) result. Rows are concatenated in file order; columns are the
    union of every file's columns (in first-seen order). Points are later grouped
    by number across all files, so the same SU shot across two files ends up in
    one group. Per-file row counts are reported.
    """
    all_rows = []
    columns = []
    seen = set()
    for path in paths:
        layer = load_table_as_layer(path)
        rows, cols = read_rows_from_layer(layer)
        for c in cols:
            if c not in seen:
                seen.add(c)
                columns.append(c)
        all_rows.extend(rows)
        messages.addMessage(
            "Read {} row(s) from {}".format(len(rows), os.path.basename(path))
        )
    return all_rows, columns


def resolve_row_xy(row, has_en, has_ll, ct_wgs_to_emlid):
    """
    Resolve a row's coordinates to (easting, northing) in the Emlid CRS.

    Prefers Longitude/Latitude (geographic, WGS84) reprojected into the Emlid
    CRS, since that's the CRS Emlid always populates regardless of whether a
    local grid is set. Falls back to Easting/Northing (already in the Emlid
    CRS) when Longitude/Latitude are missing/blank. Returns None if neither
    pair yields usable coordinates.
    """
    if has_ll:
        longitude = _clean_float(row.get("Longitude"))
        latitude = _clean_float(row.get("Latitude"))
        if longitude is not None and latitude is not None:
            p = ct_wgs_to_emlid.transform(QgsPointXY(longitude, latitude))
            return p.x(), p.y()

    if has_en:
        easting = _clean_float(row.get("Easting"))
        northing = _clean_float(row.get("Northing"))
        if easting is not None and northing is not None:
            return easting, northing

    return None


def classify_point_name(name):
    """
    Return the PointType whose prefix claims this name, or None.

    Claiming is not the same as parsing: SU_1355 is claimed by SU_TYPE and then
    rejected by parse_point_name for having no increment. That split is what
    lets the caller warn "this looks like an SU point but isn't an outline
    vertex" instead of silently lumping it in with the O_/E_/GCP_ points it
    ignores without comment.
    """
    for point_type in POINT_TYPES:
        if point_type.claim_re.match(name):
            return point_type
    return None


def parse_point_name(name, point_type):
    """
    Parse an outline point name into (number, seq).

    number is returned as an unpadded string (used as a text key / value
    downstream), seq as an int. The increment is the last integer in the name
    and the number is the integer before it; any earlier integer (a leading
    field/trench number) is ignored. Returns None when the name can't be
    resolved to 2 integers (number + increment) or 3 (field + number +
    increment), or when it contains letters where only the number/separator
    structure belongs.
    """
    if not point_type.name_re.match(name):
        return None
    nums = _NAME_INTS.findall(name)
    if len(nums) not in (2, 3):
        return None
    number = str(int(nums[-2]))   # second-to-last integer = SU / feature number
    seq = int(nums[-1])           # last integer = increment
    return number, seq


def parse_point_groups(rows, columns, messages, ct_wgs_to_emlid):
    """
    Filter to outline points only, and group them by type and number:
        {type_key: {number: [(seq, easting, northing, name), ...]}}  sorted by seq
    Also returns a flat list of every valid outline point (for the optional QC
    layers) as [(type_key, number, seq, easting, northing, name), ...].

    Only the number and the increment (seq) matter: each point name gives a
    number and an increment (see parse_point_name), points are grouped and
    matched by number alone within their type, and connected in increment order.

    Resilient to the mixed content of real exports: points whose prefix is
    claimed but that aren't outline vertices (a single labeled shot with no
    increment, or an unreadable name) are skipped with a warning, and an outline
    whose points repeat an increment (ambiguous order) is dropped with a warning
    — neither aborts the run, so the good outlines still go through. Only a
    genuinely unusable input table (missing Name / coordinate columns) raises.
    """
    columns = set(columns)
    has_en = {"Easting", "Northing"}.issubset(columns)
    has_ll = {"Longitude", "Latitude"}.issubset(columns)
    if "Name" not in columns or not (has_en or has_ll):
        raise ValueError(
            "Input table is missing required column(s). Expected an Emlid export "
            "with a Name column and either Easting/Northing or Longitude/Latitude "
            "columns. Found columns: {}".format(", ".join(sorted(columns)))
        )

    groups = {t.key: {} for t in POINT_TYPES}
    all_points = []
    dropped_other = 0
    skipped_no_coords = 0
    skipped_names = {t.key: [] for t in POINT_TYPES}

    for row in rows:
        raw = row.get("Name")
        if raw is None:
            dropped_other += 1
            continue
        name = str(raw).strip()

        point_type = classify_point_name(name)
        if point_type is None:
            # Not an outline point at all: E_ elevations, O_ finds, P_ pails,
            # GCP_ control, S_ samples, SEC_ section refs, free text.
            dropped_other += 1
            continue

        parsed = parse_point_name(name, point_type)
        if parsed is None:
            # Carries an outline prefix but isn't an outline vertex — e.g. a
            # single labeled shot with no increment (SU-2623, an elevation
            # point), or a name that can't be read as number + increment. Skip
            # it and keep going rather than aborting the whole run: real exports
            # routinely mix such points in with the outline points.
            skipped_names[point_type.key].append(name)
            continue
        number, seq = parsed

        xy = resolve_row_xy(row, has_en, has_ll, ct_wgs_to_emlid)
        if xy is None:
            skipped_no_coords += 1
            messages.addWarningMessage(
                "Skipped point '{}': no usable Easting/Northing or Longitude/Latitude "
                "coordinates.".format(name)
            )
            continue
        easting, northing = xy

        groups[point_type.key].setdefault(number, []).append(
            (seq, easting, northing, name)
        )
        all_points.append((point_type.key, number, seq, easting, northing, name))

    for point_type in POINT_TYPES:
        names = skipped_names[point_type.key]
        if names:
            messages.addWarningMessage(
                "Skipped {} {}_-prefixed point(s) that aren't outline vertices (no "
                "increment, or an unreadable name): {}. Only names giving a number "
                "and an increment are used to build outlines.".format(
                    len(names),
                    point_type.prefix,
                    ", ".join("'{}'".format(n) for n in names),
                )
            )

    # Sort each group's points by increment and sanity-check the sequence. A
    # duplicate increment within one outline makes its order ambiguous, so that
    # outline is dropped (with a warning) rather than building a wrong polygon —
    # but the others still go through.
    for point_type in POINT_TYPES:
        by_number = groups[point_type.key]
        for number in list(by_number.keys()):
            pts = by_number[number]
            pts.sort(key=lambda p: p[0])
            seqs = [p[0] for p in pts]
            if len(set(seqs)) != len(seqs):
                repeated = sorted({s for s in seqs if seqs.count(s) > 1})
                messages.addWarningMessage(
                    "{} {} repeats increment number(s) {} (full sequence: {}) — the "
                    "outline order is ambiguous, so it was skipped. Fix the point "
                    "names and rerun to include it.".format(
                        point_type.label, number, repeated, seqs
                    )
                )
                del by_number[number]
                continue
            expected = list(range(1, len(seqs) + 1))
            if seqs != expected:
                messages.addWarningMessage(
                    "{} {} increment numbers are {} (expected {}). "
                    "Points will still be connected in the order given, but check for "
                    "typos/missing points.".format(
                        point_type.label, number, seqs, expected
                    )
                )

    messages.addMessage(
        "Parsed {} outline point(s) into {}. Dropped {} point(s) of other types. "
        "Skipped {} non-outline name(s), {} point(s) with no usable "
        "coordinates.".format(
            len(all_points),
            ", ".join(
                "{} {} group(s)".format(len(groups[t.key]), t.label)
                for t in POINT_TYPES
            ),
            dropped_other,
            sum(len(v) for v in skipped_names.values()),
            skipped_no_coords,
        )
    )

    return groups, all_points


# -- Uncrossing: the bowtie check ------------------------------------------
#
# Points recorded out of perimeter order make the ring cross itself. The fix is
# 2-opt: find two edges that cross, reverse the sub-path between them, repeat.
# That swaps edges (a,b) and (c,d) for (a,c) and (b,d), which is exactly the
# uncrossing move, and because two crossing chords are always longer than the
# two that replace them (triangle inequality, strictly so for a genuine
# crossing) every move shortens the ring. A strictly decreasing quantity cannot
# revisit an ordering, so the loop terminates, and it can only stop at a ring
# with no crossings left.
#
# 2-opt is preferred over sorting the points by angle around their centroid
# because it changes the recorded order as little as possible: an outline that
# was shot correctly comes out untouched, and a concave one stays concave
# instead of being rounded out into its star-shaped hull.

#: Below this, three points count as collinear. Coordinates are metres in UTM
#: 36N and shots are ~0.5 m apart, so a triangle of 1e-9 m² is noise.
_CROSS_EPS = 1e-9

#: Belt and braces against a float-precision cycle that the termination argument
#: above says cannot happen. Real outlines here run to a few dozen points.
MAX_UNCROSS_PASSES = 500


def _side(a, b, c):
    """Which side of the line a->b point c is on: 1 left, -1 right, 0 on it."""
    v = (b[0] - a[0]) * (c[1] - a[1]) - (b[1] - a[1]) * (c[0] - a[0])
    if v > _CROSS_EPS:
        return 1
    if v < -_CROSS_EPS:
        return -1
    return 0


def segments_cross(p1, p2, p3, p4):
    """
    True when the two segments *properly* cross — they meet at a point interior
    to both, each straddling the line through the other.

    Deliberately strict. Touching at a shared endpoint, a duplicate shot, or
    three collinear points along one wall are not bowties; counting them as
    crossings would have the loop below reordering points to no effect, which is
    the one way it could fail to terminate.
    """
    return (
        _side(p1, p2, p3) * _side(p1, p2, p4) < 0
        and _side(p3, p4, p1) * _side(p3, p4, p2) < 0
    )


def _first_crossing(xy, order):
    """First pair of crossing edge indices (i, j), i < j, or None if simple."""
    n = len(order)
    for i in range(n):
        a1 = xy[order[i]]
        a2 = xy[order[(i + 1) % n]]
        # j from i+2: consecutive edges share a vertex and cannot properly cross.
        for j in range(i + 2, n):
            # Edge 0 and the closing edge n-1 are adjacent too — they share the
            # first point — so that one pair is skipped as well.
            if i == 0 and j == n - 1:
                continue
            b1 = xy[order[j]]
            b2 = xy[order[(j + 1) % n]]
            if segments_cross(a1, a2, b1, b2):
                return i, j
    return None


def uncross_ring(xy):
    """
    Reorder points so the closed ring through them does not cross itself.

    Takes a list of (x, y) and returns (order, fixes, resolved): ``order`` is a
    permutation of indices into ``xy``, ``fixes`` how many crossings were undone,
    and ``resolved`` whether the result is crossing-free. A ring of three points
    or fewer cannot cross, and an outline already shot in perimeter order comes
    back with fixes == 0 and its original order untouched.
    """
    n = len(xy)
    order = list(range(n))
    if n < 4:
        return order, 0, True

    fixes = 0
    for _ in range(MAX_UNCROSS_PASSES):
        hit = _first_crossing(xy, order)
        if hit is None:
            return order, fixes, True
        i, j = hit
        order[i + 1:j + 1] = reversed(order[i + 1:j + 1])
        fixes += 1
    return order, fixes, False


def uncross_group(pts, point_type, number, messages):
    """
    Uncross one outline's points, reporting what changed. Takes and returns the
    (seq, easting, northing, name) tuples, so the caller's grouping and the QC
    layers keep working on the same shape of data.

    Returns (pts, fixes). If the crossings could not all be undone the points
    come back untouched, in the order they were recorded: a partly-untangled
    outline is a shape nobody drew, and the warning is more use than the guess.
    """
    xy = [(easting, northing) for (_, easting, northing, _) in pts]
    order, fixes, resolved = uncross_ring(xy)
    if fixes == 0:
        return pts, 0

    if not resolved:
        messages.addWarningMessage(
            "{} {}: the outline still crosses itself after {} reordering passes, so "
            "it was built in the order recorded ({}). Check the increment numbers on "
            "these points.".format(
                point_type.label,
                number,
                MAX_UNCROSS_PASSES,
                ", ".join(str(p[0]) for p in pts),
            )
        )
        return pts, 0

    reordered = [pts[k] for k in order]
    messages.addWarningMessage(
        "{} {}: the outline crossed itself; reordered the points to uncross it "
        "({} fix(es)). Increments are now connected {} instead of {} — check the "
        "result against the shots before saving.".format(
            point_type.label,
            number,
            fixes,
            ", ".join(str(p[0]) for p in reordered),
            ", ".join(str(p[0]) for p in pts),
        )
    )
    return reordered, fixes


def uncross_groups(groups, messages):
    """
    Uncross every outline in a parsed ``groups`` structure, in place.

    Returns (fixes_by_key, stats): ``fixes_by_key`` maps (type_key, number) to
    the number of crossings undone for that outline, so temporary-layer mode can
    record it as an attribute; ``stats`` counts the outlines touched.
    """
    fixes_by_key = {}
    stats = {"uncrossed": 0, "fixes": 0}

    for point_type in POINT_TYPES:
        by_number = groups.get(point_type.key, {})
        for number in list(by_number.keys()):
            pts, fixes = uncross_group(
                by_number[number], point_type, number, messages
            )
            by_number[number] = pts
            fixes_by_key[(point_type.key, number)] = fixes
            if fixes:
                stats["uncrossed"] += 1
                stats["fixes"] += fixes

    if stats["uncrossed"]:
        messages.addMessage(
            "Uncrossed {} self-intersecting outline(s); {} crossing(s) undone in "
            "total.".format(stats["uncrossed"], stats["fixes"])
        )
    else:
        messages.addMessage("Crossover check: no self-intersecting outlines found.")
    return fixes_by_key, stats


# -- Building and writing the polygons --------------------------------------


def _geometry_is_empty(feat):
    """True when a feature has no usable polygon geometry (NULL or empty)."""
    if not feat.hasGeometry():
        return True
    geom = feat.geometry()
    return geom is None or geom.isNull() or geom.isEmpty()


def build_existing_index(target_layer, number_field):
    """
    Index the target layer's existing records by their SU/feature number:
        { number: [(feature_id, is_empty_geometry), ...] }
    Values are normalized (see normalize_number_value) so matching is robust to
    int/double/text field types.
    """
    existing = {}
    for feat in target_layer.getFeatures():
        value = normalize_number_value(feat[number_field])
        is_empty = _geometry_is_empty(feat)
        existing.setdefault(value, []).append((feat.id(), is_empty))
    return existing


def build_polygon(pts, source_crs, ct_to_target):
    """
    Build a closed polygon geometry from an outline's ordered points, in
    source_crs, reprojected to the target CRS when ct_to_target is provided
    (else left as-is because the target already matches the Emlid CRS).
    """
    ring = [QgsPointXY(e, n) for (_, e, n, _) in pts]
    if ring[0] != ring[-1]:
        ring.append(ring[0])
    geom = QgsGeometry.fromPolygonXY([ring])
    if ct_to_target is not None:
        geom.transform(ct_to_target)
    return geom


def create_polygon_layer(point_type, by_number, fixes_by_key, source_crs,
                         layer_name, messages):
    """
    Build one point type's polygons onto a brand-new temporary (in-memory) layer
    and add it to the project — a "connect the dots" mode with no target layer
    and no matching. One feature per group with >= 3 points, carrying the number,
    the vertex count, and how many crossings had to be undone, and nothing else.
    Geometry stays in the Emlid CRS (points are not reprojected, since there is
    no target).

    Returns (layer, stats) with stats = {"created": n, "too_few": n}.
    """
    authid = source_crs.authid() or "EPSG:{}".format(EMLID_WKID)
    uri = (
        "Polygon?crs={}"
        "&field={}:string(50)"
        "&field=VERTICES:integer"
        "&field=UNCROSSED:integer".format(authid, point_type.default_field)
    )
    layer = QgsVectorLayer(uri, layer_name, "memory")
    provider = layer.dataProvider()

    feats = []
    too_few = 0
    for number, pts in by_number.items():
        if len(pts) < 3:
            messages.addWarningMessage(
                "{} {} only has {} point(s) — at least 3 are needed to form a "
                "polygon. Skipped.".format(point_type.label, number, len(pts))
            )
            too_few += 1
            continue
        f = QgsFeature(layer.fields())
        f.setGeometry(build_polygon(pts, source_crs, None))
        f.setAttributes([
            str(number),
            len(pts),
            fixes_by_key.get((point_type.key, number), 0),
        ])
        feats.append(f)

    provider.addFeatures(feats)
    layer.updateExtents()
    QgsProject.instance().addMapLayer(layer)
    messages.addMessage(
        "Created temporary polygon layer '{}' with {} {} polygon(s). "
        "{} group(s) skipped for having fewer than 3 points.".format(
            layer.name(), len(feats), point_type.label, too_few
        )
    )
    return layer, {"created": len(feats), "too_few": too_few}


def plan_geometry_updates(point_type, by_number, existing, replace_regardless,
                          source_crs, ct_to_target, messages):
    """
    Decide which existing target records get their geometry replaced, matched
    by SU/feature number alone.

    Returns (updates, stats) where updates is [(feature_id, geometry), ...] and
    stats is a dict of skip counts. Never inserts and never edits attributes:
    a number with no matching record is skipped, and one matching more than one
    candidate record is skipped as ambiguous.
    """
    updates = []
    stats = {
        "too_few": 0,
        "no_match": 0,
        "has_geometry": 0,
        "ambiguous": 0,
        "to_update": 0,
    }

    for number, pts in by_number.items():
        if len(pts) < 3:
            messages.addWarningMessage(
                "{} {} only has {} point(s) — at least 3 are needed to form a "
                "polygon. Skipped.".format(point_type.label, number, len(pts))
            )
            stats["too_few"] += 1
            continue

        geom = build_polygon(pts, source_crs, ct_to_target)

        matches = existing.get(normalize_number_value(number), [])

        if not matches:
            messages.addWarningMessage(
                "{} {} has no matching record in the target layer. Skipped (this "
                "tool only replaces geometry on existing records; it never adds "
                "new ones).".format(point_type.label, number)
            )
            stats["no_match"] += 1
            continue

        if replace_regardless:
            if len(matches) > 1:
                messages.addWarningMessage(
                    "{} {} matches {} records in the target layer — ambiguous which "
                    "to replace, skipped. Resolve the duplicate record(s) before "
                    "rerunning.".format(point_type.label, number, len(matches))
                )
                stats["ambiguous"] += 1
                continue
            updates.append((matches[0][0], geom))
            stats["to_update"] += 1
        else:
            empty_matches = [(fid, is_empty) for fid, is_empty in matches if is_empty]
            if not empty_matches:
                messages.addWarningMessage(
                    "{} {} already has geometry in the target layer ({} matching "
                    "record(s), none empty). Not overwritten in 'only fill empty "
                    "geometry' mode — switch to 'replace regardless' if you intend "
                    "to replace it.".format(point_type.label, number, len(matches))
                )
                stats["has_geometry"] += 1
                continue
            if len(empty_matches) > 1:
                messages.addWarningMessage(
                    "{} {} matches {} empty-geometry record(s) — ambiguous which to "
                    "fill in, skipped. Resolve the duplicate record(s) before "
                    "rerunning.".format(point_type.label, number, len(empty_matches))
                )
                stats["ambiguous"] += 1
                continue
            updates.append((empty_matches[0][0], geom))
            stats["to_update"] += 1

    return updates, stats


def apply_geometry_updates(target_layer, updates, messages, commit=False):
    """
    Write geometry-only changes onto existing target features by feature id,
    leaving every attribute untouched.

    By default (commit=False) the changes are STAGED in the layer's edit buffer
    and NOT written to disk: the target layer is left in an edit session showing
    the new geometry on the canvas so it can be reviewed first. The user then
    keeps them with "Save Layer Edits", or discards them by toggling editing off
    without saving (or Undo). Pass commit=True to write straight to disk.

    If the target layer was already in an edit session before this ran, changes
    are added to that existing buffer and never force-committed, regardless of
    the commit flag — the user's in-progress edits are theirs to save.
    """
    if not updates:
        messages.addMessage(
            "No existing records matched in '{}' — no geometry was replaced.".format(
                target_layer.name()
            )
        )
        return 0

    already_editing = target_layer.isEditable()
    if not already_editing:
        if not target_layer.startEditing():
            raise RuntimeError(
                "Could not start an edit session on target layer '{}'. Is it "
                "editable/writable?".format(target_layer.name())
            )

    failed = []
    for fid, geom in updates:
        if not target_layer.changeGeometry(fid, geom):
            failed.append(fid)

    target_layer.triggerRepaint()
    succeeded = len(updates) - len(failed)
    if failed:
        messages.addWarningMessage(
            "Geometry replacement failed for {} feature id(s): {}".format(
                len(failed), failed
            )
        )

    if commit and not already_editing:
        if not target_layer.commitChanges():
            errs = "; ".join(target_layer.commitErrors())
            target_layer.rollBack()
            raise RuntimeError("Failed to commit geometry changes: {}".format(errs))
        messages.addMessage(
            "Replaced and saved geometry on {} existing record(s) in '{}'.".format(
                succeeded, target_layer.name()
            )
        )
    else:
        if already_editing and commit:
            messages.addWarningMessage(
                "Target layer '{}' was already in an edit session, so the new "
                "geometry was added to that buffer instead of being saved "
                "automatically.".format(target_layer.name())
            )
        messages.addMessage(
            "Staged geometry replacement on {} existing record(s) in '{}' — NOTHING "
            "is saved to disk yet. Review the new outlines on the map, then click "
            "'Save Layer Edits' (the disk icon on the Digitizing toolbar) to keep "
            "them, or toggle editing off without saving / press Undo to "
            "discard.".format(succeeded, target_layer.name())
        )
    return succeeded


def _save_qc_layer_to_disk(mem_layer, out_path, messages):
    """
    Try to write a QC memory layer to a shapefile at out_path and return the
    reloaded on-disk layer. On any failure, warn and return None so the caller
    falls back to keeping the in-memory (temporary) layer.
    """
    try:
        options = QgsVectorFileWriter.SaveVectorOptions()
        options.driverName = "ESRI Shapefile"
        options.fileEncoding = "UTF-8"
        result = QgsVectorFileWriter.writeAsVectorFormatV3(
            mem_layer, out_path, QgsProject.instance().transformContext(), options
        )
        # writeAsVectorFormatV3 returns (errorCode, errorMessage[, ...]).
        error_code = result[0] if isinstance(result, (tuple, list)) else result
        if error_code != QgsVectorFileWriter.NoError:
            raise RuntimeError(result[1] if isinstance(result, (tuple, list)) else "write error")
    except Exception as exc:  # noqa: BLE001 - any writer/API failure falls back to memory
        messages.addWarningMessage(
            "Could not save QC layer to '{}' ({}). Keeping it as a temporary "
            "in-memory layer instead.".format(out_path, exc)
        )
        return None

    saved = QgsVectorLayer(out_path, os.path.splitext(os.path.basename(out_path))[0], "ogr")
    if not saved.isValid():
        messages.addWarningMessage(
            "Saved QC layer '{}' but could not reload it; keeping the temporary "
            "in-memory layer instead.".format(out_path)
        )
        return None
    return saved


def create_qc_layers(all_points, source_crs, prefix, out_dir, messages,
                     type_keys=None):
    """
    Build one QC point layer per outline from the raw parsed vertices, for
    visually sanity-checking the polygon against the original shots.

    The points are the ones as recorded: QC layers are built from ``all_points``,
    which uncrossing never touches, so they still show the order the shots came
    in and a reordered outline can be checked against them.

    Layers are added to the current project. When out_dir is given, each is also
    saved there as a shapefile (falling back to an in-memory layer if the save
    fails); otherwise they are temporary in-memory layers. ``type_keys`` limits
    which point types get QC layers; None means all of them.
    """
    qc_groups = {}
    for type_key, number, seq, easting, northing, name in all_points:
        if type_keys is not None and type_key not in type_keys:
            continue
        qc_groups.setdefault((type_key, number), []).append(
            (seq, easting, northing, name)
        )

    created = []
    authid = source_crs.authid() or "EPSG:{}".format(EMLID_WKID)
    for (type_key, number), pts in qc_groups.items():
        point_type = POINT_TYPES_BY_KEY[type_key]
        pts.sort(key=lambda p: p[0])
        layer_name = qc_fc_name(prefix, point_type, number)
        uri = (
            "Point?crs={}"
            "&field=NAME:string(100)"
            "&field=TYPE:string(20)"
            "&field=NUMBER:string(50)"
            "&field=SEQ:integer".format(authid)
        )
        mem = QgsVectorLayer(uri, layer_name, "memory")
        provider = mem.dataProvider()
        feats = []
        for seq, easting, northing, name in pts:
            f = QgsFeature(mem.fields())
            f.setGeometry(QgsGeometry.fromPointXY(QgsPointXY(easting, northing)))
            f.setAttributes([name, point_type.label, str(number), int(seq)])
            feats.append(f)
        provider.addFeatures(feats)
        mem.updateExtents()

        layer_to_add = mem
        if out_dir:
            saved = _save_qc_layer_to_disk(
                mem, os.path.join(out_dir, layer_name + ".shp"), messages
            )
            if saved is not None:
                layer_to_add = saved

        QgsProject.instance().addMapLayer(layer_to_add)
        created.append(layer_name)

    if created:
        messages.addMessage(
            "Created {} QC point layer(s): {}".format(len(created), ", ".join(created))
        )
    return created
