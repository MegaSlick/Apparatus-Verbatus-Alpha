"""Bulk export: many pages, no witnesses -> the reference answer (stage 1, "bulk loose").

    python -m operations.training.bulk --references DIR_OR_GLOB [--references ...] \\
        --images-map MAP.jsonl|catalog.csv --held-out FILE --out DIR [--seed 0] \\
        [--max-pages N] [--max-per-volume N] [--val-share 0.05] \\
        [--weights-by-status agreed=0.7,close=0.5,...] [--long-edge 2560] \\
        [--no-copy-images] [--workers N]

Each example is one page read blind: the page render and the Perlector's own prompt for a
page with no witness testimony and no Surya census, the feed the pipeline itself builds
for such a page (`common.page_feed.assemble_page_feed(..., no_testimony=True,
surya=SURYA_ABSENT)`), rendered by `common.page_prompt.build_page_prompt`. So the prompt
is the serving prompt byte for byte for that case, and it shows no id: every target
entry's `cites` is `[]` and nothing is set aside.

The answer is the `training-reference.v2` record as a page answer, built by the
exporter's own `reference_from_json` and `build_answer` and checked with
`common.page_answer.parse_page_answer`. An entry kind that is not an entry kind becomes
`other`; a reference's own cites are dropped (no id is shown). Loss weights are the
exporter's: `loss_spans` per word status, plus one `weight` per example from the
reference's `provenance.best_status` (`--weights-by-status`; disputed pages left out
by default).

Pages on the held-out list, and the other half of a held-out original, are never
exported. The validation split is by volume, never by page. Selection and split are a
function of the inputs and `--seed` only.
"""

from __future__ import annotations

import argparse
import glob
import hashlib
import json
import statistics
import sys
import tomllib
from collections import Counter, defaultdict
from concurrent.futures import ProcessPoolExecutor
from concurrent.futures.process import BrokenProcessPool
from dataclasses import dataclass, field
from io import BytesIO
from pathlib import Path
from typing import Any

from common import page_feed as PF
from common import page_path, page_prompt
from common import request_capacity as RC
from common.imaging import dimensions
from common.page_answer import parse_page_answer
from common.page_render import LEGIBLE_INK, render_pixels, render_size
from common.page_types import ENTRY_KINDS, OTHER
from common.witness_regime import NAMED, REGIMES
from operations.bakeoff import witness_run as W
from operations.training import export as E

SCHEMA = "perlector-bulk-example.v1"
SCENARIO = "bulk-blind"
REPOSITORY = E.REPOSITORY
PROTOCOL = REPOSITORY / "config" / "perlector_protocol.toml"
RECIPES = REPOSITORY / "config" / "serving_recipes_real.toml"
SERVING_RECIPE = "unproven-real-perlector"
# Per page status (`provenance.best_status` of a corpus reference): the example's weight,
# or None to leave the page out. `unknown` is a reference that names no status;
# `checked` a page the lead has checked (status_label "lead-checked").
DEFAULT_WEIGHTS: dict[str, float | None] = {
    "checked": 1.0,
    "agreed": 0.7,
    "close": 0.5,
    "majority": 0.5,
    "single": 0.3,
    "unknown": 0.3,
    "disputed": None,
}
LABEL_LIMIT = 80  # the grammar's longest label
CHARS_PER_TOKEN = 4  # the answer token estimate: characters / 4, an estimate only

# --- inputs ----------------------------------------------------------------------------


def parse_weights(text: str | None) -> dict[str, float | None]:
    """`status=weight,...` over the defaults; a weight of `exclude` (or `x`) leaves those
    pages out."""
    weights = dict(DEFAULT_WEIGHTS)
    for item in (text or "").split(","):
        if not item.strip():
            continue
        name, _, value = item.strip().partition("=")
        if not name or not value:
            raise SystemExit(f"--weights-by-status item {item!r} is not status=weight")
        if name not in DEFAULT_WEIGHTS:
            # A typo would otherwise set a weight no page ever takes.
            raise SystemExit(
                f"--weights-by-status names unknown status {name!r}; one of {sorted(DEFAULT_WEIGHTS)}"
            )
        if value in ("exclude", "x"):
            weights[name] = None
            continue
        try:
            weights[name] = float(value)
        except ValueError:
            raise SystemExit(f"--weights-by-status {name}={value!r} is not a number") from None
        if not 0 < weights[name] <= 1:
            raise SystemExit(f"--weights-by-status {name}={value} is not in (0, 1]")
    return weights


def reference_paths(patterns: list[str]) -> list[Path]:
    """Every `*.json` the `--references` arguments name: a folder (its JSON files), a
    file, or a glob of either."""
    paths: set[Path] = set()
    for pattern in patterns:
        expanded = str(Path(pattern).expanduser())
        matches = sorted(glob.glob(expanded)) if glob.has_magic(expanded) else [expanded]
        if not matches:
            raise SystemExit(f"--references {pattern!r} matches nothing")
        for match in map(Path, matches):
            if match.is_dir():
                paths.update(match.glob("*.json"))
            elif match.is_file():
                paths.add(match)
            else:
                raise SystemExit(f"--references {match} is neither a folder nor a file")
    return sorted(paths)


def volume_of_path(path: Path) -> str:
    """A reference's volume when the image map names none: the folder above its
    `reference/` folder (the corpus layout `<volume>/reference/<stem>.json`), else its
    own folder."""
    parent = path.parent
    return parent.parent.name if parent.name == "reference" else parent.name


# The image map's fields, with the corpus catalogue's column names accepted for each.
MAP_FIELDS = {
    "stem": ("stem", "page_id"),
    "image": ("image", "page_image"),
    "sha256": ("sha256", "page_sha256"),
    "volume": ("volume",),
    "collection": ("collection",),
}


def read_images_map(path: Path) -> dict[str, dict[str, Any]]:
    """stem -> {image, sha256, volume?, collection?} from a JSONL file (one object per
    line) or a CSV with a header (the corpus `catalog.csv` works as it is). A relative
    image path is read from the map's own folder."""
    import csv

    if path.suffix.lower() == ".csv":
        with path.open(encoding="utf-8", newline="") as handle:
            rows = list(csv.DictReader(handle))
    else:
        rows = [
            json.loads(line)
            for line in path.read_text("utf-8").splitlines()
            if line.strip() and not line.lstrip().startswith("#")
        ]
    out: dict[str, dict[str, Any]] = {}
    for n, row in enumerate(rows, start=1):
        item = {
            key: next((row[name] for name in names if row.get(name)), None)
            for key, names in MAP_FIELDS.items()
        }
        if not item["stem"] or not item["image"]:
            continue  # a catalogue row with no page image is no training page
        if not item["sha256"]:
            raise SystemExit(f"{path} row {n} ({item['stem']}) names no sha256 for its image")
        image = Path(item["image"]).expanduser()
        item["image"] = str(image if image.is_absolute() else path.parent / image)
        if item["stem"] in out and out[item["stem"]] != item:
            raise SystemExit(f"{path}: page {item['stem']!r} is mapped twice, differently")
        out[item["stem"]] = item
    return out


def protocol_settings(long_edge: int | None) -> tuple[dict[str, Any], int]:
    """The sealed `[feed]` switches and the render's long edge from the Perlector
    protocol. A feed this exporter cannot build as the pipeline would is refused: the
    page image must be the legible render and no overlay drawn."""
    with PROTOCOL.open("rb") as handle:
        protocol = tomllib.load(handle)
    switches = PF.validate_feed_table(protocol[PF.FEED_TABLE])
    if switches["page_image"] != "legible" or switches["page_overlay"] != "off":
        raise SystemExit(
            f"{PROTOCOL} shows page_image={switches['page_image']!r}, "
            f"page_overlay={switches['page_overlay']!r}; the bulk export builds only the "
            "legible render with no overlay"
        )
    return switches, long_edge or protocol["page_context"]["maximum_edge"]


def perlector_geometry() -> RC.RowImageGeometry:
    """The Perlector serving row's image geometry, for the image token estimate."""
    with RECIPES.open("rb") as handle:
        profiles = tomllib.load(handle)["profiles"]
    row = next(p for p in profiles if p.get("recipe") == SERVING_RECIPE and p.get("kind") == "vllm")
    return RC.RowImageGeometry(
        row["min_pixels"], row["max_pixels"], row["patch_size"], row["merge_size"]
    )


def image_tokens(size: tuple[int, int], geometry: RC.RowImageGeometry) -> int:
    width, height = size
    factor = geometry.patch_size * geometry.merge_size
    h, w = RC.smart_resize(
        height,
        width,
        factor=factor,
        min_pixels=geometry.min_pixels,
        max_pixels=geometry.max_pixels,
    )
    return (h // factor) * (w // factor)


# --- the blind feed and prompt ---------------------------------------------------------


def blind_feed(
    stem: str,
    render: dict[str, Any],
    source_size: tuple[int, int],
    switches: dict[str, Any],
    *,
    regime: str = NAMED,
    ordinal: int = 1,
) -> dict[str, Any]:
    """The feed the pipeline builds for a page with no page Testimonium in a run with no
    Surya census: the page render only."""
    return PF.assemble_page_feed(
        page_id=stem,
        page_ordinal=ordinal,
        page_size=source_size,
        feed_switches=switches,
        witness_regime=regime,
        roster=[],
        witnesses=[],
        surya=PF.SURYA_ABSENT,
        page_render=render,
        serving_recipe=SERVING_RECIPE,
        no_testimony=True,
    )


def render_record(stem: str, item: dict[str, Any], transform: dict, sha: str | None) -> dict:
    """The page render as a feed records it (`page_render._published_render`'s shape)."""
    return {
        "source_page_id": stem,
        "source_page_ordinal": 1,
        "source": {"relative_path": Path(item["image"]).name, "sha256": item["sha256"]},
        "image_path": f"images/{stem}.png",
        "image_sha256": sha,
        "transform": transform,
        "reason": LEGIBLE_INK,
    }


# --- the target ------------------------------------------------------------------------


@dataclass
class Candidate:
    stem: str
    path: Path
    record: dict[str, Any]
    status: str
    weight: float
    volume: str = ""
    collection: str | None = None
    item: dict[str, Any] = field(default_factory=dict)
    answer_json: str = ""
    spans: list = field(default_factory=list)
    reference_record_sha256: str = ""
    page_type: str = ""
    writing: str = ""
    label: str | None = None
    notes: Counter = field(default_factory=Counter)


def shape_problem(record: dict[str, Any]) -> str | None:
    """Why a readable record cannot be a reference (provenance, entries or words of the wrong
    shape), checked before anything reads them, so one malformed page is refused alone."""
    provenance = record.get("provenance", {})
    if not isinstance(provenance, (dict, type(None))):
        return "provenance is not an object"
    if not isinstance((provenance or {}).get("best_status"), (str, type(None))):
        return "provenance best_status is not a string"
    entries = record.get("entries")
    if not isinstance(entries, list):
        return "entries is not a list"
    for n, e in enumerate(entries, start=1):
        if not isinstance(e, dict):
            return f"entry {n} is not an object"
        if not isinstance(e.get("kind"), str) or not isinstance(e.get("text"), str):
            return f"entry {n} lacks a kind or a text"
        words = e.get("words")
        if words is not None and not (
            isinstance(words, list)
            and all(isinstance(w, dict) and isinstance(w.get("text"), str) for w in words)
        ):
            return f"entry {n} has malformed words"
    return None


def status_of(record: dict[str, Any]) -> str:
    if record.get("status_label") == "lead-checked":
        return "checked"
    return (record.get("provenance") or {}).get("best_status") or "unknown"


def bulk_record(record: dict[str, Any], notes: Counter) -> dict[str, Any]:
    """The record with what a blind page cannot carry made plain: kinds outside the entry
    kinds become `other`, cites are dropped (no id is shown), a blank label is dropped and
    a long one cut to the grammar's limit."""
    out = dict(record)
    entries = []
    for e in record.get("entries") or []:
        e = {k: v for k, v in e.items() if k != "cites"}
        if e.get("kind") not in ENTRY_KINDS:
            e["kind"] = OTHER
            notes["kind_mapped_to_other"] += 1
        label = e.get("label")
        if isinstance(label, str):
            label = label.strip()
            if not label:
                label = None
            elif len(label) > LABEL_LIMIT:
                label = label[:LABEL_LIMIT].rstrip()
                notes["label_shortened"] += 1
        e["label"] = label
        entries.append(e)
    out["entries"] = entries
    return out


EMPTY_FEED = {"witnesses": [], "surya": None}


def build_target(cand: Candidate) -> None:
    """The answer, its loss spans and the reference's digest, or `E.ExampleRefused`."""
    record = bulk_record(cand.record, cand.notes)
    try:
        ref = E.reference_from_json(record)
    except SystemExit as refusal:  # the exporter's refusal of a malformed record
        raise E.ExampleRefused(str(refusal)) from None
    answer = E.build_answer(ref, EMPTY_FEED, [])
    answer_json = json.dumps(answer, ensure_ascii=False)
    state, _, problems = parse_page_answer(answer_json)
    if state != "parsed":
        raise E.ExampleRefused(f"target does not parse: {problems[:3]}")
    cand.answer_json = answer_json
    cand.spans = E.loss_spans(answer_json, answer, ref, 1.0)
    cand.reference_record_sha256 = ref.digest()
    cand.page_type, cand.writing = answer["page_type"], answer["writing"]
    cand.label = cand.record.get("status_label")
    cand.record = {}  # the target is built; the record's words need not stay in memory


# --- selection -------------------------------------------------------------------------


def _order(seed: int, *parts: str) -> str:
    return hashlib.sha256("|".join([str(seed), *parts]).encode()).hexdigest()


def select(
    cands: list[Candidate], seed: int, max_per_volume: int | None, max_pages: int | None
) -> list[Candidate]:
    """At most `max_per_volume` pages of each volume, then at most `max_pages` in all,
    taken a page per volume in turn so no volume dominates; each choice in seeded order."""
    by_volume: dict[str, list[Candidate]] = defaultdict(list)
    for c in cands:
        by_volume[c.volume].append(c)
    queues = []
    for volume in sorted(by_volume, key=lambda v: _order(seed, "volume", v)):
        pages = sorted(by_volume[volume], key=lambda c: _order(seed, "page", c.stem))
        queues.append(pages[:max_per_volume] if max_per_volume else pages)
    chosen: list[Candidate] = []
    depth = 0
    while any(depth < len(q) for q in queues):
        for q in queues:
            if depth < len(q):
                chosen.append(q[depth])
        depth += 1
    return chosen[:max_pages] if max_pages else chosen


def split_by_volume(cands: list[Candidate], seed: int, val_share: float) -> set[str]:
    """The volumes held for validation: whole volumes in seeded order until they hold
    `val_share` of the pages, always leaving at least one volume to train on."""
    sizes = Counter(c.volume for c in cands)
    if val_share <= 0 or len(sizes) < 2:
        return set()
    target = val_share * len(cands)
    val: set[str] = set()
    held = 0
    for volume in sorted(sizes, key=lambda v: _order(seed, "split", v)):
        if held >= target or len(val) == len(sizes) - 1:
            break
        val.add(volume)
        held += sizes[volume]
    return val


# --- rendering -------------------------------------------------------------------------


def _render(job: tuple[str, str, str, int, str | None]) -> dict[str, Any]:
    """One page's render (a worker process): the source checked against its sha256, the
    pipeline's pixels (`render_pixels`) saved as a lossless PNG when `out_png` is given,
    else only the sizes."""
    stem, source, sha, edge, out_png = job
    try:
        data = Path(source).read_bytes()
    except OSError as failure:
        return {"stem": stem, "error": f"image unreadable: {failure}"}
    if hashlib.sha256(data).hexdigest() != sha:
        return {"stem": stem, "error": "image sha256 differs from the map"}
    try:
        if out_png is None:
            size = dimensions(data)
            w, h = render_size(size, edge)
            return {"stem": stem, "source_size": size, "target": (w, h), "transform": None}
        pixels, transform = render_pixels(data, maximum_edge=edge)
        buffer = BytesIO()
        pixels.save(buffer, format="PNG", compress_level=6)
        png = buffer.getvalue()
        Path(out_png).write_bytes(png)
    except Exception as failure:  # noqa: BLE001 -- one bad scan never stops the export
        return {"stem": stem, "error": f"image not rendered: {type(failure).__name__}"}
    source_size = (transform["source_dimensions"]["w"], transform["source_dimensions"]["h"])
    target = (transform["target_dimensions"]["w"], transform["target_dimensions"]["h"])
    return {
        "stem": stem,
        "source_size": source_size,
        "target": target,
        "transform": transform,
        "image_sha256": hashlib.sha256(png).hexdigest(),
    }


def render_all(jobs: list, workers: int) -> dict[str, dict[str, Any]]:
    if workers <= 1:
        return {r["stem"]: r for r in map(_render, jobs)}
    # A worker that dies (out of memory, a crashing decoder) costs its pages, not the
    # export: each page it held is refused, and none is retried here, where the same page
    # could take this process down too.
    out: dict[str, dict[str, Any]] = {}
    with ProcessPoolExecutor(max_workers=workers) as pool:
        stopped = {"error": "render worker stopped before this page"}
        futures = []
        for job in jobs:
            try:
                futures.append((job[0], pool.submit(_render, job)))
            except BrokenProcessPool:  # the pool died: this page was never sent
                out[job[0]] = {"stem": job[0], **stopped}
        for stem, future in futures:
            try:
                out[stem] = future.result()
            except BrokenProcessPool:
                out[stem] = {"stem": stem, **stopped}
    return out


# --- the export ------------------------------------------------------------------------


def export_bulk(
    reference_files: list[Path],
    images: dict[str, dict[str, Any]],
    held: set[str],
    out: Path,
    *,
    seed: int = 0,
    weights: dict[str, float | None] | None = None,
    max_pages: int | None = None,
    max_per_volume: int | None = None,
    val_share: float = 0.05,
    long_edge: int | None = None,
    copy_images: bool = True,
    regime: str = NAMED,
    workers: int = 1,
) -> dict[str, Any]:
    weights = weights or dict(DEFAULT_WEIGHTS)
    E.refuse_out_in_repository(out)
    switches, edge = protocol_settings(long_edge)
    geometry = perlector_geometry()
    counts: Counter = Counter()
    excluded_status: Counter = Counter()
    refused: list[dict[str, str]] = []
    held_excluded: list[str] = []
    cands: list[Candidate] = []
    seen: dict[str, Path] = {}
    for path in reference_files:
        # One unreadable or malformed file is refused by name, never the whole export.
        try:
            record = json.loads(path.read_text("utf-8"))
        except (OSError, UnicodeDecodeError, ValueError) as failure:
            refused.append({"id": str(path), "reason": f"reference unreadable: {failure}"})
            continue
        if not isinstance(record, dict) or record.get("schema") not in E.READABLE_REFERENCE_SCHEMAS:
            counts["not_a_reference"] += 1
            continue
        stem = record.get("stem")
        if not isinstance(stem, str) or not stem.strip():
            refused.append({"id": str(path), "reason": "reference names no stem"})
            continue
        if stem in seen:
            raise SystemExit(f"page {stem!r} has two references: {seen[stem]} and {path}")
        seen[stem] = path
        if E.is_held_out(stem, held):
            held_excluded.append(stem)
            continue
        problem = shape_problem(record)
        if problem:
            refused.append({"id": stem, "reason": f"reference malformed: {problem}"})
            continue
        status = status_of(record)
        weight = weights.get(status, weights.get("unknown"))
        if weight is None:
            excluded_status[status] += 1
            continue
        item = images.get(stem)
        if item is None:
            counts["no_image"] += 1
            continue
        cand = Candidate(stem, path, record, status, weight, item=item)
        cand.volume = item.get("volume") or volume_of_path(path)
        cand.collection = item.get("collection")
        try:
            build_target(cand)
        except E.ExampleRefused as refusal:
            refused.append({"id": stem, "reason": str(refusal)})
            continue
        cands.append(cand)

    chosen = select(cands, seed, max_per_volume, max_pages)
    val_volumes = split_by_volume(chosen, seed, val_share)
    out.mkdir(parents=True, exist_ok=True)
    if copy_images:
        (out / "images").mkdir(exist_ok=True)
    jobs = [
        (
            c.stem,
            c.item["image"],
            c.item["sha256"],
            edge,
            str(out / "images" / f"{c.stem}.png") if copy_images else None,
        )
        for c in chosen
    ]
    renders = render_all(jobs, workers)

    by: dict[str, Counter] = defaultdict(Counter)
    tokens: list[dict[str, int]] = []
    prompts: Counter = Counter()
    written = Counter()
    files = {
        "train": (out / "train.jsonl").open("w", encoding="utf-8"),
        "val": (out / "val.jsonl").open("w", encoding="utf-8"),
    }
    try:
        for c in sorted(chosen, key=lambda c: (c.volume, c.stem)):
            r = renders[c.stem]
            if "error" in r:
                refused.append({"id": c.stem, "reason": r["error"]})
                continue
            transform = r["transform"] or {
                "operation": "downscale-for-page-context",
                "source_dimensions": {"w": r["source_size"][0], "h": r["source_size"][1]},
                "target_dimensions": {"w": r["target"][0], "h": r["target"][1]},
                "maximum_edge": edge,
                "resampler": "to be applied by the trainer (render_pixels)",
            }
            feed = blind_feed(
                c.stem,
                render_record(c.stem, c.item, transform, r.get("image_sha256")),
                tuple(r["source_size"]),
                switches,
                regime=regime,
            )
            prompt = page_prompt.build_page_prompt(SERVING_RECIPE, feed)
            prompt_sha = hashlib.sha256(prompt.encode("utf-8")).hexdigest()
            prompts[prompt_sha] += 1
            if copy_images:
                image_paths, digests = [f"images/{c.stem}.png"], [r["image_sha256"]]
                request = page_path.request_digest(prompt, digests)
            else:
                image_paths, digests, request = [c.item["image"]], [c.item["sha256"]], None
            est = {
                "image_tokens": image_tokens(tuple(r["target"]), geometry),
                "answer_characters": len(c.answer_json),
                "answer_tokens_estimate": round(len(c.answer_json) / CHARS_PER_TOKEN),
            }
            tokens.append(est)
            split = "val" if c.volume in val_volumes else "train"
            example = {
                "schema": SCHEMA,
                "id": c.stem,
                "page": c.stem,
                "volume": c.volume,
                "collection": c.collection,
                "split": split,
                "images": image_paths,
                "image_sha256s": digests,
                "image_rendered": copy_images,
                "image_transform": transform,
                "messages": [
                    {
                        "role": "user",
                        "content": [{"type": "image"}, {"type": "text", "text": prompt}],
                    },
                    {"role": "assistant", "content": c.answer_json},
                ],
                "weight": c.weight,
                "loss_spans": c.spans,
                "scenario": SCENARIO,
                "witness_regime": regime,
                "chat_template_kwargs": {"enable_thinking": False},
                "prompt_sha256": prompt_sha,
                "request_digest": request,
                "reference_status": c.status,
                "reference_label": c.label,
                "reference_record_sha256": c.reference_record_sha256,
                "tokens": est,
            }
            files[split].write(json.dumps(example, ensure_ascii=False) + "\n")
            written[split] += 1
            by["page_type"][c.page_type] += 1
            by["writing"][c.writing] += 1
            by["status"][c.status] += 1
            by["volume"][c.volume] += 1
            by["collection"][c.collection or "-"] += 1
            for note, n in c.notes.items():
                by["notes"][note] += n
    finally:
        for handle in files.values():
            handle.close()

    def total(key: str) -> int:
        return sum(t[key] for t in tokens)

    def summary(key: str) -> dict[str, Any] | None:
        values = [t[key] for t in tokens]
        if not values:
            return None
        return {
            "total": sum(values),
            "median": statistics.median(values),
            "max": max(values),
            "mean": round(statistics.fmean(values)),
        }

    manifest = {
        "schema": SCHEMA,
        "scenario": SCENARIO,
        "seed": seed,
        "serving_recipe": SERVING_RECIPE,
        "witness_regime": regime,
        "long_edge": edge,
        "images_rendered": copy_images,
        "weights_by_status": weights,
        "max_pages": max_pages,
        "max_per_volume": max_per_volume,
        "val_share": val_share,
        "references_read": len(reference_files),
        "candidates": len(cands),
        "chosen": len(chosen),
        "examples": dict(written),
        "val_volumes": sorted(val_volumes),
        "held_out_excluded": sorted(held_excluded),
        "excluded_by_status": dict(excluded_status),
        "skipped": dict(counts),
        "refused": refused,
        "distinct_prompts": len(prompts),
        "by_page_type": dict(by["page_type"]),
        "by_writing": dict(by["writing"]),
        "by_status": dict(by["status"]),
        "by_volume": dict(sorted(by["volume"].items())),
        "by_collection": dict(by["collection"]),
        "target_notes": dict(by["notes"]),
        "tokens": {
            "basis": (
                "AN ESTIMATE: image tokens from the Perlector row's image geometry at the "
                "render's size; answer tokens = answer characters / 4; the prompt (the same "
                "text for every page) once, as the pipeline's sealed upper bound"
            ),
            "image_tokens": summary("image_tokens"),
            "answer_tokens_estimate": summary("answer_tokens_estimate"),
            "prompt_tokens_bound_each": _prompt_bound(switches, regime),
            "total_estimate": (
                total("image_tokens")
                + total("answer_tokens_estimate")
                + len(tokens) * (_prompt_bound(switches, regime) or 0)
            ),
        },
        "written": W.now(),
    }
    W.write_json(out / "manifest.json", manifest)
    return manifest


def _prompt_bound(switches: dict[str, Any], regime: str) -> int | None:
    """The blind prompt's sealed upper bound in tokens (the same for every page)."""
    render = render_record(
        "p",
        {"image": "p.png", "sha256": "0" * 64},
        {
            "operation": "downscale-for-page-context",
            "source_dimensions": {"w": 1000, "h": 1000},
            "target_dimensions": {"w": 1000, "h": 1000},
            "maximum_edge": 1000,
            "resampler": "identity",
        },
        "0" * 64,
    )
    feed = blind_feed("p", render, (1000, 1000), switches, regime=regime)
    prompt = page_prompt.build_page_prompt(SERVING_RECIPE, feed)
    try:
        bound, _ = RC.perlector_page_prompt_bound(
            prompt,
            template_digest=page_prompt.BUILDER_SHA256,
            parts=page_prompt.prompt_parts(SERVING_RECIPE, feed),
        )
    except Exception:  # noqa: BLE001 -- an estimate, never a gate
        return None
    return bound


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument(
        "--references",
        action="append",
        required=True,
        help=f"folder, file or glob of {E.REFERENCE_SCHEMA} records (repeatable)",
    )
    parser.add_argument(
        "--images-map",
        type=Path,
        required=True,
        help="JSONL {stem, image, sha256, volume?, collection?} per page, or the corpus "
        "catalog.csv",
    )
    parser.add_argument(
        "--held-out", type=Path, required=True, help="frozen test stems, never exported"
    )
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--seed", type=int, default=0)
    parser.add_argument("--max-pages", type=int, help="at most this many pages in all")
    parser.add_argument("--max-per-volume", type=int, help="at most this many pages per volume")
    parser.add_argument(
        "--val-share", type=float, default=0.05, help="share of pages, as whole volumes"
    )
    parser.add_argument(
        "--weights-by-status",
        help="status=weight|exclude,... over the defaults "
        + ",".join(f"{k}={'exclude' if v is None else v}" for k, v in DEFAULT_WEIGHTS.items()),
    )
    parser.add_argument(
        "--long-edge", type=int, help="render long edge (default: the protocol's, 2560)"
    )
    parser.add_argument(
        "--no-copy-images",
        action="store_true",
        help="write no renders; list each source image and the transform the trainer applies",
    )
    parser.add_argument("--witness-regime", choices=sorted(REGIMES), default=NAMED)
    parser.add_argument("--workers", type=int, default=1, help="processes rendering images")
    args = parser.parse_args(argv)
    manifest = export_bulk(
        reference_paths(args.references),
        read_images_map(args.images_map),
        E.read_held_out(args.held_out),
        args.out,
        seed=args.seed,
        weights=parse_weights(args.weights_by_status),
        max_pages=args.max_pages,
        max_per_volume=args.max_per_volume,
        val_share=args.val_share,
        long_edge=args.long_edge,
        copy_images=not args.no_copy_images,
        regime=args.witness_regime,
        workers=args.workers,
    )
    shown = {
        k: v for k, v in manifest.items() if k not in ("held_out_excluded", "refused", "by_volume")
    }
    shown["refused"] = len(manifest["refused"])
    shown["volumes"] = len(manifest["by_volume"])
    print(json.dumps(shown, ensure_ascii=False, indent=1, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(main())
