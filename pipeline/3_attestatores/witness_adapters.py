"""Runnable native witness adapters, private to Attestatores.

The shared registry declares only names and scopes; native-boundary callables
are stage-local and resolve by exact name, with no default or near match.
``present(context, presentation)`` validates the closed ``presented`` block
with run-tree access for an adapter-owned crop; ``observe(presentation,
native_payload)`` derives the closed ``observed`` entries from that exact
image/response pair. Chandra's ``observe`` also takes ``page_size``, read off
this registry's ``takes_page_size`` rather than the adapter's name, because its
grammar reports boxes normalized against the whole sealed page rather than the
presented crop. Churro's grammar never reports geometry, so its ``observe``
returns only a ``bounds_source='presented'`` echo of the adapter's own crop.
Chandra's ``observe`` returns an empty list when its response has no
resolvable geometry; the stage, not the adapter, then adds the presentation
echo, excluded from routing and coverage.

A new adapter must move the shared declared-name set, this local mapping, any
native parser/retention dispatch in :mod:`feeding`, and its own rule in
``validate_adapter_presentation`` together.

A native float-to-pixel rule is declared beside each adapter as
``quantization``; ``None`` prevents a no-layout adapter from acquiring another
adapter's rule by omission.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Final, Mapping

import chandra
import churro
import dots
import feeding

from common import imaging_ports
from common.chairs.models import AbsentChair, ChairIdentity, ModelsConfig
from common.contracts.errors import SchemaRefusal
from common.exemplar_boundary import read_sealed_page
from common.imaging import crop_png, dimensions, resize_png_lanczos
from common.native_witness import validate_presented
from common.page_witness_units import reads_detector_records
from common.stage import SECONDARY_PROPOSER_CHAIR
from common.witness_adapters import AdapterRefusal, resolve_witness_adapter_name

_FORMAT_CAPABILITY_FIELDS: Final = frozenset({"can_express_uncertainty", "can_express_layout"})


def declared_format_capabilities(adapter: Any) -> dict[str, bool]:
    """What this adapter's own grammar can carry, validated, as a plain dict.

    Every reader of an adapter's capabilities (the live boundary, the page
    writer, the Chandra loop) calls this, so they cannot drift apart.

    Always returns a fresh ``dict`` rather than the adapter's own mapping, so a
    Testimonium never carries a value that could be mutated out from under it.
    """
    capabilities = adapter.format_capabilities
    if not isinstance(capabilities, Mapping) or set(capabilities) != _FORMAT_CAPABILITY_FIELDS:
        raise SchemaRefusal(
            f"adapter {adapter!r} declares a format_capabilities that is not the two-key "
            f"object this seam knows: {capabilities!r}"
        )
    for field in _FORMAT_CAPABILITY_FIELDS:
        if not isinstance(capabilities[field], bool):
            raise SchemaRefusal(
                f"adapter {adapter!r} declares format_capabilities.{field} as "
                f"{capabilities[field]!r}, not a boolean"
            )
    return dict(capabilities)


@dataclass(frozen=True, slots=True)
class RunnableAdapter:
    """The native-boundary operations, the grammar's capabilities and its pixel-conversion rule.

    Geometry must derive from the presented image and retained response, never
    from a different arm or presentation metadata alone. ``quantization`` is
    ``None`` where the response supplies no native geometry to convert at all.

    A served response is parsed by ``retain``, through `feeding.retain_model_view`.
    ``parse`` and ``fixture_parse`` name the same grammars for the fixture
    declaration check (`proof/test_fixture_declaration_contract.py`), which reads
    each declared answer through its own chair's adapter.
    """

    prompt: Callable[..., Any]
    parse: Callable[..., Any]
    retain: Callable[..., Any]
    present: Callable[..., Any]
    observe: Callable[..., Any]
    #: What this adapter's own output grammar can carry -- a fact about the
    #: grammar, never about a reply.
    format_capabilities: Mapping[str, bool]
    quantization: str | None = None
    #: Whether this adapter's ``observe`` accepts the sealed page's own size,
    #: because its response reports geometry normalized against the whole
    #: page rather than the presented view.
    takes_page_size: bool = False
    #: How this adapter resolves a declared framing name, or ``None`` where it
    #: has exactly one framing and there is nothing to choose.
    resolve_framing: Callable[..., str] | None = None
    #: How the committed fixture's declared bytes are read where they are not in
    #: the vendor grammar a served chair answers in; ``None`` where they are.
    fixture_parse: Callable[..., Any] | None = None


def _retain_dai_model_view(
    context: Any,
    *,
    view: dict[str, Any],
    raw_response: bytes,
    transport_stop_reason: str,
    parser: str | None = None,
    served: bool = False,
) -> dict[str, Any]:
    """Retain one DAI view without letting its registry identity be relabeled."""

    return feeding.retain_model_view(
        context,
        adapter="dai.v1",
        view=view,
        raw_response=raw_response,
        transport_stop_reason=transport_stop_reason,
        parser=parser,
        served=served,
    )


def _dai_present(context: Any, presentation: dict[str, Any]) -> dict[str, Any]:
    """Cut and resize DAI's view of one region from its sealed source page.

    The region is one record DAI's own project's detector found (a Designator
    `detector-region`). The crop is the region's axis-aligned bounds, never
    rotated. Its presentation is
    an ``adapter-crop`` so the complete crop→resize recipe remains executable in
    sealed-page space.
    """
    validate_presented(presentation)
    if presentation["kind"] != "region":
        raise SchemaRefusal("DAI accepts a region crop, not a page presentation")
    source_transform = presentation["transform"]
    page_id = source_transform["source_page_id"]
    _, page_bytes = read_sealed_page(context.tree, page_id, what="DAI")
    # Keep bounds failures as SchemaRefusals, not crop_png's bare ValueError.
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
        # Record the crop that ran, not a resampler Pillow never consulted.
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
    published = context.retain(model_image)
    return {
        "kind": "adapter-crop",
        "source_page_id": page_id,
        "source_page_ordinal": source_transform["source_page_ordinal"],
        "image_path": published["relative_path"],
        "image_sha256": published["sha256"],
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
            # A non-text response has no addressable span.
            "span": {"start": 0, "end": len(native_payload)}
            if isinstance(native_payload, str)
            else None,
        }
    ]


def _validate_whole_page_adapter_crop(
    resolved: str,
    source: dict[str, Any],
    presented: dict[str, Any],
    fit: Callable[[int, int], tuple[int, int]],
    build_transform: Callable[[str, int, dict[str, int], tuple[int, int]], dict[str, Any]],
    vendor_rule_phrase: str,
) -> None:
    """Re-derive a page-witness adapter-crop from the source presentation alone.

    Shared by Churro and Chandra: both size a whole page by their own vendor
    function and present the result as an ``adapter-crop``. Calling the
    adapter's own transform builder keeps this check from disagreeing with
    what the adapter itself writes.
    """
    if source["kind"] != "page":
        raise SchemaRefusal(f"{resolved} reads whole pages; it is never shown a crop of one")
    bounds = source["transform"]["bounds"]
    expected = build_transform(
        source["source_page_id"],
        source["source_page_ordinal"],
        bounds,
        fit(bounds["w"], bounds["h"]),
    )
    if (
        presented["kind"] != "adapter-crop"
        or presented["source_page_id"] != source["source_page_id"]
        or presented["source_page_ordinal"] != source["source_page_ordinal"]
        or presented["transform"] != expected
    ):
        raise SchemaRefusal(f"{resolved} adapter-crop is not the sealed page {vendor_rule_phrase}")


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
    if resolved == "churro.v1":
        _validate_whole_page_adapter_crop(
            resolved,
            source,
            presented,
            imaging_ports.resize_to_fit_churro,
            churro.presented_transform,
            "prepared by the vendor's own prepare_ocr_image rule",
        )
        return
    if resolved == "chandra.v1":
        _validate_whole_page_adapter_crop(
            resolved,
            source,
            presented,
            imaging_ports.scale_to_fit_chandra,
            chandra.presented_transform,
            "sized by the vendor's own scale_to_fit rule",
        )
        return
    if resolved == dots.ADAPTER:
        # The sealed page itself: dots.mocr's processor sizes it, not this stage.
        if source["kind"] != "page" or presented != source:
            raise SchemaRefusal(f"{resolved} is shown the sealed whole page exactly as sealed")
        return
    if resolved != "dai.v1":
        # A new adapter must add its own rule here (module docstring), so an
        # unrecognized name is refused rather than measured against DAI's.
        raise SchemaRefusal(
            f"adapter {resolved!r} has no presentation contract at this seam; add its own rule "
            "here beside its runnable binding"
        )
    if source["kind"] != "region":
        raise SchemaRefusal("DAI accepts a region crop, not a page presentation")
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
        format_capabilities=chandra.FORMAT_CAPABILITIES,
        fixture_parse=chandra.parse_fixture_placeholder,
    ),
    "churro.v1": RunnableAdapter(
        prompt=churro.prompt,
        resolve_framing=churro.resolve_framing,
        parse=churro.parse,
        retain=churro.retain,
        present=churro.present,
        observe=churro.observe,
        # No `quantization`, no `takes_page_size`: the grammar carries no
        # coordinates, so there is nothing to convert and no page size to use.
        format_capabilities=churro.FORMAT_CAPABILITIES,
    ),
    dots.ADAPTER: RunnableAdapter(
        prompt=dots.prompt,
        parse=dots.parse,
        retain=dots.retain,
        present=dots.present,
        observe=dots.observe,
        quantization=dots.QUANTIZATION_RULE,
        # Its boxes are in the processor's resized image; the page size maps them back.
        takes_page_size=True,
        format_capabilities=dots.FORMAT_CAPABILITIES,
    ),
    "dai.v1": RunnableAdapter(
        prompt=feeding.dai_prompt,
        parse=feeding.validate_dai_text,
        retain=_retain_dai_model_view,
        present=_dai_present,
        observe=_dai_observe,
        format_capabilities=feeding.DAI_FORMAT_CAPABILITIES,
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
    """Refuse shared declarations that have no stage-local callable route.

    Every witness reads whole pages, so a chair scoped any other way is refused.
    Also checks the roster's declared framings, since only the stage knows
    which framings an adapter declares; refused before a run opens rather than
    at the first request on a billing card.
    """

    for chair in models.witness_chairs:
        identity = models.chairs[chair]
        if isinstance(identity, AbsentChair):
            continue
        if identity.witness_scope != "page":
            raise SchemaRefusal(
                f"chair {chair!r} is scoped {identity.witness_scope!r}; every witness reads "
                "whole pages, so set its witness_scope to 'page'"
            )
        adapter = resolve_runnable_adapter(identity.witness_adapter)
        if reads_detector_records(identity) and not isinstance(
            models.chairs.get(SECONDARY_PROPOSER_CHAIR), ChairIdentity
        ):
            raise SchemaRefusal(
                f"chair {chair!r} reads the page as {identity.witness_adapter!r} does, one "
                f"record at a time as its own detector finds them, and the {SECONDARY_PROPOSER_CHAIR!r} "
                "chair that runs that detector is not configured; configure it"
            )
        declared = models.witness_framings.get(chair)
        if declared is None:
            continue
        if adapter.resolve_framing is None:
            raise SchemaRefusal(
                f"the roster asks chair {chair!r} in framing {declared!r}, but its adapter "
                f"{identity.witness_adapter!r} declares only one framing and can be asked in "
                "no other"
            )
        adapter.resolve_framing(declared)


def framing_for(models: ModelsConfig, chair: str) -> str | None:
    """The framing this run asks one chair in, resolved once, or ``None``.

    ``None`` only where the adapter has a single framing. Where it has several,
    the resolved name is returned even when the roster named none, since the
    default is as much a fact about a reading as an override is.
    """

    identity = models.chairs.get(chair)
    if not isinstance(identity, ChairIdentity):
        return None
    adapter = resolve_runnable_adapter(identity.witness_adapter)
    if adapter.resolve_framing is None:
        return None
    return adapter.resolve_framing(models.witness_framings.get(chair))
