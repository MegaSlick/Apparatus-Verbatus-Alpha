"""R2 geometry adapters, one-receipt Chandra custody, and additive resolution.

This module deliberately has no model imports.  The adapters accept fixture-shaped
model outputs and turn them into closed, source-preserving records.  A live caller
may supply the same shapes later, but no source is selected, ranked, or rewritten:
the resolver derives a deterministic union/hierarchy record from every retained raw
proposal and represents overlap and occlusion as review facts.
"""

from __future__ import annotations

import tomllib
from pathlib import Path
from typing import Any, Callable, Final, TypedDict

from common.contracts.canonical import digest_bytes, digest_of
from common.contracts.envelope import build_envelope, validate_envelope
from common.contracts.errors import SchemaRefusal
from common.contracts.identities import artifact_id
from common.contracts.stages import DESIGNATOR

POLICY_SCHEMA: Final = "designator-geometry-policy.v1"
RAW_PROPOSAL_SCHEMA: Final = "designator-raw-proposal.v1"
OCCLUSION_SCHEMA: Final = "designator-occlusion.v1"
RESOLUTION_SCHEMA: Final = "designator-geometry-resolution.v1"
DEFAULT_POLICY_PATH: Final = (
    Path(__file__).resolve().parents[2] / "config" / "designator_geometry.toml"
)
_SOURCES: Final = frozenset({"surya", "yolo-obb", "chandra-layout"})


class Bounds(TypedDict):
    x: int
    y: int
    w: int
    h: int


def _integer(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _sha(value: object, what: str) -> str:
    if (
        not isinstance(value, str)
        or len(value) != 64
        or any(c not in "0123456789abcdef" for c in value)
    ):
        raise SchemaRefusal(f"{what} is not a lowercase sha256")
    return value


def _closed(value: object, fields: set[str], what: str) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != fields:
        raise SchemaRefusal(f"{what} is not its closed schema")
    return value


def load_geometry_policy(path: str | Path = DEFAULT_POLICY_PATH) -> dict[str, Any]:
    """Load the sealed integer/toggle policy; unknown knobs fail closed."""
    data = Path(path).read_bytes()
    try:
        document = tomllib.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, tomllib.TOMLDecodeError) as error:
        raise SchemaRefusal(f"geometry policy cannot be decoded: {error}") from error
    document = _closed(document, {"geometry"}, "geometry policy document")
    geometry = _validate_geometry_policy(document["geometry"])
    return {"config_sha256": digest_bytes(data), **geometry}


def _validate_geometry_policy(value: object) -> dict[str, Any]:
    """Validate every named sealed value, including non-behavioural provenance."""
    geometry = _closed(value, {"schema", "surya", "yolo_obb", "provenance"}, "geometry policy")
    if geometry["schema"] != POLICY_SCHEMA:
        raise SchemaRefusal("geometry policy has an unknown schema")
    surya = _closed(
        geometry["surya"],
        {
            "role",
            "tile_width_px",
            "tile_height_px",
            "half_tile_vertical_offset_px",
            "horizontal_overlap_px",
        },
        "Surya policy",
    )
    for field in ("role",):
        if not isinstance(surya[field], str) or not surya[field]:
            raise SchemaRefusal(f"Surya policy {field} is blank")
    for field in (
        "tile_width_px",
        "tile_height_px",
        "half_tile_vertical_offset_px",
        "horizontal_overlap_px",
    ):
        if not _integer(surya[field]) or surya[field] <= 0:
            raise SchemaRefusal(f"Surya policy {field} is not a positive integer")
    if surya["half_tile_vertical_offset_px"] * 2 != surya["tile_height_px"]:
        raise SchemaRefusal("Surya vertical offset does not equal half the sealed tile height")
    if surya["horizontal_overlap_px"] * 2 != surya["tile_width_px"]:
        raise SchemaRefusal("Surya horizontal overlap does not equal half the sealed tile width")
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


def _point(value: object, page_w: int, page_h: int, what: str) -> dict[str, int]:
    point = _closed(value, {"x", "y"}, what)
    if (
        not _integer(point["x"])
        or not _integer(point["y"])
        or not (0 <= point["x"] < page_w and 0 <= point["y"] < page_h)
    ):
        raise SchemaRefusal(f"{what} is outside page pixels")
    return {"x": point["x"], "y": point["y"]}


def _polygon(value: object, page_w: int, page_h: int, what: str) -> list[dict[str, int]]:
    if not isinstance(value, list) or len(value) < 3:
        raise SchemaRefusal(f"{what} is not a polygon")
    points = [_point(point, page_w, page_h, what) for point in value]
    if len({(point["x"], point["y"]) for point in points}) < 3:
        raise SchemaRefusal(f"{what} has fewer than three distinct points")
    return points


def enclosing_aabb(points: list[dict[str, int]], page_w: int, page_h: int) -> Bounds:
    min_x, max_x = min(point["x"] for point in points), max(point["x"] for point in points)
    min_y, max_y = min(point["y"] for point in points), max(point["y"] for point in points)
    # Points name covered pixel centres, so the enclosing half-open crop reaches one
    # pixel beyond the maximum centre, except at the sealed page edge.
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
        "observed_passes",
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
        or not _integer(record["page_ordinal"])
        or record["page_ordinal"] < 0
    ):
        raise SchemaRefusal("raw proposal has invalid source or page lineage")
    if record["geometry_kind"] not in {"polygon", "obb", "aabb"} or not isinstance(
        record["geometry"], list
    ):
        raise SchemaRefusal("raw proposal has unknown geometry")
    transform = _closed(
        record["page_transform"],
        {"source_space", "target_space", "scale_x", "scale_y", "page_width_px", "page_height_px"},
        "raw proposal transform",
    )
    page_w, page_h = transform["page_width_px"], transform["page_height_px"]
    if not _integer(page_w) or not _integer(page_h) or page_w <= 0 or page_h <= 0:
        raise SchemaRefusal("raw proposal transform has invalid page size")
    for axis in ("scale_x", "scale_y"):
        scale = _closed(transform[axis], {"numerator", "denominator"}, f"raw proposal {axis}")
        if (
            not _integer(scale["numerator"])
            or not _integer(scale["denominator"])
            or scale["numerator"] <= 0
            or scale["denominator"] <= 0
        ):
            raise SchemaRefusal("raw proposal transform has invalid rational scale")
    if transform["source_space"] != "page-pixels" or transform["target_space"] != "page-pixels":
        raise SchemaRefusal("raw proposal transform does not end in page pixels")
    # source_space and target_space are forced equal above (both "page-pixels"); a
    # same-space transform is only coherent at identity scale. A non-identity
    # scale_x/scale_y here would claim resampling that never happened -- the
    # geometry stored is already in this page's actual pixel grid.
    if (
        transform["scale_x"]["numerator"] != transform["scale_x"]["denominator"]
        or transform["scale_y"]["numerator"] != transform["scale_y"]["denominator"]
    ):
        raise SchemaRefusal("raw proposal transform claims a non-identity page-pixels scale")
    geometry = _polygon(record["geometry"], page_w, page_h, "raw proposal geometry")
    expected_aabb = enclosing_aabb(geometry, page_w, page_h)
    if record["aabb"] != expected_aabb:
        raise SchemaRefusal("raw proposal AABB does not derive from its source geometry")
    if not _integer(record["score_bp"]) or not 0 <= record["score_bp"] <= 10_000:
        raise SchemaRefusal("raw proposal score is not an integer basis-point confidence")
    expected_proposal_id = _proposal_id(
        record["source"], record["page_id"], geometry, record["score_bp"]
    )
    if record["proposal_id"] != expected_proposal_id:
        raise SchemaRefusal("raw proposal identity does not derive from source content")
    _ref(record["receipt_ref"], "receipts/sha256/", "raw proposal receipt reference")
    _ref(record["response_ref"], "designator/blobs/sha256/", "raw proposal response reference")
    _sha(record["adapter_config_sha256"], "raw proposal adapter config")
    if (
        not isinstance(record["observed_passes"], list)
        or not record["observed_passes"]
        or any(not _integer(value) or value < 0 for value in record["observed_passes"])
        or record["observed_passes"] != sorted(set(record["observed_passes"]))
    ):
        raise SchemaRefusal(
            "raw proposal observed passes are not sorted unique non-negative integers"
        )
    crop_policy = record["crop_policy"]
    if crop_policy is not None:
        policy = _closed(crop_policy, {"mode", "loss_recorded"}, "raw proposal crop policy")
        if policy["mode"] not in {"aabb-enclose", "rectify"} or not isinstance(
            policy["loss_recorded"], bool
        ):
            raise SchemaRefusal("raw proposal crop policy is malformed")
    return record


def validate_occlusion(payload: object) -> dict[str, Any]:
    fields = {
        "schema",
        "occlusion_id",
        "page_id",
        "page_ordinal",
        "polygon",
        "z_relationship",
        "review_state",
        "receipt_ref",
    }
    record = _closed(payload, fields, "occlusion")
    if (
        record["schema"] != OCCLUSION_SCHEMA
        or not isinstance(record["occlusion_id"], str)
        or not record["occlusion_id"]
        or not isinstance(record["page_id"], str)
        or not record["page_id"]
        or not _integer(record["page_ordinal"])
        or record["page_ordinal"] < 0
    ):
        raise SchemaRefusal("occlusion lacks page lineage")
    # Occlusion coordinates cannot be validated without the source page's extent;
    # this payload records the exact polygon and resolver checks its page match.
    if (
        not isinstance(record["polygon"], list)
        or len(record["polygon"]) < 3
        or any(
            not isinstance(point, dict)
            or set(point) != {"x", "y"}
            or not _integer(point["x"])
            or not _integer(point["y"])
            or point["x"] < 0
            or point["y"] < 0
            for point in record["polygon"]
        )
    ):
        raise SchemaRefusal("occlusion polygon is not non-negative integer page geometry")
    if record["z_relationship"] not in {"unknown", "above-ink", "below-ink"} or record[
        "review_state"
    ] not in {"open", "reviewed"}:
        raise SchemaRefusal("occlusion has invalid z relationship or review state")
    _ref(record["receipt_ref"], "receipts/sha256/", "occlusion receipt reference")
    return record


def _proposal_id(source: str, page_id: str, geometry: list[dict[str, int]], score_bp: int) -> str:
    # Identity contains only stable final-record facts. observed_passes is
    # provenance accumulated as tilings union, so including its first-seen value
    # made an id impossible to reproduce from the final record. Score remains
    # identity-bearing: differently scored observations are distinct raw signals.
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
    passes: list[int],
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
        "observed_passes": passes,
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
    existing_without_passes = {
        key: value for key, value in existing.items() if key != "observed_passes"
    }
    proposal_without_passes = {
        key: value for key, value in proposal.items() if key != "observed_passes"
    }
    if existing_without_passes != proposal_without_passes:
        raise SchemaRefusal("raw proposal content identity collides across different records")
    existing["observed_passes"] = sorted(
        set(existing["observed_passes"] + proposal["observed_passes"])
    )


def surya_double_pass(
    *,
    page_id: str,
    page_ordinal: int,
    page_w: int,
    page_h: int,
    policy: dict[str, Any],
    receipt_ref: dict[str, str],
    response_ref: dict[str, str],
    detect: Callable[[dict[str, int]], list[dict[str, Any]]],
) -> list[dict[str, Any]]:
    """Run exact sealed tiling at zero and half-tile offset, then additive union.

    Tiling covers the full page in BOTH axes -- a page wider than one sealed tile
    is a real corpus case (parish scans vary in width; nothing in the design
    caveats tiling to narrow pages), so every column band is tiled, not only the
    first. The vertical axis carries the second half-tile-offset pass; horizontal
    tiles overlap by the sealed half-tile amount so an original x=1400 boundary
    lies inside another complete tile rather than splitting a detection into two
    unrelated proposals.
    """
    checked = load_geometry_policy_record(policy)
    tile_h, tile_w = checked["surya"]["tile_height_px"], checked["surya"]["tile_width_px"]
    horizontal_step = tile_w - checked["surya"]["horizontal_overlap_px"]
    offsets = (0, checked["surya"]["half_tile_vertical_offset_px"])
    union: dict[str, dict[str, Any]] = {}
    for pass_ordinal, offset in enumerate(offsets):
        for y in range(offset, page_h, tile_h):
            for x in range(0, page_w, horizontal_step):
                tile = {
                    "x": x,
                    "y": y,
                    "w": min(tile_w, page_w - x),
                    "h": min(tile_h, page_h - y),
                }
                for detection in detect(tile):
                    item = _closed(detection, {"polygon", "score_bp"}, "Surya detection")
                    points = _polygon(item["polygon"], page_w, page_h, "Surya polygon")
                    if not _integer(item["score_bp"]) or not 0 <= item["score_bp"] <= 10_000:
                        raise SchemaRefusal("Surya score is not basis points")
                    key = digest_of({"geometry": points, "score_bp": item["score_bp"]})
                    if key not in union:
                        union[key] = _raw(
                            "surya",
                            page_id,
                            page_ordinal,
                            points,
                            page_w,
                            page_h,
                            item["score_bp"],
                            receipt_ref,
                            response_ref,
                            checked["config_sha256"],
                            [pass_ordinal],
                            "polygon",
                        )
                    else:
                        union[key]["observed_passes"] = sorted(
                            set(union[key]["observed_passes"] + [pass_ordinal])
                        )
    return [union[key] for key in sorted(union)]


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
    """Adapt OBB fixture output without loss: retain OBB and derive sealed crop policy."""
    checked = load_geometry_policy_record(policy)
    union: dict[str, dict[str, Any]] = {}
    for ordinal, detection in enumerate(detections):
        item = _closed(detection, {"obb", "score_bp"}, "YOLO OBB detection")
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
            [ordinal],
            "obb",
        )
        raw["crop_policy"] = {
            "mode": "rectify" if checked["yolo_obb"]["rectify"] else "aabb-enclose",
            "loss_recorded": checked["yolo_obb"]["rectify"],
        }
        _retain_by_content_identity(union, raw)
    return [union[proposal_id] for proposal_id in sorted(union)]


def chandra_layout(
    *,
    page_id: str,
    page_ordinal: int,
    page_w: int,
    page_h: int,
    config_sha256: str,
    receipt_ref: dict[str, str],
    response_ref: dict[str, str],
    regions: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Split only geometry from a retained Chandra response; textual fields fail closed."""
    union: dict[str, dict[str, Any]] = {}
    for ordinal, region in enumerate(regions):
        item = _closed(region, {"bbox_1000", "score_bp"}, "Chandra layout region")
        box = item["bbox_1000"]
        if (
            not isinstance(box, list)
            or len(box) != 4
            or any(not _integer(value) or not 0 <= value <= 1000 for value in box)
        ):
            raise SchemaRefusal("Chandra bbox is not four integer 0-1000 coordinates")
        x0, y0, x1, y1 = box
        if x1 <= x0 or y1 <= y0:
            raise SchemaRefusal("Chandra bbox is empty")
        points = [
            {"x": x0 * page_w // 1000, "y": y0 * page_h // 1000},
            {"x": min(page_w - 1, (x1 * page_w + 999) // 1000 - 1), "y": y0 * page_h // 1000},
            {
                "x": min(page_w - 1, (x1 * page_w + 999) // 1000 - 1),
                "y": min(page_h - 1, (y1 * page_h + 999) // 1000 - 1),
            },
            {"x": x0 * page_w // 1000, "y": min(page_h - 1, (y1 * page_h + 999) // 1000 - 1)},
        ]
        _retain_by_content_identity(
            union,
            _raw(
                "chandra-layout",
                page_id,
                page_ordinal,
                points,
                page_w,
                page_h,
                item["score_bp"],
                receipt_ref,
                response_ref,
                config_sha256,
                [ordinal],
                "aabb",
            ),
        )
    return [union[proposal_id] for proposal_id in sorted(union)]


def load_geometry_policy_record(policy: object) -> dict[str, Any]:
    """Validate a previously loaded policy before a caller trusts its toggle."""
    if not isinstance(policy, dict) or set(policy) != {
        "config_sha256",
        "schema",
        "surya",
        "yolo_obb",
        "provenance",
    }:
        raise SchemaRefusal("loaded geometry policy is not a closed record")
    # Recheck every nested value at the adapter boundary; a caller may not remove
    # a role/provenance field after loading and still use the remaining limits.
    _sha(policy["config_sha256"], "geometry policy digest")
    _validate_geometry_policy(
        {field: policy[field] for field in ("schema", "surya", "yolo_obb", "provenance")}
    )
    return policy


def retain_chandra_response(
    tree: Any, response: bytes, receipt_ref: dict[str, str]
) -> dict[str, str]:
    """Store one raw response blob and bind it jointly to the one serving receipt."""
    _ref(receipt_ref, "receipts/sha256/", "Chandra receipt reference")
    if not isinstance(response, bytes):
        raise SchemaRefusal("Chandra raw response is not bytes")
    digest, published = tree.put_blob(DESIGNATOR, response)
    reference = {"relative_path": published.relative_path, "sha256": digest}
    return _ref(reference, "designator/blobs/sha256/", "Chandra response reference")


def read_retained_chandra_response(tree: Any, response_ref: object, receipt_ref: object) -> bytes:
    """R3's future intake boundary: forged blob or receipt references are refused."""
    response = _ref(response_ref, "designator/blobs/sha256/", "Chandra response reference")
    receipt = _ref(receipt_ref, "receipts/sha256/", "Chandra receipt reference")
    # Receipt validation is delegated to the run tree, which verifies its schema,
    # path, and bytes.  The response itself is opaque textual custody, never parsed
    # by this geometry module.
    tree.read_run_receipt(receipt)
    data = tree.read_bytes(response["relative_path"])
    if digest_bytes(data) != response["sha256"]:
        raise SchemaRefusal("Chandra response blob differs from its sealed reference")
    return data


def raw_proposal_envelope(
    *,
    run_id: str,
    subject_id: str,
    config_digest: str,
    adapter_revision: str,
    inputs: list[dict[str, str]],
    payload: dict[str, Any],
) -> dict[str, Any]:
    checked = validate_raw_proposal(payload)
    if subject_id != checked["proposal_id"]:
        raise SchemaRefusal("raw proposal envelope subject is not its proposal identity")
    return build_envelope(
        run_id=run_id,
        artifact_id=artifact_id(DESIGNATOR, "raw-proposal", subject_id),
        subject_id=subject_id,
        stage=DESIGNATOR,
        kind="raw-proposal",
        outcome="proposed",
        config_digest=config_digest,
        adapter_revision=adapter_revision,
        inputs=inputs,
        payload=checked,
    )


def occlusion_envelope(
    *,
    run_id: str,
    subject_id: str,
    config_digest: str,
    adapter_revision: str,
    inputs: list[dict[str, str]],
    payload: dict[str, Any],
) -> dict[str, Any]:
    checked = validate_occlusion(payload)
    if subject_id != checked["occlusion_id"]:
        raise SchemaRefusal("occlusion envelope subject is not its occlusion identity")
    return build_envelope(
        run_id=run_id,
        artifact_id=artifact_id(DESIGNATOR, "occlusion", subject_id),
        subject_id=subject_id,
        stage=DESIGNATOR,
        kind="occlusion",
        outcome="proposed",
        config_digest=config_digest,
        adapter_revision=adapter_revision,
        inputs=inputs,
        payload=checked,
    )


def _overlap(left: Bounds, right: Bounds) -> bool:
    return (
        left["x"] < right["x"] + right["w"]
        and right["x"] < left["x"] + left["w"]
        and left["y"] < right["y"] + right["h"]
        and right["y"] < left["y"] + left["h"]
    )


def _contains(outer: Bounds, inner: Bounds) -> bool:
    return (
        outer["x"] <= inner["x"]
        and outer["y"] <= inner["y"]
        and outer["x"] + outer["w"] >= inner["x"] + inner["w"]
        and outer["y"] + outer["h"] >= inner["y"] + inner["h"]
    )


def resolve(
    raw_envelopes: list[dict[str, Any]], occlusion_envelopes: list[dict[str, Any]]
) -> dict[str, Any]:
    """Derive the union/hierarchy record directly from source envelopes.

    Sorting fixes output order only.  It never selects or discards a proposal;
    every source input appears exactly once in ``partition`` and overlap remains an
    explicit ambiguity relation.
    """
    return validate_resolution(
        _derive_resolution(raw_envelopes, occlusion_envelopes), raw_envelopes, occlusion_envelopes
    )


def validate_resolution(
    record: object, raw_envelopes: list[dict[str, Any]], occlusion_envelopes: list[dict[str, Any]]
) -> dict[str, Any]:
    """Refuse a derived record unless recomputing it from source gives exact bytes."""
    fields = {
        "schema",
        "page_id",
        "page_ordinal",
        "raw_proposal_refs",
        "occlusion_refs",
        "union_aabbs",
        "containment",
        "ambiguities",
        "partition",
    }
    checked = _closed(record, fields, "geometry resolution")
    if checked["schema"] != RESOLUTION_SCHEMA:
        raise SchemaRefusal("geometry resolution has unknown schema")
    # Compute source truth through the public resolver and compare all derived facts.
    # The private recomputation avoids accepting a restatement merely because its own
    # predicates agree with each other (R0 freeze-note derived-record pattern).
    expected = _derive_resolution(raw_envelopes, occlusion_envelopes)
    if checked != expected:
        raise SchemaRefusal(
            "geometry resolution diverges from its raw proposal and occlusion sources"
        )
    return checked


def _derive_resolution(
    raw_envelopes: list[dict[str, Any]], occlusion_envelopes: list[dict[str, Any]]
) -> dict[str, Any]:
    # Same source validation and derivation as resolve, deliberately factored here
    # so validation recomputes from source rather than trusting resolution fields.
    raws = []
    for item in raw_envelopes:
        envelope = validate_envelope(item)
        if envelope["stage"] != DESIGNATOR or envelope["kind"] != "raw-proposal":
            raise SchemaRefusal("resolver received a non-raw-proposal envelope")
        raws.append((envelope, validate_raw_proposal(envelope["payload"])))
    occlusions = []
    for item in occlusion_envelopes:
        envelope = validate_envelope(item)
        if envelope["stage"] != DESIGNATOR or envelope["kind"] != "occlusion":
            raise SchemaRefusal("resolver received a non-occlusion envelope")
        occlusions.append((envelope, validate_occlusion(envelope["payload"])))
    if not raws:
        raise SchemaRefusal("resolver has no raw proposal denominator")
    page = (raws[0][1]["page_id"], raws[0][1]["page_ordinal"])
    if any(
        (payload["page_id"], payload["page_ordinal"]) != page for _envelope, payload in raws
    ) or any(
        (payload["page_id"], payload["page_ordinal"]) != page for _envelope, payload in occlusions
    ):
        raise SchemaRefusal("resolver source records do not share one page lineage")
    # Page lineage (id + ordinal) is not the same fact as page pixel extent: two
    # raw proposals could in principle share lineage while declaring different
    # page_width_px/page_height_px. Pin one page extent and refuse a mismatch,
    # since every AABB and occlusion coordinate below is compared as if they all
    # shared this one pixel grid.
    page_w = raws[0][1]["page_transform"]["page_width_px"]
    page_h = raws[0][1]["page_transform"]["page_height_px"]
    if any(
        (payload["page_transform"]["page_width_px"], payload["page_transform"]["page_height_px"])
        != (page_w, page_h)
        for _envelope, payload in raws
    ):
        raise SchemaRefusal("resolver raw proposals do not share one page pixel extent")
    # validate_occlusion cannot check its own polygon against the page (it has no
    # page_transform); this is that deferred extent check. An occlusion outside
    # the shared page grid is refused rather than silently accepted as if it were
    # in-bounds geometry that could plausibly touch every proposal on the page.
    for _envelope, occlusion in occlusions:
        if any(
            not (0 <= point["x"] < page_w and 0 <= point["y"] < page_h)
            for point in occlusion["polygon"]
        ):
            raise SchemaRefusal("occlusion polygon falls outside the shared page extent")
    ordered = sorted(raws, key=lambda row: row[1]["proposal_id"])
    ids = [payload["proposal_id"] for _envelope, payload in ordered]
    if len(ids) != len(set(ids)):
        raise SchemaRefusal("resolver received duplicate raw proposal identities")
    containment, overlaps = [], []
    for index, (_envelope, left) in enumerate(ordered):
        for _other_envelope, right in ordered[index + 1 :]:
            if _contains(left["aabb"], right["aabb"]):
                containment.append({"outer": left["proposal_id"], "inner": right["proposal_id"]})
            elif _contains(right["aabb"], left["aabb"]):
                containment.append({"outer": right["proposal_id"], "inner": left["proposal_id"]})
            elif _overlap(left["aabb"], right["aabb"]):
                overlaps.append(
                    {
                        "left": left["proposal_id"],
                        "right": right["proposal_id"],
                        "state": "ambiguous-overlap",
                    }
                )
    # Disposition basis (recorded here, not just in the audit report, so the
    # decision travels with the code it governs): ANY occlusion on the page marks
    # EVERY proposal on that page "review", not only proposals whose AABB
    # geometrically intersects the occlusion polygon. This is deliberately
    # over-broad. GOALS 1 ranks a missed act above a poorly read one; Governance 1
    # treats extra review effort as an acceptable cost, never a reason to read
    # less carefully; and Architecture requires occlusions are "never silently
    # read past." A tight geometric filter would let an occlusion the resolver
    # itself misjudged (e.g. a coarse polygon, or ink actually extending past its
    # drawn edge) silently clear proposals it should have flagged. The page-wide
    # flag is intentionally conservative, decided by the R2 audit seat
    # (2026-08-16); a geometric-intersection narrowing is future work if the
    # review-queue cost is measured and found to matter, never a default.
    occlusion_ids = sorted(payload["occlusion_id"] for _envelope, payload in occlusions)
    return {
        "schema": RESOLUTION_SCHEMA,
        "page_id": page[0],
        "page_ordinal": page[1],
        "raw_proposal_refs": [
            {"artifact_id": envelope["artifact_id"], "self_hash": envelope["self_hash"]}
            for envelope, _payload in ordered
        ],
        "occlusion_refs": [
            {"artifact_id": envelope["artifact_id"], "self_hash": envelope["self_hash"]}
            for envelope, _payload in sorted(occlusions, key=lambda row: row[1]["occlusion_id"])
        ],
        "union_aabbs": [
            {"proposal_id": payload["proposal_id"], "aabb": payload["aabb"]}
            for _envelope, payload in ordered
        ],
        "containment": containment,
        "ambiguities": overlaps,
        "partition": [
            {
                "proposal_id": proposal_id,
                "disposition": "review" if occlusion_ids else "accepted-coverage",
                "occlusion_ids": occlusion_ids,
            }
            for proposal_id in ids
        ],
    }
