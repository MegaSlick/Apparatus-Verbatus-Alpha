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
  entries from that exact image/response pair. Chandra's ``observe`` also takes
  a keyword ``page_size``: its grammar reports boxes normalized 0-1000 against
  the sealed page, and a page witness's act view presents one crop while
  restating page-level geometry, so the sealed page's own size is the
  denominator, not the presentation's. That keyword is read off this registry
  entry (``takes_page_size``) rather than off the adapter's name: two hard-coded
  names in one branch is the third adapter's bug. Presentation kinds remain
  ``page``, ``region``, and ``adapter-crop``: an adapter crop is an
  adapter-owned derivative and not a third witness scope. DAI is act-scoped and
  publishes one from its assigned proposal crop; both page-scoped adapters
  publish one from a whole page, sized by their own vendor's preprocessing --
  Chandra's ``scale_to_fit``, Churro's ``prepare_ocr_image``;
* an adapter whose grammar reports no geometry returns only a
  ``bounds_source='presented'`` echo of the image it was shown. That source is
  explicitly excluded from routing and coverage; no geometry is fabricated from
  the presentation itself. It is Churro's whole answer -- ``HistoricalDocument``
  has no coordinate vocabulary at all -- and Chandra's is the empty list
  `run.py` gives the same echo for when a body reports no usable box;
* a new adapter must move the shared declared-name set, this local mapping, and
  any native parser/retention dispatch in :mod:`feeding` together. A failure
  while importing any callable binding propagates before ``main`` opens a run;
  there is no fallback adapter.

A native float-to-pixel rule is declared beside each adapter as
``quantization``; ``None`` prevents a no-layout adapter from acquiring another
adapter's rule by omission, and Churro's is ``None`` because its grammar
publishes no coordinates for a rule to convert.
"""

from __future__ import annotations

from dataclasses import dataclass
from types import MappingProxyType
from typing import Any, Callable, Final, Mapping

import chandra
import churro
import feeding

from common import imaging_ports
from common.chairs.models import AbsentChair, ChairIdentity, ModelsConfig
from common.contracts.errors import SchemaRefusal
from common.contracts.stages import ATTESTATORES
from common.imaging import crop_png, dimensions, resize_png_lanczos
from common.native_witness import validate_presented
from common.witness_adapters import AdapterRefusal, resolve_witness_adapter_name

#: What an adapter that has not declared its own grammar's expressiveness
#: records: the blanket value every live attempt carried before adapters could
#: declare one (`live_witness.DEFAULT_FORMAT_CAPABILITIES`,
#: `run.py::DEFAULT_FORMAT_CAPABILITIES`). Read-only, because it is a shared
#: default and a caller that mutated it would move every undeclared adapter at
#: once; each seam copies it into the record it writes.
FALLBACK_FORMAT_CAPABILITIES: Final[Mapping[str, bool]] = MappingProxyType(
    {"can_express_uncertainty": False, "can_express_layout": False}
)


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
    #: How this adapter resolves a declared framing name to the one it will be
    #: asked in, or ``None`` where it has exactly one framing and a run has
    #: nothing to choose. Declared here, like ``takes_page_size``, so a caller
    #: asks the registry rather than comparing adapter names -- and so a second
    #: multi-framing adapter cannot be forgotten at a call site that names only
    #: the first.
    resolve_framing: Callable[..., str] | None = None
    #: What this adapter's own output grammar can carry, read by the live seam
    #: (`live_witness._format_capabilities_for`) instead of the one blanket
    #: default every live attempt used to record. It is a fact about the
    #: *grammar*, never about a reply: no response reports it, and nothing here
    #: reads a response to decide it.
    #:
    #: The default is exactly that old blanket value, so an adapter that has not
    #: yet declared its own records precisely what it recorded before. Chandra
    #: and Churro declare their own here (`chandra.FORMAT_CAPABILITIES`,
    #: `churro.FORMAT_CAPABILITIES`); DAI's lands with its own unit. Both
    #: grammars that *can* carry a doubt keep `can_express_uncertainty` false
    #: until the Perlector can compare a bracket-marker view, so that a declared
    #: uncertainty never becomes a permanently uncomparable one.
    format_capabilities: Mapping[str, bool] = FALLBACK_FORMAT_CAPABILITIES
    #: How this adapter reads the committed fixture's own declared bytes, where
    #: those are not in the vendor grammar a served chair answers in. ``None``
    #: for an adapter whose fixture rows and live answers are the same shape.
    #:
    #: Chandra alone has one at this commit: `proof/skeleton_fixture.toml`'s
    #: rows declare `fixture-chandra-response.v1`, a JSON placeholder this
    #: repository invented for a fixture that asks nothing of anybody, and their
    #: bytes are pinned into the fixture's own digests until U16 re-declares
    #: them in the vendor grammar. Declared on the registry entry rather than
    #: found by adapter name, for the reason `takes_page_size` is: a caller asks
    #: the registry which reader a posture uses, and a second adapter acquiring
    #: one cannot be forgotten at a call site that names only the first.
    #:
    #: It is retained history, never a second live grammar: the retention seam
    #: refuses this reader's parser name for a served chair outright
    #: (`feeding.retain_model_view`).
    fixture_parse: Callable[..., Any] | None = None


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
    # The same three steps the stage's own `_verified_page_bytes` performs, and
    # they must fail the same way: a sealed record with no image path is a held
    # attempt with a reason, not a bare KeyError out of the adapter boundary.
    # Shared with Chandra's own crop-and-resize step, which reads a sealed page
    # for the same purpose (`feeding.sealed_page_bytes`).
    page_bytes = feeding.sealed_page_bytes(context, page_id, what="DAI")
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
    if resolved == "churro.v1":
        # Churro prepares a whole page the way `prepare_ocr_image` does -- fit
        # inside the vendor's 2,500-pixel square, then `ensure_rgb` -- and
        # presents the result as an `adapter-crop`; an act compatibility view
        # keeps its Designator crop unchanged, because no chair was shown those
        # pixels and a vendor recipe over them would record a step that never
        # ran (`churro.present`). Re-derived here from the *source* presentation
        # alone, through the same writer the adapter uses, so this seam cannot
        # come to disagree with it about a recipe.
        if source["kind"] != "page":
            if presented != source:
                raise SchemaRefusal(
                    f"{resolved} presentation differs from the exact image it was given"
                )
            return
        bounds = source["transform"]["bounds"]
        expected = churro.presented_transform(
            source["source_page_id"],
            source["source_page_ordinal"],
            bounds,
            imaging_ports.resize_to_fit_churro(bounds["w"], bounds["h"]),
        )
        if (
            presented["kind"] != "adapter-crop"
            or presented["source_page_id"] != source["source_page_id"]
            or presented["source_page_ordinal"] != source["source_page_ordinal"]
            or presented["transform"] != expected
        ):
            raise SchemaRefusal(
                "churro.v1 adapter-crop is not the sealed page prepared by the vendor's own "
                "prepare_ocr_image rule"
            )
        return
    if resolved == "chandra.v1":
        # Chandra sizes a whole page by the vendor's own `scale_to_fit` and
        # presents the result as an `adapter-crop`; an act compatibility view
        # keeps its Designator crop unchanged, because no chair was shown those
        # pixels and a vendor resize recipe over them would record a step that
        # never ran (`chandra.present`). Re-derived here from the *source*
        # presentation alone, through the same writer the adapter uses, so this
        # seam cannot come to disagree with it about a recipe.
        if source["kind"] != "page":
            if presented != source:
                raise SchemaRefusal(
                    f"{resolved} presentation differs from the exact image it was given"
                )
            return
        bounds = source["transform"]["bounds"]
        expected = chandra.presented_transform(
            source["source_page_id"],
            source["source_page_ordinal"],
            bounds,
            imaging_ports.scale_to_fit_chandra(bounds["w"], bounds["h"]),
        )
        if (
            presented["kind"] != "adapter-crop"
            or presented["source_page_id"] != source["source_page_id"]
            or presented["source_page_ordinal"] != source["source_page_ordinal"]
            or presented["transform"] != expected
        ):
            raise SchemaRefusal(
                "chandra.v1 adapter-crop is not the sealed page sized by the vendor's own "
                "scale_to_fit rule"
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
        # No `quantization` and no `takes_page_size`, and both are the same
        # fact: `HistoricalDocument` carries no coordinates anywhere, so this
        # chair reports no normalized geometry, there is nothing for a
        # float-to-pixel rule to convert, and its `observe` has no use for the
        # sealed page's own size. Unit 12 declared both for the JSON coordinate
        # channel this repository invented; the channel is retired with the
        # prompt that asked for it, and a rule kept past the geometry it
        # converted would be a rule nobody declared for what this chair now
        # does.
        format_capabilities=churro.FORMAT_CAPABILITIES,
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
    """Refuse shared declarations that have no stage-local callable route.

    Also the roster's declared framings (`ModelsConfig.witness_framings`):
    `common/chairs/config.py` checks that each names a configured witness
    chair, and this is where the *name* is checked, because only the stage
    knows which framings an adapter declares. Refused here, before a run
    opens, rather than at the first request on a billing card.
    """

    for chair in models.witness_chairs:
        identity = models.chairs[chair]
        if isinstance(identity, AbsentChair):
            continue
        adapter = resolve_runnable_adapter(identity.witness_adapter)
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

    ``None`` where the adapter has a single framing: there is then no name to
    record beyond the prompt bytes the capture already retains. Where the
    adapter has several, the resolved name is returned even when the roster
    declared none, because the default is as much a fact about a reading as an
    override is -- and a Testimonium that recorded a framing only when someone
    happened to name one would be a record you could not compare across runs.
    """

    identity = models.chairs.get(chair)
    if not isinstance(identity, ChairIdentity):
        return None
    adapter = resolve_runnable_adapter(identity.witness_adapter)
    if adapter.resolve_framing is None:
        return None
    return adapter.resolve_framing(models.witness_framings.get(chair))
