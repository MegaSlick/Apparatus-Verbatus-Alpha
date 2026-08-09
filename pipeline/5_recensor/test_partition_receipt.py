"""The Recensor's scoped partition receipt is rebuilt from real stage artifacts."""

from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from common.contracts.errors import FatalAccounting, SchemaRefusal
from common.contracts.stages import RECENSOR
from common.runtree.store import RunTree

ROOT = Path(__file__).resolve().parents[2]


def invoke(
    root: Path, run_id: str, scenario: str, program: str, **extra
) -> subprocess.CompletedProcess:
    command = [
        sys.executable,
        str(ROOT / program),
        "--run-root",
        str(root),
        "--run-id",
        run_id,
        "--scenario",
        scenario,
    ]
    for key, value in extra.items():
        command.extend((f"--{key.replace('_', '-')}", str(value)))
    return subprocess.run(command, cwd=ROOT, capture_output=True, text=True)


def through_perlector(root: Path, run_id: str, scenario: str) -> None:
    for program in (
        "pipeline/1_exemplar/door.py",
        "pipeline/1_exemplar/run.py",
        "pipeline/2_designator/run.py",
        "pipeline/3_attestatores/run.py",
        "pipeline/4_perlector/run.py",
    ):
        result = invoke(root, run_id, scenario, program)
        assert result.returncode == 0, f"{program}: {result.stderr}"


def _load_recensor():
    path = ROOT / "pipeline/5_recensor/run.py"
    spec = importlib.util.spec_from_file_location("recensor_receipt_under_test", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_happy_recensor_pass_writes_a_complete_scoped_partition_receipt(tmp_path):
    root = tmp_path / "runs"
    through_perlector(root, "happy", "happy")
    result = invoke(root, "happy", "happy", "pipeline/5_recensor/run.py")
    assert result.returncode == 0, result.stderr

    receipt = RunTree(root, "happy").read_recensor_partition_receipt()
    assert receipt["scope"] == "proposal-acts-and-configured-witnesses"
    assert receipt["expected_act_count"] == 2
    assert receipt["recensor_status"] == "complete"
    assert receipt["by_partition_class"] == {"completed": 2, "unresolved": 0, "failed": 0}
    assert len({item["act_id"] for item in receipt["items"]}) == 2
    assert all(item["review_outcome"] == "accepted" for item in receipt["items"])


def test_recovery_replaces_the_current_partition_snapshot_without_erasing_history(tmp_path):
    root = tmp_path / "runs"
    through_perlector(root, "review", "review")
    first = invoke(root, "review", "review", "pipeline/5_recensor/run.py")
    assert first.returncode == 3, first.stderr
    tree = RunTree(root, "review")
    before = tree.read_recensor_partition_receipt()
    requested = next(item for item in before["items"] if item["act_key"] == "a1")
    assert before["recensor_status"] == "partial"
    assert requested["review_outcome"] == "recovery-requested"
    request = next(
        tree.read_artifact(RECENSOR, "recovery-request", entry["artifact_id"])
        for entry in tree.build_manifest(RECENSOR)["artifacts"]
        if entry["kind"] == "recovery-request" and entry["subject_id"] == requested["act_id"]
    )

    recrop = invoke(
        root,
        "review",
        "review",
        "pipeline/2_designator/run.py",
        operation="recover",
        act=requested["act_id"],
        recovery_request=request["artifact_id"],
    )
    assert recrop.returncode == 0, recrop.stderr
    reread = invoke(
        root, "review", "review", "pipeline/4_perlector/run.py", act=requested["act_id"]
    )
    assert reread.returncode == 0, reread.stderr
    second = invoke(root, "review", "review", "pipeline/5_recensor/run.py")
    assert second.returncode == 3, second.stderr

    after = tree.read_recensor_partition_receipt()
    accepted = next(item for item in after["items"] if item["act_key"] == "a1")
    assert accepted["review_outcome"] == "accepted"
    assert accepted["review_ref"] != requested["review_ref"]
    assert before["self_hash"] != after["self_hash"]


def test_a_tampered_stored_manifest_cannot_become_a_partition_receipt_denominator(tmp_path):
    root = tmp_path / "runs"
    through_perlector(root, "manifest", "happy")
    assert invoke(root, "manifest", "happy", "pipeline/5_recensor/run.py").returncode == 0
    tree = RunTree(root, "manifest")
    tree.resolve(tree.manifest_path(RECENSOR)).write_text("{}", encoding="utf-8")
    recensor = _load_recensor()
    args = SimpleNamespace(
        run_root=root,
        run_id="manifest",
        scenario="happy",
        fixture_root=str(ROOT / "proof"),
        models_config=str(ROOT / "config/models.toml"),
        pdf_render_config=str(ROOT / "config/pdf_render.toml"),
        pdf_target_dpi=None,
        recovery_config=str(ROOT / "config/recovery.toml"),
        hard_failure_config=str(ROOT / "config/hard_failure.toml"),
    )
    context = recensor.open_context(args, RECENSOR)

    with pytest.raises(FatalAccounting, match="manifest disagrees"):
        recensor.write_partition_receipt(context, recensor.recovery_budget(args.recovery_config))


def test_a_tampered_partition_receipt_is_refused_by_its_self_hash(tmp_path):
    root = tmp_path / "runs"
    through_perlector(root, "tampered", "happy")
    assert invoke(root, "tampered", "happy", "pipeline/5_recensor/run.py").returncode == 0
    tree = RunTree(root, "tampered")
    path = tree.resolve(tree.recensor_partition_receipt_path())
    record = json.loads(path.read_text(encoding="utf-8"))
    record["self_hash"] = "0" * 64
    path.write_text(json.dumps(record), encoding="utf-8")

    with pytest.raises(SchemaRefusal, match="self-hash"):
        tree.read_recensor_partition_receipt()


def test_a_run_that_proposed_no_acts_gets_a_visibly_partial_receipt_not_a_refusal():
    """An empty denominator is a fact about the run, not a malformed receipt.

    The Designator proposing nothing at all is the silent-failure shape this whole
    pipeline exists to catch (GOALS 1), and the Armarium's own aggregate already
    treats a sealed page nobody marked out as a named partial rather than an
    error. Refusing to build the receipt would have turned that into a traceback
    at the one boundary whose job is making it visible -- neither lane noticed,
    because no shipped scenario produces a run with zero expected acts.
    """
    from common.recensor_receipt import (
        EMPTY_DENOMINATOR_REASON,
        build_recensor_partition_receipt,
        validate_recensor_partition_receipt,
    )

    receipt = build_recensor_partition_receipt(
        run_id="r",
        config_digest="a" * 64,
        proposal_seal_ref={
            "relative_path": "2_designator/artifacts/proposal-seal.json",
            "sha256": "b" * 64,
        },
        items=[],
    )
    assert receipt["expected_act_count"] == 0
    assert receipt["recensor_status"] == "partial"
    assert receipt["reasons"] == [EMPTY_DENOMINATOR_REASON]
    assert validate_recensor_partition_receipt(receipt) == receipt


def test_an_empty_receipt_may_not_claim_to_be_complete():
    """The status derives from the reasons, and the reason is not optional."""
    from common.contracts.canonical import self_hash
    from common.recensor_receipt import (
        build_recensor_partition_receipt,
        validate_recensor_partition_receipt,
    )

    receipt = build_recensor_partition_receipt(
        run_id="r",
        config_digest="a" * 64,
        proposal_seal_ref={
            "relative_path": "2_designator/artifacts/proposal-seal.json",
            "sha256": "b" * 64,
        },
        items=[],
    )
    forged = dict(receipt, recensor_status="complete", reasons=[])
    forged["self_hash"] = self_hash({k: v for k, v in forged.items() if k != "self_hash"})
    with pytest.raises(SchemaRefusal, match="does not derive from its items"):
        validate_recensor_partition_receipt(forged)
