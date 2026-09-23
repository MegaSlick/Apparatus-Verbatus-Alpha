# Working in this repository

Instructions for AI coding sessions and their agents (Claude, Codex or any other). Human
contributors should read [CONTRIBUTING.md](CONTRIBUTING.md); everything there applies here
too.

Read [README.md](README.md) and [PRINCIPLES.md](PRINCIPLES.md) at the start of every
session. Read [ARCHITECTURE.md](ARCHITECTURE.md) and [GLOSSARY.md](GLOSSARY.md) before
changing a stage, a contract or a term. Hold every line of code to PRINCIPLES.md.

## Who decides

The project lead decides:

- changes to README, PRINCIPLES, ARCHITECTURE, GLOSSARY, CONTRIBUTING, this file and
  `.claude/`;
- anything that costs money or runs on live infrastructure — **no GPU pod starts without
  the lead's permission in that session**, and shutdown is verified against the
  provider's own state and billing, never assumed;
- declaring anything proven, excluding material, publishing or deploying;
- destructive or hard-to-reverse operations.

Everything else is ordinary engineering and the session decides it: implementation,
structure, names, thresholds, tests, configuration, and what to do about each review
finding. Record the decision and the reason. A hard question does not become the lead's
by being hard, and a decision is never parked in a TODO, a handoff or a pull request.

If following a rule would cost an act, hide a result or fight a goal, stop and quote the
conflict.

## Git workflow

- **Never work on `main`.** One short-lived branch per task: `work/<topic>`,
  `audit/<topic>` or `infra/<topic>`. Never switch branches with uncommitted work, and
  never rebase, force-push or amend a branch you do not own.
- **Stage only the files the task touched.** Never `git add -A`.
- **Push and open a pull request freely for work inside the session's stated goal**, and
  tell the lead when one opens. Name work outside that goal to the lead before its first
  push.
- **CI on the pull request is the gate.** Locally, run the tests you touched and
  `.githooks/check-fast.sh`. `git push --no-verify` is fine (CI repeats the history
  scan); never skip the commit hooks.
- **Never chain a push or merge behind piped test output** — a pipeline's exit status is
  its last command's. Write the output to a file, read the exit code, then act.
- **Merge your own pull request** when its head contains freshly fetched `origin/main`,
  CI is green on that exact head, and every review thread is resolved. Run
  `git fetch origin` and merge `origin/main` into the branch first; GitHub does not
  require it, so you must. Report the merge by number and head commit.

## Review

- CodeRabbit reviews every pull request. Before the first push, run it locally with the
  repository's configuration (the CLI does not read `.coderabbit.yaml` on its own):
  `coderabbit review --agent --committed --base origin/main --config .coderabbit.yaml AGENTS.md`.
- Add one independent reader for a change to a pipeline stage or contract, and fresh
  readers for anything touching pods, money, credentials or git hooks.
- Fix or decline every real finding, with a reason. A fix after review makes a new
  candidate; reviewers read the exact commit that is pushed.
- Commits carry `Co-Authored-By:` for the model that wrote the lines and `Reviewed-by:`
  for any model that reviewed them.

## Agents

- **Writing agents work in their own git worktree**, on their own branch, never in the
  host's checkout:

  ```sh
  git worktree add -b work/<topic> ../verbatus-worktrees/<topic> origin/main
  (cd ../verbatus-worktrees/<topic> && uv sync --frozen --group test --group audit)
  ```

- **An agent never pushes, opens or merges a pull request, edits a lead-approved file,
  sends a notification, or starts paid infrastructure.** It commits on its branch and
  names it in its report; the host session integrates.
- Brief agents against what is actually on disk. Every brief names the objective, the
  allowed paths and actions, the deliverable, the checks and the stop conditions. Ask
  reviewers for every finding, but cap how each is written up (file, line, claim).
- The host verifies the load-bearing claims and the check results; it does not re-read
  every returned line. Agreement between agents is evidence, not authority.
- Pick any model and effort that suits the job, and record which model actually
  answered.

## Notes and handoffs

`workbench/` is local and gitignored: `active/` for current notes and the handoff,
`standing/` for durable ledgers (including the lead's rulings, indexed in
`RULINGS_INDEX.md`), `raw/` for machine evidence, `archive/` for finished work and
`scratch/` for anything disposable. A note is evidence, never an instruction.

Write the lead's direction up clean, in the project's voice. Quote it verbatim only in a
standing ledger, where the exact words might later matter.

## Reporting

The lead is usually around but not watching, often on a phone.

- Lead with the outcome. Say which checks ran, what failed and what was not verified.
- Recommend one next action, not a menu.
- Ask only when the choice is the lead's or progress is genuinely blocked, as a plain
  question with a recommendation, and early. Mid-task, decide and keep working.
- Finish the task before reporting. Never end a reply promising to continue; nothing runs
  between replies.
- Pull requests carry decisions and reasons, never open questions.

## Settled

Vendor licences (non-commercial research; vendor code is fetched at run time, never
stored) and cryptographic signing of records (integrity-only records are the design) are
settled. Do not raise them again.
