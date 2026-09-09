import copy

import pytest

from common.contracts.errors import SchemaRefusal
from common.testimony_content_coverage import (
    validate_testimony_content_coverage,
    validate_testimony_content_coverage_continuation,
)


@pytest.mark.parametrize(
    "record",
    [
        None,
        [],
        {"shortfall": None, "reason": "no comparable text"},
        {"by_chair": {}, "reason": "no comparable text"},
        {
            "by_chair": {},
            "shortfall": None,
            "reason": "no comparable text",
            "unexpected": True,
        },
    ],
)
def test_primary_coverage_requires_a_closed_record_with_both_required_fields(record):
    with pytest.raises(SchemaRefusal, match="closed tri-state measurement"):
        validate_testimony_content_coverage(record)


def test_tri_state_requires_a_reason_for_an_unmeasured_page():
    with pytest.raises(SchemaRefusal, match="named reason"):
        validate_testimony_content_coverage({"by_chair": {}, "shortfall": None})


def test_tri_state_refuses_a_non_boolean_non_null_shortfall():
    with pytest.raises(SchemaRefusal, match="true, false, or null"):
        validate_testimony_content_coverage({"by_chair": {}, "shortfall": "unknown"})


def test_continuations_are_positive_unique_page_ordinals():
    row = {"by_chair": {}, "shortfall": None, "reason": "no comparable text", "page_ordinal": 2}
    rows = [row, {**row, "page_ordinal": 3}]
    expected = copy.deepcopy(rows)
    assert validate_testimony_content_coverage_continuation(rows) == expected
    assert rows == expected
    duplicate = {**row, "reason": "this distinct record still names the same page"}
    with pytest.raises(SchemaRefusal, match="repeats"):
        validate_testimony_content_coverage_continuation([row, duplicate])


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


@pytest.mark.parametrize("field", ["reason", "unmeasured_reason"])
@pytest.mark.parametrize("invalid", ["", "   ", None, 1])
def test_present_measurement_reasons_must_be_nonblank_strings(field, invalid):
    record = {"by_chair": {}, "shortfall": None, "reason": "no comparable text"}
    record[field] = invalid
    with pytest.raises(SchemaRefusal, match=f"{field} is not a non-blank string"):
        validate_testimony_content_coverage(record)


def test_measured_tri_state_requires_a_nonempty_chair_basis():
    with pytest.raises(SchemaRefusal, match="no chair measurement basis"):
        validate_testimony_content_coverage({"by_chair": {}, "shortfall": False})


def test_unmeasured_tri_state_preserves_a_null_chair_basis_unchanged():
    record = {
        "by_chair": None,
        "shortfall": None,
        "reason": "no page witness supplied comparable page text",
    }
    expected = copy.deepcopy(record)

    assert validate_testimony_content_coverage(record) == expected
    assert record == expected


@pytest.mark.parametrize(
    ("shortfall", "unmeasured_reason"),
    [
        (False, None),
        (True, None),
        (True, "some declared continuation text has no measurable act anchor"),
    ],
)
def test_measured_coverage_with_a_chair_basis_is_accepted_unchanged(shortfall, unmeasured_reason):
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
    if unmeasured_reason is not None:
        record["unmeasured_reason"] = unmeasured_reason
    expected = copy.deepcopy(record)
    validated = validate_testimony_content_coverage(record)
    assert validated == expected
    assert record == expected


def test_unmeasured_continuation_keeps_typed_uncovered_evidence_unchanged():
    record = {
        "by_chair": {
            "attestator_1": {
                "attached_spans": [],
                "uncovered_non_whitespace": {
                    "ranges": [{"start": 0, "end": 4}, {"start": 6, "end": 8}],
                    "count": 6,
                },
            }
        },
        "shortfall": None,
        "reason": "continuation text has no measurable act anchor",
    }
    expected = copy.deepcopy(record)

    assert validate_testimony_content_coverage(record) == expected
    assert record == expected


@pytest.mark.parametrize(
    ("field", "value", "match"),
    [
        ("measurement", {}, "closed measurement fields"),
        ("measurement", None, "closed measurement fields"),
        ("measurement_extra", True, "closed measurement fields"),
        ("role", "  ", "blank chair role"),
        ("attached_spans", {}, "attached_spans is not a list"),
        (
            "attached_span",
            {"start": 0, "end": -1, "act_id": "act-one"},
            "invalid offsets",
        ),
        ("attached_span", {"start": 0, "end": 1, "act_id": ""}, "act identity"),
        ("uncovered_non_whitespace", {"ranges": []}, "closed count-and-ranges"),
        ("ranges", {}, "is untyped"),
        (
            "ranges",
            [{"start": 0, "end": 1}, {"start": 1, "end": 2}],
            "positive half-open range",
        ),
        ("range", {"start": 2, "end": 2}, "positive half-open range"),
        ("range", {"start": True, "end": 2}, "positive half-open range"),
        ("count", True, "is untyped"),
        ("count", 2, "count does not equal its ranges"),
    ],
)
def test_chair_measurement_refuses_malformed_nested_evidence(field, value, match):
    measurement = {
        "attached_spans": [{"start": 0, "end": 1, "act_id": "act-one"}],
        "uncovered_non_whitespace": {"ranges": [{"start": 2, "end": 3}], "count": 1},
    }
    role = "attestator_1"
    if field == "role":
        role = value
    elif field == "measurement":
        measurement = value
    elif field == "measurement_extra":
        measurement["unexpected"] = value
    elif field == "attached_span":
        measurement["attached_spans"] = [value]
    elif field == "range":
        measurement["uncovered_non_whitespace"]["ranges"] = [value]
    elif field in {"ranges", "count"}:
        measurement["uncovered_non_whitespace"][field] = value
    else:
        measurement[field] = value
    record = {"by_chair": {role: measurement}, "shortfall": True}

    with pytest.raises(SchemaRefusal, match=match):
        validate_testimony_content_coverage(record)


@pytest.mark.parametrize(
    ("shortfall", "count"),
    [(False, 1), (True, 0)],
)
def test_measured_shortfall_must_match_the_uncovered_count(shortfall, count):
    ranges = [{"start": 2, "end": 3}] if count else []
    record = {
        "by_chair": {
            "attestator_1": {
                "attached_spans": [],
                "uncovered_non_whitespace": {"ranges": ranges, "count": count},
            }
        },
        "shortfall": shortfall,
    }

    with pytest.raises(SchemaRefusal, match="shortfall disagrees with its uncovered counts"):
        validate_testimony_content_coverage(record)
