"""The producer recipe that declares pagekit's triage rows."""

from __future__ import annotations

import pytest

from common.contracts.errors import ContractError
from operations.triage.pagekit_recipe import (
    RECIPE_SCHEMA,
    make_recipe,
    refuse_rows_outside_recipe,
    validate_recipe,
)


def _recipe(**changes):
    recipe = make_recipe(
        revision="0.1.0",
        settings_sha256="a" * 64,
        detector_methods={"skew": ["pagekit.neutral-default.skew/1"]},
    )
    recipe.update(changes)
    return recipe


def _row(identity="pagekit", revision="0.1.0", order="region-crop-rotate-crop"):
    return {
        "actor": {"kind": "producer", "identity": identity, "revision": revision},
        "split": {"operation_order": order},
    }


def test_a_recipe_names_pagekit_its_revision_and_the_mapping():
    recipe = validate_recipe(_recipe())
    assert recipe["schema"] == RECIPE_SCHEMA == "pagekit-producer-recipe.v1"
    assert recipe["producer"] == {"identity": "pagekit", "revision": "0.1.0"}
    assert recipe["operation_order"] == "region-crop-rotate-crop"


@pytest.mark.parametrize(
    "change",
    [
        {"schema": "triage-producer-recipe.v2"},
        {"producer": {"identity": "someone-else", "revision": "0.1.0"}},
        {"producer": {"identity": "pagekit", "revision": " "}},
        {"geometry_mapping": "a guess"},
        {"operation_order": "region-crop-rotate-crop-and-more"},
        {"pagekit_settings_sha256": "not a digest"},
        {"detector_methods": {"skew": [""]}},
        {"extra": True},
    ],
)
def test_a_malformed_recipe_is_refused(change):
    with pytest.raises(ContractError):
        validate_recipe(_recipe(**change))


def test_rows_must_be_the_recipes_own():
    recipe = validate_recipe(_recipe())
    refuse_rows_outside_recipe(recipe, [_row(), _row()])
    for row in (_row(identity="operations.triage.producer"), _row(revision="0.2.0")):
        with pytest.raises(ContractError, match="pagekit"):
            refuse_rows_outside_recipe(recipe, [_row(), row])
    with pytest.raises(ContractError, match="operation order"):
        refuse_rows_outside_recipe(recipe, [_row(order="region-crop-rotate")])
    # A row a person or a model made needs no producer recipe, so it may sit beside them.
    human = {
        "actor": {"kind": "human", "identity": "lead", "revision": None},
        "split": {"operation_order": "region-crop-rotate"},
    }
    refuse_rows_outside_recipe(recipe, [_row(), human])
