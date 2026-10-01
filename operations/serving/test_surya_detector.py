"""Surya's page documents, its serving row and its environment, without Surya.

Surya is never installed in the project environment, so these tests build
documents in the exact shape its runner writes (Surya's own
`TextDetectionResult` and `LayoutResult` dumps, whose boxes carry the
computed `bbox`) and drive the runner's child process through a fake.
"""

from __future__ import annotations

import copy
import dataclasses
import importlib.util
import json
import os
import re
import subprocess
import sys
import tomllib
from pathlib import Path

import pytest

from common.chairs.config import load_models_toml
from common.stage import DESIGNATOR_SURYA_CHAIR, unaddressed_chairs
from operations.serving import surya_detector
from operations.serving.client import serving_mode_for
from operations.serving.config import (
    SubprocessProfile,
    load_serving_recipes,
    parse_serving_recipes,
)
from operations.serving.errors import ServingConfigurationError
from operations.serving.surya_detector import (
    SuryaOutputRefusal,
    SuryaRunFailure,
    fixture_surya_run,
    parse_page_document,
    run_surya_subprocess,
    validate_page_document,
)

ROOT = Path(__file__).resolve().parents[2]
SURYA_ENV = ROOT / "operations" / "serving" / "surya"
contract = surya_detector.contract

WIDTH, HEIGHT = 200, 260


def _box(x0: float, y0: float, x1: float, y1: float, confidence: float | None) -> dict:
    return {
        "polygon": [[x0, y0], [x1, y0], [x1, y1], [x0, y1]],
        "confidence": confidence,
        "bbox": [x0, y0, x1, y1],
    }


def _run_facts() -> dict:
    return {
        "engine": "surya",
        "surya_ocr": "0.22.1",
        "torch": "2.14.0+cu130",
        "python": "3.12.3",
        "device": "cpu",
        "cpu_capability": "AVX512",
        "machine": "x86_64",
        "threads": 2,
        "deterministic_algorithms": True,
        "settings": {name: "None" for name in contract.OUTPUT_SETTINGS},
        "checkpoints": {
            "text_detection": {
                "source": "s3://text_detection/2025_05_07",
                "revision": None,
                "path": "text_detection/2025_05_07",
            },
            "layout": {
                "source": "hf://datalab-to/surya_layout2",
                "revision": contract.LAYOUT_REPOSITORY_REVISION,
                "path": "surya_layout2",
            },
            "order": {
                "source": "hf://datalab-to/surya_layout2/order",
                "revision": contract.LAYOUT_REPOSITORY_REVISION,
                "path": "surya_layout2/order",
            },
        },
        "weights": [{"path": "surya_layout2/rfdetr_layout.pth", "sha256": "a" * 64, "size": 1}],
    }


def _document(input_ordinal: int = 1) -> dict:
    return {
        "schema": contract.PAGE_SCHEMA,
        "input_ordinal": input_ordinal,
        "image_size": [WIDTH, HEIGHT],
        "run": _run_facts(),
        "text_detection": {
            "bboxes": [_box(20.5, 19.75, 180.25, 40.0, 0.91234), _box(20, 60, 180, 80, None)],
            "image_bbox": [0.0, 0.0, float(WIDTH), float(HEIGHT)],
        },
        "layout": {
            "bboxes": [
                {
                    **_box(20, 20, 180, 100, 0.8),
                    "label": "Text",
                    "raw_label": "Text",
                    "position": 0,
                    "count": 0,
                },
                {
                    **_box(20, 120, 180, 200, 0.7),
                    "label": "SectionHeader",
                    "raw_label": "Section-header",
                    "position": 1,
                    "count": 0,
                },
            ],
            "image_bbox": [0.0, 0.0, float(WIDTH), float(HEIGHT)],
            "raw": None,
            "error": False,
        },
        "reading_order": "surya-order-head",
        "reading_order_reason": None,
    }


def _validate(document: dict) -> dict:
    return validate_page_document(document, width=WIDTH, height=HEIGHT, input_ordinal=1)


# --- the closed page document ------------------------------------------------


def test_a_runner_shaped_document_is_accepted_as_is():
    document = _document()
    assert _validate(copy.deepcopy(document)) == document


def _mutated(path: tuple, value=None, *, delete: bool = False) -> dict:
    document = _document()
    target = document
    for key in path[:-1]:
        target = target[key]
    if delete:
        del target[path[-1]]
    else:
        target[path[-1]] = value
    return document


@pytest.mark.parametrize(
    ("path", "value", "delete", "reason"),
    [
        (("schema",), "surya-page.v0", False, "schema"),
        (("input_ordinal",), 2, False, "input_ordinal"),
        (("image_size",), [WIDTH, HEIGHT + 1], False, "image_size"),
        (("heatmap",), None, False, "unknown field"),
        (("layout", "raw"), None, True, "missing field"),
        (("text_detection", "bboxes", 0, "text"), "INK", False, "unknown field"),
        (("text_detection", "bboxes", 0, "bbox"), [20.5, 19.75, 180.0, 40.0], False, "extent"),
        (("text_detection", "bboxes", 0, "confidence"), 1.5, False, "[0, 1]"),
        (("text_detection", "bboxes", 0, "polygon"), [[0, 0], [1, 0], [1, 1]], False, "four"),
        (("text_detection", "image_bbox"), [0.0, 0.0, 199.0, 260.0], False, "whole"),
        (("layout", "bboxes", 1, "position"), 3, False, "reading order"),
        (("layout", "bboxes", 0, "label"), "", False, "non-blank"),
        (("layout", "bboxes", 0, "count"), -50, False, "non-negative"),
        (("layout", "bboxes", 1, "count"), 50, False, "is not 0"),
        (("layout", "error"), True, False, "reported an error"),
        (("layout", "error"), "no", False, "is not false"),
        (("reading_order",), "raster", False, "is not one of"),
        (("reading_order_reason",), "because", False, "null for the order head"),
        (("reading_order",), None, True, "missing field"),
        (("run", "cpu_capability"), " ", False, "non-blank"),
        (("run", "machine"), None, True, "missing field"),
        (("run", "checkpoints", "layout", "source"), "https://x", False, "s3:// or hf://"),
        (("run", "checkpoints", "order", "revision"), "0" * 40, False, "pinned Hub commit"),
        (("run", "checkpoints", "text_detection", "revision"), "v1", False, "has none of"),
        (("run", "checkpoints", "layout", "path"), "../up", False, "bundle folder"),
        (("run", "weights", 0, "sha256"), "A" * 64, False, "malformed file row"),
        (("run", "device"), "cuda", False, "deterministic CPU"),
        (("run", "deterministic_algorithms"), False, False, "deterministic CPU"),
        (("run", "threads"), 0, False, "positive"),
        (("run", "settings", "DETECTOR_TEXT_THRESHOLD"), 0.6, False, "float"),
        (("run", "settings", "EXTRA"), "1", False, "unknown field"),
        (("run", "weights"), [], False, "no weight file"),
        (("run", "engine"), "tesseract", False, "neither"),
    ],
)
def test_anything_but_the_closed_shape_is_refused_by_name(path, value, delete, reason):
    with pytest.raises(SuryaOutputRefusal, match=re.escape(reason)):
        _validate(_mutated(path, value, delete=delete))


def test_a_raster_fallback_names_its_reason():
    document = _document()
    document["reading_order"] = "raster-fallback"
    document["reading_order_reason"] = "300 detections exceed the order head's limit of 128"
    assert _validate(copy.deepcopy(document)) == document
    document["reading_order_reason"] = None
    with pytest.raises(SuryaOutputRefusal, match="non-blank reason for a raster fallback"):
        _validate(document)


@pytest.mark.parametrize(
    ("detections", "feature_map", "expected"),
    [
        (0, False, ("surya-order-head", None)),
        (1, True, ("surya-order-head", None)),
        (128, True, ("surya-order-head", None)),
        (129, True, ("raster-fallback", "129 detections exceed the order head's limit of 128")),
        (
            1,
            False,
            ("raster-fallback", "the layout detector returned no feature map for the order head"),
        ),
    ],
)
def test_the_reading_order_is_the_branch_surya_takes(detections, feature_map, expected):
    assert contract.reading_order(detections, feature_map, 128) == expected


def test_a_non_finite_coordinate_is_refused_at_parse():
    raw = json.dumps(_document()).replace("180.25", "NaN").encode()
    with pytest.raises(SuryaOutputRefusal, match="finite"):
        parse_page_document(raw, width=WIDTH, height=HEIGHT, input_ordinal=1)


def test_the_parsed_page_keeps_the_bytes_the_runner_wrote():
    raw = (json.dumps(_document(), sort_keys=True) + "\n").encode()
    page = parse_page_document(raw, width=WIDTH, height=HEIGHT, input_ordinal=1)
    assert page.raw == raw


# --- the fixture detector ----------------------------------------------------


def _identity():
    config = load_models_toml(ROOT / "config" / "models.toml")
    return config.chairs["designator_surya"]


def test_the_fixture_detector_answers_every_page_including_an_empty_one():
    lines = [
        {
            "page_ordinal": 1,
            "polygon": [[20, 20], [180, 20], [180, 40], [20, 40]],
            "confidence_bp": 9000,
        },
    ]
    blocks = [
        {
            "page_ordinal": 1,
            "polygon": [[20, 20], [180, 20], [180, 100], [20, 100]],
            "label": "Text",
            "raw_label": "Text",
            "position": 0,
            "confidence_bp": 9500,
        },
    ]
    identity = _identity()
    run = fixture_surya_run(lines, blocks, {1: (WIDTH, HEIGHT), 3: (WIDTH, HEIGHT)}, identity, None)
    assert set(run.pages) == {1, 3}
    first, empty = run.pages[1].document, run.pages[3].document
    assert first["text_detection"]["bboxes"][0]["confidence"] == 0.9
    assert first["layout"]["bboxes"][0]["bbox"] == [20.0, 20.0, 180.0, 100.0]
    assert (empty["input_ordinal"], empty["text_detection"]["bboxes"]) == (2, [])
    assert empty["layout"]["bboxes"] == []
    assert run.run_facts == {"engine": "fixture", "declared_by": "proof/skeleton_fixture.toml"}


# --- the serving row -----------------------------------------------------------


def _row(**changes) -> dict:
    row = {
        "kind": "subprocess",
        "recipe": "surya-v0",
        "chair": "designator_surya",
        "tier": "generic-24gb",
        "engine": "surya",
        "environment": "operations/serving/surya",
        "device": "cpu",
        "threads": 2,
        "startup_timeout_seconds": 300,
        "seconds_per_page": 60,
        "required_packages": {"surya-ocr": "0.22.1", "torch": "2.14.0"},
    }
    row.update(changes)
    return row


def _catalogue(*rows: dict):
    return parse_serving_recipes({"schema": "serving-recipes.v1", "profiles": list(rows)})


def test_a_subprocess_row_parses_into_its_own_profile():
    (profile,) = _catalogue(_row()).profiles
    assert isinstance(profile, SubprocessProfile)
    assert (profile.device, profile.threads, dict(profile.required_packages)) == (
        "cpu",
        2,
        {"surya-ocr": "0.22.1", "torch": "2.14.0"},
    )


@pytest.mark.parametrize(
    ("changes", "reason"),
    [
        ({"engine": "vllm"}, "engine"),
        ({"device": "cuda"}, "device"),
        ({"environment": "operations/serving"}, "runs in"),
        ({"required_packages": {"surya-ocr": "0.22.1"}}, "pins exactly"),
        ({"threads": 0}, "threads"),
        ({"port": 8000}, "unknown field"),
    ],
)
def test_a_subprocess_row_that_is_not_the_surya_shape_is_refused(changes, reason):
    with pytest.raises(ServingConfigurationError, match=reason):
        _catalogue(_row(**changes))


def test_a_subprocess_row_resolves_to_the_subprocess_mode_and_is_never_launched():
    from operations.serving import manager

    recipes = _catalogue(_row())
    config = load_models_toml(ROOT / "config" / "models.toml")
    identity = config.chairs["designator_surya"]
    surya = dataclasses.replace(identity, role="designator_surya", serving_recipe="surya-v0")
    assert serving_mode_for(recipes, surya, "generic-24gb") == "subprocess"
    with pytest.raises(ServingConfigurationError, match="no serving process is ever started"):
        manager._launchable(recipes.for_identity(surya, "generic-24gb"), surya)


# --- the roster -------------------------------------------------------------------


def test_the_surya_chair_is_in_both_rosters_and_addressed_by_stage_two():
    """Configured on the fixture roster, with a fixture row at every tier; absent
    on the real roster until a fetched bundle gives it a measured manifest."""
    fixture = tomllib.loads((ROOT / "config" / "models.toml").read_text(encoding="utf-8"))
    real = tomllib.loads((ROOT / "config" / "models-real.toml").read_text(encoding="utf-8"))
    assert fixture["chairs"][DESIGNATOR_SURYA_CHAIR]["state"] == "configured"
    assert real["chairs"][DESIGNATOR_SURYA_CHAIR]["state"] == "absent"
    fixture_rows = [
        p
        for p in load_serving_recipes(ROOT / "config" / "serving_recipes.toml").profiles
        if p.chair == DESIGNATOR_SURYA_CHAIR
    ]
    assert {(p.kind, p.tier) for p in fixture_rows} == {
        ("fixture", tier) for tier in ("generic-24gb", "generic-48gb", "generic-80gb-plus")
    }
    real_recipes = load_serving_recipes(ROOT / "config" / "serving_recipes_real.toml")
    assert not [p for p in real_recipes.profiles if p.chair == DESIGNATOR_SURYA_CHAIR]
    config = load_models_toml(ROOT / "config" / "models.toml")
    assert DESIGNATOR_SURYA_CHAIR not in unaddressed_chairs(config)


def test_the_environment_lock_pins_the_surya_release_the_rows_name():
    project = tomllib.loads((SURYA_ENV / "pyproject.toml").read_text(encoding="utf-8"))
    assert project["project"]["dependencies"] == ["surya-ocr==0.22.1"]
    lock = tomllib.loads((SURYA_ENV / "uv.lock").read_text(encoding="utf-8"))
    versions = {package["name"]: package["version"] for package in lock["package"]}
    assert versions["surya-ocr"] == _row()["required_packages"]["surya-ocr"]
    assert versions["torch"] == _row()["required_packages"]["torch"]


# --- the child process ---------------------------------------------------------------


@pytest.fixture()
def environment(tmp_path, monkeypatch):
    """A stand-in for the synced environment: an interpreter file at its path."""
    interpreter = tmp_path / "operations" / "serving" / "surya" / ".venv" / "bin" / "python"
    interpreter.parent.mkdir(parents=True)
    interpreter.write_text("")
    monkeypatch.setattr(surya_detector, "REPO_ROOT", tmp_path)
    return tmp_path


class FakeChild:
    """Answers the version check, then writes one document per page given."""

    def __init__(self, versions=None, documents=None, returncode=0, raises=None):
        self.versions = versions or {
            "surya_ocr": "0.22.1",
            "torch": "2.14.0+cu130",
            "python": "3.12.3",
        }
        self.raises = raises
        self.timeouts: list[int] = []
        self.documents = documents
        self.returncode = returncode
        self.calls: list[tuple[list[str], dict]] = []

    def __call__(self, argv, **kwargs):
        self.calls.append((argv, kwargs["env"]))
        self.timeouts.append(kwargs["timeout"])
        if self.raises is not None and "--check" not in argv:
            raise self.raises
        if "--check" in argv:
            return subprocess.CompletedProcess(argv, 0, json.dumps(self.versions), "")
        output = Path(argv[argv.index("--output-dir") + 1])
        pages = argv[argv.index("--output-dir") + 2 :]
        output.mkdir(parents=True)
        for ordinal, _page in enumerate(pages, start=1):
            if self.documents is not None and ordinal not in self.documents:
                continue
            document = (self.documents or {}).get(ordinal) or _document(ordinal)
            (output / f"page-{ordinal}.json").write_text(json.dumps(document))
        return subprocess.CompletedProcess(argv, self.returncode, "", "boom")


def _profile() -> SubprocessProfile:
    (profile,) = _catalogue(_row()).profiles
    return profile


def test_the_runner_runs_under_its_own_interpreter_with_nothing_inherited(environment, monkeypatch):
    monkeypatch.setenv("DETECTOR_TEXT_THRESHOLD", "0.1")
    monkeypatch.setenv("HF_TOKEN", "secret")
    child = FakeChild()
    run = run_surya_subprocess(
        _profile(),
        Path("/bundle"),
        {1: b"page one", 2: b"page two"},
        {1: (WIDTH, HEIGHT), 2: (WIDTH, HEIGHT)},
        _identity(),
        runner=child,
    )
    (check_argv, _), (run_argv, child_env) = child.calls
    interpreter = str(environment / "operations/serving/surya/.venv/bin/python")
    assert check_argv[:1] == run_argv[:1] == [interpreter]
    assert run_argv[2:6] == ["--weights", "/bundle", "--threads", "2"]
    assert set(child_env) <= {"LANG", "LC_ALL", "TMPDIR"}
    assert set(run.pages) == {1, 2}
    assert run.run_facts == _run_facts()
    assert run.serving_details.engine_version == (
        "surya-ocr 0.22.1; torch 2.14.0+cu130; cpu AVX512 on x86_64"
    )
    assert run.serving_details.endpoint == "subprocess://cpu/threads-2"


def test_an_environment_whose_versions_differ_from_the_row_is_refused(environment):
    child = FakeChild(versions={"surya_ocr": "0.22.0", "torch": "2.14.0", "python": "3.12.3"})
    with pytest.raises(ServingConfigurationError, match="pins"):
        surya_detector.environment_versions(_profile(), runner=child)


def test_an_unsynced_environment_is_refused_with_the_sync_command(tmp_path, monkeypatch):
    monkeypatch.setattr(surya_detector, "REPO_ROOT", tmp_path)
    with pytest.raises(ServingConfigurationError, match="uv sync --locked --project"):
        surya_detector.environment_versions(_profile(), runner=FakeChild())


def test_a_page_the_runner_wrote_nothing_for_is_refused(environment):
    child = FakeChild(documents={1: _document(1)})
    with pytest.raises(SuryaOutputRefusal, match="no document for page 2"):
        run_surya_subprocess(
            _profile(),
            Path("/b"),
            {1: b"a", 2: b"b"},
            {1: (WIDTH, HEIGHT), 2: (WIDTH, HEIGHT)},
            _identity(),
            runner=child,
        )


def test_documents_that_disagree_about_their_run_are_refused(environment):
    other = _document(2)
    other["run"]["threads"] = 3
    child = FakeChild(documents={1: _document(1), 2: other})
    with pytest.raises(SuryaOutputRefusal, match="disagree"):
        run_surya_subprocess(
            _profile(),
            Path("/b"),
            {1: b"a", 2: b"b"},
            {1: (WIDTH, HEIGHT), 2: (WIDTH, HEIGHT)},
            _identity(),
            runner=child,
        )


def _one_page(child, **kwargs):
    return run_surya_subprocess(
        _profile(), Path("/b"), {1: b"a"}, {1: (WIDTH, HEIGHT)}, _identity(), runner=child, **kwargs
    )


def test_a_failed_runner_is_refused_with_its_stderr(environment):
    with pytest.raises(SuryaRunFailure, match="boom"):
        _one_page(FakeChild(returncode=2))


@pytest.mark.parametrize(
    ("raised", "reason"),
    [
        (subprocess.TimeoutExpired(["runner"], 360), "did not finish within 360 seconds"),
        (PermissionError(13, "Permission denied"), "could not be started: .*Permission denied"),
    ],
)
def test_a_runner_that_times_out_or_cannot_start_is_a_named_failure(environment, raised, reason):
    with pytest.raises(SuryaRunFailure, match=reason):
        _one_page(FakeChild(raises=raised))


def test_the_runner_s_timeout_grows_with_the_pages_it_reads(environment):
    child = FakeChild()
    run_surya_subprocess(
        _profile(),
        Path("/b"),
        {1: b"a", 2: b"b", 3: b"c"},
        {ordinal: (WIDTH, HEIGHT) for ordinal in (1, 2, 3)},
        _identity(),
        runner=child,
    )
    # The version check gets the startup allowance; the run adds 60 s a page.
    assert child.timeouts == [300, 300 + 3 * 60]


def test_an_empty_page_set_is_refused_by_name(environment):
    with pytest.raises(SuryaOutputRefusal, match="no page to run on"):
        run_surya_subprocess(_profile(), Path("/b"), {}, {}, _identity(), runner=FakeChild())
    with pytest.raises(SuryaOutputRefusal, match="no page to run on"):
        fixture_surya_run([], [], {}, _identity(), None)


def _manifest(weights):
    return [{"path": contract.BUNDLE_FILE, "sha256": "b" * 64, "size": 9}, *weights]


def test_the_weights_a_run_names_must_be_the_files_the_manifest_pins(environment):
    weights = _run_facts()["weights"]
    assert _one_page(FakeChild(), manifest_rows=_manifest(weights)).run_facts["weights"] == weights
    other = [{**weights[0], "sha256": "c" * 64}]
    with pytest.raises(SuryaOutputRefusal, match="digest manifest pins"):
        _one_page(FakeChild(), manifest_rows=_manifest(other))
    extra = [*weights, {"path": "surya_layout2/extra.bin", "sha256": "d" * 64, "size": 1}]
    with pytest.raises(SuryaOutputRefusal, match="digest manifest pins"):
        _one_page(FakeChild(), manifest_rows=_manifest(extra))


def test_run_facts_that_name_another_version_than_the_environment_are_refused(environment):
    document = _document(1)
    document["run"]["surya_ocr"] = "0.22.0"
    with pytest.raises(SuryaOutputRefusal, match="do not describe the run"):
        _one_page(FakeChild(documents={1: document}))


def test_fixture_rows_for_a_page_that_is_not_sealed_are_refused_by_name():
    line = {"page_ordinal": 4, "polygon": [[0, 0], [1, 0], [1, 1], [0, 1]], "confidence_bp": 1}
    with pytest.raises(SuryaOutputRefusal, match=r"surya_line rows for page\(s\) \[4\]"):
        fixture_surya_run([line], [], {1: (WIDTH, HEIGHT)}, _identity(), None)


# --- the weight bundle lock -----------------------------------------------------------


def _bundle(root: Path) -> dict:
    for relative in (
        "text_detection/2025_05_07/model.safetensors",
        "surya_layout2/config.json",
        "surya_layout2/order/order_ar.pt",
    ):
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(relative.encode())
    record = contract.bundle_record(
        root, surya_ocr="0.22.1", checkpoints=copy.deepcopy(_run_facts()["checkpoints"])
    )
    (root / contract.BUNDLE_FILE).write_bytes(contract.bundle_bytes(record))
    return record


def test_a_bundle_reads_back_exactly_its_lock(tmp_path):
    record = _bundle(tmp_path)
    assert contract.read_bundle(tmp_path) == record
    assert [row["path"] for row in record["files"]] == sorted(
        row["path"] for row in record["files"]
    )


def test_a_bundle_whose_bytes_changed_is_refused(tmp_path):
    _bundle(tmp_path)
    (tmp_path / "surya_layout2/config.json").write_bytes(b"changed")
    with pytest.raises(contract.BundleRefusal, match="changed=\\['surya_layout2/config.json'\\]"):
        contract.read_bundle(tmp_path)


def test_a_bundle_with_an_unlocked_or_linked_file_is_refused(tmp_path):
    _bundle(tmp_path)
    (tmp_path / "extra.bin").write_bytes(b"x")
    with pytest.raises(contract.BundleRefusal, match="unlocked=\\['extra.bin'\\]"):
        contract.read_bundle(tmp_path)
    (tmp_path / "extra.bin").unlink()
    os.symlink(tmp_path / "surya_layout2/config.json", tmp_path / "link.json")
    with pytest.raises(contract.BundleRefusal, match="symbolic link"):
        contract.read_bundle(tmp_path)


def test_a_bundle_at_another_hub_commit_is_refused(tmp_path):
    checkpoints = copy.deepcopy(_run_facts()["checkpoints"])
    checkpoints["layout"]["revision"] = "0" * 40
    (tmp_path / "text_detection/2025_05_07").mkdir(parents=True)
    (tmp_path / "surya_layout2/order").mkdir(parents=True)
    with pytest.raises(contract.BundleRefusal, match="not the pinned Hub commit"):
        contract.bundle_record(tmp_path, surya_ocr="0.22.1", checkpoints=checkpoints)


# --- the runner's own refusals, loaded without Surya ------------------------------------


def _runner_module():
    sys.path.insert(0, str(SURYA_ENV))
    try:
        spec = importlib.util.spec_from_file_location(
            "surya_runner_under_test", SURYA_ENV / "runner.py"
        )
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module
    finally:
        sys.path.remove(str(SURYA_ENV))


def test_the_runner_refuses_a_surya_setting_set_in_its_environment(tmp_path, monkeypatch):
    runner = _runner_module()
    record = _bundle(tmp_path)
    monkeypatch.setenv("DETECTOR_TEXT_THRESHOLD", "0.2")
    with pytest.raises(runner.RunRefusal, match="DETECTOR_TEXT_THRESHOLD"):
        runner._settings_environment(tmp_path, record, 2)
    monkeypatch.delenv("DETECTOR_TEXT_THRESHOLD")
    environment = runner._settings_environment(tmp_path, record, 2)
    assert environment["TORCH_DEVICE"] == environment["FAST_DETECTOR_DEVICE"] == "cpu"
    assert environment["OMP_NUM_THREADS"] == environment["FAST_LAYOUT_NUM_THREADS"] == "2"
    assert environment["HF_HUB_OFFLINE"] == "1"


def test_the_runner_refuses_a_setting_that_is_not_surya_s_default():
    runner = _runner_module()
    defaults = {name: 0.5 for name in contract.OUTPUT_SETTINGS}
    settings = type("Settings", (), dict(defaults))()
    assert runner._checked_settings(settings, defaults) == {
        name: "0.5" for name in contract.OUTPUT_SETTINGS
    }
    settings.DETECTOR_TEXT_THRESHOLD = 0.4
    with pytest.raises(runner.RunRefusal, match="not its default"):
        runner._checked_settings(settings, defaults)


def test_the_runner_refuses_a_bundle_holding_checkpoints_surya_would_not_load(tmp_path):
    runner = _runner_module()
    record = _bundle(tmp_path)
    defaults = {
        "DETECTOR_MODEL_CHECKPOINT": "s3://text_detection/2025_05_07",
        "FAST_LAYOUT_MODEL_CHECKPOINT": "hf://datalab-to/surya_layout2",
        "FAST_ORDER_MODEL_CHECKPOINT": "hf://datalab-to/surya_layout2/order",
    }
    runner._checked_checkpoints(record, defaults)
    defaults["DETECTOR_MODEL_CHECKPOINT"] = "s3://text_detection/2026_01_01"
    with pytest.raises(runner.RunRefusal, match="text_detection"):
        runner._checked_checkpoints(record, defaults)


def test_the_runner_refuses_an_order_head_that_did_not_load():
    runner = _runner_module()
    runner._require_order_head(type("Engine", (), {"_order": object()})())
    with pytest.raises(runner.RunRefusal, match="reading-order head did not load"):
        runner._require_order_head(type("Engine", (), {"_order": None})())


def test_the_runner_refuses_a_settings_file_surya_found():
    runner = _runner_module()
    runner._checked_env_file(type("Settings", (), {"model_config": {"env_file": ""}}))
    found = type("Settings", (), {"model_config": {"env_file": "/srv/local.env"}})
    with pytest.raises(runner.RunRefusal, match="settings file at /srv/local.env"):
        runner._checked_env_file(found)


def test_the_runner_keeps_what_the_layout_detector_returns_unchanged():
    runner = _runner_module()

    class Model:
        def detect(self, images, **kwargs):
            return [["box"] * len(images)]

    model = Model()
    seen = runner._observed(model)
    assert model.detect(["page"], threshold=0.4) == [["box"]]
    assert seen == [[["box"]]]
