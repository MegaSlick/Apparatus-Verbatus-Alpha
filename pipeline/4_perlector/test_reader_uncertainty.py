"""The reader's own doubt report, carried faithfully from the call to the export.

Independent audit of 2026-09-10, finding F2: the live reader returned text and a
stop word and nothing else, the only spans ever minted were the exhausted-cap
projection under a zero cap, and an empty `uncertain_spans` list therefore read
the same whether a reader had assessed its doubts or had no channel to. These
tests drive the real Perlector, Recensor, Archetypus and Armarium over fixture
scenarios that declare a doubt report, and assert the exact spans and the exact
text separately -- never only that producer and validator agree on a boolean.
"""

from __future__ import annotations

import dataclasses
import importlib.util
import io
import json
import subprocess
import sys
import zipfile
from pathlib import Path

import annotations
import pytest

from common.contracts.errors import SchemaRefusal
from common.contracts.stages import ARCHETYPUS, ARMARIUM, PERLECTOR, RECENSOR
from common.contracts.uncertainty import from_perlectio
from common.runtree.store import RunTree
from operations.operator import review_text
from operations.operator.review import ReadOnlyRun

ROOT = Path(__file__).resolve().parents[2]
ORCHESTRATOR = ROOT / "pipeline" / "orchestrator" / "run.py"

A1_TEXT = "SYNTHETIC ACT ONE alpha beta gamma"
A1_DOUBT = {"start": 29, "end": 34, "alternatives": ["gamna", "gaMma"], "confidence": "low"}
A1_GAP = {"position": "internal", "start": 23, "end": 23, "witness_evidence": []}


def _perlector():
    spec = importlib.util.spec_from_file_location(
        "reader_uncertainty_perlector", ROOT / "pipeline" / "4_perlector" / "run.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _run(root: Path, scenario: str, *extra: str) -> subprocess.CompletedProcess[str]:
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
            str(root),
            *extra,
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
    )


def _records(tree: RunTree, stage: str, kind: str) -> dict[str, dict]:
    return {
        record["payload"]["act_key"]: record
        for record in (
            tree.read_artifact(stage, kind, entry["artifact_id"])
            for entry in tree.build_manifest(stage)["artifacts"]
            if entry["kind"] == kind
        )
    }


def _export(tree: RunTree) -> tuple[dict, dict, list[dict]]:
    (export,) = [
        tree.read_artifact(ARMARIUM, "export", entry["artifact_id"])
        for entry in tree.build_manifest(ARMARIUM)["artifacts"]
        if entry["kind"] == "export"
    ]
    payload = export["payload"]
    with zipfile.ZipFile(
        io.BytesIO(tree.read_bytes(payload["bundle"]["reference"]["relative_path"]))
    ) as z:
        manifest = json.loads(z.read("EXPORT_MANIFEST.json"))
        rows = [json.loads(line) for line in z.read("acts.jsonl").splitlines()]
    return payload, manifest, rows


# --- The assessment record, anchored to the exact text -----------------------


def test_a_reader_with_no_assessment_key_is_read_as_not_assessed_never_confident():
    perlector = _perlector()
    record = perlector._assessed({"text": "alpha", "stop_reason": "stop"}, text="alpha")
    assert record["state"] == "not-assessed"
    assert record["uncertain_spans"] == [] and record["gaps"] == []
    assert record["problem"] == annotations.NOT_ASSESSED_REASON


def test_unicode_offsets_anchor_by_code_point_and_a_repeated_word_by_position():
    perlector = _perlector()
    text = "né le dix août né à Rouen"  # 'né' twice; accents are one code point each
    second = text.index("né", 1)
    report = {
        "state": "assessed",
        "uncertain_spans": [
            {"start": second, "end": second + 2, "alternatives": ["ne"], "confidence": "medium"}
        ],
        "gaps": [{"position": "internal", "start": 3, "end": 3, "witness_evidence": []}],
        "problem": None,
    }
    record = perlector._assessed({"text": text, "assessment": report}, text=text)
    assert record["state"] == "assessed"
    (span,) = record["uncertain_spans"]
    assert text[span["start"] : span["end"]] == "né" and span["start"] == second
    assert text.encode("utf-8")[span["start"] : span["end"]] != b"n\xc3\xa9", (
        "offsets are code points, so the byte slice at the same numbers is a different string"
    )


@pytest.mark.parametrize(
    ("report", "problem_fragment"),
    [
        (
            {
                "state": "assessed",
                "uncertain_spans": [
                    {"start": 0, "end": 99, "alternatives": [], "confidence": "low"}
                ],
                "gaps": [],
                "problem": None,
            },
            "outside text bounds",
        ),
        (
            {
                "state": "assessed",
                "uncertain_spans": [],
                "gaps": [{"position": "internal", "start": 1, "end": 3, "witness_evidence": []}],
                "problem": None,
            },
            "claims characters",
        ),
        (
            {
                "state": "assessed",
                "uncertain_spans": [],
                "gaps": [{"position": "whole-act", "start": 0, "end": 0, "witness_evidence": []}],
                "problem": None,
            },
            "cannot be whole-act",
        ),
        (
            {
                "state": "assessed",
                "uncertain_spans": [
                    {"start": 0, "end": 2, "alternatives": [], "confidence": "certain"}
                ],
                "gaps": [],
                "problem": None,
            },
            "confidence",
        ),
        ({"state": "sure", "uncertain_spans": [], "gaps": [], "problem": None}, "unknown state"),
        ({"spans": []}, "closed record"),
        (
            {"state": "assessed", "uncertain_spans": [], "gaps": [], "problem": "but"},
            "carries no problem",
        ),
        (
            {"state": "not-assessed", "uncertain_spans": [], "gaps": [], "problem": None},
            "must say why",
        ),
    ],
)
def test_a_report_the_schema_cannot_anchor_becomes_a_visible_malformed_record(
    report, problem_fragment
):
    """Refused reports are retained as faults with empty layers, never as confidence."""
    perlector = _perlector()
    record = perlector._assessed({"text": "alpha beta", "assessment": report}, text="alpha beta")
    assert record["state"] == "malformed"
    assert record["uncertain_spans"] == [] and record["gaps"] == []
    assert problem_fragment in record["problem"]
    with pytest.raises(SchemaRefusal):
        annotations.validate_assessment(report, "alpha beta")


def test_the_canonical_layer_refuses_a_perlectio_sealed_before_the_assessment_existed():
    payload = {
        "text": "alpha",
        "uncertain_spans": [],
        "gaps": [],
        "self_revision": [],
    }
    with pytest.raises(SchemaRefusal, match="carries no uncertainty_assessment"):
        from_perlectio(payload)
    payload["uncertainty_assessment"] = {"state": "assessed", "problem": None}
    assert from_perlectio(payload)["assessment"] == {"state": "assessed", "problem": None}
    payload["uncertainty_assessment"] = {"state": "assessed", "problem": "odd"}
    with pytest.raises(SchemaRefusal, match="exactly when it is not assessed"):
        from_perlectio(payload)


# --- Through the real stages ---------------------------------------------------


def test_a_declared_doubt_arrives_intact_beside_the_same_text_in_review_and_export(tmp_path):
    root = tmp_path / "runs"
    result = _run(root, "reader-doubt")
    assert result.returncode == 3, result.stderr
    assert "delivered with partial text" in result.stdout
    tree = RunTree(root, "r")

    readings = _records(tree, PERLECTOR, "perlectio")
    a1, a2 = readings["a1"]["payload"], readings["a2"]["payload"]
    # Exact spans and exact text, asserted separately.
    assert a1["text"] == A1_TEXT
    assert a1["uncertain_spans"] == [A1_DOUBT]
    assert a1["gaps"] == [A1_GAP]
    assert a1["text"][A1_DOUBT["start"] : A1_DOUBT["end"]] == "gamma"
    assert a1["uncertainty_assessment"] == {"state": "assessed", "problem": None}
    assert a1["audit"]["examination"] == "complete"
    # The act that was assessed and reported nothing: an honest empty list.
    assert a2["uncertain_spans"] == [] and a2["gaps"] == []
    assert a2["uncertainty_assessment"] == {"state": "assessed", "problem": None}

    reviews = _records(tree, RECENSOR, "review")
    assert reviews["a1"]["outcome"] == "accepted", "a doubt is displayed, never a hold"
    assert reviews["a1"]["payload"]["uncertainty_assessment"] == "assessed"
    assert reviews["a2"]["payload"]["uncertainty_assessment"] == "assessed"

    established = _records(tree, ARCHETYPUS, "archetypus")
    assert established["a1"]["payload"]["text"] == A1_TEXT
    assert established["a1"]["payload"]["text_status"] == "partial", "an unread gap is partial"
    assert established["a1"]["payload"]["uncertainty"]["uncertain_spans"] == [A1_DOUBT]
    assert established["a1"]["payload"]["uncertainty"]["assessment"]["state"] == "assessed"
    assert established["a2"]["payload"]["text_status"] == "established"

    export, manifest, rows = _export(tree)
    assert export["aggregate"]["status"] == "partial"
    by_key = {act["act_key"]: act for act in export["delivered"]}
    assert by_key["a1"]["text"] == A1_TEXT
    assert by_key["a1"]["uncertainty"]["uncertain_spans"] == [A1_DOUBT]
    assert by_key["a1"]["uncertainty"]["gaps"] == [A1_GAP]
    assert by_key["a1"]["uncertainty"]["assessment"] == {"state": "assessed", "problem": None}
    assert by_key["a2"]["uncertainty"]["uncertain_spans"] == []
    # Every literal-text format carries the same layer beside the same text.
    (jsonl_a1,) = [row for row in rows if row["act_key"] == "a1"]
    assert jsonl_a1["canonical_clean_text"] == A1_TEXT
    assert jsonl_a1["uncertainty"]["uncertain_spans"] == [A1_DOUBT]
    assert jsonl_a1["uncertainty"]["assessment"]["state"] == "assessed"
    (instrument,) = [
        entry
        for entry in manifest["claims"]["not_measured"]["entries"]
        if entry["instrument"] == "perlector-uncertain-spans"
    ]
    assert instrument["status"] == "measured"
    assert instrument["detail"]["acts_assessed"] == 2
    assert instrument["detail"]["acts_not_assessed"] == 0
    assert instrument["detail"]["acts_with_uncertain_spans"] == 1

    projection = dataclasses.asdict(ReadOnlyRun(root, "r").projection())
    reading = next(act for act in projection["acts"] if act["act_key"] == "a1")["row"]
    assert reading is not None  # the export view carries the export row
    text = "\n".join(review_text.render(projection))
    assert "doubts: assessed by the reader; 1 uncertain span(s), 1 gap(s)" in text
    assert "[29, 34) 'gamma' confidence low; alternatives: gamna, gaMma" in text
    assert "gap (internal) at 23" in text


def test_a_chair_without_a_doubt_channel_is_disclosed_as_unproduced_not_confident(tmp_path):
    root = tmp_path / "runs"
    assert _run(root, "happy").returncode == 0
    tree = RunTree(root, "r")
    for reading in _records(tree, PERLECTOR, "perlectio").values():
        assert reading["payload"]["uncertainty_assessment"]["state"] == "not-assessed"
        assert reading["payload"]["uncertain_spans"] == []
    export, manifest, _rows = _export(tree)
    assert export["aggregate"]["status"] == "complete"
    for act in export["delivered"]:
        assert act["uncertainty"]["assessment"]["state"] == "not-assessed"
        assert "no channel" in act["uncertainty"]["assessment"]["problem"]
    (instrument,) = [
        entry
        for entry in manifest["claims"]["not_measured"]["entries"]
        if entry["instrument"] == "perlector-uncertain-spans"
    ]
    assert instrument["status"] == "declared-unproduced"
    assert instrument["detail"] == {
        "sealed_audit_round_cap": 1,
        "acts_delivered": 2,
        "acts_with_uncertain_spans": 0,
        "acts_assessed": 0,
        "acts_not_assessed": 2,
    }


def test_a_malformed_report_holds_the_act_with_the_problem_retained_and_no_empty_confidence(
    tmp_path,
):
    root = tmp_path / "runs"
    result = _run(root, "reader-doubt-malformed")
    assert result.returncode == 3, result.stderr
    tree = RunTree(root, "r")
    a1 = _records(tree, PERLECTOR, "perlectio")["a1"]["payload"]
    assert a1["outcome" if "outcome" in a1 else "text"] is not None
    assert a1["text"] == A1_TEXT, "the reading itself stands"
    assert a1["uncertain_spans"] == [] and a1["gaps"] == []
    assert a1["uncertainty_assessment"]["state"] == "malformed"
    assert "outside text bounds (0..34)" in a1["uncertainty_assessment"]["problem"]
    review = _records(tree, RECENSOR, "review")["a1"]
    assert review["outcome"] == "held-for-review"
    assert review["payload"]["uncertainty_assessment"] == "malformed"
    assert "doubt report over this act could not be anchored" in review["payload"]["reason"]
    export, manifest, rows = _export(tree)
    assert [act["act_key"] for act in export["delivered"]] == ["a2"]
    (held,) = export["non_delivered"]
    assert held["act_key"] == "a1" and held["category"] == "held-for-review"
    assert (jsonl_a1 := next(row for row in rows if row["act_key"] == "a1"))["uncertainty"] is None
    assert jsonl_a1["canonical_clean_text"] is None
    text = "\n".join(review_text.render(dataclasses.asdict(ReadOnlyRun(root, "r").projection())))
    assert "could not be anchored" in text


def test_a_reproof_that_changes_the_text_publishes_its_own_doubts_never_a_reanchored_copy(
    tmp_path,
):
    """audit-change: Pass B doubts "gamma" at [29,34); the re-proof publishes "gamma!" and
    its own doubt at [29,35). Only the re-proof's report may travel with its text."""
    root = tmp_path / "runs"
    assert _run(root, "audit-change").returncode == 0
    tree = RunTree(root, "r")
    a1 = _records(tree, PERLECTOR, "perlectio")["a1"]["payload"]
    assert a1["text"] == "SYNTHETIC ACT ONE alpha beta gamma!"
    assert a1["uncertain_spans"] == [
        {"start": 29, "end": 35, "alternatives": ["gamma"], "confidence": "medium"}
    ]
    assert a1["text"][29:35] == "gamma!"
    assert a1["uncertainty_assessment"] == {"state": "assessed", "problem": None}
    export, _manifest, rows = _export(tree)
    (row,) = [row for row in rows if row["act_key"] == "a1"]
    assert row["canonical_clean_text"] == a1["text"]
    assert row["uncertainty"]["uncertain_spans"] == a1["uncertain_spans"]


def test_a_cut_off_reproof_keeps_pass_bs_doubtless_assessment_with_pass_bs_text(tmp_path):
    """The assessment travels with the call whose text is published (F1 meets F2)."""
    root = tmp_path / "runs"
    assert _run(root, "audit-reproof-cutoff").returncode == 3
    a1 = _records(RunTree(root, "r"), PERLECTOR, "perlectio")["a1"]["payload"]
    assert a1["uncertainty_assessment"]["state"] == "not-assessed"
    assert a1["audit"]["examination"] == "incomplete"
    assert a1["uncertain_spans"] == []
