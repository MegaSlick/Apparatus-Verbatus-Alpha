"""Build a stand-in weight bundle: Surya's three architectures with seeded random weights.

For tests only, never for a run that publishes: it lets the runner's whole path
(Surya's loaders, both detectors, the reading-order head, the bundle lock) run
on a machine that has never fetched Surya's real weights. Run it in this
directory's own environment:

    operations/serving/surya/.venv/bin/python operations/serving/surya/standin_bundle.py \
        --out <dir> [--broken-order]

The bundle is locked like a fetched one, with Surya's own checkpoint sources at
the pinned Hub commit, so the runner's checks pass on it. `--broken-order`
writes a reading-order checkpoint Surya cannot load, which the runner must
refuse.
"""

from __future__ import annotations

import argparse
import json
from importlib import metadata
from pathlib import Path
from types import SimpleNamespace

from contract import BUNDLE_FILE, LAYOUT_REPOSITORY_REVISION, bundle_bytes, bundle_record

DETECTION = "text_detection/2025_05_07"
LAYOUT = "surya_layout2"
ORDER = "surya_layout2/order"
# The resolution and patch grid the order head's checkpoint records by default.
RESOLUTION = 448
FEATURE_GRID = 28
# Raised output biases, so the random detectors report boxes above Surya's
# default thresholds and the runner has detections to order and to compare.
DETECTION_BIAS = 3.0
LAYOUT_BIAS = 4.0


def build(out: Path, *, broken_order: bool) -> None:
    import torch
    from surya.common.order.order_ar import LAYOUT_CLASSES, ReadingOrderAR
    from surya.common.rfdetr.models import build_model
    from surya.common.rfdetr.predictor import LARGE_ARGS
    from surya.detection.model.config import EfficientViTConfig
    from surya.detection.model.encoderdecoder import EfficientViTForSemanticSegmentation
    from surya.detection.processor import SegformerImageProcessor

    torch.manual_seed(1)
    detection = out / DETECTION
    detection.mkdir(parents=True)
    detector = EfficientViTForSemanticSegmentation(EfficientViTConfig())
    with torch.no_grad():
        detector.decode_head.classifier.bias += DETECTION_BIAS
    detector.save_pretrained(detection)
    SegformerImageProcessor(size={"height": 1200, "width": 1200}).save_pretrained(detection)

    layout = out / LAYOUT
    (out / ORDER).mkdir(parents=True)
    arguments = SimpleNamespace(
        **{**LARGE_ARGS, "resolution": RESOLUTION, "positional_encoding_size": FEATURE_GRID}
    )
    arguments.device = "cpu"
    arguments.pretrain_weights = None
    state = build_model(arguments).state_dict()
    state["class_embed.bias"] = state["class_embed.bias"] + LAYOUT_BIAS
    torch.save({"model": state}, layout / "rfdetr_layout.pth")
    config = {
        "arch": "rf-detr-large",
        "resolution": RESOLUTION,
        "positional_encoding_size": FEATURE_GRID,
        "weights": "rfdetr_layout.pth",
        "categories": [{"id": i, "name": name} for i, name in enumerate(LAYOUT_CLASSES)],
    }
    (layout / "config.json").write_text(json.dumps(config), encoding="utf-8")
    order = out / ORDER / "order_ar.pt"
    if broken_order:
        order.write_bytes(b"not a checkpoint")
    else:
        head = ReadingOrderAR(d=128, layers=3, feat_dim=256, feat_hw=FEATURE_GRID, dropout=0.0)
        torch.save(
            {
                "model": head.state_dict(),
                "d": 128,
                "layers": 3,
                "feat_dim": 256,
                "feat_hw": FEATURE_GRID,
                "res": RESOLUTION,
            },
            order,
        )
    record = bundle_record(
        out,
        surya_ocr=metadata.version("surya-ocr"),
        checkpoints={
            "text_detection": {"source": f"s3://{DETECTION}", "revision": None, "path": DETECTION},
            "layout": {
                "source": f"hf://datalab-to/{LAYOUT}",
                "revision": LAYOUT_REPOSITORY_REVISION,
                "path": LAYOUT,
            },
            "order": {
                "source": f"hf://datalab-to/{ORDER}",
                "revision": LAYOUT_REPOSITORY_REVISION,
                "path": ORDER,
            },
        },
    )
    (out / BUNDLE_FILE).write_bytes(bundle_bytes(record))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--out", type=Path, required=True, help="a directory that does not exist")
    parser.add_argument("--broken-order", action="store_true", help="an unloadable order head")
    args = parser.parse_args(argv)
    if args.out.exists():
        parser.error(f"{args.out} exists")
    build(args.out, broken_order=args.broken_order)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
