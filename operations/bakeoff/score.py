"""Score cached witness outputs against the lead's per-page transcriptions.

    python -m operations.bakeoff.score --cache CACHE --gold GOLD_DIR --out SCORES_DIR
        [--hard-pages FILE] [--exclude FILE]

Each page belongs to one group of `operations.bakeoff.groups` (by its gold category, or
`test` when its header says `TEST PAGE: yes`), and each model is reported only on the
groups its arm is scored on, by the group's own headline metric. Per model and page:

- CER and WER (`operations.corpus.scoring.score_text`, graphemic-v1: case, accents,
  spelling and punctuation count; whitespace and line breaks do not);
- on pages with gold rows: line recall (each gold row matched one-to-one to a model line
  at CER <= 0.3), false-line rate (model lines matched to no gold row or heading, over
  model lines) and surname recall (the first token of each gold row found among the
  model's tokens at edit distance <= 1, each model token used once);
- false text, only where the gold has no text: more than 20 characters of output;
- for a record arm (`dai`, or units named `record-*` or `whole-page*`): each unit's text
  matched one-to-one to a gold act at CER <= 0.5, best pairs first, giving act recall,
  units unmatched, per-unit CER over matched pairs and whole-page fallbacks, so missed
  records and misread ones are told apart;
- empty output, loop (one line repeated >= 30 times in a row, or `finish_reason`
  length), seconds.

Line matching is greedy, best pairs first, not a Hungarian assignment: scipy is not in
the project environment (only the pod group pulls it in), and the matching rarely
differs on rows this distinct.

`--hard-pages` and `--exclude` take a text file with one page stem per line. Hard pages
are reported in their own table and left out of the group medians; excluded pages are
not scored at all. Writes `scores.jsonl` (one line per model and page) and `scores.md`.

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

from operations.bakeoff import groups as G
from operations.bakeoff.gold import GoldPage, load_gold_dir, reduce_marks

LOOP_RUN = 30
LINE_MATCH_CER = 0.3
SURNAME_DISTANCE = 1
UNIT_MATCH_CER = 0.5
FALSE_TEXT_CHARS = 20
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


def tokens(text: str) -> tuple[str, ...]:
    """Whitespace tokens after graphemic-v1 normalisation, as the WER counts them."""
    from operations.corpus.normalization import GRAPHEMIC_V1, MeasurementRefusal, word_units

    try:
        return word_units(text, GRAPHEMIC_V1)
    except MeasurementRefusal:
        return tuple(unicodedata.normalize("NFC", text).split())


def match_lines(gold: list[str], lines: list[str]) -> list[tuple[int, int]]:
    """Pairs (gold index, line index), one-to-one at CER <= 0.3, best pairs first."""
    pairs = []
    for i, g in enumerate(gold):
        cutoff = int(LINE_MATCH_CER * len(g))
        for j, m in enumerate(lines):
            distance = Levenshtein.distance(g, m, score_cutoff=cutoff)
            if distance <= cutoff:
                pairs.append((distance / len(g), i, j))
    used_g, used_m, matched = set(), set(), []
    for _, i, j in sorted(pairs):
        if i not in used_g and j not in used_m:
            used_g.add(i)
            used_m.add(j)
            matched.append((i, j))
    return matched


def line_keys(lines: list[str]) -> list[str]:
    return [k for line in lines if (k := _line_key(line))]


def matched_rows(gold_rows: list[str], model_lines: list[str]) -> set[int]:
    """Indices (into the non-empty gold rows) of the rows some model line matches."""
    return {i for i, _ in match_lines(line_keys(gold_rows), line_keys(model_lines))}


def line_recall(
    gold_rows: list[str], model_lines: list[str], headings: list[str] = ()
) -> dict[str, Any]:
    """Line recall on the gold rows, and the share of model lines that match no gold line.

    Headings take part in the matching so a transcribed heading is not a false line, but
    only rows count towards recall.
    """
    rows, heads, lines = line_keys(gold_rows), line_keys(list(headings)), line_keys(model_lines)
    if not rows:
        return {"line_recall": None, "rows": 0, "rows_matched": 0}
    pairs = match_lines(rows + heads, lines)
    rows_matched = sum(i < len(rows) for i, _ in pairs)
    return {
        "line_recall": rows_matched / len(rows),
        "rows": len(rows),
        "rows_matched": rows_matched,
        "model_lines": len(lines),
        "false_line_rate": (len(lines) - len(pairs)) / len(lines) if lines else None,
    }


def surname_recall(gold_rows: list[str], model_text: str) -> float | None:
    """Share of gold rows whose first token appears among the model's tokens (distance <= 1)."""
    surnames = [k.split()[0] for k in line_keys(gold_rows)]
    if not surnames:
        return None
    pool: list[str] = _line_key(model_text.replace("\n", " ")).split()
    unmatched = []
    for name in surnames:
        if name in pool:
            pool.remove(name)
        else:
            unmatched.append(name)
    found = len(surnames) - len(unmatched)
    for name in unmatched:
        near = next(
            (
                t
                for t in pool
                if Levenshtein.distance(name, t, score_cutoff=SURNAME_DISTANCE) <= SURNAME_DISTANCE
            ),
            None,
        )
        if near is not None:
            pool.remove(near)
            found += 1
    return found / len(surnames)


def expected_behaviour(gold: GoldPage) -> str | None:
    """The expected behaviour of a test page, or None for an ordinary page."""
    value = gold.header.get("TEST PAGE", "").strip()
    if not value.lower().startswith("yes"):
        return None
    return value[3:].lstrip(" :").strip() or "(no behaviour given)"


def page_group(gold: GoldPage) -> str:
    """`test`, the category's group, or `unassigned` for a category not in the table."""
    if expected_behaviour(gold) is not None:
        return G.TEST
    return G.group_of_category(gold.category) or "unassigned"


def act_texts(gold: GoldPage) -> list[str]:
    """Each gold act's text, marks reduced, as `GoldPage.act_text` gives them together."""
    texts = []
    for act in gold.acts:
        lines = [line for line in reduce_marks("\n".join(act.lines)).split("\n") if line]
        if lines:
            texts.append("\n".join(lines))
    return texts


def _unit_name(unit: dict[str, Any]) -> str:
    return str(unit.get("unit") or (unit.get("request") or {}).get("unit") or "")


def is_record_reading(record: dict[str, Any]) -> bool:
    """Whether a page was read record by record (DAI, or units named for records)."""
    if record.get("arm") == "dai":
        return True
    names = [_unit_name(u) for u in record.get("units") or [] if isinstance(u, dict)]
    return any(n.startswith(("record-", "whole-page")) for n in names)


def record_scores(record: dict[str, Any], gold: GoldPage) -> dict[str, Any]:
    """Units matched one-to-one to gold acts at CER <= 0.5, best pairs first."""
    arm = record.get("arm", "")
    units = [
        text
        for u in record.get("units") or []
        if isinstance(u, dict) and (text := normalise_output(arm, u.get("text"))).strip()
    ]
    acts = act_texts(gold)
    pairs = []
    for i, act in enumerate(acts):
        for j, unit in enumerate(units):
            cer = cer_wer(act, unit)["cer"]
            if cer is not None and cer <= UNIT_MATCH_CER:
                pairs.append((cer, i, j))
    used_a, used_u, cers = set(), set(), []
    for cer, i, j in sorted(pairs):
        if i not in used_a and j not in used_u:
            used_a.add(i)
            used_u.add(j)
            cers.append(cer)
    return {
        "acts": len(acts),
        "acts_matched": len(used_a),
        "act_recall": len(used_a) / len(acts) if acts else None,
        "units": len(units),
        "units_unmatched": len(units) - len(used_u),
        "unit_cers": cers,
        "whole_page_fallback": bool(record.get("whole_page_fallback")),
    }


def score_page(record: dict[str, Any], gold: GoldPage, hard: bool = False) -> dict[str, Any]:
    arm = record.get("arm", "")
    text = normalise_output(arm, record.get("text"))
    reference = gold.reference_text()
    loop = record.get("loop")
    if loop is None:
        loop = loop_flag(record.get("text") or "", record.get("finish_reason"))[0]
    group = page_group(gold)
    row = {
        "model": record["model"],
        "arm": arm,
        "page": gold.stem,
        "category": gold.category,
        "form": G.form_of(gold.header.get("FORM", "")),
        "group": group,
        "scored": group in G.groups_for(arm)[0],
        "hard": hard,
        "status": gold.status,
        "ref_chars": len(reference),
        "hyp_chars": len(text),
        "false_text": len(text) > FALSE_TEXT_CHARS if not reference.strip() else None,
        "empty": not text.strip(),
        "loop": bool(loop),
        "finish_reason": record.get("finish_reason"),
        "seconds": record.get("seconds"),
        "error": record.get("error"),
    }
    row.update(cer_wer(reference, text))
    if gold.rows:
        row.update(line_recall(gold.row_lines(), text.splitlines(), gold.heading_lines()))
        row["surname_recall"] = surname_recall(gold.row_lines(), text)
    if is_record_reading(record) and gold.acts:
        row["record"] = record_scores(record, gold)
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


_IMAGE_SUFFIXES = (".tif", ".tiff", ".png", ".jpg", ".jpeg", ".txt")


def read_stems(path: Path | None) -> set[str]:
    """Page stems from a text file, one per line; blank lines and `#` lines skipped."""
    if path is None:
        return set()
    stems = set()
    for raw in path.read_text("utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        for suffix in _IMAGE_SUFFIXES:
            if line.lower().endswith(suffix):
                line = line[: -len(suffix)]
                break
        stems.add(line)
    return stems


def model_arm(pages: dict[str, dict]) -> str:
    return next((r.get("arm") or "" for r in pages.values()), "")


def summarise(rows: list[dict], metric: str) -> float | None:
    """One group's value of a metric: the median per page, or for false text the share."""
    if metric == "false_text_rate":
        return _mean([float(r["false_text"]) for r in rows if r.get("false_text") is not None])
    return _median([r[metric] for r in rows if r.get(metric) is not None])


def _fmt(value: float | None, digits: int = 3) -> str:
    return "–" if value is None else f"{value:.{digits}f}"


def _median(values: list[float]) -> float | None:
    return statistics.median(values) if values else None


def _mean(values: list[float]) -> float | None:
    return statistics.fmean(values) if values else None


def _metric(rows: list[dict], metric: str) -> str:
    name = "false text" if metric == "false_text_rate" else G.METRICS[metric][0]
    return f"{name} {_fmt(summarise(rows, metric))}"


def _mean_of(rows: list[dict], metric: str) -> str:
    if metric == "false_text_rate":
        return "–"
    return _fmt(_mean([r[metric] for r in rows if r.get(metric) is not None]))


def _counts(rows: list[dict]) -> str:
    secs = [r["seconds"] for r in rows if r.get("seconds") is not None]
    return (
        f"{sum(r['empty'] for r in rows)} | {sum(r['loop'] for r in rows)} | "
        f"{sum(bool(r['error']) for r in rows)} | {_fmt(_median(secs), 1)}"
    )


def _record_cells(rows: list[dict]) -> str:
    """Act recall, units unmatched, median unit CER and whole-page fallbacks, as cells."""
    records = [r["record"] for r in rows if r.get("record")]
    acts = sum(r["acts"] for r in records)
    recall = sum(r["acts_matched"] for r in records) / acts if acts else None
    cers = [c for r in records for c in r["unit_cers"]]
    return (
        f"{_fmt(recall)} | {sum(r['units_unmatched'] for r in records)} | "
        f"{_fmt(_median(cers))} | {sum(r['whole_page_fallback'] for r in records)}"
    )


def _direction(metric: str) -> str:
    return "higher is better" if G.METRICS[metric][1] else "lower is better"


def page_form(gold: GoldPage) -> str:
    return G.form_of(gold.header.get("FORM", ""))


class _Scores:
    """The scored rows and the gold, sliced the ways the report needs."""

    def __init__(self, rows, gold, missing, arms, hard):
        self.rows, self.gold, self.missing, self.arms, self.hard = rows, gold, missing, arms, hard
        self.models = sorted({r["model"] for r in rows} | set(missing))

    def _in(self, stem: str, group: str, forms) -> bool:
        page = self.gold[stem]
        return page_group(page) == group and (forms is None or page_form(page) in forms)

    def got(self, model: str, group: str, hard: bool = False, forms=None) -> list[dict]:
        return [
            r
            for r in self.rows
            if r["model"] == model and r["hard"] == hard and self._in(r["page"], group, forms)
        ]

    def gone(self, model: str, group: str, forms=None) -> list[str]:
        return [
            p
            for p in self.missing.get(model, [])
            if self._in(p, group, forms) and p not in self.hard
        ]

    def hard_in(self, group: str, forms=None) -> int:
        return sum(self._in(p, group, forms) for p in self.hard if p in self.gold)

    def forms_in(self, group: str) -> list[str]:
        present = {page_form(p) for p in self.gold.values() if page_group(p) == group}
        return [f for f in G.FORMS if f in present] + sorted(present - set(G.FORMS))

    def groups_for(self, model: str) -> tuple[tuple[str, ...], str, bool]:
        return G.groups_for(self.arms.get(model, ""))

    def is_record_arm(self, model: str) -> bool:
        return any(r.get("record") for r in self.rows if r["model"] == model)


def _cross_table(s: _Scores, g: G.Group, forms, title: str) -> list[str]:
    name = G.METRICS[g.headline][0]
    lines = [
        f"### {g.name}, {title} ({name}, {_direction(g.headline)})",
        "",
        f"| model | pages | {name} |",
        "|---|---:|---:|",
    ]
    skipped = []
    for model in s.models:
        names, reason, _ = s.groups_for(model)
        if g.name not in names:
            skipped.append(f"{model} ({reason})")
            continue
        got = s.got(model, g.name, forms=forms)
        lines.append(f"| {model} | {len(got)} | {_fmt(summarise(got, g.headline))} |")
    lines.append("")
    if skipped:
        lines += [f"Not scored here: {', '.join(skipped)}.", ""]
    return lines


def _cross_model(s: _Scores, label: str) -> list[str]:
    lines = [f"## Compare models, one group at a time{label}", ""]
    for g in G.GROUPS:
        forms = s.forms_in(g.name)
        if g.name == G.TEST or not forms:
            continue
        if "handwritten" in forms:
            lines += _cross_table(s, g, {"handwritten"}, "handwritten pages")
        else:
            lines += _cross_table(s, g, None, "all pages (none handwritten)")
        if "typed" in forms and "handwritten" in forms:
            lines += _cross_table(s, g, {"typed"}, "typed pages")
    return lines


def _per_model(s: _Scores, model: str, label: str) -> list[str]:
    names, reason, known = s.groups_for(model)
    arm = s.arms.get(model) or "?"
    record = s.is_record_arm(model)
    lines = [f"## {model} (arm {arm}){label}", "", f"Scored on: {', '.join(names)} ({reason})."]
    if not known:
        lines.append(f"**Warning:** arm {arm!r} is not in the fairness table.")
    head = "| group | pages | hard | missing | median | mean | also | empty | loops | errors | s/page |"
    rule = "|---|---:|---:|---:|---|---:|---|---:|---:|---:|---:|"
    if record:
        head += " act recall | units unmatched | unit CER | whole-page fallbacks |"
        rule += "---:|---:|---:|---:|"
    lines += ["", head, rule]
    blank = " | | | |" if record else ""
    for g in G.GROUPS:
        forms = s.forms_in(g.name)
        if g.name not in names or g.name == G.TEST or not forms:
            continue
        slices = [(g.name, None)]
        if len(forms) > 1:
            slices += [(f"↳ {form}", {form}) for form in forms]
        for title, only in slices:
            got = s.got(model, g.name, forms=only)
            also = "; ".join(_metric(got, m) for m in g.also) or "–"
            extra = blank
            if record and only is None and g.name == "acts":
                extra = f" {_record_cells(got)} |"
            lines.append(
                f"| {title} | {len(got)} | {s.hard_in(g.name, only)} | "
                f"{len(s.gone(model, g.name, only))} | {_metric(got, g.headline)} | "
                f"{_mean_of(got, g.headline)} | {also} | {_counts(got)} |{extra}"
            )
    every = [r for r in s.rows if r["model"] == model and r["group"] != G.TEST]
    gone = [p for p in s.missing.get(model, []) if page_group(s.gold[p]) != G.TEST]
    lines += [
        f"| all pages, for reference | {len(every)} | {sum(r['hard'] for r in every)} | "
        f"{len(gone)} | {_metric(every, 'cer')} | – | – | {_counts(every)} |{blank}",
        "",
    ]
    return lines


def _test_pages(s: _Scores, label: str) -> list[str]:
    tests = [p for p in sorted(s.gold) if page_group(s.gold[p]) == G.TEST]
    if not tests:
        return []
    lines = [f"## Test pages{label}", "", "Each one for the lead to judge; never pooled.", ""]
    for stem in tests:
        lines += [
            f"### {stem} ({s.gold[stem].category})",
            "",
            f"Expected: {expected_behaviour(s.gold[stem])}",
            "",
            "| model | chars | empty | CER |",
            "|---|---:|---|---:|",
        ]
        for model in s.models:
            row = next((r for r in s.rows if r["model"] == model and r["page"] == stem), None)
            if row is None:
                lines.append(f"| {model} | missing | | |")
            else:
                empty = "yes" if row["empty"] else "no"
                lines.append(f"| {model} | {row['hyp_chars']} | {empty} | {_fmt(row['cer'])} |")
        lines.append("")
    return lines


def _page_headline(row: dict, group: str) -> str:
    metric = G.group(group).headline if group in {g.name for g in G.GROUPS} else "cer"
    if metric == "false_text_rate":
        if row.get("false_text") is None:
            return f"gold has text, CER {_fmt(row.get('cer'))}"
        return "false text" if row["false_text"] else "no false text"
    return f"{G.METRICS[metric][0]} {_fmt(row.get(metric))}"


def _hard_pages(s: _Scores, label: str) -> list[str]:
    hard = [r for r in s.rows if r["hard"] and r["group"] != G.TEST]
    if not hard:
        return []
    lines = [
        f"## Hard pages{label}",
        "",
        "Left out of the group medians above.",
        "",
        "| page | group | model | headline | CER |",
        "|---|---|---|---|---:|",
    ]
    for r in sorted(hard, key=lambda r: (r["page"], r["model"])):
        if r["scored"]:
            lines.append(
                f"| {r['page']} | {r['group']} | {r['model']} | "
                f"{_page_headline(r, r['group'])} | {_fmt(r['cer'])} |"
            )
    return lines + [""]


def report(
    rows: list[dict],
    gold: dict[str, GoldPage],
    cache: Path,
    missing: dict,
    arms: dict[str, str] | None = None,
    hard: set[str] = frozenset(),
    notes: list[str] = (),
) -> str:
    s = _Scores(rows, gold, missing, arms or {}, set(hard))
    fools = any("fool" in g.status.lower() for g in gold.values())
    label = f" {FOOLS_GOLD_LABEL}" if fools else ""
    lines = [f"# Witness bake-off scores{label}", ""]
    if fools:
        lines += [
            f"Every number on this page is {FOOLS_GOLD_LABEL}: the gold is an unchecked AI draft.",
            "",
        ]
    lines += [
        "Each model is scored only on the page groups its design applies to, by the group's "
        "headline metric (median per page; false text is the share of pages with more than "
        f"{FALSE_TEXT_CHARS} characters beyond the reference). CER/WER graphemic-v1. Line "
        "recall: gold rows matched to model lines at CER <= 0.3. s/page: median request "
        "latency (includes queueing under concurrency); throughput is under Runs.",
        "",
    ]
    unknown = sorted({m for m in s.models if not s.groups_for(m)[2]})
    if unknown:
        lines.append(f"**Warning:** not in the fairness table, scored on every group: {unknown}.")
    unassigned = sorted({g.category for g in gold.values() if page_group(g) == "unassigned"})
    if unassigned:
        lines.append(f"**Warning:** categories in no group (only in 'all pages'): {unassigned}.")
    lines += [*notes, ""]
    lines += _cross_model(s, label)
    for model in s.models:
        lines += _per_model(s, model, label)
    lines += _test_pages(s, label)
    lines += _hard_pages(s, label)
    lines += [f"## Worst pages by CER, on the groups each model is scored on{label}", ""]
    for model in s.models:
        worst = sorted(
            (
                r
                for r in rows
                if r["model"] == model and r["scored"] and r["group"] != G.TEST
                if r["cer"] is not None
            ),
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
    for model in s.models:
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


def add_page_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--cache", type=Path, required=True)
    parser.add_argument("--gold", type=Path, required=True)
    parser.add_argument("--gold-glob", default="**/*.txt")
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--models", help="comma-separated cache folders (default: all)")
    parser.add_argument("--hard-pages", type=Path, help="file of page stems, one per line")
    parser.add_argument("--exclude", type=Path, help="file of page stems not to score")


def load_inputs(args: argparse.Namespace):
    """(gold without excluded pages, cache, hard stems, notes for the report)."""
    gold = load_gold_dir(args.gold, args.gold_glob)
    hard, exclude = read_stems(args.hard_pages), read_stems(args.exclude)
    notes = []
    for name, stems in (("hard-pages", hard), ("exclude", exclude)):
        unknown = sorted(stems - set(gold))
        if unknown:
            notes.append(f"**Warning:** --{name} names stems with no gold file: {unknown}.")
    if exclude & set(gold):
        notes.append(f"Excluded from scoring: {len(exclude & set(gold))} pages.")
    gold = {stem: page for stem, page in gold.items() if stem not in exclude}
    cache = load_cache(args.cache, args.models.split(",") if args.models else None)
    return gold, cache, hard & set(gold), notes


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    add_page_arguments(parser)
    args = parser.parse_args(argv)
    gold, cache, hard, notes = load_inputs(args)
    rows, missing = [], {}
    for model, pages in cache.items():
        missing[model] = sorted(stem for stem in gold if stem not in pages)
        rows += [
            score_page(pages[stem], gold[stem], stem in hard)
            for stem in sorted(gold)
            if stem in pages
        ]
    arms = {model: model_arm(pages) for model, pages in cache.items()}
    args.out.mkdir(parents=True, exist_ok=True)
    with open(args.out / "scores.jsonl", "w", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")
    text = report(rows, gold, args.cache, missing, arms, hard, notes)
    (args.out / "scores.md").write_text(text, "utf-8")
    print(f"scored {len(rows)} page readings of {len(cache)} models -> {args.out}/scores.md")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
