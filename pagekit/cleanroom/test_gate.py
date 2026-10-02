"""The CI side of the HOLD rule, on throwaway repositories: replaying it over a branch's
commits catches what a skipped hook let through, and incident notes are append-only."""

from __future__ import annotations

import subprocess
from pathlib import Path

from pagekit.cleanroom.gate import HOLD, incidents_kept, replay_hold

NOTE = "pagekit/cleanroom/incidents/0001.md"


def git(repo: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True)


def write(repo: Path, relative: str, text: str) -> None:
    (repo / relative).parent.mkdir(parents=True, exist_ok=True)
    (repo / relative).write_text(text)
    git(repo, "add", relative)


def commit(repo: Path, message: str) -> None:
    git(repo, "commit", "-qm", message)  # no hooks are installed here


def repo_with_main(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    git(tmp_path, "init", "-q", "-b", "main", str(repo))
    for key, value in (("user.name", "T"), ("user.email", "t@example.invalid")):
        git(repo, "config", key, value)
    git(repo, "config", "commit.gpgsign", "false")
    write(repo, "pagekit/check.py", "VALUE = 1\n")
    commit(repo, "base")
    git(repo, "switch", "-q", "-c", "work/x")
    return repo


def hold(repo: Path) -> None:
    write(repo, HOLD, "Suspected leak.\n")
    write(repo, NOTE, "# Incident 0001\n\nDecision:\n")
    commit(repo, "hold")


def test_replay_catches_a_hold_lifted_without_a_decision_and_a_held_change(tmp_path):
    repo = repo_with_main(tmp_path)
    hold(repo)
    git(repo, "rm", "-q", HOLD)
    write(repo, "pagekit/check.py", "VALUE = 2\n")
    commit(repo, "skipped hook")
    problems = replay_hold(repo, "main")
    assert any("pagekit/check.py: pagekit is on HOLD" in problem for problem in problems)
    assert any("needs the lead's decision" in problem for problem in problems)


def test_replay_catches_a_deleted_incident_note(tmp_path):
    repo = repo_with_main(tmp_path)
    hold(repo)
    git(repo, "rm", "-q", HOLD, NOTE)
    commit(repo, "erase the incident")
    problems = replay_hold(repo, "main")
    assert any("never deleted" in problem for problem in problems)
    assert any("needs the lead's decision" in problem for problem in problems)


def test_replay_accepts_the_proper_sequence(tmp_path):
    repo = repo_with_main(tmp_path)
    hold(repo)
    git(repo, "rm", "-q", HOLD)
    write(repo, NOTE, "# Incident 0001\n\nDecision: false flag, a common idiom.\n")
    commit(repo, "lift")
    write(repo, "pagekit/check.py", "VALUE = 3\n")
    commit(repo, "resume")
    assert replay_hold(repo, "main") == []


def test_incident_notes_on_main_are_append_only(tmp_path):
    repo = repo_with_main(tmp_path)
    git(repo, "switch", "-q", "main")
    write(repo, NOTE, "# Incident 0001\n\nWhat was detected: a header.\n\nDecision:\n")
    commit(repo, "note on main")
    git(repo, "switch", "-q", "work/x")
    git(repo, "merge", "-q", "main")
    assert incidents_kept(repo, "main") == []

    write(repo, NOTE, "# Incident 0001\n\nWhat was detected: a header.\n\nDecision: purge.\n")
    commit(repo, "decide")
    assert incidents_kept(repo, "main") == []

    write(repo, NOTE, "# Incident 0001\n\nWhat was detected: nothing.\n\nDecision: purge.\n")
    commit(repo, "rewrite")
    assert any("was changed" in problem for problem in incidents_kept(repo, "main"))

    git(repo, "rm", "-q", NOTE)
    commit(repo, "delete")
    assert any("was deleted" in problem for problem in incidents_kept(repo, "main"))
