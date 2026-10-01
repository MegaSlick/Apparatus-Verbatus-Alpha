"""Whether a page-read run read every RecordGold record exactly once.

Reads a run tree read with `reading_unit = "page"` -- the Perlector's
`page-feed`, `page-reading`, `act-region`, `perlectio` (`perlectio.v2`) and
`page-accounting` records -- beside the admitted RecordGold records of its
pages, and gives each gold record one outcome:

- exactly once: one `act` region holds at least half of it, that region holds
  no other gold record, and its text is read there;
- merged: an act region holding it holds at least half of another gold record
  too, and its text is read -- two acts read as one;
- duplicated: two or more act regions hold it and its text is read;
- lost: no act region holds it, or its text is not read in any that does.

"Text read" is this tool's own, stricter measure, independent of the
accounting's rule (e): the character error rate of the gold text against the
best-matching substring of a holding act's reading is at most
`MAX_GOLD_CER_BP`, and no other gold record on the page is closer to that
reading -- a reading of the neighbouring record is not a reading of this one.
Rule (e) passes readings at 30% error; the proof asks more.

A failure (lost or merged) is caught when a held finding of the page's
accounting is located on it: names a placed region that overlaps it, carries a
box that overlaps it, or names a unit cited by such a region. Two weaker
catches are reported beside it and not credited: page-wide (a held finding
that names no region, box or unit: an incomplete answer, unread ink, a
measurement not taken) and unplaced-only (one that reaches the record only
through an unplaced region, which may be anywhere). A failure with no located
catch is uncaught, and a page with no accounting is `unchecked`, its own
failure, never held and caught.

Beside that: records under one witness unit or detector record that also
covers another record, how often `merged-detection` fired on regions that
truly hold two gold records, the hold codes, admitted prompt tokens against the
engine's `usage.prompt_tokens`, the `length` finish rate, time per page where
the caller supplies it, and whether each page would fit a 65,536-token context.
Rule (i) cannot see a detector record that itself merged two entries when the
reader read them as one act; the merged outcome here is what measures it.

The report holds counts and identifiers only, never text. It chooses nothing:
it runs after the tree is sealed and returns nothing to the pipeline.

The gate: at least 95% of gold records read exactly once, no failure without
a located catch and no page unchecked.
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
    HOLD_CODES,
    MERGED_DETECTION,
    SEALED_CONFIG_NAME,
    PageAccountingPolicy,
    best_substring_distance,
    is_inside,
    load_page_accounting_policy,
    normalized_text,
)
from common.page_accounting import SCHEMA as PAGE_ACCOUNTING_SCHEMA
from common.page_feed import SCHEMA as PAGE_FEED_SCHEMA
from common.page_path import ACT_REGION_SCHEMA, PAGE_READING_SCHEMA
from common.runtree.store import RunTree
from common.stage import run_sealed_config_digests

from . import CorpusRefusal
from .compare import ReadOnlyRunTree, load_exemplar_page_shas

SCHEMA: Final = "exactly-once-report.v1"
GATE_EXACTLY_ONCE_BP: Final = 9_500
# A gold record's text is read when its character error rate against the best
# holding act's reading is at most this (basis points): stricter than the
# accounting's rule (e), which passes correct readings at 30% error.
MAX_GOLD_CER_BP: Final = 2_000
FIT_CONTEXT_TOKENS: Final = 65_536
BASIS_POINTS: Final = 10_000
PERLECTIO_V2: Final = "perlectio.v2"
# The payload schema each page kind is read under; any other is refused `not-page-read`.
PAGE_KIND_SCHEMAS: Final = {
    "page-feed": PAGE_FEED_SCHEMA,
    "page-reading": PAGE_READING_SCHEMA,
    "act-region": ACT_REGION_SCHEMA,
    "perlectio": PERLECTIO_V2,
    "page-accounting": PAGE_ACCOUNTING_SCHEMA,
}
DETECTOR_RECORD: Final = "detector-record"
EXACTLY_ONCE: Final = "exactly-once"
LOST: Final = "lost"
MERGED: Final = "merged"
DUPLICATED: Final = "duplicated"
FAILURES: Final = frozenset({LOST, MERGED})

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
    in the stored page's frame (`bbox = [x, y, w, h]`), kept as the page
    records' own `bounds` `{x, y, w, h}`; `gold.jsonl` gives its text. A ledger
    record with no gold row is refused by name.
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
        if not normalized_text(texts[record_id]):
            raise Refusal(f"malformed-record: admitted record {record_id!r} has no text to measure")
        x, y, w, h = bbox
        records.append(
            {
                "record_id": record_id,
                "page_sha256": page_sha256,
                "box_px": {"x": x, "y": y, "w": w, "h": h},
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
    no page feed, a feed not read by page, or a page record under another
    schema than `PAGE_KIND_SCHEMAS` names, is refused `not-page-read`.
    """
    shas = load_exemplar_page_shas(tree)
    by_kind: dict[str, list[dict[str, Any]]] = {kind: [] for kind in PAGE_KIND_SCHEMAS}
    for entry in tree.build_manifest(PERLECTOR)["artifacts"]:
        if entry["kind"] in by_kind:
            record = tree.read_artifact(PERLECTOR, entry["kind"], entry["artifact_id"])
            schema = record["payload"].get("schema")
            if schema != PAGE_KIND_SCHEMAS[entry["kind"]]:
                raise Refusal(
                    f"not-page-read: {entry['kind']} {record['subject_id']!r} is {schema!r}, "
                    f"not {PAGE_KIND_SCHEMAS[entry['kind']]}"
                )
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
        page = act_pages.get(record["subject_id"])
        if page is None:
            raise Refusal(f"malformed-record: perlectio {record['subject_id']!r} has no act region")
        page["perlectios"].append(record["payload"])
    for record in by_kind["page-accounting"]:
        page_of(record["payload"], "page accounting")["accounting"] = record["payload"]
    return [pages[page_id] for page_id in sorted(pages)]


def sealed_policy_sha256(tree: RunTree | ReadOnlyRunTree) -> str:
    """The page-accounting policy digest the run sealed, or `policy-mismatch` by name."""
    sealed = run_sealed_config_digests(tree.read_run()).get(SEALED_CONFIG_NAME)
    if sealed is None:
        raise Refusal("policy-mismatch: the run sealed no page-accounting policy")
    return sealed


# --- the report ------------------------------------------------------------------------


def _bucket(count: int) -> str:
    return "2+" if count > 1 else str(count)


def _overlaps(a: Mapping[str, int], b: Mapping[str, int]) -> bool:
    return max(a["x"], b["x"]) < min(a["x"] + a["w"], b["x"] + b["w"]) and max(
        a["y"], b["y"]
    ) < min(a["y"] + a["h"], b["y"] + b["h"])


def _share(numerator: int, denominator: int) -> int | None:
    return None if denominator == 0 else numerator * BASIS_POINTS // denominator


def _gold_cer_bp(gold_text: str, reading: str) -> int:
    """Edits from the gold text to the best-matching substring of a reading, per gold letter."""
    gold = normalized_text(gold_text)
    if not gold:
        raise Refusal("malformed-record: a gold record has no text to measure")
    return best_substring_distance(gold, normalized_text(reading)) * BASIS_POINTS // len(gold)


def _read_in(record: Mapping[str, Any], others: Sequence[Mapping[str, Any]], reading: str) -> bool:
    """Whether a reading reads this gold record: close enough, and no other gold record closer."""
    own = _gold_cer_bp(record["text"], reading)
    return own <= MAX_GOLD_CER_BP and all(
        own <= _gold_cer_bp(other["text"], reading) for other in others
    )


def _findings(accounting: Mapping[str, Any]) -> list[tuple[str, dict[str, Any]]]:
    return [
        (name, finding)
        for name, rule in sorted(accounting["rules"].items())
        for finding in rule["findings"]
        if finding["code"] in HOLD_CODES
    ]


def _caught_by(
    page: Mapping[str, Any], box: Mapping[str, int]
) -> tuple[list[str], list[str], list[str]]:
    """The rules whose held findings are located on a gold record, hold the whole
    page, or reach it only through an unplaced region.

    A finding is located on the record when it names a placed region
    overlapping it, carries a box overlapping it, or names a unit cited by such
    a region. One that names an unplaced region (no box, so it may be
    anywhere) and no overlapping placed one is unplaced-only; one that names
    no region, box or cited unit holds the whole page.
    """
    regions = {region["n"]: region["region_boxes_px"] for region in page["act_regions"]}
    overlapping = {
        n for n, region in regions.items() if any(_overlaps(part, box) for part in region)
    }
    unplaced = {n for n, region in regions.items() if not region}
    cited_by = {row["id"]: row["by"] for row in page["accounting"]["units"]}
    located: set[str] = set()
    page_wide: set[str] = set()
    unplaced_only: set[str] = set()
    for name, finding in _findings(page["accounting"]):
        ns = [finding["n"]] if "n" in finding else []
        for key in ("ns", "inside", "compared_with"):
            ns += finding.get(key, [])
        identifier = finding.get("id")
        if not ns and identifier is not None:
            ns = cited_by.get(identifier, [])
        if "box_px" in finding:
            if _overlaps(finding["box_px"], box):
                located.add(name)
        elif ns:
            if overlapping & set(ns):
                located.add(name)
            elif unplaced & set(ns):
                unplaced_only.add(name)
        else:
            page_wide.add(name)
    return sorted(located), sorted(page_wide - located), sorted(unplaced_only - located)


def _witness_classes(page: Mapping[str, Any]) -> list[tuple[str, Mapping[str, int]]]:
    """Every boxed witness unit and detector record on the page as (class, box)."""
    units = [
        (witness["witness_label"], unit["box_px"])
        for witness in page["feed"]["witnesses"]
        for unit in witness["units"]
        if unit.get("box_px") is not None
    ]
    accounting = page["accounting"]
    if accounting is not None and accounting.get("records"):
        units += [
            (DETECTOR_RECORD, record["box_px"])
            for record in accounting["records"]
            if record["box_px"] is not None
        ]
    return units


def exactly_once_report(
    pages: Sequence[Mapping[str, Any]],
    gold: Sequence[Mapping[str, Any]],
    *,
    policy: PageAccountingPolicy,
    sealed_policy_sha256: str,
    seconds_per_page: Mapping[str, float] | None = None,
) -> dict[str, Any]:
    """The `exactly-once-report.v1` body for page records against gold records.

    `pages` as `load_page_records` returns them; `gold` as `gold_records`
    returns them; `sealed_policy_sha256` the page-accounting digest the run
    sealed; `seconds_per_page` is `{page_id: seconds}` from outside the tree,
    which records no durations. A policy other than the one the run sealed, or
    a page accounting sealed under another, is refused, so "inside" means one
    thing throughout -- including on pages that have no accounting.
    """
    if sealed_policy_sha256 != policy.sha256:
        raise Refusal(
            "policy-mismatch: the run sealed another page-accounting policy than the one given"
        )
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
                    "outcome": LOST,
                    "unchecked": False,
                    "caught_by": [],
                    "caught_page_wide_by": [],
                    "caught_unplaced_only_by": [],
                    "merge_classes": [],
                }
            )
            continue
        box = record["box_px"]
        others = [other for other in gold_by_page[record["page_sha256"]] if other is not record]
        readings = {perlectio["n"]: perlectio["text"] for perlectio in page["perlectios"]}
        holding = [
            region
            for region in page["act_regions"]
            if region["kind"] == "act" and is_inside(box, region["region_boxes_px"], policy)
        ]
        text = (
            "no-region"
            if not holding
            else "read"
            if any(_read_in(record, others, readings.get(region["n"], "")) for region in holding)
            else "not-read"
        )
        merged = any(
            is_inside(other["box_px"], region["region_boxes_px"], policy)
            for region in holding
            for other in others
        )
        outcome = (
            LOST
            if text != "read"
            else MERGED
            if merged
            else EXACTLY_ONCE
            if len(holding) == 1
            else DUPLICATED
        )
        merge_classes = sorted(
            {
                label
                for label, unit_box in _witness_classes(page)
                if is_inside(box, [unit_box], policy)
                and any(is_inside(other["box_px"], [unit_box], policy) for other in others)
            }
        )
        unchecked = page["accounting"] is None
        located, page_wide, unplaced_only = (
            _caught_by(page, box) if outcome in FAILURES and not unchecked else ([], [], [])
        )
        rows.append(
            {
                "record_id": record["record_id"],
                "page_id": page["feed"]["page_id"],
                "act_regions": _bucket(len(holding)),
                "text": text,
                "outcome": outcome,
                "unchecked": unchecked,
                "caught_by": located,
                "caught_page_wide_by": page_wide,
                "caught_unplaced_only_by": unplaced_only,
                "merge_classes": merge_classes,
            }
        )

    outcomes = Counter(row["outcome"] for row in rows)
    exactly = outcomes[EXACTLY_ONCE]
    failures = [row for row in rows if row["outcome"] in FAILURES and not row["unchecked"]]
    uncaught = [row for row in failures if not row["caught_by"]]
    unchecked_pages = sorted(
        page["feed"]["page_id"] for page in pages if page["accounting"] is None
    )
    holds_by_page = {
        page["feed"]["page_id"]: page["accounting"]["holds"]
        for page in pages
        if page["accounting"] is not None
    }
    duplicated_uncaught = sum(
        1
        for row in rows
        if row["outcome"] == DUPLICATED
        and not row["unchecked"]
        and not holds_by_page[row["page_id"]]
    )

    merge_split: dict[str, dict[str, int]] = {}
    for row in rows:
        for label in row["merge_classes"] or ["no-merge"]:
            bucket = merge_split.setdefault(label, {"records": 0, "exactly_once": 0})
            bucket["records"] += 1
            bucket["exactly_once"] += int(row["outcome"] == EXACTLY_ONCE)

    rule_i: Counter[tuple[str, bool]] = Counter()
    hold_codes: Counter[str] = Counter()
    parse_states: Counter[str] = Counter()
    finish_length = with_reading = 0
    tokens = []
    fits: Counter[str] = Counter()
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
            if not region["region_boxes_px"]:
                continue
            true_merge = (
                sum(
                    is_inside(record["box_px"], region["region_boxes_px"], policy)
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
        "max_gold_cer_bp": MAX_GOLD_CER_BP,
        "gate": {
            "exactly_once_bp": exactly_bp,
            "required_bp": GATE_EXACTLY_ONCE_BP,
            "uncaught_failures": len(uncaught),
            "unchecked_pages": len(unchecked_pages),
            "passed": exactly_bp is not None
            and exactly_bp >= GATE_EXACTLY_ONCE_BP
            and not uncaught
            and not unchecked_pages,
        },
        "records": {
            "total": len(rows),
            "exactly_once": exactly,
            "by_act_regions": dict(sorted(Counter(row["act_regions"] for row in rows).items())),
            "by_text": dict(sorted(Counter(row["text"] for row in rows).items())),
            "by_outcome": dict(sorted(outcomes.items())),
            "unchecked": sum(1 for row in rows if row["unchecked"]),
            "duplicated_on_pages_not_held": duplicated_uncaught,
            "failures_caught_by_rule": dict(
                sorted(Counter(rule for row in failures for rule in row["caught_by"]).items())
            ),
            "failures_caught_page_wide_by_rule": dict(
                sorted(
                    Counter(rule for row in failures for rule in row["caught_page_wide_by"]).items()
                )
            ),
            "failures_caught_unplaced_only_by_rule": dict(
                sorted(
                    Counter(
                        rule for row in failures for rule in row["caught_unplaced_only_by"]
                    ).items()
                )
            ),
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
            "unchecked_page_ids": unchecked_pages,
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
        f"need {gate['required_bp']}); uncaught failures {gate['uncaught_failures']}; "
        f"unchecked pages {gate['unchecked_pages']}",
        f"outcomes: {records['by_outcome']}; act regions per record: "
        f"{records['by_act_regions']}; text: {records['by_text']}",
        f"failures caught (located) by rule: {records['failures_caught_by_rule']}; "
        f"not credited: page-wide {records['failures_caught_page_wide_by_rule']}, "
        f"unplaced-only {records['failures_caught_unplaced_only_by_rule']}",
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
    tree = ReadOnlyRunTree(RunTree(args.run_root, args.run_id))
    pages = load_page_records(tree)
    seconds = (
        json.loads(args.seconds_per_page.read_text(encoding="utf-8"))
        if args.seconds_per_page
        else None
    )
    report = exactly_once_report(
        pages,
        gold,
        policy=policy,
        sealed_policy_sha256=sealed_policy_sha256(tree),
        seconds_per_page=seconds,
    )
    args.out.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    for line in summary_lines(report):
        print(line)
    return 0 if report["gate"]["passed"] else 1


if __name__ == "__main__":
    sys.exit(main())
