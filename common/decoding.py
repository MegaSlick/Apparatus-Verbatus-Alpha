"""The run-sealed decoding posture for every model reading."""

from __future__ import annotations

import math
import tomllib
from pathlib import Path
from typing import Any, Final, Mapping

from common.chandra_native_retry import validate_policy_record
from common.contracts.canonical import digest_bytes
from common.contracts.errors import ContractError
from common.contracts.identities import artifact_id, attempt_id, derive

DEFAULT_DECODING_CONFIG_PATH = Path(__file__).resolve().parents[1] / "config" / "decoding.toml"
MAX_DECODING_CONFIG_BYTES: Final = 64 * 1024
_LOAD_RECOVERY = (
    " No run or stage artifact was written. Restore or correct the decoding file and retry"
)


def load_decoding_policy(
    path: str | Path = DEFAULT_DECODING_CONFIG_PATH,
) -> tuple[dict[str, Any], str]:
    """Read the closed policy and the digest of the exact bytes used."""
    try:
        with Path(path).open("rb") as handle:
            raw = handle.read(MAX_DECODING_CONFIG_BYTES + 1)
    except OSError as error:
        raise ContractError(
            f"decoding configuration at {path} could not be read: {error}.{_LOAD_RECOVERY}"
        ) from error
    if len(raw) > MAX_DECODING_CONFIG_BYTES:
        raise ContractError(
            f"decoding configuration at {path} exceeds the "
            f"{MAX_DECODING_CONFIG_BYTES}-byte limit; a run policy is bounded "
            f"metadata, not a corpus payload.{_LOAD_RECOVERY}"
        )
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as error:
        raise ContractError(
            f"decoding configuration at {path} is not UTF-8: {error}.{_LOAD_RECOVERY}"
        ) from error
    try:
        policy = tomllib.loads(text)
    except tomllib.TOMLDecodeError as error:
        raise ContractError(
            f"decoding configuration at {path} is not valid TOML: {error}.{_LOAD_RECOVERY}"
        ) from error
    try:
        _validate_decoding_policy(policy)
    except ContractError as error:
        raise ContractError(f"{error}.{_LOAD_RECOVERY}") from error
    return policy, digest_bytes(raw)


def _validate_decoding_policy(policy: Any) -> None:
    """Close every section before its values can mint provenance identities.

    The legacy schemas carry three sections. ``decoding.v3`` adds the exact,
    closed Chandra native inference recipe admitted for Attestator 1; there is
    no enabled flag, and a v1/v2 run cannot acquire the capability on resume.
    `reading_of_record` is pinned to temperature
    0: it is the posture every Attestator and the Perlector read under, and a
    reading of record that varied would not be one. `structure` is the
    Designator's structure pass's own posture and is
    admitted at any finite, non-negative temperature: this
    pass may vary, sealed and recorded, so the loader does not pin the number
    -- whether a given value can actually be executed is the pass's own refusal
    to make at its point of use, not this loader's to hide by rejecting the
    bytes. The section is required, not optional: `common/stage.py` binds the
    name `structure` to the sealed digest of these bytes on every structural
    seal, and a sealed file with no such section would be a posture named
    over bytes that do not contain it.
    """
    if not isinstance(policy, dict):
        raise ContractError("decoding configuration is not a table")
    expected_sections = {"schema", "reading_of_record", "variance_experiment", "structure"}
    if policy.get("schema") == "decoding.v3":
        expected_sections.add("chandra_native_inference")
    if set(policy) != expected_sections:
        raise ContractError("decoding configuration has the wrong closed schema")
    if policy["schema"] not in {"decoding.v1", "decoding.v2", "decoding.v3"}:
        raise ContractError("decoding configuration has an unsupported schema")
    record = policy["reading_of_record"]
    variance = policy["variance_experiment"]
    structure = policy["structure"]
    expected_structure_fields = (
        {"temperature"}
        if policy["schema"] == "decoding.v1"
        else {"temperature", "recovery_seed_schedule", "recovery_max_attempts"}
    )
    if (
        not isinstance(structure, dict)
        or set(structure) != expected_structure_fields
        or isinstance(structure["temperature"], bool)
        or not isinstance(structure["temperature"], (int, float))
        or not math.isfinite(structure["temperature"])
        or structure["temperature"] < 0
    ):
        raise ContractError("decoding structure must declare one finite, non-negative temperature")
    if policy["schema"] in {"decoding.v2", "decoding.v3"} and (
        structure["recovery_seed_schedule"] != "base-plus-attempt-ordinal-minus-one"
        or not isinstance(structure["recovery_max_attempts"], int)
        or isinstance(structure["recovery_max_attempts"], bool)
        or not 1 <= structure["recovery_max_attempts"] <= 3
    ):
        raise ContractError(
            "decoding structure recovery must declare the supported seed schedule and "
            "an integer maximum in 1..3"
        )
    if policy["schema"] == "decoding.v3":
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


def variance_experiment_id(policy: dict[str, Any]) -> str:
    """Name the sealed variance plan, rather than an invocation that happens to run it."""
    _validate_decoding_policy(policy)
    variance = policy["variance_experiment"]
    return derive("variance-experiment", variance)


def structure_recovery_policy(policy: Mapping[str, Any]) -> dict[str, Any]:
    """Return the sealed structure recovery policy, including legacy v1 semantics.

    ``decoding.v1`` predates structure recovery and therefore permits exactly
    one request at the serving row's base seed.  New runs use ``v2``, where
    both the attempt ceiling and deterministic seed schedule are explicit in
    the bytes sealed into the run.
    """
    _validate_decoding_policy(policy)
    if policy["schema"] == "decoding.v1":
        return {"max_attempts": 1, "seed_schedule": "fixed-base"}
    structure = policy["structure"]
    return {
        "max_attempts": structure["recovery_max_attempts"],
        "seed_schedule": structure["recovery_seed_schedule"],
    }


def variance_pass_attempt_id(policy: dict[str, Any], pass_ordinal: int) -> str:
    """Derive one experimental pass, never a retry of a record reading.

    The experiment's label, seed, and declared pass count are folded into the
    subject first.  A pass then has its own ``variance-pass`` operation and
    ordinal, which keeps it outside the identity space of ordinary ``read`` or
    ``perlegere`` attempts even when it sees the same act.
    """
    experiment_id = variance_experiment_id(policy)
    passes = policy["variance_experiment"]["passes"]
    if (
        not isinstance(pass_ordinal, int)
        or isinstance(pass_ordinal, bool)
        or not 1 <= pass_ordinal <= passes
    ):
        raise ContractError(f"variance pass ordinal must be in the sealed range 1..{passes}")
    return attempt_id(experiment_id, "variance-pass", pass_ordinal)


def variance_pass_artifact_id(
    stage: str, kind: str, subject_id: str, policy: dict[str, Any], pass_ordinal: int
) -> str:
    """The immutable artifact identity for one pass of the sealed experiment."""
    return artifact_id(stage, kind, subject_id, variance_pass_attempt_id(policy, pass_ordinal))
