# common

The only project code a stage may import besides its own.

It knows nothing about stages. Stages import it; it never imports back. That is
now checked rather than declared: `common/chairs/test_chairs_import_boundary.py`
reads every `.py` file under `common/` through `ast` and fails on an import of
`pipeline`, wherever in the file it sits. Static, because numbering the stage
directories only makes `import 4_perlector` invalid — a dynamic import would
still cross.

| Module | What a stage gets from it |
|---|---|
| `contracts/` | the envelope, the one canonical serialization, identities, the outcome algebra |
| `runtree/` | the run's evidence: immutable artifacts, atomic publication, run receipts |
| `chairs/` | a named role resolved to one pinned model artifact, verified by digest |
| `stage.py` | argument shape, opening a run, publishing with the envelope filled in |
| `imaging.py` | decoding and cropping, with bounds refused rather than clamped |
| `armarium_formats.py` | the sealed Armarium projection choices — the door binds them into the run, the Armarium reads them back |
| `chandra_custody.py` | one-receipt Chandra custody: the Designator's live structure pass writes it; the read half has no served caller since the capture intake was removed (Tyrel, 2026-09-02) and is the half that defines what the binding admits. It deliberately names the Designator's blob root and serving chair — those constants come from `contracts/`, `runtree/` and `stage.py`, never from a stage module, so the import boundary above holds |

Code enters here **when a second stage needs it** — not in anticipation. Moving
something in is its own pull request, because this is the one place two agents can
genuinely collide.
