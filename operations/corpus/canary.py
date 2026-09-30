"""Private golden-canary alarm over a fetched, sealed run tree."""

from __future__ import annotations

import argparse
import io
import json
import sqlite3
from collections import Counter
from pathlib import Path
from typing import Any
from zipfile import ZipFile

from PIL import Image

from common.contracts.canonical import canonical_bytes, digest_bytes, self_hash, verify_self_hash
from common.contracts.errors import FatalAccounting
from common.contracts.identities import PROPOSAL_SEAL_ID
from common.contracts.stages import (
    ARMARIUM,
    ATTESTATORES,
    DESIGNATOR,
    DOOR,
    EXEMPLAR,
    PERLECTOR,
)
from common.exemplar_boundary import verify_exemplar_crop_lineage
from common.runtree.store import RunTree
from common.stage import canary_ordinals, latest_attempt
from operations.spike_perlector.models import OutputStatus
from operations.spike_perlector.normalization import GRAPHEMIC_V1
from operations.spike_perlector.scoring import score_response
from operations.submit.submit import build_manifest, walk_folder

from .compare import compare_page_geometry, load_exemplar_page_shas, load_pipeline_proposal_acts
from .local_admission import admit_local_set
from .reference import validate_reference_page
from .witness_evaluate import CHAIRS, attachment_index, sealed_page_bindings, witness_reading

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


def _sealed_act_pages(tree: RunTree, run: dict[str, Any]) -> dict[str, set[int]]:
    """The proposal seal counts even acts for which no crop was made."""
    seal = tree.read_artifact(DESIGNATOR, "proposal-seal", PROPOSAL_SEAL_ID)["payload"]
    acts = seal["expected_acts"]
    if not verify_self_hash(seal) or seal["count"] != len(acts):
        raise ValueError("the Designator act census is not sealed or reconciled")
    pages = {act["act_id"]: {act["page_ordinal"]} for act in acts}
    if len(pages) != len(acts):
        raise ValueError("the Designator act census repeats an identity")
    for entry in tree.build_manifest(DESIGNATOR)["artifacts"]:
        if entry["kind"] != "region":
            continue
        region = tree.read_artifact(DESIGNATOR, "region", entry["artifact_id"])
        verified = verify_exemplar_crop_lineage(tree, run, region)
        pages[region["subject_id"]].add(verified["source_page_ordinal"])
    return pages


def _contains_canary_identity(value: Any, act_ids: set[str], ordinals: set[int]) -> bool:
    """Inspect identity fields in text-free metadata, never literal fields."""
    if isinstance(value, list):
        return any(_contains_canary_identity(item, act_ids, ordinals) for item in value)
    if not isinstance(value, dict):
        return False
    for key, item in value.items():
        if (key == "act_id" or key.endswith("_act_id")) and item in act_ids:
            return True
        if (key == "ordinal" or ("page" in key and key.endswith("_ordinal"))) and item in ordinals:
            return True
        if (key == "ordinals" or ("page" in key and key.endswith("_ordinals"))) and any(
            ordinal in ordinals for ordinal in item
        ):
            return True
        if key == "act_pages" and any(
            ordinal in ordinals for pages in item.values() for ordinal in pages
        ):
            return True
        if key not in {"canonical_clean_text", "derived_search_text", "text"} and (
            isinstance(item, (dict, list)) and _contains_canary_identity(item, act_ids, ordinals)
        ):
            return True
    return False


def _canary_in_bundle(data: bytes, act_ids: set[str], ordinals: set[int]) -> bool:
    """Read only identifiers from the ZIP's real projections, without extraction."""
    with ZipFile(io.BytesIO(data)) as archive:
        names = set(archive.namelist())
        for name in ("EXPORT_MANIFEST.json", "sources.json"):
            if _contains_canary_identity(json.loads(archive.read(name)), act_ids, ordinals):
                return True
        for name in {
            "acts.jsonl",
            "review-items.jsonl",
            "coniector.jsonl",
        } & names:
            with archive.open(name) as member:
                if any(
                    _contains_canary_identity(json.loads(line), act_ids, ordinals)
                    for line in member
                ):
                    return True
        for name in names:
            if name.endswith("/readings.txt"):
                with archive.open(name) as member:
                    if any(
                        line.removeprefix(b"act-id: ").strip().decode("utf-8") in act_ids
                        for line in member
                        if line.startswith(b"act-id: ")
                    ):
                        return True
        if "acts.sqlite" in names:
            connection = sqlite3.connect(":memory:")
            try:
                connection.deserialize(archive.read("acts.sqlite"))
                for table in ("acts", "act_search"):
                    if any(
                        row[0] in act_ids
                        for row in connection.execute(f"SELECT act_id FROM {table}")
                    ):
                        return True
                if any(
                    _contains_canary_identity(json.loads(row[0]), act_ids, ordinals)
                    for row in connection.execute(
                        "SELECT source_regions_json FROM acts WHERE source_regions_json IS NOT NULL"
                    )
                ):
                    return True
            finally:
                connection.close()
    return False


def _check_run(tree: RunTree, canary_root: str | Path) -> dict[str, Any]:
    """Return only pass/fail by stage and the named rules of dead canaries."""
    stages = [DOOR, EXEMPLAR, DESIGNATOR, *CHAIRS, PERLECTOR, ARMARIUM]
    dead: list[dict[str, str]] = []

    def fail(stage: str, rule: str) -> None:
        for chair in CHAIRS if stage == ATTESTATORES else (stage,):
            if not any(row["stage"] == chair and row["rule"] == rule for row in dead):
                dead.append({"stage": chair, "rule": rule})

    run = tree.read_run()
    ordinals = canary_ordinals(run)
    if not ordinals:
        fail(DOOR, "no-sealed-canary-ledger")
    references = _references(Path(canary_root)) if ordinals else {}

    for stage in (DOOR, EXEMPLAR, DESIGNATOR, ATTESTATORES, PERLECTOR, ARMARIUM):
        try:
            manifest = tree.build_manifest(stage)
            if not any(entry["kind"] == "stage-seal" for entry in manifest["artifacts"]):
                fail(stage, "stage-not-sealed")
        except Exception as error:
            fail(stage, f"check-raised:{type(error).__name__}")
    if any(row["stage"] == DOOR for row in dead):
        for stage in stages[1:]:
            fail(stage, "canary-ingress-failed")
    else:
        try:
            page_shas = load_exemplar_page_shas(tree)
        except Exception as error:
            fail(EXEMPLAR, f"check-raised:{type(error).__name__}")
            page_shas = {}
        if set(page_shas) & ordinals != ordinals or any(
            page_shas.get(ordinal) not in references for ordinal in ordinals
        ):
            fail(EXEMPLAR, "canary-page-or-reference-missing")

        try:
            proposals = load_pipeline_proposal_acts(tree)
            bindings = sealed_page_bindings(tree)
            attachments = attachment_index(tree, sealed_pages=bindings)
        except Exception as error:
            fail(DESIGNATOR, f"check-raised:{type(error).__name__}")
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
            except Exception as error:
                fail(DESIGNATOR, f"check-raised:{type(error).__name__}")
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
                except Exception as error:
                    fail(chair, f"check-raised:{type(error).__name__}")
                    continue
                if not healthy:
                    fail(
                        chair,
                        "DAI failed on a page it was trained on"
                        if chair == "attestator_2"
                        else "canary-witness-reading-failed",
                    )

        try:
            perlectio_manifest = tree.build_manifest(PERLECTOR)
            perlectios: dict[str, list[dict]] = {}
            for entry in perlectio_manifest["artifacts"]:
                if entry["kind"] == "perlectio":
                    perlectios.setdefault(entry["subject_id"], []).append(
                        tree.read_artifact(PERLECTOR, "perlectio", entry["artifact_id"])
                    )
        except Exception as error:
            fail(PERLECTOR, f"check-raised:{type(error).__name__}")
            perlectios = {}
        for act_id, reference, _ in matched:
            try:
                reading = latest_attempt(
                    perlectios.get(act_id, []), f"canary reading of {act_id}", operation="perlegere"
                )
            except FatalAccounting:
                fail(PERLECTOR, "canary-reading-ambiguous")
                continue
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
            if len(exports) != 1:
                fail(ARMARIUM, "canary-export-ambiguous")
                export = None
            else:
                export = exports[0]["payload"]
            if export is None:
                raise ValueError("ambiguous canary export")
            act_pages = _sealed_act_pages(tree, run)
            sealed_canaries = {act_id for act_id, pages in act_pages.items() if pages & ordinals}
            if any(not act_pages[act_id] <= ordinals for act_id in sealed_canaries):
                fail(ARMARIUM, "canary-in-real-export")
            block = export.get("canary")
            if not isinstance(block, dict) or not isinstance(block.get("acts"), list):
                fail(ARMARIUM, "canary-missing-from-block")
            else:
                block_rows = {row["act_id"]: row for row in block["acts"]}
                if (
                    set(block["ordinals"]) != ordinals
                    or set(block_rows) != sealed_canaries
                    or len(block_rows) != len(block["acts"])
                    or any(
                        set(row["page_ordinals"]) != act_pages[act_id]
                        for act_id, row in block_rows.items()
                    )
                ):
                    fail(ARMARIUM, "canary-missing-from-block")
            if any(row["ordinal"] in ordinals for row in export["pages"]) or any(
                row["act_id"] in sealed_canaries
                for row in export["delivered"] + export["non_delivered"]
            ):
                fail(ARMARIUM, "canary-in-real-export")
            bundle = export["bundle"]
            data = tree.read_bytes(bundle["reference"]["relative_path"])
            if digest_bytes(data) != bundle["sha256"] or _canary_in_bundle(
                data, sealed_canaries, ordinals
            ):
                fail(ARMARIUM, "canary-in-bundle")
        except Exception as error:
            fail(ARMARIUM, f"check-raised:{type(error).__name__}")

    body = {
        "schema": "canary-verdict.v1",
        "run_id": tree.run_id,
        "stages": {stage: not any(row["stage"] == stage for row in dead) for stage in stages},
        "dead": dead,
    }
    body["self_hash"] = self_hash(body)
    return body


def check_run(tree: RunTree, canary_root: str | Path) -> dict[str, Any]:
    """A broken instrument is itself a dead canary, with a sealable verdict."""
    try:
        return _check_run(tree, canary_root)
    except Exception as error:
        return raised_verdict(tree.run_id, error)


def raised_verdict(run_id: str, error: Exception) -> dict[str, Any]:
    rule = f"check-raised:{type(error).__name__}"
    stages = [DOOR, EXEMPLAR, DESIGNATOR, *CHAIRS, PERLECTOR, ARMARIUM]
    body = {
        "schema": "canary-verdict.v1",
        "run_id": run_id,
        "stages": dict.fromkeys(stages, False),
        "dead": [{"stage": stage, "rule": rule} for stage in stages],
    }
    body["self_hash"] = self_hash(body)
    return body


def build(
    source_dir: str | Path,
    page_shas: list[str],
    output_root: str | Path,
    *,
    split: str = "train",
) -> Path:
    """Copy selected, checked reference pages into the private canary folder."""
    repo = Path(__file__).resolve().parents[2]
    source = Path(source_dir).resolve()
    output = Path(output_root).resolve()
    private = repo / "private"
    if source.is_relative_to(repo):
        raise ValueError("canary source must be outside the git tree")
    if output.is_relative_to(repo) and not output.is_relative_to(private):
        raise ValueError("canary output inside the git tree must be under private/")
    if not page_shas or len(set(page_shas)) != len(page_shas):
        raise ValueError("select distinct page digests")
    images_by_sha: dict[str, Path] = {}
    if (source / "reference-pages.json").exists():
        references = _references(source)
    else:
        ledger = admit_local_set(source, split=split)
        references = {page["page"]["sha256"]: page for page in ledger["reference_pages"]}
        for row in ledger["rows"]:
            if row["decision"] == "admitted":
                sha = row["page_sha256"]
                image = source / row["page_image"]
                if image.is_symlink():
                    raise ValueError("selected page image is a symlink")
                if not image.resolve().is_relative_to(source):
                    raise ValueError("admitted page image escapes the source directory")
                if sha in images_by_sha and images_by_sha[sha] != image:
                    raise ValueError("admitted page digest names more than one image")
                images_by_sha[sha] = image
    selected = []
    copies = []
    for sha in page_shas:
        if sha not in references:
            raise ValueError("selected page has no checked reference")
        if images_by_sha:
            image = images_by_sha.get(sha)
            if image is None:
                raise ValueError("selected page has no admitted image")
        else:
            images = [path for path in (source / "pages").glob(f"{sha}.*") if path.is_file()]
            if len(images) != 1:
                raise ValueError("selected page has no unique regular image")
            image = images[0]
        if image.is_symlink():
            raise ValueError("selected page image is a symlink")
        data = image.read_bytes()
        if digest_bytes(data) != sha:
            raise ValueError("selected page image disagrees with its reference digest")
        with Image.open(io.BytesIO(data)) as opened:
            if opened.size != (references[sha]["page"]["width"], references[sha]["page"]["height"]):
                raise ValueError("selected page image disagrees with its reference geometry")
        selected.append(references[sha])
        copies.append((sha, image.suffix, data))
    if output.exists() and any(output.iterdir()):
        raise ValueError("canary output folder is not empty")
    pages = output / "pages"
    pages.mkdir(parents=True, exist_ok=True)
    for sha, suffix, data in copies:
        (pages / f"{sha}{suffix}").write_bytes(data)
    (output / "reference-pages.json").write_bytes(canonical_bytes(selected))
    (output / "submission-manifest.json").write_bytes(
        canonical_bytes(build_manifest(walk_folder(pages)))
    )
    return output


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Build a private golden-canary submission")
    commands = parser.add_subparsers(dest="command", required=True)
    builder = commands.add_parser("build")
    builder.add_argument("--source-dir", required=True, type=Path)
    builder.add_argument("--page-sha", required=True, action="append")
    builder.add_argument("--split", choices=("train", "val"), default="train")
    builder.add_argument(
        "--output-root",
        type=Path,
        default=Path(__file__).resolve().parents[2] / "private" / "canary",
    )
    args = parser.parse_args(argv)
    if args.command == "build":
        build(args.source_dir, args.page_sha, args.output_root, split=args.split)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
