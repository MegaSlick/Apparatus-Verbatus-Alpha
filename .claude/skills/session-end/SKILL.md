---
name: session-end
description: Close a state-changing session with verified Git state, recoverable filing, a concise handoff, and the next-session brief.
disable-model-invocation: true
---

# Session end

**Run this only when Tyrel asks for it.** Never start it on your own initiative — closing the
session, filing the drawers and messaging his phone is his call. When you think the moment has
come, say so and wait to be told.

The main session runs this procedure. It owns the facts and must not delegate the
handoff. Do not delete evidence or use destructive Git commands to make the state look
clean.

Review-only sessions still establish state and replace continuity documents, but do not
reorganize another session's files or send a completion notification.

## 1. Establish the exact state

Run unfiltered:

```sh
git status --porcelain
git rev-parse --abbrev-ref --symbolic-full-name '@{upstream}'
```

If an upstream exists, inspect `<upstream>..HEAD`. Otherwise compare with the declared
base that actually exists (`origin/main`, then `main`) and label the assumption. If no
base resolves, report that the ahead range is unknown.

Run checks proportional to the change. A build/change session normally runs the fast
gate during work and the full gate before handoff. Record any check not run or not yet
verified; do not call an unmeasured state clean.

## 2. File without loss

Before moving anything, run `python3 .githooks/tidy.py` in report-only mode. Use
`--file` only when this session owns `workbench/active/` exclusively; it moves only
byte-identical archived duplicates into recoverable `scratch/`.

Move notes for genuinely finished work to
`workbench/archive/<date>_<short-topic>/`. Move cited `raw/` evidence with its work.
Leave uncertain or still-actionable files active and name why. Never move evidence to
`scratch/` merely to empty a drawer.

## 3. Preserve continuity before replacing it

Create this session's archive directory. Copy the outgoing
`workbench/active/HANDOFF.md` and `NEXT_SESSION_BRIEF.md` there before overwriting
either. If a target exists, add a numeric suffix; never overwrite an archived account.

The new handoff contains only state that dies with this session:

- unverified work and exact verification command;
- local/ignored/external changes Git cannot show;
- concrete tooling traps;
- decisions and why, especially a deliberate non-action;
- loose ends with paths to detail;
- models that wrote lines;
- requests that need Tyrel.

Point to durable files instead of restating them. Omit empty sections. Keep a normal
handoff to one screen.

## 4. Brief the next session

Write `workbench/active/NEXT_SESSION_BRIEF.md` with:

- one queue line: goal, recommended model/effort, chunk size, honest duration, and
  whether it can run unattended;
- a short paste-ready prompt beginning with `/session-start`;
- tasks in priority order and standing limits such as no push or live pod;
- an instruction to stop cleanly and run this procedure if resources run low.

The prompt points at the handoff and brief; it does not duplicate them.

## 5. Report and notify

Report:

- branch, relationship to upstream/base, and whether the tree is clean;
- checks run and their results;
- what was archived, moved to scratch, or deliberately left active;
- external actions taken or explicitly not taken;
- every live entry in `workbench/active/SUSPENSIONS.md`, by name, with its deadline;
- the next queue line and anything Tyrel must decide.

For a normal state-changing session, send this last:

```sh
sh operations/notify/notify.sh done "<what landed; what needs Tyrel; next session>"
```

If it exits nonzero, say the notification was not accepted. Never claim a phone was
reached merely because a request was attempted.
