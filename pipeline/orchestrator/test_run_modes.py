"""The staged driver is one sequence, irrespective of how an operator enters it."""

from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from common import stage as stage_module
from common.chairs.model_store import StoreRoleFetcher
from common.contracts.errors import ContractError
from common.stage import EXIT_HELD
from conftest import HELD_RECENSOR_STOP, advance_held_recensor, load_stage
from conftest import file_bytes_snapshot as snapshot

ROOT = Path(__file__).resolve().parents[2]
ORCHESTRATOR = ROOT / "pipeline" / "orchestrator" / "run.py"
FIXTURE = "synthetic-two-page-v0"
# Keep this list independent of the implementation: importing the production
# sequence would make the byte-identity test accept the same missing member.
SEQUENCE = (
    "door",
    "exemplar",
    "ink-map",
    "designator",
    "attestatores",
    "perlector",
    "recensor",
    "archetypus",
    "coniector",
    "armarium",
)


def drive(root: Path, run_id: str, scenario: str, *selection: str) -> subprocess.CompletedProcess:
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
        capture_output=True,
        text=True,
    )


@pytest.fixture(scope="module")
def automatic_once(tmp_path_factory) -> Path:
    """`--all` over page-unbroken, run once per module; tests take `automatic`, a copy of it."""
    root = tmp_path_factory.mktemp("automatic") / "runs"
    result = drive(root, "r", "page-unbroken", "--all")
    assert result.returncode == 0, result.stdout + result.stderr
    return root


@pytest.fixture
def automatic(automatic_once, tmp_path) -> Path:
    """This test's own copy of the `--all` run, free to change."""
    return shutil.copytree(automatic_once, tmp_path / "automatic", symlinks=True)


def test_store_root_reaches_a_stage_registry(tmp_path, monkeypatch) -> None:
    orchestrator = load_stage("orchestrator")
    names = (
        "submission_folder",
        "submission_manifest",
        "canary_folder",
        "canary_manifest",
        "data_gate_policy",
        "triage_decision_manifest",
        "triage_clusters",
        "triage_producer_recipe",
        "corpus_register",
        "cache_root",
        "fixture_root",
        "decoding_config",
        "serving_recipes_config",
        "pdf_render_config",
        "designator_geometry_config",
        "alignment_config",
        "page_accounting_config",
        "reconstruction_config",
        "ink_map_config",
        "formats_config",
        "recovery_config",
        "hard_failure_config",
        "review_config",
        "pdf_target_dpi",
        "placement_tier",
        "witness_context",
        "witness_context_config",
        "perlector_protocol_config",
        "perlector_audit_config",
    )
    args = argparse.Namespace(**{name: None for name in names})
    args.run_root = tmp_path / "runs"
    args.run_id = "r"
    args.scenario = "page-unbroken"
    args.models_config = str(tmp_path / "models.toml")
    args.store_root = tmp_path / "store"
    commands = []
    monkeypatch.setattr(
        orchestrator.subprocess,
        "run",
        lambda command, **kwargs: (
            commands.append(command) or subprocess.CompletedProcess(command, 0)
        ),
    )
    assert orchestrator.invoke("pipeline/3_attestatores/run.py", args) == 0
    command = commands[0]
    assert command[command.index("--store-root") + 1] == str(args.store_root)

    received = {}

    def registry_factory(config, **kwargs):  # type: ignore[no-untyped-def]
        received.update(kwargs)
        return config

    assert stage_module._open_registry(args, registry_factory) == args.models_config
    assert isinstance(received["fetcher"], StoreRoleFetcher)
    assert received["fetcher"].root == args.store_root


def test_canary_ingress_requires_a_real_submission_and_a_pair():
    orchestrator = load_stage("orchestrator")
    args = argparse.Namespace(
        submission_folder=None,
        submission_manifest=None,
        data_gate_policy=None,
        canary_folder="/private/birds",
        canary_manifest="/private/birds.json",
        triage_decision_manifest=None,
        triage_clusters=None,
        triage_producer_recipe=None,
        corpus_register=None,
    )
    with pytest.raises(ContractError, match="canary input requires a real submission"):
        orchestrator.require_coherent_ingress_options(args)
    args.submission_folder = "/private/real"
    args.canary_manifest = None
    with pytest.raises(ContractError, match="supplied together"):
        orchestrator.require_coherent_ingress_options(args)


def test_all_and_manual_stages_write_the_identical_happy_run_tree(automatic, tmp_path):
    manual = tmp_path / "manual"

    for stage in SEQUENCE:
        result = drive(manual, "r", "page-unbroken", "--stage", stage)
        assert result.returncode == 0, result.stdout + result.stderr

    assert snapshot(manual) == snapshot(automatic)


def test_all_and_a_split_semi_range_write_the_identical_happy_run_tree(automatic, tmp_path):
    split = tmp_path / "split"

    first = drive(split, "r", "page-unbroken", "--from", "door", "--to", "recensor")
    assert first.returncode == 0, first.stdout + first.stderr
    second = drive(split, "r", "page-unbroken", "--from", "archetypus", "--to", "armarium")
    assert second.returncode == 0, second.stdout + second.stderr

    assert snapshot(split) == snapshot(automatic)


def test_from_refuses_an_unsealed_predecessor_by_name(tmp_path):
    root = tmp_path / "runs"
    assert drive(root, "r", "page-unbroken", "--stage", "door").returncode == 0

    result = drive(root, "r", "page-unbroken", "--from", "designator", "--to", "designator")

    assert result.returncode == 2
    assert "predecessor ink-map has no stage-seal" in result.stderr


def test_invalid_selection_combinations_refuse_before_creating_a_tree(tmp_path):
    cases = (
        ("--all", "--stage", "door"),
        ("--all", "--from", "door", "--to", "exemplar"),
        ("--all", "--to", "door"),
        ("--stage", "door", "--to", "door"),
        ("--from", "door"),
        ("--to", "door"),
        ("--from", "exemplar", "--to", "door"),
        ("--stage", "door", "--mode", "auto"),
        ("--mode", "manual"),
    )

    for index, selection in enumerate(cases):
        root = tmp_path / str(index)
        result = drive(root, "r", "page-unbroken", *selection)
        assert result.returncode == 2, (selection, result.stdout, result.stderr)
        assert not root.exists(), selection


def test_semi_mode_stops_at_a_named_hold(tmp_path):
    result = drive(
        tmp_path / "runs",
        "r",
        "page-review",
        "--from",
        "door",
        "--to",
        "recensor",
        "--mode",
        "semi",
    )

    assert result.returncode == EXIT_HELD
    assert "semi mode stopped at held recensor" in result.stdout


def _stopped_at_the_held_recensor(root: Path, result: subprocess.CompletedProcess) -> None:
    """Held before the Archetypus: the holds named, the Coniector run, nothing established."""
    assert result.returncode == EXIT_HELD, result.stdout + result.stderr
    assert HELD_RECENSOR_STOP in result.stdout
    assert "p2:1 (" in result.stdout and "--from recensor --to armarium" in result.stdout
    assert (root / "r" / "4b_coniector").is_dir()
    assert not (root / "r" / "6_archetypus").exists()
    assert not (root / "r" / "7_armarium").exists()


@pytest.mark.parametrize(
    "selection",
    [(), ("--all",), ("--from", "door", "--to", "armarium")],
    ids=["auto", "all", "semi-to-armarium"],
)
def test_a_held_recensor_stops_every_run_before_the_archetypus(tmp_path, selection):
    """Unattended or ranged to the export, a held Recensor stops the run before export."""
    root = tmp_path / "runs"
    _stopped_at_the_held_recensor(root, drive(root, "r", "page-review", *selection))


def test_a_manual_archetypus_over_a_held_recensor_stops_too(tmp_path):
    root = tmp_path / "runs"
    drive(root, "r", "page-review", "--from", "door", "--to", "recensor")
    result = drive(root, "r", "page-review", "--stage", "archetypus")
    assert result.returncode == EXIT_HELD, result.stdout + result.stderr
    assert HELD_RECENSOR_STOP in result.stdout
    assert not (root / "r" / "6_archetypus").exists()


def test_the_big_models_range_stops_at_a_held_recensor_then_resumes_after_an_advance(tmp_path):
    """`pod_run --models big` runs perlector..armarium after the witnesses' own range.

    It stops at the held Recensor with the Coniector done, so nothing left needs
    a GPU; after a person advances the Recensor's seal, a resume from the
    Recensor exports and names every hold.
    """
    root = tmp_path / "runs"
    first = drive(root, "r", "page-review", "--from", "door", "--to", "attestatores")
    assert first.returncode == 0, first.stdout + first.stderr

    held = drive(root, "r", "page-review", "--from", "perlector", "--to", "armarium")
    _stopped_at_the_held_recensor(root, held)

    advance_held_recensor(root, "r")
    result = drive(root, "r", "page-review", "--from", "recensor", "--to", "armarium")
    assert result.returncode == EXIT_HELD, result.stdout + result.stderr
    assert "an advance record passes its current seal" in result.stdout
    assert "run r: partial" in result.stdout
    assert "act p2:1 is held-for-review" in result.stdout


def test_an_advance_bound_to_an_earlier_recensor_seal_passes_nothing(tmp_path):
    """A Recensor pass that re-seals (new decisions, say) leaves an earlier advance stale."""
    from common.contracts.approval import build_approval_record
    from common.runtree.store import RunTree

    root = tmp_path / "runs"
    drive(root, "r", "page-review")
    # An advance of the Recensor that binds a digest its current seal does not have.
    RunTree(root, "r").write_approval_record(
        build_approval_record(
            ["stage-boundary:recensor"],
            "advance",
            "advanced before the last pass",
            "0" * 64,
            "2026-10-01T12:00:00Z",
        )
    )
    result = drive(root, "r", "page-review", "--from", "recensor", "--to", "armarium")
    assert result.returncode == EXIT_HELD, result.stdout + result.stderr
    assert HELD_RECENSOR_STOP in result.stdout


def test_a_held_armarium_reports_its_terminal_reasons_under_every_mode(tmp_path):
    """Armarium holds must retain the terminal report's named partial reasons.

    Each run first stops at its held Recensor; a person's advance of that seal
    lets each mode reach the Armarium.
    """
    automatic = tmp_path / "automatic"
    manual = tmp_path / "manual"
    semi = tmp_path / "semi"

    drive(automatic, "r", "page-review", "--all")
    advance_held_recensor(automatic, "r")
    all_result = drive(automatic, "r", "page-review", "--all")
    assert all_result.returncode == EXIT_HELD
    assert "run r: partial" in all_result.stdout
    assert "act p2:1 is held-for-review" in all_result.stdout

    for stage in SEQUENCE[: SEQUENCE.index("recensor") + 1]:
        drive(manual, "r", "page-review", "--stage", stage)
    advance_held_recensor(manual, "r")
    for stage in SEQUENCE[SEQUENCE.index("recensor") + 1 : -1]:
        drive(manual, "r", "page-review", "--stage", stage)
    manual_result = drive(manual, "r", "page-review", "--stage", "armarium")
    assert manual_result.returncode == EXIT_HELD
    assert "run r: partial" in manual_result.stdout
    assert "act p2:1 is held-for-review" in manual_result.stdout

    drive(semi, "r", "page-review", "--from", "door", "--to", "recensor")
    advance_held_recensor(semi, "r")
    semi_result = drive(semi, "r", "page-review", "--from", "archetypus", "--to", "armarium")
    assert semi_result.returncode == EXIT_HELD
    assert "run r: partial" in semi_result.stdout
    assert "act p2:1 is held-for-review" in semi_result.stdout


def test_a_damaged_armarium_decode_environment_stops_the_run_at_its_producer(automatic, tmp_path):
    """A deleted terminal ``decode-environment`` refuses, and nothing is exported.

    The seal binds that record's bytes as ``decode_environment_sha256`` and reads
    it while sealing, so the producer refuses first rather than the orchestrator.
    ``common/test_stage_seal.py`` separately drives ``verify_final_seal`` against
    exactly this damage, on the layer that still owns the check.
    """
    root = automatic
    record = next((root / "r" / "7_armarium" / "artifacts" / "decode-environment").iterdir())
    kept = record.read_bytes()
    record.unlink()

    refused = drive(root, "r", "page-unbroken", "--all")

    assert refused.returncode == 2, refused.stdout + refused.stderr
    assert "armarium cannot seal its boundary" in refused.stderr
    assert "decode-environment" in refused.stderr and "is unreadable" in refused.stderr
    assert "run r: complete" not in refused.stdout
    record.write_bytes(kept)
    assert drive(root, "r", "page-unbroken", "--all").returncode == 0


def test_an_attestatores_prework_hold_leaves_a_boundary_the_next_stage_refuses(tmp_path):
    """A pre-write Attestatores hold leaves no boundary later stages may cross.

    Otherwise ``--from`` could advance past a hold that ``--all`` stops for.
    """
    root = tmp_path / "runs"
    assert (
        drive(root, "r", "page-unbroken", "--from", "door", "--to", "attestatores").returncode == 0
    )
    testimonium = next((root / "r" / "3_attestatores" / "artifacts" / "page-testimonium").iterdir())
    testimonium.unlink()

    held = drive(root, "r", "page-unbroken", "--stage", "attestatores")
    assert held.returncode == EXIT_HELD, held.stdout + held.stderr
    assert "attempt tally UNKNOWN" in held.stderr

    advanced = drive(root, "r", "page-unbroken", "--from", "perlector", "--to", "armarium")

    assert advanced.returncode == 2, advanced.stdout + advanced.stderr
    assert "perlector refuses attestatores stage-seal" in advanced.stderr
    assert not (root / "r" / "4_perlector").exists()


def test_every_mode_checkpoints_a_held_member_before_it_stops(monkeypatch, tmp_path):
    """A run that is both held and over the cap has two stop reasons; the cap wins.

    The two-act fixture cannot organically cross the cap mid-sequence, so this
    test must inject the otherwise unreachable boundary state.
    """
    orchestrator = load_stage("orchestrator")
    args = argparse.Namespace(run_root=str(tmp_path), run_id="r")
    breach = {
        "threshold": 2,
        "count": 3,
        "breached": True,
        "by_kind": {},
        "checkpoint": "designator",
    }

    checkpointed: list[str] = []

    def hold(_program, _args, **_extra):
        return orchestrator.EXIT_HELD

    def record(_args, name, _policy):
        checkpointed.append(name)
        return None

    monkeypatch.setattr(orchestrator, "invoke", hold)
    cases = (
        ("semi", ("designator", "attestatores"), "designator"),
        ("manual", ("designator",), "designator"),
        ("auto", ("attestatores",), "attestatores"),
        ("semi", ("attestatores", "perlector"), "attestatores"),
        ("manual", ("attestatores",), "attestatores"),
    )
    for mode, names, stopped_at in cases:
        checkpointed.clear()
        monkeypatch.setattr(orchestrator, "checkpoint", record)
        assert orchestrator.run_sequence(args, names, mode, {}) == EXIT_HELD
        assert checkpointed == [stopped_at], f"{mode} skipped its held member's checkpoint"

        monkeypatch.setattr(orchestrator, "checkpoint", lambda _args, _name, _policy: breach)
        assert orchestrator.run_sequence(args, names, mode, {}) == orchestrator.EXIT_RUN_HALTED
