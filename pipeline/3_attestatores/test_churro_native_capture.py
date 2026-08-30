"""Exercise Churro capture through a run tree, including retained failures.

The pinned happy scenario must traverse this boundary without moving reading
text; churro-native adds page furniture, transport truncation, and malformed XML.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from common.contracts.errors import SchemaRefusal
from common.contracts.stages import ATTESTATORES, RECENSOR
from common.runtree.store import RunTree

ROOT = Path(__file__).resolve().parents[2]
ORCHESTRATOR = ROOT / "pipeline/orchestrator/run.py"
FIXTURE = "synthetic-two-page-v0"
HEADER = "[FOLIO RUBRIC 7 -- page furniture, belongs to no entry]"


def _orchestrate(run_root: Path, scenario: str):
    return subprocess.run(
        [
            sys.executable,
            str(ORCHESTRATOR),
            "--fixture",
            FIXTURE,
            "--scenario",
            scenario,
            "--run-id",
            "r",
            "--run-root",
            str(run_root),
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
    )


def _page_testimonia(tree: RunTree) -> dict[tuple[int, str], dict]:
    records = {}
    for entry in tree.build_manifest(ATTESTATORES)["artifacts"]:
        if entry["kind"] != "page-testimonium":
            continue
        record = tree.read_artifact(ATTESTATORES, "page-testimonium", entry["artifact_id"])
        payload = record["payload"]
        records[(payload["page_ordinal"], payload["chair"])] = record
    return records


def _attachments(tree: RunTree) -> dict[str, list[dict]]:
    by_act = {}
    for entry in tree.build_manifest(ATTESTATORES)["artifacts"]:
        if entry["kind"] != "act-attachment":
            continue
        record = tree.read_artifact(ATTESTATORES, "act-attachment", entry["artifact_id"])
        by_act[record["payload"]["act_key"]] = record["payload"]["attachments"]
    return by_act


@pytest.fixture(scope="module")
def native_run(tmp_path_factory):
    root = tmp_path_factory.mktemp("churro-native") / "runs"
    result = _orchestrate(root, "churro-native")
    # Unattributed page furniture must make the scenario partial, not disappear.
    assert result.returncode == 3, result.stderr
    return RunTree(root, "r")


@pytest.fixture(scope="module")
def truncation_run(tmp_path_factory):
    root = tmp_path_factory.mktemp("churro-truncation") / "runs"
    result = _orchestrate(root, "churro-truncation")
    # Page furniture still holds the acts; the truncation itself is retained.
    assert result.returncode == 3, result.stderr
    return RunTree(root, "r")


@pytest.fixture(scope="module")
def happy_run(tmp_path_factory):
    root = tmp_path_factory.mktemp("churro-happy") / "runs"
    result = _orchestrate(root, "happy")
    assert result.returncode == 0, result.stderr
    return RunTree(root, "r")


def test_a_captured_page_reading_parses_and_keeps_its_raw_bytes(native_run):
    """Parsed text may derive from, but never replace, retained response bytes."""
    record = _page_testimonia(native_run)[(1, "attestator_3")]
    payload = record["payload"]
    capture = payload["native_capture"]

    assert record["outcome"] == "read"
    assert capture["adapter"] == "churro.v1"
    assert capture["parse"]["state"] == "parsed"
    assert payload["payload"] == capture["parse"]["text"]
    assert payload["payload"].startswith(HEADER)
    raw = native_run.read_bytes(capture["raw_response_ref"]["relative_path"])
    assert raw == f"<output>{payload['payload']}</output>".encode()
    assert capture["raw_response_ref"] in record["inputs"]
    assert payload["content_health"]["recordable"] is True
    assert payload["content_health"]["truncated"] is False
    assert payload["content_health"]["characters"] == len(payload["payload"])


def test_each_act_lands_on_its_own_words_across_page_furniture(native_run):
    """Page furniture must not shift an act span onto neighbouring text."""
    page_text = _page_testimonia(native_run)[(1, "attestator_3")]["payload"]["payload"]
    attachments = _attachments(native_run)

    spans = {}
    for act_key in ("a1", "a2"):
        entry = next(
            item
            for item in attachments[act_key]
            if item["chair"] == "attestator_3" and item["page_ordinal"] == 1
        )
        assert entry["page_witness"] is True
        assert entry["alignment"]["status"] == "aligned"
        span = entry["alignment"]["witness_span"]
        spans[act_key] = page_text[span["start"] : span["end"]]

    assert "SYNTHETIC ACT ONE" in spans["a1"]
    assert "SYNTHETIC ACT TWO" in spans["a2"]
    assert "SYNTHETIC ACT TWO" not in spans["a1"]
    assert "SYNTHETIC ACT ONE" not in spans["a2"]
    assert HEADER not in spans["a1"] and HEADER not in spans["a2"]


def test_page_text_no_act_accounts_for_holds_rather_than_disappearing(native_run):
    """Unattributed page text must hold the acts rather than disappear."""
    reviews = [
        native_run.read_artifact(RECENSOR, "review", entry["artifact_id"])
        for entry in native_run.build_manifest(RECENSOR)["artifacts"]
        if entry["kind"] == "review"
    ]
    assert reviews and all(review["outcome"] == "held-for-review" for review in reviews)
    for review in reviews:
        coverage = review["payload"]["testimony_content_coverage"]["by_chair"]["attestator_3"]
        assert coverage["uncovered_non_whitespace"]["count"] > 0
        assert "outside the ordered union" in review["payload"]["reason"]


def test_a_truncated_capture_is_visible_and_is_never_completed_or_retried(truncation_run):
    """Transport truncation retains partial text without completing or retrying it."""
    record = _page_testimonia(truncation_run)[(2, "attestator_3")]
    payload = record["payload"]
    health = payload["content_health"]

    assert record["outcome"] == "read"
    assert health["truncated"] is True
    assert health["truncation_basis"] == "trusted-response-boundary"
    assert payload["native_capture"]["transport_stop_reason"] == "length"
    raw = truncation_run.read_bytes(payload["native_capture"]["raw_response_ref"]["relative_path"])
    assert raw.decode() == f"<output>{payload['payload']}</output>"
    assert payload["attempt_ordinal"] == 1


def test_a_captured_response_that_cannot_be_parsed_keeps_its_bytes_and_names_the_cut(native_run):
    """An unrecordable response names a transport cut in its reason and basis."""
    record = _page_testimonia(native_run)[(2, "attestator_3")]
    payload = record["payload"]
    health = payload["content_health"]
    capture = payload["native_capture"]

    assert record["outcome"] == "failed"
    assert payload["payload"] is None
    assert health["recordable"] is False
    assert health["encoding"] == "invalid-or-unrecordable"
    assert capture["parse"]["state"] == "failed"
    assert capture["transport_stop_reason"] == "length"
    assert "length" in health["truncation_basis"]
    assert "cut off" in health["truncation_basis"]
    assert "stopped the response at its bound" in payload["reason"]
    raw = native_run.read_bytes(capture["raw_response_ref"]["relative_path"])
    assert raw == b"<output>" + HEADER.encode() + b"\nSYNTHETIC ACT TWO delta epsiIon zeta eta"
    assert not raw.endswith(b"</output>")


def test_a_failed_page_capture_does_not_claim_a_missing_anchor(native_run):
    """A failed response must not be misreported as a missing page anchor."""
    entry = next(
        item
        for item in _attachments(native_run)["a2"]
        if item["chair"] == "attestator_3" and item["page_ordinal"] == 2
    )
    assert entry["attached"] is False
    assert entry["alignment"]["reason"] != "missing-chandra-page-anchor"
    reference = entry["testimonium_ref"]["relative_path"]
    assert json.loads((native_run.root / reference).read_bytes())["outcome"] == "failed"


def test_the_pinned_happy_run_captures_through_churro_without_moving_a_reading(happy_run):
    """The pinned run must exercise capture without changing its reading text."""
    records = _page_testimonia(happy_run)
    assert set(records) == {
        (1, "attestator_1"),
        (1, "attestator_3"),
        (2, "attestator_1"),
        (2, "attestator_3"),
    }
    for (page_ordinal, chair), record in records.items():
        payload = record["payload"]
        assert record["outcome"] == "read", (page_ordinal, chair)
        assert payload["content_health"]["truncated"] is False
        if chair == "attestator_1":
            # The Chandra chair's page reading is the legacy join of its own
            # retained act responses; a churro capture attributed to it would
            # wear another model boundary's name.
            assert "native_capture" not in payload
            continue
        capture = payload["native_capture"]
        assert capture["adapter"] == "churro.v1"
        assert capture["parse"]["state"] == "parsed"
        assert capture["transport_stop_reason"] == "eos"
        raw = happy_run.read_bytes(capture["raw_response_ref"]["relative_path"])
        assert raw == f"<output>{payload['payload']}</output>".encode()

    assert records[(1, "attestator_1")]["payload"]["payload"] == (
        "SYNTHETIC ACT ONE alpha beta gamma\nSYNTHETIC ACT TWO delta epsilon zeta eta"
    )
    assert records[(2, "attestator_3")]["payload"]["payload"] == (
        "SYNTHETIC ACT TWO delta epsiIon zeta eta"
    )


def test_a_page_testimonium_read_verifies_its_retained_raw_response(tmp_path):
    """A nested raw reference must also be a digest-verified envelope input."""
    root = tmp_path / "runs"
    result = _orchestrate(root, "happy")
    assert result.returncode == 0, result.stderr
    tree = RunTree(root, "r")
    entry, record = next(
        (item, candidate)
        for item in tree.build_manifest(ATTESTATORES)["artifacts"]
        if item["kind"] == "page-testimonium"
        and "native_capture"
        in (candidate := tree.read_artifact(ATTESTATORES, "page-testimonium", item["artifact_id"]))[
            "payload"
        ]
    )
    raw_ref = record["payload"]["native_capture"]["raw_response_ref"]
    tree.resolve(raw_ref["relative_path"]).write_bytes(b"tampered raw response")

    with pytest.raises(SchemaRefusal, match="digest"):
        tree.read_artifact(ATTESTATORES, "page-testimonium", entry["artifact_id"])


def test_a_witness_reading_order_that_departs_from_the_anchor_degrades_visibly():
    """Monotonic alignment exposes reordered text as uncovered, never misattached."""
    from common.alignment import align_to_anchor, load_alignment_limits

    limits, _ = load_alignment_limits(ROOT / "config/alignment.toml")
    anchor = (
        "<p>SYNTHETIC ACT ONE alpha beta gamma </p><p>SYNTHETIC ACT TWO delta epsilon zeta eta</p>"
    )
    in_order = "SYNTHETIC ACT ONE alpha beta gamma\nSYNTHETIC ACT TWO delta epsilon zeta eta"
    swapped = "SYNTHETIC ACT TWO delta epsilon zeta eta\nSYNTHETIC ACT ONE alpha beta gamma"

    matched = align_to_anchor(in_order, anchor, limits)
    assert matched["status"] == "aligned"
    assert [(span["anchor"]["start"], span["anchor"]["end"]) for span in matched["spans"]] == [
        (0, 75)
    ]

    reordered = align_to_anchor(swapped, anchor, limits)
    assert reordered["status"] == "aligned"
    covered = [(span["anchor"]["start"], span["anchor"]["end"]) for span in reordered["spans"]]
    # The unmatched first range must not borrow characters from the second.
    assert covered == [(35, 75)]
    assert not any(start < 35 for start, _ in covered)
