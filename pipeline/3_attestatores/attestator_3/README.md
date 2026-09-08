# Attestator 3

A numbered chair, not a model. The model and pinned revision for this chair are
declared in `config/models.toml`.

The chair path stays stable when its assigned model changes. The configuration, not
this directory, encodes the assignment.

## What the current occupant answers, and in whose words it is asked

Its adapter is `churro.v1` (`pipeline/3_attestatores/churro.py`), page-scoped: one
call per page, one page Testimonium per (page, chair).

The chair runs the vendor's own system, under the ruling of 2026-09-06: the
vendor's preprocessing, prompt bytes, message shape, generation values and
output grammar are adopted verbatim and pinned by digest, and the vendor's
harness is not.

* **Asked** with one of two vendor-attested system strings and an image-only
  user turn — the registry's answer for `stanford-oval/churro-3B` at tag
  `v0.3.0` by default, the paper-era benchmark harness's own `SYSTEM_MESSAGE`
  as its arm. Both profiles set the user prompt to `None`, so the user turn
  carries the image alone. The bytes and their digests are in
  `common/churro_document.py`; which one a run sends is
  `config/models-real.toml`'s `[witness_framings]`, and the resolved name is
  written onto every Testimonium the chair produces.
* **Shown** the sealed page prepared the way `prepare_ocr_image` prepares it:
  fit inside the vendor's 2,500-pixel square with LANCZOS, then converted to
  RGB. Both steps are recorded on the presentation as
  `churro-prepare-ocr-image.v1` with its `colour_mode`, so the exact image the
  chair saw re-derives from the Exemplar.
* **Read** through the vendor's `HistoricalDocument` grammar. Three answers
  parse: the grammar itself, the plain reading-order text the paper-era harness
  expected, and a bare `<output>` envelope, kept so retained history still reads
  and carrying a finding that says a shape nobody asked for arrived. A
  well-formed XML body rooted at anything else reaches the capture as
  `unrecognized-shape` naming which root it was; a body that offers the grammar
  and will not parse is `failed`, its bytes retained under their digest.

**This chair reports no geometry, and that is the vendor's design rather than a
gap.** `HistoricalDocument` has `Page`, `Header`, `Body`, `Footer` and `Line`,
and no coordinate anywhere in the guide or the XSD; Churro-DS, the fine-tuning
target, is one continuous text string per page in reading order. So the only
observation is a `bounds_source="presented"` echo, which routing and coverage
exclude, and the adapter declares no float-to-pixel rule at all.

Unit 12 asked this chair for block rectangles instead, in a modified carry of a
prompt the model was never trained on, so that a geometry-blind page witness
would have boxes to attach acts by. That question, its wire contract
(`common/churro_response.py`) and its coordinate channel are retired: the
attestation is the vendor's again, and attachment is solved where it belongs —
the Perlector admits the existing `anchor-line` basis for a page witness whose
alignment for an act is `aligned` with a located span.

Two things that are not this chair's own:

* **its comparability.** Counting toward the witness floor needs `attached` and
  an aligned status, and alignment is computed against the anchor derived from
  the Chandra chair's response. The locating of this chair's text is not this
  chair's doing.
* **its prompt bytes.** Both carried strings are Apache-2.0 from
  `github.com/stanford-oval/Churro`, cited at the exact commit each was taken
  from, digested where they are carried, and re-checked against the vendor by
  `common/test_vendor_parity.py`. Not one word of either is this repository's.

`pipeline/3_attestatores/HANDOFF.md` carries the contract; this file only says
what sits in the chair.
