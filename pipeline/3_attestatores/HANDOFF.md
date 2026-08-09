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
confident merely for having said something. The happy scenario declares both
sides on act a1 — chair 1 cannot express uncertainty and claims high confidence
anyway, chair 2 can and reports doubt — so the distinction is exercised rather
than merely representable. Both claims are retained verbatim and neither reaches
an outcome, a coverage count, or `content_health`.

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

`provenance` holds the exact resolved identity/revision and, only for attempted
outcomes, the digest-checked serving receipt. A failed or absent chair cannot be
replaced by another chair.

A malformed proposal crop is isolated to its act: every chair receives its
explicit `not-run` or `dead` record, no chair is said to have read the refused
pixels, and other acts continue. Malformed native output or malformed capability
metadata similarly becomes one `failed` attempt with the remaining chair records
retained; neither case is silently repaired into a reading.

A refused crop completes this retention stage because each configured chair has
been accounted for; its explicit non-reading records force the later partial
status. Only an `UNKNOWN` attempt tally holds Stage 3 and stops orchestration.

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

Fixture response declarations are ordinal-bound. An older row without an
`attempt_ordinal` describes attempt 1 only; a successful reread therefore carries
the newly declared native response for its own ordinal rather than silently
reusing attempt 1's testimony.

Each identity is `read:<chair>:<ordinal>`; the RunTree's immutable publish
boundary atomically creates it and refuses different bytes at an existing
identity. The stage has no pointer and no artifact overwrite path.

Consumers derive current per chair through `common.stage.latest_per_chair()`.
Thus a later `failed` attempt is current and visible, while the earlier successful
attempt remains retained history. A missing or gapped history is refused rather
than repaired or selected around.

## Attempt tally

The stage's derived manifest is rebuilt from immutable Testimonia, compared to its
stored inventory, reconciled to the full act/chair denominator, and checked against
the Testimonium schema, provenance, receipts, and exact region inputs before a
re-read may append. `attempt_tally()` returns `KNOWN` only when that inventory is
whole. An absent, garbled, truncated or divergent inventory returns `UNKNOWN`,
`count=null`, `hold=true`; a re-read then exits held and writes no replacement
attempt.

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
