"""dots.mocr's layout grammar, text view and geometry, and its capture's re-derivation."""

from __future__ import annotations

import json

import pytest

from common import dots_layout
from common.contracts.canonical import digest_bytes
from common.contracts.errors import SchemaRefusal
from common.native_witness import (
    CAPTURE_TEXT_VIEWS,
    derive_dots_capture,
    dots_capture_view,
    validate_native_capture,
    verify_native_capture_bytes,
)


def _raw(cells) -> bytes:
    return json.dumps(cells).encode("utf-8")


def test_the_carried_prompt_is_the_one_the_bake_off_sent_the_real_model():
    assert digest_bytes(dots_layout.LAYOUT_PROMPT.encode()) == dots_layout.LAYOUT_PROMPT_SHA256
    # The vendor sends its placeholder in the same text part, before the prompt.
    assert dots_layout.prompt_text().startswith("<|img|><|imgpad|><|endofimg|>Please output")
    assert dots_layout.LAYOUT_PROMPT.endswith("single JSON object.\n")


def test_each_category_is_read_under_the_bake_offs_text_view():
    layout = dots_layout.parse_layout(
        _raw(
            [
                {"bbox": [0, 0, 10, 10], "category": "Page-header", "text": "## Folio **7**"},
                {"bbox": [0, 10, 10, 20], "category": "Picture"},
                {
                    "bbox": [0, 20, 10, 30],
                    "category": "Table",
                    "text": "<table><tr><td>Dupont</td><td>12</td></tr>"
                    "<tr><th>Martin &amp; fils</th><td>14</td></tr></table>",
                },
                {"bbox": [0, 30, 10, 40], "category": "Formula", "text": "$$x^2$$"},
                {"bbox": [0, 40, 10, 50], "category": "List-item", "text": "- an  \\*item\\*"},
            ]
        )
    )
    assert layout["state"] == "parsed"
    assert [cell["text"] for cell in layout["cells"]] == [
        "Folio 7",
        "",
        "Dupont 12\nMartin & fils 14",
        "x^2",
        "an *item*",
    ]
    assert layout["text"] == "Folio 7\nDupont 12\nMartin & fils 14\nx^2\nan *item*"
    assert layout["findings"] == []
    assert layout["view"] == dots_layout.TEXT_VIEW
    spans = dots_layout.cell_spans(layout["cells"])
    assert spans[1] is None
    for cell, span in zip(layout["cells"], spans, strict=True):
        if span is not None:
            assert layout["text"][span["start"] : span["end"]] == cell["text"]


@pytest.mark.parametrize(
    ("raw", "state", "words"),
    [
        (
            b'[{"bbox": [1, 2, 3, 4], "category": "Table", "text": "<tr><td>SYN',
            "failed",
            "never salvaged",
        ),
        (b"\xff\xfe", "failed", "not UTF-8"),
        (b'{"bbox": [1, 2, 3, 4]}', "unrecognized-shape", "JSON dict"),
        (b'["a cell that is a string"]', "unrecognized-shape", "cell 0"),
    ],
)
def test_an_answer_outside_the_grammar_is_not_parsed_and_not_repaired(raw, state, words):
    layout = dots_layout.parse_layout(raw)
    assert layout["state"] == state
    assert words in layout["reason"]


def test_a_malformed_cell_is_kept_with_the_grammars_own_finding():
    layout = dots_layout.parse_layout(
        _raw(
            [
                {"bbox": [1, 2, 3], "category": "Text", "text": "short box"},
                {"bbox": [1, 2, 3, 4], "category": "Marginalia", "text": "new category"},
                {"bbox": [1, 2, 3, 4], "category": "Text", "text": 7},
            ]
        )
    )
    assert layout["state"] == "parsed"
    assert layout["findings"] == [
        {"kind": "malformed-bbox", "cell": 0},
        {"kind": "unknown-category", "cell": 1},
        {"kind": "text-not-string", "cell": 2},
    ]
    assert layout["cells"][0]["bbox"] is None
    assert layout["text"] == "short box\nnew category"


def test_smart_resize_is_qwen2_vls_grid_within_the_snapshots_pixel_bounds():
    assert dots_layout.smart_resize(260, 200) == (252, 196)
    # Too small: scaled up past 3,136 pixels; too large: down under 11,289,600.
    height, width = dots_layout.smart_resize(20, 20)
    assert height * width >= dots_layout.MIN_PIXELS and height % 28 == width % 28 == 0
    height, width = dots_layout.smart_resize(7000, 5000)
    assert height * width <= dots_layout.MAX_PIXELS and height % 28 == width % 28 == 0


def test_a_cell_box_maps_back_to_the_sealed_page_and_never_off_it():
    # Page 200x260; the processor saw 196x252.
    assert dots_layout.cell_page_bounds([20, 20, 176, 78], (200, 260)) == {
        "x": 20,
        "y": 20,
        "w": 160,
        "h": 61,
    }
    assert dots_layout.cell_page_bounds([0, 0, 196, 252], (200, 260)) == {
        "x": 0,
        "y": 0,
        "w": 200,
        "h": 260,
    }
    for box in ([0, 0, 197, 10], [-1, 0, 10, 10], [10, 10, 10, 20], None):
        assert dots_layout.cell_page_bounds(box, (200, 260)) is None


def test_a_dots_capture_re_derives_from_its_raw_answer_alone():
    raw = _raw([{"bbox": [20, 20, 176, 78], "category": "Text", "text": "Le dix mai"}])
    derived = derive_dots_capture(raw, "stop", parser=dots_layout.PARSER)
    assert derived["parse"] == {"state": "parsed", "parser": "layout-json", "text": "Le dix mai"}
    assert derived["stop_reason"] == "stop"
    assert derived["text_view"] == dots_layout.TEXT_VIEW
    capture = {
        "schema": "attestatores-model-view.v1",
        "adapter": dots_layout.ADAPTER,
        "view": dots_capture_view(),
        "raw_response_ref": {
            "relative_path": f"3_attestatores/blobs/sha256/{digest_bytes(raw)}",
            "sha256": digest_bytes(raw),
        },
        "transport_stop_reason": "stop",
        **derived,
    }
    validate_native_capture(capture)
    assert verify_native_capture_bytes(capture, raw) == capture
    with pytest.raises(SchemaRefusal, match="differs from its retained raw response"):
        verify_native_capture_bytes(capture, _raw([{"bbox": [1, 2, 3, 4], "category": "Text"}]))
    with pytest.raises(SchemaRefusal, match="prompt"):
        validate_native_capture({**capture, "view": {"prompt": {"user": "another prompt"}}})
    assert CAPTURE_TEXT_VIEWS[(dots_layout.ADAPTER, dots_layout.PARSER)] == "dots-layout-text.v1"


def test_a_cut_off_answer_names_the_parse_failure_as_its_stop_reason():
    derived = derive_dots_capture(b'[{"bbox": [1, 2', "length", parser=dots_layout.PARSER)
    assert derived["parse"]["state"] == "failed"
    assert derived["stop_reason"] == "partial-parse-failed"
