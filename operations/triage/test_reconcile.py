"""Replayable, no-model reconciliation tests."""

from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

from common.contracts.canonical import canonical_bytes
from operations.triage.reconcile import ReconciliationRefusal, reconcile, reconcile_files

FIXTURES = Path(__file__).with_name("fixtures") / "reconciliation"


def test_checked_in_synthetic_verdicts_replay_exactly(tmp_path: Path):
    paths = sorted(FIXTURES.glob("seat-*.json"))
    expected = tmp_path / "expected.json"
    disagreements = tmp_path / "disagreements.json"
    reconcile_files(paths, expected, disagreements)
    assert expected.read_bytes() == canonical_bytes(
        json.loads((FIXTURES / "expected.json").read_text(encoding="utf-8"))
    )
    assert disagreements.read_bytes() == canonical_bytes(
        json.loads((FIXTURES / "disagreements.json").read_text(encoding="utf-8"))
    )


def test_unanimity_intervals_and_union_act_denominator_are_not_a_vote():
    verdicts = [
        json.loads(path.read_text(encoding="utf-8"))
        for path in sorted(FIXTURES.glob("seat-*.json"))
    ]
    expected, disagreements = reconcile(verdicts)
    assert expected["facts"]["frame-63"]["numeric_intervals"]["boundary_x_per_mille"] == [490, 510]
    assert disagreements["facts"]["frame-65"]["act_coverage_denominator"] == [
        "act-a",
        "act-b",
        "act-c",
    ]


def test_a_fact_only_one_seat_reported_keeps_that_seat_s_act_enumeration():
    """Consensus gates what the fixture asserts, never what counts as present.

    A frame only one seat reported on is where an act is likeliest to be lost, so
    the missing-fact record carries that seat's whole enumeration as the coverage
    denominator and names who reported it. GOALS 1 ranks a missed act above a
    poorly read one; recording only "missing-fact" would drop both acts silently.
    """
    verdicts = [
        json.loads(path.read_text(encoding="utf-8"))
        for path in sorted(FIXTURES.glob("seat-*.json"))
    ]
    expected, disagreements = reconcile(verdicts)
    missing = disagreements["facts"]["frame-67"]
    assert missing["reason"] == "missing-fact"
    assert missing["act_coverage_denominator"] == ["act-d", "act-e"]
    assert missing["reported_by"] == [{"identity": "synthetic-seat-a", "revision": "v1"}]
    assert "frame-67" not in expected["facts"]


def test_an_act_box_is_an_interval_and_an_unboxed_act_still_counts():
    verdicts = [
        json.loads(path.read_text(encoding="utf-8"))
        for path in sorted(FIXTURES.glob("seat-*.json"))
    ]
    expected, _disagreements = reconcile(verdicts)
    fact = expected["facts"]["frame-63"]
    assert fact["box_intervals_permille"]["act-a"] == {
        "x0": [100, 112],
        "y0": [118, 120],
        "x1": [480, 495],
        "y1": [300, 322],
    }
    # act-b is enumerated but nobody located it: in the denominator, not in geometry.
    assert "act-b" not in fact["box_intervals_permille"]
    assert fact["act_coverage_denominator"] == ["act-a", "act-b"]


def test_a_box_outside_the_frame_or_spelled_as_a_fraction_is_refused():
    """The seat sheet asks for box fractions; the schema carries per-mille integers.

    Both halves of that mismatch are refused by name rather than clamped or
    coerced, because the reconciler runs after three vendors have already been
    sent the images and a silent repair would assert geometry no seat reported.
    """
    verdict = json.loads((FIXTURES / "seat-a.json").read_text(encoding="utf-8"))
    other = json.loads((FIXTURES / "seat-b.json").read_text(encoding="utf-8"))
    outside = copy.deepcopy(verdict)
    outside["facts"]["frame-63"]["boxes"]["act-a"]["x1"] = 1200
    with pytest.raises(ReconciliationRefusal, match="lies outside the frame"):
        reconcile([outside, other])
    fractional = copy.deepcopy(verdict)
    fractional["facts"]["frame-63"]["boxes"]["act-a"]["x0"] = 0.31
    with pytest.raises(ReconciliationRefusal, match="never fractions"):
        reconcile([fractional, other])
    unenumerated = copy.deepcopy(verdict)
    unenumerated["facts"]["frame-63"]["boxes"]["act-z"] = {
        "x0": 10,
        "y0": 10,
        "x1": 20,
        "y1": 20,
    }
    with pytest.raises(ReconciliationRefusal, match="did not enumerate as an act"):
        reconcile([unenumerated, other])


def test_seats_that_disagree_beyond_the_declared_box_tolerance_do_not_assert_geometry():
    verdict = json.loads((FIXTURES / "seat-a.json").read_text(encoding="utf-8"))
    other = copy.deepcopy(json.loads((FIXTURES / "seat-b.json").read_text(encoding="utf-8")))
    other["facts"]["frame-63"]["boxes"]["act-a"]["y1"] = 700
    expected, disagreements = reconcile([verdict, other])
    # Per-fact admission: the failed box never asserts geometry, while the
    # sub-facts the seats agreed on still enter the expected record.
    assert "act-a" not in expected["facts"]["frame-63"]["box_intervals_permille"]
    assert expected["facts"]["frame-63"]["categorical"]["loose_document"] == "yes"
    assert disagreements["facts"]["frame-63"]["failed"] == ["box:act-a"]
    per_seat = disagreements["facts"]["frame-63"]["per_seat"]["box:act-a"]
    assert len(per_seat) == 2 and all("seat" in row and "value" in row for row in per_seat)
