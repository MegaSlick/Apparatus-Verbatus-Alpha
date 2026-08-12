---
name: session-end
description: Close a state-changing session with verified Git state and a concise handoff.
disable-model-invocation: true
---

# Session end

Run this when Tyrel asks to close the session. The main session owns it.

## 1. Establish state

Run unfiltered:

```sh
git fetch origin
git status --porcelain
git status --short --branch
git rev-list --left-right --count origin/main...HEAD
python3 .githooks/tidy.py
```

If fetch fails, do not measure divergence or park. Still write the handoff: record the
fetch failure, dirty tree, partial work, and skipped checks, then report the notification
outcome without claiming divergence was verified.

Run checks proportional to the change. Name every skipped or failed check. Do not make
the tree look clean by deleting evidence or discarding work.

## 2. File finished notes

Move genuinely completed task notes and cited raw evidence to one
`workbench/archive/<date>_<topic>/` directory. Leave active material active and say why.
Standing ledgers remain in place. Preserve the outgoing handoff and next-session brief in
the archive before replacing them; never overwrite an archived file.

## 3. Leave a truthful handoff

`workbench/active/HANDOFF.md` contains only state the next session cannot derive cheaply:

- branch, distance from `origin/main`, and dirty files;
- checks not run or not green;
- external or gitignored state;
- decisions and their reasoning;
- actual blockers, with paths to evidence;
- models that wrote committed lines;
- actions still requiring Tyrel under hard rule 1.

Use `Tyrel ruled (date)` only for his decision and `session decided` or
`session recommends` for the session's work. Do not turn completed engineering choices
back into questions.

Write `workbench/active/NEXT_SESSION_BRIEF.md` only when another session needs a specific
queue. Its first line says Tyrel's next stated goal outranks it. Keep it short.

## 4. Park safely

If work continues, stay on its branch. If the task is finished and
its pull request is verified merged, park on a fresh `work/boot-<date>-<time>` branch from
`origin/main`. Delete a finished local branch only after the pull request reports
`MERGED` and its `headRefOid` equals the exact branch tip; pin that oid in the delete.
Otherwise keep the branch and say why.

## 5. Leave machine state explicit

List chambers. If none run and this session started Colima, stop it. If any chamber still
runs, leave Colima up and name the chamber and purpose. Never destroy uncollected work to
tidy the close.

Report the final branch/tree state, checks, filing, external actions, live suspensions,
and the next action. Send the `done` notification through `operations/notify/notify.sh`;
say plainly if delivery fails.
