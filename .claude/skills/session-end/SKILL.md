---
name: session-end
description: Close a session with verified git state, filed notes and a short handoff.
---

# Session end

Run this when the lead asks to close, or when plan usage reaches the wind-down point.

1. **Establish state.** `git fetch origin`, `git status --short --branch`,
   `git rev-list --left-right --count origin/main...HEAD` and `git worktree list`. Name
   every check that was skipped or failed. Never tidy by deleting evidence or discarding
   work.
2. **Write the handoff**, holding only what the next session cannot cheaply work out:
   branch and distance from `main`, uncommitted work, checks that are not green, state
   outside git (pods, running jobs), decisions and their reasons, real blockers with
   paths to the evidence, and anything waiting on the lead. Locally it goes in
   `workbench/active/HANDOFF.md`, after moving the previous one into
   `workbench/archive/<date>_<topic>/`. A cloud session pushes it on its handoff branch,
   because the container is discarded.
3. **Park.** If the work continues, stay on its branch. If its pull request has merged,
   the tree is clean and the fetch succeeded, move to a fresh branch from `origin/main`.
   Delete the old local branch only if the pull request's head commit equals its tip.
   Remove an agent's worktree only once its work is merged or recorded.
4. **Notify, then report.** Send the `done` notification with
   `sh operations/notify/notify.sh done "<one line>"`; it prints nothing on success and
   names the failure otherwise. Then give the final state, the checks, any external
   actions, the notification result and the next step.
