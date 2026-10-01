"""Mounted-volume mobility and crash recovery must use real subprocesses."""

from __future__ import annotations

import hashlib
import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path
from typing import Iterator, cast

from common.durability import is_temporary_name
from common.runtree.store import RunTree
from conftest import file_digest_snapshot as snapshot
from conftest import file_identities, is_immutable_evidence
from operations.operator.backup import _is_publication_temporary, sync_run_tree

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
# Measured on `synthetic-two-page-v0`, `page-review` scenario: a complete run
# resolves 490 references, and the smallest tree this helper is asked about --
# the staged door..attestatores tree the crash test starts from -- resolves 211.
# The floor sits below that and far above zero, so it catches a class leaving
# the check without tracking every ordinary change in fixture size.
MINIMUM_RESOLVED_REFERENCES = 200


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


def _published_reading_count(root: Path, run_id: str) -> int:
    """Count the Perlector's published records while the driver is still writing them.

    A listing, not an inspection: the poll below runs it repeatedly against a
    tree being written, so it reads names only and skips the `.<target>.tmp-*`
    publication temporaries a write leaves until its rename lands. The
    whole-tree reference check runs separately, once, after the crash.
    """
    artifacts = root / run_id / "4_perlector" / "artifacts"
    if not artifacts.is_dir():
        return 0
    return sum(
        not is_temporary_name(name)
        for kind in os.listdir(artifacts)
        for name in os.listdir(artifacts / kind)
    )


def _kill_and_reap(process: subprocess.Popen[bytes]) -> int:
    """Leave no driver process group behind, including on an assertion path."""

    if process.poll() is None:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            # The process group exited between poll and kill; wait still reaps
            # the child and reports the state that won that race.
            pass
    return process.wait(timeout=120)


def _crash_mid_reading(volume: Path, scratch: Path) -> dict[str, str]:
    """SIGKILL a real Perlector only after its first append.

    A local directory stands in for the offline mount; the returned snapshot is
    the durable state available to resume.
    """

    staged = _run(volume, "r", "page-review", "--from", "door", "--to", "attestatores")
    assert staged.returncode == 0, staged.stderr
    before_crash = snapshot(volume)

    # Kill the driver's process group only after the Perlector has published its
    # first record, while it is still reading; the rest of its records and every
    # later stage's therefore cannot occur.
    #
    # The driver's output goes to files rather than pipes.  Nothing drains a
    # pipe between Popen and the kill, so a driver that filled the 64 KiB pipe
    # buffer would block in `write` and never reach the append this loop is
    # waiting for -- a stall the test would have reported as a missing record.
    out_path = scratch / "reading.out"
    err_path = scratch / "reading.err"
    with out_path.open("wb") as out_handle, err_path.open("wb") as err_handle:
        process = subprocess.Popen(
            [
                sys.executable,
                str(ORCHESTRATOR),
                "--fixture",
                FIXTURE,
                "--scenario",
                "page-review",
                "--run-id",
                "r",
                "--run-root",
                str(volume),
                "--stage",
                "perlector",
            ],
            cwd=ROOT,
            stdout=out_handle,
            stderr=err_handle,
            start_new_session=True,
        )
    # The kill window is *observed*, never timed.  This loop ends on one of two
    # facts about the driver -- a Perlector record is on disk, or the driver has
    # exited -- and on neither a clock nor a sleep.  The window widens with load
    # rather than closing, so the absolute bound below is a hang guard,
    # deliberately far larger than any load this window scales to; it is not
    # the window.
    hang_guard = time.monotonic() + 600
    try:
        while _published_reading_count(volume, "r") == 0:
            if process.poll() is not None:
                raise AssertionError(
                    "the Perlector exited before it published a record:\n"
                    f"{err_path.read_text()}\n{out_path.read_text()}"
                )
            assert time.monotonic() < hang_guard, "the driver neither appended nor exited"
            time.sleep(0.005)
        assert process.poll() is None, "the driver finished before the crash point"
    except BaseException:
        _kill_and_reap(process)
        raise
    # The bound guards against an unreapable child; it does not extend the
    # reading work window.
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
    recorded digest, a SIGKILL mid-reading leaves a resumable tree, and the
    resume rewrites no surviving evidence -- it finishes to the same bytes an
    uninterrupted local run produces.
    """
    local = tmp_path / "local-runs"
    volume = tmp_path / "mounted-volume" / "runs"
    baseline = _run(local, "r", "page-review")
    assert baseline.returncode == 3, baseline.stderr
    uninterrupted = snapshot(local)

    crashed = _crash_mid_reading(volume, tmp_path)
    crashed_identities = file_identities(volume)
    planted = _plant_publication_temporary(volume)

    resumed = _run(volume, "r", "page-review")
    assert resumed.returncode == 3, resumed.stderr
    finished = snapshot(volume)
    finished_identities = file_identities(volume)
    for path, digest in crashed.items():
        if not is_immutable_evidence(path):
            # A legitimate append rebuilds derived inventories and current-state
            # receipts; the immutable evidence they name must retain its bytes.
            # The predicate lives beside `file_identities` so that this test and
            # the acceptance resume tests cannot drift on where the line is.
            continue
        # Membership first. A deleted evidence file is the worst outcome this
        # test can find -- an act removed from a parish, with nothing
        # downstream able to say it happened -- and `finished[path]` alone
        # would report it as a bare KeyError that reads like a broken test.
        assert path in finished, f"resume deleted surviving evidence at {path}"
        assert finished[path] == digest, f"resume rewrote surviving evidence at {path}"
        # The digest above cannot see a file republished with the same bytes,
        # which is exactly what "appends without rewriting" denies. Publication
        # always mints a new inode, so an unchanged one is the direct evidence.
        assert finished_identities[path] == crashed_identities[path], (
            f"resume republished surviving evidence at {path} instead of keeping it"
        )
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
    # The resumed Perlector starts its interrupted attempt over and republishes
    # nothing that survived, so every published byte matches the uninterrupted run.
    assert finished_published == uninterrupted
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


def test_a_backup_of_a_mid_reading_tree_restores_and_resumes_byte_identically(
    tmp_path: Path,
) -> None:
    """A mid-reading backup must restore and resume byte-identically.

    The snapshot must publish for an interrupted tree, restore the crashed bytes
    exactly, and let both the source and restored copy reach the same result.
    """
    volume = tmp_path / "mounted-volume" / "runs"
    _crash_mid_reading(volume, tmp_path)

    # Whether the kill lands mid-write, leaving a `.<target>.tmp-*` publication
    # temporary behind, is a matter of timing -- so whether this test exercised
    # the backup's exclusion of them was decided by the scheduler. It failed
    # exactly once in a full-suite run for that reason. One is planted, so the
    # exclusion path is measured on every run instead of occasionally.
    planted = volume / "r" / ".run.json.tmp-interruptedpublication"
    planted.write_bytes(b"an interrupted publication, mid-write when the driver died")

    # A name shaped exactly like a publication temporary, `.<target>.tmp-<unique>`,
    # is not enough: the target it names must also resolve inside
    # `RunTree.inventory_scope()`, or the exclusion could carry off a file the
    # backup owed a person. `_is_publication_temporary_name` below tests the shape
    # alone and would wrongly accept this one, so the scope check is what has to
    # catch it.
    out_of_scope_target = "not-a-managed-run-tree-path"
    assert out_of_scope_target not in RunTree(volume, "r").inventory_scope()
    unmanaged = volume / "r" / f".{out_of_scope_target}.tmp-outsidescope"
    unmanaged.write_bytes(b"a temp-shaped name for a path this store never manages")
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
    assert f"{run}/{unmanaged.name}" not in excluded, (
        "a temp-shaped name whose target lies outside inventory_scope() was excluded anyway"
    )
    assert f"{run}/{unmanaged.name}" in published
    # Distinct run-tree paths may share verified bytes, so copied plus reused --
    # not copied alone -- is what must account for every published member.
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
    # The scope probe above has made its point; carrying its published file into
    # the resumed run is not part of what this test measures from here.
    unmanaged.unlink()
    (restored_root / "r" / unmanaged.name).unlink()
    source_residue_path = _plant_publication_temporary(volume)

    restored_run = _run(restored_root, "r", "page-review")
    assert restored_run.returncode == 3, restored_run.stderr
    source_run = _run(volume, "r", "page-review")
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
    return is_temporary_name(Path(relative).name)


def _restore(mac: Path, snapshot_sha256: str, destination: Path) -> None:
    """Test-only: production restore must have its own run-tree custody boundary."""
    record = _snapshot_manifest(mac, snapshot_sha256)
    for row in record["files"]:
        data = (mac / "objects" / "sha256" / row["sha256"]).read_bytes()
        assert hashlib.sha256(data).hexdigest() == row["sha256"], row
        target = destination / record["run_id"] / row["relative_path"]
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
