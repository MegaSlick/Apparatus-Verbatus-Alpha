"""Whether two captures of one leaf were made comparably, from Unit 5's rows.

Plan §20 states the requirement in one sentence: "a hand-cropped frame against
an auto-cropped one is itself a capture-condition difference", which is why the
re-shoot instrument "is a consumer of Unit 5's per-page mode/actor rows".  That
sentence has to become a derivation somewhere, or the ``comparably_captured``
boolean the instrument gates on degrades to whatever the first producer wiring
happens to hard-code -- and the honest wiring and the always-true wiring look
identical in a diff.

So the derivation lives here, in one place, before any producer exists to call
it.  ``common/test_capture_comparability.py`` pins two halves of that: the
derivation itself, and a source scan asserting no production module outside the
Unit 19 schema and the Unit 20 consumer names ``capture_condition`` or
``comparably_captured`` at all.  Wiring the producer without coming through this
function fails that scan.

Three triage facts are read, not two.  Plan §20 names *mode* and *actor*;
``human_override`` is included with them because a frame whose triage decision a
person overrode and a frame whose decision stood are not the same kind of crop
either, and the whole point of the field is that the difference is recorded
rather than inferred.  Reading it can only move a pair from compared to
not-compared-and-named, which leaves it in the denominator carrying a finding --
never out of it (GOVERNANCE 10, and §20's own bias warning).

This module states a difference; it never states a preference.  Neither row is
the better capture, and a pair that is not comparably captured is not a worse
pair -- it is a pair this instrument may not compute a delta for.
"""

from __future__ import annotations

from typing import Any, Final

from common.contracts.errors import SchemaRefusal
from common.contracts.stages import TRIAGE_MODES

# The exact Unit 5 decision-manifest row fields this derivation reads.  A test
# reconciles these names against `pipeline/0_triage/manifest.py`'s own closed
# row schema, so an upstream rename breaks the pin rather than silently making
# every pair comparable.
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
    """Read the three facts, refusing a row that cannot supply them.

    A refusal here is the point.  The failure this guards against is a caller
    handing over something that is not a Unit 5 row -- an empty dict, a partial
    projection, a stage record that happens to be nearby -- and receiving
    ``comparably_captured: True`` for it, which is the always-true degradation
    plan §20 forbids.  There is no default.
    """
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
    """Derive one pair's ``comparably_captured`` fact from two Unit 5 rows.

    Returns the boolean together with the named differences that produced it, so
    a producer sealing the boolean into `cross-capture-dissent.v1` can put the
    same names into that pair's ``finding_codes`` instead of asserting an
    unexplained ``False``.
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
