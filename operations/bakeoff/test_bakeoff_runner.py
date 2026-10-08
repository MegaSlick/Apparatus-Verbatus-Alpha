"""The runner end to end against a fake OpenAI server (no GPU, synthetic pages only)."""

import io
import json
import socket
import sys
from pathlib import Path

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
    def __init__(self, *_args):
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
