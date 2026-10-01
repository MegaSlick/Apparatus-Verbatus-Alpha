"""Surya's runner, run for real in its own environment, over stand-in weights.

These run only where `operations/serving/surya/.venv` has been synced
(`uv sync --locked --project operations/serving/surya`); CI never syncs it, so
there they skip. The weights are Surya's own architectures with seeded random
values (`surya/standin_bundle.py`): what is checked is the path from sealed
page bytes to checked page documents, not what Surya finds on a page.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from common.chairs.config import load_models_toml
from common.imaging import dimensions
from operations.serving.config import parse_serving_recipes
from operations.serving.surya_detector import (
    SuryaRunFailure,
    contract,
    parse_page_document,
    run_surya_subprocess,
)

ROOT = Path(__file__).resolve().parents[2]
SURYA_ENV = ROOT / "operations" / "serving" / "surya"
SURYA_PYTHON = SURYA_ENV / ".venv" / "bin" / "python"
PAGES = ROOT / "proof" / "fixtures" / "synthetic-two-page-v0"

pytestmark = pytest.mark.skipif(
    not SURYA_PYTHON.is_file(), reason="Surya's own environment is not synced on this machine"
)


def _standin(destination: Path, *extra: str) -> Path:
    result = subprocess.run(
        [
            str(SURYA_PYTHON),
            str(SURYA_ENV / "standin_bundle.py"),
            "--out",
            str(destination),
            *extra,
        ],
        capture_output=True,
        text=True,
        timeout=600,
        check=False,
    )
    assert result.returncode == 0, result.stderr[-2000:]
    return destination


@pytest.fixture(scope="module")
def bundle(tmp_path_factory) -> Path:
    return _standin(tmp_path_factory.mktemp("surya-standin") / "bundle")


def _profile():
    row = {
        "kind": "subprocess",
        "recipe": "surya-v0",
        "chair": "designator_surya",
        "tier": "generic-24gb",
        "engine": "surya",
        "environment": "operations/serving/surya",
        "device": "cpu",
        "threads": 2,
        "startup_timeout_seconds": 600,
        "seconds_per_page": 120,
        "required_packages": {"surya-ocr": "0.22.1", "torch": "2.14.0"},
    }
    (profile,) = parse_serving_recipes({"schema": "serving-recipes.v1", "profiles": [row]}).profiles
    return profile


def _identity():
    return load_models_toml(ROOT / "config" / "models.toml").chairs["designator_surya"]


def _pages() -> tuple[dict[int, bytes], dict[int, tuple[int, int]]]:
    pages = {n: (PAGES / f"page-{n}.png").read_bytes() for n in (1, 2, 3)}
    return pages, {n: dimensions(data) for n, data in pages.items()}


def _manifest(bundle: Path) -> list[dict]:
    """The rows a promoted bundle's digest manifest would carry: every file, the lock too."""
    return contract.file_rows(bundle) + [
        {
            "path": contract.BUNDLE_FILE,
            "sha256": "0" * 64,
            "size": (bundle / contract.BUNDLE_FILE).stat().st_size,
        }
    ]


def test_two_runs_over_the_same_pages_write_byte_identical_documents(bundle):
    pages, sizes = _pages()
    first = run_surya_subprocess(
        _profile(), bundle, pages, sizes, _identity(), manifest_rows=_manifest(bundle)
    )
    second = run_surya_subprocess(_profile(), bundle, pages, sizes, _identity())
    assert {n: page.raw for n, page in first.pages.items()} == {
        n: page.raw for n, page in second.pages.items()
    }
    assert first.run_facts["weights"] == contract.read_bundle(bundle)["files"]
    documents = [page.document for page in first.pages.values()]
    # The stand-in's raised biases give the detectors something to report and order.
    assert all(document["layout"]["bboxes"] for document in documents)
    assert {document["reading_order"] for document in documents} == {contract.ORDER_HEAD}


# Run in Surya's environment: the runner, with one of Surya's own fallbacks
# forced after Surya is imported and before any page is read.
_DRIVER = """
import sys
sys.path.insert(0, {environment!r})
import runner

checked_settings = runner._checked_settings


def forcing(settings, defaults):
    if {mode!r} == "max-boxes":
        from surya.common.order import predictor
        predictor.MAX_BOXES = 2
    else:
        from surya.common.rfdetr_torch import RfDetrTorch
        detect = RfDetrTorch.detect

        def without_features(self, *args, **kwargs):
            detections = detect(self, *args, **kwargs)
            for page in detections:
                page.features = None
            return detections

        RfDetrTorch.detect = without_features
    return checked_settings(settings, defaults)


runner._checked_settings = forcing
sys.exit(runner.main(sys.argv[1:]))
"""


@pytest.mark.parametrize(
    ("mode", "reason"),
    [
        ("max-boxes", "detections exceed the order head's limit of 2"),
        ("no-features", "returned no feature map"),
    ],
)
def test_a_page_surya_raster_sorted_is_recorded_as_a_raster_fallback(
    bundle, tmp_path, mode, reason
):
    page = PAGES / "page-1.png"
    output = tmp_path / "out"
    result = subprocess.run(
        [
            str(SURYA_PYTHON),
            "-c",
            _DRIVER.format(environment=str(SURYA_ENV), mode=mode),
            "--weights",
            str(bundle),
            "--threads",
            "2",
            "--output-dir",
            str(output),
            str(page),
        ],
        capture_output=True,
        text=True,
        timeout=600,
        env={},
        check=False,
    )
    assert result.returncode == 0, result.stderr[-2000:]
    width, height = dimensions(page.read_bytes())
    parsed = parse_page_document(
        (output / "page-1.json").read_bytes(), width=width, height=height, input_ordinal=1
    )
    document = parsed.document
    assert document["reading_order"] == contract.RASTER_FALLBACK
    assert reason in document["reading_order_reason"]
    assert len(document["layout"]["bboxes"]) >= 2
    assert json.loads(parsed.raw)["layout"] == document["layout"]


def test_a_bundle_whose_order_head_does_not_load_is_refused(tmp_path):
    broken = _standin(tmp_path / "broken", "--broken-order")
    pages, sizes = _pages()
    with pytest.raises(SuryaRunFailure, match="reading-order head did not load"):
        run_surya_subprocess(_profile(), broken, {1: pages[1]}, {1: sizes[1]}, _identity())
