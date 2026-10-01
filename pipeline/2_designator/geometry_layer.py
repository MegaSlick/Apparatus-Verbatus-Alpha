"""The record detector's geometry: its sealed policy and its source-preserving proposals.

This module has no model imports. `yolo_obb` turns the detector's quantized
oriented boxes into closed records that keep each box and derive its crop by
the sealed policy; nothing is selected, ranked or rewritten.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Final, TypedDict

from common.contracts.canonical import digest_of, is_plain_int, is_sha256
from common.contracts.errors import ContractError, SchemaRefusal
from common.contracts.stages import DESIGNATOR, writing_directory
from common.runtree.store import BLOBS_DIR
from common.sealed_config import read_sealed_toml

POLICY_SCHEMA: Final = "designator-geometry-policy.v1"
RAW_PROPOSAL_SCHEMA: Final = "designator-raw-proposal.v1"
DEFAULT_POLICY_PATH: Final = (
    Path(__file__).resolve().parents[2] / "config" / "designator_geometry.toml"
)
_SOURCES: Final = frozenset({"yolo-obb"})
# Where a retained detector output lives: this stage's own blobs.
RESPONSE_BLOB_PREFIX: Final = f"{writing_directory(DESIGNATOR)}/{BLOBS_DIR}/"
# What a raw proposal's `observed_ordinals` count: the detector is one call per
# page, so its ordinals index the detections within that one retained output.
_RESPONSE_DETECTION: Final = "response-detection"
_OBSERVATION_UNITS: Final = frozenset({_RESPONSE_DETECTION})


class Bounds(TypedDict):
    x: int
    y: int
    w: int
    h: int


def _sha(value: object, what: str) -> str:
    if not is_sha256(value):
        raise SchemaRefusal(f"{what} is not a lowercase sha256")
    return value


def _closed(value: object, fields: set[str], what: str) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != fields:
        raise SchemaRefusal(f"{what} is not its closed schema")
    return value


def load_geometry_policy(path: str | Path = DEFAULT_POLICY_PATH) -> dict[str, Any]:
    """Load the sealed integer/toggle policy; unknown knobs fail closed."""
    try:
        document, digest = read_sealed_toml(path, "geometry policy")
    except ContractError as error:
        raise SchemaRefusal(f"geometry policy at {path} cannot be read: {error}") from error
    document = _closed(document, {"geometry"}, "geometry policy document")
    policy = _validate_geometry_policy(document["geometry"])
    return {"config_sha256": digest, **policy}


def _validate_geometry_policy(value: object) -> dict[str, Any]:
    """Validate every named sealed value, including non-behavioural provenance."""
    geometry = _closed(value, {"schema", "yolo_obb", "provenance"}, "geometry policy")
    if geometry["schema"] != POLICY_SCHEMA:
        raise SchemaRefusal("geometry policy has an unknown schema")
    yolo = _closed(
        geometry["yolo_obb"], {"role", "task", "crop_policy", "rectify"}, "YOLO OBB policy"
    )
    if not isinstance(yolo["role"], str) or not yolo["role"]:
        raise SchemaRefusal("YOLO OBB policy role is blank")
    if (
        yolo["task"] != "obb"
        or yolo["crop_policy"] != "aabb-enclose"
        or not isinstance(yolo["rectify"], bool)
    ):
        raise SchemaRefusal(
            "YOLO OBB policy must declare obb, aabb-enclose, and a boolean rectify toggle"
        )
    if yolo["rectify"]:
        # No rectification implementation exists yet; the toggle fails closed
        # rather than publishing "rectify" records for crops nothing rectified.
        raise SchemaRefusal("YOLO OBB rectification is not implemented; the toggle fails closed")
    provenance = _closed(
        geometry["provenance"],
        {"source", "calibrated_for_this_corpus", "caveat"},
        "geometry policy provenance",
    )
    if not all(
        isinstance(provenance[field], str) and provenance[field].strip()
        for field in ("source", "caveat")
    ) or not isinstance(provenance["calibrated_for_this_corpus"], bool):
        raise SchemaRefusal("geometry policy provenance is incomplete")
    return geometry


def _polygon_points(value: object, what: str) -> list[dict[str, int]]:
    """The shape of a page polygon (>= 3 distinct points), independent of page extent.

    The page-extent check is `_polygon`'s, which has the page size.
    """
    if not isinstance(value, list) or len(value) < 3:
        raise SchemaRefusal(f"{what} is not a polygon")
    points = []
    for item in value:
        point = _closed(item, {"x", "y"}, what)
        if (
            not is_plain_int(point["x"])
            or not is_plain_int(point["y"])
            or point["x"] < 0
            or point["y"] < 0
        ):
            raise SchemaRefusal(f"{what} is not non-negative integer page geometry")
        points.append({"x": point["x"], "y": point["y"]})
    if len({(point["x"], point["y"]) for point in points}) < 3:
        raise SchemaRefusal(f"{what} has fewer than three distinct points")
    return points


def _polygon(value: object, page_w: int, page_h: int, what: str) -> list[dict[str, int]]:
    points = _polygon_points(value, what)
    if any(not (point["x"] < page_w and point["y"] < page_h) for point in points):
        raise SchemaRefusal(f"{what} is outside page pixels")
    return points


def _score_bp(value: object, what: str) -> int:
    """One basis-point confidence predicate, asked wherever a score arrives."""
    if not is_plain_int(value) or not 0 <= value <= 10_000:
        raise SchemaRefusal(f"{what} is not an integer basis-point confidence")
    return value


def enclosing_aabb(points: list[dict[str, int]], page_w: int, page_h: int) -> Bounds:
    min_x, max_x = min(point["x"] for point in points), max(point["x"] for point in points)
    min_y, max_y = min(point["y"] for point in points), max(point["y"] for point in points)
    # Points name covered pixel centres, so the half-open crop reaches one pixel
    # beyond the maximum centre, except at the page edge.
    far_x, far_y = min(page_w, max_x + 1), min(page_h, max_y + 1)
    bounds: Bounds = {"x": min_x, "y": min_y, "w": far_x - min_x, "h": far_y - min_y}
    if bounds["w"] <= 0 or bounds["h"] <= 0:
        raise SchemaRefusal("oriented geometry has no enclosing page crop")
    return bounds


def _transform(page_w: int, page_h: int) -> dict[str, Any]:
    return {
        "source_space": "page-pixels",
        "target_space": "page-pixels",
        "scale_x": {"numerator": 1, "denominator": 1},
        "scale_y": {"numerator": 1, "denominator": 1},
        "page_width_px": page_w,
        "page_height_px": page_h,
    }


def _ref(value: object, prefix: str, what: str) -> dict[str, str]:
    ref = _closed(value, {"relative_path", "sha256"}, what)
    if not isinstance(ref["relative_path"], str) or not ref["relative_path"].startswith(prefix):
        raise SchemaRefusal(f"{what} does not name {prefix}")
    return {"relative_path": ref["relative_path"], "sha256": _sha(ref["sha256"], what)}


def validate_raw_proposal(payload: object) -> dict[str, Any]:
    fields = {
        "schema",
        "proposal_id",
        "source",
        "page_id",
        "page_ordinal",
        "geometry_kind",
        "geometry",
        "aabb",
        "page_transform",
        "score_bp",
        "receipt_ref",
        "response_ref",
        "adapter_config_sha256",
        "observation_unit",
        "observed_ordinals",
        "crop_policy",
    }
    record = _closed(payload, fields, "raw proposal")
    if (
        record["schema"] != RAW_PROPOSAL_SCHEMA
        or not isinstance(record["proposal_id"], str)
        or not record["proposal_id"]
    ):
        raise SchemaRefusal("raw proposal lacks its schema or identity")
    if (
        record["source"] not in _SOURCES
        or not isinstance(record["page_id"], str)
        or not record["page_id"]
        or not is_plain_int(record["page_ordinal"])
        or record["page_ordinal"] < 0
    ):
        raise SchemaRefusal("raw proposal has invalid source or page lineage")
    if record["geometry_kind"] != "obb" or not isinstance(record["geometry"], list):
        raise SchemaRefusal("raw proposal has unknown geometry")
    transform = _closed(
        record["page_transform"],
        {"source_space", "target_space", "scale_x", "scale_y", "page_width_px", "page_height_px"},
        "raw proposal transform",
    )
    page_w, page_h = transform["page_width_px"], transform["page_height_px"]
    if not is_plain_int(page_w) or not is_plain_int(page_h) or page_w <= 0 or page_h <= 0:
        raise SchemaRefusal("raw proposal transform has invalid page size")
    for axis in ("scale_x", "scale_y"):
        scale = _closed(transform[axis], {"numerator", "denominator"}, f"raw proposal {axis}")
        if (
            not is_plain_int(scale["numerator"])
            or not is_plain_int(scale["denominator"])
            or scale["numerator"] <= 0
            or scale["denominator"] <= 0
        ):
            raise SchemaRefusal("raw proposal transform has invalid rational scale")
    if transform["source_space"] != "page-pixels" or transform["target_space"] != "page-pixels":
        raise SchemaRefusal("raw proposal transform does not end in page pixels")
    # A same-space transform is only coherent at identity scale; a non-identity
    # scale here would claim resampling that never happened.
    if (
        transform["scale_x"]["numerator"] != transform["scale_x"]["denominator"]
        or transform["scale_y"]["numerator"] != transform["scale_y"]["denominator"]
    ):
        raise SchemaRefusal("raw proposal transform claims a non-identity page-pixels scale")
    geometry = _polygon(record["geometry"], page_w, page_h, "raw proposal geometry")
    expected_aabb = enclosing_aabb(geometry, page_w, page_h)
    if record["aabb"] != expected_aabb:
        raise SchemaRefusal("raw proposal AABB does not derive from its source geometry")
    _score_bp(record["score_bp"], "raw proposal score")
    expected_proposal_id = _proposal_id(
        record["source"], record["page_id"], geometry, record["score_bp"]
    )
    if record["proposal_id"] != expected_proposal_id:
        raise SchemaRefusal("raw proposal identity does not derive from source content")
    _ref(record["receipt_ref"], "receipts/sha256/", "raw proposal receipt reference")
    _ref(record["response_ref"], RESPONSE_BLOB_PREFIX, "raw proposal response reference")
    _sha(record["adapter_config_sha256"], "raw proposal adapter config")
    # observation_unit names what observed_ordinals count, so a reader never
    # has to guess what an ordinal indexes.
    if record["observation_unit"] not in _OBSERVATION_UNITS:
        raise SchemaRefusal("raw proposal does not name what its observation ordinals count")
    if (
        not isinstance(record["observed_ordinals"], list)
        or not record["observed_ordinals"]
        or any(not is_plain_int(value) or value < 0 for value in record["observed_ordinals"])
        or record["observed_ordinals"] != sorted(set(record["observed_ordinals"]))
    ):
        raise SchemaRefusal(
            "raw proposal observed ordinals are not sorted unique non-negative integers"
        )
    crop_policy = record["crop_policy"]
    if crop_policy is not None:
        policy = _closed(crop_policy, {"mode", "loss_recorded"}, "raw proposal crop policy")
        if policy["mode"] != "aabb-enclose" or not isinstance(policy["loss_recorded"], bool):
            raise SchemaRefusal("raw proposal crop policy is malformed")
    return record


def _proposal_id(source: str, page_id: str, geometry: list[dict[str, int]], score_bp: int) -> str:
    # Identity excludes observed_ordinals (accumulated as repeated detections
    # union, so an id built from a first-seen value wouldn't reproduce from the
    # final record); score stays identity-bearing since differently scored
    # observations are distinct raw signals.
    return f"proposal_{digest_of({'source': source, 'page_id': page_id, 'geometry': geometry, 'score_bp': score_bp})[:16]}"


def _raw(
    source: str,
    page_id: str,
    page_ordinal: int,
    points: list[dict[str, int]],
    page_w: int,
    page_h: int,
    score_bp: int,
    receipt_ref: dict[str, str],
    response_ref: dict[str, str],
    config_sha: str,
    observation_unit: str,
    ordinals: list[int],
    kind: str,
) -> dict[str, Any]:
    payload = {
        "schema": RAW_PROPOSAL_SCHEMA,
        "proposal_id": _proposal_id(source, page_id, points, score_bp),
        "source": source,
        "page_id": page_id,
        "page_ordinal": page_ordinal,
        "geometry_kind": kind,
        "geometry": points,
        "aabb": enclosing_aabb(points, page_w, page_h),
        "page_transform": _transform(page_w, page_h),
        "score_bp": score_bp,
        "receipt_ref": receipt_ref,
        "response_ref": response_ref,
        "adapter_config_sha256": config_sha,
        "observation_unit": observation_unit,
        "observed_ordinals": ordinals,
        "crop_policy": None,
    }
    return validate_raw_proposal(payload)


def _retain_by_content_identity(union: dict[str, dict[str, Any]], proposal: dict[str, Any]) -> None:
    """Union exact repeated observations while retaining every observation ordinal."""
    proposal_id = proposal["proposal_id"]
    existing = union.get(proposal_id)
    if existing is None:
        union[proposal_id] = proposal
        return
    existing_content = {key: value for key, value in existing.items() if key != "observed_ordinals"}
    proposal_content = {key: value for key, value in proposal.items() if key != "observed_ordinals"}
    if existing_content != proposal_content:
        raise SchemaRefusal("raw proposal content identity collides across different records")
    existing["observed_ordinals"] = sorted(
        set(existing["observed_ordinals"] + proposal["observed_ordinals"])
    )


def yolo_obb(
    *,
    page_id: str,
    page_ordinal: int,
    page_w: int,
    page_h: int,
    policy: dict[str, Any],
    receipt_ref: dict[str, str],
    response_ref: dict[str, str],
    detections: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Adapt OBB detections without loss: retain OBB and derive sealed crop policy.

    Each detection carries `ordinal`, its index in the retained detector response,
    so a proposal's `observed_ordinals` name detections in that response even when
    the caller passes only some of them.
    """
    checked = load_geometry_policy_record(policy)
    union: dict[str, dict[str, Any]] = {}
    previous = -1
    for detection in detections:
        item = _closed(detection, {"ordinal", "obb", "score_bp"}, "YOLO OBB detection")
        ordinal = item["ordinal"]
        if not isinstance(ordinal, int) or isinstance(ordinal, bool) or ordinal <= previous:
            raise SchemaRefusal(
                "YOLO OBB detection ordinals must be increasing non-negative integers"
            )
        previous = ordinal
        points = _polygon(item["obb"], page_w, page_h, "YOLO OBB")
        if len(points) != 4:
            raise SchemaRefusal("YOLO OBB must contain exactly four corners")
        raw = _raw(
            "yolo-obb",
            page_id,
            page_ordinal,
            points,
            page_w,
            page_h,
            item["score_bp"],
            receipt_ref,
            response_ref,
            checked["config_sha256"],
            _RESPONSE_DETECTION,
            [ordinal],
            "obb",
        )
        raw["crop_policy"] = {"mode": "aabb-enclose", "loss_recorded": False}
        # crop_policy is attached after _raw builds the record, so re-validate
        # the whole thing rather than the intermediate shape no caller sees.
        _retain_by_content_identity(union, validate_raw_proposal(raw))
    return [union[proposal_id] for proposal_id in sorted(union)]


def load_geometry_policy_record(policy: object) -> dict[str, Any]:
    """Validate a previously loaded policy before a caller trusts its toggle."""
    if not isinstance(policy, dict) or set(policy) != {
        "config_sha256",
        "schema",
        "yolo_obb",
        "provenance",
    }:
        raise SchemaRefusal("loaded geometry policy is not a closed record")
    _sha(policy["config_sha256"], "geometry policy digest")
    _validate_geometry_policy(
        {field: policy[field] for field in ("schema", "yolo_obb", "provenance")}
    )
    return policy
