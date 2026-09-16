"""Read-only per-witness RecordGold scoring over sealed act attachments.

Designator geometry is paired once, independently of witness and reference
text. Each chair is then scored from the exact sealed Testimonium slice that
the current ``act-attachment`` assigns to that matched proposal.
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
from common.contracts.stages import ATTESTATORES, EXEMPLAR
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
from .compare import (
    ReadOnlyRunTree,
    compare_page_geometry,
    load_exemplar_page_shas,
    load_pipeline_proposal_acts,
)
from .local_admission import validate_local_admission_ledger
from .reference import validate_reference_page

SCHEMA = "recordgold-witness-evaluation.v1"
CHAIRS = ("attestator_1", "attestator_2", "attestator_3")


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
        raise CorpusRefusal("malformed-record: referenced Testimonium has no retained text payload")
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
        raise CorpusRefusal(
            "malformed-record: referenced Testimonium has invalid truncation health"
        )

    text = payload["payload"]
    span = attachment.get("span")
    if span is None:
        return OutputStatus.UNAVAILABLE, None, "no-defensible-span"
    if (
        not isinstance(span, Mapping)
        or set(span) != {"start", "end"}
        or not all(isinstance(span[key], int) and not isinstance(span[key], bool) for key in span)
    ):
        raise CorpusRefusal("malformed-record: attachment span is not a closed integer range")
    if not 0 <= span["start"] <= span["end"] <= len(text):
        raise CorpusRefusal("malformed-record: attachment span exceeds its retained Testimonium")
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


def _totals(
    rows: Sequence[Mapping[str, Any]], chairs: tuple[str, ...]
) -> dict[str, dict[str, Any]]:
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
                raise CorpusRefusal(
                    "malformed-record: duplicate attachments for one matched act page"
                )
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
        raise CorpusRefusal(f"missing-input-file: {path} is not a file")
    pages: dict[str, dict[str, Any]] = {}
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except UnicodeDecodeError as error:
        raise CorpusRefusal(f"reference-page-invalid: {path} is not UTF-8: {error}") from error
    for number, line in enumerate(lines, 1):
        if not line.strip():
            continue
        try:
            page = validate_reference_page(json.loads(line))
        except (ValueError, CorpusRefusal) as error:
            raise CorpusRefusal(f"reference-page-invalid: line {number}: {error}") from None
        digest = page["page"]["sha256"]
        if digest in pages:
            raise CorpusRefusal(f"reference-page-collision: duplicate page sha256 {digest}")
        pages[digest] = page
    return pages


def _sealed_page_bindings(tree: ReadOnlyRunTree) -> dict[str, _SealedPageBinding]:
    """Verify every sealed Exemplar page and retain its immutable binding facts."""
    run = tree.read_run()
    sources: dict[int, dict[str, Any]] = {}
    for source in run.get("source_manifest", []):
        ordinal = source.get("ordinal") if isinstance(source, dict) else None
        if not isinstance(ordinal, int) or isinstance(ordinal, bool):
            raise CorpusRefusal("malformed-record: run source has no integer ordinal")
        if ordinal in sources:
            raise CorpusRefusal("malformed-record: run source ordinal is duplicated")
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
            raise CorpusRefusal("malformed-record: sealed Exemplar page has no identity/ordinal")
        source = sources.get(ordinal)
        if source is None:
            raise CorpusRefusal("malformed-record: sealed Exemplar page has no submitted source")
        try:
            pixels = verify_sealed_page_pixels(tree, run, source, record)
            size = dimensions(pixels)
        except (ContractError, ValueError) as error:
            raise CorpusRefusal(
                f"malformed-record: sealed Exemplar page is invalid: {error}"
            ) from error
        if page_id in pages:
            raise CorpusRefusal("malformed-record: sealed Exemplar page identity is duplicated")
        if ordinal in page_ids_by_ordinal:
            raise CorpusRefusal("malformed-record: sealed Exemplar page ordinal is duplicated")
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
        raise CorpusRefusal(
            "malformed-record: page Testimonium presentation names no sealed Exemplar page"
        )
    try:
        pixels = read_bytes(page.image_path)
        if digest_bytes(pixels) != page.sha256 or dimensions(pixels) != page.size:
            raise CorpusRefusal(
                "malformed-record: sealed Exemplar page pixels changed before binding"
            )
        validate_presented_page_binding(
            dict(presented),
            page_ordinal=page.ordinal,
            page_image_path=page.image_path,
            page_sha256=page.sha256,
            page_size=page.size,
            page_bytes=pixels,
        )
    except (ContractError, OSError, ValueError) as error:
        raise CorpusRefusal(
            f"malformed-record: page Testimonium presentation is not bound to its sealed "
            f"Exemplar page: {error}"
        ) from error


def _attachment_index(
    tree: ReadOnlyRunTree,
    *,
    sealed_pages: Mapping[str, _SealedPageBinding] | None = None,
) -> dict[str, dict[str, list[dict[str, Any]]]]:
    if sealed_pages is None:
        sealed_pages = _sealed_page_bindings(tree)
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
                raise CorpusRefusal("malformed-record: Testimonium has no chair")
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
                raise CorpusRefusal(
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
            raise CorpusRefusal("malformed-record: act attachment has no attachment list")
        seen_pairs: set[tuple[str, int | None]] = set()
        for attachment in rows:
            if not isinstance(attachment, dict) or not isinstance(attachment.get("chair"), str):
                raise CorpusRefusal("malformed-record: act attachment has malformed chair entry")
            if attachment["chair"] not in CHAIRS:
                raise CorpusRefusal("malformed-record: act attachment names an unknown chair")
            if not isinstance(attachment.get("page_witness"), bool):
                raise CorpusRefusal("malformed-record: attachment has no page-witness scope")
            if not isinstance(attachment.get("attached"), bool) or not isinstance(
                attachment.get("comparable"), bool
            ):
                raise CorpusRefusal(
                    "malformed-record: attachment has no boolean attached/comparable facts"
                )
            if attachment["comparable"] and not attachment["attached"]:
                raise CorpusRefusal(
                    "malformed-record: attachment is comparable without being attached"
                )
            page_ordinal = attachment.get("page_ordinal")
            if attachment["page_witness"]:
                if not isinstance(page_ordinal, int) or isinstance(page_ordinal, bool):
                    raise CorpusRefusal("malformed-record: page attachment has no page ordinal")
            elif page_ordinal is not None:
                raise CorpusRefusal("malformed-record: act attachment carries a page ordinal")
            pair = (attachment["chair"], page_ordinal)
            if pair in seen_pairs:
                raise CorpusRefusal("malformed-record: duplicate attachment chair/source-page pair")
            seen_pairs.add(pair)
            reference = attachment.get("testimonium_ref")
            if not isinstance(reference, dict):
                raise CorpusRefusal("malformed-record: attachment has no Testimonium reference")
            kind = "page-testimonium" if attachment["page_witness"] else "testimonium"
            testimony = tree.read_artifact_reference(
                reference,
                stage=ATTESTATORES,
                kind=kind,
                subject_id=None if attachment["page_witness"] else act_id,
            )
            payload = testimony.get("payload")
            if not isinstance(payload, dict) or payload.get("chair") != attachment["chair"]:
                raise CorpusRefusal("malformed-record: attachment points to another chair")
            current_act = current_acts.get((act_id, attachment["chair"]))
            if current_act is None:
                raise CorpusRefusal("malformed-record: attachment has no current act Testimonium")
            if attachment.get("content_health") != current_act.get("payload", {}).get(
                "content_health"
            ):
                raise CorpusRefusal(
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
                    raise CorpusRefusal(
                        f"malformed-record: referenced page Testimonium is invalid: {error}"
                    ) from error
                _validate_page_binding(
                    payload,
                    sealed_pages=sealed_pages,
                    read_bytes=tree.read_bytes,
                )
                if payload["page_ordinal"] != page_ordinal:
                    raise CorpusRefusal("malformed-record: attachment points to another page")
                current = current_pages.get((page_ordinal, attachment["chair"]))
            else:
                current = current_act
            if current is None or testimony.get("artifact_id") != current.get("artifact_id"):
                raise CorpusRefusal("malformed-record: attachment points to a stale Testimonium")
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
            raise CorpusRefusal("malformed-record: page Testimonium has no payload")
        chair, ordinal = payload.get("chair"), payload.get("page_ordinal")
        if not isinstance(ordinal, int) or isinstance(ordinal, bool):
            raise CorpusRefusal("malformed-record: page Testimonium has no source page ordinal")
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
            raise CorpusRefusal(
                f"malformed-record: page Testimonium is invalid: {error}"
            ) from error
        _validate_page_binding(
            payload,
            sealed_pages=sealed_pages,
            read_bytes=read_bytes,
        )
        if payload["presented"]["source_page_ordinal"] != ordinal:
            raise CorpusRefusal(
                "malformed-record: page Testimonium presentation names another page"
            )
        result[chair]["missing"] -= 1
        health = payload.get("content_health")
        if not isinstance(health, Mapping) or health.get("truncated") not in {True, False, None}:
            raise CorpusRefusal("malformed-record: page Testimonium has invalid content health")
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
        raise CorpusRefusal("missing-input-file: at least one non-empty page id is required")
    if len(set(requested)) != len(requested):
        raise CorpusRefusal("malformed-record: duplicate selected page id")
    if len(set(chairs)) != len(chairs):
        raise CorpusRefusal("malformed-record: duplicate chair")

    if tuple(chairs) != CHAIRS:
        raise CorpusRefusal("malformed-record: witness evaluation requires all three chairs")

    try:
        ledger_bytes = ledger_path.read_bytes()
        ledger = validate_local_admission_ledger(json.loads(ledger_bytes))
    except (OSError, UnicodeDecodeError, ValueError, CorpusRefusal) as error:
        raise CorpusRefusal(f"reference-ledger-invalid: {error}") from error
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
            raise CorpusRefusal(
                "missing-input-file: selected page id is absent or ambiguous in admitted reference ledger"
            )
        selected[page_id] = next(iter(identities))

    read_only = ReadOnlyRunTree(tree)
    sealed_pages = _sealed_page_bindings(read_only)
    source_shas = load_exemplar_page_shas(read_only)
    ordinal_by_sha: dict[str, int] = {}
    for ordinal, digest in source_shas.items():
        if digest in ordinal_by_sha:
            raise CorpusRefusal("malformed-record: two sealed source pages carry the same sha256")
        ordinal_by_sha[digest] = ordinal
    proposals = load_pipeline_proposal_acts(read_only)
    attached = _attachment_index(read_only, sealed_pages=sealed_pages)
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
    # while `selected_ordinals` and `page_health` kept one entry (CodeRabbit).
    # Refused here, beside the duplicate page-ID check, before any scoring.
    digests_seen: dict[str, str] = {}
    for page_id in sorted(selected):
        digest = selected[page_id][0]
        if digest in digests_seen:
            raise CorpusRefusal(
                f"reference-page-collision: selected pages {digests_seen[digest]!r} and "
                f"{page_id!r} name the same page sha256 {digest}; one page is scored once"
            )
        digests_seen[digest] = page_id

    selected_ordinals: dict[int, str] = {}
    for page_id in sorted(selected):
        digest, expected_reference_hash = selected[page_id]
        page = pages.get(digest)
        if page is None or page["self_hash"] != expected_reference_hash:
            raise CorpusRefusal(
                f"reference-page-not-in-ledger: selected page {page_id!r} has no exact admitted reference page"
            )
        ordinal = ordinal_by_sha.get(digest)
        if ordinal is None:
            raise CorpusRefusal(
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


def write_report(report: Mapping[str, Any], output: Path, *, run_root: Path) -> None:
    """Create an immutable report outside the run tree."""
    body = dict(report)
    if not verify_self_hash(body):
        raise CorpusRefusal("self-hash-mismatch: witness report does not hash to itself")
    data = canonical_bytes(body)
    output = Path(output)
    resolved_output = output.resolve()
    resolved_run = run_root.resolve()
    if resolved_output == resolved_run or resolved_output.is_relative_to(resolved_run):
        raise CorpusRefusal("output-in-run-tree: a witness report must be external to the RunTree")
    output.parent.mkdir(parents=True, exist_ok=True)
    try:
        with output.open("xb") as stream:
            stream.write(data)
    except FileExistsError:
        raise CorpusRefusal(f"output-exists: {output}") from None


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--run-root", required=True)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--ledger", required=True)
    parser.add_argument("--reference-pages", required=True)
    parser.add_argument("--page-id", action="append", required=True)
    parser.add_argument("--output", required=True)
    args = parser.parse_args(argv)
    tree = RunTree(Path(args.run_root), args.run_id)
    report = evaluate_run(
        tree=tree,
        ledger_path=Path(args.ledger),
        reference_pages_path=Path(args.reference_pages),
        page_ids=args.page_id,
    )
    write_report(report, Path(args.output), run_root=tree.root)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
