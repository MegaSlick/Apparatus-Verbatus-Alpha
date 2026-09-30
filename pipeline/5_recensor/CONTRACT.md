# Recensor — contract

The Recensor establishes no text. It reviews every unit a page-read run counts and
writes append-only review history under `5_recensor/artifacts/`, using `skeleton.v1`
envelopes with a derived attempt identity, self-hash, and digest-checked parents.
Every fact is measured for every unit before the first review is published, so a
refusal found at a later unit leaves no partial set of reviews behind.

## The units

The Perlector reads each sealed page whole, and `page_review.py` reviews what it
counts. `main` refuses a run whose sealed `reading_unit` is not `"page"`. The units
are the denominator's `reading_acts` rows (`common/README.md`, "Page-read
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
the sealed page roster's size. The floor counts chairs that read the page (`read` or
`genuinely-empty`) and were not truncated, against the sealed `witness_floor`;
`health_unrecorded` and `shortfalls` (`failed`, `truncated`, `unaligned: 0`) complete
the shape the v3 receipt recomputes. DAI's page on which its own record detector found
no record below its stated cap is `genuinely-empty` with empty text, bound to the
detector's census (Attestatores CONTRACT, "A page the detector found nothing on"): it
counts toward the floor, and the validation above re-derives the census it rests on.
On a page whose reading establishes acts, the page accounting's rule (i) holds every
unit (`no-detector-record-on-act-page`), so no unit there is accepted on DAI's
silence. A DAI page whose detector's run facts state no cap, or whose records enclosed
no crop, is `not-run` and does not count. An act-scoped witness testifies to no
page-read unit and is not counted.

## Residual ink

`page_coverage_findings` measures every sealed page's own pixels against the union box
of every reading region cut on it, `act` and `other` alike (a page with none against
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
each named in `reason`.

## A page that holds no act

A `page-blank` row (`page-blank-unconfirmed`) and an entry of a page whose entries are
all `other` (`no-act-on-page-unconfirmed`) are confirmed only when the page
accounting's rules (d), (e) and (f) pass. A page of `other` entries also needs rule
(i) to pass, so no detector record lies in an `other` region; with no record detector
(`not-applicable`) or none measured it stays held. A blank page needs rule (i) to pass
or not apply, no detected Surya line, and every witness that read the page to have
retained blank text, with at least one such witness; DAI's page on which its detector
found nothing is such a witness. Blankness is measured from each witness's retained
text (`payload`), never its `content_health`; a witness whose retained payload is not
text cannot confirm a blank. The floor and residual ink must hold as for any unit, and
nothing else may hold the row. A confirmed blank is `confirmed-blank`; a confirmed
`other` entry is `accepted`. Either names the released code and why in `release`;
`confirmation` records the rule statuses, the line count, each witness's measured
blankness and every failure. An unconfirmed one stays held with its code and the
failures in `reason`.

The closed `kind="review"` payload (`common/page_review.py`'s `PAGE_REVIEW_FIELDS`,
plus the `attempt_ordinal` every review carries; that module also holds the link
fields below and is how the Archetypus and the Armarium read both records):

```
{act_key, unit_class, kind, page_ordinal, reason, hold_codes, coverage,
 page_reading_ref, page_accounting_ref, act_region_ref | null, perlectio_ref | null,
 page_coverage: {checked_pages, flagged_pages, unmeasurable_pages, ink | null},
 continuation: {continues_from_previous_page, continues_to_next_page},
 uncertainty_assessment: {state, problem} | null,
 confirmation: {confirms, rules, surya_lines, witnesses, confirmed, failures} | null,
 release: {hold_codes, reason} | null,
 notes: [{code: "continuation-flag-on-other", flags}],
 recoveries_used: 0, attempt_ordinal}
```

Its inputs are the page reading, the page accounting, the act-region and Perlectio
when there are any, the sealed Exemplar page the residual ink was measured from, and
every page Testimonium counted for the floor.

## Continuation

From the answer's flags alone, and only between `act` entries. For each page break,
the last `act` entry of page p and the first `act` entry of page p+1 are its sides;
`other` entries around them, a catchword for one, do not move them. When either side's
flag says the text runs across, one `kind="continuation-link"` (subject
`page-break:<p>:<p+1>`, attempt `attempt_id(subject, "link", 1)`) records it:

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

**No recovery.** The stage publishes no `recovery-request`, and every review carries
`recoveries_used: 0`.

## The partition receipt

`page_review.write_reading_receipt` rebuilds `recensor-partition-receipt.v3` from
disk: the units re-derived through `reading_denominator` (`expected_unit_count` of
them), `page_reading_refs` keyed by page ordinal in page order (`[{page_ordinal,
reading_ref}]`), each item's `page_disposition`, review and coverage recomputed from
the Testimonia, and its `release_reason`: the review's `release.reason` for a unit its
page reading held and this stage released, `null` otherwise. A held unit so released
is resolved and adds no receipt reason, so a run whose only held page is a blank page
the review confirmed can be `complete`; a held unit with no completed review keeps the
receipt `partial`. Each review's outcome is recomputed too: every row hold code is
kept or named in a release, a release names exactly the row's releasable codes on a
confirmed page, the witness-floor and continuation codes are what disk derives, and
the unit is held exactly when a code remains. Then the whole review is measured again
as it was published (`page_review.plan_reviews`): its coverage, residual ink,
confirmation, release, codes, reason, outcome and inputs must be exactly what disk
gives, and the Testimonia counted for the floor must be ones the page accounting
measured, so no release rests on a stale confirmation and no residual-ink hold is
lost. Every `continuation-link` is matched one to one against the breaks the answers
flag; a missing, stray or different link is refused. `continuation_links` names each
(`subject_id`, `link_ref`, `outcome`), and a held one is a receipt reason, so the
receipt is `partial` while any page break is unresolved. A review whose subject is
outside `reading_acts`, or any recovery request, is refused.

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
cannot present as complete.
