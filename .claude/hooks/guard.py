#!/usr/bin/env python3
"""The repository guard: six refusals for the session, a seventh for an agent, quiet otherwise.

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
6. Switching the git hooks off, which are the backstop for all of the above.
7. **A spawned agent** editing a governed path — the one rule that is not the same for
   both audiences, and the reason built-in agent types can be used here at all.

Tyrel named the first five. The sixth is one past that and says so where it is defined;
CLAUDE.md requires it, and without it one flag stands between a session and every hook
at once. The seventh is the balance struck when Claude Code's own agent types were let
back in: they hold `Bash`, they carry no project instruction, and hard rule 10 needs a
mechanical half exactly where the reader of the rule is absent. Six of the seven are
unrecoverable, or close enough that the difference is a bad night; the seventh is
recoverable and is there because the actor cannot be reasoned with. Everything else passes in silence, including things the old guard asked about:
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
# `rtk proxy` is spelled separately because it is **two** tokens. Listing `rtk` alone
# looked like coverage and was not: `proxy` landed where a command name was expected,
# nothing matched, and every refusal below returned no decision for anything behind it
# — a push at `main`, a recursive delete, `--no-verify`, all of it. The blind spot sat
# on the documented habit, because `RTK.md` tells a session to reach for `rtk proxy`
# whenever a summary cannot be trusted, which a careful session does constantly.
# `rtk gain` and `rtk discover` are not wrappers; they run no command after them.
# Flags are matched generically rather than by name. `rtk -v proxy git push` and
# `rtk proxy --skip-env git push` both hid a refusal from the first version of this
# fix, and naming today's four options would only work until rtk gains a fifth.
_RTK = rf"{_PATH}rtk(?:\s+-\S+)*\s+(?:proxy(?:\s+-\S+)*\s+)?"
_WRAPPER = (
    rf"(?:{_ASSIGNMENT}\s+|{_RTK}"
    rf"|{_PATH}(?:command|env|exec|nohup|setsid|sudo)\s+"
    rf"|{_PATH}(?:nice|timeout|stdbuf|time)(?:\s+-\S+|\s+\d+\S*)*\s+)*"
)


def invocation(name: str) -> re.Pattern[str]:
    return re.compile(
        # `(`, `{` and `!` open a command position exactly as `;` does. Without them
        # `(git push origin main)` and `{ rm -rf /; }` matched nothing and every
        # refusal returned silence — and subshells are not among the limits this
        # file's docstring declares, so that was a gap rather than a decision.
        # The tail stops at `)` and `}` as well, or `(git push origin main)` yields the
        # operand `main)` and matches no branch name. Quoted spans are blanked before
        # the search, so a bracket inside an argument is already a space here and only
        # shell syntax is excluded.
        # `then`, `do`, `else` and `elif` open a command position too, and only when
        # they themselves follow a separator — so `if true; then git push origin main`
        # is seen while `echo do git push` is not. Without this, every refusal in the
        # file was bypassed by wrapping the command in `if`, `for` or `while`. Two
        # review seats demonstrated it independently.
        rf"(?:^|[;&|({{!]\s*)\s*(?:(?:then|do|else|elif)\s+)*"
        rf"{_WRAPPER}{_PATH}{name}\b(?P<tail>[^\n;&|)}}]*)",
        flags=re.IGNORECASE,
    )


GIT = invocation("git")
RM = invocation("rm")
GH = invocation("gh")


def without_quoted_text(command: str) -> str:
    """The command with quoted spans blanked, the same length throughout.

    **Quoted text is data.** `printf 'first; rm -rf /; then'` names no deletion and
    `git commit -m "x; git push --force"` names no push, but every pattern here reads
    raw text and both looked like the real thing. This was found by the guard refusing
    a test fixture of its own author's — within an hour of the file being written, and
    exactly the class of unappealable false alarm its docstring warns about.

    Not shell parsing and not pretending to be: single quotes, double quotes, and a
    backslash escape. Enough to stop prose being read as structure.

    **Length is preserved so an offset into the result still maps to the original**,
    which is what lets `invocations` find a command in the blanked text and then read
    its arguments out of the real one. Blanking a quoted *operand* is safe in the
    direction that matters: `rm -rf "$HOME"` loses its target, an empty target list is
    not "all disposable", and the deletion is refused rather than waved through.
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


def invocations(pattern: re.Pattern[str], command: str) -> list[str]:
    """The argument tails of every real invocation of `pattern` in `command`.

    Found in the quote-blanked text so that a command named inside a string is not one,
    and sliced out of the original so its arguments are read as written.
    """
    tails = []
    for match in pattern.finditer(without_quoted_text(command)):
        start, end = match.span("tail")
        tails.append(command[start:end])
    return tails


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


GIT_GLOBAL_WITH_VALUE = ("-c", "-C", "--config-env", "--git-dir", "--work-tree", "--namespace")


def after_global_options(tokens: list[str]) -> list[str]:
    """The tail from the subcommand onward, with git's global options skipped.

    **Every check that asks "which subcommand is this?" has to come through here.**
    Two did not, and one global option turned both of them off: `git -C . commit -n`
    left `tokens[:1]` reading `["-C"]`, so the short `--no-verify` arm never ran, and
    `git -C . config core.hooksPath /dev/null` returned early for the same reason.
    Those are the backstops for the branch refusal, the credential scan and the
    attribution check, and `-C .` is a spelling somebody types by habit rather than to
    evade anything. Found by CodeRabbit on pull request 15 and reproduced before it was
    believed; its own account named `-c` where the token is `-C`, which changes nothing
    about the hole.
    """
    index = 0
    while index < len(tokens):
        token = tokens[index]
        if token == "--":
            return tokens[index + 1 :]
        if token in GIT_GLOBAL_WITH_VALUE:
            index += 2
            continue
        if token.startswith("-"):
            index += 1
            continue
        break
    return tokens[index:]


def git_calls(command: str) -> list[tuple[str, list[str]]]:
    """Recognizable git calls as (subcommand, arguments), skipping global options."""
    calls: list[tuple[str, list[str]]] = []
    for tail in invocations(GIT, command):
        rest = after_global_options(tokenize(tail))
        if rest:
            calls.append((rest[0].lower(), rest[1:]))
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


# -------------------------------------------- the six refusals, and a seventh for agents


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
            return "deny", (f"This checkout stands on main, and hard rule {RULE_THREE} {OFF_MAIN}")
        # **A push from a checkout standing on main is refused in every spelling.**
        # The arm below reads explicit refspecs, so a bare `git push` — the ordinary,
        # undecorated form, which sends the current branch to its upstream — named no
        # token and passed, while this function's own docstring claimed it covered
        # pushing to main. `pre-push` catches it, but only where `install.sh` has run,
        # and CLAUDE.md says that setting never travels. Hard rule 3 does not care
        # which spelling was used.
        if action == "push" and checkout_branch(working_directory(payload)) == MAIN_REF:
            return "deny", (
                f"This checkout stands on main, and hard rule {RULE_THREE} A push from "
                "here reaches main whatever refspec it names, or names none. "
                f"{OFF_MAIN}"
            )
        if action == "push" and any(
            token.lstrip("+").split(":", 1)[-1] in MAIN_NAMES
            for token in arguments
            if not token.startswith("-")
        ):
            return "deny", (
                f"That pushes at main, and hard rule {RULE_THREE} "
                "Main is read-only locally; work reaches it through a pull request."
            )
    return None


# Directories that exist to be thrown away. A recursive delete confined to these is
# ordinary housekeeping; anything else is refused. Judged by target rather than by the
# shape of the command, which is the correction the old guard needed: it refused
# `rm -rf` on the scratch drawer whose own rule is "disposable, delete without
# checking", and that refusal is most of what taught the reflex.
DISPOSABLE_ROOTS = ("workbench/scratch",)
# **A temporary directory is disposable only when it is outside the project root.**
# Read as a bare prefix, this list said the opposite of what it meant: a checkout under
# `/tmp` — a scratch clone, a sandbox, or the throwaway project root this file's own
# tests build — matched in its entirety, so `rm -rf` on that whole tree was waved
# through. macOS hid it, because `project_root()` resolves and `/var/folders/…` becomes
# `/private/var/folders/…`, so the suite passed on the host and failed in a chamber.
#
# An earlier version of this comment said "every chamber and every CI run clones this
# repository under `/tmp`". That is false and a review seat caught it: a chamber clones
# to `/work` and GitHub checks out under `/home/runner/work`. What actually sat under
# `/tmp` in the chamber was pytest's own fixture root. The defect was real; the reason
# given for it was not, and a wrong reason in a comment is what the next reader inherits.
#
# Both resolved and unresolved spellings are listed, because `disposable()` requires
# every spelling of an operand to match before it exempts one.
DISPOSABLE_PREFIXES = ("/tmp/", "/private/tmp/", "/var/folders/", "/private/var/folders/")
DISPOSABLE_NAMES = frozenset({"__pycache__", ".pytest_cache", ".ruff_cache", "node_modules"})

# What a literal path may contain, stated as what IS allowed. This gates an exemption,
# so an incomplete list must fail toward refusing: `normpath` resolves a literal `..`,
# but nothing resolves `$p`, and `p=../../..` with `rm -rf workbench/scratch/$p` leaves
# the drawer entirely.
LITERAL_PATH = re.compile(r"[A-Za-z0-9._/-]+")


def within(path: str, directory: str) -> bool:
    """True when `path` is `directory` itself or lies inside it.

    The separator is appended only when the directory does not already end in one,
    because the root directory is spelled `/` and `//` matches nothing. That case is
    not hypothetical here: `rm -rf /` contains the project, and the containment test
    below is what refuses it.
    """
    if path == directory:
        return True
    prefix = directory if directory.endswith(os.sep) else f"{directory}{os.sep}"
    return path.startswith(prefix)


def disposable(operand: str, payload: dict[str, Any]) -> bool:
    cleaned = operand.strip()
    if not LITERAL_PATH.fullmatch(cleaned):
        return False
    absolute = os.path.normpath(
        cleaned if os.path.isabs(cleaned) else str(working_directory(payload) / cleaned)
    )
    root = str(project_root())
    # Both spellings are judged, because `project_root()` resolves symlinks and a
    # literal operand does not. On macOS a checkout at `/tmp/x` is rooted at
    # `/private/tmp/x`, so comparing only the text as written would call the
    # repository a temporary directory by a different route.
    spellings = {absolute, os.path.realpath(absolute)}

    # **A target that contains the project is never disposable**, whatever it is
    # named and whatever prefix it carries. This is the direction the first version
    # of this fix did not reason in: it asked whether the target was inside the
    # project and, finding it was not, fell through to the temporary-directory
    # exemption. With a checkout at `/tmp/ci/repo`, `rm -rf /tmp/ci` matched `/tmp/`
    # and was allowed — the same repository-destroying accident the anchoring was
    # written to stop, one directory up. Found by all three review seats.
    if any(within(root, spelling) for spelling in spellings):
        return False

    # Judged by basename, so a cache directory anywhere is disposable — including
    # outside the project. Deliberate, and stated so nobody reads it as an oversight.
    # **Every spelling must have a disposable basename**, for the same reason the
    # prefix test below requires it: `node_modules` as a symlink to a real directory
    # elsewhere was waved through on the name alone, which is the one branch of this
    # function that still judged the literal text by itself.
    if all(
        os.path.basename(os.path.normpath(spelling)) in DISPOSABLE_NAMES for spelling in spellings
    ):
        return True

    if any(within(spelling, root) for spelling in spellings):
        # Inside the project, only the named drawers are disposable. The temporary
        # prefixes below never apply here, whatever the checkout is sitting in.
        return all(
            any(within(spelling, f"{root}/{drawer}") for drawer in DISPOSABLE_ROOTS)
            for spelling in spellings
        )

    # **Every** spelling must be disposable, not any one of them. `any` failed open
    # through a symlink: `rm -rf /tmp/link` where the link points at a real directory
    # matched on the literal text alone, and `rm` with a trailing slash follows it.
    return all(
        any(spelling.startswith(prefix) for prefix in DISPOSABLE_PREFIXES) for spelling in spellings
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
    for tail in invocations(RM, command):
        flags: list[str] = []
        targets: list[str] = []
        ended = False
        for token in tokenize(tail):
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
    README.md records reviewers with `Reviewed-by:` trailers after a pass returns — and
    both are recoverable from the reflog. What is not recoverable is a rewrite that has
    reached the remote, or a `reset --hard` that discards a working tree git never saw.
    """
    if tool != "Bash":
        return None
    for action, arguments in git_calls(normalize(bash_command(tool_input))):
        # `+` in front of a refspec is a force-push and takes no flag at all —
        # `git push origin +main` and `git push origin +refs/heads/x:y` both rewrite
        # the remote while matching none of the options above. Found by a review seat.
        if action == "push" and (
            flag(arguments, "--force", "--force-with-lease", "--force-if-includes", short="f")
            or any(token.startswith("+") and len(token) > 1 for token in operands(arguments))
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
    for tail in invocations(GH, command):
        tokens = [token.lower() for token in operands(tokenize(tail))]
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

GIT_CONFIG_ENV_HOOKSPATH = re.compile(r"GIT_CONFIG_KEY_\d+\s*=\s*['\"]?core\.hookspath", re.I)


def hooks_path_is_being_set(tokens: list[str]) -> bool:
    for index, token in enumerate(tokens):
        if token in ("-c", "--config-env") and index + 1 < len(tokens):
            if tokens[index + 1].startswith("core.hookspath"):
                return True
        if token.startswith(("-ccore.hookspath", "--config-env=core.hookspath")):
            return True
    # Resolved through `after_global_options`, not read off the raw tail: `git -C .
    # config core.hooksPath /dev/null` returned False here, because `tokens[:1]` was
    # `["-C"]` and never `["config"]`.
    subcommand = after_global_options(tokens)
    if subcommand[:1] != ["config"]:
        return False
    arguments = subcommand[1:]
    if _HOOKS_PATH_WRITE & set(arguments) and any(
        token.startswith("core.hookspath") for token in arguments
    ):
        return True
    # `git config core.hooksPath <value>` sets it; the same call without a value reads
    # it. **Options that take a value are skipped first**, because `--file .git/config
    # core.hooksPath /dev/null` put the path where the setting name was expected and
    # walked straight past this check — the same spelling family the check exists to
    # close, found by a review seat.
    _TAKES_A_VALUE = {"-f", "--file", "--blob", "--type", "-t", "--default"}
    values = []
    skip = False
    for token in arguments:
        if skip:
            skip = False
            continue
        if token in _TAKES_A_VALUE:
            skip = True
            continue
        if not token.startswith("-"):
            values.append(token)
    return len(values) > 1 and values[0].startswith("core.hookspath")


def switching_the_hooks_off(tool: str, tool_input: Any, payload: dict[str, Any]) -> Decision:
    """6. Turning off the git hooks, which are the backstop for everything above.

    One past the five Tyrel named, and named here rather than folded in quietly. It is
    here because CLAUDE.md says it is: "`--no-verify` and `-c core.hooksPath=` are
    blocked for Claude and open to everything else — hard rule 11. That is his way
    around his own machinery; do not close it."
    Dropping the check would have left that sentence false, and would have left one
    flag between a session and the credential scan, the branch refusal and the
    attribution check together.

    It costs nothing in noise: a session has no legitimate use for either spelling.
    """
    if tool != "Bash":
        return None
    command = normalize(bash_command(tool_input))
    # Git reads `GIT_CONFIG_KEY_<n>` / `GIT_CONFIG_VALUE_<n>` from the environment, so
    # an assignment in front of the command sets `core.hooksPath` without ever writing
    # a config file or passing `-c`. The wrapper pattern already lets assignments
    # precede a command, which is what made this invisible.
    if GIT_CONFIG_ENV_HOOKSPATH.search(command):
        return "deny", (
            "`GIT_CONFIG_KEY_*` sets `core.hooksPath` from the environment, which "
            "switches off the credential scan, the branch refusal and the attribution "
            "check together. That spelling is Tyrel's, not a session's "
            "(CLAUDE.md, Pushing and merging)."
        )
    for tail in invocations(GIT, command):
        tokens = [token.lower() for token in tokenize(tail)]
        if hooks_path_is_being_set(tokens):
            return "deny", (
                "`core.hooksPath` is the one setting that makes any hook run at all. "
                "Pointing it elsewhere switches off the credential scan, the branch "
                "refusal and the attribution check together. This spelling is Tyrel's, "
                "not a session's (CLAUDE.md, Pushing and merging)."
            )
        # Git accepts any unambiguous abbreviation, so `--no-ver` skips the hooks
        # exactly as `--no-verify` does. Matching the prefix is broad on purpose: this
        # refusal costs a session nothing it has a legitimate use for.
        # The short spelling is read off the resolved subcommand, not the raw tail.
        # `git -C . commit -n -m x` left `tokens[:1]` as `["-C"]`, so this arm never
        # ran and the bundled `-n` went through. The long-option scan above reads every
        # token and was never affected, which is what made the gap look closed.
        subcommand = after_global_options(tokens)
        if any(
            token.startswith("--no-v") and "--no-verify".startswith(token) for token in tokens
        ) or (
            subcommand[:1] == ["commit"]
            and any(SAFE_COMMIT_BUNDLE.fullmatch(token) for token in subcommand[1:])
        ):
            return "deny", (
                "`--no-verify` skips the hooks that refuse a commit on main, an "
                "unattributed message and a credential in outgoing history. It is "
                "Tyrel's way around his own machinery and not a session's "
                "(CLAUDE.md, Pushing and merging). Fix what the hook objects to."
            )
    return None


# The seven documents that bind later sessions, plus `.claude/` entire — the skills, the
# roster, this file's own policy. CLAUDE.md's Where notes go is the list.
GOVERNED_NAMES = frozenset(
    {
        "claude.md",
        "goals.md",
        "governance.md",
        "architecture.md",
        "glossary.md",
        "readme.md",
        "data_contract.md",
    }
)

# **`README.md` is governed at the project root and nowhere else.** CLAUDE.md says so in
# as many words — "the root `README.md`" — and matching it by basename refused a spawned
# agent every `operations/`, `cleanroom/` and `workbench/` README in the tree. Hard rule
# 12 makes `operations/` agent-written, so that refusal contradicted the rule it sits
# beside, and a denial is final within a session: the agent had no route forward. It is
# the likeliest explanation for the denials logged inside a chamber on 2026-08-02, where
# sub-agents were asked to update exactly those files. Found by CodeRabbit on pull
# request 15.
#
# The other six names are unique in this repository, so a basename match is still right
# for them. `.claude/` keeps matching at any depth.
ROOT_ONLY_NAMES = frozenset({"readme.md"})
_GOVERNED_ALTERNATION = "|".join(
    sorted(re.escape(name) for name in GOVERNED_NAMES - ROOT_ONLY_NAMES)
)
# The root README in shell text: bare or `./`-prefixed, never after a path separator.
# `/` is in the lookbehind class precisely so `operations/autoclave/README.md` cannot
# begin a match at its final component.
_ROOT_README = r"(?<![\w.\-/])(?:\./)?readme\.md"
# A redirect or an in-place editor naming a governed document. Coarse on purpose: it only
# ever judges an agent, where a false alarm costs one retry and a report, not a prompt.
# `.claude/` at a path boundary, so the bare relative spelling counts as well as an
# absolute one. Requiring a leading slash meant `> .claude/settings.json` passed, and
# so did `> /abs/path/.claude/hooks/guard.py` — the redirect branch only ever matched
# governed *basenames*, and `guard.py` is not one. A spawned agent could overwrite this
# file. The lookbehind keeps `mine.claude/` and `..claude/` out.
_DOT_CLAUDE = r"(?<![\w.])\.claude/"
GOVERNED_SHELL_WRITE = re.compile(
    rf"(?:(?:^|[^0-9<>&])>{{1,2}}\s*(?:\./)?(?:{_GOVERNED_ALTERNATION})\b)"
    rf"|(?:(?:^|[^0-9<>&])>{{1,2}}\s*{_ROOT_README}\b)"
    rf"|(?:(?:^|[^0-9<>&])>{{1,2}}[^\n;&|]*{_DOT_CLAUDE})"
    rf"|(?:\b(?:tee|sed\s+-i|perl\s+-pi?|patch|truncate|cp|mv|install|dd)\b[^\n;&|]*"
    rf"(?:{_GOVERNED_ALTERNATION}|{_ROOT_README}|{_DOT_CLAUDE}))",
    flags=re.IGNORECASE,
)


def subagent_name(payload: dict[str, Any]) -> str | None:
    """The runtime-supplied agent identity, absent on the main thread."""
    for key in ("agent_type", "agent_id"):
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            named = payload.get("agent_type")
            return named.strip() if isinstance(named, str) and named.strip() else "subagent"
    return None


def agent_editing_governance(tool: str, tool_input: Any, payload: dict[str, Any]) -> Decision:
    """7. A *spawned agent* writing a governed path. Silent for the main session.

    This is the one asymmetry in the file, and it is the whole of the balance struck
    when built-in agent types were allowed back in. The session may edit these — it has
    read CLAUDE.md, it is the accountable lead, and prompting it on every governed edit
    is what retired the last guard. An agent has read none of that: `general-purpose`
    and `Explore` are Claude Code's own roles, they carry no project instruction, and
    they hold `Bash` on this machine.

    Hard rule 10 is the rule; this is its mechanical half, applied only where the reader
    of the rule is absent. An agent proposes exact wording in its report instead.
    """
    agent = subagent_name(payload)
    if not agent:
        return None
    refusal = (
        f"Hard rule 10 bars subagent {agent} from editing a governed path — the "
        "documents and `.claude/`, which bind every later session. Propose exact "
        "wording in your report and let the main session make the change."
    )
    if tool in WRITING_TOOLS:
        target = written_path(tool_input, payload)
        if target is None:
            return None
        name = target.name.strip().lower()
        if "/.claude/" in f"{target}/":
            return "deny", refusal
        if name in ROOT_ONLY_NAMES:
            # Governed at the project root and nowhere else. Compared against the
            # resolved root rather than by string, so `./README.md` and an absolute
            # spelling of the same file are one answer.
            if target.parent.resolve() == project_root().resolve():
                return "deny", refusal
            return None
        if name in GOVERNED_NAMES:
            return "deny", refusal
        return None
    if tool == "Bash" and GOVERNED_SHELL_WRITE.search(normalize(bash_command(tool_input))):
        return "deny", refusal
    return None


CHECKS = (
    landing_on_main,
    recursive_delete,
    rewriting_history,
    deleting_a_remote,
    credential_into_git,
    switching_the_hooks_off,
    agent_editing_governance,
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
