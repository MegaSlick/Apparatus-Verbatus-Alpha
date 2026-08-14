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

import pytest
from _test_support import load_designator

from common.contracts.errors import ContractError
from common.contracts.stages import DESIGNATOR
from common.stage import EXIT_FATAL

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
    assert second.returncode == EXIT_FATAL
    assert "already has a region cut" in second.stderr

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


def test_an_unrecognized_operation_refuses_rather_than_running_initial_pass(tmp_path):
    """A typo of "recover" must not silently fall through to a full initial
    pass -- it must be refused as the unrecognized operation it is."""
    root = tmp_path / "runs"
    for program in ("pipeline/1_exemplar/door.py", "pipeline/1_exemplar/run.py"):
        result = _run(program, root)
        assert result.returncode == 0, f"{program}: {result.stderr}"

    result = _run("pipeline/2_designator/run.py", root, "--operation", "Recover")
    assert result.returncode == EXIT_FATAL, result.stdout
    assert "is not one of 'initial' or 'recover'" in result.stderr
    assert not (root / "r" / "2_designator" / "artifacts").exists(), (
        "an unrecognized operation must refuse before any region or seal is written"
    )


def _load_designator():
    return load_designator("designator_recovery_under_test")


def _designator_context(designator, root: Path):
    """A real Designator context over a real run, opened the way its CLI opens one."""
    from common.contracts.stages import DESIGNATOR
    from common.stage import open_context, stage_parser

    args = stage_parser("recovery bounds acceptance").parse_args(
        ["--run-root", str(root), "--run-id", "r", "--scenario", "review"]
    )
    return open_context(args, DESIGNATOR)


def test_a_recovery_at_existing_bounds_refuses_without_cutting_a_duplicate(tmp_path):
    """A recrop must add coverage rather than manufacture another reading pass.

    `region_id` binds the act and transform, so a recovery at an already-cut
    proposal rectangle would carry the same pixels and identity. It cannot recover
    coverage, and allowing it makes the Perlector receive duplicate evidence.

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

    # The already-cut proposal region's *final* (padded) bounds, not the
    # fixture's raw declared act rectangle: `cut_region` expands a proposal
    # crop by the configured capture padding before cutting it, so the bounds
    # that would actually collide with an existing region are the padded
    # ones, not the pre-padding rectangle identity is bound to.
    existing_proposal = next(
        record
        for record in (
            tree.read_artifact(DESIGNATOR, "region", entry["artifact_id"])
            for entry in tree.build_manifest(DESIGNATOR)["artifacts"]
            if entry["kind"] == "region"
        )
        if record["subject_id"] == act_id and record["payload"]["origin"] == "proposal"
    )
    existing_bounds = existing_proposal["payload"]["transform"]["bounds"]

    context = _designator_context(designator, root)
    for row in context.fixture["recovery"]:
        if row["act_key"] == "a1":
            row.update(existing_bounds)

    with pytest.raises(ContractError, match="already has a region cut"):
        designator.recovery_pass(context, act_id, request_id)
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
    assert recovery_regions == []


def test_an_out_of_page_recovery_rectangle_refuses_with_a_contract_error(tmp_path):
    """A recovery crop skips `apply_padding` (it names its own exact final
    rectangle) and so has no other bounds check before `crop_png` -- which
    raises a bare `ValueError` `run_stage` does not turn into `EXIT_FATAL`.
    The recovery path needs its own explicit check to fail the same way every
    other refusal in this pipeline does."""
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
    for row in context.fixture["recovery"]:
        if row["act_key"] == "a1":
            row.update({"x": 0, "y": 0, "w": 10**6, "h": 10**6})

    with pytest.raises(ContractError, match="recovery bounds"):
        designator.recovery_pass(context, act_id, request_id)
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
    assert recovery_regions == [], "a refused out-of-page recovery must cut no region"


def test_multiple_declared_recovery_bounds_refuse_instead_of_selecting_the_first(tmp_path):
    """A recovery request may not pick one of several fixture rectangles by order."""
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
    request_id = review["payload"]["recovery_request_ref"]["relative_path"].rsplit("/", 1)[-1][:-5]
    context = _designator_context(designator, root)
    original = next(row for row in context.fixture["recovery"] if row["act_key"] == "a1")
    context.fixture["recovery"].append(dict(original))

    with pytest.raises(ContractError, match="declares 2 recovery regions"):
        designator.recovery_pass(context, review["subject_id"], request_id)
