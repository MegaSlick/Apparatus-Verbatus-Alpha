"""The Party arm against a stand-in `party` command (no torch)."""

import json
from functools import partial
from pathlib import Path

from operations.bakeoff.lines import party
from operations.bakeoff.lines.conftest import (
    LINE_BOXES,
    RECORD_KEYS,
    FakeRunner,
    alto,
    fake_venv,
    weights_dir,
)


def _party(skip_stem=None):
    def respond(argv):
        if argv[1] == "set-lang":
            assert argv[2] == "fra"
            return 0, ""
        for i, flag in enumerate(argv):
            if flag == "-i" and Path(argv[i + 1]).name != f"{skip_stem}.in.xml":
                words = [(LINE_BOXES[0], ["une", "ligne"]), (LINE_BOXES[1], ["deux"])]
                Path(argv[i + 2]).write_text(alto("page.tif", words, order=[0, 1]))
        return 0, ""

    return respond


def _setup(pages, tmp_path):
    out, store = tmp_path / "cache", tmp_path / "store"
    weights_dir(store, "party-v2", [party.MODEL_FILE])
    segmentation = out / "_lines" / "blla"
    segmentation.mkdir(parents=True)
    for page in sorted(pages.iterdir()):
        raw = alto(str(page), [(LINE_BOXES[0], []), (LINE_BOXES[1], [])], order=[0, 1])
        (segmentation / f"{page.stem}.xml").write_text(raw)
    argv = ["run", "--pages", str(pages), "--out", str(out), "--store-root", str(store),
            "--venv-dir", str(fake_venv(tmp_path, "party")), "--device", "cuda",
            "--kraken-venv", str(tmp_path / "no-kraken")]  # fmt: skip
    return out, argv


def test_one_party_process_reads_every_page(pages, tmp_path):
    out, argv = _setup(pages, tmp_path)
    runner = FakeRunner(_party())
    assert party.main(argv, recogniser=partial(party.PartyRecogniser, runner=runner)) == 0
    assert [call[1] for call in runner.calls] == ["set-lang", "-d"]
    ocr = runner.calls[1]
    assert ocr[ocr.index("--prompt-mode") + 1] == "curves" and "--add-lang-token" in ocr
    assert ocr[ocr.index("--precision") + 1] == "bf16-mixed" and ocr.count("-i") == 2
    record = json.loads((out / "party-blla" / "p000.json").read_text())
    assert set(record) == RECORD_KEYS and record["arm"] == "party-blla"
    assert record["text"] == "une ligne\ndeux" and record["error"] is None
    assert [u["request"]["unit"] for u in record["units"]] == ["line-0001", "line-0002"]
    assert record["units"][1]["request"]["bbox"] == list(LINE_BOXES[1])


def test_a_page_party_did_not_answer_is_an_error(pages, tmp_path):
    out, argv = _setup(pages, tmp_path)
    runner = FakeRunner(_party(skip_stem="p001"))
    assert party.main(argv, recogniser=partial(party.PartyRecogniser, runner=runner)) == 1
    assert json.loads((out / "party-blla" / "p000.json").read_text())["error"] is None
    assert "no answer" in json.loads((out / "party-blla" / "p001.json").read_text())["error"]
