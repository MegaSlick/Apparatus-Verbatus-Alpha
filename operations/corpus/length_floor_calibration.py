"""Measure act density on a RecordGold-style page manifest, to judge the truncation floor.

`config/perlector_protocol.toml`'s `[truncation]` floor is a density: an act's
characters scaled from its region to its page's area. This prints that density's
distribution over every act in a manifest, and how many honest acts each candidate
floor would flag, for three regions: the bare gold box, the box padded by
`config/designator_padding.toml` without the page-edge clamp, and the padded box
clamped to the page as the Designator clamps it (`geometry.apply_padding`).

Read-only; prints numbers only, never a reading, a record id or a page.

    python operations/corpus/length_floor_calibration.py PAGE_MANIFEST.jsonl [FLOOR ...]

The manifest is one JSON object per page: `width`, `height` and `records`, each
record a `bbox` of [x, y, w, h] and its `text`.
"""

from __future__ import annotations

import json
import sys
import tomllib
from pathlib import Path
from typing import Final

from common.background import round_half_up_bp

ROOT: Final = Path(__file__).resolve().parents[2]
DEFAULT_FLOORS: Final = (50, 200, 400, 640)
EXIT_NO_USABLE_ACTS: Final = 3
PERCENTILES: Final = (("min", 0.0), ("p0.5", 0.5), ("p1", 1.0), ("p5", 5.0), ("median", 50.0))


def padded_area(box: tuple[int, int, int, int], page: tuple[int, int], padding, *, clamp: bool):
    """The area of `box` padded by `padding` basis-point fractions of its own sides."""
    x, y, w, h = box
    x0, y0 = x - round_half_up_bp(w, padding["left_bp"]), y - round_half_up_bp(h, padding["top_bp"])
    x1 = x + w + round_half_up_bp(w, padding["right_bp"])
    y1 = y + h + round_half_up_bp(h, padding["bottom_bp"])
    if clamp:
        x0, y0, x1, y1 = max(0, x0), max(0, y0), min(page[0], x1), min(page[1], y1)
    return (x1 - x0) * (y1 - y0)


def densities(pages: list[dict], padding) -> dict[str, list[float]]:
    """Characters per page-equivalent of every non-empty act, for each region variant."""
    out: dict[str, list[float]] = {"bare": [], "padded-unclamped": [], "padded-clamped": []}
    for page in pages:
        size = (page["width"], page["height"])
        for record in page["records"]:
            characters = len(record["text"].strip())
            box = tuple(record["bbox"])
            if characters == 0 or box[2] <= 0 or box[3] <= 0:
                continue
            page_area = size[0] * size[1]
            out["bare"].append(characters * page_area / (box[2] * box[3]))
            for name, clamp in (("padded-unclamped", False), ("padded-clamped", True)):
                out[name].append(
                    characters * page_area / padded_area(box, size, padding, clamp=clamp)
                )
    return out


def percentile(values: list[float], p: float) -> float:
    ordered = sorted(values)
    position = (len(ordered) - 1) * p / 100
    low = int(position)
    high = min(low + 1, len(ordered) - 1)
    return ordered[low] + (ordered[high] - ordered[low]) * (position - low)


def report(found: dict[str, list[float]], floors: tuple[int, ...]) -> str:
    lines = []
    for name, values in found.items():
        cells = ", ".join(f"{label} {percentile(values, p):,.0f}" for label, p in PERCENTILES)
        lines.append(f"{name}: {len(values)} acts; {cells}")
        for floor in floors:
            flagged = sum(value < floor for value in values)
            lines.append(f"  floor {floor}: flags {flagged} ({100 * flagged / len(values):.2f}%)")
    return "\n".join(lines)


def main(argv: list[str]) -> int:
    if not argv:
        print(__doc__)
        return 2
    padding = tomllib.loads((ROOT / "config" / "designator_padding.toml").read_text("utf-8"))[
        "padding"
    ]
    with open(argv[0], encoding="utf-8") as handle:
        pages = [json.loads(line) for line in handle if line.strip()]
    floors = tuple(int(value) for value in argv[1:]) or DEFAULT_FLOORS
    found = densities(pages, padding)
    if not found["bare"]:
        print("no usable acts: every act has empty text or a non-positive box", file=sys.stderr)
        return EXIT_NO_USABLE_ACTS
    print(report(found, floors))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
