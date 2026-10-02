"""The commit-time clean-room gate: HOLD, then the leak scan over staged pagekit files.

While ``pagekit/cleanroom/HOLD`` exists (in the last commit, the staged tree or the
working copy), a commit may change only the HOLD file and the incident records under
``pagekit/cleanroom/incidents/``. The commit that removes HOLD must also add or change
an incident record carrying the lead's ``Decision:`` line. Everything is read from
the commit being made, never from branch names or environment variables.

The pre-commit hook runs this from the repository root:

    python3 -m pagekit.cleanroom.gate

A hook can be skipped, so CI repeats the HOLD and scan checks on every pull request.
Exit 0: allowed. Exit 1: refused. Exit 2: the gate could not run.
"""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

from pagekit.cleanroom import scan

HOLD = "pagekit/cleanroom/HOLD"
INCIDENTS = "pagekit/cleanroom/incidents/"
DECISION = re.compile(r"^Decision:[ \t]*(?:purge|minor breach|false flag)\b", re.I | re.M)


def _git(root: Path, *args: str) -> subprocess.CompletedProcess[bytes]:
    return subprocess.run(["git", *args], cwd=root, capture_output=True, check=False)


def _exists(root: Path, spec: str) -> bool:
    return _git(root, "cat-file", "-e", spec).returncode == 0


def staged_changes(root: Path) -> list[tuple[str, str]]:
    """(status letter, path) for each staged change under pagekit/."""
    result = _git(root, "diff", "--cached", "--name-status", "-z", "--no-renames", "--", "pagekit")
    if result.returncode != 0:
        raise scan.ScanError("git diff --cached failed")
    fields = [item.decode("utf-8", errors="replace") for item in result.stdout.split(b"\0")]
    fields = [field for field in fields if field]
    return list(zip(fields[0::2], fields[1::2], strict=True))


def has_decision(text: str) -> bool:
    return bool(DECISION.search(text))


def hold_problems(root: Path) -> list[str]:
    in_head = _exists(root, f"HEAD:{HOLD}")
    in_index = _exists(root, f":{HOLD}")
    if not (in_head or in_index or (root / HOLD).exists()):
        return []
    changes = staged_changes(root)
    problems = [
        f"{path}: pagekit is on HOLD; only {HOLD} and {INCIDENTS} may change"
        for _status, path in changes
        if path != HOLD and not path.startswith(INCIDENTS)
    ]
    if in_head and not in_index:
        decided = any(
            status in ("A", "M")
            and path.startswith(INCIDENTS)
            and has_decision(
                _git(root, "cat-file", "blob", f":{path}").stdout.decode("utf-8", "replace")
            )
            for status, path in changes
        )
        if not decided:
            problems.append(
                f"removing {HOLD} needs the lead's decision in the same commit: an incident "
                f"record under {INCIDENTS} with a line 'Decision: purge', "
                "'Decision: minor breach' or 'Decision: false flag'"
            )
    return problems


def main(root: Path | None = None) -> int:
    root = Path.cwd() if root is None else root
    try:
        problems = hold_problems(root)
        files = scan.staged_files(root)
        hits = scan.scan(files, scan.load_deny_hashes())
    except (OSError, scan.ScanError) as error:
        print(f"pagekit clean-room gate could not run: {error}", file=sys.stderr)
        return 2
    for problem in problems:
        print(f"  {problem}", file=sys.stderr)
    if hits:
        print(scan.summary(hits, len(files)), file=sys.stderr)
    if problems or hits:
        print("  See pagekit/cleanroom/CLEANROOM.md.", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
