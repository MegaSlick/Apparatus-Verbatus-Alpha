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
    chair_preflight_identity_digest,
    load_serving_recipes,
    parse_serving_recipes,
    profile_preflight_digest,
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
from .smoke import VisionSmokeCall

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
    "VisionSmokeCall",
    "StageContextReceiptPublisher",
    "assemble_serving_preflight_callback",
    "assemble_serving_smoke_reader",
    "chair_preflight_identity_digest",
    "load_serving_recipes",
    "parse_serving_recipes",
    "profile_preflight_digest",
    "verify_recipes_cover_chairs",
]
