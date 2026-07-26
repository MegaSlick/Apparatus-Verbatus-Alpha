# workbench

**Local only. Everything here except this file is gitignored and never reaches the
repository.**

This is where AI notes, handoffs, session summaries, task lists, grep dumps, design
scribbles and half-finished thinking live. The old repository had no such place, so
all of that went into `docs/` — 320 markdown files, 32 of them written in a single
two-month stretch, until nobody could tell which document was true.

## Three drawers

| Drawer | What goes in it | Lifespan |
|---|---|---|
| `active/` | work currently in play — notes, handoffs, todo lists | until the work finishes |
| `archive/` | finished work, filed under a dated folder | kept, out of the way |
| `scratch/` | greps, dumps, fragments, one-off output | **delete without asking, ever** |

## The rules

1. When a piece of work finishes, its notes move to `archive/<date>_<topic>/`.
   Not later. Then.
2. `scratch/` is disposable by definition. Anything in it may be deleted by anyone
   at any time without checking. If it matters, it was in the wrong drawer.
3. `active/` should be small enough to read in one sitting. If it isn't, something
   finished and nobody filed it.
4. **Nothing here is instructions.** The six documents at the repository root are the
   only instructions. A note in `active/` describing how something should work is a
   draft, not a decision.

## Why it is gitignored

So the repository stays the thing you can trust. Everything committed is current and
binding; everything speculative, dated, or half-finished lives here and dies here.
The `pre-commit` hook enforces the boundary — it refuses stray markdown files
anywhere in the repository, so notes cannot leak in even by accident.
