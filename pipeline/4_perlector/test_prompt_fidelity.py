"""Prompt fidelity (invariant #49): byte-exact per-seat prompts, and a fail
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
        "  - attestator_1 (a synthetic fixture witness): 'alpha beta gamma'"
    )


def test_the_same_dossier_and_recipe_always_produce_the_same_bytes():
    first = prompts.build_prompt("fake-perlector-v0", "perlector", _dossier())
    second = prompts.build_prompt("fake-perlector-v0", "perlector", _dossier())
    assert first == second


def test_an_unregistered_recipe_refuses_rather_than_falling_back_to_a_default():
    """The load-bearing behaviour: a fine-tuned candidate served under a recipe
    with no declared builder must never silently render through some other
    seat's template."""
    with pytest.raises(ValueError, match="no declared prompt builder"):
        prompts.build_prompt("some-unregistered-recipe-v7", "perlector", _dossier())


def test_the_refusal_is_not_vacuous_it_actually_distinguishes_recipes():
    """Prove the guard can go red: a recipe that *is* registered must not
    raise, so the refusal above is really about the missing entry."""
    prompts.build_prompt("fake-perlector-v0", "perlector", _dossier())  # does not raise
    with pytest.raises(ValueError):
        prompts.build_prompt("fake-perlector-v1", "perlector", _dossier())
