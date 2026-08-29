"""Witness geometry may flag unproposed ink but cannot establish act coverage."""

import importlib.util
from pathlib import Path
from types import SimpleNamespace

import pytest

from common.contracts.approval import build_approval_record
from common.contracts.canonical import canonical_bytes, digest_bytes
from common.contracts.errors import ContractError
from common.runtree.store import RECEIPTS_DIR, RunTree
from common.stage import NUDA_APPROVAL_SUBJECT


def _load_perlector():
    path = Path(__file__).resolve().parent / "run.py"
    spec = importlib.util.spec_from_file_location("perlector_native_observation", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


perlector = _load_perlector()


def test_approval_discovery_does_not_open_a_symlink_outside_the_run_tree(tmp_path):
    config_digest = "a" * 64
    tree = RunTree.create(
        tmp_path / "runs",
        "approval-symlink",
        source_manifest=[],
        config_digest=config_digest,
        adapter_recipes={},
        witness_chairs=[],
    )
    record = build_approval_record(
        subject_ids=[NUDA_APPROVAL_SUBJECT],
        action="other",
        reason="test-only sampling approval",
        target_version_hash=config_digest,
        timestamp="2026-08-26T00:00:00Z",
    )
    data = canonical_bytes(record)
    digest = digest_bytes(data)
    outside = tmp_path / "outside-approval.json"
    outside.write_bytes(data)
    receipts = tree.resolve(RECEIPTS_DIR)
    receipts.mkdir(parents=True)
    candidate = receipts / f"{digest}.json"
    candidate.symlink_to(outside)

    context = SimpleNamespace(tree=tree, config_digest=config_digest)
    # The surviving fd-bound scan opens receipts O_NOFOLLOW relative to the
    # directory descriptor, so the redirect is refused at open time — earlier
    # than the draft loop's path resolution this test was first written against.
    with pytest.raises(ContractError, match="without following a redirect"):
        perlector.resolve_sampling_approval(
            context,
            approval_ref=NUDA_APPROVAL_SUBJECT,
            subject=NUDA_APPROVAL_SUBJECT,
        )


def test_approval_discovery_treats_non_object_json_as_untrusted_receipt_bytes(tmp_path):
    config_digest = "a" * 64
    tree = RunTree.create(
        tmp_path / "runs",
        "approval-array",
        source_manifest=[],
        config_digest=config_digest,
        adapter_recipes={},
        witness_chairs=[],
    )
    data = b"[]"
    digest = digest_bytes(data)
    receipt = tree.resolve(f"{RECEIPTS_DIR}/{digest}.json")
    receipt.parent.mkdir(parents=True)
    receipt.write_bytes(data)

    context = SimpleNamespace(tree=tree, config_digest=config_digest)
    # The surviving scan refuses a non-object receipt by name rather than
    # skipping it into an absent-approval diagnostic: louder, and it cannot
    # misattribute untrusted bytes to a missing record.
    with pytest.raises(ContractError, match="is a JSON list, not an object"):
        perlector.resolve_sampling_approval(
            context,
            approval_ref=NUDA_APPROVAL_SUBJECT,
            subject=NUDA_APPROVAL_SUBJECT,
        )


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
    """The record carries geometry it could not have seen, and is skipped anyway.

    With an empty `observed` the inner loop has nothing to walk, so the guard
    could be deleted and this would still pass. The reported box below is what
    makes the assertion capable of failing: it is exactly what would become a
    finding if a record with no presentation were ever walked.
    """
    unpresented = {
        "artifact_id": "t",
        "payload": {
            "presented": {},
            "observed": [
                {
                    "ordinal": 0,
                    "bounds": {"x": 0, "y": 200, "w": 10, "h": 40},
                    "bounds_source": "native",
                }
            ],
        },
    }

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
