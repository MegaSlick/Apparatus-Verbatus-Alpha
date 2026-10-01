"""The re-ask budget: a ruled ceiling in code, and the run's sealed record in use."""

from pathlib import Path
from types import SimpleNamespace

import pytest

from common.contracts.errors import ContractError
from common.contracts.stages import RECENSOR
from common.recovery import (
    DEFAULT_RECOVERY_CONFIG_PATH,
    RULED_ABSOLUTE_CAP,
    load_recovery_policy,
)
from common.runtree.store import RunTree
from common.stage import StageContext
from conftest import run_stage


def _policy(path, body: str):
    path.write_text(body, encoding="utf-8")
    return path


def test_the_shipped_policy_bounds_re_asks_per_page():
    assert load_recovery_policy()["page_level_reread"] == 1


def test_the_ruled_ceiling_accepts_three_re_asks(tmp_path):
    policy = _policy(
        tmp_path / "at-ceiling.toml", f"[budget]\npage_level_reread = {RULED_ABSOLUTE_CAP}\n"
    )

    assert load_recovery_policy(policy)["page_level_reread"] == RULED_ABSOLUTE_CAP


def test_the_ruled_ceiling_refuses_more_re_asks(tmp_path):
    policy = _policy(
        tmp_path / "over-ceiling.toml", f"[budget]\npage_level_reread = {RULED_ABSOLUTE_CAP + 1}\n"
    )

    with pytest.raises(ContractError, match="above the ruled maximum"):
        load_recovery_policy(policy)


@pytest.mark.parametrize(
    "body,message",
    [
        ("absolute_cap = 3\n[budget]\npage_level_reread = 1\n", "absolute_cap"),
        ("[budget]\nfallback_recrop = 1\npage_level_reread = 1\n", "unknown field"),
        ("[budget]\npage_level_reread = -1\n", "not a non-negative integer"),
        ("[budget]\npage_level_reread = true\n", "not a non-negative integer"),
        ("[budget]\n", "not a non-negative integer"),
    ],
)
def test_a_policy_outside_its_one_key_is_refused(tmp_path, body, message):
    with pytest.raises(ContractError, match=message):
        load_recovery_policy(_policy(tmp_path / "policy.toml", body))


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


def test_the_budget_is_sealed_ahead_of_the_re_ask_that_spends_it():
    """A deliberate forward binding: every run seals its re-ask budget at the Door,
    and no stage re-asks a page yet. When a stage starts reading
    `StageContext.recovery_policy`, this test goes and config/recovery.toml says so."""
    root = Path(__file__).resolve().parents[1]
    readers = sorted(
        str(path.relative_to(root))
        for folder in ("pipeline", "operations")
        for path in (root / folder).rglob("*.py")
        if not path.name.startswith("test_") and ".recovery_policy" in path.read_text("utf-8")
    )
    assert readers == []
