"""The comparability conjunct, driven over real records rather than a dict.

`dissent_against` retains a completed Testimonium whose derived payload is not
text. The required safety constraint is that `witness_coverage` counts a chair
toward the floor only where it is **attached AND comparable**. Without that
pair, a chair could be
geometrically attached, produce no comparable text at all, satisfy the floor,
and land in a run that called itself complete while every dissent row for it
read `compared: "unknown"` -- silent loss and an unproven claim, both in one record.

Two things are proven here that the arithmetic-level test in
`common/contracts/test_contracts_algebra.py` cannot prove, because it builds its
attachment facts by hand:

1. The conjunct actually fires at the seam where the floor is counted, over a
   real run's own attachment records and page testimonia -- a chair whose
   retained page testimony is structured is attached, incomparable, and below
   the floor.  **No fixture scenario produces that combination**, so before this
   module the safety net had no evidence at any stage boundary: removing
   `comparable` from `witness_coverage`'s conjunct left every end-to-end test in
   the suite green.

2. The boolean is re-derived rather than believed.  `attached`, the page
   geometry, `page_role` and `content_health` are all recomputed by both
   readers; a safety net carried by one sealed boolean nobody recomputes is
   weaker than the fact it guards, and a resealed attachment could buy the floor
   with it.  Both halves of the disagreement are refused by name.

Forged exactly one fact per test, at the read boundary, in the idiom
`test_coverage_recovery_origin.py` uses: the alternative is a fixture change,
which moves the pinned digests for a case the fixture does not otherwise need.
"""

import copy
import subprocess
import sys
from pathlib import Path

import pytest

from common.alignment import STEP_LIMIT_REASON, UNMEASURED_REASONS
from common.contracts.errors import FatalAccounting
from common.contracts.stages import ATTESTATORES, RECENSOR
from common.recensor_receipt import _reasons as receipt_reasons
from common.runtree.store import RunTree
from conftest import load_stage

ROOT = Path(__file__).resolve().parents[2]
ORCHESTRATOR = ROOT / "pipeline/orchestrator/run.py"
FIXTURE = "synthetic-two-page-v0"
SCENARIO = "happy"
PAGE_CHAIR = "attestator_1"
ACT_CHAIR = "attestator_2"


def _orchestrate(run_root: Path, run_id: str):
    return subprocess.run(
        [
            sys.executable,
            str(ORCHESTRATOR),
            "--fixture",
            FIXTURE,
            "--scenario",
            SCENARIO,
            "--run-id",
            run_id,
            "--run-root",
            str(run_root),
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
    )


@pytest.fixture
def context_and_act(tmp_path):
    """A real happy run, plus a live Recensor context over its first act."""
    root = tmp_path / "runs"
    result = _orchestrate(root, "comparability")
    assert result.returncode == 0, result.stderr
    recensor = load_stage("5_recensor")
    args = recensor.stage_parser("comparability floor test").parse_args(
        [
            "--run-root",
            str(root),
            "--run-id",
            "comparability",
            "--scenario",
            SCENARIO,
            "--fixture-root",
            str(ROOT / "proof"),
        ]
    )
    context = recensor.open_context(args, RECENSOR)
    act = next(act for act in recensor.expected_acts(context) if act["act_key"] == "a1")
    return recensor, context, act, RunTree(root, "comparability")


def _structured_page_testimony(context, monkeypatch, chair=PAGE_CHAIR, page_ordinal=1):
    """Give one page witness the structured retained payload a native adapter
    can legitimately return, leaving its geometry -- and so its attachment --
    untouched. The only native-geometry chair must remain geometrically attached
    so the test isolates comparability rather than attachment."""
    original = context.tree.read_artifact_reference

    def structured(reference, *, stage, kind, subject_id):
        record = original(reference, stage=stage, kind=kind, subject_id=subject_id)
        if (
            kind == "page-testimonium"
            and record["payload"]["chair"] == chair
            and record["payload"]["page_ordinal"] == page_ordinal
        ):
            record = copy.deepcopy(record)
            record["payload"]["payload"] = {"blocks": [{"text": "unjoinable"}]}
        return record

    monkeypatch.setattr(context.tree, "read_artifact_reference", structured)


def _rewrite_attachment(context, monkeypatch, act_id, chair, changes, page_ordinal=None):
    original = context.tree.read_artifact

    def rewritten(stage, kind, artifact_id):
        record = original(stage, kind, artifact_id)
        if stage == ATTESTATORES and kind == "act-attachment" and record["subject_id"] == act_id:
            record = copy.deepcopy(record)
            row = next(
                row
                for row in record["payload"]["attachments"]
                if row["chair"] == chair and row["page_ordinal"] == page_ordinal
            )
            row.update(changes)
        return record

    monkeypatch.setattr(context.tree, "read_artifact", rewritten)


def test_a_geometrically_attached_page_witness_without_page_text_stays_below_the_floor(
    context_and_act, monkeypatch
):
    """The case the retirement was paid for, at the seam that counts the floor.

    The chair read, its native geometry overlaps this act's sealed proposal, and
    its attachment is honestly `attached: true`.  What it has no more of is
    comparable text, so it cannot corroborate a reading and must not be counted
    as though it had -- it lands in the existing unaligned shortfall and in
    `page_granularity_only`, and the act is under-witnessed.
    """
    recensor, context, act, _tree = context_and_act
    _structured_page_testimony(context, monkeypatch)
    _rewrite_attachment(
        context, monkeypatch, act["act_id"], PAGE_CHAIR, {"comparable": False}, page_ordinal=1
    )

    current = recensor.chair_current_attempts(context, act["act_id"])
    outcomes = recensor.chair_outcomes(current)
    facts = recensor.act_attachment_facts(context, act["act_id"], current)

    assert facts[PAGE_CHAIR]["attached"] is True
    assert facts[PAGE_CHAIR]["comparable"] is False
    assert facts[PAGE_CHAIR]["attachment_basis"] == "geometric-overlap"

    coverage = recensor.witness_coverage(outcomes, context.witness_floor, attachments=facts)
    assert coverage["under_witnessed"] is True
    assert coverage["page_granularity_only"] == 1
    assert coverage["shortfalls"]["unaligned"] == 1
    # The receipt identity the consult required to hold BY CONSTRUCTION: the
    # chairs that count are the reading chairs minus the page-only ones.
    reading = sum(
        count
        for outcome, count in coverage["by_outcome"].items()
        if outcome in recensor.WITNESS_READING_OUTCOMES
    )
    assert reading - coverage["page_granularity_only"] == 2


def test_a_page_attachment_may_not_claim_a_comparability_its_testimony_denies(
    context_and_act, monkeypatch
):
    """The sealed boolean is evidence only where the evidence agrees with it."""
    recensor, context, act, _tree = context_and_act
    _structured_page_testimony(context, monkeypatch)

    current = recensor.chair_current_attempts(context, act["act_id"])
    with pytest.raises(FatalAccounting, match="retained page testimony does not support"):
        recensor.act_attachment_facts(context, act["act_id"], current)


def test_an_act_scoped_attachment_may_not_claim_a_comparability_its_payload_denies(
    context_and_act, monkeypatch
):
    """The act-scoped half of the same derivation.

    An act-scoped chair's comparable text is its own retained derived payload,
    and only a string is text.  A structured native report is retained and
    visible; it is not a witness this act may count.
    """
    recensor, context, act, _tree = context_and_act
    original = context.tree.read_artifact_reference

    def structured(reference, *, stage, kind, subject_id):
        record = original(reference, stage=stage, kind=kind, subject_id=subject_id)
        if kind == "testimonium" and record["payload"]["chair"] == ACT_CHAIR:
            record = copy.deepcopy(record)
            record["payload"]["payload"] = {"tokens": ["mu", "beta"]}
        return record

    monkeypatch.setattr(context.tree, "read_artifact_reference", structured)

    current = recensor.chair_current_attempts(context, act["act_id"])
    with pytest.raises(FatalAccounting, match="retained derived testimony does not support"):
        recensor.act_attachment_facts(context, act["act_id"], current)


def test_an_act_scoped_attachment_may_not_name_another_chairs_testimonium(
    context_and_act, monkeypatch
):
    """Reading the wrong chair's payload would launder one chair into another.

    The Perlector already refuses this at its own read seam; the floor seam
    reads the same record now, so it makes the same check rather than trusting
    that the other reader ran first.
    """
    recensor, context, act, _tree = context_and_act
    original = context.tree.read_artifact_reference

    def relabelled(reference, *, stage, kind, subject_id):
        record = original(reference, stage=stage, kind=kind, subject_id=subject_id)
        if kind == "testimonium" and record["payload"]["chair"] == ACT_CHAIR:
            record = copy.deepcopy(record)
            record["payload"]["chair"] = PAGE_CHAIR
        return record

    monkeypatch.setattr(context.tree, "read_artifact_reference", relabelled)

    current = recensor.chair_current_attempts(context, act["act_id"])
    with pytest.raises(FatalAccounting, match="another chair's Testimonium"):
        recensor.act_attachment_facts(context, act["act_id"], current)


def _rewrite_page_alignment(context, monkeypatch, act_id, change, page_ordinal=1):
    """Apply `change` to the page witness's aligned record on one page."""
    original = context.tree.read_artifact

    def rewritten(stage, kind, artifact_id):
        record = original(stage, kind, artifact_id)
        if stage == ATTESTATORES and kind == "act-attachment" and record["subject_id"] == act_id:
            record = copy.deepcopy(record)
            row = next(
                row
                for row in record["payload"]["attachments"]
                if row["chair"] == PAGE_CHAIR and row["page_ordinal"] == page_ordinal
            )
            assert row["alignment"]["status"] == "aligned", row
            change(row)
        return record

    monkeypatch.setattr(context.tree, "read_artifact", rewritten)


def test_an_aligned_record_carrying_the_retired_deadline_field_is_refused_by_name(
    context_and_act, monkeypatch
):
    """A record aligned under a wall-clock deadline says whether it aligned
    depended on the machine. The Recensor names the retired field rather than
    counting the chair from it or refusing it as an anonymous shape error."""
    recensor, context, act, _tree = context_and_act

    def carry_deadline(row):
        row["alignment"]["deadline_in_force"] = True

    _rewrite_page_alignment(context, monkeypatch, act["act_id"], carry_deadline)

    current = recensor.chair_current_attempts(context, act["act_id"])
    with pytest.raises(FatalAccounting, match="retired alignment field.*deadline_in_force"):
        recensor.act_attachment_facts(context, act["act_id"], current)


def test_an_unaligned_record_carrying_the_retired_deadline_reason_is_refused_by_name(
    context_and_act, monkeypatch
):
    """A wall-clock stop measured nothing, but read as an ordinary unaligned
    reason it would land in `unaligned`, the bucket of comparisons made. The
    Recensor names the retired reason instead of counting the chair from it."""
    recensor, context, act, _tree = context_and_act

    def stopped_on_the_clock(row):
        row["alignment"] = {"status": "unaligned", "reason": "alignment-deadline-exceeded"}
        row["comparable"] = False

    _rewrite_page_alignment(context, monkeypatch, act["act_id"], stopped_on_the_clock)

    current = recensor.chair_current_attempts(context, act["act_id"])
    with pytest.raises(FatalAccounting, match="retired unaligned reason 'alignment-deadline"):
        recensor.act_attachment_facts(context, act["act_id"], current)


@pytest.mark.parametrize("reason", sorted(UNMEASURED_REASONS))
def test_an_aligner_stop_is_unmeasured_not_a_measured_shortfall(
    context_and_act, monkeypatch, reason
):
    """The aligner stopping on any of its own bounds measured nothing, so the
    chair lands in `unmeasured`, apart from `unaligned`, a comparison made and
    found not to cover. It still leaves the floor: no comparison may be claimed.
    Both hold reasons name it as unmeasured, never as a witness failure."""
    recensor, context, act, _tree = context_and_act

    def stop(row):
        row["alignment"] = {"status": "unaligned", "reason": reason}
        row["comparable"] = False

    _rewrite_page_alignment(context, monkeypatch, act["act_id"], stop)

    current = recensor.chair_current_attempts(context, act["act_id"])
    outcomes = recensor.chair_outcomes(current)
    facts = recensor.act_attachment_facts(context, act["act_id"], current)
    assert facts[PAGE_CHAIR]["alignment_unmeasured"] is True
    assert facts[ACT_CHAIR]["alignment_unmeasured"] is False

    coverage = recensor.witness_coverage(outcomes, context.witness_floor, attachments=facts)
    assert coverage["under_witnessed"] is True
    assert coverage["shortfalls"] == {"failed": 0, "truncated": 0, "unaligned": 0, "unmeasured": 1}

    assert recensor.alignment_unmeasured_chairs(facts) == [PAGE_CHAIR]
    _outcome, route = recensor.review_route_from_findings(
        testimony_shortfall=False,
        audit_unresolved=False,
        under_witnessed=coverage["under_witnessed"],
        unmeasured_chairs=recensor.alignment_unmeasured_chairs(facts),
    )
    assert f"chair(s) ['{PAGE_CHAIR}'] were never compared with this act" in route
    assert "unmeasured, not failed" in route
    assert "a witness failure" not in route

    (receipt_reason,) = receipt_reasons(
        [{"act_id": act["act_id"], "partition_class": "completed", "coverage": coverage}]
    )
    assert "is under-witnessed" in receipt_reason
    assert "1 chair(s) were never compared with this act" in receipt_reason
    assert "unmeasured, not failed" in receipt_reason


def _page_witness_pages(context, act_id):
    """The page ordinals of the page witness's attachment rows, in record order."""
    (attachment,) = [
        context.tree.read_artifact(ATTESTATORES, "act-attachment", entry["artifact_id"])
        for entry in context.tree.build_manifest(ATTESTATORES)["artifacts"]
        if entry["kind"] == "act-attachment" and entry["subject_id"] == act_id
    ]
    return [
        row["page_ordinal"]
        for row in attachment["payload"]["attachments"]
        if row["chair"] == PAGE_CHAIR
    ]


def test_a_two_page_act_stays_unmeasured_whichever_page_row_comes_first(
    context_and_act, monkeypatch
):
    """The fixture's act that runs onto a second page has two page rows per
    page witness: its primary page, aligned here, and a continuation page, never
    aligned. With the primary page stopped on the step budget, the merged chair
    is unmeasured in either row order: the continuation row does not overwrite
    it, and a merged attached-and-comparable pair is never invented."""
    recensor, context, _act, _tree = context_and_act
    act = next(
        act
        for act in recensor.expected_acts(context)
        if len(_page_witness_pages(context, act["act_id"])) == 2
    )

    def spend_budget_and_reverse(row):
        row["alignment"] = {"status": "unaligned", "reason": STEP_LIMIT_REASON}
        row["comparable"] = False

    for reverse in (False, True):
        _rewrite_page_alignment(context, monkeypatch, act["act_id"], spend_budget_and_reverse)
        if reverse:
            rewritten = context.tree.read_artifact

            def reordered(stage, kind, artifact_id, _read=rewritten):
                record = _read(stage, kind, artifact_id)
                if (
                    stage == ATTESTATORES
                    and kind == "act-attachment"
                    and record["subject_id"] == act["act_id"]
                ):
                    record["payload"]["attachments"].reverse()
                return record

            monkeypatch.setattr(context.tree, "read_artifact", reordered)
        pages = _page_witness_pages(context, act["act_id"])
        assert pages == ([2, 1] if reverse else [1, 2])

        current = recensor.chair_current_attempts(context, act["act_id"])
        outcomes = recensor.chair_outcomes(current)
        facts = recensor.act_attachment_facts(context, act["act_id"], current)
        assert facts[PAGE_CHAIR]["alignment_unmeasured"] is True
        assert (facts[PAGE_CHAIR]["attached"], facts[PAGE_CHAIR]["comparable"]) != (True, True)
        coverage = recensor.witness_coverage(outcomes, context.witness_floor, attachments=facts)
        assert coverage["shortfalls"]["unmeasured"] == 1
        assert coverage["shortfalls"]["unaligned"] == 0
        monkeypatch.undo()


def test_a_measured_non_overlap_stays_unaligned(context_and_act, monkeypatch):
    """The other half of the split: an alignment that ran and found no common
    text is a measurement, and stays in `unaligned`."""
    recensor, context, act, _tree = context_and_act

    def no_common_text(row):
        row["alignment"] = {"status": "unaligned", "reason": "no-common-anchor-text"}
        row["comparable"] = False

    _rewrite_page_alignment(context, monkeypatch, act["act_id"], no_common_text)

    current = recensor.chair_current_attempts(context, act["act_id"])
    outcomes = recensor.chair_outcomes(current)
    facts = recensor.act_attachment_facts(context, act["act_id"], current)
    coverage = recensor.witness_coverage(outcomes, context.witness_floor, attachments=facts)
    assert coverage["shortfalls"] == {"failed": 0, "truncated": 0, "unaligned": 1, "unmeasured": 0}
