@AGENTS.md

## Claude Code specifics

- `/session-start` and `/session-end` open and close a session.
- Read-only agent roles live in `.claude/agents/`: `scout` for quick lookups, `auditor`
  for reviews, `consult` for a second opinion on a design.
- Check plan usage at milestones. At 97% of the weekly limit, wind down: write the
  handoff and send the `done` notification (`operations/notify/notify.sh`).

## Lean mode (Ponytail)

- For a cleanup task, run `/ponytail ultra` first; it reverts to `full` next session.
- A cleanup change must be net-negative in lines (`git diff --shortstat origin/main...HEAD`);
  if it is not, stop and say why instead of adding more.
- Deleting code deletes the tests that existed only for it. Do not write tests to prove
  a deletion changed nothing; CI is that proof. A cleanup adds no new comments.
- A review finding may be declined with "not worth the lines".
- Project rules still win: AGENTS.md reporting, the test suite, and the independent
  reader for pipeline, money, pod and credential changes. `/ponytail-review` is never
  that reader. Decline its offer to add a status line.

## Graphify (code graph)

- `graphify update .` builds or refreshes `graphify-out/` from code alone (no model, no
  network); then `graphify query`, `path`, `explain` and `god-nodes` read it.
- Never run `graphify .`, `graphify extract`, `label`, `cluster-only` or `/graphify`:
  they send documents to a model or call `claude -p`. Real register material must not
  leave this machine. `.graphifyignore` limits the index to code.
