"""End-to-end Armarium product exports over the real sealed fixture pipeline."""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
from io import BytesIO
from pathlib import Path
from zipfile import ZipFile

import pytest
from armarium_export import verify_export_bundle, verify_projection_identity

from common.contracts.canonical import canonical_bytes, self_hash
from common.contracts.identities import artifact_id
from common.contracts.stages import ARCHETYPUS
from common.runtree.store import RunTree

ROOT = Path(__file__).resolve().parents[2]
ORCHESTRATOR = ROOT / "pipeline" / "orchestrator" / "run.py"
ARMARIUM = ROOT / "pipeline" / "7_armarium" / "run.py"


def _orchestrate(
    run_root: Path, run_id: str, *, formats_config: Path | None = None
) -> subprocess.CompletedProcess:
    command = [
        sys.executable,
        str(ORCHESTRATOR),
        "--fixture",
        "synthetic-two-page-v0",
        "--scenario",
        "happy",
        "--run-id",
        run_id,
        "--run-root",
        str(run_root),
    ]
    if formats_config is not None:
        command.extend(("--formats-config", str(formats_config)))
    return subprocess.run(
        command,
        cwd=ROOT,
        capture_output=True,
        text=True,
    )


def _run_armarium(run_root: Path, run_id: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [
            sys.executable,
            str(ARMARIUM),
            "--run-root",
            str(run_root),
            "--run-id",
            run_id,
            "--scenario",
            "happy",
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
    )


def _export(tree: RunTree) -> dict:
    return tree.read_artifact(
        "armarium",
        "export",
        artifact_id("armarium", "export", "export", None),
    )


def test_armarium_seals_a_self_verifying_product_bundle(tmp_path):
    root = tmp_path / "runs"
    result = _orchestrate(root, "bundle")
    assert result.returncode == 0, result.stderr

    tree = RunTree(root, "bundle")
    export = _export(tree)
    bundle = export["payload"]["bundle"]
    reference = bundle["reference"]
    assert export["inputs"] == [reference]
    assert bundle["sha256"] == reference["sha256"]

    clean = tmp_path / "clean"
    manifest = verify_export_bundle(tree.read_bytes(reference["relative_path"]), clean)
    assert manifest["claims"]["status"] == "partial"
    assert manifest["claims"]["submission_inventory"]["status"] == "unreconciled"
    assert verify_projection_identity(tree.read_bytes(reference["relative_path"]), tmp_path / "identity")


def test_run_bound_pixel_embedding_packages_page_and_crop_bytes(tmp_path):
    formats = tmp_path / "formats.toml"
    formats.write_text(
        'schema = "armarium-formats.v1"\n'
        'formats = ["text-bundle", "acts-database", "jsonl", "review-items", "salvage-tier"]\n'
        "embed_pixels = true\n",
        encoding="utf-8",
    )
    root = tmp_path / "runs"
    result = _orchestrate(root, "embedded", formats_config=formats)
    assert result.returncode == 0, result.stderr

    tree = RunTree(root, "embedded")
    reference = _export(tree)["payload"]["bundle"]["reference"]
    manifest = verify_export_bundle(tree.read_bytes(reference["relative_path"]), tmp_path / "clean")
    assert manifest["formats"]["embed_pixels"] is True
    assert manifest["claims"]["pixels"]["resolution_claim"].startswith("embedded pixels")


def test_product_keeps_non_text_provenance_and_every_continuation_citation(tmp_path):
    root = tmp_path / "runs"
    result = _orchestrate(root, "provenance")
    assert result.returncode == 0, result.stderr

    tree = RunTree(root, "provenance")
    export = _export(tree)
    delivered = next(entry for entry in export["payload"]["delivered"] if entry["act_key"] == "a2")
    reference = export["payload"]["bundle"]["reference"]
    bundle_bytes = tree.read_bytes(reference["relative_path"])
    with ZipFile(BytesIO(bundle_bytes)) as archive:
        rows = [json.loads(line) for line in archive.read("acts.jsonl").decode().splitlines()]
        row = next(item for item in rows if item["act_id"] == delivered["act_id"])
        text = archive.read(
            "text/_source_folder/fixtures/synthetic-two-page-v0/readings.txt"
        ).decode()

    assert len(row["witnesses"]) == len(delivered["witnesses"])
    assert row["perlectio_ref"] == {
        "availability": "requires-retained-run-access",
        "run_relative_path": delivered["perlectio_ref"]["relative_path"],
        "sha256": delivered["perlectio_ref"]["sha256"],
    }
    assert row["witnesses"][0]["testimonium_ref"]["availability"] == (
        "requires-retained-run-access"
    )
    assert "relative_path" not in row["perlectio_ref"]
    for region in delivered["source_regions"]:
        assert f"source-page: {region['declared_path']}" in text
        assert f"source-sha256: {region['declared_sha256']}" in text


def test_production_bundle_marks_absent_salvage_inventory_as_not_produced(tmp_path):
    root = tmp_path / "runs"
    result = _orchestrate(root, "no-salvage")
    assert result.returncode == 0, result.stderr

    tree = RunTree(root, "no-salvage")
    reference = _export(tree)["payload"]["bundle"]["reference"]
    manifest = verify_export_bundle(tree.read_bytes(reference["relative_path"]), tmp_path / "clean")
    assert manifest["claims"]["salvage"]["status"] == (
        "not-produced-no-sealed-salvage-inventory"
    )
    assert manifest["claims"]["salvage"]["count"] is None


@pytest.mark.parametrize(
    ("missing_field", "expected_reason"),
    [
        ("provenance", "model identity provenance"),
        ("regions", "source-region provenance"),
    ],
)
def test_provenance_less_established_reading_becomes_a_visible_refusal(
    tmp_path, missing_field, expected_reason
):
    root = tmp_path / "runs"
    assert _orchestrate(root, "refusal").returncode == 0
    tree = RunTree(root, "refusal")
    original = next(
        tree.read_artifact(ARCHETYPUS, "archetypus", entry["artifact_id"])
        for entry in tree.build_manifest(ARCHETYPUS)["artifacts"]
        if entry["kind"] == "archetypus"
    )
    refused_act_id = original["subject_id"]

    # An immutable stage artifact cannot be altered through its normal writer.
    # This synthetic reseal is the precise counterfactual Armarium must account
    # for: the transport envelope remains valid, but its established reading has
    # no exportable provenance.  Remove old Armarium output so the new terminal
    # record has no immutable-identity collision with the prior happy export.
    shutil.rmtree(tree.root / "7_armarium")
    altered = json.loads(json.dumps(original))
    altered["payload"].pop(missing_field)
    altered["payload"]["self_hash"] = self_hash(altered["payload"])
    altered["self_hash"] = self_hash(altered)
    artifact_path = tree.resolve(tree.artifact_path(ARCHETYPUS, "archetypus", altered["artifact_id"]))
    artifact_path.write_bytes(canonical_bytes(altered))

    result = _run_armarium(root, "refusal")
    assert result.returncode == 3, result.stderr
    export = _export(tree)
    assert export["payload"]["aggregate"]["status"] == "partial"
    refused = [
        entry
        for entry in export["payload"]["review"]
        if entry["act_id"] == refused_act_id
    ]
    assert len(refused) == 1
    assert refused[0]["category"] == "refused-with-reason"
    assert expected_reason in refused[0]["reason"]
    assert not [
        entry
        for entry in export["payload"]["delivered"]
        if entry["act_id"] == refused_act_id
    ]

    reference = export["payload"]["bundle"]["reference"]
    clean = tmp_path / f"clean-{missing_field}"
    verify_export_bundle(tree.read_bytes(reference["relative_path"]), clean)
    rows = [json.loads(line) for line in (clean / "acts.jsonl").read_text().splitlines()]
    row = next(item for item in rows if item["act_id"] == refused_act_id)
    assert row["category"] == "refused-with-reason"
    assert row["canonical_clean_text"] is None
    assert verify_projection_identity(tree.read_bytes(reference["relative_path"]), tmp_path / f"id-{missing_field}")
