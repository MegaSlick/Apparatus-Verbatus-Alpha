#!/usr/bin/env python3
"""Small Claude PreToolUse tripwire for consequential actions.

This is not a shell sandbox. It recognizes obvious capabilities and either:

* denies actions a subagent may never take, plus direct-main and hook-bypass Git;
* asks Tyrel to confirm one exact destructive, paid, external, or owned-branch
  history-rewrite action; or
* stays silent and leaves Claude Code's normal permission flow in place.

Opaque scripts and deliberately disguised commands remain opaque. The durable
controls are narrow tool permissions, Git hooks, GitHub protection, review, and
the main session reading every integrated diff.
"""

from __future__ import annotations

import json
import os
import re
import shlex
import sys
from pathlib import Path
from typing import Any, Optional, Tuple

GOVERNING_DOCUMENTS = frozenset(
    {"claude.md", "goals.md", "governance.md", "architecture.md", "glossary.md", "readme.md"}
)
WRITING_TOOLS = frozenset({"Write", "Edit", "MultiEdit", "NotebookEdit"})
PATH_KEYS = ("file_path", "notebook_path", "path")

MUTATION_WORDS = frozenset(
    {
        "add",
        "approve",
        "archive",
        "close",
        "comment",
        "create",
        "delete",
        "deploy",
        "destroy",
        "disable",
        "edit",
        "enable",
        "invite",
        "launch",
        "merge",
        "move",
        "post",
        "publish",
        "remove",
        "rename",
        "reply",
        "resolve",
        "restart",
        "send",
        "set",
        "start",
        "stop",
        "terminate",
        "uninstall",
        "update",
        "upload",
        "write",
    }
)

Decision = Optional[Tuple[str, str]]


def subagent_name(payload: dict[str, Any]) -> str | None:
    """Return the runtime-supplied agent identity, absent on the main thread."""
    for key in ("agent_type", "agent_id"):
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            named = payload.get("agent_type")
            return named.strip() if isinstance(named, str) and named.strip() else "subagent"
    return None


def deny_or_ask(payload: dict[str, Any], reason: str) -> Decision:
    """Subagents are refused; the accountable main session must ask Tyrel."""
    agent = subagent_name(payload)
    if agent:
        return "deny", f"Subagent {agent} may not {reason}; report it to the main session."
    return (
        "ask",
        f"This would {reason}. Confirm this exact action only if you accept that consequence.",
    )


def project_root() -> Path:
    return Path(os.environ.get("CLAUDE_PROJECT_DIR") or os.getcwd()).resolve()


def worktree_belongs_to_project(directory: Path, project: Path) -> bool:
    marker = directory / ".git"
    if not marker.is_file():
        return False
    try:
        text = marker.read_text(encoding="utf-8", errors="replace")[:4096]
    except OSError:
        return False
    match = re.match(r"\s*gitdir:\s*(.+)", text)
    if not match:
        return False
    gitdir = Path(match.group(1).strip()).resolve()
    return str(gitdir).startswith(str(project / ".git" / "worktrees") + os.sep)


def written_path(tool_input: Any, payload: dict[str, Any]) -> Path | None:
    if not isinstance(tool_input, dict):
        return None
    raw = next((tool_input[key] for key in PATH_KEYS if key in tool_input), None)
    if not isinstance(raw, str) or not raw.strip():
        return None
    expanded = os.path.expandvars(os.path.expanduser(raw.strip()))
    if os.path.isabs(expanded):
        return Path(expanded).resolve()
    cwd = payload.get("cwd")
    base = Path(cwd) if isinstance(cwd, str) and cwd else project_root()
    return (base / expanded).resolve()


def governing_write(tool: str, tool_input: Any, payload: dict[str, Any]) -> Decision:
    if tool not in WRITING_TOOLS:
        return None
    target = written_path(tool_input, payload)
    if target is None:
        return "deny", "The repository guard could not determine the write destination."
    if target.name.strip().lower() not in GOVERNING_DOCUMENTS:
        return None

    project = project_root()
    parent = target.parent.resolve()
    if parent != project and not worktree_belongs_to_project(parent, project):
        return None
    agent = subagent_name(payload)
    if agent:
        return (
            "deny",
            f"Hard rule 10 bars subagent {agent} from editing governing document {target.name}; "
            "propose exact wording to the main session.",
        )
    return (
        "ask",
        f"{target.name} governs later sessions. Tyrel must approve its exact policy change; "
        "confirm that this edit is the wording he directed.",
    )


def has(command: str, pattern: str) -> bool:
    return re.search(pattern, command, flags=re.IGNORECASE | re.DOTALL) is not None


def flatten_command(text: str) -> str:
    """Rewrite unquoted newlines as `;` and join backslash continuations.

    Every anchored check below recognizes a command at the start of the string
    or after `;`, `&` or `|`. A newline is a separator too, and Python's `^`
    without re.MULTILINE matches only offset zero — so before this existed, a
    harmless first line hid everything after it from the entire guard. Quote
    state is tracked so a newline *inside* a quoted argument stays literal.
    """
    out: list[str] = []
    quote: str | None = None
    index = 0
    length = len(text)
    while index < length:
        char = text[index]
        if quote == "'":
            out.append(char)
            if char == "'":
                quote = None
            index += 1
            continue
        if char == "\\" and index + 1 < length:
            following = text[index + 1]
            if quote is None and following == "\n":
                index += 2  # line continuation: one command, not two
                continue
            out.append(char)
            out.append(following)
            index += 2
            continue
        if quote == '"':
            out.append(char)
            if char == '"':
                quote = None
            index += 1
            continue
        if char in "'\"":
            quote = char
            out.append(char)
            index += 1
            continue
        out.append(";" if char == "\n" else char)
        index += 1
    return "".join(out)


SHELL_PAYLOAD = re.compile(
    r"\b(?:(?:ba|z|k|da|a)?sh|eval)\b(?:\s+-[A-Za-z]+)*\s+"
    r"(?P<quote>['\"])(?P<body>(?:\\.|(?!(?P=quote)).)*)(?P=quote)",
    flags=re.IGNORECASE | re.DOTALL,
)
PAYLOAD_DEPTH = 3


def expand_command(command: str, depth: int = 0) -> str:
    """Flatten separators, then append any `sh -c` / `eval` payload as its own command.

    `sh -c 'rm -rf ~'` is not a disguise, it is ordinary shell, and treating the
    payload as opaque text meant every check inside it was skipped. Appending the
    body rather than substituting it keeps the outer text intact for the
    unanchored checks. Depth is bounded so a nested quine cannot recurse away.
    """
    flat = flatten_command(command)
    if depth >= PAYLOAD_DEPTH:
        return flat
    payloads = [
        expand_command(match.group("body"), depth + 1)
        for match in SHELL_PAYLOAD.finditer(flat)
        if match.group("body").strip()
    ]
    return flat + " ; " + " ; ".join(payloads) if payloads else flat


# Prefixes that may sit between a separator and the command being recognized.
# `nohup`, `setsid`, `nice` and `timeout` are how this project launches detached
# work, so they are ordinary usage rather than evasion — and before they were
# listed here, every one of them made the command behind them invisible.
WRAPPER_PREFIX = (
    r"(?:(?:[A-Za-z_][A-Za-z0-9_]*=[^\s;&|]+)\s+|"
    r"(?:command|exec|rtk|nohup|setsid)\s+|"
    r"(?:nice|timeout|stdbuf|ionice|time)"
    r"(?:\s+-{1,2}[A-Za-z][A-Za-z0-9-]*(?:=\S+)?|\s+\d+(?:\.\d+)?[smhd]?)*\s+|"
    r"sudo(?:(?:\s+(?:-u|--user|-g|--group)\s+\S+)|"
    r"(?:\s+(?:--user|--group)=\S+)|"
    r"(?:\s+(?:-n|-E|-H|-S|-k|--non-interactive)))*\s+|"
    r"env(?:(?:\s+(?:-i|--ignore-environment))|"
    r"(?:\s+(?:-u|--unset)\s+\S+)|(?:\s+--unset=\S+)|"
    r"(?:\s+[A-Za-z_][A-Za-z0-9_]*=[^\s;&|]+))*\s+)*"
)

GIT_INVOCATION = re.compile(
    r"(?:^|[;&|]\s*)\s*" + WRAPPER_PREFIX + r"(?:[^\s;&|]*/)?git\b(?P<tail>[^\n;&|]*)",
    flags=re.IGNORECASE,
)
RM_INVOCATION = re.compile(
    r"(?:^|[;&|]\s*)\s*" + WRAPPER_PREFIX + r"(?:[^\s;&|]*/)?rm\b(?P<tail>[^\n;&|]*)",
    flags=re.IGNORECASE,
)
# core.hooksPath supplied through git's environment-configuration variables
# reaches exactly the layer `-c core.hooksPath=` writes to. The invocation regex
# above deliberately consumes leading assignments, so they never reach the token
# list; this is matched against the whole command instead.
GIT_CONFIG_HOOKS_ENV = re.compile(
    r"\bGIT_CONFIG(?:_KEY_\d+|_PARAMETERS)\s*=\s*['\"]?[^\s'\"]*core\.hookspath",
    flags=re.IGNORECASE,
)
GIT_GLOBAL_VALUE_OPTIONS = frozenset(
    {"-c", "-C", "--config-env", "--git-dir", "--work-tree", "--namespace"}
)


def tokenize(tail: str) -> list[str]:
    """Split a command tail, falling back to a permissive split rather than giving up.

    GOVERNANCE.md 10: a check that cannot run is a failure, not a pass. `shlex`
    raises on an unbalanced quote — which bash itself may never see, because
    `git push origin main #"` is a comment to bash and an unterminated string to
    `shlex`. Dropping the invocation there let every hard denial be skipped by
    one stray quote, so an unparseable tail now yields approximate tokens and is
    judged on those. Over-recognizing a malformed command is the safe direction.
    """
    try:
        return shlex.split(tail, posix=True)
    except ValueError:
        return re.findall(r"[^\s]+", tail.replace("'", " ").replace('"', " "))


def git_calls(command: str) -> list[tuple[str, list[str], list[str]]]:
    """Return recognizable direct Git calls as (action, arguments, all tokens)."""
    calls = []
    for match in GIT_INVOCATION.finditer(command):
        tokens = tokenize(match.group("tail"))
        index = 0
        while index < len(tokens):
            token = tokens[index]
            if token == "--":
                index += 1
                break
            if token in GIT_GLOBAL_VALUE_OPTIONS:
                index += 2
                continue
            if token.startswith(("-c", "-C")) and len(token) > 2:
                index += 1
                continue
            if token.startswith(("--config-env=", "--git-dir=", "--work-tree=", "--namespace=")):
                index += 1
                continue
            if token.startswith("-"):
                index += 1
                continue
            break
        if index < len(tokens):
            calls.append((tokens[index].lower(), tokens[index + 1 :], tokens))
    return calls


def git_action(command: str, action: str) -> bool:
    return any(found == action for found, _arguments, _tokens in git_calls(command))


def hooks_path_bypass(action: str, arguments: list[str], tokens: list[str]) -> bool:
    lowered = [token.lower() for token in tokens]
    for index, token in enumerate(lowered):
        if token == "-c" and index + 1 < len(lowered):
            if lowered[index + 1].startswith("core.hookspath="):
                return True
        if token.startswith("-ccore.hookspath="):
            return True
        if token == "--config-env" and index + 1 < len(lowered):
            if lowered[index + 1].startswith("core.hookspath="):
                return True
        if token.startswith("--config-env=core.hookspath="):
            return True

    if action != "config":
        return False
    lowered_arguments = [argument.lower() for argument in arguments]
    key_indexes = [
        index for index, token in enumerate(arguments) if token.lower() == "core.hookspath"
    ]
    if (
        key_indexes
        and bool({"set", "unset"}.intersection(lowered_arguments))
        or "remove-section" in lowered_arguments
        and "core" in lowered_arguments
        or "rename-section" in lowered_arguments
        and "core" in lowered_arguments
    ):
        return True
    if not key_indexes:
        return False
    mutation_flags = {
        "--add",
        "--replace-all",
        "--unset",
        "--unset-all",
        "--rename-section",
        "--remove-section",
    }
    return bool(mutation_flags.intersection(arguments)) or any(
        arguments[index + 1 :] for index in key_indexes
    )


def push_targets_main(arguments: list[str]) -> bool:
    for raw in arguments:
        token = raw.lstrip("+")
        if token.startswith("-"):
            continue
        target = token.split(":", 1)[1] if ":" in token else token
        if target in {"main", "refs/heads/main"}:
            return True
    return False


def bundled_short_flag(arguments: list[str], letter: str) -> bool:
    """True when a short flag carrying `letter` appears, bundled or alone."""
    for token in arguments:
        if token.startswith("--") or not token.startswith("-") or len(token) < 2:
            continue
        if letter in token[1:]:
            return True
    return False


def skips_commit_hooks(action: str, arguments: list[str]) -> bool:
    """`-n` is git-commit's short --no-verify — and only git-commit's.

    Kept deliberately narrow: `-n` means --dry-run for push, clean and checkout,
    --no-stat for merge, and --no-commit for revert and cherry-pick. Treating it
    as --no-verify everywhere would refuse a pile of harmless commands, which is
    how a real alarm gets tuned out.
    """
    return action == "commit" and bundled_short_flag(arguments, "n")


def hard_git_denial(command: str) -> str | None:
    calls = git_calls(command)
    if calls and GIT_CONFIG_HOOKS_ENV.search(command):
        return "bypassing repository Git hooks is outside the allowed workflow"
    for action, arguments, tokens in calls:
        if any(token == "--no-verify" or token.startswith("--no-verify=") for token in tokens):
            return "bypassing repository Git hooks is outside the allowed workflow"
        if skips_commit_hooks(action, arguments):
            return "bypassing repository Git hooks is outside the allowed workflow"
        if hooks_path_bypass(action, arguments, tokens):
            return "bypassing repository Git hooks is outside the allowed workflow"
        if action not in {"push", "send-pack"}:
            continue
        if push_targets_main(arguments):
            return "main may move only through a pull-request merge"
    return None


def risky_git(command: str, payload: dict[str, Any]) -> Decision:
    hard = hard_git_denial(command)
    if hard:
        return "deny", f"Blocked by repository hard rule: {hard}."

    for action, arguments, _tokens in git_calls(command):
        if action in {"push", "send-pack"}:
            rewriting = any(
                token in {"-f", "--force", "--force-with-lease", "--force-if-includes"}
                or token.startswith(("--force=", "--force-with-lease=", "--force-if-includes="))
                or token.startswith("+")
                for token in arguments
            )
            if rewriting:
                return deny_or_ask(
                    payload,
                    "rewrite published history; hard rule 5 forbids this unless the "
                    "branch is exclusively yours",
                )
            return deny_or_ask(payload, "publish commits or refs to a remote repository")
        if action == "merge":
            return deny_or_ask(payload, "merge histories, which Tyrel reserves to himself")
        if action == "rebase" or (action == "commit" and "--amend" in arguments):
            return deny_or_ask(payload, "rewrite local commit history")
        restore_staged = "--staged" in arguments or any(
            token.startswith("-") and not token.startswith("--") and "S" in token[1:]
            for token in arguments
        )
        restore_worktree = "--worktree" in arguments or any(
            token.startswith("-") and not token.startswith("--") and "W" in token[1:]
            for token in arguments
        )
        destructive = (
            action == "reset"
            and "--hard" in arguments
            or action == "restore"
            and (restore_worktree or not restore_staged)
            or action == "checkout"
            or action == "clean"
            and "--dry-run" not in arguments
            and not bundled_short_flag(arguments, "n")
            or action == "branch"
            and bool({"-D", "--delete", "--force"}.intersection(arguments))
            or action == "stash"
            and bool({"drop", "clear"}.intersection(argument.lower() for argument in arguments))
            or action == "worktree"
            and "remove" in arguments
            and "--force" in arguments
        )
        if destructive:
            return deny_or_ask(payload, "discard or make work difficult to recover")
    if has(command, r"\bgh\s+pr\s+merge\b"):
        return deny_or_ask(payload, "merge histories, which Tyrel reserves to himself")
    return None


def rm_operands(command: str) -> list[str]:
    """Every non-flag operand of every recognizable rm in the command."""
    operands: list[str] = []
    for match in RM_INVOCATION.finditer(command):
        operands.extend(
            token for token in tokenize(match.group("tail")) if not token.startswith("-")
        )
    return operands


def under_scratch(operand: str) -> bool:
    cleaned = operand.strip().strip("\"'")
    if cleaned.startswith("./"):
        cleaned = cleaned[2:]
    return cleaned == "workbench/scratch" or cleaned.startswith("workbench/scratch/")


def protected_delete(command: str, payload: dict[str, Any]) -> Decision:
    if not RM_INVOCATION.search(command):
        return None
    # The scratch exemption is per-operand, not per-command. Testing whether the
    # string merely *mentions* scratch meant `rm -rf workbench/scratch ~` was
    # waved through with the home directory attached to it.
    operands = rm_operands(command)
    if operands and all(under_scratch(operand) for operand in operands):
        return None
    broad = has(command, r"\brm\b[^\n;&|]*(?:-[^\s]*r[^\s]*f|-[^\s]*f[^\s]*r)")
    protected = has(
        command,
        r"(?:^|[\s\"'])(?:/|~|\$HOME|(?:\./)?(?:\.git|\.githooks|\.claude|"
        r"workbench|private)|/[^\s\"']*/(?:\.git|\.githooks|\.claude|"
        r"workbench|private))(?:[/\s\"']|$)",
    )
    if broad or protected:
        return deny_or_ask(payload, "delete data recursively or from a protected repository area")
    return None


def governing_shell_write(command: str, payload: dict[str, Any]) -> Decision:
    if not has(command, r"\b(?:CLAUDE|GOALS|GOVERNANCE|ARCHITECTURE|GLOSSARY|README)\.md\b"):
        return None
    if not has(command, r"(?:>|>>|\b(?:tee|mv|cp|sed\s+-i|perl\s+-i|patch)\b)"):
        return None
    agent = subagent_name(payload)
    if agent:
        return "deny", f"Hard rule 10 bars subagent {agent} from rewriting governing documents."
    return deny_or_ask(payload, "rewrite a governing document from the shell")


def credential_disclosure(command: str, payload: dict[str, Any]) -> Decision:
    secret_path = has(
        command,
        r"(?:private/(?:ntfy|workcopy)\.conf|(?:^|[/\s])\.env(?:[.\s/]|$)|credentials(?:\.json)?|id_(?:rsa|ed25519))",
    )
    secret_env = has(
        command,
        r"(?:\$(?:\{)?[A-Z0-9_]*(?:TOKEN|SECRET|PASSWORD|API_KEY|PRIVATE_KEY)(?:\})?|\b(?:printenv|export\s+-p)\b)",
    )
    environment_dump = has(
        command,
        r"(?:^|[;&|]\s*)\s*(?:env(?:\s+-0)?|set|declare\s+-p(?:\s+\S+)*)\s*(?=$|[;&|])",
    )
    if secret_path or secret_env or environment_dump:
        return deny_or_ask(
            payload, "place credential-shaped material in a transcript or child process"
        )
    return None


def external_shell_mutation(command: str, payload: dict[str, Any]) -> Decision:
    runpod_change = has(
        command,
        r"\brunpodctl\b[^\n;&|]*\b(?:create|start|deploy|dev|remove|delete|stop|terminate)\b",
    ) or has(
        command,
        r"\brunpod\.(?:create_pod|resume_pod|start_pod|create_endpoint|"
        r"create_template|delete_network_volume)\s*\(",
    )
    runpod_shutdown_only = has(
        command, r"\brunpodctl\b[^\n;&|]*\b(?:stop|terminate)\b"
    ) and not has(command, r"\brunpodctl\b[^\n;&|]*\b(?:create|start|deploy|dev|remove|delete)\b")
    if runpod_change and not (runpod_shutdown_only and not subagent_name(payload)):
        return deny_or_ask(payload, "change paid RunPod infrastructure")
    if has(command, r"(?:^|[;&|]\s*)\s*ssh\b"):
        return deny_or_ask(payload, "run an opaque command on another machine")
    if has(command, r"\b(?:curl|wget|https?)\b") and has(
        command,
        r"(?:\s-X\s*(?:POST|PUT|PATCH|DELETE)\b|--request[=\s]+(?:POST|PUT|PATCH|DELETE)\b|"
        r"--data(?:-binary|-raw|-urlencode)?\b|(?:^|\s)(?-i:-[dTF]\S+)|"
        r"(?:^|\s)-d(?:\s|$)|"
        r"(?:^|\s)(?:-T|--upload-file|-F|--form)(?:[=\s]|$)|"
        r"(?:^|\s)--json(?:[=\s]|$)|"
        r"--post-(?:data|file)(?:[=\s]|$)|--method[=\s]+(?:POST|PUT|PATCH|DELETE)\b|"
        r"--body-(?:data|file)(?:[=\s]|$)|"
        r"\bhttps?\s+(?:POST|PUT|PATCH|DELETE)\b)",
    ):
        return deny_or_ask(payload, "send a state-changing network request")
    if has(
        command,
        r"\bgh\s+(?:pr\s+(?:create|close|comment|edit|merge|ready|reopen|review)|"
        r"issue\s+(?:close|comment|create|edit|reopen)|"
        r"repo\s+(?:archive|create|delete|edit|rename)|"
        r"release\s+(?:create|delete|edit|upload))\b",
    ):
        return deny_or_ask(payload, "change GitHub state visible to other people")
    if has(command, r"\bgh\s+api\b") and has(
        command, r"(?:--method|-X)[=\s]+(?:POST|PUT|PATCH|DELETE)\b|(?:^|\s)-[fF]\s"
    ):
        return deny_or_ask(payload, "send a state-changing GitHub API request")
    return None


def bash_decision(tool_input: Any, payload: dict[str, Any]) -> Decision:
    if not isinstance(tool_input, dict) or not isinstance(tool_input.get("command"), str):
        return "deny", "The repository guard could not read the Bash command."
    command = expand_command(tool_input["command"])
    for check in (
        risky_git,
        protected_delete,
        governing_shell_write,
        credential_disclosure,
        external_shell_mutation,
    ):
        found = check(command, payload)
        if found:
            return found
    if has(command, r"\b(?:chmod|mv|rm)\b[^\n;&|]*(?:\.githooks/|\.git/hooks/)"):
        return deny_or_ask(payload, "disable or remove an installed Git hook")
    return None


def has_mutating_method(value: Any) -> bool:
    if isinstance(value, dict):
        for key, item in value.items():
            if (
                str(key).lower() in {"method", "http_method", "request_method"}
                and isinstance(item, str)
                and item.lower() in {"post", "put", "patch", "delete"}
            ):
                return True
            if has_mutating_method(item):
                return True
    if isinstance(value, list):
        return any(has_mutating_method(item) for item in value)
    return False


def mcp_decision(tool: str, tool_input: Any, payload: dict[str, Any]) -> Decision:
    lowered = tool.lower()
    segments = {part for part in re.split(r"[^a-z0-9]+", lowered) if part}
    mutating_method = has_mutating_method(tool_input)
    named_mutation = bool(segments & MUTATION_WORDS)
    runpod_mutation = "runpod" in lowered and bool(
        segments & {"create", "start", "deploy", "remove", "delete", "stop", "terminate"}
    )
    runpod_shutdown_only = (
        "runpod" in lowered
        and bool(segments & {"stop", "terminate"})
        and not bool(segments & {"create", "start", "deploy", "remove", "delete"})
    )
    if runpod_shutdown_only and not subagent_name(payload):
        return None
    if named_mutation or mutating_method or runpod_mutation:
        return deny_or_ask(payload, f"call external mutation tool {tool}")
    return None


def evaluate(payload: dict[str, Any]) -> Decision:
    tool = payload.get("tool_name")
    if not isinstance(tool, str) or not tool:
        return "deny", "The repository guard received no readable tool name."
    tool_input = payload.get("tool_input")
    direct = governing_write(tool, tool_input, payload)
    if direct:
        return direct
    if tool == "Bash":
        return bash_decision(tool_input, payload)
    if tool.startswith("mcp__"):
        return mcp_decision(tool, tool_input, payload)
    return None


def emit(decision: str, reason: str) -> None:
    print(
        json.dumps(
            {
                "hookSpecificOutput": {
                    "hookEventName": "PreToolUse",
                    "permissionDecision": decision,
                    "permissionDecisionReason": reason,
                }
            }
        )
    )


def main() -> int:
    try:
        payload = json.load(sys.stdin)
        if not isinstance(payload, dict):
            raise ValueError("payload is not an object")
        decision = evaluate(payload)
    except Exception as error:
        print(
            f"repository guard could not inspect the tool call: {type(error).__name__}",
            file=sys.stderr,
        )
        return 2
    if decision:
        emit(*decision)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
