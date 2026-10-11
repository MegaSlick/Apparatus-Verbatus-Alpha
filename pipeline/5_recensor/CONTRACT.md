# Recensor: contract

The Recensor establishes no text. It reviews every unit a page-read run counts and writes
append-only review history under `5_recensor/artifacts/` as `skeleton.v1` envelopes with a
derived attempt identity, self-hash and digest-checked parents. Every fact is measured for
every unit before the first review is published, so a refusal found late leaves no partial
set of reviews behind.

## The units

The units are the denominator's `reading_acts` rows (`common/README.md`, "Page-read
denominator"): an entry of a read page's answer (`reading` or `reading-unplaced`, `kind`
`act` or `other`), or a `page-unread` or `page-blank` page row. A `page-refused` row names a
page the Exemplar refused and gets no review. Their `hold_codes` and `disposition` are the
denominator's verified verdict, page accounting included; every entry of a parsed page whose
entries are all `other` holds `no-act-on-page-unconfirmed`. This stage does not measure the
accounting again.

## The witness floor

The configured page witnesses are the sealed roster's page-scoped chairs
(`common.page_testimonia.declared_page_witness_chairs`, the same reader the Attestatores and
Perlector use). Each page's latest `page-testimonium` per chair is validated as the Perlector
validates it (`common.page_testimonia.current_page_testimonia`). A page some chair testified to
must carry every configured page witness and no other, each one its page accounting measured,
or the stage refuses.

Every unit on a page shares the page's coverage: `witness_coverage` over each roster chair's
outcome, a chair with no Testimonium counted `not-run`, so `configured` is the page roster's
size. The page roster is the sealed roster less a routed witness on a page its rule does not
route to it (`common.page_testimonia.page_witness_chairs`; `pipeline/3_attestatores/CONTRACT.md`,
"Witness routing"): on an act page a routed dots.mocr is no part of the count; on a page routed
to it, it counts like any witness and can fill the seat DAI leaves empty.

The floor counts chairs that read the page (`read` or `genuinely-empty`) and were not
truncated, against the sealed `witness_floor`. `health_unrecorded` and `shortfalls`
(`failed`, `truncated`, `unaligned: 0`) complete the shape; a receipt whose
`shortfalls.unaligned` is not 0 is refused.

**DAI on a page with no detector record.** DAI's page on which its own record detector found no
record below its stated cap is `genuinely-empty`, bound to the detector's census
(`pipeline/3_attestatores/CONTRACT.md`), and counts toward the floor: that witness is the
detector's look, not a reading of the text. On a page whose reading establishes acts, rule (i)
holds every unit (`no-detector-record-on-act-page`), so no unit there is accepted on DAI's
silence. A DAI page whose detector stated no cap, or whose records enclosed no crop, is
`not-run`.

## Residual ink

`page_coverage_findings` measures every sealed page's pixels against every box of every
reading region cut on it (each act-region's `region_boxes_px`, never their bounding
rectangle), `act` and `other` alike, under the sealed `ink-map` policy. `page_coverage`
records the page as checked, flagged or unmeasurable, and `ink` what was measured (ink,
page-spanning, audited and outside pixel counts, the paper value and the `ink-map` digest, or
the paper-value refusal), so a confirmed-blank page records how much ink was on it.

## Outcome

`accepted` only when the row is `read`, the floor holds, no page witness is unresolved, the
page's ink is checked and not flagged, and the uncertainty assessment is not malformed.
Otherwise `held-for-review`, with every row code and every code of this stage
(`under-witnessed`, `unresolved-witness`, `residual-ink`, `residual-ink-not-measurable`,
`residual-ink-not-measured`, `uncertainty-assessment-malformed`,
`continuation-off-page-edge`) in `hold_codes`, each named in `reason`. An operator review
decision can change the outcome (below).

## Review flags

A code the sealed page-accounting policy names in `[flags] codes`
(`config/page_accounting.toml`) is a review flag: measured and recorded exactly as a hold, but
holding nothing. The row's `flag_codes` are the page accounting's `flags`; this stage's own
`residual-ink` is a flag when the policy names it. The committed policy names
`no-detector-record-on-act-page`, `unread-ink`, `residual-ink` and
`witness-short-unit-not-read`; `codes = []` makes every one a hold again.

A review carries both lists apart: `hold_codes` decide the outcome; `flag_codes` are named in
`reason` as "flagged for review, not held". `review_priority`
(`common.page_review.REVIEW_PRIORITY`: 1 look first, text or ink may be missing; 2 structure
doubt; 3 thin evidence) is the lowest tier of any code the unit carries, `null` with none. The
Armarium carries every held or flagged reading with its text in the flagged layer; the
established export is unchanged by a flag. `verbatus review` does not show flags yet, and
`operations/corpus/exactly_once.py` counts holds only.

After the receipt the stage rebuilds `run-health/recensor-review-summary.json`
(`recensor-review-summary.v1`), a report for a person that no later stage reads:
`held_pages`, `flagged_pages`, `clean_pages`, `held_units`, `flagged_units`, `by_code` (pages
and units per code, and whether it acted as a hold, a flag or both), `by_page_type`, and
`queue` (every held or flagged unit in priority, then page order).

## A page that holds no act

A `page-blank` row (`page-blank-unconfirmed`) and an entry of a page whose entries are all
`other` (`no-act-on-page-unconfirmed`) are confirmed only when the page accounting's rules (d),
(e) and (f) pass or are `flag`.

- A page of `other` entries also needs rule (i) to pass, so no detector record lies in an
  `other` region; with no record detector, or none measured, it stays held.
- A blank page needs rule (i) to pass or not apply, no detected Surya line, and every witness
  that read the page to have retained blank text. DAI's census counts as a blank witness, but
  at least one blank witness must be one that read the page's text. Blankness is measured
  from each witness's retained `payload`, never its `content_health`.
- A rule the page type switches off (`page_type.applicability`) confirms whatever its status;
  an untyped page has no such rule.
- The floor and residual ink must hold as for any unit, and nothing else may hold the row.

A confirmed blank is `confirmed-blank`; a confirmed `other` entry is `accepted`. Either names
the released code and why in `release`; `confirmation` records the rule statuses, the line
count, each witness's blankness and every failure. An unconfirmed one stays held with its
failures in `reason`.

**What a run with no record detector cannot see.** Act boundaries are measured only by the
record detector (rule (i): two records in one region, `merged-detection`; a record only in an
`other` region, `record-read-as-other`). With no detector in the roster, nothing else measures
them: a witness's units are layout blocks or lines, and one act routinely spans several. On
such a run a reading that merges two acts into one entry, or reads an act as `other` on a page
with another act, is held by nothing here. No text is lost (every cited id stays in a delivered
entry), but the act count is the reading's word alone.

## The review record

The closed `kind="review"` payload (`common/page_review.py`'s `PAGE_REVIEW_FIELDS`, plus
`attempt_ordinal`; that module is also how the Archetypus and Armarium read reviews and links):

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

Its inputs are the page reading, page accounting, act-region and Perlectio when there are
any, the sealed Exemplar page, and every page Testimonium counted for the floor.

`recoveries_used` is 1 when the page's `page_readings` row names a `reask_ref`. The stage asks
for no reading again; a page's one re-ask is the Perlector's own
(`pipeline/4_perlector/CONTRACT.md`, "The re-ask").

## Continuation

From the answer's flags alone, and only between each page's current reading's `act` entries
(the first reading's, or an operator re-read's). For each page break, the last such entry of
page p and the first of page p+1 are its sides; `other` entries (a catchword) do not move them,
nor does an entry the re-ask recovered (its place in page order is not established, so it is
never a side, a link naming one is refused, and it never holds `continuation-off-page-edge`).
Breaks are taken over real pages only: a canary page is never a side
(`common/page_review.py::run_page_breaks`).

When either side's flag says the text runs across, one `kind="continuation-link"` (subject
`page-break:<p>:<p+1>`, attempt `attempt_id(subject, "link", n)`) records it:

```
{schema: "recensor-continuation-link.v1", from_page_ordinal, to_page_ordinal,
 from_act_id | null, from_act_key | null, to_act_id | null, to_act_key | null,
 continues_to_next_page, continues_from_previous_page, agreed}
```

`agreed` (outcome `accepted`) when both sides say so; one-sided (`held-for-review`)
otherwise, a side with no `act` entry being null. A disagreeing break is still recorded. A link
holds no unit and joins nothing, but a held link counts toward the stage's held total, so the
run exits held. After an operator re-read, a link that differs is filed as the break's next
attempt; the last is current (`common/page_review.py::current_link_records`). A flag on an
`act` entry not at its page's act edge holds `continuation-off-page-edge`; a flag on an `other`
entry is recorded in `notes` and holds nothing.

## Operator review decisions

A person records a decision about a held unit or page as an `approval-record.v1`
(`common/contracts/approval.py`) in the run's `receipts/sha256/`, outside every stage record.
Every pass reads all of them (`RunTree.review_decision_records`: each file is digest-checked
against its name, stored as canonical bytes, and passes its schema and self-hash, so an edited
decision is refused, never skipped) and applies them on top of the reviews it has just measured
(`common.review_decisions.apply_decisions`). A decision binds to the basis digest of the
machine's review of its unit, or of every unit on its page, so it stays bound while nothing it
looked at changes.

- **Current** (its basis matches): `release` clears the unit's own holds; `edit` clears them
  and corrects the reading; `no-missed-act` clears its page's; `exclude` makes the review
  `excluded` (`approval_ref` on the envelope; the page keeps its holds); `hold`, `missed-act`,
  `re-ask` and `re-shoot` add a `review-*` hold code. Disagreeing current decisions about one
  subject apply none and hold it.
- **Stale** (its basis changed): never applied. A stale `hold`, `missed-act` or page `hold`
  still holds its subject with a `-carried` code until a person decides again.
- **Unkept**: a stale decision whose page is no longer in the review.

A review a decision concerns carries an `operator_review` block (unit and page basis digests,
the machine's outcome and codes, what was cleared, added and carried, the findings, and every
decision touching it). A review no decision concerns is the machine's, byte for byte.

The pass then publishes one `review-decisions` record (subject `operator-review`, attempt
`attempt_id("operator-review", "decide", n)`, outcome `recorded`), reused on an unchanged
repeat:

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

`clearances` and `page_holds` become named reasons in the run aggregate, so a run a person
cleared stays `partial`. `corrections` are the units an `edit` corrected and accepted; they are
no reason. `requests` records re-asks and re-shoots. A page `re-ask` keeps the subject held under
its `review-*` code until the run resumes from the Perlector, which reads it as an operator
re-read (`pipeline/4_perlector/CONTRACT.md`, "An operator re-read"); this stage then reviews the
re-read's units, and the reviews of the superseded reading stay as published but are no longer
current (`common/page_review.py::superseded_readings`). A unit `re-ask` only holds its unit.

**A person's correction is the reading.** An `edit` names the corrected `text` and an optional
`note`, and binds to the basis of the reading it corrects. Only a held unit is edited. Current
edits of one unit agree only when they name the same text and note (`correction_digest`). An
edit also lifts `doubt-marks-malformed` and `entry-no-readable-text`
(`common/page_review.py::EDIT_CARRIES`), since the person's text carries no machine doubt
layer; `reading-unplaced` still holds. The Archetypus establishes the text
(`common/correction.py`) and the Armarium exports it beside the model's reading.

**An override sends a held reading to export.** `release` and `no-missed-act` clear the
reading's own holds too (its Perlectio's `holds` and `page_holds`). When current decisions
clear every hold, the unit is `accepted` and the Archetypus establishes the reading as read
(`common/page_review.py::operator_override`); the Armarium labels it "released by operator".
Three holds no decision overrides, because the export cannot carry the reading
(`common/page_review.py::NOT_OVERRIDABLE`): `reading-unplaced`, `doubt-marks-malformed` and
`entry-no-readable-text`. Such a unit stays held under `review-reading-held`
(`common.review_decisions.READING_HELD`), and nothing is reported cleared that did not take
effect.

The Archetypus and Armarium compare the digest of the decisions stored when they run
(`common.review_decisions.decisions_digest`) with this record's `decisions_digest` and refuse
on a difference, so a decision recorded after the Recensor's last pass is never silently
ignored.

Refused, before anything is published: a changed set of decisions once the Archetypus has
established any reading (the decisions belong in a new run, which stops at a held Recensor
before the Archetypus; `pipeline/orchestrator/CONTRACT.md`); a decision for another run, one
that fails its digest or self-hash, or one its subject does not allow
(`common/review_decisions.py::review_decision`).

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

- **Units** are re-derived through `reading_denominator`; every sealed page has at least one.
- **`pages`** binds each page's first reading, re-ask and last accounting, with what the re-ask
  did (`common.page_reask.reask_outcome`): named ids `cleared`, `set_aside`, `held` by rule (j),
  still `unread`, and entries held as `duplicate`.
- **Every review is measured again** with the run's decisions applied; coverage, residual ink,
  confirmation, release, codes, reason, outcome, inputs and `approval_ref` must be exactly what
  disk gives, and so must the `review-decisions` record. A review whose subject is outside
  `reading_acts` (other than one of a superseded reading) is refused.
- **`release_reason`** is the review's `release.reason` for a unit this stage released, its
  `reason` for one an operator decision completed, else `null`.
- **Every `continuation-link`** is matched one to one against the flagged breaks.
- **`page_holds`** is the `review-decisions` record's (empty with no decisions). A held canary
  page stays in it and keeps the receipt `partial`; the systemic held share leaves canaries out
  (`common/page_review.py::held_pages_after_review`).

`reasons` names every unreleased held unit, every under-witnessed or unresolved unit, every held
page break and every held page; the receipt is `complete` only when there is none. A run whose
only held page is a confirmed blank can be `complete`; a page every unit of which was excluded,
but which decisions still hold, keeps it `partial`.

## Stage-completion seal

Before its final manifest the stage publishes one `decode-environment` and one `stage-seal`
(or reuses both on a byte-identical retry), binding the pass's disk inventory and blob
contents, the decode-environment bytes, the run's `config_digest` and `register_digest`, and
the `(kind, outcome)` census. A held exit after publishing evidence seals it; a pass that never
reaches its seal does not, so the successor refuses the missing boundary. Seals are compared as
the set the stored inventory names; a missing named seal refuses on both sides.

The stage opens through `common.stage.open_stage_context`, which decides the fixture or real
route from one read of the run authority. The run-level hard-failure cap is the orchestrator's,
computed from disk at every stage boundary; this stage cannot halt a sequence.

## Consumer obligations

The Archetypus establishes text only for a current `accepted` review and follows its exact
`perlectio_ref`, never a newer reading. The Armarium derives the terminal category from this
review history and keeps all holds visible, so a partial result cannot present as complete; an
`excluded` review becomes `excluded-with-approval` citing its `approval_ref`.
