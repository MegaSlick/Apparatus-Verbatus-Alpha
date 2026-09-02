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
import json
import os
import shutil
import uuid
from pathlib import Path

import pytest

from common.contracts.canonical import digest_bytes
from operations.corpus import CorpusRefusal
from operations.corpus import fetch as fetch_module
from operations.corpus.holdout import build_holdout
from operations.corpus.integrate import fetched_pages_from_log
from operations.corpus.plan import build_fetch_plan
from operations.corpus.rows import build_snapshot
from operations.corpus.sidecar import load_sidecar, validate_sidecar
from operations.corpus.submission import (
    FetchedPage,
    build_submission,
    partition_into_shards,
    refuse_non_image_files,
)
from operations.corpus.submission import main as submission_main
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


def _cache_body(scratch, content: bytes) -> str:
    """Write `content` at the exact path `integrate.fetched_pages_from_log` expects.

    Unlike `_cache_file`, the digest *is* the filename — `cache.body_path`'s own
    `cache/<response-sha256>.jpg` convention — because `fetched_pages_from_log`
    resolves a page's cache path from its logged `response_sha256` alone, never
    from a name a caller chose.
    """
    digest = hashlib.sha256(content).hexdigest()
    path = scratch / "cache" / f"{digest}.jpg"
    path.write_bytes(content)
    return digest


def _fetched(
    cache_path: Path,
    response_sha256: str,
    *,
    width=4000,
    height=6000,
    declared_width=None,
    declared_height=None,
) -> FetchedPage:
    return FetchedPage(
        cache_path=cache_path,
        info_url="https://europe.iiif.teklia.com/iiif/2/x/info.json",
        image_url="https://europe.iiif.teklia.com/iiif/2/x/full/full/0/default.jpg",
        size_parameter="full",
        response_sha256=response_sha256,
        bytes=len(cache_path.read_bytes()),
        http_status=200,
        fetched_at_utc="2026-09-01T00:00:00Z",
        declared_width=width if declared_width is None else declared_width,
        declared_height=height if declared_height is None else declared_height,
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
    # The two pages carry different volumes (380403 vs 383351) — same split and
    # source, but the partition key is (split, source, volume), so each page's
    # own volume is its own shard.
    assert len(report["shards"]) == 2
    assert [shard["shard_id"] for shard in report["shards"]] == ["val-0001", "val-0002"]
    shard1, shard2 = report["shards"]

    image1 = (
        Path(shard1["folder"]) / "Ardennes" / "geneanet" / "Ardennes_BMS" / "380403" / "00026.jpg"
    )
    image2 = (
        Path(shard2["folder"]) / "Ardennes" / "geneanet" / "Ardennes_BMS" / "383351" / "00143.jpg"
    )
    assert image1.read_bytes() == b"page-one-bytes"
    assert image2.read_bytes() == b"page-two-bytes"
    # Hard-linked, not copied: same inode as the cache file.
    assert image1.stat().st_ino == page1_path.stat().st_ino

    sidecar1_path = (
        Path(shard1["sidecar_dir"])
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


# --- plan / snapshot / hold-out binding ---------------------------------------------


def test_refuses_holdout_built_from_a_different_snapshot(scratch):
    rows = [
        _row("rec-1", "val", VAL_PAGE_URL),
        _row("rec-2", "test", TEST_ONLY_PAGE_URL),
    ]
    snapshot = _snapshot(rows)
    plan = build_fetch_plan(snapshot["rows"], snapshot["self_hash"])

    # A holdout built from a *different* snapshot — here, one where the same page
    # is labelled `val` instead of `test`, so held_identifiers is empty and the
    # held-out page would otherwise slip straight through.
    other_rows = [
        _row("rec-1", "val", VAL_PAGE_URL),
        _row("rec-2", "val", TEST_ONLY_PAGE_URL),
    ]
    other_snapshot = _snapshot(other_rows)
    mismatched_holdout = build_holdout(other_snapshot["rows"], other_snapshot["self_hash"])
    assert mismatched_holdout["held_identifiers"] == []

    page1_path, page1_digest = _cache_file(scratch, "page1.jpg", b"page-one")
    page2_path, page2_digest = _cache_file(scratch, "page2.jpg", b"page-two")
    fetched = {
        "geneanet/Ardennes_BMS/380403/00026.jpg": _fetched(page1_path, page1_digest),
        "geneanet/Ardennes_BMS/999999/00099.jpg": _fetched(page2_path, page2_digest),
    }
    with pytest.raises(CorpusRefusal, match="mismatched-row-snapshot"):
        build_submission(
            plan,
            snapshot,
            mismatched_holdout,
            fetched,
            submissions_root=scratch / "submissions",
            sidecars_root=scratch / "sidecars",
            ledger_root=scratch / "ledger",
            shard_prefix="probe1",
        )
    assert not any((scratch / "submissions").rglob("*"))


def test_refuses_record_not_in_row_snapshot(scratch):
    # A plan whose self-hash binding matches the snapshot (so it clears the
    # mismatched-row-snapshot check) but whose own `records` were tampered to
    # name a record_id the snapshot never carried — the shape a corrupted or
    # hand-built plan could take even though `build_fetch_plan` itself never
    # produces one, per rule 7's "nothing is lost silently, not merely trusted".
    import copy

    from common.contracts.canonical import self_hash as recompute_self_hash

    rows = [_row("rec-1", "val", VAL_PAGE_URL)]
    snapshot = _snapshot(rows)
    plan = build_fetch_plan(snapshot["rows"], snapshot["self_hash"])
    holdout = build_holdout(snapshot["rows"], snapshot["self_hash"])

    tampered = copy.deepcopy(dict(plan))
    del tampered["self_hash"]
    tampered["pages"][0]["records"].append(
        {
            "record_id": "rec-does-not-exist",
            "physical_act_id": tampered["pages"][0]["records"][0]["physical_act_id"],
            "region": {"x": 1, "y": 1, "w": 10, "h": 10},
            "split": "val",
        }
    )
    tampered["self_hash"] = recompute_self_hash(tampered)

    page_path, digest = _cache_file(scratch, "page.jpg", b"page-bytes")
    fetched = {"geneanet/Ardennes_BMS/380403/00026.jpg": _fetched(page_path, digest)}
    report = build_submission(
        tampered,
        snapshot,
        holdout,
        fetched,
        submissions_root=scratch / "submissions",
        sidecars_root=scratch / "sidecars",
        ledger_root=scratch / "ledger",
        shard_prefix="val",
    )
    assert report["admitted_page_count"] == 0
    assert report["refusals"][0]["reason"] == "record-not-in-row-snapshot"


def test_refuses_plan_built_from_a_different_snapshot(scratch):
    rows = [_row("rec-1", "val", VAL_PAGE_URL)]
    snapshot = _snapshot(rows)
    holdout = build_holdout(snapshot["rows"], snapshot["self_hash"])

    other_snapshot = _snapshot([_row("rec-1", "val", VAL_PAGE_URL, text="autre texte")])
    mismatched_plan = build_fetch_plan(other_snapshot["rows"], other_snapshot["self_hash"])

    page_path, digest = _cache_file(scratch, "page.jpg", b"page-bytes")
    fetched = {"geneanet/Ardennes_BMS/380403/00026.jpg": _fetched(page_path, digest)}
    with pytest.raises(CorpusRefusal, match="mismatched-row-snapshot"):
        build_submission(
            mismatched_plan,
            snapshot,
            holdout,
            fetched,
            submissions_root=scratch / "submissions",
            sidecars_root=scratch / "sidecars",
            ledger_root=scratch / "ledger",
            shard_prefix="probe2",
        )


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


def test_refuses_unrecognized_page_extension(scratch):
    non_jpg_url = (
        "https://europe.iiif.teklia.com/iiif/2/geneanet%2FArdennes_BMS%2F380403%2F00026.png/"
        "239,208,1232,443/full/0/default.jpg"
    )
    rows = [_row("rec-1", "val", non_jpg_url)]
    page_path, digest = _cache_file(scratch, "page.jpg", b"page-bytes")
    fetched = {"geneanet/Ardennes_BMS/380403/00026.png": _fetched(page_path, digest)}
    report = _build(scratch, rows, fetched)
    assert report["admitted_page_count"] == 0
    assert report["refusals"][0]["reason"] == "unrecognized-page-extension"


def test_refuses_dimension_mismatch(scratch):
    rows = [_row("rec-1", "val", VAL_PAGE_URL)]
    page_path, digest = _cache_file(scratch, "page.jpg", b"page-bytes")
    fetched = {
        "geneanet/Ardennes_BMS/380403/00026.jpg": _fetched(
            page_path, digest, width=1000, height=1500, declared_width=4000, declared_height=6000
        )
    }
    report = _build(scratch, rows, fetched)
    assert report["admitted_page_count"] == 0
    assert report["refusals"][0]["reason"] == "dimension-mismatch"


def test_refused_page_does_not_taint_a_later_page_with_a_phantom_duplicate(scratch):
    # A page sorts before another (by identifier) and shares its response bytes,
    # but is itself refused `unrecognized-page-extension` — a check that used to
    # run *after* this module registered a page's digest for dedupe purposes.
    # If registration ever runs for a page that is not actually admitted, the
    # later, byte-identical, otherwise-good page is wrongly refused
    # `duplicate-page-bytes` naming a page that was never in the submission.
    bad_extension_url = (
        "https://europe.iiif.teklia.com/iiif/2/geneanet%2FArdennes_BMS%2F100000%2F00001.png/"
        "1,1,50,50/full/0/default.jpg"
    )
    rows = [
        _row("rec-1", "val", bad_extension_url),
        _row("rec-2", "val", VAL_PAGE_URL),
    ]
    shared_path, shared_digest = _cache_file(scratch, "shared.jpg", b"identical-bytes")
    fetched = {
        "geneanet/Ardennes_BMS/100000/00001.png": _fetched(shared_path, shared_digest),
        "geneanet/Ardennes_BMS/380403/00026.jpg": _fetched(shared_path, shared_digest),
    }
    report = _build(scratch, rows, fetched)
    assert report["admitted_page_count"] == 1
    reasons = {r["reason"] for r in report["refusals"]}
    assert reasons == {"unrecognized-page-extension"}


def test_refuses_unsafe_source_segment(scratch):
    # A source carrying "/" is now refused as `unsafe-source-value` when the
    # fetch plan is built (plan.py), before this page ever reaches submission's
    # own `_unsafe_page_segment` admission-time screen — the row never mints a
    # page at all, so nothing for `build_submission` to admit or refuse.
    rows = [_row("rec-1", "val", VAL_PAGE_URL, source="../../escaped")]
    page_path, digest = _cache_file(scratch, "page.jpg", b"page-bytes")
    fetched = {"geneanet/Ardennes_BMS/380403/00026.jpg": _fetched(page_path, digest)}
    snapshot = _snapshot(rows)
    plan = build_fetch_plan(snapshot["rows"], snapshot["self_hash"])
    assert plan["pages"] == []
    assert plan["refusals"][0]["reason"] == "unsafe-source-value"
    report = _build(scratch, rows, fetched)
    assert report["admitted_page_count"] == 0
    for root, _dirnames, filenames in os.walk(scratch):
        for filename in filenames:
            assert "escaped" not in str(Path(root) / filename)


def test_refuses_unsafe_identifier_segment_at_admission(scratch):
    # `plan.py`'s own `unsafe-source-value` screen (exercised above) stops a
    # row's `source` before a page is ever minted, so it can never drive
    # submission's own `_unsafe_page_segment` admission-time guard. That guard
    # is still defence in depth (verdict #21): a re-sealed or hand-built plan
    # can carry an already-minted page whose `source` was mutated after the
    # fact, with a `self_hash` recomputed to match. This test builds exactly
    # that plan and asserts submission's own screen — not plan.py's — is what
    # refuses it.
    import copy

    from common.contracts.canonical import self_hash as recompute_self_hash

    rows = [_row("rec-1", "val", VAL_PAGE_URL)]
    snapshot = _snapshot(rows)
    plan = build_fetch_plan(snapshot["rows"], snapshot["self_hash"])
    holdout = build_holdout(snapshot["rows"], snapshot["self_hash"])

    tampered = copy.deepcopy(dict(plan))
    del tampered["self_hash"]
    tampered["pages"][0]["source"] = "../../escaped"
    tampered["self_hash"] = recompute_self_hash(tampered)

    page_path, digest = _cache_file(scratch, "page.jpg", b"page-bytes")
    fetched = {"geneanet/Ardennes_BMS/380403/00026.jpg": _fetched(page_path, digest)}
    report = build_submission(
        tampered,
        snapshot,
        holdout,
        fetched,
        submissions_root=scratch / "submissions",
        sidecars_root=scratch / "sidecars",
        ledger_root=scratch / "ledger",
        shard_prefix="val",
    )
    assert report["admitted_page_count"] == 0
    assert report["refusals"][0]["reason"] == "unsafe-identifier-segment"
    for root, _dirnames, filenames in os.walk(scratch):
        for filename in filenames:
            assert "escaped" not in str(Path(root) / filename)


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


def test_symlinked_directory_inside_submission_folder_refused(scratch):
    rows = [_row("rec-1", "val", VAL_PAGE_URL)]
    page_path, digest = _cache_file(scratch, "page.jpg", b"page-bytes")
    fetched = {"geneanet/Ardennes_BMS/380403/00026.jpg": _fetched(page_path, digest)}
    report = _build(scratch, rows, fetched)
    folder = Path(report["shards"][0]["folder"])

    hidden = scratch / "hidden"
    hidden.mkdir()
    (hidden / "sidecar.json").write_bytes(b'{"schema": "recordgold-page-records.v1"}')
    (folder / "link").symlink_to(hidden, target_is_directory=True)
    with pytest.raises(CorpusRefusal, match="unexpected-file-in-submission-folder"):
        refuse_non_image_files(folder)


def test_pre_existing_stray_file_refuses_the_whole_build(scratch):
    rows = [_row("rec-1", "val", VAL_PAGE_URL)]
    page_path, digest = _cache_file(scratch, "page.jpg", b"page-bytes")
    fetched = {"geneanet/Ardennes_BMS/380403/00026.jpg": _fetched(page_path, digest)}

    shard_folder = scratch / "submissions" / "val-0001"
    shard_folder.mkdir(parents=True)
    (shard_folder / ".DS_Store").write_bytes(b"junk")

    with pytest.raises(CorpusRefusal, match="unexpected-file-in-submission-folder"):
        _build(scratch, rows, fetched, shard_prefix="val")
    assert not any((scratch / "ledger").glob("*.manifest.json"))


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
    # Different volumes (380403 vs 383351) land in separate shards; check the
    # union of every shard's own manifest against the images actually written.
    manifest_relative_paths: set[str] = set()
    for shard in report["shards"]:
        folder = Path(shard["folder"])
        sources = inventory.read_submission(folder, max_bytes=0)
        manifest = submit.build_manifest(
            [
                {"relative_path": s.relative_path, "sha256": s.sha256, "bytes": s.size}
                for s in sources
            ]
        )
        manifest_relative_paths.update(entry["relative_path"] for entry in manifest["files"])
    expected_relative_paths = {
        "Ardennes/geneanet/Ardennes_BMS/380403/00026.jpg",
        "Ardennes/geneanet/Ardennes_BMS/383351/00143.jpg",
    }
    assert manifest_relative_paths == expected_relative_paths

    # And the manifests submission() actually wrote are the same ones, on disk.
    written_relative_paths: set[str] = set()
    for shard in report["shards"]:
        written = submit.load_manifest(Path(shard["manifest_path"]))
        written_relative_paths.update(entry["relative_path"] for entry in written["files"])
    assert written_relative_paths == expected_relative_paths


# --- sidecars-root-not-inside-submissions-root, before either exists ----------------


def test_refuses_same_sidecars_and_submissions_root_before_either_exists(scratch):
    shared = scratch / "shared_root"
    assert not shared.exists()
    rows = [_row("rec-1", "val", VAL_PAGE_URL)]
    page_path, digest = _cache_file(scratch, "page.jpg", b"page-bytes")
    fetched = {"geneanet/Ardennes_BMS/380403/00026.jpg": _fetched(page_path, digest)}
    snapshot = _snapshot(rows)
    plan = build_fetch_plan(snapshot["rows"], snapshot["self_hash"])
    holdout = build_holdout(snapshot["rows"], snapshot["self_hash"])
    with pytest.raises(CorpusRefusal, match=r"^malformed-record: the sidecars root"):
        build_submission(
            plan,
            snapshot,
            holdout,
            fetched,
            submissions_root=shared,
            sidecars_root=shared,
            ledger_root=scratch / "ledger",
            shard_prefix="val",
        )


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


def _fake_page(index, *, split, source):
    return {
        "identifier": f"geneanet/{source}/vol/{index:05d}.jpg",
        "splits_present": [split],
        "source": source,
        "volume": f"geneanet/{source}/vol",
        "designation": f"{index:05d}.jpg",
        "records": [],
    }


def test_partition_into_shards_never_mixes_split_or_source_across_a_cap_boundary():
    admitted = [
        (_fake_page(index, split="train", source="Ardennes"), None) for index in range(3)
    ] + [(_fake_page(index, split="val", source="Tours"), None) for index in range(3)]
    shards = partition_into_shards(admitted, max_pages_per_shard=4)
    for shard in shards:
        splits = {tuple(page["splits_present"]) for page, _fetched in shard}
        sources = {page["source"] for page, _fetched in shard}
        assert len(splits) == 1
        assert len(sources) == 1
    seen = {page["identifier"] for shard in shards for page, _fetched in shard}
    assert len(seen) == 6


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

    tampered = dict(good)
    tampered["source"] = "Tours"
    with pytest.raises(CorpusRefusal, match="self-hash-mismatch"):
        validate_sidecar(tampered)

    with_ordinal = dict(good)
    with_ordinal["ordinal"] = 1
    del with_ordinal["self_hash"]
    with pytest.raises(CorpusRefusal, match="malformed-record"):
        validate_sidecar(with_ordinal)


def test_validate_sidecar_refuses_a_non_string_element_in_splits_present_by_name():
    """A non-string in `splits_present` must refuse by name, not leak a bare `TypeError`.

    `splits_present != sorted(set(splits_present))` sorts before checking element
    types; mixing `str` and `int` raises an unguarded `TypeError` in CPython, and
    an unhashable element (a `dict` or a `list`) raises inside `set()` before the
    sort even runs.
    """
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

    mixed_types = dict(good)
    mixed_types["splits_present"] = [1, "val"]
    with pytest.raises(CorpusRefusal, match="^malformed-record:"):
        validate_sidecar(mixed_types)

    unhashable = dict(good)
    unhashable["splits_present"] = [{"a": 1}]
    with pytest.raises(CorpusRefusal, match="^malformed-record:"):
        validate_sidecar(unhashable)


def test_validate_sidecar_refuses_zero_sized_page():
    from operations.corpus.sidecar import build_sidecar

    with pytest.raises(CorpusRefusal, match="^malformed-record: page.width"):
        build_sidecar(
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
            page={"sha256": "a" * 64, "width": 0, "height": 6000},
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


def test_validate_sidecar_refuses_region_outside_page():
    from operations.corpus.sidecar import build_sidecar

    with pytest.raises(CorpusRefusal, match="^region-outside-page:"):
        build_sidecar(
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
                    "region": {"x": 3995, "y": 1, "w": 10, "h": 10},
                    "text": "hello",
                    "text_sha256": digest_bytes(b"hello"),
                    "start_date": None,
                    "end_date": None,
                    "parish": None,
                }
            ],
        )


# --- integrate.fetched_pages_from_log: the U2/U3 seam --------------------------------


def _fetched_entry(identifier: str, response_sha256: str, **overrides) -> dict:
    entry = {
        "identifier": identifier,
        "physical_page_id": "pac_" + "0" * 40,
        "status": "fetched",
        "info_url": "https://europe.iiif.teklia.com/iiif/2/x/info.json",
        "image_url": "https://europe.iiif.teklia.com/iiif/2/x/full/full/0/default.jpg",
        "size_parameter_used": "full",
        "response_sha256": response_sha256,
        "bytes": 10,
        "http_status": 200,
        "fetched_at_utc": "2026-09-01T00:00:00Z",
        "declared_width": 4000,
        "declared_height": 6000,
        "width": 4000,
        "height": 6000,
    }
    entry.update(overrides)
    return entry


def _refused_entry(identifier: str, *, status: str = "refused", reason: str = "http-error") -> dict:
    return {
        "identifier": identifier,
        "physical_page_id": "pac_" + "0" * 40,
        "status": status,
        "reason": reason,
        "detail": f"{reason}: synthetic entry for a test",
    }


def _fetch_log(entries: list[dict], *, split: str = "val", halted=None) -> dict:
    from common.contracts.canonical import self_hash as compute_self_hash

    body = {
        "schema": fetch_module.FETCH_LOG_SCHEMA,
        "split": split,
        "plan_self_hash": digest_bytes(b"plan"),
        "holdout_self_hash": None,
        "entries": entries,
        "halted": halted,
    }
    body["self_hash"] = compute_self_hash(body)
    return body


def test_fetched_pages_from_log_builds_verified_fetched_pages(scratch):
    digest = _cache_body(scratch, b"cached-bytes")
    identifier = "geneanet/Ardennes_BMS/380403/00026.jpg"
    entry = _fetched_entry(identifier, digest)
    log = _fetch_log([entry])

    fetched = fetched_pages_from_log(log, scratch / "cache")

    assert set(fetched) == {identifier}
    page = fetched[identifier]
    assert page.cache_path == scratch / "cache" / f"{digest}.jpg"
    assert page.response_sha256 == digest
    assert page.info_url == entry["info_url"]
    assert page.image_url == entry["image_url"]
    assert page.size_parameter == "full"
    assert page.bytes == entry["bytes"]
    assert page.http_status == 200
    assert page.fetched_at_utc == entry["fetched_at_utc"]
    assert (page.declared_width, page.declared_height) == (4000, 6000)
    assert (page.width, page.height) == (4000, 6000)


def test_fetched_pages_from_log_refuses_missing_cache_file(scratch):
    entry = _fetched_entry("geneanet/x/y/1.jpg", "a" * 64)
    log = _fetch_log([entry])
    with pytest.raises(CorpusRefusal, match="fetched-page-cache-missing"):
        fetched_pages_from_log(log, scratch / "cache")


def test_fetched_pages_from_log_refuses_digest_mismatch(scratch):
    digest = _cache_body(scratch, b"actual-bytes")
    # The cache file is tampered with after the log claimed this digest.
    (scratch / "cache" / f"{digest}.jpg").write_bytes(b"tampered-bytes")
    entry = _fetched_entry("geneanet/x/y/1.jpg", digest)
    log = _fetch_log([entry])
    with pytest.raises(CorpusRefusal, match="fetched-page-cache-digest-mismatch"):
        fetched_pages_from_log(log, scratch / "cache")


def test_fetched_pages_from_log_skips_refused_and_halted_entries_and_counts_them(scratch):
    digest = _cache_body(scratch, b"good-bytes")
    fetched_entry = _fetched_entry("geneanet/x/y/1.jpg", digest)
    refused_entry = _refused_entry("geneanet/x/y/2.jpg", status="refused", reason="http-error")
    halted_entry = _refused_entry("geneanet/x/y/3.jpg", status="halted", reason="Http403Stop")
    log = _fetch_log([fetched_entry, refused_entry, halted_entry])

    fetched = fetched_pages_from_log(log, scratch / "cache")

    assert set(fetched) == {"geneanet/x/y/1.jpg"}
    non_fetched = [entry for entry in log["entries"] if entry["status"] != "fetched"]
    assert len(non_fetched) == 2
    assert {entry["status"] for entry in non_fetched} == {"refused", "halted"}


def test_fetched_pages_from_log_revalidates_the_log():
    tampered = _fetch_log([_fetched_entry("geneanet/x/y/1.jpg", "a" * 64)])
    tampered["split"] = "test"  # mutate after sealing: self_hash no longer verifies
    with pytest.raises(CorpusRefusal, match="self-hash-mismatch"):
        fetched_pages_from_log(tampered, Path("/does-not-matter"))


# --- the tracked CLI: `python -m operations.corpus.submission` -----------------------


def test_cli_builds_a_shard_from_a_synthetic_log_and_cache(scratch, tmp_path):
    rows = [_row("rec-1", "val", VAL_PAGE_URL)]
    snapshot = _snapshot(rows)
    plan = build_fetch_plan(snapshot["rows"], snapshot["self_hash"])
    holdout = build_holdout(snapshot["rows"], snapshot["self_hash"])

    digest = _cache_body(scratch, b"page-bytes")
    entry = _fetched_entry("geneanet/Ardennes_BMS/380403/00026.jpg", digest)
    log = _fetch_log([entry], split="val")

    snapshot_path = tmp_path / "snapshot.json"
    plan_path = tmp_path / "plan.json"
    holdout_path = tmp_path / "holdout.json"
    log_path = tmp_path / "fetch-log.json"
    snapshot_path.write_bytes(json.dumps(snapshot).encode("utf-8"))
    plan_path.write_bytes(json.dumps(plan).encode("utf-8"))
    holdout_path.write_bytes(json.dumps(holdout).encode("utf-8"))
    log_path.write_bytes(json.dumps(log).encode("utf-8"))

    report = submission_main(
        [
            "--snapshot",
            str(snapshot_path),
            "--plan",
            str(plan_path),
            "--holdout",
            str(holdout_path),
            "--fetch-log",
            str(log_path),
            "--cache-root",
            str(scratch / "cache"),
            "--submissions-root",
            str(scratch / "submissions"),
            "--sidecars-root",
            str(scratch / "sidecars"),
            "--ledger-root",
            str(scratch / "ledger"),
            "--shard-prefix",
            "val",
        ]
    )

    assert report["admitted_page_count"] == 1
    assert report["refused_page_count"] == 0
    assert report["shards"][0]["shard_id"] == "val-0001"
    image = (
        Path(report["shards"][0]["folder"])
        / "Ardennes"
        / "geneanet"
        / "Ardennes_BMS"
        / "380403"
        / "00026.jpg"
    )
    assert image.read_bytes() == b"page-bytes"

    report_path = scratch / "ledger" / "val-submission-report.json"
    assert report_path.exists()
    on_disk = json.loads(report_path.read_bytes())
    assert on_disk["schema"] == "recordgold-submission-report.v1"
    assert on_disk["admitted_page_count"] == 1


def test_cli_requires_every_flag():
    with pytest.raises(SystemExit):
        submission_main(["--snapshot", "x.json"])


# --- full round trip: run_fetch (loopback server) -> seal log -> integrate -> build --


def test_round_trip_fetch_log_to_submission(scratch, tmp_path):
    import threading

    from common.contracts.canonical import self_hash as compute_self_hash
    from operations.corpus import plan as plan_module
    from operations.corpus.fetch import FetchConfig, run_fetch
    from operations.corpus.test_fetch import GOOD_JPEG, INFO_JSON, _Handler, _Server
    from operations.corpus.test_fetch import _page as _fetch_test_page
    from operations.corpus.test_fetch import _script as _fetch_script

    httpd = _Server(("127.0.0.1", 0), _Handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        _fetch_script(
            httpd, "vol/roundtrip.jpg", info=[{"body": INFO_JSON}], full=[{"body": GOOD_JPEG}]
        )

        # A "val"-only row: `build_holdout` only ever parses `record_url` for a
        # `test`-split row (`holdout.py`), so a placeholder URL here never has to
        # satisfy `parse_record_url`'s real-IIIF-host requirement — only the
        # hand-built plan page below, pointed at the loopback server, has to.
        rows = [_row("rec-1", "val", "https://placeholder.example/unused")]
        snapshot = _snapshot(rows)
        holdout = build_holdout(snapshot["rows"], snapshot["self_hash"])

        page = _fetch_test_page(
            httpd,
            "vol/roundtrip.jpg",
            records=[{"record_id": "rec-1", "region": {"x": 5, "y": 5, "w": 10, "h": 10}}],
        )
        plan_body = {
            "schema": plan_module.SCHEMA,
            "corpus_id": plan_module.CORPUS_ID,
            "source_row_snapshot_self_hash": snapshot["self_hash"],
            "pages": [page],
            "refusals": [],
            "measurements": {},
        }
        plan_body["self_hash"] = compute_self_hash(plan_body)
        plan = plan_module.validate_plan(plan_body)

        fetch_cache_root = tmp_path / "fetch-cache"
        config = FetchConfig(
            cache_root=fetch_cache_root,
            info_root=tmp_path / "fetch-info",
            min_interval_seconds=0.0,
            sleep=lambda seconds: None,
            clock=lambda: "2026-09-01T00:00:00+00:00",
        )
        result = run_fetch(plan, config, split="val", holdout=holdout)
        assert result.halted is None
        assert result.entries[0]["status"] == "fetched"

        log = fetch_module._seal_fetch_log(result, split="val", plan=plan, holdout=holdout)
        log = fetch_module.validate_fetch_log(log)

        fetched_pages = fetched_pages_from_log(log, fetch_cache_root)
        assert set(fetched_pages) == {"vol/roundtrip.jpg"}

        report = build_submission(
            plan,
            snapshot,
            holdout,
            fetched_pages,
            submissions_root=scratch / "submissions",
            sidecars_root=scratch / "sidecars",
            ledger_root=scratch / "ledger",
            shard_prefix="roundtrip",
        )
        assert report["admitted_page_count"] == 1
        assert report["refused_page_count"] == 0
        image = next(Path(report["shards"][0]["folder"]).rglob("*.jpg"))
        assert image.read_bytes() == GOOD_JPEG
    finally:
        httpd.shutdown()
        thread.join(timeout=5)
