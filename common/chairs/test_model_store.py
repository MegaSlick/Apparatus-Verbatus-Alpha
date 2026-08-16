"""Synthetic-store tests for R1 acquisition; no network or real weights are used."""

import copy
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
    load_download_record,
    promote_verified_snapshot,
    read_derived_inventory,
    verify_store,
    write_derived_inventory,
    write_download_record,
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


# --- S1: publish-once custody (GOVERNANCE 4 — evidence is never overwritten) -----


def test_write_derived_inventory_reuses_identical_bytes_silently(tmp_path):
    record = _store(tmp_path)
    path = tmp_path / "inventory.json"

    first = write_derived_inventory(record, path)
    second = write_derived_inventory(record, path)

    assert first == second
    assert json.loads(path.read_bytes()) == derived_inventory(record)


def test_write_derived_inventory_refuses_a_differing_republish_and_leaves_the_file(tmp_path):
    record = _store(tmp_path)
    path = tmp_path / "inventory.json"
    write_derived_inventory(record, path)
    original_bytes = path.read_bytes()

    other = copy.deepcopy(record)
    other["capacity"]["available_bytes"] += 1

    with pytest.raises(DigestMismatchRefusal, match="already exists with different bytes"):
        write_derived_inventory(other, path)
    assert path.read_bytes() == original_bytes


def _promotion_artifact(tmp_path, entry):
    """A promotion artifact naming its own staging and manifest paths.

    Deliberately distinct from ``entry``'s already-published manifest (written by
    ``_store``'s fixture setup from a different snapshot directory), so these
    tests exercise a fresh promotion rather than colliding with the fixture.
    """
    staging = tmp_path / "staging" / "churro-3B-promoted"
    staging.mkdir(parents=True)
    (staging / "config.json").write_text('{"fixture":true}', encoding="utf-8")
    return {
        **entry,
        "staging": staging.relative_to(tmp_path).as_posix(),
        "manifest": "manifests/churro-3B-promoted.json",
    }, staging


def test_promote_verified_snapshot_reuses_identical_bytes_silently(tmp_path):
    record = _store(tmp_path)
    entry = next(item for item in record["artifacts"] if item["artifact"] == "churro-3B")
    artifact, _ = _promotion_artifact(tmp_path, entry)

    first = promote_verified_snapshot(tmp_path, artifact)
    second = promote_verified_snapshot(tmp_path, artifact)

    assert first == second


def test_promote_verified_snapshot_refuses_a_differing_republish_and_leaves_the_manifest(
    tmp_path,
):
    record = _store(tmp_path)
    entry = next(item for item in record["artifacts"] if item["artifact"] == "churro-3B")
    artifact, staging = _promotion_artifact(tmp_path, entry)
    promote_verified_snapshot(tmp_path, artifact)
    manifest_path = tmp_path / artifact["manifest"]
    original_bytes = manifest_path.read_bytes()

    (staging / "config.json").write_text('{"fixture":false}', encoding="utf-8")

    with pytest.raises(DigestMismatchRefusal, match="already exists with different bytes"):
        promote_verified_snapshot(tmp_path, artifact)
    assert manifest_path.read_bytes() == original_bytes


# --- S5: a canonical writer closes the "hand-authored JSON" gap -----------------


def test_write_download_record_round_trips_through_load_download_record(tmp_path):
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
            "snapshot_bytes": 1,
            "promotion_headroom_bytes": 1,
            "available_bytes": 2,
            "cleanup_owner": "host model-store operator",
        },
        "artifacts": [artifacts[key] for key in sorted(artifacts)],
    }

    write_download_record(record, tmp_path)

    assert load_download_record(tmp_path) == record
    raw_bytes = (tmp_path / "download_record.json").read_bytes()
    assert raw_bytes == canonical_bytes(record)


def test_load_download_record_refuses_hand_formatted_bytes_by_name(tmp_path):
    record = _store(tmp_path)
    (tmp_path / "download_record.json").write_bytes(json.dumps(record, indent=2).encode("utf-8"))

    with pytest.raises(DigestMismatchRefusal, match="not canonical bytes"):
        load_download_record(tmp_path)


# --- S3: symlink escape is refused in both directions ---------------------------


def test_promote_verified_snapshot_refuses_a_staging_symlink_that_escapes_the_store(tmp_path):
    record = _store(tmp_path)
    entry = next(item for item in record["artifacts"] if item["artifact"] == "churro-3B")
    outside = tmp_path.parent / f"{tmp_path.name}-outside"
    outside.mkdir(exist_ok=True)
    (outside / "config.json").write_text("{}", encoding="utf-8")
    escape = tmp_path / "staging-escape"
    escape.symlink_to(outside)
    artifact = {**entry, "staging": "staging-escape"}

    with pytest.raises(DigestMismatchRefusal, match="escapes configured root"):
        promote_verified_snapshot(tmp_path, artifact)


def test_promote_verified_snapshot_accepts_a_legitimate_nested_staging_path(tmp_path):
    record = _store(tmp_path)
    entry = next(item for item in record["artifacts"] if item["artifact"] == "churro-3B")
    staging = tmp_path / "staging" / "nested" / "churro-3B"
    staging.mkdir(parents=True)
    (staging / "config.json").write_text('{"fixture":true}', encoding="utf-8")
    artifact = {
        **entry,
        "staging": staging.relative_to(tmp_path).as_posix(),
        "manifest": "manifests/churro-3B-nested.json",
    }

    digest = promote_verified_snapshot(tmp_path, artifact)

    assert len(digest) == 64


# --- Battery: forged manifests, path traversal, roster mismatches ---------------


def test_verify_store_refuses_a_manifest_tampered_after_it_was_written(tmp_path):
    record = _store(tmp_path)
    entry = next(item for item in record["artifacts"] if item["artifact"] == "chandra-ocr-2")
    manifest_path = tmp_path / entry["manifest"]
    raw = json.loads(manifest_path.read_bytes())
    raw[0]["sha256"] = "0" * 64
    manifest_path.write_bytes(canonical_bytes(raw))

    with pytest.raises(DigestMismatchRefusal, match="manifest differs"):
        verify_store(tmp_path)


@pytest.mark.parametrize("field", ["snapshot", "manifest", "license"])
def test_validate_record_refuses_path_traversal_in_artifact_fields(tmp_path, field):
    record = _store(tmp_path)
    # "hf/" is a literal string prefix, not a parsed path segment, so this also
    # satisfies the huggingface "snapshot must start with hf/" shape check and
    # exercises _safe's traversal refusal rather than that earlier one.
    record["artifacts"][0][field] = "hf/../outside"

    with pytest.raises(DigestMismatchRefusal, match="safe relative POSIX path"):
        derived_inventory(record)


def test_validate_record_refuses_path_traversal_in_a_carried_path(tmp_path):
    record = _store(tmp_path)
    entry = next(item for item in record["artifacts"] if item["artifact"] == "dai-recordgold-atr")
    entry["carried"] = [
        {"name": "system.txt", "path": "../../etc/passwd", "citation": DAI_PROMPT_CITATION},
        {"name": "query.txt", "path": "query.txt", "citation": DAI_PROMPT_CITATION},
    ]

    with pytest.raises(DigestMismatchRefusal, match="safe relative POSIX path"):
        derived_inventory(record)


def test_validate_record_refuses_a_duplicate_artifact_name(tmp_path):
    record = _store(tmp_path)
    record["artifacts"][1]["artifact"] = record["artifacts"][0]["artifact"]

    with pytest.raises(DigestMismatchRefusal, match="unique"):
        derived_inventory(record)


def test_validate_record_refuses_a_five_artifact_record(tmp_path):
    record = _store(tmp_path)
    record["artifacts"] = record["artifacts"][:5]

    with pytest.raises(DigestMismatchRefusal, match="exactly six"):
        derived_inventory(record)


def test_derived_inventory_refuses_a_revision_that_disagrees_with_the_roster(tmp_path):
    record = _store(tmp_path)
    entry = next(item for item in record["artifacts"] if item["artifact"] == "chandra-ocr-2")
    entry["revision"] = "1" * 40

    with pytest.raises(DigestMismatchRefusal, match="diverges from roster policy"):
        derived_inventory(record)


def test_derived_inventory_refuses_a_unicode_artifact_name_as_a_required_artifact(tmp_path):
    record = _store(tmp_path)
    record["artifacts"][0]["artifact"] = "chandra-ocr-2’"

    with pytest.raises(DigestMismatchRefusal, match="is absent"):
        derived_inventory(record)
