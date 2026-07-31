"""Tests for the synthetic proof-page module.

Every assertion is exact — a count, not "at least one" — because a test that
passes over an empty population is a defect in this project.
"""

from __future__ import annotations

import pytest

from proof.synthetic_pages import PAGES, crop_png, page_bytes, render_page

_PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"


def _decode(png_bytes: bytes):
    # Reach into the private decoder deliberately: it is the only decoder
    # this module owns, and the crop/pixel-equality tests need row access.
    from proof.synthetic_pages import _decode_grayscale_png

    return _decode_grayscale_png(png_bytes)


def _walk_for_floats(value) -> bool:
    """Return True if any float appears anywhere inside `value`."""
    if isinstance(value, float):
        return True
    if isinstance(value, dict):
        return any(_walk_for_floats(v) for v in value.values())
    if isinstance(value, (list, tuple)):
        return any(_walk_for_floats(item) for item in value)
    return False


def test_rendering_is_byte_identical_on_repeat():
    first = render_page(PAGES[0])
    second = render_page(PAGES[0])
    assert first == second


def test_both_pages_start_with_png_signature_and_decode():
    for page in PAGES:
        rendered = render_page(page)
        assert rendered[:8] == _PNG_SIGNATURE
        width, height, rows = _decode(rendered)
        assert width == page["width"]
        assert height == page["height"]
        assert len(rows) == height


def test_exactly_two_pages_with_expected_act_counts():
    assert len(PAGES) == 2
    assert PAGES[0]["ordinal"] == 1
    assert PAGES[1]["ordinal"] == 2
    assert len(PAGES[0]["acts"]) == 2
    assert len(PAGES[1]["acts"]) == 1


def test_act_bounds_lie_inside_their_page_and_page_one_acts_do_not_overlap():
    for page in PAGES:
        for act in page["acts"]:
            b = act["bounds"]
            assert b["x"] >= 0
            assert b["y"] >= 0
            assert b["x"] + b["w"] <= page["width"]
            assert b["y"] + b["h"] <= page["height"]

    first_bounds = PAGES[0]["acts"][0]["bounds"]
    second_bounds = PAGES[0]["acts"][1]["bounds"]
    horizontally_disjoint = (
        first_bounds["x"] + first_bounds["w"] <= second_bounds["x"]
        or second_bounds["x"] + second_bounds["w"] <= first_bounds["x"]
    )
    vertically_disjoint = (
        first_bounds["y"] + first_bounds["h"] <= second_bounds["y"]
        or second_bounds["y"] + second_bounds["h"] <= first_bounds["y"]
    )
    assert horizontally_disjoint or vertically_disjoint


def test_cropping_same_bounds_twice_gives_identical_bytes():
    page = render_page(PAGES[0])
    bounds = PAGES[0]["acts"][0]["bounds"]
    first_crop = crop_png(page, bounds)
    second_crop = crop_png(page, bounds)
    assert first_crop == second_crop


def test_crops_of_two_different_acts_differ():
    page = render_page(PAGES[0])
    first_crop = crop_png(page, PAGES[0]["acts"][0]["bounds"])
    second_crop = crop_png(page, PAGES[0]["acts"][1]["bounds"])
    assert first_crop != second_crop


def test_crop_pixels_equal_corresponding_page_pixels():
    page_descriptor = PAGES[0]
    page = render_page(page_descriptor)
    bounds = page_descriptor["acts"][1]["bounds"]
    crop = crop_png(page, bounds)

    _, _, page_rows = _decode(page)
    crop_width, crop_height, crop_rows = _decode(crop)

    assert crop_width == bounds["w"]
    assert crop_height == bounds["h"]

    x0, y0, w, h = bounds["x"], bounds["y"], bounds["w"], bounds["h"]
    for row_offset in range(h):
        expected = page_rows[y0 + row_offset][x0 : x0 + w]
        assert crop_rows[row_offset] == expected


def test_out_of_bounds_crop_raises_value_error():
    page = render_page(PAGES[0])
    page_descriptor = PAGES[0]
    out_of_bounds = {
        "x": page_descriptor["width"] - 1,
        "y": 0,
        "w": 50,
        "h": 10,
    }
    with pytest.raises(ValueError):
        crop_png(page, out_of_bounds)


def test_non_png_input_raises_value_error():
    with pytest.raises(ValueError):
        crop_png(b"not a png at all", {"x": 0, "y": 0, "w": 1, "h": 1})


def test_truncated_png_input_raises_value_error():
    page = render_page(PAGES[0])
    truncated = page[:16]
    with pytest.raises(ValueError):
        crop_png(truncated, {"x": 0, "y": 0, "w": 1, "h": 1})


def test_page_bytes_matches_render_page_for_each_ordinal():
    for page in PAGES:
        assert page_bytes(page["ordinal"]) == render_page(page)


def test_no_float_appears_anywhere_in_pages():
    assert not _walk_for_floats(PAGES)
