"""Write a declared centre-cut ScanTailor Advanced v4 project.

This creates project XML from an operator-prescribed midpoint.  It does not run
ScanTailor and callers must not describe the resulting geometry as GUI- or
engine-measured.  The project is intended for the existing confined importer,
which validates and publishes its geometry before the triage bridge sees it.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import PurePosixPath
from xml.etree.ElementTree import Element, SubElement, tostring


@dataclass(frozen=True)
class PrescribedSpread:
    """One immutable master, named relative to the project file's directory."""

    source_path: str
    width: int
    height: int


def prescribed_midpoint_project(sources: list[PrescribedSpread]) -> bytes:
    """Return a deterministic v4 project with one exact vertical midpoint per source."""
    if not sources:
        raise ValueError("a prescribed ScanTailor project needs at least one source")
    if len({source.source_path for source in sources}) != len(sources):
        raise ValueError("prescribed ScanTailor project repeats a source path")
    root = Element("project", {"version": "4", "outputDirectory": "out", "layoutDirection": "LTR"})
    directories = SubElement(root, "directories")
    files = SubElement(root, "files")
    images = SubElement(root, "images")
    pages = SubElement(root, "pages")
    SubElement(root, "file-name-disambiguation")
    filters = SubElement(root, "filters")
    split = SubElement(filters, "page-split", {"defaultLayoutType": "two-pages"})
    for ordinal, source in enumerate(sources, start=1):
        relative = PurePosixPath(source.source_path)
        if relative.is_absolute() or ".." in relative.parts or relative.name in {"", "."}:
            raise ValueError("prescribed ScanTailor source paths must be canonical relative paths")
        if not all(
            isinstance(value, int) and not isinstance(value, bool) and value > 1
            for value in (source.width, source.height)
        ):
            raise ValueError("prescribed ScanTailor source dimensions must be positive integers")
        midpoint = source.width // 2
        if midpoint == 0 or midpoint == source.width:
            raise ValueError("prescribed ScanTailor midpoint leaves an empty half")
        directory_id, file_id, image_id = str(ordinal), str(ordinal), str(ordinal)
        SubElement(
            directories, "directory", {"id": directory_id, "path": relative.parent.as_posix()}
        )
        SubElement(files, "file", {"id": file_id, "dirId": directory_id, "name": relative.name})
        image = SubElement(
            images, "image", {"id": image_id, "subPages": "2", "fileId": file_id, "fileImage": "0"}
        )
        SubElement(image, "size", {"width": str(source.width), "height": str(source.height)})
        SubElement(image, "dpi", {"horizontal": "300", "vertical": "300"})
        SubElement(pages, "page", {"id": f"{ordinal}L", "imageId": image_id, "subPage": "left"})
        SubElement(pages, "page", {"id": f"{ordinal}R", "imageId": image_id, "subPage": "right"})
        entry = SubElement(split, "image", {"id": image_id, "layoutType": "two-pages"})
        params = SubElement(entry, "params", {"mode": "manual"})
        layout = SubElement(params, "pages", {"type": "two-pages"})
        outline = SubElement(layout, "outline")
        for x, y in (
            (0, 0),
            (source.width, 0),
            (source.width, source.height),
            (0, source.height),
            (0, 0),
        ):
            SubElement(outline, "point", {"x": str(x), "y": str(y)})
        cutter = SubElement(layout, "cutter1")
        SubElement(cutter, "p1", {"x": str(midpoint), "y": "0"})
        SubElement(cutter, "p2", {"x": str(midpoint), "y": str(source.height)})
        dependencies = SubElement(params, "dependencies")
        SubElement(dependencies, "rotation", {"degrees": "0"})
        SubElement(dependencies, "size", {"width": str(source.width), "height": str(source.height)})
        layout_type = SubElement(dependencies, "layoutType")
        layout_type.text = "two-pages"
    return tostring(root, encoding="utf-8") + b"\n"
