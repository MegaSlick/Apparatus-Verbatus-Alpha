"""Acceptance tests for the read/trigger/one-record operator boundary."""

from __future__ import annotations

import inspect
import io
import json
import os
import subprocess
import sys
import types
from pathlib import Path

import pytest

from common.contracts.approval import ApprovalRecordReference
from common.contracts.canonical import canonical_bytes, digest_bytes, self_hash
from common.contracts.errors import ApprovalRefusal, SchemaRefusal
from common.contracts.identities import artifact_id
from common.contracts.stages import ARMARIUM
from common.runtree.store import RunTree
from operations.operator import advance, advance_worker, cli, console, custody, review
from operations.operator.errors import ErrorCode, OperatorError
from operations.operator.review import ReviewProjection

ROOT = Path(__file__).resolve().parents[2]
ORCHESTRATOR = ROOT / "pipeline" / "orchestrator" / "run.py"


def _make_run(tmp_path: Path, *, scenario: str = "happy") -> tuple[Path, str]:
    run_root = tmp_path / "runs"
    completed = subprocess.run(
        [
            sys.executable,
            str(ORCHESTRATOR),
            "--fixture",
            "synthetic-two-page-v0",
            "--scenario",
            scenario,
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
    assert completed.returncode == (3 if scenario == "review" else 0), completed.stderr
    return run_root, "reviewed"


def _armarium_digest(run_root: Path, run_id: str) -> str:
    """The exact digest a caller must observe and bind before it may advance.

    `trigger_advance`/`record_advance` refuse an omitted `expected_digest`
    outright (they no longer trust "whatever is on disk right now"), so every
    test that drives them past an unsealed-boundary refusal has to hand one
    in, the same way a real caller would after showing it to an operator.
    """
    return advance.sealed_boundary(RunTree(run_root, run_id), "armarium")[1]


def test_read_surface_walks_stage_records_seals_census_pages_and_crops(tmp_path: Path):
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
    assert len(projected.stage_records) > len(projected.boundaries)
    tree = RunTree(run_root, run_id)
    assert all(
        row["stage"] == row["record"]["stage"]
        and row["artifact_id"] == row["record"]["artifact_id"]
        and row["kind"] == row["record"]["kind"]
        and row["subject_id"] == row["record"]["subject_id"]
        and row["outcome"] == row["record"]["outcome"]
        and row["record_ref"]["sha256"]
        == digest_bytes(tree.read_bytes(row["record_ref"]["relative_path"]))
        for row in projected.stage_records
    )
    assert len(projected.pages) == 2
    assert all(
        row["image_path"].startswith("1_exemplar/blobs/sha256/")
        and "image_data_url" not in row
        and row["record_ref"]["relative_path"].startswith("7_armarium/artifacts/export/")
        for row in projected.pages
    )
    assert all(
        act["crops"]
        and all(
            crop["image_path"].startswith("2_designator/blobs/sha256/") for crop in act["crops"]
        )
        and "image_data_url" not in act["crops"][0]
        and act["record_ref"]["relative_path"].startswith("7_armarium/artifacts/export/")
        for act in projected.acts
    )
    # A complete fixture produces no review rows, but the shape remains visible
    # when Armarium includes it rather than being inferred from an empty outcome.
    assert projected.review_items in (None, ())


def test_held_armarium_review_rows_keep_their_bundle_and_export_record_trace(tmp_path: Path):
    run_root, run_id = _make_run(tmp_path, scenario="review")

    projected = review.ReadOnlyRun(run_root, run_id).projection()

    assert projected.review_items
    for item in projected.review_items:
        assert item["bundle_path"].startswith("7_armarium/blobs/sha256/")
        assert item["member"] == "review-items.jsonl"
        assert item["line"] > 0
        assert item["record_ref"]["relative_path"].startswith("7_armarium/artifacts/export/")
        assert isinstance(item["row"], dict)


def test_review_child_receives_no_write_right_even_when_the_parent_reads_every_record(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Review must never mint the write path that advance receives."""
    run_root, run_id = _make_run(tmp_path)
    seen: dict[str, object] = {}

    class Backend:
        def launcher_failure(self, completed):
            return None

    class Completed:
        returncode = 0
        stdout = "{}"
        stderr = ""

    def confined(command, *, writable, cwd, input_text):
        seen.update(command=command, writable=writable, cwd=cwd, input_text=input_text)
        return Backend(), Completed()

    monkeypatch.setattr(cli, "run_confined", confined)
    cli._review_in_custody(run_root, run_id, ROOT)

    assert seen["writable"] is None
    assert '"stage_records"' in str(seen["input_text"])


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
        )

    assert excinfo.value.code == ErrorCode.ADVANCE_REFUSED
    assert not (tree.root / "receipts").exists()


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
        expected_digest=observed_digest,
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


def test_write_approval_record_has_exactly_one_production_call_site() -> None:
    """One implementation, really: nothing outside `advance.record_advance` writes one.

    `RunTree.write_approval_record` is the generic append-only writer several
    approval subjects share (Lectio nuda sampling, the Perlector's prior-draft
    instrument, and this unit's stage-boundary advance). A second production
    caller would be a second, unaudited route to minting one of those durable
    records -- the write-side twin of GOVERNANCE 3's "a picker rebuilt under
    another name is still a picker." Test files legitimately construct
    approval records directly to set up fixtures and are excluded; only
    source under the repository proper is asked to justify itself here.
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
        return  # The platform-neutral assertions above still run on the macOS native gate.
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


# --- The platform seam: Landlock is Linux-only; production is Tyrel's macOS host. -----


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
            expected_digest=_armarium_digest(run_root, run_id),
        )
    assert advance_error.value.code == ErrorCode.CONSOLE_CUSTODY_REFUSED


class _StubConfinement(custody.Confinement):
    """A backend that runs `command` however the test needs, and names itself."""

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
            expected_digest=_armarium_digest(run_root, run_id),
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
    _use_backend(monkeypatch, _StubConfinement(lambda command: command))  # no confinement at all

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
            expected_digest=_armarium_digest(run_root, run_id),
        )
    assert advance_error.value.code == ErrorCode.CONSOLE_CUSTODY_REFUSED
    assert "did not refuse both" in advance_error.value.detail


# --- One seam, one backend per platform, and absence that says so. --------------------


def test_the_platform_probe_picks_a_backend_and_never_leaves_one_silently_absent(monkeypatch):
    """Every platform gets an answer, and "none" is an answer that refuses."""
    assert isinstance(custody.confinement("linux"), custody.LandlockConfinement)
    assert isinstance(custody.confinement("darwin"), custody.SeatbeltConfinement)

    # Stubbed in both directions: the probe follows the host it is told about.
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
    # Says exactly what is and is not enforced, rather than only that it failed.
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
    assert "subpath" not in closed  # no allowance at all for the renderer

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

    # Not created on disk: no filesystem needs to accept this name for the
    # profile builder to have to refuse it.
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
    monkeypatch.setattr(custody.shutil, "which", lambda name: "/usr/bin/sandbox-exec")
    backend = custody.confinement("darwin")
    assert backend.command([sys.executable, "-c", "pass"])[:2] == ["/usr/bin/sandbox-exec", "-p"]
    with pytest.raises(OperatorError) as excinfo:
        backend.command(["-c", "pass"])
    assert excinfo.value.code == ErrorCode.CONSOLE_CUSTODY_REFUSED


def test_probe_and_real_launch_pin_one_launcher_binary(monkeypatch):
    """A passing probe may not be followed through a newly resolved executable."""

    answers = iter(("/tmp/first-sandbox-exec", "/tmp/replaced-sandbox-exec"))
    pinned_first = str(Path("/tmp/first-sandbox-exec").resolve())
    original_is_file = Path.is_file
    monkeypatch.setattr(
        Path,
        "is_file",
        lambda path: False if path == Path("/usr/bin/sandbox-exec") else original_is_file(path),
    )
    monkeypatch.setattr(custody.shutil, "which", lambda name: next(answers))
    backend = custody.confinement("darwin")

    first = backend.command([sys.executable, "-c", "pass"])
    second = backend.command([sys.executable, "-c", "pass"])

    # macOS resolves /tmp through the /private symlink; the pin compares the
    # canonical path the launcher actually executes.
    assert first[0] == second[0] == pinned_first


@pytest.mark.skipif(sys.platform == "darwin", reason="a native macOS host has sandbox-exec")
def test_a_host_without_sandbox_exec_refuses_and_names_what_is_unenforced():
    """Not stubbed: this Linux chamber genuinely has no ``sandbox-exec``."""
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
        command = custody.python_module_command("operations.operator.console", ROOT)
    finally:
        custody.sys.path[:] = original_path
    assert str(attacker_packages.resolve()) not in json.loads(command[7])


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


# --- The advance record: both layers of the two-layer claim, and its visibility. ------


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
        expected_digest=_armarium_digest(run_root, run_id),
    )
    advance.verify_advance(tree, "armarium", reference)  # still names the advanced boundary

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
    digest = _armarium_digest(run_root, run_id)
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
        expected_digest=_armarium_digest(run_root, run_id),
    )
    false_path = tree.root / "receipts" / "sha256" / f"{'0' * 64}.json"
    false_path.write_bytes(tree.read_bytes(reference.relative_path))

    with pytest.raises(OperatorError) as excinfo:
        review.ReadOnlyRun(run_root, run_id).projection()

    assert excinfo.value.code == ErrorCode.CONSOLE_TREE_UNREADABLE
    assert "content-addressed filename is false" in excinfo.value.detail


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
        stage_records=(),
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


# --- The advance action's authority shape: 0D's doctrine, in operations/. -------------

# The one module under `operations/operator/` that may reach the approval
# builder or the run tree's approval writer. 0D's own boundary test
# (`common/contracts/test_contracts_approval.py`) scans `pipeline/` and
# `common/` and deliberately stops there — the console is neither, and the
# whole point of Unit 4 is that a *person* acting through operations/ may
# append one record. Widening that test to `operations/` would only have
# forced an exemption for this file into a governed contract module. The
# boundary that actually matters here is narrower and belongs here: the
# console may build exactly the advance record and nothing else.
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

    assert advance_worker.main(["--run-root", str(run_root), "--run-id", run_id]) == 0

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


# --- The custody walk: what a hostile or awkward run tree can and cannot do. ----------


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
        expected_digest=_armarium_digest(run_root, run_id),
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
        expected_digest=_armarium_digest(run_root, run_id),
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


def _second_armarium_seal(tree: RunTree) -> str:
    """The synthetic reseal must pass validation before display logic is exercised."""
    from common.contracts.identities import attempt_id

    rows = [
        row
        for row in tree.build_manifest(ARMARIUM, verify_inputs=False)["artifacts"]
        if row["kind"] == "stage-seal"
    ]
    assert len(rows) == 1, "the fixture is expected to seal the Armarium exactly once"
    first = tree.read_artifact(ARMARIUM, "stage-seal", rows[0]["artifact_id"])
    attempt = attempt_id(first["subject_id"], "seal", 2)
    second = json.loads(json.dumps(first))
    second["attempt_id"] = attempt
    second["artifact_id"] = artifact_id(ARMARIUM, "stage-seal", first["subject_id"], attempt)
    second["payload"] = {**second["payload"], "attempt_id": attempt, "attempt_ordinal": 2}
    second["self_hash"] = self_hash(second)
    path = tree.resolve(tree.artifact_path(ARMARIUM, "stage-seal", second["artifact_id"]))
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(canonical_bytes(second))
    return second["artifact_id"]


def test_a_resealed_boundary_shows_every_seal_and_names_exactly_one_as_current(tmp_path: Path):
    """`current` is a label; superseded seals must remain visible evidence."""
    run_root, run_id = _make_run(tmp_path)
    tree = RunTree(run_root, run_id)
    superseded = review.ReadOnlyRun(run_root, run_id).projection()
    before = next(row for row in superseded.boundaries if row["stage"] == ARMARIUM)
    assert [seal["artifact_id"] for seal in before["seals"]] == [before["seal_artifact_id"]]
    assert before["seals"][0]["current"] is True
    assert before["seal_record_ref"] == before["seals"][0]["record_ref"]

    resealed = _second_armarium_seal(tree)

    row = next(
        candidate
        for candidate in review.ReadOnlyRun(run_root, run_id).projection().boundaries
        if candidate["stage"] == ARMARIUM
    )
    assert row["seal_artifact_id"] == resealed, "the later attempt is the one the boundary names"
    assert {seal["artifact_id"] for seal in row["seals"]} == {
        resealed,
        before["seal_artifact_id"],
    }, "the superseded seal is still on the surface a person reads"
    assert [seal["current"] for seal in row["seals"]].count(True) == 1
    assert next(seal for seal in row["seals"] if seal["current"])["artifact_id"] == resealed
    assert all("census" in seal for seal in row["seals"])
    # Each seal carries its own address, so the two are separable in the record
    # rather than two rows sharing one trace back to the current one.
    assert row["seal_record_ref"]["sha256"] != before["seal_record_ref"]["sha256"]
    assert len({seal["record_ref"]["sha256"] for seal in row["seals"]}) == 2
    for seal in row["seals"]:
        assert seal["record_ref"]["sha256"] == digest_bytes(
            tree.read_bytes(seal["record_ref"]["relative_path"])
        )


def test_a_boundary_that_moved_under_two_advances_names_each_one_separately(tmp_path: Path):
    """Each advance keeps its own verdict; a stale one remains visible."""
    run_root, run_id = _make_run(tmp_path)
    tree = RunTree(run_root, run_id)
    early = advance.trigger_advance(
        run_root,
        run_id,
        ARMARIUM,
        reason="advanced before the boundary was resealed",
        workspace=ROOT,
        expected_digest=_armarium_digest(run_root, run_id),
    )
    _second_armarium_seal(tree)
    late = advance.trigger_advance(
        run_root,
        run_id,
        ARMARIUM,
        reason="advanced again against the reseal",
        workspace=ROOT,
        expected_digest=_armarium_digest(run_root, run_id),
    )
    assert early.relative_path != late.relative_path

    records = review.ReadOnlyRun(run_root, run_id).projection().advance_records
    verdicts = {record["relative_path"]: record for record in records}
    assert set(verdicts) == {early.relative_path, late.relative_path}
    assert verdicts[late.relative_path]["boundary_current"] is True
    assert verdicts[late.relative_path]["boundary_note"] is None
    assert verdicts[early.relative_path]["boundary_current"] is False
    assert (
        "changed after this advance was recorded" in verdicts[early.relative_path]["boundary_note"]
    )
    assert all(record["boundary_stage"] == ARMARIUM for record in records)


@pytest.mark.parametrize(
    "evidence",
    ["page", "crop", "bundle", "record"],
    ids=["page-image", "act-crop", "review-bundle", "stage-record"],
)
def test_tampered_evidence_is_refused_naming_the_file_whose_bytes_moved(
    tmp_path: Path, evidence: str
):
    """The rendered refusal must name the file; its `__cause__` is not shown."""
    run_root, run_id = _make_run(tmp_path, scenario="review")
    tree = RunTree(run_root, run_id)
    projected = review.ReadOnlyRun(run_root, run_id).projection()
    if evidence == "page":
        relative = projected.pages[0]["image_path"]
    elif evidence == "crop":
        relative = next(act for act in projected.acts if act["crops"])["crops"][0]["image_path"]
    elif evidence == "bundle":
        assert projected.review_items
        relative = projected.review_items[0]["bundle_path"]
    else:
        relative = next(
            row["record_ref"]["relative_path"]
            for row in projected.stage_records
            if row["kind"] == "stage-seal"
        )
    target = tree.resolve(relative)
    if evidence == "record":
        # Canonical self-hashes ignore trailing whitespace, so change a field to
        # create a semantic tamper rather than alternate JSON serialization.
        record = json.loads(target.read_bytes().decode("utf-8"))
        record["payload"] = {**record["payload"], "census": []}
        target.write_bytes(canonical_bytes(record))
    else:
        target.write_bytes(target.read_bytes() + b"\n")

    with pytest.raises(OperatorError) as raised:
        review.ReadOnlyRun(run_root, run_id).projection()

    assert raised.value.code is ErrorCode.CONSOLE_TREE_UNREADABLE
    assert relative in raised.value.detail, "the refusal must name the file whose bytes moved"
    assert relative in raised.value.render(), "and it must survive the render a person reads"


def test_opening_a_run_for_review_changes_no_path_bytes_size_or_mtime(tmp_path: Path):
    """The unconfined parent projection must preserve the entire run tree."""
    run_root, run_id = _make_run(tmp_path, scenario="review")
    root = run_root / run_id

    def census() -> dict[str, object]:
        seen: dict[str, object] = {}
        for path in sorted(root.rglob("*")):
            key = str(path.relative_to(root))
            if path.is_dir():
                seen[key + "/"] = "directory"
            else:
                status = path.stat()
                seen[key] = (digest_bytes(path.read_bytes()), status.st_size, status.st_mtime_ns)
        return seen

    before = census()
    assert before, "the fixture run must have written a tree to walk"

    projected = review.ReadOnlyRun(run_root, run_id).projection()
    assert projected.stage_records and projected.boundaries

    after = census()
    assert set(after) == set(before), "reviewing a run created or removed a path"
    assert after == before, "reviewing a run rewrote evidence it was only meant to read"


def test_a_stage_record_changed_after_inventory_is_not_paired_with_the_old_digest(tmp_path: Path):
    """The displayed body and address must describe the same filesystem read."""
    run_root, run_id = _make_run(tmp_path)
    tree = RunTree(run_root, run_id)
    manifest_row = next(
        row
        for row in tree.build_manifest(ARMARIUM, verify_inputs=False)["artifacts"]
        if row["kind"] == "export"
    )
    target = tree.resolve(manifest_row["relative_path"])
    changed = json.loads(target.read_bytes().decode("utf-8"))
    changed["payload"] = {**changed["payload"], "scenario": "changed-during-review"}
    changed["self_hash"] = self_hash(changed)
    target.write_bytes(canonical_bytes(changed))

    with pytest.raises(SchemaRefusal, match="changed while the review inventory was being read"):
        review._record_row(tree, ARMARIUM, manifest_row)


def test_export_rows_are_derived_from_the_stage_record_snapshot_not_a_later_reread(
    tmp_path: Path,
):
    """One projection must not describe two versions of the Armarium export."""
    run_root, run_id = _make_run(tmp_path)
    tree = RunTree(run_root, run_id)
    manifest_row = next(
        row
        for row in tree.build_manifest(ARMARIUM, verify_inputs=False)["artifacts"]
        if row["kind"] == "export"
    )
    export_row = review._record_row(tree, ARMARIUM, manifest_row)
    target = tree.resolve(manifest_row["relative_path"])
    changed = json.loads(target.read_bytes().decode("utf-8"))
    changed["payload"] = {**changed["payload"], "scenario": "changed-after-stage-walk"}
    changed["self_hash"] = self_hash(changed)
    target.write_bytes(canonical_bytes(changed))

    payload, record_ref = review._armarium_payload(tree, [export_row])

    assert payload["scenario"] != "changed-after-stage-walk"
    assert payload is export_row["record"]["payload"]
    assert record_ref == export_row["record_ref"]


@pytest.mark.parametrize("evidence", ["page", "crop", "bundle"])
def test_export_references_refuse_a_digest_that_disagrees_with_the_named_bytes(
    tmp_path: Path, evidence: str
):
    """Review may not replace a contradictory recorded digest with a fresh one."""
    run_root, run_id = _make_run(tmp_path, scenario="review")
    tree = RunTree(run_root, run_id)
    export_id = artifact_id(ARMARIUM, "export", "export", None)
    record = tree.read_artifact(ARMARIUM, "export", export_id)
    if evidence == "page":
        pages = json.loads(json.dumps(record["payload"]["pages"]))
        pages[0]["image_sha256"] = "0" * 64
        record["payload"] = {**record["payload"], "pages": pages}
    elif evidence == "crop":
        delivered = json.loads(json.dumps(record["payload"]["delivered"]))
        act = next(row for row in delivered if row["source_regions"])
        act["source_regions"][0]["image_sha256"] = "0" * 64
        record["payload"] = {**record["payload"], "delivered": delivered}
    else:
        bundle = json.loads(json.dumps(record["payload"]["bundle"]))
        bundle["reference"]["sha256"] = "0" * 64
        record["payload"] = {**record["payload"], "bundle": bundle}
    record["self_hash"] = self_hash(record)
    relative = tree.artifact_path(ARMARIUM, "export", export_id)
    tree.resolve(relative).write_bytes(canonical_bytes(record))

    with pytest.raises(OperatorError) as raised:
        review.ReadOnlyRun(run_root, run_id).projection()

    assert raised.value.code is ErrorCode.CONSOLE_TREE_UNREADABLE
    assert relative in raised.value.detail
    assert "digest" in raised.value.detail


@pytest.mark.parametrize("missing", ["pages", "delivered", "non_delivered", "bundle-reference"])
def test_review_refuses_a_missing_required_armarium_projection_field(tmp_path: Path, missing: str):
    """Absent export evidence is not an empty successful review projection."""
    run_root, run_id = _make_run(tmp_path)
    tree = RunTree(run_root, run_id)
    export_id = artifact_id(ARMARIUM, "export", "export", None)
    record = tree.read_artifact(ARMARIUM, "export", export_id)
    payload = dict(record["payload"])
    if missing == "bundle-reference":
        bundle = dict(payload["bundle"])
        bundle.pop("reference")
        payload["bundle"] = bundle
    else:
        payload.pop(missing)
    record["payload"] = payload
    record["self_hash"] = self_hash(record)
    relative = tree.artifact_path(ARMARIUM, "export", export_id)
    tree.resolve(relative).write_bytes(canonical_bytes(record))

    with pytest.raises(OperatorError) as raised:
        review.ReadOnlyRun(run_root, run_id).projection()

    assert raised.value.code is ErrorCode.CONSOLE_TREE_UNREADABLE
    assert relative in raised.value.detail
    assert missing.split("-")[0] in raised.value.detail
