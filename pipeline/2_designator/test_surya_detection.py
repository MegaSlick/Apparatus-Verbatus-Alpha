"""Surya's lines and blocks in stage 2: evidence records that decide nothing.

Cheapest first: the declared quantization with no I/O, then the fixture rows
through the real stage programs (a roster configuring the Surya chair against
fixture rows), then publication determinism on resume, then a subprocess row
run in this process.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
import tomllib
from pathlib import Path

import pytest

from common.contracts.errors import ContractError, IncompatibleReuse
from common.contracts.stages import DESIGNATOR
from common.imaging import dimensions
from common.runtree.store import RunTree
from common.stage import open_context, stage_parser
from conftest import load_stage, programs_through
from operations.serving.errors import ServingConfigurationError
from operations.serving.fakes import InProcessSurya
from operations.serving.surya_detector import fixture_surya_run
from proof.build_fixture import SURYA_BLOCKS, SURYA_LINES

ROOT = Path(__file__).resolve().parents[2]
FIXTURE_CATALOGUE = ROOT / "config" / "serving_recipes.toml"
RUN_ID = "r"
TIER = "generic-48gb"
SURYA_TIERS = ("generic-24gb", "generic-48gb", "generic-80gb-plus")
designator = load_stage("2_designator")
surya_detection = designator.surya_detection


def _artifacts(root: Path, stage: str, kind: str) -> list[dict]:
    tree = RunTree(root, RUN_ID)
    return [
        tree.read_artifact(stage, kind, entry["artifact_id"])
        for entry in tree.build_manifest(stage)["artifacts"]
        if entry["kind"] == kind
    ]


def surya_subprocess_rows(recipe: str) -> str:
    """Subprocess rows for the Surya chair at every tier."""
    return "".join(
        f'\n[[profiles]]\nkind = "subprocess"\nrecipe = "{recipe}"\nchair = "designator_surya"\n'
        f'tier = "{tier}"\nengine = "surya"\nenvironment = "operations/serving/surya"\n'
        'device = "cpu"\nthreads = 2\nstartup_timeout_seconds = 300\nseconds_per_page = 60\n'
        'required_packages = { "surya-ocr" = "0.22.1", torch = "2.14.0" }\n'
        for tier in SURYA_TIERS
    )


def in_process_surya() -> InProcessSurya:
    """Surya answering a subprocess row in this process, from the fixture's declared rows."""
    return InProcessSurya(SURYA_LINES, SURYA_BLOCKS)


def _subprocess_catalogue(destination: Path) -> Path:
    """The committed fixture catalogue with the Surya chair's rows made subprocess rows."""
    source = FIXTURE_CATALOGUE.read_text(encoding="utf-8")
    fixture_rows = "".join(
        f'\n[[profiles]]\nkind = "fixture"\nrecipe = "fake-surya-v0"\nchair = "designator_surya"\n'
        f'tier = "{tier}"\n'
        'description = "offline walking-skeleton fixture for the Surya detector chair"\n'
        for tier in SURYA_TIERS
    )
    assert fixture_rows in source, "the fixture catalogue no longer carries its Surya rows"
    path = destination / "serving_recipes_surya_subprocess.toml"
    path.write_text(
        source.replace(fixture_rows, surya_subprocess_rows("fake-surya-v0")), encoding="utf-8"
    )
    return path


def _chain(root: Path, catalogue: Path) -> None:
    for program in programs_through("ink-map"):
        result = subprocess.run(
            [
                sys.executable,
                str(program),
                "--run-root",
                str(root),
                "--run-id",
                RUN_ID,
                "--scenario",
                "happy",
                "--serving-recipes-config",
                str(catalogue),
            ],
            cwd=ROOT,
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0, f"{program}: {result.stderr}"


def _argv(root: Path, catalogue: Path, *extra: str) -> list[str]:
    return [
        str(ROOT / "pipeline" / "2_designator" / "run.py"),
        "--run-root",
        str(root),
        "--run-id",
        RUN_ID,
        "--scenario",
        "happy",
        "--serving-recipes-config",
        str(catalogue),
        *extra,
    ]


_CONFIGURED_SURYA_HEAD = '[chairs.designator_surya]\nstate = "configured"\n'


def _absent_surya_models_config(tmp_path: Path) -> Path:
    """The shipped roster with the Surya chair recorded absent, for the one
    test about what an absent chair publishes."""
    config_root = tmp_path / "chair-config"
    shutil.copytree(ROOT / "config" / "model-fixtures", config_root / "model-fixtures")
    shutil.copytree(ROOT / "config" / "manifests", config_root / "manifests")
    shipped = (ROOT / "config" / "models.toml").read_text(encoding="utf-8")
    roster = tomllib.loads(shipped)
    assert roster["chairs"]["designator_surya"]["state"] == "configured"
    head, _, rest = shipped.partition(_CONFIGURED_SURYA_HEAD)
    assert rest, "the shipped roster no longer configures the Surya chair in one block"
    # The chair's block runs to the next blank line.
    _block, _, tail = rest.partition("\n\n")
    models = config_root / "models.toml"
    models.write_text(
        head
        + '[chairs.designator_surya]\nstate = "absent"\nreason = "absent under test"\n\n'
        + tail,
        encoding="utf-8",
    )
    assert tomllib.loads(models.read_text(encoding="utf-8"))["chairs"]["designator_surya"] == {
        "state": "absent",
        "reason": "absent under test",
    }
    return models


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
    """The shipped roster and fixture catalogue, which configure the Surya chair."""
    root = tmp_path_factory.mktemp("surya-fixture") / "runs"
    _run_programs(root, "ink-free-page", "designator")
    return root


def _by_subject(root: Path, kind: str) -> dict[str, dict]:
    return {record["subject_id"]: record for record in _artifacts(root, DESIGNATOR, kind)}


def test_the_fixture_path_publishes_one_census_per_page_and_one_record_per_detection(
    fixture_run,
):
    root = fixture_run
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
            assert block["reading_order"] == payload["reading_order"] == "surya-order-head"
            assert (block["label"], block["raw_label"]) == ("Text", "Text")
        assert payload["reading_order_reason"] is None

    # Acts' ink on pages 1 and 2; page 3 carries none, and still has its census.
    counts = {page["payload"]["page_ordinal"]: page["payload"] for page in pages.values()}
    assert counts[1]["line_count"] and counts[2]["line_count"]
    assert (counts[3]["line_count"], counts[3]["block_count"]) == (0, 0)
    assert counts[3]["line_subjects"] == counts[3]["block_subjects"] == []


def test_every_surya_record_has_exactly_the_contract_fields(fixture_run):
    root = fixture_run
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
            "reading_order",
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
            "reading_order",
            "reading_order_reason",
            "raw_output_ref",
            "run",
            "quantization",
            "confidence_quantization",
            "authoritative",
            "provenance",
        }


def test_a_line_box_is_its_declared_band_in_page_pixels(fixture_run):
    root = fixture_run
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
    root = fixture_run
    for kind in ("surya-page", "surya-line", "surya-block", "surya-provenance"):
        records = _artifacts(root, DESIGNATOR, kind)
        assert records
        for record in records:
            designator._refuse_text_fields(record["payload"], kind=kind)


def test_an_absent_chair_publishes_nothing_and_the_detector_is_unchanged(fixture_run, tmp_path):
    configured_root = fixture_run
    root = tmp_path / "runs"
    models = _absent_surya_models_config(tmp_path)
    _run_programs(root, "ink-free-page", "designator", "--models-config", str(models))
    for kind in ("surya-page", "surya-line", "surya-block", "surya-provenance"):
        assert _artifacts(root, DESIGNATOR, kind) == []

    # Envelope digests seal the roster, so the records are compared by content.
    def records(run_root: Path) -> list[tuple]:
        return sorted(
            (record["subject_id"], record["payload"]["bounds"])
            for record in _artifacts(run_root, DESIGNATOR, "detector-record")
        )

    assert records(root) == records(configured_root)


# --- publication determinism ---------------------------------------------------------


def _designator_context(root: Path, scenario: str):
    args = stage_parser("surya detection test").parse_args(
        ["--run-root", str(root), "--run-id", RUN_ID, "--scenario", scenario]
    )
    return open_context(args, DESIGNATOR)


def _prepared(tmp_path: Path):
    root = tmp_path / "runs"
    _run_programs(root, "happy", "ink-map")
    context = _designator_context(root, "happy")
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
    surya_detection.publish_surya_detections(context, pages, real=False)
    first = _stage_bytes(tmp_path / "runs")
    surya_detection.publish_surya_detections(context, pages, real=False)
    assert _stage_bytes(tmp_path / "runs") == first
    assert any("surya-line" in path for path in first)


def test_a_resumed_publication_that_differs_is_refused(tmp_path, monkeypatch):
    context, pages = _prepared(tmp_path)
    surya_detection.publish_surya_detections(context, pages, real=False)

    def shifted(lines, blocks, sizes, identity, details):
        moved = [{**row, "polygon": [[x + 1, y] for x, y in row["polygon"]]} for row in lines]
        return fixture_surya_run(moved, blocks, sizes, identity, details)

    monkeypatch.setattr(surya_detection, "fixture_surya_run", shifted)
    with pytest.raises(IncompatibleReuse, match="already holds different bytes"):
        surya_detection.publish_surya_detections(context, pages, real=False)


# --- a subprocess row, checked before anything is published ------------------------


def _subprocess_setup(tmp_path: Path):
    catalogue = _subprocess_catalogue(tmp_path)
    root = tmp_path / "runs"
    _chain(root, catalogue)
    return root, catalogue


def _designator_records(root: Path) -> list[dict]:
    tree = RunTree(root, RUN_ID)
    return [
        entry
        for entry in tree.build_manifest(DESIGNATOR)["artifacts"]
        if entry["kind"] != "stage-seal"
    ]


def test_a_subprocess_row_is_checked_before_anything_is_published_and_then_run(
    tmp_path, monkeypatch
):
    root, catalogue = _subprocess_setup(tmp_path)
    seen: dict[str, object] = {}

    class Watched(InProcessSurya):
        def check(self, profile):
            seen["records_at_check"] = len(_designator_records(root))
            return super().check(profile)

        def __call__(self, profile, bundle_root, pages, sizes, identity, *, manifest_rows=None):
            seen["profile"] = (profile.kind, profile.device, profile.threads)
            seen["manifest"] = manifest_rows is not None
            assert sizes == {ordinal: dimensions(data) for ordinal, data in pages.items()}
            return super().__call__(
                profile, bundle_root, pages, sizes, identity, manifest_rows=manifest_rows
            )

    monkeypatch.chdir(ROOT)
    monkeypatch.setattr(sys, "argv", _argv(root, catalogue, "--placement-tier", TIER))
    designator.main(surya_runner=Watched(SURYA_LINES, SURYA_BLOCKS))
    assert seen == {
        "records_at_check": 0,
        "profile": ("subprocess", "cpu", 2),
        "manifest": True,
    }
    pages = _artifacts(root, DESIGNATOR, "surya-page")
    assert len(pages) == 2
    # The subprocess row's run, recorded as such: no fixture engine, no fixture receipt.
    assert {page["payload"]["run"]["engine"] for page in pages} == {"surya"}
    (provenance,) = _artifacts(root, DESIGNATOR, "surya-provenance")
    receipt = json.loads(
        (root / RUN_ID / provenance["payload"]["receipt_ref"]["relative_path"]).read_text()
    )
    assert receipt["endpoint"] == "subprocess://cpu/threads-2"


def test_a_fixture_surya_row_answers_only_a_synthetic_run(tmp_path):
    """A real submission declares no Surya rows, so a fixture row has nothing to answer."""
    context, _pages = _prepared(tmp_path)
    identity = surya_detection.resolved_surya(context)
    assert surya_detection.surya_mode(context, identity, real=False) == "fixture"
    with pytest.raises(ContractError, match="fixture row on a real submission"):
        surya_detection.surya_mode(context, identity, real=True)


def test_a_subprocess_surya_row_answers_either_run(tmp_path):
    root, catalogue = _subprocess_setup(tmp_path)
    args = stage_parser("surya detection test").parse_args(
        _argv(root, catalogue, "--placement-tier", TIER)[1:]
    )
    context = open_context(args, DESIGNATOR)
    identity = surya_detection.resolved_surya(context)
    assert surya_detection.surya_mode(context, identity, real=False) == "subprocess"
    assert surya_detection.surya_mode(context, identity, real=True) == "subprocess"


def test_an_empty_page_set_is_refused_by_name(tmp_path):
    context, _pages = _prepared(tmp_path)
    with pytest.raises(ContractError, match="no sealed page for Surya"):
        surya_detection.publish_surya_detections(context, {}, real=False)


def test_the_fixture_receipt_of_a_detector_names_no_context_or_pixel_cap(tmp_path):
    context, pages = _prepared(tmp_path)
    surya_detection.publish_surya_detections(context, pages, real=False)
    (provenance,) = _artifacts(tmp_path / "runs", DESIGNATOR, "surya-provenance")
    receipt = context.tree.read_run_receipt(provenance["payload"]["receipt_ref"])
    assert (receipt["context_cap"], receipt["pixel_cap"]) == (0, 0)
    assert receipt["endpoint"].startswith("fixture://")


def _live_run(tmp_path, monkeypatch, surya):
    root, catalogue = _subprocess_setup(tmp_path)
    monkeypatch.chdir(ROOT)
    monkeypatch.setattr(sys, "argv", _argv(root, catalogue, "--placement-tier", TIER))
    designator.main(surya_runner=surya)
    return root, catalogue


def test_a_raster_fallback_is_recorded_on_the_page_and_on_every_block(tmp_path, monkeypatch):
    reason = "3 detections exceed the order head's limit of 2"
    surya = InProcessSurya(
        SURYA_LINES, SURYA_BLOCKS, reading_orders={1: ("raster-fallback", reason)}
    )
    root, _catalogue = _live_run(tmp_path, monkeypatch, surya)
    pages = {
        record["payload"]["page_ordinal"]: record["payload"]
        for record in _artifacts(root, DESIGNATOR, "surya-page")
    }
    assert (pages[1]["reading_order"], pages[1]["reading_order_reason"]) == (
        "raster-fallback",
        reason,
    )
    assert (pages[2]["reading_order"], pages[2]["reading_order_reason"]) == (
        "surya-order-head",
        None,
    )
    blocks = {
        record["subject_id"]: record["payload"]
        for record in _artifacts(root, DESIGNATOR, "surya-block")
    }
    for page in pages.values():
        assert page["block_subjects"]
        for subject in page["block_subjects"]:
            assert blocks[subject]["reading_order"] == page["reading_order"]


def test_a_resume_on_another_cpu_instruction_set_is_refused(tmp_path, monkeypatch):
    root, catalogue = _live_run(tmp_path, monkeypatch, in_process_surya())
    args = stage_parser("surya detection test").parse_args(
        _argv(root, catalogue, "--placement-tier", TIER)[1:]
    )
    context = open_context(args, DESIGNATOR)
    pages = designator.sealed_pages(designator.page_records(context))
    same = in_process_surya()
    surya_detection.publish_surya_detections(context, pages, real=False, runner=same)
    other = in_process_surya()
    other.cpu_capability = "AVX2"
    with pytest.raises(ContractError, match="engine and CPU instruction set that sealed them"):
        surya_detection.publish_surya_detections(context, pages, real=False, runner=other)


def test_an_environment_that_is_not_ready_is_refused_before_anything_is_published(
    tmp_path, monkeypatch
):
    root, catalogue = _subprocess_setup(tmp_path)

    class Unready(InProcessSurya):
        def check(self, profile):
            raise ServingConfigurationError("Surya's environment reports surya-ocr 0.22.0")

    monkeypatch.chdir(ROOT)
    monkeypatch.setattr(sys, "argv", _argv(root, catalogue, "--placement-tier", TIER))
    with pytest.raises(ContractError, match="environment is not ready"):
        designator.main(surya_runner=Unready(SURYA_LINES, SURYA_BLOCKS))
    assert _designator_records(root) == []


def test_fixture_rows_for_an_unsealed_page_are_refused_unless_the_door_refused_it(tmp_path):
    context, pages = _prepared(tmp_path)
    without_two = {ordinal: page for ordinal, page in pages.items() if ordinal != 2}
    with pytest.raises(ContractError, match=r"rows for page\(s\) \[2\], which are not"):
        surya_detection.publish_surya_detections(context, without_two, real=False)
    surya_detection.publish_surya_detections(
        context, without_two, real=False, refused_pages=frozenset({2})
    )
    published = {
        record["payload"]["page_ordinal"]
        for record in _artifacts(tmp_path / "runs", DESIGNATOR, "surya-page")
    }
    assert published == {1}
