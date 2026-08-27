"""Shared witness-adapter declarations, never the runnable adapter code.

An adapter name and scope are sealed configuration facts used by every stage.
The code which frames a request, parses a response, or retains native output
belongs exclusively to Attestatores: other stages need the declared name and
scope, not a callable route into that stage.
"""

from __future__ import annotations

import sys
from typing import Final

from common.chairs.models import AbsentChair, ModelsConfig
from common.contracts.errors import ContractError, SchemaRefusal

WITNESS_SCOPES: Final = frozenset({"page", "act"})
KNOWN_WITNESS_ADAPTER_NAMES: Final = frozenset({"chandra.v1", "churro.v1", "dai.v1"})
# Adapter names are configuration keys, not model output.  The current names are
# short, and a longer spelling cannot resolve exactly; bounding it before
# whitespace scanning or set hashing keeps a malformed config from multiplying a
# large string into its refusal message.
MAX_WITNESS_ADAPTER_NAME_LENGTH: Final = 128


class AdapterRefusal(SchemaRefusal):
    """A configured witness adapter name has no declared shared binding."""

    def __init__(self, name: object, happened: str, meaning: str, next_step: str):
        self.name = name
        if type(name) is str or name is None:
            if isinstance(name, str) and len(name) > MAX_WITNESS_ADAPTER_NAME_LENGTH:
                display = f"{name[:MAX_WITNESS_ADAPTER_NAME_LENGTH]!r}... ({len(name)} characters)"
            else:
                display = repr(name)
        else:
            # Exact built-in strings are the only string values accepted by the
            # resolver.  A str subclass may override strip, hash, equality, or
            # repr; none of those hooks may replace this refusal with its own
            # exception.
            display = f"<{type(name).__name__}>"
        super().__init__(f"witness adapter {display} {happened}. {meaning}. {next_step}")


def resolve_witness_adapter_name(name: object) -> str:
    """Require one exact declared adapter name, never a default or near match."""

    if type(name) is not str or not name:
        raise AdapterRefusal(
            name,
            "is blank or not a string",
            "No exact adapter can be resolved for its chair",
            f"Set witness_adapter in the models configuration to one of "
            f"{sorted(KNOWN_WITNESS_ADAPTER_NAMES)!r}",
        )
    if len(name) > MAX_WITNESS_ADAPTER_NAME_LENGTH:
        raise AdapterRefusal(
            name,
            f"exceeds the {MAX_WITNESS_ADAPTER_NAME_LENGTH}-character name bound",
            "No exact adapter key can require an unbounded comparison or refusal message",
            f"Set witness_adapter to one of {sorted(KNOWN_WITNESS_ADAPTER_NAMES)!r}",
        )
    if not name.strip():
        raise AdapterRefusal(
            name,
            "is blank",
            "No exact adapter can be resolved for its chair",
            f"Set witness_adapter in the models configuration to one of "
            f"{sorted(KNOWN_WITNESS_ADAPTER_NAMES)!r}",
        )
    if name not in KNOWN_WITNESS_ADAPTER_NAMES:
        raise AdapterRefusal(
            name,
            "is not declared",
            "No adapter code can run for its chair",
            f"Set witness_adapter to one of {sorted(KNOWN_WITNESS_ADAPTER_NAMES)!r}, or add "
            "the new shared declaration and runnable binding together before retrying",
        )
    return name


def validate_witness_adapter_bindings(models: ModelsConfig) -> None:
    """Validate concrete model configs before any run-tree write.

    Lightweight structural model doubles are outside this preflight. Unused
    registry keys are reported rather than fatal because declarations and chair
    configuration may be deployed independently.
    """

    if not isinstance(models, ModelsConfig):
        return

    declared: set[str] = set()
    for chair in models.witness_chairs:
        identity = models.chairs[chair]
        if isinstance(identity, AbsentChair):
            continue
        if identity.witness_adapter is None:
            raise ContractError(
                f"chair {chair!r} has no witness_adapter. Its configured occupant has no native "
                "boundary to run. Add witness_adapter and witness_scope to that chair in "
                "the models configuration before starting a run"
            )
        if identity.witness_scope not in WITNESS_SCOPES:
            raise ContractError(
                f"chair {chair!r} has invalid witness_scope {identity.witness_scope!r}. Its "
                "adapter cannot determine whether to run per page or per act. Set witness_scope "
                "to exactly 'page' or 'act' before starting a run"
            )
        declared.add(resolve_witness_adapter_name(identity.witness_adapter))

    unused = sorted(KNOWN_WITNESS_ADAPTER_NAMES - declared)
    if unused:
        # Warnings can be suppressed globally while the run still succeeds;
        # stderr keeps this non-fatal finding visible to the orchestrator.
        print(
            f"witness adapter registry: {unused} declared with no configured occupant naming "
            "it. Reported, not fatal: an adapter may land before the chair that uses it",
            file=sys.stderr,
        )
