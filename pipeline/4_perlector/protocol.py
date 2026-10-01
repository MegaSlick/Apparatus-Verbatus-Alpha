"""The sealed, non-model policy a Perlector pass reads under.

One Perlector call reads one whole page. The declaration seals the feed
switches (`[feed]`, `validate_feed_table`), the page render's edges
(`[page_context]`) and the truncation instrument's numbers (`[truncation]`).

The page feed shows witnesses under the run's witness regime: `blinded` hides
chair and model names, so a witness is a pseudonymous label and a letter; the
labels a witness wrote on its own units (a layout block's label, a section
name) are part of its report and are shown as given under either regime.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Final

from common.calibration import calibrated_claim_has_sample_evidence
from common.contracts.errors import ContractError
from common.page_feed import FEED_TABLE, validate_feed_table
from common.sealed_config import read_sealed_toml

PAGE_CONTEXT_TABLE: Final = "page_context"

# The truncation instrument's sealed numbers, kept here so the length
# floor and the legibility gate ride on the `perlector-protocol` seal rather than in source, where a
# change between runs would leave provenance byte-identical. The table carries
# its own provenance block because a number with no declared source may not
# ship as a default.
TRUNCATION_TABLE: Final = "truncation"
LENGTH_FLOOR_FIELD: Final = "length_floor_characters_per_page"
LEGIBLE_PAGE_FIELD: Final = "legible_page_pixels"
_TRUNCATION_FIELDS: Final = frozenset({LENGTH_FLOOR_FIELD, LEGIBLE_PAGE_FIELD, "provenance"})
_PROVENANCE_FIELDS: Final = frozenset(
    {
        "source",
        "corpus",
        "sample_unit",
        "sample_count",
        "statistic",
        "calibrated_for_this_corpus",
        "caveat",
    }
)
_STRING_PROVENANCE_FIELDS: Final = frozenset(
    {"source", "corpus", "sample_unit", "statistic", "caveat"}
)


def _plain_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def validate_truncation_table(table: Any) -> dict[str, Any]:
    """The sealed `[truncation]` table, checked against its bounds and returned.

    Zero is refused: a floor of zero can never fire and is the signal switched
    off by a value rather than by a decision, which is the same refusal the
    coverage audit's gates carry. The provenance block is required, not
    optional, and a `calibrated_for_this_corpus` claim must carry sample
    evidence -- the shared rule `common/calibration.py` holds every config to.
    """
    where = f"[{TRUNCATION_TABLE}]"
    if not isinstance(table, dict):
        raise ContractError(f"the Perlector protocol declaration has no {where} table")
    if set(table) != _TRUNCATION_FIELDS:
        raise ContractError(
            f"the Perlector protocol declaration's {where} is not its closed schema "
            f"{sorted(_TRUNCATION_FIELDS)}; an unread policy field cannot be applied"
        )
    floor = table[LENGTH_FLOOR_FIELD]
    if not _plain_int(floor) or floor <= 0:
        raise ContractError(
            f"the Perlector protocol declaration's {where} {LENGTH_FLOOR_FIELD} is not a "
            "positive integer; a floor of zero never fires and is the length signal switched "
            "off by a value rather than by a decision"
        )
    if not _plain_int(table[LEGIBLE_PAGE_FIELD]) or table[LEGIBLE_PAGE_FIELD] <= 0:
        raise ContractError(
            f"the Perlector protocol declaration's {where} {LEGIBLE_PAGE_FIELD} is not a "
            "positive integer; a gate of zero judges the length of pages that cannot hold a "
            "legible line"
        )
    provenance = table["provenance"]
    if not isinstance(provenance, dict) or set(provenance) != _PROVENANCE_FIELDS:
        raise ContractError(
            f"the Perlector protocol declaration's {where}.provenance is not the closed "
            f"provenance schema {sorted(_PROVENANCE_FIELDS)}; a policy value with no declared "
            "source may not be shipped as a default"
        )
    for field in sorted(_STRING_PROVENANCE_FIELDS):
        if not isinstance(provenance[field], str) or not provenance[field].strip():
            raise ContractError(
                f"the Perlector protocol declaration's {where}.provenance field {field!r} is "
                "not a non-empty string"
            )
    if not _plain_int(provenance["sample_count"]) or provenance["sample_count"] < 0:
        raise ContractError(
            f"the Perlector protocol declaration's {where}.provenance sample_count is not a "
            "non-negative integer"
        )
    if not isinstance(provenance["calibrated_for_this_corpus"], bool):
        raise ContractError(
            f"the Perlector protocol declaration's {where}.provenance "
            "calibrated_for_this_corpus is not a boolean"
        )
    if not calibrated_claim_has_sample_evidence(
        provenance["calibrated_for_this_corpus"], provenance["sample_count"]
    ):
        raise ContractError(
            f"the Perlector protocol declaration's {where}.provenance says "
            "calibrated_for_this_corpus but records no sample"
        )
    return {
        LENGTH_FLOOR_FIELD: floor,
        LEGIBLE_PAGE_FIELD: table[LEGIBLE_PAGE_FIELD],
        "provenance": dict(provenance),
    }


def _validate_page_context(record: dict[str, Any]) -> None:
    """The `[page_context]` table: closed and positive."""
    page_context = record[PAGE_CONTEXT_TABLE]
    if (
        not isinstance(page_context, dict)
        or set(page_context) != {"maximum_edge"}
        or not _plain_int(page_context["maximum_edge"])
        or page_context["maximum_edge"] <= 0
    ):
        raise ContractError(
            f"the Perlector protocol declaration's [{PAGE_CONTEXT_TABLE}] is not exactly a "
            "positive integer maximum_edge"
        )


def load(path: str | Path) -> tuple[dict[str, Any], str]:
    """Read the policy a Perlector pass will use, with its seal."""
    record, digest = read_sealed_toml(path, "Perlector protocol declaration")
    if set(record) != {TRUNCATION_TABLE, PAGE_CONTEXT_TABLE, FEED_TABLE}:
        raise ContractError("the Perlector protocol declaration is not its closed schema")
    record[TRUNCATION_TABLE] = validate_truncation_table(record[TRUNCATION_TABLE])
    _validate_page_context(record)
    record[FEED_TABLE] = validate_feed_table(record[FEED_TABLE])
    return record, digest
