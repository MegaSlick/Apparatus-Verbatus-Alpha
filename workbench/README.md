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

## Eight drawers

| Drawer | What goes in it | Lifespan |
|---|---|---|
| `active/` | work currently in play — notes, handoffs, todo lists | until the work finishes |
| `standing/` | the ledgers that outlive sessions — suspensions, alpha shortcuts, the adopted plan | until superseded or the phase ends |
| `design/` | proposals for later — not in play, not abandoned | until tested or rejected |
| `archive/` | finished work, filed under a dated folder | kept, out of the way |
| `raw/` | verbatim engine output — Codex transcripts, reviewer logs | until the work that cites it closes |
| `scratch/` | greps, dumps, fragments, one-off output | **delete without asking, ever** |
| `quarantine/` | material a session believes is dead, staged for Tyrel | **he deletes it; a session never does** |
| `tools/` | scripts a session built that later sessions run | until superseded — one line at the top says what each is for |

`quarantine/` is the one drawer whose direction is one-way. A session moves things in;
only Tyrel takes them out. The asymmetry is the point: a session being wrong about "this
is dead" costs work that cannot be recovered, and a session being slow costs nothing.
`tidy.py` reports anything that has sat over seven days, by name, at session start — a
staging drawer nobody empties is just a slower kind of clutter.

Its rules, in full, because this is the only file in the drawer that reaches another
clone: material is **moved, never copied**, so it exists here or in its old drawer and
not both; each batch is one directory named `<date>_<what-and-why>`, so the name still
says why a week later; and evidence a live finding still cites does not come here at all
— that goes to `archive/`.

**The test between the two:** if you would be annoyed to lose it, it is `archive/`. If
you are only keeping it because deleting felt presumptuous, it is `quarantine/`.

The drawer holds its own `README.md` repeating this, which `.gitignore` deliberately does
**not** track. Tracking it would mean letting git descend into a directory whose whole
purpose is holding material nobody has vetted, and personal material never enters git is
the one rule that does not bend. So this paragraph is the durable copy; that one is a
convenience for whoever opens the drawer.

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
7. `standing/` holds only what must persist across sessions and is read at open and
   close — it is never filed, never aged, and never counted against `active/`'s
   budget. It exists so `active/` can genuinely empty each sitting and its over-budget
   alarm means something. A note that merely *might* matter later is not standing; it
   is `design/` or it is finished.
8. **A plan about to be built from gets a reader on it first.** A note nobody has read
   is a draft; a plan a session is about to execute is a decision wearing a draft's
   clothes, and rule 5 is what makes the difference matter.

## What may be committed instead

A note lives here because it is *not* committable. Committed documentation means a
governed document, a `README.md`, a `HANDOFF.md`, dated evidence under `history/`, or a
declared harness document — `.githooks/doc-allowlist.sh` is the one list, and
`pre-commit` and CI refuse everything else.

So the choice is not "note or document" by feel: if what you are writing is not one of
those five things, it is a note, and it belongs in a drawer above.

## Why most of it is gitignored

So the repository stays the thing you can trust. The canonical documents bind; dated
`history/` is evidence, and the cleanroom holds material still under review — everything
speculative, dated, or half-finished beyond those lives here and dies here. The
`pre-commit` hook refuses stray notes in what you are about to commit, where the hooks
are installed; CI refuses them across the whole tree at the door.

**No drawer is an exception, `active/` included.** It briefly was one, and this file
described that exception for longer than it existed; `.githooks/doc-allowlist.sh` is
the authority and it says plainly that `workbench/` has none. Its own bound survives as
a habit worth keeping — two levels deep at most, files plus one dated subdirectory,
because deeper is a notes tree and that is what these rules exist to refuse.

The drawers stay out because they are where volume collects: `raw/` alone is 35MB of
engine logs. Tracking those is how a repository ends up with 320 markdown files again.
