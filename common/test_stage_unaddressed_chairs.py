"""`unaddressed_chairs` must know about every role a stage actually resolves.

The real roster's `secondary_proposer` is configured `absent` today, which
already keeps it out of the unaddressed set on its own (an absence is a
recorded decision). What only this test proves is the case that matters
before anyone flips that roster to a real detector: a *configured*
`secondary_proposer` must also be addressed, because `pipeline/2_designator/run.py`
resolves it every run. Without `SECONDARY_PROPOSER_CHAIR` in `unaddressed_chairs`'
own addressed set, enabling the real roster would turn every run `partial` the
first time it ran, for a reason nothing in the diff that enabled it would show.
"""

from pathlib import Path

from common.chairs.config import parse_models_config
from common.stage import (
    DESIGNATOR_CHAIR,
    PERLECTOR_CHAIR,
    SECONDARY_PROPOSER_CHAIR,
    unaddressed_chairs,
)


def _absent(reason: str = "fixture") -> dict:
    return {"state": "absent", "reason": reason}


def _configured(role: str) -> dict:
    return {
        "state": "configured",
        "source": "local-repository",
        "path": role,
        "digest_manifest": "a" * 64,
        "manifest": f"manifests/{role}.json",
        "serving_recipe": "fixture-recipe-v0",
        "license_note": "fixture only",
    }


def _config(chairs: dict, **top):
    raw = {"witness_floor": 1, "model_root": "model-fixtures", "chairs": chairs}
    raw.update(top)
    return parse_models_config(raw, source_path=None)


def test_the_shipped_models_toml_has_nothing_unaddressed():
    from common.chairs.config import load_models_toml

    config = load_models_toml(Path(__file__).resolve().parents[1] / "config" / "models.toml")
    assert unaddressed_chairs(config) == ()


def test_an_absent_secondary_proposer_is_addressed_by_its_own_absence():
    config = _config(
        {
            DESIGNATOR_CHAIR: _configured(DESIGNATOR_CHAIR),
            PERLECTOR_CHAIR: _configured(PERLECTOR_CHAIR),
            SECONDARY_PROPOSER_CHAIR: _absent(),
        }
    )
    assert unaddressed_chairs(config) == ()


def test_a_configured_secondary_proposer_is_addressed_not_flagged_partial():
    """The case that matters: enabling the real roster must not need a second
    fix here, because the resolution path already exists before the flip."""
    config = _config(
        {
            DESIGNATOR_CHAIR: _configured(DESIGNATOR_CHAIR),
            PERLECTOR_CHAIR: _configured(PERLECTOR_CHAIR),
            SECONDARY_PROPOSER_CHAIR: _configured(SECONDARY_PROPOSER_CHAIR),
        }
    )
    assert unaddressed_chairs(config) == ()


def test_a_genuinely_unaddressed_role_is_still_caught():
    """A regression guard on the fix itself: the check must not have gone vacuous."""
    config = _config(
        {
            DESIGNATOR_CHAIR: _configured(DESIGNATOR_CHAIR),
            PERLECTOR_CHAIR: _configured(PERLECTOR_CHAIR),
            SECONDARY_PROPOSER_CHAIR: _absent(),
            "misspelt_role": _configured("misspelt_role"),
        }
    )
    assert unaddressed_chairs(config) == ("misspelt_role",)
