"""Write the prepared pages, the manifest (`pagekit-prepare.v1`) and the project file.

Each page is made from its original source through its geometry chain in one
resampling (pagekit.geometry.render), filled outside the paper with the page's own
paper colour, and written losslessly: PNG, or TIFF with deflate compression. Greyscale
stays greyscale and colour stays colour. Every file is written atomically, images
first, then the manifest, then the project file, so a project file on disk always
describes outputs that were written in full.
"""

from __future__ import annotations

import hashlib
import io
from pathlib import Path
from typing import Any

from PIL import Image

from pagekit import __version__
from pagekit.answer import STEPS
from pagekit.geometry import paper_colour, render
from pagekit.prepare import PagePlan, Plan
from pagekit.project import canonical_json, write_atomic

MANIFEST_SCHEMA = "pagekit-prepare.v1"
MANIFEST_NAME = "pagekit-prepare.json"


def encode(image: Image.Image, output_format: str, dpi: tuple[float, float] | None) -> bytes:
    """The image as lossless file bytes, with its resolution when known."""
    options: dict[str, Any] = {}
    if dpi is not None:
        options["dpi"] = dpi
    buffer = io.BytesIO()
    if output_format == "png":
        image.save(buffer, "PNG", **options)
    else:
        image.save(buffer, "TIFF", compression="tiff_adobe_deflate", **options)
    return buffer.getvalue()


def _step_summary(entry: dict[str, Any]) -> dict[str, Any]:
    return {key: entry[key] for key in ("value", "origin", "confidence", "evidence", "flags")}


def _page_entry(page: PagePlan, image: Image.Image, data: bytes, fill, fill_method, fmt):
    geometry = page.chain.to_dict()
    geometry["fill"] = {"colour": list(fill) if isinstance(fill, tuple) else fill}
    geometry["fill"]["method"] = fill_method
    return {
        "source": {"name": page.source.path.name, "sha256": page.source.sha256},
        "page": page.number,
        "output": {
            "name": page.output_name,
            "sha256": hashlib.sha256(data).hexdigest(),
            "bytes": len(data),
            "format": fmt,
            "mode": image.mode,
            "size": list(image.size),
            "resolution": None if page.output_dpi is None else list(page.output_dpi),
        },
        "source_resolution": page.resolution,
        "geometry": geometry,
        "steps": {step: _step_summary(page.steps[step]) for step in STEPS},
        "flags": page.flags,
        "verdict": "review" if page.flags else "no_flags",
    }


def execute(plan: Plan) -> dict[str, Any]:
    """Write every page of `plan`, then its manifest and project file; the manifest."""
    values = {name: entry["value"] for name, entry in plan.settings.items()}
    fmt = values["output_format"]
    entries = []
    opened: tuple[str, Image.Image] | None = None
    for page in plan.pages:
        if opened is None or opened[0] != page.source.relative:
            opened = (page.source.relative, page.source.open())
        source = opened[1]
        fill, method = paper_colour(source, page.chain, values["paper_estimate_long_side_px"])
        image = render(source, page.chain, fill)
        data = encode(image, fmt, page.output_dpi)
        write_atomic(plan.output_dir / page.output_name, data)
        entries.append(_page_entry(page, image, data, fill, method, fmt))
    measured = all(entry["status"] == "MEASURED" for entry in plan.settings.values())
    manifest = {
        "schema": MANIFEST_SCHEMA,
        "tool": {"name": "pagekit", "version": __version__},
        "pages": entries,
        "thresholds": plan.settings,
        "thresholds_measured": measured,
        "thresholds_note": (
            "Every setting is a starting guess not yet calibrated on real pages; a flag "
            "means look at this page, and no flag is not proof the page is right."
            if not measured
            else "Every setting has been measured on real pages."
        ),
    }
    write_atomic(plan.output_dir / MANIFEST_NAME, canonical_json(manifest).encode("utf-8"))
    write_atomic(plan.project_path, canonical_json(plan.project).encode("utf-8"))
    return manifest


def manifest_path(output_dir: Path) -> Path:
    return output_dir / MANIFEST_NAME
