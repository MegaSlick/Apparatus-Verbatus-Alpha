#!/usr/bin/env python3
"""Tripwire for the things that cost money or destroy work.

Runs before every Bash command and every MCP tool call. Blocks a short list and
stays out of the way otherwise — a guard that fires on ordinary work gets
disabled, and a disabled guard protects nothing.

What it recognises — phrasings, not operations. Each line names the spellings
this file actually inspects. It does not block the operation; it blocks the way
of writing it. GOVERNANCE.md 10 — "claims are made only about what was actually
measured" — makes the second list below part of this claim rather than a
disclaimer on it, and every entry in it was run against this guard.

  * RunPod launches spelled as `runpodctl create|start|remove pods`,
    `runpodctl project deploy|dev`, or an MCP tool whose name holds "runpod"
    and a starting verb                        Governance 8: only Tyrel, in-session
  * RunPod API writes spelled as curl, wget, HTTPie, the runpod Python SDK,
    inline `requests`/`httpx`/`urllib`/`http.client`, or an inline
    node/bun/deno script                       same, by another route
  * network-volume deletes in those same clients and tools
  * core.hooksPath writes spelled as `git -c`, `git config` in either the flag
    or the 2.46 subcommand form, or dropping the [core] section
  * pushes at main spelled as `git push` or `git send-pack`, force-push in any
    spelling, and `--no-verify`                main moves only by merge
  * `git clean` without --dry-run              ignored evidence is not recoverable
  * `rm` of the repository, .git/.githooks/.claude, your home, or workbench
    records outside scratch

WHAT THIS IS NOT
================
It is not a security boundary and it cannot be made into one. Four rounds of
adversarial audit found a new way past every version of it, which is the
expected result: inspecting command text can always be defeated by writing the
command differently. So the honest form of the list above is the list below.

What it does not catch — each one verified against this file, not suspected:
  * a script file. `sh deploy.sh`, `python3 deploy.py` and `node deploy.js` are
    opaque; only the inline `-c` / `-e` forms are read at all.
  * a command assembled from variables, or arriving base64-encoded through a
    pipe. Any encoding is invisible here.
  * a command substitution that sits inside double quotes outside a heredoc:
    `echo "$(git push origin main)"` is allowed, while the same substitution
    unquoted, or anywhere in an unquoted heredoc body, is refused.
  * HTTP clients other than those named above, and every language runtime other
    than Python and node/bun/deno — `perl -e` reaches the API untouched.
  * git plumbing spelled as its own binary — `git-push`, `git-send-pack` —
    rather than as `git <subcommand>`.

`bash -c` and `eval` payloads are recursively inspected, up to three deep, and
so are command substitutions in a heredoc body. Executable and ambiguous
heredocs are refused because this guard cannot validate arbitrary code. A
`cat`/`tee` data heredoc is allowed only while it is really data: a quoted
delimiter (`<<'EOF'`) makes the body literal, while a bare `<<EOF` lets the
shell run `$(...)` in it before `cat` ever starts, so those substitutions are
inspected as the commands they are.

It guards **Claude only**. Codex, GPT and a human at a terminal never run it.

Installed Git hooks apply to Git operations made in that clone by any tool that
does not deliberately bypass them. They do not govern direct filesystem,
process, or network actions. See `.githooks/install.sh`. This file is a Claude
tripwire on top: it catches the honest mistake and the careless agent, promptly
and with a useful message. Treat it as a seatbelt, not a vault.
"""

import json
import os
import re
import shlex
import sys
from urllib.parse import urlsplit

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
GIT_PUSH_VALUE_OPTS = GIT_VALUE_OPTS | {
    "--exec",
    "--push-option",
    "--receive-pack",
    "--repo",
    "-o",
    "-r",
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
    # rtk is this machine's always-on command proxy, so `rtk git push --force`
    # is an ordinary thing to type and must not skip every check.
    "rtk",
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
CURL_WRITE_FLAGS = (
    "--form",
    "--form-string",
    "--json",
    "--upload-file",
)
WGET_WRITE_FLAGS = (
    "--post-file",
    "--post-data",
    "--body-data",
    "--body-file",
)

# curl permits joined short options. Options that take a value consume the
# remainder of their token, so `-sFname=x` contains a real -F while
# `-HContent-Type:x` does not contain a -T. This set comes from curl's short
# option table and lets the guard stop parsing at the right place.
CURL_SHORT_VALUE_OPTS = frozenset("AbcCdDeEFHhKmoPQrTtUuwxXyYz")

# Long curl options that consume the following token when no `=` is used.
# Keeping this explicit is what distinguishes `curl --get ...` from
# `curl --header --get ...`, where `--get` is merely the header value.
CURL_LONG_VALUE_OPTS = frozenset(
    {
        "--abstract-unix-socket",
        "--alt-svc",
        "--aws-sigv4",
        "--cacert",
        "--capath",
        "--cert",
        "--cert-type",
        "--ciphers",
        "--config",
        "--connect-timeout",
        "--connect-to",
        "--continue-at",
        "--cookie",
        "--cookie-jar",
        "--create-file-mode",
        "--crlfile",
        "--curves",
        "--data",
        "--data-ascii",
        "--data-binary",
        "--data-raw",
        "--data-urlencode",
        "--delegation",
        "--dns-interface",
        "--dns-ipv4-addr",
        "--dns-ipv6-addr",
        "--dns-servers",
        "--doh-url",
        "--dump-header",
        "--egd-file",
        "--engine",
        "--etag-compare",
        "--etag-save",
        "--expect100-timeout",
        "--form",
        "--form-string",
        "--ftp-account",
        "--ftp-alternative-to-user",
        "--ftp-method",
        "--ftp-port",
        "--ftp-ssl-ccc-mode",
        "--haproxy-clientip",
        "--header",
        "--help",
        "--hostpubmd5",
        "--hostpubsha256",
        "--hsts",
        "--interface",
        "--ipfs-gateway",
        "--json",
        "--keepalive-time",
        "--key",
        "--key-type",
        "--krb",
        "--libcurl",
        "--limit-rate",
        "--local-port",
        "--login-options",
        "--mail-auth",
        "--mail-from",
        "--mail-rcpt",
        "--max-filesize",
        "--max-redirs",
        "--max-time",
        "--netrc-file",
        "--noproxy",
        "--oauth2-bearer",
        "--output",
        "--output-dir",
        "--parallel-max",
        "--pass",
        "--pinnedpubkey",
        "--preproxy",
        "--proto",
        "--proto-default",
        "--proto-redir",
        "--proxy",
        "--proxy-cacert",
        "--proxy-capath",
        "--proxy-cert",
        "--proxy-cert-type",
        "--proxy-ciphers",
        "--proxy-crlfile",
        "--proxy-header",
        "--proxy-key",
        "--proxy-key-type",
        "--proxy-pass",
        "--proxy-pinnedpubkey",
        "--proxy-service-name",
        "--proxy-tls13-ciphers",
        "--proxy-tlsauthtype",
        "--proxy-tlspassword",
        "--proxy-tlsuser",
        "--proxy-user",
        "--proxy1.0",
        "--pubkey",
        "--quote",
        "--random-file",
        "--range",
        "--rate",
        "--referer",
        "--request",
        "--request-target",
        "--resolve",
        "--retry",
        "--retry-delay",
        "--retry-max-time",
        "--sasl-authzid",
        "--service-name",
        "--socks4",
        "--socks4a",
        "--socks5",
        "--socks5-gssapi-service",
        "--socks5-hostname",
        "--speed-limit",
        "--speed-time",
        "--stderr",
        "--telnet-option",
        "--tftp-blksize",
        "--time-cond",
        "--tls-max",
        "--tls13-ciphers",
        "--tlsauthtype",
        "--tlspassword",
        "--tlsuser",
        "--trace",
        "--trace-ascii",
        "--trace-config",
        "--unix-socket",
        "--upload-file",
        "--upload-flags",
        "--url",
        "--url-query",
        "--user",
        "--user-agent",
        "--variable",
        "--write-out",
    }
)

# HTTPie request-item separators. The first separator decides the item's kind:
# header (`:`), query (`==`), string/JSON body (`=` / `:=`), or file body (`@`).
HTTPIE_ITEM = re.compile(r"^[^:=@]+(:=|==|=|@|:)")
HTTPIE_VALUE_OPTS = frozenset(
    {
        "-a",
        "-o",
        "-p",
        "--auth",
        "--boundary",
        "--cert",
        "--cert-key",
        "--ciphers",
        "--format-options",
        "--max-headers",
        "--output",
        "--print",
        "--proxy",
        "--raw",
        "--session",
        "--session-read-only",
        "--ssl",
        "--style",
        "--timeout",
        "--verify",
    }
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


REDIRECTIONS = {">", ">>", "<", "<<", "<<<", ">&", "<&", ">|"}
INPUT_REDIRECTIONS = {"<", "<<", "<<<", "<&"}


def heredoc_declarations(line: str):
    """Return real heredocs as ``(operator, delimiter-or-None, expands)`` triples.

    Only a bare identifier or one wholly single/double-quoted identifier is
    supported. Mixed quoting, escapes and shell expansions are deliberately
    marked unsupported rather than emulated incompletely.

    ``expands`` says whether the shell will expand the body before the reading
    command ever sees it. Verified against bash: with a bare delimiter,
    ``$(echo RAN)`` in the body prints ``RAN``; with ``'EOF'``, ``"EOF"`` or
    ``\\EOF`` it prints ``$(echo RAN)`` literally. That difference decides
    whether a body is data or code.
    """
    out = []
    quote = None
    i = 0
    while i < len(line):
        ch = line[i]
        if quote:
            if ch == "\\" and quote == '"' and i + 1 < len(line):
                i += 2
                continue
            if ch == quote:
                quote = None
            i += 1
            continue
        if ch == "\\" and i + 1 < len(line):
            i += 2
            continue
        if ch in "'\"":
            quote = ch
            i += 1
            continue
        if ch == "#" and (i == 0 or line[i - 1].isspace()):
            break
        if line.startswith("<<<", i):
            i += 3
            continue
        if not line.startswith("<<", i):
            i += 1
            continue

        operator = "<<"
        i += 2
        if i < len(line) and line[i] == "-":
            operator = "<<-"
            i += 1
        while i < len(line) and line[i].isspace():
            i += 1
        if i >= len(line):
            break

        start = i
        word_quote = None
        while i < len(line):
            current = line[i]
            if word_quote:
                if current == "\\" and word_quote == '"' and i + 1 < len(line):
                    i += 2
                    continue
                if current == word_quote:
                    word_quote = None
                i += 1
                continue
            if current in "'\"":
                word_quote = current
                i += 1
                continue
            if current == "\\" and i + 1 < len(line):
                i += 2
                continue
            if current.isspace() or current in ";&|()<>":
                break
            i += 1
        raw = line[start:i]
        identifier = r"[A-Za-z_][A-Za-z_0-9]*"
        found = re.fullmatch(rf"(?:({identifier})|'({identifier})'|\"({identifier})\")", raw)
        delimiter = next((group for group in found.groups() if group), None) if found else None
        # Only group 1 — the bare, unquoted spelling — leaves the body exposed
        # to expansion. Any quoting at all makes it literal.
        expands = bool(found) and found.group(1) is not None
        out.append((operator, delimiter, expands))
    return out


def split_heredocs(command: str):
    """Remove all bodies and return them with their opening command lines."""
    lines = command.split("\n")
    cleaned, blocks, i = [], [], 0
    while i < len(lines):
        line = lines[i]
        cleaned.append(line)
        declarations = heredoc_declarations(line)
        i += 1
        for operator, tag, expands in declarations:
            if tag is None:
                blocks.append((line, "", False, False))
                continue
            j = i
            while j < len(lines):
                terminator = lines[j].lstrip("\t") if operator == "<<-" else lines[j]
                if terminator == tag:
                    break
                j += 1
            blocks.append((line, "\n".join(lines[i:j]), True, expands))
            # An unterminated heredoc consumes the rest of the shell input as
            # data. There can be no later command line to inspect.
            i = j + 1
            if j == len(lines):
                i = j
                break
    return "\n".join(cleaned), blocks


def strip_heredocs(command: str) -> str:
    """Drop data bodies: the shell does not execute them as commands."""
    return split_heredocs(command)[0]


def heredoc_blocks(command: str):
    """Return heredocs as ``(opening line, body, supported, expands)`` tuples."""
    return split_heredocs(command)[1]


def _substitution_end(text: str, start: int) -> int:
    """Index just past the ``)`` closing the ``$(`` whose ``(`` sits at ``start``.

    Inside a substitution the payload is ordinary shell text, so quoting counts
    again: a ``)`` in `'a)b'` closes nothing. Nesting is handled by depth, which
    also carries `$(( ))` arithmetic through harmlessly.
    """
    depth = 0
    quote = None
    i = start
    while i < len(text):
        ch = text[i]
        if quote == "'":
            if ch == "'":
                quote = None
            i += 1
            continue
        if ch == "\\" and i + 1 < len(text):
            i += 2
            continue
        if quote == '"':
            if ch == '"':
                quote = None
            i += 1
            continue
        if ch in "'\"":
            quote = ch
            i += 1
            continue
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
            if depth == 0:
                return i + 1
        i += 1
    raise ValueError("unbalanced command substitution")


def command_substitutions(text: str):
    """The shell code inside ``$( )`` and backticks, where expansion applies.

    Raises ValueError if a substitution is opened and never closed — the guard
    then cannot say what would run, which is a refusal and not a pass.

    In an expanding heredoc body, quotes are *not* special (bash prints the
    quotes verbatim) but a backslash still escapes ``$``, a backtick and
    itself. So the walk here honours escapes only.
    """
    out = []
    i = 0
    while i < len(text):
        ch = text[i]
        if ch == "\\" and i + 1 < len(text):
            i += 2
            continue
        if ch == "`":
            j = i + 1
            while j < len(text):
                if text[j] == "\\" and j + 1 < len(text):
                    j += 2
                    continue
                if text[j] == "`":
                    break
                j += 1
            if j >= len(text):
                raise ValueError("unterminated backtick substitution")
            out.append(text[i + 1 : j])
            i = j + 1
            continue
        if text.startswith("$(", i):
            end = _substitution_end(text, i + 1)
            out.append(text[i + 2 : end - 1])
            i = end
            continue
        i += 1
    return out


def segments(command: str):
    """Split a command into argv lists roughly the way a shell would."""
    return [argv for argv, _, _ in annotated_segments(command)]


def annotated_segments(command: str):
    """Split commands while retaining whether stdin is piped or redirected."""
    lexer = shlex.shlex(normalise(strip_heredocs(command)), posix=True, punctuation_chars=True)
    lexer.whitespace_split = True
    lexer.commenters = ""
    out, current = [], []
    skip_next = False
    piped_input = False
    redirected_input = False
    for token in lexer:
        if skip_next:
            skip_next = False
            continue
        if token in REDIRECTIONS:
            # `2>/dev/null` leaves a bare fd digit in front of the operator,
            # and a leading redirection would otherwise become the command.
            if current and re.fullmatch(r"[0-9]", current[-1]):
                current.pop()
            if token in INPUT_REDIRECTIONS:
                redirected_input = True
            skip_next = True
            continue
        if token and all(c in ";&|()" for c in token):
            if current:
                out.append((current, piped_input, redirected_input))
                current = []
            piped_input = token in ("|", "|&")
            redirected_input = False
        else:
            current.append(token)
    if current:
        out.append((current, piped_input, redirected_input))
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
                    # One wrapper's value-taking flag is another's boolean:
                    # `sudo -u tyrel cmd` takes a value, `sudo -E cmd` does not.
                    # Never swallow a token that is itself a command we check,
                    # or a token that is another flag — `sudo -E -u tyrel cmd`
                    # ate the `-u`, left `tyrel` in the command position, and
                    # every check below was skipped.
                    takes_value = (
                        token in WRAPPER_VALUE_OPTS
                        and "=" not in token
                        and len(rest) > 1
                        and not rest[1].startswith("-")
                        and base(rest[1]) not in DISPATCH
                        and not base(rest[1]).startswith("python")
                    )
                    rest = rest[2:] if takes_value else rest[1:]
                elif (
                    re.fullmatch(r"[0-9.]+[smhd]?", token)
                    or re.fullmatch(r"[A-Za-z_][A-Za-z_0-9]*=.*", token)
                    or token in ("run", "exec", "proxy", "--")
                ):
                    # `uv run python -c ...`, `rtk proxy git push ...`
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


def positional_args(argv, value_opts):
    """Return operands without mistaking option values for operands."""
    out = []
    skip = False
    options_ended = False
    for token in argv:
        if skip:
            skip = False
            continue
        if not options_ended and token == "--":
            options_ended = True
            continue
        if not options_ended and token in value_opts:
            skip = True
            continue
        if not options_ended and token.startswith("-"):
            continue
        out.append(token)
    return out


def has_short_flag(argv, wanted: str, value_options=frozenset()):
    """Whether a combined short-option token contains one Boolean flag.

    Once a value-taking option appears, the rest of that token is its value:
    `-omain` is push option `-o` with value `main`, not `-o -m -a -i -n`.
    """
    for token in argv:
        if not token.startswith("-") or token.startswith("--") or token == "-":
            continue
        for option in token[1:]:
            if option in value_options:
                break
            if option == wanted:
                return True
    return False


def curl_options(argv):
    """Yield parsed curl options without re-reading option values as flags."""
    i = 1
    while i < len(argv):
        token = argv[i]
        if token == "--":
            return
        if token.startswith("--"):
            name, separator, attached = token.partition("=")
            name = name.lower()
            value = attached if separator else ""
            if name in CURL_LONG_VALUE_OPTS and not separator and i + 1 < len(argv):
                value = argv[i + 1]
                i += 1
            yield name, value
            i += 1
            continue
        if token.startswith("-") and token != "-":
            short = token[1:]
            j = 0
            while j < len(short):
                option = short[j]
                if option in CURL_SHORT_VALUE_OPTS:
                    value = short[j + 1 :] if j + 1 < len(short) else ""
                    if not value and i + 1 < len(argv):
                        value = argv[i + 1]
                        i += 1
                    yield option, value
                    break
                yield option, ""
                j += 1
        i += 1


def httpie_body_item(token: str) -> bool:
    """Whether one HTTPie request item supplies a body rather than a read."""
    if token.startswith("-") or token.lower().startswith(("http://", "https://")):
        return False
    if token.startswith("@"):
        return True
    found = HTTPIE_ITEM.match(token)
    return bool(found and found.group(1) in ("=", ":=", "@"))


def httpie_method(argv):
    """Return HTTPie's method from its positional slot, skipping option values."""
    positional = []
    skip = False
    for token in argv[1:]:
        if skip:
            skip = False
            continue
        if token.startswith("-"):
            name = token.split("=", 1)[0]
            if name in HTTPIE_VALUE_OPTS and "=" not in token:
                skip = True
            continue
        if any(host in token.lower() for host in RUNPOD_HOSTS):
            break
        positional.append(token)
    if positional and positional[-1].lower() in (
        "get",
        "head",
        "options",
        *WRITE_METHODS,
    ):
        return positional[-1].lower()
    return None


# --------------------------------------------------------------------- rules

# Git 2.46 gave `git config` subcommands beside its old flags. Both spellings
# reach the same setting, so both are judged here; only the read forms pass.
CONFIG_READ_SUBCOMMANDS = frozenset({"get", "list"})
CONFIG_WRITE_SUBCOMMANDS = frozenset(
    {
        "set",
        "unset",
        "unset-all",
        "replace-all",
        "add",
        "remove-section",
        "rename-section",
    }
)
CONFIG_READ_OPTS = frozenset(
    {"--get", "--get-all", "--get-regexp", "--get-urlmatch", "--list", "-l"}
)
CONFIG_SECTION_OPS = frozenset({"--remove-section", "--rename-section"})


def check_git_config(args):
    """Let every read of core.hooksPath through; refuse every write to it.

    Reading is how you check `install.sh` worked, and the script invites you to.
    Refusing the read told a session that inspecting its own hooks was
    destructive, which is how a guard ends up switched off wholesale.
    """
    lowered = [a.lower() for a in args]
    positional = [a for a in args if not a.startswith("-")]
    verb = positional[0].lower() if positional else None

    # Deleting or renaming [core] takes core.hooksPath with it, and the key's
    # name never appears in the command.
    if any(a in CONFIG_SECTION_OPS for a in lowered) or verb in (
        "remove-section",
        "rename-section",
    ):
        sections = [p.lower() for p in positional if p.lower() not in CONFIG_WRITE_SUBCOMMANDS]
        if any(s == "core" or s.startswith("core.") for s in sections):
            deny("dropping the [core] section takes core.hooksPath with it and disables the hooks")

    if not any("core.hookspath" in a for a in lowered):
        return

    if verb in CONFIG_WRITE_SUBCOMMANDS:
        deny("permanently disables the git hooks for every tool in this clone")
    if verb in CONFIG_READ_SUBCOMMANDS:
        return

    # The flag spellings. A write always carries either a value as a second
    # positional argument or a mutating flag (--add, --unset, --replace-all,
    # --edit), so "exactly one positional and no flags at all" is read-only by
    # construction. --unset stays denied: removing the setting disables the
    # hooks just as thoroughly as overwriting it.
    reading = any(a in CONFIG_READ_OPTS for a in lowered)
    if len(positional) == 1 and len(args) == 1:
        reading = True
    if not reading:
        deny("permanently disables the git hooks for every tool in this clone")


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
        deny("disables the cross-tool Git alarms for this clone")
    if sub == "config":
        check_git_config(args)

    flags = list(options(args, GIT_VALUE_OPTS))

    if sub == "clean":
        dry_run = "--dry-run" in flags or has_short_flag(args, "n", frozenset({"e"}))
        if not dry_run:
            deny("git clean deletes untracked or ignored work that Git cannot restore")

    if sub in ("commit", "merge", "push", "rebase", "am", "cherry-pick"):
        if "--no-verify" in flags:
            deny("--no-verify skips the hooks that protect main, the documents and the audit gate")
        if sub == "commit" and has_short_flag(args, "n", frozenset({"m", "F", "t", "C", "c"})):
            deny("git commit -n is --no-verify, which skips the hooks")

    # `send-pack` is the plumbing behind `push`: same remote, same refspecs,
    # same reach into main, without the word "push" appearing anywhere.
    if sub in ("push", "send-pack"):
        flags = list(options(args, GIT_PUSH_VALUE_OPTS))
        dry_run = "--dry-run" in flags or has_short_flag(args, "n", frozenset({"o", "r"}))
        if dry_run:
            return

        if any(f in ("--all", "--branches") for f in flags):
            deny("push --all can update main along with the intended branches")
        if "--mirror" in flags:
            deny("push --mirror force-updates and deletes remote refs")

        # `-fu` and `-uf` are the same as `-f`; whole-token comparison missed them.
        if any(
            f == "--force" or f.startswith("--force-with-lease") for f in flags
        ) or has_short_flag(args, "f", frozenset({"o", "r"})):
            deny(
                "force-push destroys work another agent may be holding "
                "(ALLOW_FORCE_PUSH=1 if you truly mean it)"
            )
        positional = positional_args(args, GIT_PUSH_VALUE_OPTS)
        repo_by_option = any(a == "--repo" or a.startswith("--repo=") for a in args)
        refspecs = positional if repo_by_option else positional[1:]
        # With no explicit refspec, Git reads push.default and branch/remote
        # configuration. The guard cannot prove that the implicit destination is
        # not main, so an approving result here would be a false claim.
        if not refspecs and "--tags" not in flags:
            deny("the push has no explicit non-main refspec, so its destination cannot be verified")

        for a in refspecs:
            # A leading + forces, colon or not: `git push origin +main` is
            # `+main:main`. Requiring a colon here let the colonless form past
            # BOTH checks at once — past this one, and past the branch check
            # below, because the target then read as the literal "+main" and
            # never matched "main". One character defeated the two rules this
            # guard exists for.
            if a.startswith("+"):
                deny("a leading + on a refspec is a force-push by another name")
            # Strip it anyway before comparing, so the branch check stands on
            # its own rather than depending on the deny above having fired.
            target = a.lstrip("+").split(":")[-1]
            if target in ("main", "refs/heads/main"):
                deny("main moves only by merging a pull request")
            if target in ("HEAD", "@"):
                deny("HEAD is an implicit push destination and may resolve to main")


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
    client = base(argv[0]).lower()
    writes = False
    request_method = None

    if client == "curl":
        parsed = list(curl_options(argv))

        # curl -G turns --data-* into a query string. It is not an
        # unconditional pass: -G with -X POST still sends POST, while -G with
        # --form or --upload-file remains write-shaped and is refused.
        force_get = any(option in ("G", "--get") for option, _ in parsed)

        for option, value in parsed:
            if option in ("F", "T") or option in CURL_WRITE_FLAGS:
                writes = True
            if option == "d" and not force_get:
                writes = True
            if option.startswith("--data") and not force_get:
                writes = True
            if option in ("X", "--request"):
                request_method = value.lower()
                if request_method in WRITE_METHODS:
                    writes = True

    elif client == "wget":
        for i, token in enumerate(argv[1:], start=1):
            low = token.lower()
            flag, separator, attached = low.partition("=")
            if flag in WGET_WRITE_FLAGS:
                writes = True
            if flag == "--method":
                request_method = (
                    attached if separator else (argv[i + 1].lower() if i + 1 < len(argv) else "")
                )
                if request_method in WRITE_METHODS:
                    writes = True

    elif client in ("http", "https"):
        request_method = httpie_method(argv)
        if request_method in WRITE_METHODS:
            writes = True
        for token in argv[1:]:
            flag = token.lower().split("=", 1)[0]
            # HTTPie defaults to POST when it receives a body. Treat even an
            # explicit GET-with-body conservatively: guessing which token was
            # the method previously opened bypasses through option values.
            if httpie_body_item(token) or flag == "--raw":
                writes = True

    if not writes:
        return
    if "networkvolume" in joined:
        deny("writes to a network volume — irreversible, and the corpus lives there")

    # Shutting a pod down saves money, and Governance 8 requires shutdown to be
    # *verified* against provider state. A guard that blocks stopping a pod is
    # working against the rule it exists to serve.
    #
    # Judge parsed paths on RunPod URLs only. A query value such as
    # `?callback=/stop`, or an unrelated second URL ending in /stop, must not
    # turn a pod-creation request into apparent cleanup.
    runpod_paths = []
    for token in argv:
        candidate = token.split("=", 1)[1] if token.lower().startswith("--url=") else token
        if not any(host in candidate.lower() for host in RUNPOD_HOSTS):
            continue
        if not candidate.lower().startswith(("http://", "https://")):
            candidate = f"https://{candidate}"
        try:
            runpod_paths.append(urlsplit(candidate).path.rstrip("/"))
        except ValueError:
            continue

    delete_request = request_method == "delete"
    shutting_down = bool(runpod_paths) and all(
        path.endswith(("/stop", "/terminate"))
        or (delete_request and re.search(r"/pods/[^/]+$", path))
        for path in runpod_paths
    )
    if shutting_down:
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
    if re.search(
        r"\b(?:requests|httpx)\s*\.\s*(?:post|put|patch|delete)\s*\(",
        inline,
        re.I,
    ) or re.search(
        r"\b(?:requests|httpx)\s*\.\s*request\s*\(\s*['\"](?:POST|PUT|PATCH|DELETE)['\"]",
        inline,
        re.I,
    ):
        deny(f"writes to the RunPod API through an inline HTTP client — {GOV8}")

    # The standard library needs no third-party package, so it was the shortest
    # route past the two names above. A read must stay allowed: Governance 8
    # requires shutdown and pod state to be *verified*, and that is a GET.
    if re.search(r"\b(?:urllib|http\.client)\b", inline) and (
        # a keyword body in a call — `urlopen(url, data=...)`, `Request(url, data=...)`
        re.search(r"[(,]\s*data\s*=", inline)
        or re.search(r"\bmethod\s*=\s*['\"](?:POST|PUT|PATCH|DELETE)['\"]", inline, re.I)
        or re.search(r"\brequest\s*\(\s*['\"](?:POST|PUT|PATCH|DELETE)['\"]", inline, re.I)
        # a body must be bytes, so an encode is the tell for the positional form
        or re.search(r"\.encode\(", inline)
    ):
        deny(f"writes to the RunPod API through the standard library — {GOV8}")


# A write from an inline JavaScript one-liner: `fetch(url, {method:'POST'})`,
# `axios.post(url, body)`, `client.request({method: 'DELETE'})`.
NODE_WRITE = re.compile(
    r"method\s*:\s*['\"`](?:POST|PUT|PATCH|DELETE)['\"`]"
    r"|\bmethod\s*=\s*['\"`](?:POST|PUT|PATCH|DELETE)['\"`]"
    r"|\.\s*(?:post|put|patch|delete)\s*\(",
    re.I,
)


def check_node(argv):
    """Inline JavaScript reaching the RunPod API. `node` was not dispatched at
    all, so `node -e` was the shortest write past this guard after Python."""
    inline = " ".join(argv)
    lowered = inline.lower()
    if not any(host in lowered for host in RUNPOD_HOSTS) and not re.search(r"\brunpod\b", lowered):
        return
    if NODE_WRITE.search(inline):
        deny(f"writes to the RunPod API from an inline script — {GOV8}")


def check_httpie_input(command: str) -> None:
    """Catch HTTPie request bodies supplied by a pipe or input redirection."""
    visible = normalise(strip_heredocs(command))
    if not any(host in visible.lower() for host in RUNPOD_HOSTS):
        return
    try:
        parsed = annotated_segments(command)
    except ValueError:
        # The general fallback still handles visible explicit HTTP methods. If
        # shell quoting is malformed, do not guess that ordinary prose is a
        # piped request.
        return
    for argv, piped_input, redirected_input in parsed:
        argv = peel(argv)
        if not argv or base(argv[0]).lower() not in ("http", "https"):
            continue
        if not any(host in " ".join(argv).lower() for host in RUNPOD_HOSTS):
            continue
        if piped_input or redirected_input:
            deny(f"HTTPie infers POST from piped or redirected request data — {GOV8}")


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
        # Every other agent's uncommitted work lives here.
        os.path.join(project, ".claude", "worktrees"),
        # In a worktree session CLAUDE_PROJECT_DIR points at the main checkout,
        # so `rm -rf .` inside the worktree would otherwise be nobody's business.
        os.path.realpath(os.getcwd()),
        home,
    ]


def _workbench_records():
    """Ignored drawers whose contents are records rather than disposable cache."""
    project = os.path.realpath(os.environ.get("CLAUDE_PROJECT_DIR") or os.getcwd())
    workbench = os.path.join(project, "workbench")
    return [
        os.path.join(workbench, "active"),
        os.path.join(workbench, "archive"),
        os.path.join(workbench, "design"),
        os.path.join(workbench, "raw"),
        os.path.join(workbench, "tools"),
    ]


def _operand_paths(operand: str, project: str, command_cwd: str | None = None):
    """Resolve an rm operand as the hook cwd and as the project cwd."""
    expanded = os.path.expandvars(os.path.expanduser(operand))
    head = expanded
    if any(c in expanded for c in "*?["):
        head = os.path.dirname(expanded.split("*")[0].split("?")[0].split("[")[0]) or "."
    paths = {os.path.realpath(head), os.path.normpath(os.path.abspath(head))}
    if not os.path.isabs(head):
        # Hooks normally inherit the project cwd, but that is a runner
        # convention rather than part of the JSON contract. Judge a
        # project-relative spelling against CLAUDE_PROJECT_DIR as well.
        paths.add(os.path.realpath(os.path.join(project, head)))
        if command_cwd:
            paths.add(os.path.realpath(os.path.join(command_cwd, head)))
    return paths


def check_rm(argv, command_cwd: str | None = None):
    recursive = False
    operands = []
    options_ended = False
    for token in argv[1:]:
        if not options_ended and token == "--":
            options_ended = True
        elif not options_ended and token == "--recursive":
            recursive = True
        elif not options_ended and token.startswith("--"):
            continue
        elif not options_ended and token.startswith("-") and len(token) > 1:
            recursive = recursive or "r" in token.lower()
        else:
            operands.append(token)

    records = _workbench_records()
    project = os.path.realpath(os.environ.get("CLAUDE_PROJECT_DIR") or os.getcwd())
    for operand in operands:
        for path in _operand_paths(operand, project, command_cwd):
            for record in records:
                if (
                    path == record
                    or path.startswith(record + os.sep)
                    or record.startswith(path + os.sep)
                ):
                    deny(
                        f"delete of '{operand}' — workbench records outside "
                        f"scratch are not disposable"
                    )

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

        # realpath follows symlinks, normpath collapses `..` lexically, and
        # `/tmp/../Users/...` resolves differently under each.
        for path in _operand_paths(operand, project, command_cwd):
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
    "node": check_node,
    "nodejs": check_node,
    "bun": check_node,
    "deno": check_node,
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


SHELLS = ("bash", "sh", "zsh", "dash", "ksh")


def inspect(command: str, depth: int = 0) -> None:
    for opening, body, supported, expands in heredoc_blocks(command):
        if not supported:
            deny("the heredoc delimiter uses shell syntax the command guard cannot verify")
        try:
            openers = segments(opening)
        except ValueError:
            deny("the heredoc opening command cannot be parsed safely")
        resolved = False
        for argv in openers:
            argv = peel(argv)
            if not argv:
                continue
            resolved = True
            name = base(argv[0])
            if name not in ("cat", "tee"):
                deny(f"a heredoc attached to '{name}' is opaque to the command guard")
        if not resolved:
            deny("the heredoc opening has no resolvable command on the same line")

        # A `cat`/`tee` body is data only while the shell leaves it alone. With
        # an unquoted delimiter the shell expands it first, so `$(...)` and
        # backticks in the body run before `cat` is even started — which is how
        # a push to main once travelled inside something the guard called inert.
        # Inspect exactly the part the shell executes, and nothing else: reading
        # the whole body as commands would refuse every generated file whose
        # prose happens to mention `rm -rf`.
        if not expands or not body:
            continue
        try:
            payloads = command_substitutions(body)
        except ValueError:
            deny("the heredoc body opens a command substitution the guard cannot read")
        for payload in payloads:
            if depth >= 3:
                deny("a command substitution nested deeper than the guard inspects")
            inspect(payload, depth + 1)

    check_httpie_input(command)
    try:
        parsed = segments(command)
    except ValueError:
        for noun, context, reason in FALLBACK:
            if noun.search(command) and context.search(command):
                deny(reason)
        return

    project = os.path.realpath(os.environ.get("CLAUDE_PROJECT_DIR") or os.getcwd())
    command_cwd = project
    for argv in parsed:
        argv = peel(argv)
        if not argv:
            continue
        name = base(argv[0])

        if name == "cd":
            operands = [a for a in argv[1:] if not a.startswith("-")]
            if operands:
                destination = os.path.expandvars(os.path.expanduser(operands[0]))
                command_cwd = os.path.realpath(
                    destination
                    if os.path.isabs(destination)
                    else os.path.join(command_cwd, destination)
                )
            continue

        # `bash -c '<anything>'` and `eval '<anything>'` carry a whole command
        # as a string. Look inside it. The old raw-text guard caught these for
        # free; a tokenising one has to be told.
        if depth < 3:
            payload = None
            if name in SHELLS:
                for i, token in enumerate(argv):
                    if token.startswith("-") and "c" in token[1:] and i + 1 < len(argv):
                        payload = argv[i + 1]
                        break
            elif name == "eval":
                payload = " ".join(argv[1:])
            if payload:
                inspect(payload, depth + 1)

        # python3.11, python3.13 — a version suffix is not a different program.
        handler = DISPATCH.get(name) or (check_python if name.startswith("python") else None)
        if handler:
            if handler is check_rm:
                handler(argv, command_cwd)
            else:
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
