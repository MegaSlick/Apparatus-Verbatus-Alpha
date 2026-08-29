"""Derived witness facts remain closed, integer, and presentation-bound."""

import copy
from io import BytesIO

import pytest
from PIL import Image

from common.contracts.canonical import digest_bytes
from common.contracts.errors import SchemaRefusal
from common.imaging import crop_png
from common.native_witness import (
    partition_disagreement,
    unpresented_region_ids,
    validate_native_witness_geometry,
    validate_page_testimonium_payload,
    validate_partition_disagreement,
    validate_presented_page_binding,
    validate_reportable_observations,
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
    """A nested preference claim must not survive top-level schema closure."""
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


def _page_witness_act_view():
    """A page chair's act view: one crop presented, page-level geometry restated."""
    value = payload()
    value["page_witness"] = True
    value["presented"]["kind"] = "region"
    value["presented"]["region_ref"] = {"region_id": "rgn_0123456789abcdef"}
    value["presented"]["transform"].update(
        {"operation": "crop", "bounds": {"x": 10, "y": 10, "w": 20, "h": 20}}
    )
    return value


def test_a_page_witnesss_act_view_may_restate_geometry_outside_its_one_crop():
    """The chair saw the whole page; this record presents only one of its crops.

    Holding it to crop containment would refuse the honest page-space geometry
    the chair actually reported, which is what Unit 10C's coverage consumes.
    """
    value = _page_witness_act_view()
    value["observed"] = [
        {
            "ordinal": 0,
            "bounds": {"x": 0, "y": 0, "w": 5, "h": 5},
            "bounds_source": "native",
            "span": None,
        }
    ]

    assert validate_native_witness_geometry(value, page_size=(100, 80)) is value


def test_a_page_witnesss_act_view_is_still_bounded_by_the_sealed_page():
    """The relaxed wall is crop containment, never the sealed page itself."""
    value = _page_witness_act_view()
    value["observed"] = [
        {
            "ordinal": 0,
            "bounds": {"x": 0, "y": 0, "w": 101, "h": 5},
            "bounds_source": "native",
            "span": None,
        }
    ]

    with pytest.raises(SchemaRefusal, match="outside the sealed source page"):
        validate_native_witness_geometry(value, page_size=(100, 80))


def test_the_page_witness_relaxation_cannot_be_forged_onto_a_page_scoped_record():
    """`scope == "page"` presents the chair's complete view, so it keeps the wall."""
    value = _page_witness_act_view()
    value["scope"] = "page"
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


def test_an_adapter_crop_cannot_skip_re_derivation_by_withholding_its_page_bytes():
    """Otherwise the cheapest forgery is to omit the evidence that would refute it.

    The crop's digest is checked by re-cutting it from the sealed page. A caller
    that passes no page bytes must be refused, not quietly excused from the one
    check that binds an adapter-owned blob to the ink it claims to show.
    """
    value = payload()
    value["presented"]["kind"] = "adapter-crop"
    value["presented"]["transform"].update(
        {"operation": "crop", "bounds": {"x": 0, "y": 0, "w": 20, "h": 20}}
    )

    with pytest.raises(SchemaRefusal, match="cannot be re-derived without its sealed page bytes"):
        validate_presented_page_binding(
            value["presented"],
            page_ordinal=1,
            page_image_path=value["presented"]["image_path"],
            page_sha256="f" * 64,
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


def test_a_preference_refusal_names_the_testimonium_not_the_corpus_register():
    """The rule is shared; the subject of the refusal is not.

    An operator reading a witness record was told the *corpus register* may not
    express preference, because the check was reached through that module's
    private helper and its message named that module's record.
    """
    value = payload()
    value["preferred"] = True

    with pytest.raises(SchemaRefusal, match="a Testimonium may not express capture preference"):
        validate_native_witness_geometry(value)


@pytest.mark.parametrize(
    "region_payload",
    (
        {"transform": {"source_page_id": "page-1", "bounds": {"x": 0, "y": 0, "w": 5, "h": 5}}},
        {
            "region_id": "",
            "transform": {"source_page_id": "page-1", "bounds": {"x": 0, "y": 0, "w": 5, "h": 5}},
        },
        {"region_id": "r1", "transform": {"source_page_id": "page-1"}},
    ),
)
def test_a_proposal_region_with_no_comparable_identity_is_refused(region_payload):
    """Silence here would drop a crop from the disclosure list without a word.

    The list says which bound crops one presentation does not speak for. A
    region that cannot be compared must refuse, never be quietly omitted and
    read downstream as a crop the presentation covered.
    """
    with pytest.raises(SchemaRefusal, match="no page-space identity to compare"):
        unpresented_region_ids(payload()["presented"], [{"payload": region_payload}])


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
    value["partition_disagreement"] = partition_disagreement(
        {"artifact_id": "page-testimony", "payload": value}, []
    )
    with pytest.raises(SchemaRefusal, match="invalid page scope facts"):
        validate_page_testimonium_payload(value)


def _page_payload(**changes):
    """A complete, valid page Testimonium payload the closed contract accepts."""
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
            "page_role": "primary",
            "unjoined_act_attempts": [],
        }
    )
    value.update(changes)
    return value


def test_a_valid_page_testimonium_passes_its_closed_contract():
    """The refusals below must be about the change, not about the baseline."""
    value = _page_payload()

    assert validate_page_testimonium_payload(value) is value


def test_a_page_record_may_not_name_one_page_and_present_another():
    """Its observed boxes would be read as geometry from a page never shown."""
    value = _page_payload(page_ordinal=2)

    with pytest.raises(SchemaRefusal, match="names a different page than the record"):
        validate_page_testimonium_payload(value)


@pytest.mark.parametrize("ordinal", (0, -1))
def test_a_page_record_cannot_sit_below_the_first_sealed_page(ordinal):
    """Page ordinals are 1-based, so no sealed page could ever answer for these."""
    value = _page_payload(page_ordinal=ordinal)

    with pytest.raises(SchemaRefusal, match="invalid page scope facts"):
        validate_page_testimonium_payload(value)


def test_an_unpresented_page_record_still_needs_a_real_page_ordinal():
    """With no presentation to reconcile against, the bound is the only check."""
    value = _page_payload(page_ordinal=0, presented={}, observed=[])

    with pytest.raises(SchemaRefusal, match="invalid page scope facts"):
        validate_page_testimonium_payload(value)


@pytest.mark.parametrize(
    ("observed", "message"),
    (
        ("not-a-list", "not a list"),
        ([["ordinal", 0]], "not an object"),
        ([{"bounds_source": "native", "bounds": {"x": 0, "y": 0, "w": 1, "h": 1}}], "ordinal"),
        ([{"ordinal": True, "bounds_source": "native", "bounds": {}}], "ordinal"),
        ([{"ordinal": 0, "bounds_source": "vendor-guess", "bounds": {}}], "unknown bounds_source"),
        ([{"ordinal": 0, "bounds_source": "native"}], "page-pixel box"),
        (
            [{"ordinal": 0, "bounds_source": "derived", "bounds": {"x": 0, "y": 0}}],
            "page-pixel box",
        ),
    ),
)
def test_a_coverage_consumer_names_malformed_observations_instead_of_indexing_them(
    observed, message
):
    """A raw KeyError from the stage that decides recovery names no cause."""
    with pytest.raises(SchemaRefusal, match=message):
        validate_reportable_observations(observed)


def test_a_presented_echo_is_not_required_to_carry_a_box_a_consumer_never_reads():
    """Only reported geometry is measured; the echo is excluded before its box."""
    echo = [{"ordinal": 0, "bounds_source": "presented"}]

    assert validate_reportable_observations(echo) is echo


def test_partition_disagreement_retains_all_ambiguous_geometry_without_a_winner():
    testimony = {
        "artifact_id": "page-testimony",
        "payload": {
            "presented": {"source_page_id": "page-1"},
            "observed": [
                {
                    "ordinal": 0,
                    "bounds": {"x": 8, "y": 0, "w": 12, "h": 10},
                    "bounds_source": "native",
                }
            ],
        },
    }
    proposals = [
        {
            "payload": {
                "origin": "proposal",
                "transform": {
                    "source_page_id": "page-1",
                    "bounds": {"x": 0, "y": 0, "w": 10, "h": 10},
                },
            }
        },
        {
            "payload": {
                "origin": "proposal",
                "transform": {
                    "source_page_id": "page-1",
                    "bounds": {"x": 10, "y": 0, "w": 10, "h": 10},
                },
            }
        },
    ]
    disagreement = partition_disagreement(testimony, proposals)
    assert disagreement["ambiguous"] is True
    assert len(disagreement["boundary_deltas"]) == 2
    assert disagreement["ambiguous_pairings"] == disagreement["boundary_deltas"]
    assert disagreement["unclaimed_observations"] == []
    assert disagreement["overlap_rule"] == {"rule": "positive-area", "status": "unmeasured"}


def test_partition_disagreement_ties_from_the_proposal_side_too():
    """Ambiguity is symmetric when multiple observations claim one proposal."""
    testimony = {
        "artifact_id": "page-testimony",
        "payload": {
            "presented": {"source_page_id": "page-1"},
            "observed": [
                {
                    "ordinal": 0,
                    "bounds": {"x": 0, "y": 0, "w": 6, "h": 10},
                    "bounds_source": "native",
                },
                {
                    "ordinal": 1,
                    "bounds": {"x": 4, "y": 0, "w": 6, "h": 10},
                    "bounds_source": "native",
                },
            ],
        },
    }
    proposals = [
        {
            "payload": {
                "origin": "proposal",
                "transform": {
                    "source_page_id": "page-1",
                    "bounds": {"x": 0, "y": 0, "w": 10, "h": 10},
                },
            }
        }
    ]
    disagreement = partition_disagreement(testimony, proposals)
    assert disagreement["ambiguous"] is True
    assert len(disagreement["boundary_deltas"]) == 2
    assert disagreement["ambiguous_pairings"] == disagreement["boundary_deltas"]
    assert {pairing["observed_ordinal"] for pairing in disagreement["ambiguous_pairings"]} == {0, 1}


def test_page_testimonium_keeps_partition_facts_optional_in_the_record_shape():
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
            "page_role": "primary",
            "unjoined_act_attempts": [],
        }
    )
    assert validate_page_testimonium_payload(value) is value


def test_partition_disagreement_is_rederived_before_its_findings_can_trigger_recovery():
    testimony = {
        "artifact_id": "page-testimony",
        "payload": {
            "presented": {"source_page_id": "page-1"},
            "observed": [
                {
                    "ordinal": 0,
                    "bounds": {"x": 0, "y": 0, "w": 10, "h": 10},
                    "bounds_source": "native",
                }
            ],
        },
    }
    disagreement = partition_disagreement(testimony, [])
    validate_partition_disagreement(
        disagreement,
        observed=testimony["payload"]["observed"],
        source_page_id="page-1",
        testimonium_id="page-testimony",
        proposal_boxes=[],
    )

    malformed = copy.deepcopy(disagreement)
    malformed["unclaimed_observations"][0]["ordinal"] = 1
    with pytest.raises(SchemaRefusal, match="malformed unclaimed observation"):
        validate_partition_disagreement(
            malformed,
            observed=testimony["payload"]["observed"],
            source_page_id="page-1",
            testimonium_id="page-testimony",
            proposal_boxes=[],
        )

    contradictory = copy.deepcopy(disagreement)
    contradictory["observed_boxes"] = []
    with pytest.raises(SchemaRefusal, match="contradicts its observed geometry"):
        validate_partition_disagreement(
            contradictory,
            observed=testimony["payload"]["observed"],
            source_page_id="page-1",
            testimonium_id="page-testimony",
            proposal_boxes=[],
        )

    with pytest.raises(SchemaRefusal, match="sealed proposals"):
        validate_partition_disagreement(
            disagreement,
            observed=testimony["payload"]["observed"],
            source_page_id="page-1",
            testimonium_id="page-testimony",
            proposal_boxes=[{"x": 20, "y": 20, "w": 5, "h": 5}],
        )
