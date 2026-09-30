"""Every shipped real serving row, against the arithmetic that falsified it.

Read from the catalogue the operator actually ships rather than a hand-typed
copy: the defect this guards against was a whole catalogue of contexts no real
page could be served under, so the shipped bytes are the only ones worth
asserting against. It lives here rather than beside
`common/request_capacity.py` because loading a catalogue is this package's job
and `common/` imports nothing from `operations/`.
"""

from __future__ import annotations

import sys
import tomllib
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

from common.churro_document import CHURRO_PROMPT_VARIANTS
from common.request_capacity import (
    DECLARED_ANSWER_BOUND_TOKENS,
    MEASURED_PROMPT_TOKENS,
    PERLECTOR_REPRESENTATIVE_PROMPT_BOUND_TOKENS,
    act_answer_budget,
    dense_page_answer_budget,
    request_fits,
    row_image_geometry,
    sendable_max_tokens,
)
from operations.serving.config import ServingProfile, UnsupportedProfile, load_serving_recipes

_ATTESTATORES_DIR = Path(__file__).resolve().parents[2] / "pipeline" / "3_attestatores"
if str(_ATTESTATORES_DIR) not in sys.path:
    sys.path.insert(0, str(_ATTESTATORES_DIR))

import churro  # noqa: E402

MIN_PIXELS = 3136
TIER_MAX_PIXELS = {
    "generic-24gb": 1_806_336,
    "generic-48gb": 3_211_264,
    "generic-80gb-plus": 5_299_200,
}
A4_300DPI = (2480, 3508)


REPO_ROOT = Path(__file__).resolve().parents[2]
REAL_RECIPES = REPO_ROOT / "config" / "serving_recipes_real.toml"
REAL_PLACEMENT = REPO_ROOT / "config" / "pod_placement.toml"

# A 300-dpi A4 scan as each chair is actually shown it.  DAI is act-scoped and
# its adapter's own width ceiling (`pipeline/3_attestatores/feeding.dai_dimensions`:
# width <= 1500) binds before any serving row's `max_pixels`, so the page it is
# charged for is 1500x2122.  Every other chair
# is shown the sealed page unchanged.
PAGE_AS_PRESENTED = {
    "designator_structure": A4_300DPI,
    "attestator_1": A4_300DPI,
    "attestator_2": (1500, 2122),
    "attestator_3": A4_300DPI,
    "perlector": A4_300DPI,
}
# The Perlector's region crop, modelled as `TOKEN_COST_REPORT.md` section 7
# models it: full page width by one sixth of page height.
ACT_REGION_CROP = (2480, 584)
# The Perlector's prompt has no fixed text to seal, so what stands in for a
# measured constant here is what the seam actually admits on: the sealed
# tokens-per-character bound over the representative dossier of
# `TOKEN_COST_REPORT.md` section 5. Weighing the shipped rows against the floor
# would ask whether they can serve a request smaller than any the reader lets
# through.
PROMPT_TOKENS = {
    # The default framing's cost per chair: `MEASURED_PROMPT_TOKENS` carries
    # one entry per framing a chair can be asked in, and the first is the one a
    # run sends unless it names another (`churro.DEFAULT_FRAMING`).
    **{chair: entries[0].tokens for chair, entries in MEASURED_PROMPT_TOKENS.items()},
    "perlector": PERLECTOR_REPRESENTATIVE_PROMPT_BOUND_TOKENS,
}
# The comment above is true only because entry 0 happens to be Churro's
# default framing's own measurement today; nothing enforces the order, so a
# tuple reordered on a later edit would silently swap in the wrong framing's
# cost here. Checked once, by digest, against the framing `churro.py` itself
# currently names as the default -- not asserted structurally on every
# access, and not against a name hard-coded here, so a repointed
# `DEFAULT_FRAMING` fails this rather than going unnoticed.
assert (
    MEASURED_PROMPT_TOKENS["attestator_3"][0].prompt_digest
    == CHURRO_PROMPT_VARIANTS[churro.DEFAULT_FRAMING]["system_sha256"]
), "MEASURED_PROMPT_TOKENS['attestator_3'][0] is no longer churro.DEFAULT_FRAMING's measurement"


def _shipped_rows():
    rows = [
        row
        for row in load_serving_recipes(REAL_RECIPES).profiles
        if isinstance(row, ServingProfile)
    ]
    assert rows, "the shipped real catalogue names no vLLM serving row"
    return rows


def _request_shapes(row):
    """Every request shape this row must be able to serve, as the seam sends it.

    The four page chairs send one image and reserve a dense page's answer.  The
    Perlector is the one chair whose request is not one image: it sends every
    region crop and then every page render, one pair per capture view
    (`pipeline/4_perlector/live_reader.py`), and reserves one act's reading
    unless the act's own crop is page-sized -- a page-fallback act, whose
    reading is a page of text.  Testing it at one image and one act's answer
    would have described a request this pipeline never sends.

    The two-capture-view page-fallback act is deliberately absent here and
    pinned on its own below: it is the one measured shape a shipped row cannot
    serve, and asserting it fits would be false.
    """

    chair = row.chair
    page = PAGE_AS_PRESENTED[chair]
    if chair != "perlector":
        return [("a dense A4 page", [page], dense_page_answer_budget(chair))]
    act = act_answer_budget(chair)
    fallback = dense_page_answer_budget(chair)
    return [
        ("one capture view, ordinary act", [ACT_REGION_CROP, page], act),
        (
            "two capture views, ordinary act",
            [ACT_REGION_CROP, ACT_REGION_CROP, page, page],
            act,
        ),
        ("one capture view, page-fallback act", [page, page], fallback),
    ]


@pytest.mark.parametrize(
    "row,case",
    [(row, case) for row in _shipped_rows() for case in _request_shapes(row)],
    ids=lambda item: (
        f"{item.chair}@{item.tier}" if hasattr(item, "chair") else item[0].replace(" ", "-")
    ),
)
def test_catalogue_capacity_arithmetic_accepts_each_shipped_row_request_shape(row, case):
    """The catalogue's own claim, checked against the arithmetic that falsified it."""

    _label, images, answer_budget = case
    record = request_fits(row, images, PROMPT_TOKENS[row.chair], answer_budget)
    assert record["fits"] is True, record["reason"]
    assert record["headroom"] >= 0


def test_churro_80gb_admits_the_complete_vendor_answer_bound():
    row = next(
        row
        for row in _shipped_rows()
        if row.chair == "attestator_3" and row.tier == "generic-80gb-plus"
    )
    declared_answer = DECLARED_ANSWER_BOUND_TOKENS[row.chair]
    capacity = request_fits(row, [A4_300DPI], PROMPT_TOKENS[row.chair], declared_answer)
    assert capacity["fits"] is True, capacity["reason"]
    assert sendable_max_tokens(row.chair, capacity) == {"max_tokens": declared_answer}
    old_capacity = request_fits(
        replace(row, max_model_len=8192),
        [A4_300DPI],
        PROMPT_TOKENS[row.chair],
        declared_answer,
    )
    assert old_capacity["fits"] is False


def test_chandra_80gb_admits_its_native_bound_after_the_observed_page_three_prompt():
    """The live page-three arithmetic keeps Chandra's native cap on the wire.

    The retained page used 6,731 prompt tokens. Chandra's declared 12,384
    output-token bound therefore needs 19,115 tokens in total. The former
    18,000-token row leaves only 11,269 and binds generation by context; the
    changed 20,480-token row leaves enough slack for the declared bound to be
    sent. This is capacity accounting only: it does not attribute or cure the
    observed repeated-line output.
    """

    row = next(
        row
        for row in _shipped_rows()
        if row.chair == "attestator_1" and row.tier == "generic-80gb-plus"
    )
    # The actual retained Chandra presentation was 2100x2968; the processor
    # reported 6,138 image tokens plus the sealed 593-token text prompt.
    images = [(2100, 2968)]
    prompt_tokens = PROMPT_TOKENS[row.chair]
    assert prompt_tokens == 593
    declared_answer = DECLARED_ANSWER_BOUND_TOKENS[row.chair]
    assert row.max_model_len == 20_480

    capacity = request_fits(row, images, prompt_tokens, declared_answer)
    assert capacity["image_prompt_tokens"] == 6_138
    assert capacity["image_prompt_tokens"] + capacity["prompt_tokens"] == 6_731
    assert capacity["need"] == 19_115
    assert capacity["fits"] is True, capacity["reason"]
    assert capacity["headroom"] == 1_365
    assert sendable_max_tokens(row.chair, capacity) == {"max_tokens": declared_answer}

    old_capacity = request_fits(
        replace(row, max_model_len=18_000), images, prompt_tokens, declared_answer
    )
    assert old_capacity["fits"] is False
    assert old_capacity["headroom"] == -1_115
    assert sendable_max_tokens(row.chair, old_capacity) == {}


def test_the_two_view_page_fallback_act_fits_the_supported_tiers_context():
    """The one measured Perlector shape that used to overrun a shipped row."""

    needs = {}
    for row in _shipped_rows():
        if row.chair != "perlector":
            continue
        page = PAGE_AS_PRESENTED["perlector"]
        record = request_fits(
            row,
            [page, page, page, page],
            PROMPT_TOKENS["perlector"],
            dense_page_answer_budget("perlector"),
        )
        needs[row.tier] = (record["need"], record["fits"])
    assert needs == {
        # 4x5,100 + 1,173 + 1,318, against 32,768
        "generic-80gb-plus": (22891, True),
    }


def test_smaller_perlector_tiers_name_the_measured_refusal():
    rows = [
        row
        for row in load_serving_recipes(REAL_RECIPES).profiles
        if isinstance(row, UnsupportedProfile) and row.chair == "perlector"
    ]
    assert {row.tier for row in rows} == {"generic-24gb", "generic-48gb"}
    assert all("51.7 GiB" in row.reason for row in rows)


@pytest.mark.parametrize("row", _shipped_rows(), ids=lambda row: f"{row.chair}@{row.tier}")
def test_every_shipped_real_row_states_the_geometry_its_token_cost_needs(row):
    geometry = row_image_geometry(row)
    # Chandra and the Perlector are Qwen3-VL (patch 16); DAI and Churro are
    # Qwen2.5-VL (patch 14).  Both merge 2.
    expected_patch = 14 if row.chair in {"attestator_2", "attestator_3"} else 16
    assert (geometry.patch_size, geometry.merge_size) == (expected_patch, 2)


def test_no_shipped_row_exceeds_its_tiers_context_cap():
    """`operations/serving/preflight.py` refuses a row above its tier's cap.

    Asserted here rather than left to preflight because the two files moved
    together and a cap left behind would turn every raised row into a refusal
    nobody could clear without a pod.
    """

    placement = tomllib.loads(REAL_PLACEMENT.read_text(encoding="utf-8"))
    caps = {tier["id"]: tier["recipe"]["context_cap"] for tier in placement["tiers"]}
    for row in _shipped_rows():
        assert row.max_model_len <= caps[row.tier], (row.chair, row.tier)


def test_the_measured_failures_this_change_answers_are_still_failures_at_the_old_numbers():
    """The counterfactual, against the numbers the catalogue used to ship.

    Kept because the fix is a config change: without this, a later edit could
    put the old contexts back and nothing would notice until a card was rented.
    """

    old = {
        ("attestator_3", "generic-24gb"): (2048, TIER_MAX_PIXELS["generic-24gb"], 14),
        ("attestator_3", "generic-48gb"): (4096, TIER_MAX_PIXELS["generic-48gb"], 14),
        ("attestator_3", "generic-80gb-plus"): (8192, TIER_MAX_PIXELS["generic-80gb-plus"], 14),
        ("designator_structure", "generic-24gb"): (2048, TIER_MAX_PIXELS["generic-24gb"], 16),
        ("attestator_1", "generic-24gb"): (2048, TIER_MAX_PIXELS["generic-24gb"], 16),
    }
    for (chair, tier), (context, max_pixels, patch) in old.items():
        row = SimpleNamespace(
            recipe="unproven-real",
            chair=chair,
            tier=tier,
            max_model_len=context,
            min_pixels=MIN_PIXELS,
            max_pixels=max_pixels,
            patch_size=patch,
            merge_size=2,
        )
        record = request_fits(
            row,
            [PAGE_AS_PRESENTED[chair]],
            PROMPT_TOKENS[chair],
            dense_page_answer_budget(chair),
        )
        assert record["fits"] is False, (chair, tier)


def test_measured_witness_rows_have_a_600s_startup_budget() -> None:
    measured = [
        (profile.chair, profile.tier, profile.startup_timeout_seconds)
        for profile in _shipped_rows()
        if profile.chair in {"designator_structure", "attestator_1", "attestator_2", "attestator_3"}
    ]
    assert len(measured) == 12
    assert all(timeout == 600 for _, _, timeout in measured)
    assert [
        (profile.tier, profile.startup_timeout_seconds)
        for profile in _shipped_rows()
        if profile.chair == "perlector"
    ] == [
        ("generic-80gb-plus", 600),
    ]


# F005/F052: `operations/serving/preflight.py` only refuses a row's
# `gpu_memory_utilization` above its tier's `engine_memory_fraction` ceiling
# (config/pod_placement.toml) -- nothing checks a row against the VRAM floor
# this project's own arithmetic already computed for it, so a too-low value
# passes every existing gate silently and only fails once a pod is rented and
# vLLM aborts during engine init.  Every figure below is copied from the
# stated derivation, not recomputed: `config/pod_placement.toml`'s
# `generic-24gb` tier comment states DAI (attestator_2) needs 18.7 GiB of 24
# (0.78 minimum, 0.90 matching the old `serve_dai.sh`).  No other row has a
# VRAM figure derived anywhere in this tree today, so no other row is checked
# here -- adding one without a stated derivation would be inventing the
# number this test exists to hold the catalogue to.
_STATED_VRAM_NEED_GIB = {
    ("attestator_2", "generic-24gb"): 18.7,
}
_TIER_VRAM_GIB = {
    "generic-24gb": 24,
    "generic-48gb": 48,
    "generic-80gb-plus": 80,
}


def test_no_shipped_row_serves_below_its_tiers_stated_vram_floor():
    """A row that starts below its own documented VRAM need is not unproven.

    It is wrong the same way an over-context row is wrong (see the test
    above): `render_vllm_argv` (`operations/serving/manager.py`) passes
    `gpu_memory_utilization` straight to `--gpu-memory-utilization`, so a
    value below the derived floor is a VRAM allocation failure waiting for a
    rented card to discover, not a planning choice.
    """

    checked = 0
    for row in _shipped_rows():
        need_gib = _STATED_VRAM_NEED_GIB.get((row.chair, row.tier))
        if need_gib is None:
            continue
        checked += 1
        required_fraction = need_gib / _TIER_VRAM_GIB[row.tier]
        actual_fraction = float(row.gpu_memory_utilization)
        assert actual_fraction >= required_fraction, (
            f"{row.chair}@{row.tier} carries gpu_memory_utilization="
            f"{row.gpu_memory_utilization!r}, below the "
            f"{required_fraction:.4f} this project's own arithmetic needs "
            f"({need_gib} GiB of {_TIER_VRAM_GIB[row.tier]})"
        )
    assert checked == len(_STATED_VRAM_NEED_GIB), "a stated VRAM floor went unchecked"


# --- the Perlector's page render against the row, shape by shape ------------------

from operations.corpus import perlector_request_fit as fit  # noqa: E402

LETTER = (2550, 3300)
WHOLE = (0, 0, 2550, 3300)
TOP, BOTTOM = (0, 0, 2550, 1700), (0, 1600, 2550, 1700)
_PROSE = (
    "L'an mil sept cent quarante et un, le douzième jour de février, a été baptisée "
    "par nous soussigné prêtre curé de cette paroisse Marie Anne, fille légitime de "
)
# Two pages of register text per witness (twice the longest gold act, 2,972
# characters), a full neighbour cap, and a fed prior draft longer than the reading
# cap, so it is charged the cap.
DENSE = (_PROSE * 60)[:5944]
# Each neighbour sits on the act's own page.
NEIGHBOUR = ([(_PROSE * 10)[:800]] * 3, True)
PRIOR = (_PROSE * 60)[:6000]

# (pages as (size, crops), prior) -> (need under the sealed rule, fits; need with
# every page at the old 1,024 edge, fits). Needs are pinned so a prompt or render
# change that moves them is seen here.
SHAPES = {
    "over a page turn, whole-page crops": (
        [(LETTER, [WHOLE]), (LETTER, [WHOLE])],
        None,
        (29064, True),
        (29064, True),
    ),
    "dense over a page turn, half-page crops, fed prior": (
        [(LETTER, [BOTTOM]), (LETTER, [TOP])],
        PRIOR,
        (31514, True),
        (31514, True),
    ),
    "dense over a page turn, a whole-page recovery crop, fed prior": (
        [(LETTER, [BOTTOM]), (LETTER, [TOP, WHOLE])],
        PRIOR,
        (36617, False),
        (36617, False),
    ),
    "three pages, whole-page crops": (
        [(LETTER, [WHOLE])] * 3,
        None,
        (34967, False),
        (34967, False),
    ),
}


@pytest.mark.parametrize("name", SHAPES)
def test_each_pinned_request_shape_against_the_row_under_both_page_renders(name):
    """What the sealed `[page_context]` rule costs, shape by shape, against the old render.

    A page the act's crops cover whole, and every page of an act spanning more than
    one page, is rendered at the layout edge, so each shape here costs exactly what
    it did at 1,024: the page render refuses no act the old render admitted. The two
    shapes the row does not hold were refused at 1,024 too. Every gold act fits
    under both (`operations/corpus/perlector_request_fit.py`).
    """
    pages, prior, sealed_rule, old = SHAPES[name]
    row, sealed = fit.perlector_row(), fit.sealed_protocol()
    arguments = dict(
        pages=pages,
        witness_texts=[DENSE] * 3,
        neighbours=(NEIGHBOUR, NEIGHBOUR),
        prior_text=prior,
    )
    now = fit.request_record(row, sealed, edge=None, **arguments)
    before = fit.request_record(row, sealed, edge=fit.OLD_EDGE, **arguments)
    assert (now["need"], now["fits"]) == sealed_rule
    assert (before["need"], before["fits"]) == old
    assert row.max_model_len == 32768


def test_a_legible_render_stays_inside_the_rows_pixel_bound():
    """So the chair sees exactly the rendered pixels, on a letter leaf and on A4."""
    row, sealed = fit.perlector_row(), fit.sealed_protocol()
    for page in (LETTER, A4_300DPI):
        width, height = fit._rendered(page, sealed["page_context"]["maximum_edge"])
        assert width * height <= row.max_pixels


# --- vendor-fidelity pins for the shipped rows -----------------------------------


def test_dai_rows_carry_the_vendor_processors_own_pixel_range_and_hold_a_tall_act():
    """`processor_config.json` at the pinned DAI revision: shortest_edge 3,136,
    longest_edge 12,845,056.  A 1500x2500 act is about 4.8k image tokens, and it
    fits the 8,192-token row beside DAI's prompt and 1,024-token answer."""

    rows = [row for row in _shipped_rows() if row.chair == "attestator_2"]
    assert len(rows) == 3
    for row in rows:
        assert (row.min_pixels, row.max_pixels) == (3_136, 12_845_056), row.tier
        record = request_fits(
            row, [(1500, 2500)], PROMPT_TOKENS[row.chair], DECLARED_ANSWER_BOUND_TOKENS[row.chair]
        )
        assert record["image_prompt_tokens"] == 4_806, row.tier
        assert record["fits"] is True, record["reason"]
        assert row.max_model_len == 8_192


def test_every_churro_row_holds_the_vendors_whole_answer_bound_beside_a_full_page():
    """25,000 (`DEFAULT_OCR_MAX_TOKENS`, Churro v0.3.0) + the largest image the
    row's own `max_pixels` allows + the prompt, at every tier."""

    rows = [row for row in _shipped_rows() if row.chair == "attestator_3"]
    assert {row.tier for row in rows} == {"generic-24gb", "generic-48gb", "generic-80gb-plus"}
    assert DECLARED_ANSWER_BOUND_TOKENS["attestator_3"] == 25_000
    for row in rows:
        widest = row.max_pixels // 784 * 784  # a token-exact square-ish bound
        side = int(widest**0.5)
        capacity = request_fits(row, [(side, side)], PROMPT_TOKENS[row.chair], 25_000)
        assert capacity["fits"] is True, (row.tier, capacity["reason"])
        assert sendable_max_tokens(row.chair, capacity) == {"max_tokens": 25_000}


def test_perlector_min_pixels_is_the_vendors_shortest_edge_and_no_row_trusts_remote_code():
    """Qwen3.8-27B `preprocessor_config.json`: size.shortest_edge 65,536.  None of
    the four pinned repositories ships a `.py` file or an `auto_map`."""

    rows = _shipped_rows()
    assert all(not row.trust_remote_code for row in rows)
    perlector = [row for row in rows if row.chair == "perlector"]
    assert [row.min_pixels for row in perlector] == [65_536]
    tiny = request_fits(perlector[0], [(40, 40)], 100, 100)["images"][0]
    assert tiny["resized_width"] * tiny["resized_height"] >= 65_536
