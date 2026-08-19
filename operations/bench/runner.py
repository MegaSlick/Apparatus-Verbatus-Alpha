"""Synthetic-only exercise for R7b runner definitions.

This module intentionally has no serving-manager, pod, network, or model-store
imports.  It proves that the records seal and that real-cell reports cannot turn
an absent pod session into a pass.
"""

from __future__ import annotations

from .records import all_definitions, not_run, validate_definition, validate_result


def exercise_synthetic_definitions() -> list[dict[str, object]]:
    """Validate every sealed definition and emit its visible not-run result.

    What is exercised is exactly the record path: each definition validates
    against its own seal, and each cell leaves as a ``not-run`` result bound to
    the definition digest it names.  Nothing here stands in for a page, act,
    layout-gold, or injected-corruption handle, because nothing here routes one
    anywhere — the later runners receive those.  A dict of placeholder handles
    built, compared against its own literal key set, and discarded would read in
    review as a routing check while being incapable of failing, which is the
    shape of claim this bench exists to refuse.
    """
    definitions = [validate_definition(record) for record in all_definitions()]
    return [
        validate_result(not_run(record["cell"], fixture_verified=True)) for record in definitions
    ]
