"""Connected-component labelling over an ink pixel set, shared by three readers.

This is the Designator's own labeller, moved to `common/` because a second
stage needs it: `common/residual_ink.py`'s outside-coverage audit has to name
this page's page-spanning component (the one the Designator withholds from
detected grouping while keeping its pixels in conservation), so it does not
report that pixel population as ordinary outside-coverage ink.

The audit re-derives that component rather than reading the Designator's
record: it labels the same page at the same derived margin under the same
sealed `gap_tolerance_px` and `page_spanning_area_bp`, so it reaches the
identical component from the identical bytes without trusting the stage it
audits. What stays the audit's
own is the contrast it *counts* ink at, which is the half of the instrument
that makes it a second opinion rather than a restatement -- measured on 44 real
pages, using the audit's own looser ink set for the margin instead merges the
writing into the page-spanning component and hides outside-coverage ink on 41
of the 44.
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

    Retained, not deleted, because `test_structure.py` compares the two
    implementations directly to re-prove the substitution's equality claim on
    every run, and `test_conservation.py`'s independent pixel-set oracle now
    calls this rather than the row-oriented `label_components`, so the
    cross-check stays a cross-check instead of comparing row runs to
    themselves.

    No longer the shipped path because of its cost: 383 s and 2.17 GB peak RSS
    for one 8.7-megapixel photographed page at the sealed `gap_tolerance_px =
    3`. Nothing on the live path calls it.
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
                # Breaks a tie between two components sharing the same
                # (top, left) origin, deterministically, by the ink itself
                # rather than by set/dict construction order.
                tuple(sorted(group)),
            )
        )
    components.sort(key=lambda entry: (entry[0]["bounds"]["y"], entry[0]["bounds"]["x"], entry[1]))
    return [component for component, _members in components]


def runs_in_row(bits: bytes | bytearray) -> list[tuple[int, int]]:
    """One translated 0/1 scanline as maximal half-open ink runs."""
    runs: list[tuple[int, int]] = []
    start = bits.find(1)
    while start >= 0:
        end = bits.find(0, start)
        if end < 0:
            end = len(bits)
        runs.append((start, end))
        start = bits.find(1, end)
    return runs


def ink_runs_by_row(pixels) -> dict[int, list[tuple[int, int]]]:
    """The pixel set as maximal horizontal runs, one ascending list per scanline.

    A run is `(x0, x1)`, half-open at `x1`, as `runs_in_row` gives it.
    Splitting on the first missing x rather than on the first blank
    *pixel* is the same rule: this function's input is already the ink set, so
    "absent from the set" is "blank". Duplicates are tolerated by comparing with
    `>` rather than `!=`, because the declared input is a set but a caller is
    not owed a crash for handing the same pixel twice, and because the tests
    drive orderings other than a set's through here. Both halves are pinned:
    `test_two_components_sharing_a_top_left_origin_still_sort_deterministically` drives every
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

    The component geometry alone; `label_component_runs` below keeps each
    component's own runs, for the one caller that needs pixels back rather
    than a rectangle. `scan_ink_components` labels every ink pixel through
    here.

    A row-run substitution over the retired per-pixel union-find
    (`label_components_reference`, kept above as this one's oracle), made on
    measurement: the per-pixel version cost `ink_pixels x radius^2` dictionary
    operations, measured at 383 s and 2.17 GB for one photographed page at the
    sealed `gap_tolerance_px = 3`. Real ink is horizontally contiguous, so a
    page of millions of pixels is a few hundred thousand runs, turning the
    per-pixel neighbourhood probe into an interval overlap test.

    The contract is unchanged and proved, not asserted: same components, same
    bounds, same `gap_tolerance_px` semantics, and the same total order (origin
    `(top, left)`, ties broken by sorted `(x, y)` ink), with `test_structure.py`
    comparing the two implementations directly on every page shape.
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
    the same shape `ink_runs_by_row` produces.

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
        # ascend in both x0 and x1, so one forward pointer per row pair
        # replaces the full cross product.
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

    # Origin alone is not a total order: two components can share (top, left).
    # The retired implementation broke the tie on sorted (x, y) ink;
    # reproduced here without materialising pixels via a heap merge of runs.
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
