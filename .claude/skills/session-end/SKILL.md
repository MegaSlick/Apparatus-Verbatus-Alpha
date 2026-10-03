---
name: session-end
description: Close a session with verified git state and a short handoff.
---

# Session end

1. `git fetch origin`, `git status --short --branch`, `git worktree list`. Never tidy by
   deleting work.
2. Write `workbench/active/HANDOFF.md` with only what the next session cannot work out:
   branch, uncommitted work, checks not green, anything running outside git (pods,
   jobs), decisions and their reasons, and what waits on the lead. Move the previous one
   to `workbench/archive/<date>_<topic>/` first, under a name not already used there;
   never overwrite an archived handoff.
3. Check RunPod shows no pod of ours running.
4. Send `sh operations/notify/notify.sh done "<one line>"`, then report the final state,
   which checks ran and which did not, and the next step.
