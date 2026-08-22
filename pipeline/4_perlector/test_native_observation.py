"""Unit 10B's non-authoritative geometry routing check."""

import importlib.util
from pathlib import Path


def _load_perlector():
    path = Path(__file__).resolve().parent / "run.py"
    spec = importlib.util.spec_from_file_location("perlector_native_observation", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


perlector = _load_perlector()


def _testimony(bounds, *, bounds_source="native", artifact_id="testimonium-native"):
    return {
        "artifact_id": artifact_id,
        "payload": {
            "presented": {"source_page_id": "page-1"},
            "observed": [{"ordinal": 0, "bounds": bounds, "bounds_source": bounds_source}],
        },
    }


def _proposal(bounds, *, page="page-1"):
    return {
        "payload": {
            "origin": "proposal",
            "transform": {"source_page_id": page, "bounds": bounds},
        }
    }


def test_native_geometry_outside_every_sealed_proposal_is_a_named_nonfatal_finding():
    findings = perlector.unrouted_observations(
        [_testimony({"x": 0, "y": 200, "w": 10, "h": 40})],
        [_proposal({"x": 12, "y": 15, "w": 188, "h": 99})],
    )
    assert findings == [
        {
            "kind": "unrouted-observation",
            "testimonium_id": "testimonium-native",
            "ordinal": 0,
            "source_page_id": "page-1",
            "bounds": {"x": 0, "y": 200, "w": 10, "h": 40},
            "overlap_rule": {"rule": "positive-area", "status": "unmeasured"},
        }
    ]


def test_native_geometry_overlapping_a_proposal_is_silent_and_prior_finding_is_not_repeated():
    testimony = _testimony({"x": 20, "y": 20, "w": 160, "h": 80})
    assert (
        perlector.unrouted_observations(
            [testimony], [_proposal(testimony["payload"]["observed"][0]["bounds"])]
        )
        == []
    )
    assert (
        perlector.unrouted_observations(
            [_testimony({"x": 0, "y": 200, "w": 10, "h": 40})],
            [_proposal({"x": 12, "y": 15, "w": 188, "h": 99})],
            prior_findings={("testimonium-native", 0)},
        )
        == []
    )


def test_ink_a_neighbouring_act_already_proposes_is_not_unaccounted_ink():
    """The denominator is the page's whole proposal set. Scoped to the reading
    act's own regions, a witness box belonging to the act NEXT to it read as ink
    nobody had proposed -- and this exact shape fired in the pinned happy run,
    where the fixture's marginal box overlapped act a2's crop while act a1 was
    being read. Eleven such findings per box on a twelve-act page, each one an
    invitation to spend a recovery unit on ink the Designator already marked out
    (GOALS 1 is about ink nobody claimed, not ink this act did not claim)."""
    neighbour = _proposal({"x": 12, "y": 114, "w": 188, "h": 124})
    this_act = _proposal({"x": 12, "y": 15, "w": 188, "h": 99})
    box = _testimony({"x": 0, "y": 230, "w": 20, "h": 20})

    assert perlector.unrouted_observations([box], [this_act]) != []
    assert perlector.unrouted_observations([box], [this_act, neighbour]) == []


def test_a_box_on_another_page_is_judged_against_that_page_s_proposals_only():
    box = _testimony({"x": 12, "y": 16, "w": 188, "h": 75})
    far_side = _proposal({"x": 12, "y": 16, "w": 188, "h": 75}, page="page-2")
    assert perlector.unrouted_observations([box], [far_side]) != []
    assert (
        perlector.unrouted_observations(
            [box], [far_side, _proposal(box["payload"]["observed"][0]["bounds"])]
        )
        == []
    )


def test_a_recovery_region_is_not_part_of_the_routing_denominator():
    """A recovery crop is ink the pipeline went back for; it is not evidence the
    Designator's partition ever claimed that ink."""
    recovery = {
        "payload": {
            "origin": "recovery",
            "transform": {
                "source_page_id": "page-1",
                "bounds": {"x": 0, "y": 0, "w": 200, "h": 114},
            },
        }
    }
    assert (
        perlector.unrouted_observations(
            [_testimony({"x": 0, "y": 0, "w": 20, "h": 20})], [recovery]
        )
        != []
    )


def test_a_restatement_of_the_presented_image_is_not_reported_ink():
    """`bounds_source: "presented"` is this pipeline echoing the box it presented,
    not a witness reporting ink. On a page with no proposals at all, routing one
    would raise a coverage finding about ink no witness ever claimed to see."""
    echo = _testimony({"x": 0, "y": 0, "w": 200, "h": 260}, bounds_source="presented")
    assert perlector.unrouted_observations([echo], []) == []
    assert (
        perlector.unrouted_observations([_testimony({"x": 0, "y": 0, "w": 200, "h": 260})], [])
        != []
    )


def test_every_degenerate_corner_box_is_reported_separately_and_none_reads_as_coverage():
    """Ordinal-dense one-pixel boxes in a corner are schema-compliant (see
    `common/test_native_witness.py`). The honest outcome is one named finding
    each, not silence and not an act."""
    testimony = {
        "artifact_id": "testimonium-degenerate",
        "payload": {
            "presented": {"source_page_id": "page-1"},
            "observed": [
                {
                    "ordinal": index,
                    "bounds": {"x": 0, "y": 0, "w": 1, "h": 1},
                    "bounds_source": "native",
                }
                for index in range(4)
            ],
        },
    }
    findings = perlector.unrouted_observations(
        [testimony], [_proposal({"x": 12, "y": 15, "w": 188, "h": 99})]
    )
    assert [finding["ordinal"] for finding in findings] == [0, 1, 2, 3]


def test_an_unpresented_testimonium_contributes_no_observation():
    unpresented = {"artifact_id": "t", "payload": {"presented": {}, "observed": []}}
    assert perlector.unrouted_observations([unpresented], []) == []


def test_the_same_overlap_derivation_covers_act_and_page_testimonia_symmetrically():
    outside = {"x": 0, "y": 200, "w": 10, "h": 40}
    testimonia = [
        _testimony(outside, artifact_id="act-testimonium"),
        _testimony(outside, artifact_id="page-testimonium"),
    ]
    findings = perlector.unrouted_observations(
        testimonia, [_proposal({"x": 12, "y": 15, "w": 188, "h": 99})]
    )
    assert [finding["testimonium_id"] for finding in findings] == [
        "act-testimonium",
        "page-testimonium",
    ]


def test_the_runwide_proposal_denominator_verifies_every_region_before_using_geometry(monkeypatch):
    proposal = {"artifact_id": "proposal", **_proposal({"x": 12, "y": 15, "w": 20, "h": 20})}
    recovery = {
        "artifact_id": "recovery",
        "payload": {
            "origin": "recovery",
            "provenance": {},
            "transform": {
                "source_page_id": "page-1",
                "bounds": {"x": 0, "y": 0, "w": 30, "h": 30},
            },
        },
    }
    proposal["payload"]["provenance"] = {}
    records = {row["artifact_id"]: row for row in (proposal, recovery)}

    class Tree:
        @staticmethod
        def build_manifest(stage):
            return {
                "artifacts": [
                    {"kind": "region", "artifact_id": "proposal"},
                    {"kind": "region", "artifact_id": "recovery"},
                ]
            }

        @staticmethod
        def read_artifact(stage, kind, artifact_id):
            return records[artifact_id]

    class Context:
        tree = Tree()

    checked = []
    monkeypatch.setattr(perlector, "validate_serving_provenance", lambda *args, **kwargs: None)
    monkeypatch.setattr(
        perlector, "verify_region", lambda context, record: checked.append(record["artifact_id"])
    )
    assert perlector.sealed_proposal_regions(Context()) == [proposal]
    assert checked == ["proposal", "recovery"]
