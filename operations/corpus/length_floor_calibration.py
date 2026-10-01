"""Measure act density on a RecordGold-style page manifest, to judge the truncation floor.

`config/perlector_protocol.toml`'s `[truncation]` floor is a density: an act's
characters scaled from its region to its page's area. This prints that density's
distribution over every act in a manifest, over each act's gold box, and how many
honest acts each candidate floor would flag.

Read-only; prints numbers only, never a reading, a record id or a page.

    python operations/corpus/length_floor_calibration.py PAGE_MANIFEST.jsonl [FLOOR ...]

The manifest is one JSON object per page: `width`, `height` and `records`, each
record a `bbox` of [x, y, w, h] and its `text`.
"""

from __future__ import annotations

import json
import sys
from typing import Final

DEFAULT_FLOORS: Final = (50, 200, 400, 640)
EXIT_NO_USABLE_ACTS: Final = 3
PERCENTILES: Final = (("min", 0.0), ("p0.5", 0.5), ("p1", 1.0), ("p5", 5.0), ("median", 50.0))


def densities(pages: list[dict]) -> dict[str, list[float]]:
    """Characters per page-equivalent of every non-empty act, over its bare box."""
    out: dict[str, list[float]] = {"bare": []}
    for page in pages:
        size = (page["width"], page["height"])
        for record in page["records"]:
            characters = len(record["text"].strip())
            box = tuple(record["bbox"])
            if characters == 0 or box[2] <= 0 or box[3] <= 0:
                continue
            page_area = size[0] * size[1]
            out["bare"].append(characters * page_area / (box[2] * box[3]))
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
    with open(argv[0], encoding="utf-8") as handle:
        pages = [json.loads(line) for line in handle if line.strip()]
    floors = tuple(int(value) for value in argv[1:]) or DEFAULT_FLOORS
    found = densities(pages)
    if not found["bare"]:
        print("no usable acts: every act has empty text or a non-positive box", file=sys.stderr)
        return EXIT_NO_USABLE_ACTS
    print(report(found, floors))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
