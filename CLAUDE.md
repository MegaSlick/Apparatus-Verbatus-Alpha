@AGENTS.md

## Claude Code specifics

- `/session-start` and `/session-end` open and close a session.
- Read-only agent roles live in `.claude/agents/`: `scout` for quick lookups, `auditor`
  for reviews, `consult` for a second opinion on a design.
- Check plan usage at milestones. At 97% of the weekly limit, wind down: write the
  handoff and send the `done` notification (`operations/notify/notify.sh`).

## Simplest end state

- Judge a change by the system it leaves behind, not by its diff: in existing code, fix
  the root cause rather than keep a workaround. This overrides the Ponytail plugin, whose
  smallest-diff ladder, `ponytail:` comments and three-line summaries serve new code only.
- Build once, reuse everywhere: shared tools with small configs, standard library first.
- Comments and docstrings are hypotheses, not verdicts: code, tests and digests never
  depend on them, a warning becomes a regression test, and history belongs in git. A
  touched file ends with no more comment lines than it had.
- Vendor models run at a pinned revision fetched at launch, our adaptations are
  deterministic scripts, and an upstream update is flagged for review as maintenance.
- A pure cleanup (no behaviour change) runs `/ponytail ultra` first, is net-negative in
  lines (`git diff --shortstat $(git merge-base origin/main HEAD)`) and deletes tests that
  served only deleted code. A regression test for a recorded failure is always allowed.
- Only a cosmetic review finding may be declined as "not worth the lines". AGENTS.md
  reporting, the test suite and the independent reader still win; `/ponytail-review` is
  never that reader. Decline its offer to add a status line.

## Graphify (code graph)

- `graphify update .` builds or refreshes `graphify-out/` from code alone (no model, no
  network); then `graphify query`, `path`, `explain` and `god-nodes` read it.
- Never run `graphify .`, `graphify extract`, `label`, `cluster-only` or `/graphify`:
  they send documents to a model or call `claude -p`. Real register material must not
  leave this machine. `.graphifyignore` limits the index to code.
