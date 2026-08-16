"""Focused audit tests for R6's new geometry and testimony coverage inputs."""

import importlib.util
from pathlib import Path
from types import SimpleNamespace

import pytest

from common.contracts.errors import FatalAccounting

ROOT = Path(__file__).resolve().parents[2]


def _load_recensor():
    path = ROOT / "pipeline/5_recensor/run.py"
    spec = importlib.util.spec_from_file_location("recensor_run_coverage_inputs", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


RUN = _load_recensor()


class _ArtifactTree:
    def __init__(self, records):
        self.records = {record["artifact_id"]: record for record in records}

    def build_manifest(self, stage):
        return {
            "artifacts": [
                {
                    "kind": record["kind"],
                    "artifact_id": record["artifact_id"],
                    "subject_id": record["subject_id"],
                }
                for record in self.records.values()
            ]
        }

    def read_artifact(self, stage, kind, artifact_id):
        record = self.records[artifact_id]
        assert record["kind"] == kind
        return record


class _Context(SimpleNamespace):
    def artifact_ref(self, stage, kind, artifact_id):
        return {
            "relative_path": f"stages/{stage}/{kind}/{artifact_id}.json",
            "sha256": "0" * 64,
        }


def _context(*records):
    return _Context(tree=_ArtifactTree(records))


def _page_testimonium(*, outcome, reported=...):
    payload = {"page_ordinal": 1, "chair": "attestator_1"}
    if reported is not ...:
        payload["reported"] = reported
    return {
        "artifact_id": "page-witness-1",
        "kind": "page-testimonium",
        "subject_id": "page-1",
        "outcome": outcome,
        "payload": payload,
    }


def _conservation(artifact_id, *, ordinal=1, measurable=True, components=None):
    return {
        "artifact_id": artifact_id,
        "kind": "conservation",
        "subject_id": f"page-{artifact_id}",
        "outcome": "measured",
        "payload": {
            "page_ordinal": ordinal,
            "ink_measurable": measurable,
            "residual_components": [] if components is None else components,
        },
    }


def _attachment(context, *, end):
    return {
        "artifact_id": "attachment-1",
        "kind": "act-attachment",
        "subject_id": "act-1",
        "outcome": "attached",
        "payload": {
            "attachments": [
                {
                    "chair": "attestator_1",
                    "page_witness": True,
                    "testimonium_ref": context.artifact_ref(
                        RUN.ATTESTATORES, "page-testimonium", "page-witness-1"
                    ),
                    "attached": True,
                    "alignment": {
                        "status": "aligned",
                        "witness_span": {"start": 0, "end": end},
                    },
                }
            ]
        },
    }


def test_a_reading_page_testimonium_cannot_lose_its_reported_text_and_take_the_skip():
    """V4: the no-report skip belongs only to a non-reading page record."""
    act_reading = {
        "artifact_id": "act-reading-1",
        "kind": "testimonium",
        "subject_id": "act-1",
        "outcome": "read",
        "payload": {"chair": "attestator_1", "reported": "real act text"},
    }
    context = _context(act_reading, _page_testimonium(outcome="read"))

    with pytest.raises(FatalAccounting, match="reading page Testimonium has no reported text"):
        RUN.testimony_content_findings(context)


def test_a_non_reading_page_testimonium_still_has_no_content_to_compare():
    context = _context(_page_testimonium(outcome="failed"))

    assert RUN.testimony_content_findings(context) == {}


def test_each_act_gets_a_private_copy_of_its_pages_content_finding():
    """V3: mutating one act's nested payload cannot corrupt a sibling review."""
    findings = {
        1: {
            "by_chair": {
                "attestator_1": {
                    "attached_spans": [{"start": 0, "end": 5, "act_id": "act-1"}],
                    "uncovered_non_whitespace_offsets": [5],
                }
            },
            "shortfall": True,
        }
    }

    first = RUN.testimony_content_for_page(findings, 1)
    sibling = RUN.testimony_content_for_page(findings, 1)
    first["by_chair"]["attestator_1"]["uncovered_non_whitespace_offsets"].clear()
    first["shortfall"] = False

    assert sibling["by_chair"]["attestator_1"]["uncovered_non_whitespace_offsets"] == [5]
    assert sibling["shortfall"] is True
    assert findings[1]["by_chair"]["attestator_1"]["uncovered_non_whitespace_offsets"] == [5]


def test_a_real_uncovered_testimony_character_routes_to_review(monkeypatch):
    """V2a: a genuine content shortfall is measured and reaches the hold route."""
    page = _page_testimonium(outcome="read", reported="alphaX")
    context = _context(page)
    context.tree.records["attachment-1"] = _attachment(context, end=5)
    monkeypatch.setattr(
        RUN,
        "expected_acts",
        lambda unused: [{"act_id": "act-1", "act_key": "a1", "page_ordinal": 1}],
    )

    finding = RUN.testimony_content_findings(context)[1]

    assert finding["by_chair"]["attestator_1"]["uncovered_non_whitespace_offsets"] == [5]
    assert finding["shortfall"] is True
    outcome, reason = RUN.review_route_from_findings(
        testimony_shortfall=finding["shortfall"],
        audit_unresolved=False,
        under_witnessed=False,
    )
    assert outcome == "held-for-review"
    assert "testimony coverage is incomplete" in reason


def test_geometry_coverage_accepts_a_matching_residual_partition(monkeypatch):
    context = _context(_conservation("conservation-1", components=[{"bounds": {}}]))
    monkeypatch.setattr(
        RUN,
        "expected_acts",
        lambda unused: [{"act_key": "residual:1:0"}],
    )

    assert RUN.geometry_coverage_inputs(context) == {
        1: {
            "ink_measurable": True,
            "residual_component_count": 1,
            "residual_act_count": 1,
        }
    }


def test_geometry_coverage_refuses_a_divergent_residual_partition(monkeypatch):
    context = _context(_conservation("conservation-1", components=[{"bounds": {}}]))
    monkeypatch.setattr(RUN, "expected_acts", lambda unused: [])

    with pytest.raises(FatalAccounting, match="held-act partition diverges"):
        RUN.geometry_coverage_inputs(context)


def test_geometry_coverage_refuses_malformed_page_facts(monkeypatch):
    context = _context(_conservation("conservation-1", ordinal=True))
    monkeypatch.setattr(RUN, "expected_acts", lambda unused: [])

    with pytest.raises(FatalAccounting, match="malformed or duplicate page facts"):
        RUN.geometry_coverage_inputs(context)


def test_geometry_coverage_refuses_duplicate_page_facts(monkeypatch):
    context = _context(
        _conservation("conservation-1"),
        _conservation("conservation-2"),
    )
    monkeypatch.setattr(RUN, "expected_acts", lambda unused: [])

    with pytest.raises(FatalAccounting, match="malformed or duplicate page facts"):
        RUN.geometry_coverage_inputs(context)


def test_unmeasured_geometry_cannot_mint_a_residual_act(monkeypatch):
    context = _context(_conservation("conservation-1", measurable=False))
    monkeypatch.setattr(
        RUN,
        "expected_acts",
        lambda unused: [{"act_key": "residual:1:0"}],
    )

    with pytest.raises(FatalAccounting, match="unmeasured.*minted residual acts"):
        RUN.geometry_coverage_inputs(context)


def test_content_and_audit_holds_compose_in_stable_recorded_order():
    """V5: R6 coverage wins precedence, but neither active cause disappears."""
    outcome, reason = RUN.review_route_from_findings(
        testimony_shortfall=True,
        audit_unresolved=True,
        under_witnessed=True,
    )

    assert outcome == "held-for-review"
    assert reason.index("testimony coverage") < reason.index("audit re-proof cap")
    assert reason.index("audit re-proof cap") < reason.index("witness floor")
