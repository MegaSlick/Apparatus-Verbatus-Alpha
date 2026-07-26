#!/usr/bin/env python3
"""Tripwire for the things that cost money or destroy work.

Runs before every Bash command and every MCP tool call. Blocks a short list and
stays out of the way otherwise — a guard that fires on ordinary work gets
disabled, and a disabled guard protects nothing.

What it blocks:
  * bringing up billed RunPod infrastructure   Governance 8: only Tyrel, in-session
  * writing to the RunPod API                  same, by another route
  * deleting a network volume                  irreversible, and the corpus lives there
  * turning the git hooks off                  they are the only enforcement there is
  * force-push, direct push to main            main moves only by merge
  * --no-verify                                one flag defeats every local rule
  * deleting the repository or your home       the obvious one

WHAT THIS IS NOT
================
It is not a security boundary and it cannot be made into one. Two rounds of
adversarial audit found a new way past every version of it, which is the
expected result: inspecting command text can always be defeated by writing the
command differently. Known and unfixed: a script file (`python3 deploy.py`) is
opaque, `bash -c` and `eval` hide their payload, and a command built from
variables reads differently to this than it does to the shell.

It guards **Claude only**. Codex, GPT and a human at a terminal never run it.

The enforcement that binds every tool is the git hooks in `.githooks/`, and
they must be installed — see `.githooks/install.sh`. This file is a tripwire on
top: it catches the honest mistake and the careless agent, promptly and with a
useful message. Treat it as a seatbelt, not a vault.
"""

import json
import os
import re
import shlex
import sys

GOV8 = "Governance 8: a live pod needs Tyrel's explicit permission this session"

RUNPOD_HOSTS = ("runpod.io", "runpod.ai")

RUNPODCTL_BLOCKED = {
    ("create",): (f"creates pods — {GOV8}"),
    ("start",): (f"wakes a stopped pod, which bills again — {GOV8}"),
    ("project", "deploy"): (f"deploys billed infrastructure — {GOV8}"),
    ("project", "dev"): (f"starts a billed session — {GOV8}"),
    ("remove", "pods"): "removes pods in bulk by name — they may not all be yours",
}

# git's own options, before the subcommand, that carry a separate value.
GIT_GLOBAL_VALUE_OPTS = {"-c", "-C", "--git-dir", "--work-tree", "--namespace", "--exec-path"}
# Subcommand options whose value must never be mistaken for an option: this is
# what stops `git commit -m 'document the --no-verify hatch'` being blocked.
GIT_VALUE_OPTS = {
    "-m",
    "--message",
    "-F",
    "--file",
    "-t",
    "--template",
    "--reuse-message",
    "-C",
    "-c",
}

# Wrappers that put the real command one or more tokens further in.
WRAPPERS = {
    "env",
    "sudo",
    "doas",
    "nohup",
    "time",
    "timeout",
    "nice",
    "setsid",
    "stdbuf",
    "command",
    "builtin",
    "exec",
    "xargs",
    "script",
    "uv",
    "poetry",
    "pipenv",
    "hatch",
    "rye",
}

# Wrapper options that swallow the next token. Without these, `sudo -u tyrel
# git push origin main` left `tyrel` sitting in the command position and every
# check below was skipped.
WRAPPER_VALUE_OPTS = {
    "-u",
    "-g",
    "-p",
    "-s",
    "-k",
    "-n",
    "-a",
    "-I",
    "-i",
    "-P",
    "-d",
    "-E",
    "-o",
    "-e",
    "-C",
    "-l",
    "--user",
    "--group",
    "--signal",
    "--kill-after",
    "--replace",
    "--max-procs",
    "--unset",
    "--chdir",
}

# Shell grammar that is never a command name.
KEYWORDS = {
    "if",
    "then",
    "else",
    "elif",
    "fi",
    "do",
    "done",
    "while",
    "for",
    "until",
    "case",
    "esac",
    "in",
    "function",
    "select",
    "!",
    "{",
    "}",
}

WRITE_METHODS = ("post", "put", "patch", "delete")
# `-f` and `-t` are deliberately absent: in curl they are --fail and
# --telnet-option, both read-only, and blocking `curl -f` blocked exactly the
# provider-state check Governance 8 asks for.
WRITE_FLAGS = (
    "-d",
    "--data",
    "--data-raw",
    "--data-binary",
    "--data-urlencode",
    "--json",
    "--post-file",
    "--post-data",
    "--body-data",
    "--upload-file",
    "-F",
    "--form",
)

# The fallback when a command will not tokenise. Deliberately narrow: each of
# these needs a *write* alongside the noun, so that reading about a network
# volume, grepping for it, or naming it in a commit message stays allowed.
FALLBACK = [
    (
        re.compile(r"networkvolume", re.I),
        re.compile(r"\b(delete|-X\s*DELETE|--request\s*=?\s*DELETE|remove|destroy)\b", re.I),
        "deletes a network volume — irreversible, and the corpus lives there",
    ),
    (
        re.compile(
            r"\b(podFindAndDeployOnDemand|podResume|podRentInterruptable|deleteNetworkVolume)\b",
            re.I,
        ),
        re.compile(r"\b(curl|wget|http|python3?|requests|fetch)\b", re.I),
        f"deploys, resumes or destroys RunPod resources via the API — {GOV8}",
    ),
    (
        re.compile(r"\brunpodctl\b", re.I),
        re.compile(r"\b(create|start)\b|\bproject\s+(deploy|dev)\b", re.I),
        f"brings up billed RunPod infrastructure — {GOV8}",
    ),
    (
        re.compile(r"\brm\b"),
        re.compile(r"-[a-zA-Z]*r", re.I),
        "a recursive delete the guard could not parse — rewrite it simply",
    ),
]


def deny(reason: str) -> None:
    print(
        json.dumps(
            {
                "hookSpecificOutput": {
                    "hookEventName": "PreToolUse",
                    "permissionDecision": "deny",
                    "permissionDecisionReason": f"Blocked by repo guard: {reason}. Ask Tyrel.",
                }
            }
        )
    )
    sys.exit(0)


def base(token: str) -> str:
    return token.rsplit("/", 1)[-1]


def normalise(command: str) -> str:
    """Fold a command into one line that shlex can read, quotes intact.

    Splitting on newlines first was wrong twice over. A backslash at the end
    of a line continues it, so `curl ... \\` + `-X POST ...` was read as two
    commands and the second had no recognisable name. And a newline *inside*
    quotes — every multi-line commit message — left an unbalanced quote, which
    threw the parse away entirely and fell back to raw text.

    So: walk the string once, tracking quotes. Unquoted newlines become `;`.
    Quoted ones stay where they are. Line continuations disappear. Comments
    are cut at an unquoted `#` that starts a word, as a shell does.
    """
    out = []
    quote = None
    i = 0
    while i < len(command):
        ch = command[i]
        if quote:
            if ch == "\\" and quote == '"' and i + 1 < len(command):
                out.append(ch)
                out.append(command[i + 1])
                i += 2
                continue
            out.append(ch)
            if ch == quote:
                quote = None
            i += 1
            continue
        if ch == "\\" and i + 1 < len(command):
            if command[i + 1] == "\n":
                i += 2  # line continuation: the newline is not there
                continue
            out.append(ch)
            out.append(command[i + 1])
            i += 2
            continue
        if ch in "'\"":
            quote = ch
            out.append(ch)
            i += 1
            continue
        if ch == "#" and (not out or out[-1].isspace()):
            while i < len(command) and command[i] != "\n":
                i += 1
            continue
        out.append(";" if ch == "\n" else ch)
        i += 1
    return "".join(out)


HEREDOC = re.compile(r"<<-?\s*(['\"]?)([A-Za-z_][A-Za-z_0-9]*)\1")
REDIRECTIONS = {">", ">>", "<", "<<", "<<<", ">&", "<&", ">|"}


def strip_heredocs(command: str) -> str:
    """Drop heredoc bodies. A shell does not execute them; nor should this.

    Without it, `cat > notes.md <<EOF` followed by prose about `rm -rf ~` was
    read as an actual delete — and writing documents about these commands is
    most of what this repository does.
    """
    lines = command.split("\n")
    out, i = [], 0
    while i < len(lines):
        line = lines[i]
        out.append(line)
        found = HEREDOC.search(line)
        i += 1
        if found:
            tag = found.group(2)
            while i < len(lines) and lines[i].strip() != tag:
                i += 1
            i += 1  # and the closing tag itself
    return "\n".join(out)


def segments(command: str):
    """Split a command into argv lists roughly the way a shell would."""
    lexer = shlex.shlex(normalise(strip_heredocs(command)), posix=True, punctuation_chars=True)
    lexer.whitespace_split = True
    lexer.commenters = ""
    out, current = [], []
    skip_next = False
    for token in lexer:
        if skip_next:
            skip_next = False
            continue
        if token in REDIRECTIONS:
            # `2>/dev/null` leaves a bare fd digit in front of the operator,
            # and a leading redirection would otherwise become the command.
            if current and re.fullmatch(r"[0-9]", current[-1]):
                current.pop()
            skip_next = True
            continue
        if token and all(c in ";&|()" for c in token):
            if current:
                out.append(current)
                current = []
        else:
            current.append(token)
    if current:
        out.append(current)
    return out


def peel(argv):
    """Drop VAR=value prefixes, shell keywords and wrappers.

    `ALLOW_FORCE_PUSH=1 git push --force` was invisible to a dispatcher that
    only looked at argv[0] — and that spelling is the one the hooks' own help
    text teaches, so it was the likeliest bypass in the file.
    """
    seen = 0
    while argv and seen < 8:
        head = argv[0]
        if re.fullmatch(r"[A-Za-z_][A-Za-z_0-9]*=.*", head) or head in KEYWORDS:
            argv = argv[1:]
        elif base(head) in WRAPPERS:
            # Drop the wrapper and its own arguments — `timeout 300 cmd`,
            # `nice -n 5 cmd`, `sudo -u tyrel cmd`. Crucially, drop the VALUE
            # a wrapper option takes as well: leaving it behind made `tyrel`
            # the command name, and every check was skipped.
            rest = argv[1:]
            while rest:
                token = rest[0]
                if token.startswith("-"):
                    takes_value = token in WRAPPER_VALUE_OPTS and "=" not in token
                    rest = rest[2:] if takes_value and len(rest) > 1 else rest[1:]
                elif (
                    re.fullmatch(r"[0-9.]+[smhd]?", token)
                    or re.fullmatch(r"[A-Za-z_][A-Za-z_0-9]*=.*", token)
                    or token in ("run", "exec", "--")
                ):
                    # `uv run python -c ...`, `poetry run python -c ...`
                    rest = rest[1:]
                else:
                    break
            argv = rest
        else:
            return argv
        seen += 1
    return argv


def options(argv, value_opts):
    """Only the tokens that are genuinely options, with their values skipped."""
    skip = False
    for token in argv:
        if skip:
            skip = False
            continue
        if token in value_opts:
            skip = True
            continue
        if token.startswith("-"):
            yield token


# --------------------------------------------------------------------- rules


def check_git(argv):
    opts, sub, args = [], None, []
    i = 1
    while i < len(argv):
        token = argv[i]
        if token in GIT_GLOBAL_VALUE_OPTS:
            opts.append(token)
            if i + 1 < len(argv):
                opts.append(argv[i + 1])
            i += 2
        elif token.startswith("-"):
            opts.append(token)
            i += 1
        else:
            sub, args = token, argv[i + 1 :]
            break
    if sub is None:
        return

    # Turning the hooks off defeats the branch guard, the stray-note check and
    # the audit gate at once. Both the one-shot `-c` form and the permanent
    # `git config` form, which is worse because it binds every later tool.
    if any("core.hookspath" in o.lower() for o in opts):
        deny("disables the git hooks, which are the only enforcement here")
    if sub == "config" and any("core.hookspath" in a.lower() for a in args):
        deny("permanently disables the git hooks for every tool in this clone")

    flags = list(options(args, GIT_VALUE_OPTS))

    if sub in ("commit", "merge", "push", "rebase", "am", "cherry-pick"):
        if "--no-verify" in flags:
            deny("--no-verify skips the hooks that protect main, the documents and the audit gate")
        if sub == "commit" and any(
            f.startswith("-") and not f.startswith("--") and "n" in f[1:] for f in flags
        ):
            deny("git commit -n is --no-verify, which skips the hooks")

    if sub == "push":
        # `-fu` and `-uf` are the same as `-f`; whole-token comparison missed them.
        if any(
            f == "--force"
            or f.startswith("--force-with-lease")
            or (f.startswith("-") and not f.startswith("--") and "f" in f[1:])
            for f in flags
        ):
            deny(
                "force-push destroys work another agent may be holding "
                "(ALLOW_FORCE_PUSH=1 if you truly mean it)"
            )
        for a in args:
            if a.startswith("-"):
                continue
            if a.startswith("+") and ":" in a:
                deny("a leading + on a refspec is a force-push by another name")
            target = a.split(":")[-1]
            if target in ("main", "refs/heads/main"):
                deny("main moves only by merging a pull request")


def check_runpodctl(argv):
    if any(f in ("--help", "-h") for f in options(argv[1:], set())):
        return  # reading the manual costs nothing
    verbs = tuple(a for a in argv[1:] if not a.startswith("-"))
    if verbs[:1] == ("help",):
        return
    for pattern, why in RUNPODCTL_BLOCKED.items():
        if verbs[: len(pattern)] == pattern:
            deny(why)


def check_http(argv):
    joined = " ".join(argv).lower()
    if not any(host in joined for host in RUNPOD_HOSTS):
        return
    # curl -G turns --data-* into a query string: the request is a GET.
    if "-g" in [a.lower() for a in argv] or "--get" in joined:
        return
    writes = False
    for i, token in enumerate(argv):
        low = token.lower()
        flag = low.split("=", 1)[0]
        if flag in WRITE_FLAGS or flag.startswith("--data"):
            writes = True
        if flag in ("-x", "--request", "--method"):
            value = (
                low.split("=", 1)[1]
                if "=" in low
                else (argv[i + 1].lower() if i + 1 < len(argv) else "")
            )
            if value in WRITE_METHODS:
                writes = True
        # curl accepts the value attached: -XPOST, -XDELETE.
        if low.startswith("-x") and len(low) > 2 and low[2:] in WRITE_METHODS:
            writes = True
        if base(low) == "post" or low in WRITE_METHODS:
            writes = True  # httpie takes the method as a positional
    if "graphql" in joined and "query=" not in joined:
        writes = True
    if not writes:
        return
    if "networkvolume" in joined:
        deny("writes to a network volume — irreversible, and the corpus lives there")
    # Shutting a pod down saves money, and Governance 8 requires shutdown to be
    # *verified* against provider state. A guard that blocks stopping a pod is
    # working against the rule it exists to serve.
    if any(word in joined for word in ("/stop", "/terminate", "/pods/")) and not any(
        word in joined for word in ("/start", "/resume")
    ):
        if "stop" in joined or "terminate" in joined or "-xdelete" in joined or "delete" in joined:
            return
    deny(f"writes to the RunPod API, which creates or destroys billed resources — {GOV8}")


def check_python(argv):
    inline = " ".join(argv)
    if not re.search(r"\brunpod\b", inline):
        return
    if re.search(
        r"\b(create_pod|resume_pod|start_pod|create_endpoint|"
        r"create_template|delete_network_volume)\b",
        inline,
    ):
        deny(f"brings up or destroys RunPod resources through the SDK — {GOV8}")


def _precious():
    """Paths whose recursive deletion is never an accident worth allowing."""
    project = os.environ.get("CLAUDE_PROJECT_DIR") or ""
    if not project:
        # An unset variable must not silently narrow the rule to $HOME alone.
        project = os.getcwd()
    project = os.path.realpath(project)
    home = os.path.realpath(os.path.expanduser("~"))
    return [
        project,
        os.path.join(project, ".git"),
        os.path.join(project, ".githooks"),
        os.path.join(project, ".claude"),
        home,
    ]


def check_rm(argv):
    recursive = False
    operands = []
    for token in argv[1:]:
        if token == "--recursive":
            recursive = True
        elif token.startswith("--"):
            continue
        elif token.startswith("-") and len(token) > 1:
            recursive = recursive or "r" in token.lower()
        else:
            operands.append(token)
    if not recursive:
        return

    # Deny what is precious, rather than allowing only what is scratch. The
    # allowlist version blocked `rm -rf .venv`, `rm -rf __pycache__` and
    # `rm -rf workbench/scratch/*` — the last of which CLAUDE.md says outright
    # that anyone may delete without asking.
    roots = _precious()
    for operand in operands:
        # `$HOME` must resolve. An unknown variable stays literal, which lands
        # somewhere harmless and relative, and that is the right default.
        expanded = os.path.expandvars(os.path.expanduser(operand))

        # A glob deletes the *contents* of its directory, so judge the
        # directory. `rm -rf ~/*` empties your home; `rm -rf *` at the repo
        # root empties the repo. Both were allowed while the bare `rm -rf ~`
        # that nobody types was blocked.
        # By name, wherever it sits: a relative path resolves against the
        # guard's own working directory, which is not necessarily the project.
        if os.path.basename(expanded.rstrip("/")) in (".git", ".githooks", ".claude"):
            deny(
                f"recursive delete of '{operand}' — that is the repository's "
                f"history, its hooks, or the guard itself"
            )

        head = expanded
        if any(c in expanded for c in "*?["):
            head = os.path.dirname(expanded.split("*")[0].split("?")[0].split("[")[0]) or "."

        # realpath follows symlinks, normpath collapses `..` lexically, and
        # `/tmp/../Users/...` resolves differently under each.
        for path in {os.path.realpath(head), os.path.normpath(os.path.abspath(head))}:
            if path == "/" or path in roots:
                deny(
                    f"recursive delete of '{operand}' — that is the "
                    f"repository, your home directory, or the guts of one"
                )
            for root in roots:
                if root.startswith(path + os.sep):
                    deny(
                        f"recursive delete of '{operand}' — it contains the "
                        f"repository or your home directory"
                    )


def check_aws(argv):
    # `aws --profile prod s3 rm ...` puts the profile's value in the way, so
    # look for the verb pair wherever it falls rather than at a fixed offset.
    rest = [a for a in argv[1:] if not a.startswith("-")]
    for i, token in enumerate(rest[:-1]):
        if token != "s3":
            continue
        if rest[i + 1] == "rm" and "--recursive" in argv:
            deny("recursive S3 delete — irreversible")
        if rest[i + 1] == "rb" and "--force" in argv:
            deny("deletes an S3 bucket and everything in it — irreversible")


DISPATCH = {
    "git": check_git,
    "runpodctl": check_runpodctl,
    "runpod": check_runpodctl,
    "curl": check_http,
    "wget": check_http,
    "http": check_http,
    "https": check_http,
    "python": check_python,
    "python3": check_python,
    "rm": check_rm,
    "aws": check_aws,
}

# Verbs that start the meter. `stop`, `terminate` and `delete` are absent on
# purpose: they end billing, and Governance 8 requires shutdown to be verified
# against provider state. Blocking them would work against the rule.
BLOCKED_TOOL_WORDS = ("create", "start", "resume", "deploy", "launch", "rent")


def check_tool(tool: str) -> None:
    """MCP tools. Server names are arbitrary; the verbs are not."""
    lowered = tool.lower()
    if "runpod" not in lowered:
        return
    action = lowered.rsplit("__", 1)[-1]
    if "volume" in action and any(w in action for w in ("delete", "remove", "destroy")):
        deny(f"'{tool}' destroys a network volume — the corpus lives there")
    if any(word in action for word in BLOCKED_TOOL_WORDS):
        deny(f"'{tool}' creates, starts or deploys RunPod resources — {GOV8}")


def inspect(command: str) -> None:
    try:
        parsed = segments(command)
    except ValueError:
        for noun, context, reason in FALLBACK:
            if noun.search(command) and context.search(command):
                deny(reason)
        return

    for argv in parsed:
        argv = peel(argv)
        if not argv:
            continue
        name = base(argv[0])
        # python3.11, python3.13 — a version suffix is not a different program.
        handler = DISPATCH.get(name) or (check_python if name.startswith("python") else None)
        if handler:
            handler(argv)


def main() -> None:
    try:
        payload = json.load(sys.stdin)
        if not isinstance(payload, dict):
            raise ValueError("payload is not an object")
        # Check the type before coercing: `[] or ""` is `""`, which would let a
        # malformed tool_name through as an innocent empty string.
        tool = payload.get("tool_name")
        if tool is None:
            tool = ""
        if not isinstance(tool, str):
            raise ValueError("tool_name is not a string")
        tool_input = payload.get("tool_input") or {}

        check_tool(tool)

        if tool == "Bash":
            command = tool_input.get("command") if isinstance(tool_input, dict) else None
            if not isinstance(command, str):
                raise ValueError("no command to read")
            inspect(command)
    except SystemExit:
        raise
    except Exception:
        # A guard that cannot read its input must never be mistaken for one
        # that looked and approved — GOVERNANCE.md 10.
        deny("the guard could not read this tool call, so it cannot vouch for it")

    sys.exit(0)


if __name__ == "__main__":
    main()
