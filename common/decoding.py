"""The run-sealed decoding posture for every model reading."""

from __future__ import annotations

import math
from pathlib import Path
from typing import Any, Mapping

from common.chandra_native_retry import validate_policy_record
from common.contracts.errors import ContractError
from common.sealed_config import read_sealed_toml

DEFAULT_DECODING_CONFIG_PATH = Path(__file__).resolve().parents[1] / "config" / "decoding.toml"
_PERLECTOR_BOUNDS = ("reading_max_tokens", "reproof_max_tokens")
_LOAD_RECOVERY = (
    " No run or stage artifact was written. Restore or correct the decoding file and retry"
)


def load_decoding_policy(
    path: str | Path = DEFAULT_DECODING_CONFIG_PATH,
) -> tuple[dict[str, Any], str]:
    """Read the closed policy and its seal."""
    try:
        policy, digest = read_sealed_toml(path, "decoding configuration")
        _validate_decoding_policy(policy)
    except ContractError as error:
        raise ContractError(f"{error}.{_LOAD_RECOVERY}") from error
    return policy, digest


def _validate_decoding_policy(policy: Any) -> None:
    """Close every section before its values can mint provenance identities.

    The exact, closed Chandra native inference recipe is required for Attestator 1.
    `reading_of_record` is pinned to temperature 0, the posture every
    Attestator and the Perlector read under. `structure` is the Designator's
    own posture and is admitted at any finite, non-negative temperature since
    that pass may vary, sealed and recorded -- whether a value can actually be
    executed is the pass's own refusal to make, not this loader's. The section
    is required: `common/stage.py` binds the name `structure` to the sealed
    digest of these bytes on every structural seal.
    """
    if not isinstance(policy, dict):
        raise ContractError("decoding configuration is not a table")
    schema = policy.get("schema")
    if isinstance(schema, str) and schema in {"decoding.v1", "decoding.v2", "decoding.v3"}:
        raise ContractError(f"sealed under {schema}, which this build no longer reads; re-run")
    if schema != "decoding.v4":
        raise ContractError("decoding configuration has an unsupported schema")
    expected_sections = {
        "schema",
        "reading_of_record",
        "variance_experiment",
        "structure",
        "perlector_generation",
        "chandra_native_inference",
    }
    if set(policy) != expected_sections:
        raise ContractError("decoding configuration has the wrong closed schema")
    record = policy["reading_of_record"]
    variance = policy["variance_experiment"]
    structure = policy["structure"]
    expected_structure_fields = {"temperature", "recovery_seed_schedule", "recovery_max_attempts"}
    if (
        not isinstance(structure, dict)
        or set(structure) != expected_structure_fields
        or isinstance(structure["temperature"], bool)
        or not isinstance(structure["temperature"], (int, float))
        or not math.isfinite(structure["temperature"])
        or structure["temperature"] < 0
    ):
        raise ContractError("decoding structure must declare one finite, non-negative temperature")
    if (
        structure["recovery_seed_schedule"] != "base-plus-attempt-ordinal-minus-one"
        or not isinstance(structure["recovery_max_attempts"], int)
        or isinstance(structure["recovery_max_attempts"], bool)
        or not 1 <= structure["recovery_max_attempts"] <= 3
    ):
        raise ContractError(
            "decoding structure recovery must declare the supported seed schedule and "
            "an integer maximum in 1..3"
        )
    generation = policy["perlector_generation"]
    if (
        not isinstance(generation, dict)
        or set(generation) != set(_PERLECTOR_BOUNDS)
        or any(
            not isinstance(value, int) or isinstance(value, bool) or value < 1
            for value in generation.values()
        )
    ):
        raise ContractError(
            "decoding perlector_generation must declare positive integer output bounds "
            "for a reading and for a re-proof"
        )
    try:
        validate_policy_record(policy["chandra_native_inference"])
    except ContractError as error:
        raise ContractError(str(error)) from error
    if (
        not isinstance(record, dict)
        or set(record) != {"temperature"}
        or isinstance(record["temperature"], bool)
        or not isinstance(record["temperature"], (int, float))
        or record["temperature"] != 0
    ):
        raise ContractError("decoding reading_of_record must declare temperature 0")
    if not isinstance(variance, dict) or set(variance) != {"label", "seed", "passes"}:
        raise ContractError("decoding variance_experiment has the wrong closed schema")
    if not isinstance(variance["label"], str) or not variance["label"].strip():
        raise ContractError("decoding variance_experiment label must be nonblank")
    if (
        not isinstance(variance["seed"], int)
        or isinstance(variance["seed"], bool)
        or variance["seed"] < 0
    ):
        raise ContractError("decoding variance_experiment seed must be a nonnegative integer")
    if (
        not isinstance(variance["passes"], int)
        or isinstance(variance["passes"], bool)
        or variance["passes"] < 2
    ):
        raise ContractError("decoding variance_experiment passes must be an integer of at least 2")


def structure_recovery_policy(policy: Mapping[str, Any]) -> dict[str, Any]:
    """Return the attempt ceiling and seed schedule sealed into the run."""
    _validate_decoding_policy(policy)
    structure = policy["structure"]
    return {
        "max_attempts": structure["recovery_max_attempts"],
        "seed_schedule": structure["recovery_seed_schedule"],
    }


def perlector_max_tokens(policy: Mapping[str, Any]) -> tuple[int, int]:
    """Return the sealed output bounds of a Perlector reading and of its re-proof."""
    _validate_decoding_policy(policy)
    generation = policy["perlector_generation"]
    return generation["reading_max_tokens"], generation["reproof_max_tokens"]
