"""The Recensor partition receipt's builder and validator refuse every inconsistent item."""

from __future__ import annotations

import pytest

from common.contracts.canonical import self_hash
from common.contracts.errors import SchemaRefusal
from common.recensor_receipt import (
    RETIRED_RECENSOR_PARTITION_RECEIPT_SCHEMAS,
    _validate_coverage,
    build_recensor_reading_receipt,
    validate_recensor_partition_receipt,
)

DIGEST = "b" * 64


def _page_reading(ordinal: int) -> dict:
    return {
        "page_ordinal": ordinal,
        "reading_ref": {
            "relative_path": f"4_perlector/artifacts/page-reading/p{ordinal}.json",
            "sha256": DIGEST,
        },
    }


def _build(items: list[dict], pages: tuple[int, ...] = (1,)):
    return build_recensor_reading_receipt(
        run_id="r",
        config_digest="a" * 64,
        page_reading_refs=[_page_reading(ordinal) for ordinal in pages],
        items=items,
    )


def test_a_receipt_names_every_sealed_page_so_an_empty_denominator_is_refused():
    """Every sealed page is at least one unit, so a run whose reading counted
    nothing cannot be written as a receipt at all; it is refused by name."""
    with pytest.raises(SchemaRefusal, match="names no page reading"):
        _build([], pages=())


def _valid_coverage() -> dict:
    """A self-consistent page-read coverage record -- one `read`, one
    `genuinely-empty` (both COMPLETED), one `not-run` (UNRESOLVED), against a
    floor of 3, so `under_witnessed` (2 reads < floor 3) is genuinely True and
    every derived field has real content to tamper with below."""
    return {
        "configured": 3,
        "floor": 3,
        "by_outcome": {"read": 1, "genuinely-empty": 1, "not-run": 1},
        "by_class": {"completed": 2, "unresolved": 1, "failed": 0},
        "under_witnessed": True,
        "unresolved_chairs": 1,
        "health_unrecorded": 1,
        "shortfalls": {"failed": 0, "truncated": 0, "unaligned": 1},
    }


def _item_with_coverage(coverage: dict, act_id: str = "act_a1", act_key: str = "p1:1") -> dict:
    return {
        "act_id": act_id,
        "act_key": act_key,
        "page_disposition": "read",
        "review_ref": {"relative_path": "5_recensor/artifacts/review/none.json", "sha256": DIGEST},
        "review_outcome": "held-for-review",
        "partition_class": "unresolved",
        "coverage": coverage,
        "release_reason": None,
    }


def _build_with_coverage(coverage: dict):
    return _build([_item_with_coverage(coverage)])


def test_self_consistent_coverage_builds_a_receipt_cleanly():
    """Proves `_valid_coverage` is genuinely valid before the tampered variants
    below prove each mismatch it can be turned into is refused."""
    receipt = _build_with_coverage(_valid_coverage())
    assert receipt["items"][0]["coverage"] == _valid_coverage()


def test_a_by_class_that_disagrees_with_by_outcome_is_refused():
    coverage = dict(
        _valid_coverage(),
        by_class={"completed": 2, "unresolved": 0, "failed": 1},
        unresolved_chairs=0,
    )
    with pytest.raises(SchemaRefusal, match="does not fall out of its own per-outcome counts"):
        _build_with_coverage(coverage)


def test_an_under_witnessed_flag_disagreeing_with_the_floor_formula_is_refused():
    coverage = dict(_valid_coverage(), under_witnessed=False)  # 2 reads < floor 3 is True
    with pytest.raises(SchemaRefusal, match="claims under_witnessed=False"):
        _build_with_coverage(coverage)


def test_an_unresolved_chairs_count_disagreeing_with_by_class_is_refused():
    coverage = dict(_valid_coverage(), unresolved_chairs=0)  # by_class["unresolved"] is 1
    with pytest.raises(SchemaRefusal, match="unresolved chair\\(s\\) while its own by_class"):
        _build_with_coverage(coverage)


def test_an_unknown_witness_outcome_in_by_outcome_is_refused():
    coverage = dict(_valid_coverage(), by_outcome={"not-a-real-outcome": 3})
    with pytest.raises(SchemaRefusal, match="unknown witness outcome"):
        _build_with_coverage(coverage)


def test_a_missing_coverage_field_is_refused_as_malformed():
    coverage = {key: value for key, value in _valid_coverage().items() if key != "floor"}
    with pytest.raises(SchemaRefusal, match="malformed witness coverage"):
        _build_with_coverage(coverage)


def test_a_negative_coverage_count_is_refused():
    coverage = dict(_valid_coverage(), configured=-1)
    with pytest.raises(SchemaRefusal, match="invalid witness coverage counts"):
        _build_with_coverage(coverage)


def test_duplicate_act_identities_are_refused():
    """Spec 09 test 1: 'duplicate identities are errors.' The receipt itself
    refuses it (via the strictly-sorted check, since a repeat is never `>` the
    one before it) rather than relying on every future caller to never
    construct one."""
    item = _item_with_coverage(_valid_coverage())
    with pytest.raises(SchemaRefusal, match="strictly sorted"):
        _build([item, dict(item, act_key="p1:2")])


def test_unsorted_items_are_refused_on_direct_validation():
    """The builder always sorts, so the strict order is proven a receipt
    invariant by handing a validly-built receipt back with its items reversed
    and its self-hash recomputed over the tampered order."""
    first = _item_with_coverage(_valid_coverage())
    second = _item_with_coverage(_valid_coverage(), act_id="act_a2", act_key="p1:2")
    receipt = _build([first, second])
    assert [item["act_id"] for item in receipt["items"]] == ["act_a1", "act_a2"]

    reversed_record = dict(receipt, items=list(reversed(receipt["items"])))
    reversed_record["self_hash"] = self_hash(
        {k: v for k, v in reversed_record.items() if k != "self_hash"}
    )
    with pytest.raises(SchemaRefusal, match="strictly sorted"):
        validate_recensor_partition_receipt(reversed_record)


@pytest.mark.parametrize(
    "review_outcome,expected_class",
    [
        ("accepted", "completed"),
        ("confirmed-blank", "completed"),
        ("held-for-review", "unresolved"),
        ("failed", "failed"),
    ],
)
def test_every_recensor_terminal_set_combination_builds_a_matching_receipt_item(
    review_outcome, expected_class
):
    """Spec 09 test 1: 'table-driven -- every terminal-set combination.'"""
    item = dict(
        _item_with_coverage(_valid_coverage()),
        review_outcome=review_outcome,
        partition_class=expected_class,
    )
    receipt = _build([item])
    assert receipt["items"][0]["partition_class"] == expected_class
    assert receipt["by_partition_class"][expected_class] == 1


def test_a_review_outcome_no_stage_produces_is_refused():
    """The Recensor asks for no recovery, so a `recovery-requested` review is forged."""
    item = dict(
        _item_with_coverage(_valid_coverage()),
        review_outcome="recovery-requested",
        partition_class="unresolved",
    )
    with pytest.raises(SchemaRefusal, match="unknown Recensor outcome"):
        _build([item])


def test_a_receipt_item_refuses_a_partition_class_its_review_does_not_derive():
    item = dict(
        _item_with_coverage(_valid_coverage()),
        review_outcome="accepted",
        partition_class="failed",
    )
    with pytest.raises(
        SchemaRefusal,
        match=("names partition_class 'failed', but review_outcome 'accepted' derives 'completed'"),
    ):
        _build([item])


def test_a_partial_receipt_may_not_claim_to_be_complete():
    """The status derives from the reasons, and the reason is not optional."""
    receipt = _build_with_coverage(_valid_coverage())
    assert receipt["recensor_status"] == "partial"
    forged = dict(receipt, recensor_status="complete", reasons=[])
    forged["self_hash"] = self_hash({k: v for k, v in forged.items() if k != "self_hash"})
    with pytest.raises(SchemaRefusal, match="does not derive from its items"):
        validate_recensor_partition_receipt(forged)


@pytest.mark.parametrize("schema", sorted(RETIRED_RECENSOR_PARTITION_RECEIPT_SCHEMAS))
def test_a_receipt_over_proposal_acts_is_refused_by_its_schema_name(schema):
    with pytest.raises(SchemaRefusal, match=f"written as {schema}"):
        validate_recensor_partition_receipt({"schema": schema})


# --- Audit-and-repair regression (F-O3) -----------------------------------------
#
# `witness_coverage` counts a chair toward the floor only when its outcome IS a
# reading, but the validator once rederived the same number from the
# ATTESTATORES COMPLETED class -- which is wider, because it also holds
# `excluded`, an approval-bound exclusion that never looked at the ink.


def test_an_approval_bound_exclusion_does_not_make_the_receipt_refuse_its_own_writer():
    """The floor arithmetic and its rederivation must count the same chairs.

    An `excluded` chair is COMPLETED class but is not a reading, so two reads
    against a floor of three are under-witnessed although three chairs are
    completed-class.
    """
    coverage = {
        "configured": 3,
        "floor": 3,
        "by_outcome": {"read": 2, "excluded": 1},
        "by_class": {"completed": 3, "unresolved": 0, "failed": 0},
        "under_witnessed": True,
        "unresolved_chairs": 0,
        "health_unrecorded": 0,
        "shortfalls": {"failed": 0, "truncated": 0, "unaligned": 0},
    }
    _validate_coverage(coverage)


def test_the_reading_outcome_set_has_exactly_one_definition():
    """A second literal spelling of this closed set beside the floor arithmetic
    that depends on it is a silent divergence waiting to happen: a member added
    to one and not the other would count a chair that never read.
    """
    import common.contracts.outcomes as vocabulary
    import common.stage as stage

    assert stage.WITNESS_READING_OUTCOMES is vocabulary.WITNESS_READING_OUTCOMES
    assert vocabulary.WITNESS_READING_OUTCOMES < {
        outcome
        for outcome, klass in vocabulary.VOCABULARIES[vocabulary.ATTESTATORES].items()
        if klass is vocabulary.OutcomeClass.COMPLETED
    }, "a reading outcome is completed-class, but the completed class is wider"
