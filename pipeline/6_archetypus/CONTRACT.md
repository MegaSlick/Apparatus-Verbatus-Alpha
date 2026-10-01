# Archetypus — contract

The Archetypus is the first and only current stage that calls one machine reading
established. It is not a correction, a witness consensus, or a truth claim. It writes
a once-only `kind="archetypus"` record under `6_archetypus/artifacts/` for each counted
reading whose current Recensor review is exactly `accepted`. A held reading deliberately
has no Archetypus record; that absence is part of the terminal accounting, not a gap to
fill.

**Exit codes.** `EXIT_COMPLETE` when every accepted reading has a record and the index
reconciles. `EXIT_HELD` when any reading's current review is an outcome the shared
outcome algebra leaves open (no terminal category): the readings already established
are real but the stage's work is not finished, and the held act keys are named on
stderr. A refusal anywhere in establishment or index reconciliation is fatal, not a
held reading.

## Stage-completion seal

Before this producer's final manifest it publishes one `decode-environment` and
one `stage-seal`, or reuses both on a byte-identical retry. The seal witnesses
this pass's disk inventory and blob contents, and binds the exact decode-environment
bytes, run `config_digest` and `register_digest`, and `(kind, outcome)` census. An exit
held after publishing stage evidence seals it (holds remain in its census); a
pass that never reaches its seal does not seal, whether it was held or refused
before publishing stage evidence or closed fatally after publishing it, so the
successor correctly refuses the missing boundary.

Seals are compared as the SET the stored inventory names, on both sides of the
boundary: the producer refuses to re-seal, and the successor refuses to read,
when any named seal is no longer on disk. Ordinals are the contiguous run 1..N,
so removing the latest leaves a prefix that still looks whole — and the earlier
statement would then answer for a boundary it never witnessed.

## Input boundary

The denominator is `common.stage.reading_acts`, and the reviews come through
`common/page_review.py`, the one reader of the Recensor's reviews
(`current_page_reviews`: one current `review` per row, every row reviewed, no review
of an uncounted unit, each naming its row's key, kind and reading). A `page-refused`
row is the census's and is never reviewed (`reviewed_rows`).

- **Which rows are established.** A row whose current review is `accepted`, and
  that `page_review.require_establishable` allows: a row with a reading whose
  disposition is `read`, or a held row whose only hold is
  `no-act-on-page-unconfirmed` and whose review names exactly that code in its
  `release` (the Recensor confirmed its page holds no act). An accepted row with no
  reading (`page-unread`, `page-blank`) or any other hold is fatal, because a stage
  may not resurrect a held reading. Every record is built and checked before any is
  published, so a refusal leaves no partial set behind. Held and page rows end at
  the Recensor with no record; only an outcome the algebra leaves open makes the
  stage exit `EXIT_HELD`.
- **`other` readings are established too.** The Perlector names `act` and `other`
  entries on a page; both are established here, so every export shows the one
  established reading of each. The record carries `kind` (`act` | `other`).
- **The constructor** is `establish_from_accepted_page_reading(context, row,
  review_ref, page_testimonia)`, the only public constructor; `page_testimonia` is
  the row's page's entry in `common.page_testimonia.current_page_testimonia`,
  computed once per pass. Its caller supplies a row and the sealed Recensor-review
  reference, never free-standing text or a reading payload. The review is resolved
  through `RunTree.read_artifact_reference`, which refuses a reference whose actual
  bytes name a different stage or kind; that check is what makes a Testimonium or
  any other non-Perlectio artifact unable to reach this stage by being named. The
  reading is the row's `perlectio.v2`, which the review must name and input; it must
  be `read` with no `holds` or `page_holds`, of the row's kind, and not salvage-tier
  (`tier`, `source_tier` or `reading_tier` of `salvage` is refused). Its one region
  is the `act-region` the reading names (and the row counted), proven from the
  Exemplar by `verify_reading_region_lineage`; the record's region is exactly what
  that returns (`region_id`, `image_path`, `image_sha256`, `verified_dimensions`,
  `source_page_ordinal`, `source_page_id`, `transform`), a closed set, and its
  declared `image_sha256` is checked against the crop's bytes.
- **Witness custody** is `page_testimonia.shown_page_witnesses`: the feed the reading
  inputs was built under this run's witness regime; it showed at least one
  witness, or the reading is a Lectio nuda and is refused by name; every row names
  a current `page-testimonium` of the page by reference (a blinded feed names no
  chair), under the label this run's regime gives that Testimonium's chair; and the
  reading's dissent compares against exactly the letters the feed showed, once
  each. A page reading records no annotation layer, so a record's `annotations` is
  `[]` and a reading carrying one is refused.
- **Uncertainty** is `common.contracts.uncertainty.from_page_perlectio`: the
  reading's spans and gaps (which must equal the copies in its
  `uncertainty_assessment`) with its `{state, problem}` assessment, under its own
  lectio kind `page-read`, whose `self_revisions` is `null` (not measured).
- **Serving provenance** must validate (`validate_serving_provenance`, receipt
  required).
- **Inputs** are the review, the reading, its act-region and the crop.

### Real ingress

The stage opens through `common.stage.open_stage_context`, which decides the route from
one read of the run authority and, on a real submission, carries the registry and the
sealed digest map into the context. This stage reads no fixture declaration of its own on
either route; its denominator is `reading_acts`. The context's fixture slot is `None`
behind a refusing accessor and is never touched here. Nothing else changes with the
route -- the constructor path, the closed record schema and the index reconciliation are
the same on both.

## `kind="archetypus"`

The artifact subject is the stable act identity. Its payload is separately self-hashed,
its field set is **closed and checked** (`validate_record_fields`), and it contains:

```text
act_id, act_key, page_id, kind
text, text_hash
status = "established", text_status
regions, provenance, annotations, uncertainty, evidence_ref
dissent_ref, perlectio_ref, recensor_ref, self_hash
```

The closed field set is the mechanical half of "exactly one `text` field": the old
pipeline's export reached through `consolidated_literal`, `reader_text`, `literal`,
`text`, `markdown` for whichever was non-empty, and a closed set is what stops that being
rebuilt one field at a time. A record missing a field, or carrying one it is not defined
to carry, is refused before it is written.

**`status` vs `text_status`.** `status` is a fixed literal, `"established"`, required
verbatim by the Armarium's own
`verify_established_page_record` — it means "this act has exactly one Archetypus record,"
record-level. `text_status` is the richer, separate claim spec 10 asks for:
`established | partial | no_readable_text`, describing what the record's `text` actually
contains. The two are deliberately different fields answering different questions. They
are **not** mirrors: mirroring them would make every damaged act fail the Armarium's
literal check, and would put a second status decision where there is meant to be one.

**`text_status` derivation.** Computed from the reading's own text and *both* damage
layers it carries, never stored upstream:

- any canonical `uncertainty.gaps` row, or any `illegible` annotation, present →
  `partial`, regardless of whether `text` is otherwise empty or full (ink is known and
  unread somewhere in the act);
- otherwise, empty or all-whitespace `text` → `no_readable_text`;
- otherwise → `established`.

The two layers are unioned rather than ranked, so neither can hide damage the other
saw — which is what makes carrying both of them honest rather than redundant. The gap
test precedes the empty-text test for both layers: a whole-act gap over empty text is
"ink present, wholly unread", the middle silence, and reporting it as the last one
would seal a proved blank beside a gap saying the opposite.

**The derivation lives in `common/contracts/outcomes.py`** (`TEXT_STATUSES`,
`derive_text_status`, `derive_record_text_status`), not in this file, because the
Armarium recomputes the same word from the layers travelling beside the text at
export and stages talk only through `common/`
(`pipeline/test_stage_import_boundaries.py`). One spelling; the two cannot drift into
disagreeing about the same record. This module re-exports all three under its own
names, which is how this stage's tests reach them.

An empty-text `established` record is refused at the schema (`validate_text_status`).
`no_readable_text` requires `evidence_ref` — see below. A blank page is **not** a fatal
error -- a run may legitimately contain blank pages; what is
refused is the opposite collapse, ink that is merely unread reported as ink that was
never there.

**`evidence_ref` — an intentionally unfilled upstream contract.** An accepted review is
evidence that the Recensor accepted a reading; it is not evidence that the page was
blank. The constructor therefore accepts `no_readable_text` only when the review carries
a digest-checked `no_readable_text_evidence_ref` as one of its own direct inputs. The
reverse is fatal rather than ignored: a review that retains a blank proof over a reading
that establishes text is two upstream claims contradicting each other, and reading past
the one this stage does not need is how the contradiction would leave no trace. The
current Recensor publishes no such proof, and its `confirmed-blank` outcome is terminal
at that stage, bypassing this record. Consequently the current end-to-end pipeline cannot
yet publish a `no_readable_text` Archetypus. This is a named cross-stage gap, preferable
to manufacturing a blank finding from empty text; the shared outcome algebra likewise
keeps Perlector silence unresolved until a blank-proof contract exists.

`evidence_ref` is checked for shape and for membership in the review's own inputs, but —
unlike `perlectio_ref` and `recensor_ref` — it is never read, stage-checked, or
kind-checked, because no `blank-proof` artifact kind exists yet to check it against. The
one class checkable without that missing contract is refused: nothing from the reading's
own evidentiary chain may stand as evidence of its own silence — neither the reading
itself (`no_readable_text_evidence_ref == perlectio_ref`) nor any direct input of that
reading, including the crops it read. Both are a `SchemaRefusal`. Resolving the reference
through
`read_artifact_reference` against a real `kind="blank-proof"` — the way `perlectio_ref`
and `recensor_ref` are resolved — is still owed once the Recensor lane defines that kind.

**`annotations` — carried whole, never in `text`.** A page reading records no
annotation layer, so an established page record's `annotations` is `[]`; the shape
below is what `validate_annotations` holds any layer to. A list of:

- `uncertain` — `{kind, start, end, certainty, alternatives}`. A span covering at least
  one *readable* character in `text` — width alone is not enough, because a span over
  blank text would sit inside a `no_readable_text` record asserting the reader did read
  characters there. Where nothing was read, the honest shape is an `illegible` gap. It
  carries a closed certainty (`high | medium | low | unknown`)
  and the reader's own candidate readings for that span. The alternatives are the
  Perlector's, not a witness's: the Perlector reads the ink, so its uncertainty about a
  span it did read is its own. There is no witness-reference field on this shape at all.
  Be aware of what is *not* verified: alternatives are the one free-text field in the
  sealed record — non-empty, distinct strings and nothing more. Unlike a gap's quoted
  variant (checked verbatim against what its cited witness reported), nothing ties an
  alternative to the ink or to anyone; the claim that they are the reader's own rests on
  the producer, not on a check. `text` is unaffected either way.
- `illegible` — `{kind, start, end, witness_evidence}`. A **zero-width anchor**
  (`start == end`, structurally, so a gap can never carry a character), representable at
  leading, internal, trailing and whole-act positions. `witness_evidence` may be empty —
  every witness may have found the same damage — and each entry is exactly
  `{witness_ref, variant}`: the witness must be one of this reading's own basis
  testimonia, and **the quoted variant must be a substring of what that witness actually
  reported**. A variant that is neither the ink nor something a witness said is a
  reconstruction, and the record carries none -- the reader must never invent
  wording it did not see. The comparison is exact; normalizing here would be a place for the
  record to differ from the testimony it quotes.

The shapes map onto the mature convention rather than inventing markup: `<unclear
cert="">` and `<gap>` (TEI P5, "Representation of Primary Sources"; EpiDoc Guidelines).
Rendering either of them — brackets, underdots, sigla — is the Armarium's business at
export time and is deliberately not stored here. A page reading carrying an annotation
layer is refused, so every page record's `annotations` is `[]`; the rules above and
below are what `validate_annotations` holds a layer to.

**Beside, not instead of, the canonical `uncertainty` layer.** The two describe the same
kinds of damage — `uncertain` against `uncertain_spans`, `illegible` against `gaps` — and
each carries a fact the other's schema cannot hold: a `certainty` of `unknown` has no
canonical equivalent, and a canonical gap's `position`, `chair` and `testimonium_id` have
no place on an `illegible` note. Folding one into the other would therefore lose evidence,
and evidence is never discarded, so both are sealed and both travel. They cannot
contradict each other into silence because `text_status` is the union of the two: either
one recording unread ink makes the record `partial`.

**A malformed annotation is run-fatal, with no per-act route around it — a producer
obligation, not (only) a reader problem.** `validate_annotations` raises `SchemaRefusal`
out of `main` for the whole run on the first malformed note it meets: a closed kind, exact
integer offsets within `[0, len(text)]`, a closed `certainty` enum, non-empty
`alternatives`, and — for `witness_evidence` — an **exact, unnormalized substring match**
against what the cited witness reported. That comparison is correct for what this stage
stores (normalizing here is where a record starts to differ from the testimony it quotes),
but if a Perlector ever emits annotations from a language model, several of
these become reachable on ordinary model variance rather than only on forged input: a
witness quotation returned in a different Unicode normalization form (NFC vs. NFD) or with
a stripped trailing space is byte-different and refused; offsets are model-computed, so a
model counting grapheme clusters or UTF-16 units instead of Python code points is off by
one on every accented character; `certainty` values like `"very low"` or `0.7` are refused
outright. **The obligation this places on the Perlector lane: validate or repair a
reading's offsets and quotations against these same rules before sealing a Perlectio**,
so a malformed model output is refused (or normalized) at its own stage rather than taking
the whole Archetypus run down for every other act. Whether the comparison itself should
Unicode-normalize before checking substring containment (while continuing to *store* exact
bytes either way) is a product decision, not made here.

`text` and provenance are exact copies of the one reviewed Perlectio, and `regions` is
exactly what `verify_reading_region_lineage` proves of the act-region it names;
`dissent_ref` names that Perlectio artifact rather than making a second mutable dissent
copy. **`dissent_ref` and `perlectio_ref` are the same value by design, not by
accident**: `perlectio_ref` is the parent evidence this record establishes from,
`dissent_ref` is where a reader finds this act's dissent (by reference,
never copied); the dissent lives inside the Perlectio itself, so the two pointers
coincide. The Armarium's own verification requires them equal, so carrying only
one under two names is not available without breaking that consumer. `perlectio_ref` and
`recensor_ref` are typed, digest-checked references and both are direct inputs, together
with the exact crop blobs named by the reading. `text_hash` is the canonical digest of
`text` alone (`digest_of(text)`), so any export format can prove it carries the same
clean text without re-hashing the whole record. One trap for a second implementer:
`digest_of` hashes the *canonical JSON encoding* of the string — `sha256('"Maria"')`,
quotes included — not the raw UTF-8 bytes. Computing "the sha256 of the text" the
obvious way produces a mismatch against every record.

There is no alternate text, no witness text field, and no branch that chooses among
readings. `establish_from_accepted_page_reading` is the only public constructor: its
caller supplies a row and the sealed Recensor-review reference, never free-standing
text or a reading payload. A later run cannot write a second different record under the
same once-only identity.

## `6_archetypus/index.json`

A rebuildable per-run summary, derived from the immutable per-act records on disk exactly
as `manifest.json` is — never the only evidence. Written through `RunTree.write_index`,
so it is rewritable (unlike an artifact) and safe to delete and rebuild identically.

Each row is `{act_id, act_key, kind, artifact_id, text_status, text_hash,
relative_path, sha256}`, and the index itself is the closed set `{schema, run_id, stage, record_count,
rows, self_hash}` — `record_count` counts the records the index summarizes; the
reconciliation below, not the field, is what ties that number to the Recensor's accepted
set. The stage rebuilds it, reads it back, and reconciles it before finishing:
rows, records on disk, and **the readings the Recensor accepted** must be the same set, and a
missing or duplicate row is FATAL rather than a warning. The reconciliation target is
deliberately the Recensor's accepted set, recomputed from the immutable review records —
an index checked only against the writer's own list would agree with itself about an act
the writer had skipped. `validate_index` proves the same thing for any consumer that
wants it before relying on the file — with one practical caveat: its first argument is a
stage-context-shaped object carrying `.tree`, `.input_ref`, `.artifact_ref` and what
`common.stage.reading_acts` reads, so a consumer outside a stage opens one first,
exactly as `test_index.py` does with `page_context`.

## Consumer obligations

Armarium requires exactly one Archetypus record for an accepted reading, rather than
selecting one. Before export `verify_established_page_record` verifies the nested
self-hash, the record's binding to its row, its accepted review and its reading, exact
equality of `text`, `provenance`, `status` and `dissent_ref` with that reading, its one
region re-proven from the Exemplar, and its direct inputs (review, reading, act-region,
crop). It then links each region back to the original Exemplar filename ledger.

**It also reads `text_status` and `annotations`, and does not take either on trust.**
It validates the reading's annotation layer through the shared `validate_annotations`,
recomputes the uncertainty layer from the reading, and *recomputes* `text_status` from
both using the shared `derive_record_text_status`. A record claiming `established`
over its reading's own gap is fatal at export. Both fields travel into the manifest
entry, projection, and every selected literal format. `text_status` also enters the
package's text-free source graph and run aggregate, where a non-`established` status
adds its named reason and makes the run `partial`. A run whose acts are all delivered
but damaged therefore exits `EXIT_HELD` at the Armarium rather than 0 — the act is
delivered, and the run did not read all of it.

Consequences worth stating plainly:

- the older `annotations` layer is **carried, not migrated**. It is projected under the
  name `transcription_annotations`, to keep it apart from the unbuilt *semantic*
  annotation layer and its per-row `not-produced` claim. A page reading records no
  annotations, so `[]` is the ordinary value;
- `evidence_ref`, `text_hash` and `index.json` are not read at export; and
- the projection-identity test (`pipeline/orchestrator/test_projection_identity.py`)
  guards the packaged bundle's own `formats.formats` selection (currently five:
  text-bundle, acts-database, jsonl, review-items, salvage-tier), not the Armarium's
  artifact *kind* — every literal-text format ships as a member inside the single
  `export` kind, so a kind-keyed guard could never see one arrive. Cross-format text identity across the three literal formats is proven by
  `pipeline/7_armarium/armarium_export.py::_compare_literal_projections` at build and
  verify time. A sixth format must be named in both places, or the new one passes over
  in silence.
