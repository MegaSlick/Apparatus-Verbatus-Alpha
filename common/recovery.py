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
