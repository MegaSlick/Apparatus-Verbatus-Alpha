"""R5a prior-draft protocol: distinct passes, refusal gate, and fixture signal."""

import subprocess
import sys
from pathlib import Path

import protocol
import pytest

from common.contracts.errors import SchemaRefusal
from common.contracts.identities import perlector_attempt_id
from common.contracts.stages import PERLECTOR
from common.runtree.store import RunTree

ROOT = Path(__file__).resolve().parents[2]
ORCHESTRATOR = ROOT / "pipeline" / "orchestrator" / "run.py"


def _run(root, run_id="r", scenario="happy", *extra):
    return subprocess.run(
        [
            sys.executable,
            str(ORCHESTRATOR),
            "--fixture",
            "synthetic-two-page-v0",
            "--scenario",
            scenario,
            "--run-id",
            run_id,
            "--run-root",
            str(root),
            *extra,
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
    )


def _records(tree, kind):
    return [
        tree.read_artifact(PERLECTOR, kind, entry["artifact_id"])
        for entry in tree.build_manifest(PERLECTOR)["artifacts"]
        if entry["kind"] == kind
    ]


def test_sampled_triple_has_all_three_records_and_nuda_stays_unfed(tmp_path):
    root = tmp_path / "runs"
    result = _run(
        root,
        "r",
        "happy",
        "--nuda-per-mille",
        "1000",
        "--nuda-approval-ref",
        "fixture/nuda",
        "--perlector-instrument-per-mille",
        "1000",
        "--perlector-instrument-approval-ref",
        "fixture/prior",
    )
    assert result.returncode == 0, result.stderr
    tree = RunTree(root, "r")
    assert len(_records(tree, "lectio-prior")) == 2
    assert len(_records(tree, "primed-without-prior")) == 2
    finals = _records(tree, "perlectio")
    assert len(finals) == 2
    assert any(record["payload"]["self_revision"] for record in finals)
    assert any(not record["payload"]["self_revision"] for record in finals)
    for record in _records(tree, "lectio-nuda"):
        dossier = record["payload"]["dossier"]
        assert dossier["testimonia"] == []
        assert "prior_draft" not in dossier
        assert "act_attachment" not in dossier


def test_unsampled_run_has_prior_and_production_but_no_control(tmp_path):
    root = tmp_path / "runs"
    result = _run(root)
    assert result.returncode == 0, result.stderr
    tree = RunTree(root, "r")
    assert len(_records(tree, "lectio-prior")) == len(_records(tree, "perlectio")) == 2
    assert _records(tree, "primed-without-prior") == []


def test_control_refuses_without_tyrels_approval_on_fixture_path(tmp_path):
    root = tmp_path / "runs"
    result = _run(root, "r", "happy", "--perlector-instrument-per-mille", "1")
    assert result.returncode != 0
    assert "unapproved instrument sample" in result.stderr
    assert not (root / "r").exists()


def test_draft_fed_toggle_records_both_states_and_withholds_prompt_text(tmp_path):
    root = tmp_path / "runs"
    result = _run(root, "r", "happy", "--no-draft-fed")
    assert result.returncode == 0, result.stderr
    final = _records(RunTree(root, "r"), "perlectio")[0]["payload"]
    assert final["protocol"]["draft_fed"] is False
    assert final["dossier"]["prior_draft_view"] == "withheld"


def test_control_selection_is_run_id_independent():
    facts = {"frame_digest": "a" * 64, "page_digest": "b" * 64, "seed": "c" * 64}
    first = protocol.is_control_sampled("act-1", per_mille=500, **facts)
    second = protocol.is_control_sampled("act-1", per_mille=500, **facts)
    assert first == second


def test_each_prior_protocol_pass_has_a_distinct_closed_attempt_operation():
    ids = {
        perlector_attempt_id("act", operation, 1)
        for operation in ("lectio-prior", "primed-without-prior", "perlegere")
    }
    assert len(ids) == 3
    with pytest.raises(ValueError, match="unknown Perlector reading operation"):
        perlector_attempt_id("act", "prior", 1)


def test_core_fixture_declarations_exercise_departure_and_equality_both_ways():
    import tomllib

    fixture = tomllib.loads((ROOT / "proof" / "skeleton_fixture.toml").read_text())
    final = {act["key"]: act["text"] for act in fixture["act"]}
    for scenario in ("happy", "review"):
        priors = {
            row["act_key"]: row["text"]
            for row in fixture["prior_reading"]
            if row["scenario"] == scenario
        }
        assert any(priors[key] != final[key] for key in final)
        assert any(priors[key] == final[key] for key in final)


def test_a_control_reference_forged_as_a_perlectio_is_refused(tmp_path):
    root = tmp_path / "runs"
    result = _run(
        root,
        "r",
        "happy",
        "--perlector-instrument-per-mille",
        "1000",
        "--perlector-instrument-approval-ref",
        "fixture/prior",
    )
    assert result.returncode == 0, result.stderr
    tree = RunTree(root, "r")
    control = _records(tree, "primed-without-prior")[0]
    entry = next(
        item
        for item in tree.build_manifest(PERLECTOR)["artifacts"]
        if item["artifact_id"] == control["artifact_id"]
    )
    with pytest.raises(SchemaRefusal, match="not required 'perlector'/'perlectio'"):
        tree.read_artifact_reference(
            {"relative_path": entry["relative_path"], "sha256": entry["sha256"]},
            stage=PERLECTOR,
            kind="perlectio",
            subject_id=control["subject_id"],
        )
