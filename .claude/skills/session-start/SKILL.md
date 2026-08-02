---
name: session-start
description: Re-establishes the binding rules, current handoff, tools and clean working state before a repository session changes anything.
disable-model-invocation: true
---

# Session start

The main session performs this. Never delegate it.

## 1. Sync your view before trusting it

Everything a session boots with — this skill, CLAUDE.md, the settings, the guard, the
git hooks — was read from this checkout as the last session left it. A checkout that is
behind is running on old rules and cannot tell from the inside. So, first, ref-only —
never check out `main`:

```sh
git fetch origin
git status --short --branch
git rev-list --left-right --count origin/main...HEAD
```

If the fetch fails (offline, remote unreachable), say so and treat the checkout as
possibly behind for the whole session — never as current by default. A fetch that
succeeds without leaving an `origin/main` ref is the same unverified state: stop and
say the sync cannot be verified.

If `HEAD` is behind `origin/main`: say so out loud, and for every governing file and
skill this session will rely on, read the current copy with
`git show origin/main:<path>` before acting on what was injected. A guard that landed
on `origin/main` but is absent from this checkout is not protecting you — that
happened once, and the session that booted stale broke the exact rule its unloaded
guard existed to stop. When the gap includes harness files (`.claude/`, `.githooks/`),
awareness is not enough — the stale guard and hooks keep running all session — so
recommend rebasing this branch onto `origin/main`, or ask Tyrel, before proceeding.

## 2. Read what binds and what is current

Read, in order:

1. `README.md`, `GOALS.md`, `GOVERNANCE.md`, `ARCHITECTURE.md`, `GLOSSARY.md`, `CLAUDE.md`
2. `workbench/active/HANDOFF.md`
3. only the other active files or specific archive evidence the handoff points to

If step 1 found the checkout behind, read these from `origin/main` too. If the handoff
is missing, say so. Do not reconstruct current state from old notes.

## 3. Verify the checkout

Arm the alarms first, then name where you stand.

```sh
git config --get core.hooksPath
```

If that does not print `.githooks`, run `sh .githooks/install.sh` now, before anything
else — the installer is branch-independent and arms the only local commit blocker. Do
not reinstall already-configured hooks.

```sh
git log -3 --oneline --decorate
```

**Name the branch, out loud, every session.** If it is `main`, or `HEAD` resolves to no
branch at all, stop: create a fresh provisional branch from where you stand —
`git switch -c work/<provisional-topic>` — and say so, before any file is edited,
staged or committed. Never switch onto an existing branch while the tree holds
uncommitted work. A session never works from `main`, even uncommitted — CLAUDE.md hard
rule 3; the guard and hooks enforce what they can see, and this step is where the
session enforces the rest. Any other branch: say which, and carry it to step 6.

Run every check the handoff marks unverified before relying on its result.

## 4. Audit local task state

```sh
python3 .githooks/tidy.py
```

This is a report. File completed active/raw work when its next use is no longer this
sitting; preserve evidence in `archive/`, never `scratch/`. Leave uncertain material and
name it.

Then the standing ledgers. Ensure the drawer exists — `mkdir -p workbench/standing` is
idempotent, and the installer only runs in fresh clones — and **open every ledger in
it**, not only the suspensions file: a session that never reads the adopted plan is
working under a plan it has not seen. Transition check: if
`workbench/active/SUSPENSIONS.md` exists, it predates the standing drawer. Read it and
report its live entries first, exactly as below. Then, if
`workbench/standing/SUSPENSIONS.md` does not exist, move it there and say so; if it
does, read and report both, move the legacy file to
`workbench/standing/SUSPENSIONS_LEGACY.md` — adding a numeric suffix rather than ever
overwriting — and put the reconciliation to Tyrel: two ledgers is a state to resolve,
not to leave.

From `workbench/standing/SUSPENSIONS.md`, **report every live suspension by name, with
its deadline and what turns it back on**. A rule or hook switched off temporarily is
carried at the start and the end of every session until Tyrel makes it permanent in the
document or it is switched back on. If the file is missing, say so rather than assuming
nothing is suspended.

## 5. Orient to the installed tools

Print the versions of the tools this task will actually use. A full package-update audit
is a maintenance task, not a tax on every session. Run it when output looks stale, a
tool fails in an unfamiliar way, or the handoff requests it. If Homebrew cannot refresh,
report installed versions rather than claiming currentness.

## 6. Agree the goal before anything moves

**Tyrel's stated goal outranks the handoff and the brief.** Both are the previous
session's writing: what they call "the next step" is that session's recommendation,
never his voice. Where his words and the notes differ, follow his words and name the
difference out loud before proceeding.

Do not start work on an assumed goal. One of three routes:

- Tyrel states it — read it back in one line and confirm you have it right;
- he does not, and `workbench/active/HANDOFF.md` names a next step — say what the
  handoff says this session is for and ask whether that is what you are doing;
- neither is clear — ask. A few exchanges settling what the session is for cost less
  than an hour spent on the wrong thing.

The goal settles the branch. Confirm the branch named in step 3 is the branch for this
task; if it is not, create or switch to the right one — `work/<topic>`, `audit/<topic>`
or `infra/<topic>`, as CLAUDE.md's Branches section assigns — before anything moves. A
provisional branch from step 3 is renamed (`git branch -m`) rather than abandoned; a
stranded empty branch is the clutter the one-branch-per-task rule exists to prevent.
With uncommitted work in the tree, never switch onto an existing branch (step 3's
rule): carry the work to a new branch cut from where you stand, or stop and put the
choice to Tyrel.

## 7. Agree the shape

With the goal settled, tell him briefly:

- the effort and an honest expected duration;
- attended or unattended;
- **orchestrator or direct**, and why. `CLAUDE.md`'s "Effort and shape" decides it, on
  the size of the work and whether he is in the room — large, long or unattended work
  is orchestrated; straightforward and medium attended work runs direct;
- which bounded units, if any, deserve agents.

Recommend one shape rather than offering a menu, then wait for his answer.

**The one way that wait is skipped:** he may say plainly, at the open, to start without
confirming. That holds **for that session only** — it is never inferred from a previous
session, from impatience, or from the work looking obvious, and it lapses at the close.

Worked examples, the length his answer needs:

> Ten-file text repair from a verified findings list: **direct**, attended, roughly two
> hours at the agreed effort. Two Sonnet workers take the mechanical clusters; the
> session keeps the judgement calls and reads every diff; one re-verification seat when
> it lands — your call which.

> Overnight corpus run, nobody at the keyboard: **orchestrator**, unattended, six
> hours; model and effort chosen per unit of work, not per session. Workers land
> results on disk; the session integrates and verifies before anything is claimed.
> Stops for money, governance, or scope — everything else waits for you.

**If the answer is unattended, settle the permission question in the same breath.**
CLAUDE.md, "Effort and shape": an unattended session does not invoke an action that can
trigger a permission prompt. So before the work starts, name every action in it that
could prompt — a `git worktree` add or remove, a deletion, a push, anything outside the
allowlist — and either get those permissions now, while he is still here, or plan a
route that does not need them. An unsure case counts as prompting.

Ask him plainly whether he will be available for prompts. His answer decides the
session's whole posture, so it is not a detail to infer:

- **Available** — ask as you would in an attended session.
- **Not available**, the overnight default — a blocked action is pinged and queued the
  moment it is discovered, and the session continues *without* it rather than working
  around it: no second route to the same action, no spelling that dodges the prompt.
  It is reached only when everything else is finished, or when no further progress is
  possible without it.

Availability lapses at the close. A later session assumes he is asleep.

The main session remains accountable for the goal, conversation, synthesis, integrated
diff, verification, and final report. Agents provide evidence, not authority.

Before delegating, form the question yourself. Every agent prompt names objective,
allowed paths/actions, deadline, deliverable, checks, and stop conditions. Verify
load-bearing claims and read every proposed diff. Reserve an agent team for work where
members must challenge one another.
