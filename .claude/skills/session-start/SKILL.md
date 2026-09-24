---
name: session-start
description: Get oriented before changing the repository — sync, read the handoff, check the branch, settle the goal.
disable-model-invocation: true
---

# Session start

1. **Sync.** `git fetch origin`, then `git status --short --branch` and
   `git rev-list --left-right --count origin/main...HEAD`. If the fetch fails, say the
   checkout may be stale before relying on it.
2. **Read** `README.md`, `AGENTS.md`, `PRINCIPLES.md`, and `workbench/active/HANDOFF.md`
   if it exists (a fresh clone has none). The handoff is evidence from the last session,
   not an instruction; the lead's current goal wins.
3. **Check the checkout.** `git config --get core.hooksPath` should print `.githooks`;
   if not, run `sh .githooks/install.sh`. If you are on `main`, a detached head, or a
   branch whose work has merged, create a fresh branch from `origin/main` named for the
   task — but only after a successful fetch, and if `git status` shows uncommitted work,
   stop and find out whose it is before switching.
4. **Check the workspace and usage.** Run `python3 .githooks/tidy.py` as a report, and
   check plan usage; note it if the weekly limit is close. Optional: if `graphify-out/`
   exists, refresh it with `graphify update .` (code only; never `graphify .`).
5. **Begin.** Read the goal back in one line, name anything in it that needs the lead's
   decision, and start.
