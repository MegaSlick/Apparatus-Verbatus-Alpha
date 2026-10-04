"""`pagekit-producer-recipe.v1`: the producer recipe that declares pagekit's triage rows.

The Door requires a producer recipe beside any manifest with producer rows and binds
its digest into the run. The duplicate-detection instrument has its own
(`instrument.producer_recipe`); rows `verbatus prepare` writes from pagekit's
decisions are declared by this one instead: which pagekit revision decided them, the
digest of the settings it ran with, the detector method behind each step, and the
mapping and triage operation order that turned its geometry into rows. The Door holds
every producer row to the recipe it was given.

This module reads nothing from pagekit, so the Door can check a recipe without it.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any, Final

from common.contracts.canonical import is_sha256
from common.contracts.errors import SchemaRefusal
from common.contracts.triage import SPLIT_OPERATION_ORDER_V2

RECIPE_SCHEMA: Final = "pagekit-producer-recipe.v1"
PRODUCER_IDENTITY: Final = "pagekit"
GEOMETRY_MAPPING: Final = "operations.triage.pagekit_geometry-v1"
_FIELDS: Final = {
    "schema",
    "producer",
    "geometry_mapping",
    "operation_order",
    "pagekit_settings_sha256",
    "detector_methods",
}


def make_recipe(
    *, revision: str, settings_sha256: str, detector_methods: Mapping[str, Iterable[str]]
) -> dict[str, Any]:
    """The recipe for one `verbatus prepare` run, validated."""
    return validate_recipe(
        {
            "schema": RECIPE_SCHEMA,
            "producer": {"identity": PRODUCER_IDENTITY, "revision": revision},
            "geometry_mapping": GEOMETRY_MAPPING,
            "operation_order": SPLIT_OPERATION_ORDER_V2,
            "pagekit_settings_sha256": settings_sha256,
            "detector_methods": {
                step: sorted(set(methods)) for step, methods in sorted(detector_methods.items())
            },
        }
    )


def is_recipe(document: Any) -> bool:
    """Whether a producer recipe document claims to be this kind."""
    return isinstance(document, dict) and document.get("schema") == RECIPE_SCHEMA


def validate_recipe(record: Any) -> dict[str, Any]:
    if not isinstance(record, dict) or set(record) != _FIELDS:
        raise SchemaRefusal("pagekit producer recipe has the wrong closed schema")
    if record["schema"] != RECIPE_SCHEMA:
        raise SchemaRefusal("pagekit producer recipe has an unknown schema")
    producer = record["producer"]
    if (
        not isinstance(producer, dict)
        or set(producer) != {"identity", "revision"}
        or producer["identity"] != PRODUCER_IDENTITY
        or not isinstance(producer["revision"], str)
        or not producer["revision"].strip()
    ):
        raise SchemaRefusal("pagekit producer recipe must name pagekit and its revision")
    if record["geometry_mapping"] != GEOMETRY_MAPPING:
        raise SchemaRefusal("pagekit producer recipe names an unknown geometry mapping")
    if record["operation_order"] != SPLIT_OPERATION_ORDER_V2:
        raise SchemaRefusal(
            f"pagekit producer recipe must declare operation order {SPLIT_OPERATION_ORDER_V2}"
        )
    if not is_sha256(record["pagekit_settings_sha256"]):
        raise SchemaRefusal("pagekit producer recipe has no settings digest")
    methods = record["detector_methods"]
    if not isinstance(methods, dict) or not all(
        isinstance(step, str)
        and isinstance(names, list)
        and all(isinstance(name, str) and name.strip() for name in names)
        for step, names in methods.items()
    ):
        raise SchemaRefusal("pagekit producer recipe detector methods must be named per step")
    return record


def refuse_rows_outside_recipe(recipe: Mapping[str, Any], rows: Iterable[Mapping[str, Any]]):
    """Every producer row must be pagekit's, at the recipe's revision and order."""
    producer = recipe["producer"]
    for row in rows:
        actor = row["actor"]
        if actor["kind"] != "producer":
            continue
        if actor["identity"] != producer["identity"] or actor["revision"] != producer["revision"]:
            raise SchemaRefusal(
                f"a producer row is not pagekit {producer['revision']}'s, so the pagekit "
                "producer recipe does not declare it"
            )
        if row["split"]["operation_order"] != recipe["operation_order"]:
            raise SchemaRefusal(
                "a pagekit row's operation order is not the one its recipe declares"
            )
