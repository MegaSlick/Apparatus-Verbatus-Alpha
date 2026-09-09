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
from dataclasses import replace
from pathlib import Path

import pytest

from common.chairs import ChairRegistry
from common.chairs.models import AbsentChair, ChairIdentity
from common.contracts.canonical import digest_bytes
from common.contracts.errors import ContractError
from common.stage import run_config_bindings, validate_witness_context_bindings
from common.witness_context import validate_witness_context_configuration

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


def _write_context(path: Path, sentences: dict[str, str]) -> None:
    path.write_text(
        "\n".join(
            f'[{role}]\ntraining_domain = "{sentence}"\n' for role, sentence in sentences.items()
        ),
        encoding="utf-8",
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
    with pytest.raises(ContractError, match="shipped-fixture identity projection"):
        _bindings(REAL_ROSTER, FIXTURE_CONTEXT)


def test_a_semantically_copied_fixture_declaration_is_refused_for_the_real_roster(tmp_path):
    """Comments, whitespace, and location cannot disguise a shipped profile."""
    copied = tmp_path / "renamed-context.toml"
    copied.write_text(
        "# operator copy with byte-only edits\n\n"
        + FIXTURE_CONTEXT.read_text(encoding="utf-8").replace("[attestator_2]", "\n[attestator_2]"),
        encoding="utf-8",
    )

    with pytest.raises(ContractError, match="shipped-fixture identity projection"):
        _bindings(REAL_ROSTER, copied)


def test_known_fixture_sentences_remain_bound_inside_a_partly_custom_declaration(tmp_path):
    copied = tmp_path / "partly-custom-context.toml"
    _write_context(
        copied,
        {
            "attestator_1": "an operator-authored description for the first witness",
            "attestator_2": _FIXTURE_SENTENCE,
            "attestator_3": _FIXTURE_SENTENCE,
        },
    )

    with pytest.raises(
        ContractError,
        match="attestator_2.*shipped-fixture identity projection.*attestator_3",
    ):
        _bindings(REAL_ROSTER, copied)


def test_edge_whitespace_cannot_disguise_a_known_fixture_sentence(tmp_path):
    copied = tmp_path / "edge-whitespace-context.toml"
    _write_context(
        copied,
        {
            "attestator_1": "an operator-authored first witness",
            "attestator_2": f"  {_FIXTURE_SENTENCE}  ",
            "attestator_3": "an operator-authored third witness",
        },
    )
    assert digest_bytes(copied.read_bytes()) != digest_bytes(FIXTURE_CONTEXT.read_bytes())

    with pytest.raises(ContractError, match="attestator_2.*shipped-fixture identity projection"):
        _bindings(REAL_ROSTER, copied)


def test_the_refusal_names_the_chairs_and_the_declaration_it_refused():
    with pytest.raises(ContractError) as refusal:
        _bindings(REAL_ROSTER, FIXTURE_CONTEXT)

    message = str(refusal.value)
    for chair in ChairRegistry.from_toml(REAL_ROSTER).config.witness_chairs:
        assert chair in message
    assert "shipped-fixture identity projection" in message
    assert "operator-authored declaration" in message


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


def test_whitespace_edited_copies_of_the_shipped_fixture_pair_keep_their_identity(tmp_path):
    config_root = tmp_path / "config"
    shutil.copytree(ROOT / "config", config_root)
    roster = config_root / "moved-models.toml"
    context = config_root / "moved-context.toml"
    roster.write_text(
        "# moved without changing an identity\n" + FIXTURE_ROSTER.read_text(encoding="utf-8"),
        encoding="utf-8",
    )
    context.write_text(
        FIXTURE_CONTEXT.read_text(encoding="utf-8") + "\n# byte-only declaration edit\n",
        encoding="utf-8",
    )

    assert _bindings(roster, context)["config_digest"]


def test_whitespace_edited_copies_of_the_shipped_real_pair_keep_their_identity(tmp_path):
    config_root = tmp_path / "config"
    shutil.copytree(ROOT / "config", config_root)
    roster = config_root / "moved-models-real.toml"
    context = config_root / "moved-context-real.toml"
    roster.write_text(
        "# moved without changing an identity\n" + REAL_ROSTER.read_text(encoding="utf-8"),
        encoding="utf-8",
    )
    context.write_text(
        REAL_CONTEXT.read_text(encoding="utf-8") + "\n# byte-only declaration edit\n",
        encoding="utf-8",
    )

    assert _bindings(roster, context)["config_digest"]


def test_the_real_profile_is_bound_to_each_roles_exact_identity_projection():
    roster = ChairRegistry.from_toml(REAL_ROSTER).config
    first = roster.chairs["attestator_1"]
    second = roster.chairs["attestator_2"]
    assert isinstance(first, ChairIdentity) and isinstance(second, ChairIdentity)
    changed = replace(
        roster,
        chairs={
            **roster.chairs,
            "attestator_1": replace(
                first,
                repo=second.repo,
                revision=second.revision,
                digest_manifest=second.digest_manifest,
            ),
        },
    )

    with pytest.raises(ContractError, match="attestator_1.*shipped-real identity projection"):
        validate_witness_context_bindings(
            changed,
            witness_context="named",
            witness_context_config_path=REAL_CONTEXT,
            nuda_per_mille=0,
            nuda_approval_ref="",
            perlector_instrument_per_mille=0,
            perlector_instrument_approval_ref="",
        )


def test_a_recognized_profile_keeps_coverage_but_skips_an_explicit_absence():
    roster = ChairRegistry.from_toml(REAL_ROSTER).config
    changed = replace(
        roster,
        chairs={
            **roster.chairs,
            "attestator_3": AbsentChair(
                role="attestator_3", reason="unavailable for this degraded run"
            ),
        },
    )

    assert validate_witness_context_bindings(
        changed,
        witness_context="named",
        witness_context_config_path=REAL_CONTEXT,
        nuda_per_mille=0,
        nuda_approval_ref="",
        perlector_instrument_per_mille=0,
        perlector_instrument_approval_ref="",
    ) == digest_bytes(REAL_CONTEXT.read_bytes())


def test_mixed_custom_known_and_absent_roles_preserve_the_narrow_contract(tmp_path):
    roster = ChairRegistry.from_toml(FIXTURE_ROSTER).config
    changed = replace(
        roster,
        chairs={
            **roster.chairs,
            "attestator_3": AbsentChair(
                role="attestator_3", reason="unavailable for this degraded run"
            ),
        },
    )
    mixed = tmp_path / "mixed-context.toml"
    _write_context(
        mixed,
        {
            "attestator_1": "an operator-authored description for a custom fixture witness",
            "attestator_2": _FIXTURE_SENTENCE,
            "attestator_3": _FIXTURE_SENTENCE,
        },
    )

    validation = validate_witness_context_configuration(
        changed,
        mixed,
        shipped_config_root=ROOT / "config",
    )

    assert validation.source_sha256 == digest_bytes(mixed.read_bytes())
    assert validation.profile == "mixed"
    assert dict(validation.role_profiles) == {
        "attestator_1": "operator-authored",
        "attestator_2": "shipped-fixture",
        "attestator_3": "shipped-fixture",
    }
    assert validation.verified_present_roles == ("attestator_2",)


def test_local_repository_is_a_location_not_proof_of_the_fixture_identity():
    roster = ChairRegistry.from_toml(FIXTURE_ROSTER).config
    first = roster.chairs["attestator_1"]
    assert isinstance(first, ChairIdentity)
    changed = replace(
        roster,
        chairs={
            **roster.chairs,
            "attestator_1": replace(first, path="a-real-local-mirror"),
        },
    )

    with pytest.raises(ContractError, match="local repository location alone.*operator-authored"):
        validate_witness_context_bindings(
            changed,
            witness_context="named",
            witness_context_config_path=FIXTURE_CONTEXT,
            nuda_per_mille=0,
            nuda_approval_ref="",
            perlector_instrument_per_mille=0,
            perlector_instrument_approval_ref="",
        )


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


def test_a_custom_declaration_remains_operator_authored_and_changes_the_config_digest(tmp_path):
    """Which declaration a run read is inside `config_digest`, not beside it."""
    fixture_digest = _bindings(FIXTURE_ROSTER, FIXTURE_CONTEXT)["config_digest"]
    custom = tmp_path / "custom-context.toml"
    custom.write_text(
        FIXTURE_CONTEXT.read_text(encoding="utf-8").replace(
            _FIXTURE_SENTENCE,
            "an operator-authored description of a custom local witness",
        ),
        encoding="utf-8",
    )
    custom_declaration = _bindings(FIXTURE_ROSTER, custom)["config_digest"]

    assert fixture_digest != custom_declaration


def test_the_refusal_holds_under_the_blinded_regime_too():
    """Blinded withholds the sentence from the dossier, not from the record.

    The run still seals a declaration saying its real chairs are synthetic, and
    that record outlives the regime it was sealed under (GOVERNANCE 6).
    """
    with pytest.raises(ContractError, match="shipped-fixture identity projection"):
        validate_witness_context_bindings(
            ChairRegistry.from_toml(REAL_ROSTER).config,
            witness_context="blinded",
            witness_context_config_path=FIXTURE_CONTEXT,
            nuda_per_mille=0,
            nuda_approval_ref="",
            perlector_instrument_per_mille=0,
            perlector_instrument_approval_ref="",
        )


def test_a_roster_without_witness_roles_records_that_absence(tmp_path):
    roster = ChairRegistry.from_toml(FIXTURE_ROSTER).config
    without_witnesses = replace(
        roster,
        witness_floor=0,
        chairs={"perlector": roster.chairs["perlector"]},
        witness_framings={},
    )
    declaration = tmp_path / "empty-context.toml"
    declaration.write_bytes(b"")

    validation = validate_witness_context_configuration(
        without_witnesses, declaration, shipped_config_root=ROOT / "config"
    )

    assert validation.profile == "no-witness-roles"
    assert validation.declared_roles == ()
    assert validation.verified_present_roles == ()
    assert validation.role_profiles == ()
    assert validation.source_sha256 == digest_bytes(b"")
