"""Read-only per-witness RecordGold scoring over sealed act attachments.

This is deliberately a small adapter over the existing reference comparator:
Designator geometry is paired once, then each chair's already-sealed attachment
supplies (or explicitly cannot supply) the text for that same pair.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any, Mapping

from common.contracts.canonical import canonical_bytes, digest_bytes, self_hash
from common.contracts.stages import ATTESTATORES
from common.runtree.store import RunTree
from operations.spike_perlector.models import OutputStatus
from operations.spike_perlector.normalization import GRAPHEMIC_V1
from operations.spike_perlector.scoring import score_response

from . import CorpusRefusal
from .compare import compare_page, load_pipeline_proposal_acts
from .local_admission import load_local_admission_ledger
from .reference import validate_reference_page

SCHEMA = "recordgold-witness-evaluation.v1"


def witness_reading(attachment: Mapping[str, Any], testimonium: Mapping[str, Any]) -> tuple[OutputStatus, str | None, str | None]:
    """Return one defensible sealed excerpt, never choosing it with reference text."""
    if not attachment.get("attached"):
        return OutputStatus.UNAVAILABLE, None, "unattached"
    if not attachment.get("comparable"):
        return OutputStatus.UNAVAILABLE, None, "not-comparable"
    payload = testimonium.get("payload")
    if not isinstance(payload, Mapping) or not isinstance(payload.get("payload"), str):
        raise CorpusRefusal("malformed-record: referenced Testimonium has no retained text payload")
    health = attachment.get("content_health")
    if not isinstance(health, Mapping) or health.get("recordable") is not True:
        return OutputStatus.MALFORMED, None, "unrecordable-response"
    if testimonium.get("outcome") not in {"read", "genuinely-empty"}:
        return OutputStatus.MALFORMED, None, f"non-reading-{testimonium.get('outcome')!r}"
    text = payload["payload"]
    span = attachment.get("span")
    if span is None:
        return OutputStatus.UNAVAILABLE, None, "no-defensible-span"
    if not isinstance(span, Mapping) or set(span) != {"start", "end"} or not all(isinstance(span[k], int) for k in span):
        raise CorpusRefusal("malformed-record: attachment span is not a closed integer range")
    if not 0 <= span["start"] <= span["end"] <= len(text):
        raise CorpusRefusal("malformed-record: attachment span exceeds its retained Testimonium")
    return (OutputStatus.TRUNCATED if health.get("truncated") is True else OutputStatus.COMPLETE, text[span["start"]:span["end"]], None)


def evaluate_page(*, reference_page: dict[str, Any], proposals: list[dict[str, Any]], attachments: Mapping[str, Mapping[str, list[dict[str, Any]]]], testimonia: Mapping[str, Mapping[str, Any]], chairs: tuple[str, ...]) -> dict[str, Any]:
    """Score one page. ``attachments`` is act-id -> chair -> sealed attachment/ref pair."""
    reference_page = validate_reference_page(reference_page)
    # Geometry is intentionally independent of chair and reference text.
    empty = {p["act_id"]: (OutputStatus.MISSING, None) for p in proposals}
    geometry = compare_page(reference_page, proposals, empty)
    rows: list[dict[str, Any]] = []
    for pair in geometry["matched_pairs"]:
        act_id = pair["pipeline_act_id"]
        ref = next(a for a in reference_page["acts"] if a["physical_act_id"] == pair["reference_physical_act_id"])
        for chair in chairs:
            candidates = attachments.get(act_id, {}).get(chair, [])
            matches = [item for item in candidates if not item["attachment"].get("page_witness") or item["testimonium"].get("payload", {}).get("presented", {}).get("image_sha256") == reference_page["page"]["sha256"]]
            if len(matches) > 1:
                raise CorpusRefusal("malformed-record: duplicate attachments for one matched act page")
            item = matches[0] if matches else None
            if item is None:
                status, text, reason = OutputStatus.MISSING, None, "missing-act-attachment"
            else:
                attachment, reference = item.get("attachment"), item.get("testimonium")
                if not isinstance(attachment, Mapping) or not isinstance(reference, Mapping):
                    raise CorpusRefusal("malformed-record: attachment index has no sealed attachment/Testimonium pair")
                status, text, reason = witness_reading(attachment, reference)
            score = score_response(ref["text"], status=status, text=text, profile=GRAPHEMIC_V1)
            rows.append({"record_id": ref["record_id"], "pipeline_act_id": act_id, "chair": chair, "status": status.value, "reason": reason, "cer": score.cer.edits.errors, "cer_units": score.cer.reference_units, "wer": score.wer.edits.errors, "wer_units": score.wer.reference_units})
    totals = {chair: {"references": len([r for r in rows if r["chair"] == chair]) + len(geometry["misses"]), "scoreable": sum(r["chair"] == chair and r["status"] in {"complete", "truncated"} for r in rows), "cer_errors": sum(r["cer"] for r in rows if r["chair"] == chair), "cer_units": sum(r["cer_units"] for r in rows if r["chair"] == chair), "wer_errors": sum(r["wer"] for r in rows if r["chair"] == chair), "wer_units": sum(r["wer_units"] for r in rows if r["chair"] == chair), "missing_proposals": len(geometry["misses"])} for chair in chairs}
    body = {"schema": SCHEMA, "reference_page_self_hash": reference_page["self_hash"], "geometry": geometry, "rows": rows, "totals": totals, "missing_reference_records": geometry["misses"]}
    body["self_hash"] = self_hash(body)
    return body


def _reference_pages(path: Path) -> dict[str, dict[str, Any]]:
    pages = {}
    for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        try:
            page = validate_reference_page(json.loads(line))
        except (ValueError, CorpusRefusal) as error:
            raise CorpusRefusal(f"reference-page-invalid: line {number}: {error}") from None
        digest = page["page"]["sha256"]
        if digest in pages:
            raise CorpusRefusal(f"reference-page-collision: duplicate page sha256 {digest}")
        pages[digest] = page
    return pages


def _attachment_index(tree: RunTree) -> dict[str, dict[str, list[dict[str, Any]]]]:
    indexed: dict[str, dict[str, list[dict[str, Any]]]] = {}
    for entry in tree.build_manifest(ATTESTATORES)["artifacts"]:
        if entry["kind"] != "act-attachment":
            continue
        record = tree.read_artifact(ATTESTATORES, "act-attachment", entry["artifact_id"])
        rows = record.get("payload", {}).get("attachments")
        if not isinstance(rows, list):
            raise CorpusRefusal("malformed-record: act attachment has no attachment list")
        for attachment in rows:
            if not isinstance(attachment, dict) or not isinstance(attachment.get("chair"), str):
                raise CorpusRefusal("malformed-record: act attachment has malformed chair entry")
            reference = attachment.get("testimonium_ref")
            if not isinstance(reference, dict) or not isinstance(reference.get("artifact_id"), str):
                raise CorpusRefusal("malformed-record: attachment has no Testimonium reference")
            kind = "page-testimonium" if attachment.get("page_witness") else "testimonium"
            testimony = tree.read_artifact(ATTESTATORES, kind, reference["artifact_id"])
            indexed.setdefault(record["subject_id"], {}).setdefault(attachment["chair"], []).append({
                "attachment": attachment,
                "testimonium": testimony,
            })
    return indexed


def page_health_counts(records: list[Mapping[str, Any]], *, page_sha256s: set[str], chairs: tuple[str, ...]) -> dict[str, dict[str, int]]:
    """Tally valid sealed page responses without treating unknown as complete."""
    result = {chair: {"missing": len(page_sha256s), "truncated_true": 0, "truncated_false": 0, "truncated_null": 0, "failed_model_response": 0} for chair in chairs}
    seen: set[tuple[str, str]] = set()
    for record in records:
        payload = record.get("payload")
        if not isinstance(payload, Mapping):
            raise CorpusRefusal("malformed-record: page Testimonium has no payload")
        chair = payload.get("chair")
        presented = payload.get("presented")
        if chair not in result or not isinstance(presented, Mapping):
            continue
        digest = presented.get("image_sha256")
        if digest not in page_sha256s:
            continue
        key = (chair, digest)
        if key in seen:
            raise CorpusRefusal("malformed-record: duplicate page Testimonium for one chair/page")
        seen.add(key)
        result[chair]["missing"] -= 1
        health = payload.get("content_health")
        if not isinstance(health, Mapping) or health.get("truncated") not in {True, False, None}:
            raise CorpusRefusal("malformed-record: page Testimonium has invalid content health")
        truncation = health["truncated"]
        result[chair]["truncated_true" if truncation is True else "truncated_false" if truncation is False else "truncated_null"] += 1
        if record.get("outcome") == "failed":
            result[chair]["failed_model_response"] += 1
    return result


def evaluate_run(*, tree: RunTree, ledger_path: Path, reference_pages_path: Path, page_ids: set[str], chairs: tuple[str, ...]) -> dict[str, Any]:
    """Build one read-only, self-hashed report for explicit admitted page ids."""
    ledger = load_local_admission_ledger(ledger_path)
    pages = _reference_pages(reference_pages_path)
    selected_hashes = {row["page_sha256"] for row in ledger["rows"] if row["decision"] == "admitted" and row["page_id"] in page_ids}
    if len(selected_hashes) != len(page_ids):
        raise CorpusRefusal("missing-input-file: selected page id is absent from admitted reference ledger")
    proposals = load_pipeline_proposal_acts(tree)
    attached = _attachment_index(tree)
    page_records = [
        tree.read_artifact(ATTESTATORES, "page-testimonium", entry["artifact_id"])
        for entry in tree.build_manifest(ATTESTATORES)["artifacts"]
        if entry["kind"] == "page-testimonium"
    ]
    reports = []
    for digest in sorted(selected_hashes):
        page = pages.get(digest)
        if page is None:
            raise CorpusRefusal(f"reference-page-not-in-ledger: admitted page {digest} has no reference page")
        reports.append(evaluate_page(reference_page=page, proposals=[p for p in proposals if p["page_sha256"] == digest], attachments=attached, testimonia={}, chairs=chairs))
    body = {"schema": SCHEMA, "run_id": tree.run_id, "ledger_sha256": digest_bytes(ledger_path.read_bytes()), "selected_page_ids": sorted(page_ids), "chairs": list(chairs), "page_health": page_health_counts(page_records, page_sha256s=selected_hashes, chairs=chairs), "pages": reports}
    body["self_hash"] = self_hash(body)
    return body


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--run-root", required=True)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--ledger", required=True)
    parser.add_argument("--reference-pages", required=True)
    parser.add_argument("--page-id", action="append", required=True)
    parser.add_argument("--output", required=True)
    args = parser.parse_args(argv)
    output = Path(args.output)
    if output.exists():
        raise CorpusRefusal(f"output-exists: {output}")
    report = evaluate_run(tree=RunTree(args.run_root, args.run_id), ledger_path=Path(args.ledger), reference_pages_path=Path(args.reference_pages), page_ids=set(args.page_id), chairs=("attestator_1", "attestator_2", "attestator_3"))
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_bytes(canonical_bytes(report))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
