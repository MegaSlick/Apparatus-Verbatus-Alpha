"""Direct Armarium contract tests for sealed measurement inputs."""

import importlib.util
import sys
from pathlib import Path
from types import SimpleNamespace

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


def _conservation_context(rows):
    artifacts = {f"c-{index}": {"payload": row} for index, row in enumerate(rows)}
    return SimpleNamespace(
        tree=SimpleNamespace(
            build_manifest=lambda _stage: {
                "artifacts": [{"kind": "conservation", "artifact_id": key} for key in artifacts]
            },
            read_artifact=lambda _stage, _kind, artifact_id: artifacts[artifact_id],
        )
    )


@pytest.mark.parametrize(
    "rows, sealed, expected",
    [
        (
            [{"page_ordinal": 1, "ink_measurable": False, "reason": "background unavailable"}],
            {1},
            {1: "background unavailable"},
        ),
        ([{"page_ordinal": 1, "ink_measurable": True}], {1}, {}),
    ],
)
def test_conservation_exact_census_keeps_legitimate_degraded_sealed_pages(rows, sealed, expected):
    assert armarium.conservation_not_reconciled(_conservation_context(rows), {}, sealed) == expected


@pytest.mark.parametrize(
    "rows, sealed, message",
    [
        (
            [],
            {1},
            "the Designator conservation ordinal census does not exactly cover the sealed page census",
        ),
        (
            [
                {"page_ordinal": 1, "ink_measurable": True},
                {"page_ordinal": 1, "ink_measurable": True},
            ],
            {1},
            "the Designator conservation inventory repeats a sealed page ordinal",
        ),
        (
            [{"page_ordinal": 2, "ink_measurable": True}],
            {1},
            "the Designator conservation ordinal census does not exactly cover the sealed page census",
        ),
    ],
)
def test_conservation_refuses_missing_duplicate_or_unsealed_ordinals(rows, sealed, message):
    with pytest.raises(armarium.FatalAccounting, match=message):
        armarium.conservation_not_reconciled(_conservation_context(rows), {}, sealed)


@pytest.mark.parametrize("value", ["false", 0, None])
def test_calibration_flag_refuses_non_boolean_values(value):
    with pytest.raises(armarium.FatalAccounting, match="non-boolean"):
        armarium._typed_calibration_flag(
            {"calibrated_for_this_corpus": value}, "designator-padding"
        )


@pytest.mark.parametrize("value", [True, -1])
def test_calibration_sample_count_refuses_boolean_or_negative_values(value):
    with pytest.raises(armarium.FatalAccounting, match="invalid sample_count"):
        armarium._typed_sample_count(
            {"calibrated_for_this_corpus": False, "sample_count": value}, "designator-padding"
        )


def test_geometry_may_omit_sample_count():
    provenance = {"calibrated_for_this_corpus": False}
    assert armarium._typed_calibration_flag(provenance, "designator-geometry") is False
    assert armarium._typed_sample_count(provenance, "designator-geometry") is None


@pytest.mark.parametrize(
    "payload, match, cause_match",
    [
        (
            {
                "testimony_content_coverage": {"by_chair": {}, "shortfall": "unknown"},
                "testimony_content_coverage_continuation": [],
                "cross_capture_coverage": None,
            },
            "testimony-content",
            "shortfall is not true, false, or null",
        ),
        (
            {
                "testimony_content_coverage": {
                    "by_chair": {
                        "chair": {
                            "attached_spans": [],
                            "uncovered_non_whitespace": {"ranges": [], "count": 0},
                        }
                    },
                    "shortfall": False,
                },
                "testimony_content_coverage_continuation": [
                    {"by_chair": {}, "shortfall": None, "reason": "unmeasured", "page_ordinal": 2},
                    {"by_chair": {}, "shortfall": None, "reason": "unmeasured", "page_ordinal": 2},
                ],
                "cross_capture_coverage": None,
            },
            "testimony-content",
            "repeats a page ordinal",
        ),
        (
            {
                "testimony_content_coverage": {
                    "by_chair": {
                        "chair": {
                            "attached_spans": [],
                            "uncovered_non_whitespace": {"ranges": [], "count": 0},
                        }
                    },
                    "shortfall": False,
                },
                "testimony_content_coverage_continuation": [],
                "cross_capture_coverage": {"components": []},
            },
            "cross-capture",
            "cross-capture coverage record is not closed",
        ),
    ],
)
def test_armarium_consumption_refuses_malformed_review_measurements(
    monkeypatch, payload, match, cause_match
):
    monkeypatch.setattr(armarium, "conservation_not_reconciled", lambda *_args: {})
    monkeypatch.setattr(armarium, "sealed_audit_round_cap", lambda _context: 1)
    monkeypatch.setattr(armarium, "geometry_calibration_rows", lambda _context: [])
    with pytest.raises(armarium.FatalAccounting, match=match) as refusal:
        armarium.not_measured_basis(SimpleNamespace(), {}, {}, {"act-one": payload}, [])
    assert cause_match in str(refusal.value.__cause__)


def test_armarium_consumption_refuses_a_review_without_cross_capture_coverage(monkeypatch):
    payload = {
        "testimony_content_coverage": {
            "by_chair": {
                "chair": {
                    "attached_spans": [],
                    "uncovered_non_whitespace": {"ranges": [], "count": 0},
                }
            },
            "shortfall": False,
        },
        "testimony_content_coverage_continuation": [],
    }
    monkeypatch.setattr(armarium, "conservation_not_reconciled", lambda *_args: {})
    monkeypatch.setattr(armarium, "sealed_audit_round_cap", lambda _context: 1)
    monkeypatch.setattr(armarium, "geometry_calibration_rows", lambda _context: [])
    with pytest.raises(armarium.FatalAccounting, match="act-one.*no cross-capture coverage field"):
        armarium.not_measured_basis(SimpleNamespace(), {}, {}, {"act-one": payload}, [])
