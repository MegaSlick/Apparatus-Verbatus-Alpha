"""The run-sealed decoding posture for every model reading."""

from __future__ import annotations

import math
from pathlib import Path
from typing import Any, Mapping

from common.chandra_native_retry import validate_policy_record, wire_parameters
from common.contracts.errors import ContractError
from common.sealed_config import read_sealed_toml

DEFAULT_DECODING_CONFIG_PATH = Path(__file__).resolve().parents[1] / "config" / "decoding.toml"
_PERLECTOR_BOUNDS = ("reading_max_tokens", "reproof_max_tokens")
# Every sampling field a chair's row may carry: the fields an OpenAI-compatible
# vLLM request accepts for them. The serving client refuses any of them from a
# caller, so these values reach the wire only through the sealed table.
SAMPLING_FIELDS = frozenset(
    {
        "temperature",
        "top_p",
        "top_k",
        "min_p",
        "presence_penalty",
        "frequency_penalty",
        "repetition_penalty",
    }
)
# vLLM admits a negative presence or frequency penalty, which rewards repetition.
_SIGNED_FIELDS = frozenset({"presence_penalty", "frequency_penalty"})
_PROVENANCE_FIELDS = frozenset({"source", "revision", "verification"})
READING_CHAIRS = frozenset(
    {"designator_structure", "attestator_1", "attestator_2", "attestator_3", "perlector"}
)
# The chairs Chandra fills; their rows must be the first request of the pinned
# Chandra recipe, so the structure chair and a plain Attestator 1 read cannot
# drift from the maker's own pipeline.
_CHANDRA_CHAIRS = ("designator_structure", "attestator_1")
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
    `chair_decoding` holds one row per reading chair with its makers' sampling
    values and where they were read. `structure` is the Designator's coverage
    recovery policy. The section is required: `common/stage.py` binds the name
    `structure` to the sealed seal of this configuration on every structural seal.
    """
    if not isinstance(policy, dict):
        raise ContractError("decoding configuration is not a table")
    schema = policy.get("schema")
    if isinstance(schema, str) and schema in {
        "decoding.v1",
        "decoding.v2",
        "decoding.v3",
        "decoding.v4",
    }:
        raise ContractError(f"sealed under {schema}, which this build no longer reads; re-run")
    if schema != "decoding.v5":
        raise ContractError("decoding configuration has an unsupported schema")
    expected_sections = {
        "schema",
        "chair_decoding",
        "variance_experiment",
        "structure",
        "perlector_generation",
        "chandra_native_inference",
    }
    if set(policy) != expected_sections:
        raise ContractError("decoding configuration has the wrong closed schema")
    variance = policy["variance_experiment"]
    structure = policy["structure"]
    if not isinstance(structure, dict) or set(structure) != {
        "recovery_seed_schedule",
        "recovery_max_attempts",
    }:
        raise ContractError("decoding structure must declare only its coverage recovery")
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
    _validate_chair_decoding(policy["chair_decoding"])
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


def _validate_chair_decoding(table: Any) -> None:
    """Close the per-chair sampling table: known chairs, known fields, finite values."""
    if not isinstance(table, dict) or set(table) != READING_CHAIRS:
        raise ContractError(
            f"decoding chair_decoding must hold exactly one row per reading chair "
            f"{sorted(READING_CHAIRS)}"
        )
    for chair, row in table.items():
        if not isinstance(row, dict) or not _PROVENANCE_FIELDS <= set(row):
            raise ContractError(
                f"decoding chair_decoding.{chair} must name its source, revision and verification"
            )
        unknown = sorted(set(row) - SAMPLING_FIELDS - _PROVENANCE_FIELDS)
        if unknown:
            raise ContractError(
                f"decoding chair_decoding.{chair} has unknown field(s) {unknown}; an unread "
                "sampling field cannot be sent"
            )
        if any(
            not isinstance(row[field], str) or not row[field].strip()
            for field in _PROVENANCE_FIELDS
        ):
            raise ContractError(
                f"decoding chair_decoding.{chair} source, revision and verification must be "
                "nonblank text"
            )
        for field in SAMPLING_FIELDS & set(row):
            value = row[field]
            if (
                isinstance(value, bool)
                or not isinstance(value, (int, float))
                or not math.isfinite(value)
                or (value < 0 and field not in _SIGNED_FIELDS)
                or (field == "top_k" and not isinstance(value, int))
            ):
                raise ContractError(
                    f"decoding chair_decoding.{chair}.{field} must be a finite number, "
                    "non-negative unless it is a presence or frequency penalty, and an "
                    "integer for top_k"
                )
    first_chandra_request = wire_parameters(1)
    for chair in _CHANDRA_CHAIRS:
        if _sampling_values(table[chair]) != first_chandra_request:
            raise ContractError(
                f"decoding chair_decoding.{chair} must equal the first request of the pinned "
                f"Chandra recipe, {first_chandra_request!r}"
            )


def _sampling_values(row: Mapping[str, Any]) -> dict[str, int | float]:
    return {field: value for field, value in row.items() if field in SAMPLING_FIELDS}


def chair_decoding(policy: Mapping[str, Any], chair: str) -> dict[str, int | float]:
    """Return the sampling values sealed for one reading chair, exactly as sent."""
    _validate_decoding_policy(policy)
    table = policy["chair_decoding"]
    if chair not in table:
        raise ContractError(
            f"decoding chair_decoding has no row for chair {chair!r}; only reading chairs "
            f"{sorted(READING_CHAIRS)} have sampling values"
        )
    return _sampling_values(table[chair])


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
