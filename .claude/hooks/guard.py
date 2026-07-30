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
data it is — unless something that *executes* it receives it, a shell or an
interpreter, in which case it is inspected too. It
errs toward over-recognizing, so a tail it cannot tokenize is judged on
approximate tokens rather than skipped.

Heredocs are found by a quote-aware scanner rather than a pattern; see
`heredoc_declarations` for what that does and does not open.

It does **not** understand command substitution, process substitution, or a
wrapper command outside the list below. Comments are recognized only by that
heredoc scanner; every other check still reads a `#` line as ordinary text.

**Where that leaves a subagent is different, deliberately.** For the main session
anything unrecognized passes silently, because precision here buys quiet and a
false alarm spends Tyrel's attention. A subagent is unattended by definition and a
false alarm against it costs nothing — it reports back and the accountable session
runs the step itself. So a subagent naming a consequential capability *through* one
of those blind spots is refused outright rather than passed; see
`subagent_blind_spot`. The thresholds differ because the cost of being wrong does.

The durable controls remain narrow tool permissions, Git hooks, GitHub
protection, review, and the main session reading every integrated diff.
"""

from __future__ import annotations

import json
import os
import re
import shlex
import sys
from datetime import datetime, timezone
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


def deny_agent_or_ask(
    payload: dict[str, Any], agent_message: str, session_message: str
) -> Decision:
    """The same branch as `deny_or_ask`, for checks whose wording differs per audience.

    `deny_or_ask` covers the common case, where one phrase reads correctly in both
    sentences. Three checks need different ones — they name a hard rule, a document,
    or the file being written — and each was hand-rolling this branch instead, which
    two reviewers flagged independently. `{agent}` in `agent_message` is filled in.

    What is shared is the *condition*, and that is the point: which audience gets
    refused rather than asked is now decided in two places in this file rather than
    five, and `subagent_name` is called once per decision rather than twice.

    **`replace`, not `format`.** Two call sites build their message with an f-string
    that also interpolates a filename, so `str.format` saw whatever braces that
    filename contained: a review reproduced `Write` to `.githooks/odd{name}.py`
    raising `KeyError` and the guard exiting 2 — failing closed, but reporting itself
    broken instead of reporting the rule, and skipping `record()` so nothing reached
    the decision log. It also left two escaping conventions, since an f-string caller
    had to write `{{agent}}` and a plain-string caller `{agent}`. A literal
    substitution is inert to braces and reads the same text from both spellings.
    """
    agent = subagent_name(payload)
    if agent:
        return "deny", agent_message.replace("{agent}", agent)
    return "ask", session_message


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
    return deny_agent_or_ask(
        payload,
        f"Hard rule 10 bars subagent {{agent}} from editing governing document {target.name}; "
        "propose exact wording to the main session.",
        f"{target.name} governs later sessions. Tyrel must approve its exact policy change; "
        "confirm that this edit is the wording he directed.",
    )


# The machinery that decides what is allowed. Shell tampering with these was
# already refused — `chmod`/`mv`/`rm` on `.githooks/`, every `core.hooksPath` and
# `--no-verify` bypass — but a plain Write or Edit to the same paths passed in
# silence, for subagents as much as the session, and three roles hold Write.
#
# What makes that worse than an ordinary code edit: `settings.json` invokes this
# file fresh from the working tree on every tool call, and `core.hooksPath` points
# at the tracked `.githooks/`, so an edit here judges the *next* action. Two quiet
# edits and one consequential command is a rogue action nobody sees at the time.
#
# Matched on the path tail rather than against the project root, so a worktree or a
# second clone is covered too. Deliberately loose: over-recognizing another
# repository's hooks costs one prompt, and this file's stated posture is to err that
# way. Not a wall — an agent can still write a script and run it, and CLAUDE.md
# keeps code open on purpose. It closes the careless path, which is most of them.
#
# `.git/config` and `.git/hooks/` are here because a review found the write-tool
# route to them wide open while every shell spelling was hard-denied. `core.hooksPath`
# lives in `.git/config`, and `install.sh` and CLAUDE.md both say it is the one
# setting that makes any hook run at all: an agent pointing it elsewhere turns off the
# credential scan, the document policy, the branch rule and the attribution check
# together, in a file that is untracked by design so no diff would ever show it.
SELF_PROTECTING_TAILS = (
    "/.claude/hooks/",
    "/.claude/settings.json",
    "/.claude/settings.local.json",
    "/.githooks/",
    "/.git/config",
    "/.git/hooks/",
)


def protects_the_guard(target: Path) -> bool:
    # One condition, not two. This was written as
    # `text.endswith(tail.rstrip("/")) or tail in text + "/"`, and a reviewer pointed
    # out the first half is subsumed by the second: if the path ends with the tail then
    # the tail is a substring of it, and appending the separator covers the bare-
    # directory case the `endswith` was there for. Two conditions doing one job.
    text = str(target) + "/"
    return any(tail in text for tail in SELF_PROTECTING_TAILS)


def inside_project_worktree(target: Path, project: Path) -> bool:
    """True when `target` sits under a git worktree belonging to this project.

    `worktree_belongs_to_project` answers the question for one directory; this walks
    up from the written path to find the worktree root, because the path being written
    is several levels below it.
    """
    for parent in target.parents:
        if worktree_belongs_to_project(parent, project):
            return True
        if parent == parent.parent:  # reached the filesystem root
            break
    return False


def harness_write(tool: str, tool_input: Any, payload: dict[str, Any]) -> Decision:
    if tool not in WRITING_TOOLS:
        return None
    target = written_path(tool_input, payload)
    if target is None or not protects_the_guard(target):
        return None

    # A worktree is where an agent is *supposed* to write this code, and refusing it
    # there was a rule written as a wall. CLAUDE.md is explicit: "Code is not on that
    # list and stays open. Hooks, CI, the agent and skill files, `operations/`, tests
    # and everything under the pipeline are written by agents and land through review
    # like anything else." Its roster gives `infra-worker` exactly this ground —
    # "hooks, CI, seals, accounting, money paths" — so denying it here left that role
    # unable to do the only work it exists for, and this file could not have been
    # written by the agent meant to write it. A reviewer caught the contradiction;
    # CLAUDE.md had already warned that a rule shaped as a wall cost this project a
    # day once.
    #
    # The protection that remains is the one the reasoning actually supports: the live
    # checkout, whose `.githooks/` runs on the next commit and whose `settings.json`
    # is re-read on the next tool call. A worktree's edits reach the main checkout only
    # through review, which is the boundary CLAUDE.md names.
    if inside_project_worktree(target, project_root()):
        return None

    return deny_agent_or_ask(
        payload,
        "Subagent {agent} may not edit "
        + target.name
        + " in the live checkout, which decides what tools are allowed to do; work in "
        "your own worktree, or propose the change to the main session.",
        f"{target.name} decides what every later tool call is allowed to do, and the next one "
        "is judged by what you are about to write. Confirm this is the change Tyrel directed.",
    )


def without_quoted_text(command: str) -> str:
    """The command with quoted spans replaced by spaces, same length throughout.

    Quoted text is data. `echo 'example: x | http y'` names no pipeline, and a commit
    message quoting one names no pipeline either — but every pattern in this file reads
    raw text, so both looked like one. `invocation()` has the same blindness and treats
    `| http` inside a string as a command position.

    This is not shell parsing and does not pretend to be: it tracks single and double
    quotes and a backslash escape, which is enough to stop prose being read as
    structure. It is deliberately *narrow* in application — used where a false alarm on
    quoted text was actually observed, not applied wholesale to checks whose current
    behaviour is proven by tests. Widening it is a change with its own review.

    Length is preserved so an offset into the result still maps to the original.
    """
    out: list[str] = []
    quote: str | None = None
    index = 0
    while index < len(command):
        char = command[index]
        if quote is None and char == "\\" and index + 1 < len(command):
            out.append(char)
            out.append(command[index + 1])
            index += 2
            continue
        if quote is None and char in "'\"":
            quote = char
            out.append(" ")
        elif quote is not None and char == quote:
            quote = None
            out.append(" ")
        elif quote is not None:
            out.append(" " if not char.isspace() else char)
        else:
            out.append(char)
        index += 1
    return "".join(out)


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


# `SHELL_VERB` used to live here and was orphaned when the heredoc gate widened to
# interpreters; a reviewer noticed the dead constant. `EXECUTES_A_HEREDOC` replaces it.
#
# A heredoc body is data unless something *executes* it, and a shell is not the only
# thing that does. A reviewer found `python3 <<'PY'` with a push inside silent to a
# subagent and to the session alike, while the same push through `python3 -c` was
# denied and `sh <<EOF` was denied — one capability, three spellings, one of them
# uncovered. The docstring at the top of this file also claimed a heredoc is inspected
# when "a shell receives it", which described the code and not the risk.
EXECUTES_A_HEREDOC = re.compile(
    rf"\b(?:{_SHELL_NAME}|python3?|perl|ruby|node|deno|php|osascript)\b",
    flags=re.IGNORECASE,
)
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
            tag = found.group("quoted") or found.group("bare")
            index = found.end()
            if not tag:
                # `<<''` put None into a list[str]. Harmless in practice —
                # a None tag matches no terminator, and split_heredocs keeps
                # unterminated lines as commands — but only by accident of
                # that loop's shape. An empty delimiter opens nothing, same
                # as `<<$TAG`, so the safety stops depending on the accident.
                continue
            tags.append(tag)
            continue
        index += 1
    return tags, quote


def split_heredocs(text: str) -> tuple[str, list[str]]:
    """Lift heredoc bodies out of the command, returning the rest and the bodies.

    A heredoc body is stdin data, not commands: `cat <<EOF` containing the words
    `git push origin main` is a document, and reading it as a command produced an
    unappealable refusal on an ordinary write. Bodies are returned separately so
    the caller can decide — they are commands when something that runs them receives
    them, a shell or an interpreter; see `EXECUTES_A_HEREDOC`.
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
        fed_to_something_that_runs_it = bool(EXECUTES_A_HEREDOC.search(line))
        for tag in tags:
            end = index
            while end < len(lines) and lines[end].strip() != tag:
                end += 1
            if end >= len(lines):
                continue  # no terminator: those lines stay commands
            if fed_to_something_that_runs_it:
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
    short options swallows the rest of its token or the following token as a
    value. Naming them is the whole point. A generic character search over
    everything after a dash reads the `n` in `git clean -enode_modules` as
    `--dry-run` and waves a destructive clean through, and the `n` in
    `git commit -mno` as `--no-verify` and refuses an ordinary commit. Case is
    significant: `-D` and `-d` are different options.

    Several letters may be asked about at once, so "any of these means
    destructive" is one pass over the arguments rather than one pass per letter.
    """
    value_taking = VALUE_TAKING.get(action, "")
    index = 0
    while index < len(arguments):
        token = arguments[index]
        if token == "--":
            break  # a POSIX end-of-options marker; the rest are operands
        if not token.startswith("-") or token.startswith("--") or len(token) < 2:
            index += 1
            continue
        for position, character in enumerate(token[1:], start=1):
            if character in letters:
                return True
            if character in value_taking:
                # The rest of this token, or the next token when there is no
                # attached text, is this option's value rather than more flags.
                if position + 1 == len(token) and index + 1 < len(arguments):
                    index += 1
                break
        index += 1
    return False


def long_option(arguments: list[str], *names: str) -> bool:
    """True when any of `names` appears as a long option, with or without a value."""
    for token in arguments:
        if token == "--":
            break
        if token.split("=", 1)[0] in names:
            return True
    return False


def option_values(
    arguments: list[str],
    short: str = "",
    *names: str,
    value_taking: str = "",
    long_value_taking: frozenset[str] = frozenset(),
) -> list[str]:
    """Return the values given to one option, in every spelling it accepts.

    `-XPOST`, `-X POST`, `--request=POST` and `--request POST` are the same
    request. Asking only whether the option is *present* is not enough here:
    `curl -X GET` is an ordinary read and `curl -X DELETE` is not, so the caller
    has to see the value. `value_taking` is the same idea as in `short_option` —
    a letter earlier in a bundle that swallows the rest of its token means the
    letter we are looking for is that value, not a separate option.
    """
    values: list[str] = []
    index = 0
    while index < len(arguments):
        token = arguments[index]
        if token == "--":
            break
        if token.startswith("--"):
            name, separator, attached = token.partition("=")
            if name in names:
                if separator:
                    values.append(attached)
                elif index + 1 < len(arguments):
                    index += 1
                    values.append(arguments[index])
            elif not separator and name in long_value_taking and index + 1 < len(arguments):
                index += 1
        elif short and token.startswith("-") and len(token) > 1:
            for position, character in enumerate(token[1:], start=1):
                if character == short:
                    attached = token[position + 1 :]
                    if attached:
                        values.append(attached)
                    elif index + 1 < len(arguments):
                        index += 1
                        values.append(arguments[index])
                    break
                if character in value_taking:
                    if position + 1 == len(token) and index + 1 < len(arguments):
                        index += 1
                    break
        index += 1
    return values


def operands(
    arguments: list[str],
    *,
    short_value_taking: str = "",
    long_value_taking: frozenset[str] = frozenset(),
) -> list[str]:
    """The non-option tokens of an argument list, in order.

    Subcommands are operands: `create` in `runpodctl create pods`, `pr comment`
    in `gh pr comment 10`. The caller names the short and long options that take
    a separate value, so `o/r` in `gh --repo o/r pr create` is skipped before
    the command pair is read.
    """
    found: list[str] = []
    past_marker = False
    index = 0
    while index < len(arguments):
        token = arguments[index]
        if token == "--" and not past_marker:
            past_marker = True
            index += 1
            continue
        if not past_marker and token.startswith("--"):
            name, separator, _attached = token.partition("=")
            if not separator and name in long_value_taking and index + 1 < len(arguments):
                index += 1
            index += 1
            continue
        if not past_marker and token.startswith("-") and len(token) > 1:
            for position, character in enumerate(token[1:], start=1):
                if character in short_value_taking:
                    if position + 1 == len(token) and index + 1 < len(arguments):
                        index += 1
                    break
            index += 1
            continue
        found.append(token)
        index += 1
    return found


# Short options that take a value, keyed by the command or subcommand they
# belong to. A command absent here has none, which is why the default is a
# declared empty string rather than an argument each caller has to remember to
# pass — forgetting it was silent, and produced exactly the misreading the
# parameter exists to stop.
VALUE_TAKING = {
    "commit": "mFcCtSu",
    "clean": "e",
    "restore": "s",
    "branch": "u",
    # curl bundles heavily and half its alphabet swallows a value, so the list
    # has to be complete rather than convenient: `curl -fsS url` must not read
    # as a body-sending call, and `curl -obody.json url` must not read the `o`
    # value as one either.
    "curl": "AbcCdDeEFhHKmoPQrtTuUwxXyYz",
    # gh api: -f/-F are fields, -H a header, -q a jq filter, -t a template,
    # -p a preview, -X the method.
    "gh": "fFHqtpX",
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
            # A deletion is not a publication, and the prompt has to say which.
            # This file already records the standard: a bundled `-fu` "asked only
            # to 'publish', so the confirmation described something far milder than
            # what was about to happen. A prompt that misnames the consequence is
            # the prompt somebody clicks through." A review found the same defect
            # unfixed for the spellings that remove a branch from the remote.
            if long_option(arguments, "--delete", "--mirror") or short_option(
                arguments, "d", action
            ):
                return deny_or_ask(payload, "delete or overwrite refs on the remote repository")
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
    # This one was the mixed case: it hand-rolled the deny and then called
    # `deny_or_ask` for the ask, which meant asking `subagent_name` twice.
    return deny_agent_or_ask(
        payload,
        "Hard rule 10 bars subagent {agent} from rewriting governing documents.",
        "This would rewrite a governing document from the shell. Confirm this exact "
        "action only if you accept that consequence.",
    )


def credential_disclosure(command: str, payload: dict[str, Any]) -> Decision:
    # `private/` as a directory rather than two filenames. Naming the files meant
    # `cat private/ntfy.conf` was refused while `cat private/*.conf` and
    # `grep -r NTFY private/` reached the same bytes in silence — a review found
    # both.
    #
    # Two files in there are excluded, both because they hold no secret and both
    # because refusing them is a false alarm on a file somebody has a reason to open.
    # `README.md` is tracked and explains the drawer. `guard-decisions.log` is this
    # guard's own record, written specifically without command text so that it is safe
    # to read — and it was asking about itself, which is how this exclusion was found.
    # The bearer files are named by basename as well as by drawer, because a Bash call
    # brings its own working directory: `cd private && cat ntfy.conf` contains no
    # `private/` and a review found it printing the ntfy topic with this check silent —
    # the one secret that has already leaked out of the old repository.
    # `tmp`, `var` and `etc` are excluded because on macOS `/private/tmp` and
    # `/private/var` are the real paths behind `/tmp` and `/var` — so every session
    # scratch file lives under a directory literally called `private/`. Broadening this
    # check to the drawer made every command touching a temp file raise a credential
    # alarm, which cost Tyrel three separate interruptions before the cause was found.
    # A prompt on `cat /private/tmp/notes.txt` is exactly the false-alarm flood this
    # file argues at length it must not produce, and it was self-inflicted.
    secret_path = has(
        command,
        r"(?:private/(?!(?:README\.md|guard-decisions\.log)\b|(?:tmp|var|etc)\b)"
        r"|(?:^|[/\s])(?:ntfy|workcopy)\.conf\b"
        r"|(?:^|[/\s])\.env(?:[.\s/]|$)|credentials(?:\.json)?|id_(?:rsa|ed25519))",
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


# The four commands below used to be judged by matching regexes against the
# whole command line, while `git` and `rm` went through `invocation()` and
# `tokenize()`. That mismatch cost both directions. It raised false alarms,
# because a flag was found anywhere in the line rather than inside the call that
# owns it — `gh api repos/o/r > out.json; rg -f patterns.txt out.json` asked
# about a GitHub write on the strength of ripgrep's `-f`. And it left real gaps,
# because a pattern covers the spellings somebody thought of: `gh api --input
# body.json` POSTs a request body and passed in silence until the spelling was
# added by hand. Reading each call's own argument list closes both at once, and
# stops the fix being a list that has to be extended every time.
CURL_INVOCATION = invocation("curl")
WGET_INVOCATION = invocation("wget")
# httpie takes its method as the first operand: `http POST https://…`.
HTTPIE_INVOCATION = invocation("https?")
GH_INVOCATION = invocation("gh")
RUNPODCTL_INVOCATION = invocation("runpodctl")

MUTATING_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})

# One table, read by the shell path, the Python-API pattern and the MCP path alike.
#
# It was two tables until a review traced the consequence. `mcp_decision` carried its
# own inline copy of these verbs, and the copy had already drifted: it was missing
# `dev`, and *both* copies were missing `resume` and `rent`, which the pre-rebuild
# guard had blocked by name. So `mcp__runpod__resumePod` split into {resume, pod},
# matched nothing, and started a machine that bills by the hour with the guard silent
# — for the main session as well as an agent — while `createPod` correctly asked.
#
# Two verbs were lost purely because one fact lived in two places, and GOVERNANCE 8
# is the rule with an hourly bill attached. A test now asserts the shell route and
# the MCP route reach the same answer for the same verb, so the next verb added here
# cannot be added to only one of them.
RUNPOD_SHUTDOWN_VERBS = frozenset({"stop", "terminate"})
RUNPOD_ESCALATION_VERBS = frozenset(
    {"create", "start", "resume", "rent", "deploy", "dev", "remove", "delete"}
)
RUNPOD_CHANGE_VERBS = RUNPOD_SHUTDOWN_VERBS | RUNPOD_ESCALATION_VERBS
# Every one of these is visible to somebody who is not Tyrel.
GH_PUBLIC_VERBS = {
    "pr": frozenset({"create", "close", "comment", "edit", "merge", "ready", "reopen", "review"}),
    "issue": frozenset({"close", "comment", "create", "edit", "reopen"}),
    "repo": frozenset({"archive", "create", "delete", "edit", "rename"}),
    "release": frozenset({"create", "delete", "edit", "upload"}),
}
GH_GLOBAL_SHORT_VALUE_OPTIONS = "R"
GH_GLOBAL_LONG_VALUE_OPTIONS = frozenset({"--repo"})
GH_API_SHORT_VALUE_OPTIONS = "fFHqtpXR"
GH_API_LONG_VALUE_OPTIONS = frozenset(
    {
        "--cache",
        "--field",
        "--header",
        "--hostname",
        "--input",
        "--jq",
        "--method",
        "--preview",
        "--raw-field",
        "--repo",
        "--template",
    }
)


def argument_lists(pattern: re.Pattern[str], command: str) -> list[list[str]]:
    """The argument list of each call to one recognized command."""
    return [tokenize(match.group("tail")) for match in pattern.finditer(command)]


def mutating_method(
    arguments: list[str],
    short: str,
    *names: str,
    value_taking: str = "",
    long_value_taking: frozenset[str] = frozenset(),
) -> bool:
    values = option_values(
        arguments,
        short,
        *names,
        value_taking=value_taking,
        long_value_taking=long_value_taking,
    )
    return any(value.upper() in MUTATING_METHODS for value in values)


def sends_a_request_body(command: str) -> bool:
    """True when curl, wget or httpie is called in a way that changes remote state."""
    for arguments in argument_lists(CURL_INVOCATION, command):
        # -d, -T and -F are data, upload-file and form. Case matters: curl's
        # lowercase -f is --fail and its -t is --telnet-option.
        if short_option(arguments, "dTF", "curl") or long_option(
            arguments,
            "--data",
            "--data-ascii",
            "--data-binary",
            "--data-raw",
            "--data-urlencode",
            "--upload-file",
            "--form",
            "--form-string",
            "--json",
        ):
            return True
        if mutating_method(arguments, "X", "--request", value_taking=VALUE_TAKING["curl"]):
            return True
    for arguments in argument_lists(WGET_INVOCATION, command):
        # wget has no short option for a body, and its `-d` is --debug — which
        # the old whole-command pattern read as curl's `--data`.
        if long_option(arguments, "--post-data", "--post-file", "--body-data", "--body-file"):
            return True
        if mutating_method(arguments, "", "--method"):
            return True
    # `visible` for httpie throughout — the explicit-method branch below shares the
    # blindness. Writing the test for the stdin branch caught `rg 'cat body | http POST
    # url' notes.md` still asking, because that loop read the operand `POST` from inside
    # the quotes. Same family as the two failures the comment further down records; it
    # predates them and one line closes it. curl and wget above are left on the raw text
    # deliberately: their behaviour is pinned by a long list of tests, and widening the
    # quote treatment to them is a change that should earn its own review rather than
    # ride along with this one.
    visible = without_quoted_text(command)
    for arguments in argument_lists(HTTPIE_INVOCATION, visible):
        given = operands(arguments)
        if given and given[0].upper() in MUTATING_METHODS:
            return True
    # HTTPie infers POST the moment it is given data on standard input, so a piped or
    # redirected body needs no method operand at all. A review found
    # `printf … | http https://…/graphql` silent to the session and to a subagent
    # alike, while the identical curl spelling asked — a hole in one tool only, which
    # is the kind nobody trips over until it matters. The subagent tripwire does not
    # cover it either: a plain pipe is not one of INSPECTION_BLIND_SPOTS.
    # HTTPie infers POST from a body on stdin, so a pipe or a redirect into it is a
    # state-changing request carrying no method operand to recognize.
    #
    # This took three attempts and both failures are worth recording, because they were
    # the same mistake twice. Version one looked for the letters `http` after a pipe and
    # raised on `sed 's|http://old|http://new|g'`. Version two routed it through
    # `invocation()`, which fixed that and still raised on
    # `echo 'example: x | http y'` — because `invocation()` is itself quote-blind and
    # reads `| http` inside a string as a command position. A reviewer caught each one.
    #
    # So quoted spans are blanked before the text is examined at all. Prose that merely
    # *mentions* a pipeline is no longer a pipeline, which is the property both earlier
    # versions lacked, and a commit message quoting one stops being a finding.
    for arguments in argument_lists(HTTPIE_INVOCATION, visible):
        # A body arrives either by a pipe into this invocation or by a redirect among
        # its own arguments.
        if any(token == "<" or token.startswith("<") for token in arguments):
            return True
    if re.search(r"\|\s*" + _PATH + r"https?\b(?:\s|$)", visible) and argument_lists(
        HTTPIE_INVOCATION, visible
    ):
        return True
    return False


def changes_github_state(command: str, calls: list[list[str]] | None = None) -> bool:
    for arguments in argument_lists(GH_INVOCATION, command) if calls is None else calls:
        given = [
            token.lower()
            for token in operands(
                arguments,
                short_value_taking=GH_GLOBAL_SHORT_VALUE_OPTIONS,
                long_value_taking=GH_GLOBAL_LONG_VALUE_OPTIONS,
            )
        ]
        if len(given) >= 2 and given[1] in GH_PUBLIC_VERBS.get(given[0], frozenset()):
            return True
    return False


def gh_api_sends_a_body(command: str, calls: list[list[str]] | None = None) -> bool:
    """`gh api` defaults to POST as soon as anything supplies a body.

    A field, a raw field or `--input` is as good as an explicit `--method`, and
    the guard has to say so without depending on which of those the caller
    happened to type.

    `calls` lets the caller pass the already-found `gh` invocations. Both this and
    `changes_github_state` ran the same regex scan and the same tokenization over the
    same text, on every Bash call that reached them — a reviewer's finding. The cost
    was microseconds against a process startup that dwarfs it, so this is for the
    reader rather than the clock: the same work appearing twice is a thing somebody
    has to notice and reconcile. The default keeps both callable on their own.
    """
    for arguments in argument_lists(GH_INVOCATION, command) if calls is None else calls:
        given = operands(
            arguments,
            short_value_taking=GH_API_SHORT_VALUE_OPTIONS,
            long_value_taking=GH_API_LONG_VALUE_OPTIONS,
        )
        if not given or given[0].lower() != "api":
            continue
        if short_option(arguments, "fF", "gh") or long_option(
            arguments, "--field", "--raw-field", "--input"
        ):
            return True
        if mutating_method(
            arguments,
            "X",
            "--method",
            value_taking=VALUE_TAKING["gh"],
            long_value_taking=GH_API_LONG_VALUE_OPTIONS,
        ):
            return True
    return False


def runpodctl_verbs(command: str) -> set[str]:
    """The subcommand verbs every `runpodctl` call in this command names.

    A runpodctl call has one command verb: its first operand. Reading every
    operand as a verb made a pod ID or output filename named `start` look like
    a request to start paid infrastructure.
    """
    found: set[str] = set()
    for arguments in argument_lists(RUNPODCTL_INVOCATION, command):
        given = operands(arguments)
        if given:
            found.add(given[0].lower())
    return found


def external_shell_mutation(command: str, payload: dict[str, Any]) -> Decision:
    runpod_verbs = runpodctl_verbs(command)
    runpod_ctl_change = bool(runpod_verbs & RUNPOD_CHANGE_VERBS)
    runpod_api_change = has(
        command,
        r"\brunpod\.(?:create_pod|resume_pod|start_pod|create_endpoint|"
        r"create_template|delete_network_volume)\s*\(",
    )
    # The SDK spelling above is not the only way inline Python reaches paid
    # infrastructure. A review found `python3 -c "import requests;
    # requests.post(url, json=body)"` silent to the main session while the
    # `runpod.create_pod()` spelling correctly asked — the HTTP-client route that the
    # pre-rebuild guard covered and this one had quietly dropped. `INLINE_INTERPRETER`
    # cannot stand in for it: that check is subagent-only by design, and GOVERNANCE 8
    # binds the session too. Recognized only when the command also mentions runpod, so
    # an ordinary `requests.post` to anything else is still the session's own business.
    # No closing `\b`: writing the test for this caught `runpod_url` slipping past a
    # bounded spelling, because `_` is a word character. `runpod` unbounded also picks
    # up `runpod.io` and `runpodctl`, which on a path that bills by the hour is the
    # direction to be wrong in.
    runpod_http_change = has(command, r"\brunpod") and has(
        command,
        r"(?:requests|httpx|session|client)\s*\.\s*(?:post|put|patch|delete)\s*\(|"
        r"\burllib\b|\bhttp\.client\b|\bRequest\s*\(",
    )
    runpod_change = runpod_ctl_change or runpod_api_change or runpod_http_change
    # Every Python-API call recognized above starts or creates something, so its
    # presence disqualifies the shutdown exemption outright. Deriving that
    # exemption from the `runpodctl` text alone let one half of a compound
    # command cancel the warning for the other, and
    # `runpodctl stop pod abc; python3 -c 'import runpod; runpod.create_pod()'`
    # started a pod that bills by the hour with the guard silent.
    runpod_shutdown_only = (
        runpod_ctl_change
        and not runpod_api_change
        and not runpod_http_change
        and bool(runpod_verbs & RUNPOD_SHUTDOWN_VERBS)
        and not runpod_verbs & RUNPOD_ESCALATION_VERBS
    )
    if runpod_change and not (runpod_shutdown_only and not subagent_name(payload)):
        return deny_or_ask(payload, "change paid RunPod infrastructure")
    if has(command, r"(?:^|[;&|]\s*)\s*(?:ssh|scp|sftp)\b"):
        return deny_or_ask(payload, "move data to or run a command on another machine")
    # rsync only when an operand names a remote host. `ssh`, `scp` and `sftp` are
    # remote by nature, but rsync is an ordinary local copy tool and is in this
    # machine's allow list, so an unconditional ask would be a false alarm on every
    # local use. A remote target carries a colon — `host:path` or `user@host:path` —
    # and `rsync://`. A Windows drive letter cannot appear here, and a bare local
    # path with a colon in its name would over-recognize, which is the safe way to
    # be wrong.
    if has(command, r"(?:^|[;&|]\s*)\s*rsync\b[^\n;&|]*(?:rsync://|[\w.@-]+:)"):
        return deny_or_ask(payload, "copy data to another machine")
    if sends_a_request_body(command):
        return deny_or_ask(payload, "send a state-changing network request")
    gh_calls = argument_lists(GH_INVOCATION, command)
    if changes_github_state(command, gh_calls):
        return deny_or_ask(payload, "change GitHub state visible to other people")
    if gh_api_sends_a_body(command, gh_calls):
        return deny_or_ask(payload, "send a state-changing GitHub API request")
    return None


# The blind spots this module's own docstring admits to: constructs it does not
# parse, and wrappers whose real command it never sees. Each entry is deliberately
# a plain text match — the point is breadth, not precision. See subagent_blind_spot.
INSPECTION_BLIND_SPOTS = (
    (re.compile(r"\$\("), "a command substitution"),
    (re.compile(r"`[^`]"), "a backtick substitution"),
    (re.compile(r"[<>]\("), "a process substitution"),
    (
        re.compile(
            r"\b(?:xargs|env|nohup|timeout|setsid|stdbuf|nice|ionice|parallel|watch|script)\b"
        ),
        "a wrapper command",
    ),
    (re.compile(r"\bfind\b[^;&|]*-(?:exec|execdir|delete)\b"), "a find action"),
    # A heredoc handed to an interpreter. `split_heredocs` now captures the body so
    # the checks at least see it, but seeing it is not the same as understanding it:
    # `python3 <<'PY'` with `subprocess.run(["git","push"])` inside puts `git` inside a
    # quoted Python list, which is not a command position, so no Git check can fire.
    # That is this file's oldest admitted limit and no parser closes it. What closes it
    # for the audience nobody is watching is treating the construct itself as a blind
    # spot, which is what a reviewer recommended once the body-inspection route turned
    # out not to reach. The main session is unaffected, as with every entry here.
    (
        re.compile(r"\b(?:python3?|perl|ruby|node|deno|php|osascript)\b[^\n;&|]*<<"),
        "a heredoc handed to an interpreter",
    ),
)

# Capabilities worth stopping when this file cannot see what is being done with
# them. Names, not verbs: MUTATION_WORDS carries "add", "set" and "write", which
# appear in ordinary commands and would make the tripwire below useless. Bare
# `git` is out for the same reason — `$(git rev-parse HEAD)` is not the risk;
# the named history and publishing verbs are.
CONSEQUENTIAL_CAPABILITIES = frozenset(
    {
        "push",
        "merge",
        "rebase",
        "reset",
        "clean",
        "filter-branch",
        "curl",
        "wget",
        "http",
        "https",
        "gh",
        "runpod",
        "runpodctl",
        "ssh",
        "scp",
        "rsync",
        "chmod",
        "chown",
        "pip",
        "npm",
        "brew",
    }
)
CAPABILITY_WORD = re.compile(r"[A-Za-z][A-Za-z-]*")


def subagent_blind_spot(command: str, payload: dict[str, Any]) -> Decision:
    """Refuse a subagent a consequential capability this file cannot read.

    **Asymmetric on purpose.** The precise parsing in this module exists to avoid
    false alarms, and a false alarm only costs something when it interrupts Tyrel.
    Raised against a subagent it costs nothing at all: the agent reports back and
    the accountable main session runs the command itself, having read it. So the
    two audiences justify different thresholds, and the cheap one belongs on the
    side nobody is watching.

    The checks above are precise and fire on what they recognize. This one fires
    on what they *cannot* recognize — a substitution, a wrapper, a `find -exec` —
    when a consequential capability is named anywhere in the same command. It
    closes every blind spot in the docstring at once, including spellings nobody
    has thought of yet, for the audience that is unattended by definition.

    It runs last, so a recognized action still gets its specific reason.
    """
    agent = subagent_name(payload)
    if not agent:
        return None
    named = {word.lower() for word in CAPABILITY_WORD.findall(command)} & CONSEQUENTIAL_CAPABILITIES
    if not named:
        return None
    for pattern, description in INSPECTION_BLIND_SPOTS:
        if pattern.search(command):
            return (
                "deny",
                f"Subagent {agent} may not reach {', '.join(sorted(named))} through "
                f"{description}, which the repository guard cannot read. Run the step "
                f"without it, or report it to the main session.",
            )
    return None


# Scripts in this repository whose *effect* is consequential even though the
# command line naming them looks ordinary. A review found all three silent to a
# subagent: `seat.sh` spends money on a paid model seat, `notify.sh` reaches
# Tyrel's phone — and CLAUDE.md says subagents never notify, a rule that until now
# had nothing behind it — and `capture-seat-report.sh` writes reviewer evidence.
# Matched on the basename with any directory prefix, because a Bash call carries its
# own working directory: `cd operations/notify && sh notify.sh done "…"` never
# contains the joined path these patterns used to require, and a review found all
# three reachable that way — a message to Tyrel's phone, a paid seat, and reviewer
# evidence, each from a subagent with this guard silent. The extra asks a bare
# basename costs are the trade this file already says it makes.
CONSEQUENTIAL_SCRIPTS = (
    (re.compile(r"(?:^|[\s;&|/])seat\.sh\b"), "spend money on a paid model seat"),
    (re.compile(r"(?:^|[\s;&|/])notify\.sh\b"), "send a notification to Tyrel's phone"),
    (
        re.compile(r"(?:^|[\s;&|/])capture-seat-report\.sh\b"),
        "write reviewer evidence",
    ),
)

# An interpreter handed code on its own command line does anything at all, and the
# text of that code is opaque to every check in this file. `python3 -c
# 'subprocess.run(["git","push"])'` was silent to a subagent while a plain
# `git push` was denied — the guard was reading spellings, not capabilities.
#
# Denied for subagents only. The main session uses these constantly and an ask on
# every one would be the false-alarm flood that gets a guard switched off; the
# session is also the accountable reader of its own commands.
# `_PATH` for the same reason every other command pattern here carries it — a review
# found `/usr/bin/python3 -c '…'` silent while a bare `python3 -c` was denied. The
# short-option branch accepts a bundled cluster (`-Sc`), which was the second escape:
# the flag that supplies the code need not be alone in its word.
INLINE_INTERPRETER = re.compile(
    r"(?:^|[;&|]\s*)\s*" + _PATH + r"(?:python3?|perl|ruby|node|deno|php|osascript)\b"
    r"[^\n;&|]*(?:\s-[A-Za-z]*[cer]\b|\s--eval\b|\s--command\b)"
)


def subagent_consequential_script(command: str, payload: dict[str, Any]) -> Decision:
    """Refuse a subagent the scripts and interpreters that act through this file's blind side.

    Both halves are subagent-only and deliberate. The main session runs `python3 -c`
    and dispatches seats as ordinary work, with Tyrel reading the result; an agent
    doing either is spending his money, writing to his phone, or executing code
    nothing inspected — unattended, which is the whole distinction this guard draws.
    """
    agent = subagent_name(payload)
    if not agent:
        return None
    for pattern, reason in CONSEQUENTIAL_SCRIPTS:
        if pattern.search(command):
            return "deny", f"Subagent {agent} may not {reason}; report it to the main session."
    if INLINE_INTERPRETER.search(command):
        return (
            "deny",
            f"Subagent {agent} may not run code supplied inline to an interpreter, which this "
            "guard cannot read. Put the work in a reviewed file, or report it to the main session.",
        )
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
        subagent_consequential_script,
    ):
        found = check(command, payload)
        if found:
            return found
    if has(command, r"\b(?:chmod|mv|rm)\b[^\n;&|]*(?:\.githooks/|\.git/hooks/)"):
        return deny_or_ask(payload, "disable or remove an installed Git hook")
    return subagent_blind_spot(command, payload)


def has_mutating_method(value: Any) -> bool:
    if isinstance(value, dict):
        for key, item in value.items():
            if (
                str(key).lower() in {"method", "http_method", "request_method"}
                and isinstance(item, str)
                # MUTATING_METHODS, not a second copy of it: the shell path and this
                # one must agree about what a state-changing verb is, and a reviewer
                # found two independently-typed lists with nothing keeping them so.
                and item.upper() in MUTATING_METHODS
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
    # The same two frozensets the shell path reads, not a second copy of them.
    runpod_mutation = "runpod" in lowered and bool(segments & RUNPOD_CHANGE_VERBS)
    runpod_shutdown_only = (
        "runpod" in lowered
        and bool(segments & RUNPOD_SHUTDOWN_VERBS)
        and not bool(segments & RUNPOD_ESCALATION_VERBS)
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
    harness = harness_write(tool, tool_input, payload)
    if harness:
        return harness
    if tool == "Bash":
        return bash_decision(tool_input, payload)
    if tool.startswith("mcp__"):
        return mcp_decision(tool, tool_input, payload)
    return None


# Every part of this harness could refuse; none of it could remember. A review
# found that a denied subagent push, a blind-spot refusal, an ask clicked through at
# two in the morning — all of it existed only in a chat transcript, which hard rule 7
# says is where a finding goes to be lost. Tyrel is often away from the keyboard, so
# "did anything try" is a question he currently has no way to ask.
#
# One line per decision, appended. It does not gate, refuse, or claim anything; the
# receipt this project retired failed because it asserted that a review happened, and
# this asserts only what the guard itself did.
DECISION_LOG = Path("private") / "guard-decisions.log"

# **The command text is deliberately not recorded.** A refused command is exactly the
# kind that may carry a credential — `curl -H "Authorization: Bearer …"`, a topic, a
# key — and writing it to a file would persist the secret this guard exists to keep
# out of transcripts. The reason strings above name the *class* of action and were
# written to be safe to repeat, which is what makes them the right thing to log. Tool
# name and agent name are structural and carry no payload.
LOG_LINE_MAX = 400


def record(payload: dict[str, Any], decision: str, reason: str) -> None:
    """Append one line about a decision. Never raises, never blocks the decision."""
    try:
        target = project_root() / DECISION_LOG
        if not target.parent.is_dir():
            return
        stamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        tool = payload.get("tool_name")
        actor = subagent_name(payload) or "main-session"
        line = (
            f"{stamp}\t{decision}\t{actor}\t{tool if isinstance(tool, str) else '?'}\t"
            f"{' '.join(reason.split())[:LOG_LINE_MAX]}\n"
        )
        # Opened per call rather than held: the guard is a fresh process every time.
        # Append mode with one write of one line is atomic enough for concurrent
        # agents on every filesystem this runs on.
        with target.open("a", encoding="utf-8") as log:
            log.write(line)
    except Exception as error:  # noqa: BLE001 - a failed record must not block a decision
        # Not silent, and not fatal. A full disk or a read-only checkout must not
        # stop the guard deciding, but hard rule 7 means the loss has to be visible
        # rather than swallowed.
        print(
            f"repository guard could not record its decision: {type(error).__name__}",
            file=sys.stderr,
        )


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
    except Exception as error:  # noqa: BLE001 - the guard must fail closed, not crash open
        print(
            f"repository guard could not inspect the tool call: {type(error).__name__}",
            file=sys.stderr,
        )
        return 2
    if decision:
        record(payload, *decision)
        emit(*decision)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
