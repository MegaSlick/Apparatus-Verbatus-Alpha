"""How a TOML configuration is sealed into a run, and rechecked where it is used.

The seal is the digest of what the file says, not how it is written: comments,
whitespace and key order are free to edit, and any value change moves it.
"""

import json
import tomllib
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Any, Final

from common.contracts.canonical import digest_bytes
from common.contracts.errors import ContractError

# A run policy is bounded metadata, not a corpus payload.
MAX_CONFIG_BYTES: Final = 1 << 20


def parse_sealed_toml(
    data: bytes, what: str, allowed_keys: Iterable[str] | None = None
) -> tuple[dict[str, Any], str]:
    """Parse one configuration's bytes and return the table with its seal."""
    if len(data) > MAX_CONFIG_BYTES:
        raise ContractError(f"the {what} exceeds the {MAX_CONFIG_BYTES}-byte limit")
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError as error:
        raise ContractError(f"the {what} is not valid UTF-8: {error}") from error
    try:
        table = tomllib.loads(text)
    except tomllib.TOMLDecodeError as error:
        raise ContractError(f"the {what} is not valid TOML: {error}") from error
    if allowed_keys is not None:
        unknown = sorted(set(table) - set(allowed_keys))
        if unknown:
            raise ContractError(
                f"the {what} has unknown top-level field(s) {unknown}; an unread policy "
                "field cannot be applied"
            )
    try:
        # Not `canonical_bytes`, which refuses floats: a decoding temperature is one.
        sealed = json.dumps(table, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    except TypeError as error:
        raise ContractError(f"the {what} holds a date or time, which has no sealed form") from error
    return table, digest_bytes(sealed.encode("utf-8"))


def read_sealed_toml(
    path: str | Path, what: str, allowed_keys: Iterable[str] | None = None
) -> tuple[dict[str, Any], str]:
    """Read one configuration file once and return the table with its seal."""
    try:
        with Path(path).open("rb") as handle:
            data = handle.read(MAX_CONFIG_BYTES + 1)
    except OSError as error:
        raise ContractError(f"the {what} at {path} could not be read: {error}") from error
    return parse_sealed_toml(data, f"{what} at {path}", allowed_keys)


def require_sealed_config(
    sealed_config_digests: Mapping[str, str],
    name: str,
    observed_sha256: str,
    owner: str = "this run",
) -> None:
    """Refuse a configuration whose content changed after this run bound it.

    An absent name (never sealed) and a changed file are reported differently.
    """
    sealed = sealed_config_digests.get(name)
    if sealed is None:
        raise ContractError(
            f"{owner} sealed no digest for the {name} configuration, so the content "
            "a stage just read cannot be proven to be the one this run is bound to"
        )
    if sealed != observed_sha256:
        raise ContractError(
            f"the {name} configuration changed between this run's binding check and the "
            f"read that used it: bound {sealed}, read {observed_sha256}. A stage may not "
            "work under a policy the run never sealed"
        )
