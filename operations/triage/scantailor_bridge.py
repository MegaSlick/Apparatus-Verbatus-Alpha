"""Translate a deliberately narrow ScanTailor split into Door decision rows.

ScanTailor's imported geometry remains the primary record: this bridge never
edits a master or asks ScanTailor to render output.  It accepts only the one
shape the triage contract can represent exactly: a full-frame rectangular
``two-pages`` layout with one integral vertical cutter.  Perspective, deskew,
partial outlines and slanted cutters are refusals, not rounded approximations.

The returned binding sidecar is retained with the ordinary decision rows.  It
ties their derived digests to the immutable imported geometry document, whose
decimal points retain the original ScanTailor coordinates.  The triage manifest
schema intentionally remains unchanged so the Door can admit the standard rows.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Final, Mapping

from common.contracts.canonical import canonical_bytes, digest_bytes, is_sha256
from common.imaging import ENCODER_LOSSLESS_MODES
from operations.triage import producer
from operations.triage.producer import SubmittedFrame, triage_manifest

BINDING_SCHEMA: Final = "scantailor-triage-binding.v1"
ORIENTATION_SCHEMA: Final = "scantailor-orientations.v1"
_DOCUMENT_FIELDS: Final = {
    "schema",
    "project_sha256",
    "project_version",
    "source_image_count",
    "geometry",
}


class ScantailorBridgeRefusal(ValueError):
    """The imported geometry cannot be represented without changing it."""


@dataclass(frozen=True)
class ScantailorTriageRows:
    """Rows for ``producer.produce(..., transcribed_rows_by_path=...)`` and their receipt."""

    rows_by_submitted_path: dict[str, dict[str, Any]]
    binding: dict[str, Any]


def load_imported_geometry(path: str | Path) -> tuple[dict[str, Any], str]:
    """Read the canonical bytes published by the ScanTailor importer once."""
    raw = Path(path).read_bytes()
    try:
        value = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as error:
        raise ScantailorBridgeRefusal("imported ScanTailor geometry is not JSON") from error
    if canonical_bytes(value) + b"\n" != raw:
        raise ScantailorBridgeRefusal("imported ScanTailor geometry is not canonical bytes")
    _document(value)
    return value, digest_bytes(raw)


def load_orientations(path: str | Path) -> tuple[dict[str, int], str]:
    """Read the canonical dataset-orientation declaration used by the bridge."""
    raw = Path(path).read_bytes()
    try:
        value = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as error:
        raise ScantailorBridgeRefusal("ScanTailor orientations are not JSON") from error
    if canonical_bytes(value) + b"\n" != raw:
        raise ScantailorBridgeRefusal("ScanTailor orientations are not canonical bytes")
    if (
        not isinstance(value, Mapping)
        or set(value) != {"schema", "orientations"}
        or value["schema"] != ORIENTATION_SCHEMA
        or not isinstance(value["orientations"], Mapping)
        or not value["orientations"]
        or any(
            not isinstance(path, str) or not path or degrees not in {0, 180}
            for path, degrees in value["orientations"].items()
        )
    ):
        raise ScantailorBridgeRefusal("ScanTailor orientations have the wrong closed schema")
    return dict(value["orientations"]), digest_bytes(raw)


def transcribe_midpoint_splits(
    document: Mapping[str, Any],
    *,
    geometry_document_sha256: str,
    submitted_by_source_path: Mapping[str, SubmittedFrame],
    corpus_id: str,
    mode: str,
    orientation_degrees_by_source_path: Mapping[str, int] | None = None,
) -> ScantailorTriageRows:
    """Make exact decision rows from a checked imported ScanTailor document.

    ``orientation_degrees_by_source_path`` is supplied from dataset metadata,
    never ground-truth text.  A 180-degree source emits its source-right half
    first, then source-left, so the rotated outputs stay in physical reading
    order.  The original regions remain in source-frame coordinates.
    """
    _document(document)
    if not is_sha256(geometry_document_sha256):
        raise ScantailorBridgeRefusal("geometry document digest is not a SHA-256")
    if (
        not isinstance(corpus_id, str)
        or not corpus_id.strip()
        or mode not in {"manual", "semi", "auto"}
    ):
        raise ScantailorBridgeRefusal("corpus id or triage mode is not declared")
    if not isinstance(submitted_by_source_path, Mapping) or not submitted_by_source_path:
        raise ScantailorBridgeRefusal("every imported geometry needs a submitted source frame")
    orientations = orientation_degrees_by_source_path or {}
    source_paths = [entry["image"]["source_path"] for entry in document["geometry"]]
    if set(source_paths) != set(submitted_by_source_path) or set(orientations) - set(source_paths):
        raise ScantailorBridgeRefusal(
            "submitted sources and imported geometry do not have exact coverage"
        )
    rows: dict[str, dict[str, Any]] = {}
    bindings: list[dict[str, Any]] = []
    for index, entry in enumerate(document["geometry"]):
        source_path = entry["image"]["source_path"]
        frame = submitted_by_source_path[source_path]
        if not isinstance(frame, SubmittedFrame):
            raise ScantailorBridgeRefusal(
                "submitted ScanTailor source is not immutable frame bytes"
            )
        if frame.path in rows:
            raise ScantailorBridgeRefusal("two ScanTailor sources map to one submitted path")
        width, height, image_mode = _dimensions(frame.data)
        image = entry["image"]
        if image["width"] != width or image["height"] != height:
            raise ScantailorBridgeRefusal(
                "imported ScanTailor dimensions disagree with submitted bytes"
            )
        cutter = _vertical_full_frame_cutter(entry, width, height)
        degrees = orientations.get(source_path, 0)
        if degrees not in {0, 180}:
            raise ScantailorBridgeRefusal("source orientation must be 0 or 180 degrees")
        regions = ((0, cutter), (cutter, width)) if degrees == 0 else ((cutter, width), (0, cutter))
        colour_mode = "keep" if image_mode in ENCODER_LOSSLESS_MODES else "rgb"
        parts = [
            triage_manifest.make_part(
                {"x": left, "y": 0, "w": right - left, "h": height},
                {"x": 0, "y": 0, "w": right - left, "h": height},
                degrees * 1000,
                colour_mode=colour_mode,
            )
            for left, right in regions
        ]
        row = triage_manifest.make_row(
            corpus_id=corpus_id,
            source_frame_sha256=digest_bytes(frame.data),
            frame={"width": width, "height": height},
            split=triage_manifest.make_split(parts),
            re_shoot_cluster_id=None,
            confidence=0,
            mode=mode,
            actor={"kind": "scantailor", "identity": "ScanTailor Advanced", "revision": "v4"},
            human_override=False,
        )
        rows[frame.path] = row
        bindings.append(
            {
                "geometry_index": index,
                "source_path": source_path,
                "submitted_path": frame.path,
                "source_frame_sha256": row["source_frame_sha256"],
                "manifest_row_sha256": row["manifest_row_sha256"],
                "orientation_degrees": degrees,
            }
        )
    return ScantailorTriageRows(
        rows_by_submitted_path=rows,
        binding={
            "schema": BINDING_SCHEMA,
            "geometry_document_sha256": geometry_document_sha256,
            "project_sha256": document["project_sha256"],
            "rows": bindings,
        },
    )


def transcribe_imported_geometry(
    path: str | Path,
    *,
    submitted_by_source_path: Mapping[str, SubmittedFrame],
    corpus_id: str,
    mode: str,
    orientation_degrees_by_source_path: Mapping[str, int] | None = None,
) -> ScantailorTriageRows:
    """Read a published geometry document and bind it before making any rows."""
    document, document_sha256 = load_imported_geometry(path)
    return transcribe_midpoint_splits(
        document,
        geometry_document_sha256=document_sha256,
        submitted_by_source_path=submitted_by_source_path,
        corpus_id=corpus_id,
        mode=mode,
        orientation_degrees_by_source_path=orientation_degrees_by_source_path,
    )


def _document(value: Any) -> None:
    if (
        not isinstance(value, Mapping)
        or set(value) != _DOCUMENT_FIELDS
        or value.get("schema") != "scantailor-geometry.v1"
    ):
        raise ScantailorBridgeRefusal("imported ScanTailor geometry has the wrong closed schema")
    if value.get("project_version") != 4 or not is_sha256(value.get("project_sha256")):
        raise ScantailorBridgeRefusal(
            "imported ScanTailor geometry has no supported project identity"
        )
    geometry = value.get("geometry")
    if (
        not isinstance(geometry, list)
        or not geometry
        or not isinstance(value.get("source_image_count"), int)
        or isinstance(value["source_image_count"], bool)
        or value["source_image_count"] < len(geometry)
    ):
        raise ScantailorBridgeRefusal("imported ScanTailor geometry has incomplete coverage")
    seen: set[str] = set()
    for entry in geometry:
        if not isinstance(entry, Mapping) or set(entry) != {
            "image",
            "layout_type",
            "outline",
            "cutters",
        }:
            raise ScantailorBridgeRefusal(
                "imported ScanTailor geometry entry has the wrong closed schema"
            )
        image = entry["image"]
        if not isinstance(image, Mapping) or set(image) != {
            "source_path",
            "file_image",
            "width",
            "height",
            "removed_half",
        }:
            raise ScantailorBridgeRefusal(
                "imported ScanTailor image identity has the wrong closed schema"
            )
        if (
            not isinstance(image["source_path"], str)
            or not image["source_path"]
            or image["source_path"] in seen
        ):
            raise ScantailorBridgeRefusal("imported ScanTailor geometry repeats a source path")
        seen.add(image["source_path"])


def _dimensions(data: bytes) -> tuple[int, int, str]:
    try:
        # This is the producer's one decode boundary, including its Pillow
        # decompression-bomb refusal.  Calling it here ensures a bridge refusal
        # cannot consume a master that the subsequent Door admission would reject.
        return producer._decode_dimensions_and_mode(data, "submitted ScanTailor source")
    except producer.ProducerRefusal as error:
        raise ScantailorBridgeRefusal(
            "submitted ScanTailor source bytes could not be decoded"
        ) from error


def _vertical_full_frame_cutter(entry: Mapping[str, Any], width: int, height: int) -> int:
    if (
        entry["layout_type"] != "two-pages"
        or not isinstance(entry["outline"], list)
        or not isinstance(entry["cutters"], list)
    ):
        raise ScantailorBridgeRefusal("only ScanTailor two-pages geometry can enter this bridge")
    if len(entry["outline"]) != 5:
        raise ScantailorBridgeRefusal("ScanTailor outline is not four closed sides")
    ordered_outline = [_point(point) for point in entry["outline"]]
    outline = set(ordered_outline)
    if ordered_outline[0] != ordered_outline[-1] or outline != {
        (0, 0),
        (width, 0),
        (width, height),
        (0, height),
    }:
        raise ScantailorBridgeRefusal("ScanTailor outline is not the exact submitted frame")
    if len(entry["cutters"]) != 1 or not isinstance(entry["cutters"][0], Mapping):
        raise ScantailorBridgeRefusal("ScanTailor two-pages geometry needs exactly one cutter")
    cutter = entry["cutters"][0]
    if set(cutter) != {"name", "p1", "p2"} or cutter["name"] != "cutter1":
        raise ScantailorBridgeRefusal("ScanTailor cutter is not the supported primary split")
    first, second = _point(cutter["p1"]), _point(cutter["p2"])
    if first[0] != second[0] or {first[1], second[1]} != {0, height} or not 0 < first[0] < width:
        raise ScantailorBridgeRefusal(
            "ScanTailor cutter is not a full-height internal vertical line"
        )
    return first[0]


def _point(value: Any) -> tuple[int, int]:
    if not isinstance(value, Mapping) or set(value) != {"x", "y"}:
        raise ScantailorBridgeRefusal("ScanTailor point has the wrong closed schema")
    return (_integer(value["x"]), _integer(value["y"]))


def _integer(value: Any) -> int:
    if not isinstance(value, str):
        raise ScantailorBridgeRefusal("ScanTailor coordinate is not its preserved decimal spelling")
    try:
        number = Decimal(value)
    except InvalidOperation as error:
        raise ScantailorBridgeRefusal("ScanTailor coordinate is not a decimal") from error
    if not number.is_finite() or number != number.to_integral_value():
        raise ScantailorBridgeRefusal(
            "ScanTailor geometry cannot be represented exactly by triage pixels"
        )
    return int(number)
