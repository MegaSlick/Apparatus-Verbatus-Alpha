"""Doubt marks written back from a doubt report, and the offsets between the two forms."""

import random
from itertools import pairwise

import pytest

from common.contracts.errors import SchemaRefusal
from common.reading_annotations import (
    doubt_mark_offsets,
    malformed_assessment,
    read_doubt_marks,
    render_doubt_marks,
)

# Pieces that exercise every mark form, the delimiters alone and the characters a
# mark reserves, so random joins reach overlapping, adjacent and stray marks.
_PIECES = ["a", "b", "é", " ", ".", "[", "]", "|", "?", "[[", "]]", "[[?]]", "[[a]]", "[[ab|c]]"]


def _raws(count, seed):
    rng = random.Random(seed)
    return ["".join(rng.choice(_PIECES) for _ in range(rng.randint(0, 12))) for _ in range(count)]


_EXAMPLES = [
    "",
    "plain text",
    "[[?]] leading",
    "trailing [[?]]",
    "a [[?]][[?]] b",
    "a [[?]][[Jean|Jeanne]] b",
    "a [[Jean]][[?]] b",
    "stray ] and [ alone",
    "[[[b]]",
    "a[[b]]]",
    "unclosed [[ mark",
    "[[|x]] names no reading",
]


def _recorded_gaps_lost(raw, text, assessment):
    return assessment["state"] == "assessed" and not text.strip() and "[[?]]" in raw


@pytest.mark.parametrize("raw", _EXAMPLES + _raws(3000, seed=7))
def test_rendering_a_split_reading_gives_back_the_marked_reading(raw):
    text, assessment = read_doubt_marks(raw)
    if _recorded_gaps_lost(raw, text, assessment):
        # A gap in a reading with no text is not recorded, so nothing renders it.
        assert render_doubt_marks(text, assessment) == text
        return
    assert render_doubt_marks(text, assessment) == raw


@pytest.mark.parametrize("raw", _EXAMPLES + _raws(1000, seed=11))
def test_the_offset_maps_agree_with_each_other_and_with_the_marks(raw):
    text, assessment = read_doubt_marks(raw)
    raw_to_clean, clean_to_raw = doubt_mark_offsets(raw)
    assert len(raw_to_clean) == len(raw) + 1
    assert len(clean_to_raw) == len(text) + 1
    assert raw_to_clean[0] == 0 and raw_to_clean[-1] == len(text)
    mapped = [clean for clean in raw_to_clean if clean is not None]
    assert mapped == sorted(mapped)
    for clean, index in enumerate(clean_to_raw):
        if index is not None:
            assert raw_to_clean[index] == clean
    # Between two mapped raw offsets outside marks the characters are the same text.
    outside = [i for i, clean in enumerate(raw_to_clean) if clean is not None]
    for left, right in pairwise(outside):
        if right == left + 1 and raw_to_clean[right] == raw_to_clean[left] + 1:
            assert raw[left] == text[raw_to_clean[left]]


def test_a_gap_maps_to_one_zero_width_offset_and_its_inside_to_none():
    raw_to_clean, clean_to_raw = doubt_mark_offsets("ab[[?]]cd")
    assert raw_to_clean == [0, 1, 2, None, None, None, None, 2, 3, 4]
    assert clean_to_raw == [0, 1, 2, 8, 9]


def test_a_span_maps_its_ends_to_the_ends_of_its_reading():
    raw_to_clean, clean_to_raw = doubt_mark_offsets("x[[ab|c]]y")
    assert raw_to_clean == [0, 1, None, None, None, None, None, None, None, 3, 4]
    assert clean_to_raw == [0, 1, None, 9, 10]


def test_a_reading_whose_marks_do_not_parse_maps_as_the_identity():
    raw_to_clean, clean_to_raw = doubt_mark_offsets("a [[ b")
    assert raw_to_clean == clean_to_raw == list(range(7))


def test_a_report_that_is_not_assessed_renders_its_text_unchanged():
    not_assessed = {"state": "not-assessed", "uncertain_spans": [], "gaps": [], "problem": "x"}
    assert render_doubt_marks("a [[ b", malformed_assessment("x")) == "a [[ b"
    assert render_doubt_marks("text", not_assessed) == "text"


def test_overlapping_spans_are_refused():
    text, assessment = read_doubt_marks("[[abc]]")
    assessment["uncertain_spans"].append(
        {"start": 1, "end": 2, "alternatives": [], "confidence": "low"}
    )
    with pytest.raises(SchemaRefusal):
        render_doubt_marks(text, assessment)


def test_a_span_that_cannot_be_written_as_a_mark_is_refused():
    text = "a|b"
    assessment = {
        "state": "assessed",
        "uncertain_spans": [{"start": 0, "end": 3, "alternatives": [], "confidence": "low"}],
        "gaps": [],
        "problem": None,
    }
    with pytest.raises(SchemaRefusal):
        render_doubt_marks(text, assessment)
