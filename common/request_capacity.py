"""Whether one reading request fits the sealed serving row it would be sent to.

The failure this closes is arithmetic, not a pod question.  A served Qwen-VL
chair spends prompt tokens on the *image* before a single word of the prompt is
counted, and the number is decided by the model's own ``smart_resize`` against
the row's ``min_pixels``/``max_pixels``.  At 300 dpi an A4 page is 2480x3508,
and against the shipped rows that is between 1,715 and 6,693 image tokens on
its own.  A row whose ``max_model_len`` cannot hold image + prompt + a real
answer does not fail slowly: vLLM answers **HTTP 400** before it generates
anything, on a card that bills by the hour.

Nothing here downscales, clamps, or reserves.  It computes the exact count and
says whether the row can hold it, so the refusal happens on this laptop rather
than on rented silicon (GOVERNANCE 10: the number is measured, and what is not
measured is named as such).

Where the arithmetic comes from
-------------------------------
:func:`image_prompt_tokens` is a rewrite of ``smart_resize`` from
``transformers/models/qwen2_vl/image_processing_qwen2_vl.py``, read at the
``transformers`` 5.16.1 installed on the measuring host.  The recipes pin
5.14.1; that source has never been on this disk, so nothing here claims the
function is unchanged between the two -- what is claimed is the version that
was actually read, and a pod running 5.14.1 is where the two could first be
compared.  **It is a rewrite of a published formula, not
carried code** -- eleven lines of integer arithmetic, retyped here so this
repository owns what it runs, with the source named so a reader can check it
line for line.  The token count is then the processor's own formula from
``Qwen2_5VLProcessor.replace_image_token`` / ``Qwen3VLProcessor.replace_image_token``:
``image_grid_thw.prod() // merge_size**2``, which for one image is
``(h_bar // factor) * (w_bar // factor)`` where ``factor = patch_size * merge_size``.

The patch and merge sizes are **per chair**, never a default: the Qwen2.5-VL
chairs (DAI, Churro) use patch 14 / merge 2 -- 784 px per image token -- and the
Qwen3-VL chairs (Chandra, the Perlector) use patch 16 / merge 2 -- 1,024 px per
image token.  A default here would silently mis-count by 30%, so
:func:`row_image_geometry` refuses by name when the sealed row does not state
them rather than guessing (the same posture as
``pipeline/3_attestatores/live_witness.py::row_context_bound``).

Prompt tokens
-------------
The honest way to count a prompt is the chair's own tokenizer.  It is not
available offline in this frozen environment: the tokenizer files are not in
this repository, and ``transformers``' image processors import ``torch`` at
module load, for which this host (an Intel Mac) has no wheel at the required
floor.  So the fixed prompts carry a **sealed measured constant per prompt
version**, taken from the per-chair measurement in
``TOKEN_COST_REPORT.md`` section 5 -- each rendered through the repository's real
chat template with the repository's real tokenizer at the pinned revision -- and
each constant is stored beside a digest of the exact prompt text it was measured
over.  :func:`sealed_prompt_tokens` recomputes that digest and refuses when it
differs, so **editing a prompt invalidates its measurement** instead of leaving
a stale number in force.

The Perlector's prompt is built from run-time dossier content and has no fixed
text to digest, so it carries measured facts rather than one constant.  Two of
them describe a **floor**: the measured cost of a representative dossier at this
prompt shape, and the measured tokens-per-word ratio of its own tokenizer on
18th-century French register prose.  :func:`perlector_prompt_tokens` takes the
larger of those two and is a lower bound on any real prompt -- the 790 was
measured over a pass-A dossier with no fed prior draft and no reproof
instrument, and the 1.644 tokens per word over plain prose, where a dossier is
prose plus JSON scaffolding and costs more per word.

**A floor cannot decide admission, and it no longer does.**  A check that admits
on a lower bound admits exactly the requests it should have refused: a real
dossier carrying five witnesses' full act texts, or a pass-B prompt with reproof
instruments appended, passes a check measured over a 73-word pass-A dossier and
is then answered with the HTTP 400 the check exists to prevent.  So a second,
**upper** bound was measured, and it is the one
:func:`refuse_unless_it_fits` admits on: :func:`perlector_prompt_bound`.  The
floor is still computed and still travels on the capacity record beside it, with
both bases named, because it is what says the request was refused by a
measurement rather than by a margin.

The bound is measured, not padded.  168 Perlector prompts were rendered through
this repository's own builder -- one, three and five testimonia; 0, 5, 25, 100,
400, 800 and 1,200 words of register French per testimonium; with and without a
fed prior draft; with and without five appended reproof prompts; at one and two
capture views -- tokenized with the chair's pinned tokenizer through its own
chat template, and measured for tokens per character.  The ratio falls
monotonically with dossier size (0.4126 at the densest scaffolding, 0.2641 over
1,200-word acts), so the **maximum observed** ratio is the one sealed, with a
stated 5% margin over it: see :data:`PERLECTOR_BOUND_TOKENS_PER_10K_CHARACTERS`.
It is sealed the way a fixed prompt's constant is -- digested against the
template it was measured over, so an edit to the prompt builder invalidates the
measurement rather than leaving a stale rate in force -- and reconciled against
the pinned revision by the same test as the rest.

**What the bound is not.**  It bounds this repository's rendering of the prompt.
Whether vLLM's own assembly agrees with these counts token for token has never
been observed, here or anywhere in this tree, so the bound is an upper bound on
a measurement rather than a guarantee of admission (GOVERNANCE 10).

**What only a pod can settle**: whether vLLM's own prompt assembly agrees with
these counts token for token.  vLLM's OpenAI server applies the same chat
template from the same repository files, so they should; that agreement has
never been observed.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any, Final, Iterable, Mapping, Sequence

from common.contracts.canonical import digest_bytes
from common.contracts.errors import SchemaRefusal
from common.imaging import dimensions

SCHEMA: Final = "verbatus-request-capacity.v1"

# The exact source the arithmetic below was read off, named so a reader can
# check the rewrite rather than take its word for it.
SMART_RESIZE_SOURCE: Final = (
    "transformers/models/qwen2_vl/image_processing_qwen2_vl.py::smart_resize "
    "(read at transformers 5.16.1, the version installed on the measuring host; "
    "the recipes pin 5.14.1, whose source has never been on this disk and has "
    "not been compared)"
)

# ``smart_resize`` refuses an image this far from square before it resizes it.
# Kept as its own name because the refusal is the library's, not ours.
MAX_ASPECT_RATIO: Final = 200


class RequestCapacityRefusal(SchemaRefusal):
    """A request cannot be sent as shaped: the sealed row cannot hold it.

    A refusal about the request, before anything goes on the wire -- the same
    scope as ``operations.serving.errors.ChairRequestRefusal``, raised here
    because it is decided by stage-side arithmetic over the sealed row rather
    than by the client's own wire checks.  ``capacity`` carries the closed
    record (:data:`SCHEMA`) so the caller can publish the arithmetic beside
    whatever it holds or refuses, and never has to restate it from the message.
    """

    def __init__(self, message: str, *, capacity: Mapping[str, Any] | None = None) -> None:
        self.capacity = dict(capacity) if capacity is not None else None
        super().__init__(message)


# --- the arithmetic -------------------------------------------------------------


def smart_resize(
    height: int, width: int, *, factor: int, min_pixels: int, max_pixels: int
) -> tuple[int, int]:
    """The resized ``(height, width)`` a Qwen-VL image processor would use.

    A rewrite of ``smart_resize`` (see :data:`SMART_RESIZE_SOURCE`), argument
    order and rounding included: ``round`` is Python's own banker's rounding in
    both, and ``math.floor``/``math.ceil`` land on the same integers.  Three
    conditions, in the library's own order: both sides divisible by ``factor``;
    total pixels inside ``[min_pixels, max_pixels]``; aspect ratio held as
    closely as the first two allow.
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

    Not an estimate and not a bound: this is the processor's own count, from
    the resized grid ``smart_resize`` produces.  ``width``/``height`` are the
    pixels actually embedded in the request -- an adapter that crops or resizes
    before sending (DAI) has already done so by the time this is asked.
    """

    factor = _positive(patch_size, "patch_size") * _positive(merge_size, "merge_size")
    resized_height, resized_width = smart_resize(
        height,
        width,
        factor=factor,
        min_pixels=_positive(min_pixels, "min_pixels"),
        max_pixels=_positive(max_pixels, "max_pixels"),
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

    factor = _positive(patch_size, "patch_size") * _positive(merge_size, "merge_size")
    resized_height, resized_width = smart_resize(
        height,
        width,
        factor=factor,
        min_pixels=_positive(min_pixels, "min_pixels"),
        max_pixels=_positive(max_pixels, "max_pixels"),
    )
    return resized_width, resized_height


def _positive(value: object, field: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
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

    ``min_pixels``/``max_pixels`` are what the manager passes to vLLM as
    ``--mm-processor-kwargs``; ``patch_size``/``merge_size`` are the chair's
    vision-encoder geometry, declared on the row because they decide the token
    cost of every image sent under it.  They are read from the pinned
    revision's own ``preprocessor_config.json`` and recorded on the row; a row
    that states none of them is refused here rather than counted against a
    default that would be wrong for two of the four chairs.
    """

    values: dict[str, int] = {}
    missing: list[str] = []
    for field in ("min_pixels", "max_pixels", "patch_size", "merge_size"):
        value = getattr(profile, field, None)
        if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
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

    The same arithmetic :func:`request_fits` totals, exposed one image at a
    time for a caller that must decide something *about* an image before the
    record is built -- the Perlector's answer reserve is decided by whether the
    act's own crop costs what a whole-page render costs, and that comparison
    has to happen before ``answer_budget`` is passed in.  Reads the row's
    geometry once, through the same refusal :func:`row_image_geometry` gives.
    """

    geometry = row_image_geometry(row)
    return [
        image_prompt_tokens(
            width,
            height,
            min_pixels=geometry.min_pixels,
            max_pixels=geometry.max_pixels,
            patch_size=geometry.patch_size,
            merge_size=geometry.merge_size,
        )
        for width, height in images
    ]


def row_context_length(profile: Any) -> int:
    """The sealed row's ``max_model_len``, or a refusal naming the row.

    The one field in the serving contract that says how long a request the
    engine will accept.  Deliberately the same refusal posture as
    ``pipeline/3_attestatores/live_witness.py::row_context_bound``, which asks
    the same question of the same field for the sendable generation bound.
    """

    value = getattr(profile, "max_model_len", None)
    if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
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

# What ``prompt_tokens`` rests on, as a closed vocabulary, so a receipt says
# how the number was arrived at rather than leaving a reader to assume it was
# counted by a tokenizer that never ran here.
PROMPT_TOKENS_MEASURED_CONSTANT: Final = "measured-constant-for-this-prompt-version"
PROMPT_TOKENS_MEASURED_FLOOR: Final = "measured-floor-for-this-prompt-shape"
PROMPT_TOKENS_MEASURED_RATE: Final = "measured-tokens-per-word-extrapolation"
# The one basis a *dossier-built* prompt may be admitted on: the maximum
# tokens-per-character ratio measured over this prompt shape, plus a stated
# margin, over the measured chat-template overhead.  Named apart from the two
# floors above so a receipt can never be read as though a lower bound had been
# treated as an admission.
PROMPT_TOKENS_MEASURED_BOUND: Final = "measured-upper-bound-for-this-prompt-shape"
PROMPT_TOKENS_BASES: Final = frozenset(
    {
        PROMPT_TOKENS_MEASURED_CONSTANT,
        PROMPT_TOKENS_MEASURED_FLOOR,
        PROMPT_TOKENS_MEASURED_RATE,
        PROMPT_TOKENS_MEASURED_BOUND,
    }
)

# The bases a capacity record may admit a request on.  A floor names how a
# request was *refused*; it may never be the number a request was let through
# on.  `request_fits` holds this, so a call site cannot admit on a floor by
# passing one in.
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

    ``images`` is every image the request carries, in the order it carries
    them, as ``(width, height)`` in the pixels actually embedded.
    ``prompt_tokens`` is the request's text cost and ``answer_budget`` the
    tokens the answer must be able to occupy -- both are the caller's to
    supply, because only the caller knows which prompt and which answer shape
    this call is.

    ``prompt_tokens`` is the number admission is decided on, so it must rest on
    a basis in :data:`PROMPT_TOKENS_ADMITTING_BASES` -- a measured constant for
    a fixed prompt, or a measured upper bound for a dossier-built one.  A floor
    is refused here rather than accepted quietly: a check that admits on a lower
    bound admits exactly the requests it should have refused.

    ``prompt_tokens_floor``/``prompt_tokens_floor_basis`` are the *other* number
    a chair with no fixed prompt has -- what this repository has measured of the
    prompt from below.  They are recorded, never used to decide anything, so a
    receipt shows both what the request was admitted on and what was measured of
    it, each with its basis named.

    The two are independent measurements and are not cross-checked here.  The
    Perlector's floor is the larger of a rate over this prompt's own words and
    the measured cost of a *representative* dossier, and that second term is a
    claim about real dossiers rather than about arbitrarily short text -- so a
    prompt smaller than that dossier can record a floor above its own bound.
    Nothing turns on it either way: admission is the bound and only the bound.

    Never raises on a request that simply does not fit: that is a ``fits:
    False`` record with a reason, which the caller publishes and then acts on.
    It raises only when the row cannot state what it would take to compute the
    answer at all.
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

    image_records: list[dict[str, Any]] = []
    for width, height in images:
        resized_width, resized_height = resized_dimensions(
            width,
            height,
            min_pixels=geometry.min_pixels,
            max_pixels=geometry.max_pixels,
            patch_size=geometry.patch_size,
            merge_size=geometry.merge_size,
        )
        image_records.append(
            {
                "width": width,
                "height": height,
                "resized_width": resized_width,
                "resized_height": resized_height,
                "image_prompt_tokens": image_prompt_tokens(
                    width,
                    height,
                    min_pixels=geometry.min_pixels,
                    max_pixels=geometry.max_pixels,
                    patch_size=geometry.patch_size,
                    merge_size=geometry.merge_size,
                ),
            }
        )
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

    The one-line form for a call site whose contract is a refusal rather than a
    hold.  A site that holds instead reads ``record["fits"]`` and publishes the
    record either way.

    Admits on ``prompt_tokens`` and on nothing else, and :func:`request_fits`
    refuses a ``prompt_tokens_basis`` that names a floor, so the number a
    request gets through on is always a measured constant or a measured upper
    bound.
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

    ``prompt_digest`` is :func:`prompt_digest` of the prompt's own message
    texts at the moment of measurement.  It is what makes the constant expire:
    edit the prompt and :func:`sealed_prompt_tokens` refuses rather than
    carrying a number that describes text nobody sends any more.

    ``repo`` and ``revision`` are the tokenizer the count was taken with, as
    two structured fields rather than one sentence, because they are
    reconciled against ``config/models-real.toml``'s own pins by a test --
    the way ``common/chairs/model_store.py``'s inventory is reconciled against
    ``config/models.toml``.  A repointed chair invalidates its measurement
    exactly as an edited prompt does: the tokenizer that produced the number
    is no longer the tokenizer the chair would use.
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


# Measured on the session host against each chair's own tokenizer and chat
# template at the revision `config/models-real.toml` pins
# (`TOKEN_COST_REPORT.md` sections 1 and 5).  No weights were fetched and no
# tokenizer runs here: these are the recorded results, bound to the prompt text
# they were taken over.
MEASURED_PROMPT_TOKENS: Final[Mapping[str, SealedPromptTokens]] = MappingProxyType(
    {
        "designator_structure": SealedPromptTokens(
            tokens=329,
            prompt_digest="9e1a536bf5cdfda66e50e1d2f39df04de5130812c716f2e5b251092ee5973082",
            repo="datalab-to/chandra-ocr-2",
            revision="af93b47dba1b47b6640c86ccf487ed2260ab9a09",
        ),
        "attestator_1": SealedPromptTokens(
            tokens=256,
            prompt_digest="97cfb7ba5143687c0f61784026d37268cd18d60c053f99ab49e4079ccb9d629a",
            repo="datalab-to/chandra-ocr-2",
            revision="af93b47dba1b47b6640c86ccf487ed2260ab9a09",
        ),
        "attestator_2": SealedPromptTokens(
            tokens=84,
            prompt_digest="9601ebe46918c76ac3f8d094b602ffd6303cc5bf51d5973e5eff2ad93cff964a",
            repo="Teklia/Qwen2.5-VL-7B-DAI-CReTDHI-RecordGold-ATR",
            revision="e371095d4ffe585f31f4974462931ddbac61ff64",
        ),
        # Re-measured for `feeding.churro_layout_prompt` --
        # `churro-layout-prompt.v1`, the instruction the live chair is actually
        # sent since Churro's native layout landed.  The sealed 281 was taken
        # over `feeding.churro_prompt`'s trained `<output>` framing, which is
        # now what the *fixture* posture declares and not what any served
        # request carries, and its digest expired the moment the live prompt
        # changed -- which is the mechanism working, not a defect.  The
        # replacement clause asks for a JSON object with one `box_1000` per
        # block, so the prompt is longer: 441 tokens over
        # c5a8375b..., measured by the same harness, at the same pinned
        # revision, that reproduces the superseded 281 exactly over the carried
        # prompt it was taken from.
        "attestator_3": SealedPromptTokens(
            tokens=441,
            prompt_digest="c5a8375b77b15fcd09ccd9ed6212cb650a87a3b2268019082916a1c7e32f209a",
            repo="stanford-oval/churro-3B",
            revision="ca2150ea465d5a3d67818c50e234b9422619c75d",
        ),
    }
)

# The Perlector's prompt is rendered from run-time dossier content, so it has no
# fixed text to seal.  Two measured facts stand in for one constant: the floor
# is `TOKEN_COST_REPORT.md` section 5's measurement of a representative
# single-view dossier (witness regime, three testimonia, no fed prior draft),
# and the ratio is section 6's measurement of that chair's own tokenizer over
# 18th-century French register prose -- 120 tokens for 73 words.  Kept as the
# measured integer pair rather than a rounded rate so the arithmetic stays exact.
PERLECTOR_PROMPT_FLOOR_TOKENS: Final = 790
PERLECTOR_TOKENS_PER_WORD: Final = (120, 73)

# --- and the upper bound admission actually rests on ---------------------------
#
# Measured 2026-09-06 on the session host, offline, with the pinned Perlector
# tokenizer and its own chat template: 168 prompts rendered through
# `pipeline/4_perlector/prompts.py::build_prompt` for recipe
# `unproven-real-perlector` -- 1/3/5 testimonia x 0/5/25/100/400/800/1,200 words
# of 18th-century register French per testimonium x fed and withheld prior draft
# x zero and five appended reproof prompts x one and two capture views -- and
# measured for tokens per character of the rendered text.
#
# The harness reproduces `TOKEN_COST_REPORT.md` section 5 exactly on that
# section's own dossier (790 text tokens at one capture view, 794 at two), which
# is what says it is measuring the same thing the floor was measured with.
#
# The ratio falls monotonically as the dossier grows -- 0.4126 where the acts are
# empty and the JSON scaffolding is all there is, 0.2641 over 1,200-word acts --
# so the maximum is at the *small* end and it is the maximum that is sealed:
# 0.4126394 tokens per character, rounded up at the fourth decimal.
PERLECTOR_BOUND_TOKENS_PER_10K_CHARACTERS: Final = 4127
# Over the maximum, not over a mean: 5%, stated rather than folded into the
# ratio so a reader can see the measurement and the margin apart. The tightest
# measured case clears its own bound by 13.7% with it.
PERLECTOR_BOUND_SAFETY_MARGIN: Final = (105, 100)
# The chat template's own cost, which no per-character rate can carry: 52 tokens
# for the turn plus 2 for each image in it, both measured exactly (the per-image
# cost is 2 at every count from 0 to 8 images). Charged at
# `config/perlector_protocol.toml`'s `max_images` ceiling of 32 rather than at
# the images this request happens to carry, so the constant is an upper bound
# for any request this seam can build; `test_live_reader.py` reconciles the 32
# against that file, so raising the ceiling expires this number.
PERLECTOR_PROMPT_OVERHEAD_TOKENS: Final = 52 + 2 * 32
PERLECTOR_MAX_IMAGES_THE_OVERHEAD_COVERS: Final = 32
# The builder the ratio was measured through, as `prompts.py`'s own module
# digest -- the same value `prompts.prompt_evidence` records as
# `builder_sha256`. `perlector_prompt_bound` refuses when the caller's builder
# does not match it, so an edited prompt template expires this measurement
# exactly as an edited fixed prompt expires its sealed constant.
PERLECTOR_PROMPT_TEMPLATE_DIGEST: Final = (
    "ad623c7d0fd379816c471f21bda00cd7dbf1f0ecfabed00ccc2e8f8a29dbf783"
)
# The representative dossier of `TOKEN_COST_REPORT.md` section 5 -- three
# testimonia, one 73-word act each, pass A, no reproof -- as its measured
# character count and the bound over it. Sealed so the shipped-row check
# (`operations/serving/test_serving_catalogue_capacity.py`) can weigh the rows
# against the number the seam admits on rather than against the floor it no
# longer admits on; `test_request_capacity.py` re-derives the second from the
# first, so the pair cannot drift from the arithmetic.
PERLECTOR_REPRESENTATIVE_PROMPT_CHARACTERS: Final = 2269
PERLECTOR_REPRESENTATIVE_PROMPT_BOUND_TOKENS: Final = 1100
# The tokenizer all of the Perlector's measured numbers were taken with, in the
# same two structured fields a sealed prompt carries, and reconciled against
# `config/models-real.toml` by the same test: this chair has no fixed prompt to
# digest, so the pinned revision is the only thing that can expire its
# measurement.
PERLECTOR_MEASURED_TOKENIZER: Final = (
    "Qwen/Qwen3.8-27B",
    "1d4bf0f2ff6012fd82039f2fa52739d0dd7c60c0",
)


def sealed_prompt_tokens(chair: str, *texts: str) -> int:
    """The measured prompt-token count for this chair's fixed prompt, digest-checked.

    Refuses when the chair has no measurement, and refuses when the prompt text
    has changed since the measurement was taken -- a stale constant would report
    the cost of a prompt nobody sends.
    """

    entry = MEASURED_PROMPT_TOKENS.get(chair)
    if entry is None:
        raise RequestCapacityRefusal(
            f"chair {chair!r} has no measured prompt-token count; no tokenizer is available "
            "offline in this environment, so a request for this chair cannot be checked "
            f"against a row until one is measured (the measured chairs are "
            f"{sorted(MEASURED_PROMPT_TOKENS)})"
        )
    digest = prompt_digest(*texts)
    if digest != entry.prompt_digest:
        raise RequestCapacityRefusal(
            f"chair {chair!r} sends a prompt whose digest is {digest}, but its measured "
            f"prompt-token count of {entry.tokens} was taken over {entry.prompt_digest} "
            f"with {entry.measured_by}; the prompt changed after it was measured, and a request "
            "is never checked against the token cost of text nobody sends. Re-measure the "
            "prompt and update common/request_capacity.py"
        )
    return entry.tokens


def perlector_prompt_tokens(text: str) -> tuple[int, str]:
    """``(tokens, basis)`` for one rendered Perlector prompt: a floor, not a count.

    The larger of the measured floor and the measured tokens-per-word ratio
    applied to this prompt's own word count.  The ratio was measured over
    French register prose rather than over this dossier, so a count that rests
    on it is named an extrapolation in the record it lands in; the floor is a
    measurement of this prompt shape and is named as such.

    **Both inputs are lower bounds, so the result is one too.**  The floor was
    measured over a pass-A dossier with no fed prior draft and no appended
    reproof prompts, and every richer dossier is larger; the rate was measured
    over prose, and the JSON scaffolding a dossier carries costs more per word.
    A request this returns a number for is not thereby proved to fit -- it is
    proved not to fit when even the floor overruns the row.  Nothing pads it:
    a margin nobody measured is not a measurement (GOVERNANCE 10).
    """

    words = len(text.split())
    numerator, denominator = PERLECTOR_TOKENS_PER_WORD
    by_rate = -(-words * numerator // denominator)
    if by_rate > PERLECTOR_PROMPT_FLOOR_TOKENS:
        return by_rate, PROMPT_TOKENS_MEASURED_RATE
    return PERLECTOR_PROMPT_FLOOR_TOKENS, PROMPT_TOKENS_MEASURED_FLOOR


def perlector_prompt_bound(text: str, *, template_digest: str) -> tuple[int, str]:
    """``(tokens, basis)`` for one rendered Perlector prompt: the upper bound.

    The number this chair's requests are admitted on.  The measured chat-template
    overhead at the protocol's own image ceiling, plus the maximum measured
    tokens-per-character ratio with its stated margin, applied to this prompt's
    own characters.  Every one of the 168 measured prompts is below what this
    returns for it, the tightest by 13.7%.

    Characters rather than words, deliberately.  A dossier is prose *and* JSON
    scaffolding, and the scaffolding has few whitespace words for the tokens it
    costs: the same measurement gives 1.65 tokens per word over 1,200-word acts
    and 8.16 over a dossier whose acts are empty, a five-fold spread, against
    0.264 and 0.413 per character.  A per-word rate would need a margin five
    times the size to cover the same set, which is a margin standing in for a
    measurement nobody took.

    ``template_digest`` is the builder the caller is rendering through --
    ``prompts.py``'s module digest, the same value ``prompt_evidence`` records
    as ``builder_sha256``.  It is checked rather than trusted: the ratio
    describes the bytes that builder produces, and an edited builder produces
    other bytes.  This is :func:`sealed_prompt_tokens`'s discipline for a prompt
    that has no fixed text to digest -- what expires the measurement is the
    template rather than the rendering.

    **An upper bound on a measurement, not a guarantee of admission.**  vLLM's
    own prompt assembly has never been observed by this repository.  What this
    closes is the failure of admitting on a *floor*: a real five-witness dossier
    or a pass-B prompt with reproofs appended no longer passes a check that was
    measured over a 73-word pass-A dossier.
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


# What an answer costs on a dense page, per chair, in that chair's own declared
# response shape: `TOKEN_COST_REPORT.md` section 8's 800-word measurement, which
# is the larger of the two it took.  DAI's is its whole-page fallback act (its
# ordinary act answer is 230); the Perlector's is likewise its page-fallback
# act's reading rather than an ordinary act's 216.  Both are the demanding case,
# because a row that cannot hold the demanding case cannot serve a dense page.
#
# **"Its own declared response shape" is what makes this expire with a prompt.**
# Churro's 1,433 was an `<output>` envelope's answer; the live chair is now
# asked for the closed JSON object `common/churro_response.py` declares, whose
# per-block `box_1000` and key names are real tokens.  Re-measured over the same
# 800 words in the same six blocks the other page chairs' numbers were taken
# over -- so the five numbers stay comparable with each other -- it is 1,631.
# A denser answer costs more, and that is recorded rather than sealed: the same
# 800 words in 12 blocks measure 1,818 and in 24 blocks 2,204, and all three
# fit every shipped Churro row.  Twenty-four blocks is not the convention the
# other four chairs were measured under, and quietly moving one chair to a
# stricter one would make this table's rows mean different things.
MEASURED_DENSE_PAGE_ANSWER_TOKENS: Final[Mapping[str, int]] = MappingProxyType(
    {
        "designator_structure": 1575,
        "attestator_1": 1520,
        "attestator_2": 1426,
        "attestator_3": 1631,
        "perlector": 1318,
    }
)


# What one *act*'s answer costs, for the two chairs that are asked for one act
# rather than for a page: `TOKEN_COST_REPORT.md` section 8's 800-word figures
# again, but the ordinary-act rows.  These are the budgets an act-scoped
# request reserves, because reserving a whole page's answer for a request that
# asked for one act would refuse calls that measurably work -- DAI on an
# ordinary crop is the one chair sound at every tier, and refusing it would
# cost acts (GOALS 1) to protect against an overrun that cannot happen.
#
# A *page-fallback* act -- an act whose bounds are the whole page -- is not
# admitted by the back door here: its crop is a whole 300-dpi page, so it is
# caught by its own image cost, which is between four and thirty times the
# difference between these two budgets.
MEASURED_ACT_ANSWER_TOKENS: Final[Mapping[str, int]] = MappingProxyType(
    {
        "attestator_2": 230,
        "perlector": 216,
    }
)


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

    Read off the bytes themselves rather than off a presentation record, because
    the pixels the chair is charged for are the pixels actually embedded --
    ARCHITECTURE invariant 3 read forwards: the exact image shown decides the
    exact cost.
    """

    return [dimensions(image) for image in images]
