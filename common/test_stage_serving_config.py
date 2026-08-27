"""The serving-recipes CLI choice is a run-sealed input, like models-config."""

from __future__ import annotations

from pathlib import Path

from common.chairs.registry import ChairRegistry
from common.contracts.canonical import digest_bytes
from common.stage import load_fixture, run_config_bindings, stage_parser


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
