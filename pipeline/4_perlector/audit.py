"""The sealed Pass-C audit policy, loaded and checked.

Every `page-reading` records the policy it was sealed under, as not run
(`common.perlector_audit.audit_not_run`); this checks the declaration's closed
schema before stage 4 seals it into a reading.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Final

from common.contracts.errors import ContractError
from common.perlector_audit import SCHEMA
from common.sealed_config import read_sealed_toml

_CONFIG_FIELDS: Final = frozenset(
    {"schema", "default_round_cap", "absolute_round_cap", "round_cap", "approval_ref"}
)


def load(path: str | Path) -> tuple[dict[str, Any], str]:
    policy, digest = read_sealed_toml(path, "Perlector audit declaration")
    if (
        not isinstance(policy, dict)
        or set(policy) != _CONFIG_FIELDS
        or policy.get("schema") != SCHEMA
    ):
        found = policy.get("schema") if isinstance(policy, dict) else None
        raise ContractError(
            f"the Perlector audit declaration (schema {found!r}) is not its closed schema {SCHEMA}"
        )
    numeric = ("default_round_cap", "absolute_round_cap", "round_cap")
    if any(
        not isinstance(policy.get(key), int) or isinstance(policy[key], bool) for key in numeric
    ):
        raise ContractError("the Perlector audit declaration has non-integer round caps")
    if policy["default_round_cap"] != 1 or policy["absolute_round_cap"] < 1:
        raise ContractError(
            "the Perlector audit declaration must retain default cap 1 and a positive ceiling"
        )
    if not 0 <= policy["round_cap"] <= policy["absolute_round_cap"]:
        raise ContractError("the Perlector audit round cap is outside its sealed ceiling")
    if not isinstance(policy["approval_ref"], str):
        raise ContractError("the Perlector audit approval_ref must be a string")
    if policy["round_cap"] > policy["default_round_cap"] and not policy["approval_ref"].strip():
        raise ContractError(
            "an audit round cap above the default needs the project lead's approval reference; "
            "the audit loop may not raise itself"
        )
    if policy["round_cap"] > 1:
        # `absolute_round_cap` is how far the cap may ever be raised; no pass in this
        # build runs a second round, so a higher cap would be a recorded budget nothing
        # measured.
        raise ContractError(
            "this build runs no second audit round, so an audit round cap of "
            f"{policy['round_cap']} would be sealed into every audit record without ever "
            "being run; raising it needs a multi-round pass, not only an approval reference"
        )
    return policy, digest
