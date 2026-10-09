"""Which candidate arms earn a fourth witness seat, per page group: the roster metrics.

    python -m operations.bakeoff.roster --cache CACHE --gold DIR --gold-glob ... --out DIR
        [--baselines chandra,dai,churro] [--hard-pages FILE] [--exclude FILE]

Every model's plain text (`score.normalise_output`) and the gold reference are split into
graphemic-v1 tokens (`score.tokens`) and aligned with RapidFuzz's unit-cost edit script,
so each gold token is right or wrong (substituted or deleted) for each arm, and each
inserted model token is an invention. Per page group (test pages left out; hard
pages count like every page), for each candidate against the baselines:

- rescue rate: gold tokens every baseline gets wrong and the candidate gets right, over
  gold tokens every baseline gets wrong;
- error correlation: the phi coefficient between the per-token wrong indicators of the
  candidate and each baseline, beside the baselines' own pairwise phi;
- shared fabrication: candidate insertions that some baseline inserted too on the same
  page, over the candidate's insertions;
- union line recall (index-list only): gold rows matched by any baseline's lines, then by
  any baseline's or the candidate's;
- insertion rate: inserted tokens over gold tokens on the arm's own pages, beside the
  group leader's.

Rescue, correlation, shared fabrication and union recall use only pages every baseline
and the candidate have a reading for; an empty reading gets every token wrong. On each
group the baselines are only those scored there (`groups.MODEL_GROUPS`): DAI reads
records, so its empty index pages would make every correlation with it undefined and
block the rule's rescue route on every group but `acts`.

The roster rule is printed as a suggestion with its inputs, never as a decision: a
candidate earns a fourth-witness arm on a page group when it is top-two on rescue rate
with every correlation below the baselines' lowest pairwise correlation, or when it is
the lowest-invention arm among those within 10 points of the leader on the group's
headline metric (tied with no baseline, since a seat goes to an arm that invents less than
the witnesses already seated).
"""

from __future__ import annotations

import argparse
import json
import math
from collections import Counter
from dataclasses import dataclass
from itertools import combinations
from typing import Any

from rapidfuzz.distance import Levenshtein

from operations.bakeoff import groups as G
from operations.bakeoff import score as S

LEADER_MARGIN = 0.10
DEFAULT_BASELINES = "chandra,dai,churro"


@dataclass
class Reading:
    correct: list[bool]
    inserted: list[str]
    lines: list[str]


def align(reference: tuple[str, ...], hypothesis: tuple[str, ...]) -> tuple[list[bool], list[str]]:
    """(right or wrong per reference token, the hypothesis tokens inserted)."""
    ops = Levenshtein.editops(reference, hypothesis, processor=None)
    wrong = {op.src_pos for op in ops if op.tag in ("replace", "delete")}
    inserted = [hypothesis[op.dest_pos] for op in ops if op.tag == "insert"]
    return [i not in wrong for i in range(len(reference))], inserted


def read(record: dict[str, Any], gold) -> Reading:
    text = S.normalise_output(record.get("arm", ""), record.get("text"))
    correct, inserted = align(S.tokens(gold.reference_text()), S.tokens(text))
    return Reading(correct, inserted, text.splitlines())


def phi(x: list[bool], y: list[bool]) -> float | None:
    """Matthews/phi correlation of two boolean vectors; None when either is constant."""
    n11 = sum(a and b for a, b in zip(x, y, strict=True))
    n10 = sum(a and not b for a, b in zip(x, y, strict=True))
    n01 = sum(b and not a for a, b in zip(x, y, strict=True))
    n00 = len(x) - n11 - n10 - n01
    denominator = (n11 + n10) * (n01 + n00) * (n11 + n01) * (n10 + n00)
    if denominator == 0:
        return None
    return (n11 * n00 - n10 * n01) / math.sqrt(denominator)


def _ratio(numerator: int, denominator: int) -> float | None:
    return numerator / denominator if denominator else None


def _wrong(readings: dict[str, Reading], pages: list[str]) -> list[bool]:
    return [not ok for p in pages for ok in readings[p].correct]


def rescue_rate(candidate: list[bool], baselines: list[list[bool]]) -> tuple[float | None, int]:
    """(rescue rate, tokens every baseline gets wrong), from per-token wrong indicators."""
    hard = [i for i in range(len(candidate)) if all(b[i] for b in baselines)]
    return _ratio(sum(not candidate[i] for i in hard), len(hard)), len(hard)


def shared_fabrication(
    candidate: list[list[str]], baselines: dict[str, list[list[str]]]
) -> tuple[float | None, dict[str, float | None]]:
    """(share shared with any baseline, share shared with each), page by page as multisets."""
    total = sum(len(page) for page in candidate)
    anyone, each = 0, dict.fromkeys(baselines, 0)
    for index, page in enumerate(candidate):
        mine = Counter(page)
        theirs = {b: Counter(pages[index]) for b, pages in baselines.items()}
        for token, count in mine.items():
            anyone += min(count, max((t[token] for t in theirs.values()), default=0))
            for b, t in theirs.items():
                each[b] += min(count, t[token])
    return _ratio(anyone, total), {b: _ratio(n, total) for b, n in each.items()}


def union_line_recall(
    pages: list[tuple[list[str], list[list[str]]]],
) -> float | None:
    """Rows matched by any of several arms' lines, pooled over pages of (rows, arms' lines)."""
    rows = matched = 0
    for gold_rows, arms_lines in pages:
        rows += len(S.line_keys(gold_rows))
        hit: set[int] = set()
        for lines in arms_lines:
            hit |= S.matched_rows(gold_rows, lines)
        matched += len(hit)
    return _ratio(matched, rows)


def insertion_rate(readings: dict[str, Reading], pages: list[str]) -> float | None:
    mine = [p for p in pages if p in readings]
    inserted = sum(len(readings[p].inserted) for p in mine)
    return _ratio(inserted, sum(len(readings[p].correct) for p in mine))


def _better(metric: str, a: float, b: float) -> bool:
    return a > b if G.METRICS[metric][1] else a < b


def leader_and_lowest(
    headline: dict[str, float | None], insertions: dict[str, float | None], metric: str
) -> tuple[str | None, list[str]]:
    """The arm best on the headline metric, and the arms tied for least invention within
    10 points of it."""
    scored = {m: v for m, v in headline.items() if v is not None}
    if not scored:
        return None, []
    leader = None
    for model, value in sorted(scored.items()):
        if leader is None or _better(metric, value, scored[leader]):
            leader = model
    near = [
        m
        for m, v in scored.items()
        if abs(v - scored[leader]) <= LEADER_MARGIN + 1e-12 and insertions.get(m) is not None
    ]
    least = min((insertions[m] for m in near), default=None)
    return leader, sorted(m for m in near if insertions[m] == least)


def top_two(rescue: dict[str, float | None]) -> set[str]:
    """Candidates with a rescue rate at least the second-highest one (ties kept)."""
    values = sorted((v for v in rescue.values() if v is not None), reverse=True)
    if not values:
        return set()
    cut = values[min(1, len(values) - 1)]
    return {m for m, v in rescue.items() if v is not None and v >= cut}


def suggest(
    row: dict[str, Any], pairwise_min: float | None, top: set[str], least_inventing: set[str]
) -> str:
    correlations = list(row["correlation"].values())
    low_correlation = (
        pairwise_min is not None
        and correlations
        and all(c is not None and c < pairwise_min for c in correlations)
    )
    reasons = []
    if row["candidate"] in top and low_correlation:
        reasons.append("top-two rescue, correlation below the baselines'")
    if row["candidate"] in least_inventing:
        reasons.append("lowest invention within 10 points of the leader")
    return "earns a 4th-witness arm: " + "; ".join(reasons) if reasons else "no"


def _readings(cache, gold) -> dict[str, dict[str, Reading]]:
    return {
        model: {stem: read(record, gold[stem]) for stem, record in pages.items() if stem in gold}
        for model, pages in cache.items()
    }


def _headline(cache, gold, model, pages, metric) -> float | None:
    records = cache[model]
    rows = [S.score_page(records[p], gold[p]) for p in pages if p in records]
    return S.summarise(rows, metric) if rows else None


def roster_group(group, baselines, cache, gold, readings) -> list[dict[str, Any]]:
    """The roster rows of one page group: a baselines row, then one per candidate."""
    pages = sorted(p for p, g in gold.items() if S.page_group(g) == group.name)
    if not pages:
        return []
    arms = {m: S.model_arm(cache[m]) for m in cache}
    scored = [m for m in cache if group.name in G.groups_for(arms[m])[0]]
    candidates = [m for m in scored if m not in baselines]
    # The witnesses already seated on this group: a baseline not scored here (DAI off
    # act pages) is no witness, and its empty readings would only blank the correlations.
    baselines = [b for b in baselines if b in scored]
    if not baselines:
        return []
    shared = [p for p in pages if all(p in readings[b] for b in baselines)]
    pairwise = {
        f"{a}~{b}": phi(_wrong(readings[a], shared), _wrong(readings[b], shared))
        for a, b in combinations(baselines, 2)
    }
    known = [v for v in pairwise.values() if v is not None]
    pairwise_min = min(known) if known else None
    headline = {m: _headline(cache, gold, m, pages, group.headline) for m in scored}
    insertions = {m: insertion_rate(readings[m], pages) for m in cache}
    leader, lowest = leader_and_lowest(headline, insertions, group.headline)
    out = [
        {
            "group": group.name,
            "kind": "baselines",
            "baselines": list(baselines),
            "pages": len(shared),
            "baseline_pairwise": pairwise,
            "baseline_pairwise_min": pairwise_min,
            "headline_metric": group.headline,
            "headline": {m: headline.get(m) for m in baselines},
            "insertion_rate": {m: insertions[m] for m in baselines},
            "leader": leader,
            "leader_headline": headline.get(leader),
            "leader_insertion_rate": insertions.get(leader),
            "lowest_invention": lowest,
        }
    ]
    rows = []
    for c in candidates:
        mine = [p for p in shared if p in readings[c]]
        wrong = _wrong(readings[c], mine)
        base = {b: _wrong(readings[b], mine) for b in baselines}
        rescue, denominator = rescue_rate(wrong, list(base.values()))
        fabrication, by_baseline = shared_fabrication(
            [readings[c][p].inserted for p in mine],
            {b: [readings[b][p].inserted for p in mine] for b in baselines},
        )
        row = {
            "group": group.name,
            "kind": "candidate",
            "candidate": c,
            "arm": arms[c],
            "pages": len(mine),
            "gold_tokens": len(wrong),
            "baselines_all_wrong": denominator,
            "rescue_rate": rescue,
            "correlation": {b: phi(wrong, base[b]) for b in baselines},
            "baseline_pairwise_min": pairwise_min,
            "shared_fabrication": fabrication,
            "shared_fabrication_by_baseline": by_baseline,
            "insertion_rate": insertions[c],
            "headline_metric": group.headline,
            "headline": headline.get(c),
            "leader": leader,
            "leader_headline": headline.get(leader),
            "leader_insertion_rate": insertions.get(leader),
        }
        if group.name == "index-list":
            per_page = [
                (gold[p].row_lines(), [readings[b][p].lines for b in baselines]) for p in mine
            ]
            row["union_line_recall_baselines"] = union_line_recall(per_page)
            row["union_line_recall_with"] = union_line_recall(
                [
                    (r, lines + [readings[c][p].lines])
                    for (r, lines), p in zip(per_page, mine, strict=True)
                ]
            )
        rows.append(row)
    top = top_two({r["candidate"]: r["rescue_rate"] for r in rows})
    least_inventing = set() if set(lowest) & set(baselines) else set(lowest)
    for row in rows:
        row["top_two_rescue"] = row["candidate"] in top
        row["lowest_invention"] = row["candidate"] in lowest
        row["suggestion"] = suggest(row, pairwise_min, top, least_inventing)
    return out + rows


def _f(value: float | None) -> str:
    return S._fmt(value)


def report(results: list[dict[str, Any]], baselines: list[str], fools: bool) -> str:
    label = f" {S.FOOLS_GOLD_LABEL}" if fools else ""
    lines = [
        f"# Witness roster metrics{label}",
        "",
        "The rule's verdict is a suggestion for the lead, never a decision. Rescue: tokens "
        f"all of {', '.join(baselines)} get wrong that the candidate gets right. r(x): phi "
        "correlation of wrong tokens with baseline x. Shared fab.: candidate insertions a "
        "baseline also made. Ins.: inserted tokens over gold tokens. Union LR: baselines' "
        "line recall together, then with the candidate.",
        "",
    ]
    for group in G.GROUPS:
        rows = [r for r in results if r["group"] == group.name]
        if not rows:
            continue
        base, cands = rows[0], rows[1:]
        metric = G.METRICS[group.headline][0]
        pairwise = ", ".join(f"{k} {_f(v)}" for k, v in base["baseline_pairwise"].items())
        lines += [
            f"## {group.name}{label}",
            "",
            f"Baselines here: {', '.join(base['baselines'])}. "
            f"Their pairwise r ({base['pages']} pages): {pairwise or '–'}; lowest "
            f"{_f(base['baseline_pairwise_min'])}. Leader on {metric}: {base['leader']} "
            f"({_f(base['leader_headline'])}, ins. {_f(base['leader_insertion_rate'])}). "
            f"Least invention within 10 points: {', '.join(base['lowest_invention']) or '–'}.",
            "",
        ]
        if not cands:
            lines += ["No candidate is scored on this group.", ""]
            continue
        # Two narrow tables rather than one wide one: the lead reads this on a phone.
        seated = base["baselines"]
        heads = " | ".join(f"r({b})" for b in seated)
        lines += [
            f"| candidate | pages | rescue | {heads} |",
            "|---|---:|---:|" + "---:|" * len(seated),
        ]
        for r in cands:
            cells = " | ".join(_f(r["correlation"].get(b)) for b in seated)
            lines.append(f"| {r['candidate']} | {r['pages']} | {_f(r['rescue_rate'])} | {cells} |")
        union = " | union LR" if group.name == "index-list" else ""
        lines += [
            "",
            f"| candidate | shared fab. | ins. | {metric}{union} | suggestion |",
            "|---|---:|---:|---:|" + ("---|" if union else "") + "---|",
        ]
        for r in cands:
            extra = ""
            if union:
                extra = (
                    f" | {_f(r['union_line_recall_baselines'])} → {_f(r['union_line_recall_with'])}"
                )
            lines.append(
                f"| {r['candidate']} | {_f(r['shared_fabrication'])} | "
                f"{_f(r['insertion_rate'])} | {_f(r['headline'])}{extra} | {r['suggestion']} |"
            )
        lines.append("")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    S.add_page_arguments(parser)
    parser.add_argument("--baselines", default=DEFAULT_BASELINES)
    args = parser.parse_args(argv)
    baselines = [b for b in args.baselines.split(",") if b]
    gold, cache, _hard, _notes = S.load_inputs(args)
    absent = [b for b in baselines if b not in cache]
    if absent:
        print(f"refused: no cached readings for the baselines {absent}")
        return 2
    readings = _readings(cache, gold)
    results = []
    for group in G.GROUPS:
        if group.name != G.TEST:
            results += roster_group(group, baselines, cache, gold, readings)
    args.out.mkdir(parents=True, exist_ok=True)
    with open(args.out / "roster.jsonl", "w", encoding="utf-8") as handle:
        for row in results:
            handle.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")
    fools = any("fool" in g.status.lower() for g in gold.values())
    (args.out / "roster.md").write_text(report(results, baselines, fools), "utf-8")
    print(f"roster for {len(cache) - len(baselines)} candidates -> {args.out}/roster.md")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
