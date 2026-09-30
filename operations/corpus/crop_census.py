"""How many pages would qualify for crops on request, counted without a model.

The Perlector reads a whole page from a render capped at `[page_context]
maximum_edge`, which its chair's processor may shrink again (`smart_resize`).
Crops on request would let it ask, in a second round, for up to k regions of the
sealed page at native resolution. Per page of a RecordGold page manifest this
counts:

- the native size, the render the page request sends, and the size the chair
  sees after its processor (`request_capacity`'s own arithmetic);
- the gain: how many times finer, linearly, a native crop is than the page as
  the chair sees it;
- k: how many native crops a second round could add to the page request and
  still fit the sealed Perlector row. Round two is the page request exactly as
  admitted (same render, prompt charge and answer reserve) plus k crops, each
  charged its image tokens and the chat template's two tokens per image;
- whether the page qualifies: gain at least `--min-gain` and k at least 1.

The page request is the one `perlector_request_fit.py` builds: the page read
under the sealed feed, with its gold text standing in for three witnesses and
Surya lines estimated from it. Native sizes are the manifest's `width` and
`height`; no image is opened and nothing leaves the machine.

    python -m operations.corpus.crop_census PAGE_MANIFEST.jsonl --out census.json
"""

from __future__ import annotations

import argparse
import json
import math
import sys
from pathlib import Path
from typing import Any, Final, Sequence

from common.contracts.canonical import digest_bytes
from common.page_feed import request_image_sizes
from common.page_render import render_size
from common.request_capacity import (
    RequestCapacityRefusal,
    request_fits,
    row_image_geometry,
    smart_resize,
)
from operations.corpus.perlector_request_fit import (
    page_feed_for,
    page_request,
    page_shape,
    perlector_row,
    sealed_protocol,
)

SCHEMA: Final = "verbatus-crop-census.v1"
BASIS_POINTS: Final = 10_000
# Chat-template tokens each added image costs (`PERLECTOR_PAGE_PROMPT_OVERHEAD_TOKENS`).
TEMPLATE_TOKENS_PER_IMAGE: Final = 2
QUANTILES_BP: Final = (0, 1000, 2500, 5000, 7500, 9000, 10000)

DEFAULT_MIN_GAIN: Final = 1.5
DEFAULT_CROP_WIDTH: Final = 0.5
DEFAULT_CROP_HEIGHT: Final = 0.125
DEFAULT_MAX_CROPS: Final = 8


def _bp(fraction: float, what: str) -> int:
    value = round(fraction * BASIS_POINTS)
    if not 0 < value <= BASIS_POINTS:
        raise ValueError(f"{what} must be above 0 and at most 1, not {fraction}")
    return value


def crop_size(native: tuple[int, int], width_bp: int, height_bp: int) -> tuple[int, int]:
    """One crop's native `(width, height)`: the given share of each side, at least one pixel."""
    return (
        max(1, native[0] * width_bp // BASIS_POINTS),
        max(1, native[1] * height_bp // BASIS_POINTS),
    )


def seen_size(row: Any, size: tuple[int, int]) -> tuple[int, int]:
    """The `(width, height)` the chair's processor resizes an image of `size` to."""
    geometry = row_image_geometry(row)
    height, width = smart_resize(
        size[1],
        size[0],
        factor=geometry.patch_size * geometry.merge_size,
        min_pixels=geometry.min_pixels,
        max_pixels=geometry.max_pixels,
    )
    return width, height


def gain_bp(native: tuple[int, int], seen: tuple[int, int]) -> int:
    """The linear gain of native pixels over those seen, in basis points, rounded down."""
    return math.isqrt(native[0] * native[1] * BASIS_POINTS**2 // (seen[0] * seen[1]))


def admitted_crops(row: Any, capacity: dict[str, Any], crop: tuple[int, int], cap: int) -> int:
    """The most crops, up to `cap`, that round two can add to this admitted page request."""
    images = [(image["width"], image["height"]) for image in capacity["images"]]
    k = 0
    while k < cap:
        record = request_fits(
            row,
            images + [crop] * (k + 1),
            capacity["prompt_tokens"] + TEMPLATE_TOKENS_PER_IMAGE * (k + 1),
            capacity["answer_budget"],
            prompt_tokens_basis=capacity["prompt_tokens_basis"],
        )
        if not record["fits"]:
            break
        k += 1
    return k


def census_page(
    row: Any, sealed: dict[str, Any], page: dict[str, Any], options: dict[str, int]
) -> dict[str, Any]:
    """One page's counts."""
    native = (page["width"], page["height"])
    shape = page_shape(page)
    feed = page_feed_for(sealed, shape, {})
    sent = request_image_sizes(feed)
    render = render_size(native, sealed["page_context"]["maximum_edge"])
    if sent[:1] != [render]:
        raise RequestCapacityRefusal(
            f"the page request sends {sent} for a {native} page, but the page render is {render}"
        )
    seen = seen_size(row, render)
    crop = crop_size(native, options["crop_width_bp"], options["crop_height_bp"])
    entry = {
        "page_id": str(page.get("page_id")),
        "native": list(native),
        "sent": list(render),
        "seen": list(seen),
        "gain_bp": gain_bp(native, seen),
        "crop_native": list(crop),
        "crop_seen": list(seen_size(row, crop)),
        "skipped_records": shape["skipped_records"],
    }
    try:
        capacity = page_request(row, feed)["capacity"]
    except RequestCapacityRefusal as refusal:
        if refusal.capacity is None:
            raise
        return {
            **entry,
            "page_request": "refused",
            "need": refusal.capacity["need"],
            "headroom": refusal.capacity["headroom"],
            "k": 0,
            "qualifies": False,
        }
    k = admitted_crops(row, capacity, crop, options["max_crops"])
    return {
        **entry,
        "page_request": "admitted",
        "need": capacity["need"],
        "headroom": capacity["headroom"],
        "k": k,
        "qualifies": entry["gain_bp"] >= options["min_gain_bp"] and k >= 1,
    }


def quantiles(values: Sequence[int]) -> dict[str, int | None]:
    """Nearest-rank quantiles, keyed by basis points of rank."""
    ordered = sorted(values)
    out: dict[str, int | None] = {}
    for q in QUANTILES_BP:
        if not ordered:
            out[str(q)] = None
            continue
        rank = max(1, -(-q * len(ordered) // BASIS_POINTS))
        out[str(q)] = ordered[rank - 1]
    return out


def census(pages: list[dict[str, Any]], options: dict[str, int], manifest_sha256: str) -> dict:
    """The whole report: inputs, per-page counts and the summary."""
    row, sealed = perlector_row(), sealed_protocol()
    entries = sorted(
        (census_page(row, sealed, page, options) for page in pages),
        key=lambda entry: entry["page_id"],
    )
    qualifying = sum(entry["qualifies"] for entry in entries)
    histogram = {str(k): 0 for k in range(options["max_crops"] + 1)}
    for entry in entries:
        histogram[str(entry["k"])] += 1
    geometry = row_image_geometry(row)
    return {
        "schema": SCHEMA,
        "inputs": {
            **options,
            "manifest_sha256": manifest_sha256,
            "recipe": row.recipe,
            "max_model_len": row.max_model_len,
            "min_pixels": geometry.min_pixels,
            "max_pixels": geometry.max_pixels,
            "patch_size": geometry.patch_size,
            "merge_size": geometry.merge_size,
            "maximum_edge": sealed["page_context"]["maximum_edge"],
            "feed": sealed["feed"],
        },
        "pages": entries,
        "summary": {
            "pages": len(entries),
            "page_request_refused": sum(e["page_request"] == "refused" for e in entries),
            "qualifying": qualifying,
            "qualifying_bp": qualifying * BASIS_POINTS // len(entries) if entries else 0,
            "gain_bp_quantiles": quantiles([entry["gain_bp"] for entry in entries]),
            "k_histogram": histogram,
            "skipped_records": sum(entry["skipped_records"] for entry in entries),
        },
    }


def summary_text(report: dict[str, Any]) -> str:
    summary, inputs = report["summary"], report["inputs"]
    gains = ", ".join(
        f"p{int(q) // 100}={'-' if v is None else f'{v / BASIS_POINTS:.2f}'}"
        for q, v in summary["gain_bp_quantiles"].items()
    )
    return "\n".join(
        [
            f"{summary['pages']} pages; {summary['page_request_refused']} page requests refused "
            f"at {inputs['max_model_len']} tokens",
            f"qualifying (gain >= {inputs['min_gain_bp'] / BASIS_POINTS:.2f}, k >= 1): "
            f"{summary['qualifying']} ({summary['qualifying_bp'] / 100:.2f}%)",
            f"gain quantiles: {gains}",
            "k histogram: "
            + ", ".join(f"{k}: {count}" for k, count in summary["k_histogram"].items()),
            f"{summary['skipped_records']} gold records left out of the page requests",
        ]
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("manifest", type=Path, help="a RecordGold page_manifest.jsonl")
    parser.add_argument("--out", type=Path, required=True, help="where the JSON report goes")
    parser.add_argument("--min-gain", type=float, default=DEFAULT_MIN_GAIN)
    parser.add_argument(
        "--crop-width",
        type=float,
        default=DEFAULT_CROP_WIDTH,
        help="a crop's width as a share of the page's",
    )
    parser.add_argument(
        "--crop-height",
        type=float,
        default=DEFAULT_CROP_HEIGHT,
        help="a crop's height as a share of the page's",
    )
    parser.add_argument("--max-crops", type=int, default=DEFAULT_MAX_CROPS)
    args = parser.parse_args(argv)
    if args.min_gain < 1 or args.max_crops < 1:
        parser.error("--min-gain must be at least 1 and --max-crops at least 1")
    try:
        options = {
            "min_gain_bp": round(args.min_gain * BASIS_POINTS),
            "crop_width_bp": _bp(args.crop_width, "--crop-width"),
            "crop_height_bp": _bp(args.crop_height, "--crop-height"),
            "max_crops": args.max_crops,
        }
    except ValueError as error:
        parser.error(str(error))
    body = args.manifest.read_bytes()
    pages = [json.loads(line) for line in body.decode("utf-8").splitlines() if line.strip()]
    report = census(pages, options, digest_bytes(body))
    args.out.write_text(
        json.dumps(report, sort_keys=True, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    print(summary_text(report))
    return 0


if __name__ == "__main__":
    sys.exit(main())
