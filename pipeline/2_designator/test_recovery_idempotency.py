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
    request_id = review["payload"]["recovery_request_ref"]["relative_path"].rsplit("/", 1)[-1][:-5]
    assert review["outcome"] == "recovery-requested"

    recovery_args = (
        "--operation",
        "recover",
        "--act",
        act_id,
        "--recovery-request",
        request_id,
    )
    first = _run("pipeline/2_designator/run.py", root, *recovery_args)
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

    second = _run("pipeline/2_designator/run.py", root, *recovery_args)
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


def _load_designator():
    import importlib.util

    path = ROOT / "pipeline" / "2_designator" / "run.py"
    spec = importlib.util.spec_from_file_location("designator_recovery_under_test", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _designator_context(designator, root: Path):
    """A real Designator context over a real run, opened the way its CLI opens one."""
    from common.contracts.stages import DESIGNATOR
    from common.stage import open_context, stage_parser

    args = stage_parser("recovery bounds acceptance").parse_args(
        ["--run-root", str(root), "--run-id", "r", "--scenario", "review"]
    )
    return open_context(args, DESIGNATOR)


def test_a_first_recovery_is_still_cut_when_its_recrop_lands_on_the_original_bounds(tmp_path):
    """Invariant #14: the refusal above must not have bought its strictness by
    refusing good input too.

    `region_id` binds the act and the transform and nothing else, so a proposal
    region and a recovery region cut at identical bounds derive the identical id --
    `region_id(act, t) == region_id(act, dict(t))`. The duplicate guard compared
    against *every* region of the act, so a first, entirely legitimate recovery was
    refusable purely because its recrop landed on the act's original rectangle,
    which is exactly what a recrop asked for because the crop *decoded* badly
    rather than because it was *cut* badly would ask for. Nothing in the shipped
    fixture produces that collision, so nothing noticed.

    Driven in process rather than by CLI: `--fixture-root` cannot carry a modified
    fixture through the door, because the door refuses any root but the declared
    synthetic one (a caller-owned folder is real input), and the run authority binds
    the fixture into its config digest. Both of those are correct and neither is
    worth weakening for a test, so the recovery bounds are moved on the loaded
    fixture object instead, one layer inside the CLI.
    """
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

    from common.contracts.stages import DESIGNATOR, RECENSOR
    from common.runtree.store import RunTree

    designator = _load_designator()
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
    request_id = review["payload"]["recovery_request_ref"]["relative_path"].rsplit("/", 1)[-1][:-5]

    context = _designator_context(designator, root)
    act = next(row for row in context.fixture["act"] if row["key"] == "a1")
    original_bounds = {key: act[key] for key in ("x", "y", "w", "h")}
    for row in context.fixture["recovery"]:
        if row["act_key"] == "a1":
            row.update(original_bounds)

    assert designator.recovery_pass(context, act_id, request_id) == 1
    context.finish()

    recovery_regions = [
        record
        for record in (
            tree.read_artifact(DESIGNATOR, "region", entry["artifact_id"])
            for entry in tree.build_manifest(DESIGNATOR)["artifacts"]
            if entry["kind"] == "region"
        )
        if record["subject_id"] == act_id and record["payload"]["origin"] == "recovery"
    ]
    assert len(recovery_regions) == 1
    assert recovery_regions[0]["payload"]["transform"]["bounds"] == original_bounds
