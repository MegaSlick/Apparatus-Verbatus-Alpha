"""The held-page alarm's sealed share: read exactly, compared exactly."""

from __future__ import annotations

import pytest

from common.contracts.errors import ContractError
from common.review_policy import load_review_policy, systemic


def _policy(tmp_path, share: str) -> dict:
    path = tmp_path / "review.toml"
    path.write_text(f'[review]\nmax_held_page_share = "{share}"\n', encoding="utf-8")
    return load_review_policy(path)


def test_the_committed_share_is_the_ruled_one_in_fifty():
    assert load_review_policy()["max_held_page_share"] == "1/50"


@pytest.mark.parametrize(
    "held, pages, fires",
    [(0, 50, False), (1, 50, False), (2, 100, False), (2, 99, True), (3, 100, True), (1, 2, True)],
)
def test_the_alarm_fires_only_above_the_share_never_at_it(held, pages, fires):
    assert systemic(held, pages, load_review_policy()) is fires


@pytest.mark.parametrize("share", ["0/50", "51/50", "1/0", "0.02", "1 / 50", "-1/50"])
def test_a_share_that_is_not_a_fraction_of_the_pages_is_refused(tmp_path, share):
    with pytest.raises(ContractError, match="max_held_page_share"):
        _policy(tmp_path, share)


def test_an_unknown_field_is_refused(tmp_path):
    path = tmp_path / "review.toml"
    path.write_text('[review]\nmax_held_page_share = "1/50"\nother = 1\n', encoding="utf-8")
    with pytest.raises(ContractError, match="exactly max_held_page_share"):
        load_review_policy(path)
