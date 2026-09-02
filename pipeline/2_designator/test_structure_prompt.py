"""SPEC_D §1.1, §1.2, §4, §6 (D1 row).

Covers `structure_prompt.py` (sealed prompt text and its digest, and the
GOVERNANCE 10 no-preference/no-severity/no-confidence check a test can
actually pin) and the one equality this stage owns for
`common/structure_answer.py`: its page-pixel conversion against
`geometry_layer.chandra_layout`'s own arithmetic, over a grid of boxes
including the 0 and 1000 edges. It lives here rather than in `common/`
because `geometry_layer` lives in `pipeline/2_designator/` and `common/` may
not import a stage.
"""

from __future__ import annotations

import re

import geometry
import pytest
import structure_prompt
from geometry_layer import RESPONSE_BLOB_PREFIX, chandra_layout
from structure_prompt import STRUCTURE_PROMPT_VERSION, messages, prompt_sha256

from common.contracts.errors import SchemaRefusal
from common.structure_answer import STRUCTURE_ANSWER_SCHEMA, to_page_bounds

RECEIPT = {"relative_path": "receipts/sha256/" + "a" * 64 + ".json", "sha256": "a" * 64}
RESPONSE = {"relative_path": RESPONSE_BLOB_PREFIX + "b" * 64, "sha256": "b" * 64}

FORBIDDEN_WORDS = ("score", "rank", "prefer", "best", "confidence", "severity", "priority")


# ---------------------------------------------------------------------------
# Prompt shape and digest.
# ---------------------------------------------------------------------------


def test_prompt_version_is_the_declared_seal():
    assert STRUCTURE_PROMPT_VERSION == "verbatus-structure-prompt.v1"


def test_messages_is_a_system_and_user_turn_with_no_image_block():
    result = messages()
    assert isinstance(result, tuple)
    assert [message["role"] for message in result] == ["system", "user"]
    assert all(isinstance(message["content"], str) and message["content"] for message in result)


def test_prompt_digest_is_stable_across_calls():
    assert prompt_sha256() == prompt_sha256()


def test_prompt_digest_is_the_pinned_seal():
    """The mechanical half of the seal the module docstring promises: changing
    the prompt text must bump `STRUCTURE_PROMPT_VERSION` and re-pin this digest
    in the same commit, or this test catches the drift."""
    assert prompt_sha256() == "a476df092ce6c628cab02b034b371da1b2057561f71f2a0c8422557a7600dc0e"


def test_prompt_digest_changes_if_the_rendered_text_changes(monkeypatch):
    before = prompt_sha256()
    monkeypatch.setattr(structure_prompt, "_USER_TEXT", structure_prompt._USER_TEXT + " ")
    after = prompt_sha256()
    assert before != after


def test_prompt_text_states_no_preference_severity_floor_or_confidence_budget():
    """GOVERNANCE 10: an instrument may state no preference, severity floor, or
    confidence budget. Pinned directly against the rendered text, not the
    module's own docstring, so a wording change that reintroduced one of these
    words into what the model actually receives would fail here."""
    rendered = "\n".join(message["content"] for message in messages())
    hits = [word for word in FORBIDDEN_WORDS if re.search(word, rendered, re.IGNORECASE)]
    assert not hits, f"the rendered prompt text contains forbidden word(s): {hits}"


def test_prompt_asks_for_the_exact_json_shape_and_nothing_else():
    """Compared against the imported constant, not a second hard-coded literal
    -- a schema bump that forgot the prompt text must fail here, not stay
    invisible because two files each spelled the string independently."""
    rendered = "\n".join(message["content"] for message in messages())
    assert f'"schema": "{STRUCTURE_ANSWER_SCHEMA}"' in rendered
    assert "box_1000" in rendered
    assert "reading order" in rendered


# ---------------------------------------------------------------------------
# Conversion equality: common/structure_answer.py::to_page_bounds against
# geometry_layer.chandra_layout's own arithmetic.
# ---------------------------------------------------------------------------

GRID_PAGE_SIZES = [(100, 100), (37, 53), (1, 1), (2000, 3000), (1000, 1000)]
GRID_BOXES = [
    [0, 0, 1000, 1000],  # the whole page, both edges
    [0, 0, 1, 1],  # tiny box at the low edge
    [999, 999, 1000, 1000],  # tiny box at the far edge
    [0, 0, 500, 500],
    [500, 500, 1000, 1000],
    [123, 456, 789, 999],
    [0, 500, 1000, 501],  # a thin full-width strip
    [1, 1, 999, 999],
]

# A conversion one pixel wide or tall collapses `chandra_layout`'s four corner
# points to fewer than three distinct ones, and `_polygon_points`
# (geometry_layer.py:178-180, reached through `validate_raw_proposal`) refuses
# that before it ever produces an `aabb` -- so a hairline `to_page_bounds`
# result is outside the domain where the two functions are comparable at all.
# Filtered here rather than dropped silently: the count below pins that the
# filter still leaves the grid worth running, per GOVERNANCE 2.
GRID_CASES = [
    (box, page_w, page_h)
    for box in GRID_BOXES
    for (page_w, page_h) in GRID_PAGE_SIZES
    if to_page_bounds(box, page_w, page_h)["w"] >= 2
    and to_page_bounds(box, page_w, page_h)["h"] >= 2
]
assert len(GRID_CASES) == 23


@pytest.mark.parametrize("box,page_w,page_h", GRID_CASES)
def test_to_page_bounds_matches_chandra_layout_over_a_grid_of_boxes(box, page_w, page_h):
    proposals = chandra_layout(
        page_id="pg_fixture",
        page_ordinal=0,
        page_w=page_w,
        page_h=page_h,
        config_sha256="c" * 64,
        receipt_ref=RECEIPT,
        response_ref=RESPONSE,
        regions=[{"bbox_1000": box, "score_bp": 9000}],
    )
    assert len(proposals) == 1
    assert to_page_bounds(box, page_w, page_h) == proposals[0]["aabb"]


def test_to_page_bounds_matches_chandra_layout_for_two_regions_on_one_page():
    """Not only the single-region case: two distinct rectangles converted
    together, since the union path is what a real answer with several acts
    exercises."""
    boxes = [[0, 0, 500, 500], [500, 500, 1000, 1000]]
    proposals = chandra_layout(
        page_id="pg_fixture",
        page_ordinal=0,
        page_w=640,
        page_h=480,
        config_sha256="c" * 64,
        receipt_ref=RECEIPT,
        response_ref=RESPONSE,
        regions=[{"bbox_1000": box, "score_bp": 9000} for box in boxes],
    )
    assert len(proposals) == 2
    by_aabb = {tuple(sorted(p["aabb"].items())): p for p in proposals}
    for box in boxes:
        expected = to_page_bounds(box, 640, 480)
        assert tuple(sorted(expected.items())) in by_aabb


@pytest.mark.parametrize(
    "box,page_w,page_h",
    [
        ([0, 0, 1, 1], 100, 100),
        ([0, 500, 1000, 501], 1000, 1000),
    ],
)
def test_a_hairline_conversion_is_bounds_here_and_refused_by_the_layout_path(box, page_w, page_h):
    """The gap the grid filter above carves out, named rather than left
    implied: `to_page_bounds` and `geometry.validate_bounds` treat a
    conversion one pixel wide or tall as ordinary page geometry, while
    `chandra_layout` refuses the same box as not-a-polygon. The two converters
    agree everywhere `chandra_layout` actually returns a proposal; this is the
    boundary of that domain, not a mismatch inside it."""
    bounds = to_page_bounds(box, page_w, page_h)
    assert bounds["w"] == 1 or bounds["h"] == 1
    geometry.validate_bounds(bounds, page_w, page_h, "structure-chair rectangle")
    with pytest.raises(SchemaRefusal, match="fewer than three distinct points"):
        chandra_layout(
            page_id="pg_fixture",
            page_ordinal=0,
            page_w=page_w,
            page_h=page_h,
            config_sha256="c" * 64,
            receipt_ref=RECEIPT,
            response_ref=RESPONSE,
            regions=[{"bbox_1000": box, "score_bp": 9000}],
        )
