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
from common.contracts.errors import ContractError

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
