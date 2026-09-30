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
    review_decision,
)
from common.stage import NO_ACT_ON_PAGE_HOLD, PAGE_BLANK_HOLD, PAGE_UNREAD_HOLD
from conftest import load_stage

RUN = "run-1"


def payload(act_key, ordinal, hold_codes, *, unit_class="reading"):
    """A page-review payload in the Recensor's closed shape; only a few fields matter here."""
    fields = {name: None for name in PAGE_REVIEW_FIELDS}
    fields.update(
        act_key=act_key,
        unit_class=unit_class,
        kind="act",
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
    Page 2: one accepted entry. Page 3: a page no reading covered."""
    return {
        "run_id": RUN,
        "units": [
            unit(
                "a1",
                "page-1",
                1,
                ["no-autopsia", "unread-line"],
                unit_holds=["no-autopsia"],
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
        ["no-autopsia", "unread-line", "residual-ink", "continuation-off-page-edge"],
        unit_class="reading",
        unit_holds=["no-autopsia"],
        page_holds=["unread-line"],
    )
    assert scopes == {
        "unit": ["continuation-off-page-edge", "no-autopsia"],
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
    assert RECENSOR_UNIT_CODES | RECENSOR_PAGE_CODES == page_review.OWN_CODES
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
    derived["units"][1] = unit("a2", "page-1", 1, [])
    assert current_basis(derived)["pages"]["page-1"]["page_codes"] == ["unread-line"]


def test_a_machine_review_whose_outcome_does_not_follow_its_holds_is_refused():
    derived = derived_review()
    derived["units"][2]["outcome"] = "held-for-review"
    with pytest.raises(FatalAccounting, match="held exactly when a code holds it"):
        current_basis(derived)


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


def test_no_missed_act_on_a_page_with_no_page_hold_is_refused():
    derived = derived_review()
    with pytest.raises(ApprovalRefusal, match="no page hold to clear"):
        review_decision(decide(derived, "page", "page-2", "no-missed-act"), current_basis(derived))


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
    assert a1["payload"][REVIEW_FIELD]["cleared"] == {"unit": ["no-autopsia"], "page": []}
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
        ("unit", "a1", ["no-autopsia"]),
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
    )
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
    assert a2["payload"]["hold_codes"] == []
    assert classify(RECENSOR, "excluded") is OutcomeClass.COMPLETED
    assert terminal_category(RECENSOR, "excluded") is ArmariumCategory.EXCLUDED_WITH_APPROVAL
    assert result["pages"]["page-1"]["hold_codes"] == ["unread-line"]
    assert [(row["subject_id"], row["decision"]) for row in result["clearances"]] == [
        ("a2", "exclude")
    ]


@pytest.mark.parametrize("finding", ["text-misread", "split-needed", "merge-needed"])
def test_a_correction_split_or_merge_is_a_finding_and_the_unit_stays_held(finding):
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


def test_disagreeing_current_decisions_about_one_subject_are_refused():
    derived = derived_review()
    with pytest.raises(ApprovalRefusal, match="disagree"):
        apply_decisions(
            derived,
            [
                decide(derived, "unit", "a1", "release"),
                decide(derived, "unit", "a1", "hold", finding="text-misread"),
            ],
        )


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
    result = apply_decisions(derived, [decide(derived, "unit", "b1", "hold", finding="other")])
    again = copy.deepcopy(derived)
    again["units"][2]["payload"] = result["units"]["b1"]["payload"]
    again["units"][2]["outcome"] = result["units"]["b1"]["outcome"]
    with pytest.raises(FatalAccounting):
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
