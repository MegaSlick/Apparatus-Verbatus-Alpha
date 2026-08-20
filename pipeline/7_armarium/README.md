# Armarium

Writes the pipeline's sealed product bundle. It projects established Archetypus
readings, their provenance, and their links to exact regions of ink; it does not
establish, repair, choose, or rewrite text. The pipeline ends here.

Its clean verifier replays the non-text accounting basis and source graph carried
in the bundle. A self-hashed bundle therefore cannot substitute a partial status,
drop one continuation citation, replace provenance, or rewrite a review reason
while leaving the other selected projections untouched.

The bundle's first member is `EXPORT_MANIFEST.json`. Its companion formats are
the run-sealed choices in `config/formats.toml`; the default includes a readable
text bundle, SQLite/FTS database, JSONL hand-off, review items, and the separate
salvage tier. Every delivered act carries the Archetypus's own established-text
status and transcription annotation layer, so an act the pipeline knows is
damaged is visibly partial in the products and in the run's own verdict. The
separate *semantic* annotation layer remains only a boundary contract pending
Tyrel's ARCHITECTURE approval.

`run.py` seals the bundle into the run tree; `bundle.py` publishes it to a
destination outside, verifying it again on the way out. Nothing else takes a
product out of this stage.

Read [HANDOFF.md](HANDOFF.md) for what this stage writes and where. That document
is the interface — no other stage reads this one's code.

See the root [ARCHITECTURE.md](../../ARCHITECTURE.md) for how this fits the flow,
and [GLOSSARY.md](../../GLOSSARY.md) for the vocabulary.
