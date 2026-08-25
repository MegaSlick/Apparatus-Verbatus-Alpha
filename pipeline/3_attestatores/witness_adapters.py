"""Runnable native witness adapters, private to Attestatores.

The shared registry exposes only sealed names and scopes; native callables stay
in the stage that owns the model boundary. Names resolve exactly, without a
fallback. Every adapter keeps request framing, parsing, byte retention, image
presentation, and derived observation as separate operations so transport
evidence cannot be mistaken for reported geometry.

``witness_scope`` controls invocation granularity, not presentation kind or
coverage. ``present`` therefore receives run-tree context even when an adapter
uses the image unchanged, and ``observe`` receives the retained native payload
even when it contains no layout. A native float-to-pixel rule is declared beside
the adapter; ``None`` prevents a no-layout adapter from acquiring another
adapter's rule by omission.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Final

import chandra
import feeding

from common.chairs.models import AbsentChair, ModelsConfig
from common.native_witness import validate_presented
from common.witness_adapters import AdapterRefusal, resolve_witness_adapter_name


@dataclass(frozen=True, slots=True)
class RunnableAdapter:
    """The five native-boundary operations and optional pixel-conversion rule.

    Geometry must derive from the presented image and retained response, never
    from a different arm or presentation metadata alone. ``quantization`` is
    data recorded beside the raw digest; ``None`` means the response supplies no
    native geometry to convert.
    """

    prompt: Callable[..., Any]
    parse: Callable[..., Any]
    retain: Callable[..., Any]
    present: Callable[..., Any]
    observe: Callable[..., Any]
    quantization: str | None = None


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

    The common interface retains ``native_payload`` so layout-bearing adapters
    cannot be wired through a seam that is unable to inspect their output.
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


RUNNABLE_ADAPTERS: Final[dict[str, RunnableAdapter]] = {
    "chandra.v1": RunnableAdapter(
        prompt=chandra.prompt,
        parse=chandra.parse,
        retain=chandra.retain,
        present=chandra.present,
        observe=chandra.observe,
        quantization=chandra.QUANTIZATION_RULE,
    ),
    "churro.v1": RunnableAdapter(
        prompt=feeding.churro_prompt,
        parse=feeding.validate_churro_xml,
        retain=feeding.retain_model_view,
        present=_present,
        observe=_observe,
    ),
}


def resolve_runnable_adapter(name: object) -> RunnableAdapter:
    """Resolve an exact declared name to this stage's native callable shape."""

    resolved = resolve_witness_adapter_name(name)
    try:
        return RUNNABLE_ADAPTERS[resolved]
    except KeyError as error:
        raise AdapterRefusal(name, "has no runnable Attestatores adapter") from error


def declared_quantization_rules() -> frozenset[str]:
    """Derive admissible rules from bindings so the schema cannot drift from them."""
    return frozenset(
        adapter.quantization
        for adapter in RUNNABLE_ADAPTERS.values()
        if adapter.quantization is not None
    )


def validate_runnable_adapter_bindings(models: ModelsConfig) -> None:
    """Ensure this stage owns a callable route for every configured witness."""

    for chair in models.witness_chairs:
        identity = models.chairs[chair]
        if not isinstance(identity, AbsentChair):
            resolve_runnable_adapter(identity.witness_adapter)
