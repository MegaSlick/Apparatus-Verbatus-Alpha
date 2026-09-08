"""The serving-recipes CLI choice is a run-sealed input, like models-config."""

from __future__ import annotations

from pathlib import Path

from common.chairs.models import is_witness_role
from common.chairs.registry import ChairRegistry
from common.contracts.canonical import digest_bytes
from common.stage import load_fixture, run_config_bindings, stage_parser
from operations.serving.config import load_serving_recipes


def test_serving_recipes_flag_defaults_to_fixture_catalogue_and_selects_real_bytes_explicitly():
    root = Path(__file__).resolve().parents[1]
    fixture_catalogue = root / "config/serving_recipes.toml"
    real_catalogue = root / "config/serving_recipes_real.toml"
    parser = stage_parser("serving config flag")
    default = parser.parse_args(["--run-root", "runs", "--run-id", "r"])
    selected = parser.parse_args(
        [
            "--run-root",
            "runs",
            "--run-id",
            "r",
            "--serving-recipes-config",
            str(real_catalogue),
        ]
    )

    assert Path(default.serving_recipes_config) == fixture_catalogue.resolve()
    assert Path(selected.serving_recipes_config) == real_catalogue
    models = ChairRegistry.from_toml(root / "config/models.toml").config
    fixture = load_fixture(root / "proof")
    baseline = run_config_bindings(
        models, fixture, "happy", serving_recipes_config_path=default.serving_recipes_config
    )
    alternate = run_config_bindings(
        models, fixture, "happy", serving_recipes_config_path=selected.serving_recipes_config
    )

    assert baseline["serving_config_inputs"]["serving_recipes_sha256"] == digest_bytes(
        fixture_catalogue.read_bytes()
    )
    assert alternate["serving_config_inputs"]["serving_recipes_sha256"] == digest_bytes(
        real_catalogue.read_bytes()
    )
    assert baseline["config_digest"] != alternate["config_digest"]


def test_the_real_catalogues_generation_config_values_are_admitted_and_witness_scoped():
    """Regression guard tying config.py's schema rule to the shipped rows.

    `generation_config = "auto"` is admitted only for a witness (Attestator)
    row (`config.py`); U15 is the unit that actually flips a real row to it.
    Until then every row here is `"vllm"`, and this test still holds once one
    changes -- it does not merely record today's committed value.
    """

    root = Path(__file__).resolve().parents[1]
    inspected = 0
    for catalogue_path in (
        root / "config/serving_recipes.toml",
        root / "config/serving_recipes_real.toml",
    ):
        catalogue = load_serving_recipes(catalogue_path)
        for profile in catalogue.profiles:
            generation_config = getattr(profile, "generation_config", None)
            if generation_config is None:
                continue  # fixture/unsupported rows carry no vLLM flags at all
            inspected += 1
            assert generation_config in {"vllm", "auto"}
            if generation_config == "auto":
                assert is_witness_role(profile.chair), (
                    f"{catalogue_path.name} chair={profile.chair!r} uses generation_config="
                    "'auto' but is not a witness role"
                )
    # A catalogue with no `generation_config` field anywhere would pass this
    # test vacuously -- every row taking the `continue` above -- and prove
    # nothing about the rule it names. At least one row must actually carry
    # the field for the loop above to have inspected anything.
    assert inspected > 0, "no profile in either catalogue carries generation_config"
