"""The checkout-only contract, asserted from the tree rather than from intention."""

from __future__ import annotations

import tomllib
from pathlib import Path

import pytest

from common.checkout import (
    CHECKOUT_RESOURCE_DIRECTORIES,
    REPOSITORY_ROOT,
    NotACheckoutRefusal,
    missing_checkout_resources,
    require_checkout,
)


def test_this_repository_is_a_checkout_and_passes_its_own_check() -> None:
    assert missing_checkout_resources() == ()
    assert require_checkout() == REPOSITORY_ROOT


def test_a_root_without_the_checkout_directories_is_refused(tmp_path: Path) -> None:
    """The refusal an installed wheel would meet, before any verb runs.

    An outside review installed a built wheel outside a checkout and reached a
    missing `config/data_handling_policy.json` part way through loading a
    policy. This is what that operator is told instead, and it is said before
    work starts.
    """

    with pytest.raises(NotACheckoutRefusal) as refused:
        require_checkout(tmp_path)

    detail = str(refused.value)
    assert "runs from a source checkout" in detail
    for name in CHECKOUT_RESOURCE_DIRECTORIES:
        assert name in detail
    assert str(tmp_path) in detail


def test_a_partial_root_names_only_what_is_missing(tmp_path: Path) -> None:
    (tmp_path / "config").mkdir()
    (tmp_path / "pipeline").mkdir()

    assert missing_checkout_resources(tmp_path) == ("proof",)
    with pytest.raises(NotACheckoutRefusal, match="proof"):
        require_checkout(tmp_path)


def test_gold_is_not_required_to_start() -> None:
    """Real comparison material is not a precondition for a run.

    Demanding it would turn a legitimate run into a refusal, which is the
    opposite of what this check is for.
    """

    assert "gold" not in CHECKOUT_RESOURCE_DIRECTORIES


def test_the_console_entry_refuses_before_it_imports_the_application(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """`verbatus` says `not-a-checkout` and starts nothing."""

    from operations.operator import entry

    loaded = []
    monkeypatch.setattr(entry, "require_checkout", lambda: require_checkout(tmp_path))
    monkeypatch.setattr(entry, "_load_application", lambda: loaded.append(True))

    assert entry.main(["status"]) == 2
    assert loaded == []
    printed = capsys.readouterr().out
    assert "not a source checkout" in printed
    assert "Nothing was started and nothing was charged." in printed


def test_the_packaging_configuration_still_matches_the_declared_contract() -> None:
    """No `pipeline`/`config`/`proof` is packaged, and no package data smuggles them.

    If this ever changes, the contract in `operations/README.md` and the refusal
    in `common/checkout.py` are both wrong and must change with it — which is
    the point of failing here rather than discovering it from a wheel.
    """

    configuration = tomllib.loads((REPOSITORY_ROOT / "pyproject.toml").read_text())
    find = configuration["tool"]["setuptools"]["packages"]["find"]
    assert find["include"] == ["common", "common.*", "operations", "operations.*"]
    assert find["namespaces"] is False
    assert "package-data" not in configuration["tool"]["setuptools"]
    assert not (REPOSITORY_ROOT / "MANIFEST.in").exists()
