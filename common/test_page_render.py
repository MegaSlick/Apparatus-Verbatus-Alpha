"""The Perlector page render runs the resampler its transform record names."""

from io import BytesIO

import pytest
from PIL import Image

from common.imaging import (
    crop_png,
    encode_grayscale_png_deterministic,
    encode_image_deterministic,
    lanczos_source,
)
from common.page_render import _downscale_page


def _bilevel_page(width: int = 40, height: int = 24) -> bytes:
    """A sealed bilevel page of one-pixel strokes, the kind a decimation loses."""
    image = Image.new("1", (width, height), 1)
    for x in range(0, width, 3):
        for y in range(2, height - 2):
            image.putpixel((x, y), 0)
    return encode_image_deterministic(image)


def _grayscale_png(image: Image.Image) -> bytes:
    samples = image.tobytes()
    rows = [
        bytearray(samples[row * image.width : (row + 1) * image.width])
        for row in range(image.height)
    ]
    return encode_grayscale_png_deterministic(image.width, image.height, rows)


def test_a_bilevel_page_is_resampled_with_lanczos_as_its_transform_records():
    page = _bilevel_page()
    with Image.open(BytesIO(page)) as source:
        source.load()
        assert source.mode == "1", "the fixture stopped exercising the substitution"
        nearest = _grayscale_png(source.resize((20, 12), Image.Resampling.NEAREST).convert("L"))
        lanczos = _grayscale_png(source.convert("L").resize((20, 12), Image.Resampling.LANCZOS))
    assert nearest != lanczos

    rendered, transform = _downscale_page(page, maximum_edge=20)

    assert transform["resampler"] == "pillow-lanczos"
    assert transform["source_dimensions"] == {"w": 40, "h": 24}
    assert transform["target_dimensions"] == {"w": 20, "h": 12}
    assert rendered == lanczos
    assert _downscale_page(page, maximum_edge=20) == (rendered, transform)


def test_a_bilevel_page_inside_the_bound_is_shown_at_its_own_pixels(monkeypatch):
    page = _bilevel_page()

    def no_resize(*_args, **_kwargs):
        raise AssertionError("a page inside the bound was resampled")

    monkeypatch.setattr(Image.Image, "resize", no_resize)

    rendered, transform = _downscale_page(page, maximum_edge=40)

    assert transform["resampler"] == "identity"
    assert transform["target_dimensions"] == {"w": 40, "h": 24}
    with Image.open(BytesIO(crop_png(page, {"x": 0, "y": 0, "w": 40, "h": 24}))) as shown:
        shown.load()
        assert rendered == _grayscale_png(shown.convert("L"))


def _render_through_a_full_page_crop(page: bytes, maximum_edge: int) -> bytes:
    """The render as it is defined: the page's full-bounds `crop_png`, decoded and resized."""
    with Image.open(BytesIO(page)) as source:
        width, height = source.size
    with Image.open(BytesIO(crop_png(page, {"x": 0, "y": 0, "w": width, "h": height}))) as shown:
        shown.load()
        resized = lanczos_source(shown).resize((width // 2, height // 2), Image.Resampling.LANCZOS)
    return _grayscale_png(resized.convert("L"))


def _page_in(mode: str, *, transparency=None) -> bytes:
    image = Image.new("RGB", (40, 24), (240, 230, 200))
    for x in range(0, 40, 3):
        for y in range(2, 22):
            image.putpixel((x, y), (20 + x, 10 + y, 90))
    shown = image.convert(mode)
    if mode in {"LA", "RGBA"}:
        shown.putalpha(Image.linear_gradient("L").resize(shown.size))
    if transparency is not None:
        shown.info["transparency"] = transparency
    data = BytesIO()
    shown.save(data, format="PNG")
    return data.getvalue()


@pytest.mark.parametrize(
    "page",
    [
        _page_in("L"),
        _page_in("LA"),
        _page_in("RGB"),
        _page_in("RGBA"),
        _page_in("P"),
        _page_in("RGB", transparency=(240, 230, 200)),
        _page_in("L", transparency=240),
        _bilevel_page(),
        _grayscale_png(Image.linear_gradient("L").resize((40, 24))),
    ],
    ids=["L", "LA", "RGB", "RGBA", "P", "RGB-tRNS", "L-tRNS", "bilevel", "own-codec"],
)
def test_a_render_from_the_decoded_page_is_the_render_of_its_full_page_crop(page):
    """Resizing the decoded page straight away gives the bytes the full-page crop gives."""
    rendered, transform = _downscale_page(page, maximum_edge=20)

    assert transform["resampler"] == "pillow-lanczos"
    assert rendered == _render_through_a_full_page_crop(page, maximum_edge=20)
