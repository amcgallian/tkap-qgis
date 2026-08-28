"""Read a phase-plan definition file: one map per plan, with its own queries.

The phasing tool's own split derives the plans from ``space_phase``. That works
while a field's phasing *is* its space-phase table, and stops working the moment
a publication plan is a hand-picked set: 3.2.3 is "Sp.110.3 minus six SUs", and
3.5.1 is three spaces plus five loose SU numbers. Those decisions are made while
writing up, not in the data, so this reads them from a file the excavator
writes:

    ------------------------------------------------------------
    Hellenistic Pitting and Abandonment
    ------------------------------------------------------------

    Map Title: 3.1 Hellenistic Pitting and Abandonment

    Field3.1FeaturesPlan:
    "field" = '3' AND "feature" IN ('117', '120', '125', '144')

    Field3.1SUsPlan:
    "area" = '3'
    AND "sunumber" IN ('1819', '1820', '1823', '1847')

Each ``Map Title:`` starts a plan. The blocks under it are provider filters --
one for the SU layer, one for the Features layer, and optionally one for the
Spaces layer -- applied verbatim: this module never rewrites a query, so what
was tested in the Query Builder is what the plan gets.

The Spaces query is usually not written at all. An SU query already says which
spaces its plan is about, in the ``Sp.110.1`` references it filters on, so
:meth:`PlanMap.space_query_for` reads them back out and builds
``"space" IN ('110', '111')`` from them. A ``...SpacesPlan:`` block overrides
that for a plan where the spaces are not what the SU query implies.

Deliberately free of any ``qgis`` import, like the parsing half of
:mod:`phasing_core`, so the format can be exercised from a plain interpreter.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field as dc_field
from typing import Dict, List, Optional, Tuple

from .phasing_core import sql_identifier, sql_literal

#: ``---------`` or ``=========``: a rule bracketing a heading.
_DASH_RULE_RE = re.compile(r"^-{3,}$")
_EQUALS_RULE_RE = re.compile(r"^={3,}$")

#: A note to whoever is writing the file, ignored here. Also the way to park a
#: plan you are not ready to draw: comment out its ``Map Title:`` and its blocks
#: rather than deleting the queries you spent an afternoon getting right.
_COMMENT_RE = re.compile(r"^\s*#")

#: ``Map Title: 3.2.1 Early Hellenistic Construction``. Everything after the
#: colon is the title.
_MAP_TITLE_RE = re.compile(r"^map\s*title\s*:\s*(?P<title>.+?)\s*$", re.IGNORECASE)

#: ``Field3.1SUsPlan:`` -- an identifier alone on a line, ending in a colon,
#: which opens a query block. No whitespace in the name, so a line of SQL can
#: never be mistaken for one.
_BLOCK_RE = re.compile(r"^(?P<name>[^\s:]+)\s*:\s*$")

#: ``Sp.110.1`` wherever it appears -- including inside the ``LIKE`` literal of
#: an SU query, which is where a plan says which spaces it is about. Deliberately
#: looser than ``phasing_core.ENTRY_RE``: that one wants the ``(Phase Name)`` a
#: real ``space_phase`` value carries, and a query pattern has been cut off
#: before it (``'%Sp.110.1 (%'``).
_SPACE_REF_RE = re.compile(r"Sp\.(?P<space>\d+)(?:\.(?P<phase>\d+))?")

#: Leading numbering on a title: ``3.2.1`` out of ``3.2.1 Early Hellenistic``.
_NUMBER_RE = re.compile(r"^(?P<number>\d+(?:\.\d+)*)[.)]?(?:\s+|$)")

#: Which layer a query block is for.
KIND_FEATURES = "features"
KIND_SUS = "sus"
KIND_SPACES = "spaces"

#: ``SU``/``SUs`` as a word inside a run-together block name -- the ``SUs`` in
#: ``Field3.2.1SUsPlan`` or in ``LateIronAgeSUsPlan``.
#:
#: Two branches, because two things have to be true at once. Capitalised ``SU``
#: is distinctive enough to find anywhere a lowercase letter cannot follow it,
#: which is what lets it sit mid-word in camel case. Lowercase ``su`` is not --
#: it is the tail of *census* and the head of *surface* -- so it only counts as
#: a word of its own. Neither branch matches ``Surface`` or ``Sussex``.
_SU_TOKEN_RE = re.compile(
    r"(?<![A-Z])SUs?(?![a-z])"
    r"|(?:^|[^A-Za-z])sus?(?:$|[^a-z])"
)


def classify_block(block_name: str) -> Optional[str]:
    """Which layer a block's query is for, or ``None`` if it is not clear.

    Features are tested first: ``FeaturesPlan`` is unambiguous, and testing it
    first means a name that somehow mentions both is read as the more specific
    of the two rather than by whichever pattern happened to run first.
    """
    name = block_name or ""
    if "feature" in name.casefold():
        return KIND_FEATURES
    if "space" in name.casefold():
        return KIND_SPACES
    if _SU_TOKEN_RE.search(name):
        return KIND_SUS
    if "stratigraphic" in name.casefold():
        return KIND_SUS
    return None


def split_title(title: str) -> Tuple[str, str]:
    """Split ``3.2.1 Early Hellenistic Construction`` into number and name.

    A title with no leading numbering returns ``("", title)``. The number is
    only ever used for display -- plans keep file order -- so its absence costs
    nothing.
    """
    text = (title or "").strip()
    match = _NUMBER_RE.match(text)
    if not match:
        return "", text
    return match.group("number"), text[match.end():].strip()


@dataclass
class PlanMap:
    """One plan: a title, and the query for each layer it draws."""

    title: str
    section: str = ""
    su_query: str = ""
    feature_query: str = ""
    space_query: str = ""
    #: Block names the queries came from, so an error can point at the file
    #: rather than at the layer it produced.
    su_block: str = ""
    feature_block: str = ""
    space_block: str = ""
    #: 1-based line of the ``Map Title:`` that opened this plan.
    line: int = 0

    @property
    def number(self) -> str:
        return split_title(self.title)[0]

    @property
    def name(self) -> str:
        return split_title(self.title)[1]

    @property
    def has_queries(self) -> bool:
        return bool(self.su_query or self.feature_query or self.space_query)

    @property
    def spaces(self) -> List[str]:
        """Space numbers this plan is about, in the order the file mentions them.

        Read out of the SU query's own ``Sp.110.1`` references, so a plan file
        that never mentions spaces separately still knows which ones it covers.
        A plan defined purely by SU number -- 3.1 in the TKAP file is a bare
        ``sunumber IN (...)`` -- names no spaces, and correctly gets none.
        """
        out: List[str] = []
        for match in _SPACE_REF_RE.finditer(self.su_query or ""):
            space = match.group("space")
            if space not in out:
                out.append(space)
        return out

    def space_query_for(self, field: str = "space") -> str:
        """The query for the Spaces layer: the written one, or one derived.

        A ``...SpacesPlan:`` block wins outright. Otherwise the spaces named in
        the SU query become ``"space" IN ('110', '111')`` -- which is the query
        somebody would have written by hand, off the same information.
        """
        if self.space_query:
            return self.space_query
        spaces = self.spaces
        if not spaces or not field:
            return ""
        listed = ", ".join(sql_literal(space) for space in spaces)
        return f"{sql_identifier(field)} IN ({listed})"

    def tokens(self, layer_name: str = "") -> Dict[str, str]:
        """Title placeholders for this plan. See ``phasing_core.format_title``."""
        number, name = split_title(self.title)
        return {
            "title": self.title,
            "number": number,
            "name": name,
            "section": self.section,
            "spaces": ", ".join(self.spaces),
            "layer": layer_name,
        }


@dataclass
class PlanFile:
    """Everything one plan file defines."""

    maps: List[PlanMap] = dc_field(default_factory=list)
    #: The ``=====``-bracketed heading at the top, if there is one.
    document_title: str = ""
    #: Non-fatal problems: unrecognised blocks, duplicates, plans with no query.
    #: Reported rather than raised, so one bad block does not cost you the other
    #: nineteen plans.
    warnings: List[str] = dc_field(default_factory=list)
    path: str = ""

    @property
    def usable(self) -> List[PlanMap]:
        """Plans with at least one query -- the ones worth building a layer for."""
        return [plan for plan in self.maps if plan.has_queries]

    @property
    def sections(self) -> List[str]:
        """Section headings, in file order, without repeats."""
        out: List[str] = []
        for plan in self.maps:
            if plan.section and plan.section not in out:
                out.append(plan.section)
        return out


# --------------------------------------------------------------------------
# Carrying a plan's identity on the layer it produced
# --------------------------------------------------------------------------

#: Tokens a plan layer remembers about itself, and the placeholders they fill
#: in a title template.
PLAN_TOKENS = ("title", "number", "name", "section", "spaces")

#: Custom-property namespace. QGIS saves custom properties with the project, so
#: a plan layer still knows its title after QGIS is closed and reopened -- which
#: is what lets the export dialog title a plan it did not build itself.
PROPERTY_PREFIX = "tkap/plan_"


def property_key(token: str) -> str:
    return f"{PROPERTY_PREFIX}{token}"


def layer_properties(plan: PlanMap) -> Dict[str, str]:
    """Custom properties to stamp on the layers built for ``plan``."""
    tokens = plan.tokens()
    return {property_key(token): tokens[token] for token in PLAN_TOKENS}


def tokens_from_layer(layer) -> Dict[str, str]:
    """Read a plan layer's tokens back off it. ``{}`` if it is not a plan layer.

    Takes anything with ``customProperty``; no ``qgis`` import needed, which
    keeps this module testable with a stub.
    """
    try:
        raw = {
            token: layer.customProperty(property_key(token), "")
            for token in PLAN_TOKENS
        }
    except Exception:  # pragma: no cover - depends on the object passed in
        return {}
    if not (raw.get("title") or "").strip():
        return {}
    return {token: str(value or "") for token, value in raw.items()}


def parse_plan_file(text: str, path: str = "") -> PlanFile:
    """Parse a plan file. Never raises on bad input -- it warns and carries on."""
    result = PlanFile(path=path)
    lines = (text or "").replace("\r\n", "\n").replace("\r", "\n").split("\n")

    current: Optional[PlanMap] = None
    section = ""
    index = 0

    while index < len(lines):
        stripped = lines[index].strip()

        if _COMMENT_RE.match(lines[index]):
            index += 1
            continue

        # ``=====`` brackets the document heading, ``-----`` a section heading.
        heading = _heading_at(lines, index, _EQUALS_RULE_RE)
        if heading is not None:
            if not result.document_title:
                result.document_title = lines[heading].strip()
            index = heading + 2
            continue
        heading = _heading_at(lines, index, _DASH_RULE_RE)
        if heading is not None:
            section = lines[heading].strip()
            index = heading + 2
            continue
        # A rule with no heading under it is just decoration.
        if _DASH_RULE_RE.match(stripped) or _EQUALS_RULE_RE.match(stripped):
            index += 1
            continue

        match = _MAP_TITLE_RE.match(stripped)
        if match:
            current = PlanMap(
                title=match.group("title"), section=section, line=index + 1
            )
            result.maps.append(current)
            index += 1
            continue

        match = _BLOCK_RE.match(stripped)
        if match:
            body, index = _read_block(lines, index + 1)
            _attach(result, current, match.group("name"), body)
            continue

        if stripped:
            result.warnings.append(
                f"Line {index + 1}: ignored -- not a heading, a map title or a "
                f"query block: {stripped[:60]}"
            )
        index += 1

    for plan in result.maps:
        if not plan.has_queries:
            result.warnings.append(
                f"Line {plan.line}: '{plan.title}' has no query block, so there "
                f"is nothing to draw."
            )

    counts: Dict[str, int] = {}
    for plan in result.maps:
        key = plan.title.casefold()
        counts[key] = counts.get(key, 0) + 1
    for plan in result.maps:
        if counts.pop(plan.title.casefold(), 0) > 1:
            result.warnings.append(
                f"Map title used more than once: '{plan.title}'. Layer names "
                f"are made unique, but the plans will be hard to tell apart."
            )

    return result


def read_plan_file(path: str) -> PlanFile:
    """Read and parse a plan file from disk (UTF-8, BOM tolerated)."""
    with open(path, "r", encoding="utf-8-sig") as handle:
        return parse_plan_file(handle.read(), path=path)


# --------------------------------------------------------------------------
# A file to start from
# --------------------------------------------------------------------------

#: Written by the dialog's *Save a template* button. It is a real plan file --
#: the two example plans parse and would build -- so it can be checked against
#: the data before a word of it is changed, and edited down from something that
#: works rather than up from a blank page. The instructions ride along as
#: comments, which the parser ignores.
TEMPLATE = """\
============================================================
FIELD {field} PHASE PLANS {year} - MAP TITLES & QUERIES
============================================================

# How this file works
#
#   Map Title:          starts a new plan. Everything after the colon is the
#                       title, and it is what the plugin writes into the
#                       layout's title label and names the exported file after.
#
#   ...SUsPlan:         a query block. The name is yours; only its ending
#   ...FeaturesPlan:    matters -- SUs, Features or Spaces, which is how the
#   ...SpacesPlan:      plugin knows which layer to run it against. Put the
#                       query on the line(s) under it, end it with a blank line.
#
#                       You rarely need a Spaces block. An SU query filtering on
#                       Sp.110.1 has already said the plan is about space 110,
#                       so the plugin builds "space" IN ('110') from it. Write
#                       the block only to override that.
#
#   ------ rules        a section heading, purely for reading. The heading
#                       between two rules is remembered as {{section}} if you
#                       want it in the title template.
#
#   # lines             notes like this one, ignored. Comment out a plan you
#                       are not ready to draw rather than deleting its query.
#
# Queries are used exactly as written: they are the provider filter, the same
# thing you would type into Layer Properties -> Source -> Query Builder. Test
# one there, paste it here. A plan may have both blocks, or only one.
#
# The two examples below are real and will build. Edit them, or delete them and
# write your own.

------------------------------------------------------------
Name of this group of plans
------------------------------------------------------------

Map Title: {field}.1 Latest Phase - Pitting and Abandonment

Field{field}.1FeaturesPlan:
"field" = '{field}' AND "feature" IN ('117', '120', '125')

Field{field}.1SUsPlan:
"area" = '{field}'
AND "sunumber" IN ('1819', '1820', '1823')

Map Title: {field}.2.1 Earlier Phase - Construction and Occupation

Field{field}.2.1FeaturesPlan:
"field" = '{field}' AND "feature" IN ('145', '148')

# A whole space-phase, plus one SU that belongs on the plan but is not in it,
# minus two that are. This is the shape most plans end up being. The Sp.110.1
# below is also what gets this plan its space layer, with nothing further to
# write: "space" IN ('110').
Field{field}.2.1SUsPlan:
"area" = '{field}'
AND "sunumber" NOT IN ('1961', '1924')
AND (
  "space_phase" LIKE '%Sp.110.1 (%'
  OR "sunumber" IN ('1831')
)
"""


def template_text(field: str = "3", year: str = "") -> str:
    """A plan file to fill in, seeded with an excavation field and a year."""
    if not year:
        from datetime import date

        year = str(date.today().year)
    return TEMPLATE.format(field=(field or "3").strip(), year=year)


# --------------------------------------------------------------------------
# Internals
# --------------------------------------------------------------------------


def _heading_at(lines: List[str], index: int, rule_re) -> Optional[int]:
    """If ``lines[index]`` opens a ``rule / heading / rule`` sandwich, its row."""
    if not rule_re.match(lines[index].strip()):
        return None
    if index + 2 >= len(lines):
        return None
    if not lines[index + 1].strip():
        return None
    if not rule_re.match(lines[index + 2].strip()):
        return None
    return index + 1


def _read_block(lines: List[str], start: int) -> Tuple[str, int]:
    """Collect a query body: up to a blank line, or to the next construct.

    A blank line ends the block, which is what the file format uses to separate
    them. Stopping at the next construct as well means a file written without
    the blank lines still parses.
    """
    body: List[str] = []
    index = start
    while index < len(lines):
        stripped = lines[index].strip()
        if not stripped:
            break
        if _COMMENT_RE.match(lines[index]):
            # A note against a clause is skipped, not treated as the end of the
            # query: blocks end at blank lines, which the comment does not break.
            index += 1
            continue
        if (
            _DASH_RULE_RE.match(stripped)
            or _EQUALS_RULE_RE.match(stripped)
            or _MAP_TITLE_RE.match(stripped)
            or _BLOCK_RE.match(stripped)
        ):
            break
        # Right-strip only: the query keeps its own line structure, which is
        # what makes a long OR-chain readable in the Query Builder afterwards.
        body.append(lines[index].rstrip())
        index += 1
    return "\n".join(body).strip(), index


def _attach(
    result: PlanFile, current: Optional[PlanMap], name: str, body: str
) -> None:
    """Store one parsed block on the plan it belongs to, or warn."""
    if not body:
        result.warnings.append(f"Block '{name}' is empty; ignored.")
        return
    if current is None:
        result.warnings.append(
            f"Block '{name}' appears before any 'Map Title:' line; ignored."
        )
        return

    kind = classify_block(name)
    if kind is None:
        result.warnings.append(
            f"Block '{name}' (under '{current.title}') does not say which layer "
            f"it is for. Name it ...SUsPlan, ...FeaturesPlan or ...SpacesPlan; "
            f"ignored."
        )
        return

    label = {KIND_FEATURES: "Features", KIND_SPACES: "Spaces", KIND_SUS: "SUs"}[kind]
    attribute = {KIND_FEATURES: "feature", KIND_SPACES: "space", KIND_SUS: "su"}[kind]

    if getattr(current, f"{attribute}_query"):
        result.warnings.append(
            f"'{current.title}' has more than one {label} block; "
            f"'{getattr(current, f'{attribute}_block')}' is used and '{name}' "
            f"ignored."
        )
        return
    setattr(current, f"{attribute}_query", body)
    setattr(current, f"{attribute}_block", name)
