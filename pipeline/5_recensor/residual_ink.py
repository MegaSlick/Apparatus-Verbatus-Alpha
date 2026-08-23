"""Compatibility import for the one shared residual-ink implementation.

The measurement moved to ``common.residual_ink`` when the early ink-map stage
became its first consumer.  Keeping this import surface preserves the Recensor
tests while making two implementations impossible.
"""

from common.residual_ink import (  # noqa: F401
    EDGE_BAND_PIXELS,
    MINIMUM_CONTRAST_BELOW_BACKGROUND,
    MINIMUM_FRACTION_OUTSIDE_COVERAGE,
    MINIMUM_INK_PIXELS,
    SUBSTANTIAL_INK_PIXELS,
    page_edge_ink,
    page_residual_ink,
    residual_ink,
)
