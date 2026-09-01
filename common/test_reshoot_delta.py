"""Unit 20's re-shoot instrument is structural and non-evaluative."""

from __future__ import annotations

from typing import Any

import pytest

from common.contracts.canonical import self_hash, verify_self_hash
from common.contracts.errors import SchemaRefusal
from common.cross_capture_dissent import _FAILED_CONDITION_CODES, CAVEAT
from common.reshoot_delta import (
    CONDITION_CODES,
    DENOMINATOR_CAVEAT,
    build_reshoot_delta_record,
    validate_reshoot_delta_record,
)
from common.test_cross_capture_dissent import A, B, C, _pair, _record, _ref, _view


def _only_pair(record: dict[str, Any]) -> dict[str, Any]:
    (pair,) = record["pair_records"]
    return pair


def test_delta_emits_only_for_all_four_conditions_and_every_failed_pair_remains_named():
    clean = build_reshoot_delta_record(_record())
    assert _only_pair(clean)["delta_loci"] == [
        {
            "locus_id": "locus:0",
            "established_span_or_gap_ref": {"start": 0, "end": 5},
            "view_ids": ["view:a", "view:b"],
        }
    ]
    failures = (
        (
            "capture_condition",
            {"both_unoccluded": False, "comparably_captured": True},
            "capture-occlusion-condition-failed",
        ),
        (
            "capture_condition",
            {"both_unoccluded": True, "comparably_captured": False},
            "capture-comparability-condition-failed",
        ),
        ("same_ink", False, "same-ink-condition-failed"),
        ("identical_run_configuration", False, "identical-run-configuration-condition-failed"),
    )
    for field, value, code in failures:
        record = build_reshoot_delta_record(
            _record(pairs=[_pair(["view:a", "view:b"], **{field: value}, finding_codes=[code])])
        )
        pair = _only_pair(record)
        assert pair["view_ids"] == ["view:a", "view:b"]
        assert pair["delta_loci"] == []
        assert {flag["code"] for flag in record["review_flags"]} == {code}


def test_conditions_failing_in_combination_are_all_named_and_still_suppress_the_delta():
    """Multiple failures must all remain named; the gate cannot short-circuit evidence."""
    codes = (
        "capture-occlusion-condition-failed",
        "capture-comparability-condition-failed",
        "same-ink-condition-failed",
        "identical-run-configuration-condition-failed",
        "cross-capture-match-failure",
    )
    record = build_reshoot_delta_record(
        _record(
            pairs=[
                _pair(
                    ["view:a", "view:b"],
                    capture_condition={"both_unoccluded": False, "comparably_captured": False},
                    same_ink=False,
                    identical_run_configuration=False,
                    act_match_correct=False,
                    finding_codes=list(codes),
                )
            ]
        )
    )
    pair = _only_pair(record)
    assert pair["delta_loci"] == []
    assert set(pair["finding_codes"]) == set(codes)
    flag_codes = {
        flag["code"] for flag in record["review_flags"] if flag["pair_id"] == pair["pair_id"]
    }
    assert flag_codes == set(codes)


def test_three_views_keep_every_pair_including_the_one_that_fails_match():
    """A match-failing pair must remain named in the denominator beside clean pairs."""
    views = [_view("view:a", A), _view("view:b", B), _view("view:c", C)]
    pairs = [
        _pair(["view:a", "view:b"]),
        _pair(["view:a", "view:c"]),
        _pair(
            ["view:b", "view:c"],
            act_match_correct=False,
            finding_codes=["cross-capture-match-failure"],
        ),
    ]
    record = build_reshoot_delta_record(_record(views=views, pairs=pairs))
    assert {tuple(pair["view_ids"]) for pair in record["pair_records"]} == {
        ("view:a", "view:b"),
        ("view:a", "view:c"),
        ("view:b", "view:c"),
    }
    (failing,) = [
        pair for pair in record["pair_records"] if pair["view_ids"] == ["view:b", "view:c"]
    ]
    assert failing["delta_loci"] == []
    assert failing["finding_codes"] == ["cross-capture-match-failure"]
    assert {
        flag["code"] for flag in record["review_flags"] if flag["pair_id"] == failing["pair_id"]
    } == {"cross-capture-match-failure"}


def test_cross_frame_match_failure_is_a_denominator_finding_not_a_delta():
    record = build_reshoot_delta_record(
        _record(
            pairs=[
                _pair(
                    ["view:a", "view:b"],
                    act_match_correct=False,
                    finding_codes=["cross-capture-match-failure"],
                )
            ]
        )
    )
    assert _only_pair(record)["delta_loci"] == []
    assert record["review_flags"] == [
        {
            "pair_id": _only_pair(record)["pair_id"],
            "code": "cross-capture-match-failure",
            "locus_ids": [],
        }
    ]


def test_record_repeats_the_binding_caveat_and_carries_no_variance_number():
    record = build_reshoot_delta_record(_record())
    assert record["caveat"] == CAVEAT
    assert "does not identify the accurate reading" in record["caveat"]

    def keys(value: Any):
        if isinstance(value, dict):
            for key, item in value.items():
                yield key.lower()
                yield from keys(item)
        elif isinstance(value, list):
            for item in value:
                yield from keys(item)

    prohibited = ("variance", "score", "confidence", "quality", "accuracy")
    assert not any(fragment in key for key in keys(record) for fragment in prohibited)
    assert validate_reshoot_delta_record(record, _record()) == record


def test_derivative_cannot_replace_the_caveat_or_create_an_unanchored_delta():
    record = build_reshoot_delta_record(_record())
    reworded = {**record, "caveat": "different captures identify the wrong reading"}
    reworded["self_hash"] = self_hash(
        {key: value for key, value in reworded.items() if key != "self_hash"}
    )
    with pytest.raises(SchemaRefusal, match="caveat"):
        validate_reshoot_delta_record(reworded, _record())
    malformed = build_reshoot_delta_record(_record())
    malformed["pair_records"][0]["delta_loci"] = [{"locus_id": "locus:0"}]
    malformed["self_hash"] = self_hash(
        {key: value for key, value in malformed.items() if key != "self_hash"}
    )
    with pytest.raises(SchemaRefusal, match="structural locus anchor"):
        validate_reshoot_delta_record(malformed, _record())


def test_building_the_derivative_does_not_alias_its_sealed_evidence():
    """A mutable derivative must not provide a write path into Unit 19 evidence."""
    dissent = _record()
    original_perlectio_ref = dict(dissent["perlectio_ref"])
    original_span = dict(dissent["loci"][0]["established_span_or_gap_ref"])
    record = build_reshoot_delta_record(dissent)

    record["perlectio_ref"]["relative_path"] = "changed/perlectio.json"
    record["pair_records"][0]["delta_loci"][0]["established_span_or_gap_ref"]["end"] = 4

    assert dissent["perlectio_ref"] == original_perlectio_ref
    assert dissent["loci"][0]["established_span_or_gap_ref"] == original_span
    assert verify_self_hash(dissent)


def _locus(
    forms: tuple[Any, ...],
    *,
    state: str = "different-across-views",
    view_ids: tuple[str, ...] = ("view:a", "view:b"),
) -> dict[str, Any]:
    return {
        "locus_id": "locus:0",
        "established_span_or_gap_ref": {"start": 0, "end": 5},
        "comparison_state": state,
        "observations": [
            {
                "view_id": view_id,
                "observed_form": form,
                "image_region_refs": [_ref(f"crop/{view_id}.png")],
                "reason_codes": [],
            }
            for view_id, form in zip(view_ids, forms, strict=True)
        ],
    }


def test_a_pair_states_whether_it_was_compared_so_an_empty_delta_list_is_unambiguous():
    """An empty delta list must distinguish agreement from ineligible comparison."""
    compared = _only_pair(build_reshoot_delta_record(_record()))
    assert compared["comparison_state"] == "compared"
    assert compared["delta_loci"] != []

    blocked = _only_pair(
        build_reshoot_delta_record(
            _record(
                pairs=[
                    _pair(
                        ["view:a", "view:b"],
                        same_ink=False,
                        finding_codes=["same-ink-condition-failed"],
                    )
                ]
            )
        )
    )
    assert blocked["comparison_state"] == "not-compared"
    assert blocked["delta_loci"] == []
    assert blocked["uncompared_loci"] == []

    # Findings do not imply that a pair failed its comparison conditions.
    noted = _record(pairs=[_pair(["view:a", "view:b"], finding_codes=["capture-note-recorded"])])
    still_compared = _only_pair(build_reshoot_delta_record(noted))
    assert still_compared["comparison_state"] == "compared"
    assert still_compared["finding_codes"] == ["capture-note-recorded"]
    assert still_compared["delta_loci"] != []


def test_a_locus_one_capture_could_not_read_is_named_rather_than_read_as_agreement():
    """An unread form must leave a named row and flag, never imply agreement."""
    absent_form = _record(loci=[_locus(("Maria", None), state="unreadable")])
    pair = _only_pair(build_reshoot_delta_record(absent_form))
    assert pair["comparison_state"] == "compared"
    assert pair["delta_loci"] == []
    assert pair["uncompared_loci"] == [
        {
            "locus_id": "locus:0",
            "established_span_or_gap_ref": {"start": 0, "end": 5},
            "view_ids": ["view:a", "view:b"],
            "reason_codes": [
                "locus-recorded-unreadable-across-views",
                "observed-form-absent-at-a-view-of-the-pair",
            ],
        }
    ]
    flags = build_reshoot_delta_record(absent_form)["review_flags"]
    assert flags == [
        {
            "pair_id": pair["pair_id"],
            "code": "cross-capture-locus-not-compared",
            "locus_ids": ["locus:0"],
        }
    ]

    # 19D's composed dissent validator is stricter than the copy this unit was
    # built against: a locus row that omits a declared view is refused at
    # intake by name, rather than softened into an uncompared row downstream.
    # The protection is the same fact -- an absent view never reads as
    # agreement -- caught one boundary earlier.
    missing_row = _locus(("Maria",), state="not-comparable", view_ids=("view:a",))
    with pytest.raises(SchemaRefusal, match="omits view observation"):
        build_reshoot_delta_record(_record(loci=[missing_row]))


def test_a_locus_the_reader_recorded_as_not_comparable_is_not_compared_here():
    """The consumer must not overrule the reader's non-comparable state."""
    record = build_reshoot_delta_record(
        _record(loci=[_locus(("Maria", "Marta"), state="not-comparable")])
    )
    pair = _only_pair(record)
    assert pair["delta_loci"] == []
    assert [row["reason_codes"] for row in pair["uncompared_loci"]] == [
        ["locus-recorded-not-comparable-across-views"]
    ]


def test_at_a_compared_pair_every_locus_is_a_delta_a_named_reason_or_an_agreement():
    """Only observed agreement may omit a locus from both output partitions."""
    views = [_view("view:a", A), _view("view:b", B), _view("view:c", C)]
    triple = ("view:a", "view:b", "view:c")
    loci = [
        _locus(("Maria", "Marta", "Marie"), view_ids=triple),
        {**_locus(("Maria", None, "Maria"), view_ids=triple), "locus_id": "locus:1"},
        {
            **_locus(("Maria", "Maria", "Maria"), state="same-across-views", view_ids=triple),
            "locus_id": "locus:2",
        },
    ]
    dissent = _record(views=views, loci=loci)
    record = build_reshoot_delta_record(dissent)
    forms = {
        locus["locus_id"]: {
            item["view_id"]: item["observed_form"] for item in locus["observations"]
        }
        for locus in dissent["loci"]
    }
    for pair in record["pair_records"]:
        assert pair["comparison_state"] == "compared"
        delta_ids = {row["locus_id"] for row in pair["delta_loci"]}
        uncompared_ids = {row["locus_id"] for row in pair["uncompared_loci"]}
        assert not delta_ids & uncompared_ids
        for locus_id, seen in forms.items():
            observed = [seen[view_id] for view_id in pair["view_ids"]]
            if locus_id in delta_ids:
                assert None not in observed and len(set(observed)) > 1
            elif locus_id in uncompared_ids:
                assert None in observed
            else:
                # No row: present and equal at both views of this pair.
                assert None not in observed and len(set(observed)) == 1
    named_for_locus_one = {
        tuple(pair["view_ids"])
        for pair in record["pair_records"]
        for row in pair["uncompared_loci"]
        if row["locus_id"] == "locus:1"
    }
    assert named_for_locus_one == {("view:a", "view:b"), ("view:b", "view:c")}


def test_an_upstream_finding_this_unit_does_not_recognise_is_carried_not_curated():
    """The consumer must carry valid upstream findings it has no authority to curate."""
    dissent = _record(
        pairs=[
            _pair(
                ["view:a", "view:b"],
                act_match_correct=False,
                finding_codes=["cross-capture-match-failure", "capture-note-recorded"],
            )
        ]
    )
    record = build_reshoot_delta_record(dissent)
    assert _only_pair(record)["finding_codes"] == [
        "capture-note-recorded",
        "cross-capture-match-failure",
    ]
    assert validate_reshoot_delta_record(record, dissent) == record
    assert {flag["code"] for flag in record["review_flags"]} == {
        "capture-note-recorded",
        "cross-capture-match-failure",
    }


@pytest.mark.parametrize("code", (" ", "\n"))
def test_an_upstream_finding_without_a_printable_name_is_refused_not_dropped(code):
    dissent = _record(pairs=[_pair(["view:a", "view:b"], finding_codes=[code])])

    with pytest.raises(SchemaRefusal, match="upstream pair finding"):
        build_reshoot_delta_record(dissent)


@pytest.mark.parametrize(
    "code", ("cross-capture-structural-delta", "cross-capture-locus-not-compared")
)
def test_an_upstream_finding_cannot_masquerade_as_a_unit20_derived_flag(code):
    dissent = _record(pairs=[_pair(["view:a", "view:b"], finding_codes=[code])])

    with pytest.raises(SchemaRefusal, match="collide with Unit 20's derived review-flag names"):
        build_reshoot_delta_record(dissent)


def test_the_condition_vocabulary_still_matches_unit_19s_own_failure_names():
    """Open pair findings must not let the documented condition names drift.

    The expected names are pinned as literals, independent of the
    implementation constants both sides derive from: a coordinated rename of
    ``CONDITION_CODES`` and ``_FAILED_CONDITION_CODES`` together would leave
    the old cross-module equality green while every documented consumer still
    expects these exact spellings.
    """
    documented_condition_codes = {
        "capture-occlusion-condition-failed",
        "capture-comparability-condition-failed",
        "same-ink-condition-failed",
        "identical-run-configuration-condition-failed",
        "cross-capture-match-failure",
    }
    assert CONDITION_CODES == documented_condition_codes
    assert CONDITION_CODES == set(_FAILED_CONDITION_CODES.values())


def test_a_same_length_non_hex_dissent_digest_is_refused_as_malformed():
    """A digest-shaped field must be checked as a digest, not merely as 64 characters.

    A 64-character string that is not lowercase hex can never equal a real
    ``digest_of`` result, but the record must be refused for naming a malformed
    digest rather than falling through to the reproduction check for its reason.
    """
    dissent = _record()
    record = build_reshoot_delta_record(dissent)
    tampered = {**record, "dissent_digest": "g" * 64}
    tampered["self_hash"] = self_hash(
        {key: value for key, value in tampered.items() if key != "self_hash"}
    )
    with pytest.raises(SchemaRefusal, match="dissent digest is malformed"):
        validate_reshoot_delta_record(tampered, dissent)


def test_the_denominator_caveat_binds_and_cannot_be_reworded_or_dropped():
    dissent = _record()
    record = build_reshoot_delta_record(dissent)
    assert "counts nothing" in record["denominator_caveat"]
    assert "pair denominator" in record["denominator_caveat"]
    softened = {
        **record,
        "denominator_caveat": DENOMINATOR_CAVEAT.replace("is not a measurement", "is an estimate"),
    }
    softened["self_hash"] = self_hash(
        {key: value for key, value in softened.items() if key != "self_hash"}
    )
    with pytest.raises(SchemaRefusal, match="denominator caveat"):
        validate_reshoot_delta_record(softened, dissent)
    dropped = {key: value for key, value in record.items() if key != "denominator_caveat"}
    with pytest.raises(SchemaRefusal, match="closed shape"):
        validate_reshoot_delta_record(dropped, dissent)


@pytest.mark.parametrize(
    ("path", "value", "cause"),
    (
        (("pair_records",), None, "pair_records is not a list"),
        (("review_flags",), None, "review_flags is not a list"),
        (("pair_records", 0, "pair_id"), [], "pair rows are malformed"),
        (("pair_records", 0, "comparison_state"), [], "whether it was compared"),
        (("pair_records", 0, "delta_loci"), None, "delta_loci is not a list"),
        (("pair_records", 0, "uncompared_loci"), None, "uncompared_loci is not a list"),
        (("review_flags", 0, "pair_id"), [], "review flag is malformed"),
    ),
)
def test_malformed_collections_and_unhashable_values_are_named_refusals(path, value, cause):
    """A self-hashed malformed derivative is refused, never leaked as a Python error."""
    dissent = _record()
    record = build_reshoot_delta_record(dissent)
    target = record
    for segment in path[:-1]:
        target = target[segment]
    target[path[-1]] = value
    record["self_hash"] = self_hash(record)

    with pytest.raises(SchemaRefusal, match=cause):
        validate_reshoot_delta_record(record, dissent)
