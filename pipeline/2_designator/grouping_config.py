"""The sealed grouping/reconciliation policy: `config/designator_grouping.toml`.

Modelled directly on `geometry.load_padding_config` /
`geometry._load_padding_provenance`: a closed schema, refusals by name, and a
digest of the exact bytes read so the config can be bound into a run's seal.
This module owns loading and resolving the policy only -- it is not wired
into `common/stage.py` or `run.py` (that is units C and D's own work); a page
that wants resolved pixel thresholds calls `resolve_thresholds` itself, once
it has this module's config and its own page dimensions.

Two closed sub-tables, not one flat table, because the *basis* a threshold
resolves against is structural, not a naming convention: `page_fraction_bp`
values are basis points of the page's own WIDTH (`margin_bp`) or HEIGHT
(every other field) as declared in the config file's header comment, while
`absolute` values are raw pixel counts that must never be scaled by page
size at all. Putting a field in the wrong sub-table is refused by the closed
schema rather than caught by a comment nobody reads.

`primary_margin` and `secondary_margin` are refused by name wherever they
appear, in either sub-table or at the policy's own top level. They are
`structure.PRIMARY_MARGIN` and `structure.SECONDARY_MARGIN` -- 8-bit ink
intensity offsets, not page geometry -- and stay Python module constants
because `common/test_designator_recensor_ink_calibration.py` is an AST pin
that reads `SECONDARY_MARGIN` as a source literal in `structure.py` and
cross-checks it against the Recensor's own contrast constant. A per-run
config value for either name would make that cross-stage invariant
unenforceable statically.
"""

import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final

from geometry import _PROVENANCE_FIELDS, _is_plain_int, _pad_amount, _validate_dimensions

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
)
_ABSOLUTE_FIELDS: Final = ("gap_tolerance_px",)

# Names that must never appear anywhere in this policy -- see module
# docstring. Checked explicitly, with a message that names them, rather than
# left to fall out of the generic "unknown field" refusal, because a reader
# hunting for *why* these two names are forbidden should find the reason at
# the refusal site, not have to already know it.
_FORBIDDEN_NAMES: Final = ("primary_margin", "secondary_margin")

_GROUPING_TOP_FIELDS: Final = (
    "max_residual_components",
    "page_fraction_bp",
    "absolute",
    "provenance",
)


def _refuse_forbidden_names(fields: Any, where: str) -> None:
    if not isinstance(fields, dict):
        return
    found = sorted(_FORBIDDEN_NAMES_present(fields))
    if found:
        raise ContractError(
            f"the grouping configuration's {where} carries forbidden field(s) {found}; "
            "primary_margin/secondary_margin are ink-intensity offsets pinned as Python "
            "module constants in structure.py by common/test_designator_recensor_ink_calibration.py "
            "and may never become a per-run config value"
        )


def _FORBIDDEN_NAMES_present(fields: dict) -> list[str]:
    return [name for name in _FORBIDDEN_NAMES if name in fields]


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

    max_residual_components = grouping["max_residual_components"]
    if not _is_plain_int(max_residual_components) or max_residual_components < 0:
        raise ContractError(
            "the grouping configuration's max_residual_components is not a non-negative integer"
        )

    page_fraction_bp = _load_closed_int_table(
        grouping.get("page_fraction_bp"),
        _PAGE_FRACTION_BP_FIELDS,
        "[grouping.page_fraction_bp]",
    )
    absolute = _load_closed_int_table(
        grouping.get("absolute"), _ABSOLUTE_FIELDS, "[grouping.absolute]"
    )
    provenance = _load_grouping_provenance(grouping.get("provenance"))

    return {
        "config_sha256": digest_bytes(data),
        "max_residual_components": max_residual_components,
        "page_fraction_bp": page_fraction_bp,
        "absolute": absolute,
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


def _load_grouping_provenance(provenance: Any) -> dict[str, Any]:
    """Validate the grouping config's declared provenance against its closed schema.

    Identical shape to `geometry._load_padding_provenance`, for the same
    reason: every field is required and checked for shape, so a provenance
    block that is merely present cannot stand in for one that actually
    answers "where did this number come from."
    """
    if not isinstance(provenance, dict):
        raise ContractError(
            "the grouping configuration has no [grouping.provenance] table; a "
            "policy value with no declared source may not be shipped as a default"
        )
    unexpected = sorted(set(provenance) - set(_PROVENANCE_FIELDS))
    if unexpected:
        raise ContractError(
            f"the grouping configuration's provenance carries unknown field(s) {unexpected}; "
            "provenance is a closed schema so an unread field cannot be trusted"
        )
    missing = sorted(set(_PROVENANCE_FIELDS) - set(provenance))
    if missing:
        raise ContractError(
            f"the grouping configuration's provenance is missing field(s) {missing}"
        )
    for field in ("source", "corpus", "sample_unit", "statistic", "caveat"):
        if not isinstance(provenance[field], str) or not provenance[field].strip():
            raise ContractError(
                f"the grouping configuration's provenance field {field!r} is not a non-empty string"
            )
    if not _is_plain_int(provenance["sample_count"]) or provenance["sample_count"] < 0:
        raise ContractError(
            "the grouping configuration's provenance sample_count is not a non-negative integer"
        )
    if not isinstance(provenance["calibrated_for_this_corpus"], bool):
        raise ContractError(
            "the grouping configuration's provenance calibrated_for_this_corpus is not a boolean"
        )
    return dict(provenance)


@dataclass(frozen=True)
class GroupingThresholds:
    """Resolved, page-sized-specific pixel thresholds. Ints only -- never a float."""

    margin_px: int
    chain_gap_px: int
    anchor_reach_px: int
    brace_min_height_px: int
    page_edge_reach_px: int
    review_priority_min_dimension_px: int
    gap_tolerance_px: int
    max_residual_components: int


def resolve_thresholds(config: dict[str, Any], width: int, height: int) -> GroupingThresholds:
    """Resolve one page's own basis-point thresholds into pixel integers.

    `margin_px` resolves against `width`; every other page_fraction_bp field
    resolves against `height` -- the basis each field's config comment
    declares as a design decision (SPEC_C section 2), not a property
    recovered from the retired pixel constant it replaces.
    `gap_tolerance_px` and `max_residual_components` pass through unresolved:
    neither is a page-fraction quantity.

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
        gap_tolerance_px=config["absolute"]["gap_tolerance_px"],
        max_residual_components=config["max_residual_components"],
    )
