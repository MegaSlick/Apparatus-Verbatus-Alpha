"""The Perlector audit declaration's schemas, the truncation verdict and a call-record decoder.

`pipeline/4_perlector/audit.py` loads the sealed audit declaration under `SCHEMA`;
every `page-reading` records it as not run (`audit_not_run`). `common/truncation.py`
classifies a reading with the truncation rule below, and the page accounting reads
its verdicts.
`decode_recorded_generation` restores a retained call record's generation so the
sent request can be rebuilt.
"""

from __future__ import annotations

from typing import Any, Final

from common.contracts.errors import ContractError, SchemaRefusal
from common.decoding import decoded_wire_decimals

SCHEMA: Final = "perlector-audit.v3"

# The truncation instrument's verdicts; `common/truncation.py` classifies with them.
TRUNCATION_COMPLETE: Final = "complete"
TRUNCATION_TRUNCATED: Final = "truncated"
TRUNCATION_UNKNOWN: Final = "unknown"


def audit_not_run(audit_policy: dict[str, Any], audit_sha256: str) -> dict[str, Any]:
    """What every `page-reading` says about the sealed Pass-C audit: it did not run.

    Stage 4 writes it and the page-read denominator requires it, so a reading can
    claim no other audit state.
    """
    return {
        "state": "not-run",
        "round_cap": audit_policy["round_cap"],
        "policy_sha256": audit_sha256,
        "reason": "Pass C flags and re-proves spans of a reading; a whole-page reading does "
        "not run it",
    }


def length_judged(*, page_pixels: int, legible_page_pixels: int) -> bool:
    """Whether the length signal applies: the page the reading is of is legible-sized.

    The floor is a density per page, a page's lines times a line's characters; a
    page below the sealed legible size cannot hold that many lines, so there is
    no density for a reading to fall short of.
    """
    return page_pixels >= legible_page_pixels


def length_signal(*, characters: int, region_pixels: int, page_pixels: int, floor: int) -> bool:
    """The truncation length signal, as a pure function of its four terms.

    The reading's characters, scaled from its region to the page's area, against
    the sealed floor. An empty reading is never suspicious: that outcome is
    `no-readable-text`, decided elsewhere. Whether it applies at all is
    `length_judged`.
    """
    return characters > 0 and characters * page_pixels < floor * region_pixels


def truncation_classification(signals: dict[str, Any]) -> str:
    """The truncation instrument's verdict, as a pure function of its four signals.

    The engine's `length` is authoritative for `truncated`; every judged computed
    signal suspicious is `truncated`; a clean vote of the judged signals under a
    declared `stop` is `complete`; anything else is `unknown`, which holds. A
    length signal that was not judged is `None`: neutral, neither a clean nor a
    suspicious vote.
    """
    declared = signals["stop_reason_declared"]
    if declared == "length":
        return TRUNCATION_TRUNCATED
    judged = [
        signals[name]
        for name in ("unclosed_structure", "length_suspicious", "ends_abruptly")
        if signals[name] is not None
    ]
    suspicious = sum(bool(vote) for vote in judged)
    if suspicious == len(judged) and suspicious > 0:
        return TRUNCATION_TRUNCATED
    if suspicious == 0 and declared == "stop":
        return TRUNCATION_COMPLETE
    return TRUNCATION_UNKNOWN


def decode_recorded_generation(value: Any) -> Any:
    """Restore the JSON-native generation values retained by ChairClient.

    Call records replace native floats with their exact shortest wire decimal
    because canonical artifacts reject floats. Request validation must undo
    that transcription before rebuilding the HTTP bytes; serializing the tag
    itself proves a different request and rejects every legitimate float.
    """
    try:
        return decoded_wire_decimals(value)
    except ContractError as error:
        raise SchemaRefusal(
            f"a retained call record has a malformed wire decimal: {error}"
        ) from error
