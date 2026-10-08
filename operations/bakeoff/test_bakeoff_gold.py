"""The gold reader on a synthetic page file (never a real transcription)."""

from operations.bakeoff.gold import load_gold_dir, parse_gold, reduce_marks

SYNTHETIC = """# help text the checker ignores
FILE: p001.tif
STATUS: fool's gold
SOURCE: synthetic
CATEGORY: acts-19c
FORM: handwritten
CONDITION: good
VERDICT: acts
PAGES IN IMAGE: 1
TEST PAGE: no
NOTES: made up for a test

=== ACT 1 | baptism | from previous page: no | to next page: no ===
Le dix [[mai|mars]] mil huit cent
nous [[?]] pretre [struck: soussigne]
--- AI note: not gold text
avons baptise [ins: Pierre] fils
=== ACT B 2 (start) | burial | from previous page: yes | to next page: ? ===
Le onze [struck: de
cette annee] mai
=== ROWS ===
Abel | Jean | B | 12
=== HEADINGS ===
Table des [[bapt]]emes
"""


def test_header_sections_and_flags():
    page = parse_gold(SYNTHETIC, "p001")
    assert page.problems == []
    assert page.category == "acts-19c" and page.status == "fool's gold"
    assert [(a.label, a.kind, a.from_previous, a.to_next) for a in page.acts] == [
        ("1", "baptism", False, False),
        ("B 2 (start)", "burial", True, None),
    ]
    assert len(page.acts[0].lines) == 3  # the "--- AI" line is not gold
    assert page.rows == ["Abel | Jean | B | 12"]
    assert page.heading_lines() == ["Table des baptemes"]


def test_act_text_reduces_marks_across_lines():
    page = parse_gold(SYNTHETIC, "p001")
    assert page.act_text().split("\n") == [
        "Le dix mai mil huit cent",
        "nous pretre",
        "avons baptise Pierre fils",
        "Le onze mai",  # the struck span ran across the line end, and goes with it
    ]
    assert page.row_lines() == ["Abel Jean B 12"]
    assert page.reference_text().endswith("Table des baptemes\nAbel Jean B 12")


def test_reduce_marks_nested():
    assert reduce_marks("a [struck: b [[c|d]]] e") == "a e"
    assert reduce_marks("a [ins: [[b|c]] d] e") == "a b d e"
    assert reduce_marks("[[?]] x [[?]]") == "x"


def test_missing_header_is_a_problem_and_dir_loader(tmp_path):
    assert parse_gold("=== ROWS ===\nx\n").problems
    (tmp_path / "a").mkdir()
    (tmp_path / "a" / "p001.txt").write_text(SYNTHETIC, encoding="utf-8")
    (tmp_path / "README.txt").write_text("not a page file\n", encoding="utf-8")
    assert list(load_gold_dir(tmp_path)) == ["p001"]
