"""Write the prepared pages, the manifest (`pagekit-prepare.v1`), the review sheet
(`review.html`) and the project file.

Each page is made from its original source through its geometry chain in one
resampling (pagekit.geometry.render), filled outside the paper with the page's own
paper colour, and written losslessly: TIFF with deflate compression by default, or PNG.
Greyscale stays greyscale and colour stays colour, and a page keeps the source's
resolution unless shrinking is set. With the plan's tone view, the grey tone view is also written beside each page as lossless TIFF. Every file, images, manifest and project, is
first written in full as a temporary file beside its target; only then are they all
moved into place, and if any move fails the earlier ones are put back. A failure at any
point leaves the output folder and the project file as they were. Outputs of pages that
no longer exist are never deleted; the manifest lists them under stale_outputs.
Source files that could not be used are listed under skipped, with their reasons.
"""

from __future__ import annotations

import hashlib
import io
import os
import tempfile
from pathlib import Path
from typing import Any

from PIL import Image

from pagekit import __version__
from pagekit._tiff import tiff_bytes
from pagekit.answer import STEPS
from pagekit.cache import links as cache_links
from pagekit.cache import write as write_cache
from pagekit.geometry import paper_colour, render
from pagekit.greypage import to_grey
from pagekit.prepare import PagePlan, Plan
from pagekit.project import PrepareError, canonical_json
from pagekit.review import REVIEW_NAME, page_preview, source_preview
from pagekit.review import build as build_review

MANIFEST_SCHEMA = "pagekit-prepare.v1"
MANIFEST_NAME = "pagekit-prepare.json"


def encode(image: Image.Image, output_format: str, dpi: tuple[float, float] | None) -> bytes:
    """The image as lossless file bytes, with its resolution when known and none when not.

    TIFF is written by pagekit._tiff, not Pillow's writer, which can leave an unset pad
    byte before the directory (so a re-run could differ) and claims 1 dpi when given
    none."""
    if output_format != "png":
        return tiff_bytes(image, dpi)
    options: dict[str, Any] = {}
    if dpi is not None:
        options["dpi"] = dpi
    buffer = io.BytesIO()
    image.save(buffer, "PNG", **options)
    return buffer.getvalue()


def _step_summary(entry: dict[str, Any]) -> dict[str, Any]:
    return {key: entry[key] for key in ("value", "origin", "confidence", "evidence", "flags")}


def _page_entry(page: PagePlan, image: Image.Image, data: bytes, fill, fill_method, fmt):
    geometry = page.chain.to_dict()
    geometry.update(page.chain.regions())
    geometry["fill"] = {"colour": list(fill) if isinstance(fill, tuple) else fill}
    geometry["fill"]["method"] = fill_method
    return {
        "source": {"name": page.source.path.name, "sha256": page.source.sha256},
        "page": page.number,
        "output": {
            "name": page.output_name,
            "sha256": hashlib.sha256(data).hexdigest(),
            "bytes": len(data),
            "pixels_sha256": hashlib.sha256(image.tobytes()).hexdigest(),
            "format": fmt,
            "mode": image.mode,
            "size": list(image.size),
            "resolution": None if page.output_dpi is None else list(page.output_dpi),
        },
        "source_resolution": page.resolution,
        "orientation_tag": page.tag,
        "output_mode": page.mode,
        "density": page.density,
        "upright_resolution": page.upright_resolution,
        "applied": page.applied,
        "geometry": geometry,
        "steps": {step: _step_summary(page.steps[step]) for step in STEPS},
        "flags": page.flags,
        "verdict": "review" if page.flags else "no_flags",
    }


def _stage(target: Path, data: bytes, staged: list[tuple[Path, Path]]) -> None:
    """Write `data` to a new temporary file beside `target`, readable by everyone."""
    handle, temporary = tempfile.mkstemp(prefix=f".{target.name}.", dir=target.parent)
    staged.append((Path(temporary), target))
    with os.fdopen(handle, "wb") as stream:
        stream.write(data)
        stream.flush()
        os.fsync(stream.fileno())
    os.chmod(temporary, 0o644)


def _commit(staged: list[tuple[Path, Path]]) -> None:
    """Move every staged file onto its target, or, if any move fails, put every target
    back as it was."""
    for _, target in staged:
        if target.exists() and not target.is_file():
            raise OSError(f"{target} is in the way and is not a file; nothing was written")
    done: list[tuple[Path, Path | None]] = []
    try:
        for temporary, target in staged:
            backup = None
            if target.exists():
                handle, name = tempfile.mkstemp(prefix=f".{target.name}.", dir=target.parent)
                os.close(handle)
                backup = Path(name)
                try:
                    os.replace(target, backup)
                except BaseException:
                    backup.unlink(missing_ok=True)
                    raise
            done.append((target, backup))
            os.replace(temporary, target)
    except BaseException:
        for target, backup in reversed(done):
            if backup is not None:
                os.replace(backup, target)
            else:
                target.unlink(missing_ok=True)
        raise
    for _, backup in done:
        if backup is not None:
            backup.unlink(missing_ok=True)


def execute(plan: Plan) -> dict[str, Any]:
    """Write every page of `plan`, its manifest and its project file; the manifest.

    Every file is made first as a temporary file beside its target. Only when all of
    them exist are they moved into place, so a failure leaves the folder as it was.
    """
    if plan.tone_view:
        _check_tone_names(plan)
    values = {name: entry["value"] for name, entry in plan.settings.items()}
    fmt = values["output_format"]
    created = [
        folder for folder in (plan.output_dir, plan.project_path.parent) if not folder.exists()
    ]
    staged: list[tuple[Path, Path]] = []
    try:
        for folder in created:
            folder.mkdir(parents=True, exist_ok=True)
        entries = []
        previews: dict[str, Any] = {"sources": {}, "pages": []}
        long_side = values["preview_long_side_px"]
        opened: tuple[str, Image.Image] | None = None
        for page in plan.pages:
            if opened is None or opened[0] != page.source.relative:
                opened = (page.source.relative, page.source.open())
                siblings = [other for other in plan.pages if other.source is page.source]
                previews["sources"][page.source.relative] = source_preview(
                    opened[1], siblings, long_side
                )
            source = opened[1]
            fill, method = paper_colour(source, page.chain, values["paper_estimate_long_side_px"])
            image = render(source, page.chain, fill)
            if page.mode.get("mode") == "grey":  # the same geometry, then a plain conversion
                image = to_grey(image, page.mode["exact"], page.mode["rule"])
            data = encode(image, fmt, page.output_dpi)
            _stage(plan.output_dir / page.output_name, data, staged)
            entry = _page_entry(page, image, data, fill, method, fmt)
            if plan.tone_view:
                entry["tone_view"] = _tone_view(plan, page, image, staged)
            entries.append(entry)
            previews["pages"].append(page_preview(image, long_side // 2))
        measured = all(entry["status"] == "MEASURED" for entry in plan.settings.values())
        manifest = {
            "schema": MANIFEST_SCHEMA,
            "tool": {"name": "pagekit", "version": __version__},
            "pages": entries,
            "skipped": plan.skipped,
            "stale_outputs": plan.stale_outputs,
            "batch": plan.batch,
            "review": REVIEW_NAME,
            "thresholds": plan.settings,
            "thresholds_measured": measured,
            "thresholds_note": (
                "Every setting is a starting guess not yet calibrated on real pages; a flag "
                "means look at this page, and no flag is not proof the page is right."
                if not measured
                else "Every setting has been measured on real pages."
            ),
        }
        _stage(plan.output_dir / MANIFEST_NAME, canonical_json(manifest).encode("utf-8"), staged)
        previews["cache"] = cache_links(plan.cache_dir, plan.output_dir, plan.pages)
        review = build_review(plan, entries, previews)
        _stage(plan.output_dir / REVIEW_NAME, review.encode("utf-8"), staged)
        _stage(plan.project_path, canonical_json(plan.project).encode("utf-8"), staged)
        _commit(staged)
    except BaseException:
        for temporary, _ in staged:
            temporary.unlink(missing_ok=True)
        for folder in reversed(created):
            if folder.exists() and not any(folder.iterdir()):
                folder.rmdir()
        raise
    if plan.cache_dir is not None:
        # The stage cache is for looking only; failing to write it changes no output.
        try:
            write_cache(plan.cache_dir, plan.pages, values, plan.output_dir)
        except Exception as error:
            manifest["cache_note"] = f"the stage cache could not be written: {error}"
    return manifest


def tone_view_name(page: PagePlan) -> str:
    return f"{Path(page.output_name).stem}_tone.tif"


def _check_tone_names(plan: Plan) -> None:
    """Refuse, before anything is written, a tone view that would land on a prepared
    page or a source."""
    pages = {page.output_name for page in plan.pages}
    sources = {page.source.path for page in plan.pages}
    for page in plan.pages:
        target = plan.output_dir / tone_view_name(page)
        if target.name in pages or target.resolve() in sources:
            raise PrepareError(
                f"the tone view of {page.output_name} would be written over "
                f"{target.name}, a prepared page or a source; nothing was written"
            )


def _tone_view(plan: Plan, page: PagePlan, image: Image.Image, staged) -> dict[str, Any]:
    """Stage the grey tone view of a prepared page beside it; its manifest entry."""
    from pagekit.pipeline import make_tone_view

    view, record, data = make_tone_view(image, page.output_dpi)
    name = tone_view_name(page)
    _stage(plan.output_dir / name, data, staged)
    return {
        "name": name,
        "sha256": hashlib.sha256(data).hexdigest(),
        "bytes": len(data),
        "pixels_sha256": hashlib.sha256(view.tobytes()).hexdigest(),
        "record": record,
    }


def manifest_path(output_dir: Path) -> Path:
    return output_dir / MANIFEST_NAME
