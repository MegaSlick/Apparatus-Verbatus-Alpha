"""Whole-page Perlector request admission: the carried prompt bound and the answer reserve."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from common.request_capacity import (
    PAGE_ANSWER_ENTRY_SKELETON,
    PAGE_ANSWER_LINE_CITE,
    PAGE_ANSWER_WRAPPER,
    PERLECTOR_PAGE_PROMPT_OVERHEAD_TOKENS,
    PERLECTOR_PAGE_PROMPT_TEMPLATE_DIGEST,
    PROMPT_TOKENS_ADMITTING_BASES,
    PROMPT_TOKENS_REPORTED_BYTES_FIXED_CARRIED,
    RequestCapacityRefusal,
    page_answer_bound,
    page_prompt_charge,
    page_request_capacity,
    perlector_page_prompt_bound,
    reask_answer_measure,
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
MEASURE = {"longest_witness_characters": 12_000, "act_entries": 20, "surya_lines": 0}
GENERATION = {"page_max_tokens": 12288}


def _fixed(text):
    return [(text, False)]


def _bound(text, parts=None):
    return perlector_page_prompt_bound(
        text,
        template_digest=PERLECTOR_PAGE_PROMPT_TEMPLATE_DIGEST,
        parts=_fixed(text) if parts is None else parts,
    )


def _admit(
    row=ROW,
    *,
    text="x" * 40_000,
    parts=None,
    measure=MEASURE,
    generation=GENERATION,
    images=((1978, 2560),),
):
    return page_request_capacity(
        row,
        image_sizes=images,
        prompt_text=text,
        prompt_parts=_fixed(text) if parts is None else parts,
        template_digest=PERLECTOR_PAGE_PROMPT_TEMPLATE_DIGEST,
        answer_measure=measure,
        generation=generation,
    )


def test_the_fixed_page_prose_is_charged_at_the_carried_rate():
    tokens, basis = _bound("x" * 10_000)
    assert tokens - PERLECTOR_PAGE_PROMPT_OVERHEAD_TOKENS == 4334
    assert tokens == 56 + 4334
    assert basis == PROMPT_TOKENS_REPORTED_BYTES_FIXED_CARRIED
    assert PROMPT_TOKENS_REPORTED_BYTES_FIXED_CARRIED in PROMPT_TOKENS_ADMITTING_BASES


def test_a_reported_string_costs_a_token_per_byte_whatever_it_holds():
    prose = "le vingt deux mai a été baptisé " * 100
    fixed, _ = _bound(prose)
    reported, _ = _bound(prose, [(prose, True)])
    # Byte-level BPE: never more tokens than bytes, so bytes are the bound.
    assert reported == PERLECTOR_PAGE_PROMPT_OVERHEAD_TOKENS + len(prose.encode("utf-8"))
    assert reported > fixed
    # As sent or NFC-normalized, whichever has more bytes: NFC shortens a
    # decomposed accent but lengthens a composition exclusion.
    decomposed = "e\u0301" * 10
    assert page_prompt_charge([(decomposed, True)]) == (30, 0)
    excluded = "\u0958" * 10
    assert page_prompt_charge([(excluded, True)]) == (60, 0)


def test_parts_that_do_not_join_to_the_prompt_are_refused():
    with pytest.raises(RequestCapacityRefusal, match="do not join to the rendered prompt"):
        _bound('witness A (x) "text"', [("witness A (x) ", False)])
    with pytest.raises(RequestCapacityRefusal, match="not \\(text, reported\\)"):
        page_prompt_charge([("a", "yes")])


def test_an_edited_page_builder_expires_the_carried_rate():
    with pytest.raises(RequestCapacityRefusal, match="page template changed"):
        perlector_page_prompt_bound("x", template_digest="0" * 64, parts=_fixed("x"))


def test_the_answer_reserve_is_the_longest_witness_text_plus_each_entrys_scaffold():
    characters = 12_000 + 20 * len(PAGE_ANSWER_ENTRY_SKELETON) + len(PAGE_ANSWER_WRAPPER)
    expected = -(-characters * 4127 * 105 // (10_000 * 100))
    assert page_answer_bound(
        longest_witness_characters=12_000, act_entries=20, surya_lines=0, page_max_tokens=12288
    ) == (expected, False)
    assert expected == 6929


def test_each_shown_surya_line_is_reserved_one_cite_of_its_own():
    """Lines are cited one by one, never by a range, so each costs its own id."""
    characters = (
        12_000
        + 20 * len(PAGE_ANSWER_ENTRY_SKELETON)
        + 120 * len(PAGE_ANSWER_LINE_CITE)
        + len(PAGE_ANSWER_WRAPPER)
    )
    expected = -(-characters * 4127 * 105 // (10_000 * 100))
    assert page_answer_bound(
        longest_witness_characters=12_000, act_entries=20, surya_lines=120, page_max_tokens=12288
    ) == (expected, False)
    assert expected > 6929


def test_a_page_with_no_witness_text_reserves_the_whole_cap():
    for entries in (0, 20):
        assert page_answer_bound(
            longest_witness_characters=0,
            act_entries=entries,
            surya_lines=entries,
            page_max_tokens=12288,
        ) == (12288, False)


def test_an_estimate_above_the_cap_is_clamped_to_it_and_recorded():
    assert page_answer_bound(
        longest_witness_characters=300_000, act_entries=20, surya_lines=0, page_max_tokens=12288
    ) == (12288, True)
    roomy = SimpleNamespace(**{**vars(ROW), "max_model_len": 65536})
    # A looping witness never refuses the page for good: it is admitted on the cap.
    admitted = _admit(roomy, measure={**MEASURE, "longest_witness_characters": 300_000})
    assert admitted["answer_reserve"]["tokens"] == 12288
    assert admitted["answer_reserve"]["reserve_clamped"] is True
    assert admitted["capacity"]["answer_budget"] == 12288


def test_a_dense_page_is_sent_the_cap_or_the_context_left_whichever_is_smaller():
    admitted = _admit()
    record = admitted["capacity"]
    assert record["fits"]
    assert record["prompt_tokens_basis"] == PROMPT_TOKENS_REPORTED_BYTES_FIXED_CARRIED
    assert record["answer_budget"] == 6929
    left = 32768 - record["image_prompt_tokens"] - record["prompt_tokens"]
    assert admitted["max_tokens"] == min(12288, left) == left
    assert admitted["answer_reserve"] == {
        **MEASURE,
        "tokens": 6929,
        "reserve_clamped": False,
        "page_max_tokens": 12288,
    }
    roomy = SimpleNamespace(**{**vars(ROW), "max_model_len": 65536})
    assert _admit(roomy)["max_tokens"] == 12288


ROOMY = SimpleNamespace(**{**vars(ROW), "max_model_len": 65536})


@pytest.mark.parametrize(
    "measure",
    [
        # A page whose witnesses read little: the reserve is small, the bound is not.
        {"longest_witness_characters": 300, "act_entries": 1, "surya_lines": 0},
        # A dense index page: 264 Surya lines, a reserve far below what its answer needs.
        {"longest_witness_characters": 2_000, "act_entries": 3, "surya_lines": 264},
    ],
)
def test_the_answer_is_never_bounded_by_its_reserve(measure):
    for row in (ROW, ROOMY):
        admitted = _admit(row, measure=measure)
        record = admitted["capacity"]
        room = record["max_model_len"] - record["image_prompt_tokens"] - record["prompt_tokens"]
        assert admitted["answer_reserve"]["tokens"] < 4096
        assert admitted["max_tokens"] == min(GENERATION["page_max_tokens"], room)


def test_a_page_with_no_witness_text_is_sent_the_whole_cap():
    measure = {"longest_witness_characters": 0, "act_entries": 3, "surya_lines": 3}
    assert _admit(ROOMY, measure=measure)["max_tokens"] == 12288


def test_a_generation_bound_that_is_not_exactly_the_sealed_page_cap_is_refused():
    retired = {**GENERATION, "answer_headroom_bp": 20000, "answer_floor_tokens": 4096}
    for generation in ({}, {**GENERATION, "extra": 1}, retired):
        with pytest.raises(RequestCapacityRefusal, match="not exactly the sealed page cap"):
            _admit(ROOMY, generation=generation)


def test_a_page_that_does_not_fit_is_refused_whole_with_its_record():
    with pytest.raises(RequestCapacityRefusal, match="held whole") as refusal:
        _admit(text="x" * 60_000)
    assert refusal.value.capacity["fits"] is False
    assert refusal.value.capacity["headroom"] < 0


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


def test_digit_bearing_runs_of_fixed_text_are_charged_a_token_per_byte_and_prose_at_the_rate():
    row = "L100 [100,50,900,120]\n"
    text = row * 500
    assert page_prompt_charge(_fixed(text)) == (500 * (len(row) - 2), 500 * 2)
    tokens, _basis = _bound(text)
    # A byte-level tokenizer can split every digit apart; the bound never charges less.
    assert tokens >= PERLECTOR_PAGE_PROMPT_OVERHEAD_TOKENS + len(
        text.replace(" ", "").replace("\n", "")
    )
    rate_only, _ = _bound("x" * len(text))
    assert tokens > 2 * rate_only - PERLECTOR_PAGE_PROMPT_OVERHEAD_TOKENS


def test_a_digit_run_is_charged_its_utf8_bytes_not_its_characters():
    assert page_prompt_charge(_fixed("vingt-2ème baptême")) == (
        len("vingt-2ème".encode()),
        len(" baptême"),
    )


def test_a_re_ask_reserves_its_answer_on_its_named_units_text_or_the_whole_cap_with_none():
    # The most text one witness gave for the named units, one entry per named id at most.
    named = reask_answer_measure(
        [("A", "x" * 300), ("B", "y" * 200), ("A", "z" * 100)], 3, named_lines=0
    )
    assert named == {"longest_witness_characters": 400, "act_entries": 3, "surya_lines": 0}
    # Only lines or records named: nothing measures the ink, so the whole cap is reserved.
    lines_only = reask_answer_measure([], 3, named_lines=3)
    assert lines_only == {"longest_witness_characters": 0, "act_entries": 3, "surya_lines": 3}
    # A row with room for the named units' reserve but not the whole cap: the first
    # re-ask is admitted on its own reserve, the second refused whole, never trimmed.
    row = SimpleNamespace(**{**vars(ROW), "max_model_len": 8192})
    admitted = _admit(row, text="x" * 4000, measure=named)
    assert admitted["capacity"]["fits"] is True
    assert admitted["answer_reserve"]["tokens"] == admitted["capacity"]["answer_budget"] == 465
    with pytest.raises(RequestCapacityRefusal, match="held whole") as refusal:
        _admit(row, text="x" * 4000, measure=lines_only)
    assert refusal.value.capacity["fits"] is False
    assert refusal.value.capacity["answer_budget"] == 12288
    with pytest.raises(RequestCapacityRefusal, match="named_ids"):
        reask_answer_measure([], -1, named_lines=0)
    with pytest.raises(RequestCapacityRefusal, match="named_lines"):
        reask_answer_measure([], 1, named_lines=-1)
