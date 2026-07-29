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

## 4. Orient to the installed tools

Print the versions of the tools this task will actually use. A full package-update audit is a
maintenance task, not a tax on every session. Run it when output looks stale, a tool fails in
an unfamiliar way, or the handoff requests it. If Homebrew cannot refresh, report installed
versions rather than claiming currentness.

## 5. Size and lead the session

Tell Tyrel, briefly:

- the effort and expected duration;
- whether this is attended or unattended;
- which bounded units, if any, deserve agents and why.

The main session remains accountable for the goal, conversation, synthesis, integrated diff,
verification, and final report. Agents provide evidence, not authority.

Before delegating, form the question yourself. Every agent prompt names objective, allowed
paths/actions, deadline, deliverable, checks, and stop conditions. Agent output is evidence,
not authority; verify load-bearing claims and read every proposed diff.

`CLAUDE.md` currently requires agent-first orchestration and an announced wait. Follow that
binding rule until Tyrel explicitly approves a governing amendment. Keep each assignment
bounded, and reserve an agent team for work where members must challenge one another.
