"""The sealed grouping/reconciliation policy: `config/designator_grouping.toml`.

Modelled directly on `geometry.load_padding_config` /
`geometry._load_padding_provenance`: a closed schema, refusals by name, and a
digest of the exact bytes read so the config can be bound into a run's seal.
This module owns loading and resolving the policy only -- it is not wired
into `common/stage.py` or `run.py` (that is units C and D's own work); a page
that wants resolved pixel thresholds calls `resolve_thresholds` itself, once
it has this module's config and its own page dimensions.

Four closed sub-tables, not one flat table, because the *basis* a threshold
resolves against is structural, not a naming convention: `page_fraction_bp`
values are basis points of the page's own WIDTH (`margin_bp`) or HEIGHT
(every other field) as declared in the config file's header comment, while
`absolute` values are raw pixel counts that must never be scaled by page
size at all. `page_area_bp` is the third: a basis point of the page's own AREA,
which is a fraction of both dimensions at once and so resolves to no pixel
length at all -- `page_fraction_bp`'s contract is one declared basis per field,
and a field with two bases cannot be smuggled into it. It carries its own
provenance block, measured on 17 real pages.
`background` is the fourth and it carries its own provenance block:
its `band_bp` is the one length in this file that resolves against *both*
dimensions, because the band it describes is a frame, and its other three
fields are fractions of a pixel population, or of the distance between two of
them, rather than of a page, and never resolve to pixels at all. Putting a
field in the wrong sub-table is refused by the closed schema rather than caught
by a comment nobody reads.

That block was called `surround` until 2026-09-06, when it stopped being only a
surround test: `max_ink_bp` asks whether the value inferred as paper is a
background of its own page at all, on a page that has no surround and never
reaches the geometric test. `ink_margin_bp` joined it the same day for the same
reason: it derives each page's own ink threshold and has nothing to do with a
surround either. A sub-table named for one of its four fields is the misnaming
GLOSSARY's "one word per concept" refuses, so it is named for what it governs.
The evidence published by the interior-mode branch is `dark_distribution`: its
counts describe sampled dark pixels and do not establish a frame, bezel, or
paper-region boundary.

`primary_margin` and `secondary_margin` are refused by name wherever they
appear, in either sub-table or at the policy's own top level. They are
`structure.PRIMARY_MARGIN` and `structure.SECONDARY_MARGIN` -- absolute 8-bit
ink-intensity offsets, not page geometry -- and stay Python module constants
because `common/test_designator_recensor_ink_calibration.py` is an AST pin
that reads `SECONDARY_MARGIN` as a source literal in `structure.py` and
cross-checks it against the Recensor's own contrast constant. A per-run
config value for either name would make that cross-stage invariant
unenforceable statically.

**`ink_margin_bp` is not that field and does not weaken that rule.** It is a
fraction of the distance between a page's own two population modes, so it
carries no grey level of its own and cannot be read as an offset: the same
sealed 3333 derives a margin of 46 on this repository's fixture page and 71 on a
photographed register opening, because those two pages have different contrast
and not because anything in the file changed.
The invariant the AST pin protects is between the Recensor's contrast constant
and `SECONDARY_MARGIN`, and `SECONDARY_MARGIN` is not derived -- so the pin
still reads two literals and compares them, exactly as it did.
"""

import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final

from geometry import (
    _PROVENANCE_FIELDS,
    _is_plain_int,
    _pad_amount,
    _validate_dimensions,
)

# `resolve_background_policy` is re-exported rather than defined here: it stopped
# being this stage's own resolver on 2026-09-06, when the Ink Map and the
# Recensor began inferring a page's paper value under the same sealed policy.
# `run.py` and this stage's tests still call it as `grouping_config.resolve_
# background_policy`, which is what the name here preserves.
from common.background import (  # noqa: F401
    BACKGROUND_BP_FIELDS as _BACKGROUND_BP_FIELDS,
)

# One spelling of "a whole page in basis points", shared with the background
# policy's own bounds rather than written a second time here.
from common.background import (
    BASIS_POINTS as _BASIS_POINTS,
)
from common.background import (  # noqa: F401
    resolve_background_policy,
    validate_background_table,
)
from common.calibration import calibrated_claim_has_sample_evidence
from common.contracts.canonical import digest_bytes
from common.contracts.errors import ContractError

DEFAULT_GROUPING_CONFIG_PATH = (
    Path(__file__).resolve().parents[2] / "config" / "designator_grouping.toml"
)

# The two closed sub-tables' own field sets. Never overlapping, never open --
# a field belongs to exactly one basis or it is refused.
_PAGE_FRACTION_BP_FIELDS: Final = (
    "margin_bp",
    "chain_gap_bp",
    "anchor_reach_bp",
    "brace_min_height_bp",
    "page_edge_reach_bp",
    "review_priority_min_dimension_bp",
    "fallback_overlap_bp",
)
_ABSOLUTE_FIELDS: Final = ("gap_tolerance_px",)

# The one field whose basis is the page's own AREA. It is a bound on a
# *component*, not a length to resolve: `partition_page_spanning` compares a
# bounding box's area against it in basis points and never converts it to
# pixels, so it passes through `resolve_thresholds` unresolved exactly as the
# three bare counts do.
_PAGE_AREA_BP_FIELDS: Final = ("page_spanning_area_bp",)

# Names that must never appear anywhere in this policy -- see module
# docstring. Checked explicitly, with a message that names them, rather than
# left to fall out of the generic "unknown field" refusal, because a reader
# hunting for *why* these two names are forbidden should find the reason at
# the refusal site, not have to already know it.
_FORBIDDEN_NAMES: Final = ("primary_margin", "secondary_margin")

# The bare counts. None of the three is a length, so none has a page dimension
# to be a fraction of and none belongs in `absolute`, which holds pixel
# lengths: a band count is a cardinality, and the two bounds are ceilings on
# how many separate review items one page may contribute.
_GROUPING_COUNT_FIELDS: Final = (
    "max_residual_components",
    "max_secondary_proposals",
    "fallback_bands",
)

_GROUPING_TOP_FIELDS: Final = _GROUPING_COUNT_FIELDS + (
    "page_fraction_bp",
    "absolute",
    "page_area_bp",
    "background",
    "provenance",
)


def _refuse_forbidden_names(fields: dict, where: str) -> None:
    found = sorted(name for name in _FORBIDDEN_NAMES if name in fields)
    if found:
        raise ContractError(
            f"the grouping configuration's {where} carries forbidden field(s) {found}; "
            "primary_margin/secondary_margin are absolute 8-bit ink-intensity offsets pinned "
            "as Python module constants in structure.py by "
            "common/test_designator_recensor_ink_calibration.py and may never become a per-run "
            "config value. What is sealed instead is [grouping.background] ink_margin_bp, the "
            "fraction of a page's own two-mode distance that derives its margin: a population "
            "fraction, which scales with the page, and not an offset"
        )


def load_grouping_config(
    path: str | Path = DEFAULT_GROUPING_CONFIG_PATH,
) -> dict[str, Any]:
    """Read the grouping/reconciliation policy, with the digest that seals it.

    Refused loudly rather than defaulted, matching `load_padding_config`'s own
    reasoning: a threshold silently taken as zero, or a bound silently taken
    as unlimited, would change what a page's structure pass or conservation
    reconciliation does with nobody able to point at a config line that said
    so.
    """
    path = Path(path)
    try:
        data = path.read_bytes()
        config = tomllib.loads(data.decode("utf-8"))
    except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError) as error:
        raise ContractError(
            f"the grouping configuration at {path} could not be read: {error}"
        ) from error
    if not isinstance(config, dict):
        raise ContractError("the grouping configuration is not a table")

    unexpected_top_level = sorted(set(config) - {"grouping"})
    if unexpected_top_level:
        raise ContractError(
            "the grouping configuration has unknown top-level field(s) "
            f"{unexpected_top_level}; an unread policy table cannot be applied"
        )
    grouping = config.get("grouping")
    if not isinstance(grouping, dict):
        raise ContractError("the grouping configuration has no [grouping] table")

    _refuse_forbidden_names(grouping, "[grouping] table")

    unexpected = sorted(set(grouping) - set(_GROUPING_TOP_FIELDS))
    if unexpected:
        raise ContractError(
            f"the grouping configuration has unknown field(s) {unexpected}; "
            "an unread policy field cannot be applied"
        )
    missing = sorted(set(_GROUPING_TOP_FIELDS) - set(grouping))
    if missing:
        raise ContractError(f"the grouping configuration is missing field(s) {missing}")

    counts = {}
    for name in _GROUPING_COUNT_FIELDS:
        value = grouping[name]
        # `fallback_bands` is the one count that may not be zero: a grid of no
        # bands cuts nothing, and a page the structure pass found nothing on
        # would then reach the witnesses as no crop at all -- the exact loss
        # Tyrel's 2026-08-11 ruling on predetermined crops exists to prevent.
        floor = 1 if name == "fallback_bands" else 0
        if not _is_plain_int(value) or value < floor:
            shape = "positive" if floor else "non-negative"
            raise ContractError(f"the grouping configuration's {name} is not a {shape} integer")
        counts[name] = value

    page_fraction_bp = _load_closed_int_table(
        grouping.get("page_fraction_bp"),
        _PAGE_FRACTION_BP_FIELDS,
        "[grouping.page_fraction_bp]",
    )
    absolute = _load_closed_int_table(
        grouping.get("absolute"), _ABSOLUTE_FIELDS, "[grouping.absolute]"
    )
    page_area_bp = _load_page_area_bp(grouping.get("page_area_bp"))
    background = _load_background(grouping.get("background"))
    provenance = _load_provenance(grouping.get("provenance"), "[grouping.provenance]")

    return {
        "config_sha256": digest_bytes(data),
        **counts,
        "page_fraction_bp": page_fraction_bp,
        "absolute": absolute,
        "page_area_bp": page_area_bp,
        "background": background,
        "provenance": provenance,
    }


def _load_closed_int_table(table: Any, fields: tuple[str, ...], what: str) -> dict[str, int]:
    if not isinstance(table, dict):
        raise ContractError(f"the grouping configuration has no {what} table")
    _refuse_forbidden_names(table, what)
    unexpected = sorted(set(table) - set(fields))
    if unexpected:
        raise ContractError(
            f"the grouping configuration's {what} carries unknown field(s) {unexpected}; "
            "an unread policy field cannot be applied"
        )
    missing = sorted(set(fields) - set(table))
    if missing:
        raise ContractError(f"the grouping configuration's {what} is missing field(s) {missing}")
    values = {name: table[name] for name in fields}
    invalid = [name for name in fields if not _is_plain_int(values[name]) or values[name] < 0]
    if invalid:
        raise ContractError(
            f"the grouping configuration's {what} has invalid non-negative integer "
            f"field(s) {invalid}"
        )
    return values


#  `_PROVENANCE_FIELDS` names every field the closed schema carries;
# `_TYPED_PROVENANCE_FIELDS` names the ones checked by their own type below
# instead of by the string loop, so the string loop is derived from the
# difference rather than hand-copied -- a field newly added to
# `_PROVENANCE_FIELDS` is validated by construction instead of silently
# skipped by a tuple nobody remembered to extend. The assertion below fires
# at import if the two ever drift apart.
_TYPED_PROVENANCE_FIELDS: Final = frozenset({"sample_count", "calibrated_for_this_corpus"})
_STRING_PROVENANCE_FIELDS: Final = tuple(sorted(set(_PROVENANCE_FIELDS) - _TYPED_PROVENANCE_FIELDS))
assert _TYPED_PROVENANCE_FIELDS | set(_STRING_PROVENANCE_FIELDS) == set(_PROVENANCE_FIELDS)


def _load_provenance(provenance: Any, where: str) -> dict[str, Any]:
    """Validate one declared provenance block against the closed schema.

    `where` is the block's own table name, because this policy now carries two
    of them:
    the file's own, over values that are unmeasured walking-skeleton defaults,
    and `[grouping.background]`'s, over four values measured on 127 real pages.
    One shared block could not describe both honestly -- `sample_count` alone is
    0 for one and 127 for the other -- so there are two, and this function holds
    both to the same schema.

    Identical shape to `geometry._load_padding_provenance`, for the same
    reason: every field is required and checked for shape, so a provenance
    block that is merely present cannot stand in for one that actually
    answers "where did this number come from." Unlike that function, the
    string-typed fields are derived from `_PROVENANCE_FIELDS` rather than
    hand-copied, so a field added to the schema is validated rather than
    silently passed through untyped.
    """
    if not isinstance(provenance, dict):
        raise ContractError(
            f"the grouping configuration has no {where} table; a policy value with no "
            "declared source may not be shipped as a default"
        )
    unexpected = sorted(set(provenance) - set(_PROVENANCE_FIELDS))
    if unexpected:
        raise ContractError(
            f"the grouping configuration's {where} carries unknown field(s) {unexpected}; "
            "provenance is a closed schema so an unread field cannot be trusted"
        )
    missing = sorted(set(_PROVENANCE_FIELDS) - set(provenance))
    if missing:
        raise ContractError(f"the grouping configuration's {where} is missing field(s) {missing}")
    for field in _STRING_PROVENANCE_FIELDS:
        if not isinstance(provenance[field], str) or not provenance[field].strip():
            raise ContractError(
                f"the grouping configuration's {where} field {field!r} is not a non-empty string"
            )
    if not _is_plain_int(provenance["sample_count"]) or provenance["sample_count"] < 0:
        raise ContractError(
            f"the grouping configuration's {where} sample_count is not a non-negative integer"
        )
    if not isinstance(provenance["calibrated_for_this_corpus"], bool):
        raise ContractError(
            f"the grouping configuration's {where} calibrated_for_this_corpus is not a boolean"
        )
    if not calibrated_claim_has_sample_evidence(
        provenance["calibrated_for_this_corpus"], provenance["sample_count"]
    ):
        raise ContractError(
            f"the grouping configuration's {where} says calibrated_for_this_corpus but "
            "sample_count is zero"
        )
    return dict(provenance)


def _load_page_area_bp(table: Any) -> dict[str, Any]:
    """Read `[grouping.page_area_bp]` and its own provenance.

    A provenance block of its own for the reason `[grouping.background]` has
    one: `sample_count` is 0 for the file's seven unmeasured defaults and 127
    for the background policy, and this value was measured on 17 pages. Three
    blocks say three true things; folding any of them together would say a false
    one.

    **The bound is closed at both ends and neither end is arbitrary.** At or
    below zero every component on every page is page-spanning, so the grouping
    pass would withhold the entire page and no act would ever be proposed --
    refused rather than allowed to produce an empty run that reconciles. Past
    10000 no component can ever reach it, because a bounding box cannot exceed
    the page it is measured against, and a bound nothing can reach is a policy
    that reads as being in force while doing nothing; 10000 itself is legal and
    means exactly "only a component whose bounding box is the whole page".
    """
    if not isinstance(table, dict):
        raise ContractError("the grouping configuration has no [grouping.page_area_bp] table")
    _refuse_forbidden_names(table, "[grouping.page_area_bp]")
    expected = set(_PAGE_AREA_BP_FIELDS) | {"provenance"}
    unexpected = sorted(set(table) - expected)
    if unexpected:
        raise ContractError(
            f"the grouping configuration's [grouping.page_area_bp] carries unknown field(s) "
            f"{unexpected}; an unread policy field cannot be applied"
        )
    missing = sorted(expected - set(table))
    if missing:
        raise ContractError(
            f"the grouping configuration's [grouping.page_area_bp] is missing field(s) {missing}"
        )
    values = {}
    for name in _PAGE_AREA_BP_FIELDS:
        value = table[name]
        if not _is_plain_int(value) or not (0 < value <= _BASIS_POINTS):
            raise ContractError(
                f"the grouping configuration's [grouping.page_area_bp] {name} is {value!r}, "
                f"which is not an integer in 1..{_BASIS_POINTS} basis points; at or below zero "
                "every component on every page spans it and the grouping pass would withhold the "
                "whole page, and past a whole page nothing can ever reach it"
            )
        values[name] = value
    values["provenance"] = _load_provenance(
        table.get("provenance"), "[grouping.page_area_bp.provenance]"
    )
    return values


# `[grouping.background]`: how this stage infers a page's paper value, and the
# one block in this file measured against real material.
# `band_bp` is a length and scales with the page, but it is the only field here
# that resolves against a page dimension and it resolves against BOTH -- the
# band is a frame, `band_bp` of the width on the left and right and `band_bp` of
# the height on top and bottom -- so it cannot live in `page_fraction_bp`, whose
# whole contract is one declared basis per field. The other two are fractions of
# a *population* rather than of a page, so they never resolve to pixels at all:
# `max_interior_dark_bp` of the interior band's own pixels, `max_ink_bp` of the
# whole page's.
# `ink_margin_bp` is the fourth and it is not a bound at all: it is the fraction
# of the distance between the page's own two population modes that
# `structure._derived_ink_margin` deducts from the paper value to get this
# page's ink threshold. It is a fraction of a *population distance*, which is
# why it belongs here and an absolute ink offset never can -- see
# `_FORBIDDEN_NAMES`.
def _load_background(table: Any) -> dict[str, Any]:
    """Read `[grouping.background]` and its own provenance.

    A provenance block of its own, rather than a line in the file's shared one:
    every other value in this policy is an unmeasured walking-skeleton default
    with `sample_count = 0`, and folding four values measured on 127 real pages
    into that block would either overstate the rest of the file or understate
    these. Two blocks say two true things; one would say a false one.

    **The four values themselves are checked by
    `common.background.validate_background_table`, not here.** They stopped
    being this stage's alone on 2026-09-06: the Ink Map and the Recensor's
    residual-ink audit now infer a page's paper value through the same function
    under the same policy, so a value one of the three would refuse has to be a
    value all three refuse, and one validator is the only way that stays true.
    What remains here is what is genuinely this file's: the forbidden-name
    refusal, the closed field set including `provenance`, and the provenance
    block's own schema, which the other two stages do not read and which
    `_load_provenance` validates against the same shape as the rest of the file.
    """
    if not isinstance(table, dict):
        raise ContractError("the grouping configuration has no [grouping.background] table")
    _refuse_forbidden_names(table, "[grouping.background]")
    expected = set(_BACKGROUND_BP_FIELDS) | {"provenance"}
    unexpected = sorted(set(table) - expected)
    if unexpected:
        raise ContractError(
            f"the grouping configuration's [grouping.background] carries unknown field(s) "
            f"{unexpected}; an unread policy field cannot be applied"
        )
    missing = sorted(expected - set(table))
    if missing:
        raise ContractError(
            f"the grouping configuration's [grouping.background] is missing field(s) {missing}"
        )
    values = validate_background_table(table)
    values["provenance"] = _load_provenance(
        table.get("provenance"), "[grouping.background.provenance]"
    )
    return values


@dataclass(frozen=True)
class GroupingThresholds:
    """Resolved, page-sized-specific pixel thresholds. Ints only -- never a float."""

    margin_px: int
    chain_gap_px: int
    anchor_reach_px: int
    brace_min_height_px: int
    page_edge_reach_px: int
    review_priority_min_dimension_px: int
    fallback_overlap_px: int
    gap_tolerance_px: int
    max_residual_components: int
    max_secondary_proposals: int
    fallback_bands: int
    page_spanning_area_bp: int


def resolve_thresholds(config: dict[str, Any], width: int, height: int) -> GroupingThresholds:
    """Resolve one page's own basis-point thresholds into pixel integers.

    `margin_px` resolves against `width`; every other page_fraction_bp field
    resolves against `height` -- the basis each field's config comment
    declares as a design decision (SPEC_C section 2), not a property
    recovered from the retired pixel constant it replaces.
    `gap_tolerance_px`, the three counts (`max_residual_components`,
    `max_secondary_proposals`, `fallback_bands`) and `page_spanning_area_bp`
    pass through unresolved. The first four are not page-fraction quantities at
    all; the last one is, but its basis is the page's own AREA rather than one
    of its dimensions, so there is no single length for `_pad_amount` to
    round against and the comparison stays in basis points at the one place it
    is made (`grouping.partition_page_spanning`).

    Uses `geometry._pad_amount` for every basis-point resolution -- the same
    round-half-up integer rule the padding config already uses -- so this
    module never carries a second rounding rule that could quietly disagree
    with the first.
    """
    _validate_dimensions(width, height, "page")
    bp = config["page_fraction_bp"]
    return GroupingThresholds(
        margin_px=_pad_amount(width, bp["margin_bp"]),
        chain_gap_px=_pad_amount(height, bp["chain_gap_bp"]),
        anchor_reach_px=_pad_amount(height, bp["anchor_reach_bp"]),
        brace_min_height_px=_pad_amount(height, bp["brace_min_height_bp"]),
        page_edge_reach_px=_pad_amount(height, bp["page_edge_reach_bp"]),
        review_priority_min_dimension_px=_pad_amount(
            height, bp["review_priority_min_dimension_bp"]
        ),
        fallback_overlap_px=_pad_amount(height, bp["fallback_overlap_bp"]),
        gap_tolerance_px=config["absolute"]["gap_tolerance_px"],
        max_residual_components=config["max_residual_components"],
        max_secondary_proposals=config["max_secondary_proposals"],
        fallback_bands=config["fallback_bands"],
        # A fraction of the page's own AREA, which is both dimensions at once,
        # so it passes through unresolved like the three counts above rather
        # than being turned into a pixel length by `_pad_amount`. Carried on the
        # resolved set all the same, because every page's `structure-status`
        # publishes this dataclass and a component withheld from grouping must
        # be checkable against the bound that withheld it.
        page_spanning_area_bp=config["page_area_bp"]["page_spanning_area_bp"],
    )
