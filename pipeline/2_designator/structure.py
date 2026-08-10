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
"""

from typing import Final, TypedDict

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

DEFAULT_GAP_TOLERANCE_PX: Final = 3


def _ink_threshold(background: int, margin: int) -> int:
    if not (0 <= background <= 255):
        raise ContractError(f"background value {background} is not an 8-bit sample")
    if margin < 0:
        raise ContractError(f"sensitivity margin {margin} is negative")
    return background - margin


def infer_background(width: int, height: int, rows: list) -> int:
    """The page's own background value: its single most common pixel.

    A scanned register page is overwhelmingly paper, so the modal pixel value
    is the paper colour under any real lighting or scanner, not a fixed
    constant this stage would otherwise have to assume matches every page. A
    hardcoded background would be exactly the kind of magic number this
    rebuild's audit trail names as a defect class in the old pipeline's
    thresholds; inferring it per page needs no such constant at all.
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
    return max(range(256), key=lambda value: histogram[value])


def ink_pixels(width: int, height: int, rows: list, *, background: int, margin: int) -> set:
    """Every pixel at or below the ink threshold, as a set of (x, y) pairs.

    Split out from `scan_ink_components` so `conservation.py` can classify
    each ink pixel against a set of claimed crops directly, rather than
    reasoning about whole components that may straddle a crop's edge.
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


def label_components(
    pixels: set, *, gap_tolerance_px: int = DEFAULT_GAP_TOLERANCE_PX
) -> list[Component]:
    """Connected-component labeling over an arbitrary set of (x, y) pixels.

    The one union-find implementation both `scan_ink_components` (over every
    ink pixel) and `conservation.py` (over the residual subset only) use, so
    the two never drift on what "connected" means.
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


def scan_ink_components(
    width: int,
    height: int,
    rows: list,
    *,
    background: int,
    margin: int,
    gap_tolerance_px: int = DEFAULT_GAP_TOLERANCE_PX,
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


def primary_scan(width: int, height: int, rows: list, *, background: int) -> list[Component]:
    return scan_ink_components(width, height, rows, background=background, margin=PRIMARY_MARGIN)


def secondary_scan(width: int, height: int, rows: list, *, background: int) -> list[Component]:
    return scan_ink_components(width, height, rows, background=background, margin=SECONDARY_MARGIN)
