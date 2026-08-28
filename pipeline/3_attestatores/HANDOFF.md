# Attestatores — handoff

The Attestatores retains one immutable `kind="testimonium"` for every configured
chair and every Designator act, on every attempted read. It does not merge, rank,
select, or turn a Testimonium into established text. A missing artifact is never a
witness outcome.

## Stage-completion seal

Before this producer's final manifest it publishes one `decode-environment` and
one `stage-seal`, or reuses both on a byte-identical retry. The seal witnesses
this pass's disk inventory and blob contents, and binds the exact decode-environment
bytes, run `config_digest` and `register_digest`, and `(kind, outcome)` census. An exit
held after publishing stage evidence seals it (holds remain in its census); a
pass that never reaches its seal does not seal, whether it was held or refused
before publishing stage evidence or closed fatally after publishing it, so the
successor correctly refuses the missing boundary. Every difference in decoders,
platform, machine, `decode_paths_used`, and `produced_pixels` is reported by
field or decoder name. A valid difference is report-only and never refuses;
Unit 17 owns any fatal policy.

Seals are compared as the SET the stored inventory names, on both sides of the
boundary: the producer refuses to re-seal, and the successor refuses to read,
when any named seal is no longer on disk. Ordinals are the contiguous run 1..N,
so removing the latest leaves a prefix that still looks whole — and the earlier
statement would then answer for a boundary it never witnessed.

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
  represented by an empty file. Both are **derived from a retained, recordable
  response to that exact request** — same boundary, same retention, and the only
  difference between them is whether the retained body has characters in it. No
  act, page, or identity reaches either outcome by being a particular kind of
  thing.
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

A Designator page-fallback act is witnessed exactly like any other proposed act.
This used to be the one exception: the stage recognized the minted identity
(`_is_page_fallback`) and wrote `genuinely-empty` for every configured chair
before consulting any response boundary, then gave each record the proposal
regions, marked it attempted, minted a serving receipt and recorded
trusted-boundary health — three chairs on disk as having independently read a
page none of them was asked about, which the Recensor could then seal
`confirmed-blank` on (Sol-S1). The branch and its identity check are both gone;
nothing in this stage asks what kind of act it is reading.

So the fallback crop goes through the same response boundary as any other
proposed region, and a missing response is `not-run` (whole pass) or `failed`
(targeted reread) and holds the act. It is never an empty report: a `not-run`
record leaves every content-health fact `null`, because emptiness that nobody
measured is unknown rather than absent. `ink-free-page` declares one empty
witness response per chair for `page-fallback:3` and completes as a
`confirmed-blank`; `ink-free-page-unwitnessed` is the same page with those three
declarations removed and holds instead. Both are pinned end to end
(`test_an_ink_free_page_fallback_is_witnessed_and_read_end_to_end`,
`test_an_undeclared_fallback_witness_holds_the_act_instead_of_reporting_it_blank`),
and the resolution itself in
`pipeline/3_attestatores/test_page_fallback_witnessing.py`.

**A real implementation may make fallback pages cheap however it likes** — a
cheaper model, a coarser crop, a page-scoped call — but whatever it does has to
produce a response this stage retains, or the act holds. Skipping the provider
call entirely is the one thing it may not do, because the outcome it would skip
to is a positive claim about what a witness reported.

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
(a dead chair asked again is not a second attempt), a chair with no first attempt
to follow, a **page witness** (below), and an act whose **witness layer is
closed** (below). The orchestrator never invokes it, and that is a decision
rather than a gap: GOVERNANCE 11 gives recovery to *coverage* — a missed region,
a cut crop, a continuation — while a witness reread recovers *priming*, so
driving it from the recovery loop would make witness quality a loop variable.
`RECOVERY_KINDS` is unchanged. This is an operator repair with a documented
window.

A targeted reread re-derives that act's act-attachment as part of its own write,
through the `act_scoped_attachment_entry` the whole pass uses for the same
derivation. The attachment is a derived view of the per-`(act, chair)` attempt
stream and the reread appends to exactly that stream, so a reread that left it
alone wrote a Testimonium no later stage could consume: the very next Perlector
invocation refused the stale record, in the reread's own intended order. Only the
reread chair's entry is re-derived; the others are carried forward, and checked
against their chairs' current attempts on the way so a stale entry is refused
rather than laundered into a newer record.

## The one attempt model

**The reading attempt ordinal is a function of the act's crop history alone** —
one reading of the proposal, plus one for each recovery crop cut since
(`pipeline/4_perlector/run.py::_next_attempt`, and the identity the Recensor,
Archetypus and Armarium each enforce). Witness testimony never moves it.

That is a decision, not an omission. A Testimonium is a clue that primes a
reading, never the ink the reading is established from (ARCHITECTURE; GOVERNANCE
3), so a second look by a witness does not make a second reading exist — and
re-reading an act because a witness spoke again is the re-roll GOVERNANCE 11
refuses. The alternatives were weighed and rejected: advancing the ordinal on any
new current evidence makes witness quality a loop variable at the four stages that
decide whether text may be established, and deleting the reread outright leaves
the whole pass as the only retry, which costs every chair on every act its
currency to move one.

Two consequences follow, and both are enforced at entry rather than discovered
downstream.

**The reread has a window.** It is open until the Perlector establishes a reading
that cites this act's testimony, and closed afterwards. A new witness attempt on a
closed act — targeted reread *or* appending whole pass — is refused by name. The
deep reason is not the ordinal mechanics (a pending recovery reread means a new
reading can be pending even on a closed act): it is that a witness is only ever
shown the act's *original proposal crop* (`proposed_regions`; the Perlector
refuses testimony naming a recovery crop), so a second look can only ever add
priming, never coverage — and re-reading because a witness spoke again is
GOVERNANCE 11's re-roll. Mechanically, the Perlector would also recompute the
same ordinal, build a different payload, and meet its own immutable record. A held act's or an
absent chair's `not-run` reading cites no testimony and closes nothing. A pass
that only repeats attempts already sealed is a resume and is untouched.

**A targeted reread takes its act off the shared whole-pass ordinal.** The whole
pass is a run-level instrument at one ordinal and re-derives each act's attachment
there; after a reread that ordinal is already taken by a record describing a
different state. An appending whole pass on an act whose chairs no longer share
one current ordinal is therefore refused before anything is written. A partly-lost
attempt layer is not that case — its surviving pairs still share an ordinal — so
the repair pass still works.

**A page witness cannot be act-reread.** It reports one reading per page; its
act-level view is derived from the page join and that join's alignment against the
page anchor. An act-targeted reread would re-derive one act's view from an attempt
the page record does not describe, leaving the two disagreeing about the same
chair. No operation exists today to re-ask a page witness about anything —
building one would be new, page-scoped Attestatores work — and the refusal says
so rather than half-performing the act-scoped one. (The recovery vocabulary's
`page-level-reread` is a *Perlector* operation; that name is not borrowed here.)

One residual is left to the RunTree rather than checked at entry, deliberately:
reread *every* chair on one act up to the same ordinal and the act agrees again,
so an appending whole pass at that ordinal passes the shared-ordinal check and
meets the attachment collision at publication. Reaching it also needs each chair's
whole-pass attempt to be byte-identical to its reread attempt — otherwise
`_refuse_write_collision` stops the pass first — so the pass that survives is one
that had nothing to add. The outcome is a loud fatal refusal with
`RunTree.write_manifest` as the recorded one-step recovery, and closing it would
cost a second derivation of every attachment in preflight.

The end-to-end assertions for all of this are
`pipeline/orchestrator/test_attempt_model.py`.

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

Act-scoped Testimonium consumers derive current per chair through
`common.stage.latest_per_chair()`. The Recensor also reads `page-testimonium`
records directly for content coverage, deriving current per `(page, chair)`
through the shared `latest_attempt()` discipline. Thus a later `failed` attempt
is current and visible, while the earlier successful attempt remains retained
history. A missing or gapped history is refused rather than repaired or selected
around.

### Closed page-continuation record

`kind="page-testimonium"` is a closed producer record. It carries the ordinary
Testimonium fields plus exactly `scope="page"`, `page_ordinal`, `page_role`, and
`unjoined_act_attempts`; `reason` and the textual `reported` projection remain
the only conditional fields. The writer validates that exact shape before it
publishes, refusing unknown fields at this producing boundary.

`page_role` is one of `primary`, `continuation`, or `mixed`. It describes the
relationship of the page's contributing proposal acts to their scalar primary
page: a page reached only by continuations is `continuation`; a page containing
only primary regions is `primary`; and a page containing both is `mixed`.
Every proposed act joins every page represented by its proposal regions. Thus a
continuation publishes one page Testimonium per contributing page and per page
chair, and its act attachment carries one explicitly page-ordinalled reference
to each. The primary scalar remains act identity, never a reduction of the
evidence denominator.

An act whose crop was refused has no proposal regions to read pages off, and its
pages come from the sealed proposal facts instead — its own `page_ordinal` plus
the fixture's declared continuation page. A refused crop was never shown to a
witness, but the page-level non-reading Testimonium is still published for every
page it covered: turning an isolated crop failure into a page that vanishes from
the denominator is the silent loss GOALS 1 is about.

`page_role` is written by a producer that holds one page's whole act list, and
read back by two stages that hold different amounts of it. The Perlector holds
one act, so it refuses only the two labels that act's own primary-page fact
contradicts. `mixed` contradicts no single act, so the **Recensor** re-derives
the role from every act attached to the page and refuses a claim the whole page
disproves (`pipeline/5_recensor/run.py::reconcile_page_roles`). Its denominator
is this stage's own published attachments, not a second walk of the Designator's
regions, so the two groupings cannot drift apart.

### The page record's own outcome

R0 has no live page-scoped witness. A page Testimonium is `page_join`'s
concatenation of one chair's own act attempts on that page, so its outcome comes
from the joined text and not from the shape of the list that produced it:
`failed` when no attempt joined at all — or when the join could not carry
every attempt and the carried ones were all empty, because a completed absence
may only be claimed over a page this chair's join fully read (invariant 6);
`genuinely-empty` when every attempt joined and every one delivered an empty
body; `read` when the text carries a delivered character (delivered characters
beside disclosed omissions claim less, not more). Separators are placed only *between* delivered characters.
Joining every payload including the empty ones and calling the result `read`
whenever the list was non-empty gave a page of genuinely-empty acts
`payload="\n"` under a reading outcome — characters no act delivered, retained
as testimony to them (CodeRabbit W44). An act whose reading the join could not
carry is disclosed in `unjoined_act_attempts`; an act it carried as empty is not,
because it was carried.

## Act-attachment schema (R4)

Written by the same stage invocation that writes page testimony, one
`act-attachment` record per act in the proposal seal, held acts included
(`subject_id == act_id`). A held act carries one entry per chair with
`page_witness` false, `attached` false, `page_ordinal` null, and `alignment`
null. Its payload carries `attachments`: each entry with `chair`,
`attached` (bool), `span` (`{start, end}`), `content_health` (dict or null —
null is "health not recorded", a distinct fact), `page_witness` (bool,
strictly), `page_ordinal` (int for a page witness, **null** for an act-scoped
chair — the field is required either way, and the Perlector refuses a
page-scoped attachment that omits it as readily as an act-scoped one that
carries it), a `testimonium_ref` pointing at the chair's Testimonium or
page-Testimonium, and
`alignment` — null for an act-scoped chair, and for a page witness exactly one
of:

**The denominator is `(chair, contributing page)`, not `chair`.** An act-scoped
chair contributes exactly one entry. A page witness contributes one entry per
page the act's proposal regions came from, so an act that runs across the page
break carries two — the primary page's entry holds the real comparison view, and
each continuation page's entry is explicitly unaligned with reason
`continuation-page-no-act-anchor` (a page anchor locates a line for an act it
begins, not for the tail that runs onto it). The Perlector reconciles that exact
pair set against the regions it actually read
(`pipeline/4_perlector/run.py::act_attachment_view`), so an attachment cannot
claim a page the ink does not support, or drop one the ink does.

- aligned: the closed key set `{status, anchor_basis, anchor_span,
  witness_span, line_geometry, loss, offset_maps}`, with `anchor_basis` one of
  `act-anchor` (computed through Chandra's located anchor line),
  `no-page-anchor` (a genuinely-empty witness's trivial zero-length attach on
  a page with no Chandra anchor at all — the ink-free/fallback path; blank
  confirmation stays open), or `act-line-not-located` (the page's anchor
  exists but locates no line for this act — the Recensor's
  `blank_corroboration` refuses to seal a terminal blank on it).
  `witness_span` and its top-level `span` mirror index the raw retained page
  reading. Alignment is computed over the markup-stripped,
  whitespace-collapsed view and translated back to raw character offsets before
  publication. `offset_maps` instead map normalized-text positions to raw
  offsets, with `None` for synthesized separators, while `loss` records what
  normalization changed. Never index an `offset_maps` entry with
  `witness_span` or `span`: they use different coordinate spaces.
- unaligned: `{status, reason}`, reasons among `missing-chandra-page-anchor`,
  `act-anchor-line-not-located`, `no-overlap-with-act-anchor`,
  `no-raw-counterpart-for-aligned-span`,
  `character-limit`, `character-pair-limit`, `timeout`,
  `no-common-anchor-text` (the aligner's own reasons pass through
  verbatim), `non-reading-page-attempt-<outcome>` for an attempt that
  produced no reading, and `continuation-page-no-act-anchor` for a
  contributing page that is not the act's primary one.

For a page witness, `attached` is true exactly when `alignment.status` is
`aligned` and the attempt outcome is a reading — both the Perlector
(`act_attachment_view`) and the Recensor (`act_attachment_facts`) refuse any
other combination, and both pin the shapes above; a field change here is an
interface change and lands in all three files in the same commit.

The Recensor takes an act's chair-level `attached` as the OR across that chair's
contributing pages (`act_attachment_facts`): the act-level floor asks whether the
chair delivered this act at all, and a continuation page with no anchor of its own
may not erase the primary page's valid attachment. Every page reference stays
separately checked by the page-scoped content denominator beside it.

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
