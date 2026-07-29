---
name: session-start
description: Re-establishes the binding rules, current handoff, tools and clean working state before a repository session changes anything.
disable-model-invocation: true
---

# Session start

The main session performs this. Never delegate it.

## 1. Read what binds and what is current

Read, in order:

1. `README.md`, `GOALS.md`, `GOVERNANCE.md`, `ARCHITECTURE.md`, `GLOSSARY.md`, `CLAUDE.md`
2. `workbench/active/HANDOFF.md`
3. only the other active files or specific archive evidence the handoff points to

If the handoff is missing, say so. Do not reconstruct current state from old notes.

## 2. Verify the checkout

Run:

```sh
git status --short --branch
git log -3 --oneline --decorate
git config --get core.hooksPath
```

If the last command does not print `.githooks`, run `sh .githooks/install.sh`. Do not
reinstall already-configured hooks.

Run every check the handoff marks unverified before relying on its result.

## 3. Audit local task state

```sh
python3 .githooks/tidy.py
```

This is a report. File completed active/raw work when its next use is no longer this sitting;
preserve evidence in `archive/`, never `scratch/`. Leave uncertain material and name it.

Then read `workbench/active/SUSPENSIONS.md` and **report every live suspension by name, with its
deadline and what turns it back on**. A rule or hook switched off temporarily is carried at the
start and the end of every session until Tyrel makes it permanent in the document or it is
switched back on. If the file is missing, say so rather than assuming nothing is suspended.

## 4. Orient to the installed tools

Print the versions of the tools this task will actually use. A full package-update audit is a
maintenance task, not a tax on every session. Run it when output looks stale, a tool fails in
an unfamiliar way, or the handoff requests it. If Homebrew cannot refresh, report installed
versions rather than claiming currentness.

## 5. Agree the goal before anything moves

Do not start work on an assumed goal. One of three routes:

- Tyrel states it — read it back in one line and confirm you have it right;
- he does not, and `workbench/active/HANDOFF.md` names a next step — say what the handoff
  says this session is for and ask whether that is what you are doing;
- neither is clear — ask. A few exchanges settling what the session is for cost less than an
  hour spent on the wrong thing.

## 6. Agree the shape

With the goal settled, tell him briefly:

- the effort and an honest expected duration;
- attended or unattended;
- **orchestrator or direct**, and why. `CLAUDE.md`'s "Effort and shape" decides it, on the size
  of the work and whether he is in the room — large, long or unattended work is orchestrated;
  straightforward and medium attended work runs direct;
- which bounded units, if any, deserve agents.

Recommend one shape rather than offering a menu, then wait for his answer.

The main session remains accountable for the goal, conversation, synthesis, integrated diff,
verification, and final report. Agents provide evidence, not authority.

Before delegating, form the question yourself. Every agent prompt names objective, allowed
paths/actions, deadline, deliverable, checks, and stop conditions. Verify load-bearing claims
and read every proposed diff. Reserve an agent team for work where members must challenge one
another.
