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

The photo is *referenced*, not embedded. A placed ortho is tens of megabytes and
usually lives on the share; copying it into every section file would make the
sections unwieldy for no gain. If it has moved by the time a section is
reopened, the drawing still loads -- just without its backdrop.
"""

from __future__ import annotations

import json
import shutil
import tempfile
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
)
from qgis.PyQt.QtCore import QVariant

from .section_geom import SectionLine, section_crs_wkt

POLYGON_TABLE = "section_polygons"
FRAME_TABLE = "section_frame"
META_TABLE = "section_meta"

#: Bumped when the metadata layout changes in a way older files cannot satisfy.
FORMAT_VERSION = 1


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
    feat["photo_placed"] = (
        session.photo_layer.source() if session.photo_layer is not None else ""
    )
    feat["style_qml"] = session.style_qml or ""
    feat["extra_json"] = json.dumps({
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
    })
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
    return path


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


def line_from_metadata(meta: dict) -> SectionLine:
    """Rebuild the section geometry from a saved row."""
    line = SectionLine(
        (float(meta["p0_e"]), float(meta["p0_n"])),
        (float(meta["p1_e"]), float(meta["p1_n"])),
        name=meta.get("name") or "Section",
        buffer=float(meta.get("buffer") or 0.25),
        flipped=bool(meta.get("flipped")),
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
