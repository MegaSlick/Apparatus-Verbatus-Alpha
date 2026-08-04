"""Recovery cuts one crop per fulfilled request, never a second author for one.

The orchestrator's own recovery loop cannot double-invoke the Designator for one
act (`pending_recoveries` drops an act from the outstanding set the moment its
latest Recensor review stops being "recovery-requested"), so this drives the
stage directly the way its own module docstring documents as legitimate operator
usage -- the same "operator misuse, not orchestrator misuse" path this repair
closes.
"""

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def _run(program: str, root: Path, *extra: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [
            sys.executable,
            str(ROOT / program),
            "--run-root",
            str(root),
            "--run-id",
            "r",
            "--scenario",
            "review",
            *extra,
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
    )


def test_recovering_the_same_act_twice_refuses_rather_than_cutting_a_duplicate(tmp_path):
    root = tmp_path / "runs"
    for program in (
        "pipeline/1_exemplar/door.py",
        "pipeline/1_exemplar/run.py",
        "pipeline/2_designator/run.py",
        "pipeline/3_attestatores/run.py",
        "pipeline/4_perlector/run.py",
        "pipeline/5_recensor/run.py",
    ):
        result = _run(program, root)
        assert result.returncode in (0, 3), f"{program}: {result.stderr}"

    from common.contracts.stages import RECENSOR
    from common.runtree.store import RunTree

    tree = RunTree(root, "r")
    review = next(
        record
        for record in (
            tree.read_artifact(RECENSOR, "review", entry["artifact_id"])
            for entry in tree.build_manifest(RECENSOR)["artifacts"]
            if entry["kind"] == "review"
        )
        if record["payload"]["act_key"] == "a1"
    )
    act_id = review["subject_id"]
    assert review["outcome"] == "recovery-requested"

    first = _run("pipeline/2_designator/run.py", root, "--operation", "recover", "--act", act_id)
    assert first.returncode == 0, first.stderr

    from common.contracts.stages import DESIGNATOR

    recovery_regions_before = [
        record
        for record in (
            tree.read_artifact(DESIGNATOR, "region", entry["artifact_id"])
            for entry in tree.build_manifest(DESIGNATOR)["artifacts"]
            if entry["kind"] == "region"
        )
        if record["subject_id"] == act_id and record["payload"]["origin"] == "recovery"
    ]
    assert len(recovery_regions_before) == 1

    second = _run("pipeline/2_designator/run.py", root, "--operation", "recover", "--act", act_id)
    assert second.returncode == 2
    assert "already has a recovery region cut" in second.stderr

    recovery_regions_after = [
        record
        for record in (
            tree.read_artifact(DESIGNATOR, "region", entry["artifact_id"])
            for entry in tree.build_manifest(DESIGNATOR)["artifacts"]
            if entry["kind"] == "region"
        )
        if record["subject_id"] == act_id and record["payload"]["origin"] == "recovery"
    ]
    assert recovery_regions_after == recovery_regions_before, (
        "a refused duplicate recovery call must not still cut a second region"
    )
