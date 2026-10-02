"""Scan pagekit's files for signs of copied GPL page-processing code.

Four rules, each a deterministic text match. "The two projects" are ScanTailor and
ScanTailor Advanced.

- ``gpl_licence_header``: the wording of a GPL licence header or an SPDX GPL tag;
- ``foreign_copyright``: a copyright notice that names one of the two projects or
  their authors;
- ``source_repository_url``: a link into the two projects' source repositories (a
  file, tree, clone or raw URL). A bare link to a repository's front page is a credit,
  as in NOTICE, and is not matched;
- ``denied_word``: a word whose sha256 is listed in ``deny-hashes.txt``.

Output names the rule, the pagekit file and the line, never the matched text, so the
scan can be read by the host without carrying what it found. These patterns catch
accidents, not every spelling.

    python3 -m pagekit.cleanroom.scan            # tracked files under pagekit/
    python3 -m pagekit.cleanroom.scan --staged   # staged files under pagekit/

Exit 0: no hits. Exit 1: hits. Exit 2: the scan could not run.
"""

from __future__ import annotations

import argparse
import hashlib
import re
import subprocess
import sys
from collections import Counter
from collections.abc import Iterable
from pathlib import Path
from typing import NamedTuple

ROOT = Path(__file__).resolve().parents[2]
DENY_HASHES = Path(__file__).with_name("deny-hashes.txt")
SCOPE = "pagekit"

# Each pattern is written so that its own source text does not match it.
_OWNERS = r"(?:scan-?tailor|4lex[4])"
RULES: dict[str, re.Pattern[str]] = {
    "gpl_licence_header": re.compile(
        r"GNU\s+(?:Affero\s+|Lesser\s+)?General\s+Public\s+Licen[cs]e"
        r"|SPDX-License-Identifier:\s*[AL]?GPL"
        r"|either\s+version\s+\d+\s+of\s+the\s+Licen[cs]e"
        r"|Free\s+Software\s+Foundation",
        re.IGNORECASE,
    ),
    "foreign_copyright": re.compile(
        r"copyright\b.{0,80}?(?:scan\s*tailor|artsimovi(?:ch)|4lex[4])", re.IGNORECASE
    ),
    "source_repository_url": re.compile(
        r"github\.com[/:]" + _OWNERS + r"/[\w.-]+?(?:\.git\b|/[^\s)>\]]+)"
        r"|(?:raw\.githubusercontent|codeload\.github)\.com/" + _OWNERS + "/"
        r"|api\.github\.com/repos/" + _OWNERS + "/",
        re.IGNORECASE,
    ),
}
WORD = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
HEX = re.compile(r"[0-9a-f]{64}")


class Hit(NamedTuple):
    path: str
    line: int
    rule: str


class ScanError(RuntimeError):
    """The scan could not run, which is not the same as finding nothing."""


def load_deny_hashes(path: Path = DENY_HASHES) -> frozenset[str]:
    hashes = set()
    for number, raw in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if not HEX.fullmatch(line):
            raise ScanError(f"{path.name} line {number} is not a lowercase hex sha256")
        hashes.add(line)
    return frozenset(hashes)


def word_digest(word: str) -> str:
    return hashlib.sha256(word.encode("utf-8")).hexdigest()


def denied_lines(text: str, hashes: frozenset[str]) -> list[int]:
    """Line numbers holding a word whose digest is denied."""
    if not hashes:
        return []
    return [
        number
        for number, line in enumerate(text.splitlines(), 1)
        if any(word_digest(word) in hashes for word in WORD.findall(line))
    ]


def scan_text(path: str, text: str, hashes: frozenset[str]) -> list[Hit]:
    hits = []
    for number, line in enumerate(text.splitlines(), 1):
        for rule, pattern in RULES.items():
            if pattern.search(line):
                hits.append(Hit(path, number, rule))
    hits.extend(Hit(path, number, "denied_word") for number in denied_lines(text, hashes))
    return sorted(hits)


def scan(files: Iterable[tuple[str, bytes]], hashes: frozenset[str]) -> list[Hit]:
    hits = []
    for path, data in files:
        if b"\0" in data:
            continue  # binary, such as a test image
        hits.extend(scan_text(path, data.decode("utf-8", errors="replace"), hashes))
    return sorted(hits)


def _git(root: Path, *args: str) -> bytes:
    try:
        result = subprocess.run(["git", *args], cwd=root, capture_output=True, check=False)
    except OSError as error:
        raise ScanError(f"git could not run: {error}") from error
    if result.returncode != 0:
        raise ScanError(f"git {args[0]} failed")
    return result.stdout


def _paths(output: bytes) -> list[str]:
    return [item.decode("utf-8", errors="replace") for item in output.split(b"\0") if item]


def tracked_files(root: Path = ROOT) -> list[tuple[str, bytes]]:
    files = []
    for path in _paths(_git(root, "ls-files", "-z", "--", SCOPE)):
        target = root / path
        if target.is_file():
            files.append((path, target.read_bytes()))
    return files


def staged_files(root: Path = ROOT) -> list[tuple[str, bytes]]:
    """The staged content of every added or changed file under pagekit/."""
    changed = _git(
        root,
        "diff",
        "--cached",
        "--name-only",
        "-z",
        "--no-renames",
        "--diff-filter=ACM",
        "--",
        SCOPE,
    )
    return [(path, _git(root, "cat-file", "blob", f":{path}")) for path in _paths(changed)]


def summary(hits: list[Hit], files: int) -> str:
    if not hits:
        return f"pagekit clean-room scan: no hits in {files} files."
    lines = [f"pagekit clean-room scan: {len(hits)} hits in {files} files."]
    for rule, count in sorted(Counter(hit.rule for hit in hits).items()):
        lines.append(f"  {rule}: {count}")
    lines.extend(f"  {hit.path}:{hit.line}: {hit.rule}" for hit in hits)
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python3 -m pagekit.cleanroom.scan")
    parser.add_argument("--staged", action="store_true", help="scan the index, not the tree")
    args = parser.parse_args(argv)
    try:
        hashes = load_deny_hashes()
        files = staged_files() if args.staged else tracked_files()
    except (OSError, ScanError) as error:
        print(f"pagekit clean-room scan could not run: {error}", file=sys.stderr)
        return 2
    hits = scan(files, hashes)
    print(summary(hits, len(files)), file=sys.stderr if hits else sys.stdout)
    return 1 if hits else 0


if __name__ == "__main__":
    sys.exit(main())
