# Ink map — calibration record

How the numbers in `config/ink_map.toml`'s `[background]` block were measured and why
each sits where it does. The contract the stage keeps is in `CONTRACT.md`; the sample
behind each number, and what it does not establish, is also in
`config/ink_map.toml`'s provenance blocks. Source names are neutral labels (parish A, B, C; a printed parish history).

## Neutral dark-distribution evidence

The interior-mode branch uses the sealed band, interior-dark, whole-page-ink, and derived-margin arithmetic, and publishes its block as `dark_distribution`: the branch can be reached by a real photographed frame, but the retained counts alone do not establish a frame, bezel, paper region, or pixels to exclude. They are the exact sampled border-band and page-wide dark populations at the recorded level, and every sampled pixel remains in the scan's denominator. The photographed frames described below are observations of those material pages; they are not a classifier guarantee for every page this admission rule accepts.

**`infer_background_evidence`'s majority-paper assumption is checked from both sides,
and it also knows a photographed page from a dark one.** The premise is
that a scanned register page is overwhelmingly paper, so its modal pixel is the
paper colour. Two shapes break it, and both are refused. A page where ink is
the numeric majority — heavy staining, bleed-through, an inverted or
under-exposed scan — has a mode *darker than its own mean*, caught by
`mode * count >= total`. A uniformly dark page has `mode == mean`, so that
comparison passes exactly; it is caught instead by requiring the mode to be
light enough to express an ink threshold at all (`mode >= PRIMARY_MARGIN`),
because below that no 8-bit sample could ever be counted as ink and "zero ink"
would be arithmetic rather than a measurement. Without it, solid black would infer
a background of 0 and a threshold of -20, and reconcile to zero ink on a visibly
black page.

**The third shape, measured on real material, is a photograph rather than a
scan.** A photographed register opening carries a black surround around the
paper — 18 to 26% of the frame on the seven real proxies measured — and pure
black is then by a wide margin the single most common value, because the paper
itself is spread across dozens of tones in the 180-240 band. So the modal pixel
is 0 on all seven, and the majority-ink check alone refuses all seven.
The premise "the modal pixel is paper" is sound for a flatbed scan and false
for a photograph.

`common.background._dark_distribution` measures the dark fraction in a fixed interior
sample. It admits the historical photographed examples and continues to refuse
the measured inverted and dark-core controls, but it does not classify arbitrary
spatial arrangements as a frame, bezel, or page boundary. Where the interior is
within the sealed bound, the paper value is the modal pixel at or above the
page's own mean; that value still faces the `PRIMARY_MARGIN` guard and the final
ink-fraction guard. The thresholds are sealed in `config/ink_map.toml`'s
`[background]`. Its provenance has
`calibrated_for_this_corpus = true` and a positive `sample_count`; the ink map's
`[page_spanning]` block also carries measured provenance, while other thresholds
retain their own unmeasured provenance.

**The fourth shape is the one the seven-page calibration could not see, and it is
the quiet one.** On 6 of 127 real pages the modal pixel is 255 — a blown
highlight, a scanner mount, a saturated margin — which is *lighter* than the
mean, so the majority-ink question is never asked at all, 255 is taken as paper,
and 71 to 85% of the page is counted as ink. Every count downstream balances
exactly and nothing in the record marks it. So a fourth question is asked of both branches, and it needs no
geometry: does the inferred value leave the page a *minority* of ink? A
background is the surface most of the page is; a value that puts more than 70% of
its own page below the threshold it implies is not one. That bound is
`max_ink_bp`, and a page over it is refused by name.

## The background inference, calibrated on 127 pages

This is the committed record `[background.provenance]`'s `source`
points at. Measured over 60 RecordGold pages, 60 parish master pages and the
seven review proxies, sampled by the fixed seed `designator-survey-2026-09-06`.
Pages were read where they lie and never copied into this tree. Rows marked
**SYNTHETIC** are shape tests built by
`common/test_background_components.py`, not photographs.

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
| parish A | 15 | 13 | 0 | 13 | 2 | 13.3% |
| parish B | 15 | 15 | 14 | 1 | 0 | 0.0% |
| parish C | 15 | 13 | 0 | 13 | 2 | 13.3% |
| printed history | 15 | 15 | 15 | 0 | 0 | 0.0% |
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
the scan. Masking any
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

## The ink margin, derived on 127 pages

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
| parish A | 171 / 217 / 230 | 0 / 0 / 0 | 56 / 72 / 76 | 0.1635 / 0.3886 / 0.5688 | 0.1270 / 0.2310 / 0.4096 | 0.0045 / 0.0369 / 0.0797 |
| parish B | 176 / 189 / 252 | 0 / 176 / 247 | 20 / 20 / 66 | 0.0282 / 0.2137 / 0.5245 | 0.0282 / 0.2039 / 0.4571 | 0.0383 (one page) |
| parish C | 188 / 209 / 216 | 0 / 0 / 0 | 62 / 69 / 71 | 0.3263 / 0.3892 / 0.5818 | 0.1534 / 0.2633 / 0.4728 | 0.0249 / 0.0356 / 0.0678 |
| printed history | 185 / 197 / 208 | 9 / 10 / 26 | 56 / 62 / 65 | 0.3753 / 0.4722 / 0.5838 | 0.1837 / 0.2404 / 0.4581 | n/a (modal branch) |
| control (7 proxies) | 189 / 196 / 196 | 0 / 0 / 0 | 62 / 65 / 65 | 0.4756 / 0.5218 / 0.6595 | 0.2991 / 0.3391 / 0.3579 | 0.0308 / 0.0549 / 0.0722 |

parish B is the source the floor is for: 10 of its 15 pages have 22 grey levels or
fewer between their two modes and derive the floor margin of 20, so their ink
fraction barely moves. printed history takes the plain modal branch on all 15, so
it has no dark-distribution block or dark-excluded statistic to report; its
whole-page figure is the relevant observed value, and it halves.

**Components at the sealed `gap_tolerance_px = 3`**, over the 114 inferred pages:
**87 / 2,772 / 24,617** against **87 / 6,237 / 24,617** at the fixed margin — the
median more than halves, and the extremes are the two parish B pages that derive the
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
