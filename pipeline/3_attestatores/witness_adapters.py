"""Runnable native witness adapters, private to Attestatores.

The shared registry declares only names and scopes. Native-boundary callables
remain stage-local and obey these constraints:

* adapter names resolve exactly, with no default, near match, preference, or
  callable outside this Attestatores-local registry;
* ``witness_scope`` is the occupant's invocation granularity, closed to
  ``page`` or ``act`` and sealed in its identity; it says nothing about image
  kind, geometry, region identity, or coverage;
* ``present(context, presentation)`` validates the closed ``presented`` block
  with run-tree access for an adapter-owned crop, while
  ``observe(presentation, native_payload)`` derives the closed ``observed``
  entries from that exact image/response pair. Both page-scoped adapters'
  ``observe`` also take a keyword ``page_size``: their wire contracts report
  normalized boxes, and a page witness's act view presents one crop while
  restating page-level geometry, so the sealed page's own size is the
  denominator, not the presentation's. That keyword is read off this registry
  entry (``takes_page_size``) rather than off the adapter's name: two hard-coded
  names in one branch is the third adapter's bug. Presentation kinds remain
  ``page``, ``region``, and ``adapter-crop``: an adapter crop is an
  adapter-owned derivative and not a third witness scope; DAI is act-scoped and
  publishes one from its assigned proposal crop;
* a page-scoped adapter whose response carries no layout preserves its input
  presentation and returns only a ``bounds_source='presented'`` echo. That
  source is explicitly excluded from routing and coverage; no geometry is
  fabricated from the presentation itself. It is Churro's answer to a trained
  ``<output>`` body and Chandra's to the contract's page-text form -- the honest
  no-layout fallback, not either chair's whole story;
* a new adapter must move the shared declared-name set, this local mapping, and
  any native parser/retention dispatch in :mod:`feeding` together. A failure
  while importing any callable binding propagates before ``main`` opens a run;
  there is no fallback adapter.

A native float-to-pixel rule is declared beside each adapter as
``quantization``; ``None`` prevents a no-layout adapter from acquiring another
adapter's rule by omission. Churro declares its own from Unit 12 -- the same
arithmetic Chandra's rule spells, under Churro's own name, because a rule
acquired by omission is a rule nobody declared for that chair.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Final

import chandra
import churro
import feeding

from common.chairs.models import AbsentChair, ModelsConfig
from common.contracts.canonical import digest_bytes
from common.contracts.errors import SchemaRefusal
from common.contracts.identities import artifact_id
from common.contracts.stages import ATTESTATORES, EXEMPLAR
from common.imaging import crop_png, dimensions, resize_png_lanczos
from common.native_witness import validate_presented
from common.witness_adapters import AdapterRefusal, resolve_witness_adapter_name


@dataclass(frozen=True, slots=True)
class RunnableAdapter:
    """The five native-boundary operations and optional pixel-conversion rule.

    Geometry must derive from the presented image and retained response, never
    from a different arm or presentation metadata alone. ``quantization`` is
    data recorded beside the raw digest; ``None`` means the response supplies no
    native geometry to convert at all -- an adapter whose *response* carried no
    layout on this call still returns the ``bounds_source='presented'`` echo
    coverage and routing expressly exclude, which is a fact about one body
    rather than about the adapter.

    ``takes_page_size`` says this adapter's ``observe`` accepts the sealed
    page's own size, because its response can report geometry normalized against
    the whole page rather than against the view it was handed. Declared here so
    a caller asks the registry "does this adapter take page_size?" instead of
    comparing adapter names: the name comparison worked while one adapter
    reported normalized geometry and silently excluded the second one that did.
    """

    prompt: Callable[..., Any]
    parse: Callable[..., Any]
    retain: Callable[..., Any]
    present: Callable[..., Any]
    observe: Callable[..., Any]
    quantization: str | None = None
    takes_page_size: bool = False


def _retain_dai_model_view(
    tree: Any,
    *,
    view: dict[str, Any],
    raw_response: bytes,
    transport_stop_reason: str,
    parser: str | None = None,
    served: bool = False,
) -> dict[str, Any]:
    """Retain one DAI view without letting its registry identity be relabeled."""

    return feeding.retain_model_view(
        tree,
        adapter="dai.v1",
        view=view,
        raw_response=raw_response,
        transport_stop_reason=transport_stop_reason,
        parser=parser,
        served=served,
    )


def _dai_present(context: Any, presentation: dict[str, Any]) -> dict[str, Any]:
    """Cut and resize DAI's act view from its sealed source page.

    The input is the Designator proposal presentation, not pixels the adapter
    independently detected. DAI is act-scoped; this implementation does not run
    its detector and begins its crop step from the proposal it was assigned. Its
    presentation is an ``adapter-crop`` so the complete crop→resize recipe
    remains executable in sealed-page space.
    """
    validate_presented(presentation)
    if presentation["kind"] != "region":
        raise SchemaRefusal("DAI accepts an act proposal region, not a page presentation")
    source_transform = presentation["transform"]
    page_id = source_transform["source_page_id"]
    page = context.tree.read_artifact(EXEMPLAR, "page", artifact_id(EXEMPLAR, "page", page_id))
    # The same three steps the stage's own `_verified_page_bytes` performs, and
    # they must fail the same way: a sealed record with no image path is a held
    # attempt with a reason, not a bare KeyError out of the adapter boundary.
    payload = page.get("payload")
    image_path = payload.get("image_path") if isinstance(payload, dict) else None
    if not isinstance(image_path, str) or not image_path:
        raise SchemaRefusal("DAI's sealed source page has no image path to crop")
    try:
        page_bytes = context.tree.read_bytes(image_path)
    except OSError as error:
        raise SchemaRefusal(f"DAI sealed page bytes could not be read: {error}") from error
    actual_page_digest = digest_bytes(page_bytes)
    if actual_page_digest != payload.get("source_sha256"):
        raise SchemaRefusal(
            "DAI sealed page bytes changed between artifact verification and crop use"
        )
    # Bounds failures must stay SchemaRefusals so callers can hold the attempt;
    # ``crop_png`` alone would expose a bare ValueError at this boundary.
    validate_presented(presentation, page_size=dimensions(page_bytes))
    bounds = dict(source_transform["bounds"])
    crop = crop_png(page_bytes, bounds)
    source_width, source_height = bounds["w"], bounds["h"]
    target_width, target_height = feeding.dai_dimensions(source_width, source_height)
    model_transform: dict[str, Any] = {
        "operation": "crop",
        "source_page_id": page_id,
        "source_page_ordinal": source_transform["source_page_ordinal"],
        "bounds": bounds,
    }
    if (target_width, target_height) == (source_width, source_height):
        # Identity-sized views must record the crop that ran, not a resampler
        # Pillow never consulted, and must retain the crop bytes unchanged.
        # Published into this stage's own content-addressed store rather than
        # pointed at the Designator's crop path: every image a witness is shown
        # is inventoried here, and the bytes are identical either way (both are
        # `crop_png` of the same sealed page at the same bounds), so
        # `feeding.dai_model_view`'s identity rule — which compares content,
        # not the spelling of a path — is satisfied by the digest they share.
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
            # A non-text response has no addressable span even though the image
            # presentation remains evidence for the attempt.
            "span": {"start": 0, "end": len(native_payload)}
            if isinstance(native_payload, str)
            else None,
        }
    ]


def validate_adapter_presentation(
    name: object, source: dict[str, Any], presented: dict[str, Any]
) -> None:
    """Re-derive the exact presentation recipe an adapter can produce.

    Digest re-derivation proves that ``presented`` came from the sealed page,
    but not that this configured adapter could have produced that crop and
    target. Both facts are needed when an immutable Testimonium is tallied back.
    """
    resolved = resolve_witness_adapter_name(name)
    validate_presented(source)
    validate_presented(presented)
    if resolved in {"churro.v1", "chandra.v1"}:
        # Both adapters present the exact image they were given: Churro has no
        # crop of its own, and Chandra's scope controls invocation rather than
        # presentation kind (act views keep their Designator crop, the page
        # witness view keeps the whole page).
        if presented != source:
            raise SchemaRefusal(
                f"{resolved} presentation differs from the exact image it was given"
            )
        return
    if resolved != "dai.v1":
        # Everything below is DAI's crop and resize contract. Falling through to
        # it means a fourth adapter's correct presentation is measured against
        # DAI's ceilings and refused under DAI's name, sending an operator to
        # the wrong adapter. The module docstring already says a new adapter
        # moves every dispatch site together; this makes the dispatch total so
        # the omission is named instead of mis-attributed.
        raise SchemaRefusal(
            f"adapter {resolved!r} has no presentation contract at this seam; add its own rule "
            "here beside its runnable binding"
        )
    if source["kind"] != "region":
        raise SchemaRefusal("DAI accepts an act proposal region, not a page presentation")
    bounds = source["transform"]["bounds"]
    target_width, target_height = feeding.dai_dimensions(bounds["w"], bounds["h"])
    transform: dict[str, Any] = {
        "operation": "crop",
        "source_page_id": source["source_page_id"],
        "source_page_ordinal": source["source_page_ordinal"],
        "bounds": dict(bounds),
    }
    if (target_width, target_height) != (bounds["w"], bounds["h"]):
        transform = {
            **transform,
            "operation": "crop-resize-preserve-aspect",
            "resize": {
                "resampler": "pillow-lanczos",
                "dimension_rounding": "floor",
                "source_width_px": bounds["w"],
                "source_height_px": bounds["h"],
                "target_width_px": target_width,
                "target_height_px": target_height,
            },
        }
    if (
        presented["kind"] != "adapter-crop"
        or presented["source_page_id"] != source["source_page_id"]
        or presented["source_page_ordinal"] != source["source_page_ordinal"]
        or presented["transform"] != transform
    ):
        raise SchemaRefusal(
            "DAI adapter-crop does not match its assigned proposal and sealed resize ceilings"
        )


RUNNABLE_ADAPTERS: Final[dict[str, RunnableAdapter]] = {
    "chandra.v1": RunnableAdapter(
        prompt=chandra.prompt,
        parse=chandra.parse,
        retain=chandra.retain,
        present=chandra.present,
        observe=chandra.observe,
        quantization=chandra.QUANTIZATION_RULE,
        takes_page_size=True,
    ),
    "churro.v1": RunnableAdapter(
        prompt=churro.prompt,
        parse=churro.parse,
        retain=churro.retain,
        present=churro.present,
        observe=churro.observe,
        quantization=churro.QUANTIZATION_RULE,
        takes_page_size=True,
    ),
    "dai.v1": RunnableAdapter(
        prompt=feeding.dai_prompt,
        parse=feeding.validate_dai_text,
        retain=_retain_dai_model_view,
        present=_dai_present,
        observe=_dai_observe,
    ),
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


def declared_quantization_rules() -> frozenset[str]:
    """Derive admissible rules from bindings so the schema cannot drift from them."""
    return frozenset(
        adapter.quantization
        for adapter in RUNNABLE_ADAPTERS.values()
        if adapter.quantization is not None
    )


def validate_runnable_adapter_bindings(models: ModelsConfig) -> None:
    """Refuse shared declarations that have no stage-local callable route."""

    for chair in models.witness_chairs:
        identity = models.chairs[chair]
        if not isinstance(identity, AbsentChair):
            resolve_runnable_adapter(identity.witness_adapter)
