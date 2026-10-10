"""Whether one reading request fits the sealed serving row it would be sent to.

A Qwen-VL chair spends prompt tokens on the image before the text, decided by
its own ``smart_resize`` against the row's ``min_pixels``/``max_pixels``.  A
row whose ``max_model_len`` cannot hold image + prompt + answer makes vLLM
answer HTTP 400 before generating anything, on a card that bills by the hour.
This module computes the exact count and refuses locally instead; it never
downscales, clamps or reserves.

:func:`smart_resize` is a rewrite of the published formula (source named in
:data:`SMART_RESIZE_SOURCE`); the token count is the processor's own
``image_grid_thw.prod() // merge_size**2``.  Patch and merge sizes differ by
chair (784 px per token on Qwen2.5-VL, 1,024 on Qwen3-VL), so they are never
defaulted.

No tokenizer runs here (no ``torch`` wheel for this host), so fixed prompts
carry a measured constant sealed to a digest of the prompt text, and editing
the prompt invalidates it.  The Perlector's page prompt is built at run time and
is not fully measured: it is admitted on an upper bound for the text witnesses
and detectors reported (one token per UTF-8 byte) plus the builder's own fixed
wording at a measured tokens-per-character rate, carried to it
(:data:`PROMPT_TOKENS_REPORTED_BYTES_FIXED_CARRIED`).

Whether vLLM's own prompt assembly agrees with these counts token for token has
never been observed; only a pod can settle it.
"""

from __future__ import annotations

import math
import re
import unicodedata
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any, Final, Iterable, Mapping, Sequence

from common.contracts.canonical import digest_bytes, is_plain_int
from common.contracts.errors import SchemaRefusal
from common.imaging import dimensions

SCHEMA: Final = "verbatus-request-capacity.v1"

SMART_RESIZE_SOURCE: Final = (
    "transformers/models/qwen2_vl/image_processing_qwen2_vl.py::smart_resize "
    "(read at transformers 5.16.1, the version installed on the measuring host; "
    "the recipes pin 5.14.1, whose source has never been on this disk and has "
    "not been compared)"
)

# The library's own aspect-ratio refusal, mirrored.
MAX_ASPECT_RATIO: Final = 200


class RequestCapacityRefusal(SchemaRefusal):
    """A request cannot be sent as shaped: the sealed row cannot hold it.

    ``capacity`` carries the closed record (:data:`SCHEMA`) so the caller can
    publish the arithmetic rather than restate it from the message.
    """

    def __init__(self, message: str, *, capacity: Mapping[str, Any] | None = None) -> None:
        self.capacity = dict(capacity) if capacity is not None else None
        super().__init__(message)


# --- the arithmetic -------------------------------------------------------------


def smart_resize(
    height: int, width: int, *, factor: int, min_pixels: int, max_pixels: int
) -> tuple[int, int]:
    """The resized ``(height, width)`` a Qwen-VL image processor would use.

    A line-for-line rewrite of :data:`SMART_RESIZE_SOURCE`, rounding included:
    ``round`` is banker's rounding in both.
    """

    if height <= 0 or width <= 0:
        raise RequestCapacityRefusal(
            f"an image of {width}x{height} pixels has no token cost to compute; a request "
            "cannot be checked against a row it never names a real image for"
        )
    if max(height, width) / min(height, width) > MAX_ASPECT_RATIO:
        raise RequestCapacityRefusal(
            f"an image of {width}x{height} pixels has an absolute aspect ratio of "
            f"{max(height, width) / min(height, width):.1f}, which the chair's own image "
            f"processor refuses above {MAX_ASPECT_RATIO}; it would be refused by the engine "
            "before it was read, so it is refused here"
        )
    h_bar = round(height / factor) * factor
    w_bar = round(width / factor) * factor
    if h_bar * w_bar > max_pixels:
        beta = math.sqrt((height * width) / max_pixels)
        h_bar = max(factor, math.floor(height / beta / factor) * factor)
        w_bar = max(factor, math.floor(width / beta / factor) * factor)
    elif h_bar * w_bar < min_pixels:
        beta = math.sqrt(min_pixels / (height * width))
        h_bar = math.ceil(height * beta / factor) * factor
        w_bar = math.ceil(width * beta / factor) * factor
    return h_bar, w_bar


def _resize(
    width: int, height: int, min_pixels: int, max_pixels: int, patch_size: int, merge_size: int
) -> tuple[int, int, int]:
    """``(factor, resized_height, resized_width)`` with every geometry term checked positive."""
    factor = _positive(patch_size, "patch_size") * _positive(merge_size, "merge_size")
    resized_height, resized_width = smart_resize(
        height,
        width,
        factor=factor,
        min_pixels=_positive(min_pixels, "min_pixels"),
        max_pixels=_positive(max_pixels, "max_pixels"),
    )
    return factor, resized_height, resized_width


def _is_positive_int(value: object) -> bool:
    return is_plain_int(value) and value > 0


def _positive(value: object, field: str) -> int:
    if not _is_positive_int(value):
        raise RequestCapacityRefusal(
            f"{field} must be a positive integer to compute an image's token cost, not "
            f"{value!r}; nothing here defaults it"
        )
    return value


# --- the sealed row -------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class RowImageGeometry:
    """The four numbers a row must state before any image cost can be computed."""

    min_pixels: int
    max_pixels: int
    patch_size: int
    merge_size: int


def row_image_geometry(profile: Any) -> RowImageGeometry:
    """The sealed row's own image geometry, or a refusal naming the row.

    No field is defaulted: any default would be wrong for two of the four
    chairs.  ``operations/serving/manager.py::assert_processor_geometry``
    checks the row against the model snapshot's own processor configuration.
    """

    values: dict[str, int] = {}
    missing: list[str] = []
    for field in ("min_pixels", "max_pixels", "patch_size", "merge_size"):
        value = getattr(profile, field, None)
        if not _is_positive_int(value):
            missing.append(field)
        else:
            values[field] = value
    if missing:
        raise RequestCapacityRefusal(
            f"the sealed serving row ({_row_name(profile)}) states no positive "
            f"{', '.join(missing)}, so nothing here can say what one image costs this chair "
            "in prompt tokens; a patch or merge size is never assumed -- the Qwen2.5-VL "
            "chairs spend 784 px per token and the Qwen3-VL chairs 1,024, and a default "
            "would mis-count by a third"
        )
    if values["min_pixels"] > values["max_pixels"]:
        raise RequestCapacityRefusal(
            f"the sealed serving row ({_row_name(profile)}) states min_pixels "
            f"{values['min_pixels']} above max_pixels {values['max_pixels']}"
        )
    return RowImageGeometry(**values)


def _image_record(width: int, height: int, geometry: RowImageGeometry) -> dict[str, int]:
    """One embedded image's size, the size the chair sees, and its prompt-token cost."""
    factor, resized_height, resized_width = _resize(
        width,
        height,
        geometry.min_pixels,
        geometry.max_pixels,
        geometry.patch_size,
        geometry.merge_size,
    )
    return {
        "width": width,
        "height": height,
        "resized_width": resized_width,
        "resized_height": resized_height,
        "image_prompt_tokens": (resized_height // factor) * (resized_width // factor),
    }


def row_context_length(profile: Any) -> int:
    """The sealed row's ``max_model_len``, or a refusal naming the row."""

    value = getattr(profile, "max_model_len", None)
    if not _is_positive_int(value):
        raise RequestCapacityRefusal(
            f"the sealed serving row ({_row_name(profile)}) states no positive max_model_len, "
            "so nothing here can say what request length the engine accepts"
        )
    return value


def _row_name(profile: Any) -> str:
    return (
        f"recipe={getattr(profile, 'recipe', None)!r}, "
        f"chair={getattr(profile, 'chair', None)!r}, "
        f"tier={getattr(profile, 'tier', None)!r}"
    )


# --- the closed record ----------------------------------------------------------

CAPACITY_RECORD_FIELDS: Final = frozenset(
    {
        "schema",
        "recipe",
        "chair",
        "tier",
        "max_model_len",
        "min_pixels",
        "max_pixels",
        "patch_size",
        "merge_size",
        "images",
        "image_prompt_tokens",
        "prompt_tokens",
        "prompt_tokens_basis",
        "prompt_tokens_floor",
        "prompt_tokens_floor_basis",
        "answer_budget",
        "need",
        "headroom",
        "fits",
        "reason",
    }
)

# How ``prompt_tokens`` was arrived at; no tokenizer runs here, so a receipt
# must say.
PROMPT_TOKENS_MEASURED_CONSTANT: Final = "measured-constant-for-this-prompt-version"
PROMPT_TOKENS_MEASURED_FLOOR: Final = "measured-floor-for-this-prompt-shape"
# The page prompt's charge (`perlector_page_prompt_bound`): every string a witness
# or detector wrote at one token per UTF-8 byte, an upper bound for a byte-level
# BPE tokenizer; the builder's own fixed wording at a measured rate carried to
# it, never measured on the page builder itself.
PROMPT_TOKENS_REPORTED_BYTES_FIXED_CARRIED: Final = (
    "reported-text-per-byte-fixed-text-at-carried-act-rate"
)
# A text-only prompt charged whole at one token per UTF-8 byte, an upper bound for
# a byte-level BPE tokenizer whatever the text, with the chat template's cost.
PROMPT_TOKENS_ALL_TEXT_PER_BYTE: Final = "all-text-per-byte"
PROMPT_TOKENS_BASES: Final = frozenset(
    {
        PROMPT_TOKENS_MEASURED_CONSTANT,
        PROMPT_TOKENS_MEASURED_FLOOR,
        PROMPT_TOKENS_REPORTED_BYTES_FIXED_CARRIED,
        PROMPT_TOKENS_ALL_TEXT_PER_BYTE,
    }
)

# A floor may explain a refusal but never admit a request.
PROMPT_TOKENS_ADMITTING_BASES: Final = frozenset(
    {
        PROMPT_TOKENS_MEASURED_CONSTANT,
        PROMPT_TOKENS_REPORTED_BYTES_FIXED_CARRIED,
        PROMPT_TOKENS_ALL_TEXT_PER_BYTE,
    }
)


def request_fits(
    row: Any,
    images: Sequence[tuple[int, int]],
    prompt_tokens: int,
    answer_budget: int,
    *,
    prompt_tokens_basis: str = PROMPT_TOKENS_MEASURED_CONSTANT,
    prompt_tokens_floor: int | None = None,
    prompt_tokens_floor_basis: str | None = None,
) -> dict[str, Any]:
    """The closed capacity record for one request against one sealed row.

    ``images`` are ``(width, height)`` in the pixels actually embedded.
    ``prompt_tokens`` decides admission, so its basis must be in
    :data:`PROMPT_TOKENS_ADMITTING_BASES`.  ``prompt_tokens_floor`` is recorded
    only, never compared with the admitted count.

    A request that does not fit returns ``fits: False`` with a reason; this
    raises only when the row cannot state what the check needs.
    """

    geometry = row_image_geometry(row)
    max_model_len = row_context_length(row)
    prompt_tokens = _nonnegative(prompt_tokens, "prompt_tokens")
    answer_budget = _nonnegative(answer_budget, "answer_budget")
    if prompt_tokens_basis not in PROMPT_TOKENS_BASES:
        raise RequestCapacityRefusal(
            f"prompt_tokens_basis {prompt_tokens_basis!r} is not one of "
            f"{sorted(PROMPT_TOKENS_BASES)}; a capacity record says how its prompt count was "
            "arrived at, and an unnamed basis would let an estimate read as a measurement"
        )
    if prompt_tokens_basis not in PROMPT_TOKENS_ADMITTING_BASES:
        raise RequestCapacityRefusal(
            f"prompt_tokens_basis {prompt_tokens_basis!r} is a lower bound on this prompt, and "
            "a request is never admitted on one: it says only that a request costs at least "
            f"this much. Admission rests on {sorted(PROMPT_TOKENS_ADMITTING_BASES)}; a floor "
            "travels beside it on prompt_tokens_floor"
        )
    if prompt_tokens_floor is None:
        if prompt_tokens_floor_basis is not None:
            raise RequestCapacityRefusal(
                f"a capacity record names a prompt-floor basis {prompt_tokens_floor_basis!r} "
                "with no floor to attach it to"
            )
    else:
        prompt_tokens_floor = _nonnegative(prompt_tokens_floor, "prompt_tokens_floor")
        if prompt_tokens_floor_basis not in PROMPT_TOKENS_BASES:
            raise RequestCapacityRefusal(
                f"prompt_tokens_floor_basis {prompt_tokens_floor_basis!r} is not one of "
                f"{sorted(PROMPT_TOKENS_BASES)}; a recorded floor says how it was arrived at "
                "exactly as the admitted count does"
            )

    image_records = [_image_record(width, height, geometry) for width, height in images]
    image_total = sum(entry["image_prompt_tokens"] for entry in image_records)
    need = image_total + prompt_tokens + answer_budget
    headroom = max_model_len - need
    fits = headroom >= 0
    record = {
        "schema": SCHEMA,
        "recipe": getattr(row, "recipe", None),
        "chair": getattr(row, "chair", None),
        "tier": getattr(row, "tier", None),
        "max_model_len": max_model_len,
        "min_pixels": geometry.min_pixels,
        "max_pixels": geometry.max_pixels,
        "patch_size": geometry.patch_size,
        "merge_size": geometry.merge_size,
        "images": image_records,
        "image_prompt_tokens": image_total,
        "prompt_tokens": prompt_tokens,
        "prompt_tokens_basis": prompt_tokens_basis,
        "prompt_tokens_floor": prompt_tokens_floor,
        "prompt_tokens_floor_basis": prompt_tokens_floor_basis,
        "answer_budget": answer_budget,
        "need": need,
        "headroom": headroom,
        "fits": fits,
        "reason": None
        if fits
        else (
            f"{len(image_records)} image(s) cost {image_total} prompt tokens, the prompt "
            f"{prompt_tokens}, and the answer needs {answer_budget}; that is {need} against a "
            f"max_model_len of {max_model_len}, over by {-headroom}"
        ),
    }
    return record


def refuse_unless_it_fits(
    row: Any,
    images: Sequence[tuple[int, int]],
    prompt_tokens: int,
    answer_budget: int,
    *,
    what: str,
    prompt_tokens_basis: str = PROMPT_TOKENS_MEASURED_CONSTANT,
    prompt_tokens_floor: int | None = None,
    prompt_tokens_floor_basis: str | None = None,
) -> dict[str, Any]:
    """The capacity record, or :class:`RequestCapacityRefusal` carrying it.

    For call sites that refuse; a site that holds reads ``record["fits"]``.
    """

    record = request_fits(
        row,
        images,
        prompt_tokens,
        answer_budget,
        prompt_tokens_basis=prompt_tokens_basis,
        prompt_tokens_floor=prompt_tokens_floor,
        prompt_tokens_floor_basis=prompt_tokens_floor_basis,
    )
    if not record["fits"]:
        raise RequestCapacityRefusal(
            f"{what} does not fit the sealed serving row ({_row_name(row)}): {record['reason']}. "
            "Nothing was sent and nothing was downscaled: the image would have been refused by "
            "the engine before it was read",
            capacity=record,
        )
    return record


def _nonnegative(value: object, field: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise RequestCapacityRefusal(
            f"{field} must be a non-negative integer, not {value!r}; a capacity record never "
            "defaults a count it was not given"
        )
    return value


# --- the measured prompt and answer costs ---------------------------------------


@dataclass(frozen=True, slots=True)
class SealedPromptTokens:
    """One chair's measured prompt cost, bound to the exact text it was measured over.

    ``prompt_digest`` expires the constant when the prompt is edited.
    ``repo``/``revision`` name the tokenizer and are kept structured because a
    test reconciles them with ``config/models-real.toml``, so repointing the
    chair expires the measurement too.
    """

    tokens: int
    prompt_digest: str
    repo: str
    revision: str

    @property
    def measured_by(self) -> str:
        """The pair as one sentence, for a refusal message to quote."""

        return f"the tokenizer of {self.repo} at {self.revision}"


def prompt_digest(*texts: str) -> str:
    """The digest of one prompt's exact text parts, in the order they are sent."""

    return digest_bytes("\x00".join(texts).encode("utf-8"))


# Each chair's prompt cost, rendered through the model's own chat template with
# the real tokenizer at the pinned revision (`transformers` 5.16.1, the
# measuring host's; the lock pins 5.14.1), image placeholder expanded and image
# tokens subtracted.  One entry per prompt the chair can send, matched by
# digest: Churro has two framings (`pipeline/3_attestatores/churro.py::FRAMINGS`).
# Message order does not change the count.
MEASURED_PROMPT_TOKENS: Final[Mapping[str, tuple[SealedPromptTokens, ...]]] = MappingProxyType(
    {
        # Chandra's own `OCR_LAYOUT_PROMPT` (`common/chandra_layout.py`).  Its
        # size is the vendor's and is not to be trimmed: trimmed bytes would no
        # longer be the vendor's prompt.
        "attestator_1": (
            SealedPromptTokens(
                tokens=593,
                prompt_digest="025935f3e1de1acdfadd4c7d581ab17eb82e8caaffef7b64962621c80b7ca9a8",
                repo="datalab-to/chandra-ocr-2",
                revision="af93b47dba1b47b6640c86ccf487ed2260ab9a09",
            ),
        ),
        "attestator_2": (
            SealedPromptTokens(
                tokens=84,
                prompt_digest="9601ebe46918c76ac3f8d094b602ffd6303cc5bf51d5973e5eff2ad93cff964a",
                repo="Teklia/Qwen2.5-VL-7B-DAI-CReTDHI-RecordGold-ATR",
                revision="e371095d4ffe585f31f4974462931ddbac61ff64",
            ),
        ),
        # The two vendor framings (`common/churro_document.py`): 27 for the
        # default `registry-v0.3.0`, 29 for `paper-harness-ed09bc7`, whose two
        # spelling errors cost the extra tokens.
        "attestator_3": (
            SealedPromptTokens(
                tokens=27,
                prompt_digest="13592f5580805cf12d2aa14c963b872e3a4e6a5834e5d2afd7b86effd42a8b4d",
                repo="stanford-oval/churro-3B",
                revision="ca2150ea465d5a3d67818c50e234b9422619c75d",
            ),
            SealedPromptTokens(
                tokens=29,
                prompt_digest="dd7408ca72cf94f724b0522806427533a746f08cfa0cfd047e0272ebc4c4b489",
                repo="stanford-oval/churro-3B",
                revision="ca2150ea465d5a3d67818c50e234b9422619c75d",
            ),
        ),
    }
)

# The rate the page prompt's fixed wording is charged at: tokens per character
# over 168 Perlector prompts rendered with the pinned tokenizer and chat template
# (1/3/5 testimonia, 0-1,200 words each).  The ratio falls as the prompt grows, so
# the sealed value is the maximum, 0.4126394, rounded up.
PERLECTOR_BOUND_TOKENS_PER_10K_CHARACTERS: Final = 4127
# Kept apart from the ratio so measurement and margin stay visible.
PERLECTOR_BOUND_SAFETY_MARGIN: Final = (105, 100)
# The Perlector chat template's measured cost of one turn, and of each image in it.
CHAT_TURN_TOKENS: Final = 52
CHAT_IMAGE_TOKENS: Final = 2
# The tokenizer the Perlector's rate and chat-template costs above were measured with.
# A test reconciles it with `config/models-real.toml`, as it does each
# `SealedPromptTokens`, so repointing the Perlector fails CI until it is measured again.
PERLECTOR_MEASURED_TOKENIZER: Final = (
    "Qwen/Qwen3.8-27B",
    "1d4bf0f2ff6012fd82039f2fa52739d0dd7c60c0",
)


def sealed_prompt_tokens(chair: str, *texts: str) -> int:
    """The measured prompt-token count for this chair's fixed prompt, digest-checked."""

    entries = MEASURED_PROMPT_TOKENS.get(chair)
    if entries is None:
        raise RequestCapacityRefusal(
            f"chair {chair!r} has no measured prompt-token count; no tokenizer is available "
            "offline in this environment, so a request for this chair cannot be checked "
            f"against a row until one is measured (the measured chairs are "
            f"{sorted(MEASURED_PROMPT_TOKENS)})"
        )
    digest = prompt_digest(*texts)
    for entry in entries:
        if digest == entry.prompt_digest:
            return entry.tokens
    measured = ", ".join(f"{entry.tokens} over {entry.prompt_digest}" for entry in entries)
    raise RequestCapacityRefusal(
        f"chair {chair!r} sends a prompt whose digest is {digest}, but its measured "
        f"prompt-token count(s) were taken over {measured} with {entries[0].measured_by}; "
        "the prompt changed after it was measured, or it is a framing nobody has measured, "
        "and a request is never checked against the token cost of text nobody sends. "
        "Re-measure the prompt and update common/request_capacity.py"
    )


def _text_bytes(text: str) -> int:
    return len(unicodedata.normalize("NFC", text).encode("utf-8"))


def _rate_bound_tokens(characters: int) -> int:
    """The measured tokens-per-character bound, margin included, rounded up."""
    margin_numerator, margin_denominator = PERLECTOR_BOUND_SAFETY_MARGIN
    return -(
        -characters
        * PERLECTOR_BOUND_TOKENS_PER_10K_CHARACTERS
        * margin_numerator
        // (10_000 * margin_denominator)
    )


# --- the whole-page Perlector request -------------------------------------------
#
# One call reads a whole page: the page image, the text
# `common/page_prompt.py` renders from the page feed, and one JSON
# answer covering every act on the page.
#
# No tokenizer has measured the page prompt, so it is charged in two parts. Every
# string a witness or detector wrote -- a unit's text or label, a witness label,
# a block label, exactly as rendered (`page_prompt.prompt_parts`) -- costs one
# token per UTF-8 byte, as sent or NFC-normalized, whichever is more: Qwen's
# tokenizer is a byte-level BPE, so no text costs more tokens than its bytes,
# whatever a witness wrote (a runaway loop, digits, rare scripts). The rest is
# the builder's own fixed wording, ids and numbers: every whitespace-separated
# run of it holding an ASCII digit is charged per byte too, and the remaining
# prose at the measured rate (`PERLECTOR_BOUND_TOKENS_PER_10K_CHARACTERS` with its
# margin), carried to it (`PROMPT_TOKENS_REPORTED_BYTES_FIXED_CARRIED`).
# The carried rate is sealed against the page builder's own digest, so editing
# the builder expires it. Re-sealed 2026-10-10 for the recipe aliases
# (`page_prompt.RECIPE_ALIASES`): a table of recipe names only, no wording, so every
# rendered prompt is byte-identical (cold73: 73/73) and the rate carries unchanged.
# Re-sealed again for the page-type and entry-kind instruction (`[feed] page_types`):
# 442 more prose tokens on the dense test page, charged at the same carried rate, which
# was measured on this builder's English prose and is not re-measured here.
PERLECTOR_PAGE_PROMPT_TEMPLATE_DIGEST: Final = (
    "4dd2e4a5ac81daa87eee01cd08b850a49cc21edd508000f3c7998439a1b16dfd"
)
# Chat-template cost: one turn plus each image, charged at the most a
# page request sends -- the page render and its overlay (`[feed] page_overlay`).
PERLECTOR_PAGE_MAX_IMAGES: Final = 2
PERLECTOR_PAGE_PROMPT_OVERHEAD_TOKENS: Final = (
    CHAT_TURN_TOKENS + CHAT_IMAGE_TOKENS * PERLECTOR_PAGE_MAX_IMAGES
)

# The page answer's reserve. The answer transcribes the same ink the witnesses
# read, so its text is estimated at the page's longest witness text, each act
# entry adds its JSON scaffold -- this skeleton, one entry with an empty text, a
# three-word label and five cites -- and each Surya line shown adds one cite of
# its own, since lines are cited one by one, never by a range. All are
# estimated at the carried rate. The
# reserve decides admission only: it is the estimate or the page cap, whichever
# is smaller (`reserve_clamped` records when the cap won, as for a looping
# witness whose text runs far past any real page). It never bounds the reply:
# the `max_tokens` sent is the page cap or the context the prompt leaves,
# whichever is smaller (`page_answer_max_tokens`), since a dense index page
# legitimately answers far past any estimate of it. A reply that runs away is
# stopped by the sealed repetition-loop guard instead (`common/repetition_loop.py`).
PAGE_ANSWER_ENTRY_SKELETON: Final = (
    '{"n": 99, "kind": "other", "label": "baptism of a child", '
    '"cites": ["A99", "B99", "C99", "D100-D199", "S99"], "text": "", '
    '"continues_from_previous_page": false, "continues_to_next_page": false}, '
)
PAGE_ANSWER_WRAPPER: Final = '{"acts": [], "set_aside": []}'
PAGE_ANSWER_LINE_CITE: Final = '"L999", '


_WHITESPACE_RUN_SPLIT: Final = re.compile(r"(\s+)")
_ASCII_DIGIT: Final = re.compile(r"[0-9]")


def _reported_bytes(text: str) -> int:
    """One token per UTF-8 byte, as sent or NFC-normalized, whichever is more."""
    return max(len(text.encode("utf-8")), _text_bytes(text))


def page_prompt_charge(parts: Sequence[tuple[str, bool]]) -> tuple[int, int]:
    """``(byte_charged_bytes, rate_charged_characters)`` of one page prompt.

    ``parts`` are the prompt's pieces in order, each ``(text, reported)``
    (``page_prompt.prompt_parts``): a reported piece, one a witness or detector
    wrote, is charged by its bytes. Of the builder's own pieces, each
    whitespace-separated run holding an ASCII digit is charged by its bytes and
    every other character, whitespace included, at the carried rate.
    """
    byte_charged, rate_charged = 0, 0
    for text, reported in parts:
        if not isinstance(text, str) or not isinstance(reported, bool):
            raise RequestCapacityRefusal("a page prompt part is not (text, reported)")
        if reported:
            byte_charged += _reported_bytes(text)
            continue
        for run in _WHITESPACE_RUN_SPLIT.split(text):
            if _ASCII_DIGIT.search(run):
                byte_charged += _reported_bytes(run)
            else:
                rate_charged += len(run)
    return byte_charged, rate_charged


def perlector_page_prompt_bound(
    text: str, *, template_digest: str, parts: Sequence[tuple[str, bool]]
) -> tuple[int, str]:
    """``(tokens, basis)`` for one rendered page prompt (`page_prompt_charge`).

    The chat overhead, the reported pieces and digit-bearing runs at one token
    per byte, and the builder's remaining fixed prose at the carried rate.
    ``parts`` must join to exactly ``text``, or the charge would stand for
    text that was never counted.
    """

    if template_digest != PERLECTOR_PAGE_PROMPT_TEMPLATE_DIGEST:
        raise RequestCapacityRefusal(
            f"the Perlector page prompt builder digests to {template_digest}, but the "
            f"tokens-per-character rate carried to its fixed wording was sealed against "
            f"{PERLECTOR_PAGE_PROMPT_TEMPLATE_DIGEST}; the page template changed after the "
            "rate was carried to it, and a request is never admitted against the token cost "
            "of text nobody renders any more. Re-check the carried rate against the new "
            "builder and update common/request_capacity.py"
        )
    parts = list(parts)
    if "".join(part[0] for part in parts if isinstance(part, tuple) and part) != text:
        raise RequestCapacityRefusal(
            "the page prompt's parts do not join to the rendered prompt, so their charge "
            "cannot stand for the text that is sent"
        )
    byte_charged, rate_charged = page_prompt_charge(parts)
    return (
        PERLECTOR_PAGE_PROMPT_OVERHEAD_TOKENS + byte_charged + _rate_bound_tokens(rate_charged),
        PROMPT_TOKENS_REPORTED_BYTES_FIXED_CARRIED,
    )


def page_answer_bound(
    *,
    longest_witness_characters: int,
    act_entries: int,
    surya_lines: int,
    page_max_tokens: int,
) -> tuple[int, bool]:
    """``(tokens, reserve_clamped)``: the tokens reserved for one page's answer.

    The estimate is ``(longest_witness_characters + act_entries *
    len(PAGE_ANSWER_ENTRY_SKELETON) + surya_lines * len(PAGE_ANSWER_LINE_CITE)
    + len(PAGE_ANSWER_WRAPPER))`` at the carried rate, and the reserve is the estimate or the page cap, whichever is
    smaller; ``reserve_clamped`` is true when the estimate was above the cap. A
    page with no witness text shown has nothing that measures its ink, so it
    reserves the whole page cap.
    """

    longest = _nonnegative(longest_witness_characters, "longest_witness_characters")
    entries = _nonnegative(act_entries, "act_entries")
    lines = _nonnegative(surya_lines, "surya_lines")
    cap = _positive(page_max_tokens, "page_max_tokens")
    if longest == 0:
        return cap, False
    estimate = _rate_bound_tokens(
        longest
        + entries * len(PAGE_ANSWER_ENTRY_SKELETON)
        + lines * len(PAGE_ANSWER_LINE_CITE)
        + len(PAGE_ANSWER_WRAPPER)
    )
    return min(estimate, cap), estimate > cap


def page_answer_max_tokens(*, room: int, generation: Mapping[str, int]) -> int:
    """The `max_tokens` one page request sends: the sealed page cap or the context
    the prompt leaves (`room`), whichever is smaller."""
    return min(_positive(generation["page_max_tokens"], "page_max_tokens"), room)


def page_request_capacity(
    row: Any,
    *,
    image_sizes: Sequence[tuple[int, int]],
    prompt_text: str,
    prompt_parts: Sequence[tuple[str, bool]],
    template_digest: str,
    answer_measure: Mapping[str, int],
    generation: Mapping[str, int],
) -> dict[str, Any]:
    """Admit one whole-page request against its sealed row, or refuse it whole.

    ``image_sizes`` are the ``(width, height)`` of each image embedded, in
    order (``page_feed.request_image_sizes``: the page render when shown, then
    its overlay when drawn); ``prompt_text`` the text
    ``page_prompt.build_page_prompt`` rendered and ``prompt_parts`` the same
    text in its pieces (``page_prompt.prompt_parts``); ``template_digest`` its
    ``BUILDER_SHA256``; ``answer_measure`` the feed's own ``answer_measure``
    (``longest_witness_characters``, ``act_entries``, ``surya_lines``); ``generation`` the
    sealed page cap (``common.decoding.perlector_page_generation``,
    ``{"page_max_tokens"}``).

    Returns ``{"capacity": <request-capacity record>, "answer_reserve":
    {longest_witness_characters, act_entries, surya_lines, tokens,
    reserve_clamped, page_max_tokens}, "max_tokens": page_answer_max_tokens(...)}``. The prompt charge is an upper
    bound on its reported text, so the context it leaves is never overstated. Raises
    :class:`RequestCapacityRefusal` carrying the record when the row cannot
    hold image + prompt + reserve. Nothing is trimmed, split or downscaled to
    fit.
    """

    if not isinstance(answer_measure, Mapping) or set(answer_measure) != {
        "longest_witness_characters",
        "act_entries",
        "surya_lines",
    }:
        raise RequestCapacityRefusal(
            "a page request's answer measure is not exactly its longest witness text, its "
            "act entries and its Surya lines, so no answer reserve can be derived for it"
        )
    images = list(image_sizes)
    if len(images) > PERLECTOR_PAGE_MAX_IMAGES:
        raise RequestCapacityRefusal(
            f"a page request embeds {len(images)} images, more than the "
            f"{PERLECTOR_PAGE_MAX_IMAGES} its chat-template overhead is charged for"
        )
    prompt_tokens, basis = perlector_page_prompt_bound(
        prompt_text, template_digest=template_digest, parts=prompt_parts
    )
    if not isinstance(generation, Mapping) or set(generation) != {"page_max_tokens"}:
        raise RequestCapacityRefusal(
            "a page request's generation bound is not exactly the sealed page cap"
        )
    cap = _positive(generation["page_max_tokens"], "page_max_tokens")
    reserve, clamped = page_answer_bound(**answer_measure, page_max_tokens=cap)
    record = request_fits(row, images, prompt_tokens, reserve, prompt_tokens_basis=basis)
    answer_reserve = {
        **answer_measure,
        "tokens": reserve,
        "reserve_clamped": clamped,
        "page_max_tokens": cap,
    }
    if not record["fits"]:
        raise RequestCapacityRefusal(
            f"the Perlector page request does not fit the sealed serving row "
            f"({_row_name(row)}): {record['reason']}. Nothing was sent; the page is held "
            "whole, never trimmed, split or downscaled",
            capacity=record,
        )
    room = record["max_model_len"] - record["image_prompt_tokens"] - record["prompt_tokens"]
    return {
        "capacity": record,
        "answer_reserve": answer_reserve,
        "max_tokens": page_answer_max_tokens(room=room, generation=generation),
    }


def reask_answer_measure(
    named_units: Sequence[tuple[str, str]], named_ids: int, *, named_lines: int
) -> dict[str, int]:
    """What a page re-ask's answer is reserved on, as ``page_request_capacity`` takes it.

    ``named_units`` are ``(witness letter, text)`` of each witness unit the
    re-ask names, ``named_ids`` how many distinct ids it names and
    ``named_lines`` how many of them are Surya lines. Its answer transcribes
    only the ink at those ids, so its text is measured by the most text one
    witness gave for them, its entries are at most one per named id, and each
    named line adds one cite of its own, as on the first request. With no
    named witness text (only lines or records named) nothing measures the
    ink, and the reserve is the whole page cap.
    """
    by_witness: dict[str, int] = {}
    for letter, text in named_units:
        by_witness[letter] = by_witness.get(letter, 0) + len(text)
    return {
        "longest_witness_characters": max(by_witness.values(), default=0),
        "act_entries": _nonnegative(named_ids, "named_ids"),
        "surya_lines": _nonnegative(named_lines, "named_lines"),
    }


# The Coniector's request (`common/reconstruction_prompt.py`) is text only, so it
# is charged whole at one token per byte (`PROMPT_TOKENS_ALL_TEXT_PER_BYTE`), plus
# the chat template's cost for one text turn, the page request's measured 52.
TEXT_TURN_OVERHEAD_TOKENS: Final = 52

# The answer's reserve, charged per byte like the prompt: the answer's wrapper,
# and for each act and each join its entry skeleton, one finding with a reason at
# its longest, and the most departures an act may carry, each with both sides and
# its reason at their longest. The reserve decides admission only and is at most
# the sealed cap; the `max_tokens` sent is the cap or the context the prompt
# leaves, whichever is smaller.
RECONSTRUCTION_ANSWER_WRAPPER: Final = '{"acts": [], "joins": []}'
RECONSTRUCTION_ENTRY_SKELETON: Final = (
    '{"act": "p9999:999", "findings": [{"code": "cut-at-page-break", "reason": ""}], '
    '"departures": []}, '
)
RECONSTRUCTION_JOIN_SKELETON: Final = (
    '{"acts": ["p9999:999", "p9999:999"], "continues": false, "departures": []}, '
)
RECONSTRUCTION_DEPARTURE_SKELETON: Final = (
    '{"diplomatic": "", "reconstruction": "", "reason": ""}, '
)


def all_text_prompt_bound(text: str) -> tuple[int, str]:
    """``(tokens, basis)`` for one text-only prompt: every byte a token, plus one turn."""
    if not isinstance(text, str):
        raise RequestCapacityRefusal("a text-only prompt is not a string")
    return TEXT_TURN_OVERHEAD_TOKENS + _reported_bytes(text), PROMPT_TOKENS_ALL_TEXT_PER_BYTE


def reconstruction_answer_bound(
    *,
    acts: int,
    joins: int,
    max_departures_per_act: int,
    max_departure_characters: int,
    max_reason_characters: int,
    answer_max_tokens: int,
) -> tuple[int, bool]:
    """``(tokens, reserve_clamped)``: the tokens reserved for one Coniector answer.

    Each character is charged at four bytes, the most one code point takes in
    UTF-8, and each act one finding with a reason at the departure reason's
    bound. The grammar bounds neither a finding's count nor its reason, so this
    is an estimate, not a ceiling: it decides admission only, and an answer
    longer than the context left stops as a visible cut-off.
    """
    acts = _nonnegative(acts, "acts")
    joins = _nonnegative(joins, "joins")
    departures = _positive(max_departures_per_act, "max_departures_per_act")
    side = _positive(max_departure_characters, "max_departure_characters")
    reason = _positive(max_reason_characters, "max_reason_characters")
    cap = _positive(answer_max_tokens, "answer_max_tokens")
    departure = len(RECONSTRUCTION_DEPARTURE_SKELETON) + 4 * (2 * side + reason)
    per_item = departures * departure + 4 * reason
    estimate = (
        len(RECONSTRUCTION_ANSWER_WRAPPER)
        + acts * (len(RECONSTRUCTION_ENTRY_SKELETON) + per_item)
        + joins * (len(RECONSTRUCTION_JOIN_SKELETON) + per_item)
    )
    return min(estimate, cap), estimate > cap


def reconstruction_request_capacity(
    row: Any,
    *,
    prompt_text: str,
    acts: int,
    joins: int,
    policy: Any,
    answer_max_tokens: int,
) -> dict[str, Any]:
    """Admit one Coniector request against its sealed row, or refuse it whole.

    ``policy`` is the sealed `common.reconstruction.ReconstructionPolicy`, whose
    bounds size the answer. Returns ``{"capacity": <request-capacity record>,
    "answer_reserve": {acts, joins, tokens, reserve_clamped, answer_max_tokens},
    "max_tokens": min(answer_max_tokens, context the prompt leaves)}``; raises
    :class:`RequestCapacityRefusal` carrying the record when the row cannot hold
    prompt and reserve. Nothing is trimmed to fit.
    """
    prompt_tokens, basis = all_text_prompt_bound(prompt_text)
    cap = _positive(answer_max_tokens, "answer_max_tokens")
    reserve, clamped = reconstruction_answer_bound(
        acts=acts,
        joins=joins,
        max_departures_per_act=policy.max_departures_per_act,
        max_departure_characters=policy.max_departure_characters,
        max_reason_characters=policy.max_reason_characters,
        answer_max_tokens=cap,
    )
    record = request_fits(row, [], prompt_tokens, reserve, prompt_tokens_basis=basis)
    if not record["fits"]:
        raise RequestCapacityRefusal(
            f"the Coniector request does not fit the sealed serving row ({_row_name(row)}): "
            f"{record['reason']}. Nothing was sent; the page's reconstructions are not made",
            capacity=record,
        )
    room = record["max_model_len"] - record["prompt_tokens"]
    return {
        "capacity": record,
        "answer_reserve": {
            "acts": acts,
            "joins": joins,
            "tokens": reserve,
            "reserve_clamped": clamped,
            "answer_max_tokens": cap,
        },
        "max_tokens": min(cap, room),
    }


# What a dense page's answer costs, per chair, in the chair's own response
# grammar: the same 800-word `FRENCH_ACT` body for every row, so the rows stay
# comparable.  DAI and the Perlector answer it as one record or act covering
# the whole page, the demanding case.  A row holds only for its chair's current response grammar, and nothing
# checks that.
#
# Chandra's row (1645) is measured with apostrophes escaped as `&#x27;`
# (1506 written literally): the parser resolves character references, so the
# dearer spelling is a valid answer, and under-reserving for it would cut off
# an act.  Churro's (1905) is 67 `Line` elements at twelve words each.
MEASURED_DENSE_PAGE_ANSWER_TOKENS: Final[Mapping[str, int]] = MappingProxyType(
    {
        "attestator_1": 1645,
        "attestator_2": 1426,
        "attestator_3": 1905,
        "perlector": 1318,
    }
)


# One detector record's answer, for DAI, which reads a page one record at a time.
# Reserving a page's answer here would refuse record crops that fit; a page-sized
# record is still caught by its whole-page image cost.
MEASURED_RECORD_ANSWER_TOKENS: Final[Mapping[str, int]] = MappingProxyType(
    {
        "attestator_2": 230,
    }
)


# The generation bound each vendor's own inference code asks for; unlike the
# tables above, not a measured cost.
#
# * Chandra, 12,384: `chandra/settings.py::MAX_OUTPUT_TOKENS`.
# * DAI, 1,024: the model card's `max_new_tokens`.  It is not in the vendor's
#   `generation_config.json`, which `feeding.dai_generation()` carries byte for
#   byte, so it lives here.
# * Churro, 25,000: `DEFAULT_OCR_MAX_TOKENS` in the vendor's own `src/churro_ocr/providers/specs.py:77` at
#   v0.3.0 (`stanford-oval/Churro` 4abb173); the paper (arXiv:2509.19768,
#   section B.2) says only "chosen to allow generation of all gold outputs".
# * dots.mocr (`attestator_4`, seated only on the pages a routing rule names),
#   16,384: the `max_completion_tokens` of the vendor's own command line
#   (`dots_mocr/parser.py` at `rednote-hilab/dots.mocr` 23f3e56). Its prompt has
#   no measured token count yet (`MEASURED_PROMPT_TOKENS`), so a served request
#   for it is refused by name until one is measured with its tokenizer.
#
# The Perlector is a stock base model with no vendor bound;
# `operations/serving/chat_request.py` sends none.
DECLARED_ANSWER_BOUND_TOKENS: Final[Mapping[str, int]] = MappingProxyType(
    {
        "attestator_1": 12_384,
        "attestator_2": 1_024,
        "attestator_3": 25_000,
        "attestator_4": 16_384,
    }
)


def sendable_max_tokens(chair: str, capacity: Mapping[str, Any]) -> dict[str, int]:
    """The ``max_tokens`` this request may carry, or nothing where the row binds.

    The bound is ``min(declared vendor bound, max_model_len - prompt cost)``
    from this request's own capacity record.  The row term is sent as no field:
    vLLM then computes it from its own prompt assembly, whereas our count, if
    one token low, would make vLLM answer HTTP 400.  So a value is sent only
    when the declared bound is strictly below the row's remainder, leaving
    slack against an undercount.
    """

    declared = DECLARED_ANSWER_BOUND_TOKENS.get(chair)
    if declared is None:
        raise RequestCapacityRefusal(
            f"chair {chair!r} declares no upstream generation bound, so nothing here can say "
            "what answer length its occupant's own pipeline asks for; a bound is never sent "
            f"on a guess (the declared chairs are {sorted(DECLARED_ANSWER_BOUND_TOKENS)})"
        )
    if not isinstance(capacity, Mapping) or set(capacity) != CAPACITY_RECORD_FIELDS:
        raise RequestCapacityRefusal(
            f"a generation bound for chair {chair!r} was asked for against something other than "
            f"this module's own {SCHEMA} record, so the row and the prompt cost it would be "
            "derived from are not the ones this request was admitted on"
        )
    recorded_chair = capacity["chair"]
    if recorded_chair != chair:
        # A missing chair is a mismatch too, never "probably fine".
        raise RequestCapacityRefusal(
            f"a generation bound for chair {chair!r} was asked for against a capacity "
            f"record admitted for chair {recorded_chair!r}; the row and the prompt cost "
            "it would be derived from are another chair's",
            capacity=dict(capacity),
        )
    max_model_len = _positive(capacity["max_model_len"], "max_model_len")
    prompt_cost = _nonnegative(
        capacity["image_prompt_tokens"], "image_prompt_tokens"
    ) + _nonnegative(capacity["prompt_tokens"], "prompt_tokens")
    remaining = max_model_len - prompt_cost
    if remaining <= 0:
        raise RequestCapacityRefusal(
            f"the request for chair {chair!r} costs {prompt_cost} prompt tokens against a "
            f"max_model_len of {max_model_len}, so no answer fits at all; a bound of zero or "
            "less is never sent",
            capacity=dict(capacity),
        )
    return {"max_tokens": declared} if declared < remaining else {}


def dense_page_answer_budget(chair: str) -> int:
    """The measured dense-page answer budget for one chair, or a named refusal."""

    return _answer_budget(chair, MEASURED_DENSE_PAGE_ANSWER_TOKENS, "dense-page")


def record_answer_budget(chair: str) -> int:
    """The measured one-record answer budget for one chair, or a named refusal."""

    return _answer_budget(chair, MEASURED_RECORD_ANSWER_TOKENS, "one-record")


def _answer_budget(chair: str, table: Mapping[str, int], what: str) -> int:
    budget = table.get(chair)
    if budget is None:
        raise RequestCapacityRefusal(
            f"chair {chair!r} has no measured {what} answer budget; a request is never "
            "checked against a row with room reserved for an answer nobody measured "
            f"(the measured chairs are {sorted(table)})"
        )
    return budget


def image_sizes(images: Iterable[bytes]) -> list[tuple[int, int]]:
    """``(width, height)`` for each PNG a request is about to carry.

    Read off the bytes, because the embedded pixels are what the chair is
    charged for.
    """

    return [dimensions(image) for image in images]
