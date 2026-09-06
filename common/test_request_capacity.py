"""Drills for the request-capacity arithmetic.

The numbers asserted here are the ones measured on the session host against the
four pinned model repositories' own processors and tokenizers (the measurement
recorded in this branch's token-cost study, reproduced in the report beside it).
They are pinned rather than recomputed by a second implementation of the same
formula: a drift in this module's ``smart_resize`` rewrite must fail against a
*measured* value, not against another copy of the arithmetic that would drift
with it.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from common.chairs.config import load_models_toml
from common.chairs.models import ChairIdentity
from common.imaging import encode_grayscale_png
from common.request_capacity import (
    MEASURED_ACT_ANSWER_TOKENS,
    MEASURED_DENSE_PAGE_ANSWER_TOKENS,
    MEASURED_PROMPT_TOKENS,
    PERLECTOR_MEASURED_TOKENIZER,
    PERLECTOR_PROMPT_FLOOR_TOKENS,
    PROMPT_TOKENS_MEASURED_CONSTANT,
    PROMPT_TOKENS_MEASURED_FLOOR,
    PROMPT_TOKENS_MEASURED_RATE,
    SCHEMA,
    RequestCapacityRefusal,
    act_answer_budget,
    dense_page_answer_budget,
    image_prompt_tokens,
    image_sizes,
    perlector_prompt_tokens,
    prompt_digest,
    refuse_unless_it_fits,
    request_fits,
    resized_dimensions,
    row_image_geometry,
    sealed_prompt_tokens,
)

# The three tiers' sealed pixel budgets, and the two patch families the roster
# actually holds: Qwen3-VL (Chandra, the Perlector) at patch 16 -- 1,024 px per
# image token -- and Qwen2.5-VL (DAI, Churro) at patch 14 -- 784 px per token.
MIN_PIXELS = 3136
TIER_MAX_PIXELS = {
    "generic-24gb": 1_806_336,
    "generic-48gb": 3_211_264,
    "generic-80gb-plus": 5_299_200,
}
QWEN3_VL = {"patch_size": 16, "merge_size": 2}
QWEN25_VL = {"patch_size": 14, "merge_size": 2}

A4_300DPI = (2480, 3508)
FIXTURE_PAGE = (200, 260)

ROOT = Path(__file__).resolve().parents[1]


def _row(**overrides):
    """A stand-in serving row: the six fields the capacity check reads, by name."""

    fields = {
        "recipe": "unproven-real-attestatores",
        "chair": "attestator_3",
        "tier": "generic-80gb-plus",
        "max_model_len": 8192,
        "min_pixels": MIN_PIXELS,
        "max_pixels": TIER_MAX_PIXELS["generic-80gb-plus"],
        "patch_size": 14,
        "merge_size": 2,
    }
    fields.update(overrides)
    return SimpleNamespace(**fields)


# =========================== the image arithmetic ============================


@pytest.mark.parametrize(
    "tier, family, expected_tokens, expected_resized",
    [
        # Measured: an A4 300-dpi scan, at each tier's sealed max_pixels, on
        # each patch family.  `resized` is (width, height) as the chair sees it.
        ("generic-24gb", QWEN3_VL, 1715, (1120, 1568)),
        ("generic-24gb", QWEN25_VL, 2280, (1120, 1596)),
        ("generic-48gb", QWEN3_VL, 3102, (1504, 2112)),
        ("generic-48gb", QWEN25_VL, 4028, (1484, 2128)),
        ("generic-80gb-plus", QWEN3_VL, 5100, (1920, 2720)),
        ("generic-80gb-plus", QWEN25_VL, 6693, (1932, 2716)),
    ],
)
def test_an_a4_page_costs_the_measured_number_of_prompt_tokens_at_every_tier(
    tier, family, expected_tokens, expected_resized
):
    width, height = A4_300DPI
    assert (
        image_prompt_tokens(
            width, height, min_pixels=MIN_PIXELS, max_pixels=TIER_MAX_PIXELS[tier], **family
        )
        == expected_tokens
    )
    assert (
        resized_dimensions(
            width, height, min_pixels=MIN_PIXELS, max_pixels=TIER_MAX_PIXELS[tier], **family
        )
        == expected_resized
    )


@pytest.mark.parametrize("family, expected", [(QWEN3_VL, 48), (QWEN25_VL, 63)])
@pytest.mark.parametrize("tier", sorted(TIER_MAX_PIXELS))
def test_the_fixture_pages_cost_the_same_two_orders_of_magnitude_less_at_every_tier(
    family, expected, tier
):
    """The fixtures are 200x260 and never come near any tier's pixel budget.

    Measured at 48 tokens on the Qwen3-VL chairs and 63 on the Qwen2.5-VL ones,
    identically at all three tiers -- which is itself the finding: no fixture
    run exercises the arithmetic that decides the first real page.
    """

    width, height = FIXTURE_PAGE
    assert (
        image_prompt_tokens(
            width, height, min_pixels=MIN_PIXELS, max_pixels=TIER_MAX_PIXELS[tier], **family
        )
        == expected
    )


@pytest.mark.parametrize(
    "size, tier, family, expected",
    [
        # DAI's own adapter ceilings (`feeding.dai_dimensions`) bind before the
        # tier's, so the page it is shown is already 1291x1826 by the time the
        # serving row's max_pixels sees it.
        ((1291, 1826), "generic-24gb", QWEN25_VL, 2280),
        ((1291, 1826), "generic-48gb", QWEN25_VL, 2990),
        ((1291, 1826), "generic-80gb-plus", QWEN25_VL, 2990),
        # A Perlector act's region crop: full page width, one sixth of its
        # height.  Its cost is the same at 24 GB and 80 GB -- the crop is small
        # enough that no tier's max_pixels binds on it.
        ((2480, 584), "generic-24gb", QWEN3_VL, 1404),
        ((2480, 584), "generic-80gb-plus", QWEN3_VL, 1404),
    ],
)
def test_the_measured_crop_and_adapter_ceiling_costs(size, tier, family, expected):
    width, height = size
    assert (
        image_prompt_tokens(
            width, height, min_pixels=MIN_PIXELS, max_pixels=TIER_MAX_PIXELS[tier], **family
        )
        == expected
    )


def test_a_tiny_image_is_scaled_up_to_the_rows_min_pixels():
    """The other branch of `smart_resize`: below min_pixels it grows, not shrinks.

    Pinned as two independent literals rather than as one function checked
    against the other.  Both `image_prompt_tokens` and `resized_dimensions`
    call `smart_resize`, so `tokens == (height // 28) * (width // 28)` compared
    two views of a single result and would have held through a one-sided
    `ceil`-to-`floor` regression that under-counts a small crop -- the direction
    that admits a request the row cannot hold.

    The two numbers are computed by hand from the formula rather than read off
    the code.  An 8x8 image at factor 28: `round(8 / 28) * 28` is 0 on both
    sides, which is below `min_pixels` 3136, so the grow branch runs with
    `beta = sqrt(3136 / 64) = 7`, giving `ceil(8 * 7 / 28) * 28 = 56` on each
    side -- 3136 pixels exactly, the row's floor -- and `(56 // 28) ** 2 = 4`
    image tokens.
    """

    tokens = image_prompt_tokens(8, 8, min_pixels=MIN_PIXELS, max_pixels=1_806_336, **QWEN25_VL)
    resized = resized_dimensions(8, 8, min_pixels=MIN_PIXELS, max_pixels=1_806_336, **QWEN25_VL)
    assert resized == (56, 56)
    assert resized[0] * resized[1] == MIN_PIXELS
    assert tokens == 4


def test_a_tiny_non_square_crop_is_grown_on_both_sides_at_the_librarys_rounding():
    """The square case above cannot see the rounding; this one can.

    At 8x8 the grow branch's `8 * 7 / 28` is 2 exactly, so `ceil` and `floor`
    land on the same 56 and a one-sided regression survives both literals.
    Executed rather than argued: with `math.ceil` replaced by `math.floor` in
    the grow branch, 8x8 still resizes to 56x56 for 4 tokens, and 60x20 falls
    from 112x56 for 8 tokens to 84x28 for 3 -- an under-count of five tokens on
    a crop the row would then be told it can hold.  So the non-square case is
    pinned beside the square one.

    By hand at factor 28: `round(20 / 28) * 28 = 28` and `round(60 / 28) * 28 =
    56`, whose 1,568 pixels are below `min_pixels` 3136, so the grow branch runs
    with `beta = sqrt(3136 / 1200) = 1.6166`; `ceil(20 * 1.6166 / 28) * 28 = 56`
    and `ceil(60 * 1.6166 / 28) * 28 = 112`, which is 6,272 pixels and
    `(56 // 28) * (112 // 28) = 8` image tokens.
    """

    tokens = image_prompt_tokens(60, 20, min_pixels=MIN_PIXELS, max_pixels=1_806_336, **QWEN25_VL)
    resized = resized_dimensions(60, 20, min_pixels=MIN_PIXELS, max_pixels=1_806_336, **QWEN25_VL)
    assert resized == (112, 56)
    assert resized[0] * resized[1] >= MIN_PIXELS
    assert tokens == 8


def test_an_image_further_from_square_than_the_processor_allows_is_refused_by_name():
    with pytest.raises(RequestCapacityRefusal) as refusal:
        image_prompt_tokens(1, 300, min_pixels=MIN_PIXELS, max_pixels=1_806_336, **QWEN25_VL)
    assert "aspect ratio" in str(refusal.value)


@pytest.mark.parametrize("bad", [0, -1, None, True, "14"])
def test_a_patch_or_merge_size_that_is_not_a_positive_integer_is_refused(bad):
    with pytest.raises(RequestCapacityRefusal):
        image_prompt_tokens(
            100, 100, min_pixels=MIN_PIXELS, max_pixels=1_806_336, patch_size=bad, merge_size=2
        )


def test_image_sizes_reads_the_pixels_actually_embedded():
    png = encode_grayscale_png(11, 7, [bytearray(b"\x00" * 11) for _ in range(7)])
    assert image_sizes([png, png]) == [(11, 7), (11, 7)]


# ============================ the sealed row =================================


@pytest.mark.parametrize("field", ["min_pixels", "max_pixels", "patch_size", "merge_size"])
def test_a_row_that_does_not_state_its_image_geometry_is_refused_by_name(field):
    with pytest.raises(RequestCapacityRefusal) as refusal:
        row_image_geometry(_row(**{field: None}))
    message = str(refusal.value)
    assert field in message
    assert "attestator_3" in message and "generic-80gb-plus" in message
    # The refusal says why a default is not available, not merely that a field
    # is missing: half the roster would be mis-counted by a third.
    assert "784" in message and "1,024" in message


def test_a_row_missing_max_model_len_is_refused_before_any_arithmetic_runs():
    with pytest.raises(RequestCapacityRefusal) as refusal:
        request_fits(_row(max_model_len=None), [A4_300DPI], 281, 1433)
    assert "max_model_len" in str(refusal.value)


# =========================== the capacity record =============================


def test_the_record_is_closed_and_names_every_image_it_counted():
    record = request_fits(_row(), [A4_300DPI, FIXTURE_PAGE], 281, 1433)
    assert record["schema"] == SCHEMA
    assert record["chair"] == "attestator_3"
    assert [entry["image_prompt_tokens"] for entry in record["images"]] == [6693, 63]
    assert record["image_prompt_tokens"] == 6756
    assert record["prompt_tokens"] == 281
    assert record["answer_budget"] == 1433
    assert record["need"] == 6756 + 281 + 1433
    assert record["headroom"] == 8192 - record["need"]
    assert record["fits"] is False
    assert record["prompt_tokens_basis"] == PROMPT_TOKENS_MEASURED_CONSTANT
    assert str(-record["headroom"]) in record["reason"]


def test_churro_on_a_dense_a4_page_overruns_the_shipped_eighty_gigabyte_context():
    """The measured finding, as a record: 6,974 prompt against 8,192, 1,218 left
    for a 1,433-token dense-page answer.  Over by 215."""

    record = request_fits(_row(), [A4_300DPI], MEASURED_PROMPT_TOKENS["attestator_3"].tokens, 1433)
    assert record["image_prompt_tokens"] + record["prompt_tokens"] == 6974
    assert record["need"] == 8407
    assert record["headroom"] == -215
    assert record["fits"] is False


def test_the_same_request_fits_once_the_row_states_a_larger_context():
    record = request_fits(
        _row(max_model_len=16384), [A4_300DPI], MEASURED_PROMPT_TOKENS["attestator_3"].tokens, 1433
    )
    assert record["fits"] is True
    assert record["headroom"] == 16384 - 8407
    assert record["reason"] is None


def test_refuse_unless_it_fits_carries_the_whole_record_on_the_refusal():
    with pytest.raises(RequestCapacityRefusal) as refusal:
        refuse_unless_it_fits(_row(), [A4_300DPI], 281, 1433, what="one Churro page request")
    assert refusal.value.capacity is not None
    assert refusal.value.capacity["need"] == 8407
    assert refusal.value.capacity["fits"] is False
    assert "one Churro page request" in str(refusal.value)
    # Never a silent downscale: the refusal says so in as many words.
    assert "downscaled" in str(refusal.value)


def test_refuse_unless_it_fits_returns_the_record_when_it_does_fit():
    record = refuse_unless_it_fits(
        _row(max_model_len=16384), [A4_300DPI], 281, 1433, what="one Churro page request"
    )
    assert record["fits"] is True


def test_an_unnamed_prompt_token_basis_is_refused():
    with pytest.raises(RequestCapacityRefusal) as refusal:
        request_fits(_row(), [A4_300DPI], 281, 1433, prompt_tokens_basis="guessed")
    assert "basis" in str(refusal.value)


@pytest.mark.parametrize("field", ["prompt_tokens", "answer_budget"])
def test_a_negative_count_is_refused_rather_than_defaulted(field):
    kwargs = {"prompt_tokens": 281, "answer_budget": 1433, field: -1}
    with pytest.raises(RequestCapacityRefusal) as refusal:
        request_fits(_row(), [A4_300DPI], **kwargs)
    assert field in str(refusal.value)


# ===================== the sealed prompt and answer costs ====================


# The measured constants are bound to their prompts by digest.  That the digest
# still matches the prompt each stage actually sends is asserted where those
# prompts live -- `pipeline/2_designator/test_structure_pass.py` and
# `pipeline/3_attestatores/test_live_witness.py` -- so this module never has to
# import a stage across the boundary `common/README.md` draws.


def test_an_edited_prompt_invalidates_its_measured_token_count():
    with pytest.raises(RequestCapacityRefusal) as refusal:
        sealed_prompt_tokens("attestator_3", "a system prompt nobody measured", "and its user turn")
    message = str(refusal.value)
    assert "changed after it was measured" in message
    assert MEASURED_PROMPT_TOKENS["attestator_3"].prompt_digest in message


def test_a_chair_with_no_measurement_is_refused_rather_than_estimated():
    with pytest.raises(RequestCapacityRefusal) as refusal:
        sealed_prompt_tokens("perlector", "some text")
    assert "no measured prompt-token count" in str(refusal.value)


def test_prompt_digest_is_order_sensitive():
    assert prompt_digest("a", "b") != prompt_digest("b", "a")


def test_the_perlector_prompt_never_counts_below_its_measured_floor():
    tokens, basis = perlector_prompt_tokens("one two three")
    assert tokens == PERLECTOR_PROMPT_FLOOR_TOKENS
    assert basis == PROMPT_TOKENS_MEASURED_FLOOR


def test_a_large_perlector_dossier_counts_by_the_measured_rate_and_says_so():
    words = 1000
    tokens, basis = perlector_prompt_tokens(" ".join(["mot"] * words))
    # 120 tokens per 73 words, measured on this chair's own tokenizer over
    # 18th-century French register prose; ceiling division, exact integers.
    assert tokens == -(-words * 120 // 73) == 1644
    assert basis == PROMPT_TOKENS_MEASURED_RATE


@pytest.mark.parametrize(
    "chair, expected",
    [
        ("designator_structure", 1575),
        ("attestator_1", 1520),
        ("attestator_2", 1426),
        ("attestator_3", 1433),
        ("perlector", 1318),
    ],
)
def test_the_measured_dense_page_answer_budgets(chair, expected):
    assert dense_page_answer_budget(chair) == expected
    assert MEASURED_DENSE_PAGE_ANSWER_TOKENS[chair] == expected


def test_a_chair_with_no_measured_answer_budget_is_refused():
    with pytest.raises(RequestCapacityRefusal) as refusal:
        dense_page_answer_budget("recensor")
    assert "no measured dense-page answer budget" in str(refusal.value)


@pytest.mark.parametrize("chair, expected", [("attestator_2", 230), ("perlector", 216)])
def test_the_measured_single_act_answer_budgets(chair, expected):
    """The two act-scoped chairs reserve one act's answer, not a page's."""

    assert act_answer_budget(chair) == expected
    assert MEASURED_ACT_ANSWER_TOKENS[chair] == expected


@pytest.mark.parametrize("chair", ["designator_structure", "attestator_1", "attestator_3"])
def test_a_page_scoped_chair_has_no_single_act_budget_to_reserve(chair):
    """The page chairs are never asked for one act, so nothing measured one."""

    with pytest.raises(RequestCapacityRefusal) as refusal:
        act_answer_budget(chair)
    assert "no measured single-act answer budget" in str(refusal.value)


def test_dais_ordinary_act_stays_admissible_at_the_smallest_row():
    """The one chair measured sound at 24 GB must not be refused into silence.

    Reserving a whole page's answer for a request that asked for one act would
    put 702 + 84 + 1,426 against a 2,048-token row and refuse a call that
    measurably works. GOALS 1: a refused act is a missed act.
    """

    row = _row(chair="attestator_2", max_model_len=2048, max_pixels=TIER_MAX_PIXELS["generic-24gb"])
    ordinary = request_fits(row, [(1500, 353)], 84, act_answer_budget("attestator_2"))
    assert ordinary["need"] == 702 + 84 + 230 == 1016
    assert ordinary["fits"] is True
    # And the page-fallback act at the same row is still refused, on its image
    # cost alone, with the same smaller budget reserved.
    fallback = request_fits(row, [(1291, 1826)], 84, act_answer_budget("attestator_2"))
    assert fallback["image_prompt_tokens"] == 2280
    assert fallback["fits"] is False


# ===================== what the measurements are bound to ====================


def test_every_measured_prompt_names_the_tokenizer_the_real_roster_pins():
    """A measurement expires when its chair is repointed, not only when its prompt is.

    The same reconciliation `common/chairs/test_model_store.py` runs between
    the materialization inventory and `config/models.toml`, for the same
    reason: two lists of pinned repositories that agree only by coincidence
    will eventually stop agreeing, and nothing would notice. Here the drift is
    quieter still -- a chair repointed at a new revision keeps its prompt
    digest, so `sealed_prompt_tokens` would go on returning a count taken with
    a tokenizer that model no longer has.

    The Perlector is checked with the rest. It has no fixed prompt to digest,
    which makes the pinned revision the *only* thing that can expire its floor
    and its tokens-per-word rate.
    """

    roster = load_models_toml(ROOT / "config" / "models-real.toml").chairs
    measured = {
        chair: (entry.repo, entry.revision) for chair, entry in MEASURED_PROMPT_TOKENS.items()
    }
    measured["perlector"] = PERLECTOR_MEASURED_TOKENIZER

    disagreements = []
    for chair, (repo, revision) in sorted(measured.items()):
        identity = roster.get(chair)
        if not isinstance(identity, ChairIdentity):
            disagreements.append(f"{chair}: measured against {repo}@{revision}, roster has none")
            continue
        if (identity.repo, identity.revision) != (repo, revision):
            disagreements.append(
                f"{chair}: measured against {repo}@{revision}, "
                f"roster says {identity.repo}@{identity.revision}"
            )
    assert disagreements == []


def test_every_configured_real_chair_that_sends_a_request_carries_a_measurement():
    """The reconciliation in the other direction: no chair measured by nobody.

    A chair added to the real roster that some seam then asks a request for
    would be refused at run time by `sealed_prompt_tokens` for want of a
    measurement. That is the right refusal and the wrong moment -- it happens
    on a pod. The roster's own configured reading chairs are checked here
    instead. `secondary_proposer` and `annotator` are absent from the roster by
    ruling, and an absent chair sends nothing.
    """

    roster = load_models_toml(ROOT / "config" / "models-real.toml").chairs
    configured = {
        chair for chair, identity in roster.items() if isinstance(identity, ChairIdentity)
    }
    measured = set(MEASURED_PROMPT_TOKENS) | {"perlector"}
    assert configured == measured


# ============================ the rounding mode ==============================


@pytest.mark.parametrize(
    "family, size, expected_resized, expected_tokens",
    [
        # Both sides land on an exact .5 of the chair's own factor: 70/28 and
        # 98/28 are 2.5 and 3.5, 80/32 and 112/32 are 2.5 and 3.5. `round` is
        # Python's banker's rounding in the library and in this rewrite alike,
        # so 2.5 goes DOWN to 2 and 3.5 goes UP to 4 -- half-to-even, not
        # half-up. Under half-up both would round up and the image would cost
        # 12 tokens instead of 8.
        (QWEN25_VL, (98, 70), (112, 56), 8),
        (QWEN3_VL, (112, 80), (128, 64), 8),
    ],
    ids=["qwen2.5-vl-factor-28", "qwen3-vl-factor-32"],
)
def test_an_exact_half_rounds_to_even_exactly_as_the_library_does(
    family, size, expected_resized, expected_tokens
):
    """The one arithmetic difference a rewrite is most likely to get wrong.

    Every other pinned case in this module rounds a value nowhere near .5, so
    a rewrite that used half-up rounding would pass all of them. These two are
    computed with the library's own `smart_resize`, lifted verbatim by `ast`
    out of the installed `transformers` 5.16.1 and executed (the study script
    beside `TOKEN_COST_REPORT.md`, section 3), and pinned here as its results
    rather than as this module's.
    """

    width, height = size
    assert (
        resized_dimensions(
            width,
            height,
            min_pixels=MIN_PIXELS,
            max_pixels=TIER_MAX_PIXELS["generic-24gb"],
            **family,
        )
        == expected_resized
    )
    assert (
        image_prompt_tokens(
            width,
            height,
            min_pixels=MIN_PIXELS,
            max_pixels=TIER_MAX_PIXELS["generic-24gb"],
            **family,
        )
        == expected_tokens
    )
