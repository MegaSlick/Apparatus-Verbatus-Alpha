# common

The only project code a stage may import besides its own.

It knows nothing about stages. Stages import it; it never imports back. That is
now checked rather than declared: `common/seats/test_seats_import_boundary.py`
reads every `.py` file under `common/` through `ast` and fails on an import of
`pipeline`, wherever in the file it sits. Static, because numbering the stage
directories only makes `import 4_perlector` invalid — a dynamic import would
still cross.

| Module | What a stage gets from it |
|---|---|
| `contracts/` | the envelope, the one canonical serialization, identities, the outcome algebra |
| `runtree/` | the run's evidence: immutable artifacts, atomic publication, run receipts |
| `seats/` | a named role resolved to one pinned model artifact, verified by digest |
| `stage.py` | argument shape, opening a run, publishing with the envelope filled in |
| `imaging.py` | decoding and cropping, with bounds refused rather than clamped |

Code enters here **when a second stage needs it** — not in anticipation. Moving
something in is its own pull request, because this is the one place two agents can
genuinely collide.
