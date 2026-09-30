"""The sealed, non-model policy a Perlector pass reads under.

It seals what one Perlector call reads (`reading_unit`, always `"page"`), the
feed switches (`[feed]`, `validate_feed_table`), the page render's edges
(`[page_context]`) and the truncation instrument's numbers (`[truncation]`).
The declaration also carries the prior-draft fields, `max_images` and
`[neighbours]`; they are checked as sealed and no pass reads them.

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

SELECTION_RULE: Final = "digest-threshold-over-frame-page-seed-act.v1"
PAGE_SHARED_PREFIX_POLICY: Final = "page-shared-prefix-first.v1"

# The sealed prior-draft fragment's one accepted form. Pinned rather than a free-text
# config field: a phrase blacklist alone cannot stop wording that forces a change or
# picks a side.
PASS_B_FRAGMENT: Final = (
    "This is a prior reading. It may be correct, incomplete, or wrong. Independently reread "
    "the image, preserve what the ink supports, and change only what the image justifies."
)
# The sealed neighbour fragment's one accepted form, pinned like the prior-draft
# fragment: wording that invited copying across the boundary is refused.
NEIGHBOUR_FRAGMENT: Final = (
    "The neighbouring acts are the acts written just before and just after this one, as "
    "the witnesses read them; (tail) marks only the end of a reading and (head) only its "
    "beginning. They are context only: names, dates and formulas recur from act to act, "
    "and the boundary between two acts is where readings most often go wrong. Transcribe "
    "only this act's own ink and never copy a neighbour's text into it."
)
NEIGHBOURS_TABLE: Final = "neighbours"
PAGE_CONTEXT_TABLE: Final = "page_context"
READING_UNIT_FIELD: Final = "reading_unit"
READING_UNITS: Final = frozenset({"page"})
_FIELDS: Final = frozenset(
    {
        "selection_rule",
        "page_shared_prefix_policy",
        "pass_b_fragment",
        "max_images",
        "truncation",
        NEIGHBOURS_TABLE,
        PAGE_CONTEXT_TABLE,
        READING_UNIT_FIELD,
        FEED_TABLE,
    }
)
_STRING_FIELDS: Final = frozenset(
    {"selection_rule", "page_shared_prefix_policy", "pass_b_fragment", READING_UNIT_FIELD}
)

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


def _validate_small_tables(record: dict[str, Any]) -> None:
    """The `[neighbours]` and `[page_context]` tables: closed, positive, pinned."""
    neighbours = record[NEIGHBOURS_TABLE]
    if (
        not isinstance(neighbours, dict)
        or set(neighbours) != {"characters_per_row", "fragment"}
        or not _plain_int(neighbours["characters_per_row"])
        or neighbours["characters_per_row"] <= 0
    ):
        raise ContractError(
            f"the Perlector protocol declaration's [{NEIGHBOURS_TABLE}] is not "
            "exactly a positive integer characters_per_row and the fragment"
        )
    if neighbours["fragment"] != NEIGHBOUR_FRAGMENT:
        raise ContractError(
            "the neighbour fragment is not the declared form; what this pipeline says to a "
            "reader about the neighbouring acts is not a free-text configuration field"
        )
    page_context = record[PAGE_CONTEXT_TABLE]
    if (
        not isinstance(page_context, dict)
        or set(page_context) != {"maximum_edge", "covered_page_edge"}
        or not all(_plain_int(page_context[key]) and page_context[key] > 0 for key in page_context)
        or page_context["covered_page_edge"] > page_context["maximum_edge"]
    ):
        raise ContractError(
            f"the Perlector protocol declaration's [{PAGE_CONTEXT_TABLE}] is not exactly a "
            "positive integer maximum_edge and a covered_page_edge no larger than it"
        )


def load(path: str | Path) -> tuple[dict[str, Any], str]:
    """Read the policy a Perlector pass will use, with its seal."""
    record, digest = read_sealed_toml(path, "Perlector protocol declaration")
    if set(record) != _FIELDS or not all(isinstance(record[key], str) for key in _STRING_FIELDS):
        raise ContractError("the Perlector protocol declaration is not its closed schema")
    record[TRUNCATION_TABLE] = validate_truncation_table(record[TRUNCATION_TABLE])
    _validate_small_tables(record)
    if record[READING_UNIT_FIELD] not in READING_UNITS:
        raise ContractError(
            f"the Perlector protocol declaration's {READING_UNIT_FIELD} "
            f"{record[READING_UNIT_FIELD]!r} is not one of {sorted(READING_UNITS)}"
        )
    record[FEED_TABLE] = validate_feed_table(record[FEED_TABLE])
    if (
        not isinstance(record["max_images"], int)
        or isinstance(record["max_images"], bool)
        or record["max_images"] <= 0
    ):
        raise ContractError(
            "the Perlector protocol declaration's max_images is not a positive integer, "
            f"got {record['max_images']!r}"
        )
    if record["selection_rule"] != SELECTION_RULE:
        raise ContractError("the Perlector protocol declaration names an unknown selection rule")
    if record["page_shared_prefix_policy"] != PAGE_SHARED_PREFIX_POLICY:
        raise ContractError(
            "the Perlector protocol declaration names an unknown page-shared-prefix policy"
        )
    if not record["pass_b_fragment"].strip():
        raise ContractError("the Perlector protocol declaration has a blank Pass-B fragment")
    # Checked ahead of the equality check so a fragment that trips this one is
    # diagnosed for a stated reason, not merely "different from the pinned bytes".
    if "prior reading was wrong" in record["pass_b_fragment"].lower():
        raise ContractError(
            "the Pass-B fragment asserts that the prior was wrong; the protocol is neutral"
        )
    if record["pass_b_fragment"] != PASS_B_FRAGMENT:
        raise ContractError(
            "the Pass-B fragment is not the declared neutral form (iterative_reader.md:49-50); "
            "what this pipeline says to a reader about its own prior draft is not a free-text "
            "configuration field"
        )
    return record, digest
