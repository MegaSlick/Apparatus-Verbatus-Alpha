"""Tests for `operations/corpus/evaluate.py`: toy outcomes computed by hand, then one real run.

The comparator is never used as its own oracle here. Every toy case states the
edit counts it expects from the strings it chose, and the one integration case
reads a run the orchestrator actually sealed and checks the driver's rows against
the export it reads them from.
"""

from __future__ import annotations

import json
import subprocess
import sys
import tomllib
from pathlib import Path

import pytest

from common.contracts.canonical import digest_bytes, digest_of
from common.runtree.store import RunTree
from operations.corpus import CorpusRefusal
from operations.corpus.compare import compare_page, load_exemplar_page_shas
from operations.corpus.evaluate import (
    EVALUATION_REFUSAL_REASONS,
    FIXTURE_LABEL,
    SCHEMA,
    evaluate_run,
    hypotheses_from_export,
    summary_lines,
    write_report,
)
from operations.corpus.reference import build_reference_page
from operations.spike_perlector.models import OutputStatus

ROOT = Path(__file__).resolve().parents[2]
ORCHESTRATOR = ROOT / "pipeline" / "orchestrator" / "run.py"
PAGE_SHA = "a" * 64


def _reference(*acts: tuple[str, dict[str, int], str]) -> dict:
    return build_reference_page(
        page={"sha256": PAGE_SHA, "width": 1000, "height": 1000},
        source="Toy",
        volume="vol",
        designation="page.jpg",
        split="val",
        records=[
            {
                "record_id": record_id,
                "region": region,
                "split": "val",
                "text": text,
                "text_sha256": digest_bytes(text.encode("utf-8")),
            }
            for record_id, region, text in acts
        ],
    )


def _act_id(name: str) -> str:
    """A well-formed `act_` identity for a toy act, derived from its readable name."""
    return "act_" + digest_bytes(name.encode("utf-8"))[:16]


def _act(name: str, region: dict[str, int]) -> dict:
    return {"act_id": _act_id(name), "bounds": region, "page_sha256": PAGE_SHA}


def _pair(comparison: dict, name: str) -> dict:
    return next(
        pair for pair in comparison["matched_pairs"] if pair["pipeline_act_id"] == _act_id(name)
    )


BOX_A = {"x": 10, "y": 10, "w": 400, "h": 100}
BOX_B = {"x": 10, "y": 300, "w": 400, "h": 100}


def test_a_perfect_reading_scores_zero_errors_and_a_substitution_scores_one():
    reference = _reference(("r1", BOX_A, "alpha beta"), ("r2", BOX_B, "gamma delta"))
    comparison = compare_page(
        reference,
        [_act("act_p", BOX_A), _act("act_q", BOX_B)],
        {
            _act_id("act_p"): (OutputStatus.COMPLETE, "alpha beta"),
            _act_id("act_q"): (OutputStatus.COMPLETE, "gamma dexta"),
        },
    )
    perfect = _pair(comparison, "act_p")
    assert perfect["cer"]["substitutions"] == perfect["cer"]["insertions"] == 0
    assert perfect["cer"]["deletions"] == 0
    assert perfect["cer"]["matches"] == perfect["cer"]["reference_units"]
    assert perfect["wer"]["matches"] == 2 and perfect["wer"]["reference_units"] == 2
    substituted = _pair(comparison, "act_q")
    assert substituted["cer"]["substitutions"] == 1
    assert substituted["cer"]["insertions"] == substituted["cer"]["deletions"] == 0
    assert substituted["wer"]["substitutions"] == 1 and substituted["wer"]["matches"] == 1
    assert comparison["misses"] == [] and comparison["unmatched_pipeline_acts"] == []


def test_an_omission_is_deletions_and_duplicated_text_is_insertions():
    reference = _reference(("r1", BOX_A, "alpha beta"), ("r2", BOX_B, "gamma delta"))
    comparison = compare_page(
        reference,
        [_act("act_p", BOX_A), _act("act_q", BOX_B)],
        {
            _act_id("act_p"): (OutputStatus.COMPLETE, "alpha"),
            _act_id("act_q"): (OutputStatus.COMPLETE, "gamma delta gamma delta"),
        },
    )
    omitted = _pair(comparison, "act_p")
    assert omitted["wer"]["deletions"] == 1 and omitted["wer"]["matches"] == 1
    assert omitted["cer"]["deletions"] == len("alpha beta") - len("alpha")
    assert omitted["cer"]["substitutions"] == omitted["cer"]["insertions"] == 0
    duplicated = _pair(comparison, "act_q")
    assert duplicated["wer"]["insertions"] == 2 and duplicated["wer"]["matches"] == 2
    assert duplicated["cer"]["insertions"] == len(" gamma delta")
    assert duplicated["cer"]["deletions"] == duplicated["cer"]["substitutions"] == 0


@pytest.mark.parametrize(
    "status", [OutputStatus.UNAVAILABLE, OutputStatus.REFUSED, OutputStatus.MISSING]
)
def test_a_held_refused_or_missing_act_is_scored_as_wholly_deleted_never_omitted(status):
    reference = _reference(("r1", BOX_A, "alpha beta"))
    comparison = compare_page(
        reference, [_act("act_p", BOX_A)], {_act_id("act_p"): (status, "alpha beta")}
    )
    pair = _pair(comparison, "act_p")
    assert pair["status"] == status.value
    assert pair["cer"]["deletions"] == pair["cer"]["reference_units"] == len("alpha beta")
    assert pair["cer"]["matches"] == 0 and pair["wer"]["deletions"] == 2
    assert comparison["misses"] == []


def test_split_merged_ambiguous_and_extra_regions_are_matched_or_reported_never_dropped():
    # r1 is a tall record; the pipeline split it into two halves. Only the half
    # meeting the IoU threshold can match; the other is reported, not scored.
    tall = {"x": 10, "y": 10, "w": 400, "h": 400}
    upper = {"x": 10, "y": 10, "w": 400, "h": 320}
    lower = {"x": 10, "y": 330, "w": 400, "h": 80}
    # r2 and r3 are two records the pipeline merged into one box: one match at
    # most, the other reference record is a miss.
    # The merged box covers both and an extra margin: IoU 20000/50000 = 2/5
    # against each, below the 1/2 threshold, so it is eligible for neither.
    # (A merged box exactly covering both would reach IoU 1/2 against each --
    # eligible -- and the assignment would pair it with one of them.)
    left = {"x": 500, "y": 10, "w": 200, "h": 100}
    right = {"x": 700, "y": 10, "w": 200, "h": 100}
    merged = {"x": 500, "y": 10, "w": 500, "h": 100}
    # extra: a region no record covers (marginalia RecordGold did not label).
    extra = {"x": 10, "y": 800, "w": 300, "h": 100}
    reference = _reference(("r1", tall, "one"), ("r2", left, "two"), ("r3", right, "three"))
    comparison = compare_page(
        reference,
        [
            _act("split_upper", upper),
            _act("split_lower", lower),
            _act("merged", merged),
            _act("extra", extra),
        ],
        {
            _act_id("split_upper"): (OutputStatus.COMPLETE, "one"),
            _act_id("split_lower"): (OutputStatus.COMPLETE, "one"),
            _act_id("merged"): (OutputStatus.COMPLETE, "two three"),
            _act_id("extra"): (OutputStatus.COMPLETE, "note"),
        },
    )
    matched = {pair["pipeline_act_id"]: pair["record_id"] for pair in comparison["matched_pairs"]}
    assert matched == {_act_id("split_upper"): "r1"}, (
        "upper half clears IoU 1/2 (320/400); merged box does not"
    )
    assert {miss["record_id"] for miss in comparison["misses"]} == {"r2", "r3"}
    assert {row["act_id"] for row in comparison["unmatched_pipeline_acts"]} == {
        _act_id("split_lower"),
        _act_id("merged"),
        _act_id("extra"),
    }
    # The whole matrix travels, so the ambiguity is inspectable: the merged box
    # overlaps both r2 and r3 but is eligible for neither.
    merged_rows = [
        row for row in comparison["matrix"] if row["pipeline_act_id"] == _act_id("merged")
    ]
    assert len(merged_rows) == 3 and not any(row["eligible"] for row in merged_rows)
    assert all(
        row["intersection_area"] > 0
        for row in merged_rows
        if row["reference_physical_act_id"]
        != _reference(("r1", tall, "one"))["acts"][0]["physical_act_id"]
    )


def test_an_export_whose_delivered_text_is_not_the_established_text_is_refused_not_scored():
    export = {
        "delivered": [
            {
                "act_id": "act_p",
                "category": "delivered",
                "text": "alpha beta",
                "text_status": "established",
            }
        ],
        "non_delivered": [
            {
                "act_id": "act_q",
                "category": "held-for-review",
                "reason": "held",
                "text_status": None,
            }
        ],
    }
    good = hypotheses_from_export(export, {"act_p": digest_of("alpha beta")})
    assert (
        good["act_p"]["status"] is OutputStatus.COMPLETE and good["act_p"]["text"] == "alpha beta"
    )
    assert good["act_q"]["status"] is OutputStatus.UNAVAILABLE and good["act_q"]["text"] is None
    assert good["act_q"]["category"] == "held-for-review"

    with pytest.raises(CorpusRefusal, match="^export-text-mismatch"):
        hypotheses_from_export(export, {"act_p": digest_of("alpha bexa")})
    with pytest.raises(CorpusRefusal, match="^unestablished-delivered-act"):
        hypotheses_from_export(export, {})
    tampered = json.loads(json.dumps(export))
    tampered["non_delivered"][0]["category"] = "delivered-ish"
    with pytest.raises(CorpusRefusal, match="^unknown-export-category"):
        hypotheses_from_export(tampered, {"act_p": digest_of("alpha beta")})
    blank = {
        "delivered": [
            {
                "act_id": "act_b",
                "category": "delivered",
                "text": "",
                "text_status": "no_readable_text",
            }
        ],
        "non_delivered": [],
    }
    assert hypotheses_from_export(blank, {"act_b": digest_of("")})["act_b"]["status"] is (
        OutputStatus.NO_READABLE_TEXT
    )
    assert {"export-text-mismatch", "unestablished-delivered-act", "unknown-export-category"} <= (
        EVALUATION_REFUSAL_REASONS
    )


def _orchestrate(run_root: Path, scenario: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [
            sys.executable,
            str(ORCHESTRATOR),
            "--fixture",
            "synthetic-two-page-v0",
            "--scenario",
            scenario,
            "--run-id",
            "r",
            "--run-root",
            str(run_root),
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )


def _fixture_reference_for_page_one(tree: RunTree) -> dict:
    """Reference truth for fixture page 1 from the fixture's own declared acts."""
    skeleton = tomllib.load((ROOT / "proof" / "skeleton_fixture.toml").open("rb"))
    page = next(row for row in skeleton["page"] if row["ordinal"] == 1)
    acts = [row for row in skeleton["act"] if row["page_ordinal"] == 1]
    return build_reference_page(
        page={
            "sha256": load_exemplar_page_shas(tree)[1],
            "width": page["width"],
            "height": page["height"],
        },
        source="fixture",
        volume="synthetic-two-page-v0",
        designation="page-1",
        split="val",
        records=[
            {
                "record_id": act["key"],
                "region": {"x": act["x"], "y": act["y"], "w": act["w"], "h": act["h"]},
                "split": "val",
                "text": act["text"],
                "text_sha256": digest_bytes(act["text"].encode("utf-8")),
            }
            for act in acts
        ],
    )


def test_a_real_partial_export_is_scored_from_its_own_records_with_the_held_act_counted(tmp_path):
    """One integration case: the map is derived from a run the orchestrator sealed."""
    run_root = tmp_path / "runs"
    completed = _orchestrate(run_root, "audit-reproof-cutoff")
    assert completed.returncode == 3, completed.stderr
    tree = RunTree(run_root, "r")
    reference = _fixture_reference_for_page_one(tree)

    report = evaluate_run(
        tree, [reference], code_ref="test", reference_ledger_sha256="b" * 64, fixture=True
    )
    assert report["schema"] == SCHEMA
    assert report["fixture"] is True and report["label"] == FIXTURE_LABEL
    assert report["run"]["export_status"] == "partial"
    assert report["run"]["scenario"] == "audit-reproof-cutoff"
    assert len(report["run"]["export_sha256"]) == 64
    assert report["corpus"] == {
        "reference_pages": 1,
        "reference_records": 2,
        "reference_ledger_sha256": "b" * 64,
        "reference_page_self_hashes": [reference["self_hash"]],
    }
    totals = report["denominators"]
    assert totals["run_pages_sealed"] == 2 and totals["run_pages_compared"] == 1
    assert totals["run_pages_without_reference"] == 1
    assert totals["exported_acts_by_category"] == {"delivered": 1, "held-for-review": 1}
    assert totals["proposed_acts"] == 2 and totals["proposal_regions"] == 3
    assert totals["reference_records_scored"] == 2
    assert totals["reference_records_scored_by_export_category"] == {
        "delivered": 1,
        "held-for-review": 1,
    }
    assert totals["reference_records_missed"] == 0
    assert totals["reference_records_not_attempted"] == 0
    assert totals["pipeline_acts_unmatched"] == 0

    rows = {row["record_id"]: row for row in report["records"]}
    held = rows["a1"]
    assert held["outcome"] == "scored" and held["export_category"] == "held-for-review"
    assert held["status"] == "unavailable"
    assert held["cer"]["deletions"] == held["cer"]["reference_units"] > 0
    assert held["cer"]["rate"] == {
        "numerator": held["cer"]["reference_units"],
        "denominator": held["cer"]["reference_units"],
    }
    delivered = rows["a2"]
    assert delivered["outcome"] == "scored" and delivered["export_category"] == "delivered"
    assert delivered["status"] == "complete"
    assert delivered["cer"]["rate"]["numerator"] == 0 and delivered["wer"]["rate"]["numerator"] == 0
    # The aggregate is the sum of both rows: the held act's whole reference is
    # in the numerator, so a held act can never improve a score.
    assert report["aggregate"]["cer"]["rate"]["numerator"] == held["cer"]["reference_units"]
    assert report["aggregate"]["cer"]["rate"]["denominator"] == (
        held["cer"]["reference_units"] + delivered["cer"]["reference_units"]
    )
    assert report["pages_without_reference"] == [
        {"ordinal": 2, "page_sha256": load_exemplar_page_shas(tree)[2]}
    ]
    lines = summary_lines(report)
    assert any("fixture result" in line for line in lines)
    assert any("held-for-review" in line for line in lines)

    written = write_report(report, tmp_path / "out" / "evaluation.json")
    assert json.loads(written.read_bytes())["self_hash"] == report["self_hash"]
    with pytest.raises(CorpusRefusal, match="^output-exists"):
        write_report(report, written)

    # A run whose export was not sealed is refused, not scored.
    with pytest.raises(CorpusRefusal, match="^no-export"):
        evaluate_run(
            RunTree(tmp_path / "nowhere", "r"),
            [reference],
            code_ref="test",
            reference_ledger_sha256=None,
            fixture=True,
        )
