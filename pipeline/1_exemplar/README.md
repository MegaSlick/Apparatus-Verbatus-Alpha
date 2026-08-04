# Exemplar

Seals what arrived.

Takes the submitted images and makes them immutable: every page hashed, counted, and accounted for. Nothing downstream may alter what this stage sealed.

The door decides what may enter, by bytes and never by a filename. `door.py` runs
admission; `admission.py` is the one format policy, reading the admission list from
[`config/admitted_formats.toml`](../../config/admitted_formats.toml);
`image_formats.py` and `pdf_render.py` are its structural validators and its bounded
PDF page renderer, both door-private so that rendering happens once, at admission,
and no later stage has an API to re-render with.

Read [HANDOFF.md](HANDOFF.md) for what this stage writes and where. That document
is the interface — no other stage reads this one's code.

See the root [ARCHITECTURE.md](../../ARCHITECTURE.md) for how this fits the flow,
and [GLOSSARY.md](../../GLOSSARY.md) for the vocabulary.
