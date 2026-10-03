# Perlector

Reads the ink and establishes the text.

Reads the ink itself, using the testimonia as clues that sharpen its own reading and never as options to choose between. A reading cut off before the end is held, never delivered as complete.

Each sealed Exemplar page is read whole, in one call (`page_run.py`): the reader is
shown the page image and every witness's page broken into that witness's own units,
under the run-sealed named or blinded witness regime, and answers with the acts it
establishes on the page. Every page has a persisted **feed** (what the reading was
shown), a **page reading** (the answer as given, parsed or held whole) and a **page
accounting**; each entry of a valid answer becomes one act region and one Perlectio.
Each entry's **truncation** is classified by a declared instrument rather than
assumed, and **dissent** against each witness is computed on derived comparison views
after the reading is fixed, never by raw-string voting. `uncertain_spans` and `gaps`
are the two annotation layers over one clean `text`; a gap is zero-width by
construction, so testimony evidence is always linked beside the text and never inside
it.

Read [CONTRACT.md](CONTRACT.md) for what this stage writes and where. That document
is the interface — no other stage reads this one's code.

See the root [ARCHITECTURE.md](../../ARCHITECTURE.md) for how this fits the flow,
and [GLOSSARY.md](../../GLOSSARY.md) for the vocabulary.
