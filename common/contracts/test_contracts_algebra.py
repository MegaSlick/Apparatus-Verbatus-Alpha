"""The outcome algebra: total, closed, and unable to grow a picker.

Meta-invariant #93 — tests record the ruling they enforce, verbatim, with date.
The ruling behind most of this file is harvest invariant #10: "Every unit
(page/act) entering a stage is accounted for at the stage boundary as exactly one
of completed / unresolved-with-evidence / failed; a unit in none of those sets is a
FATAL accounting imbalance, never a warning."

Meta-invariant #88 — no test here reports success over an empty population. Every
loop asserts an exact expected count, never "at least one", so a vocabulary that
quietly emptied would fail rather than pass vacuously.
"""

import pytest

from common.contracts import outcomes
from common.contracts.errors import ApprovalRefusal, FatalAccounting
from common.contracts.outcomes import (
    ArmariumCategory,
    OutcomeClass,
    check_algebra_is_total,
    classify,
    require_approval,
    run_aggregate,
    terminal_category,
    witness_coverage,
)
from common.contracts.stages import ARMARIUM, ATTESTATORES, DESIGNATOR, PERLECTOR, RECENSOR

# The exact shape of the algebra as this spec defines it. Pinned as counts so that
# adding a state without deciding its class and its terminal category fails here,
# loudly, naming what changed — rather than passing because the new state happened
# to be consistent with itself.
EXPECTED_VOCABULARY_SIZES = {
    "door": 2,
    "exemplar": 2,
    "designator": 4,
    "attestatores": 6,
    "perlector": 5,
    "recensor": 5,
    "archetypus": 2,
    "armarium": 5,
}


def test_algebra_is_total():
    """Both mappings total, and the two layers agree at every terminal edge."""
    check_algebra_is_total()


def test_vocabulary_shape_is_pinned():
    assert {
        stage: len(vocabulary) for stage, vocabulary in outcomes.VOCABULARIES.items()
    } == EXPECTED_VOCABULARY_SIZES


def test_every_outcome_has_exactly_one_class_and_one_terminal_decision():
    checked = 0
    for stage, vocabulary in outcomes.VOCABULARIES.items():
        for outcome in vocabulary:
            assert isinstance(classify(stage, outcome), OutcomeClass)
            category = terminal_category(stage, outcome)
            assert category is None or isinstance(category, ArmariumCategory)
            checked += 1
    assert checked == sum(EXPECTED_VOCABULARY_SIZES.values())


# --- Blocker 4 / Sol finding B-2, closed and pinned shut -----------------------


def test_witness_failed_is_a_member_of_the_closed_vocabulary():
    """Sol B-2, 2026-07-30: "add `failed` to the witness outcome vocabulary and
    map it through the Recensor and Armarium aggregates."

    Spec 07 requires a failed re-read to derive `current=FAILED` (its retention
    ruling of 2026-07-30: attempts are append-only, "current" is the latest
    attempt with its honest status). The vocabulary listed only read / not-run /
    dead / genuinely-empty, so the supposedly closed algebra had no member for the
    exact case the ruling created. This test is the hole, welded.
    """
    assert classify(ATTESTATORES, "failed") is OutcomeClass.FAILED
    assert terminal_category(ATTESTATORES, "failed") is None


def test_witness_vocabulary_is_exactly_the_repaired_six():
    assert set(outcomes.VOCABULARIES[ATTESTATORES]) == {
        "read",
        "genuinely-empty",
        "failed",
        "dead",
        "not-run",
        "excluded",
    }


def test_witness_outcome_classes_are_pinned():
    assert {
        outcome: klass.value for outcome, klass in outcomes.VOCABULARIES[ATTESTATORES].items()
    } == {
        "read": "completed",
        "genuinely-empty": "completed",
        "failed": "failed",
        "dead": "failed",
        "not-run": "unresolved",
        "excluded": "completed",
    }


def test_no_witness_outcome_terminates_an_act():
    """GOVERNANCE 3 — the Perlector never picks, and no stage selects a winner
    among witnesses. If any witness outcome ever mapped to a terminal category,
    seat results would decide an act's fate: a picker wearing an accounting name.

    CLAUDE.md hard rule 8: "Do not build a picker... the one an agent rebuilds by
    accident." This test is where that accident would be caught.
    """
    terminating = [
        outcome
        for outcome in outcomes.VOCABULARIES[ATTESTATORES]
        if terminal_category(ATTESTATORES, outcome) is not None
    ]
    assert terminating == []


def test_perlector_failures_flow_to_the_recensor_rather_than_terminating():
    """Bounded recovery is the Recensor's, so a Perlector failure must not end the
    act before the Recensor has seen it — that would remove the recovery loop from
    the architecture by accident (ARCHITECTURE, "The Recensor")."""
    for outcome in ("truncated", "failed", "not-run"):
        assert terminal_category(PERLECTOR, outcome) is None


# --- Unknown states are fatal, never routed around ----------------------------


def test_an_outcome_outside_the_vocabulary_is_fatal():
    with pytest.raises(FatalAccounting) as caught:
        classify(RECENSOR, "probably-fine")
    assert "in no terminal set" in str(caught.value)


def test_an_unknown_stage_is_fatal():
    with pytest.raises(FatalAccounting):
        classify("consolidator", "accepted")


def test_fatal_accounting_is_not_catchable_as_a_schema_refusal():
    """A stage may catch a refused unit and record it; nothing may catch an
    accounting imbalance and carry on. Enforced by the class hierarchy, so this
    asserts the hierarchy rather than trusting the docstring."""
    from common.contracts.errors import SchemaRefusal

    assert not issubclass(FatalAccounting, SchemaRefusal)


# --- Approval-bound outcomes ---------------------------------------------------


def test_excluded_without_an_approval_record_is_refused():
    """GOVERNANCE: only Tyrel approves an exclusion. A claimed approval with no
    artifact is no approval."""
    with pytest.raises(ApprovalRefusal):
        require_approval(DESIGNATOR, "excluded", None)
    with pytest.raises(ApprovalRefusal):
        require_approval(DESIGNATOR, "excluded", "")


def test_excluded_with_an_approval_reference_passes():
    require_approval(DESIGNATOR, "excluded", "art_0123456789abcdef")


def test_unbound_outcomes_need_no_approval():
    require_approval(DESIGNATOR, "proposed", None)


def test_the_armarium_category_that_names_approval_also_requires_one():
    """`excluded` and `excluded-with-approval` are the same fact at two stages.
    Matching only the first left the terminal category that *names* approval as
    the one place the check did not reach."""
    with pytest.raises(ApprovalRefusal):
        require_approval(ARMARIUM, ArmariumCategory.EXCLUDED_WITH_APPROVAL.value, None)
    require_approval(
        ARMARIUM, ArmariumCategory.EXCLUDED_WITH_APPROVAL.value, "art_0123456789abcdef"
    )


def test_other_armarium_categories_need_no_approval():
    for category in (ArmariumCategory.DELIVERED, ArmariumCategory.CONFIRMED_BLANK):
        require_approval(ARMARIUM, category.value, None)


# --- Witness coverage ----------------------------------------------------------


def test_coverage_counts_classes_and_flags_below_floor():
    coverage = witness_coverage(
        {"attestator_1": "read", "attestator_2": "failed", "attestator_3": "dead"},
        configured_floor=3,
    )
    assert coverage["by_class"] == {"completed": 1, "unresolved": 0, "failed": 2}
    assert coverage["by_outcome"] == {"read": 1, "failed": 1, "dead": 1}
    assert coverage["under_witnessed"] is True
    assert coverage["unresolved_seats"] == 0


def test_coverage_at_floor_is_not_under_witnessed():
    coverage = witness_coverage(
        {"attestator_1": "read", "attestator_2": "genuinely-empty", "attestator_3": "read"},
        configured_floor=3,
    )
    assert coverage["under_witnessed"] is False
    assert coverage["by_class"]["completed"] == 3


def test_a_genuinely_empty_reading_counts_as_a_reading():
    """Spec 07: the old stage collapsed every absence into one indistinguishable
    empty file. A seat that looked and found nothing has read; a seat that never
    looked has not, and the two may never be the same number."""
    looked = witness_coverage({"attestator_1": "genuinely-empty"}, configured_floor=1)
    never_looked = witness_coverage({"attestator_1": "not-run"}, configured_floor=1)
    assert looked["under_witnessed"] is False
    assert never_looked["under_witnessed"] is True


def test_an_unnamed_chair_is_fatal():
    with pytest.raises(FatalAccounting):
        witness_coverage({"": "read"}, configured_floor=1)


def test_an_unknown_chair_outcome_is_fatal():
    with pytest.raises(FatalAccounting):
        witness_coverage({"attestator_1": "probably-read"}, configured_floor=1)


# --- The run aggregate ---------------------------------------------------------


def test_a_fully_delivered_well_witnessed_run_is_complete():
    aggregate = run_aggregate(
        {"act_a": ArmariumCategory.DELIVERED, "act_b": ArmariumCategory.CONFIRMED_BLANK},
        {"act_a": witness_coverage({"s1": "read", "s2": "read"}, 2)},
    )
    assert aggregate["status"] == "complete"
    assert aggregate["reasons"] == []
    assert aggregate["by_category"] == {"delivered": 1, "confirmed-blank": 1}


def test_a_run_that_accounted_for_nothing_is_not_complete():
    """GOVERNANCE 2 — "complete" is refused unless everything reconciles. Over an
    empty population every loop runs zero times and `reasons` stays empty, so the
    aggregate used to fall through to a green verdict asserting that nothing had
    gone wrong with nothing. An empty population reconciles vacuously, not actually."""
    aggregate = run_aggregate({})
    assert aggregate["status"] == "partial"
    assert aggregate["reasons"] == [
        "the run accounted for no acts and no pages, so nothing was reconciled"
    ]


def test_a_sealed_page_carrying_no_acts_is_still_complete():
    """The other direction, and the reason the check above is not simply
    "no acts": a blank page is a real and correct outcome, and a run over one
    must not be forced partial for having found nothing on it."""
    aggregate = run_aggregate({}, None, {1: {"outcome": "sealed"}})
    assert aggregate["status"] == "complete"
    assert aggregate["reasons"] == []


def test_a_held_act_forces_partial_and_names_itself():
    """GOVERNANCE 2 — a partial result is visibly partial; "complete" is refused
    unless everything reconciles."""
    aggregate = run_aggregate(
        {"act_a": ArmariumCategory.DELIVERED, "act_b": ArmariumCategory.HELD_FOR_REVIEW}
    )
    assert aggregate["status"] == "partial"
    assert aggregate["reasons"] == ["act act_b is held-for-review"]


def test_under_witnessed_coverage_forces_partial_even_when_every_act_delivered():
    """The strict reading of GOVERNANCE 2, and queued for Tyrel in spec 01: an act
    delivered on two live seats against a floor of three stays `delivered`, and
    the run says partial with the shortfall named. The act's own category is
    untouched — witness coverage never demotes text."""
    aggregate = run_aggregate(
        {"act_a": ArmariumCategory.DELIVERED},
        {"act_a": witness_coverage({"s1": "read", "s2": "dead", "s3": "dead"}, 3)},
    )
    assert aggregate["status"] == "partial"
    assert aggregate["by_category"] == {"delivered": 1}
    assert aggregate["reasons"] == ["act act_a is under-witnessed (1 of a floor of 3)"]


def test_a_chair_with_no_outcome_yet_forces_partial():
    aggregate = run_aggregate(
        {"act_a": ArmariumCategory.DELIVERED},
        {"act_a": witness_coverage({"s1": "read", "s2": "read", "s3": "not-run"}, 3)},
    )
    assert aggregate["status"] == "partial"
    assert "1 seat(s) with no outcome yet" in aggregate["reasons"][-1]


def test_a_category_that_is_not_a_category_is_fatal():
    with pytest.raises(FatalAccounting):
        run_aggregate({"act_a": "delivered"})


# --- The page census: page-level units reconcile at the last boundary too -------


def test_a_fully_sealed_census_leaves_a_complete_run_complete():
    aggregate = run_aggregate(
        {"act_a": ArmariumCategory.DELIVERED},
        None,
        {1: {"outcome": "sealed"}, 2: {"outcome": "sealed"}},
    )
    assert aggregate["status"] == "complete"
    assert aggregate["reasons"] == []
    assert aggregate["by_page_outcome"] == {"sealed": 2}


def test_a_refused_page_forces_partial_and_names_the_loss():
    """GOVERNANCE 2, at page granularity. Before this, the seal was the only
    conservation authority and it never mentioned pages: a run that lost a whole
    page at the door could still report `status: complete, reasons: []`."""
    aggregate = run_aggregate(
        {"act_a": ArmariumCategory.DELIVERED},
        None,
        {
            1: {"outcome": "sealed"},
            2: {"outcome": "refused", "reason": "digest mismatch at the door"},
        },
    )
    assert aggregate["status"] == "partial"
    assert aggregate["reasons"] == ["page 2 was refused: digest mismatch at the door"]
    assert aggregate["by_page_outcome"] == {"sealed": 1, "refused": 1}


def test_a_refused_page_with_no_recorded_reason_still_forces_partial():
    """Absent evidence never reads cleaner than damaged evidence."""
    aggregate = run_aggregate(
        {"act_a": ArmariumCategory.DELIVERED}, None, {2: {"outcome": "refused"}}
    )
    assert aggregate["status"] == "partial"
    assert aggregate["reasons"] == ["page 2 was refused: no reason was recorded"]


def test_a_page_outcome_outside_the_exemplar_vocabulary_is_fatal():
    """Unknown is never zero: a page in neither the sealed nor the refused set is
    invariant #10's imbalance, not a page to route around."""
    with pytest.raises(FatalAccounting):
        run_aggregate({"act_a": ArmariumCategory.DELIVERED}, None, {1: {"outcome": "lost"}})
    with pytest.raises(FatalAccounting):
        run_aggregate({"act_a": ArmariumCategory.DELIVERED}, None, {1: {}})


def test_armarium_categories_and_vocabulary_cannot_drift_apart():
    """Meta-invariant #91 — drift checks over agreement surfaces: wherever two
    files must agree, a test reads both from source and fails on divergence."""
    assert set(outcomes.VOCABULARIES[ARMARIUM]) == {category.value for category in ArmariumCategory}
