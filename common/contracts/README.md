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
| `serving.py` | the closed shapes and vocabularies of the live reading seam, shared by stages and `operations/` |

## Three things worth knowing before you change anything here

**Identity is derived, not assigned.** A reading act's `act_id` hashes its page,
its class and its binding -- the page-reading attempt, the entry's number `n` and
its union box (`identities.act_bindings`) -- so two entries that share a box still
get two identities, and a new attempt mints new ones. A `region_id` hashes the act
*and* the transform, so the same act seen through a different transform is a
different region and the same act.

**Witness outcomes terminate nothing.** Chair results aggregate into a coverage
record and never into a manifest category or a character of text. A page whose
every chair is `failed` still reaches the Perlector, which reads the ink. If you
ever find yourself giving a witness outcome a terminal category, you are building a
picker under an accounting name.

**An outcome with no class is fatal, not a warning.** Every unit is in exactly one
of completed, unresolved or failed; a unit in none of them is an accounting imbalance
that stops the run, never one it routes around. `check_algebra_is_total()` proves
both mappings total rather than trusting them, so a state added without a class or a
terminal decision fails at the first run.
