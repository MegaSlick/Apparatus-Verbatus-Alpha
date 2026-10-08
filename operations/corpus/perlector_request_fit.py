"""Whether each gold page's whole-page Perlector request fits the served 27B row.

For every page in a RecordGold-style page manifest this counts whether the
whole-page request fits `config/serving_recipes_real.toml`'s Perlector row at
32,768 and 65,536 tokens of context under the feed settings that matter most:
the default feed, no Surya detections, flat witness units and the page overlay.
Each page is fed as three witnesses reporting its gold text -- two in record
units with the gold boxes (as Chandra's blocks and DAI's records are), one in
lines with no box (as Churro's are) -- and one Surya line per gold text line,
boxed inside its record, with one Surya block per record. The Surya lines are
an estimate from the gold text, not detections. A gold record with no text or
no box inside the page is left out, and the count left out is printed.

Read-only; prints counts only, never a reading, a record id or a page.

    python operations/corpus/perlector_request_fit.py PAGE_MANIFEST.jsonl
"""

from __future__ import annotations

import json
import sys
from dataclasses import replace
from pathlib import Path
from typing import Any, Final

ROOT: Final = Path(__file__).resolve().parents[2]
_PERLECTOR_DIR = ROOT / "pipeline" / "4_perlector"
if str(_PERLECTOR_DIR) not in sys.path:
    sys.path.insert(0, str(_PERLECTOR_DIR))

import protocol  # noqa: E402

from common import page_feed, page_prompt  # noqa: E402
from common.contracts.canonical import digest_bytes  # noqa: E402
from common.decoding import (  # noqa: E402
    load_decoding_policy,
    perlector_page_generation,
    perlector_page_max_tokens,
)
from common.imaging import encode_grayscale_png_deterministic  # noqa: E402
from common.page_render import render_size  # noqa: E402
from common.request_capacity import (  # noqa: E402
    RequestCapacityRefusal,
    page_request_capacity,
)
from operations.serving.config import ServingProfile, load_serving_recipes  # noqa: E402


def perlector_row() -> ServingProfile:
    (row,) = [
        row
        for row in load_serving_recipes(ROOT / "config" / "serving_recipes_real.toml").profiles
        if isinstance(row, ServingProfile) and row.chair == "perlector"
    ]
    return row


def sealed_protocol() -> dict[str, Any]:
    return protocol.load(ROOT / "config" / "perlector_protocol.toml")[0]


PAGE_CONTEXTS: Final = (32_768, 65_536)
PAGE_SETTINGS: Final = {
    "default feed": {},
    "no Surya": {"surya_lines": False, "surya_blocks": False},
    "flat witness units": {"witness_units": "flat"},
    "page overlay": {"page_overlay": "boxes"},
}
_PLACEHOLDER_REF: Final = {"relative_path": "measured/none", "sha256": digest_bytes(b"")}


def _clamped(bbox: list[int], size: tuple[int, int]) -> dict[str, int] | None:
    x, y, w, h = bbox
    x0, y0 = max(0, x), max(0, y)
    x1, y1 = min(size[0], x + w), min(size[1], y + h)
    return None if x1 <= x0 or y1 <= y0 else {"x": x0, "y": y0, "w": x1 - x0, "h": y1 - y0}


def page_shape(page: dict[str, Any]) -> dict[str, Any]:
    """One gold page as record units, line units, and estimated Surya detections."""
    size = (page["width"], page["height"])
    records, lines, surya_lines, blocks = [], [], [], []
    skipped = 0
    for record in page["records"]:
        box = _clamped(record["bbox"], size)
        if box is None or not record["text"].strip():
            skipped += 1
            continue
        records.append(
            {"ordinal": len(records), "box_px": box, "label": None, "text": record["text"]}
        )
        blocks.append(
            {
                "box_px": box,
                "label": "Text",
                "position": len(blocks),
                "confidence_bp": None,
                "ref": _PLACEHOLDER_REF,
            }
        )
        texts = [line for line in record["text"].split("\n") if line.strip()]
        band = max(1, box["h"] // len(texts))
        for index, text in enumerate(texts):
            lines.append({"ordinal": len(lines) + 1, "box_px": None, "label": None, "text": text})
            top = box["y"] + min(index * band, box["h"] - 1)
            height = max(1, min(band, box["y"] + box["h"] - top))
            surya_lines.append(
                {
                    "box_px": {**box, "y": top, "h": height},
                    "confidence_bp": None,
                    "ref": _PLACEHOLDER_REF,
                }
            )
    return {
        "size": size,
        "skipped_records": skipped,
        "witnesses": [
            ("attestator_1", "chandra.v1", records),
            ("attestator_2", "dai.v1", records),
            ("attestator_3", "churro.v1", lines),
        ],
        "surya": {
            "census_ref": _PLACEHOLDER_REF,
            "block_sequence": "surya-order-head",
            "block_sequence_reason": None,
            "lines": surya_lines,
            "blocks": blocks,
        },
    }


def _blank_render(size: tuple[int, int]) -> bytes:
    """A white render of `size`, for drawing the overlay the request would carry."""
    width, height = size
    return encode_grayscale_png_deterministic(
        width, height, [bytearray(b"\xff" * width) for _ in range(height)]
    )


def page_feed_for(sealed: dict[str, Any], shape: dict[str, Any], change: dict) -> dict[str, Any]:
    """The page feed one gold page would be read under, with one feed setting changed."""
    size = shape["size"]
    render = render_size(size, sealed["page_context"]["maximum_edge"])
    switches = {**sealed["feed"], **change}
    render_bytes = _blank_render(render) if switches["page_overlay"] != "off" else None
    chairs = [chair for chair, _adapter, _units in shape["witnesses"]]
    return page_feed.assemble_page_feed(
        page_id="page",
        page_ordinal=1,
        page_size=size,
        feed_switches=switches,
        witness_regime="named",
        roster=chairs,
        witnesses=[
            {
                "chair": chair,
                "witness_label": chair,
                "adapter": adapter,
                "outcome": "read",
                "testimonium_ref": {
                    "relative_path": f"measured/{chair}",
                    "sha256": digest_bytes(chair.encode()),
                },
                "units": units,
                "findings": [],
                "answer_health": {"truncated": False, "repetition": []},
            }
            for chair, adapter, units in shape["witnesses"]
        ],
        surya=shape["surya"],
        page_render={
            "reason": "legible-ink",
            "image_sha256": digest_bytes(render_bytes or b""),
            "transform": {"target_dimensions": {"w": render[0], "h": render[1]}},
        },
        serving_recipe="unproven-real-perlector",
        page_render_bytes=render_bytes,
    )


def page_request(row: ServingProfile, feed: dict[str, Any]) -> dict[str, Any]:
    """The admitted page request, or the refusal it would meet."""
    return page_request_capacity(
        row,
        image_sizes=page_feed.request_image_sizes(feed),
        prompt_text=page_prompt.build_page_prompt("unproven-real-perlector", feed),
        prompt_parts=page_prompt.prompt_parts("unproven-real-perlector", feed),
        template_digest=page_prompt.BUILDER_SHA256,
        answer_measure=feed["answer_measure"],
        generation=page_generation(),
    )


def page_max_tokens() -> int:
    policy, _ = load_decoding_policy(ROOT / "config" / "decoding.toml")
    return perlector_page_max_tokens(policy)


def page_generation() -> dict[str, int]:
    policy, _ = load_decoding_policy(ROOT / "config" / "decoding.toml")
    return perlector_page_generation(policy)


def page_fit_table(pages: list[dict[str, Any]]) -> dict[str, dict[str, int]]:
    """Per feed setting and context: pages admitted, pages refused, and admitted pages whose
    answer reserve was clamped to the page cap."""
    row, sealed = perlector_row(), sealed_protocol()
    shapes = [page_shape(page) for page in pages]
    table = {}
    for name, change in PAGE_SETTINGS.items():
        cells = {
            context: {"fit": 0, "refused_context": 0, "reserve_clamped": 0, "worst_need": 0}
            for context in PAGE_CONTEXTS
        }
        for shape in shapes:
            feed = page_feed_for(sealed, shape, change)
            for context, cell in cells.items():
                try:
                    admitted = page_request(replace(row, max_model_len=context), feed)
                    record = admitted["capacity"]
                    cell["fit"] += 1
                    cell["reserve_clamped"] += admitted["answer_reserve"]["reserve_clamped"]
                except RequestCapacityRefusal as refusal:
                    # A refusal with no capacity record is not a context refusal.
                    if refusal.capacity is None:
                        raise
                    record = refusal.capacity
                    cell["refused_context"] += 1
                cell["worst_need"] = max(cell["worst_need"], record["need"])
        for context, cell in cells.items():
            table[f"page request, {name}, {context}"] = cell
    return table


def skipped_gold_records(pages: list[dict[str, Any]]) -> int:
    """Gold records the page shapes leave out: no text, or no box inside the page."""
    return sum(page_shape(page)["skipped_records"] for page in pages)


def main(argv: list[str]) -> int:
    if not argv:
        print(__doc__)
        return 2
    with open(argv[0], encoding="utf-8") as handle:
        pages = [json.loads(line) for line in handle if line.strip()]
    print(f"{skipped_gold_records(pages)} gold records left out of the page requests")
    for name, cell in page_fit_table(pages).items():
        print(f"{name}: {cell}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
