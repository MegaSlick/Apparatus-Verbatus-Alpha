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
  ``page``, ``region``, and ``adapter-crop``: an adapter crop is an
  adapter-owned derivative and not a third witness scope; DAI is act-scoped and
  publishes one from its assigned proposal crop;
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

import feeding

from common.chairs.models import AbsentChair, ModelsConfig
from common.contracts.errors import SchemaRefusal
from common.contracts.identities import artifact_id
from common.contracts.stages import ATTESTATORES, EXEMPLAR
from common.imaging import crop_png, dimensions, resize_png_lanczos
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


def _dai_present(context: Any, presentation: dict[str, Any]) -> dict[str, Any]:
    """Cut and resize DAI's act view from its sealed source page.

    The input is the Designator proposal presentation, not pixels the adapter
    independently detected.  DAI is act-scoped; its detector/crop step is the
    proposal it was assigned, and its own presentation is an ``adapter-crop``
    so the complete crop→resize recipe remains executable in sealed-page space.
    """
    validate_presented(presentation)
    if presentation["kind"] != "region":
        raise SchemaRefusal("DAI accepts an act proposal region, not a page presentation")
    source_transform = presentation["transform"]
    page_id = source_transform["source_page_id"]
    page = context.tree.read_artifact(EXEMPLAR, "page", artifact_id(EXEMPLAR, "page", page_id))
    page_bytes = context.tree.read_bytes(page["payload"]["image_path"])
    # Re-read against the page's real size now that it is in hand. The stage
    # validates a Designator region against its sealed page before the adapter is
    # reached, so this is a second wall rather than the only one -- but a box that
    # ran off the page left here as `crop_png`'s bare ValueError, which is not a
    # refusal any caller in this seam is written to account for, and a detector
    # that proposes past the edge is exactly what Unit 9 will bring.
    validate_presented(presentation, page_size=dimensions(page_bytes))
    bounds = dict(source_transform["bounds"])
    crop = crop_png(page_bytes, bounds)
    source_width, source_height = dimensions(crop)
    target_width, target_height = feeding._dai_dimensions(source_width, source_height)
    model_transform: dict[str, Any] = {
        "operation": "crop",
        "source_page_id": page_id,
        "source_page_ordinal": source_transform["source_page_ordinal"],
        "bounds": bounds,
    }
    if (target_width, target_height) == (source_width, source_height):
        # This is an exact crop, not a resize with a filter that never ran.
        # Pillow returns a copy before consulting LANCZOS or the mode-1
        # substitution on an identity-sized call. Recording the closed crop
        # recipe states the operation that actually produced these bytes and
        # keeps a bilevel no-op byte-identical to its Designator crop.
        model_image = crop
    else:
        model_image = resize_png_lanczos(crop, target_width, target_height)
        model_transform = {
            **model_transform,
            "operation": "crop-resize-preserve-aspect",
            "resize": {
                "resampler": "pillow-lanczos",
                "dimension_rounding": "floor",
                "source_width_px": source_width,
                "source_height_px": source_height,
                "target_width_px": target_width,
                "target_height_px": target_height,
            },
        }
    digest, published = context.tree.put_blob(ATTESTATORES, model_image)
    return {
        "kind": "adapter-crop",
        "source_page_id": page_id,
        "source_page_ordinal": source_transform["source_page_ordinal"],
        "image_path": published.relative_path,
        "image_sha256": digest,
        "transform": model_transform,
    }


def _dai_observe(presentation: dict[str, Any], native_payload: Any) -> list[dict[str, Any]]:
    """Retain DAI's text unchanged; it has no published native layout channel."""
    validate_presented(presentation)
    return [
        {
            "ordinal": 0,
            "bounds": dict(presentation["transform"]["bounds"]),
            "bounds_source": "presented",
            # A failed/no-response attempt was still shown this exact image. It
            # has no retained text to address, so the presentation fallback is
            # honest but deliberately carries no span.
            "span": {"start": 0, "end": len(native_payload)}
            if isinstance(native_payload, str)
            else None,
        }
    ]


RUNNABLE_ADAPTERS: Final[dict[str, RunnableAdapter]] = {
    "churro.v1": RunnableAdapter(
        prompt=feeding.churro_prompt,
        parse=feeding.validate_churro_xml,
        retain=feeding.retain_model_view,
        present=_present,
        observe=_observe,
    ),
    "dai.v1": RunnableAdapter(
        prompt=feeding.dai_prompt,
        parse=feeding.validate_dai_text,
        retain=feeding.retain_model_view,
        present=_dai_present,
        observe=_dai_observe,
    ),
}


def resolve_runnable_adapter(name: object) -> RunnableAdapter:
    """Resolve an exact declared name to this stage's native callable shape."""

    resolved = resolve_witness_adapter_name(name)
    try:
        return RUNNABLE_ADAPTERS[resolved]
    except KeyError as error:
        raise AdapterRefusal(name, "has no runnable Attestatores adapter") from error


def validate_runnable_adapter_bindings(models: ModelsConfig) -> None:
    """Ensure this stage owns a callable route for every configured witness."""

    for chair in models.witness_chairs:
        identity = models.chairs[chair]
        if not isinstance(identity, AbsentChair):
            resolve_runnable_adapter(identity.witness_adapter)
