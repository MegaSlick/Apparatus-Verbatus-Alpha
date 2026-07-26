# Exemplar

Seals what arrived.

Takes the submitted images and makes them immutable: every page hashed, counted, and accounted for. Nothing downstream may alter what this stage sealed.

Read [HANDOFF.md](HANDOFF.md) for what this stage writes and where. That document
is the interface — no other stage reads this one's code.

See the root [ARCHITECTURE.md](../../ARCHITECTURE.md) for how this fits the flow,
and [GLOSSARY.md](../../GLOSSARY.md) for the vocabulary.
