"""The secondary proposer publishes DAI's own records, never a verdict.

The `secondary_proposer` chair is DAI's own project's record detector. The
Designator runs it and publishes what it found as page evidence that decides
nothing. Two levels, cheapest first: the pure geometry rule with no I/O, and
records published over a real run tree.
"""

import hashlib
import json
import shutil
import subprocess
import sys
import types
from pathlib import Path

import pytest

from common.contracts.errors import ContractError
from conftest import load_stage, programs_through

ROOT = Path(__file__).resolve().parents[2]


# --- level 1: the pure geometry rule, no I/O at all ----------------------------


def test_detector_corners_are_floored_into_the_page_before_the_hull():
    designator = load_stage("2_designator")
    corners = [[30.9, -3.2], [205.0, 30.1], [170.4, 261.8], [0.0, 61.3]]
    assert designator._quantized_corners(corners, 200, 260) == [
        {"x": 30, "y": 0},
        {"x": 199, "y": 30},
        {"x": 170, "y": 259},
        {"x": 0, "y": 61},
    ]


# --- level 2: records over a real run tree -------------------------------------

# Page 1 of the synthetic fixture holds a1 at (20, 20, 160, 80) and a2 at
# (20, 120, 160, 100). One record lies inside a1, slightly rotated; one
# straddles both acts; one collapses to a single pixel at the page corner.
DECLARED_DETECTIONS = (
    {
        "page_ordinal": 1,
        "corners": [[30.2, 30.7], [170.9, 30.1], [170.4, 60.8], [30.0, 61.3]],
        "score_bp": 9731,
    },
    {"page_ordinal": 1, "corners": [[25, 95], [175, 95], [175, 150], [25, 150]], "score_bp": 8120},
    {
        "page_ordinal": 1,
        "corners": [[199.9, 259.9], [250, 300], [210, 270], [199.5, 259.2]],
        "score_bp": 2600,
    },
)


def _configured(tmp_path: Path) -> list[str]:
    """Stage flags for a run on the committed roster, whose `secondary_proposer` is a
    fixture detector, with a copy of the committed catalogue a test may edit.

    The fixture detector answers with the fixture's `[[detector_record]]` rows:
    one over each act's ink, which a test that needs other records replaces on
    the context.
    """
    catalogue = tmp_path / "chair-config" / "serving_recipes.toml"
    catalogue.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(ROOT / "config" / "serving_recipes.toml", catalogue)
    return ["--serving-recipes-config", str(catalogue)]


def _run(program: str, root: Path, extra: list[str]) -> subprocess.CompletedProcess:
    return subprocess.run(
        [
            sys.executable,
            str(ROOT / program),
            "--run-root",
            str(root),
            "--run-id",
            "r",
            "--scenario",
            "happy",
            *extra,
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
    )


def _designator_context(designator, root: Path, extra: list[str], description: str):
    from common.stage import open_context, stage_parser

    args = stage_parser(description).parse_args(
        ["--run-root", str(root), "--run-id", "r", "--scenario", "happy", *extra]
    )
    return open_context(args, designator.DESIGNATOR)


def _prepared_context(designator, root: Path, extra: list[str], description: str):
    """Run the Door through the Ink Map, then open the matching Designator context."""
    for program in programs_through("ink-map"):
        result = _run(program, root, extra)
        assert result.returncode == 0, f"{program}: {result.stderr}"
    return _designator_context(designator, root, extra, description)


def _records(context, designator, kind: str) -> list[dict]:
    return [
        context.tree.read_artifact(designator.DESIGNATOR, kind, entry["artifact_id"])
        for entry in context.tree.build_manifest(designator.DESIGNATOR)["artifacts"]
        if entry["kind"] == kind
    ]


def _published_secondary_provenance(designator, context):
    return _records(context, designator, "secondary-provenance")[0]["payload"]


def test_declared_detections_become_page_evidence_that_decides_nothing(tmp_path):
    """Every record is kept with its oriented box, hull, score and class; none
    holds or enters an act; a collapsed box is kept uncut.
    """
    designator = load_stage("2_designator")
    context = _prepared_context(
        designator, tmp_path / "runs", _configured(tmp_path), "detector records test"
    )
    context.fixture["detector_record"] = [dict(row) for row in DECLARED_DETECTIONS]
    designator.publish_page_evidence(context, real=False)
    context.finish()
    pages = _records(context, designator, "detector-page")
    by_page = {record["payload"]["page_ordinal"]: record["payload"] for record in pages}
    assert {ordinal: page["detection_count"] for ordinal, page in by_page.items()} == {1: 3, 2: 0}
    page_id = by_page[1]["record_subjects"][0].rsplit("-detector-", 1)[0]
    assert by_page[1]["record_subjects"] == [f"{page_id}-detector-{index}" for index in range(3)]

    records = {
        record["payload"]["detector_ordinal"]: record
        for record in _records(context, designator, "detector-record")
    }
    inside, straddling, collapsed = (records[index]["payload"] for index in range(3))
    # Corners floored to their pixels, the hull reaching one pixel past the last centre.
    assert inside["bounds"] == {"x": 30, "y": 30, "w": 141, "h": 32}
    assert straddling["bounds"] == {"x": 25, "y": 95, "w": 151, "h": 56}
    assert inside["raw_proposal"]["geometry_kind"] == "obb"
    assert inside["raw_proposal"]["geometry"] == [
        {"x": 30, "y": 30},
        {"x": 170, "y": 30},
        {"x": 170, "y": 60},
        {"x": 30, "y": 61},
    ]
    assert inside["raw_proposal"]["crop_policy"] == {"mode": "aabb-enclose", "loss_recorded": False}
    assert (inside["score_bp"], inside["class_name"]) == (9731, "record")
    for payload in (inside, straddling, collapsed):
        assert payload["authoritative"] is False
        assert payload["authority_effect"] == "none"
        assert payload["quantization"] == designator.DETECTOR_QUANTIZATION
    assert all("act_overlaps" not in payload for payload in (inside, straddling, collapsed))
    assert collapsed["cut"] is False
    assert collapsed["bounds"] is None and collapsed["region_ref"] is None

    # One crop per cut record, of its hull, cut by the stage's one crop path.
    regions = {
        record["subject_id"]: record for record in _records(context, designator, "detector-region")
    }
    assert set(regions) == {records[0]["subject_id"], records[1]["subject_id"]}
    region = regions[records[0]["subject_id"]]["payload"]
    assert (region["origin"], region["padding"]) == ("detector", None)
    assert region["transform"]["bounds"] == region["raw_bounds"] == inside["bounds"]
    blob = context.tree.read_bytes(region["image_path"])
    assert hashlib.sha256(blob).hexdigest() == region["image_sha256"]

    # Records enter no act: the stage cuts no act region.
    assert _records(context, designator, "region") == []


def test_a_record_s_proposal_names_its_own_detection_after_a_collapsed_one(tmp_path):
    """Observed ordinals index the retained detector output, so a collapsed box
    ahead of a cut one does not shift the cut record's proposal onto it."""
    designator = load_stage("2_designator")
    context = _prepared_context(
        designator, tmp_path / "runs", _configured(tmp_path), "collapsed-first test"
    )
    collapsed, inside, straddling = (
        DECLARED_DETECTIONS[2],
        DECLARED_DETECTIONS[0],
        DECLARED_DETECTIONS[1],
    )
    context.fixture["detector_record"] = [dict(row) for row in (collapsed, inside, straddling)]
    designator.publish_page_evidence(context, real=False)
    context.finish()
    records = {
        record["payload"]["detector_ordinal"]: record["payload"]
        for record in _records(context, designator, "detector-record")
    }
    assert records[0]["cut"] is False and records[0]["raw_proposal"] is None
    for ordinal, declared in ((1, inside), (2, straddling)):
        proposal = records[ordinal]["raw_proposal"]
        assert proposal["observed_ordinals"] == [ordinal]
        assert proposal["score_bp"] == declared["score_bp"]
        output = json.loads(
            context.tree.read_bytes(records[ordinal]["raw_output_ref"]["relative_path"])
        )
        assert output["detections"][ordinal]["corners"] == declared["corners"]


def test_a_detector_score_enters_its_record_and_proposal_rounded_half_to_even(
    tmp_path, monkeypatch
):
    """0.00015 is 2 bp on its decimal text, where float arithmetic gives 1.4999... and 1."""
    import dataclasses

    designator = load_stage("2_designator")
    context = _prepared_context(
        designator, tmp_path / "runs", _configured(tmp_path), "score rounding test"
    )
    real_secondary = designator.secondary_provenance
    corners = DECLARED_DETECTIONS[0]["corners"]

    def scoring(context, *, real):
        record, detector = real_secondary(context, real=real)
        return record, dataclasses.replace(
            detector,
            _detect=lambda _png, ordinal: (
                [{"corners": corners, "score": 0.00015, "class_id": 0}] if ordinal == 1 else []
            ),
        )

    monkeypatch.setattr(designator, "secondary_provenance", scoring)
    assert round(0.00015 * 10_000) == 1
    designator.publish_page_evidence(context, real=False)
    context.finish()
    [record] = _records(context, designator, "detector-record")
    assert record["payload"]["score_bp"] == 2
    assert record["payload"]["raw_proposal"]["score_bp"] == 2


def test_a_retried_fixture_pass_reuses_the_in_process_detector_s_sealed_receipt(
    tmp_path, monkeypatch
):
    """Each in-process load starts at its own time, so a retry that wrote a fresh
    receipt would change the sealed secondary provenance; it reuses the sealed one."""
    import dataclasses
    import itertools

    designator = load_stage("2_designator")
    root = tmp_path / "runs"
    extra = _configured(tmp_path)
    context = _prepared_context(designator, root, extra, "secondary retry test")
    loads = itertools.count()

    def load(identity, _profile, _root):
        detector = designator.fixture_record_detector(
            [], identity, designator.fixture_serving_details(identity)
        )
        started = f"2026-01-01T00:00:{next(loads):02d}Z"
        return dataclasses.replace(
            detector,
            run_facts={**detector.run_facts, "engine": "ultralytics"},
            serving_details=dataclasses.replace(detector.serving_details, started_at=started),
        )

    monkeypatch.setattr(designator, "_record_detector_mode", lambda *_a, **_k: "in-process")
    monkeypatch.setattr(designator, "load_ultralytics_record_detector", load)
    monkeypatch.setattr(
        designator,
        "bound_serving_recipes",
        lambda *_args: types.SimpleNamespace(for_identity=lambda *_a: None),
    )
    first, _detector = designator.secondary_provenance(context, real=False)
    designator._publish_secondary_provenance(context, first)
    context.finish()

    retry = _designator_context(designator, root, extra, "secondary retry test")
    again, _detector = designator.secondary_provenance(retry, real=False)
    assert next(loads) == 2
    assert again == first == _published_secondary_provenance(designator, retry)


def test_every_declared_record_is_published_on_its_page(tmp_path):
    """Records are evidence, not holds: each one is published, and its page counts it."""
    designator = load_stage("2_designator")
    root = tmp_path / "runs"
    context = _prepared_context(designator, root, _configured(tmp_path), "records exit test")
    context.fixture["detector_record"] = [dict(row) for row in DECLARED_DETECTIONS]
    designator.publish_page_evidence(context, real=False)
    context.finish()
    records = _records(context, designator, "detector-record")
    assert sorted(record["payload"]["detector_ordinal"] for record in records) == [0, 1, 2]
    [page_one] = [
        record["payload"]
        for record in _records(context, designator, "detector-page")
        if record["payload"]["page_ordinal"] == 1
    ]
    assert page_one["detection_count"] == len(DECLARED_DETECTIONS)


def test_a_detector_row_the_stage_cannot_run_is_refused_before_anything_is_cut(tmp_path):
    """The record detector is run in-process or answered by the fixture, never served."""
    designator = load_stage("2_designator")
    root = tmp_path / "runs"
    extra = _configured(tmp_path)
    catalogue = Path(extra[extra.index("--serving-recipes-config") + 1])
    source = catalogue.read_text(encoding="utf-8")
    catalogue.write_text(
        source.replace('recipe = "fake-secondary-proposer-v0"', 'recipe = "nothing-serves-this"'),
        encoding="utf-8",
    )
    context = _prepared_context(designator, root, extra, "unrunnable detector test")
    with pytest.raises(ContractError, match="serving posture of the record detector"):
        designator.publish_page_evidence(context, real=False)
    assert _records(context, designator, "detector-record") == []
