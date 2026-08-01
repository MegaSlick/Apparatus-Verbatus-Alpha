#!/usr/bin/env python3
"""The repository guard: five silent refusals, and quiet the rest of the time.

This file replaced a 2,294-line predecessor that had three verdicts — deny, ask,
silence — and spent most of its life asking. Over three days it made 639 decisions,
503 of them asks, 590 of them aimed at the main session rather than an agent. Tyrel
reports he had begun approving without reading. A prompt that is always answered yes
is not a control; it is training. So the ask verdict is gone, and with it the reason
the old file was long: nearly all of that code existed to decide *which* of two
audiences got asked, in how many spellings, at what precision.

**Two verdicts now: deny, or nothing.** A denial is final within the session — there
is no button. If a refusal is wrong for the work at hand, the session stops and says
so, and Tyrel runs the command himself or switches the guard off (README.md, Controls,
says how). That is the whole appeal path, and it is deliberate: hard rule 11 says a
guard he cannot unwire is a defect.

**What it refuses, and nothing else:**

1. Landing work on `main` — committing there, pushing there, or writing a file in a
   checkout standing there.
2. Deleting recursively outside the drawers that exist to be deleted.
3. Rewriting published history — a force-push, `filter-branch`, `filter-repo`, or a
   `reset --hard` that discards the working tree.
4. Deleting a remote ref, or the repository itself.
5. Putting a credential into git.

Every one of those is unrecoverable, or close enough that the difference is a bad
night. Everything else passes in silence, including things the old guard asked about:
ordinary pushes, `gh` calls, RunPod commands, edits to governed documents. Those are
now governed by CLAUDE.md and by the session having read it — which is the trade this
project made when it moved agents into containers. A rule a session has read is a
better control than a prompt it has learned to dismiss.

**Precision, and why it leans the other way from the old file.** The predecessor
over-recognized on purpose: a false alarm cost one prompt. Here a false alarm costs a
blocked command with no override, so this reads narrowly. It flattens newlines so a
harmless first line cannot hide a later one, and it lifts heredoc bodies out so a
document that quotes a dangerous command is not read as one. It does not expand
`sh -c` payloads, follow command substitution, or model git's option table. It is an
accident-catcher for an accountable session, not a sandbox against an adversary —
writing agents run in containers now (`operations/autoclave/README.md`) and are not
this file's problem.
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

Decision = Optional[Tuple[str, str]]

WRITING_TOOLS = frozenset({"Write", "Edit", "MultiEdit", "NotebookEdit"})
PATH_KEYS = ("file_path", "notebook_path", "path")
CONTENT_KEYS = ("content", "new_string", "new_source")

MAIN_REF = "refs/heads/main"
MAIN_NAMES = frozenset({"main", MAIN_REF})

# Hard rule 3, quoted rather than paraphrased so the refusal is arguable on sight.
RULE_THREE = '3: "A session never works from `main`." (CLAUDE.md, Hard rules)'
OFF_MAIN = "Move off it with `git switch -c work/<topic>`; staged changes come along."


# ---------------------------------------------------------------- reading the tree

def project_root() -> Path:
    return Path(os.environ.get("CLAUDE_PROJECT_DIR") or os.getcwd()).resolve()


# Distinct from `None`, which means there is no checkout here at all. A detached HEAD
# stands on no branch, so a commit there moves none, and it is not what rule 3 is
# about — but it still stops the upward walk, because the nearest checkout is the one
# a commit would land in and an outer clone's branch is an answer about a different
# repository.
NO_BRANCH = ""


def git_head_ref(directory: Path) -> str | None:
    """The branch ref this directory's checkout stands on, or None if it has none.

    Both spellings of `.git` are read: a linked worktree's is a file naming the real
    gitdir, and HEAD there is per-worktree. That is what makes this answer for the
    checkout *containing* a path rather than for the clone it was cut from — a
    worktree on `work/x` is fine while its parent clone sits on `main`.
    """
    marker = directory / ".git"
    gitdir = marker
    if marker.is_file():
        try:
            text = marker.read_text(encoding="utf-8", errors="replace")[:4096]
        except OSError:
            return NO_BRANCH
        found = re.match(r"\s*gitdir:\s*(.+)", text)
        if not found:
            return NO_BRANCH
        # `--relative-paths` writes a gitdir relative to the checkout, not to this
        # process. `Path.__truediv__` returns an absolute right operand unchanged, so
        # one expression reads both spellings.
        gitdir = (directory / Path(found.group(1).strip())).resolve()
    elif not marker.is_dir():
        return None
    try:
        head = (gitdir / "HEAD").read_text(encoding="utf-8", errors="replace")[:4096]
    except OSError:
        return NO_BRANCH
    reference = head.strip()
    return reference[4:].strip() if reference.startswith("ref:") else NO_BRANCH


def checkout_branch(start: Path) -> str | None:
    """The branch of the nearest checkout at or above `start`."""
    for directory in (start, *start.parents):
        reference = git_head_ref(directory)
        if reference is not None:
            return reference
    return None


def working_directory(payload: dict[str, Any]) -> Path:
    cwd = payload.get("cwd")
    if isinstance(cwd, str) and cwd.strip():
        return Path(cwd).resolve()
    return project_root()


def written_path(tool_input: Any, payload: dict[str, Any]) -> Path | None:
    if not isinstance(tool_input, dict):
        return None
    raw = next((tool_input[key] for key in PATH_KEYS if key in tool_input), None)
    if not isinstance(raw, str) or not raw.strip():
        return None
    expanded = os.path.expandvars(os.path.expanduser(raw.strip()))
    if os.path.isabs(expanded):
        return Path(expanded).resolve()
    return (working_directory(payload) / expanded).resolve()


def written_text(tool_input: Any) -> str:
    if not isinstance(tool_input, dict):
        return ""
    parts = [tool_input[key] for key in CONTENT_KEYS if isinstance(tool_input.get(key), str)]
    edits = tool_input.get("edits")
    if isinstance(edits, list):
        for edit in edits:
            if isinstance(edit, dict) and isinstance(edit.get("new_string"), str):
                parts.append(edit["new_string"])
    return "\n".join(parts)


# ------------------------------------------------------------ reading a command

def strip_heredocs(text: str) -> str:
    """Remove heredoc bodies, which are data rather than commands.

    `cat <<EOF` containing the words `git push --force` is a document. The old guard
    read one as a command and produced an unappealable refusal on an ordinary write;
    with no ask verdict left, that mistake now costs more, so bodies come out and are
    never looked at again. An unterminated heredoc removes nothing — lines this cannot
    reliably terminate stay commands.
    """
    if "<<" not in text:
        return text
    lines = text.split("\n")
    kept: list[str] = []
    index = 0
    while index < len(lines):
        line = lines[index]
        kept.append(line)
        index += 1
        for tag in heredoc_tags(line):
            end = index
            while end < len(lines) and lines[end].strip() != tag:
                end += 1
            if end >= len(lines):
                continue
            index = end + 1
    return "\n".join(kept)


HEREDOC_TAG = re.compile(
    r"\s*(?:(?P<quote>['\"])(?P<quoted>[^'\"]*)(?P=quote)|(?P<bare>[A-Za-z_][A-Za-z0-9_]*))"
)


def heredoc_tags(line: str) -> list[str]:
    """Terminators this line opens a heredoc for.

    Quote-aware, because a plain search cannot tell the operator from the same three
    characters inside a quoted argument or after a `#`, and each of those mistakes
    ends the same way — the following lines eaten as a body and skipped entirely.
    `<<<` is a here-string and opens nothing.
    """
    tags: list[str] = []
    quote: str | None = None
    index = 0
    while index < len(line):
        char = line[index]
        if quote:
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
            break
        if line.startswith("<<", index):
            if line.startswith("<<<", index):
                index += 3
                continue
            cursor = index + 2 + (1 if line[index + 2 : index + 3] == "-" else 0)
            found = HEREDOC_TAG.match(line, cursor)
            if not found:
                index = cursor  # a spelling this cannot read opens nothing
                continue
            tag = found.group("quoted") or found.group("bare")
            index = found.end()
            if tag:
                tags.append(tag)
            continue
        index += 1
    return tags


def flatten(text: str) -> str:
    """Rewrite unquoted newlines as `;` so a later line cannot hide behind an early one.

    Every check below recognizes a command at the start of the string or after a
    separator, and Python's `^` without re.MULTILINE matches offset zero only. Quote
    state is tracked so a newline inside a quoted argument stays literal.
    """
    if "\n" not in text:
        return text
    out: list[str] = []
    quote: str | None = None
    index = 0
    while index < len(text):
        char = text[index]
        if quote == "'":
            out.append(char)
            if char == "'":
                quote = None
            index += 1
            continue
        if char == "\\" and index + 1 < len(text):
            if quote is None and text[index + 1] == "\n":
                index += 2  # a continuation: one command, not two
                continue
            out.append(char)
            out.append(text[index + 1])
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


def normalize(command: str) -> str:
    return flatten(strip_heredocs(command))


# Any command may be written path-qualified, and this project launches detached work
# through `nohup`, `timeout` and friends. A wrapper not named here hides the command
# behind it: this list fails open, which is the right direction for an accident guard.
_PATH = r"(?:[^\s;&|]*/)?"
_ASSIGNMENT = r"[A-Za-z_][A-Za-z0-9_]*=[^\s;&|]*"
_WRAPPER = (
    rf"(?:{_ASSIGNMENT}\s+|{_PATH}(?:command|exec|rtk|nohup|setsid|sudo)\s+"
    rf"|{_PATH}(?:nice|timeout|stdbuf|time)(?:\s+-\S+|\s+\d+\S*)*\s+)*"
)


def invocation(name: str) -> re.Pattern[str]:
    return re.compile(
        rf"(?:^|[;&|]\s*)\s*{_WRAPPER}{_PATH}{name}\b(?P<tail>[^\n;&|]*)",
        flags=re.IGNORECASE,
    )


GIT = invocation("git")
RM = invocation("rm")
GH = invocation("gh")


def tokenize(tail: str) -> list[str]:
    """Split a command tail, falling back to a permissive split rather than giving up.

    GOVERNANCE.md 10: a check that cannot run is a failure, not a pass. `shlex` raises
    on an unbalanced quote that bash itself would never see — `git push origin main #"`
    is a comment to one and an unterminated string to the other — so an unparseable
    tail yields approximate tokens and is judged on those.
    """
    try:
        return shlex.split(tail, posix=True)
    except ValueError:
        return tail.replace("'", " ").replace('"', " ").split()


def git_calls(command: str) -> list[tuple[str, list[str]]]:
    """Recognizable git calls as (subcommand, arguments), skipping global options."""
    calls: list[tuple[str, list[str]]] = []
    for match in GIT.finditer(command):
        tokens = tokenize(match.group("tail"))
        index = 0
        while index < len(tokens):
            token = tokens[index]
            if token == "--":
                index += 1
                break
            if token in ("-c", "-C", "--config-env", "--git-dir", "--work-tree", "--namespace"):
                index += 2
                continue
            if token.startswith("-"):
                index += 1
                continue
            break
        if index < len(tokens):
            calls.append((tokens[index].lower(), tokens[index + 1 :]))
    return calls


def flag(arguments: list[str], *names: str, short: str = "") -> bool:
    """True when a long option in `names`, or a bundled short option in `short`, is set."""
    for token in arguments:
        if token == "--":
            break
        if token.startswith("--"):
            if token.split("=", 1)[0] in names:
                return True
        elif short and token.startswith("-") and len(token) > 1:
            if any(character in short for character in token[1:]):
                return True
    return False


def operands(arguments: list[str]) -> list[str]:
    return [token for token in arguments if not token.startswith("-")]


# ---------------------------------------------------------------- the five refusals

def landing_on_main(tool: str, tool_input: Any, payload: dict[str, Any]) -> Decision:
    """1. Committing on `main`, pushing to `main`, or writing in a checkout on `main`.

    The write is here rather than left to the git hook because it is the same act at
    an earlier moment, and because it is how the rule was actually broken — twice in
    two sessions, by a session that simply never looked at which branch it was on. The
    hook cannot see a session sitting on main writing files; this can.

    Nothing here judges *what* is being written. The rule is about where you stand.
    """
    if tool in WRITING_TOOLS:
        target = written_path(tool_input, payload)
        if target is not None and checkout_branch(target.parent) == MAIN_REF:
            return "deny", (
                f"That path is in a checkout standing on main, and hard rule {RULE_THREE} "
                f"{OFF_MAIN}"
            )
        return None
    if tool != "Bash":
        return None
    command = normalize(bash_command(tool_input))
    calls = git_calls(command)
    for action, arguments in calls:
        if action == "commit" and checkout_branch(working_directory(payload)) == MAIN_REF:
            return "deny", (
                f"This checkout stands on main, and hard rule {RULE_THREE} {OFF_MAIN}"
            )
        if action == "push" and any(
            token.lstrip("+").split(":", 1)[-1] in MAIN_NAMES
            for token in arguments
            if not token.startswith("-")
        ):
            return "deny", (
                f"That pushes at main, and hard rule {RULE_THREE} Work reaches main by "
                "pull request or not at all (hard rule 3, Branches)."
            )
    return None


# Directories that exist to be thrown away. A recursive delete confined to these is
# ordinary housekeeping; anything else is refused. Judged by target rather than by the
# shape of the command, which is the correction the old guard needed: it refused
# `rm -rf` on the scratch drawer whose own rule is "disposable, delete without
# checking", and that refusal is most of what taught the reflex.
DISPOSABLE_ROOTS = ("workbench/scratch",)
DISPOSABLE_PREFIXES = ("/tmp/", "/private/tmp/", "/var/folders/")
DISPOSABLE_NAMES = frozenset({"__pycache__", ".pytest_cache", ".ruff_cache", "node_modules"})

# What a literal path may contain, stated as what IS allowed. This gates an exemption,
# so an incomplete list must fail toward refusing: `normpath` resolves a literal `..`,
# but nothing resolves `$p`, and `p=../../..` with `rm -rf workbench/scratch/$p` leaves
# the drawer entirely.
LITERAL_PATH = re.compile(r"[A-Za-z0-9._/-]+")


def disposable(operand: str, payload: dict[str, Any]) -> bool:
    cleaned = operand.strip()
    if not LITERAL_PATH.fullmatch(cleaned):
        return False
    if os.path.basename(os.path.normpath(cleaned)) in DISPOSABLE_NAMES:
        return True
    absolute = os.path.normpath(
        cleaned if os.path.isabs(cleaned) else str(working_directory(payload) / cleaned)
    )
    if any(absolute.startswith(prefix) for prefix in DISPOSABLE_PREFIXES):
        return True
    root = str(project_root())
    return any(
        absolute == f"{root}/{drawer}" or absolute.startswith(f"{root}/{drawer}/")
        for drawer in DISPOSABLE_ROOTS
    )


def recursive_delete(tool: str, tool_input: Any, payload: dict[str, Any]) -> Decision:
    """2. A recursive delete, or a `git clean`, reaching outside the disposable drawers.

    `workbench/` is gitignored and exists only on this machine: an unguarded recursive
    delete there destroys every note, handoff and half-finished thought with no copy
    anywhere. That is the thing this refusal is actually for. A single-file `rm` passes
    — it is constant in ordinary work and cheap to redo, and refusing it is the noise
    that makes a real refusal invisible.
    """
    if tool != "Bash":
        return None
    command = normalize(bash_command(tool_input))
    for match in RM.finditer(command):
        flags: list[str] = []
        targets: list[str] = []
        ended = False
        for token in tokenize(match.group("tail")):
            if not ended and token == "--":
                ended = True  # `rm -- -rf` deletes a file named `-rf`
            elif not ended and token.startswith("-") and len(token) > 1:
                flags.append(token)
            else:
                targets.append(token)
        # `-R` is recursive on BSD rm as well as GNU. `-d` is not here: it removes an
        # empty directory and nothing else, so refusing it would be noise.
        if not flag(flags, "--recursive", short="rR"):
            continue
        if targets and all(disposable(target, payload) for target in targets):
            continue
        return "deny", (
            "That deletes recursively outside `workbench/scratch/` and the temporary "
            "directories. Delete the paths one at a time, or ask Tyrel to run it."
        )
    for action, arguments in git_calls(command):
        if action == "clean" and flag(arguments, "--force", short="f"):
            return "deny", (
                "`git clean` destroys untracked files, which in this repository means "
                "`workbench/` — gitignored, local only, and not recoverable from git. "
                "Delete what you meant to delete by name."
            )
    return None


def rewriting_history(tool: str, tool_input: Any, payload: dict[str, Any]) -> Decision:
    """3. A force-push, a history filter, or a `reset --hard`.

    A local rebase and `commit --amend` are deliberately *not* here. Both are routine —
    CLAUDE.md has `Reviewed-by:` trailers amended in after a review pass returns — and
    both are recoverable from the reflog. What is not recoverable is a rewrite that has
    reached the remote, or a `reset --hard` that discards a working tree git never saw.
    """
    if tool != "Bash":
        return None
    for action, arguments in git_calls(normalize(bash_command(tool_input))):
        if action == "push" and (
            flag(arguments, "--force", "--force-with-lease", "--force-if-includes", short="f")
        ):
            return "deny", (
                "A force-push rewrites history other people and other agents may hold. "
                "Hard rule 5: never rebase, force-push or amend a branch that is not "
                "yours. If this branch is yours and it truly needs it, stop and ask Tyrel."
            )
        if action in ("filter-branch", "filter-repo"):
            return "deny", (
                f"`git {action}` rewrites every commit it touches and cannot be undone "
                "from here. Ask Tyrel to run it."
            )
        if action == "reset" and flag(arguments, "--hard"):
            return "deny", (
                "`reset --hard` discards the working tree, including changes git has "
                "never seen and cannot return. Use `git restore <path>` for one file, or "
                "`git stash` to put the tree somewhere it can be read back."
            )
    return None


def deleting_a_remote(tool: str, tool_input: Any, payload: dict[str, Any]) -> Decision:
    """4. Deleting a remote ref, or the repository."""
    if tool != "Bash":
        return None
    command = normalize(bash_command(tool_input))
    for action, arguments in git_calls(command):
        if action != "push":
            continue
        # `git push origin :branch` is the colon spelling of a delete, and the empty
        # source is the whole of the syntax.
        colon_delete = any(
            token.lstrip("+").startswith(":") for token in arguments if not token.startswith("-")
        )
        if flag(arguments, "--delete", "--mirror", short="d") or colon_delete:
            return "deny", (
                "That deletes a ref on the remote. Nothing on this machine restores a "
                "branch someone else has already fetched away from. Ask Tyrel."
            )
    for match in GH.finditer(command):
        tokens = [token.lower() for token in operands(tokenize(match.group("tail")))]
        if tokens[:2] == ["repo", "delete"]:
            return "deny", "Deleting the repository is Tyrel's, and only from the web interface."
    return None


# High-signal only. A pattern that fires on ordinary prose is a pattern that gets
# switched off, and `pre-push` scans outgoing history as the backstop for everything
# this does not recognize.
CREDENTIALS = re.compile(
    r"sk-ant-[A-Za-z0-9_-]{16,}"
    r"|gh[pousr]_[A-Za-z0-9]{20,}"
    r"|github_pat_[A-Za-z0-9_]{20,}"
    r"|AKIA[0-9A-Z]{16}"
    r"|xox[baprs]-[A-Za-z0-9-]{10,}"
    r"|-----BEGIN (?:[A-Z ]+ )?PRIVATE KEY-----"
)

# Where secrets are allowed to live: gitignored, local, and never staged.
SECRET_DRAWERS = ("private", "workbench")


def credential_into_git(tool: str, tool_input: Any, payload: dict[str, Any]) -> Decision:
    """5. Staging `private/`, or writing a recognizable secret into tracked ground.

    `private/` holds the notification topic — a bearer secret that CLAUDE.md says
    never enters a script, note, commit or transcript. It is gitignored, so the only
    way it reaches history is somebody forcing it in, and `git add -f private/…` is
    exactly how that would read.
    """
    if tool in WRITING_TOOLS:
        target = written_path(tool_input, payload)
        if target is None or not CREDENTIALS.search(written_text(tool_input)):
            return None
        root = str(project_root())
        if not str(target).startswith(root + os.sep):
            return None
        relative = str(target)[len(root) + 1 :]
        if relative.split(os.sep, 1)[0] in SECRET_DRAWERS:
            return None
        return "deny", (
            f"That writes something shaped like a credential into {relative}, which is "
            "tracked. Secrets live in `private/` or `workbench/`, both gitignored, and "
            "are referenced from there."
        )
    if tool != "Bash":
        return None
    for action, arguments in git_calls(normalize(bash_command(tool_input))):
        if action != "add":
            continue
        for target in operands(arguments):
            head = os.path.normpath(target.strip("./")).split(os.sep, 1)[0]
            if head == "private":
                return "deny", (
                    "`private/` is gitignored because it holds the notification bearer "
                    "topic. Staging it puts a secret into history, where deleting it "
                    "later does not remove it."
                )
    return None


# `-n` is git-commit's short `--no-verify`, but half the alphabet around it swallows a
# value: `git commit -mno` carries the message "no", and reading its `n` as a bypass
# would refuse an ordinary commit. So a bundle counts only when every letter in it is
# one that takes no value — `-an`, `-nv`, `-n`. Anything else falls through, which errs
# toward letting a spelling past rather than refusing a real commit.
SAFE_COMMIT_BUNDLE = re.compile(r"-[aenqsvz]*n[aenqsvz]*")

# Reading the setting is fine and routine — `install.sh` and every session check does
# it. What is refused is *changing* it, in the two spellings that reach the same layer:
# inline config attached to one command, and a `git config` write.
_HOOKS_PATH_WRITE = {"--unset", "--unset-all", "--replace-all", "--remove-section", "--add"}


def hooks_path_is_being_set(tokens: list[str]) -> bool:
    for index, token in enumerate(tokens):
        if token in ("-c", "--config-env") and index + 1 < len(tokens):
            if tokens[index + 1].startswith("core.hookspath"):
                return True
        if token.startswith(("-ccore.hookspath", "--config-env=core.hookspath")):
            return True
    if tokens[:1] != ["config"]:
        return False
    arguments = tokens[1:]
    if _HOOKS_PATH_WRITE & set(arguments) and any(
        token.startswith("core.hookspath") for token in arguments
    ):
        return True
    # `git config core.hooksPath <value>` sets it; the same call without a value reads it.
    values = [token for token in arguments if not token.startswith("-")]
    return len(values) > 1 and values[0].startswith("core.hookspath")


def switching_the_hooks_off(tool: str, tool_input: Any, payload: dict[str, Any]) -> Decision:
    """6. Turning off the git hooks, which are the backstop for everything above.

    One past the five Tyrel named, and named here rather than folded in quietly. It is
    here because CLAUDE.md says it is: "`--no-verify` and `-c core.hooksPath=` are
    blocked for Claude and open to everything else — that asymmetry is deliberate and
    required by hard rule 11. It is his way around his own machinery; do not close it."
    Dropping the check would have left that sentence false, and would have left one
    flag between a session and the credential scan, the branch refusal and the
    attribution check together.

    It costs nothing in noise: a session has no legitimate use for either spelling.
    """
    if tool != "Bash":
        return None
    command = normalize(bash_command(tool_input))
    for match in GIT.finditer(command):
        tokens = [token.lower() for token in tokenize(match.group("tail"))]
        if hooks_path_is_being_set(tokens):
            return "deny", (
                "`core.hooksPath` is the one setting that makes any hook run at all. "
                "Pointing it elsewhere switches off the credential scan, the branch "
                "refusal and the attribution check together. This spelling is Tyrel's, "
                "not a session's (CLAUDE.md, Pushing and merging)."
            )
        if "--no-verify" in tokens or (
            tokens[:1] == ["commit"] and any(SAFE_COMMIT_BUNDLE.fullmatch(t) for t in tokens[1:])
        ):
            return "deny", (
                "`--no-verify` skips the hooks that refuse a commit on main, an "
                "unattributed message and a credential in outgoing history. It is "
                "Tyrel's way around his own machinery and not a session's "
                "(CLAUDE.md, Pushing and merging). Fix what the hook objects to."
            )
    return None


CHECKS = (
    landing_on_main,
    recursive_delete,
    rewriting_history,
    deleting_a_remote,
    credential_into_git,
    switching_the_hooks_off,
)


def bash_command(tool_input: Any) -> str:
    if isinstance(tool_input, dict) and isinstance(tool_input.get("command"), str):
        return tool_input["command"]
    return ""


def evaluate(payload: dict[str, Any]) -> Decision:
    tool = payload.get("tool_name")
    if not isinstance(tool, str) or not tool:
        return "deny", "The repository guard received no readable tool name."
    tool_input = payload.get("tool_input")
    for check in CHECKS:
        decision = check(tool, tool_input, payload)
        if decision:
            return decision
    return None


# ------------------------------------------------------------------- reporting out

# One line per refusal, appended. Hard rule 7: a refusal that exists only in a chat
# transcript is a finding gone to be lost, and Tyrel is often away from the keyboard.
# Only denials reach it now, so it stays short enough to read.
#
# **The command text is deliberately not recorded.** A refused command is exactly the
# kind that may carry the credential this guard exists to keep out of files. The reason
# strings above name the class of action and were written to be safe to repeat.
DECISION_LOG = Path("private") / "guard-decisions.log"
LOG_LINE_MAX = 400


def record(payload: dict[str, Any], decision: str, reason: str) -> None:
    """Append one line. Never raises, never blocks the decision."""
    try:
        target = project_root() / DECISION_LOG
        if not target.parent.is_dir():
            return
        stamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        tool = payload.get("tool_name")
        line = (
            f"{stamp}\t{decision}\t{tool if isinstance(tool, str) else '?'}\t"
            f"{' '.join(reason.split())[:LOG_LINE_MAX]}\n"
        )
        with target.open("a", encoding="utf-8") as log:
            log.write(line)
    except Exception as error:  # noqa: BLE001 - a failed record must not block a decision
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
    except Exception as error:  # noqa: BLE001 - the guard fails closed, it does not crash open
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
