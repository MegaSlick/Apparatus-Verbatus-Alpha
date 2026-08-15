"""Fake-first lifecycle management for one configured vLLM chair at a time."""

from .assembly import assemble_serving_preflight_callback, assemble_serving_smoke_reader
from .config import (
    CONFIG_INPUTS_SCHEMA,
    SCHEMA,
    FixtureProfile,
    ProbeSpec,
    ServingConfigInputs,
    ServingProfile,
    ServingRecipes,
    load_serving_recipes,
    parse_serving_recipes,
    verify_recipes_cover_chairs,
)
from .manager import (
    AdapterCalibration,
    ReceiptPublication,
    ServiceHandle,
    ServingManager,
    StageContextReceiptPublisher,
)
from .preflight import ServingSmokeReader

__all__ = [
    "SCHEMA",
    "CONFIG_INPUTS_SCHEMA",
    "AdapterCalibration",
    "FixtureProfile",
    "ProbeSpec",
    "ReceiptPublication",
    "ServiceHandle",
    "ServingManager",
    "ServingConfigInputs",
    "ServingProfile",
    "ServingRecipes",
    "ServingSmokeReader",
    "StageContextReceiptPublisher",
    "assemble_serving_preflight_callback",
    "assemble_serving_smoke_reader",
    "load_serving_recipes",
    "parse_serving_recipes",
    "verify_recipes_cover_chairs",
]
