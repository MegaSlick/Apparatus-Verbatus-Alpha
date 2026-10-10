"""The downscaled page render a Perlector reading is shown, and its recorded transform.

The render is deterministic for a sealed page and a transform, so the image
shown is reproducible from the Exemplar plus the recorded transform: the
Perlector's page feed shows it, and a later stage checking a page feed derives it
again from the same sealed page.
"""

from __future__ import annotations

from io import BytesIO
from typing import Any, Callable, Final

from PIL import Image

from common.contracts.errors import SchemaRefusal
from common.exemplar_boundary import read_sealed_page
from common.imaging import (
    crop_png,
    decode_page,
    dimensions,
    encode_grayscale_png_deterministic,
    lanczos_source,
)

# Modes whose page `crop_png` writes back sample for sample at 8 bits with no
# tRNS record, so decoding that full-page crop again gives the decoded page's
# own pixels in the same mode; such a page is resized straight from its decode.
_SAMPLE_EXACT_MODES: Final = frozenset({"L", "LA", "RGB", "RGBA"})


def _display_page(page_bytes: bytes, width: int, height: int) -> Image.Image:
    """The page as `crop_png` displays it at its full bounds, as a decoded image.

    Shared with the decode cache, so it is only read, never changed.
    """
    page = decode_page(page_bytes)
    if page.rows is not None:
        return Image.frombytes("L", (page.width, page.height), b"".join(page.rows))
    image = page.image
    if image.mode in _SAMPLE_EXACT_MODES and "transparency" not in image.info:
        return image
    display = crop_png(page_bytes, {"x": 0, "y": 0, "w": width, "h": height})
    with Image.open(BytesIO(display)) as shown:
        shown.load()
    return shown


def render_size(size: tuple[int, int], maximum_edge: int) -> tuple[int, int]:
    """The `(width, height)` a page of `size` is rendered at under `maximum_edge`."""
    width, height = size
    if max(width, height) <= maximum_edge:
        return width, height
    scale = maximum_edge / max(width, height)
    return max(1, round(width * scale)), max(1, round(height * scale))


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
    image = _display_page(page_bytes, width, height)
    target = render_size((image.width, image.height), maximum_edge)
    if target == (image.width, image.height):
        rendered = image.copy()
        resampler = "identity"
    else:
        rendered = lanczos_source(image).resize(target, resample=Image.Resampling.LANCZOS)
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


# Why a page render has the size it has: the page's ink made legible, or the
# whole sealed page at its own size for a page reading sealed to show it
# unscaled (`[feed] page_image = "full"`).
LEGIBLE_INK: Final = "legible-ink"
FULL_PAGE: Final = "full-page"


def build_page_render(
    context,
    *,
    source_page_id: str,
    source_page_ordinal: int,
    page_context: dict[str, int],
    full_page: bool = False,
    retain: Callable[[bytes, str], dict[str, str]] | None = None,
) -> dict[str, Any]:
    """The page render for one page reading, with its transform and its reason
    recorded (ARCHITECTURE invariant 3: the exact image shown is reproducible
    from the Exemplar plus the recorded transforms).

    `page_context` is the run's sealed `[page_context]` table: the page is
    rendered at `maximum_edge`, large enough that its ink is legible.
    `full_page` renders the sealed page at its own size instead. `retain`
    stores the render's bytes and returns their reference; it is the stage's
    own `context.retain` unless a reader re-deriving a sealed render passes one
    that only checks the bytes are already retained.
    """
    page, page_bytes = read_sealed_page(context.tree, source_page_id)
    width, height = dimensions(page_bytes)
    full = (max(width, height), FULL_PAGE)
    edge, reason = full if full_page else (page_context["maximum_edge"], LEGIBLE_INK)
    return _published_render(
        context.retain if retain is None else retain,
        page,
        page_bytes,
        source_page_id=source_page_id,
        source_page_ordinal=source_page_ordinal,
        edge=edge,
        reason=reason,
    )


def _published_render(
    retain: Callable[[bytes, str], dict[str, str]],
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
    published = retain(downscaled, "the page render")
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
