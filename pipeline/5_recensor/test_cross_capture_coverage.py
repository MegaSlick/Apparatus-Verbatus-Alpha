"""Unit 19C coverage is an act-surface denominator, never a page-ink proxy."""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

from common.contracts.errors import SchemaRefusal
from common.cross_capture_coverage import (
    build_cross_capture_coverage,
    capture_specific_recovery,
    same_chair_witness_floor,
)

ROOT = Path(__file__).resolve().parents[2]
FIXTURE = ROOT / "pipeline/4_perlector/fixtures/two-capture-leaf-cluster.json"
A = "a" * 64
B = "b" * 64


def _component(*, a_state="occluded", b_state="visible", a_visible=(), b_visible=((0, 0), (1, 0))):
    return {
        "physical_page_id": "ppg_fixture",
        "expected_cells": [[0, 0], [1, 0]],
        "required_capture_sha256s": [A, B],
        "captures": [
            {
                "source_sha256": A,
                "alignment_ref": "a-to-page",
                "visibility_state": a_state,
                "visible_cells": [list(x) for x in a_visible],
                "occluded_cells": [[0, 0], [1, 0]] if a_state == "occluded" else [],
                "occlusion_refs": ["occ:a"] if a_state == "occluded" else [],
                "finding_codes": [],
            },
            {
                "source_sha256": B,
                "alignment_ref": "b-to-page",
                "visibility_state": b_state,
                "visible_cells": [list(x) for x in b_visible],
                "occluded_cells": [[0, 0], [1, 0]] if b_state == "occluded" else [],
                "occlusion_refs": ["occ:b"] if b_state == "occluded" else [],
                "finding_codes": [],
            },
        ],
    }


def test_occluded_in_one_capture_visible_in_another_has_full_logical_coverage():
    record = build_cross_capture_coverage(logical_act_id="pac_fixture", components=[_component()])
    assert record["act_state"] == "full"
    assert record["components"][0]["union_state"] == "full"


def test_complementary_visible_surfaces_union_to_full_logical_coverage():
    component = _component(
        a_state="occluded", b_state="occluded", a_visible=((0, 0),), b_visible=((1, 0),)
    )
    component["captures"][0]["occluded_cells"] = [[1, 0]]
    component["captures"][1]["occluded_cells"] = [[0, 0]]
    record = build_cross_capture_coverage(logical_act_id="pac_fixture", components=[component])
    assert record["act_state"] == "full"


def test_every_capture_explicitly_occluded_emits_occluded_everywhere_and_holds():
    record = build_cross_capture_coverage(
        logical_act_id="pac_fixture", components=[_component(b_state="occluded", b_visible=())]
    )
    assert record["act_state"] == "occluded-everywhere"
    assert record["findings"] == [
        {"code": "occluded-everywhere", "physical_page_id": "ppg_fixture"}
    ]


def test_unmeasured_capture_is_unresolved_not_occluded_everywhere():
    component = _component(b_state="unresolved", b_visible=())
    component["captures"][1]["occluded_cells"] = []
    record = build_cross_capture_coverage(logical_act_id="pac_fixture", components=[component])
    assert record["act_state"] == "unresolved"
    assert record["findings"][0]["code"] == "capture-visibility-unresolved"


def _nine_cells():
    return [[x, y] for x in range(3) for y in range(3)]


def test_a_one_pixel_visible_sliver_defeats_occluded_everywhere():
    """Even when every capture explicitly occludes the rest, one seen cell
    anywhere means the component was never fully unseen, so the exact
    ``occluded-everywhere`` claim may not be made -- it degrades to
    ``unresolved``, never silently rounds up to ``full`` either."""
    all_cells = _nine_cells()
    rest = [c for c in all_cells if c != [0, 0]]
    component = {
        "physical_page_id": "ppg_sliver",
        "expected_cells": all_cells,
        "required_capture_sha256s": [A, B],
        "captures": [
            {
                "source_sha256": A,
                "alignment_ref": "a-to-page",
                "visibility_state": "occluded",
                "visible_cells": [],
                "occluded_cells": all_cells,
                "occlusion_refs": ["occ:a"],
                "finding_codes": [],
            },
            {
                "source_sha256": B,
                "alignment_ref": "b-to-page",
                "visibility_state": "occluded",
                "visible_cells": [[0, 0]],
                "occluded_cells": rest,
                "occlusion_refs": ["occ:b"],
                "finding_codes": [],
            },
        ],
    }
    record = build_cross_capture_coverage(logical_act_id="pac_sliver", components=[component])
    assert record["components"][0]["union_visible_cells"] == [[0, 0]]
    assert record["components"][0]["union_state"] == "unresolved"
    assert record["act_state"] == "unresolved"
    assert record["findings"] == [
        {"code": "capture-visibility-unresolved", "physical_page_id": "ppg_sliver"}
    ]


def test_a_wholly_unseen_component_is_still_exactly_occluded_everywhere():
    """The sliver fix must not weaken the true zero-visibility case."""
    all_cells = _nine_cells()
    component = {
        "physical_page_id": "ppg_blind",
        "expected_cells": all_cells,
        "required_capture_sha256s": [A, B],
        "captures": [
            {
                "source_sha256": A,
                "alignment_ref": "a-to-page",
                "visibility_state": "occluded",
                "visible_cells": [],
                "occluded_cells": all_cells,
                "occlusion_refs": ["occ:a"],
                "finding_codes": [],
            },
            {
                "source_sha256": B,
                "alignment_ref": "b-to-page",
                "visibility_state": "occluded",
                "visible_cells": [],
                "occluded_cells": all_cells,
                "occlusion_refs": ["occ:b"],
                "finding_codes": [],
            },
        ],
    }
    record = build_cross_capture_coverage(logical_act_id="pac_blind", components=[component])
    assert record["components"][0]["union_state"] == "occluded-everywhere"
    assert record["act_state"] == "occluded-everywhere"


def test_same_chair_witnessing_the_same_surface_in_both_captures_counts_once():
    """A chair that attaches to the *identical* component in two captures is
    still one chair against the floor, not two -- no per-capture credit."""
    rows = [
        {
            "chair": "attestator_1",
            "capture": A,
            "attached": True,
            "comparable": True,
            "components": ["whole"],
        },
        {
            "chair": "attestator_1",
            "capture": B,
            "attached": True,
            "comparable": True,
            "components": ["whole"],
        },
    ]
    result = same_chair_witness_floor(rows, components={"whole"}, floor=1)
    assert result["counted_chairs"] == ["attestator_1"]
    assert result["count"] == 1
    assert result["under_witnessed"] is False


def test_cross_component_cell_coordinates_do_not_leak_between_physical_pages():
    """Two components on different physical pages may share numerically
    identical cell coordinates; a mutation that lets one component's
    visibility answer the other's denominator must be caught."""
    occluded_component = _component(a_state="occluded", b_state="occluded", b_visible=())
    occluded_component["physical_page_id"] = "ppg_occluded"
    visible_component = _component(a_state="visible", b_state="visible", a_visible=((0, 0), (1, 0)))
    visible_component["physical_page_id"] = "ppg_visible"
    record = build_cross_capture_coverage(
        logical_act_id="pac_two_pages", components=[occluded_component, visible_component]
    )
    by_page = {row["physical_page_id"]: row for row in record["components"]}
    assert by_page["ppg_occluded"]["union_state"] == "occluded-everywhere"
    assert by_page["ppg_visible"]["union_state"] == "full"
    assert record["act_state"] == "unresolved"


def test_same_chair_counts_once_only_after_its_comparable_rows_cover_every_component():
    rows = [
        {
            "chair": "attestator_1",
            "capture": A,
            "attached": True,
            "comparable": True,
            "components": ["left"],
        },
        {
            "chair": "attestator_1",
            "capture": B,
            "attached": True,
            "comparable": True,
            "components": ["right"],
        },
        {
            "chair": "attestator_2",
            "capture": B,
            "attached": True,
            "comparable": False,
            "components": ["left", "right"],
        },
    ]
    result = same_chair_witness_floor(rows, components={"left", "right"}, floor=2)
    assert result == {
        "configured_floor": 2,
        "counted_chairs": ["attestator_1"],
        "count": 1,
        "under_witnessed": True,
    }


@pytest.mark.parametrize(
    ("rows", "components", "floor"),
    [
        (None, {"whole"}, 1),
        ([], {"whole"}, True),
        (
            [
                {
                    "chair": "attestator_1",
                    "capture": A,
                    "attached": True,
                    "comparable": True,
                    "components": 1,
                }
            ],
            {"whole"},
            1,
        ),
        (
            [
                {
                    "chair": "attestator_1",
                    "capture": A,
                    "attached": True,
                    "comparable": True,
                    "components": [{}],
                }
            ],
            {"whole"},
            1,
        ),
    ],
)
def test_malformed_witness_floor_inputs_are_named_schema_refusals(rows, components, floor):
    with pytest.raises(SchemaRefusal):
        same_chair_witness_floor(rows, components=components, floor=floor)


def test_duplicate_chair_capture_facts_refuse_instead_of_keeping_the_positive_row():
    rows = [
        {
            "chair": "attestator_1",
            "capture": A,
            "attached": attached,
            "comparable": attached,
            "components": ["whole"],
        }
        for attached in (False, True)
    ]
    with pytest.raises(SchemaRefusal, match="repeats a chair/capture fact"):
        same_chair_witness_floor(rows, components={"whole"}, floor=1)


def test_unhashable_visibility_state_is_a_named_schema_refusal():
    component = _component()
    component["captures"][0]["visibility_state"] = []
    with pytest.raises(SchemaRefusal, match="invalid source or state"):
        build_cross_capture_coverage(logical_act_id="pac_fixture", components=[component])


def test_negative_cell_coordinates_are_named_schema_refusals():
    component = _component()
    component["expected_cells"][0] = [-1, 0]
    with pytest.raises(SchemaRefusal, match="malformed cell"):
        build_cross_capture_coverage(logical_act_id="pac_fixture", components=[component])


def test_cross_capture_visibility_cannot_be_passed_to_unit14b_page_denominators():
    spec = importlib.util.spec_from_file_location(
        "recensor_19c", ROOT / "pipeline/5_recensor/run.py"
    )
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(module)
    assert "cross_capture_coverage" not in module.page_coverage_findings.__code__.co_names
    assert FIXTURE.exists(), "the 19B two-capture fixture remains the shared evidence path"


def test_capture_specific_recovery_requires_its_own_ink_confirmed_unit14b_trigger():
    denied = capture_specific_recovery(
        logical_act_id="pac_fixture",
        source_sha256=A,
        page_ordinal=1,
        ink_confirmed=False,
        page_observation_grant_available=True,
        act_budget_available=True,
    )
    admitted = capture_specific_recovery(
        logical_act_id="pac_fixture",
        source_sha256=A,
        page_ordinal=1,
        ink_confirmed=True,
        page_observation_grant_available=True,
        act_budget_available=True,
    )
    assert denied["admitted"] is False
    assert "no Unit 14B ink-confirmed observation" in denied["reason"]
    assert "grant is unavailable" not in denied["reason"]
    assert admitted["admitted"] is True
    assert admitted["source_sha256"] == A and admitted["page_ordinal"] == 1


def test_capture_specific_recovery_names_every_missing_conjunct():
    denied = capture_specific_recovery(
        logical_act_id="pac_fixture",
        source_sha256=A,
        page_ordinal=1,
        ink_confirmed=False,
        page_observation_grant_available=False,
        act_budget_available=False,
    )
    assert denied["reason"] == (
        "cross-capture visibility alone cannot fund recovery: this capture has no Unit 14B "
        "ink-confirmed observation; the page observation grant is unavailable; the act "
        "recovery budget is unavailable"
    )


def test_capture_specific_recovery_refuses_a_non_digest_capture_identity():
    with pytest.raises(SchemaRefusal, match="lacks logical-act/capture identity"):
        capture_specific_recovery(
            logical_act_id="pac_fixture",
            source_sha256="not-a-digest",
            page_ordinal=1,
            ink_confirmed=True,
            page_observation_grant_available=True,
            act_budget_available=True,
        )


def test_incomplete_measured_survey_refuses_instead_of_guessing_occlusion():
    component = _component()
    component["captures"][0]["occluded_cells"] = [[0, 0]]
    with pytest.raises(SchemaRefusal, match="complete expected surface"):
        build_cross_capture_coverage(logical_act_id="pac_fixture", components=[component])


def test_occluded_everywhere_cannot_drop_its_occlusion_evidence():
    """The finding may not survive while the evidence behind it disappears."""
    component = _component(b_state="occluded", b_visible=())
    component["captures"][0]["occlusion_refs"] = []
    with pytest.raises(SchemaRefusal, match="carries no occlusion evidence"):
        build_cross_capture_coverage(logical_act_id="pac_fixture", components=[component])


def test_capture_specific_recovery_has_no_visibility_or_geometry_parameter():
    """Pins the gate's surface: it takes only identity and the Unit 14B
    ink-confirmed/grant booleans, so a future caller cannot smuggle in a
    ``visible``/``occluded``/coverage argument and fund recovery from
    geometry alone."""
    import inspect

    params = set(inspect.signature(capture_specific_recovery).parameters)
    assert params == {
        "logical_act_id",
        "source_sha256",
        "page_ordinal",
        "ink_confirmed",
        "page_observation_grant_available",
        "act_budget_available",
    }


def _three_capture_component(*, visible_in):
    """One cell surface seen by a named subset of three registered captures."""
    captures = "abc"
    return {
        "physical_page_id": "ppg_fixture",
        "expected_cells": [[0, 0]],
        "required_capture_sha256s": sorted(letter * 64 for letter in captures),
        "captures": [
            {
                "source_sha256": letter * 64,
                "alignment_ref": f"{letter}-to-page",
                "visibility_state": "visible" if letter in visible_in else "occluded",
                "visible_cells": [[0, 0]] if letter in visible_in else [],
                "occluded_cells": [] if letter in visible_in else [[0, 0]],
                "occlusion_refs": [] if letter in visible_in else [f"occ:{letter}"],
                "finding_codes": [],
            }
            for letter in captures
        ],
    }


def test_a_surface_seen_by_one_capture_is_as_covered_as_one_seen_by_three():
    """Consult §7.5/§7.6: the union is a union, not a tally. A cell seen in
    one of three captures and the same cell seen in all three reach the
    identical coverage state -- nothing here is 'more covered', because a
    count of agreeing captures is one substitution away from a vote."""
    one = build_cross_capture_coverage(
        logical_act_id="pac_fixture", components=[_three_capture_component(visible_in="b")]
    )
    three = build_cross_capture_coverage(
        logical_act_id="pac_fixture", components=[_three_capture_component(visible_in="abc")]
    )
    for record in (one, three):
        assert record["act_state"] == "full"
        assert record["components"][0]["union_state"] == "full"
        assert record["components"][0]["union_visible_cells"] == [[0, 0]]
        assert record["findings"] == []


def test_the_published_coverage_record_carries_no_tally_or_ranking_field():
    """The recursive shape screen for this record specifically. A per-capture
    or per-cell count, score, rank, or percentage would be the arithmetic a
    later reader could sort captures by (consult §7.3-§7.6); the record holds
    cell sets and named states only."""
    record = build_cross_capture_coverage(
        logical_act_id="pac_fixture", components=[_three_capture_component(visible_in="ab")]
    )
    forbidden = (
        "count",
        "score",
        "rank",
        "percent",
        "ratio",
        "total",
        "weight",
        "confidence",
        "quorum",
        "majority",
        "best",
        "primary",
        "preferred",
        "winner",
    )

    def walk(value):
        if isinstance(value, dict):
            for key, item in value.items():
                assert not any(word in str(key).lower() for word in forbidden), key
                walk(item)
        elif isinstance(value, list):
            for item in value:
                walk(item)

    walk(record)
