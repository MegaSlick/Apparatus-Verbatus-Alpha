"""Runnable native witness adapters, private to Attestatores.

The shared registry in :mod:`common.witness_adapters` declares only names and
scopes. These callables cross the native model boundary and must therefore
remain in this stage, loaded as a sibling module rather than through an
importable ``pipeline`` package.

Unit 10A deliberately deviates from consult section 4.5's literal
``present``/``parse``/``observe`` spelling. The callables available in this
slice are exactly ``prompt``/``parse``/``retain``: request-text framing, native
response parsing, and content-addressed retention of the raw response and model
view. A prompt constant does not present an image, and retention is not a
derived observation. Claiming the consult's other two names here would make the
registry say work had been built that 10B still has to do.

10B may rely on this boundary:

* adapter names resolve exactly, with no default, near match, preference, or
  callable outside this Attestatores-local registry;
* ``witness_scope`` is the occupant's invocation granularity, closed to
  ``page`` or ``act`` and sealed in its identity; it says nothing about image
  kind, geometry, region identity, or coverage;
* the intake layer remains free to add ``present`` for its closed ``presented``
  block and ``observe`` for its closed ``observed`` entries. Its presentation
  kinds remain ``page``, ``region``, and ``adapter-crop``: an adapter crop is a
  page-scoped occupant's own subdivision, not a third witness scope;
* no adapter here presents an image and the fixture path still fabricates
  Testimonia. ``RUNNABLE_ADAPTERS`` is validated at stage entry but is not yet a
  dispatch site; 10B and the adapter-owning units build that path;
* a new adapter must move the shared declared-name set, this local mapping, and
  any native parser/retention dispatch in :mod:`feeding` together. A failure
  while importing any callable binding propagates before ``main`` opens a run;
  there is no fallback adapter.

These statements are the Unit 10A handoff. In particular, later work must add
the two derived seams rather than rename ``prompt`` or ``retain`` and mistake
the native layer for the intake contract.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Final

import feeding

from common.chairs.models import AbsentChair, ModelsConfig
from common.witness_adapters import AdapterRefusal, resolve_witness_adapter_name


@dataclass(frozen=True, slots=True)
class RunnableAdapter:
    """The native-boundary operations one local adapter supplies today.

    The slots are named for what they actually bind, not for the intake
    contract they will eventually serve. ``prompt`` returns the request framing
    the occupant was trained on; ``parse`` turns one native response into its
    text; ``retain`` records the exact view and the raw bytes, content-addressed.

    Two names are deliberately *not* used here. In the intake contract a witness
    Testimonium carries a ``presented`` block (the exact blob shown, with the
    transform that derives it from the sealed page) and an ``observed`` list
    (the witness's own reading-order index, bounds, and span). Neither seam
    exists yet: ``prompt`` is only the text half of a presentation, and
    retention of raw bytes is the *native* layer, expressly not the derived
    observation layer. Binding retention to a slot called ``observe`` would give
    one word two concepts, which GLOSSARY forbids, and would leave the adapter
    that later has to produce observations with its name already taken. The
    intake slice adds ``present`` and ``observe`` to this dataclass and binds
    them per adapter; until then this registry claims only what it holds.
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
    """Resolve an exact declared name to this stage's native callable shape."""

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
    """Ensure this stage owns a callable route for every configured witness."""

    for chair in models.witness_chairs:
        identity = models.chairs[chair]
        if not isinstance(identity, AbsentChair):
            resolve_runnable_adapter(identity.witness_adapter)
