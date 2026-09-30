"""Whole-page Perlector request admission: the carried prompt bound and the answer reserve."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from common.request_capacity import (
    PAGE_ANSWER_ENTRY_SKELETON,
    PAGE_ANSWER_WRAPPER,
    PERLECTOR_PAGE_PROMPT_OVERHEAD_TOKENS,
    PERLECTOR_PAGE_PROMPT_TEMPLATE_DIGEST,
    PERLECTOR_PROMPT_OVERHEAD_TOKENS,
    PERLECTOR_PROMPT_TEMPLATE_DIGEST,
    PROMPT_TOKENS_ADMITTING_BASES,
    PROMPT_TOKENS_CARRIED_BOUND,
    RequestCapacityRefusal,
    page_answer_bound,
    page_request_capacity,
    perlector_page_prompt_bound,
    perlector_prompt_bound,
)

ROW = SimpleNamespace(
    recipe="unproven-real-perlector",
    chair="perlector",
    tier="generic-80gb-plus",
    max_model_len=32768,
    min_pixels=65536,
    max_pixels=5299200,
    patch_size=16,
    merge_size=2,
)
MEASURE = {"longest_witness_characters": 12_000, "act_entries": 20}


def _admit(row=ROW, *, text="x" * 40_000, measure=MEASURE, cap=12288, images=((1978, 2560),)):
    return page_request_capacity(
        row,
        image_sizes=images,
        prompt_text=text,
        template_digest=PERLECTOR_PAGE_PROMPT_TEMPLATE_DIGEST,
        answer_measure=measure,
        page_max_tokens=cap,
    )


def test_the_page_prompt_is_admitted_on_the_act_rate_named_as_carried():
    tokens, basis = perlector_page_prompt_bound(
        "x" * 10_000, template_digest=PERLECTOR_PAGE_PROMPT_TEMPLATE_DIGEST
    )
    act_tokens, _ = perlector_prompt_bound(
        "x" * 10_000, template_digest=PERLECTOR_PROMPT_TEMPLATE_DIGEST
    )
    # The same rate and margin as the act prompt; only the chat overhead differs
    # (two images at most, the render and its overlay, not the act path's thirty-two).
    assert (
        tokens - PERLECTOR_PAGE_PROMPT_OVERHEAD_TOKENS
        == act_tokens - PERLECTOR_PROMPT_OVERHEAD_TOKENS
    )
    assert tokens == 56 + 4334
    assert basis == PROMPT_TOKENS_CARRIED_BOUND
    assert PROMPT_TOKENS_CARRIED_BOUND in PROMPT_TOKENS_ADMITTING_BASES


def test_an_edited_page_builder_expires_the_carried_rate():
    with pytest.raises(RequestCapacityRefusal, match="page template changed"):
        perlector_page_prompt_bound("x", template_digest="0" * 64)


def test_the_answer_reserve_is_the_longest_witness_text_plus_each_entrys_scaffold():
    characters = 12_000 + 20 * len(PAGE_ANSWER_ENTRY_SKELETON) + len(PAGE_ANSWER_WRAPPER)
    expected = -(-characters * 4127 * 105 // (10_000 * 100))
    assert (
        page_answer_bound(longest_witness_characters=12_000, act_entries=20, page_max_tokens=12288)
        == expected
        == 6903
    )


def test_a_page_nothing_measures_reserves_the_whole_cap():
    assert (
        page_answer_bound(longest_witness_characters=0, act_entries=0, page_max_tokens=12288)
        == 12288
    )


def test_an_admitted_page_is_sent_the_cap_or_the_context_left_whichever_is_smaller():
    admitted = _admit()
    record = admitted["capacity"]
    assert record["fits"] and record["prompt_tokens_basis"] == PROMPT_TOKENS_CARRIED_BOUND
    assert record["answer_budget"] == 6903
    left = 32768 - record["image_prompt_tokens"] - record["prompt_tokens"]
    assert admitted["max_tokens"] == min(12288, left) == left
    assert admitted["answer_reserve"] == {**MEASURE, "tokens": 6903, "page_max_tokens": 12288}
    roomy = SimpleNamespace(**{**vars(ROW), "max_model_len": 65536})
    assert _admit(roomy)["max_tokens"] == 12288


def test_a_page_that_does_not_fit_is_refused_whole_with_its_record():
    with pytest.raises(RequestCapacityRefusal, match="held whole") as refusal:
        _admit(text="x" * 60_000)
    assert refusal.value.capacity["fits"] is False
    assert refusal.value.capacity["headroom"] < 0


def test_a_reserve_above_the_page_cap_is_refused_before_anything_is_sent():
    measure = {"longest_witness_characters": 30_000, "act_entries": 20}
    roomy = SimpleNamespace(**{**vars(ROW), "max_model_len": 65536})
    with pytest.raises(RequestCapacityRefusal, match="above the sealed page cap") as refusal:
        _admit(roomy, measure=measure)
    assert refusal.value.capacity["fits"] is True


def test_no_page_image_costs_no_image_tokens():
    assert _admit(images=())["capacity"]["image_prompt_tokens"] == 0


def test_the_overlay_is_charged_as_a_second_image_of_the_render_size():
    one = _admit(text="x")["capacity"]["image_prompt_tokens"]
    two = _admit(text="x", images=((1978, 2560),) * 2)["capacity"]["image_prompt_tokens"]
    assert two == 2 * one == 2 * 4960


def test_more_images_than_the_overhead_covers_are_refused():
    with pytest.raises(RequestCapacityRefusal, match="more than the 2"):
        _admit(images=((100, 100),) * 3)


def test_an_answer_measure_that_is_not_the_feeds_is_refused():
    with pytest.raises(RequestCapacityRefusal, match="answer measure"):
        _admit(measure={"longest_witness_characters": 1})


def test_digit_bearing_runs_are_charged_a_token_per_byte_and_prose_at_the_rate():
    from common.request_capacity import page_prompt_charge

    row = "L100 [100,50,900,120]\n"
    text = row * 500
    assert page_prompt_charge(text) == (500 * (len(row) - 2), 500 * 2)
    tokens, _basis = perlector_page_prompt_bound(
        text, template_digest=PERLECTOR_PAGE_PROMPT_TEMPLATE_DIGEST
    )
    # A byte-level tokenizer can split every digit apart; the bound never charges less.
    assert tokens >= PERLECTOR_PAGE_PROMPT_OVERHEAD_TOKENS + len(
        text.replace(" ", "").replace("\n", "")
    )
    rate_only, _ = perlector_page_prompt_bound(
        "x" * len(text), template_digest=PERLECTOR_PAGE_PROMPT_TEMPLATE_DIGEST
    )
    assert tokens > 2 * rate_only - PERLECTOR_PAGE_PROMPT_OVERHEAD_TOKENS


def test_a_digit_run_is_charged_its_utf8_bytes_not_its_characters():
    from common.request_capacity import page_prompt_charge

    assert page_prompt_charge("vingt-2ème baptême") == (len("vingt-2ème".encode()), len(" baptême"))
