"""R5b Pass-C proof: one frozen page pass, neutral re-proof, and review routing."""

from __future__ import annotations

import copy
import importlib.util
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import audit
import pytest

from common.contracts.errors import ContractError, SchemaRefusal
from common.contracts.stages import PERLECTOR
from common.runtree.store import RunTree

ROOT = Path(__file__).resolve().parents[2]
ORCHESTRATOR = ROOT / "pipeline" / "orchestrator" / "run.py"


def _recensor():
    spec = importlib.util.spec_from_file_location(
        "r5b_recensor_consumer", ROOT / "pipeline" / "5_recensor" / "run.py"
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _perlector():
    spec = importlib.util.spec_from_file_location(
        "r5b_perlector_schema", ROOT / "pipeline" / "4_perlector" / "run.py"
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _run(root: Path, *extra: str, scenario: str = "happy"):
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


def _records(tree: RunTree, kind: str) -> list[dict]:
    return [
        tree.read_artifact(PERLECTOR, kind, entry["artifact_id"])
        for entry in tree.build_manifest(PERLECTOR)["artifacts"]
        if entry["kind"] == kind
    ]


def test_fixture_produces_each_audit_kind_and_records_unchanged_reproof(tmp_path):
    result = _run(tmp_path / "runs")
    assert result.returncode == 0, result.stderr
    tree = RunTree(tmp_path / "runs", "r")
    drafts = _records(tree, "audit-draft")
    findings = _records(tree, "audit-finding")
    finals = _records(tree, "perlectio")
    assert len(drafts) == len(findings) == len(finals) == 2
    assert all(record["payload"]["flags"] for record in drafts)
    assert all(record["payload"]["change_record"] == [] for record in findings)
    assert all(record["payload"]["unresolved"] is False for record in findings)
    for final in finals:
        for reproof in final["payload"]["audit"]["reproofs"]:
            prompt = reproof["prompt"].lower()
            assert "wrong" not in prompt
            assert "expected" not in prompt
            assert "confirmed unchanged" in prompt


def test_fixture_exercises_a_changed_reproof_with_its_triggering_flag_class(tmp_path):
    result = _run(tmp_path / "runs", scenario="audit-change")
    assert result.returncode == 0, result.stderr
    tree = RunTree(tmp_path / "runs", "r")
    changed = next(
        record for record in _records(tree, "audit-finding") if record["payload"]["change_record"]
    )
    assert changed["payload"]["change_record"][0]["triggering_flag_class"] == "testimony-diff"


def test_perlectio_schema_refuses_a_directional_reproof_prompt(tmp_path):
    result = _run(tmp_path / "runs")
    assert result.returncode == 0, result.stderr
    final = _records(RunTree(tmp_path / "runs", "r"), "perlectio")[0]
    payload = copy.deepcopy(final["payload"])
    payload["audit"]["reproofs"][0]["prompt"] = "The reading is wrong; replace it with gamma."
    with pytest.raises(SchemaRefusal, match="neutral location-only"):
        _perlector().validate_reading_payload(
            payload, outcome="read", fields=_perlector()._PERLECTIO_FIELDS
        )


def test_flags_are_frozen_once_per_page_and_never_cascade_from_a_reproof():
    frozen = [
        {
            "act_id": "a1",
            "page_id": "p1",
            "order": 0,
            "geometry_order": 0,
            "text": "No 2 1689 alpha",
            "testimonia": ["No 2 1689 beta"],
            "within_crop": True,
        },
        {
            "act_id": "a2",
            "page_id": "p1",
            "order": 1,
            "geometry_order": 1,
            "text": "No 1 1688 gamma",
            "testimonia": ["No 1 1688 gamma"],
            "within_crop": True,
        },
    ]
    flags = audit.flags_once_per_page(frozen)
    assert {flag["class"] for flag in flags["a2"]} == {"date-sequence", "numbering"}
    # A hypothetical re-proof result modifies a1 only.  The already-frozen a2
    # locations are the audit plan; no call recomputes them over changed text.
    changed = copy.deepcopy(frozen)
    changed[0]["text"] = "No 2 1600 beta"
    assert flags["a2"] == audit.flags_once_per_page(frozen)["a2"]
    assert audit.flags_once_per_page(changed)["a2"] != flags["a2"]


def test_raised_cap_needs_tyrels_reference_and_exhaustion_routes_review(tmp_path):
    raised = tmp_path / "raised.toml"
    raised.write_text(
        'schema = "perlector-audit.v1"\ndefault_round_cap = 1\nabsolute_round_cap = 2\nround_cap = 2\napproval_ref = ""\n'
    )
    with pytest.raises(ContractError, match="Tyrel's approval reference"):
        audit.load(raised)

    exhausted = tmp_path / "exhausted.toml"
    exhausted.write_text(
        'schema = "perlector-audit.v1"\ndefault_round_cap = 1\nabsolute_round_cap = 2\nround_cap = 0\napproval_ref = ""\n'
    )
    result = _run(tmp_path / "exhausted-runs", "--perlector-audit-config", str(exhausted))
    assert result.returncode == 3, result.stderr
    tree = RunTree(tmp_path / "exhausted-runs", "r")
    assert all(record["payload"]["unresolved"] for record in _records(tree, "audit-finding"))


def test_recensor_refuses_a_forged_audit_reference(tmp_path):
    result = _run(tmp_path / "runs")
    assert result.returncode == 0, result.stderr
    tree = RunTree(tmp_path / "runs", "r")
    final = _records(tree, "perlectio")[0]
    forged = copy.deepcopy(final)
    forged["payload"]["audit"]["finding_ref"] = forged["payload"]["audit"]["draft_ref"]

    with pytest.raises(SchemaRefusal, match="not required 'perlector'/'audit-finding'"):
        _recensor().audit_state(SimpleNamespace(tree=tree), forged, final["subject_id"])
