"""The row snapshot: the three RecordGold parquets, once, as canonical JSON.

`pyproject.toml` carries no parquet reader (recorded decision, `SPEC.md` §4:
"a 40 MB compiled runtime dependency to read 1.9 MB of metadata once"). A one-shot
scratch converter outside this repository reads the parquets with a throwaway
`pyarrow` environment and calls `build_snapshot` here to write the sealed JSON this
package actually depends on. Everything downstream — `plan.py`, `holdout.py`, and
every later unit — reads only that file, never a parquet, so the whole build is
reproducible from one gitignored, self-hashed artifact and no tracked module ever
imports `pyarrow`.

`recordgold-rows.v1` is deliberately thin: it carries exactly the eight parquet
columns per row (`split, source, record_id, record_url, start_date, end_date,
parish, text`), plus a `text_sha256` the converter computes and this module
verifies, plus the source facts that let a reader tell which parquets produced it.
Nothing here parses `record_url` — that is `plan.py`'s job, kept separate so a
change to the URL grammar never touches the snapshot's own validity.
"""

from typing import Any

from common.contracts.canonical import digest_bytes, self_hash, verify_self_hash

from . import CorpusRefusal

SCHEMA = "recordgold-rows.v1"
CORPUS_ID = "recordgold"

# The three splits the dataset card names. Closed: an unrecognised fourth split
# in a future re-export must refuse rather than pass through into a fetch plan
# that has no hold-out rule for it.
SPLITS = frozenset({"train", "val", "test"})

_ROW_FIELDS = frozenset(
    {
        "split",
        "source",
        "record_id",
        "record_url",
        "start_date",
        "end_date",
        "parish",
        "text",
        "text_sha256",
    }
)

_SOURCE_FACTS_FIELDS = frozenset({"dataset", "parquet_sha256", "converted_at_utc"})
_PARQUET_SHA256_FIELDS = SPLITS

_TOP_FIELDS = frozenset({"schema", "corpus_id", "source_facts", "rows", "self_hash"})

ROW_REFUSAL_REASONS = frozenset(
    {
        "malformed-record",
        "wrong-schema",
        "wrong-corpus",
        "unknown-split",
        "empty-field",
        "empty-text",
        "malformed-field",
        "text-sha256-mismatch",
        "duplicate-record-id",
        "empty-rows",
        "self-hash-mismatch",
    }
)


def _closed(value: Any, fields: frozenset[str], what: str) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != fields:
        raise CorpusRefusal(f"malformed-record: {what} must be the closed record {sorted(fields)}")
    return value


def _non_empty_str(value: Any, what: str) -> str:
    if not isinstance(value, str) or not value:
        raise CorpusRefusal(f"empty-field: {what} must be a non-empty string")
    return value


def validate_row(row: Any, index: int) -> dict[str, Any]:
    """Refuse a row that is not exactly the closed shape this snapshot carries."""
    row = _closed(row, _ROW_FIELDS, f"row[{index}]")
    if row["split"] not in SPLITS:
        raise CorpusRefusal(
            f"unknown-split: row[{index}] (record_id {row.get('record_id')!r}) "
            f"has split {row['split']!r}, not one of {sorted(SPLITS)}"
        )
    for field in ("source", "record_id", "record_url"):
        _non_empty_str(row[field], f"row[{index}].{field}")
    if not isinstance(row["text"], str) or not row["text"]:
        raise CorpusRefusal(
            f"empty-text: row[{index}] (record_id {row['record_id']!r}) carries no text"
        )
    if row["parish"] is not None and not isinstance(row["parish"], str):
        raise CorpusRefusal(
            f"malformed-field: row[{index}].parish must be a string or null, "
            f"got {type(row['parish']).__name__}"
        )
    for field in ("start_date", "end_date"):
        value = row[field]
        # The parquet carries these as years (int64), not date strings — measured
        # 1548-1806 (SPEC.md's intake notes). Booleans are ints in Python and are
        # refused explicitly so a stray flag can never pass as a year.
        if value is not None and (not isinstance(value, int) or isinstance(value, bool)):
            raise CorpusRefusal(
                f"malformed-field: row[{index}].{field} must be an integer year or "
                f"null, got {type(value).__name__}"
            )
    expected = digest_bytes(row["text"].encode("utf-8"))
    if row["text_sha256"] != expected:
        raise CorpusRefusal(
            f"text-sha256-mismatch: row[{index}] (record_id {row['record_id']!r}) "
            f"carries {row['text_sha256']!r}, recomputed {expected!r}"
        )
    return row


def validate_snapshot(snapshot: Any) -> dict[str, Any]:
    """Refuse a snapshot that is not exactly `recordgold-rows.v1`, closed and self-consistent."""
    snapshot = _closed(snapshot, _TOP_FIELDS, "row snapshot")
    if snapshot["schema"] != SCHEMA:
        raise CorpusRefusal(f"wrong-schema: expected {SCHEMA!r}, got {snapshot['schema']!r}")
    if snapshot["corpus_id"] != CORPUS_ID:
        raise CorpusRefusal(f"wrong-corpus: expected {CORPUS_ID!r}, got {snapshot['corpus_id']!r}")

    source_facts = _closed(snapshot["source_facts"], _SOURCE_FACTS_FIELDS, "source_facts")
    _non_empty_str(source_facts["dataset"], "source_facts.dataset")
    _non_empty_str(source_facts["converted_at_utc"], "source_facts.converted_at_utc")
    parquet_sha256 = _closed(
        source_facts["parquet_sha256"], _PARQUET_SHA256_FIELDS, "source_facts.parquet_sha256"
    )
    for split in _PARQUET_SHA256_FIELDS:
        _non_empty_str(parquet_sha256[split], f"source_facts.parquet_sha256.{split}")

    rows = snapshot["rows"]
    if not isinstance(rows, list) or not rows:
        raise CorpusRefusal("empty-rows: row snapshot must carry at least one row")

    seen_ids: set[str] = set()
    for index, row in enumerate(rows):
        validate_row(row, index)
        record_id = row["record_id"]
        if record_id in seen_ids:
            raise CorpusRefusal(
                f"duplicate-record-id: {record_id!r} appears more than once in the snapshot"
            )
        seen_ids.add(record_id)

    if not verify_self_hash(snapshot):
        raise CorpusRefusal(
            "self-hash-mismatch: row snapshot self_hash does not verify against its own content"
        )
    return snapshot


def build_snapshot(source_facts: dict[str, Any], rows: list[dict[str, Any]]) -> dict[str, Any]:
    """Seal `rows` under `source_facts` into a validated, self-hashed `recordgold-rows.v1`."""
    body = {
        "schema": SCHEMA,
        "corpus_id": CORPUS_ID,
        "source_facts": source_facts,
        "rows": rows,
    }
    body["self_hash"] = self_hash(body)
    return validate_snapshot(body)
