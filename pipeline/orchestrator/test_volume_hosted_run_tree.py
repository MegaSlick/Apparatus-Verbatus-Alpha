"""Mounted-volume mobility and crash recovery must use real subprocesses."""

from __future__ import annotations

import errno
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

import pytest

from common.contracts.errors import SchemaRefusal
from common.runtree.store import RunTree
from operations.operator.backup import _is_publication_temporary, sync_run_tree

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


def _partition_publication_temporaries(
    tree_snapshot: dict[str, str], run_id: str = "r"
) -> tuple[dict[str, str], dict[str, str]]:
    """Split a snapshot into published evidence and `.<target>.tmp-*` residue.

    A driver killed mid-write leaves the publication temporary it was writing, and
    where the kill lands is decided by the scheduler: the same SIGKILL leaves residue
    on one platform's run and not on another's, with a fresh `mkstemp` suffix each
    time. Two trees that differ only by such a name hold identical evidence, and an
    equality over raw snapshots reads that as a mismatch — which is how these
    comparisons failed on Linux while passing on macOS.

    The rule is `operations.operator.backup`'s own, imported rather than respelled:
    the backup excludes exactly these names from a snapshot and records them under
    `excluded_publication_temporaries` so the exclusion cannot be silent. Callers here
    do the same — every dropped name is returned, and asserted on, never ignored.
    """
    scope = RunTree(Path("/nonexistent"), run_id).inventory_scope()
    prefix = f"{run_id}/"
    residue = {
        path: digest
        for path, digest in tree_snapshot.items()
        if path.startswith(prefix) and _is_publication_temporary(path[len(prefix) :], scope)
    }
    published = {path: digest for path, digest in tree_snapshot.items() if path not in residue}
    return published, residue


def _plant_publication_temporary(volume: Path, run_id: str = "r") -> Path:
    """Leave the residue a mid-write kill leaves, so the exclusion is always measured.

    The backup half of this file already plants one for the same reason: whether a
    real SIGKILL lands mid-write is the scheduler's decision, so a path exercised only
    when it does is a path tested only sometimes. Planted before the resume, so the
    resume is also shown to tolerate residue rather than only the assertions below.
    """
    planted = (
        volume / run_id / "2_designator" / "artifacts" / "decode-environment"
    ) / ".art_plantedresidue.json.tmp-plantedbythistest"
    planted.parent.mkdir(parents=True, exist_ok=True)
    planted.write_bytes(b"a publication interrupted mid-write when the driver was killed")
    return planted


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
    baseline = _run(local, "r", "review")
    assert baseline.returncode == 3, baseline.stderr
    uninterrupted = snapshot(local)

    crashed = _crash_mid_recovery(volume, tmp_path)
    planted = _plant_publication_temporary(volume)

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
        published, _residue = _partition_publication_temporaries(tree_snapshot)
        return {
            path: digest
            for path, digest in published.items()
            if not path.startswith(recensor_seal_prefixes) and path != "r/5_recensor/manifest.json"
        }

    # The residue is named before it is set aside, and the planted one must be in it:
    # a kill mid-write leaves a `.<target>.tmp-*` name with a fresh random suffix, so
    # two trees holding identical evidence differ by that name alone. Dropping it is
    # not the same as overlooking it — every dropped path is checked to be residue of
    # this run tree, and the resume is shown to have carried none of it into evidence.
    finished_published, finished_residue = _partition_publication_temporaries(finished)
    planted_key = str(planted.relative_to(volume))
    assert planted_key in finished_residue, (
        "the planted publication temporary was read as published evidence"
    )
    assert planted_key not in finished_published
    assert _partition_publication_temporaries(uninterrupted)[1] == {}, (
        "an uninterrupted run left a publication temporary behind"
    )
    assert _comparable(finished) == _comparable(uninterrupted)
    extra = set(finished_published) - set(uninterrupted)
    assert extra <= {
        path for path in finished_published if path.startswith(recensor_seal_prefixes)
    }, f"resume added unexpected artifacts: {sorted(extra)}"
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
    _crash_mid_recovery(volume, tmp_path)

    # Whether the kill lands mid-write, leaving a `.<target>.tmp-*` publication
    # temporary behind, is a matter of timing -- so whether this test exercised
    # the backup's exclusion of them was decided by the scheduler. It failed
    # exactly once in a full-suite run for that reason. One is planted, so the
    # exclusion path is measured on every run instead of occasionally.
    planted = volume / "r" / ".run.json.tmp-interruptedpublication"
    planted.write_bytes(b"an interrupted publication, mid-write when the driver died")
    crashed = snapshot(volume)

    mac = tmp_path / "Mac Backup"
    report = sync_run_tree(volume, "r", mac)

    # pr/08's residue-exclusion form, read through this branch's own helpers.
    # `snapshot` hashes every file it finds; the backup deliberately leaves out
    # `.<target>.tmp-*` publication temporaries, which a SIGKILL mid-publish can
    # leave behind, so comparing the two directly makes a correct backup of an
    # interrupted tree fail. The exclusions are subtracted from the expectation
    # rather than ignored, each one has to have really been in the crashed tree,
    # and each has to look like a publication temporary -- so an exclusion can
    # never stand in for a file the backup lost.
    manifest = _snapshot_manifest(mac, report.snapshot_sha256)
    # Manifest paths are relative to the run directory; `snapshot` keys are
    # relative to the runs root, so they carry the run id as their first segment.
    run = manifest["run_id"]
    excluded = {f"{run}/{name}" for name in manifest["excluded_publication_temporaries"]}
    assert f"{run}/{planted.name}" in excluded, "the planted temporary was carried, not excluded"
    assert excluded <= set(crashed), "the snapshot excluded something the tree never held"
    assert all(_is_publication_temporary_name(name) for name in excluded), sorted(excluded)
    published = {f"{run}/{row['relative_path']}" for row in manifest["files"]}
    assert published == set(crashed) - excluded
    # Distinct run-tree paths may share verified bytes, so copied plus reused --
    # not copied alone -- is what must account for every published member. pr/08
    # additionally asserted `reused == 0` ("nothing was in the object store
    # before this backup"); measured on this branch's tree that is false --
    # copied 81, reused 2, of 83 published members -- because this content holds
    # two distinct paths with identical verified bytes. Keeping that assertion
    # through the merge would have turned a correct backup into a red test.
    assert report.copied + report.reused == len(manifest["files"])

    restored_root = tmp_path / "restored-from-mac"
    _restore(mac, report.snapshot_sha256, restored_root)
    # The restore replays the published inventory, so a crashed tree holding an
    # excluded temporary restores without it -- the tree, minus what the backup
    # deliberately never carried.
    assert snapshot(restored_root) == {
        path: digest for path, digest in crashed.items() if path not in excluded
    }
    _assert_every_reference_resolves(restored_root, "r")

    # That temporary has done its work at the run root. Removing it and planting one
    # inside a stage's artifacts directory keeps the residue where a killed driver
    # actually leaves it, and only in the source: the backup excluded the crashed
    # tree's temporaries by design, so the restored copy never had them. The two trees
    # are therefore byte-identical in evidence and cannot be byte-identical in residue,
    # which is what the equality below has to say. It failed on Linux and passed on
    # macOS while it said otherwise, because where a SIGKILL lands is the scheduler's
    # decision and the surviving `.tmp-` name carries a fresh random suffix.
    planted.unlink()
    source_residue_path = _plant_publication_temporary(volume)

    restored_run = _run(restored_root, "r", "review")
    assert restored_run.returncode == 3, restored_run.stderr
    source_run = _run(volume, "r", "review")
    assert source_run.returncode == 3, source_run.stderr
    restored_published, restored_residue = _partition_publication_temporaries(
        snapshot(restored_root)
    )
    source_published, source_residue = _partition_publication_temporaries(snapshot(volume))
    # Named, not overlooked: the source's residue is exactly what was planted plus
    # anything the crash left, the restored copy carries none, and every published
    # byte matches.
    assert str(source_residue_path.relative_to(volume)) in source_residue
    assert restored_residue == {}, (
        "the restore replayed a publication temporary as published evidence"
    )
    assert restored_published == source_published
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


def _process_has_terminated(pid: int) -> bool:
    """Has this pid stopped running -- reaped, or dead and awaiting its parent?

    Signal zero asks whether the pid exists, and a zombie still does: it holds
    its slot until someone reaps it. On Linux that is a real interval, because
    the grandchild's own parent is being killed in the same signal and cannot
    reap it; the pid only disappears once it is reparented and init collects it.
    Waiting for disappearance alone therefore spends the full deadline and then
    reports a leak after a kill that worked perfectly.

    Linux answers the actual question through procfs, where state ``Z`` is
    "terminated, not yet reaped". The comm field can contain spaces and
    parentheses, so the state is read after the last ``)``. Elsewhere -- macOS
    has no procfs -- disappearance is the only available answer and is used.
    """

    # Only the two answers that are evidence of termination. A `PermissionError`
    # says the pid exists and belongs to someone else, and an `EACCES` or `EIO`
    # reading procfs says nothing about the process at all -- reporting either
    # as "gone" would let this test pass without ever establishing that the kill
    # worked. They are left to surface as the failures they are.
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return True
    if sys.platform != "linux":
        return False
    try:
        stat_line = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8", errors="replace")
    except FileNotFoundError:
        # The entry went away between the two reads, which is the pid being
        # reaped -- the outcome this function is asked about.
        return True
    return _procfs_state_is_zombie(stat_line)


def _procfs_state_is_zombie(stat_line: str) -> bool:
    """Read the state field of a `/proc/<pid>/stat` line.

    Split out so the parse is measurable on a host without procfs. The comm
    field is parenthesised and may itself contain spaces and parentheses, so the
    state is the first token after the *last* `)`, never `split()[2]`.
    """

    return stat_line.rpartition(")")[2].split()[:1] == ["Z"]


def test_only_evidence_of_termination_counts_as_termination(monkeypatch) -> None:
    """A pid we may not signal is a pid that still exists.

    Reporting `PermissionError` as "gone" would let the process-group test pass
    without ever establishing that the kill worked, which is the one thing it
    exists to establish.
    """

    def _denied(_pid, _signal):
        raise PermissionError(errno.EPERM, "Operation not permitted")

    monkeypatch.setattr(os, "kill", _denied)
    with pytest.raises(PermissionError):
        _process_has_terminated(1)

    def _absent(_pid, _signal):
        raise ProcessLookupError(errno.ESRCH, "No such process")

    monkeypatch.setattr(os, "kill", _absent)
    assert _process_has_terminated(1)


def test_the_procfs_state_parse_survives_a_command_name_full_of_parentheses() -> None:
    """`split()[2]` is the parse this must not be, and a stat line says why."""

    assert _procfs_state_is_zombie("42 (python3) Z 1 42 42 0 -1 4194560 0 0")
    assert not _procfs_state_is_zombie("42 (python3) S 1 42 42 0 -1 4194560 0 0")
    # A real command name this repository could produce: spaces and brackets.
    assert _procfs_state_is_zombie("42 (run.py --stage recovery) Z 1 42 42")
    assert not _procfs_state_is_zombie("42 (run.py --stage recovery) R 1 42 42")
    assert _procfs_state_is_zombie("42 (weird )(name) Z 1 42 42")
    assert not _procfs_state_is_zombie("42 (weird )(name) S 1 42 42")


def test_crash_observer_cleanup_kills_and_reaps_a_live_process_group() -> None:
    """A real session, a real grandchild, and a real check that the group is gone.

    Driven through stubs -- a `Process` that always reports itself alive, an
    `os.killpg` replaced by a list append, a `wait` that returns whatever the
    stub says -- this proved only that `_kill_and_reap` calls two functions in
    one order. It would have stayed green if the wrong group were killed, or if
    a real child survived on the machine, and a leaked recovery driver
    accumulating through a parish-sized run is the thing it is named for.

    The child starts its own session and forks a grandchild that outlives it, so
    killing the process alone leaves the grandchild running: only a group-wide
    signal ends both. The grandchild reports its pid, and its death is what this
    asserts, rather than the call sequence that was supposed to cause it.
    """

    with subprocess.Popen(
        [
            sys.executable,
            "-c",
            "import os, sys, time\n"
            "child = os.fork()\n"
            "if child == 0:\n"
            "    time.sleep(600)\n"
            "    os._exit(0)\n"
            "sys.stdout.write(f'{child}\\n')\n"
            "sys.stdout.flush()\n"
            "time.sleep(600)\n",
        ],
        stdout=subprocess.PIPE,
        start_new_session=True,
    ) as process:
        assert process.stdout is not None
        grandchild = int(process.stdout.readline())
        # Running, and outside the child's own lifetime.
        assert not _process_has_terminated(grandchild)

        assert _kill_and_reap(process) == -signal.SIGKILL

        # The grandchild is not this process's child, so it is never reaped here
        # and cannot be mistaken for gone by a `waitpid` race.
        deadline = time.monotonic() + 30
        while not _process_has_terminated(grandchild):
            assert time.monotonic() < deadline, (
                f"grandchild {grandchild} survived the group kill, so a recovery driver "
                "would have been left running on the machine"
            )
            time.sleep(0.01)
