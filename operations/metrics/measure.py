"""Count Python size and suppression markers, so later cleanup can be compared.

Usage: python operations/metrics/measure.py [repo root]
Counts git-tracked .py files only, excluding gold/ and private/.
"""

import ast
import subprocess
import sys
from pathlib import Path

EXCLUDED = ("gold/", "private/")
MARKERS = ("# noqa", "# type: ignore", "# pragma: no cover")


def docstring_lines(source: str) -> int:
    nodes = (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)
    total = 0
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, nodes) and ast.get_docstring(node, clean=False) is not None:
            first = node.body[0]
            total += first.end_lineno - first.lineno + 1
    return total


def main(root: Path) -> None:
    listed = subprocess.run(
        ["git", "-C", str(root), "ls-files", "-z", "*.py"], capture_output=True, text=True, check=True
    ).stdout.split("\0")
    rows = {"src": dict.fromkeys(("files", "lines", "comments", "docstrings", *MARKERS), 0)}
    rows["test"] = dict(rows["src"])
    for name in listed:
        if not name or name.startswith(EXCLUDED) or not (root / name).is_file():
            continue
        is_test = Path(name).name.startswith("test_") or "tests" in Path(name).parts[:-1]
        kind = "test" if is_test else "src"
        source = (root / name).read_text(encoding="utf-8")
        lines = source.splitlines()
        row = rows[kind]
        row["files"] += 1
        row["lines"] += len(lines)
        row["comments"] += sum(line.lstrip().startswith("#") for line in lines)
        row["docstrings"] += docstring_lines(source)
        for marker in MARKERS:
            row[marker] += source.count(marker)
    print("| | " + " | ".join(rows["src"]) + " |")
    print("|---" * (len(rows["src"]) + 1) + "|")
    for kind, row in rows.items():
        print(f"| {kind} | " + " | ".join(str(v) for v in row.values()) + " |")


if __name__ == "__main__":
    main(Path(sys.argv[1] if len(sys.argv) > 1 else "."))
