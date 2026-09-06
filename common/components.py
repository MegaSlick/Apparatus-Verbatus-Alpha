"""Connected-component labelling over an ink pixel set, shared by three readers.

**This module is the Designator's own labeller, moved.** It lived in
`pipeline/2_designator/structure.py` until 2026-09-06 and is re-exported from
there, so every caller in that stage still reaches for it where it has always
been. It moved for the reason `common/background.py` moved the day before and by
the same rule: a second stage needs it, and `common/` may not import a stage.
The second reader is `common/residual_ink.py`, whose outside-coverage audit has
to be able to name this page's page-spanning component -- the one the
Designator withholds from grouping and mints as a held act -- so that it does
not report a component already accounted for as ink nobody is looking at.

**The audit re-derives that component; it does not read the Designator's
record.** It labels the same page at the same derived margin under the same
sealed `gap_tolerance_px` and the same sealed `page_spanning_area_bp`, so it
reaches the identical component from the identical bytes without trusting the
stage it audits. What stays the audit's own is the contrast it *counts* ink at
(`residual_ink.MINIMUM_CONTRAST_BELOW_BACKGROUND`), which is the half of the
instrument that makes it a second opinion rather than a restatement.

Why the margin is not the audit's own here, when the contrast is: measured on 44
real pages, labelling the audit's own looser ink set merges the writing into the
page-spanning component and hides 3,367 to 1,480,349 outside-coverage ink pixels
on 41 of the 44 -- it makes the audit report clean pages. A component is a
property of the page's structure, found at the level the page derives for
itself; how much ink there is, is the audit's own question. The measurement is
in the session's `AUDIT_GATES_REPORT.md`.
"""

import heapq
from functools import cmp_to_key
from typing import Iterator, TypedDict

from common.contracts.errors import ContractError
from common.imaging import Bounds


class Component(TypedDict):
    bounds: Bounds
    pixel_count: int


def label_components_reference(pixels: set, *, gap_tolerance_px: int) -> list[Component]:
    """The retired per-pixel set/union-find labeller, kept as the oracle.

    This is the implementation `label_components` had until the row-run
    substitution below replaced it. It is retained, not deleted, for two
    reasons that are both about what would otherwise stop being checked.

    First, `test_structure.py` compares the two implementations directly on
    every page shape this module's tests build and on a randomised page with
    known components, so the equality claim the substitution rests on is
    re-proved on every run rather than asserted once at the commit that made
    it. Second — and this is the one that would have gone quiet —
    `test_conservation.py`'s `_legacy_reference` used `label_components` as
    the *independent* pixel-set oracle that holds `conservation._components`'
    row-oriented notion of "connected" to the same meaning. Once
    `label_components` became row-oriented too, that oracle would have been
    comparing row runs against row runs and proving nothing. It now calls this
    function, so the cross-check stays a cross-check.

    Its cost is the reason it is no longer the shipped path: measured 383 s and
    2.17 GB of peak RSS for one 8.7-megapixel photographed page at the sealed
    `gap_tolerance_px = 3` (`workbench/active/TIMING_REPORT_2026-09-05.md` §1b).
    Nothing on the live path calls it.
    """
    if gap_tolerance_px < 0:
        raise ContractError(f"gap tolerance {gap_tolerance_px} is negative")
    if not pixels:
        return []

    parent: dict[tuple[int, int], tuple[int, int]] = {pixel: pixel for pixel in pixels}

    def find(pixel: tuple[int, int]) -> tuple[int, int]:
        root = pixel
        while parent[root] != root:
            root = parent[root]
        while parent[pixel] != root:
            parent[pixel], pixel = root, parent[pixel]
        return root

    def union(a: tuple[int, int], b: tuple[int, int]) -> None:
        root_a, root_b = find(a), find(b)
        if root_a != root_b:
            parent[root_a] = root_b

    # `gap_tolerance_px` counts empty pixels allowed *between* two ink pixels,
    # so even a tolerance of 0 must still reach an immediately adjacent pixel
    # (distance 1) -- the Chebyshev search radius is one more than the gap.
    radius = gap_tolerance_px + 1
    # Only the forward half of the neighbourhood (dy > 0, or dy == 0 and
    # dx > 0): union is symmetric, so checking both halves would just union
    # the same pair twice for every neighbouring ink pixel.
    offsets = [
        (dx, dy)
        for dy in range(0, radius + 1)
        for dx in range(-radius, radius + 1)
        if (dy > 0) or (dy == 0 and dx > 0)
    ]
    for x, y in pixels:
        for dx, dy in offsets:
            neighbour = (x + dx, y + dy)
            if neighbour in pixels:
                union((x, y), neighbour)

    members: dict[tuple[int, int], list[tuple[int, int]]] = {}
    for pixel in pixels:
        members.setdefault(find(pixel), []).append(pixel)

    components: list[tuple[Component, tuple[tuple[int, int], ...]]] = []
    for group in members.values():
        xs = [x for x, _ in group]
        ys = [y for _, y in group]
        x0, x1 = min(xs), max(xs)
        y0, y1 = min(ys), max(ys)
        components.append(
            (
                {
                    "bounds": {"x": x0, "y": y0, "w": x1 - x0 + 1, "h": y1 - y0 + 1},
                    "pixel_count": len(group),
                },
                # Two disjoint components can share a (top, left) origin -- the
                # sort key below -- while differing everywhere else; a pixel
                # belongs to exactly one component, so no two components can
                # ever share the same sorted member-pixel tuple. Carried only
                # to break that tie, never returned: two components with the
                # same origin still need *some* deterministic order, and
                # falling back to `members.values()`'s own iteration order
                # (a dict keyed by union-find root, itself pixel hash order)
                # would make that order depend on set/dict construction rather
                # than on the ink itself.
                tuple(sorted(group)),
            )
        )
    components.sort(key=lambda entry: (entry[0]["bounds"]["y"], entry[0]["bounds"]["x"], entry[1]))
    return [component for component, _members in components]


def ink_runs_by_row(pixels) -> dict[int, list[tuple[int, int]]]:
    """The pixel set as maximal horizontal runs, one ascending list per scanline.

    A run is `(x0, x1)`, half-open at `x1`, exactly as `conservation._Run`
    carries it. Splitting on the first missing x rather than on the first blank
    *pixel* is the same rule: this function's input is already the ink set, so
    "absent from the set" is "blank". Duplicates are tolerated by comparing with
    `>` rather than `!=`, because the declared input is a set but a caller is
    not owed a crash for handing the same pixel twice, and because the tests
    drive orderings other than a set's through here. Both halves are pinned:
    `test_component_order_is_total_not_merely_by_origin` drives every
    permutation of one page's pixels as an ordered `dict.keys()` view, and
    `test_a_repeated_pixel_is_tolerated_rather_than_split_into_two_runs` hands
    this function a list with duplicates in it.
    """
    by_row: dict[int, list[int]] = {}
    for x, y in pixels:
        column = by_row.get(y)
        if column is None:
            by_row[y] = [x]
        else:
            column.append(x)
    runs_by_row: dict[int, list[tuple[int, int]]] = {}
    for y, column in by_row.items():
        column.sort()
        runs: list[tuple[int, int]] = []
        start = previous = column[0]
        for x in column[1:]:
            if x > previous + 1:
                runs.append((start, previous + 1))
                start = x
            previous = x
        runs.append((start, previous + 1))
        runs_by_row[y] = runs
    return runs_by_row


def label_components(pixels: set, *, gap_tolerance_px: int) -> list[Component]:
    """Connected-component labeling over an arbitrary set of (x, y) pixels.

    The component geometry alone. `label_component_runs` below is the same
    labelling with each component's own horizontal runs kept, for the one
    caller that needs the pixels back rather than the rectangle.

    `scan_ink_components` labels every ink pixel through here.

    **This is the row-run substitution `structure.py`'s module docstring
    instructs, made on measurement.** The retired implementation
    (`label_components_reference`, kept above as this one's oracle) held a
    union-find over every ink *pixel* and probed a Chebyshev neighbourhood of
    `(gap_tolerance_px + 1)` around each one, so its cost was `ink_pixels x
    radius^2` dictionary operations. On a real photographed register page at
    300-DPI-equivalent size that measured **383 s and 2.17 GB** for one page at
    the sealed `gap_tolerance_px = 3`, paid by `run.py`'s `_analyze_page` for
    every sealed page before the first chair is called
    (`workbench/active/TIMING_REPORT_2026-09-05.md` §1b, §1e). The union-find
    here is over ink *runs* instead: real ink is horizontally contiguous, so a
    page of 5.7 million ink pixels is a few hundred thousand runs, and the
    per-pixel neighbourhood probe becomes an interval overlap test between two
    scanlines' run lists.

    **The contract is unchanged and that is proved, not asserted.** Same
    components, same bounds, same `gap_tolerance_px` semantics (it still counts
    blank pixels *between* two ink pixels, so a tolerance of 0 still reaches an
    immediately adjacent pixel and the Chebyshev radius is still one more than
    the gap), and the same total order: by component origin `(top, left)`, ties
    broken by the component's own ink compared as the sorted `(x, y)` sequence
    the retired implementation compared. `test_structure.py` compares the two
    implementations directly on every page these tests build and on randomised
    pages.

    **The technique is `conservation._components`', written beside it rather
    than imported.** `conservation.py` imports `SECONDARY_MARGIN` and
    `_ink_threshold` from this module, so this module cannot import from
    `conservation` -- the import would be circular. The two therefore stay two
    implementations of one rule, held together the way they already were:
    `test_conservation.py` compares `conservation._components` against
    `label_components_reference` above, which is the pixel-set definition both
    of them are answerable to.
    """
    return [
        component
        for component, _runs in label_component_runs(
            ink_runs_by_row(pixels) if pixels else {}, gap_tolerance_px=gap_tolerance_px
        )
    ]


def label_component_runs(
    runs_by_row: dict[int, list[tuple[int, int]]], *, gap_tolerance_px: int
) -> list[tuple[Component, list[tuple[int, int, int]]]]:
    """`label_components`, with each component's own runs kept beside it.

    The whole of the labelling lives here and `label_components` is the wrapper
    that throws the runs away, so there is one implementation of "connected"
    rather than two that could drift. A run is `(y, x0, x1)`, half-open at `x1`,
    the same shape `ink_runs_by_row` produces and `conservation._Run` carries.

    The input is runs rather than a pixel set because the caller that needs the
    runs back (`common/residual_ink.py`) already holds the page as translated
    scanlines and would otherwise materialise one Python tuple per ink pixel to
    hand them over -- the memory `structure.py`'s own docstring names as the
    remaining cost of `ink_pixels`.
    """
    if gap_tolerance_px < 0:
        raise ContractError(f"gap tolerance {gap_tolerance_px} is negative")
    if not runs_by_row:
        return []

    # One flat run table, plus each scanline's runs as indices into it in
    # ascending x order. The flat table is what union-find indexes; the
    # per-row index lists are what the sweep below walks.
    run_x0: list[int] = []
    run_x1: list[int] = []
    run_y: list[int] = []
    indices_by_row: dict[int, list[int]] = {}
    for y in sorted(runs_by_row):
        indices = []
        for x0, x1 in runs_by_row[y]:
            indices.append(len(run_x0))
            run_x0.append(x0)
            run_x1.append(x1)
            run_y.append(y)
        indices_by_row[y] = indices

    parent = list(range(len(run_x0)))

    def find(index: int) -> int:
        while parent[index] != index:
            parent[index] = parent[parent[index]]
            index = parent[index]
        return index

    def union(left: int, right: int) -> None:
        left_root, right_root = find(left), find(right)
        if left_root != right_root:
            parent[right_root] = left_root

    radius = gap_tolerance_px + 1
    for y in sorted(indices_by_row):
        current = indices_by_row[y]
        # Same scanline: two maximal runs are separated by at least one blank
        # pixel, and they join when that blank gap is within tolerance. `x1` is
        # half-open, so the blank distance between them is `x0 - x1 + 1` and
        # the legacy Chebyshev test `distance <= radius` is `x0 - x1 <= gap`.
        for position in range(len(current) - 1):
            left, right = current[position], current[position + 1]
            if run_x0[right] - run_x1[left] <= gap_tolerance_px:
                union(left, right)
        # Earlier scanlines within the Chebyshev radius. Runs on one scanline
        # are disjoint and ascending in both x0 and x1, so one forward pointer
        # per row pair replaces the full cross product: a previous-row run
        # wholly left of this `left` is wholly left of every later `left` too,
        # and past that dropped prefix the scan only needs to stop at the first
        # run wholly right of `left`. Two half-open segments hold ink pixels
        # within the horizontal Chebyshev radius exactly under the
        # dropped/stopped inequalities. This is `conservation._components`'
        # sweep; see this function's docstring for why it is not imported.
        for previous_y in range(y - radius, y):
            previous = indices_by_row.get(previous_y)
            if not previous:
                continue
            start = 0
            for left in current:
                left_x0, left_x1 = run_x0[left], run_x1[left]
                while start < len(previous) and run_x1[previous[start]] + radius <= left_x0:
                    start += 1
                for offset in range(start, len(previous)):
                    right = previous[offset]
                    if run_x0[right] >= left_x1 + radius:
                        break
                    union(left, right)

    groups: dict[int, list[int]] = {}
    for index in range(len(run_x0)):
        groups.setdefault(find(index), []).append(index)

    entries: list[tuple[Component, list[int]]] = []
    for group in groups.values():
        x0 = min(run_x0[index] for index in group)
        x1 = max(run_x1[index] for index in group)
        # `y0`/`y1` read the group's first and last run rather than scanning it,
        # which is only correct because run indices ascend with `y`: the table
        # above is built in `sorted(runs_by_row)` order and this group was
        # appended to in ascending index order. Reordering either loop would
        # silently give every multi-row component the wrong vertical bounds, so
        # the invariant is stated where it is relied on.
        y0 = run_y[group[0]]
        y1 = run_y[group[-1]] + 1
        entries.append(
            (
                {
                    "bounds": {"x": x0, "y": y0, "w": x1 - x0, "h": y1 - y0},
                    "pixel_count": sum(run_x1[index] - run_x0[index] for index in group),
                },
                group,
            )
        )

    # Two disjoint components can share a (top, left) origin while differing
    # everywhere else, so the origin alone is not a total order. The retired
    # implementation broke that tie on `tuple(sorted(group))` -- the component's
    # own ink in sorted (x, y) order. Reproduced here without materialising
    # either side's pixels: a heap merge of a group's runs yields exactly that
    # sequence, and two distinct components cannot hold identical ink, so the
    # comparison is decided at the first difference or by the shorter stream
    # running out (the tuple-prefix rule, which is `pixel_count` order).
    def origin(entry: tuple[Component, list[int]]) -> tuple[int, int]:
        return (entry[0]["bounds"]["y"], entry[0]["bounds"]["x"])

    def pixel_stream(group: list[int]) -> Iterator[tuple[int, int]]:
        return heapq.merge(
            *(((x, run_y[index]) for x in range(run_x0[index], run_x1[index])) for index in group)
        )

    def compare_ink(left: tuple[Component, list[int]], right: tuple[Component, list[int]]) -> int:
        for left_pixel, right_pixel in zip(
            pixel_stream(left[1]), pixel_stream(right[1]), strict=False
        ):
            if left_pixel != right_pixel:
                return -1 if left_pixel < right_pixel else 1
        return left[0]["pixel_count"] - right[0]["pixel_count"]

    entries.sort(key=origin)
    ordered: list[tuple[Component, list[tuple[int, int, int]]]] = []
    span_start = 0
    for index in range(1, len(entries) + 1):
        if index == len(entries) or origin(entries[index]) != origin(entries[span_start]):
            span = entries[span_start:index]
            if len(span) > 1:
                span.sort(key=cmp_to_key(compare_ink))
            ordered.extend(
                (
                    component,
                    [(run_y[member], run_x0[member], run_x1[member]) for member in group],
                )
                for component, group in span
            )
            span_start = index
    return ordered
