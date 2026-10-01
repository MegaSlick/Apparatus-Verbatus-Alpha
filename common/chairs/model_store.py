"""Materialization and verification of the durable, off-repository model store.

This module owns the downloader-agnostic materialization workflow and the
verification of its durable evidence.  The injected fetcher acquires each pinned
revision; the network client itself lives in :mod:`common.chairs.registry`.
Consumers use this module to prove the store and its derived records still
agree. Its writers preserve every evidence version: inventories and manifests
publish once, while each download-record version is digest-addressed and only
its active copy moves. Differing evidence is never overwritten.
The documented store root is
``/Users/operator/verbatus-models`` (for example only, never a default).
"""

from __future__ import annotations

import fcntl
import json
import os
import shutil
import stat
import tempfile
import time
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from types import MappingProxyType
from typing import Any, Callable, Mapping, Protocol

from common.contracts.canonical import canonical_bytes, digest_bytes
from common.durability import atomic_create, atomic_replace

from .errors import DigestMismatchRefusal
from .filesystem import apfs_alias, apfs_key, read_limited_bytes
from .manifests import (
    build_manifest,
    read_manifest,
    verify_snapshot,
)
from .models import ChairIdentity, is_hf_revision, is_sha256
from .registry import CACHE_DESCRIPTOR, load_model_card_metadata

STORE_SCHEMA = "verbatus-model-store.v2"
V1_STORE_SCHEMA = "verbatus-model-store.v1"
INVENTORY_SCHEMA = "verbatus-model-inventory.v1"

# An artifact entry is present, or `pending-fetch` naming its absence and reason,
# so a partially fetched store is visibly partial.
PRESENT_FIELDS = {
    "artifact",
    "state",
    "source",
    "repo",
    "revision",
    "snapshot",
    "manifest",
    "digest_manifest",
    "license",
    "carried",
    "required_files",
}
PENDING_FIELDS = {"artifact", "state", "source", "repo", "revision", "reason"}
RECORD_FIELDS = {"schema", "layout", "artifacts"}


@dataclass(frozen=True, slots=True)
class RequiredArtifact:
    chair: str
    artifact: str
    source: str
    repo: str | None
    revision: str | None
    # A repository may declare a licence in its model card without carrying a
    # licence file, so declaration and snapshotted text are separate evidence.
    license_declaration: str | None = None
    # A local-repository artifact has no Hub revision to fetch at, so the digest
    # of its measured manifest is its pin: a fetch that measures anything else
    # is refused before it is promoted. Its licence file is named, since a
    # bundle of several checkpoints has no single repository root to look in.
    digest_manifest: str | None = None
    license_file: str | None = None


# The measured manifest of Surya's bundle as `operations/serving/surya/prefetch.py`
# writes it: the text-detection checkpoint at Datalab's dated path and the layout
# and reading-order checkpoints at the pinned Hub commit, with the bundle's lock.
SURYA_BUNDLE_DIGEST_MANIFEST = "ad19b0280bec623e7edd1b7ca5197ded1add35af9ff0ec76e80db8d035b16cb9"

# The roster policy; the inventory derived from download_record.json refuses any
# disagreement with it. `license_declaration` is the model card's own licence id
# at the pinned revision, not a reading of its terms.
REQUIRED_ARTIFACTS = (
    RequiredArtifact(
        "attestator_1",
        "chandra-ocr-2",
        "huggingface",
        "datalab-to/chandra-ocr-2",
        "af93b47dba1b47b6640c86ccf487ed2260ab9a09",
        "openrail",
    ),
    RequiredArtifact(
        "attestator_2",
        "dai-recordgold-atr",
        "huggingface",
        "Teklia/Qwen2.5-VL-7B-DAI-CReTDHI-RecordGold-ATR",
        "e371095d4ffe585f31f4974462931ddbac61ff64",
        # The one roster repository that declares no licence anywhere.
        None,
    ),
    RequiredArtifact(
        "attestator_3",
        "churro-3B",
        "huggingface",
        "stanford-oval/churro-3B",
        "ca2150ea465d5a3d67818c50e234b9422619c75d",
        "other: qwen-research",
    ),
    # DAI's own project's record detector, read so DAI sees the page as it was
    # trained to: on crops of the records this detector finds.
    RequiredArtifact(
        "secondary_proposer",
        "yolov26-record-detection",
        "huggingface",
        "Teklia/YOLOv26-DAI-CReTDHI-Record-Detection",
        "0c57f057391113579e7af170b864542f049e67aa",
        "agpl-3.0",
    ),
    # Surya's detection and layout weight bundle: fetched by its own prefetch from
    # Datalab's model host and the Hub, so it has no single Hub pin and is kept
    # as a local repository pinned by its manifest digest
    # (operations/serving/surya/README.md). Its licence file is the layout
    # repository's, whose card declares `openrail`.
    RequiredArtifact(
        "designator_surya",
        "surya2-detection",
        "local-repository",
        None,
        None,
        "openrail",
        digest_manifest=SURYA_BUNDLE_DIGEST_MANIFEST,
        license_file="surya_layout2/LICENSE",
    ),
    RequiredArtifact(
        "perlector",
        "qwen3.8-27B",
        "huggingface",
        "Qwen/Qwen3.8-27B",
        "1d4bf0f2ff6012fd82039f2fa52739d0dd7c60c0",
        "apache-2.0",
    ),
)
SURYA_OCR_2_REFUSAL = MappingProxyType(
    {
        "artifact": "surya-ocr-2",
        "state": "not-required",
        "reason": "detector only; no OCR artifact is needed",
        "escape_hatch": "recorded-bench-need",
    }
)
DAI_PROMPT_CITATION = (
    "Teklia, Qwen2.5-VL-7B-DAI-CReTDHI-RecordGold-ATR, pinned repository files "
    "system.txt and query.txt"
)
MODEL_PAYLOAD_SUFFIXES = frozenset({".bin", ".gguf", ".onnx", ".pt", ".pth", ".safetensors"})
# Generous for a six-artifact record, yet bounds a forged control document.
MAX_DOWNLOAD_RECORD_BYTES = 1_048_576
# Repository-controlled JSON may not claim unbounded memory.
MAX_SHARD_INDEX_BYTES = 16_777_216
MATERIALIZATION_LOCK_TIMEOUT_SECONDS = 60.0
MATERIALIZATION_LOCK_POLL_SECONDS = 0.1


class MaterializationFetcher(Protocol):
    """Fetch one complete, pinned repository snapshot into an empty directory."""

    def fetch(self, repo: str, revision: str, destination: Path) -> None:
        """Write the exact repository revision below ``destination``, or raise."""


class BundleFetcher(Protocol):
    """Fetch one local-repository artifact, complete, to a path that does not exist yet."""

    def check(self, artifact: str) -> None:
        """Raise unless this fetcher can fetch ``artifact`` now; fetches nothing."""

    def fetch(self, artifact: str, destination: Path) -> None:
        """Write the artifact's whole tree at ``destination``, or raise."""


class StoreRoleFetcher:
    """Plan one configured role from the durable record and its pinned manifest."""

    def __init__(self, store_root: str | Path) -> None:
        self.root = Path(store_root).resolve()

    def plan(self, identity: ChairIdentity) -> dict[str, Any]:
        record = load_download_record(self.root)
        required = next((item for item in REQUIRED_ARTIFACTS if item.chair == identity.role), None)
        if required is None:
            raise DigestMismatchRefusal(identity.role, "no model-store artifact names this chair")
        row = next(
            (item for item in record["artifacts"] if item["artifact"] == required.artifact), None
        )
        if row is None or row["state"] != "present":
            raise DigestMismatchRefusal(identity.role, "model-store artifact is not present")
        for field, expected in (
            ("source", identity.source),
            ("repo", identity.repo),
            ("revision", identity.revision),
            ("digest_manifest", identity.digest_manifest),
        ):
            if row[field] != expected:
                raise DigestMismatchRefusal(
                    identity.role, f"model-store {field} differs from the configured pin"
                )
        manifest_path = _under(self.root, row["manifest"])
        read_manifest(manifest_path, expected_digest=identity.digest_manifest, chair=identity.role)
        snapshot = _under(self.root, row["snapshot"])
        if not snapshot.is_dir() or snapshot.is_symlink():
            raise DigestMismatchRefusal(identity.role, "model-store snapshot is not a directory")
        return {"snapshot": str(snapshot), "identity": identity.cache_descriptor()}

    def fetch(self, identity: ChairIdentity, destination: Path, paths: tuple[str, ...]) -> None:
        source_root = Path(self.plan(identity)["snapshot"])
        for relative in paths:
            source = source_root / relative
            try:
                resolved = source.resolve(strict=True)
            except OSError as error:
                raise DigestMismatchRefusal(
                    identity.role, f"model-store source file {relative!r} is unavailable: {error}"
                ) from error
            if (
                not resolved.is_relative_to(source_root)
                or source.is_symlink()
                or not source.is_file()
            ):
                raise DigestMismatchRefusal(
                    identity.role,
                    f"model-store source file {relative!r} is not a regular in-snapshot file",
                )
            target = destination / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            try:
                shutil.copyfile(source, target)
            except OSError as error:
                raise DigestMismatchRefusal(
                    identity.role, f"cannot copy model-store source file {relative!r}: {error}"
                ) from error


def materialize_real_roster(
    store_root: str | Path, fetcher: MaterializationFetcher, bundle_fetcher: BundleFetcher
) -> dict[str, Any]:
    """Fetch each real pinned artifact once and publish its measured evidence.

    The one boot-time writer for real model bytes. ``fetcher`` fetches each Hub
    repository at its revision; ``bundle_fetcher`` each local-repository
    artifact, whose measured manifest must equal its pinned digest. It never
    edits ``config/models-real.toml``: a manifest digest becomes a pin only
    through a reviewed config edit. Pending artifacts are fetched before present
    ones are re-verified, so an interrupted promotion is closed by re-fetching
    the same pin; one final whole-store verification backs every receipt.
    """

    root = Path(store_root).resolve()
    with _materialization_lock(root):
        return _materialize_real_roster_locked(root, fetcher, bundle_fetcher)


@contextmanager
def _materialization_lock(root: Path):
    """Keep the staging sweep and the final verification under one store-wide lock."""
    try:
        root.mkdir(parents=True, exist_ok=True)
    except OSError as error:
        raise DigestMismatchRefusal(
            "model-store", f"cannot create materialization root {root}: {error}"
        ) from error
    try:
        lock = (root / ".materialize.lock").open("a+b")
    except OSError as error:
        raise DigestMismatchRefusal(
            "model-store", f"cannot open materialization lock for {root}: {error}"
        ) from error
    with lock:
        deadline = time.monotonic() + MATERIALIZATION_LOCK_TIMEOUT_SECONDS
        while True:
            try:
                fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError as error:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise DigestMismatchRefusal(
                        "model-store", f"timed out acquiring materialization lock for {root}"
                    ) from error
                time.sleep(min(MATERIALIZATION_LOCK_POLL_SECONDS, remaining))
            except OSError as error:
                raise DigestMismatchRefusal(
                    "model-store", f"cannot acquire materialization lock for {root}: {error}"
                ) from error
        try:
            yield
        finally:
            fcntl.flock(lock.fileno(), fcntl.LOCK_UN)


def _materialize_real_roster_locked(
    root: Path, fetcher: MaterializationFetcher, bundle_fetcher: BundleFetcher
) -> dict[str, Any]:
    record = _initial_materialization_record()
    active = root / "download_record.json"
    if active.exists():
        previous = _load_custodied(root, _validate_upgradable_record)
        record = _with_new_requirements(previous)
        if record != previous:
            # A store written before the roster gained an artifact: the new
            # version adds it as pending-fetch, and the loop below fetches it.
            write_download_record(record, root)
        # Joined before indexing, so a missing artifact is a named refusal.
        derived_inventory(record)
    else:
        write_download_record(record, root)

    completed: dict[str, dict[str, str | None]] = {}
    requirements = _unique_requirements()
    record_by_artifact = {item["artifact"]: item for item in record["artifacts"]}
    already_present = {
        item.artifact
        for item in requirements
        if record_by_artifact[item.artifact]["state"] == "present"
    }
    # A bundle fetcher that cannot run is found before any repository downloads,
    # and the local bundles are fetched first, so a bundle that fails to fetch
    # fails before the Hub downloads are paid for.
    for requirement in requirements:
        if requirement.source == "local-repository" and requirement.artifact not in already_present:
            bundle_fetcher.check(requirement.artifact)
    local_first = sorted(requirements, key=lambda item: item.source != "local-repository")
    for requirement in local_first:
        if requirement.artifact in already_present:
            continue
        if requirement.source == "local-repository":
            present = _fetch_bundle(root, requirement, bundle_fetcher)
        else:
            if not requirement.repo or not requirement.revision:
                raise DigestMismatchRefusal(
                    requirement.artifact, "Hugging Face artifact lacks a pin"
                )
            present = _fetch_artifact(root, requirement, fetcher)
        record = _replace_record_artifact(record, present)
        write_download_record(record, root)
        completed[requirement.artifact] = _materialization_receipt(present)

    # `verify_store` covers the entire volume, so one call after all fetches
    # backs every receipt without rehashing the same bytes per artifact.
    inventory = verify_store(root)
    verified = {row["artifact"]: row for row in inventory["artifacts"]}
    for artifact in already_present:
        completed[artifact] = _materialization_receipt(verified[artifact])

    return {
        "store": str(root),
        "artifacts": [
            completed[item.artifact] for item in requirements if item.artifact in completed
        ],
        # The record `verify_store` checked, never a reload a concurrent writer
        # could have moved.
        "download_record_sha256": inventory["download_record_sha256"],
        "complete": inventory["complete"],
        # Every roster artifact is fetched here, so the real roster is complete
        # exactly when the store is.
        "real_roster_complete": inventory["complete"],
        "unattributed_staging_entries": _unattributed_staging_entries(root),
    }


def pending_local_artifacts(store_root: str | Path) -> tuple[str, ...]:
    """The local-repository artifacts :func:`materialize_real_roster` would fetch
    into this store: each the roster requires that the store does not hold present.

    Reads the store and writes nothing, so a step before materialization can
    prepare what the fetch needs. A store with no record yet needs every one.
    """

    root = Path(store_root).resolve()
    local = [item.artifact for item in _unique_requirements() if item.source == "local-repository"]
    active = root / "download_record.json"
    if not active.exists() and not active.is_symlink():
        return tuple(local)
    record = _with_new_requirements(_load_custodied(root, _validate_upgradable_record))
    states = {item["artifact"]: item["state"] for item in record["artifacts"]}
    return tuple(artifact for artifact in local if states[artifact] != "present")


def _fetch_artifact(
    root: Path, requirement: RequiredArtifact, fetcher: MaterializationFetcher
) -> dict[str, Any]:
    """Fetch, measure and promote one pinned artifact; return its present entry."""
    staging_root = _under(root, "staging")
    staging_root.mkdir(parents=True, exist_ok=True)
    for leftover in staging_root.iterdir():
        _cleanup_failed_staging(leftover)
    staging = Path(tempfile.mkdtemp(prefix=f".{requirement.artifact}.fetch-", dir=staging_root))
    try:
        fetcher.fetch(requirement.repo, requirement.revision, staging)
        if staging.is_symlink() or not staging.is_dir():
            raise DigestMismatchRefusal(
                requirement.artifact,
                "the fetcher replaced the materialization destination instead of writing "
                "the pinned revision below the empty staging directory",
            )
        _refuse_staged_symlinks(staging, requirement.artifact)
        licence = _snapshot_licence(staging, requirement)
        # Again: the synthetic licence write may have introduced a case collision
        # or a link after the first walk.
        _refuse_staged_symlinks(staging, requirement.artifact)
        carried = _carried_content(requirement, staging)
        payloads = sorted(
            path.relative_to(staging).as_posix()
            for path in staging.rglob("*")
            if path.is_file() and path.suffix in MODEL_PAYLOAD_SUFFIXES
        )
        if not payloads:
            raise DigestMismatchRefusal(
                requirement.artifact, "fetched revision has no supported model payload"
            )
        _refuse_unpinned_additions(staging, requirement.artifact)
        indexed = _indexed_shards(staging, requirement.artifact)
        licence_evidence = {licence}
        if licence in SYNTHETIC_LICENCE_SNAPSHOTS:
            # A synthetic observation makes a claim about the model card.
            licence_evidence.add(MODEL_CARD_PATH)
        required_files = sorted(
            {*licence_evidence, *payloads, *indexed, *(x["path"] for x in carried)}
        )
        manifest = f"manifests/{requirement.artifact}.json"
        digest = promote_verified_snapshot(
            root,
            {
                "artifact": requirement.artifact,
                "staging": staging.relative_to(root).as_posix(),
                "manifest": manifest,
                "required_files": required_files,
            },
        )
        destination = _under(root, f"hf/{requirement.artifact}")
        _promote_materialized_snapshot(staging, destination, requirement.artifact)
        return {
            "artifact": requirement.artifact,
            "state": "present",
            "source": requirement.source,
            "repo": requirement.repo,
            "revision": requirement.revision,
            "snapshot": f"hf/{requirement.artifact}",
            "manifest": manifest,
            "digest_manifest": digest,
            "license": licence,
            "carried": carried,
            "required_files": required_files,
        }
    except BaseException:
        _cleanup_failed_staging(staging)
        raise


def _fetch_bundle(
    root: Path, requirement: RequiredArtifact, fetcher: BundleFetcher
) -> dict[str, Any]:
    """Fetch, measure, check against its pinned digest and promote one local bundle.

    The measured manifest is compared with the pin before anything is published,
    so bytes that changed upstream leave no manifest and no present entry behind.
    """
    artifact = requirement.artifact
    if requirement.digest_manifest is None or requirement.license_file is None:
        raise DigestMismatchRefusal(
            artifact, "a local-repository artifact names no pinned digest or licence file"
        )
    staging_root = _under(root, "staging")
    staging_root.mkdir(parents=True, exist_ok=True)
    for leftover in staging_root.iterdir():
        _cleanup_failed_staging(leftover)
    work = Path(tempfile.mkdtemp(prefix=f".{artifact}.fetch-", dir=staging_root))
    snapshot = work / "snapshot"
    try:
        fetcher.fetch(artifact, snapshot)
        if snapshot.is_symlink() or not snapshot.is_dir():
            raise DigestMismatchRefusal(
                artifact, "the bundle fetcher wrote no directory at the destination it was given"
            )
        _refuse_staged_symlinks(snapshot, artifact)
        _refuse_unpinned_additions(snapshot, artifact)
        licence = snapshot / requirement.license_file
        if licence.is_symlink() or not licence.is_file() or licence.stat().st_size == 0:
            raise DigestMismatchRefusal(
                artifact, f"the fetched bundle has no licence text at {requirement.license_file!r}"
            )
        # The licence file's own repository declares its licence in the model
        # card beside it; that declaration must be the roster's.
        _reconcile_model_card_licence(licence.parent, requirement)
        measured = build_manifest(snapshot)
        digest = digest_bytes(canonical_bytes(measured.to_record()))
        if digest != requirement.digest_manifest:
            raise DigestMismatchRefusal(
                artifact,
                f"the fetched bundle measures manifest {digest}, not the pinned "
                f"{requirement.digest_manifest}; the bytes at its source have changed, and "
                "a new pin is a reviewed change",
            )
        required_files = [row.path for row in measured.rows]
        manifest = f"manifests/{artifact}.json"
        promoted = promote_verified_snapshot(
            root,
            {
                "artifact": artifact,
                "staging": snapshot.relative_to(root).as_posix(),
                "manifest": manifest,
                "required_files": required_files,
            },
        )
        if promoted != requirement.digest_manifest:
            raise DigestMismatchRefusal(
                artifact,
                f"the published manifest {manifest!r} has digest {promoted}, not the pinned "
                f"{requirement.digest_manifest}",
            )
        _promote_materialized_snapshot(snapshot, _under(root, f"local/{artifact}"), artifact)
        return {
            "artifact": artifact,
            "state": "present",
            "source": requirement.source,
            "repo": None,
            "revision": None,
            "snapshot": f"local/{artifact}",
            "manifest": manifest,
            "digest_manifest": promoted,
            "license": requirement.license_file,
            "carried": [],
            "required_files": required_files,
        }
    finally:
        _cleanup_failed_staging(work)


def _cleanup_failed_staging(staging: Path) -> None:
    """Best-effort removal of a failed fetch tree."""
    try:
        if staging.is_symlink() or not staging.is_dir():
            staging.unlink(missing_ok=True)
        else:
            shutil.rmtree(staging, ignore_errors=True)
    except OSError:
        pass


def _refuse_staged_symlinks(snapshot: Path, artifact: str) -> None:
    """Refuse links and non-portable identities before inspecting fetched bytes.

    Git snapshots carry no hard links or mount points, so either could make
    containment depend on an inode outside staging. APFS folds normalization and
    case, so names that collapse there are not two durable artifacts.
    """

    def refuse_walk(error: OSError) -> None:
        raise DigestMismatchRefusal(
            artifact, f"the fetched revision cannot be inspected for symlinks: {error}"
        ) from error

    try:
        root_device = snapshot.stat(follow_symlinks=False).st_dev
    except OSError as error:
        refuse_walk(error)
    identities: dict[str, str] = {}
    for directory, directories, filenames in os.walk(
        snapshot, followlinks=False, onerror=refuse_walk
    ):
        parent = Path(directory)
        for name in [*directories, *filenames]:
            candidate = parent / name
            relative = candidate.relative_to(snapshot).as_posix()
            previous = apfs_alias(identities, relative)
            if previous is not None:
                raise DigestMismatchRefusal(
                    artifact,
                    "the fetched revision carries paths that collide on default APFS: "
                    f"{previous!r} and {relative!r}",
                )
            if candidate.is_symlink():
                raise DigestMismatchRefusal(
                    artifact,
                    f"the fetched revision carries a symlink at {relative!r}; materialization "
                    "never reads or manifests a link target",
                )
            try:
                status = candidate.stat(follow_symlinks=False)
            except OSError as error:
                raise DigestMismatchRefusal(
                    artifact, f"the fetched revision cannot inspect {relative!r}: {error}"
                ) from error
            if status.st_dev != root_device:
                raise DigestMismatchRefusal(
                    artifact,
                    f"the fetched revision crosses a device boundary at {relative!r}; "
                    "staging containment is an inode property, not a path spelling",
                )
            if stat.S_ISREG(status.st_mode) and status.st_nlink != 1:
                raise DigestMismatchRefusal(
                    artifact,
                    f"the fetched revision carries a hard-linked file at {relative!r}; "
                    "repository evidence must be owned by this staging tree alone",
                )


# Client bookkeeping in the staged tree is refused, not measured: it would make a
# pin no second fetch reproduces. Only this exact namespace; other `.cache/*`
# files are upstream content.
CLIENT_BOOKKEEPING_PREFIX = (".cache", "huggingface")


def _refuse_unpinned_additions(snapshot: Path, artifact: str) -> None:
    found = sorted(
        path.relative_to(snapshot).as_posix()
        for path in snapshot.rglob("*")
        if path.is_file()
        and (
            ".git" in path.relative_to(snapshot).parts
            or path.relative_to(snapshot).parts[:2] == CLIENT_BOOKKEEPING_PREFIX
        )
    )
    if found:
        raise DigestMismatchRefusal(
            artifact,
            "the fetched tree carries client bookkeeping rather than only the pinned "
            f"revision: {found}. These are not repository bytes, some of them are not "
            "reproducible, and the manifest measured here becomes this artifact's pin",
        )


def _unique_requirements() -> list[RequiredArtifact]:
    """Roster order, one entry per artifact, however many chairs one fills."""

    unique: dict[str, RequiredArtifact] = {}
    for item in REQUIRED_ARTIFACTS:
        unique.setdefault(item.artifact, item)
    return list(unique.values())


def _unattributed_staging_entries(root: Path) -> list[str]:
    """Name leftover staging entries; a listing cannot tell interrupted work from a live writer."""

    staging = _under(root, "staging")
    if not staging.is_dir():
        return []
    return sorted(path.name for path in staging.iterdir())


# A split checkpoint's `weight_map` states every shard a complete fetch has.
SHARD_INDEX_NAMES = ("model.safetensors.index.json", "pytorch_model.bin.index.json")


def _indexed_shards(snapshot: Path, artifact: str) -> list[str]:
    """Reconcile a fetched snapshot against the shard index it fetched with.

    Returns the index and its shards so they join `required_files`, and every
    later `verify_store` repeats the reconciliation.
    """

    found: set[str] = set()
    for path in sorted(snapshot.rglob("*")):
        if not path.is_file() or path.name not in SHARD_INDEX_NAMES:
            continue
        try:
            index = json.loads(
                read_limited_bytes(
                    path,
                    MAX_SHARD_INDEX_BYTES,
                    artifact,
                    f"shard index {path.name!r}",
                )
            )
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
            raise DigestMismatchRefusal(
                artifact, f"shard index {path.name!r} is unreadable: {error}"
            ) from error
        weight_map = index.get("weight_map") if isinstance(index, Mapping) else None
        if not isinstance(weight_map, Mapping) or not weight_map:
            raise DigestMismatchRefusal(
                artifact, f"shard index {path.name!r} names no weight map to reconcile"
            )
        raw_shards = list(weight_map.values())
        if not all(isinstance(name, str) and name.strip() for name in raw_shards):
            raise DigestMismatchRefusal(
                artifact,
                f"shard index {path.name!r} must name shards as nonblank relative POSIX paths",
            )
        shards = sorted(set(raw_shards))
        unsafe = [
            name
            for name in shards
            if PurePosixPath(name).is_absolute()
            or not PurePosixPath(name).parts
            or ".." in PurePosixPath(name).parts
            or "\\" in name
        ]
        if unsafe:
            raise DigestMismatchRefusal(
                artifact,
                f"shard index {path.name!r} names unsafe shard paths: {unsafe}",
            )
        missing = [name for name in shards if not (path.parent / name).is_file()]
        if missing:
            raise DigestMismatchRefusal(
                artifact,
                f"the fetch is incomplete: {path.name!r} names {len(shards)} shards and "
                f"{missing} did not arrive",
            )
        found.add(path.relative_to(snapshot).as_posix())
        found.update((path.parent / name).relative_to(snapshot).as_posix() for name in shards)
    return sorted(found)


def _pending_entry(item: RequiredArtifact) -> dict[str, Any]:
    return {
        "artifact": item.artifact,
        "state": "pending-fetch",
        "source": item.source,
        "repo": item.repo,
        "revision": item.revision,
        "reason": "awaiting pinned pod-launch materialization",
    }


def _with_new_requirements(record: Mapping[str, Any]) -> dict[str, Any]:
    """The record with each roster artifact it does not name added as pending-fetch."""
    artifacts = record.get("artifacts")
    if not isinstance(artifacts, list):
        return dict(record)
    named = {item.get("artifact") for item in artifacts if isinstance(item, Mapping)}
    required = {item.artifact: item for item in REQUIRED_ARTIFACTS}
    added = [_pending_entry(required[name]) for name in sorted(set(required) - named)]
    if not added:
        return dict(record)
    return {
        **record,
        "artifacts": sorted(
            [dict(item) for item in artifacts] + added, key=lambda item: item["artifact"]
        ),
    }


def _validate_upgradable_record(raw: Mapping[str, Any]) -> None:
    """A valid record, or one valid once the roster artifacts it lacks are added.

    Every entry it does name must still match the roster; any other shape is
    refused exactly as :func:`load_download_record` refuses it.
    """
    record = raw
    if isinstance(raw, Mapping) and isinstance(raw.get("artifacts"), list):
        named = {item.get("artifact") for item in raw["artifacts"] if isinstance(item, Mapping)}
        # An entry the roster no longer names is read strictly and refused.
        if named <= {item.artifact for item in REQUIRED_ARTIFACTS}:
            record = _with_new_requirements(raw)
    _validate_record(record)
    derived_inventory(record)


def _initial_materialization_record() -> dict[str, Any]:
    return {
        "schema": STORE_SCHEMA,
        "layout": {
            "hf": "hf",
            "local": "local",
            "manifests": "manifests",
            "records": "records",
            "staging": "staging",
        },
        "artifacts": [
            _pending_entry(item)
            for item in sorted(
                {item.artifact: item for item in REQUIRED_ARTIFACTS}.values(),
                key=lambda item: item.artifact,
            )
        ],
    }


def _replace_record_artifact(
    record: Mapping[str, Any], replacement: Mapping[str, Any]
) -> dict[str, Any]:
    result = dict(record)
    result["artifacts"] = [
        dict(replacement) if item["artifact"] == replacement["artifact"] else dict(item)
        for item in record["artifacts"]
    ]
    return result


LICENCE_FILE_NAMES = frozenset(
    {"license", "license.md", "license.txt", "copying", "copying.md", "copying.txt"}
)
UNDECLARED_LICENCE_SNAPSHOT = "LICENSE-NOT-DECLARED.txt"
UNTEXTED_LICENCE_SNAPSHOT = "LICENSE-DECLARED-WITHOUT-TEXT.txt"
SYNTHETIC_LICENCE_SNAPSHOTS = frozenset({UNDECLARED_LICENCE_SNAPSHOT, UNTEXTED_LICENCE_SNAPSHOT})
MODEL_CARD_PATH = "README.md"


def _snapshot_licence(snapshot: Path, requirement: RequiredArtifact) -> str:
    """Name the licence evidence for this fetch, and never overstate it.

    Licence text, a model-card declaration only, or nothing; the roster
    declaration tells the last two apart. A synthetic sentinel is written into
    staging before the manifest is built, so the digest covers it.
    """

    try:
        candidates = sorted(
            path
            for path in snapshot.iterdir()
            if path.is_file() and path.name.lower() in LICENCE_FILE_NAMES
        )
    except OSError as error:
        raise DigestMismatchRefusal(
            requirement.artifact, f"cannot inspect the fetched repository root: {error}"
        ) from error
    if candidates:
        return candidates[0].relative_to(snapshot).as_posix()
    _reconcile_model_card_licence(snapshot, requirement)
    observation = (
        UNTEXTED_LICENCE_SNAPSHOT
        if requirement.license_declaration is not None
        else UNDECLARED_LICENCE_SNAPSHOT
    )
    path = snapshot / observation
    _write_licence_observation(
        path,
        _licence_observation_text(requirement),
        requirement.artifact,
    )
    return path.name


def _reconcile_model_card_licence(snapshot: Path, requirement: RequiredArtifact) -> None:
    """Verify the fetched card before publishing a claim about its licence metadata."""

    model_card = snapshot / MODEL_CARD_PATH
    if model_card.is_symlink() or not model_card.is_file():
        raise DigestMismatchRefusal(
            requirement.artifact,
            f"the fetched revision has no regular {MODEL_CARD_PATH} from which to verify "
            "its licence declaration",
        )
    try:
        metadata = load_model_card_metadata(model_card)
    except Exception as error:
        raise DigestMismatchRefusal(
            requirement.artifact,
            f"the fetched {MODEL_CARD_PATH} has unreadable model-card metadata: {error}",
        ) from error
    raw_license = metadata.get("license") if metadata is not None else None
    if raw_license is None:
        observed = None
    elif not isinstance(raw_license, str) or not raw_license.strip():
        raise DigestMismatchRefusal(
            requirement.artifact,
            f"the fetched {MODEL_CARD_PATH} licence declaration must be nonblank text or absent, "
            f"not {raw_license!r}",
        )
    else:
        observed = raw_license.strip()
        if observed == "other":
            raw_name = metadata.get("license_name")
            if not isinstance(raw_name, str) or not raw_name.strip():
                raise DigestMismatchRefusal(
                    requirement.artifact,
                    f"the fetched {MODEL_CARD_PATH} declares licence 'other' without a "
                    "nonblank license_name",
                )
            observed = f"other: {raw_name.strip()}"
    if observed != requirement.license_declaration:
        raise DigestMismatchRefusal(
            requirement.artifact,
            f"the fetched model card declares {observed!r}, but roster policy expects "
            f"{requirement.license_declaration!r}; synthetic licence evidence is never "
            "published from a disagreement",
        )


def _licence_observation_text(requirement: RequiredArtifact) -> str:
    origin = f"{requirement.repo}@{requirement.revision}"
    if requirement.license_declaration is not None:
        return (
            f"{origin} ships no licence file at this revision.\n"
            f"Its model card declares: {requirement.license_declaration}\n"
            "That declaration is the repository's own, recorded here because the "
            "pinned revision carries no licence text to snapshot. The card itself "
            f"is {MODEL_CARD_PATH} in this snapshot and is covered by this artifact's "
            "digest manifest; the licence's full terms are not in the pinned "
            "revision and must be read from the licence's canonical source.\n"
        )
    return (
        f"No licence file and no licence declaration were present in {origin} at "
        "fetch time: neither a licence file in the repository nor a licence in "
        "its model card.\n"
    )


def _write_licence_observation(path: Path, text: str, artifact: str) -> None:
    """Create synthetic evidence once, never overwriting repository bytes or a concurrent writer's."""

    folded_name = apfs_key(path.name)
    try:
        collision = next(
            (
                candidate.name
                for candidate in path.parent.iterdir()
                if apfs_key(candidate.name) == folded_name
            ),
            None,
        )
        if collision is not None:
            raise DigestMismatchRefusal(
                artifact,
                f"reserved synthetic licence evidence name {path.name!r} collides on "
                f"default APFS with repository path {collision!r}; upstream bytes are "
                "never overwritten",
            )
        with path.open("x", encoding="utf-8") as handle:
            handle.write(text)
    except FileExistsError as error:
        raise DigestMismatchRefusal(
            artifact,
            f"reserved synthetic licence evidence name {path.name!r} already exists in "
            "the fetched repository; upstream bytes are never overwritten",
        ) from error
    except OSError as error:
        raise DigestMismatchRefusal(
            artifact, f"cannot publish synthetic licence evidence {path.name!r}: {error}"
        ) from error


def _carried_content(requirement: RequiredArtifact, snapshot: Path) -> list[dict[str, str]]:
    if requirement.artifact != "dai-recordgold-atr":
        return []
    paths = ("system.txt", "query.txt")
    missing = [path for path in paths if not (snapshot / path).is_file()]
    if missing:
        raise DigestMismatchRefusal(
            requirement.artifact, f"pinned prompt files are absent: {missing}"
        )
    return [{"name": path, "path": path, "citation": DAI_PROMPT_CITATION} for path in paths]


def _promote_materialized_snapshot(staging: Path, destination: Path, artifact: str) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.exists():
        if build_manifest(staging).to_record() != build_manifest(destination).to_record():
            raise DigestMismatchRefusal(
                artifact, "existing snapshot differs from the newly fetched pinned bytes"
            )
        shutil.rmtree(staging)
        return
    os.replace(staging, destination)


def _materialization_receipt(entry: Mapping[str, Any]) -> dict[str, str | None]:
    return {
        "artifact": str(entry["artifact"]),
        # A local bundle has neither; the receipt says so rather than "None".
        "repo": entry["repo"],
        "revision": entry["revision"],
        "manifest": str(entry["manifest"]),
        "digest_manifest": str(entry["digest_manifest"]),
        "license": str(entry["license"]),
    }


def load_download_record(store_root: str | Path) -> dict[str, Any]:
    """Load the canonical active record and prove its immutable version exists."""

    return _load_custodied(Path(store_root).resolve(), _validate_record)


def _load_custodied(root: Path, validate: Callable[[Mapping[str, Any]], None]) -> dict[str, Any]:
    active = root / "download_record.json"
    if _is_irregular(active):
        raise DigestMismatchRefusal(
            "model-store", "download_record.json must be a regular in-store active copy"
        )
    try:
        raw_bytes = read_limited_bytes(
            active,
            MAX_DOWNLOAD_RECORD_BYTES,
            "model-store",
            "download_record.json",
        )
        raw = json.loads(raw_bytes)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise DigestMismatchRefusal(
            "model-store", f"cannot read download_record.json: {error}"
        ) from error
    if raw_bytes != canonical_bytes(raw):
        raise DigestMismatchRefusal(
            "model-store",
            "download_record.json is not canonical bytes (sorted keys, no "
            "whitespace, UTF-8, no trailing newline); write it with "
            "write_download_record rather than by hand",
        )
    validate(raw)
    digest = digest_bytes(raw_bytes)
    archive = _under(root, f"records/{digest}.json")
    if _is_irregular(archive):
        raise DigestMismatchRefusal(
            "model-store", "immutable download record version must be a regular in-store file"
        )
    try:
        archived_bytes = read_limited_bytes(
            archive,
            MAX_DOWNLOAD_RECORD_BYTES,
            "model-store",
            "immutable version of the download record",
        )
    except OSError as error:
        raise DigestMismatchRefusal(
            "model-store",
            f"active download record has no readable immutable version {archive}: {error}",
        ) from error
    if archived_bytes != raw_bytes:
        raise DigestMismatchRefusal(
            "model-store",
            f"active download record differs from immutable version {archive}",
        )
    return raw


def write_download_record(record: Mapping[str, Any], store_root: str | Path) -> str:
    """Version the host record immutably and move its active copy atomically.

    Each canonical record is published once at ``records/<sha256>.json``; only
    ``download_record.json`` moves, and earlier versions stay. A
    present artifact never moves back to pending-fetch: bytes missing after
    acquisition are lost, not unfetched.
    """

    _validate_record(record)
    root = Path(store_root).resolve()
    destination = root / "download_record.json"
    if _is_irregular(destination):
        raise DigestMismatchRefusal(
            "model-store", "download_record.json must be a regular in-store active copy"
        )
    payload = canonical_bytes(record)
    digest = digest_bytes(payload)
    if destination.exists():
        try:
            previous_bytes = destination.read_bytes()
        except OSError as error:
            raise DigestMismatchRefusal(
                "model-store", f"cannot read active download_record.json: {error}"
            ) from error
        if previous_bytes == payload:
            # Only an already-custodied record; a direct write gains no authority.
            _current_record(root, previous_bytes)
            return digest
        previous_digest = digest_bytes(previous_bytes)
        previous = _current_record(root, previous_bytes)
        _validate_record_transition(previous, record)
        _publish_once(
            _under(root, f"records/{previous_digest}.json"),
            previous_bytes,
            chair="model-store",
            label="previous or legacy download record",
        )

    # The reader's roster join, before anything is published: the writer may not
    # accept what every reader refuses.
    derived_inventory(record)
    archive = _under(root, f"records/{digest}.json")
    _publish_once(archive, payload, chair="model-store", label="download record version")
    _move_active_record(destination, archive)
    return digest


def derived_inventory(record: Mapping[str, Any]) -> dict[str, Any]:
    """Compute the seven-chair inventory from the one authoritative store record."""

    _validate_record(record)
    artifacts = {item["artifact"]: item for item in record["artifacts"]}
    rows: list[dict[str, Any]] = []
    for required in REQUIRED_ARTIFACTS:
        item = artifacts.get(required.artifact)
        if item is None:
            raise DigestMismatchRefusal(
                "model-store", f"required artifact {required.artifact!r} is absent"
            )
        for field, expected in (
            ("source", required.source),
            ("repo", required.repo),
            ("revision", required.revision),
        ):
            if item.get(field) != expected:
                raise DigestMismatchRefusal(
                    "model-store",
                    f"{required.artifact!r} {field} diverges from roster policy: expected "
                    f"{expected!r}, the record says {item.get(field)!r}",
                )
        if (
            required.digest_manifest is not None
            and item["state"] == "present"
            and item["digest_manifest"] != required.digest_manifest
        ):
            raise DigestMismatchRefusal(
                required.artifact,
                f"the store holds {required.artifact!r} at manifest {item['digest_manifest']}, "
                f"not the pinned {required.digest_manifest}; a store is never re-pinned in "
                "place, so fetch the new pin into a fresh store",
            )
        rows.append({"chair": required.chair, **item})
    pending = sorted(
        {item["artifact"] for item in record["artifacts"] if item["state"] == "pending-fetch"}
    )
    return {
        "schema": INVENTORY_SCHEMA,
        "download_record_sha256": digest_bytes(canonical_bytes(record)),
        "complete": not pending,
        "artifacts": rows,
        "pending": pending,
        "refusals": [dict(SURYA_OCR_2_REFUSAL)],
    }


def verify_store(store_root: str | Path) -> dict[str, Any]:
    """Verify every declared manifest against its existing bytes; never fetch.

    A `pending-fetch` entry has no bytes to verify, so it is passed over and
    reported: the returned inventory is then a verified inventory of a
    *partial* store, marked `complete: false`, with every pending artifact named.
    """

    root = Path(store_root).resolve()
    record = load_download_record(root)
    inventory = derived_inventory(record)
    for item in record["artifacts"]:
        if item["state"] == "pending-fetch":
            prefix = "hf" if item["source"] == "huggingface" else "local"
            possible_evidence = (
                root / prefix / item["artifact"],
                root / "manifests" / f"{item['artifact']}.json",
            )
            found = [
                path.relative_to(root).as_posix()
                for path in possible_evidence
                if path.exists() or path.is_symlink()
            ]
            if found:
                raise DigestMismatchRefusal(
                    item["artifact"],
                    "pending-fetch conflicts with existing acquisition evidence "
                    f"{found}; not-yet-fetched cannot describe fetched, partially "
                    "promoted, or fetched-and-lost bytes",
                )
            # Nothing exists to verify and nothing is claimed: the absence is
            # named in the record and travels out in the inventory's `pending`.
            continue
        snapshot = _under(root, item["snapshot"])
        _refuse_staged_symlinks(snapshot, item["artifact"])
        manifest_path = _under(root, item["manifest"])
        if not manifest_path.is_file():
            raise DigestMismatchRefusal(
                item["artifact"],
                f"digest manifest {item['manifest']!r} must be a regular file",
            )
        manifest = read_manifest(
            manifest_path, expected_digest=item["digest_manifest"], chair=item["artifact"]
        )
        rows = {row.path: row for row in manifest.rows}
        # Licence-specific refusals must win over the generic required-file
        # sweep because they identify the missing evidence class.
        license_row = rows.get(item["license"])
        if license_row is None:
            raise DigestMismatchRefusal(
                item["artifact"], "license snapshot is absent from its digest manifest"
            )
        # A zero-byte licence file exists but carries no licence terms.
        if license_row.size == 0:
            raise DigestMismatchRefusal(
                item["artifact"],
                f"license snapshot {item['license']!r} is empty; the pinned revision's "
                "licence text is the artifact, not a file of that name",
            )
        _verify_required_files(item, rows)
        for carried in item["carried"]:
            if carried["path"] not in rows:
                raise DigestMismatchRefusal(
                    item["artifact"],
                    f"carried content {carried['path']!r} is absent from its digest manifest",
                )
        if (snapshot / CACHE_DESCRIPTOR).exists():
            raise DigestMismatchRefusal(
                item["artifact"],
                f"the chair registry's cache descriptor {CACHE_DESCRIPTOR!r} is inside this "
                "store snapshot: a store directory is keyed by artifact and is not a "
                "cache_root entry, which is keyed by chair role. Fill "
                "cache_root/<role> from this snapshot through StoreRoleFetcher instead",
            )
        identity = ChairIdentity(
            role=item["artifact"],
            source=item["source"],
            repo=item["repo"],
            path=item["snapshot"] if item["source"] == "local-repository" else None,
            revision=item["revision"],
            digest_manifest=item["digest_manifest"],
            manifest=item["manifest"],
            adapter_of=None,
            serving_recipe="unproven-store-only",
            license_note="verified in off-repo model store",
        )
        verify_snapshot(identity, snapshot, manifest)
        _verify_synthetic_licence_observation(snapshot, item)
    return inventory


def _verify_synthetic_licence_observation(snapshot: Path, item: Mapping[str, Any]) -> None:
    if item["license"] not in SYNTHETIC_LICENCE_SNAPSHOTS:
        return
    requirement = next(
        required for required in REQUIRED_ARTIFACTS if required.artifact == item["artifact"]
    )
    path = snapshot / item["license"]
    try:
        actual = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as error:
        raise DigestMismatchRefusal(
            item["artifact"],
            f"synthetic licence evidence {item['license']!r} is not readable UTF-8 text: {error}",
        ) from error
    expected = _licence_observation_text(requirement)
    if actual != expected:
        # Two causes, and the message must not name only one of them. The store's
        # bytes may genuinely disagree with the pin -- or this code's wording of
        # what it observed may have been edited since the fetch that wrote them,
        # in which case the store is intact and the only repair is a re-fetch.
        # The recorded observation is a layer and is not
        # retroactively re-blessed, so the refusal states both readings.
        raise DigestMismatchRefusal(
            item["artifact"],
            f"synthetic licence evidence {item['license']!r} does not match the pinned "
            "repository and roster declaration. Either the recorded evidence differs "
            "from the pin, or the wording this code writes for that observation has "
            "changed since the fetch that recorded it; in the second case the store is "
            "intact and must be re-fetched to record the current observation",
        )


def promote_verified_snapshot(store_root: str | Path, artifact: Mapping[str, Any]) -> str:
    """Store-side promotion primitive: hash a staged snapshot, publish its manifest once.

    This is where a pin is *born*, and it is the one place in this package that
    derives one from bytes rather than checking bytes against one.  That is not
    an exception to a pin being a constant the artifact must match: the first
    manifest of a fetch has nothing to be checked against, which is why
    `config/models.toml` leaves `digest_manifest` unfilled
    until a verified fetch exists.  Every later use of that manifest — a second
    promotion, `verify_store`, `ChairRegistry.ensure` — is a constant the
    artifact must match, and a second promotion of differing bytes is refused
    below rather than repinned.

    The caller supplies an already-created staging directory.  This function does
    not copy or download bytes.  Publication follows the rest of this module's
    custody rule (evidence is never overwritten): identical bytes
    already published are reused silently, a differing manifest already at that
    name is refused, and the existing file is never touched either way. A picked
    manifest name is a pin, not a rolling pointer a second promotion may rewrite.
    """

    root = Path(store_root).resolve()
    if not isinstance(artifact, Mapping) or not {
        "artifact",
        "staging",
        "manifest",
        "required_files",
    } <= set(artifact):
        raise DigestMismatchRefusal(
            "model-store",
            "a promotion entry carries at least artifact, staging, manifest, and "
            "required_files; a pending-fetch entry has no bytes to promote",
        )
    _safe(artifact["artifact"], "artifact name")
    # Anywhere else it would sit forever under a name no valid record references.
    expected_manifest = f"manifests/{artifact['artifact']}.json"
    if artifact["manifest"] != expected_manifest:
        raise DigestMismatchRefusal(
            artifact["artifact"],
            f"a promoted manifest is published at its artifact-keyed path "
            f"{expected_manifest!r}, not {artifact['manifest']!r}; no download "
            "record may reference any other name",
        )
    staging = _under(root, artifact["staging"])
    manifest = build_manifest(staging)
    _verify_required_files(artifact, {row.path: row for row in manifest.rows})
    destination = _under(root, artifact["manifest"])
    payload = canonical_bytes(manifest.to_record())
    _publish_once(destination, payload, chair="model-store", label="verified manifest")
    return digest_bytes(payload)


def _publish_once(destination: Path, payload: bytes, *, chair: str, label: str) -> None:
    """Publish ``payload`` at ``destination`` without ever overwriting a difference.

    Every filesystem failure is a refusal against a named chair, not a bare
    ``OSError``, so a caller that catches ``ChairRefusal`` records it.
    """

    try:
        destination.parent.mkdir(parents=True, exist_ok=True)
        if _is_irregular(destination):
            raise DigestMismatchRefusal(
                chair,
                f"cannot publish {label} at {destination}: the name already exists and "
                "is not a regular file; "
                "publication never replaces existing evidence",
            )
        try:
            atomic_create(destination, payload, strict=False)
        except FileExistsError:
            # The taken name can be a directory; that `read_bytes` failure is caught below.
            if destination.read_bytes() != payload:
                raise DigestMismatchRefusal(
                    chair,
                    f"{label} {destination} already exists with different bytes; "
                    "publication never overwrites existing evidence",
                ) from None
    except OSError as error:
        raise DigestMismatchRefusal(
            chair, f"cannot publish {label} at {destination}: {error}"
        ) from error


def _move_active_record(destination: Path, archive: Path) -> None:
    """Atomically point the active name at an already-immutable record version."""

    try:
        destination.parent.mkdir(parents=True, exist_ok=True)
        atomic_replace(destination, archive.read_bytes(), strict=False)
    except OSError as error:
        raise DigestMismatchRefusal(
            "model-store", f"cannot publish active download_record.json: {error}"
        ) from error


def _current_record(root: Path, raw_bytes: bytes) -> dict[str, Any]:
    """Validate an active record before a writer can replace its bytes."""

    try:
        raw = json.loads(raw_bytes)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise DigestMismatchRefusal(
            "model-store",
            "active download_record.json is not a valid record; move or remove "
            "the old download_record.json, then re-run",
        ) from error
    if isinstance(raw, Mapping) and raw.get("schema") == STORE_SCHEMA:
        # This also proves canonical bytes and the immutable archived version. A
        # damaged current record is not silently treated as legacy and replaced.
        # A record lacking only newly required artifacts may be superseded; the
        # transition check then keeps every entry it names.
        return _load_custodied(root, _validate_upgradable_record)
    if isinstance(raw, Mapping) and raw.get("schema") == V1_STORE_SCHEMA:
        raise DigestMismatchRefusal(
            "model-store",
            f"sealed under {V1_STORE_SCHEMA}, which this build no longer reads; move or "
            "remove the old download_record.json, then re-run",
        )
    schema = raw.get("schema") if isinstance(raw, Mapping) else None
    raise DigestMismatchRefusal(
        "model-store",
        f"active download_record.json has unsupported schema {schema!r}; move or remove "
        "the old download_record.json, then re-run",
    )


def _validate_record_transition(
    previous: Mapping[str, Any], replacement: Mapping[str, Any]
) -> None:
    """Keep not-yet-fetched distinct from fetched-and-lost across versions."""

    old = {item["artifact"]: item for item in previous["artifacts"]}
    new = {item["artifact"]: item for item in replacement["artifacts"]}
    for artifact, old_item in old.items():
        replacement_item = new.get(artifact)
        if replacement_item is None:
            raise DigestMismatchRefusal(
                artifact,
                "the replacement download record does not name this recorded artifact; "
                "an entry is superseded by a new version of itself, never dropped from "
                "the record",
            )
        if old_item["state"] == "present" and replacement_item["state"] == "pending-fetch":
            raise DigestMismatchRefusal(
                artifact,
                "a fetched artifact cannot return to pending-fetch; if its bytes are "
                "missing, the present record must remain and verification reports it "
                "as fetched-and-lost",
            )


def _validate_record(raw: Mapping[str, Any]) -> None:
    if not isinstance(raw, Mapping):
        raise DigestMismatchRefusal("model-store", "download record is not a table")
    if raw.get("schema") != STORE_SCHEMA:
        if raw.get("schema") == V1_STORE_SCHEMA:
            raise DigestMismatchRefusal(
                "model-store",
                f"sealed under {V1_STORE_SCHEMA}, which this build no longer reads; move or "
                "remove the old download_record.json, then re-run",
            )
        raise DigestMismatchRefusal(
            "model-store",
            f"download record schema must be {STORE_SCHEMA!r}, not {raw.get('schema')!r}",
        )
    if set(raw) != RECORD_FIELDS:
        missing = sorted(RECORD_FIELDS - set(raw), key=str)
        unexpected = sorted(set(raw) - RECORD_FIELDS, key=str)
        raise DigestMismatchRefusal(
            "model-store",
            "download record has an invalid top-level shape: it carries exactly "
            f"{sorted(RECORD_FIELDS)}; missing={missing}, unexpected={unexpected}",
        )
    if raw["layout"] != {
        "hf": "hf",
        "local": "local",
        "manifests": "manifests",
        "records": "records",
        "staging": "staging",
    }:
        raise DigestMismatchRefusal(
            "model-store",
            "store layout must name hf, local, manifests, records, and staging roots",
        )
    items = raw["artifacts"]
    expected_artifacts = len({required.artifact for required in REQUIRED_ARTIFACTS})
    if not isinstance(items, list) or len(items) != expected_artifacts:
        raise DigestMismatchRefusal(
            "model-store",
            f"download record must name exactly {expected_artifacts} unique roster "
            "artifacts, each either present or pending-fetch",
        )
    seen: set[str] = set()
    for item in items:
        if not isinstance(item, Mapping):
            raise DigestMismatchRefusal("model-store", "artifact entry is not a table")
        if not isinstance(item.get("artifact"), str) or not item["artifact"].strip():
            raise DigestMismatchRefusal(
                "model-store", "every artifact entry must name its artifact"
            )
        if item["artifact"] in seen:
            raise DigestMismatchRefusal(
                "model-store",
                f"artifact names must be unique; {item['artifact']!r} is named twice",
            )
        seen.add(item["artifact"])
        # Both record shapes use the artifact name as a path component before a
        # roster join is guaranteed.
        _safe(item["artifact"], "artifact name")
        state = item.get("state")
        if state == "pending-fetch":
            if set(item) != PENDING_FIELDS:
                raise DigestMismatchRefusal(
                    item["artifact"],
                    "a pending-fetch entry carries exactly "
                    f"{sorted(PENDING_FIELDS)}: no snapshot, manifest, pin, licence or "
                    "carried content exists for bytes that are not on disk",
                )
            if not isinstance(item["reason"], str) or not item["reason"].strip():
                raise DigestMismatchRefusal(
                    item["artifact"],
                    "a pending-fetch entry must say why the artifact is not on disk yet",
                )
            _validate_origin(item)
            continue
        if state != "present" or set(item) != PRESENT_FIELDS:
            raise DigestMismatchRefusal(
                item["artifact"],
                f"artifact entry state must be 'present' (with exactly {sorted(PRESENT_FIELDS)}) "
                f"or 'pending-fetch' (with exactly {sorted(PENDING_FIELDS)})",
            )
        _validate_origin(item)
        if not is_sha256(item["digest_manifest"]):
            raise DigestMismatchRefusal(
                "model-store",
                f"artifact {item['artifact']!r} has an invalid source or manifest pin",
            )
        for field in ("snapshot", "manifest", "license"):
            _safe(item[field], field)
        prefix = "hf/" if item["source"] == "huggingface" else "local/"
        expected_snapshot = f"{prefix}{item['artifact']}"
        if item["snapshot"] != expected_snapshot:
            raise DigestMismatchRefusal(
                "model-store",
                f"artifact {item['artifact']!r} is sourced from {item['source']!r} and its "
                f"snapshot must be its artifact-keyed path {expected_snapshot!r}, not "
                f"{item['snapshot']!r}",
            )
        # The named-root layout constrains manifests as well as snapshots.
        expected_manifest = f"manifests/{item['artifact']}.json"
        if item["manifest"] != expected_manifest:
            raise DigestMismatchRefusal(
                "model-store",
                f"artifact {item['artifact']!r} digest manifest must be its artifact-keyed "
                f"path {expected_manifest!r}, not {item['manifest']!r}",
            )
        carried = item["carried"]
        if not isinstance(carried, list):
            raise DigestMismatchRefusal("model-store", "carried content must be a list")
        for entry in carried:
            if not isinstance(entry, Mapping) or set(entry) != {"name", "path", "citation"}:
                raise DigestMismatchRefusal(
                    "model-store", "carried content must name path and citation"
                )
            if not all(isinstance(entry[field], str) and entry[field].strip() for field in entry):
                raise DigestMismatchRefusal(
                    "model-store", "carried content fields must be nonblank text"
                )
            _safe(entry["path"], "carried path")
        if item["artifact"] == "dai-recordgold-atr":
            paths = {entry["path"] for entry in carried}
            if paths != {"system.txt", "query.txt"} or any(
                entry["citation"] != DAI_PROMPT_CITATION for entry in carried
            ):
                raise DigestMismatchRefusal(
                    "dai-recordgold-atr",
                    "DAI prompt files must be named carried content with their citation",
                )
        elif carried:
            raise DigestMismatchRefusal(
                item["artifact"], "only DAI prompt files are carried content in this roster"
            )
        required_files = item["required_files"]
        if (
            not isinstance(required_files, list)
            or not required_files
            or not all(isinstance(path, str) and path.strip() for path in required_files)
            or len(required_files) != len(set(required_files))
        ):
            raise DigestMismatchRefusal(
                item["artifact"], "required_files must be a nonempty list of unique paths"
            )
        for path in required_files:
            _safe(path, "required file")
        required_file_set = set(required_files)
        declared_requirements = {item["license"], *(entry["path"] for entry in carried)}
        if not declared_requirements <= required_file_set:
            missing = sorted(declared_requirements - required_file_set)
            raise DigestMismatchRefusal(
                item["artifact"],
                f"required_files omits mandatory licence or carried content: {missing}",
            )
        licence_name = PurePosixPath(item["license"]).name.lower()
        if (
            item["license"] not in SYNTHETIC_LICENCE_SNAPSHOTS
            and licence_name not in LICENCE_FILE_NAMES
        ):
            raise DigestMismatchRefusal(
                item["artifact"],
                f"license snapshot {item['license']!r} is neither a repository licence "
                "file nor a recognized synthetic observation",
            )
        if item["license"] in SYNTHETIC_LICENCE_SNAPSHOTS:
            requirement = next(
                (
                    required
                    for required in REQUIRED_ARTIFACTS
                    if required.artifact == item["artifact"]
                ),
                None,
            )
            if requirement is not None:
                expected = (
                    UNTEXTED_LICENCE_SNAPSHOT
                    if requirement.license_declaration is not None
                    else UNDECLARED_LICENCE_SNAPSHOT
                )
                if item["license"] != expected:
                    raise DigestMismatchRefusal(
                        item["artifact"],
                        f"synthetic licence evidence must be {expected!r} for this roster "
                        f"declaration, not {item['license']!r}",
                    )
                if MODEL_CARD_PATH not in required_files:
                    raise DigestMismatchRefusal(
                        item["artifact"],
                        f"synthetic licence evidence cites {MODEL_CARD_PATH}, which must "
                        "therefore be a required file",
                    )
        if not any(PurePosixPath(path).suffix in MODEL_PAYLOAD_SUFFIXES for path in required_files):
            raise DigestMismatchRefusal(
                item["artifact"],
                "required_files must name at least one model payload "
                f"with a supported suffix: {sorted(MODEL_PAYLOAD_SUFFIXES)}",
            )


def _verify_required_files(item: Mapping[str, Any], rows: Mapping[str, Any]) -> None:
    """Refuse an incomplete fetch even when its smaller manifest is self-consistent."""

    for path in item["required_files"]:
        row = rows.get(path)
        if row is None:
            raise DigestMismatchRefusal(
                item["artifact"],
                f"required file {path!r} is absent from its digest manifest",
            )
        if row.size == 0:
            raise DigestMismatchRefusal(item["artifact"], f"required file {path!r} is empty")


def _validate_origin(item: Mapping[str, Any]) -> None:
    """Check where an artifact comes from, which a pending entry knows as well.

    Source, repository and revision are decided when the roster is, not when the
    bytes land, so they are checked identically in both entry shapes — a
    pending-fetch entry that named no revision could not be reconciled against
    :data:`REQUIRED_ARTIFACTS` at all.
    """

    if item["source"] not in {"huggingface", "local-repository"}:
        raise DigestMismatchRefusal(
            "model-store",
            f"artifact {item['artifact']!r} has an invalid source or manifest pin",
        )
    if item["source"] == "huggingface":
        if not isinstance(item["repo"], str) or not is_hf_revision(item["revision"]):
            raise DigestMismatchRefusal(
                "model-store",
                f"Hugging Face artifact {item['artifact']!r} has no pinned repo and revision",
            )
    elif item["repo"] is not None or item["revision"] is not None:
        raise DigestMismatchRefusal(
            "model-store",
            f"local artifact {item['artifact']!r} must have no git pin",
        )


def _safe(value: object, label: str) -> None:
    """Refuse paths that do not name an object strictly below the store root."""

    if not isinstance(value, str) or not value:
        raise DigestMismatchRefusal("model-store", f"{label} is not a safe relative POSIX path")
    path = PurePosixPath(value)
    if path.is_absolute() or not path.parts or ".." in path.parts or "\\" in value:
        raise DigestMismatchRefusal("model-store", f"{label} is not a safe relative POSIX path")


def _is_irregular(path: Path) -> bool:
    """A link, or an existing name that is not a regular file."""
    return path.is_symlink() or (path.exists() and not path.is_file())


def _under(root: Path, relative: str) -> Path:
    _safe(relative, "store path")
    result = root / relative
    resolved = result.resolve()
    if root != resolved and root not in resolved.parents:
        raise DigestMismatchRefusal("model-store", "store path escapes configured root")
    candidate = root
    for part in PurePosixPath(relative).parts:
        candidate /= part
        if candidate.is_symlink():
            raise DigestMismatchRefusal(
                "model-store",
                f"store path {relative!r} must not traverse symlink component "
                f"{candidate.relative_to(root).as_posix()!r}",
            )
    return result
