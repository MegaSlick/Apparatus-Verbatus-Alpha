"""Whether a page-read run read every RecordGold record exactly once.

Reads a run tree read with `reading_unit = "page"` -- the Perlector's
`page-feed`, `page-reading`, `act-region`, `perlectio` (`perlectio.v2`) and
`page-accounting` records -- beside the admitted RecordGold records of its
pages, and counts, per gold record, how many `act` regions hold at least half
of it (0, 1, 2+) and whether its text appears in their readings. A record is
read exactly once when one act region holds it and its text was read there;
lost when no act region holds it or its text was not read; duplicated when two
or more hold it and its text was read.

Beside that: the records that sit under one witness unit that also covers
another record (a detector record or a layout block that merged two entries),
how often the accounting's rule (i) `merged-detection` fired against the act
regions that truly hold two or more gold records, which rules held the pages
where a record was lost, the hold codes, admitted prompt tokens against the
engine's `usage.prompt_tokens`, the `length` finish rate, time per page where
the caller supplies it, and whether each page would fit a 65,536-token context.

The report holds counts and identifiers only, never text. It chooses nothing:
it runs after the tree is sealed and returns nothing to the pipeline.

The gate: at least 95% of gold records read exactly once, and no record lost
on a page nothing held.
"""

from __future__ import annotations

import json
import statistics
import sys
from collections import Counter
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any, Final

from common.contracts.canonical import digest_bytes, is_sha256
from common.contracts.stages import PERLECTOR
from common.page_accounting import (
    DEFAULT_PAGE_ACCOUNTING_CONFIG_PATH,
    HOLD,
    MERGED_DETECTION,
    NOT_MEASURED,
    PageAccountingPolicy,
    is_inside,
    load_page_accounting_policy,
    unread_characters,
)
from common.runtree.store import RunTree

from . import CorpusRefusal
from .compare import ReadOnlyRunTree, load_exemplar_page_shas

SCHEMA: Final = "exactly-once-report.v1"
GATE_EXACTLY_ONCE_BP: Final = 9_500
FIT_CONTEXT_TOKENS: Final = 65_536
BASIS_POINTS: Final = 10_000
PAGE_KINDS: Final = ("page-feed", "page-reading", "act-region", "perlectio", "page-accounting")
PERLECTIO_V2: Final = "perlectio.v2"
DETECTOR_RECORD: Final = "detector-record"

EXACTLY_ONCE_REFUSAL_REASONS: Final = frozenset(
    {"malformed-record", "not-page-read", "policy-mismatch", "missing-file"}
)


class Refusal(CorpusRefusal):
    reasons = EXACTLY_ONCE_REFUSAL_REASONS


# --- inputs ----------------------------------------------------------------------------


def gold_records(
    gold_rows: Sequence[Mapping[str, Any]], ledger_rows: Sequence[Mapping[str, Any]]
) -> list[dict[str, Any]]:
    """Admitted RecordGold records as `{record_id, page_sha256, box_px, text}`.

    The admission ledger gives each admitted record its page digest and its box
    in the stored page's frame (`bbox = [x, y, w, h]`); `gold.jsonl` gives its
    text. A ledger record with no gold row is refused by name.
    """
    texts: dict[str, str] = {}
    for row in gold_rows:
        record_id, text = row.get("record_id"), row.get("text")
        if not isinstance(record_id, str) or not isinstance(text, str):
            raise Refusal("malformed-record: a gold row carries no record_id or text")
        texts[record_id] = text
    records = []
    for row in ledger_rows:
        if row.get("decision") != "admitted":
            continue
        record_id, page_sha256, bbox = row["record_id"], row["page_sha256"], row["bbox"]
        if record_id not in texts:
            raise Refusal(f"malformed-record: admitted record {record_id!r} has no gold row")
        if not is_sha256(page_sha256):
            raise Refusal(f"malformed-record: admitted record {record_id!r} has no page digest")
        if (
            not isinstance(bbox, list)
            or len(bbox) != 4
            or any(not isinstance(v, int) or isinstance(v, bool) for v in bbox)
            or bbox[2] <= 0
            or bbox[3] <= 0
        ):
            raise Refusal(
                f"malformed-record: admitted record {record_id!r} bbox is not [x, y, w, h]"
            )
        x, y, w, h = bbox
        records.append(
            {
                "record_id": record_id,
                "page_sha256": page_sha256,
                "box_px": [x, y, x + w, y + h],
                "text": texts[record_id],
            }
        )
    return sorted(records, key=lambda record: record["record_id"])


def _read_ref_json(tree: RunTree | ReadOnlyRunTree, ref: Any) -> dict[str, Any]:
    if not isinstance(ref, dict) or "relative_path" not in ref or "sha256" not in ref:
        raise Refusal(f"malformed-record: {ref!r} is not a digest-checked reference")
    body = tree.read_bytes(ref["relative_path"])
    if digest_bytes(body) != ref["sha256"]:
        raise Refusal(f"malformed-record: {ref['relative_path']} does not match its digest")
    return json.loads(body)


def load_page_records(tree: RunTree | ReadOnlyRunTree) -> list[dict[str, Any]]:
    """The Perlector's page records of a page-read tree, grouped by page, read-only.

    Each page is `{page_sha256, feed, reading, act_regions, perlectios,
    accounting, usage}`: payloads as published; `usage` is the engine's usage
    from the page reading's call record, or `None` without a call. A tree with
    no page feed, or a feed not read by page, is refused `not-page-read`.
    """
    shas = load_exemplar_page_shas(tree)
    by_kind: dict[str, list[dict[str, Any]]] = {kind: [] for kind in PAGE_KINDS}
    for entry in tree.build_manifest(PERLECTOR)["artifacts"]:
        if entry["kind"] in by_kind:
            record = tree.read_artifact(PERLECTOR, entry["kind"], entry["artifact_id"])
            by_kind[entry["kind"]].append({"subject_id": record["subject_id"], **record})
    if not by_kind["page-feed"]:
        raise Refusal("not-page-read: the Perlector published no page-feed record")

    pages: dict[str, dict[str, Any]] = {}
    for record in by_kind["page-feed"]:
        feed = record["payload"]
        if feed.get("reading_unit") != "page":
            raise Refusal(f"not-page-read: page feed {record['subject_id']!r} is not read by page")
        ordinal = feed["page_ordinal"]
        if ordinal not in shas:
            raise Refusal(f"malformed-record: page ordinal {ordinal} has no sealed Exemplar page")
        pages[feed["page_id"]] = {
            "page_sha256": shas[ordinal],
            "feed": feed,
            "reading": None,
            "act_regions": [],
            "perlectios": [],
            "accounting": None,
            "usage": None,
        }

    def page_of(payload: Mapping[str, Any], what: str) -> dict[str, Any]:
        page = pages.get(payload.get("page_id"))
        if page is None:
            raise Refusal(f"malformed-record: a {what} names a page with no page feed")
        return page

    for record in by_kind["page-reading"]:
        page = page_of(record["payload"], "page reading")
        page["reading"] = record["payload"]
        engine_call = record["payload"].get("engine_call")
        if engine_call is not None:
            page["usage"] = _read_ref_json(tree, engine_call["call_record_ref"]).get("usage")
    act_pages: dict[str, dict[str, Any]] = {}
    for record in by_kind["act-region"]:
        page = page_of(record["payload"], "act region")
        page["act_regions"].append(record["payload"])
        act_pages[record["subject_id"]] = page
    for record in by_kind["perlectio"]:
        if record["payload"].get("schema") != PERLECTIO_V2:
            continue
        page = act_pages.get(record["subject_id"])
        if page is None:
            raise Refusal(f"malformed-record: perlectio {record['subject_id']!r} has no act region")
        page["perlectios"].append(record["payload"])
    for record in by_kind["page-accounting"]:
        page_of(record["payload"], "page accounting")["accounting"] = record["payload"]
    return [pages[page_id] for page_id in sorted(pages)]


# --- the report ------------------------------------------------------------------------


def _bucket(count: int) -> str:
    return "2+" if count > 1 else str(count)


def _page_held(page: Mapping[str, Any]) -> bool:
    accounting, reading = page["accounting"], page["reading"]
    if accounting is None or reading is None:
        return True
    return bool(accounting["holds"]) or reading.get("disposition") != "read"


def _holding_rules(page: Mapping[str, Any]) -> list[str]:
    accounting = page["accounting"]
    if accounting is None:
        return ["no-page-accounting"]
    return sorted(
        name for name, rule in accounting["rules"].items() if rule["status"] in (HOLD, NOT_MEASURED)
    )


def _witness_classes(page: Mapping[str, Any]) -> list[tuple[str, list[int]]]:
    """Every boxed witness unit on the page as (class, box): `detector-record` or its witness."""
    units = []
    for witness in page["feed"]["witnesses"]:
        for unit in witness["units"]:
            if unit.get("box_px") is None:
                continue
            label = DETECTOR_RECORD if unit.get("detector_record") else witness["witness_label"]
            units.append((label, unit["box_px"]))
    return units


def _share(numerator: int, denominator: int) -> int | None:
    return None if denominator == 0 else numerator * BASIS_POINTS // denominator


def exactly_once_report(
    pages: Sequence[Mapping[str, Any]],
    gold: Sequence[Mapping[str, Any]],
    *,
    policy: PageAccountingPolicy,
    seconds_per_page: Mapping[str, float] | None = None,
) -> dict[str, Any]:
    """The `exactly-once-report.v1` body for page records against gold records.

    `pages` as `load_page_records` returns them; `gold` as `gold_records`
    returns them; `seconds_per_page` is `{page_id: seconds}` from outside the
    tree, which records no durations. A page accounting sealed under another
    policy is refused, so "inside" means one thing throughout.
    """
    for page in pages:
        accounting = page["accounting"]
        if accounting is not None and accounting["policy_sha256"] != policy.sha256:
            raise Refusal(
                f"policy-mismatch: page {page['feed']['page_id']!r} was accounted under another "
                "page-accounting policy"
            )
    by_sha = {page["page_sha256"]: page for page in pages}
    gold_by_page: dict[str, list[Mapping[str, Any]]] = {}
    for record in gold:
        gold_by_page.setdefault(record["page_sha256"], []).append(record)

    rows = []
    for record in gold:
        page = by_sha.get(record["page_sha256"])
        if page is None:
            rows.append(
                {
                    "record_id": record["record_id"],
                    "page_id": None,
                    "act_regions": "0",
                    "text": "page-not-read",
                    "outcome": "lost",
                    "page_held": False,
                    "holding_rules": [],
                    "merge_classes": [],
                }
            )
            continue
        box = record["box_px"]
        holding = [
            region["n"]
            for region in page["act_regions"]
            if region["kind"] == "act"
            and region["union_box_px"] is not None
            and is_inside(box, [region["union_box_px"]], policy)
        ]
        # As in rule (e): compared with the readings of the regions holding the
        # record, so a neighbour's formula cannot stand in for it; with every
        # reading when no region holds it.
        scope = set(holding) or {perlectio["n"] for perlectio in page["perlectios"]}
        readings = " ".join(
            perlectio["text"]
            for perlectio in sorted(page["perlectios"], key=lambda p: p["n"])
            if perlectio["n"] in scope
        )
        unread = unread_characters(record["text"], readings, policy)
        text = (
            "not-measured"
            if unread is None
            else "read"
            if unread <= policy.max_unread_characters
            else "not-read"
        )
        others = [other for other in gold_by_page[record["page_sha256"]] if other is not record]
        merge_classes = sorted(
            {
                label
                for label, unit_box in _witness_classes(page)
                if is_inside(box, [unit_box], policy)
                and any(is_inside(other["box_px"], [unit_box], policy) for other in others)
            }
        )
        outcome = (
            "lost"
            if not holding or text != "read"
            else "exactly-once"
            if len(holding) == 1
            else "duplicated"
        )
        held = _page_held(page)
        rows.append(
            {
                "record_id": record["record_id"],
                "page_id": page["feed"]["page_id"],
                "act_regions": _bucket(len(holding)),
                "text": text,
                "outcome": outcome,
                "page_held": held,
                "holding_rules": _holding_rules(page) if held and outcome != "exactly-once" else [],
                "merge_classes": merge_classes,
            }
        )

    outcomes = Counter(row["outcome"] for row in rows)
    exactly = outcomes["exactly-once"]
    lost = [row for row in rows if row["outcome"] == "lost"]
    uncaught = [row for row in lost if not row["page_held"]]
    caught_by_rule = Counter(rule for row in lost for rule in row["holding_rules"])
    duplicated_uncaught = sum(
        1 for row in rows if row["outcome"] == "duplicated" and not row["page_held"]
    )

    merge_split: dict[str, dict[str, int]] = {}
    for row in rows:
        for label in row["merge_classes"] or ["no-merge"]:
            bucket = merge_split.setdefault(label, {"records": 0, "exactly_once": 0})
            bucket["records"] += 1
            bucket["exactly_once"] += int(row["outcome"] == "exactly-once")

    rule_i = Counter()
    hold_codes: Counter[str] = Counter()
    parse_states: Counter[str] = Counter()
    finish_length = with_reading = 0
    tokens = []
    fits = Counter()
    for page in pages:
        reading, accounting = page["reading"], page["accounting"]
        if reading is not None:
            with_reading += 1
            parse_states[reading["parse_state"]] += 1
            finish_length += int(reading.get("finish_reason") == "length")
            capacity = reading.get("capacity")
            if isinstance(capacity, dict):
                admitted = capacity["image_prompt_tokens"] + capacity["prompt_tokens"]
                fits["fits" if capacity["need"] <= FIT_CONTEXT_TOKENS else "does-not-fit"] += 1
                engine = (page["usage"] or {}).get("prompt_tokens")
                if isinstance(engine, int):
                    tokens.append((admitted, engine))
        if accounting is None:
            continue
        hold_codes.update(accounting["holds"])
        fired = {
            finding["n"]
            for finding in accounting["rules"]["i"]["findings"]
            if finding["code"] == MERGED_DETECTION
        }
        page_gold = gold_by_page.get(page["page_sha256"], [])
        for region in page["act_regions"]:
            if region["kind"] != "act" or region["union_box_px"] is None:
                continue
            true_merge = (
                sum(
                    is_inside(record["box_px"], [region["union_box_px"]], policy)
                    for record in page_gold
                )
                > 1
            )
            rule_i[("fired" if region["n"] in fired else "silent", true_merge)] += 1

    seconds = sorted((seconds_per_page or {}).values())
    exactly_bp = _share(exactly, len(rows))
    return {
        "schema": SCHEMA,
        "policy_sha256": policy.sha256,
        "gate": {
            "exactly_once_bp": exactly_bp,
            "required_bp": GATE_EXACTLY_ONCE_BP,
            "uncaught_losses": len(uncaught),
            "passed": exactly_bp is not None
            and exactly_bp >= GATE_EXACTLY_ONCE_BP
            and not uncaught,
        },
        "records": {
            "total": len(rows),
            "exactly_once": exactly,
            "by_act_regions": dict(sorted(Counter(row["act_regions"] for row in rows).items())),
            "by_text": dict(sorted(Counter(row["text"] for row in rows).items())),
            "by_outcome": dict(sorted(outcomes.items())),
            "duplicated_on_pages_not_held": duplicated_uncaught,
            "lost_caught_by_rule": dict(sorted(caught_by_rule.items())),
            "uncaught_record_ids": [row["record_id"] for row in uncaught],
            "by_merge_class": dict(sorted(merge_split.items())),
        },
        "merged_detection": {
            "fired_on_true_merge": rule_i[("fired", True)],
            "fired_on_single_record": rule_i[("fired", False)],
            "silent_on_true_merge": rule_i[("silent", True)],
            "silent_on_single_record": rule_i[("silent", False)],
        },
        "pages": {
            "total": len(pages),
            "by_parse_state": dict(sorted(parse_states.items())),
            "hold_codes": dict(sorted(hold_codes.items())),
            "finish_length_bp": _share(finish_length, with_reading),
            "fit_65536": dict(sorted(fits.items())),
            "prompt_tokens": {
                "compared": len(tokens),
                "admitted_total": sum(a for a, _ in tokens),
                "engine_total": sum(e for _, e in tokens),
                "engine_over_admitted": sum(1 for a, e in tokens if e > a),
                "largest_engine_excess": max((e - a for a, e in tokens), default=None),
            },
            "seconds_per_page": {
                "measured": len(seconds),
                "median": statistics.median(seconds) if seconds else None,
                "max": seconds[-1] if seconds else None,
            },
        },
        "rows": rows,
    }


def summary_lines(report: Mapping[str, Any]) -> list[str]:
    """A short, count-only summary of a report."""
    gate, records, rule_i, pages = (
        report["gate"],
        report["records"],
        report["merged_detection"],
        report["pages"],
    )
    return [
        f"gate: {'PASS' if gate['passed'] else 'FAIL'}  exactly once "
        f"{records['exactly_once']}/{records['total']} ({gate['exactly_once_bp']} bp, "
        f"need {gate['required_bp']}); uncaught losses {gate['uncaught_losses']}",
        f"act regions per record: {records['by_act_regions']}; text: {records['by_text']}",
        f"lost records caught by rule: {records['lost_caught_by_rule']}",
        f"by merge class: {records['by_merge_class']}",
        f"merged-detection: fired on true merge {rule_i['fired_on_true_merge']}, on single "
        f"record {rule_i['fired_on_single_record']}; silent on true merge "
        f"{rule_i['silent_on_true_merge']}",
        f"pages {pages['total']}: {pages['by_parse_state']}; holds {pages['hold_codes']}",
        f"finish length {pages['finish_length_bp']} bp; fit 65,536 {pages['fit_65536']}; "
        f"prompt tokens {pages['prompt_tokens']}; seconds/page {pages['seconds_per_page']}",
    ]


# --- command line ----------------------------------------------------------------------


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.is_file():
        raise Refusal(f"missing-file: {path} is not a file")
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]


def main(argv: list[str] | None = None) -> int:
    import argparse

    from .local_admission import load_local_admission_ledger

    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--run-root", type=Path, required=True, help="the runs directory")
    parser.add_argument("--run-id", required=True, help="a sealed run read by page")
    parser.add_argument("--gold", type=Path, required=True, help="the set's gold.jsonl")
    parser.add_argument("--ledger", type=Path, required=True, help="its local admission ledger")
    parser.add_argument("--out", type=Path, required=True, help="where the JSON report goes")
    parser.add_argument(
        "--seconds-per-page", type=Path, help="optional JSON {page_id: seconds} from the pod log"
    )
    parser.add_argument(
        "--page-accounting-config", type=Path, default=DEFAULT_PAGE_ACCOUNTING_CONFIG_PATH
    )
    args = parser.parse_args(argv)

    policy = load_page_accounting_policy(args.page_accounting_config)
    ledger = load_local_admission_ledger(args.ledger)
    gold = gold_records(_read_jsonl(args.gold), ledger["rows"])
    pages = load_page_records(ReadOnlyRunTree(RunTree(args.run_root, args.run_id)))
    seconds = (
        json.loads(args.seconds_per_page.read_text(encoding="utf-8"))
        if args.seconds_per_page
        else None
    )
    report = exactly_once_report(pages, gold, policy=policy, seconds_per_page=seconds)
    args.out.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    for line in summary_lines(report):
        print(line)
    return 0 if report["gate"]["passed"] else 1


if __name__ == "__main__":
    sys.exit(main())
