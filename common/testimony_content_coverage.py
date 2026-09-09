"""Closed tri-state validation for Recensor page testimony coverage."""

from typing import Any

from common.contracts.errors import SchemaRefusal

_BASE_FIELDS = frozenset(
    {"by_chair", "shortfall", "reason", "unmeasured_reason", "unclaimed_observations"}
)


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
    if shortfall is not None and (by_chair is None or not by_chair):
        raise SchemaRefusal("measured testimony content coverage has no chair measurement basis")
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
