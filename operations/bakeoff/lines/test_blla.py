"""The blla line source and the ALTO plain-line rule, on hand-written ALTO."""

from pathlib import Path

from operations.bakeoff.lines import blla
from operations.bakeoff.lines.conftest import LINE_BOXES, FakeRunner, alto, fake_venv


def test_alto_lines_follow_the_reading_order_group():
    raw = alto("p.tif", [(LINE_BOXES[0], ["un", "deux"]), (LINE_BOXES[1], ["trois"])], order=[1, 0])
    rows = blla.read_alto(raw.encode())
    assert [row["text"] for row in rows] == ["trois", "un deux"]
    assert rows[0]["baseline"] == [[40.0, 192.0], [300.0, 192.0]]
    assert blla.alto_text(raw.encode()) == "trois\nun deux"


def test_alto_without_reading_order_keeps_document_order_and_drops_empty_lines():
    raw = alto("p.tif", [(LINE_BOXES[0], ["a"]), (LINE_BOXES[1], []), (LINE_BOXES[2], ["b  c"])])
    assert blla.alto_text(raw.encode()) == "a\nb c"


def _segmenter(tmp_path: Path):
    def respond(argv):
        xml = Path(argv[argv.index("-i") + 2])
        boxes = [(LINE_BOXES[2], []), (LINE_BOXES[0], [])]
        xml.write_text(alto(argv[argv.index("-i") + 1], boxes, order=[1, 0]))
        return 0, ""

    return FakeRunner(respond)


def test_prepare_segments_once_and_cuts_crops_in_alto_order(pages, tmp_path):
    out, venv = tmp_path / "cache", fake_venv(tmp_path, "kraken")
    runner = _segmenter(tmp_path)
    page_list = sorted(pages.iterdir())
    indexes = blla.prepare(page_list, out, venv, "cpu", runner)
    assert runner.calls[0][-2:] == ["segment", "-bl"] and "-a" in runner.calls[0]
    assert [row["bbox"] for row in indexes["p000"]["lines"]] == [
        list(LINE_BOXES[0]),
        list(LINE_BOXES[2]),
    ]
    assert (out / "_lines" / "blla" / "p000" / "0002.png").is_file()
    assert indexes["p000"]["segmenter"]["sha256"] == blla.BLLA_SHA256
    blla.prepare(page_list, out, venv, "cpu", runner)
    assert len(runner.calls) == 2  # one per page, never again


def test_device_cuda_reaches_kraken(tmp_path):
    argv = blla.segment_argv(Path("/v/bin/kraken"), Path("p.tif"), Path("p.xml"), "cuda")
    assert argv[1:3] == ["-d", "cuda:0"]
