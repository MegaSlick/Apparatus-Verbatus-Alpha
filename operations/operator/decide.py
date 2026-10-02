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

An `edit` records a person's corrected text for one held unit, with an
optional note; when the Recensor accepts the unit, the Archetypus establishes
that text as its reading, labelled "corrected by a person"
(`common/correction.py`), and the export shows the model's reading beside it.
A page `re-ask` is read again by the Perlector when the run resumes from it
(`common/page_reread.py`), as the page's next operator re-read.

Refused, before anything is written: a unit or page the Recensor's latest
pass did not review; a decision its subject does not allow (a release with no
hold of its own to clear, a release or edit of a reading no decision can send
to export, an edit of a unit nothing holds, no missed act on an unread page);
and a run whose Archetypus has established a reading or whose Armarium has
published its export, where a decision recorded now could reach nothing.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Final

from common.contracts.approval import (
    APPROVER,
    EDIT_DECISION,
    PAGE_SCOPE,
    REVIEW_DECISIONS,
    UNIT_SCOPE,
    ApprovalRecordReference,
    build_review_decision_record,
)
from common.contracts.canonical import text_sha256
from common.contracts.errors import ApprovalRefusal
from common.contracts.stages import ARCHETYPUS, ARMARIUM
from common.page_review import override_refusal, published_units
from common.review_decisions import CURRENT, published_basis, review_decision
from common.runtree.store import RunTree

# What each decision asks of the run after it, in words.
NEXT_STEP: Final = {
    "release": "the Recensor clears the unit's own holds",
    "exclude": "the Recensor excludes the unit as not an act, citing this decision",
    "hold": "the Recensor keeps the subject held with this finding",
    "no-missed-act": "the Recensor clears the page's holds",
    "missed-act": "the Recensor holds the page as missing an act",
    "re-shoot": "the Recensor holds the page and records the re-shoot request",
    EDIT_DECISION: (
        "the Recensor clears the unit's own holds, and once nothing else holds it the "
        "Archetypus establishes your text as its reading, labelled corrected by a person, "
        "with the model's reading kept beside it"
    ),
}
# What a re-ask asks of the run after it, by scope.
REREAD_PAGE: Final = (
    "when the run resumes from the Perlector (--from perlector --to armarium), the Perlector "
    "reads the page again as its next operator re-read, bound to this decision; that reading "
    "becomes the page's current one, its earlier readings stay in the run tree marked "
    "superseded, and the Recensor reviews the new reading. On a pod the re-read is paid GPU "
    "work and needs the project lead's permission like any pod start"
)
REREAD_UNIT: Final = (
    "the Recensor records the request in its review-decisions `requests` and holds the unit; "
    "the Perlector reads whole pages, so to read it again record a re-ask of its page"
)


@dataclass(frozen=True)
class PreparedDecision:
    """One decision, built and checked current against the run's latest reviews."""

    record: dict[str, Any]
    subject: str
    basis_digest: str
    held_codes: tuple[str, ...]
    # The digest of an edit's text, which its confirmation names; None otherwise.
    text_sha256: str | None = None


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


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
    text: str | None = None,
    note: str | None = None,
) -> PreparedDecision:
    """Build one decision about the unit keyed `unit` (`p1:2`) or page ordinal `page`.

    It binds to that subject's basis in the Recensor's latest reviews, so
    nothing unknown is ever written; `record_decision` checks it current again
    just before it writes. The reason, an edit's `text` and its `note` are
    bounded by the approval contract (`common.contracts.approval`), which the
    record's builder enforces.
    """
    if (unit is None) == (page is None):
        raise ApprovalRefusal("a decision names exactly one unit (by its key) or one page")
    if not isinstance(reason, str) or not reason.strip():
        raise ApprovalRefusal("a decision with no reason is unreviewable later; give --reason")
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
        if decision == EDIT_DECISION:
            if (refusal := override_refusal(_row(units, subject, entry), edit=True)) is not None:
                raise ApprovalRefusal(
                    f"correcting {unit} cannot send it to export: {refusal}. Exclude it, or "
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
        text=text,
        note=note,
    )
    return PreparedDecision(
        record=record,
        subject=what,
        basis_digest=digest,
        held_codes=tuple(sorted(held)),
        text_sha256=text_sha256(text) if decision == EDIT_DECISION else None,
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
    """Store the prepared decision where the Recensor reads every decision of the run.

    The run may have moved on while the person confirmed it: a stage after the
    Recensor ran, or another decision changed the subject's basis (an exclusion
    changes its page's). Both are checked again here, just before the write,
    so a decision that is no longer current or could reach nothing is refused
    and nothing is written.
    """
    _require_open(tree)
    stored = [record for _reference, record in tree.review_decision_records()]
    summary = review_decision(
        prepared.record, published_basis(tree.run_id, published_units(tree), stored)
    )
    if summary["state"] != CURRENT:
        raise ApprovalRefusal(
            f"the decision about {prepared.subject} is stale ({summary['stale_because']}); "
            "read the run's review again"
        )
    reference, _ = tree.write_approval_record(prepared.record)
    return reference


def report(prepared: PreparedDecision, reference: ApprovalRecordReference) -> list[str]:
    """What was recorded, and what the run does with it next, in words."""
    review = prepared.record["review"]
    decision = review["decision"]
    finding = f" with finding {review['finding']}" if review["finding"] else ""
    if decision == "re-ask":
        effect = REREAD_PAGE if review["scope"] == PAGE_SCOPE else REREAD_UNIT
    else:
        effect = NEXT_STEP[decision]
    text = (
        [f"The corrected text has digest {prepared.text_sha256}; note: {review['note'] or 'none'}."]
        if decision == EDIT_DECISION
        else []
    )
    return [
        f"Recorded: {decision}{finding} of {prepared.subject}, by {APPROVER} at "
        f"{prepared.record['timestamp']}.",
        *text,
        f"It binds to the review basis {prepared.basis_digest}; held now by: "
        f"{', '.join(prepared.held_codes) or 'nothing'}.",
        f"Decision record: {reference.relative_path} ({reference.sha256})",
        f"When the Recensor runs again, {effect}.",
        _next_step(decision == "re-ask" and review["scope"] == PAGE_SCOPE),
    ]


def _next_step(reread: bool) -> str:
    """How the run goes on after the decision: from the Perlector for a page re-ask, which
    must read the page again before the Recensor reviews it, else from the Recensor."""
    stage = "perlector" if reread else "recensor"
    what = (
        "reads the page again and then has the Recensor apply every decision recorded"
        if reread
        else "applies every decision recorded"
    )
    return (
        f"Next: resume the run from the {stage}, which {what}: `verbatus run --run-id <run> "
        f"--from {stage} --to armarium` for a run this tool started, or `pod_run --from "
        f"{stage} --to armarium` on its pod. It goes on to export once nothing is held, or "
        "once `verbatus advance --stage recensor` passes its new seal."
    )
