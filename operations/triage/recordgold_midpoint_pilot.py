"""CPU-only two-step handoff for prescribed RecordGold spread splits.

Step one writes a declared-midpoint ScanTailor project.  The operator imports it
with ``verbatus scantailor``.  Step two consumes that immutable import and writes
standard triage rows plus their binding sidecar.  No source image is changed.
"""

from __future__ import annotations

import argparse
import os
from pathlib import Path

from common.contracts.canonical import canonical_bytes, digest_bytes
from common.imaging import render_triage_derivative
from operations.triage import producer
from operations.triage.scantailor_bridge import transcribe_imported_geometry
from operations.triage.scantailor_project import PrescribedSpread, prescribed_midpoint_project


def _new(path: Path, data: bytes) -> None:
    """Publish one new artifact; a retry must inspect rather than overwrite it."""
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "wb") as handle:
        handle.write(data)
        handle.flush()
        os.fsync(handle.fileno())
    directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(directory)
    finally:
        os.close(directory)


def _page(value: str) -> tuple[str, int]:
    try:
        identifier, rotation = value.split(":", 1)
        degrees = int(rotation)
    except ValueError as error:
        raise argparse.ArgumentTypeError("page must be PAGE_ID:0 or PAGE_ID:180") from error
    if not identifier or degrees not in {0, 180}:
        raise argparse.ArgumentTypeError("page must be PAGE_ID:0 or PAGE_ID:180")
    return identifier, degrees


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-root", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument(
        "--project-dir",
        type=Path,
        help="existing directory beside --source-root for the project XML; never the submission itself",
    )
    parser.add_argument("--corpus-id", required=True)
    parser.add_argument("--page", type=_page, action="append", required=True)
    parser.add_argument("--geometry-document", type=Path)
    parser.add_argument(
        "--prepared-source-dir",
        type=Path,
        help="existing empty sibling folder for deterministic split PNGs",
    )
    args = parser.parse_args(argv)
    if not args.output_dir.is_dir():
        parser.error("--output-dir must already exist")
    if len({identifier for identifier, _rotation in args.page}) != len(args.page):
        parser.error("each --page id may appear once")
    frames: dict[str, producer.SubmittedFrame] = {}
    prescribed: list[PrescribedSpread] = []
    orientations: dict[str, int] = {}
    for identifier, degrees in args.page:
        relative = f"pages/{identifier}.jpg"
        source = args.source_root / relative
        # Resolved and contained *before* the read, not after. A page
        # identifier carrying separators or `..` otherwise makes this tool read
        # an unrelated local file, and the containment check that exists
        # further down only rejects the escaped path once its bytes are already
        # in hand (CodeRabbit).
        root = args.source_root.resolve()
        if not source.resolve().is_relative_to(root):
            parser.error(f"page identifier {identifier!r} leaves --source-root")
        try:
            data = source.read_bytes()
            width, height, _mode = producer._decode_dimensions_and_mode(data, relative)
        except (OSError, producer.ProducerRefusal) as error:
            parser.error(f"could not read the submitted master {source}: {error}")
        source_path = str(source.resolve())
        frames[source_path] = producer.SubmittedFrame(relative, data)
        prescribed.append(PrescribedSpread(relative, width, height))
        orientations[source_path] = degrees
    if args.geometry_document is None:
        if args.project_dir is None or not args.project_dir.is_dir():
            parser.error(
                "--project-dir must name an existing directory that is a strict ancestor of --source-root"
            )
        if args.project_dir.resolve() == args.source_root.resolve():
            # Equal paths satisfy `relative_to` with `Path(".")`, and the
            # generator below would then write the project *into* the submitted
            # source tree, adding an unsubmitted file to what the Door is about
            # to read (CodeRabbit).
            parser.error(
                "--project-dir must be a strict ancestor of --source-root, not equal to it"
            )
        try:
            relative_prefix = args.source_root.resolve().relative_to(args.project_dir.resolve())
        except ValueError:
            parser.error("--project-dir must be an ancestor of --source-root")
        project = args.project_dir / "recordgold-prescribed-midpoints.ScanTailor"
        project_sources = [
            PrescribedSpread(str(relative_prefix / source.source_path), source.width, source.height)
            for source in prescribed
        ]
        _new(project, prescribed_midpoint_project(project_sources))
        print(project)
        return 0
    translated = transcribe_imported_geometry(
        args.geometry_document,
        submitted_by_source_path=frames,
        corpus_id=args.corpus_id,
        mode="manual",
        orientation_degrees_by_source_path=orientations,
    )
    admitted = producer.produce(
        list(frames.values()),
        corpus_id=args.corpus_id,
        mode="manual",
        transcribed_rows_by_path=translated.rows_by_submitted_path,
    )
    materialized: list[dict[str, object]] = []
    if args.prepared_source_dir is not None:
        if not args.prepared_source_dir.is_dir() or any(args.prepared_source_dir.iterdir()):
            parser.error("--prepared-source-dir must be an existing empty directory")
        for source_path, frame in frames.items():
            row = translated.rows_by_submitted_path[frame.path]
            stem = Path(frame.path).stem
            for part_index, part in enumerate(row["split"]["parts"]):
                try:
                    pixels, geometry = render_triage_derivative(frame.data, page_index=0, part=part)
                except ValueError as error:
                    parser.error(f"could not materialize {frame.path} part {part_index}: {error}")
                output_name = f"{stem}-{'left' if part_index == 0 else 'right'}.png"
                _new(args.prepared_source_dir / output_name, pixels)
                materialized.append(
                    {
                        "prepared_relative_path": output_name,
                        "prepared_sha256": digest_bytes(pixels),
                        "prepared_width": geometry["width"],
                        "prepared_height": geometry["height"],
                        "source_path": source_path,
                        "source_frame_sha256": row["source_frame_sha256"],
                        "manifest_row_sha256": row["manifest_row_sha256"],
                        "triage_part_index": part_index,
                    }
                )
        translated.binding["materialized_pages"] = materialized
    _new(args.output_dir / "scantailor-triage-binding.json", canonical_bytes(translated.binding))
    _new(args.output_dir / "triage-decision-manifest.json", canonical_bytes(admitted.manifest))
    print(args.output_dir / "triage-decision-manifest.json")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
