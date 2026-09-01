"""Closed ScanTailor Advanced v4 project parser and its sole document writer.

The shape below is read from ScanTailor Advanced's ProjectWriter (GPL-3.0-only,
source inspected 2026-08-25): ``project`` v4, its image table, and the
``filters/page-split`` outline/cutter records.  We carry no upstream code.
"""

from __future__ import annotations

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

_REQUEST = {
    "operation",
    "project",
    "output_dir",
    "expected_project_sha256",
    "expected_output_device",
    "expected_output_inode",
}

# Real projects remain well below this bound even at thousands of pages; an
# unbounded read would let corrupt input exhaust memory before shape validation.
_MAX_PROJECT_BYTES: Final = 64 * 1024 * 1024

# ElementTree accepts UTF-16 and UTF-32 XML as well as UTF-8. Looking for only
# the ASCII byte spelling leaves a doctype (and its entities) invisible in those
# encodings even though the parser still processes it. ScanTailor writes no
# doctype in any encoding, so refuse every XML byte spelling we accept.
_DOCTYPE_MARKERS: Final = tuple(
    "<!DOCTYPE".encode(encoding)
    for encoding in ("utf-8", "utf-16-le", "utf-16-be", "utf-32-le", "utf-32-be")
)

# Coordinate spelling is preserved for stable digests, so the accepted text must
# be limited to finite Qt-serialized decimal doubles before publication.
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
    return {axis: _coordinate(element.attrib[axis], f"{what} {axis}") for axis in ("x", "y")}


def _coordinate(value: str, what: str) -> str:
    if not _DECIMAL.match(value) or not math.isfinite(float(value)):
        raise _refuse(f"{what} coordinate is not a finite decimal number")
    return value


def parse(project_bytes: bytes, project_path: Path) -> dict[str, Any]:
    if any(marker in project_bytes for marker in _DOCTYPE_MARKERS):
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
    if set(split.attrib) != {"defaultLayoutType"}:
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
            # Governance forbids selecting among competing geometries for one image.
            raise _refuse("page-split offers more than one geometry for the same image")
        claimed_images.add(entry.attrib["id"])
        image = image_paths[entry.attrib["id"]]
        # Distinct project ids can name the same file frame; accepting both would
        # leave downstream code to choose between competing physical-page claims.
        page = _physical_page_key(image["source_path"], image["file_image"])
        if page in claimed_pages:
            raise _refuse("page-split offers more than one geometry for the same physical page")
        claimed_pages.add(page)
        params = entry[0]
        _closed(params, "params", {"mode"}, ["pages", "dependencies"])
        pages = params[0]
        kind = pages.attrib.get("type")
        # Layout type before shape: an unknown kind produces an empty cutter
        # list, and the shape refusal would then tell the operator their saved
        # project is truncated when the real fact is a layout this parser does
        # not support.
        if kind not in {"single-uncut", "single-cut", "two-pages"}:
            raise _refuse("page-split geometry names an unknown layout type")
        cutters = (
            ["cutter1"]
            if kind == "two-pages"
            else (["cutter1", "cutter2"] if kind == "single-cut" else [])
        )
        _closed(pages, "pages", {"type"}, ["outline", *cutters])
        outline = pages[0]
        if outline.tag != "outline" or len(outline) != 5 or set(outline.attrib):
            raise _refuse("page-split outline is not exactly four closed sides")
        outline_points = [_point(point, "outline") for point in outline]
        if outline_points[-1] != outline_points[0]:
            raise _refuse("page-split outline does not close at its first point")
        record = {
            "image": image,
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
        # Source coverage and saved-geometry coverage remain distinct when only
        # some project images have a saved split.
        "source_image_count": len(image_paths),
        "geometry": geometry,
    }


def _physical_page_key(source_path: str, file_image: int) -> tuple[str, int]:
    """Identify one physical page for the duplicate-geometry refusal.

    macOS's default filesystem, APFS, is case-insensitive but case-preserving:
    ``masters/spread.tif`` and ``masters/SPREAD.tif`` name one file on disk even
    though they are two different strings. Comparing `source_path` by spelling
    alone would let two `<file>` rows that differ only in case each carry a
    saved geometry, which the parser would treat as two physical pages instead
    of the one the refusal above exists to catch -- a picker rebuilt by
    accident. Folded only for this identity check; the record's own
    ``source_path`` is the resolved target with symlinks followed, not the
    project file's own ``<directory>``/``<file>`` spelling.
    """
    key = source_path.casefold() if sys.platform == "darwin" else source_path
    return (key, file_image)


def _removed_half(value: str | None) -> str | None:
    """Preserve ScanTailor's page removal as geometry, without applying it."""

    if value is None:
        return None
    try:
        return {"L": "left", "R": "right"}[value]
    except KeyError as error:
        raise _refuse("image removed-half marker is neither L nor R") from error


def _open_output_dir(output_dir: Path) -> int:
    """Open the operator-approved output folder once, and keep it as the folder.

    A name is not an object. Checking `output_dir` and then writing through the
    same spelling asks the filesystem to resolve it twice, and a local process
    that swaps the folder for a link in between gets the document delivered
    somewhere the operator never approved -- `run_confined` re-checks identity
    only after the child has exited, so it cannot take that write back. The
    descriptor opened here is the folder that passes the identity check and the
    folder the document is created in, with nothing in between that resolves a
    name again. Found by CodeRabbit.
    """

    try:
        return os.open(output_dir, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    except OSError as error:
        raise _refuse(
            "output folder does not exist, is not a directory, or is a symbolic link; "
            "the console writes only into a real folder that already exists"
        ) from error


def _publish(directory: int, name: str, payload: bytes) -> None:
    """Create ``name`` in the pinned folder, durably, or refuse because it exists.

    This is `operations/pod/durable.exclusive_write`'s contract, expressed
    against an open directory rather than a path: the bytes land in a temporary
    entry that is fsynced before it is published, the publication is a hard link
    so no reader ever sees a partial document, `O_EXCL` is the exclusion, and the
    directory entry is fsynced before the caller may say the document was
    written. What it does not do is re-open the parent by name.
    """

    temporary = f".scantailor-geometry.{os.getpid()}.{os.urandom(8).hex()}"
    descriptor = os.open(
        temporary,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
        0o600,
        dir_fd=directory,
    )
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        try:
            os.link(temporary, name, src_dir_fd=directory, dst_dir_fd=directory)
        except FileExistsError:
            # The name is published, but *this* call has proved nothing about
            # its directory entry, and the caller's idempotence comparison is
            # about to report the document as written. `exclusive_write` syncs
            # on exactly this path for exactly that reason.
            os.fsync(directory)
            raise
    finally:
        try:
            os.unlink(temporary, dir_fd=directory)
        except FileNotFoundError:
            pass
    os.fsync(directory)


def _bounded_bytes(
    path: str | Path, what: str, *, follow: bool = True, dir_fd: int | None = None
) -> bytes:
    """Bound untrusted reads and refuse non-regular files without blocking.

    `O_NONBLOCK` keeps a planted FIFO from hanging the confined child before it can
    report a refusal. The descriptor's mode closes the name-to-open race.
    Existing-document comparisons also use `O_NOFOLLOW` so an idempotence check
    cannot escape the write allowance through a symlink; operator-selected project
    paths may legitimately be symlinks and have no earlier link check to race.

    ``dir_fd`` reads ``path`` as one entry of an already open directory, so the
    existing-document comparison looks at the folder the identity check passed
    rather than at whatever the folder's name resolves to by then.
    """
    flags = os.O_RDONLY | os.O_NONBLOCK | (0 if follow else os.O_NOFOLLOW)
    try:
        descriptor = os.open(path, flags, dir_fd=dir_fd)
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
    """Require publication bytes to survive JSON parse and serialization unchanged.

    The byte-form check also refuses dict subclasses that emit duplicate keys,
    which comparing the in-memory mapping alone would miss.
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
        for name in ("expected_output_device", "expected_output_inode"):
            identity_part = request[name]
            if identity_part is not None and (
                not isinstance(identity_part, int)
                or isinstance(identity_part, bool)
                or identity_part < 0
            ):
                raise _refuse(f"{name} must be a non-negative integer or null")
        if request["operation"] == "commit" and (
            request["expected_output_device"] is None or request["expected_output_inode"] is None
        ):
            raise _refuse("a commit request must carry the previewed output folder's identity")
        project = Path(request["project"])
        output_dir = Path(request["output_dir"])
        # Preview must refuse an unusable folder before promising a pinned commit;
        # repeating the check on commit catches replacement between launches. The
        # open is the check: a missing folder, a plain file and a symbolic link
        # all fail it, and what survives is a descriptor rather than a name that
        # was true once.
        output_fd = _open_output_dir(output_dir)
        try:
            # The commit repeats only the path spelling unless this identity is also
            # pinned: if the approved folder is replaced by another directory between
            # the two launches, the path spelling alone cannot tell the two apart, and
            # a document would land in a folder the operator never saw approved. The
            # identity is read from the held descriptor, so it describes the folder
            # the document is written into and not a namesake of it.
            output_status = os.fstat(output_fd)
            output_identity = (output_status.st_dev, output_status.st_ino)
            if request["operation"] == "commit" and output_identity != (
                request["expected_output_device"],
                request["expected_output_inode"],
            ):
                raise _refuse(
                    "the output folder changed after the preview was shown; nothing was written"
                )
            document = parse(_bounded_bytes(project, "project file"), project)
            if (
                request["operation"] == "commit"
                and request["expected_project_sha256"] != document["project_sha256"]
            ):
                raise _refuse("project changed after the preview; nothing was written")
            encoded = _persisted(document)
            document_digest = digest_bytes(encoded)
            document_name = f"scantailor-geometry-{document_digest}.json"
            if request["operation"] == "commit":
                try:
                    # A committed reply requires a durable directory entry and must
                    # never recreate the operator-approved parent folder.
                    _publish(output_fd, document_name, encoded)
                except FileExistsError as error:
                    existing = _bounded_bytes(
                        document_name,
                        "existing geometry document",
                        follow=False,
                        dir_fd=output_fd,
                    )
                    if existing != encoded:
                        raise _refuse(
                            "an existing geometry document does not match its digest"
                        ) from error
        finally:
            os.close(output_fd)
        summary = {
            "project_sha256": document["project_sha256"],
            "image_count": document["source_image_count"],
            "geometry_count": len(document["geometry"]),
            "document_name": document_name,
            "document_sha256": document_digest,
        }
        print(
            json.dumps(
                {
                    "status": "committed" if request["operation"] == "commit" else "preview",
                    "summary": summary,
                    "output_identity": {
                        "device": output_identity[0],
                        "inode": output_identity[1],
                    },
                }
            )
        )
        return 0
    except (OSError, ValueError, TypeError, UnicodeError) as error:
        print(json.dumps({"status": "refusal", "reason": str(error)}))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
