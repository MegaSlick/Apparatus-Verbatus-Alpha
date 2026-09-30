"""Fetch Surya's three checkpoints once, into one locked bundle directory.

Run in this directory's own environment, on a machine that may reach Datalab's
model host and the Hugging Face Hub (the pod, never a laptop holding register
material):

    uv run --project operations/serving/surya --frozen --no-sync \
        python operations/serving/surya/prefetch.py --out <volume>/surya/bundle

The detection checkpoint comes from Surya's own downloader, the layout and
reading-order checkpoints from the Hub at the pinned commit
(`contract.LAYOUT_REPOSITORY_REVISION`). The files land in `<out>.partial` and
move to `<out>` only once the lock is written, so a bundle that exists is
complete.
"""

from __future__ import annotations

import argparse
import shutil
import sys
from importlib import metadata
from pathlib import Path

from contract import (
    BUNDLE_FILE,
    LAYOUT_REPOSITORY_REVISION,
    bundle_bytes,
    bundle_record,
    read_bundle,
)

# Where each checkpoint lands inside the bundle.
DETECTION_FOLDER = "text_detection"
LAYOUT_FOLDER = "surya_layout2"


def _hub_reference(source: str) -> tuple[str, str]:
    """`hf://owner/repo/sub/folder` as the repository and the folder inside it."""
    parts = source.removeprefix("hf://").split("/")
    return "/".join(parts[:2]), "/".join(parts[2:])


def fetch(out: Path) -> dict:
    from huggingface_hub import snapshot_download
    from surya.common.s3 import download_directory
    from surya.settings import Settings

    defaults = {name: field.default for name, field in Settings.model_fields.items()}
    detection = defaults["DETECTOR_MODEL_CHECKPOINT"]
    layout_repo, layout_sub = _hub_reference(defaults["FAST_LAYOUT_MODEL_CHECKPOINT"])
    order_repo, order_sub = _hub_reference(defaults["FAST_ORDER_MODEL_CHECKPOINT"])
    if order_repo != layout_repo or layout_sub:
        raise SystemExit(
            "Surya's layout and reading-order checkpoints no longer share one repository "
            "root; this fetch lays out one Hub snapshot and must be revised first"
        )
    if out.exists():
        raise SystemExit(f"{out} already exists; a bundle is fetched once and never overwritten")
    staging = out.with_name(out.name + ".partial")
    if staging.exists():
        shutil.rmtree(staging)

    detection_path = Path(DETECTION_FOLDER) / detection.removeprefix("s3://").split("/", 1)[1]
    (staging / detection_path).mkdir(parents=True)
    # Surya's own downloader, as `S3DownloaderMixin` calls it.
    download_directory(detection.removeprefix("s3://"), str(staging / detection_path))

    revision = LAYOUT_REPOSITORY_REVISION
    snapshot_download(layout_repo, revision=revision, local_dir=staging / LAYOUT_FOLDER)
    # The Hub client's own download bookkeeping, not part of the checkpoint.
    shutil.rmtree(staging / LAYOUT_FOLDER / ".cache", ignore_errors=True)

    record = bundle_record(
        staging,
        surya_ocr=metadata.version("surya-ocr"),
        checkpoints={
            "text_detection": {
                "source": detection,
                "revision": None,
                "path": detection_path.as_posix(),
            },
            "layout": {
                "source": defaults["FAST_LAYOUT_MODEL_CHECKPOINT"],
                "revision": revision,
                "path": LAYOUT_FOLDER,
            },
            "order": {
                "source": defaults["FAST_ORDER_MODEL_CHECKPOINT"],
                "revision": revision,
                "path": f"{LAYOUT_FOLDER}/{order_sub}",
            },
        },
    )
    (staging / BUNDLE_FILE).write_bytes(bundle_bytes(record))
    read_bundle(staging)
    staging.rename(out)
    return record


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--out", type=Path, required=True, help="the bundle directory to create")
    args = parser.parse_args(argv)
    record = fetch(args.out.resolve())
    layout = record["checkpoints"]["layout"]
    print(
        f"wrote {len(record['files'])} files to {args.out}; layout at {layout['revision']}",
        file=sys.stderr,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
