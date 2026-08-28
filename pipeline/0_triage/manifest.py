"""The pre-door triage decision-manifest contract.

This module records transform decisions only.  It never transforms a source frame and it
never chooses a member of a re-shoot cluster: complementary views remain rows.

Geometry and colour conversion are per split part, not per frame.  The unit's own structural case is a
document taped over the page at its own angle, "where no single gutter exists for
auto-split and no global deskew straightens both surfaces"
(`CONSOLIDATED_REBUILD_PLAN_2026-08-20.md:332-370`); one frame-level crop box and
one frame-level rotation cannot express it, and a bound spread's two pages each
want their own crop besides.

The closed split record carries its operation order, and every rectangle and
rotation carries its coordinate/convention fields.  ``region`` is a half-open
rectangle in source-frame pixel coordinates.  After cutting it, ``crop_box`` is
a half-open rectangle in that part's local pixel coordinates.  The cropped pixels
are then rotated clockwise about their centre onto an expanded canvas.  Pixel
sampling, fill and encoding belong to Unit 7's sealed apply recipe; they are not
geometry defaults hidden in this manifest.
"""

from __future__ import annotations

import xml.etree.ElementTree as ET
from collections.abc import Mapping
from typing import Any, Final

from common.contracts.canonical import canonical_bytes, digest_bytes, is_sha256
from common.contracts.errors import SchemaRefusal
from common.contracts.stages import MAX_TRIAGE_SPLIT_PARTS, TRIAGE_MODES

MANIFEST_SCHEMA: Final = "triage-decision-manifest-v1"
CLUSTER_SCHEMA: Final = "triage-re-shoot-cluster-v1"
CONFIDENCE_ORDINALS: Final = range(0, 5)
COLOUR_MODES: Final = ("keep", "grayscale", "rgb", "bitonal")
ACTOR_KINDS: Final = ("human", "model", "scantailor")
SCANTAILOR_IDENTITY: Final = "ScanTailor Advanced"
SPLIT_OPERATION_ORDER: Final = "region-crop-rotate"
MAX_MANIFEST_ROWS: Final = 1_000
MAX_CLUSTER_RECORDS: Final = 1_000
MAX_CLUSTER_MEMBERS: Final = 4_096
# The shared cap, not a second declaration of it: the Exemplar boundary restates
# this module's row schema and bounds the same split before its own pairwise check.
MAX_SPLIT_PARTS: Final = MAX_TRIAGE_SPLIT_PARTS
MAX_SCANTAILOR_PROJECT_BYTES: Final = 4 * 1024 * 1024

_ROW_FIELDS: Final = {
    "corpus_id",
    "source_frame_sha256",
    "frame",
    "split",
    "re_shoot_cluster_id",
    "confidence",
    "mode",
    "actor",
    "human_override",
    "manifest_row_sha256",
}
_PART_FIELDS: Final = {"region", "crop_box", "rotation", "colour_mode"}
_RECTANGLE_FIELDS: Final = {"space", "x", "y", "w", "h"}
_ROTATION_FIELDS: Final = {"rotation_millidegrees", "direction", "origin", "canvas"}
_CLUSTER_FIELDS: Final = {
    "schema",
    "corpus_id",
    "cluster_id",
    "member_frame_sha256",
    "split_count",
}


class _ClosedFixtureTreeBuilder(ET.TreeBuilder):
    """Reject declarations outside the deliberately tiny fixture-only XML seam."""

    def doctype(self, name: str, pubid: str | None, system: str | None) -> None:
        raise SchemaRefusal("ScanTailor fixture declarations are outside the closed XML shape")


def _plain_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _rectangle(
    value: Any,
    container: Mapping[str, int],
    what: str,
    inside: str,
    *,
    space: str,
) -> None:
    """A positive half-open integer rectangle in its declared pixel space."""
    if not isinstance(value, dict) or set(value) != _RECTANGLE_FIELDS:
        raise SchemaRefusal(f"{what} must be a closed space/x/y/w/h rectangle")
    if value["space"] != space:
        raise SchemaRefusal(f"{what} must use {space} coordinates")
    if not all(_plain_int(value[key]) for key in ("x", "y", "w", "h")):
        raise SchemaRefusal(f"{what} has a non-integer coordinate")
    if value["w"] <= 0 or value["h"] <= 0:
        raise SchemaRefusal(f"{what} is degenerate")
    if (
        value["x"] < container["x"]
        or value["y"] < container["y"]
        or value["x"] + value["w"] > container["x"] + container["w"]
        or value["y"] + value["h"] > container["y"] + container["h"]
    ):
        raise SchemaRefusal(f"{what} lies outside {inside}")


def _rotation(value: Any) -> None:
    if not isinstance(value, dict) or set(value) != _ROTATION_FIELDS:
        raise SchemaRefusal(
            "triage rotation must be a closed rotation_millidegrees/direction/origin/canvas record"
        )
    angle = value["rotation_millidegrees"]
    if not _plain_int(angle) or not -180_000 <= angle <= 180_000:
        raise SchemaRefusal("triage rotation must be an integer in [-180000, 180000] millidegrees")
    if value["direction"] != "clockwise":
        raise SchemaRefusal("triage rotation direction must be clockwise")
    if value["origin"] != "crop-centre":
        raise SchemaRefusal("triage rotation origin must be the crop-centre")
    if value["canvas"] != "expand":
        raise SchemaRefusal("triage rotation canvas must expand so rotation clips no cropped pixel")


def _row_digest(row: Mapping[str, Any]) -> str:
    split = row.get("split")
    if (
        isinstance(split, dict)
        and isinstance(split.get("parts"), list)
        and len(split["parts"]) > MAX_SPLIT_PARTS
    ):
        # ``make_row`` derives the digest before full validation. Refuse this
        # quadratic-work input before canonical serialization copies it. The
        # message says *where* the refusal happened, because the identical one in
        # ``_validate_split`` would otherwise make the two indistinguishable, and a
        # test could not then tell that this early guard had been removed.
        raise SchemaRefusal(
            f"triage split exceeds the {MAX_SPLIT_PARTS}-part limit before its row is serialized"
        )
    payload = {key: value for key, value in row.items() if key != "manifest_row_sha256"}
    try:
        return digest_bytes(canonical_bytes(payload))
    except (TypeError, ValueError, RecursionError) as error:
        # `canonical_bytes` refuses unsupported values with TypeError, circular
        # containers with ValueError, and cycles found by its own pre-walk with
        # RecursionError. A caller building a row is inside this contract's refusal
        # algebra, so each is one here too; a bare built-in exception would escape
        # every `except SchemaRefusal` in the pipeline.
        raise SchemaRefusal(f"triage row cannot be canonically serialized: {error}") from error


def _validate_split(split: Any, frame: Mapping[str, int]) -> None:
    if (
        not isinstance(split, dict)
        or set(split) != {"operation_order", "parts"}
        or not isinstance(split["parts"], list)
        or not split["parts"]
    ):
        raise SchemaRefusal("triage split must be a non-empty closed operation_order/parts record")
    if split["operation_order"] != SPLIT_OPERATION_ORDER:
        raise SchemaRefusal(f"triage split operation_order must be {SPLIT_OPERATION_ORDER}")
    if len(split["parts"]) > MAX_SPLIT_PARTS:
        raise SchemaRefusal(f"triage split exceeds the {MAX_SPLIT_PARTS}-part limit")
    whole = {"x": 0, "y": 0, "w": frame["width"], "h": frame["height"]}
    regions = []
    for part in split["parts"]:
        if not isinstance(part, dict) or set(part) != _PART_FIELDS:
            raise SchemaRefusal(
                "triage split part must be a closed region/crop_box/rotation/colour_mode record"
            )
        _rectangle(part["region"], whole, "triage split region", "its frame", space="frame")
        part_space = {"x": 0, "y": 0, "w": part["region"]["w"], "h": part["region"]["h"]}
        _rectangle(
            part["crop_box"],
            part_space,
            "triage crop_box",
            "its own split part",
            space="part",
        )
        _rotation(part["rotation"])
        if part["colour_mode"] not in COLOUR_MODES:
            raise SchemaRefusal("triage part colour_mode is not declared")
        regions.append(part["region"])
    # Pairwise-disjoint, inside the frame, and covering the frame's exact area is
    # a proof of exact partition for axis-aligned integer rectangles — no gap and
    # no overlap, without a tolerance. It costs the number of parts rather than
    # the number of pixels, which is what makes it usable: enumerating the pixels
    # of a 4000x6000 master takes ~8s and ~3.3GB for a single row, and a parish
    # set is ruled to run past 2,000 frames.
    for index, region in enumerate(regions):
        for other in regions[index + 1 :]:
            if not (
                region["x"] + region["w"] <= other["x"]
                or other["x"] + other["w"] <= region["x"]
                or region["y"] + region["h"] <= other["y"]
                or other["y"] + other["h"] <= region["y"]
            ):
                raise SchemaRefusal(
                    "triage split parts overlap, so they do not partition the frame"
                )
    if sum(region["w"] * region["h"] for region in regions) != frame["width"] * frame["height"]:
        raise SchemaRefusal("triage split geometry does not partition the frame")


def _validate_actor(actor: Any) -> None:
    if not isinstance(actor, dict) or set(actor) != {"kind", "identity", "revision"}:
        raise SchemaRefusal("triage actor must be a closed kind/identity/revision record")
    if actor["kind"] not in ACTOR_KINDS:
        raise SchemaRefusal(f"triage actor kind must be one of {ACTOR_KINDS}")
    if not isinstance(actor["identity"], str) or not actor["identity"].strip():
        raise SchemaRefusal("triage actor identity must be a non-blank resolved name")
    if actor["kind"] == "human":
        # GOVERNANCE 6 binds the resolved revision of the *model* that produced a
        # record. A person has no revision, and requiring a string here would only
        # buy a placeholder that protects nothing. Null says "no revision exists"
        # exactly, and refusing anything else keeps one spelling of that fact.
        if actor["revision"] is not None:
            raise SchemaRefusal("a human triage actor carries no revision; it must be null")
    elif not isinstance(actor["revision"], str) or not actor["revision"].strip():
        raise SchemaRefusal(
            "triage actor revision must be the resolved model revision or the ScanTailor version"
        )


def validate_row(row: Any) -> dict[str, Any]:
    """Return a row only when its closed fields and derived digest agree."""
    if not isinstance(row, dict) or set(row) != _ROW_FIELDS:
        raise SchemaRefusal(
            "triage row must use the closed decision-manifest schema (no winner field)"
        )
    if not is_sha256(row["source_frame_sha256"]):
        raise SchemaRefusal("triage row source_frame_sha256 is not a lowercase sha256")
    if not isinstance(row["corpus_id"], str) or not row["corpus_id"].strip():
        raise SchemaRefusal("triage row corpus_id must be a non-blank string")
    frame = row["frame"]
    if (
        not isinstance(frame, dict)
        or set(frame) != {"width", "height"}
        or not all(_plain_int(frame[key]) and frame[key] > 0 for key in frame)
    ):
        raise SchemaRefusal("triage row frame must be positive integer width and height")
    _validate_split(row["split"], frame)
    cluster = row["re_shoot_cluster_id"]
    if cluster is not None and (not isinstance(cluster, str) or not cluster.strip()):
        raise SchemaRefusal("triage re_shoot_cluster_id must be null or a non-blank string")
    if not _plain_int(row["confidence"]) or row["confidence"] not in CONFIDENCE_ORDINALS:
        raise SchemaRefusal("triage confidence is outside the closed ordinal [0, 4]")
    if row["mode"] not in TRIAGE_MODES:
        raise SchemaRefusal(f"triage mode is not one of {TRIAGE_MODES}")
    _validate_actor(row["actor"])
    if not isinstance(row["human_override"], bool):
        raise SchemaRefusal("triage human_override must be present and boolean")
    if not is_sha256(row["manifest_row_sha256"]) or row["manifest_row_sha256"] != _row_digest(row):
        raise SchemaRefusal("triage manifest_row_sha256 does not bind this row")
    return row


def make_row(**fields: Any) -> dict[str, Any]:
    """Make a self-identifying row; callers cannot supply its derived link."""
    if "manifest_row_sha256" in fields:
        raise SchemaRefusal("manifest_row_sha256 is derived, not caller supplied")
    row = dict(fields)
    row["manifest_row_sha256"] = _row_digest(row)
    return validate_row(row)


def make_part(
    region: Mapping[str, int],
    crop_box: Mapping[str, int],
    rotation_millidegrees: int,
    *,
    colour_mode: str,
    region_space: str = "frame",
    crop_space: str = "part",
    rotation_direction: str = "clockwise",
    rotation_origin: str = "crop-centre",
    rotation_canvas: str = "expand",
) -> dict[str, Any]:
    """Construct one explicit split/crop/rotate/convert decision.

    Constructor inputs use the natural spaces: ``region`` is frame-local and
    ``crop_box`` is part-local.  The record names both spaces so serialized data
    cannot be read the other way later.
    """
    return {
        "region": {"space": region_space, **dict(region)},
        "crop_box": {"space": crop_space, **dict(crop_box)},
        "rotation": {
            "rotation_millidegrees": rotation_millidegrees,
            "direction": rotation_direction,
            "origin": rotation_origin,
            "canvas": rotation_canvas,
        },
        "colour_mode": colour_mode,
    }


def make_split(
    parts: list[Mapping[str, Any]], *, operation_order: str = SPLIT_OPERATION_ORDER
) -> dict[str, Any]:
    """Construct the one allowed split/crop/rotate application order."""
    return {"operation_order": operation_order, "parts": [dict(part) for part in parts]}


def validate_cluster_record(record: Any) -> dict[str, Any]:
    if (
        not isinstance(record, dict)
        or set(record) != _CLUSTER_FIELDS
        or record["schema"] != CLUSTER_SCHEMA
    ):
        raise SchemaRefusal("triage cluster record must use its closed corpus-scoped schema")
    if not all(
        isinstance(record[key], str) and record[key].strip() for key in ("corpus_id", "cluster_id")
    ):
        raise SchemaRefusal("triage cluster record needs non-blank corpus_id and cluster_id")
    members = record["member_frame_sha256"]
    if isinstance(members, list) and len(members) > MAX_CLUSTER_MEMBERS:
        raise SchemaRefusal(f"triage cluster record exceeds the {MAX_CLUSTER_MEMBERS}-member limit")
    if (
        not isinstance(members, list)
        or len(members) < 2
        or not all(is_sha256(member) for member in members)
        or len(set(members)) != len(members)
    ):
        raise SchemaRefusal("triage cluster record needs two or more distinct frame source digests")
    if not _plain_int(record["split_count"]) or record["split_count"] < 1:
        raise SchemaRefusal("triage cluster split_count must be a positive integer")
    return record


def validate_manifest(
    manifest: Any, clusters: Mapping[str, Mapping[str, Any]] | None = None
) -> dict[str, Any]:
    """Validate a manifest shard, and its rows' cluster references when any are named.

    `clusters` is optional only because a manifest whose rows name no cluster has
    nothing to resolve. A manifest that *does* name one is refused without the
    corpus-scoped records rather than validated with its cluster references
    silently unchecked (GOVERNANCE 2).
    """
    if (
        not isinstance(manifest, dict)
        or set(manifest) != {"schema", "corpus_id", "records"}
        or manifest["schema"] != MANIFEST_SCHEMA
        or not isinstance(manifest["corpus_id"], str)
        or not manifest["corpus_id"].strip()
        or not isinstance(manifest["records"], list)
    ):
        raise SchemaRefusal("triage manifest must be the closed schema/corpus_id/records record")
    if len(manifest["records"]) > MAX_MANIFEST_ROWS:
        raise SchemaRefusal(f"triage manifest exceeds the {MAX_MANIFEST_ROWS}-row shard limit")
    rows = [validate_row(row) for row in manifest["records"]]
    if any(row["corpus_id"] != manifest["corpus_id"] for row in rows):
        raise SchemaRefusal("triage manifest contains a row from a different corpus")
    if len({row["source_frame_sha256"] for row in rows}) != len(rows):
        raise SchemaRefusal("triage manifest has more than one row for a submitted frame")
    named = {row["re_shoot_cluster_id"] for row in rows} - {None}
    if named and clusters is None:
        raise SchemaRefusal(
            "triage manifest names re-shoot clusters, so it cannot be validated without "
            "their corpus-scoped cluster records"
        )
    if clusters is not None and not isinstance(clusters, Mapping):
        raise SchemaRefusal("triage cluster records must be supplied as a mapping by cluster id")
    if clusters is not None and len(clusters) > MAX_CLUSTER_RECORDS:
        raise SchemaRefusal(
            f"triage cluster mapping exceeds the {MAX_CLUSTER_RECORDS}-record limit"
        )
    checked: dict[str, Mapping[str, Any]] = {}
    for cluster_id, record in (clusters or {}).items():
        checked[cluster_id] = validate_cluster_record(record)
        if record["cluster_id"] != cluster_id:
            # A mapping key that disagrees with the record it holds would resolve a
            # row against another cluster's members entirely.
            raise SchemaRefusal("triage cluster record is filed under a different cluster id")
        if record["corpus_id"] != manifest["corpus_id"]:
            raise SchemaRefusal("triage cluster record belongs to a different corpus")
    for row in rows:
        cluster_id = row["re_shoot_cluster_id"]
        if cluster_id is None:
            continue
        record = checked.get(cluster_id)
        if record is None or row["source_frame_sha256"] not in record["member_frame_sha256"]:
            raise SchemaRefusal(
                "triage row names a re-shoot cluster that does not contain its frame"
            )
        if len(row["split"]["parts"]) != record["split_count"]:
            raise SchemaRefusal("triage cluster members have incompatible split counts")
    return manifest


def verify_submitted_frame(row: Mapping[str, Any], submitted_bytes: bytes) -> None:
    """Door-side seam: refuse bytes not named by this pre-door row."""
    if not isinstance(submitted_bytes, bytes):
        raise SchemaRefusal("triage submitted frame must be bytes")
    checked = validate_row(dict(row))
    if digest_bytes(submitted_bytes) != checked["source_frame_sha256"]:
        raise SchemaRefusal("triage row source frame digest does not match submitted bytes")


def derivative_page_backlink(row: Mapping[str, Any], part_index: int) -> dict[str, str | int]:
    """Link one derivative page to its exact manifest row and split part."""
    checked = validate_row(dict(row))
    if not _plain_int(part_index) or part_index < 0 or part_index >= len(checked["split"]["parts"]):
        raise SchemaRefusal("triage derivative part_index does not name a split part")
    return {
        "corpus_id": checked["corpus_id"],
        "source_frame_sha256": checked["source_frame_sha256"],
        "triage_manifest_row_sha256": checked["manifest_row_sha256"],
        "triage_part_index": part_index,
    }


def _fixture_rectangle(text: str, what: str) -> dict[str, int]:
    try:
        x, y, width, height = (int(part) for part in text.split(","))
    except ValueError as error:
        raise SchemaRefusal(f"ScanTailor fixture {what} geometry is malformed") from error
    return {"x": x, "y": y, "w": width, "h": height}


def transcribe_scantailor_project(
    project_bytes: bytes, *, corpus_id: str, mode: str, human_override: bool
) -> list[dict[str, Any]]:
    """Parse only the documented, unverified fixture seam for ScanTailor geometry.

    The real ScanTailor Advanced project format was unavailable offline.  This
    refuses any other XML shape rather than pretending arbitrary project files
    contain page geometry; Unit 6 must replace this seam after verification.

    Every geometric field — each part's region, its crop and its own deskew — is
    read from the project's own attributes, never synthesized: a page that was not
    actually split declares one full-frame part explicitly, the same as a page
    that was.

    The actor is built here from the project's own recorded version rather than
    taken from the caller.  GOVERNANCE 6 asks the record to protect the past, and
    a caller-supplied version is an assertion about an artifact nobody read; a
    caller-supplied *kind* would also let a transcribed row claim to be natively
    produced, which is precisely what the "distinguishable by actor alone"
    requirement is for.
    """
    if not isinstance(project_bytes, bytes):
        raise SchemaRefusal("ScanTailor project fixture must be bytes")
    if len(project_bytes) > MAX_SCANTAILOR_PROJECT_BYTES:
        raise SchemaRefusal(
            "ScanTailor project fixture exceeds the "
            f"{MAX_SCANTAILOR_PROJECT_BYTES}-byte parsing limit"
        )
    try:
        parser = ET.XMLParser(target=_ClosedFixtureTreeBuilder())
        root = ET.fromstring(project_bytes, parser=parser)
    except ET.ParseError as error:
        raise SchemaRefusal("ScanTailor project fixture is not XML") from error
    if (
        root.tag != "scantailor-project"
        or set(root.attrib) != {"shape", "version"}
        or root.attrib["shape"] != "unverified-fixture-v0"
    ):
        raise SchemaRefusal(
            "ScanTailor project shape is unverified; only the documented fixture seam is accepted"
        )
    if root.text and root.text.strip():
        raise SchemaRefusal("ScanTailor fixture project has text outside its closed XML shape")
    version = root.attrib["version"]
    if not version.strip():
        raise SchemaRefusal("ScanTailor project records no version, so no actor can be resolved")
    actor = {"kind": "scantailor", "identity": SCANTAILOR_IDENTITY, "revision": version}
    rows = []
    for page in root:
        if page.tag != "page" or set(page.attrib) != {
            "source_frame_sha256",
            "width",
            "height",
            "confidence",
            "operation_order",
        }:
            raise SchemaRefusal("ScanTailor fixture page has the wrong closed geometry shape")
        if page.text and page.text.strip():
            raise SchemaRefusal(
                "ScanTailor fixture page has text outside its closed geometry shape"
            )
        parts = []
        for part in page:
            if part.tag != "part" or set(part.attrib) != {
                "region",
                "crop",
                "rotation_millidegrees",
                "rotation_direction",
                "rotation_origin",
                "rotation_canvas",
                "colour_mode",
                "region_space",
                "crop_space",
            }:
                raise SchemaRefusal("ScanTailor fixture part has the wrong closed geometry shape")
            if len(part) or (part.text and part.text.strip()):
                raise SchemaRefusal("ScanTailor fixture part has content outside its closed shape")
            try:
                rotation = int(part.attrib["rotation_millidegrees"])
            except ValueError as error:
                raise SchemaRefusal("ScanTailor fixture rotation is malformed") from error
            parts.append(
                make_part(
                    _fixture_rectangle(part.attrib["region"], "split"),
                    _fixture_rectangle(part.attrib["crop"], "crop"),
                    rotation,
                    colour_mode=part.attrib["colour_mode"],
                    region_space=part.attrib["region_space"],
                    crop_space=part.attrib["crop_space"],
                    rotation_direction=part.attrib["rotation_direction"],
                    rotation_origin=part.attrib["rotation_origin"],
                    rotation_canvas=part.attrib["rotation_canvas"],
                )
            )
            if part.tail and part.tail.strip():
                raise SchemaRefusal(
                    "ScanTailor fixture page has text outside its closed geometry shape"
                )
        if not parts:
            raise SchemaRefusal("ScanTailor fixture page declares no split part")
        try:
            frame = {"width": int(page.attrib["width"]), "height": int(page.attrib["height"])}
        except ValueError as error:
            raise SchemaRefusal("ScanTailor fixture page dimensions are malformed") from error
        try:
            confidence = int(page.attrib["confidence"])
        except ValueError as error:
            raise SchemaRefusal("ScanTailor fixture page confidence is malformed") from error
        rows.append(
            make_row(
                corpus_id=corpus_id,
                source_frame_sha256=page.attrib["source_frame_sha256"],
                frame=frame,
                split=make_split(parts, operation_order=page.attrib["operation_order"]),
                re_shoot_cluster_id=None,
                confidence=confidence,
                mode=mode,
                actor=dict(actor),
                human_override=human_override,
            )
        )
        if page.tail and page.tail.strip():
            raise SchemaRefusal("ScanTailor fixture project has text outside its closed XML shape")
    if not rows:
        raise SchemaRefusal("ScanTailor project fixture declares no pages")
    return rows
