"""Project sealed run-tree evidence into a read-only operator shape."""

from __future__ import annotations

import hashlib
import json
import re
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from common.contracts.approval import validate_approval_record
from common.contracts.canonical import digest_bytes
from common.contracts.errors import ContractError, SchemaRefusal
from common.contracts.identities import artifact_id
from common.contracts.outcomes import OutcomeClass, classify
from common.contracts.stages import (
    ARCHETYPUS,
    ARMARIUM,
    ATTESTATORES,
    EXEMPLAR,
    PERLECTOR,
    RECENSOR,
    STAGES,
)
from common.page_review import (
    REVIEW_DECISIONS_KIND,
    REVIEW_DECISIONS_OPERATION,
    REVIEW_DECISIONS_SUBJECT,
)
from common.page_types import ACT_CLASSES
from common.review_decisions import decisions_digest
from common.runtree.store import RUN_FILE, RunTree
from common.stage import latest_attempt

from .advance import ADVANCE_SUBJECT_PREFIX, verify_sealed_boundary
from .errors import ErrorCode, OperatorError
from .records import sha256_file
from .surface import resume_command as resume_from_command

MAX_REVIEW_ITEM_BYTES = 16 * 1024 * 1024
REVIEW_PAGE_SIZE = 500
# The most one retained page of the review queue may hold.
MAX_REVIEW_PAGE_BYTES = 256 * 1024 * 1024
_REVIEW_ITEMS_MEMBER = "review-items.jsonl"


def _image_digest(tree: RunTree, relative_path: str, what: str) -> str:
    """The SHA-256 of one sealed image, streamed from a regular file, never a FIFO or link."""

    try:
        return sha256_file(tree.resolve(relative_path))
    except OSError as error:
        raise OperatorError(
            ErrorCode.CONSOLE_TREE_UNREADABLE, detail=f"{what} could not be read: {error}"
        ) from error


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
    # Lets a run without a completed export still be projected: `export` says
    # what the export record itself claims, `progress` gives one state per
    # stage (sealed / unsealed / not-run / seal-invalid), `holds` lists every
    # unresolved act pre- and post-export alike, and `next_action` states the
    # one supported continuation. All four default so existing positional and
    # keyword construction stays valid.
    export: dict[str, Any] = field(default_factory=dict)
    progress: tuple[dict[str, Any], ...] = ()
    holds: tuple[dict[str, Any], ...] = ()
    next_action: dict[str, Any] = field(default_factory=dict)
    # The denominator the Armarium reconciles its census against, not a count
    # of page records found.
    pages_declared: int | None = None
    pages_declared_note: str | None = None
    # Before export, says what the act list counts and what it leaves to the
    # export, so a zero here never reads like "no acts".
    acts_denominator_note: str | None = None
    review_items_total: int | None = None
    review_page: int = 1
    review_page_size: int = REVIEW_PAGE_SIZE
    # The page reading's labelled `other` entries, shown in the same row shape
    # as `acts` but never counted among them, before and after export alike.
    other_readings: tuple[dict[str, Any], ...] = ()
    # What the Recensor's last pass did with the run's operator review
    # decisions, and whether it saw every decision stored now.
    review_decisions: dict[str, Any] = field(default_factory=dict)


class ReadOnlyRun:
    """The renderer's intentionally tiny capability over an already-open run."""

    __slots__ = ("_tree",)

    def __init__(self, root: str | Path, run_id: str):
        self._tree = RunTree(Path(root), run_id)

    def projection(self, *, review_page: int = 1) -> ReviewProjection:
        if review_page < 1:
            raise OperatorError(ErrorCode.INVALID_COMMAND, detail="review page must be at least 1")
        tree = self._tree
        if not (tree.root / RUN_FILE).exists():
            # A mistyped or nonexistent run id is a wrong command, not damaged
            # evidence, so it is refused here rather than falling into the
            # catch-all below, whose advice ("preserve and investigate") is
            # for a tree that exists and failed verification.
            raise OperatorError(
                ErrorCode.INVALID_COMMAND,
                detail=(
                    f"no run named {tree.run_id!r} was found under {tree.root}: "
                    f"{RUN_FILE} does not exist there, so there is nothing to review"
                ),
            )
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
                # A boundary is "sealed" only when its stored seal still
                # verifies against the evidence on disk; the note says why not.
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
                        # Every completion seal remains visible; `current`
                        # names the one the boundary presently uses without
                        # suppressing an earlier attempt.
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
            acts_denominator_note = None
            armarium_state = next(row["state"] for row in progress if row["stage"] == ARMARIUM)
            export_rows = _export_rows(stage_records)
            if export_rows:
                payload, export_ref = _armarium_payload(tree, stage_records)
                pages = tuple(_image_row(tree, row, export_ref) for row in payload["pages"])
                # The Armarium attaches `source_regions` to every delivered act
                # and never to a non-delivered one: missing means damaged on a
                # delivered row, and means the record as written otherwise.
                acts = tuple(
                    _act_row(
                        tree,
                        row,
                        export_ref,
                        stage_records=stage_records,
                    )
                    for row in payload["delivered"]
                ) + tuple(
                    _act_row(
                        tree,
                        row,
                        export_ref,
                        requires_crops=False,
                        stage_records=stage_records,
                    )
                    for row in payload["non_delivered"]
                )
                other_readings = tuple(
                    _act_row(
                        tree,
                        row,
                        export_ref,
                        requires_crops=not isinstance(row, dict)
                        or row.get("category") == "delivered",
                        stage_records=stage_records,
                    )
                    for row in payload["other_readings"]
                )
                review_items, review_items_total = _review_items(
                    tree, payload, export_ref, review_page=review_page
                )
                export = _export_state(
                    export_rows[0], payload, export_ref, armarium_state, review_items
                )
            else:
                # No export: project from whatever the stages that did run
                # already sealed (pages, crops, latest reading, holding
                # review), each row naming its own record. None of this is a
                # delivered result, and `export` says so.
                pages = _sealed_pages(tree, stage_records)
                acts = _progressive_acts(tree, stage_records, "act")
                other_readings = _progressive_acts(tree, stage_records, "other")
                acts_denominator_note = _pre_export_acts_note(stage_records, acts + other_readings)
                review_items = None
                review_items_total = None
                export = {
                    "present": False,
                    "record_present": False,
                    "boundary_sealed": armarium_state == "sealed",
                    "outcome": None,
                    "claims_status": None,
                    "complete": False,
                    "record_ref": None,
                    "review_items_absent_because": "no-export",
                    "note": (
                        "there is no Armarium export record; this run has no completed "
                        "export, so the pages, crops, readings and reviews shown are read "
                        "from the sealed evidence of the stages that did run, and none of "
                        "them is a delivered result"
                    ),
                }
            holds = _holds(stage_records)
            decisions = _review_decisions(tree, stage_records)
            declared_pages, declared_note = _declared_page_count(tree)
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
                next_action=_next_action(
                    tree.run_id,
                    progress,
                    export,
                    holds,
                    _recorded_scenario(stage_records),
                    decisions,
                ),
                acts_denominator_note=acts_denominator_note,
                pages_declared=declared_pages,
                review_items_total=review_items_total,
                review_page=review_page,
                review_page_size=REVIEW_PAGE_SIZE,
                pages_declared_note=declared_note,
                other_readings=other_readings,
                review_decisions=decisions,
            )
        except (ContractError, KeyError, OSError, TypeError, ValueError) as error:
            # The rendered detail keeps the underlying refusal's evidence
            # path, since its repair instruction requires a named file.
            raise OperatorError(
                ErrorCode.CONSOLE_TREE_UNREADABLE,
                detail=(f"run-tree evidence could not be read: {type(error).__name__}: {error}"),
            ) from error


def _record_row(tree: RunTree, stage: str, manifest_row: dict[str, Any]) -> dict[str, Any]:
    """Bind displayed record fields and their address to one filesystem read,
    so a field is never paired with a digest from a later read."""

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
    for name in ("pages", "delivered", "non_delivered", "other_readings"):
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
    record_label: str = "the Armarium export record",
) -> str:
    """Verify a claimed image path and digest without repairing a mismatch.

    `record_label` names the record whose claim is checked (the Armarium
    export post-export, the Exemplar page or Perlector act-region record
    pre-export); the check is the same either way: the recorded path must be
    the digest's own content address, and the bytes there must still hash to
    it.
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
    actual_digest = _image_digest(tree, path, description)
    if actual_digest != expected_digest:
        raise OperatorError(
            ErrorCode.CONSOLE_TREE_UNREADABLE,
            detail=(
                f"{record_label} {export_path} {description} names {path} with "
                f"digest {expected_digest}, but its bytes have digest {actual_digest}"
            ),
        )
    return actual_digest


# --- The pre-export view ---------------------------------------------------
# Every helper below reads only the `stage_records` snapshot the projection
# already took and the image bytes those records name; nothing re-opens the
# tree, so one projection cannot describe two versions of a run.


def _export_state(
    export_row: dict[str, Any],
    payload: dict[str, Any],
    export_ref: dict[str, str],
    armarium_state: str,
    review_items: tuple[dict[str, Any], ...] | None,
) -> dict[str, Any]:
    """What the export record says about itself, never what its existence suggests.

    The Armarium writes an export record for a partial export as readily as a
    complete one, and writes it before sealing its own boundary, so a run
    killed between the two leaves an export record behind an unsealed stage.
    `present` therefore means a *sealed* Armarium with an export record under
    it; `complete` is the further question the record's own outcome and
    claims status answer, and the note spells out which of the three states
    this is in words.

    `review_items_absent_because` separates the two silences behind an empty
    review queue: no export at all, versus an export whose bundle carries no
    `review-items.jsonl` member because the format was not configured.
    """
    bundle = payload.get("bundle")
    claims_status = bundle.get("claims_status") if isinstance(bundle, dict) else None
    outcome = export_row["outcome"]
    sealed = armarium_state == "sealed"
    complete = outcome == "delivered" and claims_status == "complete"
    if not sealed:
        note = (
            "an export record exists but the Armarium did not seal its boundary "
            f"({armarium_state}), so this run has no completed export; the pages and acts "
            "shown are that unsealed record's accounting, verified against the sealed images"
        )
    elif complete:
        note = (
            f"the Armarium export is sealed, its outcome is {outcome!r} and its bundle "
            f"claims {claims_status!r}; the pages and acts shown are its accounting, "
            "verified against the sealed images"
        )
    else:
        note = (
            f"this is a partial export ({outcome}): the export record's outcome is "
            f"{outcome!r} and its bundle claims {claims_status!r}, so not every act was "
            "delivered; the pages and acts shown are its accounting, verified against the "
            "sealed images"
        )
    return {
        "present": sealed,
        "record_present": True,
        "boundary_sealed": sealed,
        "outcome": outcome,
        "claims_status": claims_status,
        "complete": complete and sealed,
        "record_ref": export_ref,
        "review_items_absent_because": (
            None if review_items is not None else "bundle-has-no-review-items-member"
        ),
        "note": note,
    }


def _declared_page_count(tree: RunTree) -> tuple[int | None, str | None]:
    """How many source pages the run itself declared, as the Pages denominator.

    A count of page records found is the numerator, not the denominator: a run
    whose Exemplar never wrote a record for a declared source would show a
    shorter list with nothing saying it was short. The run authority's own
    declared ledger is the denominator the Armarium reconciles its census
    against, so it is read here instead.

    A denominator that cannot be read must not close this surface, which
    exists to open damaged trees; the refusal is carried as a note beside the
    count instead of hiding the images and the reason the run stopped.
    """
    # `AttributeError`/`TypeError`: a `run.json` whose top level is an array or
    # scalar raises `AttributeError` from inside `read_run`'s self-hash check,
    # which is neither a `ContractError` nor otherwise caught here.
    try:
        authority = tree.read_run()
    except (ContractError, OSError, AttributeError, TypeError) as error:
        return (
            None,
            "the run authority could not be read as an object, so nothing declares a page "
            f"count: {type(error).__name__}: {error}",
        )
    if not isinstance(authority, dict):
        return (
            None,
            "the run authority is not an object, so nothing in it declares a page count",
        )
    manifest = authority.get("source_manifest")
    if not isinstance(manifest, list):
        return None, "the run authority declares no source manifest, so there is no page count"
    return len(manifest), None


_SCENARIO_TOKEN = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}")


def _recorded_scenario(stage_records: list[dict[str, Any]]) -> str | None:
    """The scenario this run declared, when a record in the tree names it.

    Only the Armarium export record carries the scenario by name; earlier
    stages and the run authority do not, so this is `None` for exactly the
    runs whose resume command is printed without `--scenario`.
    """
    for row in _export_rows(stage_records):
        payload = row["record"].get("payload")
        if isinstance(payload, dict) and isinstance(payload.get("scenario"), str):
            scenario = payload["scenario"]
            # This feeds a command a person is invited to type, so only a
            # well-formed scenario token is repeated; anything else counts as
            # not stated.
            if _SCENARIO_TOKEN.fullmatch(scenario):
                return scenario
            return None
    return None


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
    reading or review it acts on, so "the latest" on this console is what the
    pipeline itself would consume, refusals included.
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

    Four states a reader must be able to tell apart: completed and sealed;
    wrote records but never sealed (interrupted or still running); never run;
    and a stored seal that no longer verifies. A legitimate absence and a
    broken promise are different facts and must not collapse into each other.
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
            note = (
                "no record and no seal found here: this stage has not run, or nothing it "
                "wrote is in this tree"
            )
        rows.append(
            {"stage": stage, "state": state, "artifact_count": len(artifacts), "note": note}
        )
    return tuple(rows)


def _sealed_pages(tree: RunTree, stage_records: list[dict[str, Any]]) -> tuple[dict[str, Any], ...]:
    """Every page the Exemplar accounted for, sealed or refused, from its own record.

    Produces the same row shape as the export path, so one renderer reads
    both: a sealed page names its image and checked digest, a refused page
    stays visible with its reason and no image.
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
            record_label="the Exemplar page record",
        )
        rows.append({**projected, "image_path": payload["image_path"], "image_sha256": digest})
    rows.sort(key=lambda page: (not isinstance(page["ordinal"], int), page["ordinal"] or 0))
    return tuple(rows)


def _reading_region(stage_records: list[dict[str, Any]], act_id: str) -> dict[str, Any] | None:
    """The Perlector's `act-region` record of one act, or none before it read the page."""
    rows = _records_of(stage_records, PERLECTOR, "act-region", act_id)
    if len(rows) > 1:
        raise SchemaRefusal(f"act {act_id} has {len(rows)} act-region records; it may have one")
    return rows[0] if rows else None


def _progressive_crops(
    tree: RunTree, stage_records: list[dict[str, Any]], act_id: str
) -> list[dict[str, Any]]:
    """The crop the Perlector cut for one act, verified; none for an unplaced entry."""
    row = _reading_region(stage_records, act_id)
    if row is None:
        return []
    payload = _payload_of(row, "the Perlector act-region record")
    if payload.get("image_path") is None:
        return []
    digest = _verified_export_blob_digest(
        tree,
        stage=PERLECTOR,
        path=payload.get("image_path"),
        expected_digest=payload.get("image_sha256"),
        description=f"act {act_id!r} region {payload.get('region_id')!r}",
        export_ref=row["record_ref"],
        record_label="the Perlector act-region record",
    )
    return [
        {
            "ordinal": payload.get("page_ordinal"),
            "region_id": payload.get("region_id"),
            "image_path": payload["image_path"],
            "image_sha256": digest,
            "record_ref": row["record_ref"],
        }
    ]


def _reading_row(stage_records: list[dict[str, Any]], act_id: str) -> dict[str, Any] | None:
    """The Perlector's current reading of one act, or none, in one vocabulary.

    Built once and read from two places: before export, where it is the only
    thing to show about an act, and after it, for a non-delivered act whose
    export row carries no reading at all. `_testimonia_rows` exists for the
    same reason, for witnesses.

    The doubt report and its layers are carried through exactly as the record
    holds them: a missing or malformed value is a fault of the run tree,
    refused by field like every other projection list, never printed as no
    doubt at all.
    """
    rows = _records_of(stage_records, PERLECTOR, "perlectio", act_id)
    # An act id is minted per page-reading attempt, so it has at most one
    # Perlectio and no attempt ordinal to choose between.
    if len(rows) > 1:
        raise SchemaRefusal(f"act {act_id} has {len(rows)} Perlectio records; it may have one")
    if not rows:
        return None
    reading_row = rows[0]
    payload = _payload_of(reading_row, "the Perlectio record")
    return {
        "outcome": reading_row["outcome"],
        "text": payload.get("text"),
        "uncertainty_assessment": payload.get("uncertainty_assessment"),
        "uncertain_spans": payload.get("uncertain_spans"),
        "gaps": payload.get("gaps"),
        "truncation": payload.get("truncation"),
        "record_ref": reading_row["record_ref"],
    }


def _act_page_id(stage_records: list[dict[str, Any]], act_id: str) -> str | None:
    """The page the Perlector read one act on, or none before it read the page."""
    row = _reading_region(stage_records, act_id)
    if row is None:
        return None
    return _payload_of(row, "the Perlector act-region record").get("page_id")


def _testimonia_rows(
    stage_records: list[dict[str, Any]], page_id: str | None
) -> list[dict[str, Any]]:
    """Every page Testimonium sealed for one act's page, in the order the stage wrote them.

    Witnesses read whole pages, so an act's witnesses are its page's. Every
    attempt stays listed and each names its attempt ordinal, since two
    contradictory rows for one chair with no ordinal between them would leave
    the current reading indistinguishable from a superseded one.

    Read from the sealed Attestatores records rather than the export, so the
    same rows are available before and after export: the export names a
    witness basis only for a delivered act, and a held act's witnesses live
    only in the run tree.
    """
    if page_id is None:
        return []
    rows = []
    for row in _records_of(stage_records, ATTESTATORES, "page-testimonium", page_id):
        payload = _payload_of(row, "the page Testimonium record")
        rows.append(
            {
                "chair": payload.get("chair"),
                "outcome": row["outcome"],
                "attempt_ordinal": payload.get("attempt_ordinal"),
                "record_ref": row["record_ref"],
            }
        )
    return rows


def _act_summary(stage_records: list[dict[str, Any]], act: dict[str, Any]) -> dict[str, Any]:
    """What the stages that ran say about one act, and where it stands.

    The category names the furthest stage that has spoken about the act, in
    pipeline order, and says plainly which has not. Any text shown is the
    Perlector's machine reading, never an established or delivered one.
    """
    act_id = act["act_id"]
    testimonia = _testimonia_rows(stage_records, act.get("page_id"))
    reading = _reading_row(stage_records, act_id)
    review_row = _latest(stage_records, RECENSOR, "review", act_id, operation="recense")
    review = None
    if review_row is not None:
        payload = _payload_of(review_row, "the Recensor review record")
        review = {
            "outcome": review_row["outcome"],
            "reason": payload.get("reason"),
            "record_ref": review_row["record_ref"],
        }
    established_rows = _records_of(stage_records, ARCHETYPUS, "archetypus", act_id)
    established = None
    if len(established_rows) > 1:
        # The Archetypus writes exactly one record per act with no attempt
        # token, so a second one is a tree this console cannot read, not a
        # newer version to prefer over the other.
        raise OperatorError(
            ErrorCode.CONSOLE_TREE_UNREADABLE,
            detail=(
                f"act {act_id!r} has {len(established_rows)} Archetypus records "
                f"({', '.join(row['record_ref']['relative_path'] for row in established_rows)}); "
                "the stage writes exactly one established text per act, so this tree cannot "
                "be read out without choosing between them"
            ),
        )
    if established_rows:
        payload = _payload_of(established_rows[0], "the Archetypus record")
        established = {
            "text_status": payload.get("text_status"),
            "text_hash": payload.get("text_hash"),
            "record_ref": established_rows[0]["record_ref"],
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
        reason = (
            f"the Perlector's latest reading of this act is {reading['outcome']!r}; the "
            "Recensor has not reviewed it"
        )
    else:
        category = "witnessed, awaiting the Perlector"
        reason = (
            f"{len(testimonia)} page Testimonium record(s) are sealed for this act's page; "
            "the Perlector has not read it"
        )
    return {
        "page_ordinal": act.get("page_ordinal"),
        "page_id": act.get("page_id"),
        "testimonia": testimonia,
        "reading": reading,
        "review": review,
        "established": established,
        "category": category,
        "reason": reason,
    }


_NO_READING_NOTE = (
    "the Perlector has not read the pages, so nothing in this tree counts this run's acts yet"
)
_PRE_EXPORT_ACTS_NOTE = (
    "the acts the Perlector read, its labelled other readings listed apart; a page it could "
    "not read or found blank is counted only by the export"
)


def _pages_without_entries(
    stage_records: list[dict[str, Any]], acts: tuple[dict[str, Any], ...]
) -> list[str]:
    """Each page the Perlector's page reading gave no entry, in page order, with why."""
    with_entries = {act["row"]["page_id"] for act in acts}
    pages = []
    for row in _records_of(stage_records, PERLECTOR, "page-reading"):
        if row["subject_id"] in with_entries:
            continue
        payload = _payload_of(row, "the Perlector page-reading record")
        ordinal = payload.get("page_ordinal")
        if row["outcome"] == "read":
            why = "read, no entry"
        else:
            codes = [
                str(problem.get("code"))
                for problem in payload.get("problems") or []
                if isinstance(problem, dict)
            ]
            why = f"{row['outcome']} ({', '.join(codes) or payload.get('parse_state')})"
        pages.append((not isinstance(ordinal, int), ordinal or 0, f"page {ordinal} {why}"))
    return [text for *_order, text in sorted(pages)]


def _pre_export_acts_note(
    stage_records: list[dict[str, Any]], entries: tuple[dict[str, Any], ...]
) -> str:
    """Why the pre-export act list is what it is, naming each page read without an entry.

    `entries` is every entry read, acts and other readings alike.
    """
    empty = _pages_without_entries(stage_records, entries)
    if not entries and not empty:
        return _NO_READING_NOTE
    if not empty:
        return _PRE_EXPORT_ACTS_NOTE
    return f"{_PRE_EXPORT_ACTS_NOTE}; pages read without an entry: {'; '.join(empty)}"


def _progressive_acts(
    tree: RunTree,
    stage_records: list[dict[str, Any]],
    kind: str,
) -> tuple[dict[str, Any], ...]:
    """Every entry of one `kind` the Perlector read, from its sealed `act-region`
    records, in page order.

    `kind` is `act` or `other`, so the labelled other entries stay out of the
    act list here exactly as the export keeps them out of its act layers. Each
    row says which later stage has not spoken about it. Before the Perlector
    has read a page, the list is honestly empty.
    """
    acts = []
    for row in _records_of(stage_records, PERLECTOR, "act-region"):
        payload = _payload_of(row, "the Perlector act-region record")
        if payload.get("kind") not in ACT_CLASSES:
            raise SchemaRefusal(
                f"{row['record_ref']['relative_path']}: act-region kind "
                f"{payload.get('kind')!r} is neither 'act' nor 'other'"
            )
        if payload["kind"] != kind:
            continue
        act = {
            "act_id": row["subject_id"],
            "act_key": f"p{payload.get('page_ordinal')}:{payload.get('n')}",
            "page_id": payload.get("page_id"),
            "page_ordinal": payload.get("page_ordinal"),
        }
        summary = _act_summary(stage_records, act)
        crops = _progressive_crops(tree, stage_records, act["act_id"])
        acts.append(
            {
                "act_id": act["act_id"],
                "act_key": act["act_key"],
                "category": summary["category"],
                "reason": summary["reason"],
                "crops": crops,
                "crops_note": None if crops else "the Perlector cut no crop for this entry",
                "row": summary,
                "record_ref": row["record_ref"],
            }
        )
    acts.sort(
        key=lambda act: (
            not isinstance(act["row"]["page_ordinal"], int),
            act["row"]["page_ordinal"] or 0,
            act["act_key"],
        )
    )
    return tuple(acts)


def _holds(stage_records: list[dict[str, Any]]) -> tuple[dict[str, Any], ...]:
    """Every act the Recensor's current review leaves unresolved, with its reason.

    Derived from the sealed review records rather than from the export's
    accounting, so it is the same list before and after export -- and so an
    `advance` record, which touches no review, can never make an act disappear
    from it.
    """
    rows: list[dict[str, Any]] = []
    reviewed = sorted({row["subject_id"] for row in _records_of(stage_records, RECENSOR, "review")})
    for act_id in reviewed:
        current = _latest(stage_records, RECENSOR, "review", act_id, operation="recense")
        if current is None:
            continue
        # `classify` rather than a literal set of unresolved outcomes, so
        # tomorrow's vocabulary addition is not silently dropped from this
        # list; a word outside the closed vocabulary is fatal, not skipped.
        if classify(RECENSOR, current["outcome"]) is OutcomeClass.COMPLETED:
            continue
        payload = _payload_of(current, "the Recensor review record")
        rows.append(
            {
                "act_id": act_id,
                "act_key": payload.get("act_key"),
                "source": RECENSOR,
                "label": "Recensor review",
                "outcome": current["outcome"],
                "reason": payload.get("reason"),
                "record_ref": current["record_ref"],
            }
        )
    return tuple(rows)


_DECISION_STATES: tuple[str, ...] = ("applied", "stale", "conflicting", "carried", "unkept")


def _review_decisions(tree: RunTree, stage_records: list[dict[str, Any]]) -> dict[str, Any]:
    """The decision state the Recensor's `review-decisions` record names, as display rows.

    `stored` counts the decisions in the run's receipts now, and `current`
    says whether the record's pass applied exactly that set: a decision
    recorded after it reached no review, and the Archetypus and the Armarium
    refuse until the Recensor runs again.
    """
    stored = tree.review_decision_records()
    digest_now = (
        decisions_digest(reference.sha256 for reference, _record in stored) if stored else None
    )
    row = _latest(
        stage_records,
        RECENSOR,
        REVIEW_DECISIONS_KIND,
        REVIEW_DECISIONS_SUBJECT,
        operation=REVIEW_DECISIONS_OPERATION,
    )
    if row is None:
        return {
            "present": False,
            "stored": len(stored),
            "current": digest_now is None,
            "record_ref": None,
            **{state: () for state in _DECISION_STATES},
        }
    payload = _payload_of(row, "the Recensor review-decisions record")
    return {
        "present": True,
        "stored": len(stored),
        "current": payload.get("decisions_digest") == digest_now,
        "record_ref": row["record_ref"],
        **{
            state: tuple(
                {
                    name: summary.get(name)
                    for name in (
                        "decision",
                        "scope",
                        "subject_id",
                        "page_id",
                        "finding",
                        "reason",
                        "decision_hash",
                        "stale_because",
                    )
                }
                for summary in payload.get(state) or ()
            )
            for state in _DECISION_STATES
        },
    }


_STAGE_STATE_WORDS: tuple[tuple[str, str], ...] = (
    ("sealed", "sealed"),
    ("unsealed", "wrote records and did not seal"),
    ("not-run", "left no record or seal here"),
    ("seal-invalid", "stores a seal that no longer verifies"),
)


def _stage_census(progress: tuple[dict[str, Any], ...]) -> str:
    """Every stage's state, said rather than inferred from the first non-sealed one.

    "Everything before X is sealed and nothing from X onward has run" is two
    claims about nine stages derived from one of them. A hand-run single stage
    or a partially restored tree makes both halves false while the Stages list
    on the same screen shows the truth, so this says the list, not the sentence.
    """
    if not progress:
        return "this projection lists no stages"
    grouped: dict[str, list[str]] = {}
    for row in progress:
        grouped.setdefault(row["state"], []).append(row["stage"])
    return "; ".join(
        f"{', '.join(grouped[state])} {words}"
        for state, words in _STAGE_STATE_WORDS
        if grouped.get(state)
    )


def _next_action(
    run_id: str,
    progress: tuple[dict[str, Any], ...],
    export: dict[str, Any],
    holds: tuple[dict[str, Any], ...],
    scenario: str | None = None,
    review_decisions: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """The one supported continuation, said plainly, and what `advance` is not.

    This reports; it does not act, and it never proposes a shortcut: a hold is
    resolved by an operator review decision the Recensor applies when the run
    resumes from it, or by a new run; correction of the text happens outside
    the pipeline, and `advance` records a person's permission to pass one
    sealed stage boundary without certifying a reading or clearing a hold.
    Saying so beside every hold is what keeps the boundary permission and the
    act's resolution from being read as one thing.
    """
    resume_from = None
    census = f"Stage by stage: {_stage_census(progress)}."
    # `verbatus run` takes its root from the workspace and defaults
    # `--scenario` to `happy`, so both are stated below rather than implied.
    resume_command = f"`verbatus run --run-id {run_id}"
    resume_command += f" --scenario {scenario}`" if scenario else "`"
    resume_where = " from this workspace's run root"
    if not scenario:
        resume_where += (
            ", adding `--scenario` if this run did not use the default: nothing sealed before "
            "the export names the run's scenario, so this surface cannot supply it"
        )
    damaged = [row for row in progress if row["state"] in ("seal-invalid", "unsealed")]
    if export.get("present"):
        if export.get("complete"):
            summary = "This run has a completed export."
        else:
            summary = (
                f"This run has a partial export: the export record's outcome is "
                f"{export.get('outcome')!r} over a bundle claiming "
                f"{export.get('claims_status')!r}, so not every act was delivered and this is "
                "not a finished result."
            )
        if damaged:
            summary += " Before reviewing anything: " + "; ".join(
                f"{row['stage']} {dict(_STAGE_STATE_WORDS)[row['state']]}" for row in damaged
            )
            summary += (
                ". Treat this tree as evidence to preserve and investigate; what is shown "
                "below is read from it as it stands."
            )
        summary += (
            " Review each act below against its images; a delivered act is the pipeline's "
            "machine reading, not truth."
        )
    else:
        first = next((row for row in progress if row["state"] != "sealed"), None)
        if first is None:
            summary = (
                f"{census} No export record was found; treat the tree as evidence to "
                "preserve and investigate before anything resumes."
            )
        elif first["state"] == "not-run" and first["stage"] == ARCHETYPUS and holds:
            summary = (
                f"{census} The run stopped at a held Recensor, before the Archetypus: nothing "
                "is established or exported until the holds below are decided. Record review "
                "decisions about them in this run, then resume it from the Recensor ("
                f"{resume_from_command(run_id, RECENSOR)} for a run this tool started, or "
                "the orchestrator's `--from recensor --to armarium` on a fetched tree); "
                "the Recensor applies "
                "every decision and the run continues "
                "once nothing is held. To export with holds remaining, `advance` the Recensor "
                "boundary first; the export then names every hold."
            )
            resume_from = RECENSOR
        elif first["state"] == "not-run":
            summary = (
                f"{census} The supported continuation is to resume this run with "
                f"{resume_command}{resume_where}, which picks up from {first['stage']}, the "
                "first stage with no record here; a resume reuses the sealed evidence and "
                "never rewrites it."
            )
            resume_from = first["stage"]
        elif first["state"] == "unsealed":
            summary = (
                f"{census} {first['stage']} has written records but no completion seal: it "
                "was interrupted or is still running. Do not resume while a writer may still "
                f"be active; once none is, {resume_command}{resume_where} republishes "
                f"{first['stage']}'s records byte for byte where they are unchanged and "
                "refuses where they are not."
            )
            resume_from = first["stage"]
        else:
            summary = (
                f"{census} {first['stage']}'s completion seal no longer verifies against the "
                f"evidence on disk ({first['note']}). This is not a stage that has not run; "
                "treat the tree as evidence to preserve and investigate before anything "
                "resumes."
            )
    # Distinct acts, not hold records.
    held_acts = len({hold["act_id"] for hold in holds})
    if holds:
        summary += (
            f" {held_acts} act(s) are held or unresolved, listed below as {len(holds)} "
            "record(s), each with its recorded reason. A hold is resolved by an operator "
            "review decision recorded in this run, which the Recensor applies when the run "
            "resumes from it, or by a new run over the same sealed source: `advance` records "
            "permission to pass one sealed stage boundary and neither certifies a reading nor "
            "clears a hold, and any correction of the text happens outside the pipeline."
        )
    if review_decisions and not review_decisions.get("current", True):
        summary += (
            f" This run stores {review_decisions.get('stored')} operator review decision(s) "
            "that the Recensor's last pass did not apply as a set; resume the run from the "
            "Recensor so they reach its reviews. The Archetypus and the Armarium refuse to run "
            "until it has."
        )
    return {
        "summary": summary,
        "resume_from": resume_from,
        "held_acts": held_acts,
        "hold_records": len(holds),
    }


def _image_row(
    tree: RunTree,
    row: Any,
    export_ref: dict[str, str],
) -> dict[str, Any]:
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
        return {**projected, "image_path": None, "image_sha256": None}
    image_digest = _verified_export_blob_digest(
        tree,
        stage=EXEMPLAR,
        path=row.get("image_path"),
        expected_digest=row.get("image_sha256"),
        description=f"page {row.get('ordinal')!r}",
        export_ref=export_ref,
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
    *,
    requires_crops: bool = True,
    stage_records: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """One act as the export accounts for it, plus what only the run tree holds.

    A delivered act is described entirely by its export row. A non-delivered
    one is not: the Armarium attaches `source_regions` and a witness basis
    only to an act it delivered, so a held act's crops and witnesses are read
    here from the sealed Perlector and Attestatores records instead, the same
    ones the pre-export path reads.
    """
    if not isinstance(row, dict):
        raise OperatorError(
            ErrorCode.CONSOLE_TREE_UNREADABLE,
            detail=(
                f"the Armarium export record {export_ref['relative_path']} has an act that is "
                "not an object"
            ),
        )
    # No `[]` default for a delivered act: that would make a missing
    # `source_regions` indistinguishable from a genuinely empty crop list, and
    # an operator would approve text against ink they never saw. Absent is
    # refused like malformed, except on a non-delivered act, the one shape
    # whose writer never records the field.
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
            # An exported act's regions are the Perlector's act-region crops.
            stage=PERLECTOR,
            path=region.get("image_path"),
            expected_digest=region.get("image_sha256"),
            description=(f"act {row.get('act_id')!r} source region {region.get('region_id')!r}"),
            export_ref=export_ref,
        )
        crops.append(
            {
                "ordinal": region.get("source_page_ordinal"),
                "region_id": region.get("region_id"),
                "image_path": region["image_path"],
                "image_sha256": image_digest,
            }
        )
    act_id = row.get("act_id")
    crops_note = None
    if not crops and not requires_crops and isinstance(act_id, str) and stage_records is not None:
        crops = _progressive_crops(tree, stage_records, act_id)
        crops_note = (
            "this export row records no crops; the crop below is read from the sealed "
            "Perlector act-region record"
            if crops
            else (
                "this export row records no crops, and the Perlector cut none for this act either"
            )
        )
    elif not crops:
        crops_note = "this export row records no crops"
    return {
        "act_id": act_id,
        "act_key": row.get("act_key"),
        "category": row.get("category"),
        "reason": row.get("reason"),
        "crops": crops,
        "crops_note": crops_note,
        "row": _normalised_act_row(row, export_ref, stage_records, delivered=requires_crops),
        "record_ref": export_ref,
    }


def _normalised_act_row(
    row: dict[str, Any],
    export_ref: dict[str, str],
    stage_records: list[dict[str, Any]] | None = None,
    *,
    delivered: bool = True,
) -> dict[str, Any]:
    """One field vocabulary for both act shapes, so no witness vanishes at export.

    The pre-export row carries `testimonia`; a delivered act's export row
    carries the same facts under `witnesses`, mapped onto the one spelling the
    renderer reads. A non-delivered act's export row carries no witness basis
    and no reading at all, since the Armarium writes both only for what it
    delivered, so those are read instead from the sealed Attestatores records,
    exactly as before export.
    """
    witnesses = row.get("witnesses")
    if witnesses is None and delivered:
        # A delivered row is described entirely by its export record, so one
        # without a witness basis is damaged, not thin; recovering it from the
        # run tree would show the delivered text beside a reading the export
        # never named.
        raise OperatorError(
            ErrorCode.CONSOLE_TREE_UNREADABLE,
            detail=(
                f"the Armarium export record {export_ref['relative_path']} delivers act "
                f"{row.get('act_id')!r} with no witness basis; a delivered act's export row "
                "carries one, so this row is damaged rather than thin"
            ),
        )
    if witnesses is None and stage_records is not None and isinstance(row.get("act_id"), str):
        attached: dict[str, Any] = {}
        testimonia = _testimonia_rows(stage_records, _act_page_id(stage_records, row["act_id"]))
        if testimonia:
            attached["testimonia"] = testimonia
        # Same asymmetry for the reading: the Armarium writes no text or
        # uncertainty layer for an act it did not deliver.
        reading = _reading_row(stage_records, row["act_id"])
        if reading is not None:
            attached["reading"] = reading
        return {**row, **attached} if attached else row
    if witnesses is None or "testimonia" in row:
        return row
    if not isinstance(witnesses, list):
        raise OperatorError(
            ErrorCode.CONSOLE_TREE_UNREADABLE,
            detail=(
                f"the Armarium export record {export_ref['relative_path']} act "
                f"{row.get('act_id')!r} witnesses value is not a list"
            ),
        )
    testimonia = []
    for witness in witnesses:
        if not isinstance(witness, dict):
            raise OperatorError(
                ErrorCode.CONSOLE_TREE_UNREADABLE,
                detail=(
                    f"the Armarium export record {export_ref['relative_path']} act "
                    f"{row.get('act_id')!r} has a witness that is not an object"
                ),
            )
        testimonia.append(
            {
                "chair": witness.get("chair"),
                "outcome": witness.get("outcome"),
                # The export's witness basis names one Testimonium per chair
                # and no attempt ordinal, so absent stays absent here too.
                "attempt_ordinal": witness.get("attempt_ordinal"),
                "record_ref": witness.get("testimonium_ref"),
            }
        )
    return {**row, "testimonia": testimonia}


def _advance_records(
    tree: RunTree, boundaries: dict[str, dict[str, Any]]
) -> tuple[dict[str, Any], ...]:
    """Every advance decision on record, in stable content-addressed path order.

    ``receipts/sha256/`` is content-addressed and append-only, so two advance
    records for the same stage boundary are two distinct files that both land
    here; nothing here picks between them, a stage advanced twice is left for
    the reader to notice.

    The directory also holds non-approval receipts, so only a record that
    declares the approval schema is treated as an approval; one that declares
    it and then fails full validation is a governance fact (a tampered or
    hand-written approval) and is refused loudly.

    Walked directly rather than through a manifest-style inventory, since a
    receipt is not a stage artifact and so does not inherit the manifest
    walk's refusal of a symlinked producer directory; that containment is
    asserted here instead, refusing a symlink by identity rather than
    following it.
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
    """Say whether this advance still binds its boundary.

    `advance.verify_advance` checks the same binding at advance time; this
    is the review-side check for a boundary that may have moved since.

    The digest binding is the reason the record carries a
    `target_version_hash`; this checks it against the boundary's current
    seal digest so the console never shows "this boundary was advanced" as
    present tense once the boundary has moved on. A stale record is still
    shown and still the operator's to act on.
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
    *,
    review_page: int = 1,
) -> tuple[tuple[dict[str, Any], ...] | None, int | None]:
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
        bundle_path = tree.resolve(path)
        digest = hashlib.sha256()
        with bundle_path.open("rb") as bundle_source:
            for chunk in iter(lambda: bundle_source.read(1024 * 1024), b""):
                digest.update(chunk)
            actual_digest = digest.hexdigest()
            if actual_digest != expected_digest:
                raise OperatorError(
                    ErrorCode.CONSOLE_TREE_UNREADABLE,
                    detail=(
                        f"the Armarium export record {export_ref['relative_path']} bundle {path} "
                        f"claims digest {expected_digest}, but its bytes have digest {actual_digest}"
                    ),
                )
            bundle_source.seek(0)
            with zipfile.ZipFile(bundle_source) as archive:
                members = [
                    member
                    for member in archive.infolist()
                    if member.filename == _REVIEW_ITEMS_MEMBER
                ]
                if not members:
                    return None, None
                if len(members) != 1:
                    raise OperatorError(
                        ErrorCode.CONSOLE_TREE_UNREADABLE,
                        detail="the Armarium bundle contains more than one review-items.jsonl",
                    )
                member = members[0]
                if member.compress_type != zipfile.ZIP_STORED:
                    # Every member is written stored, never compressed: a stored
                    # member's extracted size is bounded by its own physical
                    # bytes, while a compressed one can decompress far past them.
                    raise OperatorError(
                        ErrorCode.CONSOLE_TREE_UNREADABLE,
                        detail=(
                            f"the Armarium export record {export_ref['relative_path']} bundle "
                            f"{path} member review-items.jsonl is compressed, not stored; a "
                            "review bundle is only ever written stored"
                        ),
                    )
                # Unreachable through the member filter above, since `is_dir()`
                # needs a trailing separator this name cannot have; kept as a
                # defensive check with its own message rather than none.
                if member.is_dir():  # pragma: no cover - unconstructible; see the test by this name
                    raise OperatorError(
                        ErrorCode.CONSOLE_TREE_UNREADABLE,
                        detail=(
                            "the Armarium bundle names a directory at review-items.jsonl, so it "
                            "carries no review queue to read"
                        ),
                    )
                parsed: list[dict[str, Any]] = []
                count = 0
                first = (review_page - 1) * REVIEW_PAGE_SIZE
                page_bytes = 0
                with archive.open(member) as source:
                    while line := source.readline(MAX_REVIEW_ITEM_BYTES + 3):
                        count += 1
                        row_bytes = line.removesuffix(b"\n").removesuffix(b"\r")
                        if b"\r" in row_bytes:
                            raise OperatorError(
                                ErrorCode.CONSOLE_TREE_UNREADABLE,
                                detail=(
                                    f"review-items.jsonl line {count} in bundle {path} "
                                    "contains a bare carriage return; use newline-delimited rows"
                                ),
                            )
                        if len(row_bytes) > MAX_REVIEW_ITEM_BYTES:
                            raise OperatorError(
                                ErrorCode.CONSOLE_TREE_UNREADABLE,
                                detail=f"review-items.jsonl line {count} exceeds {MAX_REVIEW_ITEM_BYTES} bytes",
                            )
                        try:
                            row = json.loads(row_bytes)
                        except ValueError as error:
                            raise OperatorError(
                                ErrorCode.CONSOLE_TREE_UNREADABLE,
                                detail=f"review-items.jsonl line {count} in bundle {path} is not valid JSON: {error}",
                            ) from error
                        if not isinstance(row, dict):
                            raise OperatorError(
                                ErrorCode.CONSOLE_TREE_UNREADABLE,
                                detail=f"review-items.jsonl line {count} in bundle {path} is not an object",
                            )
                        if first < count <= first + REVIEW_PAGE_SIZE:
                            page_bytes += len(row_bytes)
                            if page_bytes > MAX_REVIEW_PAGE_BYTES:
                                raise OperatorError(
                                    ErrorCode.CONSOLE_TREE_UNREADABLE,
                                    detail=(
                                        f"review-items.jsonl line {count} in bundle {path} "
                                        f"takes review page {review_page} past "
                                        f"{MAX_REVIEW_PAGE_BYTES} bytes"
                                    ),
                                )
                            parsed.append(
                                {
                                    "row": row,
                                    "record_ref": export_ref,
                                    "bundle_path": path,
                                    "member": _REVIEW_ITEMS_MEMBER,
                                    "line": count,
                                }
                            )
                if count and first >= count:
                    raise OperatorError(
                        ErrorCode.INVALID_COMMAND,
                        detail=(
                            f"review page {review_page} is past the end of review-items.jsonl "
                            f"in bundle {path} ({count} items; last page "
                            f"{(count - 1) // REVIEW_PAGE_SIZE + 1})"
                        ),
                    )
                return tuple(parsed), count
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
