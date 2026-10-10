# Ink map: calibration record

How the numbers in `config/ink_map.toml`'s `[background]` block were measured and why each
sits where it does. The stage's contract is in [CONTRACT.md](CONTRACT.md); each number's
sample and what it does not establish are also in the config's provenance blocks. Source
names are neutral labels (parish A, B, C; a printed parish history).

## What the background inference must survive

`common.background.infer_background_evidence` assumes a scanned register page is mostly
paper, so its modal pixel is the paper colour. Four page shapes break that, and each is
handled:

1. **Ink is the majority** (heavy staining, bleed-through, an inverted or under-exposed
   scan): the mode is darker than the mean, caught by `mode * count >= total`.
2. **A uniformly dark page**: `mode == mean` passes that check, so the mode must also be
   light enough to express an ink threshold at all (`mode >= PRIMARY_MARGIN`). Without it a
   solid black page would infer background 0 and reconcile to zero ink.
3. **A photograph, not a scan**: a black surround (18 to 26% of the frame on the measured
   proxies) makes pure black the most common value, since the paper spreads over dozens of
   tones. `common.background._dark_distribution` measures the dark fraction of a fixed
   interior sample; within the sealed bound, the paper value is the modal pixel at or above
   the page's mean. This admits photographed pages and still refuses the inverted and
   dark-core controls, but it does not classify a frame, bezel or page boundary.
4. **A blown highlight** (a modal pixel of 255 from a saturated margin or mount): 255 is
   taken as paper and 71 to 85% of the page counts as ink, with every count balancing. So
   both branches also ask whether the inferred value leaves ink a minority: a value that puts
   more than `max_ink_bp` of its own page below its threshold is refused by name
   (`paper-is-not-a-background`).

**The dark distribution is measured, never removed.** Every sampled dark pixel stays on the
page, below the ink threshold, and counted as ink. Masking a population out would mean
deciding where the page ends, and a misjudged edge would silently delete a marginal name.
The `dark_distribution` block records `border_dark_pixel_count` (fixed border band) and
`dark_pixel_count` (whole page) at the selected level: observed counts, not bounds on a bezel
or paper region.

## The background inference, calibrated on 127 pages

The record `[background.provenance]`'s `source` points at. Sample: 60 RecordGold pages, 60
parish master pages and seven review proxies as a control, drawn by the fixed seed
`designator-survey-2026-09-06`, read where they lie and never copied into this tree. Rows
marked **SYNTHETIC** are shape tests from `common/test_background_components.py`.

| Lever | Value | Why |
|---|---:|---|
| `band_bp` | 500 | 250, 500 and 1000 give identical outcomes and paper values on all 127 pages. At 1000 the band is 36% of the page's area and averages bezel against paper (the border figure's median collapses from 8166 to 5025); at 250 the dark-core SYNTHETIC control measures 5990, too close to the interior bound |
| `max_interior_dark_bp` | 5000 | sits in a measured 3,195 bp valley (below), 49 bp under its midpoint. 3000 refuses 3 real pages for nothing; 6000 would admit the dark-core control at a 250 bp band |
| `max_ink_bp` | 7000 | a **policy** bound in a continuum, placed in the widest gap near the top (6595 → 7077) and 77 bp nearer the refusing side. 8000 admits 5 of the 6 silent paper-255 pages. The loader refuses 10000 (the bound switched off) |

There is no border-darkness bound: it refused 52 of 127 pages while refusing no control the
interior bound does not.

**The interior statistic**, at the level midway between the page's dark and light modes, over
the 72 pages that reach the surround test:

| Band | Real pages (min / median / max) | Dark-core control | Inverted-scan control | Valley |
|---:|---|---:|---:|---|
| 250 bp | 409 / 1534 / 3923 | 5990 | 8170 | 2,067 bp |
| **500 bp** | **287 / 955 / 3452** | **6647** | **8333** | **3,195 bp** |
| 1000 bp | 233 / 790 / 2886 | 8413 | 8750 | 5,527 bp |

The border-band figure at the same level is published but decides nothing: real pages run
4852 / 8166 / 10000 at 500 bp, and the inverted-scan control (6578) is darker than 9 of
them, so no bound on it can separate the two.

**The whole-page ink statistic** at `background - PRIMARY_MARGIN`, over all 127: 281 / 4052 /
8502 bp (min / median / max), a continuum with no gap wider than 505 bp, so `max_ink_bp` is
placed, not derived.

**Outcome:**

| Source | Pages | Inferred | `inferred-modal` | `inferred-interior-mode` | Refused |
|---|---:|---:|---:|---:|---:|
| teklia_dai_cretdhi | 24 | 21 | 9 | 12 | 3 |
| recordgold_production_train_v1 | 32 | 29 | 11 | 18 | 3 |
| recordgold_evaluation_val_v1 | 4 | 4 | 0 | 4 | 0 |
| parish A | 15 | 13 | 0 | 13 | 2 |
| parish B | 15 | 15 | 14 | 1 | 0 |
| parish C | 15 | 13 | 0 | 13 | 2 |
| printed history | 15 | 15 | 15 | 0 | 0 |
| control (7 proxies) | 7 | 4 | 0 | 4 | 3 |
| **all** | **127** | **114** | **49** | **65** | **13 (10.2%)** |

Every refusal is `max_ink_bp`: a page whose inferred paper was not a background.

**Scale invariance, measured.** Over the 73 pages with a DPI tag, resampled to a 300-DPI
equivalent with LANCZOS, the outcome is unchanged on 72 and the inferred paper moves by at
most 2 grey levels (median 0). The measured level is a valley between two modes, which a
resample does not move the way it moves a single modal spike.

## The ink margin, derived on 127 pages

`[background] ink_margin_bp = 3333`. A page's ink threshold is
`background - max(PRIMARY_MARGIN, (paper_mode - dark_mode) * 3333 // 10000)`
(`background._derived_ink_margin`), measured on the same 127 pages.

**Why a derived margin.** A photographed leaf's paper is a population spread over dozens of
grey levels; the mode is its peak, not its edge, so a fixed offset of 20 lands inside the
paper (a median of 39% of the page counted as ink).

**Why a fraction of the two modes, not a valley.** On every photographed page the histogram
has exactly two peaks, bezel and paper, with a smooth ramp between them where the writing
lives. The density minimum sits just above the bezel (grey level 55-75), which is a
page-boundary threshold, not an ink one. The distance between the two modes does scale with
the paper population's spread, so a fixed fraction of it tracks the paper's width.

**Why 3333.** Over the ten photographed pages of the margin sweep, the ink fraction left
after subtracting the sampled whole-page dark count (a descriptive statistic, not a
writing measurement) runs 4.1-13.6% at 2500, 2.9-9.0% at 3000, **2.2-6.8% at 3333**,
1.6-4.2% at 3750 and 1.2-3.1% at 4000. The median component count at the sealed
`gap_tolerance_px = 3` is 2721 / 2557 / **2653** / 2982 / 3197 across the same values: flat
through 3000-3333, rising past it as fragmentation begins. 3000 and 3333 are not separated
by either measurement; 3333 is one third.

**The floor is `PRIMARY_MARGIN` (20) and it is load-bearing.** It binds where the two modes
are 60 grey levels apart or fewer (ten pages, all parish B, with 22 or fewer). It also makes
the derivation one-directional: no threshold rises, so nothing becomes ink that was not
before.

**The loader refuses `ink_margin_bp` at or past 5000, and 0.** `_dark_distribution`
measures at the midpoint of the two modes and publishes its counts as fractions of the ink
the page counts; that holds only while the derived threshold stays at or above the midpoint,
which is while the fraction stays under 5000. Zero is the derivation switched off.

**`max_ink_bp` is probed at the floor, not at the derived threshold.** At the derived
threshold the derivation slides with the same wrong paper value the bound is watching for:
the six paper-255 pages measure 2239-3498 bp there, interleaved with the others (median
2434). At the floor they measure 7077-8502 against a maximum of 6595 among admitted pages.
So the refusal outcome is the same 13 pages either way.

**Scale invariance, measured.** Over the same 73 resampled pages the derived margin moves by
at most 8 grey levels (median 0).

**The 127 pages under the derived margin:**

| Statistic | min | p25 | median | p75 | p90 | max |
|---|---:|---:|---:|---:|---:|---:|
| whole-page ink fraction | 0.0282 | 0.2097 | 0.2421 | 0.2752 | 0.3470 | 0.4728 |
| dark-excluded ink fraction (descriptive) | 0.0045 | 0.0301 | 0.0369 | 0.0503 | 0.0678 | 0.0962 |

Components at the sealed gap, over the 114 inferred pages: 87 / 2,772 / 24,617 (min / median
/ max).

## Known limit: two differently lit leaves

A frame holding two leaves lit differently gets one paper value, and the darker leaf is
counted as ink edge to edge (one review proxy shows it). Nothing detects the case: the page
infers, reconciles and publishes like any other, and only its ink fraction shows it. A
per-region background is the repair. Likewise, a light surround within about 15 grey levels
of the paper is inferred as the paper (a surround at 230 or above is refused by
`max_ink_bp`).
