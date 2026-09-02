"""Pure tests for `operations/corpus/rows.py` — no network, no parquet.

Before this file, nothing in the repository exercised `rows.py`: the eleven
reasons in `ROW_REFUSAL_REASONS` were declared but never shown to fire, and the
sealed row-snapshot artifact was produced by code with zero test coverage. Every
test below fires exactly one named reason, built on a minimal three-row inline
snapshot — the project's own law is that a guard must be able to fail for the
reason it names.
"""

import copy

import pytest

from common.contracts.canonical import digest_bytes
from common.contracts.canonical import self_hash as _self_hash
from operations.corpus import CorpusRefusal
from operations.corpus.rows import (
    CORPUS_ID,
    ROW_REFUSAL_REASONS,
    SCHEMA,
    build_snapshot,
    validate_row,
    validate_snapshot,
)


def _row(record_id, split="val", text="quelque texte", **overrides):
    row = {
        "split": split,
        "source": "Ardennes",
        "record_id": record_id,
        "record_url": "https://example.invalid/does/not/matter",
        "start_date": None,
        "end_date": None,
        "parish": "Rethel",
        "text": text,
        "text_sha256": digest_bytes(text.encode("utf-8")),
    }
    row.update(overrides)
    return row


def _source_facts():
    return {
        "dataset": "Teklia/DAI-CReTDHI-RecordGold-ATR",
        "parquet_sha256": {
            "train": digest_bytes(b"train"),
            "val": digest_bytes(b"val"),
            "test": digest_bytes(b"test"),
        },
        "converted_at_utc": "2026-01-01T00:00:00Z",
    }


# --- build_snapshot / validate_snapshot: the happy path -------------------------


def test_build_snapshot_seals_three_rows():
    rows = [_row("r1"), _row("r2", split="train"), _row("r3", split="test")]
    snapshot = build_snapshot(_source_facts(), rows)
    assert snapshot["schema"] == SCHEMA
    assert snapshot["corpus_id"] == CORPUS_ID
    assert len(snapshot["rows"]) == 3
    assert validate_snapshot(snapshot) == snapshot


def test_build_snapshot_is_deterministic():
    rows = [_row("r1"), _row("r2", split="train")]
    a = build_snapshot(_source_facts(), copy.deepcopy(rows))
    b = build_snapshot(_source_facts(), copy.deepcopy(rows))
    assert a == b
    assert a["self_hash"] == b["self_hash"]


# --- validate_row: every ROW_REFUSAL_REASONS name fires --------------------------


def test_validate_row_refuses_a_non_closed_shape():
    row = _row("r1")
    del row["parish"]
    with pytest.raises(CorpusRefusal, match="^malformed-record:"):
        validate_row(row, 0)


def test_validate_row_refuses_an_unknown_split():
    with pytest.raises(CorpusRefusal, match="^unknown-split:"):
        validate_row(_row("r1", split="dev"), 0)


def test_validate_row_refuses_an_empty_field():
    with pytest.raises(CorpusRefusal, match="^empty-field:"):
        validate_row(_row("r1", source=""), 0)


def test_validate_row_refuses_empty_text():
    with pytest.raises(CorpusRefusal, match="^empty-text:"):
        validate_row(_row("r1", text="", text_sha256=digest_bytes(b"")), 0)


def test_validate_row_refuses_a_non_string_non_null_parish():
    with pytest.raises(CorpusRefusal, match="^malformed-field:"):
        validate_row(_row("r1", parish=123), 0)


def test_validate_row_refuses_a_non_integer_start_date():
    with pytest.raises(CorpusRefusal, match="^malformed-field:"):
        validate_row(_row("r1", start_date="1700"), 0)


def test_validate_row_refuses_a_boolean_start_date():
    # bool is a subclass of int in Python; a stray flag must never pass as a year.
    with pytest.raises(CorpusRefusal, match="^malformed-field:"):
        validate_row(_row("r1", start_date=True), 0)


def test_validate_row_accepts_a_null_or_integer_date():
    validate_row(_row("r1", start_date=1700, end_date=None), 0)


def test_validate_row_refuses_a_text_sha256_mismatch():
    row = _row("r1")
    row["text_sha256"] = digest_bytes(b"a different text entirely")
    with pytest.raises(CorpusRefusal, match="^text-sha256-mismatch:"):
        validate_row(row, 0)


def test_row_refusal_reasons_covered_here_are_a_subset_of_the_declared_vocabulary():
    exercised = {
        "malformed-record",
        "unknown-split",
        "empty-field",
        "empty-text",
        "malformed-field",
        "text-sha256-mismatch",
    }
    assert exercised <= ROW_REFUSAL_REASONS


# --- validate_snapshot: shape, schema, and uniqueness ----------------------------


def test_validate_snapshot_refuses_wrong_schema():
    snapshot = build_snapshot(_source_facts(), [_row("r1")])
    tampered = dict(snapshot)
    tampered["schema"] = "some-other.v1"
    with pytest.raises(CorpusRefusal, match="^wrong-schema:"):
        validate_snapshot(tampered)


def test_validate_snapshot_refuses_wrong_corpus():
    snapshot = build_snapshot(_source_facts(), [_row("r1")])
    tampered = dict(snapshot)
    tampered["corpus_id"] = "some-other-corpus"
    with pytest.raises(CorpusRefusal, match="^wrong-corpus:"):
        validate_snapshot(tampered)


def test_validate_snapshot_refuses_empty_rows():
    body = {
        "schema": SCHEMA,
        "corpus_id": CORPUS_ID,
        "source_facts": _source_facts(),
        "rows": [],
    }
    body["self_hash"] = _self_hash(body)
    with pytest.raises(CorpusRefusal, match="^empty-rows:"):
        validate_snapshot(body)


def test_validate_snapshot_refuses_a_duplicate_record_id():
    body = {
        "schema": SCHEMA,
        "corpus_id": CORPUS_ID,
        "source_facts": _source_facts(),
        "rows": [_row("r1"), _row("r1", split="train")],
    }
    body["self_hash"] = _self_hash(body)
    with pytest.raises(CorpusRefusal, match="^duplicate-record-id:"):
        validate_snapshot(body)


def test_validate_snapshot_refuses_a_self_hash_mismatch():
    snapshot = build_snapshot(_source_facts(), [_row("r1")])
    tampered = dict(snapshot)
    tampered["rows"] = [_row("r2")]
    with pytest.raises(CorpusRefusal, match="^self-hash-mismatch:"):
        validate_snapshot(tampered)
