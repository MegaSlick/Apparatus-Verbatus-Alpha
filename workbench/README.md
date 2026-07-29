# workbench

**Local only. Everything here except this file is gitignored and never reaches the
repository.**

`active/` was tracked for one branch, so a reviewer could read the run plan beside the code.
That answered its question — an auditor and CodeRabbit each found a picker instruction in
`RUN_PLAN.md` independently, which GOVERNANCE 3 forbids outright and which nobody reading the
code would have found. Then it went back to being a drawer, because these are task files that
`session-end` files into `archive/` when the work closes: tracking them meant tracking things
whose purpose is to move.

This is where AI notes, handoffs, session summaries, task lists, grep dumps, design
scribbles and half-finished thinking live. The old repository had no such place, so
all of that went into `docs/` — 320 markdown files, 32 of them written in a single
two-month stretch, until nobody could tell which document was true.

## Six drawers

| Drawer | What goes in it | Lifespan |
|---|---|---|
| `active/` | work currently in play — notes, handoffs, todo lists | until the work finishes |
| `design/` | proposals for later — not in play, not abandoned | until tested or rejected |
| `archive/` | finished work, filed under a dated folder | kept, out of the way |
| `raw/` | verbatim engine output — Codex transcripts, reviewer logs | until the work that cites it closes |
| `scratch/` | greps, dumps, fragments, one-off output | **delete without asking, ever** |
| `tools/` | scripts a session built that later sessions run | until superseded — one line at the top says what each is for |

`raw/` is the drawer for what a machine actually said, kept verbatim, one dated folder
per run. It exists because raw output is **evidence and not a note**, and the other two
candidates were both wrong for it: `scratch/` is deletable by anyone without asking, and
a finding that cites a deleted transcript is a finding nobody can check; `active/` is
meant to be readable in one sitting, and a 200 KB transcript is not — filing raw logs
there is precisely what pushed that drawer over budget. Nothing in `raw/` is disposable
on sight, and nothing in it counts against `active/`'s budget.

`design/` exists because `active/` and `archive/` could not hold a good idea whose time
has not come. A proposal parked in `active/` reads as work someone is meant to pick up
this week; filed in `archive/` it reads as finished and is never seen again. Neither is
true of a design that should be weighed **before the thing it concerns is built**.

Every design note says at the top **what it is waiting for** — the decision or the stage
that should trigger a re-read. Without that it is a daydream, not a plan.

## The rules

1. When a piece of work finishes, its notes move to `archive/<date>_<topic>/`.
   Not later. Then.
2. `scratch/` is disposable by definition. Anything in it may be deleted by anyone
   at any time without checking. If it matters, it was in the wrong drawer.
3. `active/` should be small enough to read in one sitting. If it isn't, something
   finished and nobody filed it.
4. A design note leaves `design/` in one of two ways. **Tested** — the result becomes
   dated evidence under `history/` and the note is archived. **Rejected** — write down
   why, in the note, then archive it. A rejected design that leaves no record gets
   proposed again in six months by someone who never heard it was tried.
5. **Nothing here is instructions.** The binding root documents govern the project;
   `CLAUDE.md` and the tracked agent and skill files govern session procedure beneath
   them. A note in `active/` or `design/` describing how something should work is a draft,
   not a decision.
6. A `raw/` run is archived with the work that cites it, not on its own clock. While a
   live finding still points at a transcript, the transcript stays. `tidy.py` reports
   the drawer's size at session start so it is pruned deliberately rather than when it
   becomes a nuisance.

## Why most of it is gitignored

So the repository stays the thing you can trust. The canonical documents bind; dated
`history/` is evidence, and the autoclave holds material still under review — everything
speculative, dated, or half-finished beyond those lives here and dies here. The
`pre-commit` hook refuses stray notes in what you are about to commit, where the hooks
are installed; CI refuses them across the whole tree at the door.

`active/` is the one declared exception, and it is bounded rather than open: two levels
deep at most, files plus one dated subdirectory. Deeper is a notes tree, which is precisely
what this rule exists to refuse — being inside the live drawer is not a licence to grow one.
The other drawers stay out because they are where volume collects: `raw/` alone is 35MB of
engine logs. Tracking those is how a repository ends up with 320 markdown files again.
