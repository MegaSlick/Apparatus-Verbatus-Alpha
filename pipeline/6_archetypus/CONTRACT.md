# Archetypus: contract

The Archetypus is the only stage that calls one machine reading established. It is not
a correction, a witness consensus or a truth claim. For each counted reading whose
current Recensor review is exactly `accepted`, it writes one once-only
`kind="archetypus"` record under `6_archetypus/artifacts/`, and then a derived
`6_archetypus/index.json`. A held reading has no Archetypus record; that absence is part
of the terminal accounting, not a gap to fill.

**Exit codes.** `EXIT_COMPLETE` when every accepted reading has a record and the index
reconciles. `EXIT_HELD` when any reading's current review is an outcome the shared
outcome algebra leaves open (no terminal category): the readings already established
are real, the stage's work is not finished, and the open act keys are named on stderr.
A refusal anywhere in establishment or index reconciliation is fatal, never a held
reading.

## Stage-completion seal

Before its final manifest the stage publishes one `decode-environment` and one `stage-seal`
(or reuses both on a byte-identical retry), binding the pass's disk inventory and blob
contents, the decode-environment bytes, the run's `config_digest` and `register_digest`, and
the `(kind, outcome)` census. A held exit after publishing evidence seals it; a pass that never
reaches its seal does not, so the successor refuses the missing boundary. Seals are compared as
the set the stored inventory names; a missing named seal refuses on both sides.

## Input boundary

The stage opens through `common.stage.open_stage_context`, which decides the fixture or
real route from one read of `run.json`; nothing below changes with the route. The
denominator is `common.stage.reading_acts`, and reviews are read only through
`common/page_review.py`: one current `review` per counted row, every row reviewed, no
review of an uncounted unit. A `page-refused` row belongs to the page census and is
never reviewed.

Before reading any review, the stage refuses a run whose stored operator review
decisions are not the set the Recensor's last pass applied, and says to re-run the
Recensor, so a decision recorded after that pass is never silently ignored. It also
refuses to run over a Recensor hold no person has passed.

- **Which rows are established.** A row whose current review is `accepted` and that
  `common.page_review.require_establishable` allows: a row whose reading's disposition
  is `read`; a held row whose only hold is `no-act-on-page-unconfirmed` and whose
  review releases exactly that code; or a held row an operator override released (every
  hold it carries cleared by current decisions the Recensor applied: a `release` of the
  unit and a `no-missed-act` of its page). An accepted row with no reading
  (`page-unread`, `page-blank`) or any other hold is fatal: no stage resurrects a held
  reading. Every record is built and checked before any is published, so a refusal
  leaves no partial set.
- **`act` and `other` readings are both established**, so every export shows the one
  established reading of each; the record carries `kind`.
- **The reading** is the row's `perlectio.v3`, which the accepted review must name and
  input. It must be of the row's kind, hold exactly the closed Perlectio field set, and
  be `read` with no `holds` or `page_holds` (or, under an operator override, held only
  on codes the override cleared). References are resolved by stage and kind, so no
  other artifact can stand in for the review or the reading by being named.
- **The region** is the one `act-region` the reading names and the row counted, proven
  from the Exemplar by `common.exemplar_boundary.verify_reading_region_lineage`. The
  record's region is exactly what that proof returns (`region_id`, `image_path`,
  `image_sha256`, `verified_dimensions`, `source_page_ordinal`, `source_page_id`,
  `transform`), and its `image_sha256` is checked against the crop's bytes.
- **Witness custody** is `common.page_testimonia.shown_page_witnesses`: the reading's
  feed showed at least one witness (a Lectio nuda is refused by name), each a current
  `page-testimonium` of the page under the label this run's regime gives it.
- **Uncertainty** is `common.contracts.uncertainty.from_page_perlectio`: the reading's
  uncertain spans and gaps with its `{state, problem}` doubt assessment, lectio kind
  `page-read`, `self_revisions: null` (not measured).
- **Serving provenance** must validate, receipt required.
- **Inputs** are the review, the reading, its act-region, the crop and, for a
  correction, the edit's stored approval.

## `kind="archetypus"`

The artifact subject is the act identity. The payload is separately self-hashed and its
field set is closed, under the schema id `archetypus-record.v2`, which moves with it:

```text
schema = "archetypus-record.v2"
act_id, act_key, page_id, kind
text, text_hash
status = "established", text_status
regions, provenance, uncertainty
dissent_ref, perlectio_ref, recensor_ref, self_hash
```

A record missing a field, or carrying one it is not defined to carry, is refused before
it is written and on every read-back, so a second text-bearing field cannot be added one
name at a time. There is exactly one `text`.

- **`text`, `provenance`** are exact copies of the reviewed Perlectio's, unless a
  person corrected it (below). `regions` is the proven act-region.
- **`text_hash`** is `common.contracts.canonical.digest_of(text)`: the digest of the
  string's canonical JSON encoding (quotes included), not of its raw UTF-8 bytes.
- **`status`** is the fixed literal `"established"`: this act has exactly one
  Archetypus record.
- **`text_status`** says what `text` contains and is derived, never stored upstream,
  by `common.contracts.outcomes.derive_record_text_status` from `text` and the
  `uncertainty` layer: any gap makes it `partial` (ink known and unread, whether or not
  `text` is otherwise empty); otherwise `established`. The derivation calls a reading
  with no text and no gap `no_readable_text`, and the Archetypus refuses such a record
  (as the Armarium refuses to deliver one):
  an empty reading is held before it reaches this stage, and a blank page is
  `confirmed-blank` at page level. `status` and `text_status` answer different
  questions and are never mirrors.
- **`uncertainty`** is the canonical uncertainty layer above, offsets in Unicode code
  points of `text`. It is the record's one damage layer.
- **`dissent_ref` and `perlectio_ref`** are the same reference by design:
  `perlectio_ref` is the evidence the record establishes from, and `dissent_ref` is
  where a reader finds the act's dissent, which lives inside that Perlectio and is
  never copied. `recensor_ref` names the accepted review. All three are digest-checked.

**A person's correction.** When the unit's accepted review applies a current `edit`,
`text` is the person's text, read from the stored approval itself; `uncertainty` is the
one fixed layer a correction carries (`lectio_kind: "person-corrected"`, no spans, no
gaps, assessment `not-assessed`: the person's text is taken as the truth); and
`provenance` is the correction's (`common/correction.py`):

```
{label: "corrected by a person", note: str | null,
 decisions: [{decision_hash, approver, timestamp, reason, approval_ref, run_id,
              page_id, basis_digest}],
 model_reading: {label: "model reading (original)", perlectio_ref, text_sha256,
                 text_status, provenance}}
```

The person's text is not read for doubt marks: `[[...]]` in it is the person's own
characters. `model_reading` names the model's reading it corrects. The Perlectio stays
in the run tree as read, and the Armarium shows its text beside the person's.

There is no alternate text, no witness text field, and no choice among readings beyond
a person's correction. A later run cannot write a second, different record under the
same once-only identity.

## `6_archetypus/index.json`

A rebuildable per-run summary derived from the immutable records on disk, exactly as
`manifest.json` is, and never the only evidence. It is the closed set `{schema, run_id,
stage, record_count, rows, self_hash}`, `schema` being `archetypus-index.v2`; each row is `{act_id, act_key, kind,
artifact_id, text_status, text_hash, relative_path, sha256}`. The stage writes it, reads
it back and reconciles it before finishing: rows, records on disk and the readings the
Recensor accepted (recomputed from the review records) must be the same set. A missing
or duplicate row is fatal, never a warning.

## Consumer obligations

The Armarium requires exactly one Archetypus record for each accepted reading and none
for any other, rather than selecting one. It reconciles each record against its row,
its accepted review, its reading and its re-proven region, rebuilds a correction's text
and provenance from the stored edit, recomputes the uncertainty layer and `text_status`
from the reading, and requires the record's inputs to be exactly the review, reading,
act-region, crop and any stored edit. A record claiming `established` over its
reading's own gap is fatal at export. `text_status` travels into every export format and
the run aggregate, where `partial` is a named reason and makes the run partial.
