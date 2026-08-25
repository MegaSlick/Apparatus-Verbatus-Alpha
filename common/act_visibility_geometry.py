"""Unit 19C's real geometric adapter: page-pixel occlusion polygons projected
into the small-integer cell surface ``common/cross_capture_coverage.py``
unions across captures.

This is deliberately independent of ``pipeline/2_designator/geometry_layer.py``
-- a stage may not import another stage's own module (that module's own
docstring records the one prior exception, Chandra custody, moving to
``common/`` for exactly this reason). It is also deliberately independent of
that module's ``resolve()``/``_derive_resolution()`` page-wide rule ("ANY
occlusion on the page marks EVERY proposal on that page 'review'"), which
stays exactly as conservative, and exactly as unconnected to this survey, as
before (consult: "Keep that conservative page-wide review rule. Add a
separate, explicit act-surface visibility measurement for cross-capture
union"). This module is that separate measurement: an exact geometric
classification, not a coarse presence flag.

No real image-registration matrix exists anywhere in this repository -- two
captures of one physical page are photographed independently, so their pixel
grids are not directly comparable. Absent that, this adapter projects each
capture's own act-surface AABB onto a small NxN grid **normalized to that
capture's own bounds**, and classifies each cell by its fractional position
within the capture's own footprint. Grid cell (c, r) therefore means "the
same relative position within each capture's own act region," never one
shared absolute pixel frame -- an explicit, narrower claim than pixel-exact
registration, which nothing in this codebase can currently make.

**That narrower claim is exactly why these cells may not be unioned across
captures.** Consult §4.1 admits a cross-capture union only over masks "mapped
through sealed geometric alignment", and the complementary-coverage case is
the one this normalization gets wrong rather than merely coarsely: where two
captures each expose a different part of one act, each capture's AABB bounds
only the part it shows, so both classify their own 16 cells `visible` and a
naive union reports the whole surface seen when neither capture ever showed
half of it. One capture measured against its own footprint is a real, sound
measurement; two captures' cells are not comparable until a registration
lands. ``pipeline/5_recensor/run.py::act_cross_capture_coverage`` enforces
that boundary and holds the multi-capture component `unresolved` instead.
"""

from __future__ import annotations

from math import isclose

GRID: int = 4


def _point_in_polygon(x: float, y: float, polygon: list[dict[str, int]]) -> bool:
    """Even-odd ray-casting membership test over a closed polygon ring."""
    inside = False
    count = len(polygon)
    for index in range(count):
        x1, y1 = polygon[index]["x"], polygon[index]["y"]
        x2, y2 = polygon[(index + 1) % count]["x"], polygon[(index + 1) % count]["y"]
        if (y1 > y) != (y2 > y):
            x_at_y = x1 + (y - y1) * (x2 - x1) / (y2 - y1)
            if x < x_at_y:
                inside = not inside
    return inside


def _orientation(a: tuple[float, float], b: tuple[float, float], c: tuple[float, float]) -> float:
    return (b[0] - a[0]) * (c[1] - a[1]) - (b[1] - a[1]) * (c[0] - a[0])


def _on_segment(a: tuple[float, float], b: tuple[float, float], point: tuple[float, float]) -> bool:
    return (
        isclose(_orientation(a, b, point), 0.0, abs_tol=1e-9)
        and min(a[0], b[0]) <= point[0] <= max(a[0], b[0])
        and min(a[1], b[1]) <= point[1] <= max(a[1], b[1])
    )


def _segments_intersect(
    a: tuple[float, float],
    b: tuple[float, float],
    c: tuple[float, float],
    d: tuple[float, float],
) -> bool:
    orientations = (
        _orientation(a, b, c),
        _orientation(a, b, d),
        _orientation(c, d, a),
        _orientation(c, d, b),
    )
    if orientations[0] * orientations[1] < 0 and orientations[2] * orientations[3] < 0:
        return True
    return any(
        isclose(orientation, 0.0, abs_tol=1e-9) and _on_segment(start, end, point)
        for orientation, start, end, point in (
            (orientations[0], a, b, c),
            (orientations[1], a, b, d),
            (orientations[2], c, d, a),
            (orientations[3], c, d, b),
        )
    )


def _polygon_intersects_cell(
    polygon: list[dict[str, int]], *, x0: float, y0: float, x1: float, y1: float
) -> bool:
    """Whether any part of an occlusion touches one grid cell.

    A centre-point sample can miss a narrow occluder wholly contained inside
    the cell and then publish that cell as visible.  The coverage record needs
    the conservative implication in the other direction: a cell is visible
    only when the complete polygon geometry is disjoint from it.
    """
    rectangle = ((x0, y0), (x1, y0), (x1, y1), (x0, y1))
    vertices = tuple((point["x"], point["y"]) for point in polygon)
    if any(x0 <= x <= x1 and y0 <= y <= y1 for x, y in vertices):
        return True
    if any(_point_in_polygon(x, y, polygon) for x, y in rectangle):
        return True
    polygon_edges = tuple(zip(vertices, vertices[1:] + vertices[:1], strict=True))
    rectangle_edges = tuple(zip(rectangle, rectangle[1:] + rectangle[:1], strict=True))
    return any(
        _segments_intersect(poly_start, poly_end, cell_start, cell_end)
        for poly_start, poly_end in polygon_edges
        for cell_start, cell_end in rectangle_edges
    )


def expected_surface_cells(grid: int = GRID) -> list[list[int]]:
    """The complete cell surface one capture's act footprint is classified over.

    Named separately from ``classify_capture_visibility`` because a caller that
    cannot measure a capture at all still owes its component an expected
    surface: an unresolved survey classifies no cell, and a component whose
    expected surface came from whichever capture happened to be measured first
    would be a silent collapse (consult §7.14).
    """
    return [[col, row] for row in range(grid) for col in range(grid)]


def classify_capture_visibility(
    *, bounds: dict[str, int], occlusion_polygons: list[list[dict[str, int]]], grid: int = GRID
) -> dict[str, object]:
    """One capture's exact visible/occluded classification over its own AABB.

    ``occlusion_polygons`` are real page-pixel polygons; the caller has
    already excluded any whose ``z_relationship`` positively proves they do
    not occlude the ink (``below-ink``). Every other relationship
    (``above-ink``, ``unknown``) is treated as occluding here, exactly as
    conservatively as the existing page-wide rule treats any occlusion at all.
    """
    if not isinstance(bounds, dict) or set(bounds) != {"x", "y", "w", "h"}:
        raise ValueError("act-visibility bounds must be a closed x/y/w/h rectangle")
    if bounds["w"] <= 0 or bounds["h"] <= 0:
        raise ValueError("act-visibility bounds must have positive extent")
    expected = expected_surface_cells(grid)
    visible: list[list[int]] = []
    occluded: list[list[int]] = []
    for row in range(grid):
        for col in range(grid):
            x0 = bounds["x"] + col * bounds["w"] / grid
            y0 = bounds["y"] + row * bounds["h"] / grid
            x1 = bounds["x"] + (col + 1) * bounds["w"] / grid
            y1 = bounds["y"] + (row + 1) * bounds["h"] / grid
            hit = any(
                _polygon_intersects_cell(polygon, x0=x0, y0=y0, x1=x1, y1=y1)
                for polygon in occlusion_polygons
            )
            (occluded if hit else visible).append([col, row])
    return {
        "expected_cells": expected,
        "visible_cells": visible,
        "occluded_cells": occluded,
        "visibility_state": "occluded" if occluded else "visible",
    }
