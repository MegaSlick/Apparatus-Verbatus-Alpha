"""DAI's own record detector, run in-process by the Designator.

Teklia's YOLOv26-m OBB record detector finds the records DAI was trained to
read. The Designator runs it itself, on the CPU, so no card is shared. Two
detectors answer the same call: the Ultralytics runtime over the verified
weights, and a fixture that returns the boxes a synthetic fixture declares.

A detection is returned exactly as the engine gave it: four oriented corners
in page pixels as floats, a float score and a class. Turning that into integer
page geometry is the Designator's declared quantization, not this module's.
"""

from __future__ import annotations

import hashlib
import io
import math
from dataclasses import dataclass
from datetime import UTC, datetime
from importlib import metadata
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from common.chairs.models import ChairIdentity, ServingDetails

from .config import InProcessProfile, package_release
from .errors import ServingConfigurationError

# The one weights file the pinned revision ships, and its bytes, pinned here as
# well as in the manifest so a loader never unpickles a file nobody pinned.
RECORD_DETECTOR_WEIGHTS_FILE = "model.pt"
RECORD_DETECTOR_WEIGHTS_SHA256 = "d866ef5b683aa2e9e2c934bead1de50b98f7da2cb1b5116eeac28b61f5202cce"
# Read from the checkpoint's own metadata at the pinned revision: the one class
# it was trained on.
RECORD_DETECTOR_CLASS_NAMES = {0: "record"}
FIXTURE_ENGINE = "fixture"
# The most records the fixture detector returns for one page: Ultralytics' own
# predict default, which the in-process recipe also runs at.
FIXTURE_MAX_DET = 300


@dataclass(frozen=True, slots=True)
class RecordDetector:
    """One loaded detector: what it is, and the call that asks it about a page."""

    run_facts: Mapping[str, Any]
    serving_details: ServingDetails
    _detect: Any

    def detect(self, page_png: bytes, *, page_ordinal: int) -> list[dict[str, Any]]:
        """Every detection on one page, in the order the engine returned them."""
        return [_checked_detection(item) for item in self._detect(page_png, page_ordinal)]


def _checked_detection(item: Mapping[str, Any]) -> dict[str, Any]:
    corners = item.get("corners")
    if (
        not isinstance(corners, list)
        or len(corners) != 4
        or any(
            not isinstance(point, list)
            or len(point) != 2
            or any(
                isinstance(value, bool)
                or not isinstance(value, int | float)
                or not math.isfinite(value)
                for value in point
            )
            for point in corners
        )
    ):
        raise ServingConfigurationError("a record detection does not carry four (x, y) corners")
    score = item.get("score")
    if isinstance(score, bool) or not isinstance(score, int | float) or not 0 <= score <= 1:
        raise ServingConfigurationError("a record detection score is not a number in [0, 1]")
    class_id = item.get("class_id")
    if isinstance(class_id, bool) or not isinstance(class_id, int):
        raise ServingConfigurationError("a record detection carries no integer class")
    return {
        "corners": [[float(x), float(y)] for x, y in corners],
        "score": float(score),
        "class_id": class_id,
        "class_name": RECORD_DETECTOR_CLASS_NAMES.get(class_id, f"unknown-class-{class_id}"),
    }


def fixture_record_detector(
    rows: Sequence[Mapping[str, Any]], identity: ChairIdentity, details: ServingDetails
) -> RecordDetector:
    """Answer each page with the detections the synthetic fixture declares for it.

    At most `FIXTURE_MAX_DET` per page, the cap its run facts state.
    """

    def detect(_page_png: bytes, page_ordinal: int) -> list[dict[str, Any]]:
        return [
            {
                "corners": row["corners"],
                "score": row["score_bp"] / 10_000,
                "class_id": row.get("class_id", 0),
            }
            for row in rows
            if row.get("page_ordinal") == page_ordinal
        ][:FIXTURE_MAX_DET]

    return RecordDetector(
        run_facts={
            "engine": FIXTURE_ENGINE,
            "repo": identity.source_reference,
            "revision": identity.receipt_revision,
            "digest_manifest": identity.digest_manifest,
            "max_det": FIXTURE_MAX_DET,
        },
        serving_details=details,
        _detect=detect,
    )


def _installed_versions(profile: InProcessProfile) -> dict[str, str]:
    """The pinned package versions, refused unless each is exactly what is installed."""
    installed = {}
    for package, expected in sorted(profile.required_packages.items()):
        try:
            found = metadata.version(package)
        except metadata.PackageNotFoundError as error:
            raise ServingConfigurationError(
                f"the in-process record detector needs {package}=={expected}, and it is not "
                "installed; sync the pod dependency group before running the Designator"
            ) from error
        if package_release(found) != expected:
            raise ServingConfigurationError(
                f"the in-process record detector pins {package}=={expected}, and {found} is "
                "installed; the sealed catalogue would describe an engine that did not run"
            )
        installed[package] = found
    return installed


def _verified_weights(snapshot_root: Path) -> Path:
    path = snapshot_root / RECORD_DETECTOR_WEIGHTS_FILE
    if not path.is_file():
        raise ServingConfigurationError(
            f"the record detector weights are not at {path}; nothing is loaded"
        )
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    if digest != RECORD_DETECTOR_WEIGHTS_SHA256:
        raise ServingConfigurationError(
            f"the record detector weights at {path} hash to {digest}, not the pinned "
            f"{RECORD_DETECTOR_WEIGHTS_SHA256}; they are not loaded"
        )
    return path


def check_record_detector_runnable(
    profile: InProcessProfile, snapshot_root: Callable[[], Path]
) -> None:
    """Refuse a detector that could not load, without loading it: its pinned package
    versions must be installed, then its verified snapshot resolved and its weights
    hashed to the pinned digest."""
    _installed_versions(profile)
    _verified_weights(snapshot_root())


def load_ultralytics_record_detector(
    identity: ChairIdentity, profile: InProcessProfile, snapshot_root: Path
) -> RecordDetector:
    """Load the verified checkpoint on the CPU, deterministically, and describe the run."""
    versions = _installed_versions(profile)
    weights = _verified_weights(snapshot_root)
    # Imported only here: the laptop environment has neither package.
    import torch
    from PIL import Image
    from ultralytics import YOLO

    torch.use_deterministic_algorithms(True)
    # One thread keeps the float reduction order, and so every box, identical run to run.
    torch.set_num_threads(1)
    model = YOLO(str(weights), task=profile.task)
    names = {int(key): str(value) for key, value in model.names.items()}
    if names != RECORD_DETECTOR_CLASS_NAMES:
        raise ServingConfigurationError(
            f"the record detector names classes {names}, not the pinned "
            f"{RECORD_DETECTOR_CLASS_NAMES}"
        )

    def detect(page_png: bytes, _page_ordinal: int) -> list[dict[str, Any]]:
        with Image.open(io.BytesIO(page_png)) as image:
            rgb = image.convert("RGB")
        results = model.predict(
            rgb,
            imgsz=profile.imgsz,
            conf=profile.conf_bp / 10_000,
            iou=profile.iou_bp / 10_000,
            max_det=profile.max_det,
            device=profile.device,
            verbose=False,
        )
        obb = results[0].obb
        return [
            {"corners": corners, "score": score, "class_id": int(class_id)}
            for corners, score, class_id in zip(
                obb.xyxyxyxy.tolist(), obb.conf.tolist(), obb.cls.tolist(), strict=True
            )
        ]

    engine_version = "; ".join(f"{package} {version}" for package, version in versions.items())
    details = ServingDetails(
        tokenizer_revision=identity.receipt_revision,
        seed=0,
        # No token context; the model's input is one letterboxed imgsz square.
        context_cap=0,
        pixel_cap=profile.imgsz * profile.imgsz,
        engine=profile.engine,
        engine_version=engine_version,
        dtype="float32",
        adapter_identity=None,
        endpoint=f"in-process://{profile.device}",
        started_at=datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
    )
    return RecordDetector(
        run_facts={
            "engine": profile.engine,
            "repo": identity.source_reference,
            "revision": identity.receipt_revision,
            "digest_manifest": identity.digest_manifest,
            "weights_file": RECORD_DETECTOR_WEIGHTS_FILE,
            "weights_sha256": RECORD_DETECTOR_WEIGHTS_SHA256,
            "versions": versions,
            "device": profile.device,
            "imgsz": profile.imgsz,
            "conf_bp": profile.conf_bp,
            "iou_bp": profile.iou_bp,
            "max_det": profile.max_det,
            # The Teklia card extracts images at 2000 px before training at 1024; whether
            # their own inference resizes to 2000 first is not stated, so it is not done.
            "page_preprocessing": "sealed page as RGB into Ultralytics' own letterbox",
        },
        serving_details=details,
        _detect=detect,
    )
