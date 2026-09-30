"""Perlector: reads the ink, with the testimonia as fallible clues.

The record verifies its region evidence (digest against the sealed reference, decoded
size against the claimed transform), records every region and testimonium it saw by
reference, and never counts witnesses: dissent is computed after the reading is fixed
and cannot reach back into it.

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
from collections import deque
from collections.abc import Mapping
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime, timezone
from functools import partial
from pathlib import Path
from typing import Any, Final

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import audit  # noqa: E402
import combined  # noqa: E402
import dossier as dossier_module  # noqa: E402
import logical_reading  # noqa: E402
import nuda  # noqa: E402
import page_run  # noqa: E402
import prompts  # noqa: E402
import protocol  # noqa: E402
import regime  # noqa: E402
from dissent import departures, dissent_against, validate_dissent  # noqa: E402
from live_reader import EngineSignalRefusal, VLLMReader  # noqa: E402
from reader import FixtureReader, validate_audit_delivery  # noqa: E402
from throughput import PLANNED_SECONDS_PER_CALL  # noqa: E402

import operations.serving.errors as serving_errors  # noqa: E402
from common import reading_annotations as annotations  # noqa: E402
from common import truncation  # noqa: E402
from common.alignment import bracket_marker_view, markup_text_view  # noqa: E402
from common.chairs.models import AbsentChair, ChairIdentity  # noqa: E402
from common.chairs.registry import ChairRegistry  # noqa: E402
from common.chandra_native_retry import validate_trace as validate_chandra_trace  # noqa: E402
from common.contracts.approval import (  # noqa: E402
    ApprovalRecordBinding,
    ApprovalRecordReference,
    validate_approval_record,
)
from common.contracts.canonical import (  # noqa: E402
    canonical_bytes,
    digest_bytes,
    digest_of,
    is_sha256,
)
from common.contracts.envelope import (  # noqa: E402
    build_envelope,
    digest_ref,
    validate_input_refs,
)
from common.contracts.errors import (  # noqa: E402
    ApprovalRefusal,
    ContractError,
    FatalAccounting,
    SchemaRefusal,
)
from common.contracts.identities import artifact_id, perlector_attempt_id  # noqa: E402
from common.contracts.identities import attempt_id as derived_attempt_id  # noqa: E402
from common.contracts.outcomes import ATTACHMENT_BASES, page_attachment_basis  # noqa: E402
from common.contracts.prior_draft import (  # noqa: E402
    BLIND_READ_MODES,
    kind_for_view,
    refuse_removed_draft_fed,
    self_revision_for_view,
    validate_establishing_view,
)
from common.contracts.serving import (  # noqa: E402
    CHAIR_CALL_RECORD_SCHEMA,
    CHAIR_TRANSPORT_FAILURE_RECORD_SCHEMA,
)
from common.contracts.stages import (  # noqa: E402
    ATTESTATORES,
    DESIGNATOR,
    PERLECTOR,
    writing_directory,
)
from common.corpus_register import refuse_capture_preference  # noqa: E402
from common.cross_capture_autopsia import (  # noqa: E402
    atomic_delivered_pixels,
    over_capacity_reason,
    presented_image_sha256s,
    validate_autopsia,
)
from common.decoding import (  # noqa: E402
    VARIANCE_ARMS,
    load_decoding_policy,
    perlector_max_tokens,
    perlector_page_max_tokens,
    refuse_retired_call_record,
)
from common.image_sniff import PNG_SIGNATURE  # noqa: E402
from common.native_witness import (  # noqa: E402
    reported_geometry_overlaps,
    unpresented_region_ids,
    unrouted_observations,
    validate_native_witness_geometry,
)
from common.page_path import distinct_refs, refs_by_path  # noqa: E402
from common.page_testimonia import (  # noqa: E402
    declared_page_witness_chairs,
    input_order,
    sealed_proposal_regions,
    validate_page_testimonium_record,
    validate_presented_page,
    verify_page_native_capture,
    verify_region,
)
from common.perlector_failure import (  # noqa: E402
    PRE_PERLECTIO_ARTIFACTS,
    validate_failed_payload,
    validate_failed_perlectio,
)
from common.physical_act_partition import CROSS_CAPTURE_READ_NOT_BUILT  # noqa: E402
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
    verify_retained_call_sampling,
)
from operations.serving.assembly import (  # noqa: E402
    bound_serving_recipes,
    stage_chair_client,
)
from operations.serving.client import ChairClient, serving_mode_for  # noqa: E402
from operations.serving.errors import ChairResponseRefusal  # noqa: E402
from operations.serving.http import EndpointUnavailable  # noqa: E402

DESCRIPTION = "Perlector: reads the ink, with the testimonia as fallible clues."

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
    if not is_sha256(digest):
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
    validate_presented_page(context, payload, [presented])
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
    expected_inputs = refs_by_path(distinct_refs(input_references))
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


def testimonia_of(context, act_id: str, proposal_regions: list[dict]) -> list[dict]:
    """Every chair's current testimonium for this act: the latest attempt only.

    Attempts are append-only. Every record is validated, but only each
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
        verify_page_native_capture(context, f"act {act_id}", chair, testimonium, native_capture)
    # Sealed proposal geometry only, as the writer used: a recovery crop postdates
    # testimony, so it may not enlarge the denominator that attached it.
    page_bases = [
        basis
        for basis in bases
        if basis["source_page_ordinal"] == attachment_page
        and basis["region_id"] in proposal_region_ids
    ]
    # Native page and compatibility act outcomes are independent; legacy
    # page joins instead derive their outcome from the act attempts. A page
    # read one record at a time is a native page reading too.
    attachment_outcome = (
        testimonium["outcome"]
        if native_capture is not None or "presentations" in page_payload
        else chair_testimonium["outcome"]
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
    _check_page_testimonium_matches(act, attachment, page_payload)
    # The exact label is evidence about independence: `anchor-line` says the chair
    # counts only because another chair's anchor located its text.
    if attachment["attached"] and attachment["attachment_basis"] != derived_basis:
        raise SchemaRefusal(
            f"act {act_id} page attachment for chair {chair!r} names basis "
            f"{attachment['attachment_basis']!r}, but its own retained evidence "
            f"attached it by {derived_basis!r}"
        )
    return _page_comparison_view(act_id, attachment, page_payload), deltas


def _check_page_testimonium_matches(
    act: dict[str, Any], attachment: dict[str, Any], page_payload: dict[str, Any]
) -> None:
    """The page Testimonium is this chair's, for this page, in a role the act allows."""
    act_id = act["act_id"]
    chair = attachment["chair"]
    attachment_page = attachment["page_ordinal"]
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
    # Joining only proves the bytes arrived: a joined response may still be unaligned,
    # but an omitted one can never attach.
    if (
        attachment["attached"]
        and current_unjoined
        and current_unjoined[0]["outcome"] not in WITNESS_READING_OUTCOMES
    ):
        raise SchemaRefusal(
            f"act {act_id} attachment disagrees with its page Testimonium's unjoined-attempt record"
        )


def _is_explicit_unaligned(alignment: Any) -> bool:
    """An unaligned result with a reason, so the operator can tell why comparison failed."""
    return (
        isinstance(alignment, dict)
        and set(alignment) == {"status", "reason"}
        and alignment.get("status") == "unaligned"
        and isinstance(alignment["reason"], str)
        and bool(alignment["reason"].strip())
    )


def _page_comparison_view(
    act_id: str, attachment: dict[str, Any], page_payload: dict[str, Any]
) -> str | None:
    """The act's slice of an attached, aligned page reading, once its alignment is proven."""
    chair, span, alignment = attachment["chair"], attachment["span"], attachment["alignment"]
    aligned = isinstance(alignment, dict) and alignment.get("status") == "aligned"
    view = None
    if attachment["attached"]:
        if aligned:
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
            if not isinstance(page_text, str):
                raise SchemaRefusal("an attached page witness has no textual comparison view")
            view = act_comparison_view(page_text, alignment["witness_span"])
        elif span is not None or not _is_explicit_unaligned(alignment):
            raise SchemaRefusal("a geometrically attached page witness has no explicit span limit")
    elif aligned:
        # Text alignment cannot authorize a geometric attachment or comparison view.
        if span is not None:
            raise SchemaRefusal("an unattached page witness claims a comparison span")
    elif not _is_explicit_unaligned(alignment):
        raise SchemaRefusal("an unattached page witness has no explicit unaligned result")
    # Geometry alone cannot satisfy the witness floor: the act also needs an aligned
    # slice of retained page text, re-derived so it cannot be claimed by assertion.
    if attachment["comparable"] != (attachment["attached"] and aligned):
        raise SchemaRefusal(
            f"act {act_id} page attachment for chair {chair!r} claims a comparability "
            "its own recorded alignment does not support. The witness floor could count "
            "text that was never placed in this act. Rebuild comparability from the "
            "referenced page Testimonium and alignment."
        )
    return view


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
    # Comparability comes from the Testimonium's own text; structured reports are uncountable.
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

    Dissent gets a copy; the retained Testimonium stays verbatim.

    * A page witness gets this act's anchored, markup-stripped slice of its page
      reading from `act_attachment_view`; without one, `dissent_against` reports it
      unaligned.
    * An act-scoped chair whose format declares `can_express_uncertainty` gets its text
      with the bracket markers removed (`bracket_marker_view`). Otherwise
      `dissent.is_comparable` refuses to diff it and that chair's dissent goes dark.

    The capability says a chair can mark uncertainty, not which notation it uses. The
    bracket view fits the only act-scoped chair bound today, but another chair's markers
    would survive and read as disagreement.

    Every view derives from the chair's own retained bytes, after the reading is fixed.
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
    it is made. `receipt_ref` is the live chair's own receipt, passed only
    in live mode: a fixture receipt beside a real engine's reading would put a declared
    value where a measurement belongs.
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


def engine_call_inputs(
    context, engine_call: dict[str, Any] | None, *, variance_arm: str | None
) -> list[dict[str, str]]:
    """Bind the two blobs a live reading's record names as direct inputs.

    A fixture reading has no `engine_call` and adds nothing. Each reference is
    re-derived from the bytes on disk, so a record cannot name a response that is
    absent or has changed, and the call record is held to the Perlector's sealed
    sampling row and to its seed: `variance_arm`'s for a sampling-variance arm,
    otherwise the serving receipt's.
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
    call = _json_object(context.tree.read_bytes(engine_call["call_record_ref"]["relative_path"]))
    if call is None:
        raise SchemaRefusal("a live reading's call record is not a JSON object")
    try:
        verify_retained_call_sampling(context, call, "perlector", variance_arm=variance_arm)
    except ContractError as error:
        raise SchemaRefusal(
            f"a live reading's call record is not its sealed request: {error}"
        ) from error
    return references


def _start_chair(run: "_Pass") -> None:
    """Start this run's one chair and keep its receipt reference on the pass.

    Every record the pass publishes names the receipt of the service that answered,
    which `ChairClient.__enter__` has checked names this chair and revision.
    """
    # Assigned before entering so `close` covers any failure from here on; closing an
    # unstarted client is a no-op.
    run.service.client = run.client_factory(run.context, run.chair, run.args.placement_tier)
    run.service.client.__enter__()
    run.receipt_ref = dict(run.service.client.handle.receipt_reference)


def _start_live_reader(run: "_Pass") -> None:
    """Start the chair and keep the act reader that reads through it on the pass."""
    _start_chair(run)
    run.reader = VLLMReader(
        client=run.service.client,
        chair=run.chair,
        protocol_config=run.protocol_config,
        max_tokens=run.reading_max_tokens,
        reproof_max_tokens=run.reproof_max_tokens,
    )


def _attempt_artifact_id(act_id: str, kind: str, operation: str, ordinal: int) -> str:
    return artifact_id(PERLECTOR, kind, act_id, perlector_attempt_id(act_id, operation, ordinal))


def _reading_already_sealed(context, act_id: str, ordinal: int, *, act_key: str) -> bool:
    """Whether this run tree already holds this act's Perlectio at this ordinal.

    A failed Perlectio is validated before the chair is skipped, so malformed failure
    bytes cannot become a permanent resume bypass.
    """
    identifier = _attempt_artifact_id(act_id, "perlectio", "perlegere", ordinal)
    if not context.tree.resolve(
        context.tree.artifact_path(PERLECTOR, "perlectio", identifier)
    ).exists():
        return False
    reading = context.tree.read_artifact(PERLECTOR, "perlectio", identifier)
    if reading["outcome"] == "failed" and "failure" in reading["payload"]:
        validate_failed_perlectio(context, reading, act_id, expected_act_key=act_key)
    elif (
        reading["outcome"] not in ("failed", "not-run")
        and _semi_final_record(context, act_id, ordinal) is None
    ):
        # Only live passes skip sealed acts, and every live reading has its semi-final;
        # without it the page flags a resume computes would miss this act.
        raise FatalAccounting(
            f"act {act_key!r} has a live Perlectio but no semi-final, so a resumed audit "
            "cannot compute its page's flags over every act; read this page in a new run"
        )
    return True


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


# A live pass records each act's main-pass result as a `semi-final` the moment it is
# published, and every send of a reader call as a `reader-sent` record before the call
# leaves, so a resumed pass can tell a received reply from a call that never answered.
SEMI_FINAL_KIND: Final = "semi-final"
SENT_KIND: Final = "reader-sent"
SENT_SCHEMA: Final = "perlector-reader-sent.v1"
_SENT_FIELDS: Final = frozenset(
    {
        "schema",
        "act_key",
        "attempt_ordinal",
        "pass",
        "send",
        "receipt_ref",
        "concurrency",
        "image_sha256s",
    }
)
READING_PASS: Final = "reading"
REPROOF_PASS: Final = "audit-reproof"


def _semi_final_id(act_id: str, ordinal: int) -> str:
    return _attempt_artifact_id(act_id, SEMI_FINAL_KIND, "perlegere", ordinal)


def _semi_final_record(context, act_id: str, ordinal: int) -> dict[str, Any] | None:
    identifier = _semi_final_id(act_id, ordinal)
    if not context.tree.has_artifact(PERLECTOR, SEMI_FINAL_KIND, identifier):
        return None
    return context.tree.read_artifact(PERLECTOR, SEMI_FINAL_KIND, identifier)


def _sent_id(act_id: str, ordinal: int, pass_name: str, send: int) -> str:
    return artifact_id(
        PERLECTOR,
        SENT_KIND,
        act_id,
        derived_attempt_id(act_id, f"{pass_name}-sent:{ordinal}", send),
    )


def _validate_sent(payload: Any, *, act_key: str, ordinal: int, pass_name: str, send: int) -> None:
    if (
        not isinstance(payload, dict)
        or set(payload) != _SENT_FIELDS
        or payload["schema"] != SENT_SCHEMA
        or (payload["act_key"], payload["attempt_ordinal"], payload["pass"], payload["send"])
        != (act_key, ordinal, pass_name, send)
        or not isinstance(payload["receipt_ref"], dict)
        or type(payload["concurrency"]) is not int
        or payload["concurrency"] < 1
        or not isinstance(payload["image_sha256s"], list)
        or not all(isinstance(value, str) for value in payload["image_sha256s"])
    ):
        raise SchemaRefusal(f"a reader-sent record for act {act_key!r} is not its closed schema")


def _sent_records(
    context, act_id: str, act_key: str, ordinal: int, pass_name: str
) -> list[dict[str, Any]]:
    """Every send of one pass of this attempt, numbered 1..N, in order."""
    records: list[dict[str, Any]] = []
    while True:
        identifier = _sent_id(act_id, ordinal, pass_name, len(records) + 1)
        if not context.tree.has_artifact(PERLECTOR, SENT_KIND, identifier):
            return records
        record = context.tree.read_artifact(PERLECTOR, SENT_KIND, identifier)
        _validate_sent(
            record["payload"],
            act_key=act_key,
            ordinal=ordinal,
            pass_name=pass_name,
            send=len(records) + 1,
        )
        records.append(record)


def _sent_refs(context, act_id: str, act_key: str, ordinal: int, pass_name: str):
    return [
        context.artifact_ref(PERLECTOR, SENT_KIND, record["artifact_id"])
        for record in _sent_records(context, act_id, act_key, ordinal, pass_name)
    ]


def _publish_sent(
    run: "_Pass",
    act_id: str,
    act_key: str,
    ordinal: int,
    pass_name: str,
    image_sha256s: list[str],
) -> None:
    """Record, before it leaves, that this pass of the act is being sent, and under what.

    A second send names the first, so a call re-sent after an interruption is visible.
    `concurrency` is the width of the window the call was sent in: how many reader calls
    this pass kept in flight at most, which bounds the batch the engine decoded it in.
    `image_sha256s` are the images the call carries, in the order it sends them. The
    page path records a page's send the same way, the page id standing for the act.
    """
    earlier = _sent_refs(run.context, act_id, act_key, ordinal, pass_name)
    send = len(earlier) + 1
    run.context.publish(
        kind=SENT_KIND,
        subject_id=act_id,
        outcome="read",
        attempt=derived_attempt_id(act_id, f"{pass_name}-sent:{ordinal}", send),
        inputs=earlier + [dict(run.receipt_ref)],
        payload={
            "schema": SENT_SCHEMA,
            "act_key": act_key,
            "attempt_ordinal": ordinal,
            "pass": pass_name,
            "send": send,
            "receipt_ref": dict(run.receipt_ref),
            "concurrency": run.concurrency,
            "image_sha256s": list(image_sha256s),
        },
    )


# Blobs the serving manager keeps in a stage's own store beside the chair's calls.
_SERVING_BLOB_SCHEMAS: Final = frozenset({"serving-launch-audit.v1", "serving-evidence.v1"})


def _json_object(data: bytes) -> dict[str, Any] | None:
    try:
        value = json.loads(data)
    except (ValueError, UnicodeDecodeError):
        return None
    return value if isinstance(value, dict) else None


def _unrecorded_replies(context) -> tuple[list[dict[str, Any]], bool]:
    """Every retained reply no record of this stage binds, as far as it can be attributed.

    Returns the unbound call records that carry a reply, and whether any retained blob is
    a reply that cannot be attributed at all. The client retains a reply's raw bytes
    before the call record that names them, so a pass stopped between the two leaves
    bytes no call record names: every blob that is not a call record, a reply one names,
    serving evidence, a page render, or an input of some record is counted as such a
    reply.
    """
    manifest = context.tree.build_manifest(PERLECTOR)
    bound = {
        reference["relative_path"]
        for entry in manifest["artifacts"]
        for reference in context.tree.read_artifact(PERLECTOR, entry["kind"], entry["artifact_id"])[
            "inputs"
        ]
    }
    calls, named, others = [], set(), []
    for name in manifest["blobs"]:
        path = context.tree.blob_path(PERLECTOR, name)
        data = context.tree.read_bytes(path)
        if data.startswith(PNG_SIGNATURE):
            # A page render this stage cut for a reader call; a chat endpoint's reply is
            # never an image.
            continue
        record = _json_object(data)
        schema = record.get("schema") if record is not None else None
        # A call record from before the decoding bump is refused by its name, not
        # counted as a reply no record binds.
        refuse_retired_call_record(schema, subject=f"retained blob {path}")
        if schema in {CHAIR_CALL_RECORD_SCHEMA, CHAIR_TRANSPORT_FAILURE_RECORD_SCHEMA}:
            reply = record.get("raw_response_ref")
            if reply is not None:
                named.add(reply["relative_path"])
                if path not in bound:
                    calls.append(record)
        elif schema not in _SERVING_BLOB_SCHEMAS:
            others.append(path)
    unattributed = any(path not in bound and path not in named for path in others)
    return calls, unattributed


def _answers_a_send(calls: list[dict[str, Any]], markers: list[dict[str, Any]]) -> bool:
    """Whether an unbound reply came from one of these sends' sessions, about these images."""
    sent = [
        (record["payload"]["receipt_ref"], record["payload"]["image_sha256s"]) for record in markers
    ]
    return any((call.get("receipt_ref"), call.get("image_sha256s")) in sent for call in calls)


def _acts_left_to_read(context, wanted: list[dict[str, Any]]) -> int:
    """Count the acts a live pass still has to read, refusing any it cannot resume.

    An act with a Perlectio is sealed. An act with a `semi-final` has its main-pass reply
    on record and is adopted, never asked again; only its re-proof, if due, is still to
    send. Any other act must be untouched, or hold only sends whose replies never
    arrived: those are sent again, and the new send names the old. The pass refuses
    before any chair starts if an act holds records of a reply its resume cannot adopt
    (published between two records of one act, or retained but named by no record),
    because asking again would read it twice; those records stay as its evidence.
    """
    left, half_read, unrecorded = 0, [], []
    replies = None
    for act in wanted:
        if act["outcome"] == "held":
            continue
        act_id, act_key = act["act_id"], act["act_key"]
        ordinal = _next_attempt(context, act_id, act_regions(context, act_id)[0])
        if _reading_already_sealed(context, act_id, ordinal, act_key=act_key):
            continue
        present = {kind for kind, _identifier in _present_arms(context, act_id, ordinal)}
        adopted = _semi_final_record(context, act_id, ordinal) is not None
        if present & {"audit-draft", "audit-finding"} or (present and not adopted):
            half_read.append(act_key)
            continue
        pass_name = REPROOF_PASS if adopted else READING_PASS
        markers = _sent_records(context, act_id, act_key, ordinal, pass_name)
        if markers:
            replies = _unrecorded_replies(context) if replies is None else replies
            calls, unattributed = replies
            if unattributed or _answers_a_send(calls, markers):
                unrecorded.append(act_key)
                continue
        left += not adopted
    if half_read:
        raise ContractError(
            f"acts {half_read} hold artifacts from an interrupted live attempt and no "
            "Perlectio; a live pass resumes only from sealed, adoptable or untouched acts. "
            "Read these pages in a new run; the interrupted attempt's artifacts remain its "
            "evidence"
        )
    if unrecorded:
        raise ContractError(
            f"acts {unrecorded} were sent in an interrupted live attempt, and a reply is "
            "retained that no record names and that may be theirs; asking again would read "
            "them twice. Read these pages in a new run; the retained replies remain that "
            "attempt's evidence"
        )
    return left


def _utc(value: str) -> datetime:
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        raise ValueError(f"{value!r} names no time zone")
    return parsed


def refuse_past_deadline(
    deadline: datetime | None, seconds_needed: int, what: str, *, rate: str, remedy: str
) -> None:
    """Refuse work the reading deadline cannot hold, naming the planning rate and the way out."""
    if deadline is None:
        return
    remaining = int((deadline - datetime.now(timezone.utc)).total_seconds())
    if remaining < seconds_needed:
        raise ContractError(
            f"{what} needs {seconds_needed}s at {rate}, but the reading deadline "
            f"{deadline.isoformat()} leaves {remaining}s; nothing more was started. {remedy}"
        )


def _refuse_past_deadline(deadline: datetime | None, seconds_needed: int, what: str) -> None:
    refuse_past_deadline(
        deadline,
        seconds_needed,
        what,
        rate=f"{PLANNED_SECONDS_PER_CALL}s a call",
        remedy="Give a later --reading-deadline, or fewer acts with --act",
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


def _page_renders_for(context, bases: list[dict], *, page_context: dict[str, int]) -> list[dict]:
    """One page render per distinct page an act's regions touch, sized by the sealed
    `[page_context]` rule from the act's own crops on that page.

    A continuation act spans two pages; nuda and the primed pass see both,
    because sight is never what nuda withholds.
    """
    by_page: dict[str, list[dict]] = {}
    for basis in bases:
        by_page.setdefault(basis["source_page_id"], []).append(basis)
    return [
        dossier_module.build_page_render(
            context,
            source_page_id=page_id,
            source_page_ordinal=on_page[0]["source_page_ordinal"],
            page_context=page_context,
            crop_bounds=[basis["transform"]["bounds"] for basis in on_page],
            multi_page=len(by_page) > 1,
        )
        for page_id, on_page in by_page.items()
    ]


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
    return sum(dossier_module.union_area(rectangles) for rectangles in by_page.values())


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


def _smallest_page_pixels(page_renders: list[dict]) -> int:
    """The area of the smallest single page an act's regions were cut from.

    What the truncation legibility gate is judged on: a summed area would let one
    sub-legible page hide behind a large one.
    """
    return min(
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
    return sorted(inputs.values(), key=input_order)


# Closed and checked before publication: a missing field (identity, dissent, regime) is
# the failure a per-field type check never sees. Every record kind carries the doubt
# report, because a doubt reported on an instrument call is a measurement too.
_READING_FIELDS: Final = frozenset(
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
    }
)
_PERLECTIO_FIELDS: Final = _READING_FIELDS | {
    "basis",
    "lectio_kind",
    "self_revision",
    "protocol",
    "audit",
}
# No `basis`: a nuda reading has no witnesses.
_LECTIO_NUDA_FIELDS: Final = _READING_FIELDS | {"sampling"}
_LECTIO_PRIOR_FIELDS: Final = _READING_FIELDS | {"protocol"}
_PRIMED_WITHOUT_PRIOR_FIELDS: Final = _READING_FIELDS | {
    "basis",
    "sampling",
    "lectio_kind",
    "protocol",
    "membership",
}

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
_NOT_RUN_CROSS_CAPTURE_FIELDS: Final = _NOT_RUN_HELD_FIELDS | {"hold"}


def _require_closed_schema(payload: dict, fields: frozenset, *, what: str) -> None:
    missing = sorted(fields - set(payload))
    unexpected = sorted(set(payload) - fields)
    if missing or unexpected:
        raise SchemaRefusal(
            f"a Perlector {what} payload is not its closed schema: missing {missing}, "
            f"unexpected {unexpected}"
        )


def validate_not_run_payload(payload: dict, *, fields: frozenset) -> None:
    """Refuse a not-run Perlectio missing part of the record it claims.

    Capacity holds validate their autopsia and partition input where they are produced.
    """
    _require_closed_schema(payload, fields, what="not-run")


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
    run: "_Pass",
    act: dict[str, Any],
    ordinal: int,
    *,
    fields: frozenset,
    reason: str,
    inputs: list[dict[str, str]] | None = None,
    **facts: Any,
) -> None:
    """Publish why an act was not read, in the closed shape of that reason."""
    payload = {
        "act_key": act["act_key"],
        "attempt_ordinal": ordinal,
        "reason": reason,
        **facts,
        "provenance": provenance_for(run.context, run.chair, attempted=False),
    }
    validate_not_run_payload(payload, fields=fields)
    run.context.publish(
        kind="perlectio",
        subject_id=act["act_id"],
        outcome="not-run",
        attempt=perlector_attempt_id(act["act_id"], "perlegere", ordinal),
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
    """Fully validate the prospective immutable failed Perlectio before publishing it."""
    evidence = [
        dict(failure[name])
        for name in ("raw_response_ref", "call_record_ref", "receipt_ref")
        if failure[name] is not None
    ]
    inputs = distinct_refs(inputs + evidence)
    payload = {
        "act_key": act_key,
        "attempt_ordinal": ordinal,
        "reason": reason,
        "failure": failure,
        "provenance": provenance,
    }
    attempt = perlector_attempt_id(act_id, "perlegere", ordinal)
    identifier = artifact_id(PERLECTOR, "perlectio", act_id, attempt)
    candidate = build_envelope(
        run_id=context.tree.run_id,
        artifact_id=identifier,
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
    validate_failed_perlectio(context, candidate, act_id, expected_act_key=act_key)
    context.publish(
        kind="perlectio",
        subject_id=act_id,
        outcome="failed",
        attempt=attempt,
        inputs=inputs,
        payload=payload,
    )
    validate_failed_perlectio(
        context, context.tree.read_artifact(PERLECTOR, "perlectio", identifier), act_id
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


_DOSSIER_FIELDS: Final = frozenset(
    {
        "act_id",
        "act_key",
        "witness_regime",
        "regions",
        "page_renders",
        "testimonia",
        "dossier_digest",
    }
)
# Logical identity and atomic presentation travel together or not at all.
_DOSSIER_SHAPES: Final = tuple(
    _DOSSIER_FIELDS | variant | clues | cross_capture
    for variant in (
        frozenset(),
        {"act_attachment"},
        {"prior_draft_view"},
        {"act_attachment", "prior_draft_view"},
        {"prior_draft", "prior_draft_view"},
        {"act_attachment", "prior_draft", "prior_draft_view"},
    )
    for clues in (frozenset(), {"neighbours"})
    for cross_capture in (frozenset(), {"logical_act_id", "cross_capture_autopsia"})
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
    _require_closed_schema(payload, fields, what="reading")
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
    if not isinstance(reading_dossier, dict) or set(reading_dossier) not in _DOSSIER_SHAPES:
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
    protocol_record = payload.get("protocol")
    refuse_removed_draft_fed(protocol_record, "a Perlector reading")
    if protocol_record is not None and (
        not isinstance(protocol_record, dict)
        or set(protocol_record) != {"selection_rule", "page_shared_prefix_policy", "blind_read"}
        or protocol_record["blind_read"] not in BLIND_READ_MODES
    ):
        raise SchemaRefusal("a prior-draft protocol record is not its closed schema")
    _validate_lectio_kind(payload, reading_dossier)
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
    if "neighbours" in reading_dossier:
        if is_unprimed:
            raise SchemaRefusal(
                "an unprimed reading's dossier cannot carry the neighbouring acts' witness readings"
            )
        _validate_neighbours(
            reading_dossier,
            None
            if protocol_config is None
            else protocol_config[protocol.NEIGHBOURS_TABLE]["characters_per_row"],
        )
    _validate_dossier_testimonia(
        reading_dossier,
        basis,
        is_unprimed=is_unprimed,
        run_id=run_id,
        config_digest=config_digest,
    )
    _validate_reading_prompt(
        payload,
        provenance,
        reading_dossier,
        protocol_config=protocol_config,
        protocol_sha256=protocol_sha256,
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
    if "audit" in fields:
        # Re-proof offsets index the frozen semi-final, which may be longer than the final;
        # the chain check binds them before publication, so no bound is guessed here.
        audit.validate_perlectio_audit(payload.get("audit"), text_length=None)
    annotations.validate_annotations(payload, outcome=outcome)


_NEIGHBOUR_SIDES: Final = frozenset({"preceding", "following"})
_NEIGHBOUR_FIELDS: Final = frozenset({"act_id", "act_key", "same_page", "witnesses", "unavailable"})
_NEIGHBOUR_WITNESS_FIELDS: Final = frozenset(
    {"witness_label", "outcome", "reported", "reported_basis", "shown", "testimonium_ref"}
)
_TESTIMONIUM_PREFIX: Final = f"{writing_directory(ATTESTATORES)}/artifacts/testimonium/"


def neighbour_testimonium_refs(reading_dossier: dict) -> list[dict[str, str]]:
    """The Testimonia a reading was shown as neighbour clues, never as its own witnesses."""
    neighbours = reading_dossier.get("neighbours") or {}
    return [
        witness["testimonium_ref"]
        for entry in neighbours.values()
        if entry is not None
        for witness in entry["witnesses"]
    ]


def _validate_neighbours(reading_dossier: dict, characters_per_row: int | None) -> None:
    """The closed neighbour-clue shape: two sides, never this act, sealed witness refs.

    With the sealed `characters_per_row`, each reading must be cut as `neighbour_entry`
    cuts it: whole within the cap, else exactly the cap's tail (preceding) or head
    (following). Without one (an unsealed test call) the lengths are not checked.
    """
    neighbours = reading_dossier["neighbours"]
    if not isinstance(neighbours, dict) or set(neighbours) != _NEIGHBOUR_SIDES:
        raise SchemaRefusal("a Perlector dossier's neighbours are not {preceding, following}")
    for side, entry in neighbours.items():
        if entry is None:
            continue
        if (
            not isinstance(entry, dict)
            or set(entry) != _NEIGHBOUR_FIELDS
            or not isinstance(entry["act_id"], str)
            or not isinstance(entry["act_key"], str)
            or not isinstance(entry["same_page"], bool)
            or not isinstance(entry["witnesses"], list)
            or not (entry["unavailable"] is None or isinstance(entry["unavailable"], str))
        ):
            raise SchemaRefusal("a Perlector dossier's neighbour is not its closed shape")
        if entry["act_id"] == reading_dossier["act_id"]:
            raise SchemaRefusal("a Perlector dossier names its own act as its neighbour")
        if entry["unavailable"] is not None and entry["witnesses"]:
            raise SchemaRefusal(
                "a Perlector dossier's neighbour says its readings are unavailable and carries some"
            )
        for witness in entry["witnesses"]:
            if not isinstance(witness, dict) or set(witness) != _NEIGHBOUR_WITNESS_FIELDS:
                raise SchemaRefusal("a neighbour witness row is not its closed shape")
            reported = witness["reported"]
            shown = {"whole", "head", "tail"} if isinstance(reported, str) else {None}
            if (
                not isinstance(witness["witness_label"], str)
                or not (reported is None or isinstance(reported, str))
                or witness["reported_basis"] not in {"own-report", "page-slice", "none"}
                or witness["shown"] not in shown
            ):
                raise SchemaRefusal("a neighbour witness row carries a malformed reading")
            if characters_per_row is not None and reported is not None:
                cut = {"preceding": "tail", "following": "head"}[side]
                if (
                    len(reported) > characters_per_row
                    if witness["shown"] == "whole"
                    else witness["shown"] != cut or len(reported) != characters_per_row
                ):
                    raise SchemaRefusal(
                        "a neighbour witness reading is not cut as the sealed neighbour cap cuts it"
                    )
            reference = digest_ref(witness["testimonium_ref"], "a neighbour testimonium_ref")
            if not reference["relative_path"].startswith(_TESTIMONIUM_PREFIX):
                raise SchemaRefusal(
                    "a neighbour testimonium_ref does not name a sealed Attestatores Testimonium"
                )


def _validate_lectio_kind(payload: dict, reading_dossier: dict) -> None:
    lectio_kind = payload.get("lectio_kind")
    prior_draft = reading_dossier.get("prior_draft")
    if lectio_kind in ("primed-with-prior", "primed-draft-withheld"):
        expected_view = validate_establishing_view(payload, reading_dossier, "a Perlectio")
        if expected_view == "withheld":
            if "prior_draft" in reading_dossier:
                raise SchemaRefusal(
                    f"a Perlectio claims {lectio_kind} but carries a prior draft; a withheld "
                    "run makes no Pass A"
                )
        elif (
            not isinstance(prior_draft, dict)
            or set(prior_draft) != {"reference", "text"}
            or not isinstance(prior_draft["text"], str)
        ):
            raise SchemaRefusal(
                f"a Perlectio claims {lectio_kind} but carries no closed prior-draft "
                f"reference with view {expected_view!r}"
            )
        else:
            validate_input_refs([prior_draft["reference"]])
    elif lectio_kind == "primed-without-prior":
        # Key presence, not value: the shape check admits these keys, so a None
        # prior_draft beside a view key would pass a value test.
        if "prior_draft" in reading_dossier or "prior_draft_view" in reading_dossier:
            raise SchemaRefusal(
                "a Perlectio claims primed-without-prior but carries prior-draft data"
            )
    elif lectio_kind is not None:
        # Any other value would publish its prior-draft evidence uninspected.
        raise SchemaRefusal(
            f"a Perlector reading names unknown lectio kind {lectio_kind!r}; a kind this "
            "validator cannot name would publish its prior-draft evidence unchecked"
        )
    elif "prior_draft" in reading_dossier or "prior_draft_view" in reading_dossier:
        # Lectio nuda and lectio-prior are unprimed and see no prior-draft data.
        raise SchemaRefusal("an unprimed Perlector reading carries prior-draft data")


def _validate_dossier_testimonia(
    reading_dossier: dict,
    basis: Any,
    *,
    is_unprimed: bool,
    run_id: str | None,
    config_digest: str | None,
) -> None:
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


def _validate_reading_prompt(
    payload: dict,
    provenance: dict,
    reading_dossier: dict,
    *,
    protocol_config: dict[str, Any] | None,
    protocol_sha256: str | None,
) -> None:
    """The prompt record, and its protocol block, reproduce from the chair and dossier."""
    prompt_record = payload["prompt"]
    identity_record = provenance.get("resolved_identity")
    if not isinstance(identity_record, dict):
        raise SchemaRefusal("a Perlector prompt has no resolved chair identity")
    try:
        identity = ChairIdentity(**identity_record)
    except TypeError as error:
        raise SchemaRefusal("a Perlector prompt carries a malformed chair identity") from error
    protocol_record = payload.get("protocol")
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
            "neighbours": {"fragment": ""},
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
    refusal as its problem, never an empty confident list.
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


def _sealed_length_floor(
    protocol_config: dict[str, Any] | None, field: str = protocol.LENGTH_FLOOR_FIELD
) -> int | None:
    """One of this run's sealed truncation terms, or `None` when no sealed protocol reached this pass.

    Never a guessed default: a re-proof is held to the floor and gate this run sealed or to none.
    """

    if not isinstance(protocol_config, dict):
        return None
    table = protocol_config.get(protocol.TRUNCATION_TABLE)
    if not isinstance(table, dict):
        return None
    floor = table.get(field)
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
    smallest_page_pixels: int | None = None,
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
            smallest_page_pixels=smallest_page_pixels,
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
    decoding_policy: dict[str, Any] | None = None,
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
            legible_page_pixels=_sealed_length_floor(protocol_config, protocol.LEGIBLE_PAGE_FIELD),
            decoding_policy=decoding_policy,
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
    decoding_policy: dict[str, Any] | None = None,
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
                decoding_policy=decoding_policy,
            )
        )
    return audit.flags_once_per_page(frozen)


def _audit_draft(
    context,
    row: dict[str, Any],
    flags: list[dict[str, Any]],
    *,
    round_cap: int,
    policy_record: dict[str, Any],
) -> tuple[dict[str, Any], dict[str, Any], dict[str, str]]:
    """Freeze the Pass-B semi-final and its page flags before any re-proof.

    Returns the draft payload, its publication and the reference it will have once
    published. The re-proof request binds that reference before the call, and the
    draft itself is published in act order afterwards, beside its finding.
    """
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
    publication = {
        "kind": "audit-draft",
        "subject_id": row["act_id"],
        "outcome": "read",
        "attempt": perlector_attempt_id(row["act_id"], "perlegere", payload["attempt_ordinal"]),
        "inputs": row["inputs"],
        "payload": draft_payload,
    }
    envelope = context.envelope(**publication)
    reference = {
        "relative_path": context.tree.artifact_path(
            PERLECTOR, "audit-draft", envelope["artifact_id"]
        ),
        "sha256": digest_bytes(canonical_bytes(envelope)),
    }
    return draft_payload, publication, reference


def _publish_audit_draft(context, publication: dict[str, Any], reference: dict[str, str]) -> None:
    published = context.publish(**publication)
    if context.input_ref(published.relative_path) != reference:
        raise SchemaRefusal(
            f"the audit draft at {published.relative_path} is not the draft its re-proof "
            f"request bound ({reference!r})"
        )


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
        smallest_page_pixels=row["smallest_page_pixels"],
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
    """The facts every record of one act's reading attempt is published with, never rebound."""

    act_key: str
    act_id: str
    ordinal: int
    chair: ChairIdentity
    bases: list[dict]
    page_renders: list[dict]
    region_pixels: int
    page_pixels: int
    smallest_page_pixels: int
    protocol_config: dict[str, Any]
    protocol_sha256: str
    receipt_ref: dict[str, str] | None

    def provenance(self, context) -> dict:
        return provenance_for(context, self.chair, attempted=True, receipt_ref=self.receipt_ref)

    def truncation(self, text: str, stop_reason: str | None) -> dict[str, Any]:
        return truncation.classify(
            text,
            region_pixels=self.region_pixels,
            page_pixels=self.page_pixels,
            smallest_page_pixels=self.smallest_page_pixels,
            truncation_policy=self.protocol_config[protocol.TRUNCATION_TABLE],
            stop_reason=stop_reason,
        )


def _arm_reading(
    context, attempt: _Attempt, dossier: dict[str, Any], result: dict[str, Any], testimonia
) -> tuple[dict[str, Any], str, dict[str, dict[str, str]]]:
    """The fields every arm's record shares, its outcome, and its Testimonium references."""
    sealed_dossier = _reseal_dossier(dossier)
    prompt = prompts.prompt_evidence(
        attempt.chair, sealed_dossier, attempt.protocol_config, attempt.protocol_sha256
    )
    truncation_record = attempt.truncation(result["text"], result["stop_reason"])
    outcome = _resolve_outcome(
        declared_failure=None, truncation_record=truncation_record, text=result["text"]
    )
    text = "" if outcome == "no-readable-text" else result["text"]
    references = _testimonium_references(context, testimonia)
    assessment, spans, gaps = _published_doubt(
        result,
        text=text,
        outcome=outcome,
        whole_act_gaps=_whole_act_gap(testimonia, references),
    )
    payload = {
        "act_key": attempt.act_key,
        "attempt_ordinal": attempt.ordinal,
        "text": text,
        "dossier": sealed_dossier,
        "prompt": prompt,
        "truncation": truncation_record,
        "uncertain_spans": spans,
        "uncertainty_assessment": assessment,
        "gaps": gaps,
    }
    return payload, outcome, references


def _publish_arm(
    context,
    attempt: _Attempt,
    result: dict[str, Any],
    payload: dict[str, Any],
    *,
    kind: str,
    operation: str,
    outcome: str,
    fields: frozenset,
    witness_inputs: list[dict[str, str]] | None = None,
    approval_ref: ApprovalRecordBinding | None = None,
) -> None:
    """Validate one arm's record against exactly the inputs it cites, then publish it."""
    fields = with_engine_call(payload, result, fields)
    reading_inputs = (
        _reading_image_inputs(
            context,
            attempt.bases,
            attempt.page_renders,
            autopsia=payload["dossier"]["cross_capture_autopsia"],
        )
        + (witness_inputs or [])
        + engine_call_inputs(
            context,
            result.get("engine_call"),
            variance_arm=operation if operation in VARIANCE_ARMS else None,
        )
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
        kind=kind,
        subject_id=attempt.act_id,
        outcome=outcome,
        attempt=perlector_attempt_id(attempt.act_id, operation, attempt.ordinal),
        inputs=reading_inputs
        + ([approval_ref.reference.to_record()] if approval_ref is not None else []),
        payload=payload,
    )


def _protocol_record(context, protocol_config: dict[str, Any]) -> dict[str, Any]:
    return {
        "selection_rule": protocol_config["selection_rule"],
        "page_shared_prefix_policy": protocol_config["page_shared_prefix_policy"],
        "blind_read": context.blind_read,
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
    payload, outcome, _ = _arm_reading(context, attempt, dossier, result, [])
    payload.update(
        sampling=nuda.sampling_design(
            nuda_per_mille=context.nuda_per_mille,
            approval_ref=approval_ref,
        ),
        dissent=[],
        provenance=attempt.provenance(context),
    )
    _publish_arm(
        context,
        attempt,
        result,
        payload,
        kind=nuda.LECTIO_NUDA_KIND,
        operation="lectio-nuda",
        outcome=outcome,
        fields=_LECTIO_NUDA_FIELDS,
        approval_ref=approval_ref,
    )


def _publish_lectio_prior(
    context, attempt: _Attempt, dossier: dict[str, Any], result: dict[str, Any]
) -> dict:
    """Publish Pass A as a retained draft, never as a Perlectio."""
    payload, outcome, _ = _arm_reading(context, attempt, dossier, result, [])
    payload.update(
        dissent=[],
        provenance=attempt.provenance(context),
        protocol=_protocol_record(context, attempt.protocol_config),
    )
    _publish_arm(
        context,
        attempt,
        result,
        payload,
        kind="lectio-prior",
        operation="lectio-prior",
        outcome=outcome,
        fields=_LECTIO_PRIOR_FIELDS,
    )
    prior_artifact_id = _attempt_artifact_id(
        attempt.act_id, "lectio-prior", "lectio-prior", attempt.ordinal
    )
    return {
        "reference": context.artifact_ref(PERLECTOR, "lectio-prior", prior_artifact_id),
        "text": payload["text"],
    }


def _publish_lectio_prior_failure(context, attempt: _Attempt, error: Exception) -> None:
    """Keep a failed saved-mode Pass A as its own failed `lectio-prior`, never as a Perlectio.

    A contract or schema defect (no failure fact) is not an instrument failure and stays fatal.
    """
    failure = _failure_record(error, phase="establishing")
    if failure is None:
        raise error
    payload = {
        "act_key": attempt.act_key,
        "attempt_ordinal": attempt.ordinal,
        "reason": f"saved blind read {failure['kind']} failure: {failure['code']}",
        "failure": failure,
        "provenance": attempt.provenance(context),
    }
    validate_failed_payload(payload)
    evidence = [
        dict(failure[name])
        for name in ("raw_response_ref", "call_record_ref", "receipt_ref")
        if failure[name] is not None
    ]
    context.publish(
        kind="lectio-prior",
        subject_id=attempt.act_id,
        outcome="failed",
        attempt=perlector_attempt_id(attempt.act_id, "lectio-prior", attempt.ordinal),
        inputs=distinct_refs(evidence),
        payload=payload,
    )


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
    payload, outcome, testimonium_references = _arm_reading(
        context, attempt, dossier, result, testimonia
    )
    payload.update(
        basis={
            "regions": attempt.bases,
            "testimonia": _testimonia_basis(testimonia, testimonium_references),
        },
        sampling=protocol.control_sampling_design(
            per_mille=context.perlector_instrument_per_mille,
            selection_rule=attempt.protocol_config["selection_rule"],
            approval_ref=approval_ref,
        ),
        # The sampling draw is keyed by the logical act, so the membership records that
        # subject; a capture ID would make a clustered control irreproducible. `context.run`
        # was verified when opened and nothing rewrites `run.json`.
        membership={
            **context.run["corpus_frame_membership"],
            "act_id": payload["dossier"]["logical_act_id"],
            "protocol_sha256": attempt.protocol_sha256,
        },
        dissent=dissent_against(payload["text"], dissent_testimonia(testimonia, attachment_view)),
        provenance=attempt.provenance(context),
        lectio_kind="primed-without-prior",
        protocol=_protocol_record(context, attempt.protocol_config),
    )
    _publish_arm(
        context,
        attempt,
        result,
        payload,
        kind="primed-without-prior",
        operation="primed-without-prior",
        outcome=outcome,
        fields=_PRIMED_WITHOUT_PRIOR_FIELDS,
        witness_inputs=list(testimonium_references.values()) + [attachment_view["reference"]],
        approval_ref=approval_ref,
    )


def _prior_text(prior: dict[str, Any] | None) -> str:
    """The Pass-A text, or empty when the run made no Pass A."""
    return prior["text"] if prior else ""


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
    prior = primed_dossier.get("prior_draft")
    # The prompt is reproduced from the dossier the reader was handed.
    prompt = prompts.prompt_evidence(
        attempt.chair, primed_dossier, attempt.protocol_config, attempt.protocol_sha256
    )
    reading = "" if declared_failure == "no-readable-text" else result["text"]
    truncation_record = _reconciled_truncation(
        declared_failure=declared_failure,
        truncation_record=attempt.truncation(reading, result["stop_reason"]),
    )
    outcome = _resolve_outcome(
        declared_failure=declared_failure, truncation_record=truncation_record, text=reading
    )
    if outcome == "no-readable-text":
        # Whitespace resolved as unreadable is published as the empty text it is.
        reading = ""
    testimonium_references = _testimonium_references(context, testimonia)
    sealed_doubt, reader_spans, gaps = _published_doubt(
        result,
        text=reading,
        outcome=outcome,
        whole_act_gaps=_whole_act_gap(testimonia, testimonium_references),
    )
    provenance = attempt.provenance(context)
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
        "lectio_kind": kind_for_view(primed_dossier["prior_draft_view"]),
        "self_revision": self_revision_for_view(
            primed_dossier["prior_draft_view"], reading, _prior_text(prior), departures
        ),
        "protocol": _protocol_record(context, attempt.protocol_config),
    }
    # The call the published text came from; the audit loop re-points it at the
    # re-proof's call when that text is published.
    with_engine_call(payload, result, _PERLECTIO_FIELDS)
    return _pending_row(
        context,
        attempt,
        act,
        payload,
        outcome,
        order=order,
        declared_failure=declared_failure,
        testimonia=testimonia,
        attachment_view=attachment_view,
        autopsia=autopsia,
    )


def _pending_row(
    context,
    attempt: _Attempt,
    act: dict[str, Any],
    payload: dict[str, Any],
    outcome: str,
    *,
    order: int,
    declared_failure: str | None,
    testimonia: list[dict],
    attachment_view: dict[str, Any],
    autopsia: dict[str, Any],
) -> dict[str, Any]:
    """The Pass-B payload with everything the audit pass needs to finish it."""
    prior = payload["dossier"].get("prior_draft")
    engine_call = payload.get("engine_call")
    return {
        "act": act,
        "act_id": attempt.act_id,
        "order": order,
        "bases": attempt.bases,
        "payload": payload,
        "fields": _PERLECTIO_FIELDS | ({"engine_call"} if engine_call is not None else set()),
        "outcome": outcome,
        # Areas, not decoded pixels: holding every act's images until the audit loop would
        # risk an OOM kill before any Perlectio publishes. A re-proof rebuilds its pixels
        # from the sealed artifacts.
        "region_pixels": attempt.region_pixels,
        "page_pixels": attempt.page_pixels,
        "smallest_page_pixels": attempt.smallest_page_pixels,
        "declared_failure": declared_failure,
        "testimonia": testimonia,
        "attachment_view": attachment_view,
        "prior": prior,
        "autopsia": autopsia,
        "inputs": _reading_image_inputs(
            context, attempt.bases, attempt.page_renders, autopsia=autopsia
        )
        + list(_testimonium_references(context, testimonia).values())
        + [attachment_view["reference"]]
        + ([prior["reference"]] if prior else [])
        + engine_call_inputs(context, engine_call, variance_arm=None),
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
    run = _open_pass(registry_factory, serving_factory, service)
    if run.protocol_config[protocol.READING_UNIT_FIELD] == page_run.READING_UNIT:
        return _read_the_pages(run)
    _refuse_a_live_start_past_the_deadline(run)
    # The effective width, for the transcript: the journal holds only what was asked.
    print(f"perlector: up to {run.concurrency} reader calls in flight", file=sys.stderr)
    rows = _in_order_window(run.concurrency, (_reading_job(run, act) for act in run.wanted))
    pending = [row for row in rows if row is not None]
    _publish_audited_readings(run, pending)
    # Every requested act was read, resumed or acknowledged, or the pass refused.
    if not run.wanted:
        raise ContractError("the Perlector read no act and acknowledged no held act")
    # Before the seal, so a failed shutdown is never reported over a sealed stage;
    # `close` is idempotent with `main`'s `finally`.
    service.close()
    run.context.seal_boundary()
    run.context.finish()
    return EXIT_COMPLETE


def _read_the_pages(run: "_Pass") -> int:
    """The page path (`page_run.py`): every sealed page read whole, then the seal."""
    page_run.read_the_pages(
        run,
        page_run.StageHooks(
            provenance_for=provenance_for,
            engine_call_inputs=engine_call_inputs,
            start_chair=_start_chair,
            publish_sent=_publish_sent,
            sent_records=_sent_records,
            unrecorded_replies=_unrecorded_replies,
            answers_a_send=_answers_a_send,
            in_order_window=_in_order_window,
            refuse_past_deadline=refuse_past_deadline,
            failure_record=_failure_record,
            real_ingress=real_ingress,
            sent_kind=SENT_KIND,
        ),
    )
    # Before the seal, as the act path does, so a failed shutdown is never reported
    # over a sealed stage.
    run.service.close()
    run.context.seal_boundary()
    run.context.finish()
    return EXIT_COMPLETE


@dataclass
class _Pass:
    """The sealed inputs one Perlector pass reads every act under, and its live chair."""

    context: Any
    args: Any
    service: ResidentChair
    client_factory: Any
    chair: ChairIdentity | AbsentChair
    serving_mode: str
    # `None` in live mode until the first act that needs it: a resumed pass with every
    # act sealed or over capacity never loads a model on a card billed by the hour.
    reader: Any
    witness_context_table: Any
    protocol_config: dict[str, Any]
    protocol_sha256: str
    # The sealed decoding policy, and from it the output bounds of a reading and
    # of an audit re-proof.
    decoding_policy: dict[str, Any]
    reading_max_tokens: int
    reproof_max_tokens: int
    # The sealed output cap of one whole-page reading (`page_run.py`).
    page_max_tokens: int
    nuda_approval: ApprovalRecordBinding | None
    instrument_approval: ApprovalRecordBinding | None
    audit_policy: dict[str, Any]
    audit_sha256: str
    expected: list[dict[str, Any]]
    declared_order: dict[str, int]
    wanted: list[dict[str, Any]]
    partition: Any
    partition_ref: dict[str, str] | None
    holds: dict[str, Any]
    max_images: int | None
    # The run-wide routing denominator; `reported_unrouted` keeps one finding from being
    # restated by every act reaching the same page Testimonium.
    all_proposal_regions: list[dict]
    reported_unrouted: set[tuple[str, int]] = field(default_factory=set)
    receipt_ref: dict[str, str] | None = None
    unread: int = 0
    # Reader calls in flight at once; see `_reading_concurrency`.
    concurrency: int = 1
    # The main-pass results of acts a live resume found sealed: the page flags are
    # computed over every act's semi-final, as the uninterrupted pass computed them.
    sealed_semi_finals: list[dict[str, Any]] = field(default_factory=list)
    # Each act's witness readings and pages as a neighbour clue, built once on the
    # main thread and shared by the two acts it neighbours.
    neighbour_clues: dict[str, tuple[set[str], list[dict[str, Any]], str | None]] = field(
        default_factory=dict
    )

    @property
    def calls_per_act(self) -> int:
        return (
            1
            + self.audit_policy["round_cap"]
            + (self.context.blind_read != "off")
            + bool(self.context.nuda_per_mille)
            + bool(self.context.perlector_instrument_per_mille)
        )


def _open_pass(registry_factory, serving_factory, service: ResidentChair) -> _Pass:
    """Resolve every sealed input the pass needs, refusing before anything is published."""
    parser = stage_parser(DESCRIPTION)
    parser.add_argument(
        "--reading-deadline",
        type=_utc,
        default=None,
        help="UTC time by which a live pass must finish reading; it refuses to start, or "
        "to read another act, when the planned calls would run past it",
    )
    parser.add_argument(
        "--perlector-concurrency",
        type=_positive_int,
        default=None,
        help="reader calls a live pass keeps in flight at once, so the engine can batch "
        "them; capped by the served row's max_num_seqs, which is also the default. "
        "A blind-read (fed or saved) or fixture pass reads one act at a time",
    )
    args = parser.parse_args()
    context = open_stage_context(args, PERLECTOR, registry_factory=registry_factory)
    decoding_policy, decoding_sha256 = load_decoding_policy(args.decoding_config)
    context.require_sealed_config("decoding", decoding_sha256)
    reading_max_tokens, reproof_max_tokens = perlector_max_tokens(decoding_policy)
    chair = perlector_chair(context)
    serving_mode = perlector_serving_mode(context, args, chair)
    reader = fixture_reader_for(context, chair, serving_mode)
    witness_context_table = dossier_module.load_witness_context(
        Path(context.witness_context_config_path)
    )
    protocol_config, protocol_sha256 = protocol.load(context.perlector_protocol_config_path)
    context.require_sealed_config("perlector-protocol", protocol_sha256)
    nuda_approval = _sampling_approval(
        context, context.nuda_per_mille, context.nuda_approval_ref, NUDA_APPROVAL_SUBJECT
    )
    instrument_approval = _sampling_approval(
        context,
        context.perlector_instrument_per_mille,
        context.perlector_instrument_approval_ref,
        PERLECTOR_INSTRUMENT_APPROVAL_SUBJECT,
    )
    audit_policy, audit_sha256 = audit.load(context.perlector_audit_config_path)
    context.require_sealed_config("perlector-audit", audit_sha256)

    expected = expected_acts(context)
    # An attempt nobody requested would make the attempt tally meaningless.
    if args.act and protocol_config[protocol.READING_UNIT_FIELD] == page_run.READING_UNIT:
        raise ContractError(
            f"asked to read act {args.act}, but this run is sealed with reading_unit = "
            '"page": the Perlector reads every sealed page whole and names its own acts, so '
            "there is no Designator act to read alone; run the pass without --act"
        )
    wanted = [act for act in expected if args.act in (None, act["act_id"])]
    if args.act and not wanted:
        raise ContractError(f"asked to read {args.act}, which the proposal seal does not name")
    preflight_testimonia_denominator(context, wanted)
    # Recovery may narrow `wanted`; the partition denominator stays the whole proposal seal.
    if protocol_config[protocol.READING_UNIT_FIELD] == page_run.READING_UNIT:
        # The page path reads no act, so nothing would bind a partition retained here.
        partition, partition_ref, holds = None, None, {}
    else:
        partition, partition_ref, holds = logical_reading.build_run_partition(context, expected)
    max_images = protocol_config.get("max_images")
    if not isinstance(max_images, int) or isinstance(max_images, bool):
        max_images = None
    return _Pass(
        context=context,
        args=args,
        service=service,
        client_factory=serving_factory
        or partial(
            stage_chair_client,
            decoding_policy=decoding_policy,
            decoding_config_sha256=decoding_sha256,
        ),
        chair=chair,
        serving_mode=serving_mode,
        reader=reader,
        witness_context_table=witness_context_table,
        protocol_config=protocol_config,
        protocol_sha256=protocol_sha256,
        decoding_policy=decoding_policy,
        reading_max_tokens=reading_max_tokens,
        reproof_max_tokens=reproof_max_tokens,
        page_max_tokens=perlector_page_max_tokens(decoding_policy),
        nuda_approval=nuda_approval,
        instrument_approval=instrument_approval,
        audit_policy=audit_policy,
        audit_sha256=audit_sha256,
        expected=expected,
        declared_order={act["act_id"]: order for order, act in enumerate(expected)},
        wanted=wanted,
        partition=partition,
        partition_ref=partition_ref,
        holds=holds,
        max_images=max_images,
        all_proposal_regions=sealed_proposal_regions(context),
        concurrency=_reading_concurrency(context, args, chair, serving_mode),
    )


def _positive_int(value: str) -> int:
    number = int(value)
    if number < 1:
        raise ValueError(f"{value!r} is not a positive count")
    return number


def _reading_concurrency(context, args, chair, serving_mode: str) -> int:
    """How many reader calls may be in flight at once.

    Only a live engine batches, and never beyond its served row's `max_num_seqs`. A
    blind-read pass (`fed` or `saved`) stays serial: Pass A, or its failure, is published
    inline before the establishing call (`fed` cites it), and publishing happens on the
    main thread only.
    """
    if serving_mode != "live" or context.blind_read != "off":
        return 1
    bound = (
        bound_serving_recipes(context, args.serving_recipes_config)
        .for_identity(chair, args.placement_tier)
        .max_num_seqs
    )
    return min(args.perlector_concurrency or bound, bound)


def _sampling_approval(
    context, per_mille: int, approval_ref: str, subject: str
) -> ApprovalRecordBinding | None:
    if not per_mille:
        return None
    return resolve_sampling_approval(context, approval_ref=approval_ref, subject=subject)


def _refuse_a_live_start_past_the_deadline(run: _Pass) -> None:
    """Count the acts a live pass still has to read, and refuse a start it cannot finish."""
    if run.serving_mode != "live":
        return
    run.unread = _acts_left_to_read(
        run.context, [act for act in run.wanted if act["act_id"] not in run.holds]
    )
    if not run.unread:
        return
    startup = (
        bound_serving_recipes(run.context, run.args.serving_recipes_config)
        .for_identity(run.chair, run.args.placement_tier)
        .startup_timeout_seconds
    )
    _refuse_past_deadline(
        run.args.reading_deadline,
        startup + run.unread * run.calls_per_act * PLANNED_SECONDS_PER_CALL,
        f"starting the Perlector ({startup}s) and reading {run.unread} acts",
    )


@dataclass(frozen=True)
class _PreparedAct:
    """Everything one act's reader calls and their publication need, resolved in order."""

    act: dict[str, Any]
    attempt: _Attempt
    declared_failure: str | None
    testimonia: list[dict]
    attachment_view: dict[str, Any]
    autopsia: dict[str, Any]
    dossier: dict[str, Any]
    nuda_sampled: bool
    control_sampled: bool
    # A live resume's retained `semi-final` for this act, adopted instead of asking again.
    adopted: dict[str, Any] | None = None


def _reading_job(run: _Pass, act: dict[str, Any]):
    """Prepare one act on the main thread; return its reader call and in-order finish.

    An act with no call has no call: its finish publishes its not-run record, if any, or
    adopts its retained main-pass result. A live call is recorded as sent before it leaves.
    """
    prepared = _prepare_act(run, act)
    if not isinstance(prepared, _PreparedAct):
        return None, partial(_publish_without_reading, prepared)
    if prepared.adopted is not None:
        return None, partial(_adopted_row, run, prepared)
    attempt = prepared.attempt
    if run.serving_mode == "live":
        _publish_sent(
            run,
            attempt.act_id,
            attempt.act_key,
            attempt.ordinal,
            READING_PASS,
            presented_image_sha256s(prepared.autopsia),
        )
    return partial(_call_act, run, prepared), partial(_publish_act, run, prepared)


def _publish_without_reading(publication, _result: None) -> None:
    if publication is not None:
        publication()


def _prepare_act(run: _Pass, act: dict[str, Any]):
    """Resolve one act up to its reader call, or to the not-run record it will publish.

    Returns the prepared act, the deferred not-run publication, or `None` for an act
    already sealed.
    """
    context, act_id, act_key = run.context, act["act_id"], act["act_key"]
    if act["outcome"] == "held":
        # Reading part of an act would deliver a truncation as output; acknowledged
        # explicitly, never skipped, so no unit goes unaccounted.
        return partial(
            _publish_not_run,
            run,
            act,
            1,
            fields=_NOT_RUN_HELD_FIELDS,
            reason=(
                "the Designator held this act; an incomplete proposal is "
                "not read, because a reading of part of an act would be a "
                "truncation delivered as an output"
            ),
        )

    if act_id in run.holds:
        # Derived, not fixed: an act re-asked after a recrop gets its next ordinal.
        return partial(
            _publish_not_run,
            run,
            act,
            _next_attempt(context, act_id, act_regions(context, act_id)[0]),
            fields=_NOT_RUN_CROSS_CAPTURE_FIELDS,
            inputs=[run.partition_ref],
            reason=(
                "the corpus register records this act's capture as a member of a "
                "physical page, and no read across a physical page's captures is "
                "built yet; reading this capture alone could establish one capture's "
                "text for the physical act"
            ),
            hold={
                "code": CROSS_CAPTURE_READ_NOT_BUILT,
                "partition_finding": run.holds[act_id],
            },
        )

    # One region read answers both the attempt ordinal and the crops read, so
    # `_next_attempt` refuses an unplaceable origin before any immutable Perlectio exists.
    regions, proposal_regions = act_regions(context, act_id)
    ordinal = _next_attempt(context, act_id, regions)
    if isinstance(run.chair, AbsentChair):
        # An explicit record of the absence: producing nothing would leave the Recensor
        # to infer a gap it cannot see.
        return partial(
            _publish_not_run,
            run,
            act,
            ordinal,
            fields=_NOT_RUN_ABSENT_FIELDS,
            reason=f"the Perlector chair is explicitly absent: {run.chair.reason}",
            basis={"regions": [], "testimonia": []},
            dissent=[],
        )

    if run.serving_mode == "live" and _reading_already_sealed(
        context, act_id, ordinal, act_key=act_key
    ):
        # Never asked again: a second live reading would differ and the store refuses
        # it. Not counted in `unread`: `_acts_left_to_read` already excluded it.
        semi_final = _semi_final_record(context, act_id, ordinal)
        if semi_final is not None:
            _validate_semi_final(run, semi_final)
            run.sealed_semi_finals.extend(
                audit_semi_finals_for_pages(
                    act_id=act_id,
                    order=run.declared_order[act_id],
                    text=semi_final["payload"]["text"],
                    bases=semi_final["payload"]["basis"]["regions"],
                    dossier=semi_final["payload"]["dossier"],
                )
            )
        return None
    # Its main-pass reply is on record, so it is adopted and never asked again.
    adopted = _semi_final_record(context, act_id, ordinal) if run.serving_mode == "live" else None

    # A declared engine outcome stands in for a real engine's report, so it is valid
    # only when no engine answers.
    declared_failure = declared_reading_failure(context, act_key)
    if run.serving_mode == "live" and declared_failure is not None:
        raise ContractError(
            f"the fixture declares reading outcome {declared_failure!r} for "
            f"act {act_key!r} while a live chair is answering; a "
            "declared stand-in cannot override an engine that reported"
        )

    bases, testimonia, attachment_view = _witnessed_act(
        context,
        act,
        regions,
        proposal_regions,
        all_proposal_regions=run.all_proposal_regions,
        reported_unrouted=run.reported_unrouted,
    )
    region_pixels = _region_pixels(bases)
    page_renders = _page_renders_for(
        context,
        bases,
        page_context=run.protocol_config[protocol.PAGE_CONTEXT_TABLE],
    )
    page_pixels = _page_pixels(page_renders)
    smallest_page_pixels = _smallest_page_pixels(page_renders)

    # Resolved before any reader call so an absent member cannot become a partial
    # presentation.
    logical_act_id = logical_reading.logical_act_id_for(run.partition, act_id)
    autopsia = logical_reading.act_autopsia(
        context,
        logical_act_id=logical_act_id,
        partition_ref=run.partition_ref,
        act=act,
        bases=bases,
        page_renders=page_renders,
    )
    # Before any arm: one oversized act is held without killing other acts or letting
    # the transport chunk its views.
    capacity_finding = over_capacity_reason(autopsia, run.max_images)
    if capacity_finding is not None:
        run.unread -= 1
        return partial(
            _publish_not_run,
            run,
            act,
            ordinal,
            fields=_NOT_RUN_CAPACITY_FIELDS,
            inputs=_reading_image_inputs(context, bases, page_renders, autopsia=autopsia),
            reason=capacity_finding,
            basis={"regions": [], "testimonia": []},
            dissent=[],
            logical_act_id=logical_act_id,
            cross_capture_autopsia=autopsia,
        )

    if adopted is None:
        _refuse_past_deadline(
            run.args.reading_deadline,
            run.unread * run.calls_per_act * PLANNED_SECONDS_PER_CALL,
            f"reading the {run.unread} acts left",
        )
        run.unread -= 1
        if run.reader is None:
            _start_live_reader(run)

    base_dossier = dossier_module.build_dossier(
        context,
        act_id=act_id,
        act_key=act_key,
        regions=bases,
        testimonia=testimonia,
        regime=context.witness_context,
        page_renders=page_renders,
        witness_context=run.witness_context_table,
        act_attachment=attachment_view,
        # Clues about other acts only beside this act's own witnesses; a dossier with
        # none carries neither, and its empty testimonia say so.
        neighbours=_neighbours(run, act, pages={basis["source_page_id"] for basis in bases})
        if testimonia
        else None,
    )
    nuda_sampled, control_sampled = _logical_sampling_decisions(context, logical_act_id)
    attempt = _Attempt(
        act_key=act_key,
        act_id=act_id,
        ordinal=ordinal,
        chair=run.chair,
        bases=bases,
        page_renders=page_renders,
        region_pixels=region_pixels,
        page_pixels=page_pixels,
        smallest_page_pixels=smallest_page_pixels,
        protocol_config=run.protocol_config,
        protocol_sha256=run.protocol_sha256,
        receipt_ref=run.receipt_ref,
    )
    return _PreparedAct(
        act=act,
        attempt=attempt,
        declared_failure=declared_failure,
        testimonia=testimonia,
        attachment_view=attachment_view,
        autopsia=autopsia,
        dossier=base_dossier,
        nuda_sampled=nuda_sampled,
        control_sampled=control_sampled,
        adopted=adopted,
    )


def _call_act(run: _Pass, prepared: _PreparedAct) -> dict[str, Any] | Exception:
    """Every reader call of one act, safe on a worker thread: it publishes nothing.

    An act-local failure is returned, not raised, so it becomes this act's own
    failed Perlectio and never touches another act.
    """
    try:
        return combined.run_logical_passes(
            run.reader,
            autopsia=prepared.autopsia,
            dossier=prepared.dossier,
            read_bytes=run.context.tree.read_bytes,
            protocol_config=run.protocol_config,
            nuda_sampled=prepared.nuda_sampled,
            control_sampled=prepared.control_sampled,
            blind_read=run.context.blind_read,
            # Runs before the establishing arm, which embeds the prior reference it
            # returns. Pass A is only run serially (`_reading_concurrency`), so this
            # always publishes inline on the main thread.
            publish_prior=partial(_publish_lectio_prior, run.context, prepared.attempt),
            saved_prior_failures=_ACT_LOCAL_READING_FAILURES,
            record_prior_failure=partial(
                _publish_lectio_prior_failure, run.context, prepared.attempt
            ),
        )
    except _ACT_LOCAL_READING_FAILURES as error:
        return error


def _publish_act(
    run: _Pass, prepared: _PreparedAct, passes: dict[str, Any] | Exception
) -> dict[str, Any] | None:
    """Publish one act's arms, or its failure, and return its row for the audit."""
    context, attempt, act = run.context, prepared.attempt, prepared.act
    testimonia, attachment_view = prepared.testimonia, prepared.attachment_view
    if isinstance(passes, Exception):
        failure = _failure_record(passes, phase="establishing")
        assert failure is not None  # `_call_act` returns only act-local failures
        _publish_reading_failure(
            context,
            act_id=attempt.act_id,
            act_key=attempt.act_key,
            ordinal=attempt.ordinal,
            inputs=_reading_image_inputs(
                context, attempt.bases, attempt.page_renders, autopsia=prepared.autopsia
            )
            + list(_testimonium_references(context, testimonia).values())
            + [attachment_view["reference"]]
            + _published_arm_refs(context, attempt.act_id, attempt.ordinal)
            + _live_sent_refs(run, attempt.act_id, attempt.act_key, attempt.ordinal, READING_PASS),
            failure=failure,
            reason=f"live Perlector {failure['kind']} failure: {failure['code']}",
            provenance=attempt.provenance(context),
        )
        return None

    if prepared.nuda_sampled:
        _publish_lectio_nuda(
            context,
            attempt,
            passes["lectio-nuda"]["dossier"],
            passes["lectio-nuda"]["result"],
            run.nuda_approval,
        )
    if prepared.control_sampled:
        _publish_primed_without_prior(
            context,
            attempt,
            passes["primed-without-prior"]["dossier"],
            passes["primed-without-prior"]["result"],
            testimonia=testimonia,
            attachment_view=attachment_view,
            approval_ref=run.instrument_approval,
        )
    row = _established_row(
        context,
        attempt,
        act,
        passes["perlectio"],
        order=run.declared_order[attempt.act_id],
        declared_failure=prepared.declared_failure,
        testimonia=testimonia,
        attachment_view=attachment_view,
        autopsia=prepared.autopsia,
    )
    if run.serving_mode == "live":
        _publish_semi_final(run, row)
    return row


def _live_sent_refs(run: _Pass, act_id: str, act_key: str, ordinal: int, pass_name: str):
    if run.serving_mode != "live":
        return []
    return _sent_refs(run.context, act_id, act_key, ordinal, pass_name)


def _publish_semi_final(run: _Pass, row: dict[str, Any]) -> None:
    """Put a live act's main-pass result on record, and bind it into every later record.

    Published as soon as the act's calls return, so a pass that stops before its audit
    leaves nothing a resume would have to ask for again. It names every send it answers.
    """
    context, payload, act_id = run.context, row["payload"], row["act_id"]
    inputs = row["inputs"] + _sent_refs(
        context, act_id, payload["act_key"], payload["attempt_ordinal"], READING_PASS
    )
    fields = row["fields"] - {"audit"}
    validate_reading_payload(
        payload,
        outcome=row["outcome"],
        fields=fields,
        run_id=context.tree.run_id,
        config_digest=context.config_digest,
        protocol_config=run.protocol_config,
        protocol_sha256=run.protocol_sha256,
        inputs=inputs,
    )
    published = context.publish(
        kind=SEMI_FINAL_KIND,
        subject_id=act_id,
        outcome=row["outcome"],
        attempt=perlector_attempt_id(act_id, "perlegere", payload["attempt_ordinal"]),
        inputs=inputs,
        payload=payload,
    )
    row["inputs"].append(context.input_ref(published.relative_path))


def _validate_semi_final(run: _Pass, record: dict[str, Any]) -> None:
    """Refuse a semi-final read back that this run could not have written.

    Its payload is checked as the reading it is, and it must have been made under this
    run's sealed configuration and reading protocol, blind-read setting included.
    """
    context, payload = run.context, record["payload"]
    if record["config_digest"] != context.config_digest or payload.get("protocol") != (
        _protocol_record(context, run.protocol_config)
    ):
        raise ContractError(
            f"act {payload.get('act_key')!r}'s semi-final was made under another "
            "configuration or reading protocol than this run's; read this page in a new run"
        )
    validate_reading_payload(
        payload,
        outcome=record["outcome"],
        fields=(_PERLECTIO_FIELDS - {"audit"})
        | ({"engine_call"} if "engine_call" in payload else set()),
        run_id=context.tree.run_id,
        config_digest=context.config_digest,
        protocol_config=run.protocol_config,
        protocol_sha256=run.protocol_sha256,
        inputs=record["inputs"],
    )


def _adopted_row(run: _Pass, prepared: _PreparedAct, _result: None) -> dict[str, Any]:
    """Rebuild an act's audit row from its retained semi-final, asking nothing.

    The record must have been made from exactly the evidence this act has now: its inputs
    are re-derived here and must match, so a changed crop or witness is refused rather
    than paired with a reading of something else.
    """
    context, attempt, record = run.context, prepared.attempt, prepared.adopted
    payload = record["payload"]
    row = _pending_row(
        context,
        attempt,
        prepared.act,
        payload,
        record["outcome"],
        order=run.declared_order[attempt.act_id],
        declared_failure=prepared.declared_failure,
        testimonia=prepared.testimonia,
        attachment_view=prepared.attachment_view,
        autopsia=prepared.autopsia,
    )
    expected = row["inputs"] + _sent_refs(
        context, attempt.act_id, attempt.act_key, attempt.ordinal, READING_PASS
    )
    if (
        sorted(map(input_order, expected)) != sorted(map(input_order, record["inputs"]))
        or payload["basis"]["regions"] != attempt.bases
    ):
        raise ContractError(
            f"act {attempt.act_key!r}'s retained semi-final was made from other evidence "
            "than the act has now; it is not adopted and the act is not asked again. Read "
            "this page in a new run"
        )
    _validate_semi_final(run, record)
    row["inputs"].append(context.artifact_ref(PERLECTOR, SEMI_FINAL_KIND, record["artifact_id"]))
    return row


def _in_order_window(width: int, jobs) -> list[Any]:
    """Run jobs' calls with at most `width` jobs unfinished; finish every job in order here.

    A job is `(call, finish)`, and `call` is `None` for a job that only publishes. Jobs
    are drawn lazily, so the next is prepared, and its deadline checked, only once fewer
    than `width` are unfinished. `finish` runs on this thread, strictly in job order,
    with what `call` returned (`None` without a call), so records are published in the
    order a serial pass publishes them. Width 1 calls inline, with no thread.

    Every job drawn is finished: if drawing a job, a call or a finish raises, the jobs
    already drawn are still finished in order, so no reply is left without its record,
    and then the first error re-raises with any later ones attached as notes. An
    interrupt stops waiting at once, so the chair can be stopped: it first finishes
    every job whose reply has already arrived, and leaves the calls still out
    unfinished. A reply that arrived is recorded even when an earlier call is still
    out; a record's bytes do not depend on the order it was written in.
    """
    finished: list[Any] = []
    if width == 1:
        for call, finish in jobs:
            finished.append(finish(call() if call is not None else None))
        return finished
    pool = ThreadPoolExecutor(max_workers=width)
    window: deque = deque()

    def finish_first() -> None:
        future, finish = window.popleft()
        finished.append(finish(future.result() if future is not None else None))

    error: Exception | None = None
    try:
        try:
            for call, finish in jobs:
                window.append((pool.submit(call) if call is not None else None, finish))
                while window and (len(window) == width or window[0][0] is None):
                    finish_first()
        except Exception as raised:
            error = raised
        while window:
            try:
                finish_first()
            except Exception as raised:
                if error is None:
                    error = raised
                else:
                    error.add_note(f"a later act also failed: {type(raised).__name__}: {raised}")
    except BaseException as interrupt:
        for future, finish in window:
            if future is not None and (
                not future.done() or future.cancelled() or future.exception() is not None
            ):
                continue
            try:
                finish(future.result() if future is not None else None)
            except Exception as raised:
                interrupt.add_note(f"an arrived reply was not recorded: {raised}")
        raise
    finally:
        pool.shutdown(wait=False, cancel_futures=True)
    if error is not None:
        raise error
    return finished


def _publish_audited_readings(run: _Pass, pending: list[dict[str, Any]]) -> None:
    """Pass C: flag every page once over all Pass-B semi-finals, then finish each act.

    The flags are computed before any re-proof exists, one deterministic cross-act
    computation per page, so no re-proof cascades into another act's flags.
    """
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
    ] + run.sealed_semi_finals
    page_flags = _page_flags(
        run.context,
        semi_finals,
        expected=run.expected,
        recovery_act_id=run.args.act,
        protocol_config=run.protocol_config,
        protocol_sha256=run.protocol_sha256,
        decoding_policy=run.decoding_policy,
    )
    policy_record = audit.policy_record(run.audit_policy, run.audit_sha256)
    _in_order_window(
        run.concurrency,
        (_audit_job(run, row, page_flags[row["act_id"]], policy_record) for row in pending),
    )


@dataclass(frozen=True)
class _Reproof:
    """One delivered Pass-C re-proof: its request, reply, and the text its edits produce."""

    request_digest: str
    reply: dict[str, Any]
    text: str
    edits: list[dict[str, Any]]
    call_record: dict[str, Any] | None
    # A re-proof is evidence whether or not it changed the text, so it is always bound
    # as an input; `engine_call` names it only when its text is published.
    inputs: list[dict[str, str]]


def _audit_job(
    run: _Pass, row: dict[str, Any], flags: list[dict[str, Any]], policy_record: dict[str, Any]
):
    """Freeze the semi-final and return the act's re-proof call, if due, and its finish."""
    round_cap = run.audit_policy["round_cap"]
    draft_payload, draft_publication, draft_ref = _audit_draft(
        run.context, row, flags, round_cap=round_cap, policy_record=policy_record
    )
    finish = partial(
        _publish_audited_reading,
        run,
        row,
        flags,
        policy_record,
        draft_payload,
        draft_publication,
        draft_ref,
    )
    if not audit.reproof_delivery_due(flags, round_cap):
        return None, partial(finish, None)
    request = _reproof_request(run, row, flags, draft_ref)
    _refuse_past_deadline(
        run.args.reading_deadline, PLANNED_SECONDS_PER_CALL, "the next re-proof call"
    )
    if run.serving_mode == "live":
        # A resume whose every act was adopted starts its chair here, at the first call.
        if run.reader is None:
            _start_live_reader(run)
        payload = row["payload"]
        _publish_sent(
            run,
            row["act_id"],
            payload["act_key"],
            payload["attempt_ordinal"],
            REPROOF_PASS,
            presented_image_sha256s(row["autopsia"]),
        )
    return partial(_send_reproof, run, row, request), partial(finish, request)


def _publish_audited_reading(
    run: _Pass,
    row: dict[str, Any],
    flags: list[dict[str, Any]],
    policy_record: dict[str, Any],
    draft_payload: dict[str, Any],
    draft_publication: dict[str, Any],
    draft_ref: dict[str, str],
    request: dict[str, Any] | None,
    reply: dict[str, Any] | Exception | None,
) -> None:
    """Publish the draft, adopt a delivered re-proof if one was due, then finding and Perlectio."""
    context, payload, act_id = run.context, row["payload"], row["act_id"]
    _publish_audit_draft(context, draft_publication, draft_ref)
    round_cap = run.audit_policy["round_cap"]
    # The one function `validate_chain` and `audit_request` also use, so the sealed,
    # delivered and recomputed plans are one computation.
    reproofs = audit.reproof_plan(
        flags, text_length=len(payload["text"]), policy_schema=run.audit_policy["schema"]
    )
    reproof: _Reproof | None = None
    reproof_truncation: dict[str, Any] | None = None
    changes: list[dict[str, Any]] = []
    if request is not None:
        reproof = _delivered_reproof(run, row, draft_ref, request, reply)
        if reproof is None:
            return
        reproof_truncation = _adopt_reproof_text(run, row, reproof)
        # Downstream validation replays these exact edits against the frozen draft.
        changes = audit.change_records_from_edits(reproof.edits)
    # Only an exhausted cap mints spans; an incomplete re-proof is recorded as an
    # incomplete examination for the Recensor to route, never as a span or truncation.
    examination = audit.examination_state(flags, round_cap, reproof_truncation)
    unresolved = audit.unresolved_state(examination)
    uncertainty = (
        _cap_exhausted_spans(flags) if examination == audit.EXAMINATION_CAP_EXHAUSTED else []
    )
    finding_payload = {
        "act_key": row["act"]["act_key"],
        "attempt_ordinal": payload["attempt_ordinal"],
        "page_ids": draft_payload["page_ids"],
        "round_cap": round_cap,
        "policy": policy_record,
        "flags": flags,
        "change_record": changes,
        "uncertain_spans": uncertainty,
        "unresolved": unresolved,
        "examination": examination,
        "reproof_truncation": reproof_truncation,
        "reproof_edits": reproof.edits if reproof else None,
        # None when nothing was re-proofed or the reader is the fixture reader; named here
        # because the Perlectio's `engine_call` stays Pass B's when the text is unchanged.
        "reproof_call": reproof.call_record if reproof else None,
    }
    audit.validate_finding(
        finding_payload,
        # The text this act actually publishes, never a rejected rewrite.
        text=payload["text"],
        flag_text=draft_payload["semi_final_text"],
    )
    attempt_id = perlector_attempt_id(act_id, "perlegere", payload["attempt_ordinal"])
    finding = context.publish(
        kind="audit-finding",
        subject_id=act_id,
        outcome="read",
        attempt=attempt_id,
        inputs=[draft_ref],
        payload=finding_payload,
    )
    finding_ref = context.input_ref(finding.relative_path)
    # An unresolved flag never stays a clean `read`: it becomes an explicit span,
    # unlabelled; only `payload["audit"]` and the finding say which are the audit's.
    payload["uncertain_spans"] = _union_with_projection(
        [
            {"start": span["start"], "end": span["end"], "alternatives": [], "confidence": "low"}
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
        "request_digest": reproof.request_digest if reproof else None,
    }
    # The neighbours' Testimonia join the reading's lineage here and only here: they
    # are what the reading was shown, named by `dossier.neighbours`, and never part of
    # the witness basis, the Pass-C draft or its finding.
    reading_inputs = _audited_reading_inputs(
        row["inputs"] + neighbour_testimonium_refs(payload["dossier"]),
        reproof.inputs if reproof else [],
        draft_ref,
        finding_ref,
    )
    # The consumers' own cross-record validation, run before publication so a drifted
    # draft/finding relationship never becomes an unreadable artifact.
    audit.validate_chain(
        context.tree,
        {"payload": payload, "inputs": reading_inputs},
        act_id,
        length_floor_characters_per_page=_sealed_length_floor(run.protocol_config),
        legible_page_pixels=_sealed_length_floor(run.protocol_config, protocol.LEGIBLE_PAGE_FIELD),
        decoding_policy=run.decoding_policy,
    )
    validate_reading_payload(
        payload,
        outcome=row["outcome"],
        fields=row["fields"],
        run_id=context.tree.run_id,
        config_digest=context.config_digest,
        protocol_config=run.protocol_config,
        protocol_sha256=run.protocol_sha256,
        inputs=reading_inputs,
    )
    context.publish(
        kind="perlectio",
        subject_id=act_id,
        outcome=row["outcome"],
        attempt=attempt_id,
        inputs=reading_inputs,
        payload=payload,
    )


def _audited_reading_inputs(
    established: list[dict[str, str]],
    reproof_inputs: list[dict[str, str]],
    draft_ref: dict[str, str],
    finding_ref: dict[str, str],
) -> list[dict[str, str]]:
    """Dedup only the re-proof's references, which can repeat the establishing call's bytes.

    A duplicate among the established inputs stays under the envelope's own refusal.
    """
    return (
        established
        + [reference for reference in distinct_refs(reproof_inputs) if reference not in established]
        + [draft_ref, finding_ref]
    )


def _reproof_request(
    run: _Pass, row: dict[str, Any], flags: list[dict[str, Any]], draft_ref: dict[str, str]
) -> dict[str, Any]:
    """The audit request asking the chair to re-proof this act's flagged spans."""
    payload = row["payload"]
    # The request carries the frozen semi-final its locations index into, bound by the
    # published draft's digest; the dossier goes through untouched so its
    # `dossier_digest` still covers it.
    request = audit.audit_request(
        act_key=row["act"]["act_key"],
        attempt_ordinal=payload["attempt_ordinal"],
        draft_ref=draft_ref,
        semi_final_text=payload["text"],
        flags=flags,
        policy_schema=run.audit_policy["schema"],
    )
    # Enforced by the producer so it binds whichever reader sits in the chair.
    validate_audit_delivery(payload["dossier"], pass_kind="audit-reproof", audit_request=request)
    return request


def _send_reproof(
    run: _Pass, row: dict[str, Any], request: dict[str, Any]
) -> dict[str, Any] | Exception:
    """The one re-proof call, safe on a worker thread: it publishes nothing.

    An act-local failure is returned, not raised, so only this act records it.
    """
    # One reader call per act and round over the complete atomic presentation; a
    # flagged-page subset would be a capture-local call.
    pixels = atomic_delivered_pixels(
        row["autopsia"], read_bytes=run.context.tree.read_bytes, max_images=run.max_images
    )
    try:
        return run.reader.read(
            row["payload"]["dossier"],
            pass_kind="audit-reproof",
            delivered_pixels=pixels,
            audit_request=copy.deepcopy(request),
        )
    except _ACT_LOCAL_READING_FAILURES as error:
        return error


def _delivered_reproof(
    run: _Pass,
    row: dict[str, Any],
    draft_ref: dict[str, str],
    request: dict[str, Any],
    reply: dict[str, Any] | Exception,
) -> _Reproof | None:
    """Assemble a delivered re-proof; `None` once a failure is published instead."""
    context, payload = run.context, row["payload"]
    base_prompt_record = copy.deepcopy(payload["prompt"])
    base_prompt_text = prompts.build_prompt(
        run.chair.serving_recipe, run.chair.role, payload["dossier"], run.protocol_config
    )
    request_digest = audit.audit_digest(request)
    if isinstance(reply, Exception):
        failure = _failure_record(reply, phase="audit-reproof")
        assert failure is not None
        _publish_reproof_failure(
            run,
            row,
            inputs=row["inputs"]
            + [draft_ref]
            + _published_arm_refs(context, row["act_id"], payload["attempt_ordinal"])
            + _reproof_sent_refs(run, row),
            failure=failure,
            reason=f"live Perlector {failure['kind']} failure during audit re-proof: {failure['code']}",
        )
        return None
    # A Pass-C reply is exact draft-anchored edits, never a second whole reading;
    # assembly validates every edit before it touches the established text.
    try:
        text, response = audit.assemble_reproof_response(reply["text"], request)
        # Publishing whitespace as the empty reading would remove characters outside
        # the exact edits the response accounted for.
        if _publishes_empty(text) and text not in {"", payload["text"]}:
            raise audit.ReproofResponseRefusal(
                "an audit re-proof response becomes a whole-act empty projection outside "
                "its exact requested edits"
            )
        edits = copy.deepcopy(response["edits"])
        call_record = _reproof_call(
            reply, base_prompt=base_prompt_record, base_text=base_prompt_text, request=request
        )
    except audit.ReproofResponseRefusal as error:
        _publish_reproof_failure(
            run,
            row,
            inputs=row["inputs"]
            + [draft_ref]
            + engine_call_inputs(context, reply.get("engine_call"), variance_arm=None)
            + _published_arm_refs(context, row["act_id"], payload["attempt_ordinal"])
            + _reproof_sent_refs(run, row),
            failure=_failure_from_engine_call(context, reply.get("engine_call"), detail=str(error)),
            reason="the delivered audit re-proof response could not be assembled safely",
        )
        return None
    return _Reproof(
        request_digest=request_digest,
        reply=reply,
        text=text,
        edits=edits,
        call_record=call_record,
        inputs=engine_call_inputs(context, reply.get("engine_call"), variance_arm=None)
        + _reproof_sent_refs(run, row),
    )


def _reproof_sent_refs(run: _Pass, row: dict[str, Any]) -> list[dict[str, str]]:
    payload = row["payload"]
    return _live_sent_refs(
        run, row["act_id"], payload["act_key"], payload["attempt_ordinal"], REPROOF_PASS
    )


def _publish_reproof_failure(
    run: _Pass,
    row: dict[str, Any],
    *,
    inputs: list[dict[str, str]],
    failure: dict[str, Any],
    reason: str,
) -> None:
    payload = row["payload"]
    _publish_reading_failure(
        run.context,
        act_id=row["act_id"],
        act_key=row["act"]["act_key"],
        ordinal=payload["attempt_ordinal"],
        inputs=inputs,
        failure=failure,
        reason=reason,
        # The session whose call failed: after a resume it is not the one that read Pass B,
        # whose own provenance stays on the semi-final this record binds.
        provenance=provenance_for(
            run.context, run.chair, attempted=True, receipt_ref=run.receipt_ref
        ),
    )


def _adopt_reproof_text(run: _Pass, row: dict[str, Any], reproof: _Reproof) -> dict[str, Any]:
    """Publish a re-proof's changed text in place of Pass B's; return its truncation verdict.

    The verdict is measured before the text is compared with the semi-final: a cut-off
    re-proof that returned the established text verbatim must not pass as complete.
    Unchanged text keeps Pass B's provenance.
    """
    payload, reply, final_text = row["payload"], reproof.reply, reproof.text
    reproof_truncation = _row_truncation(
        row, final_text, stop_reason=reply["stop_reason"], protocol_config=run.protocol_config
    )
    if final_text == payload["text"]:
        return reproof_truncation
    payload["text"] = final_text
    # The doubt report travels with the call whose text is published, so nothing is
    # re-anchored by guesswork; this also drops a Pass-B whole-act gap, which a
    # re-proof's own report can never carry.
    reproof_assessment = _assessed(reply, text=final_text)
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
    # `engine_call` moves with the text, so a published reading never names a response
    # that did not produce it.
    row["fields"] = with_engine_call(payload, reply, row["fields"])
    if reproof.call_record is not None:
        payload["prompt"] = copy.deepcopy(reproof.call_record["audit_prompt"])
    payload["dissent"] = dissent_against(
        final_text, dissent_testimonia(row["testimonia"], row["attachment_view"])
    )
    payload["self_revision"] = self_revision_for_view(
        payload["dossier"]["prior_draft_view"], final_text, _prior_text(row["prior"]), departures
    )
    payload["truncation"] = _audited_truncation(
        pass_b=payload["truncation"],
        declared_failure=row["declared_failure"],
        text=final_text,
        region_pixels=row["region_pixels"],
        page_pixels=row["page_pixels"],
        smallest_page_pixels=row["smallest_page_pixels"],
        truncation_policy=run.protocol_config[protocol.TRUNCATION_TABLE],
        stop_reason=reply["stop_reason"],
        measured=reproof_truncation,
    )
    row["outcome"] = _resolve_outcome(
        declared_failure=row["declared_failure"],
        truncation_record=payload["truncation"],
        text=final_text,
    )
    if row["outcome"] == "no-readable-text":
        # As on the Pass-B path, unreadable text is published empty with the whole-act
        # gap carrying the witness evidence. `validate_finding` binds the sealed
        # termination to the published text, so it is re-measured over the empty text.
        payload["text"] = ""
        reproof_truncation = _row_truncation(
            row, "", stop_reason=reply["stop_reason"], protocol_config=run.protocol_config
        )
        # Re-asked against the empty text, so the record never says `assessed` over a
        # report that was thrown away.
        (
            payload["uncertainty_assessment"],
            payload["uncertain_spans"],
            payload["gaps"],
        ) = _published_doubt(
            reply,
            text="",
            outcome="no-readable-text",
            whole_act_gaps=_whole_act_gap(
                row["testimonia"], _testimonium_references(run.context, row["testimonia"])
            ),
        )
        if reproof_assessment["state"] == "malformed":
            payload["uncertainty_assessment"] = _sealed_assessment(reproof_assessment)
        payload["dissent"] = dissent_against(
            "", dissent_testimonia(row["testimonia"], row["attachment_view"])
        )
        payload["self_revision"] = self_revision_for_view(
            payload["dossier"]["prior_draft_view"], "", _prior_text(row["prior"]), departures
        )
    return reproof_truncation


def _witnessed_act(
    context,
    act: dict[str, Any],
    regions: list[dict],
    proposal_regions: list[dict],
    *,
    all_proposal_regions: list[dict],
    reported_unrouted: set[tuple[str, int]],
) -> tuple[list[dict], list[dict], dict[str, Any]]:
    """Verify every region of the act and the testimony about it: bases, testimonia, attachment.

    Every region is read, including a continuation on the next page: an act read only up
    to the fold would be truncated, a failure and not an output.
    """
    bases = [verify_region(context, region) for region in regions]
    testimonia = testimonia_of(context, act["act_id"], proposal_regions)
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
    # Both witness scopes use the run-wide proposal denominator; page testimony is
    # deduplicated so an observation is named once, not once per act.
    for finding in unrouted_observations(
        testimonia + list(page_testimonia.values()),
        all_proposal_regions,
        prior_findings=reported_unrouted,
    ):
        reported_unrouted.add((finding["testimonium_id"], finding["ordinal"]))
        # Never normalized into the nearest act; the Recensor re-derives it independently.
        print(f"non-fatal finding: {finding}", file=sys.stderr)
    # Ink uncovered only by a recovery recrop was never shown to a witness; recording that
    # keeps the gap visible. The reading itself is unaffected.
    witnessed = witnessed_region_ids(testimonia, bases)
    for basis in bases:
        basis["witness_covered"] = basis["region_id"] in witnessed
    return bases, testimonia, attachment_view


def _neighbour_clue(
    run: _Pass, act: dict[str, Any]
) -> tuple[set[str], list[dict[str, Any]], str | None]:
    """One act's pages, uncut witness readings, and why they are missing if they are.

    Read only from the sealed Designator regions and Attestatores records, through
    the same validation the act's own dossier uses, never from its Perlectio: a
    reading depends on no other act's reading, so parallel calls and a narrowed
    `--act` pass build the same dossier. A neighbour's own defect is its own act's
    to answer for when that act is read; here the clue is withheld with the reason
    recorded, and the act beside it is still read. Nothing is printed: a neighbour's
    unrouted observations are reported when it is read.
    """
    act_id = act["act_id"]
    if act_id not in run.neighbour_clues:
        if act["outcome"] == "held":
            clue = ({act["page_id"]}, [], "the Designator held this act; no witness read it")
        else:
            try:
                regions, proposal_regions = act_regions(run.context, act_id)
                bases = [verify_region(run.context, region) for region in regions]
                testimonia = testimonia_of(run.context, act_id, proposal_regions)
                attachment_view = act_attachment_view(
                    run.context,
                    act,
                    testimonia,
                    bases,
                    {region["payload"]["region_id"] for region in proposal_regions},
                    all_proposal_regions=run.all_proposal_regions,
                )
                clue = (
                    {basis["source_page_id"] for basis in bases},
                    dossier_module.neighbour_witnesses(
                        run.context,
                        testimonia=testimonia,
                        regime=run.context.witness_context,
                        witness_context=run.witness_context_table,
                        act_attachment=attachment_view,
                    ),
                    None,
                )
            except ContractError as error:
                clue = (
                    {act["page_id"]},
                    [],
                    f"its witness records did not validate: {type(error).__name__}: {error}",
                )
        run.neighbour_clues[act_id] = clue
    return run.neighbour_clues[act_id]


def _neighbours(run: _Pass, act: dict[str, Any], *, pages: set[str]) -> dict[str, Any]:
    """The acts before and after this one in the Designator's sequence, across page
    breaks, each with every witness's reading cut to the sealed cap; null at either
    end of the run."""
    position = run.declared_order[act["act_id"]]
    cap = run.protocol_config[protocol.NEIGHBOURS_TABLE]["characters_per_row"]
    entries: dict[str, Any] = {}
    for side, index in (
        (dossier_module.PRECEDING, position - 1),
        (dossier_module.FOLLOWING, position + 1),
    ):
        if not 0 <= index < len(run.expected):
            entries[side] = None
            continue
        neighbour = run.expected[index]
        neighbour_pages, witnesses, unavailable = _neighbour_clue(run, neighbour)
        entries[side] = dossier_module.neighbour_entry(
            neighbour,
            side=side,
            same_page=bool(neighbour_pages & pages),
            witnesses=witnesses,
            characters_per_row=cap,
            unavailable=unavailable,
        )
    return entries


def _next_attempt(context, act_id: str, regions: list[dict]) -> int:
    """Which reading attempt this is, derived from the act rather than from history.

    One reading of the proposal plus one per recovery region cut since, so a rerun that
    changed nothing recomputes the same ordinal and reuses the same bytes. Witness
    testimony is not counted: a Testimonium primes a reading and never makes a new
    attempt.

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
