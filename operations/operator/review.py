"""Project sealed run-tree evidence into a read-only operator shape."""

from __future__ import annotations

import io
import json
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from common.contracts.approval import validate_approval_record
from common.contracts.canonical import digest_bytes, verify_self_hash
from common.contracts.errors import ContractError, SchemaRefusal
from common.contracts.identities import artifact_id
from common.contracts.outcomes import OutcomeClass, classify
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
    # `export`      what the export record itself says -- whether one exists,
    #               whether the Armarium then sealed, the record's own outcome
    #               and its bundle's claims status -- and what that means for
    #               the rows above (`present`, `record_present`,
    #               `boundary_sealed`, `outcome`, `claims_status`, `complete`,
    #               `record_ref`, `review_items_absent_because`, `note`)
    # `progress`    one row per stage: sealed, unsealed (interrupted or still
    #               running), not-run, or seal-invalid -- a stage that has not
    #               run is named as that, never as corruption, and a seal that
    #               no longer verifies is named as that, never as not-run
    # `holds`       every act the Designator or the Recensor left unresolved,
    #               with the recorded reason, pre- and post-export alike
    # `next_action` the one supported continuation in plain words, and what
    #               `advance` does and does not do about a hold
    #
    # `pages_declared` how many source pages the run authority itself declared,
    #               so the Pages list carries the denominator the Armarium
    #               reconciles against rather than a count of records found.
    export: dict[str, Any] = field(default_factory=dict)
    progress: tuple[dict[str, Any], ...] = ()
    holds: tuple[dict[str, Any], ...] = ()
    next_action: dict[str, Any] = field(default_factory=dict)
    pages_declared: int | None = None
    pages_declared_note: str | None = None


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
            armarium_state = next(row["state"] for row in progress if row["stage"] == ARMARIUM)
            export_rows = _export_rows(stage_records)
            if export_rows:
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
                export = _export_state(
                    export_rows[0], payload, export_ref, armarium_state, review_items
                )
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
                next_action=_next_action(tree.run_id, progress, export, holds),
                pages_declared=declared_pages,
                pages_declared_note=declared_note,
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


def _export_state(
    export_row: dict[str, Any],
    payload: dict[str, Any],
    export_ref: dict[str, str],
    armarium_state: str,
    review_items: tuple[dict[str, Any], ...] | None,
) -> dict[str, Any]:
    """What the export record says about itself, never what its existence suggests.

    Three facts were on disk and none of them reached the screen. The Armarium
    writes an export record for a *partial* export as readily as a complete one
    -- outcome `held-for-review` over a bundle claiming `partial` whenever an
    act was not delivered (`pipeline/7_armarium/run.py`) -- and it writes that
    record before it seals its own boundary, so a run killed between the two
    leaves an export record behind a stage this same screen calls interrupted.
    Deriving "a completed export" from the record's mere presence announced
    both as finished work (ARCHITECTURE invariant 6, GOVERNANCE 2).

    So `present` means what a reader takes it to mean: a sealed Armarium with
    an export record under it. `complete` is the further question the record's
    own outcome and claims status answer, and the note says which of the three
    states this is in words.

    `review_items_absent_because` separates the two silences behind an empty
    review queue -- no export at all, and an export whose bundle carries no
    `review-items.jsonl` member because the format was not configured -- which
    the renderer previously reported with one sentence naming only the first.
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

    "Pages (2)" was a count of the page records found, which is the numerator
    twice over: a run whose Exemplar never wrote a record for a declared source
    showed a shorter list with nothing saying it was short. The run authority
    carries the declared ledger and `read_run` refuses it unless its own
    self-hash still verifies, which is the denominator the Armarium reconciles
    its census against (`pipeline/7_armarium/run.py`).

    A denominator that cannot be read does not close this surface. This screen
    exists to open damaged trees, and refusing the whole view because the run
    authority is missing or no longer verifies would take the images and the
    reason the run stopped away over a page count -- the F3 shape again, one
    field further in. The refusal is carried as a note beside the count
    instead, so the reader is told the denominator is unavailable and why,
    rather than shown a bare count that looks whole.
    """
    try:
        manifest = tree.read_run().get("source_manifest")
    except (ContractError, OSError) as error:
        return (
            None,
            "the run authority could not be read, so nothing declares a page count: "
            f"{type(error).__name__}: {error}",
        )
    if not isinstance(manifest, list):
        return None, "the run authority declares no source manifest, so there is no page count"
    return len(manifest), None


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
            # Absence is reported as absence. "This stage has not run" is an
            # inference from an empty directory, and a stage whose evidence was
            # deleted wholesale looks exactly the same from here.
            note = (
                "no record and no seal found here: this stage has not run, or nothing it "
                "wrote is in this tree"
            )
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


_TERMINAL_DESIGNATOR_REASONS: dict[str, tuple[str, str]] = {
    "excluded": (
        "excluded by the Designator",
        "the Designator excluded this act with approval; that ends it here, and no witness, "
        "reading or review will follow",
    ),
    "failed": (
        "failed at the Designator",
        "the Designator could not mark this act out; that ends it here, and no witness, "
        "reading or review will follow",
    ),
}


def _act_summary(stage_records: list[dict[str, Any]], act: dict[str, Any]) -> dict[str, Any]:
    """What the stages that ran say about one act, and where it stands.

    The category is derived from the furthest stage that has spoken about the
    act, in pipeline order, and says plainly which stage has not: "witnessed,
    awaiting the Perlector" is a different fact from "held-for-review", and a
    reader must not have to infer either from a missing row. The one thing read
    before any of that is the act's own Designator outcome, because `excluded`
    and `failed` end the act there and nothing downstream is coming. Text shown here is
    the Perlector's machine reading, never an established or delivered one --
    the Archetypus and the Armarium are the two stages that would make it so,
    and their absence is exactly what this view is for.
    """
    act_id = act["act_id"]
    designator_outcome = act.get("outcome")
    designator_holds = []
    for row in _records_of(stage_records, DESIGNATOR, "hold", act_id):
        payload = _payload_of(row, "the Designator hold record")
        designator_holds.append(
            {
                "reason_code": payload.get("reason_code"),
                "reason": payload.get("reason"),
                "record_ref": row["record_ref"],
            }
        )
    # Every attempt stays listed -- nothing here selects among witnesses (hard
    # rule 8) -- and each now says which attempt it was, because two rows for
    # one chair with contradictory outcomes and no ordinal between them leave
    # the reader unable to tell the current reading from the superseded one.
    testimonia = []
    for row in _records_of(stage_records, ATTESTATORES, "testimonium", act_id):
        payload = _payload_of(row, "the Testimonium record")
        testimonia.append(
            {
                "chair": payload.get("chair"),
                "outcome": row["outcome"],
                "attempt_ordinal": payload.get("attempt_ordinal"),
                "record_ref": row["record_ref"],
            }
        )
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
    if len(established_rows) > 1:
        # The Archetypus writes exactly one record per act and carries no
        # attempt token, so a second one is a tree this console cannot read,
        # not a newer version to prefer. `established_rows[-1]` resolved it by
        # directory order -- the positional default `latest_attempt` exists to
        # refuse (`common/stage.py`) -- and would have shown one of two
        # established texts with nothing saying the other was there.
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

    if designator_outcome in _TERMINAL_DESIGNATOR_REASONS:
        # `excluded` and `failed` end the act at the Designator: no witness will
        # report on it and no later stage will speak, so this is read before any
        # downstream record, not after. Falling through to the `proposed`
        # default labelled a closed act "marked out, awaiting witnesses" and
        # left a person waiting for a stage that is never going to run. The
        # Recensor refuses the same case by name rather than guessing
        # (`pipeline/5_recensor/test_designator_terminal_outcomes.py`).
        category, reason = _TERMINAL_DESIGNATOR_REASONS[designator_outcome]
    elif established is not None:
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
    elif designator_outcome == "held":
        category = "held by the Designator"
        reason = (
            "the proposal seal records this act as held by the Designator, and no hold "
            "record carrying the reason was found beside it"
        )
    elif designator_outcome == "proposed":
        category = "marked out, awaiting witnesses"
        reason = "the Designator marked this act out; no witness has reported on it"
    else:
        # Reachable the day the Designator's vocabulary widens. `classify` has
        # already refused an outcome outside that vocabulary, so what lands here
        # is a word the vocabulary knows and this surface has no sentence for --
        # which is a sentence to write, not a row to describe with the wrong one.
        raise OperatorError(
            ErrorCode.CONSOLE_TREE_UNREADABLE,
            detail=(
                f"act {act_id!r} carries Designator outcome {designator_outcome!r}, which is "
                "in the stage's vocabulary but has no plain-language reading on this surface"
            ),
        )
    return {
        "designator_outcome": designator_outcome,
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


_PROPOSAL_SEAL_ACT_FIELDS = frozenset(
    {"act_id", "act_key", "page_id", "page_ordinal", "has_continuation", "outcome", "evidence"}
)


def _seal_refusal(seal: dict[str, Any], said: str) -> OperatorError:
    return OperatorError(
        ErrorCode.CONSOLE_TREE_UNREADABLE,
        detail=f"the Designator proposal seal {seal['record_ref']['relative_path']} {said}",
    )


def _expected_acts(
    stage_records: list[dict[str, Any]],
) -> tuple[dict[str, Any], list[dict[str, Any]]] | None:
    """The act denominator, read with the checks the pipeline reads it with.

    `common/stage.py::expected_acts` is the one reader all five downstream
    consumers use, and the console must not show a shorter act list than the
    seal itself claims while every stage below reads the longer one. It cannot
    be called here: it needs a live `StageContext` -- a run authority, the
    sealed serving catalogue, real-ingress evidence -- that a read-only surface
    has no business constructing, and it re-opens the tree for a second read
    that this projection exists to avoid (one projection, one snapshot, or one
    view describes two versions of a run).

    What is reused is every check it makes about the seal, through the same
    shared functions rather than a second spelling of them: the canonical
    proposal-seal artifact id (`artifact_id`, as `_armarium_payload` pins the
    export), `verify_self_hash` over the payload, `count` reconciling with the
    rows, the closed field set on each row, no act id or key twice, and
    `classify(DESIGNATOR, ...)` over each row's outcome. Each disagreement
    refuses by name.

    `None` means the Designator has not sealed a proposal, and the act list is
    then honestly empty rather than invented from region records.
    """
    seals = _records_of(stage_records, DESIGNATOR, "proposal-seal")
    if not seals:
        return None
    if len(seals) != 1:
        raise OperatorError(
            ErrorCode.CONSOLE_TREE_UNREADABLE,
            detail=(
                f"the Designator proposal seal appeared {len(seals)} times; review requires "
                "exactly one immutable act denominator"
            ),
        )
    seal = seals[0]
    canonical_id = artifact_id(DESIGNATOR, "proposal-seal", "proposal-seal", None)
    if seal["artifact_id"] != canonical_id:
        raise _seal_refusal(
            seal,
            f"is artifact {seal['artifact_id']}, not the run's one canonical proposal seal "
            f"{canonical_id}",
        )
    payload = _payload_of(seal, "the Designator proposal seal")
    if not verify_self_hash(payload):
        raise _seal_refusal(seal, "does not verify against its own self-hash")
    expected = payload.get("expected_acts")
    count = payload.get("count")
    if not isinstance(expected, list) or not expected:
        raise _seal_refusal(seal, "names no expected acts")
    if not isinstance(count, int) or isinstance(count, bool) or count != len(expected):
        raise _seal_refusal(
            seal,
            f"declares count {count!r} over {len(expected)} expected-act row(s); the two do "
            "not reconcile, so this surface cannot say how many acts the run has",
        )
    seen_ids: set[str] = set()
    seen_keys: set[str] = set()
    for act in expected:
        if not isinstance(act, dict):
            raise _seal_refusal(seal, "names an expected act that is not an object")
        if set(act) != _PROPOSAL_SEAL_ACT_FIELDS:
            raise _seal_refusal(
                seal,
                f"has an expected-act row with fields {sorted(act)}, not the closed "
                f"denominator contract {sorted(_PROPOSAL_SEAL_ACT_FIELDS)}",
            )
        if not isinstance(act["act_id"], str) or not act["act_id"]:
            raise _seal_refusal(seal, "names an expected act with no act_id")
        if not isinstance(act["act_key"], str) or not act["act_key"]:
            raise _seal_refusal(seal, f"names act {act['act_id']!r} with no act_key")
        if act["act_id"] in seen_ids or act["act_key"] in seen_keys:
            raise _seal_refusal(
                seal,
                f"names act id {act['act_id']!r} or key {act['act_key']!r} more than once; a "
                "duplicate is not an additional act",
            )
        seen_ids.add(act["act_id"])
        seen_keys.add(act["act_key"])
        # Fatal in `expected_acts` and fatal here: an outcome outside the closed
        # vocabulary is invariant #10's imbalance, never a row to read as
        # marked-out because the word was unfamiliar.
        classify(DESIGNATOR, act["outcome"])
    return seal, expected


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
    found = _expected_acts(stage_records)
    if found is None:
        return ()
    seal, expected = found
    acts = []
    for act in expected:
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
    designator_held: set[str] = set()
    for row in _records_of(stage_records, DESIGNATOR, "hold"):
        payload = _payload_of(row, "the Designator hold record")
        designator_held.add(row["subject_id"])
        rows.append(
            {
                "act_id": row["subject_id"],
                "act_key": payload.get("act_key"),
                "source": DESIGNATOR,
                "label": "Designator hold",
                "outcome": row["outcome"],
                "reason": payload.get("reason"),
                "audit_examination": None,
                "record_ref": row["record_ref"],
            }
        )
    reviewed = sorted({row["subject_id"] for row in _records_of(stage_records, RECENSOR, "review")})
    for act_id in reviewed:
        current = _latest(stage_records, RECENSOR, "review", act_id, operation="recense")
        if current is None:
            continue
        # The set of unresolved outcomes is not spelled out here. A literal
        # frozenset happened to equal today's vocabulary and would have silently
        # dropped tomorrow's addition out of this list -- and `continue` past an
        # unrecognised outcome is the same silence with a different shape.
        # `classify` is the pipeline's own reading of the word: a COMPLETED
        # outcome is resolved and belongs in no hold list, anything else is
        # unresolved or failed and belongs in this one, and a word outside the
        # closed vocabulary is fatal rather than skipped.
        if classify(RECENSOR, current["outcome"]) is OutcomeClass.COMPLETED:
            continue
        payload = _payload_of(current, "the Recensor review record")
        rows.append(
            {
                "act_id": act_id,
                "act_key": payload.get("act_key"),
                "source": RECENSOR,
                # One act held by the Designator is reviewed as held by the
                # Recensor too, which quotes the hold
                # (`pipeline/5_recensor/run.py`). Both records stay listed --
                # they are two sealed facts -- and each now says which it is, so
                # two rows read as one act twice attested rather than two acts.
                "label": (
                    "Recensor review of that hold"
                    if act_id in designator_held
                    else "Recensor review"
                ),
                "outcome": current["outcome"],
                "reason": payload.get("reason"),
                "audit_examination": payload.get("audit_examination"),
                "record_ref": current["record_ref"],
            }
        )
    return tuple(rows)


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
    on the same screen shows the truth, so the sentence is now the list.
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
    census = f"Stage by stage: {_stage_census(progress)}."
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
        # The post-export sentence used to ignore the stage states entirely and
        # invite review over a tree whose own seals no longer verified.
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
        elif first["state"] == "not-run":
            # The operator's own word, complete enough to type. The
            # orchestrator's `--from`/`--to` spelling named a developer command
            # that also omitted the arguments it needs, so the one supported
            # next action could not be run as printed.
            summary = (
                f"{census} The supported continuation is to resume this run with `verbatus "
                f"run --run-id {run_id}`, which picks up from {first['stage']}, the first "
                "stage with no record here; a resume reuses the sealed evidence and never "
                "rewrites it."
            )
            resume_from = first["stage"]
        elif first["state"] == "unsealed":
            summary = (
                f"{census} {first['stage']} has written records but no completion seal: it "
                "was interrupted or is still running. Do not resume while a writer may still "
                f"be active; once none is, `verbatus run --run-id {run_id}` republishes "
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
    # Acts, not records. One act held by the Designator and reviewed as held by
    # the Recensor is two rows below and one act to resolve; counting rows
    # reported two acts held on a run that had one.
    held_acts = len({hold["act_id"] for hold in holds})
    if holds:
        summary += (
            f" {held_acts} act(s) are held or unresolved, listed below as {len(holds)} "
            "record(s), each with its recorded reason. A hold is resolved only by a new "
            "authorized run over the same sealed source: `advance` records permission to pass "
            "one sealed stage boundary and neither certifies a reading nor clears a hold, and "
            "any correction of the text happens outside the pipeline."
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
        "row": _normalised_act_row(row, export_ref),
        "record_ref": export_ref,
    }


def _normalised_act_row(row: dict[str, Any], export_ref: dict[str, str]) -> dict[str, Any]:
    """One field vocabulary for both act shapes, so nothing vanishes at export.

    The pre-export row carries `testimonia`; the export's own row carries the
    same facts under `witnesses`, with the Recensor review named by reference
    rather than quoted. A renderer reading one vocabulary showed every witness
    before export and none after it -- the same screen, the same run, fewer
    facts once it finished. The export's row is carried whole and the
    projection adds the pre-export spellings beside it; nothing is renamed
    away, and no field is invented for a shape that does not carry it.
    """
    witnesses = row.get("witnesses")
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
                # The export carries the evidence-backed witness basis, which
                # names one Testimonium per chair and no attempt ordinal. Absent
                # is left absent rather than filled with a number nothing said.
                "attempt_ordinal": witness.get("attempt_ordinal"),
                "record_ref": witness.get("testimonium_ref"),
            }
        )
    return {**row, "testimonia": testimonia}


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
