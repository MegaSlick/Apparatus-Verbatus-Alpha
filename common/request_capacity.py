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
the prompt invalidates it.  The Perlector's prompt is built at run time, so it
is admitted on a measured upper bound (:func:`perlector_prompt_bound`); its
measured floor is recorded beside it but never admits, because admitting on a
lower bound admits exactly the requests that overflow.

Whether vLLM's own prompt assembly agrees with these counts token for token has
never been observed; only a pod can settle it (principle 8).
"""

from __future__ import annotations

import math
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


def image_prompt_tokens(
    width: int,
    height: int,
    *,
    min_pixels: int,
    max_pixels: int,
    patch_size: int,
    merge_size: int,
) -> int:
    """Exactly how many prompt tokens one image of this size costs this chair.

    ``width``/``height`` are the pixels actually embedded, after any crop or
    resize the adapter does.
    """

    factor, resized_height, resized_width = _resize(
        width, height, min_pixels, max_pixels, patch_size, merge_size
    )
    return (resized_height // factor) * (resized_width // factor)


def resized_dimensions(
    width: int,
    height: int,
    *,
    min_pixels: int,
    max_pixels: int,
    patch_size: int,
    merge_size: int,
) -> tuple[int, int]:
    """The ``(width, height)`` the chair actually sees, for the record."""

    _factor, resized_height, resized_width = _resize(
        width, height, min_pixels, max_pixels, patch_size, merge_size
    )
    return resized_width, resized_height


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


def image_token_costs(row: Any, images: Sequence[tuple[int, int]]) -> list[int]:
    """What each image costs this sealed row, in the order it was given.

    For a caller that must compare image costs before choosing an
    ``answer_budget`` (the Perlector's act crop against a whole page).
    """

    geometry = row_image_geometry(row)
    return [
        _image_record(width, height, geometry)["image_prompt_tokens"] for width, height in images
    ]


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
PROMPT_TOKENS_MEASURED_RATE: Final = "measured-tokens-per-word-extrapolation"
# The only basis a dossier-built prompt may be admitted on.
PROMPT_TOKENS_MEASURED_BOUND: Final = "measured-upper-bound-for-this-prompt-shape"
PROMPT_TOKENS_BASES: Final = frozenset(
    {
        PROMPT_TOKENS_MEASURED_CONSTANT,
        PROMPT_TOKENS_MEASURED_FLOOR,
        PROMPT_TOKENS_MEASURED_RATE,
        PROMPT_TOKENS_MEASURED_BOUND,
    }
)

# A floor may explain a refusal but never admit a request.
PROMPT_TOKENS_ADMITTING_BASES: Final = frozenset(
    {PROMPT_TOKENS_MEASURED_CONSTANT, PROMPT_TOKENS_MEASURED_BOUND}
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
    only, never compared with the bound: a prompt shorter than the
    representative dossier can record a floor above its own bound.

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
    if set(record) != CAPACITY_RECORD_FIELDS:
        raise AssertionError(  # pragma: no cover - closed by construction above
            f"{SCHEMA} built the wrong field set: {sorted(record)}"
        )
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
        "designator_structure": (
            SealedPromptTokens(
                tokens=593,
                prompt_digest="025935f3e1de1acdfadd4c7d581ab17eb82e8caaffef7b64962621c80b7ca9a8",
                repo="datalab-to/chandra-ocr-2",
                revision="af93b47dba1b47b6640c86ccf487ed2260ab9a09",
            ),
        ),
        # The same Chandra prompt as `designator_structure`.
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

# The Perlector's floor: a representative three-testimonia dossier, and its
# tokenizer's rate over register French (120 tokens for 73 words), kept as the
# measured integer pair so the arithmetic is exact.
PERLECTOR_PROMPT_FLOOR_TOKENS: Final = 790
PERLECTOR_TOKENS_PER_WORD: Final = (120, 73)

# --- and the upper bound admission actually rests on ---------------------------
#
# Tokens per character over 168 prompts rendered through `build_prompt` with the
# pinned tokenizer and chat template (1/3/5 testimonia, 0-1,200 words each, with
# and without prior draft, reproofs and a second view).  The ratio falls as the
# dossier grows, so the sealed value is the maximum, 0.4126394, rounded up.  The
# doubt-mark instruction lowered the maximum to 0.3940 on the same grid, so the
# sealed value still bounds it.
PERLECTOR_BOUND_TOKENS_PER_10K_CHARACTERS: Final = 4127
# Kept apart from the ratio so measurement and margin stay visible.
PERLECTOR_BOUND_SAFETY_MARGIN: Final = (105, 100)
# Chat-template cost: 52 per turn plus 2 per image, charged at the protocol's
# `max_images` ceiling so it bounds any request; a test reconciles the 32 with
# `config/perlector_protocol.toml`.
PERLECTOR_PROMPT_OVERHEAD_TOKENS: Final = 52 + 2 * 32
PERLECTOR_MAX_IMAGES_THE_OVERHEAD_COVERS: Final = 32
# `prompts.py`'s module digest (`builder_sha256`): editing the builder expires
# the measured ratio.
PERLECTOR_PROMPT_TEMPLATE_DIGEST: Final = (
    "64263538423aab2f2864a05d599c704c543b908da6a1e001ca91e7fefded651d"
)
# The representative dossier's size and bound, for weighing the shipped rows
# against what is admitted on; a test re-derives the bound from the size.
PERLECTOR_REPRESENTATIVE_PROMPT_CHARACTERS: Final = 2438
PERLECTOR_REPRESENTATIVE_PROMPT_BOUND_TOKENS: Final = 1173
# Reconciled with `config/models-real.toml`: with no fixed prompt to digest, the
# pinned revision is what expires the Perlector's measurements.
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


def perlector_prompt_tokens(text: str) -> tuple[int, str]:
    """``(tokens, basis)`` for one rendered Perlector prompt: a floor, not a count.

    The larger of the measured floor and the per-word rate.  Both are lower
    bounds (richer dossiers and JSON scaffolding cost more), so this can prove
    a request does not fit but never that it does.
    """

    words = len(text.split())
    numerator, denominator = PERLECTOR_TOKENS_PER_WORD
    by_rate = -(-words * numerator // denominator)
    if by_rate > PERLECTOR_PROMPT_FLOOR_TOKENS:
        return by_rate, PROMPT_TOKENS_MEASURED_RATE
    return PERLECTOR_PROMPT_FLOOR_TOKENS, PROMPT_TOKENS_MEASURED_FLOOR


def perlector_prompt_bound(text: str, *, template_digest: str) -> tuple[int, str]:
    """``(tokens, basis)`` for one rendered Perlector prompt: the upper bound.

    Per character, not per word: JSON scaffolding has few words for its tokens,
    so the per-word rate spreads five-fold across dossiers where the
    per-character rate spreads 1.6-fold.  ``template_digest`` is ``prompts.py``'s
    module digest, checked because the ratio describes only that builder's bytes.
    """

    if template_digest != PERLECTOR_PROMPT_TEMPLATE_DIGEST:
        raise RequestCapacityRefusal(
            f"the Perlector prompt builder digests to {template_digest}, but the measured "
            f"tokens-per-character bound this chair is admitted on was taken over "
            f"{PERLECTOR_PROMPT_TEMPLATE_DIGEST} with "
            f"{PERLECTOR_MEASURED_TOKENIZER[0]} at {PERLECTOR_MEASURED_TOKENIZER[1]}; the "
            "prompt template changed after it was measured, and a request is never admitted "
            "against the token cost of text nobody renders any more. Re-measure the rate and "
            "update common/request_capacity.py"
        )
    margin_numerator, margin_denominator = PERLECTOR_BOUND_SAFETY_MARGIN
    body = -(
        -len(text)
        * PERLECTOR_BOUND_TOKENS_PER_10K_CHARACTERS
        * margin_numerator
        // (10_000 * margin_denominator)
    )
    return PERLECTOR_PROMPT_OVERHEAD_TOKENS + body, PROMPT_TOKENS_MEASURED_BOUND


# What a dense page's answer costs, per chair, in the chair's own response
# grammar: the same 800-word `FRENCH_ACT` body for every row, so the rows stay
# comparable.  DAI and the Perlector use their page-fallback act, the demanding
# case.  A row holds only for its chair's current response grammar, and nothing
# checks that.
#
# Chandra's two rows (1645) are measured with apostrophes escaped as `&#x27;`
# (1506 written literally): the parser resolves character references, so the
# dearer spelling is a valid answer, and under-reserving for it would cut off
# an act (goal 2).  Churro's (1905) is 67 `Line` elements at twelve words each.
MEASURED_DENSE_PAGE_ANSWER_TOKENS: Final[Mapping[str, int]] = MappingProxyType(
    {
        "designator_structure": 1645,
        "attestator_1": 1645,
        "attestator_2": 1426,
        "attestator_3": 1905,
        "perlector": 1318,
    }
)


# One ordinary act's answer, for the act-scoped chairs.  Reserving a page's
# answer here would refuse calls that fit and cost acts (goal 2); a
# page-fallback act is still caught by its whole-page image cost.
MEASURED_ACT_ANSWER_TOKENS: Final[Mapping[str, int]] = MappingProxyType(
    {
        "attestator_2": 230,
        "perlector": 216,
    }
)


# The generation bound each vendor's own inference code asks for; unlike the
# tables above, not a measured cost.
#
# * Chandra, 12,384: `chandra/settings.py::MAX_OUTPUT_TOKENS`.
# * DAI, 1,024: the model card's `max_new_tokens`.  It is not in the vendor's
#   `generation_config.json`, which `feeding.dai_generation()` carries byte for
#   byte, so it lives here.
# * Churro, 20,000: the CHURRO paper (arXiv:2509.19768), section B.2.
#
# The Perlector is a stock base model with no vendor bound;
# `pipeline/4_perlector/live_reader.py` sends none.
DECLARED_ANSWER_BOUND_TOKENS: Final[Mapping[str, int]] = MappingProxyType(
    {
        "designator_structure": 12_384,
        "attestator_1": 12_384,
        "attestator_2": 1_024,
        "attestator_3": 20_000,
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


def act_answer_budget(chair: str) -> int:
    """The measured single-act answer budget for one chair, or a named refusal."""

    return _answer_budget(chair, MEASURED_ACT_ANSWER_TOKENS, "single-act")


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
