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
  empty), act recall (gold acts matched one-to-one by an act or instrument entry at CER <= 0.5,
  and pages whose act count is exact), index/table row recall (`score.line_recall`),
  surname recall, invented text (inserted words per gold word; false text on pages with
  no gold text);
- answer health: parsed / malformed and why, errors, finish reasons, loop stops,
  completion tokens;
- scepticism, per witness (named by its bake-off arm, else its label), on the pages whose
  answer parsed, among the witnesses that read the page (a failed or empty witness is
  counted apart as absent):
  - only-X-right, followed: the gold word is right in X alone; share the reader has right
    (agreement with X, not proof the reader relied on it);
  - only-X-wrong, resisted: X alone wrote a wrong word; share the reader still has right
    (X leaving the word out is counted apart);
  - copy of a wrong X: of the words X got wrong with a word of its own, share where the
    reader wrote X's same wrong word (each written word stands in for one gold word at
    most); and the share of the reader's own errors that are some witness's error;
  - all witnesses wrong, recovered; all right, damaged (the reader changed a word every
    witness had right);
  - beats the vote: words the reader has right where the plurality of the witnesses that
    read was wrong, minus words it has wrong where it was right; ties that include the
    right word reported apart (`vote`, shared with `mutations.vote_check`);
  - 95% intervals from resampling pages (`BOOTSTRAP_REPS`), since words on one page are
    not independent;
- failure-inclusive: a run page with gold and no answer counts as a failed, empty reading;
- with `--compare`: the same scorecard for a second answer set (a swap, a variant, or a
  repeat of the same arm for the noise floor), side by side; the paired pages (both
  parsed) with each rate's per-page difference and its interval; and invariance: per
  page the CER between the two readings' texts, pages read identically, and gold words
  whose right/wrong flipped.
- planted errors: a `fed_arm` cache made with `--mutations` carries each page's
  `witness-mutation.v1` sidecar (`operations/bakeoff/mutations.py`); for every planted
  site the reader either resisted (its word is right), copied (it wrote the planted
  word) or went wrong another way, counted per scenario, per number of witnesses that
  carried the error (k = 1, 2, 3), per chair for k = 1, and per word class. A site is
  judged only against the reference it was planted on: the sidecar's whole-reference
  digest (`reference_record_sha256`) must equal the reference rebuilt from `--gold` as
  `mutations` builds it (statuses from the run's own feeds, `--row-kind`), and its
  scored-words digest and each site's word must match the gold's.

Every heading says "vs fool's gold (ballpark, not accuracy)" while any scored gold page's
STATUS says fool's gold; only an explicit checked status (`gold (<who> <date>)` or
`lead-checked`) on every scored page, in both compared sets, drops the label.
"""

from __future__ import annotations

import argparse
import datetime
import hashlib
import json
import random
import re
import statistics
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from rapidfuzz.distance import Levenshtein

from common.page_types import is_act_class
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
    repaired: bool = False  # the reply's bare grammar keys were quoted before parsing

    @property
    def entries(self) -> list[dict[str, Any]]:
        return F.answer_entries(self.answer)

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
            repaired=bool(reading.get("answer_repairs")),
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
            repaired=bool(record.get("answer_repairs")),
        )
    return out


def load_answers(path: Path, tree: F.RunTree) -> dict[str, Answer]:
    path = Path(path)
    if is_run_tree(path):
        if path.resolve() == tree.root.resolve():
            return answers_from_run_tree(tree)
        return answers_from_run_tree(load_tree(path))
    return answers_from_cache(path)


def load_tree(path: Path) -> F.RunTree:
    """A run tree loaded as `fed_arm` loads it (a real run, with run.json, has its seals
    proven), so its identity equals the one the caches recorded."""
    return F.load_run_tree(path, sealed=F.is_real_run(path))


def is_run_tree(path: Path) -> bool:
    return (Path(path) / "4_perlector").is_dir()


# What two compared answer sets must share: the same pages asked the same way, so a
# difference between them is the model's. The model, checkpoint and recipe may differ.
COMPARABLE = ("variant", "sampling_name", "seed", "max_tokens", "decoding_sha256", "stream")
# One folder is one model: its pages may not mix these.
MODEL_IDENTITY = ("model_name", "repo", "revision", "recipe")


def cache_setups(folder: Path) -> dict[str, dict[str, Any]] | str:
    """Per page stem, the setup its record was written under; or why a folder has none."""
    out = {}
    for path in sorted(Path(folder).glob("*.json")):
        record = json.loads(path.read_text("utf-8"))
        if record.get("schema") != F.SCHEMA:
            continue
        setup = record.get("setup")
        if not isinstance(setup, dict) or "run" not in setup:
            return f"{path.name} records no setup (written before setups were recorded)"
        out[record["page"]] = setup
    return out


def mutation_of(setup: dict[str, Any]) -> Any:
    return (setup.get("page") or {}).get("mutation")


def comparable_view(setup: dict[str, Any]) -> dict[str, Any]:
    view = {k: setup.get(k) for k in (*COMPARABLE, *MODEL_IDENTITY)}
    view["run"] = setup.get("run")
    return view


def describe_set(
    path: Path, tree: F.RunTree
) -> tuple[str, list[str], dict[str, dict[str, Any]] | None]:
    """(a line for the card, problems, per-page setups or None for a run's own readings)."""
    if is_run_tree(path):
        same = Path(path).resolve() == tree.root.resolve() or load_tree(path).identity() == (
            tree.identity()
        )
        problems = [] if same else [f"{path} is another run tree than --run-tree"]
        return "the run's own first readings", problems, None
    setups = cache_setups(path)
    if isinstance(setups, str):
        return "no setup recorded", [f"{path}: {setups}"], None
    if not setups:
        return "no answers", [f"{path} holds no fed answers"], None
    views = {json.dumps(comparable_view(s), sort_keys=True, default=str) for s in setups.values()}
    first = next(iter(setups.values()))
    problems = []
    if len(views) > 1:
        problems.append(f"{path} mixes setups (model, run, sampling, seed, cap or decoding differ)")
    if first.get("run") != tree.identity():
        problems.append(f"{path} was answered on another run tree than --run-tree")
    line = (
        f"{first.get('repo')}@{first.get('revision')}, recipe {first.get('recipe') or 'none'}, "
        f"served {first.get('model_name')}, sampling {first.get('sampling_name')} "
        f"seed {first.get('seed')}"
    )
    return line, problems, setups


def check_comparable(a: Path, b: Path, tree: F.RunTree) -> tuple[list[str], list[str]]:
    """(problems, the two sets' model lines). Two fed caches must agree on run tree, variant,
    mutation, sampling name, seed, token cap and decoding digest; a run's own readings carry
    no setup, so only their run tree is checked."""
    line_a, problems, setups_a = describe_set(a, tree)
    line_b, problems_b, setups_b = describe_set(b, tree)
    problems += problems_b
    if setups_a is not None and setups_b is not None and not problems:
        one_a, one_b = next(iter(setups_a.values())), next(iter(setups_b.values()))
        for key in COMPARABLE:
            if one_a.get(key) != one_b.get(key):
                problems.append(f"{key} differs between the two sets")
        shared = setups_a.keys() & setups_b.keys()
        if any(mutation_of(setups_a[s]) != mutation_of(setups_b[s]) for s in shared):
            problems.append("mutation differs between the two sets")
    return problems, [line_a, line_b]


# --- per page -----------------------------------------------------------------------


def _pair_gap(reference: list[str], gap: list[str]) -> list[int | None]:
    """One-to-one, order-preserving pairing of a gap's wrong gold words with the words
    written there: least total character distance, an unpaired word costing 1, so as many
    words pair as the shorter side allows. Returns, per gold word, its gap index or None."""
    m, n = len(reference), len(gap)
    cost = [[0.0] * (n + 1) for _ in range(m + 1)]
    for i in range(1, m + 1):
        cost[i][0] = float(i)
    for j in range(1, n + 1):
        cost[0][j] = float(j)
    for i in range(1, m + 1):
        for j in range(1, n + 1):
            pair = cost[i - 1][j - 1] + Levenshtein.normalized_distance(
                reference[i - 1], gap[j - 1]
            )
            cost[i][j] = min(pair, cost[i - 1][j] + 1, cost[i][j - 1] + 1)
    out: list[int | None] = [None] * m
    i, j = m, n
    while i and j:
        pair = cost[i - 1][j - 1] + Levenshtein.normalized_distance(reference[i - 1], gap[j - 1])
        if cost[i][j] == pair:
            out[i - 1] = j - 1
            i, j = i - 1, j - 1
        elif cost[i][j] == cost[i - 1][j] + 1:
            i -= 1
        else:
            j -= 1
    return out


def aligned(reference: tuple[str, ...], hypothesis: tuple[str, ...]):
    """(right per gold token, the word written in a wrong one's place or None, inserted).

    Right and wrong come from the unit-cost edit script, as `roster.align` scores them.
    The wrong gold words between two matched words share the hypothesis words written
    between them (the gap); each written word stands in for at most one gold word, paired
    in order by least character distance (`_pair_gap`), so `Jean Paul` read as `Jeanne`
    gives `Jeanne` for `Jean` and nothing for `Paul`. A copy is judged on this word.
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
    gaps: dict[tuple[int, int], list[int]] = defaultdict(list)
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
        gaps[(lo, hi)].append(i)
    for (lo, hi), members in gaps.items():
        gap = list(hypothesis[lo:hi])
        if gap:
            for i, j in zip(members, _pair_gap([reference[i] for i in members], gap), strict=True):
                if j is not None:
                    form[i] = gap[j]
    return [i not in wrong for i in range(len(reference))], form, inserted


ABSENT = "<absent>"  # a voter that wrote nothing in a gold word's place


def ballot(word: str, right: bool, form: str | None) -> str:
    """What one witness votes for a gold word: the word, its own wrong word, or absent."""
    if right:
        return word
    if form is None:
        return ABSENT
    return form if form != word else f"{form}<misplaced>"


def vote(ballots: list[str], word: str) -> str | None:
    """The plurality of the ballots against the gold word: `right` when the word wins
    alone, `tie` when it shares the top count, `wrong` when it is not among the leaders
    (a tie among wrong words is wrong), None with no voters. The one voting rule of the
    scorecard ("beats the vote") and of `mutations.vote_check` ("voting must lose")."""
    if not ballots:
        return None
    counts = Counter(ballots)
    best = max(counts.values())
    leaders = [b for b, c in counts.items() if c == best]
    if word not in leaders:
        return "wrong"
    return "right" if len(leaders) == 1 else "tie"


def reference_digest(tokens: tuple[str, ...]) -> str:
    """The scored reference's identity: a planted site's word index means nothing against
    another reference (`mutations` records it, `planted_copies` checks it)."""
    return hashlib.sha256("\x1f".join(tokens).encode("utf-8")).hexdigest()


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
        acts = [e.get("text") or "" for e in answer.entries if is_act_class(e.get("kind"))]
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
    row["ref_words"] = ref_tokens
    row["ref_digest"] = reference_digest(ref_tokens)
    row["inserted"] = inserted
    row["reader_right"] = right
    row["reader_form"] = form
    wit = {}
    for w in answer.witnesses:
        w_right, w_form, _ = aligned(ref_tokens, S.tokens(S.normalise_output(w.name, w.text)))
        wit[w.name] = {"right": w_right, "form": w_form, "read": bool(w.text.strip())}
    row["witnesses"] = wit
    return row


def page_counts(row: dict[str, Any]) -> Counter:
    """The follow, copy and vote counts over one page's gold tokens.

    Only witnesses that read the page take part: a witness with no reading (failed,
    empty) is counted under `absent:<name>` per gold word and is never "the only one
    wrong" or "the only one right". Among readers, a lone wrong witness that wrote a
    word of its own in the place is `only-wrong` (the reader resisted it or not); one
    that wrote nothing there is `only-omitted`. "Followed" means the reader has the word
    right where only that witness had it right: agreement, not proof of reliance.
    """
    c: Counter = Counter()
    wit = row["witnesses"]
    readers = [n for n in wit if wit[n]["read"]]
    absent = [n for n in wit if not wit[n]["read"]]
    words = row.get("ref_words")
    for i, r in enumerate(row["reader_right"]):
        right = {n: wit[n]["right"][i] for n in readers}
        k = sum(right.values())
        c["tokens"] += 1
        c["reader-right"] += r
        for name in absent:
            c[f"absent:{name}"] += 1
        if not right:
            c["no-reader"] += 1
        elif k == len(right):
            c["all-right"] += 1
            c["all-right,damaged"] += not r
        elif k == 0:
            c["all-wrong"] += 1
            c["all-wrong,recovered"] += r
        if len(right) >= 2 and k == 1:
            (name,) = [n for n, v in right.items() if v]
            c[f"only-right:{name}"] += 1
            c[f"only-right:{name},followed"] += r
        if len(right) >= 2 and k == len(right) - 1:
            (name,) = [n for n, v in right.items() if not v]
            kind = "only-wrong" if wit[name]["form"][i] is not None else "only-omitted"
            c[f"{kind}:{name}"] += 1
            c[f"{kind}:{name},resisted"] += r
        if right and words is not None:
            outcome = vote(
                [ballot(words[i], right[n], wit[n]["form"][i]) for n in readers], words[i]
            )
            c[f"vote-{outcome}"] += 1
            c["vote-beaten"] += outcome == "wrong" and r
            c["vote-lost"] += outcome == "right" and not r
            c["vote-tie,reader-right"] += outcome == "tie" and r
        mine = row["reader_form"][i]
        copied_any = False
        for name in readers:
            theirs = wit[name]["form"][i]
            if not right[name] and theirs is not None:
                c[f"wrong-form:{name}"] += 1
                if not r and mine == theirs:
                    c[f"wrong-form:{name},copied"] += 1
                    copied_any = True
        if not r:
            c["reader-wrong"] += 1
            c["reader-wrong,a-witness-error"] += copied_any
    return c


def scepticism(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """The follow, copy and vote counts over the gold tokens of these pages, summed from
    each page's `page_counts` (kept per page for the bootstrap)."""
    c: Counter = Counter()
    names: list[str] = []
    pages = []
    for row in rows:
        for name in row["witnesses"]:
            if name not in names:
                names.append(name)
        counts = row.get("counts")
        if counts is None:
            counts = page_counts(row)
        pages.append(counts)
        c.update(counts)
    return {"counts": c, "witnesses": names, "pages": pages}


def reference_problem(sidecar: dict[str, Any], row: dict[str, Any]) -> str | None:
    """Why a sidecar's sites cannot be judged against this row's reference, or None.

    `reference-unbound`: the sidecar names no whole reference; `reference-unchecked`:
    the scorer holds no rebuilt reference for the page (`row["reference"]`, set by
    `scorecard(..., references=)`); `reference-differs`: the whole reference differs
    (other doubt marks, statuses or entries, even with the same words);
    `words-differ`: the scored words differ from this gold's."""
    record = sidecar.get("reference_record_sha256")
    if not record or not sidecar.get("reference_sha256"):
        return "reference-unbound"
    held = row.get("reference")
    if not held:
        return "reference-unchecked"
    if record != held.get("reference_record_sha256"):
        return "reference-differs"
    if sidecar["reference_sha256"] != row.get("ref_digest"):
        return "words-differ"
    return None


def planted_copies(rows: list[dict[str, Any]], answers: dict[str, Answer]) -> dict[str, Any]:
    """Per planted site: resisted (reader right), copied (the reader wrote the planted
    word) or other-wrong; grouped by scenario, by k, by chair (k = 1) and by class.
    Sites are bound to the reference they were planted on: a sidecar whose whole
    reference is not the scorer's (`reference_problem`) has every site `misaligned`,
    and so has a site with no index, an index past the gold's words, or a `ref_word`
    that is not the gold's word at that index; misaligned sites are not judged
    (`misaligned:<why>` says why)."""
    c: Counter = Counter()
    for row in rows:
        answer = answers[row["page"]]
        sidecar = answer.mutation
        if not sidecar or not row["parsed"]:
            continue
        scenario = sidecar.get("scenario", "?")
        problem = reference_problem(sidecar, row)
        words = row.get("ref_words")
        for site in sidecar.get("planted") or []:
            i = site.get("ref_index")
            why = problem or (
                "site"
                if i is None
                or i >= len(row["reader_right"])
                or (words is not None and site.get("ref_word") != words[i])
                else None
            )
            if why:
                c["misaligned"] += 1
                c[f"misaligned:{why}"] += 1
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
        "failed": len(rows) - len(parsed),
        "cer_all_pages": cer_all,
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
        "repaired": sum(a.repaired for a in health),
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


def missing_answer(stem: str) -> Answer:
    """A gold page the answer set has no answer for: a failure, read as empty."""
    return Answer(stem, "missing", ["no-answer"], [], None, None, False, None, None, "no-answer")


def scorecard(
    answers: dict[str, Answer],
    gold: dict,
    hard: set[str],
    expected: set[str] | None = None,
    references: dict[str, dict[str, str]] | None = None,
) -> dict[str, Any]:
    """The card over every gold page that has an answer and, failure-inclusive, every
    `expected` page (default: none) that has none: it counts as an unparsed, empty
    answer (CER 1 in the "all" medians, an error in answer health), so an arm cannot
    look better by failing its hardest pages. `references` (per stem, the digests of
    the reference rebuilt from this gold, `reference_identities`) is what planted sites
    are checked against; without it no planted site is judged."""
    unanswered = sorted(s for s in gold if s not in answers)
    missing = [s for s in unanswered if s in (expected or set())]
    answers = {**answers, **{s: missing_answer(s) for s in missing}}
    rows = [score_page(a, gold[s], s in hard) for s, a in sorted(answers.items()) if s in gold]
    for row in rows:
        row["reference"] = (references or {}).get(row["page"])
        if row["parsed"]:
            row["counts"] = page_counts(row)
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
        "missing": unanswered,
        "answers": answers,
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


# --- page-level uncertainty ---------------------------------------------------------

BOOTSTRAP_REPS = 1000  # resamples of pages; words on one page are not independent
BOOTSTRAP_SEED = 0


def _draws(n: int, reps: int = BOOTSTRAP_REPS, seed: int = BOOTSTRAP_SEED) -> list[list[int]]:
    rng = random.Random(seed)
    return [rng.choices(range(n), k=n) for _ in range(reps)]


def _interval(values: list[float]) -> tuple[float, float] | None:
    values = sorted(v for v in values if v is not None)
    if len(values) < 20:
        return None
    return values[int(0.025 * len(values))], values[int(0.975 * len(values)) - 1]


def boot_ratio(pages: list[Counter], num: str, den: str) -> tuple[float, float] | None:
    """A 95% percentile interval for sum(num)/sum(den), resampling whole pages."""
    if len(pages) < 2 or not sum(p[den] for p in pages):
        return None
    out = []
    for draw in _draws(len(pages)):
        d = sum(pages[i][den] for i in draw)
        out.append(sum(pages[i][num] for i in draw) / d if d else None)
    return _interval(out)


def boot_median(values: list[float]) -> tuple[float, float] | None:
    if len(values) < 2:
        return None
    return _interval([statistics.median(values[i] for i in draw) for draw in _draws(len(values))])


def boot_paired(
    a: list[Counter], b: list[Counter], num: str, den: str
) -> tuple[float | None, tuple[float, float] | None]:
    """The paired difference B - A of sum(num)/sum(den) over the same pages, and its
    95% interval resampling pages (both answer sets take the same draw)."""

    def rate(pages, draw):
        d = sum(pages[i][den] for i in draw)
        return sum(pages[i][num] for i in draw) / d if d else None

    every = list(range(len(a)))
    ra, rb = rate(a, every), rate(b, every)
    if ra is None or rb is None:
        return None, None
    diffs = []
    for draw in _draws(len(a)) if len(a) >= 2 else []:
        x, y = rate(a, draw), rate(b, draw)
        diffs.append(None if x is None or y is None else y - x)
    return rb - ra, _interval(diffs)


def _ci(interval: tuple[float, float] | None, digits: int = 2) -> str:
    return "" if interval is None else f"; {interval[0]:.{digits}f}–{interval[1]:.{digits}f}"


def _share_ci(pages: list[Counter], num: str, den: str) -> str:
    n, d = sum(p[num] for p in pages), sum(p[den] for p in pages)
    if not d:
        return "–"
    return f"{n} / {d} ({n / d:.2f}{_ci(boot_ratio(pages, num, den))})"


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
        "CER median (all) counts every failed or missing answer as an empty reading; the "
        "range after it is a 95% interval from resampling pages.",
        "",
        "| group | pages | parsed | CER median (parsed) | CER mean (parsed) | CER median (all) |",
        "|---|---:|---:|---:|---:|---:|",
    ]
    for name, s in _blocks(card):
        out.append(
            f"| {name} | {s['pages']} | {s['parsed']} | {_f(s['cer_median_parsed'])} | "
            f"{_f(s['cer_mean_parsed'])} | {_f(s['cer_median_all'])}"
            f"{_ci(boot_median(s['cer_all_pages']), 3)} |"
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
        f"- repaired before parsing (bare grammar keys quoted): {s['repaired']} pages",
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
        "Only witnesses that read the page take part; a failed or empty witness is counted "
        'apart (absent), never as the only one wrong. "Only it wrong" needs the witness '
        "to have written a wrong word there (a word left out is counted apart). "
        '"Followed" means the reader has the word right where only that witness had it: '
        "agreement, not proof of reliance. The vote is a plurality of the witnesses that "
        "read; a tie that includes the right word is reported apart. Ranges are 95% "
        "intervals from resampling pages, not words.",
        "",
    ]
    for name, summ in _blocks(card):
        sc = summ["scepticism"]
        c, names, pages = sc["counts"], sc["witnesses"], sc["pages"]
        if not c["tokens"]:
            continue
        out += [
            f"**{name}**: {summ['parsed']} of {summ['pages']} pages parsed, {c['tokens']} gold "
            f"words, reader right {_share_ci(pages, 'reader-right', 'tokens')}; all witnesses "
            f"wrong, recovered {_share_ci(pages, 'all-wrong,recovered', 'all-wrong')}; all "
            f"right, damaged {_share(c['all-right,damaged'], c['all-right'])}; beats the vote "
            f"{c['vote-beaten']} - {c['vote-lost']} = {c['vote-beaten'] - c['vote-lost']:+d} "
            f"({_f(_ratio(c['vote-beaten'] - c['vote-lost'], c['tokens']) * 1000 if c['tokens'] else None, 1)}"
            f" per 1,000 words), vote tied {c['vote-tie']} (reader right "
            f"{c['vote-tie,reader-right']}); reader errors that are a witness's error "
            f"{_share(c['reader-wrong,a-witness-error'], c['reader-wrong'])}",
            "",
            "| witness | only it right → followed | only it wrong → resisted | its wrong word copied "
            "| only it left the word out → reader right | absent (gold words) |",
            "|---|---:|---:|---:|---:|---:|",
        ]
        for w in names:
            out.append(
                f"| {w} | {_share_ci(pages, f'only-right:{w},followed', f'only-right:{w}')} | "
                f"{_share_ci(pages, f'only-wrong:{w},resisted', f'only-wrong:{w}')} | "
                f"{_share_ci(pages, f'wrong-form:{w},copied', f'wrong-form:{w}')} | "
                f"{_share(c[f'only-omitted:{w},resisted'], c[f'only-omitted:{w}'])} | "
                f"{c[f'absent:{w}']} |"
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
        why = ", ".join(
            f"{k.split(':', 1)[1]} {v}" for k, v in sorted(p.items()) if k.startswith("misaligned:")
        )
        out.append(
            "\nSites not judged (not bound to this reference: the whole reference, its "
            f"words or the site's word differs, or no reference to check): {p['misaligned']}"
            + (f" ({why})" if why else "")
        )
    return out + [""]


PAIRED_RATES = (
    ("reader right", "reader-right", "tokens"),
    ("all witnesses wrong, recovered", "all-wrong,recovered", "all-wrong"),
)


def paired_report(card_a, card_b, names: tuple[str, str]) -> list[str]:
    """Both answer sets on the same pages: those both parsed. Failures are counted on
    each side first (failure-inclusive coverage), then each rate is compared page for
    page with a 95% interval for the difference from resampling pages."""
    a, b = names
    rows_a = {r["page"]: r for r in card_a["rows"] if r["group"] != G.TEST}
    rows_b = {r["page"]: r for r in card_b["rows"] if r["group"] != G.TEST}
    common = sorted(set(rows_a) & set(rows_b))
    both = [p for p in common if rows_a[p]["parsed"] and rows_b[p]["parsed"]]
    only_a = sum(rows_a[p]["parsed"] and not rows_b[p]["parsed"] for p in common)
    only_b = sum(rows_b[p]["parsed"] and not rows_a[p]["parsed"] for p in common)
    neither = len(common) - len(both) - only_a - only_b
    out = [
        "### Paired pages (both answers parsed)",
        "",
        f"Pages in both sets {len(common)}: both parsed {len(both)}, only {a} parsed "
        f"{only_a}, only {b} parsed {only_b}, neither {neither}. A rate below is over the "
        "paired pages only; the difference is per page, with a 95% interval from resampling "
        "pages.",
        "",
        f"| measure | {a} | {b} | {b} - {a} |",
        "|---|---:|---:|---:|",
    ]
    if not both:
        return [*out, "", "No page parsed in both sets.", ""]
    pa = [rows_a[p]["counts"] for p in both]
    pb = [rows_b[p]["counts"] for p in both]
    cer_a = [rows_a[p]["cer"] for p in both if rows_a[p]["cer"] is not None]
    cer_b = [rows_b[p]["cer"] for p in both if rows_b[p]["cer"] is not None]
    diffs = [
        rows_b[p]["cer"] - rows_a[p]["cer"] for p in both if rows_a[p]["cer"] is not None
    ]  # fmt: skip
    if diffs:
        mean_ci = None
        if len(diffs) >= 2:
            mean_ci = _interval([statistics.fmean(diffs[i] for i in d) for d in _draws(len(diffs))])
        out.append(
            f"| CER mean per page | {_f(statistics.fmean(cer_a))} | {_f(statistics.fmean(cer_b))} | "
            f"{statistics.fmean(diffs):+.3f}{_ci(mean_ci, 3)} |"
        )
    measures = list(PAIRED_RATES)
    for w in card_a["all"]["scepticism"]["witnesses"]:
        measures += [
            (f"only {w} right, followed", f"only-right:{w},followed", f"only-right:{w}"),
            (f"only {w} wrong, resisted", f"only-wrong:{w},resisted", f"only-wrong:{w}"),
            (f"{w}'s wrong word copied", f"wrong-form:{w},copied", f"wrong-form:{w}"),
        ]
    for title, num, den in measures:
        diff, interval = boot_paired(pa, pb, num, den)
        ra = _ratio(sum(c[num] for c in pa), sum(c[den] for c in pa))
        rb = _ratio(sum(c[num] for c in pb), sum(c[den] for c in pb))
        if ra is None and rb is None:
            continue
        shown = "–" if diff is None else f"{diff:+.2f}{_ci(interval)}"
        out.append(f"| {title} | {_f(ra, 2)} | {_f(rb, 2)} | {shown} |")
    return [*out, ""]


def compare_report(card_a, card_b, inv, names: tuple[str, str], label: str) -> list[str]:
    a, b = names
    out = [f"## {a} vs {b}{label}", ""]
    out += paired_report(card_a, card_b, names)
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
    out += [
        "",
        "Each set on its own pages (parsed pages; CER (all) counts failures as empty):",
        "",
        "| measure | group | " + a + " | " + b + " |",
        "|---|---|---:|---:|",
    ]
    for group in [g for g in card_a["groups"] if g in card_b["groups"]] + ["all"]:
        sa = card_a["all"] if group == "all" else card_a["groups"][group]
        sb = card_b["all"] if group == "all" else card_b["groups"][group]
        ca, cb = sa["scepticism"]["counts"], sb["scepticism"]["counts"]
        out.append(
            f"| CER median (parsed) | {group} | {_f(sa['cer_median_parsed'])} | "
            f"{_f(sb['cer_median_parsed'])} |"
        )
        out.append(
            f"| CER median (all) | {group} | {_f(sa['cer_median_all'])} | "
            f"{_f(sb['cer_median_all'])} |"
        )
        out.append(f"| pages parsed | {group} | {sa['parsed']} / {sa['pages']} | "
                   f"{sb['parsed']} / {sb['pages']} |")  # fmt: skip
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
            **{k: v for k, v in summary.items() if k not in ("scepticism", "cer_all_pages")},
            "scepticism": {"witnesses": sc["witnesses"], "counts": dict(sc["counts"])},
        }

    rows = [
        {
            k: v
            for k, v in r.items()
            if k not in ("reader_right", "reader_form", "witnesses", "ref_words", "counts")
        }
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


_CHECKED_STATUS = re.compile(
    r"lead-checked|gold\s*\(\s*[^()\s][^()]*?\s+(\d{4}-\d{2}-\d{2})\s*\)", re.IGNORECASE
)


def reference_identities(
    gold: dict, tree: F.RunTree, row_kind: str = "other"
) -> dict[str, dict[str, str]]:
    """Per gold page the run has, the digests of the reference `mutations` plants on:
    built from the gold page (`reference_from_gold`) with statuses from the run's own
    feed (`statuses_from_agreement`). A page whose gold cannot be made a reference has
    none (its planted sites are not judged)."""
    from operations.bakeoff import mutations as M

    by_stem = tree.by_stem()
    refs = {}
    for stem, page in gold.items():
        if stem not in by_stem:
            continue
        try:
            ref = M.reference_from_gold(page, row_kind)
        except ValueError:
            continue
        M.statuses_from_agreement(ref, by_stem[stem].feed)
        refs[stem] = ref
    return M.reference_identities(refs)


def is_checked_status(status: str) -> bool:
    """A gold STATUS that says a person checked every word: `gold (<who> <date>)` with the
    checker's name and an ISO date, or `lead-checked`. Anything else (bare `gold`, `gold
    (unchecked draft)`, fool's gold, silver, draft, empty, unknown) is not."""
    found = _CHECKED_STATUS.fullmatch(status.strip())
    if found is None:
        return False
    if found.group(1) is None:  # lead-checked
        return True
    try:
        datetime.date.fromisoformat(found.group(1))
    except ValueError:
        return False
    return True


def reference_label(golds: list) -> str:
    """The card's label: lead-checked only when every scored gold page, in both compared
    sets, has an explicit checked status; fool's gold when any says so; else unchecked."""
    if golds and all(is_checked_status(g.status) for g in golds):
        return " (lead-checked gold)"
    if not golds or any("fool" in g.status.lower() for g in golds):
        return f" {S.FOOLS_GOLD_LABEL}"
    return " vs an unchecked reference (ballpark, not accuracy)"


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
    parser.add_argument(
        "--row-kind", default="other", help="entry kind for index rows, as the mutations used"
    )
    args = parser.parse_args(argv)

    tree = load_tree(args.run_tree)
    gold = load_gold_dir(args.gold, args.gold_glob)
    if args.exclude:
        for stem in args.exclude.read_text("utf-8").split():
            gold.pop(stem, None)
    hard = set(args.hard_pages.read_text("utf-8").split()) if args.hard_pages else set()
    answers = load_answers(args.answers or args.run_tree, tree)
    names = (args.names or f"{(args.answers or args.run_tree).name},"
             f"{args.compare.name if args.compare else ''}").split(",")  # fmt: skip
    model_lines = None
    if args.compare:
        problems, model_lines = check_comparable(args.answers or args.run_tree, args.compare, tree)
        if problems:
            parser.exit(
                2,
                "refusing to compare answer sets that are not alike: "
                + "; ".join(problems)
                + "\nOnly the model, checkpoint and recipe may differ.\n",
            )
    other = load_answers(args.compare, tree) if args.compare else None
    # Every page of the run with gold is expected: one with no answer is a failure.
    expected = set(tree.by_stem()) & set(gold)
    scored = [g for s, g in gold.items() if s in expected or s in answers]
    if other is not None:
        scored += [g for s, g in gold.items() if s in other]
    label = reference_label(scored)
    fools = label != " (lead-checked gold)"
    references = reference_identities(gold, tree, args.row_kind)
    card = scorecard(answers, gold, hard, expected, references)
    lines = [f"# Perlector scorecard{label}", ""]
    if fools:
        lines += [
            "The reference is not lead-checked on every scored page (an unchecked AI draft or "
            "another unchecked status): every number is a ballpark against it, not an accuracy.",
            "",
        ]
    if model_lines:
        lines += [f"- {n}: {m}" for n, m in zip(names, model_lines, strict=True)] + [""]
    lines += report(card, f"{names[0]}: {len(answers)} answers", label)
    out_json: dict[str, Any] = {"label": label.strip(), "answers": _jsonable(card)}
    if other is not None:
        card_b = scorecard(other, gold, hard, expected, references)
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
