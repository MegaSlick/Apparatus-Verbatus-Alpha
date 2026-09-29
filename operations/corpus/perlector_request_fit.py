"""Whether each gold act's Perlector request fits the served 27B row, by page-render size.

For every act in a RecordGold-style page manifest this builds the request the live
reader would send -- its padded, clamped crop, its page render, the real prompt
template with three witnesses each reporting the act's gold text, the neighbour
clues of the acts before and after it, and the answer the reader reserves -- and
counts how many fit `config/serving_recipes_real.toml`'s Perlector row under three
settings: the old 1,024-pixel render with no neighbour clues, the old render with
them, and the sealed `[page_context]` rule with them.

Read-only; prints counts only, never a reading, a record id or a page.

    python operations/corpus/perlector_request_fit.py PAGE_MANIFEST.jsonl
"""

from __future__ import annotations

import json
import sys
import tomllib
from pathlib import Path
from typing import Any, Final

ROOT: Final = Path(__file__).resolve().parents[2]
_PERLECTOR_DIR = ROOT / "pipeline" / "4_perlector"
if str(_PERLECTOR_DIR) not in sys.path:
    sys.path.insert(0, str(_PERLECTOR_DIR))

import live_reader  # noqa: E402
import prompts  # noqa: E402
import protocol  # noqa: E402

from common.background import round_half_up_bp  # noqa: E402
from common.chairs.registry import ChairRegistry  # noqa: E402
from common.decoding import load_decoding_policy, perlector_max_tokens  # noqa: E402
from common.request_capacity import perlector_prompt_bound, request_fits  # noqa: E402
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


def _neighbour(texts: list[str] | None, side: str, cap: int) -> dict[str, Any] | None:
    if texts is None:
        return None
    witnesses = []
    for chair, text in zip(WITNESSES, texts, strict=True):
        shown = "whole" if len(text) <= cap else ("tail" if side == "preceding" else "head")
        cut = text if shown == "whole" else (text[-cap:] if side == "preceding" else text[:cap])
        witnesses.append({"witness_label": chair, "reported": cut, "shown": shown})
    return {"act_key": "neighbour", "same_page": True, "witnesses": witnesses, "unavailable": None}


def _rendered(page: tuple[int, int], edge: int) -> tuple[int, int]:
    width, height = page
    scale = min(1, edge / max(width, height))
    return (max(1, round(width * scale)), max(1, round(height * scale)))


def _covered(page: tuple[int, int], crops: list[tuple[int, int, int, int]]) -> bool:
    # One full-page crop is the case that occurs; `dossier.union_area` decides it in
    # the pipeline for any union.
    return any(crop == (0, 0, page[0], page[1]) for crop in crops)


def request_record(
    row: ServingProfile,
    sealed: dict[str, Any],
    *,
    pages: list[tuple[tuple[int, int], list[tuple[int, int, int, int]]]],
    witness_texts: list[str],
    neighbours: tuple[list[str] | None, list[str] | None] | None,
    prior_text: str | None,
    edge: int | None,
) -> dict[str, Any]:
    """The capacity record for one act's request.

    `pages` is each page's size and the act's crops on it as `(x, y, w, h)`.
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
            acts.append({"size": size, "box": (x, y, w, h), "text": record["text"]})
    for index, act in enumerate(acts):
        act["crop"] = padded_crop(act["box"], act["size"], padding)
        act["before"] = acts[index - 1]["text"] if index else None
        act["after"] = acts[index + 1]["text"] if index + 1 < len(acts) else None
    return acts


def padded_crop(box, page, padding) -> tuple[int, int, int, int]:
    """The box padded and clamped as `pipeline/2_designator/geometry.py` does."""
    x, y, w, h = box
    x0 = max(0, x - round_half_up_bp(w, padding["left_bp"]))
    y0 = max(0, y - round_half_up_bp(h, padding["top_bp"]))
    x1 = min(page[0], x + w + round_half_up_bp(w, padding["right_bp"]))
    y1 = min(page[1], y + h + round_half_up_bp(h, padding["bottom_bp"]))
    return (x0, y0, x1 - x0, y1 - y0)


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
                    ([act["before"]] * 3 if act["before"] else None),
                    ([act["after"]] * 3 if act["after"] else None),
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
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
