"""The operator boundary may read sealed evidence and append only one record shape."""

from __future__ import annotations

import inspect
import io
import json
import os
import subprocess
import sys
import types
import zipfile
from pathlib import Path

import pytest

from common.contracts.approval import ApprovalRecordReference
from common.contracts.canonical import canonical_bytes, digest_bytes, self_hash
from common.contracts.errors import ApprovalRefusal
from common.contracts.identities import artifact_id
from common.contracts.stages import ARMARIUM
from common.runtree.store import RunTree
from operations.operator import advance, advance_worker, cli, console, custody, review
from operations.operator.errors import ErrorCode, OperatorError
from operations.operator.review import ReviewProjection

ROOT = Path(__file__).resolve().parents[2]
ORCHESTRATOR = ROOT / "pipeline" / "orchestrator" / "run.py"


def _make_run(tmp_path: Path) -> tuple[Path, str]:
    run_root = tmp_path / "runs"
    completed = subprocess.run(
        [
            sys.executable,
            str(ORCHESTRATOR),
            "--fixture",
            "synthetic-two-page-v0",
            "--scenario",
            "happy",
            "--run-id",
            "reviewed",
            "--run-root",
            str(run_root),
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
    return run_root, "reviewed"


def _boundary_digest(run_root: Path, run_id: str, stage: str = "armarium") -> str:
    return advance.sealed_boundary(RunTree(run_root, run_id), stage)[1]


def _worker_identity_arguments(tree: RunTree) -> list[str]:
    run_identity = advance.directory_identity(tree.root, "test run tree")
    receipt_identity = advance.receipt_directory_identity(tree.root, run_identity, create=False)
    return [
        "--run-device",
        str(run_identity[0]),
        "--run-inode",
        str(run_identity[1]),
        "--receipt-device",
        str(receipt_identity[0]),
        "--receipt-inode",
        str(receipt_identity[1]),
    ]


def test_read_surface_projects_seals_census_pages_crops_and_optional_review_shape(tmp_path: Path):
    run_root, run_id = _make_run(tmp_path)

    projected = review.ReadOnlyRun(run_root, run_id).projection()

    assert {row["stage"] for row in projected.boundaries} == {
        "door",
        "exemplar",
        "ink-map",
        "designator",
        "attestatores",
        "perlector",
        "recensor",
        "archetypus",
        "armarium",
    }
    assert all(row["sealed"] and len(row["seal_digest"]) == 64 for row in projected.boundaries)
    assert all("census" in row for row in projected.boundaries)
    assert len(projected.pages) == 2
    assert all(
        row["image_data_url"].startswith("data:image/png;base64,") for row in projected.pages
    )
    assert all(act["crops"] for act in projected.acts)
    # A complete fixture produces no review rows, but the shape remains visible
    # when Armarium includes it rather than being inferred from an empty outcome.
    assert projected.review_items in (None, ())


def test_unsealed_boundary_is_refused_before_an_advance_record_is_written(tmp_path: Path):
    tree = RunTree.create(
        tmp_path,
        "unsealed",
        source_manifest=[{"relative_path": "page.png", "sha256": "a" * 64, "ordinal": 1}],
        config_digest="b" * 64,
        adapter_recipes={"designator": "fixture"},
        witness_chairs=["attestator_1"],
    )

    with pytest.raises(ApprovalRefusal, match="no stored stage-seal"):
        advance.record_advance(tree, "designator", reason="reviewed")

    assert not (tree.root / "receipts").exists()


def test_external_trigger_refuses_an_unsealed_boundary_before_creating_its_write_path(tmp_path):
    tree = RunTree.create(
        tmp_path,
        "unsealed-trigger",
        source_manifest=[{"relative_path": "page.png", "sha256": "a" * 64, "ordinal": 1}],
        config_digest="b" * 64,
        adapter_recipes={"designator": "fixture"},
        witness_chairs=["attestator_1"],
    )

    with pytest.raises(OperatorError) as excinfo:
        advance.trigger_advance(
            tmp_path,
            tree.run_id,
            "designator",
            reason="not actually sealed",
            workspace=ROOT,
            expected_digest="c" * 64,
        )

    assert excinfo.value.code == ErrorCode.ADVANCE_REFUSED
    assert not (tree.root / "receipts").exists()


def test_the_record_writer_refuses_a_missing_digest_before_any_record_is_written(tmp_path):
    run_root, run_id = _make_run(tmp_path)
    tree = RunTree(run_root, run_id)
    before = {path.name for path in (tree.root / "receipts" / "sha256").glob("*.json")}

    with pytest.raises(ApprovalRefusal, match="no reviewed stage-seal digest"):
        advance.record_advance(tree, "armarium", reason="there was no boundary digest to bind")

    assert {path.name for path in (tree.root / "receipts" / "sha256").glob("*.json")} == before


def test_the_external_trigger_refuses_an_advance_that_names_no_reviewed_digest(tmp_path):
    """Both sides of the worker boundary refuse, and the outer one has no default.

    `expected_digest` is keyword-only with no default, so the ordinary way to
    omit it does not compile a call at all. This covers the other way in: a
    caller that passes the value through and hands over `None` or an empty
    string. Either would otherwise bind the advance to whatever seal happened
    to be current, which is the substitution the typed confirmation exists to
    prevent.
    """
    run_root, run_id = _make_run(tmp_path)
    tree = RunTree(run_root, run_id)
    before = {path.name for path in (tree.root / "receipts" / "sha256").glob("*.json")}

    for absent in (None, ""):
        with pytest.raises(OperatorError) as excinfo:
            advance.trigger_advance(
                run_root,
                run_id,
                "armarium",
                reason="no digest was ever confirmed",
                workspace=ROOT,
                expected_digest=absent,
            )
        assert excinfo.value.code == ErrorCode.ADVANCE_REFUSED
        assert "no reviewed stage-seal digest" in (excinfo.value.detail or "")

    assert {path.name for path in (tree.root / "receipts" / "sha256").glob("*.json")} == before


def test_an_explicitly_blank_advance_timestamp_is_refused_not_replaced_with_now(tmp_path):
    run_root, run_id = _make_run(tmp_path)
    tree = RunTree(run_root, run_id)
    before = {path.name for path in (tree.root / "receipts" / "sha256").glob("*.json")}

    with pytest.raises(ApprovalRefusal, match="no timestamp"):
        advance.record_advance(
            tree,
            "armarium",
            reason="reviewed",
            timestamp="",
            expected_digest=_boundary_digest(run_root, run_id),
        )

    assert {path.name for path in (tree.root / "receipts" / "sha256").glob("*.json")} == before


def test_advance_worker_is_external_and_binds_the_current_seal_digest(tmp_path: Path):
    run_root, run_id = _make_run(tmp_path)
    tree = RunTree(run_root, run_id)
    before = {path.name for path in (tree.root / "receipts" / "sha256").glob("*.json")}
    _, observed_digest = advance.sealed_boundary(tree, "armarium")

    reference = advance.trigger_advance(
        run_root,
        run_id,
        "armarium",
        reason="operator reviewed the sealed Armarium boundary",
        workspace=ROOT,
        expected_digest=_boundary_digest(run_root, run_id),
    )

    after = {path.name for path in (tree.root / "receipts" / "sha256").glob("*.json")}
    assert len(after - before) == 1
    record = advance.verify_advance(tree, "armarium", reference)
    seal, digest = advance.sealed_boundary(tree, "armarium")
    assert record["subject_ids"] == ["stage-boundary:armarium"]
    assert record["target_version_hash"] == digest
    assert digest == digest_bytes(
        tree.read_bytes(tree.artifact_path("armarium", "stage-seal", seal["artifact_id"]))
    )
    assert "run_confined" in inspect.getsource(advance.trigger_advance)


# Seatbelt grants file reads but denies the executable libffi trampolines that
# `ctypes` needs. Seal verification reaches `common.stage._decode_environment`,
# whose `pypdfium2` import reaches `ctypes`; a Linux chamber cannot exercise
# Seatbelt itself, so these tests reproduce that dependency boundary.
_DENIED_UNDER_SEATBELT = ("ctypes", "_ctypes", "pypdfium2")

_BLOCKED_IMPORT_PRELUDE = """
import sys

DENIED = frozenset({denied!r})


class _Denied:
    def find_spec(self, name, path=None, target=None):
        if name.split(".")[0] in DENIED:
            raise ImportError(name + " is denied here, as Seatbelt denies it to the worker")
        return None


sys.path.insert(0, sys.argv[1])
sys.meta_path.insert(0, _Denied())
"""


def _without_seatbelt_denied_imports(
    body: str, *arguments: str, stdin: str = ""
) -> subprocess.CompletedProcess:
    """Chambers emulate only the import dependency that native Seatbelt denies."""

    source = _BLOCKED_IMPORT_PRELUDE.format(denied=_DENIED_UNDER_SEATBELT) + body
    return subprocess.run(
        [sys.executable, "-c", source, str(ROOT), *arguments],
        cwd=ROOT,
        input=stdin,
        capture_output=True,
        text=True,
        check=False,
    )


def test_the_confined_worker_completes_an_advance_without_the_imports_seatbelt_denies(tmp_path):
    """The worker's whole path must not touch `ctypes`; the checker's path does.

    Both halves are asserted here because either alone is misleading. The
    worker succeeding proves nothing if the blocker never blocks anything, and
    the checker failing proves nothing about where verification now runs.
    Together they say: the operation that fails under Seatbelt is real, it is
    on the verification path, and the verification path is no longer inside
    the confined process.
    """
    run_root, run_id = _make_run(tmp_path)
    tree = RunTree(run_root, run_id)
    before = {path.name for path in (tree.root / "receipts" / "sha256").glob("*.json")}
    request = json.dumps(
        {
            "stage": "armarium",
            "reason": "reviewed with the denied imports unavailable",
            "expected_digest": _boundary_digest(run_root, run_id),
        }
    )

    worker = _without_seatbelt_denied_imports(
        "from operations.operator.advance_worker import main\nsys.exit(main(sys.argv[2:]))\n",
        "--run-root",
        str(run_root),
        "--run-id",
        run_id,
        *_worker_identity_arguments(tree),
        stdin=request,
    )
    assert worker.returncode == 0, worker.stderr
    reference = ApprovalRecordReference(**json.loads(worker.stdout))
    assert {path.name for path in (tree.root / "receipts" / "sha256").glob("*.json")} - before
    assert advance.verify_advance(tree, "armarium", reference)["reason"] == (
        "reviewed with the denied imports unavailable"
    )

    checker = _without_seatbelt_denied_imports(
        "from common.runtree.store import RunTree\n"
        "from operations.operator.advance import verify_sealed_boundary\n"
        "verify_sealed_boundary(RunTree(sys.argv[2], sys.argv[3]), sys.argv[4])\n",
        str(run_root),
        run_id,
        "armarium",
    )
    assert checker.returncode != 0
    assert "pypdfium2 is denied here" in checker.stderr


def test_the_advance_worker_refuses_an_oversized_request_before_opening_a_tree(
    tmp_path, monkeypatch
):
    oversized = "x" * (advance.MAX_ADVANCE_REQUEST_CHARACTERS + 1)
    monkeypatch.setattr(advance_worker.sys, "stdin", io.StringIO(oversized))

    result = advance_worker.main(
        [
            "--run-root",
            str(tmp_path / "must-not-be-opened"),
            "--run-id",
            "bounded",
            "--run-device",
            "1",
            "--run-inode",
            "1",
            "--receipt-device",
            "1",
            "--receipt-inode",
            "1",
        ]
    )

    assert result == 2
    assert not (tmp_path / "must-not-be-opened").exists()


def test_an_advance_reason_is_bounded_before_it_can_amplify_a_record():
    with pytest.raises(ApprovalRefusal, match="reason exceeds"):
        advance.validate_advance_reason("x" * (advance.MAX_ADVANCE_REASON_CHARACTERS + 1))


def test_receipt_setup_does_not_create_through_a_planted_parent_symlink(tmp_path):
    run = tmp_path / "run"
    outside = tmp_path / "outside"
    run.mkdir()
    outside.mkdir()
    (run / "receipts").symlink_to(outside, target_is_directory=True)
    identity = advance.directory_identity(run, "test run")

    with pytest.raises(ApprovalRefusal, match="could not be opened"):
        advance.receipt_directory_identity(run, identity, create=True)

    assert not (outside / "sha256").exists()


def test_receipt_setup_refuses_case_variant_collisions_before_default_apfs_can_merge_them(
    tmp_path,
):
    run = tmp_path / "run"
    run.mkdir()
    (run / "Receipts").mkdir()
    identity = advance.directory_identity(run, "test run")

    with pytest.raises(ApprovalRefusal, match="collides by case"):
        advance.receipt_directory_identity(run, identity, create=True)

    assert os.listdir(run) == ["Receipts"]


def test_the_advance_worker_refuses_a_substituted_run_tree_identity(tmp_path, monkeypatch):
    run_root, run_id = _make_run(tmp_path)
    tree = RunTree(run_root, run_id)
    before = {path.name for path in (tree.root / "receipts" / "sha256").glob("*.json")}
    monkeypatch.setattr(
        advance_worker.sys,
        "stdin",
        io.StringIO(
            json.dumps(
                {
                    "stage": "armarium",
                    "reason": "the pathname now names another directory object",
                    "expected_digest": _boundary_digest(run_root, run_id),
                }
            )
        ),
    )
    identity_args = _worker_identity_arguments(tree)
    identity_args[3] = str(int(identity_args[3]) + 1)

    assert (
        advance_worker.main(["--run-root", str(run_root), "--run-id", run_id, *identity_args]) == 2
    )
    assert {path.name for path in (tree.root / "receipts" / "sha256").glob("*.json")} == before


def test_a_boundary_that_stopped_verifying_is_refused_before_the_worker_is_launched(
    tmp_path, monkeypatch
):
    """Invalid evidence must refuse in the parent before writable worker launch.

    Deleting a witnessed artifact leaves the stage-seal's own bytes untouched,
    so the reviewed digest still matches and the digest equality both sides
    bind would pass this through. Only verification catches it — which is why
    the parent must run it, and must run it before the worker exists and
    before the one directory the worker may write into is created.
    """
    run_root, run_id = _make_run(tmp_path)
    tree = RunTree(run_root, run_id)
    reviewed_digest = _boundary_digest(run_root, run_id, "attestatores")
    receipts_before = {path.name for path in (tree.root / "receipts" / "sha256").glob("*.json")}
    witnessed = next(
        row
        for row in tree.build_manifest("attestatores", verify_inputs=False)["artifacts"]
        if row["kind"] not in {"stage-seal", "decode-environment"}
    )
    tree.resolve(witnessed["relative_path"]).unlink()

    # The seal bytes did not move, so the digest the operator confirmed is
    # still the current one: nothing about equality refuses this advance.
    assert advance.stored_boundary(tree, "attestatores")[1] == reviewed_digest

    def _never(*_args, **_kwargs):
        raise AssertionError("the worker was launched for a boundary that no longer verifies")

    monkeypatch.setattr(advance, "run_confined", _never)

    with pytest.raises(OperatorError) as excinfo:
        advance.trigger_advance(
            run_root,
            run_id,
            "attestatores",
            reason="the seal no longer witnesses what is on disk",
            workspace=ROOT,
            expected_digest=reviewed_digest,
        )

    assert excinfo.value.code == ErrorCode.ADVANCE_REFUSED
    assert "no longer verifies against the run tree" in (excinfo.value.detail or "")
    assert {
        path.name for path in (tree.root / "receipts" / "sha256").glob("*.json")
    } == receipts_before


def test_write_approval_record_has_exactly_one_direct_production_spelling() -> None:
    """One direct spelling: only `advance.record_advance` names this writer.

    `RunTree.write_approval_record` is the generic append-only writer several
    approval subjects share (Lectio nuda sampling, the Perlector's prior-draft
    instrument, and this unit's stage-boundary advance). A second production
    spelling would be a second, unaudited route to minting one of those durable
    records. This spelling-level guard does not claim to prove the absence of
    dynamic dispatch; the authority-shape tests below separately constrain the
    imports and actions available to operator modules. Test files legitimately
    construct approval records directly to set up fixtures and are excluded.
    """
    root = Path(__file__).resolve().parents[2]
    definition = root / "common" / "runtree" / "store.py"
    call_sites = set()
    # Tracked files only: a bare rglob also sweeps stray local checkouts
    # (nested git worktrees under .claude/worktrees/), which are not
    # production call sites and made this scan fail on machines that have them.
    tracked = subprocess.run(
        ["git", "ls-files", "*.py"], cwd=root, capture_output=True, text=True, check=True
    ).stdout.splitlines()
    for path in sorted(root / name for name in tracked):
        if path.name.startswith("test_") or path == definition:
            continue
        for line in path.read_text(encoding="utf-8").splitlines():
            if "write_approval_record(" in line:
                call_sites.add(str(path.relative_to(root)))
                break
    assert call_sites == {"operations/operator/advance.py"}


def test_console_process_cannot_import_writers_or_keep_provider_credentials(tmp_path: Path):
    source = inspect.getsource(console)
    assert "RunTree" not in source
    assert "write_approval_record" not in source
    assert "build_approval_record" not in source
    assert "trigger_advance" not in source
    assert custody.credential_free_environment({"RUNPOD_API_KEY": "secret", "SAFE": "yes"}) == {
        "SAFE": "yes"
    }

    if sys.platform != "linux":
        return
    target = tmp_path / "evidence-mutation.txt"
    blocked = subprocess.run(
        custody.landlock_command(
            [
                sys.executable,
                "-c",
                "from pathlib import Path; Path(__import__('sys').argv[1]).write_text('no')",
                str(target),
            ]
        ),
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert blocked.returncode != 0
    assert not target.exists()


def test_console_rejects_actual_process_arguments(monkeypatch):
    monkeypatch.setattr(console.sys, "argv", ["verbatus-review", "/a/run/tree"])

    with pytest.raises(SystemExit, match="already-checked projection"):
        console.main()


def test_landlock_probe_absent_refuses_loudly_before_any_subprocess_runs(tmp_path, monkeypatch):
    """No ``setpriv`` on ``PATH`` (macOS today) must refuse, not proceed unenforced.

    The backend raises while building its command when the launcher is absent,
    which is earlier than the enforcement probe can run; this proves that
    refusal actually stops both entry points before either one spawns a
    subprocess, rather than trusting the source reading alone.
    """
    run_root, run_id = _make_run(tmp_path)
    missing = custody.NoConfinement("linux-without-setpriv")
    _use_backend(monkeypatch, missing)

    def _forbidden(*args, **kwargs):
        raise AssertionError("no subprocess may run once the platform refuses the boundary")

    monkeypatch.setattr(custody.subprocess, "run", _forbidden)

    with pytest.raises(OperatorError) as review_error:
        cli._review_in_custody(run_root, run_id, ROOT)
    assert review_error.value.code == ErrorCode.CONSOLE_CUSTODY_REFUSED

    with pytest.raises(OperatorError) as advance_error:
        advance.trigger_advance(
            run_root,
            run_id,
            "armarium",
            reason="x",
            workspace=ROOT,
            expected_digest=_boundary_digest(run_root, run_id),
        )
    assert advance_error.value.code == ErrorCode.CONSOLE_CUSTODY_REFUSED


class _StubConfinement(custody.Confinement):
    """Tests must exercise the production seam without requiring host confinement."""

    name = "stub confinement"
    platform = "stub"

    def __init__(self, wrap):
        self._wrap = wrap

    def command(self, command, *, writable=None):
        return self._wrap(list(command))

    def enforcement(self):
        return "a stub backend enforces nothing; it exists to drive the seam"


def _use_backend(monkeypatch, backend):
    monkeypatch.setattr(custody, "confinement", lambda *a, **k: backend)


def test_a_launcher_that_never_established_its_boundary_is_a_custody_refusal(tmp_path, monkeypatch):
    """A confinement launcher that exits without exec'ing must not be misread.

    This is the second platform seam, distinct from the absent-probe case: the
    launcher is found and invoked but establishes nothing — a Linux kernel
    without Landlock (``setpriv`` exits ``SETPRIV_EXIT_PRIVERR``), or a macOS
    host whose profile will not apply. Before this was classified, such a
    failure surfaced as "the run tree could not be read" / "advance refused" —
    plausible-sounding but wrong stories that send a reader chasing a data
    problem that does not exist.
    """
    run_root, run_id = _make_run(tmp_path)
    _use_backend(
        monkeypatch,
        _StubConfinement(lambda command: [sys.executable, "-c", "import sys; sys.exit(127)"]),
    )

    with pytest.raises(OperatorError) as review_error:
        cli._review_in_custody(run_root, run_id, ROOT)
    assert review_error.value.code == ErrorCode.CONSOLE_CUSTODY_REFUSED

    with pytest.raises(OperatorError) as advance_error:
        advance.trigger_advance(
            run_root,
            run_id,
            "armarium",
            reason="x",
            workspace=ROOT,
            expected_digest=_boundary_digest(run_root, run_id),
        )
    assert advance_error.value.code == ErrorCode.CONSOLE_CUSTODY_REFUSED


def test_a_backend_that_does_not_actually_deny_writes_refuses_before_the_console_opens(
    tmp_path, monkeypatch
):
    """The boundary is proven on this host, never inferred from an exit code.

    A launcher can succeed and confine nothing — a profile that compiles to
    an empty policy, a `setpriv` built without Landlock support, a future
    backend whose author guessed the syntax wrong. Every one of those exits
    zero and runs the console perfectly, which is exactly the silent absence
    the unit exists to prevent. `verify_confinement` makes the backend deny
    one real write first, so an unenforced boundary is a refusal rather than
    a console that looks identical to a confined one.
    """
    run_root, run_id = _make_run(tmp_path)
    _use_backend(monkeypatch, _StubConfinement(lambda command: command))

    with pytest.raises(OperatorError) as review_error:
        cli._review_in_custody(run_root, run_id, ROOT)
    assert review_error.value.code == ErrorCode.CONSOLE_CUSTODY_REFUSED
    assert "did not refuse both" in review_error.value.detail

    with pytest.raises(OperatorError) as advance_error:
        advance.trigger_advance(
            run_root,
            run_id,
            "armarium",
            reason="x",
            workspace=ROOT,
            expected_digest=_boundary_digest(run_root, run_id),
        )
    assert advance_error.value.code == ErrorCode.CONSOLE_CUSTODY_REFUSED
    assert "did not refuse both" in advance_error.value.detail


def test_writable_directory_identity_is_rechecked_after_policy_construction(tmp_path, monkeypatch):
    writable = tmp_path / "receipts" / "sha256"
    writable.mkdir(parents=True)

    class _SwappingBackend(_StubConfinement):
        def __init__(self):
            super().__init__(lambda command: command)
            self.calls = 0

        def command(self, command, *, writable=None):
            self.calls += 1
            if self.calls == 1:
                return [
                    sys.executable,
                    "-c",
                    "print('WRITE_REFUSED\\nNETWORK_REFUSED')",
                ]
            moved = writable.with_name("original-sha256")
            writable.rename(moved)
            writable.mkdir()
            return list(command)

    backend = _SwappingBackend()
    _use_backend(monkeypatch, backend)

    with pytest.raises(OperatorError) as excinfo:
        custody.run_confined(
            [sys.executable, *custody.CHILD_INTERPRETER_FLAGS, "-c", "print('child')"],
            writable=writable,
            cwd=ROOT,
            input_text="",
        )

    assert excinfo.value.code == ErrorCode.CONSOLE_CUSTODY_REFUSED
    assert "changed device or inode" in (excinfo.value.detail or "")


def test_a_symlink_is_never_accepted_as_the_writable_allowance(tmp_path, monkeypatch):
    real = tmp_path / "real-receipts"
    real.mkdir()
    alias = tmp_path / "receipt-alias"
    alias.symlink_to(real, target_is_directory=True)

    def _forbidden(*_args, **_kwargs):
        raise AssertionError("a subprocess ran for a symlinked write allowance")

    monkeypatch.setattr(custody.subprocess, "run", _forbidden)

    with pytest.raises(OperatorError) as excinfo:
        custody.run_confined(
            [sys.executable, *custody.CHILD_INTERPRETER_FLAGS, "-c", "pass"],
            writable=alias,
            cwd=ROOT,
            input_text="",
        )

    assert excinfo.value.code == ErrorCode.CONSOLE_CUSTODY_REFUSED
    assert "symlink" in (excinfo.value.detail or "")


def test_the_platform_probe_picks_a_backend_and_never_leaves_one_silently_absent(monkeypatch):
    """Every platform gets an answer, and "none" is an answer that refuses."""
    assert isinstance(custody.confinement("linux"), custody.LandlockConfinement)
    assert isinstance(custody.confinement("darwin"), custody.SeatbeltConfinement)

    monkeypatch.setattr(custody.sys, "platform", "darwin")
    assert isinstance(custody.confinement(), custody.SeatbeltConfinement)
    monkeypatch.setattr(custody.sys, "platform", "linux")
    assert isinstance(custody.confinement(), custody.LandlockConfinement)

    orphan = custody.confinement("win32")
    assert isinstance(orphan, custody.NoConfinement)
    with pytest.raises(OperatorError) as excinfo:
        orphan.command(["/bin/true"])
    assert excinfo.value.code == ErrorCode.CONSOLE_CUSTODY_REFUSED
    detail = excinfo.value.detail
    assert "'win32'" in detail
    assert "no provider credential" in detail and "NOT enforced" in detail


def test_the_macos_backend_states_the_same_contract_landlock_does(tmp_path):
    """Deny by default; grant reads, one exact exec, and at most one write subtree."""
    backend = custody.confinement("darwin")

    closed = backend.profile()
    assert "(deny default)" in closed
    assert "(allow default)" not in closed
    assert "(allow file-read*)" in closed
    assert "(allow process-exec (literal " in closed
    # Measured on the production Mac: a loopback connect succeeded under
    # (deny default) alone, so the network family is denied by name — its
    # PRESENCE is the contract, not its absence.
    assert "(deny network*)" in closed
    assert "mach-lookup" not in closed
    assert "process-fork" not in closed
    assert "subpath" not in closed

    writable = tmp_path / "runs" / "r1" / "receipts" / "sha256"
    writable.mkdir(parents=True)
    open_one = backend.profile(writable)
    lines = open_one.strip().splitlines()
    assert lines.index("(deny default)") < lines.index(
        f'(allow file-write* (subpath "{writable.resolve()}"))'
    )
    assert open_one.count("subpath") == 1
    assert not str(writable.resolve()).endswith("/")


def test_the_macos_backend_refuses_a_path_it_cannot_name_safely(tmp_path):
    """A run root reaches the Seatbelt child as policy text, not as an argument.

    An argument vector is inert against a stray quote; a profile is not — a
    quote would end the string literal and let the remainder of the path be
    read as SBPL. Ordinary awkward characters are escaped and still work; a
    control character, which no legitimate run root has and which is how an
    injected rule would begin, is refused outright.
    """
    backend = custody.confinement("darwin")

    quoted = tmp_path / 'a"b'
    quoted.mkdir()
    assert f'(subpath "{tmp_path.resolve()}/a\\"b")' in backend.profile(quoted)

    hostile = tmp_path / 'x\n(allow file-write* (literal "y"))'
    with pytest.raises(OperatorError) as excinfo:
        backend.profile(hostile)
    assert excinfo.value.code == ErrorCode.CONSOLE_CUSTODY_REFUSED


def test_the_macos_backend_refuses_a_program_it_cannot_name_unambiguously(monkeypatch):
    """No ``--`` separator, so the wrapped program may not look like an option.

    Whether ``sandbox-exec``'s option parsing consumes a ``--`` separator is
    not testable from this chamber, so the backend does not depend on the
    answer: it names the program by absolute path and refuses anything else.
    """
    backend = custody.confinement("darwin")
    monkeypatch.setattr(backend, "launcher", lambda *_args, **_kwargs: "/usr/bin/sandbox-exec")
    assert backend.command([sys.executable, "-c", "pass"])[:2] == ["/usr/bin/sandbox-exec", "-p"]
    with pytest.raises(OperatorError) as excinfo:
        backend.command(["-c", "pass"])
    assert excinfo.value.code == ErrorCode.CONSOLE_CUSTODY_REFUSED


def test_probe_and_real_launch_pin_one_launcher_binary(tmp_path):
    """The same spelling with a different inode is not the probed launcher."""

    launcher = tmp_path / "sandbox-exec"
    launcher.write_text("first", encoding="utf-8")
    backend = custody.Confinement()

    assert backend.launcher("sandbox-exec", system_path=str(launcher)) == str(launcher)
    replacement = tmp_path / "replacement"
    replacement.write_text("second", encoding="utf-8")
    replacement.replace(launcher)

    with pytest.raises(OperatorError) as excinfo:
        backend.launcher("sandbox-exec", system_path=str(launcher))
    assert excinfo.value.code == ErrorCode.CONSOLE_CUSTODY_REFUSED
    assert "changed after the boundary probe" in (excinfo.value.detail or "")


def test_confinement_launcher_never_falls_back_to_path(tmp_path, monkeypatch):
    attacker = tmp_path / "setpriv"
    attacker.write_text("attacker-selected launcher", encoding="utf-8")
    monkeypatch.setenv("PATH", str(tmp_path))

    with pytest.raises(OperatorError) as excinfo:
        custody.Confinement().launcher(
            "setpriv", system_path=str(tmp_path / "missing-system-setpriv")
        )

    assert excinfo.value.code == ErrorCode.CONSOLE_CUSTODY_REFUSED
    assert "no trusted setpriv" in (excinfo.value.detail or "")


@pytest.mark.skipif(sys.platform == "darwin", reason="a native macOS host has sandbox-exec")
def test_a_host_without_sandbox_exec_refuses_and_names_what_is_unenforced():
    """An actual host without ``sandbox-exec`` must refuse and name the gap."""
    with pytest.raises(OperatorError) as excinfo:
        custody.confinement("darwin").command([sys.executable, "-c", "pass"])
    assert excinfo.value.code == ErrorCode.CONSOLE_CUSTODY_REFUSED
    assert "will not run" in excinfo.value.detail


def test_each_backend_recognises_its_own_launcher_speaking_and_no_one_else():
    """A boundary failure and a program failure are different accusations."""
    landlock = custody.confinement("linux")
    assert landlock.launcher_failure(_exit(custody.SETPRIV_PRIVILEGE_FAILURE_EXIT)) is not None
    assert landlock.launcher_failure(_exit(2, stderr="advance names unknown stage")) is None

    seatbelt = custody.confinement("darwin")
    assert (
        seatbelt.launcher_failure(
            _exit(1, stderr="sandbox-exec: sandbox_apply: Operation not permitted")
        )
        is not None
    )
    assert seatbelt.launcher_failure(_exit(1, stderr="advance names unknown stage")) is None
    # 127 is Landlock's signature, not Seatbelt's; neither backend may borrow
    # the other's, or a plain "command not found" would read as a boundary
    # failure on the wrong platform.
    assert seatbelt.launcher_failure(_exit(127, stderr="/bin/sh: no such file")) is None


def _exit(returncode: int, *, stdout: str = "", stderr: str = "") -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess([], returncode, stdout, stderr)


@pytest.mark.skipif(sys.platform != "linux", reason="requires the native Landlock backend")
def test_a_run_root_containing_a_colon_still_names_one_permitted_path(tmp_path):
    """The Landlock rule is colon-delimited and the run root is operator-chosen.

    ``path-beneath:<rights>:<path>`` would be ambiguous if the launcher split
    on every colon: `/runs/a:b/receipts/sha256` would then grant `/runs/a`,
    silently widening the one write allowance to a directory holding evidence.
    util-linux splits on the first two colons only, which this pins by
    behaviour rather than by reading the manual — the parent of the permitted
    directory must still be refused.
    """
    permitted = tmp_path / "a:b" / "receipts" / "sha256"
    permitted.mkdir(parents=True)
    inside, outside = permitted / "allowed", permitted.parent / "denied"

    completed = subprocess.run(
        custody.confinement("linux").command(
            [
                sys.executable,
                "-c",
                "import sys\nfrom pathlib import Path\n"
                "for target in sys.argv[1:]:\n"
                "    try:\n"
                "        Path(target).write_text('x')\n"
                "    except OSError:\n"
                "        print('REFUSED')\n"
                "    else:\n"
                "        print('PERMITTED')\n",
                str(inside),
                str(outside),
            ],
            writable=permitted,
        ),
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
    assert completed.stdout.split() == ["PERMITTED", "REFUSED"]
    assert inside.is_file() and not outside.exists()


def test_the_confined_child_inherits_no_interpreter_or_loader_hook(tmp_path):
    """Whoever controls the worker's imports controls what it writes.

    The advance worker is the one process here permitted to write into a run
    tree. An inherited ``PYTHONPATH``, a user site-packages directory or a
    preloaded shared object could shadow `advance.py` and mint a record this
    repository never wrote — a boundary enforced at the filesystem and lost at
    the import.
    """
    child = custody.custody_environment(
        {
            "PYTHONPATH": "/tmp/attacker",
            "PYTHONSTARTUP": "/tmp/attacker/boot.py",
            "LD_PRELOAD": "/tmp/attacker.so",
            "DYLD_INSERT_LIBRARIES": "/tmp/attacker.dylib",
            "RUNPOD_API_KEY": "x",
            "SAFE": "yes",
        }
    )
    assert child == {}
    assert custody.custody_environment({"LANG": "C.UTF-8", "SAFE": "yes"}) == {"LANG": "C.UTF-8"}
    assert custody.CHILD_INTERPRETER_FLAGS == ("-I", "-S")

    attacker_packages = tmp_path / "site-packages"
    attacker_packages.mkdir()
    original_path = list(custody.sys.path)
    try:
        custody.sys.path.insert(0, str(attacker_packages))
        command = custody.python_module_command("operations.operator.console")
    finally:
        custody.sys.path[:] = original_path
    assert str(attacker_packages.resolve()) not in json.loads(command[7])


def test_no_caller_can_nominate_the_tree_the_confined_child_imports_from(tmp_path):
    """The import root is the loaded checkout, and there is no argument to say otherwise.

    A confined child that imported from a caller-supplied tree would take its
    code from whoever chose that path. The child's *working* directory is a
    different decision, made at `run_confined(cwd=...)`: under an installed
    wheel the operator's workspace is legitimately not this checkout, so the
    two values must stay separate rather than be checked against each other.
    """
    other = tmp_path / "other-checkout"
    (other / "operations" / "operator").mkdir(parents=True)
    (other / "operations" / "operator" / "custody.py").write_text(
        "# a different tree may not choose the worker code\n", encoding="utf-8"
    )

    command = custody.python_module_command("operations.operator.advance_worker")

    assert Path(command[5]) == ROOT
    assert Path(command[5]) != other.resolve()
    assert str(other) not in command
    assert "workspace" not in inspect.signature(custody.python_module_command).parameters


@pytest.mark.skipif(sys.platform != "linux", reason="requires the native Landlock backend")
def test_linux_child_sees_exactly_the_closed_cross_platform_environment():
    """Landlock must not replace the scrubbed mapping with setpriv's account defaults."""

    command = [
        sys.executable,
        *custody.CHILD_INTERPRETER_FLAGS,
        "-c",
        "import json, os; print(json.dumps(dict(os.environ), sort_keys=True))",
    ]
    _backend, completed = custody.run_confined(command, writable=None, cwd=ROOT, input_text="")

    assert completed.returncode == 0, completed.stderr
    assert json.loads(completed.stdout) == custody.custody_environment()


@pytest.mark.skipif(sys.platform != "linux", reason="requires Linux procfs and Landlock")
def test_linux_child_cannot_recover_the_launchers_original_environment_from_proc():
    """A scrubbed env is not custody if the child can reread its parent's secret env."""

    command = [
        sys.executable,
        *custody.CHILD_INTERPRETER_FLAGS,
        "-c",
        (
            "import os\n"
            "from pathlib import Path\n"
            "try:\n"
            "    Path(f'/proc/{os.getppid()}/environ').read_bytes()\n"
            "except OSError:\n"
            "    print('REFUSED')\n"
            "else:\n"
            "    print('PERMITTED')\n"
        ),
    ]
    _backend, completed = custody.run_confined(command, writable=None, cwd=ROOT, input_text="")

    assert completed.returncode == 0, completed.stderr
    assert completed.stdout.strip() == "REFUSED"


@pytest.mark.skipif(
    sys.platform != "linux", reason="requires Landlock plus the Linux seccomp filter"
)
def test_linux_child_cannot_delegate_around_landlock_through_a_socket():
    """A privileged local service must not become the compromised UI's writer."""

    command = [
        sys.executable,
        *custody.CHILD_INTERPRETER_FLAGS,
        "-c",
        (
            "import socket\n"
            "try:\n"
            "    connection = socket.socket()\n"
            "except OSError:\n"
            "    print('REFUSED')\n"
            "else:\n"
            "    connection.close()\n"
            "    print('PERMITTED')\n"
        ),
    ]
    _backend, completed = custody.run_confined(command, writable=None, cwd=ROOT, input_text="")

    assert completed.returncode == 0, completed.stderr
    assert completed.stdout.strip() == "REFUSED"


def test_credential_free_environment_catches_secrets_beyond_the_named_provider_prefixes():
    """The Perlector chair is contractually swappable to any vendor model.

    A scrubber that only knew today's four provider prefixes would silently
    stop protecting the boundary the day a differently-named vendor
    credential is wired in. The generic name-shape marker this reuses from
    `operations.pod.models` is what already keeps the same class of secret
    out of a durable controller receipt.
    """
    # Values are short and unstructured on purpose: only the env var *name* is
    # ever inspected, and a long value shaped like a real key would be exactly
    # the payload this repository's own ingress scanner exists to reject from
    # a committed test file (`.githooks/check_ingress.py`'s literal-credential
    # rule) — irony a fixture value should not have to test.
    scrubbed = custody.credential_free_environment(
        {
            "OPENAI_API_KEY": "x",
            "SOME_SERVICE_SECRET": "x",
            "VERBATUS_LAUNCH_TOKEN": "x",
            "SAFE": "yes",
        }
    )
    assert scrubbed == {"SAFE": "yes"}


def test_require_no_provider_credentials_refuses_on_the_same_marker_shapes():
    with pytest.raises(OperatorError) as excinfo:
        custody.require_no_provider_credentials({"OPENAI_API_KEY": "x", "SAFE": "yes"})
    assert excinfo.value.code == ErrorCode.CONSOLE_CUSTODY_REFUSED


def test_review_marks_invalid_and_advance_refuses_when_a_sealed_inventory_lost_evidence(tmp_path):
    run_root, run_id = _make_run(tmp_path)
    tree = RunTree(run_root, run_id)
    reviewed_digest = _boundary_digest(run_root, run_id, "attestatores")
    receipts_before = {path.name for path in (tree.root / "receipts" / "sha256").glob("*.json")}
    testimony = next(
        row
        for row in tree.build_manifest("attestatores", verify_inputs=False)["artifacts"]
        if row["kind"] not in {"stage-seal", "decode-environment"}
    )
    tree.resolve(testimony["relative_path"]).unlink()

    projected = review.ReadOnlyRun(run_root, run_id).projection()
    boundary = next(row for row in projected.boundaries if row["stage"] == "attestatores")
    assert boundary["seal_present"] is True
    assert boundary["sealed"] is False
    assert "inventory no longer matches disk" in boundary["seal_note"]

    with pytest.raises(OperatorError) as advance_error:
        advance.trigger_advance(
            run_root,
            run_id,
            "attestatores",
            reason="must not advance damaged evidence",
            workspace=ROOT,
            expected_digest=reviewed_digest,
        )
    assert advance_error.value.code == ErrorCode.ADVANCE_REFUSED
    assert "inventory no longer matches disk" in (advance_error.value.detail or "")
    assert {
        path.name for path in (tree.root / "receipts" / "sha256").glob("*.json")
    } == receipts_before


def test_review_image_rows_refuse_bytes_that_do_not_match_their_sealed_digest():
    stored = b"changed page bytes"
    tree = types.SimpleNamespace(read_bytes=lambda _path: stored)
    page = {
        "ordinal": 1,
        "page_id": "pg_example",
        "outcome": "sealed",
        "image_path": "1_exemplar/blobs/sha256/example",
        "image_sha256": "0" * 64,
    }
    act = {
        "act_id": "act_example",
        "act_key": "a1",
        "category": "delivered",
        "source_regions": [
            {
                "source_page_ordinal": 1,
                "region_id": "rgn_example",
                "image_path": "2_designator/blobs/sha256/example",
                "image_sha256": "0" * 64,
            }
        ],
    }

    with pytest.raises(OperatorError) as page_error:
        review._image_row(tree, page)
    assert "sealed page" in (page_error.value.detail or "")
    with pytest.raises(OperatorError) as crop_error:
        review._act_row(tree, act)
    assert "act crop" in (crop_error.value.detail or "")


def test_review_keeps_an_unsealed_page_visible_with_its_reason():
    row = review._image_row(
        types.SimpleNamespace(),
        {"ordinal": 2, "page_id": None, "outcome": "refused", "reason": "decoder refused"},
    )

    assert row == {
        "ordinal": 2,
        "page_id": None,
        "outcome": "refused",
        "reason": "decoder refused",
        "image_sha256": None,
        "image_data_url": None,
    }


def test_review_refuses_an_armarium_projection_that_omits_an_accounting_list():
    tree = types.SimpleNamespace(read_artifact=lambda *_args: {"payload": {"pages": []}})

    with pytest.raises(OperatorError) as excinfo:
        review._armarium_payload(tree)
    assert "no delivered list" in (excinfo.value.detail or "")


def test_unreadable_tree_recovery_never_instructs_an_evidence_repair():
    rendered = OperatorError(ErrorCode.CONSOLE_TREE_UNREADABLE).render()

    assert "Preserve the run tree unchanged" in rendered
    assert "never edit the damaged evidence in place" in rendered
    assert "repair the named evidence" not in rendered


def test_review_refuses_bundle_bytes_that_do_not_match_the_export_reference():
    tree = types.SimpleNamespace(read_bytes=lambda _path: b"changed bundle")
    payload = {
        "bundle": {
            "reference": {"relative_path": "7_armarium/blobs/sha256/example", "sha256": "0" * 64},
            "sha256": "0" * 64,
        }
    }

    with pytest.raises(OperatorError) as excinfo:
        review._review_items(tree, payload)
    assert "Armarium bundle" in (excinfo.value.detail or "")


def _review_bundle_payload(data: bytes) -> tuple[types.SimpleNamespace, dict]:
    digest = digest_bytes(data)
    tree = types.SimpleNamespace(read_bytes=lambda _path: data)
    payload = {
        "bundle": {
            "reference": {
                "relative_path": "7_armarium/blobs/sha256/example",
                "sha256": digest,
            },
            "sha256": digest,
        }
    }
    return tree, payload


def test_review_refuses_a_zip_member_that_would_expand_past_its_input_limit():
    bundle = io.BytesIO()
    with zipfile.ZipFile(bundle, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("review-items.jsonl", b"x" * (review.MAX_REVIEW_ITEMS_BYTES + 1))
    tree, payload = _review_bundle_payload(bundle.getvalue())

    with pytest.raises(OperatorError) as excinfo:
        review._review_items(tree, payload)

    assert "review limit" in (excinfo.value.detail or "")


def test_review_refuses_more_review_rows_than_the_console_can_safely_project():
    bundle = io.BytesIO()
    with zipfile.ZipFile(bundle, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("review-items.jsonl", b"{}\n" * (review.MAX_REVIEW_ITEMS + 1))
    tree, payload = _review_bundle_payload(bundle.getvalue())

    with pytest.raises(OperatorError) as excinfo:
        review._review_items(tree, payload)

    assert "records" in (excinfo.value.detail or "")


def test_review_refuses_ambiguous_duplicate_review_members():
    bundle = io.BytesIO()
    with zipfile.ZipFile(bundle, "w") as archive:
        archive.writestr("review-items.jsonl", b'{"reason":"first"}\n')
        with pytest.warns(UserWarning, match="Duplicate name"):
            archive.writestr("review-items.jsonl", b'{"reason":"second"}\n')
    tree, payload = _review_bundle_payload(bundle.getvalue())

    with pytest.raises(OperatorError) as excinfo:
        review._review_items(tree, payload)

    assert "more than one review-items.jsonl" in (excinfo.value.detail or "")


def test_verify_advance_detects_a_boundary_that_changed_after_it_was_advanced(tmp_path):
    """Digest binding must be proven against a boundary that actually changed."""
    run_root, run_id = _make_run(tmp_path)
    tree = RunTree(run_root, run_id)
    reference = advance.trigger_advance(
        run_root,
        run_id,
        "armarium",
        reason="operator reviewed the sealed Armarium boundary",
        workspace=ROOT,
        expected_digest=_boundary_digest(run_root, run_id),
    )
    advance.verify_advance(tree, "armarium", reference)

    seal, _ = advance.sealed_boundary(tree, "armarium")
    record = tree.read_artifact("armarium", "stage-seal", seal["artifact_id"])
    record["payload"] = {
        **record["payload"],
        "census": [
            *record["payload"]["census"],
            {"kind": "probe", "outcome": "sealed", "count": 1},
        ],
    }
    record["self_hash"] = self_hash(record)
    tree.resolve(tree.artifact_path("armarium", "stage-seal", seal["artifact_id"])).write_bytes(
        canonical_bytes(record)
    )

    with pytest.raises(ApprovalRefusal, match="boundary changed after it was advanced"):
        advance.verify_advance(tree, "armarium", reference)


def test_two_advance_records_for_one_boundary_are_both_persisted_and_both_visible(tmp_path):
    """Append-only means the second advance is a new record, not a silent overwrite.

    Both must actually reach a human: the review projection is the one
    surface a person reads, so a second advance decision that does not
    appear there is lost exactly as GOVERNANCE 2 forbids, even though the
    bytes are safely on disk.
    """
    run_root, run_id = _make_run(tmp_path)
    digest = _boundary_digest(run_root, run_id)
    first = advance.trigger_advance(
        run_root,
        run_id,
        "armarium",
        reason="first reviewer signed off",
        workspace=ROOT,
        expected_digest=digest,
    )
    second = advance.trigger_advance(
        run_root,
        run_id,
        "armarium",
        reason="second reviewer signed off independently",
        workspace=ROOT,
        expected_digest=digest,
    )
    assert first.relative_path != second.relative_path

    projected = review.ReadOnlyRun(run_root, run_id).projection()
    paths = {record["relative_path"] for record in projected.advance_records}
    assert paths == {first.relative_path, second.relative_path}
    assert all(
        record["subject_ids"] == ["stage-boundary:armarium"] for record in projected.advance_records
    )


def test_review_refuses_an_advance_record_copied_under_a_false_content_address(tmp_path):
    run_root, run_id = _make_run(tmp_path)
    tree = RunTree(run_root, run_id)
    reference = advance.trigger_advance(
        run_root,
        run_id,
        "armarium",
        reason="reviewed once",
        workspace=ROOT,
        expected_digest=_boundary_digest(run_root, run_id),
    )
    false_path = tree.root / "receipts" / "sha256" / f"{'0' * 64}.json"
    false_path.write_bytes(tree.read_bytes(reference.relative_path))

    with pytest.raises(OperatorError) as excinfo:
        review.ReadOnlyRun(run_root, run_id).projection()

    assert excinfo.value.code == ErrorCode.CONSOLE_TREE_UNREADABLE
    assert "content-addressed filename is false" in excinfo.value.detail


def test_review_refuses_an_in_tree_symlink_at_a_receipt_address(tmp_path):
    """An immutable receipt is a regular file, not an alias to equivalent bytes."""

    run_root, run_id = _make_run(tmp_path)
    tree = RunTree(run_root, run_id)
    reference = advance.trigger_advance(
        run_root,
        run_id,
        "armarium",
        reason="reviewed before the receipt path was replaced",
        workspace=ROOT,
        expected_digest=_boundary_digest(run_root, run_id),
    )
    receipt = tree.root / reference.relative_path
    alias = tree.root / "same-receipt-bytes.json"
    alias.write_bytes(receipt.read_bytes())
    receipt.unlink()
    receipt.symlink_to(alias)

    with pytest.raises(OperatorError) as excinfo:
        review.ReadOnlyRun(run_root, run_id).projection()

    assert excinfo.value.code == ErrorCode.CONSOLE_TREE_UNREADABLE
    assert "not an immutable regular file" in (excinfo.value.detail or "")


def test_operator_advance_requires_exact_confirmation_of_the_observed_digest(
    tmp_path, monkeypatch, capsys
):
    run_root, run_id = _make_run(tmp_path)
    tree = RunTree(run_root, run_id)
    before = {path.name for path in (tree.root / "receipts" / "sha256").glob("*.json")}
    monkeypatch.setattr(cli, "_typed_advance_confirmation", lambda phrase: "not that boundary")

    with pytest.raises(OperatorError) as excinfo:
        cli._advance_with_confirmation(
            run_root,
            run_id,
            "armarium",
            reason="reviewed in the console",
            workspace=ROOT,
        )

    assert excinfo.value.code == ErrorCode.ADVANCE_REFUSED
    assert {path.name for path in (tree.root / "receipts" / "sha256").glob("*.json")} == before
    assert "seal digest" in capsys.readouterr().out


def test_confirmed_operator_advance_runs_the_external_worker_and_reports_its_record(
    tmp_path, monkeypatch, capsys
):
    run_root, run_id = _make_run(tmp_path)
    monkeypatch.setattr(cli, "_typed_advance_confirmation", lambda phrase: phrase)

    cli._advance_with_confirmation(
        run_root,
        run_id,
        "armarium",
        reason="reviewed in the console",
        workspace=ROOT,
    )

    projected = review.ReadOnlyRun(run_root, run_id).projection()
    assert len(projected.advance_records) == 1
    assert projected.advance_records[0]["reason"] == "reviewed in the console"
    assert "Advance record:" in capsys.readouterr().out


def test_a_worker_refusal_on_stderr_cannot_be_hidden_by_a_zero_exit(tmp_path, monkeypatch):
    run_root, run_id = _make_run(tmp_path)
    tree = RunTree(run_root, run_id)
    expected_digest = _boundary_digest(run_root, run_id)

    def _false_success(*_args, **_kwargs):
        reference = advance.record_advance(
            tree,
            "armarium",
            reason="worker reported both success and refusal",
            expected_digest=expected_digest,
        )
        completed = subprocess.CompletedProcess(
            [],
            0,
            json.dumps(reference.to_record()),
            "SECURITY REFUSAL: worker state was inconsistent",
        )
        return _StubConfinement(lambda command: command), completed

    monkeypatch.setattr(advance, "run_confined", _false_success)

    with pytest.raises(OperatorError) as excinfo:
        advance.trigger_advance(
            run_root,
            run_id,
            "armarium",
            reason="worker reported both success and refusal",
            workspace=ROOT,
            expected_digest=expected_digest,
        )

    assert excinfo.value.code == ErrorCode.ADVANCE_REFUSED
    assert "reported a refusal despite returning success" in (excinfo.value.detail or "")
    assert len(review.ReadOnlyRun(run_root, run_id).projection().advance_records) == 1


def test_a_post_worker_security_refusal_keeps_its_exact_reason(tmp_path, monkeypatch):
    run_root, run_id = _make_run(tmp_path)
    tree = RunTree(run_root, run_id)
    expected_digest = _boundary_digest(run_root, run_id)

    def _success(*_args, **_kwargs):
        reference = advance.record_advance(
            tree,
            "armarium",
            reason="identity changed after the worker returned",
            expected_digest=expected_digest,
        )
        return _StubConfinement(lambda command: command), subprocess.CompletedProcess(
            [], 0, json.dumps(reference.to_record()), ""
        )

    real_require = advance.require_directory_identity
    calls = 0

    def _refuse_after_worker(path, expected, label):
        nonlocal calls
        calls += 1
        if calls == 3:
            raise ApprovalRefusal("SECURITY REFUSAL: reviewed run-tree inode changed")
        return real_require(path, expected, label)

    monkeypatch.setattr(advance, "run_confined", _success)
    monkeypatch.setattr(advance, "require_directory_identity", _refuse_after_worker)

    with pytest.raises(OperatorError) as excinfo:
        advance.trigger_advance(
            run_root,
            run_id,
            "armarium",
            reason="identity changed after the worker returned",
            workspace=ROOT,
            expected_digest=expected_digest,
        )

    assert excinfo.value.code == ErrorCode.ADVANCE_REFUSED
    assert "SECURITY REFUSAL: reviewed run-tree inode changed" in (excinfo.value.detail or "")


def test_advance_verb_is_the_confirmed_operator_path_to_the_external_worker(
    tmp_path, monkeypatch, capsys
):
    run_root, run_id = _make_run(tmp_path)
    monkeypatch.setattr(cli, "_typed_advance_confirmation", lambda phrase: phrase)

    result = cli.main(
        [
            "--workspace",
            str(ROOT),
            "advance",
            "--run-root",
            str(run_root),
            "--run-id",
            run_id,
            "--stage",
            "armarium",
            "--reason",
            "reviewed through the operator verb",
        ]
    )

    assert result == 0
    assert "Advance record:" in capsys.readouterr().out
    projected = review.ReadOnlyRun(run_root, run_id).projection()
    assert [row["reason"] for row in projected.advance_records] == [
        "reviewed through the operator verb"
    ]


def test_confirmed_digest_changed_before_worker_launch_is_refused_without_a_record(tmp_path):
    run_root, run_id = _make_run(tmp_path)
    tree = RunTree(run_root, run_id)
    seal, reviewed_digest = advance.sealed_boundary(tree, "armarium")
    before = {path.name for path in (tree.root / "receipts" / "sha256").glob("*.json")}
    record = tree.read_artifact("armarium", "stage-seal", seal["artifact_id"])
    record["payload"] = {
        **record["payload"],
        "census": [*record["payload"]["census"], {"kind": "changed", "count": 1}],
    }
    record["self_hash"] = self_hash(record)
    tree.resolve(tree.artifact_path("armarium", "stage-seal", seal["artifact_id"])).write_bytes(
        canonical_bytes(record)
    )

    with pytest.raises(OperatorError) as excinfo:
        advance.trigger_advance(
            run_root,
            run_id,
            "armarium",
            reason="reviewed before the change",
            workspace=ROOT,
            expected_digest=reviewed_digest,
        )

    assert excinfo.value.code == ErrorCode.ADVANCE_REFUSED
    assert {path.name for path in (tree.root / "receipts" / "sha256").glob("*.json")} == before


def test_boundary_changed_during_worker_append_is_retained_but_not_reported_as_success(
    tmp_path, monkeypatch
):
    """A newly stale immutable record is a refusal with a recovery location."""

    run_root, run_id = _make_run(tmp_path)
    tree = RunTree(run_root, run_id)
    reviewed_digest = _boundary_digest(run_root, run_id)
    original_run_confined = advance.run_confined

    def reseal_after_worker(*args, **kwargs):
        result = original_run_confined(*args, **kwargs)
        seal, _digest = advance.sealed_boundary(tree, "armarium")
        record = tree.read_artifact("armarium", "stage-seal", seal["artifact_id"])
        record["payload"] = {
            **record["payload"],
            "census": [*record["payload"]["census"], {"kind": "post-append-change", "count": 1}],
        }
        record["self_hash"] = self_hash(record)
        tree.resolve(tree.artifact_path("armarium", "stage-seal", seal["artifact_id"])).write_bytes(
            canonical_bytes(record)
        )
        return result

    monkeypatch.setattr(advance, "run_confined", reseal_after_worker)

    with pytest.raises(OperatorError) as refusal:
        advance.trigger_advance(
            run_root,
            run_id,
            "armarium",
            reason="operator reviewed the pre-append boundary",
            workspace=ROOT,
            expected_digest=reviewed_digest,
        )

    assert refusal.value.code == ErrorCode.ADVANCE_REFUSED
    assert "wrote checked decision record receipts/sha256/" in (refusal.value.detail or "")
    projected = review.ReadOnlyRun(run_root, run_id).projection().advance_records
    stale = [
        row for row in projected if row["reason"] == "operator reviewed the pre-append boundary"
    ]
    assert len(stale) == 1
    assert stale[0]["boundary_current"] is False


# --- The trigger: a hostile run tree can lie to a person, never to evidence. -----------


def test_a_path_traversal_image_reference_in_the_run_tree_is_refused_not_read(tmp_path):
    """A poisoned Armarium export cannot make the console read outside the run tree."""
    run_root, run_id = _make_run(tmp_path)
    tree = RunTree(run_root, run_id)
    export_id = artifact_id(ARMARIUM, "export", "export", None)
    record = tree.read_artifact(ARMARIUM, "export", export_id)
    pages = list(record["payload"]["pages"])
    pages[0] = {**pages[0], "image_path": "../../../etc/passwd"}
    record["payload"] = {**record["payload"], "pages": pages}
    record["self_hash"] = self_hash(record)
    tree.resolve(tree.artifact_path(ARMARIUM, "export", export_id)).write_bytes(
        canonical_bytes(record)
    )

    with pytest.raises(OperatorError) as excinfo:
        review.ReadOnlyRun(run_root, run_id).projection()
    assert excinfo.value.code == ErrorCode.CONSOLE_TREE_UNREADABLE


def test_hostile_projection_content_reaches_the_terminal_only_as_inert_escaped_text(
    tmp_path, monkeypatch, capsys
):
    """A malicious filename or review reason can misinform, never execute or corrupt.

    The content crosses the custody boundary twice (into the console, then
    back out) exclusively as `json.dumps` text, which escapes every control
    byte to a literal ``\\u00XX`` sequence — this is what makes the boundary
    safe against a hostile run tree, not `strip_control_bytes` alone. This
    pins that behaviour end to end through the real subprocess pipe, so a
    future change to pretty-print or otherwise hand-format the console's
    output cannot silently drop it.
    """
    hostile = ReviewProjection(
        run_id="hostile",
        boundaries=(),
        pages=(),
        acts=({"act_id": "a1", "act_key": "x\x1b[2Jwiped", "category": "baptism", "crops": []},),
        review_items=({"reason": "adversarial\x1b]0;pwned\x07 escape sequence"},),
        advance_records=(),
    )
    monkeypatch.setattr(
        cli, "ReadOnlyRun", lambda root, run_id: types.SimpleNamespace(projection=lambda: hostile)
    )

    cli._review_in_custody(tmp_path, "hostile", ROOT)

    out = capsys.readouterr().out
    assert "\x1b" not in out
    assert "wiped" in out
    assert "pwned" in out


# Producer code under `pipeline/` and `common/` may never mint its own approval.
# The operator package has one narrower exception for recording a human advance:
# only this module may reach the builder or writer, and only for that action.
APPROVAL_MINTING_OPERATOR_MODULES = frozenset({"operations/operator/advance.py"})

OPERATOR_PACKAGE = Path(__file__).resolve().parent


def test_only_the_advance_module_may_reach_the_approval_builder_or_writer():
    """The pipeline cannot approve itself; the console cannot over-approve itself.

    0D established that a producer must not mint the approval that lets it
    proceed. The console is the opposite case — it exists to record one human
    decision — so the risk inverts: not that it approves at all, but that a
    surface holding a legitimate write channel grows a second, illegitimate
    one beside it. `ACTIONS` also admits `exclusion` and `salvage-promotion`,
    and GOVERNANCE 1 reserves an exclusion to Tyrel; a console that could mint
    one would be an automated agent standing in for the human in a rule.
    """
    offenders = [
        path.relative_to(ROOT).as_posix()
        for path in sorted(OPERATOR_PACKAGE.rglob("*.py"))
        if not path.name.startswith("test_")
        and path.relative_to(ROOT).as_posix() not in APPROVAL_MINTING_OPERATOR_MODULES
        and (
            "build_approval_record" in path.read_text(encoding="utf-8")
            or "write_approval_record" in path.read_text(encoding="utf-8")
        )
    ]
    assert not offenders, (
        f"{offenders} can mint or store an approval record from the operator surface; only "
        "the advance module may, and only for the advance action"
    )


def test_the_advance_module_names_no_approval_action_but_advance():
    """Statically: no other action word is even present to be selected."""
    source = inspect.getsource(advance)
    assert advance.ADVANCE_ACTION == "advance"
    for other in ("exclusion", "salvage-promotion"):
        assert other not in source


def test_an_advance_request_cannot_ask_the_worker_for_any_other_approval(tmp_path, monkeypatch):
    """Enforced, not conventioned: the request channel carries no action at all.

    The renderer's one influence over the write is the stdin request. This
    feeds that channel a request that tries to name a different action, a
    different subject and a different approver — every field a wider approval
    would need — and shows the worker ignores all of them. The action and the
    approver are stamped by the code that owns the write, and the subject is
    derived from the stage through `advance_subject`'s closed stage list.
    """
    run_root, run_id = _make_run(tmp_path)
    tree = RunTree(run_root, run_id)
    _seal, expected_digest = advance.sealed_boundary(tree, "armarium")
    monkeypatch.setattr(
        advance_worker.sys,
        "stdin",
        io.StringIO(
            json.dumps(
                {
                    "stage": "armarium",
                    "reason": "a compromised renderer asking for more than it may have",
                    "action": "exclusion",
                    "subject_ids": ["policy:spend"],
                    "approver": "an automated agent",
                    "expected_digest": expected_digest,
                }
            )
        ),
    )
    printed: list[str] = []
    monkeypatch.setattr("builtins.print", lambda *a, **k: printed.append(" ".join(map(str, a))))

    assert (
        advance_worker.main(
            [
                "--run-root",
                str(run_root),
                "--run-id",
                run_id,
                *_worker_identity_arguments(tree),
            ]
        )
        == 0
    )

    reference = json.loads(printed[-1])
    record = tree.read_approval_record(
        ApprovalRecordReference(reference["relative_path"], reference["sha256"])
    )
    assert record["action"] == "advance"
    assert record["approver"] == "Tyrel"
    assert record["subject_ids"] == ["stage-boundary:armarium"]


def test_an_advance_naming_a_stage_that_is_not_one_writes_nothing(tmp_path):
    """The subject is derived from a closed stage list, never from the request."""
    run_root, run_id = _make_run(tmp_path)
    tree = RunTree(run_root, run_id)
    before = {path.name for path in (tree.root / "receipts" / "sha256").glob("*.json")}

    with pytest.raises(ApprovalRefusal, match="unknown stage"):
        advance.record_advance(tree, "../../etc", reason="reviewed")

    assert {path.name for path in (tree.root / "receipts" / "sha256").glob("*.json")} == before


def test_a_symlink_inside_the_run_tree_pointing_outside_it_is_refused_not_followed(tmp_path):
    """Containment is a property of the console's reader, not only of manifest walks.

    Unit 0B's containment covers the manifest walk. The console reads by three
    other routes — a page image, an act crop, and now the receipt directory —
    and a symlink is the case a `..` check alone does not answer, because the
    stored reference is an ordinary-looking relative path and the escape lives
    on disk.
    """
    run_root, run_id = _make_run(tmp_path)
    tree = RunTree(run_root, run_id)
    export_id = artifact_id(ARMARIUM, "export", "export", None)
    record = tree.read_artifact(ARMARIUM, "export", export_id)

    outside = tmp_path / "outside-the-tree.png"
    outside.write_bytes(b"\x89PNG not evidence")
    link = tree.root / "smuggled.png"
    link.symlink_to(outside)

    pages = list(record["payload"]["pages"])
    pages[0] = {**pages[0], "image_path": "smuggled.png"}
    record["payload"] = {**record["payload"], "pages": pages}
    record["self_hash"] = self_hash(record)
    tree.resolve(tree.artifact_path(ARMARIUM, "export", export_id)).write_bytes(
        canonical_bytes(record)
    )

    with pytest.raises(OperatorError) as excinfo:
        review.ReadOnlyRun(run_root, run_id).projection()
    assert excinfo.value.code == ErrorCode.CONSOLE_TREE_UNREADABLE


def test_a_relative_run_root_cannot_split_the_permitted_path_from_the_written_tree(
    tmp_path, monkeypatch
):
    """One string, two resolutions, two different trees — and a boundary guarding neither.

    The parent computes the OS write allowance from *its* resolution of the
    run root, while the worker resolves the same string against `workspace`.
    A relative path therefore let the permitted directory and the tree the
    worker opens drift apart: at best a confusing refusal, at worst an
    allowance granted over a directory nobody wrote to. Resolving once, in the
    parent, is what keeps them the same tree.
    """
    run_root, run_id = _make_run(tmp_path)
    # The launcher's own working directory and the workspace it hands the child
    # are independent: `verbatus --workspace ...` is run from wherever the
    # operator happens to be. Here they differ, which is the only condition the
    # defect needs.
    monkeypatch.chdir(tmp_path)
    relative = os.path.relpath(run_root, tmp_path)
    assert not Path(relative).is_absolute()

    reference = advance.trigger_advance(
        relative,
        run_id,
        "armarium",
        reason="reviewed from a relative root",
        workspace=ROOT,
        expected_digest=_boundary_digest(run_root, run_id),
    )

    tree = RunTree(run_root, run_id)
    assert (tree.root / reference.relative_path).is_file()
    advance.verify_advance(tree, "armarium", reference)


def test_an_advance_whose_boundary_later_changed_is_named_stale_where_a_person_reads(tmp_path):
    """`verify_advance` refuses a moved boundary; the surface has to say so too.

    Nothing on the read path called `verify_advance`, so the console displayed
    "this boundary was advanced" as a present-tense fact however far the seal
    had moved since. A check no reader performs is not a check
    (GOVERNANCE 2). The stale record is still shown — reporting it, not hiding
    it, is what keeps this a reader rather than a picker.
    """
    run_root, run_id = _make_run(tmp_path)
    tree = RunTree(run_root, run_id)
    advance.trigger_advance(
        run_root,
        run_id,
        "armarium",
        reason="reviewed the sealed boundary",
        workspace=ROOT,
        expected_digest=_boundary_digest(run_root, run_id),
    )

    fresh = review.ReadOnlyRun(run_root, run_id).projection().advance_records
    assert [row["boundary_stage"] for row in fresh] == ["armarium"]
    assert fresh[0]["boundary_current"] is True and fresh[0]["boundary_note"] is None

    seal, _ = advance.sealed_boundary(tree, "armarium")
    record = tree.read_artifact("armarium", "stage-seal", seal["artifact_id"])
    record["payload"] = {
        **record["payload"],
        "census": [
            *record["payload"]["census"],
            {"kind": "probe", "outcome": "sealed", "count": 1},
        ],
    }
    record["self_hash"] = self_hash(record)
    tree.resolve(tree.artifact_path("armarium", "stage-seal", seal["artifact_id"])).write_bytes(
        canonical_bytes(record)
    )

    stale = review.ReadOnlyRun(run_root, run_id).projection().advance_records
    assert len(stale) == 1  # still shown, never dropped
    assert stale[0]["boundary_current"] is False
    assert "changed after this advance was recorded" in stale[0]["boundary_note"]
