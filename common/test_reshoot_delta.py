"""Unit 20's re-shoot instrument is structural and non-evaluative."""

from __future__ import annotations

from typing import Any

import pytest

from common.contracts.canonical import self_hash
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
    """Plan §20's four conditions probed together, not only one at a time.

    The happy-path and single-failure cases above prove the gate opens and
    closes correctly for one broken condition; they do not prove a pair with
    *several* broken conditions keeps every one of them named rather than only
    the first found, or the last written.
    """
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
    """Plan §20's bias warning: a hard-to-match pair must not silently drop.

    Two clean pairs and one match-failing pair over three views -- the failing
    pair (the hardest one to reconcile, per the plan's own bias warning) must
    remain a named row in the denominator beside the two that pass, not
    disappear from it.
    """
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


def _locus(
    forms: tuple[Any, ...],
    *,
    state: str = "different-across-views",
    view_ids: tuple[str, ...] = ("view:a", "view:b"),
) -> dict[str, Any]:
    """One locus with an explicit per-view form; `None` form means unread there."""
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
    """GOVERNANCE 10 at the seam a delta count is quoted from.

    `delta_loci: []` carried two incompatible meanings -- "compared, and the
    captures did not differ" and "never eligible for a comparison at all" --
    and a reader was expected to recover which by inspecting `finding_codes`.
    That inference is not merely awkward, it is *wrong* in a shape Unit 19
    permits: a pair may name a finding while every one of the four conditions
    holds, in which case non-empty findings sit beside a real delta. So the
    pair states its own comparison state, and the ambiguous inference is gone.
    """
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

    # The case the old inference got wrong: findings present, conditions intact.
    noted = _record(pairs=[_pair(["view:a", "view:b"], finding_codes=["capture-note-recorded"])])
    still_compared = _only_pair(build_reshoot_delta_record(noted))
    assert still_compared["comparison_state"] == "compared"
    assert still_compared["finding_codes"] == ["capture-note-recorded"]
    assert still_compared["delta_loci"] != []


def test_a_locus_one_capture_could_not_read_is_named_rather_than_read_as_agreement():
    """The instrument may not filter what it measures (GOVERNANCE 10, 2).

    A locus read at one capture and unread at the other is the single most
    informative row this instrument can hold, and it used to produce nothing at
    all: no delta, no finding, no flag -- a record identical to a pair whose two
    captures agreed perfectly. Every way a compared pair can fail to compare a
    locus now leaves a named row and a review flag behind.
    """
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

    missing_row = _locus(("Maria",), state="not-comparable", view_ids=("view:a",))
    unobserved = _only_pair(build_reshoot_delta_record(_record(loci=[missing_row])))
    assert unobserved["delta_loci"] == []
    (row,) = unobserved["uncompared_loci"]
    assert row["reason_codes"] == [
        "locus-not-observed-at-every-view-of-the-pair",
        "locus-recorded-not-comparable-across-views",
    ]


def test_a_locus_the_reader_recorded_as_not_comparable_is_not_compared_here():
    """The reader saw the ink; the consumer did not.

    Both observed forms are present and they differ, so a naive string compare
    would call this a delta. `comparison_state` says the joint reader could not
    compare the locus across views, and the instrument does not overrule the
    only party that performed the autopsia -- it names the locus instead.
    """
    record = build_reshoot_delta_record(
        _record(loci=[_locus(("Maria", "Marta"), state="not-comparable")])
    )
    pair = _only_pair(record)
    assert pair["delta_loci"] == []
    assert [row["reason_codes"] for row in pair["uncompared_loci"]] == [
        ["locus-recorded-not-comparable-across-views"]
    ]


def test_at_a_compared_pair_every_locus_is_a_delta_a_named_reason_or_an_agreement():
    """The locus rule is total, so an absent row can only mean one thing.

    The record does not repeat a row for a locus both captures read identically
    -- that would put a row for every locus under every pair and say nothing.
    What makes the omission safe is that it is now exhaustive: after the
    uncompared rows exist, a locus with no row at a compared pair is a locus
    whose observed forms were present and equal at both views of that pair, and
    nothing else. This pins that partition so a future edit cannot quietly
    return a fourth outcome to it.
    """
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
    # locus:1 is unread at view:b, so it is named at the two pairs containing
    # view:b and is an ordinary agreement at the view:a/view:c pair.
    named_for_locus_one = {
        tuple(pair["view_ids"])
        for pair in record["pair_records"]
        for row in pair["uncompared_loci"]
        if row["locus_id"] == "locus:1"
    }
    assert named_for_locus_one == {("view:a", "view:b"), ("view:b", "view:c")}


def test_an_upstream_finding_this_unit_does_not_recognise_is_carried_not_curated():
    """The instrument may not decide what may be reported to it.

    Unit 19 requires a pair to name its failed conditions and permits it to name
    more. Validation used to require every code to be one of the five condition
    codes, so `build_...` produced records `validate_...` refused, and the only
    way to make one validate was for a producer to drop the finding it had
    named. Dropping a named finding to satisfy a downstream vocabulary is
    exactly what GOVERNANCE 2 forbids.
    """
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


def test_the_condition_vocabulary_still_matches_unit_19s_own_failure_names():
    """Reconciliation in place of the constraint that was removed.

    Dropping the closed-vocabulary check is what lets an unrecognised upstream
    finding through; it must not also let the five names this unit documents
    drift away from the five Unit 19 actually emits.
    """
    assert CONDITION_CODES == set(_FAILED_CONDITION_CODES.values())


def test_the_denominator_caveat_binds_and_cannot_be_reworded_or_dropped():
    """The second caveat is sealed exactly as the inherited one is."""
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
