"""Runnable native witness adapters, private to Attestatores.

The shared registry in :mod:`common.witness_adapters` declares only names and
scopes. These callables cross the native model boundary and must therefore
remain in this stage, loaded as a sibling module rather than through an
importable ``pipeline`` package.

Unit 10A established the exact-name, no-fallback registry and its native
``prompt``/``parse``/``retain`` boundary. Unit 10B completes consult section
4.5's derived intake shape with ``present`` and ``observe``. A prompt constant
still does not present an image, retention is still not a derived observation,
and the five operations remain distinct.

Later units may rely on this boundary:

* adapter names resolve exactly, with no default, near match, preference, or
  callable outside this Attestatores-local registry;
* ``witness_scope`` is the occupant's invocation granularity, closed to
  ``page`` or ``act`` and sealed in its identity; it says nothing about image
  kind, geometry, region identity, or coverage;
* ``present(context, presentation)`` validates the closed ``presented`` block
  with run-tree access for an adapter-owned crop, while
  ``observe(presentation, native_payload)`` derives the closed ``observed``
  entries from that exact image/response pair. Presentation kinds remain
  ``page``, ``region``, and ``adapter-crop``: an adapter crop is a page-scoped
  occupant's own subdivision, not a third witness scope;
* the Churro fixture adapter preserves its input presentation and returns only a
  ``bounds_source='presented'`` echo because its native payload has no layout.
  That source is explicitly excluded from routing and coverage; no geometry is
  fabricated from the presentation itself;
* a new adapter must move the shared declared-name set, this local mapping, and
  any native parser/retention dispatch in :mod:`feeding` together. A failure
  while importing any callable binding propagates before ``main`` opens a run;
  there is no fallback adapter.

These statements are the Unit 10B registry handoff. A later adapter may add a
new implementation behind the same five roles, but may not merge ``present``
with ``prompt`` or ``observe`` with ``retain`` and mistake native transport for
the derived intake contract.
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
    """The native-boundary operations one local adapter supplies today.

    The slots are named for what they actually bind. ``prompt`` returns the
    request framing the occupant was trained on; ``parse`` turns one native
    response into its text; ``retain`` records the exact view and the raw bytes,
    content-addressed. ``present`` binds the exact image and executable transform;
    ``observe`` takes that presentation and the retained native response together
    to derive reading-order geometry. Geometry must never come from a different
    arm or from presentation metadata alone. The Churro fixture response carries
    no layout, so its honest fallback is a ``bounds_source='presented'`` echo that
    coverage and routing expressly exclude.

    ``quantization`` is the sixth thing an adapter declares and the one thing
    that is data rather than an operation: the exact rule name by which
    ``observe`` turned native floats into integer sealed-page pixels, recorded
    beside the raw digest in the record it produced. ``None`` is the honest
    value for an adapter whose native response carries no geometry to convert --
    Churro's does not -- and it is the default, so an adapter that has no rule
    cannot acquire one by omission. Unit 10's handoff makes this a *property of
    the adapter*, declared with it; naming it here is what stops the writing
    stage from stamping one adapter's rule onto another's record.
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
    _ = context
    validate_presented(presentation)
    return presentation


def _observe(presentation: dict[str, Any], native_payload: Any) -> list[dict[str, Any]]:
    """Derive Churro's no-layout fallback beside the response it inspected."""
    validate_presented(presentation)
    presented = presentation
    # Churro's retained text has no native geometry to extract. Keeping the
    # response in this callable's required signature prevents a later layout
    # adapter from being wired to an interface that cannot inspect its own output.
    _ = native_payload
    return [
        {
            "ordinal": 0,
            "bounds": dict(presented["transform"]["bounds"]),
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
    """Every quantization rule this stage's registry actually declares.

    The writer stamps a record with its own adapter's rule; this is what the
    record's schema is closed against. Derived from the registry rather than
    listed, so an adapter that lands with a new rule is accepted by the schema
    the moment it is bound -- and one that declares no rule cannot have a
    neighbour's stamped on it.
    """
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
