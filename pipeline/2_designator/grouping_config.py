"""The sealed grouping/reconciliation policy: `config/designator_grouping.toml`.

Modelled on `geometry.load_padding_config`: a closed schema, named refusals, and
a digest of the exact bytes read so the config binds into a run's seal. This
module only loads and resolves the policy; a caller resolves pixel thresholds
itself via `resolve_thresholds` once it has this config and a page's dimensions.

Four closed sub-tables carry four different bases for a threshold, so a field
in the wrong one is a schema refusal rather than a misread comment:
`page_fraction_bp` values are basis points of page WIDTH (`margin_bp`) or
HEIGHT (everything else); `absolute` values are raw pixel counts never scaled
by page size; `page_area_bp` is a basis point of page AREA, a fraction of both
dimensions at once; `background` (real-material-measured) mixes a `band_bp`
that resolves against both dimensions (a frame) with population fractions
(`max_interior_dark_bp`, `max_ink_bp`, `ink_margin_bp`) that never resolve to
pixels at all.

`primary_margin`/`secondary_margin` are refused by name everywhere in this
policy: they are `structure.PRIMARY_MARGIN`/`SECONDARY_MARGIN`, absolute 8-bit
ink-intensity offsets pinned as Python constants and cross-checked against the
Recensor's contrast constant by an AST test, so a per-run config value for
either would make that cross-stage invariant unenforceable statically.
`ink_margin_bp` is not that field: it is a fraction of the distance between a
page's own two population modes, not a grey-level offset, so the same sealed
value derives a different margin per page's own contrast without weakening
the rule above.
"""

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final

from geometry import (
    _PROVENANCE_FIELDS,
    _is_plain_int,
    _pad_amount,
    _validate_dimensions,
)

# Re-exported (not this stage's own): the Ink Map and Recensor also infer
# paper value under the same sealed policy, and callers use this spelling.
from common.background import (  # noqa: F401
    BACKGROUND_BP_FIELDS as _BACKGROUND_BP_FIELDS,
)
from common.background import (
    BASIS_POINTS as _BASIS_POINTS,
)
from common.background import (  # noqa: F401
    resolve_background_policy,
    validate_background_table,
)
from common.calibration import calibrated_claim_has_sample_evidence
from common.contracts.errors import ContractError
from common.residual_ink import validate_coverage_audit_table
from common.sealed_config import read_sealed_toml

DEFAULT_GROUPING_CONFIG_PATH = (
    Path(__file__).resolve().parents[2] / "config" / "designator_grouping.toml"
)

# Never overlapping, never open: a field belongs to exactly one basis or is refused.
_PAGE_FRACTION_BP_FIELDS: Final = (
    "margin_bp",
    "chain_gap_bp",
    "anchor_reach_bp",
    "brace_min_height_bp",
    "review_priority_min_dimension_bp",
    "fallback_overlap_bp",
)

# A page-HEIGHT fraction like most of the table above, but its own provenance
# block: measured on 44 real pages, unlike the mostly-unmeasured block above it.
_CONTINUATION_BP_FIELDS: Final = ("page_edge_reach_bp",)
_ABSOLUTE_FIELDS: Final = ("gap_tolerance_px",)

# A bound on a component's own area, in basis points; `partition_page_spanning`
# compares against it directly and it passes through `resolve_thresholds`
# unresolved, like the bare counts below.
_PAGE_AREA_BP_FIELDS: Final = ("page_spanning_area_bp",)

# See module docstring. Checked explicitly, with a message naming why, rather
# than left to the generic "unknown field" refusal.
_FORBIDDEN_NAMES: Final = ("primary_margin", "secondary_margin")

# None of these is a page-dimension fraction. `max_residual_components` is
# read only for legacy withheld records; the current producer uses
# residual_presentation below.
_GROUPING_COUNT_FIELDS: Final = (
    "max_residual_components",
    "max_secondary_proposals",
    "fallback_bands",
)

_RESIDUAL_PRESENTATION_FIELDS: Final = (
    "residual_aggregate_max_pixel_count",
    "residual_aggregate_max_area_px",
)

_GROUPING_TOP_FIELDS: Final = _GROUPING_COUNT_FIELDS + (
    "page_fraction_bp",
    "continuation",
    "absolute",
    "page_area_bp",
    "background",
    "residual_presentation",
    "provenance",
)

# `coverage_audit` isn't the Designator's own policy -- it's the Ink Map,
# Recensor and Armarium's shared outside-coverage audit -- but it's validated
# here (via the audit's own validator) so a malformed table is refused at the
# earliest stage a run reaches. The Designator itself does nothing with it.
_TOP_LEVEL_TABLES: Final = ("grouping", "coverage_audit")


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

    Every field is refused loudly rather than defaulted, matching
    `load_padding_config`.
    """
    config, digest = read_sealed_toml(path, "grouping configuration", _TOP_LEVEL_TABLES)
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
        # fallback_bands may not be zero: a grid of no bands would let a page
        # with no found structure reach witnesses with no crop at all.
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
    continuation = _load_continuation(grouping.get("continuation"))
    page_area_bp = _load_page_area_bp(grouping.get("page_area_bp"))
    residual_presentation = _load_residual_presentation(grouping.get("residual_presentation"))
    # Validated but not applied (see _TOP_LEVEL_TABLES): only the Designator
    # itself refuses a run whose calibration block lost its provenance.
    coverage_audit = {
        **validate_coverage_audit_table(config.get("coverage_audit")),
        "provenance": _load_provenance(
            (config.get("coverage_audit") or {}).get("provenance")
            if isinstance(config.get("coverage_audit"), dict)
            else None,
            "[coverage_audit.provenance]",
        ),
        # A separate provenance block: it must not be read as covering the
        # unmeasured noise-floor pair too.
        "noise_floor_provenance": _load_provenance(
            config["coverage_audit"]["noise_floor"].get("provenance"),
            "[coverage_audit.noise_floor.provenance]",
        ),
    }
    background = _load_background(grouping.get("background"))
    provenance = _load_provenance(grouping.get("provenance"), "[grouping.provenance]")

    return {
        "config_sha256": digest,
        **counts,
        **{name: residual_presentation[name] for name in _RESIDUAL_PRESENTATION_FIELDS},
        "residual_presentation": residual_presentation,
        "page_fraction_bp": page_fraction_bp,
        "continuation": continuation,
        "coverage_audit": coverage_audit,
        "absolute": absolute,
        "page_area_bp": page_area_bp,
        "background": background,
        "provenance": provenance,
    }


def _load_residual_presentation(table: Any) -> dict[str, Any]:
    """Read the conservative, explicitly uncalibrated review granularity policy."""
    if not isinstance(table, dict):
        raise ContractError(
            "the grouping configuration has no [grouping.residual_presentation] table"
        )
    expected = set(_RESIDUAL_PRESENTATION_FIELDS) | {"provenance"}
    unexpected = sorted(set(table) - expected)
    if unexpected:
        raise ContractError(
            "the grouping configuration's [grouping.residual_presentation] carries "
            f"unknown field(s) {unexpected}; an unread policy field cannot be applied"
        )
    missing = sorted(expected - set(table))
    if missing:
        raise ContractError(
            "the grouping configuration's [grouping.residual_presentation] is missing "
            f"field(s) {missing}"
        )
    values = {name: table[name] for name in _RESIDUAL_PRESENTATION_FIELDS}
    invalid = [name for name, value in values.items() if not _is_plain_int(value) or value < 0]
    if invalid:
        raise ContractError(
            "the grouping configuration's [grouping.residual_presentation] has invalid "
            f"non-negative integer field(s) {invalid}"
        )
    values["provenance"] = _load_provenance(
        table.get("provenance"), "[grouping.residual_presentation.provenance]"
    )
    return values


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


# _STRING_PROVENANCE_FIELDS is derived, not hand-copied, so a field added to
# _PROVENANCE_FIELDS is validated by construction rather than silently skipped.
_TYPED_PROVENANCE_FIELDS: Final = frozenset({"sample_count", "calibrated_for_this_corpus"})
_STRING_PROVENANCE_FIELDS: Final = tuple(sorted(set(_PROVENANCE_FIELDS) - _TYPED_PROVENANCE_FIELDS))
assert _TYPED_PROVENANCE_FIELDS | set(_STRING_PROVENANCE_FIELDS) == set(_PROVENANCE_FIELDS)


def _load_provenance(provenance: Any, where: str) -> dict[str, Any]:
    """Validate one declared provenance block against the closed schema.

    This policy carries a separate provenance block per table (each with its
    own sample count and calibration claim) rather than one for the whole
    file, so a single number can't be over-read onto values it doesn't cover.
    `where` names the table, so a refusal points at the block that is wrong.
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


def _load_continuation(table: Any) -> dict[str, Any]:
    """Read `[grouping.continuation]` and its own provenance.

    `page_edge_reach_bp` must be a basis-point value in 1..`BASIS_POINTS`.
    Zero is excluded from the policy range (though a group can still measure
    zero distance from an edge); 10000 is permitted even though it doesn't
    discriminate by proximity, since the shipped value rests on measurement,
    not on this range alone.
    """
    if not isinstance(table, dict):
        raise ContractError("the grouping configuration has no [grouping.continuation] table")
    _refuse_forbidden_names(table, "[grouping.continuation]")
    expected = set(_CONTINUATION_BP_FIELDS) | {"provenance"}
    unexpected = sorted(set(table) - expected)
    if unexpected:
        raise ContractError(
            f"the grouping configuration's [grouping.continuation] carries unknown field(s) "
            f"{unexpected}; an unread policy field cannot be applied"
        )
    missing = sorted(expected - set(table))
    if missing:
        raise ContractError(
            f"the grouping configuration's [grouping.continuation] is missing field(s) {missing}"
        )
    values = {name: table[name] for name in _CONTINUATION_BP_FIELDS}
    reach = values["page_edge_reach_bp"]
    if not _is_plain_int(reach):
        raise ContractError(
            "the grouping configuration's [grouping.continuation] page_edge_reach_bp is not an "
            "integer"
        )
    if not 0 < reach <= _BASIS_POINTS:
        raise ContractError(
            "the grouping configuration's [grouping.continuation] page_edge_reach_bp is not a "
            f"positive basis-point integer in the supported inclusive range "
            f"1..{_BASIS_POINTS}; the permitted upper endpoint is a broad whole-page policy "
            "value"
        )
    values["provenance"] = _load_provenance(
        table.get("provenance"), "[grouping.continuation.provenance]"
    )
    return values


def _load_page_area_bp(table: Any) -> dict[str, Any]:
    """Read `[grouping.page_area_bp]` and its own provenance.

    `page_spanning_area_bp` must be in 1..10000: at or below zero every
    component on every page would be page-spanning and the grouping pass
    would withhold the whole page; past 10000 no bounding box can ever reach
    it, since it cannot exceed the page it is measured against.
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


def _load_background(table: Any) -> dict[str, Any]:
    """Read `[grouping.background]` and its own provenance.

    Its own provenance block because these four values are measured on 127
    real pages, one of several such blocks in this file (continuation is
    measured on 44, and page-area and the coverage audit carry their own
    too). The values themselves are
    validated by `common.background.validate_background_table`, shared with
    the Ink Map and the Recensor's residual-ink audit so all three refuse the
    same malformed value; this function adds only the forbidden-name refusal,
    the closed field set, and the provenance schema.
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
    residual_aggregate_max_pixel_count: int
    residual_aggregate_max_area_px: int
    page_spanning_area_bp: int


def resolve_thresholds(config: dict[str, Any], width: int, height: int) -> GroupingThresholds:
    """Resolve one page's own basis-point thresholds into pixel integers.

    `margin_px` resolves against `width`; every other page_fraction_bp field
    (and continuation's) resolves against `height`. `gap_tolerance_px`, the
    three counts and `page_spanning_area_bp` pass through unresolved: the
    counts aren't page-fraction quantities, and the area field's basis is both
    dimensions at once, so it stays in basis points at the one place it's
    compared (`grouping.partition_page_spanning`).
    """
    _validate_dimensions(width, height, "page")
    bp = config["page_fraction_bp"]
    return GroupingThresholds(
        margin_px=_pad_amount(width, bp["margin_bp"]),
        chain_gap_px=_pad_amount(height, bp["chain_gap_bp"]),
        anchor_reach_px=_pad_amount(height, bp["anchor_reach_bp"]),
        brace_min_height_px=_pad_amount(height, bp["brace_min_height_bp"]),
        page_edge_reach_px=_pad_amount(height, config["continuation"]["page_edge_reach_bp"]),
        review_priority_min_dimension_px=_pad_amount(
            height, bp["review_priority_min_dimension_bp"]
        ),
        fallback_overlap_px=_pad_amount(height, bp["fallback_overlap_bp"]),
        gap_tolerance_px=config["absolute"]["gap_tolerance_px"],
        max_residual_components=config["max_residual_components"],
        max_secondary_proposals=config["max_secondary_proposals"],
        fallback_bands=config["fallback_bands"],
        residual_aggregate_max_pixel_count=config["residual_aggregate_max_pixel_count"],
        residual_aggregate_max_area_px=config["residual_aggregate_max_area_px"],
        # Carried on the published thresholds, not just used internally by
        # `group_page`, so `run.py` can repeat the same page-spanning
        # partition later and check the withheld components it recorded.
        page_spanning_area_bp=config["page_area_bp"]["page_spanning_area_bp"],
    )
