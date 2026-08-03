"""Core phasing logic for the TKAP Stratigraphic Phasing plugin.

This module deliberately has **no QGIS UI dependencies**, and the parsing and
bucketing half has no QGIS dependency at all, so it can be exercised from a
plain Python interpreter (see ``tests/test_phasing_core.py``) as well as from
the QGIS Python console.

The QGIS-dependent helpers (layer construction, GeoPackage writing) are kept
below the pure section and import ``qgis.core`` lazily.
"""

from __future__ import annotations

import re
from collections import Counter, OrderedDict
from dataclasses import dataclass, field as dc_field
from typing import Callable, Dict, Iterable, List, Optional

# --------------------------------------------------------------------------
# Parsing
# --------------------------------------------------------------------------

#: One ``Sp.<space>.<phase> (<Phase Name>)`` entry.
ENTRY_RE = re.compile(
    r"Sp\.(?P<space>\d+)\.(?P<phase>\d+)\s*\((?P<phase_name>[^)]*)\)"
)

#: Characters that legitimately separate entries; anything else left over after
#: stripping matched entries is reported as an unparsed fragment.
_SEPARATOR_RE = re.compile(r"[,;\s]+")


@dataclass(frozen=True, order=True)
class PhaseKey:
    """Grouping key for one output layer: a phase *within a single space*.

    Phase numbers are numbered independently per space in TKAP data -- Sp.33.1
    is 'Destruction' while Sp.34.1 is 'Construction' -- so the space number is
    part of the key. See ``docs/DECISIONS.md``.
    """

    space: int
    phase: int


@dataclass(frozen=True)
class SpacePhaseEntry:
    """One parsed ``Sp.<space>.<phase> (<name>)`` entry."""

    space: int
    phase: int
    phase_name: str

    @property
    def key(self) -> PhaseKey:
        return PhaseKey(self.space, self.phase)


def coerce_text(value) -> str:
    """Normalise a raw attribute value to a plain string.

    Handles ``None``, PyQGIS ``NULL`` (a ``QVariant``), and numeric field values
    without importing anything from QGIS.
    """
    if value is None:
        return ""
    is_null = getattr(value, "isNull", None)
    if callable(is_null):
        try:
            if is_null():
                return ""
        except TypeError:
            pass
    if isinstance(value, str):
        return value.strip()
    text = str(value).strip()
    if text.upper() == "NULL":
        return ""
    return text


def parse_space_phase(value) -> List[SpacePhaseEntry]:
    """Parse one SU's ``space_phase`` string into its entries.

    Returns an empty list for null/blank input rather than raising, so callers
    can treat 'unphased SU' as an ordinary case.
    """
    text = coerce_text(value)
    if not text:
        return []
    return [
        SpacePhaseEntry(
            space=int(m.group("space")),
            phase=int(m.group("phase")),
            phase_name=m.group("phase_name").strip(),
        )
        for m in ENTRY_RE.finditer(text)
    ]


def unparsed_fragments(value) -> List[str]:
    """Return chunks of a ``space_phase`` string the entry regex did not consume.

    A non-empty result means the value was malformed *or* truncated upstream
    (a 254-char shapefile export will chop the final entry mid-name, e.g.
    ``... Sp.33.1 (Destructi``). Such a tail is silently invisible to
    :func:`parse_space_phase`, so it is surfaced as a warning instead.
    """
    text = coerce_text(value)
    if not text:
        return []
    remainder = ENTRY_RE.sub("\x00", text)
    return [frag for frag in _SEPARATOR_RE.split(remainder) if frag and frag != "\x00"]


# --------------------------------------------------------------------------
# Bucketing
# --------------------------------------------------------------------------

WARN_UNPARSED = "unparsed"
WARN_NAME_CONFLICT = "name_conflict"
WARN_NO_GEOMETRY = "no_geometry"


@dataclass
class Warning_:
    """A non-fatal data problem worth showing the operator."""

    kind: str
    message: str
    sunumber: str = ""

    def __str__(self) -> str:  # pragma: no cover - display helper
        prefix = f"SU {self.sunumber}: " if self.sunumber else ""
        return f"{prefix}{self.message}"


@dataclass
class BucketResult:
    """Everything one pass over the source layer produced."""

    #: PhaseKey -> list of features, ordered by (space, phase).
    buckets: "OrderedDict[PhaseKey, List]" = dc_field(default_factory=OrderedDict)
    #: PhaseKey -> the phase name used for labelling (first seen wins).
    names: Dict[PhaseKey, str] = dc_field(default_factory=dict)
    #: PhaseKey -> every name seen, when they disagreed.
    name_conflicts: Dict[PhaseKey, Counter] = dc_field(default_factory=dict)
    warnings: List[Warning_] = dc_field(default_factory=list)

    scanned: int = 0
    in_scope: int = 0
    phased: int = 0
    unphased: int = 0
    #: SUs listed for more than one phase within a single space (long-lived
    #: architecture). Included in every phase listed -- confirmed behaviour.
    multi_phase_sus: List[str] = dc_field(default_factory=list)
    #: (fid, PhaseKey) pairs dropped because the entry was listed twice.
    duplicate_entries: int = 0

    @property
    def spaces(self) -> List[int]:
        return sorted({k.space for k in self.buckets})

    @property
    def total_output_features(self) -> int:
        return sum(len(v) for v in self.buckets.values())

    def label_for(self, key: PhaseKey) -> str:
        return self.names.get(key, "")


def _attr(feature, name: str):
    """Read an attribute by name from a QgsFeature or a plain mapping."""
    try:
        return feature[name]
    except (KeyError, IndexError, TypeError):
        return None


def _fid(feature) -> object:
    getter = getattr(feature, "id", None)
    if callable(getter):
        return getter()
    return _attr(feature, "id")


def bucket_features(
    features: Iterable,
    space_phase_field: str,
    area_field: Optional[str] = None,
    area_value: Optional[str] = None,
    sunumber_field: str = "sunumber",
    require_geometry: bool = True,
) -> BucketResult:
    """Single pass over ``features``, grouping them into ``PhaseKey`` buckets.

    ``features`` may be any iterable of objects supporting ``feature[name]`` and
    ``feature.id()`` -- a ``QgsVectorLayer.getFeatures()`` iterator in
    production, or plain dict-backed stubs in tests.

    An SU listed for several phases within one space is added to **every** phase
    it lists; that is deliberate (long-lived walls stay on each plan they stood
    for), not a bug. An SU appearing in several *spaces* likewise lands in each
    space's bucket.
    """
    result = BucketResult()
    scoped_area = coerce_text(area_value) if area_value is not None else None
    seen_pairs = set()

    for feature in features:
        result.scanned += 1

        if area_field and scoped_area is not None:
            if coerce_text(_attr(feature, area_field)) != scoped_area:
                continue
        result.in_scope += 1

        raw = _attr(feature, space_phase_field)
        sunumber = coerce_text(_attr(feature, sunumber_field)) or str(_fid(feature))

        entries = parse_space_phase(raw)
        # Report all unparsable leftovers for a feature as one warning, so a
        # single truncated value does not produce a wall of messages.
        fragments = unparsed_fragments(raw)

        if not entries:
            result.unphased += 1
            # A value that is non-blank but yielded nothing is malformed, not
            # merely unphased -- report it.
            if fragments:
                result.warnings.append(
                    Warning_(
                        WARN_UNPARSED,
                        f"could not parse {' '.join(fragments)!r} in "
                        f"{space_phase_field}; SU excluded from all phases",
                        sunumber,
                    )
                )
            continue

        if fragments:
            result.warnings.append(
                Warning_(
                    WARN_UNPARSED,
                    f"ignored unparsable trailing text {' '.join(fragments)!r} in "
                    f"{space_phase_field} (value truncated upstream?)",
                    sunumber,
                )
            )

        if require_geometry:
            has_geom = getattr(feature, "hasGeometry", None)
            if callable(has_geom) and not has_geom():
                result.warnings.append(
                    Warning_(WARN_NO_GEOMETRY, "has no geometry; skipped", sunumber)
                )
                continue

        result.phased += 1

        by_space: Dict[int, set] = {}
        fid = _fid(feature)
        for entry in entries:
            by_space.setdefault(entry.space, set()).add(entry.phase)

            key = entry.key
            pair = (fid, key)
            if pair in seen_pairs:
                result.duplicate_entries += 1
                continue
            seen_pairs.add(pair)

            result.buckets.setdefault(key, []).append(feature)

            existing = result.names.get(key)
            if existing is None:
                result.names[key] = entry.phase_name
            elif existing != entry.phase_name:
                result.name_conflicts.setdefault(key, Counter([existing]))
                result.name_conflicts[key][entry.phase_name] += 1

        if any(len(phases) > 1 for phases in by_space.values()):
            result.multi_phase_sus.append(sunumber)

    for key, counter in result.name_conflicts.items():
        options = ", ".join(f"{n!r} x{c}" for n, c in counter.most_common())
        result.warnings.append(
            Warning_(
                WARN_NAME_CONFLICT,
                f"Sp.{key.space}.{key.phase} has conflicting phase names ({options}); "
                f"using {result.names[key]!r} for the layer name",
            )
        )

    result.buckets = OrderedDict(sorted(result.buckets.items(), key=lambda kv: kv[0]))
    return result


# --------------------------------------------------------------------------
# Naming
# --------------------------------------------------------------------------

_UNSAFE_NAME_RE = re.compile(r"[^0-9A-Za-z_]+")


def sanitise(text: str) -> str:
    """Make a fragment safe for a layer / GeoPackage table name."""
    cleaned = _UNSAFE_NAME_RE.sub("_", coerce_text(text)).strip("_")
    return cleaned


def layer_name(
    field_label: str,
    key: PhaseKey,
    phase_name: str = "",
    include_phase_name: bool = True,
    pad_phase: int = 0,
) -> str:
    """Build the output layer name, e.g. ``Field6_Sp34_Phase1_Construction``.

    ``pad_phase`` zero-pads the phase number to that width; 0 disables padding.
    Layers are inserted into the layer tree in numeric order regardless, so
    padding is only needed if you also care about alphabetical sorting
    elsewhere (e.g. browsing the GeoPackage in another tool).
    """
    parts = [sanitise(field_label) or "Field", f"Sp{key.space}"]
    number = str(key.phase).zfill(pad_phase) if pad_phase else str(key.phase)
    parts.append(f"Phase{number}")
    if include_phase_name and coerce_text(phase_name):
        parts.append(sanitise(phase_name))
    return "_".join(p for p in parts if p)


#: Phase numbering runs *downward in time*: phase 1 is the latest/uppermost,
#: higher numbers are earlier. Confirmed with the user 2026-08-02 -- contexts are
#: numbered as they are excavated, so Construction sits at the highest number.
#: Layers are therefore inserted in ascending phase order, putting the latest
#: phase at the top of the layer tree, mirroring the physical section.
HIGHER_PHASE_IS_EARLIER = True


# --------------------------------------------------------------------------
# SQL filter construction (pure -- no QGIS needed)
# --------------------------------------------------------------------------


def sql_literal(value) -> str:
    """Quote a value as a SQL string literal."""
    return "'" + coerce_text(value).replace("'", "''") + "'"


def sql_identifier(name: str) -> str:
    """Quote an attribute name for use in a provider subset string."""
    return '"' + str(name).replace('"', '""') + '"'


def phase_clause(space_phase_field: str, key: PhaseKey) -> str:
    """SQL matching SUs listed for one ``(space, phase)``.

    ``Sp.34.1 (`` is an exact stand-in for the entry regex: the trailing space
    and parenthesis stop ``Sp.34.1`` matching ``Sp.34.10``, and the leading dot
    stops ``Sp.6`` matching ``Sp.63``. Verified against every one of the 69
    (space, phase) pairs in the sample export -- the SQL and the parser select
    identical feature sets.
    """
    pattern = f"%Sp.{key.space}.{key.phase} (%"
    return f"{sql_identifier(space_phase_field)} LIKE {sql_literal(pattern)}"


def build_subset_string(
    space_phase_field: str,
    key: PhaseKey,
    area_field: Optional[str] = None,
    area_value: Optional[str] = None,
    existing: str = "",
) -> str:
    """Build the editable provider filter for one phase layer.

    Produces e.g.::

        "area" = '6' AND "space_phase" LIKE '%Sp.34.1 (%'

    which opens in the QGIS Query Builder so extra clauses can be added later.
    Any filter already on the source layer is preserved as a bracketed term.
    """
    clauses = []
    if coerce_text(existing):
        clauses.append(f"({coerce_text(existing)})")
    if area_field and coerce_text(area_value):
        clauses.append(
            f"{sql_identifier(area_field)} = {sql_literal(area_value)}"
        )
    clauses.append(phase_clause(space_phase_field, key))
    return " AND ".join(clauses)


# --------------------------------------------------------------------------
# Title templating (pure -- no QGIS needed)
# --------------------------------------------------------------------------

#: Reverse of :func:`layer_name`, for recovering tokens from an output layer.
LAYER_NAME_RE = re.compile(
    r"^(?P<field>.+?)_Sp(?P<space>\d+)_Phase(?P<phase>\d+)(?:_(?P<phase_name>.+))?$"
)

#: Default layout title. Reads as 'Field 6 - Space 34, Phase 1 (Construction)'.
DEFAULT_TITLE_TEMPLATE = "{field} - Space {space}, Phase {phase} ({phase_name})"

_TRAILING_DIGITS_RE = re.compile(r"^(?P<word>[A-Za-z]+)(?P<number>\d+)$")


def humanise_field(label: str) -> str:
    """'Field6' -> 'Field 6'; 'Sounding' -> 'Sounding'."""
    text = coerce_text(label)
    match = _TRAILING_DIGITS_RE.match(text)
    if match:
        return f"{match.group('word')} {match.group('number')}"
    return text


def parse_layer_name(name: str) -> Dict[str, str]:
    """Recover {field, space, phase, phase_name} from an output layer name.

    Used as a fallback when the provenance columns are unavailable, so titles
    still work if they were switched off.
    """
    text = coerce_text(name)
    match = LAYER_NAME_RE.match(text)
    if not match:
        return {"field": text, "space": "", "phase": "", "phase_name": "", "layer": text}
    return {
        "field": humanise_field(match.group("field")),
        "space": str(int(match.group("space"))),
        # Strip any zero padding so '{phase}' reads '9', not '09'.
        "phase": str(int(match.group("phase"))),
        "phase_name": (match.group("phase_name") or "").replace("_", " "),
        "layer": text,
    }


class _SafeTokens(dict):
    """Leaves unknown placeholders visible rather than raising or blanking."""

    def __missing__(self, key):
        return "{" + key + "}"


def format_title(template: str, tokens: Dict[str, str]) -> str:
    """Render a title template. Unknown placeholders are left as-is.

    A literal ``\\n`` in the template becomes a real line break, so a multi-line
    title block can be written in a single-line text box.
    """
    text = coerce_text(template)
    if not text:
        return ""
    text = text.replace("\\n", "\n")
    try:
        return text.format_map(_SafeTokens(tokens))
    except (ValueError, IndexError):
        # Unbalanced braces in a hand-typed template -- show it literally rather
        # than aborting the whole export.
        return text


def field_label(area_value: str, prefix: str = "Field") -> str:
    """Turn an ``area`` attribute value into a human label.

    TKAP stores the field designator as a bare value ('1'..'10', 'Sounding'),
    so numeric values get the ``Field`` prefix and anything else is used as-is.
    """
    text = coerce_text(area_value)
    if not text:
        return prefix
    if text.isdigit():
        return f"{prefix}{text}"
    return sanitise(text)


# --------------------------------------------------------------------------
# Companion layer matching (pure -- no QGIS needed)
# --------------------------------------------------------------------------

#: Finds a ``Sp<space> ... Phase<phase>`` token anywhere in a layer name.
#: Deliberately looser than :data:`LAYER_NAME_RE`, so a companion layer can be
#: called ``Features_Sp34_Phase1``, ``Sp.34 Phase 1`` or
#: ``Field6_Sp34_Phase1_Construction`` and still line up with its phase layer.
PHASE_TOKEN_RE = re.compile(
    r"Sp\.?\s*(?P<space>\d+)[\s._-]*Phase\.?\s*(?P<phase>\d+)", re.IGNORECASE
)


def phase_key_from_name(name: str) -> Optional[PhaseKey]:
    """Recover the ``(space, phase)`` a layer name refers to, or ``None``."""
    match = PHASE_TOKEN_RE.search(coerce_text(name))
    if not match:
        return None
    return PhaseKey(int(match.group("space")), int(match.group("phase")))


@dataclass
class CompanionMatch:
    """Result of pairing phase layers with a companion group's layers."""

    #: phase layer name -> companion layer name.
    pairs: Dict[str, str] = dc_field(default_factory=dict)
    #: Phase layer names with no companion at all.
    unmatched: List[str] = dc_field(default_factory=list)
    #: Companion layer names that matched no phase layer.
    unused: List[str] = dc_field(default_factory=list)
    #: phase layer name -> every companion that matched, when several did.
    ambiguous: Dict[str, List[str]] = dc_field(default_factory=dict)

    @property
    def matched(self) -> int:
        return len(self.pairs)


def match_companion_layers(
    phase_names: Iterable[str], companion_names: Iterable[str]
) -> CompanionMatch:
    """Pair each phase layer name with a companion layer of the same phase.

    Three passes, most specific first, so an operator who names the companions
    identically to the phase layers gets an exact match, while looser naming
    still works:

    1. Exact name, ignoring case and surrounding whitespace.
    2. Name run through :func:`sanitise`, so ``Field 6 Sp34 Phase1`` pairs with
       ``Field_6_Sp34_Phase1``.
    3. The ``(space, phase)`` key parsed out of both names, so a companion may
       be called anything as long as it carries ``Sp<n>`` and ``Phase<n>``.

    A phase layer with no companion is reported, not treated as an error -- not
    every phase has features, and the export should still produce a plan.
    """
    phase_names = list(phase_names)
    companion_names = list(companion_names)

    by_exact: Dict[str, List[str]] = {}
    by_sanitised: Dict[str, List[str]] = {}
    by_key: Dict[PhaseKey, List[str]] = {}
    for name in companion_names:
        by_exact.setdefault(coerce_text(name).casefold(), []).append(name)
        by_sanitised.setdefault(sanitise(name).casefold(), []).append(name)
        key = phase_key_from_name(name)
        if key is not None:
            by_key.setdefault(key, []).append(name)

    result = CompanionMatch()
    used = set()
    for phase_name in phase_names:
        candidates: List[str] = []
        for lookup, probe in (
            (by_exact, coerce_text(phase_name).casefold()),
            (by_sanitised, sanitise(phase_name).casefold()),
        ):
            if probe:
                candidates = lookup.get(probe, [])
            if candidates:
                break
        if not candidates:
            key = phase_key_from_name(phase_name)
            if key is not None:
                candidates = by_key.get(key, [])
        if not candidates:
            result.unmatched.append(phase_name)
            continue
        if len(candidates) > 1:
            # Report rather than guess silently; the first is still used so the
            # export goes ahead.
            result.ambiguous[phase_name] = list(candidates)
        result.pairs[phase_name] = candidates[0]
        used.add(candidates[0])

    result.unused = [name for name in companion_names if name not in used]
    return result


# --------------------------------------------------------------------------
# QGIS-dependent output
# --------------------------------------------------------------------------

#: Extra provenance columns written onto every output layer.
PROVENANCE_FIELDS = (
    ("ph_space", "Space number this layer represents"),
    ("ph_num", "Phase number within that space"),
    ("ph_name", "Phase name (Construction / Occupation / ...)"),
)


def _require_qgis():
    from qgis.core import (  # noqa: F401
        QgsFeature,
        QgsField,
        QgsFields,
        QgsVectorLayer,
        QgsWkbTypes,
    )

    return dict(
        QgsFeature=QgsFeature,
        QgsField=QgsField,
        QgsFields=QgsFields,
        QgsVectorLayer=QgsVectorLayer,
        QgsWkbTypes=QgsWkbTypes,
    )


def build_memory_layer(
    source_layer,
    name: str,
    features: Iterable,
    key: PhaseKey,
    phase_name: str,
    add_provenance: bool = True,
    inherit_style: bool = True,
):
    """Create an in-memory ``QgsVectorLayer`` holding ``features``.

    Geometry and attributes are copied from the source; when ``add_provenance``
    is set, ``ph_space`` / ``ph_num`` / ``ph_name`` are appended so a layer
    remains self-describing once written out or re-merged.
    """
    q = _require_qgis()
    from qgis.PyQt.QtCore import QVariant

    geom_type = q["QgsWkbTypes"].displayString(source_layer.wkbType())
    crs = source_layer.crs().authid()
    uri = f"{geom_type}?crs={crs}" if crs else geom_type
    layer = q["QgsVectorLayer"](uri, name, "memory")

    provider = layer.dataProvider()
    out_fields = [f for f in source_layer.fields()]
    if add_provenance:
        out_fields = out_fields + [
            q["QgsField"]("ph_space", QVariant.Int),
            q["QgsField"]("ph_num", QVariant.Int),
            # Positional args only: QgsField(name, type, typeName, len) -- the
            # sip bindings do not reliably accept `len=` as a keyword.
            q["QgsField"]("ph_name", QVariant.String, "", 64),
        ]
    provider.addAttributes(out_fields)
    layer.updateFields()

    source_count = len(source_layer.fields())
    out = []
    for feature in features:
        new = q["QgsFeature"](layer.fields())
        new.setGeometry(feature.geometry())
        attrs = list(feature.attributes())
        # Guard against a source feature carrying fewer attributes than the
        # layer declares (possible with some providers).
        attrs = (attrs + [None] * source_count)[:source_count]
        if add_provenance:
            attrs = attrs + [key.space, key.phase, phase_name]
        new.setAttributes(attrs)
        out.append(new)

    provider.addFeatures(out)
    layer.updateExtents()
    if inherit_style:
        copy_style(source_layer, layer)
    return layer


def source_file_path(layer) -> str:
    """Return the on-disk file a layer reads from, or '' for database sources.

    ``QgsVectorLayer.source()`` for a file provider looks like
    ``C:/data/SUs.gpkg|layername=SUs``; the path is everything before the first
    pipe. PostGIS and other database sources have no file, so return ''.
    """
    import os

    try:
        source = layer.source()
    except Exception:  # pragma: no cover - defensive
        return ""
    if not source:
        return ""
    candidate = source.split("|", 1)[0].strip()
    # A database URI contains connection keywords, never a usable path.
    if any(token in source for token in ("dbname=", "service=", "host=")):
        return ""
    if not candidate:
        return ""
    try:
        return os.path.abspath(candidate)
    except Exception:  # pragma: no cover - defensive
        return ""


def targets_source_file(layer, output_path: str) -> bool:
    """True if writing to ``output_path`` would clobber the layer's own file.

    Writing the first table to a GeoPackage uses ``CreateOrOverwriteFile``,
    which replaces the entire file. If the operator pointed the output at the
    very file the SUs are read from, that would destroy the source data.
    """
    import os

    source = source_file_path(layer)
    if not source or not output_path:
        return False
    try:
        target = os.path.abspath(output_path.strip())
    except Exception:  # pragma: no cover - defensive
        return False
    # Windows paths are case-insensitive; normcase makes the comparison safe on
    # both platforms.
    return os.path.normcase(source) == os.path.normcase(target)


def copy_style(source_layer, target_layer) -> bool:
    """Copy the source layer's full appearance onto ``target_layer``.

    Uses the named-style round trip so renderer, labelling, opacity, blend mode
    and diagrams all carry over, rather than just the renderer. Falls back to
    cloning the renderer alone if the style will not import (which can happen
    when field sets differ).
    """
    from qgis.PyQt.QtXml import QDomDocument

    try:
        document = QDomDocument()
        source_layer.exportNamedStyle(document)
        message, ok = target_layer.importNamedStyle(document)
        if ok:
            target_layer.triggerRepaint()
            return True
    except Exception:  # pragma: no cover - depends on QGIS build
        pass

    try:
        renderer = source_layer.renderer()
        if renderer is not None:
            target_layer.setRenderer(renderer.clone())
        target_layer.setOpacity(source_layer.opacity())
        labeling = source_layer.labeling()
        if labeling is not None:
            target_layer.setLabeling(labeling.clone())
            target_layer.setLabelsEnabled(source_layer.labelsEnabled())
        target_layer.triggerRepaint()
        return True
    except Exception:  # pragma: no cover - defensive
        return False


def add_provenance_virtual_fields(layer, key: PhaseKey, phase_name: str) -> None:
    """Attach ph_space / ph_num / ph_name to a provider-backed layer.

    Virtual fields live in the QGIS project, not in PostGIS, so a filtered layer
    can carry the same provenance columns as a materialised one without the
    plugin ever writing to the database.
    """
    from qgis.core import QgsField
    from qgis.PyQt.QtCore import QVariant

    try:
        layer.addExpressionField(str(key.space), QgsField("ph_space", QVariant.Int))
        layer.addExpressionField(str(key.phase), QgsField("ph_num", QVariant.Int))
        layer.addExpressionField(
            sql_literal(phase_name), QgsField("ph_name", QVariant.String, "", 64)
        )
    except Exception:  # pragma: no cover - defensive
        pass


def build_filtered_layer(
    source_layer,
    name: str,
    key: PhaseKey,
    phase_name: str,
    space_phase_field: str,
    area_field: Optional[str] = None,
    area_value: Optional[str] = None,
    add_provenance: bool = True,
    inherit_style: bool = True,
    read_only: bool = True,
):
    """Create a live layer over the same source, filtered to one phase.

    No features are copied: the layer points at the original PostGIS table (or
    file) with an editable ``subsetString``. Returns ``(layer, subset)``, where
    ``layer`` is ``None`` if the provider rejected the filter.

    Because the layer addresses the *same* table as the source, an edit session
    started on it would write to the production database. ``read_only`` defaults
    to True and sets the layer's read-only flag, which disables Toggle Editing
    in the QGIS UI. The flag is saved with the project. This guards the data;
    it does not restrict the ``subsetString``, which stays freely editable in
    the Query Builder.
    """
    from qgis.core import QgsVectorLayer

    subset = build_subset_string(
        space_phase_field,
        key,
        area_field=area_field,
        area_value=area_value,
        existing=source_layer.subsetString(),
    )

    layer = QgsVectorLayer(
        source_layer.source(), name, source_layer.providerType()
    )
    if not layer.isValid():
        return None, subset

    # setSubsetString replaces whatever filter came in via the source URI, which
    # is why any pre-existing filter is folded into `subset` above.
    if not layer.setSubsetString(subset):
        return None, subset

    if inherit_style:
        copy_style(source_layer, layer)
    if add_provenance:
        add_provenance_virtual_fields(layer, key, phase_name)
    if read_only:
        # Set last: addExpressionField on a read-only layer can be refused.
        layer.setReadOnly(True)
    return layer, subset


def write_layers_to_geopackage(
    layers: List,
    gpkg_path: str,
    overwrite_file: bool = True,
    progress: Optional[Callable[[int, int, str], None]] = None,
) -> List[str]:
    """Write ``layers`` as separate tables inside a single GeoPackage.

    The first layer either creates/overwrites the file or is appended as a new
    table depending on ``overwrite_file``; every subsequent layer uses
    ``CreateOrOverwriteLayer`` so they accumulate in one ``.gpkg`` rather than
    each replacing the whole file.

    Returns a list of error strings (empty on success).
    """
    from qgis.core import QgsVectorFileWriter, QgsProject

    errors: List[str] = []
    transform_context = QgsProject.instance().transformContext()
    total = len(layers)

    for index, layer in enumerate(layers):
        options = QgsVectorFileWriter.SaveVectorOptions()
        options.driverName = "GPKG"
        options.layerName = layer.name()
        options.fileEncoding = "UTF-8"
        if index == 0 and overwrite_file:
            options.actionOnExistingFile = (
                QgsVectorFileWriter.CreateOrOverwriteFile
            )
        else:
            options.actionOnExistingFile = (
                QgsVectorFileWriter.CreateOrOverwriteLayer
            )

        if progress:
            progress(index + 1, total, layer.name())

        result = QgsVectorFileWriter.writeAsVectorFormatV3(
            layer, gpkg_path, transform_context, options
        )
        # V3 returns (errorCode, errorMessage, newFilename, newLayer); older
        # builds return a 2-tuple.
        code = result[0]
        message = result[1] if len(result) > 1 else ""
        if code != QgsVectorFileWriter.NoError:
            errors.append(f"{layer.name()}: {message or code}")

    return errors
