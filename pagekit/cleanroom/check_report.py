"""Refuse a finding report that carries code or source details, before anyone reads it.

The host runs this on a saved report file before opening it. Output names only the
rule that failed and the report's line number, never the text, so a leaking report
does not reach whoever reads the output. The rules catch accidents, not every
spelling; CLEANROOM.md says what each one is for.

    python3 -m pagekit.cleanroom.check_report REPORT.md

Exit 0: the report passes. Exit 1: it is refused. Exit 2: it could not be checked.
"""

from __future__ import annotations

import hashlib
import re
import sys
from pathlib import Path

from pagekit.cleanroom.scan import ScanError, denied_lines, load_deny_hashes

# The finding-report template's sections, in order. Each must appear once, with text.
REQUIRED_SECTIONS = (
    "Situation",
    "Pagekit's observed behaviour",
    "General technique",
    "Settings in general terms",
)
BRIEF_DIGEST = re.compile(r"^Reader brief sha256: [0-9a-f]{64}$", re.MULTILINE)
SOURCE_LINE = re.compile(r"^Source: \S", re.MULTILINE)
DOI = re.compile(r"https://doi\.org/\S+")

# Each rule is checked line by line; a report line may break several.
LINE_RULES: dict[str, re.Pattern[str]] = {
    "code_mark": re.compile(r"`|^\s*~~~"),
    "code_token": re.compile(
        r"::|->|#\s*include\b"
        r"|^\s*(?:def|class)\s+\w"
        r"|\b(?:def|class)\s+\w+\s*[(:{]"
    ),
    "brace": re.compile(r"[{}]"),
    "semicolon_line_end": re.compile(r";\s*$"),
    "file_name": re.compile(
        r"\b[\w-]+\.(?:cpp|cc|cxx|c|hh|h|hpp|hxx|py|qml|ui|pro)\b", re.IGNORECASE
    ),
    "file_path": re.compile(r"(?:[\w.-]+/){2,}|\w\\\w"),
    "link": re.compile(r"\w+://"),
    "line_number": re.compile(r"\blines?\s+\d|\bL\d+\b|#L\d|\b\w+\.\w+:\d"),
}


def check(text: str, hashes: frozenset[str]) -> list[tuple[str, int]]:
    """Each broken rule with the report line it was found on (0 for the whole report)."""
    problems: list[tuple[str, int]] = []
    for number, raw in enumerate(text.splitlines(), 1):
        line = DOI.sub("doi", raw)
        problems.extend(
            (rule, number) for rule, pattern in LINE_RULES.items() if pattern.search(line)
        )
    problems.extend(("denied_word", number) for number in denied_lines(text, hashes))
    problems.extend(_structure(text))
    return sorted(problems, key=lambda item: (item[1], item[0]))


def _structure(text: str) -> list[tuple[str, int]]:
    problems = []
    if not BRIEF_DIGEST.search(text):
        problems.append(("missing_reader_brief_digest", 0))
    headings = [
        (number, line[3:].strip())
        for number, line in enumerate(text.splitlines(), 1)
        if line.startswith("## ")
    ]
    names = [name for _number, name in headings]
    for number, name in headings:
        if name not in REQUIRED_SECTIONS:
            problems.append(("unexpected_section", number))
    if [name for name in names if name in REQUIRED_SECTIONS] != list(REQUIRED_SECTIONS):
        problems.append(("sections_missing_repeated_or_out_of_order", 0))
        return problems
    bodies = _section_bodies(text)
    for name in REQUIRED_SECTIONS:
        if not bodies.get(name, "").strip():
            problems.append((f"empty_section:{name}", 0))
    if not SOURCE_LINE.search(bodies.get("General technique", "")):
        problems.append(("general_technique_has_no_source_line", 0))
    return problems


def _section_bodies(text: str) -> dict[str, str]:
    bodies: dict[str, str] = {}
    current = None
    for line in text.splitlines():
        if line.startswith("## "):
            current = line[3:].strip()
            bodies[current] = ""
        elif line.startswith("# "):
            current = None
        elif current is not None:
            bodies[current] += line + "\n"
    return bodies


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    if len(args) != 1:
        print("usage: python3 -m pagekit.cleanroom.check_report REPORT.md", file=sys.stderr)
        return 2
    path = Path(args[0])
    try:
        data = path.read_bytes()
        text = data.decode("utf-8")
        hashes = load_deny_hashes()
    except (OSError, UnicodeDecodeError, ScanError) as error:
        print(f"finding report could not be checked: {error}", file=sys.stderr)
        return 2
    digest = hashlib.sha256(data).hexdigest()
    problems = check(text, hashes)
    if not problems:
        print(f"finding report passes: sha256 {digest}")
        return 0
    print(f"finding report refused: sha256 {digest}", file=sys.stderr)
    for rule, number in problems:
        where = f"line {number}" if number else "whole report"
        print(f"  {rule} ({where})", file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main())
