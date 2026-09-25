"""Shared import support for tests that exercise the Designator program in process."""

import importlib.util
from pathlib import Path
from types import ModuleType

from common.components import Component
from common.contracts.errors import ContractError

ROOT = Path(__file__).resolve().parents[2]


def load_designator(module_name: str) -> ModuleType:
    """Load ``run.py`` under a caller-owned name without duplicating import mechanics."""
    path = ROOT / "pipeline" / "2_designator" / "run.py"
    spec = importlib.util.spec_from_file_location(module_name, path)
    if spec is None or spec.loader is None:  # pragma: no cover - a broken Python import runtime
        raise RuntimeError(f"could not create an import specification for {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def label_components_reference(pixels: set, *, gap_tolerance_px: int) -> list[Component]:
    """The retired per-pixel union-find labeller: the oracle `label_components` must match."""
    if gap_tolerance_px < 0:
        raise ContractError(f"gap tolerance {gap_tolerance_px} is negative")
    if not pixels:
        return []

    parent: dict[tuple[int, int], tuple[int, int]] = {pixel: pixel for pixel in pixels}

    def find(pixel: tuple[int, int]) -> tuple[int, int]:
        root = pixel
        while parent[root] != root:
            root = parent[root]
        while parent[pixel] != root:
            parent[pixel], pixel = root, parent[pixel]
        return root

    def union(a: tuple[int, int], b: tuple[int, int]) -> None:
        root_a, root_b = find(a), find(b)
        if root_a != root_b:
            parent[root_a] = root_b

    # `gap_tolerance_px` counts empty pixels allowed *between* two ink pixels,
    # so even a tolerance of 0 must still reach an immediately adjacent pixel
    # (distance 1) -- the Chebyshev search radius is one more than the gap.
    radius = gap_tolerance_px + 1
    # Only the forward half of the neighbourhood (dy > 0, or dy == 0 and
    # dx > 0): union is symmetric, so checking both halves would just union
    # the same pair twice for every neighbouring ink pixel.
    offsets = [
        (dx, dy)
        for dy in range(0, radius + 1)
        for dx in range(-radius, radius + 1)
        if (dy > 0) or (dy == 0 and dx > 0)
    ]
    for x, y in pixels:
        for dx, dy in offsets:
            neighbour = (x + dx, y + dy)
            if neighbour in pixels:
                union((x, y), neighbour)

    members: dict[tuple[int, int], list[tuple[int, int]]] = {}
    for pixel in pixels:
        members.setdefault(find(pixel), []).append(pixel)

    components: list[tuple[Component, tuple[tuple[int, int], ...]]] = []
    for group in members.values():
        xs = [x for x, _ in group]
        ys = [y for _, y in group]
        x0, x1 = min(xs), max(xs)
        y0, y1 = min(ys), max(ys)
        components.append(
            (
                {
                    "bounds": {"x": x0, "y": y0, "w": x1 - x0 + 1, "h": y1 - y0 + 1},
                    "pixel_count": len(group),
                },
                # Breaks a tie between two components sharing the same
                # (top, left) origin, deterministically, by the ink itself
                # rather than by set/dict construction order.
                tuple(sorted(group)),
            )
        )
    components.sort(key=lambda entry: (entry[0]["bounds"]["y"], entry[0]["bounds"]["x"], entry[1]))
    return [component for component, _members in components]
