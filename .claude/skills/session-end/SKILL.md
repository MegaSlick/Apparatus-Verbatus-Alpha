---
name: session-end
description: Close a state-changing session with verified Git state, recoverable filing, a boot-clean checkout, a concise handoff, and the next-session brief.
disable-model-invocation: true
---

# Session end

**Run this only when Tyrel asks for it.** Never start it on your own initiative — closing
the session, filing the drawers and messaging his phone is his call. When you think the
moment has come, say so and wait to be told.

**If the checkout stands on `main`, move off it before anything else** —
`git switch -c work/<topic>` — the guard refuses every write in a checkout standing on
main, including the handoff this procedure must produce.

The main session runs this procedure. It owns the facts and must not delegate the
handoff. Do not delete evidence or use destructive Git commands to make the state look
clean.

Review-only sessions still establish state and replace continuity documents, but move
nothing and send nothing.

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

## 3. Leave the checkout boot-clean

The next session boots on whatever this checkout holds — skills, CLAUDE.md, settings,
guard and git hooks all load from here before anyone has read a word. Park the checkout
**before** writing the handoff, so the handoff describes the state that is real, never
the state intended. Ref-only, never checking out `main`. Before parking, read
`git status --porcelain` — tracked and untracked alike; the gitignored workbench
never shows there. Empty: park. Not empty: the branch is not finished as it
stands — carry the work to the provisional branch (`git switch -c` brings the
working tree along), leave it uncommitted there, and name every carried file in
the handoff; never commit it onto a branch whose pull request already merged. If
the switch is refused because a carried file conflicts with `origin/main`, do not
force it: stay where you are, say the park did not happen, and name the
conflicting files in the handoff.

```sh
git fetch origin
git rev-list --left-right --count origin/main...HEAD
```

If the fetch fails, do not park blind: stay on the current branch, say the fetch
failed, and write "possibly behind origin/main — the sync step must run before
anything trusts this checkout" into the handoff.

- **This branch's work is finished** (its PR merged, or closed with a record): park on
  a fresh provisional branch cut from the remote tip —
  `git switch -c work/boot-<date>-<hhmm> --no-track origin/main` (time-suffixed;
  two closes can share a date). Then delete the finished branch, knowingly: this
  repository squash-merges, so `git branch -d` refuses even a genuinely merged
  branch's tip. Deletion needs two facts, both verified out loud:
  `gh pr view <number> --json state,headRefOid` shows `MERGED`, **and** the branch
  tip — read branch-qualified, `git rev-parse --verify refs/heads/<branch>`,
  because a bare name can resolve a same-named tag and prove the wrong ref —
  equals that `headRefOid`; a merged pull request says nothing about commits added
  afterwards. Both true: say so, then delete atomically with the verified tip
  pinned — `git update-ref -d refs/heads/<branch> <verified-oid>` — so a
  concurrent move of the branch between check and delete fails the delete rather
  than losing the move. `MERGED` false — a pull request closed unmerged is
  finished but never deletable this way — the branch stays, recorded as closed
  unmerged. Tip mismatch: the branch stays, and the handoff names the commits
  past the merged head.
- **Work continues on this branch:** stay on it, and write in the handoff how far
  behind `origin/main` it is, so the next session's sync step knows before it trusts
  anything local.

## 4. Preserve continuity, then write the handoff

Create this session's archive directory. Copy the outgoing
`workbench/active/HANDOFF.md` and `NEXT_SESSION_BRIEF.md` there before overwriting
either. If a target exists, add a numeric suffix; never overwrite an archived account.

The new handoff contains only state that dies with this session:

- the branch the checkout is parked on and its distance from `origin/main` — as they
  are **now**, verified by the commands above, never as intended;
- unverified work and exact verification command;
- local/ignored/external changes Git cannot show;
- concrete tooling traps;
- decisions and why, especially a deliberate non-action;
- loose ends with paths to detail;
- models that wrote lines;
- requests that need Tyrel.

**Label every decision and every queued step:** `Tyrel ruled (date)` for what he
actually decided, his words quoted or restated exactly; `session recommends` for
everything else; a completed act records its actual actor and date. The two must be
impossible to confuse — a recommendation written as a directive is how a note ends up
outranking him in a later session, and that has happened. And the handoff states only
what is true as it is written: a branch or file it names must exist, verified, not
intended.

Point to durable files instead of restating them. Omit empty sections. Keep a normal
handoff to one screen.

## 5. Brief the next session

Write `workbench/active/NEXT_SESSION_BRIEF.md`:

- first line, verbatim: "This brief is the outgoing session's recommendation; Tyrel's
  stated goal at the next open outranks every line of it.";
- one queue line, opening with its own provenance label like every other line: goal,
  recommended model/effort, chunk size, honest duration, and whether it can run
  unattended;
- tasks in priority order, **every task line carrying its own label** —
  `Tyrel ruled (date)` or `session recommends` — not only the queue line;
- a short paste-ready prompt beginning with `/session-start`;
- standing limits such as no push or live pod;
- an instruction to stop cleanly and run this procedure if resources run low.

The prompt points at the handoff and brief; it does not duplicate them.

## 6. Report and notify

Report:

- the branch the checkout is parked on, its relationship to `origin/main`, and whether
  the tree is clean;
- checks run and their results;
- what was archived, moved to scratch, or deliberately left active;
- external actions taken or explicitly not taken;
- every live entry in every suspensions ledger under `workbench/standing/` —
  `SUSPENSIONS.md` and any `SUSPENSIONS_LEGACY*` awaiting reconciliation — by
  name, with its deadline; if no suspensions ledger exists at all, say that,
  never an empty "none";
- the next queue line and anything Tyrel must decide.

For a normal state-changing session, send this last:

```sh
sh operations/notify/notify.sh done "<what landed; what needs Tyrel; next session>"
```

If it exits nonzero, say the notification was not accepted. Never claim a phone was
reached merely because a request was attempted.
