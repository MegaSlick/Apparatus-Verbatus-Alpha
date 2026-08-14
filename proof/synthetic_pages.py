"""Synthetic proof pages: the walking skeleton's only input.

No real register material exists here or may be invented. These are abstract
synthetic marks — filled rectangles and pixel runs standing in for lines of
writing — never simulated handwriting. Everything is deterministic: no
timestamps, no randomness, no font rendering (glyph shapes vary by machine
and by installed font, which would break byte-identical output), and no
floating-point arithmetic in any value that reaches the encoded bytes.

The PNG codec lives in `common/imaging.py`, not here: the Designator crops
through it and the Perlector verifies regions through it, so the bytes the
pipeline reads and the bytes these fixtures declare come from exactly one
encoder. A second copy would be a second thing to drift.
"""

from __future__ import annotations

from typing import NotRequired, TypedDict

from common.imaging import Bounds, encode_grayscale_png

FIXTURE_ID = "synthetic-two-page-v0"


class Act(TypedDict):
    ordinal: int
    bounds: Bounds
    ink: int


class Page(TypedDict):
    ordinal: int
    width: int
    height: int
    acts: tuple[Act, ...]
    scenarios: NotRequired[tuple[str, ...]]


# Background is a light gray; each act's ink value is a distinct dark tone so
# the three acts are visibly and byte-wise distinguishable from each other
# and from the page background. Bounds sit inside the page with margin and
# never overlap. Page 2's single act is the geometric continuation of page
# 1's second act — same act ordinal, a fresh rectangle on the next page;
# linking the two into one logical act is a different unit's job.
_BACKGROUND = 230

PAGES: tuple[Page, ...] = (
    {
        "ordinal": 1,
        "width": 200,
        "height": 260,
        "acts": (
            {
                "ordinal": 0,
                "bounds": {"x": 20, "y": 20, "w": 160, "h": 80},
                "ink": 40,
            },
            {
                "ordinal": 1,
                "bounds": {"x": 20, "y": 120, "w": 160, "h": 100},
                "ink": 90,
            },
        ),
    },
    {
        "ordinal": 2,
        "width": 200,
        "height": 260,
        "acts": (
            {
                "ordinal": 1,
                "bounds": {"x": 20, "y": 20, "w": 160, "h": 60},
                "ink": 90,
            },
        ),
    },
)

# A genuinely ink-free page is admitted only for the named integration
# scenario. The two-page base fixture remains the input for every pre-existing
# scenario, while this extra page lets the real downstream stage programs prove
# that a Designator-minted page fallback is witnessed and read.
SCENARIO_PAGES: tuple[Page, ...] = (
    {
        "ordinal": 3,
        "width": 200,
        "height": 260,
        "acts": (),
        "scenarios": ("ink-free-page",),
    },
)

ALL_PAGES: tuple[Page, ...] = PAGES + SCENARIO_PAGES


def _render_rows(width: int, height: int, acts: tuple[Act, ...]) -> list[bytearray]:
    """Paint the background and each act's rectangle into a row buffer.

    Each act is filled with alternating ink/background pixel runs rather than
    a solid block, so a crop of the act is distinguishable pixel-by-pixel
    from a crop of flat fill of the same ink value — this is the "simple
    pixel run standing in for a line of writing" called for by the spec.
    """
    rows = [bytearray([_BACKGROUND]) * width for _ in range(height)]
    for act in acts:
        bounds = act["bounds"]
        ink = act["ink"]
        x0, y0, w, h = bounds["x"], bounds["y"], bounds["w"], bounds["h"]
        if x0 < 0 or y0 < 0 or x0 + w > width or y0 + h > height:
            raise ValueError(f"act bounds {bounds} fall outside page {width}x{height}")
        for row_offset in range(h):
            row = rows[y0 + row_offset]
            # A run every 4 rows and a stripe every 5 columns keeps the
            # pattern trivially deterministic and cheap to eyeball in tests.
            if row_offset % 4 < 2:
                for col_offset in range(w):
                    if col_offset % 5 != 0:
                        row[x0 + col_offset] = ink
    return rows


def render_page(descriptor: Page) -> bytes:
    """Render one page descriptor to deterministic 8-bit grayscale PNG bytes."""
    width, height = descriptor["width"], descriptor["height"]
    rows = _render_rows(width, height, descriptor["acts"])
    return encode_grayscale_png(width, height, rows)


def page_bytes(ordinal: int) -> bytes:
    """Render the page with the given 1-based ordinal."""
    for page in ALL_PAGES:
        if page["ordinal"] == ordinal:
            return render_page(page)
    raise ValueError(f"no page with ordinal {ordinal}")
