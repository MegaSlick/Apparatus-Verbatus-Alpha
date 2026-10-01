"""The one checked reader for the held-page alarm in `config/review.toml`.

`[review] max_held_page_share` is the share of a run's pages that may stay
held after the Recensor before the run itself is treated as having a
systemic problem. The Door seals the file as `review`; the orchestrator
reads it where it stops at a held Recensor and checks it against that seal.
"""

from __future__ import annotations

import re
from fractions import Fraction
from pathlib import Path
from typing import Any, Final

from common.contracts.errors import ContractError
from common.sealed_config import read_sealed_toml

DEFAULT_REVIEW_CONFIG_PATH: Final = Path(__file__).resolve().parents[1] / "config" / "review.toml"
SEALED_CONFIG_NAME: Final = "review"
_SHARE: Final = re.compile(r"([1-9][0-9]{0,8})/([1-9][0-9]{0,8})")


def load_review_policy(path: str | Path = DEFAULT_REVIEW_CONFIG_PATH) -> dict[str, Any]:
    """Read the policy, validate its one share, and return its resolved record."""
    config, digest = read_sealed_toml(path, "review configuration", {"review"})
    review = config.get("review")
    if not isinstance(review, dict) or set(review) != {"max_held_page_share"}:
        raise ContractError(
            "the review configuration has no [review] table holding exactly max_held_page_share"
        )
    text = review["max_held_page_share"]
    match = _SHARE.fullmatch(text) if isinstance(text, str) else None
    if match is None or int(match[1]) > int(match[2]):
        raise ContractError(
            f"the review configuration's max_held_page_share {text!r} is not a fraction "
            '"N/D" with 0 < N <= D'
        )
    return {
        "config_sha256": digest,
        "max_held_page_share": text,
        "share": Fraction(int(match[1]), int(match[2])),
    }


def systemic(held_pages: int, pages: int, policy: dict[str, Any]) -> bool:
    """Whether `held_pages` of `pages` is more than the sealed share: a problem with the run."""
    if pages <= 0 or not 0 <= held_pages <= pages:
        raise ContractError(f"{held_pages} held of {pages} pages is not a share of a run")
    return Fraction(held_pages, pages) > policy["share"]


def alarm_line(run_id: str, held_pages: list[int], pages: int, policy: dict[str, Any]) -> str:
    """The one line a run's report and its notification carry when the alarm fires."""
    return (
        f"run {run_id}: systemic: {len(held_pages)} of {pages} page(s) are held after the "
        f"recensor, more than the sealed limit of {policy['max_held_page_share']} "
        f"(config/review.toml); so many holds point to a problem with the run itself, not a "
        f"few hard pages (held pages: {', '.join(str(page) for page in held_pages)})"
    )
