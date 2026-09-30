"""The re-ask budget: a ruled ceiling in code, and the run's sealed record in use."""

from types import SimpleNamespace

import pytest

from common.contracts.errors import ContractError, FatalAccounting
from common.contracts.identities import attempt_id
from common.contracts.stages import RECENSOR
from common.recovery import (
    DEFAULT_RECOVERY_CONFIG_PATH,
    FALLBACK_RECROP,
    RULED_ABSOLUTE_CAP,
    load_recovery_policy,
    reconcile_recovery_requests,
)
from common.runtree.store import RunTree
from common.stage import StageContext
from conftest import run_stage


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


def test_a_context_without_a_sealed_recovery_policy_refuses_rather_than_reading_as_zero():
    """A missing budget must not read as a zero budget: zero is the one wrong
    answer that looks like an answer."""
    context = StageContext(
        tree=None,
        run={},
        fixture={},
        scenario="happy",
        stage=RECENSOR,
        adapter_revision="unused",
        args=SimpleNamespace(),
        registry=None,
    )
    with pytest.raises(ContractError, match="no run-sealed recovery policy"):
        assert context.recovery_policy


def test_the_run_authority_names_the_recovery_policy_it_was_sealed_under(tmp_path):
    """Recorded, not merely hashed: a reader holding the tree can name the file."""
    root = tmp_path / "runs"
    recovery_path = tmp_path / "recovery.toml"
    # A policy other than the shipped one, so the digest proves which file was sealed.
    recovery_path.write_text(
        DEFAULT_RECOVERY_CONFIG_PATH.read_text(encoding="utf-8").replace(
            "page_level_reread = 1", "page_level_reread = 0"
        ),
        encoding="utf-8",
    )
    assert (
        load_recovery_policy(recovery_path)["config_sha256"]
        != load_recovery_policy()["config_sha256"]
    )
    result = run_stage(
        root,
        "named",
        "happy",
        "pipeline/1_exemplar/door.py",
        fixture_root="proof",
        recovery_config=recovery_path,
    )
    assert result.returncode == 0, result.stderr

    run = RunTree(root, "named").read_run()
    assert (
        run["sealed_config_digests"]["recovery"]
        == load_recovery_policy(recovery_path)["config_sha256"]
    )


POLICY = load_recovery_policy()


def _requests(count: int) -> list[dict]:
    """`count` fallback-recrop requests for one act, each counter reconciled to its predecessors."""
    return [
        {
            "outcome": "recovery-requested",
            "attempt_id": attempt_id("act_1", "recover", ordinal),
            "payload": {
                "attempt_ordinal": ordinal,
                "recovery_kind": FALLBACK_RECROP,
                "budget_allowed": POLICY["allowed"],
                "budget_used": ordinal - 1,
                "kind_budget_allowed": POLICY["fallback_recrop"],
                "kind_budget_used": ordinal - 1,
                "recovery_policy": POLICY,
            },
        }
        for ordinal in range(1, count + 1)
    ]


def test_requests_within_the_sealed_budget_reconcile_in_ordinal_order():
    requests = _requests(POLICY["fallback_recrop"])
    assert reconcile_recovery_requests(list(reversed(requests)), "act_1", POLICY) == requests


def test_a_request_above_the_sealed_budget_is_refused_at_the_accounting_boundary():
    with pytest.raises(FatalAccounting, match="above"):
        reconcile_recovery_requests(_requests(POLICY["fallback_recrop"] + 1), "act_1", POLICY)
