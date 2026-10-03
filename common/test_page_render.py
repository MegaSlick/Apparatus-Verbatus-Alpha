"""The Perlector page render runs the resampler its transform record names."""

from io import BytesIO

from PIL import Image

from common.imaging import crop_png, encode_grayscale_png_deterministic, encode_image_deterministic
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


def test_a_bilevel_page_inside_the_bound_is_shown_at_its_own_pixels():
    page = _bilevel_page()

    rendered, transform = _downscale_page(page, maximum_edge=40)

    assert transform["resampler"] == "identity"
    assert transform["target_dimensions"] == {"w": 40, "h": 24}
    with Image.open(BytesIO(crop_png(page, {"x": 0, "y": 0, "w": 40, "h": 24}))) as shown:
        shown.load()
        assert rendered == _grayscale_png(shown.convert("L"))
