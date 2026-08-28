"""The local Mac backup is content-addressed, resumable, and custody-bound."""

from __future__ import annotations

import errno
import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from . import backup as backup_module
from . import backup_worker, cli
from .backup import SCHEMA, BackupRefusal, sync_run_tree
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


def _file_bytes(root: Path) -> dict[str, bytes]:
    return {
        path.relative_to(root).as_posix(): path.read_bytes()
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }


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
    assert record["schema"] == SCHEMA
    assert record["excluded_publication_temporaries"] == []
    assert {row["relative_path"] for row in record["files"]} == {
        "run.json",
        "receipts/sha256/" + "a" * 64 + ".json",
    }
    for row in record["files"]:
        stored = mac / "objects" / "sha256" / row["sha256"]
        assert stored.is_file()
        assert hashlib.sha256(stored.read_bytes()).hexdigest() == row["sha256"]


def test_two_inventory_passes_from_one_descriptor_see_the_same_tree(tmp_path: Path) -> None:
    """`sync_run_tree` scans the anchored root twice and compares the two views.

    The passes therefore have to be independent of each other's directory
    position. `os.dup` would not be: it shares one open file description, and
    on Linux `getdents64` advances that shared offset, so the second pass
    would start at end of directory, find nothing, and refuse a backup whose
    bytes had just been copied correctly. macOS does not advance the shared
    offset, which is exactly why this asymmetry has to be asserted rather than
    left to whichever platform the suite happens to run on.
    """
    volume, run_id = _run_tree(tmp_path)
    managed = backup_module.RunTree(volume, run_id).inventory_scope()

    with backup_module._open_directory(volume / run_id, what="source run tree") as descriptor:
        first = backup_module._inventory_descriptor(descriptor, managed)
        second = backup_module._inventory_descriptor(descriptor, managed)

    assert first[0], "the first inventory pass found no files to compare"
    assert first == second


def test_backup_never_overwrites_an_object_with_the_wrong_digest(tmp_path: Path) -> None:
    volume, run_id = _run_tree(tmp_path)
    mac = tmp_path / "mac"
    source = volume / run_id / "run.json"
    digest = hashlib.sha256(source.read_bytes()).hexdigest()
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
    original = backup_module._copy_verified
    changed = False

    def copy_then_change(source_descriptor, relative, objects_descriptor, target, digest):
        nonlocal changed
        result = original(source_descriptor, relative, objects_descriptor, target, digest)
        if not changed:
            changed = True
            (volume / run_id / "run.json").write_bytes(b"changed after copy\n")
        return result

    monkeypatch.setattr("operations.operator.backup._copy_verified", copy_then_change)
    with pytest.raises(BackupRefusal, match="changed while"):
        sync_run_tree(volume, run_id, mac)
    # `Path.glob` on a missing directory yields nothing, so no existence guard
    # is needed. `assert X if cond else True` is one conditional expression and
    # passes outright whenever the directory is absent, which a later reader
    # can easily take for two assertions.
    assert not list((mac / "snapshots").rglob("*.json"))


def test_backup_snapshot_publish_survives_a_kill_before_the_link(tmp_path: Path) -> None:
    """A kill between the snapshot's temp write and its final link must resume, not brick.

    This requires an actual SIGKILL: an exception-based test would run the
    cleanup block and could not prove that the temporary-first publication
    leaves no permanent digest-named partial file.
    """
    volume, run_id = _run_tree(tmp_path)
    mac = tmp_path / "mac"

    script = (
        "import os, signal, sys\n"
        f"sys.path.insert(0, {str(ROOT)!r})\n"
        "from operations.operator import backup as backup_module\n"
        "real_link = backup_module._link_or_refuse\n"
        "def guarded_link(src, dst, directory_fd, *, target_display):\n"
        "    if 'snapshots' in target_display.parts:\n"
        "        os.kill(os.getpid(), signal.SIGKILL)\n"
        "    return real_link(src, dst, directory_fd, target_display=target_display)\n"
        "backup_module._link_or_refuse = guarded_link\n"
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

    def confined(command, *, writable, cwd, input_text):
        observed.update(
            {
                "command": command,
                "writable": writable,
                "cwd": cwd,
                "request": json.loads(input_text),
            }
        )
        report = sync_run_tree(volume, run_id, mac)

        class Completed:
            returncode = 0
            stdout = json.dumps(report.to_record())
            stderr = ""

        return Backend(), Completed()

    monkeypatch.setattr(cli, "run_confined", confined)
    cli._backup_in_custody(volume, run_id, mac, tmp_path)

    assert observed["writable"] == mac.resolve()
    assert observed["cwd"] == ROOT
    assert observed["request"]["run_id"] == run_id
    assert observed["request"]["source_identity"] == list(
        backup_module.required_identity(volume / run_id, what="source run tree")
    )
    assert observed["request"]["destination_identities"] == [
        list(identity) for identity in backup_module.destination_identities(mac)
    ]
    assert "backup_worker" in " ".join(observed["command"])
    assert str(ROOT) in observed["command"]
    assert str(tmp_path) not in observed["command"]
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


def test_backup_worker_bounds_its_request_before_json_deserialization() -> None:
    completed = subprocess.run(
        [sys.executable, "-m", "operations.operator.backup_worker"],
        cwd=ROOT,
        input=" " * (backup_worker.MAX_REQUEST_BYTES + 1),
        capture_output=True,
        text=True,
        check=False,
    )

    assert completed.returncode == 2
    assert f"larger than {backup_worker.MAX_REQUEST_BYTES} bytes" in completed.stderr
    assert completed.stdout == ""


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
    assert "source changed" in failure.value.render()


def test_backup_worker_failure_without_output_names_its_exit_status(
    tmp_path: Path, monkeypatch
) -> None:
    volume, run_id = _run_tree(tmp_path)

    class Backend:
        def launcher_failure(self, completed):
            return None

    class Completed:
        returncode = 2
        stdout = ""
        stderr = ""

    monkeypatch.setattr(cli, "run_confined", lambda *args, **kwargs: (Backend(), Completed()))
    with pytest.raises(OperatorError) as failure:
        cli._backup_in_custody(volume, run_id, tmp_path / "mac", tmp_path)

    assert "exited 2 without a diagnostic" in failure.value.render()


def test_backup_invalid_run_id_uses_the_named_backup_refusal(tmp_path: Path) -> None:
    volume, _run_id = _run_tree(tmp_path)

    with pytest.raises(OperatorError) as failure:
        cli._backup_in_custody(volume, "../escape", tmp_path / "mac", tmp_path)

    assert failure.value.code is ErrorCode.BACKUP_FAILED
    assert "run id is invalid" in failure.value.render()


# Each case names the refusal it must produce. Asserting only the shared
# phrase "backup worker report" let one surviving check answer for all five:
# delete the integer test, or the ceiling test, and the suite stayed green
# while the CLI accepted a report it cannot prove.
@pytest.mark.parametrize(
    ("report", "expected_detail"),
    (
        pytest.param(
            {"schema": "mac-run-backup.v1", "snapshot_sha256": "a" * 64, "copied": 2, "reused": 0},
            "declares schema",
            id="schema-is-not-this-backup-format",
        ),
        pytest.param(
            {"schema": SCHEMA, "snapshot_sha256": "a" * 64, "copied": "2", "reused": 0},
            "'copied' is not an integer",
            id="count-is-a-string",
        ),
        pytest.param(
            {
                "schema": SCHEMA,
                "snapshot_sha256": "a" * 64,
                "copied": backup_module.MAX_BACKUP_FILES + 1,
                "reused": 0,
            },
            "'copied' is outside",
            id="count-is-past-the-file-ceiling",
        ),
        pytest.param(
            {"schema": SCHEMA, "snapshot_sha256": "a" * 64, "copied": 0, "reused": 0},
            "successful snapshot of no files",
            id="claims-success-over-nothing",
        ),
        pytest.param(
            # Well-formed on its face, and still refused: no snapshot with that
            # digest was published, so the read-back has nothing to verify.
            {"schema": SCHEMA, "snapshot_sha256": "a" * 64, "copied": 2, "reused": 0},
            "snapshot that does not exist",
            id="names-a-snapshot-that-was-never-written",
        ),
    ),
)
def test_backup_cli_refuses_a_worker_report_that_cannot_prove_success(
    tmp_path: Path, monkeypatch, report: dict[str, object], expected_detail: str
) -> None:
    volume, run_id = _run_tree(tmp_path)

    class Backend:
        def launcher_failure(self, completed):
            return None

    class Completed:
        returncode = 0
        stdout = json.dumps(report)
        stderr = ""

    monkeypatch.setattr(cli, "run_confined", lambda *args, **kwargs: (Backend(), Completed()))

    with pytest.raises(OperatorError) as failure:
        cli._backup_in_custody(volume, run_id, tmp_path / "mac", tmp_path)

    assert failure.value.code is ErrorCode.BACKUP_FAILED
    assert expected_detail in failure.value.render()


def test_backup_cli_classifies_a_worker_report_json_conversion_failure(
    tmp_path: Path, monkeypatch
) -> None:
    volume, run_id = _run_tree(tmp_path)

    class Backend:
        def launcher_failure(self, completed):
            return None

    class Completed:
        returncode = 0
        stdout = '{"copied":' + "9" * 5000 + "}"
        stderr = ""

    monkeypatch.setattr(cli, "run_confined", lambda *args, **kwargs: (Backend(), Completed()))
    with pytest.raises(OperatorError) as failure:
        cli._backup_in_custody(volume, run_id, tmp_path / "mac", tmp_path)

    assert failure.value.code is ErrorCode.BACKUP_FAILED
    assert "integer string conversion" in failure.value.render()


def test_backup_custody_refusal_names_a_worker_and_partial_backup_state(
    tmp_path: Path, monkeypatch
) -> None:
    volume, run_id = _run_tree(tmp_path)

    def refuse(*args, **kwargs):
        raise OperatorError(ErrorCode.CONSOLE_CUSTODY_REFUSED, detail="Landlock unavailable")

    monkeypatch.setattr(cli, "run_confined", refuse)
    with pytest.raises(OperatorError) as failure:
        cli._backup_in_custody(volume, run_id, tmp_path / "mac", tmp_path)

    rendered = failure.value.render()
    assert failure.value.code is ErrorCode.CONSOLE_CUSTODY_REFUSED
    assert "custody worker" in rendered
    assert "backup may have added verified objects" in rendered


def test_backup_refuses_a_destination_inside_the_run_tree_without_writing_into_it(
    tmp_path: Path,
) -> None:
    """Overlap must be refused before layout creation mutates the sealed source."""
    volume, run_id = _run_tree(tmp_path)
    before = _file_bytes(volume)

    with pytest.raises(BackupRefusal, match="must not sit inside"):
        sync_run_tree(volume, run_id, volume / run_id / "backup")

    assert _file_bytes(volume) == before
    assert not (volume / run_id / "backup").exists()


def test_backup_refuses_a_destination_that_holds_the_run_tree(tmp_path: Path) -> None:
    volume, run_id = _run_tree(tmp_path)
    with pytest.raises(BackupRefusal, match="must not contain"):
        sync_run_tree(volume, run_id, volume)


def test_backup_layout_symlink_cannot_redirect_writes_into_the_source(tmp_path: Path) -> None:
    volume, run_id = _run_tree(tmp_path)
    source = volume / run_id
    before = _file_bytes(source)
    mac = tmp_path / "mac"
    mac.mkdir()
    (mac / "objects").symlink_to(source, target_is_directory=True)

    with pytest.raises(BackupRefusal, match="symbolic link"):
        sync_run_tree(volume, run_id, mac)

    assert _file_bytes(source) == before
    assert not (source / "sha256").exists()
    assert not (mac / "snapshots").exists()


def test_backup_refuses_when_the_selected_destination_itself_is_a_symlink(
    tmp_path: Path,
) -> None:
    volume, run_id = _run_tree(tmp_path)
    redirected = tmp_path / "redirected"
    redirected.mkdir()
    selected = tmp_path / "Mac Backup"
    selected.symlink_to(redirected, target_is_directory=True)

    with pytest.raises(BackupRefusal, match="symbolic link"):
        sync_run_tree(volume, run_id, selected)

    assert not (redirected / "objects").exists()
    assert not (redirected / "snapshots").exists()


def test_backup_leaf_swap_cannot_redirect_a_read_outside_the_run_tree(
    tmp_path: Path, monkeypatch
) -> None:
    volume, run_id = _run_tree(tmp_path)
    source = volume / run_id / "run.json"
    original_source = source.with_name("run-original.json")
    outside = tmp_path / "outside-secret"
    outside.write_bytes(b"must not enter backup")
    real_open = backup_module._open_regular_descriptor
    swapped = False

    def swap_before_open(name, parent_descriptor, *, what):
        nonlocal swapped
        if name == "run.json" and not swapped:
            swapped = True
            source.rename(original_source)
            source.symlink_to(outside)
        return real_open(name, parent_descriptor, what=what)

    monkeypatch.setattr(backup_module, "_open_regular_descriptor", swap_before_open)

    with pytest.raises(BackupRefusal, match="without following"):
        sync_run_tree(volume, run_id, tmp_path / "mac")

    assert not list((tmp_path / "mac" / "snapshots" / "sha256").glob("*.json"))


def test_backup_refuses_paths_that_collapse_on_default_apfs(tmp_path: Path) -> None:
    volume, run_id = _run_tree(tmp_path)
    source = volume / run_id
    (source / "Alpha").mkdir()
    try:
        (source / "alpha").mkdir()
    except FileExistsError:
        # This host filesystem is itself case-insensitive, so two case-variant
        # source directories cannot even be planted (the volume-hosted source
        # this guard protects is case-sensitive). Prove the refusal at its own
        # seam instead: the spelling registry is what the sync walk feeds.
        from operations.operator.backup import _record_mac_spelling

        spellings: dict[str, str] = {}
        _record_mac_spelling("Alpha/one.json", spellings)
        with pytest.raises(BackupRefusal, match="collide on default APFS"):
            _record_mac_spelling("alpha/two.json", spellings)
        return
    (source / "Alpha" / "one.json").write_bytes(b"one")
    (source / "alpha" / "two.json").write_bytes(b"two")

    with pytest.raises(BackupRefusal, match="collide on default APFS"):
        sync_run_tree(volume, run_id, tmp_path / "mac")

    assert not list((tmp_path / "mac" / "snapshots" / "sha256").glob("*.json"))


def test_backup_child_refuses_a_source_with_a_different_parent_observed_identity(
    tmp_path: Path,
) -> None:
    volume, run_id = _run_tree(tmp_path)
    mac = tmp_path / "mac"

    with pytest.raises(BackupRefusal, match="changed filesystem identity"):
        sync_run_tree(
            volume,
            run_id,
            mac,
            expected_source_identity=(0, 0),
        )

    assert not list((mac / "snapshots" / "sha256").glob("*.json"))


def test_backup_preserves_a_run_tree_contract_refusal_as_a_named_backup_refusal(
    tmp_path: Path, monkeypatch
) -> None:
    volume, run_id = _run_tree(tmp_path)
    mac = tmp_path / "mac"

    class RefusingRunTree:
        def __init__(self, *args, **kwargs):
            raise backup_module.ContractError("run tree identity drifted")

    monkeypatch.setattr(backup_module, "RunTree", RefusingRunTree)

    with pytest.raises(BackupRefusal, match="could not be bound.*identity drifted"):
        sync_run_tree(volume, run_id, mac)

    assert not list((mac / "snapshots" / "sha256").glob("*.json"))


def test_backup_child_refuses_a_replaced_layout_directory_identity(tmp_path: Path) -> None:
    volume, run_id = _run_tree(tmp_path)
    mac = tmp_path / "mac"
    source, destination = backup_module.resolve_backup_paths(volume, run_id, mac)
    backup_module.prepare_backup_layout(source, destination)
    expected = backup_module.destination_identities(destination)
    object_store = mac / "objects" / "sha256"
    object_store.rename(mac / "objects" / "sha256-before-swap")
    object_store.mkdir()

    with pytest.raises(BackupRefusal, match="layout changed filesystem identity"):
        sync_run_tree(
            volume,
            run_id,
            mac,
            expected_destination_identities=expected,
        )

    assert not list((mac / "snapshots" / "sha256").glob("*.json"))


def test_backup_refuses_an_oversized_snapshot_before_publication(
    tmp_path: Path, monkeypatch
) -> None:
    volume, run_id = _run_tree(tmp_path)
    mac = tmp_path / "mac"
    monkeypatch.setattr(backup_module, "MAX_SNAPSHOT_BYTES", 1)

    with pytest.raises(BackupRefusal, match="larger than 1 bytes"):
        sync_run_tree(volume, run_id, mac)

    assert not list((mac / "snapshots" / "sha256").glob("*.json"))


def test_backup_bounds_source_entry_count_before_publication(tmp_path: Path, monkeypatch) -> None:
    volume, run_id = _run_tree(tmp_path)
    mac = tmp_path / "mac"
    monkeypatch.setattr(backup_module, "MAX_BACKUP_ENTRIES", 1)

    with pytest.raises(BackupRefusal, match="more than 1 entries"):
        sync_run_tree(volume, run_id, mac)

    assert not list((mac / "snapshots" / "sha256").glob("*.json"))


def test_backup_excludes_but_records_run_tree_publication_temporaries(tmp_path: Path) -> None:
    volume, run_id = _run_tree(tmp_path)
    temporary = volume / run_id / ".run.json.tmp-interrupted"
    temporary.write_bytes(b"unpublished authority bytes")

    report = sync_run_tree(volume, run_id, tmp_path / "mac")
    snapshot = tmp_path / "mac" / "snapshots" / "sha256" / f"{report.snapshot_sha256}.json"
    record = json.loads(snapshot.read_text())

    assert record["excluded_publication_temporaries"] == [temporary.name]
    assert temporary.name not in {row["relative_path"] for row in record["files"]}
    assert report.copied == 2


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

    def refusing_link(source, target, **kwargs):
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


def test_backup_reads_a_new_snapshot_back_before_reporting_success(
    tmp_path: Path, monkeypatch
) -> None:
    volume, run_id = _run_tree(tmp_path)
    mac = tmp_path / "mac"
    real_link = backup_module.os.link

    def link_then_corrupt(source, target, **kwargs):
        real_link(source, target, **kwargs)
        if target.endswith(".json"):
            descriptor = os.open(target, os.O_WRONLY | os.O_TRUNC, dir_fd=kwargs["dst_dir_fd"])
            try:
                os.write(descriptor, b"corrupt after link")
            finally:
                os.close(descriptor)

    monkeypatch.setattr(backup_module.os, "link", link_then_corrupt)

    with pytest.raises(BackupRefusal, match="did not verify after publication"):
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

    def copy_then_append(source_descriptor, relative, objects_descriptor, target, digest):
        nonlocal appended
        result = original(source_descriptor, relative, objects_descriptor, target, digest)
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
    assert {row["relative_path"] for row in record["files"]} == {
        path.relative_to(volume / run_id).as_posix()
        for path in (volume / run_id).rglob("*")
        if path.is_file()
    }
    for row in record["files"]:
        stored = mac / "objects" / "sha256" / row["sha256"]
        assert hashlib.sha256(stored.read_bytes()).hexdigest() == row["sha256"]
    assert finished.reused == 2 and finished.copied == 1
