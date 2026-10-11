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
from common.contracts.serving import (
    CALL_RECORD_SCHEMAS,
    CALLER_GENERATION_FIELDS,
    WIRE_DECIMAL_FIELDS,
    WIRE_DECIMAL_SCHEMA,
)
from common.repetition_loop import GUARD_FIELDS, validate_guard
from common.sealed_config import read_sealed_toml

DEFAULT_DECODING_CONFIG_PATH = Path(__file__).resolve().parents[1] / "config" / "decoding.toml"
_PERLECTOR_BOUNDS = ("page_max_tokens", *GUARD_FIELDS)
_RECONSTRUCTOR_BOUNDS = ("answer_max_tokens",)
# The witness chairs whose replies are streamed under `[witness_generation]`'s
# guard: every served witness that sends a plain request. Chandra
# (`attestator_1`) reads under its native recipe's own retry loop instead.
STREAMED_WITNESS_CHAIRS = frozenset({"attestator_2", "attestator_3", "attestator_4"})
# Every sampling field a chair's row may carry, each a top-level field of the
# pinned vLLM 0.30.0 `ChatCompletionRequest`. Only the sealed table puts them on
# the wire; a caller's request may not name them.
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
# The sampling fields the pinned engine fills from a model's
# `generation_config.json` when a request leaves them out (vllm==0.30.0,
# `ModelConfig.get_diff_sampling_param`'s `available_params`, less
# `max_new_tokens`, which is not a sampling field). Every chair's row names all
# of them, so no reading's sampling rests on a value nobody sealed.
ENGINE_FILLED_SAMPLING_FIELDS = frozenset(
    {"temperature", "top_p", "top_k", "min_p", "repetition_penalty"}
)
# What the pinned engine samples under for a field a request leaves out when no
# generation config fills it (vllm==0.30.0,
# `ChatCompletionRequest._DEFAULT_SAMPLING_PARAMS`): the values a maker who
# serves with vLLM and sends only some fields reads under for the rest.
VLLM_REQUEST_DEFAULTS: dict[str, int | float] = {
    "temperature": 1.0,
    "top_p": 1.0,
    "top_k": 0,
    "min_p": 0.0,
    "repetition_penalty": 1.0,
}
# The ranges the pinned vLLM 0.30.0 `SamplingParams._verify_args` accepts, so a
# row the engine would refuse is refused when the policy loads. Each is
# (low, high, low inclusive); presence and frequency penalties may be negative,
# which rewards repetition. top_k is an integer, 0 meaning "off"; vLLM also
# still accepts -1 for "off", which the policy refuses so a row names one form.
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
# (vllm==0.30.0, `vllm/sampling_params.py`, `SamplingParams.__post_init__`): a
# temperature above zero but below `_MAX_TEMP` is raised to it, and a
# temperature below `_SAMPLING_EPS` is greedy, which sets top_p to 1, top_k to 0
# and min_p to 0. The call record keeps the sent value and this effective one.
VLLM_ENGINE = "vllm==0.30.0"
_VLLM_MAX_TEMP = 1e-2
_VLLM_SAMPLING_EPS = 1e-5
_VLLM_GREEDY_OVERRIDES: dict[str, int | float] = {"top_p": 1.0, "top_k": 0, "min_p": 0.0}
_PROVENANCE_FIELDS = frozenset({"source", "revision", "verification"})
READING_CHAIRS = frozenset(
    {"attestator_1", "attestator_2", "attestator_3", "perlector", "reconstructor"}
)
# Reading chairs a decoding file may carry a row for, but need not: dots.mocr
# (`attestator_4`) is seated only by a roster that routes it
# (`common/witness_routing.py`), and the committed file, which seats no such
# chair, carries no row, so its digest is the one it always was. A served
# chair with no row is refused by `ChairClient` before any request.
OPTIONAL_READING_CHAIRS = frozenset({"attestator_4"})
# The chair Chandra fills. Chandra's own pipeline sends the pinned recipe's
# temperature and top_p to a vLLM server and nothing else, so its row must be
# the recipe's first request over vLLM's defaults, and cannot drift from it.
_CHANDRA_CHAIR = "attestator_1"
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
    values and where they were read. `perlector_generation` caps one whole-page
    reading's output and seals the repetition-loop guard its streamed reply is
    stopped by, `witness_generation` seals the guard of every streamed witness
    reply, and `reconstructor_generation` caps one Coniector answer.
    """
    if not isinstance(policy, dict):
        raise ContractError("decoding configuration is not a table")
    if policy.get("schema") != "decoding.v10":
        raise ContractError("decoding configuration has an unsupported schema")
    expected_sections = {
        "schema",
        "chair_decoding",
        "perlector_generation",
        "witness_generation",
        "reconstructor_generation",
        "chandra_native_inference",
    }
    if set(policy) != expected_sections:
        raise ContractError("decoding configuration has the wrong closed schema")
    _require_output_bounds(
        policy["perlector_generation"],
        _PERLECTOR_BOUNDS,
        "decoding perlector_generation must declare a positive integer output bound and "
        "repetition-loop guard for a whole-page reading",
    )
    try:
        validate_guard({name: policy["perlector_generation"][name] for name in GUARD_FIELDS})
    except ContractError as error:
        raise ContractError(f"decoding perlector_generation: {error}") from error
    try:
        validate_guard(policy["witness_generation"])
    except ContractError as error:
        raise ContractError(f"decoding witness_generation: {error}") from error
    _require_output_bounds(
        policy["reconstructor_generation"],
        _RECONSTRUCTOR_BOUNDS,
        "decoding reconstructor_generation must declare a positive integer output bound for "
        "one reconstruction answer",
    )
    try:
        validate_policy_record(policy["chandra_native_inference"])
    except ContractError as error:
        raise ContractError(str(error)) from error
    _validate_chair_decoding(policy["chair_decoding"])


def _require_output_bounds(generation: Any, names: tuple[str, ...], refusal: str) -> None:
    if (
        not isinstance(generation, dict)
        or set(generation) != set(names)
        or any(
            not isinstance(value, int) or isinstance(value, bool) or value < 1
            for value in generation.values()
        )
    ):
        raise ContractError(refusal)


def _validate_chair_decoding(table: Any) -> None:
    """Close the per-chair sampling table: known chairs, known fields, finite values."""
    if (
        not isinstance(table, dict)
        or not READING_CHAIRS <= set(table) <= READING_CHAIRS | OPTIONAL_READING_CHAIRS
    ):
        raise ContractError(
            f"decoding chair_decoding must hold exactly one row per reading chair "
            f"{sorted(READING_CHAIRS)}, and may hold one for {sorted(OPTIONAL_READING_CHAIRS)}"
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
        missing = sorted(ENGINE_FILLED_SAMPLING_FIELDS - set(row))
        if missing:
            raise ContractError(
                f"decoding chair_decoding.{chair} must state {missing}; an absent one would "
                "leave an engine or generation-config default in force"
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
    first_chandra_request = {**VLLM_REQUEST_DEFAULTS, **wire_parameters(1)}
    if _sampling_values(table[_CHANDRA_CHAIR]) != first_chandra_request:
        raise ContractError(
            f"decoding chair_decoding.{_CHANDRA_CHAIR} must equal the first request of the "
            f"pinned Chandra recipe over vLLM's defaults, {first_chandra_request!r}"
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

    Attempt one is the chair's row. Only Attestator 1's Chandra native page
    route has later attempts, each the row with the pinned recipe's temperature
    and top_p for its ordinal, within the recipe's own seven.
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
    if chair != _CHANDRA_CHAIR or attempt_ordinal > CHANDRA_MAX_ATTEMPTS:
        raise ContractError(
            f"decoding has no attempt {attempt_ordinal} for chair {chair!r}; only the Chandra "
            "chair retries, within its recipe's ceiling"
        )
    return {**row, **wire_parameters(attempt_ordinal)}


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


def recorded_wire_decimals(view: Mapping[str, object]) -> dict[str, object]:
    """A sent or effective generation view in the form a call record holds it.

    Canonical artifacts refuse floats, since their JSON form is not stable
    enough to hash, so every float at any depth becomes the exact decimal text
    the wire body carries, tagged `wire-decimal.v1` so a reader can tell it
    from a declared string. `decoded_wire_decimals` is its strict inverse, so a
    non-finite float, which it would refuse, raises `ContractError` here.
    """
    return {key: _recorded_wire_value(item) for key, item in view.items()}


def _recorded_wire_value(value: object) -> object:
    if isinstance(value, float):
        try:
            decimal = json.dumps(value, allow_nan=False)
        except ValueError as error:
            raise ContractError(f"a wire decimal is not finite: {value!r}") from error
        return {"schema": WIRE_DECIMAL_SCHEMA, "decimal": decimal}
    if isinstance(value, Mapping):
        return {key: _recorded_wire_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_recorded_wire_value(item) for item in value]
    return value


def decoded_wire_decimals(value: object) -> object:
    """Every tagged `wire-decimal.v1` form in a recorded value, back to its float.

    Strict, so exactly one recorded form stands for each sent value: the
    decimal must be a string naming a finite float whose `json.dumps` text is
    that same string. Anything else raises `ContractError`.
    """
    if isinstance(value, Mapping):
        if set(value) == WIRE_DECIMAL_FIELDS and value.get("schema") == WIRE_DECIMAL_SCHEMA:
            decimal = value.get("decimal")
            if not isinstance(decimal, str):
                raise ContractError(f"a wire decimal is not text: {decimal!r}")
            try:
                decoded = float(decimal)
            except ValueError as error:
                raise ContractError(f"a wire decimal is not a number: {decimal!r}") from error
            if not math.isfinite(decoded) or json.dumps(decoded) != decimal:
                raise ContractError(f"a wire decimal is not canonical: {decimal!r}")
            return decoded
        return {key: decoded_wire_decimals(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [decoded_wire_decimals(item) for item in value]
    return value


def verify_call_sampling(
    call: Mapping[str, Any],
    policy: Mapping[str, Any],
    chair: str,
    *,
    expected_seed: int | None,
    attempt_ordinal: int = 1,
) -> None:
    """Refuse a call record whose sampling is not the sealed policy's for its chair.

    `generation_sent` may carry only a caller's generation fields, sampling
    fields and the seed; its sampling fields must be exactly the attempt's
    sealed values, and `sampling_effective` the pinned engine's reading of them.
    `expected_seed` is the seed the call sent: the serving receipt's. A Chandra
    native request sends none, and its reader says so with `None`.
    """
    if call.get("schema") not in CALL_RECORD_SCHEMAS:
        raise ContractError(
            f"a {chair} call record has schema {call.get('schema')!r}, not one this build writes"
        )
    expected = chair_attempt_decoding(policy, chair, attempt_ordinal)
    sent = call.get("generation_sent")
    if not isinstance(sent, Mapping):
        raise ContractError(f"a {chair} call record carries no generation_sent object")
    unknown = sorted(set(sent) - CALLER_GENERATION_FIELDS - SAMPLING_FIELDS - {"seed"})
    if unknown:
        raise ContractError(
            f"a {chair} call record sent generation field(s) {unknown}, which no caller, "
            "sealed row or seed puts on the wire"
        )
    observed = {field: sent[field] for field in SAMPLING_FIELDS if field in sent}
    if observed != recorded_wire_decimals(expected):
        raise ContractError(
            f"a {chair} call record sent sampling {observed!r}, not the sealed "
            f"{recorded_wire_decimals(expected)!r} for attempt {attempt_ordinal}"
        )
    if expected_seed is None:
        if "seed" in sent:
            raise ContractError(f"a {chair} call record sent a seed its request sends none of")
    elif type(sent.get("seed")) is not int or sent["seed"] != expected_seed:
        raise ContractError(
            f"a {chair} call record sent seed {sent.get('seed')!r}, not {expected_seed!r}"
        )
    if call.get("sampling_effective") != recorded_wire_decimals(
        engine_effective_sampling(expected)
    ):
        raise ContractError(
            f"a {chair} call record's sampling_effective is not what {VLLM_ENGINE} samples "
            "under for the sealed values"
        )


def reconstructor_max_tokens(policy: Mapping[str, Any]) -> int:
    """Return the sealed output cap of one Coniector reconstruction answer."""
    _validate_decoding_policy(policy)
    return policy["reconstructor_generation"]["answer_max_tokens"]


def perlector_page_max_tokens(policy: Mapping[str, Any]) -> int:
    """Return the sealed output cap of one whole-page Perlector reading."""
    _validate_decoding_policy(policy)
    return policy["perlector_generation"]["page_max_tokens"]


def perlector_page_generation(policy: Mapping[str, Any]) -> dict[str, int]:
    """Return the sealed bound of one whole-page reading's output, as
    `common.request_capacity.page_request_capacity` takes it."""
    _validate_decoding_policy(policy)
    return {"page_max_tokens": policy["perlector_generation"]["page_max_tokens"]}


def perlector_loop_guard(policy: Mapping[str, Any]) -> dict[str, int]:
    """Return the sealed repetition-loop guard a whole-page reading's streamed reply is
    stopped by (`common.repetition_loop`)."""
    _validate_decoding_policy(policy)
    return {name: policy["perlector_generation"][name] for name in GUARD_FIELDS}


def witness_loop_guard(policy: Mapping[str, Any], chair: str) -> dict[str, int]:
    """Return the sealed repetition-loop guard a witness chair's streamed reply is
    stopped by (`common.repetition_loop`); a chair that is never streamed has none."""
    _validate_decoding_policy(policy)
    if chair not in STREAMED_WITNESS_CHAIRS:
        raise ContractError(
            f"chair {chair!r} has no witness repetition-loop guard; the streamed witness "
            f"chairs are {sorted(STREAMED_WITNESS_CHAIRS)}"
        )
    return validate_guard(policy["witness_generation"])
