"""Turn a parsed plan file into one pair of live filtered layers per plan.

The split tool derives its layers from the data; this derives them from the plan
file. Both end up in the same place -- a group of layers, one per plan -- which
is what lets *Export Phase Plans* walk either without knowing where the queries
came from. A plan is either an editable copy of the features its query selects
or a live filtered view of them; see ``MODE_COPY`` / ``MODE_LIVE``.

Each plan produces up to two layers, both carrying the plan's name so the
export dialog's existing companion matching pairs them:

    Features group  3.1 Hellenistic Pitting and Abandonment   (Features layer)
    Plans group     3.1 Hellenistic Pitting and Abandonment   (SU layer)

The Features group is put above the Plans group, in that order, because a
feature is recorded *in* the SUs around it and has to draw on top of them.
"""

from __future__ import annotations

from dataclasses import dataclass, field as dc_field
from typing import Callable, Dict, List, Optional

from .phasing_core import build_memory_layer, build_query_layer, combine_filters
from .plan_file import PlanFile, PlanMap, layer_properties

#: Suffixes for the two output groups, appended to the plan file's stem.
GROUP_SUFFIX_PLANS = "Plans"
GROUP_SUFFIX_FEATURES = "Features"

#: What a plan layer *is*, which decides where an edit to one goes.
#:
#: ``MODE_COPY`` (the default) materialises the selected features into an
#: in-memory layer. Reshape a polygon for a figure and nothing can reach the
#: database, because there is no longer a connection to it -- at the cost of the
#: plan no longer tracking the source, its filter no longer being editable in
#: the Query Builder, and the copy living only as long as the QGIS session.
#:
#: ``MODE_LIVE`` is the filtered view: still pointed at the source table, filter
#: still editable, tracking the data. Edits to one are written **to the source
#: table**, which is the whole reason it is not the default.
MODE_COPY = "copy"
MODE_LIVE = "live"


@dataclass
class PlanCheck:
    """What one plan's queries do against the layers they were pointed at."""

    plan: PlanMap
    layer_name: str = ""
    su_count: Optional[int] = None
    feature_count: Optional[int] = None
    su_error: str = ""
    feature_error: str = ""
    #: Held between the check and the build so the layers are made once. The
    #: check has to instantiate them to count honestly -- a provider filter is
    #: SQL, and only the provider can say what it returns.
    su_layer: object = None
    feature_layer: object = None

    @property
    def ok(self) -> bool:
        return not (self.su_error or self.feature_error)

    @property
    def empty(self) -> bool:
        """True when the plan draws nothing at all -- usually a typo'd SU list."""
        return not (self.su_count or self.feature_count)

    def status(self) -> str:
        """One line for the pre-flight table."""
        if self.su_error or self.feature_error:
            return "; ".join(p for p in (self.su_error, self.feature_error) if p)
        if self.empty:
            return "matches nothing"
        return "ok"


@dataclass
class PlanSetOutcome:
    checks: List[PlanCheck] = dc_field(default_factory=list)
    errors: List[str] = dc_field(default_factory=list)
    plans_group: str = ""
    features_group: str = ""
    #: layer name -> the editable subsetString applied.
    subsets: Dict[str, str] = dc_field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return not self.errors

    @property
    def built(self) -> List[PlanCheck]:
        return [check for check in self.checks if check.su_layer is not None]


def unique_layer_names(plans: List[PlanMap]) -> Dict[int, str]:
    """Layer name per plan: the plan's title, made unique by position in the file.

    The title *verbatim* -- ``3.1 Hellenistic Pitting and Abandonment``, spaces
    and dots and all. A QGIS layer name has no character restrictions, so there
    is nothing to sanitise away, and a layer named for its title reads correctly
    everywhere it appears: the layer tree, the legend, and any title template
    that falls back to the layer name. File names are sanitised at export, where
    the restriction actually is, so the written file is still
    ``3.1_Hellenistic_Pitting_and_Abandonment.jpg``.

    Two plans may legitimately share a title while differing in their queries;
    QGIS allows duplicate layer names but the export dialog matches companions
    by name, so a collision would pair the wrong Features layer with a plan.
    """
    names: Dict[int, str] = {}
    seen: Dict[str, int] = {}
    for index, plan in enumerate(plans):
        base = plan.title.strip() or f"Plan {index + 1}"
        count = seen.get(base.casefold(), 0)
        seen[base.casefold()] = count + 1
        names[index] = base if count == 0 else f"{base} ({count + 1})"
    return names


def describe_query_problem(layer, query: str) -> str:
    """Why ``query`` looks wrong for ``layer``, or ''.

    An explanation, not a verdict. The provider has the final say on a subset
    string -- it is SQL, and a provider may accept dialect this cannot parse --
    so this is asked *after* a build fails, to turn "the provider rejected the
    query" into "layer 'Features' has no attribute 'sunumber'", which says which
    combo box is wrong. It is also cheap enough to score the layer combos with.
    """
    from qgis.core import QgsExpression

    if not query:
        return ""
    if layer is None:
        return "no layer chosen for this query"

    expression = QgsExpression(query)
    if expression.hasParserError():
        return f"cannot be parsed -- {expression.parserErrorString().strip()}"

    fields = layer.fields()
    missing = [
        column
        for column in expression.referencedColumns()
        # QgsExpression reports '*' for an expression that reads every field.
        if column != "*" and fields.lookupField(column) < 0
    ]
    if missing:
        return (
            f"layer '{layer.name()}' has no attribute "
            f"{', '.join(repr(name) for name in sorted(missing))}"
        )
    return ""


def _materialise(source_layer, filtered_layer, name, properties, inherit_style):
    """Copy a filtered view's features into an editable in-memory layer.

    The filter still decides membership -- the provider is asked, exactly as in
    live mode -- and only then are the features it returned copied out. So the
    two modes always select the same SUs; they differ in what you get to do to
    them afterwards.
    """
    features = list(filtered_layer.getFeatures())
    copy = build_memory_layer(
        source_layer,
        name,
        features,
        None,
        "",
        add_provenance=False,
        inherit_style=inherit_style,
    )
    for key, value in (properties or {}).items():
        copy.setCustomProperty(key, value)
    return copy


def check_plans(
    plan_file: PlanFile,
    su_layer,
    feature_layer=None,
    inherit_style: bool = True,
    mode: str = MODE_COPY,
    progress: Optional[Callable[[int, int, str], None]] = None,
) -> PlanSetOutcome:
    """Build every plan's layers and count what they select, without publishing.

    The layers are real and complete -- ``build_plan_set`` adds these very
    objects to the project -- so the counts shown before the build are the
    counts the built layers have. Nothing reaches the project until then.

    ``mode`` is ``MODE_COPY`` or ``MODE_LIVE``; see the constants. Either way
    the provider runs the query, so the counts are the same in both.
    """
    outcome = PlanSetOutcome()
    plans = plan_file.usable
    if su_layer is None and feature_layer is None:
        outcome.errors.append("Choose an SU layer, a Features layer, or both.")
        return outcome

    names = unique_layer_names(plans)
    total = len(plans)

    for index, plan in enumerate(plans):
        name = names[index]
        if progress:
            progress(index + 1, total, plan.title)

        check = PlanCheck(plan=plan, layer_name=name)
        properties = layer_properties(plan)

        for query, source, kind in (
            (plan.su_query, su_layer, "su"),
            (plan.feature_query, feature_layer, "feature"),
        ):
            if not query:
                continue
            if source is None:
                # Not an error: a plan file may name Features for plans on a
                # project that has no Features layer loaded.
                continue

            layer, subset, error = build_query_layer(
                source,
                name,
                query,
                properties=properties,
                inherit_style=inherit_style,
                # Editable in both modes, deliberately. A copy is safe to edit
                # because it is a copy; a live layer is unlocked because that is
                # what asking for live mode means.
                read_only=False,
            )
            if layer is None:
                # Ask the provider first, explain second: a query written for
                # PostGIS may use dialect QgsExpression cannot parse, and the
                # provider taking it is the only test that counts.
                setattr(
                    check,
                    f"{kind}_error",
                    describe_query_problem(source, query) or error,
                )
                continue

            if mode == MODE_COPY:
                try:
                    layer = _materialise(
                        source, layer, name, properties, inherit_style
                    )
                except Exception as exc:  # pragma: no cover - depends on provider
                    setattr(check, f"{kind}_error", f"could not be copied -- {exc}")
                    continue

            setattr(check, f"{kind}_layer", layer)
            if mode == MODE_LIVE:
                outcome.subsets[f"{name} ({kind})"] = subset
            try:
                setattr(check, f"{kind}_count", layer.featureCount())
            except Exception:  # pragma: no cover - depends on provider
                setattr(check, f"{kind}_count", None)

        outcome.checks.append(check)

    return outcome


def build_plan_set(
    outcome: PlanSetOutcome,
    plans_group: str,
    features_group: str,
    checks: Optional[List[PlanCheck]] = None,
    replace: bool = True,
) -> PlanSetOutcome:
    """Publish the checked layers into two named groups.

    ``checks`` narrows the run to the plans that were ticked; the default is
    everything that built. Layer order is file order, which is publication
    order, and the layer tree keeps it.

    The Features group is placed above the Plans group. Features are cut into
    and recorded within the SUs around them, so they belong on top -- and with
    *Set map layers per phase* switched off, the tree order is the *only* thing
    deciding it.
    """
    from qgis.core import QgsProject

    if checks is None:
        wanted = outcome.built
    else:
        wanted = [c for c in checks if c.su_layer or c.feature_layer]
    outcome.plans_group = plans_group
    outcome.features_group = features_group

    project = QgsProject.instance()
    has_features = any(check.feature_layer is not None for check in wanted)

    if replace:
        clear_group(plans_group)
        if has_features:
            clear_group(features_group)

    plans_node = layer_tree_group(plans_group)
    features_node = layer_tree_group(features_group) if has_features else None
    if features_node is not None:
        # Done while both are still empty: reordering the tree means moving a
        # node, and moving one with layers in it risks taking them out of the
        # project with it. Empty, there is nothing to lose.
        features_node = raise_group_above(features_group, plans_group)

    for check in wanted:
        if check.su_layer is not None:
            project.addMapLayer(check.su_layer, False)
            plans_node.addLayer(check.su_layer)
        if check.feature_layer is not None and features_node is not None:
            project.addMapLayer(check.feature_layer, False)
            features_node.addLayer(check.feature_layer)

    project.setDirty(True)
    return outcome


def raise_group_above(upper: str, lower: str):
    """Move group ``upper`` to sit directly above group ``lower``.

    Returns the node for ``upper`` -- a *different object* if it had to be
    moved, since the layer tree has no move: a node is cloned into its new
    position and the original removed. Call it before putting layers in, and
    use the node it hands back rather than the one you passed.

    Both groups must be top-level and ``upper`` must be empty; anything else is
    left exactly as it is and the existing node returned. That refusal is the
    safety: removing a populated group node can take its layers out of the
    project with it, and no layer ordering is worth that.
    """
    from qgis.core import QgsLayerTree, QgsProject

    root = QgsProject.instance().layerTreeRoot()
    upper_node = root.findGroup(upper)
    if upper_node is None or root.findGroup(lower) is None:
        return upper_node
    if upper_node.findLayers():
        return upper_node

    def positions():
        """Index of each group among the tree's top-level children, by name.

        Positions are looked up by name rather than by holding node objects:
        the same C++ node does not always come back as the same Python object,
        so ``is`` and ``in`` are not dependable across a call.
        """
        found = {}
        for index, node in enumerate(root.children()):
            if QgsLayerTree.isGroup(node):
                found.setdefault(node.name(), index)
        return found

    where = positions()
    if upper not in where or lower not in where:
        return upper_node  # nested inside another group; not ours to rearrange
    if where[upper] == where[lower] - 1:
        return upper_node  # already there

    clone = upper_node.clone()
    root.removeChildNode(upper_node)
    # Recomputed: removing the node above shifts everything below it up one.
    root.insertChildNode(positions()[lower], clone)
    return root.findGroup(upper)


def layer_tree_group(name: str):
    """The named group at the top of the layer tree, creating it if need be."""
    from qgis.core import QgsProject

    root = QgsProject.instance().layerTreeRoot()
    existing = root.findGroup(name)
    if existing is not None:
        return existing
    return root.insertGroup(0, name)


def group_layer_count(name: str, protect=()) -> int:
    """How many layers a rebuild would remove from a named group.

    Counts what :func:`clear_group` would take, so ``protect`` means the same
    thing here: layers that are never removed, and so are never counted.
    """
    from qgis.core import QgsProject

    group = QgsProject.instance().layerTreeRoot().findGroup(name)
    if group is None:
        return 0
    protect = set(protect or ())
    return sum(
        1
        for tree_layer in group.findLayers()
        if tree_layer.layerId() and tree_layer.layerId() not in protect
    )


def clear_group(name: str, protect=()) -> int:
    """Remove every layer from a group, so a rebuild replaces rather than doubles.

    Returns how many layers went. The group itself is left in place, keeping its
    position in the tree and whatever the operator had collapsed or expanded.

    ``protect`` is a set of layer ids that are never removed. The source layers
    go in it: a group is only a name, and if one of them happens to be sitting
    in the group being rebuilt, dropping it from the project is the last thing
    this tool should do to it.
    """
    from qgis.core import QgsProject

    project = QgsProject.instance()
    group = project.layerTreeRoot().findGroup(name)
    if group is None:
        return 0
    protect = set(protect or ())
    ids = [
        tree_layer.layerId()
        for tree_layer in group.findLayers()
        if tree_layer.layerId() and tree_layer.layerId() not in protect
    ]
    if ids:
        project.removeMapLayers(ids)
    return len(ids)


def group_names(stem: str) -> tuple:
    """Default group names for a plan file: ``Field3 Plans`` / ``Field3 Features``.

    A file called ``Field3PhasePlans2026_Queries.txt`` is about Field 3, so the
    leading word-and-number is the useful part of the name; the rest would only
    make two long group names in the layer tree.
    """
    import re

    stem = (stem or "").strip() or "Plan"
    match = re.match(r"^[A-Za-z]*\d+", stem)
    if match:
        stem = match.group(0)
    return f"{stem} {GROUP_SUFFIX_PLANS}", f"{stem} {GROUP_SUFFIX_FEATURES}"


def summarise(outcome: PlanSetOutcome, plan_file: PlanFile) -> str:
    """Human-readable pre-flight line for the dialog."""
    if not outcome.checks:
        return "No plans to build."

    failed = [check for check in outcome.checks if not check.ok]
    empty = [check for check in outcome.checks if check.ok and check.empty]
    with_features = [c for c in outcome.checks if c.feature_layer is not None]

    su_total = sum(c.su_count or 0 for c in outcome.checks)
    feature_total = sum(c.feature_count or 0 for c in outcome.checks)

    parts = [
        f"{len(outcome.checks)} plan(s) of the {len(plan_file.maps)} in the "
        f"file, drawing {su_total} SU polygon(s) and {feature_total} feature(s) "
        f"in total -- an SU on three plans is counted three times.",
        f"{len(with_features)} plan(s) draw features.",
    ]
    if failed:
        parts.append(f"{len(failed)} plan(s) have a query that will not run.")
    if empty:
        parts.append(
            f"{len(empty)} plan(s) select nothing -- check the SU numbers: "
            + ", ".join(c.plan.title for c in empty[:3])
            + (f", +{len(empty) - 3} more" if len(empty) > 3 else "")
        )
    if plan_file.warnings:
        parts.append(f"{len(plan_file.warnings)} file warning(s).")
    return "\n".join(parts)


__all__ = [
    "MODE_COPY",
    "MODE_LIVE",
    "PlanCheck",
    "PlanSetOutcome",
    "build_plan_set",
    "check_plans",
    "clear_group",
    "combine_filters",
    "describe_query_problem",
    "group_names",
    "layer_tree_group",
    "raise_group_above",
    "summarise",
    "unique_layer_names",
]
