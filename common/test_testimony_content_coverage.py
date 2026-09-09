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


@pytest.mark.parametrize("ordinal", [0, -1, True, "2", None])
def test_continuations_refuse_nonpositive_or_untyped_page_ordinals(ordinal):
    row = {
        "by_chair": {},
        "shortfall": None,
        "reason": "no comparable text",
        "page_ordinal": ordinal,
    }
    with pytest.raises(SchemaRefusal, match="positive page ordinal"):
        validate_testimony_content_coverage_continuation([row])


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


@pytest.mark.parametrize("shortfall", [False, True])
def test_measured_coverage_with_a_chair_basis_is_accepted_unchanged(shortfall):
    record = {
        "by_chair": {
            "attestator_1": {
                "attached_spans": [{"start": 0, "end": 5, "act_id": "act-one"}],
                "uncovered_non_whitespace": {
                    "ranges": [{"start": 5, "end": 7}] if shortfall else [],
                    "count": 2 if shortfall else 0,
                },
            }
        },
        "shortfall": shortfall,
        "unclaimed_observations": [],
    }
    assert validate_testimony_content_coverage(record) == record
