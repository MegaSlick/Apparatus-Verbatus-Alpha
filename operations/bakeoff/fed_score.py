"""The Perlector scorecard: reading quality, answer health and scepticism, page group by group.

    python -m operations.bakeoff.fed_score --run-tree RUN [--answers CACHE_DIR] \\
        --gold DIR --gold-glob '*/Prepped/*.txt' [--hard-pages FILE] \\
        [--compare CACHE_DIR_OR_RUN] [--out DIR]

The answers are a run tree's own first Perlector readings (`4_perlector` page-reading
records, the default) or a `fed_arm` cache folder (`<out>/<label>/`). The witnesses each
answer is judged against are the ones its reader was shown: the run's sealed page feeds,
or the variant feed a `fed_arm` record carries. Scores follow the bake-off's tools
(`score.py`, `roster.py`): the reading's text is every entry's text in order, doubt marks
reduced; gold tokens are graphemic-v1 words, aligned to each text by unit-cost edit
script, so each gold word is right or wrong for the reader and for each witness.

Per page group (`score.page_group`; act pages split by FORM) and for the hard pages:

- reading: CER median (parsed pages, and all pages with an unparsed answer read as
  empty), act recall (gold acts matched one-to-one by a `kind: act` entry at CER <= 0.5,
  and pages whose act count is exact), index/table row recall (`score.line_recall`),
  surname recall, invented text (inserted words per gold word; false text on pages with
  no gold text);
- answer health: parsed / malformed and why, errors, finish reasons, loop stops,
  completion tokens;
- scepticism, per witness (named by its bake-off arm, else its label), on the pages whose
  answer parsed:
  - only-X-right, followed: the gold word is right in X alone; share the reader has right;
  - only-X-wrong, resisted: X alone is wrong; share the reader still has right;
  - copy of a wrong X: of the words X got wrong with a word of its own, share where the
    reader wrote X's same wrong word; and the share of the reader's own errors that are
    some witness's error;
  - all witnesses wrong, recovered; all right, damaged (the reader changed a word every
    witness had right);
  - beats the vote: words the reader has right where most witnesses that read the page
    were wrong, minus words it has wrong where most were right;
- with `--compare`: the same scorecard for a second answer set (a swap, a variant, or a
  repeat of the same arm for the noise floor), side by side, and invariance: per page
  the CER between the two readings' texts, pages read identically, and gold words whose
  right/wrong flipped.
- planted errors: a `fed_arm` cache made with `--mutations` carries each page's
  `witness-mutation.v1` sidecar (`operations/bakeoff/mutations.py`); for every planted
  site the reader either resisted (its word is right), copied (it wrote the planted
  word) or went wrong another way, counted per scenario, per number of witnesses that
  carried the error (k = 1, 2, 3), per chair for k = 1, and per word class.

Every heading says "vs fool's gold (ballpark, not accuracy)" while any gold page's
STATUS says fool's gold; only lead-checked gold drops the label.
"""

from __future__ import annotations

import argparse
import json
import statistics
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from rapidfuzz.distance import Levenshtein

from operations.bakeoff import fed_arm as F
from operations.bakeoff import groups as G
from operations.bakeoff import score as S
from operations.bakeoff.gold import load_gold_dir

READER_ARM = "perlector"  # score.normalise_output: marks reduced, no vendor parser


@dataclass
class Witness:
    label: str
    name: str  # what the tables call it: the bake-off arm, else the label
    text: str  # as shown, units joined; "" when it did not read


@dataclass
class Answer:
    stem: str
    parse_state: str | None
    parse_codes: list[str]
    other_codes: list[str]
    answer: dict[str, Any] | None
    finish_reason: str | None
    loop_stop: bool
    completion_tokens: int | None
    seconds: float | None
    error: str | None
    witnesses: list[Witness] = field(default_factory=list)
    mutation: dict[str, Any] | None = None  # the witness-mutation sidecar, if any

    @property
    def entries(self) -> list[dict[str, Any]]:
        return [e for e in (self.answer or {}).get("acts") or [] if isinstance(e, dict)]

    @property
    def text(self) -> str:
        return F.reading_text(self.answer)


def witnesses_of(feed: dict[str, Any], names: dict[str, str] | None = None) -> list[Witness]:
    names = {**F.CHAIR_ARMS, **(names or {})}
    out = []
    for row in feed["witnesses"]:
        label = row["witness_label"]
        text = "\n".join(u["text"] for u in row["units"]) if row["outcome"] == "read" else ""
        out.append(Witness(label, names.get(label, label), text))
    return out


def _codes(problems: list[dict[str, Any]] | None) -> list[str]:
    return sorted({p.get("code", "?") for p in problems or []})


def answers_from_run_tree(tree: F.RunTree) -> dict[str, Answer]:
    out = {}
    for page in tree.pages.values():
        reading = page.reading
        if reading is None:
            continue
        call = page.call or {}
        usage = call.get("usage") or {}
        malformed = reading["parse_state"] != "parsed"
        codes = _codes(reading.get("problems"))
        out[page.stem] = Answer(
            stem=page.stem,
            parse_state=reading["parse_state"],
            parse_codes=codes if malformed else [],
            other_codes=[] if malformed else codes,
            answer=reading.get("answer"),
            finish_reason=reading.get("stop_reason") or reading.get("finish_reason"),
            loop_stop=bool((call.get("stream") or {}).get("stopped")),
            completion_tokens=usage.get("completion_tokens"),
            seconds=None,
            error=(reading.get("failure") or {}).get("code") if reading.get("failure") else None,
            witnesses=witnesses_of(page.feed),
        )
    return out


def answers_from_cache(folder: Path, names: dict[str, str] | None = None) -> dict[str, Answer]:
    out = {}
    for path in sorted(Path(folder).glob("*.json")):
        record = json.loads(path.read_text("utf-8"))
        if record.get("schema") != F.SCHEMA:
            continue
        usage = record.get("usage") or {}
        out[record["page"]] = Answer(
            stem=record["page"],
            parse_state=record.get("parse_state"),
            parse_codes=_codes(record.get("parse_problems")),
            other_codes=[],
            answer=record.get("answer"),
            finish_reason=record.get("finish_reason"),
            loop_stop=record.get("loop_stop") is not None,
            completion_tokens=usage.get("completion_tokens"),
            seconds=record.get("seconds"),
            error=record.get("error"),
            witnesses=witnesses_of(record["feed"], names),
            mutation=record.get("mutation"),
        )
    return out


def load_answers(path: Path, tree: F.RunTree) -> dict[str, Answer]:
    path = Path(path)
    if (path / "4_perlector").is_dir():
        if path.resolve() == tree.root.resolve():
            return answers_from_run_tree(tree)
        return answers_from_run_tree(F.load_run_tree(path))
    return answers_from_cache(path)


# --- per page -----------------------------------------------------------------------


def aligned(reference: tuple[str, ...], hypothesis: tuple[str, ...]):
    """(right per gold token, the word written in a wrong one's place or None, inserted).

    Right and wrong come from the unit-cost edit script, as `roster.align` scores them.
    A wrong gold word's replacement is the hypothesis word, among those between the
    matched words around it, closest to it by characters (None when the gap is empty):
    the script alone breaks ties arbitrarily, and a copy is judged on this word.
    """
    ops = Levenshtein.editops(reference, hypothesis, processor=None)
    wrong = {op.src_pos for op in ops if op.tag in ("replace", "delete")}
    inserted = sum(op.tag == "insert" for op in ops)
    pairs = [
        (block.src_start + k, block.dest_start + k)
        for block in ops.as_opcodes()
        if block.tag == "equal"
        for k in range(block.src_end - block.src_start)
    ]
    form: list[str | None] = [None] * len(reference)
    before = -1  # index into pairs of the last match left of i
    for i in range(len(reference)):
        while before + 1 < len(pairs) and pairs[before + 1][0] < i:
            before += 1
        if i not in wrong:
            continue
        lo = pairs[before][1] + 1 if before >= 0 else 0
        after = before + 1
        while after < len(pairs) and pairs[after][0] <= i:
            after += 1
        hi = pairs[after][1] if after < len(pairs) else len(hypothesis)
        gap = hypothesis[lo:hi]
        if gap:
            form[i] = min(gap, key=lambda w: Levenshtein.normalized_distance(reference[i], w))
    return [i not in wrong for i in range(len(reference))], form, inserted


def group_key(gold) -> str:
    group = S.page_group(gold)
    if group == "acts":
        return "acts-" + G.form_of(gold.header.get("FORM", "")).replace(" ", "-")
    return group


def score_page(answer: Answer, gold, hard: bool) -> dict[str, Any]:
    text = S.normalise_output(READER_ARM, answer.text)
    reference = gold.reference_text()
    ref_tokens = S.tokens(reference)
    row: dict[str, Any] = {
        "page": answer.stem,
        "group": group_key(gold),
        "hard": hard,
        "parsed": answer.parse_state == "parsed",
        "ref_tokens": len(ref_tokens),
        "cer": S.cer_wer(reference, text)["cer"] if reference.strip() else None,
        "false_text": len(text) > S.FALSE_TEXT_CHARS if not reference.strip() else None,
    }
    if S.page_group(gold) == "acts":
        acts = [e.get("text") or "" for e in answer.entries if e.get("kind") == "act"]
        rec = S.record_scores({"arm": READER_ARM, "units": [{"text": t} for t in acts]}, gold)
        row.update(
            gold_acts=rec["acts"],
            acts_matched=rec["acts_matched"],
            act_entries=len(acts),
            act_count_exact=len(acts) == rec["acts"],
        )
    if gold.rows:
        lines = text.splitlines()
        row.update(S.line_recall(gold.row_lines(), lines, gold.heading_lines()))
        row["surname_recall"] = S.surname_recall(gold.row_lines(), text)
    right, form, inserted = aligned(ref_tokens, S.tokens(text))
    row["inserted"] = inserted
    row["reader_right"] = right
    row["reader_form"] = form
    wit = {}
    for w in answer.witnesses:
        w_right, w_form, _ = aligned(ref_tokens, S.tokens(S.normalise_output(w.name, w.text)))
        wit[w.name] = {"right": w_right, "form": w_form, "read": bool(w.text.strip())}
    row["witnesses"] = wit
    return row


def scepticism(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """The follow, copy and vote counts over the gold tokens of these pages."""
    c: Counter = Counter()
    names: list[str] = []
    for row in rows:
        wit = row["witnesses"]
        for name in wit:
            if name not in names:
                names.append(name)
        voters = [n for n in wit if wit[n]["read"]]
        for i, r in enumerate(row["reader_right"]):
            right = {n: wit[n]["right"][i] for n in wit}
            k = sum(right.values())
            c["tokens"] += 1
            c["reader-right"] += r
            if k == len(right) and right:
                c["all-right"] += 1
                c["all-right,damaged"] += not r
            if k == 0:
                c["all-wrong"] += 1
                c["all-wrong,recovered"] += r
            if k == 1:
                (name,) = [n for n, v in right.items() if v]
                c[f"only-right:{name}"] += 1
                c[f"only-right:{name},followed"] += r
            if len(right) >= 2 and k == len(right) - 1:
                (name,) = [n for n, v in right.items() if not v]
                c[f"only-wrong:{name}"] += 1
                c[f"only-wrong:{name},resisted"] += r
            if voters:
                majority = sum(right[n] for n in voters) * 2 > len(voters)
                c["vote-beaten"] += (not majority) and r
                c["vote-lost"] += majority and not r
            mine = row["reader_form"][i]
            copied_any = False
            for name in wit:
                theirs = wit[name]["form"][i]
                if not right[name] and theirs is not None:
                    c[f"wrong-form:{name}"] += 1
                    if not r and mine == theirs:
                        c[f"wrong-form:{name},copied"] += 1
                        copied_any = True
            if not r:
                c["reader-wrong"] += 1
                c["reader-wrong,a-witness-error"] += copied_any
    return {"counts": c, "witnesses": names}


def planted_copies(rows: list[dict[str, Any]], answers: dict[str, Answer]) -> dict[str, Any]:
    """Per planted site: resisted (reader right), copied (the reader wrote the planted
    word) or other-wrong; grouped by scenario, by k, by chair (k = 1) and by class.
    A site whose reference index lies past this gold's words is `misaligned` (the
    sidecar was planted against another reference) and not judged."""
    c: Counter = Counter()
    for row in rows:
        answer = answers[row["page"]]
        sidecar = answer.mutation
        if not sidecar or not row["parsed"]:
            continue
        scenario = sidecar.get("scenario", "?")
        for site in sidecar.get("planted") or []:
            i = site.get("ref_index")
            if i is None or i >= len(row["reader_right"]):
                c["misaligned"] += 1
                continue
            planted = " ".join(S.tokens(site["planted"]))
            mine = row["reader_form"][i]
            outcome = (
                "resisted"
                if row["reader_right"][i]
                else "copied"
                if mine is not None and " ".join(S.tokens(mine)) == planted
                else "other-wrong"
            )
            k = site.get("k") or len(site.get("witnesses") or [])
            keys = [f"scenario:{scenario}", f"k:{k}", f"class:{site.get('cls', '?')}", "all"]
            if k == 1 and site.get("witnesses"):
                label = site["witnesses"][0]
                keys.append(f"chair:{F.CHAIR_ARMS.get(label, label)}")
            for key in keys:
                c[f"{key},sites"] += 1
                c[f"{key},{outcome}"] += 1
    return dict(c)


def _ratio(n: int, d: int) -> float | None:
    return n / d if d else None


def summarise(rows: list[dict[str, Any]], answers: dict[str, Answer]) -> dict[str, Any]:
    pages = [r["page"] for r in rows]
    parsed = [r for r in rows if r["parsed"]]
    cer_parsed = [r["cer"] for r in parsed if r["cer"] is not None]
    cer_all = [r["cer"] for r in rows if r["cer"] is not None]
    act_rows = [r for r in rows if "gold_acts" in r]
    row_rows = [r for r in rows if r.get("rows")]
    tokens = sum(r["ref_tokens"] for r in rows)
    blank = [r for r in rows if r["false_text"] is not None]
    health = [answers[p] for p in pages]
    completions = [a.completion_tokens for a in health if a.completion_tokens is not None]
    return {
        "pages": len(rows),
        "parsed": len(parsed),
        "cer_median_parsed": statistics.median(cer_parsed) if cer_parsed else None,
        "cer_mean_parsed": statistics.fmean(cer_parsed) if cer_parsed else None,
        "cer_median_all": statistics.median(cer_all) if cer_all else None,
        "gold_acts": sum(r["gold_acts"] for r in act_rows),
        "acts_matched": sum(r["acts_matched"] for r in act_rows),
        "act_entries": sum(r["act_entries"] for r in act_rows),
        "act_pages": len(act_rows),
        "act_count_exact": sum(r["act_count_exact"] for r in act_rows),
        "rows": sum(r["rows"] for r in row_rows),
        "rows_matched": sum(r["rows_matched"] for r in row_rows),
        "surname_recall": statistics.fmean(s)
        if (s := [r["surname_recall"] for r in row_rows if r.get("surname_recall") is not None])
        else None,
        "gold_tokens": tokens,
        "inserted": sum(r["inserted"] for r in rows),
        "blank_pages": len(blank),
        "false_text_pages": sum(bool(r["false_text"]) for r in blank),
        "parse_codes": Counter(code for a in health for code in a.parse_codes),
        "other_codes": Counter(code for a in health for code in a.other_codes),
        "errors": sum(a.error is not None for a in health),
        "finish": Counter(str(a.finish_reason) for a in health),
        "loop_stops": sum(a.loop_stop for a in health),
        "completion_median": statistics.median(completions) if completions else None,
        "completion_max": max(completions) if completions else None,
        "seconds_median": statistics.median(s)
        if (s := [a.seconds for a in health if a.seconds is not None])
        else None,
        # Only pages whose answer parsed: an unparsed page says nothing about whom the
        # reader follows (A4's rule), and its words count under answer health instead.
        "scepticism": scepticism(parsed),
        "planted": planted_copies(parsed, answers),
    }


# --- the whole card -----------------------------------------------------------------


GROUP_ORDER = (
    "acts-handwritten", "acts-typed", "acts-printed-form", "acts-mixed", "prose-other",
    "tables", "index-list", "blank-like", "test", "unassigned",
)  # fmt: skip


def scorecard(answers: dict[str, Answer], gold: dict, hard: set[str]) -> dict[str, Any]:
    rows = [score_page(a, gold[s], s in hard) for s, a in sorted(answers.items()) if s in gold]
    by_group: dict[str, list] = defaultdict(list)
    for row in rows:
        by_group[row["group"]].append(row)
    order = [g for g in GROUP_ORDER if g in by_group] + sorted(set(by_group) - set(GROUP_ORDER))
    groups = {g: summarise(by_group[g], answers) for g in order}
    hard_rows = [r for r in rows if r["hard"]]
    every = [r for r in rows if r["group"] != G.TEST]
    return {
        "rows": rows,
        "groups": groups,
        "hard": summarise(hard_rows, answers) if hard_rows else None,
        "hard_acts": summarise([r for r in hard_rows if r["group"].startswith("acts")], answers)
        if any(r["group"].startswith("acts") for r in hard_rows)
        else None,
        "all": summarise(every, answers),
        "missing": sorted(s for s in gold if s not in answers),
    }


def invariance(a: dict[str, Answer], b: dict[str, Answer], card_a, card_b) -> dict[str, Any]:
    """How far a second answer set moved from the first, page by page and word by word."""
    common = sorted(set(a) & set(b))
    cers, same = [], 0
    for stem in common:
        ta, tb = (S.normalise_output(READER_ARM, x[stem].text) for x in (a, b))
        same += ta == tb
        if ta.strip():
            cers.append(S.cer_wer(ta, tb)["cer"])
    rows_a = {r["page"]: r for r in card_a["rows"]}
    rows_b = {r["page"]: r for r in card_b["rows"]}
    flips = Counter()
    for stem in set(rows_a) & set(rows_b):
        key = rows_a[stem]["group"]
        for x, y in zip(rows_a[stem]["reader_right"], rows_b[stem]["reader_right"], strict=True):
            flips[f"{key}:tokens"] += 1
            flips[f"{key}:flips"] += x != y
            flips[f"{key}:gained"] += y and not x
            flips["all:tokens"] += 1
            flips["all:flips"] += x != y
            flips["all:gained"] += y and not x
    return {
        "pages": len(common),
        "identical": same,
        "cer_between_median": statistics.median(cers) if cers else None,
        "cer_between_mean": statistics.fmean(cers) if cers else None,
        "flips": flips,
    }


# --- report -------------------------------------------------------------------------


def _f(value: float | None, digits: int = 3) -> str:
    return "–" if value is None else f"{value:.{digits}f}"


def _share(n: int, d: int) -> str:
    return "–" if not d else f"{n} / {d} ({n / d:.2f})"


def _blocks(card: dict[str, Any]) -> list[tuple[str, dict[str, Any]]]:
    blocks = list(card["groups"].items())
    if card["hard_acts"]:
        blocks.append(("hard act pages", card["hard_acts"]))
    if card["hard"]:
        blocks.append(("hard pages (all)", card["hard"]))
    blocks.append(("all pages (no test)", card["all"]))
    return blocks


def report(card: dict[str, Any], title: str, label: str) -> list[str]:
    out = [f"## {title}{label}", ""]
    out += [
        "### Reading",
        "",
        "| group | pages | parsed | CER median (parsed) | CER mean (parsed) | CER median (all) |",
        "|---|---:|---:|---:|---:|---:|",
    ]
    for name, s in _blocks(card):
        out.append(
            f"| {name} | {s['pages']} | {s['parsed']} | {_f(s['cer_median_parsed'])} | "
            f"{_f(s['cer_mean_parsed'])} | {_f(s['cer_median_all'])} |"
        )
    out += [
        "",
        "| group | act recall | act entries / gold acts | act count exact | row recall | "
        "surname recall | inserted per gold word | false text (blank pages) |",
        "|---|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for name, s in _blocks(card):
        out.append(
            f"| {name} | {_share(s['acts_matched'], s['gold_acts'])} | "
            f"{s['act_entries']} / {s['gold_acts']} | {s['act_count_exact']} / {s['act_pages']} | "
            f"{_share(s['rows_matched'], s['rows'])} | {_f(s['surname_recall'])} | "
            f"{_f(_ratio(s['inserted'], s['gold_tokens']))} | "
            f"{s['false_text_pages']} / {s['blank_pages']} |"
        )
    s = card["all"]
    out += [
        "",
        "### Answer health (all pages)",
        "",
        f"- parsed {s['parsed']} / {s['pages']}; errors (no reply) {s['errors']}",
        "- malformed, by reason (pages): "
        + (", ".join(f"{k} {v}" for k, v in s["parse_codes"].most_common()) or "none"),
        "- other problems on parsed pages (page accounting, run tree only): "
        + (", ".join(f"{k} {v}" for k, v in s["other_codes"].most_common()) or "none"),
        "- finish: " + ", ".join(f"{k} {v}" for k, v in s["finish"].most_common()),
        f"- loop-guard stops {s['loop_stops']}; completion tokens median "
        f"{_f(s['completion_median'], 0)}, max {_f(s['completion_max'], 0)}; seconds median "
        f"{_f(s['seconds_median'], 1)}",
    ]
    if card["missing"]:
        out.append(f"- gold pages with no answer: {len(card['missing'])}")
    out += [
        "",
        "### Scepticism (gold words on pages whose answer parsed; witnesses as shown)",
        "",
    ]
    for name, summ in _blocks(card):
        sc = summ["scepticism"]
        c, names = sc["counts"], sc["witnesses"]
        if not c["tokens"]:
            continue
        out += [
            f"**{name}**: {summ['parsed']} parsed pages, {c['tokens']} gold words, reader right "
            f"{_f(_ratio(c['reader-right'], c['tokens']), 2)}; all witnesses wrong, recovered "
            f"{_share(c['all-wrong,recovered'], c['all-wrong'])}; all right, damaged "
            f"{_share(c['all-right,damaged'], c['all-right'])}; beats the vote "
            f"{c['vote-beaten']} - {c['vote-lost']} = {c['vote-beaten'] - c['vote-lost']:+d} "
            f"({_f(_ratio(c['vote-beaten'] - c['vote-lost'], c['tokens']) * 1000 if c['tokens'] else None, 1)}"
            " per 1,000 words); reader errors that are a witness's error "
            f"{_share(c['reader-wrong,a-witness-error'], c['reader-wrong'])}",
            "",
            "| witness | only it right → followed | only it wrong → resisted | its wrong word copied |",
            "|---|---:|---:|---:|",
        ]
        for w in names:
            out.append(
                f"| {w} | {_share(c[f'only-right:{w},followed'], c[f'only-right:{w}'])} | "
                f"{_share(c[f'only-wrong:{w},resisted'], c[f'only-wrong:{w}'])} | "
                f"{_share(c[f'wrong-form:{w},copied'], c[f'wrong-form:{w}'])} |"
            )
        out.append("")
    out += planted_report(card)
    return out


def planted_report(card: dict[str, Any]) -> list[str]:
    """The planted-error block, only when some answer carried a mutation sidecar."""
    p = card["all"]["planted"]
    if not p.get("all,sites") and not p.get("misaligned"):
        return []
    out = [
        "### Planted errors (witness-mutation sidecars; pages whose answer parsed)",
        "",
        "| group | sites | copied | resisted | other wrong |",
        "|---|---:|---:|---:|---:|",
    ]
    groups = sorted({k.split(",")[0] for k in p if "," in k}, key=lambda g: (g != "all", g))
    for g in groups:
        out.append(
            f"| {g} | {p.get(f'{g},sites', 0)} | "
            f"{_share(p.get(f'{g},copied', 0), p.get(f'{g},sites', 0))} | "
            f"{_share(p.get(f'{g},resisted', 0), p.get(f'{g},sites', 0))} | "
            f"{p.get(f'{g},other-wrong', 0)} |"
        )
    if p.get("misaligned"):
        out.append(f"\nSites not judged (reference index past this gold's words): {p['misaligned']}")
    return out + [""]


def compare_report(card_a, card_b, inv, names: tuple[str, str], label: str) -> list[str]:
    a, b = names
    out = [f"## {a} vs {b}{label}", ""]
    out += [
        f"Same pages {inv['pages']}; read identically {inv['identical']}; CER between the two "
        f"readings median {_f(inv['cer_between_median'])}, mean {_f(inv['cer_between_mean'])}. "
        "Compare with two runs of the same arm (the noise floor) before reading a change.",
        "",
        "| group | gold words | right/wrong flipped | of which now right |",
        "|---|---:|---:|---:|",
    ]
    keys = sorted({k.split(":")[0] for k in inv["flips"]}, key=lambda k: (k == "all", k))
    for key in keys:
        f = inv["flips"]
        out.append(
            f"| {key} | {f[f'{key}:tokens']} | {_share(f[f'{key}:flips'], f[f'{key}:tokens'])} | "
            f"{f[f'{key}:gained']} |"
        )
    out += ["", "| measure | group | " + a + " | " + b + " |", "|---|---|---:|---:|"]
    for group in [g for g in card_a["groups"] if g in card_b["groups"]] + ["all"]:
        sa = card_a["all"] if group == "all" else card_a["groups"][group]
        sb = card_b["all"] if group == "all" else card_b["groups"][group]
        ca, cb = sa["scepticism"]["counts"], sb["scepticism"]["counts"]
        out.append(
            f"| CER median (parsed) | {group} | {_f(sa['cer_median_parsed'])} | "
            f"{_f(sb['cer_median_parsed'])} |"
        )
        out.append(
            f"| all wrong, recovered | {group} | {_f(_ratio(ca['all-wrong,recovered'], ca['all-wrong']), 2)} | "
            f"{_f(_ratio(cb['all-wrong,recovered'], cb['all-wrong']), 2)} |"
        )
        for w in sa["scepticism"]["witnesses"]:
            ra = _ratio(ca[f"only-right:{w},followed"], ca[f"only-right:{w}"])
            rb = _ratio(cb[f"only-right:{w},followed"], cb[f"only-right:{w}"])
            if ra is not None or rb is not None:
                out.append(f"| only {w} right, followed | {group} | {_f(ra, 2)} | {_f(rb, 2)} |")
    return out + [""]


def _jsonable(card: dict[str, Any]) -> dict[str, Any]:
    def clean(summary):
        if summary is None:
            return None
        sc = summary["scepticism"]
        return {
            **{k: v for k, v in summary.items() if k != "scepticism"},
            "scepticism": {"witnesses": sc["witnesses"], "counts": dict(sc["counts"])},
        }

    rows = [
        {k: v for k, v in r.items() if k not in ("reader_right", "reader_form", "witnesses")}
        for r in card["rows"]
    ]
    return {
        "pages": rows,
        "groups": {g: clean(s) for g, s in card["groups"].items()},
        "hard": clean(card["hard"]),
        "hard_acts": clean(card["hard_acts"]),
        "all": clean(card["all"]),
        "missing": card["missing"],
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument(
        "--run-tree", type=Path, required=True, help="the sealed run (feeds, stems)"
    )
    parser.add_argument(
        "--answers", type=Path, help="a fed_arm cache folder (default: the run's own)"
    )
    parser.add_argument(
        "--compare", type=Path, help="a second answer set: cache folder or run tree"
    )
    parser.add_argument("--names", default=None, help="'A,B': what to call the two answer sets")
    parser.add_argument("--gold", type=Path, required=True)
    parser.add_argument("--gold-glob", default="**/*.txt")
    parser.add_argument("--hard-pages", type=Path)
    parser.add_argument("--exclude", type=Path, help="stems to leave out, one per line")
    parser.add_argument("--out", type=Path, help="write scorecard.md and scorecard.json here")
    args = parser.parse_args(argv)

    tree = F.load_run_tree(args.run_tree)
    gold = load_gold_dir(args.gold, args.gold_glob)
    if args.exclude:
        for stem in args.exclude.read_text("utf-8").split():
            gold.pop(stem, None)
    hard = set(args.hard_pages.read_text("utf-8").split()) if args.hard_pages else set()
    answers = load_answers(args.answers or args.run_tree, tree)
    names = (args.names or f"{(args.answers or args.run_tree).name},"
             f"{args.compare.name if args.compare else ''}").split(",")  # fmt: skip
    used = [g for s, g in gold.items() if s in answers]
    fools = any("fool" in g.status.lower() for g in used) or not used
    label = f" {S.FOOLS_GOLD_LABEL}" if fools else " (lead-checked gold)"
    card = scorecard(answers, gold, hard)
    lines = [f"# Perlector scorecard{label}", ""]
    if fools:
        lines += [
            "The reference is an unchecked AI draft: every number is a ballpark against it, "
            "not an accuracy.",
            "",
        ]
    lines += report(card, f"{names[0]}: {len(answers)} answers", label)
    out_json: dict[str, Any] = {"label": label.strip(), "answers": _jsonable(card)}
    if args.compare:
        other = load_answers(args.compare, tree)
        card_b = scorecard(other, gold, hard)
        inv = invariance(answers, other, card, card_b)
        lines += compare_report(card, card_b, inv, (names[0], names[1]), label)
        lines += report(card_b, f"{names[1]}: {len(other)} answers", label)
        out_json["compare"] = _jsonable(card_b)
        out_json["invariance"] = {**inv, "flips": dict(inv["flips"])}
    text = "\n".join(lines) + "\n"
    print(text)
    if args.out:
        args.out.mkdir(parents=True, exist_ok=True)
        (args.out / "scorecard.md").write_text(text, "utf-8")
        (args.out / "scorecard.json").write_text(
            json.dumps(out_json, ensure_ascii=False, indent=1, default=str), "utf-8"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
