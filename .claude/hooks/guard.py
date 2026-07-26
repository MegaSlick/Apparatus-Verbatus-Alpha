#!/usr/bin/env python3
"""Tripwire for the things that cost money or destroy work.

Runs before every Bash command and every MCP tool call. Blocks a short, specific
list and stays out of the way otherwise — a guard that fires constantly gets
disabled, which is worse than no guard.

What it blocks, and why:
  * bringing up billed RunPod infrastructure   Governance 8: only Tyrel, in-session
  * deleting a network volume                  irreversible, and the corpus lives there
  * removing pods in bulk                      destroys work; one named pod is fine
  * disabling the git hooks                    they are the only enforcement there is
  * force-push, direct push to main            main moves only by merge
  * skipping the hooks with --no-verify        one flag defeats every local rule
  * rm -rf outside a scratch path              the obvious one

It reads the command as a shell would — tokenised, quote-aware, split on `&&`,
`|`, `;` and newlines. That matters: the older version matched raw text, so
`git commit -m 'document the --no-verify hatch'` looked exactly like actually
passing --no-verify, and `git push origin work/x && grep -f pat file` looked
exactly like a force-push. Both were blocked. Neither should have been.

Three honest limits, so nobody mistakes this for a wall:

  1. It only guards Claude. Codex, GPT and a human at a terminal never run it.
     The git hooks in .githooks/ are the layer that binds every tool; this is a
     fast tripwire on top, not the enforcement.
  2. It cannot see inside a script. `python3 deploy.py` is three tokens, and
     what deploy.py does is invisible here. Same for a command assembled from
     variables. It catches the honest mistake and the careless agent, not a
     determined one, and it is not meant to.
  3. If a command will not parse — an apostrophe inside a heredoc is enough —
     it falls back to matching raw text for the money rules only. Those still
     fire; the subtler rules do not.
"""
import json
import re
import shlex
import sys

# ---------------------------------------------------------------- vocabulary

RUNPOD_HOSTS = ("runpod.io", "runpod.ai")

# Verbs that start the meter. `create` makes a pod, `start` wakes a stopped one,
# and `project deploy` / `project dev` stand up billed infrastructure under
# another name.
RUNPODCTL_BLOCKED = {
    ("create",): "creates pods",
    ("start",): "wakes a stopped pod, which bills again",
    ("project", "deploy"): "deploys billed infrastructure",
    ("project", "dev"): "starts a billed development session",
    ("remove", "pods"): "removes pods in bulk by name — they may not all be yours",
}

GOV8 = ("Governance 8: a live pod needs Tyrel's explicit permission "
        "this session")

# Options that carry a separate value, so the value is never mistaken for a flag.
GIT_VALUE_OPTS = {"-c", "-C", "--git-dir", "--work-tree", "--namespace",
                  "--exec-path", "-m", "--message", "-F", "--file",
                  "-t", "--template", "--reuse-message", "--squash"}

SCRATCH_MARKERS = ("/tmp/", "/private/tmp/", "scratchpad")

# Raw-text rules. Used on every command, and they are the whole of the fallback
# when a command will not tokenise. URLs and API payloads are text by nature.
RAW_RULES = [
    (re.compile(r"networkvolume", re.I),
     "touches a network volume — irreversible, and the corpus lives there"),
    (re.compile(r"\bdeleteNetworkVolume\b", re.I),
     "deletes a network volume — irreversible, and the corpus lives there"),
    (re.compile(r"\b(podFindAndDeployOnDemand|podResume|podRentInterruptable)\b", re.I),
     f"deploys or resumes a pod via the API — {GOV8}"),
    (re.compile(r"\bimport\s+runpod\b|\brunpod\.(create_pod|resume_pod)\b", re.I),
     f"brings up a pod through the RunPod SDK — {GOV8}"),
]

WRITE_METHODS = ("post", "put", "patch", "delete")
WRITE_FLAGS = ("-d", "--data", "--data-raw", "--data-binary", "--data-urlencode",
               "--json", "--post-file", "--post-data", "--upload-file", "-T")


def deny(reason: str) -> None:
    print(json.dumps({
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": f"Blocked by repo guard: {reason}. Ask Tyrel.",
        }
    }))
    sys.exit(0)


def base(token: str) -> str:
    return token.rsplit("/", 1)[-1]


def segments(command: str):
    """Split a shell command into argv lists the way a shell would.

    Quote-aware, so a flag named inside a commit message stays a string.
    Raises ValueError if the command will not tokenise.
    """
    lexer = shlex.shlex(command, posix=True, punctuation_chars=True)
    lexer.whitespace_split = True
    out, current = [], []
    for token in lexer:
        if token and all(c in ";&|()\n" for c in token):
            if current:
                out.append(current)
                current = []
        else:
            current.append(token)
    if current:
        out.append(current)
    return out


def flags_of(argv, value_opts):
    """Yield only the tokens that are genuinely options, skipping their values."""
    skip = False
    for token in argv:
        if skip:
            skip = False
            continue
        if token in value_opts:
            skip = True
            yield token
            continue
        yield token


# --------------------------------------------------------------------- rules

def check_git(argv):
    opts, sub, args = [], None, []
    i = 1
    while i < len(argv):
        token = argv[i]
        if token in GIT_VALUE_OPTS and token in ("-c", "-C", "--git-dir",
                                                 "--work-tree", "--namespace",
                                                 "--exec-path"):
            opts.append(token)
            if i + 1 < len(argv):
                opts.append(argv[i + 1])
            i += 2
        elif token.startswith("-"):
            opts.append(token)
            i += 1
        else:
            sub, args = token, argv[i + 1:]
            break
    if sub is None:
        return

    # Turning the hooks off is the single most dangerous thing available here:
    # it defeats the branch guard, the stray-note check and the audit gate at
    # once, and it reads like an ordinary command.
    for opt in opts:
        if "core.hookspath" in opt.lower():
            deny("disables the git hooks, which are the only enforcement this "
                 "repository has")

    real = [a for a in flags_of(args, GIT_VALUE_OPTS)]

    if sub in ("commit", "merge", "push", "rebase", "am"):
        if "--no-verify" in real:
            deny("--no-verify skips the hooks that protect main, the documents "
                 "and the audit gate")
        if sub == "commit" and any(
                a.startswith("-") and not a.startswith("--") and "n" in a[1:]
                for a in real):
            deny("git commit -n is --no-verify, which skips the hooks")

    if sub == "push":
        if any(a == "--force" or a.startswith("--force-with-lease") or a == "-f"
               for a in real):
            deny("force-push destroys work another agent may be holding "
                 "(ALLOW_FORCE_PUSH=1 if you truly mean it)")
        for a in args:
            if a.startswith("+") and ":" in a:
                deny("a leading + on a refspec is a force-push by another name")
            target = a.split(":")[-1] if ":" in a else a
            if target in ("main", "refs/heads/main"):
                deny("main moves only by merging a pull request")


def check_runpodctl(argv):
    rest = [a for a in argv[1:] if not a.startswith("-")]
    if any(a in ("--help", "-h", "help") for a in argv[1:]):
        return  # reading the manual costs nothing
    for verbs, why in RUNPODCTL_BLOCKED.items():
        if tuple(rest[:len(verbs)]) == verbs:
            if verbs[0] in ("create", "start", "project"):
                deny(f"{why} — {GOV8}")
            deny(f"{why} — irreversible")


def check_http(argv):
    """curl / wget / http against a RunPod endpoint with anything but a read."""
    joined = " ".join(argv)
    if not any(host in joined for host in RUNPOD_HOSTS):
        return
    # `--post-file=body.json` is one token; compare on the flag, not the value.
    lowered = [a.lower().split("=", 1)[0] for a in argv]
    writes = any(a in WRITE_FLAGS or a.startswith("--data") for a in lowered)
    for i, a in enumerate(lowered):
        if a in ("-x", "--request") and i + 1 < len(lowered):
            if lowered[i + 1] in WRITE_METHODS:
                writes = True
    if "graphql" in joined.lower():
        writes = True
    if writes:
        deny(f"writes to the RunPod API, which creates or destroys billed "
             f"resources — {GOV8}")


def check_rm(argv):
    recursive = force = False
    operands = []
    for token in argv[1:]:
        if token.startswith("--"):
            if token == "--recursive":
                recursive = True
            elif token == "--force":
                force = True
        elif token.startswith("-") and len(token) > 1:
            recursive = recursive or "r" in token.lower()
            force = force or "f" in token
        else:
            operands.append(token)
    if not (recursive and force):
        return
    for operand in operands:
        if not any(marker in operand for marker in SCRATCH_MARKERS):
            deny(f"recursive force delete of '{operand}', which is not a "
                 f"scratch path (use a literal path under /tmp/ or scratchpad)")


def check_aws(argv):
    if argv[:3] == ["aws", "s3", "rm"] and "--recursive" in argv:
        deny("recursive S3 delete — irreversible")


DISPATCH = {
    "git": check_git,
    "runpodctl": check_runpodctl,
    "curl": check_http,
    "wget": check_http,
    "http": check_http,
    "httpie": check_http,
    "rm": check_rm,
    "aws": check_aws,
}

BLOCKED_TOOLS = {
    "mcp__runpod__create-pod": f"creates a pod — {GOV8}",
    "mcp__runpod__start-pod": f"starts a pod — {GOV8}",
}


def inspect(command: str) -> None:
    for pattern, reason in RAW_RULES:
        if pattern.search(command):
            deny(reason)

    try:
        parsed = segments(command)
    except ValueError:
        return  # limit 3: the money rules above still ran; the rest cannot

    for argv in parsed:
        if not argv:
            continue
        handler = DISPATCH.get(base(argv[0]))
        if handler:
            handler(argv)
        # `env FOO=bar runpodctl create pod` and `sudo rm -rf /` both hide the
        # real command one token in.
        if base(argv[0]) in ("env", "sudo", "nohup", "time", "xargs"):
            inner = [a for a in argv[1:] if "=" not in a.split(" ")[0]]
            if inner:
                nested = DISPATCH.get(base(inner[0]))
                if nested:
                    nested(inner)


def main() -> None:
    try:
        payload = json.load(sys.stdin)
        tool = payload.get("tool_name") or ""
        tool_input = payload.get("tool_input") or {}
    except Exception:
        # A guard that cannot read its input must not be mistaken for one that
        # looked and approved — GOVERNANCE.md 10.
        deny("the guard could not read this tool call, so it cannot vouch for it")
        return

    if tool in BLOCKED_TOOLS:
        deny(BLOCKED_TOOLS[tool])

    if tool == "Bash":
        command = tool_input.get("command")
        if not isinstance(command, str):
            deny("the guard could not read this command, so it cannot vouch for it")
        inspect(command)

    sys.exit(0)


if __name__ == "__main__":
    main()
