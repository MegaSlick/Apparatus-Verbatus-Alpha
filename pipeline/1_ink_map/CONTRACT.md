# Ink map — contract

The ink map runs after the Exemplar seal and before the Designator. It writes one
`kind="ink-map"` record per sealed page, including zero-ink pages, measured by
`common.residual_ink::ink_map_page`: one background inference and one
page-spanning labelling per page, from which the edge finding and the retained
runs are both taken.

## The paper value is the Designator's, and the contrast is this stage's

The background every count here is taken below comes from
`common.background::infer_background_evidence`, under the sealed
`[grouping.background]` block of `config/designator_grouping.toml` resolved for
this page's own dimensions — the same inference, the same policy and the same
bytes the Designator's structure pass runs under, proved against the run's
`designator-grouping` seal at the point of use. The page's raw histogram mode is
not used: on a photographed opening that mode is the bezel, which would put the
threshold below every 8-bit sample and map the page as carrying no ink.

**The contrast is not shared.** `MINIMUM_CONTRAST_BELOW_BACKGROUND` is this
module's own 40, a reasoned default not measured on real pages. Sharing the
Designator's derived margin as well would make this measure a restatement of the
stage it exists to check. Because 40 is below the margin a photographed page
derives for itself, this stage counts *more* ink than that stage's primary scan
does — including some paper — and less than its conservation denominator at 2.
All three sit under one background, so that ordering is a statement about
sensitivity rather than about two different statistics.

`payload["background"]` records the paper value, the branch it came from, the
page's dark mode, the Designator's derived margin, this stage's own contrast,
the ink threshold that produced every count on the record, and the digest of the
sealed policy — so a reader can recompute the level the page was measured at.
Consumers check that `ink_margin` is the margin the sealed `ink_margin_bp`
derives from the recorded paper and dark modes
(`common.background::validate_measured_ink_map_payload`).

## A page whose ink cannot be measured is named, not zeroed

`outcome="ink-not-measurable"` is a third outcome beside `mapped` and
`unclaimed-edge-ink` (`common.residual_ink.INK_NOT_MEASURABLE`). It has two
causes, both raised as `BackgroundInferenceRefusal`:

- the shared inference refuses the page's paper (majority ink, too dark for the
  floor margin, or a background that leaves most of the page as ink); or
- the shared inference accepts the paper, but it is too dark for this stage's
  own contrast: paper at 20 to 39 passes the Designator's floor margin of 20 and
  leaves no level 40 below it. The Designator measures such a page; this stage
  cannot.

The record carries `ink_measurable: false`, the refusal's own text, and the
policy digest, and it carries **no** `edge` or `edge_findings` key at all: there
was no threshold, so there are no counts and no retained runs, and an absent key
is what makes a consumer fail loudly instead of reading zero as a measurement.

The page stays in the census. The Designator still cuts and reads it, and the
Armarium reconciles the same page denominator — that page's row records
`initial_outcome: "ink-not-measurable"` with `remeasured: null`, and no edge hold
can be derived from or released for it.

## Identity and retained evidence

Each record carries `payload["page_ordinal"]`, the sealed page's ordinal from the
run's `source_manifest`. That ordinal is the identity a consumer joins on;
`subject_id` is not, since subject naming can change. The stage refuses the whole
census rather than publish a record it cannot bind to exactly one submitted
source.

`payload["edge_findings"]` retains this stage's lossless page-space ink runs
(`ink-runs.v2`) so the Recensor and the Armarium re-measure the same pixels
rather than decode the page again under a possibly different measurement. The
shared reconciler (`common.residual_ink::reconcile_edge_finding_with_runs`)
requires the runs to span the sealed page's own width and height. **Their size on
a real page is not measured.** On the fixture, page 1 of `proof.synthetic_pages`
is 200x260, carries 2,880 runs and serializes to about 21.8 KiB of JSON; a
300-DPI register page moves both terms, so the shard disk budget is an open
question against real material.

## The edge finding

`payload["edge"]` is the bounded `unclaimed-edge-ink` detector: it measures only
the page's own perimeter strip. The strip is `[coverage_audit] edge_band_bp` of
the page's shorter side and the area gate `substantial_ink_area_bp` of its area,
both sealed in `config/designator_grouping.toml` under a provenance block that
records the sample they were measured on and what it does not establish. The
noise floor and fraction gate sit under their own provenance block, which claims
no calibration. This stage proves that file's bytes against the run's
`designator-grouping` seal, and every finding carries the resolved gate beside
the counts it decided.

**The counts are the page's AUDITED ink.** `total_ink_pixels` is this page's ink
with its page-spanning component taken out -- the component the Designator
withholds from detected grouping while retaining its pixels in conservation.
Declared or fallback coverage may claim those pixels, and conservation holds any
unclaimed remainder. `page_ink_pixels` is every pixel the audit calls ink and
`page_spanning_ink_pixels` is the separately accounted part, so the whole-page
figure is on the record. `total_ink_pixels` is the pre-proposal denominator the
later coverage checks derive from; the retained `edge_findings` runs are the same
audited set.

A flagged edge record is unresolved evidence, not a hold. `unclaimed-edge-ink`
classifies UNRESOLVED at this boundary and terminates nothing here: this stage
measures before any proposal exists, so it cannot know whether an act claims that
ink. The Armarium decides by re-measuring the retained runs against the
Designator's verified final crop bounds under the same band and gates: a clear
re-measure releases the page, a flagged one holds it, and `run_aggregate`
(`common/contracts/outcomes.py`) takes `edge_hold_pages` and appends a named
partial reason for every held page, so a run carrying one cannot report
`complete`.

The Designator consumes this producer's completion seal before any detection. The
Recensor uses the same shared residual-ink implementation for its late
proposal/recovery coverage reconciliation.

## Fixture coverage

The fixture pages are 200x260, where `edge_band_bp` resolves to a 2-pixel band
that holds none of their ink, so a fixture run maps both pages as `mapped`.
`unclaimed-edge-ink` and `ink-not-measurable` are exercised over a real
submission of pages built for them
(`test_a_real_submission_names_edge_ink_and_an_unmeasurable_page`), and the
release and hold are exercised on records built the way this stage builds them
(`pipeline/7_armarium/test_unit14b_edge_release.py`). No run tree in the
repository carries an edge finding from this stage through the Designator's
cuts to the Armarium. Positive edge-finding selectivity is measured on real
material or not claimed.
