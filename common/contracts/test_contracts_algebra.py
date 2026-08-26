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
from common.contracts.errors import ApprovalRefusal, FatalAccounting, SchemaRefusal
from common.contracts.outcomes import (
    NO_ATTRIBUTION_REASON,
    SILENT_PAGE_REASON,
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
from common.recensor_receipt import _validate_coverage

# The exact shape of the algebra as this spec defines it. Pinned as counts so that
# adding a state without deciding its class and its terminal category fails here,
# loudly, naming what changed — rather than passing because the new state happened
# to be consistent with itself.
EXPECTED_VOCABULARY_SIZES = {
    "door": 4,
    "exemplar": 3,
    "designator": 6,
    "attestatores": 8,
    "perlector": 7,
    "recensor": 7,
    "archetypus": 4,
    "armarium": 7,
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


def test_witness_vocabulary_includes_the_two_boundary_record_outcomes():
    assert set(outcomes.VOCABULARIES[ATTESTATORES]) == {
        "read",
        "genuinely-empty",
        "failed",
        "dead",
        "not-run",
        "excluded",
        "sealed",
        "recorded",
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
        "sealed": "completed",
        "recorded": "completed",
    }


def test_armarium_boundary_evidence_is_nonterminal_not_delivered_output():
    for outcome in ("sealed", "recorded"):
        assert classify(ARMARIUM, outcome) is OutcomeClass.COMPLETED
        assert terminal_category(ARMARIUM, outcome) is None


def test_no_witness_outcome_terminates_an_act():
    """GOVERNANCE 3 — the Perlector never picks, and no stage selects a winner
    among witnesses. If any witness outcome ever mapped to a terminal category,
    chair results would decide an act's fate: a picker wearing an accounting name.

    Scope, stated so this is not read as more than it is: this catches one specific
    shape of accidental picker — an *outcome* that terminates an act. A stage that
    selected among witnesses without touching this mapping would pass here untouched,
    so this is not a general guarantee against hard rule 8. `pipeline/5_recensor`'s
    quality-firewall tests constrain the recovery gate's inputs, which is the other half.
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


def test_no_readable_text_is_unresolved_until_a_blank_proof_exists():
    """Silence is not blank proof (ruling 15), so it cannot reach an Archetypus.

    The Recensor's existing unresolved branch holds this status. A future
    proof-bearing `confirmed-blank` may complete, but a Perlector saying it found
    no characters is not that proof.
    """
    assert classify(PERLECTOR, "no-readable-text") is OutcomeClass.UNRESOLVED
    assert terminal_category(PERLECTOR, "no-readable-text") is None


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
    assert coverage["unresolved_chairs"] == 0


def test_coverage_at_floor_is_not_under_witnessed():
    coverage = witness_coverage(
        {"attestator_1": "read", "attestator_2": "genuinely-empty", "attestator_3": "read"},
        configured_floor=3,
    )
    assert coverage["under_witnessed"] is False
    assert coverage["by_class"]["completed"] == 3


def test_a_genuinely_empty_reading_counts_as_a_reading():
    """Spec 07: the old stage collapsed every absence into one indistinguishable
    empty file. A chair that looked and found nothing has read; a chair that never
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
        {
            "act_a": witness_coverage({"s1": "read", "s2": "read"}, 2),
            "act_b": witness_coverage({"s1": "genuinely-empty", "s2": "read"}, 2),
        },
        {1: {"outcome": "sealed"}},
        act_pages={"act_a": [1], "act_b": [1]},
        act_text_status={"act_a": "established"},
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


def test_a_sealed_page_carrying_no_acts_is_unresolved_not_inferred_blank():
    """A zero-act signal cannot distinguish a blank page from a Designator miss.

    A future Recensor may diagnose and seal `confirmed-blank` with evidence. Until
    then, found-nothing is silence rather than proof and cannot complete a run.
    """
    aggregate = run_aggregate({}, None, {1: {"outcome": "sealed"}})
    assert aggregate["status"] == "partial"
    assert aggregate["reasons"] == [SILENT_PAGE_REASON.format(ordinal=1)]


def test_one_silent_page_beside_a_busy_one_still_forces_partial():
    """Ruling 15 at page granularity, which is the granularity it was given at.

    The run-level check asked only whether the run produced *any* acts, so a single
    silent page among busy ones had its proof obligation discharged by its
    neighbours. At ten thousand pages that is the only shape the defect can take:
    nobody ships a run where every page is blank, and the one page whose faint ink
    the Designator missed is exactly the page that reconciled to `complete`.
    """
    aggregate = run_aggregate(
        {"act_a": ArmariumCategory.DELIVERED},
        {"act_a": witness_coverage({"s1": "read"}, 1)},
        {1: {"outcome": "sealed"}, 2: {"outcome": "sealed"}},
        act_pages={"act_a": [1]},
        act_text_status={"act_a": "established"},
    )
    assert aggregate["status"] == "partial"
    assert aggregate["reasons"] == [SILENT_PAGE_REASON.format(ordinal=2)]
    assert aggregate["by_page_outcome"] == {"sealed": 2}


def test_a_run_that_names_no_page_for_its_acts_cannot_check_silence_and_says_so():
    """Self-enforcement: a caller that supplies no attribution is told, not believed.

    Defaulting to "every page produced something" would have made the check above
    optional, and an optional conservation check is one a later caller drops.
    """
    aggregate = run_aggregate(
        {"act_a": ArmariumCategory.DELIVERED},
        {"act_a": witness_coverage({"s1": "read"}, 1)},
        {1: {"outcome": "sealed"}},
        act_text_status={"act_a": "established"},
    )
    assert aggregate["status"] == "partial"
    assert aggregate["reasons"] == [NO_ATTRIBUTION_REASON]


def test_an_act_with_no_page_of_its_own_is_named_rather_than_skipped():
    aggregate = run_aggregate(
        {"act_a": ArmariumCategory.DELIVERED, "act_b": ArmariumCategory.DELIVERED},
        {
            "act_a": witness_coverage({"s1": "read"}, 1),
            "act_b": witness_coverage({"s1": "read"}, 1),
        },
        {1: {"outcome": "sealed"}},
        act_pages={"act_a": [1]},
        act_text_status={"act_a": "established", "act_b": "established"},
    )
    assert aggregate["status"] == "partial"
    assert aggregate["reasons"] == [
        "act act_b names no page, so the page it came from cannot be checked for coverage"
    ]


def test_attribution_naming_an_act_or_a_page_the_run_never_had_is_fatal():
    with pytest.raises(FatalAccounting, match="attribution names unknown act"):
        run_aggregate(
            {"act_a": ArmariumCategory.DELIVERED},
            {"act_a": witness_coverage({"s1": "read"}, 1)},
            {1: {"outcome": "sealed"}},
            act_pages={"act_a": [1], "act_ghost": [1]},
        )
    with pytest.raises(FatalAccounting, match="page census does not account for"):
        run_aggregate(
            {"act_a": ArmariumCategory.DELIVERED},
            {"act_a": witness_coverage({"s1": "read"}, 1)},
            {1: {"outcome": "sealed"}},
            act_pages={"act_a": [7]},
        )


def test_an_act_marked_out_on_a_page_the_exemplar_never_sealed_is_fatal():
    """An act cannot exist over pixels that were refused; that is not a partial run,
    it is a record contradicting itself."""
    with pytest.raises(FatalAccounting, match="which the Exemplar did not"):
        run_aggregate(
            {"act_a": ArmariumCategory.DELIVERED},
            {"act_a": witness_coverage({"s1": "read"}, 1)},
            {1: {"outcome": "refused", "reason": "unreadable"}},
            act_pages={"act_a": [1]},
        )


def test_a_held_act_forces_partial_and_names_itself():
    """GOVERNANCE 2 — a partial result is visibly partial; "complete" is refused
    unless everything reconciles."""
    aggregate = run_aggregate(
        {"act_a": ArmariumCategory.DELIVERED, "act_b": ArmariumCategory.HELD_FOR_REVIEW},
        {
            "act_a": witness_coverage({"s1": "read"}, 1),
            "act_b": witness_coverage({"s1": "read"}, 1),
        },
        {1: {"outcome": "sealed"}},
        act_pages={"act_a": [1], "act_b": [1]},
        act_text_status={"act_a": "established"},
    )
    assert aggregate["status"] == "partial"
    assert aggregate["reasons"] == ["act act_b is held-for-review"]


def test_aggregate_reason_order_does_not_depend_on_mapping_insertion_order():
    """Serialized accounting maps may return in a different order than their producer."""
    categories = {
        "act_b": ArmariumCategory.HELD_FOR_REVIEW,
        "act_a": ArmariumCategory.REFUSED_WITH_REASON,
    }
    coverage = {
        "act_b": witness_coverage({"s1": "not-run"}, 1),
        "act_a": witness_coverage({"s1": "dead"}, 1),
    }
    pages = {1: {"outcome": "sealed"}}
    act_pages = {"act_b": [1], "act_a": [1]}

    forward = run_aggregate(categories, coverage, pages, act_pages=act_pages)
    reversed_maps = run_aggregate(
        dict(reversed(categories.items())),
        dict(reversed(coverage.items())),
        pages,
        act_pages=dict(reversed(act_pages.items())),
    )

    assert reversed_maps == forward


def test_under_witnessed_coverage_forces_partial_even_when_every_act_delivered():
    """The strict reading of GOVERNANCE 2, and queued for Tyrel in spec 01: an act
    delivered on two live chairs against a floor of three stays `delivered`, and
    the run says partial with the shortfall named. The act's own category is
    untouched — witness coverage never demotes text."""
    aggregate = run_aggregate(
        {"act_a": ArmariumCategory.DELIVERED},
        {"act_a": witness_coverage({"s1": "read", "s2": "dead", "s3": "dead"}, 3)},
        {1: {"outcome": "sealed"}},
        act_pages={"act_a": [1]},
        act_text_status={"act_a": "established"},
    )
    assert aggregate["status"] == "partial"
    assert aggregate["by_category"] == {"delivered": 1}
    assert aggregate["reasons"] == ["act act_a is under-witnessed (1 of a floor of 3)"]


def test_a_chair_with_no_outcome_yet_forces_partial():
    aggregate = run_aggregate(
        {"act_a": ArmariumCategory.DELIVERED},
        {"act_a": witness_coverage({"s1": "read", "s2": "read", "s3": "not-run"}, 3)},
        {1: {"outcome": "sealed"}},
        act_pages={"act_a": [1]},
    )
    assert aggregate["status"] == "partial"
    assert "1 chair(s) with no outcome yet" in aggregate["reasons"][-1]


def test_a_category_that_is_not_a_category_is_fatal():
    with pytest.raises(FatalAccounting):
        run_aggregate({"act_a": "delivered"})


# --- The page census: page-level units reconcile at the last boundary too -------


def test_a_fully_sealed_census_leaves_a_complete_run_complete():
    """Every sealed page produced an act, so nothing is silent and nothing is
    inferred. This case previously carried only one act across two sealed pages and
    still read `complete`, which is the blank-by-silence hole one page wide."""
    aggregate = run_aggregate(
        {"act_a": ArmariumCategory.DELIVERED, "act_b": ArmariumCategory.DELIVERED},
        {
            "act_a": witness_coverage({"s1": "read"}, 1),
            "act_b": witness_coverage({"s1": "read"}, 1),
        },
        {1: {"outcome": "sealed"}, 2: {"outcome": "sealed"}},
        act_pages={"act_a": [1], "act_b": [2]},
        act_text_status={"act_a": "established", "act_b": "established"},
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
        {"act_a": witness_coverage({"s1": "read"}, 1)},
        {
            1: {"outcome": "sealed"},
            2: {"outcome": "refused", "reason": "digest mismatch at the door"},
        },
        act_pages={"act_a": [1]},
        act_text_status={"act_a": "established"},
    )
    assert aggregate["status"] == "partial"
    assert aggregate["reasons"] == ["page 2 was refused: digest mismatch at the door"]
    assert aggregate["by_page_outcome"] == {"sealed": 1, "refused": 1}


def test_a_refused_page_with_no_recorded_reason_still_forces_partial():
    """Absent evidence never reads cleaner than damaged evidence."""
    aggregate = run_aggregate(
        {"act_a": ArmariumCategory.DELIVERED},
        {"act_a": witness_coverage({"s1": "read"}, 1)},
        {1: {"outcome": "sealed"}, 2: {"outcome": "refused"}},
        act_pages={"act_a": [1]},
        act_text_status={"act_a": "established"},
    )
    assert aggregate["status"] == "partial"
    assert aggregate["reasons"] == ["page 2 was refused: no reason was recorded"]


def test_a_page_outcome_outside_the_exemplar_vocabulary_is_fatal():
    """Unknown is never zero: a page in neither the sealed nor the refused set is
    invariant #10's imbalance, not a page to route around."""
    with pytest.raises(FatalAccounting):
        run_aggregate(
            {"act_a": ArmariumCategory.DELIVERED},
            {"act_a": witness_coverage({"s1": "read"}, 1)},
            {1: {"outcome": "lost"}},
        )
    with pytest.raises(FatalAccounting):
        run_aggregate(
            {"act_a": ArmariumCategory.DELIVERED},
            {"act_a": witness_coverage({"s1": "read"}, 1)},
            {1: {}},
        )


def test_missing_coverage_or_page_census_cannot_look_complete():
    missing_coverage = run_aggregate(
        {"act_a": ArmariumCategory.DELIVERED},
        {},
        {1: {"outcome": "sealed"}},
        act_pages={"act_a": [1]},
        act_text_status={"act_a": "established"},
    )
    assert missing_coverage["status"] == "partial"
    assert missing_coverage["reasons"] == ["act act_a has no witness-coverage record"]

    missing_census = run_aggregate(
        {"act_a": ArmariumCategory.DELIVERED},
        {"act_a": witness_coverage({"s1": "read"}, 1)},
        act_text_status={"act_a": "established"},
    )
    assert missing_census["status"] == "partial"
    assert missing_census["reasons"] == [
        "the run has acts but no page census, so page conservation was not checked"
    ]


def test_coverage_for_an_unknown_act_is_fatal():
    with pytest.raises(FatalAccounting, match="coverage names unknown act"):
        run_aggregate(
            {"act_a": ArmariumCategory.DELIVERED},
            {"act_b": witness_coverage({"s1": "read"}, 1)},
            {1: {"outcome": "sealed"}},
        )


def test_armarium_categories_and_vocabulary_cannot_drift_apart():
    """Meta-invariant #91 — drift checks over agreement surfaces: wherever two
    files must agree, a test reads both from source and fails on divergence."""
    assert set(outcomes.VOCABULARIES[ARMARIUM]) - set(outcomes.BOUNDARY_OUTCOMES.values()) == {
        category.value for category in ArmariumCategory
    }


def test_the_under_witnessed_count_is_the_attached_reads_never_the_wider_class():
    """F-G2, pinned. `under_witnessed` is decided from the attached-reading
    count; `by_class["completed"]` is the wider ATTESTATORES COMPLETED class,
    which also holds `excluded` and -- since R4's per-act alignment -- a page
    witness that read its page and did not align into this act. Printing the
    wider number put a floor-satisfying count next to an under-witnessed
    verdict: "act act_a is under-witnessed (3 of a floor of 3)", a sentence
    that refutes itself, which is exactly the shape GOVERNANCE 2 and 10 refuse.

    Written on this audit: the repair landed with no named test holding it, and
    the fixture cannot produce the divergence today.
    """
    coverage = witness_coverage(
        {"s1": "read", "s2": "read", "s3": "read"},
        3,
        attachments={"s1": True, "s2": True, "s3": False},
    )
    assert coverage["under_witnessed"] is True
    assert coverage["by_class"]["completed"] == 3, "the wider class still counts all three"
    assert coverage["page_granularity_only"] == 1

    aggregate = run_aggregate(
        {"act_a": ArmariumCategory.DELIVERED},
        {"act_a": coverage},
        {1: {"outcome": "sealed"}},
        act_pages={"act_a": [1]},
        act_text_status={"act_a": "established"},
    )
    assert aggregate["reasons"] == ["act act_a is under-witnessed (2 of a floor of 3)"]


def test_the_legacy_under_witnessed_message_prints_the_count_that_raised_the_flag():
    """On the legacy path (`attachments=None`) `under_witnessed` is decided from
    the COMPLETED class, which also holds `excluded` -- and the record still
    carries `page_granularity_only`, so branching on that key's presence
    rederived the message count from reading outcomes instead: {read, excluded,
    dead} against a floor of 3 flagged at 2 and reported 1, a number no rule in
    `witness_coverage` produced. The branch is keyed on the recorded
    `granularity_basis`, so the message quotes the same arithmetic that decided
    the flag.
    """
    coverage = witness_coverage(
        {"s1": "read", "s2": "excluded", "s3": "dead"},
        3,
    )
    assert coverage["granularity_basis"] == outcomes.LEGACY_GRANULARITY_BASIS
    assert coverage["under_witnessed"] is True, "decided from the class count of 2"
    assert coverage["by_class"]["completed"] == 2
    assert coverage["page_granularity_only"] == 0, "the legacy record still carries the key"

    aggregate = run_aggregate(
        {"act_a": ArmariumCategory.DELIVERED},
        {"act_a": coverage},
        {1: {"outcome": "sealed"}},
        act_pages={"act_a": [1]},
        act_text_status={"act_a": "established"},
    )
    assert aggregate["reasons"] == ["act act_a is under-witnessed (2 of a floor of 3)"]


def test_an_unknown_granularity_basis_is_refused_never_guessed_from():
    """The closed vocabulary, closed at the consumer: a basis this module never
    produced is malformed evidence. A default here would guess the message
    count from the wrong arithmetic -- the exact defect the basis branch
    exists to repair."""
    coverage = witness_coverage(
        {"s1": "read", "s2": "read", "s3": "read"},
        3,
        attachments={"s1": True, "s2": True, "s3": False},
    )
    forged = {**coverage, "granularity_basis": "a-basis-nothing-produces"}
    with pytest.raises(FatalAccounting, match="unknown granularity basis"):
        run_aggregate(
            {"act_a": ArmariumCategory.DELIVERED},
            {"act_a": forged},
            {1: {"outcome": "sealed"}},
            act_pages={"act_a": [1]},
        )


def test_granularity_identity_is_executable_for_interim_and_native_bases():
    """The receipt's reading chairs minus page-only count equals the writer's attachments."""
    coverage = witness_coverage(
        {"s1": "read", "s2": "read", "s3": "genuinely-empty"},
        3,
        attachments={"s1": True, "s2": False, "s3": True},
    )
    assert coverage["granularity_basis"] == outcomes.NATIVE_GRANULARITY_BASIS
    assert (
        sum(coverage["by_outcome"].get(outcome, 0) for outcome in outcomes.WITNESS_READING_OUTCOMES)
        - coverage["page_granularity_only"]
        == 2
    )
    for basis in (outcomes.INTERIM_GRANULARITY_BASIS, outcomes.NATIVE_GRANULARITY_BASIS):
        candidate = {**coverage, "granularity_basis": basis}
        _validate_coverage(candidate, require_complete_granularity=True)


# --- The established text's own status: damage the category cannot express ------
#
# Opus-F1 / Sol-S4 (T0 export honesty). `delivered` says where an act ended and nothing about
# whether the reading that left is whole, so an act whose own Perlectio recorded a
# gap -- ink the reader knows is present and could not read -- aggregated to
# `complete` with an empty reason list. GOVERNANCE 2: a partial result is visibly
# partial, and Tyrel expects damage to be ordinary ("many of our records are
# damaged"), so this is the common case rather than the edge one.


def _delivered_with(status=None, **kwargs):
    """One delivered act over one sealed page, varying only its text status."""
    return run_aggregate(
        {"act_a": ArmariumCategory.DELIVERED},
        {"act_a": witness_coverage({"s1": "read"}, 1)},
        {1: {"outcome": "sealed"}},
        act_pages={"act_a": [1]},
        act_text_status=({"act_a": status} if status is not None else None),
        **kwargs,
    )


def test_a_delivered_act_with_partial_text_forces_partial_and_names_itself():
    aggregate = _delivered_with("partial")
    assert aggregate["status"] == "partial"
    # The act's own category is untouched: a damaged reading is still delivered,
    # exactly as an under-witnessed act stays delivered. The run says so beside it.
    assert aggregate["by_category"] == {"delivered": 1}
    assert aggregate["reasons"] == [
        "act act_a was delivered with partial text: its record carries ink the Perlector "
        "knows is present and could not read"
    ]


def test_a_delivered_act_whose_text_is_established_leaves_the_run_complete():
    aggregate = _delivered_with("established")
    assert aggregate["status"] == "complete"
    assert aggregate["reasons"] == []


def test_a_delivered_act_that_established_no_readable_text_is_named():
    aggregate = _delivered_with("no_readable_text")
    assert aggregate["status"] == "partial"
    assert aggregate["reasons"] == [
        "act act_a was delivered with a record that establishes no readable text; a proved "
        "blank is confirmed-blank business, and a delivered act with no text is not reconciled"
    ]


def test_a_delivered_act_with_no_text_status_is_named_rather_than_assumed_whole():
    """GOVERNANCE 10 — a metric that cannot be measured is a failure, not a pass.

    The same self-enforcement as the missing page attribution beside it: a caller
    that supplies nothing is told so, because a default of "every delivered act
    was whole" is how this check would quietly become optional again.
    """
    aggregate = _delivered_with(None)
    assert aggregate["status"] == "partial"
    # The literal sentence, exactly as the two tests above it pin theirs. Comparing
    # `NO_TEXT_STATUS_REASON` to itself would pass for any wording the constant
    # ever held, including one that stopped saying the measurement never happened.
    assert aggregate["reasons"] == [
        "act act_a was delivered with no established-text status record, so whether its "
        "one reading is whole was never measured"
    ]


def test_a_text_status_on_an_act_that_was_not_delivered_is_fatal():
    """No Archetypus record exists for a held act, so a status about its text is a
    claim about a reading that is not there -- an imbalance, not a partial run."""
    with pytest.raises(FatalAccounting, match="only a delivered act has an Archetypus record"):
        run_aggregate(
            {"act_a": ArmariumCategory.HELD_FOR_REVIEW},
            {"act_a": witness_coverage({"s1": "read"}, 1)},
            {1: {"outcome": "sealed"}},
            act_pages={"act_a": [1]},
            act_text_status={"act_a": "established"},
        )


def test_a_text_status_naming_an_act_the_run_never_had_is_fatal():
    with pytest.raises(FatalAccounting, match="text status names unknown act"):
        run_aggregate(
            {"act_a": ArmariumCategory.DELIVERED},
            {"act_a": witness_coverage({"s1": "read"}, 1)},
            {1: {"outcome": "sealed"}},
            act_pages={"act_a": [1]},
            act_text_status={"act_a": "established", "act_ghost": "partial"},
        )


def test_a_non_string_text_status_is_fatal_rather_than_a_type_error():
    """An unhashable value out of a hand-built basis must meet the named fatal,
    not a TypeError from the frozenset membership test."""
    with pytest.raises(FatalAccounting, match="which is not one of"):
        _delivered_with(["partial"])


def test_a_text_status_outside_the_closed_vocabulary_is_fatal():
    """Unknown is never `established`: a word this module never produced is
    malformed evidence, and guessing it whole is the failure mode being repaired."""
    with pytest.raises(FatalAccounting, match="which is not one of"):
        _delivered_with("mostly-fine")


# --- The derivation both stages share ------------------------------------------


def _layer(gaps=()):
    return {"uncertain_spans": [], "gaps": list(gaps), "self_revisions": []}


def _illegible(position=0):
    return {"kind": "illegible", "start": position, "end": position, "witness_evidence": []}


def test_a_clean_reading_over_both_layers_is_established():
    assert outcomes.derive_record_text_status("some real ink", [], _layer()) == "established"


def test_a_canonical_gap_makes_an_otherwise_readable_record_partial():
    gap = {"position": "internal", "start": 4, "end": 4, "witness_evidence": []}
    assert outcomes.derive_record_text_status("some real ink", [], _layer([gap])) == "partial"


def test_an_older_illegible_annotation_still_makes_a_record_partial():
    """Both layers travel, and either one recording unread ink is enough. Neither
    can hide damage the other saw, which is what makes carrying both honest."""
    assert (
        outcomes.derive_record_text_status("some real ink", [_illegible(3)], _layer()) == "partial"
    )


def test_a_canonical_gap_over_empty_text_is_partial_and_never_a_proved_blank():
    """ "We could not read it" must never quietly become "there was nothing to read".

    A whole-act gap is the middle silence -- ink present, wholly unread. Asking
    about the canonical gaps only where the rest already said `established`
    returned `no_readable_text` for exactly this record, which is a proved blank
    sealed beside a gap saying the opposite. The older annotation layer has always
    been ordered this way (`pipeline/6_archetypus/test_text_status.py`).
    """
    gap = {"position": "whole-act", "start": 0, "end": 0, "witness_evidence": []}
    assert outcomes.derive_record_text_status("", [], _layer([gap])) == "partial"


def test_an_empty_reading_with_no_damage_recorded_anywhere_is_no_readable_text():
    assert outcomes.derive_record_text_status("", [], _layer()) == "no_readable_text"


def test_a_malformed_record_that_also_carries_a_gap_is_refused_rather_than_partial():
    """The one branch that could reach a status without ever looking at the text.

    A canonical gap decides `partial` on its own, so this derivation could return
    it over a text field that is not a string and an annotation layer nothing can
    read -- and the record would leave the pipeline described by a word no one
    had checked it against. The otherwise-unused `derive_text_status` call inside
    the gap branch is the whole of what prevents that: remove it and both
    assertions below return `"partial"` instead of refusing.
    """
    gap = {"position": "internal", "start": 4, "end": 4, "witness_evidence": []}
    with pytest.raises(SchemaRefusal, match="exactly one string text"):
        outcomes.derive_record_text_status(None, [], _layer([gap]))
    with pytest.raises(SchemaRefusal, match="carries no kind"):
        outcomes.derive_record_text_status("ink", [{"start": 0, "end": 0}], _layer([gap]))


def test_a_damage_layer_that_cannot_be_read_is_refused_rather_than_called_whole():
    """A malformed layer is not a zero. Refusing here is what lets the Armarium
    turn it into a named fatal refusal instead of exporting `established`."""
    with pytest.raises(SchemaRefusal, match="carries no kind"):
        outcomes.derive_record_text_status("ink", [{"start": 0, "end": 0}], _layer())
    with pytest.raises(SchemaRefusal, match="canonical uncertainty layer's own gap list"):
        outcomes.derive_record_text_status("ink", [], {"uncertain_spans": []})
    with pytest.raises(SchemaRefusal, match="exactly one string text"):
        outcomes.derive_record_text_status(None, [], _layer())
