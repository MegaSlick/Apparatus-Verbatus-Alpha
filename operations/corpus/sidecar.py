"""The per-page sidecar: `recordgold-page-records.v1`, sealed outside the folder.

`SPEC.md` §5.1 fixes this shape and one hard rule about it: **never store the
submission ordinal.** The join key back to the sealed Exemplar page is
`page.sha256` — which for a single-frame JPEG on the `admit-or-fan-out` route
equals the admitted source digest (`pipeline/1_exemplar/HANDOFF.md`) — and that
digest survives re-sharding. An ordinal does not: it is assigned from sorted
`relative_path` at admission time (`door.py:719`), so a folder rebuilt with a
different partition would silently rebind which sidecar names which page. This
module's field set is closed and checked on every build and every load, which is
what makes "never carries an ordinal" a property this package enforces rather than
a habit a future edit could quietly break.

This sidecar carries no `physical_act_id` and no comparator-facing identity — that
ladder (`pac_` identities, §5.3) belongs to U4's `reference.py`, which reads the
fetch plan for it. This file only records what a submission actually sealed: the
fetched bytes' own facts, and each record's split, region, and expert text, exactly
as `SPEC.md` §5.1 lists them.
"""

from pathlib import Path
from typing import Any

from common.contracts.canonical import (
    canonical_bytes,
    digest_bytes,
    is_sha256,
    self_hash,
    verify_self_hash,
)

from . import CorpusRefusal
from .rows import CORPUS_ID, SPLITS

SCHEMA = "recordgold-page-records.v1"

SIDECAR_REFUSAL_REASONS = frozenset(
    {
        "malformed-record",
        "wrong-schema",
        "wrong-corpus",
        "unknown-split",
        "self-hash-mismatch",
        "region-outside-page",
    }
)

_IIIF_FIELDS = frozenset(
    {
        "identifier",
        "info_url",
        "image_url",
        "size_parameter",
        "response_sha256",
        "bytes",
        "http_status",
        "fetched_at_utc",
        "declared_width",
        "declared_height",
    }
)
_PAGE_FIELDS = frozenset({"sha256", "width", "height"})
_REGION_FIELDS = frozenset({"x", "y", "w", "h"})
_RECORD_FIELDS = frozenset(
    {
        "record_id",
        "split",
        "region",
        "text",
        "text_sha256",
        "start_date",
        "end_date",
        "parish",
    }
)
_TOP_FIELDS = frozenset(
    {
        "schema",
        "corpus_id",
        "source",
        "volume_id",
        "designation",
        "iiif",
        "page",
        "splits_present",
        "records",
        "self_hash",
    }
)


def _closed(value: Any, fields: frozenset[str], what: str) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != fields:
        raise CorpusRefusal(f"malformed-record: {what} must be the closed record {sorted(fields)}")
    return value


def _non_empty_str(value: Any, what: str) -> str:
    if not isinstance(value, str) or not value:
        raise CorpusRefusal(f"malformed-record: {what} must be a non-empty string")
    return value


def _non_negative_int(value: Any, what: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise CorpusRefusal(f"malformed-record: {what} must be a non-negative integer")
    return value


def _positive_int(value: Any, what: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
        raise CorpusRefusal(f"malformed-record: {what} must be a positive integer")
    return value


def build_sidecar(
    *,
    source: str,
    volume_id: str,
    designation: str,
    iiif: dict[str, Any],
    page: dict[str, Any],
    splits_present: list[str],
    records: list[dict[str, Any]],
) -> dict[str, Any]:
    """Seal one page's fetch facts and record truth into a validated sidecar.

    `records` carries exactly the closed per-record fields this schema names — no
    `physical_act_id`, no ordinal, nothing this module did not itself validate.
    """
    body = {
        "schema": SCHEMA,
        "corpus_id": CORPUS_ID,
        "source": source,
        "volume_id": volume_id,
        "designation": designation,
        "iiif": iiif,
        "page": page,
        "splits_present": sorted(set(splits_present)),
        "records": sorted(records, key=lambda record: record["record_id"]),
    }
    body["self_hash"] = self_hash(body)
    return validate_sidecar(body)


def validate_sidecar(sidecar: Any) -> dict[str, Any]:
    """Refuse a sidecar that is not exactly `recordgold-page-records.v1`, closed and self-consistent."""
    sidecar = _closed(sidecar, _TOP_FIELDS, "sidecar")
    if sidecar["schema"] != SCHEMA:
        raise CorpusRefusal(f"wrong-schema: expected {SCHEMA!r}, got {sidecar['schema']!r}")
    if sidecar["corpus_id"] != CORPUS_ID:
        raise CorpusRefusal(f"wrong-corpus: expected {CORPUS_ID!r}, got {sidecar['corpus_id']!r}")

    _non_empty_str(sidecar["source"], "source")
    _non_empty_str(sidecar["volume_id"], "volume_id")
    _non_empty_str(sidecar["designation"], "designation")

    iiif = _closed(sidecar["iiif"], _IIIF_FIELDS, "iiif")
    for field in ("identifier", "info_url", "image_url", "size_parameter", "fetched_at_utc"):
        _non_empty_str(iiif[field], f"iiif.{field}")
    if not is_sha256(iiif["response_sha256"]):
        raise CorpusRefusal(
            "malformed-record: iiif.response_sha256 must be a lowercase sha256 hex digest"
        )
    _non_negative_int(iiif["bytes"], "iiif.bytes")
    _non_negative_int(iiif["http_status"], "iiif.http_status")
    _non_negative_int(iiif["declared_width"], "iiif.declared_width")
    _non_negative_int(iiif["declared_height"], "iiif.declared_height")

    page = _closed(sidecar["page"], _PAGE_FIELDS, "page")
    if not is_sha256(page["sha256"]):
        raise CorpusRefusal("malformed-record: page.sha256 must be a lowercase sha256 hex digest")
    page_width = _positive_int(page["width"], "page.width")
    page_height = _positive_int(page["height"], "page.height")

    splits_present = sidecar["splits_present"]
    if not isinstance(splits_present, list) or not all(
        isinstance(split, str) for split in splits_present
    ):
        raise CorpusRefusal("malformed-record: splits_present must be a list of strings")
    if splits_present != sorted(set(splits_present)):
        raise CorpusRefusal("malformed-record: splits_present must be a sorted, deduplicated list")
    for split in splits_present:
        if split not in SPLITS:
            raise CorpusRefusal(f"unknown-split: sidecar names unknown split {split!r}")

    records = sidecar["records"]
    if not isinstance(records, list) or not records:
        raise CorpusRefusal("malformed-record: sidecar must carry at least one record")
    seen_record_ids: set[str] = set()
    referenced_splits: set[str] = set()
    for record in records:
        record = _closed(record, _RECORD_FIELDS, "sidecar record")
        record_id = record["record_id"]
        _non_empty_str(record_id, "record.record_id")
        if record_id in seen_record_ids:
            raise CorpusRefusal(
                f"malformed-record: record_id {record_id!r} appears more than once in the sidecar"
            )
        seen_record_ids.add(record_id)
        if record["split"] not in SPLITS:
            raise CorpusRefusal(
                f"unknown-split: record {record_id!r} names unknown split {record['split']!r}"
            )
        referenced_splits.add(record["split"])
        region = _closed(record["region"], _REGION_FIELDS, f"record {record_id!r} region")
        if region["x"] < 0 or region["y"] < 0 or region["w"] <= 0 or region["h"] <= 0:
            raise CorpusRefusal(
                f"malformed-record: record {record_id!r} region is not a positive rectangle"
            )
        if region["x"] + region["w"] > page_width or region["y"] + region["h"] > page_height:
            raise CorpusRefusal(
                f"region-outside-page: record {record_id!r} region {region} exceeds "
                f"the page's {page_width}x{page_height} bounds"
            )
        if not isinstance(record["text"], str) or not record["text"]:
            raise CorpusRefusal(f"malformed-record: record {record_id!r} carries no text")
        expected_text_sha256 = digest_bytes(record["text"].encode("utf-8"))
        if record["text_sha256"] != expected_text_sha256:
            raise CorpusRefusal(
                f"malformed-record: record {record_id!r} text_sha256 does not match its text"
            )
        if record["parish"] is not None and not isinstance(record["parish"], str):
            raise CorpusRefusal(
                f"malformed-record: record {record_id!r} parish must be a string or null"
            )
        for field in ("start_date", "end_date"):
            value = record[field]
            if value is not None and (not isinstance(value, int) or isinstance(value, bool)):
                raise CorpusRefusal(
                    f"malformed-record: record {record_id!r} {field} must be an integer year or null"
                )

    if referenced_splits != set(splits_present):
        raise CorpusRefusal(
            "malformed-record: splits_present must equal exactly the splits carried by records"
        )

    if not verify_self_hash(sidecar):
        raise CorpusRefusal(
            "self-hash-mismatch: sidecar self_hash does not verify against its own content"
        )
    return sidecar


def write_sidecar(path: str | Path, sidecar: dict[str, Any]) -> Path:
    """Write one already-validated sidecar as canonical bytes and return its path."""
    validate_sidecar(sidecar)
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(canonical_bytes(sidecar))
    return target


def load_sidecar(path: str | Path) -> dict[str, Any]:
    """Read a sidecar written by this module, refusing one that fails to validate."""
    import json

    return validate_sidecar(json.loads(Path(path).read_bytes()))
