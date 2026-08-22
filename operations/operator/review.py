"""Read-only run-tree projection for the first operator console increment."""

from __future__ import annotations

import base64
import io
import json
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from common.contracts.approval import validate_approval_record
from common.contracts.canonical import digest_bytes
from common.contracts.errors import ContractError
from common.contracts.identities import artifact_id
from common.contracts.stages import ARMARIUM, STAGES
from common.runtree.store import RunTree
from common.stage import latest_attempt

from .errors import ErrorCode, OperatorError


@dataclass(frozen=True, slots=True)
class ReviewProjection:
    """Only display values and immutable byte references; never a writable tree."""

    run_id: str
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
            for stage in STAGES:
                manifest = tree.build_manifest(stage, verify_inputs=False)
                seals = [
                    tree.read_artifact(stage, "stage-seal", row["artifact_id"])
                    for row in manifest["artifacts"]
                    if row["kind"] == "stage-seal"
                ]
                if not seals:
                    boundaries.append({"stage": stage, "sealed": False, "census": []})
                    continue
                seal = latest_attempt(seals, f"{stage} stage seal", operation="seal")
                path = tree.artifact_path(stage, "stage-seal", seal["artifact_id"])
                boundaries.append(
                    {
                        "stage": stage,
                        "sealed": True,
                        "seal_artifact_id": seal["artifact_id"],
                        "seal_digest": digest_bytes(tree.read_bytes(path)),
                        "census": seal["payload"]["census"],
                    }
                )
            payload = _armarium_payload(tree)
            pages = tuple(_image_row(tree, row) for row in payload.get("pages", []))
            acts = tuple(
                _act_row(tree, row)
                for row in (*payload.get("delivered", []), *payload.get("non_delivered", []))
            )
            return ReviewProjection(
                tree.run_id,
                tuple(boundaries),
                pages,
                acts,
                _review_items(tree, payload),
                _advance_records(tree, _sealed_digests(boundaries)),
            )
        except OperatorError:
            raise
        except (ContractError, KeyError, OSError, TypeError, ValueError) as error:
            raise OperatorError(
                ErrorCode.CONSOLE_TREE_UNREADABLE,
                detail="a stage boundary or immutable image reference could not be read",
            ) from error


def _armarium_payload(tree: RunTree) -> dict[str, Any]:
    try:
        record = tree.read_artifact(
            ARMARIUM, "export", artifact_id(ARMARIUM, "export", "export", None)
        )
        payload = record["payload"]
    except (KeyError, OSError, TypeError, ValueError) as error:
        raise OperatorError(
            ErrorCode.CONSOLE_TREE_UNREADABLE,
            detail="the Armarium export record could not be read",
        ) from error
    if not isinstance(payload, dict):
        raise OperatorError(
            ErrorCode.CONSOLE_TREE_UNREADABLE, detail="the Armarium export payload is not an object"
        )
    for name in ("pages", "delivered", "non_delivered"):
        if not isinstance(payload.get(name, []), list):
            raise OperatorError(
                ErrorCode.CONSOLE_TREE_UNREADABLE, detail=f"the Armarium {name} value is not a list"
            )
    return payload


def _image_row(tree: RunTree, row: Any) -> dict[str, Any]:
    if not isinstance(row, dict) or not isinstance(row.get("image_path"), str):
        raise OperatorError(
            ErrorCode.CONSOLE_TREE_UNREADABLE, detail="a page has no immutable image path"
        )
    data = tree.read_bytes(row["image_path"])
    return {
        "ordinal": row.get("ordinal"),
        "page_id": row.get("page_id"),
        "outcome": row.get("outcome"),
        "image_sha256": digest_bytes(data),
        "image_data_url": "data:image/png;base64," + base64.b64encode(data).decode("ascii"),
    }


def _act_row(tree: RunTree, row: Any) -> dict[str, Any]:
    if not isinstance(row, dict):
        raise OperatorError(
            ErrorCode.CONSOLE_TREE_UNREADABLE, detail="an Armarium act is not an object"
        )
    crops = []
    for region in row.get("source_regions", []):
        if not isinstance(region, dict) or not isinstance(region.get("image_path"), str):
            raise OperatorError(
                ErrorCode.CONSOLE_TREE_UNREADABLE, detail="an act crop has no immutable image path"
            )
        data = tree.read_bytes(region["image_path"])
        crops.append(
            {
                "ordinal": region.get("source_page_ordinal"),
                "region_id": region.get("region_id"),
                "image_sha256": digest_bytes(data),
                "image_data_url": "data:image/png;base64," + base64.b64encode(data).decode("ascii"),
            }
        )
    return {
        "act_id": row.get("act_id"),
        "act_key": row.get("act_key"),
        "category": row.get("category"),
        "crops": crops,
    }


ADVANCE_SUBJECT_PREFIX = "stage-boundary:"


def _sealed_digests(boundaries: list[dict[str, Any]]) -> dict[str, str]:
    """The digest each stage's seal has *right now*, by stage."""

    return {row["stage"]: row["seal_digest"] for row in boundaries if row["sealed"]}


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


def _review_items(tree: RunTree, payload: dict[str, Any]) -> tuple[dict[str, Any], ...] | None:
    bundle = payload.get("bundle")
    reference = bundle.get("reference") if isinstance(bundle, dict) else None
    path = reference.get("relative_path") if isinstance(reference, dict) else None
    if not isinstance(path, str):
        return None
    try:
        with zipfile.ZipFile(io.BytesIO(tree.read_bytes(path))) as archive:
            if "review-items.jsonl" not in archive.namelist():
                return None
            return tuple(
                json.loads(line) for line in archive.read("review-items.jsonl").splitlines()
            )
    except (OSError, ValueError, zipfile.BadZipFile) as error:
        raise OperatorError(
            ErrorCode.CONSOLE_TREE_UNREADABLE, detail="review-items.jsonl could not be read"
        ) from error
