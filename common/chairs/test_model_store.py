"""Synthetic-store tests for R1 acquisition; no network or real weights are used."""

import copy
import hashlib
import json
import os
import shutil
from pathlib import Path

import pytest

from common.chairs.config import load_models_toml
from common.chairs.errors import DigestMismatchRefusal
from common.chairs.manifests import build_manifest, write_manifest
from common.chairs.model_store import (
    CHAIRS_WITHOUT_ROSTER_ROLE,
    DAI_PROMPT_CITATION,
    REQUIRED_ARTIFACTS,
    STORE_SCHEMA,
    SURYA_OCR_2_REFUSAL,
    derived_inventory,
    load_download_record,
    pod_materialization_plan,
    promote_verified_snapshot,
    read_derived_inventory,
    require_complete_store,
    require_store_artifact,
    verify_store,
    write_derived_inventory,
    write_download_record,
)
from common.chairs.registry import CACHE_DESCRIPTOR
from common.contracts.canonical import canonical_bytes

ROOT = Path(__file__).resolve().parents[2]


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
        (root / "model.safetensors").write_bytes(f"weights for {requirement.artifact}\n".encode())
        carried = []
        if requirement.artifact == "dai-recordgold-atr":
            for name in ("system.txt", "query.txt"):
                (root / name).write_text(f"{name} fixture\n", encoding="utf-8")
                carried.append({"name": name, "path": name, "citation": DAI_PROMPT_CITATION})
        manifest_path = tmp_path / "manifests" / f"{requirement.artifact}.json"
        pin = write_manifest(build_manifest(root), manifest_path)
        artifacts[requirement.artifact] = {
            "artifact": requirement.artifact,
            "state": "present",
            "source": requirement.source,
            "repo": requirement.repo,
            "revision": requirement.revision,
            "snapshot": root.relative_to(tmp_path).as_posix(),
            "manifest": manifest_path.relative_to(tmp_path).as_posix(),
            "digest_manifest": pin,
            "license": "LICENSE",
            "carried": carried,
            "required_files": sorted(
                ["LICENSE", "model.safetensors", *[x["path"] for x in carried]]
            ),
        }
    record = {
        "schema": STORE_SCHEMA,
        "layout": {
            "hf": "hf",
            "local": "local",
            "manifests": "manifests",
            "records": "records",
            "staging": "staging",
        },
        "capacity": {
            "snapshot_bytes": 51_000_000_000,
            "promotion_headroom_bytes": 51_000_000_000,
            "available_bytes": 102_000_000_000,
            "cleanup_owner": "host model-store operator",
        },
        "artifacts": [artifacts[key] for key in sorted(artifacts)],
    }
    write_download_record(record, tmp_path)
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


def test_derived_inventory_reader_reverifies_snapshot_bytes(tmp_path):
    record = _store(tmp_path)
    path = tmp_path / "inventory.json"
    write_derived_inventory(record, path)
    entry = next(item for item in record["artifacts"] if item["artifact"] == "chandra-ocr-2")
    (tmp_path / entry["snapshot"] / "config.json").write_text(
        '{"fixture":"swapped"}', encoding="utf-8"
    )

    with pytest.raises(DigestMismatchRefusal, match="config.json"):
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
    (staging / "LICENSE").write_text("fixture licence\n", encoding="utf-8")
    (staging / "model.safetensors").write_bytes(b"fixture weights\n")
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
        (root / "model.safetensors").write_bytes(f"weights for {requirement.artifact}\n".encode())
        carried = []
        if requirement.artifact == "dai-recordgold-atr":
            for name in ("system.txt", "query.txt"):
                (root / name).write_text(f"{name} fixture\n", encoding="utf-8")
                carried.append({"name": name, "path": name, "citation": DAI_PROMPT_CITATION})
        manifest_path = tmp_path / "manifests" / f"{requirement.artifact}.json"
        pin = write_manifest(build_manifest(root), manifest_path)
        artifacts[requirement.artifact] = {
            "artifact": requirement.artifact,
            "state": "present",
            "source": requirement.source,
            "repo": requirement.repo,
            "revision": requirement.revision,
            "snapshot": root.relative_to(tmp_path).as_posix(),
            "manifest": manifest_path.relative_to(tmp_path).as_posix(),
            "digest_manifest": pin,
            "license": "LICENSE",
            "carried": carried,
            "required_files": sorted(
                ["LICENSE", "model.safetensors", *[x["path"] for x in carried]]
            ),
        }
    record = {
        "schema": STORE_SCHEMA,
        "layout": {
            "hf": "hf",
            "local": "local",
            "manifests": "manifests",
            "records": "records",
            "staging": "staging",
        },
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


def test_download_record_update_preserves_both_immutable_versions(tmp_path):
    complete = _store(tmp_path)
    pending = _mark_pending(
        tmp_path,
        copy.deepcopy(complete),
        "surya2-detection",
        "s3 bundle not yet fetched by the host",
    )
    pending_digest = write_download_record(pending, tmp_path)
    present = next(item for item in complete["artifacts"] if item["artifact"] == "surya2-detection")
    snapshot = tmp_path / present["snapshot"]
    snapshot.mkdir(parents=True)
    (snapshot / "config.json").write_text('{"fixture":true}', encoding="utf-8")
    (snapshot / "LICENSE").write_text("license for surya2-detection\n", encoding="utf-8")
    (snapshot / "model.safetensors").write_bytes(b"weights for surya2-detection\n")
    assert (
        write_manifest(build_manifest(snapshot), tmp_path / present["manifest"])
        == present["digest_manifest"]
    )

    present_digest = write_download_record(complete, tmp_path)

    assert load_download_record(tmp_path) == complete
    assert pending_digest != present_digest
    assert (tmp_path / "records" / f"{pending_digest}.json").read_bytes() == canonical_bytes(
        pending
    )
    assert (tmp_path / "records" / f"{present_digest}.json").read_bytes() == canonical_bytes(
        complete
    )


def test_fetched_artifact_cannot_be_relabelled_pending_fetch(tmp_path):
    record = _store(tmp_path)
    replacement = copy.deepcopy(record)
    required = next(item for item in REQUIRED_ARTIFACTS if item.artifact == "surya2-detection")
    index = next(
        index
        for index, item in enumerate(replacement["artifacts"])
        if item["artifact"] == "surya2-detection"
    )
    replacement["artifacts"][index] = {
        "artifact": required.artifact,
        "state": "pending-fetch",
        "source": required.source,
        "repo": required.repo,
        "revision": required.revision,
        "reason": "pretend it was never fetched",
    }

    with pytest.raises(DigestMismatchRefusal, match="fetched-and-lost"):
        write_download_record(replacement, tmp_path)

    assert load_download_record(tmp_path) == record
    rejected_digest = hashlib.sha256(canonical_bytes(replacement)).hexdigest()
    assert not (tmp_path / "records" / f"{rejected_digest}.json").exists()


def test_active_record_swap_does_not_rewrite_its_immutable_version(tmp_path):
    record = _store(tmp_path)
    original_bytes = canonical_bytes(record)
    original_digest = hashlib.sha256(original_bytes).hexdigest()
    archive = tmp_path / "records" / f"{original_digest}.json"
    swapped = copy.deepcopy(record)
    swapped["capacity"]["available_bytes"] += 1

    (tmp_path / "download_record.json").write_bytes(canonical_bytes(swapped))

    assert archive.read_bytes() == original_bytes
    with pytest.raises(DigestMismatchRefusal, match="immutable version"):
        load_download_record(tmp_path)


def test_writer_archives_the_legacy_host_record_before_migration(tmp_path):
    record = _store(tmp_path)
    (tmp_path / "download_record.json").unlink()
    shutil.rmtree(tmp_path / "records")
    legacy = canonical_bytes(
        {
            "datalab-to/chandra-ocr-2": {
                "revision": "af93b47dba1b47b6640c86ccf487ed2260ab9a09",
                "path": "hf/chandra-ocr-2",
            }
        }
    )
    (tmp_path / "download_record.json").write_bytes(legacy)

    write_download_record(record, tmp_path)

    legacy_digest = hashlib.sha256(legacy).hexdigest()
    assert (tmp_path / "records" / f"{legacy_digest}.json").read_bytes() == legacy
    assert load_download_record(tmp_path) == record


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
    (staging / "LICENSE").write_text("fixture licence\n", encoding="utf-8")
    (staging / "model.safetensors").write_bytes(b"fixture weights\n")
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


# --- O1: a half-materialized store is representable, and visibly partial --------


def _mark_pending(tmp_path, record, artifact, reason):
    """Rewrite one entry in the pending-fetch shape and remove its bytes.

    This is the store's real state today: five Hugging Face snapshots fetched,
    the Surya bundle not yet on disk.
    """
    required = next(item for item in REQUIRED_ARTIFACTS if item.artifact == artifact)
    for index, item in enumerate(record["artifacts"]):
        if item["artifact"] == artifact:
            shutil.rmtree(tmp_path / item["snapshot"])
            (tmp_path / item["manifest"]).unlink()
            record["artifacts"][index] = {
                "artifact": artifact,
                "state": "pending-fetch",
                "source": required.source,
                "repo": required.repo,
                "revision": required.revision,
                "reason": reason,
            }
    # Most pending-state tests need a pending genesis, not the forbidden claim
    # that a fetched artifact later became "not yet fetched". Reset only this
    # synthetic fixture's record history before publishing that genesis.
    (tmp_path / "download_record.json").unlink()
    shutil.rmtree(tmp_path / "records")
    write_download_record(record, tmp_path)
    return record


def test_a_store_whose_surya_bundle_has_not_landed_verifies_and_says_so(tmp_path):
    record = _mark_pending(
        tmp_path, _store(tmp_path), "surya2-detection", "s3 bundle not yet fetched by the host"
    )

    inventory = verify_store(tmp_path)

    assert inventory["complete"] is False
    assert inventory["pending"] == ["surya2-detection"]
    rows = {row["chair"]: row for row in inventory["artifacts"]}
    assert len(rows) == 7
    assert rows["proposer_surya2"]["state"] == "pending-fetch"
    assert rows["proposer_surya2"]["reason"] == "s3 bundle not yet fetched by the host"
    assert "snapshot" not in rows["proposer_surya2"]
    # The five artifacts that did land are verified exactly as before.
    assert all(rows[chair]["state"] == "present" for chair in rows if chair != "proposer_surya2")
    assert inventory == derived_inventory(record)


def test_require_complete_store_refuses_a_partial_store_by_name(tmp_path):
    _mark_pending(tmp_path, _store(tmp_path), "surya2-detection", "not fetched yet")

    with pytest.raises(DigestMismatchRefusal, match="surya2-detection"):
        require_complete_store(tmp_path)


def test_require_complete_store_accepts_a_store_with_every_roster_artifact(tmp_path):
    _store(tmp_path)

    inventory = require_complete_store(tmp_path)

    assert inventory["complete"] is True
    assert inventory["pending"] == []


def test_required_artifact_refuses_not_yet_fetched_by_name(tmp_path):
    _mark_pending(tmp_path, _store(tmp_path), "surya2-detection", "s3 fetch is pending")

    with pytest.raises(DigestMismatchRefusal, match="pending-fetch: s3 fetch is pending"):
        require_store_artifact(tmp_path, "surya2-detection")


def test_required_artifact_refuses_fetched_and_lost_bytes_by_filename(tmp_path):
    record = _store(tmp_path)
    entry = next(item for item in record["artifacts"] if item["artifact"] == "qwen3.5-9B")
    (tmp_path / entry["snapshot"] / "model.safetensors").unlink()

    with pytest.raises(DigestMismatchRefusal, match="model.safetensors: missing file"):
        require_store_artifact(tmp_path, "qwen3.5-9B")


def test_surya_ocr_is_not_required_and_use_refuses_with_its_escape_hatch(tmp_path):
    _store(tmp_path)

    assert SURYA_OCR_2_REFUSAL["state"] == "not-required"
    with pytest.raises(DigestMismatchRefusal, match="recorded-bench-need"):
        require_store_artifact(tmp_path, "surya-ocr-2")


def test_require_complete_store_cannot_be_satisfied_by_a_forged_inventory(tmp_path):
    record = _mark_pending(tmp_path, _store(tmp_path), "surya2-detection", "not fetched yet")
    forged = derived_inventory(record)
    forged["complete"] = True
    forged["pending"] = []

    with pytest.raises(DigestMismatchRefusal, match="surya2-detection"):
        require_complete_store(tmp_path)


def test_a_pending_entry_may_not_carry_evidence_for_bytes_that_are_not_there(tmp_path):
    record = _mark_pending(tmp_path, _store(tmp_path), "surya2-detection", "not fetched yet")
    entry = next(item for item in record["artifacts"] if item["artifact"] == "surya2-detection")
    entry["snapshot"] = "local/surya2-detection"

    with pytest.raises(DigestMismatchRefusal, match="pending-fetch entry carries exactly"):
        derived_inventory(record)


def test_a_pending_entry_must_say_why_the_artifact_is_not_on_disk(tmp_path):
    with pytest.raises(DigestMismatchRefusal, match="must say why"):
        _mark_pending(tmp_path, _store(tmp_path), "surya2-detection", "   ")


def test_a_pending_entry_is_held_to_the_same_roster_origin_as_a_present_one(tmp_path):
    record = _mark_pending(tmp_path, _store(tmp_path), "surya2-detection", "not fetched yet")
    entry = next(item for item in record["artifacts"] if item["artifact"] == "surya2-detection")
    entry["repo"] = "someone/surya2"

    with pytest.raises(DigestMismatchRefusal, match="must have no git pin"):
        derived_inventory(record)


def test_write_download_record_can_express_a_partial_store(tmp_path):
    record = _mark_pending(tmp_path, _store(tmp_path), "surya2-detection", "not fetched yet")
    elsewhere = tmp_path / "second-root"

    write_download_record(record, elsewhere)

    assert load_download_record(elsewhere) == record


# --- O2: the inventory's chair column is a roster role, not a label -------------


def test_every_store_chair_is_a_models_toml_role_or_one_recorded_exception():
    """A chair name the roster does not know cannot be joined to anything.

    The store exists to be bound to `config/models.toml` at S8. If its chair
    names drift from the roster's role keys, the divergence surfaces on the
    rented card during pod assembly instead of here.
    """

    config = load_models_toml(ROOT / "config" / "models.toml")
    store_chairs = {item.chair for item in REQUIRED_ARTIFACTS}
    assert store_chairs, "meta-invariant 88: the roster policy is not empty"

    assert store_chairs - set(config.chairs) == set(CHAIRS_WITHOUT_ROSTER_ROLE)
    assert set(CHAIRS_WITHOUT_ROSTER_ROLE) <= store_chairs
    assert all(reason.strip() for reason in CHAIRS_WITHOUT_ROSTER_ROLE.values())


# --- O3: the pod-sharing contract, encoded rather than described ----------------


def test_pod_materialization_plan_splits_verified_store_halves(tmp_path):
    record = _store(tmp_path)

    plan = pod_materialization_plan(tmp_path)

    # Six Hugging Face chairs over five snapshots: the two chandra chairs each
    # need their own role-keyed cache entry, both made from the one stored
    # snapshot, because a cache entry is keyed by role and a store is not.
    assert {chair: row["snapshot"] for chair, row in plan["cache_root_entries"].items()} == {
        "designator_structure": "hf/chandra-ocr-2",
        "attestator_1": "hf/chandra-ocr-2",
        "attestator_2": "hf/dai-recordgold-atr",
        "attestator_3": "hf/churro-3B",
        "secondary_proposer": "hf/yolo26-detection",
        "perlector": "hf/qwen3.5-9B",
    }
    assert len({row["snapshot"] for row in plan["cache_root_entries"].values()}) == 5
    # model_root is local-repository only; it is not a second cache.
    assert plan["model_root_entries"]["proposer_surya2"]["snapshot"] == ("local/surya2-detection")
    assert plan["download_record_sha256"] == derived_inventory(record)["download_record_sha256"]
    assert plan["provenance_scope"] == "verified-store-source-only"


def test_pod_materialization_plan_refuses_a_chair_not_fetched_yet(tmp_path):
    _mark_pending(tmp_path, _store(tmp_path), "surya2-detection", "not fetched yet")

    with pytest.raises(DigestMismatchRefusal, match="surya2-detection"):
        pod_materialization_plan(tmp_path)


def test_pod_materialization_plan_reverifies_source_bytes(tmp_path):
    record = _store(tmp_path)
    entry = next(item for item in record["artifacts"] if item["artifact"] == "qwen3.5-9B")
    (tmp_path / entry["snapshot"] / "config.json").write_text(
        '{"fixture":"swapped"}', encoding="utf-8"
    )

    with pytest.raises(DigestMismatchRefusal, match="config.json"):
        pod_materialization_plan(tmp_path)


def test_verify_store_refuses_a_snapshot_used_directly_as_a_cache_entry(tmp_path):
    """Pointing cache_root at the store makes the registry stamp its descriptor.

    The generic refusal for that ("extra file") names the file but not the
    cause; a store is keyed by artifact and a cache by role, and the two chairs
    sharing chandra-ocr-2 would in any case write two different descriptors
    over the one stored directory.
    """

    record = _store(tmp_path)
    entry = next(item for item in record["artifacts"] if item["artifact"] == "chandra-ocr-2")
    (tmp_path / entry["snapshot"] / CACHE_DESCRIPTOR).write_bytes(
        canonical_bytes({"role": "attestator_1"})
    )

    with pytest.raises(DigestMismatchRefusal, match="is not a cache_root entry"):
        verify_store(tmp_path)


# --- O4: a licence snapshot with no text, and publication failures ---------------


def test_store_refuses_a_licence_snapshot_with_no_text(tmp_path):
    """U17 pins the licence text of the revision, not a file with the right name."""

    record = _store(tmp_path)
    entry = next(item for item in record["artifacts"] if item["artifact"] == "churro-3B")
    snapshot = tmp_path / entry["snapshot"]
    (snapshot / "LICENSE").write_bytes(b"")
    entry["digest_manifest"] = write_manifest(
        build_manifest(snapshot), tmp_path / entry["manifest"]
    )
    write_download_record(record, tmp_path)

    with pytest.raises(DigestMismatchRefusal, match="is empty"):
        verify_store(tmp_path)


@pytest.mark.skipif(hasattr(os, "geteuid") and os.geteuid() == 0, reason="root ignores file modes")
def test_publication_into_a_read_only_store_refuses_inside_the_taxonomy(tmp_path):
    """A full disk or a locked store is what this writer exists to fail on.

    Sonnet's S4 argument leaves `_publish_once` as the real backstop against a
    store that cannot be written. A bare OSError escaping it would leave a
    caller that catches ChairRefusal — the complete public taxonomy — with an
    unhandled crash naming no chair.
    """

    record = _store(tmp_path)
    locked = tmp_path / "locked"
    locked.mkdir()
    locked.chmod(0o500)
    try:
        with pytest.raises(DigestMismatchRefusal, match="cannot publish"):
            write_derived_inventory(record, locked / "inventory.json")
    finally:
        locked.chmod(0o700)


def test_publication_onto_a_name_already_taken_by_a_directory_refuses(tmp_path):
    record = _store(tmp_path)
    (tmp_path / "inventory.json").mkdir()

    with pytest.raises(DigestMismatchRefusal, match="cannot publish"):
        write_derived_inventory(record, tmp_path / "inventory.json")


# --- O5: the refusals an operator migrating the real store will actually meet ----


def test_the_ad_hoc_download_record_refusal_names_what_the_v1_record_needs(tmp_path):
    """The host store's real record is the old download script's repo-keyed shape.

    Migrating it is a host action that happens against these refusals and
    nothing else, so each one has to say what is wrong rather than that
    something is.
    """

    ad_hoc = {
        "datalab-to/chandra-ocr-2": {"revision": "af93b47", "path": "chandra"},
        "Qwen/Qwen3.5-9B": {"revision": "c202236", "path": "qwen"},
    }
    (tmp_path / "download_record.json").write_bytes(canonical_bytes(ad_hoc))

    with pytest.raises(DigestMismatchRefusal) as refusal:
        load_download_record(tmp_path)

    message = str(refusal.value)
    assert "missing=['artifacts', 'capacity', 'layout', 'schema']" in message
    assert "unexpected=['Qwen/Qwen3.5-9B', 'datalab-to/chandra-ocr-2']" in message


def test_a_roster_divergence_names_the_pin_it_expected_and_the_one_it_found(tmp_path):
    record = _store(tmp_path)
    entry = next(item for item in record["artifacts"] if item["artifact"] == "churro-3B")
    entry["revision"] = "1" * 40

    with pytest.raises(DigestMismatchRefusal) as refusal:
        derived_inventory(record)

    message = str(refusal.value)
    assert "ca2150ea465d5a3d67818c50e234b9422619c75d" in message
    assert "1" * 40 in message


def test_a_capacity_refusal_names_the_four_fields_a_migrating_operator_must_write(tmp_path):
    record = _store(tmp_path)
    del record["capacity"]["cleanup_owner"]

    with pytest.raises(DigestMismatchRefusal, match="'snapshot_bytes'"):
        derived_inventory(record)


def test_a_digest_manifest_must_live_under_the_declared_manifests_root(tmp_path):
    """The record declares named roots; snapshots obeyed them and manifests did not."""

    record = _store(tmp_path)
    entry = next(item for item in record["artifacts"] if item["artifact"] == "churro-3B")
    stray = tmp_path / "hf" / "churro-3B-manifest.json"
    stray.write_bytes((tmp_path / entry["manifest"]).read_bytes())
    entry["manifest"] = "hf/churro-3B-manifest.json"

    with pytest.raises(DigestMismatchRefusal, match="outside the store's 'manifests/' root"):
        derived_inventory(record)


# --- L2: required files constrain a fetch, not merely the bytes that arrived -------


def test_store_refuses_a_required_weight_absent_from_a_rewritten_manifest(tmp_path):
    """A config-only snapshot cannot define its own smaller meaning of complete."""

    record = _store(tmp_path)
    entry = next(item for item in record["artifacts"] if item["artifact"] == "qwen3.5-9B")
    snapshot = tmp_path / entry["snapshot"]
    (snapshot / "model.safetensors").unlink()
    entry["digest_manifest"] = write_manifest(
        build_manifest(snapshot), tmp_path / entry["manifest"]
    )
    write_download_record(record, tmp_path)

    with pytest.raises(DigestMismatchRefusal, match="required file 'model.safetensors'"):
        verify_store(tmp_path)


def test_present_entry_required_files_must_name_a_model_payload(tmp_path):
    record = _store(tmp_path)
    entry = next(item for item in record["artifacts"] if item["artifact"] == "churro-3B")
    entry["required_files"] = ["LICENSE"]

    with pytest.raises(DigestMismatchRefusal, match="at least one model payload"):
        derived_inventory(record)


def test_store_refuses_an_empty_required_model_payload(tmp_path):
    record = _store(tmp_path)
    entry = next(item for item in record["artifacts"] if item["artifact"] == "churro-3B")
    snapshot = tmp_path / entry["snapshot"]
    (snapshot / "model.safetensors").write_bytes(b"")
    entry["digest_manifest"] = write_manifest(
        build_manifest(snapshot), tmp_path / entry["manifest"]
    )
    write_download_record(record, tmp_path)

    with pytest.raises(DigestMismatchRefusal, match="required file 'model.safetensors' is empty"):
        verify_store(tmp_path)
