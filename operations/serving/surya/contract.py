"""What Surya's runner and the project agree on: the weight bundle and the page document.

The bundle holds the three checkpoints Surya loads, fetched once and locked.

Surya fetches its detection weights from Datalab's model host and its layout and
reading-order weights from the Hugging Face Hub, each time into its own cache.
`prefetch.py` fetches them once into one directory, the bundle, and writes
`surya-bundle.json` beside them: where each checkpoint came from, at which
revision, and the digest of every file. `runner.py` loads only a bundle whose
files still match that lock, so a run can never load weights nobody recorded.

The runner writes one page document per page (`PAGE_SCHEMA`), and the
project's `operations/serving/surya_detector.py` checks it against a closed
shape before the Designator reads it.

Standard library only: the project environment loads this file too.
"""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import Any

PAGE_SCHEMA = "verbatus-surya-page.v1"
# The Surya settings that shape what the two detectors return. The runner
# refuses to run unless each holds Surya's own default, and records them.
OUTPUT_SETTINGS = (
    "DETECTOR_BATCH_SIZE",
    "DETECTOR_IMAGE_CHUNK_HEIGHT",
    "DETECTOR_TEXT_THRESHOLD",
    "DETECTOR_BLANK_THRESHOLD",
    "DETECTOR_BOX_Y_EXPAND_MARGIN",
    "FAST_LAYOUT_BATCH_SIZE",
    "FAST_LAYOUT_CONFIDENCE_THRESHOLD",
    "FAST_LAYOUT_CONTAINMENT_THRESHOLD",
    "FAST_LAYOUT_USE_ORDER",
)

BUNDLE_FILE = "surya-bundle.json"
BUNDLE_SCHEMA = "verbatus-surya-bundle.v1"
# Surya's three checkpoint settings, and the keys the bundle files them under.
CHECKPOINT_SETTINGS = {
    "text_detection": "DETECTOR_MODEL_CHECKPOINT",
    "layout": "FAST_LAYOUT_MODEL_CHECKPOINT",
    "order": "FAST_ORDER_MODEL_CHECKPOINT",
}
# The Hub commit of `datalab-to/surya_layout2` this bundle is fetched at, which
# holds both the layout and the reading-order checkpoints. Moving it is a
# reviewed change, never a fetch-time choice. Text detection has no Hub commit:
# Surya's own default names a dated path on Datalab's model host, and the
# bundle's file digests are what pin it.
LAYOUT_REPOSITORY_REVISION = "0aee81d5fd9275c0582e545bf3a56944b1e75679"
_BUNDLE_FIELDS = {"schema", "surya_ocr", "checkpoints", "files"}
_CHECKPOINT_FIELDS = {"source", "revision", "path"}
_FILE_FIELDS = {"path", "sha256", "size"}
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_READ_CHUNK = 1 << 20


class BundleRefusal(RuntimeError):
    """A bundle that is missing, malformed, or no longer matches its own lock."""


def file_rows(root: Path) -> list[dict[str, Any]]:
    """Every regular file under the bundle except the lock itself, sorted by path.

    A symbolic link is refused: the lock names bytes, and a link can change
    what it points at without any file here changing.
    """
    rows = []
    for path in sorted(root.rglob("*")):
        relative = path.relative_to(root).as_posix()
        if path.is_symlink():
            raise BundleRefusal(f"bundle entry {relative!r} is a symbolic link")
        if path.is_dir() or relative == BUNDLE_FILE:
            continue
        if not path.is_file():
            raise BundleRefusal(f"bundle entry {relative!r} is not a regular file")
        digest = hashlib.sha256()
        with path.open("rb") as handle:
            while chunk := handle.read(_READ_CHUNK):
                digest.update(chunk)
        rows.append({"path": relative, "sha256": digest.hexdigest(), "size": path.stat().st_size})
    return rows


def bundle_record(
    root: Path, *, surya_ocr: str, checkpoints: dict[str, dict[str, Any]]
) -> dict[str, Any]:
    """The lock for the bundle as it stands on disk now."""
    record = {
        "schema": BUNDLE_SCHEMA,
        "surya_ocr": surya_ocr,
        "checkpoints": checkpoints,
        "files": file_rows(root),
    }
    _check_record(record, root)
    return record


def bundle_bytes(record: dict[str, Any]) -> bytes:
    return (json.dumps(record, sort_keys=True, indent=2) + "\n").encode("utf-8")


def read_bundle(root: Path) -> dict[str, Any]:
    """The lock, checked against every file in the bundle, or a refusal naming why."""
    lock = root / BUNDLE_FILE
    try:
        record = json.loads(lock.read_bytes())
    except (OSError, ValueError) as error:
        raise BundleRefusal(f"{lock} cannot be read as a bundle lock: {error}") from error
    _check_record(record, root)
    observed = file_rows(root)
    if observed != record["files"]:
        locked = {row["path"]: row for row in record["files"]}
        found = {row["path"]: row for row in observed}
        missing = sorted(set(locked) - set(found))
        extra = sorted(set(found) - set(locked))
        changed = sorted(p for p in set(locked) & set(found) if locked[p] != found[p])
        raise BundleRefusal(
            f"the bundle at {root} no longer matches its lock: missing={missing}, "
            f"unlocked={extra}, changed={changed}"
        )
    return record


def _check_record(record: Any, root: Path) -> None:
    if not isinstance(record, dict) or set(record) != _BUNDLE_FIELDS:
        raise BundleRefusal(f"the bundle lock at {root} is not the {BUNDLE_SCHEMA} shape")
    if record["schema"] != BUNDLE_SCHEMA:
        raise BundleRefusal(f"the bundle lock at {root} names schema {record['schema']!r}")
    if not isinstance(record["surya_ocr"], str) or not record["surya_ocr"]:
        raise BundleRefusal("the bundle lock names no surya-ocr version")
    checkpoints = record["checkpoints"]
    if not isinstance(checkpoints, dict) or set(checkpoints) != set(CHECKPOINT_SETTINGS):
        raise BundleRefusal(
            f"the bundle lock must name exactly the checkpoints {sorted(CHECKPOINT_SETTINGS)}"
        )
    for name, checkpoint in checkpoints.items():
        if not isinstance(checkpoint, dict) or set(checkpoint) != _CHECKPOINT_FIELDS:
            raise BundleRefusal(f"bundle checkpoint {name!r} is not source, revision and path")
        source, revision, path = checkpoint["source"], checkpoint["revision"], checkpoint["path"]
        if not isinstance(source, str) or not source.startswith(("s3://", "hf://")):
            raise BundleRefusal(f"bundle checkpoint {name!r} names no s3:// or hf:// source")
        # The Hub is addressed by commit; Datalab's model host by its dated path alone.
        if source.startswith("hf://") and revision != LAYOUT_REPOSITORY_REVISION:
            raise BundleRefusal(
                f"bundle checkpoint {name!r} is at {revision!r}, not the pinned Hub commit "
                f"{LAYOUT_REPOSITORY_REVISION}"
            )
        if source.startswith("s3://") and revision is not None:
            raise BundleRefusal(f"bundle checkpoint {name!r} names a revision its host has none of")
        if (
            not isinstance(path, str)
            or not path
            or path.startswith("/")
            or ".." in path.split("/")
            or not (root / path).is_dir()
        ):
            raise BundleRefusal(f"bundle checkpoint {name!r} path {path!r} is not a bundle folder")
    files = record["files"]
    if not isinstance(files, list):
        raise BundleRefusal("the bundle lock lists no files")
    for row in files:
        if (
            not isinstance(row, dict)
            or set(row) != _FILE_FIELDS
            or not isinstance(row["path"], str)
            or not isinstance(row["sha256"], str)
            or not _SHA256.fullmatch(row["sha256"])
            or not isinstance(row["size"], int)
            or isinstance(row["size"], bool)
            or row["size"] < 0
        ):
            raise BundleRefusal(f"the bundle lock carries a malformed file row {row!r}")
