"""How many pages would qualify for crops on request, counted without a model.

The Perlector reads a whole page from a render capped at `[page_context]
maximum_edge`, which its chair's processor may shrink again (`smart_resize`).
Crops on request would let it ask, in a second round, for up to k regions of the
sealed page at native resolution. Per page of a RecordGold page manifest this
counts:

- the native size, the render the page request sends, and the size the chair
  sees after its processor (`request_capacity`'s own arithmetic);
- the gain: how many times finer, linearly, the chair sees a crop than it sees
  the page, after its processor has resized both;
- k: how many crops round two fits in the sealed Perlector row (`ROUND_TWO`);
- whether the page qualifies: gain at least `--min-gain` and k at least 1.

The page request is the one `perlector_request_fit.py` builds: the page read
under the sealed feed, with its gold text standing in for three witnesses and
Surya lines estimated from it. Native sizes are the manifest's `width` and
`height`, taken to be the sealed page's; no image is opened and nothing leaves
the machine.

    python -m operations.corpus.crop_census PAGE_MANIFEST.jsonl --out census.json
"""

from __future__ import annotations

import argparse
import json
import math
import sys
from pathlib import Path
from typing import Any, Final, Sequence

from common import page_prompt
from common.contracts.canonical import digest_bytes
from common.contracts.errors import SchemaRefusal
from common.page_feed import request_image_sizes
from common.page_render import render_size
from common.request_capacity import (
    CHAT_IMAGE_TOKENS,
    CHAT_TURN_TOKENS,
    RequestCapacityRefusal,
    request_fits,
    row_image_geometry,
    smart_resize,
)
from operations.corpus.perlector_request_fit import (
    ROOT,
    page_feed_for,
    page_request,
    page_shape,
    perlector_row,
    sealed_protocol,
)

SCHEMA: Final = "verbatus-crop-census.v1"
BASIS_POINTS: Final = 10_000
QUANTILES_BP: Final = (0, 1000, 2500, 5000, 7500, 9000, 10000)
ROUND_TWO_TURNS: Final = 2
ROUND_TWO: Final = (
    "the admitted page request, its answer reserve again as the round-one reply "
    "carried in context, two more chat turns (the carried reply and the new request), "
    "and k crops at their image tokens plus the chat template's tokens per image; "
    "answered within the same reserve. k never takes the request past the protocol's "
    "max_images. Wording that asks for the crops is not charged beyond the turns"
)
ESTIMATE: Final = (
    "estimates only: gold text stands in for the witnesses, and round two has never "
    "been measured against the engine"
)
CONFIGS: Final = (
    "config/perlector_protocol.toml",
    "config/serving_recipes_real.toml",
    "config/decoding.toml",
)

DEFAULT_MIN_GAIN: Final = 1.5
DEFAULT_CROP_WIDTH: Final = 0.5
DEFAULT_CROP_HEIGHT: Final = 0.125
DEFAULT_MAX_CROPS: Final = 8


def _bp(fraction: float, what: str) -> int:
    if not 0 < fraction <= 1:
        raise ValueError(f"{what} must be above 0 and at most 1, not {fraction}")
    value = round(fraction * BASIS_POINTS)
    if value == 0:
        raise ValueError(f"{what} {fraction} is under one basis point of the page")
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


def gain_bp(
    native: tuple[int, int],
    seen: tuple[int, int],
    crop: tuple[int, int] | None = None,
    crop_seen: tuple[int, int] | None = None,
) -> int:
    """Linear gain in basis points, rounded down: native pixels over the page's seen
    pixels, times the crop's own seen-over-native scale when given. Neither side counts
    an enlargement by the processor, which adds no detail."""
    if (crop is None) != (crop_seen is None):
        raise ValueError("a crop's gain needs both its native and its seen size")
    native_area = native[0] * native[1]
    numerator, denominator = native_area, min(native_area, seen[0] * seen[1])
    if crop is not None and crop_seen is not None:
        crop_area = crop[0] * crop[1]
        numerator *= min(crop_area, crop_seen[0] * crop_seen[1])
        denominator *= crop_area
    return math.isqrt(numerator * BASIS_POINTS**2 // denominator)


def admitted_crops(row: Any, capacity: dict[str, Any], crop: tuple[int, int], cap: int) -> int:
    """The most crops, up to `cap`, that round two (`ROUND_TWO`) fits."""
    images = [(image["width"], image["height"]) for image in capacity["images"]]
    reserve = capacity["answer_budget"]
    carried = capacity["prompt_tokens"] + reserve + ROUND_TWO_TURNS * CHAT_TURN_TOKENS
    k = 0
    while k < cap:
        record = request_fits(
            row,
            images + [crop] * (k + 1),
            carried + CHAT_IMAGE_TOKENS * (k + 1),
            reserve,
            prompt_tokens_basis=capacity["prompt_tokens_basis"],
        )
        if not record["fits"]:
            break
        k += 1
    return k


def _checked_pages(pages: list[Any]) -> None:
    """Refuse a manifest line the census cannot count unambiguously."""
    seen_ids: set[str] = set()
    for number, page in enumerate(pages, start=1):
        if not isinstance(page, dict):
            raise SchemaRefusal(f"manifest line {number} is not an object")
        page_id = page.get("page_id")
        if not isinstance(page_id, str) or not page_id:
            raise SchemaRefusal(f"manifest line {number} has no page_id")
        if page_id in seen_ids:
            raise SchemaRefusal(f"page_id {page_id!r} appears twice in the manifest")
        seen_ids.add(page_id)
        for side in ("width", "height"):
            value = page.get(side)
            if not isinstance(value, int) or isinstance(value, bool) or value < 1:
                raise SchemaRefusal(f"page {page_id!r} has no positive integer {side}")
        if not isinstance(page.get("records"), list):
            raise SchemaRefusal(f"page {page_id!r} has no records list")


def census_page(
    row: Any, sealed: dict[str, Any], page: dict[str, Any], options: dict[str, int]
) -> dict[str, Any]:
    """One page's counts."""
    if sealed["feed"]["page_image"] != "legible":
        raise SchemaRefusal(
            f"the census counts the legible page render; the sealed feed sends "
            f"page_image = {sealed['feed']['page_image']!r}"
        )
    native = (page["width"], page["height"])
    shape = page_shape(page)
    feed = page_feed_for(sealed, shape, {})
    sent = request_image_sizes(feed)
    render = render_size(native, sealed["page_context"]["maximum_edge"])
    if sent[:1] != [render]:
        raise SchemaRefusal(
            f"the page request sends {sent} for a {native} page, but the page render is {render}"
        )
    seen = seen_size(row, render)
    crop = crop_size(native, options["crop_width_bp"], options["crop_height_bp"])
    crop_seen = seen_size(row, crop)
    gain = gain_bp(native, seen, crop, crop_seen)
    entry = {
        "page_id": page["page_id"],
        "native": list(native),
        "sent": list(render),
        "seen": list(seen),
        "page_gain_bp": gain_bp(native, seen),
        "crop_native": list(crop),
        "crop_seen": list(crop_seen),
        "gain_bp": gain,
        "skipped_records": shape["skipped_records"],
    }
    try:
        request = page_request(row, feed)
    except RequestCapacityRefusal as refusal:
        if refusal.capacity is None:
            raise
        return {
            **entry,
            "page_request": "refused",
            "need": refusal.capacity["need"],
            "headroom": refusal.capacity["headroom"],
            "k": 0,
            "k_cap": 0,
            "k_capped": False,
            "reserve_clamped": False,
            "qualifies": False,
        }
    capacity = request["capacity"]
    cap = max(0, min(options["max_crops"], sealed["max_images"] - len(capacity["images"])))
    k = admitted_crops(row, capacity, crop, cap)
    return {
        **entry,
        "page_request": "admitted",
        "need": capacity["need"],
        "headroom": capacity["headroom"],
        "k": k,
        "k_cap": cap,
        "k_capped": k == cap,
        "reserve_clamped": request["answer_reserve"]["reserve_clamped"],
        "qualifies": gain >= options["min_gain_bp"] and k >= 1,
    }


def quantiles(values: Sequence[int]) -> list[dict[str, int | None]]:
    """Nearest-rank quantiles, `q` in basis points of rank."""
    ordered = sorted(values)
    out: list[dict[str, int | None]] = []
    for q in QUANTILES_BP:
        rank = max(1, -(-q * len(ordered) // BASIS_POINTS))
        out.append({"q": q, "value": ordered[rank - 1] if ordered else None})
    return out


def census(pages: list[dict[str, Any]], options: dict[str, int], manifest_sha256: str) -> dict:
    """The whole report: inputs, per-page counts and the summary."""
    _checked_pages(pages)
    row, sealed = perlector_row(), sealed_protocol()
    entries = sorted(
        (census_page(row, sealed, page, options) for page in pages),
        key=lambda entry: entry["page_id"],
    )
    qualifying = sum(entry["qualifies"] for entry in entries)
    geometry = row_image_geometry(row)
    return {
        "schema": SCHEMA,
        "inputs": {
            **options,
            "manifest_sha256": manifest_sha256,
            "config_sha256": {path: digest_bytes((ROOT / path).read_bytes()) for path in CONFIGS},
            "page_prompt_builder_sha256": page_prompt.BUILDER_SHA256,
            "round_two": ROUND_TWO,
            "estimate": ESTIMATE,
            "chat_turn_tokens": CHAT_TURN_TOKENS,
            "chat_image_tokens": CHAT_IMAGE_TOKENS,
            "max_images": sealed["max_images"],
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
            "k_histogram": [
                {"k": k, "pages": sum(entry["k"] == k for entry in entries)}
                for k in range(options["max_crops"] + 1)
            ],
            "k_capped": sum(entry["k_capped"] for entry in entries),
            "reserve_clamped": sum(entry["reserve_clamped"] for entry in entries),
            "skipped_records": sum(entry["skipped_records"] for entry in entries),
        },
    }


def summary_text(report: dict[str, Any]) -> str:
    summary, inputs = report["summary"], report["inputs"]
    gains = ", ".join(
        f"p{cell['q'] // 100}="
        + ("-" if cell["value"] is None else f"{cell['value'] / BASIS_POINTS:.2f}")
        for cell in summary["gain_bp_quantiles"]
    )
    return "\n".join(
        [
            f"{summary['pages']} pages; {summary['page_request_refused']} page requests refused "
            f"at {inputs['max_model_len']} tokens",
            f"qualifying (gain >= {inputs['min_gain_bp'] / BASIS_POINTS:.2f}, k >= 1): "
            f"{summary['qualifying']} ({summary['qualifying_bp'] / 100:.2f}%)",
            f"gain quantiles: {gains}",
            "k histogram: "
            + ", ".join(f"{cell['k']}: {cell['pages']}" for cell in summary["k_histogram"])
            + f" ({summary['k_capped']} reached their cap)",
            f"{summary['reserve_clamped']} answer reserves clamped to the page cap; "
            f"{summary['skipped_records']} gold records left out of the page requests",
            inputs["estimate"],
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
    if not (math.isfinite(args.min_gain) and args.min_gain >= 1) or args.max_crops < 1:
        parser.error("--min-gain must be a finite number at least 1 and --max-crops at least 1")
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
    try:
        report = census(pages, options, digest_bytes(body))
    except (SchemaRefusal, RequestCapacityRefusal) as refusal:
        print(f"crop census refused: {refusal}", file=sys.stderr)
        return 2
    args.out.write_text(
        json.dumps(report, sort_keys=True, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    print(summary_text(report))
    return 0


if __name__ == "__main__":
    sys.exit(main())
