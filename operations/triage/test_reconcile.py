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


def test_reconciliation_outputs_cannot_overwrite_a_verdict_or_each_other(tmp_path: Path):
    sources = []
    for fixture in sorted(FIXTURES.glob("seat-*.json")):
        local = tmp_path / fixture.name
        local.write_bytes(fixture.read_bytes())
        sources.append(local)
    original = sources[0].read_bytes()
    with pytest.raises(ReconciliationRefusal, match="resolve to one file"):
        reconcile_files(sources, sources[0], tmp_path / "disagreements.json")
    assert sources[0].read_bytes() == original
    shared = tmp_path / "derived" / "shared.json"
    with pytest.raises(ReconciliationRefusal, match="resolve to one file"):
        reconcile_files(sources, shared, shared)
    assert not shared.exists()


def test_unanimity_intervals_and_union_act_denominator_are_not_a_vote():
    verdicts = [
        json.loads(path.read_text(encoding="utf-8"))
        for path in sorted(FIXTURES.glob("seat-*.json"))
    ]
    expected, disagreements = reconcile(verdicts)
    assert expected["verdicts_sha256"] == disagreements["verdicts_sha256"]
    assert len(expected["verdicts_sha256"]) == 64
    assert expected["reconciliation_sha256"] == disagreements["reconciliation_sha256"]
    assert len(expected["reconciliation_sha256"]) == 64
    assert expected["facts"]["frame-63"]["numeric_intervals"]["boundary_x_per_mille"] == [490, 510]
    assert disagreements["facts"]["frame-65"]["act_coverage_denominator"] == [
        "act-001",
        "act-002",
        "act-003",
    ]
    assert "acts" in disagreements["facts"]["frame-65"]["failed"]
    assert disagreements["facts"]["frame-65"]["per_seat"]["acts"] == [
        {
            "seat": {"identity": "synthetic-seat-a", "revision": "v1"},
            "value": ["act-001", "act-002"],
        },
        {
            "seat": {"identity": "synthetic-seat-b", "revision": "v1"},
            "value": ["act-001", "act-002", "act-003"],
        },
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
    assert missing["act_coverage_denominator"] == ["act-001", "act-002"]
    assert missing["reported_by"] == [{"identity": "synthetic-seat-a", "revision": "v1"}]
    assert missing["per_seat"] == [
        {
            "seat": {"identity": "synthetic-seat-a", "revision": "v1"},
            "value": verdicts[0]["facts"]["frame-67"],
        }
    ]
    assert "frame-67" not in expected["facts"]


def test_an_act_box_is_an_interval_and_an_unboxed_act_still_counts():
    verdicts = [
        json.loads(path.read_text(encoding="utf-8"))
        for path in sorted(FIXTURES.glob("seat-*.json"))
    ]
    expected, _disagreements = reconcile(verdicts)
    fact = expected["facts"]["frame-63"]
    assert fact["box_intervals_permille"]["act-001"] == {
        "x0": [100, 112],
        "y0": [118, 120],
        "x1": [480, 495],
        "y1": [300, 322],
    }
    # act-002 is enumerated but nobody located it: in the denominator, not in geometry.
    assert "act-002" not in fact["box_intervals_permille"]
    assert fact["act_coverage_denominator"] == ["act-001", "act-002"]


def test_a_box_outside_the_frame_or_spelled_as_a_fraction_is_refused():
    """The seat sheet asks for per-mille integers; malformed fractions stay refusals.

    Both halves of that mismatch are refused by name rather than clamped or
    coerced, because the reconciler runs after three vendors have already been
    sent the images and a silent repair would assert geometry no seat reported.
    """
    verdict = json.loads((FIXTURES / "seat-a.json").read_text(encoding="utf-8"))
    other = json.loads((FIXTURES / "seat-b.json").read_text(encoding="utf-8"))
    outside = copy.deepcopy(verdict)
    outside["facts"]["frame-63"]["boxes"]["act-001"]["x1"] = 1200
    with pytest.raises(ReconciliationRefusal, match="lies outside the frame"):
        reconcile([outside, other])
    fractional = copy.deepcopy(verdict)
    fractional["facts"]["frame-63"]["boxes"]["act-001"]["x0"] = 0.31
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

    skipped = copy.deepcopy(verdict)
    skipped["facts"]["frame-63"]["acts"] = ["act-001", "act-003"]
    with pytest.raises(ReconciliationRefusal, match="contiguous positional identifiers"):
        reconcile([skipped, other])


@pytest.mark.parametrize("field", ["numeric_tolerance", "box_tolerance_permille"])
def test_declared_tolerances_are_bounded_per_mille_integers(field: str):
    verdict = json.loads((FIXTURES / "seat-a.json").read_text(encoding="utf-8"))
    other = json.loads((FIXTURES / "seat-b.json").read_text(encoding="utf-8"))
    unbounded = copy.deepcopy(verdict)
    unbounded[field] = 1001
    with pytest.raises(ReconciliationRefusal, match="per-mille integer from 0 through 1000"):
        reconcile([unbounded, other])


def test_seats_that_disagree_beyond_the_declared_box_tolerance_do_not_assert_geometry():
    verdict = json.loads((FIXTURES / "seat-a.json").read_text(encoding="utf-8"))
    other = copy.deepcopy(json.loads((FIXTURES / "seat-b.json").read_text(encoding="utf-8")))
    other["facts"]["frame-63"]["boxes"]["act-001"]["y1"] = 700
    expected, disagreements = reconcile([verdict, other])
    # Per-fact admission: the failed box never asserts geometry, while the
    # sub-facts the seats agreed on still enter the expected record.
    assert "act-001" not in expected["facts"]["frame-63"]["box_intervals_permille"]
    assert expected["facts"]["frame-63"]["categorical"]["loose_document"] == "yes"
    assert disagreements["facts"]["frame-63"]["failed"] == ["box:act-001"]
    per_seat = disagreements["facts"]["frame-63"]["per_seat"]["box:act-001"]
    assert len(per_seat) == 2 and all("seat" in row and "value" in row for row in per_seat)
