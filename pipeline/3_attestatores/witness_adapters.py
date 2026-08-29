"""Runnable native witness adapters, private to Attestatores.

The shared registry declares names and scopes only; native-boundary callables
remain stage-local. Names resolve exactly with no fallback. ``prompt``,
``parse``, and ``retain`` own native transport, while ``present`` and ``observe``
own derived intake facts and may not be merged with those transport seams.

``witness_scope`` is invocation granularity, not an image or coverage claim.
Presentation kinds remain page, region, and adapter-crop. Churro has no native
layout, so its observation is only a ``bounds_source='presented'`` association,
which routing and coverage must exclude.

A new adapter must update the shared declared-name set, this mapping, and any
native parser/retention dispatch together; import or resolution failure has no
fallback path.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Final

import feeding

from common.chairs.models import AbsentChair, ModelsConfig
from common.native_witness import validate_presented
from common.witness_adapters import AdapterRefusal, resolve_witness_adapter_name


@dataclass(frozen=True, slots=True)
class RunnableAdapter:
    """The five distinct operations every local adapter supplies.

    The slots are named for what they actually bind. ``prompt`` returns the
    request framing the occupant was trained on; ``parse`` turns one native
    response into its text; ``retain`` records the exact view and the raw bytes,
    content-addressed. ``present`` binds the exact image and executable transform;
    ``observe`` takes that presentation and the retained native response together
    to derive reading-order geometry. Geometry must never come from a different
    arm or from presentation metadata alone. The Churro fixture response carries
    no layout, so its honest fallback is a ``bounds_source='presented'`` echo that
    coverage and routing expressly exclude.
    """

    prompt: Callable[..., Any]
    parse: Callable[..., Any]
    retain: Callable[..., Any]
    present: Callable[..., Any]
    observe: Callable[..., Any]


def _present(context: Any, presentation: dict[str, Any]) -> dict[str, Any]:
    """Accept a closed presentation with access to its run-tree image source.

    Churro uses the image unchanged. An adapter that owns a sub-crop needs the
    context to publish and bind those derived bytes; omitting it here would make
    the declared ``adapter-crop`` kind impossible to produce through this seam.
    """
    validate_presented(presentation)
    return presentation


def _observe(presentation: dict[str, Any], native_payload: Any) -> list[dict[str, Any]]:
    """Derive Churro's no-layout fallback beside the response it inspected.

    The shared adapter signature includes the native response because adapters
    with native layout must derive geometry from it; Churro has no such layout.
    """
    validate_presented(presentation)
    return [
        {
            "ordinal": 0,
            "bounds": dict(presentation["transform"]["bounds"]),
            "bounds_source": "presented",
            "span": None,
        }
    ]


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
        present=_present,
        observe=_observe,
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
