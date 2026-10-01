"""Spec 10, test 5: the annotation layer, and the firewall between it and `text`.

Unit tests against `validate_annotations` directly, and the clustered
constructor's sealing of the normalised layer. A page reading records no
annotation layer, so no end-to-end run carries one.

A single act carrying
fifty gaps is ordinary material and anything that behaves acceptably at one gap
and badly at fifty is a defect. Hence the multi-gap cases below.
"""

import pytest

from common.contracts.errors import SchemaRefusal
from conftest import load_stage

archetypus = load_stage("6_archetypus")

REF_A = {
    "relative_path": "3_attestatores/artifacts/testimonium/art_aaaaaaaaaaaaaaaa.json",
    "sha256": "a" * 64,
}
REF_B = {
    "relative_path": "3_attestatores/artifacts/testimonium/art_bbbbbbbbbbbbbbbb.json",
    "sha256": "b" * 64,
}
# The roster a gap annotation may cite, and what each of those witnesses actually
# reported. A quoted variant must be something its witness really said.
WITNESSES = {
    (REF_A["relative_path"], REF_A["sha256"]): "old Reader possibly a name variant-0 v0 x",
    (REF_B["relative_path"], REF_B["sha256"]): "Sohn old variant-1 v1 x",
}


def gap(position: int, *, witness=None, variant: str = "x") -> dict:
    note = {"kind": "illegible", "start": position, "end": position, "witness_evidence": []}
    if witness is not None:
        note["witness_evidence"] = [{"witness_ref": witness, "variant": variant}]
    return note


def uncertain(start: int, end: int, *, certainty="low", alternatives=("Sohn",)) -> dict:
    return {
        "kind": "uncertain",
        "start": start,
        "end": end,
        "certainty": certainty,
        "alternatives": list(alternatives),
    }


# --- validate_annotations: bounds, closed kinds -------------------------------


def test_an_annotation_that_is_not_an_object_is_refused():
    with pytest.raises(SchemaRefusal, match="is not an object"):
        archetypus.validate_annotations(["not a dict"], "some text", WITNESSES, "annotations")


def test_an_unknown_kind_is_refused():
    note = {"kind": "speculative", "start": 0, "end": 1}
    with pytest.raises(SchemaRefusal, match="not one of"):
        archetypus.validate_annotations([note], "some text", WITNESSES, "annotations")


def test_a_span_starting_before_zero_is_refused():
    with pytest.raises(SchemaRefusal, match="outside this reading's own text bounds"):
        archetypus.validate_annotations([uncertain(-1, 2)], "some text", WITNESSES, "annotations")


def test_a_span_ending_past_the_text_length_is_refused():
    with pytest.raises(SchemaRefusal, match="outside this reading's own text bounds"):
        archetypus.validate_annotations([uncertain(0, 999)], "some text", WITNESSES, "annotations")


def test_a_non_integer_start_is_refused():
    note = {"kind": "illegible", "start": 1.5, "end": 1.5, "witness_evidence": []}
    with pytest.raises(SchemaRefusal, match="non-integer"):
        archetypus.validate_annotations([note], "some text", WITNESSES, "annotations")


def test_an_absurdly_large_end_is_refused_cleanly_rather_than_crashing_on_format():
    """The magnitude check has to fire before the bounds check formats its
    message: CPython refuses to render an int of more than ~4300 digits, so an
    offset this large turns one malformed annotation into an uncaught ValueError
    that takes the whole run down rather than refusing a single act."""
    huge = 10**10000
    note = {
        "kind": "uncertain",
        "start": 0,
        "end": huge,
        "certainty": "high",
        "alternatives": ["x"],
    }
    with pytest.raises(SchemaRefusal, match="far outside any plausible text length"):
        archetypus.validate_annotations([note], "some text", WITNESSES, "annotations")


def test_an_absurdly_large_negative_start_is_refused_cleanly_rather_than_crashing_on_format():
    huge_negative = -(10**10000)
    note = {
        "kind": "illegible",
        "start": huge_negative,
        "end": huge_negative,
        "witness_evidence": [],
    }
    with pytest.raises(SchemaRefusal, match="far outside any plausible text length"):
        archetypus.validate_annotations([note], "some text", WITNESSES, "annotations")


def test_a_boolean_start_is_refused_even_though_it_is_technically_an_int():
    note = {"kind": "illegible", "start": True, "end": True, "witness_evidence": []}
    with pytest.raises(SchemaRefusal, match="non-integer"):
        archetypus.validate_annotations([note], "some text", WITNESSES, "annotations")


def test_an_unknown_top_level_field_on_a_gap_is_refused():
    note = gap(1)
    note["note"] = "extra"
    with pytest.raises(SchemaRefusal, match="outside its closed schema"):
        archetypus.validate_annotations([note], "some text", WITNESSES, "annotations")


def test_a_certainty_field_on_a_gap_is_refused():
    """A gap read nothing, so it has no certainty about characters to declare."""
    note = gap(1)
    note["certainty"] = "low"
    with pytest.raises(SchemaRefusal, match="outside its closed schema"):
        archetypus.validate_annotations([note], "some text", WITNESSES, "annotations")


# --- The gap firewall: zero-width, structurally ------------------------------


def test_a_gap_with_start_not_equal_to_end_is_refused():
    note = {"kind": "illegible", "start": 2, "end": 5, "witness_evidence": []}
    with pytest.raises(SchemaRefusal, match="zero-width anchor"):
        archetypus.validate_annotations([note], "some text", WITNESSES, "annotations")


def test_a_zero_width_gap_with_no_evidence_is_accepted():
    """Every witness may have found the same damage; that is ordinary."""
    validated = archetypus.validate_annotations([gap(3)], "some text", WITNESSES, "annotations")
    assert validated == [{"kind": "illegible", "start": 3, "end": 3, "witness_evidence": []}]


def test_a_gap_with_the_witness_evidence_field_absent_is_accepted():
    note = {"kind": "illegible", "start": 3, "end": 3}
    validated = archetypus.validate_annotations([note], "some text", WITNESSES, "annotations")
    assert validated[0]["witness_evidence"] == []


def test_gap_evidence_must_cite_one_of_this_acts_own_witnesses():
    stranger = {
        "relative_path": "3_attestatores/artifacts/testimonium/art_cccccccccccccccc.json",
        "sha256": "c" * 64,
    }
    note = gap(2, witness=stranger, variant="Reader")
    with pytest.raises(SchemaRefusal, match="not one of this act's own witnesses"):
        archetypus.validate_annotations([note], "some text", WITNESSES, "annotations")


def test_gap_evidence_requires_a_non_empty_variant():
    note = gap(2, witness=REF_A, variant="")
    with pytest.raises(SchemaRefusal, match="names no variant reading"):
        archetypus.validate_annotations([note], "some text", WITNESSES, "annotations")


def test_a_variant_no_witness_ever_reported_is_refused():
    """A quoted variant that is neither the ink nor something its cited witness
    actually said is a reconstruction, and the record carries none of those."""
    note = gap(2, witness=REF_A, variant="INVENTED")
    with pytest.raises(SchemaRefusal, match="never reported"):
        archetypus.validate_annotations([note], "some text", WITNESSES, "annotations")


def test_a_variant_attributed_to_the_wrong_witness_is_refused():
    """`Sohn` really was reported -- by the other witness. Attribution is checked
    against the witness actually named, not against the roster as a pool."""
    note = gap(2, witness=REF_A, variant="Sohn")
    with pytest.raises(SchemaRefusal, match="never reported"):
        archetypus.validate_annotations([note], "some text", WITNESSES, "annotations")


def test_a_variant_from_a_witness_that_reported_nothing_is_refused():
    dead = {
        "relative_path": "3_attestatores/artifacts/testimonium/art_dddddddddddddddd.json",
        "sha256": "d" * 64,
    }
    witnesses = dict(WITNESSES)
    witnesses[(dead["relative_path"], dead["sha256"])] = None
    note = gap(2, witness=dead, variant="anything")
    with pytest.raises(SchemaRefusal, match="never reported"):
        archetypus.validate_annotations([note], "some text", witnesses, "annotations")


def test_gap_evidence_has_a_closed_two_field_schema():
    note = gap(2, witness=REF_A, variant="Reader")
    note["witness_evidence"][0]["candidate_text"] = "Reader"
    with pytest.raises(SchemaRefusal, match="is not exactly"):
        archetypus.validate_annotations([note], "some text", WITNESSES, "annotations")


def test_the_same_witness_claim_twice_is_refused():
    note = gap(2, witness=REF_A, variant="Reader")
    note["witness_evidence"].append({"witness_ref": REF_A, "variant": "Reader"})
    with pytest.raises(SchemaRefusal, match="repeats the same witness claim"):
        archetypus.validate_annotations([note], "some text", WITNESSES, "annotations")


def test_two_witnesses_disagreeing_at_one_gap_are_both_retained():
    """Nothing here picks between them -- both claims travel, side by side."""
    note = gap(2, witness=REF_A, variant="old")
    note["witness_evidence"].append({"witness_ref": REF_B, "variant": "Sohn"})
    validated = archetypus.validate_annotations([note], "some text", WITNESSES, "annotations")
    assert [item["variant"] for item in validated[0]["witness_evidence"]] == ["old", "Sohn"]


# --- uncertain spans: real characters, at least one, with alternatives -------


def test_an_uncertain_span_with_zero_width_is_refused():
    with pytest.raises(SchemaRefusal, match="must cover at least one"):
        archetypus.validate_annotations([uncertain(4, 4)], "some text", WITNESSES, "annotations")


def test_an_uncertain_span_covering_only_whitespace_is_refused():
    """Width is not a readable character, and the difference is load-bearing.

    On text that is entirely blank, `derive_text_status` finds no gap and returns
    `no_readable_text` — a positive finding that the act held no ink. A span
    accepted over that blankness would sit in the same record asserting the
    reader did read characters there and offering alternatives for them. The two
    silences separated here would then be one, inside a single sealed record.
    """
    with pytest.raises(SchemaRefusal, match="covering no readable character"):
        archetypus.validate_annotations([uncertain(0, 3)], "   ", WITNESSES, "annotations")


def test_no_readable_text_can_never_carry_an_annotation_at_all():
    """The closure, stated as one property rather than left to be re-derived.

    Only two annotation kinds exist: a gap forces `partial`, and an uncertain
    span now requires a readable character, which forces `established`. So the
    status that claims there was no ink is reachable only with an empty
    annotation list.
    """
    for text in ("", "   ", "\n\t "):
        assert archetypus.derive_text_status(text, []) == "no_readable_text"
        with pytest.raises(SchemaRefusal, match="covering no readable character"):
            archetypus.validate_annotations(
                [uncertain(0, len(text))], text, WITNESSES, "annotations"
            )
        gapped = archetypus.validate_annotations([gap(0)], text, WITNESSES, "annotations")
        assert archetypus.derive_text_status(text, gapped) == "partial"


def test_an_uncertain_span_with_no_alternatives_is_refused():
    with pytest.raises(SchemaRefusal, match="names no alternatives"):
        archetypus.validate_annotations(
            [uncertain(0, 4, alternatives=())], "some text", WITNESSES, "annotations"
        )


def test_an_uncertain_span_with_an_unknown_certainty_is_refused():
    with pytest.raises(SchemaRefusal, match="not one of"):
        archetypus.validate_annotations(
            [uncertain(0, 4, certainty="0.7")], "some text", WITNESSES, "annotations"
        )


def test_an_uncertain_span_with_no_certainty_at_all_is_refused():
    note = uncertain(0, 4)
    del note["certainty"]
    with pytest.raises(SchemaRefusal, match="not one of"):
        archetypus.validate_annotations([note], "some text", WITNESSES, "annotations")


def test_a_repeated_alternative_reading_is_refused():
    with pytest.raises(SchemaRefusal, match="repeats an alternative"):
        archetypus.validate_annotations(
            [uncertain(0, 4, alternatives=("Sohn", "Sohn"))], "some text", WITNESSES, "annotations"
        )


def test_an_empty_alternative_reading_is_refused():
    with pytest.raises(SchemaRefusal, match="empty or non-string alternative"):
        archetypus.validate_annotations(
            [uncertain(0, 4, alternatives=("",))], "some text", WITNESSES, "annotations"
        )


def test_an_uncertain_span_within_bounds_with_real_alternatives_is_accepted():
    """The reader's own candidate readings for characters it did read -- not a
    witness's, because a witness attaches to a gap, where nothing was read."""
    validated = archetypus.validate_annotations(
        [uncertain(0, 4, certainty="medium", alternatives=("Sohn", "Sahn"))],
        "some text",
        WITNESSES,
        "annotations",
    )
    assert validated[0] == {
        "kind": "uncertain",
        "start": 0,
        "end": 4,
        "certainty": "medium",
        "alternatives": ["Sohn", "Sahn"],
    }


def test_an_uncertain_span_carries_no_witness_reference_field_at_all():
    note = uncertain(0, 4)
    note["witness_ref"] = REF_A
    with pytest.raises(SchemaRefusal, match="outside its closed schema"):
        archetypus.validate_annotations([note], "some text", WITNESSES, "annotations")


# --- Several gaps at once, and a real stress case ----------------------------


def test_several_gaps_at_once_all_validate_and_are_all_carried():
    text = "the ---- man ---- from ---- nowhere"
    notes = [
        gap(0),  # leading
        gap(8, witness=REF_A, variant="old"),
        gap(17),
        gap(len(text)),  # trailing
    ]
    validated = archetypus.validate_annotations(notes, text, WITNESSES, "annotations")
    assert len(validated) == 4
    assert [note["start"] for note in validated] == [0, 8, 17, len(text)]
    assert all(note["start"] == note["end"] for note in validated)


def test_a_whole_act_gap_is_representable_on_empty_text():
    """Leading, internal, trailing -- and the whole-act case, where there is no
    text at all and the ink is nonetheless known to be there."""
    validated = archetypus.validate_annotations([gap(0)], "", WITNESSES, "annotations")
    assert validated == [{"kind": "illegible", "start": 0, "end": 0, "witness_evidence": []}]
    assert archetypus.derive_text_status("", validated) == "partial"


def test_fifty_gaps_at_once_behave_no_differently_than_one():
    """A damaged page yielding a few readable words plus many gaps is a
    successful partial reading, not an occasion to give up -- so the schema
    must not degrade at scale."""
    text = "abcdefghij"
    refs = [REF_A, REF_B]
    notes = [
        gap(
            index % (len(text) + 1),
            witness=refs[index % 2],
            variant=f"variant-{index % 2}",
        )
        for index in range(50)
    ]
    validated = archetypus.validate_annotations(notes, text, WITNESSES, "annotations")
    assert len(validated) == 50
    assert all(note["kind"] == "illegible" and note["start"] == note["end"] for note in validated)
    assert archetypus.derive_text_status(text, validated) == "partial"


# --- Render -> strip -> hash round-trip: a schema-sufficiency demonstration --
#
# Spec 10 test 4's second half. No stage builds real display rendering yet --
# that is the Armarium's future business at export time -- so this is a
# test-only helper proving the schema this stage writes is *sufficient* to
# support that round-trip once built, not a shipped rendering feature.


def _demo_render(text: str, annotations: list[dict]) -> str:
    """A minimal Leiden-style bracket rendering, for this test only."""
    rendered = []
    cursor = 0
    for note in sorted(annotations, key=lambda item: item["start"]):
        if note["kind"] != "illegible":
            continue
        rendered.append(text[cursor : note["start"]])
        rendered.append("⟨illegible⟩")
        cursor = note["start"]
    rendered.append(text[cursor:])
    return "".join(rendered)


def _demo_strip(rendered: str) -> str:
    return rendered.replace("⟨illegible⟩", "")


def test_render_strip_hash_round_trip_reproduces_the_canonical_text_hash():
    from common.contracts.canonical import digest_of

    text = "the man from nowhere"
    annotations = [gap(0), gap(7), gap(len(text))]
    validated = archetypus.validate_annotations(annotations, text, {}, "annotations")
    rendered = _demo_render(text, validated)
    assert rendered != text  # the display really does differ from the clean text
    stripped = _demo_strip(rendered)
    assert stripped == text
    # Against a pinned digest, not digest_of(text): once stripped == text
    # holds, comparing two calls of the same function proves nothing. The
    # constant is what a sealed record's text_hash would hold for this text,
    # so this is the recomputation a real consumer performs.
    assert digest_of(stripped) == "67173165481aa850b657885cbee282a56bcc4ff006b49aee5e266b94b4eaa035"


# --- the clustered constructor seals the normalised layer ---

# The closed region schema (`run._REGION_FIELDS`), carried whole into the record.
_REGION = {
    "region_id": "rgn_0123456789abcdef",
    "image_path": "2_designator/blobs/sha256/crop",
    "image_sha256": "c" * 64,
    "verified_dimensions": {"w": 1, "h": 1},
    "source_page_ordinal": 1,
    "source_page_id": "pg_0123456789abcdef",
    "transform": {
        "operation": "crop",
        "source_page_ordinal": 1,
        "source_page_id": "pg_0123456789abcdef",
        "bounds": {"x": 0, "y": 0, "w": 1, "h": 1},
    },
    "structure_provenance": {"chair": "designator", "revision": "fixture"},
    "witness_covered": True,
}


@pytest.mark.parametrize(
    ("lectio_kind", "view", "blind_read"),
    [
        ("primed-with-prior", "fed", "fed"),
        ("primed-draft-withheld", "withheld", "off"),
        ("primed-draft-withheld", "withheld", "saved"),
    ],
)
def test_a_joint_reading_that_omits_witness_evidence_still_establishes(
    monkeypatch, lectio_kind, view, blind_read
):
    """The clustered constructor must normalise before it seals.

    `validate_annotations` NORMALISES: an `illegible` note may legally arrive on
    the wire without `witness_evidence` (the Perlector's contract says so), and
    the validated form always carries it. `validate_logical_record` then
    requires the stored layer to equal the validated form of itself. So a
    clustered constructor that stored `payload["annotations"]` raw refused the
    first joint reading that marked unread ink: the reading was accepted, the
    ink was read, and the act established nothing at all.

    The two partition/evidence bindings are stubbed out here on purpose. They
    are proved end to end by the cluster-path suite over a real sealed
    partition; what this test isolates is the one step between an accepted
    payload and a sealed layer, which no fixture is needed to reach.
    """
    monkeypatch.setattr(
        archetypus,
        "_require_the_partition_this_reading_was_made_over",
        lambda **_kwargs: {"logical_act_id": "pac_0123456789abcdef"},
    )
    monkeypatch.setattr(archetypus, "_require_joint_evidence_binding", lambda **_kwargs: None)
    monkeypatch.setattr(
        archetypus,
        "validate_cross_capture_dissent",
        lambda record: {
            "logical_act_id": "pac_0123456789abcdef",
            "perlectio_ref": record["perlectio_ref"],
        },
    )

    fed = blind_read == "fed"
    source = "a" * 64
    text = "established text"
    prior_ref = {"relative_path": "4_perlector/artifacts/lectio-prior/x.json", "sha256": "d" * 64}
    # Wire-legal and un-normalised: no `witness_evidence` key at all.
    wire_note = {"kind": "illegible", "start": 5, "end": 5}
    payload = {
        "text": text,
        "lectio_kind": lectio_kind,
        "dossier": {"logical_act_id": "pac_0123456789abcdef", "prior_draft_view": view}
        | ({"prior_draft": {"reference": prior_ref, "text": text}} if fed else {}),
        "protocol": {"blind_read": blind_read},
        "basis": {"regions": [_REGION]},
        "provenance": {"chair": "perlector", "revision": "fixture"},
        "annotations": [wire_note],
        "uncertain_spans": [],
        "gaps": [],
        "uncertainty_assessment": {
            "state": "not-assessed",
            "problem": "this fixture reader has no channel for a doubt report",
        },
        "self_revision": [],
    }
    perlectio = {"outcome": "read", "payload": payload, "inputs": [prior_ref] if fed else []}
    perlectio_ref = {
        "relative_path": "4_perlector/artifacts/perlectio/joint.json",
        "sha256": archetypus.digest_of(perlectio),
    }
    review = {"outcome": "accepted", "payload": {"perlectio_ref": perlectio_ref}}
    review_ref = {
        "relative_path": "5_recensor/artifacts/review/joint.json",
        "sha256": archetypus.digest_of(review),
    }
    dissent = {"perlectio_ref": perlectio_ref}
    dissent_ref = {
        "relative_path": "4_perlector/artifacts/dissent/joint.json",
        "sha256": archetypus.digest_of(dissent),
    }
    logical_act = {
        "logical_act_id": "pac_0123456789abcdef",
        "physical_page_components": [
            {"physical_page_id": "ppg_0123456789abcdef", "required_capture_sha256s": [source]}
        ],
        "member_local_acts": [
            {
                "act_id": "act_0123456789abcdef",
                "act_key": "member-key",
                "page_id": "pg_0123456789abcdef",
                "page_ordinal": 1,
                "source_sha256": source,
                "proposal_refs": ["proposal:member"],
            }
        ],
    }

    record = archetypus.establish_logical_record(
        partition={},
        logical_act=logical_act,
        accepted_perlectio=perlectio,
        accepted_review=review,
        perlectio_ref=perlectio_ref,
        recensor_ref=review_ref,
        cross_capture_dissent=dissent,
        cross_capture_dissent_ref=dissent_ref,
    )

    # Sealed in the validated form, never the raw one, and the record is the
    # partial it says it is rather than an act that quietly failed to exist.
    assert record["annotations"] == [
        {"kind": "illegible", "start": 5, "end": 5, "witness_evidence": []}
    ]
    assert record["annotations"] != [wire_note]
    assert record["text_status"] == "partial"
    # And the sealed layer is exactly what its own re-validation produces, which
    # is the equality the record schema enforces on every read back.
    assert archetypus.validate_logical_record(record) == record
    assert record["uncertainty"]["lectio_kind"] == lectio_kind
    assert record["uncertainty"]["self_revisions"] == ([] if fed else None)

    def reseal_refs():
        perlectio_ref["sha256"] = archetypus.digest_of(perlectio)
        review_ref["sha256"] = archetypus.digest_of(review)
        dissent_ref["sha256"] = archetypus.digest_of(dissent)

    original_kind = payload["lectio_kind"]
    payload["lectio_kind"] = "primed-draft-withheld" if fed else "primed-with-prior"
    reseal_refs()
    with pytest.raises(SchemaRefusal, match="without a .* prior-draft view"):
        archetypus.establish_logical_record(
            partition={},
            logical_act=logical_act,
            accepted_perlectio=perlectio,
            accepted_review=review,
            perlectio_ref=perlectio_ref,
            recensor_ref=review_ref,
            cross_capture_dissent=dissent,
            cross_capture_dissent_ref=dissent_ref,
        )
    payload["lectio_kind"] = original_kind
    payload["protocol"]["blind_read"] = "off" if fed else "fed"
    reseal_refs()
    with pytest.raises(SchemaRefusal, match="contrary to its prior-draft protocol"):
        archetypus.establish_logical_record(
            partition={},
            logical_act=logical_act,
            accepted_perlectio=perlectio,
            accepted_review=review,
            perlectio_ref=perlectio_ref,
            recensor_ref=review_ref,
            cross_capture_dissent=dissent,
            cross_capture_dissent_ref=dissent_ref,
        )
    payload["protocol"]["blind_read"] = blind_read
    if not fed:
        payload["self_revision"] = [
            {
                "reading_span": {"start": 0, "end": 1},
                "testimonium_span": {"start": 0, "end": 1},
            }
        ]
        reseal_refs()
        with pytest.raises(SchemaRefusal, match="against a draft withheld"):
            archetypus.establish_logical_record(
                partition={},
                logical_act=logical_act,
                accepted_perlectio=perlectio,
                accepted_review=review,
                perlectio_ref=perlectio_ref,
                recensor_ref=review_ref,
                cross_capture_dissent=dissent,
                cross_capture_dissent_ref=dissent_ref,
            )
