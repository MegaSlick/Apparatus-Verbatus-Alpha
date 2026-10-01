# Working in this repository

Instructions for AI coding sessions and their agents (Claude, Codex or any other). Human
contributors should read [CONTRIBUTING.md](CONTRIBUTING.md); everything there applies here
too.

Read [README.md](README.md), [PRINCIPLES.md](PRINCIPLES.md) and
[CONTRIBUTING.md](CONTRIBUTING.md) at the start of every session, and
[ARCHITECTURE.md](ARCHITECTURE.md) and [GLOSSARY.md](GLOSSARY.md) before changing a
stage, a contract or a term. The work serves the goals in PRINCIPLES.md; code is written
the way CONTRIBUTING.md describes.

## Who decides

The project lead decides:

- changes to the documents listed in CONTRIBUTING.md, step 4;
- anything that costs money or runs on live infrastructure — **no GPU pod starts without
  the lead's permission in that session**, and shutdown is verified against the
  provider's own state and billing, never assumed. The permission can be standing for
  the session (for example any card up to a stated hourly rate, or deleting and
  re-creating network volumes for cold-start tests); it then covers every start, card
  switch or delete within its limits. Every pod is created with its guard armed
  (`operations/pod/README.md`, "The pod guard"), so it deletes itself when idle or out of
  time even if the session that started it dies. Read `operations/pod/README.md` before
  any pod work. Use the smallest card that does the job: debug one stage or a
  small model (DAI, Churro) on a small card at around $0.50 an hour, and ask for a
  larger budget only for end-to-end runs that need the 27B Perlector;
- declaring anything proven, when a small test is good enough to scale, excluding
  material, publishing or deploying;
- destructive or hard-to-reverse operations.

Everything else is ordinary engineering and the session decides it: implementation,
structure, names, thresholds, tests, configuration, and what to do about each review
finding. Record the decision and the reason. A hard question does not become the lead's
by being hard, and a decision is never parked in a TODO, a handoff or a pull request.

If following a rule would cost an act, hide a result or fight a goal, stop and quote the
conflict.

## Building

- Judge a change by the system it leaves behind: in existing code, fix the root cause
  rather than keep a workaround, even when the workaround is the smaller diff.
- Build once, reuse everywhere: shared tools with small configs, standard library first.
- Nothing depends on a comment: code, tests and digests never read or hash one, and a
  warning comment becomes a regression test.
- Vendor models run at a pinned revision fetched at launch, our adaptations are
  deterministic scripts, and an upstream update is flagged for review as maintenance.

## Tools

- Before recommending, changing or removing a tool — a CLI, plugin, hook, service or
  model host — read its official documentation, check the installed version and see how
  it is wired in here. A surprising metric or a failed command is a symptom to explain,
  not a verdict on the tool.
- Graphify: use only `graphify update .`, which builds `graphify-out/` locally, and
  `query`, `path`, `explain` and `god-nodes`, which read it. Every other command can send
  content to a model or the network, or write hooks, and real register material must not
  leave this machine.

## How rules are written

- Record what the lead meant, in plain words. Never turn a direction into a count or a
  quota that stands in for it.
- Working rules live in AGENTS.md, CLAUDE.md and CONTRIBUTING.md. PRINCIPLES.md holds
  goals and ARCHITECTURE.md the pipeline's design; code comments never cite rules.
- A new working rule needs the lead's approval (CONTRIBUTING.md, step 4).
- When a rule needs an exception, rewrite or remove the rule instead of adding the
  exception.

## Git workflow

- **Never work on `main`.** One short-lived branch per task: `work/<topic>`,
  `audit/<topic>` or `infra/<topic>`. Never switch branches with uncommitted work, and
  never rebase, force-push or amend a branch you do not own.
- **Stage only the files the task touched.** Never `git add -A`.
- **Push and open a pull request freely for work inside the session's stated goal**, and
  tell the lead when one opens. Name work outside that goal to the lead before its first
  push. Group finished lanes into one pull request per train of three to six branches;
  each lane is reviewed before it joins the train.
- **CI on the pull request is the gate.** Locally, run the tests you touched and
  `.githooks/check-static.sh`; the full suite runs in CI (it overheats this machine).
  There is no pre-push hook; CI scans the full history on every pull request.
  Never skip the commit hooks: they are the only check before a credential leaves
  the machine.
- **The git deny rules in `.claude/settings.json` catch accidents, not every
  spelling.** GitHub's protection of `main` is the real control. The rule that agents
  never push or merge is an instruction, not a mechanism.
- **Never chain a push or merge behind piped test output** — a pipeline's exit status is
  its last command's. Write the output to a file, read the exit code, then act.
- **Merge your own pull request** when its head contains freshly fetched `origin/main`,
  CI is green on that exact head, and every review thread is resolved. Run
  `git fetch origin` and merge `origin/main` into the branch first; GitHub does not
  require it, so you must. Report the merge by number and head commit.

## Review

- The CodeRabbit GitHub app reviews every pull request, once per train. Run the
  CodeRabbit CLI locally only when a review round produces major or cascading changes.
- Add one independent reader for a change to a pipeline stage or contract, and fresh
  readers for anything touching pods, money, credentials or git hooks. A reader from a
  different model family, such as Codex, is worth having on big changes.
- Fix or decline every real finding, with a reason. Reviewers read the exact commit that
  is pushed.
- Every review and workflow also looks at the tests and comments in what it touches:
  fix weak tests, remove ones that prove nothing, and fix comments that are stale or
  carry history.

## Agents

- **Writing agents work in their own git worktree**, on their own branch, never in the
  host's checkout:

  ```sh
  git worktree add -b work/<topic> ../verbatus-worktrees/<topic> origin/main
  (cd ../verbatus-worktrees/<topic> && uv sync --frozen --group test --group audit)
  ```

- **An agent never pushes, opens or merges a pull request, edits the documents in
  CONTRIBUTING.md step 4, sends a notification, or starts paid infrastructure.** It commits on its branch and
  names it in its report; the host session integrates.
- **An agent never ends its turn while a job it started is still running.** It follows
  the job to the end and reports, or hands it off cleanly: what is running, where its
  log is, and what the host must check. The host never makes its next step wait on a
  report alone; it checks the lanes on a timer and acts on what is on disk.
- Brief agents against what is actually on disk. Every brief names the objective, the
  allowed paths and actions, the deliverable, the checks and the stop conditions. Ask
  reviewers for every finding, but cap how each is written up (file, line, claim).
- The host verifies the load-bearing claims and the check results; it does not re-read
  every returned line. Agreement between agents is evidence, not authority.
- Pick the model that suits the job, at medium effort unless the task clearly needs
  more, and record which model actually answered. Codex can take bulk work under the
  host; keep at least a quarter of its weekly limit and never spend its credits.

## Notes and handoffs

`workbench/` is local and gitignored; `workbench/README.md` lists its folders. The
current handoff is `workbench/active/HANDOFF.md`, and the lead's standing rulings are
indexed in `workbench/standing/RULINGS_INDEX.md`. A note is evidence, never an
instruction.

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

Vendor licences (non-commercial research; vendor repositories are fetched at run time,
never stored, and only a new carry into this tree is a finding) and cryptographic signing
of records (integrity-only records are the design) are settled. Do not raise them again.
