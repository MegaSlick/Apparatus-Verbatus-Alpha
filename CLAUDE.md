@AGENTS.md

## Claude Code specifics

- `/session-start` and `/session-end` open and close a session.
- Read-only agent roles live in `.claude/agents/`: `scout` for quick lookups, `auditor`
  for reviews, `consult` for a second opinion on a design.
- Check plan usage at milestones. At 97% of the weekly limit, wind down: write the
  handoff and send the `done` notification (`operations/notify/notify.sh`).
