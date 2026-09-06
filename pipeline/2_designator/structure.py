"""Ink connected-component scanning: the walking skeleton's structure pass.

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

from common.contracts.errors import ContractError


class Component(TypedDict):
    bounds: Bounds
    pixel_count: int


# Fraction points below the declared background value, deducted from it to get
# the ink threshold: a pixel at or below `background - margin` counts as ink.
# `SECONDARY_MARGIN` is smaller, which makes `background - margin` a *higher*
# threshold -- numerically closer to the background value -- so it also
# catches fainter marks `PRIMARY_MARGIN` misses: the whole "adds recall"
# property, expressed as one number.
PRIMARY_MARGIN: Final = 20
SECONDARY_MARGIN: Final = 2

# `gap_tolerance_px` used to carry a module default here. It no longer does:
# it is the one threshold this build cannot honestly scale by page dimension
# (SPEC_C section 2), so `run.py` resolves it from the sealed absolute-pixel
# config field and passes it in on every call. A caller that forgets fails
# loudly rather than running under an unreviewed value.


class BackgroundInferenceRefusal(ContractError):
    """This page's background cannot be inferred, so its ink cannot be thresholded.

    Its own kind, rather than a bare `ContractError`, because the caller must be
    able to tell it apart from a corrupt decode. **A page this is raised for is
    still cut and still read.** Tyrel, 2026-08-11: *"Everything gets read every
    time nothing gets pulled out or held"*, and *"missing text is the worst
    failure"*. So this refusal never removes a page from the run — it says only
    that the modal pixel is not paper, so the caller must stop trusting it and
    fall back to cutting predetermined crops instead. A decode error stays fatal;
    this one changes how the page is cut, never whether it is.

    **It also ends this stage's ink measurement for that page, and that is the
    point.** `run.py` used to substitute the page's own mean as a stand-in
    divider so the accounting "had something defensible". It does not: on the
    inverted scan this module's own test uses — 80% of the page at 30, 20% at
    220 — the mean is 68, so the threshold is 48, and every pixel of the *dark
    paper* is classified as ink. Conservation then reports four fifths of a page
    as unclaimed ink and mints a held act over the background. A substituted
    divider is a guess wearing a measurement's name, and GOVERNANCE 10 forbids
    exactly that. The honest report is that this page's ink was not measured.
    """


def _ink_threshold(background: int, margin: int) -> int:
    if not (0 <= background <= 255):
        raise ContractError(f"background value {background} is not an 8-bit sample")
    if margin < 0:
        raise ContractError(f"sensitivity margin {margin} is negative")
    threshold = background - margin
    if threshold < 0:
        raise BackgroundInferenceRefusal(
            f"a background of {background} at a {margin}-point margin implies the threshold "
            f"{threshold}, which is below every 8-bit sample: no pixel could be counted as ink "
            "and a blank result here would be arithmetic rather than a measurement"
        )
    return threshold


class SurroundPolicy(TypedDict):
    """The sealed shape test that tells a photographed page from a dark page.

    Resolved per page from `config/designator_grouping.toml`'s
    `[grouping.surround]` sub-table by `grouping_config.resolve_surround_policy`
    -- *not* by `resolve_thresholds`, and deliberately not a field of
    `GroupingThresholds`: that dataclass is published verbatim as a page's
    `resolved_thresholds`, and this policy is an input to the background
    inference that runs before any threshold touches any geometry. Passed in
    whole rather than as four loose integers so a caller cannot supply three of
    the four. Every field is an integer; `band_px_x` and
    `band_px_y` are already resolved to this page's own pixels, and the two
    `_bp` fields are basis points (1/10000) of a *population*, not of a page
    dimension.
    """

    band_px_x: int
    band_px_y: int
    min_border_dark_bp: int
    max_interior_dark_bp: int


class SurroundEvidence(TypedDict):
    """What the dark-surround test measured on a page it accepted.

    Published rather than dropped. See `_dark_surround` for why this is a
    measurement and not a region.
    """

    band_px_x: int
    band_px_y: int
    dark_at_or_below: int
    dark_pixel_count: int
    border_dark_bp: int
    interior_dark_bp: int


class BackgroundEvidence(TypedDict):
    background: int
    source: str
    surround: SurroundEvidence | None


# The two `source` values `infer_background_evidence` can return. A page that
# reaches neither raises `BackgroundInferenceRefusal` instead, so there is no
# third, quieter outcome.
#
# These are the strings `run.py` publishes as a page's `background_source`,
# spelled here rather than translated there. `inferred-modal` predates this
# module's dark-surround branch and is unchanged, so every existing page record
# still reads exactly as it did; `run.py`'s own third value, `not-inferable`,
# belongs to it rather than here, because it names a refusal this function
# raises and does not return.
BACKGROUND_SOURCE_MODAL: Final = "inferred-modal"
BACKGROUND_SOURCE_INTERIOR_MODE: Final = "inferred-interior-mode"


def _dark_surround(
    width: int,
    height: int,
    rows: list,
    level: int,
    dark_pixel_count: int,
    policy: SurroundPolicy,
) -> SurroundEvidence | None:
    """Is this page's dark majority a photographic surround, or is the page dark?

    The question is geometric, and it has to be: a histogram alone cannot tell a
    black bezel around a lit page from an inverted scan, because both are "most
    of the page is dark". What separates them is *where* the dark is. On a
    photographed register page the dark is a frame: measured over the seven real
    proxies at the sealed 500 bp band, 7864-8637 basis points of the border band
    are at or below the modal value while only 156-1190 bp of the interior are.
    On the inverted scan this module's own test uses the relation reverses
    (border 6578, interior 8333), and on a light-bordered dark-cored page the
    border measures 0. A uniformly dark page never reaches this function at all:
    its mode equals its mean, so the majority-ink branch does not fire and it is
    refused one branch later by the `PRIMARY_MARGIN` guard.

    **This test never removes a pixel from anything.** It decides only which
    value is reported as paper. The surround stays in the page, stays below the
    ink threshold, and is therefore counted as ink by `primary_scan` and
    reconciled as ink by `conservation.reconcile` exactly like any other dark
    pixel. That is deliberate and it is the direction GOALS 1 requires: masking
    the surround out would mean deciding where the page ends, and a page edge
    misjudged by thirty pixels would silently delete a marginal name. Counting
    the bezel as ink is a visible, reconcilable over-count; excluding it is an
    invisible loss.

    **What would otherwise be lost is the interpretation, so that is what is
    recorded.** Without this evidence a reader sees an ink fraction of 0.66 and
    concludes the page is two-thirds written on. `SurroundEvidence` says how
    much of that counted ink is surround, at what level, and on what geometry —
    a measurement rather than a region, because a region would assert a page
    boundary this build has no calibration to assert (the same class of number
    as `gap_tolerance_px`).

    Returns `None` when the page has no interior to compare against, or when the
    shape is not a dark surround — the caller then refuses exactly as before.
    """
    band_x, band_y = policy["band_px_x"], policy["band_px_y"]
    if band_x <= 0 or band_y <= 0 or 2 * band_x >= width or 2 * band_y >= height:
        return None
    # `bytes.translate` maps every sample to 1 (at or below the level) or 0 in
    # C, so this second full-page pass costs a few milliseconds rather than the
    # seconds a per-pixel Python comparison would -- the same reason the
    # labeller stopped walking pixels one at a time.
    table = bytes(1 if value <= level else 0 for value in range(256))
    interior_dark = 0
    for y in range(band_y, height - band_y):
        row = rows[y]
        # Named here rather than left to an `AttributeError` from inside
        # `translate`. The histogram loop above iterates any sequence of ints,
        # so a caller handing this module a list-of-lists page gets that far and
        # then dies with a message naming neither the scanline nor the reason.
        # `conservation._unit_ink_runs` guards the same assumption the same way.
        if not isinstance(row, (bytes, bytearray)):
            raise ContractError(f"scanline {y} is not grayscale bytes")
        interior_dark += row[band_x : width - band_x].translate(table).count(1)
    interior_pixels = (width - 2 * band_x) * (height - 2 * band_y)
    border_pixels = width * height - interior_pixels
    border_dark = dark_pixel_count - interior_dark
    # Floor division, integers only, like every other quantity this module
    # handles. It rounds `border_dark_bp` down (stricter against the `>=` test)
    # and `interior_dark_bp` down (looser against the `<=` test); at these
    # population sizes the difference is one part in ten thousand and the
    # measured separation is several thousand basis points wide.
    border_dark_bp = border_dark * 10000 // border_pixels
    interior_dark_bp = interior_dark * 10000 // interior_pixels
    if border_dark_bp < policy["min_border_dark_bp"]:
        return None
    if interior_dark_bp > policy["max_interior_dark_bp"]:
        return None
    return {
        "band_px_x": band_x,
        "band_px_y": band_y,
        "dark_at_or_below": level,
        "dark_pixel_count": dark_pixel_count,
        "border_dark_bp": border_dark_bp,
        "interior_dark_bp": interior_dark_bp,
    }


def infer_background_evidence(
    width: int, height: int, rows: list, *, surround_policy: SurroundPolicy
) -> BackgroundEvidence:
    """The page's own background value, and how it was established.

    A scanned register page is overwhelmingly paper, so the modal pixel value
    is the paper colour under any real lighting or scanner, not a fixed
    constant this stage would otherwise have to assume matches every page. A
    hardcoded background would be exactly the kind of magic number this
    rebuild's audit trail names as a defect class in the old pipeline's
    thresholds; inferring it per page needs no such constant at all.

    **The premise above is a premise, and this function checks it.** Where
    ink is the numeric majority of a page -- a heavily inked page, an inverted
    scan, a photographic negative -- the modal pixel is the *ink* colour. The
    threshold below it then admits almost nothing, the page reconciles to zero
    ink, and the stage exits `complete` having found no acts at all. That is a
    page lost in silence, which is the exact shape GOALS 1 forbids: a missed act
    is worse than a poorly read one, and Tyrel's 2026-08-04 ruling 15 says blank
    is proved and never inferred.

    The check needs no constant either. Paper is the lighter surface, so an
    inferred background must be at least as light as the page's own mean; when
    it is darker than the average pixel, the mode is ink. Compared as
    `mode * count >= total` so the arithmetic stays in integers -- every
    quantity this module handles is an integer, and a float comparison here
    could pass by accident.

    **That comparison alone misses the uniformly dark page**, which is the one
    shape where mode and mean are equal and both wrong. A page of solid black has
    `mode == mean == 0`, so `mode * count >= total` holds exactly and this
    function used to return 0 as the paper colour. `_ink_threshold(0, 20)` is
    then -20, no 8-bit sample can be at or below it, the page counts zero ink
    pixels, and the run exits `complete` over a visibly black page -- the same
    silent loss the majority-ink check exists to stop, reached by the one route
    it does not cover. So a background must also be light enough to preserve
    `primary_scan`'s declared separation at `PRIMARY_MARGIN`.

    **And the majority-ink test alone was wrong about a photographed page,
    measured on 7 of 7 real ones.** A photograph of a register opening carries a
    black surround around the paper -- 18-26% of the frame on the seven real
    proxies -- and pure black is then by a wide margin the single most common
    value, because the paper itself is spread across dozens of tones in the
    180-240 band. So the modal pixel was 0 on every real page, the majority-ink
    branch refused every one of them, and the live path cut all seven into blind
    fallback slabs with `ink_measurable: false` and never reconciled their ink at
    all (`workbench/active/TIMING_REPORT_2026-09-05.md` §1a). The premise "the
    modal pixel is paper" is sound for a flatbed scan and false for a photograph.

    The repair is one branch, and it is asked only where the old code was about
    to refuse. `_dark_surround` asks whether the dark majority is a *frame*
    around a lighter interior rather than the page itself; where it is, the paper
    value is the modal pixel **at or above the page's own mean**, which is the
    same "paper is the lighter surface" premise applied to the population the
    surround does not dominate. That value still faces the `PRIMARY_MARGIN`
    guard, and a page with no light interior mode -- an inverted scan, a
    uniformly dark page, a page whose dark is in the middle rather than the
    frame -- still refuses by name exactly as it did before.

    Conservation separately reconciles at the more sensitive `SECONDARY_MARGIN`;
    a page this guard refuses is still cut and read, records
    `ink_measurable: false`, and holds the run rather than reporting a
    measurement it did not make.
    """
    if width <= 0 or height <= 0:
        raise ContractError(f"a {width}x{height} page has no pixels to infer a background from")
    if len(rows) != height:
        raise ContractError(f"expected {height} scanlines, got {len(rows)}")
    histogram = [0] * 256
    for y in range(height):
        row = rows[y]
        if len(row) != width:
            raise ContractError(f"scanline {y} has width {len(row)}, expected {width}")
        for value in row:
            histogram[value] += 1
    background = max(range(256), key=lambda value: histogram[value])
    counted = width * height
    total = sum(value * count for value, count in enumerate(histogram))
    if background * counted < total:
        mean = total // counted
        surround = _dark_surround(
            width, height, rows, background, sum(histogram[: background + 1]), surround_policy
        )
        if surround is not None:
            # The modal value among pixels at or above the page's own mean: the
            # paper population, measured on the whole page rather than on the
            # interior alone. The surround is entirely at or below `background`,
            # which is below the mean, so it cannot contribute a candidate here
            # -- excluding it geometrically would change nothing about this
            # answer while making it depend on the band width, which the
            # detection above already spends.
            paper = max(range(mean, 256), key=lambda value: histogram[value])
            if paper >= PRIMARY_MARGIN:
                return {
                    "background": paper,
                    "source": BACKGROUND_SOURCE_INTERIOR_MODE,
                    "surround": surround,
                }
        raise BackgroundInferenceRefusal(
            f"the most common pixel on this {width}x{height} page is {background}, which is "
            f"darker than its own mean of {mean}: the page is majority ink, so "
            "its background cannot be inferred and a blank result here would be inferred "
            "rather than proved"
            + (
                ""
                if surround is None
                else f"; a dark surround was found ({surround['border_dark_bp']} bp of the "
                f"border band and {surround['interior_dark_bp']} bp of the interior at or "
                f"below {background}) but the interior's own paper mode is darker than the "
                f"{PRIMARY_MARGIN}-point ink margin"
            )
        )
    if background < PRIMARY_MARGIN:
        raise BackgroundInferenceRefusal(
            f"the most common pixel on this {width}x{height} page is {background}, which is "
            f"darker than the {PRIMARY_MARGIN}-point ink margin: the threshold it implies is "
            "below every 8-bit sample, so no pixel on this page could ever be counted as ink "
            "and a blank result here would be arithmetic rather than a measurement"
        )
    return {"background": background, "source": BACKGROUND_SOURCE_MODAL, "surround": None}


def infer_background(
    width: int, height: int, rows: list, *, surround_policy: SurroundPolicy
) -> int:
    """`infer_background_evidence`'s background value alone.

    Kept because most callers -- and every test that builds a page to check one
    threshold -- want the integer and nothing else. The evidence function is the
    one `run.py` calls, because a page whose background came from the
    dark-surround branch has a measurement to publish and dropping it would be
    the silent half of GOVERNANCE 2.
    """
    return infer_background_evidence(width, height, rows, surround_policy=surround_policy)[
        "background"
    ]


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
    `>` rather than `!=`, because the declared input is a set but the tests also
    drive an ordered `dict.keys()` view through here and a caller is not owed a
    crash for handing the same pixel twice.
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
    width: int, height: int, rows: list, *, background: int, gap_tolerance_px: int
) -> list[Component]:
    return scan_ink_components(
        width,
        height,
        rows,
        background=background,
        margin=PRIMARY_MARGIN,
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
