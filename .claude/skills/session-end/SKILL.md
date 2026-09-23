---
name: session-end
description: Close a session with verified git state, filed notes and a short handoff.
disable-model-invocation: true
---

# Session end

Run this when the lead asks to close, or when plan usage reaches the wind-down point.

1. **Establish state.** `git fetch origin`, `git status --short --branch`,
   `git rev-list --left-right --count origin/main...HEAD`, `git worktree list` and
   `python3 .githooks/tidy.py`. Name every check that was skipped or failed. Never tidy
   by deleting evidence or discarding work.
2. **File notes.** Move finished notes, the outgoing handoff included, into one
   `workbench/archive/<date>_<topic>/` directory. Never overwrite an archived file.
3. **Write the handoff** in `workbench/active/HANDOFF.md`, holding only what the next
   session cannot cheaply work out: branch and distance from `main`, uncommitted work,
   checks that are not green, state outside git, decisions and their reasons, real
   blockers with paths to the evidence, and anything waiting on the lead.
4. **Park.** If the work continues, stay on its branch. If its pull request has merged,
   the tree is clean and the fetch succeeded, move to a fresh branch from `origin/main`;
   delete the old branch only if the pull request's head commit equals the branch tip. Remove an agent's worktree only once its
   work is merged or recorded.
5. **Notify, then report.** Send the `done` notification with
   `operations/notify/notify.sh`; it prints nothing on success and names the failure
   otherwise. Then give the final state, checks, external actions, the notification
   result and the next step.
