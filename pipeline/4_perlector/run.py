"""Perlector: reads the ink, with the testimonia as fallible clues.

The record verifies its region evidence (digest against the sealed reference, decoded
size against the claimed transform), records every region and testimonium it saw by
reference, and never counts witnesses: dissent is computed after the reading is fixed
and cannot reach back into it (principle 1).

The sealed serving-recipe row picks the reader. A `kind = "vllm"` row for the Perlector
chair selects `live_reader.VLLMReader`; any other row selects the fixture reader, whose
text comes from the fixture and proves wiring only. A real submission has no fixture
declaration, so a non-live row there refuses (`fixture_reader_for`).

Dissent records where the reading departed from each witness, which makes parroting
measurable. It is not a quality signal: on easy lines every witness agrees and zero
dissent is correct.

    python pipeline/4_perlector/run.py --run-root <dir> --run-id <id>
"""

import copy
import json
import os
import stat
import sys
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from functools import partial
from pathlib import Path
from typing import Any, Final

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import annotations  # noqa: E402
import audit  # noqa: E402
import combined  # noqa: E402
import dossier as dossier_module  # noqa: E402
import logical_reading  # noqa: E402
import nuda  # noqa: E402
import prompts  # noqa: E402
import protocol  # noqa: E402
import regime  # noqa: E402
import truncation  # noqa: E402
from dissent import departures, dissent_against, validate_dissent  # noqa: E402
from live_reader import EngineSignalRefusal, VLLMReader  # noqa: E402
from reader import FixtureReader, validate_audit_delivery  # noqa: E402

import operations.serving.errors as serving_errors  # noqa: E402
from common.alignment import bracket_marker_view, markup_text_view  # noqa: E402
from common.chairs.models import AbsentChair, ChairIdentity  # noqa: E402
from common.chairs.registry import ChairRegistry  # noqa: E402
from common.chandra_native_retry import validate_trace as validate_chandra_trace  # noqa: E402
from common.contracts.approval import (  # noqa: E402
    ApprovalRecordBinding,
    ApprovalRecordReference,
    validate_approval_record,
)
from common.contracts.canonical import canonical_bytes, digest_bytes, digest_of  # noqa: E402
from common.contracts.envelope import build_envelope, validate_input_refs  # noqa: E402
from common.contracts.errors import (  # noqa: E402
    ApprovalRefusal,
    ContractError,
    FatalAccounting,
    SchemaRefusal,
)
from common.contracts.identities import artifact_id, perlector_attempt_id  # noqa: E402
from common.contracts.outcomes import ATTACHMENT_BASES, page_attachment_basis  # noqa: E402
from common.contracts.stages import ATTESTATORES, DESIGNATOR, EXEMPLAR, PERLECTOR  # noqa: E402
from common.corpus_register import refuse_capture_preference  # noqa: E402
from common.cross_capture_autopsia import (  # noqa: E402
    atomic_delivered_pixels,
    over_capacity_reason,
    validate_autopsia,
)
from common.decoding import load_decoding_policy  # noqa: E402
from common.exemplar_boundary import verify_exemplar_crop_lineage  # noqa: E402
from common.imaging import dimensions  # noqa: E402
from common.native_witness import (  # noqa: E402
    reported_geometry_overlaps,
    unpresented_region_ids,
    unrouted_observations,
    validate_native_witness_geometry,
    validate_page_testimonium_payload,
    validate_presented_page_binding,
    verify_native_capture_blob,
)
from common.perlector_failure import (  # noqa: E402
    PRE_PERLECTIO_ARTIFACTS,
    validate_failed_perlectio,
)
from common.request_capacity import RequestCapacityRefusal  # noqa: E402
from common.runtree.store import RECEIPTS_DIR  # noqa: E402
from common.stage import (  # noqa: E402
    ATTEMPTED_WITNESS_OUTCOMES,
    EXIT_COMPLETE,
    NUDA_APPROVAL_SUBJECT,
    PERLECTOR_CHAIR,
    PERLECTOR_INSTRUMENT_APPROVAL_SUBJECT,
    WITNESS_CONTEXT_REGIMES,
    WITNESS_READING_OUTCOMES,
    expected_acts,
    fixture_serving_details,
    is_real_ingress,
    latest_attempt,
    latest_per_chair,
    open_stage_context,
    reading_basis_regions,
    recovery_region_count,
    run_stage,
    stage_manifest,
    stage_parser,
    validate_serving_provenance,
)
from operations.serving.client import ChairClient, serving_mode_for  # noqa: E402
from operations.serving.config import (  # noqa: E402
    ServingConfigInputs,
    load_serving_recipes,
)
from operations.serving.errors import ChairResponseRefusal  # noqa: E402
from operations.serving.http import EndpointUnavailable, UrllibHttpTransport  # noqa: E402
from operations.serving.manager import (  # noqa: E402
    MECHANICS_QUALIFICATION_PURPOSE,
    ServingManager,
    StageContextReceiptPublisher,
)
from operations.serving.process import SubprocessLauncher  # noqa: E402
from operations.serving.residency import (  # noqa: E402
    POD_RESIDENCY_LOCK_PATH,
    FileResidencyLease,
)

_ACT_LOCAL_READING_FAILURES: Final = (
    EngineSignalRefusal,
    ChairResponseRefusal,
    EndpointUnavailable,
    RequestCapacityRefusal,
    serving_errors.ChairTransportFailure,
)

# The sealed selector cannot hold the digest of an approval that targets its own config
# digest, so the sampling gate scans the receipt directory. These bounds make a planted
# directory a named refusal; real receipts are a few kilobytes.
MAX_SAMPLING_APPROVAL_RECEIPTS: Final = 100_000
MAX_SAMPLING_APPROVAL_RECEIPT_BYTES: Final = 4 * 1024 * 1024
MAX_SAMPLING_APPROVAL_SCAN_BYTES: Final = 1024 * 1024 * 1024


def _receipt_name_digest(name: str) -> str | None:
    suffix = ".json"
    if not name.endswith(suffix):
        return None
    digest = name[: -len(suffix)]
    if len(digest) != 64 or any(character not in "0123456789abcdef" for character in digest):
        return None
    return digest


def _open_receipts_directory(tree) -> int | None:
    """Open the receipt directory one component at a time without following links.

    `RunTree.resolve` follows symlinks, so a receipt alias could change through its other
    name after the approval was checked; descriptors pin each lookup to the opened inode.
    """
    no_follow = getattr(os, "O_NOFOLLOW", None)
    directory = getattr(os, "O_DIRECTORY", None)
    if no_follow is None or directory is None:
        raise ContractError(
            "the sampling approval gate cannot open receipt evidence without following links "
            "on this platform"
        )
    flags = os.O_RDONLY | os.O_NONBLOCK | no_follow | directory
    context_root = tree.root
    try:
        current = os.open(context_root, flags)
    except OSError as error:
        raise ContractError(
            f"run tree {context_root} could not be opened without following a redirect while "
            "resolving sampling approval"
        ) from error
    try:
        for component in RECEIPTS_DIR.split("/"):
            try:
                child = os.open(component, flags, dir_fd=current)
            except FileNotFoundError:
                os.close(current)
                return None
            except OSError as error:
                raise ContractError(
                    f"receipt directory {RECEIPTS_DIR!r} could not be opened without following "
                    "a redirect; sampling approval evidence must be plain directories"
                ) from error
            os.close(current)
            current = child
        return current
    except BaseException:
        os.close(current)
        raise


def _stable_file_metadata(details: os.stat_result) -> tuple[int, ...]:
    return (
        details.st_dev,
        details.st_ino,
        details.st_mode,
        details.st_size,
        details.st_mtime_ns,
        details.st_ctime_ns,
        details.st_nlink,
    )


def _read_receipt_bytes(directory_descriptor: int, name: str, relative_path: str) -> bytes:
    no_follow = getattr(os, "O_NOFOLLOW", None)
    if no_follow is None:
        raise ContractError(
            "the sampling approval gate cannot read receipt evidence without following links "
            "on this platform"
        )
    try:
        descriptor = os.open(
            name,
            os.O_RDONLY | os.O_NONBLOCK | no_follow,
            dir_fd=directory_descriptor,
        )
    except OSError as error:
        raise ContractError(
            f"receipt {relative_path!r} could not be opened without following a redirect while "
            "resolving sampling approval"
        ) from error
    try:
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode):
            raise ContractError(
                f"receipt {relative_path!r} is not a regular file; the sampling gate reads only "
                "immutable receipt files"
            )
        if before.st_nlink != 1:
            raise ContractError(
                f"receipt {relative_path!r} has {before.st_nlink} hard links; approval evidence "
                "must have one immutable content-addressed name"
            )
        if before.st_size > MAX_SAMPLING_APPROVAL_RECEIPT_BYTES:
            raise ContractError(
                f"receipt {relative_path!r} is larger than the "
                f"{MAX_SAMPLING_APPROVAL_RECEIPT_BYTES}-byte sampling-approval bound and was "
                "not read"
            )
        with os.fdopen(descriptor, "rb") as handle:
            descriptor = -1
            data = handle.read(MAX_SAMPLING_APPROVAL_RECEIPT_BYTES + 1)
            after = os.fstat(handle.fileno())
        if len(data) > MAX_SAMPLING_APPROVAL_RECEIPT_BYTES:
            raise ContractError(
                f"receipt {relative_path!r} grew beyond the "
                f"{MAX_SAMPLING_APPROVAL_RECEIPT_BYTES}-byte sampling-approval bound while "
                "it was read"
            )
        if _stable_file_metadata(before) != _stable_file_metadata(after):
            raise ContractError(
                f"receipt {relative_path!r} changed while it was read; moving evidence cannot "
                "authorize a sampling arm"
            )
        return data
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def _receipt_names(directory: int, *, subject: str) -> list[str]:
    """Every entry in the receipt directory, refusing an unbounded or case-colliding listing."""
    names: list[str] = []
    casefolded: dict[str, str] = {}
    try:
        with os.scandir(directory) as entries:
            for entry in entries:
                name = entry.name
                names.append(name)
                if len(names) > MAX_SAMPLING_APPROVAL_RECEIPTS:
                    raise ContractError(
                        f"receipt directory {RECEIPTS_DIR!r} holds more than "
                        f"{MAX_SAMPLING_APPROVAL_RECEIPTS} entries; the sampling approval "
                        "scan is bounded"
                    )
                folded = name.casefold()
                other = casefolded.get(folded)
                if other is not None and other != name:
                    raise ContractError(
                        f"receipt directory {RECEIPTS_DIR!r} contains case-variant names "
                        f"{other!a} and {name!a}; they collide on default APFS"
                    )
                casefolded[folded] = name
    except OSError as error:
        raise ContractError(
            f"receipt directory {RECEIPTS_DIR!r} could not be listed while resolving "
            f"approval for experiment {subject!r}"
        ) from error
    return names


def _decoded_receipt(data: bytes, relative_path: str, *, subject: str) -> dict:
    """One receipt's canonical JSON object, or a refusal naming why it is not one."""
    try:
        decoded = json.loads(data.decode("utf-8"))
    except UnicodeDecodeError as error:
        raise ContractError(
            f"receipt {relative_path!r} is not UTF-8 while resolving approval for "
            f"experiment {subject!r}. The sampling gate cannot prove exactly one "
            "approval while any receipt is undecodable. Restore the exact immutable "
            "receipt bytes or hold this run for review, then rerun the Perlector"
        ) from error
    except (ValueError, RecursionError) as error:
        raise ContractError(
            f"receipt {relative_path!r} is malformed JSON while resolving approval for "
            f"experiment {subject!r}: {error}. The sampling gate cannot prove exactly "
            "one approval while any receipt is malformed. Restore the exact immutable "
            "receipt bytes or hold this run for review, then rerun the Perlector"
        ) from error
    if not isinstance(decoded, dict):
        raise ContractError(
            f"receipt {relative_path!r} is a JSON {type(decoded).__name__}, not an object, "
            f"while resolving approval for experiment {subject!r}. The sampling gate "
            "cannot prove exactly one approval without inspecting every receipt object. "
            "Restore the exact immutable receipt bytes or hold this run for review, then "
            "rerun the Perlector"
        )
    try:
        canonical = canonical_bytes(decoded)
    except (TypeError, ValueError, RecursionError) as error:
        raise ContractError(
            f"receipt {relative_path!r} cannot be represented as canonical receipt "
            "bytes while resolving sampling approval"
        ) from error
    if canonical != data:
        raise ContractError(
            f"receipt {relative_path!r} is not canonical JSON; duplicate or ambiguous "
            "evidence cannot authorize a sampling arm"
        )
    return decoded


def _sampling_receipts(context, *, subject: str) -> list[tuple[ApprovalRecordReference, dict]]:
    """Read every receipt once and return validated records for ``subject``."""
    directory = _open_receipts_directory(context.tree)
    if directory is None:
        return []
    try:
        directory_before = _stable_file_metadata(os.fstat(directory))
        records: list[tuple[ApprovalRecordReference, dict]] = []
        scanned_bytes = 0
        for name in sorted(_receipt_names(directory, subject=subject)):
            digest = _receipt_name_digest(name)
            if digest is None:
                raise ContractError(
                    f"receipt directory {RECEIPTS_DIR!r} contains noncanonical entry {name!a}; "
                    "the sampling gate cannot prove its approval inventory on a "
                    "case-variant or non-content-addressed name"
                )
            relative_path = f"{RECEIPTS_DIR}/{name}"
            data = _read_receipt_bytes(directory, name, relative_path)
            scanned_bytes += len(data)
            if scanned_bytes > MAX_SAMPLING_APPROVAL_SCAN_BYTES:
                raise ContractError(
                    f"receipt scan exceeded the {MAX_SAMPLING_APPROVAL_SCAN_BYTES}-byte "
                    "sampling-approval bound; the gate refuses an amplified evidence directory"
                )
            actual = digest_bytes(data)
            if actual != digest:
                raise ContractError(
                    f"receipt {relative_path!r} has digest {actual}, not its content-addressed "
                    f"name {digest}; the sampling gate cannot skip corrupted evidence"
                )
            decoded = _decoded_receipt(data, relative_path, subject=subject)
            if decoded.get("subject_ids") != [subject]:
                continue
            reference = ApprovalRecordReference(relative_path, digest)
            try:
                record = validate_approval_record(decoded)
            except ApprovalRefusal as error:
                raise ContractError(
                    f"approval record {relative_path!r} for experiment {subject!r} is refused: "
                    f"{error}. The sampling gate cannot accept that record for this run's sealed "
                    f"config_digest {context.config_digest}. Preserve this run for review and "
                    "start a new run tree with one valid approval record before sampling"
                ) from error
            records.append((reference, record))
        directory_after = _stable_file_metadata(os.fstat(directory))
        if directory_after != directory_before:
            raise ContractError(
                f"receipt directory {RECEIPTS_DIR!r} changed while the sampling gate inspected "
                "it; a moving inventory cannot prove exactly one approval"
            )
        return records
    finally:
        os.close(directory)


def resolve_sampling_approval(context, *, approval_ref: str, subject: str) -> ApprovalRecordBinding:
    """Resolve one sealed experiment selector to its checked approval record.

    The selector names the subject, not the record's digest: an approval cannot be
    addressed by its content and also target a config that contains that address. The
    record proves integrity and a claimed approver, not authorship; write access to the
    run tree is the trust boundary.
    """
    if approval_ref != subject:
        raise ContractError(
            f"approval reference {approval_ref!r} does not name experiment {subject!r}; "
            "an arbitrary string is not an approval record"
        )

    candidates = _sampling_receipts(context, subject=subject)

    if not candidates:
        raise ContractError(
            f"no approval record names experiment {subject!r}; a nonzero sampling arm "
            "cannot draw without the project lead's typed approval record. Expected one record "
            f"under {RECEIPTS_DIR}/ in this run tree with subject_ids "
            f"[{subject!r}], action 'other', and target_version_hash "
            f"{context.config_digest}"
        )
    if len(candidates) != 1:
        paths = [candidate.relative_path for candidate, _record in candidates]
        raise ContractError(
            f"{len(candidates)} validated approval records {paths} name experiment {subject!r} for "
            f"this run's sealed config_digest {context.config_digest}. The sampling gate cannot "
            "choose among approval records; it requires exactly one. Preserve this run for review "
            "and start a new run tree with one current approval record before sampling"
        )

    candidate, record = candidates[0]
    # Sampling approvals use action "other", so an exclusion or salvage-promotion record
    # that happens to share the subject text cannot double as one.
    if record["action"] != "other":
        raise ContractError(
            f"approval record {candidate.relative_path!r} for experiment {subject!r} has "
            f"action {record['action']!r}, not 'other', for this run's sealed config_digest "
            f"{context.config_digest}. A sampling design approval is not an exclusion or "
            "salvage-promotion record. Preserve this run for review and start a new run tree with "
            "one approval record for action 'other' before sampling"
        )
    if record["target_version_hash"] != context.config_digest:
        raise ContractError(
            f"approval record {candidate.relative_path!r} for experiment {subject!r} names "
            f"version {record['target_version_hash']}, not this run's sealed config_digest "
            f"{context.config_digest}. The record approves a different sealed configuration. "
            "Preserve this run for review and start a new run tree with one approval record for "
            "the configuration that will be sampled"
        )
    return ApprovalRecordBinding(
        candidate,
        record["subject_ids"][0],
        record["target_version_hash"],
    )


def regions_of(context, act_id: str) -> list[dict]:
    """Every Designator region for this act, each with its provenance verified.

    The Attestatores validate these records before showing them to a witness; the
    reading must refuse the same tamper. Every region carries receipt-backed
    provenance, so a receipt is required.
    """
    records = []
    for entry in stage_manifest(context, DESIGNATOR)["artifacts"]:
        if entry["kind"] == "region" and entry["subject_id"] == act_id:
            record = context.tree.read_artifact(DESIGNATOR, "region", entry["artifact_id"])
            validate_serving_provenance(
                context,
                record.get("payload", {}).get("provenance"),
                producer_stage=DESIGNATOR,
                require_receipt=True,
            )
            records.append(record)
    return sorted(records, key=_region_ordinal)


def _region_ordinal(record: dict) -> int:
    """The sort key, refused by name rather than as a raw `KeyError`.

    Ordering runs before `verify_region`, and untrusted input must not surface as a
    traceback.
    """
    ordinal = record.get("payload", {}).get("attempt_ordinal")
    if not isinstance(ordinal, int) or isinstance(ordinal, bool):
        raise SchemaRefusal("a Designator region carries no integer attempt ordinal to order by")
    return ordinal


def act_regions(context, act_id: str) -> tuple[list[dict], list[dict]]:
    """Every region for this act, and its original-proposal subset.

    Preflight and the reading loop share this so they cannot disagree about which
    regions are proposals; the witness-coverage record depends on that.
    """
    regions = regions_of(context, act_id)
    proposals = [region for region in regions if region["payload"].get("origin") == "proposal"]
    if not proposals:
        raise ContractError(f"act {act_id} reached the Perlector with no original proposal region")
    return regions, proposals


def _region_reference(region: dict) -> dict[str, str]:
    """The exact public region facts a Testimonium may claim it saw."""
    payload = region["payload"]
    return {
        "region_id": payload["region_id"],
        "image_path": payload["image_path"],
        "image_sha256": payload["image_sha256"],
    }


def _validate_presented_page(context, payload: dict, presented: dict) -> None:
    """Bind a witness's presentation and observed geometry to its sealed Exemplar page."""
    page_id = presented.get("source_page_id")
    page = context.tree.read_artifact(EXEMPLAR, "page", artifact_id(EXEMPLAR, "page", page_id))
    page_bytes = context.tree.read_bytes(page["payload"]["image_path"])
    page_size = dimensions(page_bytes)
    validate_native_witness_geometry(payload, page_size=page_size)
    validate_presented_page_binding(
        presented,
        page_ordinal=page["payload"]["ordinal"],
        page_image_path=page["payload"]["image_path"],
        page_sha256=page["payload"]["source_sha256"],
        page_size=page_size,
        page_bytes=page_bytes,
    )


def _sorted_distinct_inputs(references: list[dict[str, str]]) -> list[dict[str, str]]:
    return sorted(_distinct_inputs(references), key=_input_order)


def _input_order(reference: dict[str, str]) -> tuple[str, str]:
    return reference["relative_path"], reference["sha256"]


def validate_testimonium_regions(context, record: dict, proposal_regions: list[dict]) -> None:
    """Validate an act Testimonium's native presentation, regions and inputs."""
    payload = record["payload"]
    presented = payload.get("presented") if isinstance(payload, dict) else None
    if not isinstance(presented, dict):
        raise SchemaRefusal("a Testimonium has no presented native witness block")
    validate_native_witness_geometry(payload)
    unpresented = payload.get("unpresented_regions")
    attempted = record["outcome"] in ATTEMPTED_WITNESS_OUTCOMES
    if not attempted:
        # Before the image-evidence refusal, which a stripped record that kept its
        # retained response would pass.
        if payload.get("raw_response_ref") is not None:
            raise SchemaRefusal(
                "a non-attempted Testimonium retains a provider response. The record would say "
                "the chair was not served while naming the bytes it answered with, outside its "
                "own input set. Record the attempted outcome that produced the response, or "
                "remove the retained reference"
            )
        if (
            payload.get("regions") != []
            or presented != {}
            or payload.get("observed") != []
            or unpresented != []
            or record.get("inputs") != []
        ):
            raise SchemaRefusal(
                "a non-attempted Testimonium carries proposal or image evidence. The record "
                "would say a chair saw pixels when its outcome says it was not served. Remove "
                "the evidence or record the attempted outcome that actually occurred"
            )
        return
    if presented == {}:
        raise SchemaRefusal(
            "an attempted Testimonium has no image presentation. Its reading or failure cannot "
            "be traced to pixels the chair received. Retain the exact presentation before "
            "publishing the attempted record"
        )
    expected_regions = [_region_reference(region) for region in proposal_regions]
    if payload.get("regions") != expected_regions:
        raise SchemaRefusal(
            "an attempted Testimonium does not bind exactly its original proposal regions. "
            "Its act association could omit or acquire evidence silently. Restore the sealed "
            "proposal references without substituting a recovery crop"
        )
    _validate_presented_page(context, payload, presented)
    input_references = [
        context.input_ref(region["payload"]["image_path"]) for region in proposal_regions
    ]
    input_references.append(context.input_ref(presented["image_path"]))
    native_inference = payload.get("native_inference")
    if native_inference is not None:
        provenance = payload.get("provenance")
        identity = provenance.get("resolved_identity") if isinstance(provenance, dict) else None
        capture = payload.get("native_capture")
        if (
            payload.get("chair") != "attestator_1"
            or payload.get("page_witness") is not True
            or not isinstance(identity, dict)
            or identity.get("role") != "attestator_1"
            or identity.get("witness_adapter") != "chandra.v1"
            or identity.get("witness_scope") != "page"
            or (
                capture is not None
                and (not isinstance(capture, dict) or capture.get("adapter") != "chandra.v1")
            )
        ):
            raise SchemaRefusal(
                "Chandra native inference belongs only to the page-scoped "
                "attestator_1 chandra.v1 act view"
            )
        for row in validate_chandra_trace(native_inference)["attempts"]:
            input_references.extend((row["intent_ref"], row["attempt_ref"]))
    expected_inputs = _sorted_distinct_inputs(input_references)
    # Re-derive the explicit limit for every presentation kind so a kind change
    # cannot understate which bound crops its one page-space image omits.
    if unpresented != unpresented_region_ids(presented, proposal_regions):
        raise SchemaRefusal(
            "a Testimonium does not name exactly the bound proposal regions its presentation "
            "does not speak for"
        )

    # Called last on both paths: a forged region presentation also has the wrong inputs,
    # and the operator must read the specific fault, not "wrong blobs".
    def _require_bound_inputs() -> None:
        if record.get("inputs") != expected_inputs:
            raise SchemaRefusal(
                "an attempted Testimonium does not bind exactly its proposal and presentation "
                "blobs. The consumer cannot prove which immutable pixels produced the report. "
                "Restore the complete digest-bound input set and remove unrelated inputs"
            )

    if presented["kind"] != "region":
        _require_bound_inputs()
        return
    matches = [
        region
        for region in regions_of(context, record["subject_id"])
        if region.get("payload", {}).get("region_id") == presented["region_ref"]["region_id"]
    ]
    if len(matches) != 1:
        raise SchemaRefusal("a Testimonium region_ref names no unique sealed region")
    region = matches[0]
    if region["payload"].get("origin") != "proposal":
        raise SchemaRefusal(
            "a recovery region cannot be presented as a witness basis; origin is not proposal"
        )
    if (
        _region_reference(region)
        != {
            "region_id": presented["region_ref"]["region_id"],
            "image_path": presented["image_path"],
            "image_sha256": presented["image_sha256"],
        }
        or region["payload"].get("transform") != presented["transform"]
    ):
        raise SchemaRefusal("a Testimonium region presentation disagrees with its sealed proposal")
    _require_bound_inputs()


def validate_page_testimonium_record(
    context,
    record: dict[str, Any],
    proposal_regions: list[dict[str, Any]],
) -> None:
    """Reconcile a page Testimonium's outcome, page, presentation, and inputs."""
    payload = record.get("payload")
    validate_page_testimonium_payload(
        payload,
        testimonium_id=record.get("artifact_id"),
        read_bytes=context.tree.read_bytes,
    )
    attempted = record["outcome"] in ATTEMPTED_WITNESS_OUTCOMES
    presented = payload["presented"]
    if payload["regions"] != []:
        raise SchemaRefusal(
            "a page Testimonium carries act-region references. Its page evidence would acquire "
            "an act identity the page record does not own. Keep act associations in the "
            "digest-bound attachments"
        )
    if not attempted:
        # As in the act-scoped check: before the image-evidence refusal, which a
        # stripped record that kept its response would pass.
        if payload.get("native_capture") is not None or payload.get("raw_response_refs"):
            raise SchemaRefusal(
                "a non-attempted page Testimonium retains a provider response. The record would "
                "say the chair was not served while naming the bytes it answered with, outside "
                "its own input set. Record the attempted outcome that produced the response, or "
                "remove the retained capture"
            )
        if presented != {} or payload["observed"] != [] or record.get("inputs") != []:
            raise SchemaRefusal(
                "a non-attempted page Testimonium carries image evidence. The record would say "
                "a chair saw pixels when its outcome says it was not served. Remove the image "
                "evidence or record the attempted outcome that actually occurred"
            )
    else:
        if presented == {}:
            raise SchemaRefusal(
                "an attempted page Testimonium has no image presentation. Its outcome cannot be "
                "traced to pixels the chair received. Retain the exact presentation before "
                "publishing the attempted record"
            )
        if (
            presented["source_page_id"] != record["subject_id"]
            or presented["source_page_ordinal"] != payload["page_ordinal"]
        ):
            raise SchemaRefusal(
                "wrong page Testimonium: its presentation names a different page than its "
                "record. Its observations would be attributed to the wrong sealed ink. Restore "
                "the page identity and ordinal of the presentation actually served"
            )
        _validate_presented_page(context, payload, presented)
        expected_inputs = [
            {"relative_path": presented["image_path"], "sha256": presented["image_sha256"]}
        ]
        # Each retained response is bound beside the presented pixels, so an ordinary
        # artifact read re-hashes it instead of trusting a nested reference.
        retained = list(payload.get("raw_response_refs", []))
        capture = payload.get("native_capture")
        if capture is not None:
            retained.append(capture["raw_response_ref"])
        native_inference = payload.get("native_inference")
        if native_inference is not None:
            for row in validate_chandra_trace(native_inference)["attempts"]:
                retained.extend((row["intent_ref"], row["attempt_ref"]))
        # De-duplicated as the producer does: one response can reach the same blob
        # through both `raw_response_refs` and `native_capture`, and
        # `validate_input_refs` refuses a repeated path, so a doubled expectation could
        # never be met.
        expected_inputs = _sorted_distinct_inputs(expected_inputs + retained)
        if record.get("inputs") != expected_inputs:
            raise SchemaRefusal(
                "a page Testimonium does not bind exactly its presented image"
                + (" and every retained raw response" if retained else "")
                + ". The consumer cannot prove which immutable pixels produced the page "
                "report. Restore the digest-bound inputs and remove unrelated ones"
            )
    page_proposals = [
        region
        for region in proposal_regions
        if region["payload"]["transform"]["source_page_id"] == record["subject_id"]
    ]
    if payload["unpresented_regions"] != unpresented_region_ids(presented, page_proposals):
        raise SchemaRefusal(
            "a page Testimonium does not name exactly the proposal regions outside its "
            "presentation. Its derived layer would look more complete than the pixels shown. "
            "Re-derive unpresented_regions from the sealed page proposals"
        )
    validate_serving_provenance(
        context,
        payload["provenance"],
        producer_stage=ATTESTATORES,
        require_receipt=attempted,
    )


def sealed_proposal_regions(context) -> list[dict]:
    """Every verified proposal in the run-wide routing denominator."""
    regions = []
    for entry in stage_manifest(context, DESIGNATOR)["artifacts"]:
        if entry["kind"] != "region":
            continue
        record = context.tree.read_artifact(DESIGNATOR, "region", entry["artifact_id"])
        # Keep the origin-specific refusal ahead of the general lineage refusal;
        # callers rely on the shared recovery-denominator vocabulary.
        recovery_region_count(record.get("subject_id", "unidentified act"), [record])
        validate_serving_provenance(
            context,
            record.get("payload", {}).get("provenance"),
            producer_stage=DESIGNATOR,
            require_receipt=True,
        )
        verify_region(context, record)
        if record["payload"]["origin"] == "proposal":
            regions.append(record)
    return regions


def testimonia_of(context, act_id: str, proposal_regions: list[dict]) -> list[dict]:
    """Every chair's current testimonium for this act: the latest attempt only.

    Attempts are append-only (principle 4). Every record is validated, but only each
    chair's latest attempt is evidence, as in the Recensor's `chair_current_attempts`, so
    dissent, witness coverage and the recorded basis never see a superseded attempt.
    """
    records = []
    for entry in stage_manifest(context, ATTESTATORES)["artifacts"]:
        if entry["kind"] == "testimonium" and entry["subject_id"] == act_id:
            record = context.tree.read_artifact(ATTESTATORES, "testimonium", entry["artifact_id"])
            validate_serving_provenance(
                context,
                record.get("payload", {}).get("provenance"),
                producer_stage=ATTESTATORES,
                require_receipt=record["outcome"] in ATTEMPTED_WITNESS_OUTCOMES,
            )
            validate_testimonium_regions(context, record, proposal_regions)
            records.append(record)
    current = latest_per_chair(records, f"testimonium for {act_id}")
    chairs = {record["payload"]["chair"] for record in current}
    configured = set(context.witness_chairs)
    missing = configured - chairs
    if missing:
        raise FatalAccounting(
            f"act {act_id} has no current Testimonium for configured chair(s) {sorted(missing)}; "
            "the Perlector may not seal a reading over a shortened witness denominator"
        )
    unsealed = chairs - configured
    if unsealed:
        raise FatalAccounting(
            f"act {act_id} carries Testimonium from chair(s) {sorted(unsealed)}, which this "
            "run was not sealed with"
        )
    return current


def declared_page_witness_chairs(context) -> set[str]:
    """Read page scope from the sealed model configuration, not from upstream records.

    A consumer may not inherit trust across a stage boundary. The uniqueness and roster
    checks stop a duplicate or a nonexistent chair from silently erasing page coverage.
    """
    roster = context.witness_chairs
    # Exact `str`, not `isinstance`: set construction and refusal formatting would run
    # subclass code.
    if (
        not isinstance(roster, list)
        or any(type(chair) is not str for chair in roster)
        or len(roster) != len(set(roster))
    ):
        raise SchemaRefusal(
            "the sealed witness roster is not a unique list of chair names. Page-witness scope "
            "cannot be derived from this run authority. Start a new run from the sealed models "
            "configuration; do not edit the existing run"
        )
    configured = context.registry.config.chairs
    unknown = set(roster) - set(configured)
    if unknown:
        raise SchemaRefusal(
            "the sealed witness roster names chair(s) absent from the current models "
            "configuration: "
            f"{sorted(unknown)} not in {sorted(configured)}. The run authority and current models "
            "configuration do not describe the same witness set. Reopen the run with its original "
            "models configuration or start a new run; do not edit sealed evidence"
        )
    return {
        chair
        for chair in roster
        if isinstance(configured[chair], ChairIdentity)
        and configured[chair].witness_scope == "page"
    }


ATTACHMENT_FIELDS: Final = frozenset(
    {
        "chair",
        "page_witness",
        "page_ordinal",
        "testimonium_ref",
        "attached",
        "comparable",
        "attachment_basis",
        "content_health",
        "alignment",
        "span",
    }
)


def _validate_attachment_shape(attachment: Any) -> None:
    """The one closed-shape rule for an attachment, applied wherever it is read.

    The chair must be an exact `str`: it becomes a set and dict key, where a subclass
    could run its own code.
    """
    if (
        not isinstance(attachment, dict)
        or set(attachment) != ATTACHMENT_FIELDS
        or type(attachment.get("chair")) is not str
        or not isinstance(attachment.get("page_witness"), bool)
        or not isinstance(attachment.get("attached"), bool)
        or not isinstance(attachment.get("comparable"), bool)
        or attachment.get("attachment_basis") not in ATTACHMENT_BASES
        or not isinstance(attachment.get("content_health"), dict)
    ):
        raise SchemaRefusal("an act-attachment record has a malformed attachment")


def act_attachment_view(
    context,
    act: dict[str, Any],
    testimonia: list[dict],
    bases: list[dict],
    proposal_region_ids: set[str],
    page_testimonia_seen: dict[str, dict] | None = None,
    all_proposal_regions: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Validate the attachment that makes a page witness act-addressable.

    Until real alignment exists, the attachment carries the chair's complete delivered
    act reading as an interim span, surfaced in the dossier so page completion is never
    taken for an act-level read. It is reconciled against `testimonia`, the current
    attempt per chair. `bases` are the verified regions the Perlector read, the
    independent page denominator.
    """
    act_id = act["act_id"]
    current = {record["payload"]["chair"]: record for record in testimonia}
    entries = [
        entry
        for entry in stage_manifest(context, ATTESTATORES)["artifacts"]
        if entry["kind"] == "act-attachment" and entry["subject_id"] == act_id
    ]
    if not entries:
        raise FatalAccounting(f"act {act_id} has no act-attachment record")
    records = [
        context.tree.read_artifact(ATTESTATORES, "act-attachment", entry["artifact_id"])
        for entry in entries
    ]
    record = latest_attempt(records, f"act-attachment for {act_id}", operation="act-attachment")
    payload = record.get("payload")
    attachments = payload.get("attachments") if isinstance(payload, dict) else None
    if (
        not isinstance(payload, dict)
        or set(payload) != {"act_key", "attempt_ordinal", "attachments"}
        or payload.get("act_key") != act["act_key"]
        or not isinstance(attachments, list)
    ):
        raise SchemaRefusal("an act-attachment record has no attachment list")
    configured = set(context.witness_chairs)
    page_ids = {basis["source_page_ordinal"]: basis["source_page_id"] for basis in bases}
    # The run-global scope is validated first, so its malformation is never
    # misattributed to one chair's attachment.
    page_chairs = declared_page_witness_chairs(context)
    # Validate every value that becomes a set or dict key first: JSON `true == 1` in
    # Python, and an unhashable value would escape as a raw TypeError.
    for attachment in attachments:
        _validate_attachment_shape(attachment)
        page_ordinal = attachment["page_ordinal"]
        if attachment["page_witness"] and (
            not isinstance(page_ordinal, int) or isinstance(page_ordinal, bool)
        ):
            raise SchemaRefusal("a page-witness attachment has no integer page ordinal")
        if not attachment["page_witness"] and page_ordinal is not None:
            raise SchemaRefusal("an act-scoped witness carries a page ordinal")
    attachment_chairs = [attachment["chair"] for attachment in attachments]
    if all_proposal_regions is None:
        all_proposal_regions = sealed_proposal_regions(context)
    if any(chair not in configured for chair in attachment_chairs):
        raise FatalAccounting(
            f"act {act_id} attachment chairs do not equal this run's configured witnesses"
        )
    expected_pairs = {
        (chair, ordinal if chair in page_chairs else None)
        for chair in configured
        for ordinal in (page_ids if chair in page_chairs else (None,))
    }
    pairs = [(attachment["chair"], attachment["page_ordinal"]) for attachment in attachments]
    if len(pairs) != len(set(pairs)) or set(pairs) != expected_pairs:
        raise FatalAccounting(
            f"act {act_id} attachments do not cover every contributing page/witness pair; "
            "its witness denominator is duplicated or incomplete; rebuild the attachment "
            "from the sealed regions and configured chairs"
        )
    # Accounting is per (chair, page), but the dossier counts witnesses: a chair counts
    # once however many pages it spans.
    page_witness_chairs: set[str] = set()
    comparison_views: dict[str, str] = {}
    edge_deltas: dict[str, list[dict[str, Any]]] = {}
    for attachment in attachments:
        chair_testimonium = _current_testimonium_for(
            attachment, act_id=act_id, current=current, page_chairs=page_chairs
        )
        chair = attachment["chair"]
        reference = attachment.get("testimonium_ref")
        if attachment["page_witness"]:
            view, deltas = _checked_page_attachment(
                context,
                act,
                attachment,
                chair_testimonium,
                reference=reference,
                page_ids=page_ids,
                bases=bases,
                proposal_region_ids=proposal_region_ids,
                all_proposal_regions=all_proposal_regions,
                page_testimonia_seen=page_testimonia_seen,
            )
            if view is not None:
                comparison_views[chair] = view
            page_witness_chairs.add(chair)
        else:
            deltas = _checked_act_scoped_attachment(
                context,
                act_id,
                attachment,
                reference=reference,
                bases=bases,
                proposal_region_ids=proposal_region_ids,
            )
        edge_deltas.setdefault(chair, []).extend(deltas)
    return {
        "reference": context.artifact_ref(ATTESTATORES, "act-attachment", record["artifact_id"]),
        # A blinded dossier may show that page evidence exists, but not the
        # chair names embedded in its retained attachment artifact.
        "page_witness_count": len(page_witness_chairs),
        # Keyed per chair (relabelled in `dossier.build_dossier`) and present only for
        # page witnesses, so a blinded reader learns which pseudonyms are page-scoped:
        # scope, never identity. Dissent needs a view attributable to a label; this is
        # its cost.
        "comparison_views": comparison_views,
        "edge_deltas": ordered_edge_deltas(edge_deltas),
    }


def _current_testimonium_for(
    attachment: dict[str, Any], *, act_id: str, current: dict[str, dict], page_chairs: set[str]
) -> dict:
    """The chair's current Testimonium, once the attachment's own facts agree with it."""
    span = attachment["span"]
    characters = attachment["content_health"].get("characters")
    if attachment["attached"] and not attachment["page_witness"]:
        if attachment["attachment_basis"] != "presented-region":
            raise SchemaRefusal("an act-scoped attachment has no presented-region basis")
        expected_end = (
            characters if isinstance(characters, int) and not isinstance(characters, bool) else 0
        )
        if span != {"start": 0, "end": expected_end}:
            raise SchemaRefusal("an attached act view does not span its complete delivered reading")
    if attachment["comparable"] and not attachment["attached"]:
        raise SchemaRefusal(
            "an unattached act view cannot claim comparable text. "
            "Text cannot count for an act when witness geometry did not attach to it. "
            "Rebuild both facts from the retained Testimonium."
        )
    # Independent of the rule above. Page witnesses reach this too, so each refusal
    # names its own field rather than calling every row an act view.
    if not attachment["attached"]:
        if attachment["attachment_basis"] != "unattached":
            raise SchemaRefusal(
                "an unattached attachment names an attachment basis other than "
                "'unattached'; nothing attached it, so nothing decided the basis"
            )
        if span is not None:
            raise SchemaRefusal("an unattached attachment claims an alignment span")
    chair = attachment["chair"]
    # A reread appends a new attempt without rewriting the attachment, so a stale
    # attachment could present a superseded outcome as live. For an act-scoped chair
    # `attached` must equal the current outcome.
    chair_testimonium = current.get(chair)
    if chair_testimonium is None:
        raise FatalAccounting(
            f"act {act_id} attachment names chair {chair!r}, which has no current Testimonium"
        )
    if not attachment["page_witness"] and attachment["attached"] != (
        chair_testimonium["outcome"] in WITNESS_READING_OUTCOMES
    ):
        raise SchemaRefusal(
            f"act {act_id} attachment for chair {chair!r} disagrees with that chair's "
            "current Testimonium outcome"
        )
    # Not exempted for page witnesses: a reread appends to the same per-(act, chair)
    # stream either way. `attached` may differ from a page witness's outcome, but
    # the health must be the current attempt's.
    if attachment["content_health"] != chair_testimonium["payload"].get("content_health"):
        raise SchemaRefusal(
            f"act {act_id} attachment for chair {chair!r} describes an attempt that is no "
            "longer this chair's current Testimonium"
        )
    expected_page_witness = chair in page_chairs
    if attachment["page_witness"] != expected_page_witness:
        raise SchemaRefusal(
            f"act {act_id} attachment changes page-witness scope for chair {chair!r}"
        )
    # `dissent.py` trusts the Testimonium's own `page_witness` flag and skips the
    # comparison for it, so a resealed flag could silence an act-scoped chair's
    # dissent row. Reconcile this copy against the run's declaration too.
    if chair_testimonium["payload"].get("page_witness", False) is not expected_page_witness:
        raise SchemaRefusal(
            f"act {act_id} Testimonium for chair {chair!r} claims a page-witness scope this "
            "run did not declare"
        )
    return chair_testimonium


def _checked_page_attachment(
    context,
    act: dict[str, Any],
    attachment: dict[str, Any],
    chair_testimonium: dict,
    *,
    reference: Any,
    page_ids: dict[int, str],
    bases: list[dict],
    proposal_region_ids: set[str],
    all_proposal_regions: list[dict[str, Any]],
    page_testimonia_seen: dict[str, dict] | None,
) -> tuple[str | None, list[dict[str, Any]]]:
    """Reconcile a page witness's attachment with its page Testimonium.

    Returns the act's comparison view of the page text, if aligned, and the edge deltas.
    """
    act_id = act["act_id"]
    chair = attachment["chair"]
    attachment_page = attachment["page_ordinal"]
    span = attachment["span"]
    view = None
    if attachment_page not in page_ids:
        raise SchemaRefusal(
            f"act {act_id} page attachment names page {attachment_page!r} outside its "
            "regions; it claims evidence the Perlector did not read; restore the "
            "attachment's contributing page"
        )
    testimonium = context.tree.read_artifact_reference(
        reference,
        stage=ATTESTATORES,
        kind="page-testimonium",
        subject_id=page_ids[attachment_page],
    )
    page_payload = testimonium.get("payload")
    validate_page_testimonium_record(context, testimonium, all_proposal_regions)
    # For the caller's run-wide routing sweep: a page Testimonium belongs to a (page,
    # chair) pair, not to this act, and this is the only digest-checked read of it.
    if page_testimonia_seen is not None and isinstance(page_payload, dict):
        page_testimonia_seen[testimonium["artifact_id"]] = testimonium
    native_capture = page_payload.get("native_capture")
    if native_capture is not None:
        if native_capture["raw_response_ref"] not in testimonium.get("inputs", []):
            raise SchemaRefusal(
                f"act {act_id} page Testimonium for chair {chair!r} does not bind its "
                "retained raw response as a verified input"
            )
        if native_capture["adapter"] != context.registry.resolve(chair).witness_adapter:
            raise SchemaRefusal(
                f"act {act_id} page Testimonium for chair {chair!r} attributes its "
                "native capture to an adapter other than that chair's configured boundary"
            )
        verify_native_capture_blob(context.tree, native_capture)
    # Sealed proposal geometry only, as the writer used: a recovery crop postdates
    # testimony, so it may not enlarge the denominator that attached it.
    page_bases = [
        basis
        for basis in bases
        if basis["source_page_ordinal"] == attachment_page
        and basis["region_id"] in proposal_region_ids
    ]
    # Native page and compatibility act outcomes are independent; legacy
    # page joins instead derive their outcome from the act attempts.
    attachment_outcome = (
        testimonium["outcome"] if native_capture is not None else chair_testimonium["outcome"]
    )
    # The producer's shared rule, recomputed so a resealed record cannot claim either
    # `attached` or its basis: a page witness attaches on its own ink over this act's
    # sealed proposal or, only where it reported none, on an anchor line in its text.
    derived_basis = page_attachment_basis(
        reading=attachment_outcome in WITNESS_READING_OUTCOMES,
        geometry_overlaps=any(
            reported_geometry_overlaps(
                page_payload.get("observed", []), basis["transform"]["bounds"]
            )
            for basis in page_bases
        ),
        alignment=attachment["alignment"],
    )
    if attachment["attached"] != (derived_basis != "unattached"):
        raise SchemaRefusal(
            f"act {act_id} page attachment for chair {chair!r} does not derive from "
            "that witness's reported geometry, or from an anchor line located in its "
            "page text, against the sealed proposal"
        )
    deltas = sealed_proposal_edge_deltas(page_payload, page_bases)
    unjoined = page_payload.get("unjoined_act_attempts")
    if (
        page_payload.get("chair") != chair
        or page_payload.get("scope") != "page"
        or page_payload.get("page_ordinal") != attachment_page
        or not isinstance(unjoined, list)
        or any(
            not isinstance(row, dict)
            or set(row) != {"act_id", "act_key", "outcome", "reason"}
            or not isinstance(row["act_id"], str)
            or not isinstance(row["act_key"], str)
            or not isinstance(row["outcome"], str)
            or not isinstance(row["reason"], str)
            or not row["reason"].strip()
            for row in unjoined
        )
    ):
        raise SchemaRefusal(f"act {act_id} attachment points to the wrong page Testimonium")
    # One act can disprove `primary` or `continuation` from its sealed
    # primary page. Only the Recensor's whole-page view can verify `mixed`.
    role = page_payload.get("page_role")
    is_act_primary_page = attachment_page == act["page_ordinal"]
    if (
        not isinstance(role, str)
        or role not in {"primary", "continuation", "mixed"}
        or (
            (is_act_primary_page and role == "continuation")
            or (not is_act_primary_page and role == "primary")
        )
    ):
        raise SchemaRefusal(
            f"act {act_id} page Testimonium for chair {chair!r} carries a page_role "
            f"{role!r} its own primary-page fact contradicts; the page relationship "
            "is false; rebuild the page Testimonium from the attachment denominator"
        )
    # Only the alignment is fixed on a continuation page, not `attached`: a page witness
    # can honestly report geometry there, but the act's anchor is on its primary page.
    if not is_act_primary_page and attachment["alignment"] != {
        "status": "unaligned",
        "reason": "continuation-page-no-act-anchor",
    }:
        raise SchemaRefusal(
            f"act {act_id} continuation-page attachment for chair {chair!r} claims "
            "an act anchor; this page carries no act-specific anchor; "
            "retain it as continuation-page-no-act-anchor"
        )
    current_unjoined = [row for row in unjoined if row["act_id"] == act_id]
    if len(current_unjoined) > 1:
        raise SchemaRefusal(
            f"act {act_id} appears more than once in a page Testimonium's unjoined-attempt record"
        )
    # No row means the act joined. An omitted act is disclosed with the outcome that
    # explains it; a reading may still be omitted (a structured native object cannot join).
    row = current_unjoined[0] if current_unjoined else None
    disclosed = row["outcome"] in WITNESS_READING_OUTCOMES if row is not None else True
    # Joining only proves the bytes arrived: a joined response may still be unaligned,
    # but an omitted one can never attach.
    if not disclosed and attachment["attached"]:
        raise SchemaRefusal(
            f"act {act_id} attachment disagrees with its page Testimonium's unjoined-attempt record"
        )
    alignment = attachment["alignment"]
    # The exact label is evidence about independence: `anchor-line` says the chair
    # counts only because another chair's anchor located its text.
    if attachment["attached"] and attachment["attachment_basis"] != derived_basis:
        raise SchemaRefusal(
            f"act {act_id} page attachment for chair {chair!r} names basis "
            f"{attachment['attachment_basis']!r}, but its own retained evidence "
            f"attached it by {derived_basis!r}"
        )
    if (
        attachment["attached"]
        and isinstance(alignment, dict)
        and alignment.get("status") == "aligned"
    ):
        if (
            set(alignment)
            != {
                "status",
                "anchor_basis",
                "anchor_chair",
                "anchor_span",
                "witness_span",
                "anchor_line_match",
                "line_geometry",
                "loss",
                "offset_maps",
                "deadline_in_force",
            }
            or (
                alignment.get("anchor_basis") == "act-anchor"
                and not isinstance(alignment.get("anchor_chair"), str)
            )
            or (
                alignment.get("anchor_basis") != "act-anchor"
                and alignment.get("anchor_chair") is not None
            )
            or span != alignment.get("witness_span")
            # Whether the SIGALRM backstop was armed, not only whether it finished.
            or not isinstance(alignment.get("deadline_in_force"), bool)
        ):
            raise SchemaRefusal("an attached page witness has no computed alignment")
        page_text = page_payload.get("payload")
        witness_span = alignment["witness_span"]
        if not isinstance(page_text, str):
            raise SchemaRefusal("an attached page witness has no textual comparison view")
        # `witness_span` indexes the raw page reading. The slice is stripped of
        # markup because dissent assumes a markup-free comparison view.
        view = act_comparison_view(page_text, witness_span)
    elif attachment["attached"] and (
        not isinstance(alignment, dict)
        or set(alignment) != {"status", "reason"}
        or alignment.get("status") != "unaligned"
        or span is not None
        or not (isinstance(alignment["reason"], str) and alignment["reason"].strip())
    ):
        raise SchemaRefusal("a geometrically attached page witness has no explicit span limit")
    elif (
        not attachment["attached"]
        and isinstance(alignment, dict)
        and alignment.get("status") == "aligned"
    ):
        # Text alignment cannot authorize a geometric attachment or comparison view.
        if span is not None:
            raise SchemaRefusal("an unattached page witness claims a comparison span")
    elif not attachment["attached"] and (
        not isinstance(alignment, dict)
        or set(alignment) != {"status", "reason"}
        or alignment.get("status") != "unaligned"
        or not (isinstance(alignment["reason"], str) and alignment["reason"].strip())
    ):
        # Without a reason the operator cannot tell why comparison failed.
        raise SchemaRefusal("an unattached page witness has no explicit unaligned result")
    # Geometry alone cannot satisfy the witness floor: the act also needs an aligned
    # slice of retained page text, re-derived so it cannot be claimed by assertion.
    if attachment["comparable"] != (
        attachment["attached"]
        and isinstance(alignment, dict)
        and alignment.get("status") == "aligned"
    ):
        raise SchemaRefusal(
            f"act {act_id} page attachment for chair {chair!r} claims a comparability "
            "its own recorded alignment does not support. The witness floor could count "
            "text that was never placed in this act. Rebuild comparability from the "
            "referenced page Testimonium and alignment."
        )
    return view, deltas


def _checked_act_scoped_attachment(
    context,
    act_id: str,
    attachment: dict[str, Any],
    *,
    reference: Any,
    bases: list[dict],
    proposal_region_ids: set[str],
) -> list[dict[str, Any]]:
    """Reconcile an act-scoped witness's attachment with its Testimonium; return its edge deltas."""
    chair = attachment["chair"]
    if attachment["page_ordinal"] is not None:
        raise SchemaRefusal("an act-scoped witness carries a page ordinal")
    if attachment["alignment"] is not None:
        raise SchemaRefusal("an act-scoped witness carries page alignment evidence")
    testimonium = context.tree.read_artifact_reference(
        reference,
        stage=ATTESTATORES,
        kind="testimonium",
        subject_id=act_id,
    )
    if testimonium.get("payload", {}).get("chair") != chair:
        raise SchemaRefusal(f"act {act_id} attachment points to another chair's Testimonium")
    # Structured reports stay retained but uncountable.
    if attachment["comparable"] != (
        attachment["attached"] and isinstance(testimonium.get("payload", {}).get("payload"), str)
    ):
        raise SchemaRefusal(
            f"act {act_id} attachment for chair {chair!r} claims a comparability its "
            "own retained derived testimony does not support. The witness floor could "
            "count a structured or absent report as act text. Rebuild comparability "
            "from the current referenced Testimonium."
        )
    return sealed_proposal_edge_deltas(
        testimonium["payload"],
        [basis for basis in bases if basis["region_id"] in proposal_region_ids],
    )


def ordered_edge_deltas(
    rows_by_chair: dict[str, list[dict[str, Any]]],
) -> dict[str, list[dict[str, Any]]]:
    """Keep each chair's multi-page delta evidence in its declared stable order."""
    return {
        chair: sorted(rows, key=lambda row: (row["ordinal"], row["region_id"]))
        for chair, rows in rows_by_chair.items()
    }


def sealed_proposal_edge_deltas(
    payload: dict[str, Any], bases: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """Per-chair offsets from observed ink to this act's sealed proposals.

    This is correspondence evidence, not a vote: each native/derived observation
    can retain every positive-area overlap.  No chair is compared with another
    chair and no magnitude is interpreted here.
    """
    rows: list[dict[str, Any]] = []
    for observation in payload.get("observed", []):
        if observation.get("bounds_source") not in {"native", "derived"}:
            continue
        observed = observation["bounds"]
        for basis in bases:
            bounds = basis["transform"]["bounds"]
            if not reported_geometry_overlaps([observation], bounds):
                continue
            rows.append(
                {
                    "ordinal": observation["ordinal"],
                    "region_id": basis["region_id"],
                    "offsets": {
                        "left": observed["x"] - bounds["x"],
                        "top": observed["y"] - bounds["y"],
                        "right": observed["x"] + observed["w"] - bounds["x"] - bounds["w"],
                        "bottom": observed["y"] + observed["h"] - bounds["y"] - bounds["h"],
                    },
                }
            )
    return sorted(rows, key=lambda row: (row["ordinal"], row["region_id"]))


def act_comparison_view(page_text: str, witness_span: dict[str, int]) -> str:
    """One act's markup-stripped slice of a page reading, from a raw span.

    `witness_span` indexes the raw page-Testimonium text, so the slice is cut from the
    raw text and markup is stripped from the slice, keeping the view safe to diff.
    """
    # Both bounds: a malformed offset gives a silently wrong slice (`text[-3:2]` is
    # valid Python), which dissent would read as a departure the witness never made. The
    # Recensor checks the same three conditions.
    if not isinstance(witness_span, dict) or set(witness_span) != {"start", "end"}:
        raise SchemaRefusal("an attached page witness carries no two-bound comparison span")
    start, end = witness_span["start"], witness_span["end"]
    if any(not isinstance(bound, int) or isinstance(bound, bool) for bound in (start, end)):
        raise SchemaRefusal("an attached page witness claims a non-integer comparison span")
    if start < 0 or end < start or end > len(page_text):
        raise SchemaRefusal("an attached page witness claims a span past its own comparison view")
    return markup_text_view(page_text[start:end])["text"]


def dissent_testimonia(testimonia: list[dict], attachment_view: dict[str, Any]) -> list[dict]:
    """Give dissent a safe comparison view without changing retained testimony.

    Dissent gets a copy; the retained Testimonium stays verbatim (principle 4).

    * A page witness gets this act's anchored, markup-stripped slice of its page
      reading from `act_attachment_view`; without one, `dissent_against` reports it
      unaligned.
    * An act-scoped chair whose format declares `can_express_uncertainty` gets its text
      with the bracket markers removed (`bracket_marker_view`). Otherwise
      `dissent.is_comparable` refuses to diff it and that chair's dissent goes dark.

    The capability says a chair can mark uncertainty, not which notation it uses. The
    bracket view fits the only act-scoped chair bound today, but another chair's markers
    would survive and read as disagreement (principle 8).

    Every view derives from the chair's own retained bytes, after the reading is fixed
    (principle 1).
    """
    views = attachment_view["comparison_views"]
    result = []
    for record in testimonia:
        copied = {**record, "payload": dict(record["payload"])}
        payload = copied["payload"]
        chair = payload["chair"]
        if payload.get("page_witness"):
            if chair in views:
                payload["comparison_reported"] = views[chair]
            result.append(copied)
            continue
        capabilities = payload.get("format_capabilities")
        reported = payload.get("payload")
        # `is True`: a non-boolean read back from a retained record must not decide a
        # comparison view.
        if (
            isinstance(capabilities, Mapping)
            and capabilities.get("can_express_uncertainty") is True
            and isinstance(reported, str)
        ):
            payload["comparison_reported"] = bracket_marker_view(reported)["text"]
        result.append(copied)
    return result


def verify_region(context, region: dict) -> dict:
    """Prove the region handed over is the region the reference describes.

    The digest catches changed bytes, decoding catches a non-image, and the dimensions
    catch a crop that does not match its transform. The cause goes into the refusal
    text because `run_stage` prints only the refusal (principle 2); these messages name
    ordinals and run-relative paths, never a submitted filename.
    """
    try:
        return verify_exemplar_crop_lineage(context.tree, context.run, region)
    except ContractError as error:
        raise SchemaRefusal(
            f"a Designator region does not trace to its Exemplar page: {error}"
        ) from error


# Re-exported from dossier.py, which derives witness coverage from the testimonia it
# carries.
witnessed_region_ids = dossier_module.witnessed_region_ids


def real_ingress(context) -> bool:
    """Whether this run authority names the real route, by the shared reader."""
    return is_real_ingress(context.run)


def declared_reading_failure(context, act_key: str) -> str | None:
    """The non-completed outcome this scenario declares for an act, if any.

    A real submission declares nothing: its non-completion is the engine's own stop
    reason, carried through `truncation.classify`.
    """
    if real_ingress(context):
        return None
    for row in context.fixture.get("reading_failure", []):
        if row["scenario"] == context.scenario and row["act_key"] == act_key:
            return row["outcome"]
    return None


def fixture_reader_for(context, chair: ChairIdentity | AbsentChair, serving_mode: str):
    """The reader a non-live pass reads through, or `None` when there is nothing to read with.

    Live mode returns `None`: the loop starts the chair on first use, so a resumed pass
    with every act sealed never loads a model. On a real submission a non-live row
    refuses, because a declared text cannot stand in for real ink; an absent chair reads
    nothing and needs no reader.
    """
    if serving_mode == "live":
        return None
    if not real_ingress(context):
        return FixtureReader(context.fixture, context.scenario)
    if isinstance(chair, AbsentChair):
        return None
    raise ContractError(
        f"the Perlector cannot read a real submission through the fixture reader: the sealed "
        f"serving-recipe row for chair {chair.role!r} is not a live row, and a declared text "
        "cannot stand in for a reading of real ink. Start a new run sealed under a catalogue "
        "whose Perlector row is live; a sealed run's catalogue cannot be changed"
    )


def perlector_chair(context) -> ChairIdentity | AbsentChair:
    """The Perlector chair, resolved by name. Never another chair, never a base."""
    resolved = context.registry.resolve(PERLECTOR_CHAIR)
    if not isinstance(resolved, (ChairIdentity, AbsentChair)):
        raise ContractError("Perlector resolution returned neither an identity nor an absence")
    return resolved


def preflight_testimonia_denominator(context, acts: list[dict]) -> None:
    """Validate the run declaration and every requested witness denominator before writes.

    A Perlectio is immutable, so one published over a short denominator cannot be
    corrected. The page-witness declaration is checked even when every act is held,
    because held acts still publish `not-run` Perlectiones.
    """
    declared_page_witness_chairs(context)
    for act in acts:
        if act["outcome"] == "held":
            continue
        _, proposal_regions = act_regions(context, act["act_id"])
        testimonia_of(context, act["act_id"], proposal_regions)


def provenance_for(
    context,
    resolved: ChairIdentity | AbsentChair,
    *,
    attempted: bool,
    receipt_ref: dict[str, str] | None = None,
) -> dict:
    """Project one Perlector outcome's immutable provenance.

    An outcome that attempted no reading (a held act, an absent chair) names what would
    have read and carries no receipt. An attempted reading re-verifies the snapshot when
    it is made (principle 6). `receipt_ref` is the live chair's own receipt, passed only
    in live mode: a fixture receipt beside a real engine's reading would put a declared
    value where a measurement belongs (principle 8).
    """
    if receipt_ref is not None and not attempted:
        raise SchemaRefusal(
            "a Perlector outcome that attempted no reading cannot carry a serving receipt; "
            "a held act and an absent chair name what would have read and stop there"
        )
    if receipt_ref is not None and isinstance(resolved, AbsentChair):
        raise SchemaRefusal(
            "an absent Perlector chair served nothing, so a receipt reference "
            "would name a serving moment this chair never had"
        )
    regime = {
        # A reading's provenance includes what its reader was shown, so every Perlectio
        # records its witness regime.
        "witness_regime": context.witness_context,
        "adapter_revision": context.adapter_revision,
    }
    if isinstance(resolved, AbsentChair):
        return {
            "chair": resolved.role,
            "chair_state": "absent",
            "absence": resolved.to_record(),
            "resolved_identity": None,
            "resolved_revision": None,
            "receipt_ref": None,
            **regime,
        }
    return {
        "chair": resolved.role,
        "chair_state": "configured",
        "resolved_identity": resolved.to_record(),
        "resolved_revision": {
            "kind": resolved.receipt_revision_kind,
            "value": resolved.receipt_revision,
        },
        "receipt_ref": (
            (
                dict(receipt_ref)
                if receipt_ref is not None
                else context.write_serving_receipt(resolved, fixture_serving_details(resolved))
            )
            if attempted
            else None
        ),
        **regime,
    }


def bound_serving_recipes(context, recipes_path: str):
    """The serving catalogue, proved to be the exact bytes this run sealed.

    Only the catalogue is digested: the placement table is read only by pod preflight
    to choose a tier, which arrives here measured on `--placement-tier`.
    """
    inputs = context.serving_config_inputs
    if inputs is None:
        raise ContractError(
            "this run authority seals no serving configuration inputs, so the catalogue that "
            "decides whether a chair is live cannot be proven; open the run with "
            "`open_stage_context`"
        )
    expected = ServingConfigInputs.from_record(inputs)
    recipes = load_serving_recipes(recipes_path)
    if recipes.source_sha256 != expected.serving_recipes_sha256:
        raise ContractError(
            f"the serving recipe catalogue at {recipes_path} is not the catalogue this run "
            "sealed; the row kind that decides live from fixture would be read out of bytes "
            "no run authority bound"
        )
    return recipes


def perlector_serving_mode(context, args, chair: ChairIdentity | AbsentChair) -> str:
    """`"fixture"` or `"live"`, from the sealed serving-recipe row kind alone.

    The catalogue is already sealed into `config_digest`; `--placement-tier` is a
    measured fact of the card and deliberately unsealed. Resolved before anything is
    published or started, so a live row without a tier refuses on an untouched tree. An
    absent chair is `fixture`: it reads nothing and has no identity to look a row up by.
    """
    if isinstance(chair, AbsentChair):
        return "fixture"
    return serving_mode_for(
        bound_serving_recipes(context, args.serving_recipes_config), chair, args.placement_tier
    )


class ResidentChair:
    """The one live chair a Perlector pass holds, and the promise it is stopped.

    `main` closes it in a `finally`, and the pass closes it before sealing, so a failed
    shutdown is never reported over a sealed stage; `close` is idempotent for that
    reason. A `ServiceStopError` propagates: an unverified shutdown must be reported.
    """

    __slots__ = ("client",)

    def __init__(self) -> None:
        self.client: ChairClient | None = None

    def close(self) -> None:
        client, self.client = self.client, None
        if client is not None:
            client.__exit__()


def default_serving_factory(recipes, *, decoding_config_sha256: str, record_temperature: int):
    """Build the production `serving_factory(context, chair, tier) -> ChairClient`.

    Tests inject a fake through the same signature. It is reached only after the sealed
    row has said `live`; it never chooses an engine.
    """

    def factory(context, chair: ChairIdentity, tier: str) -> ChairClient:
        manager = ServingManager(
            registry=context.registry,
            recipes=recipes,
            config_inputs=ServingConfigInputs.from_record(context.serving_config_inputs),
            launcher=SubprocessLauncher(),
            http=UrllibHttpTransport(),
            receipt_publisher=StageContextReceiptPublisher(context),
            _launch_purpose=(
                MECHANICS_QUALIFICATION_PURPOSE
                if getattr(context.args, "mechanics_qualification", False)
                else None
            ),
            # Engine logs sit in the run tree beside the stage's blobs, not among them:
            # the sealed inventory walks `<stage>/blobs` only, so a log still being
            # written cannot falsify it. `fetch-run` brings them home as unverified side
            # evidence.
            log_root=context.tree.resolve(context.tree.serving_log_path(context.stage)),
            # The card belongs to the pod, not the run tree: every serving stage takes
            # this container-local lease, so chairs contend for it across run ids, and
            # no network mount is trusted to honour a lock.
            residency_lease=FileResidencyLease(POD_RESIDENCY_LOCK_PATH),
            producer="pipeline/4_perlector/run.py",
        )
        return ChairClient(
            manager=manager,
            identity=chair,
            tier=tier,
            retain=partial(retain_chair_bytes, context),
            decoding_config_sha256=decoding_config_sha256,
            record_temperature=record_temperature,
            # `ChairClient.__enter__` passes a plain dict, which is what
            # `read_run_receipt` requires.
            read_receipt=context.tree.read_run_receipt,
        )

    return factory


def retain_chair_bytes(context, data: bytes) -> dict[str, str]:
    """Store one chair response or call record under its own digest.

    The client retains before it parses (principle 2). Refused after the seal, which
    witnessed this stage's blob inventory; a later write would make it false.
    """
    if context.sealed:
        raise SchemaRefusal(
            "the Perlector has sealed its completion boundary; retaining a chair response "
            "afterwards would make its witnessed blob inventory false"
        )
    digest, result = context.tree.put_blob(context.stage, data)
    return {"relative_path": result.relative_path, "sha256": digest}


def engine_call_inputs(context, engine_call: dict[str, Any] | None) -> list[dict[str, str]]:
    """Bind the two blobs a live reading's record names as direct inputs.

    A fixture reading has no `engine_call` and adds nothing. Each reference is
    re-derived from the bytes on disk, so a record cannot name a response that is
    absent or has changed.
    """
    if engine_call is None:
        return []
    if not isinstance(engine_call, dict) or set(engine_call) != {
        "call_record_ref",
        "raw_response_ref",
        "response_sha256",
        "finish_reason",
        "served_model_id",
    }:
        raise SchemaRefusal(f"a live reading's engine_call has the wrong shape: {engine_call!r}")
    if engine_call["response_sha256"] != engine_call["raw_response_ref"]["sha256"]:
        raise SchemaRefusal(
            "a live reading's engine_call names two different digests for one response: "
            f"response_sha256={engine_call['response_sha256']!r}, "
            f"raw_response_ref sha256={engine_call['raw_response_ref']['sha256']!r}"
        )
    references = []
    for name in ("raw_response_ref", "call_record_ref"):
        claimed = engine_call[name]
        observed = context.input_ref(claimed["relative_path"])
        if observed != dict(claimed):
            raise SchemaRefusal(
                f"a live reading's {name} names {claimed!r}, but the retained bytes at that "
                f"path are {observed!r}"
            )
        references.append(observed)
    return references


def _live_reader(
    context,
    args,
    *,
    chair: ChairIdentity,
    protocol_config,
    decoding_policy: dict[str, Any],
    decoding_sha256: str,
    serving_factory,
    service: "ResidentChair",
) -> tuple[VLLMReader, dict[str, str]]:
    """Start this run's one chair; return its reader and receipt reference.

    Every record the pass publishes names the receipt of the service that answered,
    which `ChairClient.__enter__` has checked names this chair and revision
    (principle 6).
    """
    factory = serving_factory or default_serving_factory(
        bound_serving_recipes(context, args.serving_recipes_config),
        decoding_config_sha256=decoding_sha256,
        # The sealed reading-of-record temperature; `ChairClient` refuses anything but 0
        # rather than coercing it.
        record_temperature=decoding_policy["reading_of_record"]["temperature"],
    )
    # Assigned before entering so `close` covers any failure from here on; closing an
    # unstarted client is a no-op.
    service.client = factory(context, chair, args.placement_tier)
    service.client.__enter__()
    reader = VLLMReader(
        client=service.client,
        chair=chair,
        protocol_config=protocol_config,
        # No sealed output bound: vLLM bounds generation by `max_model_len`, so an
        # engine `"length"` means the context was exhausted, not that the harness cut
        # the reading.
        max_tokens=None,
    )
    return reader, dict(service.client.handle.receipt_reference)


def _distinct_inputs(references: list[dict[str, str]]) -> list[dict[str, str]]:
    """One entry per path, in first-named order, refusing two digests for one path.

    One content-addressed blob can honestly be reached twice (a re-proof answering the
    same bytes; a page partition and its native capture). Two digests under one path
    means a blob was rewritten.
    """
    distinct: dict[str, dict[str, str]] = {}
    for reference in references:
        seen = distinct.get(reference["relative_path"])
        if seen is None:
            distinct[reference["relative_path"]] = reference
        elif seen != reference:
            raise SchemaRefusal(
                f"two different digests are claimed for input {reference['relative_path']!r}: "
                f"{seen!r} and {reference!r}"
            )
    return list(distinct.values())


def _attempt_artifact_id(act_id: str, kind: str, operation: str, ordinal: int) -> str:
    return artifact_id(PERLECTOR, kind, act_id, perlector_attempt_id(act_id, operation, ordinal))


def _reading_already_sealed(context, act_id: str, ordinal: int, *, act_key: str) -> bool:
    """Whether this run tree already holds this act's Perlectio at this ordinal.

    A failed Perlectio is validated before the chair is skipped, so malformed failure
    bytes cannot become a permanent resume bypass.
    """
    attempt = perlector_attempt_id(act_id, "perlegere", ordinal)
    identifier = artifact_id(PERLECTOR, "perlectio", act_id, attempt)
    if not context.tree.resolve(
        context.tree.artifact_path(PERLECTOR, "perlectio", identifier)
    ).exists():
        return False
    reading = context.tree.read_artifact(PERLECTOR, "perlectio", identifier)
    if reading["outcome"] == "failed" and "failure" in reading["payload"]:
        validate_failed_perlectio(context, reading, act_id, expected_act_key=act_key)
    return True


def _sealed_pass_kinds(context, act_id: str, ordinal: int) -> frozenset[str]:
    """Which pre-Perlectio artifacts of this attempt are already on disk.

    An attempt that stopped part-way (abort, timeout, OOM, SIGKILL) leaves immutable
    artifacts; a resume that simply read again would republish them from a second live
    answer and be refused on every retry.
    """
    return frozenset(kind for kind, _identifier in _present_arms(context, act_id, ordinal))


def _present_arms(context, act_id: str, ordinal: int):
    """The kind and identifier of each pre-Perlectio artifact of this attempt on disk."""
    for kind, operation in PRE_PERLECTIO_ARTIFACTS:
        identifier = _attempt_artifact_id(act_id, kind, operation, ordinal)
        if context.tree.has_artifact(PERLECTOR, kind, identifier):
            yield kind, identifier


def _published_arm_refs(context, act_id: str, ordinal: int) -> list[dict[str, str]]:
    """Arms published before a failed call, named so a resume and the Recensor can inspect
    what completed without re-asking the chair."""
    return [
        context.artifact_ref(PERLECTOR, kind, identifier)
        for kind, identifier in _present_arms(context, act_id, ordinal)
    ]


# The slowest live call observed (441 answer tokens beside ~6,500 prompt tokens, 80 GB
# card, 2026-09-22) took about 33 s; the mean of fifteen was 12 s.
PLANNED_SECONDS_PER_CALL: Final = 40


def _acts_left_to_read(context, wanted: list[dict[str, Any]]) -> int:
    """Count the acts a live pass still has to read, refusing any it left half-read.

    Live resume is all-or-nothing per act: an act is either sealed or untouched. An
    interrupted attempt's artifacts cannot be finished by a second serving session
    without pairing two engines' answers in one reading, so it refuses before any
    chair starts, and those artifacts stay as that attempt's evidence.
    """
    left, half_read = 0, []
    for act in wanted:
        if act["outcome"] == "held":
            continue
        act_id = act["act_id"]
        ordinal = _next_attempt(context, act_id, act_regions(context, act_id)[0])
        if _reading_already_sealed(context, act_id, ordinal, act_key=act["act_key"]):
            continue
        if _sealed_pass_kinds(context, act_id, ordinal):
            half_read.append(act["act_key"])
        left += 1
    if half_read:
        raise ContractError(
            f"acts {half_read} hold artifacts from an interrupted live attempt and no "
            "Perlectio; a live pass resumes only from sealed or untouched acts. Read these "
            "pages in a new run; the interrupted attempt's artifacts remain its evidence"
        )
    return left


def _utc(value: str) -> datetime:
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        raise ValueError(f"{value!r} names no time zone")
    return parsed


def _refuse_past_deadline(deadline: datetime | None, seconds_needed: int, what: str) -> None:
    if deadline is None:
        return
    remaining = int((deadline - datetime.now(timezone.utc)).total_seconds())
    if remaining < seconds_needed:
        raise ContractError(
            f"{what} needs {seconds_needed}s at {PLANNED_SECONDS_PER_CALL}s a call, but the "
            f"reading deadline {deadline.isoformat()} leaves {remaining}s; nothing more was "
            "started. Give a later --reading-deadline, or fewer acts with --act"
        )


def with_engine_call(payload: dict, result: dict, fields: frozenset) -> frozenset:
    """Carry a live reading's engine call on its record; a fixture reading carries none.

    The field widens the closed set for that shape rather than becoming optional, so
    every record is validated against exactly what it carries.
    """
    engine_call = result.get("engine_call")
    if engine_call is None:
        return fields
    payload["engine_call"] = engine_call
    return fields | {"engine_call"}


def _page_renders_for(context, bases: list[dict]) -> list[dict]:
    """One downscaled page render per distinct page an act's regions touch.

    A continuation act spans two pages; nuda and the primed pass see both,
    because sight is never what nuda withholds.
    """
    by_page: dict[str, dict] = {}
    for basis in bases:
        page_id = basis["source_page_id"]
        if page_id not in by_page:
            by_page[page_id] = dossier_module.build_page_render(
                context,
                source_page_id=page_id,
                source_page_ordinal=basis["source_page_ordinal"],
            )
    return list(by_page.values())


def _whole_act_gap(testimonia: list[dict], references: dict[str, dict]) -> list[dict]:
    """The one gap an unreadable act carries: zero-width, never a character in `text`.

    Each variant carries the digest-checked reference to its Testimonium, so a displayed
    variant leads back to the sealed record.
    """
    evidence = [
        {
            "chair": record["payload"]["chair"],
            "testimonium_id": record["artifact_id"],
            "reference": references[record["artifact_id"]],
            "variant": record["payload"]["payload"],
        }
        for record in testimonia
        # Presence and type, never truthiness: a genuinely-empty witness
        # reported "" and that report is the strongest corroboration a
        # whole-act gap can carry.
        if record["outcome"] in WITNESS_READING_OUTCOMES
        and isinstance(record["payload"].get("payload"), str)
    ]
    return [{"position": "whole-act", "start": 0, "end": 0, "witness_evidence": evidence}]


def _region_pixels(bases: list[dict]) -> int:
    """The page-space area an act's regions cover: their union per page, summed.

    A fallback recrop re-cuts ink its original crop covers; summing would count that
    ink twice and mark every recovered act length-suspicious. Regions on different
    pages never overlap and still add.
    """
    by_page: dict[str, list[tuple[int, int, int, int]]] = {}
    for basis in bases:
        bounds = basis["transform"]["bounds"]
        by_page.setdefault(basis["source_page_id"], []).append(
            (bounds["x"], bounds["y"], bounds["x"] + bounds["w"], bounds["y"] + bounds["h"])
        )
    return sum(_union_area(rectangles) for rectangles in by_page.values())


def _union_area(rectangles: list[tuple[int, int, int, int]]) -> int:
    """The area covered by at least one rectangle, by coordinate compression.

    Exact in integers; quadratic in the handful of regions one act carries.
    """
    xs = sorted({x for rectangle in rectangles for x in (rectangle[0], rectangle[2])})
    ys = sorted({y for rectangle in rectangles for y in (rectangle[1], rectangle[3])})
    area = 0
    for x0, x1 in zip(xs, xs[1:], strict=False):
        for y0, y1 in zip(ys, ys[1:], strict=False):
            if any(
                left <= x0 and x1 <= right and top <= y0 and y1 <= bottom
                for left, top, right, bottom in rectangles
            ):
                area += (x1 - x0) * (y1 - y0)
    return area


def _page_pixels(page_renders: list[dict]) -> int:
    """The sealed area of every distinct page an act's regions were cut from.

    The truncation length signal's denominator, summed over pages as `_region_pixels`
    sums regions, so the two form one ratio.
    """
    return sum(
        render["transform"]["source_dimensions"]["w"]
        * render["transform"]["source_dimensions"]["h"]
        for render in page_renders
    )


def _reading_image_inputs(
    context,
    bases: list[dict],
    page_renders: list[dict],
    *,
    autopsia: dict[str, Any],
) -> list[dict]:
    """Every image blob the reading saw, and its partition authority, as direct inputs.

    The envelope binds what the dossier cites, so a Perlectio cannot claim a page
    render or partition its provenance never retained.
    """
    inputs = {
        reference["relative_path"]: reference
        for reference in (context.input_ref(basis["image_path"]) for basis in bases)
    }
    for render in page_renders:
        source = render.get("source")
        if not isinstance(source, dict) or not isinstance(source.get("relative_path"), str):
            raise SchemaRefusal("a Perlector page render carries no sealed source-page reference")
        for reference in (
            context.input_ref(source["relative_path"]),
            context.input_ref(render["image_path"]),
        ):
            inputs[reference["relative_path"]] = reference
    partition_ref = validate_autopsia(autopsia)["partition_ref"]
    prior = inputs.get(partition_ref["relative_path"])
    if prior is not None and prior != partition_ref:
        raise SchemaRefusal(
            "a cross-capture partition path conflicts with another direct input digest"
        )
    inputs[partition_ref["relative_path"]] = partition_ref
    return sorted(inputs.values(), key=_input_order)


# Closed and checked before publication: a missing field (identity, dissent, regime) is
# the failure a per-field type check never sees.
_PERLECTIO_FIELDS: Final = frozenset(
    {
        "act_key",
        "attempt_ordinal",
        "text",
        "basis",
        "dossier",
        "prompt",
        "dissent",
        "truncation",
        "uncertain_spans",
        "uncertainty_assessment",
        "gaps",
        "provenance",
        "lectio_kind",
        "self_revision",
        "protocol",
        "audit",
    }
)

# The instrument record: no `basis`, since a nuda reading has no witnesses, and its
# sampling design. Every record kind carries the doubt report, because a doubt reported
# on an instrument call is a measurement too (principle 2).
_LECTIO_NUDA_FIELDS: Final = frozenset(
    {
        "act_key",
        "attempt_ordinal",
        "text",
        "dossier",
        "prompt",
        "sampling",
        "dissent",
        "truncation",
        "uncertain_spans",
        "uncertainty_assessment",
        "gaps",
        "provenance",
    }
)

_LECTIO_PRIOR_FIELDS: Final = frozenset(
    {
        "act_key",
        "attempt_ordinal",
        "text",
        "dossier",
        "prompt",
        "dissent",
        "truncation",
        "uncertain_spans",
        "uncertainty_assessment",
        "gaps",
        "provenance",
        "protocol",
    }
)

_PRIMED_WITHOUT_PRIOR_FIELDS: Final = frozenset(
    {
        "act_key",
        "attempt_ordinal",
        "text",
        "basis",
        "dossier",
        "prompt",
        "sampling",
        "dissent",
        "truncation",
        "uncertain_spans",
        "uncertainty_assessment",
        "gaps",
        "provenance",
        "lectio_kind",
        "protocol",
        "membership",
    }
)

# Each reason nothing was read has a distinct closed shape; otherwise a future
# branch could omit its provenance without failing publication.
_NOT_RUN_HELD_FIELDS: Final = frozenset({"act_key", "attempt_ordinal", "reason", "provenance"})
_NOT_RUN_ABSENT_FIELDS: Final = frozenset(
    {"act_key", "attempt_ordinal", "reason", "basis", "dissent", "provenance"}
)
_NOT_RUN_CAPACITY_FIELDS: Final = _NOT_RUN_ABSENT_FIELDS | {
    "logical_act_id",
    "cross_capture_autopsia",
}


def validate_not_run_payload(payload: dict, *, fields: frozenset) -> None:
    """Refuse a not-run Perlectio missing part of the record it claims.

    Capacity holds validate their autopsia and partition input where they are produced.
    """
    missing = sorted(fields - set(payload))
    unexpected = sorted(set(payload) - fields)
    if missing or unexpected:
        raise SchemaRefusal(
            f"a Perlector not-run payload is not its closed schema: missing {missing}, "
            f"unexpected {unexpected}"
        )


_NO_FAILURE_EVIDENCE: Final = {
    "raw_response_ref": None,
    "call_record_ref": None,
    "request_sha256": None,
    "receipt_ref": None,
    "served_model_id": None,
    "response_completion": None,
}


def _failure_facts(phase: str, kind: str, code: str, detail: str, **evidence) -> dict[str, Any]:
    return {
        "phase": phase,
        "kind": kind,
        "code": code,
        "detail": detail,
        **_NO_FAILURE_EVIDENCE,
        **evidence,
    }


def _copied_ref(reference: Mapping[str, str] | None) -> dict[str, str] | None:
    return dict(reference) if reference is not None else None


def _reported_call_evidence(error: Exception) -> dict[str, Any]:
    return {
        "call_record_ref": _copied_ref(getattr(error, "call_record_ref", None)),
        "request_sha256": getattr(error, "request_sha256", None),
        "receipt_ref": _copied_ref(getattr(error, "receipt_ref", None)),
        "served_model_id": getattr(error, "served_model_id", None),
    }


def _publish_not_run(
    context,
    *,
    act_id: str,
    ordinal: int,
    fields: frozenset,
    payload: dict[str, Any],
    inputs: list[dict[str, str]] | None = None,
) -> None:
    validate_not_run_payload(payload, fields=fields)
    context.publish(
        kind="perlectio",
        subject_id=act_id,
        outcome="not-run",
        attempt=perlector_attempt_id(act_id, "perlegere", ordinal),
        inputs=inputs,
        payload=payload,
    )


def _failure_record(error: Exception, *, phase: str) -> dict[str, Any] | None:
    """Translate only observed engine and transport failures into retained facts.

    Contract and schema errors return `None`: recorded as an engine incident, one would
    let the stage seal over an integrity defect.
    """
    if isinstance(error, RequestCapacityRefusal):
        if error.capacity is None:
            raise error
        return _failure_facts(phase, "request-capacity", "REQUEST_OVER_CAPACITY", str(error))
    if isinstance(error, EngineSignalRefusal):
        return _failure_facts(
            phase,
            "engine-signal",
            error.code,
            error.detail,
            raw_response_ref=dict(error.raw_response_ref),
            call_record_ref=dict(error.call_record_ref),
            request_sha256=error.request_sha256,
            receipt_ref=dict(error.receipt_ref),
            served_model_id=error.served_model_id,
            response_completion="complete",
        )
    if isinstance(error, ChairResponseRefusal):
        return _failure_facts(
            phase,
            "chair-response",
            error.code,
            error.detail,
            raw_response_ref=_copied_ref(getattr(error, "raw_response_ref", None)),
            response_completion="complete",
            **_reported_call_evidence(error),
        )
    if isinstance(error, serving_errors.ChairTransportFailure):
        return _failure_facts(
            phase,
            "transport",
            error.code,
            error.detail,
            response_completion=getattr(error, "response_completion", None),
            **_reported_call_evidence(error),
        )
    if isinstance(error, EndpointUnavailable):
        return _failure_facts(
            phase,
            "transport",
            "endpoint-unavailable",
            str(error),
            response_completion="unknown",
        )
    return None


def _failure_evidence_inputs(failure: Mapping[str, Any]) -> list[dict[str, str]]:
    return [
        dict(failure[name])
        for name in ("raw_response_ref", "call_record_ref", "receipt_ref")
        if failure[name] is not None
    ]


def _failure_from_engine_call(
    context, engine_call: Mapping[str, Any] | None, *, detail: str
) -> dict[str, Any]:
    """Build the response-evidence half of a malformed re-proof failure."""
    record = _failure_facts("audit-reproof", "reproof-response", "ReproofResponseRefusal", detail)
    if engine_call is None:
        return record
    call_ref = dict(engine_call["call_record_ref"])
    try:
        call = json.loads(context.tree.read_bytes(call_ref["relative_path"]))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise SchemaRefusal("a re-proof's retained chair call record is not JSON") from error
    record.update(
        {
            "raw_response_ref": dict(engine_call["raw_response_ref"]),
            "call_record_ref": call_ref,
            "request_sha256": call.get("request_sha256"),
            "receipt_ref": call.get("receipt_ref"),
            "served_model_id": engine_call.get("served_model_id"),
            "response_completion": "complete",
        }
    )
    return record


def _publish_failed_perlectio(
    context, *, act_id: str, ordinal: int, inputs: list[dict[str, str]], payload: dict[str, Any]
) -> None:
    """Fully validate the prospective immutable record before publishing it."""
    attempt = perlector_attempt_id(act_id, "perlegere", ordinal)
    inputs = _distinct_inputs(inputs)
    candidate = build_envelope(
        run_id=context.tree.run_id,
        artifact_id=artifact_id(PERLECTOR, "perlectio", act_id, attempt),
        subject_id=act_id,
        stage=PERLECTOR,
        kind="perlectio",
        outcome="failed",
        config_digest=context.config_digest,
        adapter_revision=context.adapter_revision,
        inputs=inputs,
        payload=payload,
        attempt=attempt,
    )
    validate_failed_perlectio(context, candidate, act_id, expected_act_key=payload.get("act_key"))
    context.publish(
        kind="perlectio",
        subject_id=act_id,
        outcome="failed",
        attempt=attempt,
        inputs=inputs,
        payload=payload,
    )
    identifier = artifact_id(PERLECTOR, "perlectio", act_id, attempt)
    validate_failed_perlectio(
        context, context.tree.read_artifact(PERLECTOR, "perlectio", identifier), act_id
    )


def _publish_reading_failure(
    context,
    *,
    act_id: str,
    act_key: str,
    ordinal: int,
    inputs: list[dict[str, str]],
    failure: dict[str, Any],
    reason: str,
    provenance: dict[str, Any],
) -> None:
    _publish_failed_perlectio(
        context,
        act_id=act_id,
        ordinal=ordinal,
        inputs=inputs + _failure_evidence_inputs(failure),
        payload={
            "act_key": act_key,
            "attempt_ordinal": ordinal,
            "reason": reason,
            "failure": failure,
            "provenance": provenance,
        },
    )


def _dossier_image_refs(rows: Any, *, what: str) -> list[tuple[str, str]]:
    if not isinstance(rows, list):
        raise SchemaRefusal(f"a cross-capture dossier has no {what} list")
    references = []
    for row in rows:
        if (
            not isinstance(row, dict)
            or not isinstance(row.get("image_path"), str)
            or not row["image_path"]
            or not isinstance(row.get("image_sha256"), str)
        ):
            raise SchemaRefusal(f"a cross-capture dossier carries a malformed {what} reference")
        references.append((row["image_path"], row["image_sha256"]))
    return sorted(references)


def _validate_cross_capture_dossier(
    dossier: dict[str, Any], *, inputs: list[dict[str, str]] | None
) -> None:
    """Bind a published dossier to the exact atomic presentation it claims."""
    if "cross_capture_autopsia" not in dossier:
        return
    record = validate_autopsia(dossier["cross_capture_autopsia"])
    if dossier["logical_act_id"] != record["logical_act_id"]:
        raise SchemaRefusal(
            "a Perlector dossier's logical act identity disagrees with its cross-capture autopsia"
        )
    autopsia_regions = sorted(
        (ref["relative_path"], ref["sha256"])
        for view in record["views"]
        for ref in view["region_refs"]
    )
    autopsia_pages = sorted(
        (ref["relative_path"], ref["sha256"])
        for view in record["views"]
        for ref in view["page_render_refs"]
    )
    if _dossier_image_refs(dossier["regions"], what="region") != autopsia_regions:
        raise SchemaRefusal(
            "a Perlector dossier's regions differ from its complete cross-capture autopsia"
        )
    if _dossier_image_refs(dossier["page_renders"], what="page render") != autopsia_pages:
        raise SchemaRefusal(
            "a Perlector dossier's page renders differ from its complete cross-capture autopsia"
        )
    if inputs is not None and record["partition_ref"] not in inputs:
        raise SchemaRefusal(
            "a Perlector dossier cites a cross-capture partition that is absent from the "
            "reading's direct inputs"
        )


def validate_reading_payload(
    payload: dict,
    *,
    outcome: str,
    fields: frozenset,
    run_id: str | None = None,
    config_digest: str | None = None,
    protocol_config: dict[str, Any] | None = None,
    protocol_sha256: str | None = None,
    inputs: list[dict[str, str]] | None = None,
) -> None:
    """Refuse a reading payload that is missing part of the record it claims.

    Checked when written, so a defect surfaces where it was introduced.
    """
    refuse_capture_preference(payload, what="a Perlector reading")
    missing = sorted(fields - set(payload))
    unexpected = sorted(set(payload) - fields)
    if missing or unexpected:
        raise SchemaRefusal(
            f"a Perlector reading payload is not its closed schema: missing {missing}, "
            f"unexpected {unexpected}"
        )
    if outcome == "read" and (not isinstance(payload["text"], str) or not payload["text"].strip()):
        raise SchemaRefusal("a completed reading cannot establish an empty text")
    # The caller's field set decides the record shape, so a Perlectio carrying `basis:
    # None` refuses rather than passing as unprimed. Lectio nuda and lectio-prior are
    # both unprimed, so the refusals name the condition.
    is_unprimed = "basis" not in fields
    basis = payload.get("basis")
    if is_unprimed:
        if payload["dissent"] != []:
            raise SchemaRefusal(
                "an unprimed reading (Lectio nuda or lectio-prior) cannot dissent from "
                "testimony it was not shown"
            )
    elif not isinstance(basis, dict) or not isinstance(basis.get("testimonia"), list):
        raise SchemaRefusal("a Perlectio carries no Testimonium basis for its dissent record")
    else:
        validate_dissent(
            payload["dissent"], text=payload["text"], basis_testimonia=basis["testimonia"]
        )
    provenance = payload["provenance"]
    if (
        not isinstance(provenance, dict)
        or provenance.get("witness_regime") not in WITNESS_CONTEXT_REGIMES
    ):
        raise SchemaRefusal(
            "a Perlector reading records no witness regime; a reading's provenance includes "
            "what its reader was shown"
        )
    if provenance.get("chair_state") == "configured" and not provenance.get("resolved_identity"):
        raise SchemaRefusal(
            "a Perlector reading by a configured chair records no resolved identity"
        )
    reading_dossier = payload["dossier"]
    dossier_fields = {
        "act_id",
        "act_key",
        "witness_regime",
        "regions",
        "page_renders",
        "testimonia",
        "dossier_digest",
    }
    # Logical identity and atomic presentation travel together or not at all.
    _dossier_optional_variants = (
        set(),
        {"act_attachment"},
        {"prior_draft", "prior_draft_view"},
        {"act_attachment", "prior_draft", "prior_draft_view"},
    )
    _cross_capture_fields = {"logical_act_id", "cross_capture_autopsia"}
    _allowed_dossier_shapes = tuple(
        dossier_fields | variant | extra
        for variant in _dossier_optional_variants
        for extra in (set(), _cross_capture_fields)
    )
    if not isinstance(reading_dossier, dict) or set(reading_dossier) not in _allowed_dossier_shapes:
        raise SchemaRefusal("a Perlector reading carries no closed dossier record")
    if reading_dossier["act_key"] != payload["act_key"]:
        raise SchemaRefusal("a Perlector reading disagrees with its dossier's act key")
    if reading_dossier["witness_regime"] != provenance["witness_regime"]:
        raise SchemaRefusal("a Perlector reading disagrees with its dossier's witness regime")
    dossier_body = {key: value for key, value in reading_dossier.items() if key != "dossier_digest"}
    if reading_dossier["dossier_digest"] != digest_of(dossier_body):
        raise SchemaRefusal("a Perlector dossier digest does not match the dossier it seals")
    dossier_module.assert_no_order_bearing_field(dossier_body)
    _validate_cross_capture_dossier(reading_dossier, inputs=inputs)
    lectio_kind = payload.get("lectio_kind")
    prior_draft = reading_dossier.get("prior_draft")
    if lectio_kind == "primed-with-prior":
        if (
            not isinstance(prior_draft, dict)
            or set(prior_draft) != {"reference", "text"}
            or not isinstance(prior_draft["text"], str)
            or reading_dossier.get("prior_draft_view") not in {"fed", "withheld"}
        ):
            raise SchemaRefusal(
                "a Perlectio claims primed-with-prior but carries no closed prior-draft "
                "reference and view"
            )
        validate_input_refs([prior_draft["reference"]])
    elif lectio_kind == "primed-without-prior":
        # Key presence, not value: the shape check admits these keys, so a None
        # prior_draft beside a view key would pass a value test.
        if "prior_draft" in reading_dossier or "prior_draft_view" in reading_dossier:
            raise SchemaRefusal(
                "a Perlectio claims primed-without-prior but carries prior-draft data"
            )
    elif lectio_kind is not None:
        # `None` is the kinds whose field sets exclude the key. Any other value would
        # publish its prior-draft evidence uninspected.
        raise SchemaRefusal(
            f"a Perlector reading names unknown lectio kind {lectio_kind!r}; a kind this "
            "validator cannot name would publish its prior-draft evidence unchecked"
        )
    if "act_attachment" in reading_dossier:
        attachment = reading_dossier["act_attachment"]
        if (
            not isinstance(attachment, dict)
            or set(attachment)
            != {"reference", "page_witness_count", "comparison_views", "edge_deltas"}
            or not isinstance(attachment["reference"], dict)
            or not isinstance(attachment["page_witness_count"], int)
            or isinstance(attachment["page_witness_count"], bool)
            or attachment["page_witness_count"] < 0
            or not isinstance(attachment["comparison_views"], dict)
            or not isinstance(attachment["edge_deltas"], dict)
        ):
            raise SchemaRefusal("a Perlector dossier has malformed act-attachment evidence")
        if is_unprimed:
            raise SchemaRefusal(
                "an unprimed reading's dossier cannot carry witness-derived act attachment metadata"
            )
    dossier_testimonia = reading_dossier["testimonia"]
    if not isinstance(dossier_testimonia, list):
        raise SchemaRefusal("a Perlector dossier has no Testimonium list")
    if is_unprimed:
        if dossier_testimonia:
            raise SchemaRefusal("an unprimed reading's dossier cannot carry Testimonia")
    elif len(dossier_testimonia) != len(basis["testimonia"]):
        raise SchemaRefusal(
            "a Perlector dossier does not account for exactly its Testimonium basis"
        )
    else:
        # Label for label: the prompt must show the witness set its basis, dissent and
        # export record. Blinded labels are re-derived only when the caller supplies the
        # run identity.
        basis_chairs = {row["chair"] for row in basis["testimonia"] if isinstance(row, dict)}
        dossier_labels = {
            row["witness_label"] for row in dossier_testimonia if isinstance(row, dict)
        }
        regime_name = reading_dossier["witness_regime"]
        if regime_name == regime.NAMED:
            expected_labels = basis_chairs
        elif run_id is not None and config_digest is not None:
            expected_labels = {
                regime.pseudonym_for(chair, run_id=run_id, config_digest=config_digest)
                for chair in basis_chairs
            }
        else:
            expected_labels = None
        if expected_labels is not None and dossier_labels != expected_labels:
            raise SchemaRefusal(
                "a Perlector dossier's witness labels do not match its Testimonium basis"
            )
    prompt_record = payload["prompt"]
    identity_record = provenance.get("resolved_identity")
    if not isinstance(identity_record, dict):
        raise SchemaRefusal("a Perlector prompt has no resolved chair identity")
    try:
        identity = ChairIdentity(**identity_record)
    except TypeError as error:
        raise SchemaRefusal("a Perlector prompt carries a malformed chair identity") from error
    protocol_record = payload.get("protocol")
    if protocol_record is not None and (
        not isinstance(protocol_record, dict)
        or set(protocol_record) != {"selection_rule", "page_shared_prefix_policy", "draft_fed"}
        or not isinstance(protocol_record["draft_fed"], bool)
    ):
        raise SchemaRefusal("a prior-draft protocol record is not its closed schema")
    # `draft_fed` and the dossier's view state one fact twice. `self_revision` is only
    # interpretable against a known feeding state, so they must agree even though
    # production derives both from one flag.
    prior_draft_view = reading_dossier.get("prior_draft_view")
    if protocol_record is not None and prior_draft_view is not None:
        declared_view = "fed" if protocol_record["draft_fed"] else "withheld"
        if prior_draft_view != declared_view:
            raise SchemaRefusal(
                f"a Perlector reading shows its prior draft {prior_draft_view!r} while the same "
                f"record's protocol declares draft_fed {protocol_record['draft_fed']!r}"
            )
    if protocol_config is None and protocol_record is not None:
        raise SchemaRefusal(
            "a Perlector reading carries a prior-draft protocol record but this validation "
            "call was not given the sealed protocol bytes it reproduces from -- a cwd-relative "
            "reload is not a sealed-config recheck"
        )
    # Bind the record's policy names to the sealed bytes: the prompt check below
    # reproduces only the prefix policy, so the `protocol` block could otherwise name a
    # different rule.
    if protocol_record is not None:
        declared = (protocol_record["selection_rule"], protocol_record["page_shared_prefix_policy"])
        sealed = (protocol_config["selection_rule"], protocol_config["page_shared_prefix_policy"])
        if declared != sealed:
            raise SchemaRefusal(
                f"a Perlector reading declares protocol {declared!r} while the bytes this run "
                f"sealed declare {sealed!r}"
            )
    if protocol_config is None:
        protocol_config = {
            "page_shared_prefix_policy": protocol.PAGE_SHARED_PREFIX_POLICY,
            "pass_b_fragment": "",
        }
    if protocol_sha256 is None:
        protocol_sha256 = "unsealed-test"
    expected_base_prompt = prompts.prompt_evidence(
        identity, reading_dossier, protocol_config, protocol_sha256
    )
    if isinstance(prompt_record, dict) and prompt_record.get("schema") == audit.AUDIT_PROMPT_SCHEMA:
        audit.validate_audit_prompt_evidence(prompt_record)
        if prompt_record["base_prompt"] != expected_base_prompt:
            raise SchemaRefusal(
                "a Perlector audit prompt does not reproduce its base prompt from the dossier"
            )
    elif prompt_record != expected_base_prompt:
        raise SchemaRefusal(
            "a Perlector prompt record does not reproduce from its resolved chair and dossier"
        )
    if (
        not isinstance(payload["truncation"], dict)
        or payload["truncation"].get("classification") not in truncation.CLASSIFICATIONS
    ):
        raise SchemaRefusal(
            "a Perlector reading carries no truncation classification; truncation is detected "
            "by an instrument, never assumed"
        )
    if outcome == "read" and payload["truncation"]["classification"] != truncation.COMPLETE:
        raise SchemaRefusal(
            "a truncated or unknown attempt cannot carry the completed outcome 'read'"
        )
    if outcome == "truncated" and payload["truncation"]["classification"] == truncation.COMPLETE:
        raise SchemaRefusal(
            "a Perlectio with outcome 'truncated' cannot carry a 'complete' truncation "
            "classification; outcome == 'truncated' means 'not established complete', and "
            "the truncation field is where that is confirmed or held unknown, never "
            "contradicted"
        )
    _validate_sealed_doubt(payload, fields=fields)
    if "audit" not in fields:
        annotations.validate_annotations(payload, outcome=outcome)
        return
    # Re-proof offsets index the frozen semi-final, which may be longer than the final;
    # the chain check binds them before publication, so no bound is guessed here.
    audit.validate_perlectio_audit(payload.get("audit"), text_length=None)
    annotations.validate_annotations(payload, outcome=outcome)


def _validate_sealed_doubt(payload: dict, *, fields: frozenset) -> None:
    """Refuse a sealed doubt report this producer would never have written.

    A malformed report would reach the Recensor as no hold and fail only at the
    Archetypus. The layer rule applies only to records with no audit, where every span
    is the reader's own. On an established Perlectio only `audit.validate_chain`, which
    `_read_the_acts` runs before the publish, can tell whose span is whose.
    """
    record = payload["uncertainty_assessment"]
    if not isinstance(record, dict) or set(record) != {"state", "problem"}:
        raise SchemaRefusal(
            "a Perlector reading carries no closed {state, problem} doubt assessment"
        )
    state = record["state"]
    if type(state) is not str or state not in annotations.ASSESSMENT_STATES:
        raise SchemaRefusal(f"a Perlector reading names an unknown doubt state {state!r}")
    problem = record["problem"]
    if problem is not None and (type(problem) is not str or not problem):
        raise SchemaRefusal(
            "a Perlector reading's doubt problem is neither null nor a non-empty string"
        )
    if (state == annotations.ASSESSMENT_ASSESSED) != (problem is None):
        raise SchemaRefusal(
            "a Perlector reading's doubt report carries a problem exactly when it is not assessed"
        )
    if state == annotations.ASSESSMENT_ASSESSED or "audit" in fields:
        return
    if payload["uncertain_spans"]:
        raise SchemaRefusal(
            f"a {state!r} Perlector reading publishes an uncertain span of its own; a reader "
            "that was never asked, or whose report was refused, reports no doubt"
        )
    gaps = payload["gaps"]
    # Shape first: this check runs before `validate_annotations`, so it may not
    # assume the layer is a list of objects.
    if not isinstance(gaps, list):
        raise SchemaRefusal("a Perlector reading's gap layer is not a list")
    if not all(isinstance(gap, dict) and gap.get("position") == "whole-act" for gap in gaps):
        raise SchemaRefusal(
            f"a {state!r} Perlector reading publishes a gap of its own; only a reader that "
            "was asked reports where its sight failed"
        )


def _reproof_call(
    reproof: dict[str, Any],
    *,
    base_prompt: dict[str, Any],
    base_text: str,
    request: dict[str, Any],
) -> dict[str, Any] | None:
    """The re-proof's retained call, in the closed shape the finding seals, or `None`."""
    engine_call = reproof.get("engine_call")
    if engine_call is None:
        return None
    request_sha256 = reproof.get("request_sha256")
    rendered_prompt = reproof.get("rendered_prompt")
    if not isinstance(request_sha256, str) or not isinstance(rendered_prompt, str):
        raise SchemaRefusal("a live re-proof retains no exact rendered prompt and request digest")
    return {
        "call_record_ref": dict(engine_call["call_record_ref"]),
        "raw_response_ref": dict(engine_call["raw_response_ref"]),
        "response_sha256": engine_call["response_sha256"],
        "finish_reason": engine_call["finish_reason"],
        "served_model_id": engine_call["served_model_id"],
        "request_sha256": request_sha256,
        "audit_prompt": audit.audit_prompt_evidence(
            base_prompt=base_prompt,
            base_text=base_text,
            request=request,
            request_sha256=request_sha256,
            rendered_text=rendered_prompt,
        ),
    }


def _assessed(result: dict[str, Any], *, text: str) -> dict[str, Any]:
    """The reader's doubt report over `text`, as the closed record the Perlectio seals.

    No `assessment` key means `not-assessed`: the producer never invents a report. A
    report that cannot anchor to the exact text becomes `malformed`, carrying the
    refusal as its problem, never an empty confident list (principle 8).
    """
    report = result.get("assessment")
    if report is None:
        return annotations.not_assessed()
    try:
        return annotations.validate_assessment(copy.deepcopy(report), text)
    except SchemaRefusal as error:
        return annotations.malformed_assessment(f"the reader's doubt report was refused: {error}")


def _union_with_projection(
    projected: list[dict[str, Any]], reader_spans: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """The published span layer: the exhausted-cap projection, then the reader's own.

    An exact repeat is kept: it is two instruments doubting the same thing, and no other
    artifact holds the reader's report, so dropping it would erase the reader's
    agreement with the audit.
    """
    return projected + list(reader_spans)


def _sealed_assessment(assessment: dict[str, Any]) -> dict[str, Any]:
    """The sealed state and problem; the report's spans and gaps go in the record's layers."""
    return {"state": assessment["state"], "problem": assessment["problem"]}


def _published_doubt(
    result: dict[str, Any], *, text: str, outcome: str, whole_act_gaps: list[dict[str, Any]]
) -> tuple[dict[str, Any], list[dict[str, Any]], list[dict[str, Any]]]:
    """One call's doubt report as the record publishes it: sealed state, spans, gaps.

    Over an unreadable act the report is re-asked against the empty text actually
    published, so a report that cannot anchor becomes `malformed` and visible.
    """
    if outcome == "no-readable-text":
        reasked = _assessed(result, text="")
        # A zero-width edge gap validates over empty text but would be replaced by the
        # whole-act gap under an `assessed` state, so any layer of its own makes the
        # report `malformed` rather than silently dropped.
        if reasked["uncertain_spans"] or reasked["gaps"]:
            reasked = annotations.malformed_assessment(
                "the reader's doubt report was set aside: it reports a doubt of its own over "
                "an act published as unreadable, where the whole-act gap is the only "
                "annotation the outcome allows"
            )
        return _sealed_assessment(reasked), [], whole_act_gaps
    assessment = _assessed(result, text=text)
    return (
        _sealed_assessment(assessment),
        list(assessment["uncertain_spans"]),
        list(assessment["gaps"]),
    )


def _resolve_outcome(*, declared_failure: str | None, truncation_record: dict, text: str) -> str:
    """One place the outcome is decided, so the precedence is stated once:
    a scenario's declared engine behaviour outranks the computed detector
    (it stands in for a real engine's own report), the detector outranks a
    default `read`, and an empty reading is never silently `read`."""
    if declared_failure is not None:
        return declared_failure
    if truncation.holds_as_failure(truncation_record["classification"]):
        return "truncated"
    if _publishes_empty(text):
        # The same emptiness rubric the publish-time schema uses: a reader
        # returning "\n" for one act is an unreadable act, not a reason to
        # abort the stage and lose the parish's other readings.
        return "no-readable-text"
    return "read"


def _publishes_empty(text: str) -> bool:
    """The one emptiness rubric, shared with the audit so both measure the published text."""
    return not text.strip()


def _reconciled_truncation(*, declared_failure: str | None, truncation_record: dict) -> dict:
    """Keep the published truncation record from contradicting a declared failure.

    A declared failure stands in for an engine's report that the reading did not
    complete, which the text's shape need not show. The signals stay as measured; the
    classification rises to `unknown`, since the instrument did not see the cutoff.
    """
    if (
        declared_failure == "truncated"
        and truncation_record["classification"] == truncation.COMPLETE
    ):
        return {**truncation_record, "classification": truncation.UNKNOWN}
    return truncation_record


def _sealed_length_floor(protocol_config: dict[str, Any] | None) -> int | None:
    """This run's sealed truncation floor, or `None` when no sealed protocol reached this pass.

    Never a guessed default: a re-proof is held to the floor this run sealed or to none.
    """

    if not isinstance(protocol_config, dict):
        return None
    table = protocol_config.get(protocol.TRUNCATION_TABLE)
    if not isinstance(table, dict):
        return None
    floor = table.get(protocol.LENGTH_FLOOR_FIELD)
    return floor if isinstance(floor, int) and not isinstance(floor, bool) else None


def _audited_truncation(
    *,
    pass_b: dict,
    declared_failure: str | None,
    text: str,
    region_pixels: int,
    page_pixels: int,
    truncation_policy: dict,
    stop_reason: str | None,
    measured: dict | None = None,
) -> dict:
    """The truncation instrument, re-measured over an audit-changed reading.

    A re-proof that changes the text invalidates the Pass-B record, stop reason
    included; otherwise a cut-off re-proof could replace text while the record said
    `complete`. The verdict never improves: a span-scoped re-proof cannot restore ink a
    cut-off Pass B never read.
    """
    # `measured` is the caller's measurement of the same text and stop reason, passed in
    # so this is visibly one measurement.
    audited = _reconciled_truncation(
        declared_failure=declared_failure,
        truncation_record=measured
        if measured is not None
        else truncation.classify(
            text,
            region_pixels=region_pixels,
            page_pixels=page_pixels,
            truncation_policy=truncation_policy,
            stop_reason=stop_reason,
        ),
    )
    if (
        pass_b["classification"] != truncation.COMPLETE
        and audited["classification"] == truncation.COMPLETE
    ):
        return {**audited, "classification": pass_b["classification"]}
    return audited


def _audit_semi_final(
    *,
    act_id: str,
    page_id: str,
    order: int,
    text: str,
    regions: list[dict[str, Any]],
    dossier: dict[str, Any],
) -> dict[str, Any]:
    """Derive one Pass-C row from either a pending or sealed Perlectio."""
    if not regions:
        raise FatalAccounting(f"Perlectio for {act_id} has no region for its audit geometry")
    first = regions[0]
    bounds = first.get("transform", {}).get("bounds")
    if first.get("source_page_id") != page_id or not isinstance(bounds, dict):
        raise FatalAccounting(
            f"Perlectio for {act_id} does not bind its starting page and crop geometry"
        )
    # Proved before it becomes a sort key: comparing None with an integer would end the
    # page's flag pass in an unnamed TypeError.
    if any(
        not isinstance(bounds.get(side), int) or isinstance(bounds.get(side), bool)
        for side in ("x", "y")
    ):
        raise FatalAccounting(
            f"Perlectio for {act_id} has no integer crop origin to order its page audit by"
        )
    testimonia = dossier.get("testimonia")
    if not isinstance(testimonia, list):
        raise FatalAccounting(f"Perlectio for {act_id} has no sealed audit testimonia")
    reports: list[str] = []
    for record in testimonia:
        if not isinstance(record, dict):
            raise FatalAccounting(f"Perlectio for {act_id} carries a non-object audit testimonium")
        reported = record.get("reported")
        # `None` covers non-reading and structured reports; the latter remains
        # present in dissent as incomparable rather than becoming invented text.
        if reported is None:
            continue
        if not isinstance(reported, str):
            raise FatalAccounting(
                "a dossier reported value is neither text nor null. "
                "The audit cannot compare it without coercing witness evidence. "
                "Rebuild the dossier from retained derived testimony before running the audit."
            )
        reports.append(reported)
    return {
        "act_id": act_id,
        "page_id": page_id,
        "order": order,
        # The crop position, independent of declared order; otherwise the "order" flag
        # class could never fire.
        "geometry_order": (bounds.get("y"), bounds.get("x")),
        "text": text,
        "testimonia": reports,
        # Pass C accounts only within the delivered crop. Page partition and
        # residual-ink predicates belong to the Recensor.
        "within_crop": True,
    }


def audit_page_ids(bases: list[dict[str, Any]]) -> list[str]:
    """The complete page set for one act's page audit, sorted.

    Only a denominator: page sequence lives on each region basis, so list order carries
    no meaning.
    """
    page_ids = sorted({basis["source_page_id"] for basis in bases})
    if not page_ids:
        raise FatalAccounting(
            "a Perlectio has no source pages for its audit; no page comparison can be "
            "measured; restore its verified region basis before running Pass C"
        )
    return page_ids


def audit_semi_finals_for_pages(
    *, act_id: str, order: int, text: str, bases: list[dict[str, Any]], dossier: dict[str, Any]
) -> list[dict[str, Any]]:
    """Place the same act in every page comparison its pixels contribute to."""
    grouped: dict[str, list[dict[str, Any]]] = {}
    for basis in bases:
        grouped.setdefault(basis["source_page_id"], []).append(basis)
    return [
        _audit_semi_final(
            act_id=act_id, page_id=page_id, order=order, text=text, regions=regions, dossier=dossier
        )
        for page_id, regions in sorted(grouped.items())
    ]


def _sealed_sibling_semi_finals(
    context,
    current: list[dict[str, Any]],
    *,
    expected: list[dict[str, Any]],
    protocol_config: dict[str, Any] | None = None,
    protocol_sha256: str | None = None,
) -> list[dict[str, Any]]:
    """Read same-page sibling Perlectiones as immutable recovery context.

    Sibling text comes from the sealed, validated Perlectio, never a reconstruction.
    Only rows are returned, so a sibling cannot be republished.
    """
    current_ids = {row["act_id"] for row in current}
    page_ids = {row["page_id"] for row in current}
    order_by_id = {act["act_id"]: order for order, act in enumerate(expected)}
    # Primary-page scalars cannot select candidates: only a sibling's sealed
    # region basis reveals whether a continuation shares the recovered page.
    # Omitting a row can also invent adjacency between its former neighbours.
    sibling_ids = {act["act_id"] for act in expected if act["act_id"] not in current_ids}
    records_by_subject: dict[str, list[dict[str, Any]]] = {act_id: [] for act_id in sibling_ids}
    for entry in context.tree.build_manifest(PERLECTOR)["artifacts"]:
        if entry["kind"] != "perlectio" or entry["subject_id"] not in sibling_ids:
            continue
        record = context.tree.read_artifact(PERLECTOR, "perlectio", entry["artifact_id"])
        records_by_subject[entry["subject_id"]].append(record)

    siblings = []
    for act_id in sorted(sibling_ids, key=order_by_id.__getitem__):
        records = records_by_subject[act_id]
        if not records:
            # Every expected act carries a Perlectio by now (a held one carries
            # `not-run`). A missing one would shrink the page's flag comparisons or
            # invent adjacency between its neighbours.
            raise FatalAccounting(
                f"act {act_id} has no Perlectio, so recovery cannot determine whether it "
                "shares a contributing page; the page denominator is unknown; restore its "
                "retained Perlectio before recomputing the audit"
            )
        reading = latest_attempt(
            records, f"sealed sibling Perlectio for {act_id}", operation="perlegere"
        )
        payload = reading.get("payload")
        # Operational failures never produced Pass-B text, so they are skipped, but only
        # once the failed-Perlectio validator proves them; a malformed record aborts.
        if reading["outcome"] == "failed":
            validate_failed_perlectio(
                context,
                reading,
                act_id,
                expected_act_key=payload.get("act_key") if isinstance(payload, dict) else None,
            )
            continue
        # A not-run sibling was also absent from the original Pass-B collection. Any
        # other outcome without text is malformed and may not be dropped.
        if reading["outcome"] == "not-run":
            continue
        if not isinstance(payload, dict) or not isinstance(payload.get("text"), str):
            raise FatalAccounting(
                f"sealed sibling Perlectio for {act_id} has outcome {reading['outcome']!r} "
                "but no text to audit; a malformed sibling may not be dropped from the "
                "page's flag comparisons"
            )
        validate_reading_payload(
            payload,
            outcome=reading["outcome"],
            fields=_PERLECTIO_FIELDS,
            run_id=context.tree.run_id,
            config_digest=context.config_digest,
            protocol_config=protocol_config,
            protocol_sha256=protocol_sha256,
            inputs=reading["inputs"],
        )
        chain = audit.validate_chain(
            context.tree,
            reading,
            act_id,
            length_floor_characters_per_page=_sealed_length_floor(protocol_config),
        )
        draft_payload = chain["draft"]["payload"]
        finding_payload = chain["finding"]["payload"]
        expected_page = expected[order_by_id[act_id]]["page_id"]
        if (
            expected_page not in draft_payload["page_ids"]
            or expected_page not in finding_payload["page_ids"]
        ):
            raise FatalAccounting(
                f"sealed sibling Perlectio for {act_id} does not reconcile with its audit chain"
            )
        bases = reading_basis_regions(reading, f"sealed sibling Perlectio for {act_id}")
        sibling_page_ids = {basis["source_page_id"] for basis in bases}
        if not page_ids.intersection(sibling_page_ids):
            continue
        # Recovery must reproduce the whole-run denominator: a cross-page
        # sibling participates in each contributing page's comparisons.
        siblings.extend(
            audit_semi_finals_for_pages(
                act_id=act_id,
                order=order_by_id[act_id],
                text=payload["text"],
                bases=bases,
                dossier=payload["dossier"],
            )
        )
    return siblings


def flag_location_basis(
    dossier: dict[str, Any], flags: list[dict[str, Any]], *, semi_final_text: str
) -> list[dict[str, str]]:
    """Name the chair and retained-text derivation behind testimony-diff flags.

    Only a report whose comparison with the semi-final produced a frozen flag location
    is named; an agreeing witness did not locate the flag. Each row names its location,
    so it cannot be read against the wrong flag.
    """
    # The shared constant, not a literal: the validator expects a basis row for every
    # witness-derived class, including ones added later.
    located_classes: dict[tuple[int, int], str] = {}
    for flag in flags:
        if flag.get("class") in audit.WITNESS_DERIVED_LOCATION_CLASSES:
            located_classes[(flag["location"]["start"], flag["location"]["end"])] = flag["class"]
    if not located_classes:
        return []
    rows = dossier.get("testimonia", [])
    located = []
    for row in rows:
        if (
            not isinstance(row, dict)
            or not isinstance(row.get("reported"), str)
            or row.get("reported_basis") not in {"own-report", "page-slice"}
            or row["reported"] == semi_final_text
        ):
            continue
        span = audit.text_change_span(semi_final_text, row["reported"])
        if span not in located_classes:
            continue
        located.append(
            {
                "class": located_classes[span],
                "chair": row["witness_label"],
                "derivation": row["reported_basis"],
                "location": {"start": span[0], "end": span[1]},
            }
        )
    return sorted(
        located,
        key=lambda row: (
            row["location"]["start"],
            row["location"]["end"],
            row["chair"],
            row["derivation"],
        ),
    )


def _page_flags(
    context,
    semi_finals: list[dict[str, Any]],
    *,
    expected: list[dict[str, Any]],
    recovery_act_id: str | None,
    protocol_config: dict[str, Any] | None = None,
    protocol_sha256: str | None = None,
) -> dict[str, list[dict[str, Any]]]:
    frozen = list(semi_finals)
    if recovery_act_id is not None:
        frozen.extend(
            _sealed_sibling_semi_finals(
                context,
                frozen,
                expected=expected,
                protocol_config=protocol_config,
                protocol_sha256=protocol_sha256,
            )
        )
    return audit.flags_once_per_page(frozen)


def _publish_audit_draft(
    context,
    row: dict[str, Any],
    flags: list[dict[str, Any]],
    *,
    round_cap: int,
    policy_record: dict[str, Any],
) -> tuple[dict[str, Any], dict[str, str]]:
    """Freeze the Pass-B semi-final and its page flags before any re-proof."""
    payload = row["payload"]
    draft_payload = {
        "act_key": row["act"]["act_key"],
        "attempt_ordinal": payload["attempt_ordinal"],
        "semi_final_text": payload["text"],
        "page_ids": audit_page_ids(row["bases"]),
        "round_cap": round_cap,
        "policy": policy_record,
        "flags": flags,
        "flag_location_basis": flag_location_basis(
            payload["dossier"], flags, semi_final_text=payload["text"]
        ),
    }
    audit.validate_draft(draft_payload)
    draft = context.publish(
        kind="audit-draft",
        subject_id=row["act_id"],
        outcome="read",
        attempt=perlector_attempt_id(row["act_id"], "perlegere", payload["attempt_ordinal"]),
        inputs=row["inputs"],
        payload=draft_payload,
    )
    return draft_payload, context.input_ref(draft.relative_path)


def _cap_exhausted_spans(flags: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        {"start": start, "end": end, "reason": "audit-round-cap-exhausted"}
        for start, end in ((flag["location"]["start"], flag["location"]["end"]) for flag in flags)
        if start < end
    ]


def _row_truncation(
    row: dict[str, Any], text: str, *, stop_reason: str | None, protocol_config: dict[str, Any]
) -> dict[str, Any]:
    return truncation.classify(
        text,
        region_pixels=row["region_pixels"],
        page_pixels=row["page_pixels"],
        truncation_policy=protocol_config[protocol.TRUNCATION_TABLE],
        stop_reason=stop_reason,
    )


def _reseal_dossier(dossier: dict[str, Any]) -> dict[str, Any]:
    """Sweep and seal the final fields retained for publication."""
    body = {key: value for key, value in dossier.items() if key != "dossier_digest"}
    # Combined transport fields are added after dossier construction, so its
    # original preference sweep and digest do not cover the published object.
    dossier_module.assert_no_order_bearing_field(body)
    return {**body, "dossier_digest": digest_of(body)}


@dataclass(frozen=True)
class _Attempt:
    """The facts every record of one act's reading attempt is published with."""

    act_key: str
    act_id: str
    ordinal: int
    chair: ChairIdentity
    bases: list[dict]
    page_renders: list[dict]
    region_pixels: int
    page_pixels: int
    protocol_config: dict[str, Any]
    protocol_sha256: str
    receipt_ref: dict[str, str] | None


def _publication_pass_data(
    attempt: _Attempt, dossier: dict[str, Any], result: dict[str, Any]
) -> tuple[dict[str, Any], dict[str, Any], str, dict[str, Any], str]:
    sealed_dossier = _reseal_dossier(dossier)
    prompt = prompts.prompt_evidence(
        attempt.chair, sealed_dossier, attempt.protocol_config, attempt.protocol_sha256
    )
    truncation_record = truncation.classify(
        result["text"],
        region_pixels=attempt.region_pixels,
        page_pixels=attempt.page_pixels,
        truncation_policy=attempt.protocol_config[protocol.TRUNCATION_TABLE],
        stop_reason=result["stop_reason"],
    )
    outcome = _resolve_outcome(
        declared_failure=None, truncation_record=truncation_record, text=result["text"]
    )
    text = "" if outcome == "no-readable-text" else result["text"]
    return sealed_dossier, prompt, outcome, truncation_record, text


def _arm_image_inputs(
    context, attempt: _Attempt, sealed_dossier: dict[str, Any]
) -> list[dict[str, str]]:
    return _reading_image_inputs(
        context,
        attempt.bases,
        attempt.page_renders,
        autopsia=sealed_dossier["cross_capture_autopsia"],
    )


def _protocol_record(context, protocol_config: dict[str, Any]) -> dict[str, Any]:
    return {
        "selection_rule": protocol_config["selection_rule"],
        "page_shared_prefix_policy": protocol_config["page_shared_prefix_policy"],
        "draft_fed": context.draft_fed,
    }


def _testimonium_references(context, testimonia: list[dict]) -> dict[str, dict[str, str]]:
    return {
        record["artifact_id"]: context.artifact_ref(
            ATTESTATORES, "testimonium", record["artifact_id"]
        )
        for record in testimonia
    }


def _testimonia_basis(testimonia: list[dict], references: dict[str, dict]) -> list[dict]:
    return [
        {
            "chair": record["payload"]["chair"],
            "artifact_id": record["artifact_id"],
            "outcome": record["outcome"],
            "reference": references[record["artifact_id"]],
        }
        for record in testimonia
    ]


def _publish_lectio_nuda(
    context,
    attempt: _Attempt,
    dossier: dict[str, Any],
    result: dict[str, Any],
    approval_ref: ApprovalRecordBinding,
) -> None:
    """Publish outside Perlectio kind and attempt identity with no witness facts."""
    nuda_dossier, prompt, outcome, truncation_record, nuda_text = _publication_pass_data(
        attempt, dossier, result
    )
    nuda_assessment, nuda_spans, nuda_gaps = _published_doubt(
        result,
        text=nuda_text,
        outcome=outcome,
        whole_act_gaps=_whole_act_gap([], {}),
    )
    payload = {
        "act_key": attempt.act_key,
        "attempt_ordinal": attempt.ordinal,
        "text": nuda_text,
        "dossier": nuda_dossier,
        "prompt": prompt,
        "sampling": nuda.sampling_design(
            nuda_per_mille=context.nuda_per_mille,
            approval_ref=approval_ref,
        ),
        "dissent": [],
        "truncation": truncation_record,
        "uncertain_spans": nuda_spans,
        "uncertainty_assessment": nuda_assessment,
        "gaps": nuda_gaps,
        "provenance": provenance_for(
            context, attempt.chair, attempted=True, receipt_ref=attempt.receipt_ref
        ),
    }
    fields = with_engine_call(payload, result, _LECTIO_NUDA_FIELDS)
    reading_inputs = _arm_image_inputs(context, attempt, nuda_dossier) + engine_call_inputs(
        context, result.get("engine_call")
    )
    validate_reading_payload(
        payload,
        outcome=outcome,
        fields=fields,
        protocol_config=attempt.protocol_config,
        protocol_sha256=attempt.protocol_sha256,
        inputs=reading_inputs,
    )
    context.publish(
        kind=nuda.LECTIO_NUDA_KIND,
        subject_id=attempt.act_id,
        outcome=outcome,
        attempt=perlector_attempt_id(attempt.act_id, "lectio-nuda", attempt.ordinal),
        inputs=reading_inputs + [approval_ref.reference.to_record()],
        payload=payload,
    )


def _publish_lectio_prior(
    context, attempt: _Attempt, dossier: dict[str, Any], result: dict[str, Any]
) -> dict:
    """Publish Pass A as a retained draft, never as a Perlectio."""
    prior_dossier, prompt, outcome, truncation_record, text = _publication_pass_data(
        attempt, dossier, result
    )
    prior_assessment, prior_spans, prior_gaps = _published_doubt(
        result,
        text=text,
        outcome=outcome,
        whole_act_gaps=_whole_act_gap([], {}),
    )
    payload = {
        "act_key": attempt.act_key,
        "attempt_ordinal": attempt.ordinal,
        "text": text,
        "dossier": prior_dossier,
        "prompt": prompt,
        "dissent": [],
        "truncation": truncation_record,
        "uncertain_spans": prior_spans,
        "uncertainty_assessment": prior_assessment,
        "gaps": prior_gaps,
        "provenance": provenance_for(
            context, attempt.chair, attempted=True, receipt_ref=attempt.receipt_ref
        ),
        "protocol": _protocol_record(context, attempt.protocol_config),
    }
    fields = with_engine_call(payload, result, _LECTIO_PRIOR_FIELDS)
    reading_inputs = _arm_image_inputs(context, attempt, prior_dossier) + engine_call_inputs(
        context, result.get("engine_call")
    )
    validate_reading_payload(
        payload,
        outcome=outcome,
        fields=fields,
        protocol_config=attempt.protocol_config,
        protocol_sha256=attempt.protocol_sha256,
        inputs=reading_inputs,
    )
    context.publish(
        kind="lectio-prior",
        subject_id=attempt.act_id,
        outcome=outcome,
        attempt=perlector_attempt_id(attempt.act_id, "lectio-prior", attempt.ordinal),
        inputs=reading_inputs,
        payload=payload,
    )
    prior_artifact_id = _attempt_artifact_id(
        attempt.act_id, "lectio-prior", "lectio-prior", attempt.ordinal
    )
    return {
        "reference": context.artifact_ref(PERLECTOR, "lectio-prior", prior_artifact_id),
        "text": text,
    }


def _publish_primed_without_prior(
    context,
    attempt: _Attempt,
    dossier: dict[str, Any],
    result: dict[str, Any],
    *,
    testimonia: list[dict],
    attachment_view: dict[str, Any],
    approval_ref: ApprovalRecordBinding,
) -> None:
    """The sampled control sees witnesses but never the Pass-A draft."""
    control_dossier, prompt, outcome, truncation_record, text = _publication_pass_data(
        attempt, dossier, result
    )
    testimonium_references = _testimonium_references(context, testimonia)
    # `context.run` was verified when opened and nothing rewrites `run.json`, so it is
    # not re-read per act.
    membership = context.run["corpus_frame_membership"]
    control_assessment, control_spans, control_gaps = _published_doubt(
        result,
        text=text,
        outcome=outcome,
        whole_act_gaps=_whole_act_gap(testimonia, testimonium_references),
    )
    payload = {
        "act_key": attempt.act_key,
        "attempt_ordinal": attempt.ordinal,
        "text": text,
        "basis": {
            "regions": attempt.bases,
            "testimonia": _testimonia_basis(testimonia, testimonium_references),
        },
        "dossier": control_dossier,
        "prompt": prompt,
        "sampling": protocol.control_sampling_design(
            per_mille=context.perlector_instrument_per_mille,
            selection_rule=attempt.protocol_config["selection_rule"],
            approval_ref=approval_ref,
        ),
        # The digest draw above is keyed by the logical act. Record that same
        # subject here; a local capture ID would make a clustered control's
        # retained membership impossible to reproduce from its own facts.
        "membership": {
            **membership,
            "act_id": control_dossier["logical_act_id"],
            "protocol_sha256": attempt.protocol_sha256,
        },
        "dissent": dissent_against(text, dissent_testimonia(testimonia, attachment_view)),
        "truncation": truncation_record,
        "uncertain_spans": control_spans,
        "uncertainty_assessment": control_assessment,
        "gaps": control_gaps,
        "provenance": provenance_for(
            context, attempt.chair, attempted=True, receipt_ref=attempt.receipt_ref
        ),
        "lectio_kind": "primed-without-prior",
        "protocol": _protocol_record(context, attempt.protocol_config),
    }
    fields = with_engine_call(payload, result, _PRIMED_WITHOUT_PRIOR_FIELDS)
    reading_inputs = (
        _arm_image_inputs(context, attempt, control_dossier)
        + list(testimonium_references.values())
        + [attachment_view["reference"]]
        + engine_call_inputs(context, result.get("engine_call"))
    )
    validate_reading_payload(
        payload,
        outcome=outcome,
        fields=fields,
        run_id=context.tree.run_id,
        config_digest=context.config_digest,
        protocol_config=attempt.protocol_config,
        protocol_sha256=attempt.protocol_sha256,
        inputs=reading_inputs,
    )
    context.publish(
        kind="primed-without-prior",
        subject_id=attempt.act_id,
        outcome=outcome,
        attempt=perlector_attempt_id(attempt.act_id, "primed-without-prior", attempt.ordinal),
        inputs=reading_inputs + [approval_ref.reference.to_record()],
        payload=payload,
    )


def _established_row(
    context,
    attempt: _Attempt,
    act: dict[str, Any],
    establishing: dict[str, Any],
    *,
    order: int,
    declared_failure: str | None,
    testimonia: list[dict],
    attachment_view: dict[str, Any],
    autopsia: dict[str, Any],
) -> dict[str, Any]:
    """The Pass-B Perlectio payload and everything the audit pass needs to finish it.

    Publication consumes the one establishing result; it never chooses or merges
    capture-local readings.
    """
    primed_dossier = _reseal_dossier(establishing["dossier"])
    result = establishing["result"]
    prior = primed_dossier["prior_draft"]
    # The prompt is reproduced from the retained dossier. In the withheld arm
    # `combined.py` removed the prior text before the call; the prompt builder
    # ignores a withheld prior, so both copies render the same bytes.
    prompt = prompts.prompt_evidence(
        attempt.chair, primed_dossier, attempt.protocol_config, attempt.protocol_sha256
    )
    # A declared failure is refused in live mode before any call.
    reading = "" if declared_failure == "no-readable-text" else result["text"]
    truncation_record = _reconciled_truncation(
        declared_failure=declared_failure,
        truncation_record=truncation.classify(
            reading,
            region_pixels=attempt.region_pixels,
            page_pixels=attempt.page_pixels,
            truncation_policy=attempt.protocol_config[protocol.TRUNCATION_TABLE],
            stop_reason=result["stop_reason"],
        ),
    )
    outcome = _resolve_outcome(
        declared_failure=declared_failure, truncation_record=truncation_record, text=reading
    )
    if outcome == "no-readable-text":
        # Whitespace resolved as unreadable is published as the empty text its schema
        # requires.
        reading = ""
    testimonium_references = _testimonium_references(context, testimonia)
    sealed_doubt, reader_spans, gaps = _published_doubt(
        result,
        text=reading,
        outcome=outcome,
        whole_act_gaps=_whole_act_gap(testimonia, testimonium_references),
    )
    provenance = provenance_for(
        context, attempt.chair, attempted=True, receipt_ref=attempt.receipt_ref
    )
    payload = {
        "act_key": act["act_key"],
        "attempt_ordinal": attempt.ordinal,
        "text": reading,
        "basis": {
            "regions": attempt.bases,
            "testimonia": _testimonia_basis(testimonia, testimonium_references),
        },
        "dossier": primed_dossier,
        "prompt": prompt,
        "dissent": dissent_against(reading, dissent_testimonia(testimonia, attachment_view)),
        "truncation": truncation_record,
        "uncertain_spans": reader_spans,
        "gaps": gaps,
        "uncertainty_assessment": sealed_doubt,
        "provenance": provenance,
        "lectio_kind": "primed-with-prior",
        "self_revision": departures(reading, prior["text"]),
        "protocol": _protocol_record(context, attempt.protocol_config),
    }
    return {
        "act": act,
        "act_id": attempt.act_id,
        "order": order,
        "bases": attempt.bases,
        "payload": payload,
        # The call the published text came from; the audit loop re-points it at the
        # re-proof's call when that text is published.
        "fields": with_engine_call(payload, result, _PERLECTIO_FIELDS),
        "outcome": outcome,
        # Areas, not decoded pixels: holding every act's images until the audit loop
        # would grow memory with the act count. A re-proof rebuilds its pixels from the
        # sealed artifacts.
        "region_pixels": attempt.region_pixels,
        "page_pixels": attempt.page_pixels,
        "declared_failure": declared_failure,
        "testimonia": testimonia,
        "attachment_view": attachment_view,
        "prior": prior,
        "autopsia": autopsia,
        "inputs": _reading_image_inputs(
            context, attempt.bases, attempt.page_renders, autopsia=autopsia
        )
        + list(testimonium_references.values())
        + [attachment_view["reference"], prior["reference"]]
        + engine_call_inputs(context, result.get("engine_call")),
    }


def _logical_sampling_decisions(context, logical_act_id: str) -> tuple[bool, bool]:
    """Choose instrument membership once for a logical act, never per capture."""
    nuda_sampled = nuda.is_nuda_sampled(
        logical_act_id,
        run_id=context.tree.run_id,
        nuda_per_mille=context.nuda_per_mille,
    )
    frame_membership = context.run["corpus_frame_membership"]
    control_sampled = protocol.is_control_sampled(
        logical_act_id,
        frame_digest=frame_membership["frame_digest"],
        page_digest=frame_membership["page_digest"],
        seed=frame_membership["seed"],
        per_mille=context.perlector_instrument_per_mille,
    )
    return nuda_sampled, control_sampled


def main(registry_factory=ChairRegistry.from_toml, serving_factory=None) -> int:
    """Run the pass, and guarantee any chair it started is stopped.

    Both parameters are test seams; neither decides which engine answers, which is the
    sealed serving-recipe row's business. `_read_the_acts` stops the chair before
    sealing; the `finally` covers a pass that raised first.

    `ChairResponseRefusal` is a `RuntimeError`, which `run_stage` does not catch, so it
    is re-raised as a `ContractError` to exit as a named refusal. `ChairRequestRefusal`
    is deliberately not caught: it means this code built a bad request, and a traceback
    at the construction site is worth more.
    """
    service = ResidentChair()
    try:
        return _read_the_acts(registry_factory, serving_factory, service)
    except ChairResponseRefusal as refusal:
        raise ContractError(f"{type(refusal).__name__}: {refusal}") from refusal
    finally:
        service.close()


def _read_the_acts(registry_factory, serving_factory, service: ResidentChair) -> int:
    """One Perlector pass: every requested act read once and published once."""
    parser = stage_parser(__doc__.splitlines()[0])
    parser.add_argument(
        "--reading-deadline",
        type=_utc,
        default=None,
        help="UTC time by which a live pass must finish reading; it refuses to start, or "
        "to read another act, when the planned calls would run past it",
    )
    args = parser.parse_args()
    context = open_stage_context(args, PERLECTOR, registry_factory=registry_factory)
    decoding_policy, decoding_sha256 = load_decoding_policy(args.decoding_config)
    context.require_sealed_config("decoding", decoding_sha256)
    # Resolved before anything is published or started, so they refuse on an untouched tree.
    chair = perlector_chair(context)
    serving_mode = perlector_serving_mode(context, args, chair)
    reader = fixture_reader_for(context, chair, serving_mode)
    witness_context_table = dossier_module.load_witness_context(
        Path(context.witness_context_config_path)
    )
    protocol_config, protocol_sha256 = protocol.load(context.perlector_protocol_config_path)
    context.require_sealed_config("perlector-protocol", protocol_sha256)
    nuda_approval = (
        resolve_sampling_approval(
            context,
            approval_ref=context.nuda_approval_ref,
            subject=NUDA_APPROVAL_SUBJECT,
        )
        if context.nuda_per_mille
        else None
    )
    instrument_approval = (
        resolve_sampling_approval(
            context,
            approval_ref=context.perlector_instrument_approval_ref,
            subject=PERLECTOR_INSTRUMENT_APPROVAL_SUBJECT,
        )
        if context.perlector_instrument_per_mille
        else None
    )
    audit_policy, audit_sha256 = audit.load(context.perlector_audit_config_path)
    context.require_sealed_config("perlector-audit", audit_sha256)

    expected = expected_acts(context)
    declared_order = {act["act_id"]: order for order, act in enumerate(expected)}
    # An attempt nobody requested would make the attempt tally meaningless.
    wanted = [act for act in expected if args.act in (None, act["act_id"])]
    if args.act and not wanted:
        raise ContractError(f"asked to read {args.act}, which the proposal seal does not name")
    preflight_testimonia_denominator(context, wanted)

    # Recovery may narrow `wanted`, but the partition denominator remains the
    # complete proposal seal for every invocation.
    partition, partition_ref = logical_reading.build_run_partition(context, expected)
    max_images = protocol_config.get("max_images")
    if not isinstance(max_images, int) or isinstance(max_images, bool):
        max_images = None

    read = 0
    acknowledged = 0
    resumed = 0
    pending: list[dict[str, Any]] = []
    # The routing denominator is every sealed proposal; `reported_unrouted` keeps one
    # finding from being restated by every act reaching the same page Testimonium.
    all_proposal_regions = sealed_proposal_regions(context)
    reported_unrouted: set[tuple[str, int]] = set()

    # A live chair starts on first use, so a resumed pass with every act sealed or over
    # capacity never loads a model on a card billed by the hour.
    receipt_ref: dict[str, str] | None = None

    calls_per_act = (
        2
        + audit_policy["round_cap"]
        + bool(context.nuda_per_mille)
        + bool(context.perlector_instrument_per_mille)
    )
    unread = _acts_left_to_read(context, wanted) if serving_mode == "live" else 0
    if unread:
        startup = (
            bound_serving_recipes(context, args.serving_recipes_config)
            .for_identity(chair, args.placement_tier)
            .startup_timeout_seconds
        )
        _refuse_past_deadline(
            args.reading_deadline,
            startup + unread * calls_per_act * PLANNED_SECONDS_PER_CALL,
            f"starting the Perlector ({startup}s) and reading {unread} acts",
        )

    for act in wanted:
        act_id = act["act_id"]
        if act["outcome"] == "held":
            # Reading part of an act would deliver a truncation as output; acknowledged
            # explicitly, never skipped, so no unit goes unaccounted.
            _publish_not_run(
                context,
                act_id=act_id,
                ordinal=1,
                fields=_NOT_RUN_HELD_FIELDS,
                payload={
                    "act_key": act["act_key"],
                    "attempt_ordinal": 1,
                    "reason": (
                        "the Designator held this act; an incomplete proposal is "
                        "not read, because a reading of part of an act would be a "
                        "truncation delivered as an output"
                    ),
                    "provenance": provenance_for(context, chair, attempted=False),
                },
            )
            acknowledged += 1
            continue

        # One region read answers both the attempt ordinal and the crops read, so
        # `_next_attempt` refuses an unplaceable origin before any immutable Perlectio
        # exists.
        regions, proposal_regions = act_regions(context, act_id)
        ordinal = _next_attempt(context, act_id, regions)
        if isinstance(chair, AbsentChair):
            # An explicit record of the absence: producing nothing would leave the Recensor
            # to infer a gap it cannot see.
            _publish_not_run(
                context,
                act_id=act_id,
                ordinal=ordinal,
                fields=_NOT_RUN_ABSENT_FIELDS,
                payload={
                    "act_key": act["act_key"],
                    "attempt_ordinal": ordinal,
                    "reason": f"the Perlector chair is explicitly absent: {chair.reason}",
                    "basis": {"regions": [], "testimonia": []},
                    "dissent": [],
                    "provenance": provenance_for(context, chair, attempted=False),
                },
            )
            acknowledged += 1
            continue

        if serving_mode == "live" and _reading_already_sealed(
            context, act_id, ordinal, act_key=act["act_key"]
        ):
            # Never asked again: a second live reading would differ and the store refuses
            # it (principle 4).
            resumed += 1
            continue

        # A declared engine outcome stands in for a real engine's report, so it is valid
        # only when no engine answers. A live pass over a declared act refuses here,
        # before any chair starts or arm is published (principle 8).
        declared_failure = declared_reading_failure(context, act["act_key"])
        if serving_mode == "live" and declared_failure is not None:
            raise ContractError(
                f"the fixture declares reading outcome {declared_failure!r} for "
                f"act {act['act_key']!r} while a live chair is answering; a "
                "declared stand-in cannot override an engine that reported"
            )

        # Every region is read, including a continuation on the next page: an act read
        # only up to the fold would be truncated, a failure and not an output.
        bases = [verify_region(context, region) for region in regions]
        testimonia = testimonia_of(context, act_id, proposal_regions)
        page_testimonia: dict[str, dict] = {}
        attachment_view = act_attachment_view(
            context,
            act,
            testimonia,
            bases,
            {region["payload"]["region_id"] for region in proposal_regions},
            page_testimonia_seen=page_testimonia,
            all_proposal_regions=all_proposal_regions,
        )
        # Both witness scopes use the run-wide proposal denominator. Deduplicate
        # page testimony so an observation is named once, not once per act.
        unrouted = unrouted_observations(
            testimonia + list(page_testimonia.values()),
            all_proposal_regions,
            prior_findings=reported_unrouted,
        )
        for finding in unrouted:
            reported_unrouted.add((finding["testimonium_id"], finding["ordinal"]))
            # Never normalized into the nearest act; the Recensor re-derives it independently.
            print(f"non-fatal finding: {finding}", file=sys.stderr)

        # Ink uncovered only by a recovery recrop was never shown to a witness;
        # recording that keeps the gap visible. The reading itself is unaffected.
        witnessed = witnessed_region_ids(testimonia, bases)
        for basis in bases:
            basis["witness_covered"] = basis["region_id"] in witnessed

        region_pixels = _region_pixels(bases)
        page_renders = _page_renders_for(context, bases)
        page_pixels = _page_pixels(page_renders)

        # Resolve the required capture set before any reader call so an absent
        # member cannot become a partial presentation.
        logical_act_id = logical_reading.logical_act_id_for(partition, act_id)
        autopsia = logical_reading.act_autopsia(
            context,
            logical_act_id=logical_act_id,
            partition_ref=partition_ref,
            act=act,
            bases=bases,
            page_renders=page_renders,
        )
        # Route capacity before building any arm: one oversized act must be held
        # without killing other acts or allowing the transport to chunk views.
        capacity_finding = over_capacity_reason(autopsia, max_images)
        if capacity_finding is not None:
            _publish_not_run(
                context,
                act_id=act_id,
                ordinal=ordinal,
                fields=_NOT_RUN_CAPACITY_FIELDS,
                inputs=_reading_image_inputs(context, bases, page_renders, autopsia=autopsia),
                payload={
                    "act_key": act["act_key"],
                    "attempt_ordinal": ordinal,
                    "reason": capacity_finding,
                    "basis": {"regions": [], "testimonia": []},
                    "dissent": [],
                    "provenance": provenance_for(context, chair, attempted=False),
                    "logical_act_id": logical_act_id,
                    "cross_capture_autopsia": autopsia,
                },
            )
            unread -= 1
            acknowledged += 1
            continue

        _refuse_past_deadline(
            args.reading_deadline,
            unread * calls_per_act * PLANNED_SECONDS_PER_CALL,
            f"reading the {unread} acts left",
        )
        unread -= 1
        if reader is None:
            reader, receipt_ref = _live_reader(
                context,
                args,
                chair=chair,
                protocol_config=protocol_config,
                decoding_policy=decoding_policy,
                decoding_sha256=decoding_sha256,
                serving_factory=serving_factory,
                service=service,
            )

        base_dossier = dossier_module.build_dossier(
            context,
            act_id=act_id,
            act_key=act["act_key"],
            regions=bases,
            testimonia=testimonia,
            regime=context.witness_context,
            page_renders=page_renders,
            witness_context=witness_context_table,
            act_attachment=attachment_view,
        )

        nuda_sampled, control_sampled = _logical_sampling_decisions(context, logical_act_id)

        attempt = _Attempt(
            act_key=act["act_key"],
            act_id=act_id,
            ordinal=ordinal,
            chair=chair,
            bases=bases,
            page_renders=page_renders,
            region_pixels=region_pixels,
            page_pixels=page_pixels,
            protocol_config=protocol_config,
            protocol_sha256=protocol_sha256,
            receipt_ref=receipt_ref,
        )
        # Runs before the establishing arm, which embeds the prior reference it returns.
        publish_prior = partial(_publish_lectio_prior, context, attempt)

        # Every arm receives the complete presentation in one reader call.
        try:
            passes = combined.run_logical_passes(
                reader,
                autopsia=autopsia,
                dossier=base_dossier,
                read_bytes=context.tree.read_bytes,
                protocol_config=protocol_config,
                nuda_sampled=nuda_sampled,
                control_sampled=control_sampled,
                draft_fed=context.draft_fed,
                publish_prior=publish_prior,
            )
        except _ACT_LOCAL_READING_FAILURES as error:
            failure = _failure_record(error, phase="establishing")
            assert failure is not None  # narrowed by the exception tuple above
            _publish_reading_failure(
                context,
                act_id=act_id,
                act_key=act["act_key"],
                ordinal=ordinal,
                inputs=_reading_image_inputs(context, bases, page_renders, autopsia=autopsia)
                + [
                    context.artifact_ref(ATTESTATORES, "testimonium", record["artifact_id"])
                    for record in testimonia
                ]
                + [attachment_view["reference"]]
                + _published_arm_refs(context, act_id, ordinal),
                failure=failure,
                reason=f"live Perlector {failure['kind']} failure: {failure['code']}",
                provenance=provenance_for(context, chair, attempted=True, receipt_ref=receipt_ref),
            )
            acknowledged += 1
            continue

        if nuda_sampled:
            _publish_lectio_nuda(
                context,
                attempt,
                passes["lectio-nuda"]["dossier"],
                passes["lectio-nuda"]["result"],
                nuda_approval,
            )

        if control_sampled:
            _publish_primed_without_prior(
                context,
                attempt,
                passes["primed-without-prior"]["dossier"],
                passes["primed-without-prior"]["result"],
                testimonia=testimonia,
                attachment_view=attachment_view,
                approval_ref=instrument_approval,
            )

        pending.append(
            _established_row(
                context,
                attempt,
                act,
                passes["perlectio"],
                order=declared_order[act_id],
                declared_failure=declared_failure,
                testimonia=testimonia,
                attachment_view=attachment_view,
                autopsia=autopsia,
            )
        )
        read += 1

    # All Pass-B semi-finals together, before any re-proof exists: one deterministic
    # cross-act computation per page, with no cascade.
    semi_finals = [
        semi_final
        for row in pending
        for semi_final in audit_semi_finals_for_pages(
            act_id=row["act_id"],
            order=row["order"],
            text=row["payload"]["text"],
            bases=row["bases"],
            dossier=row["payload"]["dossier"],
        )
    ]
    page_flags = _page_flags(
        context,
        semi_finals,
        expected=expected,
        recovery_act_id=args.act,
        protocol_config=protocol_config,
        protocol_sha256=protocol_sha256,
    )
    policy_record = audit.policy_record(audit_policy, audit_sha256)
    for row in pending:
        payload = row["payload"]
        act_id = row["act_id"]
        flags = page_flags[act_id]
        draft_payload, draft_ref = _publish_audit_draft(
            context, row, flags, round_cap=audit_policy["round_cap"], policy_record=policy_record
        )
        page_ids = draft_payload["page_ids"]
        final_text = payload["text"]
        # The truncation verdict on the re-proof call itself, measured before its text
        # is compared with the semi-final: a cut-off re-proof that returned the
        # established text verbatim must not pass as complete.
        reproof_truncation: dict[str, Any] | None = None
        # The one function `validate_chain` and `audit_request` also use, so the sealed,
        # delivered and recomputed plans are one computation.
        reproofs = audit.reproof_plan(
            flags, text_length=len(final_text), policy_schema=audit_policy["schema"]
        )
        request_digest: str | None = None
        changes: list[dict[str, Any]] = []
        reproof_edits: list[dict[str, Any]] | None = None
        payload_fields = row["fields"]
        # A re-proof is evidence whether or not it changed the text, so it is always
        # bound as an input; `engine_call` names it only when its text is published.
        reproof_inputs: list[dict[str, str]] = []
        reproof_call_record: dict[str, Any] | None = None
        # The predicate `validate_chain` re-derives from the frozen draft.
        if audit.reproof_delivery_due(flags, audit_policy["round_cap"]):
            base_prompt_record = copy.deepcopy(payload["prompt"])
            base_prompt_text = prompts.build_prompt(
                chair.serving_recipe, chair.role, payload["dossier"], protocol_config
            )
            # One reader call per act and round over the complete atomic presentation; a
            # flagged-page subset would be a capture-local call.
            reproof_pixels = atomic_delivered_pixels(
                row["autopsia"], read_bytes=context.tree.read_bytes, max_images=max_images
            )
            # The request carries the frozen semi-final its locations index into, bound by
            # the published draft's digest; the dossier goes through untouched so its
            # `dossier_digest` still covers it.
            audit_request = audit.audit_request(
                act_key=row["act"]["act_key"],
                attempt_ordinal=payload["attempt_ordinal"],
                draft_ref=draft_ref,
                semi_final_text=payload["text"],
                flags=flags,
                policy_schema=audit_policy["schema"],
            )
            request_digest = audit.audit_digest(audit_request)
            # Enforced by the producer so it binds whichever reader sits in the chair.
            validate_audit_delivery(
                payload["dossier"], pass_kind="audit-reproof", audit_request=audit_request
            )
            _refuse_past_deadline(
                args.reading_deadline, PLANNED_SECONDS_PER_CALL, "the next re-proof call"
            )
            try:
                reproof = reader.read(
                    payload["dossier"],
                    pass_kind="audit-reproof",
                    delivered_pixels=reproof_pixels,
                    audit_request=copy.deepcopy(audit_request),
                )
            except _ACT_LOCAL_READING_FAILURES as error:
                failure = _failure_record(error, phase="audit-reproof")
                assert failure is not None
                _publish_reading_failure(
                    context,
                    act_id=act_id,
                    act_key=row["act"]["act_key"],
                    ordinal=payload["attempt_ordinal"],
                    inputs=row["inputs"]
                    + [draft_ref]
                    + _published_arm_refs(context, act_id, payload["attempt_ordinal"]),
                    failure=failure,
                    reason=f"live Perlector {failure['kind']} failure during audit re-proof: {failure['code']}",
                    provenance=payload["provenance"],
                )
                continue
            # A Pass-C reply is exact draft-anchored edits, never a second whole reading;
            # assembly validates every edit before it touches the established text.
            try:
                final_text, reproof_response = audit.assemble_reproof_response(
                    reproof["text"], audit_request
                )
                # Publishing whitespace as the empty reading would remove characters
                # outside the exact edits the response accounted for.
                if _publishes_empty(final_text) and final_text not in {"", payload["text"]}:
                    raise audit.ReproofResponseRefusal(
                        "an audit re-proof response becomes a whole-act empty projection outside "
                        "its exact requested edits"
                    )
                reproof_edits = copy.deepcopy(reproof_response["edits"])
                reproof_call_record = _reproof_call(
                    reproof,
                    base_prompt=base_prompt_record,
                    base_text=base_prompt_text,
                    request=audit_request,
                )
            except audit.ReproofResponseRefusal as error:
                _publish_reading_failure(
                    context,
                    act_id=act_id,
                    act_key=row["act"]["act_key"],
                    ordinal=payload["attempt_ordinal"],
                    inputs=row["inputs"]
                    + [draft_ref]
                    + engine_call_inputs(context, reproof.get("engine_call"))
                    + _published_arm_refs(context, act_id, payload["attempt_ordinal"]),
                    failure=_failure_from_engine_call(
                        context, reproof.get("engine_call"), detail=str(error)
                    ),
                    reason="the delivered audit re-proof response could not be assembled safely",
                    provenance=payload["provenance"],
                )
                continue
            reproof_inputs = engine_call_inputs(context, reproof.get("engine_call"))
            reproof_truncation = _row_truncation(
                row, final_text, stop_reason=reproof["stop_reason"], protocol_config=protocol_config
            )
            # The edits are already checked; this binds the accepted text to its
            # producing call. Unchanged text keeps Pass B's provenance.
            if final_text != payload["text"]:
                payload["text"] = final_text
                # The doubt report travels with the call whose text is published, so
                # nothing is re-anchored by guesswork; this also drops a Pass-B whole-act gap.
                reproof_assessment = _assessed(reproof, text=final_text)
                if "[[" in final_text or "]]" in final_text:
                    reproof_assessment = annotations.malformed_assessment(
                        "a re-proof replacement carries a doubt mark its JSON answer cannot anchor"
                    )
                elif reproof_assessment["state"] != "assessed" and (
                    payload["gaps"] or payload["uncertain_spans"]
                ):
                    reproof_assessment = annotations.malformed_assessment(
                        "a re-proof replaced text over which Pass B marked doubts; those marks "
                        "stay in Pass B's retained response and cannot be re-anchored here"
                    )
                payload["uncertainty_assessment"] = _sealed_assessment(reproof_assessment)
                payload["uncertain_spans"] = list(reproof_assessment["uncertain_spans"])
                payload["gaps"] = list(reproof_assessment["gaps"])
                # `engine_call` moves with the text, so a published reading never names
                # a response that did not produce it.
                payload_fields = with_engine_call(payload, reproof, payload_fields)
                if reproof_call_record is not None:
                    payload["prompt"] = copy.deepcopy(reproof_call_record["audit_prompt"])
                payload["dissent"] = dissent_against(
                    final_text, dissent_testimonia(row["testimonia"], row["attachment_view"])
                )
                payload["self_revision"] = departures(final_text, row["prior"]["text"])
                # Re-measured over the published text with the re-proof's own stop
                # reason; `_audited_truncation` never lets Pass C improve the verdict.
                payload["truncation"] = _audited_truncation(
                    pass_b=payload["truncation"],
                    declared_failure=row["declared_failure"],
                    text=final_text,
                    region_pixels=row["region_pixels"],
                    page_pixels=row["page_pixels"],
                    truncation_policy=protocol_config[protocol.TRUNCATION_TABLE],
                    stop_reason=reproof["stop_reason"],
                    measured=reproof_truncation,
                )
                row["outcome"] = _resolve_outcome(
                    declared_failure=row["declared_failure"],
                    truncation_record=payload["truncation"],
                    text=final_text,
                )
                if row["outcome"] == "no-readable-text":
                    # As on the Pass-B path, unreadable text is published empty, with
                    # the whole-act gap carrying the witness evidence.
                    final_text = ""
                    payload["text"] = ""
                    # `validate_finding` binds the sealed termination to the published
                    # text, so it is re-measured over the emptied text.
                    reproof_truncation = _row_truncation(
                        row,
                        final_text,
                        stop_reason=reproof["stop_reason"],
                        protocol_config=protocol_config,
                    )
                    # Re-asked against the empty text, so the record never says
                    # `assessed` over a report that was thrown away.
                    (
                        payload["uncertainty_assessment"],
                        payload["uncertain_spans"],
                        payload["gaps"],
                    ) = _published_doubt(
                        reproof,
                        text="",
                        outcome="no-readable-text",
                        whole_act_gaps=_whole_act_gap(
                            row["testimonia"], _testimonium_references(context, row["testimonia"])
                        ),
                    )
                    if reproof_assessment["state"] == "malformed":
                        payload["uncertainty_assessment"] = _sealed_assessment(reproof_assessment)
                    payload["dissent"] = dissent_against(
                        "", dissent_testimonia(row["testimonia"], row["attachment_view"])
                    )
                    payload["self_revision"] = departures("", row["prior"]["text"])
            # Record the exact validated edits; downstream validation replays
            # them against the frozen draft and the published text.
            changes = audit.change_records_from_edits(reproof_edits)
        # Only an exhausted cap mints spans; an incomplete re-proof is recorded as an
        # incomplete examination for the Recensor to route, never as a span or truncation.
        examination = audit.examination_state(
            flags,
            audit_policy["round_cap"],
            reproof_truncation,
        )
        unresolved = audit.unresolved_state(examination)
        uncertainty = (
            _cap_exhausted_spans(flags) if examination == audit.EXAMINATION_CAP_EXHAUSTED else []
        )
        finding_payload = {
            "act_key": row["act"]["act_key"],
            "attempt_ordinal": payload["attempt_ordinal"],
            "page_ids": page_ids,
            "round_cap": audit_policy["round_cap"],
            "policy": policy_record,
            "flags": flags,
            "change_record": changes,
            "uncertain_spans": uncertainty,
            "unresolved": unresolved,
            "examination": examination,
            "reproof_truncation": reproof_truncation,
            "reproof_edits": reproof_edits,
            # Named here because the Perlectio's `engine_call` stays Pass B's when the
            # text is unchanged.
            "reproof_call": reproof_call_record if reproof_truncation is not None else None,
        }
        audit.validate_finding(
            finding_payload,
            # The text this act actually publishes, never a rejected rewrite.
            text=payload["text"],
            flag_text=draft_payload["semi_final_text"],
        )
        finding = context.publish(
            kind="audit-finding",
            subject_id=act_id,
            outcome="read",
            attempt=perlector_attempt_id(act_id, "perlegere", payload["attempt_ordinal"]),
            inputs=[draft_ref],
            payload=finding_payload,
        )
        finding_ref = context.input_ref(finding.relative_path)
        # An unresolved flag never stays a clean `read`: it becomes an explicit span,
        # unlabelled; only `payload["audit"]` and the finding say which are the audit's.
        payload["uncertain_spans"] = _union_with_projection(
            [
                {
                    "start": span["start"],
                    "end": span["end"],
                    "alternatives": [],
                    "confidence": "low",
                }
                for span in uncertainty
            ],
            payload["uncertain_spans"],
        )
        payload["audit"] = {
            "draft_ref": draft_ref,
            "finding_ref": finding_ref,
            "finding_digest": audit.audit_digest(finding_payload),
            "unresolved": unresolved,
            "examination": examination,
            "reproofs": reproofs,
            # The request actually delivered, or `None` when nothing needed re-proof or
            # the cap was spent; `reproofs` alone cannot tell these apart.
            "request_digest": request_digest,
        }
        # Dedup only the re-proof's references, which can repeat the establishing call's
        # bytes; a duplicate in `row["inputs"]` stays under the envelope's refusal.
        reading_inputs = (
            row["inputs"]
            + [
                reference
                for reference in _distinct_inputs(reproof_inputs)
                if reference not in row["inputs"]
            ]
            + [
                draft_ref,
                finding_ref,
            ]
        )
        # The consumers' own cross-record validation, run before publication so a
        # drifted draft/finding relationship never becomes an unreadable artifact.
        audit.validate_chain(
            context.tree,
            {"payload": payload, "inputs": reading_inputs},
            act_id,
            length_floor_characters_per_page=_sealed_length_floor(protocol_config),
        )
        validate_reading_payload(
            payload,
            outcome=row["outcome"],
            fields=payload_fields,
            run_id=context.tree.run_id,
            config_digest=context.config_digest,
            protocol_config=protocol_config,
            protocol_sha256=protocol_sha256,
            inputs=reading_inputs,
        )
        context.publish(
            kind="perlectio",
            subject_id=act_id,
            outcome=row["outcome"],
            attempt=perlector_attempt_id(act_id, "perlegere", payload["attempt_ordinal"]),
            inputs=reading_inputs,
            payload=payload,
        )

    if read == 0 and acknowledged == 0 and resumed == 0:
        raise ContractError("the Perlector read no act and acknowledged no held act")

    # Before the seal, so a failed shutdown is never reported over a sealed stage;
    # `close` is idempotent with `main`'s `finally`.
    service.close()
    context.seal_boundary()
    context.finish()
    return EXIT_COMPLETE


def _next_attempt(context, act_id: str, regions: list[dict]) -> int:
    """Which reading attempt this is, derived from the act rather than from history.

    One reading of the proposal plus one per recovery region cut since, so a rerun that
    changed nothing recomputes the same ordinal and reuses the same bytes. Witness
    testimony is not counted: a Testimonium primes a reading and never makes a new
    attempt (principles 1, 7).

    Counted by the shared `recovery_region_count`: the Recensor, Archetypus and Armarium
    each require an act's reading count to equal its recovery crops plus one, and it
    refuses an unknown origin before any model call. A resume therefore recomputes this
    ordinal rather than appending. `_read_the_acts` handles a live chair's
    non-reproducible bytes; any remaining collision is refused (`IncompatibleReuse`)
    rather than overwriting evidence.
    """
    return recovery_region_count(act_id, regions) + 1


if __name__ == "__main__":
    raise SystemExit(run_stage(main))
