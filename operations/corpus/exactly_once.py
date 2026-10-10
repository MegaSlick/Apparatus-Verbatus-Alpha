"""Whether a page-read run read every RecordGold record exactly once.

Reads a run tree -- the Perlector's `page-feed`, `page-reading`,
`act-region`, `perlectio` and `page-accounting` records -- beside the admitted RecordGold records of its
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
accounting -- one whose code is among its `holds` -- is located on it: names a
placed region that overlaps it, carries a box that overlaps it, or names a unit
cited by such a region. Three weaker catches are reported beside it and not
credited: page-wide (a held finding that names no region, box or unit: an
incomplete answer, unread ink, a measurement not taken), unplaced-only (one that
reaches the record only through an unplaced region, which may be anywhere) and
flagged (a located review flag, which holds nothing, so the reading is
delivered as read). A finding of a rule the page type switched off is neither
held nor flagged and catches nothing. A failure with no located catch is
uncaught, and a page with no accounting is `unchecked`, its own failure, never
held and caught.

Beside that: records under one witness unit or detector record that also
covers another record, how often `merged-detection` fired on regions that
truly hold two gold records, the hold codes, admitted prompt tokens against the
engine's `usage.prompt_tokens`, the `length` finish rate, time per page where
the caller supplies it, and whether each page would fit a 65,536-token context.
Rule (i) cannot see a detector record that itself merged two entries when the
reader read them as one act; the merged outcome here is what measures it.

The report holds counts and identifiers only, never text. It chooses nothing:
it runs after the tree is sealed and returns nothing to the pipeline.

Only gold on pages the run sealed is scored; ledger pages outside the run are
counted, not lost, so a run over part of a set is judged on its own pages. A
page may be re-asked once: its records are judged on the sealed final
accounting and every act region the page holds, and again on the first
reading alone, so the report states what the re-ask recovered. A page a person
had read again (an operator re-read, attempt 3 on, the reading the Recensor's
receipt binds) is judged on that reading, as the run counts it, and again on the
machine's own readings it superseded (the first reading and its re-ask), so the
report states what the person's retry changed (`operator_reread`) and a retry
never quietly improves the reader's figure.

The gate: at least 95% of gold records read exactly once, no failure without
a located catch and no page unchecked.
"""

from __future__ import annotations

import json
import statistics
import sys
from collections import Counter
from collections.abc import Collection, Mapping, Sequence
from pathlib import Path
from typing import Any, Final

from common.contracts.canonical import (
    canonical_bytes,
    digest_bytes,
    is_plain_int,
    is_sha256,
    verify_self_hash,
)
from common.contracts.errors import ContractError
from common.contracts.stages import PERLECTOR
from common.page_accounting import (
    DEFAULT_PAGE_ACCOUNTING_CONFIG_PATH,
    MERGED_DETECTION,
    REASK_DUPLICATE,
    SEALED_CONFIG_NAME,
    PageAccountingPolicy,
    answer_basis,
    best_substring_distance,
    is_inside,
    load_page_accounting_policy,
    normalized_text,
)
from common.page_accounting import SCHEMA as PAGE_ACCOUNTING_SCHEMA
from common.page_edges import OPERATOR_REREAD_FIRST
from common.page_feed import SCHEMA as PAGE_FEED_SCHEMA
from common.page_path import (
    ACT_REGION_SCHEMA,
    OPERATOR_REREAD_FIELD,
    PAGE_READING_SCHEMA,
    PERLECTIO_SCHEMA,
)
from common.runtree.store import RunTree
from common.stage import run_sealed_config_digests, verify_final_seal

from . import CorpusRefusal
from .cache import write_new_file
from .compare import ReadOnlyRunTree, load_exemplar_page_shas
from .local_admission import load_local_admission_ledger, validate_local_admission_ledger
from .normalization import GRAPHEMIC_V1, within_text_bounds
from .scoring import TEXT_OUT_OF_BOUNDS

SCHEMA: Final = "exactly-once-report.v5"
GATE_EXACTLY_ONCE_BP: Final = 9_500
# A gold record's text is read when its character error rate against the best
# holding act's reading is at most this (basis points): stricter than the
# accounting's rule (e), which passes correct readings at 30% error.
MAX_GOLD_CER_BP: Final = 2_000
FIT_CONTEXT_TOKENS: Final = 65_536
BASIS_POINTS: Final = 10_000
# The payload schema each page kind is read under; any other is refused `not-page-read`.
PAGE_KIND_SCHEMAS: Final = {
    "page-feed": PAGE_FEED_SCHEMA,
    "page-reading": PAGE_READING_SCHEMA,
    "act-region": ACT_REGION_SCHEMA,
    "perlectio": PERLECTIO_SCHEMA,
    "page-accounting": PAGE_ACCOUNTING_SCHEMA,
}
DETECTOR_RECORD: Final = "detector-record"
EXACTLY_ONCE: Final = "exactly-once"
LOST: Final = "lost"
MERGED: Final = "merged"
DUPLICATED: Final = "duplicated"
FAILURES: Final = frozenset({LOST, MERGED})
# A record whose holding act's reading is beyond the scoring profile's text
# bounds: not measured, never exactly-once, and the gate cannot pass with one.
UNMEASURED: Final = "unmeasured"
# A page is read once and may be re-asked once; each reading has its own accounting.
FIRST_READING: Final = 1
REASK_READING: Final = 2
READING_ATTEMPTS: Final = (FIRST_READING, REASK_READING)
ANSWER_BASES: Final = {"attempt-1": FIRST_READING, "combined": REASK_READING}

EXACTLY_ONCE_REFUSAL_REASONS: Final = frozenset(
    {
        "malformed-record",
        "missing-file",
        "no-export",
        "not-page-read",
        "output-exists",
        "output-in-run-tree",
        "policy-mismatch",
        "reference-mismatch",
    }
)


class Refusal(CorpusRefusal):
    reasons = EXACTLY_ONCE_REFUSAL_REASONS


# --- inputs ----------------------------------------------------------------------------


def gold_records(gold_body: bytes, ledger: Mapping[str, Any]) -> tuple[list[dict[str, Any]], int]:
    """Admitted RecordGold records as `{record_id, page_sha256, box_px, text}`,
    and the number of gold rows not scored.

    `gold_body` is the bytes of the set's `gold.jsonl` and `ledger` its
    admission ledger. The ledger is validated and the gold bytes must be the
    exact file its receipt sealed. The file is read as admission reads it:
    blank lines skipped, and a row with no string `record_id` or `text` left
    out, as admission refused it. Each admitted record's text is the gold row
    whose text digests to the `text_sha256` the ledger's reference page holds
    for it, so where admission kept one of two rows naming a record, the copy
    it kept is the one scored. The ledger gives each admitted record its page
    digest and its box in the stored page's frame (`bbox = [x, y, w, h]`), kept
    as the page records' own `bounds` `{x, y, w, h}`. A ledger record with no
    gold row, or gold rows none of which matches its text digest, is refused by
    name.
    """
    ledger = validate_local_admission_ledger(ledger)
    sealed = ledger["receipt"]["digests"]["gold.jsonl"]
    if digest_bytes(gold_body) != sealed:
        raise Refusal(
            f"reference-mismatch: the gold file digests to {digest_bytes(gold_body)}, the "
            f"admission ledger sealed gold.jsonl as {sealed}"
        )
    candidates: dict[str, list[str]] = {}
    rows = _jsonl_rows(gold_body, "gold.jsonl")
    for row in rows:
        record_id, text = row.get("record_id"), row.get("text")
        if isinstance(record_id, str) and isinstance(text, str):
            candidates.setdefault(record_id, []).append(text)
    admitted_sha256 = {
        act["record_id"]: act["text_sha256"]
        for page in ledger["reference_pages"]
        for act in page["acts"]
    }
    texts: dict[str, str] = {}
    for record_id, sha256 in admitted_sha256.items():
        given = candidates.get(record_id, [])
        matching = [text for text in given if digest_bytes(text.encode("utf-8")) == sha256]
        if given and not matching:
            raise Refusal(
                f"reference-mismatch: no gold row for {record_id!r} matches its admitted text "
                "digest"
            )
        if matching:
            texts[record_id] = matching[0]
    records = []
    for row in ledger["rows"]:
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
    return sorted(records, key=lambda record: record["record_id"]), len(rows) - len(records)


def _read_ref_json(tree: RunTree | ReadOnlyRunTree, ref: Any) -> dict[str, Any]:
    if not isinstance(ref, dict) or "relative_path" not in ref or "sha256" not in ref:
        raise Refusal(f"malformed-record: {ref!r} is not a digest-checked reference")
    body = tree.read_bytes(ref["relative_path"])
    if digest_bytes(body) != ref["sha256"]:
        raise Refusal(f"malformed-record: {ref['relative_path']} does not match its digest")
    return json.loads(body)


def _attempt(value: Any, what: str, *, reread: bool = False) -> int:
    """A reading attempt: 1 or 2, or an operator re-read's (3 on) when `reread` says it is one."""
    if value not in READING_ATTEMPTS and not (
        reread and is_plain_int(value) and value >= OPERATOR_REREAD_FIRST
    ):
        raise Refusal(f"malformed-record: {what} names reading attempt {value!r}")
    return value


def _accounting_attempt(payload: Mapping[str, Any]) -> int:
    """Which reading of the page an accounting accounts for, read from its own schema.

    A `page-accounting.v1` accounts for the page's only reading; a v2 says so
    in `answer_basis`: the first reading alone, or the re-ask combined with it.
    """
    if "answer_basis" not in payload:
        return FIRST_READING
    basis = payload["answer_basis"]
    if basis in ANSWER_BASES:
        return ANSWER_BASES[basis]
    # An operator re-read (attempt 3 on) is accounted alone, as "attempt-<n>".
    reread = basis.removeprefix("attempt-") if isinstance(basis, str) else ""
    if (
        reread.isdigit()
        and int(reread) >= OPERATOR_REREAD_FIRST
        and basis == answer_basis(int(reread))
    ):
        return int(reread)
    raise Refusal(f"malformed-record: a page accounting has answer basis {basis!r}")


def load_page_records(tree: RunTree | ReadOnlyRunTree) -> list[dict[str, Any]]:
    """The Perlector's page records of a page-read tree, grouped by page, read-only.

    Each page is `{page_sha256, feed, reading, usage, reask, reask_usage,
    act_regions, perlectios, accounting, first_accounting}`: payloads as
    published. `reading` is the page's first reading and `reask` its re-ask
    (`attempt_ordinal` 2), or `None`; `usage` and `reask_usage` are the engine's
    usage from each one's call record, or `None` without a call. `accounting`
    is the sealed final accounting -- the re-ask's on a re-asked page, `None`
    when the last reading has none -- and `first_accounting` the first
    reading's. Two records for one reading of a page are refused. A tree with
    no page feed, or a page record under another schema than
    `PAGE_KIND_SCHEMAS` names, is refused `not-page-read`.
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
            by_kind[entry["kind"]].append(
                {
                    "subject_id": record["subject_id"],
                    "relative_path": entry["relative_path"],
                    "sha256": entry["sha256"],
                    **record,
                }
            )
    if not by_kind["page-feed"]:
        raise Refusal("not-page-read: the Perlector published no page-feed record")

    pages: dict[str, dict[str, Any]] = {}
    for record in by_kind["page-feed"]:
        feed = record["payload"]
        ordinal = feed["page_ordinal"]
        if ordinal not in shas:
            raise Refusal(f"malformed-record: page ordinal {ordinal} has no sealed Exemplar page")
        pages[feed["page_id"]] = {
            "page_sha256": shas[ordinal],
            "feed": feed,
            "readings": {},
            "usages": {},
            "accountings": {},
            "paths": {},
            "act_regions": [],
            "perlectios": [],
        }

    def page_of(payload: Mapping[str, Any], what: str) -> dict[str, Any]:
        page = pages.get(payload.get("page_id"))
        if page is None:
            raise Refusal(f"malformed-record: a {what} names a page with no page feed")
        return page

    for record in by_kind["page-reading"]:
        payload = record["payload"]
        page = page_of(payload, "page reading")
        attempt = _attempt(
            payload.get("attempt_ordinal", FIRST_READING),
            "a page reading",
            reread=isinstance(payload.get(OPERATOR_REREAD_FIELD), Mapping),
        )
        page["paths"][attempt] = {
            "relative_path": record["relative_path"],
            "sha256": record["sha256"],
        }
        if attempt in page["readings"]:
            raise Refusal(
                f"malformed-record: page {payload['page_id']!r} has two readings at attempt "
                f"{attempt}"
            )
        page["readings"][attempt] = payload
        engine_call = payload.get("engine_call")
        page["usages"][attempt] = (
            None
            if engine_call is None
            else _read_ref_json(tree, engine_call["call_record_ref"]).get("usage")
        )
    for record in by_kind["page-accounting"]:
        payload = record["payload"]
        page = page_of(payload, "page accounting")
        attempt = _accounting_attempt(payload)
        if attempt in page["accountings"]:
            raise Refusal(
                f"malformed-record: page {payload['page_id']!r} has two accountings at attempt "
                f"{attempt}"
            )
        page["accountings"][attempt] = payload
    act_pages: dict[str, dict[str, Any]] = {}
    for record in by_kind["act-region"]:
        payload = record["payload"]
        page = page_of(payload, "act region")
        _attempt(payload.get("reading_attempt", FIRST_READING), "an act region")
        page["act_regions"].append(payload)
        act_pages[record["subject_id"]] = page
    for record in by_kind["perlectio"]:
        page = act_pages.get(record["subject_id"])
        if page is None:
            raise Refusal(f"malformed-record: perlectio {record['subject_id']!r} has no act region")
        page["perlectios"].append(record["payload"])

    loaded = []
    for page_id in sorted(pages):
        page = pages[page_id]
        readings, usages, accountings, paths = (
            page.pop("readings"),
            page.pop("usages"),
            page.pop("accountings"),
            page.pop("paths"),
        )
        if REASK_READING in readings and FIRST_READING not in readings:
            raise Refusal(f"malformed-record: page {page_id!r} has a re-ask and no first reading")
        if set(accountings) - set(readings):
            raise Refusal(
                f"malformed-record: page {page_id!r} has an accounting for a reading it does not have"
            )
        last = max(readings, default=FIRST_READING)
        machine = max((o for o in readings if o < OPERATOR_REREAD_FIRST), default=FIRST_READING)
        machine_view = {
            "reading": readings.get(FIRST_READING),
            "usage": usages.get(FIRST_READING),
            "reask": readings.get(REASK_READING),
            "reask_usage": usages.get(REASK_READING),
            "accounting": accountings.get(machine),
            "first_accounting": accountings.get(FIRST_READING),
        }
        if last >= OPERATOR_REREAD_FIRST:
            # An operator re-read: judged on it, as the run counts it, and on the
            # machine's own readings it superseded.
            current = _receipt_reading(tree, page_id, page["feed"]["page_ordinal"])
            if current != paths[last]:
                raise Refusal(
                    f"malformed-record: page {page_id!r}'s last operator re-read is not the "
                    "reading the Recensor's receipt binds"
                )
            known = {ref["relative_path"] for ref in paths.values()}
            superseded = {
                ref["relative_path"]
                for ordinal, ref in paths.items()
                if ordinal < OPERATOR_REREAD_FIRST
            }
            for kind in ("act_regions", "perlectios"):
                if not all(_of_reading(record, known) for record in page[kind]):
                    raise Refusal(
                        f"malformed-record: page {page_id!r} has an act record naming no "
                        "reading of the page"
                    )
            current = current["relative_path"]
            regions, perlectios = page["act_regions"], page["perlectios"]
            judged = {current} | superseded
            left_out = sum(
                1 for record in (*regions, *perlectios) if not _of_reading(record, judged)
            )
            page.update(
                reading=readings[last],
                usage=usages[last],
                reask=None,
                reask_usage=None,
                accounting=accountings.get(last),
                first_accounting=accountings.get(last),
                act_regions=[r for r in regions if _of_reading(r, {current})],
                perlectios=[r for r in perlectios if _of_reading(r, {current})],
                superseded={
                    **machine_view,
                    "act_regions": [r for r in regions if _of_reading(r, superseded)],
                    "perlectios": [r for r in perlectios if _of_reading(r, superseded)],
                },
                earlier_reread_act_records=left_out,
            )
        else:
            page.update(machine_view)
        loaded.append(page)
    return loaded


def _receipt_reading(tree: RunTree | ReadOnlyRunTree, page_id: str, ordinal: int) -> dict[str, Any]:
    """The page reading the Recensor's partition receipt binds for page `ordinal`, path and digest."""
    receipt = tree.read_recensor_partition_receipt()
    rows = [row for row in receipt.get("pages") or [] if row.get("page_ordinal") == ordinal]
    if len(rows) != 1:
        raise Refusal(
            f"malformed-record: the Recensor's receipt binds no one reading for page {page_id!r}, "
            "which a person had read again"
        )
    reference = rows[0].get("reading_ref")
    if not (
        isinstance(reference, Mapping)
        and isinstance(reference.get("relative_path"), str)
        and isinstance(reference.get("sha256"), str)
    ):
        raise Refusal(
            f"malformed-record: the Recensor's receipt binds page {page_id!r}'s reading "
            "without a path and digest"
        )
    return {"relative_path": reference["relative_path"], "sha256": reference["sha256"]}


def _of_reading(payload: Mapping[str, Any], paths: Collection[str]) -> bool:
    """Whether an act region or Perlectio was read by a page reading at one of `paths`."""
    reference = payload.get("page_reading_ref")
    return isinstance(reference, Mapping) and reference.get("relative_path") in paths


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


def _findings(
    accounting: Mapping[str, Any], codes: Collection[str]
) -> list[tuple[str, dict[str, Any]]]:
    """Each rule's findings whose code is among `codes`, by rule name.

    `codes` is the page accounting's own `holds` or `flags`, so a finding the
    sealed policy makes a review flag, or one of a rule the page type switched
    off, is never taken for a hold.
    """
    return [
        (name, finding)
        for name, rule in sorted(accounting["rules"].items())
        for finding in rule["findings"]
        if finding["code"] in codes
    ]


def _caught_by(
    page: Mapping[str, Any], box: Mapping[str, int], listed: str = "holds"
) -> tuple[list[str], list[str], list[str]]:
    """The rules whose held findings are located on a gold record, hold the whole
    page, or reach it only through an unplaced region.

    A held finding is one whose code is among the accounting's `holds`; with
    `listed="flags"` the same is measured over its review flags.

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
    for name, finding in _findings(page["accounting"], page["accounting"][listed]):
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


def _score_records(
    pages: Sequence[Mapping[str, Any]],
    gold: Sequence[Mapping[str, Any]],
    policy: PageAccountingPolicy,
) -> list[dict[str, Any]]:
    """One row per gold record, judged on the act regions and accounting each page gives."""
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
                    "flagged_by": [],
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
        # Gold texts are bounded where the admission ledger validates its
        # reference pages; a reading is bounded here, before it is compared.
        text = (
            "no-region"
            if not holding
            else TEXT_OUT_OF_BOUNDS
            if not all(
                within_text_bounds(readings.get(region["n"], ""), GRAPHEMIC_V1)
                for region in holding
            )
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
            UNMEASURED
            if text == TEXT_OUT_OF_BOUNDS
            else LOST
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
        # A review flag located on the failure is reported, never credited: it
        # holds nothing, so the reading is delivered as it was read.
        flagged = _caught_by(page, box, "flags")[0] if outcome in FAILURES and not unchecked else []
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
                "flagged_by": flagged,
                "merge_classes": merge_classes,
            }
        )
    return rows


def _region_attempt(region: Mapping[str, Any]) -> int:
    return region.get("reading_attempt", FIRST_READING)


def _first_reading_view(page: Mapping[str, Any]) -> dict[str, Any]:
    """The page as its first reading left it: its entries and its accounting only."""
    first = [region for region in page["act_regions"] if _region_attempt(region) == FIRST_READING]
    numbers = {region["n"] for region in first}
    return {
        **page,
        "act_regions": first,
        "perlectios": [p for p in page["perlectios"] if p["n"] in numbers],
        "accounting": page.get("first_accounting", page["accounting"]),
    }


def _outcome_counts(rows: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    outcomes = Counter(row["outcome"] for row in rows)
    return {
        "records": len(rows),
        "exactly_once": outcomes[EXACTLY_ONCE],
        "exactly_once_bp": _share(outcomes[EXACTLY_ONCE], len(rows)),
        "by_outcome": dict(sorted(outcomes.items())),
    }


def _reask_duplicate_numbers(page: Mapping[str, Any]) -> set[int]:
    """The entry numbers rule (j) of the page's final accounting holds as re-ask duplicates."""
    accounting = page.get("accounting")
    if accounting is None:
        return set()
    return {
        finding["n"]
        for finding in accounting["rules"]["j"]["findings"]
        if finding["code"] == REASK_DUPLICATE
    }


def _reask_effect(
    pages: Sequence[Mapping[str, Any]],
    before: Sequence[Mapping[str, Any]],
    after: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    """What the re-ask changed: pages re-asked, entries it added, records before and after.

    An added act that rule (j) holds as a duplicate of a first-reading entry is
    counted in `reask_duplicates`, not among the acts the re-ask recovered.
    """
    reasked = sorted(page["feed"]["page_id"] for page in pages if page.get("reask") is not None)
    added = [
        region
        for page in pages
        for region in page["act_regions"]
        if _region_attempt(region) == REASK_READING
    ]
    duplicates = [
        region
        for page in pages
        for region in page["act_regions"]
        if _region_attempt(region) == REASK_READING
        and region["n"] in _reask_duplicate_numbers(page)
    ]
    on_reasked = set(reasked)
    pairs = list(zip(before, after, strict=True))
    return {
        "pages_reasked": len(reasked),
        "reasked_page_ids": reasked,
        "reask_by_parse_state": dict(
            sorted(
                Counter(
                    page["reask"]["parse_state"] for page in pages if page.get("reask") is not None
                ).items()
            )
        ),
        "entries_added_by_reask": dict(sorted(Counter(r["kind"] for r in added).items())),
        "acts_recovered_on_reask": sum(1 for region in added if region["kind"] == "act")
        - sum(1 for region in duplicates if region["kind"] == "act"),
        "reask_duplicates": len(duplicates),
        "before_reask": _outcome_counts(before),
        "after_reask": _outcome_counts(after),
        "on_reasked_pages": {
            "before_reask": _outcome_counts([b for b, _ in pairs if b["page_id"] in on_reasked]),
            "after_reask": _outcome_counts([a for _, a in pairs if a["page_id"] in on_reasked]),
        },
        "records_now_exactly_once": sum(
            1 for b, a in pairs if b["outcome"] != EXACTLY_ONCE and a["outcome"] == EXACTLY_ONCE
        ),
        "records_no_longer_exactly_once": sum(
            1 for b, a in pairs if b["outcome"] == EXACTLY_ONCE and a["outcome"] != EXACTLY_ONCE
        ),
    }


def exactly_once_report(
    pages: Sequence[Mapping[str, Any]],
    gold: Sequence[Mapping[str, Any]],
    *,
    policy: PageAccountingPolicy,
    sealed_policy_sha256: str,
    sealed_page_sha256s: Collection[str],
    seconds_per_page: Mapping[str, float] | None = None,
) -> dict[str, Any]:
    """The `exactly-once-report.v5` body for page records against gold records.

    `pages` as `load_page_records` returns them; `gold` as `gold_records`
    returns them; `sealed_policy_sha256` the page-accounting digest the run
    sealed; `sealed_page_sha256s` the digests of the pages the Exemplar sealed:
    only gold on those pages is scored, and the rest of the ledger is counted
    under `scope`, so a run over a subset of the ledger is judged on its own
    pages. A caller may pass the pages chosen for the run instead, so a chosen
    page the run did not seal is lost. `seconds_per_page` is `{page_id: seconds}` from outside the tree,
    which records no durations. A policy other than the one the run sealed, or
    a page accounting sealed under another, is refused, so "inside" means one
    thing throughout -- including on pages that have no accounting.

    The gate is judged on each page's sealed final accounting and every act
    region the page holds; `reask` reports the same records judged on the
    first readings alone, and what the machine's own re-asks added. A page a
    person had read again enters `reask` as the machine's readings left it,
    and `operator_reread` reports what the person's retry changed.
    """
    if sealed_policy_sha256 != policy.sha256:
        raise Refusal(
            "policy-mismatch: the run sealed another page-accounting policy than the one given"
        )
    for page in pages:
        machine = page.get("superseded", {})
        for accounting in (
            page["accounting"],
            page.get("first_accounting"),
            machine.get("accounting"),
            machine.get("first_accounting"),
        ):
            if accounting is not None and accounting["policy_sha256"] != policy.sha256:
                raise Refusal(
                    f"policy-mismatch: page {page['feed']['page_id']!r} was accounted under "
                    "another page-accounting policy"
                )
    sealed = set(sealed_page_sha256s)
    outside = [record for record in gold if record["page_sha256"] not in sealed]
    gold = [record for record in gold if record["page_sha256"] in sealed]
    gold_by_page: dict[str, list[Mapping[str, Any]]] = {}
    for record in gold:
        gold_by_page.setdefault(record["page_sha256"], []).append(record)

    rows = _score_records(pages, gold, policy)
    # The machine's own readings: a re-read page as its first reading and re-ask
    # left it, so the re-ask is credited only with what the machine read.
    machine_pages = [
        {**page, **page["superseded"]} if "superseded" in page else page for page in pages
    ]
    machine_rows = _score_records(machine_pages, gold, policy)
    first_rows = _score_records([_first_reading_view(page) for page in machine_pages], gold, policy)
    reread = _reread_effect(pages, machine_rows, rows)

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
                fits["fits" if capacity["need"] <= FIT_CONTEXT_TOKENS else "does-not-fit"] += 1
        # Each call's admitted prompt against the engine's own count, re-ask included.
        for sent, usage in ((reading, page["usage"]), (page.get("reask"), page.get("reask_usage"))):
            capacity = (sent or {}).get("capacity")
            engine = (usage or {}).get("prompt_tokens")
            if isinstance(capacity, dict) and isinstance(engine, int):
                tokens.append((capacity["image_prompt_tokens"] + capacity["prompt_tokens"], engine))
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
    unmeasured = outcomes[UNMEASURED]
    run_shas = {page["page_sha256"] for page in pages}
    return {
        "schema": SCHEMA,
        "policy_sha256": policy.sha256,
        "max_gold_cer_bp": MAX_GOLD_CER_BP,
        "gate": {
            "exactly_once_bp": exactly_bp,
            "required_bp": GATE_EXACTLY_ONCE_BP,
            "uncaught_failures": len(uncaught),
            "unchecked_pages": len(unchecked_pages),
            "unmeasured_records": unmeasured,
            "passed": exactly_bp is not None
            and exactly_bp >= GATE_EXACTLY_ONCE_BP
            and not uncaught
            and not unchecked_pages
            and not unmeasured,
        },
        "scope": {
            "sealed_pages": len(sealed),
            "sealed_pages_with_gold": len(gold_by_page),
            "sealed_pages_with_gold_not_read": len(set(gold_by_page) - run_shas),
            "ledger_pages_outside_run": len({record["page_sha256"] for record in outside}),
            "ledger_records_outside_run": len(outside),
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
            "failures_flagged_by_rule": dict(
                sorted(Counter(rule for row in failures for rule in row["flagged_by"]).items())
            ),
            "uncaught_record_ids": [row["record_id"] for row in uncaught],
            "by_merge_class": dict(sorted(merge_split.items())),
        },
        "reask": _reask_effect(machine_pages, first_rows, machine_rows),
        **reread,
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


def _reread_effect(
    pages: Sequence[Mapping[str, Any]],
    before: Sequence[Mapping[str, Any]],
    after: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    """What a person's operator re-reads changed; nothing for a run with none.

    The same records judged on the machine's own readings each re-read
    superseded (`before_reread`) and on the readings the run counts
    (`after_reread`, the gate's), with the pages a person had read again and
    the count of act records from earlier operator re-reads, which a later
    re-read superseded and neither judgement scores.
    """
    reread = [page for page in pages if "superseded" in page]
    if not reread:
        return {}
    on_reread = {page["feed"]["page_id"] for page in reread}
    pairs = list(zip(before, after, strict=True))
    return {
        "operator_reread": {
            "page_ordinals": sorted(page["feed"]["page_ordinal"] for page in reread),
            "earlier_reread_act_records_left_out": sum(
                page["earlier_reread_act_records"] for page in reread
            ),
            "before_reread": _outcome_counts(before),
            "after_reread": _outcome_counts(after),
            "on_reread_pages": {
                "before_reread": _outcome_counts(
                    [b for b, _ in pairs if b["page_id"] in on_reread]
                ),
                "after_reread": _outcome_counts([a for _, a in pairs if a["page_id"] in on_reread]),
            },
            "records_now_exactly_once": sum(
                1 for b, a in pairs if b["outcome"] != EXACTLY_ONCE and a["outcome"] == EXACTLY_ONCE
            ),
            "records_no_longer_exactly_once": sum(
                1 for b, a in pairs if b["outcome"] == EXACTLY_ONCE and a["outcome"] != EXACTLY_ONCE
            ),
        }
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
        f"unchecked pages {gate['unchecked_pages']}; unmeasured records "
        f"{gate['unmeasured_records']}",
        f"outcomes: {records['by_outcome']}; act regions per record: "
        f"{records['by_act_regions']}; text: {records['by_text']}",
        f"failures caught (located) by rule: {records['failures_caught_by_rule']}; "
        f"not credited: page-wide {records['failures_caught_page_wide_by_rule']}, "
        f"unplaced-only {records['failures_caught_unplaced_only_by_rule']}, "
        f"flagged only {records['failures_flagged_by_rule']}",
        f"by merge class: {records['by_merge_class']}",
        f"scope: {report['scope']}",
        f"re-ask: {report['reask']['pages_reasked']} page(s), "
        f"{report['reask']['acts_recovered_on_reask']} act(s) recovered, "
        f"{report['reask']['reask_duplicates']} duplicate(s) held; exactly once "
        f"{report['reask']['before_reask']['exactly_once']} before, "
        f"{report['reask']['after_reask']['exactly_once']} after",
        f"merged-detection: fired on true merge {rule_i['fired_on_true_merge']}, on single "
        f"record {rule_i['fired_on_single_record']}; silent on true merge "
        f"{rule_i['silent_on_true_merge']}",
        f"pages {pages['total']}: {pages['by_parse_state']}; holds {pages['hold_codes']}",
        f"finish length {pages['finish_length_bp']} bp; fit 65,536 {pages['fit_65536']}; "
        f"prompt tokens {pages['prompt_tokens']}; seconds/page {pages['seconds_per_page']}",
    ]


# --- command line ----------------------------------------------------------------------


def _jsonl_rows(body: bytes, what: str) -> list[dict[str, Any]]:
    try:
        rows = [json.loads(line) for line in body.decode("utf-8").splitlines() if line.strip()]
    except ValueError as error:
        raise Refusal(f"malformed-record: {what} is not UTF-8 JSON lines: {error}") from error
    if not all(isinstance(row, dict) for row in rows):
        raise Refusal(f"malformed-record: a line of {what} is not a JSON object")
    return rows


def _read_bytes(path: Path) -> bytes:
    if not path.is_file():
        raise Refusal(f"missing-file: {path} is not a file")
    return path.read_bytes()


def selected_page_sha256s(path: Path, ledger_self_hash: str) -> tuple[set[str], str]:
    """The page digests a `proof_pages` selection chose from this ledger, and its self-hash."""
    if not path.is_file():
        raise Refusal(f"missing-file: {path} is not a file")
    selection = json.loads(path.read_bytes())
    if not verify_self_hash(selection) or selection.get("ledger_self_hash") != ledger_self_hash:
        raise Refusal(
            "malformed-record: the selection does not hash to itself or was drawn from another "
            "ledger"
        )
    return {page["page_sha256"] for page in selection["pages"]}, selection["self_hash"]


def main(argv: list[str] | None = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--run-root", type=Path, required=True, help="the runs directory")
    parser.add_argument("--run-id", required=True, help="a sealed run read by page")
    parser.add_argument("--gold", type=Path, required=True, help="the set's gold.jsonl")
    parser.add_argument("--ledger", type=Path, required=True, help="its local admission ledger")
    parser.add_argument("--out", type=Path, required=True, help="a new file outside the run tree")
    parser.add_argument(
        "--selection",
        type=Path,
        help=(
            "proof_pages' selection.json: score the pages chosen for the run, so a chosen page "
            "the run did not seal is lost rather than left out"
        ),
    )
    parser.add_argument(
        "--seconds-per-page", type=Path, help="optional JSON {page_id: seconds} from the pod log"
    )
    parser.add_argument(
        "--page-accounting-config", type=Path, default=DEFAULT_PAGE_ACCOUNTING_CONFIG_PATH
    )
    args = parser.parse_args(argv)

    run_tree = RunTree(args.run_root, args.run_id)
    if args.out.resolve().is_relative_to(run_tree.root.resolve()):
        raise Refusal("output-in-run-tree: the report must be written outside the run tree")
    policy = load_page_accounting_policy(args.page_accounting_config)
    ledger = load_local_admission_ledger(args.ledger)
    gold, rows_not_scored = gold_records(_read_bytes(args.gold), ledger)
    tree = ReadOnlyRunTree(run_tree)
    try:
        export_record = verify_final_seal(tree)
    except ContractError as error:
        raise Refusal(f"no-export: the run has no verified Armarium export ({error})") from error
    pages = load_page_records(tree)
    if args.selection:
        scope, selection_self_hash = selected_page_sha256s(args.selection, ledger["self_hash"])
    else:
        scope, selection_self_hash = set(load_exemplar_page_shas(tree).values()), None
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
        sealed_page_sha256s=scope,
        seconds_per_page=seconds,
    )
    report["reference"] = {
        "ledger_self_hash": ledger["self_hash"],
        "gold_jsonl_sha256": ledger["receipt"]["digests"]["gold.jsonl"],
        "split": ledger["split"],
        "rows_not_scored": rows_not_scored,
    }
    report["run"] = {
        "run_id": run_tree.run_id,
        "export_sha256": digest_bytes(canonical_bytes(export_record)),
    }
    report["scope"]["basis"] = "selection" if args.selection else "sealed"
    report["scope"]["selection_self_hash"] = selection_self_hash
    body = (json.dumps(report, indent=2, sort_keys=True) + "\n").encode("utf-8")
    if not write_new_file(args.out, body):
        raise Refusal(f"output-exists: {args.out}")
    for line in summary_lines(report):
        print(line)
    return 0 if report["gate"]["passed"] else 1


if __name__ == "__main__":
    sys.exit(main())
