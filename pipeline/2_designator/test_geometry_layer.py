"""The record detector's geometry policy and proposals; no model calls or downloads."""

from __future__ import annotations

from copy import deepcopy

import pytest
from geometry_layer import (
    DEFAULT_POLICY_PATH,
    RESPONSE_BLOB_PREFIX,
    load_geometry_policy,
    load_geometry_policy_record,
    validate_raw_proposal,
    yolo_obb,
)

from common.contracts.errors import SchemaRefusal
from common.sealed_config import read_sealed_toml

RECEIPT = {"relative_path": "receipts/sha256/" + "a" * 64 + ".json", "sha256": "a" * 64}
RESPONSE = {"relative_path": RESPONSE_BLOB_PREFIX + "b" * 64, "sha256": "b" * 64}
PAGE_ID = "pg_fixture"
PAGE_ORDINAL = 0


def test_sealed_policy_exposes_the_yolo_rectification_toggle():
    policy = load_geometry_policy()
    # Must be the same raw bytes common/stage.py seals, or an unchanged file refuses.
    assert policy["config_sha256"] == read_sealed_toml(DEFAULT_POLICY_PATH, "geometry")[1]
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


def test_an_unreadable_geometry_policy_file_is_a_named_refusal_not_an_oserror(tmp_path):
    """A missing or unreadable sealed policy reaches the caller as a named
    refusal, never a bare OSError out of the middle of the loader."""
    with pytest.raises(SchemaRefusal, match="geometry policy"):
        load_geometry_policy(tmp_path / "absent.toml")


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


def test_an_enabled_rectify_toggle_is_refused_until_an_implementation_exists():
    """A sealed policy with rectify=true would publish mode="rectify" records
    for crops nothing rectified -- a false claim in published custody. The
    toggle fails closed until the implementation and its tests arrive."""
    policy = deepcopy(load_geometry_policy())
    policy["yolo_obb"]["rectify"] = True
    with pytest.raises(SchemaRefusal, match="rectification is not implemented"):
        load_geometry_policy_record(policy)


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
                "ordinal": 0,
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


def test_proposal_identity_is_reproducible_without_first_seen_observation_provenance():
    obb = [{"x": 10, "y": 20}, {"x": 20, "y": 10}, {"x": 30, "y": 20}, {"x": 20, "y": 30}]
    proposal = yolo_obb(
        page_id="pg_fixture",
        page_ordinal=0,
        page_w=100,
        page_h=100,
        policy=load_geometry_policy(),
        receipt_ref=RECEIPT,
        response_ref=RESPONSE,
        detections=[
            {"ordinal": 0, "obb": obb, "score_bp": 8000},
            {"ordinal": 1, "obb": obb, "score_bp": 8000},
        ],
    )[0]
    assert proposal["observed_ordinals"] == [0, 1]
    for ordinals in ([0], [1], [0, 1]):
        validate_raw_proposal({**proposal, "observed_ordinals": ordinals})

    with pytest.raises(SchemaRefusal, match="identity does not derive"):
        validate_raw_proposal({**proposal, "proposal_id": "proposal_forged000000"})


def test_raw_proposal_transform_refuses_a_non_identity_scale_in_page_pixel_space():
    """page-pixels-to-page-pixels can only be 1:1."""
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
                "ordinal": 0,
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


def test_empty_detections_lists_are_held_as_empty_not_an_error():
    policy = load_geometry_policy()
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


def test_degenerate_obb_with_only_three_distinct_corners_is_accepted():
    """A real detector may emit a duplicated corner (rounding collapse); that
    is still >= 3 distinct points, so it is held, not refused."""
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
                "ordinal": 0,
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
                "ordinal": 0,
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
                    "ordinal": 0,
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
        detections=[
            {"ordinal": 0, "obb": obb, "score_bp": 8000},
            {"ordinal": 1, "obb": obb, "score_bp": 8000},
        ],
    )
    assert len(yolo) == 1
    assert yolo[0]["observation_unit"] == "response-detection"
    assert yolo[0]["observed_ordinals"] == [0, 1]


def test_a_raw_proposal_must_name_what_its_observation_ordinals_count():
    """`[0, 1]` means two detections in one response; the record says what an
    ordinal counts, so a reader never has to guess."""
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
                "ordinal": 0,
                "obb": [
                    {"x": 10, "y": 20},
                    {"x": 20, "y": 10},
                    {"x": 30, "y": 20},
                    {"x": 20, "y": 30},
                ],
                "score_bp": 9000,
            }
        ],
    )[0]
    assert proposal["observation_unit"] == "response-detection"
    with pytest.raises(SchemaRefusal, match="what its observation ordinals count"):
        validate_raw_proposal({**proposal, "observation_unit": "pass"})
    with pytest.raises(SchemaRefusal, match="observed ordinals"):
        validate_raw_proposal({**proposal, "observed_ordinals": [1, 0]})


@pytest.mark.parametrize("ordinals", [[1, 1], [2, 1], [-1, 0], [True, 2]])
def test_detection_ordinals_must_increase_from_zero_or_above(ordinals):
    """Ordinals are indices into the retained response, so they cannot repeat,
    run backwards or be negative."""
    obb = [{"x": 10, "y": 20}, {"x": 20, "y": 10}, {"x": 30, "y": 20}, {"x": 20, "y": 30}]
    with pytest.raises(SchemaRefusal, match="ordinals must be increasing"):
        yolo_obb(
            page_id="pg_fixture",
            page_ordinal=0,
            page_w=100,
            page_h=100,
            policy=load_geometry_policy(),
            receipt_ref=RECEIPT,
            response_ref=RESPONSE,
            detections=[{"ordinal": ordinal, "obb": obb, "score_bp": 8000} for ordinal in ordinals],
        )


def test_a_proposal_keeps_the_source_ordinal_it_was_given():
    obb = [{"x": 10, "y": 20}, {"x": 20, "y": 10}, {"x": 30, "y": 20}, {"x": 20, "y": 30}]
    [proposal] = yolo_obb(
        page_id="pg_fixture",
        page_ordinal=0,
        page_w=100,
        page_h=100,
        policy=load_geometry_policy(),
        receipt_ref=RECEIPT,
        response_ref=RESPONSE,
        detections=[{"ordinal": 3, "obb": obb, "score_bp": 8000}],
    )
    assert proposal["observed_ordinals"] == [3]
