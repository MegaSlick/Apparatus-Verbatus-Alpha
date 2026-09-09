"""Closed tri-state validation for Recensor page testimony coverage."""

from typing import Any

from common.contracts.errors import SchemaRefusal

_BASE_FIELDS = frozenset(
    {"by_chair", "shortfall", "reason", "unmeasured_reason", "unclaimed_observations"}
)
_CHAIR_MEASUREMENT_FIELDS = frozenset({"attached_spans", "uncovered_non_whitespace"})
_ATTACHED_SPAN_FIELDS = frozenset({"start", "end", "act_id"})
_UNCOVERED_FIELDS = frozenset({"ranges", "count"})
_RANGE_FIELDS = frozenset({"start", "end"})


def _plain_non_negative_integer(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


def _validate_chair_measurement(role: object, measurement: object) -> int:
    if not isinstance(role, str) or not role.strip():
        raise SchemaRefusal("testimony content coverage has a non-textual or blank chair role")
    if not isinstance(measurement, dict) or set(measurement) != _CHAIR_MEASUREMENT_FIELDS:
        raise SchemaRefusal(
            f"testimony content coverage chair {role!r} does not carry the closed measurement fields"
        )

    attached_spans = measurement["attached_spans"]
    if not isinstance(attached_spans, list):
        raise SchemaRefusal(
            f"testimony content coverage chair {role!r} attached_spans is not a list"
        )
    for ordinal, span in enumerate(attached_spans):
        subject = f"testimony content coverage chair {role!r} attached span {ordinal}"
        if not isinstance(span, dict) or set(span) != _ATTACHED_SPAN_FIELDS:
            raise SchemaRefusal(f"{subject} is not a closed span")
        start, end, act_id = span["start"], span["end"], span["act_id"]
        if (
            not _plain_non_negative_integer(start)
            or not _plain_non_negative_integer(end)
            or end < start
            or not isinstance(act_id, str)
            or not act_id.strip()
        ):
            raise SchemaRefusal(f"{subject} has invalid offsets or act identity")

    uncovered = measurement["uncovered_non_whitespace"]
    if not isinstance(uncovered, dict) or set(uncovered) != _UNCOVERED_FIELDS:
        raise SchemaRefusal(
            f"testimony content coverage chair {role!r} uncovered_non_whitespace "
            "is not a closed count-and-ranges measurement"
        )
    ranges = uncovered["ranges"]
    count = uncovered["count"]
    if not isinstance(ranges, list) or not _plain_non_negative_integer(count):
        raise SchemaRefusal(
            f"testimony content coverage chair {role!r} uncovered_non_whitespace is untyped"
        )
    measured_count = 0
    previous_end = -1
    for ordinal, item in enumerate(ranges):
        subject = f"testimony content coverage chair {role!r} uncovered range {ordinal}"
        if not isinstance(item, dict) or set(item) != _RANGE_FIELDS:
            raise SchemaRefusal(f"{subject} is not a closed range")
        start, end = item["start"], item["end"]
        if (
            not _plain_non_negative_integer(start)
            or not _plain_non_negative_integer(end)
            or end <= start
            or start <= previous_end
        ):
            raise SchemaRefusal(f"{subject} is not a separated positive half-open range")
        measured_count += end - start
        previous_end = end
    if count != measured_count:
        raise SchemaRefusal(
            f"testimony content coverage chair {role!r} uncovered count does not equal its ranges"
        )
    return count


def validate_testimony_content_coverage(
    record: object, *, continuation: bool = False
) -> dict[str, Any]:
    """Validate the producer's tri-state measurement record without routing it.

    ``shortfall`` is exactly True, False, or None.  None records an unavailable
    measurement and therefore requires its named reason; it is not a clean result.
    """
    expected = _BASE_FIELDS | ({"page_ordinal"} if continuation else set())
    if (
        not isinstance(record, dict)
        or not {"by_chair", "shortfall"} <= set(record)
        or not set(record) <= expected
    ):
        raise SchemaRefusal(
            "testimony content coverage record is not a closed tri-state measurement"
        )
    by_chair = record["by_chair"]
    if by_chair is not None and not isinstance(by_chair, dict):
        raise SchemaRefusal("testimony content coverage by_chair is neither an object nor null")
    for field in ("reason", "unmeasured_reason"):
        if field in record and (not isinstance(record[field], str) or not record[field].strip()):
            raise SchemaRefusal(f"testimony content coverage {field} is not a non-blank string")
    if "unclaimed_observations" in record and not isinstance(
        record["unclaimed_observations"], list
    ):
        raise SchemaRefusal("testimony content coverage unclaimed_observations is not a list")
    shortfall = record["shortfall"]
    if shortfall is not True and shortfall is not False and shortfall is not None:
        raise SchemaRefusal("testimony content coverage shortfall is not true, false, or null")
    uncovered_counts = (
        [_validate_chair_measurement(role, measurement) for role, measurement in by_chair.items()]
        if isinstance(by_chair, dict)
        else []
    )
    if shortfall is not None and (by_chair is None or not by_chair):
        raise SchemaRefusal("measured testimony content coverage has no chair measurement basis")
    if shortfall is not None and shortfall is not any(count > 0 for count in uncovered_counts):
        raise SchemaRefusal(
            "measured testimony content coverage shortfall disagrees with its uncovered counts"
        )
    if continuation:
        ordinal = record.get("page_ordinal")
        if not isinstance(ordinal, int) or isinstance(ordinal, bool) or ordinal <= 0:
            raise SchemaRefusal(
                "continuation testimony content coverage has no positive page ordinal"
            )
    if shortfall is None:
        reason = record.get("reason")
        if not isinstance(reason, str) or not reason.strip():
            raise SchemaRefusal("unmeasured testimony content coverage has no named reason")
    return record


def validate_testimony_content_coverage_continuation(rows: object) -> list[dict[str, Any]]:
    if not isinstance(rows, list):
        raise SchemaRefusal("continuation testimony content coverage is not a list")
    checked = [validate_testimony_content_coverage(row, continuation=True) for row in rows]
    ordinals = [row["page_ordinal"] for row in checked]
    if len(ordinals) != len(set(ordinals)):
        raise SchemaRefusal("continuation testimony content coverage repeats a page ordinal")
    return checked
