"""Synthetic-store tests for R1 acquisition; no network or real weights are used."""

import copy
import hashlib
import json
import os
import re
import shutil
from dataclasses import replace
from pathlib import Path

import pytest

from common.chairs import model_store
from common.chairs.config import load_models_toml
from common.chairs.errors import DigestMismatchRefusal
from common.chairs.manifests import build_manifest, write_manifest
from common.chairs.model_store import (
    CHAIRS_WITHOUT_ROSTER_ROLE,
    DAI_PROMPT_CITATION,
    REQUIRED_ARTIFACTS,
    STORE_SCHEMA,
    SURYA_OCR_2_REFUSAL,
    UNDECLARED_LICENCE_SNAPSHOT,
    UNTEXTED_LICENCE_SNAPSHOT,
    derived_inventory,
    load_download_record,
    materialize_real_roster,
    pod_materialization_plan,
    promote_verified_snapshot,
    read_derived_inventory,
    require_complete_store,
    require_store_artifact,
    verify_store,
    write_derived_inventory,
    write_download_record,
)
from common.chairs.models import ChairIdentity
from common.chairs.registry import CACHE_DESCRIPTOR
from common.contracts.canonical import canonical_bytes, digest_bytes

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


def test_store_refuses_a_pinned_licence_whose_bytes_are_gone(tmp_path):
    # The manifest still pins LICENSE; only the snapshot's bytes vanished, so
    # this is byte-verification failing, not record validation.
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


def _promotion_artifact(
    tmp_path,
    entry,
    staging="staging/churro-3B-promoted",
):
    """A promotion artifact staging fresh bytes for the entry's own manifest name.

    Promotion admits exactly one manifest name per artifact (the artifact-keyed
    path the record layer enforces), so the fixture's already-published manifest
    is removed first: these tests exercise a fresh promotion at the one name a
    record may reference, from a staging directory of this helper's own bytes.
    """
    (tmp_path / entry["manifest"]).unlink()
    staged = tmp_path / staging
    staged.mkdir(parents=True)
    (staged / "config.json").write_text('{"fixture":true}', encoding="utf-8")
    (staged / "LICENSE").write_text("fixture licence\n", encoding="utf-8")
    (staged / "model.safetensors").write_bytes(b"fixture weights\n")
    return {**entry, "staging": staging}, staged


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


def test_promote_verified_snapshot_refuses_a_pending_shaped_entry_by_name(tmp_path):
    # A pending-fetch entry carries no staging, manifest, or required_files; it
    # must be refused through the taxonomy, not escape as a bare KeyError.
    record = _mark_pending(tmp_path, _store(tmp_path), "surya2-detection", "not fetched yet")
    entry = next(item for item in record["artifacts"] if item["artifact"] == "surya2-detection")

    with pytest.raises(DigestMismatchRefusal, match="no bytes to promote"):
        promote_verified_snapshot(tmp_path, entry)


# --- S5: a canonical writer closes the "hand-authored JSON" gap -----------------


def test_write_download_record_round_trips_through_load_download_record(tmp_path):
    """`_store` is the one host-record fixture, and it writes through this writer.

    This body was a verbatim second copy of `_store`, differing only in capacity
    figures neither assertion reads. One fixture means a record shape that
    changes cannot pass here while failing everywhere else.
    """

    record = _store(tmp_path)

    assert load_download_record(tmp_path) == record
    assert (tmp_path / "download_record.json").read_bytes() == canonical_bytes(record)


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

    # A byte-valid rollback to the archived pending version cannot make the
    # already-materialized snapshot become "not yet fetched" again.
    (tmp_path / "download_record.json").write_bytes(canonical_bytes(pending))
    assert load_download_record(tmp_path) == pending
    with pytest.raises(DigestMismatchRefusal, match="existing acquisition evidence"):
        verify_store(tmp_path)


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


def test_a_recorded_artifact_cannot_be_renamed_out_of_the_next_record_version(tmp_path):
    """A dropped name must refuse by name, not escape the closed refusal taxonomy.

    Six unique artifacts in, six out, so renaming one drops the old name. The
    transition check read the replacement by that key directly and raised a bare
    ``KeyError`` naming no chair — outside ``errors.py``'s "complete public
    taxonomy", and silent about which artifact left the record.
    """

    record = _store(tmp_path)
    replacement = copy.deepcopy(record)
    entry = next(item for item in replacement["artifacts"] if item["artifact"] == "churro-3B")
    entry["artifact"] = "churro-3B-renamed"
    entry["snapshot"] = "hf/churro-3B-renamed"
    entry["manifest"] = "manifests/churro-3B-renamed.json"

    with pytest.raises(DigestMismatchRefusal, match="does not name this recorded artifact"):
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


def test_active_record_symlink_is_not_accepted_as_in_store_custody(tmp_path):
    _store(tmp_path)
    active = tmp_path / "download_record.json"
    external = tmp_path.parent / f"{tmp_path.name}-active-record"
    external.write_bytes(active.read_bytes())
    active.unlink()
    active.symlink_to(external)

    with pytest.raises(DigestMismatchRefusal, match="regular in-store active copy"):
        load_download_record(tmp_path)


def test_active_record_fifo_is_refused_before_any_blocking_read(tmp_path):
    os.mkfifo(tmp_path / "download_record.json")

    with pytest.raises(DigestMismatchRefusal, match="regular in-store active copy"):
        load_download_record(tmp_path)


def test_immutable_record_version_cannot_hide_behind_an_internal_symlink(tmp_path):
    record = _store(tmp_path)
    digest = digest_bytes(canonical_bytes(record))
    archive = tmp_path / "records" / f"{digest}.json"
    backing = archive.with_name("mutable-backing.json")
    archive.replace(backing)
    archive.symlink_to(backing.name)

    with pytest.raises(DigestMismatchRefusal, match="must not traverse symlink component"):
        load_download_record(tmp_path)


def test_verified_snapshot_root_cannot_hide_behind_an_internal_symlink(tmp_path):
    record = _store(tmp_path)
    entry = next(item for item in record["artifacts"] if item["artifact"] == "churro-3B")
    snapshot = tmp_path / entry["snapshot"]
    backing = snapshot.with_name("churro-3B-mutable-backing")
    snapshot.replace(backing)
    snapshot.symlink_to(backing.name, target_is_directory=True)

    with pytest.raises(DigestMismatchRefusal, match="must not traverse symlink component"):
        verify_store(tmp_path)


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
    artifact, staged = _promotion_artifact(tmp_path, entry, staging="staging/nested/churro-3B")

    digest = promote_verified_snapshot(tmp_path, artifact)

    # The claim is the published artifact, not the return value's shape: the
    # manifest of the staged bytes sits at the artifact-keyed name, and the
    # returned digest is the digest of those exact published bytes.
    published = (tmp_path / artifact["manifest"]).read_bytes()
    assert published == canonical_bytes(build_manifest(staged).to_record())
    assert digest == digest_bytes(published)


def test_materializer_refuses_a_staging_root_symlink_before_fetching_outside_store(tmp_path):
    store = tmp_path / "store"
    store.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (store / "staging").symlink_to(outside, target_is_directory=True)

    with pytest.raises(DigestMismatchRefusal, match="escapes configured root"):
        materialize_real_roster(
            store, _FakeMaterializationFetcher(), capacity=dict(_MATERIALIZATION_CAPACITY)
        )

    assert sorted(outside.iterdir()) == []


def test_materializer_refuses_a_fetcher_that_replaces_its_staging_directory(tmp_path):
    store = tmp_path / "store"
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "model.safetensors").write_bytes(b"outside weights")
    (outside / "README.md").write_text("---\nlicense: openrail\n---\n", encoding="utf-8")

    class _ReplacesDestination:
        def fetch(self, repo: str, revision: str, destination: Path) -> None:
            destination.rmdir()
            destination.symlink_to(outside, target_is_directory=True)

    with pytest.raises(DigestMismatchRefusal, match="replaced the materialization destination"):
        materialize_real_roster(
            store, _ReplacesDestination(), capacity=dict(_MATERIALIZATION_CAPACITY)
        )

    assert not (outside / UNTEXTED_LICENCE_SNAPSHOT).exists()
    assert sorted((store / "staging").iterdir()) == []


def test_materializer_names_a_cleanup_failure_without_losing_the_fetch_failure(
    tmp_path, monkeypatch
):
    class _FailsAfterWriting:
        def fetch(self, repo: str, revision: str, destination: Path) -> None:
            (destination / "partial.safetensors").write_bytes(b"partial")
            raise RuntimeError("fetch transport failed")

    def refuse_cleanup(path, **kwargs):
        raise PermissionError("cleanup denied")

    monkeypatch.setattr(model_store.shutil, "rmtree", refuse_cleanup)

    with pytest.raises(
        DigestMismatchRefusal,
        match="fetch transport failed.*staging cleanup also failed.*cleanup denied",
    ):
        materialize_real_roster(
            tmp_path, _FailsAfterWriting(), capacity=dict(_MATERIALIZATION_CAPACITY)
        )


def test_materializer_refuses_a_staged_symlink_before_reading_its_target(tmp_path, monkeypatch):
    outside_index = tmp_path / "outside-index.json"
    outside_index.write_text(
        json.dumps({"weight_map": {"layer": "model.safetensors"}}), encoding="utf-8"
    )

    class _SymlinkedShardIndex:
        def fetch(self, repo: str, revision: str, destination: Path) -> None:
            (destination / "model.safetensors").write_bytes(b"weights")
            (destination / "LICENSE").write_text("terms", encoding="utf-8")
            (destination / "model.safetensors.index.json").symlink_to(outside_index)

    # `_indexed_shards` does not use `Path.read_text`; it reads through
    # `_read_limited_bytes`. Guarding the wrong call left the claim in this
    # test's name -- that the external index was never read -- asserted nowhere,
    # so a change that read the symlinked index before the symlink check would
    # have passed here.
    real_read_limited_bytes = model_store._read_limited_bytes

    def refuse_external_read(path, *args, **kwargs):
        if Path(path).resolve() == outside_index:
            raise AssertionError("the external shard index was read")
        return real_read_limited_bytes(path, *args, **kwargs)

    monkeypatch.setattr(model_store, "_read_limited_bytes", refuse_external_read)

    with pytest.raises(DigestMismatchRefusal, match="symlink"):
        materialize_real_roster(
            tmp_path, _SymlinkedShardIndex(), capacity=dict(_MATERIALIZATION_CAPACITY)
        )


def test_materializer_refuses_a_hard_link_to_bytes_owned_outside_staging(tmp_path):
    outside = tmp_path / "outside-operator-file"
    outside.write_bytes(b"not repository evidence")
    store = tmp_path / "store"

    class _HardLinksExternalBytes:
        def fetch(self, repo: str, revision: str, destination: Path) -> None:
            del repo, revision
            os.link(outside, destination / "model.safetensors")

    with pytest.raises(DigestMismatchRefusal, match="hard-linked file"):
        materialize_real_roster(
            store, _HardLinksExternalBytes(), capacity=dict(_MATERIALIZATION_CAPACITY)
        )

    assert outside.read_bytes() == b"not repository evidence"
    assert outside.stat().st_nlink == 1


def test_promote_verified_snapshot_refuses_a_manifest_name_no_record_may_reference(tmp_path):
    record = _store(tmp_path)
    entry = next(item for item in record["artifacts"] if item["artifact"] == "churro-3B")
    artifact, _ = _promotion_artifact(tmp_path, entry)
    artifact["manifest"] = "manifests/churro-3B-nested.json"

    with pytest.raises(DigestMismatchRefusal, match="artifact-keyed path"):
        promote_verified_snapshot(tmp_path, artifact)


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


def test_download_record_read_is_bounded_before_json_deserialization(tmp_path, monkeypatch):
    monkeypatch.setattr(model_store, "MAX_DOWNLOAD_RECORD_BYTES", 32)
    (tmp_path / "download_record.json").write_bytes(b"{" + b"x" * 32)

    with pytest.raises(DigestMismatchRefusal, match="32-byte control-artifact limit"):
        load_download_record(tmp_path)


def test_verify_store_refuses_a_manifest_fifo_before_any_blocking_read(tmp_path):
    record = _store(tmp_path)
    entry = next(item for item in record["artifacts"] if item["artifact"] == "churro-3B")
    manifest = tmp_path / entry["manifest"]
    manifest.unlink()
    os.mkfifo(manifest)

    with pytest.raises(DigestMismatchRefusal, match="must be a regular file"):
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


def test_validate_record_refuses_an_artifact_name_that_is_itself_a_path(tmp_path):
    """The artifact name keys a snapshot directory and a manifest filename.

    ``derived_inventory``'s roster join holds it to a known name, but
    ``write_download_record`` validates a record without that join, and
    ``verify_store`` builds a *pending* entry's absent-evidence paths from the
    name alone — a pending entry carries no snapshot or manifest field to check.
    """

    record = _store(tmp_path)
    record["artifacts"][0]["artifact"] = "../escape"

    with pytest.raises(DigestMismatchRefusal, match="artifact name is not a safe relative"):
        write_download_record(record, tmp_path)


def test_a_store_path_may_not_name_the_store_root_itself(tmp_path):
    """'.' has no path parts, so every other clause of the rule passed it."""

    record = _store(tmp_path)
    record["artifacts"][0]["snapshot"] = "."

    with pytest.raises(DigestMismatchRefusal, match="snapshot is not a safe relative"):
        derived_inventory(record)


def test_validate_record_refuses_a_duplicate_artifact_name(tmp_path):
    record = _store(tmp_path)
    record["artifacts"][1]["artifact"] = record["artifacts"][0]["artifact"]

    with pytest.raises(DigestMismatchRefusal, match="unique"):
        derived_inventory(record)


def test_validate_record_refuses_a_five_artifact_record(tmp_path):
    record = _store(tmp_path)
    record["artifacts"] = record["artifacts"][:5]

    with pytest.raises(DigestMismatchRefusal, match="exactly 6 unique roster"):
        derived_inventory(record)


def test_derived_inventory_refuses_a_revision_that_disagrees_with_the_roster(tmp_path):
    record = _store(tmp_path)
    entry = next(item for item in record["artifacts"] if item["artifact"] == "chandra-ocr-2")
    entry["revision"] = "1" * 40

    with pytest.raises(DigestMismatchRefusal, match="diverges from roster policy"):
        derived_inventory(record)


def test_derived_inventory_holds_a_renamed_artifact_to_the_artifact_keyed_path_rule(tmp_path):
    # The path rule fires before the roster join ever sees the name: the entry's
    # snapshot and manifest are keyed by the old spelling, so the renamed entry
    # is refused as a layout mismatch, not as a stranger to the roster.
    record = _store(tmp_path)
    record["artifacts"][0]["artifact"] = "chandra-ocr-2’"

    with pytest.raises(DigestMismatchRefusal, match="artifact-keyed path"):
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
    entry = next(item for item in record["artifacts"] if item["artifact"] == "qwen3.8-27B")
    (tmp_path / entry["snapshot"] / "model.safetensors").unlink()

    with pytest.raises(DigestMismatchRefusal, match="model.safetensors: missing file"):
        require_store_artifact(tmp_path, "qwen3.8-27B")


def test_required_artifact_names_every_chair_it_serves_not_one_rows_chair(tmp_path):
    """chandra-ocr-2 fills two chairs; an artifact-keyed answer must say both."""

    _store(tmp_path)

    result = require_store_artifact(tmp_path, "chandra-ocr-2")

    assert result["chairs"] == ["attestator_1", "designator_structure"]
    assert "chair" not in result


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

    # The door takes a store root and re-derives its own inventory from real
    # bytes, so a flipped `complete` flag has no way in — and the wrong-shape
    # mistake is refused inside the taxonomy, naming what was expected, not
    # left to pathlib's TypeError.
    with pytest.raises(DigestMismatchRefusal, match="carries no authority"):
        require_complete_store(forged)
    with pytest.raises(DigestMismatchRefusal, match="surya2-detection"):
        require_complete_store(tmp_path)


def test_write_download_record_refuses_what_its_readers_would_refuse(tmp_path):
    """The writer runs the roster join: no record is published that every reader refuses."""

    record = _store(tmp_path)
    active = (tmp_path / "download_record.json").read_bytes()
    entry = next(item for item in record["artifacts"] if item["artifact"] == "chandra-ocr-2")
    entry["revision"] = "1" * 40

    with pytest.raises(DigestMismatchRefusal, match="diverges from roster policy"):
        write_download_record(record, tmp_path)
    assert (tmp_path / "download_record.json").read_bytes() == active
    rejected_digest = digest_bytes(canonical_bytes(record))
    assert not (tmp_path / "records" / f"{rejected_digest}.json").exists()


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

    The store exists to be bound to `config/models.toml` when the real roster is
    activated. If its chair names drift from the roster's role keys, the
    divergence surfaces on the rented card during pod assembly instead of here.
    """

    config = load_models_toml(ROOT / "config" / "models.toml")
    store_chairs = {item.chair for item in REQUIRED_ARTIFACTS}
    assert store_chairs, "meta-invariant 88: the roster policy is not empty"

    assert store_chairs - set(config.chairs) == set(CHAIRS_WITHOUT_ROSTER_ROLE)
    assert set(CHAIRS_WITHOUT_ROSTER_ROLE) <= store_chairs
    assert all(reason.strip() for reason in CHAIRS_WITHOUT_ROSTER_ROLE.values())


def _artifact_disagreements(chairs) -> list[str]:
    """Reconcile a roster's Hugging Face chairs against the store's own pins.

    Extracted so the reconciliation can be run against a roster that is not the
    live one. The live roster is all `local-repository` fixtures, so running this
    over it compares nothing — which is the correct answer for the current
    repository state and is exactly why it cannot be the only test.
    """

    store = {item.chair: item for item in REQUIRED_ARTIFACTS}
    problems = []
    for role, identity in sorted(chairs.items()):
        if not isinstance(identity, ChairIdentity) or identity.source != "huggingface":
            continue
        entry = store.get(role)
        if entry is None:
            problems.append(f"{role}: resolves to a fetched repository, store names no artifact")
            continue
        if (entry.source, entry.repo, entry.revision) != (
            "huggingface",
            identity.repo,
            identity.revision,
        ):
            problems.append(
                f"{role}: roster says {identity.repo}@{identity.revision}, "
                f"store says {entry.repo}@{entry.revision}"
            )
    return problems


def test_the_live_roster_never_disagrees_with_the_store_about_an_artifact():
    """Role keys reconciling is not the same as the artifacts reconciling.

    The test above proves the two lists agree on *which chairs exist*. It does not
    look at what each chair points AT, and that is the half that actually drifted:
    when the Perlector moved from `Qwen3.5-9B` to `Qwen3.8-27B`, `models.toml` and
    `REQUIRED_ARTIFACTS` could have been changed one without the other and every
    committed check would still have passed, while a pod materialized the store and
    fetched the wrong weights.

    Vacuous against the live roster today — every live chair is a fixture snapshot —
    so the test below it supplies a parseable Hugging Face roster and proves the
    reconciliation actually fires. Both are kept: this one guards the file that
    ships, that one guards the logic.
    """

    config = load_models_toml(ROOT / "config" / "models.toml")
    assert _artifact_disagreements(config.chairs) == []


def test_a_huggingface_roster_must_name_the_store_s_exact_repo_and_revision():
    """Store pins must reconcile with the sole chair-to-model authority.

    The agreeing case derives from the store entry so only deliberate mutations
    are independent pin literals.
    """

    perlector = next(item for item in REQUIRED_ARTIFACTS if item.chair == "perlector")
    assert perlector.source == "huggingface", "the Perlector pin stopped being a fetched repo"
    live = load_models_toml(ROOT / "config" / "models.toml").chairs["perlector"]
    agreeing = replace(
        live,
        source="huggingface",
        repo=perlector.repo,
        revision=perlector.revision,
    )

    assert _artifact_disagreements({"perlector": agreeing}) == []

    drifted_revision = replace(agreeing, revision="0" * 40)
    assert _artifact_disagreements({"perlector": drifted_revision}) == [
        f"perlector: roster says {perlector.repo}@{'0' * 40}, "
        f"store says {perlector.repo}@{perlector.revision}"
    ]

    drifted_repo = replace(agreeing, repo="Qwen/Qwen3.5-9B")
    assert len(_artifact_disagreements({"perlector": drifted_repo})) == 1

    unknown_chair = _artifact_disagreements({"annotator": agreeing})
    assert unknown_chair == ["annotator: resolves to a fetched repository, store names no artifact"]


def test_real_roster_and_materialization_inventory_name_the_same_pinned_repositories():
    """The selectable real roster cannot drift from the launch-time fetch list."""

    real = load_models_toml(ROOT / "config" / "models-real.toml")
    assert _artifact_disagreements(real.chairs) == []
    expected = {
        item.chair: (item.repo, item.revision)
        for item in REQUIRED_ARTIFACTS
        if item.source == "huggingface"
    }
    observed = {
        role: (identity.repo, identity.revision)
        for role, identity in real.chairs.items()
        if isinstance(identity, ChairIdentity) and identity.source == "huggingface"
    }
    assert observed == expected


_MATERIALIZATION_CAPACITY = {
    "snapshot_bytes": 100,
    "promotion_headroom_bytes": 100,
    "available_bytes": 200,
    "cleanup_owner": "pod operator",
}


class _FakeMaterializationFetcher:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    def fetch(self, repo: str, revision: str, destination: Path) -> None:
        self.calls.append((repo, revision))
        destination.mkdir(parents=True, exist_ok=True)
        (destination / "model.safetensors").write_bytes(f"weights {repo}@{revision}".encode())
        requirement = next(item for item in REQUIRED_ARTIFACTS if item.repo == repo)
        metadata = ""
        if requirement.license_declaration is not None:
            license_id, separator, license_name = requirement.license_declaration.partition(": ")
            metadata = f"license: {license_id}\n"
            if separator:
                metadata += f"license_name: {license_name}\n"
        (destination / "README.md").write_text(f"---\n{metadata}---\n", encoding="utf-8")
        if "RecordGold" in repo:
            (destination / "system.txt").write_text("system prompt", encoding="utf-8")
            (destination / "query.txt").write_text("query prompt", encoding="utf-8")
        else:
            (destination / "LICENSE").write_text("upstream licence", encoding="utf-8")


def test_a_missing_client_package_is_not_reported_as_a_corrupt_model_card(tmp_path, monkeypatch):
    """A refusal that already names its cause must not be relabelled.

    `load_model_card_metadata` builds the production fetcher, which raises
    `UnresolvedChairRefusal("huggingface_hub is not installed ...")` when the
    package is absent from the image. The blanket `except Exception` republished
    that as "the fetched README.md has unreadable model-card metadata", so at pod
    boot the operator read that the pinned repository's card was damaged and
    re-fetched an intact repository while the GPU billed.
    """

    from common.chairs.errors import UnresolvedChairRefusal

    snapshot = tmp_path / "snapshot"
    snapshot.mkdir()
    (snapshot / model_store.MODEL_CARD_PATH).write_text(
        "---\nlicense: mit\n---\n", encoding="utf-8"
    )

    def no_client(_path):
        raise UnresolvedChairRefusal(
            "attestator_1", "huggingface_hub is not installed for the production fetcher"
        )

    monkeypatch.setattr(model_store, "load_model_card_metadata", no_client)
    requirement = next(item for item in REQUIRED_ARTIFACTS if item.source == "huggingface")

    with pytest.raises(UnresolvedChairRefusal) as refusal:
        model_store._reconcile_model_card_licence(snapshot, requirement)

    assert "huggingface_hub is not installed" in str(refusal.value)
    assert "unreadable model-card metadata" not in str(refusal.value)


def test_a_resumed_materialization_refuses_a_capacity_plan_that_is_not_the_recorded_one(tmp_path):
    """The observation the caller supplied is not thrown away without a word.

    A resume rebound `record` to the one on disk, so the supplied capacity was
    neither recorded nor compared: a volume that was resized, or a store moved to
    a different volume, kept publishing the previous volume's figures. Only
    `_validate_record` looks at capacity, and it asks nothing beyond whether one
    plan is self-consistent, so nothing reported the disagreement.
    """

    fetcher = _FakeMaterializationFetcher()
    materialize_real_roster(tmp_path, fetcher, capacity=dict(_MATERIALIZATION_CAPACITY))
    assert load_download_record(tmp_path)["capacity"] == dict(_MATERIALIZATION_CAPACITY)

    moved = dict(_MATERIALIZATION_CAPACITY) | {"available_bytes": 400}

    with pytest.raises(DigestMismatchRefusal) as refusal:
        materialize_real_roster(tmp_path, fetcher, capacity=moved)

    detail = str(refusal.value)
    assert "capacity plan differs" in detail
    assert "400" in detail
    # The recorded plan is evidence and is not rewritten by the refusal.
    assert load_download_record(tmp_path)["capacity"] == dict(_MATERIALIZATION_CAPACITY)


def test_pod_materializer_fetches_each_real_pin_once_and_records_measured_evidence(tmp_path):
    fetcher = _FakeMaterializationFetcher()
    capacity = dict(_MATERIALIZATION_CAPACITY)

    receipt = materialize_real_roster(tmp_path, fetcher, capacity=capacity)

    expected = {
        (item.repo, item.revision) for item in REQUIRED_ARTIFACTS if item.source == "huggingface"
    }
    assert sorted(fetcher.calls) == sorted(expected)
    assert {row["artifact"] for row in receipt["artifacts"]} == {
        item.artifact for item in REQUIRED_ARTIFACTS if item.source == "huggingface"
    }
    assert all(len(row["digest_manifest"]) == 64 for row in receipt["artifacts"])
    record = load_download_record(tmp_path)
    dai = next(item for item in record["artifacts"] if item["artifact"] == "dai-recordgold-atr")
    assert dai["license"] == UNDECLARED_LICENCE_SNAPSHOT
    assert (
        (tmp_path / dai["snapshot"] / dai["license"])
        .read_text(encoding="utf-8")
        .startswith("No licence file and no licence declaration were present")
    )
    assert receipt["complete"] is False  # Surya remains an explicit non-real-roster pending item.
    assert receipt["real_roster_complete"] is True


def test_pod_materializer_reuses_verified_present_snapshots_without_refetching(tmp_path):
    fetcher = _FakeMaterializationFetcher()
    capacity = dict(_MATERIALIZATION_CAPACITY)
    materialize_real_roster(tmp_path, fetcher, capacity=capacity)
    calls = list(fetcher.calls)

    materialize_real_roster(tmp_path, fetcher, capacity=capacity)

    assert fetcher.calls == calls


def test_materializer_joins_a_loaded_record_to_the_roster_before_indexing_it(tmp_path):
    record = _store(tmp_path)
    entry = next(item for item in record["artifacts"] if item["artifact"] == "churro-3B")
    entry["artifact"] = "churro-3B-renamed"
    entry["snapshot"] = "hf/churro-3B-renamed"
    entry["manifest"] = "manifests/churro-3B-renamed.json"
    payload = canonical_bytes(record)
    (tmp_path / "download_record.json").write_bytes(payload)
    (tmp_path / "records" / f"{digest_bytes(payload)}.json").write_bytes(payload)

    with pytest.raises(DigestMismatchRefusal, match="required artifact 'churro-3B' is absent"):
        materialize_real_roster(
            tmp_path, _FakeMaterializationFetcher(), capacity=dict(_MATERIALIZATION_CAPACITY)
        )


def test_materializer_does_not_call_another_writers_staging_entry_an_orphan(tmp_path):
    staging = tmp_path / "staging"
    staging.mkdir()
    (staging / ".other-materializer.fetch-live").mkdir()

    receipt = materialize_real_roster(
        tmp_path, _FakeMaterializationFetcher(), capacity=dict(_MATERIALIZATION_CAPACITY)
    )

    assert receipt["unattributed_staging_entries"] == [".other-materializer.fetch-live"]
    assert "staging_orphans" not in receipt


def test_a_second_boot_verifies_the_whole_store_once_not_once_per_artifact(tmp_path, monkeypatch):
    """A populated boot re-verifies once because each call hashes the whole store."""

    fetcher = _FakeMaterializationFetcher()
    materialize_real_roster(tmp_path, fetcher, capacity=dict(_MATERIALIZATION_CAPACITY))
    present = {item["artifact"] for item in load_download_record(tmp_path)["artifacts"]} - {
        "surya2-detection"
    }
    assert len(present) == 5

    calls = []
    real = model_store.verify_store
    monkeypatch.setattr(
        model_store, "verify_store", lambda root: (calls.append(root), real(root))[1]
    )
    receipt = materialize_real_roster(tmp_path, fetcher, capacity=dict(_MATERIALIZATION_CAPACITY))

    assert len(calls) == 1
    assert {row["artifact"] for row in receipt["artifacts"]} == present


def test_materializer_receipt_digest_names_the_record_whole_store_verification_checked(
    tmp_path, monkeypatch
):
    """A concurrent valid active-record update cannot relabel verified evidence."""

    fetcher = _FakeMaterializationFetcher()
    capacity = dict(_MATERIALIZATION_CAPACITY)
    materialize_real_roster(tmp_path, fetcher, capacity=capacity)
    verify = model_store.verify_store
    observed: dict[str, str] = {}

    def verify_then_advance_active_record(root):  # type: ignore[no-untyped-def]
        inventory = verify(root)
        observed["verified"] = inventory["download_record_sha256"]
        replacement = load_download_record(root)
        replacement["capacity"]["available_bytes"] += 1
        observed["advanced"] = write_download_record(replacement, root)
        return inventory

    monkeypatch.setattr(model_store, "verify_store", verify_then_advance_active_record)

    receipt = materialize_real_roster(tmp_path, fetcher, capacity=capacity)

    assert observed["verified"] != observed["advanced"]
    assert receipt["download_record_sha256"] == observed["verified"]
    assert digest_bytes(canonical_bytes(load_download_record(tmp_path))) == observed["advanced"]


def _die_on_call(monkeypatch, name, ordinal):
    """Kill the materializer inside one named step, the way a pod dies."""

    real = getattr(model_store, name)
    calls = {"n": 0}

    def interrupted(*args, **kwargs):
        calls["n"] += 1
        if calls["n"] == ordinal:
            raise KeyboardInterrupt("pod died mid-materialization")
        return real(*args, **kwargs)

    monkeypatch.setattr(model_store, name, interrupted)


@pytest.mark.parametrize(
    ("killed_at", "ordinal", "expected_evidence"),
    [
        # Between publishing the second artifact's manifest and moving its
        # snapshot into place: a manifest with no snapshot beside it.
        (
            "_promote_materialized_snapshot",
            2,
            ["manifests/dai-recordgold-atr.json"],
        ),
        # Between moving the first artifact's snapshot into place and recording
        # it present — the second record write, the first being the all-pending
        # record this store opened with: a full snapshot the record denies.
        (
            "write_download_record",
            2,
            ["hf/chandra-ocr-2", "manifests/chandra-ocr-2.json"],
        ),
    ],
)
def test_a_boot_killed_mid_materialization_resumes_without_hand_repair(
    tmp_path, monkeypatch, killed_at, ordinal, expected_evidence
):
    """Every fetch-to-record interruption window must recover by re-fetching its pin."""

    capacity = dict(_MATERIALIZATION_CAPACITY)
    _die_on_call(monkeypatch, killed_at, ordinal)
    with pytest.raises(KeyboardInterrupt):
        materialize_real_roster(tmp_path, _FakeMaterializationFetcher(), capacity=capacity)
    monkeypatch.undo()

    with pytest.raises(DigestMismatchRefusal) as refusal:
        verify_store(tmp_path)
    assert str(expected_evidence) in str(refusal.value)
    # Interrupts outside `Exception` must still release their staged capacity.
    assert sorted((tmp_path / "staging").iterdir()) == []

    receipt = materialize_real_roster(tmp_path, _FakeMaterializationFetcher(), capacity=capacity)

    assert {row["artifact"] for row in receipt["artifacts"]} == {
        item.artifact for item in REQUIRED_ARTIFACTS if item.source == "huggingface"
    }
    assert receipt["unattributed_staging_entries"] == []
    verify_store(tmp_path)


def test_a_resumed_boot_still_refuses_bytes_that_differ_from_the_first_fetch(tmp_path, monkeypatch):
    """Resuming is a re-fetch that must agree with the orphan, never a repin.

    The recovery above works because a pinned revision fetched twice yields the
    same bytes, so the orphaned manifest is republished identically and reused.
    If it does not, the orphan is the pin and the new bytes lose: publication
    never overwrites existing evidence (GOVERNANCE 4).
    """

    capacity = dict(_MATERIALIZATION_CAPACITY)

    class _Drifted(_FakeMaterializationFetcher):
        def fetch(self, repo: str, revision: str, destination: Path) -> None:
            super().fetch(repo, revision, destination)
            (destination / "model.safetensors").write_bytes(b"different weights")

    _die_on_call(monkeypatch, "_promote_materialized_snapshot", 2)
    with pytest.raises(KeyboardInterrupt):
        materialize_real_roster(tmp_path, _FakeMaterializationFetcher(), capacity=capacity)
    monkeypatch.undo()

    with pytest.raises(DigestMismatchRefusal, match="already exists with different bytes"):
        materialize_real_roster(tmp_path, _Drifted(), capacity=capacity)


def test_a_repository_that_ships_no_licence_file_may_still_have_declared_one(tmp_path):
    """No licence file and no licence declaration are distinct observations."""

    class _NoLicenceFiles(_FakeMaterializationFetcher):
        def fetch(self, repo: str, revision: str, destination: Path) -> None:
            super().fetch(repo, revision, destination)
            (destination / "LICENSE").unlink(missing_ok=True)

    materialize_real_roster(tmp_path, _NoLicenceFiles(), capacity=dict(_MATERIALIZATION_CAPACITY))

    record = load_download_record(tmp_path)
    stored = {item["artifact"]: item for item in record["artifacts"]}
    for requirement in REQUIRED_ARTIFACTS:
        if requirement.source != "huggingface":
            continue
        entry = stored[requirement.artifact]
        text = (tmp_path / entry["snapshot"] / entry["license"]).read_text(encoding="utf-8")
        if requirement.license_declaration is None:
            assert entry["license"] == UNDECLARED_LICENCE_SNAPSHOT
            assert "no licence declaration" in text
        else:
            assert entry["license"] == UNTEXTED_LICENCE_SNAPSHOT
            assert requirement.license_declaration in text
    # Synthetic evidence must be inside the manifest's custody boundary.
    yolo = stored["yolo26-detection"]
    assert yolo["license"] in yolo["required_files"]
    assert "README.md" in yolo["required_files"]
    (tmp_path / yolo["snapshot"] / yolo["license"]).write_text("agpl-3.0", encoding="utf-8")
    with pytest.raises(DigestMismatchRefusal, match=UNTEXTED_LICENCE_SNAPSHOT):
        verify_store(tmp_path)


def test_declared_licence_observation_requires_the_model_card_it_cites(tmp_path):
    """A short fetch cannot make a synthetic observation about absent evidence."""

    requirement = next(item for item in REQUIRED_ARTIFACTS if item.artifact == "yolo26-detection")
    (tmp_path / "model.pt").write_bytes(b"weights")

    with pytest.raises(DigestMismatchRefusal, match="no regular README.md"):
        model_store._snapshot_licence(tmp_path, requirement)

    assert not (tmp_path / UNTEXTED_LICENCE_SNAPSHOT).exists()


def test_declared_licence_observation_must_match_the_fetched_model_card(tmp_path):
    requirement = next(item for item in REQUIRED_ARTIFACTS if item.artifact == "yolo26-detection")
    (tmp_path / "model.pt").write_bytes(b"weights")
    (tmp_path / "README.md").write_text("---\nlicense: apache-2.0\n---\n", encoding="utf-8")

    with pytest.raises(
        DigestMismatchRefusal,
        match="model card declares 'apache-2.0'.*roster policy expects 'agpl-3.0'",
    ):
        model_store._snapshot_licence(tmp_path, requirement)

    assert not (tmp_path / UNTEXTED_LICENCE_SNAPSHOT).exists()


def test_undeclared_licence_observation_requires_the_model_card_it_describes(tmp_path):
    requirement = next(item for item in REQUIRED_ARTIFACTS if item.artifact == "dai-recordgold-atr")
    (tmp_path / "model.safetensors").write_bytes(b"weights")

    with pytest.raises(DigestMismatchRefusal, match="no regular README.md"):
        model_store._snapshot_licence(tmp_path, requirement)

    assert not (tmp_path / UNDECLARED_LICENCE_SNAPSHOT).exists()


def test_nested_third_party_licence_is_not_called_the_repository_licence(tmp_path):
    requirement = next(item for item in REQUIRED_ARTIFACTS if item.artifact == "dai-recordgold-atr")
    (tmp_path / "README.md").write_text("---\nbase_model: example/base\n---\n", encoding="utf-8")
    nested = tmp_path / "vendor"
    nested.mkdir()
    (nested / "LICENSE").write_text("third-party terms", encoding="utf-8")

    assert model_store._snapshot_licence(tmp_path, requirement) == UNDECLARED_LICENCE_SNAPSHOT


@pytest.mark.parametrize(
    ("artifact", "reserved_name"),
    [
        ("yolo26-detection", UNTEXTED_LICENCE_SNAPSHOT),
        ("dai-recordgold-atr", UNDECLARED_LICENCE_SNAPSHOT),
    ],
)
def test_synthetic_licence_observation_never_overwrites_repository_bytes(
    tmp_path, artifact, reserved_name
):
    requirement = next(item for item in REQUIRED_ARTIFACTS if item.artifact == artifact)
    metadata = ""
    if requirement.license_declaration is not None:
        metadata = f"license: {requirement.license_declaration}\n"
    (tmp_path / "README.md").write_text(f"---\n{metadata}---\n", encoding="utf-8")
    reserved = tmp_path / reserved_name
    reserved.write_bytes(b"upstream repository bytes")

    with pytest.raises(DigestMismatchRefusal, match="upstream bytes are never overwritten"):
        model_store._snapshot_licence(tmp_path, requirement)

    assert reserved.read_bytes() == b"upstream repository bytes"


def test_declared_and_undeclared_synthetic_licence_records_cannot_be_swapped(tmp_path):
    record = _store(tmp_path)
    yolo = next(item for item in record["artifacts"] if item["artifact"] == "yolo26-detection")
    yolo["license"] = UNDECLARED_LICENCE_SNAPSHOT
    yolo["required_files"] = [UNDECLARED_LICENCE_SNAPSHOT, "model.safetensors"]

    with pytest.raises(DigestMismatchRefusal, match="must be 'LICENSE-DECLARED-WITHOUT-TEXT.txt'"):
        derived_inventory(record)


def test_declared_without_text_record_keeps_its_model_card_required(tmp_path):
    record = _store(tmp_path)
    yolo = next(item for item in record["artifacts"] if item["artifact"] == "yolo26-detection")
    yolo["license"] = UNTEXTED_LICENCE_SNAPSHOT
    yolo["required_files"] = [UNTEXTED_LICENCE_SNAPSHOT, "model.safetensors"]

    with pytest.raises(DigestMismatchRefusal, match="README.md.*required file"):
        derived_inventory(record)


def test_undeclared_record_keeps_the_model_card_that_proves_absence_required(tmp_path):
    record = _store(tmp_path)
    dai = next(item for item in record["artifacts"] if item["artifact"] == "dai-recordgold-atr")
    dai["license"] = UNDECLARED_LICENCE_SNAPSHOT
    dai["required_files"] = [
        UNDECLARED_LICENCE_SNAPSHOT,
        "model.safetensors",
        "query.txt",
        "system.txt",
    ]

    with pytest.raises(DigestMismatchRefusal, match="README.md.*required file"):
        derived_inventory(record)


def test_store_rechecks_synthetic_licence_text_against_roster_policy(tmp_path):
    record = _store(tmp_path)
    dai = next(item for item in record["artifacts"] if item["artifact"] == "dai-recordgold-atr")
    snapshot = tmp_path / dai["snapshot"]
    (snapshot / "LICENSE").unlink()
    (snapshot / UNDECLARED_LICENCE_SNAPSHOT).write_text(
        "a different claim about the pinned repository\n", encoding="utf-8"
    )
    (snapshot / "README.md").write_text("---\nbase_model: example/base\n---\n", encoding="utf-8")
    dai["license"] = UNDECLARED_LICENCE_SNAPSHOT
    dai["required_files"] = sorted(
        {
            UNDECLARED_LICENCE_SNAPSHOT,
            "README.md",
            "model.safetensors",
            *(entry["path"] for entry in dai["carried"]),
        }
    )
    dai["digest_manifest"] = write_manifest(build_manifest(snapshot), tmp_path / dai["manifest"])
    write_download_record(record, tmp_path)

    with pytest.raises(DigestMismatchRefusal, match="does not match the pinned repository"):
        verify_store(tmp_path)


class _ShardedFetcher(_FakeMaterializationFetcher):
    """A repository that publishes its checkpoint as a shard index plus shards."""

    def __init__(self, *, drop: str | None = None) -> None:
        super().__init__()
        self.drop = drop

    def fetch(self, repo: str, revision: str, destination: Path) -> None:
        super().fetch(repo, revision, destination)
        (destination / "model.safetensors").unlink()
        shards = ("model-00001-of-00002.safetensors", "model-00002-of-00002.safetensors")
        (destination / "model.safetensors.index.json").write_text(
            json.dumps({"weight_map": {f"layer.{n}": name for n, name in enumerate(shards)}}),
            encoding="utf-8",
        )
        for name in shards:
            if name == self.drop:
                continue
            (destination / name).write_bytes(f"{name} of {repo}@{revision}".encode())


def test_a_fetch_that_stops_short_of_its_shard_index_is_refused_not_measured(tmp_path):
    """A short fetch must not become the pin that later verifications agree with.

    The manifest of a first materialization is derived from the bytes that
    landed rather than checked against a pin, so a fetch that ends early is
    otherwise measured, recorded `present`, reported `complete`, and agrees with
    itself at every later verification. A sharded repository states its own
    completeness in `weight_map`, and that is inside the pinned revision.
    """

    fetcher = _ShardedFetcher(drop="model-00002-of-00002.safetensors")

    with pytest.raises(DigestMismatchRefusal, match="the fetch is incomplete"):
        materialize_real_roster(tmp_path, fetcher, capacity=dict(_MATERIALIZATION_CAPACITY))

    assert not (tmp_path / "hf").exists()
    assert sorted((tmp_path / "staging").iterdir()) == []


def test_shard_index_refuses_a_non_text_path_instead_of_coercing_it(tmp_path):
    (tmp_path / "model.safetensors.index.json").write_text(
        json.dumps({"weight_map": {"layer": 7}}), encoding="utf-8"
    )
    (tmp_path / "7").write_bytes(b"not a valid shard name")

    with pytest.raises(DigestMismatchRefusal, match="nonblank relative POSIX paths"):
        model_store._indexed_shards(tmp_path, "fixture-artifact")


def test_shard_index_read_is_bounded_before_json_deserialization(tmp_path, monkeypatch):
    monkeypatch.setattr(model_store, "MAX_SHARD_INDEX_BYTES", 32)
    (tmp_path / "model.safetensors.index.json").write_bytes(b"{" + b"x" * 32)

    with pytest.raises(DigestMismatchRefusal, match="32-byte control-artifact limit"):
        model_store._indexed_shards(tmp_path, "fixture-artifact")


def test_shard_index_refuses_parent_traversal_inside_the_named_taxonomy(tmp_path):
    snapshot = tmp_path / "snapshot"
    snapshot.mkdir()
    (tmp_path / "outside.safetensors").write_bytes(b"outside")
    (snapshot / "model.safetensors.index.json").write_text(
        json.dumps({"weight_map": {"layer": "../outside.safetensors"}}), encoding="utf-8"
    )

    with pytest.raises(DigestMismatchRefusal, match="unsafe shard paths"):
        model_store._indexed_shards(snapshot, "fixture-artifact")


def test_a_complete_sharded_fetch_keeps_reconciling_after_the_boot_that_made_it(tmp_path):
    """The index and its shards are required files, so the check outlives the fetch."""

    materialize_real_roster(tmp_path, _ShardedFetcher(), capacity=dict(_MATERIALIZATION_CAPACITY))

    record = load_download_record(tmp_path)
    entry = next(item for item in record["artifacts"] if item["artifact"] == "churro-3B")
    assert "model.safetensors.index.json" in entry["required_files"]
    assert "model-00002-of-00002.safetensors" in entry["required_files"]
    (tmp_path / entry["snapshot"] / "model-00002-of-00002.safetensors").unlink()

    with pytest.raises(DigestMismatchRefusal, match="model-00002-of-00002.safetensors"):
        verify_store(tmp_path)


def test_the_real_roster_carries_the_licence_notes_it_was_drafted_with():
    """A licence note is Tyrel's acceptance, and a copy of it is not a paraphrase.

    `config/models.toml` holds the drafted real roster commented out, one
    `license_note` per row recording what that repository licenses and that it
    was accepted under the research track on 2026-08-20.
    `config/models-real.toml` is that roster made selectable, so its notes must
    be those notes and not a session's rewording of them.
    """

    drafted = _commented_licence_notes(ROOT / "config" / "models.toml")
    real = load_models_toml(ROOT / "config" / "models-real.toml")
    carried = {
        role: identity.license_note
        for role, identity in real.chairs.items()
        if isinstance(identity, ChairIdentity)
    }

    # `_commented_licence_notes` reads a fixed comment shape. Reflowing or
    # reindenting that block used to make this fail with `KeyError: 'perlector'`
    # below -- a missing dictionary key in a licence test, with nothing pointing
    # at comment formatting in a config file, and the comparison the docstring is
    # about never running at all.
    unparsed = sorted(set(carried) - set(drafted))
    assert not unparsed, (
        f"no commented `license_note` was parsed for {unparsed}; the drafted roster in "
        "config/models.toml no longer matches the comment shape this test reads, so the "
        "notes were not compared"
    )
    assert carried == {role: drafted[role] for role in carried}
    assert len(carried) == 6


def test_the_store_agrees_with_the_roster_about_which_repository_declares_nothing():
    """The store's `license_declaration` and the roster's note are one fact.

    The store column decides which sentinel a fetch without a licence file
    writes; the roster note is what Tyrel accepted. If they disagree the store
    records a licence position nobody took, so the disagreement is caught here
    rather than at a pod launch.
    """

    real = load_models_toml(ROOT / "config" / "models-real.toml")
    for requirement in REQUIRED_ARTIFACTS:
        if requirement.source != "huggingface":
            continue
        note = real.chairs[requirement.chair].license_note.lower()
        declares_nothing = "no licence declared" in note
        assert declares_nothing == (requirement.license_declaration is None), requirement.chair


def _commented_licence_notes(path: Path) -> dict[str, str]:
    """The `license_note` of each chair in a roster that is commented out."""

    notes: dict[str, str] = {}
    role = None
    for line in path.read_text(encoding="utf-8").splitlines():
        chair = re.fullmatch(r"# \[chairs\.(\w+)\]", line)
        if chair:
            role = chair.group(1)
            continue
        note = re.fullmatch(r'# license_note = "(.*)"', line)
        if note and role is not None:
            notes[role] = note.group(1)
    return notes


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
        "perlector": "hf/qwen3.8-27B",
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
    entry = next(item for item in record["artifacts"] if item["artifact"] == "qwen3.8-27B")
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


def test_store_refuses_a_licence_snapshot_with_no_text(tmp_path):
    """The pinned evidence is licence text, not an empty file with the right name."""

    record = _store(tmp_path)
    entry = next(item for item in record["artifacts"] if item["artifact"] == "churro-3B")
    snapshot = tmp_path / entry["snapshot"]
    (snapshot / "LICENSE").write_bytes(b"")
    entry["digest_manifest"] = write_manifest(
        build_manifest(snapshot), tmp_path / entry["manifest"]
    )
    write_download_record(record, tmp_path)

    with pytest.raises(DigestMismatchRefusal, match="licence text is the artifact"):
        verify_store(tmp_path)


def test_store_names_a_licence_missing_from_its_manifest_as_the_licence(tmp_path):
    """The licence-specific refusal fires, not the generic required-file sweep."""

    record = _store(tmp_path)
    entry = next(item for item in record["artifacts"] if item["artifact"] == "churro-3B")
    snapshot = tmp_path / entry["snapshot"]
    (snapshot / "LICENSE").unlink()
    entry["digest_manifest"] = write_manifest(
        build_manifest(snapshot), tmp_path / entry["manifest"]
    )
    write_download_record(record, tmp_path)

    with pytest.raises(DigestMismatchRefusal, match="license snapshot is absent"):
        verify_store(tmp_path)


@pytest.mark.skipif(hasattr(os, "geteuid") and os.geteuid() == 0, reason="root ignores file modes")
def test_publication_into_a_read_only_store_refuses_inside_the_taxonomy(tmp_path):
    """Write failures must remain inside the complete public refusal taxonomy."""

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


def test_the_ad_hoc_download_record_refusal_names_what_the_v1_record_needs(tmp_path):
    """The host store's real record is the old download script's repo-keyed shape.

    Migrating it is a host action that happens against these refusals and
    nothing else, so each one has to say what is wrong rather than that
    something is.
    """

    ad_hoc = {
        "datalab-to/chandra-ocr-2": {"revision": "af93b47", "path": "chandra"},
        "Qwen/Qwen3.8-27B": {"revision": "1d4bf0f", "path": "qwen"},
    }
    (tmp_path / "download_record.json").write_bytes(canonical_bytes(ad_hoc))

    with pytest.raises(DigestMismatchRefusal) as refusal:
        load_download_record(tmp_path)

    message = str(refusal.value)
    assert "missing=['artifacts', 'capacity', 'layout', 'schema']" in message
    assert "unexpected=['Qwen/Qwen3.8-27B', 'datalab-to/chandra-ocr-2']" in message


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
    """The record's named-root layout constrains both snapshots and manifests."""

    record = _store(tmp_path)
    entry = next(item for item in record["artifacts"] if item["artifact"] == "churro-3B")
    stray = tmp_path / "hf" / "churro-3B-manifest.json"
    stray.write_bytes((tmp_path / entry["manifest"]).read_bytes())
    entry["manifest"] = "hf/churro-3B-manifest.json"

    with pytest.raises(DigestMismatchRefusal, match="artifact-keyed path"):
        derived_inventory(record)


def test_artifact_cannot_claim_another_artifacts_verified_snapshot(tmp_path):
    record = _store(tmp_path)
    chandra = next(item for item in record["artifacts"] if item["artifact"] == "chandra-ocr-2")
    qwen = next(item for item in record["artifacts"] if item["artifact"] == "qwen3.8-27B")
    qwen["snapshot"] = chandra["snapshot"]
    qwen["manifest"] = chandra["manifest"]
    qwen["digest_manifest"] = chandra["digest_manifest"]

    with pytest.raises(DigestMismatchRefusal, match="artifact-keyed path 'hf/qwen3.8-27B'"):
        derived_inventory(record)


def test_exported_never_required_policy_cannot_be_mutated():
    with pytest.raises(TypeError):
        SURYA_OCR_2_REFUSAL["state"] = "pending-fetch"


def test_store_refuses_a_required_weight_absent_from_a_rewritten_manifest(tmp_path):
    """A config-only snapshot cannot define its own smaller meaning of complete."""

    record = _store(tmp_path)
    entry = next(item for item in record["artifacts"] if item["artifact"] == "qwen3.8-27B")
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


def test_a_fetcher_that_leaves_client_state_behind_is_refused_not_measured(tmp_path):
    """The store checks the fetcher's contract rather than trusting it.

    `HuggingFaceMaterializationFetcher` removes what its client writes, but the
    cost of that being wrong is a pin no second fetch can reproduce, published
    into the config through a reviewed edit that has no way to see the problem.
    So the measurement refuses it too.
    """

    class _LeavesClientState(_FakeMaterializationFetcher):
        def fetch(self, repo: str, revision: str, destination: Path) -> None:
            super().fetch(repo, revision, destination)
            cache = destination / ".cache" / "huggingface" / "download"
            cache.mkdir(parents=True)
            (cache / "model.safetensors.metadata").write_text("commit\netag\n1787288876.2\n")

    with pytest.raises(DigestMismatchRefusal, match="client bookkeeping"):
        materialize_real_roster(
            tmp_path, _LeavesClientState(), capacity=dict(_MATERIALIZATION_CAPACITY)
        )
    assert not (tmp_path / "hf").exists()


def test_repository_owned_cache_path_is_manifested_not_deleted_or_called_client_state(tmp_path):
    class _RepositoryCacheFile(_FakeMaterializationFetcher):
        def fetch(self, repo: str, revision: str, destination: Path) -> None:
            super().fetch(repo, revision, destination)
            cache = destination / ".cache"
            cache.mkdir()
            (cache / "repository-owned.json").write_text("pinned bytes", encoding="utf-8")

    materialize_real_roster(
        tmp_path, _RepositoryCacheFile(), capacity=dict(_MATERIALIZATION_CAPACITY)
    )

    record = load_download_record(tmp_path)
    for entry in record["artifacts"]:
        if entry["state"] != "present":
            continue
        assert (tmp_path / entry["snapshot"] / ".cache/repository-owned.json").is_file()
