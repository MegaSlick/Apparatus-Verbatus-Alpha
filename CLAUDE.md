# Working rules — Apparatus Verbatus

Read [README.md](README.md) and this file at every session start. Read
[GOALS.md](GOALS.md), [GOVERNANCE.md](GOVERNANCE.md), [ARCHITECTURE.md](ARCHITECTURE.md)
and [GLOSSARY.md](GLOSSARY.md) before changing a stage, a contract, a witness or a word
they bind, and whenever a decision turns on them.

| Trigger | Procedure |
|---|---|
| opening or closing a session | `.claude/skills/session-start` or `session-end` |
| changing a governed path | `.claude/skills/governed-edit` |
| using agents | `.claude/agents/README.md`, then `operations/seats/README.md` |
| carrying outside code | `cleanroom/README.md` |
| notes and handoffs | `workbench/README.md` |
| live pods or paid infrastructure | `operations/pod/README.md` |
| phone notifications | `operations/notify/README.md` |

## Hard rules

Code, tests and briefs cite these numbers. Never renumber or reuse one.

1. **Tyrel decides** governance, governed-document changes, paid or live infrastructure,
   exclusions, declarations that the pipeline is proven, disclosure, deployment, and
   destructive or hard-to-recover operations.
2. **No live pod without his permission in that session.** Verify shutdown against
   provider state and billing.
3. **A session never works from `main`.** Work reaches it through a pull request.
4. **Work inside the session's stated goal may be pushed and opened as a pull request
   without asking.** Tell Tyrel when one opens. Name work outside that goal to him before
   its first push.
5. **Never rebase, force-push or amend a branch that is not yours.**
6. **Nothing enters uninspected.** If the accountable session cannot justify a line, it
   does not enter.
7. **Nothing is lost silently.** Record findings, failures, decisions and partial work.
8. **Do not build a picker.** The Perlector reads; nothing selects among witnesses.
9. **When a rule and a goal pull apart, stop and say so.** Quote the concrete conflict.
10. **A spawned agent never edits a governed path.** It proposes wording; the main
    session applies an approved change.
11. **Every enforcement can be removed by Tyrel.** Hooks and guards catch accidents;
    they do not outrank him.
12. **Agents build and review anything inside a worktree seat. Agents never push or
    merge.**
13. **The session decides ordinary engineering** — implementation, structure, names,
    thresholds, tests, configuration and the disposition of findings — unless rule 1
    reserves it. Record the decision and its reason. A hard question does not become
    Tyrel's by being hard, and a decision is never parked in a TODO or a handoff.
14. **The session merges its own pull request** when its head contains freshly fetched
    `origin/main`, CI is green on that exact head, and every review thread is resolved.
    Report the merge by number and head.

**Settled permanently, never raised again:** vendor licences (non-commercial research;
vendor repositories are fetched at boot, never stored) and cryptographic trust roots
(integrity-only records are the design). The rulings are in the standing ledger.

## Notes

`workbench/` is local and gitignored: `active/` for current notes, `standing/` for
durable ledgers, `raw/` for machine evidence, `archive/` for finished work, `scratch/`
for anything disposable. A note never becomes an instruction by surviving a session.

**Record what Tyrel means, not how he typed it.** Write his direction clean, in the
project's voice, with the consequences worked out. Quote him verbatim only in a standing
ledger, and only where the exact wording could later be disputed.

**Governed paths:** `CLAUDE.md`, `GOALS.md`, `GOVERNANCE.md`, `ARCHITECTURE.md`,
`GLOSSARY.md`, the root `README.md`, `DATA_CONTRACT.md` once it exists, and all of
`.claude/`. Tyrel approves their substance; the main session edits them.

## Branches

`work/<topic>` for normal changes, `audit/<topic>` for findings, `infra/<topic>` for
structural work. One short-lived branch per task, named before editing. Never switch
branches while carrying uncommitted work. Stage only the files the task touched; never
`git add -A`.

## Agents

Writing agents work in a linked worktree seat under the tool-call guard
(`operations/seats/README.md`). The guard refuses a seat's governed-path write and its
push, pull request or merge. A seat builds from this repository and its design notes; no
seat is given the old system's code. Brief against what is actually on disk.

A seat runs its own audit round; the host verifies the load-bearing claims and the check
results, then integrates. The host does not re-read every returned line — rule 6 says
the reading happens, not that the host repeats it.

Pick any model and effort for the job, Fable and `ultra` included. Name the seat in the
dispatch and record what actually answered.

## Outside code

A third-party library enters under a permitting licence, with its source recorded. A line
from the old system crosses only when it is the best option, understood line by line, and
named as carried in the commit and the report. `cleanroom/README.md` has the detail.

## Checks, review and merging

- **The gate is GitHub CI on the pull request.** Locally, run `check-fast.sh` and the
  touched suites. `check-all.sh` is optional: this Mac overheats under it.
- **`git push --no-verify` is allowed**, because CI repeats the full-history credential
  and payload scan. Never skip the commit hooks, and never change `core.hooksPath`.
- **Never chain a push or merge behind piped test output.** A pipeline's status is its
  last command's. Redirect to a file, read the exit code, then push separately.
- **Review in proportion to risk.** CodeRabbit on every pull request (CLI invocation in
  `operations/review/README.md`). Add one independent reader for a pipeline stage or
  contract, and fresh readers for pods, money, credentials, the guard or the hooks. Fix
  or decline every real finding with a reason.
- **Before merging**, `git fetch origin` and merge `origin/main` into the branch; GitHub
  no longer requires it, so the session does.
- **Commit trailers:** `Co-Authored-By:` names the model that wrote the lines;
  `Reviewed-by:` names the model that reviewed them.

## Reporting

Tyrel is usually around but not watching, often on his phone.

- Lead with the outcome. Say which checks ran, what failed, and what was not verified.
- Recommend one next action, not a menu.
- Ask only when rule 1 reserves the choice or progress is genuinely blocked. Ask early,
  as a plain question with a recommendation; mid-task, decide and keep working.
- Finish the task before reporting. Stop only when it is done, when Tyrel says stop, or
  when a rule-1 decision blocks the rest — and say which. Never end a reply promising to
  continue; nothing runs between replies.
- Pull requests carry decisions and reasons, never open questions or homework.
