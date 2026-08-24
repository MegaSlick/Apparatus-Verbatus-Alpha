"""Non-tautological refusals for Unit 10B's derived witness waist."""

import copy
from io import BytesIO

import pytest
from PIL import Image

from common.contracts.canonical import digest_bytes
from common.contracts.errors import SchemaRefusal
from common.imaging import crop_png
from common.native_witness import (
    unpresented_region_ids,
    validate_native_witness_geometry,
    validate_page_testimonium_payload,
    validate_presented_page_binding,
)


def payload():
    return {
        # The retained text an observed span addresses. Both kinds store it
        # under `payload`; a span may not name an offset it cannot answer.
        "payload": "SYNTHETIC ACT ONE",
        "presented": {
            "kind": "page",
            "source_page_id": "page-1",
            "source_page_ordinal": 1,
            "image_path": "1_exemplar/blobs/sha256/" + "0" * 64,
            "image_sha256": "0" * 64,
            "transform": {
                "operation": "whole",
                "source_page_id": "page-1",
                "source_page_ordinal": 1,
                "bounds": {"x": 0, "y": 0, "w": 100, "h": 80},
            },
        },
        "observed": [
            {
                "ordinal": 0,
                "bounds": {"x": 0, "y": 0, "w": 100, "h": 80},
                "bounds_source": "presented",
                "span": {"start": 0, "end": 4},
            }
        ],
    }


@pytest.mark.parametrize(
    ("change", "message"),
    [
        (lambda value: value["observed"][0].update({"ordinal": 1}), "dense"),
        (lambda value: value["observed"][0]["bounds"].update({"x": 1}), "differs"),
        (
            lambda value: (
                value["observed"][0].update({"bounds_source": "native"}),
                value["observed"][0]["bounds"].update({"w": 101}),
            ),
            "outside",
        ),
        (
            lambda value: (
                value["observed"][0].update({"bounds_source": "native"}),
                value["observed"][0]["bounds"].update({"x": 1.5}),
            ),
            "float",
        ),
        (lambda value: value["observed"][0].update({"confidence": "high"}), "closed schema"),
        (lambda value: value.update({"preferred": True}), "preference"),
        (lambda value: value["presented"].update({"region_ref": {"region_id": "r"}}), "closed"),
        (
            lambda value: value["observed"][0].update({"bounds_source": "vendor-estimated"}),
            "unknown bounds_source",
        ),
        (
            lambda value: value["presented"]["transform"].update({"extra": "field"}),
            "complete page transform",
        ),
        (lambda value: value["observed"][0].update({"preferred": True}), "preference"),
    ],
)
def test_closed_native_geometry_refuses_each_invalid_derived_fact(change, message):
    value = payload()
    change(value)
    with pytest.raises(SchemaRefusal, match=message):
        validate_native_witness_geometry(value, page_size=(100, 80))


@pytest.mark.parametrize(("field", "value"), [("kind", []), ("bounds_source", {})])
def test_unhashable_enum_values_are_named_refusals_not_python_tracebacks(field, value):
    record = payload()
    if field == "kind":
        record["presented"][field] = value
    else:
        record["observed"][0][field] = value
    with pytest.raises(SchemaRefusal, match="unknown"):
        validate_native_witness_geometry(record, page_size=(100, 80))


def test_overlapping_spans_are_refused_after_each_entry_independently_validates():
    value = payload()
    second = copy.deepcopy(value["observed"][0])
    second.update({"ordinal": 1, "span": {"start": 3, "end": 6}})
    value["observed"].append(second)
    with pytest.raises(SchemaRefusal, match="overlap"):
        validate_native_witness_geometry(value, page_size=(100, 80))


def test_region_ref_is_allowed_only_for_region_presentation():
    value = payload()
    value["presented"]["kind"] = "region"
    value["presented"]["region_ref"] = {"region_id": "rgn_0123456789abcdef"}
    assert validate_native_witness_geometry(value, page_size=(100, 80)) is value


def test_an_explicitly_unpresented_witness_records_no_observations():
    value = {"presented": {}, "observed": []}
    assert validate_native_witness_geometry(value) is value

    value["observed"] = [payload()["observed"][0]]
    with pytest.raises(SchemaRefusal, match="unpresented"):
        validate_native_witness_geometry(value)


def test_a_span_past_the_end_of_the_retained_text_is_refused():
    """A span is an address into text this record actually holds. Shape alone
    validated `{"start": 0, "end": 10_000}` over a seventeen-character reading:
    a consumer resolving it either crashes or silently quotes less than the
    witness said, and neither is a retained report (GOALS 5)."""
    value = payload()
    value["observed"][0]["span"] = {"start": 0, "end": len(value["payload"]) + 1}
    with pytest.raises(SchemaRefusal, match="runs past the end"):
        validate_native_witness_geometry(value, page_size=(100, 80))
    value["observed"][0]["span"] = {"start": 0, "end": len(value["payload"])}
    assert validate_native_witness_geometry(value, page_size=(100, 80)) is value


def test_a_span_on_a_record_that_retains_no_text_is_refused():
    """A structured native payload (or a failed attempt) retains no string for a
    span to index, so the honest span is `null`, not an offset into nothing."""
    value = payload()
    value["payload"] = {"lines": ["structured native output"]}
    with pytest.raises(SchemaRefusal, match="does not retain"):
        validate_native_witness_geometry(value, page_size=(100, 80))
    value["observed"][0]["span"] = None
    assert validate_native_witness_geometry(value, page_size=(100, 80)) is value


def test_a_confusable_bounds_source_is_not_the_declared_enum_member():
    """`nativ\u0435` is Cyrillic \u0435, renders as "native", and is a different
    string. Nothing in this path case-folds or confusable-folds an enum member,
    and NFC (which `common/alignment.py` applies to text) does not map Cyrillic
    onto Latin, so the refusal holds rather than resting on nobody trying."""
    value = payload()
    value["observed"][0].update(
        {"bounds_source": "nativ\u0435", "bounds": {"x": 0, "y": 0, "w": 100, "h": 80}}
    )
    with pytest.raises(SchemaRefusal, match="unknown bounds_source"):
        validate_native_witness_geometry(value, page_size=(100, 80))


def test_a_confusable_field_name_is_an_unknown_field_not_the_field_it_resembles():
    value = payload()
    value["observed"][0]["\u0455pan"] = {"start": 0, "end": 1}  # Cyrillic dze
    with pytest.raises(SchemaRefusal, match="closed schema"):
        validate_native_witness_geometry(value, page_size=(100, 80))


def test_a_preference_key_nested_deep_inside_a_transform_is_still_refused():
    """`_refuse_preference` is reused verbatim from the corpus register and is
    recursive; nothing tested that at depth, and a preference claim buried in a
    sub-object is exactly where one would survive a top-level-only check."""
    value = payload()
    value["presented"]["transform"]["bounds"] = {
        "x": 0,
        "y": 0,
        "w": 100,
        "h": 80,
    }
    value["presented"]["transform"]["operation"] = {"name": "whole", "preferred": True}
    with pytest.raises(SchemaRefusal, match="preference"):
        validate_native_witness_geometry(value, page_size=(100, 80))


def test_degenerate_corner_boxes_satisfy_the_schema_and_stay_witness_geometry():
    """Ordinal-dense one-pixel boxes in a corner are schema-compliant, and that
    is the correct answer here: this waist validates shape, not plausibility. The
    routing rule is what must not read them as coverage, which
    `pipeline/4_perlector/test_native_observation.py` proves it does not."""
    value = payload()
    value["observed"] = [
        {
            "ordinal": index,
            "bounds": {"x": 0, "y": 0, "w": 1, "h": 1},
            "bounds_source": "native",
            "span": None,
        }
        for index in range(4)
    ]
    assert validate_native_witness_geometry(value, page_size=(100, 80)) is value


def test_an_observed_box_cannot_claim_pixels_outside_the_exact_presented_image():
    """Page-space coordinates still have to fall inside the image the chair saw.
    Page bounds alone admitted a native box wholly outside a region presentation,
    attributing pixels to a witness that its own record said were never shown."""
    value = payload()
    value["presented"]["kind"] = "region"
    value["presented"]["region_ref"] = {"region_id": "rgn_0123456789abcdef"}
    value["presented"]["transform"].update(
        {"operation": "crop", "bounds": {"x": 10, "y": 10, "w": 20, "h": 20}}
    )
    value["observed"] = [
        {
            "ordinal": 0,
            "bounds": {"x": 0, "y": 0, "w": 5, "h": 5},
            "bounds_source": "native",
            "span": None,
        }
    ]
    with pytest.raises(SchemaRefusal, match="outside the exact image presentation"):
        validate_native_witness_geometry(value, page_size=(100, 80))


def test_a_page_presentation_may_not_name_another_page_s_blob():
    """The forgery this wall exists for: a self-consistent record naming page 1
    while carrying page 2's real, digest-bound pixels. Its boxes would then be
    checked against page 1's dimensions and read as page 1 geometry."""
    value = payload()
    other_page_blob = "1_exemplar/blobs/sha256/" + "1" * 64
    value["presented"].update({"image_path": other_page_blob, "image_sha256": "1" * 64})
    with pytest.raises(SchemaRefusal, match="not the sealed page it claims"):
        validate_presented_page_binding(
            value["presented"],
            page_ordinal=1,
            page_image_path="1_exemplar/blobs/sha256/" + "0" * 64,
            page_sha256="0" * 64,
            page_size=(100, 80),
        )


def test_a_page_presentation_that_is_only_part_of_its_page_is_an_adapter_crop():
    value = payload()
    value["presented"]["transform"]["bounds"] = {"x": 0, "y": 0, "w": 50, "h": 80}
    with pytest.raises(SchemaRefusal, match="does not cover its whole sealed page"):
        validate_presented_page_binding(
            value["presented"],
            page_ordinal=1,
            page_image_path=value["presented"]["image_path"],
            page_sha256=value["presented"]["image_sha256"],
            page_size=(100, 80),
        )


def test_an_adapter_crop_carrying_the_whole_page_blob_may_not_claim_a_sub_page_crop():
    value = payload()
    value["presented"]["kind"] = "adapter-crop"
    value["presented"]["transform"]["bounds"] = {"x": 10, "y": 10, "w": 20, "h": 20}
    with pytest.raises(SchemaRefusal, match="whole sealed page's blob under a sub-page"):
        validate_presented_page_binding(
            value["presented"],
            page_ordinal=1,
            page_image_path=value["presented"]["image_path"],
            page_sha256=value["presented"]["image_sha256"],
            page_size=(100, 80),
        )


def test_an_adapter_crop_blob_must_rederive_from_its_sealed_page_recipe():
    buffer = BytesIO()
    Image.new("RGB", (100, 80), "white").save(buffer, format="PNG")
    page_bytes = buffer.getvalue()
    bounds = {"x": 10, "y": 10, "w": 20, "h": 20}
    crop = crop_png(page_bytes, bounds)
    value = payload()
    value["presented"].update(
        {
            "kind": "adapter-crop",
            "image_path": "3_attestatores/blobs/sha256/" + digest_bytes(crop),
            "image_sha256": digest_bytes(crop),
        }
    )
    value["presented"]["transform"].update({"operation": "crop", "bounds": bounds})
    validate_presented_page_binding(
        value["presented"],
        page_ordinal=1,
        page_image_path="1_exemplar/blobs/sha256/" + digest_bytes(page_bytes),
        page_sha256=digest_bytes(page_bytes),
        page_size=(100, 80),
        page_bytes=page_bytes,
    )

    value["presented"]["image_sha256"] = "f" * 64
    with pytest.raises(SchemaRefusal, match="does not re-derive"):
        validate_presented_page_binding(
            value["presented"],
            page_ordinal=1,
            page_image_path="1_exemplar/blobs/sha256/" + digest_bytes(page_bytes),
            page_sha256=digest_bytes(page_bytes),
            page_size=(100, 80),
            page_bytes=page_bytes,
        )


def test_a_page_presentation_that_matches_its_sealed_page_passes():
    value = payload()
    validate_presented_page_binding(
        value["presented"],
        page_ordinal=1,
        page_image_path=value["presented"]["image_path"],
        page_sha256=value["presented"]["image_sha256"],
        page_size=(100, 80),
    )


def test_a_page_presentation_cannot_relabel_its_sealed_page_ordinal():
    value = payload()
    value["presented"]["source_page_ordinal"] = 2
    value["presented"]["transform"]["source_page_ordinal"] = 2
    with pytest.raises(SchemaRefusal, match="ordinal disagrees"):
        validate_presented_page_binding(
            value["presented"],
            page_ordinal=1,
            page_image_path=value["presented"]["image_path"],
            page_sha256=value["presented"]["image_sha256"],
            page_size=(100, 80),
        )


def test_a_page_presentation_requires_the_executable_whole_operation():
    value = payload()
    value["presented"]["transform"]["operation"] = "crop"
    with pytest.raises(SchemaRefusal, match="whole-page transform"):
        validate_presented_page_binding(
            value["presented"],
            page_ordinal=1,
            page_image_path=value["presented"]["image_path"],
            page_sha256=value["presented"]["image_sha256"],
            page_size=(100, 80),
        )


def test_unpresented_regions_are_derived_from_containment_for_every_presentation_kind():
    regions = [
        {
            "payload": {
                "region_id": "r1",
                "transform": {
                    "source_page_id": "page-1",
                    "bounds": {"x": 10, "y": 10, "w": 20, "h": 20},
                },
            }
        },
        {
            "payload": {
                "region_id": "r2",
                "transform": {
                    "source_page_id": "page-2",
                    "bounds": {"x": 0, "y": 0, "w": 20, "h": 20},
                },
            }
        },
    ]
    presented = payload()["presented"]
    presented["kind"] = "adapter-crop"
    presented["transform"].update(
        {"operation": "crop", "bounds": {"x": 0, "y": 0, "w": 40, "h": 40}}
    )
    assert unpresented_region_ids(presented, regions) == ["r2"]
    assert unpresented_region_ids({}, regions) == []


def test_page_payload_closure_is_shared_with_the_consumer_and_refuses_unhashable_roles():
    value = payload()
    value.update(
        {
            "chair": "attestator_1",
            "act_key": "page-1",
            "attempt_ordinal": 1,
            "regions": [],
            "provenance": {},
            "format_capabilities": {},
            "witness_reported": None,
            "content_health": {},
            "unpresented_regions": [],
            "scope": "page",
            "page_ordinal": 1,
            "page_role": [],
            "unjoined_act_attempts": [],
        }
    )
    with pytest.raises(SchemaRefusal, match="invalid page scope facts"):
        validate_page_testimonium_payload(value)
