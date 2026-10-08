"""Normaliser, CER, line recall, loop flag and the report, on synthetic text only."""

import json

import pytest

from operations.bakeoff import score as S
from operations.bakeoff.test_bakeoff_gold import SYNTHETIC


def test_chandra_html_tables_become_lines():
    html = (
        '<div data-bbox="0 0 500 500" data-label="Text"><p>Le dix mai</p></div>'
        '<div data-bbox="0 500 1000 1000" data-label="Table"><table>'
        "<tr><td>Abel</td><td>B</td></tr><tr><td>Roy</td><td>M</td></tr></table></div>"
    )
    assert S.normalise_output("chandra", html) == "Le dix mai\nAbel B\nRoy M"
    assert S.normalise_output("chandra", "<p>not a layout answer</p>") == "not a layout answer"


def test_think_markdown_churro_and_dai():
    text = "<think>plan</think>\n# Titre\n| a | b |\n|---|---|\n| c | d |\n- x -"
    assert S.normalise_output("qwen-blind", text) == "Titre\na b\nc d\n- x -"
    assert S.normalise_output("qwen-blind", "<think>never closed") == ""
    assert S.normalise_output("qwen-blind", "un [[?]] deux [[trois|tres]]") == "un deux trois"
    xml = "<HistoricalDocument><Page><Body><Line>un</Line><Line>deux</Line></Body></Page>"
    assert S.normalise_output("churro", xml + "</HistoricalDocument>") == "un\ndeux"
    assert S.normalise_output("dai", "Jean [UNCERTAIN] Roy") == "Jean Roy"


def test_cer_wer():
    assert S.cer_wer("abcd", "abcd")["cer"] == 0
    assert S.cer_wer("abcd", "abxd")["cer"] == pytest.approx(0.25)
    assert S.cer_wer("un deux", "un deux trois")["wer"] == pytest.approx(0.5)
    assert S.cer_wer("", "text")["cer"] is None
    long = S.cer_wer("ab", "x" * 30_000)
    assert long["cer"] > 1 and long["cer_basis"].startswith("fallback")


def test_line_recall_is_one_to_one():
    gold = ["Abel Jean B 12", "Abel Jean B 13", "Roy Marie M 4"]
    model = ["Abel Jean B 12", "Roy Marie M 4", "something else"]
    result = S.line_recall(gold, model)
    assert (result["rows"], result["rows_matched"]) == (3, 2)
    assert S.line_recall(["Abel Jean B 12"], ["Abel Jean 8 12"])["line_recall"] == 1
    assert S.line_recall(["Abel Jean B 12"], ["Zz"])["line_recall"] == 0


def test_loop_flag():
    assert S.loop_flag("a\n" * 29 + "b", "stop") == (False, None)
    assert S.loop_flag("x\n" + " A  \n\n" * 30, "stop")[0]
    assert S.loop_flag("fine", "length") == (True, "finish_reason length")


def test_main_writes_labelled_report(tmp_path):
    gold_dir, cache = tmp_path / "gold", tmp_path / "cache"
    gold_dir.mkdir()
    (gold_dir / "p001.txt").write_text(SYNTHETIC, encoding="utf-8")
    (gold_dir / "p002.txt").write_text(SYNTHETIC.replace("acts-19c", "index"), encoding="utf-8")
    (cache / "dai").mkdir(parents=True)
    page = {
        "schema": "bakeoff-witness-page.v1",
        "model": "dai",
        "arm": "dai",
        "page": "p001",
        "text": "Le dix mai mil huit cent\nnous pretre",
        "finish_reason": "stop",
        "loop": False,
        "seconds": 2.0,
        "error": None,
    }
    (cache / "dai" / "p001.json").write_text(json.dumps(page), encoding="utf-8")
    (cache / "dai" / "run.json").write_text(json.dumps({"pages": 1, "wall_seconds": 4.0}))
    assert S.main(["--cache", str(cache), "--gold", str(gold_dir), "--out", str(tmp_path)]) == 0
    rows = [json.loads(x) for x in (tmp_path / "scores.jsonl").read_text().splitlines()]
    assert len(rows) == 1 and 0 < rows[0]["cer"] < 1
    md = (tmp_path / "scores.md").read_text()
    assert S.FOOLS_GOLD_LABEL in md and "## index" in md and "p001 (" in md
    assert "| dai | 0 | 1 |" in md  # the index page has no reading: counted missing
