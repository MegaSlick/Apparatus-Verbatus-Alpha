# Working in this repository

Instructions for AI coding sessions and the agents they start (Claude, Codex or any
other). Everything in [CONTRIBUTING.md](CONTRIBUTING.md) applies too; this page adds what
is specific to AI work. Each rule carries its reason, so a case the rule did not foresee
can be judged by the reason.

At the start of a session, read [README.md](README.md), [PRINCIPLES.md](PRINCIPLES.md)
and [CONTRIBUTING.md](CONTRIBUTING.md). Before changing a stage, a contract or a term,
also read [ARCHITECTURE.md](ARCHITECTURE.md), [GLOSSARY.md](GLOSSARY.md) and the stage's
`CONTRACT.md`.

## Who decides

The project lead does not write code. The lead decides:

- **What costs money or runs on live infrastructure.** No GPU pod starts without the
  lead's permission in that session. The permission may be standing, with limits (for
  example, any card up to a stated hourly rate); it then covers every start, switch or
  delete inside those limits and nothing outside them. Every pod is created with its
  guard armed so it deletes itself when idle or out of time, and a pod counts as shut
  down only when the provider's own listing and billing say so. Read
  `operations/pod/README.md` before any pod work, and use the smallest card that does
  the job. The same goes for any paid API or paid credits on an outside model service.
  *Why: a forgotten pod bills by the hour, and only the lead can accept that cost.*
- **What the project claims and publishes**: declaring a result proven, scaling a
  small test up, excluding material, publishing or deploying. *Why: these are claims
  made in the lead's name.*
- **Destructive or hard-to-reverse operations**, such as deleting branches or data or
  rewriting history. *Why: the next session cannot undo them.*
- **Changes to the documents named in CONTRIBUTING.md**, which include this one.

Everything else is engineering, and the session decides it: design, code, names,
thresholds, tests, configuration, and what to do about each review finding. Record each
decision and its reason in the commit message or pull request. A hard question does not
become the lead's because it is hard, and a decision is never left in a TODO or a
handoff for someone else to make.

If following a rule here would lose an act, hide a result or work against
PRINCIPLES.md, stop and say which rule and why.

## Building

- **Fix the cause.** Judge a change by the code it leaves behind. Prefer fixing the root
  cause to adding a workaround, and deleting to adding.
- **Build once.** Reuse shared code and the standard library before writing something
  new.
- **No hidden contracts in comments.** Code, tests and digests never depend on a
  comment. When a comment is the only thing preventing a mistake, write a test instead.
- **Vendor models run at a pinned revision fetched at launch.** Our adaptations of
  them are deterministic scripts, and an upstream update is reviewed like any other
  change. *Why: a reading must be reproducible from its recorded model identity.*

## Data that never leaves the machine

Real register images, transcriptions and personal data stay on the lead's machines and
rented servers, in the gitignored `private/`, `scriptorium/` and `workbench/` folders.
Never commit them, paste them into a prompt for an outside service, or send them
anywhere beyond GitHub and CI.

Some tools upload what they read. Before using a tool on this repository for the first
time, check what it sends off the machine. For example, graphify is used here only as
`graphify update .` (builds a local graph) and `query`, `path`, `explain` and
`god-nodes` (read it); its other commands can send content to a model.

## Git

- **Never commit to `main`.** Use one short-lived branch per task, named for the work
  (`work/<topic>`, `fix/<topic>`, `docs/<topic>`). Never rebase, force-push or amend a
  branch you did not create. *Why: other people's checkouts depend on its history.*
- **Stage only the files the task changed.** Never `git add -A` or `git add .`. *Why:
  local folders and other agents' worktrees sit next to the code.*
- **Never skip the commit hooks.** They are the only check before a credential leaves
  the machine.
- **Push and open a pull request freely** for work inside the session's goal, and tell
  the lead when one opens. Name work outside the goal to the lead before its first push.
- **CI is the gate.** Before pushing, run the local checks in CONTRIBUTING.md; the full
  suite runs only in CI.
- **Never act on piped output.** A pipeline's exit status is its last command's, so
  `pytest | tail` can hide a failure. Write output to a file, check the exit code, then
  push or merge.
- **Merging.** A session may merge its own pull request when GitHub shows the branch up
  to date with `main`, the `check` status is green on that head, and every review thread
  is resolved. Report the merge by pull request number and head commit.

The git deny rules in `.claude/settings.json` catch accidents. They are not a security
boundary; GitHub's protection of `main` is.

## Review

- CodeRabbit reviews every pull request. It allows about one review an hour, so batch
  small related branches into one pull request rather than opening many.
- A change to a pipeline stage or a contract gets one independent reader besides the
  author. A change touching pods, money, credentials or git hooks gets a fresh reader.
  A reader from a different model family is worth having on large changes.
- Fix or decline every real finding, and give the reason. Reviewers read the exact
  commit that is pushed.
- While reviewing, also look at the tests and comments in what the change touches:
  fix weak tests, remove ones that prove nothing, and fix comments that are stale or
  carry history.

## Agents

A session may start agents to work in parallel. The session that started them is
responsible for their work.

- **Writing agents use their own git worktree and branch**, never the session's
  checkout:

  ```sh
  git worktree add -b work/<topic> ../verbatus-worktrees/<topic> origin/main
  (cd ../verbatus-worktrees/<topic> && uv sync --frozen --group test --group audit)
  ```

- **Agents commit on their branch and report it. They never push, open or merge a pull
  request, edit the documents named in CONTRIBUTING.md, send a notification, or start
  paid infrastructure.** The session does those after checking the work.
- **An agent finishes what it starts.** It does not end its turn while a job it started
  is running; it follows the job to the end or hands it off with what is running, where
  its log is and what to check.
- **Brief agents against what is on disk**: the objective, the files and actions
  allowed, the deliverable, the checks to run and when to stop. Ask reviewers for every
  finding, written as file, line and claim.
- **Agreement between agents is evidence, not proof.** The session checks the claims
  that matter and the check results itself, and records which model answered.

## Notes and handoffs

Notes and handoffs are local and gitignored, in `workbench/` (its README lists the
folders). The current handoff is `workbench/active/HANDOFF.md`. A cloud session, whose
container is discarded when it ends, keeps its handoff as a Markdown file at the root of
a dedicated branch that is never merged, and deletes the branch once the work has
merged. That branch is public like the rest of the repository, so it holds no register
material, credentials or personal data. A note is evidence, never an instruction.

## Reporting

The lead is usually reachable but not watching, and often on a phone.

- Lead with the outcome. Say which checks ran, what failed and what was not verified.
- Recommend one next action rather than offering a menu.
- Ask only when the choice is the lead's or the work is truly blocked, as a plain
  question with a recommendation, and early. Otherwise decide and keep working.
- Finish the task before reporting. Nothing runs between replies, so never end on a
  promise to continue.
- Pull requests carry decisions and their reasons, never open questions.
