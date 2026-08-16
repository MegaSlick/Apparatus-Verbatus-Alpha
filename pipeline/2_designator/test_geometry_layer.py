"""Fixture-shaped R2 adapter and custody tests; no model calls or downloads."""

from __future__ import annotations

from itertools import cycle

import pytest
from geometry_layer import (
    DEFAULT_POLICY_PATH,
    chandra_layout,
    load_geometry_policy,
    occlusion_envelope,
    raw_proposal_envelope,
    read_retained_chandra_response,
    resolve,
    retain_chandra_response,
    surya_double_pass,
    validate_raw_proposal,
    validate_resolution,
    yolo_obb,
)

from common.contracts.canonical import digest_bytes
from common.contracts.errors import SchemaRefusal

RECEIPT = {"relative_path": "receipts/sha256/" + "a" * 64 + ".json", "sha256": "a" * 64}
RESPONSE = {"relative_path": "designator/blobs/sha256/" + "b" * 64, "sha256": "b" * 64}


def test_sealed_policy_exposes_integer_surya_sizing_and_yolo_rectification_toggle():
    policy = load_geometry_policy()
    assert policy["surya"]["tile_height_px"] == 1400
    assert policy["surya"]["half_tile_vertical_offset_px"] == 700
    assert policy["yolo_obb"] == {
        "role": "designator_yolo_obb",
        "task": "obb",
        "crop_policy": "aabb-enclose",
        "rectify": False,
    }


def test_unknown_geometry_policy_knob_is_refused(tmp_path):
    text = DEFAULT_POLICY_PATH.read_text(encoding="utf-8") + "\nunknown = true\n"
    path = tmp_path / "geometry.toml"
    path.write_text(text, encoding="utf-8")
    with pytest.raises(SchemaRefusal, match="closed schema"):
        load_geometry_policy(path)


def test_surya_runs_both_half_offset_tilings_and_unions_without_discarding_pass_evidence():
    policy = load_geometry_policy()
    calls = []

    def detect(tile):
        calls.append(tile)
        return [
            {"polygon": [{"x": 4, "y": 4}, {"x": 8, "y": 4}, {"x": 8, "y": 8}], "score_bp": 9000}
        ]

    proposals = surya_double_pass(
        page_id="pg_fixture",
        page_ordinal=0,
        page_w=100,
        page_h=2600,
        policy=policy,
        receipt_ref=RECEIPT,
        response_ref=RESPONSE,
        detect=detect,
    )
    assert [call["y"] for call in calls] == [0, 1400, 700, 2100]
    assert len(proposals) == 1
    assert proposals[0]["source"] == "surya"
    assert proposals[0]["observed_passes"] == [0, 1]
    validate_raw_proposal(proposals[0])


def test_yolo_retains_obb_and_derives_aabb_under_default_policy():
    proposals = yolo_obb(
        page_id="pg_fixture",
        page_ordinal=0,
        page_w=100,
        page_h=100,
        policy=load_geometry_policy(),
        receipt_ref=RECEIPT,
        response_ref=RESPONSE,
        detections=[
            {
                "obb": [
                    {"x": 10, "y": 20},
                    {"x": 20, "y": 10},
                    {"x": 30, "y": 20},
                    {"x": 20, "y": 30},
                ],
                "score_bp": 8000,
            }
        ],
    )
    assert proposals[0]["geometry_kind"] == "obb"
    assert proposals[0]["aabb"] == {"x": 10, "y": 10, "w": 21, "h": 21}
    assert proposals[0]["crop_policy"] == {"mode": "aabb-enclose", "loss_recorded": False}
    validate_raw_proposal(proposals[0])


def test_chandra_split_refuses_text_and_only_preserves_geometry_reference():
    with pytest.raises(SchemaRefusal, match="closed schema"):
        chandra_layout(
            page_id="pg_fixture",
            page_ordinal=0,
            page_w=100,
            page_h=100,
            config_sha256="c" * 64,
            receipt_ref=RECEIPT,
            response_ref=RESPONSE,
            regions=[{"bbox_1000": [0, 0, 1000, 1000], "score_bp": 9000, "text": "forbidden"}],
        )


class _FixtureTree:
    def __init__(self):
        self.blobs = {}
        self.receipts = []

    def put_blob(self, stage, data):
        digest = digest_bytes(data)
        path = f"{stage}/blobs/sha256/{digest}"
        self.blobs[path] = data
        return digest, type("Published", (), {"relative_path": path})()

    def read_run_receipt(self, reference):
        self.receipts.append(reference)
        return {"fixture": True}

    def read_bytes(self, path):
        return self.blobs[path]


def test_one_chandra_response_has_one_receipt_and_two_consumable_references():
    tree = _FixtureTree()
    body = b'{"regions":[{"text":"R3-only custody"}]}'
    response_ref = retain_chandra_response(tree, body, RECEIPT)
    assert response_ref["relative_path"] in tree.blobs
    assert read_retained_chandra_response(tree, response_ref, RECEIPT) == body
    assert tree.receipts == [RECEIPT]
    # Geometry itself contains only the sealed blob reference, never the response text.
    geometry = chandra_layout(
        page_id="pg_fixture",
        page_ordinal=0,
        page_w=100,
        page_h=100,
        config_sha256="c" * 64,
        receipt_ref=RECEIPT,
        response_ref=response_ref,
        regions=[{"bbox_1000": [0, 0, 500, 500], "score_bp": 9000}],
    )[0]
    assert "R3-only custody" not in str(geometry)


def test_chandra_custody_refuses_a_forged_blob_reference():
    tree = _FixtureTree()
    response_ref = retain_chandra_response(tree, b"fixture", RECEIPT)
    forged = {**response_ref, "sha256": "0" * 64}
    with pytest.raises(SchemaRefusal, match="differs"):
        read_retained_chandra_response(tree, forged, RECEIPT)


def _raw_envelope(payload):
    return raw_proposal_envelope(
        run_id="r2-fixture",
        subject_id=payload["proposal_id"],
        config_digest="d" * 64,
        adapter_revision="fixture-r2-v1",
        inputs=[],
        payload=payload,
    )


def _occlusion_envelope():
    return occlusion_envelope(
        run_id="r2-fixture",
        subject_id="occ_fixture",
        config_digest="d" * 64,
        adapter_revision="fixture-r2-v1",
        inputs=[],
        payload={
            "schema": "designator-occlusion.v1",
            "occlusion_id": "occ_fixture",
            "page_id": "pg_fixture",
            "page_ordinal": 0,
            "polygon": [{"x": 11, "y": 11}, {"x": 12, "y": 11}, {"x": 12, "y": 12}],
            "z_relationship": "unknown",
            "review_state": "open",
            "receipt_ref": RECEIPT,
        },
    )


def _geometry_sources():
    policy = load_geometry_policy()
    first = chandra_layout(
        page_id="pg_fixture",
        page_ordinal=0,
        page_w=100,
        page_h=100,
        config_sha256=policy["config_sha256"],
        receipt_ref=RECEIPT,
        response_ref=RESPONSE,
        regions=[{"bbox_1000": [100, 100, 500, 500], "score_bp": 9000}],
    )[0]
    second = yolo_obb(
        page_id="pg_fixture",
        page_ordinal=0,
        page_w=100,
        page_h=100,
        policy=policy,
        receipt_ref=RECEIPT,
        response_ref=RESPONSE,
        detections=[
            {
                "obb": [
                    {"x": 40, "y": 20},
                    {"x": 70, "y": 20},
                    {"x": 70, "y": 60},
                    {"x": 40, "y": 60},
                ],
                "score_bp": 8000,
            }
        ],
    )[0]
    return [_raw_envelope(first), _raw_envelope(second)]


def test_graduated_raw_proposal_produces_deterministic_additive_partition_without_occlusion():
    raw_sources = _geometry_sources()
    forward = resolve(raw_sources, [])
    reverse = resolve(list(reversed(raw_sources)), [])
    assert forward == reverse
    assert len(forward["union_aabbs"]) == 2
    assert {row["proposal_id"] for row in forward["partition"]} == {
        record["payload"]["proposal_id"] for record in raw_sources
    }
    assert {row["disposition"] for row in forward["partition"]} == {"accepted-coverage"}
    assert forward["ambiguities"], "overlap must be represented, not selected away"


def test_graduated_occlusion_produces_review_partition_and_is_not_silently_read_past():
    raw_sources = _geometry_sources()
    result = resolve(raw_sources, [_occlusion_envelope()])
    assert result["occlusion_refs"]
    assert {row["disposition"] for row in result["partition"]} == {"review"}
    assert {tuple(row["occlusion_ids"]) for row in result["partition"]} == {("occ_fixture",)}


def test_resolver_consumer_refuses_a_forged_derived_reference_instead_of_trusting_a_restatement():
    raw_sources = _geometry_sources()
    derived = resolve(raw_sources, [])
    derived["raw_proposal_refs"][0]["self_hash"] = "0" * 64
    with pytest.raises(SchemaRefusal, match="diverges"):
        validate_resolution(derived, raw_sources, [])


def test_resolver_consumer_refuses_a_raw_proposal_with_unsealed_extra_field():
    raw_sources = _geometry_sources()
    forged = dict(raw_sources[0])
    forged["payload"] = {**forged["payload"], "text": "must not cross geometry boundary"}
    with pytest.raises(SchemaRefusal, match="self-hash|closed schema"):
        resolve([forged, raw_sources[1]], [])


# --- BREAKER battery (Sonnet R2 adversarial test-authorship duty) ---------------


def test_surya_tiles_the_full_page_width_not_only_the_first_tile_column():
    """S2: a page wider than one sealed tile must still be scanned past x=1400."""
    policy = load_geometry_policy()
    calls = []

    def detect(tile):
        calls.append(tile)
        return []

    surya_double_pass(
        page_id="pg_wide",
        page_ordinal=0,
        page_w=3000,
        page_h=1400,
        policy=policy,
        receipt_ref=RECEIPT,
        response_ref=RESPONSE,
        detect=detect,
    )
    # Every (y, x) tile origin actually issued to the detector.
    origins = {(call["y"], call["x"]) for call in calls}
    assert (0, 0) in origins and (0, 1400) in origins and (0, 2800) in origins, (
        "Surya must tile across the full 3000px width, not just x=0..1400"
    )
    widths = {call["x"]: call["w"] for call in calls}
    assert widths[2800] == 200, "the last column tile must be clipped to the remaining width"


def test_surya_still_covers_a_narrow_page_with_a_single_column():
    policy = load_geometry_policy()
    calls = []

    def detect(tile):
        calls.append(tile)
        return []

    surya_double_pass(
        page_id="pg_narrow",
        page_ordinal=0,
        page_w=100,
        page_h=100,
        policy=policy,
        receipt_ref=RECEIPT,
        response_ref=RESPONSE,
        detect=detect,
    )
    assert {call["x"] for call in calls} == {0}
    assert {call["w"] for call in calls} == {100}


def test_surya_union_key_includes_score_so_a_rescored_repeat_is_retained_not_merged():
    """Union invariant: identical geometry at a different score across passes is
    two honest raw signals, never silently averaged or discarded."""
    policy = load_geometry_policy()
    polygon = [{"x": 4, "y": 4}, {"x": 8, "y": 4}, {"x": 8, "y": 8}]
    scores = cycle([9000, 7000])

    def detect(tile):
        return [{"polygon": polygon, "score_bp": next(scores)}]

    proposals = surya_double_pass(
        page_id="pg_fixture",
        page_ordinal=0,
        page_w=100,
        page_h=2600,  # tall enough that both offset-0 and offset-700 passes tile it
        policy=policy,
        receipt_ref=RECEIPT,
        response_ref=RESPONSE,
        detect=detect,
    )
    assert len(proposals) == 2, "a different score is a different raw observation, not a duplicate"
    assert {p["score_bp"] for p in proposals} == {9000, 7000}
    assert len({p["proposal_id"] for p in proposals}) == 2, (
        "each retained proposal keeps its own id"
    )


def test_raw_proposal_transform_refuses_a_non_identity_scale_in_page_pixel_space():
    """Adversarial transform: page-pixels-to-page-pixels can only be 1:1."""
    proposal = yolo_obb(
        page_id="pg_fixture",
        page_ordinal=0,
        page_w=100,
        page_h=100,
        policy=load_geometry_policy(),
        receipt_ref=RECEIPT,
        response_ref=RESPONSE,
        detections=[
            {
                "obb": [
                    {"x": 10, "y": 20},
                    {"x": 20, "y": 10},
                    {"x": 30, "y": 20},
                    {"x": 20, "y": 30},
                ],
                "score_bp": 8000,
            }
        ],
    )[0]
    forged = {
        **proposal,
        "page_transform": {
            **proposal["page_transform"],
            "scale_x": {"numerator": 2, "denominator": 1},
        },
    }
    with pytest.raises(SchemaRefusal, match="non-identity"):
        validate_raw_proposal(forged)


def test_resolver_refuses_an_occlusion_polygon_outside_the_shared_page_extent():
    """S3 (extent half): validate_occlusion cannot see the page; the resolver must."""
    raw_sources = _geometry_sources()  # 100x100 page
    out_of_bounds = occlusion_envelope(
        run_id="r2-fixture",
        subject_id="occ_out_of_bounds",
        config_digest="d" * 64,
        adapter_revision="fixture-r2-v1",
        inputs=[],
        payload={
            "schema": "designator-occlusion.v1",
            "occlusion_id": "occ_out_of_bounds",
            "page_id": "pg_fixture",
            "page_ordinal": 0,
            "polygon": [{"x": 500, "y": 500}, {"x": 501, "y": 500}, {"x": 501, "y": 501}],
            "z_relationship": "unknown",
            "review_state": "open",
            "receipt_ref": RECEIPT,
        },
    )
    with pytest.raises(SchemaRefusal, match="outside the shared page extent"):
        resolve(raw_sources, [out_of_bounds])


def test_resolver_refuses_raw_proposals_with_mismatched_page_pixel_extent():
    """Page lineage (id+ordinal) is not page pixel extent; both must be pinned."""
    policy = load_geometry_policy()
    small = yolo_obb(
        page_id="pg_shared",
        page_ordinal=0,
        page_w=100,
        page_h=100,
        policy=policy,
        receipt_ref=RECEIPT,
        response_ref=RESPONSE,
        detections=[
            {
                "obb": [
                    {"x": 10, "y": 20},
                    {"x": 20, "y": 10},
                    {"x": 30, "y": 20},
                    {"x": 20, "y": 30},
                ],
                "score_bp": 8000,
            }
        ],
    )[0]
    big = chandra_layout(
        page_id="pg_shared",
        page_ordinal=0,
        page_w=200,
        page_h=200,
        config_sha256=policy["config_sha256"],
        receipt_ref=RECEIPT,
        response_ref=RESPONSE,
        regions=[{"bbox_1000": [0, 0, 500, 500], "score_bp": 9000}],
    )[0]
    with pytest.raises(SchemaRefusal, match="page pixel extent"):
        resolve([_raw_envelope(small), _raw_envelope(big)], [])
