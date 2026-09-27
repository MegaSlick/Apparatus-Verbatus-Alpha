"""Private golden-canary alarm over a fetched, sealed run tree."""

from __future__ import annotations

import json
from collections import Counter
from pathlib import Path
from typing import Any

from common.contracts.canonical import self_hash
from common.contracts.stages import (
    ARMARIUM,
    ATTESTATORES,
    DESIGNATOR,
    DOOR,
    EXEMPLAR,
    PERLECTOR,
)
from common.runtree.store import RunTree
from common.stage import canary_ordinals
from operations.spike_perlector.models import OutputStatus
from operations.spike_perlector.normalization import GRAPHEMIC_V1
from operations.spike_perlector.scoring import score_response

from .compare import compare_page_geometry, load_exemplar_page_shas, load_pipeline_proposal_acts
from .reference import validate_reference_page
from .witness_evaluate import CHAIRS, _attachment_index, _sealed_page_bindings, witness_reading

MIN_ACTS_FOUND = 0.5
MIN_SHARED_CHARACTERS = 0.4
MAX_LINE_REPEATS = 5


def _repeated(text: str) -> bool:
    lines = (line.strip() for line in text.splitlines())
    return any(
        count > MAX_LINE_REPEATS
        for count in Counter(line for line in lines if len(line) >= 10).values()
    )


def _shared(reference: str, text: str | None, status: OutputStatus) -> bool:
    score = score_response(reference, status=status, text=text, profile=GRAPHEMIC_V1)
    return score.cer.edits.matches >= MIN_SHARED_CHARACTERS * score.cer.reference_units


def _references(root: Path) -> dict[str, dict[str, Any]]:
    path = root / "reference-pages.json"
    records = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(records, list) or not records:
        raise ValueError("the private canary reference has no pages")
    pages = [validate_reference_page(record) for record in records]
    by_sha = {page["page"]["sha256"]: page for page in pages}
    if len(by_sha) != len(pages):
        raise ValueError("the private canary reference repeats a page digest")
    return by_sha


def check_run(tree: RunTree, canary_root: str | Path) -> dict[str, Any]:
    """Return only pass/fail by stage and the named rules of dead canaries."""
    stages = [DOOR, EXEMPLAR, DESIGNATOR, *CHAIRS, PERLECTOR, ARMARIUM]
    dead: list[dict[str, str]] = []

    def fail(stage: str, rule: str, value: str = "failed") -> None:
        if not any(row["stage"] == stage for row in dead):
            dead.append({"stage": stage, "rule": rule, "value": value})

    run = tree.read_run()
    ordinals = canary_ordinals(run)
    if not ordinals:
        fail(DOOR, "no-sealed-canary-ledger")
    try:
        references = _references(Path(canary_root))
    except (OSError, ValueError, TypeError):
        fail(DOOR, "missing-or-invalid-private-reference")
        references = {}

    rows = [row for row in run.get("source_manifest", []) if row.get("ordinal") in ordinals]
    if len(rows) != len(ordinals):
        fail(DOOR, "canary-source-census")
    for stage in (DOOR, EXEMPLAR, DESIGNATOR, ATTESTATORES, PERLECTOR, ARMARIUM):
        try:
            manifest = tree.build_manifest(stage)
            if not any(entry["kind"] == "stage-seal" for entry in manifest["artifacts"]):
                fail(stage, "stage-not-sealed")
        except Exception:
            fail(stage, "stage-not-sealed")
    if any(row["stage"] == DOOR for row in dead):
        for stage in stages[1:]:
            fail(stage, "canary-ingress-failed")
    else:
        try:
            page_shas = load_exemplar_page_shas(tree)
        except Exception:
            fail(EXEMPLAR, "canary-page-not-sealed")
            page_shas = {}
        if set(page_shas) & ordinals != ordinals or any(
            page_shas.get(ordinal) not in references for ordinal in ordinals
        ):
            fail(EXEMPLAR, "canary-page-or-reference-missing")

        try:
            proposals = load_pipeline_proposal_acts(tree)
            bindings = _sealed_page_bindings(tree)
            attachments = _attachment_index(tree, sealed_pages=bindings)
        except Exception:
            fail(DESIGNATOR, "canary-proposal-unreadable")
            proposals, attachments = [], {}

        matched: list[tuple[str, dict[str, Any], int]] = []
        for ordinal in sorted(ordinals):
            page = references.get(page_shas.get(ordinal))
            if page is None:
                continue
            try:
                geometry = compare_page_geometry(
                    page,
                    [
                        proposal
                        for proposal in proposals
                        if proposal["page_sha256"] == page_shas[ordinal]
                    ],
                )
            except Exception:
                fail(DESIGNATOR, "canary-geometry-unreadable")
                continue
            if len(geometry["matched_pairs"]) < MIN_ACTS_FOUND * len(page["acts"]):
                fail(DESIGNATOR, "fewer-than-half-gold-acts-found")
            by_id = {act["physical_act_id"]: act for act in page["acts"]}
            matched.extend(
                (pair["pipeline_act_id"], by_id[pair["reference_physical_act_id"]], ordinal)
                for pair in geometry["matched_pairs"]
            )
        if not matched:
            fail(DESIGNATOR, "no-matched-canary-acts")
            for chair in CHAIRS:
                fail(chair, "no-canary-testimonium")

        for chair in CHAIRS:
            for act_id, reference, ordinal in matched:
                candidates = attachments.get(act_id, {}).get(chair, [])
                candidates = [
                    item
                    for item in candidates
                    if not item["attachment"]["page_witness"]
                    or item["attachment"]["page_ordinal"] == ordinal
                ]
                if len(candidates) != 1:
                    fail(
                        chair,
                        "DAI failed on a page it was trained on"
                        if chair == "attestator_2"
                        else "missing-canary-testimonium",
                    )
                    continue
                try:
                    status, reading, _ = witness_reading(
                        candidates[0]["attachment"], candidates[0]["testimonium"]
                    )
                    healthy = (
                        status == OutputStatus.COMPLETE
                        and _shared(reference["text"], reading, status)
                        and not _repeated(reading or "")
                    )
                except Exception:
                    healthy = False
                if not healthy:
                    fail(
                        chair,
                        "DAI failed on a page it was trained on"
                        if chair == "attestator_2"
                        else "canary-witness-reading-failed",
                    )

        try:
            perlectio_manifest = tree.build_manifest(PERLECTOR)
            perlectios = {
                entry["subject_id"]: tree.read_artifact(
                    PERLECTOR, "perlectio", entry["artifact_id"]
                )
                for entry in perlectio_manifest["artifacts"]
                if entry["kind"] == "perlectio"
            }
        except Exception:
            perlectios = {}
        for act_id, reference, _ in matched:
            reading = perlectios.get(act_id)
            payload = reading.get("payload", {}) if isinstance(reading, dict) else {}
            status = reading.get("outcome") if isinstance(reading, dict) else None
            text = payload.get("text")
            if status != "read" or not isinstance(text, str) or not text.strip() or _repeated(text):
                fail(PERLECTOR, "canary-reading-empty-truncated-or-repeated")
            elif not _shared(reference["text"], text, OutputStatus.COMPLETE):
                fail(PERLECTOR, "canary-reading-shared-too-little-ink")
        if not matched:
            fail(PERLECTOR, "no-canary-reading")

        try:
            export_manifest = tree.build_manifest(ARMARIUM)
            exports = [
                tree.read_artifact(ARMARIUM, "export", entry["artifact_id"])
                for entry in export_manifest["artifacts"]
                if entry["kind"] == "export"
            ]
            export = exports[-1]["payload"]
            block = export["canary"]
            canary_ids = {row["act_id"] for row in block["acts"]}
            matched_ids = {act_id for act_id, _, _ in matched}
            if (
                set(block["ordinals"]) != ordinals
                or not matched_ids <= canary_ids
                or any(not set(row["page_ordinals"]) <= ordinals for row in block["acts"])
            ):
                fail(ARMARIUM, "canary-block-missing-act-or-page")
            if any(row["ordinal"] in ordinals for row in export["pages"]) or any(
                row["act_id"] in canary_ids | matched_ids
                for row in export["delivered"] + export["non_delivered"]
            ):
                fail(ARMARIUM, "canary-reached-real-export")
        except Exception:
            fail(ARMARIUM, "canary-export-unreadable")

    body = {
        "schema": "canary-verdict.v1",
        "run_id": tree.run_id,
        "stages": {stage: not any(row["stage"] == stage for row in dead) for stage in stages},
        "dead": dead,
    }
    body["self_hash"] = self_hash(body)
    return body
