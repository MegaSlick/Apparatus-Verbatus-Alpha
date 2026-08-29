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

from common.contracts.errors import SchemaRefusal
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


_REFERENCE_KEYS = frozenset({"relative_path", "sha256"})
# The floor is the point: with only `assert matched`, one added field on a
# reference shape would drop that whole class out of `_references` while the
# count stayed comfortably above zero, and the test would go on reporting a
# volume-hosted tree movable with an unverified set of references inside it.
#
# Measured on `synthetic-two-page-v0`, `review` scenario: a complete run
# resolves 313 references, and the smallest tree this helper is asked about --
# the staged door..recensor tree the crash test starts from -- resolves 162.
# The floor sits below that and far above zero, so it catches a class leaving
# the check without tracking every ordinary change in fixture size.
MINIMUM_RESOLVED_REFERENCES = 150


def _references(value: object) -> Iterator[dict[str, str]]:
    """Exactly the two-key shape, which is the *run-tree-relative* reference.

    Deliberately not a superset match. Measured: broadening it to "carries
    both keys" also matches the fixture ingress row
    `{"ordinal", "relative_path", "sha256"}`, whose `relative_path` is
    relative to the repository rather than to the run tree, and
    `RunTree.resolve` then fails on a reference that was never this test's
    to resolve. Two vocabularies share two key names; the floor above, not a
    wider match, is what catches a shape silently leaving this check.
    """
    if isinstance(value, dict):
        if set(value) == _REFERENCE_KEYS and all(isinstance(value[key], str) for key in value):
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
    # A floor, not merely nonzero: a broken traversal would otherwise prove
    # mobility vacuously, and a partly broken one would prove it on whatever
    # references happened to survive the match.
    assert matched >= MINIMUM_RESOLVED_REFERENCES, (
        f"only {matched} run-tree references were resolved under {tree.root}; "
        f"at least {MINIMUM_RESOLVED_REFERENCES} were expected"
    )
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


def _region_count_mid_write(root: Path, run_id: str, *, unchanged: int) -> int:
    """Count regions while the driver writes, absorbing only its own renames.

    The blob walk lists a directory and then stats every name it listed, and it
    refuses any entry that vanished in between.  That refusal is correct and
    stays: a stage manifest is built after its stage has finished writing, and
    a file disappearing under a finished stage is a fault.  This poll is the
    one caller that reads the tree while a live driver is still publishing into
    it, and a publish is `.NAME.tmp-XXXX` followed by a rename -- so the poll
    can list a temporary name whose rename lands before the stat, and be told
    the tree changed under it.  It did, which is the condition this loop is
    waiting on.

    Only that race is absorbed, and it has to match on both counts: the refusal
    is chained to an ENOENT -- a name that was listed and is now gone -- and the
    vanished name is a `.<target>.tmp-<unique>` publication temporary.  An
    ordinary evidence file disappearing mid-run is not that, and neither is a
    malformed manifest or a digest mismatch; each is re-raised here with its own
    reason rather than restated as "no change" until the hang guard expires.
    The quiescent counts either side of this window refuse normally in every
    case.
    """
    try:
        return _region_count(root, run_id)
    except SchemaRefusal as refusal:
        cause = refusal.__cause__
        vanished = getattr(cause, "filename", None)
        if (
            isinstance(cause, FileNotFoundError)
            and isinstance(vanished, str)
            and _is_publication_temporary_name(vanished)
        ):
            return unchanged
        raise


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
        while _region_count_mid_write(volume, "r", unchanged=region_count) == region_count:
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
        # Membership first. A deleted evidence file is the worst outcome this
        # test can find -- an act removed from a parish, with nothing
        # downstream able to say it happened -- and `finished[path]` alone
        # would report it as a bare KeyError that reads like a broken test.
        assert path in finished, f"resume deleted surviving evidence at {path}"
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

    # `snapshot` hashes every file it finds; the backup deliberately leaves out
    # `.<target>.tmp-*` publication temporaries, which a SIGKILL mid-publish can
    # leave behind. Comparing the two directly makes a correct backup of an
    # interrupted tree fail. The excluded names are compared instead of ignored:
    # the manifest has to account for every file the snapshot did not carry, and
    # each one has to be a publication temporary rather than lost evidence.
    manifest = _snapshot_manifest(mac, report.snapshot_sha256)
    # Manifest paths are relative to the run directory; `snapshot` keys are
    # relative to the runs root, so they carry the run id as their first segment.
    run = manifest["run_id"]
    backed_up = {f"{run}/{row['relative_path']}" for row in manifest["files"]}
    excluded = set(crashed) - backed_up
    assert excluded == {f"{run}/{name}" for name in manifest["excluded_publication_temporaries"]}
    assert all(_is_publication_temporary_name(name) for name in excluded), sorted(excluded)

    published = {path: digest for path, digest in crashed.items() if path in backed_up}
    # Distinct run-tree paths may share verified bytes, so copied plus reused
    # objects—not copied objects alone—must account for every snapshot member.
    assert report.copied + report.reused == len(published)

    restored_root = tmp_path / "restored-from-mac"
    _restore(mac, report.snapshot_sha256, restored_root)
    assert snapshot(restored_root) == published
    _assert_every_reference_resolves(restored_root, "r")

    assert _run(restored_root, "r", "review").returncode == 3
    assert _run(volume, "r", "review").returncode == 3
    # After both resume, the temporaries are gone and the two trees are equal
    # again -- so this comparison stays whole-tree with nothing filtered out.
    assert snapshot(restored_root) == snapshot(volume)
    _assert_every_reference_resolves(restored_root, "r")


def _snapshot_manifest(mac: Path, snapshot_sha256: str) -> dict:
    """The published snapshot record: what the backup carried, and what it left."""
    return json.loads((mac / "snapshots" / "sha256" / f"{snapshot_sha256}.json").read_text())


def _is_publication_temporary_name(relative: str) -> bool:
    """A same-directory `.<target>.tmp-<unique>` name, as `RunTree` publishes."""
    name = Path(relative).name
    if not name.startswith("."):
        return False
    target, separator, unique = name[1:].partition(".tmp-")
    return bool(separator and target and unique)


def _restore(mac: Path, snapshot_sha256: str, destination: Path) -> None:
    """Test-only: production restore must have its own run-tree custody boundary."""
    record = _snapshot_manifest(mac, snapshot_sha256)
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
