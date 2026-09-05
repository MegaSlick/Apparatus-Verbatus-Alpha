"""Perlector: reads the ink, with the testimonia as fallible clues.

The fake here proves wiring and nothing else — its text comes from the fixture, so
it demonstrates exactly zero about reading. What it *does* prove is the shape of
the record, and the shape is where GOVERNANCE 3 either holds or quietly fails:

  It verifies the region evidence.        The stage reads the bytes, checks their
                                          digest against the sealed reference, and
                                          decodes them to confirm the image is the
                                          size the transform claims. The fixture
                                          reader observes pixels only to prove a
                                          page-fallback act empty; ordinary text
                                          remains declared fixture text.
  It records its basis.                   The region it read, and every testimonium
                                          it saw, by reference.
  It never counts witnesses.              No branch anywhere in this file reads how
                                          many chairs agreed. The dissent record is
                                          computed *after* the reading is fixed,
                                          and cannot reach back into it.

Which reader answers is the sealed serving-recipe row's business, not this file's: a
`kind = "vllm"` row for the resolved Perlector chair selects `live_reader.VLLMReader`
behind one `ChairClient`, and every other catalogue selects the fixture reader above.
Nothing downstream of the reader call changes with that choice — the record shape, the
truncation instrument and the Recensor's routing are the same either way. HANDOFF.md's
"Live reader" section carries the mapping, the refusals and the resume rule. On a real
submission there is no declaration for the fixture reader to read from, so a non-live
row there refuses by name (`fixture_reader_for`), and a declared reading failure has no
counterpart: a real reading's non-completion is the engine's own stop reason.

Dissent is structural, not evaluative: it records where the reading departed from
each witness, which makes parroting measurable without new instrumentation. It is
not a quality signal — most lines in a register are easy and every witness agrees,
and zero dissent there is the correct output.

    python pipeline/4_perlector/run.py --run-root <dir> --run-id <id>
"""

import copy
import json
import os
import stat
import sys
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
from live_reader import VLLMReader  # noqa: E402
from reader import FixtureReader, validate_audit_delivery  # noqa: E402

from common.alignment import markup_text_view  # noqa: E402
from common.chairs.models import AbsentChair, ChairIdentity  # noqa: E402
from common.chairs.registry import ChairRegistry  # noqa: E402
from common.contracts.approval import (  # noqa: E402
    ApprovalRecordBinding,
    ApprovalRecordReference,
    validate_approval_record,
)
from common.contracts.canonical import canonical_bytes, digest_bytes, digest_of  # noqa: E402
from common.contracts.envelope import validate_input_refs  # noqa: E402
from common.contracts.errors import (  # noqa: E402
    ApprovalRefusal,
    ContractError,
    FatalAccounting,
    SchemaRefusal,
)
from common.contracts.identities import artifact_id, perlector_attempt_id  # noqa: E402
from common.contracts.outcomes import ATTACHMENT_BASES  # noqa: E402
from common.contracts.stages import (  # noqa: E402
    ATTESTATORES,
    DESIGNATOR,
    EXEMPLAR,
    PERLECTOR,
    writing_directory,
)
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
    stage_parser,
    validate_serving_provenance,
)
from operations.serving.client import ChairClient, serving_mode_for  # noqa: E402
from operations.serving.config import (  # noqa: E402
    ServingConfigInputs,
    load_serving_recipes,
)
from operations.serving.http import UrllibHttpTransport  # noqa: E402
from operations.serving.manager import (  # noqa: E402
    ServingManager,
    StageContextReceiptPublisher,
)
from operations.serving.process import SubprocessLauncher  # noqa: E402
from operations.serving.residency import FileResidencyLease  # noqa: E402

# A sampling gate has to inspect the shared receipt directory because the sealed
# experiment selector cannot contain the content address of the approval that
# targets that selector's own config digest.  Bounds turn a planted directory
# into a named refusal instead of an unbounded preflight.  Real receipts are a
# few kilobytes; these ceilings allow a large alpha run while keeping both one
# object and the aggregate scan finite.
# A live chair's engine logs and its residency lease, both inside the run tree so
# they travel with the evidence they belong to. The logs sit beside this stage's
# artifacts and blobs rather than among them: `_stage_blob_inventory` walks
# `<stage>/blobs` alone, so an engine still writing its log while the stage seals
# cannot make the witnessed inventory false. The lease is run-scoped, not
# stage-scoped, because the card is: the Attestatores' witness chairs and this
# reader must contend for one lock, or two stages co-reside on one GPU.
SERVING_LOG_DIRECTORY: Final = "serving-logs"
RESIDENCY_LOCK_FILE: Final = "pod-gpu.lock"

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
    """Open the canonical receipt directory one component at a time, no-follow.

    ``RunTree.resolve`` deliberately resolves symlinks before its spelling-based
    containment check.  That is suitable for ordinary movable references, but
    not for this security gate: a receipt alias could be changed through its
    other name after approval was checked.  Directory descriptors keep every
    lookup anchored to the inode that was actually opened.
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


def _sampling_receipts(context, *, subject: str) -> list[tuple[ApprovalRecordReference, dict]]:
    """Read every receipt once and return validated records for ``subject``."""
    directory = _open_receipts_directory(context.tree)
    if directory is None:
        return []
    try:
        directory_before = _stable_file_metadata(os.fstat(directory))
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

        records: list[tuple[ApprovalRecordReference, dict]] = []
        scanned_bytes = 0
        for name in sorted(names):
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

    The sealed selector avoids a hash fixed point: an approval cannot both be
    addressed by its content and target a configuration containing that address.
    Candidates pass path, digest, schema, self-hash, approver, sole-subject,
    action, and exact-version checks before the binding reaches either arm.

    The record authenticates integrity and a claimed approver, not authorship;
    run-tree write authority remains the trust boundary. Authenticating the human
    act itself would require an out-of-band signature.
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
            "cannot draw without Tyrel's typed approval record. Expected one record "
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
    # "exclusion" and "salvage-promotion" approve a different governed action entirely
    # (GOVERNANCE 1); a sampling design is filed under "other" so a record meant to
    # authorize an exclusion can never double as a sampling approval by coincidence
    # of subject text.
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
    """Every Designator region for this act, its own provenance verified first.

    `pipeline/3_attestatores/run.py::proposed_regions` already validates the
    identical artifact kind before showing a region to a witness; reading it here
    unvalidated would let a tampered Designator provenance reach a real reading
    while the equivalent tamper on a Testimonium is refused. A region always
    carries a receipt-backed provenance -- `structure_provenance` refuses before
    any region is cut if the Designator chair is absent or unverifiable -- so
    every region validated here requires one.
    """
    records = []
    for entry in context.tree.build_manifest(DESIGNATOR)["artifacts"]:
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
    """The sort key, refused by name rather than escaping as a raw `KeyError`.

    Ordering happens before `verify_region` validates the region, so this is
    the one place a resealed record whose payload lost `attempt_ordinal` is
    read — and a stage boundary that answers untrusted input with a traceback
    is the one thing the Designator's own tests assert never appears in stderr.
    """
    ordinal = record.get("payload", {}).get("attempt_ordinal")
    if not isinstance(ordinal, int) or isinstance(ordinal, bool):
        raise SchemaRefusal("a Designator region carries no integer attempt ordinal to order by")
    return ordinal


def act_regions(context, act_id: str) -> tuple[list[dict], list[dict]]:
    """Every region for this act, and the original-proposal subset of it.

    One spelling, because the preflight and the reading loop both need the pair
    and both refuse the same way. A second copy of the filter is how the two
    come to disagree about what counts as a proposal region, which is the fact
    the witness-coverage record is built on.
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
    """Validate native presentation, rather than the retired shared-crop premise."""
    payload = record["payload"]
    presented = payload.get("presented") if isinstance(payload, dict) else None
    if not isinstance(presented, dict):
        raise SchemaRefusal("a Testimonium has no presented native witness block")
    validate_native_witness_geometry(payload)
    unpresented = payload.get("unpresented_regions")
    attempted = record["outcome"] in ATTEMPTED_WITNESS_OUTCOMES
    if not attempted:
        # Ahead of the image-evidence refusal, which names none of this: a
        # stripped record whose retained response survives passes that rule and
        # still holds the bytes the chair answered with.
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
    inputs = {}
    for region in proposal_regions:
        reference = context.input_ref(region["payload"]["image_path"])
        inputs[reference["relative_path"]] = reference
    reference = context.input_ref(presented["image_path"])
    inputs[reference["relative_path"]] = reference
    expected_inputs = sorted(
        inputs.values(), key=lambda item: (item["relative_path"], item["sha256"])
    )
    # Re-derive the explicit limit for every presentation kind so a kind change
    # cannot understate which bound crops its one page-space image omits.
    if unpresented != unpresented_region_ids(presented, proposal_regions):
        raise SchemaRefusal(
            "a Testimonium does not name exactly the bound proposal regions its presentation "
            "does not speak for"
        )

    # One spelling of this refusal, called from both paths rather than hoisted
    # above them. Order is load-bearing: a forged region presentation also has
    # the wrong inputs, and checking those first would answer "wrong blobs" for
    # a record whose actual fault is that it presents a recovery crop as a
    # witness basis. The specific fault has to be the one the operator reads.
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
        # Same order and same reason as the act-scoped seam above: the retained
        # response is checked first because the image-evidence refusal below
        # does not mention it, and a stripped record keeps it.
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
        page = context.tree.read_artifact(
            EXEMPLAR,
            "page",
            artifact_id(EXEMPLAR, "page", presented["source_page_id"]),
        )
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
        expected_inputs = [
            {"relative_path": presented["image_path"], "sha256": presented["image_sha256"]}
        ]
        # Every retained response the record derived from, in the payload's own
        # order: the partition's responses, then a native capture. Each is
        # bound beside the presented pixels so an ordinary artifact read
        # re-hashes it, rather than trusting a nested reference nobody opens.
        retained = list(payload.get("raw_response_refs", []))
        capture = payload.get("native_capture")
        if capture is not None:
            retained.append(capture["raw_response_ref"])
        # A native capture that parsed reaches the same retained response the
        # partition already named (`live_witness.captured_page_attempt`, the
        # chandra.v1 branch): the writer names that reference once
        # (`_named_once`, 3_attestatores/run.py), and the reader must count it
        # once too, or a page record binding one blob twice would be refused
        # for the very thing it did right. Order-preserving, keyed on the
        # reference's own identity, so two genuinely distinct responses still
        # count as two.
        seen_retained: set[tuple[str, str]] = set()
        deduped_retained = []
        for reference in retained:
            key = (reference["relative_path"], reference["sha256"])
            if key in seen_retained:
                continue
            seen_retained.add(key)
            deduped_retained.append(reference)
        # Sorted the way the envelope stores inputs, exactly as the act-scoped
        # seam above does. Comparing against the payload's own order passes only
        # while the retained paths happen to sort after the presented image.
        expected_inputs = sorted(
            expected_inputs + deduped_retained,
            key=lambda item: (item["relative_path"], item["sha256"]),
        )
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
    for entry in context.tree.build_manifest(DESIGNATOR)["artifacts"]:
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
    """Every chair's current testimonium for this act — the latest attempt only.

    Attempts are append-only (GOVERNANCE 4): a failed re-read is recorded beside
    the earlier success, never over it. Every record is still read and its
    provenance still validated, but only each chair's latest attempt is returned
    as evidence — the same collapsing `pipeline/5_recensor/run.py::chair_outcomes`
    already does for the identical artifacts, so dissent, witness-coverage, and the
    Perlectio's own recorded basis cannot see a superseded attempt as though it
    were still live.
    """
    records = []
    for entry in context.tree.build_manifest(ATTESTATORES)["artifacts"]:
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
    """Read page scope independently from the sealed model configuration.

    A consumer may not inherit trust across a stage boundary: this side must not
    take the Attestatores' validation, or a fixture declaration, for the sealed
    authority. Uniqueness keeps duplicates from disappearing into a set, and the
    roster check below prevents a scope claim for a nonexistent chair from
    silently erasing page coverage.
    """
    roster = context.witness_chairs
    # `type(chair) is not str` rather than `isinstance`: set construction and
    # refusal formatting both invoke subclass-defined behaviour, so the exact
    # built-in string is required first (work/boundary-named-refusals).
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

    `type(chair) is not str` rather than `isinstance`: the value becomes a set
    and dict key below, and both set construction and refusal formatting invoke
    subclass-defined behaviour, so the exact built-in string is required first.
    Kept in one place because a second, looser copy of a closed schema is how a
    field added to one list quietly escapes validation in the other.
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
    """Validate the R0 attachment that makes a page witness act-addressable.

    R4 owns alignment; until then this is the chair's complete delivered act
    reading as an interim span, retained beside the page Testimonium and surfaced
    in the dossier rather than silently treating page completion as an act-level
    read.

    `testimonia` is this act's *current* attempt per chair, already collapsed by
    `testimonia_of`. The attachment is a derived view of one attempt, so it is
    checked against that collapse rather than trusted on its own: see the
    per-chair reconciliation below. `bases` are the verified regions the
    Perlector read and therefore the independent page denominator.
    """
    act_id = act["act_id"]
    current = {record["payload"]["chair"]: record for record in testimonia}
    entries = [
        entry
        for entry in context.tree.build_manifest(ATTESTATORES)["artifacts"]
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
    # The run-global scope reading is validated first, and by the strictest of
    # the readings that met here: exact strings, no duplicates hiding in a set,
    # and no chair outside this run's sealed roster. It must fail before any
    # per-attachment diagnostic can misattribute its malformation to a chair record.
    page_chairs = declared_page_witness_chairs(context)
    # Validate every value that becomes a set/dict key before pair accounting.
    # JSON booleans compare equal to integers in Python (`True == 1`), and an
    # unhashable JSON value would otherwise escape as a raw TypeError here. The
    # attachment is untrusted evidence read from disk, so neither may reach the
    # denominator as though it named a real page.
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
    pairs = [
        (attachment.get("chair"), attachment.get("page_ordinal"))
        if isinstance(attachment, dict)
        else (None, None)
        for attachment in attachments
    ]
    if len(pairs) != len(set(pairs)) or set(pairs) != expected_pairs:
        raise FatalAccounting(
            f"act {act_id} attachments do not cover every contributing page/witness pair; "
            "its witness denominator is duplicated or incomplete; rebuild the attachment "
            "from the sealed regions and configured chairs"
        )
    # Attachment accounting is per (chair, page), but this dossier field names
    # witnesses. Count a chair once even when its evidence spans several pages;
    # otherwise the reader is told a two-chair roster contains four witnesses.
    page_witness_chairs: set[str] = set()
    comparison_views: dict[str, str] = {}
    edge_deltas: dict[str, list[dict[str, Any]]] = {}
    for attachment in attachments:
        _validate_attachment_shape(attachment)
        span = attachment["span"]
        characters = attachment["content_health"].get("characters")
        if attachment["attached"] and not attachment["page_witness"]:
            if attachment["attachment_basis"] != "presented-region":
                raise SchemaRefusal("an act-scoped attachment has no presented-region basis")
            expected_end = (
                characters
                if isinstance(characters, int) and not isinstance(characters, bool)
                else 0
            )
            if span != {"start": 0, "end": expected_end}:
                raise SchemaRefusal(
                    "an attached act view does not span its complete delivered reading"
                )
        if attachment["comparable"] and not attachment["attached"]:
            raise SchemaRefusal(
                "an unattached act view cannot claim comparable text. "
                "Text cannot count for an act when witness geometry did not attach to it. "
                "Rebuild both facts from the retained Testimonium."
            )
        # A plain `if`, not an `elif`. The two rules are independent -- one about
        # comparable text without attachment, one about a span or basis an
        # unattached view may not carry -- and chained, the second read as the
        # alternative to the first, which is not what it checks. The outcomes are
        # the same either way; what changes is where the next branch added here
        # gets wired.
        #
        # Two separate faults inside it, and each names its own field. This branch
        # is reached by page-witness attachments as well as act-scoped ones, so the
        # refusal may not call every row an "act view"; and a row whose only fault
        # is its basis sent the operator to a `span` that was already null.
        if not attachment["attached"]:
            if attachment["attachment_basis"] != "unattached":
                raise SchemaRefusal(
                    "an unattached attachment names an attachment basis other than "
                    "'unattached'; nothing attached it, so nothing decided the basis"
                )
            if span is not None:
                raise SchemaRefusal("an unattached attachment claims an alignment span")
        chair = attachment["chair"]
        attachment_page = attachment["page_ordinal"]
        # The attachment describes one attempt, and the reread path
        # (`pipeline/3_attestatores/run.py::reread_pass`) appends a new act-scoped
        # attempt without rewriting it — D8 leaves page-witness reread addressing
        # to R3. Unchecked, the record then presents a superseded attempt's
        # outcome and delivered-character count as this act's live attachment,
        # which is exactly what `testimonia_of`'s latest-attempt collapse exists
        # to stop ("cannot see a superseded attempt as though it were still
        # live"), and what R0's own `granularity_basis` claims is impossible:
        # `attached` IS the current act outcome before R4 alignment. Refuse the
        # divergence rather than count a stale span or a stale reading as
        # current. R4 replaces this reconciliation with real alignment. Found in
        # audit; F-O1.
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
        # NOT exempted for a page witness, unlike the `attached`/outcome check just
        # above: `content_health` is recorded from this act's own per-chair attempt
        # (`attempts_by_pair` in `pipeline/3_attestatores/run.py`) for every chair,
        # page witness or not -- a targeted reread (`reread_pass`) appends a new
        # attempt to that exact same per-(act, chair) `testimonium` stream whether
        # or not the chair is page-scoped, so a page witness's attachment can go
        # stale after a reread exactly as an act-scoped one can (REOPENED F-O1):
        # `attached` legitimately differs from the chair's outcome for a page
        # witness (alignment can honestly fail against live text), but the health
        # of the attempt the attachment actually describes must always still be
        # this chair's current one.
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
        # The act-scoped Testimonium carries the same scope claim a second time, as
        # its optional `page_witness` flag, and `pipeline/4_perlector/dissent.py`
        # trusts that flag directly: a record wearing it emits `compared: "unknown"`
        # instead of a real comparison. The attachment's copy is checked above and
        # this one was checked nowhere, so a resealed Testimonium for an ordinary
        # act-scoped chair could silence that chair's dissent row — the structural
        # parroting instrument switched off behind a well-formed and plausible
        # reason, which is the one failure mode ARCHITECTURE's dissent section
        # exists to make measurable. Two spellings of one fact, so both are
        # reconciled against the run's own declaration. Found in fresh-context
        # review (P2).
        if chair_testimonium["payload"].get("page_witness", False) is not expected_page_witness:
            raise SchemaRefusal(
                f"act {act_id} Testimonium for chair {chair!r} claims a page-witness scope this "
                "run did not declare"
            )
        reference = attachment.get("testimonium_ref")
        if attachment["page_witness"]:
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
            # Collected for the caller's routing sweep rather than examined here:
            # a page Testimonium belongs to a (page, chair) pair, not to this act,
            # so its observations must be judged once per run and not once per act
            # that happens to sit on the page. This is the only digest-checked read
            # of these records the Perlector makes, which is why the sink hangs
            # here instead of a second walk of the Attestatores manifest -- a
            # second walk would need its own current-attempt collapse and would be
            # the third mirror of the Recensor's (see `current_page_testimonia`).
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
            # The SEALED PROPOSAL geometry, never every current basis region.
            # The writer computes this attachment from `proposed_regions`
            # (`pipeline/3_attestatores/run.py`) and cannot do otherwise: a
            # recovery region does not exist when a witness runs, and the reread
            # rule forbids new testimony after a reading. Re-deriving here over
            # a recovery crop as well therefore does not check the writer, it
            # contradicts it -- and it contradicts it in exactly the case Unit
            # 10C exists for. A page witness reporting ink outside every
            # proposal routes to a fallback recrop; the expanded crop then
            # overlaps the observation the proposal missed, and the reread
            # refused the act's own attachment record as forged. That is
            # retrospective coverage arriving through the attachment door
            # (consult 4.1, wall 1: a recovery crop may not become coverage
            # after the fact), and it turned a recoverable coverage finding
            # into a hard stage failure.
            # A recovery crop postdates testimony and therefore cannot expand
            # the sealed-proposal denominator used to attach that testimony.
            page_bases = [
                basis
                for basis in bases
                if basis["source_page_ordinal"] == attachment_page
                and basis["region_id"] in proposal_region_ids
            ]
            # Native page and compatibility act outcomes are independent; legacy
            # page joins instead derive their outcome from the act attempts.
            attachment_outcome = (
                testimonium["outcome"]
                if native_capture is not None
                else chair_testimonium["outcome"]
            )
            geometrically_attached = attachment_outcome in WITNESS_READING_OUTCOMES and any(
                reported_geometry_overlaps(
                    page_payload.get("observed", []), basis["transform"]["bounds"]
                )
                for basis in page_bases
            )
            if attachment["attached"] != geometrically_attached:
                raise SchemaRefusal(
                    f"act {act_id} page attachment for chair {chair!r} does not derive from "
                    "that witness's reported geometry against the sealed proposal"
                )
            edge_deltas.setdefault(chair, []).extend(
                sealed_proposal_edge_deltas(page_payload, page_bases)
            )
            unjoined = (
                page_payload.get("unjoined_act_attempts")
                if isinstance(page_payload, dict)
                else None
            )
            if (
                not isinstance(page_payload, dict)
                or page_payload.get("chair") != chair
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
            if not is_act_primary_page and (
                attachment["attached"]
                or attachment["alignment"]
                != {
                    "status": "unaligned",
                    "reason": "continuation-page-no-act-anchor",
                }
            ):
                raise SchemaRefusal(
                    f"act {act_id} continuation-page attachment for chair {chair!r} claims "
                    "an act anchor; this compatibility record has no page-specific anchor; "
                    "retain it as continuation-page-no-act-anchor"
                )
            current_unjoined = [row for row in unjoined if row["act_id"] == act_id]
            if len(current_unjoined) > 1:
                raise SchemaRefusal(
                    f"act {act_id} appears more than once in a page Testimonium's "
                    "unjoined-attempt record"
                )
            # An act the page join omitted is disclosed with the attempt outcome
            # that explains it, and that outcome must be the one the attachment
            # records. Not `bool(rows) != attached`, which was this check before:
            # it read every omission as a failure, so the moment the join could
            # omit a genuine reading -- a structured native object it cannot
            # concatenate -- the honest disclosure of that omission became a
            # refusal, and staying silent stayed legal. Absence of a row still
            # means the act joined, so an unattached act must always be named.
            # Found in audit; F-O7.
            row = current_unjoined[0] if current_unjoined else None
            disclosed = row["outcome"] in WITNESS_READING_OUTCOMES if row is not None else True
            # Joining a response into the retained page body only proves that
            # the bytes arrived. It does not prove a bounded text alignment can
            # attach them to this act (notably a genuine empty response has no
            # span). An omitted response can never be attached; a joined one may
            # still be explicitly unaligned.
            if not disclosed and attachment["attached"]:
                raise SchemaRefusal(
                    f"act {act_id} attachment disagrees with its page Testimonium's "
                    "unjoined-attempt record"
                )
            alignment = attachment["alignment"]
            if attachment["attached"] and attachment["attachment_basis"] != "geometric-overlap":
                raise SchemaRefusal("an attached page witness has no geometric-overlap basis")
            if (
                attachment["attached"]
                and isinstance(alignment, dict)
                and alignment.get("status") == "aligned"
            ):
                if (
                    not isinstance(alignment, dict)
                    or set(alignment)
                    != {
                        "status",
                        "anchor_basis",
                        "anchor_chair",
                        "anchor_span",
                        "witness_span",
                        "line_geometry",
                        "loss",
                        "offset_maps",
                    }
                    or alignment.get("status") != "aligned"
                    or (
                        alignment.get("anchor_basis") == "act-anchor"
                        and not isinstance(alignment.get("anchor_chair"), str)
                    )
                    or (
                        alignment.get("anchor_basis") != "act-anchor"
                        and alignment.get("anchor_chair") is not None
                    )
                    or span != alignment.get("witness_span")
                ):
                    raise SchemaRefusal("an attached page witness has no computed alignment")
                page_text = page_payload.get("payload")
                witness_span = alignment["witness_span"]
                if not isinstance(page_text, str):
                    raise SchemaRefusal("an attached page witness has no textual comparison view")
                # `witness_span` indexes the RAW page reading. It is stored that
                # way at the one storage point (`pipeline/3_attestatores/run.py`
                # clips in the normalized space the matcher measured in, then
                # translates through the alignment's own `offset_map`), so the
                # raw text is the space this slice belongs in and every consumer
                # of the field shares it. F-X3's requirement is met by
                # `act_comparison_view` stripping the SLICE: the premise
                # `dissent.is_comparable` rests on -- that `comparison_reported`
                # is a markup-stripped view and therefore safe to diff -- must
                # hold whichever space the offsets came from, and a raw slice
                # handed on unstripped would carry whatever markup it cut
                # through. Found in audit; F-X3, recomposed by R6's F-G2.
                comparison_views[chair] = act_comparison_view(page_text, witness_span)
            elif attachment["attached"] and (
                not isinstance(alignment, dict)
                or set(alignment) != {"status", "reason"}
                or alignment.get("status") != "unaligned"
                or span is not None
                or not (isinstance(alignment["reason"], str) and alignment["reason"].strip())
            ):
                raise SchemaRefusal(
                    "a geometrically attached page witness has no explicit span limit"
                )
            elif (
                not attachment["attached"]
                and isinstance(alignment, dict)
                and alignment.get("status") == "aligned"
            ):
                # Text alignment survives independently but cannot authorize a
                # geometric attachment or comparison view.
                if span is not None:
                    raise SchemaRefusal("an unattached page witness claims a comparison span")
            elif not attachment["attached"] and (
                not isinstance(alignment, dict)
                or set(alignment) != {"status", "reason"}
                or alignment.get("status") != "unaligned"
                or not (isinstance(alignment["reason"], str) and alignment["reason"].strip())
            ):
                # The producer emits exactly {status, reason}; a reason-free
                # mapping would validate while leaving the operator no
                # statement of why comparison failed.
                raise SchemaRefusal("an unattached page witness has no explicit unaligned result")
            page_witness_chairs.add(chair)
            # Geometry alone cannot satisfy the witness floor: this act must also
            # have an aligned slice of retained page text. Re-derive the boolean
            # so a resealed attachment cannot claim comparability by assertion.
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
        else:
            if attachment_page is not None:
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
                raise SchemaRefusal(
                    f"act {act_id} attachment points to another chair's Testimonium"
                )
            # Act-scoped comparability comes from the referenced Testimonium's
            # own text; structured reports stay retained but uncountable.
            if attachment["comparable"] != (
                attachment["attached"]
                and isinstance(testimonium.get("payload", {}).get("payload"), str)
            ):
                raise SchemaRefusal(
                    f"act {act_id} attachment for chair {chair!r} claims a comparability its "
                    "own retained derived testimony does not support. The witness floor could "
                    "count a structured or absent report as act text. Rebuild comparability "
                    "from the current referenced Testimonium."
                )
            edge_deltas.setdefault(chair, []).extend(
                sealed_proposal_edge_deltas(
                    testimonium["payload"],
                    [basis for basis in bases if basis["region_id"] in proposal_region_ids],
                )
            )
    return {
        "reference": context.artifact_ref(ATTESTATORES, "act-attachment", record["artifact_id"]),
        # A blinded dossier may show that page evidence exists, but not the
        # chair names embedded in its retained attachment artifact.
        "page_witness_count": len(page_witness_chairs),
        # Stated exactly, because the count above was chosen to disclose an
        # aggregate and this does not: `comparison_views` is keyed per chair,
        # relabeled through `witness_label` in `dossier.build_dossier`, and
        # present only for page witnesses. A blinded reader therefore learns
        # WHICH pseudonyms are page-scoped -- scope, never identity, but with
        # a roster of three where one chair is act-scoped that narrows a
        # witness from one-in-three to one-in-two and names the act-scoped
        # chair outright. U3 requires the act-anchored view and the view has
        # to be attributable to a label for dissent to use it, so this is the
        # cost of the instrument rather than an oversight; it is recorded here
        # so R5a/R5b, which own the dossier's reference-based act views, can
        # weigh it deliberately. R4 audit, F-X5.
        "comparison_views": comparison_views,
        "edge_deltas": ordered_edge_deltas(edge_deltas),
    }


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
    """One act's markup-stripped slice of a page reading, from a RAW span.

    Since the wave composed R4's per-act clip with R6's raw translation,
    `witness_span` indexes the RAW page-Testimonium text at the one storage
    point (`pipeline/3_attestatores/run.py`) — every consumer of the field
    shares that space. The slice is therefore taken from the raw bytes, and
    the markup stripping F-X3 requires (a comparison view safe to diff — a
    raw slice would carry whatever markup it cut through) is applied to the
    slice itself, not to the whole page before slicing.
    """
    # Both bounds, not only the end: this reads an artifact back from disk, so
    # it is the last gate before untrusted numbers become a comparison view. A
    # slice is the one place a malformed offset does not announce itself:
    # `text[-3:2]` is a perfectly good Python expression and a silently wrong
    # comparison view, which dissent would then read as departure from a
    # witness that said no such thing -- or as corroborating a blank it never
    # reported. The Recensor's own consumer of this field checks the same
    # three conditions.
    if not isinstance(witness_span, dict) or set(witness_span) != {"start", "end"}:
        raise SchemaRefusal("an attached page witness carries no two-bound comparison span")
    start, end = witness_span["start"], witness_span["end"]
    if any(not isinstance(bound, int) or isinstance(bound, bool) for bound in (start, end)):
        raise SchemaRefusal("an attached page witness claims a non-integer comparison span")
    if start < 0 or end < start or end > len(page_text):
        raise SchemaRefusal("an attached page witness claims a span past its own comparison view")
    return markup_text_view(page_text[start:end])["text"]


def dissent_testimonia(testimonia: list[dict], attachment_view: dict[str, Any]) -> list[dict]:
    """Give dissent an act-anchored page slice without changing retained testimony."""
    views = attachment_view["comparison_views"]
    result = []
    for record in testimonia:
        copied = {**record, "payload": dict(record["payload"])}
        chair = copied["payload"]["chair"]
        if copied["payload"].get("page_witness") and chair in views:
            copied["payload"]["comparison_reported"] = views[chair]
        result.append(copied)
    return result


def verify_region(context, region: dict) -> dict:
    """Prove the region handed over is the region the reference describes.

    Three checks, because each catches a different lie: the digest catches bytes
    that changed under a sealed reference, decoding catches a reference pointing at
    something that is not an image, and the dimensions catch a crop that does not
    match the transform it claims to be.

    The underlying cause is carried into the refusal text, not only onto the
    exception chain. `run_stage` prints the refusal it catches, so a cause left
    behind on `__cause__` reached nobody: every one of those distinct faults —
    a missing blob, a relabelled act, a transform outside the page — arrived at
    the operator as the same nine words, and the one thing they needed to know,
    which of them it was, had been thrown away one frame down (GOVERNANCE 2).
    The boundary's own messages name ordinals and run-tree-relative paths, never
    a submitted filename, so nothing this adds to stderr crosses the logging rule.
    """
    try:
        return verify_exemplar_crop_lineage(context.tree, context.run, region)
    except ContractError as error:
        raise SchemaRefusal(
            f"a Designator region does not trace to its Exemplar page: {error}"
        ) from error


# Moved into dossier.py so the dossier derives witness coverage from the same
# testimonia it carries; re-exported here for its callers and tests.
witnessed_region_ids = dossier_module.witnessed_region_ids


def real_ingress(context) -> bool:
    """Whether this context's run authority names the real route.

    Delegates to `common.stage.is_real_ingress`, the same reader the shared
    constructor and `expected_acts` use, so the two cannot disagree about
    which route a run is on.
    """
    return is_real_ingress(context.run)


def declared_reading_failure(context, act_key: str) -> str | None:
    """The non-completed outcome this scenario declares for an act, if any.

    The fixture is the authority on what a scenario does, exactly as it is for a
    witness failure. A reading that did not succeed still carries whatever text
    it managed, which is the shape that matters: it is what let a `truncated`
    reading be established as the one text.

    A real submission declares nothing, so this is `None` there by name rather
    than by an empty table: a real reading's non-completion is the engine's own
    stop reason, carried through `truncation.classify`, and no declaration
    stands in for it. The branch lives here so the call site does not have to
    know which route it is on.
    """
    if real_ingress(context):
        return None
    for row in context.fixture.get("reading_failure", []):
        if row["scenario"] == context.scenario and row["act_key"] == act_key:
            return row["outcome"]
    return None


def fixture_reader_for(context, chair: ChairIdentity | AbsentChair, serving_mode: str):
    """The reader a non-live pass reads through, or `None` when there is nothing to read with.

    Live mode is `None` here on both routes: the loop starts the chair on first
    use (`_live_reader`), so a resumed pass whose acts are all sealed never
    loads a model to read nothing. On the fixture route the reader exists from
    this line, exactly as before. On a real submission there is no declaration
    for it to read from: a configured chair whose sealed serving-recipe row is
    not live refuses by name, because a declared text cannot stand in for a
    reading of real ink; an absent chair reads nothing and needs no reader --
    every act publishes the same explicit `not-run` record it does on the
    fixture route -- so it gets `None` rather than a refusal about a reader it
    would never have consulted.
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

    A Perlectio is immutable, so one published over a short denominator cannot
    be corrected: restoring the missing witness changes the bytes under the same
    reading identity, and the run can no longer resume normally. Checking the
    whole requested set first is what stops a malformed act discovered late from
    leaving an unfixable reading behind it.

    The run-global page-witness declaration is checked even when every act is
    held, because those acts still publish immutable ``not-run`` Perlectiones.
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

    A record for a reading that never happened — a held act, or an absent chair —
    names what would have read and stops there. Manufacturing a receipt for it
    would be a serving moment nobody observed.

    A reading that *did* happen re-verifies the configured snapshot first, at the
    moment it is produced rather than once at run creation: GOVERNANCE 6 is about
    identity when the reading was made, and a receipt captured at serve time and
    copied forward is the weaker claim spec 02 names and refuses.

    `receipt_ref` is the live chair's own receipt, published by the serving
    manager when the service actually started and read back by
    `ChairClient.__enter__` before any reading was taken. It is passed only in
    live mode, and it is not an optimisation: `fixture_serving_details` declares
    `fixture://` for an endpoint and `fixture` for a dtype, so minting one
    beside a reading a real engine produced would put a declared fixture value
    where a measurement belongs (GOVERNANCE 10). Fixture mode passes nothing and
    writes the declared receipt exactly as before, which is what leaves its bytes
    where they were.
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
        # Tyrel's 2026-07-30 ruling: witness identity travels under a run-level
        # toggle, and every Perlectio records the regime it ran under, because a
        # reading's provenance includes what its reader was shown.
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

    Only the recipe catalogue is read, so only its digest is compared.
    `operations.serving.assembly._load_bound_configuration` additionally re-reads
    and re-digests `config/pod_placement.toml` because pod preflight parses that
    table to *choose* a tier; this stage never does. The tier arrives already
    measured on `--placement-tier`, and the manager still carries the run-sealed
    pair into every launch audit, where `StageContext.write_serving_launch_audit`
    refuses one that differs. Digesting a file nothing here reads would be a
    check on bytes this stage cannot act on.
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

    No new configuration key decides this and none is added: the catalogue
    `--serving-recipes-config` names is already sealed into `config_digest`
    through `serving_config_inputs`, so the fact is one the run authority
    already states (spec 08 §5). `--placement-tier` is the one thing that must
    be supplied beside it, and it is deliberately unsealed — a measured runtime
    fact of the card, not run configuration.

    Resolved once, before the run partition is published and before any chair is
    started, so a live catalogue named without a tier refuses while the tree is
    still exactly as this invocation found it.

    An absent chair resolves to fixture without consulting the catalogue: it
    reads nothing at all — every act publishes an explicit not-run record naming
    the absence — so there is no resolved identity to look a row up by, and
    inventing one to ask about would be asking which engine an absence would
    have used.
    """
    if isinstance(chair, AbsentChair):
        return "fixture"
    return serving_mode_for(
        bound_serving_recipes(context, args.serving_recipes_config), chair, args.placement_tier
    )


class ResidentChair:
    """The one live chair a Perlector pass holds, and the promise it is stopped.

    A holder rather than a `with` block around the pass, because the pass is six
    hundred lines that have nothing to do with serving and every line of it would
    have moved to gain the guarantee. `main` closes this in a `finally`; the pass
    also closes it explicitly before it seals, so on the ordinary path the
    service is verified down *before* the completion boundary is written and a
    failed shutdown cannot be reported over a sealed stage. `close` is idempotent
    for exactly that reason.

    A `ServiceStopError` propagates. An unverified shutdown of a child this
    process started is the local form of GOVERNANCE 8's rule that shutdown is
    verified rather than inferred, and swallowing it in cleanup is how a run
    reports success over a service nobody proved was gone.
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

    Returned as a closure rather than exposed as a bare function so the one
    parameter list `main` injects against stays `(context, chair, tier)`: a test
    passes `operations.serving.fakes.fake_serving_factory`, and production passes
    nothing and gets this. The seam is a dependency injection point, never a
    choice among engines — which engine answers is the sealed row's business
    (`perlector_serving_mode`), and this factory is only reached once that row
    has already said `live`.
    """

    def factory(context, chair: ChairIdentity, tier: str) -> ChairClient:
        manager = ServingManager(
            registry=context.registry,
            recipes=recipes,
            config_inputs=ServingConfigInputs.from_record(context.serving_config_inputs),
            launcher=SubprocessLauncher(),
            http=UrllibHttpTransport(),
            receipt_publisher=StageContextReceiptPublisher(context),
            log_root=context.tree.resolve(
                f"{writing_directory(context.stage)}/{SERVING_LOG_DIRECTORY}"
            ),
            # One card, one resident chair, one lease file for the whole run
            # tree: the Attestatores' witness chairs and this reader contend for
            # the same GPU, and the lease is what makes a second start refuse
            # instead of co-residing.
            residency_lease=FileResidencyLease(context.tree.resolve(RESIDENCY_LOCK_FILE)),
            producer="pipeline/4_perlector/run.py",
        )
        return ChairClient(
            manager=manager,
            identity=chair,
            tier=tier,
            retain=partial(retain_chair_bytes, context),
            decoding_config_sha256=decoding_config_sha256,
            record_temperature=record_temperature,
            # Wired bare. `ChairClient.__enter__` copies
            # `ServiceHandle.receipt_reference` into a plain `dict` before it
            # calls this, which is exactly the type `RunTree.read_run_receipt`
            # requires, so no stage-side conversion is left to do.
            read_receipt=context.tree.read_run_receipt,
        )

    return factory


def retain_chair_bytes(context, data: bytes) -> dict[str, str]:
    """Store one chair response or call record under its own digest.

    The client retains before it parses, so this runs before anything has looked
    at the body: it is the durability half of response-as-arrival (GOVERNANCE 2),
    and it is a property of the client rather than of this stage's publication
    order. Guarded after the seal for the reason `StageContext._write_serving_blob`
    is — this writes into the stage's own blob directory, whose inventory digest
    the completion seal witnessed, and a write afterwards makes that inventory
    false while the symptom lands on the next stage.
    """
    if context.sealed:
        raise SchemaRefusal(
            "the Perlector has sealed its completion boundary; retaining a chair response "
            "afterwards would make its witnessed blob inventory false"
        )
    digest, result = context.tree.put_blob(context.stage, data)
    return {"relative_path": result.relative_path, "sha256": digest}


def engine_call_inputs(context, engine_call: dict[str, Any] | None) -> list[dict[str, str]]:
    """Bind the two blobs a live reading's own record names as direct inputs.

    A fixture reading has no engine behind it, sets no `engine_call`, and adds
    nothing here — which is what keeps its envelope, and the acceptance pin over
    it, exactly where they were.

    Each reference is re-derived from the bytes on disk and compared to what the
    reader claimed, rather than copied: a record naming a response that is not
    there, or whose digest has moved, would otherwise publish an input list that
    reads as evidence and resolves to nothing.
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
    """Start this run's one chair and return the reader that speaks to it.

    The receipt reference comes back beside the reader because every record this
    pass publishes must name the receipt of the service that actually answered —
    `ChairClient.__enter__` has already read it back through the tree and refused
    a receipt that no longer names this chair and revision (GOVERNANCE 6).
    """
    factory = serving_factory or default_serving_factory(
        bound_serving_recipes(context, args.serving_recipes_config),
        decoding_config_sha256=decoding_sha256,
        # The sealed reading-of-record posture, taken from the bytes this run
        # bound and rechecked. `ChairClient` refuses anything but 0 rather than
        # coercing one, so a policy that said otherwise is a named refusal here
        # instead of a silent zero on every call record.
        record_temperature=decoding_policy["reading_of_record"]["temperature"],
    )
    # Assigned before it is entered: a `ChairClient` that fails partway through
    # `__enter__` has already stopped whatever it started, and `close` on one
    # that never started is a no-op — but the assignment is what makes that
    # true of every future failure between these two lines as well.
    service.client = factory(context, chair, args.placement_tier)
    service.client.__enter__()
    reader = VLLMReader(
        client=service.client,
        chair=chair,
        protocol_config=protocol_config,
        # No sealed output bound exists and this section does not invent one:
        # vLLM bounds generation by `max_model_len`, so an engine `"length"`
        # then honestly means the context itself was exhausted rather than that
        # the harness cut the reading short. A sealed bound belongs with the
        # variance-experiment section, which will need one too.
        max_tokens=None,
    )
    return reader, dict(service.client.handle.receipt_reference)


def _distinct_inputs(references: list[dict[str, str]]) -> list[dict[str, str]]:
    """One entry per path, in first-named order, refusing two digests for one path.

    Needed only where a live re-proof joins the establishing call: both are
    content-addressed, so an engine that answered the same bytes twice names one
    blob twice and the envelope would carry a duplicate input. Two *different*
    digests under one path cannot happen in a content-addressed store, so if it
    ever does, something has rewritten a blob and this refuses rather than
    picking one.
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


def _reading_already_sealed(context, act_id: str, ordinal: int) -> bool:
    """Whether this run tree already holds this act's Perlectio at this ordinal.

    Existence, not content: the question is whether asking a chair again would
    write over an immutable record, and any Perlectio at this identity — a
    completed reading or a not-run acknowledgement — already answers for it.
    """
    attempt = perlector_attempt_id(act_id, "perlegere", ordinal)
    identifier = artifact_id(PERLECTOR, "perlectio", act_id, attempt)
    return context.tree.resolve(
        context.tree.artifact_path(PERLECTOR, "perlectio", identifier)
    ).exists()


def with_engine_call(payload: dict, result: dict, fields: frozenset) -> frozenset:
    """Carry a live reading's engine call on its record, and a fixture one's nothing.

    The `_NOT_RUN_CAPACITY_FIELDS` precedent: a field that exists only on one
    shape of record widens the closed set for that shape rather than becoming
    optional inside one set, so every record is still validated against a schema
    that names exactly what it carries.
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
    """The one gap an unreadable act carries: zero-width, evidence attached,
    never a character inside `text` (the establishment firewall,
    `annotations.py`).

    Each variant travels with the digest-checked reference to the Testimonium
    that reported it, so a displayed "⟨illegible — witnesses agree: …⟩" leads
    back to the sealed record rather than to a chair name somebody would then
    have to go looking for.
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
    return sum(
        basis["transform"]["bounds"]["w"] * basis["transform"]["bounds"]["h"] for basis in bases
    )


def _reading_image_inputs(
    context,
    bases: list[dict],
    page_renders: list[dict],
    *,
    autopsia: dict[str, Any],
) -> list[dict]:
    """Every image blob the reading saw and its partition authority.

    The dossier records these references as payload facts; the envelope input
    list independently binds them as direct evidence.  Omitting page context
    there would let a Perlectio claim it saw a render that its own provenance
    never retained as an input. The cross-capture partition is equally direct:
    it is the authority for which capture set had to be presented, so a reading
    whose dossier cites it must bind the same immutable bytes as an input.
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
    return sorted(inputs.values(), key=lambda item: (item["relative_path"], item["sha256"]))


# Every field an established-reading Perlectio payload carries. Closed, and
# checked before publication rather than described in the handoff and hoped
# for: spec 08's schema test asks that a Perlectio "missing identity, missing
# dissent, missing regime record, or with annotation spans outside text bounds"
# be refused, and three of those four are fields that would simply be absent
# rather than wrong. An absent field is the failure mode a per-field type check
# never sees.
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
        "gaps",
        "provenance",
        "lectio_kind",
        "self_revision",
        "protocol",
        "audit",
    }
)

# The same, for the instrument record. It carries no `basis` -- a nuda reading
# has no witness basis to record, which is the whole point of it -- and it does
# carry the sampling design it was drawn under.
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

    Deliberately the common closed field-set check: a not-run payload has no
    completed reading to validate. Capacity holds additionally validate their
    retained autopsia and direct partition input at their production branch.
    """
    missing = sorted(fields - set(payload))
    unexpected = sorted(set(payload) - fields)
    if missing or unexpected:
        raise SchemaRefusal(
            f"a Perlector not-run payload is not its closed schema: missing {missing}, "
            f"unexpected {unexpected}"
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
    protocol_config: dict[str, str | int] | None = None,
    protocol_sha256: str | None = None,
    inputs: list[dict[str, str]] | None = None,
) -> None:
    """Refuse a reading payload that is missing part of the record it claims.

    Producer-local and deliberately so: `validate_serving_provenance` already
    refuses a wrong-schema provenance wherever a Perlectio is *consumed*, and
    this is the matching check at the moment one is written, so a defect
    surfaces where it was introduced rather than one stage later.
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
    # The caller's field set decides which record shape this is: a Perlectio
    # payload smuggling `basis: None` must refuse as a missing witness basis,
    # never slip down the unprimed branch with every witness gone from the
    # record. Two record kinds are unprimed since R5a -- Lectio nuda and the
    # universal Pass-A `lectio-prior` -- so the branch is named for the
    # condition rather than for one of its two occupants, and so are its
    # refusals: a refusal that says "Lectio nuda" over a lectio-prior record
    # sends the next reader to the wrong artifact.
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
        # Key presence, not value: the dossier field-set check above admits the
        # {prior_draft, prior_draft_view} key combination, so a None prior_draft
        # beside a view key would slip a value-only test.
        if "prior_draft" in reading_dossier or "prior_draft_view" in reading_dossier:
            raise SchemaRefusal(
                "a Perlectio claims primed-without-prior but carries prior-draft data"
            )
    elif lectio_kind is not None:
        # `None` is the two kinds whose field sets exclude the key entirely
        # (lectio-nuda and lectio-prior). Any other value matched neither
        # branch above, so its prior-draft evidence would publish uninspected
        # and the defect would surface one stage later at the Archetypus —
        # the opposite of what this validator promises.
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
        # Label-for-label, not merely count-for-count: a sealed reading must not
        # show one witness set in the prompt while its basis, dissent and export
        # record another. Named labels are the chairs themselves; blinded labels
        # are re-derived from the run's own identity when the caller supplies it
        # (the production publish path always does — the bare form exists for
        # schema tests that assert other refusals).
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
    # Two statements of one fact: the run-level `draft_fed` the record declares
    # and the per-act view its dossier names. Both derive from the same flag in
    # `main()`, so this cannot fire on the production path -- and that is the
    # point. `self_revision` is only interpretable against a known feeding
    # state, so a record that claimed `draft_fed` true while its dossier
    # withheld the draft would make the Pass-A->B change rate (a standing metric
    # under design v2.1) mean nothing, with nothing in the record saying so.
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
    # The record's own two policy names, bound to the sealed bytes rather than
    # merely present. `prompt.page_shared_prefix_policy` is already reproduced
    # from `protocol_config` by the prompt check below, so a payload could
    # declare one rule in its `protocol` block while its prompt was built under
    # another, and nothing said so. Unfireable on the production path for the
    # same reason the draft_fed cross-check above is, and recorded here for the
    # same reason: what these two names mean is what the run sealed.
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
    if prompt_record != prompts.prompt_evidence(
        identity, reading_dossier, protocol_config, protocol_sha256
    ):
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
    if "audit" not in fields:
        annotations.validate_annotations(payload, outcome=outcome)
        return
    # The re-proof locations are offsets in the frozen semi-final, which may
    # be longer than the corrected final after a deletion. The full shared
    # chain check binds them to the audit draft before publication; this
    # payload-only shape check therefore does not guess a bound from final text.
    audit.validate_perlectio_audit(payload.get("audit"), text_length=None)
    annotations.validate_annotations(payload, outcome=outcome)


def _resolve_outcome(*, declared_failure: str | None, truncation_record: dict, text: str) -> str:
    """One place the outcome is decided, so the precedence is stated once:
    a scenario's declared engine behaviour outranks the computed detector
    (it stands in for a real engine's own report), the detector outranks a
    default `read`, and an empty reading is never silently `read`."""
    if declared_failure is not None:
        return declared_failure
    if truncation.holds_as_failure(truncation_record["classification"]):
        return "truncated"
    if not text.strip():
        # The same emptiness rubric the publish-time schema uses: a reader
        # returning "\n" for one act is an unreadable act, not a reason to
        # abort the stage and lose the parish's other readings.
        return "no-readable-text"
    return "read"


def _reconciled_truncation(*, declared_failure: str | None, truncation_record: dict) -> dict:
    """Keep the published truncation record from contradicting a declared failure.

    A declared failure stands in for a real engine's own report that a reading
    did not complete, and nothing about *why* need show in the text's shape --
    so the three computed signals can land on `complete` under an outcome that
    means "not established complete" (HANDOFF.md, verbatim). The signals stay
    exactly as measured; only the classification is raised to `unknown`, because
    the instrument did not itself confirm a cutoff. Something outside it did.
    """
    if (
        declared_failure == "truncated"
        and truncation_record["classification"] == truncation.COMPLETE
    ):
        return {**truncation_record, "classification": truncation.UNKNOWN}
    return truncation_record


def _audited_truncation(
    *,
    pass_b: dict,
    declared_failure: str | None,
    text: str,
    region_pixels: int,
    stop_reason: str | None,
) -> dict:
    """The truncation instrument, re-measured over an audit-changed reading.

    Three of the four declared signals are computed over the reading text and
    the fourth is the engine's own word on why it stopped, so a Pass-C re-proof
    that changes the text invalidates the whole record: the published Perlectio
    would otherwise state what the *pre-audit* text looked like, and `outcome`
    is derived from that record. The re-proof's own stop reason was dropped
    entirely, so a re-proof generation cut off mid-emission could replace
    established text while the record still read `complete` — the reading
    delivered as an output would be the truncated one (ARCHITECTURE: "it reads
    through to the end; truncation is a failure, not an output").

    **Pass C may only ever make this worse.** The re-proof is span-scoped and
    H8-bounded to the flagged location, so it cannot restore ink a cut-off Pass
    B never read. A clean re-proof over a truncated semi-final therefore keeps
    the earlier classification: the recomputed signals describe the published
    text, but the verdict never improves.
    """
    audited = _reconciled_truncation(
        declared_failure=declared_failure,
        truncation_record=truncation.classify(
            text, region_pixels=region_pixels, stop_reason=stop_reason
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
    # Proved before it becomes a sort key: `bounds.get` handed a crop record
    # that lost either number a (None, None) geometry_order, and comparing
    # None with an integer ends the whole page's flag pass in an unnamed
    # TypeError -- the same failure the `_region_ordinal` refusal exists to
    # prevent.
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
        # The act's own crop position, independent of the sequence it was
        # declared and processed in -- reusing `order` here would make declared
        # and geometric identical by construction, so the "order" flag class
        # could never fire (audit finding H1).
        "geometry_order": (bounds.get("y"), bounds.get("x")),
        "text": text,
        "testimonia": reports,
        # Pass C accounts only within the delivered crop. Page partition and
        # residual-ink predicates belong to the Recensor.
        "within_crop": True,
    }


def audit_page_ids(bases: list[dict[str, Any]]) -> list[str]:
    """The complete canonical page set for one act's page audit.

    Page sequence remains on each immutable region basis. This field is only a
    denominator, so retaining ordinal traversal here would give its list order
    an accidental representative meaning after the singular ``page_id`` was
    removed.
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

    `RunTree.build_manifest` and `read_artifact` both validate every envelope's
    self-hash, derived path, run/config binding, and input bytes. The sibling
    text below therefore comes from the sealed Perlectio in the tree, never a
    reconstruction from fixture or source text. Only rows are returned; the
    publication loop remains over `pending`, so a sibling cannot be republished.
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
            # Never a skip: by the time a recovery pass runs, every expected
            # act carries a Perlectio -- a held one carries `not-run`, handled
            # below. Zero artifacts means a reading that existed is no longer
            # here, and a page flag pass computed over a short row set would
            # seal a different flag set than the page's evidence supports. A
            # missing middle row can remove its own comparison or create a new
            # adjacency between its former neighbours.
            raise FatalAccounting(
                f"act {act_id} has no Perlectio, so recovery cannot determine whether it "
                "shares a contributing page; the page denominator is unknown; restore its "
                "retained Perlectio before recomputing the audit"
            )
        reading = latest_attempt(
            records, f"sealed sibling Perlectio for {act_id}", operation="perlegere"
        )
        payload = reading.get("payload")
        # A held/not-run sibling was absent from the original frozen Pass-B
        # collection too, so it contributes no row to a later recovery audit.
        # ONLY that outcome skips: any other outcome whose payload lost its
        # text is malformed evidence, and dropping it would quietly shrink the
        # page's cross-act flag comparisons exactly like the zero-record case
        # above.
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
        chain = audit.validate_chain(context.tree, reading, act_id)
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

    Only a report whose exact comparison with the semi-final produced one of
    the frozen testimony-diff locations is named.  An agreeing witness is
    evidence in the dossier, but it did not locate that flag and must not be
    attributed as though it did.  This records a location basis only; it does
    not promote testimony into a reading or make boundary geometry a text flag.

    Each row names the location it accounts for.  Without it the record could
    not say *which* flag a chair located: two rows beside two flags proved only
    that the lists were the same length, and a basis row could be read against
    the wrong flag with nothing able to detect it.  The span is already computed
    here to decide membership, so carrying it costs nothing and makes the
    binding checkable where the record is validated.
    """
    # `audit.WITNESS_DERIVED_LOCATION_CLASSES`, not the literal it holds today.
    # The validator expects a basis row for every flag of a witness-derived
    # class; a producer filtering on a hardcoded name would emit none for a
    # class added to that constant, and every draft carrying the new class
    # would be refused with no Perlectio published for those acts.
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


def _reseal_dossier(dossier: dict[str, Any]) -> dict[str, Any]:
    """Sweep and seal the final fields retained for publication."""
    body = {key: value for key, value in dossier.items() if key != "dossier_digest"}
    # Combined transport fields are added after dossier construction, so its
    # original preference sweep and digest do not cover the published object.
    dossier_module.assert_no_order_bearing_field(body)
    return {**body, "dossier_digest": digest_of(body)}


def _publication_pass_data(
    chair: ChairIdentity,
    dossier: dict[str, Any],
    result: dict[str, Any],
    *,
    region_pixels: int,
    protocol_config: dict[str, str | int],
    protocol_sha256: str,
) -> tuple[dict[str, Any], dict[str, Any], str, dict[str, Any], str]:
    sealed_dossier = _reseal_dossier(dossier)
    prompt = prompts.prompt_evidence(chair, sealed_dossier, protocol_config, protocol_sha256)
    truncation_record = truncation.classify(
        result["text"], region_pixels=region_pixels, stop_reason=result["stop_reason"]
    )
    outcome = _resolve_outcome(
        declared_failure=None, truncation_record=truncation_record, text=result["text"]
    )
    text = "" if outcome == "no-readable-text" else result["text"]
    return sealed_dossier, prompt, outcome, truncation_record, text


def _publish_lectio_nuda(
    context,
    *,
    act_key: str,
    act_id: str,
    ordinal: int,
    chair: ChairIdentity,
    dossier: dict[str, Any],
    result: dict[str, Any],
    bases: list[dict],
    page_renders: list[dict],
    region_pixels: int,
    protocol_config: dict[str, str | int],
    protocol_sha256: str,
    approval_ref: ApprovalRecordBinding,
    receipt_ref: dict[str, str] | None = None,
) -> None:
    """Publish outside Perlectio kind and attempt identity with no witness facts."""
    nuda_dossier, prompt, outcome, truncation_record, nuda_text = _publication_pass_data(
        chair,
        dossier,
        result,
        region_pixels=region_pixels,
        protocol_config=protocol_config,
        protocol_sha256=protocol_sha256,
    )
    payload = {
        "act_key": act_key,
        "attempt_ordinal": ordinal,
        "text": nuda_text,
        "dossier": nuda_dossier,
        "prompt": prompt,
        "sampling": nuda.sampling_design(
            nuda_per_mille=context.nuda_per_mille,
            approval_ref=approval_ref,
        ),
        "dissent": [],
        "truncation": truncation_record,
        "uncertain_spans": [],
        "gaps": _whole_act_gap([], {}) if outcome == "no-readable-text" else [],
        "provenance": provenance_for(context, chair, attempted=True, receipt_ref=receipt_ref),
    }
    fields = with_engine_call(payload, result, _LECTIO_NUDA_FIELDS)
    reading_inputs = _reading_image_inputs(
        context,
        bases,
        page_renders,
        autopsia=nuda_dossier["cross_capture_autopsia"],
    ) + engine_call_inputs(context, result.get("engine_call"))
    validate_reading_payload(
        payload,
        outcome=outcome,
        fields=fields,
        protocol_config=protocol_config,
        protocol_sha256=protocol_sha256,
        inputs=reading_inputs,
    )
    context.publish(
        kind=nuda.LECTIO_NUDA_KIND,
        subject_id=act_id,
        outcome=outcome,
        attempt=perlector_attempt_id(act_id, "lectio-nuda", ordinal),
        inputs=reading_inputs + [approval_ref.reference.to_record()],
        payload=payload,
    )


def _publish_lectio_prior(
    context,
    dossier: dict[str, Any],
    result: dict[str, Any],
    *,
    act_key,
    act_id,
    ordinal,
    chair,
    bases,
    page_renders,
    region_pixels,
    protocol_config,
    protocol_sha256,
    receipt_ref: dict[str, str] | None = None,
) -> dict:
    """Publish Pass A as a retained draft, never as a Perlectio."""
    prior_dossier, prompt, outcome, truncation_record, text = _publication_pass_data(
        chair,
        dossier,
        result,
        region_pixels=region_pixels,
        protocol_config=protocol_config,
        protocol_sha256=protocol_sha256,
    )
    payload = {
        "act_key": act_key,
        "attempt_ordinal": ordinal,
        "text": text,
        "dossier": prior_dossier,
        "prompt": prompt,
        "dissent": [],
        "truncation": truncation_record,
        "uncertain_spans": [],
        "gaps": _whole_act_gap([], {}) if outcome == "no-readable-text" else [],
        "provenance": provenance_for(context, chair, attempted=True, receipt_ref=receipt_ref),
        "protocol": {
            "selection_rule": protocol_config["selection_rule"],
            "page_shared_prefix_policy": protocol_config["page_shared_prefix_policy"],
            "draft_fed": context.draft_fed,
        },
    }
    fields = with_engine_call(payload, result, _LECTIO_PRIOR_FIELDS)
    reading_inputs = _reading_image_inputs(
        context,
        bases,
        page_renders,
        autopsia=prior_dossier["cross_capture_autopsia"],
    ) + engine_call_inputs(context, result.get("engine_call"))
    validate_reading_payload(
        payload,
        outcome=outcome,
        fields=fields,
        protocol_config=protocol_config,
        protocol_sha256=protocol_sha256,
        inputs=reading_inputs,
    )
    context.publish(
        kind="lectio-prior",
        subject_id=act_id,
        outcome=outcome,
        attempt=perlector_attempt_id(act_id, "lectio-prior", ordinal),
        inputs=reading_inputs,
        payload=payload,
    )
    prior_artifact_id = artifact_id(
        PERLECTOR, "lectio-prior", act_id, perlector_attempt_id(act_id, "lectio-prior", ordinal)
    )
    return {
        "reference": context.artifact_ref(PERLECTOR, "lectio-prior", prior_artifact_id),
        "text": text,
    }


def _publish_primed_without_prior(
    context,
    *,
    act_key,
    act_id,
    ordinal,
    chair,
    dossier: dict[str, Any],
    result: dict[str, Any],
    bases,
    page_renders,
    region_pixels,
    testimonia,
    attachment_view,
    protocol_config,
    protocol_sha256,
    approval_ref: ApprovalRecordBinding,
    receipt_ref: dict[str, str] | None = None,
) -> None:
    """The sampled control sees witnesses but never the Pass-A draft."""
    control_dossier, prompt, outcome, truncation_record, text = _publication_pass_data(
        chair,
        dossier,
        result,
        region_pixels=region_pixels,
        protocol_config=protocol_config,
        protocol_sha256=protocol_sha256,
    )
    testimonium_references = {
        record["artifact_id"]: context.artifact_ref(
            ATTESTATORES, "testimonium", record["artifact_id"]
        )
        for record in testimonia
    }
    # `context.run` is the run authority `open_context` already read and
    # verified from disk, and nothing writes `run.json` after the Door creates
    # it. Re-reading it here would re-verify the same bytes once per sampled
    # act, and once more per act at the sampling decision below.
    membership = context.run["corpus_frame_membership"]
    payload = {
        "act_key": act_key,
        "attempt_ordinal": ordinal,
        "text": text,
        "basis": {
            "regions": bases,
            "testimonia": [
                {
                    "chair": record["payload"]["chair"],
                    "artifact_id": record["artifact_id"],
                    "outcome": record["outcome"],
                    "reference": testimonium_references[record["artifact_id"]],
                }
                for record in testimonia
            ],
        },
        "dossier": control_dossier,
        "prompt": prompt,
        "sampling": protocol.control_sampling_design(
            per_mille=context.perlector_instrument_per_mille,
            selection_rule=protocol_config["selection_rule"],
            approval_ref=approval_ref,
        ),
        # The digest draw above is keyed by the logical act. Record that same
        # subject here; a local capture ID would make a clustered control's
        # retained membership impossible to reproduce from its own facts.
        "membership": {
            **membership,
            "act_id": control_dossier["logical_act_id"],
            "protocol_sha256": protocol_sha256,
        },
        "dissent": dissent_against(text, dissent_testimonia(testimonia, attachment_view)),
        "truncation": truncation_record,
        "uncertain_spans": [],
        "gaps": _whole_act_gap(testimonia, testimonium_references)
        if outcome == "no-readable-text"
        else [],
        "provenance": provenance_for(context, chair, attempted=True, receipt_ref=receipt_ref),
        "lectio_kind": "primed-without-prior",
        "protocol": {
            "selection_rule": protocol_config["selection_rule"],
            "page_shared_prefix_policy": protocol_config["page_shared_prefix_policy"],
            "draft_fed": context.draft_fed,
        },
    }
    fields = with_engine_call(payload, result, _PRIMED_WITHOUT_PRIOR_FIELDS)
    reading_inputs = (
        _reading_image_inputs(
            context,
            bases,
            page_renders,
            autopsia=control_dossier["cross_capture_autopsia"],
        )
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
        protocol_config=protocol_config,
        protocol_sha256=protocol_sha256,
        inputs=reading_inputs,
    )
    context.publish(
        kind="primed-without-prior",
        subject_id=act_id,
        outcome=outcome,
        attempt=perlector_attempt_id(act_id, "primed-without-prior", ordinal),
        inputs=reading_inputs + [approval_ref.reference.to_record()],
        payload=payload,
    )


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

    Both parameters are dependency seams, not runtime choices. `registry_factory`
    supplies the chair implementation; `serving_factory` supplies the client a
    live chair is read through, and is reached only when the sealed
    serving-recipe row for the resolved Perlector chair already says the chair is
    live (`perlector_serving_mode`). Passing one does not make a run live and
    omitting one does not make a run fixture: no argument to this function
    decides which engine answers.

    The `finally` is the guarantee, not the ordinary path — `_read_the_acts`
    stops the chair itself before it seals, so a failed shutdown is never
    reported over a sealed stage. This one catches the exceptional path, where
    the pass raised before it reached its own shutdown.
    """
    service = ResidentChair()
    try:
        return _read_the_acts(registry_factory, serving_factory, service)
    finally:
        service.close()


def _read_the_acts(registry_factory, serving_factory, service: ResidentChair) -> int:
    """One Perlector pass: every requested act read once and published once."""
    args = stage_parser(__doc__.splitlines()[0]).parse_args()
    # Either ingress route, decided from one read of the run authority; the
    # real route carries the registry and sealed digests the lines below need.
    context = open_stage_context(args, PERLECTOR, registry_factory=registry_factory)
    decoding_policy, decoding_sha256 = load_decoding_policy(args.decoding_config)
    context.require_sealed_config("decoding", decoding_sha256)
    # Resolved here, before the run partition is published and long before a
    # chair is started: the sealed catalogue and `--placement-tier` are all this
    # answer needs, and a live catalogue named without a tier must refuse while
    # the tree is still exactly as this invocation found it.
    chair = perlector_chair(context)
    serving_mode = perlector_serving_mode(context, args, chair)
    # Resolved here too, before the run partition is published and before any
    # chair is started: a real submission with a non-live row refuses here, by
    # name, while the tree is still exactly as this invocation found it.
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

    # A recovery re-reads only the acts that were recovered. Re-reading the rest
    # would add an attempt nobody requested to an act nothing happened to, and an
    # attempt tally that counts work no stage asked for stops meaning anything.
    expected = expected_acts(context)
    declared_order = {act["act_id"]: order for order, act in enumerate(expected)}
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
    # Walked once for the whole run: the routing denominator is every sealed
    # proposal, and `reported_unrouted` keeps one observation's finding from being
    # restated by every act that reaches the same page Testimonium.
    all_proposal_regions = sealed_proposal_regions(context)
    reported_unrouted: set[tuple[str, int]] = set()

    # A live chair is started on first use, not here. `reader`, resolved above
    # before the run partition was published, already exists; in live mode the
    # loop below starts a chair the first time an act actually clears capacity
    # and needs a reading, so a resumed pass whose acts are all already sealed
    # -- or all over capacity -- never loads a 27B model onto a card that bills
    # by the hour to read nothing. `service` owns the shutdown from the moment
    # the client exists.
    receipt_ref: dict[str, str] | None = None

    for act in wanted:
        act_id = act["act_id"]
        if act["outcome"] == "held":
            # A held act's proposal is incomplete — its page or its continuation
            # never sealed. Reading whatever regions exist would produce a
            # reading of part of an act, and it reads through to the end:
            # truncation is a failure, not an output. The act is acknowledged
            # with an explicit unresolved outcome rather than skipped, because
            # a unit this stage never mentions is invariant #10's imbalance.
            payload = {
                "act_key": act["act_key"],
                "attempt_ordinal": 1,
                "reason": (
                    "the Designator held this act; an incomplete proposal is "
                    "not read, because a reading of part of an act would be a "
                    "truncation delivered as an output"
                ),
                "provenance": provenance_for(context, chair, attempted=False),
            }
            validate_not_run_payload(payload, fields=_NOT_RUN_HELD_FIELDS)
            context.publish(
                kind="perlectio",
                subject_id=act_id,
                outcome="not-run",
                attempt=perlector_attempt_id(act_id, "perlegere", 1),
                payload=payload,
            )
            acknowledged += 1
            continue

        # Read once, before the ordinal is derived from it: the same region set
        # answers both which attempt this is and which crops are read, and
        # `_next_attempt` refuses an unplaceable origin here rather than leaving
        # the next stage to discover it over an immutable Perlectio. Every act
        # reaching this line already had its regions walked and validated by
        # `preflight_testimonia_denominator`, absent chair or not.
        regions, proposal_regions = act_regions(context, act_id)
        ordinal = _next_attempt(context, act_id, regions)
        if isinstance(chair, AbsentChair):
            # No chair to read with. Every act still gets an explicit record
            # naming the absence: a stage that simply produced nothing would
            # leave the Recensor to infer a gap it cannot see.
            payload = {
                "act_key": act["act_key"],
                "attempt_ordinal": ordinal,
                "reason": f"the Perlector chair is explicitly absent: {chair.reason}",
                "basis": {"regions": [], "testimonia": []},
                "dissent": [],
                "provenance": provenance_for(context, chair, attempted=False),
            }
            validate_not_run_payload(payload, fields=_NOT_RUN_ABSENT_FIELDS)
            context.publish(
                kind="perlectio",
                subject_id=act_id,
                outcome="not-run",
                attempt=perlector_attempt_id(act_id, "perlegere", ordinal),
                payload=payload,
            )
            acknowledged += 1
            continue

        if serving_mode == "live" and _reading_already_sealed(context, act_id, ordinal):
            # **A live act sealed at this ordinal is never asked again.** Fixture
            # readers are deterministic, so a resumed fixture pass recomputes the
            # same ordinal, republishes the same bytes and the store reuses them
            # (`_next_attempt`'s own docstring). A live chair cannot promise that:
            # the second reading's bytes differ, the store refuses the collision
            # (`IncompatibleReuse`), and the run ends loudly one act into a
            # resume. The act is already recorded, so the honest resume is to
            # leave it recorded and read the rest — GOVERNANCE 4, evidence is
            # never overwritten. Counted apart from `read`, because this
            # invocation did not read it and a tally that said otherwise would be
            # this pass measuring work it did not do.
            resumed += 1
            continue

        # The scenario's declared engine behaviour stands in for a real
        # engine's own report and, when present, decides `reading` and
        # `outcome` together: a declared `no-readable-text` means nothing was
        # read, not that the fixture's normal act text happens to still apply.
        # That stand-in is only honest when there is no real report to stand
        # in for. Checked here, before any chair is started or arm published:
        # a live pass answering a declared act is a misconfiguration knowable
        # from the fixture and the act key alone, and this stage must refuse
        # while the tree is still exactly as this invocation found it --
        # GOVERNANCE 10 names exactly this override.
        declared_failure = declared_reading_failure(context, act["act_key"])
        if serving_mode == "live" and declared_failure is not None:
            raise ContractError(
                f"the fixture declares reading outcome {declared_failure!r} for "
                f"act {act['act_key']!r} while a live chair is answering; a "
                "declared stand-in cannot override an engine that reported"
            )

        # Every region of the act is verified and read, including a continuation
        # on the next page: an act that ran over the page break and was read only
        # up to the fold would be truncated, which is a failure and not an output.
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
            # Named on stderr before this stage seals, never silently normalized
            # into the closest act. The Recensor independently re-derives the
            # same finding from this observed geometry and the sealed proposal
            # denominator, then may route it through bounded coverage recovery.
            # It does not trust the page record's optional retained snapshot.
            print(f"non-fatal finding: {finding}", file=sys.stderr)

        # Which regions any witness actually saw. Ink uncovered by a recovery
        # recrop was never shown to a witness, and saying so is the difference
        # between a gap in the record and a gap nobody can see. It changes nothing
        # about the reading — the Perlector reads the ink either way.
        witnessed = witnessed_region_ids(testimonia, bases)
        for basis in bases:
            basis["witness_covered"] = basis["region_id"] in witnessed

        region_pixels = _region_pixels(bases)
        page_renders = _page_renders_for(context, bases)

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
            capacity_inputs = _reading_image_inputs(context, bases, page_renders, autopsia=autopsia)
            payload = {
                "act_key": act["act_key"],
                "attempt_ordinal": ordinal,
                "reason": capacity_finding,
                "basis": {"regions": [], "testimonia": []},
                "dissent": [],
                "provenance": provenance_for(context, chair, attempted=False),
                "logical_act_id": logical_act_id,
                "cross_capture_autopsia": autopsia,
            }
            validate_not_run_payload(payload, fields=_NOT_RUN_CAPACITY_FIELDS)
            context.publish(
                kind="perlectio",
                subject_id=act_id,
                outcome="not-run",
                attempt=perlector_attempt_id(act_id, "perlegere", ordinal),
                inputs=capacity_inputs,
                payload=payload,
            )
            acknowledged += 1
            continue

        if reader is None:
            # First act of a live pass that actually clears capacity and needs
            # reading. One chair for the whole run, entered once here and
            # stopped once at the end: the sequential-serving posture, and the
            # reason this is a single `is None` rather than a per-act start.
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

        # The unprimed instrument, sampled by the run's own predeclared design
        # (`nuda_per_mille`, fixed before the run) -- an independent artifact
        # from the establishing pass, decided once per logical act rather than
        # once per capture, exactly as the control sample below is.
        nuda_sampled, control_sampled = _logical_sampling_decisions(context, logical_act_id)

        # Bind loop-local publication facts now; the callback runs before the
        # establishing arm and returns the immutable prior reference it embeds.
        publish_prior = partial(
            _publish_lectio_prior,
            context,
            act_key=act["act_key"],
            act_id=act_id,
            ordinal=ordinal,
            chair=chair,
            bases=bases,
            page_renders=page_renders,
            region_pixels=region_pixels,
            protocol_config=protocol_config,
            protocol_sha256=protocol_sha256,
            receipt_ref=receipt_ref,
        )

        # Every arm receives the complete presentation in one reader call.
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

        if nuda_sampled:
            _publish_lectio_nuda(
                context,
                act_key=act["act_key"],
                act_id=act_id,
                ordinal=ordinal,
                chair=chair,
                dossier=passes["lectio-nuda"]["dossier"],
                result=passes["lectio-nuda"]["result"],
                bases=bases,
                page_renders=page_renders,
                region_pixels=region_pixels,
                protocol_config=protocol_config,
                protocol_sha256=protocol_sha256,
                approval_ref=nuda_approval,
                receipt_ref=receipt_ref,
            )

        if control_sampled:
            _publish_primed_without_prior(
                context,
                act_key=act["act_key"],
                act_id=act_id,
                ordinal=ordinal,
                chair=chair,
                dossier=passes["primed-without-prior"]["dossier"],
                result=passes["primed-without-prior"]["result"],
                bases=bases,
                page_renders=page_renders,
                region_pixels=region_pixels,
                testimonia=testimonia,
                attachment_view=attachment_view,
                protocol_config=protocol_config,
                protocol_sha256=protocol_sha256,
                approval_ref=instrument_approval,
                receipt_ref=receipt_ref,
            )

        # Publication consumes the one establishing result; it never chooses or
        # merges capture-local readings.
        primed_dossier = _reseal_dossier(passes["perlectio"]["dossier"])
        result = passes["perlectio"]["result"]
        prior = primed_dossier["prior_draft"]
        # The prompt is reproduced from the retained dossier. In the fed arm it
        # is also the reader dossier; in the withheld arm `combined.py` removed
        # the prior text before the call and restored it only on this separate
        # evidence copy. The prompt builder ignores a withheld prior, so both
        # objects render the same bytes without giving the reader a side channel.
        prompt = prompts.prompt_evidence(chair, primed_dossier, protocol_config, protocol_sha256)

        # `declared_failure` was resolved and, in live mode, already refused
        # before any reader call above -- this is the one remaining use of the
        # value, deciding `reading` and `outcome` together.
        reading = "" if declared_failure == "no-readable-text" else result["text"]
        truncation_record = _reconciled_truncation(
            declared_failure=declared_failure,
            truncation_record=truncation.classify(
                reading, region_pixels=region_pixels, stop_reason=result["stop_reason"]
            ),
        )
        outcome = _resolve_outcome(
            declared_failure=declared_failure, truncation_record=truncation_record, text=reading
        )
        if outcome == "no-readable-text":
            # See the nuda publish path: whitespace resolved as unreadable is
            # published as the empty text its schema requires.
            reading = ""
        testimonium_references = {
            record["artifact_id"]: context.artifact_ref(
                ATTESTATORES, "testimonium", record["artifact_id"]
            )
            for record in testimonia
        }
        gaps = (
            _whole_act_gap(testimonia, testimonium_references)
            if outcome == "no-readable-text"
            else []
        )

        provenance = provenance_for(context, chair, attempted=True, receipt_ref=receipt_ref)
        payload = {
            "act_key": act["act_key"],
            "attempt_ordinal": ordinal,
            "text": reading,
            "basis": {
                "regions": bases,
                "testimonia": [
                    {
                        "chair": record["payload"]["chair"],
                        "artifact_id": record["artifact_id"],
                        "outcome": record["outcome"],
                        "reference": testimonium_references[record["artifact_id"]],
                    }
                    for record in testimonia
                ],
            },
            "dossier": primed_dossier,
            "prompt": prompt,
            "dissent": dissent_against(reading, dissent_testimonia(testimonia, attachment_view)),
            "truncation": truncation_record,
            "uncertain_spans": [],
            "gaps": gaps,
            "provenance": provenance,
            "lectio_kind": "primed-with-prior",
            "self_revision": departures(reading, prior["text"]),
            "protocol": {
                "selection_rule": protocol_config["selection_rule"],
                "page_shared_prefix_policy": protocol_config["page_shared_prefix_policy"],
                "draft_fed": context.draft_fed,
            },
        }
        # The record names the call its published text came from. Here that is
        # the establishing pass; the audit loop below re-points it at the
        # re-proof's own call in exactly the case where the re-proof's text is
        # the one published, beside `truncation` and `self_revision`, which move
        # for the same reason.
        payload_fields = with_engine_call(payload, result, _PERLECTIO_FIELDS)
        pending.append(
            {
                "act": act,
                "act_id": act_id,
                "order": declared_order[act_id],
                "bases": bases,
                "payload": payload,
                "fields": payload_fields,
                "outcome": outcome,
                # Deliberately NOT the decoded pixels: holding every act's
                # delivered images until the audit loop reaches it would grow
                # peak memory with the number of acts on the run -- a parish
                # of several hundred acts is several hundred sets of
                # page-sized buffers held at once, and the failure mode is an
                # OOM kill after the drafts are on disk and before any
                # Perlectio is published. The rare re-proof rebuilds its
                # pixels from the same sealed artifacts instead.
                "region_pixels": region_pixels,
                "declared_failure": declared_failure,
                "testimonia": testimonia,
                "attachment_view": attachment_view,
                "prior": prior,
                "autopsia": autopsia,
                "inputs": _reading_image_inputs(context, bases, page_renders, autopsia=autopsia)
                + list(testimonium_references.values())
                + [attachment_view["reference"], prior["reference"]]
                + engine_call_inputs(context, result.get("engine_call")),
            }
        )
        read += 1

    # The page flag pass receives these immutable Pass-B semi-finals together,
    # before any re-proof result exists.  Its output is therefore one
    # deterministic cross-act computation per page, with no cascade.
    semi_finals = []
    for row in pending:
        payload = row["payload"]
        bases = row["bases"]
        semi_finals.extend(
            audit_semi_finals_for_pages(
                act_id=row["act_id"],
                order=row["order"],
                text=payload["text"],
                bases=bases,
                dossier=payload["dossier"],
            )
        )
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
        page_ids = audit_page_ids(row["bases"])
        draft_payload = {
            "act_key": row["act"]["act_key"],
            "attempt_ordinal": payload["attempt_ordinal"],
            "semi_final_text": payload["text"],
            "page_ids": page_ids,
            "round_cap": audit_policy["round_cap"],
            "policy": policy_record,
            "flags": flags,
            "flag_location_basis": flag_location_basis(
                payload["dossier"], flags, semi_final_text=payload["text"]
            ),
        }
        audit.validate_draft(draft_payload)
        draft = context.publish(
            kind="audit-draft",
            subject_id=act_id,
            outcome="read",
            attempt=perlector_attempt_id(act_id, "perlegere", payload["attempt_ordinal"]),
            inputs=row["inputs"],
            payload=draft_payload,
        )
        draft_ref = context.input_ref(draft.relative_path)
        final_text = payload["text"]
        unresolved = bool(flags) and audit_policy["round_cap"] == 0
        # The plan this act's frozen flags imply, computed once by the function
        # `validate_chain` re-derives it with and `audit.audit_request` builds
        # the reader's copy from. What is sealed below, what the reader is
        # handed, and what every later consumer recomputes are therefore the
        # same computation over the same frozen flags rather than three
        # spellings that have to be kept agreeing.
        reproofs = audit.reproof_plan(flags, text_length=len(final_text))
        request_digest: str | None = None
        changes: list[dict[str, Any]] = []
        uncertainty: list[dict[str, Any]] = []
        payload_fields = row["fields"]
        # A re-proof reading is evidence of this Perlectio whether or not it
        # changed the text: it is the second thing that looked at this act's
        # pixels, and its response is what the `change_record` below reports on.
        # Bound as an input in both cases; named by `payload["engine_call"]` only
        # when its text is the one published.
        reproof_inputs: list[dict[str, str]] = []
        # The same predicate `validate_chain` re-derives from the frozen draft:
        # one spelling of "a re-proof request exists for this act".
        if audit.reproof_delivery_due(flags, audit_policy["round_cap"]):
            # Exactly one reader invocation for this act and audit round.  The
            # list of neutral locations is retained on the Perlectio below;
            # no flag result can reopen this page's frozen calculation.
            #
            # Re-proof must reload the establishing pass's complete atomic
            # presentation; limiting it to the flagged page would reintroduce a
            # capture-local reader call.
            reproof_pixels = atomic_delivered_pixels(
                row["autopsia"], read_bytes=context.tree.read_bytes, max_images=max_images
            )
            # Ordering constraint: `payload["text"]` and `payload["dossier"]`
            # are read here, BEFORE the re-proof result overwrites the final
            # text below — the request must carry the frozen semi-final its
            # locations index into, and the dossier must stay the sealed one.
            #
            # The instrument, delivered rather than only sealed. The draft is
            # already published, so its reference names bytes that exist and
            # carries their digest: the request the reader receives is bound to
            # the exact frozen semi-final its locations index into. `pass_kind`
            # stays routing -- everything the reader needs to know about this
            # pass is in the request (`reader.py`'s module docstring).
            #
            # The dossier goes through untouched. It used to travel as
            # `{**dossier, "semi_final_text": ...}`, which handed the reader an
            # object whose own `dossier_digest` no longer covered its contents.
            audit_request = audit.audit_request(
                act_key=row["act"]["act_key"],
                attempt_ordinal=payload["attempt_ordinal"],
                draft_ref=draft_ref,
                semi_final_text=payload["text"],
                flags=flags,
            )
            request_digest = audit.audit_digest(audit_request)
            # The producer enforces the delivery contract itself, so the
            # obligation binds whichever reader sits in the chair rather than
            # resting on each implementation remembering to call the seam's
            # validator (FixtureReader also calls it; twice is harmless).
            validate_audit_delivery(
                payload["dossier"], pass_kind="audit-reproof", audit_request=audit_request
            )
            reproof = reader.read(
                payload["dossier"],
                # The literal, not `audit.REPROOF_PASS_KIND`: every producer
                # call site spells its pass so `test_reader.py`'s pin can read
                # them out of this file and hold `reader.PASS_KINDS` to exactly
                # the set run.py calls. That pin is also what makes the literal
                # safe here -- a misspelling fails it rather than falling
                # through to the establishing branch.
                pass_kind="audit-reproof",
                delivered_pixels=reproof_pixels,
                # A copy, so a reader that mutated its input could not leave
                # the sealed digest describing an object that no longer exists
                # -- the exact shape of lie this seam was rebuilt to end.
                audit_request=copy.deepcopy(audit_request),
            )
            final_text = reproof["text"]
            pre_audit_text = payload["text"]
            reproof_inputs = engine_call_inputs(context, reproof.get("engine_call"))
            if final_text != payload["text"]:
                payload["text"] = final_text
                # `engine_call` names the call the published text came from, so
                # it moves with the text. Leaving the establishing call's record
                # here would bind a published reading to a response that did not
                # produce it -- the same false provenance `truncation` and
                # `self_revision` below were repaired for (audit finding H6).
                payload_fields = with_engine_call(payload, reproof, payload_fields)
                payload["dissent"] = dissent_against(
                    final_text, dissent_testimonia(row["testimonia"], row["attachment_view"])
                )
                # self_revision was computed against the pre-audit Pass-B
                # reading (audit finding H6); an audit-changed text is the one
                # actually published, so the recorded self-revision must
                # describe *its* departure from Pass A, not a reading that
                # never left the Perlector.
                payload["self_revision"] = departures(final_text, row["prior"]["text"])
                # The truncation instrument is the same case as `self_revision`
                # and was the last field still describing a reading nobody
                # published: three of its four signals are computed over the
                # reading text, and `outcome` is derived from the record, so an
                # audit-changed text left both stating what the *pre-audit* text
                # looked like -- and the re-proof's own engine word on why it
                # stopped was dropped entirely, so a re-proof cut off mid-emission
                # could replace established text while the record still read
                # `complete` (ARCHITECTURE: "truncation is a failure, not an
                # output"; GOVERNANCE 10).
                #
                # Pass C may only ever make this worse -- see
                # `_audited_truncation`.
                payload["truncation"] = _audited_truncation(
                    pass_b=payload["truncation"],
                    declared_failure=row["declared_failure"],
                    text=final_text,
                    region_pixels=row["region_pixels"],
                    stop_reason=reproof["stop_reason"],
                )
                row["outcome"] = _resolve_outcome(
                    declared_failure=row["declared_failure"],
                    truncation_record=payload["truncation"],
                    text=final_text,
                )
                if row["outcome"] == "no-readable-text":
                    # One emptiness rubric everywhere: the Pass-B path empties
                    # `text` for this outcome (whitespace was never established
                    # ink) and attaches the whole-act gap so the absence
                    # travels with the witness evidence that corroborates it.
                    # A re-proof that turned the reading unreadable published
                    # neither -- an act saying "nothing readable here" while
                    # carrying whitespace as its text and no evidence of the
                    # absence.
                    final_text = ""
                    payload["text"] = ""
                    payload["gaps"] = _whole_act_gap(
                        row["testimonia"],
                        {
                            record["artifact_id"]: context.artifact_ref(
                                ATTESTATORES, "testimonium", record["artifact_id"]
                            )
                            for record in row["testimonia"]
                        },
                    )
                    payload["dissent"] = dissent_against(
                        "", dissent_testimonia(row["testimonia"], row["attachment_view"])
                    )
                    payload["self_revision"] = departures("", row["prior"]["text"])
                elif payload["gaps"] and all(
                    gap.get("position") == "whole-act" for gap in payload["gaps"]
                ):
                    # The symmetric direction: a Pass-B no-readable-text act
                    # carried the whole-act gap, and a re-proof that restored
                    # readable text would otherwise publish established text
                    # BESIDE a gap claiming the whole act is empty --
                    # validate_annotations refuses exactly that, so the valid
                    # re-proof could never publish. Only the whole-act shape
                    # clears; a legitimate narrower gap is not this case.
                    payload["gaps"] = []
            # After the projection, not before it: `validate_chain` recomputes
            # the change record from the draft's semi-final against the
            # PUBLISHED text, so the record must describe the projected text.
            changes = audit.change_record(pre_audit_text, final_text, flags)
        if unresolved:
            for flag in flags:
                start, end = flag["location"]["start"], flag["location"]["end"]
                if start < end:
                    uncertainty.append(
                        {"start": start, "end": end, "reason": "audit-round-cap-exhausted"}
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
        }
        audit.validate_finding(
            finding_payload,
            text=final_text,
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
        # R5b's uncertainty is carried on the existing Perlectio layer until
        # R8 reconciles the canonical export schema.  An unresolved flag never
        # silently remains a clean `read`: it becomes an explicit span and the
        # Recensor consumes the companion `unresolved` fact below.
        payload["uncertain_spans"] = [
            {"start": span["start"], "end": span["end"], "alternatives": [], "confidence": "low"}
            for span in uncertainty
        ]
        payload["audit"] = {
            "draft_ref": draft_ref,
            "finding_ref": finding_ref,
            "finding_digest": audit.audit_digest(finding_payload),
            "unresolved": unresolved,
            "reproofs": reproofs,
            # Which request the reader was actually handed, or `None` where
            # none was: an act with no flag has nothing to re-prove, and an act
            # whose sealed cap is spent records its flags as exhausted-cap
            # uncertainty instead of running a round it may not run. Both are
            # honest absences, and a record that could not tell them from a
            # delivered re-proof is the ambiguity `reproofs` alone used to
            # carry.
            "request_digest": request_digest,
        }
        # Dedup is scoped to the re-proof's own references: those are the only
        # ones a live engine can legitimately repeat, when a re-proof answers
        # with the same bytes as the establishing call and the content-addressed
        # store names the same path twice. `row["inputs"]` (the image, testimonia,
        # attachment and prior references) can never legitimately repeat, so a
        # duplicate there stays under the envelope's own double-count refusal
        # instead of being silently absorbed here.
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
        # The producer and every later consumer use the same cross-record
        # validation. Run it before the Perlectio is published so a drifted
        # draft/finding relationship never becomes an unreadable artifact.
        audit.validate_chain(
            context.tree,
            {"payload": payload, "inputs": reading_inputs},
            act_id,
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

    # Before the seal, not after it: a chair still resident while the completion
    # boundary is written would let a failed shutdown be reported over a sealed
    # stage. `main`'s own `finally` still holds for the paths that never reach
    # this line, and `close` is idempotent so the two never fight.
    service.close()
    context.seal_boundary()
    context.finish()
    return EXIT_COMPLETE


def _next_attempt(context, act_id: str, regions: list[dict]) -> int:
    """Which reading attempt this is, derived from the act rather than from history.

    Counting existing Perlectiones and adding one would make the answer depend on
    how many times the stage had been *invoked*, so a rerun of an unchanged run
    would append a reading nobody asked for and the Archetypus would then point at
    it. The reading attempt is instead a function of the act's own state: one
    reading of the proposal, and one more for each recovery region cut since. A
    rerun that changed nothing therefore recomputes the same ordinal, produces the
    same bytes, and is reused rather than rewritten.

    **The one attempt model, and the one reader for it.** Witness testimony does
    not appear in this derivation, and that is the model rather than an omission:
    a Testimonium is a clue that primes a reading, never the ink the reading is
    established from, so a second look by a witness does not make a second reading
    attempt exist (GOVERNANCE 3, 11). `pipeline/3_attestatores/run.py::reread_pass`
    therefore closes its own window at the reading rather than moving this number.

    Counted through `recovery_region_count`, the same shared reader the Recensor,
    Archetypus and Armarium ask, because this copy asked only whether an origin
    equalled `"recovery"` and silently counted every other value — including an
    unknown or malformed one — as zero. A resealed Designator tree carrying origin
    `"mystery"` was therefore read and published here at attempt 1 and became fatal
    only at the next stage, by which time this Perlectio was already immutable and
    the retry had nowhere to go. Refused before any model call or publication now
    (Sol-S5).

    **A crashed pass resumes here; it does not append here.** Unit 2 asks what a
    Perlector pass interrupted mid-stage does about the acts it already read, and
    the answer is: nothing. Probing this act's identities and landing on the first
    free one was tried and is wrong, because this ordinal is not free-floating —
    `recovery_region_count` is re-derived by the Recensor, the Archetypus and the
    Armarium, each of which requires an act's reading count to equal its recovery
    crop count plus one. A resumed pass that appended a second reading for an act
    with no recrop therefore sealed its own boundary, reported success, and left
    the run dead one stage later on `act ... carries 2 Perlectio attempt(s) for 0
    recovery crop(s)`. `witness_bound_reading_acts` states the same rule from the
    Attestatores side: a reading is made only by a crop, and new ink after a
    reading routes through a Recensor recovery request, which mints a region and
    moves this number.

    So a resume recomputes the same ordinal and republishes. Every chair that
    exists today is deterministic, so that republication is byte-identical and
    the RunTree reuses it. `ARCHITECTURE.md`'s vLLM caveat says a future real
    chair need not be bit-identical, and there the republication is refused
    (`IncompatibleReuse`) rather than allowed to overwrite immutable evidence:
    loud, nothing written, nothing lost. The forward path from that refusal is
    the one the design already has — a Recensor recovery request — and it is
    deliberately not a second reading minted here, because a reading nobody
    requested is what GOVERNANCE 11 refuses and what all three consumers reject.
    """
    return recovery_region_count(act_id, regions) + 1


if __name__ == "__main__":
    raise SystemExit(run_stage(main))
