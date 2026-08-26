"""Closed ScanTailor Advanced v4 project parser and its sole document writer.

The shape below is read from ScanTailor Advanced's ProjectWriter (GPL-3.0-only,
source inspected 2026-08-25): ``project`` v4, its image table, and the
``filters/page-split`` outline/cutter records.  We carry no upstream code.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import stat
import sys
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any, Final

from common.contracts.canonical import canonical_bytes, digest_bytes, is_sha256
from operations.pod.durable import exclusive_write

_REQUEST = {"operation", "project", "output_dir", "expected_project_sha256"}

# A real ScanTailor Advanced project XML is a few pages of attributes per image;
# even an archive of thousands of pages stays well under this. Generous on
# purpose, and bounded on purpose: an unbounded read lets a hostile or merely
# corrupt file exhaust memory before this module ever gets to refuse its shape.
_MAX_PROJECT_BYTES: Final = 64 * 1024 * 1024

# ElementTree accepts UTF-16 and UTF-32 XML as well as UTF-8. Looking for only
# the ASCII byte spelling leaves a doctype (and its entities) invisible in those
# encodings even though the parser still processes it. ScanTailor writes no
# doctype in any encoding, so refuse every XML byte spelling we accept.
_DOCTYPE_MARKERS: Final = tuple(
    "<!DOCTYPE".encode(encoding)
    for encoding in ("utf-8", "utf-16-le", "utf-16-be", "utf-32-le", "utf-32-be")
)

# ScanTailor writes coordinates as Qt-serialized doubles. This is the decimal
# spelling of one, and nothing else: the value is kept as the exact string it
# was written as (see `_point`), so without a vocabulary the file decides what
# text lands in a geometry record. `x="not-a-number"` and `x="1e999"` both
# reached the published document before this pattern existed.
_DECIMAL: Final = re.compile(r"[-+]?(?:[0-9]+\.?[0-9]*|\.[0-9]+)(?:[eE][-+]?[0-9]+)?\Z")


def _refuse(message: str) -> ValueError:
    return ValueError(f"ScanTailor project refusal: {message}")


def _closed(element: ET.Element, tag: str, attributes: set[str], children: list[str]) -> None:
    if (
        element.tag != tag
        or set(element.attrib) != attributes
        or [node.tag for node in element] != children
    ):
        raise _refuse(f"{tag} has an unsupported or truncated shape")


def _integer(value: str, what: str) -> int:
    try:
        result = int(value)
    except ValueError as error:
        raise _refuse(f"{what} is not an integer") from error
    if result < 0:
        raise _refuse(f"{what} is negative")
    return result


def _point(element: ET.Element, what: str, *, tag: str = "point") -> dict[str, str]:
    _closed(element, tag, {"x", "y"}, [])
    # ScanTailor stores decimal coordinates. Preserve their exact spelling; turning
    # them into floats would make a claimed digest machine-dependent. Checking the
    # spelling is not converting it: the string that lands in the record is the
    # string that was in the file, and it is a number.
    return {axis: _coordinate(element.attrib[axis], f"{what} {axis}") for axis in ("x", "y")}


def _coordinate(value: str, what: str) -> str:
    if not _DECIMAL.match(value) or not math.isfinite(float(value)):
        raise _refuse(f"{what} coordinate is not a finite decimal number")
    return value


def parse(project_bytes: bytes, project_path: Path) -> dict[str, Any]:
    if any(marker in project_bytes for marker in _DOCTYPE_MARKERS):
        # Named explicitly rather than left to the parser's own defences: a real
        # ScanTailor Advanced project never declares a doctype or an entity, so
        # refusing the byte pattern outright is a refusal that holds regardless
        # of which XML library version is installed, instead of depending on
        # expat's amplification-limit heuristics to catch an entity bomb.
        raise _refuse("file declares a DOCTYPE or entity, which a real project never needs")
    try:
        root = ET.fromstring(project_bytes)
    except ET.ParseError as error:
        raise _refuse("file is malformed or truncated XML") from error
    _closed(
        root,
        "project",
        {"version", "outputDirectory", "layoutDirection"},
        ["directories", "files", "images", "pages", "file-name-disambiguation", "filters"],
    )
    if root.attrib["version"] != "4":
        raise _refuse("version is not the supported ScanTailor Advanced project version 4")
    directories, files, images, _pages, _disambiguation, filters = root
    directory_paths: dict[str, str] = {}
    for directory in directories:
        _closed(directory, "directory", {"id", "path"}, [])
        if directory.attrib["id"] in directory_paths:
            raise _refuse("directories repeat an id")
        directory_paths[directory.attrib["id"]] = directory.attrib["path"]
    file_paths: dict[str, str] = {}
    for file in files:
        _closed(file, "file", {"id", "dirId", "name"}, [])
        if file.attrib["id"] in file_paths or file.attrib["dirId"] not in directory_paths:
            raise _refuse("files repeat an id or name an unknown directory")
        file_paths[file.attrib["id"]] = str(
            Path(directory_paths[file.attrib["dirId"]]) / file.attrib["name"]
        )
    image_paths: dict[str, dict[str, Any]] = {}
    for image in images:
        if (
            set(image.attrib)
            not in (
                {"id", "subPages", "fileId", "fileImage"},
                {"id", "subPages", "fileId", "fileImage", "removed"},
            )
            or image.tag != "image"
            or [node.tag for node in image] != ["size", "dpi"]
        ):
            raise _refuse("image has an unsupported or truncated shape")
        _closed(image[0], "size", {"width", "height"}, [])
        _closed(image[1], "dpi", {"horizontal", "vertical"}, [])
        identifier = image.attrib["id"]
        if identifier in image_paths or image.attrib["fileId"] not in file_paths:
            raise _refuse("images repeat an id or name an unknown file")
        image_paths[identifier] = {
            "source_path": str(
                (project_path.parent / file_paths[image.attrib["fileId"]]).resolve()
            ),
            "file_image": _integer(image.attrib["fileImage"], "file image index"),
            "width": _integer(image[0].attrib["width"], "image width"),
            "height": _integer(image[0].attrib["height"], "image height"),
            "removed_half": _removed_half(image.attrib.get("removed")),
        }
    if filters.tag != "filters" or filters.attrib:
        raise _refuse("filters have an unsupported shape")
    split_candidates = [item for item in filters if item.tag == "page-split"]
    if len(split_candidates) != 1:
        raise _refuse("project does not contain exactly one page-split record")
    split = split_candidates[0]
    if split.tag != "page-split" or set(split.attrib) != {"defaultLayoutType"}:
        raise _refuse("page-split settings have an unsupported shape")
    geometry: list[dict[str, Any]] = []
    claimed_images: set[str] = set()
    claimed_pages: set[tuple[str, int]] = set()
    for entry in split:
        if entry.tag != "image" or set(entry.attrib) not in ({"id"}, {"id", "layoutType"}):
            raise _refuse("page-split image has an unsupported shape")
        if len(entry) != 1:
            raise _refuse("page-split image has no complete geometry")
        if entry.attrib["id"] not in image_paths:
            raise _refuse("page-split geometry names an unknown image")
        if entry.attrib["id"] in claimed_images:
            # Rule 8: nothing selects among geometries. Two page-split records
            # for the same image are two competing claims on one page, and nothing
            # downstream is a picker that may choose between them.
            raise _refuse("page-split offers more than one geometry for the same image")
        claimed_images.add(entry.attrib["id"])
        image = image_paths[entry.attrib["id"]]
        # The same rule, reached through the other door. An image *id* is the
        # project's own label; the page is the file and the frame within it. Two
        # distinct ids may name one physical page -- two `<image>` rows on one
        # `fileId`/`fileImage`, or two `<file>` rows resolving to one path -- and
        # each may carry its own outline. Keying only on the id let that pair
        # through as two records, which is the picker the id check refuses.
        page = (image["source_path"], image["file_image"])
        if page in claimed_pages:
            raise _refuse("page-split offers more than one geometry for the same physical page")
        claimed_pages.add(page)
        params = entry[0]
        _closed(params, "params", {"mode"}, ["pages", "dependencies"])
        pages = params[0]
        kind = pages.attrib.get("type")
        cutters = (
            ["cutter1"]
            if kind == "two-pages"
            else (["cutter1", "cutter2"] if kind == "single-cut" else [])
        )
        _closed(pages, "pages", {"type"}, ["outline", *cutters])
        if kind not in {"single-uncut", "single-cut", "two-pages"}:
            raise _refuse("page-split geometry names an unknown layout type")
        outline = pages[0]
        if outline.tag != "outline" or len(outline) != 5 or set(outline.attrib):
            raise _refuse("page-split outline is not exactly four closed sides")
        outline_points = [_point(point, "outline") for point in outline]
        if outline_points[-1] != outline_points[0]:
            raise _refuse("page-split outline does not close at its first point")
        record = {
            "image": image_paths[entry.attrib["id"]],
            "layout_type": kind,
            "outline": outline_points,
            "cutters": [],
        }
        for cutter in pages[1:]:
            _closed(cutter, cutter.tag, set(), ["p1", "p2"])
            record["cutters"].append(
                {
                    "name": cutter.tag,
                    "p1": _point(cutter[0], "cutter", tag="p1"),
                    "p2": _point(cutter[1], "cutter", tag="p2"),
                }
            )
        geometry.append(record)
    if not geometry:
        raise _refuse("project has no saved page-split geometry")
    return {
        "schema": "scantailor-geometry.v1",
        "project_sha256": digest_bytes(project_bytes),
        "project_version": 4,
        # A fact about the project this document describes, and not the same
        # number as `len(geometry)`: a project may hold forty images and have
        # saved geometry for three. The summary reported the geometry count under
        # both names, so a preview claimed the project held as many images as it
        # had splits. The label stays v1: nothing reads this schema yet, and
        # `common/contracts/canonical.py` records that these first labels are
        # deliberately disposable before alpha.
        "source_image_count": len(image_paths),
        "geometry": geometry,
    }


def _removed_half(value: str | None) -> str | None:
    """Preserve ScanTailor's page removal as geometry, without applying it."""

    if value is None:
        return None
    try:
        return {"L": "left", "R": "right"}[value]
    except KeyError as error:
        raise _refuse("image removed-half marker is neither L nor R") from error


def _bounded_bytes(path: Path, what: str, *, follow: bool = True) -> bytes:
    """Read a file this module trusts nothing about, or refuse it by name.

    Every read here is one of these: the project file the operator names, and the
    existing document a repeated commit compares itself against, which sits in an
    operator-named folder and is no more trustworthy than the project.

    The open carries `records._bounded_bytes`'s protections, and carries them for
    the reason that module already gives. `O_NONBLOCK` is the load-bearing one: a
    FIFO left at either path takes the *open* itself, not the read, so without it
    the confined child hangs having printed nothing and the console waits on a
    subprocess that will never speak. Refusing on the open descriptor's own
    `st_mode`, rather than on the name, is what survives a swap between the two.

    `O_NOFOLLOW` is applied only where a symlink would mean something. The
    existing-document read decides whether a repeated commit is the same evidence
    or a collision, so it must read the file the write allowance covers and not
    one a link points at. The project file is a path a person typed, with no
    earlier check for a link to race, and refusing a symlinked project would cost
    them a legitimate spelling to protect nothing.
    """
    flags = os.O_RDONLY | os.O_NONBLOCK | (0 if follow else os.O_NOFOLLOW)
    try:
        descriptor = os.open(path, flags)
    except OSError as error:
        raise _refuse(f"{what} could not be opened: {error}") from error
    with os.fdopen(descriptor, "rb") as handle:
        if not stat.S_ISREG(os.fstat(handle.fileno()).st_mode):
            raise _refuse(f"{what} is not a regular file")
        data = handle.read(_MAX_PROJECT_BYTES + 1)
    if len(data) > _MAX_PROJECT_BYTES:
        raise _refuse(f"{what} is larger than {_MAX_PROJECT_BYTES} bytes and was not read")
    return data


def _persisted(document: dict[str, Any]) -> bytes:
    """The exact bytes a commit would publish, checked as a serialized fixed point.

    The 21B write boundary this seam must land through: serialize once, reparse,
    and require the bytes to be a fixed point before anything is written. A dict
    built entirely from this module's own parsing has no tuple channel today;
    the byte-form check also refuses dict subclasses that emit duplicate keys.
    """
    encoded = canonical_bytes(document) + b"\n"
    try:
        reparsed = json.loads(encoded.decode("utf-8"))
        fixed = canonical_bytes(reparsed) + b"\n"
    except (TypeError, ValueError, UnicodeDecodeError) as error:
        raise _refuse("geometry document has no canonical JSON fixed point") from error
    if not isinstance(reparsed, dict) or fixed != encoded:
        raise _refuse("geometry document's serialized form is not a canonical JSON fixed point")
    return encoded


def main() -> int:
    try:
        request = json.loads(sys.stdin.read())
        if (
            not isinstance(request, dict)
            or set(request) != _REQUEST
            or request["operation"] not in {"preview", "commit"}
        ):
            raise _refuse("request has an invalid shape")
        if not isinstance(request["project"], str) or not isinstance(request["output_dir"], str):
            raise _refuse("request paths are invalid")
        if request["expected_project_sha256"] is not None and not is_sha256(
            request["expected_project_sha256"]
        ):
            raise _refuse("expected project digest is invalid")
        project = Path(request["project"])
        output_dir = Path(request["output_dir"])
        # Checked on both launches, and checked here rather than in the caller.
        # The preview runs with no write allowance at all, so a folder that
        # cannot receive the document has to be named *before* the operator is
        # shown a digest and told the commit is pinned to it -- otherwise the
        # only report of a mistyped folder is the kernel refusing to build a
        # Landlock rule over a path that is not there, which reads as a custody
        # fault rather than as "that folder does not exist". Repeating it on the
        # commit launch is what catches a folder swapped in between the two.
        if output_dir.is_symlink() or not output_dir.is_dir():
            raise _refuse(
                "output folder does not exist, is not a directory, or is a symbolic link; "
                "the console writes only into a real folder that already exists"
            )
        document = parse(_bounded_bytes(project, "project file"), project)
        if (
            request["operation"] == "commit"
            and request["expected_project_sha256"] != document["project_sha256"]
        ):
            raise _refuse("project changed after the preview; nothing was written")
        encoded = _persisted(document)
        document_digest = hashlib.sha256(encoded).hexdigest()
        path = output_dir / f"scantailor-geometry-{document_digest}.json"
        if request["operation"] == "commit":
            try:
                # The one atomic writer: fsync's the bytes and, strictly, the
                # directory entry that names them, so a "committed" reply is
                # never printed for a write a power cut could still lose.
                exclusive_write(path, encoded, strict=True, create_parent=False)
            except FileExistsError as error:
                if _bounded_bytes(path, "existing geometry document", follow=False) != encoded:
                    raise _refuse(
                        "an existing geometry document does not match its digest"
                    ) from error
        summary = {
            "project_sha256": document["project_sha256"],
            "image_count": document["source_image_count"],
            "geometry_count": len(document["geometry"]),
            "document_path": str(path),
            "document_sha256": document_digest,
        }
        print(
            json.dumps(
                {
                    "status": "committed" if request["operation"] == "commit" else "preview",
                    "summary": summary,
                }
            )
        )
        return 0
    except (OSError, ValueError, TypeError, UnicodeError) as error:
        print(json.dumps({"status": "refusal", "reason": str(error)}))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
