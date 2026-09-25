"""One page, one paper value: the background inference every stage that reads ink runs.

Three stages threshold the same pixels and used to disagree about what paper
is: the Designator infers paper from a page's own two grey-level population
modes, while the Ink Map and Recensor took the raw histogram mode instead --
on a photographed opening that mode is the bezel, near-zero, so their audit
counted approximately zero ink over a page full of writing and the cross-stage
containment pin held vacuously (an empty set is contained in anything).

So the inference lives here, and all three stages get the same background, the
same derived ink margin, and the same refusal by name. What is shared is the
background, not the sensitivity: each caller keeps its own margin below it
(the Designator's derived margin, its `SECONDARY_MARGIN = 2`, the audit's
`MINIMUM_CONTRAST_BELOW_BACKGROUND = 40`) so the three numbers stay comparable
without the audit restating the stage it audits.

This module may not import a stage (`pipeline/test_stage_import_boundaries.py`):
it reads the sealed policy's own bytes and takes everything else as arguments.
"""

import re
import tomllib
from pathlib import Path
from typing import Any, Final, TypedDict

from common.contracts.canonical import digest_bytes, is_plain_int
from common.contracts.errors import ContractError

# Fraction points below a page's inferred background value, deducted from it to
# get the level at or below which a pixel counts as ink.
#
# Not the margin any scan runs at: it is the *floor* under the margin each page
# derives for itself (`_derived_ink_margin`), and the level
# `_settle_background_evidence`'s plausibility probe is measured at, since the
# floor is the most permissive threshold a scan can ever apply. Lives here
# rather than in `structure.py` because all three ink-thresholding stages now
# reach it through this module, and because the AST pin in
# `common/test_designator_recensor_ink_calibration.py` reads it as a source
# literal against the sealed `max_ink_bp` measured at this level.
PRIMARY_MARGIN: Final = 20

# Deliberately not derived or configured: a fixed 2 below background is
# smaller than any derived margin, so the Designator's secondary scan and
# conservation, and the Perlector's page-fallback reader, are strictly more
# sensitive than the primary scan on every page, never the reverse. A derived
# value could invert that on some page, trading a visible over-count for a
# possible silent loss.
SECONDARY_MARGIN: Final = 2

# The denominator of every basis-point fraction this module is handed. The
# sealed policy states its fractions in the same basis points
# `config/designator_grouping.toml` uses everywhere else; spelling the
# denominator once here keeps the arithmetic in one place.
BASIS_POINTS: Final = 10000


class BackgroundInferenceRefusal(ContractError):
    """This page's background cannot be inferred, so its ink cannot be thresholded.

    Its own kind, rather than a bare `ContractError`, so a caller can tell it
    apart from a corrupt decode. A page this is raised for is still cut and
    still read -- it changes how the page is cut, never whether it is -- but it
    ends this stage's ink measurement for that page: substituting a stand-in
    divider (the page's own mean) is a guess wearing a measurement's name,
    which principle 8 forbids.
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

    The page supplies the distance (`paper - dark_mode`, its own two population
    modes); the sealed policy supplies only the fraction of it, `ink_margin_bp`.
    A fixed offset instead would land inside the paper on a photograph (its
    tones spread over dozens of levels) while being right for a flat scan --
    the distance between the two modes is the missing scale, large on a
    photograph with a black surround and small on a flat page.

    Floored at `PRIMARY_MARGIN`: below a gap between the two modes that the
    floor divides to less than the floor itself -- 60 grey levels, only at the
    sealed `ink_margin_bp = 3333` -- the page has too little separation to
    derive a margin from, and the floor holds those pages at their
    pre-existing behaviour, keeping the change monotone (no page's threshold
    ever rises).

    What this cannot do: a frame holding two leaves lit differently has two
    dominant paper populations, and this places the threshold for only one --
    known from real material (proxy `da9e07ec...` of the 127-page calibration)
    and undetected by any bound; a per-region background is the repair, and a
    different unit.

    `paper >= dark_mode` holds by construction, but the guard exists because a
    caller could pass the two swapped, which would silently raise the threshold
    above the paper value instead of lowering it.
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

    Resolved per page, and deliberately not a field of `GroupingThresholds`:
    this policy is an input to the inference that runs before any threshold
    touches any geometry. Passed in whole, not as four loose integers, so a
    caller cannot supply three of the four. `band_px_x`/`band_px_y` are already
    resolved to this page's own pixels; the `_bp` fields are basis points of a
    *population*, not a page dimension -- which is why `ink_margin_bp` can live
    in a sealed policy at all, unlike an absolute ink offset.
    """

    band_px_x: int
    band_px_y: int
    max_interior_dark_bp: int
    max_ink_bp: int
    ink_margin_bp: int


class DarkDistributionEvidence(TypedDict):
    """Dark-population measurements used by the interior-mode branch.

    The branch's admission rule accepts when the measured interior dark
    fraction is within the sealed limit; it does not prove a physical frame.
    These values record the measured distribution and sampled geometry, never
    a page boundary, bezel count, or paper-region count. `dark_at_or_below` is
    the midpoint between the dark and light modes, capped at the derived ink
    threshold, so both dark counts stay subsets of the ink the primary scan
    counts.
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

    `dark_mode` and `ink_margin` are here on both branches; `dark_distribution`
    only when the interior-mode branch measured one. A reader holding
    `background`, `dark_mode` and the sealed `ink_margin_bp` can recompute the
    threshold every ink count on this page was taken at -- omitting them would
    make the ink fraction a number whose divider was inferred and then dropped.
    """

    background: int
    source: str
    dark_distribution: DarkDistributionEvidence | None
    dark_mode: int
    ink_margin: int


# The two `source` values `infer_background_evidence` can return; a page that
# reaches neither raises `BackgroundInferenceRefusal` instead. Spelled here
# rather than translated in `run.py`, which publishes them as `background_source`.
# Neither string may be renamed: existing records carry it as published.
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

    The interior sample distinguishes the photographed pages from the refusing
    controls measured in the sealed policy's own calibration caveat. It does
    not establish a physical frame: a spatially uniform mixture with 40% dark
    pixels also passes the existing interior bound.
    The border sample is reported but imposes no enrichment requirement.

    The level is the integer midpoint between the dark and light population
    modes (not either mode itself), so it sits in the valley between them
    rather than on a spike a resample could smooth away and move.

    Only the interior fraction decides admission; the border fraction is
    measured and published but decides nothing, since it discriminates less
    than the interior figure does (a border bound here used to refuse many
    real pages the interior bound did not).

    This test never removes a pixel: it only decides which value is reported
    as paper. The surround stays below the ink threshold and is counted and
    reconciled as ink like any other dark pixel -- deliberately, since masking
    it out would mean deciding where the page ends, and a misjudged edge would
    silently delete a marginal name. Counting the bezel as ink is a visible,
    reconcilable over-count; excluding it would be an invisible loss.
    `DarkDistributionEvidence` records the sampled population and geometry
    without classifying any pixel as frame or writing, which would need
    page-boundary ground truth this pass does not have.

    Returns `None` when the page has no interior to compare against, or the
    interior is itself dark; the caller then refuses as before.
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
        if not isinstance(row, (bytes, bytearray)):
            raise ContractError(f"scanline {y} is not grayscale bytes")
        interior_dark += row[band_x : width - band_x].translate(table).count(1)
    interior_pixels = (width - 2 * band_x) * (height - 2 * band_y)
    border_pixels = width * height - interior_pixels
    border_dark = dark_pixel_count - interior_dark
    # Floor division rounds `interior_dark_bp` down, the looser direction
    # against the `<=` bound below.
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
    is the paper colour under any real lighting or scanner -- no hardcoded
    constant needed, and none assumed to match every page.

    That premise is checked, not assumed. Where ink is the numeric majority
    (a heavily inked page, an inverted scan, a negative) the modal pixel is
    ink, not paper: paper must be at least as light as the page's own mean
    (`mode * count >= total`, kept in integers to avoid a float passing by
    accident), and light enough to express a threshold at the floor margin
    `PRIMARY_MARGIN` -- a uniformly dark page has mode == mean and is wrong on
    both counts, so it needs this second check too.

    The majority-ink test alone is also wrong for a photograph, measured on 7
    of 7 real proxies: a black surround (18-26% of the frame) makes pure black
    the modal pixel even though the paper itself measures in the 180-240
    band, so every one of those pages was refused and fell back to a blind,
    unreconciled crop. The repair, `_dark_distribution`, measures the interior
    dark fraction and, if it is within the sealed limit, takes the modal pixel
    at or above the page's own mean as paper instead -- without proving a
    frame or page boundary, and still subject to `PRIMARY_MARGIN` and the
    final ink-fraction guard below.

    That arrangement was in turn wrong in the other direction on 6 of 127 real
    pages (not represented in the 7-proxy calibration): a blown highlight or
    scanner mount put the modal pixel at 255, lighter than the mean, so the
    majority-ink question was never asked, that value was taken as paper, and
    71-85% of the page silently counted as ink with every downstream check
    reconciling exactly. So the inferred value, from either branch, faces one
    more question needing no geometry: does it leave the page a *minority* of
    ink? The bound is `max_ink_bp`, measured at `PRIMARY_MARGIN` rather than
    the page's own derived threshold, because the derivation would otherwise
    read the same wrong paper value and slide the threshold down with it,
    hiding exactly the failure this bound exists to catch (measured: those six
    pages are indistinguishable from ordinary ones at the derived threshold,
    but far outside the range at the floor). A page this refuses is refused by
    name, still cut and read, and records `ink_measurable: false`.

    Two shapes are known to be wrong and neither is caught, recorded rather
    than repaired: a surround within ~15 grey levels of the paper is inferred
    as the paper (pinned by
    `test_a_light_surround_close_to_the_paper_tone_is_not_caught_and_that_is_recorded`,
    which asserts the wrong answer so it cannot change unnoticed); and a frame
    holding two differently-lit
    leaves gets one paper value, so the darker leaf reads as ink edge to edge
    (found on real material, proxy `da9e07ec...`; `_derived_ink_margin` can
    only place a threshold for one dominant population). A per-region
    background is the repair, and a different unit.

    Conservation separately reconciles at the more sensitive, non-derived
    `SECONDARY_MARGIN`; a page `_ink_threshold` refuses at that margin is
    likewise still cut and read, and holds the run rather than reporting an
    unmade measurement.
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
    # The page's two population modes, needed by both branches: the modal
    # value at or above the mean is the paper population's peak (measured on
    # the whole page since a photographed surround is entirely below the
    # mean), and its mirror below the mean is the dark population's peak. On
    # the plain modal branch `paper` and `background` are the same value,
    # asserted by `test_the_paper_mode_and_the_modal_background_are_one_value_on_that_branch`.
    paper = max(range(mean, 256), key=lambda value: histogram[value])
    dark_mode = max(range(0, mean + 1), key=lambda value: histogram[value])
    ink_margin = _derived_ink_margin(paper, dark_mode, background_policy["ink_margin_bp"])
    if background * counted < total:
        # Midpoint of the two modes, capped at this page's own ink threshold
        # so the recorded dark population stays a subset of counted ink.
        # Floored at 0 since a paper value below the margin implies a negative
        # threshold and this level indexes a histogram; that page is refused
        # three lines later regardless.
        level = min((dark_mode + paper) // 2, max(0, paper - ink_margin))
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

    Returns the evidence unchanged, or raises. A background is the surface
    most of the page is; a value leaving the majority at or below the ink
    threshold is not one, whichever branch produced it -- the silent failure
    found on 6 of 127 pages.

    Measured at `PRIMARY_MARGIN`, the floor under this page's derived margin,
    rather than at the page's own derived threshold: the derivation would
    otherwise take the same wrong paper value and slide the threshold down
    with it, hiding exactly the failure this bound exists to catch. No page is
    *scanned* at `PRIMARY_MARGIN`; it is now only the one level every page
    shares, which a corpus-wide bound needs and a per-page scan does not.

    A refusal here is the ordinary `BackgroundInferenceRefusal`: still cut and
    read, `ink_measurable: false`.
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
    the silent half of principle 2.
    """
    return infer_background_evidence(width, height, rows, background_policy=background_policy)[
        "background"
    ]


#: Names this policy refuses wherever they appear: `PRIMARY_MARGIN` and
#: `SECONDARY_MARGIN`, absolute 8-bit offsets an AST pin in
#: `common/test_designator_recensor_ink_calibration.py` reads as source
#: literals, which a per-run config value would make unenforceable statically.
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
    if not is_plain_int(ordinal) or ordinal <= 0:
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


def validate_measured_ink_map_payload(payload: Any, *, audit_contrast: int) -> dict[str, Any]:
    """Validate the closed current Ink Map measurement before consuming its runs.

    The retained ``ink-runs.v2`` codec records audited ink after the named
    page-spanning component is removed, but its run geometry alone cannot
    establish the background predicate that selected those pixels. This
    envelope does: it names the page's complete audit background and the sealed
    grouping bytes that selected it.
    """
    if not isinstance(payload, dict):
        raise ContractError("the measured ink-map record has no object payload")
    fields = {"page_ordinal", "ink_measurable", "background", "ink", "edge", "edge_findings"}
    if set(payload) != fields:
        raise ContractError(
            "the measured ink-map payload is not closed: expected exactly "
            "page_ordinal, ink_measurable, background, ink, edge, and edge_findings"
        )
    ordinal = payload["page_ordinal"]
    if not is_plain_int(ordinal) or ordinal <= 0:
        raise ContractError(
            "the measured ink-map payload page_ordinal is not a positive plain integer"
        )
    if payload["ink_measurable"] is not True:
        raise ContractError("the measured ink-map payload ink_measurable is not true")
    if not isinstance(payload["ink"], dict) or not isinstance(payload["edge"], dict):
        raise ContractError("the measured ink-map payload has no object ink and edge findings")
    if not isinstance(payload["edge_findings"], dict):
        raise ContractError("the measured ink-map payload has no object retained edge findings")
    background = payload["background"]
    if not isinstance(background, dict):
        raise ContractError("the measured ink-map payload has no object background evidence")
    background_fields = {
        "background_level",
        "background_source",
        "dark_mode",
        "ink_margin",
        "contrast_below_background",
        "ink_threshold",
        "config_sha256",
    }
    if set(background) != background_fields:
        raise ContractError("the measured ink-map background evidence is not closed")
    for field in (
        "background_level",
        "dark_mode",
        "ink_margin",
        "contrast_below_background",
        "ink_threshold",
    ):
        if not is_plain_int(background[field]):
            raise ContractError(f"the measured ink-map background {field} is not a plain integer")
    if not 0 <= background["background_level"] <= 255 or not 0 <= background["dark_mode"] <= 255:
        raise ContractError("the measured ink-map background levels are outside 8-bit range")
    if not PRIMARY_MARGIN <= background["ink_margin"] <= 255:
        raise ContractError("the measured ink-map background ink_margin is outside its domain")
    if background["dark_mode"] > background["background_level"]:
        raise ContractError("the measured ink-map background dark_mode exceeds its paper level")
    if not isinstance(background["background_source"], str) or background[
        "background_source"
    ] not in {
        BACKGROUND_SOURCE_MODAL,
        BACKGROUND_SOURCE_INTERIOR_MODE,
    }:
        raise ContractError("the measured ink-map background_source is unknown")
    if background["contrast_below_background"] != audit_contrast:
        raise ContractError("the measured ink-map background has the wrong audit contrast")
    if background["ink_threshold"] != background["background_level"] - audit_contrast:
        raise ContractError("the measured ink-map background ink_threshold is inconsistent")
    if not 0 <= background["ink_threshold"] <= 255:
        raise ContractError("the measured ink-map background ink_threshold is outside 8-bit range")
    digest = background["config_sha256"]
    if not isinstance(digest, str) or re.fullmatch(r"[0-9a-f]{64}", digest) is None:
        raise ContractError(
            "the measured ink-map background config_sha256 is not lowercase SHA-256 hex"
        )
    return {"page_ordinal": ordinal, "background_config_sha256": digest}


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

    Bounds live here, not only in the Designator's own loader, because the Ink
    Map runs *before* the Designator and would otherwise publish a whole
    stage's records under an unread or forbidden field before anything caught
    it. Provenance is not checked here: `_load_provenance` validates it
    against the same schema, and the Designator refuses a run whose block has
    lost it while the Ink Map and Recensor do not -- the one asymmetry between
    the three readers, on the field recording where a number came from rather
    than one deciding what a page measures.
    """
    if not isinstance(table, dict):
        raise ContractError(f"the grouping configuration has no {where} table")
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
        if not is_plain_int(values[name]) or not 0 <= values[name] <= BASIS_POINTS:
            raise ContractError(
                f"the grouping configuration's {where} {name} is not a basis-point "
                f"integer in 0..{BASIS_POINTS}"
            )
    # Structural, not a taste: `_dark_distribution` measures at the midpoint of
    # the two modes, and its sampled counts stay subsets of the ink scan only
    # while the derived threshold is above that midpoint, i.e. this fraction
    # stays under 5000. Zero is refused because it derives no margin at all,
    # leaving every page on the floor.
    if (
        not is_plain_int(values["ink_margin_bp"])
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
    if not is_plain_int(values["band_bp"]) or not 0 < values["band_bp"] < BASIS_POINTS // 2:
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

    For the two stages that need this block and nothing else around it; the
    Designator reads the same file through `load_grouping_config` and
    validates this block the same way, so the three stages cannot come to run
    under different numbers. Refused loudly rather than defaulted: a bound
    silently taken as unlimited would change what an audit calls ink with
    nobody able to point at a config line that said so.
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

    Separate from the Designator's `resolve_thresholds` and deliberately not a
    field of `GroupingThresholds`: background inference runs before any
    threshold touches any geometry, so folding it in would move every existing
    page record's published bytes for a value that pass never used.

    `config` is either a whole loaded grouping config or this module's own
    `load_background_config` result, since both carry the sealed block under
    `background` -- which is what keeps one resolver for three stages.
    """
    if not is_plain_int(width) or not is_plain_int(height) or width <= 0 or height <= 0:
        raise ContractError(f"page {width}x{height} does not have positive integer dimensions")
    background = config["background"]
    return {
        "band_px_x": round_half_up_bp(width, background["band_bp"]),
        "band_px_y": round_half_up_bp(height, background["band_bp"]),
        "max_interior_dark_bp": background["max_interior_dark_bp"],
        "max_ink_bp": background["max_ink_bp"],
        "ink_margin_bp": background["ink_margin_bp"],
    }
