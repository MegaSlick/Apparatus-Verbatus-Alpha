"""Which producer recipe declares which triage rows: the one check the Door makes, and
`verbatus upload` makes first, so a pod is never started on triage the Door refuses.

A manifest with producer rows reaches the Door with its producer's recipe: the
duplicate-detection instrument's (`instrument.producer_recipe`) or pagekit's
(`pagekit_recipe`). The recipe is validated by its own schema, and every producer row
must be the recipe's own.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any

from common.contracts.errors import ContractError
from operations.triage import pagekit_recipe
from operations.triage.instrument import validate_producer_recipe


def validate_recipe_document(document: Any) -> None:
    """Refuse a producer recipe neither producer would have written."""
    try:
        if pagekit_recipe.is_recipe(document):
            pagekit_recipe.validate_recipe(document)
        else:
            validate_producer_recipe(document)
    except ContractError as error:
        raise ContractError(f"the triage producer recipe is invalid: {error}") from error


def refuse_rows_outside_recipe(document: Any, rows: Iterable[Mapping[str, Any]]) -> None:
    """Each producer's rows are declared by that producer's own recipe."""
    rows = list(rows)
    try:
        if pagekit_recipe.is_recipe(document):
            pagekit_recipe.refuse_rows_outside_recipe(document, rows)
        elif any(
            row["actor"]["kind"] == "producer"
            and row["actor"]["identity"] == pagekit_recipe.PRODUCER_IDENTITY
            for row in rows
        ):
            raise ContractError(
                "pagekit's rows are declared by a pagekit producer recipe, and the one "
                "supplied is the duplicate-detection instrument's"
            )
    except ContractError as error:
        raise ContractError(
            f"the triage producer recipe does not declare the manifest's rows ({error}); no "
            "run was created; supply the recipe written beside this manifest"
        ) from error
