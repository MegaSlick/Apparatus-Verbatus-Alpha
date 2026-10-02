"""The record detector's answer is checked before any stage reads it."""

import hashlib
import importlib.abc
import importlib.util
import io
import math
import os
import sys
from importlib import metadata
from types import ModuleType, SimpleNamespace

import pytest
from PIL import Image

from operations.serving import detector as detector_module
from operations.serving.detector import RecordDetector
from operations.serving.errors import ServingConfigurationError

SQUARE = [[0, 0], [10, 0], [10, 10], [0, 10]]


def _detector(*detections):
    return RecordDetector(
        run_facts={}, serving_details=None, _detect=lambda _png, _ordinal: list(detections)
    )


def test_a_detection_with_four_finite_corners_is_read():
    [read] = _detector({"corners": SQUARE, "score": 0.5, "class_id": 0}).detect(b"", page_ordinal=1)
    assert read["corners"][2] == [10.0, 10.0] and read["class_name"] == "record"


@pytest.mark.parametrize("value", [math.nan, math.inf, -math.inf, True, "1"])
def test_a_corner_that_is_not_a_finite_number_is_refused(value):
    corners = [list(point) for point in SQUARE]
    corners[1][0] = value
    detector = _detector({"corners": corners, "score": 0.5, "class_id": 0})
    with pytest.raises(ServingConfigurationError, match="four \\(x, y\\) corners"):
        detector.detect(b"", page_ordinal=1)


@pytest.mark.parametrize("score", [-0.1, 1.1, math.nan, True, "0.5", None])
def test_a_score_outside_zero_to_one_or_not_a_number_is_refused(score):
    detector = _detector({"corners": SQUARE, "score": score, "class_id": 0})
    with pytest.raises(ServingConfigurationError, match="score is not a number in \\[0, 1\\]"):
        detector.detect(b"", page_ordinal=1)


@pytest.mark.parametrize("class_id", [True, 0.0, "0", None])
def test_a_class_that_is_not_an_integer_is_refused(class_id):
    detector = _detector({"corners": SQUARE, "score": 0.5, "class_id": class_id})
    with pytest.raises(ServingConfigurationError, match="no integer class"):
        detector.detect(b"", page_ordinal=1)


def test_a_class_the_checkpoint_does_not_name_is_kept_under_its_number_with_no_name():
    [read] = _detector({"corners": SQUARE, "score": 1, "class_id": 3}).detect(b"", page_ordinal=1)
    assert (read["class_id"], read["class_name"], read["score"]) == (3, None, 1.0)


# --- load-time guards --------------------------------------------------------

PINS = {"torch": "2.13.0", "ultralytics": "8.4.14"}


def _installed(monkeypatch, versions):
    def version(package):
        if package not in versions:
            raise metadata.PackageNotFoundError(package)
        return versions[package]

    monkeypatch.setattr(
        detector_module,
        "metadata",
        SimpleNamespace(version=version, PackageNotFoundError=metadata.PackageNotFoundError),
    )


def _profile():
    return SimpleNamespace(
        required_packages=PINS,
        task="obb",
        imgsz=1024,
        conf_bp=2500,
        iou_bp=7000,
        max_det=300,
        device="cpu",
        engine="ultralytics",
    )


def test_an_installed_local_build_of_the_pinned_release_is_accepted(monkeypatch):
    _installed(monkeypatch, {"torch": "2.13.0+cu130", "ultralytics": "8.4.14"})
    assert detector_module._installed_versions(_profile())["torch"] == "2.13.0+cu130"


@pytest.mark.parametrize(
    ("versions", "refusal"),
    [
        ({"torch": "2.13.1", "ultralytics": "8.4.14"}, "pins torch==2.13.0, and 2.13.1"),
        ({"torch": "2.13.0"}, "needs ultralytics==8.4.14, and it is not installed"),
    ],
)
def test_a_package_other_than_the_pinned_release_is_refused(monkeypatch, versions, refusal):
    _installed(monkeypatch, versions)
    with pytest.raises(ServingConfigurationError, match=refusal):
        detector_module._installed_versions(_profile())


def _weights(tmp_path, monkeypatch, *, pinned: bytes, present: bytes | None):
    monkeypatch.setattr(
        detector_module, "RECORD_DETECTOR_WEIGHTS_SHA256", hashlib.sha256(pinned).hexdigest()
    )
    if present is not None:
        (tmp_path / detector_module.RECORD_DETECTOR_WEIGHTS_FILE).write_bytes(present)
    return tmp_path


def test_weights_whose_bytes_are_not_the_pinned_ones_are_refused(tmp_path, monkeypatch):
    root = _weights(tmp_path, monkeypatch, pinned=b"pinned", present=b"other")
    with pytest.raises(ServingConfigurationError, match="not the pinned"):
        detector_module._verified_weights(root)


def test_missing_weights_are_refused(tmp_path, monkeypatch):
    _installed(monkeypatch, PINS)
    root = _weights(tmp_path, monkeypatch, pinned=b"pinned", present=None)
    with pytest.raises(ServingConfigurationError, match="weights are not at"):
        detector_module.check_record_detector_runnable(_profile(), lambda: root)


def test_pinned_weights_and_packages_are_runnable(tmp_path, monkeypatch):
    _installed(monkeypatch, PINS)
    root = _weights(tmp_path, monkeypatch, pinned=b"pinned", present=b"pinned")
    detector_module.check_record_detector_runnable(_profile(), lambda: root)


@pytest.mark.parametrize(
    ("names", "refused"), [({0: "record"}, False), ({0: "record", 1: "margin"}, True)]
)
def test_the_loader_refuses_a_checkpoint_naming_other_classes(
    tmp_path, monkeypatch, names, refused
):
    _installed(monkeypatch, PINS)
    root = _weights(tmp_path, monkeypatch, pinned=b"pinned", present=b"pinned")

    class FakeYolo:
        def __init__(self, path, task):
            assert path == str(root / detector_module.RECORD_DETECTOR_WEIGHTS_FILE)
            assert task == "obb"
            self.names = names

    torch = SimpleNamespace(use_deterministic_algorithms=lambda flag: None, set_num_threads=int)
    monkeypatch.setitem(sys.modules, "torch", torch)
    _fake_ultralytics(monkeypatch, yolo=FakeYolo)
    identity = SimpleNamespace(
        source_reference="Teklia/record-detector",
        receipt_revision="a" * 40,
        digest_manifest="b" * 64,
    )

    if refused:
        with pytest.raises(ServingConfigurationError, match="names classes"):
            detector_module.load_ultralytics_record_detector(identity, _profile(), root)
    else:
        loaded = detector_module.load_ultralytics_record_detector(identity, _profile(), root)
        assert loaded.run_facts["versions"] == PINS


# --- offline import ---------------------------------------------------------


class _FakeUltralytics(importlib.abc.MetaPathFinder, importlib.abc.Loader):
    """Stands in for Ultralytics, reading its switches at import as the real one does."""

    def __init__(self, version, yolo):
        self.version, self.yolo, self.settings = version, yolo, {"sync": True, "hub": True}

    def find_spec(self, name, path=None, target=None):
        if name in ("ultralytics", "ultralytics.utils"):
            return importlib.util.spec_from_loader(name, self, is_package=name == "ultralytics")
        return None

    def create_module(self, spec):
        return None

    def exec_module(self, module: ModuleType) -> None:
        if module.__name__ == "ultralytics":
            module.__version__, module.YOLO = self.version, self.yolo
            return
        module.ONLINE = os.environ.get("YOLO_OFFLINE") != "true"
        module.AUTOINSTALL = os.environ.get("YOLO_AUTOINSTALL", "true") != "false"
        module.USER_CONFIG_DIR = os.environ.get("YOLO_CONFIG_DIR", os.path.expanduser("~"))
        module.SETTINGS = self.settings


def _fake_ultralytics(monkeypatch, *, version="8.4.14", yolo=object):
    fake = _FakeUltralytics(version, yolo)
    for name in ("ultralytics", "ultralytics.utils"):
        monkeypatch.delitem(sys.modules, name, raising=False)
    monkeypatch.setattr(sys, "meta_path", [fake, *sys.meta_path])
    return fake


def test_ultralytics_is_imported_offline_and_its_switches_do_not_outlive_the_import(
    monkeypatch,
):
    monkeypatch.delenv("YOLO_OFFLINE", raising=False)
    fake = _fake_ultralytics(monkeypatch, yolo="the YOLO class")

    assert detector_module.offline_ultralytics() == "the YOLO class"

    assert fake.settings == {"sync": False, "hub": False}
    assert "YOLO_OFFLINE" not in os.environ


def test_an_ultralytics_imported_without_its_switches_is_refused(monkeypatch):
    monkeypatch.delenv("YOLO_OFFLINE", raising=False)
    _fake_ultralytics(monkeypatch)
    importlib.import_module("ultralytics.utils")
    with pytest.raises(ServingConfigurationError, match="not offline"):
        detector_module.offline_ultralytics()


def test_an_ultralytics_release_whose_switches_were_not_read_is_refused(monkeypatch):
    _fake_ultralytics(monkeypatch, version="8.5.0")
    with pytest.raises(ServingConfigurationError, match="8.5.0 is imported"):
        detector_module.offline_ultralytics()


# --- what the detector is shown ----------------------------------------------


def _page(mode, value):
    buffer = io.BytesIO()
    Image.new(mode, (4, 2), value).save(buffer, format="TIFF" if mode == "I;16" else "PNG")
    return buffer.getvalue()


def _shown(page_bytes):
    with Image.open(io.BytesIO(detector_module.convert_page_to_rgb(page_bytes))) as image:
        return image.mode, image.getpixel((0, 0))


def test_a_16_bit_page_reaches_the_detector_with_its_ink():
    # 3000/65535 is dark ink; a bare RGB conversion clips it to white.
    with Image.open(io.BytesIO(_page("I;16", 3000))) as image:
        assert image.convert("RGB").getpixel((0, 0)) == (255, 255, 255)
    assert _shown(_page("I;16", 3000)) == ("RGB", (11, 11, 11))


@pytest.mark.parametrize(("mode", "value"), [("L", 40), ("RGB", (10, 20, 30)), ("LA", (40, 0))])
def test_an_8_bit_page_is_shown_as_its_own_rgb_conversion(mode, value):
    with Image.open(io.BytesIO(_page(mode, value))) as image:
        expected = image.convert("RGB").getpixel((0, 0))
    assert _shown(_page(mode, value)) == ("RGB", expected)


def test_a_page_with_no_display_conversion_is_refused_by_name():
    buffer = io.BytesIO()
    Image.new("F", (4, 2), 0.5).save(buffer, format="TIFF")
    with pytest.raises(ServingConfigurationError, match="cannot be shown this page as RGB"):
        detector_module.convert_page_to_rgb(buffer.getvalue())
