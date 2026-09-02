"""`compare.py`: read-only IoU assignment and scoring, and the picker fence around it."""

from __future__ import annotations

import ast
import subprocess
from fractions import Fraction
from pathlib import Path

import pytest

from common.contracts.canonical import digest_bytes
from common.contracts.canonical import self_hash as _self_hash
from common.contracts.envelope import build_envelope
from common.contracts.identities import act_id, artifact_id, attempt_id, page_id
from common.contracts.stages import DESIGNATOR, EXEMPLAR
from common.runtree.store import RunTree
from operations.spike_perlector.models import OutputStatus

from . import CorpusRefusal
from .compare import (
    COMPARE_REFUSAL_REASONS,
    MAX_ACTS_PER_PAGE,
    ReadOnlyRunTree,
    compare_page,
    count_excluded_designator_artifacts,
    load_exemplar_page_shas,
    load_pipeline_proposal_acts,
    validate_comparison,
)
from .reference import build_reference_page

ROOT = Path(__file__).resolve().parents[2]
CONFIG_DIGEST = "c" * 64
RUN_ID = "r1"
PAGE_SOURCE_SHA256 = digest_bytes(b"synthetic recordgold page bytes")


def _page(width: int = 2000, height: int = 3000) -> dict:
    return {"sha256": PAGE_SOURCE_SHA256, "width": width, "height": height}


def _record(record_id: str, region: dict, split: str = "val", text: str = "Baptisé Jean") -> dict:
    return {
        "record_id": record_id,
        "region": region,
        "split": split,
        "text": text,
        "text_sha256": digest_bytes(text.encode("utf-8")),
    }


def _reference(records: list[dict]) -> dict:
    return build_reference_page(
        page=_page(),
        source="Ardennes",
        volume="geneanet/Ardennes_BMS/380403",
        designation="00026",
        split="val",
        records=records,
    )


def _pipeline_act(act_id: str, bounds: dict, page_sha256: str = PAGE_SOURCE_SHA256) -> dict:
    return {"act_id": act_id, "bounds": bounds, "page_sha256": page_sha256}


# --- Building a minimal, real run tree ---------------------------------------


def _make_run(tmp_path: Path) -> RunTree:
    source_manifest = [
        {"relative_path": "proof/page-1.jpg", "sha256": PAGE_SOURCE_SHA256, "ordinal": 1}
    ]
    return RunTree.create(
        tmp_path,
        RUN_ID,
        source_manifest=source_manifest,
        config_digest=CONFIG_DIGEST,
        adapter_recipes={"designator": "fake-designator-v0"},
        witness_chairs=["attestator_1"],
    )


def _publish(tree: RunTree, **kwargs) -> None:
    tree.publish_artifact(build_envelope(run_id=RUN_ID, config_digest=CONFIG_DIGEST, **kwargs))


def _seal_page(tree: RunTree, *, ordinal: int = 1) -> str:
    """Seal one Exemplar page and return its `pg_` identity."""
    origin = {"kind": "source", "sha256": PAGE_SOURCE_SHA256}
    transform = {"operation": "whole"}
    identity = page_id(origin, transform)
    _publish(
        tree,
        artifact_id=artifact_id(EXEMPLAR, "page", identity),
        subject_id=identity,
        stage=EXEMPLAR,
        kind="page",
        outcome="sealed",
        adapter_revision="fake-exemplar-v0",
        inputs=[],
        payload={"ordinal": ordinal, "source_sha256": PAGE_SOURCE_SHA256},
    )
    return identity


def _refuse_page(tree: RunTree, *, ordinal: int, reason: str = "declared-hash-mismatch") -> str:
    """Seal one Exemplar page as `outcome: "refused"`, the shape a Door refusal
    produces (`pipeline/1_exemplar/run.py::_refused_page_payload`) -- no
    `source_sha256` at all."""
    origin = {"kind": "source", "sha256": digest_bytes(f"refused source {ordinal}".encode())}
    identity = page_id(origin, {"operation": "whole"})
    _publish(
        tree,
        artifact_id=artifact_id(EXEMPLAR, "page", identity),
        subject_id=identity,
        stage=EXEMPLAR,
        kind="page",
        outcome="refused",
        adapter_revision="fake-exemplar-v0",
        inputs=[],
        payload={
            "ordinal": ordinal,
            "declared_path": f"proof/page-{ordinal}.jpg",
            "declared_sha256": digest_bytes(f"declared {ordinal}".encode()),
            "reason": reason,
        },
    )
    return identity


def _propose_region(tree: RunTree, page_identity: str, *, ordinal: int, bounds: dict) -> str:
    """Seal one Designator proposal region and return its `act_` identity."""
    act_identity = act_id(page_identity, "proposal", bounds)
    attempt = attempt_id(act_identity, "crop", 1)
    transform = {
        "operation": "crop",
        "source_page_ordinal": ordinal,
        "source_page_id": page_identity,
        "bounds": bounds,
    }
    _publish(
        tree,
        artifact_id=artifact_id(DESIGNATOR, "region", act_identity, attempt),
        subject_id=act_identity,
        stage=DESIGNATOR,
        kind="region",
        outcome="proposed",
        adapter_revision="fake-designator-v0",
        inputs=[],
        attempt=attempt,
        payload={"origin": "proposal", "transform": transform, "raw_bounds": bounds},
    )
    return act_identity


def _recovery_region(tree: RunTree, page_identity: str, *, ordinal: int, bounds: dict) -> str:
    """Seal one Designator *recovery* region -- never a proposal, must not enter the matrix."""
    act_identity = act_id(page_identity, "residual", bounds)
    attempt = attempt_id(act_identity, "crop", 1)
    transform = {
        "operation": "crop",
        "source_page_ordinal": ordinal,
        "source_page_id": page_identity,
        "bounds": bounds,
    }
    _publish(
        tree,
        artifact_id=artifact_id(DESIGNATOR, "region", act_identity, attempt),
        subject_id=act_identity,
        stage=DESIGNATOR,
        kind="region",
        outcome="proposed",
        adapter_revision="fake-designator-v0",
        inputs=[],
        attempt=attempt,
        payload={"origin": "recovery", "transform": transform, "raw_bounds": bounds},
    )
    return act_identity


# --- Reading a completed run tree, read-only ---------------------------------


def test_load_exemplar_page_shas_reads_the_sealed_page(tmp_path):
    tree = _make_run(tmp_path)
    _seal_page(tree, ordinal=1)
    shas = load_exemplar_page_shas(tree)
    assert shas == {1: PAGE_SOURCE_SHA256}


def test_load_exemplar_page_shas_skips_a_refused_page_beside_a_sealed_one(tmp_path):
    """A single Door refusal inside an otherwise ordinary run must not crash this
    reader -- the refused page carries no `source_sha256` at all, by design."""
    tree = _make_run(tmp_path)
    _seal_page(tree, ordinal=1)
    _refuse_page(tree, ordinal=2)
    shas = load_exemplar_page_shas(tree)
    assert shas == {1: PAGE_SOURCE_SHA256}


def test_load_exemplar_page_shas_refuses_a_non_integer_ordinal(tmp_path):
    tree = _make_run(tmp_path)
    origin = {"kind": "source", "sha256": PAGE_SOURCE_SHA256}
    identity = page_id(origin, {"operation": "whole"})
    _publish(
        tree,
        artifact_id=artifact_id(EXEMPLAR, "page", identity),
        subject_id=identity,
        stage=EXEMPLAR,
        kind="page",
        outcome="sealed",
        adapter_revision="fake-exemplar-v0",
        inputs=[],
        payload={"ordinal": "1", "source_sha256": PAGE_SOURCE_SHA256},
    )
    with pytest.raises(CorpusRefusal, match="^malformed-record:"):
        load_exemplar_page_shas(tree)


def test_load_exemplar_page_shas_refuses_a_non_digest_source_sha256(tmp_path):
    tree = _make_run(tmp_path)
    origin = {"kind": "source", "sha256": PAGE_SOURCE_SHA256}
    identity = page_id(origin, {"operation": "whole"})
    _publish(
        tree,
        artifact_id=artifact_id(EXEMPLAR, "page", identity),
        subject_id=identity,
        stage=EXEMPLAR,
        kind="page",
        outcome="sealed",
        adapter_revision="fake-exemplar-v0",
        inputs=[],
        payload={"ordinal": 1, "source_sha256": "not-a-digest"},
    )
    with pytest.raises(CorpusRefusal, match="^malformed-record:"):
        load_exemplar_page_shas(tree)


def test_load_exemplar_page_shas_refuses_a_duplicate_ordinal(tmp_path):
    tree = _make_run(tmp_path)
    origin_a = {"kind": "source", "sha256": digest_bytes(b"page a bytes")}
    origin_b = {"kind": "source", "sha256": digest_bytes(b"page b bytes")}
    for origin in (origin_a, origin_b):
        identity = page_id(origin, {"operation": "whole"})
        _publish(
            tree,
            artifact_id=artifact_id(EXEMPLAR, "page", identity),
            subject_id=identity,
            stage=EXEMPLAR,
            kind="page",
            outcome="sealed",
            adapter_revision="fake-exemplar-v0",
            inputs=[],
            payload={"ordinal": 1, "source_sha256": origin["sha256"]},
        )
    with pytest.raises(CorpusRefusal, match="^malformed-record:"):
        load_exemplar_page_shas(tree)


def test_load_pipeline_proposal_acts_excludes_recovery_regions(tmp_path):
    tree = _make_run(tmp_path)
    page_identity = _seal_page(tree, ordinal=1)
    proposal_bounds = {"x": 90, "y": 90, "w": 210, "h": 90}
    proposal_act = _propose_region(tree, page_identity, ordinal=1, bounds=proposal_bounds)
    _recovery_region(tree, page_identity, ordinal=1, bounds={"x": 500, "y": 500, "w": 20, "h": 20})

    acts = load_pipeline_proposal_acts(tree)
    assert [act["act_id"] for act in acts] == [proposal_act]
    assert acts[0]["bounds"] == proposal_bounds
    assert acts[0]["page_sha256"] == PAGE_SOURCE_SHA256


def test_load_pipeline_proposal_acts_reads_raw_bounds_not_the_padded_capture_rectangle(tmp_path):
    """The IoU term must be the structural rectangle, never the padded capture crop.

    `raw_bounds` and `transform.bounds` deliberately differ here, the way a real
    padded proposal cut's do (`2_designator/run.py`'s `apply_padding`) -- if this
    read the padded rectangle instead, `acts[0]["bounds"]` would come back as the
    larger, padded box rather than the detected one.
    """
    tree = _make_run(tmp_path)
    page_identity = _seal_page(tree, ordinal=1)
    raw_bounds = {"x": 100, "y": 100, "w": 200, "h": 80}
    padded_bounds = {"x": 50, "y": 50, "w": 300, "h": 180}
    act_identity = act_id(page_identity, "proposal", raw_bounds)
    attempt = attempt_id(act_identity, "crop", 1)
    transform = {
        "operation": "crop",
        "source_page_ordinal": 1,
        "source_page_id": page_identity,
        "bounds": padded_bounds,
    }
    _publish(
        tree,
        artifact_id=artifact_id(DESIGNATOR, "region", act_identity, attempt),
        subject_id=act_identity,
        stage=DESIGNATOR,
        kind="region",
        outcome="proposed",
        adapter_revision="fake-designator-v0",
        inputs=[],
        attempt=attempt,
        payload={"origin": "proposal", "transform": transform, "raw_bounds": raw_bounds},
    )

    acts = load_pipeline_proposal_acts(tree)
    assert acts[0]["bounds"] == raw_bounds
    assert acts[0]["bounds"] != padded_bounds


def test_load_pipeline_proposal_acts_refuses_an_unresolvable_page_ordinal(tmp_path):
    tree = _make_run(tmp_path)
    page_identity = page_id(
        {"kind": "source", "sha256": PAGE_SOURCE_SHA256}, {"operation": "whole"}
    )
    # No Exemplar page sealed at all -- the region names an ordinal nothing seals.
    _propose_region(tree, page_identity, ordinal=1, bounds={"x": 0, "y": 0, "w": 10, "h": 10})
    with pytest.raises(CorpusRefusal, match="unresolvable-page-ordinal"):
        load_pipeline_proposal_acts(tree)


def test_load_pipeline_proposal_acts_refuses_a_region_with_no_usable_ordinal(tmp_path):
    tree = _make_run(tmp_path)
    page_identity = _seal_page(tree, ordinal=1)
    bounds = {"x": 0, "y": 0, "w": 10, "h": 10}
    act_identity = act_id(page_identity, "proposal", bounds)
    attempt = attempt_id(act_identity, "crop", 1)
    # transform carries no source_page_ordinal at all -- a shape an earlier
    # Designator revision could have sealed.
    _publish(
        tree,
        artifact_id=artifact_id(DESIGNATOR, "region", act_identity, attempt),
        subject_id=act_identity,
        stage=DESIGNATOR,
        kind="region",
        outcome="proposed",
        adapter_revision="fake-designator-v0",
        inputs=[],
        attempt=attempt,
        payload={"origin": "proposal", "transform": {"operation": "crop"}, "raw_bounds": bounds},
    )
    with pytest.raises(CorpusRefusal, match="^malformed-record:"):
        load_pipeline_proposal_acts(tree)


def test_load_pipeline_proposal_acts_refuses_a_region_with_no_raw_bounds(tmp_path):
    tree = _make_run(tmp_path)
    page_identity = _seal_page(tree, ordinal=1)
    bounds = {"x": 0, "y": 0, "w": 10, "h": 10}
    act_identity = act_id(page_identity, "proposal", bounds)
    attempt = attempt_id(act_identity, "crop", 1)
    transform = {
        "operation": "crop",
        "source_page_ordinal": 1,
        "source_page_id": page_identity,
        "bounds": bounds,
    }
    # raw_bounds is entirely absent from the payload.
    _publish(
        tree,
        artifact_id=artifact_id(DESIGNATOR, "region", act_identity, attempt),
        subject_id=act_identity,
        stage=DESIGNATOR,
        kind="region",
        outcome="proposed",
        adapter_revision="fake-designator-v0",
        inputs=[],
        attempt=attempt,
        payload={"origin": "proposal", "transform": transform},
    )
    with pytest.raises(CorpusRefusal, match="^malformed-record:"):
        load_pipeline_proposal_acts(tree)


def test_reads_only_through_a_read_only_wrapper(tmp_path):
    """The functions this module exposes over a run tree touch only read methods.

    `ReadOnlyRunTree` refuses every write outright; running the same read path
    through it proves `compare.py`'s run-tree reading never reaches for one.
    """
    tree = _make_run(tmp_path)
    page_identity = _seal_page(tree, ordinal=1)
    _propose_region(tree, page_identity, ordinal=1, bounds={"x": 90, "y": 90, "w": 210, "h": 90})

    wrapped = ReadOnlyRunTree(tree)
    assert load_exemplar_page_shas(wrapped) == {1: PAGE_SOURCE_SHA256}
    assert len(load_pipeline_proposal_acts(wrapped)) == 1


@pytest.mark.parametrize(
    "method,kwargs",
    [
        ("publish_artifact", {"envelope": {}}),
        ("put_blob", {"stage": DESIGNATOR, "data": b""}),
        ("write_manifest", {"stage": DESIGNATOR}),
        ("write_index", {"stage": DESIGNATOR, "index": {}}),
        ("write_run_receipt", {"receipt": {}}),
        ("write_approval_record", {"record": {}}),
    ],
)
def test_read_only_wrapper_refuses_every_write(tmp_path, method, kwargs):
    tree = _make_run(tmp_path)
    wrapped = ReadOnlyRunTree(tree)
    with pytest.raises(CorpusRefusal, match="run-tree-write-refused"):
        getattr(wrapped, method)(**kwargs)


# --- Assignment and scoring over one page -------------------------------------


def test_matched_pair_is_scored_and_miss_and_unmatched_are_reported():
    reference = _reference(
        [
            _record("rec-1", {"x": 100, "y": 100, "w": 200, "h": 80}, text="Baptisé Jean"),
            _record("rec-2", {"x": 100, "y": 300, "w": 200, "h": 80}, text="Marié Marie"),
        ]
    )
    matched_pipeline_bounds = {"x": 100, "y": 100, "w": 200, "h": 80}  # exact overlap with rec-1
    unmatched_pipeline_bounds = {"x": 900, "y": 900, "w": 50, "h": 50}  # nowhere near either act
    pipeline_acts = [
        _pipeline_act("act_0000000000000001", matched_pipeline_bounds),
        _pipeline_act("act_0000000000000002", unmatched_pipeline_bounds),
    ]
    hypotheses = {"act_0000000000000001": (OutputStatus.COMPLETE, "Baptisé Jean")}

    comparison = compare_page(reference, pipeline_acts, hypotheses)

    assert len(comparison["matched_pairs"]) == 1
    matched = comparison["matched_pairs"][0]
    assert matched["pipeline_act_id"] == "act_0000000000000001"
    assert matched["record_id"] == "rec-1"
    assert matched["cer"]["substitutions"] == 0
    assert matched["cer"]["insertions"] == 0
    assert matched["cer"]["deletions"] == 0

    # rec-2 was never matched: reported as a miss, never dropped.
    assert comparison["misses"] == [
        {"physical_act_id": reference["acts"][1]["physical_act_id"], "record_id": "rec-2"}
    ]

    # The second pipeline act was never matched: reported, not scored.
    assert comparison["unmatched_pipeline_acts"] == [{"act_id": "act_0000000000000002"}]
    scored_act_ids = {pair["pipeline_act_id"] for pair in comparison["matched_pairs"]}
    assert "act_0000000000000002" not in scored_act_ids


def test_unmatched_reference_act_is_never_dropped_even_with_no_pipeline_acts():
    reference = _reference([_record("rec-1", {"x": 0, "y": 0, "w": 100, "h": 100})])
    comparison = compare_page(reference, [], hypotheses={})
    assert comparison["matched_pairs"] == []
    assert comparison["misses"] == [
        {"physical_act_id": reference["acts"][0]["physical_act_id"], "record_id": "rec-1"}
    ]


def test_below_threshold_iou_is_not_an_eligible_match():
    reference = _reference([_record("rec-1", {"x": 0, "y": 0, "w": 100, "h": 100})])
    # Overlaps only slightly -- far below the predeclared 0.5 threshold.
    pipeline_acts = [_pipeline_act("act_0000000000000001", {"x": 90, "y": 90, "w": 100, "h": 100})]
    comparison = compare_page(reference, pipeline_acts, hypotheses={})
    assert comparison["matched_pairs"] == []
    assert comparison["misses"][0]["record_id"] == "rec-1"
    assert comparison["unmatched_pipeline_acts"] == [{"act_id": "act_0000000000000001"}]
    entry = comparison["matrix"][0]
    assert entry["eligible"] is False


def test_assignment_maximises_total_iou_not_a_greedy_first_match():
    """Two reference acts, two pipeline acts, and a greedy match would pick worse.

    `pact-a`'s own best partner is `ref-2` (IoU 17/23, the largest edge on the
    page); its edge to `ref-1` is worse (3/5). `pact-b`'s only eligible partner
    is `ref-2` (2/3) -- its edge to `ref-1` (1/4) falls below the predeclared
    1/2 threshold, so that pair is not an edge at all. A greedy scan -- whether
    it walks pipeline acts in order or simply takes the largest edge first --
    binds `pact-a` to `ref-2` for 17/23 and stops there: `pact-b` has nowhere
    left to go, so `ref-1` goes unmatched and `pact-b` is reported unmatched.
    The exact assignment gives up `pact-a`'s own best partner in favour of the
    pairing worth more in total (`3/5 + 2/3 = 19/15` beats `17/23`), matching
    every act.
    """
    ref1_region = {"x": 0, "y": 0, "w": 100, "h": 100}
    ref2_region = {"x": 40, "y": 0, "w": 100, "h": 100}
    reference = _reference(
        [
            _record("ref-1", ref1_region, text="premier"),
            _record("ref-2", ref2_region, text="second"),
        ]
    )
    pact_a_bounds = {"x": 25, "y": 0, "w": 100, "h": 100}
    pact_b_bounds = {"x": 60, "y": 0, "w": 100, "h": 100}
    pipeline_acts = [
        _pipeline_act("act_000000000000000a", pact_a_bounds),
        _pipeline_act("act_000000000000000b", pact_b_bounds),
    ]
    hypotheses = {
        "act_000000000000000a": (OutputStatus.COMPLETE, "premier"),
        "act_000000000000000b": (OutputStatus.COMPLETE, "second"),
    }
    comparison = compare_page(reference, pipeline_acts, hypotheses)
    pairs = {pair["pipeline_act_id"]: pair["record_id"] for pair in comparison["matched_pairs"]}
    assert pairs == {"act_000000000000000a": "ref-1", "act_000000000000000b": "ref-2"}
    assert comparison["misses"] == []
    assert comparison["unmatched_pipeline_acts"] == []


def test_tied_total_iou_breaks_toward_the_lower_reference_id():
    """No DP mask survives to break a tie; the replacement rule is pinned here.

    Both pipeline acts share one bounding box, offset so it overlaps `ref-1`
    and `ref-2` by different amounts individually -- but because the two
    pipeline acts are identical, the two candidate total-weight assignments
    (`a`-`ref-1`/`b`-`ref-2` vs. `a`-`ref-2`/`b`-`ref-1`) tie exactly. The
    compare.py docstring states the replacement rule -- rows (pipeline acts,
    already sorted by `act_id`) processed in ascending index, lowest reference
    column index wins a tie -- and this pins the outcome that rule predicts:
    `act-a` (the lower id, processed first) takes `ref-1` (the lower column).
    """
    ref1_region = {"x": 0, "y": 0, "w": 100, "h": 100}
    ref2_region = {"x": 40, "y": 0, "w": 100, "h": 100}
    reference = _reference(
        [
            _record("ref-1", ref1_region, text="premier"),
            _record("ref-2", ref2_region, text="second"),
        ]
    )
    shared_bounds = {"x": 20, "y": 0, "w": 100, "h": 100}
    pipeline_acts = [
        _pipeline_act("act_000000000000000a", shared_bounds),
        _pipeline_act("act_000000000000000b", shared_bounds),
    ]
    hypotheses = {
        "act_000000000000000a": (OutputStatus.COMPLETE, "premier"),
        "act_000000000000000b": (OutputStatus.COMPLETE, "second"),
    }
    comparison = compare_page(reference, pipeline_acts, hypotheses, threshold=Fraction(1, 10))
    pairs = {pair["pipeline_act_id"]: pair["record_id"] for pair in comparison["matched_pairs"]}
    assert pairs == {"act_000000000000000a": "ref-1", "act_000000000000000b": "ref-2"}


def test_matched_pipeline_act_with_no_hypothesis_is_refused():
    reference = _reference([_record("rec-1", {"x": 0, "y": 0, "w": 100, "h": 100})])
    pipeline_acts = [_pipeline_act("act_0000000000000001", {"x": 0, "y": 0, "w": 100, "h": 100})]
    with pytest.raises(CorpusRefusal, match="missing-hypothesis"):
        compare_page(reference, pipeline_acts, hypotheses={})


def test_iou_exactly_at_the_threshold_is_eligible():
    """A pair whose IoU is exactly the predeclared threshold is an eligible edge.

    Chosen so intersection/union is exactly 1/2 in exact integer arithmetic:
    a 100x100 reference box and a 100x50 pipeline box sharing the top half give
    intersection 5000, union 10000, IoU exactly Fraction(1, 2). This does not by
    itself prove the comparison is float-free -- a binary-float `intersection /
    union` also rounds to exactly 0.5 here, so this pair alone cannot separate
    an exact implementation from a float one; see the companion test below for
    that.
    """
    reference = _reference([_record("rec-1", {"x": 0, "y": 0, "w": 100, "h": 100}, text="Baptisé")])
    pipeline_acts = [_pipeline_act("act_0000000000000001", {"x": 0, "y": 0, "w": 100, "h": 50})]
    hypotheses = {"act_0000000000000001": (OutputStatus.COMPLETE, "Baptisé")}
    comparison = compare_page(
        reference, pipeline_acts, hypotheses=hypotheses, threshold=Fraction(1, 2)
    )
    assert comparison["matrix"][0]["eligible"] is True
    assert comparison["matrix"][0]["intersection_area"] == 5000
    assert comparison["matrix"][0]["union_area"] == 10000
    assert comparison["misses"] == []


def test_a_threshold_finer_than_a_float_still_refuses_the_boundary_pair():
    """A threshold a hair above 1/2 -- closer than a double's resolution at 0.5.

    `float(Fraction(1, 2) + Fraction(1, 10**18))` rounds to exactly `0.5`, so
    any implementation that compares IoU in binary floating point would call
    the same 5000/10000 pair eligible here. Only the exact integer
    cross-multiplication in `compare_page` refuses it.
    """
    reference = _reference([_record("rec-1", {"x": 0, "y": 0, "w": 100, "h": 100})])
    pipeline_acts = [_pipeline_act("act_0000000000000001", {"x": 0, "y": 0, "w": 100, "h": 50})]
    threshold = Fraction(1, 2) + Fraction(1, 10**18)
    assert float(threshold) == 0.5
    comparison = compare_page(reference, pipeline_acts, hypotheses={}, threshold=threshold)
    assert comparison["matrix"][0]["eligible"] is False
    assert comparison["matched_pairs"] == []
    assert comparison["misses"] == [
        {"physical_act_id": reference["acts"][0]["physical_act_id"], "record_id": "rec-1"}
    ]


# --- Identity family fencing --------------------------------------------------


def test_refuses_a_pac_identity_offered_as_a_pipeline_act():
    reference = _reference([_record("rec-1", {"x": 0, "y": 0, "w": 100, "h": 100})])
    pac_identity = reference["acts"][0]["physical_act_id"]
    pipeline_acts = [_pipeline_act(pac_identity, {"x": 0, "y": 0, "w": 100, "h": 100})]
    with pytest.raises(CorpusRefusal, match="wrong-identity-family"):
        compare_page(reference, pipeline_acts, hypotheses={})


def test_refuses_a_malformed_act_id():
    reference = _reference([_record("rec-1", {"x": 0, "y": 0, "w": 100, "h": 100})])
    pipeline_acts = [_pipeline_act("not-an-identity", {"x": 0, "y": 0, "w": 100, "h": 100})]
    with pytest.raises(CorpusRefusal, match="wrong-identity-family"):
        compare_page(reference, pipeline_acts, hypotheses={})


# --- Composition: the loader's own output feeds compare_page directly ---------


def test_load_pipeline_proposal_acts_output_composes_straight_into_compare_page(tmp_path):
    """`load_pipeline_proposal_acts(tree)` needs no reshaping before `compare_page`.

    This is `SPEC.md`'s "a synthetic run tree with known boxes" reaching the
    comparator end to end: seal a page and a proposal region, build the matching
    reference page for the same sealed page bytes, and hand the loader's own list
    straight to `compare_page`.
    """
    tree = _make_run(tmp_path)
    page_identity = _seal_page(tree, ordinal=1)
    bounds = {"x": 100, "y": 100, "w": 200, "h": 80}
    proposal_act = _propose_region(tree, page_identity, ordinal=1, bounds=bounds)

    reference = _reference([_record("rec-1", bounds, text="Baptisé Jean")])
    pipeline_acts = load_pipeline_proposal_acts(tree)
    hypotheses = {proposal_act: (OutputStatus.COMPLETE, "Baptisé Jean")}

    comparison = compare_page(reference, pipeline_acts, hypotheses)
    assert len(comparison["matched_pairs"]) == 1
    assert comparison["matched_pairs"][0]["pipeline_act_id"] == proposal_act
    assert comparison["misses"] == []


def test_compare_page_refuses_a_pipeline_act_from_a_different_page():
    reference = _reference([_record("rec-1", {"x": 0, "y": 0, "w": 100, "h": 100})])
    other_page_sha256 = digest_bytes(b"a wholly different page")
    pipeline_acts = [
        _pipeline_act(
            "act_0000000000000001",
            {"x": 0, "y": 0, "w": 100, "h": 100},
            page_sha256=other_page_sha256,
        )
    ]
    with pytest.raises(CorpusRefusal, match="wrong-page"):
        compare_page(reference, pipeline_acts, hypotheses={})


def test_compare_page_refuses_a_pipeline_act_whose_bounds_leave_the_page():
    """A frame error, not a failed detection: the box does not belong to this page at all.

    `page_sha256` equality makes the reference page's own width/height that
    act's frame -- an act at (5000, 5000) on a 2000x3000 page is refused by
    name rather than scored as a MISS with no cause on the record.
    """
    reference = _reference([_record("rec-1", {"x": 100, "y": 100, "w": 200, "h": 80})])
    pipeline_acts = [
        _pipeline_act("act_0000000000000001", {"x": 5000, "y": 5000, "w": 50, "h": 50})
    ]
    with pytest.raises(CorpusRefusal, match="^region-outside-page:"):
        compare_page(reference, pipeline_acts, hypotheses={})


# --- Threshold must be an exact Fraction, never a float -----------------------


def test_compare_page_refuses_a_float_threshold():
    """`compare_page` never compares IoU in binary floating point -- a caller-
    supplied `float` threshold must not silently reach that comparison."""
    reference = _reference([_record("rec-1", {"x": 0, "y": 0, "w": 100, "h": 100})])
    pipeline_acts = [_pipeline_act("act_0000000000000001", {"x": 0, "y": 0, "w": 100, "h": 50})]
    with pytest.raises(CorpusRefusal, match="^malformed-record:"):
        compare_page(reference, pipeline_acts, hypotheses={}, threshold=0.5)


@pytest.mark.parametrize("bad_threshold", [Fraction(0), Fraction(-1), Fraction(2)])
def test_compare_page_refuses_a_threshold_outside_zero_to_one(bad_threshold):
    reference = _reference([_record("rec-1", {"x": 0, "y": 0, "w": 100, "h": 100})])
    with pytest.raises(CorpusRefusal, match="^malformed-record:"):
        compare_page(reference, [], hypotheses={}, threshold=bad_threshold)


def test_compare_page_accepts_a_threshold_of_exactly_one():
    reference = _reference([_record("rec-1", {"x": 0, "y": 0, "w": 100, "h": 100})])
    comparison = compare_page(reference, [], hypotheses={}, threshold=Fraction(1))
    assert comparison["threshold"] == {"numerator": 1, "denominator": 1}


def test_validate_comparison_refuses_a_malformed_threshold_sub_record():
    reference = _reference([_record("rec-1", {"x": 0, "y": 0, "w": 100, "h": 100})])
    comparison = compare_page(reference, [], hypotheses={})
    tampered = dict(comparison)
    tampered["threshold"] = {"numerator": "oops", "denominator": 0}
    tampered["self_hash"] = _self_hash(tampered)
    with pytest.raises(CorpusRefusal, match="^malformed-record:"):
        validate_comparison(tampered)


# --- Every declared refusal reason actually fires ------------------------------


def test_compare_page_refuses_a_non_closed_pipeline_act():
    reference = _reference([_record("rec-1", {"x": 0, "y": 0, "w": 100, "h": 100})])
    malformed = dict(_pipeline_act("act_0000000000000001", {"x": 0, "y": 0, "w": 100, "h": 100}))
    malformed["extra"] = True
    with pytest.raises(CorpusRefusal, match="^malformed-record:"):
        compare_page(reference, [malformed], hypotheses={})


def test_compare_page_refuses_too_many_reference_acts_for_one_page():
    """`MAX_ACTS_PER_PAGE` is a sanity bound on the assignment's size, and this
    is the arm reference_count alone fires it on.

    Each reference act gets its own non-overlapping box, spaced down the
    page's height rather than across its width so `MAX_ACTS_PER_PAGE + 1`
    boxes still fit the 2000x3000 test page -- so none of this is incidentally
    about pipeline-side crowding either, and there is exactly one pipeline act
    here, which alone can never make the count unbounded.
    """
    records = [
        _record(f"rec-{index}", {"x": 0, "y": index * 20, "w": 10, "h": 10})
        for index in range(MAX_ACTS_PER_PAGE + 1)
    ]
    reference = _reference(records)
    pipeline_acts = [_pipeline_act("act_0000000000000001", {"x": 0, "y": 0, "w": 10, "h": 10})]
    with pytest.raises(CorpusRefusal, match="too-many-acts-for-page"):
        compare_page(reference, pipeline_acts, hypotheses={})


def test_compare_page_scores_a_page_at_the_corpus_measured_maximum():
    """30 reference acts, 30 matching pipeline acts -- this corpus's own measured
    maximum (`plan.py`'s `records_per_page_distribution` over the sealed row
    snapshot). The old `MAX_ACTS_PER_PAGE = 20` bitmask DP refused this page
    outright; the exact maximum-weight matching scores it, matching every act.
    """
    count = 30

    def bounds(index: int) -> dict:
        column, row = index % 16, index // 16
        return {"x": column * 120, "y": row * 120, "w": 100, "h": 100}

    records = [_record(f"rec-{index}", bounds(index)) for index in range(count)]
    reference = _reference(records)
    pipeline_acts = [_pipeline_act(f"act_{index:016x}", bounds(index)) for index in range(count)]
    hypotheses = {
        f"act_{index:016x}": (OutputStatus.COMPLETE, "Baptisé Jean") for index in range(count)
    }
    comparison = compare_page(reference, pipeline_acts, hypotheses)
    assert len(comparison["matched_pairs"]) == count
    assert comparison["misses"] == []
    assert comparison["unmatched_pipeline_acts"] == []


def test_compare_page_scores_normally_despite_many_non_overlapping_pipeline_proposals():
    """A crowded index-style page must not lose its reference comparison.

    `pipeline_count` alone does not drive the DP's exponent -- only acts with
    an eligible edge into a reference act do. `MAX_ACTS_PER_PAGE + 5` pipeline
    proposals sit far from every reference box (no eligible edge at all), well
    over the cap by raw count, alongside two ordinary pipeline acts that do
    match. The page must be scored, not refused, and every far-away proposal
    must come back reported as unmatched, not silently dropped.

    The far-away boxes are deliberately still inside the page's own frame --
    they sit clear of both reference boxes and of each other, but a box that
    left the page would hit `region-outside-page` instead of exercising the
    behaviour this test is actually about.
    """
    ref1_region = {"x": 0, "y": 0, "w": 100, "h": 100}
    ref2_region = {"x": 300, "y": 0, "w": 100, "h": 100}
    reference = _reference(
        [
            _record("rec-1", ref1_region, text="premier"),
            _record("rec-2", ref2_region, text="second"),
        ]
    )
    pipeline_acts = [
        _pipeline_act("act_000000000000000a", dict(ref1_region)),
        _pipeline_act("act_000000000000000b", dict(ref2_region)),
    ]
    far_away_ids = []
    for index in range(MAX_ACTS_PER_PAGE + 5):
        far_id = f"act_fa{index:014x}"
        far_away_ids.append(far_id)
        column, row = index % 28, index // 28
        far_bounds = {"x": 20 + column * 70, "y": 2000 + row * 70, "w": 50, "h": 50}
        pipeline_acts.append(_pipeline_act(far_id, far_bounds))
    hypotheses = {
        "act_000000000000000a": (OutputStatus.COMPLETE, "premier"),
        "act_000000000000000b": (OutputStatus.COMPLETE, "second"),
    }

    comparison = compare_page(reference, pipeline_acts, hypotheses)

    pairs = {pair["pipeline_act_id"]: pair["record_id"] for pair in comparison["matched_pairs"]}
    assert pairs == {"act_000000000000000a": "rec-1", "act_000000000000000b": "rec-2"}
    assert comparison["misses"] == []
    unmatched_ids = {entry["act_id"] for entry in comparison["unmatched_pipeline_acts"]}
    assert unmatched_ids == set(far_away_ids)


def test_validate_comparison_refuses_wrong_schema():
    reference = _reference([_record("rec-1", {"x": 0, "y": 0, "w": 100, "h": 100})])
    comparison = compare_page(reference, [], hypotheses={})
    tampered = dict(comparison)
    tampered["schema"] = "some-other.v1"
    with pytest.raises(CorpusRefusal, match="^wrong-schema:"):
        validate_comparison(tampered)


def test_validate_comparison_refuses_a_tampered_self_hash():
    reference = _reference([_record("rec-1", {"x": 0, "y": 0, "w": 100, "h": 100})])
    comparison = compare_page(reference, [], hypotheses={})
    tampered = dict(comparison)
    tampered["normalization_profile_id"] = "some-other-profile"
    with pytest.raises(CorpusRefusal, match="^self-hash-mismatch:"):
        validate_comparison(tampered)


def test_validate_comparison_refuses_a_non_list_matrix_even_with_no_misses():
    """A matrix that is not a list, and an emptied misses list, must not slip by.

    Probes the hole the nested-shape check closes: the top-level key set alone
    passing says nothing about `matrix` actually being a list of matrix entries.
    """
    reference = _reference([_record("rec-1", {"x": 0, "y": 0, "w": 100, "h": 100})])
    comparison = compare_page(reference, [], hypotheses={})
    tampered = dict(comparison)
    tampered["matrix"] = "not a list at all"
    tampered["misses"] = []
    tampered["self_hash"] = _self_hash(tampered)
    with pytest.raises(CorpusRefusal, match="^malformed-record:"):
        validate_comparison(tampered)


def _exercised_compare_refusals(tmp_path: Path) -> dict[str, str]:
    """One real refusal per `COMPARE_REFUSAL_REASONS` member, produced by this
    module. A hand-typed set here could drift from the declared vocabulary in
    either direction without failing; deriving it from what the module itself
    raises cannot."""
    exercised: dict[str, str] = {}
    reference = _reference([_record("rec-1", {"x": 0, "y": 0, "w": 100, "h": 100})])

    malformed = dict(_pipeline_act("act_0000000000000001", {"x": 0, "y": 0, "w": 100, "h": 100}))
    malformed["extra"] = True
    try:
        compare_page(reference, [malformed], hypotheses={})
    except CorpusRefusal as error:
        exercised["malformed-record"] = str(error)

    comparison = compare_page(reference, [], hypotheses={})
    tampered_schema = dict(comparison)
    tampered_schema["schema"] = "some-other.v1"
    try:
        validate_comparison(tampered_schema)
    except CorpusRefusal as error:
        exercised["wrong-schema"] = str(error)

    crowded_reference = _reference(
        [
            _record(f"rec-{index}", {"x": 0, "y": index * 20, "w": 10, "h": 10})
            for index in range(MAX_ACTS_PER_PAGE + 1)
        ]
    )
    crowded_pipeline = [_pipeline_act("act_0000000000000001", {"x": 0, "y": 0, "w": 10, "h": 10})]
    try:
        compare_page(crowded_reference, crowded_pipeline, hypotheses={})
    except CorpusRefusal as error:
        exercised["too-many-acts-for-page"] = str(error)

    pac_identity = reference["acts"][0]["physical_act_id"]
    bad_family_acts = [_pipeline_act(pac_identity, {"x": 0, "y": 0, "w": 100, "h": 100})]
    try:
        compare_page(reference, bad_family_acts, hypotheses={})
    except CorpusRefusal as error:
        exercised["wrong-identity-family"] = str(error)

    other_page_sha256 = digest_bytes(b"a wholly different page")
    wrong_page_acts = [
        _pipeline_act(
            "act_0000000000000001",
            {"x": 0, "y": 0, "w": 100, "h": 100},
            page_sha256=other_page_sha256,
        )
    ]
    try:
        compare_page(reference, wrong_page_acts, hypotheses={})
    except CorpusRefusal as error:
        exercised["wrong-page"] = str(error)

    outside_acts = [_pipeline_act("act_0000000000000001", {"x": 5000, "y": 5000, "w": 50, "h": 50})]
    try:
        compare_page(reference, outside_acts, hypotheses={})
    except CorpusRefusal as error:
        exercised["region-outside-page"] = str(error)

    unresolvable_tree = _make_run(tmp_path / "unresolvable-ordinal")
    unresolvable_page_identity = page_id(
        {"kind": "source", "sha256": PAGE_SOURCE_SHA256}, {"operation": "whole"}
    )
    _propose_region(
        unresolvable_tree,
        unresolvable_page_identity,
        ordinal=1,
        bounds={"x": 0, "y": 0, "w": 10, "h": 10},
    )
    try:
        load_pipeline_proposal_acts(unresolvable_tree)
    except CorpusRefusal as error:
        exercised["unresolvable-page-ordinal"] = str(error)

    matched_acts = [_pipeline_act("act_0000000000000001", {"x": 0, "y": 0, "w": 100, "h": 100})]
    try:
        compare_page(reference, matched_acts, hypotheses={})
    except CorpusRefusal as error:
        exercised["missing-hypothesis"] = str(error)

    tampered_hash = dict(comparison)
    tampered_hash["normalization_profile_id"] = "some-other-profile"
    try:
        validate_comparison(tampered_hash)
    except CorpusRefusal as error:
        exercised["self-hash-mismatch"] = str(error)

    write_tree = _make_run(tmp_path / "write-refused")
    wrapped = ReadOnlyRunTree(write_tree)
    try:
        wrapped.publish_artifact(envelope={})
    except CorpusRefusal as error:
        exercised["run-tree-write-refused"] = str(error)

    return exercised


def test_every_compare_refusal_reason_is_exercised(tmp_path):
    exercised = _exercised_compare_refusals(tmp_path)
    assert set(exercised) == COMPARE_REFUSAL_REASONS
    for reason, text in exercised.items():
        assert text.split(":", 1)[0] == reason, f"{reason} produced {text!r}"


# --- Excluded-region provenance -------------------------------------------------


def test_count_excluded_designator_artifacts_counts_by_kind_and_by_origin(tmp_path):
    tree = _make_run(tmp_path)
    page_identity = _seal_page(tree, ordinal=1)
    _propose_region(tree, page_identity, ordinal=1, bounds={"x": 0, "y": 0, "w": 10, "h": 10})
    _recovery_region(tree, page_identity, ordinal=1, bounds={"x": 20, "y": 20, "w": 10, "h": 10})

    counts = count_excluded_designator_artifacts(tree)
    assert counts["by_kind"] == {}
    assert counts["by_origin"] == {"recovery": 1}


def test_compare_page_carries_excluded_region_counts_into_the_record(tmp_path):
    tree = _make_run(tmp_path)
    page_identity = _seal_page(tree, ordinal=1)
    bounds = {"x": 100, "y": 100, "w": 200, "h": 80}
    proposal_act = _propose_region(tree, page_identity, ordinal=1, bounds=bounds)
    _recovery_region(tree, page_identity, ordinal=1, bounds={"x": 500, "y": 500, "w": 20, "h": 20})

    reference = _reference([_record("rec-1", bounds, text="Baptisé Jean")])
    pipeline_acts = load_pipeline_proposal_acts(tree)
    hypotheses = {proposal_act: (OutputStatus.COMPLETE, "Baptisé Jean")}
    excluded = count_excluded_designator_artifacts(tree)

    comparison = compare_page(reference, pipeline_acts, hypotheses, excluded_region_counts=excluded)
    assert comparison["excluded_region_counts"] == {"by_kind": {}, "by_origin": {"recovery": 1}}


def test_compare_page_defaults_excluded_region_counts_to_an_explicit_empty_shape():
    reference = _reference([_record("rec-1", {"x": 0, "y": 0, "w": 100, "h": 100})])
    comparison = compare_page(reference, [], hypotheses={})
    assert comparison["excluded_region_counts"] == {"by_kind": {}, "by_origin": {}}


def test_compare_page_default_excluded_region_counts_is_a_fresh_object_each_call():
    """Mutating one record's default `excluded_region_counts` must never contaminate
    a later, independent record -- these must never be the same object."""
    reference = _reference([_record("rec-1", {"x": 0, "y": 0, "w": 100, "h": 100})])
    first = compare_page(reference, [], hypotheses={})
    first["excluded_region_counts"]["by_origin"]["recovery"] = 3
    second = compare_page(reference, [], hypotheses={})
    assert second["excluded_region_counts"] == {"by_kind": {}, "by_origin": {}}
    assert second["excluded_region_counts"] is not first["excluded_region_counts"]
    assert (
        second["excluded_region_counts"]["by_origin"]
        is not first["excluded_region_counts"]["by_origin"]
    )


def test_compare_page_copies_a_callers_excluded_region_counts():
    """`compare_page` must not place the caller's own nested dicts in the record --
    mutating the caller's dict afterwards must not touch a record already built."""
    reference = _reference([_record("rec-1", {"x": 0, "y": 0, "w": 100, "h": 100})])
    caller_counts = {"by_kind": {}, "by_origin": {"recovery": 1}}
    comparison = compare_page(reference, [], hypotheses={}, excluded_region_counts=caller_counts)
    caller_counts["by_origin"]["recovery"] = 99
    assert comparison["excluded_region_counts"] == {"by_kind": {}, "by_origin": {"recovery": 1}}
    validate_comparison(comparison)  # still verifies -- untouched by the caller's later edit


def test_compare_page_refuses_an_excluded_region_counts_with_an_unknown_lens():
    reference = _reference([_record("rec-1", {"x": 0, "y": 0, "w": 100, "h": 100})])
    with pytest.raises(CorpusRefusal, match="^malformed-record:"):
        compare_page(
            reference,
            [],
            hypotheses={},
            excluded_region_counts={"by_kind": {}, "by_origin": {}, "by_stage": {}},
        )


# --- Provenance on the record itself --------------------------------------------


def test_compare_page_records_the_corpus_id_and_the_reference_pages_self_hash():
    reference = _reference([_record("rec-1", {"x": 0, "y": 0, "w": 100, "h": 100})])
    comparison = compare_page(reference, [], hypotheses={})
    assert comparison["corpus_id"] == reference["corpus_id"]
    assert comparison["reference_page_self_hash"] == reference["self_hash"]


# --- Import-graph fence: pipeline/ must never import operations.corpus -------
#
# This scan does not catch everything a determined violator could write: a
# nonliteral dynamic import (a module name built at runtime and handed to
# `import_module`), or an import reached only through `sys.modules` /
# `importlib.util.spec_from_file_location`, is outside what a static AST walk can
# see. It does catch every literal `import`, `from ... import ...` (absolute or
# relative), and literal `__import__`/`import_module` call this repository's own
# `operations.corpus` imports would actually be written as.


def _imported_module_names(tree: ast.AST) -> set[str]:
    """Every module name this file names in a statically knowable import.

    `from ...operations import corpus` is a relative `ImportFrom` whose `module`
    is `"operations"` and whose one alias names `"corpus"` -- neither half alone
    says `operations.corpus`, so an alias named `corpus` (or a `corpus.<rest>`
    submodule) under a bare `operations` module is composed into that dotted name
    explicitly, the same way `pipeline/1_exemplar/test_import_boundaries.py`'s
    `imports_module` compares the first dotted segment for its own boundary.
    """
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            names.add(node.module)
            if node.module == "operations":
                for alias in node.names:
                    if alias.name == "corpus" or alias.name.startswith("corpus."):
                        names.add(f"operations.{alias.name}")
        elif isinstance(node, ast.Call):
            func = node.func
            called = (isinstance(func, ast.Name) and func.id in {"__import__"}) or (
                isinstance(func, ast.Attribute) and func.attr == "import_module"
            )
            if called and node.args and isinstance(node.args[0], ast.Constant):
                value = node.args[0].value
                if isinstance(value, str):
                    names.add(value)
    return names


def _pipeline_python_files() -> list[str]:
    result = subprocess.run(
        [
            "git",
            "ls-files",
            "-z",
            "--cached",
            "--others",
            "--exclude-standard",
            "--",
            "pipeline/*.py",
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=True,
    )
    return [path for path in result.stdout.split("\0") if path]


def test_the_pipeline_population_is_the_repositorys_own_python():
    """The guard on the guard: an empty or truncated population would pass vacuously.

    Meta-invariant #88 (`pipeline/1_exemplar/test_import_boundaries.py`): no loop
    here reports success over an empty population.
    """
    files = _pipeline_python_files()
    assert len(files) >= 100, f"expected pipeline/'s tracked Python files, found {len(files)}"
    assert "pipeline/2_designator/run.py" in files
    missing = [path for path in files if not (ROOT / path).exists()]
    assert not missing, (
        "git names a pipeline/ Python file this checkout does not hold, so the "
        f"boundary was checked against something other than the working tree: {missing}"
    )


def test_no_pipeline_module_imports_operations_corpus():
    offenders = []
    for relative_path in _pipeline_python_files():
        path = ROOT / relative_path
        # No try/except here: a file git tracks but that fails to parse is a
        # failed check, not a skipped one -- the population test above already
        # proved every listed path exists in this checkout.
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=relative_path)
        for name in _imported_module_names(tree):
            if name == "operations.corpus" or name.startswith("operations.corpus."):
                offenders.append((relative_path, name))
    assert offenders == [], f"pipeline/ modules importing operations.corpus: {offenders}"
