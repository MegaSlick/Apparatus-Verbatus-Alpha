"""Derive pairwise capture comparability from Unit 5 triage facts.

Missing or malformed facts must be refused rather than defaulted to comparable.
Mode, actor identity and revision, and human override status all describe the
capture condition; any difference blocks a delta but never prefers one capture.
"""

from __future__ import annotations

from typing import Any, Final

from common.contracts.errors import SchemaRefusal
from common.contracts.stages import TRIAGE_MODES

# These names must remain reconciled with Unit 5's closed row schema.  Schema
# drift must fail the pin instead of silently making every pair comparable.
TRIAGE_FACT_FIELDS: Final = ("mode", "actor", "human_override")
ACTOR_FACT_FIELDS: Final = ("kind", "identity", "revision")
TRIAGE_ACTOR_KINDS: Final = ("human", "model", "scantailor", "producer")

MODE_DIFFERS: Final = "triage-mode-differs"
ACTOR_KIND_DIFFERS: Final = "triage-actor-kind-differs"
ACTOR_IDENTITY_DIFFERS: Final = "triage-actor-identity-differs"
ACTOR_REVISION_DIFFERS: Final = "triage-actor-revision-differs"
HUMAN_OVERRIDE_DIFFERS: Final = "triage-human-override-differs"

COMPARABILITY_DIFFERENCE_CODES: Final = frozenset(
    {
        MODE_DIFFERS,
        ACTOR_KIND_DIFFERS,
        ACTOR_IDENTITY_DIFFERS,
        ACTOR_REVISION_DIFFERS,
        HUMAN_OVERRIDE_DIFFERS,
    }
)

_ACTOR_CODES: Final = {
    "kind": ACTOR_KIND_DIFFERS,
    "identity": ACTOR_IDENTITY_DIFFERS,
    "revision": ACTOR_REVISION_DIFFERS,
}


def _triage_facts(row: Any, label: str) -> dict[str, Any]:
    """Return validated facts; an absent fact must never imply comparability."""
    if not isinstance(row, dict) or not set(TRIAGE_FACT_FIELDS) <= set(row):
        raise SchemaRefusal(
            f"capture comparability: {label} does not carry the triage decision facts "
            f"{list(TRIAGE_FACT_FIELDS)}; comparability is refused rather than assumed, "
            "because an unread capture condition is not a satisfied one"
        )
    mode = row["mode"]
    if mode not in TRIAGE_MODES:
        raise SchemaRefusal(
            f"capture comparability: {label} triage mode is not one of {TRIAGE_MODES}"
        )
    actor = row["actor"]
    if not isinstance(actor, dict) or set(actor) != set(ACTOR_FACT_FIELDS):
        raise SchemaRefusal(
            f"capture comparability: {label} triage actor does not carry {list(ACTOR_FACT_FIELDS)}"
        )
    if actor["kind"] not in TRIAGE_ACTOR_KINDS:
        raise SchemaRefusal(
            f"capture comparability: {label} triage actor kind is not one of {TRIAGE_ACTOR_KINDS}"
        )
    if not isinstance(actor["identity"], str) or not actor["identity"].strip():
        raise SchemaRefusal(
            f"capture comparability: {label} triage actor identity is not a resolved name"
        )
    if actor["kind"] == "human":
        if actor["revision"] is not None:
            raise SchemaRefusal(
                f"capture comparability: {label} human triage actor revision is not null"
            )
    elif not isinstance(actor["revision"], str) or not actor["revision"].strip():
        raise SchemaRefusal(f"capture comparability: {label} triage actor revision is not resolved")
    if not isinstance(row["human_override"], bool):
        raise SchemaRefusal(f"capture comparability: {label} human_override is not boolean")
    return {"mode": mode, "actor": actor, "human_override": row["human_override"]}


def comparability_from_triage(row_a: Any, row_b: Any) -> dict[str, Any]:
    """Return comparability and the symmetric differences that explain it.

    Callers sealing a false result must carry the same names into the pair's
    findings; an unexplained false would hide the capture-condition evidence.
    """
    facts_a = _triage_facts(row_a, "the first capture's triage row")
    facts_b = _triage_facts(row_b, "the second capture's triage row")
    codes: set[str] = set()
    if facts_a["mode"] != facts_b["mode"]:
        codes.add(MODE_DIFFERS)
    for field, code in _ACTOR_CODES.items():
        if facts_a["actor"][field] != facts_b["actor"][field]:
            codes.add(code)
    if facts_a["human_override"] != facts_b["human_override"]:
        codes.add(HUMAN_OVERRIDE_DIFFERS)
    return {"comparably_captured": not codes, "difference_codes": sorted(codes)}
