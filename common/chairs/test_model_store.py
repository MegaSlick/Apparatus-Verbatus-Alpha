"""Synthetic-store tests for R1 acquisition; no network or real weights are used."""

import json

import pytest

from common.chairs.errors import DigestMismatchRefusal
from common.chairs.manifests import build_manifest, write_manifest
from common.chairs.model_store import (
    DAI_PROMPT_CITATION,
    REQUIRED_ARTIFACTS,
    STORE_SCHEMA,
    SURYA_OCR_2_REFUSAL,
    derived_inventory,
    read_derived_inventory,
    verify_store,
    write_derived_inventory,
)
from common.contracts.canonical import canonical_bytes


def _store(tmp_path):
    """The host-record fixture shape, reduced to harmless byte-sized snapshots."""
    artifacts = {}
    for requirement in {item.artifact: item for item in REQUIRED_ARTIFACTS}.values():
        root = (
            tmp_path
            / ("hf" if requirement.source == "huggingface" else "local")
            / requirement.artifact
        )
        root.mkdir(parents=True)
        (root / "config.json").write_text('{"fixture":true}', encoding="utf-8")
        (root / "LICENSE").write_text(f"license for {requirement.artifact}\n", encoding="utf-8")
        carried = []
        if requirement.artifact == "dai-recordgold-atr":
            for name in ("system.txt", "query.txt"):
                (root / name).write_text(f"{name} fixture\n", encoding="utf-8")
                carried.append({"name": name, "path": name, "citation": DAI_PROMPT_CITATION})
        manifest_path = tmp_path / "manifests" / f"{requirement.artifact}.json"
        pin = write_manifest(build_manifest(root), manifest_path)
        artifacts[requirement.artifact] = {
            "artifact": requirement.artifact,
            "source": requirement.source,
            "repo": requirement.repo,
            "revision": requirement.revision,
            "snapshot": root.relative_to(tmp_path).as_posix(),
            "manifest": manifest_path.relative_to(tmp_path).as_posix(),
            "digest_manifest": pin,
            "license": "LICENSE",
            "carried": carried,
        }
    record = {
        "schema": STORE_SCHEMA,
        "layout": {"hf": "hf", "local": "local", "manifests": "manifests", "staging": "staging"},
        "capacity": {
            "snapshot_bytes": 51_000_000_000,
            "promotion_headroom_bytes": 51_000_000_000,
            "available_bytes": 102_000_000_000,
            "cleanup_owner": "host model-store operator",
        },
        "artifacts": [artifacts[key] for key in sorted(artifacts)],
    }
    (tmp_path / "download_record.json").write_bytes(canonical_bytes(record))
    return record


def test_host_download_record_fixture_derives_seven_chair_inventory_and_verifies_bytes(tmp_path):
    record = _store(tmp_path)

    inventory = verify_store(tmp_path)

    assert inventory == derived_inventory(record)
    assert [row["chair"] for row in inventory["artifacts"]] == [
        item.chair for item in REQUIRED_ARTIFACTS
    ]
    assert inventory["refusals"] == [SURYA_OCR_2_REFUSAL]
    assert len({row["artifact"] for row in inventory["artifacts"]}) == 6


def test_derived_inventory_cannot_restate_divergent_store_facts(tmp_path):
    record = _store(tmp_path)
    path = tmp_path / "inventory.json"
    write_derived_inventory(record, path)
    raw = json.loads(path.read_text(encoding="utf-8"))
    raw["artifacts"][0]["revision"] = "0" * 40
    path.write_bytes(canonical_bytes(raw))

    with pytest.raises(DigestMismatchRefusal, match="diverges"):
        read_derived_inventory(tmp_path, path)


def test_store_refuses_a_license_that_is_not_byte_pinned(tmp_path):
    record = _store(tmp_path)
    entry = next(item for item in record["artifacts"] if item["artifact"] == "chandra-ocr-2")
    snapshot = tmp_path / entry["snapshot"]
    (snapshot / "LICENSE").unlink()

    with pytest.raises(DigestMismatchRefusal, match="LICENSE"):
        verify_store(tmp_path)
