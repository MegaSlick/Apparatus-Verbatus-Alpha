# Attestatores — handoff

The Attestatores records one append-only `kind="testimonium"` outcome for every
configured witness and every act in the Designator's proposal seal. It writes
ordinary `skeleton.v1` artifacts under `3_attestatores/artifacts/`; the derived
manifest is only inventory. A missing record is never a witness outcome.

## Input boundary

The stage reads the self-hashed proposal denominator and its exact Designator
evidence. For a proposed act it accepts only `origin="proposal"` regions,
validates each crop against the exact Exemplar page and transform, and gives all
configured witnesses that same original proposal set. A recovery crop is not
silently substituted for what a witness saw. For a Designator-held act, every
witness receives an explicit `not-run` outcome instead of a partial reading.

The current writer is the deterministic synthetic skeleton. It is a contract
exercise, not a live witness/model integration; a real run does not reach it
because System 03 stops before real structural proposal work.

## `kind="testimonium"`

Each record is subject-bound to the act and attempt-bound to
`read:<chair>:<ordinal>`. The current fixture writer emits `read`,
`genuinely-empty`, `failed`, or `not-run`; all remain visible as history. Its
payload includes:

```text
chair, act_key, attempt_ordinal
regions = [{region_id, image_path, image_sha256}, ...]
provenance, format_capabilities, content_health
reported                for read and genuinely-empty outcomes
reason                  for failed/not-run outcomes
```

For `read`, `genuinely-empty`, and `failed`, the direct input set is precisely
the pixel blobs of the proposal regions attempted, and provenance includes the
resolved chair identity, revision, adapter recipe, and a serving receipt. A
genuinely-empty report is a completed read of the pixels (`reported=""` and
`content_health.empty=true`), not an absent attempt; it counts as witness
coverage. It is still one fallible Testimonium, not evidence that the act or page
is blank, and it never authorizes `confirmed-blank`; that diagnosis belongs to
the unbuilt Recensor. `not-run` carries no invented receipt or region input.

## Consumer obligations

Perlector reads every Testimonium for an act, verifies its direct inputs and
serving provenance, and requires its `regions` payload to be exactly the current
original-proposal region set (not a recovery crop added after the witness ran).
It then derives the current record for each chair by unique attempt ordinal while
leaving every historical attempt in the tree, and retains a digest-checked
reference to every current record it consulted in the Perlectio basis. It may
record disagreement structurally, but it does not choose
a witness, count agreement to determine text, or promote a missing/failed result
to coverage. Recensor uses the same current-per-chair derivation and refuses
duplicate ordinals rather than choosing an arbitrary record.
