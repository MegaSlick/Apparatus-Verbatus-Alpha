"""The runner end to end against a fake OpenAI server (no GPU, synthetic pages only)."""

import io
import json
import socket
import sys
from pathlib import Path

import pytest
from PIL import Image, ImageDraw

from operations.bakeoff import arms as A
from operations.bakeoff import witness_run as W

FAKE = Path(__file__).with_name("fake_vllm_server.py")


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _pages(folder: Path, count: int) -> None:
    folder.mkdir()
    for index in range(count):
        image = Image.new("L", (400, 300), 255)
        ImageDraw.Draw(image).text((20, 20 + 30 * index), "synthetic", fill=0)
        image.save(folder / f"p{index:03d}.tif", compression="tiff_lzw")


def _weights(tmp_path: Path, name: str) -> str:
    folder = tmp_path / "weights" / name
    folder.mkdir(parents=True)
    (folder / "config.json").write_text("{}")
    return f"{name}={folder}"


class FakeDetector:
    def __init__(self, *_args, **_kwargs):
        pass

    def records(self, page_png):
        width, height = A._size(page_png)
        if width == 0:
            return []
        boxes = [{"x": 200, "y": 0, "w": 200, "h": 150}, {"x": 0, "y": 0, "w": 199, "h": 300}]
        return A.order_records(boxes, width)


def test_run_all_caches_every_page_and_resumes(tmp_path, monkeypatch):
    monkeypatch.setattr(A, "RecordDetector", FakeDetector)
    pages, out = tmp_path / "pages", tmp_path / "cache"
    _pages(pages, 3)
    argv = [
        "run-all",
        "--models",
        "chandra,dai,churro",
        "--pages",
        str(pages),
        "--out",
        str(out),
        "--port",
        str(_free_port()),
        "--max-num-seqs",
        "2",
        "--startup-timeout",
        "60",
        "--vllm-cmd",
        sys.executable,
        str(FAKE),
        "--detector-weights",
        str(tmp_path),
        "--weights",
        *(_weights(tmp_path, n) for n in ("chandra", "dai", "churro")),
    ]
    assert W.main(argv) == 0
    for model in ("chandra", "dai", "churro"):
        records = [json.loads((out / model / f"p{i:03d}.json").read_text()) for i in range(3)]
        assert all(r["error"] is None and not r["empty"] and not r["loop"] for r in records)
        assert json.loads((out / model / "run.json").read_text())["concurrency"] == 4
    dai = json.loads((out / "dai" / "p000.json").read_text())
    assert [u["request"]["bounds"]["x"] for u in dai["units"]] == [0, 200]  # left column first
    assert dai["text"] == "Le dix mai\nLe dix mai"
    assert dai["units"][0]["request"]["sampling"]["stop_token_ids"] == [151643]
    chandra = json.loads((out / "chandra" / "p001.json").read_text())
    request = chandra["units"][0]["request"]
    assert request["sampling"]["chat_template_kwargs"] == {"enable_thinking": False}
    assert request["max_tokens"] == 12_384 and request["prompt_roles"] == ["user"]
    assert "--max-num-seqs" in chandra["server"]["argv"]
    assert "Le dix mai" in chandra["units"][0]["raw_response"]
    events = [json.loads(x)["event"] for x in (out / "events.jsonl").read_text().splitlines()]
    assert events.count("server-ready") == 3 and events.count("server-stopped") == 3

    # A second run finds everything cached and starts no server.
    assert W.main(argv) == 0
    events = [json.loads(x)["event"] for x in (out / "events.jsonl").read_text().splitlines()]
    assert events.count("server-ready") == 3 and events.count("nothing-to-do") == 3


def test_errors_are_recorded_and_retried(tmp_path):
    pages, out = tmp_path / "pages", tmp_path / "cache"
    _pages(pages, 1)
    argv = [
        "run",
        "--model",
        "churro",
        "--pages",
        str(pages),
        "--out",
        str(out),
        "--server-url",
        f"http://127.0.0.1:{_free_port()}",
        "--request-timeout",
        "5",
    ]
    assert W.main(argv) == 0
    record = json.loads((out / "churro" / "p000.json").read_text())
    assert record["error"] and record["units"][0]["text"] is None
    assert not W.cached_ok(out / "churro" / "p000.json")


def test_record_geometry_and_order():
    corners = [[10.7, 5.2], [50.1, 5.9], [50.4, 40.0], [10.2, 39.3]]
    assert A.obb_bounds(corners, 100, 100) == {"x": 10, "y": 5, "w": 41, "h": 36}
    assert A.obb_bounds([[1, 1]] * 4, 100, 100) is None
    one_column = [{"x": 600, "y": 50, "w": 300, "h": 10}, {"x": 10, "y": 10, "w": 900, "h": 10}]
    assert [b["y"] for b in A.order_records(one_column, 1000)] == [10, 50]


def test_page_loading_and_request_bounds(tmp_path):
    Image.new("1", (64, 48), 1).save(tmp_path / "bilevel.tif", compression="group4")
    png = A.load_page_png(tmp_path / "bilevel.tif")
    assert Image.open(io.BytesIO(png)).mode == "L" and A._size(png) == (64, 48)
    row = A.serving_row("attestator_1", "generic-48gb")
    unit = {"unit": "page", "bounds": {}, "png": _png(4000, 6000)}
    _, record = A.build_request(
        A.ARMS["chandra"], unit, row=row, served_name="m", max_model_len=18_000
    )
    assert record["max_tokens"] is None and record["max_tokens_basis"].startswith("omitted")


def _png(width: int, height: int) -> bytes:
    buffer = io.BytesIO()
    Image.new("L", (width, height), 255).save(buffer, format="PNG")
    return buffer.getvalue()


class EmptyDetector(FakeDetector):
    settings = {"imgsz": 1024, "conf": 0.25}

    def records(self, page_png):
        return []


def _dai_argv(pages: Path, out: Path, tmp_path: Path, *extra: str) -> list[str]:
    return [
        "run", "--model", "dai", "--pages", str(pages), "--out", str(out),
        "--port", str(_free_port()), "--max-num-seqs", "1", "--startup-timeout", "60",
        "--vllm-cmd", sys.executable, str(FAKE),
        "--detector-weights", str(tmp_path), "--weights", str(tmp_path / "weights" / "dai"),
        *extra,
    ]  # fmt: skip


def test_dai_whole_page_fallback_and_record_cache_settings(tmp_path, monkeypatch):
    monkeypatch.setattr(A, "RecordDetector", EmptyDetector)
    pages, out = tmp_path / "pages", tmp_path / "cache"
    _pages(pages, 1)
    _weights(tmp_path, "dai")

    # No record and no fallback: a valid, empty page, and the cache says what was asked.
    assert W.main(_dai_argv(pages, out, tmp_path)) == 0
    page = json.loads((out / "dai" / "p000.json").read_text())
    assert page["empty"] and page["finish_reason"] == "no-records" and not page["units"]
    assert page["whole_page_fallback"] is False
    records = json.loads((out / "dai" / "_records" / "p000.json").read_text())
    assert records["detected"] == 0 and records["records"] == []
    assert records["settings"] == {"conf": None, "imgsz": None, "fallback": "none"}
    assert records["detector_settings"] == EmptyDetector.settings

    # Other settings: the record cache is rebuilt, the page is shown whole, read as one unit.
    out2 = tmp_path / "cache2"
    extra = (
        "--record-fallback",
        "whole-page",
        "--detector-conf",
        "0.1",
        "--detector-imgsz",
        "1536",
    )
    assert W.main(_dai_argv(pages, out2, tmp_path, "--label", "dai", *extra)) == 0
    page = json.loads((out2 / "dai" / "p000.json").read_text())
    assert not page["empty"] and page["record_units"] == ["whole-page"]
    assert page["whole_page_fallback"] is True
    assert page["units"][0]["request"]["bounds"] == {"x": 0, "y": 0, "w": 400, "h": 300}
    records = json.loads((out2 / "dai" / "_records" / "p000.json").read_text())
    assert records["settings"] == {"conf": 0.1, "imgsz": 1536, "fallback": "whole-page"}
    assert records["records"][0]["fallback"] == "whole-page" and records["detected"] == 0


def test_whole_page_record_and_scored_boxes_keep_order():
    whole = A.whole_page_record(800, 600)
    assert whole["w"] == 800 and whole["fallback"] == "whole-page" and whole["score"] is None
    scored = [
        {"x": 500, "y": 10, "w": 200, "h": 50, "score": 0.9},
        {"x": 10, "y": 100, "w": 300, "h": 50, "score": 0.4},
    ]
    assert [b["score"] for b in A.order_records(scored, 800)] == [0.4, 0.9]


def test_whole_page_fallback_unit_is_width_capped_like_a_crop():
    # A page wider than DAI's 1,500 px ceiling: the fallback unit keeps the page's
    # bounds but the image DAI is shown is scaled down to 1,500 px, aspect kept.
    page = _png(3000, 1200)
    units = A.page_units(A.ARMS["dai"], page, [A.whole_page_record(3000, 1200)])
    assert [u["unit"] for u in units] == ["whole-page"]
    assert units[0]["bounds"] == {"x": 0, "y": 0, "w": 3000, "h": 1200}
    assert A._size(units[0]["png"]) == (1500, 600)


def test_record_dicts_from_an_old_cache_still_order_and_crop():
    # Records written before scores existed carry only x, y, w and h.
    old = [{"x": 210, "y": 0, "w": 190, "h": 150}, {"x": 0, "y": 0, "w": 199, "h": 300}]
    ordered = A.order_records(old, 400)
    units = A.page_units(A.ARMS["dai"], _png(400, 300), ordered)
    assert [u["unit"] for u in units] == ["record-0", "record-1"]
    assert units[0]["bounds"] == {"x": 0, "y": 0, "w": 199, "h": 300}


def test_a_label_is_refused_under_another_checkpoint_or_recipe(tmp_path):
    pages, out = tmp_path / "pages", tmp_path / "cache"
    _pages(pages, 1)
    argv = [
        "run", "--model", "churro", "--pages", str(pages), "--out", str(out),
        "--port", str(_free_port()), "--startup-timeout", "60",
        "--vllm-cmd", sys.executable, str(FAKE), "--weights", _weights(tmp_path, "churro"),
    ]  # fmt: skip
    assert W.main(argv) == 0
    cached = out / "churro" / "p000.json"
    assert json.loads(cached.read_text())["recipe"] is None
    assert W.main(argv) == 0  # the same setup resumes
    with pytest.raises(SystemExit, match="cached under another setup .*revision"):
        W.main([*argv, "--revision", "b" * 40])
    # A page cached before recipes were recorded is judged by checkpoint and revision only.
    assert W.cache_conflict(cached, {"repo": "x/y", "revision": "z", "recipe": "r"}) == [
        "recipe",
        "repo",
        "revision",
    ]
    record = json.loads(cached.read_text())
    del record["recipe"]
    cached.write_text(json.dumps(record))
    assert (
        W.cache_conflict(
            cached, {"repo": record["repo"], "revision": record["revision"], "recipe": "fp8"}
        )
        == []
    )


def test_a_stale_handoff_never_signals_an_unrelated_process(tmp_path):
    import subprocess

    argv = [sys.executable, "-c", "import time; time.sleep(60)"]
    process = subprocess.Popen(argv, start_new_session=True)
    try:
        stale = {"url": "http://127.0.0.1:1", "argv": ["vllm", "serve", "x"], "pid": process.pid}
        W.Server.adopted(stale).stop()
        assert process.poll() is None  # the pid is alive but is not the recorded server
        W.Server.adopted({**stale, "argv": argv}).stop()
        process.wait(timeout=70)
        assert process.returncode is not None
    finally:
        if process.poll() is None:
            process.kill()
