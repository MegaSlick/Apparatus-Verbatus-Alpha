"""Regression tests for terminal accounting guards that previously had no witness.

Each test supplies the smallest synthetic contradiction that reaches one real
stage entry point.  The contradiction is deliberately impossible through normal
publication: that is precisely why the terminal boundary must reject it rather
than relying on an earlier stage never to produce it.
"""

import importlib.util
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from common.armarium_formats import ArmariumFormats
from common.chairs.registry import ChairRegistry
from common.contracts.approval import synthetic_fixture_ingress_record
from common.contracts.errors import ApprovalRefusal, FatalAccounting
from common.contracts.outcomes import ArmariumCategory
from common.contracts.stages import DOOR, EXEMPLAR
from common.runtree.store import RunTree
from common.stage import EXIT_COMPLETE, EXIT_FATAL, StageContext, load_fixture, run_config_bindings

ROOT = Path(__file__).resolve().parents[2]
EXEMPLAR_CLI = ROOT / "pipeline" / "1_exemplar" / "run.py"


def _stage_module(name: str, path: Path):
    """Load one numeric-directory stage without treating it as a package."""
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _parser_stub():
    """The stage parser seam for a direct, synthetic entry-point test."""
    return SimpleNamespace(parse_args=lambda: SimpleNamespace())


class _RecordingContext:
    """Just enough sealed-context surface for terminal-only stage paths."""

    def __init__(self) -> None:
        self.published: list[dict] = []
        self.finished = False
        self.fixture = {"fixture_id": "synthetic-terminal-guard-v0"}
        self.scenario = "synthetic-terminal-guard"
        self.config_digest = "a" * 64
        self.run = {"source_manifest": []}
        self.registry = SimpleNamespace(config=object())
        self.witness_chairs: list[str] = []
        self.witness_floor = 0
        self.armarium_formats = ArmariumFormats(
            ("text-bundle", "acts-database", "jsonl", "review-items", "salvage-tier"),
            False,
        )
        self.tree = SimpleNamespace(
            read_bytes=lambda _path: b"synthetic bundle input",
            put_blob=lambda _stage, _data: (
                "b" * 64,
                SimpleNamespace(relative_path="synthetic/armarium-export.zip"),
            ),
        )

    def publish(self, **record) -> None:
        self.published.append(record)

    def finish(self) -> None:
        self.finished = True

    def artifact_ref(self, stage: str, kind: str, identity: str) -> dict[str, str]:
        return {
            "relative_path": f"synthetic/{stage}/{kind}/{identity}.json",
            "sha256": "a" * 64,
        }

    def input_ref(self, relative_path: str) -> dict[str, str]:
        return {"relative_path": relative_path, "sha256": "b" * 64}


def _all_refused_door_tree(root: Path) -> RunTree:
    """Publish one synthetically refused source without the Door's earlier gate.

    ``process_sources`` normally belongs to a Door run that calls
    ``require_some_admitted`` afterwards.  The Exemplar nevertheless has its own
    terminal guard: a resumed or tampered tree can contain only valid refusal
    artifacts, and the Exemplar must not seal that empty corpus.
    """
    fixture = load_fixture(str(ROOT / "proof"))
    registry = ChairRegistry.from_toml(str(ROOT / "config" / "models.toml"))
    bindings = run_config_bindings(registry.config, fixture, "happy")
    tree = RunTree.create(
        root,
        "all-refused",
        source_manifest=[
            {
                "ordinal": 1,
                "relative_path": "synthetic/unreadable.bin",
                "sha256": "a" * 64,
            }
        ],
        config_digest=bindings["config_digest"],
        adapter_recipes=bindings["adapter_recipes"],
        witness_chairs=bindings["witness_chairs"],
        ingress=synthetic_fixture_ingress_record(),
    )
    context = StageContext(
        tree=tree,
        run=tree.read_run(),
        fixture=fixture,
        scenario="happy",
        stage=DOOR,
        adapter_revision=bindings["adapter_recipes"][DOOR],
        args=None,
        registry=registry,
    )
    context.publish(
        kind="admission",
        subject_id="source-1",
        outcome="refused",
        payload={
            "ordinal": 1,
            "declared_path": "synthetic/unreadable.bin",
            "declared_sha256": "a" * 64,
            "reason": "corrupt: deliberately invalid synthetic bytes",
        },
    )
    context.finish()
    return tree


def test_exemplar_never_seals_a_corpus_with_only_refused_sources(tmp_path):
    """The Exemplar's own all-refused guard must stop a zero-sealed corpus."""
    tree = _all_refused_door_tree(tmp_path / "runs")

    result = subprocess.run(
        [
            sys.executable,
            str(EXEMPLAR_CLI),
            "--run-root",
            str(tmp_path / "runs"),
            "--run-id",
            "all-refused",
            "--fixture-root",
            str(ROOT / "proof"),
            "--scenario",
            "happy",
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
    )

    assert result.returncode == EXIT_FATAL
    assert "every admitted source failed to seal" in result.stderr
    assert "Traceback" not in result.stderr
    artifacts = tree.build_manifest(EXEMPLAR)["artifacts"]
    assert [entry["outcome"] for entry in artifacts if entry["kind"] == "page"] == ["refused"]
    assert not [entry for entry in artifacts if entry["kind"] == "seal"]


def test_archetypus_refuses_to_resurrect_a_designator_held_act(monkeypatch):
    """A synthetic accepted review cannot establish a seal-held act."""
    archetypus = _stage_module(
        "archetypus_terminal_guard_test", ROOT / "pipeline" / "6_archetypus" / "run.py"
    )
    context = _RecordingContext()
    held = {
        "act_id": "act_held",
        "act_key": "held",
        "page_id": "pg_held",
        "outcome": "held",
    }
    accepted_review = {"artifact_id": "art_accepted", "outcome": "accepted"}

    monkeypatch.setattr(archetypus, "stage_parser", lambda _description: _parser_stub())
    monkeypatch.setattr(archetypus, "open_context", lambda *_args, **_kwargs: context)
    monkeypatch.setattr(archetypus, "expected_acts", lambda _context: [held])
    monkeypatch.setattr(archetypus, "final_review", lambda _context, _act_id: accepted_review)
    # These seams make the counterfactual complete if the guard is removed: the
    # stage would publish an Archetypus and exit 0, not merely trip an unrelated
    # fake-context attribute error.
    monkeypatch.setattr(
        archetypus,
        "reviewed_reading",
        lambda _context, _review, _act_id: (
            {"outcome": "read", "payload": {"text": "synthetic established text"}},
            {"relative_path": "synthetic/perlectio.json", "sha256": "c" * 64},
        ),
    )
    monkeypatch.setattr(archetypus, "reading_basis_regions", lambda _record, _what: [])
    monkeypatch.setattr(archetypus, "validate_serving_provenance", lambda *_args, **_kwargs: None)

    with pytest.raises(FatalAccounting, match="may not resurrect a held act"):
        archetypus.main()
    assert context.published == []
    assert not context.finished


def test_armarium_refuses_when_a_terminal_proposal_seal_disagrees_with_export(monkeypatch):
    """A delivered category may not override a held Designator seal entry."""
    armarium = _stage_module(
        "armarium_terminal_guard_test", ROOT / "pipeline" / "7_armarium" / "run.py"
    )
    context = _RecordingContext()
    held = {
        "act_id": "act_held",
        "act_key": "held",
        "page_id": "pg_held",
        "outcome": "held",
    }
    accepted_review = {
        "artifact_id": "art_accepted",
        "outcome": "accepted",
        "payload": {"coverage": {"under_witnessed": False}},
    }

    monkeypatch.setattr(armarium, "stage_parser", lambda _description: _parser_stub())
    monkeypatch.setattr(armarium, "open_context", lambda *_args, **_kwargs: context)
    monkeypatch.setattr(armarium, "page_census", lambda _context: {1: {"outcome": "sealed"}})
    monkeypatch.setattr(armarium, "pages_marked_out", lambda _context, _cache: {"act_held": [1]})
    monkeypatch.setattr(armarium, "expected_acts", lambda _context: [held])
    monkeypatch.setattr(
        armarium,
        "categorize",
        lambda _context, _act_id, _cache: (ArmariumCategory.DELIVERED, accepted_review, None),
    )
    monkeypatch.setattr(
        armarium,
        "run_aggregate",
        lambda *_args, **_kwargs: {"status": "complete", "reasons": []},
    )
    monkeypatch.setattr(armarium, "unaddressed_chairs", lambda _config: ())

    with pytest.raises(FatalAccounting, match="seal and the export may not disagree"):
        armarium.main()
    assert context.published == []
    assert not context.finished


def test_the_synthetic_terminal_guard_context_can_complete_when_no_contradiction_exists(
    monkeypatch,
):
    """Control: the Armarium test's fake context is not a permanently failing stub."""
    armarium = _stage_module(
        "armarium_terminal_guard_control_test", ROOT / "pipeline" / "7_armarium" / "run.py"
    )
    context = _RecordingContext()
    proposed = {
        "act_id": "act_proposed",
        "act_key": "proposed",
        "page_id": "pg_proposed",
        "outcome": "proposed",
    }
    accepted_review = {
        "artifact_id": "art_accepted",
        "outcome": "accepted",
        "payload": {"coverage": {"under_witnessed": False}},
    }

    monkeypatch.setattr(armarium, "stage_parser", lambda _description: _parser_stub())
    monkeypatch.setattr(armarium, "open_context", lambda *_args, **_kwargs: context)
    monkeypatch.setattr(armarium, "page_census", lambda _context: {1: {"outcome": "sealed"}})
    monkeypatch.setattr(
        armarium, "pages_marked_out", lambda _context, _cache: {"act_proposed": [1]}
    )
    monkeypatch.setattr(armarium, "expected_acts", lambda _context: [proposed])
    monkeypatch.setattr(
        armarium,
        "categorize",
        lambda _context, _act_id, _cache: (ArmariumCategory.HELD_FOR_REVIEW, accepted_review, None),
    )
    monkeypatch.setattr(
        armarium,
        "run_aggregate",
        lambda *_args, **_kwargs: {"status": "complete", "reasons": []},
    )
    monkeypatch.setattr(armarium, "unaddressed_chairs", lambda _config: ())
    monkeypatch.setattr(
        armarium,
        "build_armarium_bundle",
        # Coherent with the `run_aggregate` stub above: the real code folds the
        # aggregate's reasons into the ledger, so a `run_aggregate` of "complete"
        # beside a manifest `claims.status` of "partial" is a combination the real
        # code makes impossible. This control test only checks that the fake
        # context can complete at all, so nothing was ever proved by the mismatch
        # -- but an incoherent stub reads as a real case to a later reader.
        lambda *_args: SimpleNamespace(
            data=b"synthetic bundle",
            manifest={"self_hash": "c" * 64, "claims": {"status": "complete"}},
        ),
    )

    assert armarium.main() == EXIT_COMPLETE
    assert context.finished
    assert [record["kind"] for record in context.published] == ["manifest-entry", "export"]


def test_a_delivered_act_with_no_established_record_stops_the_export(monkeypatch):
    """The state the control test above used to stand in, now refused rather than run.

    Before the product bundle existed this fake could report `delivered` with no
    Archetypus record and complete, because nothing downstream needed the text. The
    projection does, and there is no reading to substitute for it -- so the category
    and the evidence disagreeing is a fatal imbalance and not a row with an empty
    text field.
    """
    armarium = _stage_module(
        "armarium_delivered_without_record_test", ROOT / "pipeline" / "7_armarium" / "run.py"
    )
    context = _RecordingContext()
    proposed = {
        "act_id": "act_proposed",
        "act_key": "proposed",
        "page_id": "pg_proposed",
        "outcome": "proposed",
    }
    accepted_review = {
        "artifact_id": "art_accepted",
        "outcome": "accepted",
        "payload": {"coverage": {"under_witnessed": False}},
    }

    monkeypatch.setattr(armarium, "stage_parser", lambda _description: _parser_stub())
    monkeypatch.setattr(armarium, "open_context", lambda *_args, **_kwargs: context)
    monkeypatch.setattr(armarium, "page_census", lambda _context: {1: {"outcome": "sealed"}})
    monkeypatch.setattr(
        armarium, "pages_marked_out", lambda _context, _cache: {"act_proposed": [1]}
    )
    monkeypatch.setattr(armarium, "expected_acts", lambda _context: [proposed])
    monkeypatch.setattr(
        armarium,
        "categorize",
        lambda _context, _act_id, _cache: (ArmariumCategory.DELIVERED, accepted_review, None),
    )
    monkeypatch.setattr(armarium, "unaddressed_chairs", lambda _config: ())

    with pytest.raises(FatalAccounting, match="no literal Archetypus text"):
        armarium.main()
    assert not context.finished


def test_armarium_exclusion_cannot_export_without_its_approval_artifact_reference():
    armarium = _stage_module(
        "armarium_exclusion_approval_test", ROOT / "pipeline" / "7_armarium" / "run.py"
    )
    with pytest.raises(ApprovalRefusal, match="approval-record reference"):
        armarium.exclusion_approval_ref({}, ArmariumCategory.EXCLUDED_WITH_APPROVAL)
    assert (
        armarium.exclusion_approval_ref(
            {"approval_ref": "art_0123456789abcdef"},
            ArmariumCategory.EXCLUDED_WITH_APPROVAL,
        )
        == "art_0123456789abcdef"
    )
