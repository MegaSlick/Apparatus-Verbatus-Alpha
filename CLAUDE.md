@AGENTS.md

## Claude Code specifics

- `/session-start` and `/session-end` open and close a session. Run them yourself when
  the moment comes.
- Read-only agent roles live in `.claude/agents/`: `scout` for quick lookups, `auditor`
  for reviews of code or documents, and `consult` for a second opinion on a design
  before committing to it.
- Check plan usage at milestones. At 97% of the weekly limit, wind down: run
  `/session-end` so the next session starts from a written handoff.
- The lead also runs the optional ponytail plugin, installed per user, which nudges
  code toward the simplest change. `PONYTAIL_SUBAGENT_MATCHER` in the settings keeps it
  on the writing agents and off the review roles. Where its shortest diff would keep a
  workaround, AGENTS.md "Building" wins. Without the plugin the setting does nothing.
