"""Fetch Surya's three checkpoints once, into one locked bundle directory.

Run in this directory's own environment, on a machine that may reach Datalab's
model host and the Hugging Face Hub (the pod, never a laptop holding register
material). The pod's model store runs it at launch
(`operations/serving/surya_detector.py::SuryaBundleFetcher`) and accepts the
bundle only if its measured manifest is the pinned one; by hand:

    operations/serving/surya/.venv/bin/python operations/serving/surya/prefetch.py \
        --out <bundle>

after `uv sync --locked --project operations/serving/surya` has built that
environment. `--check` in place of `--out` runs the imports and the settings
check the fetch starts with, and fetches nothing.

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
    found_settings_file,
    read_bundle,
)

# Where each checkpoint lands inside the bundle.
DETECTION_FOLDER = "text_detection"
LAYOUT_FOLDER = "surya_layout2"


def _hub_reference(source: str) -> tuple[str, str]:
    """`hf://owner/repo/sub/folder` as the repository and the folder inside it."""
    parts = source.removeprefix("hf://").split("/")
    return "/".join(parts[:2]), "/".join(parts[2:])


def _defaults() -> dict:
    """Surya's default settings, refused when a found `local.env` could override them.

    Imports everything the fetch uses, so a broken environment fails here.
    """
    import huggingface_hub  # noqa: F401
    import surya.common.s3  # noqa: F401
    from surya.settings import Settings

    # Surya's downloader reads its host from Surya's settings, which a found
    # `local.env` could set.
    found = found_settings_file(Settings)
    if found:
        raise SystemExit(f"Surya found a settings file at {found}; remove it, then fetch again")
    defaults = {name: field.default for name, field in Settings.model_fields.items()}
    layout_repo, layout_sub = _hub_reference(defaults["FAST_LAYOUT_MODEL_CHECKPOINT"])
    order_repo, _ = _hub_reference(defaults["FAST_ORDER_MODEL_CHECKPOINT"])
    if order_repo != layout_repo or layout_sub:
        raise SystemExit(
            "Surya's layout and reading-order checkpoints no longer share one repository "
            "root; this fetch lays out one Hub snapshot and must be revised first"
        )
    return defaults


def fetch(out: Path) -> dict:
    from huggingface_hub import snapshot_download
    from surya.common.s3 import download_directory

    defaults = _defaults()
    detection = defaults["DETECTOR_MODEL_CHECKPOINT"]
    layout_repo, _ = _hub_reference(defaults["FAST_LAYOUT_MODEL_CHECKPOINT"])
    _, order_sub = _hub_reference(defaults["FAST_ORDER_MODEL_CHECKPOINT"])
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
    action = parser.add_mutually_exclusive_group(required=True)
    action.add_argument("--out", type=Path, help="the bundle directory to create")
    action.add_argument(
        "--check",
        action="store_true",
        help="import what the fetch uses and check Surya's settings; fetch nothing",
    )
    args = parser.parse_args(argv)
    if args.check:
        _defaults()
        return 0
    record = fetch(args.out.resolve())
    layout = record["checkpoints"]["layout"]
    print(
        f"wrote {len(record['files'])} files to {args.out}; layout at {layout['revision']}",
        file=sys.stderr,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
