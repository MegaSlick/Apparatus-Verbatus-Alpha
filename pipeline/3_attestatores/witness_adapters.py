"""Runnable native witness adapters, private to Attestatores.

Only names and scopes are shared; native callables stay stage-local so another
stage cannot dispatch into Attestatores. Names resolve exactly with no default
or fallback. ``prompt`` frames text and ``retain`` stores native output; neither
may be relabeled as the separate presentation or observation seams.

``RUNNABLE_ADAPTERS`` is currently a preflight agreement surface, not a dispatch
site. A new adapter must update the shared name declaration, this mapping, and
its native parser or retention dispatch together.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Final

import feeding

from common.chairs.models import AbsentChair, ModelsConfig
from common.witness_adapters import AdapterRefusal, resolve_witness_adapter_name


@dataclass(frozen=True, slots=True)
class RunnableAdapter:
    """Native operations bound by one adapter.

    Request framing and raw retention are not presentation and observation;
    those contract seams require separate bindings.
    """

    prompt: Callable[..., Any]
    parse: Callable[..., Any]
    retain: Callable[..., Any]


def _retain_churro_model_view(
    tree: Any,
    *,
    view: dict[str, Any],
    raw_response: bytes,
    transport_stop_reason: str,
    parser: str | None = None,
) -> dict[str, Any]:
    """Retain one Churro view without letting its registry identity be relabeled."""

    return feeding.retain_model_view(
        tree,
        adapter="churro.v1",
        view=view,
        raw_response=raw_response,
        transport_stop_reason=transport_stop_reason,
        parser=parser,
    )


RUNNABLE_ADAPTERS: Final[dict[str, RunnableAdapter]] = {
    "churro.v1": RunnableAdapter(
        prompt=feeding.churro_prompt,
        parse=feeding.validate_churro_xml,
        retain=_retain_churro_model_view,
    )
}


def resolve_runnable_adapter(name: object) -> RunnableAdapter:
    """Resolve an exact declared name with no runnable fallback."""

    resolved = resolve_witness_adapter_name(name)
    try:
        return RUNNABLE_ADAPTERS[resolved]
    except KeyError as error:
        raise AdapterRefusal(
            name,
            "has no runnable Attestatores binding",
            "Its shared declaration cannot execute at the native witness boundary",
            "Add the same exact name to RUNNABLE_ADAPTERS before retrying",
        ) from error


def validate_runnable_adapter_bindings(models: ModelsConfig) -> None:
    """Refuse shared declarations that have no stage-local callable route."""

    for chair in models.witness_chairs:
        identity = models.chairs[chair]
        if not isinstance(identity, AbsentChair):
            resolve_runnable_adapter(identity.witness_adapter)
