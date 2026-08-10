"""Prompt fidelity (invariant #49): byte-exact per-chair prompts, and a fail
closed refusal for any recipe with no registered builder.
"""

import prompts
import pytest


def _dossier():
    return {
        "act_key": "a1",
        "witness_regime": "named",
        "testimonia": [
            {
                "witness_label": "attestator_1",
                "model_name": "fixture/attestator-1",
                "resolved_provenance": {"resolved_revision": "fixture-v1"},
                "training_domain": "a synthetic fixture witness",
                "outcome": "read",
                "reported": "alpha beta gamma",
            }
        ],
    }


def test_the_fixture_recipe_reproduces_its_declared_template_byte_for_byte():
    built = prompts.build_prompt("fake-perlector-v0", "perlector", _dossier())
    assert built == (
        "role: perlector\n"
        "act: a1\n"
        "witness_regime: named\n"
        "testimonia:\n"
        "  - attestator_1 (a synthetic fixture witness): 'alpha beta gamma'; "
        'model=\'fixture/attestator-1\'; provenance={"resolved_revision":"fixture-v1"}'
    )


def test_the_same_dossier_and_recipe_always_produce_the_same_bytes():
    first = prompts.build_prompt("fake-perlector-v0", "perlector", _dossier())
    second = prompts.build_prompt("fake-perlector-v0", "perlector", _dossier())
    assert first == second


def test_an_unregistered_recipe_refuses_rather_than_falling_back_to_a_default():
    """The load-bearing behaviour: a fine-tuned candidate served under a recipe
    with no declared builder must never silently render through some other
    chair's template."""
    with pytest.raises(ValueError, match="no declared prompt builder"):
        prompts.build_prompt("some-unregistered-recipe-v7", "perlector", _dossier())


def test_the_refusal_is_not_vacuous_it_actually_distinguishes_recipes():
    """Prove the guard can go red: a recipe that *is* registered must not
    raise, so the refusal above is really about the missing entry."""
    prompts.build_prompt("fake-perlector-v0", "perlector", _dossier())  # does not raise
    with pytest.raises(ValueError):
        prompts.build_prompt("fake-perlector-v1", "perlector", _dossier())


def test_prompt_evidence_binds_the_builders_own_bytes_not_only_its_name(monkeypatch):
    """D-7: the recipe name pins *which* builder ran only by convention -- a
    future edit to `_fake_perlector_v0` must change what the record claims
    about itself, or an old record's `rendered_sha256` would be unreproducible
    with nothing to say why."""
    from common.chairs.models import ChairIdentity

    chair = ChairIdentity(
        role="perlector",
        source="local-repository",
        repo=None,
        path="perlector",
        revision=None,
        digest_manifest="0" * 64,
        manifest="manifests/perlector.json",
        adapter_of=None,
        serving_recipe="fake-perlector-v0",
        license_note="fixture identity only; no model weights or model license apply",
    )
    dossier = _dossier() | {"dossier_digest": "d" * 64}
    before = prompts.prompt_evidence(chair, dossier)
    assert "builder_sha256" in before

    def _mutated_builder(chair_role, dossier):
        return prompts._fake_perlector_v0(chair_role, dossier) + "\n"

    monkeypatch.setitem(prompts._BUILDERS, "fake-perlector-v0", _mutated_builder)
    after = prompts.prompt_evidence(chair, dossier)
    assert after["builder_sha256"] != before["builder_sha256"], (
        "an edited builder must change its own digest in the record"
    )
    assert after["rendered_sha256"] != before["rendered_sha256"]
