"""The leak scan: pagekit's own tree is clean, and each rule finds its synthetic hit."""

from __future__ import annotations

import hashlib

import pytest

from pagekit.cleanroom import scan

NONE: frozenset[str] = frozenset()
# Assembled at run time so this file does not hold what it tests for.
OWNER = "scan" + "tailor"
FORK = "4lex" + "4"


def test_pagekit_tree_has_no_hits():
    """NOTICE's credit links name the projects' front pages, which the URL rule allows."""
    files = scan.tracked_files()
    assert any(path == "pagekit/NOTICE" for path, _data in files)
    hits = scan.scan(files, scan.load_deny_hashes())
    assert hits == [], scan.summary(hits, len(files))


@pytest.mark.parametrize(
    ("line", "rule"),
    [
        (
            "under the terms of the GNU " + "General Public License as published",
            "gpl_licence_header",
        ),
        ("SPDX-License-" + "Identifier: GPL-3.0-or-later", "gpl_licence_header"),
        ("either version 3 " + "of the License, or", "gpl_licence_header"),
        ("Copyright (C) 2007 " + "Scan" + "Tailor authors", "foreign_copyright"),
        (f"https://github.com/{OWNER}/{OWNER}/blob/master/x.cpp", "source_repository_url"),
        (f"git clone https://github.com/{FORK}/{OWNER}-advanced.git", "source_repository_url"),
        (
            f"https://raw.githubusercontent.com/{FORK}/{OWNER}-advanced/master/y",
            "source_repository_url",
        ),
    ],
)
def test_each_rule_finds_its_synthetic_hit(line, rule):
    assert [hit.rule for hit in scan.scan_text("x.md", f"intro\n{line}\n", NONE)] == [rule]
    assert scan.scan_text("x.md", f"intro\n{line}\n", NONE)[0].line == 2


@pytest.mark.parametrize(
    "line",
    [
        "Copyright (C) 2007 Joseph Artsi" + "movich",
        "Copyright \u00a9 2015 " + FORK,
        "Copyright 2019 the Scan" + "Tailor Advanced developers",
        "\u00a9 2020 Scan" + "Tailor authors",
        "(c) 2007-2009 Joseph Artsi" + "movich <someone@example.invalid>",
        " * COPYRIGHT (C) " + OWNER + " contributors",
    ],
)
def test_copyright_notices_in_several_formats_are_hits(line):
    assert [hit.rule for hit in scan.scan_text("x.cpp", line + "\n", NONE)] == ["foreign_copyright"]


@pytest.mark.parametrize(
    "line",
    [
        "the host scans for GPL/Scan" + "Tailor copyright or licence headers",
        "a copyright notice naming Scan" + "Tailor or its authors is refused",
        "Copyright questions about Scan" + "Tailor are a lawyer's matter",
    ],
)
def test_prose_about_copyright_notices_is_not_a_hit(line):
    assert scan.scan_text("x.md", line + "\n", NONE) == []


def test_a_front_page_credit_link_is_not_a_hit():
    for link in (
        f"https://github.com/{OWNER}/{OWNER}",
        f"https://github.com/{FORK}/{OWNER}-advanced/",
    ):
        assert scan.scan_text("NOTICE", f"  Credit: {link}\n", NONE) == []


def test_a_denied_word_is_found_by_hash_and_the_summary_never_prints_it():
    word = "Florp" + "Deskewer"
    hashes = frozenset({hashlib.sha256(word.encode()).hexdigest()})
    hits = scan.scan([("pagekit/a.py", f"x = 1\ny = {word}()\n".encode())], hashes)
    assert hits == [scan.Hit("pagekit/a.py", 2, "denied_word")]
    assert word not in scan.summary(hits, 1)
    assert "pagekit/a.py:2: denied_word" in scan.summary(hits, 1)


def test_binary_files_are_skipped_and_a_bad_deny_file_cannot_pass(tmp_path):
    assert scan.scan([("pagekit/a.png", b"\x89PNG\0GNU General " + b"Public License")], NONE) == []
    bad = tmp_path / "deny.txt"
    bad.write_text("# comment\nnot-a-hash\n")
    with pytest.raises(scan.ScanError):
        scan.load_deny_hashes(bad)
