"""Derived witness facts remain closed, integer, and presentation-bound."""

import copy
from io import BytesIO

import pytest
from PIL import Image

from common.contracts.canonical import digest_bytes
from common.contracts.errors import SchemaRefusal
from common.contracts.serving import STOP_REASON_UNREPORTED
from common.imaging import crop_png
from common.native_witness import (
    CHURRO_MAX_RESPONSE_BYTES,
    CHURRO_PARSERS,
    CHURRO_RESPONSE_NOT_BYTES,
    derive_churro_capture,
    parse_churro_response,
    partition_disagreement,
    unpresented_region_ids,
    validate_churro_xml,
    validate_native_capture,
    validate_native_witness_geometry,
    validate_page_testimonium_payload,
    validate_partition_disagreement,
    validate_presented,
    validate_presented_page_binding,
    validate_reportable_observations,
    validate_retained_response_refs,
    verify_native_capture_bytes,
)


def _native_capture() -> dict:
    return {
        "schema": "attestatores-model-view.v1",
        "adapter": "churro.v1",
        "view": {
            "prompt": {"system": "system prompt", "user": "user prompt"},
            "generation": {"max_new_tokens": 24_000},
        },
        "raw_response_ref": {
            "relative_path": "3_attestatores/blobs/sha256/" + "a" * 64,
            "sha256": "a" * 64,
        },
        "transport_stop_reason": "stop",
        "stop_reason": "stop",
        "findings": [],
        "parse": {"state": "parsed", "parser": "xml", "text": "read"},
    }


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


def test_page_retained_response_reference_rechecks_the_blob_bytes():
    raw = b"retained native response"
    digest = digest_bytes(raw)
    reference = {"relative_path": f"3_attestatores/blobs/sha256/{digest}", "sha256": digest}
    validate_retained_response_refs(
        {"raw_response_refs": [reference]}, read_bytes=lambda _path: raw
    )
    with pytest.raises(SchemaRefusal, match="differs from its digest"):
        validate_retained_response_refs(
            {"raw_response_refs": [reference]}, read_bytes=lambda _path: b"changed"
        )
    with pytest.raises(SchemaRefusal, match="could not be read"):
        validate_retained_response_refs(
            {"raw_response_refs": [reference]},
            read_bytes=lambda _path: (_ for _ in ()).throw(FileNotFoundError("gone")),
        )


def test_page_retained_response_digest_is_lowercase_hex():
    reference = {
        "relative_path": "3_attestatores/blobs/sha256/" + "z" * 64,
        "sha256": "z" * 64,
    }
    with pytest.raises(SchemaRefusal, match="closed blob reference"):
        validate_retained_response_refs({"raw_response_refs": [reference]})


def test_page_retained_response_digest_shape_is_the_module_wide_one():
    """An uppercase digest is refused although its path is derived from it.

    The path check alone cannot see this: `relative_path` and `sha256` agree
    with each other. Only the digest-shape rule refuses it, and this seam and
    the native-capture seam below must spell that rule the same way, or one of
    them starts binding a blob identity the other refuses.
    """
    digest = "A" * 64
    reference = {"relative_path": f"3_attestatores/blobs/sha256/{digest}", "sha256": digest}
    with pytest.raises(SchemaRefusal, match="closed blob reference"):
        validate_retained_response_refs({"raw_response_refs": [reference]})


def test_page_retained_response_path_is_derived_from_its_digest():
    reference = {
        "relative_path": "3_attestatores/blobs/sha256/" + "a" * 64,
        "sha256": "b" * 64,
    }
    with pytest.raises(SchemaRefusal, match="closed blob reference"):
        validate_retained_response_refs({"raw_response_refs": [reference]})


def test_the_public_page_seam_actually_reaches_the_retained_response_check():
    """The three tests above call the checker directly and pass either way.

    `validate_page_testimonium_payload` is the only caller in the pipeline, and
    the Recensor hands it `read_bytes` precisely so the retained bytes are
    re-hashed against their recorded digests. A stray `return` above that call
    made the whole check unreachable while the unit tests stayed green: the
    record then reported a verified retained response nobody had verified
    (GOVERNANCE 10). This case goes through the public function, so the dead
    path fails loudly rather than quietly.
    """
    raw = b"retained native response"
    digest = digest_bytes(raw)
    reference = {"relative_path": f"3_attestatores/blobs/sha256/{digest}", "sha256": digest}

    value = _page_payload(raw_response_refs=[reference])
    assert validate_page_testimonium_payload(value, read_bytes=lambda _path: raw) is value

    with pytest.raises(SchemaRefusal, match="differs from its digest"):
        validate_page_testimonium_payload(
            _page_payload(raw_response_refs=[reference]), read_bytes=lambda _path: b"changed"
        )

    # The shape checks reach the seam too, not only the byte recheck: a caller
    # that passes no `read_bytes` still gets the closed reference contract.
    with pytest.raises(SchemaRefusal, match="closed blob reference"):
        validate_page_testimonium_payload(
            _page_payload(
                raw_response_refs=[
                    {"relative_path": "3_attestatores/blobs/sha256/" + "a" * 64, "sha256": "b" * 64}
                ]
            )
        )


def _page_with_churro_capture() -> dict:
    value = payload()
    text = value["payload"]
    digest = digest_bytes(f"<output>{text}</output>".encode())
    value.update(
        {
            "chair": "attestator_1",
            "act_key": "page-1",
            "attempt_ordinal": 1,
            "regions": [],
            "provenance": {},
            "format_capabilities": {},
            "witness_reported": None,
            "content_health": {
                "native_type": "string",
                "encoding": "utf-8-json-native",
                "recordable": True,
                "empty": False,
                "blank": False,
                "truncated": False,
                "characters": len(text),
                "truncation_basis": "trusted-response-boundary",
            },
            "unpresented_regions": [],
            "scope": "page",
            "page_ordinal": 1,
            "page_role": "primary",
            "unjoined_act_attempts": [],
            "native_capture": {
                "schema": "attestatores-model-view.v1",
                "adapter": "churro.v1",
                "view": {
                    "prompt": {"system": "system prompt", "user": "user prompt"},
                    "generation": {"max_new_tokens": 24_000},
                },
                "raw_response_ref": {
                    "relative_path": f"3_attestatores/blobs/sha256/{digest}",
                    "sha256": digest,
                },
                "transport_stop_reason": "eos",
                "stop_reason": "eos",
                "findings": [],
                "parse": {"state": "parsed", "parser": "xml", "text": text},
            },
        }
    )
    return value


@pytest.mark.parametrize(
    ("change", "message"),
    [
        (
            lambda value: value["native_capture"]["raw_response_ref"].update(
                relative_path="3_attestatores/blobs/sha256/not-the-digest"
            ),
            "content-addressed",
        ),
        (
            # Path and digest agree here, so only the digest-shape rule refuses
            # it. The retained-response seam above spells the same rule.
            #
            # That rule has its own message since the shared reference shape was
            # given `is_sha256`: this uppercase digest is refused at the shape,
            # before the path-agreement check whose wording the row above
            # matches. What is pinned is unchanged -- uppercase is not this
            # pipeline's lowercase-hex digest -- under the refusal that decides
            # it.
            lambda value: value["native_capture"]["raw_response_ref"].update(
                relative_path=f"3_attestatores/blobs/sha256/{'A' * 64}", sha256="A" * 64
            ),
            "invalid raw-response reference",
        ),
        (
            lambda value: value["native_capture"].update(schema="attestatores-model-view.v9"),
            "unknown retained model-view schema",
        ),
        (
            lambda value: value["native_capture"]["view"].update(unread="ignored"),
            "exactly its prompt and generation",
        ),
        (
            lambda value: value["native_capture"]["view"]["generation"].update(
                max_new_tokens=10**100
            ),
            "24000-token bound",
        ),
        (
            lambda value: value["native_capture"].update(stop_reason="partial-parse-failed"),
            "disagrees with its parse and findings",
        ),
        (
            lambda value: value["native_capture"].update(
                parse={"state": "pending", "parser": "xml"}
            ),
            "terminal parse record",
        ),
        (
            # A parser this chair has no branch for. Unit 12 widened the
            # admissible parser names from `{"xml"}` to `CHURRO_PARSERS`; it did
            # not open them.
            lambda value: value["native_capture"].update(
                parse={"state": "parsed", "parser": "json", "text": "x"}
            ),
            "terminal parse record",
        ),
        (
            # `unrecognized-shape` is coupled to the live `churro` parser. Under
            # `xml` it is unreachable by construction -- `validate_churro_xml`
            # returns text or raises -- so a record claiming it there is forged.
            lambda value: value["native_capture"].update(
                parse={"state": "unrecognized-shape", "parser": "xml", "outcome": "invalid-json"},
                stop_reason="partial-parse-unrecognized-shape",
            ),
            "cannot reach one",
        ),
        (
            lambda value: value["native_capture"]["findings"].extend(
                [
                    {
                        "kind": "post-hoc-repetition-uninspected",
                        "reason": "first",
                        "inspected": "raw-response",
                    },
                    {
                        "kind": "post-hoc-repetition-uninspected",
                        "reason": "second",
                        "inspected": "raw-response",
                    },
                ]
            ),
            "more than one repetition finding",
        ),
        (
            lambda value: value["native_capture"]["parse"].update(text="different"),
            "payload differs",
        ),
        (
            lambda value: value["content_health"].update(characters=0),
            "health differs",
        ),
    ],
)
def test_churro_native_capture_closes_its_evidence_reference_and_derived_facts(change, message):
    value = _page_with_churro_capture()
    change(value)
    with pytest.raises(SchemaRefusal, match=message):
        validate_page_testimonium_payload(value)


def test_a_cut_off_empty_churro_capture_is_recordable_but_not_a_reported_absence():
    value = _page_with_churro_capture()
    value["payload"] = ""
    value["content_health"].update(
        empty=True,
        blank=True,
        truncated=True,
        characters=0,
    )
    value["native_capture"]["transport_stop_reason"] = "length"
    value["native_capture"]["stop_reason"] = "length"
    value["native_capture"]["parse"]["text"] = ""
    value["observed"][0]["span"] = None
    value["reason"] = "provider stopped at length; a cut-off response is not a confirmed blank"

    assert validate_page_testimonium_payload(value) is value

    # The `reported` projection is retired; the closed schema refuses the key
    # itself, so a claimed absence has no field left to ride in on.
    value["reported"] = ""
    with pytest.raises(SchemaRefusal, match="not its closed schema"):
        validate_page_testimonium_payload(value)


def test_churro_capture_derivation_is_checked_against_its_authoritative_raw_bytes():
    value = _page_with_churro_capture()
    raw = f"<output>{value['payload']}</output>".encode()
    assert verify_native_capture_bytes(value["native_capture"], raw) is value["native_capture"]

    value["native_capture"]["parse"]["text"] = "a coherently resealed false projection"
    with pytest.raises(SchemaRefusal, match="parse.*retained raw response"):
        verify_native_capture_bytes(value["native_capture"], raw)


def test_a_churro_capture_admits_the_live_unreported_stop_reason():
    """U5: a live page-scoped chair (Churro) whose wire response carried no
    `finish_reason` at all retains `STOP_REASON_UNREPORTED`
    (`common/contracts/serving.py`) as its transport word -- distinct from
    every fixture/engine word already in `_CHURRO_STOP_REASONS`, and it must
    be admitted here or every such live response would fail this check by
    name alone, indistinguishably from a genuinely unknown transport word."""
    value = _native_capture()
    value["transport_stop_reason"] = STOP_REASON_UNREPORTED
    value["stop_reason"] = STOP_REASON_UNREPORTED
    assert validate_native_capture(value) is value


def test_an_unreported_churro_boundary_publishes_unknown_truncation_not_false():
    """U8, the HANDOFF's third owed gap: three states, not two.

    The shared page contract re-derived a Churro page record's health from its
    capture by asking one question -- "is this word a cut-off word" -- and an
    engine that reported *nothing* answered it "no", which the record then
    published as `truncated: false`: a completed boundary nobody observed
    (GOVERNANCE 10). The live boundary measures three states, and this is the
    third: unknown, said so in the basis. Before the fix this payload was
    refused by name, so a live Churro chair whose wire carried no
    `finish_reason` could not publish a page record at all.
    """

    value = _page_with_churro_capture()
    value["native_capture"]["transport_stop_reason"] = STOP_REASON_UNREPORTED
    value["native_capture"]["stop_reason"] = STOP_REASON_UNREPORTED
    value["content_health"].update(truncated=None, truncation_basis="not-recorded")

    assert validate_page_testimonium_payload(value) is value

    # And the two-valued answer it replaces is now refused: `false` here is a
    # claim about a boundary the engine never reported.
    value["content_health"].update(truncated=False, truncation_basis="trusted-response-boundary")
    with pytest.raises(SchemaRefusal, match="health differs"):
        validate_page_testimonium_payload(value)


def test_an_unreported_boundary_over_an_empty_churro_page_is_not_a_confirmed_blank():
    """The same guard the cut-off case already had, for the same reason.

    An empty reading may be published as a measured absence only when the
    boundary positively said the model finished. "Cut off" and "never said"
    both fail that test, so both owe the record a failed-attempt reason.
    """

    value = _page_with_churro_capture()
    value["payload"] = ""
    value["native_capture"]["transport_stop_reason"] = STOP_REASON_UNREPORTED
    value["native_capture"]["stop_reason"] = STOP_REASON_UNREPORTED
    value["native_capture"]["parse"]["text"] = ""
    value["observed"][0]["span"] = None
    value["content_health"].update(
        empty=True, blank=True, truncated=None, characters=0, truncation_basis="not-recorded"
    )
    value["reason"] = "the provider's stop boundary was never confirmed complete"

    assert validate_page_testimonium_payload(value) is value

    del value["reason"]
    with pytest.raises(SchemaRefusal, match="no failed-attempt reason"):
        validate_page_testimonium_payload(value)


def test_a_reported_natural_stop_is_unchanged_by_the_third_state():
    """The two measured states still reconcile exactly as they did.

    The widening adds a third answer; it must not move either of the two a
    record already written under the old rule carries.
    """

    complete = _page_with_churro_capture()
    assert complete["native_capture"]["transport_stop_reason"] == "eos"
    assert complete["content_health"]["truncated"] is False
    assert validate_page_testimonium_payload(complete) is complete

    cut_off = _page_with_churro_capture()
    cut_off["native_capture"]["transport_stop_reason"] = "length"
    cut_off["native_capture"]["stop_reason"] = "length"
    cut_off["content_health"].update(truncated=True)
    assert validate_page_testimonium_payload(cut_off) is cut_off


def test_a_capture_may_record_a_shape_its_parser_ran_over_and_could_not_place():
    """U8, the HANDOFF's fourth owed gap: `unrecognized-shape` is admitted.

    `pipeline/3_attestatores/chandra.py` has produced this state since it was
    written -- the vendor publishes no response specimen, so a real Chandra
    body is a named surprise rather than a parse failure -- and
    `feeding.retain_model_view` records it. The shared contract had no room for
    it, so the retained model view of the one state a live Chandra response
    actually reaches could be attached to no record at all: the bytes stayed,
    the adapter's own account of them was dropped.
    """

    value = _native_capture()
    value["adapter"] = "chandra.v1"
    value["view"] = {"prompt": {"instruction": "Transcribe this complete page."}}
    value["transport_stop_reason"] = "stop"
    value["stop_reason"] = "partial-parse-unrecognized-shape"
    value["parse"] = {
        "state": "unrecognized-shape",
        "parser": "json",
        "outcome": "unverified-response-schema",
    }

    assert validate_native_capture(value) is value


@pytest.mark.parametrize(
    "parse",
    (
        # The state must name the shape it could not place, in `outcome`.
        {"state": "unrecognized-shape", "parser": "json"},
        {"state": "unrecognized-shape", "parser": "json", "outcome": ""},
        {"state": "unrecognized-shape", "parser": "json", "outcome": None},
        # And it may not borrow another state's third field: three states,
        # three distinct pieces of evidence, so a record cannot wear one
        # state's clothing while carrying another's.
        {"state": "unrecognized-shape", "parser": "json", "reason": "nope"},
        {"state": "unrecognized-shape", "parser": "json", "text": "smuggled"},
        {"state": "parsed", "parser": "json", "outcome": "unverified-response-schema"},
    ),
)
def test_an_unrecognized_shape_capture_must_name_the_shape_and_nothing_else(parse):
    value = _native_capture()
    value["adapter"] = "chandra.v1"
    value["view"] = {"prompt": {"instruction": "Transcribe this complete page."}}
    value["stop_reason"] = "partial-parse-unrecognized-shape"
    value["parse"] = parse
    with pytest.raises(SchemaRefusal, match="wrong shape|without naming it"):
        validate_native_capture(value)


def test_a_churro_capture_still_refuses_a_genuinely_unknown_stop_reason():
    value = _native_capture()
    value["transport_stop_reason"] = "abort"
    value["stop_reason"] = "abort"
    with pytest.raises(SchemaRefusal, match="unknown transport stop reason"):
        validate_native_capture(value)


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


@pytest.mark.parametrize(
    "reference",
    (
        {"relative_path": "3_attestatores/blobs/sha256/deadbeef"},
        {"relative_path": "3_attestatores/blobs/sha256/deadbeef", "sha256": None},
        "not a reference at all",
        None,
    ),
)
def test_a_page_edge_finding_refuses_a_malformed_retained_reference_by_name(reference):
    """Its provenance check indexed the reference before proving it was one.

    A rejected box is traced to the bytes that produced it by matching its
    `response_sha256` against the page record's retained references. Read
    straight through, a reference that is not a mapping, or carries no string
    digest, escaped this validator as a bare `KeyError` or `TypeError` -- and a
    traceback out of a provenance contract is not the named refusal it owes the
    reader who has to act on it.
    """
    raw = b"retained native response"
    digest = digest_bytes(raw)
    overshoots = [
        {
            "kind": "page-edge-overshoot",
            "response_sha256": digest,
            "ordinal": 1,
            "bounds": {"x": 0, "y": 0, "w": 201, "h": 260},
            "sealed_page_bounds": {"x": 0, "y": 0, "w": 200, "h": 260},
        }
    ]
    value = _page_payload(raw_response_refs=[reference])
    value["partition_disagreement"] = partition_disagreement(
        {"artifact_id": "page-testimonium", "payload": value},
        [],
        page_edge_overshoots=overshoots,
    )

    with pytest.raises(SchemaRefusal, match="malformed retained response reference"):
        validate_page_testimonium_payload(
            value, testimonium_id="page-testimonium", read_bytes=lambda _path: raw
        )


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
    """Optional means both shapes pass, so both shapes are asserted here."""
    without_partition = _page_payload()
    assert "partition_disagreement" not in without_partition
    assert validate_page_testimonium_payload(without_partition) is without_partition

    with_partition = _page_payload()
    with_partition["partition_disagreement"] = partition_disagreement(
        {"artifact_id": "page-testimony", "payload": with_partition}, []
    )
    assert validate_page_testimonium_payload(with_partition) is with_partition


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


def _resized_presentation():
    value = payload()
    value["presented"].update({"kind": "adapter-crop", "image_sha256": "b" * 64})
    value["presented"]["image_path"] = "3_attestatores/blobs/sha256/" + "b" * 64
    value["presented"]["transform"].update(
        {
            "operation": "crop-resize-preserve-aspect",
            "bounds": {"x": 0, "y": 0, "w": 40, "h": 20},
            "resize": {
                "resampler": "pillow-lanczos",
                "dimension_rounding": "floor",
                "source_width_px": 40,
                "source_height_px": 20,
                "target_width_px": 20,
                "target_height_px": 10,
            },
        }
    )
    return value["presented"]


def test_a_resize_recipe_must_preserve_the_aspect_its_own_operation_names():
    """Digest replay accepts stretched targets, but page mapping requires one scale."""
    presented = _resized_presentation()
    validate_presented(presented)

    presented["transform"]["resize"]["target_height_px"] = 18
    with pytest.raises(SchemaRefusal, match="does not preserve the aspect"):
        validate_presented(presented)


def test_a_resize_recipe_rounds_down_and_never_up():
    """The sealed ``floor`` rule makes 40x21 at width 20 exactly 10 pixels high."""
    presented = _resized_presentation()
    presented["transform"]["bounds"]["h"] = 21
    presented["transform"]["resize"]["source_height_px"] = 21
    validate_presented(presented)

    presented["transform"]["resize"]["target_height_px"] = 11
    with pytest.raises(SchemaRefusal, match="does not preserve the aspect"):
        validate_presented(presented)


def test_a_resize_recipe_never_scales_a_dimension_away_entirely():
    """Flooring may not erase a dimension; executable resize targets start at 1px."""
    presented = _resized_presentation()
    presented["transform"]["bounds"].update({"w": 4000, "h": 3})
    presented["transform"]["resize"].update(
        {"source_width_px": 4000, "source_height_px": 3, "target_width_px": 1000}
    )
    presented["transform"]["resize"]["target_height_px"] = 1
    validate_presented(presented)

    presented["transform"]["resize"]["target_height_px"] = 0
    with pytest.raises(SchemaRefusal, match="resize dimensions are invalid"):
        validate_presented(presented)


def test_a_resize_dimension_is_typed_before_the_aspect_identity_reads_it():
    """Malformed dimensions must reach a named refusal before aspect arithmetic."""
    presented = _resized_presentation()
    presented["transform"]["resize"]["target_width_px"] = "20"
    with pytest.raises(SchemaRefusal, match="resize dimensions are invalid"):
        validate_presented(presented)


def test_a_resize_recipe_refuses_an_over_bound_target_as_its_own_schema_error():
    """A resealed target is untrusted input. It must be refused before Pillow
    allocates it, and as a SchemaRefusal the tally knows how to hold."""
    presented = _resized_presentation()
    presented["transform"]["resize"].update(
        {"target_width_px": 200_000_002, "target_height_px": 100_000_001}
    )

    with pytest.raises(SchemaRefusal, match="target exceeds.*pixel bound"):
        validate_presented(presented)


def test_native_capture_accepts_a_genuine_blob_reference():
    validate_native_capture(_native_capture())


@pytest.mark.parametrize(
    "sha256",
    [
        "b" * 63,  # too short
        "b" * 65,  # too long
        "g" * 64,  # non-hex character
        ("B" * 64),  # uppercase is not this pipeline's lowercase-hex shape
        # Exactly 64 characters, checked: at 63 this row was refused for its
        # length like `"b" * 63` above, and reported coverage of the
        # full-length non-hex case that it never exercised.
        "not-a-digest-but-still-non-empty-and-sixty-four-characters-longg",
    ],
)
def test_native_capture_refuses_a_raw_response_reference_that_is_not_a_real_sha256(sha256):
    """A shape check alone (any two non-empty strings) let a malformed or
    forged digest stand as this record's claim to trace back to retained
    bytes (ARCHITECTURE invariant 2, GOALS 5) -- the same gap `is_sha256`
    closes everywhere else this pipeline validates a blob reference.
    """
    value = _native_capture()
    value["raw_response_ref"]["sha256"] = sha256
    with pytest.raises(SchemaRefusal, match="invalid raw-response reference"):
        validate_native_capture(value)


def test_native_capture_refuses_a_blank_relative_path():
    value = _native_capture()
    value["raw_response_ref"]["relative_path"] = ""
    with pytest.raises(SchemaRefusal, match="invalid raw-response reference"):
        validate_native_capture(value)


# ===================== Unit 12: Churro's live two-shape parser ====================
#
# `parser` selects the branch, and the two postures re-derive through the branch
# their own record was written under. Everything below is offline and byte-level.

_WIRE = (
    b'{"schema": "verbatus-churro-page-response.v1", "blocks": '
    b'[{"box_1000": [110, 85, 890, 375], "text": "ACT ONE"}, '
    b'{"box_1000": [110, 470, 890, 835], "text": "ACT TWO"}]}'
)


def test_the_two_parser_names_are_closed_to_the_two_this_chair_can_run():
    assert CHURRO_PARSERS == frozenset({"xml", "churro"})


def test_the_wire_contract_parses_to_the_joined_page_text():
    assert parse_churro_response(_WIRE) == {"state": "parsed", "text": "ACT ONE\nACT TWO"}


def test_the_trained_envelope_stays_fully_legal_on_the_live_parser():
    assert parse_churro_response(b"<output>plain reading</output>") == {
        "state": "parsed",
        "text": "plain reading",
    }


@pytest.mark.parametrize(
    ("body", "outcome"),
    [
        # A JSON object answering a question nobody put to this chair.
        (b'{"schema": "verbatus-chandra-page-response.v1", "blocks": []}', None),
        (b'{"text": "no schema at all"}', None),
        # This contract's own schema, refused inside it by name.
        (b'{"schema": "verbatus-churro-page-response.v1"}', "missing-block-list"),
        (
            b'{"schema": "verbatus-churro-page-response.v1", "blocks": [{"box_1000": [0, 0, 0, 0],'
            b' "text": "x"}]}',
            "malformed-block-geometry",
        ),
        (
            b'{"schema": "verbatus-churro-page-response.v1", "blocks": [], "blocks": []}',
            "unverified-response-schema",
        ),
    ],
)
def test_a_json_body_this_parser_cannot_place_is_an_unrecognized_shape_not_a_failure(body, outcome):
    """The parser ran, read the whole response, and could name no shape it knows.

    Distinct from `failed`, where it refused the bytes. The last row is why the
    dispatch decode is permissive and the contract's own decode is strict: two
    `blocks` members declaring this schema must be refused BY this contract, not
    fall through to the XML door wearing a last-wins value.
    """
    result = parse_churro_response(body)
    assert result["state"] == "unrecognized-shape"
    assert result["outcome"] == (outcome or "unverified-response-schema")


#: Bodies the JSON door cannot place, so the dispatch hands each to the trained
#: parser. Named once because the two tests below are one claim in two halves --
#: *that* these fail and *where* they fail -- and a body present in only one of
#: them would prove neither.
_NON_OBJECT_BODIES = (
    b"not xml and not json",
    b'["a json array, not an object"]',
    b'"a bare json string"',
    b"<output attr='no'>x</output>",
    b"<notoutput>x</notoutput>",
)


@pytest.mark.parametrize("body", _NON_OBJECT_BODIES)
def test_a_non_object_body_reaches_the_trained_parser_and_fails_there(body):
    result = parse_churro_response(body)
    assert result["state"] == "failed"
    assert result["reason"]


@pytest.mark.parametrize("body", _NON_OBJECT_BODIES)
def test_a_non_object_body_is_handed_to_the_trained_parser_by_name(body, monkeypatch):
    """The prior test proves these bodies fail; this one proves *where*.

    `validate_churro_xml` is stubbed so the assertion cannot pass on any
    generic failure reason -- only on the trained door's own text -- which
    is what pins the dispatch to that parser rather than to a coincidence.
    """

    def trained_parser(raw):
        assert raw == body
        return "trained reading"

    monkeypatch.setattr("common.native_witness.validate_churro_xml", trained_parser)
    assert parse_churro_response(body) == {"state": "parsed", "text": "trained reading"}


@pytest.mark.parametrize("raw", ("<output>a str, not bytes</output>", None, 7, ["x"], {"a": 1}))
def test_a_body_that_is_not_bytes_is_refused_by_the_same_name_at_both_churro_doors(raw):
    """One boundary, two return conventions, one sentence for a non-bytes body.

    `validate_churro_xml` has always named this; the dispatcher in front of it
    reached `raw.decode` first and raised `AttributeError` off the caller's
    object instead -- an unnamed interpreter error where the door one branch
    away answers with a sentence a capture can record. Both now say the same
    thing, and it is spelled once (`CHURRO_RESPONSE_NOT_BYTES`).
    """
    assert parse_churro_response(raw) == {"state": "failed", "reason": CHURRO_RESPONSE_NOT_BYTES}
    with pytest.raises(SchemaRefusal, match=CHURRO_RESPONSE_NOT_BYTES):
        validate_churro_xml(raw)


def test_an_oversized_response_is_refused_before_it_ever_reaches_json_loads(monkeypatch):
    """The byte-length gate sits ahead of `json.loads`, not behind a caught error.

    A body one byte over `CHURRO_MAX_RESPONSE_BYTES` must be named the same way
    `derive_churro_capture` names an oversized response, and it must never be
    handed to the JSON parser to find that out -- an unbounded parse over
    retained bytes is exactly the cost this gate exists to avoid paying. Proven
    here by making `json.loads` itself raise: if the dispatcher reached it, this
    test would fail on that raise rather than on a mismatched record.
    """

    def _must_not_be_called(*_args, **_kwargs):
        raise AssertionError("json.loads must not run on an oversized response")

    monkeypatch.setattr("common.native_witness.json.loads", _must_not_be_called)
    oversized = b"x" * (CHURRO_MAX_RESPONSE_BYTES + 1)
    assert parse_churro_response(oversized) == {
        "state": "failed",
        "reason": (
            "Churro response exceeds the retained parsing limit of "
            f"{CHURRO_MAX_RESPONSE_BYTES} bytes (received {len(oversized)})"
        ),
    }


def test_the_guard_admits_exactly_what_the_trained_door_behind_it_admits():
    """`bytearray` is what `validate_churro_xml` has always taken, so it passes.

    A guard in front of that door that admitted less would refuse a body the
    door itself would have read -- which is the failure mode this fix exists to
    remove, not one to introduce a branch earlier. What each contract behind the
    dispatch then does with a `bytearray` is that contract's own answer:
    `churro_response.parse` takes `bytes` alone and names its refusal
    (`raw-response-not-bytes`), which is a placed shape, not an unnamed crash.
    """
    assert parse_churro_response(bytearray(b"<output>plain reading</output>")) == {
        "state": "parsed",
        "text": "plain reading",
    }
    assert parse_churro_response(bytearray(_WIRE)) == {
        "state": "unrecognized-shape",
        "outcome": "raw-response-not-bytes",
    }


def test_the_fixture_parser_name_cannot_reach_the_json_branch_at_all():
    """Fixture byte identity as a property of the dispatcher, not of the corpus.

    A committed fixture body could not take the JSON branch even if someone
    wrote one: `parser="xml"` reaches `validate_churro_xml` and nothing else.
    """
    derived = derive_churro_capture(_WIRE, "eos", parser="xml")
    assert derived["parse"]["state"] == "failed"
    assert derived["parse"]["parser"] == "xml"
    assert derived["stop_reason"] == "partial-parse-failed"


def test_the_live_parser_name_records_the_wire_contract_as_parsed_page_text():
    derived = derive_churro_capture(_WIRE, "eos", parser="churro")
    assert derived["parse"] == {
        "state": "parsed",
        "parser": "churro",
        "text": "ACT ONE\nACT TWO",
    }
    assert derived["stop_reason"] == "eos"
    assert derived["findings"] == []


def test_the_live_parser_records_an_unplaceable_shape_with_its_own_stop_reason():
    derived = derive_churro_capture(b'{"schema": "something-else"}', "stop", parser="churro")
    assert derived["parse"] == {
        "state": "unrecognized-shape",
        "parser": "churro",
        "outcome": "unverified-response-schema",
    }
    assert derived["stop_reason"] == "partial-parse-unrecognized-shape"


def test_the_parse_outcome_wins_over_a_repeated_tail_and_the_repetition_is_still_recorded():
    """As `failed` already did, and the finding stays in `findings` (GOVERNANCE 2).

    Pinned on the record rather than on a contrived body: an unrecognized shape
    is inspected as raw bytes, and raw bytes that are a JSON object end in `"}`,
    so the tail-anchored detector will not fire on one in practice. What the
    validator must refuse is a capture that carries both facts and lets the
    repetition name the stop reason -- which is the direction a later edit to
    `derive_churro_capture`'s branch order would break.
    """
    capture = _native_capture()
    capture["transport_stop_reason"] = "eos"
    capture["parse"] = {
        "state": "unrecognized-shape",
        "parser": "churro",
        "outcome": "unverified-response-schema",
    }
    capture["findings"] = [
        {
            "kind": "post-hoc-repetition",
            "unit_characters": 24,
            "repeats": 3,
            "inspected": "raw-response",
        }
    ]
    capture["stop_reason"] = "partial-parse-unrecognized-shape"
    assert validate_native_capture(capture) is capture
    capture["stop_reason"] = "partial-post-hoc-repetition-detected"
    with pytest.raises(SchemaRefusal, match="disagrees with its parse and findings"):
        validate_native_capture(capture)


def test_the_repetition_detector_reads_the_transcription_under_the_wire_contract():
    """`parse["text"]` is the joined page text, so the measurement moves off the JSON.

    That is the right input and the record already says which view was read:
    repetition is a fact about what the model transcribed, not about the
    punctuation of the envelope it arrived in.
    """
    unit = "the same clause over and over again. "
    body = (
        b'{"schema": "verbatus-churro-page-response.v1", "blocks": [{"box_1000": '
        b'[0, 0, 100, 100], "text": "' + (unit * 12).encode("utf-8") + b'"}]}'
    )
    derived = derive_churro_capture(body, "eos", parser="churro")
    assert derived["parse"]["state"] == "parsed"
    assert derived["findings"][0]["kind"] == "post-hoc-repetition"
    assert derived["findings"][0]["inspected"] == "parsed-text"
    assert derived["stop_reason"] == "partial-post-hoc-repetition-detected"


def test_an_oversized_body_is_refused_before_either_parser_under_both_names():
    # The bound itself, not a copy of its arithmetic: a literal here would keep
    # passing against a gate that had moved, which is the one thing this test is
    # for.
    oversized = b"x" * (CHURRO_MAX_RESPONSE_BYTES + 1)
    for parser in sorted(CHURRO_PARSERS):
        derived = derive_churro_capture(oversized, "eos", parser=parser)
        assert derived["parse"]["state"] == "failed"
        assert derived["parse"]["parser"] == parser
        assert derived["stop_reason"] == "partial-parse-failed"
        assert derived["findings"][0]["kind"] == "post-hoc-repetition-uninspected"


@pytest.mark.parametrize(
    ("body", "state", "stop_reason"),
    [
        (_WIRE, "parsed", "eos"),
        (b"<output>trained</output>", "parsed", "eos"),
        (b'{"schema": "unknown"}', "unrecognized-shape", "partial-parse-unrecognized-shape"),
        (b"not parseable at all", "failed", "partial-parse-failed"),
    ],
)
def test_every_live_capture_state_validates_and_re_derives_from_its_own_bytes(
    body, state, stop_reason
):
    """The three stages that re-derive a Churro capture must reach the same facts.

    `verify_native_capture_bytes` re-derives under `capture["parse"]["parser"]`,
    so this is the check the Perlector's and the Recensor's reads make too.
    """
    digest = digest_bytes(body)
    capture = _native_capture()
    capture["raw_response_ref"] = {
        "relative_path": f"3_attestatores/blobs/sha256/{digest}",
        "sha256": digest,
    }
    capture["transport_stop_reason"] = "eos"
    capture.update(derive_churro_capture(body, "eos", parser="churro"))
    assert capture["parse"]["state"] == state
    assert capture["stop_reason"] == stop_reason
    assert validate_native_capture(capture) is capture
    assert verify_native_capture_bytes(capture, body) is capture


def test_a_live_capture_whose_stop_reason_disagrees_with_its_shape_refusal_is_refused():
    """12B pins both directions: the widening admits the state, not any stop reason."""
    digest = digest_bytes(b'{"schema": "unknown"}')
    capture = _native_capture()
    capture["raw_response_ref"] = {
        "relative_path": f"3_attestatores/blobs/sha256/{digest}",
        "sha256": digest,
    }
    capture["parse"] = {
        "state": "unrecognized-shape",
        "parser": "churro",
        "outcome": "unverified-response-schema",
    }
    capture["stop_reason"] = "stop"
    with pytest.raises(SchemaRefusal, match="disagrees with its parse and findings"):
        validate_native_capture(capture)


def test_a_live_capture_cannot_be_re_derived_under_the_fixture_parser_name():
    """The parser name is part of the record, and re-derivation honours it."""
    digest = digest_bytes(_WIRE)
    capture = _native_capture()
    capture["raw_response_ref"] = {
        "relative_path": f"3_attestatores/blobs/sha256/{digest}",
        "sha256": digest,
    }
    capture["transport_stop_reason"] = "eos"
    capture.update(derive_churro_capture(_WIRE, "eos", parser="churro"))
    capture["parse"] = {**capture["parse"], "parser": "xml"}
    with pytest.raises(SchemaRefusal, match="differs from its retained raw response"):
        verify_native_capture_bytes(capture, _WIRE)


def test_a_parse_state_that_names_no_refusal_is_refused_rather_than_raising_a_key_error():
    """The failure mode this helper was extracted to remove, pinned in both directions."""
    from common.native_witness import native_parse_refusal

    assert native_parse_refusal({"state": "failed", "reason": "unparseable"}) == "unparseable"
    assert native_parse_refusal({"state": "unrecognized-shape", "outcome": "invalid-json"}) == (
        "the response shape was not recognized: invalid-json"
    )
    for state in ("parsed", "pending", "not-requested"):
        with pytest.raises(SchemaRefusal, match="carries no refusal to name"):
            native_parse_refusal({"state": state, "text": "x"})
