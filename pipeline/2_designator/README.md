# Designator

Marks out the acts.

Finds the acts on each page and marks their bounds. It may use textual cues as well as visual ones — act boundaries are often signalled by a marginal name or a formulaic opening rather than by whitespace. It never establishes the authoritative transcription.

It also accounts for what it did *not* claim: every page's own ink is rescanned independently of what marking-out found, and any ink no crop covers becomes a held act rather than an absence. A page the structure pass cannot mark out is held with the reason named, never skipped.

Read [HANDOFF.md](HANDOFF.md) for what this stage writes and where. That document
is the interface — no other stage reads this one's code.

See the root [ARCHITECTURE.md](../../ARCHITECTURE.md) for how this fits the flow,
and [GLOSSARY.md](../../GLOSSARY.md) for the vocabulary.
