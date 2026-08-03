"""The drawing session: what happens to the QGIS project while a section is open.

The session takes over the main canvas, because that is the only way to get the
real vertex tool -- ``QgsVertexTool`` is not exposed to PyQGIS, so an embedded
canvas can create features but cannot reshape them, which is the entire job.

Taking over the canvas means everything it touches has to be put back:

* the project CRS becomes the section's engineering CRS, and is restored on exit
* plan-view layers are hidden (they have no transform path into an engineering
  CRS and would otherwise spew transform errors), and their visibility is
  restored
* snapping config is swapped and restored
* the layers the session creates are removed

:meth:`SectionSession.end` is safe to call twice and tries to restore as much as
it can even if part of the teardown fails, so a mistake here cannot leave the
user's project in a broken state.
"""

from __future__ import annotations

import tempfile
from dataclasses import dataclass, field
from pathlib import Path

from qgis.core import (
    QgsCoordinateReferenceSystem,
    QgsFeature,
    QgsField,
    QgsFillSymbol,
    QgsGeometry,
    QgsPalLayerSettings,
    QgsPointXY,
    QgsProject,
    QgsRasterLayer,
    QgsRectangle,
    QgsRuleBasedRenderer,
    QgsSingleSymbolRenderer,
    QgsSnappingConfig,
    QgsTextFormat,
    QgsTolerance,
    QgsVectorLayer,
    QgsVectorLayerSimpleLabeling,
)
from qgis.PyQt.QtCore import QVariant
from qgis.PyQt.QtGui import QColor

from .photo import Fit, write_placed_raster
from .section_geom import SectionLine, section_crs_wkt
from .su_source import SUCandidate, SeedSource

GROUP_NAME = "Section drawing"
POLYGON_LAYER_NAME = "Section polygons"
PHOTO_LAYER_NAME = "Section photo"
FRAME_LAYER_NAME = "Section frame"

#: Attribute driving per-row visibility. Also referenced in the label
#: expression, so hiding a row hides its label too.
HIDDEN_FIELD = "hidden"

#: Layer opacity while drawing, when the SU layer's own symbology is in use.
#: Those styles are built for opaque plan drawings; here the photo underneath is
#: the thing being traced and has to stay visible. Restored to full for export.
DRAWING_OPACITY = 0.55


def section_label_settings() -> QgsPalLayerSettings:
    """SU numbers, placed inside their own polygon and never dropped.

    A section drawing is read by matching a number to a shape, so a label that
    silently vanishes because it collided with a neighbour is worse than one
    that overlaps slightly. Hence ``displayAll``, no obstacle behaviour, and
    ``fitInPolygonOnly`` off so a thin lens still gets numbered.

    Shared with the figure export so the printed drawing is labelled exactly
    like the screen.
    """
    settings = QgsPalLayerSettings()
    # Just the number: "SU" in front of every polygon is noise on a drawing
    # whose caption already says these are stratigraphic units. A hidden row's
    # label goes with it, so text never floats over a polygon that is not drawn.
    settings.fieldName = f'if("{HIDDEN_FIELD}" is 1, \'\', "su_number")'
    settings.isExpression = True

    # Horizontal keeps the number upright and inside the polygon; centroidInside
    # forces the anchor into the shape rather than the bounding box centre,
    # which matters once a unit has been dragged into a concave lens.
    settings.placement = QgsPalLayerSettings.Horizontal
    settings.centroidInside = True
    settings.centroidWhole = False
    settings.fitInPolygonOnly = False
    settings.displayAll = True
    settings.priority = 10
    settings.obstacleSettings().setIsObstacle(False)

    text = QgsTextFormat()
    text.setSize(9)
    text.setColor(QColor(255, 255, 255))
    buf = text.buffer()
    buf.setEnabled(True)
    buf.setSize(1.0)
    buf.setColor(QColor(0, 0, 0, 200))
    text.setBuffer(buf)
    settings.setFormat(text)
    return settings


def _seed_geometry(cand: SUCandidate) -> QgsGeometry:
    """A box per chainage span, at the candidate's vertical extent.

    An SU the wall crosses twice gets two boxes in one multipolygon rather than
    one box bridging the gap, so the user trims rather than splits.
    """
    rings = []
    for span in cand.spans:
        rings.append([[
            QgsPointXY(span.x_min, cand.alt_min),
            QgsPointXY(span.x_max, cand.alt_min),
            QgsPointXY(span.x_max, cand.alt_max),
            QgsPointXY(span.x_min, cand.alt_max),
            QgsPointXY(span.x_min, cand.alt_min),
        ]])
    if not rings:
        return QgsGeometry()
    return QgsGeometry.fromMultiPolygonXY(rings)


@dataclass
class SectionSession:
    """One open section. Owns the layers it adds and the project state it changed."""

    line: SectionLine
    iface: object
    title: str = ""
    space_number: str | None = None
    #: The SU layer the candidates came from. Its symbology is copied onto the
    #: drawing layer so the section matches the project's plan drawings.
    source_layer: QgsVectorLayer | None = None
    #: A .qml to take symbology from instead. Wins over ``source_layer``,
    #: because a layer that has not had the project style applied yet has
    #: nothing worth inheriting, and that is easy to not notice.
    style_qml: str | None = None

    crs: QgsCoordinateReferenceSystem = field(init=False, default=None)
    polygon_layer: QgsVectorLayer | None = field(init=False, default=None)
    photo_layer: QgsRasterLayer | None = field(init=False, default=None)
    frame_layer: QgsVectorLayer | None = field(init=False, default=None)
    candidates: list[SUCandidate] = field(default_factory=list)

    _saved_crs: QgsCoordinateReferenceSystem | None = field(init=False, default=None)
    _saved_visibility: dict[str, bool] = field(init=False, default_factory=dict)
    _saved_snapping: QgsSnappingConfig | None = field(init=False, default=None)
    _group_name: str = field(init=False, default=GROUP_NAME)
    _ended: bool = field(init=False, default=False)
    _workdir: Path | None = field(init=False, default=None)
    #: The photo this section was rectified against, recorded so a saved
    #: section can find it again.
    photo_source: str = field(init=False, default="")
    #: Where this session was last saved, so Save can overwrite without asking.
    saved_to: str = field(init=False, default="")

    # ------------------------------------------------------------- lifecycle --

    def begin(self) -> None:
        """Swap the project into section space and add the session's layers."""
        project = QgsProject.instance()

        self.crs = QgsCoordinateReferenceSystem()
        if not self.crs.createFromWkt(section_crs_wkt(self.line)):
            raise RuntimeError("Could not build the section-local CRS")

        self._saved_crs = project.crs()
        self._saved_snapping = QgsSnappingConfig(project.snappingConfig())
        self._hide_plan_layers()

        project.setCrs(self.crs)
        self._make_polygon_layer()
        self._make_frame_layer()
        self._configure_snapping()
        self.zoom_to_section()

    def end(self, *, restore: bool = True) -> None:
        """Remove the session's layers and put the project back.

        Every step is independently guarded: a failure restoring one thing must
        not prevent the rest from being restored.
        """
        if self._ended:
            return
        self._ended = True
        project = QgsProject.instance()

        for layer in (self.polygon_layer, self.frame_layer, self.photo_layer):
            try:
                if layer is not None and project.mapLayer(layer.id()) is not None:
                    if isinstance(layer, QgsVectorLayer) and layer.isEditable():
                        layer.rollBack()
                    project.removeMapLayer(layer.id())
            except Exception:
                pass

        try:
            root = project.layerTreeRoot()
            group = root.findGroup(self._group_name)
            if group is not None:
                root.removeChildNode(group)
        except Exception:
            pass

        if restore:
            for restore_step in (
                self._restore_crs,
                self._restore_visibility,
                self._restore_snapping,
            ):
                try:
                    restore_step()
                except Exception:
                    pass

        try:
            self.iface.mapCanvas().refresh()
        except Exception:
            pass

    # ----------------------------------------------------------- project state --

    def _hide_plan_layers(self) -> None:
        """Hide everything currently visible, remembering what was on.

        Plan-view layers are in EPSG:32636 and there is no transform path into
        an engineering CRS, so leaving them on produces either nothing or a
        stream of transform warnings.
        """
        root = QgsProject.instance().layerTreeRoot()
        for node in root.findLayers():
            self._saved_visibility[node.layerId()] = node.isVisible()
            node.setItemVisibilityChecked(False)

    def _restore_visibility(self) -> None:
        root = QgsProject.instance().layerTreeRoot()
        for layer_id, was_visible in self._saved_visibility.items():
            node = root.findLayer(layer_id)
            if node is not None:
                node.setItemVisibilityChecked(was_visible)

    def _restore_crs(self) -> None:
        if self._saved_crs is not None:
            QgsProject.instance().setCrs(self._saved_crs)

    def _restore_snapping(self) -> None:
        if self._saved_snapping is not None:
            QgsProject.instance().setSnappingConfig(self._saved_snapping)

    def _configure_snapping(self) -> None:
        """Vertex+segment snapping at a tolerance that suits centimetre work."""
        config = QgsSnappingConfig(QgsProject.instance().snappingConfig())
        config.setEnabled(True)
        config.setMode(QgsSnappingConfig.AllLayers)
        config.setType(QgsSnappingConfig.VertexAndSegment)
        config.setUnits(QgsTolerance.ProjectUnits)
        config.setTolerance(0.02)          # 2 cm in section space
        config.setIntersectionSnapping(True)
        QgsProject.instance().setSnappingConfig(config)

    # ---------------------------------------------------------------- layers --

    def _group(self):
        root = QgsProject.instance().layerTreeRoot()
        group = root.findGroup(self._group_name)
        if group is None:
            group = root.insertGroup(0, self._group_name)
        return group

    def _make_polygon_layer(self) -> None:
        """The layer the user actually draws on, in section-local CRS."""
        layer = QgsVectorLayer(
            f"MultiPolygon?crs={self.crs.toWkt()}", POLYGON_LAYER_NAME, "memory"
        )
        if not layer.isValid():
            raise RuntimeError("Could not create the section polygon layer")

        provider = layer.dataProvider()
        # String fields are left UNBOUNDED (no len). A width-limited QgsField
        # silently drops the WHOLE feature when a value overflows it on save to
        # GeoPackage (QgsVectorFileWriter reports success but writes no row), so
        # one unexpectedly long label or note would lose a drawn polygon. An
        # unbounded field can never overflow; GeoPackage TEXT has no limit.
        provider.addAttributes([
            QgsField("su_id", QVariant.Int),
            QgsField("su_number", QVariant.String),
            QgsField("label", QVariant.String),
            QgsField("su_type", QVariant.String),
            QgsField("subtype", QVariant.String),
            # The project styles SUs on subsubtype, so it has to be here for the
            # clean drawing's cloned renderer and legend to work.
            QgsField("subsubtype", QVariant.String),
            QgsField("seed_source", QVariant.String),
            QgsField("edited", QVariant.Bool),
            # 1 = drawn with a fully transparent symbol. Deliberately *not* a
            # renderer filter: QGIS's snapping locator honours
            # willRenderFeature(), so a filtered-out feature stops snapping,
            # whereas an invisibly-rendered one keeps every vertex available.
            QgsField("hidden", QVariant.Int),
            QgsField("notes", QVariant.String),
        ])
        layer.updateFields()

        self._style_polygon_layer(layer)
        QgsProject.instance().addMapLayer(layer, False)
        self._group().insertLayer(0, layer)
        self.polygon_layer = layer

    def _style_polygon_layer(self, layer: QgsVectorLayer) -> None:
        """Style the drawing layer the way the SU layer is already styled.

        Two things have to hold at once. The section units should look like the
        project's SUs -- same categories, same colours -- so nobody has to
        restyle by hand every session. And a row hidden from the panel has to
        keep its vertices, which means rendering it invisibly rather than
        filtering it out, because QGIS's snapping locator honours
        ``willRenderFeature()``.

        Both are met by nesting: the source layer's renderer is converted to
        rules and hung underneath a ``not hidden`` rule, with a transparent
        sibling for the hidden ones. Nested rule filters combine, so each
        feature still takes exactly one branch.
        """
        root = QgsRuleBasedRenderer.Rule(None)

        shown = QgsRuleBasedRenderer.Rule(None)      # container, no symbol
        shown.setFilterExpression(f'"{HIDDEN_FIELD}" is not 1')
        shown.setLabel("Section units")
        root.appendChild(shown)

        adopted = self._source_rules(layer)
        if adopted is None:
            # No usable source symbology: a pale fill with a strong outline,
            # which at least stays readable over a photo.
            fallback = QgsRuleBasedRenderer.Rule(QgsFillSymbol.createSimple({
                "color": "255,255,255,40",
                "outline_color": "255,80,0,255",
                "outline_width": "0.5",
                "outline_width_unit": "Point",
            }))
            fallback.setLabel("Section unit")
            shown.appendChild(fallback)
            layer.setOpacity(1.0)
        else:
            for rule in adopted:
                shown.appendChild(rule)
            # The source styling is meant for opaque plan drawings; here it sits
            # over a photo that has to stay readable underneath.
            layer.setOpacity(DRAWING_OPACITY)

        ghost = QgsRuleBasedRenderer.Rule(QgsFillSymbol.createSimple({
            "color": "0,0,0,0",
            "outline_color": "0,0,0,0",
            "outline_style": "no",
        }))
        ghost.setFilterExpression(f'"{HIDDEN_FIELD}" is 1')
        ghost.setLabel("Hidden (still snaps)")
        root.appendChild(ghost)

        # Replace the renderer rather than setting a symbol on the existing one:
        # a categorised or rule-based renderer has no single symbol to set.
        layer.setRenderer(QgsRuleBasedRenderer(root))

        layer.setLabeling(QgsVectorLayerSimpleLabeling(section_label_settings()))
        layer.setLabelsEnabled(True)

    def _source_rules(self, layer: QgsVectorLayer):
        """The SU layer's symbology as detachable rules, or None.

        Returns child rules ready to nest, so the caller does not have to know
        whether the source was categorised, graduated or already rule-based.
        ``layer`` is passed in rather than read from ``self`` because styling
        happens while the drawing layer is still being built.
        """
        from .figure import adopt_source_renderer

        # Adopt onto a throwaway layer with the same fields, so the real one is
        # never left holding a half-applied renderer if this bails out.
        probe = QgsVectorLayer(
            f"MultiPolygon?crs={self.crs.toWkt()}", "probe", "memory"
        )
        probe.dataProvider().addAttributes(layer.fields().toList())
        probe.updateFields()

        took = False
        if self.style_qml:
            took = self._load_qml(probe, self.style_qml)
        if not took and self.source_layer is not None:
            took = adopt_source_renderer(probe, self.source_layer)
        if not took:
            return None

        converted = QgsRuleBasedRenderer.convertFromRenderer(probe.renderer())
        if converted is None:
            return None
        # clone(): the rules outlive the converted renderer they came from.
        return [child.clone() for child in converted.rootRule().children()]

    @staticmethod
    def _load_qml(layer: QgsVectorLayer, path: str) -> bool:
        """Apply a .qml, returning whether it produced usable symbology.

        A QML written for the SU layer names fields the section layer also
        carries (``subsubtype`` above all), so it transfers directly. It is
        accepted only if it yields something better than a single symbol --
        otherwise there is no point preferring it over the layer.
        """
        try:
            message, ok = layer.loadNamedStyle(path)
        except Exception:
            return False
        if not ok:
            return False
        renderer = layer.renderer()
        if renderer is None:
            return False
        # A style that collapses to one symbol is not what anyone means by
        # "use the project symbology".
        return type(renderer).__name__ != "QgsSingleSymbolRenderer"

    def restyle(self, *, source_layer=None, style_qml: str | None = None) -> bool:
        """Re-apply symbology to the drawing layer mid-session.

        Returns whether anything other than the fallback fill was used, so the
        caller can say plainly whether the project style actually took.
        """
        if source_layer is not None:
            self.source_layer = source_layer
        if style_qml is not None:
            self.style_qml = style_qml
        if self.polygon_layer is None:
            return False
        self._style_polygon_layer(self.polygon_layer)
        self.polygon_layer.triggerRepaint()
        return self._source_rules(self.polygon_layer) is not None

    def _make_frame_layer(self) -> None:
        """An unfilled box showing the limits of the section being drawn.

        Without a photo there is otherwise nothing on the canvas at all -- just
        seed columns floating in blank space -- and no way to tell where the
        wall ends. The box is the drawing surface: its left and right edges are
        the ends of the trace you drew, and its top and bottom are the section's
        vertical extent, whether that came from a placed photo or was typed in
        by hand.

        Read-only, so it cannot be dragged by accident, but still snappable, so
        units can be run hard against the edge of the section.
        """
        layer = QgsVectorLayer(
            f"Polygon?crs={self.crs.toWkt()}&field=label:string(64)",
            FRAME_LAYER_NAME, "memory",
        )
        if not layer.isValid():
            raise RuntimeError("Could not create the section frame layer")

        xmin, ymin, xmax, ymax = self.line.section_extent()
        feat = QgsFeature(layer.fields())
        feat.setGeometry(QgsGeometry.fromPolygonXY([[
            QgsPointXY(xmin, ymin), QgsPointXY(xmax, ymin),
            QgsPointXY(xmax, ymax), QgsPointXY(xmin, ymax),
            QgsPointXY(xmin, ymin),
        ]]))
        feat["label"] = (
            f"{self.line.length:.2f} m x {ymax - ymin:.2f} m  "
            f"({ymin:.2f}-{ymax:.2f} m)"
        )
        layer.dataProvider().addFeatures([feat])
        layer.updateExtents()

        layer.setRenderer(QgsSingleSymbolRenderer(QgsFillSymbol.createSimple({
            "color": "0,0,0,0",
            "outline_color": "0,180,255,255",
            "outline_width": "0.7",
            "outline_width_unit": "Point",
            "outline_style": "dash",
        })))
        layer.setReadOnly(True)

        QgsProject.instance().addMapLayer(layer, False)
        # Under the polygons, over the photo: a boundary you can see past.
        self._group().insertLayer(1, layer)
        self.frame_layer = layer

    def attach_photo(self, source_path: str, fit: Fit, *, workdir: Path | None = None) -> str:
        """Place the photo into section space and load it under the polygons."""
        self._workdir = workdir or Path(tempfile.mkdtemp(prefix="tkap_section_"))
        self._workdir.mkdir(parents=True, exist_ok=True)
        # Remembered so a saved section can name the photo it was built from.
        self.photo_source = str(source_path)
        stem = Path(source_path).stem
        out = self._workdir / f"{stem}_section.tif"

        write_placed_raster(source_path, out, fit, self.crs.toWkt())

        layer = QgsRasterLayer(str(out), PHOTO_LAYER_NAME)
        if not layer.isValid():
            raise RuntimeError(f"Placed raster failed to load: {out}")

        if self.photo_layer is not None:
            QgsProject.instance().removeMapLayer(self.photo_layer.id())
        QgsProject.instance().addMapLayer(layer, False)
        self._group().addLayer(layer)      # below the polygons
        self.photo_layer = layer
        return str(out)

    def reload_placed_photo(self, placed_path: str) -> None:
        """Re-attach an already-rectified raster, as saved with a section.

        No warping: the file on disk is the output of the original placement,
        so reopening a section costs nothing and cannot drift from what was
        drawn against.
        """
        layer = QgsRasterLayer(str(placed_path), PHOTO_LAYER_NAME)
        if not layer.isValid():
            raise RuntimeError(f"Could not load the placed photo: {placed_path}")
        if self.photo_layer is not None:
            QgsProject.instance().removeMapLayer(self.photo_layer.id())
        QgsProject.instance().addMapLayer(layer, False)
        self._group().addLayer(layer)
        self.photo_layer = layer

    # --------------------------------------------------------------- seeding --

    def seed(self, candidates: list[SUCandidate], *, replace: bool = True) -> int:
        """Write seed boxes for the included candidates. Returns the count.

        ``replace`` clears any unedited seeds first; polygons the user has
        already touched are kept, so re-seeding after changing the SU list never
        destroys work.
        """
        self.candidates = list(candidates)
        return self._write_seeds(self.candidates, replace=replace)

    def _write_seeds(
        self, candidates: list[SUCandidate], *, replace: bool = False
    ) -> int:
        """Write seed geometry without touching the roster.

        Kept separate because adding or re-seeding a single unit must not
        redefine which SUs the section contains -- an easy mistake to make when
        one method does both.
        """
        if self.polygon_layer is None:
            raise RuntimeError("Session has no polygon layer; call begin() first")
        layer = self.polygon_layer

        if replace:
            doomed = [
                f.id() for f in layer.getFeatures()
                if not f["edited"]
            ]
            if doomed:
                layer.dataProvider().deleteFeatures(doomed)

        kept = {f["su_id"] for f in layer.getFeatures()}
        feats = []
        for cand in candidates:
            if not cand.include or cand.su_id in kept:
                continue
            geom = _seed_geometry(cand)
            if geom.isEmpty():
                continue
            feat = QgsFeature(layer.fields())
            feat.setGeometry(geom)
            feat["su_id"] = cand.su_id
            feat["su_number"] = cand.su_number
            feat["label"] = cand.label
            feat["su_type"] = cand.su_type
            feat["subtype"] = cand.subtype
            feat["subsubtype"] = cand.subsubtype
            feat["seed_source"] = cand.seed_source.value
            feat["edited"] = False
            feat["notes"] = (
                "" if cand.on_trace else "caught by buffer only, does not cross the trace"
            )
            feats.append(feat)

        layer.dataProvider().addFeatures(feats)
        layer.updateExtents()
        layer.triggerRepaint()
        return len(feats)

    def add_candidates(self, candidates: list[SUCandidate]) -> int:
        """Bring further SUs into an already-open session.

        The roster is not fixed when the drawing window opens: an SU that was
        missed by the buffer, or one the excavator only recognises once the
        photo is up, can be added at any point.
        """
        if self.polygon_layer is None:
            raise RuntimeError("Session has no polygon layer; call begin() first")
        known = {c.su_id for c in self.candidates}
        fresh = [c for c in candidates if c.su_id not in known]
        self.candidates.extend(fresh)
        for c in fresh:
            c.include = True
        return self._write_seeds(fresh, replace=False)

    def reseed_one(self, candidate: SUCandidate) -> int:
        """Put a fresh seed box back for a single unit, leaving the roster be."""
        candidate.include = True
        return self._write_seeds([candidate], replace=False)

    def set_elevations(self, cand: SUCandidate, z_min: float, z_max: float) -> int:
        """Snap a unit's polygon to a given top and bottom.

        The horizontal extent is kept: chainage came from the plan geometry and
        is the one thing the section already knows for certain, so only the
        vertical is replaced. Each part keeps its own x range, so a unit the
        wall crosses twice stays two boxes.

        Session-only. Nothing is written to the SU layer, by design -- a number
        typed here is a drawing decision, not a survey observation.

        Returns the number of features reshaped.
        """
        layer = self.polygon_layer
        if layer is None:
            return 0
        if z_max <= z_min:
            raise ValueError("Top must be above the base")

        edited_idx = layer.fields().indexOf("edited")
        geometries: dict[int, QgsGeometry] = {}
        attributes: dict[int, dict] = {}

        for feat in layer.getFeatures():
            if feat["su_id"] != cand.su_id or not feat.hasGeometry():
                continue
            rings = []
            geom = feat.geometry()
            parts = geom.asGeometryCollection() if geom.isMultipart() else [geom]
            for part in parts:
                box = part.boundingBox()
                rings.append([[
                    QgsPointXY(box.xMinimum(), z_min),
                    QgsPointXY(box.xMaximum(), z_min),
                    QgsPointXY(box.xMaximum(), z_max),
                    QgsPointXY(box.xMinimum(), z_max),
                    QgsPointXY(box.xMinimum(), z_min),
                ]])
            if not rings:
                continue
            geometries[feat.id()] = QgsGeometry.fromMultiPolygonXY(rings)
            if edited_idx >= 0:
                # Mark it edited so a later re-seed leaves it alone.
                attributes[feat.id()] = {edited_idx: True}

        if not geometries:
            return 0

        provider = layer.dataProvider()
        provider.changeGeometryValues(geometries)
        if attributes:
            provider.changeAttributeValues(attributes)
        layer.updateExtents()
        layer.triggerRepaint()

        cand.alt_min, cand.alt_max = z_min, z_max
        return len(geometries)

    def set_hidden(self, su_id: int, hidden: bool) -> int:
        """Show or hide an SU's polygons. Hidden ones still snap.

        Returns the number of features changed. Uses the provider directly so
        this never lands in the user's undo stack -- toggling visibility is a
        view action, and having it interleaved with geometry edits would make
        Ctrl+Z unpredictable.
        """
        layer = self.polygon_layer
        if layer is None:
            return 0
        idx = layer.fields().indexOf(HIDDEN_FIELD)
        if idx < 0:
            return 0
        changes = {
            f.id(): {idx: 1 if hidden else 0}
            for f in layer.getFeatures()
            if f["su_id"] == su_id
        }
        if changes:
            layer.dataProvider().changeAttributeValues(changes)
            layer.triggerRepaint()
        return len(changes)

    def is_hidden(self, su_id: int) -> bool:
        layer = self.polygon_layer
        if layer is None:
            return False
        return any(
            f["hidden"] == 1 for f in layer.getFeatures() if f["su_id"] == su_id
        )

    def set_all_hidden(self, hidden: bool) -> int:
        total = 0
        for cand in self.candidates:
            total += self.set_hidden(cand.su_id, hidden)
        return total

    def isolate(self, su_id: int) -> None:
        """Show only this SU. The rest stay snappable but out of the way."""
        for cand in self.candidates:
            self.set_hidden(cand.su_id, cand.su_id != su_id)

    def restore_polygons(self, features, fields) -> int:
        """Load saved geometry into a fresh session, replacing any seeds.

        Matched by field name rather than position, so a section saved before a
        field was added still opens: anything missing is simply left null.
        """
        layer = self.polygon_layer
        if layer is None:
            raise RuntimeError("Session has no polygon layer; call begin() first")

        existing = [f.id() for f in layer.getFeatures()]
        if existing:
            layer.dataProvider().deleteFeatures(existing)

        names = set(layer.fields().names())
        rebuilt = []
        for saved in features:
            feat = QgsFeature(layer.fields())
            feat.setGeometry(saved.geometry())
            for field_name in fields.names():
                if field_name in names:
                    feat[field_name] = saved[field_name]
            rebuilt.append(feat)

        layer.dataProvider().addFeatures(rebuilt)
        layer.updateExtents()
        layer.triggerRepaint()
        return len(rebuilt)

    def remove_su(self, su_id: int) -> int:
        """Drop an SU's polygons from the session. Returns how many went."""
        if self.polygon_layer is None:
            return 0
        doomed = [f.id() for f in self.polygon_layer.getFeatures() if f["su_id"] == su_id]
        if doomed:
            self.polygon_layer.dataProvider().deleteFeatures(doomed)
            self.polygon_layer.triggerRepaint()
        return len(doomed)

    def has_polygon_for(self, su_id: int) -> bool:
        if self.polygon_layer is None:
            return False
        return any(f["su_id"] == su_id for f in self.polygon_layer.getFeatures())

    # ------------------------------------------------------------------ view --

    def section_rectangle(self, pad: float = 0.25) -> QgsRectangle:
        xmin, ymin, xmax, ymax = self.line.section_extent(pad=pad)
        return QgsRectangle(xmin, ymin, xmax, ymax)

    def zoom_to_section(self) -> None:
        canvas = self.iface.mapCanvas()
        canvas.setExtent(self.section_rectangle())
        canvas.refresh()

    def zoom_to_su(self, su_id: int, pad: float = 0.15) -> bool:
        if self.polygon_layer is None:
            return False
        boxes = [
            f.geometry().boundingBox()
            for f in self.polygon_layer.getFeatures()
            if f["su_id"] == su_id and f.hasGeometry()
        ]
        if not boxes:
            return False
        rect = boxes[0]
        for b in boxes[1:]:
            rect.combineExtentWith(b)
        rect.grow(pad)
        self.iface.mapCanvas().setExtent(rect)
        self.iface.mapCanvas().refresh()
        return True

    def start_editing(self) -> None:
        """Put the polygon layer in edit mode and make it current, so the
        vertex tool on the main toolbar acts on it immediately."""
        if self.polygon_layer is None:
            return
        self.iface.setActiveLayer(self.polygon_layer)
        if not self.polygon_layer.isEditable():
            self.polygon_layer.startEditing()

    # ------------------------------------------------------------------ meta --

    def default_title(self) -> str:
        if self.space_number:
            return f"Section of Space {self.space_number}"
        return f"Section {self.line.name}"

    def summary(self) -> str:
        included = sum(1 for c in self.candidates if c.include)
        return (
            f"{self.line.length:.2f} m long, azimuth {self.line.azimuth:.1f} deg - "
            f"{included} SUs"
        )
