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
        # -n is --dry-run here, not --no-verify; bundled spellings included.
        "git clean -n",
        "git clean -nd",
        'git commit -am "routine change"',
        # A wrapper prefix must not turn a harmless command into a finding.
        "nohup git status",
        "timeout 30 git log --oneline",
        # The scratch exemption still applies when scratch is the only target.
        "rm -rf workbench/scratch",
        "rm -rf workbench/scratch/",
        "rm -rf ./workbench/scratch/old-run",
        "rm -rf workbench/scratch/a workbench/scratch/b",
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
        # Naming scratch must not disarm the check for every other operand.
        "rm -rf workbench/scratch ~",
        "rm -rf workbench/scratch $HOME",
        "/bin/rm -rf workbench/scratch /Users/tyrel",
        "rm -rf workbench/scratch ../../elsewhere",
        # A delete hidden behind a separator, a wrapper, or a shell payload.
        "echo tidying\nrm -rf ~",
        "sh -c 'rm -rf ~'",
        "nohup rm -rf /",
        # Other anchored checks that shared the newline defect.
        "cd /tmp\nssh pod.example reboot",
        "cd /tmp\nenv",
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
        "cd /tmp\ncurl --data x=1 https://example.test",
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
        # A newline is a command separator; without normalization the guard
        # saw only line one and everything after it was invisible.
        "cd /Users/tyrel/verbatus_alpha\ngit push --force origin main",
        "true\ngit push origin main",
        # A backslash continuation is the opposite case: one command, two lines.
        "git push \\\n  origin main",
        # Wrapper verbs this project uses for detached work.
        "nohup git push origin main",
        "timeout 60 git push origin main",
        "nice -n 10 git push origin main",
        "setsid git push origin main",
        # Shell payloads are inspected, not treated as opaque text.
        "bash -c 'git push origin main'",
        'sh -c "git push origin main"',
        # An unparseable tail must not delete the invocation from the list.
        'git push origin main #"',
        'git push origin main "x|y"',
        # -n is git-commit's short --no-verify.
        'git commit -n -m "message"',
        'git commit -nm "message"',
        # Config injected through the environment reaches the -c layer.
        "GIT_CONFIG_COUNT=1 GIT_CONFIG_KEY_0=core.hooksPath "
        "GIT_CONFIG_VALUE_0=/dev/null git commit -m 'message'",
        "GIT_CONFIG_PARAMETERS='core.hooksPath=/dev/null' git commit -m 'message'",
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


# The command strings above record which bypasses were closed. These pin the
# normalization itself, so a future rewrite is measured against the mechanism
# rather than against fifteen strings it could special-case one at a time.


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("git status", "git status"),
        ("one\ntwo", "one;two"),
        ("one \\\ntwo", "one two"),
        ("echo 'a\nb'", "echo 'a\nb'"),
        ('echo "a\nb"', 'echo "a\nb"'),
    ],
)
def test_flatten_separates_commands_without_touching_quoted_newlines(raw, expected):
    assert guard.flatten_command(raw) == expected


def test_expand_appends_a_shell_payload_as_its_own_command():
    assert guard.expand_command("sh -c 'rm -rf ~'").endswith(" ; rm -rf ~")


def test_expand_stops_at_the_nesting_bound():
    nested = "sh -c " + "'sh -c " * 6 + "rm" + "'" * 6
    assert guard.expand_command(nested).count("rm") <= guard.PAYLOAD_DEPTH + 2


def test_a_document_heredoc_is_data_but_one_piped_to_a_shell_is_not():
    document = "cat > notes.md <<EOF\ngit push origin main\nEOF"
    piped = "bash <<EOF\ngit push origin main\nEOF"
    assert guard.evaluate(payload(tool_input={"command": document})) is None
    assert guard.evaluate(payload(tool_input={"command": piped}))[0] == "deny"


def test_tokenize_returns_approximate_tokens_rather_than_giving_up():
    assert guard.tokenize(' push origin main #"') == ["push", "origin", "main", "#"]
    assert guard.tokenize(" push origin main") == ["push", "origin", "main"]


@pytest.mark.parametrize(
    ("operand", "expected"),
    [
        ("workbench/scratch", True),
        ("./workbench/scratch/run", True),
        ("workbench/scratch/../../etc", False),
        ("workbench/active", False),
        ("~", False),
    ],
)
def test_scratch_exemption_is_per_operand_and_resolves_traversal(operand, expected):
    assert guard.under_scratch(operand) is expected
