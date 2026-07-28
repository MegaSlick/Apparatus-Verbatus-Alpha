---
name: session-end
description: Closes a session in this repository — files the work that finished, archives the outgoing handoff, and writes the one that replaces it. Use before finishing any session that changed state, learned something non-obvious, or left work unfinished.
disable-model-invocation: true
---

# Session end

**The main session runs this itself.** Never a subagent, never a workflow. Only the session
that did the work knows what it did, what it decided, and what it left half-finished. A
subagent asked to write a handoff will reconstruct a plausible one from the files, which is
worse than no handoff because it reads as a record.

**Nothing here deletes anything.** Duplicates move to `scratch/` — deferred deletion,
recoverable only until someone empties that drawer — and finished work moves to `archive/`,
which is kept. This runs at the point in a session where context is
fullest and mistakes are likeliest, so it is built so a mistake costs a move and never a
loss. If you are reaching for `rm`, you have left the procedure.

Work through it in order. Filing before writing is not a preference: the handoff you are
about to replace is the only account of how the last session reached its conclusions.

## 1. Establish what you are leaving behind

```sh
rtk proxy git status --porcelain
rtk proxy git log --oneline origin/main..HEAD
```

Use `rtk proxy git`, not bare `git` — the proxy path is the one output the summarising
wrapper is guaranteed not to filter. The wrapper's known failures (a truncated `git log`, a
passing suite reported as no tests) were fixed by upgrading RTK, but a stale binary brings
them back silently — which is why `/session-start` checks `brew outdated rtk` and this step
does not trust the filtered path at the moment of record.

Uncommitted changes are a fact the next session needs. Do not tidy them away to make the
report look clean.

**Work reaches a pull request by default.** Anything the status shows that is not on its
way into one — an uncommitted line, an unpushed commit, a branch with no PR — is either
routed deliberately (read the diff, commit it with the push it belongs to) or named in the
handoff with the reason it stays behind. That includes Tyrel's own hand edits: read the
diff, then let them ride the next push inside a **normally attributed** commit, rather than
sitting +1/−1 for five sessions.

`ALLOW_UNATTRIBUTED=1` is **not** the route for those. CLAUDE.md reserves it for a commit
no machine touched at all, and a session that reaches for it to sweep up a hand edit is
recording "no model wrote this" over a commit a model helped assemble.

## 2. File what is provably filed already

```sh
python3 .githooks/tidy.py --file
```

Moves anything in `active/` byte-identical to an archived copy into `scratch/`. Safe at any
context, because a checksum decides it rather than you.

## 3. File the work that finished

For each piece of work that **finished**, move its notes to
`workbench/archive/<date>_<topic>/`. Now, not later.

The test is not how recent a file is. It is: **can you say what the next session is meant
to do with it?** If you cannot, it is not active, however good it is.

Leave anything you are unsure about, and name it in the handoff as unsure. An unfiled note
costs the next session a minute; a wrongly filed one costs it the thread.

**`raw/` is filed with the work, not on its own.** A run's transcripts move to
`workbench/archive/<date>_<topic>/` alongside the notes that cite them, so the finding
and the evidence for it stay together. While a live finding still points at a
transcript, the transcript stays where it is — a citation to a file nobody kept is
worse than no citation, because it reads as though somebody checked.

Do not empty `raw/` to make the drawer look tidy. It is evidence: `scratch/` is the
drawer for things that may vanish, and nothing should ever be moved from `raw/` into it.

## 4. Check project memory

The tidy report flags an index line pointing at nothing, or a file in no index line. Beyond
that: did this session learn something still true in a month and not recoverable from the
repository — a trap in the tooling, a working preference, a constraint nothing records? That
is a memory. If it is only true until the next session, it is a handoff line. Most things
are.

## 5. Archive the outgoing handoff, then write the new one

Copy `workbench/active/HANDOFF.md` into this session's archive folder **before** overwriting
it. If that folder already holds a `HANDOFF.md`, suffix the copy (`HANDOFF-2.md`) rather
than overwrite — the archive never loses an earlier account.

### The one rule

**Write only what dies with this session.**

Anything recoverable from the repository, the git history, or an output folder does not go
in the handoff — point at it in one line instead. A handoff that restates the record is
context bloat, and the next session pays for every word.

Before writing a paragraph, ask: *could the next session find this out by looking?* If yes,
give the path and move on.

### Map problems; do not enumerate them

One line per problem, naming where the detail lives.

> **The two readers disagree on how far the defect reaches** — the counts differ and
> neither is verified. Detail in `workbench/active/<the note that holds both counts>.md`.

Not three paragraphs restating both sides.

### What belongs

Most sessions have little in some of these and nothing in others. Empty sections get
deleted, not filled.

- **Unverified at handoff time.** Anything still running, or done but unchecked, with the
  exact commands to check it. The most valuable section and the most often missed.
- **Tools built this session that live somewhere ephemeral.** A scratchpad dies with the
  session. Move it to `workbench/tools/` and say what it is for in one line.
- **Traps.** Where the tooling, the data, or a prepared script lies to you, with the
  specific symptom. "X silently truncates at 50 lines" is useful; "be careful with X" is not.
- **Decisions and why**, especially decisions *not* to do something.
- **Changes nobody will find by reading the repository.** Local settings, permissions,
  anything gitignored, anything outside the repo.
- **Loose ends**, one line each, with a pointer.
- **Which models wrote what.** Name this session's model, and any subagent on a different
  model that wrote lines rather than only reading. The commit trailers are built from this, and
  nobody can reconstruct it afterwards.
- **What needs Tyrel**, kept separate from what needs the next session.

### What does not belong

- Status, dates, counts and hashes the record already holds.
- Anything already in a canonical document — quote a rule and it will drift from the rule.
- Narration of what you did. The next session needs the *state*, not the story.
- Praise for the work, or a summary of how much was done.

### Length

**One screen for a normal session.** Longer and you are restating the record. The only
sessions that earn more are ones that produced tools, traps, or unverified state, and the
extra length should be those three things and nothing else.

## 6. Report what moved

What was archived, what went to `scratch/`, and what you deliberately left in `active/` and
why.

**This is not the end of the procedure.** Steps 7 and 8 are mandatory: the next session is
briefed and its opening prompt written, and only then is Tyrel told the session is over. A
session that reports what moved and stops here has left the next one with no queue line and
left him with no notification — which is most of what this skill exists to produce.

**A session that both starts and ends in one sitting still writes one.**

## 7. Brief the next session, and write the prompt that starts it

Two things, both in the chat where he can reach them.

**First, the queue line:** **the goal, the model and effort to open with, the size of the
chunk, and an honest duration — including whether it can run overnight unattended.** Tyrel
queues sessions from this line, often at night, and an under-scoped session wastes the run.

> Next: the launch contract and budget guard. Open as Opus 5 medium. Large chunk, tests
> first; 4–8 hours unattended once the pod-scripts decision is answered; overnight-capable.

If the honest answer is thirty minutes of cleanup, say that instead — a short session
queued as a night is a wasted night, and so is an eight-hour chunk queued as a coffee break.

**Then the prompt itself, ready to paste.** A block he can copy into a fresh session and
send without editing. He is often launching at midnight and should not have to compose
anything; asking a tired human to write the prompt is how a night gets queued wrong.

The prompt opens with `/session-start` so the new session runs its own procedure, and it
**points at the documents rather than restating them** — the handoff and any brief are on
disk, and a prompt that repeats them burns the context the session was meant to save. What
it must carry in its own words: whether the session is attended, the effort to open at, the
tasks in priority order, the standing limits (what it may not do), and what to do when it
runs out of budget, time, or power.

> ```
> /session-start Unmonitored overnight run. Read workbench/active/HANDOFF.md and
> workbench/active/NEXT_SESSION_BRIEF.md first; the brief has the tasks and the limits.
> Open at <model> <effort>. Do not push. Stop cleanly and write the handoff if you run low.
> ```

Keep it short enough to read at a glance and complete enough to run unattended.

## 8. Tell him it is over

```sh
sh operations/notify/notify.sh done "<what landed; what needs him; next session, model, size>"
```

One line, sent last, after the handoff is written. He is often away when a session ends, and
the handoff he cannot see yet is the whole reason this exists. If something needs him, say
which thing — "3 files landed, checks green, needs the reviewer pass" beats "session complete".

CLAUDE.md's Reporting section holds the other three events and the rule about noise.
