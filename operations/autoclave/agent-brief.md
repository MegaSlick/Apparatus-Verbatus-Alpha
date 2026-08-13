# You are in the autoclave

This is a disposable container, not Tyrel's checkout. Read `/work/GOALS.md`,
`/work/GOVERNANCE.md`, `/work/ARCHITECTURE.md`, `/work/GLOSSARY.md`, and
`/work/CLAUDE.md` from disk before concluding anything.

## Boundary

- `/work` is your clone and branch. Write and test there.
- `/src` is the host repository, read-only; `private/`, `workbench/`, and `scriptorium/`
  are masked.
- `/out` is writable host scratch used for briefs, reports, and bundles.
- `/specs` is a writable copy of design notes, not the host originals.
- `/window`, when present, is read-only reference to the old pipeline.
- Your vendor credential volume is writable, shared with later chambers, and outside the
  returned branch. Change nothing in it except through the vendor CLI.
- Network egress is open. Use it for current documentation and maintained dependencies;
  never publish repository contents or invoke a paid action.

You may orchestrate internally when the task is large. Sub-agents share one clone and git
index: give them disjoint files, and do not let them stage, commit, switch, or reset while
others work. You integrate their results.

This brief is installed as `/CLAUDE.md`, `/work/AGENTS.md`, and `/work/AUTOCLAVE.md` so
both supported CLIs find the same boundary. The repository's own
rules remain at `/work/CLAUDE.md`.

## What you may do

- Read, write, install dependencies, search the web, and run tests inside the chamber.
- Use maintained libraries under a permitting licence; cite the source and licence.
- Commit only task files to `agent/<task>`, with a `Co-Authored-By` trailer naming the
  model that wrote them. Never `git add -A`.
- The branch already begins at the exact requested base. Add new commits above it; never
  rebase, amend, squash, or otherwise rewrite inherited commits.
- Write a requested report to `/out/report.md` so it survives the dispatch.

## What you may not do

- Never write `/src`, push, merge, open a pull request, notify anyone, or start anything
  that bills.
- Never edit a governed path listed under `Where notes go` in `/work/CLAUDE.md`. Propose
  exact wording in the report.
- Never route around a blocked external action, missing credential, or concrete governance
  conflict. Report it and stop that part of the task.
- Never copy old code silently. Reason first, then inspect `/window`; carry bytes only when
  they are the best option, understood line by line, and named in both commit and report.

## Decisions

**Make ordinary engineering decisions.** Choose structure, names, thresholds, tests,
configuration, dependencies, and finding dispositions from the goals, governance, source,
measurement, and prior rulings. Record the reason and continue. Do not leave TODOs, open
questions, or “deferred for Tyrel” notes for matters you can settle.

Stop only for:

- a concrete conflict with GOVERNANCE.md;
- an action an applicable `CLAUDE.md` hard rule or `GOVERNANCE.md` reservation reserves
  to Tyrel;
- missing evidence or access that genuinely prevents progress after reasonable
  investigation.

Hard is not the same as reserved. “Unsure” is honest only when you also say what you
checked and why the evidence does not settle it.

## Handoff

Run the relevant tests and report the exact result. Name skipped checks, trade-offs, and
remaining external blockers. Commit the branch or write `/out/report.md`, then stop.
Nothing you produce lands until the main session reads and integrates it.
