"""Orchestration between the dialog and :mod:`phasing_core`.

Keeps the dialog free of layer-building logic and gives the QGIS Python console
a one-call entry point:

    from tkap_tools.phasing.phasing_runner import run_phasing, PhasingParams
    run_phasing(PhasingParams(layer=iface.activeLayer(), area_value="6"))
"""

from __future__ import annotations

from dataclasses import dataclass, field as dc_field
from typing import Callable, Dict, List, Optional

from .phasing_core import (
    BucketResult,
    bucket_features,
    build_filtered_layer,
    build_memory_layer,
    field_label,
    layer_name,
    targets_source_file,
    write_layers_to_geopackage,
)

#: Live layers over the original source, with an editable subsetString.
MODE_FILTERED = "filtered"
MODE_MEMORY = "memory"
MODE_GEOPACKAGE = "geopackage"


@dataclass
class PhasingParams:
    layer: object
    area_value: Optional[str] = None
    area_field: str = "area"
    space_phase_field: str = "space_phase"
    sunumber_field: str = "sunumber"
    mode: str = MODE_FILTERED
    gpkg_path: str = ""
    include_phase_name: bool = True
    pad_phase: int = 0
    add_provenance_fields: bool = True
    inherit_style: bool = True
    #: Filtered layers address the same table as the source, so they are made
    #: read-only by default to prevent an accidental edit session writing to
    #: production. Does not affect the editability of the filter itself.
    read_only_filtered: bool = True
    field_prefix: str = "Field"
    add_to_project: bool = True


@dataclass
class PhasingOutcome:
    result: Optional[BucketResult] = None
    layers: List = dc_field(default_factory=list)
    layer_names: List[str] = dc_field(default_factory=list)
    errors: List[str] = dc_field(default_factory=list)
    group_name: str = ""
    #: layer name -> the editable subsetString applied (filtered mode only).
    subsets: Dict[str, str] = dc_field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return not self.errors


def scan(params: PhasingParams) -> BucketResult:
    """Bucket the source layer without creating any output. Used for preview."""
    from qgis.core import QgsFeatureRequest

    layer = params.layer
    request = QgsFeatureRequest()
    wanted = [
        name
        for name in (
            params.space_phase_field,
            params.area_field,
            params.sunumber_field,
        )
        if name and layer.fields().lookupField(name) >= 0
    ]
    # Only pull the attributes we actually read -- matters for a remote PostGIS
    # layer, where fetching every column of every SU is needlessly slow.
    if wanted:
        request.setSubsetOfAttributes(wanted, layer.fields())
    request.setFlags(QgsFeatureRequest.NoGeometry)

    return bucket_features(
        layer.getFeatures(request),
        space_phase_field=params.space_phase_field,
        area_field=params.area_field or None,
        area_value=params.area_value,
        sunumber_field=params.sunumber_field,
        require_geometry=False,
    )


def run_phasing(
    params: PhasingParams,
    progress: Optional[Callable[[int, int, str], None]] = None,
) -> PhasingOutcome:
    """Build the per-phase layers and deliver them per ``params.mode``."""
    from qgis.core import QgsProject

    outcome = PhasingOutcome()
    layer = params.layer

    if layer is None:
        outcome.errors.append("No source layer selected.")
        return outcome

    for name, label in (
        (params.space_phase_field, "space_phase"),
        (params.area_field, "field designator"),
    ):
        if name and layer.fields().lookupField(name) < 0:
            outcome.errors.append(
                f"Layer '{layer.name()}' has no attribute '{name}' ({label})."
            )

    # Writing the first table to a GeoPackage uses CreateOrOverwriteFile, which
    # replaces the whole file. Refuse outright if that file is the source.
    if params.mode == MODE_GEOPACKAGE and targets_source_file(layer, params.gpkg_path):
        outcome.errors.append(
            f"Refusing to write to '{params.gpkg_path}': that is the file layer "
            f"'{layer.name()}' reads from, and writing would overwrite the "
            f"source data. Choose a different output path."
        )

    if outcome.errors:
        return outcome

    # Full pass, geometry included this time.
    result = bucket_features(
        layer.getFeatures(),
        space_phase_field=params.space_phase_field,
        area_field=params.area_field or None,
        area_value=params.area_value,
        sunumber_field=params.sunumber_field,
        require_geometry=True,
    )
    outcome.result = result

    if not result.buckets:
        return outcome

    label = field_label(params.area_value, params.field_prefix)
    outcome.group_name = label

    built = []
    total = len(result.buckets)
    for index, (key, features) in enumerate(result.buckets.items()):
        phase_name = result.label_for(key)
        name = layer_name(
            label,
            key,
            phase_name,
            include_phase_name=params.include_phase_name,
            pad_phase=params.pad_phase,
        )
        if progress:
            progress(index + 1, total, name)

        if params.mode == MODE_FILTERED:
            phase_layer, subset = build_filtered_layer(
                layer,
                name,
                key,
                phase_name,
                space_phase_field=params.space_phase_field,
                area_field=params.area_field or None,
                area_value=params.area_value,
                add_provenance=params.add_provenance_fields,
                inherit_style=params.inherit_style,
                read_only=params.read_only_filtered,
            )
            if phase_layer is None:
                outcome.errors.append(
                    f"{name}: provider rejected the filter -- {subset}"
                )
                continue
            outcome.subsets[name] = subset
            built.append(phase_layer)
        else:
            built.append(
                build_memory_layer(
                    layer,
                    name,
                    features,
                    key,
                    phase_name,
                    add_provenance=params.add_provenance_fields,
                    inherit_style=params.inherit_style,
                )
            )

    if outcome.errors:
        return outcome

    outcome.layers = built
    outcome.layer_names = [lyr.name() for lyr in built]

    if params.mode == MODE_GEOPACKAGE:
        if not params.gpkg_path:
            outcome.errors.append("No GeoPackage path given.")
            return outcome
        errors = write_layers_to_geopackage(
            built, params.gpkg_path, overwrite_file=True, progress=progress
        )
        outcome.errors.extend(errors)
        if not errors and params.add_to_project:
            _add_geopackage_layers(params.gpkg_path, outcome.layer_names, label)
    elif params.add_to_project:
        _add_layers_to_group(built, label)
        project = QgsProject.instance()
        project.setDirty(True)

    return outcome


def _layer_tree_group(name: str):
    from qgis.core import QgsProject

    root = QgsProject.instance().layerTreeRoot()
    existing = root.findGroup(name)
    if existing is not None:
        return existing
    return root.insertGroup(0, name)


def _add_layers_to_group(layers: List, group_name: str) -> None:
    """Add layers to the project inside a named group, in bucket order.

    Order matters twice over. Buckets are sorted by (space, phase), and the
    layer tree preserves insertion order, so Phase10 sits after Phase9 rather
    than sorting alphabetically between Phase1 and Phase2. And because higher
    phase numbers are *earlier* in time, ascending order puts the latest phase
    at the top of the group, mirroring the physical section.
    """
    from qgis.core import QgsProject

    project = QgsProject.instance()
    group = _layer_tree_group(group_name)
    for layer in layers:
        project.addMapLayer(layer, False)
        group.addLayer(layer)


def _add_geopackage_layers(gpkg_path: str, names: List[str], group_name: str) -> None:
    from qgis.core import QgsProject, QgsVectorLayer

    project = QgsProject.instance()
    group = _layer_tree_group(group_name)
    for name in names:
        uri = f"{gpkg_path}|layername={name}"
        layer = QgsVectorLayer(uri, name, "ogr")
        if layer.isValid():
            project.addMapLayer(layer, False)
            group.addLayer(layer)


def summarise(result: BucketResult, area_value: str = "", prefix: str = "Field") -> str:
    """Human-readable preview line for the dialog."""
    if result is None:
        return ""
    label = field_label(area_value, prefix) if area_value else "All fields"
    if not result.buckets:
        if result.in_scope == 0:
            return f"{label}: no SUs match that value."
        return (
            f"{label}: {result.in_scope} SUs, but none carry a phase "
            f"-- nothing to output."
        )

    spaces = result.spaces
    parts = [
        f"{label}: {len(result.buckets)} layers from {result.phased} phased SUs "
        f"across {len(spaces)} space(s) {spaces}.",
        f"{result.unphased} of {result.in_scope} SUs here have no phase; skipped.",
    ]
    if result.total_output_features != result.phased:
        parts.append(
            f"{result.total_output_features} output features -- an SU repeats in "
            f"every space and phase it lists."
        )
    if result.warnings:
        parts.append(f"{len(result.warnings)} warning(s).")
    return "\n".join(parts)
