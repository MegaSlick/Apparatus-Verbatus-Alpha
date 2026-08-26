"""Read-only run-tree projection for the first operator console increment."""

from __future__ import annotations

import io
import json
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from common.contracts.approval import validate_approval_record
from common.contracts.canonical import digest_bytes
from common.contracts.errors import ContractError, SchemaRefusal
from common.contracts.identities import artifact_id
from common.contracts.stages import ARMARIUM, DESIGNATOR, EXEMPLAR, STAGES
from common.runtree.store import RunTree
from common.stage import latest_attempt

from .errors import ErrorCode, OperatorError


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
                    boundaries.append({"stage": stage, "sealed": False, "census": []})
                    continue
                current = latest_attempt(
                    [row["record"] for row in seals], f"{stage} stage seal", operation="seal"
                )
                seal = next(row for row in seals if row["artifact_id"] == current["artifact_id"])
                boundaries.append(
                    {
                        "stage": stage,
                        "sealed": True,
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
            payload, export_ref = _armarium_payload(tree, stage_records)
            pages = tuple(_image_row(tree, row, export_ref) for row in payload["pages"])
            acts = tuple(
                _act_row(tree, row, export_ref)
                for row in (*payload["delivered"], *payload["non_delivered"])
            )
            return ReviewProjection(
                tree.run_id,
                tuple(stage_records),
                tuple(boundaries),
                pages,
                acts,
                _review_items(tree, payload, export_ref),
                _advance_records(
                    tree,
                    {row["stage"]: row["seal_digest"] for row in boundaries if row["sealed"]},
                ),
            )
        except OperatorError:
            raise
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
) -> str:
    """A fresh digest must not repair a contradictory exported path or digest."""

    export_path = export_ref["relative_path"]
    if not isinstance(path, str) or not isinstance(expected_digest, str):
        raise OperatorError(
            ErrorCode.CONSOLE_TREE_UNREADABLE,
            detail=(
                f"the Armarium export record {export_path} {description} has no immutable "
                "image path and digest"
            ),
        )
    expected_path = tree.blob_path(stage, expected_digest)
    if path != expected_path:
        raise OperatorError(
            ErrorCode.CONSOLE_TREE_UNREADABLE,
            detail=(
                f"the Armarium export record {export_path} {description} claims digest "
                f"{expected_digest} but names {path}, not its content-addressed path "
                f"{expected_path}"
            ),
        )
    data = tree.read_bytes(path)
    actual_digest = digest_bytes(data)
    if actual_digest != expected_digest:
        raise OperatorError(
            ErrorCode.CONSOLE_TREE_UNREADABLE,
            detail=(
                f"the Armarium export record {export_path} {description} names {path} with "
                f"digest {expected_digest}, but its bytes have digest {actual_digest}"
            ),
        )
    return actual_digest


def _image_row(tree: RunTree, row: Any, export_ref: dict[str, str]) -> dict[str, Any]:
    if not isinstance(row, dict):
        raise OperatorError(
            ErrorCode.CONSOLE_TREE_UNREADABLE,
            detail=(
                f"the Armarium export record {export_ref['relative_path']} has a page that is "
                "not an object"
            ),
        )
    image_digest = _verified_export_blob_digest(
        tree,
        stage=EXEMPLAR,
        path=row.get("image_path"),
        expected_digest=row.get("image_sha256"),
        description=f"page {row.get('ordinal')!r}",
        export_ref=export_ref,
    )
    return {
        "ordinal": row.get("ordinal"),
        "page_id": row.get("page_id"),
        "outcome": row.get("outcome"),
        "image_path": row["image_path"],
        "image_sha256": image_digest,
        "record_ref": export_ref,
    }


def _act_row(tree: RunTree, row: Any, export_ref: dict[str, str]) -> dict[str, Any]:
    if not isinstance(row, dict):
        raise OperatorError(
            ErrorCode.CONSOLE_TREE_UNREADABLE,
            detail=(
                f"the Armarium export record {export_ref['relative_path']} has an act that is "
                "not an object"
            ),
        )
    source_regions = row.get("source_regions", [])
    if not isinstance(source_regions, list):
        raise OperatorError(
            ErrorCode.CONSOLE_TREE_UNREADABLE,
            detail=(
                f"the Armarium export record {export_ref['relative_path']} act "
                f"{row.get('act_id')!r} source_regions value is not a list"
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
        "crops": crops,
        "row": row,
        "record_ref": export_ref,
    }


ADVANCE_SUBJECT_PREFIX = "stage-boundary:"


def _advance_records(tree: RunTree, sealed: dict[str, str]) -> tuple[dict[str, Any], ...]:
    """Every advance decision on record for this run, oldest reference-name first.

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
    """

    receipts_dir = tree.resolve("receipts/sha256")
    if not receipts_dir.is_dir():
        return ()
    records: list[dict[str, Any]] = []
    for path in sorted(receipts_dir.glob("*.json")):
        relative_path = path.relative_to(tree.root).as_posix()
        data = tree.read_bytes(relative_path)
        try:
            decoded = json.loads(data.decode("utf-8"))
        except (UnicodeDecodeError, ValueError) as error:
            raise OperatorError(
                ErrorCode.CONSOLE_TREE_UNREADABLE,
                detail=f"receipt {relative_path} is not valid JSON",
            ) from error
        if not isinstance(decoded, dict) or decoded.get("schema") != "approval-record.v0":
            continue  # not an approval record; some other receipt kind
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
                **_still_binds(record, sealed),
            }
        )
    return tuple(records)


def _still_binds(record: dict[str, Any], sealed: dict[str, str]) -> dict[str, Any]:
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
    current = sealed.get(stage)
    if current is None:
        return {
            "boundary_stage": stage,
            "boundary_current": False,
            "boundary_note": f"{stage} has no stored stage-seal now, so this advance binds a boundary that is no longer there",
        }
    if current != record["target_version_hash"]:
        return {
            "boundary_stage": stage,
            "boundary_current": False,
            "boundary_note": f"{stage}'s seal changed after this advance was recorded; the advance binds an earlier boundary",
        }
    return {"boundary_stage": stage, "boundary_current": True, "boundary_note": None}


def _review_items(
    tree: RunTree, payload: dict[str, Any], export_ref: dict[str, str]
) -> tuple[dict[str, Any], ...] | None:
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
        bundle_bytes = tree.read_bytes(path)
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
            if "review-items.jsonl" not in archive.namelist():
                return None
            return tuple(
                {
                    "row": json.loads(line),
                    "record_ref": export_ref,
                    "bundle_path": path,
                    "member": "review-items.jsonl",
                    "line": number,
                }
                for number, line in enumerate(
                    archive.read("review-items.jsonl").splitlines(), start=1
                )
            )
    except (OSError, ValueError, zipfile.BadZipFile) as error:
        raise OperatorError(
            ErrorCode.CONSOLE_TREE_UNREADABLE,
            detail=(
                f"review-items.jsonl could not be read from bundle {path}: "
                f"{type(error).__name__}: {error}"
            ),
        ) from error
