"""The closed, derived waist of a native witness report.

Native responses remain in their retained raw blob.  These values are the small
set this pipeline derives from them: what image was presented and the page-pixel
rectangles a witness reported seeing.  They deliberately contain no act identity
or preference: correspondence is a consumer lookup, never witness testimony.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from typing import Any, Final

from common import churro_document
from common.chairs.models import is_hf_revision
from common.contracts.canonical import digest_bytes, is_sha256
from common.contracts.errors import SchemaRefusal
from common.contracts.serving import STOP_REASON_UNREPORTED
from common.contracts.stages import ATTESTATORES, writing_directory
from common.corpus_register import refuse_capture_preference
from common.imaging import (
    MAX_PIXELS,
    convert_png_to_rgb,
    crop_png,
    dimensions,
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
PAGE_TESTIMONIUM_REQUIRED_FIELDS: Final = frozenset(
    {
        "chair",
        "act_key",
        "attempt_ordinal",
        "regions",
        "provenance",
        "format_capabilities",
        "payload",
        "witness_reported",
        "content_health",
        "presented",
        "observed",
        "unpresented_regions",
        "scope",
        "page_ordinal",
        "page_role",
        "unjoined_act_attempts",
    }
)
PAGE_TESTIMONIUM_OPTIONAL_FIELDS: Final = frozenset(
    {
        "reason",
        "partition_disagreement",
        # The retained responses this record's own derived geometry was
        # quantized from, and the declared rule that converted them. Plural
        # because a page record's partition may be assembled from more than one
        # retained response; a page witness that answers once retains one.
        "raw_response_refs",
        "adapter_metadata",
        "native_capture",
    }
)
PAGE_ROLES: Final = frozenset({"primary", "continuation", "mixed"})

# The rounding rule each resizing operation's own publisher applies -- and so,
# read as a key set, which operations resize at all. Ours and Churro's truncate
# (`int()`); Chandra's snaps to its 28-pixel patch grid, and calling that
# `floor` would be a record that reads false (GOVERNANCE 10).
_DIMENSION_ROUNDING: Final = {
    "crop-resize-preserve-aspect": "floor",
    "chandra-scale-to-fit.v1": "grid-28",
    "churro-prepare-ocr-image.v1": "floor",
}
# The executable transforms an `adapter-crop` presentation may name.
#
# Every one of them is replayed from sealed page bytes by
# `validate_presented_page_binding` before its blob digest is believed, so the
# list is not documentation: an operation absent here has no re-derivation and
# is refused rather than trusted (ARCHITECTURE invariant 3). The resizing
# subset is derived from `_DIMENSION_ROUNDING` rather than restated, because
# the two lists disagreeing is a schema that requires a recipe it then cannot
# name a rounding rule for.
#
# `crop` and `crop-resize-preserve-aspect` are this repository's own recipes.
# The two `.v1` operations are ports of a vendor's own preprocessing, adopted
# under the ruling that admitted them and named after the vendor function each
# reproduces, so a record says which vendor pipeline sized the pixels rather
# than only that something resized them:
#
# * `chandra-scale-to-fit.v1` -- `chandra/model/util.py::scale_to_fit` at
#   `datalab-to/chandra @ d4f7467435aa4137d9539f000ddf0b7ced3eb43f`: LANCZOS
#   onto a 28-pixel grid inside a 6,291,456-pixel area, with the vendor's own
#   greedy aspect trim. Its target therefore does *not* preserve the source
#   aspect exactly, which is why the aspect identity in `_validate_resize_recipe`
#   is not applied to it -- and does not have to be: Chandra's own geometry is
#   `data-bbox` normalized 0-1000, mapped by `to_page_bounds` against the
#   *sealed page* size, never through this resized view.
# * `churro-prepare-ocr-image.v1` -- `src/churro_ocr/_internal/image.py::
#   prepare_ocr_image` at `stanford-oval/Churro @
#   4abb17386d9656199c2776195926545fc527a691`: `ensure_rgb(resize_image_to_fit(
#   img, 2500, 2500))`, LANCZOS, downscale-only, and the RGB step recorded as
#   `colour_mode` because the vendor performs it before the model sees a pixel.
#
# **One departure, named here rather than discovered later.** Both ports resize
# through `common/imaging.resize_png_lanczos`, which promotes a bilevel (mode
# `"1"`) image to `"L"` before resampling, because Pillow 12.3.0 silently
# substitutes NEAREST for LANCZOS on modes `"1"` and `"P"` -- a recipe that
# said `pillow-lanczos` and delivered nearest-neighbour would be a record that
# reads false. Churro's `resize_image_to_fit` resizes whatever it loaded
# directly, so on a *bitonal* sealed page (triage's `bitonal` mode writes one,
# and `_PNG_IDENTITY_MODES` seals it as `"1"`) the vendor gets Pillow's NEAREST
# and this replay gets a true LANCZOS. The pixels differ, and the departure is
# reproduced deliberately in the direction of the honest resampler rather than
# corrected in the direction of the vendor's accident. `test_a_bitonal_crop_
# replays_through_our_lanczos_not_the_vendors_nearest` pins it, and **U8's
# vendor parity table needs a mode-`"1"` row that expects inequality here**;
# a parity test that asserted byte equality on a bitonal source would be
# asserting something neither port claims. Chandra is unaffected: its own
# loader converts to RGB before anything resizes (`chandra/input.py::
# load_image`), and LANCZOS-then-expand equals expand-then-LANCZOS on both
# `"L"` and `"1"` sources.
RESIZING_ADAPTER_CROP_OPERATIONS: Final = frozenset(_DIMENSION_ROUNDING)
ADAPTER_CROP_OPERATIONS: Final = frozenset({"crop"}) | RESIZING_ADAPTER_CROP_OPERATIONS
#: The colour conversions an adapter may perform between the resize and the
#: wire, spelled from the words `pipeline/0_triage/manifest.py::COLOUR_MODES`
#: already uses for the same concept (GLOSSARY: one concept, one word). Only
#: `rgb` is executable here, and `keep` says explicitly that no conversion ran
#: -- a distinction a missing field cannot make on a record that is allowed to
#: omit it. A further mode arrives with the vendor that performs it, never
#: ahead of one.
ADAPTER_COLOUR_MODES: Final = frozenset({"keep", "rgb"})
#: Which operations may name a `colour_mode`, and which must. `ensure_rgb` is
#: half of `prepare_ocr_image`, so a Churro presentation that does not say what
#: it did to the colour samples has not recorded the vendor operation it names.
_COLOUR_MODE_REQUIRED_OPERATIONS: Final = frozenset({"churro-prepare-ocr-image.v1"})
_COLOUR_MODE_OPTIONAL_OPERATIONS: Final = frozenset({"chandra-scale-to-fit.v1"})
#: And which may name only one value. `prepare_ocr_image` is
#: `ensure_rgb(resize_image_to_fit(...))` with no branch in it, so a Churro
#: record saying `keep` says the RGB half of the operation it names did not
#: run. Requiring the key and then admitting either answer would let a record
#: name the vendor operation over a grayscale blob the vendor never sends --
#: the same silence the required-field rule above was written to close, one
#: level in. `keep` stays meaningful for Chandra, whose `scale_to_fit`
#: performs no conversion of its own.
_COLOUR_MODE_FIXED_VALUES: Final = {"churro-prepare-ocr-image.v1": "rgb"}
# `chandra/model/util.py::scale_to_fit` at the pinned sha: LANCZOS onto a
# 28-pixel grid, under a 3072x2048 = 6,291,456-pixel maximum area.
#
# **The vendor's 1792x28 = 50,176-pixel minimum is not a bound on its output
# and is deliberately not checked here.** It is the *input* area below which
# `scale_to_fit` scales up; the grid snap that follows rounds each side to the
# nearest 28 and can land back under it. A 100x80 crop scales to 250.4x200.4,
# snaps to 252x196, and 49,392 px is below the minimum the vendor was aiming
# at -- a record its own function produced. Refusing that would refuse a legal
# Chandra presentation, which is the one thing this vocabulary must never do.
# The maximum is a real post-condition: the refinement loop trims blocks until
# the area is under it, and its 1x1 escape is 784 px.
CHANDRA_SCALE_GRID_PX: Final = 28
CHANDRA_SCALE_MAX_PIXELS: Final = 3072 * 2048
# `resize_image_to_fit(img, 2500, 2500)` at the pinned tag; the same 2,500 is
# `_MAX_IMAGE_DIM` in the paper-era harness and `MAX_IMAGE_DIM` in the
# standalone `churro_transformers_infer.py`. Re-exported from the port that
# also performs the arithmetic rather than restated: this module holds a record
# to the vendor's rule, and a second literal of the vendor's own bound is a
# second thing to keep in step with the vendor.
CHURRO_MAX_IMAGE_DIM_PX: Final = CHURRO_MAX_INLINE_IMAGE_DIM

# Churro's *declared* output bound, retained on every request's
# `generation_declared` and in every retained Churro model view as the record
# of what Churro's own pipeline asks for.  It is not, by itself, what goes on
# the wire: `common/request_capacity.py::sendable_max_tokens` sends
# `min(this, max_model_len - image - prompt)` against the request's own
# capacity record, because vLLM's admission rule is
# `prompt_tokens + max_tokens <= max_model_len` and every sealed Churro row
# caps `max_model_len` far below this number (8,192 at 24 GB and 48 GB, 16,384
# at 80 GB+).
#
# **The number is not a carried configuration value, and it has three sources.**
# It was 24,000 here, described as Churro's "carried HuggingFace-generate
# `max_new_tokens`"; the model's `generation_config.json` at the pinned
# revision carries no such field, so that description named a source that does
# not exist and the number belonged to nobody. 20,000 is what three separate
# vendor artifacts say, and they are listed with what each one *is*, because
# two of them are generation bounds and the third is not:
#
# 1. **The CHURRO paper, section B.2** (arXiv:2509.19768): a maximum of 20,000
#    generated tokens, "chosen to allow generation of all gold outputs". A
#    generation bound, stated by the authors as such.
# 2. **The standalone reference implementation**,
#    `churro_transformers_infer.py:41-46` at
#    `stanford-oval/churro @ 2db3d9f5489cf12fbbe7384dd7f1b97b5f6f298b`: the
#    `--max-new-tokens` default is 20,000. Also a generation bound, and the one
#    vendor artifact that runs this model without a server.
# 3. **`utils/llm/models.py::COMPLETION_TOKENS_FOR_STANDARD_MODELS = 20_000`**
#    at the paper-era release, wired into the `MODEL_MAP` row for churro and
#    passed to the container as `--max-model-len`. **This one is a context
#    length, not a cap** -- the harness sends no `max_tokens` at all -- and it
#    is recorded here as corroboration of the number with that difference
#    named, never as a third statement of the same quantity. Reading it as a
#    generation bound is the specific misreading the research refuted, and the
#    sealed Churro row's `max_model_len = 20000` is where it actually lands.
#
# The value is declared once for the whole repository in
# `request_capacity.DECLARED_ANSWER_BOUND_TOKENS` beside the other three
# chairs' bounds, so one chair's bound cannot drift from the table the wire
# value is computed from; this module re-exports that entry rather than
# restating the integer.
#
# Four MiB still allows more than 209 UTF-8 response bytes per declared token
# -- far beyond an OCR transcription -- while giving the XML parser and the
# post-hoc repetition scan a hard ceiling whatever bound the request carried.
CHURRO_OUTPUT_TOKENS: Final = DECLARED_ANSWER_BOUND_TOKENS["attestator_3"]
# One name, so the chair's three legal answer shapes cannot acquire three
# intake bounds. It was declared beside the retired JSON wire contract
# (`common/churro_response.py`) and re-exported here; that module is gone with
# the coordinate channel it closed, so the number is declared here, which is
# where `derive_churro_capture` applies it -- before either parser or the
# repetition detector is handed a byte. `common/churro_document.py` deliberately
# declares no ceiling of its own and takes this one as an argument, so the
# grammar reader and the seam that bounds it cannot drift apart.
CHURRO_MAX_RESPONSE_BYTES: Final = 4 * 1024 * 1024
# The tail-cycle scan's own two numbers, chair-neutral like the scan itself:
# the shortest repeating unit it will call a cycle, and how many times that
# unit must recur at the very end of the response before it is one. Both are
# declared, not measured -- the design defers calibrating them against
# Chandra's own `detect_repeat_token` thresholds to Stage 2, and a threshold
# nobody has measured says so rather than wearing a measurement's authority
# (GOVERNANCE 10).
_REPETITION_WINDOW: Final = 24
_REPETITION_MIN_REPEATS: Final = 3


def _integer(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


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
    if not all(_integer(value[key]) for key in _BOUNDS_FIELDS):
        raise SchemaRefusal(f"{what} has non-integer page-pixel coordinates")
    if value["x"] < 0 or value["y"] < 0 or value["w"] <= 0 or value["h"] <= 0:
        raise SchemaRefusal(f"{what} is not a non-empty non-negative page-pixel box")
    if page_size is not None and (
        value["x"] + value["w"] > page_size[0] or value["y"] + value["h"] > page_size[1]
    ):
        raise SchemaRefusal(f"{what} falls outside the sealed source page")
    return value


def churro_fit_target(source_width: int, source_height: int) -> tuple[int, int]:
    """The exact size `resize_image_to_fit(img, 2500, 2500)` returns for a crop.

    `common/imaging_ports.py::resize_to_fit_churro` is the port of
    `src/churro_ocr/_internal/image.py::resize_image_to_fit` at the pinned sha,
    and it is called rather than restated. It was restated here while this
    module was the only reader of the rule; the adapter that *writes* the
    record now sizes its image through the port, and a contract validator
    holding a record to a second copy of the arithmetic would be pinned to that
    copy rather than to the vendor -- the two would agree for exactly as long
    as both were wrong. The byte equality of the port itself against the
    fetched vendor source is proved in `common/test_vendor_parity.py`.

    Checking only that a target fits the square and does not enlarge would
    admit a stretch the vendor cannot produce: a 4000x3000 crop presented as
    2500x100 passes both of those and re-derives, because re-derivation replays
    whatever target the record asks for. It would then carry the vendor's name
    over an image the vendor's own code would never have made.
    """
    return resize_to_fit_churro(source_width, source_height)


def _validate_resize_recipe(transform: dict[str, Any]) -> None:
    """Close the executable resize recipe, then the rules its operation names.

    Everything above the per-operation block is shared: a closed six-field
    recipe, a resampler this repository can actually run, positive integer
    dimensions typed before any arithmetic reads them, a target inside the
    executable pixel bound, and source dimensions equal to the crop the
    recipe sits on. That last one is what makes the recipe replayable at all;
    the digest check in `validate_presented_page_binding` replays whatever
    target the record asked for, so a target nothing constrains re-derives
    happily and is still the wrong image.

    What each operation adds is what its own publisher's rule makes checkable
    from the recorded numbers, and the two publishers do not offer the same
    thing. Churro's `resize_image_to_fit` is a closed formula, so the target is
    held to it exactly (`churro_fit_target`). Chandra's `scale_to_fit` is a
    scale, a grid snap and a greedy trim loop, and a second hand-written copy
    of *that* in a contract validator would be pinned to itself rather than to
    the vendor -- it would agree with a drifted port for exactly as long as
    both were wrong -- so its target is held only to the grid and the maximum
    area, the two facts its output always satisfies. The byte equality of
    either port against the fetched vendor source is proved offline in
    `common/test_vendor_parity.py`; these are the rules a record can be held to
    here, with no network and no vendor package installed.
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
        _integer(resize[field]) and resize[field] > 0
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
        # `preserve-aspect` and `floor` are the operation's own words, so they are
        # required to be true of the numbers beside them rather than left as
        # description. Without this a record could name this operation over a
        # target that stretches the crop, and still pass every other check here:
        # the digest re-derives, because re-derivation replays whatever target
        # the record asked for. What it would cost is the identification
        # `_dai_observe` makes when it reports the crop's own page bounds as the
        # box for the whole shown image; downstream view-to-page mapping is only
        # sound over a uniform scale.
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
        or not _integer(value["source_page_ordinal"])
        or value["source_page_ordinal"] < 1
        or not isinstance(value["image_path"], str)
        or not value["image_path"]
        # The lowercase hex shape, not merely 64 characters: a blob identity
        # that cannot be a digest can never match one, and saying so here names
        # the malformed presentation instead of a later mismatch.
        or not is_sha256(value["image_sha256"])
    ):
        raise SchemaRefusal("a Testimonium presented block has invalid source or blob identity")
    transform = value["transform"]
    if not isinstance(transform, dict):
        raise SchemaRefusal("a Testimonium presented block has no complete page transform")
    # Every branch below hashes this value against a frozenset, so a non-string
    # operation is normalized away first. Left as it arrives, an unhashable one
    # -- a list, a dict -- would raise `TypeError` out of a contract check
    # instead of the named refusal two checks further down, which is the exact
    # failure `test_unhashable_enum_values_are_named_refusals_not_python_
    # tracebacks` pins for the other enums in this schema.
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
        # Only an `adapter-crop` is ever replayed against sealed page bytes
        # (`validate_presented_page_binding`). A resize recipe on a `page` or a
        # `region` presentation is therefore a transform nothing re-derives --
        # the record would describe a resampling and never be held to it, which
        # is precisely what ARCHITECTURE invariant 3 exists to prevent. Every
        # producer in the tree already publishes these as `adapter-crop`
        # (`witness_adapters.py::_dai_present`); this says so.
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
    presentation_is_witness_view: bool = True,
) -> list[dict[str, Any]]:
    """Validate dense witness order, source-page boxes, and non-overlapping text spans.

    A span addresses this Testimonium's exact retained string in code points,
    never the normalized alignment view. It may be null, but it may not name an
    offset the record cannot answer (GOALS 5).
    """
    if not isinstance(value, list):
        raise SchemaRefusal("a Testimonium observed block is not a list")
    spans: list[tuple[int, int]] = []
    for index, item in enumerate(value):
        if not isinstance(item, dict) or set(item) != {
            "ordinal",
            "bounds",
            "bounds_source",
            "span",
        }:
            raise SchemaRefusal("a Testimonium observed entry is not its closed schema")
        if not _integer(item["ordinal"]) or item["ordinal"] != index:
            raise SchemaRefusal("a Testimonium observed ordinals are not dense, unique, 0-based")
        if (
            not isinstance(item["bounds_source"], str)
            or item["bounds_source"] not in BOUNDS_SOURCES
        ):
            raise SchemaRefusal("a Testimonium observed box has an unknown bounds_source")
        if (
            item["bounds_source"] == "presented"
            and item["bounds"] != presented["transform"]["bounds"]
        ):
            raise SchemaRefusal(
                "a presented-source observed box differs from the presented transform"
            )
        bounds = _bounds(item["bounds"], "a Testimonium observed box", page_size=page_size)
        # The containment refusal below guards records whose presentation IS the
        # complete view the witness received (page-scoped records, and act
        # records of act-scoped chairs). A page witness's act view restates the
        # chair's page-level geometry in page-pixel space, so its boxes may
        # legitimately exceed this record's one-crop presentation; they remain
        # bounded by the sealed page via `page_size` above, and Unit 10C's
        # coverage derivation is what consumes that page-space geometry.
        presented_bounds = presented["transform"]["bounds"]
        if presentation_is_witness_view and not (
            presented_bounds["x"] <= bounds["x"]
            and presented_bounds["y"] <= bounds["y"]
            and presented_bounds["x"] + presented_bounds["w"] >= bounds["x"] + bounds["w"]
            and presented_bounds["y"] + presented_bounds["h"] >= bounds["y"] + bounds["h"]
        ):
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
                or not _integer(span["start"])
                or not _integer(span["end"])
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


def validate_native_witness_geometry(
    payload: Any, *, page_size: tuple[int, int] | None = None
) -> dict[str, Any]:
    """Validate the two derived blocks and recursively refuse preference claims.

    An empty pair records the distinct, visible fact that the chair was never
    shown an image.  It is required for held acts, refused pages, and explicitly
    absent chairs: inventing a whole-page presentation for any of those paths
    would claim evidence that did not exist.  Once a presentation exists, the
    ordinary closed geometry contract applies unchanged.
    """
    if not isinstance(payload, dict):
        raise SchemaRefusal("a Testimonium payload is not an object")
    refuse_capture_preference(payload, what="a Testimonium")
    presented = payload.get("presented")
    observed = payload.get("observed")
    if presented == {}:
        if observed != []:
            raise SchemaRefusal("an unpresented Testimonium must carry an empty observed block")
        return payload
    presented = validate_presented(presented, page_size=page_size)
    # Both Testimonium kinds retain the exact span-addressable text in `payload`.
    validate_observed(
        observed,
        presented=presented,
        page_size=page_size,
        retained_text=payload.get("payload"),
        # An act view of a page witness (`page_witness: True`, never scope="page")
        # restates page-level geometry; every other record presents the witness's
        # own complete view. Consumers reconcile the flag against the sealed
        # page-witness declaration, so it cannot be forged onto an act chair.
        presentation_is_witness_view=(
            payload.get("scope") == "page" or payload.get("page_witness") is not True
        ),
    )
    return payload


def validate_presented_page_binding(
    presented: dict[str, Any],
    *,
    page_ordinal: int,
    page_image_path: str,
    page_sha256: str,
    page_size: tuple[int, int],
    page_bytes: bytes | None = None,
) -> None:
    """Bind a whole-page presentation to the sealed page it names.

    `presented` says which sealed page it is in `source_page_id` and which blob
    was shown in `image_path`/`image_sha256`, and nothing reconciled the two.
    A record could therefore name page 1 and carry page 2's real, digest-bound
    pixels: self-consistent, readable, and a lie about which ink a witness saw.
    Its observed boxes would then be validated against page 1's dimensions and
    read, by any later coverage derivation, as page 1 geometry (GOALS 5;
    ARCHITECTURE invariant 3).

    Region callers also bind the sealed Designator record. An adapter-crop is an
    exact PNG crop or its explicitly recorded LANCZOS resize; either operation
    must reproduce its retained digest from sealed page bytes.
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
        derived = crop_png(page_bytes, bounds)
        if operation in RESIZING_ADAPTER_CROP_OPERATIONS:
            resize = presented["transform"]["resize"]
            # The closed recipe repeats crop dimensions so any re-deriver drift
            # becomes a named schema refusal before resizing.
            if dimensions(derived) != (resize["source_width_px"], resize["source_height_px"]):
                raise SchemaRefusal("a resized adapter-crop recipe disagrees with its sealed crop")
            derived = resize_png_lanczos(
                derived, resize["target_width_px"], resize["target_height_px"]
            )
        # The colour step runs last because that is where the vendor performs
        # it -- `prepare_ocr_image` is `ensure_rgb(resize_image_to_fit(...))`,
        # and converting first would resample three expanded channels instead
        # of the one the vendor resampled. `keep` is recorded and executes
        # nothing, which is the whole of its meaning.
        if presented["transform"].get("colour_mode") == "rgb":
            try:
                derived = convert_png_to_rgb(derived)
            except ValueError as error:
                raise SchemaRefusal(
                    f"an adapter-crop presentation's colour conversion cannot be replayed from "
                    f"its sealed page ({error})"
                ) from error
        expected_sha256 = digest_bytes(derived)
        if presented["image_sha256"] != expected_sha256:
            raise SchemaRefusal(
                "an adapter-crop presentation blob does not re-derive from its sealed page transform"
            )


def validate_unpresented_regions(payload: Any) -> list[str]:
    """Close the explicit list of bound proposal regions outside one presentation."""
    if not isinstance(payload, dict):
        raise SchemaRefusal("a Testimonium payload is not an object")
    unpresented = payload.get("unpresented_regions")
    if (
        not isinstance(unpresented, list)
        or any(not isinstance(region_id, str) or not region_id for region_id in unpresented)
        or len(set(unpresented)) != len(unpresented)
    ):
        raise SchemaRefusal(
            "a Testimonium's unpresented_regions is not a unique list of region ids"
        )
    if payload.get("presented") == {} and unpresented:
        raise SchemaRefusal(
            "a Testimonium with no presentation at all cannot name regions its presentation "
            "does not speak for"
        )
    return unpresented


def unpresented_region_ids(
    presented: dict[str, Any], proposal_regions: list[dict[str, Any]]
) -> list[str]:
    """Re-derive which bound proposal crops fall outside one presented image.

    The list is inapplicable to an empty presentation. For a real presentation,
    a proposal is expressible by this record exactly when it lies wholly inside
    the presentation's page-space bounds; changing presentation kind must not
    change that disclosure rule.
    """
    if presented == {}:
        return []
    if not isinstance(presented, dict):
        raise SchemaRefusal("a Testimonium presented block is not an object")
    page_id = presented.get("source_page_id")
    presented_bounds = presented.get("transform", {}).get("bounds")
    if not isinstance(page_id, str) or not isinstance(presented_bounds, dict):
        raise SchemaRefusal("a Testimonium presentation cannot locate its page-space bounds")

    unpresented: list[str] = []
    for region in proposal_regions:
        payload = region.get("payload") if isinstance(region, dict) else None
        transform = payload.get("transform") if isinstance(payload, dict) else None
        bounds = transform.get("bounds") if isinstance(transform, dict) else None
        region_id = payload.get("region_id") if isinstance(payload, dict) else None
        if not isinstance(region_id, str) or not region_id or not isinstance(bounds, dict):
            raise SchemaRefusal("a bound proposal region has no page-space identity to compare")
        contained = (
            transform.get("source_page_id") == page_id
            and presented_bounds["x"] <= bounds["x"]
            and presented_bounds["y"] <= bounds["y"]
            and presented_bounds["x"] + presented_bounds["w"] >= bounds["x"] + bounds["w"]
            and presented_bounds["y"] + presented_bounds["h"] >= bounds["y"] + bounds["h"]
        )
        if not contained:
            unpresented.append(region_id)
    return unpresented


def _truncation_from_stop_word(transport_stop_reason: str) -> tuple[bool | None, str]:
    """The three states the live response boundary can actually measure.

    The engine says one of three things about where a reading stopped, and this
    contract used to be able to record only two of them. Asking "is this word a
    cut-off word" made an engine that reported *nothing* indistinguishable from
    one that reported a natural stop, so a live Churro page whose wire carried
    no ``finish_reason`` could only be published as ``truncated: false`` — a
    completed boundary nobody observed, which GOVERNANCE 10 refuses. The third
    state is the honest one: unknown, and said so in the basis, exactly the
    shape `validate_content_health` already closes and the live boundary
    (`pipeline/3_attestatores/live_witness.py::_content_health`) already
    derives. Nothing about the two measured states changes, so a record written
    under the old two-valued rule reconciles identically.
    """

    if transport_stop_reason == STOP_REASON_UNREPORTED:
        return None, "not-recorded"
    return transport_stop_reason in _CHURRO_CUTOFF_STOP_REASONS, "trusted-response-boundary"


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
    page_role = payload["page_role"]
    if (
        payload["scope"] != "page"
        or not _integer(payload["page_ordinal"])
        # Page ordinals are 1-based everywhere; `validate_presented` already
        # refuses `source_page_ordinal < 1`, and a page record free to name 0 or
        # a negative page could not be reconciled against any sealed page.
        or payload["page_ordinal"] < 1
        or not isinstance(page_role, str)
        or page_role not in PAGE_ROLES
        or not isinstance(payload["unjoined_act_attempts"], list)
    ):
        raise SchemaRefusal("a page Testimonium has invalid page scope facts")
    validate_unpresented_regions(payload)
    validated = validate_native_witness_geometry(payload)
    # Which page the record says it speaks for, and which page it says it was
    # shown, are two separate fields. Left unreconciled, a record can name page
    # 2 while carrying page 5's presentation and page-5 observed boxes; a
    # consumer keying on `page_ordinal` would then read page-5 geometry as page
    # 2's (GOALS 5, ARCHITECTURE invariant 3). The Perlector checks this at its
    # own seam; closing it here means every consumer of the shared contract gets
    # it, including the Recensor's coverage derivation.
    presented = payload["presented"]
    if presented and presented["source_page_ordinal"] != payload["page_ordinal"]:
        raise SchemaRefusal(
            "a page Testimonium's presentation names a different page than the record. Its "
            "observed geometry would be attributed to ink the chair was never shown. Restore "
            "the page ordinal of the presentation actually served"
        )
    if "partition_disagreement" in payload:
        presented = payload["presented"]
        disagreement = validate_partition_disagreement(
            payload["partition_disagreement"],
            observed=payload["observed"],
            source_page_id=presented.get("source_page_id") if presented else None,
            testimonium_id=testimonium_id,
        )
        _validate_page_edge_overshoot_response_refs(
            disagreement["page_edge_overshoots"], payload.get("raw_response_refs")
        )
    if "native_capture" in payload:
        capture = validate_native_capture(payload["native_capture"])
        if capture["adapter"] == "churro.v1":
            parse = capture["parse"]
            parsed_text = parse.get("text")
            if parse["state"] == "parsed":
                if payload["payload"] != parsed_text:
                    raise SchemaRefusal(
                        "a Churro page Testimonium payload differs from its parsed native capture"
                    )
                truncated, truncation_basis = _truncation_from_stop_word(
                    capture["transport_stop_reason"]
                )
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
                        "a Churro page Testimonium health differs from its parsed native capture"
                    )
                # An empty page reading may be published as a confirmed blank
                # only when the boundary positively said the model finished.
                # Cut off and unreported both fail that test, for the same
                # reason and with the same consequence: the attempt carries a
                # reason and is not a blank anyone measured.
                interrupted_silence = truncated is not False and parsed_text == ""
                if interrupted_silence:
                    # The retired `reported` projection cannot smuggle a claimed
                    # absence any more; the closed schema refuses the key itself.
                    if not (isinstance(payload.get("reason"), str) and payload["reason"].strip()):
                        raise SchemaRefusal(
                            "a cut-off empty Churro page capture has no failed-attempt reason"
                        )
                elif "reason" in payload:
                    raise SchemaRefusal(
                        "a usable Churro page capture carries a failed-attempt reason"
                    )
            else:
                if payload["payload"] is not None:
                    raise SchemaRefusal("an unread Churro page capture claims retained page text")
                cut_off = capture["transport_stop_reason"] in _CHURRO_CUTOFF_STOP_REASONS
                # `failed` names its refusal in `reason`; `unrecognized-shape`
                # names the shape in `outcome`. One helper, so the record and
                # this check cannot describe the same capture differently.
                parse_refusal = native_parse_refusal(parse)
                basis = (
                    "response cut off by the provider "
                    f"({capture['transport_stop_reason']!r}); {parse_refusal}"
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
                        "an unparseable Churro page Testimonium health differs from its capture"
                    )
                reason = payload.get("reason")
                if not isinstance(reason, str) or not reason.strip() or parse_refusal not in reason:
                    raise SchemaRefusal(
                        "an unparseable Churro page capture has no reason naming its parser refusal"
                    )
    validate_retained_response_refs(payload, read_bytes=read_bytes)
    return validated


def validate_retained_response_refs(
    payload: dict[str, Any], *, read_bytes: Callable[[str], bytes] | None = None
) -> None:
    """Close a page partition's links to its retained native responses.

    A partition may derive from several responses, so references remain plural
    and ordered. The producing stage owns its blob namespace; this shared seam
    closes the reference shape and prevents quantization metadata from appearing
    without the bytes whose geometry it describes.
    """
    refs = payload.get("raw_response_refs")
    if refs is not None:
        if not isinstance(refs, list) or not refs:
            raise SchemaRefusal("a page Testimonium raw_response_refs is not a non-empty list")
        expected_prefix = f"{writing_directory(ATTESTATORES)}/blobs/sha256/"
        for reference in refs:
            if (
                not isinstance(reference, dict)
                or set(reference) != {"relative_path", "sha256"}
                or not isinstance(reference["relative_path"], str)
                or not reference["relative_path"]
                or not is_sha256(reference["sha256"])
                or reference["relative_path"] != expected_prefix + reference["sha256"]
            ):
                raise SchemaRefusal(
                    "a page Testimonium retained-response reference is not a closed blob reference"
                )
        if len({reference["sha256"] for reference in refs}) != len(refs):
            raise SchemaRefusal("a page Testimonium names one retained response twice")
        if read_bytes is not None:
            for reference in refs:
                try:
                    retained = read_bytes(reference["relative_path"])
                except OSError as error:
                    raise SchemaRefusal(
                        f"page Testimonium retained response {reference['relative_path']} could "
                        f"not be read: {error}"
                    ) from error
                if digest_bytes(retained) != reference["sha256"]:
                    raise SchemaRefusal(
                        f"page Testimonium retained response {reference['relative_path']} "
                        "differs from its digest"
                    )
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


# A declared, deliberately UNMEASURED routing rule.  Unit 10 records only the
# unambiguous zero-overlap case; calibrating a near-overlap threshold would be a
# measurement claim GOVERNANCE 10 does not permit until something has actually
# been measured.
UNROUTED_OBSERVATION_OVERLAP: Final = {"rule": "positive-area", "status": "unmeasured"}


def _overlaps(left: dict[str, int], right: dict[str, int]) -> bool:
    """Whether two page-pixel boxes share positive area, never containment."""
    return min(left["x"] + left["w"], right["x"] + right["w"]) > max(left["x"], right["x"]) and min(
        left["y"] + left["h"], right["y"] + right["h"]
    ) > max(left["y"], right["y"])


def validate_reportable_observations(observed: Any) -> list[dict[str, Any]]:
    """Close only the observation fields a coverage derivation indexes by name.

    `validate_observed` is the full contract and needs the presentation to
    check containment; a consumer deriving coverage from a retained record
    holds observations without necessarily re-deriving that presentation. What
    it does do is index `ordinal`, `bounds_source`, and `bounds` on every row,
    so a row that is not a closed observation leaves that consumer as a raw
    KeyError instead of a named refusal — from stages whose whole contract is
    that a fault arrives with its cause attached (GOVERNANCE 2).
    """
    if not isinstance(observed, list):
        raise SchemaRefusal("a Testimonium observed block is not a list")
    for item in observed:
        if not isinstance(item, dict):
            raise SchemaRefusal("a Testimonium observed entry is not an object")
        if not _integer(item.get("ordinal")):
            raise SchemaRefusal("a Testimonium observed entry has no integer ordinal")
        if item.get("bounds_source") not in BOUNDS_SOURCES:
            raise SchemaRefusal("a Testimonium observed box has an unknown bounds_source")
        # Only reported geometry is measured against proposals; a `presented`
        # echo is excluded before its box is read, so it is not required to be
        # a box the consumer would never compare.
        if item["bounds_source"] in REPORTED_BOUNDS_SOURCES:
            _bounds(item.get("bounds"), "a Testimonium observed box", page_size=None)
    return observed


def reported_geometry_overlaps(observed: list[dict[str, Any]], bounds: dict[str, int]) -> bool:
    """Presentation echoes never count as reported geometric overlap."""
    return any(
        observation.get("bounds_source") in REPORTED_BOUNDS_SOURCES
        and _overlaps(observation["bounds"], bounds)
        for observation in observed
    )


def split_page_edge_overshoots(
    observed: list[dict[str, Any]], *, page_size: tuple[int, int]
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Separate, never clamp, native boxes that run past a sealed page edge.

    A Chandra response can name several independent layout blocks.  One box
    whose quantized maximum edge exceeds the sealed page is not usable witness
    geometry: the ordinary observed-box wall would refuse it, and changing its
    edge to fit would falsely report a different box.  It also says nothing
    about the other blocks in the response.  Keep those valid blocks, with
    dense ordinals rebuilt from their surviving response order, and retain the
    rejected box as a named fact for the durable page partition.

    This helper intentionally accepts only already-derived, closed observation
    entries.  It is not a permissive alternate validator: malformed geometry
    still reaches the existing refusal path, and the returned overshoot keeps
    the exact quantized bounds rather than an in-page substitute.
    """
    if (
        not isinstance(page_size, tuple)
        or len(page_size) != 2
        or not all(_integer(value) and value > 0 for value in page_size)
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
        if not isinstance(item, dict) or set(item) != {
            "ordinal",
            "bounds",
            "bounds_source",
            "span",
        }:
            raise SchemaRefusal(
                "the page-edge check received an observed entry outside its closed schema. "
                "The rejected box could lose facts when converted into a finding. "
                "Restore the complete observed entry and run the page-edge derivation again."
            )
        if not _integer(item["ordinal"]) or item["ordinal"] != source_ordinal:
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


def unrouted_observations(
    testimonia: list[dict[str, Any]],
    proposal_regions: list[dict[str, Any]],
    *,
    prior_findings: set[tuple[str, int]] | None = None,
) -> list[dict[str, Any]]:
    """Named, non-fatal findings for reported ink no sealed proposal accounts for.

    The finding is coverage evidence, not a verdict on text or witness quality.
    It gives the Recensor's bounded fallback-recrop route the only legal next
    step; it never turns an observation into an act or moves an attempt ordinal.

    The denominator is every sealed proposal on the presented page, not one
    act's proposals; otherwise a neighboring act's ink becomes a false finding.

    Only reported geometry counts. A `bounds_source: "presented"` box restates
    the input image and must not create a finding about ink no witness reported.
    """
    prior_findings = prior_findings or set()
    proposal_boxes = [
        region["payload"]["transform"]
        for region in proposal_regions
        if region.get("payload", {}).get("origin") == "proposal"
    ]
    findings: list[dict[str, Any]] = []
    for testimony in testimonia:
        payload = testimony["payload"]
        presented = payload["presented"]
        if not presented:
            continue
        for observation in payload["observed"]:
            if observation.get("bounds_source") not in REPORTED_BOUNDS_SOURCES:
                continue
            key = (testimony["artifact_id"], observation["ordinal"])
            if key in prior_findings:
                continue
            bounds = observation["bounds"]
            overlaps = any(
                transform["source_page_id"] == presented["source_page_id"]
                and _overlaps(bounds, transform["bounds"])
                for transform in proposal_boxes
            )
            if not overlaps:
                findings.append(
                    {
                        "kind": "unrouted-observation",
                        "testimonium_id": testimony["artifact_id"],
                        "ordinal": observation["ordinal"],
                        "source_page_id": presented["source_page_id"],
                        "bounds": dict(bounds),
                        "overlap_rule": dict(UNROUTED_OBSERVATION_OVERLAP),
                    }
                )
    return findings


def partition_disagreement(
    testimonium: dict[str, Any],
    proposal_regions: list[dict[str, Any]],
    *,
    page_edge_overshoots: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Record page/chair partition facts without selecting any pairing.

    All positive-area pairings are retained.  Where one observation intersects
    multiple proposals, every competing pairing remains in the record; neither
    witness nor proposal wins a correspondence decision here.
    """
    payload = testimonium["payload"]
    presented = payload["presented"]
    page_id = presented.get("source_page_id") if isinstance(presented, dict) else None
    proposals = sorted(
        [
            dict(region["payload"]["transform"]["bounds"])
            for region in proposal_regions
            if region.get("payload", {}).get("origin") == "proposal"
            and region["payload"]["transform"].get("source_page_id") == page_id
        ],
        key=lambda box: (box["y"], box["x"], box["h"], box["w"]),
    )
    observations = [
        {
            "ordinal": observation["ordinal"],
            "bounds": dict(observation["bounds"]),
            "bounds_source": observation["bounds_source"],
        }
        for observation in payload.get("observed", [])
        if observation.get("bounds_source") in REPORTED_BOUNDS_SOURCES
    ]
    deltas, unobserved_proposals, ambiguous_pairings = _partition_pairing_facts(
        proposals, observations
    )
    return {
        "proposal_boxes": proposals,
        "observed_boxes": observations,
        "unclaimed_observations": unrouted_observations([testimonium], proposal_regions),
        "unobserved_proposals": unobserved_proposals,
        "boundary_deltas": deltas,
        "ambiguous": bool(ambiguous_pairings),
        "ambiguous_pairings": ambiguous_pairings,
        "overlap_rule": dict(UNROUTED_OBSERVATION_OVERLAP),
        "page_edge_overshoots": [] if page_edge_overshoots is None else page_edge_overshoots,
    }


def _partition_pairing_facts(
    proposals: list[dict[str, int]], observations: list[dict[str, Any]]
) -> tuple[list[dict[str, Any]], list[dict[str, int]], list[dict[str, Any]]]:
    """Ambiguity is symmetric, so neither side may choose a single pairing."""
    deltas: list[dict[str, Any]] = []
    pairing_keys: list[tuple[int, int, int, int]] = []
    # A tie is a tie from either side: one observation spanning several
    # proposals, or several observations each claiming the same proposal.
    # Counting matches per proposal as well as per observation is what makes
    # `ambiguous` answer "two observations tie" the way the geometry actually
    # ties, not only the direction the loop happens to walk first.
    observation_match_counts: dict[int, int] = {}
    proposal_match_counts: dict[tuple[int, int, int, int], int] = {}
    for observation in observations:
        matches = [proposal for proposal in proposals if _overlaps(observation["bounds"], proposal)]
        observation_match_counts[observation["ordinal"]] = len(matches)
        for proposal in matches:
            key = (proposal["x"], proposal["y"], proposal["w"], proposal["h"])
            proposal_match_counts[key] = proposal_match_counts.get(key, 0) + 1
            pairing = {
                "proposal_box": dict(proposal),
                "observed_ordinal": observation["ordinal"],
                "observed_box": dict(observation["bounds"]),
                "edge_offsets": {
                    "left": observation["bounds"]["x"] - proposal["x"],
                    "top": observation["bounds"]["y"] - proposal["y"],
                    "right": observation["bounds"]["x"]
                    + observation["bounds"]["w"]
                    - proposal["x"]
                    - proposal["w"],
                    "bottom": observation["bounds"]["y"]
                    + observation["bounds"]["h"]
                    - proposal["y"]
                    - proposal["h"],
                },
            }
            deltas.append(pairing)
            pairing_keys.append(key)
    ambiguous_pairings = [
        pairing
        for pairing, key in zip(deltas, pairing_keys, strict=True)
        if observation_match_counts[pairing["observed_ordinal"]] > 1
        or proposal_match_counts[key] > 1
    ]
    unobserved_proposals = [
        proposal
        for proposal in proposals
        if (proposal["x"], proposal["y"], proposal["w"], proposal["h"]) not in proposal_match_counts
    ]
    return deltas, unobserved_proposals, ambiguous_pairings


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
# `vendor_identity` is admitted and optional, for the reason `framing` is:
# every record written before this field existed is exactly what it was and
# stays valid, and no fixture digest moves the day the field is admitted. It
# becomes a fact each adapter *writes* when that adapter lands its vendor
# grammar (Chandra U9, Churro U10, DAI U11), and each adapter's own tests pin
# its presence for that chair -- which is where a per-chair requirement can be
# stated truthfully, since the three chairs do not have the same vendor
# artifacts: DAI publishes no inference code at all, so its identity names the
# weights repository and revision the carried files came from, where Chandra's
# and Churro's name a source repository and commit.
_NATIVE_CAPTURE_OPTIONAL_FIELDS: Final = frozenset({"vendor_identity"})
_VENDOR_IDENTITY_FIELDS: Final = frozenset({"repository", "sha", "carried_strings"})
#: The parser names a retained model view may record, one per vendor grammar.
#:
#: `html` is Chandra's `data-bbox` layout, `xml` Churro's HistoricalDocument,
#: `text` DAI's plain UTF-8. The vocabulary is enumerated rather than left as
#: "any non-empty string" because `verify_native_capture_bytes` re-derives a
#: capture *under the name the record carries*: a name no dispatcher answers to
#: is a record that can never be re-derived, and it would be discovered as a
#: `KeyError` at re-derivation rather than as a refusal at the seam that wrote
#: it (GOVERNANCE 2).
NATIVE_CAPTURE_PARSERS: Final = frozenset({"html", "xml", "text"})
#: The one name still written in this tree at this commit, admitted so this
#: contract can land ahead of the unit that retires it, and separate so that
#: retirement is a one-line deletion rather than an edit to the vocabulary
#: above. `json` is the committed fixture's Chandra placeholder
#: (`fixture-chandra-response.v1`), kept until U16 re-declares `proof/`'s rows
#: in the vendor grammars. It is not a vendor grammar and does not survive this
#: wave. Unit 12's `churro` live-posture dispatcher is gone: the Churro adapter
#: reads the vendor's own `HistoricalDocument` grammar under `xml`
#: (`common/churro_document.py`), in both postures, so there is one Churro
#: parser rather than a live one and a fixture one.
_TRANSITIONAL_CAPTURE_PARSERS: Final = frozenset({"json"})
_ADMITTED_CAPTURE_PARSERS: Final = NATIVE_CAPTURE_PARSERS | _TRANSITIONAL_CAPTURE_PARSERS
# `unrecognized-shape` is the state a parser reaches when it ran, read the
# whole response, and could name no shape it knows — distinct from `failed`,
# where the parser refused the bytes, and from `parsed`, where it produced
# text. `pipeline/3_attestatores/chandra.py` has produced it since it was
# written (the vendor publishes no response specimen, so a real Chandra body is
# a named surprise rather than a parse failure), and
# `feeding.retain_model_view` records it; this contract simply had no room for
# it, so the one state a live Chandra response actually reaches could not be
# attached to any record. Admitting it puts the retained model view back beside
# the bytes it describes instead of dropping the view and keeping only the
# blob.
_NATIVE_CAPTURE_PARSE_STATES: Final = frozenset(
    {"not-requested", "pending", "parsed", "failed", "unrecognized-shape"}
)
#: The post-hoc scan's own two findings. Separate from the grammar's, because
#: only one of them may appear more than never: a capture carries at most one
#: repetition finding, while a grammar answer can legitimately report several
#: facts about itself at once (an echo trimmed *and* a page whose ink lies
#: outside its sections).
_CHURRO_REPETITION_FINDING_KINDS: Final = frozenset(
    {"post-hoc-repetition", "post-hoc-repetition-uninspected"}
)
#: Everything a Churro capture's `findings` may name. The grammar's own half is
#: `common/churro_document.py`'s closed set, taken from it rather than restated
#: so a finding that module learns to report is not a finding this contract
#: silently refuses. Their internal shapes are closed where they are produced
#: (`validate_churro_document_parse`) and held exactly by
#: `verify_native_capture_bytes`, which re-derives the whole findings list from
#: the retained bytes and compares it; what this module closes here is the
#: vocabulary and the repetition findings' own fields.
_CHURRO_CAPTURE_FINDING_KINDS: Final = (
    _CHURRO_REPETITION_FINDING_KINDS | churro_document.DOCUMENT_FINDING_KINDS
)
_CHURRO_CUTOFF_STOP_REASONS: Final = frozenset({"length", "max_new_tokens"})
# `eos`/`stop`/`max_new_tokens` are the fixture transport's own vocabulary;
# `length` is vLLM's cut-off word for the same fact on the live wire.
# `STOP_REASON_UNREPORTED` (`common/contracts/serving.py`) is neither a fixture
# nor an engine word -- it is what a live page-scoped chair (Churro) retains
# when the wire carried no `finish_reason` at all, and it must be admitted
# here or every such live Churro response would fail `_validate_churro_capture`
# by name, indistinguishably from a genuinely unknown transport word.
_CHURRO_STOP_REASONS: Final = (
    frozenset({"eos", "stop"}) | _CHURRO_CUTOFF_STOP_REASONS | {STOP_REASON_UNREPORTED}
)


#: The parser name `derive_churro_capture` can run for this chair -- one name,
#: for one grammar, in both postures. Unit 12 carried two, `"xml"` for the
#: committed fixture's `<output>` bodies and `"churro"` for a live dispatcher
#: that also read this repository's own JSON coordinate contract. That contract
#: is retired (`pipeline/3_attestatores/churro.py` says why), so what is left is
#: the vendor's own grammar, and both postures read it through
#: `common/churro_document.py`. `verify_native_capture_bytes` re-derives under
#: the name the record was written with, and there is now exactly one branch for
#: it to reach.
CHURRO_PARSERS: Final = frozenset({churro_document.CHURRO_PARSER})


def parse_churro_response(raw: bytes, *, system_prompt: str | None = None) -> dict[str, Any]:
    """One Churro body, read under the vendor's own grammar and its two fallbacks.

    Returns `common/churro_document.py`'s own parse record -- state, parser,
    response byte count, findings, and for a parse the shape, text view, text,
    sections, marked spans and page count. This module adds exactly one thing to
    that call: the intake ceiling, `CHURRO_MAX_RESPONSE_BYTES`, which the
    grammar module deliberately declares none of so that one chair cannot end up
    with two of them.

    `system_prompt` is the exact string the request sent. Only that string is
    ever trimmed from the head of a response
    (`churro_document.trim_leading_prompt`, the vendor's own
    `run_churro_ocr.py` rule), and it is passed rather than guessed because
    trimming against a framing that was not sent could remove real
    transcription that happens to begin the same way.

    A body that is not bytes raises the grammar module's own named refusal
    rather than returning a record: both callers here have already bound bytes
    (`feeding.retain_model_view` and `verify_native_capture_bytes` each check
    the type before they reach this), so a non-bytes body at this seam is a
    programming error rather than one more thing a witness said.
    """

    return churro_document.parse_churro_document(
        raw, system_prompt=system_prompt, max_bytes=CHURRO_MAX_RESPONSE_BYTES
    )


def native_parse_refusal(parse: dict[str, Any]) -> str:
    """The one sentence a non-parsed native capture is described by.

    Two readers have to agree on it, or a record refuses itself: the live
    boundary composes an attempt's `reason` and unrecordable `truncation_basis`
    from it (`pipeline/3_attestatores/live_witness.py`), and this module's page
    validator re-derives both and compares. It was one inline expression in each
    place, identical by coincidence, and the second state Unit 12 admits --
    `unrecognized-shape`, whose evidence is `outcome` and not `reason` -- is
    where the coincidence would have ended in a `KeyError` from inside a
    contract check.
    """
    if parse["state"] == "failed":
        return parse["reason"]
    if parse["state"] == "unrecognized-shape":
        return f"the response shape was not recognized: {parse['outcome']}"
    # Never reached by either caller -- both take this path only after their
    # `parsed` branches have returned -- and stated rather than left to a
    # `KeyError` from inside a contract check, which is the failure mode this
    # helper exists to have removed once already.
    raise SchemaRefusal(
        f"a {parse['state']!r} native parse record carries no refusal to name; "
        "only a failed or unrecognized-shape parse describes one"
    )


def detect_repetition(raw: bytes | bytearray | str) -> dict[str, Any] | None:
    """Report a repeated tail after capture; this function has no generation input.

    Chair-neutral, and deliberately so. The scan was written for Churro because
    Churro was the first page-scoped chair in the tree, but nothing in it is
    Churro's: a degeneration loop is the dominant failure mode of every
    VLM-OCR reader, and the same tail-cycle fact has to be recordable for
    Chandra's HTML, DAI's plain text and the Perlector's own reading. The name
    said otherwise, so the next caller's choice was to import a function whose
    name asserts a chair it is not, or to write a second copy that would drift.
    `detect_churro_repetition` remains bound to this function, so every
    existing caller and every retained default keeps working unchanged.

    It never re-rolls, never scores, and never gates: it returns a finding the
    caller records beside the bytes (GOVERNANCE 11 -- recovery recovers
    coverage, not quality).

    Text is accepted directly as well as bytes. A caller that already holds the
    decoded reading -- the Perlector does -- should not have to re-encode it to
    ask this question, and a round trip through UTF-8 could only turn a string
    that cannot fail the decode into one that cannot fail it differently. Bytes
    behave exactly as they did: a body that is not UTF-8 is not scanned, and
    says so as a finding rather than as an exception.
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


#: The name this scan was published under while it belonged to one chair. Kept
#: as an alias rather than a wrapper so the two cannot diverge, and because
#: `derive_churro_capture`'s default argument and `feeding.py`'s import both
#: bind it by name.
detect_churro_repetition = detect_repetition


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

    Oversized bytes have already crossed the response boundary, so they remain
    evidence in the caller's blob store.  They are not handed to the parser or
    the detector; both unperformed operations are named in the retained facts.

    `parser` asks for the one grammar this chair has (`CHURRO_PARSERS`), or is
    `None` for a retention that asks for no parse at all. Any other name is
    refused rather than answered `not-requested`: a record naming a parser this
    seam did not run would be a finished attempt wearing the look of one nobody
    started (GOVERNANCE 2).

    The grammar's own findings travel with the reading, ahead of the post-hoc
    scan's. They are facts about this response that the page text alone cannot
    show -- a prompt echo trimmed off its head, a stray `&` escaped to make it
    parse, a page whose ink lies outside every section the vendor's walk
    reaches, a body that arrived in the retired `<output>` envelope nobody asked
    for.

    The repetition detector then inspects `parse["text"]` where a parse produced
    one and the raw bytes otherwise, naming which in the finding's `inspected`
    field -- unchanged, and the parsed text is the right input: repetition is a
    fact about what the model transcribed, not about the punctuation of the
    envelope or the indentation of the XML it arrived in.
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
            "stop_reason": (
                "partial-parse-failed" if parser is not None else transport_stop_reason
            ),
        }

    parse: dict[str, Any] = {"state": "not-requested", "parser": None}
    findings: list[dict[str, Any]] = []
    stop_reason = transport_stop_reason
    if parser is not None:
        document = document_parser(raw, system_prompt=system_prompt)
        # Copied, not aliased: this function promises mutable-free facts, and a
        # caller's own `document_parser` is free to hand back records it keeps.
        findings.extend(dict(finding) for finding in document["findings"])
        if document["state"] == "parsed":
            parse = {"state": "parsed", "parser": parser, "text": document["text"]}
        elif document["state"] == "failed":
            parse = {"state": "failed", "parser": parser, "reason": document["reason"]}
            stop_reason = "partial-parse-failed"
        else:
            # The grammar names an unplaceable shape in `reason`, and the whole
            # sentence becomes the capture's `outcome` rather than a short token
            # derived from it. It is the only thing that says *which* root
            # element arrived, and a record that dropped it would name the
            # surprise without naming what the surprise was (GOVERNANCE 2).
            parse = {
                "state": "unrecognized-shape",
                "parser": parser,
                "outcome": document["reason"],
            }
            stop_reason = "partial-parse-unrecognized-shape"
    parsed_text = parse.get("text")
    inspected, basis = (
        (parsed_text.encode("utf-8"), "parsed-text")
        if isinstance(parsed_text, str)
        else (raw, "raw-response")
    )
    if finding := repetition_detector(inspected):
        findings.append({**finding, "inspected": basis})
        # The parse outcome wins over a repeated tail, as `failed` already did:
        # a body this parser could not place is the more load-bearing fact about
        # the capture, and the repetition stays recorded in `findings` either
        # way, so nothing is lost by the precedence (GOVERNANCE 2).
        if finding["kind"] == "post-hoc-repetition" and parse["state"] not in {
            "failed",
            "unrecognized-shape",
        }:
            stop_reason = "partial-post-hoc-repetition-detected"
    return {"parse": parse, "findings": findings, "stop_reason": stop_reason}


def churro_capture_system_prompt(capture: dict[str, Any]) -> str | None:
    """The exact system string a retained Churro capture says its request sent.

    Read off the capture's own retained `view.prompt` rather than passed in, so
    a re-derivation trims the head of the response against the framing that
    reading was actually taken under. Every other route would be a guess: this
    chair has two attested framings, and a re-derivation that tried the wrong
    one could either leave an echo in the text or cut transcription out of it,
    and would then disagree with the record it is checking.

    ``None`` where no capture wrote one. `_validate_churro_capture` requires the
    key for a `churro.v1` capture, so that is a record this seam did not write.
    """

    view = capture.get("view")
    prompt = view.get("prompt") if isinstance(view, dict) else None
    system = prompt.get("system") if isinstance(prompt, dict) else None
    return system if isinstance(system, str) else None


def verify_native_capture_bytes(value: Any, raw: bytes) -> dict[str, Any]:
    """Verify one capture's derived record against its authoritative raw blob."""
    capture = validate_native_capture(value)
    if not isinstance(raw, bytes):
        raise SchemaRefusal("a page Testimonium raw response is not bytes")
    actual_digest = digest_bytes(raw)
    if actual_digest != capture["raw_response_ref"]["sha256"]:
        raise SchemaRefusal(
            "a page Testimonium raw response has digest "
            f"{actual_digest}, not its native capture digest "
            f"{capture['raw_response_ref']['sha256']}"
        )
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
    try:
        raw = tree.read_bytes(reference["relative_path"])
    except OSError as error:
        raise SchemaRefusal(
            f"a page Testimonium raw response could not be read: {error}"
        ) from error
    return verify_native_capture_bytes(capture, raw)


def _validate_churro_capture(value: dict[str, Any]) -> None:
    """Close the rules that belong to Churro alone, once the shared shape holds.

    `validate_native_capture` closes what every adapter's retained model view
    must carry. These are Churro's own: its transport reasons, its prompt and
    generation view, its terminal XML parse, its single repetition finding and
    the stop-reason arithmetic over the two. Keeping them here gives the next
    page adapter a visible slot for its own rules instead of one body where the
    shared closure and one adapter's specifics are stacked without a seam.
    """
    parse = value["parse"]
    state, parser, findings = parse["state"], parse.get("parser"), value["findings"]
    if value["transport_stop_reason"] not in _CHURRO_STOP_REASONS:
        raise SchemaRefusal(
            "a Churro page capture has an unknown transport stop reason "
            f"{value['transport_stop_reason']!r}"
        )
    view = value["view"]
    # `framing` is admitted and optional, and both halves of that are
    # deliberate. Optional, because every record written before this chair had
    # more than one framing is still exactly what it was and stays valid.
    # Admitted, because a live reading is now taken under one of several
    # declared framings (`pipeline/3_attestatores/churro.py::FRAMINGS`) and the
    # record has to be able to say which -- the prompt bytes beside it identify
    # the wording, but only a reader who already knows both wordings can tell
    # them apart, and a run's own name for the question it asked is the fact an
    # A/B compares on.
    if set(view) - {"framing"} != {"prompt", "generation"}:
        raise SchemaRefusal(
            "a Churro page capture does not retain exactly its prompt and generation view"
        )
    if "framing" in view and (not isinstance(view["framing"], str) or not view["framing"]):
        raise SchemaRefusal("a Churro page capture names a framing that is not a nonblank string")
    prompt, generation = view["prompt"], view["generation"]
    # `{system}` and nothing else. Both vendor-attested profiles set the user
    # prompt to `None` (`common/churro_document.py`), so the user turn carries
    # the image alone, and a retained view with a `user` member would be a
    # record of text this chair is never sent -- the retired two-message framing
    # this repository asked in until the vendor systems ruling.
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
        or not isinstance(generation["max_new_tokens"], int)
        or isinstance(generation["max_new_tokens"], bool)
        or generation["max_new_tokens"] != CHURRO_OUTPUT_TOKENS
    ):
        raise SchemaRefusal(
            f"a Churro page capture does not retain its {CHURRO_OUTPUT_TOKENS}-token bound"
        )
    # All three states the one grammar can reach. The `parser != "churro"` gate
    # Unit 12 put on `unrecognized-shape` is gone with the second parser name:
    # `xml` now reaches `common/churro_document.py`, which can conclude any of
    # the three, and a gate saying otherwise would refuse the vendor's own
    # well-formed answer under an unexpected root element.
    if state not in {"parsed", "failed", "unrecognized-shape"} or parser not in CHURRO_PARSERS:
        raise SchemaRefusal("a retained Churro page capture has no terminal parse record")
    repetitions = [
        finding for finding in findings if finding["kind"] in _CHURRO_REPETITION_FINDING_KINDS
    ]
    if len(repetitions) > 1:
        raise SchemaRefusal("a Churro page capture carries more than one repetition finding")
    for finding in findings:
        kind = finding["kind"]
        if kind not in _CHURRO_CAPTURE_FINDING_KINDS:
            raise SchemaRefusal(f"a Churro page capture has unknown finding kind {kind!r}")
        if kind not in _CHURRO_REPETITION_FINDING_KINDS:
            # A grammar finding. Its own closed shape belongs to the module that
            # produces it (`churro_document.validate_churro_document_parse`) and
            # is held exactly by `verify_native_capture_bytes`, which re-derives
            # the whole list from the retained bytes and compares. Restating the
            # per-kind field sets here would be a second copy free to drift from
            # the grammar that writes them.
            continue
        if kind == "post-hoc-repetition":
            if set(finding) != {"kind", "unit_characters", "repeats", "inspected"} or any(
                not isinstance(finding[field], int)
                or isinstance(finding[field], bool)
                or finding[field] <= 0
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
    expected_stop = value["transport_stop_reason"]
    if state == "failed":
        expected_stop = "partial-parse-failed"
    elif state == "unrecognized-shape":
        # Ordered after `failed` and before the repetition clause, matching
        # `derive_churro_capture`'s own precedence: the parse outcome is what
        # the stop reason names, and the repetition finding stays in `findings`.
        expected_stop = "partial-parse-unrecognized-shape"
    elif repeated:
        expected_stop = "partial-post-hoc-repetition-detected"
    if value["stop_reason"] != expected_stop:
        raise SchemaRefusal(
            "a Churro page capture stop reason disagrees with its parse and findings"
        )


def validate_vendor_identity(value: Any) -> dict[str, Any]:
    """Close which vendor pin the bytes beside this capture were taken from.

    GOVERNANCE 6 requires every stored reading to carry the resolved identity
    and revision of the *model* that produced it, and it already does. This is
    the other half of the same obligation once a chair runs its vendor's own
    system: the prompt bytes, the message shape and the output grammar are the
    vendor's, taken at one commit, and a re-parse under a different pin
    produces a different reading of the same retained response. Without the pin
    on the record that difference is invisible -- two Testimonia disagree and
    nothing says the second one read the bytes through another vendor's rules.

    Three fields, and each is load-bearing:

    * ``repository`` -- where the material came from. For Chandra and Churro
      that is the vendor's source repository; for DAI, which publishes no
      inference code, it is the weights repository its carried files ship in.
    * ``sha`` -- the exact 40-hex commit or revision, never a tag or a branch.
      A movable name would make the record's own claim unfalsifiable.
    * ``carried_strings`` -- the sha256 of each string this repository carries
      from that pin, by the name it is carried under, so a byte that changed in
      our tree is visible against the vendor commit that is supposed to have
      supplied it. This is the digest half of the offline byte-equality check
      in ``common/test_vendor_parity.py``; here it travels *with* the reading.

    ``carried_strings`` may not be empty. A vendor identity naming a repository
    and a commit and then no bytes at all records a provenance for nothing.
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
    reference = value["raw_response_ref"]
    if not isinstance(reference, dict) or set(reference) != {"relative_path", "sha256"}:
        raise SchemaRefusal("a page Testimonium native capture has no raw-response reference")
    # A shape check alone (any two non-empty strings) let a malformed or
    # forged reference stand as this record's own claim to be traceable back
    # to retained bytes (ARCHITECTURE invariant 2, GOALS 5) -- the same
    # `{relative_path, sha256}` shape is held to `is_sha256` everywhere else
    # this pipeline closes a blob reference; this was the one place it was not.
    if (
        not isinstance(reference["relative_path"], str)
        or not reference["relative_path"]
        or not is_sha256(reference["sha256"])
    ):
        raise SchemaRefusal(
            "a page Testimonium native capture has an invalid raw-response reference"
        )
    digest = reference["sha256"]
    expected_path = f"{writing_directory(ATTESTATORES)}/blobs/sha256/{digest}"
    if not is_sha256(digest) or reference["relative_path"] != expected_path:
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
        # `unrecognized-shape` names the shape it could not place in `outcome`,
        # where `failed` names the refusal in `reason` and `parsed` carries
        # `text`. Three states, three distinct third fields, so a record cannot
        # wear one state's clothing while carrying another's evidence.
        third = {"parsed": "text", "failed": "reason"}.get(state, "outcome")
        expected = {"state", "parser", third}
    if set(parse) != expected:
        raise SchemaRefusal("a page Testimonium native capture parse record has the wrong shape")
    # Three separate faults, three separate sentences. A parse nobody asked for
    # naming a parser, and a parse naming a parser no grammar answers to, have
    # different fixes -- and the second is the one that would otherwise surface
    # as a `KeyError` from inside `verify_native_capture_bytes` rather than as
    # a refusal at the seam that wrote it (GOVERNANCE 2).
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
    if value["adapter"] == "churro.v1":
        _validate_churro_capture(value)
    return value


def validate_partition_disagreement(
    value: Any,
    *,
    observed: Any = None,
    source_page_id: str | None = None,
    testimonium_id: str | None = None,
    proposal_boxes: list[dict[str, int]] | None = None,
) -> dict[str, Any]:
    """Close the retained facts without converting them into a verdict."""
    required = {
        "proposal_boxes",
        "observed_boxes",
        "unclaimed_observations",
        "unobserved_proposals",
        "boundary_deltas",
        "ambiguous",
        "ambiguous_pairings",
        "overlap_rule",
        "page_edge_overshoots",
    }
    if not isinstance(value, dict) or set(value) != required:
        raise SchemaRefusal("a page Testimonium partition_disagreement is not its closed schema")
    for field in ("proposal_boxes", "unobserved_proposals"):
        if not isinstance(value[field], list):
            raise SchemaRefusal(
                "a page Testimonium partition disagreement has malformed proposal boxes"
            )
        for box in value[field]:
            _bounds(box, "a page Testimonium partition proposal box", page_size=None)
    if proposal_boxes is not None and value["proposal_boxes"] != sorted(
        proposal_boxes,
        key=lambda box: (box["y"], box["x"], box["h"], box["w"]),
    ):
        raise SchemaRefusal(
            "a page Testimonium partition disagreement contradicts the sealed proposals on its page"
        )
    if not isinstance(value["observed_boxes"], list):
        raise SchemaRefusal(
            "a page Testimonium partition disagreement has malformed observed boxes"
        )
    for observation in value["observed_boxes"]:
        if not isinstance(observation, dict) or set(observation) != {
            "ordinal",
            "bounds",
            "bounds_source",
        }:
            raise SchemaRefusal("a page Testimonium partition observed box is malformed")
        if (
            not _integer(observation["ordinal"])
            or observation["bounds_source"] not in REPORTED_BOUNDS_SOURCES
        ):
            raise SchemaRefusal(
                "a page Testimonium partition observed box is not reported geometry"
            )
        _bounds(observation["bounds"], "a page Testimonium partition observed box", page_size=None)
    if observed is not None:
        expected_observed = [
            {
                "ordinal": observation["ordinal"],
                "bounds": dict(observation["bounds"]),
                "bounds_source": observation["bounds_source"],
            }
            for observation in observed
            if observation.get("bounds_source") in REPORTED_BOUNDS_SOURCES
        ]
        if value["observed_boxes"] != expected_observed:
            raise SchemaRefusal(
                "a page Testimonium partition disagreement contradicts its observed geometry"
            )
    if value["overlap_rule"] != UNROUTED_OBSERVATION_OVERLAP:
        raise SchemaRefusal(
            "a page Testimonium partition disagreement changes its declared overlap rule"
        )
    if not isinstance(value["ambiguous"], bool):
        raise SchemaRefusal(
            "a page Testimonium partition disagreement ambiguous flag is not boolean"
        )
    for field in ("unclaimed_observations", "boundary_deltas", "ambiguous_pairings"):
        if not isinstance(value[field], list):
            raise SchemaRefusal(
                "a page Testimonium partition disagreement has malformed retained facts"
            )
    overshoots = value["page_edge_overshoots"]
    if not isinstance(overshoots, list):
        raise SchemaRefusal(
            "the page Testimonium partition disagreement has malformed page-edge findings. "
            "The rejected witness geometry cannot be accounted from this value. "
            "Rebuild the partition disagreement with a list of closed findings."
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
            or not _integer(finding["ordinal"])
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
    expected_deltas, expected_unobserved, expected_ambiguous = _partition_pairing_facts(
        value["proposal_boxes"], value["observed_boxes"]
    )
    if value["unobserved_proposals"] != expected_unobserved:
        raise SchemaRefusal(
            "a page Testimonium partition disagreement contradicts its unobserved proposals"
        )
    if value["boundary_deltas"] != expected_deltas:
        raise SchemaRefusal(
            "a page Testimonium partition disagreement contradicts its boundary deltas"
        )
    if value["ambiguous_pairings"] != expected_ambiguous or value["ambiguous"] != bool(
        expected_ambiguous
    ):
        raise SchemaRefusal(
            "a page Testimonium partition disagreement contradicts its ambiguous pairings"
        )
    expected_unclaimed = [
        observation
        for observation in value["observed_boxes"]
        if not any(
            _overlaps(observation["bounds"], proposal) for proposal in value["proposal_boxes"]
        )
    ]
    if len(value["unclaimed_observations"]) != len(expected_unclaimed):
        raise SchemaRefusal(
            "a page Testimonium partition disagreement contradicts its unclaimed observations"
        )
    for finding, observation in zip(
        value["unclaimed_observations"], expected_unclaimed, strict=True
    ):
        if (
            not isinstance(finding, dict)
            or set(finding)
            != {
                "kind",
                "testimonium_id",
                "ordinal",
                "source_page_id",
                "bounds",
                "overlap_rule",
            }
            or finding["kind"] != "unrouted-observation"
            or not isinstance(finding["testimonium_id"], str)
            or not finding["testimonium_id"]
            or (testimonium_id is not None and finding["testimonium_id"] != testimonium_id)
            or finding["ordinal"] != observation["ordinal"]
            or (source_page_id is not None and finding["source_page_id"] != source_page_id)
            or finding["bounds"] != observation["bounds"]
            or finding["overlap_rule"] != UNROUTED_OBSERVATION_OVERLAP
        ):
            raise SchemaRefusal(
                "a page Testimonium partition disagreement has a malformed unclaimed observation"
            )
    return value


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
    # Closed before it is indexed. `reference["sha256"]` over a malformed entry
    # raised KeyError or TypeError straight through this validator, and an
    # escaping builtin is not the named refusal a page Testimonium's provenance
    # contract owes its reader.
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
