"""Tests for `operations/corpus/evaluate.py`: toy outcomes computed by hand, then one real run.

The comparator is never used as its own oracle here. Every toy case states the
edit counts it expects from the strings it chose, and the integration cases read
a run the orchestrator actually sealed and check the driver's rows against the
export it reads them from.

The three outcomes the whole "whole denominator" claim rests on -- a reference
record missed, a reference record not attempted, and a pipeline act nothing
matched -- each have their own case here, and every reason in
`EVALUATION_REFUSAL_REASONS` is shown to fire (independent audit of 2026-09-11,
finding 15).
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
import tomllib
from pathlib import Path

import pytest

from common.contracts.canonical import canonical_bytes, digest_bytes, digest_of
from common.contracts.canonical import self_hash as _self_hash
from common.runtree.store import RunTree
from operations.corpus import CorpusRefusal
from operations.corpus.compare import compare_page, load_exemplar_page_shas
from operations.corpus.evaluate import (
    EVALUATION_REFUSAL_REASONS,
    FIXTURE_LABEL,
    SCHEMA,
    evaluate_run,
    hypotheses_from_export,
    load_reference_ledger,
    load_reference_pages,
    main,
    run_is_fixture,
    summary_lines,
    validate_evaluation,
    write_report,
)
from operations.corpus.local_admission import SCHEMA as LEDGER_SCHEMA
from operations.corpus.local_admission import validate_local_admission_ledger
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


def _reseal(report: dict) -> dict:
    body = {key: value for key, value in report.items() if key != "self_hash"}
    body["self_hash"] = _self_hash(body)
    return body


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

    with pytest.raises(CorpusRefusal, match="^export-text-mismatch:"):
        hypotheses_from_export(export, {"act_p": digest_of("alpha bexa")})
    with pytest.raises(CorpusRefusal, match="^unestablished-delivered-act:"):
        hypotheses_from_export(export, {})
    tampered = json.loads(json.dumps(export))
    tampered["non_delivered"][0]["category"] = "delivered-ish"
    with pytest.raises(CorpusRefusal, match="^unknown-export-category:"):
        hypotheses_from_export(tampered, {"act_p": digest_of("alpha beta")})
    with pytest.raises(CorpusRefusal, match="^malformed-record:"):
        hypotheses_from_export({"delivered": []}, {})
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


def test_the_fixture_label_is_read_from_the_runs_own_sealed_identity_not_from_a_flag():
    """A flag can be omitted; the export's identity field cannot be (finding 11)."""
    assert run_is_fixture({"fixture_id": "synthetic-two-page-v0"}) is True
    assert run_is_fixture({"submission_id": "a" * 64}) is False
    with pytest.raises(CorpusRefusal, match="^ambiguous-run-identity:"):
        run_is_fixture({})
    with pytest.raises(CorpusRefusal, match="^ambiguous-run-identity:"):
        run_is_fixture({"fixture_id": "f", "submission_id": "s"})
    # A blank identity is no identity, and must not read as a fixture run.
    with pytest.raises(CorpusRefusal, match="^ambiguous-run-identity:"):
        run_is_fixture({"fixture_id": "   "})
    assert run_is_fixture({"fixture_id": "f", "submission_id": ""}) is True


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


def _fixture_acts() -> list[dict]:
    skeleton = tomllib.load((ROOT / "proof" / "skeleton_fixture.toml").open("rb"))
    return [row for row in skeleton["act"] if row["page_ordinal"] == 1]


def _fixture_page() -> dict:
    skeleton = tomllib.load((ROOT / "proof" / "skeleton_fixture.toml").open("rb"))
    return next(row for row in skeleton["page"] if row["ordinal"] == 1)


def _record_row(act: dict) -> dict:
    return {
        "record_id": act["key"],
        "region": {"x": act["x"], "y": act["y"], "w": act["w"], "h": act["h"]},
        "split": "val",
        "text": act["text"],
        "text_sha256": digest_bytes(act["text"].encode("utf-8")),
    }


def _fixture_reference_for_page_one(tree: RunTree, *, keep=None, extra=None) -> dict:
    """Reference truth for fixture page 1 from the fixture's own declared acts.

    `keep` narrows which of the fixture's acts are annotated (so the pipeline act
    for a dropped one becomes an unmatched act, reported and not scored) and
    `extra` adds an annotated record over ink the pipeline proposed nothing for
    (a miss).
    """
    page = _fixture_page()
    records = [_record_row(act) for act in _fixture_acts() if keep is None or act["key"] in keep]
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
        records=records + list(extra or []),
    )


def _ledger_for(*references: dict) -> dict:
    """A real `recordgold-local-admission.v1` around the given reference pages.

    `evaluate_run` validates the ledger it is handed now, so a test cannot hand
    it a two-field stand-in: this builds the shape `admit_local_set` produces,
    one admitted row per act, and seals it the same way.
    """
    rows = []
    for reference in references:
        for act in reference["acts"]:
            rows.append(
                {
                    "record_id": act["record_id"],
                    "page_id": reference["designation"],
                    "split": reference["split"],
                    "record_url": "https://europe.iiif.teklia.com/iiif/2/"
                    f"{reference['volume']}%2F{reference['designation']}/"
                    f"{act['region']['x']},{act['region']['y']},"
                    f"{act['region']['w']},{act['region']['h']}/full/0/default.jpg",
                    "iiif_rotation": "0",
                    "source_bbox": [act["region"][key] for key in ("x", "y", "w", "h")],
                    "bbox": [act["region"][key] for key in ("x", "y", "w", "h")],
                    "page_sha256": reference["page"]["sha256"],
                    "page_width": reference["page"]["width"],
                    "page_height": reference["page"]["height"],
                    "page_image": f"pages/{reference['designation']}.jpg",
                    "physical_page_id": "ppg_"
                    + digest_bytes(reference["self_hash"].encode("utf-8"))[:16],
                    "physical_act_id": act["physical_act_id"],
                    "reference_page_self_hash": reference["self_hash"],
                    "decision": "admitted",
                    "reason": None,
                    "detail": None,
                }
            )
    ledger = {
        "schema": LEDGER_SCHEMA,
        "corpus_id": "recordgold",
        "set_root": "/nowhere/a-synthetic-set",
        "split": "val",
        "receipt": {
            "path": "/nowhere/a-synthetic-set/fetch_receipt.json",
            "receipt_sha256": digest_bytes(b"receipt"),
            "status": "complete",
            "requested_splits": ["val"],
            "digests": {
                "gold.jsonl": digest_bytes(b"gold"),
                "page_manifest.jsonl": digest_bytes(b"manifest"),
            },
        },
        "row_snapshot": {"consulted": False, "self_hash": None, "records_cross_checked": 0},
        "summary": {
            "records": len(rows),
            "admitted": len(rows),
            "refused": 0,
            "refused_by_reason": {},
            "pages_listed": len(references),
            "pages_by_outcome": {
                "admitted": len(references),
                "refused": 0,
                "no-gold-row": 0,
                "all-records-refused": 0,
            },
            "admitted_by_rotation": {"0": len(rows)},
        },
        "rows": rows,
        "reference_pages": list(references),
    }
    ledger["self_hash"] = _self_hash(ledger)
    return validate_local_admission_ledger(ledger)


def _page_never_sealed() -> dict:
    """A reference page whose digest no run sealed: its records are not attempted."""
    return build_reference_page(
        page={"sha256": "c" * 64, "width": 1000, "height": 1000},
        source="fixture",
        volume="synthetic-two-page-v0",
        designation="page-elsewhere",
        split="val",
        records=[
            {
                "record_id": "elsewhere-1",
                "region": {"x": 1, "y": 1, "w": 100, "h": 50},
                "split": "val",
                "text": "un acte sur une page que rien n'a scellée",
                "text_sha256": digest_bytes(
                    "un acte sur une page que rien n'a scellée".encode("utf-8")
                ),
            }
        ],
    )


@pytest.fixture(scope="module")
def sealed_run(tmp_path_factory):
    """One orchestrated fixture run, shared by every integration case below."""
    run_root = tmp_path_factory.mktemp("runs")
    completed = _orchestrate(run_root, "audit-reproof-cutoff")
    assert completed.returncode == 3, completed.stderr
    return RunTree(run_root, "r")


def test_a_real_partial_export_is_scored_from_its_own_records_with_the_held_act_counted(
    sealed_run, tmp_path
):
    """One integration case: the map is derived from a run the orchestrator sealed."""
    tree = sealed_run
    reference = _fixture_reference_for_page_one(tree)

    ledger = _ledger_for(reference)
    report = evaluate_run(tree, [reference], code_ref="test", reference_ledger=ledger)
    assert report["schema"] == SCHEMA
    assert report["fixture"] is True and report["label"] == FIXTURE_LABEL
    assert report["run"]["export_status"] == "partial"
    assert report["run"]["scenario"] == "audit-reproof-cutoff"
    assert len(report["run"]["export_sha256"]) == 64
    assert report["corpus"] == {
        "reference_pages": 1,
        "reference_records": 2,
        # Derived from the ledger's own body, never accepted beside it.
        "reference_ledger_sha256": digest_bytes(canonical_bytes(ledger)),
        "reference_ledger_verified": True,
        "reference_page_self_hashes": [reference["self_hash"]],
        "splits": {"scored": ["val"], "present_on_pages": ["val"]},
    }
    check = report["code_ref_check"]
    assert check["state"] in ("matches-checkout", "differs-from-checkout", "no-checkout-found")
    if check["checkout_head"] is not None:
        # Any honest abbreviation of the head reads as a match, not just the
        # three lengths this module first thought of (round 2 item 3).
        for length in (7, 10, 12, 40):
            abbreviated = evaluate_run(
                sealed_run, [reference], code_ref=check["checkout_head"][:length]
            )
            assert abbreviated["code_ref_check"]["state"] == "matches-checkout"
        short = evaluate_run(sealed_run, [reference], code_ref=check["checkout_head"][:6])
        assert short["code_ref_check"]["state"] == "differs-from-checkout"
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
    # in the numerator, so a held act can never improve a score. With nothing
    # missed, the two aggregates agree exactly.
    assert report["aggregate"]["normalization_profile_id"] == "graphemic-v1"
    # The validator holds the profile to the declared vocabulary, not to the
    # constant this module happens to score with today: a report sealed under
    # another declared profile is a historically correct record.
    other = json.loads(json.dumps(report))
    other["aggregate"]["normalization_profile_id"] = "allographic-v1"
    assert validate_evaluation(_reseal(other))["fixture"] is True
    unknown = json.loads(json.dumps(report))
    unknown["aggregate"]["normalization_profile_id"] = "not-a-profile"
    with pytest.raises(CorpusRefusal, match="^malformed-record:"):
        validate_evaluation(_reseal(unknown))
    matched = report["aggregate"]["matched_pairs_only"]
    assert matched["cer"]["rate"]["numerator"] == held["cer"]["reference_units"]
    assert matched["cer"]["rate"]["denominator"] == (
        held["cer"]["reference_units"] + delivered["cer"]["reference_units"]
    )
    assert report["aggregate"]["including_missed_records"]["cer"] == matched["cer"]
    assert report["pages_without_reference"] == [
        {"ordinal": 2, "page_sha256": load_exemplar_page_shas(tree)[2]}
    ]
    lines = summary_lines(report)
    assert any("fixture result" in line for line in lines)
    assert any("held-for-review" in line for line in lines)
    assert any("matched pairs only" in line for line in lines)
    assert any("including missed records" in line for line in lines)
    assert any("split(s) val" in line for line in lines)

    written = write_report(report, tmp_path / "out" / "evaluation.json")
    assert json.loads(written.read_bytes())["self_hash"] == report["self_hash"]
    with pytest.raises(CorpusRefusal, match="^output-exists:"):
        write_report(report, written)


def test_a_missed_record_moves_only_the_aggregate_that_counts_it(sealed_run):
    """GOALS 1: an act nobody found must be visible in a number, not only in a count."""
    tree = sealed_run
    unread = "un acte que le pipeline n'a jamais proposé"
    missed_record = {
        "record_id": "never-proposed",
        "region": {"x": 5, "y": 5, "w": 60, "h": 20},
        "split": "val",
        "text": unread,
        "text_sha256": digest_bytes(unread.encode("utf-8")),
    }
    baseline = evaluate_run(tree, [_fixture_reference_for_page_one(tree)], code_ref="test")
    report = evaluate_run(
        tree,
        [_fixture_reference_for_page_one(tree, extra=[missed_record])],
        code_ref="test",
    )
    assert report["denominators"]["reference_records_missed"] == 1
    missed = next(row for row in report["records"] if row["record_id"] == "never-proposed")
    assert missed["outcome"] == "missed" and missed["cer"] is None
    assert missed["pipeline_act_id"] is None and missed["export_category"] is None

    # The matched-pairs rate cannot see the miss at all; the second one does.
    assert (
        report["aggregate"]["matched_pairs_only"]["cer"]
        == baseline["aggregate"]["matched_pairs_only"]["cer"]
    )
    with_missed = report["aggregate"]["including_missed_records"]["cer"]
    matched_only = report["aggregate"]["matched_pairs_only"]["cer"]
    assert with_missed["deletions"] > matched_only["deletions"]
    assert with_missed["rate"]["numerator"] > matched_only["rate"]["numerator"]
    assert "missed" in report["aggregate"]["including_missed_records"]["scope"]


def test_a_page_the_run_never_sealed_leaves_its_records_not_attempted_never_scored(sealed_run):
    tree = sealed_run
    report = evaluate_run(
        tree,
        [_fixture_reference_for_page_one(tree), _page_never_sealed()],
        code_ref="test",
    )
    assert report["denominators"]["reference_pages_not_in_run"] == 1
    assert report["denominators"]["reference_records_not_attempted"] == 1
    row = next(row for row in report["records"] if row["record_id"] == "elsewhere-1")
    assert row["outcome"] == "not-attempted" and row["page_sha256"] == "c" * 64
    # Not attempted is a coverage gap, not a reading: neither rate counts it.
    assert (
        report["aggregate"]["including_missed_records"]["cer"]
        == (report["aggregate"]["matched_pairs_only"]["cer"])
    )


def test_a_pipeline_act_no_reference_record_covers_is_reported_and_not_scored(sealed_run):
    tree = sealed_run
    report = evaluate_run(
        tree, [_fixture_reference_for_page_one(tree, keep={"a2"})], code_ref="test"
    )
    assert report["denominators"]["pipeline_acts_unmatched"] == 1
    (unmatched,) = report["unmatched_pipeline_acts"]
    assert unmatched["export_category"] == "held-for-review"
    assert "records-only" in unmatched["note"]
    # It moved no rate: the reference it would have been scored against is not
    # annotated, so scoring it would invent a denominator.
    assert report["denominators"]["reference_records_scored"] == 1
    assert report["denominators"]["reference_records_missed"] == 0


def test_every_record_row_carries_one_closed_shape_whatever_its_outcome(sealed_run):
    """A scored, a missed and a not-attempted row all answer the same questions."""
    tree = sealed_run
    unread = "un acte que le pipeline n'a jamais proposé"
    report = evaluate_run(
        tree,
        [
            _fixture_reference_for_page_one(
                tree,
                extra=[
                    {
                        "record_id": "never-proposed",
                        "region": {"x": 5, "y": 5, "w": 60, "h": 20},
                        "split": "val",
                        "text": unread,
                        "text_sha256": digest_bytes(unread.encode("utf-8")),
                    }
                ],
            ),
            _page_never_sealed(),
        ],
        code_ref="test",
    )
    assert {row["outcome"] for row in report["records"]} == {
        "scored",
        "missed",
        "not-attempted",
    }
    assert len({frozenset(row) for row in report["records"]}) == 1


def test_a_comparison_refusal_travels_under_this_modules_name(sealed_run):
    """`compare_page` refuses by its own vocabulary; a caller sees this module's.

    A reference page declaring a frame smaller than the one the run sealed its
    acts in makes `compare_page` refuse `region-outside-page` -- a real
    disagreement between the two sides, and a name outside
    `EVALUATION_REFUSAL_REASONS`. Round 2 item 11 settles that a delegated
    refusal travels under the caller's own name, with the delegate's text kept
    in the detail.
    """
    tree = sealed_run
    cramped = build_reference_page(
        page={"sha256": load_exemplar_page_shas(tree)[1], "width": 30, "height": 30},
        source="fixture",
        volume="synthetic-two-page-v0",
        designation="page-1-in-a-smaller-frame",
        split="val",
        records=[
            {
                "record_id": "r-cramped",
                "region": {"x": 1, "y": 1, "w": 10, "h": 10},
                "split": "val",
                "text": "un acte",
                "text_sha256": digest_bytes("un acte".encode("utf-8")),
            }
        ],
    )
    with pytest.raises(CorpusRefusal, match="^comparison-refused:") as refused:
        evaluate_run(tree, [cramped], code_ref="test")
    assert "region-outside-page" in str(refused.value)


def test_two_reference_pages_over_one_page_digest_are_refused(sealed_run):
    tree = sealed_run
    reference = _fixture_reference_for_page_one(tree)
    with pytest.raises(CorpusRefusal, match="^reference-page-collision:"):
        evaluate_run(tree, [reference, reference], code_ref="test")


def test_naming_a_ledger_is_a_check_not_a_caption(sealed_run):
    """`reference_ledger_verified` can only be true of something that is a ledger."""
    tree = sealed_run
    reference = _fixture_reference_for_page_one(tree)
    ledger = _ledger_for(reference)
    report = evaluate_run(tree, [reference], code_ref="test", reference_ledger=ledger)
    assert report["corpus"]["reference_ledger_verified"] is True
    assert report["corpus"]["reference_ledger_sha256"] == digest_bytes(canonical_bytes(ledger))

    # A ledger that carries other pages does not bless these ones.
    with pytest.raises(CorpusRefusal, match="^reference-page-not-in-ledger:"):
        evaluate_run(
            tree,
            [reference],
            code_ref="test",
            reference_ledger=_ledger_for(_page_never_sealed()),
        )
    # And a thing shaped like an answer is not a ledger.
    with pytest.raises(CorpusRefusal, match="^reference-ledger-invalid:"):
        evaluate_run(
            tree,
            [reference],
            code_ref="test",
            reference_ledger={"rows": [{"reference_page_self_hash": reference["self_hash"]}]},
        )


def test_a_reference_page_that_does_not_validate_is_refused_under_this_modules_name(sealed_run):
    """Round 2 item 11: a delegated refusal travels under the caller's vocabulary."""
    reference = json.loads(json.dumps(_fixture_reference_for_page_one(sealed_run)))
    reference["acts"][0]["text"] = " "
    with pytest.raises(CorpusRefusal, match="^reference-page-invalid:") as refused:
        evaluate_run(sealed_run, [reference], code_ref="test")
    assert "empty-normalized-text" in str(refused.value), "the delegate's own name is kept in view"


def test_the_command_line_refuses_a_missing_or_damaged_input_by_name(sealed_run, tmp_path):
    with pytest.raises(CorpusRefusal, match="^missing-input-file:"):
        load_reference_pages(tmp_path / "nowhere.jsonl")
    with pytest.raises(CorpusRefusal, match="^missing-input-file:"):
        load_reference_ledger(tmp_path / "nowhere.json")
    damaged = tmp_path / "pages.jsonl"
    damaged.write_bytes(b"\xff\xfe not utf-8 at all\n")
    with pytest.raises(CorpusRefusal, match="^malformed-record:"):
        load_reference_pages(damaged)
    not_a_ledger = tmp_path / "ledger.json"
    not_a_ledger.write_text(json.dumps({"schema": "something-else.v1"}))
    with pytest.raises(CorpusRefusal, match="^reference-ledger-invalid:"):
        load_reference_ledger(not_a_ledger)


def test_a_run_with_no_verified_export_is_refused_not_scored(sealed_run, tmp_path):
    reference = _fixture_reference_for_page_one(sealed_run)
    with pytest.raises(CorpusRefusal, match="^no-export:"):
        evaluate_run(RunTree(tmp_path / "nowhere", "r"), [reference], code_ref="test")


def test_an_evaluation_must_name_the_code_it_ran_under(sealed_run):
    reference = _fixture_reference_for_page_one(sealed_run)
    with pytest.raises(CorpusRefusal, match="^malformed-record:"):
        evaluate_run(sealed_run, [reference], code_ref="")


# --- The validator ----------------------------------------------------------------


def test_the_validator_refuses_a_report_edited_after_it_was_sealed(sealed_run):
    report = json.loads(
        json.dumps(
            evaluate_run(sealed_run, [_fixture_reference_for_page_one(sealed_run)], code_ref="test")
        )
    )
    assert validate_evaluation(dict(report))["self_hash"] == report["self_hash"]

    edited = json.loads(json.dumps(report))
    edited["denominators"]["reference_records_scored"] = 99
    with pytest.raises(CorpusRefusal, match="^self-hash-mismatch:"):
        validate_evaluation(edited)
    with pytest.raises(CorpusRefusal, match="^malformed-record:") as refused:
        validate_evaluation(_reseal(edited))
    assert "disagree with the denominators" in str(refused.value)


@pytest.mark.parametrize("value", [[1], {"delivered": True}, {"delivered": -1}, {"delivered": "1"}])
def test_the_validator_refuses_a_category_histogram_that_is_not_counts(sealed_run, value):
    """A list, a bool, a negative or a string is refused by name before it is summed."""
    report = json.loads(
        json.dumps(
            evaluate_run(sealed_run, [_fixture_reference_for_page_one(sealed_run)], code_ref="test")
        )
    )
    tampered = json.loads(json.dumps(report))
    tampered["denominators"]["reference_records_scored_by_export_category"] = value
    with pytest.raises(CorpusRefusal, match="^malformed-record:") as refused:
        validate_evaluation(_reseal(tampered))
    assert "reference_records_scored_by_export_category" in str(refused.value)


def test_the_validator_refuses_a_foreign_schema_and_a_label_that_does_not_match(sealed_run):
    report = json.loads(
        json.dumps(
            evaluate_run(sealed_run, [_fixture_reference_for_page_one(sealed_run)], code_ref="test")
        )
    )
    tampered = json.loads(json.dumps(report))
    tampered["schema"] = "something-else.v1"
    with pytest.raises(CorpusRefusal, match="^wrong-schema:"):
        validate_evaluation(_reseal(tampered))

    relabelled = json.loads(json.dumps(report))
    relabelled["fixture"] = False
    with pytest.raises(CorpusRefusal, match="^malformed-record:") as refused:
        validate_evaluation(_reseal(relabelled))
    assert "sealed identity" in str(refused.value)


def test_write_report_refuses_an_off_shape_record_before_it_reaches_disk(sealed_run, tmp_path):
    report = json.loads(
        json.dumps(
            evaluate_run(sealed_run, [_fixture_reference_for_page_one(sealed_run)], code_ref="test")
        )
    )
    report["extra"] = "a field the schema does not carry"
    with pytest.raises(CorpusRefusal, match="^malformed-record:"):
        write_report(report, tmp_path / "never-written.json")
    assert not (tmp_path / "never-written.json").exists()


# --- The command-line entry point ---------------------------------------------------


def test_the_command_line_scores_a_sealed_run_and_prints_its_summary(sealed_run, tmp_path, capsys):
    pages = tmp_path / "reference-pages.jsonl"
    pages.write_text(json.dumps(_fixture_reference_for_page_one(sealed_run)) + "\n")
    output = tmp_path / "evaluation.json"
    assert (
        main(
            [
                "--run-root",
                str(sealed_run.root.parent),
                "--run-id",
                "r",
                "--reference-pages",
                str(pages),
                "--code-ref",
                "test",
                "--output",
                str(output),
            ]
        )
        == 0
    )
    written = validate_evaluation(json.loads(output.read_bytes()))
    assert written["fixture"] is True
    assert "fixture result" in capsys.readouterr().out


def test_every_declared_evaluation_reason_is_exercised_here():
    """Derived from this file's own anchored assertions, never hand-typed."""
    source = Path(__file__).read_text(encoding="utf-8")
    exercised = set(re.findall(r'pytest\.raises\(CorpusRefusal, match="\^([a-z0-9-]+):"\)', source))
    missing = EVALUATION_REFUSAL_REASONS - exercised
    assert missing == set(), f"declared but never shown to fire: {sorted(missing)}"
