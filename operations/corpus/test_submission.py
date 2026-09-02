"""Tests for `operations/corpus/submission.py` and `sidecar.py` — no network.

These tests write real files under this checkout's `private/` — the one approved
storage root (`config/data_handling_policy.json`) — because `gate.py` resolves
every approved root against the repository this file lives in, so a path under
pytest's own `tmp_path` would always be refused by the gate. Every test gets a
fresh, uuid-named scratch tree under `private/corpora/recordgold/_test_scratch/`
and removes it on teardown; nothing here is committed (`.gitignore`'s `private/*`
covers the whole tree regardless).
"""

import hashlib
import shutil
import uuid
from pathlib import Path

import pytest

from common.contracts.canonical import digest_bytes
from operations.corpus import CorpusRefusal
from operations.corpus.holdout import build_holdout
from operations.corpus.plan import build_fetch_plan
from operations.corpus.rows import build_snapshot
from operations.corpus.sidecar import load_sidecar, validate_sidecar
from operations.corpus.submission import (
    FetchedPage,
    build_submission,
    partition_into_shards,
    refuse_non_image_files,
)
from operations.submit import gate, inventory, submit

REPO_ROOT = Path(__file__).resolve().parents[2]

VAL_PAGE_URL = (
    "https://europe.iiif.teklia.com/iiif/2/geneanet%2FArdennes_BMS%2F380403%2F00026.jpg/"
    "239,208,1232,443/full/0/default.jpg"
)
VAL_PAGE_SECOND_RECORD_URL = (
    "https://europe.iiif.teklia.com/iiif/2/geneanet%2FArdennes_BMS%2F380403%2F00026.jpg/"
    "10,10,100,100/full/0/default.jpg"
)
VAL_OTHER_PAGE_URL = (
    "https://europe.iiif.teklia.com/iiif/2/geneanet%2FArdennes_BMS%2F383351%2F00143.jpg/"
    "103,139,1278,1566/full/0/default.jpg"
)
TEST_ONLY_PAGE_URL = (
    "https://europe.iiif.teklia.com/iiif/2/geneanet%2FArdennes_BMS%2F999999%2F00099.jpg/"
    "1,1,50,50/full/0/default.jpg"
)
CROSS_SPLIT_URL_SAME_AS_VAL_PAGE = (
    "https://europe.iiif.teklia.com/iiif/2/geneanet%2FArdennes_BMS%2F380403%2F00026.jpg/"
    "500,500,50,50/full/0/default.jpg"
)


def _row(record_id, split, url, source="Ardennes", text="quelque texte", parish="Rethel"):
    return {
        "split": split,
        "source": source,
        "record_id": record_id,
        "record_url": url,
        "start_date": 1700,
        "end_date": 1701,
        "parish": parish,
        "text": text,
        "text_sha256": digest_bytes(text.encode("utf-8")),
    }


def _snapshot(rows):
    source_facts = {
        "dataset": "Teklia/DAI-CReTDHI-RecordGold-ATR",
        "converted_at_utc": "2026-09-01T00:00:00Z",
        "parquet_sha256": {split: "a" * 64 for split in ("train", "val", "test")},
    }
    return build_snapshot(source_facts, rows)


@pytest.fixture
def scratch(tmp_path):
    del tmp_path  # unused: the gate only accepts locations under this repo's private/
    root = REPO_ROOT / "private" / "corpora" / "recordgold" / "_test_scratch" / uuid.uuid4().hex
    (root / "cache").mkdir(parents=True)
    (root / "submissions").mkdir()
    (root / "sidecars").mkdir()
    (root / "ledger").mkdir()
    yield root
    shutil.rmtree(root, ignore_errors=True)


def _cache_file(scratch, name: str, content: bytes) -> tuple[Path, str]:
    path = scratch / "cache" / name
    path.write_bytes(content)
    return path, hashlib.sha256(content).hexdigest()


def _fetched(cache_path: Path, response_sha256: str, *, width=4000, height=6000) -> FetchedPage:
    return FetchedPage(
        cache_path=cache_path,
        info_url="https://europe.iiif.teklia.com/iiif/2/x/info.json",
        image_url="https://europe.iiif.teklia.com/iiif/2/x/full/full/0/default.jpg",
        size_parameter="full",
        response_sha256=response_sha256,
        bytes=len(cache_path.read_bytes()),
        http_status=200,
        fetched_at_utc="2026-09-01T00:00:00Z",
        declared_width=width,
        declared_height=height,
        width=width,
        height=height,
    )


def _build(scratch, rows, fetched_pages, *, shard_prefix="val", max_pages_per_shard=1000):
    snapshot = _snapshot(rows)
    plan = build_fetch_plan(snapshot["rows"], snapshot["self_hash"])
    holdout = build_holdout(snapshot["rows"], snapshot["self_hash"])
    return build_submission(
        plan,
        snapshot,
        holdout,
        fetched_pages,
        submissions_root=scratch / "submissions",
        sidecars_root=scratch / "sidecars",
        ledger_root=scratch / "ledger",
        shard_prefix=shard_prefix,
        max_pages_per_shard=max_pages_per_shard,
    )


# --- happy path -----------------------------------------------------------------


def test_build_submission_writes_images_and_outside_sidecars(scratch):
    rows = [
        _row("rec-1", "val", VAL_PAGE_URL),
        _row("rec-2", "val", VAL_PAGE_SECOND_RECORD_URL),
        _row("rec-3", "val", VAL_OTHER_PAGE_URL, source="Ardennes"),
    ]
    page1_path, page1_digest = _cache_file(scratch, "page1.jpg", b"page-one-bytes")
    page2_path, page2_digest = _cache_file(scratch, "page2.jpg", b"page-two-bytes")
    fetched = {
        "geneanet/Ardennes_BMS/380403/00026.jpg": _fetched(page1_path, page1_digest),
        "geneanet/Ardennes_BMS/383351/00143.jpg": _fetched(page2_path, page2_digest),
    }
    report = _build(scratch, rows, fetched)

    assert report["admitted_page_count"] == 2
    assert report["refused_page_count"] == 0
    assert len(report["shards"]) == 1
    shard = report["shards"][0]
    assert shard["shard_id"] == "val-0001"

    image1 = (
        Path(shard["folder"]) / "Ardennes" / "geneanet" / "Ardennes_BMS" / "380403" / "00026.jpg"
    )
    image2 = (
        Path(shard["folder"]) / "Ardennes" / "geneanet" / "Ardennes_BMS" / "383351" / "00143.jpg"
    )
    assert image1.read_bytes() == b"page-one-bytes"
    assert image2.read_bytes() == b"page-two-bytes"
    # Hard-linked, not copied: same inode as the cache file.
    assert image1.stat().st_ino == page1_path.stat().st_ino

    sidecar1_path = (
        Path(shard["sidecar_dir"])
        / "Ardennes"
        / "geneanet"
        / "Ardennes_BMS"
        / "380403"
        / "00026.json"
    )
    sidecar1 = load_sidecar(sidecar1_path)
    assert sidecar1["schema"] == "recordgold-page-records.v1"
    assert {record["record_id"] for record in sidecar1["records"]} == {"rec-1", "rec-2"}
    assert sidecar1["splits_present"] == ["val"]
    assert sidecar1["page"]["sha256"] == page1_digest


def test_sidecar_never_carries_an_ordinal(scratch):
    rows = [_row("rec-1", "val", VAL_PAGE_URL)]
    page_path, digest = _cache_file(scratch, "page.jpg", b"one-page")
    fetched = {"geneanet/Ardennes_BMS/380403/00026.jpg": _fetched(page_path, digest)}
    report = _build(scratch, rows, fetched)
    sidecar_dir = Path(report["shards"][0]["sidecar_dir"])
    sidecar_path = next(sidecar_dir.rglob("*.json"))
    sidecar = load_sidecar(sidecar_path)
    assert "ordinal" not in sidecar
    for record in sidecar["records"]:
        assert "ordinal" not in record


# --- hold-out refusals ------------------------------------------------------------


def test_refuses_holdout_only_page_by_name(scratch):
    rows = [
        _row("rec-1", "val", VAL_PAGE_URL),
        _row("rec-2", "test", TEST_ONLY_PAGE_URL),
    ]
    page1_path, page1_digest = _cache_file(scratch, "page1.jpg", b"page-one")
    page2_path, page2_digest = _cache_file(scratch, "page2.jpg", b"page-two")
    fetched = {
        "geneanet/Ardennes_BMS/380403/00026.jpg": _fetched(page1_path, page1_digest),
        "geneanet/Ardennes_BMS/999999/00099.jpg": _fetched(page2_path, page2_digest),
    }
    report = _build(scratch, rows, fetched)
    assert report["admitted_page_count"] == 1
    reasons = {r["reason"] for r in report["refusals"]}
    assert reasons == {"holdout-page"}
    refused = [r for r in report["refusals"] if r["reason"] == "holdout-page"][0]
    assert refused["identifier"] == "geneanet/Ardennes_BMS/999999/00099.jpg"


def test_refuses_cross_split_page_by_name(scratch):
    rows = [
        _row("rec-1", "val", VAL_PAGE_URL),
        _row("rec-2", "test", CROSS_SPLIT_URL_SAME_AS_VAL_PAGE),
    ]
    page1_path, page1_digest = _cache_file(scratch, "page1.jpg", b"page-one")
    fetched = {"geneanet/Ardennes_BMS/380403/00026.jpg": _fetched(page1_path, page1_digest)}
    report = _build(scratch, rows, fetched)
    assert report["admitted_page_count"] == 0
    assert report["refusals"][0]["reason"] == "cross-split-page"


def test_refuses_page_not_fetched(scratch):
    rows = [_row("rec-1", "val", VAL_PAGE_URL)]
    report = _build(scratch, rows, {})
    assert report["admitted_page_count"] == 0
    assert report["refusals"][0]["reason"] == "page-not-fetched"


def test_refuses_duplicate_page_bytes(scratch):
    rows = [
        _row("rec-1", "val", VAL_PAGE_URL),
        _row("rec-2", "val", VAL_OTHER_PAGE_URL),
    ]
    shared_path, shared_digest = _cache_file(scratch, "shared.jpg", b"identical-bytes")
    fetched = {
        "geneanet/Ardennes_BMS/380403/00026.jpg": _fetched(shared_path, shared_digest),
        "geneanet/Ardennes_BMS/383351/00143.jpg": _fetched(shared_path, shared_digest),
    }
    report = _build(scratch, rows, fetched)
    assert report["admitted_page_count"] == 1
    assert report["refusals"][0]["reason"] == "duplicate-page-bytes"


def test_refuses_response_sha256_mismatch(scratch):
    rows = [_row("rec-1", "val", VAL_PAGE_URL)]
    page_path, _real_digest = _cache_file(scratch, "page.jpg", b"actual-bytes")
    fetched = {
        "geneanet/Ardennes_BMS/380403/00026.jpg": _fetched(page_path, "0" * 64),
    }
    report = _build(scratch, rows, fetched)
    assert report["admitted_page_count"] == 0
    assert report["refusals"][0]["reason"] == "response-sha256-mismatch"


# --- images-only guard ------------------------------------------------------------


def test_planted_ds_store_refused(scratch):
    rows = [_row("rec-1", "val", VAL_PAGE_URL)]
    page_path, digest = _cache_file(scratch, "page.jpg", b"page-bytes")
    fetched = {"geneanet/Ardennes_BMS/380403/00026.jpg": _fetched(page_path, digest)}
    report = _build(scratch, rows, fetched)
    folder = Path(report["shards"][0]["folder"])

    (folder / ".DS_Store").write_bytes(b"junk")
    with pytest.raises(CorpusRefusal, match="unexpected-file-in-submission-folder"):
        refuse_non_image_files(folder)


def test_planted_sidecar_inside_submission_folder_refused(scratch):
    rows = [_row("rec-1", "val", VAL_PAGE_URL)]
    page_path, digest = _cache_file(scratch, "page.jpg", b"page-bytes")
    fetched = {"geneanet/Ardennes_BMS/380403/00026.jpg": _fetched(page_path, digest)}
    report = _build(scratch, rows, fetched)
    folder = Path(report["shards"][0]["folder"])

    stray_sidecar = folder / "Ardennes" / "geneanet" / "Ardennes_BMS" / "380403" / "00026.json"
    stray_sidecar.write_bytes(b'{"schema": "recordgold-page-records.v1"}')
    with pytest.raises(CorpusRefusal, match="unexpected-file-in-submission-folder"):
        refuse_non_image_files(folder)


def test_clean_folder_passes_the_images_only_guard(scratch):
    rows = [_row("rec-1", "val", VAL_PAGE_URL)]
    page_path, digest = _cache_file(scratch, "page.jpg", b"page-bytes")
    fetched = {"geneanet/Ardennes_BMS/380403/00026.jpg": _fetched(page_path, digest)}
    report = _build(scratch, rows, fetched)
    refuse_non_image_files(Path(report["shards"][0]["folder"]))  # does not raise


# --- the real submit door: manifest names exactly the images ----------------------


def test_submit_build_manifest_names_exactly_the_images(scratch):
    rows = [
        _row("rec-1", "val", VAL_PAGE_URL),
        _row("rec-2", "val", VAL_OTHER_PAGE_URL),
    ]
    page1_path, page1_digest = _cache_file(scratch, "page1.jpg", b"page-one")
    page2_path, page2_digest = _cache_file(scratch, "page2.jpg", b"page-two")
    fetched = {
        "geneanet/Ardennes_BMS/380403/00026.jpg": _fetched(page1_path, page1_digest),
        "geneanet/Ardennes_BMS/383351/00143.jpg": _fetched(page2_path, page2_digest),
    }
    report = _build(scratch, rows, fetched)
    folder = Path(report["shards"][0]["folder"])

    sources = inventory.read_submission(folder, max_bytes=0)
    manifest = submit.build_manifest(
        [{"relative_path": s.relative_path, "sha256": s.sha256, "bytes": s.size} for s in sources]
    )
    expected_relative_paths = {
        "Ardennes/geneanet/Ardennes_BMS/380403/00026.jpg",
        "Ardennes/geneanet/Ardennes_BMS/383351/00143.jpg",
    }
    assert {entry["relative_path"] for entry in manifest["files"]} == expected_relative_paths

    # And the manifest submission() actually wrote is the same one, on disk.
    written = submit.load_manifest(Path(report["shards"][0]["manifest_path"]))
    assert {entry["relative_path"] for entry in written["files"]} == expected_relative_paths


# --- gate acceptance of every written path -----------------------------------------


def test_gate_accepts_every_written_path(scratch):
    rows = [_row("rec-1", "val", VAL_PAGE_URL)]
    page_path, digest = _cache_file(scratch, "page.jpg", b"page-bytes")
    fetched = {"geneanet/Ardennes_BMS/380403/00026.jpg": _fetched(page_path, digest)}
    report = _build(scratch, rows, fetched)

    policy = gate.load_policy(gate.DEFAULT_POLICY_PATH)
    roots = gate.approved_storage_roots(policy)
    shard = report["shards"][0]
    for label, location in (
        ("folder", Path(shard["folder"])),
        ("sidecar_dir", Path(shard["sidecar_dir"])),
        ("manifest_path", Path(shard["manifest_path"])),
    ):
        gate.require_approved_storage_location(location, roots, label)  # does not raise
    for image in Path(shard["folder"]).rglob("*.jpg"):
        gate.require_approved_storage_location(image, roots, "image")
    for sidecar_file in Path(shard["sidecar_dir"]).rglob("*.json"):
        gate.require_approved_storage_location(sidecar_file, roots, "sidecar")


# --- .gitignore --------------------------------------------------------------------


def test_gitignore_carries_private_star():
    lines = (REPO_ROOT / ".gitignore").read_text().splitlines()
    assert "private/*" in lines


# --- partitioning -------------------------------------------------------------------


def _fake_admitted(count):
    admitted = []
    for index in range(count):
        page = {
            "identifier": f"geneanet/src/vol/{index:05d}.jpg",
            "splits_present": ["val"],
            "source": "Ardennes",
            "volume": "geneanet/src/vol",
            "designation": f"{index:05d}.jpg",
            "records": [],
        }
        admitted.append((page, None))
    return admitted


def test_partition_into_shards_caps_each_slice():
    admitted = _fake_admitted(2500)
    shards = partition_into_shards(admitted, max_pages_per_shard=1000)
    assert [len(shard) for shard in shards] == [1000, 1000, 500]
    seen = {page["identifier"] for shard in shards for page, _fetched in shard}
    assert len(seen) == 2500


def test_partition_into_shards_refuses_non_positive_cap():
    with pytest.raises(CorpusRefusal, match="malformed-record"):
        partition_into_shards(_fake_admitted(1), max_pages_per_shard=0)


# --- sidecar module directly ---------------------------------------------------------


def test_validate_sidecar_refuses_extra_field():
    from operations.corpus.sidecar import build_sidecar

    good = build_sidecar(
        source="Ardennes",
        volume_id="geneanet/Ardennes_BMS/380403",
        designation="00026.jpg",
        iiif={
            "identifier": "geneanet/Ardennes_BMS/380403/00026.jpg",
            "info_url": "https://europe.iiif.teklia.com/iiif/2/x/info.json",
            "image_url": "https://europe.iiif.teklia.com/iiif/2/x/full/full/0/default.jpg",
            "size_parameter": "full",
            "response_sha256": "a" * 64,
            "bytes": 100,
            "http_status": 200,
            "fetched_at_utc": "2026-09-01T00:00:00Z",
            "declared_width": 4000,
            "declared_height": 6000,
        },
        page={"sha256": "a" * 64, "width": 4000, "height": 6000},
        splits_present=["val"],
        records=[
            {
                "record_id": "rec-1",
                "split": "val",
                "region": {"x": 1, "y": 1, "w": 10, "h": 10},
                "text": "hello",
                "text_sha256": digest_bytes(b"hello"),
                "start_date": None,
                "end_date": None,
                "parish": None,
            }
        ],
    )
    validate_sidecar(good)

    with_ordinal = dict(good)
    with_ordinal["ordinal"] = 1
    del with_ordinal["self_hash"]
    with pytest.raises(CorpusRefusal, match="malformed-record"):
        validate_sidecar(with_ordinal)
