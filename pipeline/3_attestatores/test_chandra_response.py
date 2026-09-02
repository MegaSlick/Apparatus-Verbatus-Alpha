"""The Chandra page witness's closed response contract, and the adapter over it.

Every test here is offline and byte-level: `chandra_response.parse` over
hand-built bodies, and `chandra.observe` over the same bytes. What is pinned
is the *closure* of the contract -- exactly two declared forms parse, every
other shape is refused by a name from the closed set, and the geometry that
comes out is in sealed-page pixels by the Designator's own conversion.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

STAGE = Path(__file__).resolve().parent
if str(STAGE) not in sys.path:
    sys.path.insert(0, str(STAGE))

import chandra  # noqa: E402
import chandra_response  # noqa: E402

from common.contracts.errors import SchemaRefusal  # noqa: E402
from common.structure_answer import to_page_bounds  # noqa: E402

SCHEMA = chandra_response.PAGE_RESPONSE_SCHEMA
PAGE_SIZE = (200, 260)


def _body(**fields) -> bytes:
    return json.dumps({"schema": SCHEMA, **fields}).encode("utf-8")


def _page_presentation(kind: str = "page") -> dict:
    bounds = (
        {"x": 0, "y": 0, "w": 200, "h": 260}
        if kind == "page"
        else {"x": 20, "y": 20, "w": 160, "h": 80}
    )
    presentation = {
        "kind": kind,
        "source_page_id": "page-1",
        "source_page_ordinal": 1,
        "image_path": "1_exemplar/blobs/sha256/" + "0" * 64,
        "image_sha256": "0" * 64,
        "transform": {
            "operation": "whole" if kind == "page" else "crop",
            "source_page_id": "page-1",
            "source_page_ordinal": 1,
            "bounds": bounds,
        },
    }
    if kind == "region":
        presentation["region_ref"] = {"region_id": "act-1-region-0"}
    return presentation


# ================================ the two forms ================================


def test_the_blocks_form_parses_to_joined_page_text_with_one_span_per_block():
    body = _body(
        blocks=[
            {"box_1000": [100, 77, 900, 385], "text": "SYNTHETIC ACT ONE"},
            {"box_1000": [100, 462, 900, 846], "text": "SYNTHETIC ACT TWO"},
        ]
    )
    parsed = chandra_response.parse(body)
    assert not chandra_response.is_refusal(parsed)
    assert parsed["geometry"] is True
    assert parsed["page_text"] == "SYNTHETIC ACT ONE\nSYNTHETIC ACT TWO"
    assert parsed["spans"] == [{"start": 0, "end": 17}, {"start": 18, "end": 35}]
    assert [block["ordinal"] for block in parsed["blocks"]] == [0, 1]
    assert chandra.parse(body) == "SYNTHETIC ACT ONE\nSYNTHETIC ACT TWO"


def test_an_empty_block_contributes_no_separator_and_a_zero_width_span():
    body = _body(
        blocks=[
            {"box_1000": [0, 0, 500, 500], "text": "one"},
            {"box_1000": [0, 500, 500, 1000], "text": ""},
            {"box_1000": [500, 0, 1000, 1000], "text": "two"},
        ]
    )
    parsed = chandra_response.parse(body)
    assert parsed["page_text"] == "one\ntwo"
    assert parsed["spans"] == [
        {"start": 0, "end": 3},
        {"start": 3, "end": 3},
        {"start": 4, "end": 7},
    ]


def test_the_page_text_form_parses_with_no_geometry():
    parsed = chandra_response.parse(_body(text="the whole page"))
    assert parsed["geometry"] is False
    assert parsed["blocks"] == []
    assert parsed["page_text"] == "the whole page"
    assert parsed["spans"] == []
    assert chandra.parse(_body(text="the whole page")) == "the whole page"


def test_an_empty_blocks_list_is_the_blocks_form_with_no_text():
    parsed = chandra_response.parse(_body(blocks=[]))
    assert parsed["geometry"] is True
    assert parsed["page_text"] == ""
    assert chandra.parse(_body(blocks=[])) == ""


# ============================ everything else refuses ==========================


@pytest.mark.parametrize(
    ("raw", "outcome"),
    [
        ("not bytes", "raw-response-not-bytes"),
        (b"\xff\xfe", "invalid-json"),
        (b"{not json", "invalid-json"),
        (b"[1, 2]", "top-level-not-object"),
        (b'{"markdown": "a real Chandra body"}', "unverified-response-schema"),
        (b'{"schema": "fixture-chandra-response.v1", "markdown": "x", "blocks": []}', None),
        (b'{"schema": "some-other.v1", "blocks": []}', "unverified-response-schema"),
        (_body(blocks=[], extra=1), "unverified-response-schema"),
        (_body(), "missing-page-form"),
        (_body(blocks=[], text="x"), "conflicting-page-forms"),
        (_body(text=7), "malformed-page-text"),
        (_body(blocks="x"), "malformed-block"),
        (_body(blocks=[7]), "malformed-block"),
        (_body(blocks=[{"text": "x"}]), "malformed-block"),
        (
            _body(blocks=[{"box_1000": [0, 0, 1, 1], "text": "x", "label": "y"}]),
            "unverified-response-schema",
        ),
        (_body(blocks=[{"box_1000": [0, 0, 1, 1], "text": 7}]), "malformed-block-text"),
        (_body(blocks=[{"box_1000": [0, 0, 1], "text": "x"}]), "malformed-block-geometry"),
        (_body(blocks=[{"box_1000": [0, 0, 1001, 1], "text": "x"}]), "malformed-block-geometry"),
        (_body(blocks=[{"box_1000": [5, 0, 5, 1], "text": "x"}]), "malformed-block-geometry"),
        (_body(blocks=[{"box_1000": [0, 0, True, 1], "text": "x"}]), "malformed-block-geometry"),
        (_body(blocks=[{"box_1000": [-1, 0, 1, 1], "text": "x"}]), "malformed-block-geometry"),
    ],
)
def test_every_shape_outside_the_contract_is_refused_by_a_closed_name(raw, outcome):
    parsed = chandra_response.parse(raw)
    if outcome is None:
        # The fixture placeholder is not this contract's shape; the adapter
        # dispatches it to its own validation, and this module names it.
        assert parsed == {"parse_outcome": "unverified-response-schema"}
        assert chandra.parse(raw) == "x"
        return
    assert parsed == {"parse_outcome": outcome}
    assert outcome in chandra_response.PARSE_OUTCOMES
    if isinstance(raw, bytes):
        # The adapter agrees with the contract on every refusal it dispatches.
        adapter_outcome = chandra.parse(raw)
        assert isinstance(adapter_outcome, dict) and "parse_outcome" in adapter_outcome


def test_a_duplicate_member_is_an_unverified_shape_not_a_silent_last_wins():
    raw = (
        b'{"schema": "' + SCHEMA.encode() + b'", "blocks": [], "blocks": '
        b'[{"box_1000": [0, 0, 1, 1], "text": "second"}]}'
    )
    assert chandra_response.parse(raw) == {"parse_outcome": "unverified-response-schema"}
    assert chandra.parse(raw) == {"parse_outcome": "unverified-response-schema"}


def test_the_byte_and_block_ceilings_refuse_by_name(monkeypatch):
    monkeypatch.setattr(chandra_response, "MAX_RESPONSE_BYTES", 8)
    assert chandra_response.parse(b"123456789") == {"parse_outcome": "response-too-large"}
    monkeypatch.setattr(chandra_response, "MAX_RESPONSE_BYTES", 16 * 1024 * 1024)
    monkeypatch.setattr(chandra_response, "MAX_BLOCKS", 1)
    two = _body(blocks=[{"box_1000": [0, 0, 1, 1], "text": "a"}] * 2)
    assert chandra_response.parse(two) == {"parse_outcome": "too-many-blocks"}


def test_excessive_nesting_is_named_rather_than_raised(monkeypatch):
    def _exhausts_the_stack(_text, **_kwargs):
        raise RecursionError("maximum recursion depth exceeded while decoding")

    monkeypatch.setattr(chandra_response.json, "loads", _exhausts_the_stack)
    assert chandra_response.parse(_body(blocks=[])) == {"parse_outcome": "excessive-json-nesting"}


def test_a_refusal_outside_the_closed_set_cannot_be_minted():
    with pytest.raises(ValueError, match="undeclared parse outcome"):
        chandra_response._refuse("invented-outcome")


# ================================== geometry ==================================


def test_block_geometry_lands_in_sealed_page_pixels_by_the_designators_conversion():
    body = _body(
        blocks=[
            {"box_1000": [100, 77, 900, 385], "text": "SYNTHETIC ACT ONE"},
            {"box_1000": [100.4, 461.9, 899.2, 845.5], "text": "SYNTHETIC ACT TWO"},
        ]
    )
    observed = chandra.observe(_page_presentation(), body, page_size=PAGE_SIZE)
    assert observed == [
        {
            "ordinal": 0,
            "bounds": to_page_bounds([100, 77, 900, 385], 200, 260),
            "bounds_source": "native",
            "span": {"start": 0, "end": 17},
        },
        {
            # Floats quantized low-floor / far-ceil in normalized space first.
            "ordinal": 1,
            "bounds": to_page_bounds([100, 461, 900, 846], 200, 260),
            "bounds_source": "native",
            "span": {"start": 18, "end": 35},
        },
    ]
    assert observed[0]["bounds"] == {"x": 20, "y": 20, "w": 160, "h": 81}
    # 461.9 floors to 461 before conversion, so the rectangle grew by a row
    # rather than shrinking: quantization never loses ink.
    assert observed[1]["bounds"] == {"x": 20, "y": 119, "w": 160, "h": 101}


def test_a_normalized_box_at_the_far_edge_never_overshoots_the_sealed_page():
    body = _body(blocks=[{"box_1000": [0, 0, 1000, 1000], "text": "everything"}])
    [block] = chandra.observe(_page_presentation(), body, page_size=PAGE_SIZE)
    assert block["bounds"] == {"x": 0, "y": 0, "w": 200, "h": 260}


def test_an_act_view_converts_against_the_page_not_its_own_crop():
    """A page witness's act view presents one crop while restating page geometry."""
    body = _body(blocks=[{"box_1000": [100, 462, 900, 846], "text": "SYNTHETIC ACT TWO"}])
    [block] = chandra.observe(_page_presentation("region"), body, page_size=PAGE_SIZE)
    assert block["bounds"] == {"x": 20, "y": 120, "w": 160, "h": 100}


def test_block_geometry_without_a_page_size_is_refused_not_misplaced():
    body = _body(blocks=[{"box_1000": [100, 77, 900, 385], "text": "x"}])
    with pytest.raises(SchemaRefusal, match="pass page_size"):
        chandra.observe(_page_presentation(), body)


@pytest.mark.parametrize("body", [_body(text="page text only"), _body(blocks=[])])
def test_a_response_with_no_block_geometry_derives_none(body):
    """Not an echo from the adapter: the shared page-edge check admits only
    reported geometry, and `run.py` supplies the presentation echo itself for a
    page with none, as it does for the fixture's no-geometry rows."""
    assert chandra.observe(_page_presentation(), body) == []
    assert chandra.observe(_page_presentation(), body, page_size=PAGE_SIZE) == []


def test_a_refused_body_derives_no_geometry():
    assert chandra.observe(_page_presentation(), _body(blocks=[7]), page_size=PAGE_SIZE) == []
    assert chandra.observe(_page_presentation(), b'{"markdown": "x"}', page_size=PAGE_SIZE) == []


# ================================= the prompt =================================


def test_the_served_prompt_asks_for_exactly_the_contract_and_the_fixture_prompt_is_frozen():
    prompt = chandra.prompt()
    assert set(prompt) == {"instruction"}
    assert SCHEMA in prompt["instruction"]
    assert "box_1000" in prompt["instruction"]
    # The fixture posture records this declaration, and it is not the served
    # instruction: the pinned fixture bytes must not move when the served
    # wording does.
    assert chandra.FIXTURE_PROMPT == {
        "instruction": "Transcribe this complete page and report layout blocks in reading order."
    }
    assert chandra.FIXTURE_PROMPT != prompt
