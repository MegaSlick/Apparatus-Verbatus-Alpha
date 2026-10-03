"""Direct Armarium contract tests for sealed measurement inputs."""

import pytest

from conftest import load_stage

armarium = load_stage("7_armarium", isolate_path=True)


@pytest.mark.parametrize("value", ["false", 0, None])
def test_calibration_flag_refuses_non_boolean_values(value):
    with pytest.raises(armarium.FatalAccounting, match="non-boolean"):
        armarium._typed_calibration_flag(
            {"calibrated_for_this_corpus": value}, "perlector-protocol"
        )


@pytest.mark.parametrize("value", [True, -1])
def test_calibration_sample_count_refuses_boolean_or_negative_values(value):
    with pytest.raises(armarium.FatalAccounting, match="invalid sample_count"):
        armarium._typed_sample_count(
            {"calibrated_for_this_corpus": False, "sample_count": value}, "perlector-protocol"
        )


def test_geometry_may_omit_sample_count():
    provenance = {"calibrated_for_this_corpus": False}
    assert armarium._typed_calibration_flag(provenance, "designator-geometry") is False
    assert armarium._typed_sample_count(provenance, "designator-geometry") is None
