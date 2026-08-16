"""Independent, row-oriented residual-ink reconciliation (U13).

The original skeleton built three Python ``set[(x, y)]`` values.  That made the
proof easy to read, but a dense 600dpi parish page turns every ink pixel into
several Python objects.  This implementation has the same threshold and
gap-connectivity semantics while retaining only ink *runs* and the currently
active claimed rectangles.  It is therefore O(page pixels + ink runs) memory,
rather than O(ink pixels), and its denominator still comes from page pixels,
not a proposer-derived mask.
"""

from __future__ import annotations

from collections import defaultdict
from typing import Final, TypedDict

import geometry
from structure import DEFAULT_GAP_TOLERANCE_PX, SECONDARY_MARGIN, _ink_threshold

from common.contracts.errors import ContractError


class ReconciliationResult(TypedDict):
    total_ink_pixel_count: int
    claimed_pixel_count: int
    residual_pixel_count: int
    residual_components: list[dict]


DEFAULT_REVIEW_PRIORITY_MIN_DIMENSION_PX: Final = 6


class _Run(TypedDict):
    x0: int
    x1: int  # exclusive, including any blank bridge only in the geometry below
    ink_count: int
    y: int


def _plain_int(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _ink_runs(row: object, threshold: int, y: int, gap: int) -> list[_Run]:
    """Return maximal ink runs, joining only the permitted same-row gaps."""
    if not isinstance(row, (bytes, bytearray)):
        raise ContractError(f"scanline {y} is not grayscale bytes")
    runs: list[_Run] = []
    start: int | None = None
    last_ink: int | None = None
    count = 0
    for x, value in enumerate(row):
        if value <= threshold:
            if start is None:
                start = x
            elif last_ink is not None and x - last_ink - 1 > gap:
                runs.append({"x0": start, "x1": last_ink + 1, "ink_count": count, "y": y})
                start, count = x, 0
            last_ink = x
            count += 1
    if start is not None and last_ink is not None:
        runs.append({"x0": start, "x1": last_ink + 1, "ink_count": count, "y": y})
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
    """Split an ink run around claims, counting actual ink rather than area."""
    # A run may span tolerated blank gaps.  Re-scanning just that short row slice
    # would need the image again, so the caller supplies exact unit runs below.
    # Here every x represents ink: `_residual_unit_runs` deliberately supplies
    # those exact runs after the topology scan has formed connectivity runs.
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


def _unit_ink_runs(row: object, threshold: int, y: int) -> list[_Run]:
    """Exact contiguous ink runs used for counting and residual topology."""
    return _ink_runs(row, threshold, y, 0)


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
        # Exact residual runs on the same scanline are separately stored so
        # claimed intervals can split one original ink run.  Rejoin only the
        # legacy-permitted blank gap before looking at earlier scanlines.
        for left, right in (
            (current[index], current[index + 1]) for index in range(len(current) - 1)
        ):
            if runs[right]["x0"] - runs[left]["x1"] <= gap:
                union(left, right)
        for previous_y in range(max(0, y - radius), y):
            for left in current:
                for right in by_row.get(previous_y, []):
                    # Two half-open segments have ink pixels within the required
                    # horizontal Chebyshev radius exactly under this inequality.
                    if (
                        runs[left]["x0"] < runs[right]["x1"] + radius
                        and runs[right]["x0"] < runs[left]["x1"] + radius
                    ):
                        union(left, right)
    groups: dict[int, list[_Run]] = defaultdict(list)
    for index, run in enumerate(runs):
        groups[find(index)].append(run)
    components = []
    for group in groups.values():
        x0 = min(run["x0"] for run in group)
        x1 = max(run["x1"] for run in group)
        y0 = min(run["y"] for run in group)
        y1 = max(run["y"] for run in group) + 1
        components.append(
            {
                "bounds": {"x": x0, "y": y0, "w": x1 - x0, "h": y1 - y0},
                "pixel_count": sum(run["ink_count"] for run in group),
            }
        )
    return sorted(
        components, key=lambda item: (item["bounds"]["y"], item["bounds"]["x"], item["pixel_count"])
    )


def reconcile(
    width: int,
    height: int,
    rows: list,
    *,
    background: int,
    claimed_bounds: list[geometry.Bounds],
    margin: int = SECONDARY_MARGIN,
    gap_tolerance_px: int = DEFAULT_GAP_TOLERANCE_PX,
    review_priority_min_dimension_px: int = DEFAULT_REVIEW_PRIORITY_MIN_DIMENSION_PX,
) -> ReconciliationResult:
    """Reconcile every page-ink pixel once against final crop rectangles."""
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
    total = claimed = 0
    residual_runs: list[_Run] = []
    for y, row in enumerate(rows):
        if len(row) != width:
            raise ContractError(f"scanline {y} has width {len(row)}, expected {width}")
        for bounds in ends[y]:
            active.remove(bounds)
        active.extend(starts[y])
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
