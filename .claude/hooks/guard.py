#!/usr/bin/env python3
"""Tripwire for the things that cost money or destroy work.

Runs before every Bash command and every MCP tool call. Blocks a short, specific
list and stays out of the way otherwise — a guard that fires constantly gets
disabled, which is worse than no guard.

What it blocks, and why:
  * bringing a pod to life             Governance 8: only Tyrel, only in-session
  * deploying a project as an endpoint Governance 8: same — it bills the same way
  * removing pods in bulk              destroys work; a named single pod is fine
  * force-push / history rewrite       destroys work another agent may be holding
  * direct push to main                main moves only by merge
  * skipping the hooks                 --no-verify is how every local rule dies
  * rm -rf outside a scratch path      the obvious one

Two honest limits, so nobody mistakes this for a wall:

  1. It only guards Claude. Codex, GPT and a human at a terminal never run it.
     The git hooks in .githooks/ are the layer that binds every tool; this one
     is a fast tripwire on top, not the enforcement.
  2. It matches text, not intent. A command assembled from variables, or piped
     through a shell, reads differently to a regex than it does to the machine.
     It catches the honest mistake and the careless agent. It does not catch a
     determined one, and it is not meant to.
"""
import json
import re
import sys

# Anything that starts the meter. `create` makes a pod, `start` wakes a stopped
# one, and `project deploy` / `project dev` stand up billed infrastructure under
# another name. All four are Governance 8.
POD_LIVE = (r"\brunpodctl\s+(create|start)\b"
            r"|\brunpodctl\s+project\s+(deploy|dev)\b")

BLOCKED_BASH = [
    (POD_LIVE,
     "brings up billed RunPod infrastructure — Governance 8: needs Tyrel's explicit permission this session"),
    (r"POST[^|;]*\brunpod\.io[^|;]*\bpods\b",
     "creates a pod via the API — Governance 8: needs Tyrel's permission"),
    (r"\bcurl\b[^|;]*\brunpod\.io/graphql\b[^|;]*(podFindAndDeployOnDemand|podResume)",
     "deploys or resumes a pod — Governance 8: needs Tyrel's permission"),

    # `remove pods` deletes every pod matching a name. `remove pod <id>` is
    # ordinary cleanup and stays allowed, so the guard does not cry wolf.
    (r"\brunpodctl\s+remove\s+pods\b",
     "removes pods in bulk by name — irreversible, and they may not all be yours"),

    (r"\baws\s+s3\s+rm\b[^|;]*--recursive",
     "recursive S3 delete — irreversible"),

    (r"\bgit\s+push\b[^|;]*(--force|--force-with-lease|-f)\b",
     "force-push destroys work another agent may be holding"),
    # `main` as a whole argument or as a refspec target, so that `work/main-fix`
    # and `infra/main-guard` are the ordinary branch names they look like.
    (r"\bgit\s+push\b[^|;]*(\s|:)main(\s|$)",
     "main moves only by merging a pull request"),

    # --no-verify is the cheapest way past every local rule in this repository:
    # the branch guard, the stray-note check and the audit gate all live in
    # hooks, and this one flag skips all of them at once.
    (r"\bgit\s+(commit|merge|push)\b[^|;]*--no-verify",
     "--no-verify skips the hooks that protect main and the documents"),
    (r"\bgit\s+commit\b[^|;]*\s-n\b",
     "git commit -n is --no-verify, which skips the hooks"),

    (r"\brm\s+(-[a-zA-Z]*[rR][a-zA-Z]*f|-[a-zA-Z]*f[a-zA-Z]*[rR])\b(?![^|;]*(/tmp/|scratchpad))",
     "recursive force delete outside a scratch path"),
]

BLOCKED_TOOLS = {
    "mcp__runpod__create-pod": "creates a pod — Governance 8: needs Tyrel's explicit permission this session",
    "mcp__runpod__start-pod": "starts a pod — Governance 8: needs Tyrel's explicit permission this session",
}


def deny(reason: str) -> None:
    print(json.dumps({
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": f"Blocked by repo guard: {reason}. Ask Tyrel.",
        }
    }))
    sys.exit(0)


def main() -> None:
    try:
        payload = json.load(sys.stdin)
    except Exception:
        sys.exit(0)  # unparseable input is not a reason to halt the session

    tool = payload.get("tool_name", "")
    if tool in BLOCKED_TOOLS:
        deny(BLOCKED_TOOLS[tool])

    if tool == "Bash":
        command = payload.get("tool_input", {}).get("command", "")
        for pattern, reason in BLOCKED_BASH:
            if re.search(pattern, command):
                deny(reason)

    sys.exit(0)


if __name__ == "__main__":
    main()
