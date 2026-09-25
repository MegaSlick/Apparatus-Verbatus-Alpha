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
oracle this one is checked against. Connectivity labelling here (`_components`)
is separate from `structure.label_components` because the two work over
different objects -- runs here, pixels there -- and that same oracle holds
both to one meaning of "connected".
"""

from __future__ import annotations

import heapq
from collections import defaultdict
from functools import cmp_to_key
from typing import Iterator, TypedDict

import geometry
from structure import SECONDARY_MARGIN, _ink_threshold

from common.contracts.errors import ContractError


class ReconciliationResult(TypedDict):
    total_ink_pixel_count: int
    claimed_pixel_count: int
    residual_pixel_count: int
    residual_components: list[dict]


# No module default for review_priority_min_dimension_px: run.py resolves it
# per page from sealed config and must pass it explicitly.


class _Run(TypedDict):
    """One horizontal stretch of contiguous ink on scanline `y`, half-open at `x1`.

    `x1 - x0 == ink_count` always; the two are carried separately because
    `_components` reads the span while the accounting reads the count. Tolerated
    blank gaps are bridged in `_components`' connectivity, never inside a run.
    """

    x0: int
    x1: int
    ink_count: int
    y: int


def _plain_int(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _unit_ink_runs(row: object, threshold: int, y: int) -> list[_Run]:
    """Exact contiguous ink runs: one run per unbroken stretch, a single blank
    pixel ends it. Gap tolerance is deliberately not applied here -- it belongs
    to `_components`, which decides *connected*, not *ink* -- or a tolerated
    gap would count as ink `_subtract_claims` charges a crop for.
    """
    if not isinstance(row, (bytes, bytearray)):
        raise ContractError(f"scanline {y} is not grayscale bytes")
    runs: list[_Run] = []
    start: int | None = None
    for x, value in enumerate(row):
        if value <= threshold:
            if start is None:
                start = x
        elif start is not None:
            runs.append({"x0": start, "x1": x, "ink_count": x - start, "y": y})
            start = None
    if start is not None:
        end = len(row)
        runs.append({"x0": start, "x1": end, "ink_count": end - start, "y": y})
    return runs


def _merged_claim_intervals(active: list[geometry.Bounds]) -> list[tuple[int, int]]:
    intervals = sorted((bounds["x"], bounds["x"] + bounds["w"]) for bounds in active)
    merged: list[tuple[int, int]] = []
    for start, end in intervals:
        if merged and start <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], end))
        else:
            merged.append((start, end))
    return merged


def _subtract_claims(run: _Run, claims: list[tuple[int, int]]) -> tuple[int, list[_Run]]:
    """Split an ink run around claims, counting actual ink rather than area.

    `claims` must arrive merged and sorted (`_merged_claim_intervals`); overlapping
    claims would otherwise be counted twice.
    """
    residual: list[_Run] = []
    claimed = 0
    cursor = run["x0"]
    for start, end in claims:
        if end <= cursor:
            continue
        if start >= run["x1"]:
            break
        if cursor < start:
            residual.append(
                {
                    "x0": cursor,
                    "x1": min(start, run["x1"]),
                    "ink_count": min(start, run["x1"]) - cursor,
                    "y": run["y"],
                }
            )
        covered_start, covered_end = max(cursor, start), min(run["x1"], end)
        if covered_start < covered_end:
            claimed += covered_end - covered_start
            cursor = covered_end
    if cursor < run["x1"]:
        residual.append(
            {"x0": cursor, "x1": run["x1"], "ink_count": run["x1"] - cursor, "y": run["y"]}
        )
    return claimed, residual


def _components(runs: list[_Run], gap: int) -> list[dict]:
    """Label residual runs with the legacy Chebyshev gap rule, without pixels."""
    if not runs:
        return []
    parent = list(range(len(runs)))

    def find(index: int) -> int:
        while parent[index] != index:
            parent[index] = parent[parent[index]]
            index = parent[index]
        return index

    def union(left: int, right: int) -> None:
        left_root, right_root = find(left), find(right)
        if left_root != right_root:
            parent[right_root] = left_root

    by_row: dict[int, list[int]] = defaultdict(list)
    for index, run in enumerate(runs):
        by_row[run["y"]].append(index)
    radius = gap + 1
    for y in sorted(by_row):
        current = by_row[y]
        # Claim-splitting can leave several residual runs on one scanline;
        # rejoin adjacent ones within the tolerated gap before looking back.
        for left, right in (
            (current[index], current[index + 1]) for index in range(len(current) - 1)
        ):
            if runs[right]["x0"] - runs[left]["x1"] <= gap:
                union(left, right)
        for previous_y in range(max(0, y - radius), y):
            previous = by_row.get(previous_y)
            if not previous:
                continue
            # Runs on a row are disjoint and strictly left-to-right, so one
            # forward pointer per row pair replaces the full cross product:
            # once a previous-row run passes `left`'s right edge (+radius), no
            # later `left` needs to look further back than that point either.
            start = 0
            for left in current:
                while (
                    start < len(previous)
                    and runs[previous[start]]["x1"] + radius <= runs[left]["x0"]
                ):
                    start += 1
                for offset in range(start, len(previous)):
                    right = previous[offset]
                    if runs[right]["x0"] >= runs[left]["x1"] + radius:
                        break
                    union(left, right)
    groups: dict[int, list[_Run]] = defaultdict(list)
    for index, run in enumerate(runs):
        groups[find(index)].append(run)
    entries = []
    for group in groups.values():
        x0 = min(run["x0"] for run in group)
        x1 = max(run["x1"] for run in group)
        y0 = min(run["y"] for run in group)
        y1 = max(run["y"] for run in group) + 1
        entries.append(
            (
                {
                    "bounds": {"x": x0, "y": y0, "w": x1 - x0, "h": y1 - y0},
                    "pixel_count": sum(run["ink_count"] for run in group),
                },
                group,
            )
        )

    # Published order must not depend on union-find insertion order: components
    # sharing a (top, left) origin are broken by comparing their ink itself,
    # over a lazily merged run stream so memory stays proportional to runs.
    def origin(entry: tuple[dict, list[_Run]]) -> tuple[int, int]:
        return (entry[0]["bounds"]["y"], entry[0]["bounds"]["x"])

    entries.sort(key=origin)
    ordered: list[dict] = []
    span_start = 0
    for index in range(1, len(entries) + 1):
        if index == len(entries) or origin(entries[index]) != origin(entries[span_start]):
            span = entries[span_start:index]
            if len(span) > 1:
                span.sort(key=cmp_to_key(_compare_ink_streams))
            ordered.extend(component for component, _group in span)
            span_start = index
    return ordered


def _pixel_stream(group: list[_Run]) -> Iterator[tuple[int, int]]:
    """A tied component's ink in sorted (x, y) order, lazily, via a heap merge
    of its runs (each run already yields ascending x for a fixed y).
    """
    return heapq.merge(*(((x, run["y"]) for x in range(run["x0"], run["x1"])) for run in group))


def _compare_ink_streams(left: tuple[dict, list[_Run]], right: tuple[dict, list[_Run]]) -> int:
    """Lexicographic sorted-pixel comparison without materialising either side."""
    for left_pixel, right_pixel in zip(
        _pixel_stream(left[1]), _pixel_stream(right[1]), strict=False
    ):
        if left_pixel != right_pixel:
            return -1 if left_pixel < right_pixel else 1
    return left[0]["pixel_count"] - right[0]["pixel_count"]


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
    if not _plain_int(width) or not _plain_int(height) or width <= 0 or height <= 0:
        raise ContractError(f"a {width}x{height} page has no pixels to scan")
    if len(rows) != height:
        raise ContractError(f"expected {height} scanlines, got {len(rows)}")
    if not _plain_int(gap_tolerance_px) or gap_tolerance_px < 0:
        raise ContractError(f"gap tolerance {gap_tolerance_px} is negative")
    if not _plain_int(review_priority_min_dimension_px) or review_priority_min_dimension_px < 0:
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
    residual_runs: list[_Run] = []
    for y, row in enumerate(rows):
        if len(row) != width:
            raise ContractError(f"scanline {y} has width {len(row)}, expected {width}")
        opening, closing = starts.get(y), ends.get(y)
        if opening or closing:
            for bounds in closing or ():
                active.remove(bounds)
            active.extend(opening or ())
            intervals = _merged_claim_intervals(active)
        for run in _unit_ink_runs(row, threshold, y):
            total += run["ink_count"]
            covered, residual = _subtract_claims(run, intervals)
            claimed += covered
            residual_runs.extend(residual)
    components = _components(residual_runs, gap_tolerance_px)
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
