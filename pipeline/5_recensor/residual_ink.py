"""The shared residual-ink implementation, under the name this stage's tests use.

Test support, not stage code: `run.py` imports `common.residual_ink` directly.
Test modules in this directory import this by name (pytest puts the directory on
`sys.path` for them), and the names below are the ones they reach for.
"""

from common.background import BASIS_POINTS
from common.residual_ink import (  # noqa: F401
    MINIMUM_CONTRAST_BELOW_BACKGROUND,
    MINIMUM_FRACTION_OUTSIDE_BP_FIELD,
    MINIMUM_INK_PIXELS_FIELD,
    load_coverage_audit_config,
    page_residual_ink,
    residual_ink,
    resolve_coverage_audit_policy,
)

# The two noise-floor values under the names this directory's tests build their
# stimuli from, read from the sealed `[coverage_audit.noise_floor]` rather than
# from a module constant: since 2026-09-14 the file is the authority and the
# stage reads it under the run's seal, so a test anchored on these names moves
# with the sealed value instead of pinning a number the stage no longer reads.
_NOISE_FLOOR = load_coverage_audit_config()["coverage_audit"]
MINIMUM_INK_PIXELS: int = _NOISE_FLOOR[MINIMUM_INK_PIXELS_FIELD]
MINIMUM_FRACTION_OUTSIDE_COVERAGE: float = (
    _NOISE_FLOOR[MINIMUM_FRACTION_OUTSIDE_BP_FIELD] / BASIS_POINTS
)
