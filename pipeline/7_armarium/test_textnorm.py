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


def test_search_fold_refuses_absent_literal_instead_of_silently_emptying_it():
    with pytest.raises(SchemaRefusal, match="literal string"):
        search_fold(None)  # type: ignore[arg-type]


def test_search_fold_revision_is_explicit_for_derived_columns():
    assert TEXTNORM_REVISION == "armarium-textnorm-v1"
