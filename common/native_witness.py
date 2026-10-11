"""The closed, derived waist of a native witness report.

Native responses remain in their retained raw blob.  These values are the small
set this pipeline derives from them: what image was presented and the page-pixel
rectangles a witness reported seeing.  They deliberately contain no act identity
or preference: correspondence is a consumer lookup, never witness testimony.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable
from typing import Any, Final

from common import chandra_layout, churro_document, dots_layout
from common.chairs.models import is_hf_revision
from common.chandra_native_retry import validate_trace as validate_chandra_native_trace
from common.contracts.canonical import digest_bytes, is_plain_int, is_sha256
from common.contracts.envelope import digest_ref, read_verified
from common.contracts.errors import SchemaRefusal
from common.contracts.serving import READER_STOP_REPETITION_LOOP, STOP_REASON_UNREPORTED
from common.contracts.stages import ATTESTATORES, writing_directory
from common.corpus_register import refuse_capture_preference
from common.imaging import (
    MAX_PIXELS,
    convert_png_to_rgb,
    crop_png,
    dimensions,
    page_sha256,
    resize_png_lanczos,
)
from common.imaging_ports import CHURRO_MAX_INLINE_IMAGE_DIM, resize_to_fit_churro
from common.request_capacity import DECLARED_ANSWER_BOUND_TOKENS

PRESENTATION_KINDS: Final = frozenset({"page", "region", "adapter-crop"})
# `native` and `derived` are reported-ink evidence. `presented` only associates
# a geometry-free response with its input image; treating it as observed ink
# would give complete coverage to a witness that reported no geometry.
BOUNDS_SOURCES: Final = frozenset({"native", "derived", "presented"})
REPORTED_BOUNDS_SOURCES: Final = frozenset({"native", "derived"})
_BOUNDS_FIELDS: Final = frozenset({"x", "y", "w", "h"})
_OBSERVED_ENTRY_FIELDS: Final = frozenset({"ordinal", "bounds", "bounds_source", "span"})
PAGE_TESTIMONIUM_REQUIRED_FIELDS: Final = frozenset(
    {
        "chair",
        "attempt_ordinal",
        "provenance",
        "format_capabilities",
        "payload",
        "witness_reported",
        "content_health",
        "presented",
        "observed",
        "scope",
        "page_ordinal",
    }
)
PAGE_TESTIMONIUM_OPTIONAL_FIELDS: Final = frozenset(
    {
        "reason",
        # Witness boxes that ran past the sealed page edge, kept as findings.
        "page_edge_overshoots",
        # Plural: a page partition may be assembled from several responses.
        "raw_response_refs",
        "adapter_metadata",
        "native_capture",
        "native_inference",
        # A chair shown several images of one page (DAI, one per record its own
        # detector found): every image in order, `presented` being the first,
        # each unit's retained model view (null where no response arrived), and
        # each unit's retained call record (null where no request was sent).
        "presentations",
        "unit_captures",
        "unit_call_refs",
        # The retained call record of the one request a whole-page chair was sent.
        "serving_call_ref",
    }
)

# Each resizing operation's rounding rule; the key set is also the set of
# operations that resize.  Chandra snaps to its 28-pixel grid, which is not `floor`.
_DIMENSION_ROUNDING: Final = {
    "crop-resize-preserve-aspect": "floor",
    "chandra-scale-to-fit.v1": "grid-28",
    "churro-prepare-ocr-image.v1": "floor",
}
# The transforms an `adapter-crop` may name.  Each is replayed from sealed page
# bytes by `validate_presented_page_binding`, so an operation absent here is
# refused rather than trusted (ARCHITECTURE invariant 3).
#
# The `.v1` operations are ports of vendor preprocessing, named after the
# function each reproduces:
#
# * `chandra-scale-to-fit.v1` -- `chandra/model/util.py::scale_to_fit` at
#   `datalab-to/chandra @ d4f7467435aa4137d9539f000ddf0b7ced3eb43f`.  Its greedy
#   trim does not preserve aspect exactly, which is harmless: Chandra's boxes
#   are mapped against the sealed page, never through this view.
# * `churro-prepare-ocr-image.v1` -- `src/churro_ocr/_internal/image.py::
#   prepare_ocr_image` at `stanford-oval/Churro @
#   4abb17386d9656199c2776195926545fc527a691`: `ensure_rgb(resize_image_to_fit(
#   img, 2500, 2500))`.
#
# One known departure: `resize_png_lanczos` promotes a bitonal (`"1"`) image to
# `"L"` because Pillow (12.3.0) silently uses NEAREST for LANCZOS on that mode.
# Churro does not, so on a bitonal page our replay differs from the vendor's
# pixels, deliberately (pinned by `test_a_bitonal_crop_replays_through_our_
# lanczos_not_the_vendors_nearest`).  Chandra converts to RGB first, so it is
# unaffected.
RESIZING_ADAPTER_CROP_OPERATIONS: Final = frozenset(_DIMENSION_ROUNDING)
ADAPTER_CROP_OPERATIONS: Final = frozenset({"crop"}) | RESIZING_ADAPTER_CROP_OPERATIONS
#: Colour conversions an adapter may record, in the words
#: `pipeline/0_triage/manifest.py::COLOUR_MODES` uses.  `keep` states that none
#: ran, which an omitted field cannot.
ADAPTER_COLOUR_MODES: Final = frozenset({"keep", "rgb"})
#: Churro must record `colour_mode`: `ensure_rgb` is half of the operation it names.
_COLOUR_MODE_REQUIRED_OPERATIONS: Final = frozenset({"churro-prepare-ocr-image.v1"})
_COLOUR_MODE_OPTIONAL_OPERATIONS: Final = frozenset({"chandra-scale-to-fit.v1"})
#: Churro's conversion is unconditional, so `keep` would be false.  Chandra's
#: `scale_to_fit` converts nothing itself (its loader does), so it is not fixed.
_COLOUR_MODE_FIXED_VALUES: Final = {"churro-prepare-ocr-image.v1": "rgb"}
#: Operations that convert colour before resizing (Chandra converts at load,
#: `chandra/input.py::load_image`; Churro after).  On `LA`/`RGBA` pages the two
#: orders give different pixels, so the replay must follow the vendor's.
_COLOUR_BEFORE_RESIZE: Final = frozenset({"chandra-scale-to-fit.v1"})
# `scale_to_fit`'s grid and maximum area.  Its 50,176-pixel minimum is not
# checked: the grid snap can land the output below it (100x80 becomes 252x196).
CHANDRA_SCALE_GRID_PX: Final = 28
CHANDRA_SCALE_MAX_PIXELS: Final = 3072 * 2048
# Re-exported from the port so the vendor's bound has one literal.
CHURRO_MAX_IMAGE_DIM_PX: Final = CHURRO_MAX_INLINE_IMAGE_DIM

# Churro's declared output bound, recorded on every request; what goes on the
# wire is `request_capacity.sendable_max_tokens`.  Source: the vendor's
# `DEFAULT_OCR_MAX_TOKENS = 25_000` (`src/churro_ocr/providers/specs.py:77` at `stanford-oval/Churro` tag
# v0.3.0, 4abb173); the CHURRO paper, section B.2, gives the reason for its size.
# (`utils/llm/models.py::COMPLETION_TOKENS_FOR_STANDARD_MODELS` in the same
# vendor repository is a context length, not a generation bound.)
CHURRO_OUTPUT_TOKENS: Final = DECLARED_ANSWER_BOUND_TOKENS["attestator_3"]
# The one intake ceiling before the parser or the repetition scan reads a byte;
# about 167 bytes per declared token, far beyond any transcription.
# `churro_document` takes it as an argument rather than declaring its own.
CHURRO_MAX_RESPONSE_BYTES: Final = 4 * 1024 * 1024
# Shortest repeating unit and minimum tail repeats.  Declared, not measured.
_REPETITION_WINDOW: Final = 24
_REPETITION_MIN_REPEATS: Final = 3


def _refuse_float(value: Any, what: str) -> None:
    if isinstance(value, float):
        raise SchemaRefusal(f"{what} carries a float; derived witness geometry is integer pixels")
    if isinstance(value, dict):
        for key, item in value.items():
            _refuse_float(item, f"{what}.{key}")
    elif isinstance(value, list):
        for index, item in enumerate(value):
            _refuse_float(item, f"{what}[{index}]")


def _bounds(value: Any, what: str, *, page_size: tuple[int, int] | None) -> dict[str, int]:
    if not isinstance(value, dict) or set(value) != _BOUNDS_FIELDS:
        raise SchemaRefusal(f"{what} is not the closed {{x, y, w, h}} page-pixel box")
    if any(isinstance(value[key], float) for key in _BOUNDS_FIELDS):
        raise SchemaRefusal(f"{what} carries a float; derived witness geometry is integer pixels")
    if not all(is_plain_int(value[key]) for key in _BOUNDS_FIELDS):
        raise SchemaRefusal(f"{what} has non-integer page-pixel coordinates")
    if value["x"] < 0 or value["y"] < 0 or value["w"] <= 0 or value["h"] <= 0:
        raise SchemaRefusal(f"{what} is not a non-empty non-negative page-pixel box")
    if page_size is not None and (
        value["x"] + value["w"] > page_size[0] or value["y"] + value["h"] > page_size[1]
    ):
        raise SchemaRefusal(f"{what} falls outside the sealed source page")
    return value


def _contains(outer: dict[str, int], inner: dict[str, int]) -> bool:
    return (
        outer["x"] <= inner["x"]
        and outer["y"] <= inner["y"]
        and outer["x"] + outer["w"] >= inner["x"] + inner["w"]
        and outer["y"] + outer["h"] >= inner["y"] + inner["h"]
    )


def _attestatores_blob_path(digest: str) -> str:
    return f"{writing_directory(ATTESTATORES)}/blobs/sha256/{digest}"


def churro_fit_target(source_width: int, source_height: int) -> tuple[int, int]:
    """The exact size `resize_image_to_fit(img, 2500, 2500)` returns for a crop.

    Calls the port rather than restating it, so the validator and the adapter
    share one copy (checked against the vendor in `common/test_vendor_parity.py`).
    The exact size matters because replay reproduces any target, including a
    stretch the vendor cannot produce.
    """
    return resize_to_fit_churro(source_width, source_height)


def _validate_resize_recipe(transform: dict[str, Any]) -> None:
    """Close the executable resize recipe, then the rules its operation names.

    Replay reproduces whatever target the record asks for, so the target must
    be constrained here.  Churro's target is held to its closed formula;
    Chandra's trim loop is not re-implemented, so its target is held only to
    the grid and maximum area, which its output always satisfies.
    """
    operation = transform["operation"]
    resize = transform["resize"]
    if not isinstance(resize, dict) or set(resize) != {
        "resampler",
        "dimension_rounding",
        "source_width_px",
        "source_height_px",
        "target_width_px",
        "target_height_px",
    }:
        raise SchemaRefusal("a resized adapter-crop has no closed resize recipe")
    if (
        resize["resampler"] != "pillow-lanczos"
        or resize["dimension_rounding"] != _DIMENSION_ROUNDING[operation]
    ):
        raise SchemaRefusal("a resized adapter-crop has an unknown executable resize recipe")
    # Malformed schema values must become a named refusal before any of the
    # identities below perform arithmetic on them.
    if not all(
        is_plain_int(resize[field]) and resize[field] > 0
        for field in (
            "source_width_px",
            "source_height_px",
            "target_width_px",
            "target_height_px",
        )
    ):
        raise SchemaRefusal("a resized adapter-crop resize dimensions are invalid")
    if resize["target_width_px"] * resize["target_height_px"] > MAX_PIXELS:
        raise SchemaRefusal(
            "a resized adapter-crop target exceeds the executable image pixel bound"
        )
    bounds = transform["bounds"]
    if resize["source_width_px"] != bounds["w"] or resize["source_height_px"] != bounds["h"]:
        raise SchemaRefusal("a resized adapter-crop resize dimensions are invalid")
    source_width, source_height = resize["source_width_px"], resize["source_height_px"]
    target_width, target_height = resize["target_width_px"], resize["target_height_px"]
    if operation == "crop-resize-preserve-aspect":
        # Hold the numbers to the name: view-to-page mapping (`_dai_observe`) is
        # only sound over a uniform scale.
        if target_height != max(1, source_height * target_width // source_width):
            raise SchemaRefusal(
                "a resized adapter-crop does not preserve the aspect its operation names"
            )
    elif operation == "chandra-scale-to-fit.v1":
        if target_width % CHANDRA_SCALE_GRID_PX or target_height % CHANDRA_SCALE_GRID_PX:
            raise SchemaRefusal(
                f"a {operation} target is not on the vendor's {CHANDRA_SCALE_GRID_PX}-pixel "
                "patch grid, so it is not a size that port can have produced"
            )
        if target_width * target_height > CHANDRA_SCALE_MAX_PIXELS:
            raise SchemaRefusal(
                f"a {operation} target of {target_width}x{target_height} px exceeds the "
                f"vendor's own maximum area of {CHANDRA_SCALE_MAX_PIXELS} px"
            )
    elif operation == "churro-prepare-ocr-image.v1":
        expected = churro_fit_target(source_width, source_height)
        if (target_width, target_height) != expected:
            raise SchemaRefusal(
                f"a {operation} target of {target_width}x{target_height} px is not the size the "
                f"vendor's fit rule produces from {source_width}x{source_height} px "
                f"({expected[0]}x{expected[1]}): the {CHURRO_MAX_IMAGE_DIM_PX}x"
                f"{CHURRO_MAX_IMAGE_DIM_PX} square, downscale-only, one isotropic scale"
            )


def validate_presented(value: Any, *, page_size: tuple[int, int] | None = None) -> dict[str, Any]:
    """Validate the exact image presentation and its page-space recipe."""
    if not isinstance(value, dict):
        raise SchemaRefusal("a Testimonium presented block is not an object")
    kind = value.get("kind")
    required = {
        "kind",
        "source_page_id",
        "source_page_ordinal",
        "image_path",
        "image_sha256",
        "transform",
    }
    if kind == "region":
        required.add("region_ref")
    if set(value) != required:
        raise SchemaRefusal("a Testimonium presented block is not its closed kind-specific schema")
    if not isinstance(kind, str) or kind not in PRESENTATION_KINDS:
        raise SchemaRefusal("a Testimonium presented block has an unknown presentation kind")
    if (
        not isinstance(value["source_page_id"], str)
        or not value["source_page_id"]
        or not is_plain_int(value["source_page_ordinal"])
        or value["source_page_ordinal"] < 1
        or not isinstance(value["image_path"], str)
        or not value["image_path"]
        or not is_sha256(value["image_sha256"])
    ):
        raise SchemaRefusal("a Testimonium presented block has invalid source or blob identity")
    transform = value["transform"]
    if not isinstance(transform, dict):
        raise SchemaRefusal("a Testimonium presented block has no complete page transform")
    # Normalized first: an unhashable value would raise TypeError in the
    # frozenset lookups below instead of a named refusal.
    operation = transform.get("operation")
    if not isinstance(operation, str):
        operation = None
    required_transform_fields = {
        "operation",
        "source_page_ordinal",
        "source_page_id",
        "bounds",
    }
    optional_transform_fields: set[str] = set()
    if operation in RESIZING_ADAPTER_CROP_OPERATIONS:
        required_transform_fields.add("resize")
    if operation in _COLOUR_MODE_REQUIRED_OPERATIONS:
        required_transform_fields.add("colour_mode")
    elif operation in _COLOUR_MODE_OPTIONAL_OPERATIONS:
        optional_transform_fields.add("colour_mode")
    transform_fields = set(transform)
    if not (
        required_transform_fields <= transform_fields
        and transform_fields <= required_transform_fields | optional_transform_fields
    ):
        raise SchemaRefusal("a Testimonium presented block has no complete page transform")
    if (
        not isinstance(transform["operation"], str)
        or transform["source_page_id"] != value["source_page_id"]
        or transform["source_page_ordinal"] != value["source_page_ordinal"]
    ):
        raise SchemaRefusal("a Testimonium presented transform disagrees with its source page")
    _bounds(transform["bounds"], "a Testimonium presented transform", page_size=page_size)
    if "colour_mode" in transform and (
        not isinstance(transform["colour_mode"], str)
        or transform["colour_mode"] not in ADAPTER_COLOUR_MODES
    ):
        raise SchemaRefusal(
            "a Testimonium presented transform names an unknown colour conversion; the exact "
            f"image the chair saw cannot be replayed from it (known modes "
            f"{sorted(ADAPTER_COLOUR_MODES)})"
        )
    fixed_colour_mode = _COLOUR_MODE_FIXED_VALUES.get(operation)
    if fixed_colour_mode is not None and transform.get("colour_mode") != fixed_colour_mode:
        raise SchemaRefusal(
            f"a {operation} presentation records colour_mode "
            f"{transform.get('colour_mode')!r}; the vendor's own operation performs "
            f"{fixed_colour_mode!r} unconditionally, so any other answer records a half of it "
            "that did not run"
        )
    if operation in RESIZING_ADAPTER_CROP_OPERATIONS:
        # Only an adapter-crop is replayed, so a resize anywhere else would never
        # be checked (ARCHITECTURE invariant 3).
        if kind != "adapter-crop":
            raise SchemaRefusal(
                f"a {kind!r} presentation names the resizing operation {operation!r}; only an "
                "adapter-crop is re-derived from its sealed page, so the resize would never be "
                "checked against the image it claims to describe"
            )
        _validate_resize_recipe(transform)
    if kind == "region":
        ref = value["region_ref"]
        if (
            not isinstance(ref, dict)
            or set(ref) != {"region_id"}
            or not isinstance(ref["region_id"], str)
            or not ref["region_id"]
        ):
            raise SchemaRefusal("a region presentation has no closed region_ref")
    _refuse_float(value, "a Testimonium presented block")
    return value


def validate_observed(
    value: Any,
    *,
    presented: dict[str, Any],
    page_size: tuple[int, int] | None = None,
    retained_text: Any = None,
    unit_presentations: list[dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    """Validate dense witness order, source-page boxes, and non-overlapping text spans.

    A span addresses this Testimonium's exact retained string in code points,
    never the normalized alignment view. It may be null, but it may not name an
    offset the record cannot answer, so every span stays checkable against
    the record. A record shown one image per unit passes `unit_presentations`,
    and each box is checked against its own unit's image, not the first.
    """
    if not isinstance(value, list):
        raise SchemaRefusal("a Testimonium observed block is not a list")
    if unit_presentations is not None and len(unit_presentations) != len(value):
        raise SchemaRefusal(
            "a page Testimonium shown several images does not report one box per image"
        )
    spans: list[tuple[int, int]] = []
    for index, item in enumerate(value):
        shown = presented if unit_presentations is None else unit_presentations[index]
        if not isinstance(item, dict) or set(item) != _OBSERVED_ENTRY_FIELDS:
            raise SchemaRefusal("a Testimonium observed entry is not its closed schema")
        if not is_plain_int(item["ordinal"]) or item["ordinal"] != index:
            raise SchemaRefusal("a Testimonium observed ordinals are not dense, unique, 0-based")
        if (
            not isinstance(item["bounds_source"], str)
            or item["bounds_source"] not in BOUNDS_SOURCES
        ):
            raise SchemaRefusal("a Testimonium observed box has an unknown bounds_source")
        if item["bounds_source"] == "presented" and item["bounds"] != shown["transform"]["bounds"]:
            raise SchemaRefusal(
                "a presented-source observed box differs from the presented transform"
            )
        bounds = _bounds(item["bounds"], "a Testimonium observed box", page_size=page_size)
        if not _contains(shown["transform"]["bounds"], bounds):
            raise SchemaRefusal(
                "a Testimonium observed box falls outside the exact image presentation. "
                "The record would attribute unseen page pixels to this witness. Correct the "
                "adapter's page-space transform or refuse this response"
            )
        span = item["span"]
        if span is not None:
            if (
                not isinstance(span, dict)
                or set(span) != {"start", "end"}
                or not is_plain_int(span["start"])
                or not is_plain_int(span["end"])
                or span["start"] < 0
                or span["end"] < span["start"]
            ):
                raise SchemaRefusal(
                    "a Testimonium observed span is not a non-negative [start, end) span"
                )
            if not isinstance(retained_text, str):
                raise SchemaRefusal(
                    "a Testimonium observed span addresses text this record does not retain"
                )
            if span["end"] > len(retained_text):
                raise SchemaRefusal(
                    "a Testimonium observed span runs past the end of its own retained text"
                )
            spans.append((span["start"], span["end"]))
    ordered_spans = sorted(spans)
    for previous, current in zip(ordered_spans, ordered_spans[1:], strict=False):
        if current[0] < previous[1]:
            raise SchemaRefusal("a Testimonium observed spans overlap")
    _refuse_float(value, "a Testimonium observed block")
    return value


def record_presentations(payload: dict[str, Any]) -> list[dict[str, Any]]:
    """Every image one Testimonium's chair was shown, in order.

    A record shown several images lists them all in `presentations`; any other
    record was shown its one `presented` image, or nothing.
    """
    if "presentations" in payload:
        presentations = payload["presentations"]
        if not isinstance(presentations, list):
            raise SchemaRefusal("a Testimonium's presentations is not a list")
        return list(presentations)
    presented = payload.get("presented")
    return [presented] if presented else []


def _validate_presentations(
    payload: dict[str, Any], *, page_size: tuple[int, int] | None
) -> list[dict[str, Any]]:
    """A several-image page record: first equals `presented`, all crops of one page."""
    presentations = payload["presentations"]
    if payload.get("scope") != "page" or not isinstance(presentations, list) or not presentations:
        raise SchemaRefusal(
            "a Testimonium's presentations belong only to a page record shown at least one image"
        )
    if presentations[0] != payload.get("presented"):
        raise SchemaRefusal("a page Testimonium's presented image is not its first presentation")
    for presentation in presentations:
        validate_presented(presentation, page_size=page_size)
        if (
            presentation["kind"] != "adapter-crop"
            or presentation["source_page_id"] != (presentations[0]["source_page_id"])
        ):
            raise SchemaRefusal(
                "a page Testimonium's presentations are not adapter crops of its one page"
            )
    return presentations


def validate_native_witness_geometry(
    payload: Any, *, page_size: tuple[int, int] | None = None
) -> dict[str, Any]:
    """Validate the two derived blocks and recursively refuse preference claims.

    An empty pair records that the chair was never shown an image (refused
    pages, absent chairs).
    """
    if not isinstance(payload, dict):
        raise SchemaRefusal("a Testimonium payload is not an object")
    refuse_capture_preference(payload, what="a Testimonium")
    presented = payload.get("presented")
    observed = payload.get("observed")
    if presented == {}:
        if observed != [] or "presentations" in payload:
            raise SchemaRefusal("an unpresented Testimonium must carry an empty observed block")
        return payload
    presented = validate_presented(presented, page_size=page_size)
    unit_presentations = (
        _validate_presentations(payload, page_size=page_size)
        if "presentations" in payload
        else None
    )
    validate_observed(
        observed,
        presented=presented,
        page_size=page_size,
        retained_text=payload.get("payload"),
        unit_presentations=unit_presentations,
    )
    return payload


def _replay_colour_mode(transform: dict[str, Any], derived: bytes) -> bytes:
    """Run the recorded colour step, or nothing at all where none was recorded."""
    if transform.get("colour_mode") != "rgb":
        return derived
    try:
        return convert_png_to_rgb(derived)
    except ValueError as error:
        raise SchemaRefusal(
            f"an adapter-crop presentation's colour conversion cannot be replayed from "
            f"its sealed page ({error})"
        ) from error


# The sha256 each adapter-crop transform re-derives from a page, by the page's
# sha256 and the transform. The derivation is a pure function of the two, and one
# process asks for the same one several times: several stages' checks, and the
# presentations of one page to several chairs. A derivation that refused is
# never held, so it refuses again.
_DERIVED_SHA256: dict[tuple[str, str], str] = {}


def transform_key(transform: dict[str, Any]) -> str:
    """The name a transform is held under: its JSON with sorted keys."""
    return json.dumps(transform, sort_keys=True)


def derived_presentation_sha256(page_bytes: bytes, transform: dict[str, Any]) -> str:
    """The sha256 of the image an adapter-crop transform cuts and replays from `page_bytes`."""
    key = (page_sha256(page_bytes), transform_key(transform))
    held = _DERIVED_SHA256.get(key)
    if held is None:
        held = digest_bytes(_derive_adapter_crop(page_bytes, transform))
        _DERIVED_SHA256[key] = held
    return held


def remember_derived_presentation(page_digest: str, key: str, sha256: str) -> None:
    """Hold a derivation another process made from bytes with sha256 `page_digest`."""
    _DERIVED_SHA256[(page_digest, key)] = sha256


def _derive_adapter_crop(page_bytes: bytes, transform: dict[str, Any]) -> bytes:
    operation = transform["operation"]
    derived = crop_png(page_bytes, transform["bounds"])
    colour_before_resize = operation in _COLOUR_BEFORE_RESIZE
    if colour_before_resize:
        derived = _replay_colour_mode(transform, derived)
    if operation in RESIZING_ADAPTER_CROP_OPERATIONS:
        resize = transform["resize"]
        # A colour step cannot change dimensions, so this holds either order.
        if dimensions(derived) != (resize["source_width_px"], resize["source_height_px"]):
            raise SchemaRefusal("a resized adapter-crop recipe disagrees with its sealed crop")
        derived = resize_png_lanczos(derived, resize["target_width_px"], resize["target_height_px"])
    if not colour_before_resize:
        derived = _replay_colour_mode(transform, derived)
    return derived


def validate_presented_page_binding(
    presented: dict[str, Any],
    *,
    page_ordinal: int,
    page_image_path: str,
    page_sha256: str,
    page_size: tuple[int, int],
    page_bytes: bytes | None = None,
) -> None:
    """Bind a presentation to the sealed page it names.

    Otherwise a record could name page 1 while carrying page 2's pixels,
    breaking the trace back to the source page. An adapter-crop must
    re-derive its digest from the sealed page bytes.
    """
    kind = presented["kind"]
    whole_page = {"x": 0, "y": 0, "w": page_size[0], "h": page_size[1]}
    if presented["source_page_ordinal"] != page_ordinal:
        raise SchemaRefusal(
            "a presentation's source page ordinal disagrees with the sealed page its id names"
        )
    if kind == "page":
        if presented["image_path"] != page_image_path or presented["image_sha256"] != page_sha256:
            raise SchemaRefusal(
                "a page presentation names a blob that is not the sealed page it claims to be"
            )
        if presented["transform"]["bounds"] != whole_page:
            raise SchemaRefusal(
                "a page presentation does not cover its whole sealed page; a partial image is "
                "an adapter-crop, not a page"
            )
        if presented["transform"]["operation"] != "whole":
            raise SchemaRefusal("a page presentation has no executable whole-page transform")
    elif kind == "adapter-crop":
        bounds = presented["transform"]["bounds"]
        if presented["image_sha256"] == page_sha256 and bounds != whole_page:
            raise SchemaRefusal(
                "an adapter-crop presentation carries the whole sealed page's blob under a "
                "sub-page transform"
            )
        operation = presented["transform"]["operation"]
        if operation not in ADAPTER_CROP_OPERATIONS:
            raise SchemaRefusal(
                "an adapter-crop presentation has no executable sealed-page crop transform"
            )
        if page_bytes is None:
            raise SchemaRefusal(
                "an adapter-crop presentation cannot be re-derived without its sealed page bytes"
            )
        expected_sha256 = derived_presentation_sha256(page_bytes, presented["transform"])
        if presented["image_sha256"] != expected_sha256:
            raise SchemaRefusal(
                "an adapter-crop presentation blob does not re-derive from its sealed page transform"
            )


def _truncation_from_stop_word(transport_stop_reason: str) -> tuple[bool | None, str]:
    """Truncated, not truncated, or unknown when the engine reported no stop reason.

    An unreported stop is never recorded as a natural one.
    """

    if transport_stop_reason == STOP_REASON_UNREPORTED:
        return None, "not-recorded"
    return transport_stop_reason in _CHURRO_CUTOFF_STOP_REASONS, "trusted-response-boundary"


def cut_off_basis(transport_stop_reason: str, parse_reason: str) -> str:
    """The health basis of a cut-off response that did not parse: who cut it, and why
    it is unread. Shared by the writer and this validator so the two cannot drift."""

    who = (
        "stopped by the client on a repetition loop"
        if transport_stop_reason == READER_STOP_REPETITION_LOOP
        else "cut off by the provider"
    )
    return f"response {who} ({transport_stop_reason!r}); {parse_reason}"


def validate_page_testimonium_payload(
    payload: Any,
    *,
    testimonium_id: str | None = None,
    read_bytes: Callable[[str], bytes] | None = None,
) -> dict[str, Any]:
    """Close the page-scoped native Testimonium at writer and consumer seams."""
    if not isinstance(payload, dict) or not (
        set(payload) <= PAGE_TESTIMONIUM_REQUIRED_FIELDS | PAGE_TESTIMONIUM_OPTIONAL_FIELDS
    ):
        raise SchemaRefusal("a page Testimonium is not its closed schema")
    if missing := sorted(PAGE_TESTIMONIUM_REQUIRED_FIELDS - set(payload)):
        raise SchemaRefusal(f"a page Testimonium lacks required field(s) {missing}")
    if (
        payload["scope"] != "page"
        or not is_plain_int(payload["page_ordinal"])
        or payload["page_ordinal"] < 1
    ):
        raise SchemaRefusal("a page Testimonium has invalid page scope facts")
    validated = validate_native_witness_geometry(payload)
    # The page spoken for and the page shown must agree, or a consumer keying on
    # `page_ordinal` reads another page's geometry, breaking the trace back
    # to the source page.
    presented = payload["presented"]
    if presented and presented["source_page_ordinal"] != payload["page_ordinal"]:
        raise SchemaRefusal(
            "a page Testimonium's presentation names a different page than the record. Its "
            "observed geometry would be attributed to ink the chair was never shown. Restore "
            "the page ordinal of the presentation actually served"
        )
    if "page_edge_overshoots" in payload:
        if not presented:
            raise SchemaRefusal(
                "a page Testimonium with no presentation names page-edge findings; a chair "
                "shown nothing reported no box to reject"
            )
        _validate_page_edge_overshoot_response_refs(
            validate_page_edge_overshoots(payload["page_edge_overshoots"]),
            payload.get("raw_response_refs"),
        )
    if "native_capture" in payload:
        capture = validate_native_capture(payload["native_capture"])
        if capture["adapter"] == "churro.v1":
            _validate_churro_page_health(payload, capture)
        elif capture["adapter"] == dots_layout.ADAPTER:
            _validate_churro_page_health(payload, capture, label="dots.mocr")
    if "native_inference" in payload:
        _validate_chandra_native_inference(payload)
    if "unit_captures" in payload:
        _validate_unit_captures(payload)
    elif "presentations" in payload:
        raise SchemaRefusal("a page Testimonium shown several images names no unit captures")
    if "unit_call_refs" in payload:
        _validate_unit_call_refs(payload, read_bytes)
    elif "presentations" in payload:
        raise SchemaRefusal("a page Testimonium shown several images names no unit call records")
    if "serving_call_ref" in payload:
        _validate_serving_call_ref(payload, read_bytes)
    validate_retained_response_refs(payload, read_bytes=read_bytes)
    return validated


def _validate_unit_captures(payload: dict[str, Any]) -> None:
    """One retained model view per presentation, each bound to a retained response."""
    captures = payload["unit_captures"]
    presentations = payload.get("presentations")
    if (
        not isinstance(presentations, list)
        or not isinstance(captures, list)
        or len(captures) != len(presentations)
    ):
        raise SchemaRefusal("a page Testimonium names unit captures that are not one per image")
    retained = payload.get("raw_response_refs") or []
    for capture in captures:
        if capture is None:
            continue
        checked = validate_native_capture(capture)
        if checked["raw_response_ref"] not in retained:
            raise SchemaRefusal(
                "a page Testimonium unit capture names a response the record does not retain"
            )


def _validate_unit_call_refs(
    payload: dict[str, Any], read_bytes: Callable[[str], bytes] | None
) -> None:
    """One retained call record per presentation, each a closed Attestatores blob."""
    references = payload["unit_call_refs"]
    presentations = payload.get("presentations")
    if (
        not isinstance(presentations, list)
        or not isinstance(references, list)
        or len(references) != len(presentations)
    ):
        raise SchemaRefusal("a page Testimonium names unit call records that are not one per image")
    for reference in references:
        if reference is None:
            continue
        digest_ref(reference, "a page Testimonium unit call record reference")
        if reference["relative_path"] != _attestatores_blob_path(reference["sha256"]):
            raise SchemaRefusal(
                "a page Testimonium unit call record reference is not a closed blob reference"
            )
        if read_bytes is not None:
            read_verified(read_bytes, reference, "page Testimonium unit call record")


def _validate_serving_call_ref(
    payload: dict[str, Any], read_bytes: Callable[[str], bytes] | None
) -> None:
    """One retained call record for a chair sent the whole page in one request."""
    if "unit_call_refs" in payload:
        raise SchemaRefusal(
            "a page Testimonium names both one serving call and a call per image; a chair "
            "is sent either the whole page or one request per image"
        )
    reference = payload["serving_call_ref"]
    digest_ref(reference, "a page Testimonium serving call record reference")
    if reference["relative_path"] != _attestatores_blob_path(reference["sha256"]):
        raise SchemaRefusal(
            "a page Testimonium serving call record reference is not a closed blob reference"
        )
    if read_bytes is not None:
        read_verified(read_bytes, reference, "page Testimonium serving call record")


def _validate_churro_page_health(
    payload: dict[str, Any], capture: dict[str, Any], *, label: str = "Churro"
) -> None:
    """A whole-page capture's record agrees with its parse: text, health and reason.

    Churro's rule, and dots.mocr's (`label`), whose page records are written
    the same way: the parsed text is the page text, the health is measured
    from it and the stop word, and an unread or cut-off empty answer names why.
    """
    parse = capture["parse"]
    parsed_text = parse.get("text")
    if parse["state"] == "parsed":
        if payload["payload"] != parsed_text:
            raise SchemaRefusal(
                f"a {label} page Testimonium payload differs from its parsed native capture"
            )
        truncated, truncation_basis = _truncation_from_stop_word(capture["transport_stop_reason"])
        expected_health = {
            "native_type": "string",
            "encoding": "utf-8-json-native",
            "recordable": True,
            "empty": parsed_text == "",
            "blank": parsed_text.strip() == "",
            "truncated": truncated,
            "characters": len(parsed_text),
            "truncation_basis": truncation_basis,
        }
        if payload["content_health"] != expected_health:
            raise SchemaRefusal(
                f"a {label} page Testimonium health differs from its parsed native capture"
            )
        # An empty reading is a confirmed blank only when the model
        # positively finished; cut off or unreported needs a reason.
        interrupted_silence = truncated is not False and parsed_text == ""
        if interrupted_silence:
            if not (isinstance(payload.get("reason"), str) and payload["reason"].strip()):
                raise SchemaRefusal(
                    f"a cut-off empty {label} page capture has no failed-attempt reason"
                )
        elif "reason" in payload:
            raise SchemaRefusal(f"a usable {label} page capture carries a failed-attempt reason")
    else:
        if payload["payload"] is not None:
            raise SchemaRefusal(f"an unread {label} page capture claims retained page text")
        cut_off = capture["transport_stop_reason"] in _CHURRO_CUTOFF_STOP_REASONS
        # The same helper the writer uses, so the two cannot disagree.
        parse_refusal = native_parse_refusal(parse)
        basis = (
            cut_off_basis(capture["transport_stop_reason"], parse_refusal)
            if cut_off
            else parse_refusal
        )
        expected_health = {
            "native_type": "unrecordable",
            "encoding": "invalid-or-unrecordable",
            "recordable": False,
            "empty": None,
            "blank": None,
            "truncated": None,
            "characters": None,
            "truncation_basis": basis,
        }
        if payload["content_health"] != expected_health:
            raise SchemaRefusal(
                f"an unparseable {label} page Testimonium health differs from its capture"
            )
        reason = payload.get("reason")
        if not isinstance(reason, str) or not reason.strip() or parse_refusal not in reason:
            raise SchemaRefusal(
                f"an unparseable {label} page capture has no reason naming its parser refusal"
            )


def _validate_chandra_native_inference(payload: dict[str, Any]) -> None:
    provenance = payload.get("provenance")
    identity = provenance.get("resolved_identity") if isinstance(provenance, dict) else None
    if (
        payload.get("chair") != "attestator_1"
        or not isinstance(identity, dict)
        or identity.get("role") != "attestator_1"
        or identity.get("witness_adapter") != "chandra.v1"
        or identity.get("witness_scope") != "page"
    ):
        raise SchemaRefusal(
            "Chandra native inference provenance belongs only to page-scoped "
            "attestator_1 with chandra.v1"
        )
    capture = payload.get("native_capture")
    if capture is not None and capture.get("adapter") != "chandra.v1":
        raise SchemaRefusal(
            "Chandra native inference provenance names a non-Chandra native capture"
        )
    validate_chandra_native_trace(payload["native_inference"])


def validate_retained_response_refs(
    payload: dict[str, Any], *, read_bytes: Callable[[str], bytes] | None = None
) -> None:
    """Close a page partition's links to its retained native responses.

    Quantization metadata is refused without the bytes it describes.
    """
    refs = payload.get("raw_response_refs")
    if refs is not None:
        if not isinstance(refs, list) or not refs:
            raise SchemaRefusal("a page Testimonium raw_response_refs is not a non-empty list")
        for reference in refs:
            digest_ref(reference, "a page Testimonium retained-response reference")
            if reference["relative_path"] != _attestatores_blob_path(reference["sha256"]):
                raise SchemaRefusal(
                    "a page Testimonium retained-response reference is not a closed blob reference"
                )
        if len({reference["sha256"] for reference in refs}) != len(refs):
            raise SchemaRefusal("a page Testimonium names one retained response twice")
        if read_bytes is not None:
            for reference in refs:
                read_verified(read_bytes, reference, "page Testimonium retained response")
    metadata = payload.get("adapter_metadata")
    if metadata is not None:
        if (
            not isinstance(metadata, dict)
            or set(metadata) != {"geometry_quantization"}
            or not isinstance(metadata["geometry_quantization"], str)
            or not metadata["geometry_quantization"]
        ):
            raise SchemaRefusal("a page Testimonium adapter metadata is not its closed shape")
        if refs is None:
            raise SchemaRefusal(
                "a page Testimonium declares a quantization rule with no retained response to "
                "have applied it to"
            )


def split_page_edge_overshoots(
    observed: list[dict[str, Any]], *, page_size: tuple[int, int]
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Separate, never clamp, native boxes that run past a sealed page edge.

    Clamping would report a different box.  One bad box says nothing about the
    response's other blocks, so the rest survive with dense ordinals and the
    overshoot is kept as a finding with its exact bounds.
    """
    if (
        not isinstance(page_size, tuple)
        or len(page_size) != 2
        or not all(is_plain_int(value) and value > 0 for value in page_size)
    ):
        raise SchemaRefusal(
            "the sealed page edge has no positive integer dimensions. "
            "Out-of-page geometry cannot be distinguished from valid geometry without that edge. "
            "Restore the sealed page dimensions and derive the observations again."
        )
    survivors: list[dict[str, Any]] = []
    overshoots: list[dict[str, Any]] = []
    page_bounds = {"x": 0, "y": 0, "w": page_size[0], "h": page_size[1]}
    for source_ordinal, item in enumerate(observed):
        if not isinstance(item, dict) or set(item) != _OBSERVED_ENTRY_FIELDS:
            raise SchemaRefusal(
                "the page-edge check received an observed entry outside its closed schema. "
                "The rejected box could lose facts when converted into a finding. "
                "Restore the complete observed entry and run the page-edge derivation again."
            )
        if not is_plain_int(item["ordinal"]) or item["ordinal"] != source_ordinal:
            raise SchemaRefusal(
                "the page-edge check received observed ordinals that are not dense, unique, "
                "and 0-based. The response order of a rejected box is therefore ambiguous. "
                "Re-derive the observation list in the response's original order."
            )
        if (
            not isinstance(item["bounds_source"], str)
            or item["bounds_source"] not in REPORTED_BOUNDS_SOURCES
        ):
            raise SchemaRefusal(
                "the page-edge check received a box that is not reported witness geometry. "
                "A presentation echo cannot become a witness page-edge finding. "
                "Keep only native or derived witness boxes in this derivation."
            )
        bounds = _bounds(item["bounds"], "a page-edge observed box", page_size=None)
        if bounds["x"] + bounds["w"] > page_size[0] or bounds["y"] + bounds["h"] > page_size[1]:
            if item["span"] is not None:
                raise SchemaRefusal(
                    "the page-edge check received an out-of-page box with a text span. "
                    "The finding schema cannot retain that span, so converting it would lose "
                    "evidence. Retain the span in a supported record or remove it at the "
                    "observation producer."
                )
            overshoots.append(
                {
                    "kind": "page-edge-overshoot",
                    "ordinal": item["ordinal"],
                    "bounds": dict(bounds),
                    "sealed_page_bounds": dict(page_bounds),
                }
            )
        else:
            survivors.append({**item, "ordinal": len(survivors), "bounds": dict(bounds)})
    return survivors, overshoots


_NATIVE_CAPTURE_FIELDS: Final = frozenset(
    {
        "schema",
        "adapter",
        "view",
        "raw_response_ref",
        "transport_stop_reason",
        "stop_reason",
        "findings",
        "parse",
    }
)
# `vendor_identity` is the vendor pin a chair's request carried, where it
# carries one. `text_view` names the view a vendor grammar's parse was read
# under; only an adapter and parser in `CAPTURE_TEXT_VIEWS` has one, and there it
# is required whenever the bytes are re-derived.
_NATIVE_CAPTURE_OPTIONAL_FIELDS: Final = frozenset({"vendor_identity", "text_view"})
# The text view each vendor grammar's capture must name, by (adapter, parser).
# A capture naming any other view, or none, is refused whatever its parse state:
# its text and findings are not what this build's parser reads from the same bytes.
CAPTURE_TEXT_VIEWS: Final = {
    (dots_layout.ADAPTER, dots_layout.PARSER): dots_layout.TEXT_VIEW,
    ("chandra.v1", "html"): chandra_layout.LAYOUT_TEXT_VIEW,
    ("churro.v1", churro_document.CHURRO_PARSER): churro_document.CHURRO_TEXT_VIEW,
}


def capture_text_view(adapter: str, parser: str | None) -> str | None:
    """The text view a capture of this adapter and parser records, if it records one."""
    return CAPTURE_TEXT_VIEWS.get((adapter, parser))


def validate_capture_text_view(capture: dict[str, Any]) -> dict[str, Any]:
    """Refuse a capture not read under this build's text view for its grammar.

    Called wherever a retained capture is reused or re-derived, so such a
    capture is refused by its view rather than as a capture that differs from
    its own bytes.
    """
    current = CAPTURE_TEXT_VIEWS.get((capture["adapter"], capture["parse"].get("parser")))
    if current is None:
        return capture
    named = capture.get("text_view")
    if named != current:
        raise SchemaRefusal(
            f"a {capture['adapter']} page capture names text view {named!r}, not {current}; "
            "re-run the submission from the Door"
        )
    return capture


_VENDOR_IDENTITY_FIELDS: Final = frozenset({"repository", "sha", "carried_strings"})
#: One parser name per vendor grammar: Chandra `html`, Churro `xml`, DAI `text`,
#: dots.mocr `layout-json`. Closed because re-derivation dispatches on the recorded name.
NATIVE_CAPTURE_PARSERS: Final = frozenset({"html", "xml", "text", dots_layout.PARSER})
#: `json` is the committed fixture's Chandra placeholder, not a vendor grammar;
#: kept apart so removing it is one line once `proof/` uses vendor grammars.
_TRANSITIONAL_CAPTURE_PARSERS: Final = frozenset({"json"})
_ADMITTED_CAPTURE_PARSERS: Final = NATIVE_CAPTURE_PARSERS | _TRANSITIONAL_CAPTURE_PARSERS
# `unrecognized-shape`: the parser read the whole response and knew no shape
# for it, as opposed to `failed` (refused the bytes).
_NATIVE_CAPTURE_PARSE_STATES: Final = frozenset(
    {"not-requested", "pending", "parsed", "failed", "unrecognized-shape"}
)
#: The repetition scan's findings, which any native capture may carry. Kept
#: apart from the grammar's findings: a capture carries at most one of these.
REPETITION_FINDING_KINDS: Final = frozenset(
    {"post-hoc-repetition", "post-hoc-repetition-uninspected"}
)
#: The grammar's half is imported, not restated, so the two cannot drift.
_CHURRO_CAPTURE_FINDING_KINDS: Final = (
    REPETITION_FINDING_KINDS | churro_document.DOCUMENT_FINDING_KINDS
)
_CHURRO_CUTOFF_STOP_REASONS: Final = frozenset(
    {"length", "max_new_tokens", READER_STOP_REPETITION_LOOP}
)
# `eos`/`stop`/`max_new_tokens` are the fixture transport's words, `length` is
# vLLM's cut-off word, `repetition-loop` is the client's when it stopped a
# streamed reply on a loop, and `STOP_REASON_UNREPORTED` is what a live page
# chair retains when the wire carried no `finish_reason`.
_CHURRO_STOP_REASONS: Final = (
    frozenset({"eos", "stop"}) | _CHURRO_CUTOFF_STOP_REASONS | {STOP_REASON_UNREPORTED}
)


#: One grammar, read the same way by fixture and live runs.
CHURRO_PARSERS: Final = frozenset({churro_document.CHURRO_PARSER})


def parse_churro_response(raw: bytes, *, system_prompt: str | None = None) -> dict[str, Any]:
    """One Churro body, read under the vendor's own grammar and its two fallbacks.

    Adds the intake ceiling to `churro_document.parse_churro_document`, and
    closes the record it returns, so the capture writer and the Perlector's
    page feed index a record of the declared shape or refuse it by name.
    `system_prompt` must be the exact string sent: trimming an echo of a
    framing that was not sent could cut real transcription.
    """

    return churro_document.validate_churro_document_parse(
        churro_document.parse_churro_document(
            raw, system_prompt=system_prompt, max_bytes=CHURRO_MAX_RESPONSE_BYTES
        )
    )


def churro_text_outside_sections(raw: bytes, *, system_prompt: str | None = None) -> str:
    """`churro_document.text_outside_sections` under the intake ceiling."""

    return churro_document.text_outside_sections(
        raw, system_prompt=system_prompt, max_bytes=CHURRO_MAX_RESPONSE_BYTES
    )


def native_parse_refusal(parse: dict[str, Any]) -> str:
    """The one sentence a non-parsed native capture is described by.

    Shared by the live boundary, which writes it, and the page validator, which
    re-derives it; they must agree.
    """
    if parse["state"] == "failed":
        return parse["reason"]
    if parse["state"] == "unrecognized-shape":
        return f"the response shape was not recognized: {parse['outcome']}"
    raise SchemaRefusal(
        f"a {parse['state']!r} native parse record carries no refusal to name; "
        "only a failed or unrecognized-shape parse describes one"
    )


def detect_repetition(raw: bytes | bytearray | str) -> dict[str, Any] | None:
    """Report a repeated tail after capture; this function has no generation input.

    Chair-neutral.  Returns a finding for the caller to record; it never
    re-rolls or gates.  Bytes that are not UTF-8 give an
    "uninspected" finding rather than an exception.
    """
    if isinstance(raw, str):
        text = raw
    else:
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError:
            return {
                "kind": "post-hoc-repetition-uninspected",
                "reason": "response is not UTF-8 text",
            }
    normalized = re.sub(r"\s+", " ", text).strip()
    if len(normalized) < _REPETITION_WINDOW * _REPETITION_MIN_REPEATS:
        return None
    for width in range(
        _REPETITION_WINDOW,
        min(256, len(normalized) // _REPETITION_MIN_REPEATS) + 1,
    ):
        unit = normalized[-width:]
        repeats = 1
        while (repeats + 1) * width <= len(normalized) and (
            normalized[-(repeats + 1) * width : -repeats * width] == unit
        ):
            repeats += 1
        if repeats >= _REPETITION_MIN_REPEATS:
            return {"kind": "post-hoc-repetition", "unit_characters": width, "repeats": repeats}
    return None


def derive_churro_capture(
    raw: bytes,
    transport_stop_reason: str,
    *,
    parser: str | None,
    system_prompt: str | None = None,
    document_parser=parse_churro_response,
    repetition_detector=detect_repetition,
) -> dict[str, Any]:
    """Derive the exact mutable-free facts a Churro capture may publish.

    Oversized bytes stay in the blob store unparsed and unscanned, and the
    record says so.  `parser` is `None` for no parse; an unknown name is
    refused rather than recorded as `not-requested`.  The
    repetition scan reads the parsed text where there is one, since repetition
    is about what was transcribed, not the XML around it.
    """
    if parser is not None and parser not in CHURRO_PARSERS:
        raise SchemaRefusal(
            f"a Churro capture cannot be derived under parser {parser!r}; this chair reads one "
            f"grammar and the parser names for it are {sorted(CHURRO_PARSERS)}"
        )
    if len(raw) > CHURRO_MAX_RESPONSE_BYTES:
        reason = (
            "Churro response exceeds the retained parsing limit of "
            f"{CHURRO_MAX_RESPONSE_BYTES} bytes (received {len(raw)})"
        )
        parse = (
            {"state": "not-requested", "parser": None}
            if parser is None
            else {"state": "failed", "parser": parser, "reason": reason}
        )
        return {
            "parse": parse,
            "findings": [
                {
                    "kind": "post-hoc-repetition-uninspected",
                    "reason": reason,
                    "inspected": "raw-response",
                }
            ],
            "stop_reason": _churro_stop_reason(
                transport_stop_reason, parse["state"], repeated=False
            ),
        }

    parse: dict[str, Any] = {"state": "not-requested", "parser": None}
    findings: list[dict[str, Any]] = []
    text_view: str | None = None
    if parser is not None:
        document = document_parser(raw, system_prompt=system_prompt)
        # Copied: an injected parser may hand back records it keeps.
        findings.extend(dict(finding) for finding in document["findings"])
        if document["state"] == "parsed":
            parse = {"state": "parsed", "parser": parser, "text": document["text"]}
            text_view = document["view"]
        elif document["state"] == "failed":
            parse = {"state": "failed", "parser": parser, "reason": document["reason"]}
        else:
            # The whole sentence, because it is what names the root element.
            parse = {
                "state": "unrecognized-shape",
                "parser": parser,
                "outcome": document["reason"],
            }
    parsed_text = parse.get("text")
    inspected, basis = (
        (parsed_text.encode("utf-8"), "parsed-text")
        if isinstance(parsed_text, str)
        else (raw, "raw-response")
    )
    repeated = False
    if finding := repetition_detector(inspected):
        findings.append({**finding, "inspected": basis})
        repeated = finding["kind"] == "post-hoc-repetition"
    derived = {
        "parse": parse,
        "findings": findings,
        "stop_reason": _churro_stop_reason(
            transport_stop_reason, parse["state"], repeated=repeated
        ),
    }
    # The view the parser says it read under, where it read the document at all.
    if text_view is not None:
        derived["text_view"] = text_view
    return derived


def _churro_stop_reason(transport_stop_reason: str, parse_state: str, *, repeated: bool) -> str:
    """A parse failure outranks repetition; the repetition finding is kept either way.

    A reply the client stopped on a repetition loop keeps that stop over the
    post-hoc scan's finding, which then only restates it.
    """
    if parse_state == "failed":
        return "partial-parse-failed"
    if parse_state == "unrecognized-shape":
        return "partial-parse-unrecognized-shape"
    if repeated and transport_stop_reason != READER_STOP_REPETITION_LOOP:
        return "partial-post-hoc-repetition-detected"
    return transport_stop_reason


def churro_capture_system_prompt(capture: dict[str, Any]) -> str | None:
    """The exact system string a retained Churro capture says its request sent.

    Re-derivation must trim against the framing actually sent; with two
    framings, any other source would be a guess.
    """

    view = capture.get("view")
    prompt = view.get("prompt") if isinstance(view, dict) else None
    system = prompt.get("system") if isinstance(prompt, dict) else None
    return system if isinstance(system, str) else None


def derive_dots_capture(
    raw: bytes, transport_stop_reason: str, *, parser: str | None
) -> dict[str, Any]:
    """The facts a dots.mocr capture may publish, derived from its raw answer alone.

    Churro's shape (`derive_churro_capture`): the parse under the layout
    grammar (`common/dots_layout.py`), the grammar's findings and the
    repetition scan's, and the stop reason they leave. The scan reads the
    parsed text where there is one, else the raw answer.
    """
    if parser is not None and parser != dots_layout.PARSER:
        raise SchemaRefusal(
            f"a dots.mocr capture cannot be derived under parser {parser!r}; this chair reads "
            f"one grammar, {dots_layout.PARSER!r}"
        )
    parse: dict[str, Any] = {"state": "not-requested", "parser": None}
    findings: list[dict[str, Any]] = []
    text_view: str | None = None
    if parser is not None:
        layout = dots_layout.parse_layout(raw)
        if layout["state"] == "parsed":
            parse = {"state": "parsed", "parser": parser, "text": layout["text"]}
            findings.extend(dict(finding) for finding in layout["findings"])
            text_view = layout["view"]
        elif layout["state"] == "failed":
            parse = {"state": "failed", "parser": parser, "reason": layout["reason"]}
        else:
            parse = {"state": "unrecognized-shape", "parser": parser, "outcome": layout["reason"]}
    parsed_text = parse.get("text")
    inspected, basis = (
        (parsed_text.encode("utf-8"), "parsed-text")
        if isinstance(parsed_text, str)
        else (raw, "raw-response")
    )
    repeated = False
    if len(raw) <= dots_layout.MAX_RESPONSE_BYTES and (finding := detect_repetition(inspected)):
        findings.append({**finding, "inspected": basis})
        repeated = finding["kind"] == "post-hoc-repetition"
    derived = {
        "parse": parse,
        "findings": findings,
        "stop_reason": _churro_stop_reason(
            transport_stop_reason, parse["state"], repeated=repeated
        ),
    }
    if text_view is not None:
        derived["text_view"] = text_view
    return derived


def _validate_dots_capture(value: dict[str, Any]) -> None:
    """Close the rules that belong to dots.mocr alone, once the shared shape holds."""
    parse = value["parse"]
    if value["transport_stop_reason"] not in _CHURRO_STOP_REASONS:
        raise SchemaRefusal(
            "a dots.mocr page capture has an unknown transport stop reason "
            f"{value['transport_stop_reason']!r}"
        )
    if value["view"] != dots_capture_view():
        raise SchemaRefusal(
            "a dots.mocr page capture does not retain exactly the vendor's prompt and its "
            f"{dots_layout.MAX_COMPLETION_TOKENS}-token bound"
        )
    if (
        parse["state"] not in {"parsed", "failed", "unrecognized-shape"}
        or parse.get("parser") != dots_layout.PARSER
    ):
        raise SchemaRefusal("a retained dots.mocr page capture has no terminal parse record")
    for finding in value["findings"]:
        if finding["kind"] not in REPETITION_FINDING_KINDS | dots_layout.FINDING_KINDS:
            raise SchemaRefusal(
                f"a dots.mocr page capture has unknown finding kind {finding['kind']!r}"
            )
    repeated = any(finding["kind"] == "post-hoc-repetition" for finding in value["findings"])
    if value["stop_reason"] != _churro_stop_reason(
        value["transport_stop_reason"], parse["state"], repeated=repeated
    ):
        raise SchemaRefusal(
            "a dots.mocr page capture stop reason disagrees with its parse and findings"
        )


def dots_capture_view() -> dict[str, Any]:
    """The one model view a dots.mocr capture retains: the prompt sent and its answer bound."""
    return {
        "prompt": {"user": dots_layout.prompt_text()},
        "generation": {"max_completion_tokens": dots_layout.MAX_COMPLETION_TOKENS},
    }


def verify_native_capture_bytes(value: Any, raw: bytes) -> dict[str, Any]:
    """Verify one capture's derived record against raw bytes already digest-checked.

    A capture read under a text view this build no longer produces is refused by
    that name first, rather than reported as differing from its bytes.
    """
    capture = validate_capture_text_view(validate_native_capture(value))
    if capture["adapter"] == dots_layout.ADAPTER:
        derived = derive_dots_capture(
            raw, capture["transport_stop_reason"], parser=capture["parse"]["parser"]
        )
        for field in ("parse", "findings", "stop_reason"):
            if capture[field] != derived[field]:
                raise SchemaRefusal(
                    f"a dots.mocr native capture's {field} differs from its retained raw response"
                )
        return capture
    if capture["adapter"] != "churro.v1":
        return capture
    derived = derive_churro_capture(
        raw,
        capture["transport_stop_reason"],
        parser=capture["parse"]["parser"],
        system_prompt=churro_capture_system_prompt(capture),
    )
    for field in ("parse", "findings", "stop_reason"):
        if capture[field] != derived[field]:
            raise SchemaRefusal(
                f"a Churro native capture's {field} differs from its retained raw response"
            )
    return capture


def verify_native_capture_blob(tree: Any, value: Any) -> dict[str, Any]:
    """Read, digest-check, and derive from the same raw bytes without a check/use gap."""
    capture = validate_native_capture(value)
    reference = capture["raw_response_ref"]
    raw = read_verified(tree.read_bytes, reference, "a page Testimonium raw response")
    return verify_native_capture_bytes(capture, raw)


def _validate_churro_capture(value: dict[str, Any]) -> None:
    """Close the rules that belong to Churro alone, once the shared shape holds."""
    parse = value["parse"]
    state, parser, findings = parse["state"], parse.get("parser"), value["findings"]
    if value["transport_stop_reason"] not in _CHURRO_STOP_REASONS:
        raise SchemaRefusal(
            "a Churro page capture has an unknown transport stop reason "
            f"{value['transport_stop_reason']!r}"
        )
    view = value["view"]
    # `framing` names which declared framing was asked; it may be absent.
    if set(view) - {"framing"} != {"prompt", "generation"}:
        raise SchemaRefusal(
            "a Churro page capture does not retain exactly its prompt and generation view"
        )
    if "framing" in view and (not isinstance(view["framing"], str) or not view["framing"]):
        raise SchemaRefusal("a Churro page capture names a framing that is not a nonblank string")
    prompt, generation = view["prompt"], view["generation"]
    # System only: both vendor framings send the image alone in the user turn.
    if (
        not isinstance(prompt, dict)
        or set(prompt) != {"system"}
        or not isinstance(prompt["system"], str)
        or not prompt["system"]
    ):
        raise SchemaRefusal("a Churro page capture has no closed nonblank system-only prompt view")
    if (
        not isinstance(generation, dict)
        or set(generation) != {"max_new_tokens"}
        or not is_plain_int(generation["max_new_tokens"])
        or generation["max_new_tokens"] != CHURRO_OUTPUT_TOKENS
    ):
        raise SchemaRefusal(
            f"a Churro page capture does not retain its {CHURRO_OUTPUT_TOKENS}-token bound"
        )
    if state not in {"parsed", "failed", "unrecognized-shape"} or parser not in CHURRO_PARSERS:
        raise SchemaRefusal("a retained Churro page capture has no terminal parse record")
    repetitions = [finding for finding in findings if finding["kind"] in REPETITION_FINDING_KINDS]
    if len(repetitions) > 1:
        raise SchemaRefusal("a Churro page capture carries more than one repetition finding")
    for finding in findings:
        kind = finding["kind"]
        if kind not in _CHURRO_CAPTURE_FINDING_KINDS:
            raise SchemaRefusal(f"a Churro page capture has unknown finding kind {kind!r}")
        if kind not in REPETITION_FINDING_KINDS:
            # Grammar findings are held exactly by `verify_native_capture_bytes`.
            continue
        if kind == "post-hoc-repetition":
            if set(finding) != {"kind", "unit_characters", "repeats", "inspected"} or any(
                not is_plain_int(finding[field]) or finding[field] <= 0
                for field in ("unit_characters", "repeats")
            ):
                raise SchemaRefusal(
                    "a Churro page capture has a malformed post-hoc repetition finding"
                )
        elif set(finding) != {"kind", "reason", "inspected"} or not (
            isinstance(finding["reason"], str) and finding["reason"]
        ):
            raise SchemaRefusal(
                "a Churro page capture has a malformed uninspected-repetition finding"
            )
        if finding["inspected"] not in {"parsed-text", "raw-response"}:
            raise SchemaRefusal(
                "a Churro page capture repetition finding does not name the inspected view"
            )
    repeated = any(finding["kind"] == "post-hoc-repetition" for finding in findings)
    if value["stop_reason"] != _churro_stop_reason(
        value["transport_stop_reason"], state, repeated=repeated
    ):
        raise SchemaRefusal(
            "a Churro page capture stop reason disagrees with its parse and findings"
        )


def validate_vendor_identity(value: Any) -> dict[str, Any]:
    """Close which vendor pin the bytes beside this capture were taken from.

    The prompt, message shape and grammar are the vendor's at one commit, and a
    different pin reads the same bytes differently.  ``sha`` is
    an exact commit, never a movable tag.  ``carried_strings`` digests each
    string carried from that pin (DAI's ``repository`` is its weights repository).
    """
    if not isinstance(value, dict) or set(value) != _VENDOR_IDENTITY_FIELDS:
        raise SchemaRefusal(
            "a page Testimonium native capture vendor identity is not its closed "
            f"{sorted(_VENDOR_IDENTITY_FIELDS)} schema"
        )
    if not isinstance(value["repository"], str) or not value["repository"].strip():
        raise SchemaRefusal("a page Testimonium native capture vendor identity names no repository")
    if not is_hf_revision(value["sha"]):
        raise SchemaRefusal(
            "a page Testimonium native capture vendor identity is not pinned to an exact 40-hex "
            "commit; a tag or branch can move under the record that cites it"
        )
    carried = value["carried_strings"]
    if not isinstance(carried, dict) or not carried:
        raise SchemaRefusal(
            "a page Testimonium native capture vendor identity carries no string digests, so it "
            "records a vendor pin for no bytes"
        )
    for name, digest in carried.items():
        if not isinstance(name, str) or not name.strip():
            raise SchemaRefusal(
                "a page Testimonium native capture vendor identity names a carried string with "
                "no name"
            )
        if not is_sha256(digest):
            raise SchemaRefusal(
                f"a page Testimonium native capture vendor identity records {name!r} without a "
                "sha256 of the bytes actually carried"
            )
    return value


def validate_native_capture(value: Any) -> dict[str, Any]:
    """Close the derived model view; raw response bytes remain in its referenced blob."""
    if not isinstance(value, dict) or not (
        _NATIVE_CAPTURE_FIELDS
        <= set(value)
        <= _NATIVE_CAPTURE_FIELDS | _NATIVE_CAPTURE_OPTIONAL_FIELDS
    ):
        raise SchemaRefusal(
            "a page Testimonium native capture is not its retained model-view schema"
        )
    if "vendor_identity" in value:
        validate_vendor_identity(value["vendor_identity"])
    for field in ("schema", "adapter", "transport_stop_reason", "stop_reason"):
        if not isinstance(value[field], str) or not value[field]:
            raise SchemaRefusal(f"a page Testimonium native capture has a blank {field}")
    if value["schema"] != "attestatores-model-view.v1":
        raise SchemaRefusal(
            "a page Testimonium native capture has an unknown retained model-view schema"
        )
    if not isinstance(value["view"], dict):
        raise SchemaRefusal("a page Testimonium native capture view is not an object")
    reference = digest_ref(
        value["raw_response_ref"], "a page Testimonium native capture raw-response reference"
    )
    if reference["relative_path"] != _attestatores_blob_path(reference["sha256"]):
        raise SchemaRefusal(
            "a page Testimonium native capture raw-response reference is not its "
            "content-addressed Attestatores blob path and digest"
        )
    findings = value["findings"]
    if not isinstance(findings, list) or not all(
        isinstance(finding, dict) and isinstance(finding.get("kind"), str) and finding["kind"]
        for finding in findings
    ):
        raise SchemaRefusal("a page Testimonium native capture has a malformed findings list")
    parse = value["parse"]
    state = parse.get("state") if isinstance(parse, dict) else None
    if not isinstance(parse, dict) or state not in _NATIVE_CAPTURE_PARSE_STATES:
        raise SchemaRefusal("a page Testimonium native capture has a malformed parse record")
    parser = parse.get("parser")
    if state in {"not-requested", "pending"}:
        expected = {"state", "parser"}
    else:
        # Distinct third fields, so one state cannot carry another's evidence.
        third = {"parsed": "text", "failed": "reason"}.get(state, "outcome")
        expected = {"state", "parser", third}
    if set(parse) != expected:
        raise SchemaRefusal("a page Testimonium native capture parse record has the wrong shape")
    # Separate sentences, because the faults have different fixes.
    if state == "not-requested":
        if parser is not None:
            raise SchemaRefusal(
                "a page Testimonium native capture states that no parse was requested and then "
                f"names {parser!r} as the parser that ran it"
            )
    elif not isinstance(parser, str) or parser not in _ADMITTED_CAPTURE_PARSERS:
        raise SchemaRefusal(
            f"a page Testimonium native capture names parser {parser!r}, which no vendor grammar "
            f"in this pipeline answers to, so the capture could never be re-derived from its "
            f"retained bytes (the grammars are {sorted(NATIVE_CAPTURE_PARSERS)})"
        )
    if state == "parsed" and not isinstance(parse["text"], str):
        raise SchemaRefusal("a page Testimonium native capture claims parsed with no text")
    if state == "failed" and not (isinstance(parse["reason"], str) and parse["reason"]):
        raise SchemaRefusal(
            "a page Testimonium native capture claims a parse failure with no reason"
        )
    if state == "unrecognized-shape" and not (
        isinstance(parse["outcome"], str) and parse["outcome"]
    ):
        raise SchemaRefusal(
            "a page Testimonium native capture claims an unrecognized shape without naming it"
        )
    if "text_view" in value and not (
        isinstance(value["text_view"], str) and value["text_view"].strip()
    ):
        raise SchemaRefusal("a page Testimonium native capture names a blank text view")
    if value["adapter"] == "churro.v1":
        _validate_churro_capture(value)
    elif value["adapter"] == dots_layout.ADAPTER:
        _validate_dots_capture(value)
    if "text_view" in value and (value["adapter"], parser) not in CAPTURE_TEXT_VIEWS:
        raise SchemaRefusal(
            f"a {value['adapter']} page capture under parser {parser!r} names text view "
            f"{value['text_view']!r}, but that grammar's parse is read under no text view"
        )
    return value


def validate_page_edge_overshoots(overshoots: Any) -> list[dict[str, Any]]:
    """Close a page Testimonium's page-edge findings: each rejected box, once, as reported."""
    if not isinstance(overshoots, list):
        raise SchemaRefusal(
            "the page Testimonium has malformed page-edge findings. "
            "The rejected witness geometry cannot be accounted from this value. "
            "Rebuild the page-edge findings as a list of closed findings."
        )
    seen_overshoots: set[tuple[str, int]] = set()
    for finding in overshoots:
        if not isinstance(finding, dict) or set(finding) != {
            "kind",
            "response_sha256",
            "ordinal",
            "bounds",
            "sealed_page_bounds",
        }:
            raise SchemaRefusal(
                "a page Testimonium page-edge finding is outside its closed schema. "
                "Its rejected box or response provenance cannot be accounted. "
                "Rebuild the finding from the retained response and sealed page edge."
            )
        if (
            finding["kind"] != "page-edge-overshoot"
            or not isinstance(finding["response_sha256"], str)
            or len(finding["response_sha256"]) != 64
            or not is_plain_int(finding["ordinal"])
            or finding["ordinal"] < 0
        ):
            raise SchemaRefusal(
                "a page Testimonium page-edge finding has an invalid identity. "
                "The finding cannot be traced to one response block. "
                "Restore its response digest and non-negative response ordinal."
            )
        bounds = _bounds(finding["bounds"], "a page Testimonium page-edge finding", page_size=None)
        page_bounds = _bounds(
            finding["sealed_page_bounds"],
            "a page Testimonium page-edge finding sealed page",
            page_size=None,
        )
        if (
            page_bounds["x"] != 0
            or page_bounds["y"] != 0
            or (
                bounds["x"] + bounds["w"] <= page_bounds["w"]
                and bounds["y"] + bounds["h"] <= page_bounds["h"]
            )
        ):
            raise SchemaRefusal(
                "a page Testimonium page-edge finding does not retain an out-of-page box. "
                "The record claims a rejection that its own geometry does not support. "
                "Re-derive the finding from the exact quantized witness box."
            )
        key = (finding["response_sha256"], finding["ordinal"])
        if key in seen_overshoots:
            raise SchemaRefusal(
                "a page Testimonium names one page-edge finding twice. "
                "The same response block would be counted as two findings. "
                "Remove the duplicate and rebuild the page partition."
            )
        seen_overshoots.add(key)
    return overshoots


def _validate_page_edge_overshoot_response_refs(
    overshoots: list[dict[str, Any]], raw_response_refs: Any
) -> None:
    """Require every rejected box to name bytes retained by its page record."""
    if not overshoots:
        return
    if not isinstance(raw_response_refs, list):
        raise SchemaRefusal(
            "a page-edge finding has no retained response reference. "
            "The rejected box cannot be traced back to the bytes that produced it. "
            "Retain the raw response reference before publishing the finding."
        )
    # Closed before indexing, so a malformed entry is a named refusal.
    if any(
        not isinstance(reference, dict) or not isinstance(reference.get("sha256"), str)
        for reference in raw_response_refs
    ):
        raise SchemaRefusal(
            "a page-edge finding names a malformed retained response reference. "
            "The rejected box cannot be traced back to the bytes that produced it. "
            "Retain the raw response reference before publishing the finding."
        )
    known = {reference["sha256"] for reference in raw_response_refs}
    if any(finding["response_sha256"] not in known for finding in overshoots):
        raise SchemaRefusal(
            "a page-edge finding names no retained response on its page Testimonium. "
            "Its geometry provenance is absent from the record that carries it. "
            "Attach the matching raw response reference and rebuild the page Testimonium."
        )
