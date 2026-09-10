"""A page-spanning component, through a whole Designator pass, on the record.

`test_grouping.py` pins the partition and `test_shared_background.py` proves the
withheld pixels stay in `conservation.reconcile`'s accounting. Neither of them
runs the stage, and no fixture page carries a page-spanning component -- the
largest on a walking-skeleton page covers 2996 basis points of the page against
the sealed bound of 5000 -- so without this module the `page_spanning_components`
block would ship never having been published by the code that publishes it.

The page under test is the fixture's own page 1 with a dark frame painted around
it: exactly the shape a photographed register opening has, and the shape the
survey measured fourteen of. The fixture's own ink is untouched inside the frame,
so both declared acts still match the structural groups detection finds for them
and the run proceeds as an ordinary one -- what changes is that the frame is one
more connected component, and its bounding box is the leaf.

Built on `test_page_residual_bound.py`'s harness, which already substitutes one
sealed page's pixels inside a real Door-and-Exemplar run; the alternative was a
new fixture page, and adding one would move every acceptance digest in the
repository to test a branch a substituted page reaches just as truthfully.
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

# How wide the painted frame is, in pixels of a 200x260 page. Twelve is chosen
# so the frame is unambiguous on both counts that matter and marginal on
# neither: it is 10,464 pixels, a fifth of the page, so the page is still
# majority paper and `infer_background` takes the same `inferred-modal` branch
# at the same paper value of 230 that the unframed fixture takes -- this test is
# about grouping, and a frame wide enough to move the background inference would
# be testing two things at once. And it leaves 8 pixels of paper between the
# frame and the nearest fixture ink, comfortably past the sealed
# `gap_tolerance_px = 3`, so the frame labels as its own component rather than
# chaining into an act and taking it along.
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
        evidence = designator.structure.infer_background_evidence(
            width, height, rows, background_policy=policy["background"]
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
        # The shipped bound, asked for by name: this module is not about the
        # residual ceiling and must run under whatever the policy actually seals.
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
    """The decision is on the record, not inferable from a group that is missing.

    One component, its bounds the whole page, its pixel count the frame's own.
    GOVERNANCE 2: a page-sized region the grouping pass declined to claim may
    not reach a reader as an unexplained residual.
    """
    _designator, context, _held = framed_pass
    payload = _conservation_for(context, 1)["payload"]
    assert payload["page_spanning_components"] == [
        {"bounds": {"x": 0, "y": 0, "w": 200, "h": 260}, "pixel_count": 10_464}
    ]


def test_no_group_on_the_framed_page_claims_the_leaf(framed_pass):
    """The whole point: the frame stops being connective tissue.

    Before the bound, a component with the page's own bounds sat in the body
    column with the page's own y-range, so `_chain_body` chained every other
    body component to it and one group came back whose bounds were the leaf.
    """
    _designator, context, _held = framed_pass
    records = _records(context, "act-group")
    assert records, "the framed page must still propose acts"
    for record in records:
        payload = record["payload"]
        # `detected_bounds` is the structural group the act was matched against
        # -- the rectangle grouping actually produced, as against the declared
        # one the run asked for. It is the one that would have been the leaf.
        for field in ("declared_bounds", "detected_bounds"):
            bounds = payload[field]
            if bounds is None:
                continue
            assert (bounds["w"], bounds["h"]) != (200, 260), (
                f"{payload['act_key']}'s {field} is the whole page"
            )


def test_the_frames_pixels_are_still_counted_and_now_appear_as_residual(framed_pass):
    """Withheld from grouping, never removed from the accounting.

    `total_ink_pixel_count` is conservation's own rescan at `SECONDARY_MARGIN`,
    so it contains every frame pixel; `claimed` is what the act crops cover; and
    the difference is the residual, which `run.py` mints as held acts a reviewer
    opens. The identity `claimed + residual == total` is what makes "nothing was
    lost" a check rather than a claim.
    """
    _designator, context, held = framed_pass
    payload = _conservation_for(context, 1)["payload"]
    total = payload["total_ink_pixel_count"]
    claimed = payload["claimed_pixel_count"]
    residual = payload["residual_pixel_count"]
    assert claimed + residual == total
    # The unframed fixture page counts 11,520 ink pixels and claims every one of
    # them, so its residual is 0 -- that is what the acceptance trees measure on
    # both pages of both scenarios. Framed, conservation's own rescan counts the
    # frame's 10,464 as well, and the identity above is what says none of them
    # left the accounting.
    assert total == 11_520 + 10_464
    # The residual is 7,788 rather than the whole 10,464: the two acts' padded
    # capture rectangles reach into the frame and legitimately claim 2,676 of its
    # pixels. Both halves are asserted rather than the round number, because
    # "the frame is entirely residual" would be a false statement about a page
    # whose crops overlap it, and the claim that matters is that the residual
    # stopped being zero.
    assert residual == 7_788
    assert claimed == total - 7_788
    assert payload["residual_component_count"] >= 1
    assert payload["residual_component_count"] <= payload["max_residual_components"]
    assert held is True, "unclaimed ink withholds a complete exit"


def test_the_unframed_fixture_page_publishes_no_such_block(framed_pass):
    """Page 2 is untouched, and says so by the key being absent rather than null.

    The same reason `dark_distribution` is absent on a page that had none: a key carrying
    an empty list on every ordinary page would move bytes nothing measured
    differently, and every acceptance digest in the repository with it.
    """
    _designator, context, _held = framed_pass
    payload = _conservation_for(context, 2)["payload"]
    assert "page_spanning_components" not in payload
    assert payload["residual_pixel_count"] == 0


def test_the_sealed_bound_is_on_every_structure_status_record(framed_pass):
    """`resolved_thresholds` carries the bound a withheld component was judged by.

    A record naming what was set aside, without naming the number that set it
    aside, could not be checked by anyone reading it later.
    """
    _designator, context, _held = framed_pass
    statuses = _records(context, "structure-status")
    assert statuses
    for record in statuses:
        assert record["payload"]["resolved_thresholds"]["page_spanning_area_bp"] == 5000
