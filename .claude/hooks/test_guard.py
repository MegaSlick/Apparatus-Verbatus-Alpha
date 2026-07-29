from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

SCRIPT = Path(__file__).with_name("guard.py")
SPEC = importlib.util.spec_from_file_location("verbatus_guard", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
guard = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(guard)


def payload(
    tool: str = "Bash",
    tool_input: dict | None = None,
    *,
    agent: str | None = None,
    cwd: Path | None = None,
) -> dict:
    result = {
        "tool_name": tool,
        "tool_input": tool_input if tool_input is not None else {"command": "git status"},
    }
    if agent:
        result.update({"agent_type": agent, "agent_id": "agent-123"})
    if cwd:
        result["cwd"] = str(cwd)
    return result


@pytest.mark.parametrize(
    "command",
    [
        "git status --short",
        "git diff --check",
        "git switch work/topic",
        "git clean --dry-run",
        "git config --get core.hooksPath",
        'git commit -m "document --no-verify semantics"',
        "runpodctl get pod abc",
        "runpodctl stop pod abc",
        "env NAME=value command",
        "curl -fsS https://example.test/status",
        "gh pr view 10",
        "rm /tmp/one-file",
        "rg TOKEN README.md",
    ],
)
def test_ordinary_read_or_bounded_local_work_stays_open(command):
    assert guard.evaluate(payload(tool_input={"command": command})) is None


@pytest.mark.parametrize(
    "command",
    [
        "git push origin work/topic",
        "git push --force origin work/topic",
        "git push -f origin work/topic",
        "git push origin +work/topic",
        "ALLOW_FORCE_PUSH=work/topic git push --force origin work/topic",
        "git merge work/topic",
        "git rebase main",
        "git commit --amend",
        "env GIT_TRACE=0 git push origin work/topic",
        "git reset --hard HEAD",
        "git restore app.py",
        "git restore --staged --worktree app.py",
        "git restore -SW app.py",
        "git checkout app.py",
        "git clean -fd",
        "git branch -D work/topic",
        "git stash clear",
        "rm -rf workbench/archive",
        "rm ./workbench/active/HANDOFF.md",
        "rm /tmp/repo/workbench/active/HANDOFF.md",
        "/bin/rm /tmp/repo/workbench/active/HANDOFF.md",
        "cat private/ntfy.conf",
        "runpodctl create pods",
        "ssh pod.example reboot",
        "curl -X POST https://example.test/jobs",
        "curl -dfoo=bar https://example.test/jobs",
        "curl -T artifact.bin https://example.test/upload",
        "curl -Tartifact.bin https://example.test/upload",
        "curl -F file=@artifact.bin https://example.test/upload",
        "curl -Ffile=@artifact.bin https://example.test/upload",
        "curl --json '{}' https://example.test/jobs",
        "wget --post-data=x https://example.test/jobs",
        "http POST https://example.test/jobs",
        "python -c 'import runpod; runpod.create_pod()'",
        "gh pr comment 10 --body fixed",
        "env | sort",
        "set",
        "declare -p",
    ],
)
def test_main_session_gets_one_exact_confirmation_for_consequential_actions(command):
    decision, reason = guard.evaluate(payload(tool_input={"command": command}))
    assert decision == "ask"
    assert "Confirm this exact action" in reason


@pytest.mark.parametrize(
    "command",
    [
        "git push origin work/topic",
        "git push --force origin work/topic",
        "git merge work/topic",
        "git reset --hard HEAD",
        "rm -rf workbench/archive",
        "cat private/ntfy.conf",
        "runpodctl start pod abc",
        "runpodctl stop pod abc",
        "curl --data x=1 https://example.test",
        "gh issue close 3",
    ],
)
def test_subagents_cannot_take_consequential_or_external_actions(command):
    decision, reason = guard.evaluate(payload(tool_input={"command": command}, agent="worker"))
    assert decision == "deny"
    assert "main session" in reason


@pytest.mark.parametrize(
    "command",
    [
        "git push origin main",
        "git push origin HEAD:refs/heads/main",
        "git push --no-verify origin work/topic",
        "git -c core.hooksPath=/dev/null push origin work/topic",
        "rtk git push --force origin main",
        "/usr/bin/git push origin main",
        "env -i git push origin main",
        "sudo -u tyrel git push origin main",
        "git config unset core.hooksPath",
        "git config remove-section core",
        "git config rename-section core core-old",
    ],
)
def test_hard_git_rules_are_denied_even_to_the_main_session(command):
    decision, reason = guard.evaluate(payload(tool_input={"command": command}))
    assert decision == "deny"
    assert "hard rule" in reason.lower()


@pytest.mark.parametrize("name", sorted(guard.GOVERNING_DOCUMENTS))
def test_governing_documents_ask_main_and_deny_subagents(tmp_path, monkeypatch, name):
    monkeypatch.setenv("CLAUDE_PROJECT_DIR", str(tmp_path))
    target = tmp_path / name
    main = guard.evaluate(payload("Write", {"file_path": str(target)}, cwd=tmp_path))
    child = guard.evaluate(
        payload("Edit", {"file_path": str(target)}, cwd=tmp_path, agent="worker")
    )
    assert main and main[0] == "ask"
    assert child and child[0] == "deny"


def test_nested_readme_is_not_a_governing_document(tmp_path, monkeypatch):
    monkeypatch.setenv("CLAUDE_PROJECT_DIR", str(tmp_path))
    target = tmp_path / "pipeline" / "README.md"
    assert guard.evaluate(payload("Write", {"file_path": str(target)}, cwd=tmp_path)) is None


@pytest.mark.parametrize(
    ("tool", "expected"),
    [
        ("mcp__github__list_pull_requests", None),
        ("mcp__drive__search", None),
        ("mcp__github__create_comment", "ask"),
        ("mcp__slack__send_message", "ask"),
    ],
)
def test_mcp_tools_are_classified_by_capability(tool, expected):
    decision = guard.evaluate(payload(tool, {"query": "safe"}))
    assert (decision[0] if decision else None) == expected


def test_mutating_mcp_is_denied_to_a_subagent():
    decision = guard.evaluate(payload("mcp__github__update_issue", {"number": 3}, agent="auditor"))
    assert decision and decision[0] == "deny"


def test_read_only_mcp_query_text_is_not_mistaken_for_an_http_method():
    decision = guard.evaluate(payload("mcp__drive__search", {"query": "deleted post"}))
    assert decision is None


def test_neutral_mcp_http_tool_asks_on_a_structured_mutating_method():
    decision = guard.evaluate(
        payload("mcp__http__request", {"request": {"method": "POST", "url": "https://x"}})
    )
    assert decision and decision[0] == "ask"


def test_cli_emits_claude_hook_json():
    raw = json.dumps(payload(tool_input={"command": "git push origin work/topic"}))
    result = subprocess.run(
        [sys.executable, str(SCRIPT)],
        input=raw,
        text=True,
        capture_output=True,
        check=False,
        env={**os.environ, "CLAUDE_PROJECT_DIR": str(SCRIPT.parents[2])},
    )
    assert result.returncode == 0
    output = json.loads(result.stdout)
    assert output["hookSpecificOutput"]["permissionDecision"] == "ask"


def test_cli_starts_with_system_python_when_available():
    system_python = Path("/usr/bin/python3")
    if not system_python.exists():
        pytest.skip("no system Python")
    raw = json.dumps(payload(tool_input={"command": "git status --short"}))
    result = subprocess.run(
        [str(system_python), str(SCRIPT)],
        input=raw,
        text=True,
        capture_output=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout == ""


def test_cli_fails_closed_on_malformed_input():
    result = subprocess.run(
        [sys.executable, str(SCRIPT)],
        input="{",
        text=True,
        capture_output=True,
        check=False,
    )
    assert result.returncode == 2
    assert result.stdout == ""
    assert "could not inspect" in result.stderr
