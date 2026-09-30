"""The downscaled page render a Perlector reading is shown, and its recorded transform.

The render is deterministic for a sealed page and a transform, so the image
shown is reproducible from the Exemplar plus the recorded transform: the
Perlector's act dossier and its page feed show it, and a later stage checking a
page feed derives it again from the same sealed page.
"""

from __future__ import annotations

from io import BytesIO
from typing import Any, Callable, Final

from PIL import Image

from common.contracts.errors import SchemaRefusal
from common.exemplar_boundary import read_sealed_page
from common.imaging import crop_png, dimensions, encode_grayscale_png_deterministic


def _downscale_page(page_bytes: bytes, *, maximum_edge: int) -> tuple[bytes, dict[str, Any]]:
    """A genuine downscale of the sealed page, deterministic for a fixed input.

    The sealed page goes through `common.imaging.crop_png` first, at its own
    full bounds, so this render inherits the one decode-and-display policy the
    rest of the pipeline uses -- including the high-precision handling that
    stops a 16-bit scan rendering as near-white. Pillow then does the resize
    with a named resampler.

    The transform record names the source size, the target size and the
    resampler rather than only a factor, because ARCHITECTURE invariant 3 asks
    that the exact image shown be reproducible from the Exemplar plus the
    recorded transforms -- and "downscaled by 2" is only reproducible by
    someone who also has this function. A page already inside the bound is
    recorded as `identity` rather than silently resampled to itself.
    """
    width, height = dimensions(page_bytes)
    display = crop_png(page_bytes, {"x": 0, "y": 0, "w": width, "h": height})
    with Image.open(BytesIO(display)) as image:
        image.load()
        if max(image.width, image.height) <= maximum_edge:
            rendered = image.copy()
            resampler = "identity"
        else:
            scale = maximum_edge / max(image.width, image.height)
            rendered = image.resize(
                (max(1, round(image.width * scale)), max(1, round(image.height * scale))),
                resample=Image.Resampling.LANCZOS,
            )
            resampler = "pillow-lanczos"
        # The project's own deterministic encoder, never Pillow's: Pillow's
        # wheels bundle their own zlib, so `rendered.save(...)` produces
        # different bytes on Linux than on macOS, and a run-time blob's bytes
        # name its content-addressed path and every artifact digest downstream.
        grayscale = rendered.convert("L")
        samples = grayscale.tobytes()
        deterministic = encode_grayscale_png_deterministic(
            grayscale.width,
            grayscale.height,
            [
                bytearray(samples[row * grayscale.width : (row + 1) * grayscale.width])
                for row in range(grayscale.height)
            ],
        )
        return deterministic, {
            "operation": "downscale-for-page-context",
            "source_dimensions": {"w": width, "h": height},
            "target_dimensions": {"w": rendered.width, "h": rendered.height},
            "maximum_edge": maximum_edge,
            "resampler": resampler,
        }


# Why a page render has the size it has: the page's ink made legible, or a
# layout-sized render of a page the act's own full-resolution crops already
# show whole.
LEGIBLE_INK: Final = "legible-ink"
COVERED_BY_CROP: Final = "covered-by-crop"
MULTI_PAGE_ACT: Final = "multi-page-act"
# The whole sealed page at its own size, for a page reading sealed to show it
# unscaled (`[feed] page_image = "full"`).
FULL_PAGE: Final = "full-page"


def union_area(rectangles: list[tuple[int, int, int, int]]) -> int:
    """The area covered by at least one `(x0, y0, x1, y1)` rectangle, by coordinate
    compression. Exact in integers; quadratic in the handful of regions one act carries.
    """
    xs = sorted({x for rectangle in rectangles for x in (rectangle[0], rectangle[2])})
    ys = sorted({y for rectangle in rectangles for y in (rectangle[1], rectangle[3])})
    area = 0
    for x0, x1 in zip(xs, xs[1:], strict=False):
        for y0, y1 in zip(ys, ys[1:], strict=False):
            if any(
                left <= x0 and x1 <= right and top <= y0 and y1 <= bottom
                for left, top, right, bottom in rectangles
            ):
                area += (x1 - x0) * (y1 - y0)
    return area


def build_page_render(
    context,
    *,
    source_page_id: str,
    source_page_ordinal: int,
    page_context: dict[str, int],
    crop_bounds: list[dict[str, int]],
    multi_page: bool = False,
    full_page: bool = False,
    retain: Callable[[bytes], dict[str, str]] | None = None,
) -> dict[str, Any]:
    """The page render for one act's page, with its transform and its reason
    recorded (ARCHITECTURE invariant 3: the exact image shown is reproducible
    from the Exemplar plus the recorded transforms).

    `page_context` is the run's sealed `[page_context]` table. A page is
    rendered at `maximum_edge`, large enough that its ink is legible, unless the
    act's own crops on it (`crop_bounds`, sealed-page coordinates) cover the
    whole page: those crops already carry every pixel at full resolution, so the
    page is rendered at `covered_page_edge` as layout only, and a second
    full-resolution copy does not crowd the act out of the served row. Every
    page of an act spanning more than one page (`multi_page`) is rendered at
    `covered_page_edge` too: a legible render of each page would refuse acts
    over a page turn that the served row holds with layout renders.
    `full_page` renders the sealed page at its own size, whatever the crops.
    `retain` stores the render's bytes and returns their reference; it is the
    stage's own `context.retain` unless a reader re-deriving a sealed render
    passes one that only checks the bytes are already retained.
    """
    page, page_bytes = read_sealed_page(context.tree, source_page_id)
    width, height = dimensions(page_bytes)
    retain = context.retain if retain is None else retain
    if full_page:
        return _published_render(
            retain,
            page,
            page_bytes,
            source_page_id=source_page_id,
            source_page_ordinal=source_page_ordinal,
            edge=max(width, height),
            reason=FULL_PAGE,
        )
    covered = (
        union_area(
            [
                (
                    max(0, bounds["x"]),
                    max(0, bounds["y"]),
                    min(width, bounds["x"] + bounds["w"]),
                    min(height, bounds["y"] + bounds["h"]),
                )
                for bounds in crop_bounds
            ]
        )
        == width * height
    )
    reason = MULTI_PAGE_ACT if multi_page else COVERED_BY_CROP if covered else LEGIBLE_INK
    edge = page_context["maximum_edge" if reason == LEGIBLE_INK else "covered_page_edge"]
    return _published_render(
        retain,
        page,
        page_bytes,
        source_page_id=source_page_id,
        source_page_ordinal=source_page_ordinal,
        edge=edge,
        reason=reason,
    )


def _published_render(
    retain: Callable[[bytes], dict[str, str]],
    page: dict[str, Any],
    page_bytes: bytes,
    *,
    source_page_id: str,
    source_page_ordinal: int,
    edge: int,
    reason: str,
) -> dict[str, Any]:
    try:
        downscaled, transform = _downscale_page(page_bytes, maximum_edge=edge)
    except (OSError, ValueError, Image.DecompressionBombError) as error:
        raise SchemaRefusal(
            "a sealed Exemplar page could not be rendered as Perlector page context"
        ) from error
    published = retain(downscaled)
    return {
        "source_page_id": source_page_id,
        "source_page_ordinal": source_page_ordinal,
        # The sealed page this render was derived from, named so the derivation
        # can be checked rather than believed.
        "source": {
            "relative_path": page["payload"]["image_path"],
            "sha256": page["payload"]["source_sha256"],
        },
        "image_path": published["relative_path"],
        "image_sha256": published["sha256"],
        "transform": transform,
        "reason": reason,
    }
