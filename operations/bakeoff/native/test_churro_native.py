"""The Churro native arm against the fake vLLM server, and its port of the vendor extractor.

The extractor needs `lxml`, which only the arm's own environment has; its test is
skipped where `lxml` is missing, and the end-to-end run then stands in a plain reader.
"""

import dataclasses
import io
import json
import sys
from pathlib import Path

import pytest
from PIL import Image

from operations.bakeoff import witness_run as W
from operations.bakeoff.native import churro_native as C
from operations.bakeoff.native.test_chandra_native import RECORD_KEYS, _weights
from operations.bakeoff.test_bakeoff_runner import _free_port, _pages

FAKE = Path(__file__).parents[1] / "fake_vllm_server.py"


def _arm(monkeypatch):
    """The arm, with a plain reader standing in for the extractor where lxml is missing."""
    try:
        import lxml.etree  # noqa: F401

        return C.ARM
    except ImportError:
        monkeypatch.setattr(
            C, "extract_actual_text_from_xml", lambda xml, etree: ("Le dix mai", None)
        )
        return dataclasses.replace(C.ARM, load_vendor=lambda: C.SimpleNamespace(etree=None))


def test_runs_the_release_runner_settings_and_resumes(tmp_path, monkeypatch):
    arm = _arm(monkeypatch)
    pages = tmp_path / "pages"
    _pages(pages, 1)
    Image.new("L", (3000, 1000), 255).save(pages / "p001.tif", compression="tiff_lzw")
    argv = [
        "run",
        "--pages", str(pages),
        "--out", str(tmp_path / "cache"),
        "--weights", str(_weights(tmp_path)),
        "--port", str(_free_port()),
        "--startup-timeout", "60",
        "--vllm-cmd", sys.executable, str(FAKE),
    ]  # fmt: skip
    assert C.base.main(arm, argv) == 0
    record = json.loads((tmp_path / "cache" / "churro-native" / "p001.json").read_text())
    assert set(record) == RECORD_KEYS and record["arm"] == "churro-native"
    assert record["text"] == "Le dix mai" and record["error"] is None
    request = record["units"][0]["request"]
    assert request["image_size"] == [2500, 833] and request["image_format"] == "PNG"
    assert request["sampling"] == {"temperature": 0.6} and request["max_tokens"] is None
    argv_sent = record["server"]["argv"]
    assert argv_sent[argv_sent.index("--max-model-len") + 1] == "20000"
    assert "--trust-remote-code" in argv_sent and "churro" in argv_sent
    assert "HistoricalDocument" in record["units"][0]["raw_response"]
    assert C.base.main(arm, argv) == 0
    events = (tmp_path / "cache" / "events.jsonl").read_text().splitlines()
    assert json.loads(events[-1])["event"] == "nothing-to-do"


def test_an_unreachable_server_is_tried_twice_and_recorded(tmp_path, monkeypatch):
    arm = _arm(monkeypatch)
    _pages(tmp_path / "pages", 1)
    argv = [
        "run",
        "--pages", str(tmp_path / "pages"),
        "--out", str(tmp_path / "cache"),
        "--server-url", f"http://127.0.0.1:{_free_port()}",
        "--request-timeout", "5",
    ]  # fmt: skip
    assert C.base.main(arm, argv) == 1
    path = tmp_path / "cache" / "churro-native" / "p000.json"
    record = json.loads(path.read_text())
    assert record["units"][0]["request"]["attempts_made"] == 2 and not W.cached_ok(path)


def test_image_and_prompt_rules(tmp_path):
    Image.new("1", (5000, 2600), 1).save(tmp_path / "a.tif", compression="group4")
    data, image_format, size = C.vendor_image(tmp_path / "a.tif")
    assert (image_format, size) == ("PNG", (2500, 1300))
    assert Image.open(io.BytesIO(data)).mode == "RGB"
    Image.new("RGB", (100, 80)).save(tmp_path / "b.jpg", quality=90)
    assert C.vendor_image(tmp_path / "b.jpg")[1:] == ("JPEG", (100, 80))
    prompt = C.SYSTEM_PROMPT
    assert C.trim_leading_prompt(prompt + "\n <HistoricalDocument/>", prompt) == (
        "<HistoricalDocument/>"
    )
    assert C.extract_actual_text_from_xml("plain answer", None) == ("plain answer", None)


SAMPLE_XML = """<?xml version="1.0" encoding="UTF-8"?>
<HistoricalDocument xmlns="http://example.org/historical">
  <Metadata><Language>French</Language><Description>A register page</Description></Metadata>
  <Page>
    <Header><PageNumber>12</PageNumber></Header>
    <Body>
      <Heading>Naissances</Heading>
      <Paragraph>
        <Line>Le dix mai mil huit cent</Line>
        <Line>est né <Deletion>Pierre</Deletion> Jean & fils<lb/></Line>
        <Line><Gap/>témoins <Illegible/>Paul</Line>
      </Paragraph>
      <Figure><Description>a seal</Description></Figure>
    </Body>
    <Footer>Signé</Footer>
  </Page>
  <Page><Body><Line>Second page
"""


def test_the_vendor_extractor_reads_headers_body_and_footers():
    etree = pytest.importorskip("lxml.etree")
    text, note = C.extract_actual_text_from_xml(SAMPLE_XML, etree)
    assert note is None
    # Each text node is a line: a removed Deletion or Gap leaves its neighbours joined.
    assert text.split("\n") == [
        "12",
        "Naissances",
        "Le dix mai mil huit cent",
        "est né  Jean & fils",
        "témoins Paul",
        "Signé",
        "",
        "Second page",
    ]
    assert C.extract_actual_text_from_xml("<HistoricalDocument></HistoricalDocument>", etree) == (
        "",
        "no text in any Page's Header, Body or Footer",
    )
