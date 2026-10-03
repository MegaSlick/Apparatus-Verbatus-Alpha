"""Doubt marks read into a doubt report, written back from one, and the offsets between."""

import random
from itertools import pairwise

import pytest

from common.contracts.errors import SchemaRefusal
from common.contracts.uncertainty import PAGE_READ_LECTIO, corrected_layer, validate
from common.reading_annotations import (
    bracket_doubt_marks,
    diplomatic_display,
    doubt_mark_offsets,
    malformed_assessment,
    read_doubt_marks,
    render_doubt_marks,
)


def _stored_layer(assessment):
    """The doubt layer a Perlectio stores for this report, as the canonical schema reads it."""
    return {
        "uncertain_spans": assessment["uncertain_spans"],
        "gaps": assessment["gaps"],
        "self_revisions": None,
        "assessment": {"state": assessment["state"], "problem": assessment["problem"]},
        "lectio_kind": PAGE_READ_LECTIO,
    }


@pytest.mark.parametrize(
    "raw,text,state,spans,gaps",
    [
        ("plain ink", "plain ink", "assessed", 0, 0),
        ("[[?]] a [[b]] [[?]]", " a b ", "assessed", 1, 2),
        ("[[?]]", "", "assessed", 0, 0),
        ("[[?]]\n[[?]]", "\n", "assessed", 0, 0),
        ("a [[?|b]]", "a [[?|b]]", "malformed", 0, 0),
        ("a [[b|]]", "a [[b|]]", "malformed", 0, 0),
        ("a [[b", "a [[b", "malformed", 0, 0),
        ("a ]] b", "a ]] b", "malformed", 0, 0),
        ("a [[x [[y]] z]]", "a [[x [[y]] z]]", "malformed", 0, 0),
        ("a [[|y]]", "a [[|y]]", "malformed", 0, 0),
    ],
)
def test_doubt_marks_parse_or_leave_the_answer_as_returned(raw, text, state, spans, gaps):
    published, report = read_doubt_marks(raw)
    assert (published, report["state"]) == (text, state)
    assert (len(report["uncertain_spans"]), len(report["gaps"])) == (spans, gaps)
    validate(_stored_layer(report), published)


@pytest.mark.parametrize(
    "raw",
    [
        "le vingt deux du mois [[?]]",
        "le vingt deux du mois [[?]]\n",
        "le vingt deux du mois [[?]].",
        "le vingt deux du mois [[?]] ;»\n",
    ],
)
def test_ink_past_the_crop_marked_at_the_end_is_a_trailing_gap(raw):
    """A zero-width gap at the end, carrying no text, even when the reader closes the
    line or the sentence after the mark."""
    text, assessment = read_doubt_marks(raw)
    gaps = [{"position": "trailing", "start": 22, "end": 22, "witness_evidence": []}]
    assert assessment["gaps"] == gaps
    validate(_stored_layer(assessment), text)


def test_a_mark_followed_by_more_words_stays_internal():
    _text, assessment = read_doubt_marks("le vingt [[?]] du mois")
    assert assessment["gaps"][0]["position"] == "internal"


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
    validate(_stored_layer(assessment), text)
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


@pytest.mark.parametrize(
    "report",
    [
        {"state": "assessed", "uncertain_spans": [], "gaps": []},
        {"state": "assessed", "uncertain_spans": [{"start": 0}], "gaps": [], "problem": None},
        {"state": "assessed", "uncertain_spans": [], "gaps": [], "problem": "why"},
    ],
)
def test_a_malformed_doubt_report_is_refused_by_name(report):
    with pytest.raises(SchemaRefusal):
        render_doubt_marks("text", report)


@pytest.mark.parametrize(
    "raw, shown",
    [
        ("Jean [[?]] [[Martin|Morin]], fils", "Jean [illegible] [Martin?], fils"),
        ("[[?]][[Pierre]] Roy", "[illegible][Pierre?] Roy"),
        ("no doubt here", "no doubt here"),
    ],
)
def test_the_diplomatic_text_brackets_only_where_the_ink_is_doubtful(raw, shown):
    text, assessment = read_doubt_marks(raw)
    assert diplomatic_display(text, _stored_layer(assessment)) == shown
    assert bracket_doubt_marks(raw) == shown


def test_a_reading_with_no_machine_doubt_report_is_shown_as_it_is():
    assert diplomatic_display("Jean [Martin]", corrected_layer()) == "Jean [Martin]"
    whole = _stored_layer(read_doubt_marks("x")[1]) | {
        "gaps": [{"position": "whole-act", "start": 0, "end": 0, "witness_evidence": []}]
    }
    assert diplomatic_display("", whole) == "[illegible]"
