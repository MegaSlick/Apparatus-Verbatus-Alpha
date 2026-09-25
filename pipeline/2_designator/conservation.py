"""Independent, row-oriented residual-ink reconciliation.

Conservation: claimed ink plus residual ink equals every ink pixel found.

The denominator is a fresh scan of the page's own pixels, never the structure
pass's proposals: a mark the grouping pass never emitted a region for still has
somewhere to be counted, as a residual component, rather than vanishing with no
denominator to be missing from.

`review_priority` only orders which residual a reviewer sees first; every
residual is accounted for regardless of size, and removing the priority
threshold would reorder review, never drop a region.

This stage conserves ink at `background - SECONDARY_MARGIN` (2 levels), far
more sensitive than the Recensor's independent page-coverage audit
(`MINIMUM_CONTRAST_BELOW_BACKGROUND`, 40 levels). The asymmetry is safe in one
direction only, and that direction is the invariant: every pixel the Recensor
calls ink is ink to this stage too, so its audit can never flag ink this
accounting silently missed. `common/test_designator_recensor_ink_calibration.py`
pins the containment; narrowing this margin past the Recensor's must be a
deliberate two-sided change.

Ink is tracked as runs and active claimed rectangles rather than per-pixel
sets, keeping memory O(page pixels + ink runs) instead of O(ink pixels); the
retired pixel-set implementation is kept in `test_conservation.py` as the
oracle this one is checked against. Residual runs are labelled by
`common.components.label_component_runs`, the one meaning of "connected".
"""

from __future__ import annotations

from collections import defaultdict
from typing import TypedDict

import geometry
from structure import SECONDARY_MARGIN, _ink_threshold

from common.components import label_component_runs, runs_in_row
from common.contracts.canonical import is_plain_int
from common.contracts.errors import ContractError


class ReconciliationResult(TypedDict):
    total_ink_pixel_count: int
    claimed_pixel_count: int
    residual_pixel_count: int
    residual_components: list[dict]


# No module default for review_priority_min_dimension_px: run.py resolves it
# per page from sealed config and must pass it explicitly.


def _merged_claim_intervals(active: list[geometry.Bounds]) -> list[tuple[int, int]]:
    intervals = sorted((bounds["x"], bounds["x"] + bounds["w"]) for bounds in active)
    merged: list[tuple[int, int]] = []
    for start, end in intervals:
        if merged and start <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], end))
        else:
            merged.append((start, end))
    return merged


def _subtract_claims(
    run: tuple[int, int], claims: list[tuple[int, int]]
) -> tuple[int, list[tuple[int, int]]]:
    """Split an ink run around claims, counting actual ink rather than area.

    `claims` must arrive merged and sorted (`_merged_claim_intervals`); overlapping
    claims would otherwise be counted twice.
    """
    x0, x1 = run
    residual: list[tuple[int, int]] = []
    claimed = 0
    cursor = x0
    for start, end in claims:
        if end <= cursor:
            continue
        if start >= x1:
            break
        if cursor < start:
            residual.append((cursor, min(start, x1)))
        covered_start, covered_end = max(cursor, start), min(x1, end)
        if covered_start < covered_end:
            claimed += covered_end - covered_start
            cursor = covered_end
    if cursor < x1:
        residual.append((cursor, x1))
    return claimed, residual


def reconcile(
    width: int,
    height: int,
    rows: list,
    *,
    background: int,
    claimed_bounds: list[geometry.Bounds],
    margin: int = SECONDARY_MARGIN,
    gap_tolerance_px: int,
    review_priority_min_dimension_px: int,
) -> ReconciliationResult:
    """Reconcile one page's ink against the crops actually cut on it.

    `claimed_bounds` must be the final, padded crop rectangles -- not the
    structure pass's raw proposals -- since padding may already cover ink the
    raw rectangle didn't. The final check is not
    `claimed + residual == total` (true by construction) but the independent
    one: that the published residual components sum back to the residual ink
    counted, so no residual pixel is missing from a region a reviewer can see.
    """
    if not is_plain_int(width) or not is_plain_int(height) or width <= 0 or height <= 0:
        raise ContractError(f"a {width}x{height} page has no pixels to scan")
    if len(rows) != height:
        raise ContractError(f"expected {height} scanlines, got {len(rows)}")
    if not is_plain_int(gap_tolerance_px) or gap_tolerance_px < 0:
        raise ContractError(f"gap tolerance {gap_tolerance_px} is negative")
    if not is_plain_int(review_priority_min_dimension_px) or review_priority_min_dimension_px < 0:
        raise ContractError(
            f"review priority threshold {review_priority_min_dimension_px} is negative"
        )
    for bounds in claimed_bounds:
        geometry.validate_bounds(bounds, width, height, "claimed bounds")
    threshold = _ink_threshold(background, margin)
    starts: dict[int, list[geometry.Bounds]] = defaultdict(list)
    ends: dict[int, list[geometry.Bounds]] = defaultdict(list)
    for bounds in claimed_bounds:
        starts[bounds["y"]].append(bounds)
        ends[bounds["y"] + bounds["h"]].append(bounds)
    active: list[geometry.Bounds] = []
    intervals: list[tuple[int, int]] = []
    total = claimed = 0
    residual_by_row: dict[int, list[tuple[int, int]]] = {}
    ink = bytes(value <= threshold for value in range(256))
    for y, row in enumerate(rows):
        if not isinstance(row, (bytes, bytearray)):
            raise ContractError(f"scanline {y} is not grayscale bytes")
        if len(row) != width:
            raise ContractError(f"scanline {y} has width {len(row)}, expected {width}")
        opening, closing = starts.get(y), ends.get(y)
        if opening or closing:
            for bounds in closing or ():
                active.remove(bounds)
            active.extend(opening or ())
            intervals = _merged_claim_intervals(active)
        residual_row: list[tuple[int, int]] = []
        for run in runs_in_row(row.translate(ink)):
            total += run[1] - run[0]
            covered, residual = _subtract_claims(run, intervals)
            claimed += covered
            residual_row.extend(residual)
        if residual_row:
            residual_by_row[y] = residual_row
    components = [
        component
        for component, _runs in label_component_runs(
            residual_by_row, gap_tolerance_px=gap_tolerance_px
        )
    ]
    accounted = []
    for component in components:
        bounds = component["bounds"]
        accounted.append(
            {
                **component,
                "review_priority": "high"
                if max(bounds["w"], bounds["h"]) >= review_priority_min_dimension_px
                else "low",
            }
        )
    residual_count = total - claimed
    if sum(component["pixel_count"] for component in accounted) != residual_count:
        raise ContractError("row-oriented residual components do not reconcile to residual ink")
    return {
        "total_ink_pixel_count": total,
        "claimed_pixel_count": claimed,
        "residual_pixel_count": residual_count,
        "residual_components": accounted,
    }
