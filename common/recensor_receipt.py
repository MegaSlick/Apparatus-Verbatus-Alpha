"""A self-hashed, scoped partition receipt for the Recensor boundary.

This is deliberately not the pipeline's final export verdict.  It proves only
the act denominator and the configured-witness denominator at the point the
Recensor has reviewed them.  Page-level blank proof, residual ink, and final
Archetypus/Armarium categories require evidence this receipt does not pretend
to own.

v1, v2 and v3 count the Designator's proposal acts (`proposal_seal_ref`,
`expected_act_count`); v3 counts a chair the aligner stopped on as `unmeasured`,
apart from `unaligned`. v4 and v5, the page-read receipts, count a page-read
run's units (`common.stage.reading_acts`, the classes in
`COUNTED_READING_CLASSES`; `expected_unit_count`): every sealed page has at
least one unit and every unit's page is a sealed page's, and each item carries
the unit's page disposition instead of a Designator outcome. A unit its page
reading held is completed at the Recensor only with the reason its review
released it (`release_reason`); so released, it is resolved. A held unit with
no completed review keeps the receipt `partial`. `continuation_links` names
every page break an answer flags, so a break only one side says the text runs
across keeps the run partial.

The two differ in how they name the pages. v4 has `page_reading_refs`,
`[{page_ordinal, reading_ref}]` in page order, naming each sealed page's
`page-reading`. v5, the one the Recensor writes, has `pages`,
`[{page_ordinal, reading_ref, reask_ref, accounting_ref, reask}]` in page
order, binding each page's first reading, its re-ask (`None` on a page not
re-asked) and its last accounting, with `reask` what the re-ask did
(`common.page_reask.reask_outcome`: the named ids, split into `cleared`,
`set_aside`, `held` and `unread`, and the combined numbers of its entries held
as `duplicate`), `None` on a page not re-asked.
"""

from __future__ import annotations

import re
from typing import Any, Final

from common.contracts.canonical import is_plain_int, is_sha256, self_hash, verify_self_hash
from common.contracts.envelope import digest_ref
from common.contracts.errors import FatalAccounting, ReceiptVersionMismatch, SchemaRefusal
from common.contracts.outcomes import (
    INTERIM_GRANULARITY_BASIS,
    NATIVE_GRANULARITY_BASIS,
    SHORTFALL_KINDS,
    WITNESS_READING_OUTCOMES,
    OutcomeClass,
    classify,
    witness_failure_shortfall,
)
from common.contracts.stages import ATTESTATORES, DESIGNATOR, RECENSOR

RECENSOR_PARTITION_RECEIPT_SCHEMA: Final = "recensor-partition-receipt.v1"
RECENSOR_PARTITION_RECEIPT_SCHEMA_V2: Final = "recensor-partition-receipt.v2"
# v3 splits a chair the aligner stopped on (`unmeasured`) out of `unaligned`.
RECENSOR_PARTITION_RECEIPT_SCHEMA_V3: Final = "recensor-partition-receipt.v3"
# v4 counts a page-read run's units instead of the Designator's proposal acts.
RECENSOR_PARTITION_RECEIPT_SCHEMA_V4: Final = "recensor-partition-receipt.v4"
# v5 also binds each page's re-ask and last accounting.
RECENSOR_PARTITION_RECEIPT_SCHEMA_V5: Final = "recensor-partition-receipt.v5"
# The receipts that count a page-read run's units; v5 is the one written.
PAGE_READ_RECEIPT_SCHEMAS: Final = frozenset(
    {RECENSOR_PARTITION_RECEIPT_SCHEMA_V4, RECENSOR_PARTITION_RECEIPT_SCHEMA_V5}
)
# The shortfall buckets each granular version carries; v2's `unaligned` also
# counted a chair whose alignment was never measured.
_SHORTFALL_KEYS: Final = {
    RECENSOR_PARTITION_RECEIPT_SCHEMA_V2: frozenset({"failed", "truncated", "unaligned"}),
    RECENSOR_PARTITION_RECEIPT_SCHEMA_V3: frozenset(SHORTFALL_KINDS),
    # A page-read run's witnesses read the whole page and are never aligned to an act.
    RECENSOR_PARTITION_RECEIPT_SCHEMA_V4: frozenset({"failed", "truncated", "unaligned"}),
    RECENSOR_PARTITION_RECEIPT_SCHEMA_V5: frozenset({"failed", "truncated", "unaligned"}),
}
RECENSOR_PARTITION_RECEIPT_SCOPE: Final = "proposal-acts-and-configured-witnesses"
RECENSOR_READING_RECEIPT_SCOPE: Final = "reading-acts-and-configured-witnesses"
PAGE_DISPOSITIONS: Final = frozenset({"read", "held"})
# `p<page ordinal>:<entry n>`, or the page row of a page with no entry.
_READING_ACT_KEY: Final = re.compile(r"p[1-9][0-9]*:(?:[1-9][0-9]*|unread|blank)")
_READING_ACT_PAGE: Final = re.compile(r"p([1-9][0-9]*):")
_COMMON_FIELDS: Final = frozenset(
    {
        "schema",
        "run_id",
        "config_digest",
        "scope",
        "items",
        "by_partition_class",
        "recensor_status",
        "reasons",
        "self_hash",
    }
)
_ACT_FIELDS: Final = frozenset({"proposal_seal_ref", "expected_act_count"})
_READING_FIELDS: Final = {
    RECENSOR_PARTITION_RECEIPT_SCHEMA_V4: frozenset(
        {"page_reading_refs", "expected_unit_count", "continuation_links"}
    ),
    RECENSOR_PARTITION_RECEIPT_SCHEMA_V5: frozenset(
        {"pages", "expected_unit_count", "continuation_links"}
    ),
}
_PAGE_FIELDS: Final = frozenset(
    {"page_ordinal", "reading_ref", "reask_ref", "accounting_ref", "reask"}
)
_REASK_FIELDS: Final = frozenset({"named", "cleared", "set_aside", "held", "unread", "duplicate"})
_FEED_ID: Final = re.compile(r"[A-Z][1-9][0-9]*")
CONTINUATION_LINK_OUTCOMES: Final = frozenset({"accepted", "held-for-review"})
_LINK_FIELDS: Final = frozenset({"subject_id", "link_ref", "outcome"})
_SCOPE_BY_SCHEMA: Final = {
    RECENSOR_PARTITION_RECEIPT_SCHEMA: RECENSOR_PARTITION_RECEIPT_SCOPE,
    RECENSOR_PARTITION_RECEIPT_SCHEMA_V2: RECENSOR_PARTITION_RECEIPT_SCOPE,
    RECENSOR_PARTITION_RECEIPT_SCHEMA_V3: RECENSOR_PARTITION_RECEIPT_SCOPE,
    RECENSOR_PARTITION_RECEIPT_SCHEMA_V4: RECENSOR_READING_RECEIPT_SCOPE,
    RECENSOR_PARTITION_RECEIPT_SCHEMA_V5: RECENSOR_READING_RECEIPT_SCOPE,
}
_PARTITION_KEYS: Final = tuple(klass.value for klass in OutcomeClass)


def build_recensor_partition_receipt(
    *,
    run_id: str,
    config_digest: str,
    proposal_seal_ref: dict[str, str],
    items: list[dict[str, Any]],
) -> dict[str, Any]:
    """Build a receipt whose summary is mechanically derived from its items."""

    checked_items = [dict(item) for item in items]
    for item in checked_items:
        _validate_item(item)
    checked_items.sort(key=lambda item: item["act_id"])
    reasons = _reasons(checked_items)
    record: dict[str, Any] = {
        "schema": RECENSOR_PARTITION_RECEIPT_SCHEMA_V3,
        "run_id": run_id,
        "config_digest": config_digest,
        "scope": RECENSOR_PARTITION_RECEIPT_SCOPE,
        "proposal_seal_ref": proposal_seal_ref,
        "expected_act_count": len(checked_items),
        "items": checked_items,
        "by_partition_class": _partition_counts(checked_items),
        "recensor_status": _status(reasons),
        "reasons": reasons,
    }
    record["self_hash"] = self_hash(record)
    return validate_recensor_partition_receipt(record)


def build_recensor_reading_receipt(
    *,
    run_id: str,
    config_digest: str,
    pages: list[dict[str, Any]],
    items: list[dict[str, Any]],
    continuation_links: list[dict[str, Any]] = (),
) -> dict[str, Any]:
    """Build a v5 receipt over a page-read run's units, its summary derived from its items.

    `pages` names every sealed page by its page ordinal, in page order:
    `{page_ordinal, reading_ref, reask_ref, accounting_ref, reask}`, the
    first reading, the re-ask (`None` on a page not re-asked), the page's
    last accounting, and what the re-ask did (`page_reask.reask_outcome`,
    `None` on a page not re-asked). Each item is one counted `reading_acts`
    row as the Recensor reviewed it: `{act_id, act_key, page_disposition,
    review_ref, review_outcome, partition_class, coverage, release_reason}`,
    `release_reason` being the reason the Recensor's review record gives for
    completing a unit its page reading held, and `None` otherwise. Each
    continuation link is `{subject_id, link_ref, outcome}` for one flagged
    page break.
    """

    schema = RECENSOR_PARTITION_RECEIPT_SCHEMA_V5
    checked_items = [dict(item) for item in items]
    for item in checked_items:
        _validate_item(item, schema=schema)
    checked_items.sort(key=lambda item: item["act_id"])
    links = sorted((dict(link) for link in continuation_links), key=_link_order)
    for link in links:
        _validate_link(link)
    reasons = _reasons(checked_items, schema=schema, links=links)
    record: dict[str, Any] = {
        "schema": schema,
        "run_id": run_id,
        "config_digest": config_digest,
        "scope": RECENSOR_READING_RECEIPT_SCOPE,
        "pages": [
            {
                "page_ordinal": row["page_ordinal"],
                "reading_ref": dict(row["reading_ref"]),
                "reask_ref": None if row["reask_ref"] is None else dict(row["reask_ref"]),
                "accounting_ref": dict(row["accounting_ref"]),
                "reask": None if row["reask"] is None else dict(row["reask"]),
            }
            for row in pages
        ],
        "expected_unit_count": len(checked_items),
        "continuation_links": links,
        "items": checked_items,
        "by_partition_class": _partition_counts(checked_items),
        "recensor_status": _status(reasons),
        "reasons": reasons,
    }
    record["self_hash"] = self_hash(record)
    return validate_recensor_partition_receipt(record)


def validate_recensor_partition_receipt(record: Any) -> dict[str, Any]:
    """Validate the closed receipt schema and its derived summary."""

    schema = record.get("schema") if isinstance(record, dict) else None
    reading = schema in PAGE_READ_RECEIPT_SCHEMAS
    denominator_fields = _READING_FIELDS[schema] if reading else _ACT_FIELDS
    if not isinstance(record, dict) or set(record) != _COMMON_FIELDS | denominator_fields:
        raise SchemaRefusal("Recensor partition receipt has the wrong closed schema")
    if schema not in _SCOPE_BY_SCHEMA or not verify_self_hash(record):
        raise SchemaRefusal("Recensor partition receipt has an invalid schema or self-hash")
    count = expected_count(record)
    if (
        not isinstance(record["run_id"], str)
        or not record["run_id"]
        or not is_sha256(record["config_digest"])
        or record["scope"] != _SCOPE_BY_SCHEMA[schema]
        or not _is_count(count)
        or not isinstance(record["items"], list)
        or count != len(record["items"])
    ):
        raise SchemaRefusal("Recensor partition receipt has invalid run or denominator facts")
    if reading:
        links = record["continuation_links"]
        if not isinstance(links, list):
            raise SchemaRefusal("Recensor page-read receipt continuation_links is not a list")
        for link in links:
            _validate_link(link)
        subjects = [link["subject_id"] for link in links]
        if subjects != sorted(
            set(subjects), key=lambda subject: _link_order({"subject_id": subject})
        ):
            raise SchemaRefusal(
                "Recensor page-read receipt continuation links are not one per page break, "
                "in page order"
            )
    else:
        _validate_reference(record["proposal_seal_ref"], "proposal-seal reference")
    previous_act_id = ""
    for item in record["items"]:
        _validate_item(item, schema=record["schema"])
        act_id = item["act_id"]
        if not act_id or act_id <= previous_act_id:
            raise SchemaRefusal(
                "Recensor partition receipt items must be strictly sorted by unique act identity"
            )
        previous_act_id = act_id
    if reading:
        _validate_page_readings(record)
    if record["by_partition_class"] != _partition_counts(record["items"]):
        raise SchemaRefusal("Recensor partition receipt partition counts do not reconcile")
    reasons = _reasons(record["items"], schema=schema, links=record.get("continuation_links", []))
    if record["reasons"] != reasons or record["recensor_status"] != _status(reasons):
        raise SchemaRefusal("Recensor partition receipt status does not derive from its items")
    return record


def expected_count(record: dict[str, Any]) -> Any:
    """The receipt's sealed denominator count: acts for v1 to v3, units for v4 and v5."""
    if record.get("schema") in PAGE_READ_RECEIPT_SCHEMAS:
        return record["expected_unit_count"]
    return record["expected_act_count"]


def _link_order(link: dict[str, Any]) -> tuple[int, ...]:
    subject = link["subject_id"]
    return tuple(int(part) for part in subject.split(":")[1:])


def _validate_link(link: Any) -> None:
    if not isinstance(link, dict) or set(link) != _LINK_FIELDS:
        raise SchemaRefusal("Recensor partition receipt continuation link has the wrong shape")
    subject = link["subject_id"]
    parts = subject.split(":") if isinstance(subject, str) else []
    if (
        len(parts) != 3
        or parts[0] != "page-break"
        or not all(part.isdigit() and str(int(part)) == part for part in parts[1:])
        or int(parts[2]) != int(parts[1]) + 1
    ):
        raise SchemaRefusal(
            f"Recensor partition receipt names continuation link {subject!r}, not "
            "page-break:<p>:<p+1>"
        )
    if link["outcome"] not in CONTINUATION_LINK_OUTCOMES:
        raise SchemaRefusal(
            f"Recensor partition receipt continuation link {subject} is {link['outcome']!r}, "
            f"not one of {sorted(CONTINUATION_LINK_OUTCOMES)}"
        )
    _validate_reference(link["link_ref"], "continuation-link reference")


def _validate_page_readings(record: dict[str, Any]) -> None:
    """Every sealed page's reading once, by ordinal, each page with a unit and no unit elsewhere.

    A v5 receipt's pages also bind each page's re-ask and last accounting, and
    what the re-ask did (`_validate_reask`).
    """
    v5 = record["schema"] == RECENSOR_PARTITION_RECEIPT_SCHEMA_V5
    rows = record["pages"] if v5 else record["page_reading_refs"]
    fields = _PAGE_FIELDS if v5 else frozenset({"page_ordinal", "reading_ref"})
    if not isinstance(rows, list) or not rows:
        raise SchemaRefusal(
            "Recensor page-read receipt names no page reading; every sealed page's "
            "reading is part of its denominator"
        )
    ordinals = []
    for row in rows:
        if (
            not isinstance(row, dict)
            or set(row) != fields
            or not is_plain_int(row["page_ordinal"])
            or row["page_ordinal"] < 1
        ):
            raise SchemaRefusal(
                f"Recensor partition receipt page reading is not {{{', '.join(sorted(fields))}}} "
                "with a positive page ordinal"
            )
        _validate_reference(row["reading_ref"], "page-reading reference")
        if v5:
            _validate_reference(row["accounting_ref"], "page-accounting reference")
            _validate_reask(row)
        ordinals.append(row["page_ordinal"])
    if ordinals != sorted(set(ordinals)):
        raise SchemaRefusal(
            "Recensor page-read receipt page readings are not keyed by strictly increasing "
            "page ordinals; each sealed page is named once, in page order"
        )
    paths = [row["reading_ref"]["relative_path"] for row in rows]
    if len(set(paths)) != len(paths):
        raise SchemaRefusal("Recensor page-read receipt names one page reading twice")
    keys = [item.get("act_key") if isinstance(item, dict) else None for item in record["items"]]
    if len(set(map(str, keys))) != len(keys):
        raise SchemaRefusal("Recensor page-read receipt names one act key twice")
    pages = {
        int(match[1])
        for key in keys
        if isinstance(key, str) and (match := _READING_ACT_PAGE.match(key)) is not None
    }
    unread = sorted(set(ordinals) - pages)
    if unread:
        raise SchemaRefusal(
            f"Recensor page-read receipt counts no unit on sealed page(s) {unread}; every "
            "sealed page is at least one unit"
        )
    stray = sorted(pages - set(ordinals))
    if stray:
        raise SchemaRefusal(
            f"Recensor page-read receipt counts units on page(s) {stray}, which name no "
            "sealed page reading"
        )


def _validate_reask(row: dict[str, Any]) -> None:
    """A re-asked page names its re-ask and what it did; any other page names neither.

    `named` is the re-ask's distinct ids, and `cleared`, `set_aside`, `held`
    and `unread` split them, each in `named`'s order; `duplicate` is the
    increasing combined numbers of its entries held as duplicates.
    """
    page = row["page_ordinal"]
    reask = row["reask"]
    if (row["reask_ref"] is None) != (reask is None):
        raise SchemaRefusal(
            f"Recensor partition receipt page {page} names a re-ask without what it did, or "
            "what a re-ask did without the re-ask"
        )
    if reask is None:
        return
    _validate_reference(row["reask_ref"], "page re-ask reference")
    if not isinstance(reask, dict) or set(reask) != _REASK_FIELDS:
        raise SchemaRefusal(
            f"Recensor partition receipt page {page}'s re-ask is not {sorted(_REASK_FIELDS)}"
        )
    named = reask["named"]
    if (
        not isinstance(named, list)
        or not named
        or not all(isinstance(item, str) and _FEED_ID.fullmatch(item) for item in named)
        or len(set(named)) != len(named)
    ):
        raise SchemaRefusal(
            f"Recensor partition receipt page {page}'s re-ask names no id, or one id twice"
        )
    parts = [reask[name] for name in ("cleared", "set_aside", "held", "unread")]
    if (
        not all(isinstance(part, list) for part in parts)
        or sorted(item for part in parts for item in part if isinstance(item, str)) != sorted(named)
        or any(part != [item for item in named if item in part] for part in parts)
    ):
        raise SchemaRefusal(
            f"Recensor partition receipt page {page}'s re-ask does not split its named ids "
            "into cleared, set aside, held and unread, each in the order named"
        )
    duplicate = reask["duplicate"]
    if (
        not isinstance(duplicate, list)
        or not all(is_plain_int(n) and n >= 1 for n in duplicate)
        or duplicate != sorted(set(duplicate))
    ):
        raise SchemaRefusal(
            f"Recensor partition receipt page {page}'s re-ask duplicates are not increasing "
            "entry numbers"
        )


def _is_count(value: Any) -> bool:
    return is_plain_int(value) and value >= 0


def _partition_counts(items: list[dict[str, Any]]) -> dict[str, int]:
    counts = {key: 0 for key in _PARTITION_KEYS}
    for item in items:
        counts[item["partition_class"]] += 1
    return counts


def _status(reasons: list[str]) -> str:
    return "complete" if not reasons else "partial"


def witnessed_count(coverage: dict[str, Any], *, page_read: bool = False) -> int:
    """The count an act's `under_witnessed` flag is judged from.

    With `page_granularity_only`: reading outcomes less page-only contributions,
    which must reproduce `witness_coverage`'s own count exactly. Reading
    outcomes, not the COMPLETED class, because that class also holds approval
    exclusions that never looked at the ink. On a page-read run (v4 or v5), where
    every witness reads the whole page: the reading outcomes less the
    truncated ones. Otherwise (v1): the COMPLETED class.
    """
    reading_chairs = sum(
        coverage["by_outcome"].get(outcome, 0) for outcome in WITNESS_READING_OUTCOMES
    )
    if page_read:
        return reading_chairs - coverage["shortfalls"]["truncated"]
    if "page_granularity_only" in coverage:
        return reading_chairs - coverage["page_granularity_only"]
    return coverage["by_class"][OutcomeClass.COMPLETED.value]


def _validate_item(item: Any, *, schema: str = RECENSOR_PARTITION_RECEIPT_SCHEMA_V3) -> None:
    reading = schema in PAGE_READ_RECEIPT_SCHEMAS
    required = {
        "act_id",
        "act_key",
        "review_ref",
        "review_outcome",
        "partition_class",
        "coverage",
    } | ({"page_disposition", "release_reason"} if reading else {"designator_outcome"})
    if not isinstance(item, dict) or set(item) != required:
        raise SchemaRefusal("Recensor partition receipt item has the wrong closed schema")
    if not isinstance(item["act_id"], str) or not item["act_id"]:
        raise SchemaRefusal("Recensor partition receipt item has no act identity")
    if not isinstance(item["act_key"], str) or not item["act_key"]:
        raise SchemaRefusal("Recensor partition receipt item has no act key")
    if reading and not _READING_ACT_KEY.fullmatch(item["act_key"]):
        raise SchemaRefusal(
            f"Recensor page-read receipt item names act key {item['act_key']!r}, not "
            "p<page>:<n>, p<page>:unread or p<page>:blank"
        )
    if reading and item["page_disposition"] not in PAGE_DISPOSITIONS:
        raise SchemaRefusal(
            f"Recensor partition receipt item names page_disposition "
            f"{item['page_disposition']!r}, not one of {sorted(PAGE_DISPOSITIONS)}"
        )
    try:
        if not reading:
            classify(DESIGNATOR, item["designator_outcome"])
        expected_class = classify(RECENSOR, item["review_outcome"]).value
    except FatalAccounting as error:
        raise SchemaRefusal(
            "Recensor partition receipt item names an unknown Designator or Recensor outcome"
        ) from error
    if item["partition_class"] != expected_class:
        raise SchemaRefusal(
            "Recensor partition receipt item names partition_class "
            f"{item['partition_class']!r}, but review_outcome {item['review_outcome']!r} "
            f"derives {expected_class!r}"
        )
    _validate_reference(item["review_ref"], "review reference")
    if reading:
        _validate_release(item)
    _validate_coverage(item["coverage"], schema=schema)


def _validate_release(item: dict[str, Any]) -> None:
    """A held unit completed at review names why it was released; no other unit names one."""
    released = (
        item["page_disposition"] == "held"
        and item["partition_class"] == OutcomeClass.COMPLETED.value
    )
    reason = item["release_reason"]
    if released and not (isinstance(reason, str) and reason.strip()):
        raise SchemaRefusal(
            f"Recensor page-read receipt item {item['act_key']} was held by its page reading "
            f"and is {item['review_outcome']} at the Recensor, but names no reason its review "
            "released it"
        )
    if not released and reason is not None:
        raise SchemaRefusal(
            f"Recensor page-read receipt item {item['act_key']} names a release reason, but "
            "only a held unit completed at review is released"
        )


def _validate_coverage(
    coverage: Any, *, schema: str = RECENSOR_PARTITION_RECEIPT_SCHEMA_V3
) -> None:
    required = {
        "configured",
        "floor",
        "by_outcome",
        "by_class",
        "under_witnessed",
        "unresolved_chairs",
    }
    granularity_fields = {
        "page_granularity_only",
        "health_unrecorded",
        "shortfalls",
        "granularity_basis",
    }
    if not isinstance(coverage, dict):
        raise SchemaRefusal("Recensor partition receipt has malformed witness coverage")
    present_granularity = set(coverage) & granularity_fields
    if schema in PAGE_READ_RECEIPT_SCHEMAS:
        # Every witness of a page-read run reads the whole page, so no count is
        # judged at act granularity: health and shortfalls, never attachment.
        if present_granularity != {"health_unrecorded", "shortfalls"}:
            raise SchemaRefusal(
                "Recensor page-read receipt coverage carries exactly health_unrecorded and "
                "shortfalls beside its counts; a page-read run attaches no witness to an act"
            )
    if schema == RECENSOR_PARTITION_RECEIPT_SCHEMA and present_granularity:
        raise ReceiptVersionMismatch(
            "receipt schema v1 cannot carry page-granularity coverage facts; use receipt "
            f"version {RECENSOR_PARTITION_RECEIPT_SCHEMA_V3}"
        )
    allowed = required | granularity_fields
    if set(coverage) - allowed or not required <= set(coverage):
        raise SchemaRefusal("Recensor partition receipt has malformed witness coverage")
    if (
        schema in _SHORTFALL_KEYS
        and schema not in PAGE_READ_RECEIPT_SCHEMAS
        and not granularity_fields <= set(coverage)
    ):
        raise SchemaRefusal(
            f"Recensor partition receipt {schema} omits one or more required granularity facts"
        )
    for field in ("configured", "floor", "unresolved_chairs"):
        value = coverage[field]
        if not _is_count(value):
            raise SchemaRefusal(
                f"Recensor partition receipt has invalid witness coverage counts: {field!r} "
                f"is {value!r}, not a non-negative integer"
            )
    by_outcome = coverage["by_outcome"]
    by_class = coverage["by_class"]
    if not isinstance(by_outcome, dict) or not all(
        isinstance(outcome, str) and outcome and _is_count(count)
        for outcome, count in by_outcome.items()
    ):
        raise SchemaRefusal(
            "Recensor partition receipt's by_outcome is not a mapping of non-empty witness "
            "outcome names to non-negative integer counts"
        )
    if not isinstance(by_class, dict) or set(by_class) != set(_PARTITION_KEYS):
        raise SchemaRefusal(
            "Recensor partition receipt's by_class does not name exactly the partition "
            f"classes {sorted(_PARTITION_KEYS)}"
        )
    if not all(_is_count(count) for count in by_class.values()):
        raise SchemaRefusal(
            f"Recensor partition receipt's by_class {by_class} holds a count that is not a "
            "non-negative integer"
        )
    if sum(by_outcome.values()) != coverage["configured"]:
        raise SchemaRefusal(
            f"Recensor partition receipt's by_outcome totals {sum(by_outcome.values())} "
            f"against {coverage['configured']} configured chair(s); every configured chair "
            "gets exactly one outcome"
        )
    if sum(by_class.values()) != coverage["configured"]:
        raise SchemaRefusal(
            f"Recensor partition receipt's by_class totals {sum(by_class.values())} against "
            f"{coverage['configured']} configured chair(s)"
        )
    if coverage["unresolved_chairs"] != by_class[OutcomeClass.UNRESOLVED.value]:
        raise SchemaRefusal(
            f"Recensor partition receipt names {coverage['unresolved_chairs']} unresolved "
            f"chair(s) while its own by_class counts "
            f"{by_class[OutcomeClass.UNRESOLVED.value]}"
        )
    if not isinstance(coverage["under_witnessed"], bool):
        raise SchemaRefusal(
            f"Recensor partition receipt's under_witnessed is {coverage['under_witnessed']!r}, "
            "not a boolean"
        )
    page_only = coverage.get("page_granularity_only", 0)
    # Typed before `witnessed_count` subtracts it.
    if not _is_count(page_only):
        raise SchemaRefusal("Recensor partition receipt has invalid page_granularity_only count")
    reading_chairs = sum(by_outcome.get(outcome, 0) for outcome in WITNESS_READING_OUTCOMES)
    if page_only > reading_chairs:
        raise SchemaRefusal(
            "Recensor partition receipt has more page-only contributions than chairs that read"
        )
    # Only `page_granularity_only` decides the formula; the check always runs.
    page_read = schema in PAGE_READ_RECEIPT_SCHEMAS
    if page_read:
        # Typed before `witnessed_count` subtracts it.
        shortfalls = coverage["shortfalls"]
        truncated = shortfalls.get("truncated") if isinstance(shortfalls, dict) else None
        if not _is_count(truncated) or truncated > reading_chairs:
            raise SchemaRefusal(
                "Recensor page-read receipt counts truncated readings that are not a count "
                "of chairs that read the page"
            )
    witnessed = witnessed_count(coverage, page_read=page_read)
    if coverage["under_witnessed"] != (witnessed < coverage["floor"]):
        compared_label = (
            "page read(s)"
            if page_read
            else "act-level completed read(s)"
            if "page_granularity_only" in coverage
            else "completed chair(s)"
        )
        raise SchemaRefusal(
            f"Recensor partition receipt claims under_witnessed="
            f"{coverage['under_witnessed']}, but {witnessed} {compared_label} "
            f"against a floor of {coverage['floor']} says otherwise"
        )
    derived_by_class = {key: 0 for key in _PARTITION_KEYS}
    for outcome, count in by_outcome.items():
        try:
            derived_by_class[classify(ATTESTATORES, outcome).value] += count
        except FatalAccounting as error:
            raise SchemaRefusal(
                "Recensor partition receipt has an unknown witness outcome"
            ) from error
    if by_class != derived_by_class:
        raise SchemaRefusal(
            f"Recensor partition receipt's by_class {by_class} does not fall out of its own "
            f"per-outcome counts, which classify as {derived_by_class}"
        )
    if schema in _SHORTFALL_KEYS:
        shortfall_keys = _SHORTFALL_KEYS[schema]
        # Present: `_validate_coverage` checked the granularity facts each schema carries.
        health_unrecorded = coverage["health_unrecorded"]
        shortfalls = coverage["shortfalls"]
        if not _is_count(health_unrecorded):
            raise SchemaRefusal("Recensor partition receipt has invalid health_unrecorded count")
        if (
            isinstance(shortfalls, dict)
            and "unmeasured" in shortfalls
            and "unmeasured" not in shortfall_keys
        ):
            raise ReceiptVersionMismatch(
                f"receipt schema {schema} cannot carry the unmeasured shortfall; use "
                f"{RECENSOR_PARTITION_RECEIPT_SCHEMA_V3}"
            )
        if (
            not isinstance(shortfalls, dict)
            or set(shortfalls) != shortfall_keys
            or not all(_is_count(value) for value in shortfalls.values())
        ):
            raise SchemaRefusal("Recensor partition receipt has malformed shortfalls")
        configured = coverage["configured"]
        if health_unrecorded > configured or any(
            value > configured for value in shortfalls.values()
        ):
            raise SchemaRefusal(
                "Recensor partition receipt counts more granularity facts than configured chairs"
            )
        if "granularity_basis" in coverage and coverage["granularity_basis"] not in {
            INTERIM_GRANULARITY_BASIS,
            NATIVE_GRANULARITY_BASIS,
        }:
            raise SchemaRefusal(
                f"Recensor partition receipt {schema} does not name an honest granularity "
                "measurement basis"
            )
        if shortfalls["failed"] != by_outcome.get("failed", 0):
            raise SchemaRefusal(
                "Recensor partition receipt's failed shortfall does not derive from "
                "its own failed witness outcomes"
            )


def _validate_reference(reference: Any, what: str) -> None:
    digest_ref(reference, f"Recensor partition receipt {what}")


EMPTY_DENOMINATOR_REASON: Final = (
    "the Designator proposed no acts at all, so this receipt has no denominator to "
    "reconcile; a run that marked nothing out on its pages cannot be complete, "
    "since a missed act is worse than a poorly read one"
)


def _reasons(
    items: list[dict[str, Any]],
    *,
    schema: str = RECENSOR_PARTITION_RECEIPT_SCHEMA_V2,
    links: list[dict[str, Any]] = (),
) -> list[str]:
    # An empty denominator is a reason, not a malformed receipt: refusing would
    # hide the silent failure this boundary exists to show.
    page_read = schema in PAGE_READ_RECEIPT_SCHEMAS
    if not items:
        return [EMPTY_DENOMINATOR_REASON]
    # A page-read run counts units: act entries, `other` entries and page rows.
    counted = "unit" if page_read else "act"
    reasons: list[str] = []
    for item in items:
        act_id = item["act_id"]
        if (
            page_read
            and item["page_disposition"] == "held"
            and item["partition_class"] != OutcomeClass.COMPLETED.value
        ):
            reasons.append(f"unit {act_id} was held by its page reading and is not released")
        if item["partition_class"] != OutcomeClass.COMPLETED.value:
            reasons.append(f"{counted} {act_id} is {item['partition_class']} at the Recensor")
        coverage = item["coverage"]
        if coverage["under_witnessed"]:
            measured = (
                "page reads"
                if page_read
                else "act-level reads"
                if "page_granularity_only" in coverage
                else "completed chairs"
            )
            witnessed = witnessed_count(coverage, page_read=page_read)
            reason = (
                f"{counted} {act_id} is under-witnessed "
                f"({witnessed} {measured} of a floor of {coverage['floor']})"
            )
            shortfalls = coverage.get("shortfalls", {})
            unmeasured = shortfalls.get("unmeasured", 0)
            if unmeasured:
                # The aligner's stop is named apart: it is not the witness falling short.
                reason += (
                    f"; {unmeasured} chair(s) were never compared with this act on at least "
                    "one page because the aligner stopped on its own bound, so their coverage "
                    "is unmeasured, not failed"
                )
                if witness_failure_shortfall(shortfalls):
                    reason += "; other chairs fell short as well, a witness failure"
            reasons.append(reason)
        if coverage["unresolved_chairs"]:
            reasons.append(
                f"{counted} {act_id} has {coverage['unresolved_chairs']} chair(s) with no "
                "outcome yet"
            )
    for link in links:
        if link["outcome"] != "accepted":
            reasons.append(
                f"{link['subject_id']} is {link['outcome']}: only one side says the text runs "
                "across the page break"
            )
    return reasons
