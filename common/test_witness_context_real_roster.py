"""The real roster's factual witness context, and the pairing that enforces it.

`config/witness_context.toml` describes all three chairs as synthetic fixtures.
Under the `named` regime that sentence is handed to the Perlector as fact about
the witness whose testimony it is reading, so a real run sealed under it tells
the reader that Chandra-2, DAI-RecordGold and Churro-3B are fixtures --
GOVERNANCE 7's "feed it completely and honestly" failing on the first real call.
`config/witness_context-real.toml` is the declaration the real roster is read
under, selected on `--witness-context-config` exactly as the roster is selected
on `--models-config`.
"""

from __future__ import annotations

import shutil
import tomllib
from pathlib import Path

import pytest

from common.chairs import ChairRegistry
from common.contracts.canonical import digest_bytes
from common.contracts.errors import ContractError
from common.stage import run_config_bindings, validate_witness_context_bindings

ROOT = Path(__file__).resolve().parents[1]
FIXTURE_ROSTER = ROOT / "config" / "models.toml"
REAL_ROSTER = ROOT / "config" / "models-real.toml"
FIXTURE_CONTEXT = ROOT / "config" / "witness_context.toml"
REAL_CONTEXT = ROOT / "config" / "witness_context-real.toml"

_FIXTURE_SENTENCE = "a synthetic fixture witness; no real training domain applies"


def _bindings(models_config: Path, witness_context_config: Path) -> dict:
    return run_config_bindings(
        ChairRegistry.from_toml(models_config).config,
        {"fixture": "none"},
        "test",
        witness_context_config_path=witness_context_config,
    )


def test_the_real_declaration_covers_the_real_rosters_witness_chairs_and_no_others():
    """A chair with no entry is refused at run creation; a stray one is too.

    Asserted against the roster rather than a hard-coded list of three, so a
    roster change that adds or renames a witness fails here rather than at the
    Door of the first real run.
    """
    declared = tomllib.loads(REAL_CONTEXT.read_text(encoding="utf-8"))
    roster = ChairRegistry.from_toml(REAL_ROSTER).config

    assert set(declared) == set(roster.witness_chairs)
    for chair, entry in declared.items():
        assert set(entry) == {"training_domain"}, chair


def test_no_real_witness_is_described_as_a_synthetic_fixture():
    """The whole point of the file: not one sentence may be the fixture's."""
    declared = tomllib.loads(REAL_CONTEXT.read_text(encoding="utf-8"))

    for chair, entry in declared.items():
        domain = entry["training_domain"]
        assert _FIXTURE_SENTENCE not in domain, chair
        assert "synthetic fixture" not in domain, chair
        # Honest about what is not known rather than blank: every sentence here
        # either cites something this repository holds or says the domain is
        # unknown, and a bare placeholder would pass the emptiness check in
        # `validate_witness_context_bindings` while saying nothing at all.
        assert len(domain.split()) >= 12, chair


def test_a_roster_of_published_models_may_not_take_the_fixture_declaration():
    """The pairing refusal, at run creation, before any stage runs."""
    with pytest.raises(ContractError, match="resolve to a published model repository"):
        _bindings(REAL_ROSTER, FIXTURE_CONTEXT)


def test_the_refusal_names_the_chairs_and_the_declaration_it_refused():
    with pytest.raises(ContractError) as refusal:
        _bindings(REAL_ROSTER, FIXTURE_CONTEXT)

    message = str(refusal.value)
    for chair in ChairRegistry.from_toml(REAL_ROSTER).config.witness_chairs:
        assert chair in message
    assert str(FIXTURE_CONTEXT) in message
    assert "config/witness_context-real.toml" in message


def test_a_moved_copy_of_the_fixture_roster_is_not_treated_as_a_real_one(tmp_path):
    """The test is what the chairs ARE, not which path the roster sits at.

    `common/test_stage_real_ingress.py` moves one byte of a chair's record into
    a temporary copy of the fixture roster to prove the sealed-binding recheck
    names it. That copy's witnesses are still local fixture snapshots, so the
    fixture declaration is still true of them and this refusal must not fire.
    """
    config_root = tmp_path / "chair-config"
    shutil.copytree(ROOT / "config" / "model-fixtures", config_root / "model-fixtures")
    shutil.copytree(ROOT / "config" / "manifests", config_root / "manifests")
    moved = config_root / "models.toml"
    moved.write_text(
        FIXTURE_ROSTER.read_text(encoding="utf-8").replace(
            'license_note = "fixture identity only; no model weights or model license apply"',
            'license_note = "a moved chair record"',
            1,
        ),
        encoding="utf-8",
    )

    assert _bindings(moved, FIXTURE_CONTEXT)["config_digest"]


def test_a_real_run_seals_the_real_declarations_bytes():
    """The digest the run seals is this file's, measured, not the default's."""
    sealed = validate_witness_context_bindings(
        ChairRegistry.from_toml(REAL_ROSTER).config,
        witness_context="named",
        witness_context_config_path=REAL_CONTEXT,
        nuda_per_mille=0,
        nuda_approval_ref="",
        perlector_instrument_per_mille=0,
        perlector_instrument_approval_ref="",
    )

    assert sealed == digest_bytes(REAL_CONTEXT.read_bytes())
    assert sealed != digest_bytes(FIXTURE_CONTEXT.read_bytes())


def test_a_fixture_run_still_seals_the_fixture_declaration():
    """The shipped roster is unmoved; nothing here changes a fixture run."""
    sealed = validate_witness_context_bindings(
        ChairRegistry.from_toml(FIXTURE_ROSTER).config,
        witness_context="named",
        witness_context_config_path=FIXTURE_CONTEXT,
        nuda_per_mille=0,
        nuda_approval_ref="",
        perlector_instrument_per_mille=0,
        perlector_instrument_approval_ref="",
    )

    assert sealed == digest_bytes(FIXTURE_CONTEXT.read_bytes())


def test_the_two_declarations_produce_different_run_configuration_digests():
    """Which declaration a run read is inside `config_digest`, not beside it."""
    fixture_digest = _bindings(FIXTURE_ROSTER, FIXTURE_CONTEXT)["config_digest"]
    real_declaration = _bindings(FIXTURE_ROSTER, REAL_CONTEXT)["config_digest"]

    assert fixture_digest != real_declaration


def test_the_refusal_holds_under_the_blinded_regime_too():
    """Blinded withholds the sentence from the dossier, not from the record.

    The run still seals a declaration saying its real chairs are synthetic, and
    that record outlives the regime it was sealed under (GOVERNANCE 6).
    """
    with pytest.raises(ContractError, match="synthetic fixture"):
        validate_witness_context_bindings(
            ChairRegistry.from_toml(REAL_ROSTER).config,
            witness_context="blinded",
            witness_context_config_path=FIXTURE_CONTEXT,
            nuda_per_mille=0,
            nuda_approval_ref="",
            perlector_instrument_per_mille=0,
            perlector_instrument_approval_ref="",
        )
