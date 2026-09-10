# Ink map — handoff

The ink map runs after the Exemplar seal and before the Designator. It writes one
`kind="ink-map"` record per sealed page, including zero-ink pages, with the shared
`common.residual_ink::residual_ink` result measured against empty coverage.

## The paper value is the Designator's, and the contrast is this stage's

Since 2026-09-06 the background every count here is taken below comes from
`common.background::infer_background_evidence`, under the sealed
`[grouping.background]` block of `config/designator_grouping.toml` resolved for
this page's own dimensions — the same inference, the same policy and the same
bytes the Designator's structure pass runs under, proved against the run's
`designator-grouping` seal at the point of use.

**What that repaired.** This stage used to take the page's single most common
pixel as paper. On a photographed register opening that is the bezel — 0 or near
it on every page of the Designator's 127-page calibration that reaches its
surround branch — so `background - MINIMUM_CONTRAST_BELOW_BACKGROUND` was below
every 8-bit sample, the page mapped as carrying approximately no ink at all, and
`flagged` was `False` by construction. A coverage audit that passes because its
threshold cannot be reached is not evidence of coverage, and the cross-stage
containment pin in `common/test_designator_recensor_ink_calibration.py` held
over an empty set.

**What is deliberately *not* shared is the contrast.**
`MINIMUM_CONTRAST_BELOW_BACKGROUND` stays this module's own 40. Sharing the
Designator's derived margin as well would make this measure a restatement of the
stage it exists to check independently. Because 40 is below the margin a
photographed page derives for itself, this stage counts *more* ink than that
stage's primary scan does — including paper — and far less than its conservation
denominator of 2. All three now sit under one background, which is what makes
that ordering a statement about sensitivity rather than about two different
statistics.

`payload["background"]` records the paper value, the branch it came from, the
page's dark mode, the Designator's derived margin, this stage's own contrast,
the ink threshold that produced every count on the record, and the digest of the
sealed policy — so a reader can recompute the level the page was measured at
(GOVERNANCE 6).

## A page whose paper cannot be inferred is named, not zeroed

`outcome="ink-not-measurable"` is a third outcome beside `mapped` and
`unclaimed-edge-ink` (`common.residual_ink.INK_NOT_MEASURABLE`). The record
carries `ink_measurable: false`, the refusal's own text, and the policy digest,
and it carries **no** `ink`, `edge` or `edge_findings` key at all: there was no
threshold, so there are no counts and no retained runs, and an absent key is what
makes a consumer fail loudly instead of reading zero as a measurement.

The page stays in the census. The Designator still cuts and reads it, and the
Armarium reconciles the same page denominator it always did — that page's row
records `initial_outcome: "ink-not-measurable"` with `remeasured: null`, and no
edge hold can be derived from or released for it. No fixture page reaches this
outcome; every walking-skeleton page is majority-paper and infers through the
plain modal branch.

Each record carries `payload["page_ordinal"]`, the sealed page's ordinal from the
run's `source_manifest`. That ordinal is the identity a consumer joins on: this
document is the interface, and without it named here a consumer has to guess —
keying on `subject_id` instead silently mismatches records the day subject naming
changes, and a page's ink evidence lands against the wrong act. The stage refuses
the whole census rather than publish a record it cannot bind to exactly one
submitted source.

`payload["edge_findings"]` retains this stage's lossless page-space ink runs so
the Armarium can re-measure the same pixels rather than decode the page again
under a possibly different measurement. **Its size on a real page is PROPOSED,
NOT YET MEASURED.** What is measured is the fixture: page 1 of
`proof.synthetic_pages` is 200x260, carries 2,880 runs, and serializes to about
21.8 KiB of JSON. That figure says nothing about a 300-DPI register page, whose
larger raster and denser handwriting move both terms, and this stage's shard is
described elsewhere in units of a thousand pages. Extrapolating from the fixture
would be exactly the unmeasured claim GOVERNANCE 10 forbids, so the shard disk
budget stays an open question against real material and is named here rather
than assumed away.

`payload["edge"]` is the bounded `unclaimed-edge-ink` detector: it measures only
the page's own perimeter strip using that same implementation. A flagged record
is unresolved evidence, not a hold. **Unit 14 owns the explicit hold outcome for
an unproposed cross-page half act.**

**The strip and the gate are measured now, and both are sealed.** They were the
flat 64 pixels and the flat 2,000 outside-coverage pixels until 2026-09-06, both
PROPOSED-NOT-MEASURED and both reasoned against a 200x260 fixture. They are
`[coverage_audit] edge_band_bp = 100` and `substantial_ink_area_bp = 4` in
`config/designator_grouping.toml`, `sample_count = 44`, resolved per page against
its own shorter side and its own area; the block's caveat carries what the
sample does and does not establish. This stage proves that file's bytes against
the run's `designator-grouping` seal exactly as it does for the background
policy, and every finding it publishes carries the resolved gate beside the
counts it decided.

**The counts on this record are the page's AUDITED ink.** `total_ink_pixels` and
`outside_ink_pixels` are this page's ink with its page-spanning component taken
out of both -- the component the Designator withholds from detected grouping
while retaining all its pixels in conservation. Declared or fallback coverage
may claim those pixels, and conservation holds any unclaimed remainder.
`page_ink_pixels` is every pixel the audit calls ink and
`page_spanning_ink_pixels` is that separately accounted part, so the whole-page
figure is on the record and nothing has gone quiet. The retained
`edge_findings` runs are the same audited set, which is why their schema id is
`ink-runs.v2` and an old reader is refused rather than quietly measuring new
content under the old contract.

The Designator consumes this producer's completion seal before any detection. The
Recensor continues to use the same shared residual-ink implementation for its late
proposal/recovery coverage reconciliation.

## Two things Unit 14 must not misread

**`payload["ink"]["flagged"]` is not an alarm here.** This stage measures with
*empty* coverage, because before the Designator runs there is no coverage to
measure against. `flagged` in `common.residual_ink` means "outside-coverage ink
passed a gate", so with empty coverage it is true of every page carrying more
than 24 ink pixels, and `fraction_outside_per_million` is 1,000,000 on every
inked page and 0 on a blank one. Both are true statements and neither is
informative on its own. The field this record exists to carry forward is
`total_ink_pixels`: it is the pre-proposal denominator Unit 14's coverage
derives from. The alarm on this record is the *outcome* —
`unclaimed-edge-ink` — which is measured against a real central rectangle.

**A flagged edge record does not by itself make the run `partial`, but an
unreleased one now does.** `unclaimed-edge-ink` still classifies UNRESOLVED at
*this* boundary and terminates nothing here: this stage measures before any
proposal exists, so it cannot know whether an act claims that ink. What decides
the run is the Armarium's re-measure against the Designator's verified crops —
see the Unit 14B ledger below. `run_aggregate`
(`common/contracts/outcomes.py`) takes `edge_hold_pages` and appends a named
partial reason for every page whose edge ink no crop released, so a run
carrying one cannot report `complete`. A page whose ink the crops did claim is
released and adds no reason.

This paragraph previously recorded the opposite, as the deferral this unit had
chosen while Unit 14 was outstanding. Unit 14B has landed; the sentence is kept
here corrected rather than deleted because this file is the stage interface and
a consumer who built against the old contract needs to see that it moved.

## The fixture's current edge measure is quiet

The synthetic pages are 200x260. Their historical 64-pixel perimeter reached
painted acts, but the current sealed `edge_band_bp = 100` resolves to a
2-pixel band and contains no fixture ink. The fixture therefore exercises the
`mapped` outcome, not `unclaimed-edge-ink`; it does not establish positive
edge-finding selectivity. The 64-pixel figures below are historical comparison
evidence, not the active detector or an `EDGE_BAND_PIXELS` configuration.
Selectivity is measured on real material or not claimed.

## Unit 14B reconciliation ledger — release is by the same ink, not by exemption

The fixture's apparent degeneracy was measured against the actual declared
crop rectangles before changing it. On page 1 (200x260), the 64-pixel initial
edge band contains 8,328 of 11,520 ink pixels (72.2917%); the two declared act
crops leave 0 outside pixels. On page 2, it contains 3,384 of 3,840 pixels
(88.125%); its declared continuation crop likewise leaves 0. Thus the ink is
genuinely *claimed*; the semantic defect was treating a pre-proposal finding
as unreleased after the Designator had supplied coverage, not a specimen with
unclaimed edge ink.

Unit 14B originally retained the fixture and fixed band.
**The band was re-derived on 2026-09-06**: at the sealed `edge_band_bp` the same
two pages contain 0 of 11,520 and 0 of 3,840 ink pixels in their perimeter
strips. `MINIMUM_INK_PIXELS` remains the noise floor; the substantial-ink gate
is resolved from page area. Armarium re-measures the Ink Map's retained,
lossless page-space runs against verified final Designator crop bounds. A clear
re-measure releases the page; a flagged re-measure holds it. The
`structure-failure` scenario remains partial for its recorded structure failure
and unclaimed residual ink, not an edge hold. This distinguishes a pre-proposal
signal from a genuine unresolved coverage finding without weakening either.
