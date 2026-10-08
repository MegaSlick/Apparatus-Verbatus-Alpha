"""Read the lead's per-image bake-off text files (one `.txt` per image, same stem).

Format (the bake-off set's TEMPLATE.txt): header lines `KEY: value` (FILE, STATUS, SOURCE,
CATEGORY, FORM, CONDITION, VERDICT, PAGES IN IMAGE, TEST PAGE, NOTES), then sections:

    === ACT <label> | <kind> | from previous page: yes/no | to next page: yes/no ===
    === ROWS ===        one row per line, columns separated by " | "
    === HEADINGS ===    titles and column headings of an index or list

Lines starting `#` (help) and `--- AI` (the AI's notes) are never gold text. Marks:
`[[?]]` unread ink, `[[word]]` and `[[word|other]]` doubtful readings, `[struck: ...]`
crossed out, `[ins: ...]` written in above the line.

This reader never changes a file and never judges its content; it only parses.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

HEADER_KEYS = (
    "FILE",
    "STATUS",
    "SOURCE",
    "CATEGORY",
    "FORM",
    "CONDITION",
    "VERDICT",
    "PAGES IN IMAGE",
    "TEST PAGE",
    "NOTES",
)
_HEADER = re.compile(r"^(" + "|".join(re.escape(k) for k in HEADER_KEYS) + r"):\s?(.*)$")
_SECTION = re.compile(r"^===\s*(.*?)\s*===\s*$")
_DOUBT = re.compile(r"\[\[([^\[\]]*)\]\]")
_STRUCK = re.compile(r"\[struck:\s*([^\[\]]*)\]")
_INSERTED = re.compile(r"\[ins:\s*([^\[\]]*)\]")


@dataclass
class Act:
    label: str
    kind: str
    from_previous: bool | None
    to_next: bool | None
    lines: list[str] = field(default_factory=list)


@dataclass
class GoldPage:
    stem: str
    header: dict[str, str]
    acts: list[Act]
    rows: list[str]
    headings: list[str]
    problems: list[str]

    @property
    def category(self) -> str:
        return self.header.get("CATEGORY", "").strip() or "unknown"

    @property
    def status(self) -> str:
        return self.header.get("STATUS", "").strip()

    def act_text(self) -> str:
        """All act text in order, marks reduced (`reduce_marks`), one line per gold line.

        Reduced over each act as a whole, so a struck or inserted span that runs onto
        the next line is still one span.
        """
        return "\n".join(
            line
            for act in self.acts
            for line in reduce_marks("\n".join(act.lines)).split("\n")
            if line
        )

    def row_lines(self) -> list[str]:
        """Each row as plain text: marks reduced, column separators turned into spaces."""
        return [r for row in self.rows if (r := reduce_marks(row.replace(" | ", " ")))]

    def heading_lines(self) -> list[str]:
        return [h for heading in self.headings if (h := reduce_marks(heading))]

    def reference_text(self) -> str:
        """What a whole-page reading is compared with: acts, then headings and rows."""
        parts = [self.act_text(), *self.heading_lines(), *self.row_lines()]
        return "\n".join(p for p in parts if p)


def _yes_no(value: str) -> bool | None:
    word = value.strip().lower()
    return True if word.startswith("yes") else False if word.startswith("no") else None


def reduce_marks(line: str) -> str:
    """One gold line as scored text.

    `[[?]]` is dropped, `[[word|other]]` and `[[word]]` keep their first reading,
    struck text is dropped, inserted text is kept in place. Innermost marks first, so a
    doubtful word inside a struck or inserted span is handled too. Spaces are collapsed.
    """
    previous = None
    text = line
    while previous != text:
        previous = text
        text = _DOUBT.sub(
            lambda m: "" if m.group(1).strip() == "?" else m.group(1).split("|")[0], text
        )
        text = _STRUCK.sub("", text)
        text = _INSERTED.sub(lambda m: m.group(1), text)
    return "\n".join(" ".join(part.split()) for part in text.split("\n"))


def parse_gold(text: str, stem: str = "") -> GoldPage:
    header: dict[str, str] = {}
    acts: list[Act] = []
    rows: list[str] = []
    headings: list[str] = []
    problems: list[str] = []
    section: str | None = None  # None (header), "act", "rows", "headings"
    for number, raw in enumerate(text.splitlines(), 1):
        line = raw.rstrip()
        if line.lstrip().startswith("#") or line.startswith("--- AI"):
            continue
        heading = _SECTION.match(line)
        if heading:
            title = heading.group(1)
            upper = title.upper()
            if upper == "ROWS":
                section = "rows"
            elif upper == "HEADINGS":
                section = "headings"
            elif upper.startswith("ACT"):
                parts = [p.strip() for p in title.split("|")]
                flags = {
                    p.split(":", 1)[0].strip().lower(): p.split(":", 1)[1]
                    for p in parts[2:]
                    if ":" in p
                }
                acts.append(
                    Act(
                        label=parts[0][3:].strip(),
                        kind=parts[1] if len(parts) > 1 else "",
                        from_previous=_yes_no(flags.get("from previous page", "")),
                        to_next=_yes_no(flags.get("to next page", "")),
                    )
                )
                section = "act"
            else:
                problems.append(f"line {number}: unknown section {title!r}")
                section = "unknown"
            continue
        if section is None:
            match = _HEADER.match(line)
            if match:
                header[match.group(1)] = match.group(2).strip()
            elif line.strip():
                problems.append(f"line {number}: text before any section that is not a header")
            continue
        if not line.strip():
            continue
        if section == "act":
            acts[-1].lines.append(line)
        elif section == "rows":
            rows.append(line)
        elif section == "headings":
            headings.append(line)
    missing = [k for k in ("STATUS", "CATEGORY", "VERDICT") if k not in header]
    if missing:
        problems.append(f"missing header lines: {missing}")
    return GoldPage(stem, header, acts, rows, headings, problems)


def load_gold_dir(directory: Path, pattern: str = "**/*.txt") -> dict[str, GoldPage]:
    """Every page file matching `pattern` under `directory`, keyed by stem.

    For the bake-off set, `pattern="*/Prepped/*.txt"`: the Full image texts share stems
    with unsplit prepped images.
    """
    pages: dict[str, GoldPage] = {}
    for path in sorted(directory.glob(pattern)):
        if path.stem in pages:
            raise SystemExit(f"two gold files share the stem {path.stem!r}")
        page = parse_gold(path.read_text(encoding="utf-8"), path.stem)
        if "CATEGORY" in page.header:  # skip READMEs and notes that are not page files
            pages[path.stem] = page
    return pages
