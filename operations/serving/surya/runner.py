"""Run Surya's text-line detector and fast layout detector on page images.

Run in this directory's own environment, never the project's:

    operations/serving/surya/.venv/bin/python operations/serving/surya/runner.py \
        --weights <bundle> --threads 1 --output-dir <dir> page-1.png page-2.png

For the n-th page given it writes `<output-dir>/page-<n>.json`: Surya's own
results for that page, dumped from its own pydantic models, beside the facts of
the run. `--check` prints the installed versions and loads nothing.

Each model runs the way its makers wrote it, on the whole page:
`DetectionPredictor.local()` is Surya's own process-local text-line detector,
and `LayoutEngine.run_batch` is the exact call Surya's shared fast-layout
server makes for each batch. The engine is hosted in this process rather than
behind that server because the server's client attaches to any fast-layout
server already running on the host, whatever it was started with, and leaves
the one it spawns running after this process exits.

Determinism: CPU only, a fixed thread count, torch's deterministic algorithms,
one page per call, and Surya's own default thresholds, checked rather than
assumed. The same bundle, environment, thread count and CPU instruction set give
the same bytes; the run facts name the instruction set torch chose kernels for.

No network: every checkpoint is handed to Surya as a directory inside the
bundle, which Surya's loaders take as a local path before any fetch, and the
Hugging Face libraries are switched offline. Surya has no offline switch of its
own. It does read a `local.env` file for its settings wherever one sits above
its own package, so the runner refuses to run when Surya found one.

Reading order: Surya orders blocks with its learned head, or raster-sorts them
when a page has more detections than the head takes or no feature map came back
for it. Each page document records which ordering its positions come from.
"""

from __future__ import annotations

import argparse
import json
import os
import platform
import sys
from importlib import metadata
from pathlib import Path
from typing import Any

from contract import (
    CHECKPOINT_SETTINGS,
    OUTPUT_SETTINGS,
    PAGE_SCHEMA,
    BundleRefusal,
    read_bundle,
    reading_order,
)


class RunRefusal(RuntimeError):
    """The run cannot be made the way it is recorded; nothing is written."""


def _versions() -> dict[str, str]:
    return {
        "surya_ocr": metadata.version("surya-ocr"),
        "torch": metadata.version("torch"),
        "python": platform.python_version(),
    }


def _settings_environment(bundle_root: Path, bundle: dict[str, Any], threads: int) -> dict:
    """The environment Surya's settings read at import: CPU, fixed threads, no network."""
    for name in (*OUTPUT_SETTINGS, *CHECKPOINT_SETTINGS.values()):
        if name in os.environ:
            raise RunRefusal(f"{name} is set in the environment; Surya's own default is used")
    order = bundle["checkpoints"]["order"]["path"]
    return {
        "TORCH_DEVICE": "cpu",
        "FAST_DETECTOR_DEVICE": "cpu",
        "FAST_LAYOUT_NUM_THREADS": str(threads),
        "FAST_ORDER_MODEL_CHECKPOINT": str(bundle_root / order),
        "DISABLE_TQDM": "true",
        "OMP_NUM_THREADS": str(threads),
        "MKL_NUM_THREADS": str(threads),
        "HF_HUB_OFFLINE": "1",
        "TRANSFORMERS_OFFLINE": "1",
    }


def _checked_settings(settings: Any, defaults: dict[str, Any]) -> dict[str, str]:
    """Surya's output-shaping settings as they stand, refused unless each is its default.

    Written as strings: the run facts travel into records that carry no floats.
    """
    effective = {}
    for name in OUTPUT_SETTINGS:
        value = getattr(settings, name)
        if value != defaults[name]:
            raise RunRefusal(
                f"Surya setting {name} is {value!r}, not its default {defaults[name]!r}"
            )
        effective[name] = repr(value)
    return effective


def _checked_env_file(settings_class: Any) -> None:
    """Surya's settings read a `local.env` found above its package; the run records
    only settings it can see, so a found file is refused rather than read."""
    found = settings_class.model_config.get("env_file")
    if found:
        raise RunRefusal(f"Surya found a settings file at {found}; remove it, then run again")


def _require_order_head(engine: Any) -> None:
    """Surya loads the reading-order head lazily and, if it cannot, raster-sorts
    every page with only a log line; a bundle whose head does not load is refused."""
    if engine._order is None:
        raise RunRefusal("Surya's reading-order head did not load from the bundle")


def _observed(model: Any) -> list[Any]:
    """Keep what the layout detector returns to `LayoutEngine.run_batch`, unchanged,
    so the runner can tell which ordering Surya applied to each page."""
    detect = model.detect
    seen: list[Any] = []

    def observing_detect(*args: Any, **kwargs: Any) -> Any:
        detections = detect(*args, **kwargs)
        seen.append(detections)
        return detections

    model.detect = observing_detect
    return seen


def _checked_checkpoints(bundle: dict[str, Any], defaults: dict[str, Any]) -> None:
    """The bundle holds exactly the checkpoints Surya would fetch for itself."""
    for name, setting in CHECKPOINT_SETTINGS.items():
        if bundle["checkpoints"][name]["source"] != defaults[setting]:
            raise RunRefusal(
                f"the bundle's {name} checkpoint is {bundle['checkpoints'][name]['source']!r}; "
                f"Surya {bundle['surya_ocr']} loads {defaults[setting]!r}"
            )


def run(bundle_root: Path, threads: int, output_dir: Path, pages: list[Path]) -> None:
    bundle_root = bundle_root.resolve()
    try:
        bundle = read_bundle(bundle_root)
    except BundleRefusal as error:
        raise RunRefusal(str(error)) from error
    versions = _versions()
    if bundle["surya_ocr"] != versions["surya_ocr"]:
        raise RunRefusal(
            f"the bundle was fetched for surya-ocr {bundle['surya_ocr']}, and "
            f"{versions['surya_ocr']} is installed"
        )
    os.environ.update(_settings_environment(bundle_root, bundle, threads))

    # Imported only now, so Surya's settings read the environment set above.
    import torch
    from PIL import Image
    from surya.common.order import predictor as order_predictor
    from surya.detection import DetectionPredictor
    from surya.fast_layout.server import LayoutEngine
    from surya.settings import Settings, settings

    _checked_env_file(Settings)
    defaults = {name: field.default for name, field in Settings.model_fields.items()}
    effective = _checked_settings(settings, defaults)
    _checked_checkpoints(bundle, defaults)
    torch.set_num_threads(threads)
    torch.set_num_interop_threads(1)
    torch.use_deterministic_algorithms(True)
    torch.manual_seed(0)

    checkpoints = bundle["checkpoints"]
    detector = DetectionPredictor.local(
        checkpoint=str(bundle_root / checkpoints["text_detection"]["path"]),
        device="cpu",
        dtype=torch.float32,
    )
    engine = LayoutEngine(checkpoint=str(bundle_root / checkpoints["layout"]["path"]))
    if str(engine.model.device) != "cpu" or detector.model.training:
        raise RunRefusal("a Surya detector is not on the CPU in inference mode")

    facts = {
        "engine": "surya",
        **versions,
        "device": "cpu",
        # torch picks CPU kernels by instruction set, so floats are only
        # reproducible on the same one.
        "cpu_capability": torch.backends.cpu.get_cpu_capability(),
        "machine": platform.machine(),
        "threads": threads,
        "deterministic_algorithms": True,
        "settings": effective,
        "checkpoints": checkpoints,
        "weights": bundle["files"],
    }
    # The same parameters FastLayoutPredictor sends the server when a caller names none.
    layout_params = {
        "threshold": settings.FAST_LAYOUT_CONFIDENCE_THRESHOLD,
        "use_order": settings.FAST_LAYOUT_USE_ORDER,
    }
    detections = _observed(engine.model)
    documents = []
    for ordinal, path in enumerate(pages, start=1):
        # Surya's own image loader for a page image.
        image = Image.open(path).convert("RGB")
        (lines,) = detector([image])
        detections.clear()
        (layout,) = engine.run_batch([image], [dict(layout_params)])
        _require_order_head(engine)
        ((page_detections,),) = detections
        # The limit is read where Surya's own head reads it, when it runs.
        order, reason = reading_order(
            len(page_detections),
            getattr(page_detections, "features", None) is not None,
            order_predictor.MAX_BOXES,
        )
        documents.append(
            {
                "schema": PAGE_SCHEMA,
                "input_ordinal": ordinal,
                "image_size": [image.width, image.height],
                "run": facts,
                "text_detection": lines.model_dump(
                    mode="json", exclude={"heatmap", "affinity_map"}
                ),
                "layout": layout.model_dump(mode="json"),
                "reading_order": order,
                "reading_order_reason": reason,
            }
        )
    output_dir.mkdir(parents=True, exist_ok=True)
    for ordinal, document in enumerate(documents, start=1):
        text = json.dumps(document, sort_keys=True, separators=(",", ":"), allow_nan=False)
        (output_dir / f"page-{ordinal}.json").write_text(text + "\n", encoding="utf-8")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--check", action="store_true", help="print versions, load nothing")
    parser.add_argument("--weights", type=Path, help="the locked Surya weight bundle")
    parser.add_argument("--threads", type=int, help="torch CPU threads, fixed for the run")
    parser.add_argument("--output-dir", type=Path, help="where page-<n>.json is written")
    parser.add_argument("pages", nargs="*", type=Path, help="page images, in order")
    args = parser.parse_args(argv)
    if args.check:
        print(json.dumps(_versions(), sort_keys=True))
        return 0
    if args.weights is None or args.output_dir is None or not args.pages:
        parser.error("--weights, --output-dir and at least one page are required")
    if args.threads is None or args.threads < 1:
        parser.error("--threads must be a positive integer")
    try:
        run(args.weights, args.threads, args.output_dir, args.pages)
    except RunRefusal as error:
        print(f"surya runner refused: {error}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
