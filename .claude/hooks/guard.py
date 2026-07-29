#!/usr/bin/env python3
"""Small Claude PreToolUse tripwire for consequential actions.

It recognizes obvious capabilities and either:

* denies actions a subagent may never take, plus direct-main and hook-bypass Git;
* asks Tyrel to confirm one exact destructive, paid, external, or owned-branch
  history-rewrite action; or
* stays silent and leaves Claude Code's normal permission flow in place.

**What it reads, and where that stops.** This is not a shell sandbox, but it is
no longer a plain text match either, and the boundary is worth stating exactly.
It normalizes a command before deciding: unquoted newlines become separators,
backslash continuations are joined, a quoted `sh -c` or `eval` payload is
expanded and inspected to a bounded depth, and a heredoc body is treated as the
data it is — unless a shell receives it, in which case it is inspected too. It
errs toward over-recognizing, so a tail it cannot tokenize is judged on
approximate tokens rather than skipped.

Heredocs are found by a quote-aware scanner rather than a pattern; see
`heredoc_declarations` for what that does and does not open.

It does **not** understand command substitution, process substitution, or a
wrapper command outside the list below. Comments are recognized only by that
heredoc scanner; every other check still reads a `#` line as ordinary text.
Anything it fails to recognize passes silently.

The durable controls remain narrow tool permissions, Git hooks, GitHub
protection, review, and the main session reading every integrated diff.
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
    if "\n" not in text:
        return text  # nothing to flatten, and this is the overwhelming majority
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


# Any command may be written path-qualified: `/bin/bash` is `bash`, `/usr/bin/env`
# is `env`. One definition, used by the shell patterns here and by `invocation()`.
_PATH = r"(?:[^\s;&|]*/)?"
_SHELL_NAME = r"(?:(?:ba|z|k|da|a)?sh|eval)"

SHELL_PAYLOAD = re.compile(
    # A shell at a command position, then whatever options precede its `-c`
    # payload. Accepting only bundled short flags meant
    # `bash --noprofile -c "git push --no-verify origin main"` was never looked
    # inside; long options, options that take a value, and `/bin/bash` all count.
    rf"(?:^|[\s;&|(]){_PATH}{_SHELL_NAME}\b"
    # Unbounded is safe here, and the reason is worth stating because the obvious
    # worry is wrong: an option must begin with `-` and an option's value may
    # not, so the two alternatives are disjoint on their first character. Every
    # token therefore has exactly one role, there is nothing for the engine to
    # try two ways, and no combination to explore. A counted bound would only
    # cap how many options may precede `-c` before the payload stops being read.
    r"(?:\s+(?:--[A-Za-z][A-Za-z0-9-]*|-[A-Za-z]+)"
    r"(?:\s+[A-Za-z0-9_,=.:/+][A-Za-z0-9_,=.:/+-]*)?)*"
    # The two body alternatives are kept disjoint for the same reason — a
    # backslash may only be consumed by the escape branch.
    r"\s+(?P<quote>['\"])(?P<body>(?:\\.|(?!(?P=quote))[^\\])*)(?P=quote)",
    flags=re.IGNORECASE | re.DOTALL,
)
PAYLOAD_DEPTH = 3


SHELL_VERB = re.compile(rf"\b{_SHELL_NAME}\b", flags=re.IGNORECASE)
HEREDOC_DELIMITER = re.compile(
    r"\s*(?:(?P<quote>['\"])(?P<quoted>[^'\"]*)(?P=quote)|(?P<bare>[A-Za-z_][A-Za-z0-9_]*))"
)


def heredoc_declarations(line: str, quote: str | None) -> tuple[list[str], str | None]:
    """Tags this line opens a heredoc for, and the quote state it leaves behind.

    Quote-aware deliberately. A global regex over raw text cannot tell a heredoc
    operator from the same three characters inside a quoted argument, after a
    comment, or in the here-string operator `<<<` — and each of those mistakes
    ends the same way, with the following line eaten as body and skipped by every
    check in this file. Two independent reviews found that hole by two different
    spellings on the same day.
    """
    tags: list[str] = []
    index = 0
    length = len(line)
    while index < length:
        char = line[index]
        if quote:
            if char == "\\" and quote == '"' and index + 1 < length:
                index += 2
                continue
            if char == quote:
                quote = None
            index += 1
            continue
        if char == "\\":
            index += 2
            continue
        if char in "'\"":
            quote = char
            index += 1
            continue
        if char == "#" and (index == 0 or line[index - 1].isspace()):
            break  # a comment: bash will not execute the rest of this line
        if line.startswith("<<", index):
            if line.startswith("<<<", index):
                index += 3  # a here-string feeds one word, and opens no body
                continue
            cursor = index + 2
            if cursor < length and line[cursor] == "-":
                cursor += 1  # `<<-` strips leading tabs from the terminator
            found = HEREDOC_DELIMITER.match(line, cursor)
            if not found:
                # A delimiter this scanner cannot read (`<<$TAG`) is left alone.
                # Consuming lines we cannot reliably terminate is precisely how a
                # command disappears, so an unknown spelling opens nothing.
                index = cursor
                continue
            tags.append(found.group("quoted") or found.group("bare"))
            index = found.end()
            continue
        index += 1
    return tags, quote


def split_heredocs(text: str) -> tuple[str, list[str]]:
    """Lift heredoc bodies out of the command, returning the rest and the bodies.

    A heredoc body is stdin data, not commands: `cat <<EOF` containing the words
    `git push origin main` is a document, and reading it as a command produced an
    unappealable refusal on an ordinary write. Bodies are returned separately so
    the caller can decide — they are commands only when a shell receives them.
    An unterminated heredoc opens nothing; `heredoc_declarations` says why.
    """
    if "<<" not in text:
        # Nothing here can open one, so `kept` would rejoin to exactly `text`.
        # One C-level substring search instead of a Python character loop, on a
        # path that runs before every tool call the agent makes.
        return text, []
    lines = text.split("\n")
    kept: list[str] = []
    bodies: list[str] = []
    quote: str | None = None
    index = 0
    while index < len(lines):
        line = lines[index]
        kept.append(line)
        index += 1
        tags, quote = heredoc_declarations(line, quote)
        if not tags:
            continue
        fed_to_a_shell = bool(SHELL_VERB.search(line))
        for tag in tags:
            end = index
            while end < len(lines) and lines[end].strip() != tag:
                end += 1
            if end >= len(lines):
                continue  # no terminator: those lines stay commands
            if fed_to_a_shell:
                bodies.append("\n".join(lines[index:end]))
            index = end + 1  # skip the body and its terminator line
    return "\n".join(kept), bodies


def expand_command(command: str, depth: int = 0) -> str:
    """Flatten separators, then append any `sh -c` / `eval` payload as its own command.

    `sh -c 'rm -rf ~'` is not a disguise, it is ordinary shell, and treating the
    payload as opaque text meant every check inside it was skipped. Appending the
    body rather than substituting it keeps the outer text intact for the
    unanchored checks. Depth is bounded so a nested quine cannot recurse away.
    """
    outside, shell_input = split_heredocs(command)
    flat = flatten_command(outside)
    if depth >= PAYLOAD_DEPTH:
        return flat
    payloads = [
        expand_command(body, depth + 1)
        for body in ([match.group("body") for match in SHELL_PAYLOAD.finditer(flat)] + shell_input)
        if body.strip()
    ]
    return " ; ".join([flat, *payloads])


# Prefixes that may sit between a separator and the command being recognized.
# `nohup`, `setsid`, `nice` and `timeout` are how this project launches detached
# work, so they are ordinary usage rather than evasion — and before they were
# listed here, every one of them made the command behind them invisible.
#
# This list fails OPEN, which is the thing to know before editing it: a wrapper
# that is not named here hides the command behind it from the whole guard.
# `xargs`, `doas`, `su -c`, `flock`, `script` and `taskset` are not covered.
# `FOO= git push …` is valid shell and assigns an empty value, so the value part
# is `*` rather than `+`. Requiring one character meant the assignment was not
# recognized as a prefix at all, the Git call behind it was never seen, and a
# direct `--no-verify` push to main passed the guard in silence.
_ASSIGNMENT = r"[A-Za-z_][A-Za-z0-9_]*=[^\s;&|]*"
_PLAIN_WRAPPER = rf"{_PATH}(?:command|exec|rtk|nohup|setsid)\s+"
_MEASURED_WRAPPER = (
    rf"{_PATH}(?:nice|timeout|stdbuf|ionice|time)"
    r"(?:\s+-{1,2}[A-Za-z][A-Za-z0-9-]*(?:=\S+)?|\s+\d+(?:\.\d+)?[smhd]?)*\s+"
)
_SUDO = (
    rf"{_PATH}sudo(?:(?:\s+(?:-u|--user|-g|--group)\s+\S+)|"
    r"(?:\s+(?:--user|--group)=\S+)|"
    r"(?:\s+(?:-n|-E|-H|-S|-k|--non-interactive)))*\s+"
)
_ENV = (
    rf"{_PATH}env(?:(?:\s+(?:-i|--ignore-environment))|"
    r"(?:\s+(?:-u|--unset)\s+\S+)|(?:\s+--unset=\S+)|"
    rf"(?:\s+{_ASSIGNMENT}))*(?:\s+--)?\s+"
)
WRAPPER_PREFIX = rf"(?:{_ASSIGNMENT}\s+|{_PLAIN_WRAPPER}|{_MEASURED_WRAPPER}|{_SUDO}|{_ENV})*"


def invocation(name: str) -> re.Pattern[str]:
    """Recognize `name` at a command position, with its wrapper prefix captured."""
    return re.compile(
        r"(?:^|[;&|]\s*)\s*(?P<prefix>" + WRAPPER_PREFIX + r")"
        rf"{_PATH}{name}\b(?P<tail>[^\n;&|]*)",
        flags=re.IGNORECASE,
    )


GIT_INVOCATION = invocation("git")
RM_INVOCATION = invocation("rm")
# Configuration handed to git through its environment reaches the same layer
# `-c core.hooksPath=` writes to, and the guard cannot read a config file it is
# pointed at. Matched against the assignments attached to *this* invocation —
# scanning the whole command would fire on a variable named anywhere near an
# unrelated `git status`.
GIT_CONFIG_ASSIGNMENT = re.compile(r"\bGIT_CONFIG[A-Z0-9_]*\s*=", flags=re.IGNORECASE)
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
        return tail.replace("'", " ").replace('"', " ").split()


def git_calls(command: str) -> list[tuple[str, list[str], list[str], str]]:
    """Return recognizable Git calls as (action, arguments, all tokens, wrapper prefix)."""
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
            calls.append(
                (tokens[index].lower(), tokens[index + 1 :], tokens, match.group("prefix"))
            )
    return calls


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
    # Normalized once, here, rather than each set below carrying every spelling.
    # Git accepts an operation as a bare subcommand and as a `--`-prefixed flag,
    # so listing both doubled every set and it was easy to add one and forget the
    # other — which is exactly what happened: only `remove-section` was matched,
    # and `--remove-section` fell through to a silent `False`.
    operations = {argument.lower().lstrip("-") for argument in arguments}
    # Deleting or renaming the whole `[core]` section takes `core.hooksPath` with
    # it and switches every installed hook off — the credential scan, the document
    # policy, the branch rule, the attribution check.
    if {"remove-section", "rename-section"} & operations and "core" in operations:
        return True
    key_indexes = [
        index for index, token in enumerate(arguments) if token.lower() == "core.hookspath"
    ]
    if not key_indexes:
        return False
    if {"set", "unset", "add", "replace-all", "unset-all"} & operations:
        return True
    return any(arguments[index + 1 :] for index in key_indexes)


def push_targets_main(arguments: list[str]) -> bool:
    for raw in arguments:
        token = raw.lstrip("+")
        if token.startswith("-"):
            continue
        target = token.split(":", 1)[1] if ":" in token else token
        if target in {"main", "refs/heads/main"}:
            return True
    return False


def short_option(arguments: list[str], letters: str, action: str = "") -> bool:
    """True when any short option in `letters` is present, alone or bundled.

    `action` names the Git subcommand, and is used only to look up which of its
    short options swallow the rest of their token as a value. Naming them is the
    whole point. A generic character search over everything after a dash reads
    the `n` in `git clean -enode_modules` as `--dry-run` and waves a destructive
    clean through, and the `n` in `git commit -mno` as `--no-verify` and refuses
    an ordinary commit. Case is significant: `-D` and `-d` are different options.

    Several letters may be asked about at once, so "any of these means
    destructive" is one pass over the arguments rather than one pass per letter.
    """
    value_taking = VALUE_TAKING.get(action, "")
    for token in arguments:
        if token == "--":
            break  # a POSIX end-of-options marker; the rest are operands
        if not token.startswith("-") or token.startswith("--") or len(token) < 2:
            continue
        for character in token[1:]:
            if character in letters:
                return True
            if character in value_taking:
                break  # the rest of this token is that option's value, not flags
    return False


def long_option(arguments: list[str], *names: str) -> bool:
    """True when any of `names` appears as a long option, with or without a value."""
    for token in arguments:
        if token == "--":
            break
        if token.split("=", 1)[0] in names:
            return True
    return False


# Short options that take a value, keyed by the subcommand they belong to. A
# subcommand absent here has none, which is why the default is a declared empty
# string rather than an argument each caller has to remember to pass — forgetting
# it was silent, and produced exactly the misreading the parameter exists to stop.
VALUE_TAKING = {
    "commit": "mFcCtSu",
    "clean": "e",
    "restore": "s",
    "branch": "u",
}


def skips_commit_hooks(action: str, arguments: list[str]) -> bool:
    """`-n` is git-commit's short --no-verify — and only git-commit's.

    Kept deliberately narrow: `-n` means --dry-run for push, clean and checkout,
    --no-stat for merge, and --no-commit for revert and cherry-pick. Treating it
    as --no-verify everywhere would refuse a pile of harmless commands, which is
    how a real alarm gets tuned out.
    """
    return action == "commit" and short_option(arguments, "n", action)


HOOK_BYPASS = "bypassing repository Git hooks is outside the allowed workflow"


def hard_git_denial(calls: list[tuple[str, list[str], list[str], str]]) -> str | None:
    for action, arguments, tokens, prefix in calls:
        if (
            long_option(tokens, "--no-verify")
            or skips_commit_hooks(action, arguments)
            or hooks_path_bypass(action, arguments, tokens)
            or GIT_CONFIG_ASSIGNMENT.search(prefix)
        ):
            return HOOK_BYPASS
        if action in {"push", "send-pack"} and push_targets_main(arguments):
            return "main may move only through a pull-request merge"
    return None


def discards_work(action: str, arguments: list[str]) -> bool:
    """Whether this Git subcommand discards work or makes it hard to recover.

    One branch per subcommand, so a `git status` pays for none of them — the
    option scans below used to run for every recognized Git call and be thrown
    away for all but two of them.
    """
    if action == "reset":
        return long_option(arguments, "--hard")
    if action == "restore":
        staged = long_option(arguments, "--staged") or short_option(arguments, "S", action)
        worktree = long_option(arguments, "--worktree") or short_option(arguments, "W", action)
        return worktree or not staged
    if action == "checkout":
        return True
    if action == "clean":
        return not long_option(arguments, "--dry-run") and not short_option(arguments, "n", action)
    if action == "branch":
        # `-Df` is `git branch -D --force`; exact token membership saw neither
        # half of it and deleted an unmerged branch without asking.
        return long_option(arguments, "--delete", "--force") or short_option(
            arguments, "DdfM", action
        )
    if action == "stash":
        return bool({"drop", "clear"}.intersection(argument.lower() for argument in arguments))
    if action == "worktree":
        return "remove" in arguments and long_option(arguments, "--force")
    return False


def risky_git(command: str, payload: dict[str, Any]) -> Decision:
    calls = git_calls(command)
    hard = hard_git_denial(calls)
    if hard:
        return "deny", f"Blocked by repository hard rule: {hard}."

    for action, arguments, _tokens, _prefix in calls:
        if action in {"push", "send-pack"}:
            # Read with the same two helpers as everything else. The exact-token
            # membership this replaces missed a bundled `-fu`, which still asked
            # — but asked only to "publish", understating what was being agreed
            # to. One idiom for "is this option present" means one thing to get
            # right, and the next subcommand added here inherits it.
            rewriting = (
                long_option(arguments, "--force", "--force-with-lease", "--force-if-includes")
                or short_option(arguments, "f", action)
                or any(token.startswith("+") for token in arguments)
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
        if action == "rebase" or (action == "commit" and long_option(arguments, "--amend")):
            return deny_or_ask(payload, "rewrite local commit history")
        if discards_work(action, arguments):
            return deny_or_ask(payload, "discard or make work difficult to recover")
    if has(command, r"\bgh\s+pr\s+merge\b"):
        return deny_or_ask(payload, "merge histories, which Tyrel reserves to himself")
    return None


def rm_arguments(command: str) -> tuple[list[str], list[str]] | None:
    """Flags and operands of every recognizable rm, or None when there is no rm."""
    matches = list(RM_INVOCATION.finditer(command))
    if not matches:
        return None
    flags: list[str] = []
    operands: list[str] = []
    for match in matches:
        options_ended = False
        for token in tokenize(match.group("tail")):
            if not options_ended and token == "--":
                options_ended = True  # `rm -- -rf` deletes a file named `-rf`
                continue
            if not options_ended and token.startswith("-") and len(token) > 1:
                flags.append(token)
            else:
                operands.append(token)
    return flags, operands


# The characters a literal path may contain. Stated as what IS allowed, not as
# what is forbidden: this gates an *exemption* from the deletion guard, so an
# incomplete list must fail toward asking. A denylist of shell metacharacters
# would exempt every character nobody thought of — and `normpath` resolves a
# literal `..`, but nothing can resolve `$p`, so `p=../../..` followed by
# `rm -rf workbench/scratch/$p` deletes far outside the drawer.
LITERAL_PATH = re.compile(r"[A-Za-z0-9._/-]+")


def under_scratch(operand: str) -> bool:
    """True only for the scratch drawer itself or something genuinely inside it.

    `normpath` resolves the `./` and `..` spellings, so a traversal back out of
    scratch stops being exempt rather than reading as a scratch path. Anything
    that is not a plain literal path is refused outright rather than guessed at.
    """
    cleaned = operand.strip()
    if not LITERAL_PATH.fullmatch(cleaned):
        return False
    cleaned = os.path.normpath(cleaned)
    return cleaned == "workbench/scratch" or cleaned.startswith("workbench/scratch" + os.sep)


def protected_delete(command: str, payload: dict[str, Any]) -> Decision:
    parsed = rm_arguments(command)
    if parsed is None:
        return None
    flags, operands = parsed
    # The scratch exemption is per-operand, not per-command. Testing whether the
    # string merely *mentions* scratch meant `rm -rf workbench/scratch ~` was
    # waved through with the home directory attached to it.
    if operands and all(under_scratch(operand) for operand in operands):
        return None
    # `-R` is recursive on both BSD and GNU rm. Only lowercase bundled `-r` was
    # read, so `rm -Rf src` deleted without asking while `rm -rf src` asked.
    recursive = short_option(flags, "rR") or long_option(flags, "--recursive")
    forced = short_option(flags, "f") or long_option(flags, "--force")
    broad = recursive and forced
    protected = has(
        command,
        r"(?:^|[\s\"'])(?:/|~|\$HOME|(?:\./)?(?:\.git|\.githooks|\.claude|"
        r"workbench|private)|/[^\s\"']*/(?:\.git|\.githooks|\.claude|"
        r"workbench|private))(?:[/\s\"']|$)",
    )
    if broad or protected:
        return deny_or_ask(payload, "delete data recursively or from a protected repository area")
    return None


# Built from GOVERNING_DOCUMENTS so the Write-tool guard and the shell guard can
# never disagree about what governs. Spelled out separately, a seventh document
# would have been added to one and forgotten in the other — silently.
_GOVERNING_NAMES = "(?:{})".format(
    "|".join(re.escape(name) for name in sorted(GOVERNING_DOCUMENTS))
)
# Only a document at the repository root governs anything. `operations/README.md`
# is an ordinary file, and refusing to `grep` it under hard rule 10 was a refusal
# with no appeal against a read — and it contradicted this file's own test that a
# nested README is not a governing document. The Write-tool path resolves the
# real destination and is the control that matters; this is the coarse shell-side
# net, and it deliberately errs toward letting a path-qualified spelling through
# rather than blocking ordinary reads of nested files.
GOVERNING_SHELL_TARGET = re.compile(
    rf"(?<![\w/.-])(?:\./)?{_GOVERNING_NAMES}\b", flags=re.IGNORECASE
)
# The redirection has to name the document. A bare `>` anywhere in the command
# was enough before, so `grep heading README.md 2>/dev/null` read as a rewrite.
GOVERNING_REDIRECT = re.compile(
    rf"(?:^|[^0-9<>&])>{{1,2}}\s*(?:\./)?{_GOVERNING_NAMES}\b", flags=re.IGNORECASE
)
# In-place editors that leave no redirection behind. Not exhaustive and cannot
# be: a `python3 -c` that opens the file for writing still passes here.
GOVERNING_REWRITE_TOOL = re.compile(
    r"\b(?:tee|mv|cp|patch|ed|truncate)\b|\b(?:sed|perl)\s+-i|"
    r"\bawk\s+-i\s*inplace\b|\bdd\b[^\n;&|]*\bof=",
    flags=re.IGNORECASE,
)


def governing_shell_write(command: str, payload: dict[str, Any]) -> Decision:
    if not GOVERNING_SHELL_TARGET.search(command):
        return None
    if not (GOVERNING_REDIRECT.search(command) or GOVERNING_REWRITE_TOOL.search(command)):
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
    runpod_ctl_change = has(
        command,
        r"\brunpodctl\b[^\n;&|]*\b(?:create|start|deploy|dev|remove|delete|stop|terminate)\b",
    )
    runpod_api_change = has(
        command,
        r"\brunpod\.(?:create_pod|resume_pod|start_pod|create_endpoint|"
        r"create_template|delete_network_volume)\s*\(",
    )
    runpod_change = runpod_ctl_change or runpod_api_change
    # Every Python-API call recognized above starts or creates something, so its
    # presence disqualifies the shutdown exemption outright. Deriving that
    # exemption from the `runpodctl` text alone let one half of a compound
    # command cancel the warning for the other, and
    # `runpodctl stop pod abc; python3 -c 'import runpod; runpod.create_pod()'`
    # started a pod that bills by the hour with the guard silent.
    runpod_shutdown_only = (
        runpod_ctl_change
        and not runpod_api_change
        and has(command, r"\brunpodctl\b[^\n;&|]*\b(?:stop|terminate)\b")
        and not has(command, r"\brunpodctl\b[^\n;&|]*\b(?:create|start|deploy|dev|remove|delete)\b")
    )
    if runpod_change and not (runpod_shutdown_only and not subagent_name(payload)):
        return deny_or_ask(payload, "change paid RunPod infrastructure")
    if has(command, r"(?:^|[;&|]\s*)\s*ssh\b"):
        return deny_or_ask(payload, "run an opaque command on another machine")
    if has(command, r"\b(?:curl|wget|https?)\b") and has(
        command,
        r"(?:\s-X\s*(?:POST|PUT|PATCH|DELETE)\b|--request[=\s]+(?:POST|PUT|PATCH|DELETE)\b|"
        r"--data(?:-binary|-raw|-urlencode)?\b|(?:^|\s)(?-i:-[dTF]\S+)|"
        r"(?:^|\s)-d(?:\s|$)|"
        r"(?:^|\s)(?:-T|--upload-file|--form-string|-F|--form)(?:[=\s]|$)|"
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
    # `gh api` defaults to POST as soon as any field parameter is supplied, so a
    # field is as good as an explicit --method. Only whitespace-separated `-f`
    # and `-F` were recognized, which left `--field`, `--raw-field` and the
    # attached `-fbody=test` spelling posting comments without an ask.
    if has(command, r"\bgh\s+api\b") and has(
        command,
        r"(?:--method|-X)[=\s]+(?:POST|PUT|PATCH|DELETE)\b|"
        r"(?:^|\s)--(?:raw-)?field(?:[=\s]|$)|(?:^|\s)--input(?:[=\s]|$)|"
        r"(?:^|\s)-[fF]\S*(?=\s|$)",
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


CAMEL_PIECE = re.compile(r"[A-Z]+(?![a-z])|[A-Z][a-z0-9]*|[a-z0-9]+")
NAME_SEPARATOR = re.compile(r"[^A-Za-z0-9]+")


def tool_segments(tool: str) -> set[str]:
    """Every word in an MCP tool name, however the vendor cased it.

    Lowercasing before splitting on non-alphanumerics destroys the only boundary
    a camelCase name has. `mcp__runpod__createPod` collapsed to the single
    segment `createpod`, which matches no mutation word, and a pod that bills by
    the hour was created with no ask — from a subagent as much as the main
    session. camelCase is the MCP naming norm and every test here used snake.
    """
    segments: set[str] = set()
    for part in NAME_SEPARATOR.split(tool):
        segments.update(piece.lower() for piece in CAMEL_PIECE.findall(part))
    return segments


def mcp_decision(tool: str, tool_input: Any, payload: dict[str, Any]) -> Decision:
    lowered = tool.lower()
    segments = tool_segments(tool)
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
