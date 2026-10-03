"""The commit-time clean-room gate: HOLD, then the leak scan over staged pagekit files.

While ``pagekit/cleanroom/HOLD`` exists (in the last commit, the staged tree or the
working copy), a commit may change only the HOLD file and the incident records under
``pagekit/cleanroom/incidents/``. The commit that removes HOLD must also add or change
an incident record carrying the lead's ``Decision:`` line, and every incident record it
leaves must carry one. Everything is read from
the commit being made, never from branch names or environment variables.

The pre-commit hook runs this from the repository root:

    python3 -m pagekit.cleanroom.gate

An incident note is never deleted, held or not.

A hook can be skipped, so CI repeats these checks on every pull request: it replays
the HOLD rule over every commit the branch adds (``replay_hold``), checks that every
incident note on main is still there with its text unchanged (``incidents_kept``), and
runs the leak scan over the tree.
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


EMPTY_TREE = "4b825dc642cb6eb9a060e54bf8d69288fbee4904"


def _changes(root: Path, *diff_args: str) -> list[tuple[str, str]]:
    """(status letter, path) for each change under pagekit/ in a diff."""
    result = _git(root, "diff", *diff_args, "--name-status", "-z", "--no-renames", "--", "pagekit")
    if result.returncode != 0:
        raise scan.ScanError("git diff failed")
    fields = [item.decode("utf-8", errors="replace") for item in result.stdout.split(b"\0")]
    fields = [field for field in fields if field]
    return list(zip(fields[0::2], fields[1::2], strict=True))


def staged_changes(root: Path) -> list[tuple[str, str]]:
    return _changes(root, "--cached")


def has_decision(text: str) -> bool:
    return bool(DECISION.search(text))


def _hold_rule(
    root: Path, before: str, after: str, changes: list[tuple[str, str]], held: bool = False
) -> list[str]:
    """The HOLD rule for one change from tree `before` to tree `after`.

    `before` and `after` are object-name prefixes: "HEAD:", ":" for the index, or a
    commit followed by ":". `held` adds a hold seen elsewhere (the working copy).
    """
    problems = [
        f"{path}: an incident note is never deleted"
        for status, path in changes
        if status == "D" and path.startswith(INCIDENTS)
    ]
    in_before = _exists(root, f"{before}{HOLD}")
    in_after = _exists(root, f"{after}{HOLD}")
    if not (in_before or in_after or held):
        return problems
    problems += [
        f"{path}: pagekit is on HOLD; only {HOLD} and {INCIDENTS} may change"
        for _status, path in changes
        if path != HOLD and not path.startswith(INCIDENTS)
    ]
    if in_before and not in_after:
        notes = _incident_notes(root, after)
        undecided = [path for path in notes if not has_decision(_blob(root, f"{after}{path}"))]
        decided_here = any(
            status in ("A", "M") and path.startswith(INCIDENTS) and path not in undecided
            for status, path in changes
        )
        if not decided_here:
            problems.append(
                f"removing {HOLD} needs the lead's decision in the same commit: an incident "
                f"record under {INCIDENTS} with a line 'Decision: purge', "
                "'Decision: minor breach' or 'Decision: false flag'"
            )
        problems += [f"{path}: removing {HOLD} needs a decision in this note" for path in undecided]
    return problems


def _blob(root: Path, spec: str) -> str:
    result = _git(root, "cat-file", "blob", spec)
    if result.returncode != 0:
        raise scan.ScanError(f"git could not read {spec}")
    return result.stdout.decode("utf-8", "replace")


def _incident_notes(root: Path, tree: str) -> list[str]:
    """Every incident note in `tree`, an object-name prefix as in `_hold_rule`."""
    if tree == ":":
        listed = _git(root, "ls-files", "-z", "--", INCIDENTS)
    else:
        listed = _git(root, "ls-tree", "-r", "-z", "--name-only", tree.rstrip(":"), "--", INCIDENTS)
    if listed.returncode != 0:
        raise scan.ScanError(f"git could not list {INCIDENTS} in {tree}")
    return [item.decode("utf-8", "replace") for item in listed.stdout.split(b"\0") if item]


def hold_problems(root: Path) -> list[str]:
    """The HOLD rule for the commit being made: last commit to the index."""
    return _hold_rule(root, "HEAD:", ":", staged_changes(root), (root / HOLD).exists())


def _lines(result: subprocess.CompletedProcess[bytes], what: str) -> list[str]:
    if result.returncode != 0:
        raise scan.ScanError(f"git could not list {what}")
    return result.stdout.decode("utf-8", "replace").split()


def replay_hold(root: Path, base: str) -> list[str]:
    """The HOLD rule replayed over every commit in base..HEAD, each against its first
    parent, so a commit made with the hook skipped is still caught."""
    problems = []
    commits = _lines(_git(root, "rev-list", "--reverse", f"{base}..HEAD"), "commits")
    for commit in commits:
        parent = _git(root, "rev-parse", "--verify", "--quiet", f"{commit}^1")
        before = parent.stdout.decode().strip() if parent.returncode == 0 else EMPTY_TREE
        changes = _changes(root, before, commit)
        problems += [
            f"commit {commit[:12]}: {problem}"
            for problem in _hold_rule(root, f"{before}:", f"{commit}:", changes)
        ]
    return problems


def incidents_kept(root: Path, base: str) -> list[str]:
    """Every incident note on `base` is still in HEAD, its text on `base` (trailing
    whitespace aside) unchanged at the start: notes are added to, never rewritten."""
    listed = _git(root, "ls-tree", "-r", "-z", "--name-only", base, "--", INCIDENTS)
    if listed.returncode != 0:
        raise scan.ScanError(f"git could not list {INCIDENTS} on {base}")
    problems = []
    for path in [item.decode() for item in listed.stdout.split(b"\0") if item]:
        read = _git(root, "cat-file", "blob", f"{base}:{path}")
        if read.returncode != 0:
            raise scan.ScanError(f"git could not read {path} on {base}")
        old = read.stdout.rstrip()
        new = _git(root, "cat-file", "blob", f"HEAD:{path}")
        if new.returncode != 0:
            problems.append(f"{path}: an incident note on {base} was deleted")
        elif not new.stdout.startswith(old):
            problems.append(f"{path}: text already on {base} was changed; add to the note instead")
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
