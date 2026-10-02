"""The finding-report check on synthetic reports: a clean one passes, each rule refuses."""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from pagekit.cleanroom import check_report
from pagekit.cleanroom.check_report import REQUIRED_SECTIONS, check

TEMPLATE = Path(__file__).with_name("templates") / "finding-report.md"
NONE: frozenset[str] = frozenset()

CLEAN = """# Finding: a margin note written sideways near the gutter

Reader brief sha256: {digest}

## Situation

A short note runs sideways down the inner margin, close to the fold of the book.

## Pagekit's observed behaviour

On the synthetic page the host ran, pagekit counted the note as discarded ink and sent
the crop to review, which is right. It reported nothing about the direction of the note.

## General technique

Text written at an angle can be found by projecting ink onto rows and columns at
several angles and keeping the angle with the sharpest peaks.
Source: projection profiles, as surveyed in Nagy, "Twenty years of document image
analysis in PAMI", IEEE PAMI 22(1):38-62, 2000, https://doi.org/10.1109/34.824820

## Settings in general terms

A search over angles within a few degrees of vertical, in steps of about half a degree.
""".format(digest="a" * 64)


def rules(text: str, hashes: frozenset[str] = NONE) -> set[str]:
    return {rule for rule, _line in check(text, hashes)}


def test_a_clean_report_passes():
    assert check(CLEAN, NONE) == []


def test_the_template_and_the_checker_name_the_same_sections():
    headings = [
        line[3:].strip() for line in TEMPLATE.read_text().splitlines() if line.startswith("## ")
    ]
    assert headings == list(REQUIRED_SECTIONS)


@pytest.mark.parametrize(
    ("inserted", "rule"),
    [
        ("```\nsomething\n```", "code_mark"),
        ("Call the `thing` here.", "code_mark"),
        ("~~~", "code_mark"),
        ("Use Thing" + "::" + "run on the page.", "code_token"),
        ("The result -" + "> the page.", "code_token"),
        ("#" + "include the header first.", "code_token"),
        ("def" + " margin(page): it returns the box.", "code_token"),
        ("class" + " PageBox: holds the edges.", "code_token"),
        ("It keeps a map " + "{" + "left, right" + "}" + " of edges.", "brace"),
        ("The margin is set to twelve" + ";", "semicolon_line_end"),
        ("This happens in margins" + ".cpp near the end.", "file_name"),
        ("See the header deskew" + ".h for this.", "file_name"),
        ("See the header deskew" + ".hpp for this.", "file_name"),
        ("It is in the layout" + ".qml screen.", "file_name"),
        ("It is in the helper" + ".py script.", "file_name"),
        ("It sits under src" + "/core/" + "filters somewhere.", "file_path"),
        ("See https" + "://example.org/page for more.", "link"),
        ("The loop starts at line" + " 120 of the file.", "line_number"),
        ("Around L" + "212 the margin is set.", "line_number"),
        ("Around margins" + ".cpp:" + "40 the value is set.", "line_number"),
    ],
)
def test_each_rule_refuses_its_content(inserted, rule):
    text = CLEAN.replace(
        "## Settings in general terms\n", f"## Settings in general terms\n\n{inserted}\n"
    )
    assert rule in rules(text)


def test_ordinary_english_class_and_a_doi_link_are_not_code():
    text = CLEAN.replace("Text written", "A class of pages with text written")
    assert check(text, NONE) == []


def test_a_word_on_the_deny_list_is_refused_by_its_hash_alone():
    word = "Zorblatt" + "Margin"
    hashes = frozenset({hashlib.sha256(word.encode()).hexdigest()})
    text = CLEAN.replace("A short note", f"A {word} note")
    assert rules(text, hashes) == {"denied_word"}
    assert check(CLEAN, hashes) == []


@pytest.mark.parametrize("section", REQUIRED_SECTIONS)
def test_a_missing_section_is_refused(section):
    text = CLEAN.replace(f"## {section}\n", "")
    assert "sections_missing_repeated_or_out_of_order" in rules(text)


def test_an_empty_section_an_extra_section_and_no_source_are_refused():
    empty = CLEAN.replace(
        "A search over angles within a few degrees of vertical, in steps of about half a degree.\n",
        "",
    )
    assert "empty_section:Settings in general terms" in rules(empty)
    assert "unexpected_section" in rules(CLEAN + "\n## Notes\n\nMore.\n")
    assert "general_technique_has_no_source_line" in rules(CLEAN.replace("Source: ", "From "))


def test_the_reader_brief_digest_is_required():
    assert "missing_reader_brief_digest" in rules(CLEAN.replace("a" * 64, "unknown"))


def test_the_command_never_prints_the_refused_text(tmp_path, capsys):
    secret = "Qwerty" + "Leak" + "::" + "run"
    report = tmp_path / "report.md"
    report.write_text(CLEAN.replace("A short note", f"A {secret} note"))
    assert check_report.main([str(report)]) == 1
    output = capsys.readouterr()
    assert "code_token" in output.err
    assert secret not in output.out + output.err
    assert "Qwerty" not in output.out + output.err

    report.write_text(CLEAN)
    assert check_report.main([str(report)]) == 0
    assert hashlib.sha256(CLEAN.encode()).hexdigest() in capsys.readouterr().out
    assert check_report.main([str(tmp_path / "missing.md")]) == 2
