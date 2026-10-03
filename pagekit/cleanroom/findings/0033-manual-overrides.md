# Finding: keeping a person's correction when earlier steps are re-run

Reader brief sha256: 5312f3531b6e1a561c1b5d4d572977aaf3a50b2aa5bcefb8917946d977930c1d

## Situation

Automatic decisions are sometimes wrong and a person corrects them: a split line moved, an angle typed in, a content box redrawn. When an earlier step changes later (for example the split is redone), automatic results downstream must be recomputed, but a correction should survive if it still makes sense. The document records that its reference tool stores each step's result with a fingerprint of the inputs that produced it and a mode (automatic or manual); automatic results are recomputed when the fingerprint changes, manual ones are kept and re-attached. It also records that this is hard to drive from a pipeline: invalidation is implicit, and a value copied to other pages can be silently recomputed if its mode or fingerprint does not match.

## Pagekit's observed behaviour

Not built yet. Pagekit takes crops as declared input, never changes them, and keeps no per-step state. Its report records the input file's SHA-256, size and crops, so a re-run on the same input gives byte-identical output.

## General technique

Store each step's parameters with their origin (detected, manual, or locked), a confidence, and a content hash of the inputs they were computed from. When inputs change, recompute detected values, keep manual values and flag them for a check if their inputs moved, and report exactly which pages and steps became stale and why, before running anything. Corrections are ordinary data in the project file, applied by command, so a person or an agent can make them without an interactive tool.
Source: general knowledge

## Settings in general terms

How far an input may move before a manual value is flagged depends on the step; for geometry it depends on the pixel tolerance at the working resolution. Hash inputs should include everything the step reads, including configuration, so runs are reproducible.
