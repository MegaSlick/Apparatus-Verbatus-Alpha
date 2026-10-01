"""Direct Armarium contract tests for sealed measurement inputs."""

import importlib.util
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
ARMARIUM_DIR = ROOT / "pipeline" / "7_armarium"
_STAGE_MODULE_NAMES = ("armarium_export", "display", "textnorm")
_MISSING = object()


def _load_armarium_contract_run():
    """Load Armarium's CLI without leaking its bare sibling-import environment.

    ``run.py`` adds its own directory while it imports ``armarium_export``.  A
    persistent entry would make a later Perlector ``import run`` resolve this
    stage's CLI during collection.  The three bare names below are Armarium's
    complete sibling-import chain; shared ``common.*`` modules stay cached so
    their classes retain their single process identity.
    """
    spec = importlib.util.spec_from_file_location("armarium_contract_run", ARMARIUM_DIR / "run.py")
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    original_path = list(sys.path)
    previous_modules = {
        name: sys.modules.get(name, _MISSING) for name in (*_STAGE_MODULE_NAMES, spec.name)
    }
    try:
        sys.path[:0] = [str(ROOT), str(ARMARIUM_DIR)]
        for name in _STAGE_MODULE_NAMES:
            sys.modules.pop(name, None)
        sys.modules[spec.name] = module
        spec.loader.exec_module(module)
    finally:
        sys.path[:] = original_path
        for name, previous in previous_modules.items():
            if previous is _MISSING:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = previous
    return module


armarium = _load_armarium_contract_run()


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
