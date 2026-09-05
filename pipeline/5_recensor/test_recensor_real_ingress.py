"""Real-ingress contexts for the Perlector, the Recensor and the Archetypus.

Three stages, one test file, because the wiring is one change: each stage's
`main` opens through `common.stage.open_stage_context` instead of the
fixture-only `open_context`, and the two fixture concepts that lived in this
group -- the Perlector's declared `reading_failure` and the Recensor's declared
`hold_acts` -- are answered by name on a real submission rather than by an empty
table. The stage programs are loaded by path under unambiguous names (every
stage has a top-level `run`), and the real run is driven by the real programs:
Door, Exemplar and Ink Map over the synthetic fixture's own two pages, submitted
as a real folder. No pod, no socket, no model.

What is proven:

- each `main` hands its parsed argv, its own stage name and the registry factory
  it was given to `open_stage_context`, and calls no opener of its own;
- `declared_reading_failure` is `None` on a real run without touching the
  refusing fixture accessor, and still reads the declaration on the fixture route;
- `fixture_reader_for` refuses a real submission whose sealed row is not live,
  by name, and needs no reader for an absent chair;
- `declared_unreconciled` has no producer on a real run, so the review route is
  silent about cross-act reconciliation there;
- driven as programs over a real submission, all three stages refuse at their
  own missing predecessor seal with their contexts already opened: no "sealed no
  digest", no fixture accessor, no binding refusal, no traceback, nothing written.
"""

from __future__ import annotations

import importlib.util
import json
import shutil
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

import common.stage as stage_module
from common.chairs.models import AbsentChair
from common.chairs.registry import ChairRegistry
from common.contracts.approval import real_ingress_record
from common.contracts.errors import ContractError
from common.contracts.stages import ARCHETYPUS, PERLECTOR, RECENSOR, SEAL_PREDECESSORS
from common.stage import EXIT_FATAL, REAL_SCENARIO, StageContext
from operations.submit import gate, submit

ROOT = Path(__file__).resolve().parents[2]
PIPELINE = ROOT / "pipeline"
DOOR_CLI = PIPELINE / "1_exemplar" / "door.py"
EXEMPLAR_CLI = PIPELINE / "1_exemplar" / "run.py"
INK_MAP_CLI = PIPELINE / "1_ink_map" / "run.py"
STAGE_PROGRAMS = {
    PERLECTOR: PIPELINE / "4_perlector" / "run.py",
    RECENSOR: PIPELINE / "5_recensor" / "run.py",
    ARCHETYPUS: PIPELINE / "6_archetypus" / "run.py",
}
FIXTURE_PAGES = ROOT / "proof" / "fixtures" / "synthetic-two-page-v0"
RUN_ID = "real-ingress-stages"


def _load_stage(stage: str):
    """Load one stage's `run.py` under a name that cannot collide with another's.

    Each stage program inserts its own directory at the front of `sys.path` for
    its sibling modules; the insertion is undone here so loading the Perlector
    first cannot make the Recensor's siblings resolve to the wrong directory.
    """
    program = STAGE_PROGRAMS[stage]
    spec = importlib.util.spec_from_file_location(f"{stage}_run_under_real_ingress_test", program)
    module = importlib.util.module_from_spec(spec)
    original_path = list(sys.path)
    try:
        sys.path.insert(0, str(program.parent))
        spec.loader.exec_module(module)
    finally:
        sys.path[:] = original_path
    return module


PERLECTOR_RUN = _load_stage(PERLECTOR)
RECENSOR_RUN = _load_stage(RECENSOR)
ARCHETYPUS_RUN = _load_stage(ARCHETYPUS)


class _Opened(Exception):
    """Raised by the stub constructor so `main` stops at the seam under test."""


def _real_context(stage: str, **fields) -> StageContext:
    """A bare real-ingress context: `fixture=None`, the constant scenario, no tree."""
    return StageContext(
        tree=None,
        run={"ingress": real_ingress_record(), **fields},
        fixture=None,
        scenario=REAL_SCENARIO,
        stage=stage,
        adapter_revision=None,
        args=None,
        registry=None,
    )


def _fixture_context(stage: str, fixture: dict, scenario: str) -> StageContext:
    """A bare fixture-route context; no ingress record, as this stage's older trees."""
    return StageContext(
        tree=None,
        run={},
        fixture=fixture,
        scenario=scenario,
        stage=stage,
        adapter_revision=None,
        args=None,
        registry=None,
    )


# --- the constructor seam ---------------------------------------------------------


@pytest.mark.parametrize(
    "module, stage",
    [
        (PERLECTOR_RUN, PERLECTOR),
        (RECENSOR_RUN, RECENSOR),
        (ARCHETYPUS_RUN, ARCHETYPUS),
    ],
    ids=[PERLECTOR, RECENSOR, ARCHETYPUS],
)
def test_each_stage_opens_through_the_shared_constructor_and_owns_no_opener(
    module, stage, monkeypatch
):
    """`main` hands argv, its own stage name and the registry factory it was
    given to `common.stage.open_stage_context`, which decides the route from one
    read of the run authority. Nothing before that call may run, so the stub
    stops `main` there; nothing else in the module may open a run on its own."""
    args = SimpleNamespace(run_root="unused", run_id="r")
    opened = []

    def registry_factory(_path):
        raise AssertionError("the stage must pass the factory through, not resolve it")

    def open_stage_context(observed_args, observed_stage, *, registry_factory):
        opened.append((observed_args, observed_stage, registry_factory))
        raise _Opened

    def open_context(*_args, **_kwargs):
        raise AssertionError("the fixture-only opener must not be called by main")

    class _Parser:
        @staticmethod
        def parse_args():
            return args

    monkeypatch.setattr(module, "stage_parser", lambda *_args: _Parser())
    monkeypatch.setattr(module, "open_stage_context", open_stage_context)
    if hasattr(module, "open_context"):
        monkeypatch.setattr(module, "open_context", open_context)

    with pytest.raises(_Opened):
        module.main(registry_factory=registry_factory)
    assert opened == [(args, stage, registry_factory)]
    assert not hasattr(module, "_open"), "a stage-private opener is the drift this closes"


# --- the Perlector's two fixture concepts -------------------------------------------


def test_a_real_run_declares_no_reading_failure_and_never_asks_for_the_fixture():
    """`None` by name on the real route. The refusing accessor would have turned a
    `.get` into a refusal; the point is that the function never reaches it, so a
    real reading's outcome is the engine's stop reason and nothing else."""
    context = _real_context(PERLECTOR)

    assert PERLECTOR_RUN.declared_reading_failure(context, "structural:1:1") is None
    with pytest.raises(ContractError, match="perlector asked its context for fixture"):
        _ = context.fixture


def test_the_fixture_route_still_reads_its_declared_reading_failure():
    """The branch is gated on the run authority, not on the fixture's shape."""
    fixture = {
        "reading_failure": [
            {"scenario": "declared", "act_key": "k", "outcome": "truncated"},
            {"scenario": "other", "act_key": "k", "outcome": "no-readable-text"},
        ]
    }
    context = _fixture_context(PERLECTOR, fixture, "declared")

    assert PERLECTOR_RUN.declared_reading_failure(context, "k") == "truncated"
    assert PERLECTOR_RUN.declared_reading_failure(context, "absent") is None


def test_the_fixture_reader_is_refused_by_name_on_a_real_submission():
    """A configured chair whose sealed row is not live has nothing to read from
    on a real run: no declaration exists, and one cannot be invented."""
    context = _real_context(PERLECTOR)
    chair = SimpleNamespace(role="perlector")

    with pytest.raises(ContractError) as refusal:
        PERLECTOR_RUN.fixture_reader_for(context, chair, "fixture")
    message = str(refusal.value)
    assert "cannot read a real submission through the fixture reader" in message
    assert "'perlector'" in message
    assert "asked its context for fixture" not in message, "refused by name, not by accessor"


def test_the_fixture_reader_refusal_actually_fires_before_any_write(real_root, monkeypatch):
    """Drives `_read_the_acts` itself, so deleting the refusal -- or
    introducing a write ahead of it under any name -- fails this test. It is
    the whole proof that the refusal lands before the partition blob is
    written: a companion test comparing the two calls' positions in the
    function's source text asserted the same ordering far more weakly, and
    would have gone on passing over any rewrite that kept the textual order
    while changing what runs. `verify_predecessor_seal` is stubbed out because
    the real route that would reach this line refuses earlier still, at the
    predecessor seal (see the parametrized test above) -- there is no real
    Designator yet to build a submission past that point -- and the chair and
    serving-mode resolution are pinned directly to the case under test rather
    than requiring a real catalogue and a live-tier flag.
    """
    monkeypatch.chdir(ROOT)
    monkeypatch.setattr(stage_module, "verify_predecessor_seal", lambda *_a, **_k: None)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            str(STAGE_PROGRAMS[PERLECTOR]),
            "--run-root",
            str(real_root),
            "--run-id",
            RUN_ID,
        ],
    )
    monkeypatch.setattr(
        PERLECTOR_RUN, "perlector_chair", lambda context: SimpleNamespace(role="perlector")
    )
    monkeypatch.setattr(PERLECTOR_RUN, "perlector_serving_mode", lambda *_a, **_k: "fixture")

    before = _tree_bytes(real_root)
    with pytest.raises(
        ContractError, match="cannot read a real submission through the fixture reader"
    ):
        PERLECTOR_RUN._read_the_acts(
            registry_factory=ChairRegistry.from_toml,
            serving_factory=None,
            service=None,
        )
    assert _tree_bytes(real_root) == before, "a refused fixture reader must write nothing"


def test_an_absent_chair_on_a_real_run_needs_no_reader_and_a_live_row_starts_none_yet():
    """Two `None`s for two different reasons: an absent chair reads nothing and
    publishes `not-run` for every act; a live row starts its chair on first use."""
    context = _real_context(PERLECTOR)
    absent = AbsentChair(role="perlector", reason="test-only absence")

    assert PERLECTOR_RUN.fixture_reader_for(context, absent, "fixture") is None
    assert PERLECTOR_RUN.fixture_reader_for(context, SimpleNamespace(role="p"), "live") is None


def test_the_fixture_route_constructs_its_reader_exactly_as_before():
    fixture = {"act": [], "page": [], "scenario": []}
    context = _fixture_context(PERLECTOR, fixture, "happy")

    reader = PERLECTOR_RUN.fixture_reader_for(context, SimpleNamespace(role="p"), "fixture")

    assert isinstance(reader, PERLECTOR_RUN.FixtureReader)
    assert reader._fixture is fixture
    assert reader._scenario == "happy"


# --- the Recensor's one fixture concept ---------------------------------------------


def test_unreconciled_has_no_producer_on_a_real_run_and_the_route_stays_silent():
    """`False` because nothing fed the cause, not because anything measured it;
    with every other cause absent the route composes to no hold at all."""
    assert RECENSOR_RUN.declared_unreconciled(None, "structural:1:1") is False
    assert (
        RECENSOR_RUN.review_route_from_findings(
            testimony_shortfall=None,
            audit_unresolved=None,
            under_witnessed=False,
            unreconciled=RECENSOR_RUN.declared_unreconciled(None, "structural:1:1"),
        )
        is None
    )


def test_declared_recovery_has_no_producer_on_a_real_run_and_still_reads_a_fixture_scenario():
    """`False` because nothing fed the cause, not because any act was measured
    as needing no recovery; a fixture scenario that declares `recover_acts`
    still feeds it on the fixture route."""
    assert RECENSOR_RUN.declared_recovery(None, "structural:1:1") is False

    fixture = {
        "act": [],
        "page": [],
        "scenario": [{"name": "recovered", "recover_acts": ["held-act"]}],
    }
    scenario = RECENSOR_RUN.declared_scenario(_fixture_context(RECENSOR, fixture, "recovered"))
    assert RECENSOR_RUN.declared_recovery(scenario, "held-act") is True
    assert RECENSOR_RUN.declared_recovery(scenario, "other-act") is False


def test_declared_scenario_is_none_on_a_real_run_and_the_declared_row_on_the_fixture_route():
    """The one branch `main` reads the scenario through, named as its own function.

    A real submission carries no fixture to declare one, and the refusing
    accessor is never touched to find that out. The fixture route still reads
    the exact row `scenario_for` names.
    """
    assert RECENSOR_RUN.declared_scenario(_real_context(RECENSOR)) is None

    fixture = {"act": [], "page": [], "scenario": [{"name": "held", "hold_acts": ["held-act"]}]}
    scenario = RECENSOR_RUN.declared_scenario(_fixture_context(RECENSOR, fixture, "held"))
    assert scenario == {"name": "held", "hold_acts": ["held-act"]}


def test_a_declared_scenario_still_feeds_unreconciled_on_the_fixture_route():
    scenario = {"name": "held", "hold_acts": ["held-act"]}

    assert RECENSOR_RUN.declared_unreconciled(scenario, "held-act") is True
    assert RECENSOR_RUN.declared_unreconciled(scenario, "other-act") is False
    outcome, reason = RECENSOR_RUN.review_route_from_findings(
        testimony_shortfall=None,
        audit_unresolved=None,
        under_witnessed=False,
        unreconciled=RECENSOR_RUN.declared_unreconciled(scenario, "held-act"),
    )
    assert outcome == "held-for-review"
    assert "did not reconcile" in reason


def test_the_real_route_reads_the_ingress_record_the_constructor_read():
    """The same reading `common.stage` makes: absent is synthetic, present must parse."""
    assert RECENSOR_RUN.real_ingress(_real_context(RECENSOR)) is True
    assert RECENSOR_RUN.real_ingress(_fixture_context(RECENSOR, {}, "happy")) is False
    assert PERLECTOR_RUN.real_ingress(_real_context(PERLECTOR)) is True
    assert PERLECTOR_RUN.real_ingress(_fixture_context(PERLECTOR, {}, "happy")) is False


# --- the programs, over a real submission ------------------------------------------


def _run_program(program: Path, *argv: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(program), *argv], cwd=ROOT, capture_output=True, text=True
    )


@pytest.fixture(scope="module")
def real_template(tmp_path_factory) -> Path:
    """One real submission, carried by the real programs to the Ink Map's seal.

    The Designator refuses on real ingress by design, so this is as far as any
    real run goes today; the three stages under test open on top of it.
    """
    base = tmp_path_factory.mktemp("real-ingress-stages-template")
    approved = base / "approved-storage"
    source = approved / "submitted-pages"
    source.mkdir(parents=True)
    for name in ("page-1.png", "page-2.png"):
        shutil.copyfile(FIXTURE_PAGES / name, source / name)
    policy = json.loads(gate.DEFAULT_POLICY_PATH.read_text(encoding="utf-8"))
    policy["storage_roots"] = [str(approved)]
    policy_path = base / "data-gate-policy.json"
    policy_path.write_text(json.dumps(policy), encoding="utf-8")
    ledger = approved / "submission-ledger.json"
    submit.submit(source, ledger, policy_path=policy_path)
    root = approved / "runs"
    door = _run_program(
        DOOR_CLI,
        "--run-root",
        str(root),
        "--run-id",
        RUN_ID,
        "--submission-folder",
        str(source),
        "--submission-manifest",
        str(ledger),
        "--data-gate-policy",
        str(policy_path),
    )
    assert door.returncode == 0, door.stderr
    for program in (EXEMPLAR_CLI, INK_MAP_CLI):
        result = _run_program(program, "--run-root", str(root), "--run-id", RUN_ID)
        assert result.returncode == 0, f"{program.name}: {result.stderr}"
    return root


@pytest.fixture
def real_root(real_template, tmp_path) -> Path:
    root = tmp_path / "runs"
    shutil.copytree(real_template, root)
    return root


def _tree_bytes(root: Path) -> dict[str, bytes]:
    return {
        str(path.relative_to(root)): path.read_bytes() for path in root.rglob("*") if path.is_file()
    }


@pytest.mark.parametrize("stage", [PERLECTOR, RECENSOR, ARCHETYPUS])
def test_each_stage_refuses_a_real_run_at_its_predecessor_seal_with_its_context_open(
    real_root, stage
):
    """The honest claim of the wiring, and its whole value today.

    A stage that reached the predecessor-seal refusal is a stage whose context
    opened: the fixture-only opener would have refused first with a binding
    mismatch (`bound to different config_digest`), a context without the sealed
    map would have refused with `sealed no digest`, and an unconverted fixture
    reader would have named the accessor. None of those appears; the tree is
    byte-identical; and the refusal is a named one, not a traceback.
    """
    before = _tree_bytes(real_root)

    result = _run_program(STAGE_PROGRAMS[stage], "--run-root", str(real_root), "--run-id", RUN_ID)

    assert result.returncode == EXIT_FATAL, result.stderr
    assert f"predecessor {SEAL_PREDECESSORS[stage]} has no stage-seal" in result.stderr
    assert "sealed no digest" not in result.stderr
    assert "asked its context for fixture declarations" not in result.stderr
    assert "bound to different" not in result.stderr
    assert "Traceback" not in result.stderr
    assert _tree_bytes(real_root) == before, "a refused open writes nothing"
