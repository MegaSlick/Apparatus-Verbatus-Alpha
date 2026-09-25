"""Churro's page-witness adapter: the vendor's own preprocessing, prompt,
message shape and output grammar, adopted verbatim and pinned by digest,
never the vendor's harness. The grammar reader lives in
`common/churro_document.py` because Churro captures are re-derived from
their retained blob at three stages, none of which may import this module.

Churro-DS carries no geometry -- one continuous reading-order text string per
page -- so this chair is asked no coordinate channel and `observe` reports
only the presented-page echo, excluded from routing and coverage. Attachment
for an act instead runs on the Perlector's `anchor-line` basis for an aligned
page witness.

Provenance: `github.com/stanford-oval/Churro` (Apache-2.0) at tag `v0.3.0` =
`4abb17386d9656199c2776195926545fc527a691` for the registry string, the image
rule and the grammar guide, and at the paper-era release
`ed09bc7fd6475c333a25427f3d0b9227af46ce27` for the harness string and the XSD;
`stanford-oval/churro-3B` at `ca2150ea465d5a3d67818c50e234b9422619c75d` for the
weights. No vendor package is installed; every carried byte is re-checked
against the vendor by `common/test_vendor_parity.py`.
"""

from __future__ import annotations

from functools import partial
from types import MappingProxyType
from typing import Any, Callable, Final, Mapping

import feeding

from common import churro_document
from common.contracts.errors import SchemaRefusal
from common.contracts.stages import ATTESTATORES
from common.imaging import convert_png_to_rgb, crop_png, dimensions, resize_png_lanczos
from common.imaging_ports import resize_to_fit_churro
from common.native_witness import parse_churro_response, validate_presented

#: `native_witness.py`'s operation name for the vendor's `prepare_ocr_image`.
PRESENT_OPERATION: Final = "churro-prepare-ocr-image.v1"
#: `prepare_ocr_image` is `ensure_rgb(resize_image_to_fit(...))` unconditionally,
#: so this adapter always converts and always says so.
PRESENT_COLOUR_MODE: Final = "rgb"

#: Declared from the vendor grammar, not assumed. `HistoricalDocument` has no
#: coordinate anywhere in its XSD, so layout is false; it does carry doubt
#: (`Illegible`, `Gap`, `Deletion`, `Addition`), so uncertainty is true -- safe
#: to declare because this chair attaches on the anchor-line basis, so an act
#: it reaches always has a comparison view and an act it doesn't stays
#: `compared: "unknown"` regardless.
FORMAT_CAPABILITIES: Final[Mapping[str, bool]] = MappingProxyType(
    {"can_express_uncertainty": True, "can_express_layout": False}
)


#: The framings this chair can be asked in, both a vendor artifact's own
#: bytes and both system-only. Derived from `churro_document.CHURRO_PROMPT_VARIANTS`
#: so a variant added there is never one this adapter silently lacks.
FRAMINGS: Final[Mapping[str, Callable[[], dict[str, str]]]] = MappingProxyType(
    {
        name: partial(churro_document.churro_prompt_view, name)
        for name in sorted(churro_document.CHURRO_PROMPT_VARIANTS)
    }
)

#: The vendor registry's current answer for this model id
#: (`providers/specs.py::resolve_ocr_profile` at tag `v0.3.0`). Which of the
#: two framings the fine-tuning saw is undocumented, so this is the attested
#: current default rather than a guess.
DEFAULT_FRAMING: Final = "registry-v0.3.0"

if DEFAULT_FRAMING not in FRAMINGS:  # pragma: no cover - import-time guard
    raise SchemaRefusal(
        f"churro.v1's default framing {DEFAULT_FRAMING!r} is not one the vendor grammar "
        f"declares ({sorted(FRAMINGS)}); a chair is never asked a question nobody carried"
    )


def resolve_framing(framing: Any = None) -> str:
    """One declared framing name, exactly, or a refusal listing the declared set.

    Selects the wording of a question before the page is read; it never
    chooses among readings, so this is not a picker (principle 1).
    """

    if framing is None:
        return DEFAULT_FRAMING
    if not isinstance(framing, str) or framing not in FRAMINGS:
        raise SchemaRefusal(
            f"churro.v1 has no framing named {framing!r}; the declared framings are "
            f"{sorted(FRAMINGS)} and a reading is never taken under a near match"
        )
    return framing


def prompt(framing: Any = None) -> dict[str, str]:
    """The vendor's system string, as `{"system": ...}` with no `user` key.

    Both attested profiles send no user text at all, so `live_witness._page_messages`
    builds an image-only user turn; recording `"user": ""` here would claim
    empty user text was sent rather than none.
    """

    return FRAMINGS[resolve_framing(framing)]()


def vendor_identity(system_prompt: Any) -> dict[str, Any] | None:
    """Which vendor commit the prompt bytes beside a reading were taken from.

    Matched against the retained system string itself, not the framing name:
    the fixture posture writes a prompt view with no framing named, and a
    record keyed on a name could pin bytes it never actually retained.
    ``None`` when the string matches no carried variant, so there is honestly
    no vendor pin for text no vendor supplied.
    """

    for name, entry in churro_document.CHURRO_PROMPT_VARIANTS.items():
        if entry["system"] == system_prompt:
            provenance = churro_document.churro_prompt_provenance(name)
            return {
                "repository": provenance["repository"],
                "sha": provenance["commit"],
                "carried_strings": {provenance["symbol"]: provenance["system_sha256"]},
            }
    return None


def parse(raw_response: bytes, *, system_prompt: str | None = None) -> Any:
    """Return the page text of a vendor grammar answer, or a named shape outcome.

    Same two return kinds as `chandra.parse`, for the same reason: a caller
    that handles one page witness handles this one. `system_prompt` is the
    exact string this request sent, since only that string may be trimmed from
    the response head (`churro_document.trim_leading_prompt`) -- trimming
    against an unsent framing could remove real transcription that happens to
    start the same way. Delegates to `common/churro_document.py` so this
    reading and the retention re-derivers can never disagree.
    """

    result = parse_churro_response(raw_response, system_prompt=system_prompt)
    if result["state"] == "parsed":
        return result["text"]
    if result["state"] == "unrecognized-shape":
        return {"parse_outcome": result["reason"]}
    raise SchemaRefusal(result["reason"])


def retain(
    tree: Any,
    *,
    view: dict[str, Any],
    raw_response: bytes,
    transport_stop_reason: str,
    parser: str | None = None,
    served: bool = False,
) -> dict[str, Any]:
    """Retain one Churro view under its own registry identity only.

    Accepts no ``adapter`` argument to pin: forwarding one would let code that
    had resolved ``churro.v1`` file this response under another chair's model
    boundary, and the retained record would then name the wrong one
    (principle 6). Chandra's and DAI's wrappers pin their names the same way.
    """
    return feeding.retain_model_view(
        tree,
        adapter="churro.v1",
        view=view,
        raw_response=raw_response,
        transport_stop_reason=transport_stop_reason,
        parser=parser,
        served=served,
    )


def present(context: Any, presentation: dict[str, Any]) -> dict[str, Any]:
    """Size and colour a whole page the way Churro's own pipeline does, and record it.

    Adapted from `src/churro_ocr/_internal/image.py::prepare_ocr_image`.
    Resize then convert-to-RGB, matching the vendor's own
    `ensure_rgb(resize_image_to_fit(...))` order: converting first would
    resample three expanded channels instead of the one the vendor resampled.
    `native_witness.py::validate_presented_page_binding` replays these same
    steps against the sealed page and refuses a digest mismatch.

    Only a whole-page presentation is transformed and recorded; a `region`
    presentation (an act view of this page witness) is returned unchanged,
    since no chair was ever shown those pixels and minting a vendor recipe
    over them would claim a step that never ran. A page already inside the
    2,500-pixel square still records the operation, since the vendor's own
    function still runs on it.
    """

    validate_presented(presentation)
    if presentation["kind"] != "page":
        return presentation
    transform = presentation["transform"]
    page_id = transform["source_page_id"]
    page_bytes = feeding.sealed_page_bytes(context, page_id, what="Churro")
    # Keep bounds failures as SchemaRefusals, not crop_png's bare ValueError.
    validate_presented(presentation, page_size=dimensions(page_bytes))
    bounds = dict(transform["bounds"])
    source_width, source_height = bounds["w"], bounds["h"]
    target_width, target_height = resize_to_fit_churro(source_width, source_height)
    resized = resize_png_lanczos(crop_png(page_bytes, bounds), target_width, target_height)
    try:
        model_image = convert_png_to_rgb(resized)
    except ValueError as error:
        # Name ensure_rgb's failure here rather than raise a bare interpreter error.
        raise SchemaRefusal(
            f"Churro's presented page cannot be converted to RGB, which is half of the vendor's "
            f"own prepare_ocr_image and cannot be recorded as having run: {error}"
        ) from error
    digest, published = context.tree.put_blob(ATTESTATORES, model_image)
    return {
        "kind": "adapter-crop",
        "source_page_id": page_id,
        "source_page_ordinal": transform["source_page_ordinal"],
        "image_path": published.relative_path,
        "image_sha256": digest,
        "transform": presented_transform(
            page_id,
            transform["source_page_ordinal"],
            bounds,
            (target_width, target_height),
        ),
    }


def presented_transform(
    page_id: str,
    page_ordinal: int,
    bounds: dict[str, int],
    target: tuple[int, int],
) -> dict[str, Any]:
    """The one transform this adapter writes, shared with the re-deriver so
    the two copies of this recipe cannot drift apart.
    """

    return {
        "operation": PRESENT_OPERATION,
        "source_page_id": page_id,
        "source_page_ordinal": page_ordinal,
        "bounds": dict(bounds),
        "colour_mode": PRESENT_COLOUR_MODE,
        "resize": {
            "resampler": "pillow-lanczos",
            "dimension_rounding": "floor",
            "source_width_px": bounds["w"],
            "source_height_px": bounds["h"],
            "target_width_px": target[0],
            "target_height_px": target[1],
        },
    }


def observe(presentation: dict[str, Any], native_payload: Any) -> list[dict[str, Any]]:
    """The no-layout echo: Churro reports no geometry, by vendor design.

    A single ``bounds_source="presented"`` entry restating the presentation,
    excluded from routing and coverage, so the record says which image this
    reading speaks for without fabricating geometry from it.

    Takes no ``page_size``: this adapter's `witness_adapters.RunnableAdapter.
    takes_page_size` is `False`, so callers never pass one -- a flag callers
    read rather than an adapter-name comparison.

    `native_payload` is part of the common adapter interface and deliberately
    unread: no property of a response turns a presentation echo into ink.
    """

    del native_payload
    validate_presented(presentation)
    return [
        {
            "ordinal": 0,
            "bounds": dict(presentation["transform"]["bounds"]),
            "bounds_source": "presented",
            "span": None,
        }
    ]
