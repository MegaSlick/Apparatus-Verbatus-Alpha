"""The fed-witness reader arm (`qwen-fed`): the Perlector's own page request, sent to any model.

    python -m operations.bakeoff.fed_arm prompts --run-tree RUN
    python -m operations.bakeoff.fed_arm run --run-tree RUN --out CACHE --label NAME \\
        --model-name SERVED (--server-url URL | --weights DIR) [variant options]

A sealed run tree already holds everything a Perlector page reading was shown: the
`page-feed` record of every page (each witness's units as the reader saw them, Surya's
lines and blocks, the switches) and the page render it sent (`4_perlector/blobs/`).
A pipeline run tree (one with `run.json`) is used only after its Exemplar and
Perlector stage seals verify (`verify_seals`), and every reference followed -- a
reading's feed, its call record, the render -- is checked against its recorded digest.
`prompts` rebuilds every page's prompt with `common.page_prompt.build_page_prompt` and
checks it against the digests the run recorded (`prompt.rendered_sha256`,
`instruction_sha256`; `builder_sha256` is reported apart), rebuilds the request's images
as stage 4 sends them (the render, then the overlay when the feed draws one), checks
them and the reading's `request_digest`, and rebuilds the whole request body with
`operations.serving.http.request_body` and checks it against the request digest of the
run's own call record, so what this arm sends is provably what the pipeline sent. It
also reports where this checkout's Perlector sampling or loop guard differs from the
run's sealed `config/decoding.toml` (`run` refuses then, unless `--accept-new-config`).

`run` sends that request -- the render (and overlay) first, then the rebuilt text, one user turn,
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
  count copied planted errors. The other feed variants apply on top of it. A record
  must name the reference it was planted from (`reference_sha256`, the scored words,
  and `reference_record_sha256`, the whole reference); both enter the cache identity,
  and with `--gold DIR` (and `--gold-glob`, `--row-kind`) they are checked against the
  reference rebuilt from that gold as `mutations` builds it, before anything is sent.

Each page is cached at `<out>/<label>/<page stem>.json` (schema `bakeoff-fed-page.v1`):
the setup (`setup_of`: the run tree by content, served name, `--repo`/`--revision`/
`--recipe` of the checkpoint behind it, sampling, seed, token cap, stream mode, loop
guard, variant, decoding digest, the page's mutation identity), the feed as shown, the
prompt evidence and whether it matches the run's, the request digest and image digests,
the reply's content, finish reason, usage, any loop stop, the grammar's parse
(`common.page_answer`) and the reading's text. A label holds one setup: a page cached
under another, or whose request bytes now differ, is refused, never mixed in. A page
cached without an error is skipped next time; one that ran into the request timeout
(cut at a real total deadline) is a terminal failure, not resent at the same request,
timeout and concurrency. The streamed reply's raw bytes sit beside it as
`<page stem>.sse.gz`.

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


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


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
    # What the tree is, by content (so a copy at another path is the same tree):
    # run id, run.json's digest, the digest of every feed and page record, and
    # whether its stage seals were verified (`identity()`).
    sealed: bool = False
    run_id: str | None = None
    run_sha256: str | None = None
    records_sha256: str | None = None
    decoding_sha256: str | None = None  # the run's sealed config/decoding.toml digest

    def blob(self, relative: str) -> bytes:
        return (self.root / relative).read_bytes()

    def verified_blob(self, ref: dict[str, Any], what: str) -> bytes:
        """`ref`'s bytes, refused unless they match its recorded sha256."""
        data = self.blob(ref["relative_path"])
        if not isinstance(ref.get("sha256"), str) or _sha(data) != ref["sha256"]:
            raise SystemExit(
                f"{self.root}: {what} {ref['relative_path']} does not match its recorded digest"
            )
        return data

    def render(self, page: Page) -> bytes | None:
        """The page render the feed records, digest-checked."""
        render = page.feed["page_render"]
        if render is None:
            return None
        ref = {"relative_path": render["image_path"], "sha256": render["image_sha256"]}
        return self.verified_blob(ref, f"page {page.stem}'s render")

    def by_stem(self) -> dict[str, Page]:
        return {p.stem: p for p in self.pages.values()}

    def identity(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "run_sha256": self.run_sha256,
            "records_sha256": self.records_sha256,
            "sealed": self.sealed,
        }


# The stage seals a fed page reads under, as (producer, the reader that proves it):
# the Exemplar's page records (the stems) and the Perlector's feeds, renders,
# readings and call records.
SEALS = (("exemplar", "ink-map"), ("perlector", "recensor"))


def is_real_run(root: Path) -> bool:
    """A pipeline run tree (it has the run authority, `run.json`), whose seals are proven
    before use; a synthetic test tree has none, and its pages are cached as unsealed."""
    return (Path(root) / "run.json").is_file()


def verify_seals(root: Path) -> None:
    """Prove the run's stage seals as the pipeline's own readers prove them, or refuse.

    A seal names every artifact and blob its stage wrote by digest, so a feed,
    render or call record changed or swapped after the run fails here.
    """
    from common.contracts.errors import ContractError
    from common.runtree.store import RunTree as Store
    from common.stage import verify_stage_seal

    try:
        store = Store(root.parent, root.name)
        for producer, reader in SEALS:
            verify_stage_seal(store, producer, reader)
    except (ContractError, OSError, ValueError, KeyError) as error:
        raise SystemExit(
            f"{root}: the run tree's stage seals do not verify ({error}); a fed page is "
            "sent only from a run whose stage seals prove it"
        ) from error


def load_run_tree(root: Path, *, sealed: bool = False) -> RunTree:
    """Every page's feed, stem, first reading and call record from a run tree.

    With `sealed`, the tree's Exemplar and Perlector stage seals are proven first
    (`verify_seals`). Either way every reference followed is digest-checked (a
    reading's feed, its call record), and each feed must name the page the
    Exemplar's record of its ordinal names.
    """
    root = Path(root)
    if sealed:
        verify_seals(root)
    records = []
    stems, page_ids = {}, {}
    for path in sorted((root / "1_exemplar/artifacts/page").glob("*.json")):
        data = path.read_bytes()
        records.append((f"1_exemplar/artifacts/page/{path.name}", _sha(data)))
        envelope = json.loads(data)
        page = envelope["payload"]
        stems[page["ordinal"]] = Path(page["declared_path"]).stem
        page_ids[page["ordinal"]] = envelope.get("subject_id")
    feeds: dict[str, tuple[dict[str, Any], str]] = {}
    for path in sorted((root / "4_perlector/artifacts/page-feed").glob("*.json")):
        data = path.read_bytes()
        relative = f"4_perlector/artifacts/page-feed/{path.name}"
        records.append((relative, _sha(data)))
        feeds[relative] = (json.loads(data)["payload"], _sha(data))
    if not feeds:
        raise SystemExit(f"no page-feed records under {root}/4_perlector/artifacts/page-feed")
    tree = RunTree(root, sealed=sealed)
    tree.records_sha256 = _sha(json.dumps(sorted(records)).encode())
    run_json = root / "run.json"
    if run_json.is_file():
        data = run_json.read_bytes()
        tree.run_sha256 = _sha(data)
        authority = json.loads(data)
        tree.run_id = authority.get("run_id")
        tree.decoding_sha256 = (authority.get("sealed_config_digests") or {}).get("decoding")
    tree.run_id = tree.run_id or root.name
    for feed, _ in feeds.values():
        ordinal = feed["page_ordinal"]
        if ordinal in tree.pages:
            raise SystemExit(f"two page feeds for page {ordinal} in {root}")
        if page_ids.get(ordinal) is not None and page_ids[ordinal] != feed["page_id"]:
            raise SystemExit(
                f"{root}: the feed for page {ordinal} names page {feed['page_id']}, "
                f"the Exemplar's record names {page_ids[ordinal]}"
            )
        tree.pages[ordinal] = Page(ordinal, stems.get(ordinal, f"page-{ordinal:04d}"), feed)
    for path in sorted((root / "4_perlector/artifacts/page-reading").glob("*.json")):
        reading = _payload(path)
        page = tree.pages.get(reading["page_ordinal"])
        if page is None or reading["attempt_ordinal"] != 1:
            continue
        ref = reading["feed_ref"]
        found = feeds.get(ref["relative_path"])
        if found is None or found[0] is not page.feed:
            continue
        if ref.get("sha256") != found[1]:
            raise SystemExit(
                f"{root}: page {page.ordinal}'s reading names its feed with digest "
                f"{ref.get('sha256')}, the feed on disk is {found[1]}"
            )
        if reading.get("page_id", page.feed["page_id"]) != page.feed["page_id"]:
            raise SystemExit(f"{root}: page {page.ordinal}'s reading names another page")
        page.reading = reading
        call = reading.get("engine_call")
        if call:
            data = tree.verified_blob(call["call_record_ref"], f"page {page.ordinal}'s call record")
            page.call = json.loads(data)
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


# The feed fields a witness mutation never changes (it changes the witness rows and,
# for a blinded feed, the regime): they must be the source feed's own, so a mutation
# made from another run's or another page's feed is refused.
MUTATION_FIXED = ("page_id", "page_ordinal", "page_size", "page_render", "surya", "feed_digest")


def load_mutation(
    variant: Variant,
    stem: str,
    source: dict[str, Any] | None = None,
    references: dict[str, dict[str, str]] | None = None,
) -> dict[str, Any] | None:
    """The page's witness-mutation record under `variant.mutations`, or None.

    The record must be this page's and made from this feed: its page stem and
    ordinal, its page digest (the render's), and the feed fields a mutation keeps
    (`MUTATION_FIXED`, including the original `feed_digest`) are checked against
    `source`, the run's sealed feed. It must name its reference by both digests
    (`mutations.REFERENCE_KEYS`); with `references` (per stem, the digests of the
    reference the caller will score against, `mutations.reference_identities`) they
    must be that reference's. Its file digest is kept as `_record_sha256` (the
    mutation's identity in the cache).
    """
    if not variant.mutations:
        return None
    path = Path(variant.mutations) / f"{stem}.json"
    if not path.is_file():
        raise SystemExit(f"--mutations {variant.mutations}: no record for page {stem!r}")
    data = path.read_bytes()
    record = json.loads(data)
    if record.get("schema") != "witness-mutation.v1":
        raise SystemExit(f"{path} is not a witness-mutation.v1 record")
    if source is not None:
        render = source.get("page_render") or {}
        expected_sha = (
            render.get("image_sha256") or source.get("page_id") or str(source.get("page_ordinal"))
        )  # mutations.page_sha's rule
        problems = []
        if record.get("page") != stem:
            problems.append(f"page {record.get('page')!r}")
        if record.get("page_ordinal") != source["page_ordinal"]:
            problems.append(f"page ordinal {record.get('page_ordinal')}")
        if record.get("page_sha") != expected_sha:
            problems.append("page digest")
        feed = record.get("feed") or {}
        problems += [f"feed {k}" for k in MUTATION_FIXED if feed.get(k) != source.get(k)]
        if problems:
            raise SystemExit(
                f"{path} was not made from this run's feed for page {stem}: "
                f"{', '.join(problems)} differ"
            )
    from operations.bakeoff import mutations as M

    unbound = [k for k in M.REFERENCE_KEYS if not record.get(k)]
    if unbound:
        raise SystemExit(
            f"{path} does not name the reference it was planted from ({', '.join(unbound)} "
            "missing); rebuild it with operations.bakeoff.mutations"
        )
    if references is not None:
        differ = M.reference_mismatch(record, references.get(stem))
        if differ:
            raise SystemExit(
                f"{path} was planted from another reference than --gold gives for page "
                f"{stem} ({', '.join(differ)}); rebuild the mutations from this reference"
            )
    return {**record, "_record_sha256": _sha(data)}


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
    if variant.image == "none":
        feed["overlay"] = None  # drawn on the render, so gone with it
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
        feed["switches"] = {**feed["switches"], "page_image": "off", "page_overlay": "off"}
    return feed


def page_image(tree: RunTree, page: Page, variant: Variant) -> bytes | None:
    """The render this variant sends, or None (digest-checked against its feed)."""
    if variant.image == "none":
        return None
    if variant.image == "swap":
        ordinals = sorted(o for o, p in tree.pages.items() if p.feed["page_render"])
        other = ordinals[(ordinals.index(page.ordinal) + 1) % len(ordinals)]
        return tree.render(tree.pages[other])
    png = tree.render(page)
    if png is None:
        return None
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


def request_images(
    tree: RunTree, page: Page, feed: dict[str, Any], variant: Variant
) -> tuple[list[bytes], dict[str, Any]]:
    """(the images this variant sends, in the pipeline's order, the feed as shown).

    As stage 4 sends them (`common.page_path.request_images`): the render, then the
    overlay when the feed draws one. The run's own overlay is redrawn and checked
    against its recorded digest (`page_overlay.overlay_image`); when the variant
    changes the rows or the render, the overlay is drawn afresh from the shown feed
    on the render as sent, and the shown feed's `overlay` record says so.
    """
    from common import page_overlay

    render = page_image(tree, page, variant)
    images = [] if render is None else [render]
    if render is None or feed.get("overlay") is None:
        return images, feed
    if feed is page.feed and variant.image == "clear":
        images.append(page_overlay.overlay_image(feed, tree.blob))
        return images, feed
    plan = page_overlay.overlay_plan(feed)
    png = page_overlay.draw_page_overlay(render, plan)
    feed = {
        **feed,
        "overlay": {
            **plan,
            "source_image_sha256": _sha(render),
            "renderer_sha256": page_overlay.RENDERER_SHA256,
            "image_sha256": _sha(png),
        },
    }
    images.append(png)
    return images, feed


# --- the request --------------------------------------------------------------------


def decoding_policy() -> tuple[dict[str, Any], str]:
    """This checkout's sealed decoding policy (config/decoding.toml) and its digest."""
    from common.decoding import load_decoding_policy

    return load_decoding_policy(A.ROOT / "config" / "decoding.toml")


def sealed_sampling() -> dict[str, int | float]:
    from common.decoding import chair_decoding

    return chair_decoding(decoding_policy()[0], "perlector")


def loop_guard() -> dict[str, int]:
    from common.decoding import perlector_loop_guard

    return perlector_loop_guard(decoding_policy()[0])


def sampling_for(name: str) -> dict[str, int | float]:
    sealed = sealed_sampling()
    if name == "sealed":
        return sealed
    if name == "greedy":
        return {**sealed, **GREEDY}
    raise SystemExit(f"unknown sampling {name!r}: greedy or sealed")


def _wire(value: Any) -> Any:
    """A call record's wire-decimal value as the number it sent."""
    if isinstance(value, dict) and value.get("schema") == "wire-decimal.v1":
        return float(value["decimal"])
    return value


def config_differences(tree: RunTree) -> list[str]:
    """Where this checkout's Perlector sampling and loop guard differ from the run's.

    The run's sealed `config/decoding.toml` digest (run.json `sealed_config_digests`)
    against this checkout's, then each recorded call's sent sampling and loop guard
    against what this checkout would send. Empty when they agree.
    """
    found = []
    _, digest = decoding_policy()
    if tree.decoding_sha256 is not None and tree.decoding_sha256 != digest:
        found.append(
            f"config/decoding.toml is {digest[:12]}, the run sealed {tree.decoding_sha256[:12]}"
        )
    sealed, guard = sealed_sampling(), loop_guard()
    for ordinal in sorted(tree.pages):
        call = tree.pages[ordinal].call
        if not call:
            continue
        sent = {k: _wire(v) for k, v in (call.get("sampling_effective") or {}).items()}
        if sent and sent != {k: float(v) if isinstance(v, float) else v for k, v in sealed.items()}:
            found.append(f"page {ordinal}: the run sent sampling {sent}, this checkout {sealed}")
        recorded = (call.get("stream") or {}).get("loop_guard")
        if recorded is not None and recorded != guard:
            found.append(f"page {ordinal}: the run's loop guard {recorded}, this checkout {guard}")
    return found


PROMPT_TEXT_FIELDS = ("serving_recipe", "rendered_sha256", "instruction_sha256")


def prompt_check(feed: dict[str, Any]) -> dict[str, Any]:
    """The rebuilt prompt's evidence beside the run's, field by field.

    `identical`: every field, the builder's code digest included. `text_identical`:
    the recipe, the rendered text and the instruction -- the bytes the model is sent
    -- whatever else changed in the builder's module (a recipe alias, say).
    """
    from common import page_prompt

    recipe = feed["prompt"]["serving_recipe"]
    rebuilt = page_prompt.page_prompt_evidence(recipe, feed)
    sealed = feed["prompt"]
    return {
        "rebuilt": rebuilt,
        "matches": {k: rebuilt[k] == sealed.get(k) for k in rebuilt},
        "identical": rebuilt == sealed,
        "text_identical": all(rebuilt[k] == sealed.get(k) for k in PROMPT_TEXT_FIELDS),
    }


def build_body(
    feed: dict[str, Any],
    images: list[bytes] | bytes | None,
    *,
    model_name: str,
    sampling: dict[str, int | float],
    seed: int,
    max_tokens: int,
    stream: bool,
) -> tuple[bytes, str]:
    """(the request body as the pipeline renders it, the prompt text).

    `images` are the request's images in order (`request_images`: the render, then
    the overlay); a single image or None is accepted as the render alone.
    """
    from common import page_prompt
    from operations.serving.chat_request import image_content_blocks
    from operations.serving.http import request_body

    if images is None:
        images = []
    elif isinstance(images, (bytes, bytearray)):
        images = [bytes(images)]
    text = page_prompt.build_page_prompt(feed["prompt"]["serving_recipe"], feed)
    content = [*image_content_blocks(list(images)), {"type": "text", "text": text}]
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


def request_check(tree: RunTree, page: Page) -> dict[str, Any]:
    """The page's whole request rebuilt as the run sent it, against what the run recorded.

    The images (render, then overlay) against the call record's `image_sha256s` and the
    feed's own digests (`page_path.request_image_sha256s`); the reading's
    `request_digest` (text and image digests, `page_path.request_digest`); and the
    whole body -- served name, seed, token cap, sealed sampling, stream mode, every
    image byte and the text -- against the call's `request_sha256`. `None` fields
    where the run recorded nothing to compare.
    """
    from common import page_path

    images, _ = request_images(tree, page, page.feed, Variant())
    digests = [_sha(image) for image in images]
    out: dict[str, Any] = {
        "images": digests == page_path.request_image_sha256s(page.feed),
        "reading_digest": None,
        "call_images": None,
        "request": None,
    }
    text = None
    if page.reading is not None and page.reading.get("request_digest"):
        from common import page_prompt

        text = page_prompt.build_page_prompt(page.feed["prompt"]["serving_recipe"], page.feed)
        out["reading_digest"] = (
            page_path.request_digest(text, digests) == page.reading["request_digest"]
        )
    if page.call is not None:
        sent = page.call["generation_sent"]
        out["call_images"] = digests == page.call["image_sha256s"]
        body, _ = build_body(
            page.feed,
            images,
            model_name=page.call["served_model_id"],
            sampling=sealed_sampling(),
            seed=sent["seed"],
            max_tokens=sent["max_tokens"],
            stream=page.call.get("stream") is not None,
        )
        out["request"] = _sha(body) == page.call["request_sha256"]
    out["identical"] = all(v is not False for v in out.values())
    return out


def verify_prompts(tree: RunTree) -> dict[str, Any]:
    """Rebuild every page's prompt and whole request; compare with what the run recorded."""
    rows = []
    for ordinal in sorted(tree.pages):
        page = tree.pages[ordinal]
        check = prompt_check(page.feed)
        row = {
            "page": ordinal,
            "stem": page.stem,
            **check["matches"],
            "prompt": check["text_identical"],
            "builder": check["identical"],
        }
        request = request_check(tree, page)
        row.update(
            images=request["images"] and request["call_images"] is not False,
            reading_digest=request["reading_digest"],
            request=request["request"],
        )
        rows.append(row)
    return {
        "pages": len(rows),
        "prompts_identical": sum(r["prompt"] for r in rows),
        "builder_identical": sum(r["builder"] for r in rows),
        "images_identical": sum(r["images"] for r in rows),
        "reading_digests_identical": sum(bool(r["reading_digest"]) for r in rows),
        "reading_digests_checked": sum(r["reading_digest"] is not None for r in rows),
        "requests_checked": sum(r["request"] is not None for r in rows),
        "requests_identical": sum(bool(r["request"]) for r in rows),
        "config": config_differences(tree),
        "rows": rows,
    }


# --- sending ------------------------------------------------------------------------


def _stream(url: str, body: bytes, timeout: float, guard: dict[str, int]) -> dict[str, Any]:
    """POST a streamed request; stop reading at the first repetition loop, or when
    `timeout` seconds have passed in all (`W.read_chunk`: never one socket wait past it)."""
    from common.repetition_loop import LoopScanner
    from operations.serving.http import SSE_DONE, sse_data, sse_events, stream_chunk_content

    request = urllib.request.Request(
        url + "/v1/chat/completions", data=body, headers={"Content-Type": "application/json"}
    )
    started = time.monotonic()
    deadline = started + timeout
    raw = bytearray()
    out: dict[str, Any] = {"http_status": None, "error": None, "loop_stop": None, "stop": None}
    content: list[str] = []
    finish, usage, pending = None, None, b""
    scanner = LoopScanner(guard)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            out["http_status"] = response.status
            while True:
                chunk = W.read_chunk(response, deadline, timeout)
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
                if time.monotonic() > deadline:
                    raise TimeoutError("the request's total deadline passed")
    except urllib.error.HTTPError as failure:
        out.update(http_status=failure.code, error=f"HTTP {failure.code}")
        raw.extend(failure.read())
    except (OSError, urllib.error.URLError) as failure:
        if W._timed_out(failure):
            out.update(stop=W.REQUEST_TIMEOUT, error=f"{W.REQUEST_TIMEOUT} after {timeout:g} s")
        else:
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
        "stop": result.get("stop"),
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
    return "\n".join(str(e.get("text") or "") for e in answer_entries(answer))


def answer_entries(answer: dict[str, Any] | None) -> list[dict[str, Any]]:
    """An answer's entries in either grammar: `entries` (page types named) or `acts`."""
    if not answer:
        return []
    entries = answer.get("entries") if "entries" in answer else answer.get("acts")
    return [e for e in entries or [] if isinstance(e, dict)]


# --- the cache's identity -------------------------------------------------------------


def setup_of(
    args: argparse.Namespace, tree: RunTree, variant: Variant, guard: dict[str, int]
) -> dict[str, Any]:
    """Everything a label's answers depend on beyond each page's own request bytes.

    The run tree (by content), the checkpoint behind the served name (`--repo`,
    `--revision`, `--recipe`), sampling, seed, token cap, stream mode and loop guard,
    the variant, and this checkout's decoding policy digest. A label holds one setup:
    a page cached under another is refused, never mixed in.
    """
    stream = not args.no_stream
    return {
        "run": tree.identity(),
        "model_name": args.model_name,
        "repo": args.repo,
        "revision": args.revision,
        "recipe": args.recipe,
        "sampling_name": args.sampling,
        "sampling": sampling_for(args.sampling),
        "seed": args.seed,
        "max_tokens": args.max_tokens,
        "stream": stream,
        "loop_guard": guard if stream else None,
        "variant": variant.record(),
        "decoding_sha256": decoding_policy()[1],
    }


def _digest(value: Any) -> str:
    return _sha(json.dumps(value, sort_keys=True, ensure_ascii=False).encode("utf-8"))


def mutation_identity(mutation: dict[str, Any] | None) -> dict[str, Any] | None:
    if mutation is None:
        return None
    keys = (
        "_record_sha256", "scenario", "seed", "turn", "page_sha", "reference_sha256",
        "reference_record_sha256",
    )  # fmt: skip
    return {k: mutation.get(k) for k in keys}


def serving_of(args: argparse.Namespace) -> dict[str, Any]:
    """The serving conditions a timeout depends on (as `witness_run`'s)."""
    return {
        "request_timeout": args.request_timeout,
        "concurrency": args.concurrency,
        "server_url": bool(args.server_url),
        "max_num_seqs": None if args.server_url else args.max_num_seqs,
        "max_model_len": None if args.server_url else args.max_model_len,
        "max_num_batched_tokens": None if args.server_url else args.max_num_batched_tokens,
        "gpu_memory_utilization": None if args.server_url else args.gpu_memory_utilization,
    }


def cached_state(path: Path, setup: dict[str, Any], request_sha: str, settings_sha: str) -> str:
    """What to do with a page: "send", "done" (answered), or "failed" (a terminal
    failure at these very settings: sending it again would repeat it).

    A page cached under another setup, or whose request bytes differ from what this
    setup sends now (another feed, mutation, image or builder), is refused.
    """
    try:
        old = json.loads(path.read_text("utf-8"))
    except (OSError, ValueError):
        return "send"
    if old.get("setup_sha256") != _digest(setup):
        raise SystemExit(
            f"{path} was cached under another setup "
            f"({_setup_difference(old.get('setup'), setup)} differ); use a new --label"
        )
    if (old.get("request") or {}).get("sha256") != request_sha:
        raise SystemExit(
            f"{path}: this page's request now differs from the cached one (its feed, "
            "mutation, images or prompt changed); use a new --label"
        )
    if old.get("error") is None:
        return "done"
    failure = old.get("failure") or {}
    if failure.get("terminal") and failure.get("settings_sha256") == settings_sha:
        return "failed"
    return "send"


def _setup_difference(old: Any, new: dict[str, Any]) -> str:
    if not isinstance(old, dict):
        return "it was written before setups were recorded; its setup and this one"
    return ", ".join(sorted(k for k in set(old) | set(new) if old.get(k) != new.get(k)))


def page_record(
    page: Page, feed: dict[str, Any], variant: Variant, args: argparse.Namespace,
    body: bytes, images: list[bytes], result: dict[str, Any],
    mutation: dict[str, Any] | None = None, *, setup: dict[str, Any] | None = None,
    settings_sha: str | None = None,
) -> dict[str, Any]:  # fmt: skip
    from common.page_answer import parse_page_answer

    check = prompt_check(feed)
    sent_sha = _sha(body)
    recorded = page.call["request_sha256"] if page.call else None
    if result["error"] is None:
        state, answer, problems = parse_page_answer(result["content"])
    else:
        state, answer, problems = None, None, []
    failure = None
    if result.get("stop") in W.TERMINAL_STOPS:
        failure = {
            "terminal": True,
            "reasons": [result["stop"]],
            "settings": serving_of(args),
            "settings_sha256": settings_sha,
        }
    digests = [_sha(image) for image in images]
    return {
        "schema": SCHEMA,
        "arm": ARM.name,
        "label": args.label,
        "model_name": args.model_name,
        "sampling_name": args.sampling,
        "setup": setup,
        "setup_sha256": None if setup is None else _digest(setup),
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
            "text_matches_run": check["text_identical"],
            "feed_changed": variant.feed_changed,
        },
        "request": {
            "sha256": sent_sha,
            "matches_run": recorded == sent_sha if recorded else None,
            "max_tokens": args.max_tokens,
            "seed": args.seed,
            "stream": not args.no_stream,
            "sampling": sampling_for(args.sampling),
            "image_sha256": digests[0] if digests else None,
            "image_sha256s": digests,
            "image_mode": variant.image,
        },
        "http_status": result["http_status"],
        "content": result["content"],
        "finish_reason": result["finish_reason"],
        "engine_finish_reason": result["engine_finish_reason"],
        "loop_stop": result["loop_stop"],
        "usage": result["usage"],
        "seconds": result["seconds"],
        "raw_sha256": _sha(result["raw"]),
        "raw_bytes": len(result["raw"]),
        "parse_state": state,
        "parse_problems": problems,
        "answer": answer,
        "text": reading_text(answer),
        "error": result["error"],
        "failure": failure,
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


def assert_quantization(row: dict[str, Any], weights: Path) -> None:
    """Refuse a quantized recipe on a snapshot whose config.json does not declare it.

    The serving manager's guard (`operations/serving/manager.py::assert_quantization`)
    on the snapshot this arm launches, so `--recipe unproven-real-perlector-fp8` with
    bf16 `--weights` is refused rather than quantized at load. (Branch
    work/serving-nvfp4 adds the same wrapper to `arms.py` for `witness_run`.)
    """
    if row.get("quantization") is None:
        return
    from types import SimpleNamespace

    from operations.serving.errors import ServingConfigurationError
    from operations.serving.manager import assert_quantization as guard

    profile = SimpleNamespace(
        quantization=row["quantization"],
        chair=row.get("chair"),
        recipe=row.get("recipe"),
        tier=row.get("tier"),
    )
    try:
        guard(SimpleNamespace(root=Path(weights)), profile)
    except ServingConfigurationError as error:
        raise SystemExit(str(error)) from error


def mutation_references(
    args: argparse.Namespace, tree: RunTree
) -> dict[str, dict[str, str]] | None:
    """With `--gold`, the reference digests per page, built from that gold as
    `operations.bakeoff.mutations` builds them (statuses from the run's own feeds);
    without it None (the records' digests are still required and cached)."""
    if not getattr(args, "gold", None):
        return None
    from operations.bakeoff import mutations as M

    refs = M.references_from_gold_dir(args.gold, args.gold_glob, tree, args.row_kind)
    return M.reference_identities(refs)


def _prepare(
    args: argparse.Namespace, tree: RunTree, variant: Variant, setup: dict[str, Any]
) -> list[tuple]:
    """Each page's shown feed, images and body; the pages still to send."""
    todo = []
    config_new = bool(config_differences(tree))
    folder = args.out / args.label
    references = mutation_references(args, tree) if variant.mutations else None
    for page in _pick(tree, args.pages, args.limit):
        mutation = load_mutation(variant, page.stem, page.feed, references)
        feed = apply_variant(page.feed, variant, _load_added(variant, page.stem), mutation)
        if not variant.feed_changed and not args.accept_new_builder:
            check = request_check(tree, page)
            # An accepted new config changes the body's sampling, nothing else.
            skip = ("identical", "request") if config_new else ("identical",)
            problems = [k for k, v in check.items() if v is False and k not in skip]
            if not prompt_check(feed)["text_identical"]:
                problems.insert(0, "prompt text")
            if problems:
                raise SystemExit(
                    f"page {page.ordinal}: the rebuilt request differs from the run's "
                    f"({', '.join(problems)}); the builder or images changed since the run. "
                    "Pass --accept-new-builder to send it anyway"
                )
        images, feed = request_images(tree, page, feed, variant)
        body, _ = build_body(
            feed, images, model_name=args.model_name, sampling=sampling_for(args.sampling),
            seed=args.seed, max_tokens=args.max_tokens, stream=not args.no_stream,
        )  # fmt: skip
        page_setup = {**setup, "page": {"mutation": mutation_identity(mutation)}}
        settings_sha = W.settings_digest(
            args.repo, args.revision,
            [{"setup_sha256": _digest(page_setup), "request_sha256": _sha(body)}],
            serving_of(args),
        )  # fmt: skip
        state = cached_state(folder / f"{page.stem}.json", page_setup, _sha(body), settings_sha)
        if state == "failed":
            W.event(args.out, "page-not-retried", model=args.label, page=page.stem)
        if state != "send":
            continue
        todo.append((page, feed, images, body, mutation, page_setup, settings_sha))
    return todo


def run(args: argparse.Namespace) -> int:
    tree = load_run_tree(args.run_tree, sealed=is_real_run(args.run_tree))
    differences = config_differences(tree)
    if differences and not args.accept_new_config:
        raise SystemExit(
            "this checkout's Perlector decoding differs from the run's sealed one: "
            + "; ".join(differences[:3])
            + ". Run from the run's commit, or pass --accept-new-config to send this "
            "checkout's (recorded in the cache's setup)"
        )
    variant = variant_from_args(args)
    guard = loop_guard()
    setup = setup_of(args, tree, variant, guard)
    setup["accepted"] = {
        "new_builder": bool(args.accept_new_builder),
        "new_config": bool(differences),
    }
    folder = args.out / args.label
    folder.mkdir(parents=True, exist_ok=True)
    todo = _prepare(args, tree, variant, setup)
    if not todo:
        W.event(args.out, "nothing-to-do", model=args.label)
        return 0
    server = None
    url = (args.server_url or "").rstrip("/")
    if not url:
        weights = A.resolve_weights(ARM, args.weights, None, None)
        row = A.serving_row(ARM.chair, args.tier or ARM.default_tier, args.recipe)
        assert_quantization(row, weights)
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
    errors = failed = 0
    started = time.monotonic()
    try:
        W.event(args.out, "requests-start", model=args.label, pages=len(todo))

        def send(item):
            body = item[3]
            if args.no_stream:
                return item, _whole(url, body, args.request_timeout)
            return item, _stream(url, body, args.request_timeout, guard)

        with ThreadPoolExecutor(max_workers=max(1, args.concurrency)) as pool:
            for future in as_completed([pool.submit(send, item) for item in todo]):
                item, result = future.result()
                page, feed, images, body, mutation, page_setup, settings_sha = item
                record = page_record(
                    page, feed, variant, args, body, images, result, mutation,
                    setup=page_setup, settings_sha=settings_sha,
                )  # fmt: skip
                if record["failure"] is not None:
                    failed += 1
                elif record["error"] is not None:
                    errors += 1
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
        "setup": setup,
        "pages": len(todo),
        "errors": errors,
        "terminal_failures": failed,
        "wall_seconds": round(time.monotonic() - started, 3),
        "concurrency": args.concurrency,
        "variant": variant.record(),
        "finished": W.now(),
    }
    W.write_json(folder / "run.json", summary)
    W.event(args.out, "requests-done", model=args.label, wall_seconds=summary["wall_seconds"])
    # A terminal failure (timeout) is recorded and not resent at these settings, as
    # witness_run does; only other errors fail the run.
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
    p.add_argument(
        "--gold",
        type=Path,
        help="with --mutations: the reference pages; each record must be planted from them",
    )
    p.add_argument("--gold-glob", default="**/*.txt")
    p.add_argument("--row-kind", default="other", help="entry kind for index rows (as mutations)")


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
    r.add_argument(
        "--accept-new-config",
        action="store_true",
        help="send this checkout's decoding settings where they differ from the run's sealed ones",
    )
    r.add_argument(
        "--revision",
        help="the checkpoint revision served under --model-name, part of the cache's identity; "
        "required with --recipe or --repo, else the Perlector chair's pinned bf16 revision",
    )
    r.add_argument("--repo", help="the checkpoint's repository (default: the Perlector chair's)")
    r.add_argument("--recipe", help="a serving recipe for --weights (serving_recipes_real*.toml)")
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
    if args.command == "run":
        if args.revision is None and (args.recipe or args.repo):
            parser.error("--recipe and --repo name another checkpoint: say its --revision")
        if args.revision is None:
            identity = A.chair_identity(ARM.chair)
            args.repo, args.revision = identity.get("repo"), identity.get("revision")
    return args


def prompts(args: argparse.Namespace) -> int:
    tree = load_run_tree(args.run_tree, sealed=is_real_run(args.run_tree))
    if args.show is not None:
        from common import page_prompt

        variant = variant_from_args(args)
        page = tree.pages[args.show]
        references = mutation_references(args, tree) if variant.mutations else None
        mutation = load_mutation(variant, page.stem, page.feed, references)
        feed = apply_variant(page.feed, variant, _load_added(variant, page.stem), mutation)
        print(page_prompt.build_page_prompt(feed["prompt"]["serving_recipe"], feed))
        return 0
    report = verify_prompts(tree)
    if args.json:
        W.write_json(args.json, report)
    print(
        f"pages {report['pages']} (seals {'verified' if tree.sealed else 'NOT checked'}): "
        f"prompt text byte-identical {report['prompts_identical']}; "
        f"images (render and overlay) identical {report['images_identical']}; "
        f"reading request digests {report['reading_digests_identical']} of "
        f"{report['reading_digests_checked']}; whole requests byte-identical "
        f"{report['requests_identical']} of {report['requests_checked']} with a recorded call; "
        f"builder code unchanged on {report['builder_identical']}"
    )
    for line in report["config"]:
        print(f"  config differs: {line}")
    for row in report["rows"]:
        if (
            not row["prompt"]
            or not row["images"]
            or False in (row["request"], row["reading_digest"])
        ):
            print(f"  differs: page {row['page']} {row['stem']}: {row}")
    ok = report["prompts_identical"] == report["images_identical"] == report["pages"]
    ok = ok and report["requests_identical"] == report["requests_checked"]
    ok = ok and report["reading_digests_identical"] == report["reading_digests_checked"]
    return 0 if ok and not report["config"] else 1


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    if args.command == "prompts":
        return prompts(args)
    return run(args)


if __name__ == "__main__":
    sys.exit(main())
