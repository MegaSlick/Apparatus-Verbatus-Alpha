"""Whether each gold act's Perlector request fits the served 27B row, by page-render size.

For every act in a RecordGold-style page manifest this builds the request the live
reader would send -- its padded, clamped crop, its page render, the real prompt
template with three witnesses each reporting the act's gold text, the neighbour
clues of the acts before and after it, and the answer the reader reserves -- and
counts how many fit `config/serving_recipes_real.toml`'s Perlector row under three
settings: the old 1,024-pixel render with no neighbour clues, the old render with
them, and the sealed `[page_context]` rule with them.

It then counts, per page, whether the whole-page request (`reading_unit =
"page"`) fits at 32,768 and 65,536 tokens of context under the feed settings
that matter most: the default feed, no Surya detections, flat witness units and
the page overlay. Each page is fed as three witnesses reporting its gold text --
two in record units with the gold boxes (as Chandra's blocks and DAI's records
are), one in lines with no box (as Churro's are) -- and one Surya line per gold
text line, boxed inside its record, with one Surya block per record. The Surya
lines are an estimate from the gold text, not detections. A gold record with no
text or no box inside the page is left out, and the count left out is printed.

Read-only; prints counts only, never a reading, a record id or a page.

    python operations/corpus/perlector_request_fit.py PAGE_MANIFEST.jsonl
"""

from __future__ import annotations

import json
import sys
import tomllib
from dataclasses import replace
from pathlib import Path
from typing import Any, Final

ROOT: Final = Path(__file__).resolve().parents[2]
_PERLECTOR_DIR = ROOT / "pipeline" / "4_perlector"
if str(_PERLECTOR_DIR) not in sys.path:
    sys.path.insert(0, str(_PERLECTOR_DIR))

import live_reader  # noqa: E402
import prompts  # noqa: E402
import protocol  # noqa: E402

from common import page_feed, page_prompt  # noqa: E402
from common.background import round_half_up_bp  # noqa: E402
from common.chairs.registry import ChairRegistry  # noqa: E402
from common.contracts.canonical import digest_bytes  # noqa: E402
from common.decoding import (  # noqa: E402
    load_decoding_policy,
    perlector_max_tokens,
    perlector_page_max_tokens,
)
from common.imaging import encode_grayscale_png_deterministic  # noqa: E402
from common.request_capacity import (  # noqa: E402
    RequestCapacityRefusal,
    page_request_capacity,
    perlector_prompt_bound,
    request_fits,
)
from operations.serving.config import ServingProfile, load_serving_recipes  # noqa: E402

WITNESSES: Final = ("attestator_1", "attestator_2", "attestator_3")
OLD_EDGE: Final = 1024


def perlector_row() -> ServingProfile:
    (row,) = [
        row
        for row in load_serving_recipes(ROOT / "config" / "serving_recipes_real.toml").profiles
        if isinstance(row, ServingProfile) and row.chair == "perlector"
    ]
    return row


def sealed_protocol() -> dict[str, Any]:
    return protocol.load(ROOT / "config" / "perlector_protocol.toml")[0]


def reading_max_tokens() -> int:
    policy, _ = load_decoding_policy(ROOT / "config" / "decoding.toml")
    return perlector_max_tokens(policy)[0]


def _witness_rows() -> list[dict[str, Any]]:
    """The three real chairs as a named dossier shows them: name, domain, provenance."""
    registry = ChairRegistry.from_toml(ROOT / "config" / "models-real.toml")
    domains = tomllib.loads(
        (ROOT / "config" / "witness_context-real.toml").read_text(encoding="utf-8")
    )
    rows = []
    for chair in WITNESSES:
        identity = registry.resolve(chair)
        rows.append(
            {
                "witness_label": chair,
                "training_domain": domains[chair]["training_domain"],
                "model_name": identity.source_reference,
                "resolved_provenance": {
                    "adapter_revision": identity.serving_recipe,
                    "chair": chair,
                    "chair_state": "configured",
                    "receipt_ref": {
                        "relative_path": "receipts/sha256/" + "0" * 69,
                        "sha256": "0" * 64,
                    },
                    "resolved_identity": identity.to_record(),
                    "resolved_revision": {"kind": "digest-manifest", "value": "0" * 64},
                },
            }
        )
    return rows


_ROWS: list[dict[str, Any]] | None = None


def _neighbour(clue: tuple[list[str], bool] | None, side: str, cap: int) -> dict[str, Any] | None:
    if clue is None:
        return None
    texts, same_page = clue
    witnesses = []
    for chair, text in zip(WITNESSES, texts, strict=True):
        shown = "whole" if len(text) <= cap else ("tail" if side == "preceding" else "head")
        cut = text if shown == "whole" else (text[-cap:] if side == "preceding" else text[:cap])
        witnesses.append({"witness_label": chair, "reported": cut, "shown": shown})
    return {
        "act_key": "neighbour",
        "same_page": same_page,
        "witnesses": witnesses,
        "unavailable": None,
    }


def _rendered(page: tuple[int, int], edge: int) -> tuple[int, int]:
    width, height = page
    scale = min(1, edge / max(width, height))
    return (max(1, round(width * scale)), max(1, round(height * scale)))


def _covered(page: tuple[int, int], crops: list[tuple[int, int, int, int]]) -> bool:
    # One full-page crop is the case that occurs; `common.page_render.union_area` decides it in
    # the pipeline for any union.
    return any(crop == (0, 0, page[0], page[1]) for crop in crops)


def request_record(
    row: ServingProfile,
    sealed: dict[str, Any],
    *,
    pages: list[tuple[tuple[int, int], list[tuple[int, int, int, int]]]],
    witness_texts: list[str],
    neighbours: tuple[tuple[list[str], bool] | None, tuple[list[str], bool] | None] | None,
    prior_text: str | None,
    edge: int | None,
) -> dict[str, Any]:
    """The capacity record for one act's request.

    `pages` is each page's size and the act's crops on it as `(x, y, w, h)`.
    Each neighbour is its witness texts and whether it shares a page with the act.
    `edge=None` applies the sealed `[page_context]` rule; an integer renders every
    page at that edge. `neighbours=None` renders no neighbour clues.
    """
    global _ROWS
    if _ROWS is None:
        _ROWS = _witness_rows()
    context = sealed["page_context"]
    crops = [(w, h) for _page, boxes in pages for (_x, _y, w, h) in boxes]
    renders = [
        _rendered(
            page,
            edge
            if edge is not None
            else context["covered_page_edge"]
            if len(pages) > 1 or _covered(page, boxes)
            else context["maximum_edge"],
        )
        for page, boxes in pages
    ]
    cap = sealed["neighbours"]["characters_per_row"]
    dossier: dict[str, Any] = {
        "witness_regime": "named",
        "act_key": "a",
        "testimonia": [
            {**row_, "reported": text} for row_, text in zip(_ROWS, witness_texts, strict=True)
        ],
        "prior_draft_view": "fed" if prior_text is not None else "withheld",
    }
    if prior_text is not None:
        dossier["prior_draft"] = {"text": prior_text}
    if neighbours is not None:
        dossier["neighbours"] = {
            "preceding": _neighbour(neighbours[0], "preceding", cap),
            "following": _neighbour(neighbours[1], "following", cap),
        }
    text = prompts.build_prompt("unproven-real-perlector", "perlector", dossier, sealed)
    block = prompts.neighbour_block(dossier, sealed)
    prompt, basis = perlector_prompt_bound(
        text,
        template_digest=prompts.BUILDER_SHA256,
        capped_spans=[
            *([(block, None)] if block else ()),
            *([(prior_text, reading_max_tokens())] if prior_text else ()),
        ],
    )
    answer = live_reader._reserved_answer_budget(
        "perlector", profile=row, region_sizes=crops, page_render_sizes=renders
    )
    return request_fits(row, crops + renders, prompt, answer, prompt_tokens_basis=basis)


def gold_shapes(pages: list[dict], padding: dict[str, int]) -> list[dict[str, Any]]:
    """Each gold act's page, padded clamped crop, text, and its neighbours' texts."""
    acts = []
    for page in pages:
        size = (page["width"], page["height"])
        for record in page["records"]:
            x, y, w, h = record["bbox"]
            if w <= 0 or h <= 0 or not record["text"].strip():
                continue
            acts.append(
                {"page": id(page), "size": size, "box": (x, y, w, h), "text": record["text"]}
            )
    for index, act in enumerate(acts):
        act["crop"] = padded_crop(act["box"], act["size"], padding)
        act["before"] = acts[index - 1] if index else None
        act["after"] = acts[index + 1] if index + 1 < len(acts) else None
    return acts


def padded_crop(box, page, padding) -> tuple[int, int, int, int]:
    """The box padded and clamped as `pipeline/2_designator/geometry.py` does."""
    x, y, w, h = box
    x0 = max(0, x - round_half_up_bp(w, padding["left_bp"]))
    y0 = max(0, y - round_half_up_bp(h, padding["top_bp"]))
    x1 = min(page[0], x + w + round_half_up_bp(w, padding["right_bp"]))
    y1 = min(page[1], y + h + round_half_up_bp(h, padding["bottom_bp"]))
    return (x0, y0, x1 - x0, y1 - y0)


def _clue(act: dict[str, Any], other: dict[str, Any] | None) -> tuple[list[str], bool] | None:
    """A neighbour's texts and whether it sits on the act's page, as the live dossier sets it."""
    if other is None:
        return None
    return [other["text"]] * len(WITNESSES), other["page"] == act["page"]


def fit_table(acts: list[dict[str, Any]]) -> dict[str, dict[str, int]]:
    row, sealed = perlector_row(), sealed_protocol()
    settings = {
        "old 1024, no neighbours": (OLD_EDGE, False),
        "old 1024, neighbours": (OLD_EDGE, True),
        "sealed rule, neighbours": (None, True),
    }
    table = {name: {"fit": 0, "refused": 0, "worst_need": 0} for name in settings}
    newly_refused = 0
    for act in acts:
        fits = {}
        for name, (edge, with_neighbours) in settings.items():
            record = request_record(
                row,
                sealed,
                pages=[(act["size"], [act["crop"]])],
                witness_texts=[act["text"]] * len(WITNESSES),
                neighbours=(
                    _clue(act, act["before"]),
                    _clue(act, act["after"]),
                )
                if with_neighbours
                else None,
                prior_text=None,
                edge=edge,
            )
            fits[name] = record["fits"]
            cell = table[name]
            cell["fit" if record["fits"] else "refused"] += 1
            cell["worst_need"] = max(cell["worst_need"], record["need"])
        newly_refused += fits["old 1024, neighbours"] and not fits["sealed rule, neighbours"]
    table["newly refused by the page render"] = {"count": newly_refused}
    return table


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
    render = _rendered(size, sealed["page_context"]["maximum_edge"])
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
        page_max_tokens=page_max_tokens(),
    )


def page_max_tokens() -> int:
    policy, _ = load_decoding_policy(ROOT / "config" / "decoding.toml")
    return perlector_page_max_tokens(policy)


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
    padding = tomllib.loads((ROOT / "config" / "designator_padding.toml").read_text("utf-8"))[
        "padding"
    ]
    with open(argv[0], encoding="utf-8") as handle:
        pages = [json.loads(line) for line in handle if line.strip()]
    acts = gold_shapes(pages, padding)
    print(f"{len(acts)} acts on {len(pages)} pages, each on one page")
    for name, cell in fit_table(acts).items():
        print(f"{name}: {cell}")
    print(f"{skipped_gold_records(pages)} gold records left out of the page requests")
    for name, cell in page_fit_table(pages).items():
        print(f"{name}: {cell}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
