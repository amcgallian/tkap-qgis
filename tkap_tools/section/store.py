"""Saving a section to a GeoPackage, and getting it back.

One file per section, holding everything needed to carry on where you left off:

* ``section_polygons`` -- the drawn units, with their attributes
* ``section_frame``    -- the drawing surface
* ``section_meta``     -- a single row describing the section itself: the trace
  endpoints in EPSG:32636, the vertical extent, the flip, the widened drawing
  extent, the photo it was rectified against and the matrix that placed it

The geometry is stored in the section's own CRS, so the file opens sensibly in
plain QGIS without the plugin -- x is chainage, y is elevation, and the CRS
remark says how to get back to real-world coordinates.

The photo is *referenced*, not embedded: a placed ortho is tens of megabytes, and
folding one into every section file would make the sections unwieldy for no gain.
But it is referenced from right next to the section file, not from wherever it
happened to be warped -- the drawing and the photograph it was traced over are
one thing to the person who made them, so they are kept as one thing on disk and
travel together. On top of that the *placement itself* is recorded, so a backdrop
that does go missing can be rebuilt from the original photograph rather than
costing the user every control point they picked. See :func:`find_photo`.
"""

from __future__ import annotations

import json
import shutil
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

from qgis.core import (
    QgsCoordinateReferenceSystem,
    QgsCoordinateTransformContext,
    QgsFeature,
    QgsField,
    QgsFields,
    QgsGeometry,
    QgsPointXY,
    QgsVectorFileWriter,
    QgsVectorLayer,
    QgsWkbTypes,
)
from qgis.PyQt.QtCore import QVariant

from .section_geom import SectionLine, section_crs_wkt

POLYGON_TABLE = "section_polygons"
FRAME_TABLE = "section_frame"
META_TABLE = "section_meta"

#: Bumped when the metadata layout changes in a way older files cannot satisfy.
FORMAT_VERSION = 1


def placed_photo_name(gpkg_path: str | Path) -> str:
    """What a section file calls the placed backdrop it keeps beside itself.

    Derived from the section file's own name rather than the photo's, so a
    section that has lost its metadata -- or been handed over as a bare pair of
    files -- can still be reunited with its backdrop by name alone.
    """
    return f"{Path(gpkg_path).stem}_photo.tif"


class SectionStoreError(RuntimeError):
    pass


def _plain(value):
    """Coerce a QGIS attribute into something ``json`` can write.

    A NULL attribute does not come back as ``None`` -- PyQGIS hands over a null
    ``QVariant``, which ``json.dumps`` refuses with "Object of type QVariant is
    not JSON serializable". Since a unit with no recorded type is completely
    ordinary, that turned an everyday save into a failure.
    """
    if value is None:
        return None
    if isinstance(value, QVariant):
        return None if value.isNull() else _plain(value.value())
    if isinstance(value, (bool, int, float, str)):
        return value
    # Dates, QByteArray and anything else exotic: keep it readable rather than
    # losing the row.
    return str(value)


def _meta_fields() -> QgsFields:
    # String fields are left UNBOUNDED (no len). A GeoPackage TEXT column has no
    # length limit, but a QgsField with a width does: when a value is longer
    # than that width, QgsVectorFileWriter reports success yet silently writes
    # ZERO rows -- so the whole section_meta feature vanished, and reopening
    # then failed with "no section metadata row". A real section over a photo
    # has many SUs, so its extra_json ran past the old 4096 cap and triggered
    # exactly that, while an empty test section stayed under it and saved fine.
    # An unbounded field can never overflow, so nothing is ever dropped.
    fields = QgsFields()
    fields.append(QgsField("format_version", QVariant.Int))
    fields.append(QgsField("name", QVariant.String))
    fields.append(QgsField("title", QVariant.String))
    fields.append(QgsField("space_number", QVariant.String))
    for name in ("p0_e", "p0_n", "p1_e", "p1_n", "z_min", "z_max",
                 "x_min", "x_max", "buffer"):
        fields.append(QgsField(name, QVariant.Double))
    fields.append(QgsField("flipped", QVariant.Int))
    fields.append(QgsField("photo_path", QVariant.String))
    fields.append(QgsField("photo_placed", QVariant.String))
    fields.append(QgsField("style_qml", QVariant.String))
    # Free-form so the format can grow without another version bump.
    fields.append(QgsField("extra_json", QVariant.String))
    return fields


def save_session(session, path: str | Path, *, title: str = "") -> Path:
    """Write a session to ``path``. Returns the path actually written."""
    path = Path(path)
    if path.suffix.lower() != ".gpkg":
        path = path.with_suffix(".gpkg")
    if session.polygon_layer is None:
        raise SectionStoreError("This session has no drawing to save")

    line = session.line
    ctx = QgsCoordinateTransformContext()

    # Before anything is written, get the backdrop next to the file that refers
    # to it, so the pair can be copied to a share or another machine together.
    placed_abs, placed_rel = _photo_beside(session, path)

    def write(layer, name, first):
        options = QgsVectorFileWriter.SaveVectorOptions()
        options.driverName = "GPKG"
        options.layerName = name
        options.fileEncoding = "UTF-8"
        options.actionOnExistingFile = (
            QgsVectorFileWriter.CreateOrOverwriteFile if first
            else QgsVectorFileWriter.CreateOrOverwriteLayer
        )
        err = QgsVectorFileWriter.writeAsVectorFormatV3(
            layer, str(path), ctx, options
        )
        # The signature has varied across releases; the code is always first.
        code = err[0] if isinstance(err, (tuple, list)) else err
        if code != QgsVectorFileWriter.NoError:
            detail = err[1] if isinstance(err, (tuple, list)) and len(err) > 1 else ""
            raise SectionStoreError(f"Could not write {name}: {detail or code}")

    # Everything that can fail is done before a single byte is written.
    # Building the metadata last meant a serialisation error left a file with
    # polygons but no section_meta -- which then failed to open with a
    # confusing "not a saved section", the original problem one step removed.
    meta = QgsVectorLayer(
        f"Point?crs={session.crs.toWkt()}", META_TABLE, "memory"
    )
    meta.dataProvider().addAttributes(_meta_fields().toList())
    meta.updateFields()

    feat = QgsFeature(meta.fields())
    feat["format_version"] = FORMAT_VERSION
    feat["name"] = line.name
    feat["title"] = title or session.default_title()
    feat["space_number"] = session.space_number or ""
    feat["p0_e"], feat["p0_n"] = float(line.p0[0]), float(line.p0[1])
    feat["p1_e"], feat["p1_n"] = float(line.p1[0]), float(line.p1[1])
    feat["z_min"] = float(line.z_min if line.z_min is not None else 0.0)
    feat["z_max"] = float(line.z_max if line.z_max is not None else 0.0)
    feat["x_min"], feat["x_max"] = float(line.x_min), float(line.x_max)
    feat["buffer"] = float(line.buffer)
    feat["flipped"] = 1 if line.flipped else 0
    feat["photo_path"] = getattr(session, "photo_source", "") or ""
    # Read from the session, never from the layer. Taking it off the layer meant
    # that reopening a section whose backdrop had gone -- so there was no layer
    # -- and then saving wrote an empty path over the only record of it, which
    # made a recoverable section permanently unrecoverable. Absolute on purpose:
    # this is also the only field an older build of the plugin reads.
    feat["photo_placed"] = placed_abs
    feat["style_qml"] = session.style_qml or ""
    source = getattr(session, "source_layer", None)
    extra = {
        # Which vertical datum the drawing's heights are on. A label rather than
        # a conversion -- the geometry is already on it -- but reopening without
        # it would state the wrong datum in the section CRS.
        "height_datum": line.height_datum,
        # Which SU layer this was drawn from. Recorded so reopening can find it
        # again: without it a reopened section had no source layer at all, and
        # "Add unit..." dead-ended on "this session has no source SU layer".
        # Three ways to match because each fails differently -- the layer id
        # changes when the project is rebuilt, the name when someone renames it,
        # and the source when the data moves.
        "su_layer": {
            "id": source.id() if source is not None else "",
            "name": source.name() if source is not None else "",
            "source": source.source() if source is not None else "",
        },
        "candidates": [
            {
                "su_id": int(c.su_id),
                "su_number": _plain(c.su_number),
                "su_type": _plain(c.su_type),
                "subtype": _plain(c.subtype),
                "subsubtype": _plain(c.subsubtype),
                "include": bool(c.include),
            }
            for c in session.candidates
        ],
    }

    # How the backdrop was placed. Recorded because the placement used to be
    # thrown away the instant it had been applied, so a section that lost its
    # rectified photo could only be recovered by picking every control point
    # again -- even though the original photograph was usually still on disk and
    # still correct. Written whenever there is a placement to describe, whether
    # or not the layer is currently loaded.
    photo = _photo_block(session, placed_abs, placed_rel)
    if photo:
        extra["photo"] = photo

    feat["extra_json"] = json.dumps(extra)
    # A point at the middle of the section, purely so the row has a geometry and
    # the table shows up as a layer rather than an aspatial table.
    feat.setGeometry(QgsGeometry.fromPointXY(QgsPointXY(
        (line.x_min + line.x_max) / 2.0,
        ((line.z_min or 0.0) + (line.z_max or 0.0)) / 2.0,
    )))
    meta.dataProvider().addFeatures([feat])
    meta.updateExtents()

    # Metadata first, so a file that exists at all is one that can be reopened.
    write(meta, META_TABLE, first=True)
    write(session.polygon_layer, POLYGON_TABLE, first=False)
    if session.frame_layer is not None:
        write(session.frame_layer, FRAME_TABLE, first=False)

    # Cheap insurance: prove the file we just wrote can be read back -- and that
    # section_meta actually has its row, not just that the table exists -- rather
    # than finding out days later when someone tries to open it. Verified through
    # a throwaway copy (see _readable_copy) so checking the save does not leave
    # the real file pooled open and blocking the next overwrite.
    with _readable_copy(path) as check:
        meta_back = _open(check, META_TABLE)
        if (meta_back is None or next(meta_back.getFeatures(), None) is None
                or _open(check, POLYGON_TABLE) is None):
            raise SectionStoreError(
                f"{path.name} was written but cannot be read back - the save "
                "did not complete."
            )

    # Only now that the file is known good, move the live layer onto the copy
    # beside it. Doing it earlier would leave a session pointing at the backdrop
    # for a section that turned out not to have been written. Without it the
    # layer stays on the scratch copy for the rest of the session and every
    # later save re-copies the whole ortho.
    if (session.photo_layer is not None and placed_abs
            and session.photo_layer.source() != placed_abs):
        try:
            session.reload_placed_photo(placed_abs)
        except Exception:
            # The saved file is correct either way; this is only about which
            # copy is on screen.
            pass

    return path


def _photo_beside(session, gpkg: Path) -> tuple[str, str]:
    """Put the placed backdrop next to ``gpkg``. Returns (absolute, relative).

    The drawing and the photograph it was traced over are one thing to the
    person who made them, so they are kept as one thing on disk. Previously the
    placed raster stayed in the temp directory it was warped into and only its
    path was recorded, which meant Windows swept it away days later and no other
    machine could ever see it.

    Best effort throughout: a section that saves without its backdrop is a bad
    day, and a section that fails to save is a lost one.
    """
    layer = getattr(session, "photo_layer", None)
    placed = layer.source() if layer is not None else ""
    # Falling back to the recorded path is what lets a section that reopened
    # without its backdrop still be saved without losing track of it.
    placed = placed or (getattr(session, "photo_placed", "") or "")
    if not placed or not Path(placed).is_file():
        return placed, ""

    dest = gpkg.parent / placed_photo_name(gpkg)
    if Path(placed) == dest:
        return str(dest), dest.name          # already here; every save after the first

    try:
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(placed, dest)
        # Statistics and overviews are regenerable, but copying them spares the
        # user a recompute the first time the section is opened elsewhere.
        for suffix in (".aux.xml", ".ovr"):
            side = Path(str(placed) + suffix)
            if side.exists():
                try:
                    shutil.copy2(side, Path(str(dest) + suffix))
                except OSError:
                    pass
    except OSError:
        return placed, ""

    return str(dest), dest.name


def _photo_block(session, placed_abs: str, placed_rel: str) -> dict:
    """The ``extra_json`` record of how the backdrop was placed, or {}."""
    from .photo import fit_to_dict, points_to_list

    fit = getattr(session, "photo_fit", None)
    source = getattr(session, "photo_source", "") or ""
    if not source and fit is None and not placed_abs:
        return {}

    block = {
        "source": source,
        # Relative first, so the pair can be copied anywhere together. Stored
        # POSIX-style because a backslash is an ordinary filename character on
        # anything but Windows.
        "placed": Path(placed_rel).as_posix() if placed_rel else "",
        "placed_abs": placed_abs,
        # Kept for older readers, and always the same two values now: every
        # section is drawn in ellipsoidal heights with nothing converted.
        "separation": 0.0,
        "datum": session.line.height_datum,
    }
    if fit is not None:
        block["fit"] = fit_to_dict(fit)
        block["points"] = points_to_list(getattr(session, "photo_points", []) or [])
    return block


class _readable_copy:
    """A disposable copy of a GeoPackage, for reading it safely.

    Reading a .gpkg with a QgsVectorLayer is reliable inside QGIS, but QGIS's
    OGR provider keeps every file it opens in a connection pool for the life of
    the process, and that pooled handle then blocks the next *overwrite* of the
    same file -- which is what broke saving a section twice. Reading raw OGR
    avoided the pool but proved unreliable inside the running app.

    So every read goes through a copy instead: the real section file is never
    opened by the provider, so it is never locked and can always be overwritten.
    The copy is what gets pooled, and it is thrown away. Used as a context
    manager; ``with _readable_copy(path) as tmp:`` yields the copy's path.
    """

    def __init__(self, path: Path) -> None:
        self._path = Path(path)
        self._dir: str | None = None

    def __enter__(self) -> Path:
        self._dir = tempfile.mkdtemp(prefix="tkap_read_")
        dst = Path(self._dir) / self._path.name
        # copy2 opens the source only for reading, which Windows allows even
        # when the file is pooled open elsewhere.
        shutil.copy2(self._path, dst)
        for suffix in ("-wal", "-shm", "-journal"):
            side = self._path.with_name(self._path.name + suffix)
            if side.exists():
                try:
                    shutil.copy2(side, Path(self._dir) / side.name)
                except OSError:
                    pass
        return dst

    def __exit__(self, *exc) -> None:
        # The copy is now pooled open too, so it usually cannot be deleted; that
        # is fine, it is a small temp file the OS will clear. Best-effort only.
        if self._dir:
            shutil.rmtree(self._dir, ignore_errors=True)


def _open(path: Path, table: str) -> QgsVectorLayer | None:
    """Open a GeoPackage table as a QgsVectorLayer.

    Reliable inside QGIS, but it leaves the file pooled open, so callers that
    will then overwrite the same path must open a :class:`_readable_copy` first
    rather than the real file.
    """
    layer = QgsVectorLayer(f"{path}|layername={table}", table, "ogr")
    return layer if layer.isValid() else None


def read_metadata(path: str | Path) -> dict:
    """Read a saved section's metadata without rebuilding anything.

    Reads through a throwaway copy so reopening never leaves the real file
    pooled open and blocking a later save -- see :class:`_readable_copy`.
    """
    path = Path(path)
    if not path.exists():
        raise SectionStoreError(f"{path} does not exist")

    with _readable_copy(path) as copy:
        layer = _open(copy, META_TABLE)
        if layer is None:
            raise SectionStoreError(
                f"{path.name} has no {META_TABLE} table - not a saved section"
            )
        feat = next(layer.getFeatures(), None)
        if feat is None:
            raise SectionStoreError(f"{path.name} has no section metadata row")
        data = {name: feat[name] for name in layer.fields().names()}

    version = data.get("format_version") or 0
    if version > FORMAT_VERSION:
        raise SectionStoreError(
            f"{path.name} was written by a newer version of the plugin "
            f"(format {version}, this understands {FORMAT_VERSION})"
        )
    try:
        data["extra"] = json.loads(data.get("extra_json") or "{}")
    except (TypeError, ValueError):
        data["extra"] = {}
    return data


def find_source_layer(meta: dict):
    """The SU layer a saved section was drawn from, if it is in the project.

    Returns (layer, how) where ``how`` names the match for reporting, or
    (None, reason) when nothing suitable is loaded. Sections saved before the
    source layer was recorded simply have nothing to match on, which is not an
    error -- the panel lets one be picked either way.

    Matched by id, then by data source, then by name, in falling order of how
    much they prove. An id is exact but does not survive the project being
    rebuilt; a data source survives that but not the file moving; a name
    survives both but is the weakest claim, so it is only accepted when it picks
    out exactly one layer.
    """
    from qgis.core import QgsProject

    saved = (meta.get("extra") or {}).get("su_layer") or {}
    if not any(saved.get(k) for k in ("id", "source", "name")):
        return None, "the section was saved before the SU layer was recorded"

    polygons = [
        layer for layer in QgsProject.instance().mapLayers().values()
        if isinstance(layer, QgsVectorLayer)
        and layer.geometryType() == QgsWkbTypes.PolygonGeometry
    ]

    layer_id = saved.get("id") or ""
    if layer_id:
        for layer in polygons:
            if layer.id() == layer_id:
                return layer, f"'{layer.name()}'"

    source = saved.get("source") or ""
    if source:
        for layer in polygons:
            if layer.source() == source:
                return layer, f"'{layer.name()}' (matched on its data source)"

    name = saved.get("name") or ""
    if name:
        by_name = [layer for layer in polygons if layer.name() == name]
        if len(by_name) == 1:
            return by_name[0], f"'{name}' (matched on name)"
        if len(by_name) > 1:
            return None, f"more than one layer is called '{name}'"

    return None, f"'{name or 'the SU layer'}' is not loaded in this project"


@dataclass
class PhotoResolution:
    """How a saved section's backdrop can be got back, and how sure we are."""

    #: An existing placed raster to load as it stands.
    placed: str = ""
    #: The original photograph, when the placement has to be redone.
    source: str = ""
    fit: object = None
    points: list = field(default_factory=list)
    #: What a section saved before heights were settled was fitted with.
    #: Nothing applies it any more; it is read so a re-placement can say the
    #: drawing was built on an offset that no longer exists.
    separation: float = 0.0
    #: Plain English, for the message bar.
    how: str = ""

    @property
    def can_replace(self) -> bool:
        """Whether the backdrop can be rebuilt from the original photograph."""
        return self.fit is not None and bool(self.source) and Path(self.source).is_file()


def find_photo(meta: dict, gpkg_path: str | Path) -> PhotoResolution:
    """Where a saved section's backdrop can be found now.

    The same shape of problem as :func:`find_source_layer`, and answered the same
    way: several candidates tried in falling order of how much each one proves,
    and a ``how`` string so the user is told which one worked rather than being
    left to wonder why the drawing looks different.

    Candidates, in order: the copy beside the section file; the path it was saved
    from; a backdrop named after the section file sitting beside it; and finally
    the original photograph plus the saved placement, which costs a re-warp but
    needs nothing to have stayed where it was.

    This only *finds*. Re-warping is the caller's business, because it is slow
    and wants a wait cursor around it.
    """
    from .photo import fit_from_dict, points_from_list

    gpkg = Path(gpkg_path)
    saved = (meta.get("extra") or {}).get("photo") or {}

    res = PhotoResolution(
        source=saved.get("source") or (meta.get("photo_path") or ""),
        fit=fit_from_dict(saved.get("fit")),
        points=points_from_list(saved.get("points")),
        separation=float(saved.get("separation") or 0.0),
    )

    relative = saved.get("placed") or ""
    if relative:
        beside = gpkg.parent / relative
        if beside.is_file():
            res.placed, res.how = str(beside), "beside the section file"
            return res

    recorded = saved.get("placed_abs") or (meta.get("photo_placed") or "")
    if recorded and Path(recorded).is_file():
        res.placed, res.how = recorded, "at the path it was saved from"
        return res

    # A section file and its backdrop that were copied somewhere together, where
    # the metadata predates the relative path being recorded.
    by_name = gpkg.parent / placed_photo_name(gpkg)
    if by_name.is_file():
        res.placed, res.how = str(by_name), f"as {by_name.name}, beside the section file"
        return res

    if res.can_replace:
        res.how = (
            f"re-placed from {Path(res.source).name} using the control points "
            "saved with the section"
        )
        return res

    if not (recorded or relative or res.source):
        res.how = "this section was drawn without a photo"
    elif res.source:
        res.how = (
            f"the rectified photo is not beside {gpkg.name}, and "
            f"{Path(res.source).name} is no longer where it was"
        )
    else:
        res.how = f"the rectified photo is not beside {gpkg.name}"
    return res


def line_from_metadata(meta: dict) -> SectionLine:
    """Rebuild the section geometry from a saved row."""
    line = SectionLine(
        (float(meta["p0_e"]), float(meta["p0_n"])),
        (float(meta["p1_e"]), float(meta["p1_n"])),
        name=meta.get("name") or "Section",
        buffer=float(meta.get("buffer") or 0.25),
        flipped=bool(meta.get("flipped")),
    )
    # Read as saved rather than forced: a section drawn before heights were
    # settled may say orthometric, and the caller warns about that rather than
    # relabelling the axis under a drawing that was not made on it.
    line.height_datum = (
        (meta.get("extra") or {}).get("height_datum") or "orthometric"
    )
    line.set_vertical_extent(float(meta["z_min"]), float(meta["z_max"]))
    x_min, x_max = meta.get("x_min"), meta.get("x_max")
    if x_min is not None and x_max is not None:
        # set_chainage_extent, not extend_to: the saved span is the frame as it
        # was last left, including cropped in past the ends of the trace.
        # extend_to would quietly widen it back out to the full trace and the
        # section would reopen bigger than it was saved.
        line.set_chainage_extent(float(x_min), float(x_max))
    return line


def load_polygons(path: str | Path):
    """The saved drawing, as (features, fields), ready to copy into a session.

    Read through a throwaway copy so reopening never pools the real file open --
    otherwise a later save back to the same section would fail to overwrite it.
    The features are fully materialised inside the copy, so they stay valid once
    it is discarded.
    """
    path = Path(path)
    with _readable_copy(path) as copy:
        layer = _open(copy, POLYGON_TABLE)
        if layer is None:
            raise SectionStoreError(
                f"{path.name} has no {POLYGON_TABLE} table - not a saved section"
            )
        return list(layer.getFeatures()), layer.fields()


def crs_for(meta: dict) -> QgsCoordinateReferenceSystem:
    crs = QgsCoordinateReferenceSystem()
    crs.createFromWkt(section_crs_wkt(line_from_metadata(meta)))
    return crs
