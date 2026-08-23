"""The comparability conjunct, driven over real records rather than a dict.

Unit 14A retired `dissent_against`'s refusal of a completed Testimonium whose
retained derived payload is not text.  The consult (`/out/CONSULT_REPORT.md` 3)
made that retirement conditional on a safety net in the same commit: an act
attachment gained a `comparable` boolean, and `witness_coverage` counts a chair
toward the witness floor only where it is **attached AND comparable**.  Without
that pair, after 10C made attachment purely geometric, a chair could be
geometrically attached, produce no comparable text at all, satisfy the floor,
and land in a run that called itself complete while every dissent row for it
read `compared: "unknown"` -- GOVERNANCE 2 and 10 in one record.

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
import importlib.util
import subprocess
import sys
from pathlib import Path

import pytest

from common.contracts.errors import FatalAccounting
from common.contracts.stages import ATTESTATORES, RECENSOR
from common.runtree.store import RunTree

ROOT = Path(__file__).resolve().parents[2]
ORCHESTRATOR = ROOT / "pipeline/orchestrator/run.py"
FIXTURE = "synthetic-two-page-v0"
SCENARIO = "happy"
PAGE_CHAIR = "attestator_1"
ACT_CHAIR = "attestator_2"


def _load_recensor():
    spec = importlib.util.spec_from_file_location(
        "recensor_comparability_under_test", ROOT / "pipeline/5_recensor/run.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


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
    recensor = _load_recensor()
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
    untouched.  This is the consult's own worst case: the only native-geometry
    chair is exactly the chair that can be geometrically attached."""
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


def test_an_act_scoped_attachment_cannot_forge_both_floor_booleans_false(
    context_and_act, monkeypatch
):
    """The Recensor derives attachment from the current Testimonium itself.

    A mirror that computes ``comparable`` from the attachment row's own
    ``attached`` value is one assertion in two costumes: forging both false
    removes a real reading chair from the floor while preserving the equation.
    The current Testimonium is independent evidence and names the forgery.
    """
    recensor, context, act, _tree = context_and_act
    _rewrite_attachment(
        context,
        monkeypatch,
        act["act_id"],
        ACT_CHAIR,
        {
            "attached": False,
            "comparable": False,
            "attachment_basis": "unattached",
            "span": None,
        },
    )

    current = recensor.chair_current_attempts(context, act["act_id"])
    with pytest.raises(FatalAccounting, match="disagrees with the current Testimonium outcome"):
        recensor.act_attachment_facts(context, act["act_id"], current)
