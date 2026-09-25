"""Regression properties for the Armarium's derived search fold."""

from __future__ import annotations

import pytest
from textnorm import TEXTNORM_REVISION, search_fold

from common.contracts.errors import SchemaRefusal


@pytest.mark.parametrize(
    ("literal", "expected"),
    [
        ("sœur", "soeur"),
        ("Cæsar", "caesar"),
        ("Cǣsar", "caesar"),
        ("Ǣ", "ae"),
        ("ǣ", "ae"),
        ("Ǽ", "ae"),
        ("ǽ", "ae"),
        ("d’Amours", "damours"),
        ("Straße", "strasse"),
        ("ȣa8atchin8tin", "8a8atchin8tin"),
        ("  père, Québec! ", "pere quebec"),
    ],
)
def test_search_fold_keeps_the_harvested_search_behaviour(literal, expected):
    assert search_fold(literal) == expected


def test_search_fold_is_idempotent_for_the_regression_population():
    examples = [
        "",
        "Cǣsar",
        "ȣa8atchin8tin",
        "d'Amours",
        "œuvre — déjà vu",
        "Straße",
        "...",
        "Κόσμε",
    ]
    for value in examples:
        folded = search_fold(value)
        assert search_fold(folded) == folded, value


def test_search_fold_never_empties_a_string_that_carries_a_letter_or_digit():
    """A fold that emptied a real reading would give it a search key
    indistinguishable from a blank one, the collapse principle 2 refuses
    everywhere else.
    """
    examples = [
        "Cǣsar",
        "ȣ",
        "8",
        "d’Amours",
        "  — é —  ",
        "Κόσμε",
        "\u0301a",
    ]
    for value in examples:
        assert any(character.isalnum() for character in value), value
        assert search_fold(value) != ""


def test_search_fold_is_idempotent_for_the_accented_ligature_the_window_folded_twice():
    """A single fold must reach the fixed point in one pass, not two.

    U+01E3 is not in the substitution table; it decomposes to the bare ligature
    plus a combining macron. Substituting before decomposing would leave the
    bare ligature standing for a second fold to change again.
    """
    assert search_fold("ǣ") == "ae"
    assert search_fold(search_fold("ǣ")) == "ae"


def test_search_fold_refuses_absent_literal_instead_of_silently_emptying_it():
    with pytest.raises(SchemaRefusal, match="literal string"):
        search_fold(None)  # type: ignore[arg-type]


def test_search_fold_revision_is_explicit_for_derived_columns():
    assert TEXTNORM_REVISION == "armarium-textnorm-v1"
