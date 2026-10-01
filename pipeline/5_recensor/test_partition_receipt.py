"""The Recensor partition receipt's builder and validator refuse every inconsistent item."""

from __future__ import annotations

import pytest

from common.contracts.errors import SchemaRefusal
from common.contracts.outcomes import INTERIM_GRANULARITY_BASIS


def test_a_run_that_proposed_no_acts_gets_a_visibly_partial_receipt_not_a_refusal():
    """An empty denominator is a fact about the run, not a malformed receipt.

    The Designator proposing nothing at all is the silent-failure shape this whole
    pipeline exists to catch, and the Armarium's own aggregate already
    treats a sealed page nobody marked out as a named partial rather than an
    error. Refusing to build the receipt would have turned that into a traceback
    at the one boundary whose job is making it visible -- neither lane noticed,
    because no shipped scenario produces a run with zero expected acts.
    """
    from common.recensor_receipt import (
        EMPTY_DENOMINATOR_REASON,
        build_recensor_partition_receipt,
        validate_recensor_partition_receipt,
    )

    receipt = build_recensor_partition_receipt(
        run_id="r",
        config_digest="a" * 64,
        proposal_seal_ref={
            "relative_path": "2_designator/artifacts/proposal-seal.json",
            "sha256": "b" * 64,
        },
        items=[],
    )
    assert receipt["expected_act_count"] == 0
    assert receipt["recensor_status"] == "partial"
    assert receipt["reasons"] == [EMPTY_DENOMINATOR_REASON]
    assert validate_recensor_partition_receipt(receipt) == receipt


def _valid_coverage() -> dict:
    """A self-consistent witness coverage record, the shape `common.contracts.
    outcomes.witness_coverage` actually returns -- one `read`, one
    `genuinely-empty` (both COMPLETED), one `not-run` (UNRESOLVED), against a
    floor of 3, so `under_witnessed` (2 completed < floor 3) is genuinely True
    and every derived field has real content to tamper with below."""
    return {
        "configured": 3,
        "floor": 3,
        "by_outcome": {"read": 1, "genuinely-empty": 1, "not-run": 1},
        "by_class": {"completed": 2, "unresolved": 1, "failed": 0},
        "under_witnessed": True,
        "unresolved_chairs": 1,
        "page_granularity_only": 0,
        "health_unrecorded": 1,
        "shortfalls": {"failed": 0, "truncated": 0, "unaligned": 1},
        "granularity_basis": INTERIM_GRANULARITY_BASIS,
    }


def _item_with_coverage(coverage: dict) -> dict:
    return {
        "act_id": "a1",
        "act_key": "a1",
        "designator_outcome": "proposed",
        "review_ref": {
            "relative_path": "5_recensor/artifacts/review/none.json",
            "sha256": "b" * 64,
        },
        "review_outcome": "held-for-review",
        "partition_class": "unresolved",
        "coverage": coverage,
    }


def _build_with_coverage(coverage: dict):
    from common.recensor_receipt import build_recensor_partition_receipt

    return build_recensor_partition_receipt(
        run_id="r",
        config_digest="a" * 64,
        proposal_seal_ref={
            "relative_path": "2_designator/artifacts/proposal-seal.json",
            "sha256": "b" * 64,
        },
        items=[_item_with_coverage(coverage)],
    )


def test_self_consistent_coverage_builds_a_receipt_cleanly():
    """Proves `_valid_coverage` is genuinely valid before the tampered variants
    below prove each mismatch it can be turned into is refused -- otherwise a
    refusal below could be firing on the wrong field entirely."""
    receipt = _build_with_coverage(_valid_coverage())
    assert receipt["items"][0]["coverage"] == _valid_coverage()


# Each of these three drives a genuinely different coverage fault, and each
# now pins the message for *its* fault rather than one sentence all three
# shared. While that sentence was common to every branch, any one of these
# tests would have passed on the wrong refusal firing -- which is the same
# false-green shape the receipt itself exists to refuse.
def test_a_by_class_that_disagrees_with_by_outcome_is_refused():
    # Self-consistent in every OTHER respect -- it totals `configured`, its
    # unresolved count matches `unresolved_chairs`, and `under_witnessed` still
    # follows from its completed count against the floor -- so the only thing
    # wrong with it is that classifying `by_outcome` derives
    # {completed: 2, unresolved: 1, failed: 0} instead. The earlier fixture here
    # tripped the `unresolved_chairs` check first and never reached this one,
    # which nobody could see while both raised the same sentence.
    coverage = dict(
        _valid_coverage(),
        by_class={"completed": 2, "unresolved": 0, "failed": 1},
        unresolved_chairs=0,
    )
    with pytest.raises(SchemaRefusal, match="does not fall out of its own per-outcome counts"):
        _build_with_coverage(coverage)


def test_an_under_witnessed_flag_disagreeing_with_the_floor_formula_is_refused():
    coverage = dict(_valid_coverage(), under_witnessed=False)  # 2 completed < floor 3 is True
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
    """Spec 09 test 1: 'duplicate identities are errors.' Two items naming the
    same act_id is a fabricated denominator no real Recensor pass can produce --
    the receipt itself refuses it (via the strictly-sorted check, since a repeat
    is never `>` the one before it) rather than relying on every future caller
    to never construct one."""
    from common.recensor_receipt import build_recensor_partition_receipt

    item = _item_with_coverage(_valid_coverage())
    with pytest.raises(SchemaRefusal, match="strictly sorted"):
        build_recensor_partition_receipt(
            run_id="r",
            config_digest="a" * 64,
            proposal_seal_ref={
                "relative_path": "2_designator/artifacts/proposal-seal.json",
                "sha256": "b" * 64,
            },
            items=[item, dict(item)],
        )


def test_unsorted_items_are_refused_on_direct_validation():
    """`build_recensor_partition_receipt` always sorts before returning, so it
    cannot itself produce an unsorted receipt. The strict order is a receipt
    invariant checked again by `validate_recensor_partition_receipt`, not merely
    an artifact of the one builder that happens to sort today -- proven here by
    handing a validly-built receipt back with its items reversed and its
    self-hash recomputed over the tampered order, the same way a hand-edited
    file on disk would reach validation."""
    from common.contracts.canonical import self_hash
    from common.recensor_receipt import (
        build_recensor_partition_receipt,
        validate_recensor_partition_receipt,
    )

    first = _item_with_coverage(_valid_coverage())
    second = dict(_item_with_coverage(_valid_coverage()), act_id="a2", act_key="a2")
    receipt = build_recensor_partition_receipt(
        run_id="r",
        config_digest="a" * 64,
        proposal_seal_ref={
            "relative_path": "2_designator/artifacts/proposal-seal.json",
            "sha256": "b" * 64,
        },
        items=[first, second],
    )
    assert [item["act_id"] for item in receipt["items"]] == ["a1", "a2"]

    reversed_record = dict(receipt, items=list(reversed(receipt["items"])))
    reversed_record["self_hash"] = self_hash(reversed_record)
    with pytest.raises(SchemaRefusal, match="strictly sorted"):
        validate_recensor_partition_receipt(reversed_record)


@pytest.mark.parametrize(
    "review_outcome,expected_class",
    [
        ("accepted", "completed"),
        ("recovery-requested", "unresolved"),
        ("confirmed-blank", "completed"),
        ("held-for-review", "unresolved"),
        ("failed", "failed"),
    ],
)
def test_every_recensor_terminal_set_combination_builds_a_matching_receipt_item(
    review_outcome, expected_class
):
    """Spec 09 test 1: 'table-driven -- every terminal-set combination.' The
    Recensor's closed outcome vocabulary (`common/contracts/outcomes.py`) has
    exactly five members; this drives every one of them through the receipt and
    checks the partition class `classify` actually derives, not one asserted by
    the caller (`_validate_item` refuses a mismatch, so a wrong table entry here
    would fail loudly rather than pass silently)."""
    from common.recensor_receipt import build_recensor_partition_receipt

    item = dict(
        _item_with_coverage(_valid_coverage()),
        review_outcome=review_outcome,
        partition_class=expected_class,
    )
    receipt = build_recensor_partition_receipt(
        run_id="r",
        config_digest="a" * 64,
        proposal_seal_ref={
            "relative_path": "2_designator/artifacts/proposal-seal.json",
            "sha256": "b" * 64,
        },
        items=[item],
    )
    assert receipt["items"][0]["partition_class"] == expected_class
    assert receipt["by_partition_class"][expected_class] == 1


def test_a_receipt_item_refuses_a_partition_class_its_review_does_not_derive():
    """Pin the refusal, whose wording was repaired after review."""
    from common.recensor_receipt import build_recensor_partition_receipt

    item = dict(
        _item_with_coverage(_valid_coverage()),
        review_outcome="accepted",
        partition_class="failed",
    )
    with pytest.raises(
        SchemaRefusal,
        match=("names partition_class 'failed', but review_outcome 'accepted' derives 'completed'"),
    ):
        build_recensor_partition_receipt(
            run_id="r",
            config_digest="a" * 64,
            proposal_seal_ref={
                "relative_path": "2_designator/artifacts/proposal-seal.json",
                "sha256": "b" * 64,
            },
            items=[item],
        )


def test_an_empty_receipt_may_not_claim_to_be_complete():
    """The status derives from the reasons, and the reason is not optional."""
    from common.contracts.canonical import self_hash
    from common.recensor_receipt import (
        build_recensor_partition_receipt,
        validate_recensor_partition_receipt,
    )

    receipt = build_recensor_partition_receipt(
        run_id="r",
        config_digest="a" * 64,
        proposal_seal_ref={
            "relative_path": "2_designator/artifacts/proposal-seal.json",
            "sha256": "b" * 64,
        },
        items=[],
    )
    forged = dict(receipt, recensor_status="complete", reasons=[])
    forged["self_hash"] = self_hash({k: v for k, v in forged.items() if k != "self_hash"})
    with pytest.raises(SchemaRefusal, match="does not derive from its items"):
        validate_recensor_partition_receipt(forged)


# --- Audit-and-repair regression (F-O4) -----------------------------------------
#
# Audit-and-repair seat 3, R0. `page_granularity_only` is subtracted from the
# completed count before the v2 block that typed it ever runs, so a non-integer
# value left `_validate_coverage` through a raw TypeError -- not a ContractError,
# and so not something a caller that refuses malformed evidence by name can catch.


@pytest.mark.parametrize("value", ["1", 1.0, None, [1]])
def test_a_non_integer_page_granularity_count_is_a_named_refusal(value):
    """Every other malformed coverage field in this validator is a named refusal."""
    from common.recensor_receipt import _validate_coverage

    coverage = dict(_valid_coverage(), page_granularity_only=value)
    with pytest.raises(SchemaRefusal, match="invalid page_granularity_only"):
        _validate_coverage(coverage)


# --- Audit-and-repair regression (F-O3) -----------------------------------------
#
# Audit-and-repair seat 3, R0. `witness_coverage` counts a chair toward the
# act floor only when its outcome IS a reading, but `_validate_coverage`
# rederived the same number from the ATTESTATORES COMPLETED class -- which is
# wider, because it also holds `excluded`, an approval-bound exclusion that never
# looked at the ink. Writer and validator therefore disagreed for any act with an
# excluded chair, and `build_recensor_partition_receipt` validates every item it
# builds, so no receipt could be written for such an act at all.


def _coverage_with_an_excluded_chair() -> dict:
    from common.contracts.outcomes import witness_coverage

    outcomes = {"attestator_1": "read", "attestator_2": "read", "attestator_3": "excluded"}
    attachments = {
        "attestator_1": {
            "attached": True,
            "comparable": True,
            "truncated": False,
            "health_unrecorded": False,
        },
        "attestator_2": {
            "attached": True,
            "comparable": True,
            "truncated": False,
            "health_unrecorded": False,
        },
        "attestator_3": {
            "attached": False,
            "comparable": False,
            "truncated": None,
            "health_unrecorded": True,
        },
    }
    return witness_coverage(outcomes, 3, attachments=attachments)


def test_an_approval_bound_exclusion_does_not_make_the_receipt_refuse_its_own_writer():
    """The floor arithmetic and its rederivation must count the same chairs.

    An `excluded` chair is COMPLETED class but is not a reading, so
    `witness_coverage` records `under_witnessed=True` for two reads against a
    floor of three. The rederivation must reach the same answer instead of
    reading three completed chairs off `by_class` and calling the record a liar.
    """
    coverage = _coverage_with_an_excluded_chair()
    assert coverage["under_witnessed"] is True
    assert coverage["by_class"]["completed"] == 3
    from common.recensor_receipt import _validate_coverage

    _validate_coverage(coverage)


def test_the_reading_outcome_set_has_exactly_one_definition():
    """A second literal spelling of this closed set beside the floor arithmetic
    that depends on it is a silent divergence waiting to happen: a member added
    to one and not the other would attach a chair that never read.
    """
    import common.contracts.outcomes as vocabulary
    import common.stage as stage

    assert stage.WITNESS_READING_OUTCOMES is vocabulary.WITNESS_READING_OUTCOMES
    assert vocabulary.WITNESS_READING_OUTCOMES < {
        outcome
        for outcome, klass in vocabulary.VOCABULARIES[vocabulary.ATTESTATORES].items()
        if klass is vocabulary.OutcomeClass.COMPLETED
    }, "a reading outcome is completed-class, but the completed class is wider"
