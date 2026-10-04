"""The second triage operation order: crop, rotate, crop again on a canvas filled with a
recorded level. Rows of the first order keep their meaning; a sealed page of either
order maps back to its master through the row alone. Synthetic images only."""

from __future__ import annotations

import copy
from io import BytesIO

import pytest
from PIL import Image, ImageDraw

from common.contracts import triage
from common.contracts.canonical import digest_bytes
from common.contracts.errors import ContractError, SchemaRefusal
from common.imaging import (
    TRIAGE_APPLY_RECIPE,
    TRIAGE_APPLY_RECIPE_V2,
    imaging_library_versions,
    render_triage_derivative,
    triage_apply_recipe,
    triage_mode_transform,
    triage_operations,
    triage_point_to_frame,
)


def _png(image: Image.Image) -> bytes:
    output = BytesIO()
    image.save(output, format="PNG")
    return output.getvalue()


def _v2_part(**changes):
    part = triage.make_part(
        {"x": 0, "y": 0, "w": 300, "h": 200},
        {"x": 20, "y": 10, "w": 260, "h": 180},
        7_500,
        colour_mode="keep",
        post_crop_box={"x": -6, "y": 12, "w": 250, "h": 190},
        fill=[180],
    )
    part.update(changes)
    return part


def _row(part, order=triage.SPLIT_OPERATION_ORDER_V2, frame=(300, 200), master=b"m"):
    return triage.make_row(
        corpus_id="parish-a",
        source_frame_sha256=digest_bytes(master),
        frame={"width": frame[0], "height": frame[1]},
        split=triage.make_split([part], operation_order=order),
        re_shoot_cluster_id=None,
        confidence=0,
        mode="auto",
        actor={"kind": "producer", "identity": "pagekit", "revision": "0.1.0"},
        human_override=False,
    )


def test_a_row_of_the_second_order_carries_its_post_crop_and_fill():
    row = _row(_v2_part())
    part = row["split"]["parts"][0]
    assert part["post_crop_box"] == {"space": "rotated", "x": -6, "y": 12, "w": 250, "h": 190}
    assert part["fill"] == {"levels": [180]}


@pytest.mark.parametrize(
    ("order", "change", "message"),
    [
        (triage.SPLIT_OPERATION_ORDER, {}, "region/crop_box/rotation/colour_mode"),
        (triage.SPLIT_OPERATION_ORDER_V2, {"fill": {"levels": [256]}}, "fill"),
        (triage.SPLIT_OPERATION_ORDER_V2, {"fill": {"levels": []}}, "fill"),
        (triage.SPLIT_OPERATION_ORDER_V2, {"fill": None}, "fill"),
        (
            triage.SPLIT_OPERATION_ORDER_V2,
            {"post_crop_box": {"space": "rotated", "x": 0, "y": 0, "w": 0, "h": 4}},
            "post_crop_box",
        ),
        (
            triage.SPLIT_OPERATION_ORDER_V2,
            {"post_crop_box": {"space": "part", "x": 0, "y": 0, "w": 4, "h": 4}},
            "post_crop_box",
        ),
    ],
)
def test_a_part_that_does_not_match_its_operation_order_is_refused(order, change, message):
    with pytest.raises(SchemaRefusal, match=message):
        _row(_v2_part(**change), order=order)


def test_a_row_of_the_first_order_cannot_carry_a_post_crop():
    part = _v2_part()
    del part["fill"]
    with pytest.raises(SchemaRefusal, match="region/crop_box/rotation/colour_mode record"):
        _row(part, order=triage.SPLIT_OPERATION_ORDER)


def test_the_first_orders_rows_and_recipe_are_unchanged():
    assert triage_apply_recipe(triage.SPLIT_OPERATION_ORDER) is TRIAGE_APPLY_RECIPE
    assert dict(TRIAGE_APPLY_RECIPE) == {
        "schema": "triage-raster-apply-v1",
        "rotation_resample": "Pillow.Resampling.BICUBIC",
        "rotation_fill": "Pillow-default-zero",
        "rotation_expand": True,
        "colour_conversion": "Pillow.Image.convert-direct-or-via-RGB",
        "encoder": "common.imaging.encode_image_deterministic-v1",
    }
    assert triage_apply_recipe(triage.SPLIT_OPERATION_ORDER_V2) is TRIAGE_APPLY_RECIPE_V2
    assert TRIAGE_APPLY_RECIPE_V2["schema"] == "triage-raster-apply-v2"


@pytest.mark.parametrize(("mode", "levels"), [("L", [180]), ("RGB", [10, 200, 30])])
def test_the_canvas_beyond_the_scan_is_the_recorded_fill_never_black(mode, levels):
    paper = 230 if mode == "L" else (230, 225, 210)
    master = _png(Image.new(mode, (300, 200), paper))
    part = _v2_part(fill={"levels": levels})

    rendered, geometry = render_triage_derivative(master, page_index=0, part=part)

    page = Image.open(BytesIO(rendered))
    assert page.size == (250, 190) == (geometry["width"], geometry["height"])
    expected = levels[0] if mode == "L" else tuple(levels)
    # Left of the canvas (post-crop x is -6) and a rotated-away corner are both fill.
    assert page.getpixel((2, 100)) == expected
    assert page.getpixel((249, 0)) == expected
    assert page.getpixel((125, 95)) == paper
    extrema = page.getextrema()
    darkest = [extrema[0]] if mode == "L" else [band[0] for band in extrema]
    assert min(darkest) >= min(levels), "black appeared where the row recorded a fill"


def _dots(image: Image.Image, dots):
    draw = ImageDraw.Draw(image)
    for x, y in dots:
        draw.rectangle((x - 3, y - 3, x + 3, y + 3), fill=10)
    return image


def _centres(page: Image.Image):
    grey = page.convert("L")
    width, height = grey.size
    pixels = grey.load()
    seen, found = set(), []
    for y in range(height):
        for x in range(width):
            if pixels[x, y] >= 100 or (x, y) in seen:
                continue
            stack, points = [(x, y)], []
            seen.add((x, y))
            while stack:
                px, py = stack.pop()
                points.append((px, py))
                for nx, ny in ((px + 1, py), (px - 1, py), (px, py + 1), (px, py - 1)):
                    if 0 <= nx < width and 0 <= ny < height and (nx, ny) not in seen:
                        if pixels[nx, ny] < 100:
                            seen.add((nx, ny))
                            stack.append((nx, ny))
            found.append(
                (
                    sum(p[0] for p in points) / len(points) + 0.5,
                    sum(p[1] for p in points) / len(points) + 0.5,
                )
            )
    return found


@pytest.mark.parametrize(
    "part",
    [
        triage.make_part(
            {"x": 100, "y": 0, "w": 200, "h": 200},
            {"x": 10, "y": 15, "w": 180, "h": 170},
            90_000,
            colour_mode="keep",
        ),
        triage.make_part(
            {"x": 0, "y": 0, "w": 300, "h": 200},
            {"x": 20, "y": 10, "w": 260, "h": 180},
            -3_250,
            colour_mode="keep",
            post_crop_box={"x": 9, "y": 7, "w": 240, "h": 160},
            fill=[230],
        ),
    ],
    ids=["first-order", "second-order"],
)
def test_a_point_on_the_sealed_page_maps_back_to_its_master(part):
    _maps_back(part, tolerance=0.5)


@pytest.mark.parametrize("rotation", [90_000, -90_000, 180_000, 0])
@pytest.mark.parametrize("crop_height", [170, 171])
@pytest.mark.parametrize("second_order", [False, True], ids=["first-order", "second-order"])
def test_a_quarter_turned_page_maps_back_exactly_whatever_its_crop_parity(
    rotation, crop_height, second_order
):
    """Pillow transposes a quarter turn; the map must be that transpose, exact, also when
    the crop's width and height differ by an odd number."""
    extra = (
        {"post_crop_box": {"x": -3, "y": 2, "w": 190, "h": 185}, "fill": [230]}
        if second_order
        else {}
    )
    part = triage.make_part(
        {"x": 100, "y": 0, "w": 200, "h": 200},
        {"x": 10, "y": 15, "w": 180, "h": crop_height},
        rotation,
        colour_mode="keep",
        **extra,
    )
    _maps_back(part, tolerance=0.01)


def _maps_back(part, *, tolerance):
    dots = [(150, 40), (170, 160), (230, 100), (260, 50)]
    master = _png(_dots(Image.new("L", (300, 200), 230), dots))

    rendered, _ = render_triage_derivative(master, page_index=0, part=part)

    centres = _centres(Image.open(BytesIO(rendered)))
    assert len(centres) == len(dots)
    for centre in centres:
        x, y = triage_point_to_frame(part, centre)
        nearest = min(dots, key=lambda dot: (dot[0] + 0.5 - x) ** 2 + (dot[1] + 0.5 - y) ** 2)
        assert abs(nearest[0] + 0.5 - x) < tolerance and abs(nearest[1] + 0.5 - y) < tolerance


def _contract(part, master):
    row = _row(part, master=master)
    sealed, geometry = render_triage_derivative(master, page_index=0, part=part)
    order = row["split"]["operation_order"]
    contract = {
        **imaging_library_versions(),
        "source_mode": geometry["source_mode"],
        "source_bands": geometry["source_bands"],
        "mode_transform": triage_mode_transform(
            order, geometry["source_mode"], geometry["color_mode"]
        ),
        "output": {"codec": "png", "color_mode": geometry["color_mode"]},
        "container_page_index": 0,
        "width": geometry["width"],
        "height": geometry["height"],
        "deterministic_encoder": "common.imaging.encode_image_deterministic-v1",
        "derivative_page": {
            "kind": "sealed-derivative-page-v1",
            "parent_frame_sha256": row["source_frame_sha256"],
            "parent_frame_page_index": 0,
            "triage_manifest_row": row,
            "triage_backlink": triage.derivative_page_backlink(row, 0),
            "operation_order": order,
            "apply_recipe": dict(triage_apply_recipe(order)),
            "operations": triage_operations(part, order),
        },
    }
    parent = {"sha256": row["source_frame_sha256"], "stored_at": "x", "source_frame_index": 0}
    return contract, parent, sealed


def test_the_exemplar_re_derives_a_second_order_page_and_holds_it_to_its_recipe():
    from common.exemplar_boundary import verify_triage_derivative

    master = _png(_dots(Image.new("L", (300, 200), 230), [(150, 100)]))
    contract, parent, sealed = _contract(_v2_part(), master)
    assert contract["derivative_page"]["operations"][3:] == [
        {"operation": "post-crop", "bounds": _v2_part()["post_crop_box"]},
        {"operation": "fill", "fill": {"levels": [180]}},
        {"operation": "convert", "colour_mode": "keep"},
    ]

    verify_triage_derivative(contract, master, digest_bytes(master), parent, digest_bytes(sealed))

    older = copy.deepcopy(contract)
    older["derivative_page"]["apply_recipe"] = dict(TRIAGE_APPLY_RECIPE)
    with pytest.raises(ContractError, match="apply recipe"):
        verify_triage_derivative(older, master, digest_bytes(master), parent, digest_bytes(sealed))


@pytest.mark.parametrize(
    "post_crop_box",
    [
        {"x": 5000, "y": 5000, "w": 300, "h": 200},  # misses the scan: a page of fill
        {"x": -100, "y": 0, "w": 300, "h": 200},  # a third of the canvas's width beyond it
        {"x": 0, "y": 0, "w": 300, "h": 260},  # 60 px, three tenths of its height, beyond
    ],
)
def test_a_post_crop_that_misses_or_runs_far_past_the_scan_is_refused(post_crop_box):
    master = _png(Image.new("L", (300, 200), 230))
    part = triage.make_part(
        {"x": 0, "y": 0, "w": 300, "h": 200},
        {"x": 0, "y": 0, "w": 300, "h": 200},
        0,
        colour_mode="keep",
        post_crop_box=post_crop_box,
        fill=[180],
    )
    with pytest.raises(ValueError, match="post-crop"):
        render_triage_derivative(master, page_index=0, part=part)


def test_a_post_crop_may_run_a_margins_width_past_the_scan():
    master = _png(Image.new("L", (300, 200), 230))
    part = triage.make_part(
        {"x": 0, "y": 0, "w": 300, "h": 200},
        {"x": 0, "y": 0, "w": 300, "h": 200},
        0,
        colour_mode="keep",
        post_crop_box={"x": -40, "y": -30, "w": 380, "h": 260},
        fill=[180],
    )
    rendered, _ = render_triage_derivative(master, page_index=0, part=part)
    assert Image.open(BytesIO(rendered)).size == (380, 260)


@pytest.mark.parametrize(
    ("mode", "paper", "levels"),
    [("1", 0, [128]), ("LA", (10, 255), [200, 128]), ("RGBA", (10, 20, 30, 255), [9, 9, 9, 0])],
)
def test_a_fill_the_mode_cannot_carry_as_recorded_is_refused(mode, paper, levels):
    """A bilevel page holds only 0 or 255, and rotation premultiplies a fill whose alpha is
    below 255, so neither would come out as the row records it."""
    master = _png(Image.new(mode, (31, 23), paper))
    part = triage.make_part(
        {"x": 0, "y": 0, "w": 31, "h": 23},
        {"x": 0, "y": 0, "w": 31, "h": 23},
        -5_000,
        colour_mode="keep",
        post_crop_box={"x": -3, "y": -3, "w": 35, "h": 28},
        fill=levels,
    )
    with pytest.raises(ValueError, match="fill"):
        render_triage_derivative(master, page_index=0, part=part)


@pytest.mark.parametrize(
    ("mode", "paper", "levels"), [("1", 0, [255]), ("LA", (10, 255), [200, 255])]
)
def test_a_fill_the_mode_carries_is_every_pixel_beyond_the_crop(mode, paper, levels):
    master = _png(Image.new(mode, (31, 23), paper))
    part = triage.make_part(
        {"x": 0, "y": 0, "w": 31, "h": 23},
        {"x": 0, "y": 0, "w": 31, "h": 23},
        -5_000,
        colour_mode="keep",
        post_crop_box={"x": -3, "y": -3, "w": 35, "h": 28},
        fill=levels,
    )
    rendered, _ = render_triage_derivative(master, page_index=0, part=part)
    page = Image.open(BytesIO(rendered))
    expected = levels[0] if len(levels) == 1 else tuple(levels)
    assert page.getpixel((0, 0)) == expected


@pytest.mark.parametrize("rotation", [0, 90_000, -90_000, 180_000, 1_234, -45_000, 179_999])
@pytest.mark.parametrize("crop", [(31, 23), (180, 171), (260, 180)])
def test_the_canvas_size_checked_before_rendering_is_the_one_the_renderer_makes(rotation, crop):
    from common.imaging import triage_rotated_canvas_size

    part = triage.make_part(
        {"x": 0, "y": 0, "w": 300, "h": 200},
        {"x": 0, "y": 0, "w": crop[0], "h": crop[1]},
        rotation,
        colour_mode="keep",
    )
    rotated = Image.new("L", crop).rotate(-rotation / 1000, expand=True)
    assert triage_rotated_canvas_size(part) == rotated.size
