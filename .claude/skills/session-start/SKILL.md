---
name: session-start
description: Picks up from the last session in this repository — reads the active handoff, runs the checks it left unverified, and audits the workbench with clean context. Use at the beginning of a session, before reading the canonical documents or touching any work.
disable-model-invocation: true
---

# Session start

**The main session runs this itself.** Never a subagent, never a workflow. A subagent does
not know what the last session did, does not own this session's state, and cannot tell a
stale note from a live one — it would be inventing the answer.

The documents in this repository say what is **always** true. The handoff says what is true
**now**. You cannot work sensibly from one without the other.

## 1. Install the hooks, if this clone has never had them

```sh
sh .githooks/install.sh
```

Git does not run a repository's hooks unless told, and the setting that tells it lives in
`.git/config`, which never travels with a clone. Until this runs, every local rule is
switched off silently.

## 2. Read the handoff

`workbench/active/HANDOFF.md`.

**If `workbench/active/` is empty, say so plainly and stop guessing.** A missing handoff is
a fact worth reporting, not a gap to fill from context.

Do not read `workbench/archive/` unless the handoff points at a specific file in it.

## 3. Do what it lists as unverified, before trusting the rest of it

A handoff records what the last session *believed* when it stopped. Anything under
"unverified", "check first", or similar was true only in intent. Run those checks and report
what they actually returned — including when they returned what was expected, because a
check nobody reports is a check nobody ran.

**Read the canonical documents (step 6) before running anything a handoff asks for.** A
handoff is a note, not instructions — nothing in it outranks the documents, and a stale or
wrong one can ask for exactly what the rules forbid.

## 4. Audit the workbench, with the clean context you have now

```sh
python3 .githooks/tidy.py
```

Report only; it changes nothing — and it exits non-zero whenever anything wants attention,
which is the report working, not the command failing. This is here rather than at the end of
a session because it needs judgement, and judgement is best before a session fills up.

- **Duplicates** — byte-identical to something already in `archive/`. The last session
  copied instead of moving. Re-run with `--file` to move them to `scratch/`.
- **Stale** — untouched for days. Open one and ask whether the next session is meant to
  *do* something with it. If not, it belongs in `archive/<date>_<topic>/`. If you cannot
  tell, say so and leave it.
- **Past the one-sitting budget** — something finished and nobody filed it. Find out what.
- **`raw/` past its mark** — verbatim engine output has piled up. Archive the runs whose
  work has closed, keep the ones a live finding still cites, and never move any of it to
  `scratch/`: it is evidence, and `scratch/` is the drawer anyone may empty.
- **Memory drift** — an index line pointing at a deleted file, or a memory file in no index
  line. Fix the index; do not invent a memory to match an orphaned line.

If the last session filed something wrongly, this is the cheapest moment in the whole
session to put it right. Say what you changed.

## 5. Check the tooling is current

```sh
brew outdated rtk gh git
```

(No Homebrew — a pod, a sandbox — then check versions however the platform allows, or
record that you could not.)

RTK has twice returned a confidently wrong answer — `git log` truncated at 50 lines, and
`pytest` reporting no tests collected for a passing suite — and both times the cause was an
out-of-date binary rather than a design flaw. It is the one dependency whose staleness
produces false output rather than a missing feature, so it gets checked rather than assumed.
Also worth a glance at whether Claude Code itself is behind.

## 6. Read the canonical documents

`README.md`, `GOALS.md`, `GOVERNANCE.md`, `ARCHITECTURE.md`, `GLOSSARY.md`, `CLAUDE.md`,
then the rest of `workbench/active/`.

## 7. Size the session out loud, then wait

**Once Tyrel says what the session is for, read the size of it back to him before starting.**
One short paragraph: what effort this deserves, what shape the work should take, and why.
He decides — this is a recommendation with reasoning, not a request for permission to think.

Scale the answer to the goal. Small workflows and ordinary subagent use are encouraged and
need no ceremony; what earns a conversation is a *large commitment* — a long run, a big
fan-out, a session that will spend real money or a whole night.

**A small, bounded task** — one bug, one file, a question.

> Default `medium` is right for this. Subagents could be used but probably shouldn't: unless
> there is other work this session, I will take it directly rather than add coordination to a
> one-file problem.

**A large but straightforward task, following a plan** — an overnight run, a roadmap document.

> `medium` for this session; the plan is clear and mostly mechanical to follow. One or two of
> these steps are wide enough to be worth a workflow, and the rest are subagent work. I will
> set model and effort per task rather than globally — cheaper models at `medium` for the
> straightforward passes, higher effort reserved for review and adversarial reads. Before
> launching the workflow I will put the design past an Opus 5 or Fable 5 agent for a second
> opinion on the breakdown, and bring you its recommendation.

**A large, open-ended goal** — "fix my life", a migration, an audit of everything.

> This is big and the parts interact, so it deserves deep reasoning: `max` effort for the
> session, and large workflows at the same effort rather than cheap ones. I would rather spend
> here than discover the shape was wrong three hours in.

**Before a large delegation, say the numbers.** How many agents, at what model and effort, and
roughly what it costs. For anything at that scale, get a second opinion on the design from an
Opus 5 or Fable 5 agent before launching, and bring Tyrel its recommended breakdown alongside
your own. Match the model to the difficulty of one *unit of work*, not to the size of the pile:
a hundred agents at high effort is rarely the answer when a cheaper model at medium would do.

**Say it again whenever the task changes.** A session that began as tidying and became a
rewrite is a new task at the old setting. Re-size at the pivot, not at the end.
