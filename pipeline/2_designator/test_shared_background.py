"""One paper value across three stages, proved on the shape that broke the old one.

`common/test_designator_recensor_ink_calibration.py` pins the two margins as
source literals and proves the three ink sets nest under one background. This
proves the same claim through the *shipped functions on both sides*: the
Designator's own `structure.ink_pixels` and `conservation`-margin scan against
`common.residual_ink.residual_ink`, on a page with a photographed register
opening's shape. A stage may import `common/`; the reverse is what
`pipeline/test_stage_import_boundaries.py` forbids, and it is why this half of
the proof lives here rather than beside the pin.
"""

import grouping_config
import pytest
import structure
from grouping_config import load_grouping_config, resolve_background_policy

from common.residual_ink import (
    MINIMUM_CONTRAST_BELOW_BACKGROUND,
    load_coverage_audit_config,
    page_spanning_components,
    residual_ink,
    resolve_coverage_audit_policy,
)

# The shape, not a photograph: a 200x200 frame of near-black at 5 around a lit
# interior at 210, with three marks on it. What it takes from real material is
# the one property that broke the retired inference -- 20,400 pixels of frame
# against the interior's 19,000 of paper (19,600 less the 600 the three marks
# take), so the page's single most common value is the frame's
# 5, which `common/residual_ink.py` called paper until 2026-09-06. Nothing here
# is a calibration sample; the Designator's 127 real pages are in its own survey.
FRAME = 5
PAPER = 210
DARK_STROKE = 40
MID_STROKE = 160
FAINT_STROKE = 180


def photographed_shaped_page() -> tuple[int, int, list[bytearray]]:
    width = height = 200
    border = 30
    rows = [bytearray([FRAME] * width) for _ in range(height)]
    for y in range(border, height - border):
        for x in range(border, width - border):
            rows[y][x] = PAPER
    for value, top, tall in ((DARK_STROKE, 60, 10), (MID_STROKE, 90, 5), (FAINT_STROKE, 110, 5)):
        for y in range(top, top + tall):
            for x in range(50, 80):
                rows[y][x] = value
    return width, height, rows


def test_all_three_readers_infer_one_paper_value_on_a_photographed_shaped_page():
    """The Designator, the Ink Map and the Recensor's audit, one call, one answer.

    The Ink Map and the Recensor reach the inference through
    `common.residual_ink`; this stage reaches it through `structure`, which
    re-exports it. Asserting the two objects are the same function is what makes
    "they cannot come to disagree" a fact about the code rather than about two
    call sites that currently match.
    """
    import common.background
    import common.residual_ink

    assert structure.infer_background_evidence is common.background.infer_background_evidence
    assert common.residual_ink.infer_background_evidence is (
        common.background.infer_background_evidence
    )

    width, height, rows = photographed_shaped_page()
    policy = resolve_background_policy(load_grouping_config(), width, height)
    evidence = structure.infer_background_evidence(width, height, rows, background_policy=policy)

    assert evidence["background"] == PAPER
    assert evidence["source"] == structure.BACKGROUND_SOURCE_INTERIOR_MODE
    assert evidence["dark_mode"] == FRAME
    # (210 - 5) * 3333 // 10000 = 68, well past the floor of 20.
    assert evidence["ink_margin"] == 68

    audited = residual_ink(
        width,
        height,
        rows,
        [],
        background_policy=policy,
        coverage_policy=resolve_coverage_audit_policy(load_coverage_audit_config(), width, height),
    )
    assert audited["background"]["background_level"] == evidence["background"]
    assert audited["background"]["background_source"] == evidence["source"]
    assert audited["background"]["dark_mode"] == evidence["dark_mode"]
    assert audited["background"]["ink_margin"] == evidence["ink_margin"]
    assert audited["background"]["ink_threshold"] == PAPER - MINIMUM_CONTRAST_BELOW_BACKGROUND


def test_the_audit_sees_every_stroke_the_scan_saw_and_the_stage_still_sees_more():
    """The containment, over the shipped ink sets rather than over thresholds.

    Three nested sets, strictly, and the strictness is what says the claim is
    not an equality that happens to hold today:

    * `structure.ink_pixels` at the page's own derived margin -- what the
      proposing scan counts.
    * The audit's, at its own fixed contrast of 40. It must contain the scan's,
      or a mark the Designator dismissed could be called ink by the audit and
      the disagreement would be in the direction that loses ink.
    * `structure.ink_pixels` at `SECONDARY_MARGIN`, the conservation
      denominator, which must contain the audit's for the same reason one level
      up.
    """
    width, height, rows = photographed_shaped_page()
    policy = resolve_background_policy(load_grouping_config(), width, height)
    evidence = structure.infer_background_evidence(width, height, rows, background_policy=policy)
    background = evidence["background"]

    scanned = structure.ink_pixels(
        width, height, rows, background=background, margin=evidence["ink_margin"]
    )
    reconciled = structure.ink_pixels(
        width, height, rows, background=background, margin=structure.SECONDARY_MARGIN
    )
    audited = structure.ink_pixels(
        width, height, rows, background=background, margin=MINIMUM_CONTRAST_BELOW_BACKGROUND
    )

    # The retired inference on this page: the frame wins the histogram, and 40
    # levels below 5 is below every 8-bit sample, so the audit's set was empty
    # and every containment below was true of nothing.
    histogram = [0] * 256
    for row in rows:
        for value in row:
            histogram[value] += 1
    assert max(range(256), key=lambda value: histogram[value]) == FRAME
    assert FRAME - MINIMUM_CONTRAST_BELOW_BACKGROUND < 0

    assert scanned < audited < reconciled
    assert len(scanned) == 20_700
    assert len(audited) == 20_850
    assert len(reconciled) == 21_000

    strokes = {(x, y) for y in range(60, 70) for x in range(50, 80)}
    assert strokes <= scanned
    assert strokes <= audited

    # And the audit's own counts come from the same set, through the shipped
    # measure rather than through a threshold restated here.
    measured = residual_ink(
        width,
        height,
        rows,
        [],
        background_policy=policy,
        coverage_policy=resolve_coverage_audit_policy(load_coverage_audit_config(), width, height),
    )
    # The audit's WHOLE set, which is what the containment above is about. Its
    # `total_ink_pixels` is the audited pair's denominator -- the same set less
    # this page's page-spanning component, which here is the frame -- and both
    # are on the record so the subtraction is visible rather than silent.
    assert measured["page_ink_pixels"] == len(audited)
    assert measured["page_spanning_ink_pixels"] == 20_400
    assert measured["total_ink_pixels"] == len(audited) - 20_400


def test_a_page_the_inference_refuses_is_refused_by_the_audit_too():
    """One refusal, one name, whichever stage asks.

    The inverted scan `test_structure.py` uses: 80% of the page at 30 and 20% at
    220, whose mode is darker than its own mean and whose interior is dark, so
    no branch can call anything on it paper. The Designator refuses it and
    records `ink_measurable: false`; the audit must refuse it by the same
    exception rather than measure zero residual ink on it, which is what a
    coverage proof taken under a divider that is not paper would be.
    """
    width = height = 100
    rows = [bytearray([30] * width) for _ in range(height)]
    for y in range(80, height):
        rows[y] = bytearray([220] * width)
    policy = resolve_background_policy(load_grouping_config(), width, height)

    with pytest.raises(structure.BackgroundInferenceRefusal):
        structure.infer_background_evidence(width, height, rows, background_policy=policy)
    with pytest.raises(structure.BackgroundInferenceRefusal):
        residual_ink(
            width,
            height,
            rows,
            [],
            background_policy=policy,
            coverage_policy=resolve_coverage_audit_policy(
                load_coverage_audit_config(), width, height
            ),
        )


def test_the_frame_is_withheld_from_grouping_and_still_counted_by_conservation():
    """The whole of the 2026-09-06 grouping unit, on one page, through shipped functions.

    The same photographed-shaped page above, driven through
    `structure.primary_scan`, `grouping.partition_page_spanning`,
    `grouping.group_page` and `conservation.reconcile` in the order `run.py`
    drives them. Both halves of the claim are asserted here because either one
    alone would be a different and wrong change:

      * the frame does not group -- it labels as exactly ONE component whose
        bounding box is the page, it is the one component withheld, and the only
        group that comes back is the mark, so nothing on this page is claimed by
        a rectangle the size of the leaf;
      * the frame is still ink -- every one of its pixels is inside
        `total_ink_pixel_count`, and because no group claims it, it is inside
        `residual_pixel_count`, which `run.py` mints as held acts a reviewer
        opens. Nothing was removed from the page, from the scan, or from the
        accounting.

    The residual is what makes the coverage audit non-vacuous again: before this
    rule the single group's bounds were the page, so claimed was the whole ink
    set and the residual was zero on every real page measured -- an audit whose
    denominator, "ink outside declared coverage", was empty by construction.
    """
    import conservation
    import grouping
    from grouping_config import resolve_thresholds

    width, height, rows = photographed_shaped_page()
    config = load_grouping_config()
    policy = resolve_background_policy(config, width, height)
    thresholds = resolve_thresholds(config, width, height)
    evidence = structure.infer_background_evidence(width, height, rows, background_policy=policy)

    components = structure.primary_scan(
        width,
        height,
        rows,
        background=evidence["background"],
        margin=evidence["ink_margin"],
        gap_tolerance_px=thresholds.gap_tolerance_px,
    )
    # The frame is one component and its bounding box is the leaf. This is the
    # property the whole rule rests on and it is measured here rather than
    # assumed: on all fourteen real pages the survey measured, the same thing
    # was true and the component held 69.5 to 95.2 percent of the counted ink.
    assert [component["bounds"] for component in components] == [
        {"x": 0, "y": 0, "w": width, "h": height},
        {"x": 50, "y": 60, "w": 30, "h": 10},
    ]
    frame, mark = components
    assert frame["pixel_count"] == 20_400
    assert mark["pixel_count"] == 300

    grouped, withheld = grouping.partition_page_spanning(
        components, width, height, page_spanning_area_bp=thresholds.page_spanning_area_bp
    )
    assert grouped == [mark]
    assert withheld == [frame]

    groups = grouping.group_page(
        components,
        width,
        height,
        margin_px=thresholds.margin_px,
        chain_gap_px=thresholds.chain_gap_px,
        anchor_reach_px=thresholds.anchor_reach_px,
        brace_min_height_px=thresholds.brace_min_height_px,
        page_spanning_area_bp=thresholds.page_spanning_area_bp,
    )
    assert [group["bounds"] for group in groups] == [{"x": 50, "y": 60, "w": 30, "h": 10}]

    result = conservation.reconcile(
        width,
        height,
        rows,
        background=evidence["background"],
        claimed_bounds=[group["bounds"] for group in groups],
        gap_tolerance_px=thresholds.gap_tolerance_px,
        review_priority_min_dimension_px=thresholds.review_priority_min_dimension_px,
    )
    # Conservation rescans at `SECONDARY_MARGIN`, so its total is the 20,700 the
    # primary scan counted plus the 300 of fainter stroke only it sees.
    assert result["total_ink_pixel_count"] == 21_000
    assert result["claimed_pixel_count"] == 300
    assert result["residual_pixel_count"] == 20_700
    assert (
        result["claimed_pixel_count"] + result["residual_pixel_count"]
        == (result["total_ink_pixel_count"])
    )
    # Every withheld pixel is inside the residual: nothing left the accounting.
    assert result["residual_pixel_count"] >= frame["pixel_count"]
    assert len(result["residual_components"]) == 3
    assert len(result["residual_components"]) <= config["max_residual_components"]


def test_the_audit_withholds_exactly_the_component_the_grouping_pass_withholds():
    """The load-bearing claim of the outside-coverage exclusion, on both sides.

    The audit takes this page's page-spanning component out of both its counts,
    and it must be the *same* component `grouping.partition_page_spanning`
    withheld from column assignment and body chaining -- otherwise the audit is
    setting aside ink nobody is holding, which is a missed act reported as a
    clean page.

    It is the same by construction rather than by agreement: both sides label the
    same bytes at the margin this page derives for itself, under one sealed
    `gap_tolerance_px` and one sealed `page_spanning_area_bp`, through the one
    labeller in `common/components.py`. This asserts it over the components
    themselves, so a later change that gave either side its own threshold fails
    here rather than in a run.
    """
    import grouping

    width, height, rows = photographed_shaped_page()
    config = load_grouping_config()
    policy = resolve_background_policy(config, width, height)
    thresholds = grouping_config.resolve_thresholds(config, width, height)
    evidence = structure.infer_background_evidence(width, height, rows, background_policy=policy)

    components = structure.primary_scan(
        width,
        height,
        rows,
        background=evidence["background"],
        margin=evidence["ink_margin"],
        gap_tolerance_px=thresholds.gap_tolerance_px,
    )
    _kept, withheld = grouping.partition_page_spanning(
        components, width, height, page_spanning_area_bp=thresholds.page_spanning_area_bp
    )

    found, mask = page_spanning_components(
        width,
        height,
        rows,
        background_evidence={
            "background_level": evidence["background"],
            "ink_margin": evidence["ink_margin"],
        },
        coverage_policy=resolve_coverage_audit_policy(load_coverage_audit_config(), width, height),
    )

    assert [component["bounds"] for component in found] == [
        component["bounds"] for component in withheld
    ]
    assert [component["pixel_count"] for component in found] == [
        component["pixel_count"] for component in withheld
    ]
    # Non-vacuous: this page really does have one, and it is the frame.
    assert len(found) == 1
    assert found[0]["bounds"] == {"x": 0, "y": 0, "w": width, "h": height}
    assert sum(mask) == found[0]["pixel_count"] == 20_400

    # And what the audit publishes is that component and nothing else.
    finding = residual_ink(
        width,
        height,
        rows,
        [],
        background_policy=policy,
        coverage_policy=resolve_coverage_audit_policy(load_coverage_audit_config(), width, height),
    )
    assert finding["page_spanning_components"] == [found[0]["bounds"]]
    assert finding["page_spanning_ink_pixels"] == 20_400
    assert (
        finding["page_ink_pixels"] - finding["page_spanning_ink_pixels"]
        == (finding["total_ink_pixels"])
    )
