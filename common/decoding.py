"""The run-sealed decoding posture for every model reading."""

from __future__ import annotations

import math
import tomllib
from pathlib import Path
from typing import Any, Final

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

    Three sections, each closed. `reading_of_record` is pinned to temperature
    0: it is the posture every Attestator and the Perlector read under, and a
    reading of record that varied would not be one. `structure` is the
    Designator's structure pass's own posture (Tyrel, 2026-09-02) and is
    admitted at any finite, non-negative temperature: the ruling is that this
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
    if set(policy) != {"schema", "reading_of_record", "variance_experiment", "structure"}:
        raise ContractError("decoding configuration has the wrong closed schema")
    if policy["schema"] != "decoding.v1":
        raise ContractError("decoding configuration has an unsupported schema")
    record = policy["reading_of_record"]
    variance = policy["variance_experiment"]
    structure = policy["structure"]
    if (
        not isinstance(structure, dict)
        or set(structure) != {"temperature"}
        or isinstance(structure["temperature"], bool)
        or not isinstance(structure["temperature"], (int, float))
        or not math.isfinite(structure["temperature"])
        or structure["temperature"] < 0
    ):
        raise ContractError("decoding structure must declare one finite, non-negative temperature")
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
