"""The run-sealed decoding posture for every model reading."""

from __future__ import annotations

import tomllib
from pathlib import Path
from typing import Any

from common.contracts.canonical import digest_bytes
from common.contracts.errors import ContractError
from common.contracts.identities import artifact_id, attempt_id, derive

DEFAULT_DECODING_CONFIG_PATH = Path(__file__).resolve().parents[1] / "config" / "decoding.toml"
_LOAD_RECOVERY = (
    " No run or stage artifact was written. Restore or correct the decoding file and retry"
)


def load_decoding_policy(
    path: str | Path = DEFAULT_DECODING_CONFIG_PATH,
) -> tuple[dict[str, Any], str]:
    """Read the closed policy and the digest of the exact bytes used."""
    try:
        raw = Path(path).read_bytes()
    except OSError as error:
        raise ContractError(
            f"decoding configuration at {path} could not be read: {error}.{_LOAD_RECOVERY}"
        ) from error
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
    """Close both sections before their values can mint provenance identities."""
    if not isinstance(policy, dict):
        raise ContractError("decoding configuration is not a table")
    if set(policy) != {"schema", "reading_of_record", "variance_experiment"}:
        raise ContractError("decoding configuration has the wrong closed schema")
    if policy["schema"] != "decoding.v1":
        raise ContractError("decoding configuration has an unsupported schema")
    record = policy["reading_of_record"]
    variance = policy["variance_experiment"]
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
