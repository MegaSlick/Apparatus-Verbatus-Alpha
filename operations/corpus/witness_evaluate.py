"""Read-only per-witness RecordGold scoring.

On a run read by page, each witness the Perlector's page feed showed is scored
from its own units: a unit belongs to the reference record holding most of its
box, and a record no unit lies on is an empty hypothesis. Otherwise Designator
geometry is paired once, independently of witness and reference text, and each
chair is scored from the exact sealed Testimonium slice that the current
``act-attachment`` assigns to that matched proposal.
"""

from __future__ import annotations

import argparse
import json
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Collection, Mapping, Sequence

from common.contracts.canonical import (
    canonical_bytes,
    digest_bytes,
    self_hash,
    verify_self_hash,
)
from common.contracts.errors import ContractError
from common.contracts.stages import ATTESTATORES, EXEMPLAR, PERLECTOR
from common.exemplar_boundary import verify_sealed_page_pixels
from common.imaging import dimensions
from common.native_witness import (
    validate_page_testimonium_payload,
    validate_presented_page_binding,
)
from common.runtree.store import RunTree
from common.stage import latest_attempt
from operations.spike_perlector.models import OutputStatus
from operations.spike_perlector.normalization import GRAPHEMIC_V1
from operations.spike_perlector.scoring import score_response

from . import CorpusRefusal
from .cache import write_new_file
from .compare import (
    ReadOnlyRunTree,
    compare_page_geometry,
    load_exemplar_page_shas,
    load_pipeline_proposal_acts,
)
from .local_admission import validate_local_admission_ledger
from .reference import validate_reference_page

DESCRIPTION = "Read-only per-witness RecordGold scoring over sealed act attachments."

SCHEMA = "recordgold-witness-evaluation.v1"
CHAIRS = ("attestator_1", "attestator_2", "attestator_3")

WITNESS_EVALUATION_REFUSAL_REASONS = frozenset(
    {
        "malformed-record",
        "missing-input-file",
        "output-exists",
        "output-in-run-tree",
        "reference-ledger-invalid",
        "reference-page-collision",
        "reference-page-invalid",
        "reference-page-not-in-ledger",
        "reference-page-not-in-run",
        "self-hash-mismatch",
    }
)


class Refusal(CorpusRefusal):
    reasons = WITNESS_EVALUATION_REFUSAL_REASONS


@dataclass(frozen=True, slots=True)
class _SealedPageBinding:
    ordinal: int
    image_path: str
    sha256: str
    size: tuple[int, int]


def witness_reading(
    attachment: Mapping[str, Any], testimonium: Mapping[str, Any]
) -> tuple[OutputStatus, str | None, str | None]:
    """Return one defensible sealed excerpt, never choosing it with reference text."""
    if attachment.get("attached") is not True:
        return OutputStatus.UNAVAILABLE, None, "unattached"
    if attachment.get("comparable") is not True:
        return OutputStatus.UNAVAILABLE, None, "not-comparable"

    payload = testimonium.get("payload")
    if not isinstance(payload, Mapping) or not isinstance(payload.get("payload"), str):
        raise Refusal("malformed-record: referenced Testimonium has no retained text payload")
    outcome = testimonium.get("outcome")
    if outcome not in {"read", "genuinely-empty"}:
        return OutputStatus.UNAVAILABLE, None, f"non-reading-{outcome!r}"

    # Attachment health is the current act-attempt currency check even for a
    # page witness. Completion of the scored text belongs to its Testimonium.
    health = payload.get("content_health")
    if not isinstance(health, Mapping) or health.get("recordable") is not True:
        return OutputStatus.MALFORMED, None, "unrecordable-response"
    if health.get("truncated") is None:
        return OutputStatus.UNAVAILABLE, None, "unknown-truncation"
    if not isinstance(health.get("truncated"), bool):
        raise Refusal("malformed-record: referenced Testimonium has invalid truncation health")

    text = payload["payload"]
    span = attachment.get("span")
    if span is None:
        return OutputStatus.UNAVAILABLE, None, "no-defensible-span"
    if (
        not isinstance(span, Mapping)
        or set(span) != {"start", "end"}
        or not all(isinstance(span[key], int) and not isinstance(span[key], bool) for key in span)
    ):
        raise Refusal("malformed-record: attachment span is not a closed integer range")
    if not 0 <= span["start"] <= span["end"] <= len(text):
        raise Refusal("malformed-record: attachment span exceeds its retained Testimonium")
    status = OutputStatus.TRUNCATED if health["truncated"] else OutputStatus.COMPLETE
    return status, text[span["start"] : span["end"]], None


def _score_row(
    reference: Mapping[str, Any],
    *,
    pipeline_act_id: str | None,
    chair: str,
    status: OutputStatus,
    text: str | None,
    reason: str | None,
) -> dict[str, Any]:
    score = score_response(reference["text"], status=status, text=text, profile=GRAPHEMIC_V1)
    return {
        "record_id": reference["record_id"],
        "pipeline_act_id": pipeline_act_id,
        "chair": chair,
        "status": status.value,
        "reason": reason,
        "cer": score.cer.edits.errors,
        "cer_units": score.cer.reference_units,
        "wer": score.wer.edits.errors,
        "wer_units": score.wer.reference_units,
    }


def _totals(rows: Sequence[Mapping[str, Any]], chairs: Sequence[str]) -> dict[str, dict[str, Any]]:
    totals: dict[str, dict[str, Any]] = {}
    for chair in chairs:
        chair_rows = [row for row in rows if row["chair"] == chair]
        statuses = Counter(row["status"] for row in chair_rows)
        totals[chair] = {
            "references": len(chair_rows),
            "scoreable": statuses[OutputStatus.COMPLETE.value]
            + statuses[OutputStatus.TRUNCATED.value],
            "statuses": {status.value: statuses[status.value] for status in OutputStatus},
            "cer_errors": sum(row["cer"] for row in chair_rows),
            "cer_units": sum(row["cer_units"] for row in chair_rows),
            "wer_errors": sum(row["wer"] for row in chair_rows),
            "wer_units": sum(row["wer_units"] for row in chair_rows),
            "missing_proposals": sum(row["reason"] == "missing-proposal" for row in chair_rows),
        }
    return totals


def evaluate_page(
    *,
    reference_page: dict[str, Any],
    source_page_ordinal: int,
    proposals: list[dict[str, Any]],
    attachments: Mapping[str, Mapping[str, list[dict[str, Any]]]],
    chairs: tuple[str, ...],
) -> dict[str, Any]:
    """Score every reference act for every chair on one source page."""
    reference_page = validate_reference_page(reference_page)
    geometry = compare_page_geometry(reference_page, proposals)
    references = {act["physical_act_id"]: act for act in reference_page["acts"]}
    rows: list[dict[str, Any]] = []
    for pair in geometry["matched_pairs"]:
        act_id = pair["pipeline_act_id"]
        reference = references[pair["reference_physical_act_id"]]
        for chair in chairs:
            candidates = attachments.get(act_id, {}).get(chair, [])
            matches = [
                item
                for item in candidates
                if not item["attachment"]["page_witness"]
                or item["attachment"]["page_ordinal"] == source_page_ordinal
            ]
            if len(matches) > 1:
                raise Refusal("malformed-record: duplicate attachments for one matched act page")
            if not matches:
                status, text, reason = OutputStatus.MISSING, None, "missing-act-attachment"
            else:
                status, text, reason = witness_reading(
                    matches[0]["attachment"], matches[0]["testimonium"]
                )
            rows.append(
                _score_row(
                    reference,
                    pipeline_act_id=act_id,
                    chair=chair,
                    status=status,
                    text=text,
                    reason=reason,
                )
            )

    # A missed proposal is checked ink and contributes full deletions to all
    # three chairs instead of disappearing from their denominators.
    for miss in geometry["misses"]:
        reference = references[miss["physical_act_id"]]
        for chair in chairs:
            rows.append(
                _score_row(
                    reference,
                    pipeline_act_id=None,
                    chair=chair,
                    status=OutputStatus.MISSING,
                    text=None,
                    reason="missing-proposal",
                )
            )

    body = {
        "schema": SCHEMA,
        "reference_page_self_hash": reference_page["self_hash"],
        "source_page_ordinal": source_page_ordinal,
        "geometry": geometry,
        "rows": rows,
        "totals": _totals(rows, chairs),
    }
    body["self_hash"] = self_hash(body)
    return body


def _reference_pages(path: Path) -> dict[str, dict[str, Any]]:
    if not path.is_file():
        raise Refusal(f"missing-input-file: {path} is not a file")
    pages: dict[str, dict[str, Any]] = {}
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except UnicodeDecodeError as error:
        raise Refusal(f"reference-page-invalid: {path} is not UTF-8: {error}") from error
    for number, line in enumerate(lines, 1):
        if not line.strip():
            continue
        try:
            page = validate_reference_page(json.loads(line))
        except (ValueError, CorpusRefusal) as error:
            raise Refusal(f"reference-page-invalid: line {number}: {error}") from None
        digest = page["page"]["sha256"]
        if digest in pages:
            raise Refusal(f"reference-page-collision: duplicate page sha256 {digest}")
        pages[digest] = page
    return pages


def sealed_page_bindings(tree: ReadOnlyRunTree) -> dict[str, _SealedPageBinding]:
    """Verify every sealed Exemplar page and retain its immutable binding facts."""
    run = tree.read_run()
    sources: dict[int, dict[str, Any]] = {}
    for source in run.get("source_manifest", []):
        ordinal = source.get("ordinal") if isinstance(source, dict) else None
        if not isinstance(ordinal, int) or isinstance(ordinal, bool):
            raise Refusal("malformed-record: run source has no integer ordinal")
        if ordinal in sources:
            raise Refusal("malformed-record: run source ordinal is duplicated")
        sources[ordinal] = source

    pages: dict[str, _SealedPageBinding] = {}
    page_ids_by_ordinal: dict[int, str] = {}
    for entry in tree.build_manifest(EXEMPLAR)["artifacts"]:
        if entry["kind"] != "page":
            continue
        record = tree.read_artifact(EXEMPLAR, "page", entry["artifact_id"])
        if record.get("outcome") != "sealed":
            continue
        payload = record.get("payload")
        ordinal = payload.get("ordinal") if isinstance(payload, dict) else None
        page_id = record.get("subject_id")
        if (
            not isinstance(ordinal, int)
            or isinstance(ordinal, bool)
            or not isinstance(page_id, str)
            or not page_id
        ):
            raise Refusal("malformed-record: sealed Exemplar page has no identity/ordinal")
        source = sources.get(ordinal)
        if source is None:
            raise Refusal("malformed-record: sealed Exemplar page has no submitted source")
        try:
            pixels = verify_sealed_page_pixels(tree, run, source, record)
            size = dimensions(pixels)
        except (ContractError, ValueError) as error:
            raise Refusal(f"malformed-record: sealed Exemplar page is invalid: {error}") from error
        if page_id in pages:
            raise Refusal("malformed-record: sealed Exemplar page identity is duplicated")
        if ordinal in page_ids_by_ordinal:
            raise Refusal("malformed-record: sealed Exemplar page ordinal is duplicated")
        page_ids_by_ordinal[ordinal] = page_id
        pages[page_id] = _SealedPageBinding(
            ordinal=ordinal,
            image_path=payload["image_path"],
            sha256=payload["source_sha256"],
            size=size,
        )
    return pages


def _validate_page_binding(
    payload: Mapping[str, Any],
    *,
    sealed_pages: Mapping[str, _SealedPageBinding],
    read_bytes: Callable[[str], bytes],
) -> None:
    presented = payload.get("presented")
    page_id = presented.get("source_page_id") if isinstance(presented, Mapping) else None
    page = sealed_pages.get(page_id) if isinstance(page_id, str) else None
    if page is None:
        raise Refusal(
            "malformed-record: page Testimonium presentation names no sealed Exemplar page"
        )
    try:
        pixels = read_bytes(page.image_path)
        if digest_bytes(pixels) != page.sha256 or dimensions(pixels) != page.size:
            raise Refusal("malformed-record: sealed Exemplar page pixels changed before binding")
        validate_presented_page_binding(
            dict(presented),
            page_ordinal=page.ordinal,
            page_image_path=page.image_path,
            page_sha256=page.sha256,
            page_size=page.size,
            page_bytes=pixels,
        )
    except (ContractError, OSError, ValueError) as error:
        raise Refusal(
            f"malformed-record: page Testimonium presentation is not bound to its sealed "
            f"Exemplar page: {error}"
        ) from error


def attachment_index(
    tree: ReadOnlyRunTree,
    *,
    sealed_pages: Mapping[str, _SealedPageBinding] | None = None,
) -> dict[str, dict[str, list[dict[str, Any]]]]:
    if sealed_pages is None:
        sealed_pages = sealed_page_bindings(tree)
    histories: dict[str, list[dict[str, Any]]] = {}
    act_testimonia: dict[tuple[str, str], list[dict[str, Any]]] = {}
    page_testimonia: dict[tuple[int, str], list[dict[str, Any]]] = {}
    for entry in tree.build_manifest(ATTESTATORES)["artifacts"]:
        if entry["kind"] == "act-attachment":
            histories.setdefault(entry["subject_id"], []).append(
                tree.read_artifact(ATTESTATORES, "act-attachment", entry["artifact_id"])
            )
        elif entry["kind"] == "testimonium":
            record = tree.read_artifact(ATTESTATORES, "testimonium", entry["artifact_id"])
            chair = record.get("payload", {}).get("chair")
            if not isinstance(chair, str):
                raise Refusal("malformed-record: Testimonium has no chair")
            act_testimonia.setdefault((entry["subject_id"], chair), []).append(record)
        elif entry["kind"] == "page-testimonium":
            record = tree.read_artifact(ATTESTATORES, "page-testimonium", entry["artifact_id"])
            payload = record.get("payload")
            chair = payload.get("chair") if isinstance(payload, dict) else None
            ordinal = payload.get("page_ordinal") if isinstance(payload, dict) else None
            if (
                not isinstance(chair, str)
                or not isinstance(ordinal, int)
                or isinstance(ordinal, bool)
            ):
                raise Refusal(
                    "malformed-record: page Testimonium has no chair/source-page identity"
                )
            page_testimonia.setdefault((ordinal, chair), []).append(record)

    current_acts = {
        pair: latest_attempt(
            records,
            f"Testimonium for {pair!r}",
            operation=f"read:{pair[1]}",
        )
        for pair, records in act_testimonia.items()
    }
    current_pages = {
        pair: latest_attempt(
            records,
            f"page Testimonium for page {pair[0]}, chair {pair[1]}",
            operation=f"read:{pair[1]}",
        )
        for pair, records in page_testimonia.items()
    }

    indexed: dict[str, dict[str, list[dict[str, Any]]]] = {}
    for act_id, records in histories.items():
        record = latest_attempt(records, f"act-attachment for {act_id}", operation="act-attachment")
        rows = record.get("payload", {}).get("attachments")
        if not isinstance(rows, list):
            raise Refusal("malformed-record: act attachment has no attachment list")
        seen_pairs: set[tuple[str, int | None]] = set()
        for attachment in rows:
            if not isinstance(attachment, dict) or not isinstance(attachment.get("chair"), str):
                raise Refusal("malformed-record: act attachment has malformed chair entry")
            if attachment["chair"] not in CHAIRS:
                raise Refusal("malformed-record: act attachment names an unknown chair")
            if not isinstance(attachment.get("page_witness"), bool):
                raise Refusal("malformed-record: attachment has no page-witness scope")
            if not isinstance(attachment.get("attached"), bool) or not isinstance(
                attachment.get("comparable"), bool
            ):
                raise Refusal(
                    "malformed-record: attachment has no boolean attached/comparable facts"
                )
            if attachment["comparable"] and not attachment["attached"]:
                raise Refusal("malformed-record: attachment is comparable without being attached")
            page_ordinal = attachment.get("page_ordinal")
            if attachment["page_witness"]:
                if not isinstance(page_ordinal, int) or isinstance(page_ordinal, bool):
                    raise Refusal("malformed-record: page attachment has no page ordinal")
            elif page_ordinal is not None:
                raise Refusal("malformed-record: act attachment carries a page ordinal")
            pair = (attachment["chair"], page_ordinal)
            if pair in seen_pairs:
                raise Refusal("malformed-record: duplicate attachment chair/source-page pair")
            seen_pairs.add(pair)
            reference = attachment.get("testimonium_ref")
            if not isinstance(reference, dict):
                raise Refusal("malformed-record: attachment has no Testimonium reference")
            kind = "page-testimonium" if attachment["page_witness"] else "testimonium"
            testimony = tree.read_artifact_reference(
                reference,
                stage=ATTESTATORES,
                kind=kind,
                subject_id=None if attachment["page_witness"] else act_id,
            )
            payload = testimony.get("payload")
            if not isinstance(payload, dict) or payload.get("chair") != attachment["chair"]:
                raise Refusal("malformed-record: attachment points to another chair")
            current_act = current_acts.get((act_id, attachment["chair"]))
            if current_act is None:
                raise Refusal("malformed-record: attachment has no current act Testimonium")
            if attachment.get("content_health") != current_act.get("payload", {}).get(
                "content_health"
            ):
                raise Refusal(
                    "malformed-record: attachment health differs from current act Testimonium"
                )
            if attachment["page_witness"]:
                try:
                    validate_page_testimonium_payload(
                        payload,
                        testimonium_id=testimony.get("artifact_id"),
                        read_bytes=tree.read_bytes,
                    )
                except ContractError as error:
                    raise Refusal(
                        f"malformed-record: referenced page Testimonium is invalid: {error}"
                    ) from error
                _validate_page_binding(
                    payload,
                    sealed_pages=sealed_pages,
                    read_bytes=tree.read_bytes,
                )
                if payload["page_ordinal"] != page_ordinal:
                    raise Refusal("malformed-record: attachment points to another page")
                current = current_pages.get((page_ordinal, attachment["chair"]))
            else:
                current = current_act
            if current is None or testimony.get("artifact_id") != current.get("artifact_id"):
                raise Refusal("malformed-record: attachment points to a stale Testimonium")
            indexed.setdefault(act_id, {}).setdefault(attachment["chair"], []).append(
                {"attachment": attachment, "testimonium": testimony}
            )
    return indexed


def page_health_counts(
    records: list[Mapping[str, Any]],
    *,
    page_sha256_by_ordinal: Mapping[int, str],
    sealed_pages: Mapping[str, _SealedPageBinding],
    read_bytes: Callable[[str], bytes],
    chairs: tuple[str, ...],
) -> dict[str, dict[str, int]]:
    """Tally current sealed page responses, keyed through their source ordinal."""
    result = {
        chair: {
            "missing": len(page_sha256_by_ordinal),
            "truncated_true": 0,
            "truncated_false": 0,
            "truncated_null": 0,
            "failed_model_response": 0,
        }
        for chair in chairs
    }
    histories: dict[tuple[int, str], list[Mapping[str, Any]]] = {}
    for record in records:
        payload = record.get("payload")
        if not isinstance(payload, Mapping):
            raise Refusal("malformed-record: page Testimonium has no payload")
        chair, ordinal = payload.get("chair"), payload.get("page_ordinal")
        if not isinstance(ordinal, int) or isinstance(ordinal, bool):
            raise Refusal("malformed-record: page Testimonium has no source page ordinal")
        if chair not in result or ordinal not in page_sha256_by_ordinal:
            continue
        histories.setdefault((ordinal, chair), []).append(record)

    for (ordinal, chair), history in histories.items():
        current = latest_attempt(
            list(history),
            f"page Testimonium for page {ordinal}, chair {chair}",
            operation=f"read:{chair}",
        )
        payload = current["payload"]
        try:
            validate_page_testimonium_payload(
                dict(payload),
                testimonium_id=current.get("artifact_id"),
                read_bytes=read_bytes,
            )
        except ContractError as error:
            raise Refusal(f"malformed-record: page Testimonium is invalid: {error}") from error
        _validate_page_binding(
            payload,
            sealed_pages=sealed_pages,
            read_bytes=read_bytes,
        )
        if payload["presented"]["source_page_ordinal"] != ordinal:
            raise Refusal("malformed-record: page Testimonium presentation names another page")
        result[chair]["missing"] -= 1
        health = payload.get("content_health")
        if not isinstance(health, Mapping) or health.get("truncated") not in {True, False, None}:
            raise Refusal("malformed-record: page Testimonium has invalid content health")
        truncation = health["truncated"]
        key = (
            "truncated_true"
            if truncation is True
            else "truncated_false"
            if truncation is False
            else "truncated_null"
        )
        result[chair][key] += 1
        if current.get("outcome") == "failed":
            result[chair]["failed_model_response"] += 1
    return result


def _aggregate_page_totals(
    reports: Sequence[Mapping[str, Any]], chairs: tuple[str, ...]
) -> dict[str, dict[str, Any]]:
    return _totals([row for report in reports for row in report["rows"]], chairs)


def evaluate_run(
    *,
    tree: RunTree,
    ledger_path: Path,
    reference_pages_path: Path,
    page_ids: Collection[str],
    chairs: tuple[str, ...] = CHAIRS,
) -> dict[str, Any]:
    """Build one read-only, self-hashed report for explicit admitted page ids."""
    requested = list(page_ids)
    if not requested or any(not isinstance(page_id, str) or not page_id for page_id in requested):
        raise Refusal("missing-input-file: at least one non-empty page id is required")
    if len(set(requested)) != len(requested):
        raise Refusal("malformed-record: duplicate selected page id")
    if len(set(chairs)) != len(chairs):
        raise Refusal("malformed-record: duplicate chair")

    if tuple(chairs) != CHAIRS:
        raise Refusal("malformed-record: witness evaluation requires all three chairs")

    try:
        ledger_bytes = ledger_path.read_bytes()
        ledger = validate_local_admission_ledger(json.loads(ledger_bytes))
    except (OSError, UnicodeDecodeError, ValueError, CorpusRefusal) as error:
        raise Refusal(f"reference-ledger-invalid: {error}") from error
    pages = _reference_pages(reference_pages_path)

    admitted_by_page: dict[str, set[tuple[str, str]]] = {}
    for row in ledger["rows"]:
        if row["decision"] == "admitted":
            admitted_by_page.setdefault(row["page_id"], set()).add(
                (row["page_sha256"], row["reference_page_self_hash"])
            )
    selected: dict[str, tuple[str, str]] = {}
    for page_id in requested:
        identities = admitted_by_page.get(page_id, set())
        if len(identities) != 1:
            raise Refusal(
                "missing-input-file: selected page id is absent or ambiguous in admitted reference ledger"
            )
        selected[page_id] = next(iter(identities))

    read_only = ReadOnlyRunTree(tree)
    sealed_pages = sealed_page_bindings(read_only)
    source_shas = load_exemplar_page_shas(read_only)
    ordinal_by_sha: dict[str, int] = {}
    for ordinal, digest in source_shas.items():
        if digest in ordinal_by_sha:
            raise Refusal("malformed-record: two sealed source pages carry the same sha256")
        ordinal_by_sha[digest] = ordinal
    proposals = load_pipeline_proposal_acts(read_only)
    attached = attachment_index(read_only, sealed_pages=sealed_pages)
    page_records = [
        read_only.read_artifact(ATTESTATORES, "page-testimonium", entry["artifact_id"])
        for entry in read_only.build_manifest(ATTESTATORES)["artifacts"]
        if entry["kind"] == "page-testimonium"
    ]

    reports = []
    # Two selected page IDs may name the same digest. Nothing above refuses it:
    # the ledger does not enforce unique `page_sha256` and the reference-hash
    # sets collapse duplicates, so both iterations would score the same page and
    # ordinal -- doubling `reports`, each chair's totals and `missing_proposals`
    # while `selected_ordinals` and `page_health` kept one entry.
    # Refused here, beside the duplicate page-ID check, before any scoring.
    digests_seen: dict[str, str] = {}
    for page_id in sorted(selected):
        digest = selected[page_id][0]
        if digest in digests_seen:
            raise Refusal(
                f"reference-page-collision: selected pages {digests_seen[digest]!r} and "
                f"{page_id!r} name the same page sha256 {digest}; one page is scored once"
            )
        digests_seen[digest] = page_id

    selected_ordinals: dict[int, str] = {}
    for page_id in sorted(selected):
        digest, expected_reference_hash = selected[page_id]
        page = pages.get(digest)
        if page is None or page["self_hash"] != expected_reference_hash:
            raise Refusal(
                f"reference-page-not-in-ledger: selected page {page_id!r} has no exact admitted reference page"
            )
        ordinal = ordinal_by_sha.get(digest)
        if ordinal is None:
            raise Refusal(
                f"reference-page-not-in-run: selected page {page_id!r} was not sealed by the Exemplar"
            )
        selected_ordinals[ordinal] = digest
        reports.append(
            evaluate_page(
                reference_page=page,
                source_page_ordinal=ordinal,
                proposals=[proposal for proposal in proposals if proposal["page_sha256"] == digest],
                attachments=attached,
                chairs=chairs,
            )
        )
    body = {
        "schema": SCHEMA,
        "run_id": tree.run_id,
        "ledger_sha256": digest_bytes(ledger_bytes),
        "selected_page_ids": sorted(requested),
        "chairs": list(chairs),
        "reference_records": sum(len(report["rows"]) for report in reports) // len(chairs),
        "page_health": page_health_counts(
            page_records,
            page_sha256_by_ordinal=selected_ordinals,
            sealed_pages=sealed_pages,
            read_bytes=read_only.read_bytes,
            chairs=chairs,
        ),
        "pages": reports,
        "totals": _aggregate_page_totals(reports, chairs),
    }
    body["self_hash"] = self_hash(body)
    return body


# --- page path: the witnesses the page feed showed ------------------------------------

PAGE_SCHEMA = "recordgold-witness-evaluation.page.v1"
PAGE_FEED = "page-feed"


def _area_overlap(a: Mapping[str, int], b: Mapping[str, int]) -> int:
    width = min(a["x"] + a["w"], b["x"] + b["w"]) - max(a["x"], b["x"])
    height = min(a["y"] + a["h"], b["y"] + b["h"]) - max(a["y"], b["y"])
    return max(width, 0) * max(height, 0)


def _unit_record(unit_box: Mapping[str, int], acts: Sequence[Mapping[str, Any]]) -> str | None:
    """The reference record a unit lies on: the one holding most of its area, at least half.

    A unit on no record (marginalia, a heading, ink between records) belongs to
    none, and a tie goes to the lower record id, so the assignment depends on
    geometry alone and never on what either text says.
    """
    best_overlap, best = 0, None
    for act in sorted(acts, key=lambda act: act["record_id"]):
        overlap = _area_overlap(unit_box, act["region"])
        if overlap > best_overlap:
            best_overlap, best = overlap, act["record_id"]
    return best if best is not None and 2 * best_overlap >= unit_box["w"] * unit_box["h"] else None


def _witness_name(witness: Mapping[str, Any]) -> str:
    return witness["chair"] if witness.get("chair") else witness["witness_label"]


def evaluate_feed_page(
    *, reference_page: dict[str, Any], feed: Mapping[str, Any]
) -> dict[str, Any]:
    """Score every reference record for every witness row of one page feed.

    A witness's text for a record is its units lying on that record
    (`_unit_record`), joined in the witness's own order. A record no unit lies
    on is an empty hypothesis (`no-unit-on-record`); a witness that did not
    read, or whose units carry no box, gives every record an empty hypothesis
    by name. Every row is kept, so the denominator is every record on the page
    for every witness the feed showed.
    """
    reference_page = validate_reference_page(reference_page)
    acts = sorted(reference_page["acts"], key=lambda act: act["record_id"])
    rows: list[dict[str, Any]] = []
    units: dict[str, dict[str, int]] = {}
    witnesses = sorted(feed.get("witnesses") or [], key=_witness_name)
    for witness in witnesses:
        name = _witness_name(witness)
        if name in units:
            raise Refusal(f"malformed-record: page feed shows witness {name!r} twice")
        own = witness.get("units") or []
        boxed = [unit for unit in own if unit.get("box_px") is not None]
        by_record: dict[str, list[str]] = {}
        off_record = 0
        for unit in boxed:
            record_id = _unit_record(unit["box_px"], acts)
            if record_id is None:
                off_record += 1
            else:
                by_record.setdefault(record_id, []).append(unit["text"])
        units[name] = {
            "units": len(own),
            "boxed": len(boxed),
            "on_a_record": len(boxed) - off_record,
            "on_no_record": off_record,
        }
        truncated = (witness.get("answer_health") or {}).get("truncated")
        for act in acts:
            text: str | None = None
            if witness.get("outcome") != "read":
                status, reason = OutputStatus.UNAVAILABLE, f"witness-{witness.get('outcome')}"
            elif own and not boxed:
                status, reason = OutputStatus.UNAVAILABLE, "witness-units-unboxed"
            elif act["record_id"] not in by_record:
                status, reason = OutputStatus.MISSING, "no-unit-on-record"
            else:
                text = "\n".join(by_record[act["record_id"]])
                status = OutputStatus.TRUNCATED if truncated is True else OutputStatus.COMPLETE
                reason = "truncation-unknown" if truncated is None else None
            rows.append(
                _score_row(
                    act,
                    pipeline_act_id=None,
                    chair=name,
                    status=status,
                    text=text,
                    reason=reason,
                )
            )
    names = tuple(units)
    body = {
        "schema": PAGE_SCHEMA,
        "reference_page_self_hash": reference_page["self_hash"],
        "page_id": feed["page_id"],
        "source_page_ordinal": feed["page_ordinal"],
        "witness_testimony": feed.get("witness_testimony"),
        "units": units,
        "rows": rows,
        "totals": _totals(rows, names),
    }
    body["self_hash"] = self_hash(body)
    return body


def page_feeds(tree: ReadOnlyRunTree) -> dict[int, dict[str, Any]]:
    """Every Perlector page feed of a page-read run, by page ordinal."""
    feeds: dict[int, dict[str, Any]] = {}
    for entry in tree.build_manifest(PERLECTOR)["artifacts"]:
        if entry["kind"] != PAGE_FEED:
            continue
        feed = tree.read_artifact(PERLECTOR, PAGE_FEED, entry["artifact_id"])["payload"]
        ordinal = feed.get("page_ordinal")
        if feed.get("reading_unit") != "page" or not isinstance(ordinal, int):
            raise Refusal(
                f"malformed-record: page feed {entry['artifact_id']!r} is not read by page"
            )
        if ordinal in feeds:
            raise Refusal(f"malformed-record: two page feeds for page ordinal {ordinal}")
        feeds[ordinal] = feed
    return feeds


def evaluate_page_feed_run(
    *,
    tree: RunTree,
    ledger_path: Path,
    reference_pages_path: Path,
    page_ids: Collection[str] | None = None,
) -> dict[str, Any]:
    """One self-hashed report of each page-feed witness against the reference records.

    The pages scored are the admitted reference pages the run sealed, or the
    named `page_ids` among them; an admitted page the run did not seal is
    counted in `reference_pages_outside_run`, never scored as a miss. A
    sealed page with no page feed is refused by name.
    """
    try:
        ledger_bytes = ledger_path.read_bytes()
        ledger = validate_local_admission_ledger(json.loads(ledger_bytes))
    except (OSError, UnicodeDecodeError, ValueError, CorpusRefusal) as error:
        raise Refusal(f"reference-ledger-invalid: {error}") from error
    pages = _reference_pages(reference_pages_path)
    admitted: dict[str, set[tuple[str, str]]] = {}
    for row in ledger["rows"]:
        if row["decision"] == "admitted":
            admitted.setdefault(row["page_id"], set()).add(
                (row["page_sha256"], row["reference_page_self_hash"])
            )
    for page_id, identities in admitted.items():
        if len(identities) != 1:
            raise Refusal(
                f"malformed-record: page id {page_id!r} names more than one admitted page"
            )
    requested = sorted(admitted) if page_ids is None else list(page_ids)
    if len(set(requested)) != len(requested):
        raise Refusal("malformed-record: duplicate selected page id")
    unknown = sorted(set(requested) - set(admitted))
    if unknown:
        raise Refusal(f"reference-page-not-in-ledger: selected page {unknown[0]!r} is not admitted")

    read_only = ReadOnlyRunTree(tree)
    ordinal_by_sha: dict[str, int] = {}
    for ordinal, digest in load_exemplar_page_shas(read_only).items():
        if digest in ordinal_by_sha:
            raise Refusal("malformed-record: two sealed source pages carry the same sha256")
        ordinal_by_sha[digest] = ordinal
    feeds = page_feeds(read_only)

    reports = []
    outside = []
    reference_records = 0
    for page_id in sorted(requested):
        [(digest, reference_hash)] = admitted[page_id]
        ordinal = ordinal_by_sha.get(digest)
        if ordinal is None:
            if page_ids is not None:
                raise Refusal(
                    f"reference-page-not-in-run: selected page {page_id!r} was not sealed by "
                    "the Exemplar"
                )
            outside.append(page_id)
            continue
        page = pages.get(digest)
        if page is None or page["self_hash"] != reference_hash:
            raise Refusal(
                f"reference-page-not-in-ledger: page {page_id!r} has no exact admitted "
                "reference page"
            )
        if ordinal not in feeds:
            raise Refusal(
                f"malformed-record: sealed page {page_id!r} (ordinal {ordinal}) has no page feed"
            )
        reports.append(evaluate_feed_page(reference_page=page, feed=feeds[ordinal]))
        reference_records += len(page["acts"])
    names = tuple(sorted({row["chair"] for report in reports for row in report["rows"]}))
    rows = [row for report in reports for row in report["rows"]]
    totals = _totals(rows, names)
    for name, total in totals.items():
        scored = [
            row
            for row in rows
            if row["chair"] == name
            and row["status"] in {OutputStatus.COMPLETE.value, OutputStatus.TRUNCATED.value}
        ]
        total["scoreable_cer_errors"] = sum(row["cer"] for row in scored)
        total["scoreable_cer_units"] = sum(row["cer_units"] for row in scored)
        total["reasons"] = dict(
            sorted(
                Counter(row["reason"] or "scored" for row in rows if row["chair"] == name).items()
            )
        )
        del total["missing_proposals"]
    body = {
        "schema": PAGE_SCHEMA,
        "basis": PAGE_FEED,
        "run_id": tree.run_id,
        "ledger_sha256": digest_bytes(ledger_bytes),
        "selected_page_ids": sorted(requested),
        "reference_pages_outside_run": sorted(outside),
        "witnesses": list(names),
        "reference_records": reference_records,
        "pages": reports,
        "totals": totals,
    }
    body["self_hash"] = self_hash(body)
    return body


def write_report(report: Mapping[str, Any], output: Path, *, run_root: Path) -> None:
    """Create an immutable report outside the run tree."""
    body = dict(report)
    if not verify_self_hash(body):
        raise Refusal("self-hash-mismatch: witness report does not hash to itself")
    data = canonical_bytes(body)
    output = Path(output)
    resolved_output = output.resolve()
    resolved_run = run_root.resolve()
    if resolved_output == resolved_run or resolved_output.is_relative_to(resolved_run):
        raise Refusal("output-in-run-tree: a witness report must be external to the RunTree")
    if not write_new_file(output, data):
        raise Refusal(f"output-exists: {output}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=DESCRIPTION)
    parser.add_argument("--run-root", required=True)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--ledger", required=True)
    parser.add_argument("--reference-pages", required=True)
    parser.add_argument(
        "--page-id",
        action="append",
        help="a page to score; a page-read run defaults to every admitted page it sealed",
    )
    parser.add_argument(
        "--basis",
        choices=(PAGE_FEED, "act-attachment"),
        help="what a witness is scored from; by default the page feeds when the run has them",
    )
    parser.add_argument("--output", required=True)
    args = parser.parse_args(argv)
    tree = RunTree(Path(args.run_root), args.run_id)
    basis = args.basis or (PAGE_FEED if page_feeds(ReadOnlyRunTree(tree)) else "act-attachment")
    if basis == PAGE_FEED:
        report = evaluate_page_feed_run(
            tree=tree,
            ledger_path=Path(args.ledger),
            reference_pages_path=Path(args.reference_pages),
            page_ids=args.page_id,
        )
    else:
        report = evaluate_run(
            tree=tree,
            ledger_path=Path(args.ledger),
            reference_pages_path=Path(args.reference_pages),
            page_ids=args.page_id or [],
        )
    write_report(report, Path(args.output), run_root=tree.root)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
