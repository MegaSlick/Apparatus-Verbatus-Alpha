"""Fixtures shared by tests that cross pipeline stage directories."""

import ast
import functools
import hashlib
import importlib.util
import inspect
import json
import os
import pwd
import shutil
import stat
import subprocess
import sys
import tempfile
import textwrap
import tomllib
from collections.abc import Iterator
from pathlib import Path
from types import ModuleType
from unittest import mock

import pytest

from common.contracts.canonical import canonical_bytes, digest_bytes, self_hash
from common.stage import _stage_seal_payload, latest_attempt

ROOT = Path(__file__).resolve().parent

# The notification topic every test process and its subprocesses see; notify.sh
# never posts to it.
NOTIFY_TEST_SINK_TOPIC = "verbatus-test-sink"


def pytest_configure(config: pytest.Config) -> None:
    """Put the session on the sink topic and a temporary operator state folder before
    collection, so no test can page the lead or write real operator records."""
    os.environ["NTFY_TOPIC"] = NOTIFY_TEST_SINK_TOPIC
    # Session and module fixtures run before any per-test state folder exists.
    os.environ.setdefault(ACCOUNT_STATE_HOME_VARIABLE, os.environ.get("XDG_STATE_HOME", ""))
    state_home = tempfile.mkdtemp(prefix="verbatus-xdg-state-")
    config.add_cleanup(lambda: shutil.rmtree(state_home, ignore_errors=True))
    os.environ["XDG_STATE_HOME"] = state_home


# The account's own XDG_STATE_HOME, kept for worker processes, which start after the
# session has replaced it.
ACCOUNT_STATE_HOME_VARIABLE = "VERBATUS_TEST_ACCOUNT_XDG_STATE_HOME"


def _account_operator_state_dirs() -> tuple[Path, ...]:
    """Where the operator CLI keeps real state for this account when no folder is named."""
    candidates = []
    state_home = os.environ.get(ACCOUNT_STATE_HOME_VARIABLE, os.environ.get("XDG_STATE_HOME", ""))
    if os.path.isabs(state_home):
        candidates.append(Path(state_home) / "verbatus")
    for home in (os.environ.get("HOME", ""), pwd.getpwuid(os.getuid()).pw_dir):
        if os.path.isabs(home):
            candidates.append(Path(home) / ".local" / "state" / "verbatus")
    return tuple(dict.fromkeys(candidates))


# Read before any fixture redirects the environment.
ACCOUNT_OPERATOR_STATE_DIRS = _account_operator_state_dirs()


def account_operator_state_snapshot() -> dict[str, tuple[int, int] | None]:
    """Every file under the account's real operator state, by size and mtime."""
    snapshot: dict[str, tuple[int, int] | None] = {}
    for directory in ACCOUNT_OPERATOR_STATE_DIRS:
        snapshot[str(directory)] = None
        for path in sorted(directory.rglob("*")) if directory.is_dir() else ():
            status = path.lstat()
            snapshot[str(path)] = (status.st_size, status.st_mtime_ns)
    return snapshot


@pytest.fixture(autouse=True)
def operator_state_home(tmp_path_factory: pytest.TempPathFactory) -> Iterator[Path]:
    """A fresh XDG_STATE_HOME outside the checkout for every test and its subprocesses.

    The operator CLI otherwise keeps receipts in the account's real state folder, so
    a test that names no state folder would add synthetic records to real history.
    Its own patch, not the test's `monkeypatch`: sharing that would let a test's
    `monkeypatch.undo()` drop the redirect, and would undo the test's patches only
    after module fixtures set up later had restored theirs, leaving their patched
    values behind for later tests.
    """
    state_home = tmp_path_factory.mktemp("xdg-state")
    with pytest.MonkeyPatch.context() as patch:
        patch.setenv("XDG_STATE_HOME", str(state_home))
        yield state_home


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    if "CI" in os.environ:
        return
    skip_local = pytest.mark.skip(reason="hostile_local runs in CI only")
    for item in items:
        if item.get_closest_marker("hostile_local"):
            item.add_marker(skip_local)


def load_stage(stage: str, module: str = "run", *, isolate_path: bool = False) -> ModuleType:
    """A fresh copy of `pipeline/<stage>/<module>.py`, left out of `sys.modules`.

    Stage folders are numbered, not packages, and every entry file is `run.py`,
    so a bare import would bind whichever stage the import cache met first. The
    module is registered only while it executes, because a `slots=True`
    dataclass looks itself up there. `isolate_path` restores `sys.path`
    afterwards, so later bare imports cannot resolve into the stage directory.
    """
    path = ROOT / "pipeline" / stage / f"{module}.py"
    spec = importlib.util.spec_from_file_location(f"{stage}_{module}_under_test", path)
    loaded = importlib.util.module_from_spec(spec)
    original_path = list(sys.path)
    sys.modules[spec.name] = loaded
    try:
        if isolate_path:
            sys.path.insert(0, str(path.parent))
        spec.loader.exec_module(loaded)
    finally:
        del sys.modules[spec.name]
        if isolate_path:
            sys.path[:] = original_path
    return loaded


def code_text(source: object) -> str:
    """The code of a module, class, function or source string, without comments or docstrings.

    Tests that read source must not be satisfied or broken by prose about it.
    """
    text = source if isinstance(source, str) else inspect.getsource(source)
    tree = ast.parse(textwrap.dedent(text))
    for node in ast.walk(tree):
        body = getattr(node, "body", None)
        if (
            body
            and isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef))
            and isinstance(body[0], ast.Expr)
            and isinstance(body[0].value, ast.Constant)
            and isinstance(body[0].value.value, str)
        ):
            node.body = body[1:] or [ast.Pass()]
    return ast.unparse(tree)


@functools.cache
def stage_programs() -> dict[str, str]:
    """The orchestrator's own stage programs, by stage name, in flow order."""
    programs = load_stage("orchestrator").STAGE_PROGRAMS
    assert list(programs) == [
        "door",
        "exemplar",
        "ink-map",
        "designator",
        "attestatores",
        "perlector",
        "coniector",
        "recensor",
        "archetypus",
        "armarium",
    ], "a stage was added to or dropped from the orchestrator's sequence"
    return programs


def programs_through(last: str) -> tuple[str, ...]:
    """The stage programs from the Door through `last`, in flow order."""
    names = list(stage_programs())
    return tuple(stage_programs()[name] for name in names[: names.index(last) + 1])


def run_stage(
    root: Path, run_id: str, scenario: str, program: str, **options: object
) -> subprocess.CompletedProcess[str]:
    """Invoke a fixture stage with the ordinary run arguments."""
    command = [
        sys.executable,
        str(ROOT / program),
        "--run-root",
        str(root),
        "--run-id",
        run_id,
        "--scenario",
        scenario,
    ]
    for name, value in options.items():
        command.extend((f"--{name.replace('_', '-')}", str(value)))
    return subprocess.run(command, cwd=ROOT, capture_output=True, text=True)


def run_orchestrator(
    root: Path, run_id: str, scenario: str, **options: object
) -> subprocess.CompletedProcess[str]:
    """Invoke the fixture orchestrator with the ordinary run arguments."""
    command = [
        sys.executable,
        str(ROOT / "pipeline/orchestrator/run.py"),
        "--fixture",
        "synthetic-two-page-v0",
        "--scenario",
        scenario,
        "--run-id",
        run_id,
        "--run-root",
        str(root),
    ]
    for name, value in options.items():
        command.extend((f"--{name.replace('_', '-')}", str(value)))
    return subprocess.run(command, cwd=ROOT, capture_output=True, text=True)


def run_through(root: Path, run_id: str, scenario: str, last: str) -> None:
    """Run fixture stages through ``last``, asserting each stage completed."""
    for program in programs_through(last):
        result = run_stage(root, run_id, scenario, program)
        assert result.returncode == 0, f"{program}: {result.stderr}"


HELD_RECENSOR_STOP = "stopped at a held recensor, before the archetypus"


def advance_held_recensor(root: Path, run_id: str) -> None:
    """Record a person's advance of the Recensor's current seal, as the advance worker does.

    A run whose Recensor holds anything stops before the Archetypus; with this
    record the next run passes that seal and exports, naming every hold.
    """
    from common.runtree.store import RunTree
    from operations.operator.advance import record_advance, stored_boundary

    tree = RunTree(root, run_id)
    _seal, digest = stored_boundary(tree, "recensor")
    record_advance(
        tree,
        "recensor",
        reason="export with every hold named",
        expected_digest=digest,
        timestamp="2026-10-01T12:00:00Z",
    )


def file_bytes_snapshot(root: Path) -> dict[str, bytes]:
    """Read the bytes of every regular file under a test run root."""
    return {
        str(path.relative_to(root)): path.read_bytes()
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }


def file_digest_snapshot(root: Path) -> dict[str, str]:
    """Hash every regular file under a test run root."""
    return {
        str(path.relative_to(root)): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }


def file_identities(root: Path) -> dict[str, tuple[int, int]]:
    """Device and inode for each file, to distinguish reuse from equal rewrites."""
    return {
        str(path.relative_to(root)): (path.stat().st_dev, path.stat().st_ino)
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }


_DERIVED_INVENTORY_SUFFIXES = (
    "/manifest.json",
    "/manifest-door.json",
    "/index.json",
    "run-health/recensor-partition-receipt.json",
    "run-health/witness-routing.json",
)


def is_immutable_evidence(path: str) -> bool:
    """Exclude inventories and current receipts that a resume may republish."""
    return not path.endswith(_DERIVED_INVENTORY_SUFFIXES)


def tree_snapshot(root: Path) -> dict[str, str]:
    """Describe the root and every entry without following symlinks.

    Include directories and special entries so refusal tests detect partial writes.
    """

    root = Path(root)
    if not root.is_symlink() and not root.exists():
        return {}
    snapshot: dict[str, str] = {".": _describe(root)}
    if root.is_symlink():
        return snapshot
    # A descendant the walk cannot read is not an absent descendant: `os.walk`'s
    # default `onerror=None` drops the subtree in silence, and a refusal probe
    # would then report "no tree change" over entries it never examined. The
    # snapshot fails loudly instead.
    for parent, directory_names, file_names in os.walk(root, onerror=_raise_walk_error):
        for name in (*directory_names, *file_names):
            path = Path(parent) / name
            snapshot[str(path.relative_to(root))] = _describe(path)
    return dict(sorted(snapshot.items()))


def _raise_walk_error(error: OSError) -> None:
    """`os.walk` swallows scan errors by default; a snapshot may not."""
    raise error


def _describe(path: Path) -> str:
    """One entry, as the snapshot records it -- the root included."""
    if path.is_symlink():
        return f"symlink -> {os.readlink(path)}"
    mode = path.lstat().st_mode
    if stat.S_ISDIR(mode):
        return "directory"
    if stat.S_ISREG(mode):
        return f"file {hashlib.sha256(path.read_bytes()).hexdigest()}"
    return "irregular entry"


def rebind_stage_seal_artifact(tree, stage: str, *, rewrite_manifest: bool = True) -> None:
    """Carry a deliberate semantic forgery through its producer's completion seal.

    A stage seal witnesses the producer's whole boundary, so any test that
    rewrites an upstream record now trips the seal first and proves the boundary
    refusal instead of the check it was written for. Rebinding models the other
    hypothesis — a producer that wrote the bad record *and* honestly witnessed
    it — which is the state the downstream check exists to catch and the only
    state in which it is reachable at all.

    Boundary-corruption tests deliberately do NOT rebind: leaving the seal alone
    is what proves the earlier refusal.

    `rewrite_manifest=False` is for the forgeries that leave a dangling input
    reference behind on purpose. `write_manifest` revalidates every input, so it
    would refuse here instead of at the consumer the test is aiming at — and the
    stored inventory is not what the seal is read out of anyway.
    """
    seals = [
        tree.read_artifact(stage, "stage-seal", entry["artifact_id"])
        for entry in tree.build_manifest(stage, verify_inputs=False)["artifacts"]
        if entry["kind"] == "stage-seal"
    ]
    seal = latest_attempt(seals, f"{stage} stage seal", operation="seal")
    payload = seal["payload"]
    seal["payload"] = _stage_seal_payload(
        tree,
        stage,
        payload["attempt_ordinal"],
        seal["attempt_id"],
        verify_inputs=rewrite_manifest,
        verify_blob_addresses=rewrite_manifest,
    )
    seal["self_hash"] = self_hash(seal)
    tree.resolve(tree.artifact_path(stage, "stage-seal", seal["artifact_id"])).write_bytes(
        canonical_bytes(seal)
    )
    if rewrite_manifest:
        tree.write_manifest(stage)


def _repoint_retained_references(tree, node) -> bool:
    """Repoint every `{relative_path, sha256}` pair in a record to the bytes on disk.

    A retained reference is not only the envelope's `inputs` list: a producer that
    reconciles evidence carries the same pair inside its payload (the Designator's
    proposal seal names each act's region and hold evidence that way). Repointing
    one and not the other leaves the record disagreeing with itself, so the walk
    is over the whole record. A reference whose file is absent is left alone --
    that is a dangling reference some tests plant on purpose.
    """
    changed = False
    if isinstance(node, dict):
        path, sha = node.get("relative_path"), node.get("sha256")
        if isinstance(path, str) and isinstance(sha, str):
            try:
                actual = digest_bytes(tree.read_bytes(path))
            except OSError:
                actual = sha
            if actual != sha:
                node["sha256"] = actual
                changed = True
        for value in node.values():
            changed |= _repoint_retained_references(tree, value)
    elif isinstance(node, list):
        for value in node:
            changed |= _repoint_retained_references(tree, value)
    return changed


def rewitness_stage_boundary(tree, stage: str) -> None:
    """Rebind a stage's whole boundary, retained input references included.

    `rebind_stage_seal_artifact` rebinds the seal alone, which is enough while the
    forgery is the last thing in the tree that names the changed bytes. It is not
    enough once some artifact of this stage retained an input reference to them:
    the reference still records the pre-forgery digest, so the next reader stops
    at `the bytes changed under a sealed reference` — this stage's own boundary —
    instead of at the semantic check the calling test is named for.

    Repointing each stale reference to the bytes now on disk models the same
    hypothesis the seal rebind does, one link further out: a producer that read
    the forged record and honestly witnessed what it read. No refusal is
    softened; the boundary checks are satisfied rather than stepped around, so
    the consumer must catch the forgery on its own re-derivation or catch it
    nowhere. Tests aimed *at* a boundary refusal must not call this.

    Artifacts of one stage also reference each other, so rewriting one moves the
    bytes a sibling recorded; the sweep runs to a fixed point.
    """
    for _ in range(len(tree.build_manifest(stage, verify_inputs=False)["artifacts"]) + 1):
        settled = True
        for entry in tree.build_manifest(stage, verify_inputs=False)["artifacts"]:
            path = tree.resolve(entry["relative_path"])
            record = json.loads(path.read_text(encoding="utf-8"))
            if _repoint_retained_references(tree, record):
                payload = record.get("payload")
                # A producer that seals its payload separately must have that
                # inner hash recomputed too, or the reader stops on the payload's
                # own self-hash instead of on the check the calling test names.
                if isinstance(payload, dict) and "self_hash" in payload:
                    payload["self_hash"] = self_hash(payload)
                record["self_hash"] = self_hash(record)
                path.write_bytes(canonical_bytes(record))
                settled = False
        if settled:
            break
    else:  # pragma: no cover - a reference cycle would be a contract failure, not a test bug
        raise AssertionError(f"{stage} input references never settled; the sweep found a cycle")
    rebind_stage_seal_artifact(tree, stage)


@pytest.fixture
def rebind_stage_seal():
    """Expose coherent test forgeries without putting test support in stage code."""
    return rebind_stage_seal_artifact


@pytest.fixture
def rewitness_boundary():
    """The seal rebind above, extended to the stage's retained input references."""
    return rewitness_stage_boundary


@pytest.fixture(scope="session")
def orchestrated_run(tmp_path_factory):
    """`copy(destination, run_id, scenario, expected_exit=0)` lays a fixture orchestrator run
    at `destination`, running the orchestrator once per run id and scenario per session.
    `past_held_recensor=True` carries a run that stops at a held Recensor on to its export,
    through a recorded advance of the Recensor's seal (`advance_held_recensor`).

    The run tree holds no wall-clock time and no path of its own, so a copy is the tree a
    fresh run there would write (pipeline/orchestrator/test_orchestrated_run_copy.py holds
    that); each caller gets its own copy to change. Every run is made under the environment
    as it stood when the fixture was set up, so a test that changed it before asking for a
    copy cannot change the run every later caller shares.
    """
    built: dict[tuple[str, str, bool], tuple[Path, subprocess.CompletedProcess[str]]] = {}
    environment = dict(os.environ)

    def copy(
        destination: Path,
        run_id: str,
        scenario: str,
        expected_exit: int = 0,
        *,
        past_held_recensor: bool = False,
    ) -> Path:
        key = (run_id, scenario, past_held_recensor)
        if key not in built:
            root = tmp_path_factory.mktemp("orchestrated") / "runs"
            with mock.patch.dict(os.environ, environment, clear=True):
                result = run_orchestrator(root, run_id, scenario)
                if past_held_recensor:
                    # Held at its Recensor, the run exports once a person advances that seal.
                    assert HELD_RECENSOR_STOP in result.stdout, result.stdout + result.stderr
                    advance_held_recensor(root, run_id)
                    result = run_orchestrator(root, run_id, scenario)
                built[key] = root, result
        root, result = built[key]
        assert result.returncode == expected_exit, result.stderr
        shutil.copytree(root, destination, symlinks=True)
        return destination

    return copy


@pytest.fixture
def empty_triage_manifest(tmp_path: Path) -> Path:
    """An empty but valid Door decision manifest for malformed-input cases."""
    from door import triage_manifest

    path = tmp_path / "manifest.json"
    path.write_text(
        json.dumps(
            {"schema": triage_manifest.MANIFEST_SCHEMA, "corpus_id": "parish-a", "records": []}
        ),
        encoding="utf-8",
    )
    return path


@pytest.fixture
def absent_third_chair_config(tmp_path: Path) -> Path:
    """Copy the live model config and mark its third witness explicitly absent."""
    config_root = tmp_path / "chair-config"
    shutil.copytree(ROOT / "config" / "model-fixtures", config_root / "model-fixtures")
    shutil.copytree(ROOT / "config" / "manifests", config_root / "manifests")
    live = (ROOT / "config" / "models.toml").read_text(encoding="utf-8")
    assert tomllib.loads(live)["chairs"]["attestator_3"]["state"] == "configured"
    section_start = live.index("[chairs.attestator_3]\n")
    next_table = live.find("\n[", section_start + 1)
    section_end = len(live) - 1 if next_table == -1 else next_table
    absent = """[chairs.attestator_3]
state = "absent"
reason = "fixture test removes this witness without replacing it"
"""
    path = config_root / "models.toml"
    path.write_text(live[:section_start] + absent + live[section_end + 1 :], encoding="utf-8")
    rewritten = tomllib.loads(path.read_text(encoding="utf-8"))
    assert rewritten["chairs"]["attestator_3"]["state"] == "absent"
    assert set(rewritten["chairs"]) == set(tomllib.loads(live)["chairs"]), (
        "the splice changed which chairs the roster declares"
    )
    return path


def reask_recovery_config(directory: Path, page_level_reread: int) -> Path:
    """The committed recovery policy with its page re-ask budget set, written under `directory`."""
    text = (ROOT / "config" / "recovery.toml").read_text(encoding="utf-8")
    assert "\npage_level_reread = 1\n" in text
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / "recovery.toml"
    path.write_text(
        text.replace("\npage_level_reread = 1\n", f"\npage_level_reread = {page_level_reread}\n"),
        "utf-8",
    )
    return path


def floor_models_config(directory: Path, floor: int) -> Path:
    """The live model config with its witness floor set to `floor`, written under `directory`."""
    shutil.copytree(ROOT / "config" / "model-fixtures", directory / "model-fixtures")
    shutil.copytree(ROOT / "config" / "manifests", directory / "manifests")
    live = (ROOT / "config" / "models.toml").read_text(encoding="utf-8")
    assert "\nwitness_floor = 3\n" in live
    path = directory / "models.toml"
    path.write_text(live.replace("\nwitness_floor = 3\n", f"\nwitness_floor = {floor}\n"), "utf-8")
    assert tomllib.loads(path.read_text(encoding="utf-8"))["witness_floor"] == floor
    return path


def build_page_tree(
    base: Path,
    scenario: str,
    run_id: str = "r",
    *,
    floor: int = 3,
    reask: int | None = 0,
    **options,
) -> tuple[Path, dict[str, object]]:
    """A fixture tree read page by page, through the Perlector; returns (root, stage options).

    The committed protocol and roster read page by page. The options, which
    every later stage of the run takes too, name a recovery policy with the page
    re-ask budget `reask` (off by default, so each page stands on its first
    reading; `None` keeps the committed policy and names none), and set the
    witness floor to `floor` when it is not the committed one; `options` adds
    others (for example `witness_context="blinded"`).
    """
    if reask is not None:
        options = {"recovery_config": reask_recovery_config(base / "config", reask), **options}
    if floor != 3:
        options = {"models_config": floor_models_config(base / "models", floor), **options}
    root = base / "runs"
    for program in programs_through("perlector"):
        result = run_stage(root, run_id, scenario, program, **options)
        assert result.returncode == 0, f"{program}: {result.stderr}"
    return root, options


def page_context(
    root: Path,
    run_id: str,
    scenario: str,
    options: dict[str, object],
    stage=None,
    serving_reader=None,
):
    """A page-read tree's context under `options`, opened as `stage` (the Recensor's by
    default), with `serving_reader` for a test that reads a live call again."""
    from common.contracts.stages import RECENSOR
    from common.stage import open_context, stage_parser

    args = stage_parser("page-read test").parse_args(
        [
            "--run-root",
            str(root),
            "--run-id",
            run_id,
            "--scenario",
            scenario,
            *(
                item
                for name, value in options.items()
                for item in (f"--{name.replace('_', '-')}", str(value))
            ),
        ]
    )
    return open_context(args, stage or RECENSOR, serving_reader=serving_reader)


def _stage_records(root: Path, run_id: str, stage_dir: str, kind: str) -> list[tuple[Path, dict]]:
    directory = root / run_id / stage_dir / "artifacts" / kind
    return [
        (path, json.loads(path.read_text(encoding="utf-8")))
        for path in sorted(directory.glob("*.json"))
    ]


def _write_record(path: Path, record: dict) -> None:
    record["self_hash"] = self_hash(
        {key: value for key, value in record.items() if key != "self_hash"}
    )
    path.write_bytes(canonical_bytes(record))


# --- Recensor decisions the fixture cannot reach -------------------------------------
#
# Page-read tests run the real Recensor. These forge its records only for a
# decision it does not make on the fixture's pages, each rewitnessed as a
# Recensor that made it; the partition receipt is not rewritten, since no
# stage after the Recensor reads it.


def forge_page_review(root: Path, run_id: str, act_key: str, outcome: str, **payload) -> None:
    """Give the real Recensor's review of `act_key` another outcome and payload fields."""
    from common.contracts.stages import RECENSOR
    from common.runtree.store import RunTree

    [(path, record)] = [
        (path, record)
        for path, record in _stage_records(root, run_id, "5_recensor", "review")
        if record["payload"]["act_key"] == act_key
    ]
    record["outcome"] = outcome
    record["payload"].update(payload)
    _write_record(path, record)
    rewitness_stage_boundary(RunTree(root, run_id), RECENSOR)


def forge_continuation_links(
    root: Path,
    run_id: str,
    scenario: str,
    options: dict[str, object],
    links: list[tuple[str, str, bool]],
) -> None:
    """Replace the real Recensor's continuation links with `(head_key, tail_key, agreed)` rows.

    Each is written in the `recensor-continuation-link.v1` shape between the two
    named readings, inputting both.
    """
    from common.contracts.identities import attempt_id
    from common.contracts.stages import RECENSOR
    from common.page_review import CONTINUATION_LINK_KIND, CONTINUATION_LINK_SCHEMA
    from common.stage import reading_acts

    context = page_context(root, run_id, scenario, options)
    for path, _record in _stage_records(root, run_id, "5_recensor", CONTINUATION_LINK_KIND):
        path.unlink()
    rows = {row["act_key"]: row for row in reading_acts(context)}
    for head_key, tail_key, agreed in links:
        head, tail = rows[head_key], rows[tail_key]
        subject = f"page-break:{head['page_ordinal']}:{tail['page_ordinal']}"
        record = context.envelope(
            kind=CONTINUATION_LINK_KIND,
            subject_id=subject,
            outcome="accepted" if agreed else "held-for-review",
            attempt=attempt_id(subject, "link", 1),
            inputs=[head["perlectio_ref"], tail["perlectio_ref"]],
            payload={
                "schema": CONTINUATION_LINK_SCHEMA,
                "from_page_ordinal": head["page_ordinal"],
                "to_page_ordinal": tail["page_ordinal"],
                "from_act_id": head["act_id"],
                "from_act_key": head_key,
                "to_act_id": tail["act_id"],
                "to_act_key": tail_key,
                "continues_to_next_page": head["continues_to_next_page"] is True,
                "continues_from_previous_page": tail["continues_from_previous_page"] is True,
                "agreed": agreed,
            },
        )
        path = context.tree.resolve(
            context.tree.artifact_path(RECENSOR, CONTINUATION_LINK_KIND, record["artifact_id"])
        )
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(canonical_bytes(record))
    rewitness_stage_boundary(context.tree, RECENSOR)


# --- dots.mocr seated on index and table pages ----------------------------------------

DOTS_CHAIR = "attestator_4"
DOTS_TIERS = ("generic-24gb", "generic-48gb", "generic-80gb-plus")


def dots_models_config(directory: Path, *, routed: bool = True) -> Path:
    """The committed fixture roster with dots.mocr (`attestator_4`) seated, routed or not.

    Written under `directory`; nothing committed seats the chair. `routed` adds
    `[witness_routing]` (index and table pages only). The synthetic fixture's
    `dots-*` scenarios make page 2 a page the rule routes.
    """
    from common.chairs.manifests import build_manifest, write_manifest

    shutil.copytree(ROOT / "config" / "model-fixtures", directory / "model-fixtures")
    shutil.copytree(ROOT / "config" / "manifests", directory / "manifests")
    snapshot = directory / "model-fixtures" / DOTS_CHAIR
    snapshot.mkdir()
    (snapshot / "identity.txt").write_bytes(f"fixture chair: {DOTS_CHAIR}\n".encode())
    pin = write_manifest(build_manifest(snapshot), directory / "manifests" / f"{DOTS_CHAIR}.json")
    text = (ROOT / "config" / "models.toml").read_text(encoding="utf-8")
    chair = f"""
[chairs.{DOTS_CHAIR}]
state = "configured"
source = "local-repository"
path = "{DOTS_CHAIR}"
digest_manifest = "{pin}"
manifest = "manifests/{DOTS_CHAIR}.json"
serving_recipe = "fake-attestatores-v0"
license_note = "fixture identity only; no model weights or model license apply"
witness_adapter = "dots-mocr.v1"
witness_scope = "page"
"""
    routing = f'\n[witness_routing]\n{DOTS_CHAIR} = "index-and-table.v1"\n' if routed else ""
    path = directory / "models.toml"
    path.write_text(text + chair + routing, encoding="utf-8")
    return path


def dots_serving_recipes(directory: Path) -> Path:
    """The committed fixture catalogue with `attestator_4`'s fixture row at every tier."""
    rows = "".join(
        f"""
[[profiles]]
kind = "fixture"
recipe = "fake-attestatores-v0"
chair = "{DOTS_CHAIR}"
tier = "{tier}"
description = "offline walking-skeleton fixture for Attestator 4 (dots.mocr)"
"""
        for tier in DOTS_TIERS
    )
    path = directory / "serving_recipes.toml"
    path.write_text(
        (ROOT / "config" / "serving_recipes.toml").read_text(encoding="utf-8") + rows,
        encoding="utf-8",
    )
    return path
