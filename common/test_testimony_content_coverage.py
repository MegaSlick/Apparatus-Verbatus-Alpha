import pytest

from common.contracts.errors import SchemaRefusal
from common.testimony_content_coverage import (
    validate_testimony_content_coverage,
    validate_testimony_content_coverage_continuation,
)


def test_tri_state_requires_a_reason_for_an_unmeasured_page():
    with pytest.raises(SchemaRefusal, match="named reason"):
        validate_testimony_content_coverage({"by_chair": {}, "shortfall": None})


def test_tri_state_refuses_a_non_boolean_non_null_shortfall():
    with pytest.raises(SchemaRefusal, match="true, false, or null"):
        validate_testimony_content_coverage({"by_chair": {}, "shortfall": "unknown"})


def test_continuations_are_positive_unique_page_ordinals():
    row = {"by_chair": {}, "shortfall": None, "reason": "no comparable text", "page_ordinal": 2}
    assert validate_testimony_content_coverage_continuation([row]) == [row]
    with pytest.raises(SchemaRefusal, match="repeats"):
        validate_testimony_content_coverage_continuation([row, row])


def test_tri_state_refuses_untyped_producer_fields():
    with pytest.raises(SchemaRefusal, match="by_chair"):
        validate_testimony_content_coverage({"by_chair": [], "shortfall": False})
    with pytest.raises(SchemaRefusal, match="unclaimed_observations"):
        validate_testimony_content_coverage(
            {"by_chair": {"chair": {}}, "shortfall": False, "unclaimed_observations": "none"}
        )


def test_measured_tri_state_requires_a_nonempty_chair_basis():
    with pytest.raises(SchemaRefusal, match="no chair measurement basis"):
        validate_testimony_content_coverage({"by_chair": {}, "shortfall": False})
