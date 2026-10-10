"""Which pages a routed witness reads: one deterministic, recorded decision per page.

A run's models configuration may seat a witness chair on some pages only
(`[witness_routing]`, `common/chairs/config.py`). Absent, every configured
witness reads every sealed page and nothing here runs; such a run seals
exactly what it sealed before routing existed.

The one rule, `index-and-table.v1`, routes a page to the chair when the
Designator's own page evidence says the page is an index list or a table:

* `surya-table`: Surya's layout tagged at least one block on the page `Table`;
* `zero-detector-records`: the record detector (DAI's own project's YOLO model)
  found no record on the page.

Either signal routes the page; `both` names a page both signals route. The
decision is read only from the Designator's sealed records, before any witness
reads the page, so it never depends on a witness's reading or on the
Perlector's page type. It is recomputed from those records wherever a later
stage needs a page's roster, and the Attestatores publish it per page as a
`witness-routing` record (subject: the page id) and summarise it in
`run-health/witness-routing.json`, so a person can see which pages the routed
chair read and why.

On a page the rule does not route, the routed chair is not part of the page's
roster at all: it has no Testimonium there, is not shown to the Perlector and
is not counted against the witness floor, so an act page is read, shown and
counted exactly as it was with three witnesses. On a routed page it is one
more page witness like any other, and counts toward the floor like any other.
"""

from __future__ import annotations

from typing import Any, Final, Mapping

from common.contracts.errors import FatalAccounting, SchemaRefusal
from common.contracts.stages import DESIGNATOR
from common.page_path import SURYA_BLOCK_KIND, SURYA_PAGE_KIND

ROUTING_KIND: Final = "witness-routing"
ROUTING_SCHEMA: Final = "witness-routing.v1"
ROUTING_SUMMARY_SCHEMA: Final = "witness-routing-summary.v1"
INDEX_AND_TABLE_RULE: Final = "index-and-table.v1"
SURYA_TABLE_LABEL: Final = "Table"
SIGNAL_SURYA_TABLE: Final = "surya-table"
SIGNAL_NO_DETECTOR_RECORD: Final = "zero-detector-records"
SIGNAL_BOTH: Final = "both"
DETECTOR_PAGE_KIND: Final = "detector-page"
ROUTING_FIELDS: Final = frozenset(
    {
        "schema",
        "page_id",
        "page_ordinal",
        "rule",
        "routed_chairs",
        "routed",
        "signal",
        "surya_table_blocks",
        "detector_record_count",
    }
)


def routed_chairs(context) -> Mapping[str, str]:
    """The run's routed witness chairs and their rule, empty when the run routes none."""
    return context.registry.config.witness_routing


def routing_rule(context) -> str | None:
    """The one rule the run's routed chairs share, or `None` when it routes none."""
    rules = set(routed_chairs(context).values())
    if not rules:
        return None
    if rules != {INDEX_AND_TABLE_RULE}:
        raise SchemaRefusal(
            f"the sealed witness routing names rule(s) {sorted(rules)}; this build reads "
            f"only {INDEX_AND_TABLE_RULE!r}"
        )
    return INDEX_AND_TABLE_RULE


def _designator_entries(context) -> list[dict[str, Any]]:
    # `common.stage` reads `common.page_path`, which reads this module lazily.
    from common.stage import stage_manifest

    return stage_manifest(context, DESIGNATOR)["artifacts"]


def _only(entries: list[dict[str, Any]], kind: str, subject: str, page_id: str) -> dict | None:
    found = [entry for entry in entries if entry["kind"] == kind and entry["subject_id"] == subject]
    if len(found) > 1:
        raise FatalAccounting(f"the Designator sealed two {kind} records for page {page_id}")
    return found[0] if found else None


def page_signals(context, page_id: str) -> tuple[dict[str, Any], list[dict[str, str]]]:
    """The two signals for one sealed page, and the Designator records they were read from.

    Refused (`FatalAccounting`) when the page has no Surya census or no
    detector census: a routed run requires both chairs, so a page either
    measure is missing for cannot be routed honestly either way.
    """
    entries = _designator_entries(context)
    census_entry = _only(entries, SURYA_PAGE_KIND, page_id, page_id)
    detector_entry = _only(entries, DETECTOR_PAGE_KIND, page_id, page_id)
    if census_entry is None or detector_entry is None:
        missing = [
            name
            for name, entry in (("Surya census", census_entry), ("detector census", detector_entry))
            if entry is None
        ]
        raise FatalAccounting(
            f"page {page_id} has no {' and no '.join(missing)}, so whether the routed witness "
            "reads it cannot be decided from the Designator's evidence"
        )
    census = context.tree.read_artifact(DESIGNATOR, SURYA_PAGE_KIND, census_entry["artifact_id"])
    payload = census.get("payload")
    subjects = payload.get("block_subjects") if isinstance(payload, dict) else None
    if not isinstance(subjects, list) or payload.get("page_id") != page_id:
        raise FatalAccounting(f"page {page_id}'s Surya census names no block list for the page")
    refs = [context.artifact_ref(DESIGNATOR, SURYA_PAGE_KIND, census_entry["artifact_id"])]
    tables = []
    for subject in subjects:
        entry = _only(entries, SURYA_BLOCK_KIND, subject, page_id)
        if entry is None:
            raise FatalAccounting(f"page {page_id}'s Surya census names block {subject!r} unsealed")
        block = context.tree.read_artifact(DESIGNATOR, SURYA_BLOCK_KIND, entry["artifact_id"])
        if block["payload"].get("label") == SURYA_TABLE_LABEL:
            tables.append(subject)
            refs.append(context.artifact_ref(DESIGNATOR, SURYA_BLOCK_KIND, entry["artifact_id"]))
    detector = context.tree.read_artifact(
        DESIGNATOR, DETECTOR_PAGE_KIND, detector_entry["artifact_id"]
    )["payload"]
    count = detector.get("detection_count") if isinstance(detector, dict) else None
    if not isinstance(count, int) or isinstance(count, bool) or count < 0:
        raise FatalAccounting(f"page {page_id}'s detector census states no detection count")
    refs.append(context.artifact_ref(DESIGNATOR, DETECTOR_PAGE_KIND, detector_entry["artifact_id"]))
    page_ordinal = payload.get("page_ordinal")
    return {
        "page_ordinal": page_ordinal,
        "surya_table_blocks": tables,
        "detector_record_count": count,
    }, refs


def signal_of(table_blocks: list[str], detector_record_count: int) -> str | None:
    """Which signal routes a page under `index-and-table.v1`, or `None` when none does."""
    table = bool(table_blocks)
    empty = detector_record_count == 0
    if table and empty:
        return SIGNAL_BOTH
    if table:
        return SIGNAL_SURYA_TABLE
    if empty:
        return SIGNAL_NO_DETECTOR_RECORD
    return None


def page_routing(context, page_id: str) -> tuple[dict[str, Any], list[dict[str, str]]]:
    """One page's `witness-routing` payload and its Designator inputs, re-derived."""
    rule = routing_rule(context)
    if rule is None:
        raise SchemaRefusal("this run routes no witness, so no page has a routing decision")
    signals, refs = page_signals(context, page_id)
    signal = signal_of(signals["surya_table_blocks"], signals["detector_record_count"])
    return {
        "schema": ROUTING_SCHEMA,
        "page_id": page_id,
        "page_ordinal": signals["page_ordinal"],
        "rule": rule,
        "routed_chairs": sorted(routed_chairs(context)),
        "routed": signal is not None,
        "signal": signal,
        "surya_table_blocks": signals["surya_table_blocks"],
        "detector_record_count": signals["detector_record_count"],
    }, refs


def page_roster(context, page_id: str, declared: set[str]) -> set[str]:
    """The page witnesses of one page: `declared`, less each routed chair not routed to it.

    `declared` is the run's sealed page-witness roster
    (`common.page_path.declared_page_witness_chairs`). A run that routes no
    chair gets `declared` back unchanged, without reading anything.
    """
    routed = set(routed_chairs(context))
    if not routed:
        return set(declared)
    decision, _refs = page_routing(context, page_id)
    return set(declared) if decision["routed"] else set(declared) - routed


def validate_routing_record(context, record: dict[str, Any]) -> None:
    """A sealed `witness-routing` record is exactly the decision its page's evidence gives."""
    payload = record.get("payload")
    if not isinstance(payload, dict) or set(payload) != ROUTING_FIELDS:
        raise SchemaRefusal("a witness-routing record is not its closed schema")
    expected, refs = page_routing(context, record.get("subject_id"))
    if payload != expected or record.get("inputs") != sorted(
        refs, key=lambda ref: ref["relative_path"]
    ):
        raise SchemaRefusal(
            f"the witness-routing record for page {record.get('subject_id')} is not the decision "
            "its page's sealed Designator evidence gives"
        )


def routing_summary(decisions: list[dict[str, Any]]) -> dict[str, Any]:
    """The run-health summary: each page's decision, in page order, and the counts."""
    pages = sorted(decisions, key=lambda decision: decision["page_ordinal"])
    by_signal: dict[str, int] = {}
    for decision in pages:
        key = decision["signal"] or "not-routed"
        by_signal[key] = by_signal.get(key, 0) + 1
    return {
        "schema": ROUTING_SUMMARY_SCHEMA,
        "rule": pages[0]["rule"] if pages else None,
        "routed_chairs": pages[0]["routed_chairs"] if pages else [],
        "pages": len(pages),
        "routed_pages": sum(1 for decision in pages if decision["routed"]),
        "by_signal": dict(sorted(by_signal.items())),
        "decisions": [
            {
                "page_id": decision["page_id"],
                "page_ordinal": decision["page_ordinal"],
                "routed": decision["routed"],
                "signal": decision["signal"],
                "surya_table_blocks": len(decision["surya_table_blocks"]),
                "detector_record_count": decision["detector_record_count"],
            }
            for decision in pages
        ],
    }
