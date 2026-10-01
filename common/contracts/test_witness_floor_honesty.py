"""Floor honesty: what the page-read receipt checks of its own coverage.

Shortfalls derive as: `failed` from the existing outcome vocabulary; `truncated`
from content_health.truncated == True; `unaligned` is always 0, because every
witness reads the whole page. content_health.truncated == None (not recorded) is
not a recorded failure and is not a shortfall, but the receipt records it as
health-unrecorded.

The page-read receipt (common/recensor_receipt.py::_validate_coverage) checks
what its own counts can show: every count is bounded by the chairs it could
describe, `failed` equals `by_outcome["failed"]`, `unaligned` is always 0, and
`under_witnessed` agrees with the truncated count. It cannot see content_health,
so an in-bounds `truncated` or `health_unrecorded` is accepted as given; that
the writer derives both from content_health is proven by
`pipeline/5_recensor/test_page_review.py`.
"""

from __future__ import annotations

import pytest

from common.contracts.errors import SchemaRefusal
from common.recensor_receipt import _validate_coverage


def _base_coverage(**overrides) -> dict:
    """A page-read receipt's coverage: counts, health and shortfalls."""
    coverage = {
        "configured": 3,
        "floor": 3,
        "by_outcome": {"read": 3},
        "by_class": {"completed": 3, "unresolved": 0, "failed": 0},
        "under_witnessed": False,
        "unresolved_chairs": 0,
        "health_unrecorded": 0,
        "shortfalls": {"failed": 0, "truncated": 0, "unaligned": 0},
    }
    coverage.update(overrides)
    return coverage


# --- The receipt refuses a value its own counts contradict.


def test_an_honest_page_read_coverage_is_accepted():
    _validate_coverage(_base_coverage())


def test_a_health_unrecorded_count_beyond_the_configured_chairs_is_refused():
    """Unrecorded health is counted per chair; a count above three chairs lies."""
    coverage = _base_coverage(by_outcome={"read": 2, "genuinely-empty": 1}, health_unrecorded=4)
    with pytest.raises(SchemaRefusal, match="more granularity facts than configured chairs"):
        _validate_coverage(coverage)


@pytest.mark.parametrize("unaligned", [-1, "1", True])
def test_an_unaligned_shortfall_that_is_not_a_count_is_refused(unaligned):
    """A shortfall class carries a count, and only a count."""
    coverage = _base_coverage(shortfalls={"failed": 0, "truncated": 0, "unaligned": unaligned})
    with pytest.raises(SchemaRefusal, match="malformed shortfalls"):
        _validate_coverage(coverage)


def _one_failed_witness(failed_shortfall: int) -> dict:
    """Two reads and one failed witness, under a floor of three."""
    return _base_coverage(
        by_outcome={"read": 2, "failed": 1},
        by_class={"completed": 2, "unresolved": 0, "failed": 1},
        under_witnessed=True,
        shortfalls={"failed": failed_shortfall, "truncated": 0, "unaligned": 0},
    )


def test_a_failed_shortfall_equal_to_the_failed_outcomes_is_accepted():
    _validate_coverage(_one_failed_witness(1))


def test_a_failed_shortfall_that_hides_a_failed_witness_is_refused():
    with pytest.raises(SchemaRefusal, match="failed shortfall does not derive"):
        _validate_coverage(_one_failed_witness(0))


def test_a_well_typed_unaligned_shortfall_is_refused():
    """Every witness reads the whole page, so no witness is ever short for alignment."""
    coverage = _base_coverage(shortfalls={"failed": 0, "truncated": 0, "unaligned": 1})
    with pytest.raises(SchemaRefusal, match="unaligned shortfall"):
        _validate_coverage(coverage)


# --- An under-witnessed unit may not say otherwise ---------------------------------


def test_an_under_witnessed_unit_cannot_claim_otherwise():
    """Three reads against a floor of three, one of them cut off: only two count,
    so a record claiming `under_witnessed=False` is lying and is refused."""
    coverage = _base_coverage(shortfalls={"failed": 0, "truncated": 1, "unaligned": 0})
    with pytest.raises(SchemaRefusal, match="under_witnessed"):
        _validate_coverage(coverage)
