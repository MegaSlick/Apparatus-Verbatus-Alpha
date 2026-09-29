@AGENTS.md

## Claude Code specifics

- `/session-start` and `/session-end` open and close a session; Claude runs either one
  itself when the moment comes, including the plan-usage wind-down.
- Read-only agent roles live in `.claude/agents/`: `scout` for quick lookups, `auditor`
  for reviews, `consult` (on Fable) for a second opinion on a design.
- Check plan usage at milestones. At 97% of the weekly limit, wind down: write the
  handoff and send the `done` notification (`operations/notify/notify.sh`).

## Ponytail

The ponytail plugin keeps new work lean. Its ladder picks the simplest solution and its
root-cause rule matches ours; where its shortest diff would keep a workaround, the end
state in AGENTS.md "Building" wins. Agents that write or plan code get it; the review
roles do not.

- A deliberate shortcut and its limit go in the commit message, not a `ponytail:`
  comment.
- AGENTS.md reporting is a standing request for a full report, which ponytail's output
  rule already allows.
- Start a cleanup with `/ponytail-audit`, and run `/ponytail-review` on a non-trivial diff
  before committing. Neither replaces the independent reader.
