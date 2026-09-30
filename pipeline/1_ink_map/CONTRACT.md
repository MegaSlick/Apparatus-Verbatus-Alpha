# Ink map — contract

The ink map runs after the Exemplar seal and before the Designator. It writes one
`kind="ink-map"` record per sealed page, including zero-ink pages, with the shared
`common.residual_ink::residual_ink` result measured against empty coverage.

## The paper value is the Designator's, and the contrast is this stage's

The background every count here is taken below comes from
`common.background::infer_background_evidence`, under the sealed
`[background]` block of `config/ink_map.toml` resolved for this page's own
dimensions — the same inference, the same policy and the same bytes the
Designator's structure pass runs under, proved against the run's `ink-map` seal
at the point of use.

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
sealed policy — so a reader can recompute the level the page was measured at.

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
would be exactly the unmeasured claim we refuse to make, so the shard disk
budget stays an open question against real material and is named here rather
than assumed away.

`payload["edge"]` is the bounded `unclaimed-edge-ink` detector: it measures only
the page's own perimeter strip using that same implementation. A flagged record
is unresolved evidence, not a hold. **Unit 14 owns the explicit hold outcome for
an unproposed cross-page half act.**

**The strip and the gate are measured now, and both are sealed.** They were the
flat 64 pixels and the flat 2,000 outside-coverage pixels, both
PROPOSED-NOT-MEASURED and both reasoned against a 200x260 fixture. They are
`[coverage_audit] edge_band_bp = 100` and `substantial_ink_area_bp = 4` in
`config/ink_map.toml`, `sample_count = 44`, resolved per page against
its own shorter side and its own area; the block's caveat carries what the
sample does and does not establish. This stage proves that file's bytes against
the run's `ink-map` seal exactly as it does for the background
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
**The band was re-derived**: at the sealed `edge_band_bp` the same
two pages contain 0 of 11,520 and 0 of 3,840 ink pixels in their perimeter
strips. The sealed `minimum_ink_pixels` (`[coverage_audit.noise_floor]`, formerly a
module constant) remains the noise floor; the substantial-ink gate
is resolved from page area. Armarium re-measures the Ink Map's retained,
lossless page-space runs against verified final Designator crop bounds. A clear
re-measure releases the page; a flagged re-measure holds it. The
`structure-failure` scenario remains partial for its recorded structure failure
and unclaimed residual ink, not an edge hold. This distinguishes a pre-proposal
signal from a genuine unresolved coverage finding without weakening either.

## Background inference, shared by every stage that reads paper

`common/background.py` infers each page's paper value and ink threshold under
`config/ink_map.toml`'s sealed `[background]`, and every stage that reads paper
calls it: this stage, the Designator's structure pass and conservation, and the
Recensor's audit. The measurements below were taken through the Designator's
shipped pass, which runs the same inference; where they say "this stage" they
mean the Designator, whose `structure-status` records publish these figures.

**Every stage's paper value comes from one inference.** The Ink Map's and the
Recensor's used to be the raw histogram mode: `common/residual_ink.py::_background_level` returned the page's
single most common pixel and `residual_ink` then called a pixel ink only if it
was `MINIMUM_CONTRAST_BELOW_BACKGROUND = 40` levels below it, so on a
photographed opening — whose most common value is the bezel, 0 or near it on
every one of the 127 calibration pages that reaches the surround branch — the
residual of a page full of writing computed to approximately zero and the
independent coverage proof passed by construction.

The inference now lives in `common/background.py` and all three stages call it
on the same page bytes: the Designator's `structure.py` re-exports it, and
`pipeline/1_ink_map/run.py` and `pipeline/5_recensor/run.py` reach it through
`common/residual_ink.py`. The decision the contract left open was whether the
audit should share this inference and lose its independence, or grow its own on
the same real material. **It shares the background and keeps its own contrast.**
Paper is a property of the page, not of the stage looking at it — two stages
inferring two different paper values for one page is a disagreement about the
specimen, and there is no second opinion to be had about it. Sensitivity is a
property of the instrument, and that stays separate: the Designator scans at the
margin each page derives for itself, reconciles at `SECONDARY_MARGIN = 2`, and
the audit runs at its own fixed 40. All three now sit under one background,
which is what turns `recensor_contrast >= SECONDARY_MARGIN` from arithmetic over
an empty set into a real containment —
`test_the_containment_is_not_vacuous_on_a_photographed_page` proves it on a
photographed-shaped page where the three ink sets nest strictly.

The background policy lives in `config/ink_map.toml`'s `[background]`, sealed as
`ink-map`. Every stage that infers paper reads it, validates it through one
function (`common.background.validate_background_table`) and proves the bytes
against the run's own `ink-map` seal, so each record names the policy it ran
under. The Designator also takes `page_spanning_area_bp` and `gap_tolerance_px` from
the ink map's `[page_spanning]` and `[connectivity]`, so the component it
withholds is the one the coverage audit takes out.

A page this inference refuses is now refused by name at all three: the Designator
records `background_source: "not-inferable"` and `ink_measurable: false`, the Ink
Map publishes `outcome="ink-not-measurable"` with no counts, and the Recensor's
audit carries the page in `page_coverage.unmeasurable_pages` on every act that
touches it rather than in `checked_pages`.

### Neutral dark-distribution evidence

The interior-mode branch still uses the same sealed band, interior-dark, whole-page-ink, and derived-margin arithmetic. Its published block is now `dark_distribution`, not `surround`: the branch can be reached by a real photographed frame, but the retained counts alone do not establish a frame, bezel, paper region, or pixels to exclude. They are the exact sampled border-band and page-wide dark populations at the recorded level, and every sampled pixel remains in the primary scan and conservation denominator. Historical passages below describing known photographed frames remain observations of those material pages; they are not a classifier guarantee for every page this admission rule accepts.

**`infer_background_evidence`'s majority-paper assumption is checked from both sides,
and it also knows a photographed page from a dark one.** The premise is
that a scanned register page is overwhelmingly paper, so its modal pixel is the
paper colour. Two shapes break it and both are refusals now. A page where ink is
the numeric majority — heavy staining, bleed-through, an inverted or
under-exposed scan — has a mode *darker than its own mean*, caught by
`mode * count >= total`. A uniformly dark page has `mode == mean`, so that
comparison passes exactly; it is caught instead by requiring the mode to be
light enough to express an ink threshold at all (`mode >= PRIMARY_MARGIN`),
because below that no 8-bit sample could ever be counted as ink and "zero ink"
would be arithmetic rather than a measurement. Solid black used to infer a
background of 0, threshold -20, and reconcile to zero ink on a visibly black
page — and on a page with no declared act, nothing caught it and the run exited
`EXIT_COMPLETE`.

**The third shape, measured on real material, is a photograph rather than a
scan.** A photographed register opening carries a black surround around the
paper — 18 to 26% of the frame on the seven real proxies measured — and pure
black is then by a wide margin the single most common value, because the paper
itself is spread across dozens of tones in the 180-240 band. So the modal pixel
was 0 on all seven, the majority-ink branch refused all seven, and the live
path cut every one into blind fallback slabs and reconciled none of their ink.
The premise "the modal pixel is paper" is sound for a flatbed scan and false
for a photograph.

`structure._dark_distribution` measures the dark fraction in a fixed interior
sample. It admits the historical photographed examples and continues to refuse
the measured inverted and dark-core controls, but it does not classify arbitrary
spatial arrangements as a frame, bezel, or page boundary. Where the interior is
within the sealed bound, the paper value is the modal pixel at or above the
page's own mean; that value still faces the `PRIMARY_MARGIN` guard and the final
ink-fraction guard. The thresholds are sealed in `config/ink_map.toml`'s
`[background]`. Its provenance has
`calibrated_for_this_corpus = true` and a positive `sample_count`; the
grouping file's `[grouping.continuation]` and the ink map's `[page_spanning]`
blocks also carry measured provenance, while other thresholds retain their own
unmeasured provenance.

**The fourth shape is the one the seven-page calibration could not see, and it is
the quiet one.** On 6 of 127 real pages the modal pixel is 255 — a blown
highlight, a scanner mount, a saturated margin — which is *lighter* than the
mean, so the majority-ink question is never asked at all, 255 is taken as paper,
and 71 to 85% of the page is counted as ink. `group_page` finds structure,
`conservation.reconcile` balances exactly, residual is zero, and nothing in the
record marks it. So a fourth question is asked of both branches, and it needs no
geometry: does the inferred value leave the page a *minority* of ink? A
background is the surface most of the page is; a value that puts more than 70% of
its own page below the threshold it implies is not one. That bound is
`max_ink_bp`, and a page over it is refused by name.

### The background inference, calibrated on 127 pages

This is the committed record `[background.provenance]`'s `source`
points at. Measured over 60 RecordGold pages, 60 parish master pages and the
seven review proxies, sampled by the fixed seed `designator-survey-2026-09-06`.
Pages were read where they lie and never copied into this tree. Rows marked
**SYNTHETIC** are shape tests built by
`common/test_background_components.py` and
`pipeline/2_designator/test_structure_failure.py`, not photographs.

**What each lever is, and what it was measured against.**

| lever | value | rejected alternatives, measured | where it sits |
|---|---:|---|---|
| `band_bp` | 500 | 250 and 1000 bp. At the two bounds below, all three give an **identical** accept-or-refuse outcome and an identical paper value on every one of the 127 pages — the band is no longer a lever at all. 1000 is rejected because the band is then 20% of each dimension and 36% of the page's area and has stopped being a frame: it averages bezel against paper, and the real pages' border figure collapses from a median of 8166 to 5025. **Not for its valley** — 1000 has the widest of the three (5,527 bp), as the table below says. Against 250 the valley does decide: 2,067 bp with the dark-core SYNTHETIC control at 5990, so a 6000 bound would already admit it | unchanged from the seven-page calibration |
| `max_interior_dark_bp` | 5000 | 3000 (the seven-page value) refuses 3 real pages and buys nothing; 6000 admits the dark-core SYNTHETIC control at a 250 bp band | in a measured 3,195 bp valley, 49 bp below its midpoint |
| `max_ink_bp` | 7000 | 8000 admits 5 of the 6 silent pages; 10000 is the bound switched off and the loader now refuses it | a **policy** bound in a continuum, in the widest gap near the top (6595→7077) and 77 bp nearer the refusing side |
| ~~`min_border_dark_bp`~~ | *removed* | 7000 refused 52 of 127; 3000–4000 would admit 82%/69% of the majority-ink pages. Removed rather than lowered: it refuses no control the interior bound does not, so no value of it earns its cost | — |

**The interior statistic, at the level midway between the page's dark mode and
its light mode**, over the 72 of 127 pages that reach the surround test:

| band | real pages (min / median / max) | SYNTHETIC dark-core control | SYNTHETIC inverted scan | valley |
|---:|---|---:|---:|---|
| 250 bp | 409 / 1534 / 3923 | 5990 | 8170 | 2,067 bp |
| **500 bp** | **287 / 955 / 3452** | **6647** | **8333** | **3,195 bp** |
| 1000 bp | 233 / 790 / 2886 | 8413 | 8750 | 5,527 bp |

The border-band figure at the same level, published and deciding nothing: real
pages 4852 / 8166 / 10000 at 500 bp, against 6578 for the inverted-scan
SYNTHETIC control — darker than 9 of the 72 real pages, which is why no bound on
it can separate them.

**The whole-page ink statistic** at `background - PRIMARY_MARGIN` under the
inferred paper, over all 127: minimum 281, median 4052, maximum 8502 bp. It is a
continuum with no gap wider than 505 bp anywhere in it, so `max_ink_bp` is
placed and not derived. The gaps at the top: 6148 → 6595 (447), **6595 → 7077
(482)**, 7077 → 7211 (134), then nothing wider than 168 until 7997 → 8502 (505).

**Outcome on the 127, against the seven-page calibration it replaces:**

| source | pages | inferred | `inferred-modal` | `inferred-interior-mode` | refused | refusal rate |
|---|---:|---:|---:|---:|---:|---:|
| teklia_dai_cretdhi | 24 | 21 | 9 | 12 | 3 | 12.5% |
| recordgold_production_train_v1 | 32 | 29 | 11 | 18 | 3 | 9.4% |
| recordgold_evaluation_val_v1 | 4 | 4 | 0 | 4 | 0 | 0.0% |
| Embrun | 15 | 13 | 0 | 13 | 2 | 13.3% |
| Oka | 15 | 15 | 14 | 1 | 0 | 0.0% |
| Montebello | 15 | 13 | 0 | 13 | 2 | 13.3% |
| Saint-Jacques | 15 | 15 | 15 | 0 | 0 | 0.0% |
| control (7 proxies) | 7 | 4 | 0 | 4 | 3 | 42.9% |
| **all** | **127** | **114** | **49** | **65** | **13** | **10.2%** |

The seven-page calibration refused **52 of 127 (40.9%)**, every one of them by
`min_border_dark_bp`. This one refuses **13 (10.2%)**, every one of them by
`max_ink_bp` — that is, every remaining refusal is a page whose inferred paper
was not a background, which is the failure this stage should be loud about. 48
pages move from refused to inferred and 9 the other way; the 9 are the 6 silent
paper-255 pages and 3 of the seven review proxies, whose ink fractions at
`PRIMARY_MARGIN` (0.7449, 0.7557, 0.7690) are as unreadable as the six.

**Scale invariance, measured rather than argued.** Over the 73 sampled pages that
carry a DPI tag, resampled to a 300-DPI equivalent with LANCZOS: the outcome is
unchanged on **72 of 73**, and on those 72 the inferred paper value moves by at
most **2 grey levels** (median 0). The single page that changes is refused
natively by `max_ink_bp` and inferred after the resample. The same 73 pairs under
the seven-page calibration agree on **64 of 73**. The old block's admissibility
argument — that its three values were population fractions and therefore
scale-invariant — was wrong in a way worth naming: the fractions were
scale-invariant and the *level they were measured at*, the page's own modal
pixel, was not. A LANCZOS resample smooths a hard black spike away and the mode
moves off it. The level is now a valley between two modes, which is what the 2
grey levels above are measuring.

**The dark distribution is measured, never removed.** Every sampled dark pixel
stays on the page, stays below the ink threshold, and is counted as ink by
`primary_scan` and reconciled as ink by `conservation.reconcile`. Masking any
population out would mean deciding where the page ends, and a page edge misjudged
by thirty pixels would silently delete a marginal name — the worst kind of
loss. The `dark_distribution` block records two observed counts:
`border_dark_pixel_count` in the fixed border band and `dark_pixel_count` across
the whole page at the selected level. They are not bounds on a bezel or on a
paper region. For the 65 historical calibration pages that inferred through this
branch, the historical ratios of observed dark counts to counted ink at the
derived margin were **34.1%-79.7%** (median 58.6%) for the border-band count and
**78.5%-96.9%** (median 88.8%) for the whole-page count. These are descriptive
historical ratios, not page-boundary or writing measurements. At the retired
fixed margin of 20, the same ratios were 22.5%-58.1% (median 36.6%) and
30.3%-83.6% (median 56.9%).

**What the same 127 pages then fixed** is the constant this section used to end
by naming: `PRIMARY_MARGIN = 20` is no longer the margin the scan runs at. It is
the floor under a per-page derivation and the level `max_ink_bp` is probed at.
The next section is that measurement.

### The ink margin, derived on 127 pages

`config/ink_map.toml`'s `[background] ink_margin_bp = 3333`. The page's ink threshold is
`background - _derived_ink_margin(background, dark_mode, ink_margin_bp)`, that
is `max(PRIMARY_MARGIN, (paper_mode - dark_mode) * 3333 // 10000)` below its own
paper value. Measured on the same 127 pages, the same sample and seed, through
the same driver.

**What was wrong.** A photographed register leaf's paper is not one tone. It is a
population spread over dozens of grey levels by lighting, page curl and the
camera's response, and the modal value is that population's *peak*, not its
edge. An offset of 20 below the peak therefore lands inside the paper. Over the
127 pages at the old constant, the whole-page ink fraction ran 0.028 to 0.66 with
a median of 0.39, and, in the historical calculation that subtracted the whole-page dark count
from both ink and page counts, the resulting dark-excluded statistic still had a
median of 23%. It is not a paper-region or ground-truth writing measurement; it
only showed that the old fixed threshold included substantial lighter population.

**Why a fraction of the two modes, and not a valley.** The obvious repair is the
classical one: put the threshold in the valley between the ink population and the
paper population. There is no such valley on this material, and that is a
finding rather than a difficulty. On every photographed page in the sample the
histogram has exactly two peaks — the bezel and the paper — with a long, smooth,
monotonically rising ramp between them where the writing lives. The minimum-density
level between the page's two modes therefore sits just *above the bezel*, around
grey level 55-75, which is a page-boundary threshold and not an ink one: at it the
scan would count the frame and drop most of the writing. The writing is not a
mode. It is a few percent of the pixels smeared under the paper peak's own left
skirt, and no density statistic separates it from that skirt.

What the two modes *do* give is the page's own **scale**. The distance between
them is large on a photograph with a black surround and small on a flat scan,
in the same proportion as the paper population's own spread, so a fixed fraction
of it tracks the paper's width where a fixed offset cannot. That is the whole
claim, and the table below is what it is worth.

**Why the fraction is 3333 and not its neighbours.** Two measurements decide it,
and a third rules out the extremes.

* **The historical dark-excluded statistic** — ink left after the sampled
  whole-page dark count is subtracted from both ink and page counts. It is a
  descriptive comparison, not a paper-region or writing measurement. Over the
  ten photographed pages of the survey's own margin sweep, driven live through
  the shipped pass: 4.1%-13.6% at 2500, 2.9%-9.0% at 3000, **2.2%-6.8% at
  3333**, 1.6%-4.2% at 3750, 1.2%-3.1% at 4000. The range is retained as a
  historical observation of how the threshold moves the sampled population.
* **The component count at the sealed `gap_tolerance_px = 3`** — checked for the
  two failures a wrong margin produces, and showing neither at 3333. The median
  over the same ten pages is 2721 / 2557 / **2653** / 2982 / 3197 across
  2500-4000: flat at the bottom through 3000-3333 and rising past it, which is
  fragmentation beginning. It is a weak discriminator between 3000 and 3333 and
  it is reported as one.
* **Whether tightening changes the sampled distribution.** Going from 2500 to
  3333 removes 39-53% of the historical dark-excluded pixels on all ten pages
  while the component count moves by -24% to +35%, rising on three of them. The
  result is retained as an observed component/threshold trade-off; it does not
  identify paper, writing, or a physical boundary.

3000 and 3333 are not separated by these historical comparisons, and the report
says so. 3333 is retained as the sealed measured setting and because it is one
third, which is a number the code can state without a second one beside it.

**The floor is `PRIMARY_MARGIN` and it is load-bearing.** At 3333 it binds
wherever the two modes are 60 grey levels apart or fewer. Ten of the 127 pages
are — all from one source, all with 22 levels or fewer between their modes — and
on those the derivation has no separation to scale by. The floor also makes the
whole change one-directional: no page's derived margin is smaller than 20, so no
page's threshold rises and no page can start counting as ink anything it did not
count as ink before.

**The bound `ink_margin_bp` is held under, and why it is structural.** The loader
refuses any value at or past 5000. `_dark_distribution` measures at the midpoint of
the two modes and publishes two dark counts *as fractions of the ink the page
goes on to count*; that reading is true exactly while the derived threshold stays
at or above the midpoint, which is exactly while the fraction stays under 5000.
At 5000 the two coincide; past it the dark-distribution block would count pixels
the scan does not. Zero is refused for the reason the two bounds beside it are:
it is the derivation switched off by a value rather than by a decision.

**Scale invariance, measured.** Over the same 73 DPI-tagged pages resampled to a
300-DPI equivalent with LANCZOS, the derived margin moves by **at most 8 grey
levels** (median 0) and the historical dark-excluded statistic by at most 0.51
percentage points (median 0.01). The accept-or-refuse outcome is unchanged from the previous
unit's 72 of 73, because the bound that decides it is not measured at the derived
threshold — see the next paragraph.

**`max_ink_bp` is probed at the floor, and that is a decision with a measurement
behind it.** Asked at the page's own derived threshold the bound stops working:
the derivation takes the same wrong paper value as its upper end and slides the
threshold down with it. On the six pages whose modal branch inferred a paper of
255 — the exact silent failure that bound exists for — the whole-page ink figure
at the derived threshold is 2239-3498 bp against a median of 2434 over the other
121, completely interleaved, so no value of `max_ink_bp` separates them there. At
the floor they measure 7077-8502 against a maximum of 6595 among the pages it
admits. The bound and the derivation ask different questions and are measured at
different levels; the constant `PRIMARY_MARGIN` is what the first one needs — a
level every page shares.

**`SECONDARY_MARGIN` is not derived, by decision.** It stays 2, and so does
`conservation.reconcile`'s margin, which defaults to it. The primary margin
governs what this stage *proposes*; the secondary margin and the conservation
denominator govern what it cannot *lose*, and erring sensitive there means a
mark the grouping pass missed still appears as a residual component rather
than as an absence. Deriving those too would trade a
visible over-count for a possible silent loss. Two properties follow and both are
still pinned: the conservation scan at `SECONDARY_MARGIN` is strictly more sensitive than the primary on
every page, because 2 is below the floor and no page can invert them; and the
cross-stage containment with the Recensor's `MINIMUM_CONTRAST_BELOW_BACKGROUND`
stays a comparison of two source literals that
`common/test_designator_recensor_ink_calibration.py` reads statically.

**The cost of that decision, stated rather than buried.** Conservation at a margin
of 2 counts a photographed page as almost entirely ink — the measured
distribution is in the table below — so on real material its residual accounting
is an over-count by a very large factor, and it was already that before this unit.
Nothing here makes it worse and nothing here fixes it. What this unit changes is
that the two numbers no longer look alike: the primary scan's ink fraction is now
a measurement of the page and conservation's is not, and a reader comparing them
will see the difference rather than two plausible numbers that disagree.

**What the 127 pages measure, before and after.** Both runs went through the
shipped pass, one page per process, on the same pages in the same order.

| statistic | min | p25 | median | p75 | p90 | max |
|---|---:|---:|---:|---:|---:|---:|
| historical dark-excluded statistic, fixed margin 20 | 0.0462 | 0.1857 | **0.2327** | 0.2762 | 0.3637 | 0.5266 |
| historical dark-excluded statistic, derived margin | 0.0045 | 0.0301 | **0.0369** | 0.0503 | 0.0678 | 0.0962 |
| whole-page ink fraction, fixed margin 20 | 0.0282 | 0.3448 | 0.3946 | 0.4630 | 0.5365 | 0.6595 |
| whole-page ink fraction, derived margin | 0.0282 | 0.2097 | 0.2421 | 0.2752 | 0.3470 | 0.4728 |

The historical dark-excluded statistic subtracts the sampled whole-page dark
count from both ink and page counts. It does not identify a paper region or the
part a register's writing is on. The whole-page figure retains every sampled
pixel and reconciles; no page boundary is inferred or masked.

Per source, on the 114 inferred pages:

| source | paper (min/med/max) | dark mode | derived margin | ink fraction at 20 | ink fraction derived | historical dark-excluded, derived |
|---|---|---|---|---|---|---|
| teklia_dai_cretdhi | 172 / 212 / 231 | 0 / 0 / 22 | 57 / 69 / 75 | 0.2626 / 0.3710 / 0.6149 | 0.1875 / 0.2117 / 0.3899 | 0.0165 / 0.0356 / 0.0962 |
| recordgold_production_train_v1 | 157 / 212 / 245 | 0 / 1 / 23 | 52 / 69 / 80 | 0.2816 / 0.3946 / 0.5365 | 0.1780 / 0.2481 / 0.4080 | 0.0192 / 0.0474 / 0.0696 |
| recordgold_evaluation_val_v1 | 216 / 223 / 224 | 0 / 0 / 1 | 71 / 74 / 74 | 0.3062 / 0.4221 / 0.5624 | 0.2145 / 0.2437 / 0.2767 | 0.0221 / 0.0432 / 0.0490 |
| Embrun | 171 / 217 / 230 | 0 / 0 / 0 | 56 / 72 / 76 | 0.1635 / 0.3886 / 0.5688 | 0.1270 / 0.2310 / 0.4096 | 0.0045 / 0.0369 / 0.0797 |
| Oka | 176 / 189 / 252 | 0 / 176 / 247 | 20 / 20 / 66 | 0.0282 / 0.2137 / 0.5245 | 0.0282 / 0.2039 / 0.4571 | 0.0383 (one page) |
| Montebello | 188 / 209 / 216 | 0 / 0 / 0 | 62 / 69 / 71 | 0.3263 / 0.3892 / 0.5818 | 0.1534 / 0.2633 / 0.4728 | 0.0249 / 0.0356 / 0.0678 |
| Saint-Jacques | 185 / 197 / 208 | 9 / 10 / 26 | 56 / 62 / 65 | 0.3753 / 0.4722 / 0.5838 | 0.1837 / 0.2404 / 0.4581 | n/a (modal branch) |
| control (7 proxies) | 189 / 196 / 196 | 0 / 0 / 0 | 62 / 65 / 65 | 0.4756 / 0.5218 / 0.6595 | 0.2991 / 0.3391 / 0.3579 | 0.0308 / 0.0549 / 0.0722 |

Oka is the source the floor is for: 10 of its 15 pages have 22 grey levels or
fewer between their two modes and derive the floor margin of 20, so their ink
fraction barely moves. Saint-Jacques takes the plain modal branch on all 15, so
it has no dark-distribution block or dark-excluded statistic to report; its
whole-page figure is the relevant observed value, and it halves.

**Components at the sealed `gap_tolerance_px = 3`**, over the 114 inferred pages:
**87 / 2,772 / 24,617** against **87 / 6,237 / 24,617** at the fixed margin — the
median more than halves, and the extremes are the two Oka pages that derive the
floor and therefore do not move at all.

**The refusal outcome is unchanged, page for page.** 114 inferred, 13 refused,
every refusal `paper-is-not-a-background`, the same 13 files as before this unit.
That is by construction: the bound is probed at the floor and the floor did not
move.

**A frame holding two differently-lit leaves gets one paper value, and the
darker leaf reads as ink.** This is a limit of the design and it was found on
real material rather than on a shape test: the review proxy `da9e07ec…` is a
two-leaf opening whose right leaf is genuinely darker than the single value
inferred for the whole frame, so at that page's own derived threshold the left
leaf and the covering sheet fall out of the ink set correctly and the right leaf
is counted as ink edge to edge (overlay `changed4_proxy_da9e07ec.png` in the
session's Designator report; its ink fraction moves 0.6595 → 0.3579 and stops
there). The derivation makes the threshold right for the page's *dominant* paper
population and cannot make it right for two of them. **Nothing detects the
case**: the page infers, reconciles and publishes like any other, no bound
refuses it, and its ink fraction is the only place the failure shows — which
makes this the same shape as the light-surround limit the sealed caveat already
names, one level up. A per-region background is the repair, and it is a
different unit.
