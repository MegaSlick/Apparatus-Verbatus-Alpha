# contracts

The one executable authority for `skeleton.v1`. Every stage's `CONTRACT.md`
describes what that stage owns and links here; none of them carries a second copy
of the schema, because two copies of a contract is one contract and one thing that
goes stale.

`skeleton.v1` is the schema label every artifact, run authority and record in a run
tree carries (`canonical.SCHEMA_LABEL`). A reader refuses any other label rather than
reinterpret old evidence under a changed contract, so a change that an existing reader
would misread needs a new label.

| File | What it settles |
|---|---|
| `canonical.py` | one serialization, so a digest means the same thing on every machine |
| `identities.py` | the derived identities, each a digest of its bindings and therefore verifiable |
| `outcomes.py` | the outcome algebra — three classes, nine vocabularies, one total transition table |
| `envelope.py` | what every artifact wears, and what a consumer refuses at a handoff |
| `approval.py` | the one shape an approval is recorded in |
| `stages.py` | the stage names and the eight handoffs |
| `errors.py` | the refusals, kept separate so a stage can catch what it means to catch |
| `uncertainty.py` | the canonical uncertainty layer: uncertain spans, gaps and self-revisions anchored to one text |
| `annotations.py` | the Archetypus annotation layer, shared by the stages that seal and check it |
| `prior_draft.py` | the recorded relationship between a prior draft and its establishing reading |
| `serving.py` | the closed shapes and vocabularies of the live reading seam, shared by stages and `operations/` |

## Three things worth knowing before you change anything here

**Identity is derived, not assigned.** An `act_id` hashes the *original* proposal,
so a recrop cannot change it; a `region_id` hashes the act *and* the transform, so
a recrop must. "Act identity survives recropping" is therefore the only thing the
derivation is able to do, rather than something code has to remember.

**Witness outcomes terminate nothing.** Chair results aggregate into a coverage
record and never into a manifest category or a character of text. An act whose
every chair is `failed` still reaches the Perlector, which reads the ink. If you
ever find yourself giving a witness outcome a terminal category, you are building a
picker under an accounting name.

**An outcome with no class is fatal, not a warning.** Every unit is in exactly one
of completed, unresolved or failed; a unit in none of them is an accounting imbalance
that stops the run, never one it routes around. `check_algebra_is_total()` proves
both mappings total rather than trusting them, so a state added without a class or a
terminal decision fails at the first run.
