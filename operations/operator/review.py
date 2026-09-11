"""Project sealed run-tree evidence into a read-only operator shape."""

from __future__ import annotations

import io
import json
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from common.contracts.approval import validate_approval_record
from common.contracts.canonical import digest_bytes
from common.contracts.errors import ContractError, SchemaRefusal
from common.contracts.identities import artifact_id
from common.contracts.stages import (
    ARCHETYPUS,
    ARMARIUM,
    ATTESTATORES,
    DESIGNATOR,
    EXEMPLAR,
    PERLECTOR,
    RECENSOR,
    STAGES,
)
from common.runtree.store import RunTree
from common.stage import latest_attempt

from .advance import ADVANCE_SUBJECT_PREFIX, verify_sealed_boundary
from .errors import ErrorCode, OperatorError

MAX_REVIEW_ITEMS_BYTES = 16 * 1024 * 1024
MAX_REVIEW_ITEMS = 50_000
_REVIEW_ITEMS_MEMBER = "review-items.jsonl"

# The review queue was bounded and the images beside it were not. Every sealed
# page and every act crop is still read whole to verify its digest, and the
# projection verifies all of them in one pass, so a parish-sized run met no
# limit, only the memory of the machine. The evidence would be intact on disk
# and unreadable on the one surface a person reads.
#
# This is a bound, not the eventual answer: a console for real parish volumes
# should verify one image at a time as the renderer fetches it, which is a
# change to what the child receives rather than an audit repair. The number is
# the largest that still passes through this pipe with room for the JSON copy
# on both sides, so it refuses only runs the one-pass verification design could
# not have served anyway — and it refuses them by name instead of by
# exhaustion (GOVERNANCE 2).
MAX_PROJECTED_IMAGE_BYTES = 256 * 1024 * 1024


class _ImageBudget:
    """One running allowance over every image the projection embeds."""

    __slots__ = ("_limit", "_spent")

    def __init__(self, limit: int = MAX_PROJECTED_IMAGE_BYTES) -> None:
        self._limit = limit
        self._spent = 0

    def spend(self, count: int, what: str) -> None:
        self._spent += count
        if self._spent > self._limit:
            raise OperatorError(
                ErrorCode.CONSOLE_TREE_UNREADABLE,
                detail=(
                    f"this run's page and crop images and its export bundle pass "
                    f"{self._limit} bytes at {what}, "
                    "which is more than the console can project in one read-only view; the "
                    "run tree is intact and unchanged, and a narrower selection can be "
                    "reviewed while this limit stands"
                ),
            )


def _budgeted_image_bytes(
    tree: RunTree, relative_path: str, budget: _ImageBudget, what: str
) -> bytes:
    """Charge an image against the budget before it is anywhere near memory.

    `RunTree.read_bytes` loads the whole file and only then hands its length to
    `spend`, so a page larger than the allowance was fully resident at the exact
    moment the limit existed to refuse it -- on a parish-sized image that is the
    console dying rather than refusing by name (GOVERNANCE 2). The size on disk
    is charged first and the read is then bounded by what was charged, so a file
    that grew between the two spends nothing it was not allowed.
    """

    path = tree.resolve(relative_path)
    try:
        size = path.stat().st_size
    except OSError as error:
        raise OperatorError(
            ErrorCode.CONSOLE_TREE_UNREADABLE, detail=f"{what} could not be measured: {error}"
        ) from error
    budget.spend(size, what)
    with path.open("rb") as handle:
        data = handle.read(size + 1)
    if len(data) != size:
        raise OperatorError(
            ErrorCode.CONSOLE_TREE_UNREADABLE,
            detail=f"{what} changed size while the console was reading it",
        )
    return data


@dataclass(frozen=True, slots=True)
class ReviewProjection:
    """Only display values and immutable byte references; never a writable tree."""

    run_id: str
    stage_records: tuple[dict[str, Any], ...]
    boundaries: tuple[dict[str, Any], ...]
    pages: tuple[dict[str, Any], ...]
    acts: tuple[dict[str, Any], ...]
    review_items: tuple[dict[str, Any], ...] | None
    advance_records: tuple[dict[str, Any], ...]
    # The pre-export view (independent audit of 2026-09-10, F3). Before this,
    # the projection refused the whole run whenever the Armarium had not
    # exported -- exactly the moment a person needs to see the images, the
    # readings and the reason the run stopped. These four are derived from the
    # same `stage_records` snapshot as everything above, and default so the
    # seven fields callers construct by position or keyword stay as pinned.
    #
    # `export`      whether an export record exists, and what that means for
    #               the rows above (`present`, `record_ref`, `note`)
    # `progress`    one row per stage: sealed, unsealed (interrupted or still
    #               running), not-run, or seal-invalid -- a stage that has not
    #               run is named as that, never as corruption, and a seal that
    #               no longer verifies is named as that, never as not-run
    # `holds`       every act the Designator or the Recensor left unresolved,
    #               with the recorded reason, pre- and post-export alike
    # `next_action` the one supported continuation in plain words, and what
    #               `advance` does and does not do about a hold
    export: dict[str, Any] = field(default_factory=dict)
    progress: tuple[dict[str, Any], ...] = ()
    holds: tuple[dict[str, Any], ...] = ()
    next_action: dict[str, Any] = field(default_factory=dict)


class ReadOnlyRun:
    """The renderer's intentionally tiny capability over an already-open run."""

    __slots__ = ("_tree",)

    def __init__(self, root: str | Path, run_id: str):
        self._tree = RunTree(Path(root), run_id)

    def projection(self) -> ReviewProjection:
        tree = self._tree
        try:
            boundaries: list[dict[str, Any]] = []
            stage_records: list[dict[str, Any]] = []
            for stage in STAGES:
                manifest = tree.build_manifest(stage, verify_inputs=False)
                records = [_record_row(tree, stage, row) for row in manifest["artifacts"]]
                stage_records.extend(records)
                seals = [row for row in records if row["kind"] == "stage-seal"]
                if not seals:
                    boundaries.append(
                        {
                            "stage": stage,
                            "sealed": False,
                            "seal_present": False,
                            "seal_note": "this stage has no stored stage-seal",
                            "census": [],
                        }
                    )
                    continue
                current = latest_attempt(
                    [row["record"] for row in seals], f"{stage} stage seal", operation="seal"
                )
                seal = next(row for row in seals if row["artifact_id"] == current["artifact_id"])
                # The tree's protection is kept beside 21F's display rows: the
                # boundary is "sealed" only when its stored seal still verifies
                # against the evidence on disk, and the note says why not.
                try:
                    verify_sealed_boundary(tree, stage)
                except ContractError as error:
                    seal_valid = False
                    seal_note = str(error)
                else:
                    seal_valid = True
                    seal_note = None
                boundaries.append(
                    {
                        "stage": stage,
                        "sealed": seal_valid,
                        "seal_present": True,
                        "seal_note": seal_note,
                        "seal_artifact_id": seal["artifact_id"],
                        "seal_digest": seal["record_ref"]["sha256"],
                        "census": seal["record"]["payload"]["census"],
                        "seal_record_ref": seal["record_ref"],
                        # Every completion seal remains visible.  `current` describes
                        # the seal that the boundary presently names; it does not
                        # suppress an earlier attempt or decide what an operator does.
                        "seals": tuple(
                            {
                                "artifact_id": candidate["artifact_id"],
                                "census": candidate["record"]["payload"]["census"],
                                "current": candidate["artifact_id"] == seal["artifact_id"],
                                "record_ref": candidate["record_ref"],
                            }
                            for candidate in seals
                        ),
                    }
                )
            progress = _progress(boundaries, stage_records)
            # One allowance across pages and crops together: the projection
            # verifies them all in one pass, so bounding either alone would
            # bound nothing.
            budget = _ImageBudget()
            if _export_rows(stage_records):
                payload, export_ref = _armarium_payload(tree, stage_records)
                pages = tuple(_image_row(tree, row, export_ref, budget) for row in payload["pages"])
                # The two act lists carry different writer contracts: Armarium
                # attaches `source_regions` to every delivered act and never to
                # a non-delivered one (pipeline/7_armarium/run.py builds review
                # entries without it). A delivered act missing its crop list is
                # therefore a damaged record; a non-delivered act without one
                # is the record as written.
                acts = tuple(
                    _act_row(tree, row, export_ref, budget) for row in payload["delivered"]
                ) + tuple(
                    _act_row(tree, row, export_ref, budget, requires_crops=False)
                    for row in payload["non_delivered"]
                )
                review_items = _review_items(tree, payload, export_ref, budget)
                export = {
                    "present": True,
                    "record_ref": export_ref,
                    "note": (
                        "the Armarium export exists; the pages and acts shown are its "
                        "accounting, verified against the sealed images"
                    ),
                }
            else:
                # No export: the run stopped, or was stopped, before the
                # Armarium. Everything a person needs to see at that moment --
                # the sealed pages, the crops, the latest reading and the
                # review that held it -- is already sealed by the stages that
                # did run, so it is projected from those records, each row
                # naming the record it came from. Nothing here is a delivered
                # result, and `export` says so where a reader reads.
                pages = _sealed_pages(tree, stage_records, budget)
                acts = _progressive_acts(tree, stage_records, budget)
                review_items = None
                export = {
                    "present": False,
                    "record_ref": None,
                    "note": (
                        "there is no Armarium export record; this run has no completed "
                        "export, so the pages, crops, readings and reviews shown are read "
                        "from the sealed evidence of the stages that did run, and none of "
                        "them is a delivered result"
                    ),
                }
            holds = _holds(stage_records)
            return ReviewProjection(
                tree.run_id,
                tuple(stage_records),
                tuple(boundaries),
                pages,
                acts,
                review_items,
                _advance_records(
                    tree,
                    {row["stage"]: row for row in boundaries if row.get("seal_present")},
                ),
                export=export,
                progress=progress,
                holds=holds,
                next_action=_next_action(progress, export, holds),
            )
        except (ContractError, KeyError, OSError, TypeError, ValueError) as error:
            # The rendered detail must retain the evidence path from the
            # underlying refusal; its repair instruction requires a named file.
            raise OperatorError(
                ErrorCode.CONSOLE_TREE_UNREADABLE,
                detail=(f"run-tree evidence could not be read: {type(error).__name__}: {error}"),
            ) from error


def _record_row(tree: RunTree, stage: str, manifest_row: dict[str, Any]) -> dict[str, Any]:
    """Bind displayed record fields and their address to one filesystem read.

    Every stage outcome must remain visible; this projection cannot collapse
    records into a summary or pair fields with a digest from a later read.
    """

    record, record_bytes = tree.read_artifact_snapshot(
        stage, manifest_row["kind"], manifest_row["artifact_id"]
    )
    record_digest = digest_bytes(record_bytes)
    if record_digest != manifest_row["sha256"]:
        raise SchemaRefusal(
            f"{manifest_row['relative_path']}: changed while the review inventory was being "
            "read; its record body and immutable address cannot be paired safely"
        )
    return {
        "stage": stage,
        "artifact_id": record["artifact_id"],
        "kind": record["kind"],
        "subject_id": record["subject_id"],
        "outcome": record["outcome"],
        "record_ref": {
            "relative_path": manifest_row["relative_path"],
            "sha256": record_digest,
        },
        "record": record,
    }


def _armarium_payload(
    tree: RunTree, stage_records: list[dict[str, Any]]
) -> tuple[dict[str, Any], dict[str, str]]:
    """Use the stage-record snapshot; rereading could mix two export versions."""

    export_id = artifact_id(ARMARIUM, "export", "export", None)
    expected_path = tree.artifact_path(ARMARIUM, "export", export_id)
    matches = [
        row
        for row in stage_records
        if row["stage"] == ARMARIUM and row["kind"] == "export" and row["artifact_id"] == export_id
    ]
    if not matches:
        # "appeared 0 times" named a count where the condition is a missing
        # stage: a run halted before Armarium has no export to review at all,
        # and the operator should be told that rather than a tally.
        raise OperatorError(
            ErrorCode.CONSOLE_TREE_UNREADABLE,
            detail=(
                f"there is no Armarium export record at {expected_path}; this run has no "
                "completed export, so no review projection can be built from it"
            ),
        )
    if len(matches) != 1:
        raise OperatorError(
            ErrorCode.CONSOLE_TREE_UNREADABLE,
            detail=(
                f"the Armarium export record {expected_path} appeared {len(matches)} times; "
                "review requires exactly one immutable export snapshot"
            ),
        )
    export_row = matches[0]
    payload = export_row["record"]["payload"]
    if not isinstance(payload, dict):
        raise OperatorError(
            ErrorCode.CONSOLE_TREE_UNREADABLE,
            detail=f"the Armarium export record {expected_path} payload is not an object",
        )
    for name in ("pages", "delivered", "non_delivered"):
        if name not in payload or not isinstance(payload[name], list):
            raise OperatorError(
                ErrorCode.CONSOLE_TREE_UNREADABLE,
                detail=(
                    f"the Armarium export record {expected_path} {name} value is missing or "
                    "not a list"
                ),
            )
    return payload, export_row["record_ref"]


def _verified_export_blob_digest(
    tree: RunTree,
    *,
    stage: str,
    path: Any,
    expected_digest: Any,
    description: str,
    export_ref: dict[str, str],
    budget: _ImageBudget,
    record_label: str = "the Armarium export record",
) -> str:
    """A fresh digest must not repair a contradictory recorded path or digest.

    `record_label` names the kind of record whose claim is being checked: the
    Armarium export for the post-export rows, the Exemplar page or Designator
    region record for the pre-export ones. The check is the same either way --
    the recorded path must be the digest's own content address and the bytes
    there must still hash to it -- and a refusal names the record and the file.
    """

    export_path = export_ref["relative_path"]
    if not isinstance(path, str) or not isinstance(expected_digest, str):
        raise OperatorError(
            ErrorCode.CONSOLE_TREE_UNREADABLE,
            detail=(
                f"{record_label} {export_path} {description} has no immutable image path and digest"
            ),
        )
    expected_path = tree.blob_path(stage, expected_digest)
    if path != expected_path:
        raise OperatorError(
            ErrorCode.CONSOLE_TREE_UNREADABLE,
            detail=(
                f"{record_label} {export_path} {description} claims digest "
                f"{expected_digest} but names {path}, not its content-addressed path "
                f"{expected_path}"
            ),
        )
    data = _budgeted_image_bytes(tree, path, budget, description)
    actual_digest = digest_bytes(data)
    if actual_digest != expected_digest:
        raise OperatorError(
            ErrorCode.CONSOLE_TREE_UNREADABLE,
            detail=(
                f"{record_label} {export_path} {description} names {path} with "
                f"digest {expected_digest}, but its bytes have digest {actual_digest}"
            ),
        )
    return actual_digest


# --- The pre-export view -------------------------------------------------------
#
# Every helper below reads only `stage_records` -- the one snapshot the
# projection already took, each row bound to its digest by `_record_row` -- and
# the image bytes those records name. Nothing re-opens the tree for a second
# opinion, so one projection cannot describe two versions of a run.

_UNRESOLVED_REVIEW_OUTCOMES = frozenset({"held-for-review", "recovery-requested", "failed"})


def _export_rows(stage_records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    export_id = artifact_id(ARMARIUM, "export", "export", None)
    return [
        row
        for row in stage_records
        if row["stage"] == ARMARIUM and row["kind"] == "export" and row["artifact_id"] == export_id
    ]


def _records_of(
    stage_records: list[dict[str, Any]], stage: str, kind: str, subject_id: str | None = None
) -> list[dict[str, Any]]:
    return [
        row
        for row in stage_records
        if row["stage"] == stage
        and row["kind"] == kind
        and (subject_id is None or row["subject_id"] == subject_id)
    ]


def _payload_of(row: dict[str, Any], what: str) -> dict[str, Any]:
    payload = row["record"].get("payload")
    if not isinstance(payload, dict):
        raise OperatorError(
            ErrorCode.CONSOLE_TREE_UNREADABLE,
            detail=f"{what} {row['record_ref']['relative_path']} payload is not an object",
        )
    return payload


def _latest(
    stage_records: list[dict[str, Any]],
    stage: str,
    kind: str,
    subject_id: str,
    *,
    operation: str,
) -> dict[str, Any] | None:
    """The current attempt of one act's records, by the shared attempt rule.

    `latest_attempt` is the same function every stage uses to choose the
    reading or review it acts on, so what the console shows as "the latest" is
    what the pipeline itself would consume -- and its refusals (a duplicated
    ordinal, an attempt id that does not re-derive, a gap in the sequence) are
    refusals of the evidence by name, not something this view smooths over.
    """
    rows = _records_of(stage_records, stage, kind, subject_id)
    if not rows:
        return None
    current = latest_attempt(
        [row["record"] for row in rows], f"{kind} of {subject_id}", operation=operation
    )
    return next(row for row in rows if row["artifact_id"] == current["artifact_id"])


def _progress(
    boundaries: list[dict[str, Any]], stage_records: list[dict[str, Any]]
) -> tuple[dict[str, Any], ...]:
    """One row per stage saying what state its evidence is in, in plain words.

    Four states, because a reader has to tell four different things apart and
    the tree can show any of them: a stage that completed and sealed; one that
    wrote records but never sealed (interrupted, or still running); one that
    has not run at all; and one whose stored seal no longer verifies against
    what is on disk. The last is never reported as "not run" and the third is
    never reported as damage -- a legitimate absence and a broken promise are
    different facts (GOVERNANCE 2, 10).
    """
    by_stage = {row["stage"]: row for row in boundaries}
    rows = []
    for stage in STAGES:
        boundary = by_stage[stage]
        artifacts = [
            row for row in stage_records if row["stage"] == stage and row["kind"] != "stage-seal"
        ]
        if boundary["seal_present"] and boundary["sealed"]:
            state = "sealed"
            note = f"completed and sealed; {len(artifacts)} record(s)"
        elif boundary["seal_present"]:
            state = "seal-invalid"
            note = (
                "a completion seal is stored but no longer verifies against the evidence on "
                f"disk: {boundary['seal_note']}"
            )
        elif artifacts:
            state = "unsealed"
            note = (
                f"{len(artifacts)} record(s) written and no completion seal: this stage was "
                "interrupted or is still running, so nothing it wrote is complete"
            )
        else:
            state = "not-run"
            note = "no record and no seal: this stage has not run"
        rows.append(
            {"stage": stage, "state": state, "artifact_count": len(artifacts), "note": note}
        )
    return tuple(rows)


def _sealed_pages(
    tree: RunTree, stage_records: list[dict[str, Any]], budget: _ImageBudget
) -> tuple[dict[str, Any], ...]:
    """Every page the Exemplar accounted for, sealed or refused, from its own record.

    The same row shape the export path produces, so one renderer reads both:
    a sealed page names its image and the digest the bytes were just checked
    against; a refused page stays visible with its reason and no image.
    """
    rows = []
    for row in _records_of(stage_records, EXEMPLAR, "page"):
        record = row["record"]
        payload = _payload_of(row, "the Exemplar page record")
        projected = {
            "ordinal": payload.get("ordinal"),
            "page_id": record["subject_id"] if record["outcome"] == "sealed" else None,
            "outcome": record["outcome"],
            "reason": payload.get("reason"),
            "record_ref": row["record_ref"],
        }
        if record["outcome"] != "sealed":
            rows.append({**projected, "image_path": None, "image_sha256": None})
            continue
        digest = _verified_export_blob_digest(
            tree,
            stage=EXEMPLAR,
            path=payload.get("image_path"),
            expected_digest=payload.get("source_sha256"),
            description=f"page {payload.get('ordinal')!r}",
            export_ref=row["record_ref"],
            budget=budget,
            record_label="the Exemplar page record",
        )
        rows.append({**projected, "image_path": payload["image_path"], "image_sha256": digest})
    rows.sort(key=lambda page: (not isinstance(page["ordinal"], int), page["ordinal"] or 0))
    return tuple(rows)


def _progressive_crops(
    tree: RunTree, stage_records: list[dict[str, Any]], act_id: str, budget: _ImageBudget
) -> list[dict[str, Any]]:
    crops = []
    for row in _records_of(stage_records, DESIGNATOR, "region", act_id):
        payload = _payload_of(row, "the Designator region record")
        transform = payload.get("transform") if isinstance(payload.get("transform"), dict) else {}
        digest = _verified_export_blob_digest(
            tree,
            stage=DESIGNATOR,
            path=payload.get("image_path"),
            expected_digest=payload.get("image_sha256"),
            description=f"act {act_id!r} region {payload.get('region_id')!r}",
            export_ref=row["record_ref"],
            budget=budget,
            record_label="the Designator region record",
        )
        crops.append(
            {
                "ordinal": transform.get("source_page_ordinal"),
                "region_id": payload.get("region_id"),
                "image_path": payload["image_path"],
                "image_sha256": digest,
                "origin": payload.get("origin"),
                "attempt_ordinal": payload.get("attempt_ordinal"),
                "record_ref": row["record_ref"],
            }
        )
    crops.sort(
        key=lambda crop: (
            not isinstance(crop["attempt_ordinal"], int),
            crop["attempt_ordinal"] or 0,
            str(crop["region_id"]),
        )
    )
    return crops


def _act_summary(stage_records: list[dict[str, Any]], act: dict[str, Any]) -> dict[str, Any]:
    """What the stages that ran say about one act, and where it stands.

    The category is derived from the furthest stage that has spoken about the
    act, in pipeline order, and says plainly which stage has not: "witnessed,
    awaiting the Perlector" is a different fact from "held-for-review", and a
    reader must not have to infer either from a missing row. Text shown here is
    the Perlector's machine reading, never an established or delivered one --
    the Archetypus and the Armarium are the two stages that would make it so,
    and their absence is exactly what this view is for.
    """
    act_id = act["act_id"]
    designator_holds = [
        {
            "reason_code": _payload_of(row, "the Designator hold record").get("reason_code"),
            "reason": _payload_of(row, "the Designator hold record").get("reason"),
            "record_ref": row["record_ref"],
        }
        for row in _records_of(stage_records, DESIGNATOR, "hold", act_id)
    ]
    testimonia = [
        {
            "chair": _payload_of(row, "the Testimonium record").get("chair"),
            "outcome": row["outcome"],
            "record_ref": row["record_ref"],
        }
        for row in _records_of(stage_records, ATTESTATORES, "testimonium", act_id)
    ]
    reading_row = _latest(stage_records, PERLECTOR, "perlectio", act_id, operation="perlegere")
    reading = None
    if reading_row is not None:
        payload = _payload_of(reading_row, "the Perlectio record")
        truncation = payload.get("truncation")
        audit = payload.get("audit")
        reading = {
            "outcome": reading_row["outcome"],
            "text": payload.get("text"),
            "reason": payload.get("reason"),
            "truncation": truncation.get("classification")
            if isinstance(truncation, dict)
            else None,
            "audit": {
                "unresolved": audit.get("unresolved"),
                "examination": audit.get("examination"),
            }
            if isinstance(audit, dict)
            else None,
            "record_ref": reading_row["record_ref"],
        }
    review_row = _latest(stage_records, RECENSOR, "review", act_id, operation="recense")
    review = None
    if review_row is not None:
        payload = _payload_of(review_row, "the Recensor review record")
        review = {
            "outcome": review_row["outcome"],
            "reason": payload.get("reason"),
            "audit_unresolved": payload.get("audit_unresolved"),
            "audit_examination": payload.get("audit_examination"),
            "record_ref": review_row["record_ref"],
        }
    established_rows = _records_of(stage_records, ARCHETYPUS, "archetypus", act_id)
    established = None
    if established_rows:
        payload = _payload_of(established_rows[-1], "the Archetypus record")
        established = {
            "text_status": payload.get("text_status"),
            "text_hash": payload.get("text_hash"),
            "record_ref": established_rows[-1]["record_ref"],
        }

    if established is not None:
        category = "established, awaiting export"
        reason = (
            f"the Archetypus established this act's text ({established['text_status']}); "
            "the Armarium has not exported it"
        )
    elif review is not None:
        if review["outcome"] == "accepted":
            category = "accepted, awaiting establishment"
        else:
            category = review["outcome"]
        reason = review["reason"]
    elif reading is not None:
        category = f"read: {reading['outcome']}, awaiting the Recensor"
        reason = reading["reason"] or (
            f"the Perlector's latest reading of this act is {reading['outcome']!r}; the "
            "Recensor has not reviewed it"
        )
    elif testimonia:
        category = "witnessed, awaiting the Perlector"
        reason = (
            f"{len(testimonia)} Testimonium record(s) are sealed for this act; the Perlector "
            "has not read it"
        )
    elif designator_holds:
        category = "held by the Designator"
        reason = "; ".join(str(hold["reason"]) for hold in designator_holds)
    else:
        category = "marked out, awaiting witnesses"
        reason = "the Designator marked this act out; no witness has reported on it"
    return {
        "designator_outcome": act.get("outcome"),
        "page_ordinal": act.get("page_ordinal"),
        "page_id": act.get("page_id"),
        "designator_holds": designator_holds,
        "testimonia": testimonia,
        "reading": reading,
        "review": review,
        "established": established,
        "category": category,
        "reason": reason,
    }


def _progressive_acts(
    tree: RunTree, stage_records: list[dict[str, Any]], budget: _ImageBudget
) -> tuple[dict[str, Any], ...]:
    """Every act the Designator's proposal seal expects, from the sealed evidence.

    The denominator is the seal's own `expected_acts` -- the same list every
    downstream stage reads through `common/stage.py::expected_acts` -- so an
    act no later stage has spoken about is still a row here, labelled with
    which stage has not spoken. No seal means the Designator has not run, and
    the act list is honestly empty rather than invented from region records.
    """
    seals = _records_of(stage_records, DESIGNATOR, "proposal-seal")
    if not seals:
        return ()
    if len(seals) != 1:
        raise OperatorError(
            ErrorCode.CONSOLE_TREE_UNREADABLE,
            detail=(
                f"the Designator proposal seal appeared {len(seals)} times; review requires "
                "exactly one immutable act denominator"
            ),
        )
    seal = seals[0]
    expected = _payload_of(seal, "the Designator proposal seal").get("expected_acts")
    if not isinstance(expected, list):
        raise OperatorError(
            ErrorCode.CONSOLE_TREE_UNREADABLE,
            detail=(
                f"the Designator proposal seal {seal['record_ref']['relative_path']} has no "
                "expected_acts list"
            ),
        )
    acts = []
    for act in expected:
        if not isinstance(act, dict) or not isinstance(act.get("act_id"), str):
            raise OperatorError(
                ErrorCode.CONSOLE_TREE_UNREADABLE,
                detail=(
                    f"the Designator proposal seal {seal['record_ref']['relative_path']} "
                    "names an act that is not an object with an act_id"
                ),
            )
        summary = _act_summary(stage_records, act)
        acts.append(
            {
                "act_id": act["act_id"],
                "act_key": act.get("act_key"),
                "category": summary["category"],
                "reason": summary["reason"],
                "crops": _progressive_crops(tree, stage_records, act["act_id"], budget),
                "row": summary,
                "record_ref": seal["record_ref"],
            }
        )
    return tuple(acts)


def _holds(stage_records: list[dict[str, Any]]) -> tuple[dict[str, Any], ...]:
    """Every act left unresolved by the Designator or the Recensor, with its reason.

    Derived from the sealed records rather than from the export's accounting,
    so it is the same list before and after export -- and so an `advance`
    record, which touches neither stage's records, can never make an act
    disappear from it.
    """
    rows: list[dict[str, Any]] = []
    for row in _records_of(stage_records, DESIGNATOR, "hold"):
        payload = _payload_of(row, "the Designator hold record")
        rows.append(
            {
                "act_id": row["subject_id"],
                "act_key": payload.get("act_key"),
                "source": DESIGNATOR,
                "outcome": row["outcome"],
                "reason": payload.get("reason"),
                "audit_examination": None,
                "record_ref": row["record_ref"],
            }
        )
    reviewed = sorted({row["subject_id"] for row in _records_of(stage_records, RECENSOR, "review")})
    for act_id in reviewed:
        current = _latest(stage_records, RECENSOR, "review", act_id, operation="recense")
        if current is None or current["outcome"] not in _UNRESOLVED_REVIEW_OUTCOMES:
            continue
        payload = _payload_of(current, "the Recensor review record")
        rows.append(
            {
                "act_id": act_id,
                "act_key": payload.get("act_key"),
                "source": RECENSOR,
                "outcome": current["outcome"],
                "reason": payload.get("reason"),
                "audit_examination": payload.get("audit_examination"),
                "record_ref": current["record_ref"],
            }
        )
    return tuple(rows)


def _next_action(
    progress: tuple[dict[str, Any], ...],
    export: dict[str, Any],
    holds: tuple[dict[str, Any], ...],
) -> dict[str, Any]:
    """The one supported continuation, said plainly, and what `advance` is not.

    This reports; it does not act, and it never proposes a shortcut: a hold is
    resolved by a new authorized run over the same sealed source, correction of
    the text happens outside the pipeline, and `advance` records a person's
    permission to pass one sealed stage boundary without certifying a reading
    or clearing a hold. Saying so beside every hold is what keeps the boundary
    permission and the act's resolution from being read as one thing.
    """
    resume_from = None
    if export.get("present"):
        summary = (
            "This run has a completed export. Review each act below against its images; "
            "a delivered act is the pipeline's machine reading, not truth."
        )
    else:
        first = next((row for row in progress if row["state"] != "sealed"), None)
        if first is None:
            summary = (
                "Every stage is sealed but no export record was found; treat the tree as "
                "evidence to preserve and investigate before anything resumes."
            )
        elif first["state"] == "not-run":
            summary = (
                f"The run stopped before {first['stage']}: every stage before it is sealed and "
                f"nothing from {first['stage']} onward has run. The supported continuation is to "
                f"resume the run from {first['stage']} (the orchestrator's --from "
                f"{first['stage']} --to armarium, or that stage alone with --stage "
                f"{first['stage']}); a resume reuses the sealed evidence and never rewrites it."
            )
            resume_from = first["stage"]
        elif first["state"] == "unsealed":
            summary = (
                f"{first['stage']} has written records but no completion seal: it was "
                "interrupted or is still running. Do not resume while a writer may still be "
                f"active; once none is, resuming from {first['stage']} republishes its records "
                "byte for byte where they are unchanged and refuses where they are not."
            )
            resume_from = first["stage"]
        else:
            summary = (
                f"{first['stage']}'s completion seal no longer verifies against the evidence "
                f"on disk ({first['note']}). This is not a stage that has not run; treat the "
                "tree as evidence to preserve and investigate before anything resumes."
            )
    if holds:
        summary += (
            f" {len(holds)} act(s) are held or unresolved; each is listed with its recorded "
            "reason. A hold is resolved only by a new authorized run over the same sealed "
            "source: `advance` records permission to pass one sealed stage boundary and "
            "neither certifies a reading nor clears a hold, and any correction of the text "
            "happens outside the pipeline."
        )
    return {"summary": summary, "resume_from": resume_from, "held_acts": len(holds)}


def _image_row(
    tree: RunTree,
    row: Any,
    export_ref: dict[str, str],
    budget: _ImageBudget | None = None,
) -> dict[str, Any]:
    budget = _ImageBudget() if budget is None else budget
    if not isinstance(row, dict):
        raise OperatorError(
            ErrorCode.CONSOLE_TREE_UNREADABLE,
            detail=(
                f"the Armarium export record {export_ref['relative_path']} has a page that is "
                "not an object"
            ),
        )
    projected = {
        "ordinal": row.get("ordinal"),
        "page_id": row.get("page_id"),
        "outcome": row.get("outcome"),
        "reason": row.get("reason"),
        "record_ref": export_ref,
    }
    if row.get("outcome") != "sealed":
        # An unsealed page stays visible with its reason; only a sealed page
        # claims an immutable image reference to verify.
        return {**projected, "image_path": None, "image_sha256": None}
    image_digest = _verified_export_blob_digest(
        tree,
        stage=EXEMPLAR,
        path=row.get("image_path"),
        expected_digest=row.get("image_sha256"),
        description=f"page {row.get('ordinal')!r}",
        export_ref=export_ref,
        budget=budget,
    )
    return {
        **projected,
        "image_path": row["image_path"],
        "image_sha256": image_digest,
    }


def _act_row(
    tree: RunTree,
    row: Any,
    export_ref: dict[str, str],
    budget: _ImageBudget | None = None,
    *,
    requires_crops: bool = True,
) -> dict[str, Any]:
    budget = _ImageBudget() if budget is None else budget
    if not isinstance(row, dict):
        raise OperatorError(
            ErrorCode.CONSOLE_TREE_UNREADABLE,
            detail=(
                f"the Armarium export record {export_ref['relative_path']} has an act that is "
                "not an object"
            ),
        )
    # No `[]` default for a delivered act. One whose export row omits
    # `source_regions` would then reach the console as an act with an empty
    # crop list, and the operator could not tell "this act records no crop"
    # from "the crop list went missing" — they would be approving text they
    # never saw against the ink (GOVERNANCE 2, GOALS 5). Absent is refused
    # exactly like malformed. A non-delivered act is the one shape whose
    # writer never records the field, so only there absent means absent.
    source_regions = row.get("source_regions")
    if source_regions is None and not requires_crops:
        source_regions = []
    if not isinstance(source_regions, list):
        raise OperatorError(
            ErrorCode.CONSOLE_TREE_UNREADABLE,
            detail=(
                f"the Armarium export record {export_ref['relative_path']} act "
                f"{row.get('act_id')!r} source_regions value is missing or not a list"
            ),
        )
    crops = []
    for region in source_regions:
        if not isinstance(region, dict):
            raise OperatorError(
                ErrorCode.CONSOLE_TREE_UNREADABLE,
                detail=(
                    f"the Armarium export record {export_ref['relative_path']} act "
                    f"{row.get('act_id')!r} has a source region that is not an object"
                ),
            )
        image_digest = _verified_export_blob_digest(
            tree,
            stage=DESIGNATOR,
            path=region.get("image_path"),
            expected_digest=region.get("image_sha256"),
            description=(f"act {row.get('act_id')!r} source region {region.get('region_id')!r}"),
            export_ref=export_ref,
            budget=budget,
        )
        crops.append(
            {
                "ordinal": region.get("source_page_ordinal"),
                "region_id": region.get("region_id"),
                "image_path": region["image_path"],
                "image_sha256": image_digest,
            }
        )
    return {
        "act_id": row.get("act_id"),
        "act_key": row.get("act_key"),
        "category": row.get("category"),
        "reason": row.get("reason"),
        "crops": crops,
        "row": row,
        "record_ref": export_ref,
    }


def _advance_records(
    tree: RunTree, boundaries: dict[str, dict[str, Any]]
) -> tuple[dict[str, Any], ...]:
    """Every advance decision on record, in stable content-addressed path order.

    ``receipts/sha256/`` is content-addressed, so two advance records for the
    same stage boundary — an append, never an overwrite — are two distinct
    files that both land here. Nothing here picks between them or shows only
    the latest; a stage advanced twice is a fact for the reader of the
    console to notice and act on, not one this projection resolves for them.

    The receipt directory also holds non-approval receipts (a chair's serving
    receipt shares the same content-addressed path scheme). Only a record
    that declares the approval schema is treated as an approval at all; a
    record that declares it and then fails full validation is a governance
    fact — a tampered or hand-written approval — and is refused loudly rather
    than skipped, matching every other read this projection performs.

    ``receipts/sha256`` itself is walked directly rather than through a
    ``build_manifest``-style inventory, because a receipt is not a stage
    artifact. That means it does not inherit the manifest walk's own refusal
    of a symlinked producer directory (``RunTree._inventory_directory``), so
    the same containment has to be asserted here: a symlink standing in for
    this directory could point anywhere content-addressed self-consistency
    can be satisfied by an attacker who names their own file, which a
    directory *outside* `inventory_scope()` always can. Refused by identity,
    not followed and trusted.
    """

    receipts_relative = "receipts/sha256"
    if (tree.root / receipts_relative).is_symlink():
        raise OperatorError(
            ErrorCode.CONSOLE_TREE_UNREADABLE,
            detail=(
                f"{receipts_relative} is a link, not the directory this store wrote; an "
                "advance record is read from the store's own receipts directory, never an "
                "alias"
            ),
        )
    receipts_parent = tree.root / "receipts"
    receipts_dir = tree.resolve(receipts_relative)
    if not receipts_dir.exists():
        return ()
    if (
        receipts_parent.is_symlink()
        or receipts_dir.is_symlink()
        or not receipts_parent.is_dir()
        or not receipts_dir.is_dir()
    ):
        raise OperatorError(
            ErrorCode.CONSOLE_TREE_UNREADABLE,
            detail="the content-addressed receipt directory is not a real directory",
        )
    records: list[dict[str, Any]] = []
    for path in sorted(receipts_dir.glob("*.json")):
        relative_path = path.relative_to(tree.root).as_posix()
        if path.is_symlink() or not path.is_file():
            raise OperatorError(
                ErrorCode.CONSOLE_TREE_UNREADABLE,
                detail=f"receipt {relative_path} is not an immutable regular file",
            )
        data = tree.read_bytes(relative_path)
        try:
            decoded = json.loads(data.decode("utf-8"))
        except (UnicodeDecodeError, ValueError) as error:
            raise OperatorError(
                ErrorCode.CONSOLE_TREE_UNREADABLE,
                detail=f"receipt {relative_path} is not valid JSON",
            ) from error
        if not isinstance(decoded, dict) or decoded.get("schema") != "approval-record.v0":
            continue
        record = validate_approval_record(decoded)
        if record["action"] != "advance":
            continue
        digest = digest_bytes(data)
        if path.stem != digest:
            raise OperatorError(
                ErrorCode.CONSOLE_TREE_UNREADABLE,
                detail=(
                    f"advance receipt {relative_path} contains digest {digest}, so its "
                    "content-addressed filename is false"
                ),
            )
        records.append(
            {
                **record,
                "relative_path": relative_path,
                "sha256": digest,
                **_still_binds(record, boundaries),
            }
        )
    return tuple(records)


def _still_binds(record: dict[str, Any], boundaries: dict[str, dict[str, Any]]) -> dict[str, Any]:
    """Say, on the surface a person reads, whether this advance still binds its boundary.

    `advance.verify_advance` already refuses a record whose seal changed after
    it was written, but nothing on the read path called it, so the console
    displayed "this boundary was advanced" as a present-tense fact however far
    the boundary had moved since. Detectable only by a function nobody calls is
    not detectable (GOVERNANCE 2), and the digest binding is the whole reason
    the record carries a `target_version_hash`.

    This reports; it does not choose. A stale record is still shown, still
    named, and still the operator's to act on — hiding it would be the picker
    hard rule 8 forbids, wearing a tidier face.
    """

    subjects = record["subject_ids"]
    if len(subjects) != 1 or not subjects[0].startswith(ADVANCE_SUBJECT_PREFIX):
        return {
            "boundary_stage": None,
            "boundary_current": False,
            "boundary_note": "this advance record names no single stage boundary",
        }
    stage = subjects[0][len(ADVANCE_SUBJECT_PREFIX) :]
    boundary = boundaries.get(stage)
    if boundary is None:
        return {
            "boundary_stage": stage,
            "boundary_current": False,
            "boundary_note": (
                f"{stage} has no stored stage-seal now, so this advance binds a "
                "boundary that is no longer there"
            ),
        }
    current = boundary["seal_digest"]
    if current != record["target_version_hash"]:
        return {
            "boundary_stage": stage,
            "boundary_current": False,
            "boundary_note": (
                f"{stage}'s seal changed after this advance was recorded; the advance "
                "binds an earlier boundary"
            ),
        }
    if not boundary["sealed"]:
        return {
            "boundary_stage": stage,
            "boundary_current": False,
            "boundary_note": (
                f"{stage}'s stored seal no longer verifies against the run tree: "
                f"{boundary['seal_note']}"
            ),
        }
    return {"boundary_stage": stage, "boundary_current": True, "boundary_note": None}


def _review_items(
    tree: RunTree,
    payload: dict[str, Any],
    export_ref: dict[str, str],
    budget: _ImageBudget | None = None,
) -> tuple[dict[str, Any], ...] | None:
    budget = _ImageBudget() if budget is None else budget
    bundle = payload.get("bundle")
    if not isinstance(bundle, dict):
        raise OperatorError(
            ErrorCode.CONSOLE_TREE_UNREADABLE,
            detail=f"the Armarium export record {export_ref['relative_path']} has no bundle",
        )
    reference = bundle.get("reference")
    if not isinstance(reference, dict):
        raise OperatorError(
            ErrorCode.CONSOLE_TREE_UNREADABLE,
            detail=(
                f"the Armarium export record {export_ref['relative_path']} bundle has no "
                "immutable reference"
            ),
        )
    path = reference.get("relative_path")
    expected_digest = reference.get("sha256")
    exported_digest = bundle.get("sha256")
    if (
        not isinstance(path, str)
        or not isinstance(expected_digest, str)
        or exported_digest != expected_digest
    ):
        raise OperatorError(
            ErrorCode.CONSOLE_TREE_UNREADABLE,
            detail=(
                f"the Armarium export record {export_ref['relative_path']} bundle reference "
                "has no single digest"
            ),
        )
    expected_path = tree.blob_path(ARMARIUM, expected_digest)
    if path != expected_path:
        raise OperatorError(
            ErrorCode.CONSOLE_TREE_UNREADABLE,
            detail=(
                f"the Armarium export record {export_ref['relative_path']} bundle claims digest "
                f"{expected_digest} but names {path}, not its content-addressed path "
                f"{expected_path}"
            ),
        )
    try:
        # The same shape the image allowance exists for: the bundle zip is read
        # whole to verify its digest, in the same projection pass that already
        # holds every page and crop, so it spends from the same allowance and an
        # oversized run refuses by name instead of by exhaustion (GOVERNANCE 2).
        bundle_bytes = _budgeted_image_bytes(tree, path, budget, "the Armarium export bundle")
        actual_digest = digest_bytes(bundle_bytes)
        if actual_digest != expected_digest:
            raise OperatorError(
                ErrorCode.CONSOLE_TREE_UNREADABLE,
                detail=(
                    f"the Armarium export record {export_ref['relative_path']} bundle {path} "
                    f"claims digest {expected_digest}, but its bytes have digest {actual_digest}"
                ),
            )
        with zipfile.ZipFile(io.BytesIO(bundle_bytes)) as archive:
            members = [
                member for member in archive.infolist() if member.filename == _REVIEW_ITEMS_MEMBER
            ]
            if not members:
                return None
            if len(members) != 1:
                raise OperatorError(
                    ErrorCode.CONSOLE_TREE_UNREADABLE,
                    detail="the Armarium bundle contains more than one review-items.jsonl",
                )
            member = members[0]
            if member.compress_type != zipfile.ZIP_STORED:
                # `build_armarium_bundle` writes every member stored, never
                # compressed (armarium_export.py), for exactly this reason: a
                # stored member's extracted size is bounded by its own physical
                # bytes, while a compressed one can decompress far past them.
                # `verify_export_bundle` already refuses this for the sealed
                # package; the review surface reads the same bundle format and
                # must refuse it here too, before decompressing anything.
                raise OperatorError(
                    ErrorCode.CONSOLE_TREE_UNREADABLE,
                    detail=(
                        f"the Armarium export record {export_ref['relative_path']} bundle "
                        f"{path} member review-items.jsonl is compressed, not stored; a "
                        "review bundle is only ever written stored"
                    ),
                )
            # Two faults, two sentences. Joined, a directory entry answered with
            # "exceeds the operator review limit" -- `file_size` is 0 for one --
            # and sent the operator looking for an oversized queue that does not
            # exist. The branch is in fact unreachable through the member filter
            # above, since `is_dir()` needs a trailing separator this name cannot
            # have; it stays as a defensive check, but a defensive check that
            # names the wrong fault is worse than none.
            if member.is_dir():  # pragma: no cover - unconstructible; see the test by this name
                raise OperatorError(
                    ErrorCode.CONSOLE_TREE_UNREADABLE,
                    detail=(
                        "the Armarium bundle names a directory at review-items.jsonl, so it "
                        "carries no review queue to read"
                    ),
                )
            if member.file_size > MAX_REVIEW_ITEMS_BYTES:
                raise OperatorError(
                    ErrorCode.CONSOLE_TREE_UNREADABLE,
                    detail=(
                        "review-items.jsonl exceeds the operator review limit of "
                        f"{MAX_REVIEW_ITEMS_BYTES} bytes"
                    ),
                )
            with archive.open(member) as source:
                review_bytes = source.read(MAX_REVIEW_ITEMS_BYTES + 1)
            # Kept, and unreachable while the check above holds. `zipfile`
            # bounds a member read by the `file_size` the central directory
            # declares, so a header that lies small cannot decompress past it —
            # measured, not assumed, and what a falsified header actually
            # produces is a CRC failure caught below. This stays as the bound
            # that does not depend on the archive library's own accounting.
            if len(review_bytes) > MAX_REVIEW_ITEMS_BYTES:
                raise OperatorError(
                    ErrorCode.CONSOLE_TREE_UNREADABLE,
                    detail=(
                        "review-items.jsonl expands beyond the operator review limit of "
                        f"{MAX_REVIEW_ITEMS_BYTES} bytes"
                    ),
                )
            lines = review_bytes.splitlines()
            if len(lines) > MAX_REVIEW_ITEMS:
                raise OperatorError(
                    ErrorCode.CONSOLE_TREE_UNREADABLE,
                    detail=(
                        "review-items.jsonl contains more than the operator review limit of "
                        f"{MAX_REVIEW_ITEMS} records"
                    ),
                )
            # Parsed row by row rather than in one comprehension: the queue may
            # hold up to MAX_REVIEW_ITEMS rows, and a refusal that names only
            # the bundle leaves the operator to find the bad row by hand. The
            # one-based number each item already carries is now in both
            # refusals. Found by CodeRabbit.
            parsed: list[dict[str, Any]] = []
            for number, line in enumerate(lines, start=1):
                try:
                    row = json.loads(line)
                except ValueError as error:
                    raise OperatorError(
                        ErrorCode.CONSOLE_TREE_UNREADABLE,
                        detail=(
                            f"review-items.jsonl line {number} in bundle {path} is not "
                            f"valid JSON: {error}"
                        ),
                    ) from error
                if not isinstance(row, dict):
                    raise OperatorError(
                        ErrorCode.CONSOLE_TREE_UNREADABLE,
                        detail=(
                            f"review-items.jsonl line {number} in bundle {path} is not an object"
                        ),
                    )
                parsed.append(
                    {
                        "row": row,
                        "record_ref": export_ref,
                        "bundle_path": path,
                        "member": _REVIEW_ITEMS_MEMBER,
                        "line": number,
                    }
                )
            return tuple(parsed)
    except OperatorError:
        raise
    except (OSError, ValueError, RuntimeError, zipfile.BadZipFile) as error:
        raise OperatorError(
            ErrorCode.CONSOLE_TREE_UNREADABLE,
            detail=(
                f"review-items.jsonl could not be read from bundle {path}: "
                f"{type(error).__name__}: {error}"
            ),
        ) from error
