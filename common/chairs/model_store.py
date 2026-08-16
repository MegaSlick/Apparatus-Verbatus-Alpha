"""Verification of the durable, off-repository model store.

This module deliberately has no downloader, HTTP client, or cache-fill path.  A
host operator materializes the pinned bytes once; consumers then use this module
to prove the store and its derived records still agree.  It never fetches, and
its few writers (the canonical download record, the derived inventory, a
promoted manifest) only ever publish once: identical bytes already on disk are
reused silently, differing bytes are refused, and an existing file is never
overwritten (GOVERNANCE 4).  The documented store root is
``/Users/tyrel/verbatus-models`` (for example only, never a default).
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, Mapping

from common.contracts.canonical import canonical_bytes, digest_bytes

from .errors import DigestMismatchRefusal
from .manifests import (
    build_manifest,
    read_manifest,
    verify_snapshot,
)
from .models import ChairIdentity, is_hf_revision, is_sha256

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
}
PENDING_FIELDS = {"artifact", "state", "source", "repo", "revision", "reason"}


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
CHAIRS_WITHOUT_ROSTER_ROLE = {
    "proposer_surya2": (
        "config/models.toml configures no Surya detection chair, in its live "
        "fixture roster or its commented real roster; roster membership is "
        "Tyrel's decision at S8"
    )
}
SURYA_OCR_2_REFUSAL = {
    "artifact": "surya-ocr-2",
    "state": "not-fetched",
    "reason": "detector only; no OCR artifact is needed",
    "escape_hatch": "recorded-bench-need",
}
DAI_PROMPT_CITATION = (
    "Teklia, Qwen2.5-VL-7B-DAI-CReTDHI-RecordGold-ATR, pinned repository files "
    "system.txt and query.txt"
)


def load_download_record(store_root: str | Path) -> dict[str, Any]:
    """Load the canonical host record without contacting any remote service."""

    root = Path(store_root)
    try:
        raw_bytes = (root / "download_record.json").read_bytes()
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
    return raw


def write_download_record(record: Mapping[str, Any], store_root: str | Path) -> str:
    """Publish the host record in its one canonical serialization, once.

    The host operator assembles ``record`` from what was actually fetched; this
    function only guarantees the bytes on disk are the exact canonical form
    :func:`load_download_record` requires, so a hand-formatted file never earns
    a "not canonical bytes" refusal that names no cause. Publication follows the
    rest of this module's custody rule (GOVERNANCE 4): identical bytes already
    on disk are reused silently, differing bytes are refused, and an existing
    file is never overwritten.
    """

    _validate_record(record)
    destination = Path(store_root) / "download_record.json"
    payload = canonical_bytes(record)
    _publish_once(destination, payload, chair="model-store", label="download_record.json")
    return digest_bytes(payload)


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
                    "model-store", f"{required.artifact!r} {field} diverges from roster policy"
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
        "refusals": [SURYA_OCR_2_REFUSAL],
    }


def require_complete_store(inventory: Mapping[str, Any]) -> None:
    """Refuse an inventory that is honest about being partial, by name.

    ``verify_store`` proves the bytes that exist; it never invents the ones that
    do not, so it returns a partial inventory rather than refusing outright.
    This is the door for the consumer that genuinely needs every roster artifact
    on disk — S8 roster activation, a pod assembly — and it names each artifact
    still to be fetched rather than reporting a count.
    """

    if not inventory.get("complete"):
        raise DigestMismatchRefusal(
            "model-store",
            "model store is not complete; still pending fetch: "
            f"{', '.join(inventory.get('pending') or ['(unknown)'])}",
        )


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
    """Refuse an inventory that merely claims, rather than derives, store facts."""

    record = load_download_record(store_root)
    try:
        actual = Path(path).read_bytes()
    except OSError as error:
        raise DigestMismatchRefusal(
            "model-store", f"cannot read derived inventory: {error}"
        ) from error
    expected = canonical_bytes(derived_inventory(record))
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
            # Nothing to verify and nothing to claim: the absence is already
            # named in the record and travels out in the inventory's `pending`.
            continue
        manifest_path = _under(root, item["manifest"])
        manifest = read_manifest(
            manifest_path, expected_digest=item["digest_manifest"], chair=item["artifact"]
        )
        if item["license"] not in {row.path for row in manifest.rows}:
            raise DigestMismatchRefusal(
                item["artifact"], "license snapshot is absent from its digest manifest"
            )
        for carried in item["carried"]:
            if carried["path"] not in {row.path for row in manifest.rows}:
                raise DigestMismatchRefusal(
                    item["artifact"],
                    f"carried content {carried['path']!r} is absent from its digest manifest",
                )
        snapshot = _under(root, item["snapshot"])
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
    """Host-side promotion primitive: stage, verify, then publish a manifest once.

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
    """

    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(f".{destination.name}.candidate-{os.getpid()}")
    try:
        temporary.write_bytes(payload)
        try:
            os.link(temporary, destination)
        except FileExistsError:
            if destination.read_bytes() != payload:
                raise DigestMismatchRefusal(
                    chair,
                    f"{label} {destination} already exists with different bytes; "
                    "publication never overwrites existing evidence",
                ) from None
        except OSError as error:
            raise DigestMismatchRefusal(chair, f"cannot publish {label}: {error}") from error
    finally:
        temporary.unlink(missing_ok=True)


def _validate_record(raw: Mapping[str, Any]) -> None:
    if not isinstance(raw, Mapping) or set(raw) != {"schema", "layout", "capacity", "artifacts"}:
        raise DigestMismatchRefusal("model-store", "download record has an invalid top-level shape")
    if raw["schema"] != STORE_SCHEMA:
        raise DigestMismatchRefusal(
            "model-store", f"download record schema must be {STORE_SCHEMA!r}"
        )
    if raw["layout"] != {
        "hf": "hf",
        "local": "local",
        "manifests": "manifests",
        "staging": "staging",
    }:
        raise DigestMismatchRefusal(
            "model-store", "store layout must name hf, local, manifests, and staging roots"
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
        or set(capacity)
        != {"snapshot_bytes", "promotion_headroom_bytes", "available_bytes", "cleanup_owner"}
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
            "model-store", "capacity must record bytes, double-space headroom, and cleanup owner"
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
        if not isinstance(item.get("artifact"), str) or item["artifact"] in seen:
            raise DigestMismatchRefusal("model-store", "artifact names must be unique")
        seen.add(item["artifact"])
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
        prefix = "hf/" if item["source"] == "huggingface" else "local/"
        if not str(item["snapshot"]).startswith(prefix):
            raise DigestMismatchRefusal(
                "model-store",
                f"artifact {item['artifact']!r} is sourced from {item['source']!r} and its "
                f"snapshot must live under {prefix!r}",
            )
        for field in ("snapshot", "manifest", "license"):
            _safe(item[field], field)
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
    if (
        not isinstance(value, str)
        or not value
        or PurePosixPath(value).is_absolute()
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
