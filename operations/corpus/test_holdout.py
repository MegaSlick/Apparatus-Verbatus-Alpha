"""Pure tests for `operations/corpus/holdout.py` — no network, no parquet.

Every row is an inline dict shaped like a `recordgold-rows.v1` row.
"""

import copy
import json

import pytest

from common.contracts.canonical import digest_bytes
from common.contracts.canonical import self_hash as _self_hash
from operations.corpus import CorpusRefusal
from operations.corpus.holdout import (
    build_holdout,
    load_holdout,
    main,
    refuse_held_out_page,
    validate_holdout,
)
from operations.corpus.rows import build_snapshot

SNAPSHOT_HASH = "0" * 64

VAL_PAGE_URL = (
    "https://europe.iiif.teklia.com/iiif/2/geneanet%2FArdennes_BMS%2F380403%2F00026.jpg/"
    "10,10,100,100/full/0/default.jpg"
)
CROSS_SPLIT_URL_SAME_PAGE = (
    "https://europe.iiif.teklia.com/iiif/2/geneanet%2FArdennes_BMS%2F380403%2F00026.jpg/"
    "200,200,50,50/full/0/default.jpg"
)
TEST_ONLY_PAGE_URL = (
    "https://europe.iiif.teklia.com/iiif/2/geneanet%2FArdennes_BMS%2F383351%2F00143.jpg/"
    "103,139,1278,1566/full/0/default.jpg"
)
TRAIN_PAGE_URL = (
    "https://europe.iiif.teklia.com/iiif/2/geneanet%2FArdennes_BMS%2F999999%2F00001.jpg/"
    "1,1,10,10/full/0/default.jpg"
)


def _row(record_id, split, url):
    text = "quelque texte"
    return {
        "split": split,
        "source": "Ardennes",
        "record_id": record_id,
        "record_url": url,
        "start_date": None,
        "end_date": None,
        "parish": "Rethel",
        "text": text,
        "text_sha256": digest_bytes(text.encode("utf-8")),
    }


# --- build_holdout ---------------------------------------------------------------


def test_build_holdout_only_counts_the_test_split():
    rows = [
        _row("v1", "val", VAL_PAGE_URL),
        _row("t1", "train", TRAIN_PAGE_URL),
        _row("s1", "test", TEST_ONLY_PAGE_URL),
    ]
    holdout = build_holdout(rows, SNAPSHOT_HASH)
    assert holdout["schema"] == "recordgold-holdout.v1"
    assert holdout["held_identifiers"] == ["geneanet/Ardennes_BMS/383351/00143.jpg"]
    assert holdout["held_record_ids"] == ["s1"]
    assert holdout["entries"] == [
        {"identifier": "geneanet/Ardennes_BMS/383351/00143.jpg", "record_ids": ["s1"]}
    ]


def test_build_holdout_groups_multiple_test_records_on_one_page():
    rows = [_row("s1", "test", TEST_ONLY_PAGE_URL), _row("s2", "test", TEST_ONLY_PAGE_URL)]
    holdout = build_holdout(rows, SNAPSHOT_HASH)
    assert holdout["held_record_ids"] == ["s1", "s2"]
    assert holdout["entries"][0]["record_ids"] == ["s1", "s2"]


def test_build_holdout_is_deterministic():
    rows = [_row("s1", "test", TEST_ONLY_PAGE_URL), _row("v1", "val", VAL_PAGE_URL)]
    a = build_holdout(rows, SNAPSHOT_HASH)
    b = build_holdout(copy.deepcopy(rows), SNAPSHOT_HASH)
    assert a == b
    assert a["self_hash"] == b["self_hash"]


def test_build_holdout_with_no_test_rows_is_empty_and_still_valid():
    rows = [_row("v1", "val", VAL_PAGE_URL)]
    holdout = build_holdout(rows, SNAPSHOT_HASH)
    assert holdout["held_identifiers"] == []
    assert holdout["held_record_ids"] == []
    assert holdout["entries"] == []
    validate_holdout(holdout)


# --- refuse_held_out_page: the load-bearing predicate ---------------------------


def test_refuse_held_out_page_allows_a_page_not_in_the_ledger():
    rows = [_row("s1", "test", TEST_ONLY_PAGE_URL)]
    holdout = build_holdout(rows, SNAPSHOT_HASH)
    # No exception: an unheld identifier is simply allowed through.
    refuse_held_out_page(holdout, "geneanet/Ardennes_BMS/999999/00001.jpg", ["val"])


def test_refuse_held_out_page_refuses_a_pure_test_page_by_name():
    rows = [_row("s1", "test", TEST_ONLY_PAGE_URL)]
    holdout = build_holdout(rows, SNAPSHOT_HASH)
    with pytest.raises(CorpusRefusal, match="^holdout-page:"):
        refuse_held_out_page(holdout, "geneanet/Ardennes_BMS/383351/00143.jpg", ["test"])


def test_refuse_held_out_page_refuses_a_val_page_that_also_carries_a_test_record():
    # The scenario SPEC.md names explicitly: a page carrying both val and test
    # records must be refused as cross-split-page, not merely as holdout-page.
    rows = [_row("v1", "val", VAL_PAGE_URL), _row("s1", "test", CROSS_SPLIT_URL_SAME_PAGE)]
    holdout = build_holdout(rows, SNAPSHOT_HASH)
    assert holdout["held_identifiers"] == ["geneanet/Ardennes_BMS/380403/00026.jpg"]
    with pytest.raises(CorpusRefusal, match="^cross-split-page:"):
        refuse_held_out_page(holdout, "geneanet/Ardennes_BMS/380403/00026.jpg", ["test", "val"])


# --- validate_holdout ---------------------------------------------------------


def test_validate_holdout_refuses_a_self_hash_mismatch():
    rows = [_row("s1", "test", TEST_ONLY_PAGE_URL)]
    holdout = build_holdout(rows, SNAPSHOT_HASH)
    tampered = dict(holdout)
    tampered["source_row_snapshot_self_hash"] = "1" * 64
    with pytest.raises(CorpusRefusal, match="^self-hash-mismatch:"):
        validate_holdout(tampered)


def test_validate_holdout_refuses_an_open_field_set():
    rows = [_row("s1", "test", TEST_ONLY_PAGE_URL)]
    holdout = build_holdout(rows, SNAPSHOT_HASH)
    tampered = dict(holdout)
    tampered["extra"] = 1
    with pytest.raises(CorpusRefusal, match="^malformed-record:"):
        validate_holdout(tampered)


def test_validate_holdout_refuses_wrong_schema():
    rows = [_row("s1", "test", TEST_ONLY_PAGE_URL)]
    holdout = build_holdout(rows, SNAPSHOT_HASH)
    tampered = dict(holdout)
    tampered["schema"] = "some-other.v1"
    with pytest.raises(CorpusRefusal, match="^wrong-schema:"):
        validate_holdout(tampered)


def test_validate_holdout_refuses_held_record_ids_out_of_sync_with_entries():
    rows = [_row("s1", "test", TEST_ONLY_PAGE_URL)]
    holdout = build_holdout(rows, SNAPSHOT_HASH)
    tampered = dict(holdout)
    tampered["held_record_ids"] = ["s1", "s2"]
    tampered["self_hash"] = _self_hash(tampered)
    with pytest.raises(CorpusRefusal, match="^malformed-record:"):
        validate_holdout(tampered)


def test_validate_holdout_refuses_a_non_digest_source_row_snapshot_self_hash():
    rows = [_row("s1", "test", TEST_ONLY_PAGE_URL)]
    holdout = build_holdout(rows, SNAPSHOT_HASH)
    tampered = dict(holdout)
    tampered["source_row_snapshot_self_hash"] = ""
    with pytest.raises(CorpusRefusal, match="^malformed-record: source_row_snapshot_self_hash"):
        validate_holdout(tampered)


# --- main / load_holdout: the tracked emitter and loader ------------------------


def _write_snapshot(tmp_path, rows):
    source_facts = {
        "dataset": "Teklia/DAI-CReTDHI-RecordGold-ATR",
        "parquet_sha256": {
            split: digest_bytes(split.encode("utf-8")) for split in ("train", "val", "test")
        },
        "converted_at_utc": "2026-01-01T00:00:00Z",
    }
    snapshot = build_snapshot(source_facts, rows)
    snapshot_path = tmp_path / "rows.json"
    snapshot_path.write_text(json.dumps(snapshot))
    return snapshot_path


def test_main_builds_and_writes_a_validated_holdout(tmp_path):
    snapshot_path = _write_snapshot(tmp_path, [_row("s1", "test", TEST_ONLY_PAGE_URL)])
    output_path = tmp_path / "holdout.json"
    holdout = main(snapshot_path, output_path)
    assert holdout["schema"] == "recordgold-holdout.v1"
    assert output_path.exists()


def test_load_holdout_returns_a_byte_identical_validated_holdout(tmp_path):
    snapshot_path = _write_snapshot(tmp_path, [_row("s1", "test", TEST_ONLY_PAGE_URL)])
    output_path = tmp_path / "holdout.json"
    built = main(snapshot_path, output_path)
    loaded = load_holdout(output_path)
    assert loaded == built


def test_load_holdout_refuses_a_tampered_file(tmp_path):
    snapshot_path = _write_snapshot(tmp_path, [_row("s1", "test", TEST_ONLY_PAGE_URL)])
    output_path = tmp_path / "holdout.json"
    main(snapshot_path, output_path)
    tampered = json.loads(output_path.read_text())
    tampered["source_row_snapshot_self_hash"] = digest_bytes(b"a different snapshot entirely")
    output_path.write_text(json.dumps(tampered))
    with pytest.raises(CorpusRefusal, match="^self-hash-mismatch:"):
        load_holdout(output_path)
