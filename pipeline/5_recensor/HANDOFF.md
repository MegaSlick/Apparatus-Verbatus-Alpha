# Recensor — handoff

# Stage-completion seal

Before this producer's final manifest it publishes one `decode-environment` and
one `stage-seal`. The seal witnesses this pass's disk inventory, blob contents,
run `config_digest` and `register_digest`, and `(kind, outcome)` census. An exit
held after publishing stage evidence seals it (holds remain in its census); a
pass held or refused before publishing stage evidence does not seal, so the
successor correctly refuses the missing boundary. Every difference in decoders,
platform, machine, `decode_paths_used`, and `produced_pixels` is reported by
field or decoder name. A valid difference is report-only and never refuses;
Unit 17 owns any fatal policy.

Seals are compared as the SET the stored inventory names, on both sides of the
boundary: the producer refuses to re-seal, and the successor refuses to read,
when any named seal is no longer on disk. Ordinals are the contiguous run 1..N,
so removing the latest leaves a prefix that still looks whole — and the earlier
statement would then answer for a boundary it never witnessed.
The Recensor establishes no text. It writes append-only review history under
`5_recensor/artifacts/`, using `skeleton.v1` envelopes with a derived attempt
identity, self-hash, and digest-checked parents. The stage first validates every
act's witness denominator, so a duplicate or unsealed witness record is refused
before it writes a review for an earlier act.

## Input boundary and current state

For each Designator expected act, Recensor reads all Testimonia by chair and
derives a current outcome only from the unique greatest `attempt_ordinal`. A
missing configured chair, an unsealed extra chair, or a duplicate ordinal is a
fatal accounting error; it is never resolved by sort order. Completed coverage is
`read` plus `genuinely-empty`, while failed and not-run outcomes remain visible
shortfalls.

The act-attachment mirror is checked against those exact current records before
the witness floor is counted. For a page witness, geometry against the sealed
proposal independently derives `attached`; for an act-scoped witness, the exact
current Testimonium's outcome derives `attached` and its retained payload derives
`comparable`. The attachment must reference that current artifact. Thus a paired
`attached: false, comparable: false` forgery cannot pass merely because the two
booleans remain internally consistent.

It reads the current Perlectio in the same unique-ordinal manner, verifies its
direct evidence, and reconciles the Designator's proposed continuation flag
against its own authoritative continuation link (see below). A non-completed
reading, short continuation, exhausted recovery budget, or declared hold is
recorded as held-for-review rather than accepted.

## The continuation link is the Recensor's, not the Designator's

ARCHITECTURE and spec 09: "the Designator proposes continuations; the
Recensor's link is the authoritative relation." `recensor_continuation_link`
derives `is_continuation`/`page_ordinals`/`region_ids` directly from the
act's *original proposal* regions — never from the Designator's own
`has_continuation` seal flag, which is that stage's proposal, not a settled
fact this stage inherits unexamined. `reconcile_continuation` then checks the
seal's claim against this link: a claim the evidence does not confirm is held
for review (a continuation shortfall); the seal denying a continuation its own
evidence already proves is fatal accounting, because silently agreeing with an
under-claiming seal would let it override this stage's own authority over the
fact. Every review payload — including a Designator-held act's — carries this
fact under `payload["continuation"]`, derived from whatever regions were
actually cut rather than hardcoded. A Designator hold has two shapes
(`pipeline/2_designator/run.py::initial_pass`): the act's own page never
sealed, so no region of it is cut and the link really is empty; or the page
sealed and the near-side region **was** cut while a declared continuation's
page did not, in which case the link carries that region's real page and
region ids. Reporting the second shape as empty is what
`fbf2374` fixed — it dropped a flagged page's only evidence whenever the held
act was the one act touching it.

## `kind="review"`

Every readable-act review payload has `act_key`, `attempt_ordinal`, coverage,
the applicable recovery counts/bounds, `continuation`, `page_coverage`, a reason
where applicable, and `perlectio_ref`. `continuation` and `page_coverage` are on
every review shape without exception, including the `recovery-requested` one: an
act whose recrop is never answered would otherwise leave its flagged-page finding
recorded nowhere at all.
The Perlectio reference is both a payload fact and a direct input: it names
exactly the reading the review assessed. Ordinary terminal records use
`accepted` or `held-for-review`; held Designator acts instead directly input
their hold evidence.

An accepted review is not a new reading and does not select among witnesses. It
only records that this precise Perlectio and the conserved geometry/coverage
reconciled.

## Blank confirmation: `confirmed-blank`, the other terminal outcome for a non-completed reading

ARCHITECTURE and spec 09 name it: "a zero-output unit is diagnosed, then either
sealed confirmed-blank with evidence or held unresolved-with-evidence. Never
quietly completed." `blank_corroboration` is the gate. It fires only when the
Perlector's own reading — its direct examination of the ink, never testimony —
returned `no-readable-text`, and every witness that reached a completed-class
outcome for that act independently reports `genuinely-empty` too, with the
configured witness floor met and no chair left unresolved.

This is **unanimity about an absence, never a selection among presences**
(GOVERNANCE 3): nothing here chooses a reading, and no text is established
either way. The Perlector already made the direct claim; the witnesses only
corroborate or contradict it. A single chair that actually read text refuses
corroboration outright — the act falls through to the ordinary
`held-for-review` path instead, exactly as an under-witnessed or unresolved
act does. Every other non-completed Perlector outcome (`failed`, `truncated`,
`not-run`) is not a positive claim of absence and never reaches this gate; it
always holds. A continuation shortfall or a scenario-declared hold also
disqualifies confirmation, for the same reason they disqualify acceptance:
there is unread or human-flagged evidence the corroboration cannot see.
So does any recovery region: inherited Testimonia remain bound to the original
proposal regions, and cannot corroborate absence in witness-uncovered ink they
never saw. The expanded act remains held even when every inherited Testimonium
reported `genuinely-empty`.

**The seal's own evidence claim is checked before it is published.** The record
this outcome writes says that named chairs actually read this act and
independently report the same absence, and until Sol-S1 nothing verified that
sentence: the Attestatores could mint a completed `genuinely-empty` for every
chair from a Designator page-fallback act's identity, without asking anything,
and this stage read the three artifacts as three independent completed reads.
Stage 3 no longer produces such a record (`pipeline/3_attestatores/HANDOFF.md`,
"Outcomes and provenance") — that upstream deletion is the Sol-S1 repair, and
this gate is defence in depth against a resealed or foreign artifact rather
than a second catch for Sol-S1 itself (whose fabricated records carried both
facts, minted by the same buggy writer). `blank_corroboration` requires each
corroborating chair's current Testimonium to retain the regions it was shown
and the serving receipt for the attempt; a completed-class outcome missing
either is a record this pipeline's own writer cannot produce, so it is
`FatalAccounting` rather than a quiet hold: a hold would say the evidence was
weak, and what is true is that it is not this stage's to interpret. This is a
presence check; its strong per-byte counterpart runs at the Perlector
(`validate_serving_provenance` and region-identity verification) over the same
artifacts earlier in every run. `chair_read_evidence` derives those facts from the same
`chair_current_attempts` collapse `chair_outcomes` uses, so the two cannot
disagree about which attempt is current.

`confirmed-blank` is COMPLETED-class and terminal at the Recensor
(`ArmariumCategory.CONFIRMED_BLANK`) — Archetypus's existing `review["outcome"]
!= "accepted": continue` guard and Armarium's existing generic
`terminal_category(RECENSOR, review["outcome"])` routing already handle it
correctly with no code of their own; both were built to this shape before this
outcome was ever produced.

## Residual-ink page coverage: `payload["page_coverage"]`

ARCHITECTURE's candidate list, spec 09's own words: "coverage vs the proposal-
set seal **plus a residual-ink check whose input is the page image itself,
never the proposal set** — a denominator derived only from proposals cannot
see an act nobody proposed (GOALS 1)." `pipeline/5_recensor/residual_ink.py`
is that check: a pure function over one sealed page's own decoded pixels and
the page-pixel bounds of every region currently cut on it (proposal and
recovery, from every act that touches the page), with no witness, no reading,
and no stage's claim about what it found anywhere in it. `run.py`'s
`page_coverage_findings` computes this once per run, for every sealed page
with at least one region cut on it, and caches it for every act that reaches
one of those pages.

A flagged page — enough ink outside every currently-cut region, past both a
minimum pixel count and a minimum fraction (both PROPOSED, not measured; see
that module's own comment) — holds **every act that touches it**, not a
guessed "responsible" one: nobody yet knows which act, if any, the uncovered
ink belongs to, and a human needs the whole page. A successful recovery crop
that reaches the missed ink clears the finding on the very next Recensor pass,
with no code path here that requests one — recovery requests are per-act, and
there is no act to request a recrop for when the ink belongs to nobody's
proposal at all. `payload["page_coverage"]` (`checked_pages`, `flagged_pages`)
is recorded for every act, the same way `continuation` is, not only when it
flags something.

**A page with zero regions cut on it at all has no late finding here.** The
preceding Ink Map stage now measures every sealed page before detection,
including this shape, through the same `common.residual_ink` implementation.
This late proposal/recovery reconciliation still has no act to attach such a
finding to; Unit 14 owns the hold outcome. The classic silent-failure shape
this check exists to catch — the old pipeline's own measured 218-of-29,950
pages that claimed success while producing nothing — is a page the Designator
marked out *nothing* on. What changed with Unit 9 is that the pixels are no
longer unexamined: the early map holds a record for that page, measured before
any proposal existed. What has not changed is that this late pass has no act
to hang a finding on, and that the run aggregate still becomes `partial` for
such a page only through the Designator's own silent-page reason
(`common/contracts/outcomes.py`), not through anything the map records.
Closing the rest of that gap needs either a real structural Designator that
can be *wrong* about finding zero acts (the walking skeleton's synthetic
proposer always agrees with the declared fixture) or a page-level reread
capability neither this pass nor spec 08 builds yet. Left named, not papered over.

## `kind="recovery-request"`

ARCHITECTURE and spec 09 both name two distinct recovery operations: a
Designator recrop (`fallback-recrop`) and a Perlector page-level or
continuation-aware reread (`page-level-reread`). `config/recovery.toml`
budgets them separately, and every request now names which one it means in
`payload["recovery_kind"]` — a request or review missing or misnaming it is
fatal accounting, never a silent default. The kind names are the pipeline's
own hyphenated vocabulary and are deliberately not the snake_case TOML keys
they are budgeted under, so renaming a config key never moves a sealed
artifact's words. Only `fallback-recrop` has a real
downstream implementation today: the Designator refuses to answer any other
kind, and the orchestrator refuses to dispatch one, rather than silently
treating it as a recrop. So the Recensor requests only `fallback-recrop`, even
where ARCHITECTURE's "full-page or continuation-aware pass" would suggest
`page-level-reread` (a continuation shortfall) — asking for an operation
nothing downstream can honor would trade a graceful hold for a hard crash, and
that is a regression, not a fix. `recovery_state` also tracks each kind's own
sub-budget (`requests_by_kind`) rather than pooling every request into one
shared count.

When the bounded per-kind budget permits a recrop, Recensor appends a
`recovery-requested` request. Its direct input is the exact Perlectio, and its
payload carries the act key, ordinal, recovery kind, coverage, budget
used/allowed, the complete resolved recovery policy, and `perlectio_ref`. It
appends a matching `recovery-requested` review whose direct inputs are that
same Perlectio and exact request, with `recovery_request_ref` and the same
policy in its payload.

`config/recovery.toml` is read through `common.recovery`, is included in the
run configuration digest, and records its file digest, absolute cap, and resolved
allowed budget. The orchestrator reads the latest review, rechecks its request and
policy bindings, then invokes the Designator with the request id. Neither a bare
CLI command nor an unbound request may cause a recrop.

## The run-level hard-failure cap is the orchestrator's, not this stage's

Distinct from the per-act recovery budget above: `common/hard_failure.py` and
`config/hard_failure.toml` bound how many accounted hard failures (a closed,
configured list of `(stage, outcome)` pairs — see that config's own comments
for the proposed list and its reasoning) ONE RUN may carry before it needs
Tyrel rather than another automatic stage invocation. It is computed fresh
from the artifacts on disk at every stage boundary and every recovery round,
by `pipeline/orchestrator/run.py`, never by this stage — the Recensor has no
run-level view and no authority to halt a sequence it does not control. Two
hard failures is Tyrel's named early warning and does not stop anything; more
than two halts the orchestrator at the next stage boundary, with whatever
finished intact. A recovery round is three sections — every outstanding act's
recrop, then every reread, then one Recensor pass — and the cap is judged at
each of those three boundaries, never between two acts of the same batch.

`config/hard_failure.toml` is sealed into `run.json`'s `config_digest` exactly
as `config/recovery.toml` is: a later edit to what counts as a hard failure
refuses the sealed run rather than reinterpreting failures already on disk.

## The partition receipt

Spec 09: "a self-hashed run receipt that **recomputes every denominator from
the artifacts on disk** rather than trusting stage manifests. The receipt is
what makes 'complete' a refutable claim." After its own manifest is refreshed,
this stage writes `run-health/recensor-partition-receipt.json`
(`common/recensor_receipt.py`). It refuses to build at all if any upstream
stage manifest disagrees with its on-disk artifacts, rederives the act
denominator through `expected_acts`, recomputes every act's witness coverage
from the testimonia themselves, and refuses a review whose recorded act key or
coverage does not match what disk says. Its summary — `by_partition_class`,
`recensor_status`, `reasons` — is derived from its own items and revalidated on
the way out, so a hand-edited count cannot survive a read.

**Its scope is part of the record and is narrower than "the run is complete".**
`scope` says so literally: the proposal-act and configured-witness denominators
at the moment the Recensor reviewed them. The residual-ink and continuation
facts live in the review payloads it cites, and a page nobody cut a region on
is outside every denominator here. The Armarium's own export aggregate remains
the run-level statement.

Unlike an artifact it is replaced in place rather than appended: a bounded
recovery legitimately changes the current partition, while the immutable review
and request evidence it was derived from stays beside it. It is inside
`inventory_scope()` all the same — a record a reviewer recomputes denominators
from may not be a file nothing accounts for.

## Consumer obligations

Archetypus establishes text only for a current `accepted` review and follows its
exact `perlectio_ref`; it does not reselect a newer reading. Armarium derives the
terminal category from this review history and keeps all holds visible, so a
partial result cannot present as complete.
