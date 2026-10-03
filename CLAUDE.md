@AGENTS.md

## Claude Code specifics

- `/session-start` and `/session-end` open and close a session.
- Check plan usage at milestones; near the weekly limit, run `/session-end` so the next
  session starts from a written handoff.
- Specialists (extra agents and guides) sit unloaded in `.claude/specialists/`. Read its
  `README.md` only when the lead asks about specialists or a task clearly calls for one;
  never import it here.
