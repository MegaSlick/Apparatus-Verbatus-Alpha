"""The kraken arms end to end against a stand-in kraken command (no torch)."""

import json
import unicodedata
from functools import partial
from pathlib import Path

import pytest

from operations.bakeoff.lines import harness, kraken_ppocr
from operations.bakeoff.lines.conftest import (
    LINE_BOXES,
    RECORD_KEYS,
    FakeRunner,
    alto,
    fake_venv,
    weights_dir,
)

PPOCR = kraken_ppocr.MODELS["ppocrv6"]


def _kraken_lines(argv):
    """kraken -i crop out.txt ... ocr -s: one plain-text file per crop."""
    for i, flag in enumerate(argv):
        if flag == "-i":
            out = Path(argv[i + 2])
            out.write_text(f"ligne {out.stem}\n")
    return 0, ""


def _kraken_page(argv):
    """kraken -a -i page out.xml segment -bl ocr: an ALTO answer."""
    xml = Path(argv[argv.index("-i") + 2])
    lines = [(LINE_BOXES[0], ["premiere", "ligne"]), (LINE_BOXES[1], ["seconde"])]
    xml.write_text(alto(argv[argv.index("-i") + 1], lines, order=[0, 1]))
    return 0, ""


def _argv(pages, out, store, venv, *extra):
    return ["run", "--pages", str(pages), "--out", str(out), "--store-root", str(store),
            "--venv-dir", str(venv), *extra]  # fmt: skip


def test_surya_lines_arm_writes_records_and_resumes(pages, surya_dir, tmp_path):
    out, store = tmp_path / "cache", tmp_path / "store"
    weights_dir(store, PPOCR["artifact"], [PPOCR["file"]])
    venv = fake_venv(tmp_path, "kraken")
    runner = FakeRunner(_kraken_lines)
    factory = partial(kraken_ppocr.KrakenRecogniser, runner=runner)
    argv = _argv(pages, out, store, venv, "--lines", "surya", "--lines-dir", str(surya_dir))
    assert kraken_ppocr.main(argv, recogniser=factory) == 0
    record = json.loads((out / "kraken-ppocrv6-surya" / "p000.json").read_text())
    assert set(record) == RECORD_KEYS
    assert record["arm"] == "kraken-ppocrv6-surya" and record["error"] is None
    assert record["text"] == "ligne 0001\nligne 0002\nligne 0003"
    assert record["revision"].endswith(PPOCR["sha256"])
    first = record["units"][0]
    assert (
        first["request"]["bbox"] == list(LINE_BOXES[1]) and first["raw_response"] == "ligne 0001\n"
    )
    assert record["server"]["argv"][-4:-1] == ["ocr", "-s", "-m"]
    assert len(runner.calls) == 2
    run = json.loads((out / "kraken-ppocrv6-surya" / "run.json").read_text())
    assert set(run) == {
        "model",
        "pages",
        "units",
        "wall_seconds",
        "concurrency",
        "max_num_seqs",
        "finished",
    }

    assert kraken_ppocr.main(argv, recogniser=factory) == 0
    assert len(runner.calls) == 2  # everything cached: kraken is not called again
    events = [json.loads(x)["event"] for x in (out / "events.jsonl").read_text().splitlines()]
    assert events[-1] == "nothing-to-do"


def test_blla_arm_is_kraken_native_and_normalises_alto(pages, tmp_path):
    out, store = tmp_path / "cache", tmp_path / "store"
    weights_dir(store, PPOCR["artifact"], [PPOCR["file"]])
    venv = fake_venv(tmp_path, "kraken")
    runner = FakeRunner(_kraken_page)
    factory = partial(kraken_ppocr.KrakenRecogniser, runner=runner)
    assert (
        kraken_ppocr.main(_argv(pages, out, store, venv, "--lines", "blla"), recogniser=factory)
        == 0
    )
    record = json.loads((out / "kraken-ppocrv6-blla" / "p001.json").read_text())
    assert record["text"] == "premiere ligne\nseconde"
    first, second = record["units"]
    assert first["request"]["unit"] == "line-0001" and first["raw_response"] == "premiere ligne"
    assert second["request"]["bbox"] == list(LINE_BOXES[1])
    assert first["request"]["line_source"].startswith("blla")
    assert runner.calls[0][-5:-1] == ["segment", "-bl", "ocr", "-m"]
    assert runner.calls[0][1:3] == ["-d", "cpu"]  # never kraken's `auto`


def test_a_failed_page_is_recorded_and_retried(pages, surya_dir, tmp_path):
    out, store = tmp_path / "cache", tmp_path / "store"
    weights_dir(store, PPOCR["artifact"], [PPOCR["file"]])
    venv = fake_venv(tmp_path, "kraken")
    failing = FakeRunner(lambda argv: (1, ""))
    argv = _argv(
        pages, out, store, venv, "--lines", "surya", "--lines-dir", str(surya_dir), "--limit", "1"
    )
    assert (
        kraken_ppocr.main(argv, recogniser=partial(kraken_ppocr.KrakenRecogniser, runner=failing))
        == 1
    )
    record = json.loads((out / "kraken-ppocrv6-surya" / "p000.json").read_text())
    assert record["error"] and record["finish_reason"] == "error"
    working = FakeRunner(_kraken_lines)
    assert (
        kraken_ppocr.main(argv, recogniser=partial(kraken_ppocr.KrakenRecogniser, runner=working))
        == 0
    )
    assert len(working.calls) == 1


def test_refusals(pages, surya_dir, tmp_path, capsys):
    out, store = tmp_path / "cache", tmp_path / "store"
    argv = _argv(
        pages, out, store, tmp_path / "nowhere", "--lines", "surya", "--lines-dir", str(surya_dir)
    )
    assert kraken_ppocr.main(argv) == 2  # no weights
    weights_dir(store, PPOCR["artifact"], [PPOCR["file"]])
    assert kraken_ppocr.main(argv) == 2  # no environment
    assert "install" in capsys.readouterr().err
    no_lines = _argv(pages, out, store, fake_venv(tmp_path, "kraken"), "--lines", "surya")
    assert (
        kraken_ppocr.main(no_lines, recogniser=partial(kraken_ppocr.KrakenRecogniser, runner=None))
        == 2
    )


def test_install_syncs_the_locked_recipe(tmp_path):
    runner = FakeRunner(lambda argv: (0, ""))
    assert harness.install(kraken_ppocr.ARM, tmp_path / "venv", runner) == 0
    assert runner.calls == [["uv", "sync", "--frozen", "--project", str(kraken_ppocr.ARM.recipe)]]
    assert (kraken_ppocr.ARM.recipe / "uv.lock").is_file()


def test_check_compares_installed_versions_with_the_pins(tmp_path, capsys):
    venv = fake_venv(tmp_path)
    good = FakeRunner(lambda argv: (0, json.dumps(kraken_ppocr.ARM.pins)))
    assert harness.check(kraken_ppocr.ARM, venv, good) == 0
    stale = FakeRunner(lambda argv: (0, json.dumps({"kraken": "7.0.3", "torch": "2.14.0"})))
    assert harness.check(kraken_ppocr.ARM, venv, stale) == 2
    assert '"ok": false' in capsys.readouterr().out


def test_plain_lines_are_nfc_with_whitespace_collapsed():
    assert harness.plain("été  \n\n  a \t b ") == "été\na b"


@pytest.mark.parametrize("model", ["mccatmus", "mcfondue"])
def test_the_french_mlmodel_arms_pin_their_own_file_and_write_nfc(
    model, pages, surya_dir, tmp_path
):
    pinned = kraken_ppocr.MODELS[model]
    out, store = tmp_path / "cache", tmp_path / "store"
    weights_dir(store, pinned["artifact"], [pinned["file"]])

    def nfd_lines(argv):
        for i, flag in enumerate(argv):
            if flag == "-i":
                Path(argv[i + 2]).write_text(
                    unicodedata.normalize("NFD", "fait à Paris\n"), "utf-8"
                )
        return 0, ""

    runner = FakeRunner(nfd_lines)
    argv = _argv(pages, out, store, fake_venv(tmp_path, "kraken"), "--model", model,
                 "--lines", "surya", "--lines-dir", str(surya_dir), "--limit", "1")  # fmt: skip
    factory = partial(kraken_ppocr.KrakenRecogniser, runner=runner)
    assert kraken_ppocr.main(argv, recogniser=factory) == 0
    record = json.loads((out / f"kraken-{model}-surya" / "p000.json").read_text())
    assert record["arm"] == f"kraken-{model}-surya"
    assert record["repo"] == f"https://doi.org/{pinned['doi']}"
    assert record["revision"] == f"{pinned['file']}@sha256:{pinned['sha256']}"
    assert runner.calls[0][-1].endswith(f"{pinned['artifact']}/{pinned['file']}")
    assert record["text"].split("\n")[0] == unicodedata.normalize("NFC", "fait à Paris")
