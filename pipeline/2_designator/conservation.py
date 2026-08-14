"""Conservation: claimed ink plus residual ink equals every ink pixel found.

The old pipeline's own conservation logic proved coverage of units the
structure model itself had already emitted -- a chunk not claimed by a crop
was accounted for, but a mark the structure model never emitted as a chunk at
all had no denominator to be missing from. An independent second read
(`/stage/70_gpt_review/ASSESSMENT.md:172-173`) named this precisely: "the
present conservation logic proves coverage of units already emitted by a
structural model. It cannot prove that the model did not miss ink entirely."
This module closes exactly that gap: it does not trust the structure pass's own claimed
regions as the denominator. It rescans the page's actual pixels independently
and classifies every ink pixel found as claimed (inside some cut crop) or
residual (not inside any), so a mark the grouping pass never produced a region
for still appears -- as a residual component, never as an absence.

**Every residual region is accounted regardless of size.** `review_priority`
below orders which residual a reviewer looks at first; it never decides
whether a residual exists in the accounting. Deleting the priority threshold
entirely would only reorder review, never drop a region -- which is the
property GOVERNANCE 10 requires of any threshold in an instrument: the
instrument may not constrain what it measures.

**Two ink calibrations exist in this pipeline, by decision rather than drift.**
This stage conserves a pixel as ink at `background - SECONDARY_MARGIN` (2 levels),
the most sensitive declared structural threshold available to this stage;
the Recensor's independent page-coverage check
(`pipeline/5_recensor/residual_ink.py`) requires `MINIMUM_CONTRAST_BELOW_BACKGROUND`
(40 levels). They are different instruments: this one is the Designator
reconciling its own cut and errs sensitive, because a faint mark it dismisses
here is GOALS 1's worst failure; the Recensor's is an after-the-fact audit of
the same pages and errs confident, because it exists to catch whole missed
regions rather than to re-litigate faint pixels a held act already accounts
for. The asymmetry is safe in exactly one direction, and that direction is the
invariant: every pixel the Recensor calls ink is ink to this stage too, so the
audit can never flag ink this accounting silently ignored — while ink only this
stage sees ends as a held residual act, which is visible, never lost. The
containment is pinned where the two stages legitimately meet,
`common/test_designator_recensor_ink_calibration.py`; narrowing this stage's
margin past the Recensor's contrast would break the safe direction and must be
a deliberate two-sided change.

**This is a mechanism-scale implementation, not the real-page backend.**
`reconcile` currently materialises the page's ink, claimed ink, and residual
ink as Python coordinate sets. That is deliberately legible over the walking
skeleton's tiny synthetic pages, but it is the substitution boundary before a
real corpus: replace this classification with a row-oriented bitmap/interval
or native array implementation, together with `structure.ink_pixels` and
`structure.label_components`, after measurement on representative pages. A
rewrite now would optimise an unmeasured skeleton; carrying these three sets
onto dense high-resolution pages would be equally unjustified.
"""

from typing import Final, TypedDict

import geometry
from structure import (
    DEFAULT_GAP_TOLERANCE_PX,
    SECONDARY_MARGIN,
    Component,
    ink_pixels,
    label_components,
)

from common.contracts.errors import ContractError


class ReconciliationResult(TypedDict):
    total_ink_pixel_count: int
    claimed_pixel_count: int
    residual_pixel_count: int
    residual_components: list[dict]


# A residual component at or above this many pixels, on either axis, is
# reviewed first -- a priority ordering, not a filter. See the module
# docstring: nothing here may become an inclusion test.
DEFAULT_REVIEW_PRIORITY_MIN_DIMENSION_PX: Final = 6


def _is_claimed(x: int, y: int, claimed_bounds: list[geometry.Bounds]) -> bool:
    for bounds in claimed_bounds:
        if (
            bounds["x"] <= x < bounds["x"] + bounds["w"]
            and bounds["y"] <= y < bounds["y"] + bounds["h"]
        ):
            return True
    return False


def reconcile(
    width: int,
    height: int,
    rows: list,
    *,
    background: int,
    claimed_bounds: list[geometry.Bounds],
    margin: int = SECONDARY_MARGIN,
    gap_tolerance_px: int = DEFAULT_GAP_TOLERANCE_PX,
    review_priority_min_dimension_px: int = DEFAULT_REVIEW_PRIORITY_MIN_DIMENSION_PX,
) -> ReconciliationResult:
    """Reconcile one page's ink against the crops actually cut on it.

    `claimed_bounds` is the final, padded crop rectangles this stage actually
    cut -- not the structure pass's raw proposals -- because a crop's padding
    is exactly what may already cover ink the raw proposal's own rectangle did
    not. Every ink pixel is classified once; `claimed_pixel_count +
    residual_pixel_count == total_ink_pixel_count` always, by construction,
    because every pixel in the denominator is examined exactly once.
    """
    if review_priority_min_dimension_px < 0:
        raise ContractError(
            f"review priority threshold {review_priority_min_dimension_px} is negative"
        )
    for bounds in claimed_bounds:
        geometry.validate_bounds(bounds, width, height, "claimed bounds")

    pixels = ink_pixels(width, height, rows, background=background, margin=margin)
    claimed = {pixel for pixel in pixels if _is_claimed(pixel[0], pixel[1], claimed_bounds)}
    residual = pixels - claimed
    residual_components: list[Component] = label_components(
        residual, gap_tolerance_px=gap_tolerance_px
    )

    accounted: list[dict] = []
    for component in residual_components:
        bounds = component["bounds"]
        high_priority = max(bounds["w"], bounds["h"]) >= review_priority_min_dimension_px
        accounted.append(
            {
                "bounds": bounds,
                "pixel_count": component["pixel_count"],
                "review_priority": "high" if high_priority else "low",
            }
        )

    return {
        "total_ink_pixel_count": len(pixels),
        "claimed_pixel_count": len(claimed),
        "residual_pixel_count": len(residual),
        "residual_components": accounted,
    }
