"""Non-tautological refusals for Unit 10B's derived witness waist."""

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
    validate_presented_page_binding,
    verify_native_capture_bytes,
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
            "reported": text,
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
            "terminal XML parse",
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
    value.pop("reported")
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

    value["reported"] = ""
    with pytest.raises(SchemaRefusal, match="claims a reported absence"):
        validate_page_testimonium_payload(value)


def test_churro_capture_derivation_is_checked_against_its_authoritative_raw_bytes():
    value = _page_with_churro_capture()
    raw = f"<output>{value['payload']}</output>".encode()
    assert verify_native_capture_bytes(value["native_capture"], raw) is value["native_capture"]

    value["native_capture"]["parse"]["text"] = "a coherently resealed false projection"
    with pytest.raises(SchemaRefusal, match="parse.*retained raw response"):
        verify_native_capture_bytes(value["native_capture"], raw)


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
    """Two observations claiming the same proposal is a tie, symmetric to one
    observation claiming two proposals — the consult names both as "two
    observations tie", and each observation here matches only one proposal on
    its own side of the count, so a one-sided (observation-only) tie check
    would silently miss this."""
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
