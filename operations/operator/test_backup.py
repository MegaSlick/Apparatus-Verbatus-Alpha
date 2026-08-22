"""The local Mac backup is content-addressed, resumable, and custody-bound."""

from __future__ import annotations

import errno
import hashlib
import json
import subprocess
import sys
from pathlib import Path

import pytest

from . import backup as backup_module
from . import cli
from .backup import BackupRefusal, sync_run_tree
from .custody import credential_free_environment
from .errors import ErrorCode, OperatorError

ROOT = Path(__file__).resolve().parents[2]


def _run_tree(tmp_path: Path) -> tuple[Path, str]:
    root = tmp_path / "volume"
    run = root / "r"
    (run / "receipts" / "sha256").mkdir(parents=True)
    (run / "run.json").write_bytes(b"sealed run\n")
    (run / "receipts" / "sha256" / ("a" * 64 + ".json")).write_bytes(b"receipt\n")
    return root, "r"


def test_backup_is_content_addressed_resumable_and_verifies_each_digest(tmp_path: Path) -> None:
    volume, run_id = _run_tree(tmp_path)
    mac = tmp_path / "Mac Backup"

    first = sync_run_tree(volume, run_id, mac)
    second = sync_run_tree(volume, run_id, mac)

    assert first.copied == 2 and first.reused == 0
    assert second.copied == 0 and second.reused == 2
    snapshot = mac / "snapshots" / "sha256" / f"{first.snapshot_sha256}.json"
    record = json.loads(snapshot.read_text())
    assert record["run_id"] == run_id
    assert {row["relative_path"] for row in record["files"]} == {
        "run.json",
        "receipts/sha256/" + "a" * 64 + ".json",
    }
    assert all((mac / "objects" / "sha256" / row["sha256"]).is_file() for row in record["files"])


def test_backup_never_overwrites_an_object_with_the_wrong_digest(tmp_path: Path) -> None:
    volume, run_id = _run_tree(tmp_path)
    mac = tmp_path / "mac"
    source = volume / run_id / "run.json"
    digest = __import__("hashlib").sha256(source.read_bytes()).hexdigest()
    target = mac / "objects" / "sha256" / digest
    target.parent.mkdir(parents=True)
    target.write_bytes(b"foreign bytes")

    with pytest.raises(BackupRefusal, match="not overwritten"):
        sync_run_tree(volume, run_id, mac)

    assert target.read_bytes() == b"foreign bytes"


def test_backup_refuses_to_publish_a_snapshot_when_the_source_moves(
    tmp_path: Path, monkeypatch
) -> None:
    volume, run_id = _run_tree(tmp_path)
    mac = tmp_path / "mac"
    original = __import__("operations.operator.backup", fromlist=["_copy_verified"])._copy_verified
    changed = False

    def copy_then_change(source, target, digest):
        nonlocal changed
        result = original(source, target, digest)
        if not changed:
            changed = True
            (volume / run_id / "run.json").write_bytes(b"changed after copy\n")
        return result

    monkeypatch.setattr("operations.operator.backup._copy_verified", copy_then_change)
    with pytest.raises(BackupRefusal, match="changed while"):
        sync_run_tree(volume, run_id, mac)
    assert not list((mac / "snapshots").rglob("*.json")) if (mac / "snapshots").exists() else True


def test_backup_snapshot_publish_survives_a_kill_before_the_link(tmp_path: Path) -> None:
    """A kill between the snapshot's temp write and its final link must resume, not brick.

    `_publish_bytes` used to open the final content-addressed name directly
    (`O_CREAT | O_EXCL`) and write into it; a kill between that open and the
    write left a permanently empty file at the exact path a snapshot's own
    digest names, and every later retry read it back, found it did not match,
    and refused forever -- the one crash point in this module `_copy_verified`
    already survives. This drives an actual uncatchable SIGKILL through the
    write path in a child process, not a monkeypatched exception a `finally`
    block would still run for, and proves the fixed temp-then-link idiom
    leaves nothing a resume cannot complete.
    """
    volume, run_id = _run_tree(tmp_path)
    mac = tmp_path / "mac"

    script = (
        "import os, signal, sys\n"
        f"sys.path.insert(0, {str(ROOT)!r})\n"
        "from operations.operator import backup as backup_module\n"
        "real_link = os.link\n"
        "def guarded_link(src, dst):\n"
        "    if 'snapshots' in str(dst):\n"
        "        os.kill(os.getpid(), signal.SIGKILL)\n"
        "    return real_link(src, dst)\n"
        "os.link = guarded_link\n"
        f"backup_module.sync_run_tree({str(volume)!r}, {run_id!r}, {str(mac)!r})\n"
    )

    killed = subprocess.run(
        [sys.executable, "-c", script], cwd=ROOT, capture_output=True, text=True
    )
    assert killed.returncode == -9, (killed.returncode, killed.stdout, killed.stderr)
    assert not list((mac / "snapshots").rglob("*.json"))

    finished = sync_run_tree(volume, run_id, mac)
    published = list((mac / "snapshots" / "sha256").glob("*.json"))
    assert len(published) == 1
    record = json.loads(published[0].read_text())
    assert record["run_id"] == run_id
    assert finished.reused == 2 and finished.copied == 0


def test_backup_cli_uses_a_confined_credential_free_child(tmp_path: Path, monkeypatch) -> None:
    volume, run_id = _run_tree(tmp_path)
    mac = tmp_path / "mac"
    observed = {}

    class Backend:
        def launcher_failure(self, completed):
            return None

    class Completed:
        returncode = 0
        stdout = json.dumps(
            {"schema": "mac-run-backup.v1", "snapshot_sha256": "a" * 64, "copied": 2, "reused": 0}
        )
        stderr = ""

    def confined(command, *, writable, cwd, input_text):
        observed.update(
            {"command": command, "writable": writable, "request": json.loads(input_text)}
        )
        return Backend(), Completed()

    monkeypatch.setattr(cli, "run_confined", confined)
    cli._backup_in_custody(volume, run_id, mac, tmp_path)

    assert observed["writable"] == mac.resolve()
    assert observed["request"]["run_id"] == run_id
    assert "backup_worker" in " ".join(observed["command"])
    assert credential_free_environment({"RUNPOD_S3_SECRET_KEY": "secret", "SAFE": "yes"}) == {
        "SAFE": "yes"
    }


@pytest.mark.skipif(sys.platform != "linux", reason="the chamber proves the Landlock worker")
def test_backup_cli_executes_the_worker_inside_the_real_custody_boundary(tmp_path: Path) -> None:
    volume, run_id = _run_tree(tmp_path)
    mac = tmp_path / "mac"

    cli._backup_in_custody(volume, run_id, mac, Path(__file__).resolve().parents[2])

    assert list((mac / "objects" / "sha256").iterdir())
    assert list((mac / "snapshots" / "sha256").iterdir())


def test_backup_worker_failure_uses_the_named_three_part_operator_refusal(
    tmp_path: Path, monkeypatch
) -> None:
    volume, run_id = _run_tree(tmp_path)

    class Backend:
        def launcher_failure(self, completed):
            return None

    class Completed:
        returncode = 2
        stdout = ""
        stderr = "source changed"

    monkeypatch.setattr(cli, "run_confined", lambda *args, **kwargs: (Backend(), Completed()))
    with pytest.raises(OperatorError) as failure:
        cli._backup_in_custody(volume, run_id, tmp_path / "mac", tmp_path)
    assert failure.value.code is ErrorCode.BACKUP_FAILED
    assert failure.value.render().count("\n") >= 2


def test_backup_refuses_a_destination_inside_the_run_tree_without_writing_into_it(
    tmp_path: Path,
) -> None:
    """The sealed tree must not become its own backup store, even briefly.

    Only the after-the-fact inventory recheck used to notice this, which is to
    say it was noticed after the whole run tree had been copied into the run
    tree -- and after custody had granted that path, inside the sealed tree, as
    the one place the credential-free child could write.
    """
    volume, run_id = _run_tree(tmp_path)
    before = {
        path.relative_to(volume).as_posix(): path.read_bytes()
        for path in sorted(volume.rglob("*"))
        if path.is_file()
    }

    with pytest.raises(BackupRefusal, match="must not sit inside"):
        sync_run_tree(volume, run_id, volume / run_id / "backup")

    after = {
        path.relative_to(volume).as_posix(): path.read_bytes()
        for path in sorted(volume.rglob("*"))
        if path.is_file()
    }
    assert after == before
    assert not (volume / run_id / "backup").exists()


def test_backup_refuses_a_destination_that_holds_the_run_tree(tmp_path: Path) -> None:
    volume, run_id = _run_tree(tmp_path)
    with pytest.raises(BackupRefusal, match="must not contain"):
        sync_run_tree(volume, run_id, volume)


def test_the_overlap_check_reads_filesystem_identity_and_not_the_spelling(tmp_path: Path) -> None:
    """Two names for one directory are the same directory, whatever they read as.

    The Mac target is case-insensitive by default and `Path.resolve` does not
    correct case on macOS, so `is_relative_to` -- a comparison of spellings --
    answers False for two paths that name one directory there. That exact
    condition cannot be built on this container's case-sensitive filesystem, so
    the equivalent real fact is used: an alias that `is_relative_to` misses and
    device-and-inode catches. The macOS spelling itself is on the host checklist.
    """
    real = tmp_path / "real"
    (real / "runs" / "r").mkdir(parents=True)
    alias = tmp_path / "alias"
    alias.symlink_to(real)

    assert not (real / "runs" / "r").is_relative_to(alias)
    assert backup_module._contains(alias, real / "runs" / "r")
    assert not backup_module._contains(tmp_path / "elsewhere", real / "runs" / "r")


def test_backup_names_a_backup_directory_that_refuses_hard_links(
    tmp_path: Path, monkeypatch
) -> None:
    """An exFAT volume or an SMB share is an ordinary Mac backup directory.

    The filesystem cannot be built inside this container, so the kernel's own
    answer for one is raised at the one call that would meet it. What is being
    checked is not that `os.link` fails -- it is that the failure reaches the
    operator as the setup fact they can act on, the way `_atomic_create` already
    names the same condition for the run root, instead of as an errno about a
    temporary file nobody asked for.
    """
    volume, run_id = _run_tree(tmp_path)
    mac = tmp_path / "exfat"

    def refusing_link(source, target):
        raise OSError(errno.EOPNOTSUPP, "Operation not supported")

    monkeypatch.setattr(backup_module.os, "link", refusing_link)
    with pytest.raises(BackupRefusal, match="refuses hard links") as refusal:
        sync_run_tree(volume, run_id, mac)

    # The refusal has to name a destination the operator can change to, not only
    # report that a link failed.
    assert "exFAT" in str(refusal.value) and str(mac) in str(refusal.value)
    assert not list((mac / "objects" / "sha256").glob("[0-9a-f]*"))


def test_backup_refuses_a_snapshot_name_taken_by_a_symlink(tmp_path: Path) -> None:
    """A published snapshot is an immutable index; a link is not one.

    The bytes behind a symlink can change after the tool has called the backup
    current, so the name is refused rather than followed -- including a dangling
    link, which `Path.exists` answers False for and which therefore used to
    reach `os.link` and escape as a bare `FileNotFoundError`.
    """
    volume, run_id = _run_tree(tmp_path)
    mac = tmp_path / "mac"
    expected = sync_run_tree(volume, run_id, tmp_path / "probe").snapshot_sha256
    (mac / "snapshots" / "sha256").mkdir(parents=True)
    (mac / "snapshots" / "sha256" / f"{expected}.json").symlink_to(tmp_path / "nowhere")

    with pytest.raises(BackupRefusal, match="not a regular file"):
        sync_run_tree(volume, run_id, mac)


def test_a_backup_taken_across_a_concurrent_append_refuses_and_stays_resumable(
    tmp_path: Path, monkeypatch
) -> None:
    """What a backup taken while a stage writes gives: a prior state or a refusal.

    A run tree only ever gains members, and each is published whole by atomic
    link, so no reader can see a half-written file. The digest re-read inside
    `_copy_verified` catches a member that changed under the copy, and the
    second whole-tree inventory catches an append that landed anywhere else. So
    a snapshot either names one coherent state of the tree or is never published
    at all -- and the objects already copied stay valid, so the refusal is a
    resume rather than a loss. That is checked here by appending to the tree
    from inside the copy, which is the interleaving without the race.
    """
    volume, run_id = _run_tree(tmp_path)
    mac = tmp_path / "mac"
    original = backup_module._copy_verified
    appended = False

    def copy_then_append(source, target, digest):
        nonlocal appended
        result = original(source, target, digest)
        if not appended:
            appended = True
            (volume / run_id / "receipts" / "sha256" / ("b" * 64 + ".json")).write_bytes(b"later\n")
        return result

    monkeypatch.setattr(backup_module, "_copy_verified", copy_then_append)
    with pytest.raises(BackupRefusal, match="changed while it was being copied"):
        sync_run_tree(volume, run_id, mac)
    assert not list((mac / "snapshots" / "sha256").glob("*.json"))

    monkeypatch.undo()
    finished = sync_run_tree(volume, run_id, mac)
    published = list((mac / "snapshots" / "sha256").glob("*.json"))
    assert len(published) == 1
    record = json.loads(published[0].read_text())
    # The published snapshot names the settled tree exactly -- every member, and
    # nothing the interrupted pass left behind.
    assert {row["relative_path"] for row in record["files"]} == {
        path.relative_to(volume / run_id).as_posix()
        for path in (volume / run_id).rglob("*")
        if path.is_file()
    }
    for row in record["files"]:
        stored = mac / "objects" / "sha256" / row["sha256"]
        assert hashlib.sha256(stored.read_bytes()).hexdigest() == row["sha256"]
    # Two of the three objects were already on disk from the refused pass, so
    # the refusal cost nothing but the snapshot.
    assert finished.reused == 2 and finished.copied == 1
