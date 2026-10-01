"""Pins for this project's ink-calibration constants.

The first pins `PRIMARY_MARGIN` as a source literal: the sealed `max_ink_bp`
was measured at that level. The others check that the audit's contrast and the
derived scan sit below one shared background, so their ink sets actually nest
on a photographed-shaped page rather than holding over nothing.
"""

import ast
from pathlib import Path

from common.background import (
    PRIMARY_MARGIN,
    infer_background_evidence,
    load_background_config,
    resolve_background_policy,
)
from common.residual_ink import (
    MINIMUM_CONTRAST_BELOW_BACKGROUND,
    load_coverage_audit_config,
    residual_ink,
    resolve_coverage_audit_policy,
)

ROOT = Path(__file__).resolve().parents[1]


def _literal_constant(path: Path, name: str) -> int:
    """Read one declared numeric constant without importing its module."""
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    for node in tree.body:
        target = node.target if isinstance(node, ast.AnnAssign) else None
        value_node = node.value if isinstance(node, ast.AnnAssign) else None
        if isinstance(node, ast.Assign) and len(node.targets) == 1:
            target, value_node = node.targets[0], node.value
        if isinstance(target, ast.Name) and target.id == name:
            value = ast.literal_eval(value_node)
            assert isinstance(value, int)
            assert not isinstance(value, bool)
            return value
    raise AssertionError(f"{path} does not declare {name}")


def test_the_sealed_ink_bound_still_sits_at_the_level_it_was_measured_at():
    """`max_ink_bp = 7000` was placed in a gap measured at `PRIMARY_MARGIN = 20`.

    `_settle_background_evidence` probes at that constant, and the whole-page
    ink distribution the bound sits in was measured there and nowhere else
    (`config/ink_map.toml`'s `[background]` provenance). Move the level and
    every one of those numbers is about a different statistic, while the bound
    goes on refusing pages for a reason nobody re-measured. A sealed field
    would be a second home for one number whose only correct setting is the
    constant's, so the constant is pinned here instead.
    """
    probe_level = _literal_constant(ROOT / "common" / "background.py", "PRIMARY_MARGIN")
    assert probe_level == 20, (
        "config/ink_map.toml's max_ink_bp = 7000 was placed in a gap measured with "
        "the probe at this level; re-measure that bound over the 127-page calibration "
        "before changing it"
    )


def photographed_shaped_page() -> tuple[int, int, list[bytearray]]:
    """A SYNTHETIC page with the shape a photographed register opening has.

    Not a photograph and not claimed to be one: a 200x200 frame of near-black at
    5, a lit interior of paper at 210, and marks on it. What it reproduces from
    real material is that **the surround wins the histogram**: 20,400 pixels of
    frame against the interior's 19,000 of paper, so the page's single most
    common value is 5.

    * **40** -- below both thresholds: ink to the derived scan and to the audit.
    * **160** -- below the audit's 170 and above the derived scan's 142: ink to
      the audit and not to the scan, the direction the audit's whole purpose
      depends on.
    * **180** -- above both: ink to neither.
    """
    width = height = 200
    frame = 30
    rows = [bytearray([5] * width) for _ in range(height)]
    for y in range(frame, height - frame):
        for x in range(frame, width - frame):
            rows[y][x] = 210
    for value, top in ((40, 60), (160, 90), (180, 110)):
        for y in range(top, top + (10 if value == 40 else 5)):
            for x in range(50, 80):
                rows[y][x] = value
    return width, height, rows


def _ink_at(rows: list[bytearray], threshold: int) -> set[tuple[int, int]]:
    return {
        (x, y) for y, row in enumerate(rows) for x, value in enumerate(row) if value <= threshold
    }


def test_the_containment_is_not_vacuous_on_a_photographed_page():
    """One background, two margins, and two ink sets that actually nest.

    Taken below the raw histogram mode, the audit's 40-level contrast is below
    every 8-bit sample on this page and its ink set is empty. Below the shared
    inference's paper value, under the shipped sealed policy, the audit sees
    every stroke the derived scan sees, and one more.
    """
    width, height, rows = photographed_shaped_page()
    policy = resolve_background_policy(load_background_config(), width, height)
    evidence = infer_background_evidence(width, height, rows, background_policy=policy)

    histogram = [0] * 256
    for row in rows:
        for value in row:
            histogram[value] += 1
    raw_mode = max(range(256), key=lambda value: histogram[value])
    assert raw_mode == 5
    assert raw_mode - MINIMUM_CONTRAST_BELOW_BACKGROUND < 0

    assert evidence["background"] == 210
    assert evidence["source"] == "inferred-interior-mode"
    assert evidence["ink_margin"] == 68

    background = evidence["background"]
    primary = _ink_at(rows, background - evidence["ink_margin"])
    audited = _ink_at(rows, background - MINIMUM_CONTRAST_BELOW_BACKGROUND)

    assert len(audited) == 20_850
    assert primary < audited
    assert len(primary) == 20_700
    strokes = {(x, y) for y in range(60, 70) for x in range(50, 80)}
    assert strokes <= primary
    assert strokes <= audited

    # Through the shipped audit rather than a threshold restated here: coverage
    # is empty, so every ink pixel is outside it, and the page flags.
    finding = residual_ink(
        width,
        height,
        rows,
        [],
        background_policy=policy,
        coverage_policy=resolve_coverage_audit_policy(load_coverage_audit_config(), width, height),
    )
    assert finding["background"]["background_level"] == background
    assert finding["background"]["contrast_below_background"] == MINIMUM_CONTRAST_BELOW_BACKGROUND
    assert finding["background"]["ink_threshold"] == background - MINIMUM_CONTRAST_BELOW_BACKGROUND
    # `page_ink_pixels` is the audit's whole set; `total_ink_pixels` is the same
    # set with the page-spanning component (here the bezel) taken out.
    assert finding["page_ink_pixels"] == len(audited)
    assert finding["page_spanning_ink_pixels"] == 20_400
    assert finding["page_spanning_components"] == [{"x": 0, "y": 0, "w": width, "h": height}]
    assert finding["total_ink_pixels"] == len(audited) - 20_400 == 450
    assert finding["outside_ink_pixels"] == 450
    assert finding["flagged"] is True


def test_the_probe_level_is_below_the_audits_own_contrast():
    """`PRIMARY_MARGIN` floors every derived margin; the audit's 40 sits above it,
    so the audit's threshold is the stricter of the two below one background."""
    assert PRIMARY_MARGIN < MINIMUM_CONTRAST_BELOW_BACKGROUND
