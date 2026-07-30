---
name: session-end
description: Close a state-changing session with verified Git state, recoverable filing, a concise handoff, a boot-clean checkout, and the next-session brief.
disable-model-invocation: true
---

# Session end

**Run this only when Tyrel asks for it.** Never start it on your own initiative — closing
the session, filing the drawers and messaging his phone is his call. When you think the
moment has come, say so and wait to be told.

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

Before moving anything, run `python3 .githooks/tidy.py`. It reads and reports; it
moves nothing, and it has no mode that does. Anything it names as already archived
and byte-identical is a candidate you decide on — only this session knows which work
closed.

Move notes for genuinely finished work to
`workbench/archive/<date>_<short-topic>/`. Move cited `raw/` evidence with its work.
Leave uncertain or still-actionable files active and name why. Never move evidence to
`scratch/` merely to empty a drawer.

Standing ledgers live in `workbench/standing/` and are never filed — they outlive
sessions by design. When filing is done, `active/` should be back inside its budget;
if it is not, name each file that stays and why it is still this coming sitting's work.

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

**Label every decision and every queued step:** `Tyrel ruled (date)` for what he
actually decided, in his words or with his words quoted; `session recommends` for
everything else. The two must be impossible to confuse — a recommendation written as a
directive is how a note ends up outranking him in a later session, and that has
happened. And state only what is true when the handoff is written: a branch or file the
handoff names must exist, verified, not intended.

Point to durable files instead of restating them. Omit empty sections. Keep a normal
handoff to one screen.

## 4. Brief the next session

Write `workbench/active/NEXT_SESSION_BRIEF.md` with:

- a first line saying the brief is this session's recommendation, and that Tyrel's
  stated goal at the next open outranks it;
- one queue line: goal, recommended model/effort, chunk size, honest duration, and
  whether it can run unattended — labelled like the handoff;
- a short paste-ready prompt beginning with `/session-start`;
- tasks in priority order and standing limits such as no push or live pod;
- an instruction to stop cleanly and run this procedure if resources run low.

The prompt points at the handoff and brief; it does not duplicate them.

## 5. Leave the checkout boot-clean

The next session boots on whatever this checkout holds — its skills, CLAUDE.md,
settings, guard and git hooks all load from here before anyone has read a word. A
session parked on a stale base hands the next session stale rules; that is how a landed
guard once failed to fire. So, ref-only, never checking out `main`:

```sh
git fetch origin
git rev-list --left-right --count origin/main...HEAD
```

- **This branch's work is finished** (its PR merged, or closed with a record): park the
  checkout on a fresh provisional branch cut from the current remote tip —
  `git switch -c work/boot-<date> --no-track origin/main` — then delete the finished
  branch with `git branch -d` (it refuses if anything is unmerged; do not force it).
- **Work continues on this branch:** stay on it, and write in the handoff how far
  behind `origin/main` it is, so the next session's sync step knows before it trusts
  anything local.

Name the branch the checkout is parked on in the handoff either way.

## 6. Report and notify

Report:

- the branch the checkout is parked on, its relationship to `origin/main`, and whether
  the tree is clean;
- checks run and their results;
- what was archived, moved to scratch, or deliberately left active;
- external actions taken or explicitly not taken;
- every live entry in `workbench/standing/SUSPENSIONS.md`, by name, with its deadline;
- the next queue line and anything Tyrel must decide.

For a normal state-changing session, send this last:

```sh
sh operations/notify/notify.sh done "<what landed; what needs Tyrel; next session>"
```

If it exits nonzero, say the notification was not accepted. Never claim a phone was
reached merely because a request was attempted.
