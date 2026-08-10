"""The one checked reader for the bounded recovery policy.

Recovery policy changes which artifacts the Recensor may request and which
subprocesses the orchestrator may dispatch.  It is therefore run-shaping
configuration, not a stage-local convenience file read from whichever current
directory happened to launch a command.
"""

import tomllib
from pathlib import Path
from typing import Any, Final

from common.contracts.canonical import digest_bytes
from common.contracts.errors import ContractError, FatalAccounting
from common.contracts.identities import attempt_id

DEFAULT_RECOVERY_CONFIG_PATH = Path(__file__).resolve().parents[1] / "config" / "recovery.toml"

# Tyrel's ruled ceiling. The configuration may choose a lower run budget, but it
# may not turn three bounded recovery rounds into a larger one.
RULED_ABSOLUTE_CAP: Final = 3

# The two recovery operations ARCHITECTURE and spec 09 both name as distinct: a
# fallback/expanded recrop (Designator-owned) and a page-level or
# continuation-aware reread (Perlector-owned). `config/recovery.toml` already
# budgets them separately; these are the closed vocabulary a `recovery-request`
# payload's `recovery_kind` field is drawn from, so every consumer of that field
# (the Recensor that writes it, the Designator and orchestrator that read it)
# names the same two strings rather than each inventing its own spelling.
#
# These name a coverage OPERATION, never a reading's quality. They are spelled
# the way every other durable word in this pipeline is spelled — hyphenated, like
# `recovery-requested`, `no-readable-text`, `genuinely-empty` — and are
# deliberately NOT the config field names they are budgeted under. A sealed
# artifact's vocabulary must not move because somebody renamed a TOML key, so the
# mapping below is the one place the two spellings meet.
FALLBACK_RECROP: Final = "fallback-recrop"
PAGE_LEVEL_REREAD: Final = "page-level-reread"
RECOVERY_KINDS: Final = {
    FALLBACK_RECROP: "fallback_recrop",
    PAGE_LEVEL_REREAD: "page_level_reread",
}


def load_recovery_policy(path: str | Path = DEFAULT_RECOVERY_CONFIG_PATH) -> dict[str, Any]:
    """Read one policy, validate its bounds, and return its resolved record."""
    path = Path(path)
    try:
        data = path.read_bytes()
        config = tomllib.loads(data.decode("utf-8"))
    except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError) as error:
        raise ContractError(
            f"the recovery configuration at {path} could not be read as a policy: {error}"
        ) from error
    budget = config.get("budget") if isinstance(config, dict) else None
    if not isinstance(budget, dict):
        raise ContractError("the recovery configuration has no [budget] table")
    required = ("absolute_cap", "fallback_recrop", "page_level_reread")
    values = {
        "absolute_cap": config.get("absolute_cap"),
        "fallback_recrop": budget.get("fallback_recrop"),
        "page_level_reread": budget.get("page_level_reread"),
    }
    invalid = [
        name
        for name in required
        if not isinstance(values[name], int) or isinstance(values[name], bool) or values[name] < 0
    ]
    if invalid:
        raise ContractError(
            f"the recovery configuration has invalid non-negative integer field(s) {invalid}"
        )
    if values["absolute_cap"] > RULED_ABSOLUTE_CAP:
        raise ContractError(
            f"the recovery configuration names absolute_cap {values['absolute_cap']}, above the "
            f"ruled maximum of {RULED_ABSOLUTE_CAP}; recovery is PURE ABSOLUTE, STOP AT 3"
        )
    allowed = values["fallback_recrop"] + values["page_level_reread"]
    if allowed > values["absolute_cap"]:
        raise ContractError(
            f"the configured recovery budget of {allowed} exceeds the absolute cap "
            f"of {values['absolute_cap']}. The cap is a ruling, not a default"
        )
    return {
        "config_sha256": digest_bytes(data),
        "absolute_cap": values["absolute_cap"],
        "fallback_recrop": values["fallback_recrop"],
        "page_level_reread": values["page_level_reread"],
        "allowed": allowed,
    }


def recovery_kind_budget(policy: dict[str, Any], recovery_kind: str) -> int:
    """Return one configured kind's allowance, refusing a made-up operation.

    The one reader that crosses between the artifact vocabulary and the config
    field it is budgeted under. Every consumer asks here rather than indexing the
    resolved policy with a string it built itself, so a request naming an
    operation this pipeline does not have is refused at the boundary instead of
    quietly reading as a zero budget.
    """
    field = RECOVERY_KINDS.get(recovery_kind)
    if field is None:
        raise ContractError(
            f"recovery kind {recovery_kind!r} is not one of {sorted(RECOVERY_KINDS)}"
        )
    value = policy.get(field)
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise ContractError(
            f"resolved recovery policy has no non-negative budget for {recovery_kind!r}"
        )
    return value


def reconcile_recovery_requests(
    requests: list[dict[str, Any]], act_id: str, policy: dict[str, Any]
) -> list[dict[str, Any]]:
    """One act's recovery requests in ordinal order, refusing a history that does
    not reconcile to the sealed budget.

    The one implementation of this arithmetic. The Recensor writes the requests
    and needs much more state around them (which review answered which request,
    which recrop and reread followed); the Designator and the orchestrator read
    one current request and need only this. Both ends of a bounded budget
    counting it independently is how the two ends drift apart.

    Each request's recorded counters are checked against the requests that came
    before it rather than trusted because they sit inside a self-hashed payload:
    the self-hash proves nobody edited the record after publication, not that the
    number was ever right. Ordinals must be contiguous from 1 — a missing attempt
    renumbered away would let a spent budget read as an unspent one, which is the
    one arithmetic this bounded loop cannot afford to get wrong.

    Both callers read every request through `RunTree.read_artifact`, which already
    recomputes each `artifact_id` from the stage, kind, subject and attempt it
    carries (`common/contracts/envelope.py`) and refuses a mismatch, so nothing
    here re-derives it.
    """
    by_ordinal: dict[int, dict[str, Any]] = {}
    for request in requests:
        payload = request.get("payload")
        if not isinstance(payload, dict):
            raise FatalAccounting(f"recovery request for {act_id} has no object payload")
        ordinal = payload.get("attempt_ordinal")
        if (
            request.get("outcome") != "recovery-requested"
            or not isinstance(ordinal, int)
            or isinstance(ordinal, bool)
            or request.get("attempt_id") != attempt_id(act_id, "recover", ordinal)
        ):
            raise FatalAccounting(
                f"recovery request for {act_id} does not carry its bound recovery ordinal"
            )
        # A request that does not say which of the two operations it means cannot
        # be checked against the kind-specific budget or dispatched to the right
        # owning stage, so it is refused here rather than left for the Designator
        # or the orchestrator to guess at.
        if payload.get("recovery_kind") not in RECOVERY_KINDS:
            raise FatalAccounting(
                f"recovery request for {act_id} does not carry a recognized recovery_kind "
                f"(one of {sorted(RECOVERY_KINDS)}); a request must name which recovery "
                "operation it means"
            )
        if ordinal in by_ordinal:
            raise FatalAccounting(
                f"act {act_id} carries two recovery requests for ordinal {ordinal}; recovery "
                "has no rule for choosing one"
            )
        by_ordinal[ordinal] = request

    if set(by_ordinal) != set(range(1, len(by_ordinal) + 1)):
        raise FatalAccounting(
            f"act {act_id} has non-contiguous recovery request ordinal(s) "
            f"{sorted(by_ordinal)}; a missing attempt may not be renumbered away"
        )
    ordered = [by_ordinal[ordinal] for ordinal in sorted(by_ordinal)]

    used_by_kind = {kind: 0 for kind in RECOVERY_KINDS}
    for total_used, request in enumerate(ordered):
        payload = request["payload"]
        kind = payload["recovery_kind"]
        kind_allowed = recovery_kind_budget(policy, kind)
        counters = ("budget_allowed", "budget_used", "kind_budget_allowed", "kind_budget_used")
        if (
            any(
                not isinstance(payload.get(field), int) or isinstance(payload.get(field), bool)
                for field in counters
            )
            or payload.get("recovery_policy") != policy
            or payload.get("budget_allowed") != policy["allowed"]
            or payload.get("budget_used") != total_used
            or payload.get("kind_budget_allowed") != kind_allowed
            or payload.get("kind_budget_used") != used_by_kind[kind]
        ):
            raise FatalAccounting(
                f"recovery request for {act_id} has a recorded total or kind budget that does "
                "not reconcile to its preceding immutable requests"
            )
        used_by_kind[kind] += 1

    if len(ordered) > policy["allowed"] or len(ordered) > policy["absolute_cap"]:
        raise FatalAccounting(
            f"act {act_id} has {len(ordered)} recovery request(s), above its sealed total budget"
        )
    for kind, used in used_by_kind.items():
        if used > recovery_kind_budget(policy, kind):
            raise FatalAccounting(
                f"act {act_id} has {used} {kind!r} request(s), above that kind's sealed budget"
            )
    return ordered
