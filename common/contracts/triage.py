"""The triage decision-manifest contract: per-frame split, crop, rotation and
colour decisions made before the Door, and the re-shoot cluster records beside
them. It records decisions only: it never transforms a source frame and never
chooses a member of a re-shoot cluster.

Geometry and colour conversion are per split part, not per frame, because a
document taped over the page at its own angle has no single gutter for
auto-split or global deskew to straighten both surfaces with, and a bound
spread's two pages each want their own crop besides.

``region`` is a half-open rectangle in source-frame pixel coordinates; after
cutting it, ``crop_box`` is half-open in that part's local pixel coordinates.
The cropped pixels are then rotated clockwise about the crop's centre onto an
expanded canvas. Pixel sampling, fill and encoding belong to the apply recipe
(`common.imaging.triage_apply_recipe`), not to geometry defaults hidden here.

A split names its operation order, which is also its version. Under
``region-crop-rotate`` a part is exactly the four fields above and the canvas beyond
the scan is the recipe's black. Under ``region-crop-rotate-crop`` a part also carries
``post_crop_box``, a half-open rectangle in the rotated canvas's coordinates (it may
reach past the canvas), and ``fill``, the sample levels, in the master's own mode,
that every pixel of the result outside the rotated scan takes. So a deskewed page is
cropped tight, and its margin can be paper rather than black.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Final

from common.contracts.canonical import canonical_bytes, digest_bytes, is_plain_int, is_sha256
from common.contracts.errors import SchemaRefusal
from common.contracts.stages import (
    MAX_TRIAGE_SPLIT_PARTS,
    TRIAGE_ACTOR_FIELDS,
    TRIAGE_ACTOR_KINDS,
    TRIAGE_MODES,
    TRIAGE_PART_FIELDS,
    TRIAGE_PART_FIELDS_V2,
    TRIAGE_ROW_FIELDS,
)

MANIFEST_SCHEMA: Final = "triage-decision-manifest-v1"
CLUSTER_SCHEMA: Final = "triage-re-shoot-cluster-v1"
CONFIDENCE_ORDINALS: Final = range(0, 5)
COLOUR_MODES: Final = ("keep", "grayscale", "rgb", "bitonal")
SPLIT_OPERATION_ORDER: Final = "region-crop-rotate"
SPLIT_OPERATION_ORDER_V2: Final = "region-crop-rotate-crop"
SPLIT_OPERATION_ORDERS: Final = (SPLIT_OPERATION_ORDER, SPLIT_OPERATION_ORDER_V2)
_PART_FIELDS: Final = {
    SPLIT_OPERATION_ORDER: TRIAGE_PART_FIELDS,
    SPLIT_OPERATION_ORDER_V2: TRIAGE_PART_FIELDS_V2,
}
# A post-crop may reach past its canvas, but not without limit: the render's pixel
# bound refuses the area, and this refuses a coordinate no scan could need first.
MAX_POST_CROP_COORDINATE: Final = 1_000_000
MAX_MANIFEST_ROWS: Final = 1_000
MAX_CLUSTER_RECORDS: Final = 1_000
MAX_CLUSTER_MEMBERS: Final = 4_096
# Caps the quadratic pairwise-disjointness check; a real frame with more parts
# needs a triage-policy change, not a bigger loop. Well under the 1,000-page shard,
# which one frame's parts must fit in.
MAX_SPLIT_PARTS: Final = MAX_TRIAGE_SPLIT_PARTS

_RECTANGLE_FIELDS: Final = {"space", "x", "y", "w", "h"}
_ROTATION_FIELDS: Final = {"rotation_millidegrees", "direction", "origin", "canvas"}
_CLUSTER_FIELDS: Final = {
    "schema",
    "corpus_id",
    "cluster_id",
    "member_frame_sha256",
    "split_count",
}


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
    if not all(is_plain_int(value[key]) for key in ("x", "y", "w", "h")):
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


def _post_crop(value: Any) -> None:
    if not isinstance(value, dict) or set(value) != _RECTANGLE_FIELDS:
        raise SchemaRefusal("triage post_crop_box must be a closed space/x/y/w/h rectangle")
    if value["space"] != "rotated":
        raise SchemaRefusal("triage post_crop_box must use rotated coordinates")
    if not all(is_plain_int(value[key]) for key in ("x", "y", "w", "h")):
        raise SchemaRefusal("triage post_crop_box has a non-integer coordinate")
    if value["w"] <= 0 or value["h"] <= 0:
        raise SchemaRefusal("triage post_crop_box is degenerate")
    if any(abs(value[key]) > MAX_POST_CROP_COORDINATE for key in ("x", "y", "w", "h")):
        raise SchemaRefusal("triage post_crop_box lies implausibly far from its canvas")


def _fill(value: Any) -> None:
    levels = value.get("levels") if isinstance(value, dict) else None
    if (
        not isinstance(value, dict)
        or set(value) != {"levels"}
        or not isinstance(levels, list)
        or not 1 <= len(levels) <= 4
        or not all(is_plain_int(level) and 0 <= level <= 255 for level in levels)
    ):
        raise SchemaRefusal(
            "triage fill must be a closed record of one to four sample levels in [0, 255]"
        )


def _rotation(value: Any) -> None:
    if not isinstance(value, dict) or set(value) != _ROTATION_FIELDS:
        raise SchemaRefusal(
            "triage rotation must be a closed rotation_millidegrees/direction/origin/canvas record"
        )
    angle = value["rotation_millidegrees"]
    if not is_plain_int(angle) or not -180_000 <= angle <= 180_000:
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
        # message names where it was refused, apart from the same limit in
        # ``_validate_split``.
        raise SchemaRefusal(
            f"triage split exceeds the {MAX_SPLIT_PARTS}-part limit before its row is serialized"
        )
    payload = {key: value for key, value in row.items() if key != "manifest_row_sha256"}
    try:
        return digest_bytes(canonical_bytes(payload))
    except TypeError as error:
        # `canonical_bytes` refuses every unrepresentable value with TypeError.
        raise SchemaRefusal(f"triage row cannot be canonically serialized: {error}") from error


def _validate_split(split: Any, frame: Mapping[str, int]) -> None:
    if (
        not isinstance(split, dict)
        or set(split) != {"operation_order", "parts"}
        or not isinstance(split["parts"], list)
        or not split["parts"]
    ):
        raise SchemaRefusal("triage split must be a non-empty closed operation_order/parts record")
    if split["operation_order"] not in SPLIT_OPERATION_ORDERS:
        raise SchemaRefusal(
            f"triage split operation_order must be one of {', '.join(SPLIT_OPERATION_ORDERS)}"
        )
    fields = _PART_FIELDS[split["operation_order"]]
    if len(split["parts"]) > MAX_SPLIT_PARTS:
        raise SchemaRefusal(f"triage split exceeds the {MAX_SPLIT_PARTS}-part limit")
    whole = {"x": 0, "y": 0, "w": frame["width"], "h": frame["height"]}
    regions = []
    for part in split["parts"]:
        if not isinstance(part, dict) or set(part) != fields:
            raise SchemaRefusal(
                "triage split part must be a closed "
                + (
                    "region/crop_box/rotation/colour_mode record"
                    if fields == TRIAGE_PART_FIELDS
                    else "region/crop_box/rotation/post_crop_box/fill/colour_mode record"
                )
                + f" under operation order {split['operation_order']}"
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
        if fields == TRIAGE_PART_FIELDS_V2:
            _post_crop(part["post_crop_box"])
            _fill(part["fill"])
        if part["colour_mode"] not in COLOUR_MODES:
            raise SchemaRefusal("triage part colour_mode is not declared")
        regions.append(part["region"])
    # Pairwise-disjoint, inside the frame, and covering the frame's exact area is
    # a proof of exact partition for axis-aligned integer rectangles — no gap and
    # no overlap, without a tolerance. It costs the number of parts rather than
    # the number of pixels, which is what makes it usable: enumerating the pixels
    # of a 4000x6000 master takes ~8s and ~3.3GB for a single row, and a parish
    # set can run past 2,000 frames.
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
    if not isinstance(actor, dict) or set(actor) != TRIAGE_ACTOR_FIELDS:
        raise SchemaRefusal("triage actor must be a closed kind/identity/revision record")
    if actor["kind"] not in TRIAGE_ACTOR_KINDS:
        raise SchemaRefusal(f"triage actor kind must be one of {TRIAGE_ACTOR_KINDS}")
    if not isinstance(actor["identity"], str) or not actor["identity"].strip():
        raise SchemaRefusal("triage actor identity must be a non-blank resolved name")
    if actor["kind"] == "human":
        # The resolved revision belongs to the *model* that produced a
        # record. A person has no revision, and requiring a string here would only
        # buy a placeholder that protects nothing. Null says "no revision exists"
        # exactly, and refusing anything else keeps one spelling of that fact.
        if actor["revision"] is not None:
            raise SchemaRefusal("a human triage actor carries no revision; it must be null")
    elif not isinstance(actor["revision"], str) or not actor["revision"].strip():
        raise SchemaRefusal(
            "triage actor revision must be the resolved model revision, ScanTailor version, "
            "or producer revision"
        )


def validate_row(row: Any) -> dict[str, Any]:
    """Return a row only when its closed fields and derived digest agree."""
    if not isinstance(row, dict) or set(row) != TRIAGE_ROW_FIELDS:
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
        or not all(is_plain_int(frame[key]) and frame[key] > 0 for key in frame)
    ):
        raise SchemaRefusal("triage row frame must be positive integer width and height")
    _validate_split(row["split"], frame)
    cluster = row["re_shoot_cluster_id"]
    if cluster is not None and (not isinstance(cluster, str) or not cluster.strip()):
        raise SchemaRefusal("triage re_shoot_cluster_id must be null or a non-blank string")
    if not is_plain_int(row["confidence"]) or row["confidence"] not in CONFIDENCE_ORDINALS:
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
    post_crop_box: Mapping[str, int] | None = None,
    fill: list[int] | None = None,
) -> dict[str, Any]:
    """Construct one explicit split/crop/rotate/convert decision.

    Constructor inputs use the natural spaces: ``region`` is frame-local and
    ``crop_box`` is part-local.  The record names both spaces so serialized data
    cannot be read the other way later. With ``post_crop_box`` (rotated-canvas
    coordinates) and ``fill`` (sample levels), the part is one of the second
    operation order.
    """
    if (post_crop_box is None) != (fill is None):
        raise SchemaRefusal("a post-crop and its fill are given together or not at all")
    second = {}
    if post_crop_box is not None:
        second = {
            "post_crop_box": {"space": "rotated", **dict(post_crop_box)},
            "fill": {"levels": list(fill)},
        }
    return {
        **second,
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
    if not is_plain_int(record["split_count"]) or record["split_count"] < 1:
        raise SchemaRefusal("triage cluster split_count must be a positive integer")
    return record


def validate_manifest(
    manifest: Any, clusters: Mapping[str, Mapping[str, Any]] | None = None
) -> dict[str, Any]:
    """Validate a manifest shard, and its rows' cluster references when any are named.

    `clusters` is optional only because a manifest whose rows name no cluster has
    nothing to resolve. A manifest that *does* name one is refused without the
    corpus-scoped records rather than validated with its cluster references
    silently unchecked.
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
    # Membership holds in both directions: a listed member whose own row names no
    # cluster, or another one, would drop out of every cluster check and be read
    # as an independent act.
    cluster_of_row = {row["source_frame_sha256"]: row["re_shoot_cluster_id"] for row in rows}
    cluster_of_member: dict[str, str] = {}
    for cluster_id, record in checked.items():
        for member in record["member_frame_sha256"]:
            if cluster_of_member.setdefault(member, cluster_id) != cluster_id:
                raise SchemaRefusal("a frame is a member of more than one re-shoot cluster")
            if member in cluster_of_row and cluster_of_row[member] != cluster_id:
                raise SchemaRefusal(
                    "a re-shoot cluster lists a frame whose own triage row does not name it"
                )
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
    if (
        not is_plain_int(part_index)
        or part_index < 0
        or part_index >= len(checked["split"]["parts"])
    ):
        raise SchemaRefusal("triage derivative part_index does not name a split part")
    return {
        "corpus_id": checked["corpus_id"],
        "source_frame_sha256": checked["source_frame_sha256"],
        "triage_manifest_row_sha256": checked["manifest_row_sha256"],
        "triage_part_index": part_index,
    }
