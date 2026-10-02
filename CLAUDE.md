@AGENTS.md

## Claude Code specifics

- `/session-start` and `/session-end` open and close a session. Run them yourself when
  the moment comes.
- Read-only agent roles live in `.claude/agents/`: `scout` for quick lookups, `auditor`
  for reviews of code or documents, and `consult` for a second opinion on a design
  before committing to it.
- Check plan usage at milestones. At 97% of the weekly limit, wind down: run
  `/session-end` so the next session starts from a written handoff.
