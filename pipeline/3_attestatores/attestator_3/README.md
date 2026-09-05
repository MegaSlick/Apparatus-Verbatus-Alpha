# Attestator 3

A numbered chair, not a model. The model and pinned revision for this chair are
declared in `config/models.toml`.

The chair path stays stable when its assigned model changes. The configuration, not
this directory, encodes the assignment.

## What the current occupant answers, and why it is asked twice over

Its adapter is `churro.v1` (`pipeline/3_attestatores/churro.py`), page-scoped: one
call per page, one page Testimonium per (page, chair).

The occupant's model card documents no output format at all — no layout, no
bounding boxes, no reading order, no schema, no example body — and the trained
prompt this repository carries verbatim asks for `<output>extracted text
here</output>` and for reading order and layout *of the text*, never for
coordinates. So this chair reported no geometry, its only observation was a
`bounds_source="presented"` echo that routing and coverage exclude, and it never
attached to an act on a live path.

Unit 12 changed the question rather than the arithmetic. The served chair is
asked, in its own two-message framing, for the closed shape
`common/churro_response.py` declares (`feeding.churro_layout_prompt`), and **two
answers are legal**:

* the wire contract — block text with normalized `box_1000` geometry, converted
  to sealed-page pixels by the conversion the Designator and Chandra already
  share. The chair then attaches to an act by **its own** boxes overlapping that
  act's own sealed proposal, basis `geometric-overlap`. Nothing selects among
  witnesses: two chairs may independently overlap one rectangle and both say so;
* the trained `<output>` envelope, unchanged. A model that ignores the new
  clause still reads, retains and aligns, and lands exactly where this chair
  landed before — read, unattached, and the record says so.

Anything else is refused **by name**, with its bytes already retained: a JSON
body in an undeclared shape reaches the capture as `unrecognized-shape` naming
the shape, and a body that is neither JSON nor a parseable envelope as `failed`
naming the refusal. The contract is this repository's question, not a vendor
measurement, so the first pod reading either validates it or arrives as a named
surprise (GOVERNANCE 10).

Two things that are not this chair's own:

* **its comparability.** Counting toward the witness floor needs `attached` and
  an aligned status, and alignment is computed against the anchor derived from
  the Chandra chair's response. The geometry here is this chair's; the locating
  of its text is not.
* **its prompt bytes.** Clauses 1–5 and the closing reading-order paragraph are
  Apache-2.0 carried bytes from the Churro release; only the two output-format
  instructions are this repository's wording, quoted beside their replacements
  in `feeding.churro_layout_prompt`.

`pipeline/3_attestatores/HANDOFF.md` carries the contract; this file only says
what sits in the chair.
