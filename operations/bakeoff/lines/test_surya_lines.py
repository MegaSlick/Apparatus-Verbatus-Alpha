"""The Surya line source: matching documents to pages, reading order, crop geometry."""

import json
import subprocess
from pathlib import Path

import pytest
from PIL import Image

from operations.bakeoff.lines import crops, surya_lines
from operations.bakeoff.lines.conftest import LINE_BOXES, surya_documents


def test_reading_order_follows_blocks_then_rows():
    document = json.loads(surya_documents(1)[1])
    ordered = surya_lines.ordered_lines(document)
    # The lower block is position 0, so its row (left, then right) comes first.
    assert [line["bbox"] for line in ordered] == [list(LINE_BOXES[i]) for i in (1, 2, 0)]
    assert [line["block"] for line in ordered] == [0, 0, 1]
    assert [line["detected"] for line in ordered] == [2, 0, 1]


def test_lines_outside_every_block_come_last():
    document = json.loads(surya_documents(1)[1])
    document["layout"]["bboxes"] = document["layout"]["bboxes"][:1]  # drop the top block
    ordered = surya_lines.ordered_lines(document)
    assert [line["block"] for line in ordered] == [0, 0, None]


def test_prepare_writes_crops_and_index_once(pages, surya_dir, tmp_path):
    out = tmp_path / "cache"
    page_list = sorted(pages.iterdir())
    indexes = surya_lines.prepare(page_list, surya_dir, out)
    index = indexes["p000"]
    assert index["schema"] == crops.INDEX_SCHEMA and index["source"] == "surya"
    assert [row["file"] for row in index["lines"]] == ["0001.png", "0002.png", "0003.png"]
    crop = Image.open(out / "_lines" / "surya" / "p000" / "0001.png")
    x0, y0, x1, y1 = LINE_BOXES[1]
    assert crop.size == (x1 - x0, y1 - y0) and crop.mode == "L"
    assert crop.getpixel((0, 0)) == 255 and crop.getpixel((crop.width // 2, crop.height // 2)) == 0
    stamp = (out / "_lines" / "surya" / "p000.json").stat().st_mtime_ns
    surya_lines.prepare(page_list, surya_dir, out)
    assert (out / "_lines" / "surya" / "p000.json").stat().st_mtime_ns == stamp


def test_documents_match_by_stem_without_an_index(pages, surya_dir):
    (surya_dir / "pages.json").unlink()
    for ordinal, stem in ((1, "p000"), (2, "p001")):
        (surya_dir / f"page-{ordinal}.json").rename(surya_dir / f"{stem}.json")
    found = surya_lines.match_documents(surya_dir, sorted(pages.iterdir()))
    assert found["p001"].name == "p001.json"


def test_a_page_without_a_document_is_refused(pages, surya_dir):
    (surya_dir / "page-2.json").unlink()
    with pytest.raises(surya_lines.LinesRefusal, match="p001"):
        surya_lines.match_documents(surya_dir, sorted(pages.iterdir()))


def test_polygon_mask_paints_outside_white():
    page = Image.new("L", (100, 50), 0)
    triangle = [[0, 0], [100, 0], [0, 50]]
    crop = crops.crop_line(page, triangle, crops.polygon_bbox(triangle, 100, 50))
    assert crop.getpixel((2, 2)) == 0 and crop.getpixel((97, 47)) == 255


def test_command_writes_the_index_and_names_the_runner(pages, tmp_path, capsys):
    lines_dir = tmp_path / "surya-out"
    argv = ["command", "--pages", str(pages), "--lines-dir", str(lines_dir)]
    assert surya_lines.main([*argv, "--weights", "/bundle"]) == 0
    printed = capsys.readouterr().out
    assert "operations/serving/surya/.venv/bin/python" in printed and "runner.py" in printed
    assert printed.rstrip().endswith("p001.tif") and "--threads 8" in printed
    assert json.loads((lines_dir / "pages.json").read_text())["pages"] == ["p000", "p001"]


def test_a_document_written_for_another_page_is_refused(pages, surya_dir, tmp_path):
    """A stale pages.json: the documents swapped, as if the runner had the pages in
    another order. The recorded ordinal no longer matches the file name."""
    first, second = surya_dir / "page-1.json", surya_dir / "page-2.json"
    a, b = first.read_bytes(), second.read_bytes()
    first.write_bytes(b), second.write_bytes(a)
    with pytest.raises(surya_lines.LinesRefusal, match="input ordinal"):
        surya_lines.prepare(sorted(pages.iterdir()), surya_dir, tmp_path / "cache")


def test_a_document_of_another_size_is_refused(pages, surya_dir, tmp_path):
    document = json.loads((surya_dir / "page-1.json").read_text())
    document["image_size"] = [601, 400]
    (surya_dir / "page-1.json").write_text(json.dumps(document))
    with pytest.raises(surya_lines.LinesRefusal, match="stale"):
        surya_lines.prepare(sorted(pages.iterdir()), surya_dir, tmp_path / "cache")


def _pin(folder, tmp_path):
    """A one-file bundle and the manifest that pins it."""
    folder.mkdir(parents=True)
    (folder / "weights.bin").write_bytes(b"weights")
    manifest = tmp_path / "pin.json"
    digest = surya_lines.hashlib.sha256(b"weights").hexdigest()
    manifest.write_text(json.dumps([{"path": "weights.bin", "sha256": digest, "size": 7}]))
    return manifest


def test_the_bundle_is_the_stores_copy_else_fetched_and_always_checked(tmp_path, monkeypatch):
    store, own = tmp_path / "store", tmp_path / "own"
    monkeypatch.setattr(
        surya_lines, "BUNDLE_MANIFEST", _pin(store / "local" / "surya2-detection", tmp_path)
    )
    assert surya_lines.ensure_bundle(store, own) == store / "local" / "surya2-detection"

    (store / "local" / "surya2-detection" / "weights.bin").write_bytes(b"changed")
    calls = []

    def prefetch(argv, check):
        calls.append(argv)
        out = Path(argv[argv.index("--out") + 1])
        out.mkdir()
        (out / "weights.bin").write_bytes(b"weights")
        return subprocess.CompletedProcess(argv, 0)

    assert surya_lines.ensure_bundle(store, own, runner=prefetch) == own
    assert calls[0][1].endswith("prefetch.py")
    (own / "weights.bin").write_bytes(b"other")
    with pytest.raises(surya_lines.LinesRefusal, match="move it aside"):
        surya_lines.ensure_bundle(store, own, runner=prefetch)


def test_run_detects_only_the_pages_without_a_document_then_cuts(
    pages, surya_dir, tmp_path, monkeypatch
):
    env = tmp_path / "surya-env"
    (env / ".venv" / "bin").mkdir(parents=True)
    (env / ".venv" / "bin" / "python").write_text("")
    monkeypatch.setattr(surya_lines, "SURYA_ENV", env)
    second = (surya_dir / "page-2.json").read_bytes()
    (surya_dir / "page-2.json").write_text('{"cut off mid-wri')  # a run stopped mid-write
    calls = []

    def runner(argv, check):
        calls.append(argv)
        (surya_dir / "page-2.json").write_bytes(second)
        return subprocess.CompletedProcess(argv, 0)

    page_list = sorted(pages.iterdir())
    surya_lines.detect(page_list, surya_dir, tmp_path / "bundle", 8, runner)
    assert calls[0][-3:] == ["--first-ordinal", "2", str(page_list[1])]
    surya_lines.detect(page_list, surya_dir, tmp_path / "bundle", 8, runner)
    assert len(calls) == 1  # every page has its document
    assert surya_lines.prepare(page_list, surya_dir, tmp_path / "cache")["p001"]["lines"]

    (surya_dir / "pages.json").write_text(json.dumps({"schema": "x", "pages": ["p001", "p000"]}))
    with pytest.raises(surya_lines.LinesRefusal, match="another order"):
        surya_lines.detect(page_list, surya_dir, tmp_path / "bundle", 8, runner)
