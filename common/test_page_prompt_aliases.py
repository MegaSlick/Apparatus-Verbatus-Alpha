"""The Perlector's variant serving recipes (FP8, MTP, FP8 KV cache) take the base recipe's
page and re-ask prompts, so a chair can point at one (review C7, 2026-10-09).

Synthetic feed only; no model, no network.
"""

import tomllib
from pathlib import Path

import pytest

from common import page_prompt
from operations.bakeoff.test_bakeoff_fed_arm import _feed, _png

ROOT = Path(__file__).resolve().parents[1]
BASE = "unproven-real-perlector"


def _variant_recipes() -> list[str]:
    raw = tomllib.loads((ROOT / "config" / "serving_recipes_real_variants.toml").read_text())
    return sorted({row["recipe"] for row in raw["profiles"] if row.get("chair") == "perlector"})


def test_every_perlector_variant_recipe_has_the_base_prompt():
    recipes = _variant_recipes()
    assert recipes and set(recipes) <= set(page_prompt.RECIPE_ALIASES)
    png = _png("p")
    feed = _feed(1, "4_perlector/blobs/sha256/x", png, "Le dix mai", "Le dix mars")
    base_text = page_prompt.build_page_prompt(BASE, feed)
    base = page_prompt.page_prompt_evidence(BASE, feed)
    reask = {"prior_entries": [], "named": []}
    for recipe in recipes:
        assert page_prompt.build_page_prompt(recipe, feed) == base_text
        assert page_prompt.prompt_parts(recipe, feed) == page_prompt.prompt_parts(BASE, feed)
        evidence = page_prompt.page_prompt_evidence(recipe, feed)
        assert evidence == {**base, "serving_recipe": recipe}  # it names the recipe asked for
        assert page_prompt.page_reask_prompt(recipe, feed, reask) == page_prompt.page_reask_prompt(
            BASE, feed, reask
        )


def test_an_unregistered_recipe_is_still_refused():
    feed = _feed(1, "x", _png("p"), "a", "b")
    with pytest.raises(ValueError, match="no declared page prompt builder"):
        page_prompt.build_page_prompt("unproven-real-perlector-something-else", feed)
