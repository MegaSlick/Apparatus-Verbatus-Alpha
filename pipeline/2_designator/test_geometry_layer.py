"""Fixture-shaped R2 adapter and custody tests; no model calls or downloads."""

from __future__ import annotations

from copy import deepcopy

import pytest
from geometry_layer import (
    DEFAULT_POLICY_PATH,
    chandra_layout,
    load_geometry_policy,
    load_geometry_policy_record,
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

# A polygon inside the vertical band both Surya passes tile (the offset-0 pass
# covers y=0..1400, the offset-700 pass covers y=700..2100), so a physically
# honest detector reports it from one tile in each pass.
BOTH_PASS_POLYGON = [{"x": 4, "y": 804}, {"x": 8, "y": 804}, {"x": 8, "y": 808}]


def _detector_that_only_sees_its_own_tile(polygon, score=lambda tile: 9000):
    """A detector reports page-absolute geometry, and only for tiles containing it.

    The adapter now holds `detect` to exactly this. A fixture that returned one
    page-absolute polygon from every tile described a detector that cannot
    exist -- and it was the only reason the union looked exercised across the two
    passes.
    """

    def detect(tile):
        far_x, far_y = tile["x"] + tile["w"], tile["y"] + tile["h"]
        if all(
            tile["x"] <= point["x"] < far_x and tile["y"] <= point["y"] < far_y for point in polygon
        ):
            return [{"polygon": polygon, "score_bp": score(tile)}]
        return []

    return detect


def test_sealed_policy_exposes_integer_surya_sizing_and_yolo_rectification_toggle():
    policy = load_geometry_policy()
    assert policy["surya"]["tile_height_px"] == 1400
    assert policy["surya"]["half_tile_vertical_offset_px"] == 700
    assert policy["surya"]["horizontal_overlap_px"] == 700
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


def test_unknown_top_level_geometry_policy_table_is_refused(tmp_path):
    text = DEFAULT_POLICY_PATH.read_text(encoding="utf-8") + "\n[unread]\nvalue = 1\n"
    path = tmp_path / "geometry.toml"
    path.write_text(text, encoding="utf-8")
    with pytest.raises(SchemaRefusal, match="closed schema"):
        load_geometry_policy(path)


@pytest.mark.parametrize(
    "field_path",
    [
        ("schema",),
        ("surya", "role"),
        ("surya", "tile_width_px"),
        ("surya", "tile_height_px"),
        ("surya", "half_tile_vertical_offset_px"),
        ("surya", "horizontal_overlap_px"),
        ("yolo_obb", "role"),
        ("yolo_obb", "task"),
        ("yolo_obb", "crop_policy"),
        ("yolo_obb", "rectify"),
        ("provenance", "source"),
        ("provenance", "calibrated_for_this_corpus"),
        ("provenance", "caveat"),
    ],
)
def test_point_of_use_policy_refuses_every_missing_sealed_value(field_path):
    policy = deepcopy(load_geometry_policy())
    parent = policy
    for field in field_path[:-1]:
        parent = parent[field]
    del parent[field_path[-1]]
    with pytest.raises(SchemaRefusal, match="closed"):
        load_geometry_policy_record(policy)


def test_surya_runs_both_half_offset_tilings_and_unions_without_discarding_pass_evidence():
    policy = load_geometry_policy()
    calls = []
    seeing = _detector_that_only_sees_its_own_tile(BOTH_PASS_POLYGON)

    def detect(tile):
        calls.append(tile)
        return seeing(tile)

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
    # One physical detection in the band both passes tile: seen once per pass and
    # unioned, with both pass ordinals retained under their declared unit.
    assert proposals[0]["observation_unit"] == "surya-tiling-pass"
    assert proposals[0]["observed_ordinals"] == [0, 1]
    validate_raw_proposal(proposals[0])


def test_surya_horizontal_overlap_recovers_a_detection_cut_by_the_original_width_seam():
    """V1: union cannot reconstruct partial boxes, so one tile must see the whole detection."""
    policy = load_geometry_policy()
    target = [{"x": 1390, "y": 100}, {"x": 1410, "y": 100}, {"x": 1410, "y": 120}]

    def detect(tile):
        x1, y1 = tile["x"] + tile["w"], tile["y"] + tile["h"]
        if all(tile["x"] <= point["x"] < x1 and tile["y"] <= point["y"] < y1 for point in target):
            return [{"polygon": target, "score_bp": 9000}]
        return []

    proposals = surya_double_pass(
        page_id="pg_width_seam",
        page_ordinal=0,
        page_w=3000,
        page_h=1400,
        policy=policy,
        receipt_ref=RECEIPT,
        response_ref=RESPONSE,
        detect=detect,
    )
    assert len(proposals) == 1
    assert proposals[0]["geometry"] == target


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
        # `RunTree.read_bytes` is `self.resolve(path).read_bytes()`, so a
        # reference to a blob that is not there surfaces as OSError, not KeyError.
        if path not in self.blobs:
            raise FileNotFoundError(path)
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


def test_chandra_custody_names_a_missing_blob_instead_of_crashing_on_it():
    """A retained response that is gone is a custody refusal, not a bare OSError.

    `RunTree.read_run_receipt`, one line above the read this covers, already
    wraps the same failure for the same reason: a reference that no longer
    resolves is a provenance failure, and R3 must be able to tell it apart from a
    crash in the reader.
    """
    tree = _FixtureTree()
    response_ref = retain_chandra_response(tree, b"fixture", RECEIPT)
    tree.blobs.clear()  # the run tree lost the blob the reference still names
    with pytest.raises(SchemaRefusal, match="could not be read"):
        read_retained_chandra_response(tree, response_ref, RECEIPT)


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


def test_page_wide_occlusion_disposition_includes_in_bounds_nonintersecting_geometry():
    raw_sources = _geometry_sources()
    distant = occlusion_envelope(
        run_id="r2-fixture",
        subject_id="occ_distant",
        config_digest="d" * 64,
        adapter_revision="fixture-r2-v1",
        inputs=[],
        payload={
            "schema": "designator-occlusion.v1",
            "occlusion_id": "occ_distant",
            "page_id": "pg_fixture",
            "page_ordinal": 0,
            "polygon": [{"x": 90, "y": 90}, {"x": 91, "y": 90}, {"x": 91, "y": 91}],
            "z_relationship": "unknown",
            "review_state": "open",
            "receipt_ref": RECEIPT,
        },
    )
    result = resolve(raw_sources, [distant])
    assert {row["disposition"] for row in result["partition"]} == {"review"}
    assert {tuple(row["occlusion_ids"]) for row in result["partition"]} == {("occ_distant",)}


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
    detect = _detector_that_only_sees_its_own_tile(
        BOTH_PASS_POLYGON, score=lambda tile: 9000 if tile["y"] == 0 else 7000
    )

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


def test_proposal_identity_is_reproducible_without_first_seen_pass_provenance():
    proposal = surya_double_pass(
        page_id="pg_fixture",
        page_ordinal=0,
        page_w=100,
        page_h=2600,
        policy=load_geometry_policy(),
        receipt_ref=RECEIPT,
        response_ref=RESPONSE,
        detect=_detector_that_only_sees_its_own_tile(BOTH_PASS_POLYGON),
    )[0]
    assert proposal["observed_ordinals"] == [0, 1]
    for ordinals in ([0], [1], [0, 1]):
        validate_raw_proposal({**proposal, "observed_ordinals": ordinals})

    with pytest.raises(SchemaRefusal, match="identity does not derive"):
        validate_raw_proposal({**proposal, "proposal_id": "proposal_forged000000"})


def test_content_identity_unions_exact_duplicate_single_call_detections():
    policy = load_geometry_policy()
    obb = [
        {"x": 10, "y": 20},
        {"x": 20, "y": 10},
        {"x": 30, "y": 20},
        {"x": 20, "y": 30},
    ]
    yolo = yolo_obb(
        page_id="pg_fixture",
        page_ordinal=0,
        page_w=100,
        page_h=100,
        policy=policy,
        receipt_ref=RECEIPT,
        response_ref=RESPONSE,
        detections=[{"obb": obb, "score_bp": 8000}, {"obb": obb, "score_bp": 8000}],
    )
    assert len(yolo) == 1
    assert yolo[0]["observation_unit"] == "response-detection"
    assert yolo[0]["observed_ordinals"] == [0, 1]

    region = {"bbox_1000": [100, 100, 500, 500], "score_bp": 9000}
    chandra = chandra_layout(
        page_id="pg_fixture",
        page_ordinal=0,
        page_w=100,
        page_h=100,
        config_sha256=policy["config_sha256"],
        receipt_ref=RECEIPT,
        response_ref=RESPONSE,
        regions=[region, region],
    )
    assert len(chandra) == 1
    assert chandra[0]["observation_unit"] == "response-detection"
    assert chandra[0]["observed_ordinals"] == [0, 1]


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


def test_resolver_refuses_two_occlusions_sharing_one_identity():
    """The resolver refuses colliding raw proposal ids; occlusions are the same fact.

    `occlusion_ids` goes into every partition row, so an id counted twice tells a
    reviewer that two separate obstructions bear on every proposal on the page
    when only one was ever recorded.
    """
    raw_sources = _geometry_sources()
    first = _occlusion_envelope()
    second = occlusion_envelope(
        run_id="r2-fixture",
        subject_id="occ_fixture",  # the same identity as `first`
        config_digest="d" * 64,
        adapter_revision="fixture-r2-v1",
        inputs=[],
        payload={
            "schema": "designator-occlusion.v1",
            "occlusion_id": "occ_fixture",
            "page_id": "pg_fixture",
            "page_ordinal": 0,
            "polygon": [{"x": 40, "y": 40}, {"x": 41, "y": 40}, {"x": 41, "y": 41}],
            "z_relationship": "unknown",
            "review_state": "open",
            "receipt_ref": RECEIPT,
        },
    )
    assert first["payload"]["polygon"] != second["payload"]["polygon"]
    with pytest.raises(SchemaRefusal, match="duplicate occlusion identities"):
        resolve(raw_sources, [first, second])


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


def test_resolve_does_not_mutate_its_inputs_across_the_internal_double_derivation():
    """S4: resolve() calls _derive_resolution twice (once directly, once inside
    validate_resolution). Confirm the second derivation sees the same untouched
    inputs -- no TOCTOU between the two passes."""
    raw_sources = _geometry_sources()
    occlusions = [_occlusion_envelope()]
    before_raw = [dict(item) for item in raw_sources]
    before_occ = [dict(item) for item in occlusions]
    first = resolve(raw_sources, occlusions)
    second = resolve(raw_sources, occlusions)
    assert first == second
    assert raw_sources == before_raw
    assert occlusions == before_occ


def test_empty_detections_lists_are_held_as_empty_not_an_error():
    policy = load_geometry_policy()
    assert (
        surya_double_pass(
            page_id="pg_fixture",
            page_ordinal=0,
            page_w=100,
            page_h=100,
            policy=policy,
            receipt_ref=RECEIPT,
            response_ref=RESPONSE,
            detect=lambda tile: [],
        )
        == []
    )
    assert (
        yolo_obb(
            page_id="pg_fixture",
            page_ordinal=0,
            page_w=100,
            page_h=100,
            policy=policy,
            receipt_ref=RECEIPT,
            response_ref=RESPONSE,
            detections=[],
        )
        == []
    )


def test_resolver_refuses_a_page_with_zero_raw_proposals_rather_than_an_empty_partition():
    """Recorded verdict, not a fix: a page with no raw proposal source has no
    denominator for the resolver to partition at all, so it fails closed instead
    of silently returning an empty coverage record."""
    with pytest.raises(SchemaRefusal, match="no raw proposal denominator"):
        resolve([], [])


def test_three_point_collinear_polygon_is_accepted_as_a_thin_one_pixel_region():
    """Breaker battery: a degenerate 3-point line does not crash the pipeline;
    the enclosing AABB's half-open-edge rule gives it a real, non-empty crop."""
    policy = load_geometry_policy()
    collinear = [{"x": 10, "y": 10}, {"x": 20, "y": 10}, {"x": 30, "y": 10}]  # one horizontal line

    proposals = surya_double_pass(
        page_id="pg_fixture",
        page_ordinal=0,
        page_w=100,
        page_h=100,
        policy=policy,
        receipt_ref=RECEIPT,
        response_ref=RESPONSE,
        detect=_detector_that_only_sees_its_own_tile(collinear, score=lambda tile: 5000),
    )
    assert proposals[0]["aabb"] == {"x": 10, "y": 10, "w": 21, "h": 1}
    validate_raw_proposal(proposals[0])


def test_degenerate_obb_with_only_three_distinct_corners_is_accepted():
    """OBB requires exactly four corner points structurally; a real detector may
    still emit a duplicated corner (rounding collapse). That is still >= 3
    distinct points, so it is held, not refused."""
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
                    {"x": 10, "y": 10},
                    {"x": 10, "y": 10},  # duplicate corner: degenerate but not refused
                    {"x": 20, "y": 10},
                    {"x": 30, "y": 10},
                ],
                "score_bp": 5000,
            }
        ],
    )
    assert proposal[0]["aabb"] == {"x": 10, "y": 10, "w": 21, "h": 1}


def test_unicode_page_id_round_trips_through_adapter_and_resolver():
    policy = load_geometry_policy()
    page_id = "página_église_日本"  # accented + CJK
    proposal = chandra_layout(
        page_id=page_id,
        page_ordinal=0,
        page_w=100,
        page_h=100,
        config_sha256=policy["config_sha256"],
        receipt_ref=RECEIPT,
        response_ref=RESPONSE,
        regions=[{"bbox_1000": [0, 0, 500, 500], "score_bp": 9000}],
    )[0]
    assert proposal["page_id"] == page_id
    resolved = resolve([_raw_envelope(proposal)], [])
    assert resolved["page_id"] == page_id


@pytest.mark.parametrize("score_bp", [0, 10_000])
def test_score_bp_boundary_values_are_accepted(score_bp):
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
                "score_bp": score_bp,
            }
        ],
    )[0]
    assert proposal["score_bp"] == score_bp


@pytest.mark.parametrize("score_bp", [-1, 10_001])
def test_score_bp_boundary_values_are_refused(score_bp):
    with pytest.raises(SchemaRefusal, match="score"):
        yolo_obb(
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
                    "score_bp": score_bp,
                }
            ],
        )


def test_chandra_bbox_at_a_one_pixel_page_collapses_to_too_few_distinct_points_and_is_refused():
    """S5 breaker battery, x1=1000/page_w=1 edge: the floor-left and ceil-right
    formulas both round to the page's only column, so a full-range bbox on a
    degenerate 1px page yields fewer than three distinct corners. That is a
    correct fail-closed refusal (GOVERNANCE 2: never silently accepted as a real
    crop), not a coverage loss -- a 1px page is not a real corpus case."""
    policy = load_geometry_policy()
    with pytest.raises(SchemaRefusal, match="fewer than three distinct points"):
        chandra_layout(
            page_id="pg_tiny",
            page_ordinal=0,
            page_w=1,
            page_h=1,
            config_sha256=policy["config_sha256"],
            receipt_ref=RECEIPT,
            response_ref=RESPONSE,
            regions=[{"bbox_1000": [0, 0, 1000, 1000], "score_bp": 9000}],
        )


def test_chandra_bbox_rounding_never_inverts_for_a_thin_real_region():
    """A genuinely thin but non-degenerate region (a header rule, a signature
    line) on a realistically sized page: right edge must never land left of the
    left edge after the ceil-based rounding, and the crop must be non-empty."""
    policy = load_geometry_policy()
    thin = chandra_layout(
        page_id="pg_fixture",
        page_ordinal=0,
        page_w=2000,
        page_h=2000,
        config_sha256=policy["config_sha256"],
        receipt_ref=RECEIPT,
        response_ref=RESPONSE,
        regions=[{"bbox_1000": [500, 500, 501, 999], "score_bp": 9000}],
    )
    aabb = thin[0]["aabb"]
    assert aabb["w"] >= 1 and aabb["h"] >= 1, "no inverted or zero-area crop for a thin real region"

    # x1 pinned at the maximum normalized coordinate on an ordinary page: the
    # ceil-based right edge must land exactly at the page's last column, not
    # past it and not before the left edge.
    full_width = chandra_layout(
        page_id="pg_fixture",
        page_ordinal=0,
        page_w=2000,
        page_h=2000,
        config_sha256=policy["config_sha256"],
        receipt_ref=RECEIPT,
        response_ref=RESPONSE,
        regions=[{"bbox_1000": [0, 0, 1000, 1000], "score_bp": 9000}],
    )[0]
    assert full_width["aabb"] == {"x": 0, "y": 0, "w": 2000, "h": 2000}


def test_a_raw_proposal_must_name_what_its_observation_ordinals_count():
    """`[0, 1]` means two tiling passes for Surya and two detections in one
    response for YOLO/Chandra; the record says which rather than leaving a reader
    to guess from the source name."""
    proposal = chandra_layout(
        page_id="pg_fixture",
        page_ordinal=0,
        page_w=100,
        page_h=100,
        config_sha256="c" * 64,
        receipt_ref=RECEIPT,
        response_ref=RESPONSE,
        regions=[{"bbox_1000": [0, 0, 500, 500], "score_bp": 9000}],
    )[0]
    assert proposal["observation_unit"] == "response-detection"
    with pytest.raises(SchemaRefusal, match="what its observation ordinals count"):
        validate_raw_proposal({**proposal, "observation_unit": "pass"})
    with pytest.raises(SchemaRefusal, match="observed ordinals"):
        validate_raw_proposal({**proposal, "observed_ordinals": [1, 0]})


def test_occlusion_polygon_answers_the_same_shape_question_as_proposal_geometry():
    """One degenerate point repeated three times is not page geometry, and the two
    validators may not disagree about that."""
    with pytest.raises(SchemaRefusal, match="fewer than three distinct points"):
        occlusion_envelope(
            run_id="r2-fixture",
            subject_id="occ_degenerate",
            config_digest="d" * 64,
            adapter_revision="fixture-r2-v1",
            inputs=[],
            payload={
                "schema": "designator-occlusion.v1",
                "occlusion_id": "occ_degenerate",
                "page_id": "pg_fixture",
                "page_ordinal": 0,
                "polygon": [{"x": 11, "y": 11}, {"x": 11, "y": 11}, {"x": 11, "y": 11}],
                "z_relationship": "unknown",
                "review_state": "open",
                "receipt_ref": RECEIPT,
            },
        )


def test_surya_refuses_tile_local_coordinates_instead_of_placing_ink_on_the_wrong_region():
    """The union across overlapping tiles assumes page-absolute detector output.

    A tile-local polygon is indistinguishable from a page-absolute one by shape
    alone, so without this refusal a live adapter that forgot the tile origin
    would file every proposal at the wrong place on the page, silently, and one
    physical detection seen from several tiles would be retained several times at
    several wrong places instead of unioning into one.
    """
    policy = load_geometry_policy()
    line = [{"x": 800, "y": 100}, {"x": 1500, "y": 100}, {"x": 1500, "y": 120}]

    def detect_tile_local(tile):
        far_x, far_y = tile["x"] + tile["w"], tile["y"] + tile["h"]
        if not all(
            tile["x"] <= point["x"] < far_x and tile["y"] <= point["y"] < far_y for point in line
        ):
            return []
        return [
            {
                "polygon": [
                    {"x": point["x"] - tile["x"], "y": point["y"] - tile["y"]} for point in line
                ],
                "score_bp": 9000,
            }
        ]

    with pytest.raises(SchemaRefusal, match="outside the tile it was issued for"):
        surya_double_pass(
            page_id="pg_local",
            page_ordinal=0,
            page_w=3000,
            page_h=1400,
            policy=policy,
            receipt_ref=RECEIPT,
            response_ref=RESPONSE,
            detect=detect_tile_local,
        )


def test_a_detection_wider_than_the_overlap_keeps_its_complete_sighting_and_its_fragments():
    """Full-width tiling, horizontal overlap, and the union key, read together.

    A detection wider than the sealed 700px overlap cannot fit inside every tile
    that touches it, so overlapping tiles clip it differently and each clipping is
    retained as its own raw proposal. That is honest retention, not double
    counting: one tile still saw the whole detection, and the resolver publishes
    the complete sighting as containing each fragment rather than choosing
    between them. No coverage denominator is inflated -- the residual-ink check
    counts page pixels, never proposals (U13).
    """
    policy = load_geometry_policy()
    line = [
        {"x": 800, "y": 100},
        {"x": 1500, "y": 100},
        {"x": 1500, "y": 120},
        {"x": 800, "y": 120},
    ]

    def detect_clipping(tile):
        far_x, far_y = tile["x"] + tile["w"], tile["y"] + tile["h"]
        if not any(
            tile["x"] <= point["x"] < far_x and tile["y"] <= point["y"] < far_y for point in line
        ):
            return []
        clipped = [
            {
                "x": min(max(point["x"], tile["x"]), far_x - 1),
                "y": min(max(point["y"], tile["y"]), far_y - 1),
            }
            for point in line
        ]
        if len({(point["x"], point["y"]) for point in clipped}) < 3:
            return []
        return [{"polygon": clipped, "score_bp": 9000}]

    proposals = surya_double_pass(
        page_id="pg_straddle",
        page_ordinal=0,
        page_w=3000,
        page_h=1400,
        policy=policy,
        receipt_ref=RECEIPT,
        response_ref=RESPONSE,
        detect=detect_clipping,
    )
    spans = {(row["aabb"]["x"], row["aabb"]["x"] + row["aabb"]["w"]) for row in proposals}
    assert (800, 1501) in spans, "the tile spanning x=700..2100 saw the whole detection"
    assert (800, 1400) in spans and (1400, 1501) in spans, "clipped fragments are retained too"

    resolved = resolve([_raw_envelope(row) for row in proposals], [])
    complete = next(row["proposal_id"] for row in proposals if row["aabb"]["w"] == 701)
    contained = {row["inner"] for row in resolved["containment"] if row["outer"] == complete}
    assert contained == {row["proposal_id"] for row in proposals} - {complete}, (
        "every fragment is published as contained by the complete sighting"
    )
    assert len(resolved["partition"]) == len(proposals), "invariant 8: one row per proposal"
    assert {row["disposition"] for row in resolved["partition"]} == {"accepted-coverage"}


def test_two_proposals_with_the_same_box_are_an_ambiguity_not_an_invented_hierarchy():
    """Coincident AABBs each 'contain' the other, so publishing one as the outer
    one invents a parent-child relation out of the proposal_id sort order."""
    policy = load_geometry_policy()
    proposals = surya_double_pass(
        page_id="pg_coincident",
        page_ordinal=0,
        page_w=100,
        page_h=2600,
        policy=policy,
        receipt_ref=RECEIPT,
        response_ref=RESPONSE,
        detect=_detector_that_only_sees_its_own_tile(
            BOTH_PASS_POLYGON, score=lambda tile: 9000 if tile["y"] == 0 else 7000
        ),
    )
    assert len(proposals) == 2
    assert proposals[0]["aabb"] == proposals[1]["aabb"]

    envelopes = [_raw_envelope(row) for row in proposals]
    resolved = resolve(envelopes, [])
    assert resolved["containment"] == [], "equal boxes are not a hierarchy"
    assert [row["state"] for row in resolved["ambiguities"]] == ["coincident-aabb"]
    assert {resolved["ambiguities"][0]["left"], resolved["ambiguities"][0]["right"]} == {
        row["proposal_id"] for row in proposals
    }
    assert resolve(list(reversed(envelopes)), []) == resolved

    # A strictly larger box still contains each of them: containment survives as a
    # relation, it just stops being asserted between equals.
    bigger = yolo_obb(
        page_id="pg_coincident",
        page_ordinal=0,
        page_w=100,
        page_h=2600,
        policy=policy,
        receipt_ref=RECEIPT,
        response_ref=RESPONSE,
        detections=[
            {
                "obb": [
                    {"x": 0, "y": 800},
                    {"x": 20, "y": 800},
                    {"x": 20, "y": 820},
                    {"x": 0, "y": 820},
                ],
                "score_bp": 8000,
            }
        ],
    )[0]
    with_outer = resolve(envelopes + [_raw_envelope(bigger)], [])
    assert {row["inner"] for row in with_outer["containment"]} == {
        row["proposal_id"] for row in proposals
    }
    assert {row["outer"] for row in with_outer["containment"]} == {bigger["proposal_id"]}
