"""One page, one paper value: the background inference every stage that reads ink runs.

**Three stages threshold the same pixels and until 2026-09-06 two of them
disagreed with the third about what paper is.** The Designator infers a page's
paper value from its own two grey-level population modes, tells a photographed
opening from a dark page, and refuses by name a value that is not a background
of its own page (`config/designator_grouping.toml`'s `[grouping.background]`,
calibrated on 127 real pages). The Ink Map and the Recensor's residual-ink audit
took the page's raw histogram mode instead. On a photographed register opening
that mode is the bezel -- 0 or near it on every one of those 127 pages that
reaches the surround branch -- so `background - 40` was below every 8-bit
sample, the audit counted approximately zero ink over a page full of writing,
and the cross-stage containment pin
(`common/test_designator_recensor_ink_calibration.py`) held vacuously: an empty
set is contained in anything. A coverage audit that passes by construction is
not a second opinion, and a green one on a photographed page was not evidence of
coverage.

So the inference lives here, in `common/`, and all three stages call it on the
same page bytes and get the same background, the same derived ink margin and the
same refusal by name. `pipeline/2_designator/structure.py` re-exports these
names -- it is where they lived until this module existed and where the stage's
own tests and its `conservation.py` reach for them -- and adds nothing of its
own to them.

**What is shared is the background, not the sensitivity.** Each caller keeps its
own margin below that shared value, and they are deliberately different numbers:
the Designator's primary scan runs at the margin `_derived_ink_margin` gives the
page, its conservation reconciles at the far more sensitive
`structure.SECONDARY_MARGIN = 2`, and the audit uses its own
`residual_ink.MINIMUM_CONTRAST_BELOW_BACKGROUND = 40`. Sharing the background is
what makes those three numbers comparable at all; sharing the margin would make
the audit a restatement of the stage it audits.

**This module may not import a stage** (`pipeline/test_stage_import_boundaries.py`),
and it does not: it reads the sealed policy's own bytes and takes everything else
from its arguments. The `[grouping.background]` block stays in
`config/designator_grouping.toml` rather than moving to a shared file, and the
reason is recorded at `load_background_config`.
"""

import re
import tomllib
from pathlib import Path
from typing import Any, Final, TypedDict

from common.contracts.canonical import digest_bytes
from common.contracts.errors import ContractError

# Fraction points below a page's inferred background value, deducted from it to
# get the level at or below which a pixel counts as ink.
#
# **`PRIMARY_MARGIN` is not the margin any scan runs at.** It is the *floor*
# under the margin each page derives for itself (`_derived_ink_margin`), and it
# is the level the background-plausibility probe in `_settle_background_evidence`
# is measured at -- which is the same statement twice, because the floor is the
# most permissive threshold a scan can ever apply. Measured on 127 real pages: 20
# grey levels below the paper *mode* is well inside the paper *population* of a
# photographed page, whose tones spread over dozens of levels, so a fixed 20
# counted a median of 39% of a real page as ink -- 23% in the historical
# dark-excluded derived statistic, which is not a paper-region measurement
# (`DESIGNATOR_SURVEY_2026-09-06.md` sections 5 and 6, and the tables in
# `pipeline/2_designator/HANDOFF.md` under "The ink margin, derived on 127
# pages"). On this repository's synthetic pages the two are indistinguishable:
# paper is one flat tone and ink is another, 140 grey levels below it on both
# pages that carry any, so the floor threshold and the derived one select the
# identical pixel set.
#
# It lives here rather than in `pipeline/2_designator/structure.py` because all
# three stages that threshold ink now reach it through this module, and because
# `common/test_designator_recensor_ink_calibration.py` pins it as a source
# literal against the sealed `max_ink_bp` that was measured at this level. The
# Designator's own `SECONDARY_MARGIN` stays in `structure.py`: it is that
# stage's conservation denominator, nothing here reads it, and the AST pin still
# compares two source literals in two files.
PRIMARY_MARGIN: Final = 20

# The denominator of every basis-point fraction this module is handed. The
# sealed policy states its fractions in the same basis points
# `config/designator_grouping.toml` uses everywhere else; spelling the
# denominator once here keeps the arithmetic in one place.
BASIS_POINTS: Final = 10000


class BackgroundInferenceRefusal(ContractError):
    """This page's background cannot be inferred, so its ink cannot be thresholded.

    Its own kind, rather than a bare `ContractError`, because the caller must be
    able to tell it apart from a corrupt decode. **A page this is raised for is
    still cut and still read.** Tyrel, 2026-08-11: *"Everything gets read every
    time nothing gets pulled out or held"*, and *"missing text is the worst
    failure"*. So this refusal never removes a page from the run — it says only
    that the modal pixel is not paper, so the caller must stop trusting it and
    fall back to cutting predetermined crops instead. A decode error stays fatal;
    this one changes how the page is cut, never whether it is.

    **It also ends this stage's ink measurement for that page, and that is the
    point.** `run.py` used to substitute the page's own mean as a stand-in
    divider so the accounting "had something defensible". It does not: on the
    inverted scan this module's own test uses — 80% of the page at 30, 20% at
    220 — the mean is 68, so the threshold is 48, and every pixel of the *dark
    paper* is classified as ink. Conservation then reports four fifths of a page
    as unclaimed ink and mints a held act over the background. A substituted
    divider is a guess wearing a measurement's name, and GOVERNANCE 10 forbids
    exactly that. The honest report is that this page's ink was not measured.
    """


def _ink_threshold(background: int, margin: int) -> int:
    if not (0 <= background <= 255):
        raise ContractError(f"background value {background} is not an 8-bit sample")
    if margin < 0:
        raise ContractError(f"sensitivity margin {margin} is negative")
    threshold = background - margin
    if threshold < 0:
        raise BackgroundInferenceRefusal(
            f"a background of {background} at a {margin}-point margin implies the threshold "
            f"{threshold}, which is below every 8-bit sample: no pixel could be counted as ink "
            "and a blank result here would be arithmetic rather than a measurement"
        )
    return threshold


def _derived_ink_margin(paper: int, dark_mode: int, ink_margin_bp: int) -> int:
    """How far below this page's paper value its own ink threshold sits.

    **The page supplies the distance; the sealed policy supplies only the
    fraction of it.** A photographed register page's paper is not one tone: it
    is a broad population spread over dozens of grey levels by lighting, page
    curl and the camera's own response, and the modal value is merely that
    population's peak. A fixed offset of 20 below the peak therefore lands
    *inside* the paper, and on 127 real pages it left a **median of 39%** of the
    page below the ink threshold. In that historical 127-page calculation,
    subtracting the measured page-wide dark population from both counts produced
    a **median 23% dark-excluded statistic**. It is not a paper-region or
    ground-truth writing fraction (`DESIGNATOR_SURVEY_2026-09-06.md` section 6,
    and the tables in HANDOFF.md). The distance between
    the page's own two population modes is the scale that fixed offset was
    missing: it is large on a photograph with a black surround and small on a
    flat scan, exactly as the paper's own spread is.

    `paper - dark_mode` is that distance, and both ends are already in hand --
    `infer_background_evidence` computes them to place the interior-mode
    measurement. The
    threshold sits `ink_margin_bp` of the way down from the paper mode toward
    the dark mode. At the sealed 3333 basis points that is one third of the way
    to within a basis point, which puts the threshold two thirds of the way *up*
    from the dark mode -- strictly above the midpoint the dark-distribution
    measurement uses, by very nearly (paper - dark_mode) / 6, which keeps every
    published dark-distribution pixel inside this threshold's ink set.

    **Floored at `PRIMARY_MARGIN`, and the floor is not decoration.** At the
    sealed fraction the floor binds wherever the two modes are 60 grey levels
    apart or fewer; ten of the 127 calibration pages are, and on those ten the
    gap is 22 levels or fewer. A third of that is a threshold so close to the
    paper mode that the page has no separation to measure with. The floor holds
    those pages at exactly the behaviour they had before this unit, and it makes
    the whole change monotone in the
    conservative direction: no page's threshold ever rises, so no page can start
    counting as ink anything it does not count as ink today.

    **What this cannot do: one page, two lightings.** The derivation places the
    threshold for the page's *dominant* paper population, and a frame holding
    two leaves lit differently has two. Measured on real material rather than on
    a shape test: the review proxy `da9e07ec...` of the 127-page calibration is a
    two-leaf opening whose right leaf is genuinely darker than the single value
    inferred for the whole frame, so at that page's own derived threshold the
    left leaf and the covering sheet come out of the ink set correctly and the
    right leaf is counted as ink edge to edge (overlay
    `changed4_proxy_da9e07ec.png` in the session's Designator report). **Nothing
    detects it.** The page infers, reconciles and publishes like any other, no
    bound refuses it, and its own ink fraction is the only place the failure
    shows -- the same shape as the light-surround limit
    `infer_background_evidence` names, one level up. A per-region background is
    the repair and it is a different unit.

    Integers only, floor division, like every other quantity here. `paper >=
    dark_mode` holds by construction -- `dark_mode` is the modal value at or
    below the page's mean and `paper` the modal value at or above it -- so the
    product is never negative; the guard is here because a caller could pass the
    two the other way round and a negative margin would silently *raise* the
    threshold above the paper value.
    """
    if paper < dark_mode:
        raise ContractError(
            f"a paper mode of {paper} below this page's dark mode of {dark_mode} is not a "
            "page's own two populations; the ink margin derived from it would raise the "
            "threshold above the paper value rather than lowering it"
        )
    return max(PRIMARY_MARGIN, (paper - dark_mode) * ink_margin_bp // BASIS_POINTS)


class BackgroundPolicy(TypedDict):
    """The sealed policy the background inference runs under.

    Resolved per page from `config/designator_grouping.toml`'s
    `[grouping.background]` sub-table by
    `grouping_config.resolve_background_policy` -- *not* by
    `resolve_thresholds`, and deliberately not a field of `GroupingThresholds`:
    that dataclass is published verbatim as a page's `resolved_thresholds`, and
    this policy is an input to the inference that runs before any threshold
    touches any geometry. Passed in whole rather than as four loose integers so
    a caller cannot supply three of the four. Every field is an integer;
    `band_px_x` and `band_px_y` are already resolved to this page's own pixels,
    and the two `_bp` fields are basis points (1/10000) of a *population*, not
    of a page dimension.

    Two bounds, not three. `min_border_dark_bp` was here until 2026-09-06 and is
    gone on measurement: over 127 real pages it refused 52 of them and refused
    no control the interior bound did not already refuse (the calibration table
    in `pipeline/2_designator/HANDOFF.md`). `max_ink_bp` replaces it, and it
    asks a different question -- not where this page's dark is, but whether the
    value inferred as paper is a background of this page at all.

    `ink_margin_bp` is the fourth field and the only one that is not a bound. It
    is the fraction -- of the distance between this page's two population modes,
    not of any page dimension -- that `_derived_ink_margin` deducts from the
    paper value to get the threshold the scan applies. It is a *population*
    fraction like the two `_bp` bounds above, which is why it can live in a
    sealed policy at all: an absolute ink offset is refused by
    `grouping_config._FORBIDDEN_NAMES` by name and always will be.
    """

    band_px_x: int
    band_px_y: int
    max_interior_dark_bp: int
    max_ink_bp: int
    ink_margin_bp: int


class DarkDistributionEvidence(TypedDict):
    """Dark-population measurements used by the interior-mode branch.

    The branch may be reached by a photographed frame, but its admission rule
    does not prove one: it accepts when the measured interior dark fraction is
    within the sealed limit. These values therefore record the measured dark
    distribution and the geometry sampled, never a page boundary, bezel count,
    or paper-region count.

    `dark_at_or_below` is the midpoint between the page's dark and light modes,
    capped at the derived ink threshold. Both dark counts are consequently
    subsets of the ink the primary scan counts. `border_dark_pixel_count` is the
    count in the sampled border band and `dark_pixel_count` is the page-wide
    count at that same level; neither identifies which pixels are frame or
    writing without independent ground truth.
    """

    band_px_x: int
    band_px_y: int
    dark_mode: int
    dark_at_or_below: int
    dark_pixel_count: int
    border_dark_pixel_count: int
    border_dark_bp: int
    interior_dark_bp: int


class BackgroundEvidence(TypedDict):
    """This page's paper value, how it was established, and what it will be
    thresholded at.

    `dark_mode` and `ink_margin` are here on both branches.  `dark_distribution`
    is present only when the interior-mode branch measured one; it records the
    branch's dark-population samples without asserting a page boundary. A reader holding `background`, `dark_mode`
    and the sealed `ink_margin_bp` can recompute `ink_margin` exactly, and with
    it the threshold every ink count on this page was taken at. Without them the
    ink fraction of a page would be a number whose divider was inferred and then
    dropped, which is the silent half of GOVERNANCE 2.
    """

    background: int
    source: str
    dark_distribution: DarkDistributionEvidence | None
    dark_mode: int
    ink_margin: int


# The two `source` values `infer_background_evidence` can return. A page that
# reaches neither raises `BackgroundInferenceRefusal` instead, so there is no
# third, quieter outcome.
#
# These are the strings `run.py` publishes as a page's `background_source`,
# spelled here rather than translated there. `inferred-modal` predates this
# module's interior-mode branch and is unchanged, so every existing page record
# still reads exactly as it did; `run.py`'s own third value, `not-inferable`,
# belongs to it rather than here, because it names a refusal this function
# raises and does not return.
BACKGROUND_SOURCE_MODAL: Final = "inferred-modal"
BACKGROUND_SOURCE_INTERIOR_MODE: Final = "inferred-interior-mode"


def _dark_distribution(
    width: int,
    height: int,
    rows: list,
    *,
    level: int,
    dark_mode: int,
    dark_pixel_count: int,
    policy: BackgroundPolicy,
) -> DarkDistributionEvidence | None:
    """Measure this page's dark distribution for the interior-mode branch.

    The interior sample distinguishes the photographed pages and refusing
    controls measured below. It does not establish a physical frame: a spatially
    uniform mixture with 40% dark pixels also passes the existing interior bound.
    The border sample is reported but imposes no enrichment requirement.

    **The level this is measured at is the page's own, and it is not a mode.**
    Until 2026-09-06 `level` was the page's single modal pixel, and that is the
    statistic the 127-page survey broke: a LANCZOS resample smooths a hard black
    spike away, the mode moves off it, and the same page measures differently at
    two sizes (`DESIGNATOR_SURVEY_2026-09-06.md` §7). The level is now the
    integer midpoint between the *dark* population's mode and the *light*
    population's mode -- both taken on the page's own histogram, split at its own
    mean -- so it sits in the valley between the two populations rather than on
    either spike. Over the 73 pages the survey could resample, the paper value
    this produces moves by at most 2 grey levels between a page and its
    300-DPI-equivalent, on 72 of which the accept/refuse outcome is identical.

    **One bound decides, and it is the interior one.** Over 127 real pages the
    interior figure at this level runs 287 to 3452 basis points; the two
    synthetic refusing controls measure 6647 (a dark core inside a light border)
    and 8333 (an inverted scan). The border figure discriminates nothing the
    interior figure does not -- the inverted scan's border is 6578 bp, darker
    than the border band of 9 of the 72 real pages that reach this test -- and
    the border bound that used to sit here refused 52 real pages for it. It is
    measured and published, because it is what makes the block readable, and it
    decides nothing.

    A uniformly dark page never reaches this function at all: its mode equals its
    mean, so the majority-ink branch does not fire and it is refused one branch
    later by the `PRIMARY_MARGIN` guard.

    **This test never removes a pixel from anything.** It decides only which
    value is reported as paper. The surround stays in the page, stays below the
    ink threshold, and is therefore counted as ink by `primary_scan` and
    reconciled as ink by `conservation.reconcile` exactly like any other dark
    pixel. That is deliberate and it is the direction GOALS 1 requires: masking
    the surround out would mean deciding where the page ends, and a page edge
    misjudged by thirty pixels would silently delete a marginal name. Counting
    the bezel as ink is a visible, reconcilable over-count; excluding it is an
    invisible loss.

    **What would otherwise be lost is the interpretation, so that is what is
    recorded.** Without this evidence a reader sees an ink fraction of 0.66 and
    concludes the page is two-thirds written on. `DarkDistributionEvidence` records the sampled dark population, its level,
    and its geometry. It does not classify any of those pixels as a frame or as
    writing; that would require page-boundary ground truth this pass does not
    have.

    Returns `None` when the page has no interior to compare against, or when the
    interior is itself dark — the caller then refuses exactly as before.
    """
    band_x, band_y = policy["band_px_x"], policy["band_px_y"]
    if band_x <= 0 or band_y <= 0 or 2 * band_x >= width or 2 * band_y >= height:
        return None
    # `bytes.translate` maps every sample to 1 (at or below the level) or 0 in
    # C, so this second full-page pass costs a few milliseconds rather than the
    # seconds a per-pixel Python comparison would -- the same reason the
    # labeller stopped walking pixels one at a time.
    table = bytes(1 if value <= level else 0 for value in range(256))
    interior_dark = 0
    for y in range(band_y, height - band_y):
        row = rows[y]
        # Named here rather than left to an `AttributeError` from inside
        # `translate`. The histogram loop above iterates any sequence of ints,
        # so a caller handing this module a list-of-lists page gets that far and
        # then dies with a message naming neither the scanline nor the reason.
        # `conservation._unit_ink_runs` guards the same assumption the same way.
        if not isinstance(row, (bytes, bytearray)):
            raise ContractError(f"scanline {y} is not grayscale bytes")
        interior_dark += row[band_x : width - band_x].translate(table).count(1)
    interior_pixels = (width - 2 * band_x) * (height - 2 * band_y)
    border_pixels = width * height - interior_pixels
    border_dark = dark_pixel_count - interior_dark
    # Floor division, integers only, like every other quantity this module
    # handles. It rounds `interior_dark_bp` down, which is the looser direction
    # against the `<=` bound below; at these population sizes that is one part
    # in ten thousand against a measured valley 3,195 basis points wide.
    border_dark_bp = border_dark * BASIS_POINTS // border_pixels
    interior_dark_bp = interior_dark * BASIS_POINTS // interior_pixels
    if interior_dark_bp > policy["max_interior_dark_bp"]:
        return None
    return {
        "band_px_x": band_x,
        "band_px_y": band_y,
        "dark_mode": dark_mode,
        "dark_at_or_below": level,
        "dark_pixel_count": dark_pixel_count,
        "border_dark_pixel_count": border_dark,
        "border_dark_bp": border_dark_bp,
        "interior_dark_bp": interior_dark_bp,
    }


def infer_background_evidence(
    width: int, height: int, rows: list, *, background_policy: BackgroundPolicy
) -> BackgroundEvidence:
    """The page's own background value, and how it was established.

    A scanned register page is overwhelmingly paper, so the modal pixel value
    is the paper colour under any real lighting or scanner, not a fixed
    constant this stage would otherwise have to assume matches every page. A
    hardcoded background would be exactly the kind of magic number this
    rebuild's audit trail names as a defect class in the old pipeline's
    thresholds; inferring it per page needs no such constant at all.

    **The premise above is a premise, and this function checks it.** Where
    ink is the numeric majority of a page -- a heavily inked page, an inverted
    scan, a photographic negative -- the modal pixel is the *ink* colour. The
    threshold below it then admits almost nothing, the page reconciles to zero
    ink, and the stage exits `complete` having found no acts at all. That is a
    page lost in silence, which is the exact shape GOALS 1 forbids: a missed act
    is worse than a poorly read one, and Tyrel's 2026-08-04 ruling 15 says blank
    is proved and never inferred.

    The check needs no constant either. Paper is the lighter surface, so an
    inferred background must be at least as light as the page's own mean; when
    it is darker than the average pixel, the mode is ink. Compared as
    `mode * count >= total` so the arithmetic stays in integers -- every
    quantity this module handles is an integer, and a float comparison here
    could pass by accident.

    **That comparison alone misses the uniformly dark page**, which is the one
    shape where mode and mean are equal and both wrong. A page of solid black has
    `mode == mean == 0`, so `mode * count >= total` holds exactly and this
    function used to return 0 as the paper colour. `_ink_threshold(0, 20)` is
    then -20, no 8-bit sample can be at or below it, the page counts zero ink
    pixels, and the run exits `complete` over a visibly black page -- the same
    silent loss the majority-ink check exists to stop, reached by the one route
    it does not cover. So a background must also be light enough to express an
    ink threshold at the floor margin `PRIMARY_MARGIN`, which is the least this
    page's own derived margin can be.

    **And the majority-ink test alone was wrong about a photographed page,
    measured on 7 of 7 real ones.** A photograph of a register opening carries a
    black surround around the paper -- 18-26% of the frame on the seven real
    proxies -- and pure black is then by a wide margin the single most common
    value, because the paper itself is spread across dozens of tones in the
    180-240 band. So the modal pixel was 0 on every real page, the majority-ink
    branch refused every one of them, and the live path cut all seven into blind
    fallback slabs with `ink_measurable: false` and never reconciled their ink at
    all (`workbench/active/TIMING_REPORT_2026-09-05.md` §1a). The premise "the
    modal pixel is paper" is sound for a flatbed scan and false for a photograph.

    The repair is one branch, and it is asked only where the old code was about
    to refuse. `_dark_distribution` measures the dark fraction in the interior
    and admits it within the sealed limit. It does not prove a frame or a page
    boundary. On admission, the proposed paper value is the modal pixel **at or
    above the page's own mean**, applying the "paper is the lighter surface"
    premise to that lighter population. The value still faces `PRIMARY_MARGIN`
    and the final ink-fraction guard. The measured inverted-scan and dark-core
    controls still refuse; a uniformly valued dark page refuses at the margin
    guard. Other spatial arrangements are not classified by this evidence.

    **And the whole arrangement above was still wrong in the other direction,
    measured on 127 real pages.** The seven proxies it was calibrated on contain
    no example of the failure, so the calibration could not have found it: on 6
    of 127 pages (4.7%) the modal pixel is 255 -- a blown highlight, a scanner
    mount, a saturated margin -- which is *lighter* than the mean, so the
    majority-ink question is never asked at all, the mode is taken as paper, and
    71-85% of the page is then counted as ink. Every downstream check passes.
    `group_page` finds structure, `conservation.reconcile` balances exactly,
    residual is zero, and the record carries no mark of any kind
    (`DESIGNATOR_SURVEY_2026-09-06.md` §5). A wrong paper value on the modal
    branch is not noisy: it is silent, which is the half of GOVERNANCE 2 that
    costs the most to find later.

    So the inferred value, from whichever branch, faces one last question that
    needs no geometry: **does it leave the page a minority of ink?** A background
    is by definition the surface most of the page is; a value that puts *more
    than* 70% of its own page at or below the ink threshold is not describing
    the page's surface, and the ink fraction it implies would reconcile without
    meaning anything. The bound is `max_ink_bp` in the sealed policy, measured at
    `PRIMARY_MARGIN` -- the floor under the derived margin, which is the most
    permissive threshold this page's scan can ever apply. A page it refuses is
    refused by name, is still cut and still read, and records
    `ink_measurable: false` -- the visible failure GOVERNANCE 10 asks for, in
    place of a number that cannot be read.

    **That probe is measured at the floor rather than at the page's own derived
    threshold, and that is a decision with a measurement behind it.** Asked at
    the derived threshold the bound stops working entirely: the derivation reads
    the same wrong paper value the bound is watching for, moves the threshold
    down with it, and hands back an ink fraction that looks ordinary. Measured
    on the six pages whose modal branch inferred a paper of 255 -- the exact
    silent failure this bound exists to catch -- the whole-page ink figure at
    the derived threshold is 2239 to 3498 basis points against a median of 2434
    over the other 121, completely interleaved, so no value of `max_ink_bp`
    separates them there. At the floor they measure 7077 to 8502 against a
    maximum of 6595 among the pages it admits, exactly as they did before this
    unit. The bound and the derivation ask different questions and must be
    measured at different levels.

    **Two shapes this inference is known to get wrong, and neither is caught.**
    Both are recorded rather than repaired, because a limit nobody has written
    down is the failure GOVERNANCE 2 is about.

    * **A surround within about 15 grey levels of the paper** is inferred *as*
      the paper: the frame wins the mode, clears the majority-ink test, and the
      ink fraction it implies is inside `max_ink_bp`. The consequence is a paper
      value that much too high. Measured on a SYNTHETIC page and pinned by
      `test_structure.py::
      test_a_light_surround_close_to_the_paper_tone_is_not_caught_and_that_is_recorded`,
      which asserts the wrong answer so it cannot change unnoticed.
    * **A frame holding two leaves lit differently gets one paper value**, and
      the darker leaf is then counted as ink edge to edge while the lighter one
      reads correctly. Found on real material -- the review proxy `da9e07ec...`
      of the 127-page calibration, overlay `changed4_proxy_da9e07ec.png` in the
      session's Designator report -- and not on a shape test. See
      `_derived_ink_margin`, which places the threshold for the page's dominant
      paper population and cannot place it for two. A per-region background is
      the repair and it is a different unit.

    In both cases the page infers, reconciles and publishes like any other; the
    page's own ink fraction is the only place either failure shows, and nothing
    in the record marks it as wrong.

    Conservation separately reconciles at the more sensitive `SECONDARY_MARGIN`,
    which is not derived; a page this guard refuses is still cut and read,
    records `ink_measurable: false`, and holds the run rather than reporting a
    measurement it did not make.
    """
    if width <= 0 or height <= 0:
        raise ContractError(f"a {width}x{height} page has no pixels to infer a background from")
    if len(rows) != height:
        raise ContractError(f"expected {height} scanlines, got {len(rows)}")
    histogram = [0] * 256
    for y in range(height):
        row = rows[y]
        if len(row) != width:
            raise ContractError(f"scanline {y} has width {len(row)}, expected {width}")
        for value in row:
            histogram[value] += 1
    background = max(range(256), key=lambda value: histogram[value])
    counted = width * height
    total = sum(value * count for value, count in enumerate(histogram))
    mean = total // counted
    # The page's two population modes, computed on both branches because the
    # derived ink margin needs both on both. The modal value at or above the
    # page's own mean is the paper population's peak, measured on the whole page
    # rather than on the interior alone -- on a photographed page the surround is
    # entirely below the mean, so it cannot contribute a candidate here, and
    # excluding it geometrically would change nothing about this answer while
    # making it depend on the band width. Its mirror at or below the mean is the
    # dark population's own peak.
    #
    # On the plain modal branch `paper` and `background` are the same value: a
    # background that survives `background * counted >= total` is at or above
    # the mean, so the page's global mode is also the mode of the population at
    # or above the mean. Pinned rather than assumed, in
    # `test_the_paper_mode_and_the_modal_background_are_one_value_on_that_branch`.
    paper = max(range(mean, 256), key=lambda value: histogram[value])
    dark_mode = max(range(0, mean + 1), key=lambda value: histogram[value])
    ink_margin = _derived_ink_margin(paper, dark_mode, background_policy["ink_margin_bp"])
    if background * counted < total:
        # The dark-distribution measurement uses the integer midpoint of the two modes,
        # which is a valley rather than either spike -- the whole reason it
        # survives a resample that smooths the black spike away and moves the
        # plain mode off it.
        #
        # Capped at this page's own ink threshold so that every pixel the
        # dark-distribution block counts is a pixel the scan will count as ink -- which is
        # what keeps the recorded dark population a subset of counted ink
        # rather than an arithmetic that happens to work out. With a derived
        # margin the cap is provably slack wherever the derivation is not itself
        # floored: the threshold sits (paper - dark_mode) * (1 - ink_margin_bp
        # /10000) above the dark mode and the midpoint at half of it, so at any
        # `ink_margin_bp` below 5000 the threshold is strictly the higher of the
        # two. So it can bind only where the floor is what the derivation
        # returned *and* the two modes are less than 2 * PRIMARY_MARGIN apart --
        # a page with no contrast to scale by. On the 127 calibration pages it
        # binds on 10, all of them from one source and all with 22 grey levels
        # or fewer between their modes, and it moves the level down, which is
        # the admitting direction. Floored at 0 because a paper value below the
        # margin implies a negative threshold, and this level indexes a
        # histogram: the page it happens on is refused three lines later, but
        # not before this slice is taken.
        level = min((dark_mode + paper) // 2, max(0, paper - ink_margin))
        # Keyword-only past `rows`: `level`, `dark_mode` and the dark pixel
        # count are three integers in a row, and a transposition of any two of
        # them would produce a wrong answer rather than an error.
        dark_distribution = _dark_distribution(
            width,
            height,
            rows,
            level=level,
            dark_mode=dark_mode,
            dark_pixel_count=sum(histogram[: level + 1]),
            policy=background_policy,
        )
        if dark_distribution is not None and paper >= PRIMARY_MARGIN:
            return _settle_background_evidence(
                {
                    "background": paper,
                    "source": BACKGROUND_SOURCE_INTERIOR_MODE,
                    "dark_distribution": dark_distribution,
                    "dark_mode": dark_mode,
                    "ink_margin": ink_margin,
                },
                width=width,
                height=height,
                histogram=histogram,
                counted=counted,
                policy=background_policy,
            )
        raise BackgroundInferenceRefusal(
            f"the most common pixel on this {width}x{height} page is {background}, which is "
            f"darker than its own mean of {mean}: the page is majority ink, so "
            "its background cannot be inferred and a blank result here would be inferred "
            "rather than proved"
            + (
                ""
                if dark_distribution is None
                else f"; a dark distribution was measured ({dark_distribution['border_dark_bp']} bp of the "
                f"border band and {dark_distribution['interior_dark_bp']} bp of the interior at or "
                f"below {level}) but the interior's own paper mode is darker than the "
                f"{PRIMARY_MARGIN}-point ink margin"
            )
        )
    if background < PRIMARY_MARGIN:
        raise BackgroundInferenceRefusal(
            f"the most common pixel on this {width}x{height} page is {background}, which is "
            f"darker than the {PRIMARY_MARGIN}-point ink margin: the threshold it implies is "
            "below every 8-bit sample, so no pixel on this page could ever be counted as ink "
            "and a blank result here would be arithmetic rather than a measurement"
        )
    return _settle_background_evidence(
        {
            "background": background,
            "source": BACKGROUND_SOURCE_MODAL,
            "dark_distribution": None,
            "dark_mode": dark_mode,
            "ink_margin": ink_margin,
        },
        width=width,
        height=height,
        histogram=histogram,
        counted=counted,
        policy=background_policy,
    )


def _settle_background_evidence(
    evidence: BackgroundEvidence,
    *,
    width: int,
    height: int,
    histogram: list[int],
    counted: int,
    policy: BackgroundPolicy,
) -> BackgroundEvidence:
    """The last question, asked of both branches: is this value a background?

    Returns the evidence it was given, unchanged, or raises. Keyword-only past
    the evidence for the same reason `_dark_distribution` is: `width`, `height` and
    `counted` are three integers whose transposition would be silent.

    A background is the surface most of the page is. A value that leaves the
    majority of its own page at or below the ink threshold is not one, whichever
    branch produced it, and the ink fraction it implies reconciles perfectly
    while meaning nothing -- the silent failure `DESIGNATOR_SURVEY_2026-09-06.md`
    §5 found on 6 of 127 pages and the seven-page calibration could not have
    seen.

    **Measured at `PRIMARY_MARGIN`, which is the floor under this page's derived
    margin and therefore the most permissive threshold `primary_scan` could
    apply to it.** Asking it at the page's own derived threshold instead would
    switch the bound off: the derivation takes the same wrong paper value as its
    upper end and slides the threshold down with it, so the six pages this bound
    exists for measure 2239-3498 bp there, inside the ordinary range. See
    `infer_background_evidence` for the measurement. No page is *scanned* at
    `PRIMARY_MARGIN` any more; what the constant is now is the one level every
    page shares, which is exactly what a bound comparing pages across a corpus
    needs and a per-page scan does not.

    A refusal here is the ordinary `BackgroundInferenceRefusal`: the page is
    still cut, still read, and records `ink_measurable: false`. It loses this
    stage's ink accounting on that page, which is a real cost named in
    `BackgroundInferenceRefusal`'s own docstring, and it is the cost GOVERNANCE
    10 prices lower than a measurement that cannot be read.
    """
    threshold = _ink_threshold(evidence["background"], PRIMARY_MARGIN)
    ink_bp = sum(histogram[: threshold + 1]) * BASIS_POINTS // counted
    if ink_bp > policy["max_ink_bp"]:
        raise BackgroundInferenceRefusal(
            f"the value {evidence['background']} inferred as this {width}x{height} page's "
            f"paper ({evidence['source']}) leaves {ink_bp} basis points of the page at or "
            f"below the ink threshold {threshold} it implies, past the "
            f"{policy['max_ink_bp']} this policy admits: a background that is a minority of "
            "its own page is not a measurement of paper, and the ink fraction it implies "
            "would reconcile exactly while meaning nothing"
        )
    return evidence


def infer_background(
    width: int, height: int, rows: list, *, background_policy: BackgroundPolicy
) -> int:
    """`infer_background_evidence`'s background value alone.

    Kept because most callers -- and every test that builds a page to check one
    threshold -- want the integer and nothing else. The evidence function is the
    one `run.py` calls, because a page whose background came from the
    interior-mode branch has a measurement to publish and dropping it would be
    the silent half of GOVERNANCE 2.
    """
    return infer_background_evidence(width, height, rows, background_policy=background_policy)[
        "background"
    ]


#: Names this policy refuses wherever they appear. They are `PRIMARY_MARGIN` and
#: `pipeline/2_designator/structure.SECONDARY_MARGIN` -- absolute 8-bit
#: ink-intensity offsets, not page geometry -- and they stay Python module
#: constants because `common/test_designator_recensor_ink_calibration.py` is an
#: AST pin that reads them as source literals and cross-checks them against the
#: Recensor's own contrast constant. A per-run config value for either would make
#: that cross-stage invariant unenforceable statically.
FORBIDDEN_NAMES: Final = ("primary_margin", "secondary_margin")


def validate_ink_not_measurable_payload(payload: Any) -> dict[str, Any]:
    """Validate the closed Ink Map refusal payload before a consumer drops evidence.

    `ink-not-measurable` is a census record of an unavailable measurement, never
    an empty measured finding. The producer writes exactly these four fields; a
    consumer must refuse an omitted, added, or contradictory field before it
    maps the record to ``None`` or an export row without runs.
    """
    if not isinstance(payload, dict):
        raise ContractError("the ink-not-measurable record has no object payload")
    fields = {
        "page_ordinal",
        "ink_measurable",
        "background_refusal",
        "background_config_sha256",
    }
    if set(payload) != fields:
        raise ContractError(
            "the ink-not-measurable payload is not closed: expected exactly "
            f"{sorted(fields)}, got {sorted(payload)}"
        )
    ordinal = payload["page_ordinal"]
    if not _plain_int(ordinal) or ordinal <= 0:
        raise ContractError(
            "the ink-not-measurable payload page_ordinal is not a positive plain integer"
        )
    if payload["ink_measurable"] is not False:
        raise ContractError("the ink-not-measurable payload ink_measurable is not false")
    refusal = payload["background_refusal"]
    if not isinstance(refusal, str) or not refusal.strip():
        raise ContractError(
            "the ink-not-measurable payload background_refusal is not a non-blank string"
        )
    digest = payload["background_config_sha256"]
    if not isinstance(digest, str) or re.fullmatch(r"[0-9a-f]{64}", digest) is None:
        raise ContractError(
            "the ink-not-measurable payload background_config_sha256 is not lowercase SHA-256 hex"
        )
    return {
        "page_ordinal": ordinal,
        "ink_measurable": False,
        "background_refusal": refusal,
        "background_config_sha256": digest,
    }


def _plain_int(value: Any) -> bool:
    """An `int` that is not a `bool`.

    `bool` is an `int` subclass, so an unqualified `isinstance` reads `True` as
    the basis point `1`. A fifth copy of this two-line predicate rather than an
    import: `geometry`, `grouping.py`, `conservation.py` and
    `pipeline/0_triage/manifest.py` each own theirs for the same reason this
    module owns this one -- `common/` may not import a stage.
    """
    return isinstance(value, int) and not isinstance(value, bool)


def round_half_up_bp(dimension: int, bp: int) -> int:
    """Round-half-up pixel amount for a basis-point fraction of one dimension.

    Pure integer arithmetic, never a float, so the amount actually applied is
    deterministic and independent of Python's float rounding rules.

    This is the project's one basis-point rounding rule.
    `pipeline/2_designator/geometry._pad_amount` was that rule until this module
    existed and now delegates here, so a page's band and a page's padding cannot
    come to round differently.
    """
    return (dimension * bp + BASIS_POINTS // 2) // BASIS_POINTS


# The shared sealed policy remains in the Designator-named configuration while
# its three readers bind the same exact bytes into their records.
DEFAULT_BACKGROUND_CONFIG_PATH: Final = (
    Path(__file__).resolve().parents[1] / "config" / "designator_grouping.toml"
)

# The four basis-point fields of `[grouping.background]`, in file order.
BACKGROUND_BP_FIELDS: Final = (
    "band_bp",
    "max_interior_dark_bp",
    "max_ink_bp",
    "ink_margin_bp",
)


def validate_background_table(
    table: Any, *, where: str = "[grouping.background]"
) -> dict[str, int]:
    """The four sealed values, checked against their bounds and returned.

        The bounds live here rather than in the Designator's own loader because
        three stages now run under this block, and a value one of them would refuse
        is a value all three must. `where` names the table in the refusal because
        the Designator's loader reads it out of a larger file and this module's
        reads it out of that file alone.

    Provenance is the one thing *not* checked here.
        `[grouping.background.provenance]` is validated by
        `pipeline/2_designator/grouping_config._load_provenance` against the same
        closed schema as the rest of that file's provenance blocks, and
        re-implementing that schema here would be a second copy of it. What that
        means concretely is stated rather than left implicit: the Designator refuses
        a run whose calibration block has lost its provenance, and the Ink Map and
        the Recensor do not. That is the one asymmetry between the three readers, and
        it is on the field that records where a number came from rather than on any
        field that decides what a page measures.
    """
    if not isinstance(table, dict):
        raise ContractError(f"the grouping configuration has no {where} table")
    # The closed field set and the two forbidden names are checked here as well
    # as in the Designator's own loader, and not only there, because the Ink Map
    # runs *before* the Designator: a block carrying an unread field, or an
    # absolute ink offset under a name this policy refuses by name, would
    # otherwise publish a whole stage's records before anything caught it. The
    # Designator's loader still refuses first with its own message when it is the
    # reader; these are the same refusals for the two readers that see this block
    # alone.
    forbidden = sorted(name for name in FORBIDDEN_NAMES if name in table)
    if forbidden:
        raise ContractError(
            f"the grouping configuration's {where} carries forbidden field(s) {forbidden}; "
            "primary_margin/secondary_margin are absolute 8-bit ink-intensity offsets pinned "
            "as Python module constants and may never become a per-run config value. What is "
            "sealed instead is ink_margin_bp, the fraction of a page's own two-mode distance "
            "that derives its margin: a population fraction, which scales with the page, and "
            "not an offset"
        )
    unexpected = sorted(set(table) - set(BACKGROUND_BP_FIELDS) - {"provenance"})
    if unexpected:
        raise ContractError(
            f"the grouping configuration's {where} carries unknown field(s) "
            f"{unexpected}; an unread policy field cannot be applied"
        )
    missing = sorted(set(BACKGROUND_BP_FIELDS) - set(table))
    if missing:
        raise ContractError(f"the grouping configuration's {where} is missing field(s) {missing}")
    values = {name: table[name] for name in BACKGROUND_BP_FIELDS}
    for name in ("max_interior_dark_bp", "max_ink_bp"):
        if not _plain_int(values[name]) or not 0 <= values[name] <= BASIS_POINTS:
            raise ContractError(
                f"the grouping configuration's {where} {name} is not a basis-point "
                f"integer in 0..{BASIS_POINTS}"
            )
    # `ink_margin_bp` is bounded strictly below half its range, and the bound is
    # structural rather than a taste. `_dark_distribution` measures at the midpoint
    # of the page's two modes. Its sampled counts must remain subsets of the ink
    # scan, which holds while the derived threshold stays above that midpoint,
    # i.e. while this fraction stays under 5000. At 5000 they coincide; past it
    # the published counts could include pixels the scan does not. Zero is refused for the
    # reason the two bounds above are: a fraction of zero is the derivation
    # switched off by a value rather than by a decision, leaving every page on
    # the floor.
    if (
        not _plain_int(values["ink_margin_bp"])
        or not 0 < values["ink_margin_bp"] < BASIS_POINTS // 2
    ):
        raise ContractError(
            f"the grouping configuration's {where} ink_margin_bp is not a "
            f"basis-point integer strictly between 0 and {BASIS_POINTS // 2}; zero derives "
            "no margin at all and leaves every page on PRIMARY_MARGIN, and half or "
            "more puts this page's ink threshold at or below the level the dark-distribution measurement "
            "measures at, where that test's two dark counts stop being subsets of the ink "
            "they are published as fractions of"
        )
    # A band of zero leaves no border to measure and a band at or over half the
    # page leaves no interior, so both ends are refused rather than silently
    # turning the test off -- `_dark_distribution` would return `None` for either,
    # and a page would then refuse for a reason no config line stated.
    if not _plain_int(values["band_bp"]) or not 0 < values["band_bp"] < BASIS_POINTS // 2:
        raise ContractError(
            f"the grouping configuration's {where} band_bp is not a basis-point "
            f"integer strictly between 0 and {BASIS_POINTS // 2}; a band of zero has no "
            "border to measure and a band of half the page has no interior to compare it against"
        )
    # Both bounds refuse, and a bound at the top of its range refuses nothing.
    # `max_interior_dark_bp = 10000` admits a page every one of whose interior
    # pixels is at or below the level -- an inverted scan and a page of solid
    # dark alike -- and `max_ink_bp = 10000` admits a background that leaves the
    # whole page as ink. Either is the test switched off by a value rather than
    # by a decision, which is the shape this policy refuses everywhere else.
    for name in ("max_interior_dark_bp", "max_ink_bp"):
        if values[name] == BASIS_POINTS:
            raise ContractError(
                f"the grouping configuration's {where} {name} is "
                f"{BASIS_POINTS} basis points, which refuses nothing: a bound at the top of "
                "its own range is the test turned off, and this policy is sealed into a run "
                "as something that decides"
            )
    return values


def load_background_config(
    path: str | Path = DEFAULT_BACKGROUND_CONFIG_PATH,
) -> dict[str, Any]:
    """The sealed background policy and the digest of the bytes it was read from.

    For the two stages that need this block and nothing else around it. The
    Designator reads the same file through
    `pipeline/2_designator/grouping_config.load_grouping_config`, which needs
    every other block in it as well and validates this one through
    `validate_background_table` exactly as this does -- so the three stages
    cannot come to run under different numbers, and each one's record carries
    `config_sha256` over the same bytes (GOVERNANCE 6: a record names what it
    ran under).

    Refused loudly rather than defaulted, matching `load_grouping_config` and
    `geometry.load_padding_config`: a bound silently taken as unlimited would
    change what an audit calls ink with nobody able to point at a config line
    that said so.
    """
    path = Path(path)
    try:
        data = path.read_bytes()
        config = tomllib.loads(data.decode("utf-8"))
    except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError) as error:
        raise ContractError(
            f"the background configuration at {path} could not be read: {error}"
        ) from error
    if not isinstance(config, dict):
        raise ContractError("the background configuration is not a table")
    grouping = config.get("grouping")
    if not isinstance(grouping, dict):
        raise ContractError("the background configuration has no [grouping] table")
    return {
        "config_sha256": digest_bytes(data),
        "background": validate_background_table(grouping.get("background")),
    }


def resolve_background_policy(config: dict[str, Any], width: int, height: int) -> BackgroundPolicy:
    """One page's own resolved background-inference policy.

    Separate from the Designator's `resolve_thresholds` and deliberately *not* a
    field of its `GroupingThresholds`. `pipeline/2_designator/run.py` publishes
    the whole `GroupingThresholds` as a page's `resolved_thresholds`, and this
    policy answers a question asked strictly before that record exists -- the
    background inference runs before any threshold is applied to any geometry.
    Folding it in would put a background-inference input into the structure
    pass's published geometry and move every existing page record's bytes for a
    value that pass never used.

    `config` is either a whole loaded grouping config or this module's own
    `load_background_config` result: both carry the sealed block under
    `background`, which is what keeps one resolver for three stages.

    Both bands resolve through `round_half_up_bp`, the one rounding rule.
    """
    if not _plain_int(width) or not _plain_int(height) or width <= 0 or height <= 0:
        raise ContractError(f"page {width}x{height} does not have positive integer dimensions")
    background = config["background"]
    return {
        "band_px_x": round_half_up_bp(width, background["band_bp"]),
        "band_px_y": round_half_up_bp(height, background["band_bp"]),
        "max_interior_dark_bp": background["max_interior_dark_bp"],
        "max_ink_bp": background["max_ink_bp"],
        "ink_margin_bp": background["ink_margin_bp"],
    }
