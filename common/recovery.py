"""The one checked reader for the re-ask budget in `config/recovery.toml`.

`[budget] page_level_reread` bounds how many times a page may be asked again.
The run seals the file as `recovery` at the door, and every reader takes that
sealed record rather than reading the file again.
"""

from pathlib import Path
from typing import Any, Final

from common.contracts.errors import ContractError
from common.sealed_config import read_sealed_toml

DEFAULT_RECOVERY_CONFIG_PATH = Path(__file__).resolve().parents[1] / "config" / "recovery.toml"

# The ceiling on re-asks of one page; the configuration may choose less, never more.
RULED_ABSOLUTE_CAP: Final = 3


def load_recovery_policy(path: str | Path = DEFAULT_RECOVERY_CONFIG_PATH) -> dict[str, Any]:
    """Read the policy, validate its one bound, and return its resolved record."""
    config, digest = read_sealed_toml(path, "recovery configuration", {"budget"})
    budget = config.get("budget")
    if not isinstance(budget, dict):
        raise ContractError("the recovery configuration has no [budget] table")
    unknown = sorted(set(budget) - {"page_level_reread"})
    if unknown:
        raise ContractError(f"the recovery configuration's [budget] has unknown field(s) {unknown}")
    rereads = budget.get("page_level_reread")
    if not isinstance(rereads, int) or isinstance(rereads, bool) or rereads < 0:
        raise ContractError(
            "the recovery configuration's page_level_reread is not a non-negative integer"
        )
    if rereads > RULED_ABSOLUTE_CAP:
        raise ContractError(
            f"the recovery configuration names page_level_reread {rereads}, above the ruled "
            f"maximum of {RULED_ABSOLUTE_CAP}"
        )
    return {"config_sha256": digest, "page_level_reread": rereads}
