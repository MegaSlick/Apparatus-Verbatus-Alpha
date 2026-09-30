"""The run-sealed decoding posture for every model reading."""

from __future__ import annotations

import json
import math
from pathlib import Path
from typing import Any, Mapping

from common.chandra_native_retry import (
    CHANDRA_MAX_ATTEMPTS,
    validate_policy_record,
    wire_parameters,
)
from common.contracts.errors import ContractError
from common.contracts.serving import WIRE_DECIMAL_SCHEMA
from common.sealed_config import read_sealed_toml

DEFAULT_DECODING_CONFIG_PATH = Path(__file__).resolve().parents[1] / "config" / "decoding.toml"
_PERLECTOR_BOUNDS = ("reading_max_tokens", "reproof_max_tokens")
# Every sampling field a chair's row may carry: the top-level sampling fields of
# the pinned vLLM 0.27.1 `ChatCompletionRequest`. Only the sealed table puts
# them on the wire; a caller's request may not name them.
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
# The ranges the pinned vLLM 0.27.1 `SamplingParams._verify_args` accepts, so a
# row the engine would refuse is refused when the policy loads. Each is
# (low, high, low inclusive); presence and frequency penalties may be negative,
# which rewards repetition. top_k is an integer, 0 meaning "off".
_SAMPLING_RANGES: dict[str, tuple[float, float, bool]] = {
    "temperature": (0.0, 2.0, True),
    "top_p": (0.0, 1.0, False),
    "top_k": (0, math.inf, True),
    "min_p": (0.0, 1.0, True),
    "presence_penalty": (-2.0, 2.0, True),
    "frequency_penalty": (-2.0, 2.0, True),
    "repetition_penalty": (0.0, math.inf, False),
}
# How the pinned engine rewrites a request's sampling values before it samples
# (vllm==0.27.1, `vllm/sampling_params.py`, `SamplingParams.__post_init__`): a
# temperature above zero but below `_MAX_TEMP` is raised to it, and a
# temperature below `_SAMPLING_EPS` is greedy, which sets top_p to 1, top_k to 0
# and min_p to 0. The call record keeps the sent value and this effective one.
VLLM_ENGINE = "vllm==0.27.1"
_VLLM_MAX_TEMP = 1e-2
_VLLM_SAMPLING_EPS = 1e-5
_VLLM_GREEDY_OVERRIDES: dict[str, int | float] = {"top_p": 1.0, "top_k": 0, "min_p": 0.0}
# The two arms of the sampling-variance experiment, in the order their seeds
# are derived: the arm at index i sends `variance_experiment.seed + i`.
VARIANCE_ARMS = ("lectio-prior", "lectio-nuda")
_MAX_SEED = 2**63 - 1
_PROVENANCE_FIELDS = frozenset({"source", "revision", "verification"})
READING_CHAIRS = frozenset(
    {"designator_structure", "attestator_1", "attestator_2", "attestator_3", "perlector"}
)
# The chairs Chandra fills; their rows must be the first request of the pinned
# Chandra recipe, so the structure chair and a plain Attestator 1 read cannot
# drift from the maker's own pipeline.
_CHANDRA_CHAIRS = ("designator_structure", "attestator_1")
# The structure chair's coverage recovery sends, at attempt n, the pinned Chandra
# recipe's own request n: the maker's recovery from a degenerate page.
STRUCTURE_RECOVERY_SCHEDULE = "chandra-native-retry"
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
    recovery policy; the section is required, since `common/stage.py` binds the
    name `structure` to the sealed seal of this configuration on every structural
    seal. `variance_experiment` seeds the two sampling-variance arms.
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
        "recovery_schedule",
        "recovery_max_attempts",
    }:
        raise ContractError("decoding structure must declare only its coverage recovery")
    if (
        structure["recovery_schedule"] != STRUCTURE_RECOVERY_SCHEDULE
        or not isinstance(structure["recovery_max_attempts"], int)
        or isinstance(structure["recovery_max_attempts"], bool)
        or not 1 <= structure["recovery_max_attempts"] <= 3
    ):
        raise ContractError(
            f"decoding structure recovery must declare the {STRUCTURE_RECOVERY_SCHEDULE!r} "
            "schedule and an integer maximum in 1..3"
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
    if not isinstance(variance, dict) or set(variance) != {"seed"}:
        raise ContractError("decoding variance_experiment has the wrong closed schema")
    if (
        not isinstance(variance["seed"], int)
        or isinstance(variance["seed"], bool)
        or not 0 <= variance["seed"] <= _MAX_SEED - len(VARIANCE_ARMS)
    ):
        raise ContractError(
            "decoding variance_experiment seed must be a nonnegative integer that leaves every "
            "arm's seed within vLLM's 64-bit range"
        )


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
        if "temperature" not in row:
            raise ContractError(
                f"decoding chair_decoding.{chair} must state its temperature; an absent one "
                "would leave the engine's default in force"
            )
        for field in sorted(SAMPLING_FIELDS & set(row)):
            value = row[field]
            low, high, low_inclusive = _SAMPLING_RANGES[field]
            if (
                isinstance(value, bool)
                or not isinstance(value, (int, float))
                or not math.isfinite(value)
                or (field == "top_k" and not isinstance(value, int))
                or value > high
                or value < low
                or (value == low and not low_inclusive)
            ):
                interval = f"{'[' if low_inclusive else '('}{low}, {high}]"
                raise ContractError(
                    f"decoding chair_decoding.{chair}.{field} must be a finite number in "
                    f"{interval}{', and an integer' if field == 'top_k' else ''}; got {value!r}"
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


def chair_attempt_decoding(
    policy: Mapping[str, Any], chair: str, attempt_ordinal: int
) -> dict[str, int | float]:
    """The sampling values one attempt of a chair sends.

    Attempt one is the chair's row. Only the two Chandra chairs have later
    attempts, and each sends the pinned recipe's request for its ordinal: the
    structure chair within its sealed recovery ceiling, Attestator 1's native
    page route within the recipe's own seven.
    """
    row = chair_decoding(policy, chair)
    if (
        not isinstance(attempt_ordinal, int)
        or isinstance(attempt_ordinal, bool)
        or attempt_ordinal < 1
    ):
        raise ContractError(
            f"a decoding attempt ordinal must be a positive integer, not {attempt_ordinal!r}"
        )
    if attempt_ordinal == 1:
        return row
    ceiling = {
        "designator_structure": policy["structure"]["recovery_max_attempts"],
        "attestator_1": CHANDRA_MAX_ATTEMPTS,
    }.get(chair)
    if ceiling is None or attempt_ordinal > ceiling:
        raise ContractError(
            f"decoding has no attempt {attempt_ordinal} for chair {chair!r}; only the Chandra "
            "chairs retry, within their sealed ceilings"
        )
    return wire_parameters(attempt_ordinal)


def variance_arm_seed(policy: Mapping[str, Any], arm: str) -> int:
    """The seed one arm of the sampling-variance experiment sends.

    Lectio nuda and the lectio-prior draft are the same request, so under one
    seed they would be one draw; each arm's own seed makes them two.
    """
    _validate_decoding_policy(policy)
    if arm not in VARIANCE_ARMS:
        raise ContractError(
            f"{arm!r} is not an arm of the variance experiment; the arms are {list(VARIANCE_ARMS)}"
        )
    return policy["variance_experiment"]["seed"] + VARIANCE_ARMS.index(arm)


def engine_effective_sampling(sampling: Mapping[str, int | float]) -> dict[str, int | float]:
    """The values the pinned engine samples under, for the fields a request sent."""
    effective = dict(sampling)
    temperature = effective.get("temperature")
    if temperature is None:
        return effective
    if 0 < temperature < _VLLM_MAX_TEMP:
        effective["temperature"] = _VLLM_MAX_TEMP
    elif temperature < _VLLM_SAMPLING_EPS:
        for field, value in _VLLM_GREEDY_OVERRIDES.items():
            if field in effective:
                effective[field] = value
    return effective


def recorded_sampling(sampling: Mapping[str, int | float]) -> dict[str, object]:
    """Sampling values in the form a call record holds them.

    Canonical artifacts refuse floats, so a float is its exact wire decimal,
    tagged `wire-decimal.v1`, the transcription `ChairClient` records.
    """
    return {
        field: (
            {"schema": WIRE_DECIMAL_SCHEMA, "decimal": json.dumps(value)}
            if isinstance(value, float)
            else value
        )
        for field, value in sampling.items()
    }


def verify_call_sampling(
    call: Mapping[str, Any],
    policy: Mapping[str, Any],
    chair: str,
    *,
    attempt_ordinal: int = 1,
) -> None:
    """Refuse a call record whose sampling is not the sealed policy's for its chair.

    `generation_sent` must carry exactly the attempt's sealed sampling fields,
    and `sampling_effective` the pinned engine's reading of them.
    """
    expected = chair_attempt_decoding(policy, chair, attempt_ordinal)
    sent = call.get("generation_sent")
    if not isinstance(sent, Mapping):
        raise ContractError(f"a {chair} call record carries no generation_sent object")
    observed = {field: sent[field] for field in SAMPLING_FIELDS if field in sent}
    if observed != recorded_sampling(expected):
        raise ContractError(
            f"a {chair} call record sent sampling {observed!r}, not the sealed "
            f"{recorded_sampling(expected)!r} for attempt {attempt_ordinal}"
        )
    if call.get("sampling_effective") != recorded_sampling(engine_effective_sampling(expected)):
        raise ContractError(
            f"a {chair} call record's sampling_effective is not what {VLLM_ENGINE} samples "
            "under for the sealed values"
        )


def structure_recovery_policy(policy: Mapping[str, Any]) -> dict[str, Any]:
    """Return the attempt ceiling and sampling schedule sealed into the run."""
    _validate_decoding_policy(policy)
    structure = policy["structure"]
    return {
        "max_attempts": structure["recovery_max_attempts"],
        "sampling_schedule": structure["recovery_schedule"],
    }


def perlector_max_tokens(policy: Mapping[str, Any]) -> tuple[int, int]:
    """Return the sealed output bounds of a Perlector reading and of its re-proof."""
    _validate_decoding_policy(policy)
    generation = policy["perlector_generation"]
    return generation["reading_max_tokens"], generation["reproof_max_tokens"]
