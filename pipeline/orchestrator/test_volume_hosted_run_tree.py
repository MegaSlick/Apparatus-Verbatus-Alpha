"""Offline acceptance of a mounted-volume run root and crash recovery."""

from __future__ import annotations

import hashlib

# Loaded by path, not dotted import: stage directories are not importable
# packages (pipeline/test_stage_import_boundaries.py), and the acceptance
# module's own _load_recensor sets the idiom.
import importlib.util as _importlib_util
import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path
from typing import Iterator

from common.runtree.store import RunTree
from operations.operator.backup import sync_run_tree

_ACCEPTANCE_PATH = Path(__file__).resolve().parent / "test_orchestrator_acceptance.py"
_spec = _importlib_util.spec_from_file_location("orchestrator_acceptance_helpers", _ACCEPTANCE_PATH)
_acceptance = _importlib_util.module_from_spec(_spec)
_spec.loader.exec_module(_acceptance)
snapshot = _acceptance.snapshot

ROOT = Path(__file__).resolve().parents[2]
ORCHESTRATOR = ROOT / "pipeline" / "orchestrator" / "run.py"
FIXTURE = "synthetic-two-page-v0"


def _run(
    root: Path, run_id: str, scenario: str, *selection: str
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [
            sys.executable,
            str(ORCHESTRATOR),
            "--fixture",
            FIXTURE,
            "--scenario",
            scenario,
            "--run-id",
            run_id,
            "--run-root",
            str(root),
            *selection,
        ],
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=False,
    )


def _references(value: object) -> Iterator[dict[str, str]]:
    if isinstance(value, dict):
        if set(value) == {"relative_path", "sha256"} and all(
            isinstance(value[key], str) for key in value
        ):
            yield value  # type: ignore[misc]
        for nested in value.values():
            yield from _references(nested)
    elif isinstance(value, list):
        for nested in value:
            yield from _references(nested)


def _assert_every_reference_resolves(root: Path, run_id: str) -> int:
    tree = RunTree(root, run_id)
    matched = 0
    for path in sorted(tree.root.rglob("*.json")):
        for reference in _references(json.loads(path.read_text())):
            resolved = tree.resolve(reference["relative_path"])
            assert resolved.is_file(), reference
            assert hashlib.sha256(resolved.read_bytes()).hexdigest() == reference["sha256"]
            matched += 1
    # A walk that matched nothing would pass this function vacuously, which is
    # the one way it could report a movable tree without having read one.
    assert matched, f"no run-tree references were found under {tree.root}"
    return matched


def _region_count(root: Path, run_id: str) -> int:
    """How many region artifacts the Designator has published so far.

    `verify_inputs=False` deliberately: this is a count, not an inspection, and
    the poll below runs it repeatedly against a tree the driver is still writing.
    Re-verifying every input reference on every poll made each iteration cost
    what the window itself is worth, which is how an observed event turns back
    into a race.  The whole-tree reference check runs separately, once, after
    the crash.
    """
    manifest = RunTree(root, run_id).build_manifest("designator", verify_inputs=False)
    return len([entry for entry in manifest["artifacts"] if entry["kind"] == "region"])


def _crash_mid_recovery(volume: Path, scratch: Path) -> dict[str, str]:
    """Drive a real recovery on `volume` and SIGKILL it mid-member.

    Returns the crashed tree's snapshot.  A local directory stands in for the
    offline volume mount required by Unit 27.
    """

    # First create the same durable boundary on the mounted-volume path.
    staged = _run(volume, "r", "review", "--from", "door", "--to", "recensor")
    assert staged.returncode == 3, staged.stderr
    before_crash = snapshot(volume)
    region_count = _region_count(volume, "r")

    # Recovery is an actual driver member.  Kill its process only after the
    # Designator has appended the recovery crop, while the driver is still in
    # that member; the later Perlector/Recensor writes therefore cannot occur.
    #
    # The driver's output goes to files rather than pipes.  Nothing drains a
    # pipe between Popen and the kill, so a driver that filled the 64 KiB pipe
    # buffer would block in `write` and never reach the append this loop is
    # waiting for -- a stall the test would have reported as a missing crop.
    out_path = scratch / "recovery.out"
    err_path = scratch / "recovery.err"
    with out_path.open("wb") as out_handle, err_path.open("wb") as err_handle:
        process = subprocess.Popen(
            [
                sys.executable,
                str(ORCHESTRATOR),
                "--fixture",
                FIXTURE,
                "--scenario",
                "review",
                "--run-id",
                "r",
                "--run-root",
                str(volume),
                "--stage",
                "recovery",
            ],
            cwd=ROOT,
            stdout=out_handle,
            stderr=err_handle,
            start_new_session=True,
        )
    # The kill window is *observed*, never timed.  This loop ends on one of two
    # facts about the driver -- the recovery crop is on disk, or the driver has
    # exited -- and on neither a clock nor a sleep.  A ten-second wall-clock
    # deadline stood here and was the wrong bound: measured in this container,
    # the append that lands 0.35s in on an idle machine lands at 6.6-9.4s under
    # ten-times CPU oversubscription, so under a loaded parallel suite the test
    # failed for want of a CPU rather than for a defect.  The absolute bound
    # below is a hang guard, deliberately far larger than any load this window
    # scales to; it is not the window.
    hang_guard = time.monotonic() + 600
    while _region_count(volume, "r") == region_count:
        if process.poll() is not None:
            raise AssertionError(
                "recovery exited before it appended a crop:\n"
                f"{err_path.read_text()}\n{out_path.read_text()}"
            )
        assert time.monotonic() < hang_guard, "the recovery driver neither appended nor exited"
        time.sleep(0.01)
    # The window between the Designator's append and the Perlector's first write
    # measures 0.31s idle and 4.9-7.1s under the same ten-times load, against a
    # poll costing at most 0.23s there: it widens with load rather than closing,
    # which is what makes this an observation and not a race.
    assert process.poll() is None, "recovery finished before the crash point"
    os.killpg(process.pid, signal.SIGKILL)
    # Reaping a process group that has already been SIGKILLed; the bound is a
    # hang guard for an unreapable child, not a wait for work to finish.
    assert process.wait(timeout=120) == -signal.SIGKILL

    crashed = snapshot(volume)
    assert any(path not in before_crash for path in crashed), "the crash followed a real append"
    _assert_every_reference_resolves(volume, "r")
    return crashed


def test_volume_hosted_tree_is_movable_and_crash_resume_appends_without_rewriting(
    tmp_path: Path,
) -> None:
    """Unit 27's first two claims: the tree moves, and a crash resumes by appending.

    Every input reference in the volume-hosted tree resolves and hashes to its
    recorded digest, a SIGKILL mid-recovery leaves a resumable tree, and the
    resume rewrites no surviving evidence -- it finishes to the same bytes an
    uninterrupted local run produces.
    """
    local = tmp_path / "local-runs"
    volume = tmp_path / "mounted-volume" / "runs"
    assert _run(local, "r", "review").returncode == 3
    uninterrupted = snapshot(local)

    crashed = _crash_mid_recovery(volume, tmp_path)

    resumed = _run(volume, "r", "review")
    assert resumed.returncode == 3, resumed.stderr
    finished = snapshot(volume)
    for path, digest in crashed.items():
        if path.endswith(("/manifest.json", "/manifest-door.json", "/index.json")) or path.endswith(
            "run-health/recensor-partition-receipt.json"
        ):
            # These are explicitly derived inventories/current-state receipts;
            # a legitimate append rebuilds them.  The evidence they inventory
            # must retain its bytes, which is the assertion below.
            continue
        assert finished[path] == digest, f"resume rewrote surviving evidence at {path}"
    assert finished == uninterrupted
    matched = _assert_every_reference_resolves(volume, "r")

    # A volume reached through a symlink is the ordinary Mac and Linux mount
    # spelling, and it is the one way a relative reference could still be read
    # against the wrong root: `RunTree` resolves its root once at construction,
    # so the containment check compares two resolved spellings rather than one
    # of each.  Checked rather than assumed, because a run tree that stops
    # resolving when the mount is named differently is a run tree that is not
    # movable.
    alias = tmp_path / "volume-alias"
    alias.symlink_to(volume)
    assert _assert_every_reference_resolves(alias, "r") == matched


def test_a_backup_of_a_mid_recovery_tree_restores_and_resumes_byte_identically(
    tmp_path: Path,
) -> None:
    """The Mac backup of a tree whose recovery is mid-flight is a resumable tree.

    Three claims at once, none of which the crash test above reaches: that
    `verbatus backup` publishes a snapshot for a tree holding an *in-progress*
    recovery rather than refusing one; that restoring that snapshot reproduces
    the crashed tree byte for byte; and that the driver resumes the restored
    copy to the same bytes as the tree it was copied from.  The test above
    already ties that tree to an uninterrupted local run, so the equality here
    carries the restored copy to the same place.
    """
    volume = tmp_path / "mounted-volume" / "runs"
    crashed = _crash_mid_recovery(volume, tmp_path)

    mac = tmp_path / "Mac Backup"
    report = sync_run_tree(volume, "r", mac)
    assert report.copied == len(crashed) and report.reused == 0

    restored_root = tmp_path / "restored-from-mac"
    _restore(mac, report.snapshot_sha256, restored_root)
    assert snapshot(restored_root) == crashed
    _assert_every_reference_resolves(restored_root, "r")

    assert _run(restored_root, "r", "review").returncode == 3
    assert _run(volume, "r", "review").returncode == 3
    assert snapshot(restored_root) == snapshot(volume)
    _assert_every_reference_resolves(restored_root, "r")


def _restore(mac: Path, snapshot_sha256: str, destination: Path) -> None:
    """Rebuild a run tree from one backup snapshot, verifying every digest.

    There is no `verbatus restore` verb and this unit does not add one: the
    snapshot is a complete self-describing inventory and the store is content
    addressed, so a restore is a digest-checked copy and nothing more.  A verb
    that *writes into* a run tree is a custody question in its own right under
    Unit 4's doctrine, and it belongs to whichever unit takes that question up
    rather than to the last seat of this one.  What this unit owes is the
    evidence that the backup can be restored, which is what this performs.
    """
    record = json.loads((mac / "snapshots" / "sha256" / f"{snapshot_sha256}.json").read_text())
    for row in record["files"]:
        data = (mac / "objects" / "sha256" / row["sha256"]).read_bytes()
        assert hashlib.sha256(data).hexdigest() == row["sha256"], row
        target = destination / record["run_id"] / row["relative_path"]
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
