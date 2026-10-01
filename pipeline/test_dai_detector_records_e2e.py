"""DAI reading the records its own detector found, from the Door to its page Testimonium.

The roster is the committed one: DAI (`attestator_2`) is page-scoped and
`secondary_proposer`, DAI's own project's record detector, is configured on a
fixture row. The Door, Exemplar and Ink Map
run as real programs; the Designator runs its own `main` in process, with the
detector's boxes declared on its stage context in place of the shipped
fixture's; the Attestatores run live through `operations/serving/fakes.py`, as in
`pipeline/test_live_reading_seam_e2e.py`, whose driver this module reuses.

What it shows: DAI is asked once per detector record, in the detector's order,
shown each record's own crop; its page Testimonium lists every image it was
shown and each unit's capture; and each act's slice is the records it owns.
"""

from __future__ import annotations

import copy
import dataclasses
import json
import subprocess
import sys
from pathlib import Path

import pytest

from conftest import load_stage, programs_through

ROOT = Path(__file__).resolve().parents[1]
DESIGNATOR_DIR = ROOT / "pipeline" / "2_designator"
if str(DESIGNATOR_DIR) not in sys.path:
    sys.path.insert(0, str(DESIGNATOR_DIR))

from test_live_reading_seam_e2e import (  # noqa: E402
    CHANDRA_PAGE_ONE,
    CHANDRA_PAGE_TWO,
    CHURRO_PAGE_ONE,
    CHURRO_PAGE_TWO,
    DAI_ACT_ONE,
    DAI_ACT_TWO,
    TIER,
    TIERS,
    WitnessWorld,
    _toml_profile,
    _vllm_row,
    attestatores,
    stage_argv,
)

from common.chairs.registry import ChairRegistry  # noqa: E402
from common.contracts.errors import SchemaRefusal  # noqa: E402
from common.contracts.stages import ATTESTATORES, DESIGNATOR  # noqa: E402
from common.decoding import load_decoding_policy  # noqa: E402
from common.runtree.store import RunTree  # noqa: E402
from common.stage import EXIT_COMPLETE  # noqa: E402
from operations.serving.config import (  # noqa: E402
    chair_preflight_identity_digest,
    load_serving_recipes,
    profile_preflight_digest,
)
from operations.serving.fakes import ScriptedAnswer  # noqa: E402

designator = load_stage("2_designator")

RUN_ID = "r"
MODELS = ROOT / "config" / "models.toml"
DAI = "attestator_2"
# Page 1 holds a1 at (20, 20, 160, 80) and a2 at (20, 120, 160, 100); page 2
# holds a2's continuation at (20, 20, 160, 60). The detector finds a2's record
# first, so its order differs from act order.
DETECTIONS = (
    {
        "page_ordinal": 1,
        "corners": [[25, 130], [175, 130], [175, 210], [25, 210]],
        "score_bp": 9100,
    },
    {"page_ordinal": 1, "corners": [[25, 25], [175, 25], [175, 95], [25, 95]], "score_bp": 9500},
    {"page_ordinal": 2, "corners": [[25, 25], [175, 25], [175, 75], [25, 75]], "score_bp": 8800},
)
DAI_CONTINUATION = "zeta eta"


def _catalogue(path: Path, models: Path) -> Path:
    """Fixture rows for the Designator's three chairs, live rows for the witnesses."""
    registry = ChairRegistry.from_toml(str(models))
    rows = [
        {
            "kind": "fixture",
            "recipe": recipe,
            "chair": chair,
            "tier": tier,
            "description": "offline walking-skeleton fixture row",
        }
        for chair, recipe in (
            ("secondary_proposer", "fake-secondary-proposer-v0"),
            ("designator_surya", "fake-surya-v0"),
        )
        for tier in TIERS
    ]
    for index, chair in enumerate(("attestator_1", DAI, "attestator_3", "perlector")):
        identity = registry.resolve(chair)
        for tier in TIERS:
            row = _vllm_row(
                recipe=identity.serving_recipe, chair=chair, tier=tier, port=8400 + index
            )
            row["preflight_identity_digest"] = chair_preflight_identity_digest(identity)
            row["preflight_digest"] = profile_preflight_digest(row)
            rows.append(row)
    path.write_text(
        'schema = "serving-recipes.v1"\n\n' + "\n".join(_toml_profile(row) for row in rows),
        encoding="utf-8",
    )
    load_serving_recipes(path)
    return path


def _argv(run_root: Path, catalogue: Path, models: Path, placement_tier=None) -> list[str]:
    argv = stage_argv(run_root, catalogue, placement_tier=placement_tier)
    argv[argv.index("--models-config") + 1] = str(models)
    return argv


def _records(tree: RunTree, stage: str, kind: str) -> list[dict]:
    return [
        tree.read_artifact(stage, kind, entry["artifact_id"])
        for entry in tree.build_manifest(stage)["artifacts"]
        if entry["kind"] == kind
    ]


def _run_main(module, argv: list[str], **kwargs) -> int:
    original = sys.argv
    sys.argv = [module.__file__, *argv]
    try:
        return module.main(**kwargs)
    finally:
        sys.argv = original


def _witness(work: Path, detections, dai_answers: list[str], *, cap_stated: bool = True):
    """Run the Door through the Attestatores with these declared detections.

    `cap_stated=False` drops `max_det` from the detector's run facts, so it
    states no cap.
    """
    models = MODELS
    catalogue = _catalogue(work / "serving_recipes.toml", models)
    run_root = work / "runs"
    for program in programs_through("ink-map"):
        result = subprocess.run(
            [sys.executable, "-I", str(ROOT / program), *_argv(run_root, catalogue, models)],
            cwd=ROOT,
            capture_output=True,
            text=True,
        )
        assert result.returncode == EXIT_COMPLETE, f"{program}: {result.stderr}"

    real_open = designator._open

    def open_with_detections(args, registry_factory):
        context, real_input = real_open(args, registry_factory)
        context.fixture["detector_record"] = [dict(row) for row in detections]
        return context, real_input

    real_detector = designator.fixture_record_detector

    def uncapped_detector(rows, identity, details):
        detector = real_detector(rows, identity, details)
        facts = {key: value for key, value in detector.run_facts.items() if key != "max_det"}
        return dataclasses.replace(detector, run_facts=facts)

    patch = pytest.MonkeyPatch()
    patch.setattr(designator, "_open", open_with_detections)
    if not cap_stated:
        patch.setattr(designator, "fixture_record_detector", uncapped_detector)
    try:
        assert _run_main(designator, _argv(run_root, catalogue, models)) == EXIT_COMPLETE
    finally:
        patch.undo()

    _policy, decoding_sha256 = load_decoding_policy(str(ROOT / "config" / "decoding.toml"))
    scripts = {
        "attestator_1": [
            ScriptedAnswer(content=CHANDRA_PAGE_ONE, finish_reason="stop"),
            ScriptedAnswer(content=CHANDRA_PAGE_TWO, finish_reason="stop"),
        ],
        # One answer per detector record, in the detector's order.
        DAI: [ScriptedAnswer(content=answer, finish_reason="stop") for answer in dai_answers],
        "attestator_3": [
            ScriptedAnswer(content=CHURRO_PAGE_ONE, finish_reason="stop"),
            ScriptedAnswer(content=CHURRO_PAGE_TWO, finish_reason="stop"),
        ],
    }
    world = WitnessWorld(catalogue, decoding_sha256, work / "witness-world", scripts)
    exit_code = _run_main(
        attestatores,
        _argv(run_root, catalogue, models, placement_tier=TIER),
        serving_factory=world.factory,
    )
    assert exit_code == EXIT_COMPLETE
    return RunTree(run_root, RUN_ID), world


@pytest.fixture(scope="module")
def witnessed(tmp_path_factory):
    return _witness(
        tmp_path_factory.mktemp("dai-detector-e2e"),
        DETECTIONS,
        [DAI_ACT_TWO, DAI_ACT_ONE, DAI_CONTINUATION],
    )


def test_dai_is_asked_once_per_detector_record_and_shown_that_records_crop(witnessed):
    """One request per record; the page record lists each crop and each capture."""
    tree, world = witnessed
    regions = {
        record["subject_id"]: record["payload"]
        for record in _records(tree, DESIGNATOR, designator.DETECTOR_REGION_KIND)
    }
    pages = {
        record["payload"]["page_ordinal"]: record["payload"]
        for record in _records(tree, ATTESTATORES, "page-testimonium")
        if record["payload"]["chair"] == DAI
    }
    assert set(pages) == {1, 2}
    # One request per record, in the detector's order, after the readiness probe.
    readings = [json.loads(body)["choices"][0] for body in world.served(DAI)]
    assert [answer["message"]["content"] for answer in readings if "finish_reason" in answer] == [
        DAI_ACT_TWO,
        DAI_ACT_ONE,
        DAI_CONTINUATION,
    ]

    for page_ordinal, payload in pages.items():
        page = next(
            record["payload"]
            for record in _records(tree, DESIGNATOR, designator.DETECTOR_PAGE_KIND)
            if record["payload"]["page_ordinal"] == page_ordinal
        )
        crops = [regions[subject] for subject in page["record_subjects"]]
        # Every image DAI was shown is its record's own crop, in detector order.
        assert [shown["transform"]["bounds"] for shown in payload["presentations"]] == [
            crop["transform"]["bounds"] for crop in crops
        ]
        assert len(payload["unit_captures"]) == len(crops)
        assert [item["bounds"] for item in payload["observed"]] == [
            crop["transform"]["bounds"] for crop in crops
        ]
        # DAI reports no geometry: each box echoes the crop that unit was shown.
        assert {item["bounds_source"] for item in payload["observed"]} == {"presented"}

    # Page 1's text joins in the detector's own order: it found a2's record first.
    page_one = next(
        record
        for record in _records(tree, ATTESTATORES, "page-testimonium")
        if record["payload"]["chair"] == DAI and record["payload"]["page_ordinal"] == 1
    )
    assert page_one["outcome"] == "read"
    assert page_one["payload"]["payload"] == f"{DAI_ACT_TWO}\n{DAI_ACT_ONE}"
    spans = [item["span"] for item in page_one["payload"]["observed"]]
    assert spans == [
        {"start": 0, "end": len(DAI_ACT_TWO)},
        {"start": len(DAI_ACT_TWO) + 1, "end": len(DAI_ACT_TWO) + 1 + len(DAI_ACT_ONE)},
    ]


def test_a_page_whose_records_enclose_no_crop_is_not_run_with_its_census_count(tmp_path):
    """A record that collapses to one pixel is counted but is not a unit, so the page
    is not asked and its reason says records were found, not that none were."""
    collapsed = {
        "page_ordinal": 2,
        "corners": [[30.1, 30.2], [30.9, 30.1], [30.8, 30.9], [30.2, 30.7]],
        "score_bp": 8800,
    }
    tree, world = _witness(tmp_path, (*DETECTIONS[:2], collapsed), [DAI_ACT_TWO, DAI_ACT_ONE])
    [page_two] = [
        record
        for record in _records(tree, ATTESTATORES, "page-testimonium")
        if record["payload"]["chair"] == DAI and record["payload"]["page_ordinal"] == 2
    ]
    assert page_two["outcome"] == "not-run"
    assert page_two["payload"]["reason"] == attestatores.no_detector_unit_reason(1)
    assert "found 1 record(s)" in page_two["payload"]["reason"]
    assert page_two["inputs"] == []


def _dai_page(tree: RunTree, page_ordinal: int) -> dict:
    [page] = [
        record
        for record in _records(tree, ATTESTATORES, "page-testimonium")
        if record["payload"]["chair"] == DAI and record["payload"]["page_ordinal"] == page_ordinal
    ]
    return page


def test_a_page_the_detector_found_nothing_on_below_its_cap_is_dais_blank_testimony(tmp_path):
    """DAI looked and saw nothing: `genuinely-empty`, empty text, bound to the census.

    It is never asked about the page, shown no image and names no serving
    moment; its testimony is the detector's census of no record below its cap.
    """
    tree, world = _witness(tmp_path, DETECTIONS[:2], [DAI_ACT_TWO, DAI_ACT_ONE])
    readings = [json.loads(body)["choices"][0] for body in world.served(DAI)]
    assert [answer["message"]["content"] for answer in readings if "finish_reason" in answer] == [
        DAI_ACT_TWO,
        DAI_ACT_ONE,
    ]
    page_two = _dai_page(tree, 2)
    payload = page_two["payload"]
    assert page_two["outcome"] == "genuinely-empty"
    assert payload["payload"] == ""
    assert payload["reason"] == attestatores.NO_DETECTOR_RECORD_REASON
    # The fixed health every reader requires is exactly the health of empty text read whole.
    assert payload["content_health"] == attestatores.content_health("", completed=True)
    assert payload["content_health"] == attestatores.BLANK_TESTIMONY_HEALTH
    assert payload["presented"] == {} and payload["observed"] == []
    assert payload["provenance"]["receipt_ref"] is None
    [census] = [
        entry
        for entry in tree.build_manifest(DESIGNATOR)["artifacts"]
        if entry["kind"] == designator.DETECTOR_PAGE_KIND
        and tree.read_artifact(DESIGNATOR, entry["kind"], entry["artifact_id"])["payload"][
            "page_ordinal"
        ]
        == 2
    ]
    [bound] = page_two["inputs"]
    assert bound["relative_path"].endswith(census["artifact_id"] + ".json")


def test_a_page_dai_saw_nothing_on_is_never_asked_and_names_no_receipt(tmp_path):
    """Blank testimony is the page's: DAI is asked only about the page its detector
    found a record on, and the blank page names no serving moment."""
    tree, world = _witness(tmp_path, DETECTIONS[2:], [DAI_CONTINUATION])
    page_one = _dai_page(tree, 1)
    assert page_one["outcome"] == "genuinely-empty"
    assert page_one["payload"]["provenance"]["receipt_ref"] is None
    assert "unit_call_refs" not in page_one["payload"]
    readings = [json.loads(body)["choices"][0] for body in world.served(DAI)]
    assert [answer["message"]["content"] for answer in readings if "finish_reason" in answer] == [
        DAI_CONTINUATION
    ]


def test_a_detector_that_states_no_cap_leaves_dai_not_run_on_a_page_it_found_nothing_on(
    tmp_path,
):
    """Run facts that state no cap are incomplete, so no census is testimony: DAI is `not-run`."""
    tree, _world = _witness(tmp_path, DETECTIONS[:2], [DAI_ACT_TWO, DAI_ACT_ONE], cap_stated=False)
    page_two = _dai_page(tree, 2)
    assert page_two["outcome"] == "not-run"
    assert page_two["payload"]["reason"] == attestatores.UNCAPPED_DETECTOR_REASON
    assert page_two["payload"]["payload"] is None
    assert page_two["inputs"] == []


def test_each_unit_call_is_bound_and_held_to_the_sealed_sampling_on_resume(tmp_path, monkeypatch):
    """Every unit's call record is named on the page record and bound as an input, and
    a resumed pass holds each to the sealed sampling row and seed before reading it."""
    tree, world = _witness(tmp_path, DETECTIONS, [DAI_ACT_TWO, DAI_ACT_ONE, DAI_CONTINUATION])
    pages = [
        record
        for record in _records(tree, ATTESTATORES, "page-testimonium")
        if record["payload"]["chair"] == DAI
    ]
    for record in pages:
        references = record["payload"]["unit_call_refs"]
        assert len(references) == len(record["payload"]["presentations"])
        assert None not in references
        assert all(reference in record["inputs"] for reference in references)

    checked = []
    real = attestatores.verify_page_call_sampling

    def recording(context, payload, chair):
        if chair == DAI:
            checked.append((context, payload["page_ordinal"]))
        return real(context, payload, chair)

    monkeypatch.setattr(attestatores, "verify_page_call_sampling", recording)
    resumed = WitnessWorld(world.catalogue, world.decoding_sha256, tmp_path / "resume", {})
    argv = _argv(tmp_path / "runs", world.catalogue, MODELS, TIER)
    assert _run_main(attestatores, argv, serving_factory=resumed.factory) == EXIT_COMPLETE
    assert {ordinal for _context, ordinal in checked} == {1, 2}

    context = checked[0][0]
    payload = copy.deepcopy(pages[0]["payload"])
    call = json.loads(tree.read_bytes(payload["unit_call_refs"][0]["relative_path"]))
    call["generation_sent"]["seed"] += 1
    digest, forged = tree.put_blob(ATTESTATORES, json.dumps(call).encode("utf-8"))
    payload["unit_call_refs"][0] = {"relative_path": forged.relative_path, "sha256": digest}
    with pytest.raises(SchemaRefusal, match="unit call record is not its sealed request"):
        real(context, payload, DAI)
