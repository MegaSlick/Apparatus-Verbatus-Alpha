"""Direct Armarium contract tests for sealed measurement inputs."""

import importlib.util
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(ROOT), str(ROOT / "pipeline" / "7_armarium")]
spec = importlib.util.spec_from_file_location(
    "armarium_contract_run", ROOT / "pipeline" / "7_armarium" / "run.py"
)
assert spec and spec.loader
armarium = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = armarium
spec.loader.exec_module(armarium)


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
    "rows, sealed",
    [
        ([{"page_ordinal": 1, "ink_measurable": False, "reason": "background unavailable"}], {1}),
        ([{"page_ordinal": 1, "ink_measurable": True}], {1}),
    ],
)
def test_conservation_exact_census_keeps_legitimate_degraded_sealed_pages(rows, sealed):
    result = armarium.conservation_not_reconciled(_conservation_context(rows), {}, sealed)
    assert set(result) <= sealed


@pytest.mark.parametrize(
    "rows, sealed",
    [
        ([], {1}),
        (
            [
                {"page_ordinal": 1, "ink_measurable": True},
                {"page_ordinal": 1, "ink_measurable": True},
            ],
            {1},
        ),
        ([{"page_ordinal": 2, "ink_measurable": True}], {1}),
    ],
)
def test_conservation_refuses_missing_duplicate_or_unsealed_ordinals(rows, sealed):
    with pytest.raises(armarium.FatalAccounting, match="conservation"):
        armarium.conservation_not_reconciled(_conservation_context(rows), {}, sealed)


@pytest.mark.parametrize(
    "provenance",
    [
        {"calibrated_for_this_corpus": "false"},
        {"calibrated_for_this_corpus": False, "sample_count": True},
        {"calibrated_for_this_corpus": False, "sample_count": -1},
    ],
)
def test_calibration_provenance_refuses_malformed_present_values(provenance):
    with pytest.raises(armarium.FatalAccounting):
        armarium._typed_calibration_flag(provenance, "designator-padding")
        armarium._typed_sample_count(provenance, "designator-padding")


def test_geometry_may_omit_sample_count():
    provenance = {"calibrated_for_this_corpus": False}
    assert armarium._typed_calibration_flag(provenance, "designator-geometry") is False
    assert armarium._typed_sample_count(provenance, "designator-geometry") is None


@pytest.mark.parametrize(
    "payload, match",
    [
        (
            {
                "testimony_content_coverage": {"by_chair": {}, "shortfall": "unknown"},
                "testimony_content_coverage_continuation": [],
                "cross_capture_coverage": None,
            },
            "testimony-content",
        ),
        (
            {
                "testimony_content_coverage": {"by_chair": {"chair": {}}, "shortfall": False},
                "testimony_content_coverage_continuation": [
                    {"by_chair": {}, "shortfall": None, "reason": "unmeasured", "page_ordinal": 2},
                    {"by_chair": {}, "shortfall": None, "reason": "unmeasured", "page_ordinal": 2},
                ],
                "cross_capture_coverage": None,
            },
            "testimony-content",
        ),
        (
            {
                "testimony_content_coverage": {"by_chair": {"chair": {}}, "shortfall": False},
                "testimony_content_coverage_continuation": [],
                "cross_capture_coverage": {"components": []},
            },
            "cross-capture",
        ),
    ],
)
def test_armarium_consumption_refuses_malformed_review_measurements(monkeypatch, payload, match):
    monkeypatch.setattr(armarium, "conservation_not_reconciled", lambda *_args: {})
    monkeypatch.setattr(armarium, "sealed_audit_round_cap", lambda _context: 1)
    monkeypatch.setattr(armarium, "geometry_calibration_rows", lambda _context: [])
    with pytest.raises(armarium.FatalAccounting, match=match):
        armarium.not_measured_basis(SimpleNamespace(), {}, {}, {"act-one": payload}, [])
