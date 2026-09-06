"""Ink connected-component scanning: the walking skeleton's structure pass.

**The background inference this pass thresholds against is no longer here.** It
moved to `common/background.py` on 2026-09-06 so that the Ink Map and the
Recensor's residual-ink audit infer the same paper value on the same page bytes
that this stage does; the names are re-exported at the top of this file, and
`common/background.py`'s own docstring carries the reason and the measurement.
What stays here is the scanning: the ink set, the labeller, the two sensitivity
presets and the two scans.

A real structure-pass model reads a page once and yields regions plus a
structural classification (ARCHITECTURE: "it may use textual as well as visual
cues"). This walking skeleton has no model to call -- constraint: no pod, no
model call, synthetic pages only -- so its structure pass is a real,
deterministic, independently-checkable *visual* pass: it finds every
ink-bearing connected component on the decoded page and reports its geometry.
What is absent is the textual half of that sentence and the model that would
supply it — not the pass, which runs for real on every page.

Two independent sensitivity presets exist, `PRIMARY` and `SECONDARY`, so that
wiring in a genuinely different secondary detector (`run.py`'s
`secondary_provenance`) has a real, principled difference to report rather
than a second call to the same function: `SECONDARY` is more sensitive, so it
may find faint ink `PRIMARY` misses, matching the P0-incident-shaped rule this
stage is built against -- a secondary proposer adds recall, and only recall.

Connectivity is tolerant of a small gap, not strict pixel adjacency. Real ink
is not a solid fill -- pen strokes, serifs and letterforms leave gaps a strict
flood fill would report as separate marks -- so two ink pixels separated by up
to `gap_tolerance_px` empty pixels are treated as one component (a tolerance
of 0 still reaches an immediately adjacent pixel). This is an ordinary
morphological "close" before labeling, not a fixture-specific hack; it is what
keeps one word from scanning as a dozen one-pixel islands.

**The substitution boundary for real pages was explicit, and half of it has
now been crossed on measurement.** `label_components` is the row-run
implementation `conservation.py` uses (U13), because the retired per-pixel
set/union-find version measured **383 s and 2.17 GB for one 8.7-megapixel
photographed page** at the sealed `gap_tolerance_px = 3`, against 0.41 s for
`conservation.reconcile` labelling *more* ink on the same page
(`workbench/active/TIMING_REPORT_2026-09-05.md` §1b, §1e). The retired
implementation stays here as `_label_components_reference`, the oracle both
this module's and `conservation.py`'s labelling are checked against.

**`ink_pixels` was deliberately not substituted with it.** The docstring used
to instruct replacing the two "as a pair", to protect the threshold and
connectivity contract they share; the measurement says the pair is not the unit
of the decision. `ink_pixels` is 1.45 s at 8.7 megapixels — 0.37% of the pass —
and `label_components` was 99.6% of it, so a paired replacement would have given
up 1.5 s to save 390. The shared contract is honoured by a
`label_components`-only substitution, which is what this is. What `ink_pixels`
does still cost is *memory*: it materialises one Python tuple per ink pixel, and
that is the remaining share of the 2.17 GB. Substituting it is a real piece of
work, unmeasured here beyond that sentence, and named rather than deferred
silently.
"""

from typing import Final

from common.background import (  # noqa: F401  (re-exported: see the note below)
    BACKGROUND_SOURCE_INTERIOR_MODE,
    BACKGROUND_SOURCE_MODAL,
    BASIS_POINTS,
    PRIMARY_MARGIN,
    BackgroundEvidence,
    BackgroundInferenceRefusal,
    BackgroundPolicy,
    SurroundEvidence,
    _dark_surround,
    _derived_ink_margin,
    _ink_threshold,
    _settle_background_evidence,
    infer_background,
    infer_background_evidence,
)
from common.components import (  # noqa: F401  (re-exported: see the note below)
    Component,
    ink_runs_by_row,
    label_component_runs,
    label_components,
    label_components_reference,
)
from common.contracts.errors import ContractError

# The background inference this scan runs on top of lives in `common/background.py`
# and is re-exported here. It moved on 2026-09-06 because three stages threshold
# ink and only this one had the measured inference: the Ink Map and the
# Recensor's residual-ink audit took the page's raw histogram mode as paper,
# which on a photographed opening is the bezel, so their audit measured
# approximately zero ink over a page full of writing and the cross-stage
# containment pin held vacuously. The names below are this module's own history
# and every caller in this stage still reaches for them here; nothing in this
# file changes what they do.
#
# `PRIMARY_MARGIN` is the floor under the margin each page derives for itself and
# the level the background-plausibility probe is measured at -- see
# `common/background.py`, which carries the 127-page measurement behind both.
#
# `SECONDARY_MARGIN` stays here and is deliberately *not* derived. It is a
# fraction point below the declared background value: a pixel at or below
# `background - margin` counts as ink, and 2 is smaller than any derived margin,
# which makes `background - margin` a *higher* threshold -- numerically closer to
# the background value -- so it also catches fainter marks the primary scan
# misses: the whole "adds recall" property, expressed as one number. It is the
# sensitive instrument: `secondary_scan` exists to add recall and only recall,
# and `conservation.reconcile` uses it as the denominator that makes a mark the
# grouping pass missed appear as residual rather than as an absence. A derived
# margin there would trade a visible over-count for a possible silent loss,
# which is the one direction GOALS 1 forbids. Three properties follow and all
# three are pinned: the secondary scan is strictly more sensitive than the
# primary on *every* page (2 is below the floor, so no page can invert them);
# the Recensor's audit, which now infers the same background this stage does,
# runs at a contrast at or above this margin, so it can never call ink what this
# stage's own accounting dismissed; and both comparisons stay between source
# literals `common/test_designator_recensor_ink_calibration.py` can read
# statically.
SECONDARY_MARGIN: Final = 2


# `gap_tolerance_px` used to carry a module default here. It no longer does:
# it is the one threshold this build cannot honestly scale by page dimension
# (SPEC_C section 2), so `run.py` resolves it from the sealed absolute-pixel
# config field and passes it in on every call. A caller that forgets fails
# loudly rather than running under an unreviewed value.


def ink_pixels(width: int, height: int, rows: list, *, background: int, margin: int) -> set:
    """Every pixel at or below the ink threshold, as a set of (x, y) pairs.

    Split out from `scan_ink_components` so a caller that needs the raw ink set
    -- rather than whole components, which may straddle a crop's edge -- can
    take it directly. `conservation.py` was that caller until U13 replaced its
    pixel sets with row runs; it now shares only `_ink_threshold` and
    `SECONDARY_MARGIN` with this module, and `test_conservation.py` keeps this
    function as the oracle its replacement is checked against.
    """
    if width <= 0 or height <= 0:
        raise ContractError(f"a {width}x{height} page has no pixels to scan")
    if len(rows) != height:
        raise ContractError(f"expected {height} scanlines, got {len(rows)}")
    threshold = _ink_threshold(background, margin)

    ink: set[tuple[int, int]] = set()
    for y in range(height):
        row = rows[y]
        if len(row) != width:
            raise ContractError(f"scanline {y} has width {len(row)}, expected {width}")
        for x in range(width):
            if row[x] <= threshold:
                ink.add((x, y))
    return ink


# The labeller itself moved to `common/components.py` on 2026-09-06, and only
# the labeller: the ink set above, the two sensitivity presets and the two scans
# below are still this stage's own. It moved because the Recensor's
# outside-coverage audit now has to name the page-spanning component this stage
# withholds from grouping, `common/` may not import a stage, and a third copy of
# a rule this repository already keeps two of would be the drift surface
# `label_components`' own docstring argues against. The three names below are
# re-exported under the spellings this stage has always used, so every caller
# and every test in it reaches for them exactly where they were; nothing about
# what they do changes.
_label_components_reference = label_components_reference
_ink_runs_by_row = ink_runs_by_row


def scan_ink_components(
    width: int,
    height: int,
    rows: list,
    *,
    background: int,
    margin: int,
    gap_tolerance_px: int,
) -> list[Component]:
    """Every ink-bearing connected component on a decoded grayscale page.

    `rows` is exactly what `common.imaging.decode_grayscale_png` returns: one
    bytearray per scanline, one byte per pixel. Components are returned sorted
    by (top, left) so the result is deterministic and independent of set/dict
    iteration order -- this is a structure pass, and its output feeds identity
    derivation downstream, so an unordered result would make a rerun's
    numbering a coin flip.
    """
    pixels = ink_pixels(width, height, rows, background=background, margin=margin)
    return label_components(pixels, gap_tolerance_px=gap_tolerance_px)


def primary_scan(
    width: int, height: int, rows: list, *, background: int, margin: int, gap_tolerance_px: int
) -> list[Component]:
    """The primary proposer's scan, at the margin this page derived for itself.

    `margin` has no default and never will. It used to be `PRIMARY_MARGIN`, and
    the constant is still the floor under every value this is now passed --
    `infer_background_evidence` publishes it as `ink_margin` on the evidence,
    and `run.py` hands that same integer here and to the page's published
    record. A caller that forgets it fails loudly rather than running the whole
    page under a threshold nobody resolved, which is the rule
    `gap_tolerance_px` above is already here under.
    """
    return scan_ink_components(
        width,
        height,
        rows,
        background=background,
        margin=margin,
        gap_tolerance_px=gap_tolerance_px,
    )


def secondary_scan(
    width: int, height: int, rows: list, *, background: int, gap_tolerance_px: int
) -> list[Component]:
    return scan_ink_components(
        width,
        height,
        rows,
        background=background,
        margin=SECONDARY_MARGIN,
        gap_tolerance_px=gap_tolerance_px,
    )
