"""Mounted-volume mobility and crash recovery must use real subprocesses."""

from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path
from typing import Iterator, cast

from common.runtree.store import RunTree
from operations.operator.backup import sync_run_tree

# Stage code may not import `pipeline` by dotted path; the boundary test permits
# an explicit path load for a same-stage test helper.
_ACCEPTANCE_PATH = Path(__file__).resolve().parent / "test_orchestrator_acceptance.py"
_spec = importlib.util.spec_from_file_location("orchestrator_acceptance_helpers", _ACCEPTANCE_PATH)
_acceptance = importlib.util.module_from_spec(_spec)
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
            yield cast(dict[str, str], value)
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
    # Nonzero evidence is required; a broken traversal would otherwise prove
    # mobility vacuously.
    assert matched, f"no run-tree references were found under {tree.root}"
    return matched


def _region_count(root: Path, run_id: str) -> int:
    """Polling must count regions without re-verifying a tree being mutated.

    `verify_inputs=False` deliberately: this is a count, not an inspection, and
    the poll below runs it repeatedly against a tree the driver is still writing.
    Re-verifying every input reference on every poll made each iteration cost
    what the window itself is worth, which is how an observed event turns back
    into a race.  The whole-tree reference check runs separately, once, after
    the crash.
    """
    manifest = RunTree(root, run_id).build_manifest("designator", verify_inputs=False)
    return sum(entry["kind"] == "region" for entry in manifest["artifacts"])


def _kill_and_reap(process: subprocess.Popen[bytes]) -> int:
    """Leave no recovery process group behind, including on an assertion path."""

    if process.poll() is None:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            # The process group exited between poll and kill; wait still reaps
            # the child and reports the state that won that race.
            pass
    return process.wait(timeout=120)


def _crash_mid_recovery(volume: Path, scratch: Path) -> dict[str, str]:
    """SIGKILL a real recovery only after its first append.

    A local directory stands in for the offline mount; the returned snapshot is
    the durable state available to resume.
    """

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
    try:
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
    except BaseException:
        _kill_and_reap(process)
        raise
    # The bound guards against an unreapable child; it does not extend the
    # recovery work window.
    assert _kill_and_reap(process) == -signal.SIGKILL

    crashed = snapshot(volume)
    assert any(path not in before_crash for path in crashed), "the crash followed a real append"
    _assert_every_reference_resolves(volume, "r")
    return crashed


def test_volume_hosted_tree_is_movable_and_crash_resume_appends_without_rewriting(
    tmp_path: Path,
) -> None:
    """A moved tree must resolve identically, and crash resume may only append.

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
            # A legitimate append rebuilds derived inventories and current-state
            # receipts; the immutable evidence they name must retain its bytes.
            continue
        assert finished[path] == digest, f"resume rewrote surviving evidence at {path}"
    # A resumed Recensor re-entry seals its own boundary: it adds one more
    # decode-environment/stage-seal attempt pair, and the stage's derived
    # manifest and terminal seal follow. Every other byte matches the
    # uninterrupted run exactly; the extra pair is the honest record that a
    # re-entry happened, not a rewrite of surviving evidence.
    recensor_seal_prefixes = (
        "r/5_recensor/artifacts/stage-seal/",
        "r/5_recensor/artifacts/decode-environment/",
    )

    def _comparable(tree_snapshot):
        return {
            path: digest
            for path, digest in tree_snapshot.items()
            if not path.startswith(recensor_seal_prefixes) and path != "r/5_recensor/manifest.json"
        }

    assert _comparable(finished) == _comparable(uninterrupted)
    extra = set(finished) - set(uninterrupted)
    assert extra <= {path for path in finished if path.startswith(recensor_seal_prefixes)}, (
        f"resume added unexpected artifacts: {sorted(extra)}"
    )
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
    """A mid-recovery backup must restore and resume byte-identically.

    The snapshot must publish for an interrupted tree, restore the crashed bytes
    exactly, and let both the source and restored copy reach the same result.
    """
    volume = tmp_path / "mounted-volume" / "runs"
    crashed = _crash_mid_recovery(volume, tmp_path)

    mac = tmp_path / "Mac Backup"
    report = sync_run_tree(volume, "r", mac)
    # Distinct run-tree paths may share verified bytes, so copied plus reused
    # objects—not copied objects alone—must account for every snapshot member.
    assert report.copied + report.reused == len(crashed)

    restored_root = tmp_path / "restored-from-mac"
    _restore(mac, report.snapshot_sha256, restored_root)
    assert snapshot(restored_root) == crashed
    _assert_every_reference_resolves(restored_root, "r")

    assert _run(restored_root, "r", "review").returncode == 3
    assert _run(volume, "r", "review").returncode == 3
    assert snapshot(restored_root) == snapshot(volume)
    _assert_every_reference_resolves(restored_root, "r")


def _restore(mac: Path, snapshot_sha256: str, destination: Path) -> None:
    """Test-only: production restore must have its own run-tree custody boundary."""
    record = json.loads((mac / "snapshots" / "sha256" / f"{snapshot_sha256}.json").read_text())
    for row in record["files"]:
        data = (mac / "objects" / "sha256" / row["sha256"]).read_bytes()
        assert hashlib.sha256(data).hexdigest() == row["sha256"], row
        target = destination / record["run_id"] / row["relative_path"]
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)


def test_crash_observer_cleanup_kills_and_reaps_a_live_process_group(monkeypatch) -> None:
    observed: list[object] = []

    class Process:
        pid = 123

        def poll(self):
            return None

        def wait(self, *, timeout):
            observed.append(("wait", timeout))
            return -signal.SIGKILL

    monkeypatch.setattr(os, "killpg", lambda pid, sent: observed.append(("killpg", pid, sent)))

    assert _kill_and_reap(Process()) == -signal.SIGKILL
    assert observed == [("killpg", 123, signal.SIGKILL), ("wait", 120)]
