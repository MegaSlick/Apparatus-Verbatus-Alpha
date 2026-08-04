# Exemplar

Seals what arrived.

Takes the submitted images and makes them immutable: every page hashed, counted, and accounted for. Nothing downstream may alter what this stage sealed.

The door decides what may enter, by bytes and never by a filename — and, per Tyrel's
ruling 2026-08-04, nothing is rejected: a refusal is always either damaged bytes or a
format this project has no reader for yet, never a decision about the file. `door.py`
runs admission; `admission.py` is the one format policy, reading the admission list
from [`config/admitted_formats.toml`](../../config/admitted_formats.toml);
`image_formats.py` is its structural validator for single-image rasters, and
`pdf_render.py`/`tiff_render.py` are its bounded page renderers for the two formats
that can hold more than one page — a PDF, rasterised whole per page through
`pypdfium2`, or a TIFF the door finds holding more than one image directory. All
three are door-private, so rendering happens once, at admission, and no later stage
has an API to re-render with.

Read [HANDOFF.md](HANDOFF.md) for what this stage writes and where. That document
is the interface — no other stage reads this one's code.

See the root [ARCHITECTURE.md](../../ARCHITECTURE.md) for how this fits the flow,
and [GLOSSARY.md](../../GLOSSARY.md) for the vocabulary.
