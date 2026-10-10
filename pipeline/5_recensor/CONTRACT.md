# Recensor — contract

The Recensor establishes no text. It reviews every unit a page-read run counts and
writes append-only review history under `5_recensor/artifacts/`, using `skeleton.v1`
envelopes with a derived attempt identity, self-hash, and digest-checked parents.
Every fact is measured for every unit before the first review is published, so a
refusal found at a later unit leaves no partial set of reviews behind.

## The units

The Perlector reads each sealed page whole, and `page_review.py` reviews what it
counts. The units are the denominator's `reading_acts` rows (`common/README.md`, "Page-read
denominator"): an entry of a read page's answer (`reading` or `reading-unplaced`,
`kind` `act` or `other`), a `page-unread` or `page-blank` page row. A `page-refused`
row names a page the Exemplar refused and is not a counted unit; it gets no review.
Their `hold_codes` and `disposition` are the denominator's verified verdict, page
accounting included, and every entry of a parsed page whose entries are all `other`
holds `no-act-on-page-unconfirmed`; this stage does not measure the accounting again.

## The witness floor

The configured page witnesses are the sealed roster's page-scoped chairs, read by
`common.page_testimonia.declared_page_witness_chairs`, the one reader the Attestatores
and the Perlector also use. Each page's latest `page-testimonium` per chair is
validated as the Perlector validates it
(`common.page_testimonia.current_page_testimonia`: the record, its presentation and
inputs, its unpresented regions, its provenance and its native capture); a page some
chair testified to must carry every configured page witness and no other, and each
must be one its page accounting measured, or the stage refuses. Every unit on a page
shares the page's coverage: `witness_coverage` over each roster chair's outcome, a
roster chair with no Testimonium for the page counted `not-run`, so `configured` is
the page roster's size. The page roster is the sealed roster's, less a routed witness
on a page its rule does not route to it (`common.page_testimonia.page_witness_chairs`;
Attestatores CONTRACT, "Witness routing"): on an act page a routed dots.mocr is no part
of the count, so three of three still read; on a page routed to it, it counts like any
witness, so three of the four reading meets a floor of 3, and dots.mocr can fill the
seat DAI leaves empty on a page its detector found nothing on. The floor counts chairs that read the page (`read` or
`genuinely-empty`) and were not truncated, against the sealed `witness_floor`;
`health_unrecorded` and `shortfalls` (`failed`, `truncated`, `unaligned: 0`) complete
the shape the page-read receipt recomputes; a receipt whose `shortfalls.unaligned` is not 0 is
refused. DAI's page on which its own record detector found
no record below its stated cap is `genuinely-empty` with empty text, bound to the
detector's census (Attestatores CONTRACT, "A page the detector found nothing on"): it
counts toward the floor, and the validation above re-derives the census it rests on.
On a page with no detector record, DAI's witness is therefore the detector's look, not
a reading of the page's text. On a page whose reading establishes acts,
the page accounting's rule (i) holds every unit (`no-detector-record-on-act-page`), so no unit there is accepted on DAI's
silence. A DAI page whose detector's run facts state no cap, or whose records enclosed
no crop, is `not-run` and does not count.

## Residual ink

`page_coverage_findings` measures every sealed page's own pixels against every box of
every reading region cut on it (each act-region's `region_boxes_px`, never the
rectangle around them), `act` and `other` alike (a page with none against
nothing), under the sealed `ink-map` policy. `page_coverage` records the unit's page
as checked, flagged or unmeasurable, and `ink` what was measured: the page's ink,
page-spanning, audited and outside pixel counts, the paper value and the `ink-map`
config digest, or the paper-value refusal and that digest (`null` for a page with no
finding). A confirmed-blank page therefore records how much ink was measured on it.

## Outcome

`accepted` only when the row is `read`, the floor holds, no page witness is
unresolved, the page's ink is checked and not flagged, and the reading's uncertainty
assessment is not malformed. Otherwise `held-for-review`, with every row code and
every code of this stage (`under-witnessed`, `unresolved-witness`, `residual-ink`,
`residual-ink-not-measurable`, `residual-ink-not-measured`,
`uncertainty-assessment-malformed`, `continuation-off-page-edge`) in `hold_codes` and
each named in `reason`. An operator review decision can change that outcome
("Operator review decisions" below).

## Review flags

A code the sealed page-accounting policy names in `[flags] codes`
(`config/page_accounting.toml`, `common/page_accounting.py`) is a review flag: the
finding is measured and recorded exactly as a hold is, but it holds nothing. The row's
`flag_codes` are the page accounting's `flags`; this stage's own `residual-ink` is a
flag when the policy names it. The committed policy names four: `no-detector-record-on-act-page`,
`unread-ink`, `residual-ink` and `witness-short-unit-not-read`; `codes = []` restores
every one of them as a hold. A unit's review carries both lists apart: `hold_codes`
decide the outcome, `flag_codes` are named in `reason` as "flagged for review, not
held", and `review_priority` (`common.page_review.REVIEW_PRIORITY`: 1 look first, text
or ink may be missing; 2 structure doubt; 3 thin evidence) places the unit in the
review queue by the lowest tier of any code it carries, hold or flag, `null` with
none. A page said to hold no act is confirmed when rules (d), (e), (f) and (i) each
pass or are `flag`: the flagged finding is recorded on every unit of the page and
reaches the flagged export. The Armarium carries every held or flagged reading with
its text in the flagged layer (`pipeline/7_armarium/CONTRACT.md`); the established
export is unchanged by a flag.

Not built: `verbatus review` does not show flags yet, and the exactly-once
reconciliation (`operations/corpus/exactly_once.py`) counts holds only.

After the receipt the stage rebuilds `run-health/recensor-review-summary.json`
(`recensor-review-summary.v1`), a report for a person and nothing a later stage reads:
`held_pages`, `flagged_pages` (a flag and no hold) and `clean_pages`; `held_units` and
`flagged_units`; `by_code`, each code's pages and units counted apart, since a
page-level finding reaches every unit of its page, with whether it acted `as` a hold,
a flag or both; `by_page_type`, pages held, flagged and clean under the type the page
reading names (`page_type` on the reading or its answer, else `untyped`); and `queue`,
every held or flagged unit in review priority, then page order.

## A page that holds no act

A `page-blank` row (`page-blank-unconfirmed`) and an entry of a page whose entries are
all `other` (`no-act-on-page-unconfirmed`) are confirmed only when the page
accounting's rules (d), (e) and (f) pass. A page of `other` entries also needs rule
(i) to pass, so no detector record lies in an `other` region; with no record detector
(`not-applicable`) or none measured it stays held. A blank page needs rule (i) to pass
or not apply, no detected Surya line, and every witness that read the page to have
retained blank text. DAI's census counts as a blank witness, but at least one blank
witness must be one that read the page's text, so a page no witness read as blank text
stays held. Blankness is measured from each
witness's retained text (`payload`), never its `content_health`; a witness whose retained payload is not
text cannot confirm a blank. The floor and residual ink must hold as for any unit, and
nothing else may hold the row. A confirmed blank is `confirmed-blank`; a confirmed
`other` entry is `accepted`. Either names the released code and why in `release`;
`confirmation` records the rule statuses, the line count, each witness's measured
blankness and every failure. An unconfirmed one stays held with its code and the
failures in `reason`.

**What a run with no record detector cannot see.** Where one act ends and the next
begins is measured only by the record detector: page accounting rule (i) holds two
records inside one reading region (`merged-detection`) and a record inside only an
`other` region (`record-read-as-other`). With no record detector in the sealed roster,
rule (i) does not apply, and nothing else on the page measures act boundaries: a
witness's units are layout blocks or lines, and one act routinely spans several of them
(a margin name and its body, say), so counting them against entries would hold
ordinary pages. On such a run a reading that merges two acts into one entry, citing and
transcribing both, or reads an act as an `other` entry on a page that keeps another
act, is held by nothing here. No text is lost: every cited id and its text stays in an
entry the export delivers, the merged acts as one act and the relabelled one in the
other layer, but the run's act count is the reading's word alone.

The closed `kind="review"` payload (`common/page_review.py`'s `PAGE_REVIEW_FIELDS`,
plus the `attempt_ordinal` every review carries; that module also holds the link
fields below and is how the Archetypus and the Armarium read both records):

```
{act_key, unit_class, kind, page_ordinal, reason, hold_codes, flag_codes,
 review_priority: 1 | 2 | 3 | null, coverage,
 page_reading_ref, page_accounting_ref, act_region_ref | null, perlectio_ref | null,
 page_coverage: {checked_pages, flagged_pages, unmeasurable_pages, ink | null},
 continuation: {continues_from_previous_page, continues_to_next_page},
 uncertainty_assessment: {state, problem} | null,
 confirmation: {confirms, rules, surya_lines, witnesses, confirmed, failures} | null,
 release: {hold_codes, reason} | null,
 notes: [{code: "continuation-flag-on-other", flags}],
 recoveries_used: 0 | 1, attempt_ordinal,
 operator_review}           # only on a review an operator decision concerns
```

Its inputs are the page reading, the page accounting, the act-region and Perlectio
when there are any, the sealed Exemplar page the residual ink was measured from, and
every page Testimonium counted for the floor.

## Continuation

From the answer's flags alone, and only between each page's current whole-page
reading's `act` entries (`reading_attempt` 1, or an operator re-read's, 3 on). For each page break, the last such entry of page p and the
first of page p+1 are its sides; `other` entries around them, a catchword for one, do
not move them. Nor does an entry the Perlector's re-ask recovered: it was asked about
ids alone, may set no continuation flag, and its place in page order is not
established, so it never moves a page's act edge, is never a side of a link (a link
naming one is refused) and never holds `continuation-off-page-edge`, and a
first-reading entry at the edge stays at the edge whatever the re-ask added after it.
Breaks are taken over the run's real pages only: a canary page, which the Door appends
after them, is never a side of a link, so the last real page's flag reads as it would
with no canary beside it (`common/page_review.py::run_page_breaks`).
When either side's
flag says the text runs across, one `kind="continuation-link"` (subject
`page-break:<p>:<p+1>`, attempt `attempt_id(subject, "link", n)`) records it. A pass
over readings an operator re-read changed files a link that differs from the break's
last as its next attempt; the last is current, and one naming a superseded reading
is kept and current no more (`common/page_review.py::current_link_records`):

```
{schema: "recensor-continuation-link.v1", from_page_ordinal, to_page_ordinal,
 from_act_id | null, from_act_key | null, to_act_id | null, to_act_key | null,
 continues_to_next_page, continues_from_previous_page, agreed}
```

`agreed` (outcome `accepted`) when both sides say so; one-sided (outcome
`held-for-review`) otherwise, a side with no `act` entry being null. A break whose
sides disagree is still recorded, never dropped. Its inputs are the named sides'
Perlectios and, for a null side on a sealed page, that page's reading. A link holds no
unit and joins nothing, but a held link counts toward the stage's held total, so a run
with a one-sided break exits held, and the receipt names it. A continuation flag on an
`act` entry that is not at its page's act edge is on no break: that entry holds
`continuation-off-page-edge`. A flag on an `other` entry joins nothing and holds
nothing; its review records it in `notes`.

**No recovery request.** The stage asks for no reading again. A page's one re-ask is
stage 4's own (`pipeline/4_perlector/CONTRACT.md`, "The re-ask"): every review of a unit on the page carries `recoveries_used`, the
page's re-asks from its `page_readings` row (1 when it names a `reask_ref`, else 0),
and the receipt measures it again with the rest of the review.

## Operator review decisions

A person records a decision about a held unit or page as an `approval-record.v1`
(`common/contracts/approval.py`) in the run's `receipts/sha256/`, outside every stage
record. Every pass reads all of them (`RunTree.review_decision_records`: every file in
that directory is digest-checked against its name, and every approval among them must
be stored as its canonical bytes and pass its own schema and self-hash, so an edited
decision is refused, never skipped) and applies them on top of the reviews it has just
measured (`common.review_decisions.apply_decisions`). A decision binds to the basis
digest of the machine's review of its unit, or of every unit on its page, so it stays
bound across passes while nothing it looked at changes.

- **Current** (its basis is the subject's now): a `release` clears the unit's own
  holds, an `edit` clears them and corrects the reading (below), a `no-missed-act`
  its page's, an `exclude` makes the review `excluded`
  (`approval_ref` on the envelope citing the stored decision; the page keeps its
  holds), and a `hold`, `missed-act`, `re-ask` or `re-shoot` adds its `review-*` hold
  code. Disagreeing current decisions about one subject are applied by none and hold
  it.
- **Stale** (its basis no longer matches): never applied. A stale `hold`, `missed-act`
  or page `hold` still holds its subject with a `-carried` code until a person decides
  against the current basis.
- **Unkept**: a stale decision whose page is no longer in the review.

A review a decision concerns carries an `operator_review` block: the unit and page
basis digests, the machine's outcome and codes, what was cleared, added and carried,
the findings, and every decision touching it; its `hold_codes` and `reason` are the
result. A review no decision concerns is the machine's, byte for byte, and a run that
stores no decision publishes exactly what it would without this section.

The pass then publishes one `review-decisions` record (subject `operator-review`,
attempt `attempt_id("operator-review", "decide", n)`, outcome `recorded`), reused on an
unchanged repeat:

```
{schema: "recensor-review-decisions.v1", decisions_digest,
 applied, stale, conflicting, carried, unkept,    # decision summaries
 clearances: [{scope, subject_id, act_key, page_id, page_ordinal, decision,
               cleared, decision_hashes}],
 corrections: [{subject_id, act_key, page_id, page_ordinal, cleared, decision_hashes}],
 page_holds: [{page_ordinal, hold_codes}],        # every page still held
 requests: [{scope, subject_id, page_id, page_ordinal, decision, decision_hashes}],
 attempt_ordinal}
```

The Armarium hands `clearances` and `page_holds` to the run aggregate, where each is a
named reason, so a run a person cleared stays `partial`. `corrections` are the units a
person's `edit` corrected and accepted; they are no reason, since the person's text is
taken as the truth, and a correction kept held by anything else is not among them.
`requests` records the re-asks and re-shoots asked for. An operator's page `re-ask` is
the request to send the page through the Perlector again: the subject stays held under
its `review-*` code until the run resumes from the Perlector, which reads the page again
as an operator re-read (`pipeline/4_perlector/CONTRACT.md`, "An operator re-read"). This
stage then reviews the re-read's units; the re-ask, whose page has changed, is stale.
The reviews of a superseded reading's units stay as published and are current no more:
a review whose `page_reading_ref` an operator re-read supersedes is not a stray, and is
not among the held items (`common/page_review.py::superseded_readings`). A unit
`re-ask` holds its unit; the Perlector reads whole pages.

**A person's correction is the reading.** An `edit` names the corrected `text` and an
optional `note`, bounded by the approval contract, and binds to the basis of the
reading it corrects, so it goes stale when that reading changes. Only a held unit is
edited. Current edits of one unit agree only when they name the same text and note
(`correction_digest` on each summary); otherwise they conflict and hold it. An edit
lifts the holds an override cannot (`common/page_review.py::EDIT_CARRIES`):
`doubt-marks-malformed` and `entry-no-readable-text`, since the person's text is what
is delivered and carries no machine doubt layer; `reading-unplaced` still holds, since
no one's text gives the reading a region to cite. The Archetypus establishes the text
(`common/correction.py`) and the Armarium exports it beside the model's reading.

**An override sends a held reading to export.** A `release` clears the unit's own holds
and a `no-missed-act` its page's, the reading's own included: the codes its Perlectio
holds it on (`holds`, `page_holds`) as well as this stage's. When current decisions
clear every hold the reading carries, the unit is `accepted`, and the Archetypus
establishes the reading exactly as read (`common/page_review.py::operator_override`,
the one check all three stages make); the Armarium labels it "released by operator".
Three holds no decision overrides, because the export cannot carry the reading
(`common/page_review.py::NOT_OVERRIDABLE`): `reading-unplaced` (no region on the page to
cite), `doubt-marks-malformed` (no doubt report the export can anchor) and
`entry-no-readable-text` (no text, and an empty reading is exported only as a proved
blank). When the decisions about such a unit would accept it, it stays held under
`review-reading-held` (`common.review_decisions.READING_HELD`) with the reading's own
codes, its reason says why, and the rest of the pass goes on. Only what took effect is
reported cleared: the unit's `operator_review.cleared` and its row in `clearances` drop
every code that holds it again, and a unit row left with nothing cleared is dropped, so
the aggregate never reports a release that did not happen.

The Archetypus and the Armarium each compare the digest of the decisions stored when
they run (`common.review_decisions.decisions_digest`) with this record's
`decisions_digest`, and refuse on a difference, so a decision recorded after this
stage's last pass is never silently ignored: the Recensor runs again first. A run that
stores no decision has no record, and neither stage checks anything else.

Refused, before anything is published:

- a changed set of decisions once the Archetypus has established a reading (any
  `kind="archetypus"` record; its seal and index alone establish nothing): a decision
  recorded then cannot reach what was established, so the decisions belong in a new run
  of the submission, which stops at a held Recensor before the Archetypus
  (`pipeline/orchestrator/CONTRACT.md`);
- a decision for another run, one that fails its digest or self-hash, or one its
  subject does not allow (`common/review_decisions.py::review_decision`).

## The partition receipt

After every review is published, the stage rebuilds `recensor-partition-receipt.v6`
(`run-health/recensor-partition-receipt.json`) from disk:

```
{schema, run_id, config_digest, scope: "reading-acts-and-configured-witnesses",
 pages: [{page_ordinal, reading_ref, reask_ref | null, accounting_ref,
          reask: {named, cleared, set_aside, held, unread, duplicate} | null}],
 expected_unit_count,
 continuation_links: [{subject_id, link_ref, outcome}],
 page_holds: [{page_ordinal, hold_codes}],
 items: [{act_id, act_key, page_disposition, review_ref, review_outcome,
          partition_class, coverage, release_reason}],
 by_partition_class, recensor_status: "complete" | "partial", reasons, self_hash}
```

- **The units** are re-derived through `reading_denominator`; `expected_unit_count`
  counts them, every sealed page has at least one and no unit names another page.
- **`pages`** binds, in page order, each page's first reading, its re-ask and its last
  accounting, with `reask` what the re-ask did (`common.page_reask.reask_outcome`): the
  named ids, split into those a re-ask entry rule (j) does not hold accounts for
  (`cleared`), those the re-ask set aside (`set_aside`), those only a re-ask entry rule
  (j) holds accounts for (`held`) and those still unread (`unread`), and the entry
  numbers rule (j) holds as duplicates (`duplicate`).
- **Every review is measured again** as it was published, with the run's decisions
  applied: its coverage, residual ink, confirmation, release, codes, reason, outcome,
  inputs and `approval_ref` must be exactly what disk gives, and so must the
  `review-decisions` record; the Testimonia counted for the floor must be ones the page
  accounting measured. So no release rests on a stale confirmation and no hold is lost.
  A review whose subject is outside `reading_acts`, other than a review of a reading an
  operator re-read superseded, is refused.
- **`release_reason`** is the review's `release.reason` for a unit its page reading held
  and this stage released, the review's `reason` for one an operator decision completed,
  `null` otherwise.
- **Every `continuation-link`** is matched one to one against the breaks the answers
  flag; a missing, stray or different link is refused.
- **`page_holds`** is the `review-decisions` record's `page_holds`, empty for a run that
  stores no decision. A held canary page stays in it and keeps the receipt `partial`: a
  canary is a known-answer page, so a held canary is a real signal about the run. The
  systemic alarm's held share still leaves canary pages out, since they are controls,
  not pages of the register (`common/page_review.py::held_pages_after_review`).

`reasons` names every held unit that is not released, every under-witnessed or
unresolved unit, every held page break and every held page, and the receipt is
`complete` only when there is none. A held unit its review released is resolved, so a run
whose only held page is a blank page the review confirmed can be `complete`; a page whose
every unit an operator excluded, but which decisions still hold, keeps it `partial`. A
receipt of any other schema is refused.

## Stage-completion seal

Before this producer's final manifest it publishes one `decode-environment` and one
`stage-seal`, or reuses both on a byte-identical retry. The seal witnesses this pass's
disk inventory and blob contents, and binds the exact decode-environment bytes, run
`config_digest` and `register_digest`, and `(kind, outcome)` census. An exit held
after publishing stage evidence seals it (holds remain in its census); a pass that
never reaches its seal does not seal, whether it was held or refused before publishing
stage evidence or closed fatally after publishing it, so the successor correctly
refuses the missing boundary.

Seals are compared as the SET the stored inventory names, on both sides of the
boundary: the producer refuses to re-seal, and the successor refuses to read, when any
named seal is no longer on disk. Ordinals are the contiguous run 1..N, so removing the
latest leaves a prefix that still looks whole — and the earlier statement would then
answer for a boundary it never witnessed.

## Real ingress

The stage opens through `common.stage.open_stage_context`, which decides the route
from one read of the run authority and, on a real submission, carries the registry and
the sealed digest map this stage requires before its first line of work. The context's
fixture slot is `None` behind a refusing accessor; this stage never touches it on the
real route.

## The run-level hard-failure cap is the orchestrator's

`common/hard_failure.py` and `config/hard_failure.toml` bound how many accounted hard
failures one run may carry. `pipeline/orchestrator/run.py` computes it from the
artifacts on disk at every stage boundary; this stage has no run-level view and no
authority to halt a sequence it does not control.

## Consumer obligations

Archetypus establishes text only for a current `accepted` review and follows its exact
`perlectio_ref`; it does not reselect a newer reading. Armarium derives the terminal
category from this review history and keeps all holds visible, so a partial result
cannot present as complete; an `excluded` review becomes `excluded-with-approval`
citing the review's `approval_ref`, and the `review-decisions` record's clearances and
held pages are reasons in the run aggregate.
