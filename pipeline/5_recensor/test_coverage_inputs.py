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


def _context(*records):
    return SimpleNamespace(tree=_ArtifactTree(records))


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


def test_a_reading_page_testimonium_cannot_lose_its_reported_text_and_take_the_skip():
    """V4: the no-report skip belongs only to a non-reading page record."""
    context = _context(_page_testimonium(outcome="read"))

    with pytest.raises(FatalAccounting, match="reading page Testimonium has no reported text"):
        RUN.testimony_content_findings(context)


def test_a_non_reading_page_testimonium_still_has_no_content_to_compare():
    context = _context(_page_testimonium(outcome="failed"))

    assert RUN.testimony_content_findings(context) == {}
