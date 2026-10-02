"""Which pages a person asked the Perlector to read again, and with which decisions.

A page `re-ask` decision (`common.contracts.approval`), current against the
Recensor's latest reviews, asks for an operator re-read of its page
(`common.page_path`, "an operator re-read"). The Perlector reads this when it
runs, so the re-read happens on the next pass that resumes from the Perlector;
the Recensor's next pass then finds the decision stale, since the page it
looked at has a new reading, as a re-ask is meant to.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from common.contracts.approval import ApprovalRecordReference
from common.contracts.stages import PERLECTOR
from common.page_path import OPERATOR_REREAD_FIELD, PAGE_READING_KIND
from common.page_review import published_units
from common.review_decisions import page_reask_decisions, published_basis

Decision = tuple[ApprovalRecordReference, dict[str, Any]]


def stored_decisions(tree) -> dict[str, Decision]:
    """The run's stored review decisions, by the digest the run tree stores each under."""
    return {
        reference.sha256: (reference, record)
        for reference, record in tree.review_decision_records()
    }


def answered_decisions(tree) -> set[str]:
    """The decision hashes some operator re-read of this run already answers."""
    answered: set[str] = set()
    for entry in tree.build_manifest(PERLECTOR, verify_inputs=False)["artifacts"]:
        if entry["kind"] != PAGE_READING_KIND:
            continue
        payload = tree.read_artifact(PERLECTOR, PAGE_READING_KIND, entry["artifact_id"])["payload"]
        block = payload.get(OPERATOR_REREAD_FIELD) if isinstance(payload, Mapping) else None
        if isinstance(block, Mapping):
            answered |= {
                decision["decision_hash"]
                for decision in block.get("decisions") or []
                if isinstance(decision, Mapping)
            }
    return answered


def requested_rereads(tree, stored: dict[str, Decision] | None = None) -> dict[str, list[Decision]]:
    """Each page a current page re-ask asks to be read again, with the decisions not yet answered.

    `{page_id: [(approval reference, record)]}` in hash order. A decision is
    current when it binds to the page as the Recensor's latest reviews show it
    (`published_basis`, as `verbatus decide` bound it), and a page whose current
    decisions disagree asks for nothing (`page_reask_decisions`). A decision an
    operator re-read already answers asks for nothing again, so a pass resumed
    before the Recensor has reviewed the new reading reads no page twice.
    `stored` is `stored_decisions(tree)`, when the caller already holds it.
    """
    stored = stored_decisions(tree) if stored is None else stored
    if not stored:
        return {}
    units = published_units(tree)
    if not units:
        return {}
    by_hash = {record["self_hash"]: (reference, record) for reference, record in stored.values()}
    basis = published_basis(tree.run_id, units, [record for _ref, record in stored.values()])
    answered = answered_decisions(tree)
    requested = {}
    for page_id, summaries in page_reask_decisions(
        basis, [record for _ref, record in stored.values()]
    ).items():
        waiting = [
            by_hash[summary["decision_hash"]]
            for summary in summaries
            if summary["decision_hash"] not in answered
        ]
        if waiting:
            requested[page_id] = waiting
    return requested
