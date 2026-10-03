"""Operator review decisions: scope, binding, staleness and deterministic application."""

from __future__ import annotations

import copy

import pytest

from common.contracts.approval import build_review_decision_record
from common.contracts.canonical import canonical_bytes, digest_of
from common.contracts.errors import ApprovalRefusal, FatalAccounting
from common.contracts.outcomes import (
    ArmariumCategory,
    OutcomeClass,
    classify,
    run_aggregate,
    terminal_category,
)
from common.contracts.stages import RECENSOR
from common.page_review import PAGE_REVIEW_FIELDS
from common.review_decisions import (
    BASIS_CHANGED,
    CURRENT,
    PAGE_WIDE_ENTRY_CODES,
    RECENSOR_PAGE_CODES,
    RECENSOR_UNIT_CODES,
    REVIEW_FIELD,
    STALE,
    SUBJECT_ABSENT,
    aggregate_clearances,
    apply_decisions,
    basis_digest,
    classify_holds,
    current_basis,
    held_pages,
    page_reask_decisions,
    published_basis,
    review_decision,
)
from common.stage import NO_ACT_ON_PAGE_HOLD, PAGE_BLANK_HOLD, PAGE_UNREAD_HOLD
from conftest import load_stage

RUN = "run-1"


def payload(act_key, ordinal, hold_codes, *, unit_class="reading", kind="act"):
    """A page-review payload in the Recensor's closed shape; only a few fields matter here."""
    fields = {name: None for name in PAGE_REVIEW_FIELDS}
    fields.update(
        act_key=act_key,
        unit_class=unit_class,
        kind=kind,
        page_ordinal=ordinal,
        reason="held: " + ", ".join(hold_codes) if hold_codes else "read and witnessed",
        hold_codes=sorted(hold_codes),
        coverage={"floor": 2, "under_witnessed": False},
        notes=[],
        recoveries_used=0,
    )
    return fields


def unit(act_id, page_id, ordinal, hold_codes, *, unit_holds=(), page_holds=(), **kind):
    body = payload(f"p{ordinal}:{act_id}", ordinal, hold_codes, **kind)
    blank = body["unit_class"] == "page-blank"
    return {
        "act_id": act_id,
        "page_id": page_id,
        "outcome": "held-for-review" if hold_codes else "confirmed-blank" if blank else "accepted",
        "payload": body,
        "unit_holds": list(unit_holds),
        "page_holds": list(page_holds),
    }


def derived_review():
    """Page 1: two entries, one with its own hold, both under the page's unread line.
    Page 2: one accepted entry. Page 3: a page never read."""
    return {
        "run_id": RUN,
        "units": [
            unit(
                "a1",
                "page-1",
                1,
                ["doubt-marks-malformed", "unread-line"],
                unit_holds=["doubt-marks-malformed"],
                page_holds=["unread-line"],
            ),
            unit("a2", "page-1", 1, ["unread-line"], page_holds=["unread-line"]),
            unit("b1", "page-2", 2, []),
            unit(
                "u3",
                "page-3",
                3,
                [PAGE_UNREAD_HOLD, "page-answer-incomplete"],
                unit_class="page-unread",
            ),
        ],
    }


def decide(derived, scope, subject, name, *, finding=None, page_id=None, basis=None, **extra):
    basis = basis or current_basis(derived)
    if scope == "unit":
        found = basis["units"][subject]
        page_id, digest = page_id or found["page_id"], found["basis_digest"]
    else:
        page_id, digest = subject, basis["pages"][subject]["basis_digest"]
    fields = {
        "run_id": RUN,
        "scope": scope,
        "subject_id": subject,
        "page_id": page_id,
        "decision": name,
        "finding": finding,
        "basis_digest": digest,
        "reason": f"{name} after looking at the page",
        "timestamp": "2026-09-30T12:00:00Z",
    }
    fields.update(extra)
    return build_review_decision_record(**fields)


# --- scope -----------------------------------------------------------------------------


def test_an_entrys_own_holds_are_unit_scope_and_its_pages_are_page_scope():
    scopes = classify_holds(
        ["reading-incomplete", "unread-line", "residual-ink", "continuation-off-page-edge"],
        unit_class="reading",
        unit_holds=["reading-incomplete"],
        page_holds=["unread-line"],
    )
    assert scopes == {
        "unit": ["continuation-off-page-edge", "reading-incomplete"],
        "page": ["residual-ink", "unread-line"],
    }


def test_a_code_both_the_entry_and_its_page_hold_needs_both_decisions():
    scopes = classify_holds(
        ["duplicate-region"],
        unit_class="reading",
        unit_holds=["duplicate-region"],
        page_holds=["duplicate-region"],
    )
    assert scopes == {"unit": ["duplicate-region"], "page": ["duplicate-region"]}


def test_every_hold_of_a_page_level_row_is_page_scope():
    scopes = classify_holds(
        [PAGE_BLANK_HOLD, "unread-ink"], unit_class="page-blank", unit_holds=[], page_holds=[]
    )
    assert scopes == {"unit": [], "page": ["page-blank-unconfirmed", "unread-ink"]}


def test_the_no_act_hold_is_page_scope():
    scopes = classify_holds(
        [NO_ACT_ON_PAGE_HOLD], unit_class="reading", unit_holds=[], page_holds=[]
    )
    assert scopes == {"unit": [], "page": [NO_ACT_ON_PAGE_HOLD]}


def test_a_hold_of_unknown_scope_is_refused():
    with pytest.raises(FatalAccounting, match="neither the entry's own nor its page's"):
        classify_holds(["mystery"], unit_class="reading", unit_holds=[], page_holds=[])


def test_every_code_the_recensor_adds_has_a_scope():
    page_review = load_stage("5_recensor", "page_review")
    added = {
        page_review.UNDER_WITNESSED,
        page_review.UNRESOLVED_WITNESS,
        page_review.RESIDUAL_INK,
        page_review.RESIDUAL_INK_NOT_MEASURABLE,
        page_review.RESIDUAL_INK_NOT_MEASURED,
        page_review.ASSESSMENT_MALFORMED,
        page_review.CONTINUATION_OFF_EDGE,
    }
    assert RECENSOR_UNIT_CODES | RECENSOR_PAGE_CODES == added
    assert not RECENSOR_UNIT_CODES & RECENSOR_PAGE_CODES


# --- the basis --------------------------------------------------------------------------


def test_the_basis_is_the_machine_payload_and_refuses_an_applied_or_stamped_one():
    body = payload("p1:1", 1, [])
    assert basis_digest(body) == digest_of(body)
    for extra in (REVIEW_FIELD, "attempt_ordinal"):
        with pytest.raises(FatalAccounting, match="before any decision"):
            basis_digest({**body, extra: 1})


def test_a_page_basis_changes_when_any_unit_on_it_changes_or_one_is_added():
    derived = derived_review()
    before = current_basis(derived)["pages"]["page-1"]["basis_digest"]
    changed = copy.deepcopy(derived)
    changed["units"][1]["payload"]["coverage"]["floor"] = 3
    assert current_basis(changed)["pages"]["page-1"]["basis_digest"] != before
    grown = copy.deepcopy(derived)
    grown["units"].append(unit("a3", "page-1", 1, ["unread-line"], page_holds=["unread-line"]))
    assert current_basis(grown)["pages"]["page-1"]["basis_digest"] != before


def test_a_page_hold_is_the_union_of_its_units_page_holds():
    derived = derived_review()
    derived["units"][1] = unit("a2", "page-1", 1, [], page_holds=["unread-line"])
    assert current_basis(derived)["pages"]["page-1"]["page_codes"] == ["unread-line"]


def test_a_machine_review_whose_outcome_does_not_follow_its_holds_is_refused():
    derived = derived_review()
    derived["units"][2]["outcome"] = "held-for-review"
    with pytest.raises(FatalAccounting, match="held exactly when a code holds it"):
        current_basis(derived)


def _repeat_a1(units):
    units.append(copy.deepcopy(units[0]))


def _second_ordinal(units):
    units[1]["payload"]["page_ordinal"] = 9


@pytest.mark.parametrize(
    ("mutate", "message"),
    [
        (_repeat_a1, "names unit 'a1' twice"),
        (_second_ordinal, "named with two ordinals"),
        (lambda units: units[0].update(extra=1), "is not the closed"),
        (lambda units: units[0].update(act_id=""), "names no act id or page id"),
        (lambda units: units[0].update(unit_holds="doubt"), "something other than codes"),
        (lambda units: units[0]["payload"].pop("page_ordinal"), "no page ordinal or unit key"),
        (lambda units: units[2]["payload"].update(unit_class="mystery"), "names unit class"),
    ],
)
def test_a_malformed_derived_review_is_refused_by_name(mutate, message):
    derived = derived_review()
    mutate(derived["units"])
    with pytest.raises(FatalAccounting, match=message):
        current_basis(derived)


def test_clearances_name_a_unit_by_act_id_or_act_key_only():
    result = apply_decisions(derived_review(), [])
    with pytest.raises(FatalAccounting, match="named by act_id or act_key"):
        aggregate_clearances(result, unit_key="x")


# --- one decision ----------------------------------------------------------------------


def test_a_decision_bound_to_the_current_basis_is_current():
    derived = derived_review()
    summary = review_decision(decide(derived, "unit", "a1", "release"), current_basis(derived))
    assert summary["state"] == CURRENT
    assert summary["stale_because"] is None


def test_a_decision_for_another_run_is_refused():
    derived = derived_review()
    record = decide(derived, "unit", "a1", "release", run_id="run-2")
    with pytest.raises(ApprovalRefusal, match="was offered to run"):
        review_decision(record, current_basis(derived))


def test_a_v0_approval_is_not_a_review_decision():
    from common.contracts.approval import build_approval_record

    record = build_approval_record(["a1"], "exclusion", "why", "a" * 64, "2026-09-30T12:00:00Z")
    with pytest.raises(ApprovalRefusal, match="not a review decision"):
        review_decision(record, current_basis(derived_review()))


def test_a_unit_decision_naming_another_page_is_refused():
    derived = derived_review()
    record = decide(derived, "unit", "a1", "release", page_id="page-2")
    with pytest.raises(ApprovalRefusal, match="names another page"):
        review_decision(record, current_basis(derived))


def test_a_release_with_no_unit_hold_to_release_is_refused():
    derived = derived_review()
    with pytest.raises(ApprovalRefusal, match="no hold of its own"):
        review_decision(decide(derived, "unit", "a2", "release"), current_basis(derived))


def test_a_unit_decision_about_a_page_level_row_is_refused():
    derived = derived_review()
    with pytest.raises(ApprovalRefusal, match="takes page decisions"):
        review_decision(decide(derived, "unit", "u3", "exclude"), current_basis(derived))


def test_no_missed_act_on_an_unread_page_is_refused():
    derived = derived_review()
    with pytest.raises(ApprovalRefusal, match="never read"):
        review_decision(decide(derived, "page", "page-3", "no-missed-act"), current_basis(derived))


def test_no_missed_act_on_a_page_with_no_page_hold_clears_nothing_and_is_still_named():
    derived = derived_review()
    result = apply_decisions(derived, [decide(derived, "page", "page-2", "no-missed-act")])
    assert result["units"]["b1"]["outcome"] == "accepted"
    assert [(row["subject_id"], row["cleared"]) for row in result["clearances"]] == [("page-2", [])]


def test_a_decision_about_a_unit_no_longer_counted_is_stale():
    derived = derived_review()
    record = decide(derived, "unit", "a1", "release")
    derived["units"] = [u for u in derived["units"] if u["act_id"] != "a1"]
    summary = review_decision(record, current_basis(derived))
    assert (summary["state"], summary["stale_because"]) == (STALE, SUBJECT_ABSENT)


# --- applying -------------------------------------------------------------------------


def test_no_decisions_leaves_every_unit_byte_for_byte():
    derived = derived_review()
    result = apply_decisions(derived, [])
    for machine in derived["units"]:
        out = result["units"][machine["act_id"]]
        assert out["outcome"] == machine["outcome"]
        assert canonical_bytes(out["payload"]) == canonical_bytes(machine["payload"])
    assert result["applied"] == result["stale"] == result["clearances"] == []


def test_releasing_every_entry_does_not_clear_the_page_hold():
    derived = derived_review()
    result = apply_decisions(derived, [decide(derived, "unit", "a1", "release")])
    a1 = result["units"]["a1"]
    assert a1["outcome"] == "held-for-review"
    assert a1["payload"]["hold_codes"] == ["unread-line"]
    assert a1["payload"][REVIEW_FIELD]["cleared"] == {
        "unit": ["doubt-marks-malformed"],
        "page": [],
    }
    assert "still held by unread-line" in a1["payload"]["reason"]
    assert result["pages"]["page-1"]["hold_codes"] == ["unread-line"]


def test_a_release_and_no_missed_act_accept_the_unit_and_name_both_clearances():
    derived = derived_review()
    decisions = [
        decide(derived, "unit", "a1", "release"),
        decide(derived, "page", "page-1", "no-missed-act"),
    ]
    result = apply_decisions(derived, decisions)
    assert result["units"]["a1"]["outcome"] == "accepted"
    assert result["units"]["a1"]["payload"]["hold_codes"] == []
    assert result["units"]["a2"]["outcome"] == "accepted"
    assert result["pages"]["page-1"]["hold_codes"] == []
    assert [(row["scope"], row["subject_id"], row["cleared"]) for row in result["clearances"]] == [
        ("page", "page-1", ["unread-line"]),
        ("unit", "a1", ["doubt-marks-malformed"]),
    ]
    # The untouched page keeps its bytes.
    assert result["units"]["b1"]["payload"] == derived["units"][2]["payload"]


def test_a_run_whose_every_hold_was_cleared_stays_partial_naming_each_clearance():
    derived = derived_review()
    derived["units"] = derived["units"][:3]
    result = apply_decisions(
        derived,
        [
            decide(derived, "unit", "a1", "release"),
            decide(derived, "page", "page-1", "no-missed-act"),
        ],
    )
    acts = {act_id: ArmariumCategory.DELIVERED for act_id in result["units"]}
    aggregate = run_aggregate(
        acts,
        {act_id: {"under_witnessed": False, "unresolved_chairs": 0} for act_id in acts},
        {1: {"outcome": "sealed"}, 2: {"outcome": "sealed"}},
        act_pages={"a1": [1], "a2": [1], "b1": [2]},
        act_text_status={act_id: "established" for act_id in acts},
        review_clearances=aggregate_clearances(result, unit_key="act_id"),
        review_page_holds=held_pages(result),
    )
    assert held_pages(result) == {}
    assert aggregate["status"] == "partial"
    assert len(aggregate["reasons"]) == 2
    assert all("operator review decision" in reason for reason in aggregate["reasons"])


def test_a_missed_act_keeps_every_unit_on_the_page_held_after_their_release():
    derived = derived_review()
    result = apply_decisions(
        derived,
        [
            decide(derived, "unit", "a1", "release"),
            decide(derived, "page", "page-1", "missed-act"),
        ],
    )
    for act_id in ("a1", "a2"):
        assert result["units"][act_id]["outcome"] == "held-for-review"
        assert "review-missed-act" in result["units"][act_id]["payload"]["hold_codes"]
    assert result["pages"]["page-1"]["hold_codes"] == ["review-missed-act", "unread-line"]


def test_an_exclusion_is_approval_bound_and_leaves_the_page_hold_on_the_page():
    derived = derived_review()
    result = apply_decisions(derived, [decide(derived, "unit", "a2", "exclude")])
    a2 = result["units"]["a2"]
    assert a2["outcome"] == "excluded"
    assert a2["payload"]["hold_codes"] == ["unread-line"]
    assert a2["payload"][REVIEW_FIELD]["cleared"] == {"unit": [], "page": []}
    assert "its page stays held by unread-line" in a2["payload"]["reason"]
    assert classify(RECENSOR, "excluded") is OutcomeClass.COMPLETED
    assert terminal_category(RECENSOR, "excluded") is ArmariumCategory.EXCLUDED_WITH_APPROVAL
    assert result["pages"]["page-1"]["hold_codes"] == ["unread-line"]
    assert [(row["subject_id"], row["decision"]) for row in result["clearances"]] == [
        ("a2", "exclude")
    ]


def test_an_edit_and_no_missed_act_accept_the_unit_as_a_correction_not_a_clearance():
    """A person's text is the truth: the edit clears the unit's own holds and is no reason."""
    derived = derived_review()
    edit = decide(derived, "unit", "a1", "edit", text="SYNTHETIC ACT ONE", note="from the ink")
    result = apply_decisions(derived, [edit, decide(derived, "page", "page-1", "no-missed-act")])
    a1 = result["units"]["a1"]
    assert (a1["outcome"], a1["payload"]["hold_codes"]) == ("accepted", [])
    assert a1["payload"][REVIEW_FIELD]["cleared"] == {
        "unit": ["doubt-marks-malformed"],
        "page": ["unread-line"],
    }
    assert "corrected its text (a person's edit)" in a1["payload"]["reason"]
    assert [(row["scope"], row["subject_id"]) for row in result["clearances"]] == [
        ("page", "page-1")
    ]
    assert [(row["subject_id"], row["cleared"]) for row in result["corrections"]] == [
        ("a1", ["doubt-marks-malformed"])
    ]
    [summary] = [s for s in result["applied"] if s["decision"] == "edit"]
    assert summary["correction_digest"] == digest_of(
        {"text": "SYNTHETIC ACT ONE", "note": "from the ink"}
    )


def test_an_edit_alone_leaves_the_unit_held_by_its_page():
    derived = derived_review()
    result = apply_decisions(derived, [decide(derived, "unit", "a1", "edit", text="ONE")])
    assert result["units"]["a1"]["outcome"] == "held-for-review"
    assert result["units"]["a1"]["payload"]["hold_codes"] == ["unread-line"]
    # Corrected by nothing yet: no reading of it reaches the export.
    assert result["corrections"] == []


def test_an_edit_of_a_reading_the_machine_accepted_is_refused():
    derived = derived_review()
    with pytest.raises(ApprovalRefusal, match="only a held reading is corrected by a person"):
        apply_decisions(derived, [decide(derived, "unit", "b1", "edit", text="ONE")])


def test_edits_naming_different_texts_conflict_and_hold_the_unit():
    derived = derived_review()
    result = apply_decisions(
        derived,
        [
            decide(derived, "unit", "a2", "edit", text="ONE"),
            decide(derived, "unit", "a2", "edit", text="TWO"),
        ],
    )
    assert result["units"]["a2"]["outcome"] == "held-for-review"
    assert "review-conflict" in result["units"]["a2"]["payload"]["hold_codes"]
    assert len(result["conflicting"]) == 2
    assert result["corrections"] == []


def test_edits_naming_the_same_text_and_note_agree():
    derived = derived_review()
    result = apply_decisions(
        derived,
        [
            decide(derived, "unit", "a2", "edit", text="ONE", reason="first look"),
            decide(derived, "unit", "a2", "edit", text="ONE", reason="second look"),
            decide(derived, "page", "page-1", "no-missed-act"),
        ],
    )
    assert result["units"]["a2"]["outcome"] == "accepted"
    [row] = result["corrections"]
    assert len(row["decision_hashes"]) == 2


def test_an_edit_and_a_release_of_one_unit_conflict():
    derived = derived_review()
    result = apply_decisions(
        derived,
        [
            decide(derived, "unit", "a1", "edit", text="ONE"),
            decide(derived, "unit", "a1", "release"),
        ],
    )
    assert "review-conflict" in result["units"]["a1"]["payload"]["hold_codes"]


def test_a_stale_edit_corrects_nothing():
    derived = derived_review()
    edit = decide(derived, "unit", "a1", "edit", text="ONE")
    derived["units"][0]["payload"]["reason"] = "the machine read the page again"
    result = apply_decisions(derived, [edit])
    assert result["units"]["a1"]["outcome"] == "held-for-review"
    assert result["corrections"] == []
    assert [s["decision"] for s in result["stale"]] == ["edit"]


@pytest.mark.parametrize("finding", ["text-misread", "split-needed", "merge-needed"])
def test_a_misread_split_or_merge_held_with_a_finding_keeps_the_unit_held(finding):
    derived = derived_review()
    result = apply_decisions(derived, [decide(derived, "unit", "b1", "hold", finding=finding)])
    b1 = result["units"]["b1"]
    assert b1["outcome"] == "held-for-review"
    assert b1["payload"]["hold_codes"] == ["review-hold"]
    assert b1["payload"][REVIEW_FIELD]["findings"] == [finding]
    assert result["clearances"] == []


def test_re_asks_and_re_shoots_are_requests_for_the_driver_and_hold_the_unit():
    derived = derived_review()
    result = apply_decisions(
        derived,
        [
            decide(derived, "unit", "a1", "re-ask"),
            decide(derived, "page", "page-3", "re-shoot"),
        ],
    )
    assert [(r["scope"], r["subject_id"], r["decision"]) for r in result["requests"]] == [
        ("page", "page-3", "re-shoot"),
        ("unit", "a1", "re-ask"),
    ]
    assert "review-reask" in result["units"]["a1"]["payload"]["hold_codes"]
    assert "review-reshoot" in result["units"]["u3"]["payload"]["hold_codes"]


def test_no_missed_act_confirms_a_blank_page():
    derived = {
        "run_id": RUN,
        "units": [unit("p4", "page-4", 4, [PAGE_BLANK_HOLD], unit_class="page-blank")],
    }
    result = apply_decisions(derived, [decide(derived, "page", "page-4", "no-missed-act")])
    assert result["units"]["p4"]["outcome"] == "confirmed-blank"


def test_disagreeing_current_decisions_are_kept_unapplied_and_hold_their_subject():
    derived = derived_review()
    result = apply_decisions(
        derived,
        [
            decide(derived, "unit", "a1", "release"),
            decide(derived, "unit", "a1", "hold", finding="text-misread"),
        ],
    )
    a1 = result["units"]["a1"]
    assert a1["outcome"] == "held-for-review"
    assert a1["payload"]["hold_codes"] == [
        "doubt-marks-malformed",
        "review-conflict",
        "unread-line",
    ]
    assert result["applied"] == [] and result["clearances"] == []
    assert [s["decision"] for s in result["conflicting"]] == ["hold", "release"]
    assert "disagree" in a1["payload"]["reason"]


def test_holds_with_different_findings_agree_and_both_are_kept():
    derived = derived_review()
    result = apply_decisions(
        derived,
        [
            decide(derived, "unit", "b1", "hold", finding="text-misread"),
            decide(derived, "unit", "b1", "hold", finding="split-needed"),
        ],
    )
    assert result["units"]["b1"]["payload"][REVIEW_FIELD]["findings"] == [
        "split-needed",
        "text-misread",
    ]


# --- idempotence and staleness ----------------------------------------------------------


def test_applying_twice_is_applying_once_whatever_the_order():
    derived = derived_review()
    decisions = [
        decide(derived, "unit", "a1", "release"),
        decide(derived, "page", "page-1", "no-missed-act"),
        decide(derived, "unit", "b1", "hold", finding="text-misread"),
    ]
    once = canonical_bytes(apply_decisions(derived, decisions))
    assert canonical_bytes(apply_decisions(derived, decisions + decisions)) == once
    assert canonical_bytes(apply_decisions(derived, decisions[::-1])) == once
    assert canonical_bytes(apply_decisions(copy.deepcopy(derived), decisions)) == once


def test_an_applied_review_is_not_a_basis_to_apply_decisions_to_again():
    derived = derived_review()
    # A decision that adds no code, so only the review block marks the payload.
    result = apply_decisions(derived, [decide(derived, "page", "page-2", "no-missed-act")])
    assert result["units"]["b1"]["payload"]["hold_codes"] == []
    again = copy.deepcopy(derived)
    again["units"][2]["payload"] = result["units"]["b1"]["payload"]
    again["units"][2]["outcome"] = result["units"]["b1"]["outcome"]
    with pytest.raises(FatalAccounting, match="before any decision"):
        apply_decisions(again, [])


def test_a_re_pass_with_an_unchanged_basis_keeps_decisions_bound():
    derived = derived_review()
    decisions = [decide(derived, "unit", "a1", "release")]
    first = apply_decisions(derived, decisions)
    second = apply_decisions(copy.deepcopy(derived), decisions)
    assert [s["state"] for s in second["applied"]] == [CURRENT]
    assert second["units"]["a1"] == first["units"]["a1"]


def test_a_changed_basis_makes_decisions_stale_kept_and_not_applied():
    derived = derived_review()
    decisions = [
        decide(derived, "unit", "a1", "release"),
        decide(derived, "page", "page-1", "no-missed-act"),
    ]
    changed = copy.deepcopy(derived)
    changed["units"][0]["payload"]["coverage"]["floor"] = 3
    result = apply_decisions(changed, decisions)
    assert result["applied"] == []
    assert {(s["scope"], s["stale_because"]) for s in result["stale"]} == {
        ("unit", BASIS_CHANGED),
        ("page", BASIS_CHANGED),
    }
    a1 = result["units"]["a1"]
    assert a1["outcome"] == "held-for-review"
    assert a1["payload"]["hold_codes"] == changed["units"][0]["payload"]["hold_codes"]
    assert [s["state"] for s in a1["payload"][REVIEW_FIELD]["decisions"]] == [STALE, STALE]
    assert "stale" in a1["payload"]["reason"]
    assert result["clearances"] == []


def test_a_unit_decision_stays_bound_when_only_its_page_decision_goes_stale():
    derived = derived_review()
    decisions = [
        decide(derived, "unit", "a1", "release"),
        decide(derived, "page", "page-1", "no-missed-act"),
    ]
    changed = copy.deepcopy(derived)
    changed["units"][1]["payload"]["coverage"]["floor"] = 3
    result = apply_decisions(changed, decisions)
    assert [(s["scope"], s["state"]) for s in result["applied"]] == [("unit", CURRENT)]
    assert result["units"]["a1"]["payload"]["hold_codes"] == ["unread-line"]


def test_a_decision_whose_unit_is_gone_is_kept_in_its_pages_reviews():
    derived = derived_review()
    record = decide(derived, "unit", "a1", "release")
    derived["units"] = derived["units"][1:]
    result = apply_decisions(derived, [record])
    kept = result["units"]["a2"]["payload"][REVIEW_FIELD]["decisions"]
    assert [(s["subject_id"], s["stale_because"]) for s in kept] == [("a1", SUBJECT_ABSENT)]
    assert result["unkept"] == []


def test_a_decision_whose_page_is_gone_is_returned_unkept():
    derived = derived_review()
    record = decide(derived, "unit", "b1", "hold", finding="other")
    derived["units"] = [u for u in derived["units"] if u["page_id"] != "page-2"]
    result = apply_decisions(derived, [record])
    assert [s["subject_id"] for s in result["unkept"]] == ["b1"]


# --- scope as stage 4 and the page accounting set it ---------------------------------------


@pytest.mark.parametrize("page_wide", sorted(PAGE_WIDE_ENTRY_CODES))
def test_every_hold_stage_4_and_the_page_accounting_set_lands_in_its_scope(page_wide):
    """A page-wide code rides on the entry's own holds, yet is page scope."""
    from common import page_path
    from common.page_accounting import HOLD_CODES

    entry_codes = {
        page_path.DOUBT_MARKS_MALFORMED,
        page_path.READING_INCOMPLETE,
        page_path.ENTRY_NO_READABLE_TEXT,
    }
    scopes = classify_holds(
        sorted(entry_codes | {page_wide} | HOLD_CODES),
        unit_class="reading",
        unit_holds=sorted(entry_codes | {page_wide}),
        page_holds=sorted(HOLD_CODES),
    )
    assert set(scopes["unit"]) == entry_codes
    assert set(scopes["page"]) == HOLD_CODES | {page_wide}


def test_a_page_read_without_its_image_is_not_released_entry_by_entry():
    derived = {
        "run_id": RUN,
        "units": [
            unit("a1", "page-1", 1, ["no-autopsia"], unit_holds=["no-autopsia"]),
        ],
    }
    result = apply_decisions(derived, [decide(derived, "unit", "a1", "hold", finding="other")])
    assert current_basis(derived)["units"]["a1"]["page_codes"] == ["no-autopsia"]
    with pytest.raises(ApprovalRefusal, match="no hold of its own"):
        apply_decisions(derived, [decide(derived, "unit", "a1", "release")])
    assert "no-autopsia" in result["units"]["a1"]["payload"]["hold_codes"]


def test_units_of_one_page_naming_different_page_accounting_holds_are_refused():
    derived = derived_review()
    derived["units"][1]["page_holds"] = ["unread-line", "unread-ink"]
    with pytest.raises(FatalAccounting, match="different page accounting holds"):
        current_basis(derived)


def test_a_code_both_scopes_hold_stays_held_after_a_release_alone():
    derived = {
        "run_id": RUN,
        "units": [
            unit(
                "a1",
                "page-1",
                1,
                ["duplicate-region"],
                unit_holds=["duplicate-region"],
                page_holds=["duplicate-region"],
            )
        ],
    }
    result = apply_decisions(derived, [decide(derived, "unit", "a1", "release")])
    assert result["units"]["a1"]["payload"]["hold_codes"] == ["duplicate-region"]
    both = apply_decisions(
        derived,
        [
            decide(derived, "unit", "a1", "release"),
            decide(derived, "page", "page-1", "no-missed-act"),
        ],
    )
    assert both["units"]["a1"]["outcome"] == "accepted"


def test_no_missed_act_alone_leaves_a_unit_held_by_its_own_holds():
    derived = derived_review()
    result = apply_decisions(derived, [decide(derived, "page", "page-1", "no-missed-act")])
    assert result["units"]["a1"]["payload"]["hold_codes"] == ["doubt-marks-malformed"]
    assert result["units"]["a2"]["outcome"] == "accepted"


# --- exclusion and the page ---------------------------------------------------------------


def test_an_exclusion_on_a_page_with_a_missed_act_keeps_the_sign_on_the_unit_and_page():
    derived = derived_review()
    excluded = [decide(derived, "unit", "a1", "exclude"), decide(derived, "unit", "a2", "exclude")]
    basis = current_basis(derived, excluded)
    result = apply_decisions(
        derived, excluded + [decide(derived, "page", "page-1", "missed-act", basis=basis)]
    )
    for act_id in ("a1", "a2"):
        excluded = result["units"][act_id]
        assert excluded["outcome"] == "excluded"
        assert "review-missed-act" in excluded["payload"]["hold_codes"]
        assert "holds it" not in excluded["payload"]["reason"]
        assert "its page stays held by" in excluded["payload"]["reason"]
    # Every act excluded: the page is also held as one with no act.
    assert held_pages(result)[1] == [NO_ACT_ON_PAGE_HOLD, "review-missed-act", "unread-line"]
    assert [(r["subject_id"], r["cleared"]) for r in result["clearances"]] == [
        ("a1", ["doubt-marks-malformed"]),
        ("a2", []),
    ]


def test_excluding_every_act_on_a_page_holds_its_other_entries_until_no_missed_act():
    derived = {
        "run_id": RUN,
        "units": [
            unit("a1", "page-1", 1, []),
            unit("o2", "page-1", 1, [], kind="other"),
        ],
    }
    excluded = [decide(derived, "unit", "a1", "exclude")]
    result = apply_decisions(derived, excluded)
    assert result["units"]["a1"]["payload"]["hold_codes"] == []
    assert result["units"]["o2"]["outcome"] == "held-for-review"
    assert result["units"]["o2"]["payload"]["hold_codes"] == [NO_ACT_ON_PAGE_HOLD]
    assert held_pages(result) == {1: [NO_ACT_ON_PAGE_HOLD]}

    basis = current_basis(derived, excluded)
    assert basis["pages"]["page-1"]["excluded_acts"] == ["a1"]
    confirmed = apply_decisions(
        derived, excluded + [decide(derived, "page", "page-1", "no-missed-act", basis=basis)]
    )
    assert confirmed["units"]["o2"]["outcome"] == "accepted"
    assert held_pages(confirmed) == {}
    assert [(r["scope"], r["cleared"]) for r in confirmed["clearances"]] == [
        ("page", [NO_ACT_ON_PAGE_HOLD]),
        ("unit", []),
    ]


def test_no_missed_act_recorded_before_every_act_was_excluded_is_stale():
    derived = {
        "run_id": RUN,
        "units": [
            unit("a1", "page-1", 1, []),
            unit("o2", "page-1", 1, [], kind="other"),
        ],
    }
    early = decide(derived, "page", "page-1", "no-missed-act")
    result = apply_decisions(derived, [early, decide(derived, "unit", "a1", "exclude")])
    assert [(s["scope"], s["stale_because"]) for s in result["stale"]] == [("page", BASIS_CHANGED)]
    assert result["units"]["o2"]["outcome"] == "held-for-review"
    assert result["units"]["o2"]["payload"]["hold_codes"] == [NO_ACT_ON_PAGE_HOLD]
    assert held_pages(result) == {1: [NO_ACT_ON_PAGE_HOLD]}


def test_a_cleared_other_reading_is_named_by_key_in_the_run_aggregate():
    derived = {
        "run_id": RUN,
        "units": [
            unit("a1", "page-1", 1, []),
            unit(
                "o2",
                "page-1",
                1,
                ["doubt-marks-malformed"],
                unit_holds=["doubt-marks-malformed"],
                kind="other",
            ),
        ],
    }
    result = apply_decisions(derived, [decide(derived, "unit", "o2", "release")])
    aggregate = run_aggregate(
        {"p1:a1": ArmariumCategory.DELIVERED},
        {"p1:a1": {"under_witnessed": False, "unresolved_chairs": 0}},
        {1: {"outcome": "sealed"}},
        act_pages={"p1:a1": [1]},
        act_text_status={"p1:a1": "established"},
        review_clearances=aggregate_clearances(result, unit_key="act_key"),
        review_page_holds=held_pages(result),
    )
    assert aggregate["reasons"] == [
        "reading p1:o2 on page 1 was cleared by an operator review decision (release), "
        "clearing doubt-marks-malformed; a person's decision, not a machine check"
    ]


# --- citing and binding the decision set -------------------------------------------------


def test_a_summary_names_the_digest_the_run_tree_stores_the_record_under():
    from common.contracts.canonical import digest_bytes

    derived = derived_review()
    record = decide(derived, "unit", "a1", "release")
    summary = review_decision(record, current_basis(derived))
    assert summary["record_sha256"] == digest_bytes(canonical_bytes(record))


def test_the_result_names_the_decision_set_it_applied():
    derived = derived_review()
    one = [decide(derived, "unit", "a1", "release")]
    two = one + [decide(derived, "unit", "b1", "hold", finding="other")]
    assert (
        apply_decisions(derived, one)["decisions_digest"]
        == apply_decisions(derived, one + one)["decisions_digest"]
    )
    assert (
        apply_decisions(derived, one)["decisions_digest"]
        != apply_decisions(derived, two)["decisions_digest"]
    )


# --- more staleness ------------------------------------------------------------------


@pytest.mark.parametrize(
    ("name", "finding", "code"),
    [
        ("missed-act", None, "review-missed-act-carried"),
        ("hold", "other", "review-page-hold-carried"),
    ],
)
def test_a_stale_holding_page_decision_still_holds_its_page_carried(name, finding, code):
    derived = derived_review()
    record = decide(derived, "page", "page-1", name, finding=finding)
    changed = copy.deepcopy(derived)
    changed["units"][1]["payload"]["coverage"]["floor"] = 3
    result = apply_decisions(changed, [record])
    assert [s["stale_because"] for s in result["stale"]] == [BASIS_CHANGED]
    assert result["applied"] == []
    assert [s["decision_hash"] for s in result["carried"]] == [record["self_hash"]]
    assert result["pages"]["page-1"]["hold_codes"] == [code, "unread-line"]
    assert result["pages"]["page-1"]["carried"] == [code]
    for act_id in ("a1", "a2"):
        reviewed = result["units"][act_id]
        assert reviewed["outcome"] == "held-for-review"
        assert code in reviewed["payload"]["hold_codes"]
        assert reviewed["payload"][REVIEW_FIELD]["carried"] == [code]
        assert "carried from stale operator decision" in reviewed["payload"]["reason"]
    # No finding of a stale decision is presented as current.
    assert result["units"]["a1"]["payload"][REVIEW_FIELD]["findings"] == []


def test_a_stale_unit_hold_still_holds_its_unit_carried():
    derived = derived_review()
    record = decide(derived, "unit", "b1", "hold", finding="other")
    changed = copy.deepcopy(derived)
    changed["units"][2]["payload"]["coverage"]["floor"] = 3
    result = apply_decisions(changed, [record])
    assert [s["subject_id"] for s in result["stale"]] == ["b1"]
    b1 = result["units"]["b1"]
    assert b1["outcome"] == "held-for-review"
    assert b1["payload"]["hold_codes"] == ["review-hold-carried"]
    # A unit hold holds its unit, not its page.
    assert 2 not in held_pages(result)


def test_a_missed_act_recorded_before_an_exclusion_still_holds_the_page():
    derived = derived_review()
    missed = decide(derived, "page", "page-1", "missed-act")
    exclusion = decide(derived, "unit", "a2", "exclude")
    result = apply_decisions(derived, [missed, exclusion])
    assert [(s["decision"], s["stale_because"]) for s in result["stale"]] == [
        ("missed-act", BASIS_CHANGED)
    ]
    assert [s["decision"] for s in result["carried"]] == ["missed-act"]
    assert held_pages(result)[1] == ["review-missed-act-carried", "unread-line"]
    assert "review-missed-act-carried" in result["units"]["a1"]["payload"]["hold_codes"]
    a2 = result["units"]["a2"]
    assert a2["outcome"] == "excluded"
    assert "its page stays held by review-missed-act-carried" in a2["payload"]["reason"]
    # Deterministic and idempotent: order and repeats change nothing.
    assert apply_decisions(derived, [exclusion, missed, missed]) == result

    # A decision recorded against the current basis replaces the carried hold.
    basis = current_basis(derived, [exclusion])
    again = decide(derived, "page", "page-1", "missed-act", basis=basis)
    renewed = apply_decisions(derived, [missed, exclusion, again])
    assert renewed["carried"] == []
    assert held_pages(renewed)[1] == ["review-missed-act", "unread-line"]


def test_a_partial_exclusion_on_a_two_act_page_makes_no_missed_act_stale():
    derived = derived_review()
    early = decide(derived, "page", "page-1", "no-missed-act")
    result = apply_decisions(derived, [early, decide(derived, "unit", "a2", "exclude")])
    assert [(s["scope"], s["stale_because"]) for s in result["stale"]] == [("page", BASIS_CHANGED)]
    assert result["carried"] == []
    assert result["pages"]["page-1"]["cleared"] == []
    assert held_pages(result)[1] == ["unread-line"]
    assert result["units"]["a1"]["payload"]["hold_codes"] == [
        "doubt-marks-malformed",
        "unread-line",
    ]
    assert [r["subject_id"] for r in result["clearances"]] == ["a2"]


def test_a_stale_or_conflicting_exclusion_is_not_an_excluded_act():
    derived = derived_review()
    stale_exclusion = decide(derived, "unit", "a1", "exclude")
    changed = copy.deepcopy(derived)
    changed["units"][0]["payload"]["coverage"]["floor"] = 3
    assert current_basis(changed, [stale_exclusion])["pages"]["page-1"]["excluded_acts"] == []

    conflicting = [
        decide(derived, "unit", "a2", "exclude"),
        decide(derived, "unit", "a2", "hold", finding="other"),
    ]
    basis = current_basis(derived, conflicting)
    assert basis["pages"]["page-1"]["excluded_acts"] == []
    assert (
        basis["pages"]["page-1"]["basis_digest"]
        == (current_basis(derived)["pages"]["page-1"]["basis_digest"])
    )
    page = decide(derived, "page", "page-1", "missed-act")
    result = apply_decisions(derived, conflicting + [page])
    assert [s["decision"] for s in result["applied"]] == ["missed-act"]


def test_a_page_decision_bound_to_exclusions_goes_stale_when_one_is_dropped():
    derived = derived_review()
    exclusions = [
        decide(derived, "unit", "a1", "exclude"),
        decide(derived, "unit", "a2", "exclude"),
    ]
    basis = current_basis(derived, exclusions)
    assert basis["pages"]["page-1"]["excluded_acts"] == ["a1", "a2"]
    confirmed = decide(derived, "page", "page-1", "no-missed-act", basis=basis)
    assert [
        s["decision"] for s in apply_decisions(derived, exclusions + [confirmed])["applied"]
    ] == [
        "no-missed-act",
        "exclude",
        "exclude",
    ]
    result = apply_decisions(derived, exclusions[:1] + [confirmed])
    assert [(s["decision"], s["stale_because"]) for s in result["stale"]] == [
        ("no-missed-act", BASIS_CHANGED)
    ]
    assert held_pages(result)[1] == ["unread-line"]
    assert result["units"]["a2"]["payload"]["hold_codes"] == ["unread-line"]


def test_a_page_decision_whose_page_is_gone_is_returned_unkept():
    derived = derived_review()
    record = decide(derived, "page", "page-3", "re-shoot")
    derived["units"] = [u for u in derived["units"] if u["page_id"] != "page-3"]
    result = apply_decisions(derived, [record])
    assert [(s["subject_id"], s["stale_because"]) for s in result["unkept"]] == [
        ("page-3", SUBJECT_ABSENT)
    ]
    assert result["requests"] == []


# --- the basis read back from published reviews ---------------------------------------


def _published(derived, applied):
    """Each unit as a Recensor pass published it: the applied review's payload."""
    return [
        {
            **machine,
            "outcome": applied["units"][machine["act_id"]]["outcome"],
            "payload": applied["units"][machine["act_id"]]["payload"],
        }
        for machine in derived["units"]
    ]


def test_the_basis_read_back_from_published_reviews_is_the_one_the_recensor_derives():
    """A decision bound to the reviews a person read is current on the stage's next pass."""
    derived = derived_review()
    decisions = [
        decide(derived, "unit", "a1", "exclude"),
        decide(derived, "unit", "b1", "hold", finding="text-misread"),
    ]
    applied = apply_decisions(derived, decisions)
    assert REVIEW_FIELD in applied["units"]["a1"]["payload"]
    assert REVIEW_FIELD not in applied["units"]["a2"]["payload"]
    assert published_basis(RUN, _published(derived, applied), decisions) == current_basis(
        derived, decisions
    )


def test_a_basis_read_back_without_the_operator_block_would_bind_to_the_wrong_review():
    """The block keeps the machine's basis: the decided payload is not what a decision binds to."""
    derived = derived_review()
    decisions = [decide(derived, "unit", "b1", "hold", finding="text-misread")]
    applied = apply_decisions(derived, decisions)
    published = _published(derived, applied)
    decided = applied["units"]["b1"]["payload"]
    assert published_basis(RUN, published, decisions)["units"]["b1"]["basis_digest"] != (
        digest_of({key: value for key, value in decided.items() if key != REVIEW_FIELD})
    )


# --- a page re-ask asks for an operator re-read ----------------------------------------


def test_a_current_page_re_ask_asks_for_its_page_to_be_read_again():
    derived = derived_review()
    basis = current_basis(derived)
    reask = decide(derived, "page", "page-1", "re-ask")
    asked = page_reask_decisions(basis, [reask])
    assert list(asked) == ["page-1"]
    [summary] = asked["page-1"]
    assert summary["decision_hash"] == reask["self_hash"]


def test_a_stale_or_conflicting_page_re_ask_asks_for_nothing():
    derived = derived_review()
    basis = current_basis(derived)
    stale = decide(derived, "page", "page-1", "re-ask", basis_digest="0" * 64)
    assert page_reask_decisions(basis, [stale]) == {}
    conflicting = [
        decide(derived, "page", "page-1", "re-ask"),
        decide(derived, "page", "page-1", "no-missed-act"),
    ]
    assert page_reask_decisions(basis, conflicting) == {}
    unit = decide(derived, "unit", "a1", "re-ask")
    assert page_reask_decisions(basis, [unit]) == {}
