"""One paper value across three stages, proved on the shape that broke the old one.

`common/test_designator_recensor_ink_calibration.py` pins the two margins as
literals; this proves the same claim through the shipped functions on both
sides -- `structure.ink_pixels`/`conservation` against
`common.residual_ink.residual_ink` -- on a page shaped like a photographed
register opening. Lives here, not beside the pin, because a stage may import
`common/` but not the reverse.
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

# A 200x200 frame of near-black around a lit interior with three marks: the
# frame's 20,400 pixels outnumber the interior's paper, so the page's single
# most common value is the frame's, not the paper the shared inference finds.
# Nothing here is a calibration sample; the Designator's 127 real pages are in
# its own survey.
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

    Asserting the two objects are the same function makes "they cannot
    disagree" a fact about the code, not just two call sites that happen to match.
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
    """Three strictly nested ink sets, over the shipped functions, not thresholds:
    the proposing scan's own margin, the audit's fixed contrast of 40 (must
    contain the scan's, or the audit could lose ink the scan found), and the
    conservation denominator at SECONDARY_MARGIN (must contain the audit's).
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

    # The retired inference: 40 levels below the frame's 5 is below every 8-bit
    # sample, so its audit set was empty and every containment below was vacuous.
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

    measured = residual_ink(
        width,
        height,
        rows,
        [],
        background_policy=policy,
        coverage_policy=resolve_coverage_audit_policy(load_coverage_audit_config(), width, height),
    )
    # total_ink_pixels is the audited set less the page-spanning frame; both
    # numbers are on the record, so the subtraction is visible, not silent.
    assert measured["page_ink_pixels"] == len(audited)
    assert measured["page_spanning_ink_pixels"] == 20_400
    assert measured["total_ink_pixels"] == len(audited) - 20_400


def test_a_page_the_inference_refuses_is_refused_by_the_audit_too():
    """One refusal, one name, whichever stage asks: an inverted page whose mode
    is darker than its own mean must refuse rather than measure zero residual
    ink under a divider that isn't paper.
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
    """The whole grouping unit, through shipped functions, in run.py's order.

    Both halves matter: the frame doesn't group (it's the one component
    withheld, so only the mark's group comes back) and the frame is still ink
    (every pixel stays in total_ink_pixel_count, and since no group claims it,
    in residual_pixel_count too). Before this rule the residual was zero on
    every real page, making the coverage audit's denominator vacuous.
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
    # The frame is one component and its bounding box is the leaf -- the
    # property the whole rule rests on, measured rather than assumed.
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
    # Conservation rescans at SECONDARY_MARGIN: 20,700 primary-scan pixels plus
    # 300 of fainter stroke only it sees.
    assert result["total_ink_pixel_count"] == 21_000
    assert result["claimed_pixel_count"] == 300
    assert result["residual_pixel_count"] == 20_700
    assert (
        result["claimed_pixel_count"] + result["residual_pixel_count"]
        == (result["total_ink_pixel_count"])
    )
    assert result["residual_pixel_count"] >= frame["pixel_count"]
    assert len(result["residual_components"]) == 3
    assert len(result["residual_components"]) <= config["max_residual_components"]


def test_the_audit_withholds_exactly_the_component_the_grouping_pass_withholds():
    """The audit's page-spanning exclusion must be the *same* component
    `grouping.partition_page_spanning` withholds, by construction (one margin,
    one sealed threshold, one labeller) rather than by coincidence.
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
    assert len(found) == 1
    assert found[0]["bounds"] == {"x": 0, "y": 0, "w": width, "h": height}
    assert sum(mask) == found[0]["pixel_count"] == 20_400

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
