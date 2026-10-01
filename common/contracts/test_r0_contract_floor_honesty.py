"""Floor honesty: the page-read receipt argues with a dishonest coverage value.

Shortfalls derive as: `failed` from the existing outcome vocabulary; `truncated`
from content_health.truncated == True; `unaligned` is always 0, because every
witness reads the whole page. content_health.truncated == None (not recorded) is
not a recorded failure and is not a shortfall, but the receipt records it as
health-unrecorded. The page-read receipt
(common/recensor_receipt.py::_validate_coverage) carries health and shortfalls and
argues with a dishonest value of either. `pipeline/5_recensor/test_page_review.py`
checks the writer derives them.
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


# --- The receipt argues with a dishonest value of each fact it carries.


def test_an_honest_page_read_coverage_is_accepted():
    _validate_coverage(_base_coverage())


def test_a_health_unrecorded_count_beyond_the_configured_chairs_is_refused():
    """Unrecorded health is counted per chair; a count above three chairs lies."""
    coverage = _base_coverage(by_outcome={"read": 2, "genuinely-empty": 1}, health_unrecorded=4)
    with pytest.raises(SchemaRefusal, match="more granularity facts than configured chairs"):
        _validate_coverage(coverage)


def test_a_non_integer_unaligned_shortfall_is_refused():
    """A shortfall class carries a count, and only a count."""
    coverage = _base_coverage(shortfalls={"failed": 0, "truncated": 0, "unaligned": -1})
    with pytest.raises(SchemaRefusal, match="malformed shortfalls"):
        _validate_coverage(coverage)


# --- An under-witnessed unit may not say otherwise ---------------------------------


def test_an_under_witnessed_unit_cannot_claim_otherwise():
    """Three reads against a floor of three, one of them cut off: only two count,
    so a record claiming `under_witnessed=False` is lying and is refused."""
    coverage = _base_coverage(shortfalls={"failed": 0, "truncated": 1, "unaligned": 0})
    with pytest.raises(SchemaRefusal, match="under_witnessed"):
        _validate_coverage(coverage)
