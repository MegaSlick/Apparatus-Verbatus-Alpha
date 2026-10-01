"""Record one operator review decision about a held unit or page of a run.

This and `advance.py` are the operator package's only approval writers; this
one writes only the review action (`approval-record.v1`, built by
`build_review_decision_record`). A decision binds to the basis of the review
the person read: the Recensor's latest published review of the unit, or of
every unit on the page (`common.review_decisions.published_basis`), computed
here from the run tree, so the Recensor's next pass finds it current while
nothing it looked at changes. The decision changes nothing by itself: the
Recensor applies it on its next pass (`pipeline/5_recensor/CONTRACT.md`,
"Operator review decisions").

Refused, before anything is written: a unit or page the Recensor's latest
pass did not review; a decision its subject does not allow (a release with no
hold of its own to clear, a release of a reading no decision can send to
export, no missed act on an unread page); and a run whose Archetypus has
established a reading or whose Armarium has published its export, where a
decision recorded now could reach nothing.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Final

from common.contracts.approval import (
    APPROVER,
    PAGE_SCOPE,
    REVIEW_DECISIONS,
    UNIT_SCOPE,
    ApprovalRecordReference,
    build_review_decision_record,
)
from common.contracts.errors import ApprovalRefusal
from common.contracts.stages import ARCHETYPUS, ARMARIUM, PERLECTOR, RECENSOR
from common.page_path import PAGE_READING_KIND, PERLECTIO_KIND
from common.page_review import REVIEW_KIND, REVIEW_OPERATION, override_refusal
from common.review_decisions import CURRENT, published_basis, review_decision
from common.runtree.store import RunTree
from common.stage import latest_attempt

MAX_DECISION_REASON_CHARACTERS: Final = 4_000
# What each decision asks of the run after it, in words.
NEXT_STEP: Final = {
    "release": "the Recensor clears the unit's own holds",
    "exclude": "the Recensor excludes the unit as not an act, citing this decision",
    "hold": "the Recensor keeps the subject held with this finding",
    "no-missed-act": "the Recensor clears the page's holds",
    "missed-act": "the Recensor holds the page as missing an act",
    "re-shoot": "the Recensor holds the page and records the re-shoot request",
}
RERUN_MISSING: Final = (
    "the Recensor records the request in its review-decisions `requests`, and holds the "
    "subject until it is read again. Nothing can start that re-read yet: the Perlector reads "
    "a page as attempt 1 and its one re-ask as attempt 2 and refuses any other "
    "(`common.page_path.page_reading_attempt`), and the reading denominator has no rule for "
    "which of two first readings of a page is current, so a re-run is a new run of the "
    "submission until those exist"
)


@dataclass(frozen=True)
class PreparedDecision:
    """One decision, built and checked current against the run's latest reviews."""

    record: dict[str, Any]
    subject: str
    basis_digest: str
    held_codes: tuple[str, ...]


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def published_units(tree: RunTree) -> list[dict[str, Any]]:
    """The Recensor's latest review of every unit, as `published_basis` reads them.

    Each unit's page is the one its page reading names, and its own and page
    holds its Perlectio's, as sealed; a page row has no reading and holds
    for its page.
    """
    reviews: dict[str, list[dict[str, Any]]] = {}
    for entry in tree.build_manifest(RECENSOR, verify_inputs=False)["artifacts"]:
        if entry["kind"] == REVIEW_KIND:
            reviews.setdefault(entry["subject_id"], []).append(
                tree.read_artifact(RECENSOR, REVIEW_KIND, entry["artifact_id"])
            )
    units = []
    for act_id, records in sorted(reviews.items()):
        review = latest_attempt(records, f"review of {act_id}", operation=REVIEW_OPERATION)
        payload = {
            key: value for key, value in review["payload"].items() if key != "attempt_ordinal"
        }
        page = tree.read_artifact_reference(
            payload["page_reading_ref"], stage=PERLECTOR, kind=PAGE_READING_KIND
        )
        holds: list[str] = []
        page_holds: list[str] = []
        if payload["perlectio_ref"] is not None:
            reading = tree.read_artifact_reference(
                payload["perlectio_ref"], stage=PERLECTOR, kind=PERLECTIO_KIND, subject_id=act_id
            )
            holds, page_holds = reading["payload"]["holds"], reading["payload"]["page_holds"]
        units.append(
            {
                "act_id": act_id,
                "page_id": page["subject_id"],
                "outcome": review["outcome"],
                "payload": payload,
                "unit_holds": holds,
                "page_holds": page_holds,
            }
        )
    return units


def _require_open(tree: RunTree) -> None:
    """Refuse a run past the point a decision recorded now could reach."""
    for stage, kind, what in (
        (ARMARIUM, "export", "the Armarium has published its export"),
        (ARCHETYPUS, "archetypus", "the Archetypus has established a reading"),
    ):
        found = [
            entry
            for entry in tree.build_manifest(stage, verify_inputs=False)["artifacts"]
            if entry["kind"] == kind
        ]
        if found:
            raise ApprovalRefusal(
                f"run {tree.run_id}: {what}, so a decision recorded now could reach nothing. "
                "Record it in a new run of this submission instead"
            )


def prepare_decision(
    tree: RunTree,
    *,
    decision: str,
    reason: str,
    unit: str | None = None,
    page: int | None = None,
    finding: str | None = None,
    timestamp: str | None = None,
) -> PreparedDecision:
    """Build one decision about the unit keyed `unit` (`p1:2`) or page ordinal `page`.

    It binds to that subject's basis in the Recensor's latest reviews and is
    checked current there before it is returned, so nothing stale or unknown
    is ever written.
    """
    if (unit is None) == (page is None):
        raise ApprovalRefusal("a decision names exactly one unit (by its key) or one page")
    if not isinstance(reason, str) or not reason.strip():
        raise ApprovalRefusal("a decision with no reason is unreviewable later; give --reason")
    if len(reason) > MAX_DECISION_REASON_CHARACTERS:
        raise ApprovalRefusal(
            f"a decision reason is bounded to {MAX_DECISION_REASON_CHARACTERS} characters"
        )
    scope = UNIT_SCOPE if unit is not None else PAGE_SCOPE
    if decision not in REVIEW_DECISIONS[scope]:
        raise ApprovalRefusal(
            f"{decision!r} is not a {scope} decision; a {scope} decision is one of "
            f"{', '.join(REVIEW_DECISIONS[scope])}"
        )
    _require_open(tree)
    units = published_units(tree)
    if not units:
        raise ApprovalRefusal(f"run {tree.run_id} has no Recensor review to decide about")
    stored = [record for _reference, record in tree.review_decision_records()]
    basis = published_basis(tree.run_id, units, stored)
    if scope == UNIT_SCOPE:
        matches = [act_id for act_id, entry in basis["units"].items() if entry["act_key"] == unit]
        if not matches:
            raise ApprovalRefusal(
                f"the Recensor's latest pass reviewed no unit {unit!r} in run {tree.run_id}"
            )
        [subject] = matches
        entry = basis["units"][subject]
        page_id, digest = entry["page_id"], entry["basis_digest"]
        held = next(u["payload"]["hold_codes"] for u in units if u["act_id"] == subject)
        if decision == "release":
            row = _row(units, subject, entry)
            if (refusal := override_refusal(row)) is not None and row["hold_codes"]:
                raise ApprovalRefusal(
                    f"releasing {unit} cannot send it to export: {refusal}. Exclude it, or "
                    "hold it with a finding"
                )
        what = f"unit {unit} ({subject})"
    else:
        matches = [pid for pid, entry in basis["pages"].items() if entry["page_ordinal"] == page]
        if not matches:
            raise ApprovalRefusal(
                f"the Recensor's latest pass reviewed nothing on page {page} of run {tree.run_id}"
            )
        [subject] = matches
        page_id, digest = subject, basis["pages"][subject]["basis_digest"]
        held = basis["pages"][subject]["page_codes"]
        what = f"page {page} ({subject})"
    record = build_review_decision_record(
        run_id=tree.run_id,
        scope=scope,
        subject_id=subject,
        page_id=page_id,
        decision=decision,
        finding=finding,
        basis_digest=digest,
        reason=reason,
        timestamp=timestamp or _now(),
    )
    summary = review_decision(record, basis)
    if summary["state"] != CURRENT:
        raise ApprovalRefusal(
            f"a decision about {what} would be stale ({summary['stale_because']}); read the "
            "run's review again"
        )
    return PreparedDecision(
        record=record, subject=what, basis_digest=digest, held_codes=tuple(sorted(held))
    )


def _row(units: list[dict[str, Any]], act_id: str, entry: dict[str, Any]) -> dict[str, Any]:
    """The unit as `page_review.override_refusal` reads a row: its class and own holds."""
    unit = next(u for u in units if u["act_id"] == act_id)
    return {
        "act_key": entry["act_key"],
        "class": entry["unit_class"],
        "perlectio_ref": unit["payload"]["perlectio_ref"],
        "hold_codes": sorted(set(unit["unit_holds"]) | set(unit["page_holds"])),
    }


def record_decision(tree: RunTree, prepared: PreparedDecision) -> ApprovalRecordReference:
    """Store the prepared decision where the Recensor reads every decision of the run."""
    reference, _ = tree.write_approval_record(prepared.record)
    return reference


def report(prepared: PreparedDecision, reference: ApprovalRecordReference) -> list[str]:
    """What was recorded, and what the run does with it next, in words."""
    review = prepared.record["review"]
    decision = review["decision"]
    finding = f" with finding {review['finding']}" if review["finding"] else ""
    effect = RERUN_MISSING if decision == "re-ask" else NEXT_STEP[decision]
    return [
        f"Recorded: {decision}{finding} of {prepared.subject}, by {APPROVER} at "
        f"{prepared.record['timestamp']}.",
        f"It binds to the review basis {prepared.basis_digest}; held now by: "
        f"{', '.join(prepared.held_codes) or 'nothing'}.",
        f"Decision record: {reference.relative_path} ({reference.sha256})",
        f"When the Recensor runs again, {effect}.",
        "Next: resume the run from the recensor (`--from recensor --to armarium`), which "
        "applies every decision recorded; it goes on to export once nothing is held, or once "
        "`verbatus advance --stage recensor` passes its new seal.",
    ]
