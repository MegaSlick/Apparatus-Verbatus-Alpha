"""Pure tests for `operations/corpus/local_admission.py`: synthetic sets, no network.

The corpus's forty 180-degree records are the case (independent audit of
2026-09-10, F4). A synthetic two-page set -- one page stated upright, one stated
through a 180-degree view -- is written under `tmp_path` with real JPEG bytes.

**What the pixel test here can and cannot prove.** It builds the rotated view by
rotating the stored page itself, so it confirms that the formula and the
rotation agree -- the algebra, over real pixel data. It cannot tell a right
formula from a wrong one, because both sides of it rest on the same assumption
about which frame the stored page is in. That question was settled outside this
file, against the real material; the answer is recorded in `transform_region`'s
docstring and in the host's rotation check of 2026-09-11.

Every reason in `LOCAL_ADMISSION_REFUSAL_REASONS` is shown to fire here, and the
coverage test at the bottom derives what was exercised from this file's own
assertions rather than from a hand-typed list (`test_rows.py`'s convention).
"""

from __future__ import annotations

import ast
import io
import json
from pathlib import Path

import pytest
from PIL import Image

from common.contracts.canonical import digest_bytes
from common.contracts.canonical import self_hash as _self_hash
from operations.corpus import CorpusRefusal
from operations.corpus.local_admission import (
    LOCAL_ADMISSION_REFUSAL_REASONS,
    SCHEMA,
    admit_local_set,
    load_local_admission_ledger,
    main,
    transform_region,
    validate_local_admission_ledger,
)
from operations.corpus.reference import validate_reference_page
from operations.corpus.rows import build_snapshot

HOST = "https://europe.iiif.teklia.com/iiif/2/"
UPRIGHT_ID = "dai-cretdhi%2FTours%2FSaint_X%2F6NUM_001%2FFRAD037_001_002.JPG"
ROTATED_ID = "dai-cretdhi%2FIle_de_re%2Fimg%2FSMa-BMS_1637%2Ffr_ad017_0661.jpg"
WIDTH, HEIGHT = 400, 300


def _page_bytes(seed: int, *, exif_orientation: int | None = None) -> bytes:
    """A page whose every 20x20 cell carries a distinct colour, so a crop is unambiguous."""
    image = Image.new("RGB", (WIDTH, HEIGHT))
    pixels = image.load()
    for y in range(HEIGHT):
        for x in range(WIDTH):
            pixels[x, y] = ((x // 20 * 13 + seed) % 256, (y // 20 * 29 + seed) % 256, (x + y) % 256)
    buffer = io.BytesIO()
    if exif_orientation is None:
        image.save(buffer, format="JPEG", quality=95)
    else:
        exif = Image.Exif()
        exif[0x0112] = exif_orientation
        image.save(buffer, format="JPEG", quality=95, exif=exif)
    return buffer.getvalue()


def _url(identifier: str, box: list[int], rotation: str) -> str:
    x, y, w, h = box
    return f"{HOST}{identifier}/{x},{y},{w},{h}/full/{rotation}/default.jpg"


ROTATED_URL_180 = _url(ROTATED_ID, [100, 40, 160, 100], "180")


def _record(
    record_id,
    page_id,
    identifier,
    source_box,
    rotation,
    *,
    source="Tours",
    split="val",
    text="un texte",
):
    if rotation == "0":
        bbox = list(source_box)
    else:
        x, y, w, h = source_box
        bbox = [WIDTH - x - w, HEIGHT - y - h, w, h]
    return {
        "end_date": 1700,
        "parish": "Saint-X",
        "record_id": record_id,
        "record_url": _url(identifier, source_box, rotation),
        "source": source,
        "split": split,
        "start_date": 1700,
        "text": text,
        "bbox": bbox,
        "source_bbox": list(source_box),
        "iiif_rotation": rotation,
        "page_id": page_id,
    }


def _write_set(
    root: Path,
    records: list[dict],
    pages: dict[str, bytes],
    *,
    receipt_ok=True,
    manifest_overrides: dict[str, dict] | None = None,
) -> Path:
    (root / "pages").mkdir(parents=True)
    for page_id, body in pages.items():
        (root / "pages" / f"{page_id}.jpg").write_bytes(body)
    manifest_rows = []
    for page_id in pages:
        row = {
            "page_id": page_id,
            "image": f"pages/{page_id}.jpg",
            "width": WIDTH,
            "height": HEIGHT,
            "iiif_rotation": "0",
            "records": [entry for entry in records if entry["page_id"] == page_id],
        }
        row.update((manifest_overrides or {}).get(page_id, {}))
        manifest_rows.append(row)
    gold = "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in records).encode()
    manifest = "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in manifest_rows).encode()
    (root / "gold.jsonl").write_bytes(gold)
    (root / "page_manifest.jsonl").write_bytes(manifest)
    receipt = {
        "schema": "recordgold_full_page_fetch_v1",
        "status": "complete",
        "requested_splits": ["val"],
        "artifacts": {
            "gold_jsonl_sha256": digest_bytes(gold) if receipt_ok else "0" * 64,
            "page_manifest_jsonl_sha256": digest_bytes(manifest),
        },
    }
    (root / "fetch_receipt.json").write_text(json.dumps(receipt))
    return root


UPRIGHT_BOX = [40, 60, 200, 80]
ROTATED_BOX = [100, 40, 160, 100]  # stated in the 180-degree view


def _two_page_set(root: Path, **overrides) -> Path:
    records = [
        _record("r-up", "page-up", UPRIGHT_ID, UPRIGHT_BOX, "0"),
        _record("r-up-2", "page-up", UPRIGHT_ID, [10, 200, 100, 50], "0", text="autre texte"),
        _record(
            "r-rot", "page-rot", ROTATED_ID, ROTATED_BOX, "180", source="Ile de Ré", text="tourné"
        ),
    ]
    for record in records:
        record.update(overrides.get(record["record_id"], {}))
    return _write_set(
        root,
        records,
        {"page-up": _page_bytes(1), "page-rot": _page_bytes(2)},
        manifest_overrides=overrides.get("_manifest"),
    )


def _reseal(ledger: dict) -> dict:
    """A ledger whose self-hash is right again after an edit, so the checks past it fire."""
    body = {key: value for key, value in ledger.items() if key != "self_hash"}
    body["self_hash"] = _self_hash(body)
    return body


def _only_reason(ledger: dict, reason: str, *, count: int = 1) -> dict:
    """Assert the ledger refused exactly `count` record(s), all for `reason`.

    The coverage test at the bottom reads reason names out of these calls as
    well as out of `pytest.raises`, because most of this module's vocabulary
    lands in a ledger row rather than in a raised exception.
    """
    assert ledger["summary"]["refused_by_reason"] == {reason: count}
    assert reason in LOCAL_ADMISSION_REFUSAL_REASONS
    return ledger


def test_transform_region_is_the_identity_at_zero_and_the_far_corner_at_180():
    region = {"x": 100, "y": 40, "w": 160, "h": 100}
    assert transform_region(region, rotation="0", width=WIDTH, height=HEIGHT) == region
    assert transform_region(region, rotation="180", width=WIDTH, height=HEIGHT) == {
        "x": WIDTH - 100 - 160,
        "y": HEIGHT - 40 - 100,
        "w": 160,
        "h": 100,
    }
    with pytest.raises(CorpusRefusal, match="^unsupported-rotation-parameter:"):
        transform_region(region, rotation="90", width=WIDTH, height=HEIGHT)


def test_the_180_carry_and_a_180_rotation_agree_over_real_pixels():
    """The algebra against pixels; which frame the stored page is in is settled elsewhere.

    Rotating the stored page to build the view means both sides of this
    assertion rest on the same assumption, so it confirms that the formula is
    the point reflection it claims to be and nothing further.
    """
    stored = Image.open(io.BytesIO(_page_bytes(2))).convert("RGB")
    rotated_view = stored.rotate(180)
    x, y, w, h = ROTATED_BOX
    seen_in_view = rotated_view.crop((x, y, x + w, y + h)).rotate(180)
    carried = transform_region(
        {"x": x, "y": y, "w": w, "h": h}, rotation="180", width=WIDTH, height=HEIGHT
    )
    on_stored = stored.crop(
        (carried["x"], carried["y"], carried["x"] + carried["w"], carried["y"] + carried["h"])
    )
    assert seen_in_view.size == on_stored.size
    assert seen_in_view.tobytes() == on_stored.tobytes()


def test_every_record_of_a_set_is_admitted_or_refused_with_rotation_provenance(tmp_path):
    root = _two_page_set(tmp_path / "set")
    ledger = admit_local_set(root, split="val", output_dir=tmp_path / "out")

    assert ledger["schema"] == SCHEMA
    assert ledger["summary"] == {
        "records": 3,
        "admitted": 3,
        "refused": 0,
        "refused_by_reason": {},
        "pages_listed": 2,
        "pages_by_outcome": {
            "admitted": 2,
            "refused": 0,
            "no-gold-row": 0,
            "all-records-refused": 0,
        },
        "pages_admitted": 2,
        "pages_refused": 0,
        "admitted_by_rotation": {"0": 2, "180": 1},
    }
    assert ledger["receipt"]["digests"]["gold.jsonl"] == digest_bytes(
        (root / "gold.jsonl").read_bytes()
    )
    assert ledger["receipt"]["receipt_sha256"] == digest_bytes(
        (root / "fetch_receipt.json").read_bytes()
    )
    assert ledger["row_snapshot"] == {
        "consulted": False,
        "self_hash": None,
        "records_cross_checked": 0,
    }
    rows = {row["record_id"]: row for row in ledger["rows"]}
    rotated = rows["r-rot"]
    assert rotated["decision"] == "admitted"
    assert rotated["iiif_rotation"] == "180"
    assert rotated["source_bbox"] == ROTATED_BOX
    x, y, w, h = ROTATED_BOX
    assert rotated["bbox"] == [WIDTH - x - w, HEIGHT - y - h, w, h]
    assert rotated["record_url"].endswith("/full/180/default.jpg")
    assert rotated["page_sha256"] == digest_bytes((root / "pages" / "page-rot.jpg").read_bytes())
    assert rotated["page_width"] == WIDTH and rotated["page_height"] == HEIGHT
    assert rotated["physical_act_id"].startswith("pac_")
    assert rotated["physical_page_id"].startswith("ppg_")

    pages = {
        page["page"]["sha256"]: validate_reference_page(page) for page in ledger["reference_pages"]
    }
    reference = pages[rotated["page_sha256"]]
    assert reference["source"] == "Ile de Ré"
    assert reference["split"] == "val"
    (act,) = reference["acts"]
    assert act["record_id"] == "r-rot"
    assert act["region"] == {"x": WIDTH - x - w, "y": HEIGHT - y - h, "w": w, "h": h}
    assert act["physical_act_id"] == rotated["physical_act_id"]
    assert rotated["reference_page_self_hash"] == reference["self_hash"]
    assert {row["record_id"] for row in ledger["rows"]} == {"r-up", "r-up-2", "r-rot"}
    assert len({row["physical_act_id"] for row in ledger["rows"]}) == 3

    # The written ledger is the validated one, and loading it re-validates.
    assert load_local_admission_ledger(tmp_path / "out" / "ledger.json") == ledger
    assert len((tmp_path / "out" / "reference-pages.jsonl").read_text().splitlines()) == 2
    with pytest.raises(CorpusRefusal, match="never overwritten"):
        admit_local_set(root, split="val", output_dir=tmp_path / "out")


def test_every_row_carries_one_closed_shape_whether_admitted_or_refused(tmp_path):
    root = _two_page_set(tmp_path / "set", **{"r-rot": {"text": ""}})
    ledger = admit_local_set(root, split="val")
    assert len({frozenset(row) for row in ledger["rows"]}) == 1, (
        "an admitted row and a refused row answer the same questions"
    )
    refused = next(row for row in ledger["rows"] if row["decision"] == "refused")
    assert refused["physical_act_id"] is None and refused["reference_page_self_hash"] is None
    assert refused["reason"] and refused["detail"]


def test_admission_changes_nothing_in_the_set(tmp_path):
    root = _two_page_set(tmp_path / "set")
    before = {
        p: (digest_bytes(p.read_bytes()), p.stat().st_mtime_ns)
        for p in root.rglob("*")
        if p.is_file()
    }
    admit_local_set(root, split="val")
    assert {
        p: (digest_bytes(p.read_bytes()), p.stat().st_mtime_ns)
        for p in root.rglob("*")
        if p.is_file()
    } == before


def test_an_output_directory_inside_the_set_is_refused_before_anything_is_read(tmp_path):
    root = _two_page_set(tmp_path / "set")
    with pytest.raises(CorpusRefusal, match="^output-inside-set:"):
        admit_local_set(root, split="val", output_dir=root / "ledger")
    assert not (root / "ledger").exists()


def test_the_held_split_needs_a_deliberate_release_and_the_flag_releases_nothing_else(tmp_path):
    root = _two_page_set(tmp_path / "set")
    with pytest.raises(CorpusRefusal, match="^held-split-not-released:"):
        admit_local_set(root, split="test")
    with pytest.raises(CorpusRefusal, match="^held-split-not-released:"):
        admit_local_set(root, split="val", release_test_split=True)
    # Released, the held split is admitted like any other -- and every row of
    # this set carries `val`, so each one is refused by its own split.
    ledger = admit_local_set(root, split="test", release_test_split=True)
    assert ledger["summary"]["refused_by_reason"] == {"unknown-split": 3}


@pytest.mark.parametrize(
    ("override", "reason"),
    [
        # A 90-degree view has no conversion: refused by name, never treated as upright.
        (
            {"record_url": _url(ROTATED_ID, ROTATED_BOX, "90"), "iiif_rotation": "90"},
            "unsupported-rotation-parameter",
        ),
        # The row says one rotation and its URL another.
        ({"iiif_rotation": "0"}, "inconsistent-transform"),
        # A transformed box that is not the carry of the source box.
        ({"bbox": [0, 0, 160, 100]}, "inconsistent-transform"),
        # A box carried off the page.
        (
            {
                "record_url": _url(ROTATED_ID, [-5, 40, 160, 100], "180"),
                "source_bbox": [-5, 40, 160, 100],
                "bbox": [WIDTH + 5 - 160, HEIGHT - 140, 160, 100],
            },
            "non-positive-region",
        ),
        ({"source": "Ile/de/Re"}, "unsafe-source-value"),
        ({"text": ""}, "malformed-record"),
        # Non-empty ink that normalises to nothing. Admitted before, it took the
        # whole scoring run down later under a refusal naming no record at all.
        ({"text": "   "}, "empty-normalized-text"),
        ({"split": "train"}, "unknown-split"),
        ({"record_url": "not-a-iiif-url"}, "unparseable-record-url"),
        (
            {"record_url": ROTATED_URL_180.replace("europe.iiif.teklia.com", "example.invalid")},
            "unexpected-host",
        ),
        (
            {"record_url": ROTATED_URL_180.replace("/full/180/", "/max/180/")},
            "unsupported-size-parameter",
        ),
        (
            {"record_url": ROTATED_URL_180.replace("/default.jpg", "/gray.jpg")},
            "unsupported-quality-parameter",
        ),
        (
            {"record_url": ROTATED_URL_180.replace("/default.jpg", "/default.png")},
            "unsupported-format-parameter",
        ),
        (
            {"record_url": _url("dai-cretdhi%2F..%2Ffr_ad017_0661.jpg", ROTATED_BOX, "180")},
            "unsafe-identifier-segment",
        ),
        # A designation that is nothing but whitespace declares no page at all.
        (
            {"record_url": _url("dai-cretdhi%2FIle_de_re%2F%20", ROTATED_BOX, "180")},
            "unmintable-page-identity",
        ),
    ],
)
def test_a_record_that_cannot_be_carried_honestly_is_refused_by_name(tmp_path, override, reason):
    root = _two_page_set(tmp_path / "set", **{"r-rot": override})
    ledger = admit_local_set(root, split="val")
    assert ledger["summary"]["records"] == 3
    assert ledger["summary"]["admitted"] == 2
    _only_reason(ledger, reason)
    refused = next(row for row in ledger["rows"] if row["record_id"] == "r-rot")
    assert refused["decision"] == "refused" and refused["reason"] == reason
    assert refused["record_url"] and refused["source_bbox"] is not None
    # The upright page's truth is unaffected; the rotated page has no truth to build.
    assert [len(page["acts"]) for page in ledger["reference_pages"]] == [2]
    assert ledger["summary"]["pages_by_outcome"]["all-records-refused"] == 1
    assert sum(ledger["summary"]["pages_by_outcome"].values()) == ledger["summary"]["pages_listed"]


def test_a_box_whose_carry_leaves_the_page_is_refused_not_clipped(tmp_path):
    x, y, w, h = 300, 40, 160, 100  # x + w > WIDTH in the rotated view: carried x is negative
    override = {
        "record_url": _url(ROTATED_ID, [x, y, w, h], "180"),
        "source_bbox": [x, y, w, h],
        "bbox": [WIDTH - x - w, HEIGHT - y - h, w, h],
    }
    ledger = admit_local_set(_two_page_set(tmp_path / "set", **{"r-rot": override}), split="val")
    _only_reason(ledger, "region-outside-page")


def test_two_records_claiming_one_region_refuse_the_page_they_share(tmp_path):
    """`reference.py` refuses the duplicate region; this module names whose build refused."""
    collision = {
        "record_url": _url(UPRIGHT_ID, UPRIGHT_BOX, "0"),
        "source_bbox": list(UPRIGHT_BOX),
        "bbox": list(UPRIGHT_BOX),
    }
    ledger = admit_local_set(_two_page_set(tmp_path / "set", **{"r-up-2": collision}), split="val")
    _only_reason(ledger, "reference-build-refused", count=2)
    assert ledger["summary"]["pages_admitted"] == 1


def test_a_page_whose_pixels_disagree_with_the_manifest_refuses_every_record_on_it(tmp_path):
    root = _two_page_set(tmp_path / "set")
    wrong = Image.new("RGB", (WIDTH - 1, HEIGHT))
    buffer = io.BytesIO()
    wrong.save(buffer, format="JPEG")
    (root / "pages" / "page-up.jpg").write_bytes(buffer.getvalue())
    ledger = admit_local_set(root, split="val")
    _only_reason(ledger, "dimension-mismatch", count=2)
    assert ledger["summary"]["pages_refused"] == 1 and ledger["summary"]["pages_admitted"] == 1
    assert {row["record_id"] for row in ledger["rows"] if row["decision"] == "refused"} == {
        "r-up",
        "r-up-2",
    }


def test_a_page_that_does_not_decode_or_declares_a_display_rotation_is_refused_by_name(tmp_path):
    root = _two_page_set(tmp_path / "set")
    (root / "pages" / "page-rot.jpg").write_bytes(b"this is not a JPEG at all")
    _only_reason(admit_local_set(root, split="val"), "non-image-body")

    rotated = _two_page_set(tmp_path / "exif")
    (rotated / "pages" / "page-rot.jpg").write_bytes(_page_bytes(2, exif_orientation=3))
    _only_reason(admit_local_set(rotated, split="val"), "exif-orientation")


@pytest.mark.parametrize("shape", ["traversal", "absolute"])
def test_a_manifest_image_path_that_leaves_the_set_is_refused_rather_than_read(tmp_path, shape):
    outside = tmp_path / "outside.jpg"
    outside.write_bytes(_page_bytes(9))
    image = f"../{outside.name}" if shape == "traversal" else str(outside)
    root = _two_page_set(tmp_path / f"set-{shape}", _manifest={"page-rot": {"image": image}})
    ledger = _only_reason(admit_local_set(root, split="val"), "unsafe-page-image-path")
    assert ledger["summary"]["pages_refused"] == 1


def test_a_missing_page_file_and_a_duplicate_record_are_refused_by_name(tmp_path):
    root = _two_page_set(tmp_path / "set")
    (root / "pages" / "page-rot.jpg").unlink()
    _only_reason(admit_local_set(root, split="val"), "missing-page-file")

    duplicated = _two_page_set(tmp_path / "dup")
    gold = (duplicated / "gold.jsonl").read_bytes()
    first = gold.splitlines()[0] + b"\n"
    (duplicated / "gold.jsonl").write_bytes(gold + first)
    receipt = json.loads((duplicated / "fetch_receipt.json").read_text())
    receipt["artifacts"]["gold_jsonl_sha256"] = digest_bytes(
        (duplicated / "gold.jsonl").read_bytes()
    )
    (duplicated / "fetch_receipt.json").write_text(json.dumps(receipt))
    ledger = admit_local_set(duplicated, split="val")
    assert ledger["summary"]["records"] == 4
    _only_reason(ledger, "duplicate-record-id")


def test_a_record_naming_a_page_the_manifest_never_lists_is_refused_and_both_pages_accounted(
    tmp_path,
):
    root = _two_page_set(tmp_path / "set", **{"r-rot": {"page_id": "page-nowhere"}})
    ledger = _only_reason(admit_local_set(root, split="val"), "inconsistent-page-manifest")
    assert ledger["summary"]["pages_by_outcome"]["no-gold-row"] == 1, (
        "page-rot is listed but no gold row names it any more"
    )
    assert sum(ledger["summary"]["pages_by_outcome"].values()) == ledger["summary"]["pages_listed"]


def test_a_page_listed_twice_refuses_the_whole_set(tmp_path):
    root = _two_page_set(tmp_path / "set")
    manifest = (root / "page_manifest.jsonl").read_bytes()
    (root / "page_manifest.jsonl").write_bytes(manifest + manifest.splitlines()[0] + b"\n")
    receipt = json.loads((root / "fetch_receipt.json").read_text())
    receipt["artifacts"]["page_manifest_jsonl_sha256"] = digest_bytes(
        (root / "page_manifest.jsonl").read_bytes()
    )
    (root / "fetch_receipt.json").write_text(json.dumps(receipt))
    with pytest.raises(CorpusRefusal, match="^inconsistent-page-manifest:"):
        admit_local_set(root, split="val")


def test_a_set_whose_files_are_not_the_receipts_bytes_is_refused_before_any_row(tmp_path):
    root = _write_set(
        tmp_path / "set",
        [_record("r-up", "page-up", UPRIGHT_ID, UPRIGHT_BOX, "0")],
        {"page-up": _page_bytes(1)},
        receipt_ok=False,
    )
    with pytest.raises(CorpusRefusal, match="^receipt-hash-mismatch:"):
        admit_local_set(root, split="val")
    with pytest.raises(CorpusRefusal, match="^unknown-split:"):
        admit_local_set(root, split="calibration")


def test_a_receipt_that_is_not_json_refuses_by_name_rather_than_crashing(tmp_path):
    root = _two_page_set(tmp_path / "set")
    (root / "fetch_receipt.json").write_text("{ this is not JSON")
    with pytest.raises(CorpusRefusal, match="^malformed-record:") as refused:
        admit_local_set(root, split="val")
    assert "fetch_receipt.json" in str(refused.value)


def test_a_set_file_that_is_not_utf8_refuses_by_name_rather_than_crashing(tmp_path):
    root = _two_page_set(tmp_path / "set")
    (root / "gold.jsonl").write_bytes(b"\xff\xfe not utf-8 at all\n")
    receipt = json.loads((root / "fetch_receipt.json").read_text())
    receipt["artifacts"]["gold_jsonl_sha256"] = digest_bytes((root / "gold.jsonl").read_bytes())
    (root / "fetch_receipt.json").write_text(json.dumps(receipt))
    with pytest.raises(CorpusRefusal, match="^malformed-record:") as refused:
        admit_local_set(root, split="val")
    assert "not UTF-8" in str(refused.value)


@pytest.mark.parametrize("missing", ["gold.jsonl", "page_manifest.jsonl", "fetch_receipt.json"])
def test_a_set_missing_one_of_its_files_is_refused_by_the_files_name(tmp_path, missing):
    root = _two_page_set(tmp_path / "set")
    (root / missing).unlink()
    with pytest.raises(CorpusRefusal, match="^missing-set-file:") as refused:
        admit_local_set(root, split="val")
    assert missing in str(refused.value)


# --- The sealed row snapshot: the one witness that was never in the set ----------


def _snapshot(root: Path) -> dict:
    rows = []
    for line in (root / "gold.jsonl").read_text(encoding="utf-8").splitlines():
        gold = json.loads(line)
        rows.append(
            {
                "split": gold["split"],
                "source": gold["source"],
                "record_id": gold["record_id"],
                "record_url": gold["record_url"],
                "start_date": gold["start_date"],
                "end_date": gold["end_date"],
                "parish": gold["parish"],
                "text": gold["text"],
                "text_sha256": digest_bytes(gold["text"].encode("utf-8")),
            }
        )
    return build_snapshot(
        {
            "dataset": "Teklia/DAI-CReTDHI-RecordGold-ATR",
            "parquet_sha256": {
                split: digest_bytes(split.encode()) for split in ("train", "val", "test")
            },
            "converted_at_utc": "2026-09-11T00:00:00Z",
        },
        rows,
    )


def test_a_snapshot_that_agrees_is_recorded_as_the_witness_it_was(tmp_path):
    root = _two_page_set(tmp_path / "set")
    snapshot = _snapshot(root)
    ledger = admit_local_set(root, split="val", row_snapshot=snapshot)
    assert ledger["summary"]["admitted"] == 3
    assert ledger["row_snapshot"] == {
        "consulted": True,
        "self_hash": snapshot["self_hash"],
        "records_cross_checked": 3,
    }


def test_a_text_rewritten_in_the_set_is_caught_by_the_one_witness_outside_it(tmp_path):
    """The receipt and the files it names share a directory; the snapshot does not."""
    root = _two_page_set(tmp_path / "set")
    honest = _snapshot(root)
    # Rewrite one text *and* re-write the receipt, exactly as an undetectable
    # tamper would: the receipt check passes and the snapshot check does not.
    gold = [json.loads(line) for line in (root / "gold.jsonl").read_text().splitlines()]
    for row in gold:
        if row["record_id"] == "r-rot":
            row["text"] = "un texte que personne n'a écrit"
    body = "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in gold).encode()
    (root / "gold.jsonl").write_bytes(body)
    receipt = json.loads((root / "fetch_receipt.json").read_text())
    receipt["artifacts"]["gold_jsonl_sha256"] = digest_bytes(body)
    (root / "fetch_receipt.json").write_text(json.dumps(receipt))

    assert admit_local_set(root, split="val")["summary"]["admitted"] == 3, (
        "the receipt alone cannot see it"
    )
    ledger = admit_local_set(root, split="val", row_snapshot=honest)
    _only_reason(ledger, "snapshot-mismatch")
    assert ledger["row_snapshot"]["records_cross_checked"] == 2


# --- The validator and the loader -------------------------------------------------


def test_the_validator_refuses_a_ledger_edited_after_it_was_sealed(tmp_path):
    ledger = admit_local_set(_two_page_set(tmp_path / "set"), split="val")
    assert validate_local_admission_ledger(dict(ledger)) == ledger

    edited = json.loads(json.dumps(ledger))
    edited["summary"]["admitted"] = 99
    with pytest.raises(CorpusRefusal, match="^self-hash-mismatch:"):
        validate_local_admission_ledger(edited)

    # Re-sealed after the edit, so the self-hash verifies again: now the
    # reconciliation against the rows themselves is what catches it.
    with pytest.raises(CorpusRefusal, match="^malformed-record:") as refused:
        validate_local_admission_ledger(_reseal(edited))
    assert "the rows decide" in str(refused.value)


@pytest.mark.parametrize(
    ("field", "reason"), [("schema", "wrong-schema"), ("corpus_id", "wrong-corpus")]
)
def test_the_validator_refuses_a_foreign_schema_or_corpus(tmp_path, field, reason):
    ledger = json.loads(json.dumps(admit_local_set(_two_page_set(tmp_path / "set"), split="val")))
    ledger[field] = "something-else.v1"
    with pytest.raises(CorpusRefusal, match=f"^{reason}:"):
        validate_local_admission_ledger(_reseal(ledger))


def test_the_validator_refuses_a_reason_histogram_that_does_not_sum(tmp_path):
    ledger = json.loads(
        json.dumps(
            admit_local_set(_two_page_set(tmp_path / "set", **{"r-rot": {"text": ""}}), split="val")
        )
    )
    ledger["summary"]["refused_by_reason"] = {"malformed-record": 7}
    with pytest.raises(CorpusRefusal, match="^malformed-record:") as refused:
        validate_local_admission_ledger(_reseal(ledger))
    assert "refused_by_reason" in str(refused.value)


def test_the_validator_refuses_a_page_census_that_does_not_account_for_the_manifest(tmp_path):
    ledger = json.loads(json.dumps(admit_local_set(_two_page_set(tmp_path / "set"), split="val")))
    ledger["summary"]["pages_listed"] = 5
    with pytest.raises(CorpusRefusal, match="^malformed-record:") as refused:
        validate_local_admission_ledger(_reseal(ledger))
    assert "pages_by_outcome accounts for" in str(refused.value)


def test_the_loader_is_the_only_tracked_way_back_in(tmp_path):
    root = _two_page_set(tmp_path / "set")
    admit_local_set(root, split="val", output_dir=tmp_path / "out")
    path = tmp_path / "out" / "ledger.json"
    assert load_local_admission_ledger(path)["summary"]["admitted"] == 3
    body = json.loads(path.read_bytes())
    body["rows"][0]["decision"] = "maybe"
    path.write_bytes(json.dumps(body).encode())
    with pytest.raises(CorpusRefusal, match="^self-hash-mismatch:"):
        load_local_admission_ledger(path)


# --- The command-line entry point --------------------------------------------------


def test_the_command_line_writes_a_loadable_ledger_and_prints_its_summary(tmp_path, capsys):
    root = _two_page_set(tmp_path / "set")
    snapshot_path = tmp_path / "rows.json"
    snapshot_path.write_text(json.dumps(_snapshot(root)))
    assert (
        main(
            [
                str(root),
                "--split",
                "val",
                "--output-dir",
                str(tmp_path / "out"),
                "--row-snapshot",
                str(snapshot_path),
            ]
        )
        == 0
    )
    printed = json.loads(capsys.readouterr().out)
    ledger = load_local_admission_ledger(tmp_path / "out" / "ledger.json")
    assert printed == ledger["summary"]
    assert ledger["row_snapshot"]["records_cross_checked"] == 3


def test_the_command_line_refuses_the_held_split_without_its_flag(tmp_path):
    root = _two_page_set(tmp_path / "set")
    with pytest.raises(CorpusRefusal, match="^held-split-not-released:"):
        main([str(root), "--split", "test", "--output-dir", str(tmp_path / "out")])


def _exercised_reasons() -> set[str]:
    """Every reason this file actually asserts, read from its own syntax tree.

    Three shapes carry a reason here: the second argument of an `_only_reason`
    call (a ledger row), the anchored `match=` of a `pytest.raises` (a raised
    refusal), and the reason column of a `parametrize` table (a raised or
    recorded refusal, one per case). A regex over the source could not tell the
    second argument of a call whose first argument itself contains commas, so
    this reads the tree rather than the text.
    """
    tree = ast.parse(Path(__file__).read_text(encoding="utf-8"))
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            func = node.func
            if isinstance(func, ast.Name) and func.id == "_only_reason":
                if len(node.args) > 1 and isinstance(node.args[1], ast.Constant):
                    found.add(node.args[1].value)
            if isinstance(func, ast.Attribute) and func.attr in ("raises", "parametrize"):
                for keyword in node.keywords:
                    if keyword.arg == "match" and isinstance(keyword.value, ast.Constant):
                        found.add(keyword.value.value.lstrip("^").rstrip(":"))
                if func.attr == "parametrize":
                    for argument in node.args:
                        found |= {
                            item.value
                            for item in ast.walk(argument)
                            if isinstance(item, ast.Constant) and isinstance(item.value, str)
                        }
    return found


def test_every_declared_local_admission_reason_is_exercised_here():
    """`exercised` is read from this file's own assertions, never hand-typed.

    A hand-typed set can drift from the tests it claims to describe: a phantom
    reason, or a deleted test, both leave it green. `test_rows.py` set this
    convention and says why -- reasons had been "declared but never shown to
    fire".
    """
    missing = LOCAL_ADMISSION_REFUSAL_REASONS - _exercised_reasons()
    assert missing == set(), f"declared but never shown to fire: {sorted(missing)}"
