"""Unit tests: the real-ingress binding contract for stages after the Door.

The run tree is real to the Ink Map's seal -- the Door, the Exemplar and the Ink
Map run as programs over a genuine real submission, made of the synthetic
fixture's own two pages copied into an approved storage root. These unit tests
hold `common/stage.py` to the real-ingress contract itself.

What is proven, unit by unit:

- `open_stage_context` opens a real run with a registry, the sealed digest map,
  the parsed formats and recovery policy, `fixture=None` behind a refusing
  accessor, and `REAL_SCENARIO` regardless of `--scenario`;
- `_refuse_incompatible_real_reuse` names the sealed policy that moved, fires
  before the predecessor-seal refusal, and writes nothing;
- `exemplar_page_ids` agrees with the fixture declaration on the happy fixture
  run and with the sealed bytes on the real run;
- `refuse_unlive_real_reading` refuses a real submission on a row that is not
  live, by the stage's name, and passes a fixture run, a live row and an
  absent chair.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from common.chairs.models import AbsentChair
from common.contracts.approval import real_ingress_record
from common.contracts.canonical import canonical_bytes, digest_bytes, self_hash
from common.contracts.errors import (
    ContractError,
    IncompatibleReuse,
    SchemaRefusal,
)
from common.contracts.identities import page_id as derive_page_id
from common.contracts.stages import ATTESTATORES, DESIGNATOR, INK_MAP
from common.decoding import DEFAULT_DECODING_CONFIG_PATH
from common.fixture_identity import page_identity
from common.runtree.store import RunTree
from common.stage import (
    DEFAULT_ARMARIUM_FORMATS_CONFIG_PATH,
    REAL_SCENARIO,
    StageContext,
    canary_ordinals,
    exemplar_page_ids,
    load_fixture,
    open_context,
    open_stage_context,
    real_run_policy_digest,
    refuse_unlive_real_reading,
    stage_parser,
    submission_identity,
)
from operations.submit import gate, submit

ROOT = Path(__file__).resolve().parents[1]
ORCHESTRATOR = ROOT / "pipeline" / "orchestrator" / "run.py"
DOOR_CLI = ROOT / "pipeline" / "1_exemplar" / "door.py"
EXEMPLAR_CLI = ROOT / "pipeline" / "1_exemplar" / "run.py"
INK_MAP_CLI = ROOT / "pipeline" / "1_ink_map" / "run.py"
MODELS_CONFIG = ROOT / "config" / "models.toml"
FIXTURE = "synthetic-two-page-v0"
FIXTURE_PAGES = ROOT / "proof" / "fixtures" / FIXTURE
RUN_ID = "real-ingress-unit"
FIXTURE_RUN_ID = "fixture-page-index-unit"


def _run_program(program: Path, *argv: str) -> None:
    result = subprocess.run(
        [sys.executable, str(program), *argv], cwd=ROOT, capture_output=True, text=True
    )
    assert result.returncode == 0, f"{program.name}: {result.stderr}"


@pytest.fixture(scope="module")
def real_template(tmp_path_factory) -> tuple[Path, Path]:
    """One real submission, carried by the real programs to the Ink Map's seal.

    Stopping at the Ink Map is the point: the contexts below open the stages
    after it. Returns the run root and the submission ledger.
    """
    base = tmp_path_factory.mktemp("real-ingress-template")
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
    _run_program(
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
    _run_program(EXEMPLAR_CLI, "--run-root", str(root), "--run-id", RUN_ID)
    _run_program(INK_MAP_CLI, "--run-root", str(root), "--run-id", RUN_ID)
    return root, ledger


@pytest.fixture
def real_root(real_template, tmp_path) -> Path:
    """A private copy of the real run, so each test may publish into it."""
    template, _ledger = real_template
    root = tmp_path / "runs"
    shutil.copytree(template, root)
    return root


@pytest.fixture(scope="module")
def fixture_template(tmp_path_factory) -> Path:
    """The happy synthetic fixture run, Door and Exemplar only."""
    root = tmp_path_factory.mktemp("fixture-page-index-template") / "runs"
    result = subprocess.run(
        [
            sys.executable,
            str(ORCHESTRATOR),
            "--fixture",
            FIXTURE,
            "--scenario",
            "happy",
            "--run-root",
            str(root),
            "--run-id",
            FIXTURE_RUN_ID,
            "--from",
            "door",
            "--to",
            "exemplar",
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    return root


def _args(root: Path, run_id: str = RUN_ID, *extra: str, scenario: str = "happy"):
    """Argv the orchestrator would forward, with the two cwd-relative defaults pinned."""
    return stage_parser("real-ingress unit context").parse_args(
        [
            "--run-root",
            str(root),
            "--run-id",
            run_id,
            "--scenario",
            scenario,
            "--fixture-root",
            str(ROOT / "proof"),
            "--models-config",
            str(MODELS_CONFIG),
            *extra,
        ]
    )


def _open(root: Path, stage: str, *extra: str, scenario: str = "happy") -> StageContext:
    return open_stage_context(_args(root, RUN_ID, *extra, scenario=scenario), stage)


def _snapshot(root: Path) -> dict[str, bytes]:
    return {
        str(path.relative_to(root)): path.read_bytes() for path in root.rglob("*") if path.is_file()
    }


# --- the real context, opened ---------------------------------------------------


def test_a_real_run_opens_with_its_bindings_and_no_fixture(real_root):
    """`--scenario` is argv nobody sealed on this route, so it is ignored, not
    honoured: the context's scenario is the constant, and its fixture refuses."""
    context = _open(real_root, DESIGNATOR, scenario="no-such-declared-scenario")

    assert context.scenario == REAL_SCENARIO
    assert context.stage == DESIGNATOR
    assert context.registry is not None
    assert context.armarium_formats is not None
    assert context.serving_config_inputs is not None
    sealed = context.sealed_config_digests
    assert {"models", "armarium-formats", "run-policy", "decoding", "recovery"} <= set(sealed)
    assert context.recovery_policy["config_sha256"] == sealed["recovery"]
    with pytest.raises(ContractError, match="designator asked its context for fixture"):
        _ = context.fixture


# --- the binding recheck ----------------------------------------------------------


def _moved_models_config(tmp_path: Path) -> Path:
    """A roster whose only movement is one chair's record, not its membership."""
    config_root = tmp_path / "chair-config"
    shutil.copytree(ROOT / "config" / "model-fixtures", config_root / "model-fixtures")
    shutil.copytree(ROOT / "config" / "manifests", config_root / "manifests")
    live = MODELS_CONFIG.read_text(encoding="utf-8")
    note = 'license_note = "fixture identity only; no model weights or model license apply"'
    assert note in live
    moved = live.replace(note, 'license_note = "a moved chair record"', 1)
    path = config_root / "models.toml"
    path.write_text(moved, encoding="utf-8")
    return path


def _moved(tmp_path: Path, source: Path, old: str, new: str) -> Path:
    """One value the run never sealed."""
    live = source.read_text(encoding="utf-8")
    assert old in live
    copy = tmp_path / source.name
    copy.write_text(live.replace(old, new, 1), encoding="utf-8")
    return copy


@pytest.mark.parametrize(
    ("flag", "value", "named"),
    [
        (
            "--decoding-config",
            lambda tmp: _moved(
                tmp, DEFAULT_DECODING_CONFIG_PATH, "page_max_tokens = 12288", "page_max_tokens = 1"
            ),
            "decoding",
        ),
        ("--models-config", _moved_models_config, "models"),
        (
            "--formats-config",
            lambda tmp: _moved(
                tmp,
                DEFAULT_ARMARIUM_FORMATS_CONFIG_PATH,
                "embed_pixels = false",
                "embed_pixels = true",
            ),
            "armarium-formats",
        ),
        ("--witness-context", lambda _tmp: "blinded", "run-policy"),
    ],
)
def test_a_moved_input_is_refused_by_name_before_the_seal_check_and_writes_nothing(
    real_root, tmp_path, flag, value, named
):
    """The leg that proves the constructor, not only the seal check.

    Opened for the Attestatores on a tree with no Designator seal at all: a
    refusal that reached the predecessor check would be `SchemaRefusal`; the
    binding refusal is `IncompatibleReuse`, names the policy that moved, and
    leaves every byte where it was.
    """
    before = _snapshot(real_root)

    with pytest.raises(IncompatibleReuse) as refusal:
        _open(real_root, ATTESTATORES, flag, str(value(tmp_path)))

    message = str(refusal.value)
    assert f"sealed configuration {named} moved" in message, message
    assert message.endswith(
        "No stage work was written. Resume with the original sealed inputs, or start a "
        "new run for the changed inputs"
    )
    assert "no stage-seal" not in message
    assert _snapshot(real_root) == before


def test_an_unmoved_input_reaches_the_seal_refusal_not_the_binding_one(real_root):
    """The other half of the ordering claim: with nothing moved, the seal is next."""
    with pytest.raises(SchemaRefusal, match="predecessor designator has no stage-seal"):
        _open(real_root, ATTESTATORES)


def test_a_run_sealed_before_the_real_only_names_existed_cannot_be_resumed(real_root):
    """An absent name is named apart from a moved one; it needs a different repair."""
    tree = RunTree(real_root, RUN_ID)
    path = tree.resolve("run.json")
    run = json.loads(path.read_text(encoding="utf-8"))
    del run["sealed_config_digests"]["models"]
    run["self_hash"] = self_hash(run)
    path.write_bytes(canonical_bytes(run))

    with pytest.raises(IncompatibleReuse) as refusal:
        _open(real_root, INK_MAP)

    assert "sealed no digest for the models configuration" in str(refusal.value)
    assert "moved" not in str(refusal.value)


def test_a_run_with_reversed_witness_chairs_is_refused_by_name(real_root):
    """The roster-membership leg: `witness_chairs` compared, not merely present."""
    tree = RunTree(real_root, RUN_ID)
    path = tree.resolve("run.json")
    run = json.loads(path.read_text(encoding="utf-8"))
    chairs = run["witness_chairs"]
    reversed_chairs = list(reversed(chairs))
    assert reversed_chairs != chairs, "witness_chairs must have more than one distinct entry"
    run["witness_chairs"] = reversed_chairs
    run["self_hash"] = self_hash(run)
    path.write_bytes(canonical_bytes(run))

    with pytest.raises(IncompatibleReuse, match="witness_chairs"):
        _open(real_root, INK_MAP)


def test_a_run_with_a_moved_door_adapter_recipe_is_refused_by_name(real_root):
    """The adapter-recipe leg, which is what binds `REAL_DOOR_ADAPTER_REVISION`."""
    tree = RunTree(real_root, RUN_ID)
    path = tree.resolve("run.json")
    run = json.loads(path.read_text(encoding="utf-8"))
    assert run["adapter_recipes"]["door"] != "exemplar-door-v4"
    run["adapter_recipes"] = {**run["adapter_recipes"], "door": "exemplar-door-v4"}
    run["self_hash"] = self_hash(run)
    path.write_bytes(canonical_bytes(run))

    with pytest.raises(IncompatibleReuse, match="adapter_recipes"):
        _open(real_root, INK_MAP)


def test_a_run_sealed_with_no_data_handling_digest_is_refused_by_name(real_root):
    """The presence leg: `data-handling` is real-only and checked apart from the
    digest-map comparison the other sealed names go through."""
    tree = RunTree(real_root, RUN_ID)
    path = tree.resolve("run.json")
    run = json.loads(path.read_text(encoding="utf-8"))
    assert "data-handling" in run["sealed_config_digests"]
    del run["sealed_config_digests"]["data-handling"]
    run["self_hash"] = self_hash(run)
    path.write_bytes(canonical_bytes(run))

    with pytest.raises(IncompatibleReuse) as refusal:
        _open(real_root, INK_MAP)

    assert "sealed no digest for the data-handling configuration" in str(refusal.value)


def test_run_policy_digest_moves_with_each_of_its_fields():
    base = dict(
        witness_context="named",
        mechanics_qualification=False,
    )
    moved = {
        "witness_context": "blinded",
        # A run created ordinarily must not resume under the mechanics flag and
        # pass the reuse check, mixing ordinary and mechanics-only artefacts in
        # one tree.
        "mechanics_qualification": True,
    }
    assert real_run_policy_digest(**base) == real_run_policy_digest(**base)
    for field, value in moved.items():
        assert real_run_policy_digest(**{**base, field: value}) != real_run_policy_digest(**base), (
            field
        )
    with pytest.raises(ContractError, match="mechanics_qualification must be a bool"):
        real_run_policy_digest(**{**base, "mechanics_qualification": 1})


# --- one page index for both routes ----------------------------------------------


def test_exemplar_page_ids_equals_the_fixture_declaration_on_the_happy_run(fixture_template):
    """No byte moves: the index says exactly what `page_identity` said.

    Opened through `open_stage_context`, so this is also the synthetic branch of
    the constructor -- `open_context` handed the tree and authority it read.
    """
    context = open_stage_context(_args(fixture_template, FIXTURE_RUN_ID), INK_MAP)
    fixture = load_fixture(str(ROOT / "proof"))
    happy_pages = [
        page for page in fixture["page"] if "scenarios" not in page or "happy" in page["scenarios"]
    ]

    assert exemplar_page_ids(context) == {
        page["ordinal"]: page_identity(fixture, page["ordinal"]) for page in happy_pages
    }
    assert context.fixture == fixture
    assert context.scenario == "happy"
    assert submission_identity(context.run) is None


def test_exemplar_page_ids_on_a_real_run_derive_from_the_sealed_bytes(real_root, real_template):
    _template, ledger = real_template
    context = _open(real_root, INK_MAP)

    assert exemplar_page_ids(context) == {
        ordinal: derive_page_id(
            {"kind": "source", "sha256": digest_bytes((FIXTURE_PAGES / name).read_bytes())},
            {"operation": "whole"},
        )
        for ordinal, name in ((1, "page-1.png"), (2, "page-2.png"))
    }
    assert submission_identity(context.run) == json.loads(ledger.read_text())["self_hash"]


# --- submission_identity refuses a forged or absent filename ledger --------------


def test_only_a_sealed_canary_ledger_marks_pages_and_preserves_submission_identity():
    real, canary = "a" * 64, "b" * 64
    run = {
        "ingress": real_ingress_record(),
        "sealed_config_digests": {"canary-ledger": canary},
        "source_manifest": [
            {"ordinal": 1, "ledger_sha256": real, "relative_path": "canary/x.jpg"},
            {"ordinal": 2, "ledger_sha256": canary, "relative_path": "bird.jpg"},
        ],
    }
    assert canary_ordinals(run) == {2}
    assert submission_identity(run) == real
    assert canary_ordinals({**run, "sealed_config_digests": {}}) == set()


def test_submission_identity_refuses_a_real_run_with_no_source_manifest():
    """No `source_manifest` at all: nothing to name a submission by."""
    run = {"ingress": real_ingress_record()}
    with pytest.raises(ContractError, match="no submitted source manifest to name a submission by"):
        submission_identity(run)


def test_submission_identity_refuses_a_real_run_naming_two_filename_ledgers():
    """Two source rows disagreeing on `ledger_sha256`: no single identity to choose."""
    run = {
        "ingress": real_ingress_record(),
        "source_manifest": [{"ledger_sha256": "a" * 64}, {"ledger_sha256": "b" * 64}],
    }
    with pytest.raises(ContractError, match="filename ledgers, not one"):
        submission_identity(run)


def test_submission_identity_refuses_a_real_run_with_a_non_sha256_ledger():
    """A `ledger_sha256` that is not a sha256 hex string: nothing may stand in for it."""
    run = {
        "ingress": real_ingress_record(),
        "source_manifest": [{"ledger_sha256": "not-a-sha256"}],
    }
    with pytest.raises(ContractError, match="no filename-ledger sha256"):
        submission_identity(run)


def test_open_context_takes_the_tree_and_its_authority_together(fixture_template):
    tree = RunTree(fixture_template, FIXTURE_RUN_ID)
    with pytest.raises(ContractError, match="together or neither"):
        open_context(_args(fixture_template, FIXTURE_RUN_ID), INK_MAP, tree=tree)


def _bare_context(run: dict) -> StageContext:
    return StageContext(
        tree=None,
        run=run,
        fixture=None,
        scenario=REAL_SCENARIO,
        stage="test",
        adapter_revision=None,
        args=None,
        registry=None,
    )


def test_a_real_submission_on_a_row_that_is_not_live_is_refused_by_the_stage_name():
    real = _bare_context({"ingress": real_ingress_record()})
    chair = SimpleNamespace(role="reconstructor")
    with pytest.raises(
        ContractError, match="the Coniector cannot read a real submission"
    ) as caught:
        refuse_unlive_real_reading(real, chair, "fixture", stage="Coniector")
    assert "'reconstructor'" in str(caught.value)
    refuse_unlive_real_reading(real, chair, "live")
    refuse_unlive_real_reading(real, AbsentChair(role="perlector", reason="absent"), "fixture")
    refuse_unlive_real_reading(_bare_context({}), chair, "fixture")
