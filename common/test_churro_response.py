"""The Churro page witness's closed response contract.

Every test here is offline and byte-level: `churro_response.parse` over
hand-built bodies. What is pinned is the *closure* of the contract -- exactly
one declared form parses, every other shape is refused by a name from the
closed set, nothing is repaired or defaulted, and the geometry that comes out
is in sealed-page pixels by the Designator's own conversion.

The unit that dispatches to this module (`churro.parse`, the capture states, the
`parser="churro"` branch) is pinned in `pipeline/3_attestatores/`; this module
is the contract alone.
"""

from __future__ import annotations

import json

import pytest

from common import churro_response, native_witness
from common.structure_answer import to_page_bounds

SCHEMA = churro_response.PAGE_RESPONSE_SCHEMA
PAGE_SIZE = (200, 260)


def _body(**fields) -> bytes:
    return json.dumps({"schema": SCHEMA, **fields}).encode("utf-8")


# ================================== the one form ==================================


def test_the_blocks_form_parses_to_joined_page_text_with_one_span_per_block():
    body = _body(
        blocks=[
            {"box_1000": [110, 85, 890, 375], "text": "SYNTHETIC ACT ONE"},
            {"box_1000": [110, 470, 890, 835], "text": "SYNTHETIC ACT TWO"},
        ]
    )
    parsed = churro_response.parse(body)
    assert not churro_response.is_refusal(parsed)
    assert parsed["schema"] == SCHEMA
    assert parsed["page_text"] == "SYNTHETIC ACT ONE\nSYNTHETIC ACT TWO"
    assert parsed["spans"] == [{"start": 0, "end": 17}, {"start": 18, "end": 35}]
    assert [block["ordinal"] for block in parsed["blocks"]] == [0, 1]


def test_an_empty_block_list_is_a_page_that_holds_no_text_not_a_refusal():
    parsed = churro_response.parse(_body(blocks=[]))
    assert not churro_response.is_refusal(parsed)
    assert parsed["blocks"] == []
    assert parsed["page_text"] == ""
    assert parsed["spans"] == []


def test_an_empty_block_contributes_no_separator_and_a_zero_width_span():
    body = _body(
        blocks=[
            {"box_1000": [0, 0, 500, 500], "text": "one"},
            {"box_1000": [0, 500, 500, 1000], "text": ""},
            {"box_1000": [500, 0, 1000, 1000], "text": "two"},
        ]
    )
    parsed = churro_response.parse(body)
    assert parsed["page_text"] == "one\ntwo"
    # The zero-width span sits where the block's text would have started had it
    # delivered any -- immediately after "one", before the separator the empty
    # block did not earn.
    assert parsed["spans"] == [
        {"start": 0, "end": 3},
        {"start": 3, "end": 3},
        {"start": 4, "end": 7},
    ]


def test_every_span_indexes_the_page_text_the_same_parse_returned():
    """The spans are not an independent claim; they must cut the page text up."""
    texts = ["alpha", "", "beta gamma", "delta"]
    parsed = churro_response.parse(
        _body(
            blocks=[
                {"box_1000": [0, index * 100, 1000, index * 100 + 100], "text": text}
                for index, text in enumerate(texts)
            ]
        )
    )
    page_text = parsed["page_text"]
    for block, span in zip(parsed["blocks"], parsed["spans"], strict=True):
        assert page_text[span["start"] : span["end"]] == block["text"]


def test_there_is_no_text_only_form_here_the_trained_envelope_is_that_form():
    """Chandra's second form is refused by name rather than quietly accepted.

    Not a gap: `<output>...</output>` is Churro's trained no-geometry answer and
    stays legal on its own branch. A second spelling of the same fact inside the
    wire contract would be two records for one thing (module docstring).
    """
    body = json.dumps({"schema": SCHEMA, "text": "the whole page"}).encode("utf-8")
    assert churro_response.parse(body) == {"parse_outcome": "unverified-response-schema"}


# ============================== the closed refusals ==============================


@pytest.mark.parametrize(
    ("body", "outcome"),
    [
        ("not bytes at all", "raw-response-not-bytes"),
        (b"{not json", "invalid-json"),
        (b"\xff\xfe not utf-8", "invalid-json"),
        (b'"a bare string"', "top-level-not-object"),
        (b"[]", "top-level-not-object"),
        (b'{"blocks": []}', "unverified-response-schema"),
        (
            b'{"schema": "verbatus-chandra-page-response.v1", "blocks": []}',
            "unverified-response-schema",
        ),
        (b'{"schema": "verbatus-churro-page-response.v1"}', "missing-block-list"),
    ],
)
def test_a_body_outside_the_declared_shape_is_refused_by_its_own_name(body, outcome):
    assert churro_response.parse(body) == {"parse_outcome": outcome}


def test_an_extra_top_level_key_is_unverified_rather_than_ignored():
    body = json.dumps({"schema": SCHEMA, "blocks": [], "confidence": 0.9}).encode("utf-8")
    assert churro_response.parse(body) == {"parse_outcome": "unverified-response-schema"}


def test_a_duplicate_member_is_unverified_not_resolved_last_wins():
    """The stdlib's default silently keeps one of two values; this refuses both."""
    body = b'{"schema": "verbatus-churro-page-response.v1", "blocks": [], "blocks": []}'
    assert json.loads(body) == {"schema": SCHEMA, "blocks": []}
    assert churro_response.parse(body) == {"parse_outcome": "unverified-response-schema"}


def test_excessive_nesting_is_named_rather_than_crashing_the_stage():
    body = b'{"schema": "x", "blocks": ' + b"[" * 20_000 + b"]" * 20_000 + b"}"
    assert churro_response.parse(body) == {"parse_outcome": "excessive-json-nesting"}


def test_a_body_past_the_parse_ceiling_is_refused_before_it_is_decoded():
    body = b"x" * (churro_response.MAX_RESPONSE_BYTES + 1)
    assert churro_response.parse(body) == {"parse_outcome": "response-too-large"}


def test_the_json_door_and_the_trained_xml_door_share_one_intake_bound():
    """One chair, one parse ceiling: a body cannot be too large for only one shape."""
    assert churro_response.MAX_RESPONSE_BYTES == native_witness.CHURRO_MAX_RESPONSE_BYTES
    assert churro_response.MAX_RESPONSE_BYTES == 4 * 1024 * 1024


def test_more_blocks_than_the_ceiling_is_refused_by_name():
    blocks = [{"box_1000": [0, 0, 1, 1], "text": ""}] * (churro_response.MAX_BLOCKS + 1)
    assert churro_response.parse(_body(blocks=blocks)) == {"parse_outcome": "too-many-blocks"}


def test_exactly_the_ceiling_is_admitted():
    blocks = [{"box_1000": [0, 0, 1000, 1000], "text": ""}] * churro_response.MAX_BLOCKS
    parsed = churro_response.parse(_body(blocks=blocks))
    assert not churro_response.is_refusal(parsed)
    assert len(parsed["blocks"]) == churro_response.MAX_BLOCKS


@pytest.mark.parametrize(
    ("block", "outcome"),
    [
        ("a string, not a block", "malformed-block"),
        ({"box_1000": [0, 0, 10, 10]}, "malformed-block"),
        ({"text": "no geometry"}, "malformed-block"),
        (
            {"box_1000": [0, 0, 10, 10], "text": "x", "reading_order": 1},
            "unverified-response-schema",
        ),
        ({"box_1000": [0, 0, 10], "text": "x"}, "malformed-block-geometry"),
        ({"box_1000": [10, 0, 10, 10], "text": "x"}, "malformed-block-geometry"),
        ({"box_1000": [0, 0, 1001, 10], "text": "x"}, "malformed-block-geometry"),
        ({"box_1000": [-1, 0, 10, 10], "text": "x"}, "malformed-block-geometry"),
        ({"box_1000": [True, 0, 10, 10], "text": "x"}, "malformed-block-geometry"),
        ({"box_1000": ["0", 0, 10, 10], "text": "x"}, "malformed-block-geometry"),
        ({"box_1000": [0, 0, 10, 10], "text": 5}, "malformed-block-text"),
        ({"box_1000": [0, 0, 10, 10], "text": None}, "malformed-block-text"),
    ],
)
def test_one_malformed_block_refuses_the_whole_response_by_name(block, outcome):
    good = {"box_1000": [0, 0, 100, 100], "text": "a good block"}
    assert churro_response.parse(_body(blocks=[good, block])) == {"parse_outcome": outcome}
    assert churro_response.parse(_body(blocks=[block, good])) == {"parse_outcome": outcome}


def test_a_non_finite_coordinate_is_malformed_geometry_not_an_interpreter_error():
    body = b'{"schema": "verbatus-churro-page-response.v1", "blocks": [{"box_1000": [0, 0, NaN, 10], "text": "x"}]}'
    assert churro_response.parse(body) == {"parse_outcome": "malformed-block-geometry"}


def test_a_blocks_value_that_is_not_a_list_is_the_missing_block_list_outcome():
    for value in ({}, "blocks", 3, None):
        body = json.dumps({"schema": SCHEMA, "blocks": value}).encode("utf-8")
        assert churro_response.parse(body) == {"parse_outcome": "missing-block-list"}


def test_every_outcome_this_module_can_produce_is_inside_its_declared_set():
    """A refusal outside `PARSE_OUTCOMES` is a name no reader was told about."""
    with pytest.raises(ValueError):
        churro_response._refuse("a-name-nobody-declared")


def test_the_declared_outcome_set_is_exactly_what_this_contract_can_reach():
    assert churro_response.PARSE_OUTCOMES == frozenset(
        {
            "raw-response-not-bytes",
            "response-too-large",
            "invalid-json",
            "excessive-json-nesting",
            "top-level-not-object",
            "unverified-response-schema",
            "missing-block-list",
            "too-many-blocks",
            "malformed-block",
            "malformed-block-geometry",
            "malformed-block-text",
        }
    )


# ============================ geometry and quantization ===========================


def test_a_block_converts_to_sealed_page_pixels_by_the_shared_conversion():
    parsed = churro_response.parse(_body(blocks=[{"box_1000": [110, 85, 890, 375], "text": "x"}]))
    block = parsed["blocks"][0]
    bounds = churro_response.block_page_bounds(block, page_size=PAGE_SIZE)
    assert bounds == to_page_bounds(block["box_1000"], *PAGE_SIZE)
    assert bounds == {"x": 22, "y": 22, "w": 156, "h": 76}


def test_the_conversion_clamps_so_a_full_page_box_cannot_overshoot_the_page():
    parsed = churro_response.parse(_body(blocks=[{"box_1000": [0, 0, 1000, 1000], "text": "x"}]))
    bounds = churro_response.block_page_bounds(parsed["blocks"][0], page_size=PAGE_SIZE)
    assert bounds == {"x": 0, "y": 0, "w": 200, "h": 260}


def test_a_fractional_box_is_quantized_low_edges_floor_far_edges_ceil():
    parsed = churro_response.parse(
        _body(blocks=[{"box_1000": [10.7, 20.2, 30.1, 40.9], "text": "x"}])
    )
    assert parsed["blocks"][0]["box_1000"] == [10, 20, 31, 41]


# ================================== the dispatch fact ==============================


def test_only_this_contracts_own_schema_claims_a_body():
    assert churro_response.declares_wire_contract({"schema": SCHEMA})
    assert not churro_response.declares_wire_contract(
        {"schema": "verbatus-chandra-page-response.v1"}
    )
    assert not churro_response.declares_wire_contract({"blocks": []})
    assert not churro_response.declares_wire_contract(["not an object"])
    assert not churro_response.declares_wire_contract(None)
