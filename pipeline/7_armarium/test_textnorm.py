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
    assert all(search_fold(search_fold(value)) == search_fold(value) for value in examples)


def test_search_fold_never_empties_a_string_that_carries_a_letter_or_digit():
    """The second harvested property, and the one that protects the ledger.

    Spec 11 sends this unit's property tests through the window as its spec. A fold
    that emptied a real reading would give it a search key indistinguishable from a
    blank one, which is the collapse GOVERNANCE 2 refuses everywhere else.
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
    """The old pipeline substituted before decomposing and needed two passes here.

    U+01E3 is not in the substitution table; it decomposes to the bare ligature plus
    a combining macron. Substituting first left the bare ligature standing, so one
    fold produced a value a second fold would change again.
    """
    assert search_fold("ǣ") == "ae"
    assert search_fold(search_fold("ǣ")) == "ae"


def test_search_fold_refuses_absent_literal_instead_of_silently_emptying_it():
    with pytest.raises(SchemaRefusal, match="literal string"):
        search_fold(None)  # type: ignore[arg-type]


def test_search_fold_revision_is_explicit_for_derived_columns():
    assert TEXTNORM_REVISION == "armarium-textnorm-v1"
