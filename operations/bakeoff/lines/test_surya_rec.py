"""The Surya recogniser arm against a stand-in worker (no Surya, no model server)."""

import json
from functools import partial
from pathlib import Path

from operations.bakeoff.lines import surya_rec
from operations.bakeoff.lines.conftest import RECORD_KEYS, FakeRunner, fake_venv, weights_dir

# Block HTML in the shape Surya's BLOCK_PROMPT answers take, hand-written.
BLOCKS = [
    {"reading_order": 1, "label": "Text", "html": "<p>deuxieme<br/>bloc</p>", "skipped": False,
     "error": False, "polygon": [[0, 0], [1, 0], [1, 1], [0, 1]]},
    {"reading_order": 0, "label": "Table", "skipped": False, "error": False,
     "html": "<table><tr><td>a</td><td>b</td></tr><tr><td>c</td><td>d</td></tr></table>",
     "polygon": [[0, 0], [1, 0], [1, 1], [0, 1]]},
    {"reading_order": 2, "label": "Picture", "html": "", "skipped": True, "error": False,
     "polygon": [[0, 0], [1, 0], [1, 1], [0, 1]]},
]  # fmt: skip


def _worker(argv):
    job = json.loads(Path(argv[argv.index("--job") + 1]).read_text())
    pages = [
        {"stem": p["stem"], "blocks": BLOCKS, "error": None, "seconds": 1.5} for p in job["pages"]
    ]
    result = {"facts": {"backend": "llamacpp"}, "pages": pages}
    Path(argv[argv.index("--result") + 1]).write_text(json.dumps(result))
    return 0, ""


def test_html_lines():
    assert surya_rec.html_lines("<p>un&amp;deux<br>trois</p><p>  quatre </p>") == [
        "un&deux",
        "trois",
        "quatre",
    ]


def test_blocks_mode_sends_cached_layout_and_orders_blocks(pages, surya_dir, tmp_path):
    out, store = tmp_path / "cache", tmp_path / "store"
    weights_dir(store, "surya-ocr-2-gguf", list(surya_rec.GGUF_FILES))
    runner = FakeRunner(_worker)
    argv = ["run", "--mode", "blocks", "--lines-dir", str(surya_dir), "--pages", str(pages),
            "--out", str(out), "--store-root", str(store), "--venv-dir", str(fake_venv(tmp_path))]  # fmt: skip
    assert surya_rec.main(argv, recogniser=partial(surya_rec.SuryaRecogniser, runner=runner)) == 0
    assert len(runner.calls) == 1 and runner.calls[0][1:3] == ["-I", surya_rec.__file__]
    job = json.loads(Path(runner.calls[0][runner.calls[0].index("--job") + 1]).read_text())
    assert job["mode"] == "blocks" and job["pages"][0]["layout"]["bboxes"][0]["position"] == 0
    record = json.loads((out / "surya-rec-surya-blocks" / "p000.json").read_text())
    assert set(record) == RECORD_KEYS and record["arm"] == "surya-rec-surya-blocks"
    assert record["text"] == "a b\nc d\ndeuxieme\nbloc"
    assert [u["request"]["unit"] for u in record["units"]] == [
        "block-000",
        "block-001",
        "block-002",
    ]


def test_without_gguf_or_server_it_refuses(pages, surya_dir, tmp_path):
    store = tmp_path / "store"
    weights_dir(store, "surya-ocr-2-gguf", [])
    argv = ["run", "--lines-dir", str(surya_dir), "--pages", str(pages), "--out", str(tmp_path / "c"),
            "--store-root", str(store), "--venv-dir", str(fake_venv(tmp_path))]  # fmt: skip
    assert surya_rec.main(argv) == 2


def test_blocks_mode_with_a_missing_document_refuses_cleanly(pages, surya_dir, tmp_path):
    (surya_dir / "page-2.json").unlink()
    store = tmp_path / "store"
    weights_dir(store, "surya-ocr-2-gguf", list(surya_rec.GGUF_FILES))
    runner = FakeRunner(_worker)
    argv = ["run", "--mode", "blocks", "--lines-dir", str(surya_dir), "--pages", str(pages),
            "--out", str(tmp_path / "c"), "--store-root", str(store),
            "--venv-dir", str(fake_venv(tmp_path))]  # fmt: skip
    assert surya_rec.main(argv, recogniser=partial(surya_rec.SuryaRecogniser, runner=runner)) == 2
    assert runner.calls == []


def test_page_mode_reads_no_documents(pages, tmp_path):
    store = tmp_path / "store"
    weights_dir(store, "surya-ocr-2-gguf", list(surya_rec.GGUF_FILES))
    runner = FakeRunner(_worker)
    out = tmp_path / "c"
    argv = ["run", "--pages", str(pages), "--out", str(out), "--store-root", str(store),
            "--venv-dir", str(fake_venv(tmp_path))]  # fmt: skip
    assert surya_rec.main(argv, recogniser=partial(surya_rec.SuryaRecogniser, runner=runner)) == 0
    assert json.loads((out / "surya-rec-surya" / "p000.json").read_text())["error"] is None


def test_serve_starts_vllm_for_the_run_and_stops_it(pages, tmp_path):
    """--serve: the arm's own server (the fake vLLM) is up while the worker runs, the job
    points Surya at it, and it is gone afterwards."""
    import sys
    import urllib.request

    from operations.bakeoff.test_bakeoff_runner import FAKE, _free_port

    store, out, port = tmp_path / "store", tmp_path / "c", _free_port()
    weights_dir(store, "surya-ocr-2", ["config.json", "model.safetensors"])
    seen = []

    def worker(argv):
        job = json.loads(Path(argv[argv.index("--job") + 1]).read_text())
        with urllib.request.urlopen(job["server_url"] + "/models", timeout=5) as response:
            seen.append((job["server_url"], json.loads(response.read())["data"][0]["id"]))
        return _worker(argv)

    argv = ["run", "--serve", "--port", str(port), "--vllm-cmd", sys.executable, str(FAKE),
            "--startup-timeout", "60", "--pages", str(pages), "--out", str(out),
            "--store-root", str(store), "--venv-dir", str(fake_venv(tmp_path))]  # fmt: skip
    runner = FakeRunner(worker)
    assert surya_rec.main(argv, recogniser=partial(surya_rec.SuryaRecogniser, runner=runner)) == 0
    assert seen == [(f"http://127.0.0.1:{port}/v1", surya_rec.SERVED_NAME)]
    record = json.loads((out / "surya-rec-surya" / "p000.json").read_text())
    assert record["revision"] == surya_rec.HF_REVISION and record["error"] is None
    events = [json.loads(line)["event"] for line in (out / "events.jsonl").read_text().splitlines()]
    assert events.index("server-ready") < events.index("server-stopped")
    try:
        urllib.request.urlopen(f"http://127.0.0.1:{port}/v1/models", timeout=2)
        raise AssertionError("the server is still up")
    except OSError:
        pass


def test_serve_argv_names_what_surya_asks_for(tmp_path):
    argv = surya_rec.serve_argv(tmp_path, 8191, 0.9)
    assert argv[:2] == ["serve", str(tmp_path)]
    assert argv[argv.index("--served-model-name") + 1] == "datalab-to/surya-ocr-2"
    assert "--no-trust-remote-code" in argv
