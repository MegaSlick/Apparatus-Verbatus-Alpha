# Attestatores — handoff

The Attestatores retains one immutable `kind="testimonium"` for every configured
chair and every Designator act, on every attempted read. It does not merge, rank,
select, or turn a Testimonium into established text. A missing artifact is never a
witness outcome.

## Exact input boundary

For a proposed act, the stage accepts only Designator regions whose provenance and
Exemplar crop lineage verify. Each attempted Testimonium names precisely the
original proposal regions and their pixel blobs. A later recovery crop is not
silently substituted for what a chair saw.

The current writer is the declared synthetic skeleton. Its `fixture://` serving
facts are fixture declarations, not measurements of a live model. Spec 04 has not
landed a serving response/body contract, and Spec 06's capture-as-Testimonium
intake is not present in this tree; no fake capture intake is claimed here.

## Testimonium schema

Every record payload has these fields:

```text
chair, act_key, attempt_ordinal
regions = [{region_id, image_path, image_sha256}, ...]
provenance, format_capabilities
payload, witness_reported, content_health
reason                         only when a named non-reading/failure needs one
```

`payload` is the witness's JSON-native output, retained as its own shape. An
object, array, integer, boolean, null, or text response is not flattened into a
common body schema. `witness_reported` is the witness's separate self-report — a
confidence/status claim remains evidence, but it is not health and cannot make
the stage treat a channel as complete. `content_health` is stage-computed from
native output and a trusted response-boundary fact: recordability, UTF-8 validity,
emptiness, blankness, character count for text, and truncation. It never reads
`witness_reported`; when the real serving boundary cannot supply completion,
truncation is `null`, not guessed from punctuation.

`format_capabilities` says what this witness's output format can express at all,
and it is the fact a later reader needs beside `witness_reported` to avoid a
specific mistake: a witness whose format cannot say "unsure" must not be read as
confident merely for having said something. The `witness-capabilities` scenario
declares both sides on act a1 — chair 1 cannot express uncertainty and claims
high confidence anyway, chair 2 can and reports doubt — so the distinction is
exercised rather than merely representable without blinding one chair in the
reference happy run's dissent record. Both claims are retained verbatim and
neither reaches an outcome, a coverage count, or `content_health`.

The synthetic fixture declares complete responses, so its retained text gets
`truncated=false`. A malformed or unrecordable provider response becomes a
`failed` Testimonium with an explicit reason; it is not decoded with replacement
characters, stringified, or turned into an empty report. Malformed witness
metadata never rewrites facts about a recordable native payload: an unavailable
`format_capabilities` record is retained as `null`, and `content_health` continues
to describe the native response. Arbitrary
binary response retention remains an explicit Spec 04 response-contract decision.
The current canonical artifact format can faithfully retain only float-free
JSON-native values: its shared canonical writer refuses floating-point numbers,
and this stage records that refusal as `failed` rather than coercing the number.
That is an unresolved gap against Spec 07's unexpected-but-parseable payload
requirement, not a claim that the float was retained verbatim.

### Temporary textual bridge

The prohibited-to-edit Perlector still consumes `payload.reported` as a string.
Until its owner migrates that reader, a recordable *textual* native payload also
carries `reported` as a deprecated compatibility projection. It is never derived
from `witness_reported`, never used by Attestatores health, and no structured
native payload is coerced into it. A structured Testimonium therefore lands
verbatim here but the current Perlector visibly refuses it; that integration work
belongs to the Perlector/serving-contract owners.

## Outcomes and provenance

Every configured chair has one explicit outcome per act per attempt:

- `read` and `genuinely-empty` mean a chair actually read the exact regions and
  carry a serving receipt. `genuinely-empty` has native `payload=""`; it is never
  represented by an empty file.
- `failed` means an attempt reached the response boundary but produced no usable
  Testimonium. It also carries the attempted region inputs and a receipt.
- `dead` means an `AbsentChair`: the chair was unavailable and no attempt reached
  the region. It retains the absence record, with no invented receipt.
- `not-run` means a configured chair was never attempted, including a held or
  refused proposal. It retains the resolved pin but no invented receipt.
- `excluded` is never produced by this writer. Generic envelope validation
  refuses a missing reference but checks only that the identifier is non-empty;
  Stage 3 does not yet resolve that identifier to verified Tyrel approval-record
  bytes. The positive approved-exclusion path is therefore not implemented.

For a Designator page-fallback act, this stage computes `genuinely-empty` for
every configured chair from the act's derived identity (`_is_page_fallback`),
with no fixture declaration. This is the one witness outcome not driven by a
declaration table. Whether a real serving implementation may keep that
short-circuit and skip provider calls for fallback pages remains an open ruling
for Tyrel: the 2026-08-11 fallback ruling intended fallback crops to be read
downstream. The current path is pinned by
`test_an_ink_free_page_fallback_is_witnessed_and_read_end_to_end`.

`provenance` holds the exact resolved identity/revision and, only for attempted
outcomes, the digest-checked serving receipt. A failed or absent chair cannot be
replaced by another chair.

A malformed proposal crop is isolated to its act: every chair receives its
explicit `not-run` or `dead` record, no chair is said to have read the refused
pixels, and other acts continue. Malformed native output or malformed capability
metadata similarly becomes one `failed` attempt with the remaining chair records
retained; neither case is silently repaired into a reading.

A refused crop completes this retention stage because each configured chair has
been accounted for, and the explicit non-reading records are what make the
shortfall visible downstream. It is worth being exact about how far that goes
today: the Perlector verifies the same crop lineage itself, so a crop this stage
refused for a broken lineage is refused there as a named fatal rather than
carried into a partial export. Retention completing is the guarantee here; a
partial export past a refused crop is not one this tree currently reaches.

Stage 3 holds, and every hold stops orchestration, in two shapes: an `UNKNOWN`
attempt tally, and a whole pass refused by its own no-write preflight. The
preflight refuses more than one thing — bytes that differ from an attempt
already sealed at that ordinal, an ordinal past the next appendable one, a
fixture declaring conflicting outcomes for one pair at one ordinal — and every
one of them writes nothing. Only the tally says the evidence channel is damaged.

## Retention and current state

Two write paths, and both append.

`--attempt-ordinal N` (default `1`) is the whole pass: every configured chair on
every expected act, at that one ordinal. For each `(act, chair)` pair the writer
permits only an exact byte-identical repeat of an ordinal that pair already holds,
or its next contiguous one — so the same command twice is a resume rather than a
second reading, and the whole pass still resumes over a folder in which one chair
has been reread past it. `current + 2` is refused: a gap means an attempt that
existed is no longer here.

`--operation reread --act <act_id> --chair <role>` moves exactly one chair on one
act, at the ordinal that chair's own history says comes next. This is the path a
real reread uses: a reread happens because one witness failed on one act, and
re-witnessing the other chairs to reach it would re-read ink nobody doubted and
spend a provider call per chair per act to do it. Every other chair's current
record stays the attempt it already was. It is refused, writing nothing, for an
act the proposal seal does not name, a chair the run is not sealed with, a
Designator-held act (no witness was shown a reading there), an absent chair
(a dead chair asked again is not a second attempt), and a chair with no first
attempt to follow. The orchestrator never invokes it.

A targeted reread deliberately leaves the act attachment unchanged. If the
reread changes the chair's outcome or its `content_health`, the Perlector and
the Recensor both refuse the now-stale attachment by name. The recovery is a
whole Attestatores pass with `--attempt-ordinal` set to the reread's ordinal,
run before continuing, so the attachment describes the attempt that actually
stands.

Neither path accepts the other's arguments: `--attempt-ordinal` beside a reread,
or `--act`/`--chair` beside a whole pass, is refused rather than ignored. An
operation this stage does not implement is refused for the same reason — a
mistyped `reread` would otherwise run a whole pass and exit 0 over a witness it
never asked again.

Fixture response declarations are ordinal-bound. An older row without an
`attempt_ordinal` describes attempt 1 only; a successful reread therefore carries
the newly declared native response for its own ordinal rather than silently
reusing attempt 1's testimony. A reread for which no response is declared at its
own ordinal is `failed`, not `not-run`: the invocation named one chair on one
act, so it is an attempt that produced no usable Testimonium.

Each attempt identity binds the act, the operation `read:<chair>`, and the
ordinal — `attempt_id(act_id, f"read:{chair}", ordinal)`. The RunTree's
immutable publish boundary atomically creates it and refuses different bytes at
an existing identity. The stage has no pointer and no artifact overwrite path.

Consumers derive current per chair through `common.stage.latest_per_chair()`.
Thus a later `failed` attempt is current and visible, while the earlier successful
attempt remains retained history. A missing or gapped history is refused rather
than repaired or selected around.

## Act-attachment schema (R4)

Written by the same stage invocation that writes page testimony, one
`act-attachment` record per proposed act (`subject_id == act_id`). Its payload
carries `attachments`: one entry per configured chair, each with `chair`,
`attached` (bool), `span` (`{start, end}`), `content_health` (dict or null —
null is "health not recorded", a distinct fact), `page_witness` (bool,
strictly), a `reference` to the chair's Testimonium or page-Testimonium, and
`alignment` — null for an act-scoped chair, and for a page witness exactly one
of:

- aligned: the closed key set `{status, anchor_basis, anchor_span,
  witness_span, line_geometry, loss, offset_maps}`, with `anchor_basis` one of
  `act-anchor` (computed through Chandra's located anchor line),
  `no-page-anchor` (a genuinely-empty witness's trivial zero-length attach on
  a page with no Chandra anchor at all — the ink-free/fallback path; blank
  confirmation stays open), or `act-line-not-located` (the page's anchor
  exists but locates no line for this act — the Recensor's
  `blank_corroboration` refuses to seal a terminal blank on it).
  `witness_span` indexes the markup-stripped, whitespace-collapsed view of
  the page reading, never raw bytes.
- unaligned: `{status, reason}`, reasons among `missing-chandra-page-anchor`,
  `act-anchor-line-not-located`, `no-overlap-with-act-anchor`,
  `character-limit`, `character-pair-limit`, `timeout`,
  `no-common-anchor-text` (the aligner's own reasons pass through
  verbatim), and `non-reading-page-attempt-<outcome>` for an attempt that
  produced no reading.

For a page witness, `attached` is true exactly when `alignment.status` is
`aligned` and the attempt outcome is a reading — both the Perlector
(`act_attachment_view`) and the Recensor (`act_attachment_facts`) refuse any
other combination, and both pin the shapes above; a field change here is an
interface change and lands in all three files in the same commit.

## Attempt tally

The stage's derived manifest is rebuilt from immutable Testimonia, compared to its
stored inventory, and checked against the Testimonium schema, provenance, receipts,
and exact region inputs before a re-read may append. The full act/chair denominator
is reconciled at the close of a pass rather than before one — see the last section,
which says why. `attempt_tally()` returns `KNOWN` only when that inventory is
whole. An absent, garbled, truncated or divergent inventory returns `UNKNOWN`,
`count=null`, `hold=true`, and the check runs before anything is written, so a
re-read over a damaged inventory appends nothing. The stored inventory counts as
evidence that attempts existed even when the walk finds none left: a folder whose
whole Testimonium layer is gone but whose manifest still describes it holds, rather
than taking the first-run path and writing attempt 1 over a history that recorded
more. The closing tally can also hold *after* an attempt was appended — the append
happened and is retained; what the hold says is that the folder no longer
reconciles.

**This channel is the count of attempts, not a witness's own output.** A provider
response the stage could not retain is one witness's channel, and the `failed`
attempt naming it — with `content_health.recordable=false` and a reason — is a
counted, accounted record. It leaves the tally `KNOWN`, the act under-witnessed
and the run visibly partial, and it does not stop the Perlector reading ink that
was never in doubt. Two `recordable=false` shapes are still `UNKNOWN`, because
neither can be resolved in the run's favour: a record claiming `read` or
`genuinely-empty` while saying nothing could retain what it read, and a `failed`
record carrying no reason.

This check runs immediately after Stage 3 and before a later re-read; the
orchestrator stops at an Attestatores `UNKNOWN` hold, so an older complete export
cannot mask it. Direct invocation of a later owner stage still needs that owner's
own evidence-boundary check and is not simulated here.

**Whether every configured act/chair pair is accounted for is a closing check, not
a precondition.** A pass killed part way through leaves attempts on disk and no
stored manifest, and the pass that would supply the missing pairs may not be
refused for their being missing. The stored manifest is still required before a
re-read, per spec 07 test 5, so an interrupted pass holds until someone
re-derives it — `RunTree.write_manifest("attestatores")`, one step, losing
nothing because the manifest is derived from the immutable attempts. After that
the pass resumes: the attempts already written are byte-identical repeats and the
missing ones are created. If the denominator still does not reconcile once the
pass has run, the folder holds.

One thing to know before reaching for that step: it loses nothing *while the
attempts it describes are still on disk*. Over a folder whose attempts are gone,
re-deriving the manifest discards the last record that they existed, and the
pass that follows restarts the history at ordinal 1. That is a decision someone
may legitimately take; it is not one to take without reading the manifest first.
