"""The PyLaia arms against a stand-in `pylaia-htr-decode-ctc` (no torch)."""

import json
from functools import partial
from pathlib import Path

from PIL import Image

from operations.bakeoff.lines import blla, pylaia
from operations.bakeoff.lines.conftest import (
    LINE_BOXES,
    RECORD_KEYS,
    FakeRunner,
    alto,
    fake_venv,
    weights_dir,
)

# PyLaia's decode output as its Decode callback prints it: tqdm progress noise on the
# same stream, then `<id> <text>` per image (join_string "", spaces converted).
SAMPLE_STDOUT = (
    "Decoding:   0%|          | 0/1 [00:00<?, ?it/s]\n0001 la premiere ligne\n0002 seconde\n0003 \n"
)


def _decoder(argv):
    config = json.loads(Path(argv[argv.index("--config") + 1]).read_text())
    ids = Path(config["img_list"]).read_text().split()
    lines = {"0001": "la premiere ligne", "0002": "seconde"}
    return 0, "".join(f"{i} {lines.get(i, '')}\n" for i in ids)


def test_parse_predictions_reads_ids_and_text():
    assert pylaia.parse_predictions(SAMPLE_STDOUT) == {
        "0001": "la premiere ligne",
        "0002": "seconde",
        "0003": "",
    }


def _store(tmp_path, model, lm=False):
    store = tmp_path / "store"
    files = [*pylaia.WEIGHT_FILES, *([pylaia.LM_FILE] if lm else [])]
    return store, weights_dir(store, f"pylaia-{model}", files)


def test_surya_arm_resizes_to_128_grey_and_writes_records(pages, surya_dir, tmp_path):
    out = tmp_path / "cache"
    store, weights = _store(tmp_path, "belfort")
    venv = fake_venv(tmp_path, "pylaia-htr-decode-ctc")
    runner = FakeRunner(_decoder)
    argv = ["run", "--model", "belfort", "--lines", "surya", "--lines-dir", str(surya_dir),
            "--pages", str(pages), "--out", str(out), "--store-root", str(store), "--venv-dir", str(venv)]  # fmt: skip
    assert pylaia.main(argv, recogniser=partial(pylaia.PylaiaRecogniser, runner=runner)) == 0
    record = json.loads((out / "pylaia-belfort-surya" / "p000.json").read_text())
    assert set(record) == RECORD_KEYS and record["repo"] == "Teklia/pylaia-belfort"
    assert record["revision"] == pylaia.MODELS["belfort"]["revision"]
    assert record["text"] == "la premiere ligne\nseconde" and record["error"] is None
    sent = Image.open(record["units"][0]["request"]["image_sent"]["file"])
    x0, y0, x1, y1 = LINE_BOXES[1]
    assert sent.mode == "L" and sent.height == 128 and sent.width == (x1 - x0) * 128 // (y1 - y0)
    config = json.loads(Path(runner.calls[0][-1]).read_text())
    assert config["decode"]["use_language_model"] is False and config["trainer"] == {"gpus": 0}
    assert config["common"]["train_path"] == str(weights) and config["data"]["color_mode"] == "L"


def test_lm_arm_names_and_refusal(pages, tmp_path):
    out = tmp_path / "cache"
    store, weights = _store(tmp_path, "popp")
    venv = fake_venv(tmp_path, "pylaia-htr-decode-ctc")
    segmentation = out / "_lines" / "blla"
    segmentation.mkdir(parents=True)
    for page in sorted(pages.iterdir()):
        raw = alto(str(page), [(LINE_BOXES[0], []), (LINE_BOXES[1], [])], order=[0, 1])
        (segmentation / f"{page.stem}.xml").write_text(raw)
    argv = ["run", "--model", "popp", "--lm", "--lines", "blla", "--pages", str(pages),
            "--out", str(out), "--store-root", str(store), "--venv-dir", str(venv),
            "--kraken-venv", str(tmp_path / "no-kraken")]  # fmt: skip
    factory = partial(pylaia.PylaiaRecogniser, runner=FakeRunner(_decoder))
    assert pylaia.main(argv, recogniser=factory) == 2  # no language model file yet
    (weights / pylaia.LM_FILE).write_text("\\data\\\n")
    runner = FakeRunner(_decoder)
    assert pylaia.main(argv, recogniser=partial(pylaia.PylaiaRecogniser, runner=runner)) == 0
    record = json.loads((out / "pylaia-popp-lm-blla" / "p001.json").read_text())
    assert record["arm"] == "pylaia-popp-lm-blla" and record["text"] == "la premiere ligne\nseconde"
    decode = json.loads(Path(runner.calls[0][-1]).read_text())["decode"]
    assert (
        decode["use_language_model"] is True and decode["language_model_weight"] == pylaia.LM_WEIGHT
    )
    assert decode["language_model_path"].endswith(pylaia.LM_FILE)
    assert blla.read_alto((segmentation / "p001.xml").read_bytes())[0]["text"] == ""


def test_missing_predictions_are_errors(pages, surya_dir, tmp_path):
    out = tmp_path / "cache"
    store, _ = _store(tmp_path, "belfort")
    venv = fake_venv(tmp_path, "pylaia-htr-decode-ctc")
    runner = FakeRunner(lambda argv: (0, "0001 seule\n"))
    argv = ["run", "--lines", "surya", "--lines-dir", str(surya_dir), "--limit", "1",
            "--pages", str(pages), "--out", str(out), "--store-root", str(store), "--venv-dir", str(venv)]  # fmt: skip
    assert pylaia.main(argv, recogniser=partial(pylaia.PylaiaRecogniser, runner=runner)) == 1
    record = json.loads((out / "pylaia-belfort-surya" / "p000.json").read_text())
    assert "no prediction for 0002" in record["error"]
