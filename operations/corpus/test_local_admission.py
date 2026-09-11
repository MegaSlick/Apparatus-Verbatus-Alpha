"""Pure tests for `operations/corpus/local_admission.py`: synthetic sets, no network.

The corpus's forty 180-degree records are the case (independent audit of
2026-09-10, F4). A synthetic two-page set -- one page stated upright, one stated
through a 180-degree view -- is written under `tmp_path` with real JPEG bytes,
so the geometry claim is tested against pixels, not against the formula alone.
"""

from __future__ import annotations

import io
import json
from pathlib import Path

import pytest
from PIL import Image

from common.contracts.canonical import digest_bytes
from operations.corpus import CorpusRefusal
from operations.corpus.local_admission import (
    LOCAL_ADMISSION_REFUSAL_REASONS,
    SCHEMA,
    admit_local_set,
    transform_region,
)
from operations.corpus.reference import validate_reference_page

HOST = "https://europe.iiif.teklia.com/iiif/2/"
UPRIGHT_ID = "dai-cretdhi%2FTours%2FSaint_X%2F6NUM_001%2FFRAD037_001_002.JPG"
ROTATED_ID = "dai-cretdhi%2FIle_de_re%2Fimg%2FSMa-BMS_1637%2Ffr_ad017_0661.jpg"
WIDTH, HEIGHT = 400, 300


def _page_bytes(seed: int) -> bytes:
    """A page whose every 20x20 cell carries a distinct colour, so a crop is unambiguous."""
    image = Image.new("RGB", (WIDTH, HEIGHT))
    pixels = image.load()
    for y in range(HEIGHT):
        for x in range(WIDTH):
            pixels[x, y] = ((x // 20 * 13 + seed) % 256, (y // 20 * 29 + seed) % 256, (x + y) % 256)
    buffer = io.BytesIO()
    image.save(buffer, format="JPEG", quality=95)
    return buffer.getvalue()


def _url(identifier: str, box: list[int], rotation: str) -> str:
    x, y, w, h = box
    return f"{HOST}{identifier}/{x},{y},{w},{h}/full/{rotation}/default.jpg"


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
    root: Path, records: list[dict], pages: dict[str, bytes], *, receipt_ok=True
) -> Path:
    (root / "pages").mkdir(parents=True)
    for page_id, body in pages.items():
        (root / "pages" / f"{page_id}.jpg").write_bytes(body)
    manifest_rows = []
    for page_id in pages:
        manifest_rows.append(
            {
                "page_id": page_id,
                "image": f"pages/{page_id}.jpg",
                "width": WIDTH,
                "height": HEIGHT,
                "iiif_rotation": "0",
                "records": [row for row in records if row["page_id"] == page_id],
            }
        )
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
    return _write_set(root, records, {"page-up": _page_bytes(1), "page-rot": _page_bytes(2)})


def test_transform_region_is_the_identity_at_zero_and_the_far_corner_at_180():
    region = {"x": 100, "y": 40, "w": 160, "h": 100}
    assert transform_region(region, rotation="0", width=WIDTH, height=HEIGHT) == region
    assert transform_region(region, rotation="180", width=WIDTH, height=HEIGHT) == {
        "x": WIDTH - 100 - 160,
        "y": HEIGHT - 40 - 100,
        "w": 160,
        "h": 100,
    }
    with pytest.raises(CorpusRefusal, match="^unsupported-rotation-parameter"):
        transform_region(region, rotation="90", width=WIDTH, height=HEIGHT)


def test_a_180_degree_box_carried_to_the_stored_page_crops_the_same_ink(tmp_path):
    """The formula against pixels: the box in the rotated view, turned back, is the stored crop."""
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
        "pages_admitted": 2,
        "pages_refused": 0,
        "admitted_by_rotation": {"0": 2, "180": 1},
    }
    assert ledger["receipt"]["digests"]["gold.jsonl"] == digest_bytes(
        (root / "gold.jsonl").read_bytes()
    )
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

    written = json.loads((tmp_path / "out" / "ledger.json").read_bytes())
    assert written["summary"] == ledger["summary"]
    assert len((tmp_path / "out" / "reference-pages.jsonl").read_text().splitlines()) == 2
    with pytest.raises(CorpusRefusal, match="never overwritten"):
        admit_local_set(root, split="val", output_dir=tmp_path / "out")


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
        ({"split": "train"}, "unknown-split"),
    ],
)
def test_a_record_that_cannot_be_carried_honestly_is_refused_by_name(tmp_path, override, reason):
    root = _two_page_set(tmp_path / "set", **{"r-rot": override})
    ledger = admit_local_set(root, split="val")
    assert ledger["summary"]["records"] == 3
    assert ledger["summary"]["admitted"] == 2
    assert ledger["summary"]["refused_by_reason"] == {reason: 1}
    assert reason in LOCAL_ADMISSION_REFUSAL_REASONS
    refused = next(row for row in ledger["rows"] if row["record_id"] == "r-rot")
    assert refused["decision"] == "refused" and refused["reason"] == reason
    assert refused["record_url"] and refused["source_bbox"] is not None
    # The upright page's truth is unaffected; the rotated page has no truth to build.
    assert [len(page["acts"]) for page in ledger["reference_pages"]] == [2]


def test_a_box_whose_carry_leaves_the_page_is_refused_not_clipped(tmp_path):
    x, y, w, h = 300, 40, 160, 100  # x + w > WIDTH in the rotated view: carried x is negative
    override = {
        "record_url": _url(ROTATED_ID, [x, y, w, h], "180"),
        "source_bbox": [x, y, w, h],
        "bbox": [WIDTH - x - w, HEIGHT - y - h, w, h],
    }
    ledger = admit_local_set(_two_page_set(tmp_path / "set", **{"r-rot": override}), split="val")
    assert ledger["summary"]["refused_by_reason"] == {"region-outside-page": 1}


def test_a_page_whose_pixels_disagree_with_the_manifest_refuses_every_record_on_it(tmp_path):
    root = _two_page_set(tmp_path / "set")
    wrong = Image.new("RGB", (WIDTH - 1, HEIGHT))
    buffer = io.BytesIO()
    wrong.save(buffer, format="JPEG")
    (root / "pages" / "page-up.jpg").write_bytes(buffer.getvalue())
    ledger = admit_local_set(root, split="val")
    assert ledger["summary"]["refused_by_reason"] == {"dimension-mismatch": 2}
    assert ledger["summary"]["pages_refused"] == 1 and ledger["summary"]["pages_admitted"] == 1
    assert {row["record_id"] for row in ledger["rows"] if row["decision"] == "refused"} == {
        "r-up",
        "r-up-2",
    }


def test_a_missing_page_file_and_a_duplicate_record_are_refused_by_name(tmp_path):
    root = _two_page_set(tmp_path / "set")
    (root / "pages" / "page-rot.jpg").unlink()
    ledger = admit_local_set(root, split="val")
    assert ledger["summary"]["refused_by_reason"] == {"missing-page-file": 1}

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
    assert ledger["summary"]["refused_by_reason"] == {"duplicate-record-id": 1}


def test_a_set_whose_files_are_not_the_receipts_bytes_is_refused_before_any_row(tmp_path):
    root = _write_set(
        tmp_path / "set",
        [_record("r-up", "page-up", UPRIGHT_ID, UPRIGHT_BOX, "0")],
        {"page-up": _page_bytes(1)},
        receipt_ok=False,
    )
    with pytest.raises(CorpusRefusal, match="^receipt-hash-mismatch"):
        admit_local_set(root, split="val")
    with pytest.raises(CorpusRefusal, match="^unknown-split"):
        admit_local_set(root, split="calibration")


@pytest.mark.parametrize("missing", ["gold.jsonl", "page_manifest.jsonl", "fetch_receipt.json"])
def test_a_set_missing_one_of_its_files_is_refused_by_the_files_name(tmp_path, missing):
    root = _two_page_set(tmp_path / "set")
    (root / missing).unlink()
    with pytest.raises(CorpusRefusal, match="^missing-set-file") as refused:
        admit_local_set(root, split="val")
    assert missing in str(refused.value)
    assert "missing-set-file" in LOCAL_ADMISSION_REFUSAL_REASONS
