"""Synthetic-store tests for model acquisition; no network or real weights are used."""

import copy
import hashlib
import json
import os
import shutil
import threading
from concurrent.futures import ThreadPoolExecutor, TimeoutError
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

from common.chairs import model_store
from common.chairs.config import load_models_toml, parse_models_config
from common.chairs.errors import DigestMismatchRefusal
from common.chairs.manifests import build_manifest, read_manifest, write_manifest
from common.chairs.model_store import (
    DAI_PROMPT_CITATION,
    LICENCE_FILE_NAMES,
    REQUIRED_ARTIFACTS,
    STORE_SCHEMA,
    SURYA_OCR_2_REFUSAL,
    SYNTHETIC_LICENCE_SNAPSHOTS,
    UNDECLARED_LICENCE_SNAPSHOT,
    UNTEXTED_LICENCE_SNAPSHOT,
    StoreRoleFetcher,
    derived_inventory,
    load_download_record,
    materialize_real_roster,
    pending_local_artifacts,
    promote_verified_snapshot,
    verify_store,
    write_download_record,
)
from common.chairs.models import ChairIdentity
from common.chairs.registry import CACHE_DESCRIPTOR, ChairRegistry
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
        carried = []
        if requirement.source == "local-repository":
            # The miniature bundle `_fake_bundle_pin` pins.
            _write_fake_bundle(root)
            manifest_path = tmp_path / "manifests" / f"{requirement.artifact}.json"
            measured = build_manifest(root)
            artifacts[requirement.artifact] = {
                "artifact": requirement.artifact,
                "state": "present",
                "source": requirement.source,
                "repo": None,
                "revision": None,
                "snapshot": root.relative_to(tmp_path).as_posix(),
                "manifest": manifest_path.relative_to(tmp_path).as_posix(),
                "digest_manifest": write_manifest(measured, manifest_path),
                "license": requirement.license_file,
                "carried": [],
                "required_files": [row.path for row in measured.rows],
            }
            continue
        (root / "config.json").write_text('{"fixture":true}', encoding="utf-8")
        (root / "LICENSE").write_text(f"license for {requirement.artifact}\n", encoding="utf-8")
        (root / "model.safetensors").write_bytes(f"weights for {requirement.artifact}\n".encode())
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


def test_store_refuses_a_pinned_licence_whose_bytes_are_gone(tmp_path):
    # The manifest still pins LICENSE; only the snapshot's bytes vanished, so
    # this is byte-verification failing, not record validation.
    record = _store(tmp_path)
    entry = next(item for item in record["artifacts"] if item["artifact"] == "chandra-ocr-2")
    snapshot = tmp_path / entry["snapshot"]
    (snapshot / "LICENSE").unlink()

    with pytest.raises(DigestMismatchRefusal, match="LICENSE"):
        verify_store(tmp_path)


# --- publish-once custody (evidence is never overwritten) -----


def test_publication_reuses_identical_bytes_silently(tmp_path):
    path = tmp_path / "evidence.json"

    model_store._publish_once(path, b"evidence", chair="model-store", label="evidence")
    model_store._publish_once(path, b"evidence", chair="model-store", label="evidence")

    assert path.read_bytes() == b"evidence"


def test_publication_refuses_a_differing_republish_and_leaves_the_file(tmp_path):
    path = tmp_path / "evidence.json"
    model_store._publish_once(path, b"evidence", chair="model-store", label="evidence")

    with pytest.raises(DigestMismatchRefusal, match="already exists with different bytes"):
        model_store._publish_once(path, b"other", chair="model-store", label="evidence")
    assert path.read_bytes() == b"evidence"


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


# --- a canonical writer, never hand-authored JSON ----------------------------


def test_write_download_record_round_trips_through_load_download_record(tmp_path):
    """`_store` is the one host-record fixture, and it writes through this writer.

    This body was a verbatim second copy of `_store`. One fixture means a record shape that
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
    _write_fake_bundle(snapshot)
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


@pytest.mark.hostile_local
def test_active_record_swap_does_not_rewrite_its_immutable_version(tmp_path):
    record = _store(tmp_path)
    original_bytes = canonical_bytes(record)
    original_digest = hashlib.sha256(original_bytes).hexdigest()
    archive = tmp_path / "records" / f"{original_digest}.json"
    swapped = copy.deepcopy(record)
    swapped["artifacts"][0]["required_files"] = sorted(
        [*swapped["artifacts"][0]["required_files"], "config.json"]
    )

    (tmp_path / "download_record.json").write_bytes(canonical_bytes(swapped))

    assert archive.read_bytes() == original_bytes
    with pytest.raises(DigestMismatchRefusal, match="immutable version"):
        load_download_record(tmp_path)


@pytest.mark.hostile_local
def test_active_record_symlink_is_not_accepted_as_in_store_custody(tmp_path):
    _store(tmp_path)
    active = tmp_path / "download_record.json"
    external = tmp_path.parent / f"{tmp_path.name}-active-record"
    external.write_bytes(active.read_bytes())
    active.unlink()
    active.symlink_to(external)

    with pytest.raises(DigestMismatchRefusal, match="regular in-store active copy"):
        load_download_record(tmp_path)


@pytest.mark.hostile_local
def test_active_record_fifo_is_refused_before_any_blocking_read(tmp_path):
    os.mkfifo(tmp_path / "download_record.json")

    with pytest.raises(DigestMismatchRefusal, match="regular in-store active copy"):
        load_download_record(tmp_path)


@pytest.mark.hostile_local
def test_immutable_record_version_cannot_hide_behind_an_internal_symlink(tmp_path):
    record = _store(tmp_path)
    digest = digest_bytes(canonical_bytes(record))
    archive = tmp_path / "records" / f"{digest}.json"
    backing = archive.with_name("mutable-backing.json")
    archive.replace(backing)
    archive.symlink_to(backing.name)

    with pytest.raises(DigestMismatchRefusal, match="must not traverse symlink component"):
        load_download_record(tmp_path)


@pytest.mark.hostile_local
def test_verified_snapshot_root_cannot_hide_behind_an_internal_symlink(tmp_path):
    record = _store(tmp_path)
    entry = next(item for item in record["artifacts"] if item["artifact"] == "churro-3B")
    snapshot = tmp_path / entry["snapshot"]
    backing = snapshot.with_name("churro-3B-mutable-backing")
    snapshot.replace(backing)
    snapshot.symlink_to(backing.name, target_is_directory=True)

    with pytest.raises(DigestMismatchRefusal, match="must not traverse symlink component"):
        verify_store(tmp_path)


def test_writer_refuses_an_unsupported_active_record_without_archiving_it(tmp_path):
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

    with pytest.raises(DigestMismatchRefusal, match="unsupported schema None"):
        write_download_record(record, tmp_path)
    assert (tmp_path / "download_record.json").read_bytes() == legacy
    assert not (tmp_path / "records").exists()


def test_v1_active_record_refuses_writers_before_publication(tmp_path):
    record = _store(tmp_path)
    old = canonical_bytes({**record, "schema": "verbatus-model-store.v1"})
    active = tmp_path / "download_record.json"
    active.write_bytes(old)
    archives = set((tmp_path / "records").iterdir())

    with pytest.raises(DigestMismatchRefusal, match="move or remove the old download_record.json"):
        write_download_record(record, tmp_path)
    assert active.read_bytes() == old
    assert set((tmp_path / "records").iterdir()) == archives

    with pytest.raises(DigestMismatchRefusal, match="move or remove the old download_record.json"):
        materialize_real_roster(tmp_path, _FakeMaterializationFetcher(), _FakeBundleFetcher())
    assert active.read_bytes() == old
    assert set((tmp_path / "records").iterdir()) == archives


# --- symlink escape is refused in both directions ---------------------------


@pytest.mark.hostile_local
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


@pytest.mark.hostile_local
def test_materializer_refuses_a_staging_root_symlink_before_fetching_outside_store(tmp_path):
    store = tmp_path / "store"
    store.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (store / "staging").symlink_to(outside, target_is_directory=True)

    with pytest.raises(DigestMismatchRefusal, match="escapes configured root"):
        materialize_real_roster(store, _FakeMaterializationFetcher(), _FakeBundleFetcher())

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
        materialize_real_roster(store, _ReplacesDestination(), _FakeBundleFetcher())

    assert not (outside / UNTEXTED_LICENCE_SNAPSHOT).exists()
    assert sorted((store / "staging").iterdir()) == []


def test_materializer_preserves_fetch_failure_when_cleanup_fails(tmp_path, monkeypatch):
    class _FailsAfterWriting:
        def fetch(self, repo: str, revision: str, destination: Path) -> None:
            (destination / "partial.safetensors").write_bytes(b"partial")
            raise RuntimeError("fetch transport failed")

    def refuse_cleanup(path, **kwargs):
        raise PermissionError("cleanup denied")

    monkeypatch.setattr(model_store.shutil, "rmtree", refuse_cleanup)

    with pytest.raises(RuntimeError, match="fetch transport failed"):
        materialize_real_roster(tmp_path, _FailsAfterWriting(), _FakeBundleFetcher())


@pytest.mark.hostile_local
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

    # `_indexed_shards` reads through `read_limited_bytes`, so guarding that call
    # is what proves the symlinked index is never read.
    real_read_limited_bytes = model_store.read_limited_bytes

    def refuse_external_read(path, *args, **kwargs):
        if Path(path).resolve() == outside_index:
            raise AssertionError("the external shard index was read")
        return real_read_limited_bytes(path, *args, **kwargs)

    monkeypatch.setattr(model_store, "read_limited_bytes", refuse_external_read)

    with pytest.raises(DigestMismatchRefusal, match="symlink"):
        materialize_real_roster(tmp_path, _SymlinkedShardIndex(), _FakeBundleFetcher())


@pytest.mark.hostile_local
def test_materializer_refuses_a_hard_link_to_bytes_owned_outside_staging(tmp_path):
    outside = tmp_path / "outside-operator-file"
    outside.write_bytes(b"not repository evidence")
    store = tmp_path / "store"

    class _HardLinksExternalBytes:
        def fetch(self, repo: str, revision: str, destination: Path) -> None:
            del repo, revision
            os.link(outside, destination / "model.safetensors")

    with pytest.raises(DigestMismatchRefusal, match="hard-linked file"):
        materialize_real_roster(store, _HardLinksExternalBytes(), _FakeBundleFetcher())

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


def test_download_record_read_is_bounded_before_json_deserialization(tmp_path, monkeypatch):
    monkeypatch.setattr(model_store, "MAX_DOWNLOAD_RECORD_BYTES", 32)
    (tmp_path / "download_record.json").write_bytes(b"{" + b"x" * 32)

    with pytest.raises(DigestMismatchRefusal, match="32-byte control-artifact limit"):
        load_download_record(tmp_path)


@pytest.mark.hostile_local
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


def test_validate_record_refuses_a_four_artifact_record(tmp_path):
    record = _store(tmp_path)
    record["artifacts"] = record["artifacts"][:4]

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

    This is the store's real state today: four Hugging Face snapshots fetched,
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
    assert rows["designator_surya"]["state"] == "pending-fetch"
    assert rows["designator_surya"]["reason"] == "s3 bundle not yet fetched by the host"
    assert "snapshot" not in rows["designator_surya"]
    # The artifacts that did land are verified exactly as before.
    assert all(rows[chair]["state"] == "present" for chair in rows if chair != "designator_surya")
    assert inventory == derived_inventory(record)


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


def test_every_store_chair_is_a_models_toml_role():
    """A chair name the roster does not know cannot be joined to anything.

    The store exists to be bound to `config/models.toml` when the real roster is
    activated. If its chair names drift from the roster's role keys, the
    divergence surfaces on the rented card during pod assembly instead of here.
    """

    config = load_models_toml(ROOT / "config" / "models.toml")
    store_chairs = {item.chair for item in REQUIRED_ARTIFACTS}
    assert store_chairs, "meta-invariant 88: the roster policy is not empty"

    assert store_chairs <= set(config.chairs)


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


class _FakeBundleFetcher:
    """Surya's bundle in miniature: a lock, the layout licence and one payload."""

    def __init__(self) -> None:
        self.calls: list[str] = []
        self.checked: list[str] = []

    def check(self, artifact: str) -> None:
        self.checked.append(artifact)

    def fetch(self, artifact: str, destination: Path) -> None:
        self.calls.append(artifact)
        _write_fake_bundle(destination)


def _write_fake_bundle(destination: Path) -> None:
    (destination / "surya_layout2").mkdir(parents=True)
    (destination / "surya_layout2" / "LICENSE").write_text("layout licence\n", encoding="utf-8")
    (destination / "surya_layout2" / "README.md").write_text(
        "---\nlicense: openrail\nlicense_link: LICENSE\n---\n", encoding="utf-8"
    )
    (destination / "surya_layout2" / "rfdetr_layout.pth").write_bytes(b"layout weights\n")
    (destination / "surya-bundle.json").write_text('{"fixture": true}\n', encoding="utf-8")


@pytest.fixture(autouse=True)
def _fake_bundle_pin(tmp_path_factory):
    """Pin Surya's requirement to the miniature bundle, as the real one is pinned
    to the measured manifest of the real bundle. Its own patch, so a test that
    undoes its `monkeypatch` keeps this pin."""
    bundle = tmp_path_factory.mktemp("fake-bundle") / "snapshot"
    _write_fake_bundle(bundle)
    pin = digest_bytes(canonical_bytes(build_manifest(bundle).to_record()))
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(
            model_store,
            "REQUIRED_ARTIFACTS",
            tuple(
                replace(item, digest_manifest=pin) if item.source == "local-repository" else item
                for item in model_store.REQUIRED_ARTIFACTS
            ),
        )
        yield


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


def test_pod_materializer_fetches_each_real_pin_once_and_records_measured_evidence(tmp_path):
    fetcher = _FakeMaterializationFetcher()
    bundles = _FakeBundleFetcher()

    receipt = materialize_real_roster(tmp_path, fetcher, bundles)

    expected = {
        (item.repo, item.revision) for item in REQUIRED_ARTIFACTS if item.source == "huggingface"
    }
    assert sorted(fetcher.calls) == sorted(expected)
    assert bundles.calls == ["surya2-detection"]
    assert {row["artifact"] for row in receipt["artifacts"]} == {
        item.artifact for item in REQUIRED_ARTIFACTS
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
    surya = next(item for item in record["artifacts"] if item["artifact"] == "surya2-detection")
    assert (surya["state"], surya["snapshot"], surya["repo"], surya["revision"]) == (
        "present",
        "local/surya2-detection",
        None,
        None,
    )
    assert surya["license"] == "surya_layout2/LICENSE"
    assert surya["required_files"] == [
        "surya-bundle.json",
        "surya_layout2/LICENSE",
        "surya_layout2/README.md",
        "surya_layout2/rfdetr_layout.pth",
    ]
    pinned = next(
        item for item in model_store.REQUIRED_ARTIFACTS if item.artifact == "surya2-detection"
    )
    assert surya["digest_manifest"] == pinned.digest_manifest
    assert receipt["complete"] is True
    assert receipt["real_roster_complete"] is True
    assert list((tmp_path / "staging").iterdir()) == []


def test_a_bundle_that_measures_other_than_its_pin_is_refused_before_it_is_promoted(tmp_path):
    class _Drifted(_FakeBundleFetcher):
        def fetch(self, artifact: str, destination: Path) -> None:
            super().fetch(artifact, destination)
            (destination / "surya_layout2" / "rfdetr_layout.pth").write_bytes(b"changed\n")

    with pytest.raises(DigestMismatchRefusal, match="a new pin is a reviewed change"):
        materialize_real_roster(tmp_path, _FakeMaterializationFetcher(), _Drifted())

    record = load_download_record(tmp_path)
    surya = next(item for item in record["artifacts"] if item["artifact"] == "surya2-detection")
    assert surya["state"] == "pending-fetch"
    assert not (tmp_path / "local" / "surya2-detection").exists()
    assert not (tmp_path / "manifests" / "surya2-detection.json").exists()
    assert list((tmp_path / "staging").iterdir()) == []
    # Nothing was published, so a later fetch of the pinned bytes completes the store.
    receipt = materialize_real_roster(tmp_path, _FakeMaterializationFetcher(), _FakeBundleFetcher())
    assert receipt["complete"] is True


def test_a_bundle_without_its_licence_text_is_refused(tmp_path):
    class _NoLicence(_FakeBundleFetcher):
        def fetch(self, artifact: str, destination: Path) -> None:
            super().fetch(artifact, destination)
            (destination / "surya_layout2" / "LICENSE").unlink()

    with pytest.raises(DigestMismatchRefusal, match="no licence text"):
        materialize_real_roster(tmp_path, _FakeMaterializationFetcher(), _NoLicence())


def test_a_store_that_recorded_surya_pending_is_completed_by_the_next_launch(tmp_path):
    """A store that names the bundle pending-fetch is completed by the next launch,
    which fetches nothing else again."""
    fetcher = _FakeMaterializationFetcher()

    class _Unreachable(_FakeBundleFetcher):
        def fetch(self, artifact: str, destination: Path) -> None:
            raise OSError("model host unreachable")

    with pytest.raises(OSError, match="unreachable"):
        materialize_real_roster(tmp_path, fetcher, _Unreachable())
    record = load_download_record(tmp_path)
    surya = next(item for item in record["artifacts"] if item["artifact"] == "surya2-detection")
    assert surya["state"] == "pending-fetch"
    bundles = _FakeBundleFetcher()

    receipt = materialize_real_roster(tmp_path, fetcher, bundles)

    # Each Hub pin fetched once over both launches, the bundle once.
    assert sorted(fetcher.calls) == sorted(set(fetcher.calls))
    assert bundles.calls == ["surya2-detection"]
    assert receipt["real_roster_complete"] is True


def test_a_bundle_whose_card_declares_another_licence_is_refused(tmp_path):
    class _Relicensed(_FakeBundleFetcher):
        def fetch(self, artifact: str, destination: Path) -> None:
            super().fetch(artifact, destination)
            (destination / "surya_layout2" / "README.md").write_text(
                "---\nlicense: mit\n---\n", encoding="utf-8"
            )

    with pytest.raises(DigestMismatchRefusal, match="declares 'mit'.*expects 'openrail'"):
        materialize_real_roster(tmp_path, _FakeMaterializationFetcher(), _Relicensed())
    assert not (tmp_path / "manifests" / "surya2-detection.json").exists()


def test_a_bundle_fetcher_that_cannot_run_is_refused_before_anything_downloads(tmp_path):
    class _NoEnvironment(_FakeBundleFetcher):
        def check(self, artifact: str) -> None:
            raise RuntimeError(f"no environment to fetch {artifact}")

    fetcher = _FakeMaterializationFetcher()
    with pytest.raises(RuntimeError, match="no environment to fetch surya2-detection"):
        materialize_real_roster(tmp_path, fetcher, _NoEnvironment())
    assert fetcher.calls == []


def test_a_bundle_that_fails_to_fetch_leaves_the_hub_fetcher_uncalled(tmp_path):
    class _Unreachable(_FakeBundleFetcher):
        def fetch(self, artifact: str, destination: Path) -> None:
            raise OSError("model host unreachable")

    fetcher = _FakeMaterializationFetcher()
    with pytest.raises(OSError, match="model host unreachable"):
        materialize_real_roster(tmp_path, fetcher, _Unreachable())
    assert fetcher.calls == []


def test_a_complete_store_does_not_ask_the_bundle_fetcher_to_run(tmp_path):
    materialize_real_roster(tmp_path, _FakeMaterializationFetcher(), _FakeBundleFetcher())
    bundles = _FakeBundleFetcher()

    materialize_real_roster(tmp_path, _FakeMaterializationFetcher(), bundles)

    assert (bundles.checked, bundles.calls) == ([], [])


def test_pending_local_artifacts_names_what_the_next_launch_would_fetch(tmp_path):
    assert pending_local_artifacts(tmp_path / "no-store-yet") == ("surya2-detection",)

    class _Unreachable(_FakeBundleFetcher):
        def fetch(self, artifact: str, destination: Path) -> None:
            raise OSError("model host unreachable")

    with pytest.raises(OSError):
        materialize_real_roster(tmp_path, _FakeMaterializationFetcher(), _Unreachable())
    assert pending_local_artifacts(tmp_path) == ("surya2-detection",)
    materialize_real_roster(tmp_path, _FakeMaterializationFetcher(), _FakeBundleFetcher())
    assert pending_local_artifacts(tmp_path) == ()


def test_a_present_bundle_at_another_pin_is_refused_by_name(tmp_path, monkeypatch):
    """A store that fetched the bundle at an earlier pin is not complete under a new one."""
    materialize_real_roster(tmp_path, _FakeMaterializationFetcher(), _FakeBundleFetcher())
    monkeypatch.setattr(
        model_store,
        "REQUIRED_ARTIFACTS",
        tuple(
            replace(item, digest_manifest="1" * 64) if item.source == "local-repository" else item
            for item in model_store.REQUIRED_ARTIFACTS
        ),
    )

    for check in (verify_store, pending_local_artifacts):
        with pytest.raises(DigestMismatchRefusal, match="surya2-detection.*fresh store"):
            check(tmp_path)
    bundles = _FakeBundleFetcher()
    with pytest.raises(DigestMismatchRefusal, match="not the pinned 1111"):
        materialize_real_roster(tmp_path, _FakeMaterializationFetcher(), bundles)
    assert bundles.calls == []


def test_a_store_written_before_surya_joined_the_roster_is_upgraded_then_fetched(
    tmp_path, monkeypatch
):
    earlier = tuple(
        item for item in model_store.REQUIRED_ARTIFACTS if item.chair != "designator_surya"
    )
    fetcher = _FakeMaterializationFetcher()
    with monkeypatch.context() as patch:
        patch.setattr(model_store, "REQUIRED_ARTIFACTS", earlier)
        materialize_real_roster(tmp_path, fetcher, _FakeBundleFetcher())
    earlier_bytes = (tmp_path / "download_record.json").read_bytes()
    assert "surya2-detection" not in earlier_bytes.decode("utf-8")
    calls = list(fetcher.calls)
    bundles = _FakeBundleFetcher()

    receipt = materialize_real_roster(tmp_path, fetcher, bundles)

    assert fetcher.calls == calls
    assert bundles.calls == ["surya2-detection"]
    assert receipt["real_roster_complete"] is True
    versions = [json.loads(path.read_bytes()) for path in (tmp_path / "records").glob("*.json")]
    assert any(
        {item["artifact"]: item["state"] for item in version["artifacts"]}.get("surya2-detection")
        == "pending-fetch"
        for version in versions
    )


def test_pod_materializer_reuses_verified_present_snapshots_without_refetching(tmp_path):
    fetcher = _FakeMaterializationFetcher()
    materialize_real_roster(tmp_path, fetcher, _FakeBundleFetcher())
    calls = list(fetcher.calls)

    materialize_real_roster(tmp_path, fetcher, _FakeBundleFetcher())

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
        materialize_real_roster(tmp_path, _FakeMaterializationFetcher(), _FakeBundleFetcher())


def _materialized_before_the_detector_joined(tmp_path, monkeypatch):
    """A real store written while the roster did not yet require the record detector."""
    earlier = tuple(
        item for item in model_store.REQUIRED_ARTIFACTS if item.chair != "secondary_proposer"
    )
    fetcher = _FakeMaterializationFetcher()
    with monkeypatch.context() as patch:
        patch.setattr(model_store, "REQUIRED_ARTIFACTS", earlier)
        materialize_real_roster(tmp_path, fetcher, _FakeBundleFetcher())
    return fetcher


def test_a_store_written_before_an_artifact_joined_the_roster_is_upgraded_then_fetched(
    tmp_path, monkeypatch
):
    fetcher = _materialized_before_the_detector_joined(tmp_path, monkeypatch)
    earlier_bytes = (tmp_path / "download_record.json").read_bytes()
    with pytest.raises(DigestMismatchRefusal, match="exactly 6 unique roster"):
        load_download_record(tmp_path)
    calls = list(fetcher.calls)

    receipt = materialize_real_roster(tmp_path, fetcher, _FakeBundleFetcher())

    detector = next(item for item in REQUIRED_ARTIFACTS if item.chair == "secondary_proposer")
    assert fetcher.calls == [*calls, (detector.repo, detector.revision)]
    assert receipt["real_roster_complete"] is True
    record = load_download_record(tmp_path)
    [entry] = [item for item in record["artifacts"] if item["artifact"] == detector.artifact]
    assert entry["state"] == "present"
    # Every version stays: the earlier record, and the one that added the artifact pending.
    versions = {path.read_bytes() for path in (tmp_path / "records").glob("*.json")}
    assert earlier_bytes in versions
    assert any(
        {item["artifact"]: item["state"] for item in json.loads(version)["artifacts"]}.get(
            detector.artifact
        )
        == "pending-fetch"
        for version in versions
    )


def test_an_older_store_whose_entries_left_the_roster_is_not_upgraded(tmp_path, monkeypatch):
    fetcher = _materialized_before_the_detector_joined(tmp_path, monkeypatch)
    record = json.loads((tmp_path / "download_record.json").read_bytes())
    entry = next(item for item in record["artifacts"] if item["artifact"] == "churro-3B")
    entry["revision"] = "1" * 40
    payload = canonical_bytes(record)
    (tmp_path / "download_record.json").write_bytes(payload)
    (tmp_path / "records" / f"{digest_bytes(payload)}.json").write_bytes(payload)
    calls = list(fetcher.calls)

    with pytest.raises(DigestMismatchRefusal, match="diverges from roster policy"):
        materialize_real_roster(tmp_path, fetcher, _FakeBundleFetcher())
    assert fetcher.calls == calls
    assert (tmp_path / "download_record.json").read_bytes() == payload


def test_materializer_clears_leftover_staging_before_fetch(tmp_path):
    staging = tmp_path / "staging"
    staging.mkdir()
    (staging / ".abandoned.fetch-leftover").mkdir()

    receipt = materialize_real_roster(tmp_path, _FakeMaterializationFetcher(), _FakeBundleFetcher())

    assert receipt["unattributed_staging_entries"] == []
    assert list(staging.iterdir()) == []


def test_materializer_waits_for_store_lock_before_sweeping_staging(tmp_path):
    staging = tmp_path / "staging"
    staging.mkdir()
    live = staging / ".other-materializer.fetch-live"
    live.mkdir()
    started = threading.Event()

    def second_writer():
        started.set()
        return materialize_real_roster(
            tmp_path, _FakeMaterializationFetcher(), _FakeBundleFetcher()
        )

    with ThreadPoolExecutor(max_workers=1) as workers:
        with model_store._materialization_lock(tmp_path):
            future = workers.submit(second_writer)
            assert started.wait(timeout=5)
            with pytest.raises(TimeoutError):
                future.result(timeout=0.2)
            assert live.is_dir()
        receipt = future.result(timeout=10)

    assert receipt["unattributed_staging_entries"] == []
    assert list(staging.iterdir()) == []


@pytest.mark.parametrize(
    ("stage", "message"),
    [
        ("mkdir", "cannot create materialization root"),
        ("open", "cannot open materialization lock"),
        ("flock", "cannot acquire materialization lock"),
    ],
)
def test_materializer_names_store_lock_setup_failure(tmp_path, monkeypatch, stage, message):
    root = tmp_path / "store"

    def denied(*args, **kwargs):
        raise OSError("fixture access denied")

    with monkeypatch.context() as patch:
        if stage == "mkdir":
            patch.setattr(model_store.Path, "mkdir", denied)
        elif stage == "open":
            patch.setattr(model_store.Path, "open", denied)
        else:
            patch.setattr(model_store.fcntl, "flock", denied)
        with pytest.raises(DigestMismatchRefusal, match=message) as caught:
            materialize_real_roster(root, _FakeMaterializationFetcher(), _FakeBundleFetcher())

    assert str(root) in str(caught.value)
    assert isinstance(caught.value.__cause__, OSError)


def test_materializer_refuses_store_lock_after_bounded_wait(tmp_path, monkeypatch):
    root = tmp_path / "store"

    def busy(*args, **kwargs):
        assert args[1] & model_store.fcntl.LOCK_NB
        raise BlockingIOError("fixture lock held")

    monkeypatch.setattr(model_store.fcntl, "flock", busy)
    monkeypatch.setattr(model_store, "MATERIALIZATION_LOCK_TIMEOUT_SECONDS", 0)

    with pytest.raises(DigestMismatchRefusal, match="timed out acquiring") as caught:
        materialize_real_roster(root, _FakeMaterializationFetcher(), _FakeBundleFetcher())

    assert str(root) in str(caught.value)
    assert isinstance(caught.value.__cause__, BlockingIOError)


def test_a_second_pod_waits_out_another_pod_s_store_work_and_says_so(tmp_path, monkeypatch, capsys):
    """A pod booting while another holds the store lock through a seven-minute check waits,
    saying so each minute, rather than refusing after one."""
    assert model_store.MATERIALIZATION_LOCK_TIMEOUT_SECONDS >= 15 * 60
    clock = [0.0]
    attempts = []

    def flock(fd, operation):
        if operation & model_store.fcntl.LOCK_UN:
            return
        attempts.append(clock[0])
        if clock[0] < 7 * 60:
            raise BlockingIOError("held by the other pod")

    def sleep(seconds):
        clock[0] += 30

    monkeypatch.setattr(model_store.fcntl, "flock", flock)
    monkeypatch.setattr(
        model_store, "time", SimpleNamespace(monotonic=lambda: clock[0], sleep=sleep)
    )

    with model_store._materialization_lock(tmp_path / "store"):
        pass

    assert attempts[-1] == 7 * 60
    waits = [line for line in capsys.readouterr().err.splitlines() if "waiting" in line]
    assert len(waits) == 6
    assert waits[0].startswith("model-store: waiting 60 s of 1200 s for the materialization lock")


def test_materializer_clears_staging_left_after_failed_cleanup_on_next_fetch(tmp_path, monkeypatch):
    class FailingFetcher:
        def fetch(self, repo: str, revision: str, destination: Path) -> None:
            (destination / "partial.safetensors").write_bytes(b"partial")
            raise RuntimeError("fetch transport failed")

    real_rmtree = model_store.shutil.rmtree

    def refuse_cleanup(path, **kwargs):
        raise PermissionError("cleanup denied")

    monkeypatch.setattr(model_store.shutil, "rmtree", refuse_cleanup)
    with pytest.raises(RuntimeError, match="fetch transport failed"):
        materialize_real_roster(tmp_path, FailingFetcher(), _FakeBundleFetcher())
    assert list((tmp_path / "staging").iterdir())

    monkeypatch.setattr(model_store.shutil, "rmtree", real_rmtree)
    receipt = materialize_real_roster(tmp_path, _FakeMaterializationFetcher(), _FakeBundleFetcher())
    assert receipt["unattributed_staging_entries"] == []
    assert list((tmp_path / "staging").iterdir()) == []


def test_a_second_boot_verifies_the_whole_store_once_not_once_per_artifact(tmp_path, monkeypatch):
    """A populated boot re-verifies once because each call hashes the whole store."""

    fetcher = _FakeMaterializationFetcher()
    materialize_real_roster(tmp_path, fetcher, _FakeBundleFetcher())
    present = {item["artifact"] for item in load_download_record(tmp_path)["artifacts"]}
    assert len(present) == 6

    calls = []
    real = model_store.verify_store
    monkeypatch.setattr(
        model_store,
        "verify_store",
        lambda root, **kwargs: (calls.append(root), real(root, **kwargs))[1],
    )
    receipt = materialize_real_roster(tmp_path, fetcher, _FakeBundleFetcher())

    assert len(calls) == 1
    assert {row["artifact"] for row in receipt["artifacts"]} == present


def _hashed_artifacts(monkeypatch):
    """Which store snapshots `verify_store` read byte for byte, by artifact."""
    hashed: list[str] = []
    real = model_store.verify_snapshot

    def record(identity, snapshot, manifest, **kwargs):  # type: ignore[no-untyped-def]
        hashed.append(identity.role)
        return real(identity, snapshot, manifest, **kwargs)

    monkeypatch.setattr(model_store, "verify_snapshot", record)
    return hashed


def test_a_boot_whose_copies_hash_the_bytes_checks_the_store_structure_only(tmp_path, monkeypatch):
    fetcher = _FakeMaterializationFetcher()
    materialize_real_roster(tmp_path, fetcher, _FakeBundleFetcher())
    hashed = _hashed_artifacts(monkeypatch)

    receipt = materialize_real_roster(
        tmp_path,
        fetcher,
        _FakeBundleFetcher(),
        hashed_at_copy=("perlector", "reconstructor", "attestator_1"),
    )

    assert sorted(hashed) == [
        "churro-3B",
        "dai-recordgold-atr",
        "surya2-detection",
        "yolov26-record-detection",
    ]
    assert receipt["real_roster_complete"] is True
    assert receipt["store_bytes"]["hashed_at_copy"] == {
        "artifacts": ["chandra-ocr-2", "qwen3.8-27B"],
        "roles": ["attestator_1", "perlector", "reconstructor"],
    }
    assert receipt["store_bytes"]["hashed_at_fetch"] == []
    assert receipt["store_bytes"]["statement"].startswith(
        "bytes verified at copy, for roles attestator_1, perlector, reconstructor:"
    )


def test_structure_only_still_refuses_a_missing_or_resized_store_file(tmp_path):
    record = _store(tmp_path)
    entry = next(item for item in record["artifacts"] if item["artifact"] == "qwen3.8-27B")
    weights = tmp_path / entry["snapshot"] / "model.safetensors"
    data = weights.read_bytes()
    weights.write_bytes(data[:-2] + b"X\n")
    verify_store(tmp_path, bytes_hashed_elsewhere=("qwen3.8-27B",))

    with pytest.raises(DigestMismatchRefusal, match="model.safetensors"):
        verify_store(tmp_path)
    weights.write_bytes(data + b"longer")
    with pytest.raises(DigestMismatchRefusal, match="model.safetensors: size"):
        verify_store(tmp_path, bytes_hashed_elsewhere=("qwen3.8-27B",))
    weights.unlink()
    with pytest.raises(DigestMismatchRefusal, match="model.safetensors"):
        verify_store(tmp_path, bytes_hashed_elsewhere=("qwen3.8-27B",))


def test_artifacts_fetched_in_this_call_are_not_hashed_a_second_time(tmp_path, monkeypatch):
    hashed = _hashed_artifacts(monkeypatch)

    receipt = materialize_real_roster(tmp_path, _FakeMaterializationFetcher(), _FakeBundleFetcher())

    assert hashed == []
    assert receipt["store_bytes"]["hashed_at_boot"] == []
    assert len(receipt["store_bytes"]["hashed_at_fetch"]) == 6
    assert "statement" not in receipt["store_bytes"]


SMALL_CARD_ROLES = (
    "attestator_1",
    "attestator_2",
    "attestator_3",
    "designator_surya",
    "secondary_proposer",
)
BIG_CARD_ROLES = ("perlector", "reconstructor")


def test_each_half_of_a_split_fetches_and_verifies_only_its_own_artifacts(tmp_path, monkeypatch):
    qwen = next(item for item in REQUIRED_ARTIFACTS if item.chair == "perlector")
    fetcher = _FakeMaterializationFetcher()

    small = materialize_real_roster(tmp_path, fetcher, _FakeBundleFetcher(), roles=SMALL_CARD_ROLES)

    assert (qwen.repo, qwen.revision) not in fetcher.calls
    assert small["selection_complete"] is True
    assert small["real_roster_complete"] is False
    assert "qwen3.8-27B" not in {row["artifact"] for row in small["artifacts"]}
    states = {
        item["artifact"]: item["state"] for item in load_download_record(tmp_path)["artifacts"]
    }
    assert states["qwen3.8-27B"] == "pending-fetch"

    fetcher.calls.clear()
    bundles = _FakeBundleFetcher()
    hashed = _hashed_artifacts(monkeypatch)
    big = materialize_real_roster(tmp_path, fetcher, bundles, roles=BIG_CARD_ROLES)

    assert fetcher.calls == [(qwen.repo, qwen.revision)]
    assert (bundles.checked, bundles.calls) == ([], [])
    assert hashed == []
    assert big["selection_complete"] is True
    assert big["real_roster_complete"] is False
    assert big["store_bytes"]["not_verified"] == sorted(
        {item.artifact for item in REQUIRED_ARTIFACTS} - {"qwen3.8-27B"}
    )

    whole = materialize_real_roster(tmp_path, fetcher, _FakeBundleFetcher())
    assert whole["selection"] is None
    assert whole["real_roster_complete"] is True
    assert whole["store_bytes"]["not_verified"] == []


def test_a_selection_whose_artifact_is_absent_is_not_complete(tmp_path):
    class _Unreachable(_FakeBundleFetcher):
        def fetch(self, artifact: str, destination: Path) -> None:
            raise OSError("model host unreachable")

    with pytest.raises(OSError):
        materialize_real_roster(
            tmp_path, _FakeMaterializationFetcher(), _Unreachable(), roles=SMALL_CARD_ROLES
        )
    # The big card does not need the bundle, so its half is complete without it.
    big = materialize_real_roster(
        tmp_path, _FakeMaterializationFetcher(), _Unreachable(), roles=BIG_CARD_ROLES
    )
    assert big["selection_complete"] is True
    assert big["complete"] is False


def test_materializer_receipt_digest_names_the_record_whole_store_verification_checked(
    tmp_path, monkeypatch
):
    """A concurrent valid active-record update cannot relabel verified evidence."""

    fetcher = _FakeMaterializationFetcher()
    materialize_real_roster(tmp_path, fetcher, _FakeBundleFetcher())
    verify = model_store.verify_store
    observed: dict[str, str] = {}

    def verify_then_advance_active_record(root, **kwargs):  # type: ignore[no-untyped-def]
        inventory = verify(root, **kwargs)
        observed["verified"] = inventory["download_record_sha256"]
        replacement = load_download_record(root)
        # Every artifact is present, so the valid change is the entries' order.
        replacement["artifacts"].reverse()
        observed["advanced"] = write_download_record(replacement, root)
        return inventory

    monkeypatch.setattr(model_store, "verify_store", verify_then_advance_active_record)

    receipt = materialize_real_roster(tmp_path, fetcher, _FakeBundleFetcher())

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
            ["manifests/chandra-ocr-2.json"],
        ),
        # Between moving the first artifact's snapshot into place and recording
        # it present — the second record write, the first being the all-pending
        # record this store opened with: a full snapshot the record denies.
        (
            "write_download_record",
            2,
            ["local/surya2-detection", "manifests/surya2-detection.json"],
        ),
    ],
)
def test_a_boot_killed_mid_materialization_resumes_without_hand_repair(
    tmp_path, monkeypatch, killed_at, ordinal, expected_evidence
):
    """Every fetch-to-record interruption window must recover by re-fetching its pin."""

    _die_on_call(monkeypatch, killed_at, ordinal)
    with pytest.raises(KeyboardInterrupt):
        materialize_real_roster(tmp_path, _FakeMaterializationFetcher(), _FakeBundleFetcher())
    monkeypatch.undo()

    with pytest.raises(DigestMismatchRefusal) as refusal:
        verify_store(tmp_path)
    assert str(expected_evidence) in str(refusal.value)
    # Interrupts outside `Exception` must still release their staging.
    assert sorted((tmp_path / "staging").iterdir()) == []

    receipt = materialize_real_roster(tmp_path, _FakeMaterializationFetcher(), _FakeBundleFetcher())

    assert {row["artifact"] for row in receipt["artifacts"]} == {
        item.artifact for item in REQUIRED_ARTIFACTS
    }
    assert receipt["unattributed_staging_entries"] == []
    verify_store(tmp_path)


def test_a_resumed_boot_still_refuses_bytes_that_differ_from_the_first_fetch(tmp_path, monkeypatch):
    """Resuming is a re-fetch that must agree with the orphan, never a repin.

    The recovery above works because a pinned revision fetched twice yields the
    same bytes, so the orphaned manifest is republished identically and reused.
    If it does not, the orphan is the pin and the new bytes lose: publication
    never overwrites existing evidence.
    """

    class _Drifted(_FakeMaterializationFetcher):
        def fetch(self, repo: str, revision: str, destination: Path) -> None:
            super().fetch(repo, revision, destination)
            (destination / "model.safetensors").write_bytes(b"different weights")

    _die_on_call(monkeypatch, "_promote_materialized_snapshot", 2)
    with pytest.raises(KeyboardInterrupt):
        materialize_real_roster(tmp_path, _FakeMaterializationFetcher(), _FakeBundleFetcher())
    monkeypatch.undo()

    with pytest.raises(DigestMismatchRefusal, match="already exists with different bytes"):
        materialize_real_roster(tmp_path, _Drifted(), _FakeBundleFetcher())


def test_a_repository_that_ships_no_licence_file_may_still_have_declared_one(tmp_path):
    """No licence file and no licence declaration are distinct observations."""

    class _NoLicenceFiles(_FakeMaterializationFetcher):
        def fetch(self, repo: str, revision: str, destination: Path) -> None:
            super().fetch(repo, revision, destination)
            (destination / "LICENSE").unlink(missing_ok=True)

    materialize_real_roster(tmp_path, _NoLicenceFiles(), _FakeBundleFetcher())

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
    chandra = stored["chandra-ocr-2"]
    assert chandra["license"] in chandra["required_files"]
    assert "README.md" in chandra["required_files"]
    (tmp_path / chandra["snapshot"] / chandra["license"]).write_text("openrail", encoding="utf-8")
    with pytest.raises(DigestMismatchRefusal, match=UNTEXTED_LICENCE_SNAPSHOT):
        verify_store(tmp_path)


def test_declared_licence_observation_requires_the_model_card_it_cites(tmp_path):
    """A short fetch cannot make a synthetic observation about absent evidence."""

    requirement = next(item for item in REQUIRED_ARTIFACTS if item.artifact == "chandra-ocr-2")
    (tmp_path / "model.safetensors").write_bytes(b"weights")

    with pytest.raises(DigestMismatchRefusal, match="no regular README.md"):
        model_store._snapshot_licence(tmp_path, requirement)

    assert not (tmp_path / UNTEXTED_LICENCE_SNAPSHOT).exists()


def test_declared_licence_observation_must_match_the_fetched_model_card(tmp_path):
    requirement = next(item for item in REQUIRED_ARTIFACTS if item.artifact == "chandra-ocr-2")
    (tmp_path / "model.safetensors").write_bytes(b"weights")
    (tmp_path / "README.md").write_text("---\nlicense: apache-2.0\n---\n", encoding="utf-8")

    with pytest.raises(
        DigestMismatchRefusal,
        match="model card declares 'apache-2.0'.*roster policy expects 'openrail'",
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
        ("chandra-ocr-2", UNTEXTED_LICENCE_SNAPSHOT),
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
    chandra = next(item for item in record["artifacts"] if item["artifact"] == "chandra-ocr-2")
    chandra["license"] = UNDECLARED_LICENCE_SNAPSHOT
    chandra["required_files"] = [UNDECLARED_LICENCE_SNAPSHOT, "model.safetensors"]

    with pytest.raises(DigestMismatchRefusal, match="must be 'LICENSE-DECLARED-WITHOUT-TEXT.txt'"):
        derived_inventory(record)


def test_declared_without_text_record_keeps_its_model_card_required(tmp_path):
    record = _store(tmp_path)
    chandra = next(item for item in record["artifacts"] if item["artifact"] == "chandra-ocr-2")
    chandra["license"] = UNTEXTED_LICENCE_SNAPSHOT
    chandra["required_files"] = [UNTEXTED_LICENCE_SNAPSHOT, "model.safetensors"]

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
        materialize_real_roster(tmp_path, fetcher, _FakeBundleFetcher())

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

    materialize_real_roster(tmp_path, _ShardedFetcher(), _FakeBundleFetcher())

    record = load_download_record(tmp_path)
    entry = next(item for item in record["artifacts"] if item["artifact"] == "churro-3B")
    assert "model.safetensors.index.json" in entry["required_files"]
    assert "model-00002-of-00002.safetensors" in entry["required_files"]
    (tmp_path / entry["snapshot"] / "model-00002-of-00002.safetensors").unlink()

    with pytest.raises(DigestMismatchRefusal, match="model-00002-of-00002.safetensors"):
        verify_store(tmp_path)


def test_each_real_licence_note_agrees_with_the_licence_evidence_its_manifest_seals():
    """A chair's licence note and the licence evidence its measured manifest seals
    are one fact.

    The manifest in `config/manifests` is the measurement of the fetched bytes:
    it seals exactly one piece of licence evidence per chair, the licence text a
    repository carries, the text a bundle names, or the observation a fetch
    writes when a repository declares a licence without text or declares none.
    A note says no licence is declared exactly where the manifest seals that
    observation.
    """

    real = load_models_toml(ROOT / "config" / "models-real.toml")
    requirements = {item.chair: item for item in REQUIRED_ARTIFACTS}
    configured = {
        role: identity
        for role, identity in real.chairs.items()
        if isinstance(identity, ChairIdentity)
    }
    assert set(configured) == set(requirements)
    for role, identity in configured.items():
        requirement = requirements[role]
        rows = {
            row.path: row
            for row in read_manifest(
                ROOT / "config" / identity.manifest,
                expected_digest=identity.digest_manifest,
                chair=role,
            ).rows
        }
        if requirement.license_file is not None:
            evidence = {requirement.license_file} & set(rows)
        else:
            evidence = {
                path
                for path in rows
                if path.lower() in LICENCE_FILE_NAMES or path in SYNTHETIC_LICENCE_SNAPSHOTS
            }
        assert len(evidence) == 1, (role, evidence)
        (licence,) = evidence
        assert rows[licence].size > 0, role
        declares_nothing = "no licence declared" in identity.license_note.lower()
        assert declares_nothing == (licence == UNDECLARED_LICENCE_SNAPSHOT), role


def test_the_store_agrees_with_the_roster_about_which_repository_declares_nothing():
    """The store's `license_declaration` and the roster's note are one fact.

    The store column decides which sentinel a fetch without a licence file
    writes; the roster note is what the project lead accepted. If they disagree the store
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


def test_registry_populates_and_reuses_digest_caches_from_verified_store_sources(tmp_path):
    """Six cache roles are supplied locally, including both Chandra roles."""

    record = _mark_pending(tmp_path, _store(tmp_path), "surya2-detection", "local bundle pending")
    real = load_models_toml(ROOT / "config" / "models-real.toml")
    entries = {
        entry["artifact"]: entry for entry in record["artifacts"] if entry["state"] == "present"
    }
    source_identities = []
    chairs = {}
    config_root = tmp_path / "configured-roster"
    for role, identity in real.chairs.items():
        if not isinstance(identity, ChairIdentity) or identity.source != "huggingface":
            continue
        artifact = next(item.artifact for item in REQUIRED_ARTIFACTS if item.chair == role)
        entry = entries[artifact]
        configured = replace(
            identity,
            digest_manifest=entry["digest_manifest"],
            manifest=f"manifests/{role}.json",
        )
        source_identities.append(configured)
        chairs[role] = {
            "state": "configured",
            "source": configured.source,
            "repo": configured.repo,
            "revision": configured.revision,
            "digest_manifest": configured.digest_manifest,
            "manifest": configured.manifest,
            "serving_recipe": configured.serving_recipe,
            "license_note": configured.license_note,
        }
        if configured.witness_adapter is not None:
            chairs[role]["witness_adapter"] = configured.witness_adapter
        if configured.witness_scope is not None:
            chairs[role]["witness_scope"] = configured.witness_scope
        manifest_target = config_root / configured.manifest
        manifest_target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(tmp_path / entry["manifest"], manifest_target)
    config = parse_models_config(
        {"witness_floor": 3, "chairs": chairs}, source_path=config_root / "models.toml"
    )

    class RecordingFetcher:
        def __init__(self) -> None:
            self.calls: list[str] = []
            self.delegate = StoreRoleFetcher(tmp_path)

        def fetch(self, identity, destination, paths):  # type: ignore[no-untyped-def]
            self.calls.append(identity.role)
            self.delegate.fetch(identity, destination, paths)

    fetcher = RecordingFetcher()
    registry = ChairRegistry(config, cache_root=tmp_path / "chair-cache", fetcher=fetcher)
    for identity in source_identities:
        registry.ensure(identity)

    # The Perlector and the Coniector's reconstructor pin one manifest: one copy.
    digests = {identity.digest_manifest for identity in source_identities}
    assert len(fetcher.calls) == len(digests) < len(chairs)
    assert {"perlector", "reconstructor"} - set(fetcher.calls) != set()
    for identity in source_identities:
        assert (
            tmp_path / "chair-cache" / "by-digest" / identity.digest_manifest / CACHE_DESCRIPTOR
        ).is_file()

    fetcher.calls.clear()
    restarted = ChairRegistry(config, cache_root=tmp_path / "chair-cache", fetcher=fetcher)
    for identity in source_identities:
        restarted.ensure(identity)
    assert fetcher.calls == []


def test_role_fetch_reads_record_without_rehashing_whole_store(tmp_path, monkeypatch):
    record = _mark_pending(tmp_path, _store(tmp_path), "surya2-detection", "local bundle pending")
    real = load_models_toml(ROOT / "config" / "models-real.toml")
    identity = real.chairs["perlector"]
    assert isinstance(identity, ChairIdentity)
    row = next(item for item in record["artifacts"] if item["artifact"] == "qwen3.8-27B")
    identity = replace(identity, digest_manifest=row["digest_manifest"])
    monkeypatch.setattr(
        model_store, "verify_store", lambda root: pytest.fail("whole store rehashed")
    )
    fetcher = StoreRoleFetcher(tmp_path)
    destination = tmp_path / "role-copy"
    destination.mkdir()
    fetcher.fetch(identity, destination, ("config.json", "model.safetensors"))
    assert (destination / "model.safetensors").is_file()
    with pytest.raises(DigestMismatchRefusal, match="configured pin"):
        fetcher.plan(replace(identity, revision="0" * 40))


def test_role_fetch_hashes_each_file_as_it_copies_and_refuses_a_difference(tmp_path):
    record = _store(tmp_path)
    real = load_models_toml(ROOT / "config" / "models-real.toml")
    row = next(item for item in record["artifacts"] if item["artifact"] == "qwen3.8-27B")
    identity = replace(real.chairs["perlector"], digest_manifest=row["digest_manifest"])
    manifest = read_manifest(
        tmp_path / row["manifest"], expected_digest=row["digest_manifest"], chair="perlector"
    )
    pinned = {item.path: item.sha256 for item in manifest.rows}
    fetcher = StoreRoleFetcher(tmp_path)
    paths = ("config.json", "model.safetensors")

    first = tmp_path / "first-copy"
    first.mkdir()
    ledger = fetcher.fetch(identity, first, paths)
    assert ledger.digests == {path: pinned[path] for path in paths}

    snapshot = tmp_path / row["snapshot"]
    weights = (snapshot / "model.safetensors").read_bytes()
    (snapshot / "model.safetensors").write_bytes(weights[:-2] + b"X\n")
    second = tmp_path / "second-copy"
    second.mkdir()
    with pytest.raises(DigestMismatchRefusal, match="model.safetensors: sha256"):
        fetcher.fetch(identity, second, paths)
    with pytest.raises(DigestMismatchRefusal, match="not in the manifest"):
        fetcher.fetch(identity, second, ("unpinned.bin",))


def test_role_fetch_copies_through_a_shared_pool_when_given_one(tmp_path):
    from common.chairs.manifests import CopyPool
    from common.cpus import IoWorkers

    record = _store(tmp_path)
    real = load_models_toml(ROOT / "config" / "models-real.toml")
    row = next(item for item in record["artifacts"] if item["artifact"] == "qwen3.8-27B")
    identity = replace(real.chairs["perlector"], digest_manifest=row["digest_manifest"])
    destination = tmp_path / "shared-copy"
    destination.mkdir()

    with CopyPool(IoWorkers(5, "shared")) as pool:
        ledger = StoreRoleFetcher(tmp_path, pool=pool).fetch(
            identity, destination, ("config.json", "model.safetensors")
        )
        assert pool._threads, "the copies ran on the shared pool's workers"

    assert ledger.workers == IoWorkers(5, "shared")
    assert sorted(ledger.digests) == ["config.json", "model.safetensors"]


def test_verify_store_refuses_a_snapshot_used_directly_as_a_cache_entry(tmp_path):
    """Pointing cache_root at the store makes the registry stamp its descriptor.

    The generic refusal for that ("extra file") names the file but not the
    cause; a store is keyed by artifact and a cache by manifest digest.
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

    locked = tmp_path / "locked"
    locked.mkdir()
    locked.chmod(0o500)
    try:
        with pytest.raises(DigestMismatchRefusal, match="cannot publish"):
            model_store._publish_once(
                locked / "evidence.json", b"evidence", chair="model-store", label="evidence"
            )
    finally:
        locked.chmod(0o700)


def test_publication_onto_a_name_already_taken_by_a_directory_refuses(tmp_path):
    (tmp_path / "evidence.json").mkdir()

    with pytest.raises(DigestMismatchRefusal, match="cannot publish"):
        model_store._publish_once(
            tmp_path / "evidence.json", b"evidence", chair="model-store", label="evidence"
        )


def test_the_ad_hoc_download_record_refusal_names_the_v2_schema_it_needs(tmp_path):
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

    assert "schema must be 'verbatus-model-store.v2', not None" in str(refusal.value)


def test_a_roster_divergence_names_the_pin_it_expected_and_the_one_it_found(tmp_path):
    record = _store(tmp_path)
    entry = next(item for item in record["artifacts"] if item["artifact"] == "churro-3B")
    entry["revision"] = "1" * 40

    with pytest.raises(DigestMismatchRefusal) as refusal:
        derived_inventory(record)

    message = str(refusal.value)
    assert "ca2150ea465d5a3d67818c50e234b9422619c75d" in message
    assert "1" * 40 in message


def test_a_v1_record_is_refused_by_name_before_its_shape(tmp_path):
    record = _store(tmp_path)
    record["schema"] = "verbatus-model-store.v1"
    record["capacity"] = {"cleanup_owner": "host model-store operator"}

    with pytest.raises(
        DigestMismatchRefusal,
        match="sealed under verbatus-model-store.v1, which this build no longer reads; move or remove",
    ):
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
        materialize_real_roster(tmp_path, _LeavesClientState(), _FakeBundleFetcher())
    assert not (tmp_path / "hf").exists()


def test_repository_owned_cache_path_is_manifested_not_deleted_or_called_client_state(tmp_path):
    class _RepositoryCacheFile(_FakeMaterializationFetcher):
        def fetch(self, repo: str, revision: str, destination: Path) -> None:
            super().fetch(repo, revision, destination)
            cache = destination / ".cache"
            cache.mkdir()
            (cache / "repository-owned.json").write_text("pinned bytes", encoding="utf-8")

    materialize_real_roster(tmp_path, _RepositoryCacheFile(), _FakeBundleFetcher())

    record = load_download_record(tmp_path)
    for entry in record["artifacts"]:
        if entry["source"] != "huggingface":
            continue
        assert (tmp_path / entry["snapshot"] / ".cache/repository-owned.json").is_file()


def test_a_recorded_artifact_cannot_be_renamed_out_of_the_next_record_version(tmp_path):
    """A dropped name must refuse by name, not escape the closed refusal taxonomy.

    Five unique artifacts in, five out, so renaming one drops the old name. The
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


def test_verify_store_refuses_a_manifest_tampered_after_it_was_written(tmp_path):
    record = _store(tmp_path)
    entry = next(item for item in record["artifacts"] if item["artifact"] == "chandra-ocr-2")
    manifest_path = tmp_path / entry["manifest"]
    raw = json.loads(manifest_path.read_bytes())
    raw[0]["sha256"] = "0" * 64
    manifest_path.write_bytes(canonical_bytes(raw))

    with pytest.raises(DigestMismatchRefusal, match="manifest differs"):
        verify_store(tmp_path)
