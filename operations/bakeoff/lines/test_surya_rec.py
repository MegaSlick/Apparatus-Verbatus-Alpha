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
    record = json.loads((out / "surya-rec-surya" / "p000.json").read_text())
    assert set(record) == RECORD_KEYS and record["arm"] == "surya-rec-surya"
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
