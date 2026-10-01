"""The re-ask budget: a ceiling in code, and the run's sealed record in use."""

from pathlib import Path
from types import SimpleNamespace

import pytest

from common.contracts.errors import ContractError
from common.contracts.stages import RECENSOR
from common.recovery import (
    DEFAULT_RECOVERY_CONFIG_PATH,
    REREAD_CEILING,
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


def test_the_ruled_ceiling_accepts_one_re_ask(tmp_path):
    policy = _policy(
        tmp_path / "at-ceiling.toml", f"[budget]\npage_level_reread = {REREAD_CEILING}\n"
    )

    assert load_recovery_policy(policy)["page_level_reread"] == REREAD_CEILING == 1


def test_the_ruled_ceiling_refuses_more_re_asks(tmp_path):
    policy = _policy(
        tmp_path / "over-ceiling.toml", f"[budget]\npage_level_reread = {REREAD_CEILING + 1}\n"
    )

    with pytest.raises(ContractError, match="above the maximum"):
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


def test_the_page_re_ask_planner_is_the_only_reader_of_the_budget():
    """The sealed budget is spent by the page re-ask alone.

    Every stage line that reads `StageContext.recovery_policy` hands it straight
    to `common.page_reask.reask_budget` (the Perlector that plans the re-ask, and
    the denominator that plans it again to verify it), and only that function
    and the loader read `page_level_reread` out of the record.
    """
    root = Path(__file__).resolve().parents[1]
    sources = {
        str(path.relative_to(root)): path.read_text("utf-8")
        for folder in ("common", "pipeline", "operations")
        for path in (root / folder).rglob("*.py")
        if not path.name.startswith("test_")
    }
    readers = sorted(
        (name, line.strip())
        for name, text in sources.items()
        for line in text.splitlines()
        if ".recovery_policy" in line
    )
    assert readers, "no stage reads the sealed recovery policy"
    assert all("page_reask.reask_budget(" in line for _name, line in readers), readers
    assert {name for name, _line in readers} == {
        "common/stage.py",
        "pipeline/4_perlector/page_run.py",
    }
    budget_readers = sorted(
        name
        for name, text in sources.items()
        if '["page_level_reread"]' in text or '.get("page_level_reread")' in text
    )
    assert budget_readers == ["common/page_reask.py", "common/recovery.py"]
