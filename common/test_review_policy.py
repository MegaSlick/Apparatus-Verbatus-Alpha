"""The held-page alarm's sealed share and minimum: read exactly, compared exactly."""

from __future__ import annotations

import pytest

from common.contracts.errors import ContractError
from common.review_policy import load_review_policy, systemic


def _policy(tmp_path, share: str = "1/50", minimum: object = 2) -> dict:
    path = tmp_path / "review.toml"
    path.write_text(
        f'[review]\nmax_held_page_share = "{share}"\nmin_systemic_held_pages = {minimum}\n',
        encoding="utf-8",
    )
    return load_review_policy(path)


def test_the_committed_policy_is_one_in_fifty_from_two_held_pages():
    policy = load_review_policy()
    assert (policy["max_held_page_share"], policy["min_systemic_held_pages"]) == ("1/50", 2)


@pytest.mark.parametrize(
    "held, pages, fires",
    [(0, 50, False), (1, 50, False), (2, 100, False), (2, 99, True), (3, 100, True), (2, 2, True)],
)
def test_the_alarm_fires_only_above_the_share_never_at_it(held, pages, fires):
    assert systemic(held, pages, load_review_policy()) is fires


@pytest.mark.parametrize(
    "held, pages, minimum, fires",
    [(1, 2, 2, False), (1, 10, 2, False), (2, 10, 2, True), (2, 10, 3, False), (3, 10, 3, True)],
    ids=["one-of-two", "below", "at", "below-a-larger-minimum", "at-a-larger-minimum"],
)
def test_the_alarm_needs_the_sealed_minimum_of_held_pages(tmp_path, held, pages, minimum, fires):
    assert systemic(held, pages, _policy(tmp_path, minimum=minimum)) is fires


@pytest.mark.parametrize("share", ["0/50", "51/50", "1/0", "0.02", "1 / 50", "-1/50"])
def test_a_share_that_is_not_a_fraction_of_the_pages_is_refused(tmp_path, share):
    with pytest.raises(ContractError, match="max_held_page_share"):
        _policy(tmp_path, share=share)


@pytest.mark.parametrize("minimum", ["0", "-1", "true", '"2"', "2.0"])
def test_a_minimum_that_is_not_a_whole_number_of_pages_is_refused(tmp_path, minimum):
    with pytest.raises(ContractError, match="min_systemic_held_pages"):
        _policy(tmp_path, minimum=minimum)


@pytest.mark.parametrize(
    "body",
    [
        '[review]\nmax_held_page_share = "1/50"\n',
        '[review]\nmax_held_page_share = "1/50"\nmin_systemic_held_pages = 2\nother = 1\n',
    ],
    ids=["missing", "unknown"],
)
def test_a_review_table_without_exactly_its_two_fields_is_refused(tmp_path, body):
    path = tmp_path / "review.toml"
    path.write_text(body, encoding="utf-8")
    with pytest.raises(ContractError, match="exactly max_held_page_share and min_systemic"):
        load_review_policy(path)
