"""The sealed, non-model policy for R5a's prior-draft protocol."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Final

from common.calibration import calibrated_claim_has_sample_evidence
from common.contracts.approval import ApprovalRecordBinding
from common.contracts.canonical import digest_of
from common.contracts.errors import ContractError
from common.sealed_config import read_sealed_toml
from common.stage import PERLECTOR_INSTRUMENT_APPROVAL_SUBJECT

SELECTION_RULE: Final = "digest-threshold-over-frame-page-seed-act.v1"
PAGE_SHARED_PREFIX_POLICY: Final = "page-shared-prefix-first.v1"

# The only text the pipeline puts in front of the reader about its own prior
# draft. Pinned rather than a free-text config field: a phrase blacklist alone
# cannot stop wording that forces a change or picks a side. The sealed bytes
# still ride on every record so a run says which exact form ran.
PASS_B_FRAGMENT: Final = (
    "This is a prior reading. It may be correct, incomplete, or wrong. Independently reread "
    "the image, preserve what the ink supports, and change only what the image justifies."
)
_FIELDS: Final = frozenset(
    {"selection_rule", "page_shared_prefix_policy", "pass_b_fragment", "max_images", "truncation"}
)
_STRING_FIELDS: Final = frozenset(
    {"selection_rule", "page_shared_prefix_policy", "pass_b_fragment"}
)

# The truncation instrument's one sealed number, kept here so the length
# floor rides on the `perlector-protocol` seal rather than in source, where a
# change between runs would leave provenance byte-identical. The table carries
# its own provenance block because a number with no declared source may not
# ship as a default.
TRUNCATION_TABLE: Final = "truncation"
LENGTH_FLOOR_FIELD: Final = "length_floor_characters_per_page"
_TRUNCATION_FIELDS: Final = frozenset({LENGTH_FLOOR_FIELD, "provenance"})
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
    return {LENGTH_FLOOR_FIELD: floor, "provenance": dict(provenance)}


def load(path: str | Path) -> tuple[dict[str, Any], str]:
    """Read the policy a Perlector pass will use, with its seal."""
    record, digest = read_sealed_toml(path, "Perlector protocol declaration")
    if set(record) != _FIELDS or not all(isinstance(record[key], str) for key in _STRING_FIELDS):
        raise ContractError("the Perlector protocol declaration is not its closed schema")
    record[TRUNCATION_TABLE] = validate_truncation_table(record[TRUNCATION_TABLE])
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


def validate_control_per_mille(value: int) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or not 0 <= value <= 1000:
        raise ValueError(
            f"perlector_instrument_per_mille must be an integer in [0, 1000], got {value!r}"
        )
    return value


def is_control_sampled(
    act_id: str, *, frame_digest: str, page_digest: str, seed: str, per_mille: int
) -> bool:
    """Uniform digest threshold over run-stable corpus and act facts only.

    `int(digest[:8], 16) % 1000` is not perfectly uniform (2**32 % 1000 ==
    296), biasing the low 296 thresholds by ~2.3e-5% -- negligible at any
    corpus size this pipeline will sample, and left uncorrected because a
    rejection-sampling retry would exist only for a bias no real run could
    detect.
    """
    validate_control_per_mille(per_mille)
    if per_mille == 0:
        return False
    digest = digest_of(
        {
            "purpose": "perlector-prior-control",
            "frame_digest": frame_digest,
            "page_digest": page_digest,
            "seed": seed,
            "act_id": act_id,
        }
    )
    return int(digest[:8], 16) % 1000 < per_mille


def control_sampling_design(
    *, per_mille: int, selection_rule: str, approval_ref: ApprovalRecordBinding
) -> dict[str, object]:
    """Bind each control sample to its rate, rule, and typed approval (principle 8)."""
    validate_control_per_mille(per_mille)
    if selection_rule != SELECTION_RULE:
        raise ValueError(
            f"design {PERLECTOR_INSTRUMENT_APPROVAL_SUBJECT!r} does not execute selection "
            f"rule {selection_rule!r}"
        )
    if not isinstance(approval_ref, ApprovalRecordBinding):
        raise ValueError(
            "a prior-draft control was drawn with an untyped approval reference; "
            "an arbitrary string is not an approval record"
        )
    if approval_ref.subject != PERLECTOR_INSTRUMENT_APPROVAL_SUBJECT:
        raise ValueError(
            "a prior-draft control executes design "
            f"{PERLECTOR_INSTRUMENT_APPROVAL_SUBJECT!r}, but its approval record names "
            f"{approval_ref.subject!r}"
        )
    return {
        "perlector_instrument_per_mille": per_mille,
        "selection_rule": selection_rule,
        "approval_ref": approval_ref.reference.to_record(),
    }
