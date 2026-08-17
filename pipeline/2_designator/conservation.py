"""Independent, row-oriented residual-ink reconciliation (U13).

Conservation: claimed ink plus residual ink equals every ink pixel found.

**The denominator is the page's own pixels, never the structure pass's own
output.** The old pipeline's conservation logic proved coverage of units the
structure model had already emitted -- a chunk not claimed by a crop was
accounted for, but a mark the model never emitted as a chunk at all had no
denominator to be missing from. An independent second read
(`/stage/70_gpt_review/ASSESSMENT.md:172-173`) named it precisely: "the present
conservation logic proves coverage of units already emitted by a structural
model. It cannot prove that the model did not miss ink entirely." This module
rescans the page's actual pixels and classifies every one it finds as claimed
(inside some cut crop) or residual (not inside any), so a mark the grouping pass
never produced a region for still appears -- as a residual component, never as
an absence.

**Every residual region is accounted regardless of size.** `review_priority`
below orders which residual a reviewer looks at first; it never decides whether
a residual exists in the accounting. Deleting the priority threshold entirely
would only reorder review, never drop a region -- which is the property
GOVERNANCE 10 requires of any threshold in an instrument: the instrument may not
constrain what it measures.

**Two ink calibrations exist in this pipeline, by decision rather than drift.**
This stage conserves a pixel as ink at `background - SECONDARY_MARGIN` (2
levels), the most sensitive declared structural threshold available to it; the
Recensor's independent page-coverage check
(`pipeline/5_recensor/residual_ink.py`) requires
`MINIMUM_CONTRAST_BELOW_BACKGROUND` (40 levels). They are different instruments:
this one is the Designator reconciling its own cut and errs sensitive, because a
faint mark it dismisses here is GOALS 1's worst failure; the Recensor's is an
after-the-fact audit of the same pages and errs confident, because it exists to
catch whole missed regions rather than to re-litigate faint pixels a held act
already accounts for. The asymmetry is safe in exactly one direction, and that
direction is the invariant: every pixel the Recensor calls ink is ink to this
stage too, so the audit can never flag ink this accounting silently ignored --
while ink only this stage sees ends as a held residual act, which is visible,
never lost. The containment is pinned where the two stages legitimately meet,
`common/test_designator_recensor_ink_calibration.py`; narrowing this stage's
margin past the Recensor's contrast would break the safe direction and must be a
deliberate two-sided change.

**How it is computed, and why that changed.** The original skeleton built three
Python ``set[(x, y)]`` values. That made the proof easy to read, but a dense
600dpi parish page turns every ink pixel into several Python objects. This
implementation keeps the same threshold and gap-connectivity semantics while
retaining only ink *runs* and the currently active claimed rectangles, so its
memory is O(page pixels + ink runs) rather than O(ink pixels). Equivalence to
the retired pixel-set algorithm is not asserted, it is exercised: the retired
implementation is kept in `test_conservation.py` as an oracle and compared
against on randomized, fully-inked, and exhaustive claim-edge pages.

Connectivity labelling is this module's own (`_components`) rather than
`structure.label_components`, because the two now work over different objects --
runs here, pixels there. `test_conservation.py`'s oracle is what holds the two
to one meaning of "connected".
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


# A residual component at or above this many pixels, on either axis, is
# reviewed first -- a priority ordering, not a filter. See the module
# docstring: nothing here may become an inclusion test.
DEFAULT_REVIEW_PRIORITY_MIN_DIMENSION_PX: Final = 6


class _Run(TypedDict):
    """One horizontal stretch of ink on scanline `y`, half-open at `x1`.

    Every run this module builds is contiguous ink: `_unit_ink_runs` splits on
    the first blank pixel, and `_subtract_claims` only ever cuts a run shorter.
    So `x1 - x0 == ink_count` for every run, and the two are carried separately
    only because `_components` reads the span while the accounting reads the
    count. Tolerated blank gaps are bridged in `_components`' connectivity, never
    inside a run.
    """

    x0: int
    x1: int
    ink_count: int
    y: int


def _plain_int(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _unit_ink_runs(row: object, threshold: int, y: int) -> list[_Run]:
    """Exact contiguous ink runs: one run per unbroken stretch of ink.

    A single blank pixel ends a run. Gap tolerance is not applied here on
    purpose: it belongs to `_components`, which decides what is *connected*,
    while this decides what is *ink*. Bridging a tolerated gap into the run
    itself would put blank pixels inside `ink_count` and make `_subtract_claims`
    charge a crop for paper it covers.
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

    Every x in `run` is ink -- `_unit_ink_runs` guarantees it -- so an interval
    width here *is* an ink count, and neither the claimed total nor a residual
    piece has to re-read the scanline to know how much ink it covers. `claims`
    must arrive merged and sorted (`_merged_claim_intervals`); overlapping claims
    would otherwise be counted twice.
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
        # Exact residual runs on the same scanline are separately stored so
        # claimed intervals can split one original ink run.  Rejoin only the
        # legacy-permitted blank gap before looking at earlier scanlines.
        for left, right in (
            (current[index], current[index + 1]) for index in range(len(current) - 1)
        ):
            if runs[right]["x0"] - runs[left]["x1"] <= gap:
                union(left, right)
        for previous_y in range(max(0, y - radius), y):
            previous = by_row.get(previous_y)
            if not previous:
                continue
            # Runs on one scanline are disjoint and left-to-right --
            # `_unit_ink_runs` scans each row in x order and `_subtract_claims`
            # only cuts a run into left-to-right pieces -- so both x0 and x1 are
            # strictly increasing along a row (the same-row rejoin above already
            # leans on this order). That monotonicity is what makes one forward
            # pointer per row pair replace the full cross product: a previous-row
            # run wholly left of this `left` is wholly left of every later `left`
            # too, and past that dropped prefix every remaining run passes the
            # left-side test, so the scan only needs to stop at the first run
            # wholly right of `left`. Two half-open segments have ink pixels
            # within the required horizontal Chebyshev radius exactly under the
            # dropped/stopped inequalities.
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
    """Reconcile one page's ink against the crops actually cut on it.

    `claimed_bounds` is the final, padded crop rectangles this stage actually
    cut -- not the structure pass's raw proposals -- because a crop's padding is
    exactly what may already cover ink the raw proposal's own rectangle did not.
    Every ink pixel is classified once; `claimed_pixel_count +
    residual_pixel_count == total_ink_pixel_count` always, by construction,
    because every pixel in the denominator is examined exactly once. The final
    check below is not that identity (which holds by arithmetic) but the
    independent one: that the residual *components* published for review sum back
    to the residual ink counted, so no residual pixel is left out of a region a
    reviewer can be shown.
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
    # The merged claim intervals are a function of the active set alone, and the
    # active set only changes on a row where some crop starts or ends. Reading
    # `starts`/`ends` with `.get` rather than `[y]` also keeps the sweep from
    # inserting an empty list for every scanline on the page.
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
