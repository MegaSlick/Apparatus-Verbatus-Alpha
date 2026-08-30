# contracts

The one executable authority for `skeleton.v1`. Every stage's `HANDOFF.md`
describes what that stage owns and links here; none of them carries a second copy
of the schema, because two copies of a contract is one contract and one thing that
goes stale.

`skeleton.v1` is disposable on purpose. It exists to prove wiring and bookkeeping
before any model, GPU, or real page exists, and it proves nothing about reading
ink. `DATA_CONTRACT.md` is written later, from what specs 01–03 actually taught us.

| File | What it settles |
|---|---|
| `canonical.py` | one serialization, so a digest means the same thing on every machine |
| `identities.py` | the six identities, derived from their bindings and therefore verifiable |
| `outcomes.py` | the outcome algebra — three classes, nine vocabularies, one total transition table |
| `envelope.py` | what every artifact wears, and what a consumer refuses at a handoff |
| `approval.py` | the one shape a Tyrel-approval is recorded in |
| `stages.py` | the stage names and the eight handoffs |
| `errors.py` | the refusals, kept separate so a stage can catch what it means to catch |

## Three things worth knowing before you change anything here

**Identity is derived, not assigned.** An `act_id` hashes the *original* proposal,
so a recrop cannot change it; a `region_id` hashes the act *and* the transform, so
a recrop must. "Act identity survives recropping" is therefore the only thing the
derivation is able to do, rather than something code has to remember.

**Witness outcomes terminate nothing.** Chair results aggregate into a coverage
record and never into a manifest category or a character of text. An act whose
every chair is `failed` still reaches the Perlector, which reads the ink. If you
ever find yourself giving a witness outcome a terminal category, you are building a
picker under an accounting name — GOVERNANCE 3, and CLAUDE.md's eighth hard rule.

**An outcome with no class is fatal, not a warning.** Harvest invariant #10.
`check_algebra_is_total()` proves both mappings total rather than trusting them, so
a state added without a class or a terminal decision fails at the first run.
