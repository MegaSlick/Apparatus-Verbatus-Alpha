"""The fed-witness reader arm (`qwen-fed`): the Perlector's own page request, sent to any model.

    python -m operations.bakeoff.fed_arm prompts --run-tree RUN
    python -m operations.bakeoff.fed_arm run --run-tree RUN --out CACHE --label NAME \\
        --model-name SERVED (--server-url URL | --weights DIR) [variant options]

A sealed run tree already holds everything a Perlector page reading was shown: the
`page-feed` record of every page (each witness's units as the reader saw them, Surya's
lines and blocks, the switches) and the page render it sent (`4_perlector/blobs/`).
`prompts` rebuilds every page's prompt with `common.page_prompt.build_page_prompt` and
checks it against the digests the run recorded (`prompt.rendered_sha256`,
`instruction_sha256`, `builder_sha256`), and rebuilds the whole request body with
`operations.serving.http.request_body` and checks it against the request digest of the
run's own call record, so what this arm sends is provably what the pipeline sent.

`run` sends that request -- the render first, then the rebuilt text, one user turn,
thinking off, `max_tokens` 12,288, streamed under the Perlector's sealed repetition-loop
guard (`common.repetition_loop`) -- to whatever model a vLLM server serves under
`--model-name`: the base, a LoRA adapter's name or a merged checkpoint. Sampling is
greedy by default (temperature 0, so two arms differ by their inputs, not the dice), or
`--sampling sealed`: the Perlector's sealed row, which with the run's served name and an
unchanged feed reproduces the run's request byte for byte (the noise-floor repeat).

Variants, each applied to the feed before the prompt is rendered, so the prompt and
instruction stay what the pipeline would build for that feed:

- `--drop-witness LABEL`: the witness row is removed (as if the chair were off the
  roster); letters are then reassigned in sorted label order, as `page_feed` assigns them.
- `--add-witness LABEL=CACHE`: a bake-off witness cache (`<CACHE>/<page stem>.json`, the
  `witness_run`/native record) becomes a witness row named LABEL. dots.mocr's layout
  cells become units with their boxes; any other arm is one unit of its plain text.
  Use a chair-like label (`attestator_4`): under the named regime the label is shown.
- `--letter-map LABEL=X,...`: give witnesses other letters (unit ids follow).
- `--witness-order LABEL,LABEL,...`: show the rows in this order (letters unchanged
  unless `--letter-map` also moves them). Together they are the swap test.
- `--image clear|none|blur|blank|swap`: the render as sent; none drops the image and
  lets the builder say so ("page image: not shown."); blur (`--blur-radius`), blank (a
  white page of the same size) and swap (the next page's render) keep the prompt as is.
- `--mutations DIR`: a folder of `witness-mutation.v1` records (`<page stem>.json`,
  written by `operations.bakeoff.mutations`): each page's feed is the record's
  counterfactual feed (planted errors, a dropped act, a shuffled roster...) and the
  record's sidecar of planted sites travels with the cached answer, so `fed_score` can
  count copied planted errors. The other feed variants apply on top of it.

Each page is cached at `<out>/<label>/<page stem>.json` (schema `bakeoff-fed-page.v1`):
the variant, the feed as shown, the prompt evidence and whether it matches the run's,
the request digest, the reply's content, finish reason, usage, any loop stop, the
grammar's parse (`common.page_answer`) and the reading's text. A page cached without an
error is skipped next time; a label already holding another variant or model is refused.
The streamed reply's raw bytes sit beside it as `<page stem>.sse.gz`.

Scoring: `operations/bakeoff/fed_score.py`. An experiment tool outside the pipeline's
custody: it publishes nothing into the run tree and reads it only.
"""

from __future__ import annotations

import argparse
import base64
import copy
import gzip
import hashlib
import io
import json
import math
import sys
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from operations.bakeoff import arms as A
from operations.bakeoff import witness_run as W

SCHEMA = "bakeoff-fed-page.v1"
ARM = A.Arm("qwen-fed", "perlector", None, "page", "generic-80gb-plus")
RECIPE = "unproven-real-perlector"  # the recorded feeds' serving recipe
MAX_TOKENS = 12_288  # config/perlector_protocol.toml page_max_tokens, as the run sent it
GREEDY = {"temperature": 0.0, "top_p": 1.0, "top_k": 0, "min_p": 0.0}
IMAGE_MODES = ("clear", "none", "blur", "blank", "swap")
# The bake-off arm each chair of the 2026-10 roster is (config/models-real.toml).
CHAIR_ARMS = {"attestator_1": "chandra", "attestator_2": "dai", "attestator_3": "churro"}


# --- the run tree -------------------------------------------------------------------


def _payload(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text("utf-8"))["payload"]


@dataclass
class Page:
    ordinal: int
    stem: str
    feed: dict[str, Any]
    reading: dict[str, Any] | None = None  # the run's first page-reading, if any
    call: dict[str, Any] | None = None  # its retained call record, if any


@dataclass
class RunTree:
    root: Path
    pages: dict[int, Page] = field(default_factory=dict)

    def blob(self, relative: str) -> bytes:
        return (self.root / relative).read_bytes()

    def by_stem(self) -> dict[str, Page]:
        return {p.stem: p for p in self.pages.values()}


def load_run_tree(root: Path) -> RunTree:
    """Every page's feed, stem, first reading and call record from a sealed run tree."""
    root = Path(root)
    stems = {}
    for path in sorted((root / "1_exemplar/artifacts/page").glob("*.json")):
        page = _payload(path)
        stems[page["ordinal"]] = Path(page["declared_path"]).stem
    feeds: dict[str, dict[str, Any]] = {}
    for path in sorted((root / "4_perlector/artifacts/page-feed").glob("*.json")):
        feeds[f"4_perlector/artifacts/page-feed/{path.name}"] = _payload(path)
    if not feeds:
        raise SystemExit(f"no page-feed records under {root}/4_perlector/artifacts/page-feed")
    tree = RunTree(root)
    for feed in feeds.values():
        ordinal = feed["page_ordinal"]
        if ordinal in tree.pages:
            raise SystemExit(f"two page feeds for page {ordinal} in {root}")
        tree.pages[ordinal] = Page(ordinal, stems.get(ordinal, f"page-{ordinal:04d}"), feed)
    for path in sorted((root / "4_perlector/artifacts/page-reading").glob("*.json")):
        reading = _payload(path)
        page = tree.pages.get(reading["page_ordinal"])
        if page is None or reading["attempt_ordinal"] != 1:
            continue
        if feeds.get(reading["feed_ref"]["relative_path"]) is not page.feed:
            continue
        page.reading = reading
        call = reading.get("engine_call")
        if call:
            page.call = json.loads(tree.blob(call["call_record_ref"]["relative_path"]))
    return tree


# --- variants -----------------------------------------------------------------------


@dataclass(frozen=True)
class Variant:
    drop: tuple[str, ...] = ()
    add: tuple[tuple[str, str], ...] = ()  # (label, cache dir)
    letter_map: tuple[tuple[str, str], ...] = ()
    order: tuple[str, ...] = ()
    image: str = "clear"
    blur_radius: float = 8.0
    mutations: str | None = None  # folder of witness-mutation.v1 records, one per page

    @property
    def feed_changed(self) -> bool:
        return (
            bool(self.drop or self.add or self.letter_map or self.order or self.mutations)
            or self.image == "none"
        )

    @property
    def unchanged(self) -> bool:
        return not self.feed_changed and self.image == "clear"

    def record(self) -> dict[str, Any]:
        return {
            "drop": list(self.drop),
            "add": {label: str(path) for label, path in self.add},
            "letter_map": dict(self.letter_map),
            "order": list(self.order),
            "image": self.image,
            "blur_radius": self.blur_radius if self.image == "blur" else None,
            "mutations": self.mutations,
        }


def _reletter(row: dict[str, Any], letter: str) -> dict[str, Any]:
    old = row["letter"]
    row = {**row, "letter": letter}
    row["units"] = [{**u, "id": letter + u["id"][len(old) :]} for u in row["units"]]
    return row


def _health(record: dict[str, Any]) -> dict[str, Any]:
    repeating = [{"kind": "post-hoc-repetition"}] if record.get("loop") else []
    return {"truncated": record.get("finish_reason") == "length", "repetition": repeating}


def witness_row_from_cache(
    label: str, record: dict[str, Any] | None, page_size: dict[str, int]
) -> dict[str, Any]:
    """A bake-off witness record as a page-feed witness row (letter set later).

    dots.mocr's layout cells (`units[0].cells`, boxes in its loaded image's pixels) become
    one unit each, boxed on the page; any other record is one unit of its plain text
    (`score.normalise_output`), with no box. No record or an error is `failed`.
    """
    from common import page_feed
    from operations.bakeoff import score as S

    row: dict[str, Any] = {
        "letter": "?",
        "witness_label": label,
        "chair": label,
        "unit_kind": "layout-block",
        "outcome": "failed",
        "testimonium_ref": None,
        "findings": [],
        "answer_health": {"truncated": None, "repetition": []},
        "units": [],
        "source": None if record is None else {"model": record.get("model")},
    }
    if record is None or record.get("error"):
        return row
    units: list[dict[str, Any]] = []
    first = (record.get("units") or [{}])[0]
    cells, loaded = first.get("cells"), (first.get("request") or {}).get("loaded_size")
    size = (page_size["w"], page_size["h"])
    if cells and loaded:
        from operations.bakeoff.native.dots_mocr import cells_lines

        for cell in cells:
            if cell.get("category") == "Picture":
                continue
            text = cells_lines([cell])
            if not text.strip():
                continue
            box_px = None
            bbox = cell.get("bbox") or []
            if len(bbox) == 4:
                sx, sy = size[0] / loaded[0], size[1] / loaded[1]
                x0, y0 = max(0, int(bbox[0] * sx)), max(0, int(bbox[1] * sy))
                x1 = min(size[0], math.ceil(bbox[2] * sx))
                y1 = min(size[1], math.ceil(bbox[3] * sy))
                if x1 > x0 and y1 > y0:
                    box_px = {"x": x0, "y": y0, "w": x1 - x0, "h": y1 - y0}
            units.append(
                {
                    "id": f"?{len(units) + 1}",
                    "ordinal": len(units),
                    "box_px": box_px,
                    "box_1000": None if box_px is None else page_feed.box_1000(box_px, size),
                    "label": cell.get("category"),
                    "text": text,
                }
            )
    else:
        text = S.normalise_output(record.get("arm", ""), record.get("text"))
        if text.strip():
            units.append(
                {
                    "id": "?1",
                    "ordinal": 0,
                    "box_px": None,
                    "box_1000": None,
                    "label": None,
                    "text": text,
                }
            )
    row.update(outcome="read", answer_health=_health(record), units=units)
    return row


def load_mutation(variant: Variant, stem: str) -> dict[str, Any] | None:
    """The page's witness-mutation record under `variant.mutations`, or None."""
    if not variant.mutations:
        return None
    path = Path(variant.mutations) / f"{stem}.json"
    if not path.is_file():
        raise SystemExit(f"--mutations {variant.mutations}: no record for page {stem!r}")
    record = json.loads(path.read_text("utf-8"))
    if record.get("schema") != "witness-mutation.v1":
        raise SystemExit(f"{path} is not a witness-mutation.v1 record")
    return record


def apply_variant(
    feed: dict[str, Any],
    variant: Variant,
    added: dict[str, Any],
    mutation: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """The feed as this variant shows it. `added` maps each added label to its record;
    `mutation` (a witness-mutation.v1 record) replaces the feed before the other variants."""
    from common.page_feed import WITNESS_LETTERS

    if not variant.feed_changed:
        return feed
    feed = copy.deepcopy(mutation["feed"] if mutation else feed)
    rows = feed["witnesses"]
    labels = [r["witness_label"] for r in rows]
    unknown = [label for label in variant.drop if label not in labels]
    if unknown:
        raise SystemExit(f"--drop-witness {unknown}: this feed's witnesses are {labels}")
    rows = [r for r in rows if r["witness_label"] not in variant.drop]
    for label, _ in variant.add:
        if label in labels:
            raise SystemExit(f"--add-witness {label}: the feed already has a witness {label}")
        rows.append(witness_row_from_cache(label, added.get(label), feed["page_size"]))
    if variant.drop or variant.add:
        # page_feed's rule: letters in sorted witness_label order.
        rows = sorted(rows, key=lambda r: r["witness_label"])
        if len(rows) > len(WITNESS_LETTERS):
            raise SystemExit(f"{len(rows)} witnesses, more than the {len(WITNESS_LETTERS)} letters")
        rows = [_reletter(r, letter) for letter, r in zip(WITNESS_LETTERS, rows, strict=False)]
    if variant.letter_map:
        mapping = dict(variant.letter_map)
        missing = sorted(set(mapping) - {r["witness_label"] for r in rows})
        if missing:
            raise SystemExit(f"--letter-map names witnesses not shown: {missing}")
        new = {r["witness_label"]: mapping.get(r["witness_label"], r["letter"]) for r in rows}
        if len(set(new.values())) != len(new) or not set(new.values()) <= set(WITNESS_LETTERS):
            raise SystemExit(
                f"--letter-map gives letters {new}: they must be distinct witness letters"
            )
        rows = [_reletter(r, new[r["witness_label"]]) for r in rows]
    if variant.order:
        shown = [r["witness_label"] for r in rows]
        if sorted(variant.order) != sorted(shown):
            raise SystemExit(f"--witness-order {list(variant.order)} must list exactly {shown}")
        position = {label: i for i, label in enumerate(variant.order)}
        rows = sorted(rows, key=lambda r: position[r["witness_label"]])
    feed["witnesses"] = rows
    if variant.image == "none":
        feed["page_render"] = None
        feed["switches"] = {**feed["switches"], "page_image": "off"}
    return feed


def page_image(tree: RunTree, page: Page, variant: Variant) -> bytes | None:
    """The render this variant sends, or None."""
    if variant.image == "none":
        return None
    if variant.image == "swap":
        ordinals = sorted(o for o, p in tree.pages.items() if p.feed["page_render"])
        other = ordinals[(ordinals.index(page.ordinal) + 1) % len(ordinals)]
        return tree.blob(tree.pages[other].feed["page_render"]["image_path"])
    render = page.feed["page_render"]
    if render is None:
        return None
    png = tree.blob(render["image_path"])
    if variant.image == "clear":
        return png
    from PIL import Image, ImageFilter

    with Image.open(io.BytesIO(png)) as image:
        image.load()
        if variant.image == "blur":
            changed = image.filter(ImageFilter.GaussianBlur(variant.blur_radius))
        else:  # blank
            changed = Image.new(image.mode, image.size, "white")
        out = io.BytesIO()
        changed.save(out, format="PNG")
    return out.getvalue()


# --- the request --------------------------------------------------------------------


def sealed_sampling() -> dict[str, int | float]:
    from common.decoding import chair_decoding, load_decoding_policy

    policy, _ = load_decoding_policy(A.ROOT / "config" / "decoding.toml")
    return chair_decoding(policy, "perlector")


def loop_guard() -> dict[str, int]:
    from common.decoding import load_decoding_policy, perlector_loop_guard

    policy, _ = load_decoding_policy(A.ROOT / "config" / "decoding.toml")
    return perlector_loop_guard(policy)


def sampling_for(name: str) -> dict[str, int | float]:
    sealed = sealed_sampling()
    if name == "sealed":
        return sealed
    if name == "greedy":
        return {**sealed, **GREEDY}
    raise SystemExit(f"unknown sampling {name!r}: greedy or sealed")


def prompt_check(feed: dict[str, Any]) -> dict[str, Any]:
    """The rebuilt prompt's evidence beside the run's, field by field."""
    from common import page_prompt

    recipe = feed["prompt"]["serving_recipe"]
    rebuilt = page_prompt.page_prompt_evidence(recipe, feed)
    sealed = feed["prompt"]
    return {
        "rebuilt": rebuilt,
        "matches": {k: rebuilt[k] == sealed.get(k) for k in rebuilt},
        "identical": rebuilt == sealed,
    }


def build_body(
    feed: dict[str, Any],
    image: bytes | None,
    *,
    model_name: str,
    sampling: dict[str, int | float],
    seed: int,
    max_tokens: int,
    stream: bool,
) -> tuple[bytes, str]:
    """(the request body as the pipeline renders it, the prompt text)."""
    from common import page_prompt
    from operations.serving.chat_request import image_content_blocks
    from operations.serving.http import request_body

    text = page_prompt.build_page_prompt(feed["prompt"]["serving_recipe"], feed)
    content = [
        *image_content_blocks([image] if image is not None else []),
        {"type": "text", "text": text},
    ]
    payload = {
        "max_tokens": max_tokens,
        "chat_template_kwargs": {"enable_thinking": False},
        "messages": [{"role": "user", "content": content}],
    }
    body = request_body(
        payload,
        model_id=model_name,
        seed=seed,
        deterministic=False,
        sampling=sampling,
        stream=stream,
    )
    return body, text


def verify_prompts(tree: RunTree) -> dict[str, Any]:
    """Rebuild every page's prompt and request; compare with what the run recorded."""
    sealed = sealed_sampling()
    rows = []
    for ordinal in sorted(tree.pages):
        page = tree.pages[ordinal]
        check = prompt_check(page.feed)
        row = {"page": ordinal, "stem": page.stem, **check["matches"], "prompt": check["identical"]}
        row["request"] = None
        if page.call is not None:
            sent = page.call["generation_sent"]
            image = (
                tree.blob(page.feed["page_render"]["image_path"])
                if page.feed["page_render"]
                else None
            )
            images_ok = (
                [hashlib.sha256(image).hexdigest()] == page.call["image_sha256s"]
                if image
                else not page.call["image_sha256s"]
            )
            body, _ = build_body(
                page.feed,
                image,
                model_name=page.call["served_model_id"],
                sampling=sealed,
                seed=sent["seed"],
                max_tokens=sent["max_tokens"],
                stream=page.call.get("stream") is not None,
            )
            row["image"] = images_ok
            row["request"] = hashlib.sha256(body).hexdigest() == page.call["request_sha256"]
        rows.append(row)
    return {
        "pages": len(rows),
        "prompts_identical": sum(r["prompt"] for r in rows),
        "requests_checked": sum(r["request"] is not None for r in rows),
        "requests_identical": sum(bool(r["request"]) for r in rows),
        "rows": rows,
    }


# --- sending ------------------------------------------------------------------------


def _stream(url: str, body: bytes, timeout: float, guard: dict[str, int]) -> dict[str, Any]:
    """POST a streamed request; stop reading at the first repetition loop."""
    from common.repetition_loop import LoopScanner
    from operations.serving.http import SSE_DONE, sse_data, sse_events, stream_chunk_content

    request = urllib.request.Request(
        url + "/v1/chat/completions", data=body, headers={"Content-Type": "application/json"}
    )
    started = time.monotonic()
    raw = bytearray()
    out: dict[str, Any] = {"http_status": None, "error": None, "loop_stop": None}
    content: list[str] = []
    finish, usage, pending = None, None, b""
    scanner = LoopScanner(guard)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            out["http_status"] = response.status
            while True:
                chunk = (
                    response.read1(65536) if hasattr(response, "read1") else response.read(65536)
                )
                if not chunk:
                    break
                raw.extend(chunk)
                events, pending = sse_events(pending + chunk)
                stop = False
                for event in events:
                    try:
                        data = sse_data(event)
                    except UnicodeDecodeError:
                        continue
                    if data is None or data == SSE_DONE:
                        continue
                    piece = stream_chunk_content(data)
                    content.append(piece)
                    try:
                        parsed = json.loads(data)
                    except ValueError:
                        continue
                    choices = parsed.get("choices") or []
                    if choices and choices[0].get("finish_reason"):
                        finish = choices[0]["finish_reason"]
                    if parsed.get("usage"):
                        usage = parsed["usage"]
                    if scanner.feed(piece) is not None:
                        stop = True
                        break
                if stop:
                    out["loop_stop"] = scanner.finding
                    break
    except urllib.error.HTTPError as failure:
        out.update(http_status=failure.code, error=f"HTTP {failure.code}")
        raw.extend(failure.read())
    except (OSError, urllib.error.URLError) as failure:
        out["error"] = f"{type(failure).__name__}: {failure}"
    out.update(
        content="".join(content),
        finish_reason="repetition-loop" if out["loop_stop"] else finish,
        engine_finish_reason=finish,
        usage=usage,
        seconds=round(time.monotonic() - started, 3),
        raw=bytes(raw),
    )
    return out


def _whole(url: str, body: bytes, timeout: float) -> dict[str, Any]:
    result = W.post(url, json.loads(body), timeout)
    raw = (result.get("raw_response") or "").encode("utf-8")
    if result.get("raw_response_b64"):
        raw = base64.b64decode(result["raw_response_b64"])
    return {
        "http_status": result["http_status"],
        "error": result["error"],
        "loop_stop": None,
        "content": result["text"] or "",
        "finish_reason": result["finish_reason"],
        "engine_finish_reason": result["finish_reason"],
        "usage": result["usage"],
        "seconds": result["seconds"],
        "raw": raw,
    }


def reading_text(answer: dict[str, Any] | None) -> str:
    """Every entry's text in order, one per line block: what the scorecard compares."""
    if not answer:
        return ""
    return "\n".join(
        str(e.get("text") or "") for e in answer.get("acts") or [] if isinstance(e, dict)
    )


def page_record(
    page: Page, feed: dict[str, Any], variant: Variant, args: argparse.Namespace,
    body: bytes, image: bytes | None, result: dict[str, Any],
    mutation: dict[str, Any] | None = None,
) -> dict[str, Any]:  # fmt: skip
    from common.page_answer import parse_page_answer

    check = prompt_check(feed)
    sent_sha = hashlib.sha256(body).hexdigest()
    recorded = page.call["request_sha256"] if page.call else None
    if result["error"] is None:
        state, answer, problems = parse_page_answer(result["content"])
    else:
        state, answer, problems = None, None, []
    return {
        "schema": SCHEMA,
        "arm": ARM.name,
        "label": args.label,
        "model_name": args.model_name,
        "sampling_name": args.sampling,
        "run_tree": str(args.run_tree),
        "run_id": (page.reading or {}).get("provenance", {}).get("run_id")
        or Path(args.run_tree).name,
        "page": page.stem,
        "page_ordinal": page.ordinal,
        "variant": variant.record(),
        "mutation": None
        if mutation is None
        else {k: v for k, v in mutation.items() if k != "feed"},
        "feed": feed,
        "prompt": {
            **check["rebuilt"],
            "matches_run": check["identical"],
            "feed_changed": variant.feed_changed,
        },
        "request": {
            "sha256": sent_sha,
            "matches_run": recorded == sent_sha if recorded else None,
            "max_tokens": args.max_tokens,
            "seed": args.seed,
            "stream": not args.no_stream,
            "sampling": sampling_for(args.sampling),
            "image_sha256": None if image is None else hashlib.sha256(image).hexdigest(),
            "image_mode": variant.image,
        },
        "http_status": result["http_status"],
        "content": result["content"],
        "finish_reason": result["finish_reason"],
        "engine_finish_reason": result["engine_finish_reason"],
        "loop_stop": result["loop_stop"],
        "usage": result["usage"],
        "seconds": result["seconds"],
        "raw_sha256": hashlib.sha256(result["raw"]).hexdigest(),
        "raw_bytes": len(result["raw"]),
        "parse_state": state,
        "parse_problems": problems,
        "answer": answer,
        "text": reading_text(answer),
        "error": result["error"],
        "written": W.now(),
    }


def _load_added(variant: Variant, stem: str) -> dict[str, Any]:
    added = {}
    for label, folder in variant.add:
        path = Path(folder) / f"{stem}.json"
        added[label] = json.loads(path.read_text("utf-8")) if path.is_file() else None
    return added


def _pick(tree: RunTree, pages: str | None, limit: int | None) -> list[Page]:
    chosen = [tree.pages[o] for o in sorted(tree.pages)]
    if pages:
        wanted = {p.strip() for p in pages.split(",") if p.strip()}
        chosen = [p for p in chosen if p.stem in wanted or str(p.ordinal) in wanted]
        missing = wanted - {p.stem for p in chosen} - {str(p.ordinal) for p in chosen}
        if missing:
            raise SystemExit(f"--pages names pages not in the run tree: {sorted(missing)}")
    return chosen[: limit or None]


def _same_setup(path: Path, variant: Variant, args: argparse.Namespace) -> None:
    try:
        old = json.loads(path.read_text("utf-8"))
    except (OSError, ValueError):
        return
    mine = (variant.record(), args.model_name, args.sampling)
    theirs = (old.get("variant"), old.get("model_name"), old.get("sampling_name"))
    if mine != theirs:
        raise SystemExit(
            f"{path.parent} already holds another setup {theirs}; use a new --label for {mine}"
        )


def run(args: argparse.Namespace) -> int:
    tree = load_run_tree(args.run_tree)
    variant = variant_from_args(args)
    sampling = sampling_for(args.sampling)
    guard = loop_guard()
    folder = args.out / args.label
    folder.mkdir(parents=True, exist_ok=True)
    pages = _pick(tree, args.pages, args.limit)
    todo = []
    for page in pages:
        path = folder / f"{page.stem}.json"
        _same_setup(path, variant, args)
        if W.cached_ok(path):
            continue
        mutation = load_mutation(variant, page.stem)
        feed = apply_variant(page.feed, variant, _load_added(variant, page.stem), mutation)
        if (
            not variant.feed_changed
            and not prompt_check(feed)["identical"]
            and not args.accept_new_builder
        ):
            raise SystemExit(
                f"page {page.ordinal}: the rebuilt prompt differs from the run's recorded digests "
                "(the builder changed since the run); pass --accept-new-builder to send it anyway"
            )
        image = page_image(tree, page, variant)
        body, _ = build_body(
            feed, image, model_name=args.model_name, sampling=sampling, seed=args.seed,
            max_tokens=args.max_tokens, stream=not args.no_stream,
        )  # fmt: skip
        todo.append((page, feed, image, body, mutation))
    if not todo:
        W.event(args.out, "nothing-to-do", model=args.label)
        return 0
    server = None
    url = (args.server_url or "").rstrip("/")
    if not url:
        weights = A.resolve_weights(ARM, args.weights, None, None)
        row = A.serving_row(ARM.chair, args.tier or ARM.default_tier)
        argv = A.server_argv(
            row, weights, port=args.port, served_name=args.model_name,
            gpu_memory_utilization=args.gpu_memory_utilization, max_num_seqs=args.max_num_seqs,
            max_model_len=args.max_model_len, max_num_batched_tokens=args.max_num_batched_tokens,
        )  # fmt: skip
        prefix = args.vllm_cmd or [sys.executable, "-m", "vllm.entrypoints.cli.main"]
        W.event(args.out, "server-start", model=args.label, weights=str(weights))
        server = W.Server(prefix, argv, args.port, folder / "server.log")
        try:
            server.wait_ready(args.startup_timeout)
        except BaseException:
            server.stop()
            raise
        W.event(args.out, "server-ready", model=args.label)
        url = server.url
    errors = 0
    started = time.monotonic()
    try:
        W.event(args.out, "requests-start", model=args.label, pages=len(todo))

        def send(item):
            page, feed, image, body, mutation = item
            if args.no_stream:
                result = _whole(url, body, args.request_timeout)
            else:
                result = _stream(url, body, args.request_timeout, guard)
            return page, feed, image, body, mutation, result

        with ThreadPoolExecutor(max_workers=max(1, args.concurrency)) as pool:
            for future in as_completed([pool.submit(send, item) for item in todo]):
                page, feed, image, body, mutation, result = future.result()
                record = page_record(page, feed, variant, args, body, image, result, mutation)
                errors += record["error"] is not None
                with gzip.open(folder / f"{page.stem}.sse.gz", "wb") as handle:
                    handle.write(result["raw"])
                W.write_json(folder / f"{page.stem}.json", record)
    finally:
        if server is not None:
            server.stop()
            W.event(args.out, "server-stopped", model=args.label)
    summary = {
        "model": args.label,
        "model_name": args.model_name,
        "pages": len(todo),
        "errors": errors,
        "wall_seconds": round(time.monotonic() - started, 3),
        "concurrency": args.concurrency,
        "variant": variant.record(),
        "finished": W.now(),
    }
    W.write_json(folder / "run.json", summary)
    W.event(args.out, "requests-done", model=args.label, wall_seconds=summary["wall_seconds"])
    return 1 if errors else 0


# --- command line -------------------------------------------------------------------


def _pairs(values: list[str], flag: str) -> tuple[tuple[str, str], ...]:
    out = []
    for value in values:
        for item in value.split(","):
            if not item.strip():
                continue
            if "=" not in item:
                raise SystemExit(f"{flag} takes LABEL=VALUE, not {item!r}")
            key, val = item.split("=", 1)
            out.append((key.strip(), val.strip()))
    return tuple(out)


def variant_from_args(args: argparse.Namespace) -> Variant:
    drop = tuple(x.strip() for v in args.drop_witness for x in v.split(",") if x.strip())
    order = tuple(x.strip() for x in (args.witness_order or "").split(",") if x.strip())
    return Variant(
        drop=drop,
        add=_pairs(args.add_witness, "--add-witness"),
        letter_map=_pairs(args.letter_map, "--letter-map"),
        order=order,
        image=args.image,
        blur_radius=args.blur_radius,
        mutations=str(args.mutations) if args.mutations else None,
    )


def _variant_options(p: argparse.ArgumentParser) -> None:
    p.add_argument("--drop-witness", action="append", default=[], metavar="LABEL")
    p.add_argument("--add-witness", action="append", default=[], metavar="LABEL=CACHE_DIR")
    p.add_argument("--letter-map", action="append", default=[], metavar="LABEL=LETTER")
    p.add_argument("--witness-order", metavar="LABEL,LABEL,...")
    p.add_argument("--image", choices=IMAGE_MODES, default="clear")
    p.add_argument("--blur-radius", type=float, default=8.0)
    p.add_argument("--mutations", type=Path, metavar="DIR", help="witness-mutation.v1 records")


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = parser.add_subparsers(dest="command", required=True)
    p = sub.add_parser("prompts", help="rebuild every prompt and request; compare with the run")
    p.add_argument("--run-tree", type=Path, required=True)
    p.add_argument("--json", type=Path, help="write the per-page check here")
    p.add_argument("--show", type=int, metavar="ORDINAL", help="print one page's variant prompt")
    _variant_options(p)
    r = sub.add_parser("run", help="send every page's request to a served model")
    r.add_argument("--run-tree", type=Path, required=True)
    r.add_argument("--out", type=Path, required=True)
    r.add_argument("--label", required=True, help="cache folder: name the model and variant")
    r.add_argument(
        "--model-name", required=True, help="the served model name (base, adapter, merge)"
    )
    r.add_argument("--sampling", choices=["greedy", "sealed"], default="greedy")
    r.add_argument("--seed", type=int, default=0)
    r.add_argument("--max-tokens", type=int, default=MAX_TOKENS)
    r.add_argument("--no-stream", action="store_true", help="one whole reply, no loop guard")
    r.add_argument("--pages", help="comma-separated stems or ordinals")
    r.add_argument("--limit", type=int)
    r.add_argument("--concurrency", type=int, default=16)
    r.add_argument("--accept-new-builder", action="store_true")
    r.add_argument("--server-url", help="a running server (vLLM's OpenAI API)")
    r.add_argument("--weights", type=Path, help="start vLLM on this snapshot instead")
    r.add_argument("--vllm-cmd", nargs="+", help="command prefix before 'serve'")
    r.add_argument("--tier")
    r.add_argument("--port", type=int, default=8190)
    r.add_argument("--max-model-len", type=int)
    r.add_argument("--max-num-seqs", type=int, default=32)
    r.add_argument("--max-num-batched-tokens", type=int)
    r.add_argument("--gpu-memory-utilization", type=float, default=0.92)
    r.add_argument("--startup-timeout", type=float, default=1200)
    r.add_argument("--request-timeout", type=float, default=1800)
    _variant_options(r)
    args = parser.parse_args(argv)
    if args.command == "run" and not args.server_url and not args.weights:
        parser.error("run needs --server-url or --weights")
    return args


def prompts(args: argparse.Namespace) -> int:
    tree = load_run_tree(args.run_tree)
    if args.show is not None:
        from common import page_prompt

        variant = variant_from_args(args)
        page = tree.pages[args.show]
        mutation = load_mutation(variant, page.stem)
        feed = apply_variant(page.feed, variant, _load_added(variant, page.stem), mutation)
        print(page_prompt.build_page_prompt(feed["prompt"]["serving_recipe"], feed))
        return 0
    report = verify_prompts(tree)
    if args.json:
        W.write_json(args.json, report)
    print(
        f"pages {report['pages']}: prompts byte-identical {report['prompts_identical']}; "
        f"requests byte-identical {report['requests_identical']} of {report['requests_checked']} "
        "with a recorded call"
    )
    for row in report["rows"]:
        if not row["prompt"] or row["request"] is False:
            print(f"  differs: page {row['page']} {row['stem']}: {row}")
    ok = report["prompts_identical"] == report["pages"]
    ok = ok and report["requests_identical"] == report["requests_checked"]
    return 0 if ok else 1


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    if args.command == "prompts":
        return prompts(args)
    return run(args)


if __name__ == "__main__":
    sys.exit(main())
