"""Shared closed shape for run-sealed serving configuration inputs."""

from typing import Final

SERVING_CONFIG_INPUTS_SCHEMA: Final = "serving-config-inputs.v1"
SERVING_CONFIG_INPUTS_FIELDS: Final = frozenset(
    {"schema", "serving_recipes_sha256", "pod_placement_sha256"}
)
