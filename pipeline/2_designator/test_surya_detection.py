"""Surya's lines and blocks in stage 2: evidence records that decide nothing.

Cheapest first: the declared quantization with no I/O, then the fixture path
through the real stage programs (a roster configuring the Surya chair against
fixture rows), then publication determinism on resume, then the live pass's
ordering around the structure chair.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
import tomllib
from pathlib import Path

import pytest
from test_structure_pass import (
    FIXTURE_CATALOGUE,
    RUN_ID,
    SERVED_MODEL_ID,
    STRUCTURE_ANSWER_KIND,
    TIER,
    _argv,
    _artifacts,
    _chain,
    _happy_answers,
    _live_catalogue,
    _serving_factory,
    designator,
)

from common.contracts.errors import ContractError, IncompatibleReuse
from common.contracts.stages import DESIGNATOR
from common.imaging import dimensions
from common.stage import open_context, stage_parser
from conftest import programs_through
from operations.serving.fakes import FakeEndpoint
from operations.serving.surya_detector import fixture_surya_run
from proof.build_fixture import SURYA_BLOCKS, SURYA_LINES

ROOT = Path(__file__).resolve().parents[2]
surya_detection = designator.surya_detection

_ABSENT_SURYA = """[chairs.designator_surya]
state = "absent"
reason = "no Surya detector is configured for the offline walking skeleton"
"""

_CONFIGURED_SURYA = """[chairs.designator_surya]
state = "configured"
source = "local-repository"
path = "designator_structure"
digest_manifest = "{digest_manifest}"
manifest = "manifests/designator_structure.json"
serving_recipe = "{recipe}"
license_note = "fixture identity only; no model weights or model license apply"
"""

_TIERS = ("generic-24gb", "generic-48gb", "generic-80gb-plus")


def _fixture_rows(recipe: str) -> str:
    return "".join(
        f'\n[[profiles]]\nkind = "fixture"\nrecipe = "{recipe}"\nchair = "designator_surya"\n'
        f'tier = "{tier}"\ndescription = "Surya fixture rows under test"\n'
        for tier in _TIERS
    )


def _subprocess_rows(recipe: str) -> str:
    return "".join(
        f'\n[[profiles]]\nkind = "subprocess"\nrecipe = "{recipe}"\nchair = "designator_surya"\n'
        f'tier = "{tier}"\nengine = "surya"\nenvironment = "operations/serving/surya"\n'
        'device = "cpu"\nthreads = 2\ntimeout_seconds = 600\n'
        'required_packages = { "surya-ocr" = "0.22.1", torch = "2.14.0" }\n'
        for tier in _TIERS
    )


def _surya_models_config(tmp_path: Path, recipe: str) -> Path:
    """The shipped roster with the Surya chair configured, standing on the
    structure chair's own fixture snapshot."""
    config_root = tmp_path / "chair-config"
    shutil.copytree(ROOT / "config" / "model-fixtures", config_root / "model-fixtures")
    shutil.copytree(ROOT / "config" / "manifests", config_root / "manifests")
    shipped = (ROOT / "config" / "models.toml").read_text(encoding="utf-8")
    assert _ABSENT_SURYA in shipped
    digest = tomllib.loads(shipped)["chairs"]["designator_structure"]["digest_manifest"]
    models = config_root / "models.toml"
    models.write_text(
        shipped.replace(
            _ABSENT_SURYA, _CONFIGURED_SURYA.format(digest_manifest=digest, recipe=recipe)
        ),
        encoding="utf-8",
    )
    return models


def _catalogue(base: Path, rows: str, source: Path = FIXTURE_CATALOGUE) -> Path:
    path = base / f"serving_recipes_surya_{source.stem}.toml"
    path.write_text(source.read_text(encoding="utf-8") + rows, encoding="utf-8")
    return path


# --- the declared quantization -------------------------------------------------


def test_corners_floor_to_their_pixel_and_clamp_to_the_page():
    polygon = [[-3.2, 0.99], [19.999, 0.0], [250.0, 30.5], [4.0, 300.0]]
    corners = surya_detection.quantized_corners(polygon, 200, 260)
    assert corners == [
        {"x": 0, "y": 0},
        {"x": 19, "y": 0},
        {"x": 199, "y": 30},
        {"x": 4, "y": 259},
    ]


def test_bounds_are_the_half_open_hull_of_the_corner_pixels():
    corners = surya_detection.quantized_corners(
        [[20.0, 20.0], [180.0, 20.0], [180.0, 40.0], [20.0, 40.0]], 200, 260
    )
    # A corner on pixel 180 names that pixel, so the box reaches one past it.
    assert surya_detection.quantized_bounds(corners, 200, 260) == {
        "x": 20,
        "y": 20,
        "w": 161,
        "h": 21,
    }
    edge = surya_detection.quantized_corners(
        [[0.0, 0.0], [200.0, 0.0], [200.0, 260.0], [0.0, 260.0]], 200, 260
    )
    assert surya_detection.quantized_bounds(edge, 200, 260) == {
        "x": 0,
        "y": 0,
        "w": 200,
        "h": 260,
    }


@pytest.mark.parametrize(
    ("confidence", "basis_points"),
    [(None, None), (0.0, 0), (1.0, 10000), (0.91234, 9123), (0.00005, 0), (0.00015, 2)],
)
def test_confidence_rounds_half_to_even_in_basis_points(confidence, basis_points):
    assert surya_detection.confidence_bp(confidence) == basis_points


# --- the fixture path through the real stage programs ----------------------------


def _run_programs(root: Path, scenario: str, last: str, *extra: str) -> None:
    for program in programs_through(last):
        result = subprocess.run(
            [
                sys.executable,
                str(ROOT / program),
                "--run-root",
                str(root),
                "--run-id",
                RUN_ID,
                "--scenario",
                scenario,
                *extra,
            ],
            cwd=ROOT,
            capture_output=True,
            text=True,
        )
        assert result.returncode in (0, 3), f"{program}: {result.stderr}"


@pytest.fixture(scope="module")
def fixture_run(tmp_path_factory):
    base = tmp_path_factory.mktemp("surya-fixture")
    models = _surya_models_config(base, "fake-surya-v0")
    catalogue = _catalogue(base, _fixture_rows("fake-surya-v0"))
    root = base / "runs"
    extra = ("--models-config", str(models), "--serving-recipes-config", str(catalogue))
    _run_programs(root, "ink-free-page", "designator", *extra)
    return root, models, catalogue


def _by_subject(root: Path, kind: str) -> dict[str, dict]:
    return {record["subject_id"]: record for record in _artifacts(root, DESIGNATOR, kind)}


def test_the_fixture_path_publishes_one_census_per_page_and_one_record_per_detection(
    fixture_run,
):
    root, _models, _catalogue_path = fixture_run
    pages = _by_subject(root, "surya-page")
    lines = _by_subject(root, "surya-line")
    blocks = _by_subject(root, "surya-block")
    assert sorted(page["payload"]["page_ordinal"] for page in pages.values()) == [1, 2, 3]

    for page_id, page in pages.items():
        payload = page["payload"]
        ordinal = payload["page_ordinal"]
        declared_lines = [row for row in SURYA_LINES if row["page_ordinal"] == ordinal]
        declared_blocks = [row for row in SURYA_BLOCKS if row["page_ordinal"] == ordinal]
        assert payload["line_count"] == len(declared_lines)
        assert payload["block_count"] == len(declared_blocks)
        assert payload["line_subjects"] == [
            f"{page_id}-surya-line-{n}" for n in range(1, len(declared_lines) + 1)
        ]
        assert payload["block_subjects"] == [
            f"{page_id}-surya-block-{n}" for n in range(1, len(declared_blocks) + 1)
        ]
        assert payload["authoritative"] is False
        assert payload["run"] == {"engine": "fixture", "declared_by": "proof/skeleton_fixture.toml"}
        for n, (subject, row) in enumerate(
            zip(payload["line_subjects"], declared_lines, strict=True), 1
        ):
            line = lines[subject]["payload"]
            assert (line["n"], line["page_id"], line["authoritative"]) == (n, page_id, False)
            assert line["confidence_bp"] == row["confidence_bp"]
            assert line["raw_output_ref"] == payload["raw_output_ref"]
        for n, (subject, row) in enumerate(
            zip(payload["block_subjects"], declared_blocks, strict=True), 1
        ):
            block = blocks[subject]["payload"]
            assert block["reading_order_position"] == n - 1 == row["position"]
            assert (block["label"], block["raw_label"]) == ("Text", "Text")

    # Acts' ink on pages 1 and 2; page 3 carries none, and still has its census.
    counts = {page["payload"]["page_ordinal"]: page["payload"] for page in pages.values()}
    assert counts[1]["line_count"] and counts[2]["line_count"]
    assert (counts[3]["line_count"], counts[3]["block_count"]) == (0, 0)
    assert counts[3]["line_subjects"] == counts[3]["block_subjects"] == []


def test_every_surya_record_has_exactly_the_contract_fields(fixture_run):
    root, _models, _catalogue_path = fixture_run
    common = {
        "schema",
        "page_id",
        "page_ordinal",
        "n",
        "polygon_px",
        "bounds",
        "quantization",
        "confidence_bp",
        "confidence_quantization",
        "raw_output_ref",
        "authoritative",
        "provenance",
    }
    for record in _artifacts(root, DESIGNATOR, "surya-line"):
        assert set(record["payload"]) == common
        assert record["payload"]["schema"] == "surya-line.v1"
    for record in _artifacts(root, DESIGNATOR, "surya-block"):
        assert set(record["payload"]) == common | {
            "label",
            "raw_label",
            "reading_order_position",
            "count",
        }
        assert record["payload"]["schema"] == "surya-block.v1"
    for record in _artifacts(root, DESIGNATOR, "surya-page"):
        assert set(record["payload"]) == {
            "schema",
            "page_id",
            "page_ordinal",
            "page_width_px",
            "page_height_px",
            "line_count",
            "block_count",
            "line_subjects",
            "block_subjects",
            "layout_error",
            "raw_output_ref",
            "run",
            "quantization",
            "confidence_quantization",
            "authoritative",
            "provenance",
        }


def test_a_line_box_is_its_declared_band_in_page_pixels(fixture_run):
    root, _models, _catalogue_path = fixture_run
    pages = _by_subject(root, "surya-page")
    page_id, page = next(
        (subject, record)
        for subject, record in pages.items()
        if record["payload"]["page_ordinal"] == 1
    )
    first = _by_subject(root, "surya-line")[f"{page_id}-surya-line-1"]["payload"]
    row = next(row for row in SURYA_LINES if row["page_ordinal"] == 1)
    (x0, y0), _, (x1, y1), _ = row["polygon"]
    assert first["polygon_px"] == [
        {"x": x0, "y": y0},
        {"x": x1, "y": y0},
        {"x": x1, "y": y1},
        {"x": x0, "y": y1},
    ]
    assert first["bounds"] == {"x": x0, "y": y0, "w": x1 - x0 + 1, "h": y1 - y0 + 1}
    assert (
        page["payload"]["quantization"] == first["quantization"] == ("surya-corner-floor-clamp.v1")
    )


def test_the_surya_records_carry_no_text(fixture_run):
    root, _models, _catalogue_path = fixture_run
    for kind in ("surya-page", "surya-line", "surya-block", "surya-provenance"):
        records = _artifacts(root, DESIGNATOR, kind)
        assert records
        for record in records:
            designator._refuse_text_fields(record["payload"], kind=kind)


def test_an_absent_chair_publishes_nothing_and_the_acts_are_the_same(fixture_run, tmp_path):
    configured_root, _models, _catalogue_path = fixture_run
    root = tmp_path / "runs"
    _run_programs(root, "ink-free-page", "designator")
    for kind in ("surya-page", "surya-line", "surya-block", "surya-provenance"):
        assert _artifacts(root, DESIGNATOR, kind) == []

    # Envelope digests seal the roster, so the acts are compared by identity.
    def acts(run_root: Path) -> list[tuple]:
        (seal,) = _artifacts(run_root, DESIGNATOR, "proposal-seal")
        return [
            (row["act_id"], row["act_key"], row["outcome"], row["page_ordinal"])
            for row in seal["payload"]["expected_acts"]
        ]

    def held(run_root: Path) -> list[str]:
        return sorted(record["subject_id"] for record in _artifacts(run_root, DESIGNATOR, "hold"))

    assert acts(root) == acts(configured_root)
    assert held(root) == held(configured_root)


# --- publication determinism ---------------------------------------------------------


def _designator_context(root: Path, models: Path, catalogue: Path, scenario: str):
    args = stage_parser("surya detection test").parse_args(
        [
            "--run-root",
            str(root),
            "--run-id",
            RUN_ID,
            "--scenario",
            scenario,
            "--models-config",
            str(models),
            "--serving-recipes-config",
            str(catalogue),
        ]
    )
    return open_context(args, DESIGNATOR)


def _prepared(tmp_path: Path):
    models = _surya_models_config(tmp_path, "fake-surya-v0")
    catalogue = _catalogue(tmp_path, _fixture_rows("fake-surya-v0"))
    root = tmp_path / "runs"
    extra = ("--models-config", str(models), "--serving-recipes-config", str(catalogue))
    _run_programs(root, "happy", "ink-map", *extra)
    context = _designator_context(root, models, catalogue, "happy")
    pages = designator.sealed_pages(designator.page_records(context))
    return context, pages


def _stage_bytes(root: Path) -> dict[str, bytes]:
    directory = root / RUN_ID / "2_designator"
    return {
        path.relative_to(directory).as_posix(): path.read_bytes()
        for path in sorted(directory.rglob("*"))
        if path.is_file()
    }


def test_a_resumed_publication_writes_identical_bytes(tmp_path):
    context, pages = _prepared(tmp_path)
    surya_detection.publish_surya_detections(context, pages, live=False)
    first = _stage_bytes(tmp_path / "runs")
    surya_detection.publish_surya_detections(context, pages, live=False)
    assert _stage_bytes(tmp_path / "runs") == first
    assert any("surya-line" in path for path in first)


def test_a_resumed_publication_that_differs_is_refused(tmp_path, monkeypatch):
    context, pages = _prepared(tmp_path)
    surya_detection.publish_surya_detections(context, pages, live=False)

    def shifted(lines, blocks, sizes, identity, details):
        moved = [{**row, "polygon": [[x + 1, y] for x, y in row["polygon"]]} for row in lines]
        return fixture_surya_run(moved, blocks, sizes, identity, details)

    monkeypatch.setattr(surya_detection, "fixture_surya_run", shifted)
    with pytest.raises(IncompatibleReuse, match="already holds different bytes"):
        surya_detection.publish_surya_detections(context, pages, live=False)


# --- the live pass: checked before the structure chair, run after it -----------------


def _live_setup(tmp_path: Path, rows: str):
    models = _surya_models_config(tmp_path, "surya-v0")
    catalogue = _catalogue(tmp_path, rows, source=_live_catalogue(tmp_path))
    root = tmp_path / "runs"
    _chain(root, catalogue, "--models-config", str(models))
    endpoint = FakeEndpoint(served_model_id=SERVED_MODEL_ID)
    endpoint.script(*_happy_answers())
    factory = _serving_factory(
        endpoint, catalogue, tmp_path / "logs", tmp_path / "lock", ROOT / "config" / "decoding.toml"
    )
    return root, models, catalogue, endpoint, factory


def test_the_live_pass_runs_surya_only_after_the_structure_chair_has_answered(
    tmp_path, monkeypatch
):
    root, models, catalogue, endpoint, factory = _live_setup(tmp_path, _subprocess_rows("surya-v0"))
    seen: dict[str, object] = {}

    def fake_subprocess(profile, bundle_root, pages, sizes, identity):
        seen["answers_before"] = len(_artifacts(root, DESIGNATOR, STRUCTURE_ANSWER_KIND))
        seen["requests_before"] = len(endpoint.requests)
        seen["profile"] = (profile.kind, profile.device, profile.threads)
        assert sizes == {ordinal: dimensions(data) for ordinal, data in pages.items()}
        rows = [row for row in SURYA_LINES if row["page_ordinal"] in pages]
        blocks = [row for row in SURYA_BLOCKS if row["page_ordinal"] in pages]
        return fixture_surya_run(
            rows, blocks, sizes, identity, surya_detection.fixture_serving_details(identity)
        )

    monkeypatch.setattr(designator.surya_detection, "run_surya_subprocess", fake_subprocess)
    monkeypatch.chdir(ROOT)
    monkeypatch.setattr(
        sys,
        "argv",
        _argv(root, catalogue, "--placement-tier", TIER, "--models-config", str(models)),
    )
    designator.main(serving_factory=factory)
    assert seen == {"answers_before": 2, "requests_before": 2, "profile": ("subprocess", "cpu", 2)}
    assert len(_artifacts(root, DESIGNATOR, "surya-page")) == 2


def test_a_fixture_surya_row_is_refused_by_the_live_pass_before_any_chair_is_asked(
    tmp_path, monkeypatch
):
    root, models, catalogue, endpoint, factory = _live_setup(tmp_path, _fixture_rows("surya-v0"))
    monkeypatch.chdir(ROOT)
    monkeypatch.setattr(
        sys,
        "argv",
        _argv(root, catalogue, "--placement-tier", TIER, "--models-config", str(models)),
    )
    with pytest.raises(ContractError, match="a fixture row answers only the fixture pass"):
        designator.main(serving_factory=factory)
    assert endpoint.requests == []
    assert _artifacts(root, DESIGNATOR, "surya-page") == []
