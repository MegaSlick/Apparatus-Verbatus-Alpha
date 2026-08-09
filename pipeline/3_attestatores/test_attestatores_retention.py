"""Spec 07 retention and native-Testimonium tests over the real stage program."""

import copy
import importlib.util
import subprocess
import sys
from pathlib import Path

import pytest

from common.contracts.canonical import canonical_bytes, self_hash
from common.contracts.errors import IncompatibleReuse
from common.contracts.stages import ATTESTATORES, DESIGNATOR
from common.runtree.store import RunTree
from common.stage import latest_per_chair

ROOT = Path(__file__).resolve().parents[2]


def _load_attestatores():
    path = Path(__file__).resolve().parent / "run.py"
    spec = importlib.util.spec_from_file_location("attestatores_retention_under_test", path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


attestatores = _load_attestatores()


def invoke_stage(
    run_root: Path,
    run_id: str,
    scenario: str,
    program: str,
    *,
    fixture_root: Path = ROOT / "proof",
    **extra,
) -> subprocess.CompletedProcess:
    command = [
        sys.executable,
        str(ROOT / program),
        "--run-root",
        str(run_root),
        "--run-id",
        run_id,
        "--scenario",
        scenario,
        "--fixture-root",
        str(fixture_root),
    ]
    for key, value in extra.items():
        command.extend((f"--{key.replace('_', '-')}", str(value)))
    return subprocess.run(command, cwd=ROOT, capture_output=True, text=True)


def run_to_designator(
    tmp_path: Path, scenario: str, *, fixture_root: Path = ROOT / "proof"
) -> tuple[Path, RunTree]:
    run_root = tmp_path / "runs"
    for program in (
        "pipeline/1_exemplar/door.py",
        "pipeline/1_exemplar/run.py",
        "pipeline/2_designator/run.py",
    ):
        result = invoke_stage(run_root, "retention", scenario, program, fixture_root=fixture_root)
        assert result.returncode == 0, f"{program}: {result.stderr}"
    return run_root, RunTree(run_root, "retention")


def _testimonia(tree: RunTree) -> list[dict]:
    return [
        tree.read_artifact(ATTESTATORES, "testimonium", entry["artifact_id"])
        for entry in tree.build_manifest(ATTESTATORES)["artifacts"]
        if entry["kind"] == "testimonium"
    ]


def _testimonium_for(tree: RunTree, *, act_key: str, chair: str, ordinal: int) -> dict:
    return next(
        record
        for record in _testimonia(tree)
        if record["payload"]["act_key"] == act_key
        and record["payload"]["chair"] == chair
        and record["payload"]["attempt_ordinal"] == ordinal
    )


def test_reread_appends_and_current_keeps_the_new_failed_outcome(tmp_path):
    run_root, tree = run_to_designator(tmp_path, "reread-failure")
    first_result = invoke_stage(
        run_root,
        "retention",
        "reread-failure",
        "pipeline/3_attestatores/run.py",
        attempt_ordinal=1,
    )
    assert first_result.returncode == 0, first_result.stderr
    first = _testimonium_for(tree, act_key="a1", chair="attestator_3", ordinal=1)
    first_path = tree.resolve(tree.artifact_path(ATTESTATORES, "testimonium", first["artifact_id"]))
    first_bytes = first_path.read_bytes()

    # A same-ordinal resume is exact-byte reuse, not an overwrite path.
    resumed = invoke_stage(
        run_root,
        "retention",
        "reread-failure",
        "pipeline/3_attestatores/run.py",
        attempt_ordinal=1,
    )
    assert resumed.returncode == 0, resumed.stderr
    assert first_path.read_bytes() == first_bytes

    reread = invoke_stage(
        run_root,
        "retention",
        "reread-failure",
        "pipeline/3_attestatores/run.py",
        attempt_ordinal=2,
    )
    assert reread.returncode == 0, reread.stderr
    records = [
        record
        for record in _testimonia(tree)
        if record["payload"]["act_key"] == "a1" and record["payload"]["chair"] == "attestator_3"
    ]
    assert [record["payload"]["attempt_ordinal"] for record in sorted(
        records, key=lambda record: record["payload"]["attempt_ordinal"]
    )] == [1, 2]
    assert first_path.read_bytes() == first_bytes
    assert len({record["artifact_id"] for record in records}) == 2
    current = next(
        record
        for record in latest_per_chair(records, "reread Testimonia")
        if record["payload"]["chair"] == "attestator_3"
    )
    assert current["payload"]["attempt_ordinal"] == 2
    assert current["outcome"] == "failed"
    assert attestatores.attempt_tally(tree)["count"] == 12


def test_an_actual_testimonium_identity_refuses_replacement_at_the_store_boundary(tmp_path):
    run_root, tree = run_to_designator(tmp_path, "happy")
    result = invoke_stage(run_root, "retention", "happy", "pipeline/3_attestatores/run.py")
    assert result.returncode == 0, result.stderr
    original = _testimonium_for(tree, act_key="a1", chair="attestator_1", ordinal=1)
    path = tree.resolve(tree.artifact_path(ATTESTATORES, "testimonium", original["artifact_id"]))
    before = path.read_bytes()
    changed = copy.deepcopy(original)
    changed["payload"]["payload"] = "different native witness output"
    changed["self_hash"] = self_hash(changed)

    with pytest.raises(IncompatibleReuse, match="immutable"):
        tree.publish_artifact(changed)
    assert path.read_bytes() == before


def test_parseable_native_payload_and_self_report_remain_separate_in_a_real_artifact(tmp_path):
    run_root, tree = run_to_designator(tmp_path, "structured-witness")
    result = invoke_stage(
        run_root,
        "retention",
        "structured-witness",
        "pipeline/3_attestatores/run.py",
    )
    assert result.returncode == 0, result.stderr
    record = _testimonium_for(tree, act_key="a1", chair="attestator_1", ordinal=1)
    native = {"tokens": ["μ", "beta"], "layout": {"line": 4}, "uncertain": True}
    self_report = {"confidence": "certain", "note": "witness claim only"}
    assert record["payload"]["payload"] == native
    assert record["payload"]["witness_reported"] == self_report
    assert "reported" not in record["payload"]
    assert record["payload"]["content_health"] == attestatores.content_health(
        native, completed=True
    )
    _, _, _, changed_health, problem = attestatores.prepared_response(
        {"payload": native, "witness_reported": {"confidence": "unsure"}}
    )
    assert problem is None
    assert changed_health == record["payload"]["content_health"]


def test_unrecordable_native_output_becomes_failed_without_replacement_text():
    bad_native = "\ud800"
    native, self_report, capabilities, health, problem = attestatores.prepared_response(
        {"payload": bad_native}
    )
    assert native is None
    assert self_report is None
    assert capabilities == attestatores.DEFAULT_FORMAT_CAPABILITIES
    assert health["recordable"] is False
    assert health["encoding"] == "invalid-or-unrecordable"
    assert "valid UTF-8" in problem
    record = attestatores.testimonium_payload(
        chair="attestator_1",
        act_key="a1",
        ordinal=1,
        regions=[],
        provenance={},
        format_capabilities=capabilities,
        native_payload=native,
        witness_reported=self_report,
        health=health,
        outcome="failed",
        reason=problem,
    )
    assert record["payload"] is None
    assert "reported" not in record
    assert "�" not in str(record)


def test_configured_never_attempted_seat_is_not_run_not_dead(tmp_path):
    run_root, tree = run_to_designator(tmp_path, "not-run-witness")
    result = invoke_stage(run_root, "retention", "not-run-witness", "pipeline/3_attestatores/run.py")
    assert result.returncode == 0, result.stderr
    record = _testimonium_for(tree, act_key="a1", chair="attestator_3", ordinal=1)
    assert record["outcome"] == "not-run"
    assert record["inputs"] == []
    assert record["payload"]["regions"] == []
    assert record["payload"]["payload"] is None
    assert record["payload"]["provenance"]["chair_state"] == "configured"
    assert record["payload"]["provenance"]["receipt_ref"] is None


def test_one_refused_crop_records_its_chairs_and_leaves_the_other_act_intact(tmp_path):
    run_root, tree = run_to_designator(tmp_path, "happy")
    entry = next(
        entry
        for entry in tree.build_manifest(DESIGNATOR)["artifacts"]
        if entry["kind"] == "region"
    )
    region = tree.read_artifact(DESIGNATOR, "region", entry["artifact_id"])
    refused_key = region["payload"]["act_key"]
    intact_key = "a2" if refused_key == "a1" else "a1"
    tree.resolve(region["payload"]["image_path"]).write_bytes(b"broken crop bytes")

    result = invoke_stage(run_root, "retention", "happy", "pipeline/3_attestatores/run.py")

    assert result.returncode == 0, result.stderr
    records = _testimonia(tree)
    assert len(records) == 6
    refused = [record for record in records if record["payload"]["act_key"] == refused_key]
    intact = [record for record in records if record["payload"]["act_key"] == intact_key]
    assert len(refused) == len(intact) == 3
    assert all(record["outcome"] == "not-run" for record in refused)
    assert all(record["inputs"] == [] and record["payload"]["regions"] == [] for record in refused)
    assert all(record["outcome"] == "read" for record in intact)


def test_malformed_one_witness_response_is_retained_as_failed_and_holds_the_tally(tmp_path):
    run_root, tree = run_to_designator(tmp_path, "malformed-witness")
    result = invoke_stage(run_root, "retention", "malformed-witness", "pipeline/3_attestatores/run.py")
    assert result.returncode == 3, result.stderr
    records = _testimonia(tree)
    assert len(records) == 6
    malformed = _testimonium_for(tree, act_key="a1", chair="attestator_3", ordinal=1)
    assert malformed["outcome"] == "failed"
    assert malformed["payload"]["payload"] is None
    assert malformed["payload"]["content_health"]["recordable"] is False
    assert "reported" not in malformed["payload"]
    assert sum(record["outcome"] == "read" for record in records) == 5
    tally = attestatores.attempt_tally(tree)
    assert tally["state"] == "UNKNOWN"
    assert tally["count"] is None
    assert tally["hold"] is True


def test_malformed_capabilities_fail_one_attempt_without_aborting_other_chairs(tmp_path):
    run_root, tree = run_to_designator(tmp_path, "malformed-capabilities")
    result = invoke_stage(
        run_root,
        "retention",
        "malformed-capabilities",
        "pipeline/3_attestatores/run.py",
    )
    assert result.returncode == 3, result.stderr
    records = _testimonia(tree)
    assert len(records) == 6
    malformed = _testimonium_for(tree, act_key="a1", chair="attestator_3", ordinal=1)
    assert malformed["outcome"] == "failed"
    assert malformed["payload"]["payload"] == "SYNTHETIC ACT ONE alpha beta"
    assert malformed["payload"]["format_capabilities"] == attestatores.DEFAULT_FORMAT_CAPABILITIES
    assert malformed["payload"]["content_health"]["recordable"] is False
    assert malformed["inputs"]
    assert malformed["payload"]["provenance"]["receipt_ref"] is not None
    assert sum(record["outcome"] == "read" for record in records) == 5


@pytest.mark.parametrize("damage", ("absent", "garbled", "truncated"))
def test_damaged_attempt_tally_is_unknown_and_refuses_to_add_a_replacement(tmp_path, damage):
    run_root, tree = run_to_designator(tmp_path, "happy")
    initial = invoke_stage(run_root, "retention", "happy", "pipeline/3_attestatores/run.py")
    assert initial.returncode == 0, initial.stderr
    manifest_path = tree.resolve(tree.manifest_path(ATTESTATORES))
    original = manifest_path.read_bytes()
    if damage == "absent":
        manifest_path.unlink()
    elif damage == "garbled":
        manifest_path.write_bytes(b"{")
    else:
        manifest_path.write_bytes(original[:-1])

    tally = attestatores.attempt_tally(tree)
    assert tally["state"] == "UNKNOWN"
    assert tally["count"] is None
    assert tally["hold"] is True
    retry = invoke_stage(
        run_root,
        "retention",
        "happy",
        "pipeline/3_attestatores/run.py",
        attempt_ordinal=2,
    )
    assert retry.returncode == 3
    assert "UNKNOWN" in retry.stderr
    assert all(record["payload"]["attempt_ordinal"] == 1 for record in _testimonia(tree))


def test_resealed_missing_testimonium_provenance_makes_the_next_tally_unknown(tmp_path):
    run_root, tree = run_to_designator(tmp_path, "happy")
    initial = invoke_stage(run_root, "retention", "happy", "pipeline/3_attestatores/run.py")
    assert initial.returncode == 0, initial.stderr
    record = _testimonium_for(tree, act_key="a1", chair="attestator_1", ordinal=1)
    changed = copy.deepcopy(record)
    del changed["payload"]["provenance"]
    changed["self_hash"] = self_hash(changed)
    path = tree.resolve(tree.artifact_path(ATTESTATORES, "testimonium", record["artifact_id"]))
    path.write_bytes(canonical_bytes(changed))
    tree.write_manifest(ATTESTATORES)

    tally = attestatores.attempt_tally(tree)
    assert tally["state"] == "UNKNOWN"
    assert tally["count"] is None
    assert tally["hold"] is True

    retry = invoke_stage(
        run_root,
        "retention",
        "happy",
        "pipeline/3_attestatores/run.py",
        attempt_ordinal=2,
    )

    assert retry.returncode == 3
    assert "UNKNOWN" in retry.stderr
    assert all(record["payload"]["attempt_ordinal"] == 1 for record in _testimonia(tree))


def test_resealed_malformed_content_health_makes_the_tally_unknown(tmp_path):
    run_root, tree = run_to_designator(tmp_path, "happy")
    initial = invoke_stage(run_root, "retention", "happy", "pipeline/3_attestatores/run.py")
    assert initial.returncode == 0, initial.stderr
    record = _testimonium_for(tree, act_key="a1", chair="attestator_1", ordinal=1)
    changed = copy.deepcopy(record)
    changed["payload"]["content_health"]["truncated"] = "not-a-truncation-state"
    changed["self_hash"] = self_hash(changed)
    path = tree.resolve(tree.artifact_path(ATTESTATORES, "testimonium", record["artifact_id"]))
    path.write_bytes(canonical_bytes(changed))
    tree.write_manifest(ATTESTATORES)

    tally = attestatores.attempt_tally(tree)

    assert tally["state"] == "UNKNOWN"
    assert tally["count"] is None
    assert tally["hold"] is True
