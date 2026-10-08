"""Score cached witness outputs against the lead's per-page transcriptions.

    python -m operations.bakeoff.score --cache CACHE --gold GOLD_DIR --out SCORES_DIR

Per model and page: CER and WER (`operations.corpus.scoring.score_text`, graphemic-v1:
case, accents, spelling and punctuation count; whitespace and line breaks do not), line
recall on index and list pages (each gold row matched one-to-one to a model line at
CER <= 0.3, greedy best-first), empty output, loop (one line repeated >= 30 times in a
row, or `finish_reason` length), seconds. Writes `scores.jsonl` and `scores.md`.

While the gold files say STATUS "fool's gold" (an unchecked AI draft), every number is
labelled "vs fool's gold (ballpark, not accuracy)".
"""

from __future__ import annotations

import argparse
import html
import json
import re
import statistics
import unicodedata
from pathlib import Path
from typing import Any

from rapidfuzz.distance import Levenshtein

from operations.bakeoff.gold import GoldPage, load_gold_dir, reduce_marks

LOOP_RUN = 30
LINE_MATCH_CER = 0.3
LINE_RECALL_CATEGORIES = frozenset({"index", "list"})
FOOLS_GOLD_LABEL = "vs fool's gold (ballpark, not accuracy)"
_THINK = re.compile(r"<think>.*?(</think>|\Z)", re.DOTALL)
_TABLE_RULE = re.compile(r"\|?(\s*:?-{3,}:?\s*\|)+(\s*:?-{3,}:?\s*)?\|?")
_TAG = re.compile(r"<[^>]+>")
_BREAKING_TAG = re.compile(r"<\s*/?\s*(br|p|div|tr|li|h[1-6]|table|ul|ol|pre)\b[^>]*>", re.I)


def _line_key(line: str) -> str:
    return " ".join(unicodedata.normalize("NFC", line).split()).casefold()


def loop_flag(text: str, finish_reason: str | None) -> tuple[bool, str | None]:
    """A loop: the same normalised non-empty line >= 30 times in a row, or a length stop."""
    run, previous = 0, None
    for line in text.splitlines():
        key = _line_key(line)
        if not key:
            continue
        run = run + 1 if key == previous else 1
        previous = key
        if run >= LOOP_RUN:
            return True, f"line repeated {LOOP_RUN}+ times"
    if finish_reason == "length":
        return True, "finish_reason length"
    return False, None


def _strip_html(text: str) -> str:
    text = _BREAKING_TAG.sub("\n", text)
    text = re.sub(r"<\s*/?\s*t[dh]\b[^>]*>", " ", text, flags=re.I)
    return html.unescape(_TAG.sub("", text))


def _markdown_to_lines(text: str) -> str:
    out = []
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("```") or _TABLE_RULE.fullmatch(stripped):
            continue  # code fences and table separator rows
        if stripped.startswith("|"):
            stripped = " ".join(cell.strip() for cell in stripped.strip("|").split("|"))
        stripped = re.sub(r"^#+\s*", "", stripped).replace("**", "")
        out.append(stripped)
    return "\n".join(out)


def normalise_output(arm: str, text: str | None) -> str:
    """One model answer as plain text, one line per line the model gave."""
    text = _THINK.sub("", text or "")
    if arm == "chandra":
        from common import chandra_layout

        parsed = chandra_layout.parse_layout_html(text.encode("utf-8"))
        text = _strip_html(text) if chandra_layout.is_refusal(parsed) else parsed["page_text"]
    elif arm == "churro":
        from common import churro_document

        prompt = churro_document.churro_system_prompt("registry-v0.3.0")
        try:
            parsed = churro_document.parse_churro_document(
                text.encode("utf-8"), system_prompt=prompt, max_bytes=50_000_000
            )
            text = parsed["text"] if parsed.get("state") == "parsed" else _strip_html(text)
        except Exception:  # noqa: BLE001 -- a bench: any unreadable XML falls back to tags stripped
            text = _strip_html(text)
    elif arm == "dai":
        text = text.replace("[UNCERTAIN]", "").replace("[CROSSED_OUT]", "")
    else:
        text = reduce_marks(text)
    lines = (" ".join(line.split()) for line in _markdown_to_lines(text).splitlines())
    return "\n".join(line for line in lines if line)


def cer_wer(reference: str, hypothesis: str) -> dict[str, Any]:
    """CER/WER by the repository's scorer; past its text bound, a plain Levenshtein."""
    from operations.corpus.normalization import GRAPHEMIC_V1, MeasurementRefusal
    from operations.corpus.scoring import score_text

    if not reference.strip():
        return {"cer": None, "wer": None, "cer_basis": "no reference text"}
    try:
        score = score_text(reference, hypothesis, profile=GRAPHEMIC_V1)
        return {"cer": score.cer.rate, "wer": score.wer.rate, "cer_basis": "scoring.score_text"}
    except MeasurementRefusal:
        ref, hyp = (
            " ".join(unicodedata.normalize("NFC", t).split()) for t in (reference, hypothesis)
        )
        return {
            "cer": Levenshtein.distance(ref, hyp) / len(ref),
            "wer": Levenshtein.distance(ref.split(), hyp.split()) / max(1, len(ref.split())),
            "cer_basis": "fallback: output past the scorer's text bound",
        }


def line_recall(gold_rows: list[str], model_lines: list[str]) -> dict[str, Any]:
    """Gold rows matched one-to-one to model lines at CER <= 0.3, best pairs first."""
    gold = [_line_key(r) for r in gold_rows if _line_key(r)]
    lines = [_line_key(m) for m in model_lines if _line_key(m)]
    if not gold:
        return {"line_recall": None, "rows": 0, "rows_matched": 0}
    pairs = []
    for i, g in enumerate(gold):
        cutoff = int(LINE_MATCH_CER * len(g))
        for j, m in enumerate(lines):
            distance = Levenshtein.distance(g, m, score_cutoff=cutoff)
            if distance <= cutoff:
                pairs.append((distance / len(g), i, j))
    used_g, used_m = set(), set()
    for _, i, j in sorted(pairs):
        if i not in used_g and j not in used_m:
            used_g.add(i)
            used_m.add(j)
    return {"line_recall": len(used_g) / len(gold), "rows": len(gold), "rows_matched": len(used_g)}


def score_page(record: dict[str, Any], gold: GoldPage) -> dict[str, Any]:
    text = normalise_output(record.get("arm", ""), record.get("text"))
    reference = gold.reference_text()
    loop = record.get("loop")
    if loop is None:
        loop = loop_flag(record.get("text") or "", record.get("finish_reason"))[0]
    row = {
        "model": record["model"],
        "page": gold.stem,
        "category": gold.category,
        "status": gold.status,
        "ref_chars": len(reference),
        "hyp_chars": len(text),
        "empty": not text.strip(),
        "loop": bool(loop),
        "finish_reason": record.get("finish_reason"),
        "seconds": record.get("seconds"),
        "error": record.get("error"),
    }
    row.update(cer_wer(reference, text))
    if gold.category in LINE_RECALL_CATEGORIES and gold.rows:
        row.update(line_recall(gold.row_lines(), text.splitlines()))
    return row


def load_cache(cache: Path, models: list[str] | None) -> dict[str, dict[str, dict]]:
    out: dict[str, dict[str, dict]] = {}
    for folder in sorted(p for p in cache.iterdir() if p.is_dir()):
        if models and folder.name not in models:
            continue
        pages = {}
        for path in sorted(folder.glob("*.json")):
            if path.name == "run.json":
                continue
            record = json.loads(path.read_text("utf-8"))
            if record.get("schema", "").startswith("bakeoff-witness-page"):
                pages[record["page"]] = record
        if pages:
            out[folder.name] = pages
    return out


def _fmt(value: float | None, digits: int = 3) -> str:
    return "–" if value is None else f"{value:.{digits}f}"


def _median(values: list[float]) -> float | None:
    return statistics.median(values) if values else None


def _mean(values: list[float]) -> float | None:
    return statistics.fmean(values) if values else None


def report(rows: list[dict], gold: dict[str, GoldPage], cache: Path, missing: dict) -> str:
    fools = any("fool" in g.status.lower() for g in gold.values())
    label = f" {FOOLS_GOLD_LABEL}" if fools else ""
    lines = [f"# Witness bake-off scores{label}", ""]
    if fools:
        lines += [
            f"Every number on this page is {FOOLS_GOLD_LABEL}: the gold is an unchecked AI draft.",
            "",
        ]
    lines += [
        "CER/WER graphemic-v1 (case, accents, spelling, punctuation count). Line recall: "
        "gold rows matched to model lines at CER <= 0.3. s/page: median request "
        "latency (includes queueing under concurrency); throughput is under Runs.",
        "",
    ]
    models = sorted({r["model"] for r in rows} | set(missing))
    for category in sorted({g.category for g in gold.values()}):
        lines += [
            f"## {category}{label}",
            "",
            "| model | pages | missing | median CER | mean CER | median WER | line recall "
            "| empty | loops | errors | s/page |",
            "|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|",
        ]
        for model in models:
            got = [r for r in rows if r["model"] == model and r["category"] == category]
            gone = [p for p in missing.get(model, []) if gold[p].category == category]
            cers = [r["cer"] for r in got if r["cer"] is not None]
            wers = [r["wer"] for r in got if r["wer"] is not None]
            recalls = [r["line_recall"] for r in got if r.get("line_recall") is not None]
            secs = [r["seconds"] for r in got if r.get("seconds") is not None]
            lines.append(
                f"| {model} | {len(got)} | {len(gone)} | {_fmt(_median(cers))} | "
                f"{_fmt(_mean(cers))} | {_fmt(_median(wers))} | {_fmt(_mean(recalls))} | "
                f"{sum(r['empty'] for r in got)} | {sum(r['loop'] for r in got)} | "
                f"{sum(bool(r['error']) for r in got)} | {_fmt(_median(secs), 1)} |"
            )
        lines.append("")
    lines += [f"## Worst pages by CER{label}", ""]
    for model in models:
        worst = sorted(
            (r for r in rows if r["model"] == model and r["cer"] is not None),
            key=lambda r: r["cer"],
            reverse=True,
        )[:5]
        named = ", ".join(f"{r['page']} ({r['cer']:.2f})" for r in worst) or "none"
        lines.append(f"- **{model}**: {named}")
    lines += [
        "",
        "## Runs",
        "",
        "| model | pages | wall s | s/page throughput | concurrency |",
        "|---|---:|---:|---:|---:|",
    ]
    for model in models:
        run = cache / model / "run.json"
        if run.is_file():
            r = json.loads(run.read_text("utf-8"))
            per = r["wall_seconds"] / r["pages"] if r.get("pages") else None
            lines.append(
                f"| {model} | {r['pages']} | {r['wall_seconds']:.0f} | {_fmt(per, 1)} | "
                f"{r.get('concurrency')} |"
            )
    lines += ["", "(The Runs table covers only the last invocation for each model.)", ""]
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--cache", type=Path, required=True)
    parser.add_argument("--gold", type=Path, required=True)
    parser.add_argument("--gold-glob", default="**/*.txt")
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--models", help="comma-separated cache folders (default: all)")
    args = parser.parse_args(argv)
    gold = load_gold_dir(args.gold, args.gold_glob)
    cache = load_cache(args.cache, args.models.split(",") if args.models else None)
    rows, missing = [], {}
    for model, pages in cache.items():
        missing[model] = sorted(stem for stem in gold if stem not in pages)
        rows += [score_page(pages[stem], gold[stem]) for stem in sorted(gold) if stem in pages]
    args.out.mkdir(parents=True, exist_ok=True)
    with open(args.out / "scores.jsonl", "w", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")
    (args.out / "scores.md").write_text(report(rows, gold, args.cache, missing), "utf-8")
    print(f"scored {len(rows)} page readings of {len(cache)} models -> {args.out}/scores.md")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
