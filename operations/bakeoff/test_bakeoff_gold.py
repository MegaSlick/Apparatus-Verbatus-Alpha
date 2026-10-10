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


def test_diplomatic_text_keeps_doubt_marks_and_scores_like_reduce_marks():
    # C7 P1: training targets were built from reduce_marks, which erases [[?]] and the
    # other readings. The diplomatic text keeps the Perlector's doubt grammar; its scored
    # words are still exactly reduce_marks's.
    from operations.bakeoff.gold import diplomatic_text, marked_words, scored_text

    cases = [
        "Le [[?]] [[juin|juillet]] mil huit",
        "a [struck: b [[c|d]]] e",
        "a [ins: [[b|c]] d] e",
        "[[?]] x [[?]]",
        "l'[[abbé]], [[Jean Baptiste|J. Bte]] fils",
        "Le [struck: dix\nonze] mai",
    ]
    for raw in cases:
        text = diplomatic_text(raw)
        assert [w.text for w in marked_words(text)] == reduce_marks(raw).split(), raw
    assert diplomatic_text("Le [[?]] [[juin|juillet]] mil") == "Le [[?]] [[juin|juillet]] mil"
    assert diplomatic_text("a [struck: b [[c|d]]] e") == "a e"
    assert diplomatic_text("a [ins: [[b|c]] d] e") == "a [[b|c]] d e"
    assert scored_text("[[?]] [[?]]") == ""
    words = marked_words("l'[[abbé]], [[Jean Baptiste|J. Bte]] fils")
    assert [(w.text, w.doubtful) for w in words] == [
        ("l'abbé,", True), ("Jean", True), ("Baptiste", True), ("fils", False),
    ]  # fmt: skip
    text = "l'[[abbé]], x"
    assert "".join(text[i] for i in marked_words(text)[0].chars) == "l'abbé,"


def test_doubt_is_tracked_by_position_not_spelling():
    # C7 P2: `Marie épouse [[Marie|Maria]]` -- the first, certain Marie is not doubtful.
    from operations.bakeoff.gold import marked_words

    words = marked_words("Marie épouse [[Marie|Maria]]")
    assert [(w.text, w.doubtful) for w in words] == [
        ("Marie", False), ("épouse", False), ("Marie", True),
    ]  # fmt: skip
