"""The Designator's records carry no text, at the schema boundary.

A Surya detection or a detector record is geometry-shaped JSON; a payload
carrying a `text` or `reported` field would still be that, so nothing but an
explicit walk of the payload (`_refuse_text_fields`) would ever refuse it.
"""

import subprocess
import sys
from pathlib import Path

import pytest

from common.contracts.errors import ContractError
from conftest import load_stage, programs_through

ROOT = Path(__file__).resolve().parents[2]


# --- the mechanical check, direct --------------------------------------------


@pytest.mark.parametrize(
    "forbidden_key", ["text", "reported", "transcription", "content", "reading"]
)
def test_a_forbidden_content_key_is_refused_at_the_top_level(forbidden_key):
    designator = load_stage("2_designator")
    with pytest.raises(ContractError, match="detector-record artifact carries no text"):
        designator._refuse_text_fields(
            {forbidden_key: "SYNTHETIC ACT ONE alpha beta gamma"}, kind="detector-record"
        )


@pytest.mark.parametrize("forbidden_key", ["Text", "TRANSCRIPTION", "Chosen", "PIVOT"])
def test_forbidden_keys_cannot_bypass_the_boundary_by_changing_case(forbidden_key):
    designator = load_stage("2_designator")
    with pytest.raises(ContractError, match="carries no text"):
        designator._refuse_text_fields({forbidden_key: "leaked"}, kind="detector-record")


@pytest.mark.parametrize(
    "forbidden_key", ["text", "reported", "transcription", "content", "reading"]
)
def test_a_forbidden_content_key_is_refused_at_any_depth(forbidden_key):
    designator = load_stage("2_designator")
    nested = {"raw_proposal": {"aabb": {"x": 1, "y": 2, "w": 3, "h": 4}, forbidden_key: "leaked"}}
    with pytest.raises(ContractError, match="carries no text"):
        designator._refuse_text_fields(nested, kind="detector-record")


@pytest.mark.parametrize(
    "forbidden_key", ["text", "reported", "transcription", "content", "reading"]
)
def test_a_forbidden_content_key_is_refused_inside_a_list(forbidden_key):
    designator = load_stage("2_designator")
    nested = {"record_subjects": [{"bounds": {"x": 0, "y": 0, "w": 1, "h": 1}, forbidden_key: "x"}]}
    with pytest.raises(ContractError, match="carries no text"):
        designator._refuse_text_fields(nested, kind="detector-page")


def test_geometry_and_class_fields_are_not_forbidden():
    """A detector's class name and score are not a transcription and must not be refused."""
    designator = load_stage("2_designator")
    payload = {
        "page_ordinal": 1,
        "detector_ordinal": 0,
        "bounds": {"x": 20, "y": 20, "w": 160, "h": 80},
        "score_bp": 9000,
        "class_id": 0,
        "class_name": "record",
        "authority_effect": "none",
    }
    designator._refuse_text_fields(payload, kind="detector-record")  # must not raise


# --- the real published records carry none of the forbidden fields ------------


def test_no_published_designator_record_carries_a_forbidden_field(tmp_path):
    root = tmp_path / "runs"
    for program in programs_through("designator"):
        result = subprocess.run(
            [
                sys.executable,
                str(ROOT / program),
                "--run-root",
                str(root),
                "--run-id",
                "r",
                "--scenario",
                "happy",
            ],
            cwd=ROOT,
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0, f"{program}: {result.stderr}"

    from common.contracts.stages import DESIGNATOR
    from common.runtree.store import RunTree

    designator = load_stage("2_designator")
    tree = RunTree(root, "r")
    kinds = set()
    for entry in tree.build_manifest(DESIGNATOR)["artifacts"]:
        record = tree.read_artifact(DESIGNATOR, entry["kind"], entry["artifact_id"])
        designator._refuse_text_fields(record["payload"], kind=entry["kind"])  # must not raise
        kinds.add(entry["kind"])
    assert {"surya-page", "surya-line", "detector-page", "detector-record"} <= kinds
