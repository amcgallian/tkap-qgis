# -*- coding: utf-8 -*-
"""
core.py

Pure logic for the "Emlid to SU" QGIS plugin, ported from the ArcGIS Pro
Python Toolbox (EmlidToSU.pyt). arcpy is replaced by PyQGIS (qgis.core);
this module holds everything that does not touch the Qt dialog, so it can be
read/tested independently of the UI.

The plugin offers two output modes:

  1. Replace geometry on an existing SU layer (plan_geometry_updates +
     apply_geometry_updates). This ONLY replaces the geometry of SU polygon
     records that already exist in the target layer — it never inserts new SU
     rows and never edits attributes. Two matching behaviours:
       * replace_regardless=True  -> overwrite the geometry of any matching SU,
                                     whether or not it already had a shape.
       * replace_regardless=False -> only fill records whose geometry is
                                     currently empty/NULL (the ArcGIS "Replace
                                     Empty" rule); a record that already has a
                                     shape is left alone.
     In both, an SU that matches more than one target record is skipped as
     ambiguous, and an SU with no matching record is skipped (never inserted).

  2. Build polygons on a new temporary layer (create_polygon_layer): a simple
     "connect the dots" mode with no target and no matching — the SU polygons
     are written to a fresh in-memory layer carrying just the SU number and
     vertex count.

Input can be one file, several files combined (read_rows_from_files, grouped by
SU across all of them), or a layer already loaded in the project.

Matching is on the SU number alone. Point names in the "Name" column may be
either form:
    SU_{field}.{su}-{seq}   (proper form, e.g. SU_6.1355-1)
    SU_{su}-{seq}           (shorthand people sometimes use, e.g. SU_1355-1)
Both read as the same SU (the number before the increment); any leading field
number is ignored. The increment ({seq}) orders the outline points.
"""

import os
import re

from qgis.core import (
    QgsVectorLayer,
    QgsFeature,
    QgsGeometry,
    QgsPointXY,
    QgsCoordinateReferenceSystem,
    QgsCoordinateTransform,
    QgsProject,
    QgsVectorFileWriter,
    NULL,
)

from ..common import SITE_CRS_EPSG, SU_FIELD_EMLID_TARGET, WGS84_EPSG

# SU point names carry an SU number and an increment: the increment is the LAST
# integer in the name and the SU is the integer right before it. Anything before
# that (a legacy leading field/trench number, as in the old SU_{field}.{su}-{seq}
# form) is ignored — matching is on SU number alone. So SU_1355-1 and the older
# SU_6.1355-1 both read as SU 1355, increment 1.
#
# The match is deliberately lenient about separators and zero-padding: the parts
# may be divided by any run of non-alphanumeric characters (underscore, dot,
# dash, comma, slash, colon, space, ...), so data-entry slips like SU_1355 1,
# SU-1355-1, SU_01355-1, SU#1355:1 still parse. What it does NOT tolerate is a
# letter sitting where only the number-and-separator structure belongs (e.g.
# SUB_5-1 or SU_13x55-1) — those are rejected so genuinely garbled names still
# fail rather than being silently misread. The name must contain 2 numbers
# (SU, increment) or 3 (legacy field, SU, increment); see parse_su_name().
#
# ^SU(?![A-Za-z]) : starts with SU not followed by another letter (rejects SURF…)
# [^A-Za-z]+$     : the remainder is digits + non-letter separators only
SU_NAME_LENIENT = re.compile(r'^SU(?![A-Za-z])[^A-Za-z]+$', re.IGNORECASE)
_SU_NAME_INTS = re.compile(r'\d+')

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

# Default target attribute field holding the SU number. Standardized across the
# project's SU layers; exposed in the dialog as a field dropdown defaulting to
# this. Matching is on the SU number alone — no field/trench field is used.
SU_FIELD_NAME = SU_FIELD_EMLID_TARGET


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


def normalize_su_value(value):
    """
    Normalize an SU/FIELD value to a stable string key for matching.

    QGIS fields are typed, so the same SU number can arrive as an int (322), a
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


def qc_fc_name(prefix, su):
    """Build a valid layer/file name (letters/digits/underscore only) for one SU's QC layer."""
    safe_su = re.sub(r'[^0-9A-Za-z]+', '_', str(su))
    name = "{}_{}".format(prefix, safe_su)
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
            "The selected Emlid point source could not be read (invalid or "
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
    by SU across all files, so the same SU shot across two files ends up in one
    group. Per-file row counts are reported.
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


def parse_su_name(name):
    """
    Parse an SU point name into (su, seq).

    su is returned as an unpadded string (used as a text key / value
    downstream), seq as an int. The increment is the last integer in the name
    and the SU is the integer before it; any earlier integer (a legacy leading
    field/trench number) is ignored. Returns None when the name can't be
    resolved to 2 integers (SU + increment) or 3 (legacy field + SU + increment),
    or when it contains letters where only the number/separator structure
    belongs.
    """
    if not SU_NAME_LENIENT.match(name):
        return None
    nums = _SU_NAME_INTS.findall(name)
    if len(nums) not in (2, 3):
        return None
    su = str(int(nums[-2]))   # second-to-last integer = SU number
    seq = int(nums[-1])       # last integer = increment
    return su, seq


def parse_su_groups(rows, columns, messages, ct_wgs_to_emlid):
    """
    Filter to SU_ outline points only, and group into an ordered dict:
        {su: [(seq, easting, northing, name), ...]}  sorted by seq
    Also returns a flat list of all valid SU points (for the optional QC layer).

    Only the SU number and the increment (seq) matter: each point name gives an
    SU number and an increment (see parse_su_name), points are grouped and
    matched by SU number alone, and connected in increment order.

    Resilient to the mixed content of real exports: SU-prefixed points that
    aren't outline vertices (a single labeled shot with no increment, or an
    unreadable name) are skipped with a warning, and an SU whose points repeat
    an increment (ambiguous order) is dropped with a warning — neither aborts
    the run, so the good SUs still go through. Only a genuinely unusable input
    table (missing Name / coordinate columns) raises.
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

    groups = {}
    all_points = []
    dropped_non_su = 0
    skipped_no_coords = 0
    skipped_su_names = []

    for row in rows:
        raw = row.get("Name")
        if raw is None:
            dropped_non_su += 1
            continue
        name = str(raw).strip()

        if not name.upper().startswith("SU"):
            dropped_non_su += 1
            continue

        parsed = parse_su_name(name)
        if parsed is None:
            # Starts with SU but isn't an outline vertex — e.g. a single labeled
            # shot with no increment (SU-2623, an elevation point), or a name
            # that can't be read as SU number + increment. Skip it and keep
            # going rather than aborting the whole run: real exports routinely
            # mix such points in with the outline points.
            skipped_su_names.append(name)
            continue
        su, seq = parsed

        xy = resolve_row_xy(row, has_en, has_ll, ct_wgs_to_emlid)
        if xy is None:
            skipped_no_coords += 1
            messages.addWarningMessage(
                "Skipped point '{}': no usable Easting/Northing or Longitude/Latitude "
                "coordinates.".format(name)
            )
            continue
        easting, northing = xy

        groups.setdefault(su, []).append((seq, easting, northing, name))
        all_points.append((su, seq, easting, northing, name))

    if skipped_su_names:
        messages.addWarningMessage(
            "Skipped {} SU-prefixed point(s) that aren't outline vertices (no "
            "increment, or an unreadable name): {}. Only names giving an SU "
            "number and an increment are used to build outlines.".format(
                len(skipped_su_names),
                ", ".join("'{}'".format(n) for n in skipped_su_names),
            )
        )

    # Sort each group's points by increment and sanity-check the sequence. A
    # duplicate increment within one SU makes the outline order ambiguous, so
    # that SU is dropped (with a warning) rather than building a wrong polygon —
    # but the other SUs still go through.
    for su in list(groups.keys()):
        pts = groups[su]
        pts.sort(key=lambda p: p[0])
        seqs = [p[0] for p in pts]
        if len(set(seqs)) != len(seqs):
            repeated = sorted({s for s in seqs if seqs.count(s) > 1})
            messages.addWarningMessage(
                "SU {} repeats increment number(s) {} (full sequence: {}) — the "
                "outline order is ambiguous, so this SU was skipped. Fix the point "
                "names and rerun to include it.".format(su, repeated, seqs)
            )
            del groups[su]
            continue
        expected = list(range(1, len(seqs) + 1))
        if seqs != expected:
            messages.addWarningMessage(
                "SU {} increment numbers are {} (expected {}). "
                "Points will still be connected in the order given, but check for "
                "typos/missing points.".format(su, seqs, expected)
            )

    messages.addMessage(
        "Parsed {} SU outline point(s) into {} SU group(s). "
        "Dropped {} non-SU point(s). Skipped {} SU-prefixed non-outline name(s), "
        "{} point(s) with no usable coordinates.".format(
            len(all_points), len(groups), dropped_non_su,
            len(skipped_su_names), skipped_no_coords
        )
    )

    return groups, all_points


def _geometry_is_empty(feat):
    """True when a feature has no usable polygon geometry (NULL or empty)."""
    if not feat.hasGeometry():
        return True
    geom = feat.geometry()
    return geom is None or geom.isNull() or geom.isEmpty()


def build_existing_index(target_layer, su_field):
    """
    Index the target layer's existing records by their SU number:
        { su: [(feature_id, is_empty_geometry), ...] }
    SU values are normalized (see normalize_su_value) so matching is robust to
    int/double/text field types.
    """
    existing = {}
    for feat in target_layer.getFeatures():
        su_val = normalize_su_value(feat[su_field])
        is_empty = _geometry_is_empty(feat)
        existing.setdefault(su_val, []).append((feat.id(), is_empty))
    return existing


def build_su_polygon(pts, source_crs, ct_to_target):
    """
    Build a closed polygon geometry from an SU's ordered points, in source_crs,
    reprojected to the target CRS when ct_to_target is provided (else left as-is
    because the target already matches the Emlid CRS).
    """
    ring = [QgsPointXY(e, n) for (_, e, n, _) in pts]
    if ring[0] != ring[-1]:
        ring.append(ring[0])
    geom = QgsGeometry.fromPolygonXY([ring])
    if ct_to_target is not None:
        geom.transform(ct_to_target)
    return geom


def create_polygon_layer(groups, source_crs, layer_name, messages):
    """
    Build the SU polygons onto a brand-new temporary (in-memory) polygon layer
    and add it to the project — a "connect the dots" mode with no target layer
    and no matching. One feature per SU group with >= 3 points; each carries the
    SU number and the vertex count, and nothing else. Geometry stays in the
    Emlid CRS (points are not reprojected, since there is no target).

    Returns (layer, stats) with stats = {"created": n, "too_few": n}.
    """
    authid = source_crs.authid() or "EPSG:{}".format(EMLID_WKID)
    uri = (
        "Polygon?crs={}"
        "&field={}:string(50)"
        "&field=VERTICES:integer".format(authid, SU_FIELD_NAME)
    )
    layer = QgsVectorLayer(uri, layer_name or "SU polygons (temp)", "memory")
    provider = layer.dataProvider()

    feats = []
    too_few = 0
    for su, pts in groups.items():
        if len(pts) < 3:
            messages.addWarningMessage(
                "SU {} only has {} point(s) — at least 3 are needed to form a "
                "polygon. Skipped.".format(su, len(pts))
            )
            too_few += 1
            continue
        f = QgsFeature(layer.fields())
        f.setGeometry(build_su_polygon(pts, source_crs, None))
        f.setAttributes([str(su), len(pts)])
        feats.append(f)

    provider.addFeatures(feats)
    layer.updateExtents()
    QgsProject.instance().addMapLayer(layer)
    messages.addMessage(
        "Created temporary polygon layer '{}' with {} SU polygon(s). "
        "{} SU group(s) skipped for having fewer than 3 points.".format(
            layer.name(), len(feats), too_few
        )
    )
    return layer, {"created": len(feats), "too_few": too_few}


def plan_geometry_updates(groups, existing, replace_regardless,
                          source_crs, ct_to_target, messages):
    """
    Decide which existing target records get their geometry replaced, matched
    by SU number alone.

    Returns (updates, stats) where updates is [(feature_id, geometry), ...] and
    stats is a dict of skip counts. Never inserts and never edits attributes:
    an SU with no matching record is skipped, and an SU matching more than one
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

    for su, pts in groups.items():
        if len(pts) < 3:
            messages.addWarningMessage(
                "SU {} only has {} point(s) — at least 3 are needed to form a "
                "polygon. Skipped.".format(su, len(pts))
            )
            stats["too_few"] += 1
            continue

        geom = build_su_polygon(pts, source_crs, ct_to_target)

        matches = existing.get(normalize_su_value(su), [])

        if not matches:
            messages.addWarningMessage(
                "SU {} has no matching record in the target layer. Skipped "
                "(this tool only replaces geometry on existing SU records; it "
                "never adds new ones).".format(su)
            )
            stats["no_match"] += 1
            continue

        if replace_regardless:
            if len(matches) > 1:
                messages.addWarningMessage(
                    "SU {} matches {} records in the target layer — ambiguous "
                    "which to replace, skipped. Resolve the duplicate record(s) "
                    "before rerunning.".format(su, len(matches))
                )
                stats["ambiguous"] += 1
                continue
            updates.append((matches[0][0], geom))
            stats["to_update"] += 1
        else:
            empty_matches = [(fid, is_empty) for fid, is_empty in matches if is_empty]
            if not empty_matches:
                messages.addWarningMessage(
                    "SU {} already has geometry in the target layer ({} matching "
                    "record(s), none empty). Not overwritten in 'only fill empty "
                    "geometry' mode — switch to 'replace regardless' if you intend "
                    "to replace it.".format(su, len(matches))
                )
                stats["has_geometry"] += 1
                continue
            if len(empty_matches) > 1:
                messages.addWarningMessage(
                    "SU {} matches {} empty-geometry record(s) — ambiguous which "
                    "to fill in, skipped. Resolve the duplicate record(s) before "
                    "rerunning.".format(su, len(empty_matches))
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
        messages.addMessage("No existing SU records matched — no geometry was replaced.")
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
            "Replaced and saved geometry on {} existing SU record(s).".format(succeeded)
        )
    else:
        if already_editing and commit:
            messages.addWarningMessage(
                "Target layer '{}' was already in an edit session, so the new "
                "geometry was added to that buffer instead of being saved "
                "automatically.".format(target_layer.name())
            )
        messages.addMessage(
            "Staged geometry replacement on {} existing SU record(s) in the edit "
            "buffer — NOTHING is saved to disk yet. Review the new outlines on the "
            "map, then click 'Save Layer Edits' (the disk icon on the Digitizing "
            "toolbar) to keep them, or toggle editing off without saving / press "
            "Undo to discard.".format(succeeded)
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


def create_qc_layers(all_points, source_crs, prefix, out_dir, messages):
    """
    Build one QC point layer per SU from the raw parsed vertices, for visually
    sanity-checking the polygon against the original shots. Layers are added to
    the current project. When out_dir is given, each is also saved there as a
    shapefile (falling back to an in-memory layer if the save fails); otherwise
    they are temporary in-memory layers.
    """
    qc_groups = {}
    for su, seq, easting, northing, name in all_points:
        qc_groups.setdefault(su, []).append((seq, easting, northing, name))

    created = []
    authid = source_crs.authid() or "EPSG:{}".format(EMLID_WKID)
    for su, pts in qc_groups.items():
        pts.sort(key=lambda p: p[0])
        layer_name = qc_fc_name(prefix, su)
        uri = (
            "Point?crs={}"
            "&field=NAME:string(100)"
            "&field={}:string(50)"
            "&field=SEQ:integer".format(authid, SU_FIELD_NAME)
        )
        mem = QgsVectorLayer(uri, layer_name, "memory")
        provider = mem.dataProvider()
        feats = []
        for seq, easting, northing, name in pts:
            f = QgsFeature(mem.fields())
            f.setGeometry(QgsGeometry.fromPointXY(QgsPointXY(easting, northing)))
            f.setAttributes([name, str(su), int(seq)])
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
