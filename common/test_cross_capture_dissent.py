"""Consult §8.5's dissent and Unit 20 handoff claims, as executable checks.

`common/cross_capture_dissent.py` landed with no test module of its own: the
whole `cross-capture-dissent.v1` contract and the Unit 20 seam were exercised
only by one happy-path construction inside
`pipeline/4_perlector/test_cross_capture_cluster_path.py`.  Consult §8.5 names
eight claims for exactly this surface (tests 44-51), of which the composed
fixture pinned parts of two.  "No variance number", "every unordered pair
including failures", and "the caveat says instability is not accuracy" were
sentences in a report rather than properties anything measured -- which is
GOVERNANCE 10's own distinction between a goal and a claim.

Named for what each proves rather than for its consult number, since the
numbering is the consult's index and this file is the repository's evidence.
"""

from __future__ import annotations

from itertools import combinations
from typing import Any

import pytest

from common.contracts.canonical import digest_of, self_hash, verify_self_hash
from common.contracts.errors import SchemaRefusal
from common.cross_capture_dissent import (
    CAVEAT,
    build_cross_capture_dissent,
    unit20_dissent_input,
    validate_cross_capture_dissent,
)

ESTABLISHED_TEXT = "Le dix-neuf aout, acte commun aux deux captures."
A = "a" * 64
B = "b" * 64
C = "c" * 64


def _ref(path: str) -> dict[str, str]:
    return {"relative_path": path, "sha256": digest_of(path)}


def _view(view_id: str, source: str) -> dict[str, Any]:
    return {
        "view_id": view_id,
        "source_sha256": source,
        "region_refs": [_ref(f"crop/{view_id}.png")],
        "visibility_state": "visible",
    }


def _pair(view_ids: list[str], **overrides: Any) -> dict[str, Any]:
    canonical = sorted(view_ids)
    row = {
        "pair_id": f"pair:{digest_of(canonical)}",
        "view_ids": canonical,
        "capture_condition": {"both_unoccluded": True, "comparably_captured": True},
        "same_ink": True,
        "identical_run_configuration": True,
        "act_match_correct": True,
        "finding_codes": [],
    }
    row.update(overrides)
    return row


def _record(**overrides: Any) -> dict[str, Any]:
    views = overrides.pop("views", [_view("view:a", A), _view("view:b", B)])
    view_ids = [view["view_id"] for view in views]
    body: dict[str, Any] = {
        "schema": "cross-capture-dissent.v1",
        "logical_act_id": "pac_0123456789abcdef",
        "perlectio_ref": _ref("4_perlector/artifacts/perlectio/joint.json"),
        "partition_ref": _ref("4_perlector/blobs/physical-act-partition.json"),
        "config_digest": C,
        "model_provenance": {"chair": "perlector", "revision": "fixture"},
        "reader_invocation_ref": _ref("4_perlector/receipts/joint.json"),
        "response_observation_digest": digest_of("observations"),
        "views": views,
        "loci": [
            {
                "locus_id": "locus:0",
                "established_span_or_gap_ref": {"start": 0, "end": 5},
                "comparison_state": "different-across-views",
                "observations": [
                    {
                        "view_id": view_id,
                        "observed_form": form,
                        "image_region_refs": [_ref(f"crop/{view_id}.png")],
                        "reason_codes": [],
                    }
                    for view_id, form in zip(view_ids, ("Maria", "Marta", "Marie"), strict=False)
                ],
            }
        ],
        "pairs": [_pair(list(pair)) for pair in combinations(sorted(view_ids), 2)],
    }
    body.update(overrides)
    return build_cross_capture_dissent(**body)


def test_the_caveat_says_instability_is_not_accuracy_and_agreement_is_not_proof():
    """§8.5/50. The exact fixed wording the consult binds, not a paraphrase."""
    record = _record()
    assert record["caveat"] == CAVEAT
    assert "does not identify the accurate reading" in CAVEAT
    assert "Agreement does not prove accuracy" in CAVEAT


def test_the_caveat_cannot_be_replaced_reworded_or_dropped_by_a_consumer_path():
    """The instrument may not soften its own warning (GOVERNANCE 10)."""
    with pytest.raises(SchemaRefusal):
        _record(caveat="Cross-capture disagreement indicates a likely misreading.")
    # Omitted rather than contradicted: the builder supplies the one wording.
    assert _record()["caveat"] == CAVEAT
    sealed = _record()
    stripped = {key: value for key, value in sealed.items() if key != "caveat"}
    with pytest.raises(SchemaRefusal):
        validate_cross_capture_dissent(stripped)
    reworded = {**sealed, "caveat": CAVEAT.replace("does not", "may not")}
    with pytest.raises(SchemaRefusal):
        validate_cross_capture_dissent(reworded)
    assert unit20_dissent_input(sealed)["caveat"] == CAVEAT


def test_the_record_contains_every_unordered_capture_pair_and_no_variance_number():
    """§8.5/47, both halves, over three views rather than the easy two."""
    views = [_view("view:a", A), _view("view:b", B), _view("view:c", C)]
    record = _record(views=views)
    assert [pair["view_ids"] for pair in sorted(record["pairs"], key=lambda row: row["pair_id"])]
    assert {tuple(pair["view_ids"]) for pair in record["pairs"]} == {
        ("view:a", "view:b"),
        ("view:a", "view:c"),
        ("view:b", "view:c"),
    }
    with pytest.raises(SchemaRefusal):
        _record(
            views=views,
            pairs=[_pair(["view:a", "view:b"]), _pair(["view:a", "view:c"])],
        )


@pytest.mark.parametrize(
    "carrier",
    [
        {"model_provenance": {"chair": "perlector", "capture_variance": 0.31}},
        {"model_provenance": {"chair": "perlector", "legibility_score": 7}},
        {"model_provenance": {"chair": "perlector", "nested": {"confidence": 0.9}}},
    ],
)
def test_no_scalar_quality_claim_can_be_sealed_anywhere_in_the_record(carrier):
    """§6: structural observations and image anchors, never a score or a variance.

    `model_provenance` was the hole -- checked only for being an object, so a
    caller could seal any number at all inside the one record whose whole
    contract is that it makes no such claim.
    """
    with pytest.raises(SchemaRefusal):
        _record(**carrier)


def test_a_deeply_nested_model_provenance_becomes_a_refusal_not_a_recursion_crash():
    """model_provenance is accepted as any object at all (module docstring), so
    a witness response nested past Python's recursion limit must become a
    `SchemaRefusal`, never an uncaught `RecursionError` that would crash the
    whole stage process and take every other logical act down with it."""
    nested: Any = "leaf"
    for _ in range(5000):
        nested = {"chair": "perlector", "nested": nested}
    with pytest.raises(SchemaRefusal, match="nests too deeply"):
        _record(model_provenance=nested)


def test_no_capture_can_be_named_preferred_inside_the_record():
    """§7 shape 1, screened by the register's own shared preference vocabulary."""
    for field in ("preferred", "best_capture", "winner_view", "primary_observation"):
        with pytest.raises(SchemaRefusal, match="preference"):
            _record(model_provenance={"chair": "perlector", field: "view:a"})


def test_dissent_requires_nonempty_provenance_and_sequence_denominators():
    with pytest.raises(SchemaRefusal, match="provenance is absent or empty"):
        _record(model_provenance={})
    sealed = _record()
    body = {key: value for key, value in sealed.items() if key != "self_hash"}
    for field in ("views", "pairs", "loci"):
        with pytest.raises(SchemaRefusal, match=rf"{field} are not a list"):
            build_cross_capture_dissent(**(body | {field: None}))


def test_noncanonical_model_provenance_is_a_named_contract_refusal():
    for provenance in ({1: "non-string key"}, {"temperature": 0.2}):
        with pytest.raises(SchemaRefusal, match="no canonical serial form.*digest-bound JSON"):
            _record(model_provenance=provenance)


def test_a_failed_unit20_condition_is_a_named_pair_finding_not_an_omitted_pair():
    """§8.5/48 and /49, the four conditions failing one at a time and all at once."""
    conditions = (
        (
            "capture_condition",
            {"both_unoccluded": False, "comparably_captured": True},
            "capture-occlusion-condition-failed",
        ),
        ("same_ink", False, "same-ink-condition-failed"),
        (
            "identical_run_configuration",
            False,
            "identical-run-configuration-condition-failed",
        ),
        ("act_match_correct", False, "cross-capture-match-failure"),
    )
    for field, value, code in conditions:
        record = _record(
            pairs=[_pair(["view:a", "view:b"], **{field: value}, finding_codes=[code])]
        )
        (pair,) = record["pairs"]
        assert pair["view_ids"] == ["view:a", "view:b"]
        assert pair["finding_codes"] == [code]
    all_failed = _record(
        pairs=[
            _pair(
                ["view:a", "view:b"],
                capture_condition={"both_unoccluded": False, "comparably_captured": False},
                same_ink=False,
                identical_run_configuration=False,
                act_match_correct=False,
                finding_codes=[
                    "capture-occlusion-condition-failed",
                    "capture-comparability-condition-failed",
                    "same-ink-condition-failed",
                    "identical-run-configuration-condition-failed",
                    "cross-capture-match-failure",
                ],
            )
        ]
    )
    # The pair the plan's conditions all reject is still one row of the
    # denominator Unit 20 divides by, and it still crosses the seam.
    (failed,) = all_failed["pairs"]
    assert failed["act_match_correct"] is False
    assert unit20_dissent_input(all_failed)["pairs"] == all_failed["pairs"]


def test_a_failed_pair_condition_cannot_travel_unnamed_or_under_an_unrelated_code():
    for codes in ([], ["some-other-finding"]):
        with pytest.raises(SchemaRefusal, match="same-ink-condition-failed.*Unit 20 pair"):
            _record(
                pairs=[
                    _pair(
                        ["view:a", "view:b"],
                        same_ink=False,
                        finding_codes=codes,
                    )
                ]
            )


def test_a_passed_pair_condition_cannot_carry_its_failure_finding():
    with pytest.raises(SchemaRefusal, match="passed condition.*same-ink-condition-failed"):
        _record(
            pairs=[
                _pair(
                    ["view:a", "view:b"],
                    same_ink=True,
                    finding_codes=["same-ink-condition-failed"],
                )
            ]
        )


def test_resealing_cannot_hide_an_unnamed_failed_pair_condition():
    forged = _record()
    forged["pairs"][0]["same_ink"] = False
    forged["self_hash"] = self_hash(forged)
    with pytest.raises(SchemaRefusal, match="same-ink-condition-failed.*Unit 20 pair"):
        validate_cross_capture_dissent(forged)


def test_one_active_capture_after_retraction_has_one_view_and_an_empty_pair_denominator():
    record = _record(views=[_view("view:a", A)])
    assert [view["source_sha256"] for view in record["views"]] == [A]
    assert record["pairs"] == []
    assert unit20_dissent_input(record)["pairs"] == []


def test_dissent_structural_ids_are_normalization_stable_and_sources_are_unique():
    decomposed = "view:e\u0301"
    with pytest.raises(SchemaRefusal, match="printable NFC identity"):
        _record(views=[_view(decomposed, A)])
    with pytest.raises(SchemaRefusal, match="repeat a view/capture identity"):
        _record(views=[_view("view:a", A), _view("view:alias", A)])


def test_each_locus_accounts_for_every_view_exactly_once_and_under_its_own_anchors():
    record = _record()
    locus = record["loci"][0]
    with pytest.raises(SchemaRefusal, match="loci repeat an identity"):
        _record(loci=[locus, locus])
    repeated = {
        **locus,
        "observations": [locus["observations"][0], locus["observations"][0]],
    }
    with pytest.raises(SchemaRefusal, match="one capture cannot count twice"):
        _record(loci=[repeated])
    omitted = {**locus, "observations": locus["observations"][:-1]}
    with pytest.raises(SchemaRefusal, match="omits view observation.*every capture"):
        _record(loci=[omitted])
    borrowed = {
        **locus,
        "observations": [
            {**locus["observations"][0], "image_region_refs": [_ref("crop/view:b.png")]},
            *locus["observations"][1:],
        ],
    }
    with pytest.raises(SchemaRefusal, match="outside that view.*cannot borrow"):
        _record(loci=[borrowed])


def test_the_established_text_cannot_travel_in_a_locus_anchor():
    """The one record built to hold no text may not hold it in a free-form field.

    `established_span_or_gap_ref` passed through entirely unchecked, so the
    established string fitted in it exactly as well as an offset span did.
    """
    for anchor in (ESTABLISHED_TEXT, {"text": ESTABLISHED_TEXT}, ["Maria"], 5):
        with pytest.raises(SchemaRefusal):
            _record(
                loci=[
                    {
                        "locus_id": "locus:0",
                        "established_span_or_gap_ref": anchor,
                        "comparison_state": "unreadable",
                        "observations": [
                            {
                                "view_id": view_id,
                                "observed_form": None,
                                "image_region_refs": [_ref(f"crop/{view_id}.png")],
                                "reason_codes": ["illegible"],
                            }
                            for view_id in ("view:a", "view:b")
                        ],
                    }
                ]
            )
    # The two shapes the field's own name allows both pass.
    assert _record()["loci"][0]["established_span_or_gap_ref"] == {"start": 0, "end": 5}
    gap = _record(
        loci=[
            {
                "locus_id": "locus:0",
                "established_span_or_gap_ref": _ref("4_perlector/artifacts/gap/joint.json"),
                "comparison_state": "unreadable",
                "observations": [
                    {
                        "view_id": view_id,
                        "observed_form": None,
                        "image_region_refs": [_ref(f"crop/{view_id}.png")],
                        "reason_codes": ["illegible"],
                    }
                    for view_id in ("view:a", "view:b")
                ],
            }
        ]
    )
    assert set(gap["loci"][0]["established_span_or_gap_ref"]) == {"relative_path", "sha256"}


def test_no_established_text_is_reachable_through_the_unit20_seam():
    """§8.5/51 and consult §10.13's seam contents, checked to any depth.

    The composed fixture asserted `"text" not in unit20_input`, which is a
    top-level key test: an observed form nested two levels down inside a
    projected `loci` list would have satisfied it.  This walks the whole
    projection instead, and pins the seam's exact contents so a later widening
    is a deliberate edit rather than a drift.
    """
    record = _record()
    seam = unit20_dissent_input(record)
    assert set(seam) == {"schema", "logical_act_id", "perlectio_ref", "pairs", "caveat"}
    assert seam["logical_act_id"] == record["logical_act_id"]
    assert seam["pairs"] == record["pairs"]

    def strings(value: Any):
        if isinstance(value, dict):
            for key, item in value.items():
                yield str(key)
                yield from strings(item)
        elif isinstance(value, list):
            for item in value:
                yield from strings(item)
        elif isinstance(value, str):
            yield value

    reachable = list(strings(seam))
    assert ESTABLISHED_TEXT not in reachable
    # Every observed form the record holds -- the closest thing in it to a
    # rival reading -- stays behind the seam.
    for locus in record["loci"]:
        for observation in locus["observations"]:
            assert observation["observed_form"] not in reachable
    assert not any("variance" in fragment.lower() for fragment in reachable)


def test_the_seam_refuses_a_record_that_is_not_its_own_sealed_form():
    """The projection is a validated read, never a reformat of what it is handed."""
    record = _record()
    tampered = {**record, "logical_act_id": "pac_ffffffffffffffff"}
    assert not verify_self_hash(tampered)
    with pytest.raises(SchemaRefusal):
        unit20_dissent_input(tampered)
    dropped_pair = {**record, "pairs": []}
    with pytest.raises(SchemaRefusal):
        unit20_dissent_input(dropped_pair)
