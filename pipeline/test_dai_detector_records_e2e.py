"""DAI reading the records its own detector found, from the Door to its page Testimonium.

The roster here is the committed fixture roster with two changes: DAI
(`attestator_2`) is page-scoped and `secondary_proposer`, DAI's own project's
record detector, is configured on a fixture row. The Door, Exemplar and Ink Map
run as real programs; the Designator runs its own `main` in process, with the
detector's boxes declared on its stage context (the shipped fixture declares
none); the Attestatores run live through `operations/serving/fakes.py`, as in
`pipeline/test_live_reading_seam_e2e.py`, whose driver this module reuses.

What it shows: DAI is asked once per detector record, in the detector's order,
shown each record's own crop; its page Testimonium lists every image it was
shown and each unit's capture; and each act's slice is the records it owns.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
import tomllib
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
DAI = "attestator_2"
_DAI_ACT_SCOPE = 'witness_adapter = "dai.v1"\nwitness_scope = "act"\n'
_DAI_PAGE_SCOPE = 'witness_adapter = "dai.v1"\nwitness_scope = "page"\n'
_ABSENT_DETECTOR = """[chairs.secondary_proposer]
state = \"absent\"
reason = \"no secondary proposer is configured for the offline walking skeleton\"
"""
_FIXTURE_DETECTOR = """[chairs.secondary_proposer]
state = \"configured\"
source = \"local-repository\"
path = \"designator_structure\"
digest_manifest = \"{digest_manifest}\"
manifest = \"manifests/designator_structure.json\"
serving_recipe = \"fake-secondary-proposer-v0\"
license_note = \"fixture identity only; no model weights or model license apply\"
"""

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


def _config(work: Path) -> Path:
    """The fixture roster with DAI page-scoped and its detector on a fixture row."""
    config_root = work / "config"
    shutil.copytree(ROOT / "config" / "model-fixtures", config_root / "model-fixtures")
    shutil.copytree(ROOT / "config" / "manifests", config_root / "manifests")
    shipped = (ROOT / "config" / "models.toml").read_text(encoding="utf-8")
    assert shipped.count(_DAI_ACT_SCOPE) == 1 and _ABSENT_DETECTOR in shipped
    digest = tomllib.loads(shipped)["chairs"]["designator_structure"]["digest_manifest"]
    models = config_root / "models.toml"
    models.write_text(
        shipped.replace(_DAI_ACT_SCOPE, _DAI_PAGE_SCOPE).replace(
            _ABSENT_DETECTOR, _FIXTURE_DETECTOR.format(digest_manifest=digest)
        ),
        encoding="utf-8",
    )
    return models


def _catalogue(path: Path, models: Path) -> Path:
    """Fixture rows for the Designator's two chairs, live rows for the witnesses."""
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
            ("designator_structure", "fake-designator-v0"),
            ("secondary_proposer", "fake-secondary-proposer-v0"),
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


@pytest.fixture(scope="module")
def witnessed(tmp_path_factory):
    work = tmp_path_factory.mktemp("dai-detector-e2e")
    models = _config(work)
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
        context.fixture["detector_record"] = [dict(row) for row in DETECTIONS]
        return context, real_input

    patch = pytest.MonkeyPatch()
    patch.setattr(designator, "_open", open_with_detections)
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
        DAI: [
            ScriptedAnswer(content=DAI_ACT_TWO, finish_reason="stop"),
            ScriptedAnswer(content=DAI_ACT_ONE, finish_reason="stop"),
            ScriptedAnswer(content=DAI_CONTINUATION, finish_reason="stop"),
        ],
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
        assert {item["bounds_source"] for item in payload["observed"]} == {"native"}

    # Page 1's text joins in act order although the detector found a2's record first.
    page_one = next(
        record
        for record in _records(tree, ATTESTATORES, "page-testimonium")
        if record["payload"]["chair"] == DAI and record["payload"]["page_ordinal"] == 1
    )
    assert page_one["outcome"] == "read"
    assert page_one["payload"]["payload"] == f"{DAI_ACT_ONE}\n{DAI_ACT_TWO}"
    spans = [item["span"] for item in page_one["payload"]["observed"]]
    assert spans == [
        {"start": len(DAI_ACT_ONE) + 1, "end": len(DAI_ACT_ONE) + 1 + len(DAI_ACT_TWO)},
        {"start": 0, "end": len(DAI_ACT_ONE)},
    ]


def test_each_act_is_placed_in_dais_page_text_by_the_records_it_owns(witnessed):
    """Each act's DAI entry on its own page spans exactly the records it owns.

    A continuation page carries no act anchor for any page witness, DAI included,
    so a2's page-2 entry attaches by geometry and stays unaligned.
    """
    tree, _world = witnessed
    placed = {
        record["payload"]["act_key"]: {
            entry["page_ordinal"]: (
                entry["attached"],
                entry["span"],
                entry["alignment"].get("anchor_basis") or entry["alignment"].get("reason"),
                [line["bbox"] for line in entry["alignment"].get("line_geometry", [])],
            )
            for entry in record["payload"]["attachments"]
            if entry["chair"] == DAI
        }
        for record in _records(tree, ATTESTATORES, "act-attachment")
    }
    two_start = len(DAI_ACT_ONE) + 1
    assert placed == {
        "a1": {
            1: (
                True,
                {"start": 0, "end": len(DAI_ACT_ONE)},
                "detector-record",
                [{"x": 25, "y": 25, "w": 151, "h": 71}],
            ),
        },
        "a2": {
            1: (
                True,
                {"start": two_start, "end": two_start + len(DAI_ACT_TWO)},
                "detector-record",
                [{"x": 25, "y": 130, "w": 151, "h": 81}],
            ),
            2: (True, None, "continuation-page-no-act-anchor", []),
        },
    }
