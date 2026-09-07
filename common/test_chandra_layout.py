"""What the Chandra carry claims, and what its reader actually does.

Every page in this file is hand-written. Not one is copied from the vendor's
examples, README or test fixtures: a test page that came from the vendor would
be a second, unrecorded carry of vendor bytes, and it would also prove less --
the interesting pages are the malformed ones the vendor never publishes. (One
malformed `data-bbox` below is spelled "defaulting to full image" on purpose:
it is the vendor's own console message for that case, standing in the field
where its substitute would have come from.)

Two halves. The first pins the carry: the prompt renders to the digest recorded
against `datalab-to/chandra @ d4f7467…`, it still states the scale the geometry
divides by, and the 36 tags, 14 attributes and 19 labels are all there and all
in the prompt. Each of those checks is then made to fail on purpose, because a
seal nobody has watched refuse anything is a comment.

The second half pins the reader, and above all the four departures from
`chandra/output.py::parse_layout`. Each is asserted as a *fact about the
output*, not as a call that returned without raising: the vendor's
`[0, 0, 1, 1]` substitute appears nowhere; the `Blank-Page` block is present
and carries no text and no geometry; the nested `data-bbox` is still in the
retained content; and a block the reader loses to unbalanced markup is named by
the reconciliation rather than silently absent.
"""

from __future__ import annotations

import hashlib
from typing import Any

import pytest

from common import chandra_layout
from common.chandra_layout import (
    ALLOWED_ATTRIBUTES,
    ALLOWED_TAGS,
    BBOX_SCALE,
    BLANK_PAGE_LABEL,
    LAYOUT_FINDING_KINDS,
    LAYOUT_TEXT_VIEW,
    MAX_LAYOUT_BLOCKS,
    MAX_QUOTED_ATTRIBUTE_CHARACTERS,
    MAX_RESPONSE_BYTES,
    OCR_LAYOUT_LABELS,
    OCR_LAYOUT_PROMPT,
    OCR_LAYOUT_PROMPT_SHA256,
    PARSE_OUTCOMES,
    PROMPT_ENDING,
    PROMPT_ENDING_SHA256,
    UNLABELLED_BLOCK_LABEL,
    block_page_bounds,
    is_refusal,
    layout_block_text,
    parse_bbox_attribute,
    parse_layout_html,
    vendor_scaled_bbox,
)
from common.structure_answer import to_page_bounds


def _sha256(text: str) -> str:
    """Recomputed here rather than imported: a digest helper shared with the
    thing it checks would pass a mangled encoding through both sides."""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _read(html: str) -> dict[str, Any]:
    parsed = parse_layout_html(html.encode("utf-8"))
    assert not is_refusal(parsed), parsed
    return parsed


def _kinds(parsed: dict[str, Any]) -> list[str]:
    return [finding["kind"] for finding in parsed["findings"]]


def _finding(parsed: dict[str, Any], kind: str) -> dict[str, Any]:
    matches = [finding for finding in parsed["findings"] if finding["kind"] == kind]
    assert len(matches) == 1, f"expected exactly one {kind}, got {parsed['findings']}"
    return matches[0]


# ---------------------------------------------------------------------------
# The carry
# ---------------------------------------------------------------------------


def test_carried_prompt_renders_to_the_digest_recorded_against_the_vendor_commit():
    assert _sha256(OCR_LAYOUT_PROMPT) == OCR_LAYOUT_PROMPT_SHA256
    assert _sha256(PROMPT_ENDING) == PROMPT_ENDING_SHA256


def test_the_layout_prompt_ends_with_the_shared_prompt_ending():
    # The vendor builds `OCR_LAYOUT_PROMPT` by interpolating `PROMPT_ENDING`
    # last and stripping. Two independently carried strings that had drifted
    # apart would still each match their own digest.
    assert OCR_LAYOUT_PROMPT.endswith(PROMPT_ENDING)
    assert OCR_LAYOUT_PROMPT != PROMPT_ENDING


def test_the_tag_and_attribute_order_is_what_reaches_the_model():
    # Both lists enter the prompt as Python `repr`, so re-sorting either one
    # silently changes the bytes a chair is sent. This is what makes that
    # visible rather than a comment asking for care.
    assert repr(ALLOWED_TAGS) in PROMPT_ENDING
    assert repr(ALLOWED_ATTRIBUTES) in PROMPT_ENDING
    assert len(ALLOWED_TAGS) == 36
    assert len(ALLOWED_ATTRIBUTES) == 14
    assert len(set(ALLOWED_TAGS)) == len(ALLOWED_TAGS)
    assert len(set(ALLOWED_ATTRIBUTES)) == len(ALLOWED_ATTRIBUTES)
    assert "data-bbox" in ALLOWED_ATTRIBUTES
    assert "data-label" in ALLOWED_ATTRIBUTES


def test_every_label_this_module_names_is_a_label_the_prompt_offers():
    assert len(OCR_LAYOUT_LABELS) == 19
    assert len(set(OCR_LAYOUT_LABELS)) == 19
    for label in OCR_LAYOUT_LABELS:
        assert f"\n- {label}\n" in OCR_LAYOUT_PROMPT
    # And the reverse: the prompt offers no label this module fails to name.
    assert OCR_LAYOUT_PROMPT.count("\n- ") == 19
    assert BLANK_PAGE_LABEL in OCR_LAYOUT_LABELS
    assert UNLABELLED_BLOCK_LABEL not in OCR_LAYOUT_LABELS


def test_the_prompt_still_states_the_scale_the_geometry_divides_by():
    assert f"Bboxes are normalized 0-{BBOX_SCALE}." in OCR_LAYOUT_PROMPT
    assert BBOX_SCALE == 1000


@pytest.mark.parametrize(
    ("attribute", "value", "expected"),
    [
        ("OCR_LAYOUT_PROMPT_SHA256", "0" * 64, "renders to sha256"),
        ("OCR_LAYOUT_PROMPT", OCR_LAYOUT_PROMPT.replace("0-1000", "0-100"), "0-1000"),
        ("ALLOWED_TAGS", ALLOWED_TAGS[:-1], "not the vendor's 36"),
        ("ALLOWED_ATTRIBUTES", ALLOWED_ATTRIBUTES[:-1], "not the vendor's 14"),
        ("OCR_LAYOUT_LABELS", OCR_LAYOUT_LABELS[:-1], "not 19"),
        ("OCR_LAYOUT_LABELS", ("Caption",) * 19, "repeats a label"),
        (
            "OCR_LAYOUT_LABELS",
            OCR_LAYOUT_LABELS[:-1] + ("Marginal-Name",),
            "is not offered by the carried prompt",
        ),
    ],
)
def test_the_import_seal_refuses_a_carry_that_drifted(monkeypatch, attribute, value, expected):
    """The seal has teeth, and this is where they are shown.

    `_seal` runs once at import, where a passing run proves nothing about what
    it would refuse. Each case here edits exactly one carried value the way a
    careless hand would -- a digest left behind after a re-pin, a scale changed
    in the prompt but not in `BBOX_SCALE`, a list one element short, a label
    the prompt never offered -- and asserts the module refuses to be that.
    """
    monkeypatch.setattr(chandra_layout, attribute, value)
    with pytest.raises(RuntimeError, match=expected):
        chandra_layout._seal()


def test_the_seal_passes_on_the_carry_as_it_stands():
    # The counterpart to the case above: with nothing patched, `_seal` returns.
    assert chandra_layout._seal() is None


def test_undeclared_outcomes_and_findings_cannot_be_minted():
    with pytest.raises(ValueError, match="undeclared parse outcome"):
        chandra_layout._refuse("something-plausible")
    with pytest.raises(ValueError, match="undeclared layout finding kind"):
        chandra_layout._finding("something-plausible")


# ---------------------------------------------------------------------------
# `data-bbox`
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("100 77 900 385", [100, 77, 900, 385]),
        ("0 0 1000 1000", [0, 0, 1000, 1000]),
        ("+1 +2 +3 +4", [1, 2, 3, 4]),
    ],
)
def test_a_well_formed_bbox_is_four_normalized_integers(raw, expected):
    box, reason = parse_bbox_attribute(raw)
    assert box == expected
    assert reason is None


@pytest.mark.parametrize(
    ("raw", "expected_reason"),
    [
        (None, "no data-bbox"),
        ("", "expected 4 space-separated components"),
        ("1 2 3", "expected 4 space-separated components"),
        ("1 2 3 4 5", "expected 4 space-separated components"),
        ("1  2 3 4", "expected 4 space-separated components"),
        ("full image", "expected 4 space-separated components"),
        ("1 2 3 four", "not plain decimal integers"),
        ("1.0 2 3 4", "not plain decimal integers"),
        # `int("1_0")` is 10 in Python. A model never asked for that shape, and
        # reading it as ten would be the substituted value this reader exists
        # not to publish.
        ("1_0 2 3 4", "not plain decimal integers"),
        ("-1 2 3 4", "outside [0, 1000]"),
        ("1 2 1001 4", "outside [0, 1000]"),
        ("30 2 30 4", "x1 <= x0 or y1 <= y0"),
        ("1 40 3 40", "x1 <= x0 or y1 <= y0"),
        ("1 40 3 20", "x1 <= x0 or y1 <= y0"),
    ],
)
def test_a_malformed_bbox_is_refused_by_name_and_never_defaulted(raw, expected_reason):
    box, reason = parse_bbox_attribute(raw)
    assert box is None
    assert expected_reason in reason


_PAGE_SIZES = [(200, 260), (2080, 2976), (1, 1), (2550, 3300), (997, 1013)]
_BOXES = [
    [0, 0, 1000, 1000],
    [0, 0, 1, 1],
    [1, 1, 2, 2],
    [100, 77, 900, 385],
    [999, 999, 1000, 1000],
    [333, 111, 334, 112],
    [500, 500, 1000, 1000],
]


@pytest.mark.parametrize("page_size", _PAGE_SIZES)
@pytest.mark.parametrize("box", _BOXES)
def test_our_rectangle_contains_the_box_the_model_actually_reported(box, page_size):
    """Nothing the model placed is cropped away by the conversion.

    The near edges are floored and the far edges ceiled, in exact integer
    arithmetic, so the real-valued rectangle `[x0*W/1000, x1*W/1000]` always
    lies inside ours. If that ever stopped holding, Chandra's blocks would be
    cropped tighter than the model reported them -- ink lost behind a clean
    status, which GOALS 1 rates worst.
    """
    page_w, page_h = page_size
    ours = block_page_bounds(_block(bbox_1000=box), page_size=page_size)
    assert ours is not None
    assert ours["x"] == box[0] * page_w // BBOX_SCALE
    assert ours["y"] == box[1] * page_h // BBOX_SCALE
    assert ours["x"] + ours["w"] == min(page_w, -(-box[2] * page_w // BBOX_SCALE))
    assert ours["y"] + ours["h"] == min(page_h, -(-box[3] * page_h // BBOX_SCALE))
    assert ours["w"] >= 1
    assert ours["h"] >= 1
    assert ours["x"] + ours["w"] <= page_w
    assert ours["y"] + ours["h"] <= page_h


@pytest.mark.parametrize("page_size", _PAGE_SIZES)
@pytest.mark.parametrize("box", _BOXES)
def test_against_the_vendors_own_arithmetic_only_its_float_division_separates_us(box, page_size):
    """What the difference from `parse_layout`'s own numbers actually is.

    The vendor divides by 1000 in floating point before multiplying, so at a
    coordinate whose true product is a whole number its result falls a pixel
    short. Its far edges are then inside ours, which is the direction that
    matters; its near edges are at ours or one pixel outside, never further.
    Stated as two bounded facts rather than one containment claim, because the
    containment claim is not true and pretending it is would hide a real
    property of the vendor's arithmetic.
    """
    ours = block_page_bounds(_block(bbox_1000=box), page_size=page_size)
    assert ours is not None
    vx0, vy0, vx1, vy1 = vendor_scaled_bbox(box, page_size=page_size)
    assert 0 <= ours["x"] - vx0 <= 1
    assert 0 <= ours["y"] - vy0 <= 1
    assert ours["x"] + ours["w"] >= vx1
    assert ours["y"] + ours["h"] >= vy1


def test_the_vendors_float_division_really_does_lose_a_pixel():
    """The concrete case the bound above exists for.

    A guard whose slack is never taken up is a guard nobody has watched work.
    `100/1000 * 2550` is exactly 255; the vendor's `int(100 * (2550/1000))` is
    254.
    """
    assert vendor_scaled_bbox([100, 77, 900, 385], page_size=(2550, 3300))[0] == 254
    assert 100 * 2550 // BBOX_SCALE == 255
    ours = block_page_bounds(_block(bbox_1000=[100, 77, 900, 385]), page_size=(2550, 3300))
    assert ours is not None
    assert ours["x"] == 255


def _block(**overrides: Any) -> dict[str, Any]:
    block = {
        "ordinal": 0,
        "label": "Text",
        "label_declared": True,
        "blank_page": False,
        "bbox_1000": [100, 77, 900, 385],
        "content": "",
        "text": "",
        "nested_bboxes": [],
    }
    block.update(overrides)
    return block


def test_the_conversion_this_reader_guards_really_does_return_nonsense_for_a_bad_box():
    """Why `parse_bbox_attribute` refuses boxes the conversion would accept.

    `to_page_bounds` is a conversion, not a validator. These three calls are
    what a degenerate, inverted or negative box actually produces if it is
    allowed through -- a zero-width rectangle, a negative-width one, and one
    that starts off the page. Measured here rather than asserted in a comment,
    so removing the ordering check has a visible consequence in this file
    instead of a quiet one downstream.
    """
    assert to_page_bounds([500, 100, 500, 200], 200, 260)["w"] == 0
    assert to_page_bounds([600, 100, 500, 200], 200, 260)["w"] == -20
    assert to_page_bounds([-50, 100, 500, 200], 200, 260)["x"] == -10
    # The upper-range check is not defending against this: a far edge past the
    # scale is clamped to the page. It refuses because a component above the
    # scale means the model ignored the denominator the prompt gave it.
    assert to_page_bounds([0, 0, 5000, 5000], 200, 260) == {"x": 0, "y": 0, "w": 200, "h": 260}


def test_a_block_with_no_resolvable_geometry_yields_no_rectangle():
    assert block_page_bounds(_block(bbox_1000=None), page_size=(200, 260)) is None
    assert block_page_bounds(_block(blank_page=True), page_size=(200, 260)) is None


# ---------------------------------------------------------------------------
# The four departures from `chandra/output.py::parse_layout`
# ---------------------------------------------------------------------------

_MALFORMED_PAGE = (
    '<div data-bbox="100 77 900 385" data-label="Text"><p>Ce jourdhuy</p></div>'
    '<div data-bbox="defaulting to full image" data-label="Caption"><p>Pierre Roy</p></div>'
)


def test_a_malformed_bbox_is_named_and_the_vendors_substitute_never_appears():
    parsed = _read(_MALFORMED_PAGE)
    assert [block["bbox_1000"] for block in parsed["blocks"]] == [[100, 77, 900, 385], None]
    assert _finding(parsed, "malformed-bbox") == {
        "kind": "malformed-bbox",
        "ordinal": 1,
        "reason": "components are not plain decimal integers",
        "data_bbox": "defaulting to full image",
        "data_bbox_truncated": False,
    }
    # The vendor prints a message and substitutes `[0, 0, 1, 1]`. Neither the
    # box nor a rectangle derived from it exists anywhere in this reading.
    assert [0, 0, 1, 1] not in [block["bbox_1000"] for block in parsed["blocks"]]
    assert block_page_bounds(parsed["blocks"][1], page_size=(200, 260)) is None
    # The block itself survives, text and all: geometry is what was lost, not
    # the reading.
    assert parsed["blocks"][1]["text"] == "Pierre Roy"
    assert "Pierre Roy" in parsed["page_text"]


def test_a_valueless_bbox_attribute_is_not_the_same_fact_as_no_bbox_attribute():
    """Two different answers must not collapse into one record.

    `html.parser` reports `<div data-bbox>` as `None`, exactly as it reports an
    absent attribute. BeautifulSoup hands the vendor `""`, so `""` is both the
    faithful value and the one that keeps the two apart.
    """
    valueless = _read('<div data-bbox data-label="Text">Roy</div>')
    absent = _read('<div data-label="Text">Roy</div>')
    assert _finding(valueless, "malformed-bbox")["data_bbox"] == ""
    assert _finding(absent, "malformed-bbox")["data_bbox"] is None
    assert "no data-bbox" in _finding(absent, "malformed-bbox")["reason"]
    assert "no data-bbox" not in _finding(valueless, "malformed-bbox")["reason"]


def test_a_repeated_attribute_takes_the_last_value_as_the_vendors_parser_does():
    parsed = _read('<div data-bbox="1 2 3 4" data-bbox="10 20 30 40" data-label="Text">x</div>')
    assert parsed["blocks"][0]["bbox_1000"] == [10, 20, 30, 40]


def test_a_finding_quotes_a_bounded_amount_of_what_the_model_wrote():
    """The record cannot be made large by one attribute.

    The response bytes are retained whole and are where the whole story is;
    what a finding carries is a look at what the model wrote, under a stated
    bound that says when it was cut.
    """
    parsed = _read(f'<div data-bbox="{"z" * 5000}" data-label="Text">Roy</div>')
    finding = _finding(parsed, "malformed-bbox")
    assert finding["data_bbox"] == "z" * 120
    assert finding["data_bbox_truncated"] is True
    assert len(finding["data_bbox"]) == MAX_QUOTED_ATTRIBUTE_CHARACTERS
    # And a short one is quoted whole, unmarked.
    short = _finding(_read('<div data-bbox="zz" data-label="Text">Roy</div>'), "malformed-bbox")
    assert short["data_bbox"] == "zz"
    assert short["data_bbox_truncated"] is False


def test_a_blank_page_block_is_retained_without_text_or_geometry():
    parsed = _read(
        '<div data-bbox="0 0 1000 1000" data-label="Blank-Page">nothing written here</div>'
    )
    (block,) = parsed["blocks"]
    assert block["label"] == BLANK_PAGE_LABEL
    assert block["blank_page"] is True
    assert block["text"] == ""
    assert parsed["page_text"] == ""
    assert parsed["spans"] == [{"start": 0, "end": 0}]
    assert block_page_bounds(block, page_size=(200, 260)) is None
    # Its declared box is still recorded rather than thrown away, and the
    # retention is itself a finding, because the vendor drops the block whole.
    assert block["bbox_1000"] == [0, 0, 1000, 1000]
    assert block["content"] == "nothing written here"
    assert _finding(parsed, "blank-page-retained")["ordinal"] == 0


def test_a_blank_page_between_two_blocks_does_not_disturb_their_spans():
    parsed = _read(
        '<div data-bbox="0 0 100 100" data-label="Text">first</div>'
        '<div data-bbox="0 0 100 100" data-label="Blank-Page"></div>'
        '<div data-bbox="0 100 100 200" data-label="Text">second</div>'
    )
    assert parsed["page_text"] == "first\nsecond"
    starts_and_ends = [(span["start"], span["end"]) for span in parsed["spans"]]
    assert starts_and_ends == [(0, 5), (5, 5), (6, 12)]
    for block, span in zip(parsed["blocks"], parsed["spans"], strict=True):
        assert parsed["page_text"][span["start"] : span["end"]] == block["text"]


def test_nested_data_bbox_is_recorded_and_left_in_the_retained_content():
    parsed = _read(
        '<div data-bbox="0 0 500 500" data-label="Table">'
        '<table><tr><td data-bbox="10 10 20 20">Jean</td>'
        '<td data-bbox="20 10 30 20">Roy</td></tr></table></div>'
    )
    (block,) = parsed["blocks"]
    assert block["nested_bboxes"] == ["10 10 20 20", "20 10 30 20"]
    # The vendor deletes these attributes from the content it returns.
    assert 'data-bbox="10 10 20 20"' in block["content"]
    assert 'data-bbox="20 10 30 20"' in block["content"]
    finding = _finding(parsed, "nested-bbox-retained")
    assert finding == {"kind": "nested-bbox-retained", "blocks": 1, "attributes": 2}
    # Recorded, and used for nothing: no page geometry is derived from them.
    assert block["bbox_1000"] == [0, 0, 500, 500]


def test_a_block_lost_to_unbalanced_markup_is_named_by_the_reconciliation():
    """The reconciliation earns its place here or nowhere.

    A `<div>` inside an unclosed element is not a top-level block -- for
    BeautifulSoup's `recursive=False` either -- so the reader is right to
    return one block. The count that matters is that it says so: the raw
    answer holds two divs at the start of a line and one of them did not become
    a block.
    """
    parsed = _read(
        '<div data-bbox="0 0 100 100" data-label="Text">kept</div>'
        '<p><div data-bbox="0 100 100 200" data-label="Text">swallowed</div></p>'
    )
    assert len(parsed["blocks"]) == 1
    assert parsed["blocks"][0]["text"] == "kept"
    assert _finding(parsed, "block-count-mismatch") == {
        "kind": "block-count-mismatch",
        "parsed_blocks": 1,
        "top_level_divs": 2,
    }


def test_a_clean_page_reconciles_and_reports_nothing():
    parsed = _read(
        '<div data-bbox="0 0 100 100" data-label="Text">one</div>'
        '<div data-bbox="0 100 100 200" data-label="Text">two</div>'
    )
    assert parsed["findings"] == []
    assert parsed["page_text"] == "one\ntwo"


# ---------------------------------------------------------------------------
# Reading the answer
# ---------------------------------------------------------------------------


def test_blocks_are_returned_in_document_order_with_their_labels():
    parsed = _read(
        '<div data-bbox="0 0 100 50" data-label="Page-Header">Registre</div>'
        '<div data-bbox="0 50 100 150" data-label="Text">Le vingt</div>'
        '<div data-bbox="0 150 100 200">unlabelled</div>'
    )
    assert [block["ordinal"] for block in parsed["blocks"]] == [0, 1, 2]
    assert [block["label"] for block in parsed["blocks"]] == [
        "Page-Header",
        "Text",
        UNLABELLED_BLOCK_LABEL,
    ]
    assert [block["label_declared"] for block in parsed["blocks"]] == [True, True, False]
    assert parsed["text_view"] == LAYOUT_TEXT_VIEW


def test_an_empty_data_label_takes_the_vendors_own_default():
    # `chandra/output.py`: `if not label: label = "block"`. An empty string is
    # falsy there too, so this is the vendor's behaviour, not a convenience.
    parsed = _read('<div data-bbox="0 0 100 50" data-label="">x</div>')
    assert parsed["blocks"][0]["label"] == UNLABELLED_BLOCK_LABEL
    assert parsed["blocks"][0]["label_declared"] is False


def test_content_is_the_answers_own_characters_not_a_reserialization():
    parsed = _read(
        "<div data-bbox=\"0 0 100 50\" data-label='Text'><span class='marge' >Roy</span></div>"
    )
    assert parsed["blocks"][0]["content"] == "<span class='marge' >Roy</span>"


def test_a_top_level_div_that_closes_itself_is_an_empty_block():
    parsed = _read('<div data-bbox="0 0 1000 1000" data-label="Blank-Page"/>')
    (block,) = parsed["blocks"]
    assert block["content"] == ""
    assert block["blank_page"] is True
    assert block["bbox_1000"] == [0, 0, 1000, 1000]


def test_an_answer_cut_off_mid_block_keeps_the_bytes_it_did_send():
    parsed = _read('<div data-bbox="0 0 100 50" data-label="Text"><p>Le vingt-huit')
    (block,) = parsed["blocks"]
    assert block["content"] == "<p>Le vingt-huit"
    assert block["text"] == "Le vingt-huit"
    assert _finding(parsed, "unclosed-block")["ordinal"] == 0


def test_a_stray_end_tag_invents_no_element():
    parsed = _read('</span><div data-bbox="0 0 100 50" data-label="Text">Roy</div></p>')
    assert len(parsed["blocks"]) == 1
    assert parsed["blocks"][0]["text"] == "Roy"


def test_a_div_nested_in_a_block_is_not_a_second_block():
    parsed = _read(
        '<div data-bbox="0 0 100 50" data-label="Complex-Block">'
        '<div data-bbox="10 10 20 20">inner</div>outer</div>'
    )
    (block,) = parsed["blocks"]
    assert block["bbox_1000"] == [0, 0, 100, 50]
    assert block["nested_bboxes"] == ["10 10 20 20"]
    assert "block-count-mismatch" not in _kinds(parsed)


@pytest.mark.parametrize(
    ("raw", "outcome"),
    [
        ("not bytes", "raw-response-not-bytes"),
        (None, "raw-response-not-bytes"),
        (b"", "no-layout-blocks"),
        (b"Le vingt-huit octobre mil sept cent.", "no-layout-blocks"),
        (b"<p>Le vingt-huit octobre.</p>", "no-layout-blocks"),
        # A wrapper the vendor's `recursive=False` would also find nothing in,
        # named apart from prose so a first real reading says which happened.
        (b"<html><body><div data-bbox='0 0 1 2'>x</div></body></html>", "blocks-not-at-top-level"),
        (b"<p><div data-bbox='0 0 1 2'>x</div></p>", "blocks-not-at-top-level"),
        (b'<div data-bbox="0 0 1 2">\xff\xfe</div>', "invalid-utf8"),
    ],
)
def test_bytes_this_reader_cannot_read_are_refused_by_name(raw, outcome):
    parsed = parse_layout_html(raw)
    assert is_refusal(parsed)
    assert parsed["parse_outcome"] == outcome
    assert parsed["parse_outcome"] in PARSE_OUTCOMES


def test_an_oversized_response_is_refused_without_being_parsed():
    parsed = parse_layout_html(b"<div data-bbox='0 0 1 1'>x</div>" + b" " * MAX_RESPONSE_BYTES)
    assert parsed == {"parse_outcome": "response-too-large"}


def test_more_blocks_than_the_ceiling_admits_are_refused_whole():
    one = '<div data-bbox="0 0 1 1" data-label="Text">x</div>'
    assert parse_layout_html((one * MAX_LAYOUT_BLOCKS).encode()) != {
        "parse_outcome": "too-many-layout-blocks"
    }
    parsed = parse_layout_html((one * (MAX_LAYOUT_BLOCKS + 1)).encode())
    assert parsed == {"parse_outcome": "too-many-layout-blocks"}


def test_every_finding_a_page_can_produce_is_a_declared_kind():
    parsed = _read(
        '<div data-bbox="nope" data-label="Blank-Page"><b data-bbox="1 1 2 2">x</b></div>'
        '<p><div data-bbox="0 0 1 1">swallowed</div></p>'
        '<div data-bbox="0 0 1 1" data-label="Text">open'
    )
    assert set(_kinds(parsed)) == {
        "malformed-bbox",
        "blank-page-retained",
        "nested-bbox-retained",
        "block-count-mismatch",
        "unclosed-block",
    }
    assert set(_kinds(parsed)) <= LAYOUT_FINDING_KINDS


# ---------------------------------------------------------------------------
# The text view `chandra-layout-text.v1`
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("content", "expected"),
    [
        ("<p>Le vingt</p>", "Le vingt"),
        ("plain", "plain"),
        ("<p>a<br>b</p>", "a\nb"),
        ("<p>a<br/>b</p>", "a\nb"),
        ("<p>a</p><p>b</p>", "a\nb"),
        ("<ul><li>a</li><li>b</li></ul>", "a\nb"),
        ("<h2>Titre</h2><p>corps</p>", "Titre\ncorps"),
        # Inline markup is transparent: its content is the reading.
        ("<p>Jean<sup>e</sup> Roy</p>", "Jeane Roy"),
        ("<p><b>Marie</b> <i>Anne</i> <u>Roy</u></p>", "Marie Anne Roy"),
        ("<p><del>Pierre</del></p>", "Pierre"),
        ("<p>x <span>y</span> z</p>", "x y z"),
        ("<p><a href='#'>lien</a></p>", "lien"),
        ("<p><math>x^2</math></p>", "x^2"),
        # Source indentation and line breaks are markup formatting, not ink.
        ("<p>\n    Le    vingt\n    huit\n</p>", "Le vingt huit"),
        ("<p>a</p>\n\n\n<p>b</p>", "a\nb"),
        # Character references are resolved by the parser, once.
        ("<p>Roy &amp; fils</p>", "Roy & fils"),
        ("<p>d&#233;c&egrave;s</p>", "décès"),
        # Table cells do not run together into one word.
        ("<table><tr><td>Jean</td><td>Roy</td></tr></table>", "Jean Roy"),
        (
            "<table><tr><td>a</td><td>b</td></tr><tr><td>c</td><td>d</td></tr></table>",
            "a b\nc d",
        ),
        # An image is a description of a picture, not a transcription of ink.
        ("<p>avant</p><img alt='a seal' /><p>apres</p>", "avant\napres"),
        ("<img alt='only a seal' />", ""),
        ("", ""),
        ("   \n  ", ""),
    ],
)
def test_the_text_view_reads_one_block_by_its_declared_rule(content, expected):
    assert layout_block_text(content) == expected


def test_preformatted_text_keeps_its_own_whitespace():
    assert layout_block_text("<p>x</p><pre>  a\n  b</pre>") == "x\n  a\n  b"


def test_the_text_view_is_the_same_rule_the_page_text_is_built_from():
    parsed = _read(
        '<div data-bbox="0 0 100 50" data-label="Text"><p>a<br>b</p></div>'
        '<div data-bbox="0 50 100 100" data-label="Text"><p>  c   d </p></div>'
    )
    for block in parsed["blocks"]:
        assert block["text"] == layout_block_text(block["content"])
    assert parsed["page_text"] == "a\nb\nc d"


def test_a_block_that_delivered_no_text_gets_a_zero_width_span_where_it_would_have_sat():
    parsed = _read(
        '<div data-bbox="0 0 100 50" data-label="Text">Roy</div>'
        '<div data-bbox="0 50 100 100" data-label="Image"><img alt="a seal"/></div>'
        '<div data-bbox="0 100 100 150" data-label="Text">Jean</div>'
    )
    assert [block["text"] for block in parsed["blocks"]] == ["Roy", "", "Jean"]
    assert parsed["page_text"] == "Roy\nJean"
    assert parsed["spans"][1] == {"start": 3, "end": 3}
    assert parsed["spans"][2] == {"start": 4, "end": 8}


def test_spans_locate_every_delivered_block_in_the_page_text():
    parsed = _read(
        "".join(
            f'<div data-bbox="0 {n * 10} 100 {n * 10 + 9}" data-label="Text">acte {n}</div>'
            for n in range(1, 8)
        )
    )
    for block, span in zip(parsed["blocks"], parsed["spans"], strict=True):
        assert parsed["page_text"][span["start"] : span["end"]] == block["text"]
    assert parsed["page_text"].count("\n") == 6
