"""Verification of the durable, off-repository model store.

This module deliberately has no downloader, HTTP client, or cache-fill path.  A
host operator materializes the pinned bytes once; consumers then use this module
to prove the store and its derived records still agree.  It never fetches, and
its few writers preserve every evidence version: inventories and manifests
publish once, while each download-record version is digest-addressed and only
its active copy moves. Differing evidence is never overwritten (GOVERNANCE 4).
The documented store root is
``/Users/tyrel/verbatus-models`` (for example only, never a default).
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from types import MappingProxyType
from typing import Any, Mapping

from common.contracts.canonical import canonical_bytes, digest_bytes

from .errors import DigestMismatchRefusal
from .manifests import (
    build_manifest,
    read_manifest,
    verify_snapshot,
)
from .models import ChairIdentity, is_hf_revision, is_sha256
from .registry import CACHE_DESCRIPTOR

STORE_SCHEMA = "verbatus-model-store.v1"
INVENTORY_SCHEMA = "verbatus-model-inventory.v1"

# An artifact entry is one of two closed shapes.  A store is materialized one
# snapshot at a time, so "every roster artifact is already on disk" is a state
# the host reaches, not the only state it may record: an entry the operator has
# not fetched yet is written in the `pending-fetch` shape, which names the
# absence and its reason instead of leaving the store unrepresentable until the
# last byte lands (GOVERNANCE 2 — a partial result is visibly partial).  The
# schema label stays `.v1`: no record has ever been written in the earlier
# shape, so there is no evidence on disk for a bump to protect.
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
RECORD_FIELDS = {"schema", "layout", "capacity", "artifacts"}
CAPACITY_FIELDS = {
    "snapshot_bytes",
    "promotion_headroom_bytes",
    "available_bytes",
    "cleanup_owner",
}


@dataclass(frozen=True, slots=True)
class RequiredArtifact:
    chair: str
    artifact: str
    source: str
    repo: str | None
    revision: str | None


# This is the roster policy, not a second copy of store facts.  The inventory is
# computed from download_record.json and refuses any disagreement with this list.
REQUIRED_ARTIFACTS = (
    RequiredArtifact(
        "designator_structure",
        "chandra-ocr-2",
        "huggingface",
        "datalab-to/chandra-ocr-2",
        "af93b47dba1b47b6640c86ccf487ed2260ab9a09",
    ),
    RequiredArtifact(
        "attestator_1",
        "chandra-ocr-2",
        "huggingface",
        "datalab-to/chandra-ocr-2",
        "af93b47dba1b47b6640c86ccf487ed2260ab9a09",
    ),
    RequiredArtifact(
        "attestator_2",
        "dai-recordgold-atr",
        "huggingface",
        "Teklia/Qwen2.5-VL-7B-DAI-CReTDHI-RecordGold-ATR",
        "e371095d4ffe585f31f4974462931ddbac61ff64",
    ),
    RequiredArtifact(
        "attestator_3",
        "churro-3B",
        "huggingface",
        "stanford-oval/churro-3B",
        "ca2150ea465d5a3d67818c50e234b9422619c75d",
    ),
    RequiredArtifact(
        "secondary_proposer",
        "yolo26-detection",
        "huggingface",
        "Teklia/YOLOv26-DAI-CReTDHI-Record-Detection",
        "0c57f057391113579e7af170b864542f049e67aa",
    ),
    RequiredArtifact("proposer_surya2", "surya2-detection", "local-repository", None, None),
    RequiredArtifact(
        "perlector",
        "qwen3.5-9B",
        "huggingface",
        "Qwen/Qwen3.5-9B",
        "c202236235762e1c871ad0ccb60c8ee5ba337b9a",
    ),
)
# The `chair` column above is a `config/models.toml` role key, not a label: an
# inventory whose chair names cannot be joined to the roster tells the operator
# who materializes the store nothing about which chair each snapshot serves.
# `test_model_store.py` reconciles the two lists, with exactly one recorded
# exception — Surya 2 has no roster role yet, and roster membership is Tyrel's
# decision at S8 (CLAUDE.md hard rule 1), so this store entry names the artifact
# R2's Designator geometry adapter is built against and waits for that word.
CHAIRS_WITHOUT_ROSTER_ROLE = MappingProxyType(
    {
        "proposer_surya2": (
            "config/models.toml configures no Surya detection chair, in its live "
            "fixture roster or its commented real roster; roster membership is "
            "Tyrel's decision at S8"
        )
    }
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


def load_download_record(store_root: str | Path) -> dict[str, Any]:
    """Load the canonical active record and prove its immutable version exists."""

    root = Path(store_root).resolve()
    active = root / "download_record.json"
    if active.is_symlink():
        raise DigestMismatchRefusal(
            "model-store", "download_record.json must be a regular in-store active copy"
        )
    try:
        raw_bytes = active.read_bytes()
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
    _validate_record(raw)
    digest = digest_bytes(raw_bytes)
    archive = _under(root, f"records/{digest}.json")
    if archive.is_symlink():
        raise DigestMismatchRefusal(
            "model-store", "immutable download record version must not be a symlink"
        )
    try:
        archived_bytes = archive.read_bytes()
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

    The host operator assembles ``record`` from what was actually fetched; this
    function only guarantees the bytes on disk are the exact canonical form
    :func:`load_download_record` requires, so a hand-formatted file never earns
    a "not canonical bytes" refusal that names no cause. A store evolves from
    ``pending-fetch`` to ``present``. Each canonical record is therefore
    published once at ``records/<sha256>.json``; only the active copy
    ``download_record.json`` moves. Previous record bytes remain at
    their digest-addressed names (GOVERNANCE 4), including the old ad-hoc record
    this migration replaces. A present artifact may never move backwards to
    pending-fetch: missing bytes after acquisition are fetched-and-lost, not
    not-yet-fetched.
    """

    _validate_record(record)
    root = Path(store_root).resolve()
    destination = root / "download_record.json"
    if destination.is_symlink():
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
            # Reuse only an already-custodied v1 record. A caller cannot make a
            # direct write authoritative merely by handing the same mapping to
            # this writer afterwards.
            _current_v1_record(root, previous_bytes)
            return digest
        previous_digest = digest_bytes(previous_bytes)
        previous = _current_v1_record(root, previous_bytes)
        if previous is not None:
            _validate_record_transition(previous, record)
        _publish_once(
            _under(root, f"records/{previous_digest}.json"),
            previous_bytes,
            chair="model-store",
            label="previous or legacy download record",
        )

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
        rows.append({"chair": required.chair, **item})
    # An inventory over a half-materialized store is a real inventory of a
    # partial store, never a complete one.  Both facts travel with it: each row
    # carries its own `state`, and `pending`/`complete` say at the top what a
    # consumer would otherwise have to rediscover by scanning rows.
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


def pod_materialization_plan(store_root: str | Path) -> dict[str, Any]:
    """Return a byte-verified source plan for materializing a complete pod cache.

    The store is "durable, off-repository, and shared with the future pod", and
    the two sides key their directories differently.  ``ChairRegistry`` reads a
    Hugging Face chair from ``cache_root/<role>`` — one directory per *role*,
    carrying the cache's own ``.chair-identity.json`` — while this store holds
    one directory per *artifact*, because chandra-ocr-2 fills two chairs at one
    revision and is stored once rather than twice.  So a pod materializes a
    role-keyed ``cache_root`` from this binding; it never points ``cache_root``
    at the store's ``hf/``, where five artifact-named directories satisfy none
    of the seven role lookups, and where the two chairs sharing chandra would
    write two different cache descriptors over one directory.

    ``model_root`` is the other half and is not interchangeable with the first:
    it is local-repository only and is resolved relative to
    ``config/models.toml``'s own directory (``registry._resolve_local_path``),
    so the Surya bundle is bound by a chair ``path`` beneath that root and never
    through any cache.

    This function states which roles need which half and where each one's bytes
    are in the store. It accepts a store root, not a caller-supplied record, and
    re-verifies every source byte before returning. A pending artifact therefore
    cannot satisfy it, and a claimed manifest digest is never enough by itself.

    It copies nothing and proves nothing about a pod: materializing a cache entry
    is a host action, the destination chair verifies its own pinned manifest, and
    only a :class:`ServingReceipt` proves which weights actually served a call.
    The explicit ``provenance_scope`` keeps this plan from masquerading as that
    receipt.
    """

    inventory = require_complete_store(store_root)
    cache_root_entries: dict[str, dict[str, Any]] = {}
    model_root_entries: dict[str, dict[str, Any]] = {}
    for row in inventory["artifacts"]:
        source = {
            "artifact": row["artifact"],
            "snapshot": row["snapshot"],
            "manifest": row["manifest"],
            "digest_manifest": row["digest_manifest"],
            "revision": row["revision"],
        }
        if row["source"] == "huggingface":
            cache_root_entries[row["chair"]] = source
        else:
            model_root_entries[row["chair"]] = source
    return {
        "provenance_scope": "verified-store-source-only",
        "download_record_sha256": inventory["download_record_sha256"],
        "cache_root_entries": cache_root_entries,
        "model_root_entries": model_root_entries,
    }


def require_complete_store(store_root: str | Path) -> dict[str, Any]:
    """Verify the store's real bytes and refuse a partial result by name.

    ``verify_store`` proves the bytes that exist; it never invents the ones that
    do not, so it returns a partial inventory rather than refusing outright.
    This is the door for a consumer that genuinely needs every roster artifact
    on disk — S8 roster activation or a pod materialization plan. It accepts the
    store root rather than an inventory-shaped mapping so a caller cannot flip a
    derived ``complete`` flag while bytes are pending or missing.
    """

    inventory = verify_store(store_root)
    if not inventory["complete"]:
        raise DigestMismatchRefusal(
            "model-store",
            f"model store is not complete; still pending fetch: {', '.join(inventory['pending'])}",
        )
    return inventory


def require_store_artifact(store_root: str | Path, artifact: str) -> dict[str, Any]:
    """Return a byte-verified present artifact or refuse its exact absence class."""

    if artifact == SURYA_OCR_2_REFUSAL["artifact"]:
        raise DigestMismatchRefusal(
            artifact,
            f"artifact is {SURYA_OCR_2_REFUSAL['state']}: "
            f"{SURYA_OCR_2_REFUSAL['reason']}; the only escape hatch is a "
            f"{SURYA_OCR_2_REFUSAL['escape_hatch']}",
        )
    inventory = verify_store(store_root)
    rows = [row for row in inventory["artifacts"] if row["artifact"] == artifact]
    if not rows:
        raise DigestMismatchRefusal(artifact, "artifact is not part of the required roster")
    if rows[0]["state"] == "pending-fetch":
        raise DigestMismatchRefusal(artifact, f"artifact is pending-fetch: {rows[0]['reason']}")
    return rows[0]


def write_derived_inventory(record: Mapping[str, Any], path: str | Path) -> str:
    """Publish a derived record once; readers must call :func:`read_derived_inventory`.

    Identical bytes already at ``path`` are reused silently; differing bytes are
    refused and the existing file is left untouched (GOVERNANCE 4 — evidence is
    never overwritten).
    """

    payload = canonical_bytes(derived_inventory(record))
    _publish_once(Path(path), payload, chair="model-store", label="derived inventory")
    return digest_bytes(payload)


def read_derived_inventory(store_root: str | Path, path: str | Path) -> dict[str, Any]:
    """Refuse an inventory not backed by the store's current verified bytes."""

    try:
        actual = Path(path).read_bytes()
    except OSError as error:
        raise DigestMismatchRefusal(
            "model-store", f"cannot read derived inventory: {error}"
        ) from error
    expected = canonical_bytes(verify_store(store_root))
    if actual != expected:
        raise DigestMismatchRefusal(
            "model-store", "derived inventory diverges from download_record.json"
        )
    return json.loads(actual)


def verify_store(store_root: str | Path) -> dict[str, Any]:
    """Verify every declared manifest against its existing bytes; never fetch.

    A `pending-fetch` entry has no bytes to verify, so it is passed over and
    reported: the returned inventory is then a verified inventory of a
    *partial* store, marked `complete: false`.  Call
    :func:`require_complete_store` where every roster artifact must be on disk.
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
        manifest_path = _under(root, item["manifest"])
        manifest = read_manifest(
            manifest_path, expected_digest=item["digest_manifest"], chair=item["artifact"]
        )
        rows = {row.path: row for row in manifest.rows}
        _verify_required_files(item, rows)
        license_row = rows.get(item["license"])
        if license_row is None:
            raise DigestMismatchRefusal(
                item["artifact"], "license snapshot is absent from its digest manifest"
            )
        # U17 asks for the licence *text* of the pinned revision. An empty file
        # satisfies "a licence snapshot exists" and carries no terms at all, and
        # three of this roster's licences are the reason the check is here:
        # revenue-capped, non-commercial, and one repository that declares none.
        if license_row.size == 0:
            raise DigestMismatchRefusal(
                item["artifact"],
                f"license snapshot {item['license']!r} is empty; the pinned revision's "
                "licence text is the artifact, not a file of that name",
            )
        for carried in item["carried"]:
            if carried["path"] not in rows:
                raise DigestMismatchRefusal(
                    item["artifact"],
                    f"carried content {carried['path']!r} is absent from its digest manifest",
                )
        snapshot = _under(root, item["snapshot"])
        if (snapshot / CACHE_DESCRIPTOR).exists():
            raise DigestMismatchRefusal(
                item["artifact"],
                f"the chair registry's cache descriptor {CACHE_DESCRIPTOR!r} is inside this "
                "store snapshot: a store directory is keyed by artifact and is not a "
                "cache_root entry, which is keyed by chair role. Materialize "
                "cache_root/<role> from this snapshot instead — see "
                "pod_materialization_plan",
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
    return inventory


def promote_verified_snapshot(store_root: str | Path, artifact: Mapping[str, Any]) -> str:
    """Host-side promotion primitive: hash a staged snapshot, publish its manifest once.

    This is where a pin is *born*, and it is the one place in this package that
    derives one from bytes rather than checking bytes against one.  That is not
    an exception to "a pin is a constant the artifact must match" (harvest #43,
    `README.md`): the first manifest of a fetch has nothing to be checked
    against, which is why `config/models.toml` leaves `digest_manifest` unfilled
    until a verified fetch exists.  Every later use of that manifest — a second
    promotion, `verify_store`, `ChairRegistry.ensure` — is a constant the
    artifact must match, and a second promotion of differing bytes is refused
    below rather than repinned.

    The caller supplies an already-created staging directory.  This function does
    not copy or download bytes; capacity must therefore reserve source + staging
    space before it is called.  Publication follows the rest of this module's
    custody rule (GOVERNANCE 4 — evidence is never overwritten): identical bytes
    already published are reused silently, a differing manifest already at that
    name is refused, and the existing file is never touched either way. A picked
    manifest name is a pin, not a rolling pointer a second promotion may rewrite.
    """

    root = Path(store_root).resolve()
    staging = _under(root, artifact["staging"])
    manifest = build_manifest(staging)
    _verify_required_files(artifact, {row.path: row for row in manifest.rows})
    destination = _under(root, artifact["manifest"])
    payload = canonical_bytes(manifest.to_record())
    _publish_once(destination, payload, chair="model-store", label="verified manifest")
    return digest_bytes(payload)


def _publish_once(destination: Path, payload: bytes, *, chair: str, label: str) -> None:
    """Publish ``payload`` at ``destination`` without ever overwriting a difference.

    A hard link is an atomic create on the target filesystem: the temporary is
    fully written first, then either acquires the final name or the link raises
    ``FileExistsError`` without touching the competing file. Reused verbatim from
    the identical-bytes-reuse, differing-bytes-refusal custody rule
    ``common/runtree/store.py::_atomic_create`` already applies to run artifacts,
    so the model store's evidence keeps the one rule this system settled on.

    Every filesystem failure along that path is a refusal against a named chair,
    not a bare ``OSError``: a read-only store, a full disk, and a name already
    taken by a directory are exactly the conditions this writer exists to fail
    on, and ``errors.py`` calls its list "the complete public taxonomy" —
    ``manifests.file_size`` records the same reasoning for the read side.
    """

    temporary = destination.with_name(f".{destination.name}.candidate-{os.getpid()}")
    try:
        destination.parent.mkdir(parents=True, exist_ok=True)
        temporary.write_bytes(payload)
        try:
            os.link(temporary, destination)
        except FileExistsError:
            # `read_bytes` may raise in its own right — the taken name can be a
            # directory — and that is caught below as the publication failure
            # it is.
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
    finally:
        temporary.unlink(missing_ok=True)


def _move_active_record(destination: Path, archive: Path) -> None:
    """Atomically point the active name at an already-immutable record version."""

    temporary = destination.with_name(f".{destination.name}.active-{os.getpid()}")
    try:
        destination.parent.mkdir(parents=True, exist_ok=True)
        temporary.unlink(missing_ok=True)
        temporary.write_bytes(archive.read_bytes())
        os.replace(temporary, destination)
    except OSError as error:
        raise DigestMismatchRefusal(
            "model-store", f"cannot publish active download_record.json: {error}"
        ) from error
    finally:
        temporary.unlink(missing_ok=True)


def _current_v1_record(root: Path, raw_bytes: bytes) -> dict[str, Any] | None:
    """Return a current v1 record, while allowing the one legacy migration input."""

    try:
        raw = json.loads(raw_bytes)
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None
    if isinstance(raw, Mapping) and raw.get("schema") == STORE_SCHEMA:
        # This also proves canonical bytes and the immutable archived version. A
        # damaged v1 record is not silently treated as legacy and replaced.
        return load_download_record(root)
    return None


def _validate_record_transition(
    previous: Mapping[str, Any], replacement: Mapping[str, Any]
) -> None:
    """Keep not-yet-fetched distinct from fetched-and-lost across versions."""

    old = {item["artifact"]: item for item in previous["artifacts"]}
    new = {item["artifact"]: item for item in replacement["artifacts"]}
    for artifact, old_item in old.items():
        replacement_item = new.get(artifact)
        if replacement_item is None:
            # Both records hold exactly six unique artifacts, so a name the
            # replacement does not carry was renamed or swapped for another.
            # Reading `new[artifact]` here raised a bare `KeyError` that named
            # no chair, which is the one thing `errors.py`'s "complete public
            # taxonomy" forbids; an artifact leaving the record silently is
            # also what GOVERNANCE 2 forbids.
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
    if set(raw) != RECORD_FIELDS:
        missing = sorted(RECORD_FIELDS - set(raw), key=str)
        unexpected = sorted(set(raw) - RECORD_FIELDS, key=str)
        raise DigestMismatchRefusal(
            "model-store",
            "download record has an invalid top-level shape: it carries exactly "
            f"{sorted(RECORD_FIELDS)}; missing={missing}, unexpected={unexpected}",
        )
    if raw["schema"] != STORE_SCHEMA:
        raise DigestMismatchRefusal(
            "model-store",
            f"download record schema must be {STORE_SCHEMA!r}, not {raw['schema']!r}",
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
    # `capacity` is a self-declared plan, exactly like a ServingProfile's GPU
    # figures (operations/serving/config.py) are "configuration/planning values,
    # never a claim that an unmeasured card can sustain them" (GOVERNANCE 10).
    # Nothing here calls `shutil.disk_usage`: this module never fetches or
    # touches the host beyond the bytes a chair's manifest already names, so it
    # has no more standing to assert free disk space than a human operator's
    # own accounting does. The arithmetic below only catches an internally
    # inconsistent plan (headroom or availability that contradicts itself); the
    # actual backstop against a full disk is `_publish_once` failing loudly when
    # a write really cannot complete.
    capacity = raw["capacity"]
    if (
        not isinstance(capacity, Mapping)
        or set(capacity) != CAPACITY_FIELDS
        or not all(
            isinstance(capacity[key], int)
            and not isinstance(capacity[key], bool)
            and capacity[key] >= 0
            for key in ("snapshot_bytes", "promotion_headroom_bytes", "available_bytes")
        )
        or not isinstance(capacity["cleanup_owner"], str)
        or not capacity["cleanup_owner"].strip()
    ):
        raise DigestMismatchRefusal(
            "model-store",
            "capacity must record bytes, double-space headroom, and cleanup owner: exactly "
            f"{sorted(CAPACITY_FIELDS)}, the first three nonnegative integers and the last "
            "a nonblank name",
        )
    if capacity["promotion_headroom_bytes"] < capacity["snapshot_bytes"]:
        raise DigestMismatchRefusal(
            "model-store", "promotion headroom must reserve a second full snapshot"
        )
    if (
        capacity["available_bytes"]
        < capacity["snapshot_bytes"] + capacity["promotion_headroom_bytes"]
    ):
        raise DigestMismatchRefusal(
            "model-store", "available capacity cannot cover verified promotion double-space"
        )
    items = raw["artifacts"]
    if not isinstance(items, list) or len(items) != 6:
        raise DigestMismatchRefusal(
            "model-store",
            "download record must name exactly six unique roster artifacts, each either "
            "present or pending-fetch",
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
        # The artifact name is a path component in both entry shapes: a present
        # entry's snapshot and manifest are built from it below, and
        # `verify_store` builds a pending entry's absent-evidence paths from it
        # with no other field to check.  Only the roster join in
        # `derived_inventory` keeps it to a known name today, and this validator
        # is reached without that join (`write_download_record`), so the name is
        # held to the same path rule as every other path in the record.
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
        # The record declares a named-root layout and then had to be obeyed for
        # snapshots only, which left `manifests` decorative: a manifest could sit
        # anywhere under the root, including beside a snapshot it does not
        # describe. A layout half-enforced is a layout a consumer cannot rely on.
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
        declared_requirements = {item["license"], *(entry["path"] for entry in carried)}
        if not declared_requirements <= set(required_files):
            missing = sorted(declared_requirements - set(required_files))
            raise DigestMismatchRefusal(
                item["artifact"],
                f"required_files omits mandatory licence or carried content: {missing}",
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
    """Refuse anything that is not a relative POSIX path strictly under the root.

    ``manifests._safe_relative`` is the same rule for manifest rows, and it also
    refuses ``"."``.  This copy did not: ``"."`` has no path parts, so it passed
    every clause and named the store root itself — ``_under(root, ".")`` returns
    the root, and ``_publish_once`` would then write its temporary as a *sibling*
    of the root rather than inside it.  No field this guards may legitimately be
    the root, so the two spellings of the one rule now agree.
    """

    if (
        not isinstance(value, str)
        or not value
        or PurePosixPath(value).is_absolute()
        or not PurePosixPath(value).parts
        or ".." in PurePosixPath(value).parts
        or "\\" in value
    ):
        raise DigestMismatchRefusal("model-store", f"{label} is not a safe relative POSIX path")


def _under(root: Path, relative: str) -> Path:
    _safe(relative, "store path")
    result = (root / relative).resolve()
    if root != result and root not in result.parents:
        raise DigestMismatchRefusal("model-store", "store path escapes configured root")
    return result
