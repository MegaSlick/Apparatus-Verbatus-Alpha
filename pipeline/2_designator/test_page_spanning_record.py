"""A page-spanning component, through a whole Designator pass, on the record.

`test_grouping.py` pins the partition and `test_shared_background.py` proves
the withheld pixels stay in `conservation.reconcile`'s accounting, but neither
runs the stage, and no fixture page naturally carries a page-spanning
component -- so this module paints a dark frame onto the fixture's page 1,
matching a photographed register opening, and runs it through a real pass.

Built on `test_page_residual_bound.py`'s harness, which already substitutes
one sealed page's pixels inside a real run, rather than a new fixture page
that would move every acceptance digest in the repository.
"""

import pytest
from test_page_residual_bound import (
    _base_run,
    _conservation_for,
    _designator_context,
    _grouping_config_with_bound,
    _load_designator,
    _records,
    _substitute_page_pixels,
)

from common.imaging import encode_grayscale_png, grayscale_rows

# 12px keeps the page majority-paper (background inference unaffected) while
# leaving 8px of paper past the sealed gap_tolerance_px, so the frame labels
# as its own component rather than chaining into an act.
_FRAME_BAND_PX = 12
_FRAME_INK = 5


def _frame_only_page_png() -> bytes:
    """A uniform paper field with one dark page-spanning component."""
    width, height = 200, 260
    rows = [bytearray([230]) * width for _ in range(height)]
    for y in range(height):
        for x in range(width):
            if (
                x < _FRAME_BAND_PX
                or y < _FRAME_BAND_PX
                or x >= width - _FRAME_BAND_PX
                or y >= height - _FRAME_BAND_PX
            ):
                rows[y][x] = _FRAME_INK
    return encode_grayscale_png(width, height, rows)


def _framed_page_png() -> bytes:
    """The fixture's page 1 with a photographed opening's dark border painted on."""
    from proof.synthetic_pages import page_bytes

    width, height, rows = grayscale_rows(page_bytes(1))
    for y in range(height):
        for x in range(width):
            if (
                x < _FRAME_BAND_PX
                or y < _FRAME_BAND_PX
                or x >= width - _FRAME_BAND_PX
                or y >= height - _FRAME_BAND_PX
            ):
                rows[y][x] = _FRAME_INK
    return encode_grayscale_png(width, height, rows)


@pytest.fixture(scope="module")
def frame_only_pass(tmp_path_factory):
    """One full Designator pass where every found component is withheld."""
    with pytest.MonkeyPatch.context() as monkeypatch:
        root = tmp_path_factory.mktemp("frame-only") / "runs"
        grouping_config = _grouping_config_with_bound(root.parent, 2000)
        _base_run(root, grouping_config)
        designator = _load_designator()
        context = _designator_context(root, designator, grouping_config)
        page = _frame_only_page_png()
        _substitute_page_pixels(designator, monkeypatch, 1, page)

        width, height, rows = grayscale_rows(page)
        policy = designator.grouping_config.load_grouping_config(grouping_config)
        background_policy = designator.grouping_config.resolve_background_policy(
            policy, width, height
        )
        evidence = designator.structure.infer_background_evidence(
            width, height, rows, background_policy=background_policy
        )
        thresholds = designator.grouping_config.resolve_thresholds(policy, width, height)
        components = designator.structure.primary_scan(
            width,
            height,
            rows,
            background=evidence["background"],
            margin=evidence["ink_margin"],
            gap_tolerance_px=thresholds.gap_tolerance_px,
        )
        grouped, withheld = designator.grouping.partition_page_spanning(
            components,
            width,
            height,
            page_spanning_area_bp=thresholds.page_spanning_area_bp,
        )
        assert components and not grouped and withheld == components

        held = designator.initial_pass(context)
        yield designator, context, held


@pytest.fixture(scope="module")
def framed_pass(tmp_path_factory):
    """One whole Designator initial pass over a framed page 1."""
    with pytest.MonkeyPatch.context() as monkeypatch:
        root = tmp_path_factory.mktemp("framed") / "runs"
        # The shipped bound, asked for by name; not about the residual ceiling.
        grouping_config = _grouping_config_with_bound(root.parent, 2000)
        _base_run(root, grouping_config)
        designator = _load_designator()
        context = _designator_context(root, designator, grouping_config)
        _substitute_page_pixels(designator, monkeypatch, 1, _framed_page_png())
        held = designator.initial_pass(context)
        yield designator, context, held


def test_all_withheld_ink_uses_readable_fallbacks_without_claiming_no_ink(frame_only_pass):
    """The full stage preserves the cause, crops, and exact pixel accounting."""
    _designator, context, held = frame_only_pass
    fallback = _records(context, "page-fallback")[0]["payload"]
    assert fallback["page_ordinal"] == 1
    assert "every connected component" in fallback["reason"]
    assert "found no ink" not in fallback["reason"]

    page_groups = [
        record["payload"]
        for record in _records(context, "act-group")
        if record["payload"]["act_key"] in {"a1", "a2"}
    ]
    assert page_groups
    assert all(group["structure_evidence"] == "fallback-tiles" for group in page_groups)
    assert all("found ink" in group["rationale"] for group in page_groups)
    assert all("found no ink" not in group["rationale"] for group in page_groups)

    regions = [
        record["payload"]
        for record in _records(context, "region")
        if record["payload"]["act_key"] == fallback["act_key"]
    ]
    assert len(regions) == fallback["tile_count"] > 0
    assert all(region["image_sha256"] and region["padding"] is None for region in regions)

    seal = _records(context, "proposal-seal")[0]["payload"]
    fallback_row = next(
        row for row in seal["expected_acts"] if row["act_key"] == fallback["act_key"]
    )
    assert fallback_row["outcome"] == "proposed"
    assert len(fallback_row["evidence"]) == fallback["tile_count"]

    conservation = _conservation_for(context, 1)["payload"]
    assert conservation["page_spanning_components"] == [
        {"bounds": {"x": 0, "y": 0, "w": 200, "h": 260}, "pixel_count": 10_464}
    ]
    assert conservation["total_ink_pixel_count"] == 10_464
    assert conservation["claimed_pixel_count"] == 10_464
    assert conservation["residual_pixel_count"] == 0
    assert held is False


def test_the_frame_is_published_by_name_on_the_conservation_record(framed_pass):
    """The decision is on the record, not inferable from a group that is missing."""
    _designator, context, _held = framed_pass
    payload = _conservation_for(context, 1)["payload"]
    assert payload["page_spanning_components"] == [
        {"bounds": {"x": 0, "y": 0, "w": 200, "h": 260}, "pixel_count": 10_464}
    ]


def test_no_group_on_the_framed_page_claims_the_leaf(framed_pass):
    """The whole point: the frame stops being connective tissue for `_chain_body`."""
    _designator, context, _held = framed_pass
    records = _records(context, "act-group")
    assert records, "the framed page must still propose acts"
    for record in records:
        payload = record["payload"]
        for field in ("declared_bounds", "detected_bounds"):
            bounds = payload[field]
            if bounds is None:
                continue
            assert (bounds["w"], bounds["h"]) != (200, 260), (
                f"{payload['act_key']}'s {field} is the whole page"
            )


def test_the_frames_pixels_are_still_counted_and_now_appear_as_residual(framed_pass):
    """Withheld from grouping, never removed from the accounting."""
    _designator, context, held = framed_pass
    payload = _conservation_for(context, 1)["payload"]
    total = payload["total_ink_pixel_count"]
    claimed = payload["claimed_pixel_count"]
    residual = payload["residual_pixel_count"]
    assert claimed + residual == total
    # 11,520 unframed ink pixels plus the frame's 10,464.
    assert total == 11_520 + 10_464
    # Not the whole 10,464: the two acts' padded capture rectangles legitimately
    # claim 2,676 of the frame's pixels.
    assert residual == 7_788
    assert claimed == total - 7_788
    assert payload["residual_component_count"] >= 1
    assert "max_residual_components" not in payload
    assert held is True, "unclaimed ink withholds a complete exit"


def test_the_unframed_fixture_page_publishes_no_such_block(framed_pass):
    """Page 2 is untouched, and says so by the key being absent rather than null."""
    _designator, context, _held = framed_pass
    payload = _conservation_for(context, 2)["payload"]
    assert "page_spanning_components" not in payload
    assert payload["residual_pixel_count"] == 0


def test_the_sealed_bound_is_on_every_structure_status_record(framed_pass):
    """`resolved_thresholds` carries the bound a withheld component was judged by."""
    _designator, context, _held = framed_pass
    statuses = _records(context, "structure-status")
    assert statuses
    for record in statuses:
        assert record["payload"]["resolved_thresholds"]["page_spanning_area_bp"] == 5000
