"""The capacity plan: widths derived from the measured card, never below the row."""

from __future__ import annotations

import json
import tomllib
from dataclasses import replace
from decimal import Decimal
from pathlib import Path

import pytest

from common.chairs.config import load_models_toml
from operations.pod.preflight import load_placement_table
from operations.serving.capacity import (
    CapacityPlan,
    derive_capacity_plan,
    derive_max_num_seqs,
    plan_from_argument,
)
from operations.serving.config import (
    ServingConfigInputs,
    ServingProfile,
    load_serving_recipes,
    parse_serving_recipes,
)
from operations.serving.errors import ServingConfigurationError

ROOT = Path(__file__).resolve().parents[2]
RECIPES = load_serving_recipes(ROOT / "config" / "serving_recipes_real.toml")
MODELS = load_models_toml(ROOT / "config" / "models-real.toml")
PLACEMENT = load_placement_table(ROOT / "config" / "pod_placement.toml")
INPUTS = ServingConfigInputs("a" * 64, "b" * 64)


def _plan(vram_gib: int) -> CapacityPlan:
    tier = PLACEMENT.choose(Decimal(vram_gib))
    return derive_capacity_plan(
        vram_gib=Decimal(vram_gib),
        gpu_count=1,
        compute_capability="9.0",
        tier=tier.identifier,
        engine_memory_fraction=tier.recipe.engine_memory_fraction,
        recipes=RECIPES,
        chairs=MODELS.chairs,
        serving_config_inputs=INPUTS,
    )


# (row max_num_seqs, planned max_num_seqs) per chair, from the shipped rows' own
# weights and KV figures with 4 GiB of engine overhead. DAI is attestator_2, Churro
# attestator_3, the Qwen3.8-27B the Perlector and the reconstructor. Chandra
# (attestator_1) states no KV figure and is never planned.
DERIVED = {
    24: {"attestator_2": (1, 4), "attestator_3": (1, 2)},
    48: {"attestator_2": (2, 38), "attestator_3": (2, 24)},
    80: {
        "attestator_2": (8, 64),
        "attestator_3": (8, 54),
        "perlector": (4, 4),
        "reconstructor": (8, 8),
    },
    96: {
        "attestator_2": (8, 64),
        "attestator_3": (8, 64),
        "perlector": (4, 7),
        "reconstructor": (8, 8),
    },
    141: {
        "attestator_2": (8, 64),
        "attestator_3": (8, 64),
        "perlector": (4, 17),
        "reconstructor": (8, 17),
    },
}


@pytest.mark.parametrize("vram_gib", sorted(DERIVED))
def test_derived_widths_per_card(vram_gib):
    plan = _plan(vram_gib)
    observed = {
        role: (chair.row_max_num_seqs, chair.max_num_seqs) for role, chair in plan.chairs.items()
    }
    assert observed == DERIVED[vram_gib]
    assert "attestator_1" not in plan.chairs


def test_the_row_is_the_floor_however_small_the_card():
    # A card with no room at all still gets the row's own width.
    assert (
        derive_max_num_seqs(
            vram_gib=Decimal("20"),
            memory_fraction=Decimal("0.9"),
            weights_gib=Decimal("51.7"),
            kv_gib_per_seq=Decimal("4"),
            row_max_num_seqs=4,
        )
        == 4
    )
    for vram_gib in DERIVED:
        for chair in _plan(vram_gib).chairs.values():
            assert chair.max_num_seqs >= chair.row_max_num_seqs


def test_the_width_is_capped():
    assert (
        derive_max_num_seqs(
            vram_gib=Decimal("1000"),
            memory_fraction=Decimal("0.9"),
            weights_gib=Decimal("1"),
            kv_gib_per_seq=Decimal("0.1"),
            row_max_num_seqs=2,
            cap=16,
        )
        == 16
    )


def test_the_budget_is_the_fraction_the_row_launches_with():
    # Churro's 24 GB row launches at 0.58 of the card, under the tier's 0.90:
    # 0.58 x 24 - 7 - 4 = 2.92 GiB, two sequences of 1.1 GiB.
    churro = _plan(24).chairs["attestator_3"]
    assert churro.memory_fraction == Decimal("0.58")
    assert churro.max_num_seqs == 2


def test_the_plan_round_trips_and_its_digest_binds_it():
    plan = _plan(96)
    again = plan_from_argument(plan.to_argument())
    assert again == plan and again.digest == plan.digest
    record = json.loads(plan.to_argument())
    record["chairs"]["perlector"]["max_num_seqs"] = 9
    with pytest.raises(ServingConfigurationError, match="digest"):
        CapacityPlan.from_record(record)
    assert plan_from_argument(None) is None


def test_a_plan_below_the_floor_is_refused():
    record = json.loads(_plan(96).to_argument())
    record["chairs"]["perlector"]["max_num_seqs"] = 2
    with pytest.raises(ServingConfigurationError, match="floor"):
        CapacityPlan.from_record(record)


def _row(role: str, tier: str) -> ServingProfile:
    row = RECIPES.for_identity(MODELS.chairs[role], tier)
    assert isinstance(row, ServingProfile)
    return row


def test_launch_profile_widens_only_max_num_seqs():
    plan = _plan(96)
    row = _row("perlector", "generic-80gb-plus")
    launched = plan.launch_profile(row, "perlector")
    assert launched.max_num_seqs == 7
    assert launched == type(row)(**{**_fields(row), "max_num_seqs": 7})
    chandra = _row("attestator_1", "generic-80gb-plus")
    assert plan.launch_profile(chandra, "attestator_1") is chandra


def _fields(row: ServingProfile) -> dict:
    return {name: getattr(row, name) for name in row.__slots__}


def test_launch_profile_refuses_a_plan_for_another_row_or_tier():
    plan = _plan(48)
    with pytest.raises(ServingConfigurationError, match="tier"):
        plan.launch_profile(_row("attestator_2", "generic-80gb-plus"), "attestator_2")
    row = _row("attestator_2", "generic-48gb")
    with pytest.raises(ServingConfigurationError, match="max_num_seqs"):
        plan.launch_profile(replace(row, max_num_seqs=3), "attestator_2")


def test_a_plan_from_other_serving_configuration_is_refused():
    plan = _plan(80)
    plan.require_inputs(INPUTS)
    with pytest.raises(ServingConfigurationError, match="sealed"):
        plan.require_inputs(ServingConfigInputs("c" * 64, "b" * 64))


@pytest.mark.parametrize("value", ["0", "-1", "abc", 4, 0.47])
def test_a_row_s_capacity_figures_must_be_positive_decimal_strings(value):
    raw = tomllib.loads((ROOT / "config" / "serving_recipes_real.toml").read_text())
    row = next(item for item in raw["profiles"] if "kv_gib_per_seq" in item)
    row["kv_gib_per_seq"] = value
    with pytest.raises(ServingConfigurationError, match="kv_gib_per_seq"):
        parse_serving_recipes(raw)
