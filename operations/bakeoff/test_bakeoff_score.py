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
    assert S.FOOLS_GOLD_LABEL in md and "### acts" in md and "p001 (" in md
    assert "| acts | 1 | 0 | 0 | CER" in md
    assert "| all pages, for reference | 1 | 0 | 1 |" in md  # the index page: missing


def gold_text(category, acts=(), rows=(), headings=(), test="no", form="handwritten", more=()):
    """A synthetic gold file; the words are invented, never a real transcription.

    `acts` are the lines of a first act; each entry of `more` is the text of a further act.
    """
    text = (
        f"FILE: x.tif\nSTATUS: fool's gold\nCATEGORY: {category}\nFORM: {form}\n"
        f"VERDICT: acts\nTEST PAGE: {test}\n\n"
    )
    if acts:
        text += "=== ACT 1 | baptism | from previous page: no | to next page: no ===\n"
        text += "\n".join(acts) + "\n"
    for number, act in enumerate(more, 2):
        text += f"=== ACT {number} | burial | from previous page: no | to next page: no ===\n"
        text += act + "\n"
    if headings:
        text += "=== HEADINGS ===\n" + "\n".join(headings) + "\n"
    if rows:
        text += "=== ROWS ===\n" + "\n".join(rows) + "\n"
    return text


ROWS = ["Abellus | Iovan | 12", "Rossius | Mara | 4", "Cantor | Lucius | 7"]
ACT = ["Anno domini vigesimo baptizavi Petrum", "filium legitimum Marci fabri"]
PAGES = {
    "a001": gold_text("acts-19c", acts=ACT),
    "a002": gold_text("acts-18c", acts=["Die tertia sepultus est Paulus senex"]),
    "h001": gold_text("acts-20c", acts=["Quarto idus nupserunt Titus et Livia"]),
    "i001": gold_text("index", rows=ROWS, headings=["Tabula nominum"]),
    "l001": gold_text("list", rows=["Ferrarius | Gaius | 3", "Pistor | Iulia | 9"]),
    "g001": gold_text("ledger", rows=["Solvit Gaius | x | 12", "Debet Iulia | v | 3"]),
    "c001": gold_text("contract", acts=["Coram notario convenerunt partes infrascriptae"]),
    "b001": gold_text("blank"),
    "n001": gold_text("near-blank", acts=["vacat"]),
    "t001": gold_text("index", rows=ROWS, test="yes: a rotated page, expect nothing"),
}


def write_gold(directory, pages=PAGES):
    directory.mkdir(parents=True, exist_ok=True)
    for stem, text in pages.items():
        (directory / f"{stem}.txt").write_text(text, encoding="utf-8")


def write_reading(cache, model, arm, page, text, **extra):
    folder = cache / model
    folder.mkdir(parents=True, exist_ok=True)
    record = {
        "schema": "bakeoff-witness-page.v1",
        "model": model,
        "arm": arm,
        "page": page,
        "text": text,
        "finish_reason": "stop",
        "loop": False,
        "seconds": 1.0,
        "error": None,
        **extra,
    }
    (folder / f"{page}.json").write_text(json.dumps(record), encoding="utf-8")


def _full_reading(stem):
    from operations.bakeoff.gold import parse_gold

    return parse_gold(PAGES[stem], stem).reference_text()


def _cache(tmp_path):
    cache = tmp_path / "cache"
    for stem in PAGES:
        write_reading(cache, "chandra", "chandra-native", stem, _full_reading(stem))
        write_reading(cache, "popp", "pylaia-popp-blla", stem, _full_reading(stem))
        write_reading(cache, "mystery", "label-x", stem, _full_reading(stem))
        dai_text = _full_reading(stem) if stem.startswith("a") or stem == "h001" else ""
        write_reading(cache, "dai", "dai", stem, dai_text)
    write_reading(cache, "chandra", "chandra-native", "b001", "Lorem ipsum dolor sit amet nimis")
    write_reading(cache, "popp", "pylaia-popp-blla", "b001", "Lorem ipsum")
    write_reading(cache, "chandra", "chandra-native", "h001", "nihil simile")
    return cache


def test_groups_hard_pages_and_fair_report(tmp_path):
    write_gold(tmp_path / "gold")
    cache = _cache(tmp_path)
    (tmp_path / "hard.txt").write_text("# hard pages\nh001.tif\n", encoding="utf-8")
    (tmp_path / "exclude.txt").write_text("n001\n", encoding="utf-8")
    argv = ["--cache", str(cache), "--gold", str(tmp_path / "gold"), "--out", str(tmp_path)]
    argv += ["--hard-pages", str(tmp_path / "hard.txt"), "--exclude", str(tmp_path / "exclude.txt")]
    assert S.main(argv) == 0
    rows = [json.loads(x) for x in (tmp_path / "scores.jsonl").read_text().splitlines()]
    by = {(r["model"], r["page"]): r for r in rows}
    assert by["chandra", "t001"]["group"] == "test"  # an index page, but a test page
    assert by["chandra", "i001"]["group"] == "index-list"
    assert by["chandra", "g001"]["group"] == "tables"
    assert not by["dai", "i001"]["scored"] and by["dai", "a001"]["scored"]
    assert not by["popp", "c001"]["scored"] and by["popp", "g001"]["scored"]
    assert by["chandra", "h001"]["hard"] and not by["chandra", "a001"]["hard"]
    assert ("chandra", "n001") not in by  # excluded
    assert by["chandra", "b001"]["false_text"] and not by["popp", "b001"]["false_text"]
    assert by["chandra", "i001"]["surname_recall"] == 1
    assert by["chandra", "i001"]["false_line_rate"] == 0  # the heading is not a false line

    md = (tmp_path / "scores.md").read_text()
    dai = md.split("## dai (arm dai)")[1].split("\n## ")[0]
    assert "| acts | 2 | 1 | 0 | CER 0.000 | 0.000 | WER 0.000 |" in dai  # h001 counted hard
    assert "index-list" not in dai and "all pages, for reference" in dai
    index = md.split("### index-list, handwritten pages (line recall, higher is better)")[1].split(
        "###"
    )[0]
    assert "| chandra | 2 | 1.000 |" in index and "| dai |" not in index
    assert "Not scored here: dai (reads records on act pages" in index
    prose = md.split("### prose-other")[1].split("###")[0]
    assert "popp (census-table model" in prose
    blank = md.split(
        "### blank-like, handwritten pages (false text (pages with no gold text), lower"
    )[1].split("###")[0]
    assert "| chandra | 1 | 1.000 |" in blank and "| mystery | 1 | 0.000 |" in blank
    assert "not in the fairness table" in md and "'mystery'" in md
    assert "## Test pages" in md and "Expected: a rotated page, expect nothing" in md
    assert "## Hard pages" in md and "| h001 | acts | chandra | CER" in md
    assert "Excluded from scoring: 1 pages." in md
    acts = md.split("### acts, handwritten pages (CER, lower is better)")[1].split("###")[0]
    assert "| chandra | 2 | 0.000 |" in acts  # the hard page's bad reading is left out


def test_surname_recall_and_false_lines():
    rows = ["Abellus Iovan 12", "Rossius Mara 4", "Cantor Lucius 7"]
    assert S.surname_recall(rows, "Abelus Iovan\nzz Rossius\nnothing") == pytest.approx(2 / 3)
    assert S.surname_recall(rows, "Abellus") == pytest.approx(1 / 3)  # one token, used once
    assert S.surname_recall([], "x") is None
    result = S.line_recall(rows, ["Abellus Iovan 12", "Tabula", "noise line"], ["Tabula"])
    assert result["line_recall"] == pytest.approx(1 / 3)
    assert result["false_line_rate"] == pytest.approx(1 / 3)


def test_read_stems_and_expected_behaviour(tmp_path):
    from operations.bakeoff.gold import parse_gold

    (tmp_path / "s.txt").write_text("p001.tif\n\n# note\np002\n", encoding="utf-8")
    assert S.read_stems(tmp_path / "s.txt") == {"p001", "p002"}
    page = parse_gold(gold_text("blank", test="yes: say it is blank"), "p001")
    assert S.expected_behaviour(page) == "say it is blank"
    assert S.page_group(page) == "test"
    assert S.expected_behaviour(parse_gold(gold_text("blank"), "p002")) is None


def _run(tmp_path, pages):
    write_gold(tmp_path / "gold", pages)
    argv = ["--cache", str(tmp_path / "cache"), "--gold", str(tmp_path / "gold")]
    assert S.main([*argv, "--out", str(tmp_path)]) == 0
    rows = [json.loads(x) for x in (tmp_path / "scores.jsonl").read_text().splitlines()]
    return {(r["model"], r["page"]): r for r in rows}, (tmp_path / "scores.md").read_text()


def test_form_rows_and_handwritten_and_typed_comparisons(tmp_path):
    from operations.bakeoff.groups import form_of

    assert form_of("Typed") == "typed" and form_of("printed form") == "printed form"
    assert form_of("") == "handwritten" and form_of("mixed: hand and print") == "mixed"
    pages = {
        "a001": gold_text("acts-19c", acts=ACT),
        "a002": gold_text("acts-20c", acts=ACT, form="typed"),
        "a003": gold_text("acts-20c", acts=ACT, form="typed"),
        "a004": gold_text("acts-19c", acts=ACT, form="printed form"),
    }
    cache = tmp_path / "cache"
    for stem in pages:
        write_reading(cache, "chandra", "chandra-native", stem, " ".join(ACT))
    write_reading(cache, "chandra", "chandra-native", "a003", "")
    by, md = _run(tmp_path, pages)
    assert (
        by["chandra", "a002"]["form"] == "typed" and by["chandra", "a001"]["form"] == "handwritten"
    )
    section = md.split("## chandra (arm chandra-native)")[1]
    assert "| median | mean |" in section
    assert "| acts | 4 | 0 | 0 | CER 0.000 |" in section
    assert "| ↳ handwritten | 1 |" in section and "| ↳ printed form | 1 |" in section
    typed_row = next(line for line in section.splitlines() if line.startswith("| ↳ typed |"))
    assert "| ↳ typed | 2 | 0 | 0 | CER 0.500 |" in typed_row  # median of 0 and 1
    hand = md.split("### acts, handwritten pages (CER")[1].split("###")[0]
    assert "| chandra | 1 | 0.000 |" in hand
    typed = md.split("### acts, typed pages (CER")[1].split("##")[0]
    assert "| chandra | 2 | 0.500 |" in typed


def test_record_arm_is_scored_by_act(tmp_path):
    first, second = "Anno domini baptizavi Petrum filium", "Die tertia sepultus est Paulus senex"
    pages = {
        "a001": gold_text("acts-19c", acts=[first], more=[second]),
        "a002": gold_text("acts-19c", acts=[first], more=[second]),
    }
    cache = tmp_path / "cache"
    units = [
        {"request": {"unit": "record-0"}, "text": first},
        {"request": {"unit": "record-1"}, "text": "zzzz qqqq wwww"},
    ]
    write_reading(cache, "dai", "dai", "a001", first + "\nzzzz qqqq wwww", units=units)
    whole = [{"request": {"unit": "whole-page"}, "text": first + "\n" + second}]
    write_reading(
        cache, "dai", "dai", "a002", first + "\n" + second, units=whole, whole_page_fallback=True
    )
    write_reading(cache, "chandra", "chandra-native", "a001", first)
    by, md = _run(tmp_path, pages)
    one, two = by["dai", "a001"]["record"], by["dai", "a002"]["record"]
    assert (one["act_recall"], one["units_unmatched"], one["unit_cers"]) == (0.5, 1, [0.0])
    assert two["whole_page_fallback"] and two["acts_matched"] == 0  # one unit, two acts
    assert "record" not in by["chandra", "a001"]
    dai = md.split("## dai (arm dai)")[1].split("\n## ")[0]
    assert "act recall | units unmatched | unit CER | whole-page fallbacks |" in dai
    acts = next(line for line in dai.splitlines() if line.startswith("| acts |"))
    assert acts.endswith("| 0.250 | 2 | 0.000 | 1 |")
    chandra = md.split("## chandra (arm chandra-native)")[1].split("\n## ")[0]
    assert "act recall" not in chandra
    assert S.is_record_reading({"arm": "other", "units": [{"unit": "record-3"}]})
    assert not S.is_record_reading({"arm": "other", "units": [{"unit": "page"}]})


def test_false_text_only_where_the_gold_has_no_text():
    from operations.bakeoff.gold import parse_gold

    def row(category, acts, text):
        record = {"model": "m", "arm": "chandra-native", "text": text}
        return S.score_page(record, parse_gold(gold_text(category, acts=acts), "p001"))

    long = "Lorem ipsum dolor sit amet nimis longe"
    texted = row("non-register", ["Avis venditionis domus"], long)
    assert texted["false_text"] is None and texted["cer"] is not None
    assert row("blank", [], long)["false_text"] is True
    assert row("blank", [], "Lorem")["false_text"] is False
    rows = [texted, row("blank", [], long), row("blank", [], "")]
    assert S.summarise(rows, "false_text_rate") == pytest.approx(0.5)
