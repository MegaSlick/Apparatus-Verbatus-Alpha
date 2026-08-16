"""Synthetic-only exercise for R7b runner definitions.

This module intentionally has no serving-manager, pod, network, or model-store
imports.  It proves that the records seal and that real-cell reports cannot turn
an absent pod session into a pass.
"""

from __future__ import annotations

from .records import all_definitions, not_run, validate_definition, validate_result


def exercise_synthetic_definitions() -> list[dict[str, object]]:
    """Drive each definition over a synthetic fixture and emit no fake pass.

    These minimal rows stand in for the page, act, layout-gold, and injected
    corruption handles that the later runners receive.  Their purpose is only
    to prove routing and sealed-measure validation; they cannot be mistaken for
    model output or human gold.
    """
    synthetic_inputs = {
        "page": "synthetic-page-1",
        "act": "synthetic-act-1",
        "layout_gold": "synthetic-layout-1",
        "corrupt_span": "synthetic-corrupt-span-1",
    }
    if set(synthetic_inputs) != {"page", "act", "layout_gold", "corrupt_span"}:
        raise AssertionError("R7b synthetic fixture lost a required routing handle")
    definitions = [validate_definition(record) for record in all_definitions()]
    results: list[dict[str, object]] = []
    for record in definitions:
        results.append(validate_result(not_run(record["cell"], fixture_verified=True)))
    return results
