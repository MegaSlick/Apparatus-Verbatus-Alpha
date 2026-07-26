#!/usr/bin/env python3
"""Tripwire for the things that cost money or destroy work.

Runs before every Bash command and every MCP tool call. Blocks a short, specific
list and stays out of the way otherwise — a guard that fires constantly gets
disabled, which is worse than no guard.

What it blocks, and why:
  * starting or creating a pod        Governance 8: only Tyrel, only in-session
  * deleting a network volume         irreversible, and the corpus lives there
  * force-push / history rewrite      destroys work another agent may be holding
  * direct push to main               main moves only by merge
  * rm -rf outside a scratch path     the obvious one

Everything else is allowed. Tyrel has said he wants broad access for long
workflows; this is the narrow exception list, not a general cage.
"""
import json
import re
import sys

BLOCKED_BASH = [
    (r"\brunpodctl\s+create\b", "creates a pod — Governance 8: needs Tyrel's explicit permission this session"),
    (r"POST[^|;]*\brunpod\.io[^|;]*\bpods\b", "creates a pod via the API — Governance 8: needs Tyrel's permission"),
    (r"\bcurl\b[^|;]*\brunpod\.io/graphql\b[^|;]*podFindAndDeployOnDemand", "deploys a pod — Governance 8: needs Tyrel's permission"),
    (r"\brunpodctl\s+remove\s+volume\b", "deletes a network volume — irreversible"),
    (r"\baws\s+s3\s+rm\b[^|;]*--recursive", "recursive S3 delete — irreversible"),
    (r"\bgit\s+push\b[^|;]*(--force|-f)\b", "force-push destroys work another agent may be holding"),
    (r"\bgit\s+push\b[^|;]*\bmain\b", "main moves only by merging a pull request"),
    (r"\bgit\s+push\b[^|;]*--no-verify", "--no-verify skips the hooks that protect main"),
    (r"\brm\s+(-[a-zA-Z]*[rR][a-zA-Z]*f|-[a-zA-Z]*f[a-zA-Z]*[rR])\b(?![^|;]*(/tmp/|scratchpad))", "recursive force delete outside a scratch path"),
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
