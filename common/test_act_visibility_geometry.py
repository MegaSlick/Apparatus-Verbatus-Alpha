"""Unit 19C's pure geometric adapter: exact, not conservative-page-wide."""

from __future__ import annotations

import pytest

from common.act_visibility_geometry import GRID, classify_capture_visibility, expected_surface_cells

BOUNDS = {"x": 0, "y": 0, "w": 40, "h": 40}


def _rectangle(x0: int, y0: int, x1: int, y1: int) -> list[dict[str, int]]:
    return [{"x": x0, "y": y0}, {"x": x1, "y": y0}, {"x": x1, "y": y1}, {"x": x0, "y": y1}]


def test_no_occlusion_polygon_leaves_every_cell_visible():
    result = classify_capture_visibility(bounds=BOUNDS, occlusion_polygons=[])
    assert result["visibility_state"] == "visible"
    assert result["occluded_cells"] == []
    assert len(result["visible_cells"]) == GRID * GRID
    assert len(result["expected_cells"]) == GRID * GRID


def test_a_polygon_enclosing_the_whole_bounds_occludes_every_cell():
    polygon = _rectangle(-1, -1, 41, 41)
    result = classify_capture_visibility(bounds=BOUNDS, occlusion_polygons=[polygon])
    assert result["visibility_state"] == "occluded"
    assert sorted(result["occluded_cells"]) == sorted(result["expected_cells"])
    assert result["visible_cells"] == []


def test_a_polygon_over_one_corner_leaves_the_rest_visible():
    """A quarter of the bounds occluded -- a real partial-coverage sliver."""
    polygon = _rectangle(-1, -1, 21, 21)
    result = classify_capture_visibility(bounds=BOUNDS, occlusion_polygons=[polygon])
    assert result["visibility_state"] == "occluded"
    assert [0, 0] in result["occluded_cells"]
    assert [GRID - 1, GRID - 1] in result["visible_cells"]
    assert set(map(tuple, result["occluded_cells"])) | set(
        map(tuple, result["visible_cells"])
    ) == set(map(tuple, result["expected_cells"]))


def test_a_narrow_polygon_inside_one_cell_cannot_hide_between_sample_points():
    """Visibility describes the cell surface, not only its centre point.

    The polygon fits wholly inside the upper-left 10x10 cell without covering
    its centre.  A centre-point classifier called all sixteen cells visible,
    silently dropping a real occlusion from the union denominator.
    """
    polygon = _rectangle(1, 1, 4, 4)
    result = classify_capture_visibility(bounds=BOUNDS, occlusion_polygons=[polygon])
    assert result["visibility_state"] == "occluded"
    assert [0, 0] in result["occluded_cells"]
    assert len(result["visible_cells"]) < GRID * GRID


def test_expected_cells_are_grid_indices_not_pixel_coordinates():
    """Two different-sized bounds still publish the identical grid index set.

    This is the adapter's explicit, documented projection: no real image
    registration exists in this repository, so cell identity is normalized
    position within each capture's own AABB, never a shared absolute pixel
    frame. The identical index set therefore does *not* make two captures'
    cells comparable; the Recensor refuses that union until a sealed
    registration exists.
    """
    small = classify_capture_visibility(
        bounds={"x": 5, "y": 5, "w": 8, "h": 8}, occlusion_polygons=[]
    )
    large = classify_capture_visibility(
        bounds={"x": 100, "y": 200, "w": 800, "h": 240}, occlusion_polygons=[]
    )
    assert small["expected_cells"] == large["expected_cells"]


def test_bounds_must_be_a_closed_positive_rectangle():
    with pytest.raises(ValueError):
        classify_capture_visibility(bounds={"x": 0, "y": 0, "w": 0, "h": 10}, occlusion_polygons=[])
    with pytest.raises(ValueError):
        classify_capture_visibility(bounds={"x": 0, "y": 0, "w": 10}, occlusion_polygons=[])


@pytest.mark.parametrize("grid", [0, -1, True, 1.5])
def test_grid_must_be_a_positive_non_boolean_integer(grid):
    with pytest.raises(ValueError, match="positive integer"):
        expected_surface_cells(grid)


@pytest.mark.parametrize(
    "bounds",
    [
        {"x": 0, "y": 0, "w": "10", "h": 10},
        {"x": False, "y": 0, "w": 10, "h": 10},
        {"x": -1, "y": 0, "w": 10, "h": 10},
    ],
)
def test_bounds_refuse_non_integer_or_negative_page_coordinates(bounds):
    with pytest.raises(ValueError, match="bounds"):
        classify_capture_visibility(bounds=bounds, occlusion_polygons=[])


@pytest.mark.parametrize(
    "polygons",
    [
        None,
        [[{"x": 0}]],
        [[{"x": 0, "y": 0}, {"x": 0, "y": 0}, {"x": 0, "y": 0}]],
    ],
)
def test_malformed_occlusion_geometry_is_a_named_value_refusal(polygons):
    with pytest.raises(ValueError, match="occlusion"):
        classify_capture_visibility(bounds=BOUNDS, occlusion_polygons=polygons)
