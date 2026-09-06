"""Every shipped real serving row, against the arithmetic that falsified it.

Read from the catalogue the operator actually ships rather than a hand-typed
copy: the defect this guards against was a whole catalogue of contexts no real
page could be served under, so the shipped bytes are the only ones worth
asserting against. It lives here rather than beside
`common/request_capacity.py` because loading a catalogue is this package's job
and `common/` imports nothing from `operations/`.
"""

from __future__ import annotations

import tomllib
from pathlib import Path
from types import SimpleNamespace

import pytest

from common.request_capacity import (
    MEASURED_PROMPT_TOKENS,
    PERLECTOR_REPRESENTATIVE_PROMPT_BOUND_TOKENS,
    act_answer_budget,
    dense_page_answer_budget,
    request_fits,
    row_image_geometry,
)
from operations.serving.config import ServingProfile, load_serving_recipes

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
# its adapter's own ceilings (`pipeline/3_attestatores/feeding.dai_dimensions`:
# width <= 1500, height <= 4096, area <= 2359296) bind before any serving row's
# `max_pixels`, so the page it is charged for is 1291x1826.  Every other chair
# is shown the sealed page unchanged.
PAGE_AS_PRESENTED = {
    "designator_structure": A4_300DPI,
    "attestator_1": A4_300DPI,
    "attestator_2": (1291, 1826),
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
    **{chair: entry.tokens for chair, entry in MEASURED_PROMPT_TOKENS.items()},
    "perlector": PERLECTOR_REPRESENTATIVE_PROMPT_BOUND_TOKENS,
}


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
def test_every_shipped_real_row_can_serve_the_requests_its_chair_sends(row, case):
    """The catalogue's own claim, checked against the arithmetic that falsified it.

    Every row must hold the images its chair really sends at its own
    `max_pixels`, plus that chair's measured prompt, plus the answer that
    request reserves.  Before this branch none of the 24 GB rows could, two
    of the 48 GB rows could not fit the prompt alone, and Churro at 80 GB+ left
    1,058 tokens for a 1,631-token answer.  A row that provably cannot answer
    is not unproven; it is wrong, and the catalogue is not allowed to ship one.

    Churro is weighed here as the live chair is really asked -- the 441-token
    layout instruction and the JSON object it asks back -- not as the trained
    `<output>` carry the fixture posture declares.  Both of its numbers rose
    with that instruction and all three of its rows still hold the request.
    """

    _label, images, answer_budget = case
    record = request_fits(row, images, PROMPT_TOKENS[row.chair], answer_budget)
    assert record["fits"] is True, record["reason"]
    assert record["headroom"] >= 0


def test_the_two_view_page_fallback_act_is_served_at_two_tiers_and_named_at_the_third():
    """The one measured Perlector shape a shipped row cannot serve.

    An act whose bounds are the whole page, seen from two captures, sends four
    page-sized images.  At 24 GB and 48 GB the raised 16,384 holds them; at
    80 GB+ the same four images cost 20,400 tokens on their own, and no context
    this catalogue ships can hold the request.  The prompt charged here is the
    1,100 the seam admits on over the representative dossier, not the 790 floor
    it used to be weighed against; the verdicts are unchanged by the difference
    and the three needs move by 310 each.  Pinned rather than passed over:
    the pipeline refuses it on this laptop with the arithmetic
    (`pipeline/4_perlector/live_reader.py`), and a later edit that quietly
    changes which tiers can serve it changes this test.
    """

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
        # 4x1,715 + 1,100 + 1,318
        "generic-24gb": (9278, True),
        # 4x3,102 + 1,100 + 1,318
        "generic-48gb": (14826, True),
        # 4x5,100 + 1,100 + 1,318, against 16,384
        "generic-80gb-plus": (22818, False),
    }


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
