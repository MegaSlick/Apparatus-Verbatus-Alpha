"""The ruled recovery ceiling is code-owned, not a mutable configuration preference."""

import pytest

from common.contracts.errors import ContractError
from common.recovery import RULED_ABSOLUTE_CAP, load_recovery_policy


def _policy(path, *, absolute_cap: int, fallback_recrop: int = 0, page_level_reread: int = 0):
    path.write_text(
        "\n".join(
            (
                f"absolute_cap = {absolute_cap}",
                "[budget]",
                f"fallback_recrop = {fallback_recrop}",
                f"page_level_reread = {page_level_reread}",
                "",
            )
        ),
        encoding="utf-8",
    )
    return path


def test_the_ruled_recovery_ceiling_accepts_three(tmp_path):
    policy = _policy(
        tmp_path / "at-ceiling.toml",
        absolute_cap=RULED_ABSOLUTE_CAP,
        fallback_recrop=1,
        page_level_reread=2,
    )

    assert load_recovery_policy(policy)["absolute_cap"] == RULED_ABSOLUTE_CAP


def test_the_ruled_recovery_ceiling_refuses_a_larger_configured_cap(tmp_path):
    policy = _policy(tmp_path / "over-ceiling.toml", absolute_cap=RULED_ABSOLUTE_CAP + 1)

    with pytest.raises(ContractError, match="STOP AT 3"):
        load_recovery_policy(policy)
