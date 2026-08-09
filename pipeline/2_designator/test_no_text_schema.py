"""acts/-equivalent artifacts carry no text, at the schema boundary (spec 06 test 6).

`kind="act-group"` is this stage's `acts/` contract: crop-to-act grouping
evidence, and nothing else. `_refuse_text_fields` is the mechanical proof that
a transcription cannot enter one and pass silently -- a payload carrying a
`text` or `reported` field is still geometry-shaped JSON otherwise, so nothing
but an explicit walk of the payload would ever refuse it.
"""

import importlib.util
import subprocess
import sys
from pathlib import Path

import pytest

from common.contracts.errors import ContractError

ROOT = Path(__file__).resolve().parents[2]


def _load_designator():
    path = ROOT / "pipeline" / "2_designator" / "run.py"
    spec = importlib.util.spec_from_file_location("designator_no_text_schema_under_test", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


# --- the mechanical check, direct --------------------------------------------


@pytest.mark.parametrize(
    "forbidden_key", ["text", "reported", "transcription", "content", "reading"]
)
def test_a_forbidden_content_key_is_refused_at_the_top_level(forbidden_key):
    designator = _load_designator()
    with pytest.raises(ContractError, match="carries no text"):
        designator._refuse_text_fields({forbidden_key: "SYNTHETIC ACT ONE alpha beta gamma"})


@pytest.mark.parametrize("forbidden_key", ["Text", "TRANSCRIPTION", "Chosen", "PIVOT"])
def test_forbidden_keys_cannot_bypass_the_boundary_by_changing_case(forbidden_key):
    designator = _load_designator()
    with pytest.raises(ContractError, match="carries no text"):
        designator._refuse_text_fields({forbidden_key: "leaked"})


def test_an_unknown_text_synonym_cannot_enter_the_closed_act_group_contract():
    designator = _load_designator()
    payload = {
        "act_key": "a1",
        "declared_bounds": {"x": 1, "y": 2, "w": 3, "h": 4},
        "detected_bounds": {"x": 1, "y": 2, "w": 3, "h": 4},
        "body_member_count": 1,
        "anchor_count": 0,
        "rationale": "single margin anchor seeds one body run",
        "continuation": None,
        "ocr_text": "leaked",
    }
    with pytest.raises(ContractError, match="closed contract"):
        designator._validate_act_group_payload(payload)


@pytest.mark.parametrize(
    "forbidden_key", ["text", "reported", "transcription", "content", "reading"]
)
def test_a_forbidden_content_key_is_refused_at_any_depth(forbidden_key):
    designator = _load_designator()
    nested = {
        "continuation": {
            "detected_bounds": {"x": 1, "y": 2, "w": 3, "h": 4},
            forbidden_key: "leaked",
        }
    }
    with pytest.raises(ContractError, match="carries no text"):
        designator._refuse_text_fields(nested)


@pytest.mark.parametrize(
    "forbidden_key", ["text", "reported", "transcription", "content", "reading"]
)
def test_a_forbidden_content_key_is_refused_inside_a_list(forbidden_key):
    designator = _load_designator()
    nested = {
        "body_members": [{"bounds": {"x": 0, "y": 0, "w": 1, "h": 1}, forbidden_key: "leaked"}]
    }
    with pytest.raises(ContractError, match="carries no text"):
        designator._refuse_text_fields(nested)


def test_geometry_and_rationale_fields_are_not_forbidden():
    """The mechanism, not the ink: a code-generated rationale describing which
    grouping rule fired is not a transcription and must not be refused."""
    designator = _load_designator()
    payload = {
        "act_key": "a1",
        "declared_bounds": {"x": 20, "y": 20, "w": 160, "h": 80},
        "detected_bounds": {"x": 21, "y": 20, "w": 159, "h": 78},
        "body_member_count": 1,
        "anchor_count": 0,
        "rationale": "no margin anchor precedes this body run; a candidate leading fragment",
        "continuation": None,
    }
    designator._refuse_text_fields(payload)  # must not raise


# --- the real published artifact carries none of the forbidden fields ---------


def test_a_real_act_group_artifact_carries_no_forbidden_field(tmp_path):
    root = tmp_path / "runs"
    for program in (
        "pipeline/1_exemplar/door.py",
        "pipeline/1_exemplar/run.py",
        "pipeline/2_designator/run.py",
    ):
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

    designator = _load_designator()
    tree = RunTree(root, "r")
    act_groups = [
        tree.read_artifact(DESIGNATOR, "act-group", entry["artifact_id"])
        for entry in tree.build_manifest(DESIGNATOR)["artifacts"]
        if entry["kind"] == "act-group"
    ]
    assert len(act_groups) == 2  # a1 and a2, both proposed in the happy scenario
    for record in act_groups:
        designator._validate_act_group_payload(record["payload"])  # closed schema; must not raise


def test_deleting_the_check_lets_a_forged_text_field_publish_uninspected(tmp_path):
    """Proves the guard actually guards something: with `_refuse_text_fields`
    bypassed, an act-group artifact carrying a `text` field publishes cleanly,
    because nothing else in the envelope/payload schema forbids an extra key."""
    designator = _load_designator()
    from common.contracts.canonical import digest_of

    root = tmp_path / "runs"
    for program in (
        "pipeline/1_exemplar/door.py",
        "pipeline/1_exemplar/run.py",
    ):
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

    from common.stage import open_context, stage_parser

    args = stage_parser("no-text schema bypass proof").parse_args(
        ["--run-root", str(root), "--run-id", "r", "--scenario", "happy"]
    )
    context = open_context(args, designator.DESIGNATOR)
    forged_payload = {
        "act_key": "a1",
        "declared_bounds": {"x": 20, "y": 20, "w": 160, "h": 80},
        "text": "SYNTHETIC ACT ONE alpha beta gamma",
    }
    # No `_refuse_text_fields` call here at all -- the point of this test is
    # that nothing else in `context.publish` would have stopped it.
    published = context.publish(
        kind="act-group",
        subject_id="act_0000000000000000",
        outcome="proposed",
        inputs=[],
        payload=forged_payload,
    )
    stored = tree_read(context, published)
    assert "text" in stored["payload"]
    assert digest_of(stored["payload"]) == digest_of(forged_payload)


def tree_read(context, published):
    return context.tree.read_artifact(
        context.stage, "act-group", published.relative_path.rsplit("/", 1)[-1].removesuffix(".json")
    )
