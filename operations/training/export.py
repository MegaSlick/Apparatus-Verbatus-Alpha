"""Export Perlector training examples: page image + witness feed -> the reference answer.

    python -m operations.training.export --run-tree RUN --gold DIR --gold-glob '*/Prepped/*.txt' \\
        --held-out FILE --out DIR [--seed 0] [--variants-per-page 4] [--mix honest=40,...] \\
        [--blinded-share 0.5] [--pages STEMS]

Every example is one page shown once under one witness story (`operations.bakeoff.mutations`:
honest, planted, removed, structural) with the same answer every time: the reference
(gold or silver) in the Perlector's own JSON grammar (`common.page_answer`). The prompt is
the Perlector's, with every image the request carries: `fed_arm.request_images` gives
the images as stage 4 sends them (the render, then the page overlay when the feed draws
one, `common/page_path.py`), `fed_arm.build_body` renders the request, and the example
carries those images in that order and the body's text block as the prompt, byte for
byte. Every page's source request is checked against the run before any variant is
drawn (prompt text, image digests, the reading's `request_digest`); an honest, named
example's whole request (text and image digests, `page_path.request_digest`) must equal
the run's recorded `request_digest`, or the export stops.

The dataset is neutral (`images` + `messages` JSONL, one object per line, HF-datasets and
mlx-vlm friendly), with the loss weights as a separate field:

    {"id", "page", "images": ["images/<stem>.png", "images/<stem>.overlay-<sha16>.png"],
     "image_sha256s": [...],                     # one per image, in the request's order
     "messages": [{"role": "user", "content": [{"type": "image"}, {"type": "image"},
                                               {"type": "text", "text": PROMPT}]},
                  {"role": "assistant", "content": ANSWER_JSON}],
     "loss_spans": [[start, end, weight], ...],   # character spans over the assistant content
     "scenario", "family", "witness_regime", "planted_sites", "chat_template_kwargs",
     "prompt_sha256", "request_digest", "prompt_matches_run", "request_matches_run",
     "reference_status", "reference_sha256", "reference_record_sha256", "tokens": {...}}

The mutation that made each example names the reference it was planted from
(`reference_sha256`, `reference_record_sha256`); the target is built only from that same
reference (checked per example), and `planted.jsonl` carries both digests to the scorer.

Loss weights per reference word: checked 1.0, agreed 0.7, draft 0.3, unresolved 0 (the
training plan's table); the JSON scaffold 1.0; cites `CITES_WEIGHT` while they are
approximated from geometry (a silver reference with its own cites gets 1.0). Pages on the
frozen held-out list are never exported, nor the other half of a held-out original.

Beside `train.jsonl`: `planted.jsonl` (each example's mutation sidecar, for the scorer),
`images/`, and `manifest.json` (the feed-mix counts, planted sites by class and k, the
voting-must-lose check on the whole set and on the honest feeds alone, and a token-length
summary: the prompt's sealed upper bound from `common.request_capacity`, no tokenizer
being available offline, and the answer's characters, bytes and carried-rate estimate).
"""

from __future__ import annotations

import argparse
import hashlib
import json
import random
import re
import statistics
import sys
from collections import Counter
from pathlib import Path
from typing import Any

from rapidfuzz.distance import Levenshtein

from common import page_path
from operations.bakeoff import fed_arm as F
from operations.bakeoff import mutations as M
from operations.bakeoff import score as S
from operations.bakeoff import witness_run as W
from operations.bakeoff.fed_score import is_checked_status
from operations.bakeoff.gold import load_gold_dir, marked_words, scored_text

SCHEMA = "perlector-training-example.v1"
REFERENCE_SCHEMA = "training-reference.v1"
WEIGHTS = {"checked": 1.0, "agreed": 0.7, "draft": 0.3, "unresolved": 0.0}
CITES_WEIGHT = 0.3  # cites rebuilt from geometry are a draft, not a checked fact
UNIT_OVERLAP = 0.5  # share of a unit's words found in an entry's text to cite it
_WORD = re.compile(r"\S+")

# --- held-out pages --------------------------------------------------------------------

original_of = M.original_of  # `X_1L` and `X_2R` are the two halves of `X`


def read_held_out(path: Path) -> set[str]:
    stems = set()
    for line in path.read_text("utf-8").splitlines():
        line = line.split("#", 1)[0].strip()
        if line:
            stems.add(Path(line).stem)
    return stems


def is_held_out(stem: str, held: set[str]) -> bool:
    return stem in held or any(original_of(stem) == original_of(h) for h in held)


# --- the reference ---------------------------------------------------------------------


def reference_from_json(record: dict[str, Any]) -> M.Reference:
    """A `training-reference.v1` record: entries with text and, per word, status and class."""
    if record.get("schema") != REFERENCE_SCHEMA:
        raise SystemExit(f"reference {record.get('stem')!r} is not {REFERENCE_SCHEMA}")
    ref = M.Reference(record["stem"], record.get("status_label", "silver"))
    for n, e in enumerate(record["entries"]):
        ref.entries.append(
            {
                "kind": e["kind"],
                "label": e.get("label"),
                "text": e["text"],
                "continues_from_previous_page": bool(e.get("continues_from_previous_page")),
                "continues_to_next_page": bool(e.get("continues_to_next_page")),
                "cites": e.get("cites"),
            }
        )
        words = e.get("words")
        if words is None:
            default = "checked" if ref.status_label == "lead-checked" else "draft"
            words = [
                {"text": t, "status": "unresolved" if doubtful else default}
                for t, doubtful in M.reference_words(e["text"])
            ]
        for w in words:
            status = w.get("status", "draft")
            if status not in WEIGHTS:
                raise SystemExit(
                    f"{record['stem']}: word status {status!r} not in {sorted(WEIGHTS)}"
                )
            ref.words.append(M.RefWord(w["text"], status, w.get("cls") or M.classify(w["text"]), n))
    return ref


def load_references(
    tree: F.RunTree, gold: Path | None, gold_glob: str, reference_dir: Path | None, row_kind: str
) -> dict[str, M.Reference]:
    refs: dict[str, M.Reference] = {}
    stems = tree.by_stem()
    if reference_dir:
        for path in sorted(Path(reference_dir).glob("*.json")):
            record = json.loads(path.read_text("utf-8"))
            if record.get("schema") == REFERENCE_SCHEMA and record["stem"] in stems:
                refs[record["stem"]] = reference_from_json(record)
    if gold:
        for stem, page in load_gold_dir(gold, gold_glob).items():
            if stem in stems and stem not in refs:
                ref = M.reference_from_gold(page, row_kind)
                M.statuses_from_agreement(ref, stems[stem].feed)
                refs[stem] = ref
    return refs


# --- the answer in the Perlector's grammar --------------------------------------------


def _keys(text: str) -> list[str]:
    return [k for k in (" ".join(S.tokens(w)) for w in _WORD.findall(text)) if k]


def _unit_boxes(feed: dict[str, Any], ids: set[str]) -> list[list[int]]:
    return [
        u["box_1000"]
        for r in feed["witnesses"]
        for u in r["units"]
        if u["id"] in ids and u.get("box_1000")
    ]


class ExampleRefused(ValueError):
    """An example the exporter will not write (its target would be wrong); the reason is
    counted in the manifest."""


def shown_ids(feed: dict[str, Any]) -> set[str]:
    """Every id the prompt shows: witness units, Surya lines and blocks."""
    surya = feed.get("surya") or {}
    return {u["id"] for r in feed["witnesses"] for u in r["units"]} | {
        d["id"] for d in [*(surya.get("lines") or []), *(surya.get("blocks") or [])]
    }


def _map_cite(cite: str, id_map: dict[str, str | None] | None, shown: set[str]) -> list[str]:
    """A reference cite (written against the source feed) in the shown feed's ids: [] when
    its unit is no longer shown; ExampleRefused when it names no id at all."""

    def one(uid: str) -> str | None:
        if id_map is not None and uid in id_map:
            return id_map[uid]
        if uid in shown:
            return uid
        raise ExampleRefused(f"cite {uid!r} is neither a source unit nor a shown id")

    if "-" in cite and cite not in shown and (id_map is None or cite not in id_map):
        first, _, last = cite.partition("-")
        a, b = one(first), one(last)
        if a is None or b is None or a[:1] != b[:1]:
            raise ExampleRefused(f"range cite {cite!r} does not survive this feed")
        return [a if a == b else f"{a}-{b}"]
    mapped = one(cite)
    return [] if mapped is None else [mapped]


def _cited_ids(cite: str, feed: dict[str, Any]) -> list[str]:
    """The ids a cite covers: itself, or every unit of a range `A2-A5` in shown order."""
    first, dash, last = cite.partition("-")
    if not dash:
        return [cite]
    for row in feed["witnesses"]:
        ids = [u["id"] for u in row["units"]]
        if first in ids and last in ids and ids.index(first) <= ids.index(last):
            return ids[ids.index(first) : ids.index(last) + 1]
    return [cite]  # not a range over one shown witness: reported as not shown


def build_answer(
    ref: M.Reference,
    feed: dict[str, Any],
    set_aside_ids: list[str],
    id_map: dict[str, str | None] | None = None,
) -> dict[str, Any]:
    """The reference as a page answer: entries in order with cites rebuilt from the shown
    feed (witness units by word overlap, Surya lines and blocks by the cited boxes' rows),
    planted units set aside, every other shown id cited by some entry.

    A reference that brings its own cites (all entries or none, else ExampleRefused) has
    them remapped to the shown feed through the mutation's `id_map` (source unit id ->
    shown id, None when no longer shown), every cite is checked against the shown ids
    (ExampleRefused on any mismatch), and every shown id it does not cite is set aside.
    """
    aside = set(set_aside_ids)
    shown = shown_ids(feed)
    given = [e.get("cites") is not None for e in ref.entries]
    own_cites = any(given)
    if own_cites and not all(given):
        raise ExampleRefused("the reference gives cites for some entries only")
    entries = []
    for n, e in enumerate(ref.entries):
        cites: list[str] = []
        for cite in e.get("cites") or []:
            for mapped in _map_cite(cite, id_map, shown):
                if mapped not in cites:
                    cites.append(mapped)
        entries.append(
            {
                "n": n + 1,
                "kind": e["kind"],
                "label": e.get("label"),
                "cites": cites,
                "text": e["text"],
                "continues_from_previous_page": e["continues_from_previous_page"],
                "continues_to_next_page": e["continues_to_next_page"],
            }
        )
    if own_cites:
        cited = {uid for e in entries for c in e["cites"] for uid in _cited_ids(c, feed)}
        unknown = sorted(cited - shown)
        if unknown:
            raise ExampleRefused(f"target cites ids not shown: {unknown}")
        planted = cited & aside
        if planted:
            raise ExampleRefused(f"target cites planted units: {sorted(planted)}")
        aside |= shown - cited
    if not own_cites and entries:
        entry_keys = [set(_keys(scored_text(e["text"]))) for e in entries]
        best_for: dict[str, tuple[float, int]] = {}
        for row in M.present(feed):
            for unit in row["units"]:
                if unit["id"] in aside:
                    continue
                keys = _keys(unit["text"])
                if not keys:
                    aside.add(unit["id"])
                    continue
                scores = [sum(k in ek for k in keys) / len(keys) for ek in entry_keys]
                top = max(range(len(entries)), key=lambda i: (scores[i], -i))
                best_for[unit["id"]] = (scores[top], top)
        for uid, (score, top) in best_for.items():
            if score > 0:
                entries[top]["cites"].append(uid)
            else:
                aside.add(uid)
        # Surya lines and blocks: by the vertical band of each entry's cited unit boxes;
        # with no boxes at all, spread evenly over the entries in order.
        surya = feed.get("surya") or {}
        detections = [*(surya.get("lines") or []), *(surya.get("blocks") or [])]
        bands = []
        for e in entries:
            boxes = _unit_boxes(feed, set(e["cites"]))
            bands.append((min(b[1] for b in boxes), max(b[3] for b in boxes)) if boxes else None)
        for k, det in enumerate(detections):
            box = det.get("box_1000")
            if box and any(bands):
                mid = (box[1] + box[3]) / 2
                inside = [i for i, b in enumerate(bands) if b and b[0] <= mid <= b[1]]
                if inside:
                    pick = inside[0]
                else:
                    pick = min(
                        (i for i, b in enumerate(bands) if b),
                        key=lambda i: min(abs(mid - bands[i][0]), abs(mid - bands[i][1])),
                    )
            else:
                pick = min(k * len(entries) // max(1, len(detections)), len(entries) - 1)
            entries[pick]["cites"].append(det["id"])
    for e in entries:
        if e["label"] is None:
            del e["label"]
    reasons = {uid: "not on the page" for uid in set_aside_ids}
    return {
        "acts": entries,
        "set_aside": [
            {"id": uid, "reason": reasons.get(uid, "not in the reading")} for uid in sorted(aside)
        ],
    }


# --- loss spans ------------------------------------------------------------------------


def _json_offset(text: str, k: int) -> int:
    """Offset inside the JSON-encoded string value of `text` where `text[k:]` starts."""
    return len(json.dumps(text[:k], ensure_ascii=False)) - 1


def mark_weight(ref: M.Reference) -> float:
    """The weight of the doubt-mark syntax (`[[`, `|other`, `]]`, `[[?]]`): where the ink
    is unread or uncertain is the reference's judgement, so it weighs as a checked word on
    a lead-checked reference and as a draft word otherwise."""
    checked = is_checked_status(ref.status_label or "")
    return WEIGHTS["checked"] if checked else WEIGHTS["draft"]


def _text_weights(text: str, ref: M.Reference, entry: int) -> list[float | None]:
    """A weight per character of an entry's text: each scored word by its reference
    status, the doubt-mark syntax by `mark_weight`, whitespace None (scaffold)."""
    words = marked_words(text)
    keys = [" ".join(S.tokens(w.text)) for w in words]
    ref_idx = ref.entry_words(entry)
    ref_keys = [ref.words[i].text for i in ref_idx]
    status = ["draft"] * len(words)
    ops = Levenshtein.editops(ref_keys, keys, processor=None)
    for block in ops.as_opcodes():
        if block.tag == "equal":
            for k in range(block.src_end - block.src_start):
                status[block.dest_start + k] = ref.words[ref_idx[block.src_start + k]].status
    syntax = mark_weight(ref)
    weights: list[float | None] = [None if ch.isspace() else syntax for ch in text]
    for word, st in zip(words, status, strict=True):
        for i in word.chars:
            weights[i] = WEIGHTS[st]
    return weights


def loss_spans(answer_json: str, answer: dict[str, Any], ref: M.Reference, cites_weight: float):
    """[start, end, weight] over the assistant text: words by reference status, doubt
    marks by `mark_weight`, cites by `cites_weight`, everything else (the scaffold) 1.0.
    Spans tile the whole string."""
    special: list[tuple[int, int, float]] = []
    cursor = 0
    for n, e in enumerate(answer["acts"]):
        cites_json = '"cites": ' + json.dumps(e["cites"], ensure_ascii=False)
        at = answer_json.index(cites_json, cursor)
        special.append((at + len('"cites": '), at + len(cites_json), cites_weight))
        cursor = at + len(cites_json)
        text_json = '"text": ' + json.dumps(e["text"], ensure_ascii=False)
        at = answer_json.index(text_json, cursor)
        value = at + len('"text": ')
        cursor = at + len(text_json)
        weights = _text_weights(e["text"], ref, n)
        k = 0
        while k < len(weights):
            if weights[k] is None:
                k += 1
                continue
            start = k
            while k < len(weights) and weights[k] == weights[start]:
                k += 1
            special.append(
                (
                    value + _json_offset(e["text"], start),
                    value + _json_offset(e["text"], k),
                    weights[start],
                )
            )
    special.sort()
    spans: list[list[float]] = []
    pos = 0
    for start, end, weight in special:
        if start > pos:
            spans.append([pos, start, 1.0])
        spans.append([start, end, weight])
        pos = end
    if pos < len(answer_json):
        spans.append([pos, len(answer_json), 1.0])
    return spans


# --- token lengths ---------------------------------------------------------------------


def token_estimate(feed: dict[str, Any], prompt: str, answer_json: str, page: F.Page) -> dict:
    from common import page_prompt
    from common import request_capacity as RC

    parts = page_prompt.prompt_parts(feed["prompt"]["serving_recipe"], feed)
    try:
        bound, basis = RC.perlector_page_prompt_bound(
            prompt, template_digest=page_prompt.BUILDER_SHA256, parts=parts
        )
    except Exception as failure:  # noqa: BLE001 -- an estimate, never a gate
        bound, basis = None, f"unavailable: {type(failure).__name__}"
    image_tokens = None
    if page.reading:
        cap = (page.reading.get("capacity") or {}).get("capacity") or {}
        image_tokens = cap.get("image_prompt_tokens")
    rate = RC.PERLECTOR_BOUND_TOKENS_PER_10K_CHARACTERS / 10_000
    return {
        "prompt_characters": len(prompt),
        "prompt_tokens_bound": bound,
        "prompt_tokens_basis": basis,
        "image_tokens": image_tokens,
        "answer_characters": len(answer_json),
        "answer_bytes": len(answer_json.encode("utf-8")),
        "answer_tokens_estimate": round(len(answer_json) * rate),
    }


# --- the request ---------------------------------------------------------------------


def source_request_problems(tree: F.RunTree, page: F.Page) -> list[str]:
    """Where the page's own request, rebuilt from its sealed feed, differs from what the
    run sent: the prompt text, the images (render and overlay) against the feed's and the
    call record's digests, and the reading's `request_digest`. Empty when it rebuilds."""
    problems = []
    if not F.prompt_check(page.feed)["text_identical"]:
        problems.append("prompt text")
    check = F.request_check(tree, page)
    problems += [k for k in ("images", "call_images", "reading_digest") if check[k] is False]
    return problems


def example_request(
    tree: F.RunTree, page: F.Page, feed: dict[str, Any]
) -> tuple[list[bytes], dict[str, Any], str]:
    """(the images, the feed as shown, the prompt) as the pipeline and the fed arm send
    this feed: the render, then the overlay when the feed draws one. A feed equal to the
    run's sends the run's own overlay (redrawn and checked against its recorded digest);
    a changed feed has its overlay drawn afresh from the rows it shows
    (`fed_arm.request_images`)."""
    source = page.feed if feed == page.feed else feed
    images, shown = F.request_images(tree, page, source, F.Variant())
    _, prompt = F.build_body(
        shown, images, model_name="training", sampling=F.GREEDY, seed=0,
        max_tokens=F.MAX_TOKENS, stream=False,
    )  # fmt: skip
    return images, shown, prompt


def _image_paths(
    out: Path, stem: str, render_rel: str | None, images: list[bytes], digests: list[str]
) -> list[str]:
    """Each image's path in the request's order: the render's (`render_rel`, written once
    per page), then each overlay, written once under its digest."""
    paths = []
    for n, (image, digest) in enumerate(zip(images, digests, strict=True)):
        if n == 0 and render_rel is not None:
            paths.append(render_rel)
            continue
        rel = f"images/{stem}.overlay-{digest[:16]}.png"
        if not (out / rel).is_file():
            (out / rel).write_bytes(image)
        paths.append(rel)
    return paths


# --- the export ------------------------------------------------------------------------


def parse_mix(text: str | None) -> dict[str, int]:
    if not text:
        return dict(M.DEFAULT_MIX)
    mix = {}
    for item in text.split(","):
        name, _, value = item.strip().partition("=")
        if name not in M.SCENARIOS:
            raise SystemExit(f"--mix names an unknown scenario {name!r}; one of {M.SCENARIOS}")
        mix[name] = int(value)
    return mix


def export(
    tree: F.RunTree,
    refs: dict[str, M.Reference],
    held: set[str],
    out: Path,
    *,
    seed: int = 0,
    variants_per_page: int = 4,
    mix: dict[str, int] | None = None,
    blinded_share: float = 0.5,
    pages: list[F.Page] | None = None,
    copy_images: bool = True,
    allow_prompt_mismatch: bool = False,
) -> dict[str, Any]:
    """Write the dataset. Before any variant is drawn, every page's source feed must
    rebuild the run's own request (`source_request_problems`: prompt text, images,
    request digest): a page that differs stops the export (SystemExit naming the pages)
    unless `allow_prompt_mismatch`, which leaves those pages out and lists them in the
    manifest. Each example carries every image of its request; an honest, named
    example whose request digest differs from the run's recorded one stops the export.
    An example whose target cannot be made right (`ExampleRefused`) is not written and
    is counted by reason."""
    mix = mix or dict(M.DEFAULT_MIX)
    out.mkdir(parents=True, exist_ok=True)
    (out / "images").mkdir(exist_ok=True)
    chosen = pages or [tree.pages[o] for o in sorted(tree.pages)]
    kept = [p for p in chosen if p.stem in refs and not is_held_out(p.stem, held)]
    excluded = sorted(p.stem for p in chosen if p.stem in refs and is_held_out(p.stem, held))
    # The source request is checked once per page, before any variant: every variant's
    # prompt and images come from the same builder and renders, so a page that does not
    # rebuild its run request cannot be trusted in any form. (The builder's code digest
    # is not compared: an alias added to the module changes it, not the bytes sent.)
    source_problems = {p.stem: source_request_problems(tree, p) for p in kept}
    mismatched = sorted(stem for stem, problems in source_problems.items() if problems)
    if mismatched and not allow_prompt_mismatch:
        detail = "; ".join(f"{s} ({', '.join(source_problems[s])})" for s in mismatched)
        raise SystemExit(
            f"{len(mismatched)} page(s) do not rebuild the run's recorded request: {detail}; "
            "check the builder and the run's configuration, or pass --allow-prompt-mismatch "
            "to leave them out"
        )
    kept = [p for p in kept if p.stem not in mismatched]
    refused: list[dict[str, str]] = []
    # Invented acts and injections borrow other pages' text: only from pages that may be
    # trained on, never a held-out page or its sibling half.
    donor_refs = {stem: r for stem, r in refs.items() if not is_held_out(stem, held)}
    counts: Counter = Counter()
    turns: Counter = Counter()
    planted_by: Counter = Counter()
    tokens: list[dict] = []
    vote_items, honest_items = [], []
    matched = Counter()
    image_counts: Counter = Counter()
    with (
        (out / "train.jsonl").open("w", encoding="utf-8") as data,
        (out / "planted.jsonl").open("w", encoding="utf-8") as side,
    ):
        for page in kept:
            ref = refs[page.stem]
            rng = random.Random(f"{M.page_sha(page.feed)}|mix|{seed}")
            render = page.feed.get("page_render")
            render_rel = None
            if render:
                if copy_images:
                    render_rel = f"images/{page.stem}.png"
                    (out / render_rel).write_bytes(tree.render(page))
                else:
                    render_rel = str((tree.root / render["image_path"]).resolve())

            for k, scenario in enumerate(
                M.draw_scenarios(mix, variants_per_page, rng, ref.blank), start=1
            ):
                turn = turns[scenario]
                turns[scenario] += 1
                mutation = M.mutate(
                    page.feed, ref, scenario, seed=seed, turn=turn, stem=page.stem,
                    donors=M.donor_acts(donor_refs, page.stem),
                )  # fmt: skip
                blinded = rng.random() < blinded_share and mutation.feed["witnesses"]
                if blinded:
                    M.blind_labels(mutation, seed)
                example_id = f"{page.stem}#{k}"
                # The target is built from the very reference the mutation was planted on.
                bound = M.reference_mismatch(
                    mutation.record(), M.reference_identities({page.stem: ref})[page.stem]
                )
                if bound:
                    raise SystemExit(
                        f"{example_id}: the reference changed between planting and the "
                        f"target ({', '.join(bound)} differ)"
                    )
                images, feed, prompt = example_request(tree, page, mutation.feed)
                digests = [hashlib.sha256(image).hexdigest() for image in images]
                if digests != page_path.request_image_sha256s(feed):
                    raise SystemExit(
                        f"{example_id}: the images do not match the shown feed's digests"
                    )
                request_sha = page_path.request_digest(prompt, digests)
                matches_run = request_matches = None
                if scenario == "honest" and not blinded:
                    matches_run = F.prompt_check(feed)["text_identical"]
                    recorded = (page.reading or {}).get("request_digest")
                    matched["checked"] += 1
                    matched["identical"] += bool(matches_run)
                    if recorded:
                        request_matches = request_sha == recorded
                        matched["requests_checked"] += 1
                        matched["requests_identical"] += request_matches
                    else:
                        matched["requests_unrecorded"] += 1
                    if not matches_run or request_matches is False:
                        raise SystemExit(
                            f"{example_id}: an honest, named example's request differs from "
                            f"the run's (prompt text {'same' if matches_run else 'differs'}, "
                            f"request digest {request_sha[:12]} vs recorded "
                            f"{(recorded or '-')[:12]}); the exporter would train on a "
                            "request the pipeline never sends"
                        )
                try:
                    answer = build_answer(ref, feed, mutation.set_aside_ids, id_map=mutation.id_map)
                except ExampleRefused as refusal:
                    refused.append({"id": example_id, "scenario": scenario, "reason": str(refusal)})
                    continue
                answer_json = json.dumps(answer, ensure_ascii=False)
                cites_weight = (
                    1.0 if any(e.get("cites") is not None for e in ref.entries) else CITES_WEIGHT
                )
                spans = loss_spans(answer_json, answer, ref, cites_weight)
                est = token_estimate(feed, prompt, answer_json, page)
                tokens.append(est)
                content = [*({"type": "image"} for _ in images), {"type": "text", "text": prompt}]
                image_counts[len(images)] += 1
                example = {
                    "schema": SCHEMA,
                    "id": example_id,
                    "page": page.stem,
                    "images": _image_paths(out, page.stem, render_rel, images, digests),
                    "image_sha256s": digests,
                    "messages": [
                        {"role": "user", "content": content},
                        {"role": "assistant", "content": answer_json},
                    ],
                    "loss_spans": spans,
                    "scenario": scenario,
                    "family": mutation.family,
                    "witness_regime": feed["witness_regime"],
                    "planted_sites": len(mutation.planted),
                    "set_aside_ids": mutation.set_aside_ids,
                    "chat_template_kwargs": {"enable_thinking": False},
                    "prompt_sha256": hashlib.sha256(prompt.encode("utf-8")).hexdigest(),
                    "request_digest": request_sha,
                    "prompt_matches_run": matches_run,
                    "request_matches_run": request_matches,
                    "source_prompt_matches_run": True,
                    "reference_status": ref.status_label,
                    "reference_sha256": mutation.reference_sha256,
                    "reference_record_sha256": mutation.reference_record_sha256,
                    "cites_weight": cites_weight,
                    "tokens": est,
                }
                data.write(json.dumps(example, ensure_ascii=False) + "\n")
                side.write(
                    json.dumps({"id": example_id, **mutation.sidecar()}, ensure_ascii=False) + "\n"
                )
                counts[scenario] += 1
                counts[f"family:{mutation.family}"] += 1
                counts["blinded"] += bool(blinded)
                for site in mutation.planted:
                    planted_by[f"class:{site['cls']}"] += 1
                    planted_by[f"k:{site.get('k') or len(site['witnesses'])}"] += 1
                    planted_by["sites"] += 1
                vote_items.append((ref, feed))
                if scenario == "honest":
                    honest_items.append((ref, feed))

    def summary(key):
        values = [t[key] for t in tokens if t.get(key) is not None]
        if not values:
            return None
        return {
            "median": statistics.median(values),
            "max": max(values),
            "mean": round(statistics.fmean(values)),
        }

    manifest = {
        "schema": SCHEMA,
        "seed": seed,
        "variants_per_page": variants_per_page,
        "mix": mix,
        "blinded_share": blinded_share,
        "pages": len(kept),
        "held_out_excluded": excluded,
        "examples": sum(counts[s] for s in M.SCENARIOS),
        "by_scenario": {s: counts[s] for s in M.SCENARIOS if counts[s]},
        "by_family": {f: counts[f"family:{f}"] for f in M.FAMILIES},
        "blinded_examples": counts["blinded"],
        "planted": dict(planted_by),
        "source_prompts_checked": len(kept) + len(mismatched),
        "prompt_mismatch_excluded": mismatched,
        "refused": refused,
        "honest_prompts_checked": matched["checked"],
        "honest_prompts_identical_to_run": matched["identical"],
        "honest_requests_checked": matched["requests_checked"],
        "honest_requests_identical_to_run": matched["requests_identical"],
        "honest_requests_unrecorded": matched["requests_unrecorded"],
        "examples_by_image_count": {str(n): image_counts[n] for n in sorted(image_counts)},
        "vote_check": {"all": M.vote_check(vote_items), "honest": M.vote_check(honest_items)},
        "tokens": {
            key: summary(key)
            for key in (
                "prompt_characters",
                "prompt_tokens_bound",
                "image_tokens",
                "answer_characters",
                "answer_tokens_estimate",
            )
        },
        "reference_statuses": Counter(r.status_label for r in refs.values()),
        "written": W.now(),
    }
    W.write_json(out / "manifest.json", manifest)
    return manifest


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--run-tree", type=Path, required=True)
    parser.add_argument(
        "--gold", type=Path, help="bake-off gold/fool's gold pages (stand-in reference)"
    )
    parser.add_argument("--gold-glob", default="**/*.txt")
    parser.add_argument("--reference", type=Path, help=f"folder of {REFERENCE_SCHEMA} records")
    parser.add_argument(
        "--held-out", type=Path, required=True, help="frozen test stems, never exported"
    )
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--seed", type=int, default=0)
    parser.add_argument("--variants-per-page", type=int, default=4)
    parser.add_argument("--mix", help="scenario=share,... (default: the training plan's mix)")
    parser.add_argument("--blinded-share", type=float, default=0.5)
    parser.add_argument(
        "--row-kind", default="other", help="entry kind for index rows (decision 4)"
    )
    parser.add_argument("--pages", help="comma-separated stems or ordinals")
    parser.add_argument("--no-copy-images", action="store_true", help="reference the run's renders")
    parser.add_argument(
        "--allow-prompt-mismatch",
        action="store_true",
        help="leave out (and list) pages whose prompt no longer rebuilds the run's, "
        "instead of stopping",
    )
    args = parser.parse_args(argv)
    if not args.gold and not args.reference:
        parser.error("give --gold and/or --reference")
    tree = F.load_run_tree(args.run_tree, sealed=F.is_real_run(args.run_tree))
    refs = load_references(tree, args.gold, args.gold_glob, args.reference, args.row_kind)
    held = read_held_out(args.held_out)
    manifest = export(
        tree, refs, held, args.out, seed=args.seed, variants_per_page=args.variants_per_page,
        mix=parse_mix(args.mix), blinded_share=args.blinded_share,
        pages=F._pick(tree, args.pages, None) if args.pages else None,
        copy_images=not args.no_copy_images, allow_prompt_mismatch=args.allow_prompt_mismatch,
    )  # fmt: skip
    print(
        json.dumps(
            {k: v for k, v in manifest.items() if k != "held_out_excluded"},
            ensure_ascii=False,
            indent=1,
            default=str,
        )
    )
    print(f"held-out pages excluded: {len(manifest['held_out_excluded'])}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
