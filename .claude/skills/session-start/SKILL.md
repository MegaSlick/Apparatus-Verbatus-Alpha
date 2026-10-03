---
name: session-start
description: Get oriented before changing the repository — sync, read the handoff, check the branch, settle the goal.
---

# Session start

1. `git fetch origin`, then `git status --short --branch`. If the fetch fails, say the
   checkout may be stale.
2. Read `AGENTS.md`, `README.md`, `PRINCIPLES.md`, and `workbench/active/HANDOFF.md` if
   it exists. The handoff is a note from the last session; the lead's goal today wins.
3. If on `main` or a merged branch, start a fresh branch from `origin/main`. If
   `git status` shows uncommitted work you did not make, find out whose it is first.
4. Read the goal back in one line, ask the lead for any session rule set if the session
   is long or important and none was given, and start.
