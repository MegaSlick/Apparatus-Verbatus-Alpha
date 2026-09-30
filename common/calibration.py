"""Small shared predicates for sealed calibration provenance."""

from __future__ import annotations

from typing import Any, Final

from common.contracts.canonical import is_plain_int
from common.contracts.errors import ContractError


def calibrated_claim_has_sample_evidence(
    calibrated_for_this_corpus: bool, sample_count: int | None
) -> bool:
    """A calibration claim with a published count needs at least one sample.

    ``None`` is deliberately distinct from zero: geometry provenance does not
    publish a count, so this predicate cannot invent one. Callers validate the
    boolean and count types before applying this semantic relation.
    """
    return not calibrated_for_this_corpus or sample_count is None or sample_count > 0


#: The closed schema of one sealed policy table's `provenance` block.
PROVENANCE_FIELDS: Final = frozenset(
    {
        "source",
        "corpus",
        "sample_unit",
        "sample_count",
        "statistic",
        "calibrated_for_this_corpus",
        "caveat",
    }
)
TYPED_PROVENANCE_FIELDS: Final = frozenset({"sample_count", "calibrated_for_this_corpus"})
#: Derived rather than listed, so a field added to the schema is type-checked.
STRING_PROVENANCE_FIELDS: Final = tuple(sorted(PROVENANCE_FIELDS - TYPED_PROVENANCE_FIELDS))


def validate_provenance_block(
    provenance: Any, *, where: str, owner: str = "the grouping configuration"
) -> dict[str, Any]:
    """One declared provenance block, held to the closed schema.

    Every field is required and checked for shape, so a block that is present
    but says nothing (an empty string, a calibration claim over no samples)
    is refused. `owner` names the configuration and `where` the table, so a
    refusal points at the block that is wrong.
    """
    if not isinstance(provenance, dict):
        raise ContractError(
            f"{owner} has no {where} table; a policy value with no declared source may not "
            "be shipped as a default"
        )
    unexpected = sorted(set(provenance) - PROVENANCE_FIELDS)
    if unexpected:
        raise ContractError(
            f"{owner}'s {where} carries unknown field(s) {unexpected}; provenance is a closed "
            "schema so an unread field cannot be trusted"
        )
    missing = sorted(PROVENANCE_FIELDS - set(provenance))
    if missing:
        raise ContractError(f"{owner}'s {where} is missing field(s) {missing}")
    for field in STRING_PROVENANCE_FIELDS:
        if not isinstance(provenance[field], str) or not provenance[field].strip():
            raise ContractError(f"{owner}'s {where} field {field!r} is not a non-empty string")
    if not is_plain_int(provenance["sample_count"]) or provenance["sample_count"] < 0:
        raise ContractError(f"{owner}'s {where} sample_count is not a non-negative integer")
    if not isinstance(provenance["calibrated_for_this_corpus"], bool):
        raise ContractError(f"{owner}'s {where} calibrated_for_this_corpus is not a boolean")
    if not calibrated_claim_has_sample_evidence(
        provenance["calibrated_for_this_corpus"], provenance["sample_count"]
    ):
        raise ContractError(
            f"{owner}'s {where} says calibrated_for_this_corpus but sample_count is zero"
        )
    return dict(provenance)
