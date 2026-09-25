"""Ink connected-component scanning for the structure pass.

Background inference (the threshold this scan runs against) lives in
`common/background.py`, shared with the Ink Map and the Recensor's residual-ink
audit so all three infer paper from the same page bytes; those names are
re-exported below. This stage keeps only the ink set, the labeller aliases, the
two sensitivity presets and the two scans.

There is no model here: this is a real, deterministic, visual connected-component
pass over the decoded page, not the textual structural classification a model
would add.

`primary_scan` runs at each page's own derived `ink_margin`, not at
`PRIMARY_MARGIN` -- that constant is only the floor under the derivation
(`structure_pass.py:810`). `SECONDARY_MARGIN` backs `secondary_scan` at a
fixed, lower margin so a real secondary detector has a principled difference
to report: the secondary scan is strictly more sensitive, so it adds recall
without ever missing what the primary catches, and `conservation.reconcile`
uses it as the residual-ink denominator.

Connectivity tolerates a small gap rather than requiring strict pixel adjacency:
real ink is not a solid fill, so two ink pixels within `gap_tolerance_px` of each
other are treated as one component. This is an ordinary morphological "close"
before labelling.

`label_components` is a fast row-run labeller; `_label_components_reference`
(the retired per-pixel implementation) is kept as the independent oracle both
this module's and `conservation.py`'s labelling are checked against.
"""

from typing import Final

from common.background import (  # noqa: F401  (re-exported: see the note above)
    BACKGROUND_SOURCE_INTERIOR_MODE,
    BACKGROUND_SOURCE_MODAL,
    BASIS_POINTS,
    PRIMARY_MARGIN,
    BackgroundEvidence,
    BackgroundInferenceRefusal,
    BackgroundPolicy,
    DarkDistributionEvidence,
    _dark_distribution,
    _derived_ink_margin,
    _ink_threshold,
    _settle_background_evidence,
    infer_background,
    infer_background_evidence,
)
from common.components import (  # noqa: F401  (re-exported: see the note above)
    Component,
    ink_runs_by_row,
    label_component_runs,
    label_components,
    label_components_reference,
)
from common.contracts.errors import ContractError

# Deliberately not derived or configured: a fixed 2 below background is
# smaller than any derived margin, so `secondary_scan` is guaranteed strictly
# more sensitive than `primary_scan` on every page, never the reverse. A
# derived value could invert that on some page, trading a visible over-count
# for a possible silent loss. Read as a literal by an AST test, so it must
# stay one.
SECONDARY_MARGIN: Final = 2

# No module default: this is the one threshold that cannot honestly scale by
# page dimension, so `run.py` must resolve it from the sealed config field and
# pass it explicitly on every call.


def ink_pixels(width: int, height: int, rows: list, *, background: int, margin: int) -> set:
    """Every pixel at or below the ink threshold, as a set of (x, y) pairs.

    Split out from `scan_ink_components` for callers that need the raw ink set
    rather than whole components, which may straddle a crop's edge.

    Still materialises one tuple per ink pixel, the remaining share of the
    383s/2.17GB measured before the run-based labeller replaced the rest
    (`common/components.py:186`, `test_page_residual_bound.py:397-400`).
    """
    if width <= 0 or height <= 0:
        raise ContractError(f"a {width}x{height} page has no pixels to scan")
    if len(rows) != height:
        raise ContractError(f"expected {height} scanlines, got {len(rows)}")
    threshold = _ink_threshold(background, margin)

    ink: set[tuple[int, int]] = set()
    for y in range(height):
        row = rows[y]
        if len(row) != width:
            raise ContractError(f"scanline {y} has width {len(row)}, expected {width}")
        for x in range(width):
            if row[x] <= threshold:
                ink.add((x, y))
    return ink


# The labeller lives in common/components.py (common/ cannot import a stage);
# these aliases keep this stage's own call sites and tests unchanged.
_label_components_reference = label_components_reference
_ink_runs_by_row = ink_runs_by_row


def scan_ink_components(
    width: int,
    height: int,
    rows: list,
    *,
    background: int,
    margin: int,
    gap_tolerance_px: int,
) -> list[Component]:
    """Every ink-bearing connected component on a decoded grayscale page.

    `rows` is exactly what `common.imaging.decode_grayscale_png` returns: one
    bytearray per scanline, one byte per pixel. Components are returned sorted
    by (top, left) so the result is deterministic and independent of set/dict
    iteration order -- this is a structure pass, and its output feeds identity
    derivation downstream, so an unordered result would make a rerun's
    numbering a coin flip.
    """
    pixels = ink_pixels(width, height, rows, background=background, margin=margin)
    return label_components(pixels, gap_tolerance_px=gap_tolerance_px)


def primary_scan(
    width: int, height: int, rows: list, *, background: int, margin: int, gap_tolerance_px: int
) -> list[Component]:
    """The primary proposer's scan, at the margin this page derived for itself.

    `margin` has no default: a caller must pass the value
    `infer_background_evidence` published as `ink_margin`, so no page runs
    under a threshold nobody resolved.
    """
    return scan_ink_components(
        width,
        height,
        rows,
        background=background,
        margin=margin,
        gap_tolerance_px=gap_tolerance_px,
    )


def secondary_scan(
    width: int, height: int, rows: list, *, background: int, gap_tolerance_px: int
) -> list[Component]:
    return scan_ink_components(
        width,
        height,
        rows,
        background=background,
        margin=SECONDARY_MARGIN,
        gap_tolerance_px=gap_tolerance_px,
    )
