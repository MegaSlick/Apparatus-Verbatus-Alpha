"""References to bytes this stage retained, checked and de-duplicated."""

from __future__ import annotations

import json
from typing import Any

from common.contracts.canonical import is_sha256
from common.contracts.envelope import read_verified
from common.contracts.errors import SchemaRefusal


def is_positive_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 1


def sorted_refs(references: list[dict[str, str]]) -> list[dict[str, str]]:
    return sorted(references, key=lambda ref: (ref.get("relative_path", ""), ref.get("sha256", "")))


def validate_stage_blob_ref(reference: Any, field: str) -> dict[str, str]:
    """Close one content-addressed reference to this stage's own blob store."""
    prefix = "3_attestatores/blobs/sha256/"
    if (
        not isinstance(reference, dict)
        or set(reference) != {"relative_path", "sha256"}
        or not isinstance(reference["relative_path"], str)
        or not is_sha256(reference["sha256"])
        or reference["relative_path"] != prefix + reference["sha256"]
    ):
        raise SchemaRefusal(f"a Testimonium {field} is not an Attestatores blob reference")
    return reference


def validate_raw_response_ref(reference: Any) -> dict[str, str]:
    """Close one retained-response reference to this stage's own blob store."""
    return validate_stage_blob_ref(reference, "raw_response_ref")


def named_once(references: list[Any]) -> list[Any]:
    """Keep the first mention of each input reference, in the order given.

    A repeat is not a second response and must not read as one.
    """
    seen: set[str] = set()
    kept: list[Any] = []
    for reference in references:
        key = (
            json.dumps(reference, sort_keys=True)
            if isinstance(reference, dict)
            else repr(reference)
        )
        if key in seen:
            continue
        seen.add(key)
        kept.append(reference)
    return kept


def validate_retained_response_blob(
    tree: Any, reference: Any, field: str = "raw_response_ref"
) -> None:
    """Re-hash the named response or call blob during tally read-back."""
    checked = validate_stage_blob_ref(reference, field)
    what = f"retained witness {field}"
    read_verified(tree.read_bytes, checked, what)
