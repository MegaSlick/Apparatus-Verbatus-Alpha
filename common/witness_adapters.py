"""Shared witness-adapter declarations, never the runnable adapter code.

An adapter name and scope are sealed configuration facts used by every stage.
The code which presents a request, parses a response, or retains native output
belongs exclusively to Attestatores: other stages need the declared name and
scope, not a callable route into that stage.
"""

from __future__ import annotations

import sys
from typing import Final

from common.chairs.models import AbsentChair, ModelsConfig
from common.contracts.errors import ContractError, SchemaRefusal

WITNESS_SCOPES: Final = frozenset({"page", "act"})
KNOWN_WITNESS_ADAPTER_NAMES: Final = frozenset({"chandra.v1", "churro.v1"})


class AdapterRefusal(SchemaRefusal):
    """A configured witness adapter name has no declared shared binding."""

    def __init__(self, name: object, reason: str):
        self.name = name
        super().__init__(f"witness adapter {name!r} {reason}")


def resolve_witness_adapter_name(name: object) -> str:
    """Require one exact declared adapter name, never a default or near match."""

    if not isinstance(name, str) or not name.strip():
        raise AdapterRefusal(name, "is blank or not a string")
    if name not in KNOWN_WITNESS_ADAPTER_NAMES:
        raise AdapterRefusal(name, "is not declared")
    return name


def validate_witness_adapter_bindings(models: ModelsConfig) -> None:
    """Validate sealed witness names and scopes before any run-tree write.

    An adapter may be registered before configuration assigns it an occupant,
    so unused keys are reported without refusing the run.
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
                f"chair {chair!r} has no witness_adapter; a configured witness has no "
                "native boundary to run"
            )
        if identity.witness_scope not in WITNESS_SCOPES:
            raise ContractError(
                f"chair {chair!r} has invalid witness_scope {identity.witness_scope!r}"
            )
        declared.add(resolve_witness_adapter_name(identity.witness_adapter))

    unused = sorted(KNOWN_WITNESS_ADAPTER_NAMES - declared)
    if unused:
        # Printed, not warned. A `RuntimeWarning` is the only report in this tree
        # that an unrelated global switch can erase: `PYTHONWARNINGS=ignore` or
        # `-W ignore` deletes it and the run still exits successfully, which is
        # the shape GOVERNANCE 2 refuses. stderr is the surface every stage
        # already reports its non-fatal findings on, and the orchestrator
        # forwards a child's stderr on the normal path
        # (`pipeline/orchestrator/run.py::invoke`), so the operator who ran the
        # pipeline is told.
        print(
            f"witness adapter registry: {unused} declared with no configured occupant naming "
            "it. Reported, not fatal: an adapter may land before the chair that uses it",
            file=sys.stderr,
        )
