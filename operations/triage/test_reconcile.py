"""Replayable, no-model reconciliation tests."""

from __future__ import annotations

import copy
import errno
import hashlib
import json
import os
from pathlib import Path

import pytest

from common.contracts.canonical import canonical_bytes
from operations.triage.reconcile import (
    EXPECTED_SCHEMA,
    ReconciliationRefusal,
    reconcile,
    reconcile_files,
    validate_reconciliation_pair,
)

FIXTURES = Path(__file__).with_name("fixtures") / "reconciliation"


def _tree_snapshot(root: Path) -> dict[str, str]:
    """Every byte under `root`, so a refusal's own claim can be checked.

    Each path guard in `_canonical_distinct_paths` tells the operator that nothing
    was written. That is a statement about this directory, and until it is compared
    against the directory it is a statement the suite takes on trust -- exactly the
    shape GOVERNANCE 10 refuses, since a pass that published the disagreement
    document and then refused would still print it.
    """
    return {
        str(path.relative_to(root)): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }


def _stray_writes(before: dict[str, str], after: dict[str, str]) -> list[str]:
    """The paths a refusal moved, named -- a count would not say which file to look at."""
    return sorted(
        name for name in before.keys() | after.keys() if before.get(name) != after.get(name)
    )


def _local_verdicts(tmp_path: Path) -> list[Path]:
    local = []
    for fixture in sorted(FIXTURES.glob("seat-*.json")):
        target = tmp_path / fixture.name
        target.write_bytes(fixture.read_bytes())
        local.append(target)
    return local


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


def test_reconciliation_paths_refuse_case_collisions_and_source_symlinks(tmp_path: Path):
    sources = []
    for fixture in sorted(FIXTURES.glob("seat-*.json")):
        local = tmp_path / fixture.name
        local.write_bytes(fixture.read_bytes())
        sources.append(local)
    with pytest.raises(ReconciliationRefusal, match="case-insensitive filesystem"):
        reconcile_files(sources, tmp_path / "Result.json", tmp_path / "result.json")
    linked = tmp_path / "linked-seat.json"
    linked.symlink_to(sources[0])
    with pytest.raises(ReconciliationRefusal, match="symbolic link"):
        reconcile_files(
            [linked, sources[1]], tmp_path / "expected.json", tmp_path / "disagreements.json"
        )
    fifo = tmp_path / "verdict.fifo"
    fifo.unlink(missing_ok=True)
    os.mkfifo(fifo)
    with pytest.raises(ReconciliationRefusal, match="not a regular file"):
        reconcile_files(
            [fifo, sources[1]], tmp_path / "expected.json", tmp_path / "disagreements.json"
        )
    assert not (tmp_path / "expected.json").exists()
    assert not (tmp_path / "disagreements.json").exists()


def _unresolvable_output(tmp_path, sources, monkeypatch):
    """The only guard here with no filesystem shape of its own to build."""
    doomed = tmp_path / "unresolvable"
    original = Path.resolve

    def refuse(self, strict=False):
        if self == doomed:
            raise OSError(errno.EIO, "simulated resolution failure")
        return original(self, strict=strict)

    monkeypatch.setattr(Path, "resolve", refuse)
    return sources, doomed / "expected.json", tmp_path / "disagreements.json"


def _colliding_spellings(tmp_path, sources, _monkeypatch):
    return sources, tmp_path / "Result.json", tmp_path / "result.json"


def _unstatable_output(tmp_path, sources, _monkeypatch):
    """A parent that is a regular file: `lstat` fails with ENOTDIR, not ENOENT."""
    plain = tmp_path / "not-a-directory.json"
    plain.write_bytes(b"{}")
    return sources, plain / "expected.json", tmp_path / "disagreements.json"


def _linked_source(tmp_path, sources, _monkeypatch):
    linked = tmp_path / "linked-seat.json"
    linked.symlink_to(sources[0])
    return (
        [linked, sources[1]],
        tmp_path / "expected.json",
        tmp_path / "disagreements.json",
    )


def _irregular_source(tmp_path, sources, _monkeypatch):
    fifo = tmp_path / "verdict.fifo"
    os.mkfifo(fifo)
    return (
        [fifo, sources[1]],
        tmp_path / "expected.json",
        tmp_path / "disagreements.json",
    )


def _one_file_under_two_names(tmp_path, sources, _monkeypatch):
    twin = tmp_path / "twin-seat.json"
    os.link(sources[0], twin)
    return (
        [sources[0], twin],
        tmp_path / "expected.json",
        tmp_path / "disagreements.json",
    )


@pytest.mark.parametrize(
    ("build", "named"),
    (
        pytest.param(_unresolvable_output, "could not be resolved", id="unresolvable"),
        pytest.param(_colliding_spellings, "case-insensitive filesystem", id="case-collision"),
        pytest.param(_unstatable_output, "could not be verified", id="unstatable"),
        pytest.param(_linked_source, "is a symbolic link", id="symlink"),
        pytest.param(_irregular_source, "is not a regular file", id="irregular"),
        pytest.param(_one_file_under_two_names, "name one file", id="one-inode"),
    ),
)
def test_a_refused_reconciliation_path_wrote_nothing_it_disowned(
    tmp_path: Path, monkeypatch, build, named: str
):
    """Every path guard's "nothing was written" measured against the directory.

    The tests above check the named cause and the two output names. That leaves the
    sentence itself unmeasured, and it is the half an operator acts on: the advice is
    to repair the path and retry, which is only safe while the previous attempt left
    no half-published pair behind. `reconcile_files` publishes the disagreements
    document *before* the expected one deliberately, so a guard that moved below the
    writes would leave exactly one of the two on disk -- new positive assertions with
    no dissent record, or a dissent record for assertions nobody made.

    The comparison is over content, not names: the source verdicts already exist, so
    a guard that overwrote one would leave the name list untouched. The assertion
    names the paths that moved, because "one file changed" does not say which.
    """
    sources, expected, disagreements = build(tmp_path, _local_verdicts(tmp_path), monkeypatch)
    before = _tree_snapshot(tmp_path)

    with pytest.raises(ReconciliationRefusal) as refusal:
        reconcile_files(sources, expected, disagreements)

    assert named in str(refusal.value)
    assert "nothing was written" in str(refusal.value)
    assert _stray_writes(before, _tree_snapshot(tmp_path)) == [], (
        "the refusal wrote to the tree it disowned"
    )


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
    validate_reconciliation_pair(expected, disagreements)
    tampered = copy.deepcopy(expected)
    tampered["facts"]["frame-63"]["categorical"]["loose_document"] = "no"
    with pytest.raises(ReconciliationRefusal, match="do not match their shared digest"):
        validate_reconciliation_pair(tampered, disagreements)


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


def test_untrusted_verdict_counts_and_numeric_measurements_are_bounded_before_reconciliation():
    verdict = json.loads((FIXTURES / "seat-a.json").read_text(encoding="utf-8"))
    other = json.loads((FIXTURES / "seat-b.json").read_text(encoding="utf-8"))
    with pytest.raises(ReconciliationRefusal, match="between 2 and 32"):
        reconcile([verdict] * 33)
    unbounded = copy.deepcopy(verdict)
    unbounded["facts"]["frame-63"]["numeric"]["boundary_x_per_mille"] = 10**1000
    with pytest.raises(ReconciliationRefusal, match="per-mille integers"):
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


def test_one_seat_or_one_seat_twice_is_never_unanimity():
    """Independence is the whole claim; neither bound was exercised.

    Unanimity across one reader is not unanimity, and the same file supplied twice is
    one reader counted twice. Either would publish `expected.json` as agreement between
    seats with nothing downstream able to tell that only one reader ever looked.
    """
    verdict = json.loads((FIXTURES / "seat-a.json").read_text(encoding="utf-8"))
    with pytest.raises(ReconciliationRefusal, match="between 2 and 32"):
        reconcile([verdict])
    with pytest.raises(ReconciliationRefusal, match="repeat a seat identity and revision"):
        reconcile([verdict, copy.deepcopy(verdict)])
    # Two revisions of one identity are two resolved models, and are accepted as two
    # seats: the refusal is keyed on the resolved pair, not on the identity alone.
    second_revision = copy.deepcopy(verdict)
    second_revision["seat"]["revision"] = second_revision["seat"]["revision"] + "-b"
    expected, _disagreements = reconcile([verdict, second_revision])
    assert len(expected["seats"]) == 2


def test_an_open_face_dialect_is_refused_rather_than_read_as_agreement():
    """The closed enum is what makes unanimity on this fact decidable by observation.

    Before it was closed, one seat's "up" and another's "recto" were two dialects for
    one observation, and the reconciler could only record them as disagreement. A
    return to open vocabularies must refuse, not quietly read as either.
    """
    verdict = json.loads((FIXTURES / "seat-a.json").read_text(encoding="utf-8"))
    other = json.loads((FIXTURES / "seat-b.json").read_text(encoding="utf-8"))
    dialect = copy.deepcopy(verdict)
    fact = next(iter(dialect["facts"].values()))
    fact["categorical"]["loose_document_face"] = "recto"
    with pytest.raises(ReconciliationRefusal, match="loose_document_face must be one of"):
        reconcile([dialect, other])
    for face in ("written-side-up", "written-side-down", "indeterminate", "none"):
        accepted = copy.deepcopy(verdict)
        next(iter(accepted["facts"].values()))["categorical"]["loose_document_face"] = face
        reconcile([accepted, other])


def test_every_dated_measured_pass_is_a_whole_pair_and_a_sealed_one_verifies():
    """A historical measurement is evidence, and evidence is never overwritten.

    `test_checked_in_synthetic_verdicts_replay_exactly` pins only the synthetic
    fixtures, so nothing watched the dated measured outputs at all. They cannot be
    replayed here — their seat files hold real material and never enter this repository
    — but a v2 pair seals itself: `reconciliation_sha256` is taken over both documents
    together, so an edit to either half breaks the seal unless whoever made it re-ran
    the reconciler, which is what a legitimate change is.

    The 2026-08-22 pass is a v1 pair, written before those seals existed, and it cannot
    be upgraded from anything in this repository: re-sealing it means re-running the
    reconciler over private seat files on the host. So it is checked for the shape it
    does have — both halves present, same schema generation, same seats — and the
    unverifiable part is named here rather than left to look guarded.
    """
    measured = Path(__file__).with_name("measured")
    passes = sorted(path for path in measured.iterdir() if path.is_dir())
    assert passes, "no dated measured pass was checked at all"
    for dated in passes:
        expected_path = dated / "expected.json"
        disagreements_path = dated / "disagreements.json"
        assert expected_path.is_file() and disagreements_path.is_file(), (
            f"{dated.name} is half a reconciliation pair; the other half cannot be verified"
        )
        expected = json.loads(expected_path.read_text(encoding="utf-8"))
        disagreements = json.loads(disagreements_path.read_text(encoding="utf-8"))
        if expected.get("schema") == EXPECTED_SCHEMA:
            validate_reconciliation_pair(expected, disagreements)
            continue
        assert expected["schema"] == "triage-structural-expected.v1", (
            f"{dated.name} names an unknown expected-document schema"
        )
        assert disagreements["schema"] == "triage-structural-disagreements.v1", (
            f"{dated.name} pairs two different schema generations"
        )
        assert expected["seats"] == disagreements["seats"], (
            f"{dated.name} halves name different seats, so they are not one pass"
        )
        assert expected["seats"] and expected["facts"]
        # The disagreement half is the failure evidence, and a v1 pair carries no
        # seal to notice its loss: without this, a `disagreements.json` emptied of
        # its fact groups would still pass on schema and seats alone, and a reader
        # would hold a pass whose recorded failures had quietly gone.
        assert set(disagreements["facts"]) == set(expected["facts"]), (
            f"{dated.name} records disagreements for facts its expected half never names"
        )
        for fact_id, group in disagreements["facts"].items():
            assert isinstance(group, dict) and group, (
                f"{dated.name} carries an empty disagreement group for {fact_id}"
            )
            assert group.get("per_seat"), (
                f"{dated.name} names a disagreement for {fact_id} without the seat readings"
            )
        # The 2026-08-22 pass read seven frames, and each is one fact group. The count
        # is pinned because both halves losing one group together would otherwise
        # agree with each other about evidence that is no longer there.
        if dated.name == "2026-08-22_005469606_62-68":
            assert len(disagreements["facts"]) == 7
            assert len(expected["seats"]) == 3
