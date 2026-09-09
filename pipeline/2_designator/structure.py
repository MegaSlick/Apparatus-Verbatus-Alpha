"""Ink connected-component scanning: the walking skeleton's structure pass.

**The background inference this pass thresholds against is no longer here.** It
moved to `common/background.py` on 2026-09-06 so that the Ink Map and the
Recensor's residual-ink audit infer the same paper value on the same page bytes
that this stage does; the names are re-exported at the top of this file, and
`common/background.py`'s own docstring carries the reason and the measurement.
What stays here is the scanning: the ink set, the labeller, the two sensitivity
presets and the two scans.

A real structure-pass model reads a page once and yields regions plus a
structural classification (ARCHITECTURE: "it may use textual as well as visual
cues"). This walking skeleton has no model to call -- constraint: no pod, no
model call, synthetic pages only -- so its structure pass is a real,
deterministic, independently-checkable *visual* pass: it finds every
ink-bearing connected component on the decoded page and reports its geometry.
What is absent is the textual half of that sentence and the model that would
supply it — not the pass, which runs for real on every page.

Two independent sensitivity presets exist, `PRIMARY` and `SECONDARY`, so that
wiring in a genuinely different secondary detector (`run.py`'s
`secondary_provenance`) has a real, principled difference to report rather
than a second call to the same function: `SECONDARY` is more sensitive, so it
may find faint ink `PRIMARY` misses, matching the P0-incident-shaped rule this
stage is built against -- a secondary proposer adds recall, and only recall.

Connectivity is tolerant of a small gap, not strict pixel adjacency. Real ink
is not a solid fill -- pen strokes, serifs and letterforms leave gaps a strict
flood fill would report as separate marks -- so two ink pixels separated by up
to `gap_tolerance_px` empty pixels are treated as one component (a tolerance
of 0 still reaches an immediately adjacent pixel). This is an ordinary
morphological "close" before labeling, not a fixture-specific hack; it is what
keeps one word from scanning as a dozen one-pixel islands.

**The substitution boundary for real pages was explicit, and half of it has
now been crossed on measurement.** `label_components` is the row-run
implementation `conservation.py` uses (U13), because the retired per-pixel
set/union-find version measured **383 s and 2.17 GB for one 8.7-megapixel
photographed page** at the sealed `gap_tolerance_px = 3`, against 0.41 s for
`conservation.reconcile` labelling *more* ink on the same page
(`workbench/active/TIMING_REPORT_2026-09-05.md` §1b, §1e). The retired
implementation stays here as `_label_components_reference`, the oracle both
this module's and `conservation.py`'s labelling are checked against.

**`ink_pixels` was deliberately not substituted with it.** The docstring used
to instruct replacing the two "as a pair", to protect the threshold and
connectivity contract they share; the measurement says the pair is not the unit
of the decision. `ink_pixels` is 1.45 s at 8.7 megapixels — 0.37% of the pass —
and `label_components` was 99.6% of it, so a paired replacement would have given
up 1.5 s to save 390. The shared contract is honoured by a
`label_components`-only substitution, which is what this is. What `ink_pixels`
does still cost is *memory*: it materialises one Python tuple per ink pixel, and
that is the remaining share of the 2.17 GB. Substituting it is a real piece of
work, unmeasured here beyond that sentence, and named rather than deferred
silently.
"""

import heapq
from functools import cmp_to_key
from typing import Final, Iterator, TypedDict

from geometry import Bounds

from common.background import (  # noqa: F401  (re-exported: see the note below)
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
from common.contracts.errors import ContractError


class Component(TypedDict):
    bounds: Bounds
    pixel_count: int


# The background inference this scan runs on top of lives in `common/background.py`
# and is re-exported here. It moved on 2026-09-06 because three stages threshold
# ink and only this one had the measured inference: the Ink Map and the
# Recensor's residual-ink audit took the page's raw histogram mode as paper,
# which on a photographed opening is a dark population, so their audit measured
# approximately zero ink over a page full of writing and the cross-stage
# containment pin held vacuously. The names below are this module's own history
# and every caller in this stage still reaches for them here; nothing in this
# file changes what they do.
#
# `PRIMARY_MARGIN` is the floor under the margin each page derives for itself and
# the level the background-plausibility probe is measured at -- see
# `common/background.py`, which carries the 127-page measurement behind both.
#
# `SECONDARY_MARGIN` stays here and is deliberately *not* derived. It is a
# fraction point below the declared background value: a pixel at or below
# `background - margin` counts as ink, and 2 is smaller than any derived margin,
# which makes `background - margin` a *higher* threshold -- numerically closer to
# the background value -- so it also catches fainter marks the primary scan
# misses: the whole "adds recall" property, expressed as one number. It is the
# sensitive instrument: `secondary_scan` exists to add recall and only recall,
# and `conservation.reconcile` uses it as the denominator that makes a mark the
# grouping pass missed appear as residual rather than as an absence. A derived
# margin there would trade a visible over-count for a possible silent loss,
# which is the one direction GOALS 1 forbids. Three properties follow and all
# three are pinned: the secondary scan is strictly more sensitive than the
# primary on *every* page (2 is below the floor, so no page can invert them);
# the Recensor's audit, which now infers the same background this stage does,
# runs at a contrast at or above this margin, so it can never call ink what this
# stage's own accounting dismissed; and both comparisons stay between source
# literals `common/test_designator_recensor_ink_calibration.py` can read
# statically.
SECONDARY_MARGIN: Final = 2


# `gap_tolerance_px` used to carry a module default here. It no longer does:
# it is the one threshold this build cannot honestly scale by page dimension
# (SPEC_C section 2), so `run.py` resolves it from the sealed absolute-pixel
# config field and passes it in on every call. A caller that forgets fails
# loudly rather than running under an unreviewed value.


def ink_pixels(width: int, height: int, rows: list, *, background: int, margin: int) -> set:
    """Every pixel at or below the ink threshold, as a set of (x, y) pairs.

    Split out from `scan_ink_components` so a caller that needs the raw ink set
    -- rather than whole components, which may straddle a crop's edge -- can
    take it directly. `conservation.py` was that caller until U13 replaced its
    pixel sets with row runs; it now shares only `_ink_threshold` and
    `SECONDARY_MARGIN` with this module, and `test_conservation.py` keeps this
    function as the oracle its replacement is checked against.
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


def _label_components_reference(pixels: set, *, gap_tolerance_px: int) -> list[Component]:
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


def _ink_runs_by_row(pixels) -> dict[int, list[tuple[int, int]]]:
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

    `scan_ink_components` labels every ink pixel through here.

    **This is the row-run substitution `structure.py`'s module docstring
    instructs, made on measurement.** The retired implementation
    (`_label_components_reference`, kept above as this one's oracle) held a
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
    `_label_components_reference` above, which is the pixel-set definition both
    of them are answerable to.
    """
    if gap_tolerance_px < 0:
        raise ContractError(f"gap tolerance {gap_tolerance_px} is negative")
    if not pixels:
        return []

    runs_by_row = _ink_runs_by_row(pixels)
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
    ordered: list[Component] = []
    span_start = 0
    for index in range(1, len(entries) + 1):
        if index == len(entries) or origin(entries[index]) != origin(entries[span_start]):
            span = entries[span_start:index]
            if len(span) > 1:
                span.sort(key=cmp_to_key(compare_ink))
            ordered.extend(component for component, _group in span)
            span_start = index
    return ordered


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

    `margin` has no default and never will. It used to be `PRIMARY_MARGIN`, and
    the constant is still the floor under every value this is now passed --
    `infer_background_evidence` publishes it as `ink_margin` on the evidence,
    and `run.py` hands that same integer here and to the page's published
    record. A caller that forgets it fails loudly rather than running the whole
    page under a threshold nobody resolved, which is the rule
    `gap_tolerance_px` above is already here under.
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
