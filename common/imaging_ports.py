"""Vendor resize rules, ported as pure dimension arithmetic.

Two witnesses resize the page before it reaches the engine, and each does it
its own way.  The vendor systems ruling adopts those recipes verbatim, so the
arithmetic has to be *ours* — reproduced here rather than imported — for the
reason the vendor systems design gives: no vendor package is installed in
alpha, and every fidelity claim has to be provable in the laptop gate.  What
lives here is only the geometry decision.  The pixels themselves still move
through ``common/imaging.py``'s sealed ``resize_png_lanczos`` recipe, which is
what makes the presented image reproducible from the Exemplar plus the record
(ARCHITECTURE invariant 3).

Both functions are **carried third-party logic**, named as carried under
`cleanroom/README.md`'s citation rule, and both sources permit it:

* :func:`scale_to_fit_chandra` reproduces ``chandra/model/util.py::scale_to_fit``
  from ``github.com/datalab-to/chandra`` at
  ``d4f7467435aa4137d9539f000ddf0b7ced3eb43f`` (package ``chandra-ocr`` 0.2.0,
  Apache-2.0).  That file's bytes at the pinned commit are SHA-256
  ``a13fcd1d8830406a39a7423c42afceca793bb8a3ef0181130ed4dd54263f1774``.
* :func:`resize_to_fit_churro` reproduces
  ``src/churro_ocr/_internal/image.py::resize_image_to_fit`` as
  ``prepare_ocr_image`` calls it, from ``github.com/stanford-oval/Churro`` at
  ``4abb17386d9656199c2776195926545fc527a691`` (tag ``v0.3.0``, Apache-2.0).
  That file's bytes at the pinned commit are SHA-256
  ``0708d2efe879b9b2387616e7283f3a21069c3dc79666a67bf6cef13c7d1dacd9``.

``common/test_vendor_parity.py`` pins both digests again beside a table of
dimensions, and its network-gated arm re-fetches the two files and runs the
vendor's own functions against these ports over that table.

**Departures, deliberate and recorded.** Each vendor function takes and returns
a Pillow image; these take and return dimensions, because the resampling is
``imaging.resize_png_lanczos``'s job here and only the arithmetic is the
vendor's.  Each vendor function also has an early return that is not a scaling
decision — Chandra returns the image untouched when a side is not positive,
Churro's caller never reaches it with one — and rather than reproduce a silent
pass-through of an impossible page, both ports refuse a non-positive side.  A
sealed page always has two positive sides, so no admitted input takes a
different path than the vendor's; the refusal only replaces returning nonsense
unchanged with saying so.
"""

from __future__ import annotations

from typing import Final

# `chandra/model/util.py::scale_to_fit` defaults at the pinned commit.  The
# vendor's grid is 28 even though Chandra-2 is a patch-16 x merge-2 (grid 32)
# model: that mismatch is the vendor's own, it is what every vendor caller
# runs, and the vendor systems design adopts it rather than correcting it.  The
# engine then applies its own grid-32 `smart_resize` on top, exactly as the
# vendor's benchmark ran.
CHANDRA_MAX_SIZE: Final[tuple[int, int]] = (3072, 2048)
CHANDRA_MIN_SIZE: Final[tuple[int, int]] = (1792, 28)
CHANDRA_GRID_SIZE: Final = 28

# `src/churro_ocr/_internal/image.py::MAX_INLINE_IMAGE_DIM` at the pinned
# commit, used by `prepare_ocr_image` for both sides.  The paper-era tree
# states the same 2,500 twice more (`_MAX_IMAGE_DIM`, `MAX_IMAGE_DIM`).
CHURRO_MAX_INLINE_IMAGE_DIM: Final = 2_500


def _refuse_non_positive(width: int, height: int) -> None:
    """Both ports take a real page, and a real page has two positive sides."""
    for name, value in (("width", width), ("height", height)):
        if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
            raise ValueError(f"a presented page {name} must be a positive integer, not {value!r}")


def scale_to_fit_chandra(
    width: int,
    height: int,
    *,
    max_size: tuple[int, int] = CHANDRA_MAX_SIZE,
    min_size: tuple[int, int] = CHANDRA_MIN_SIZE,
    grid_size: int = CHANDRA_GRID_SIZE,
) -> tuple[int, int]:
    """Chandra's presented size for a ``width`` x ``height`` page.

    The vendor's four steps, in its order and with its arithmetic: an ideal
    float scale from the pixel bounds; the scaled sides converted to integer
    grid blocks with ``round``; a refinement loop that trims one block at a
    time, choosing whichever trim distorts the *original* aspect ratio less,
    until the block area is inside the pixel ceiling; and the block counts
    multiplied back out to pixels.

    ``round`` is Python's banker's rounding in both trees, so the reproduction
    is exact rather than merely close, and the loop's tie-break (``<``, so an
    equal loss trims the height) is the vendor's.
    """
    _refuse_non_positive(width, height)

    original_ar = width / height
    current_pixels = width * height
    max_pixels = max_size[0] * max_size[1]
    min_pixels = min_size[0] * min_size[1]

    # 1. Determine ideal float scale based on pixel bounds
    scale = 1.0
    if current_pixels > max_pixels:
        scale = (max_pixels / current_pixels) ** 0.5
    elif current_pixels < min_pixels:
        scale = (min_pixels / current_pixels) ** 0.5

    # 2. Convert dimensions to integer "grid blocks"
    w_blocks = max(1, round((width * scale) / grid_size))
    h_blocks = max(1, round((height * scale) / grid_size))

    # 3. Refinement Loop: Ensure we are under the max limit
    while (w_blocks * h_blocks * grid_size * grid_size) > max_pixels:
        if w_blocks == 1 and h_blocks == 1:
            break
        if w_blocks == 1:
            h_blocks -= 1
            continue
        if h_blocks == 1:
            w_blocks -= 1
            continue
        ar_w_loss = abs(((w_blocks - 1) / h_blocks) - original_ar)
        ar_h_loss = abs((w_blocks / (h_blocks - 1)) - original_ar)
        if ar_w_loss < ar_h_loss:
            w_blocks -= 1
        else:
            h_blocks -= 1

    # 4. Calculate final pixel dimensions
    return w_blocks * grid_size, h_blocks * grid_size


def resize_to_fit_churro(
    width: int,
    height: int,
    *,
    max_width: int = CHURRO_MAX_INLINE_IMAGE_DIM,
    max_height: int = CHURRO_MAX_INLINE_IMAGE_DIM,
) -> tuple[int, int]:
    """Churro's presented size for a ``width`` x ``height`` page.

    Downscale-only: a page already inside the box is returned untouched, and
    otherwise both sides take the single smaller ratio and are truncated by
    ``int`` — not rounded — with a floor of one pixel.  ``prepare_ocr_image``
    additionally converts to RGB; that is a colour step, recorded separately by
    the adapter, and no part of this arithmetic.
    """
    _refuse_non_positive(width, height)

    if width <= max_width and height <= max_height:
        return width, height
    scale = min(max_width / width, max_height / height)
    return max(1, int(width * scale)), max(1, int(height * scale))
