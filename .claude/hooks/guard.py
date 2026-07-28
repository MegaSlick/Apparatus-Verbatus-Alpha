#!/usr/bin/env python3
"""Tripwire for the things that cost money or destroy work.

Runs before every Bash command and every MCP tool call. Blocks a short list and
stays out of the way otherwise — a guard that fires on ordinary work gets
disabled, and a disabled guard protects nothing.

What it blocks:
  * bringing up billed RunPod infrastructure   Governance 8: only Tyrel, in-session
  * writing to the RunPod API                  same, by another route
  * deleting a network volume                  irreversible, and the corpus lives there
  * turning the git hooks off                  they are the only local enforcement
  * force-push, direct push to main            main moves only by merge
  * --no-verify                                one flag defeats every local rule
  * deleting the repository or your home       the obvious one

WHAT THIS IS NOT
================
It is not a security boundary and it cannot be made into one. Four rounds of
adversarial audit found a new way past every version of it, which is the
expected result: inspecting command text can always be defeated by writing the
command differently.

Known and unfixed: a script file (`python3 deploy.py`) is opaque, and a command
assembled from variables reads differently here than it does to the shell.
`bash -c` and `eval` payloads *are* inspected, one level of nesting at a time,
up to three deep — but base64 through a pipe, or any other encoding, is not.

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


HEREDOC = re.compile(r"<<-?\s*(['\"]?)([A-Za-z_][A-Za-z_0-9]*)\1")
REDIRECTIONS = {">", ">>", "<", "<<", "<<<", ">&", "<&", ">|"}
INPUT_REDIRECTIONS = {"<", "<<", "<<<", "<&"}


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
            j = i
            while j < len(lines) and lines[j].strip() != tag:
                j += 1
            # Only treat it as a heredoc if the terminator is actually there.
            # `echo "see <<EOF for the syntax"` has no closing tag, and
            # discarding the rest of the command would hide everything after it.
            if j < len(lines):
                i = j + 1
    return "\n".join(out)


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
        deny("disables the git hooks, which are all the local enforcement there is")
    if sub == "config" and any("core.hookspath" in a.lower() for a in args):
        # Reading the setting is how you check install.sh worked — and the
        # script invites you to. Only writing it is the problem.
        reading = any(a in ("--get", "--get-all", "--get-regexp", "--list", "-l") for a in args)
        if not reading:
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


SHELLS = ("bash", "sh", "zsh", "dash", "ksh")


def inspect(command: str, depth: int = 0) -> None:
    check_httpie_input(command)
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
