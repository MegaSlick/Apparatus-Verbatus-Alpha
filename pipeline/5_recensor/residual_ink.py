"""The shared residual-ink implementation, under the name this stage's tests use.

Test support, not stage code: `run.py` imports `common.residual_ink` directly.
Test modules in this directory import this by name (pytest puts the directory on
`sys.path` for them), and the names below are the ones they reach for.
"""

from common.residual_ink import (  # noqa: F401
    MINIMUM_CONTRAST_BELOW_BACKGROUND,
    MINIMUM_FRACTION_OUTSIDE_COVERAGE,
    MINIMUM_INK_PIXELS,
    SUBSTANTIAL_INK_PIXELS,
    page_residual_ink,
    residual_ink,
)
