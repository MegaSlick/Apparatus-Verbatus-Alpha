"""The Recensor's scoped partition receipt is rebuilt from real stage artifacts."""

from __future__ import annotations

import copy
import importlib.util
import json
import subprocess
import sys
from pathlib import Path

import pytest

from common.contracts.errors import FatalAccounting, SchemaRefusal
from common.contracts.outcomes import INTERIM_GRANULARITY_BASIS, NATIVE_GRANULARITY_BASIS
from common.contracts.stages import ATTESTATORES, RECENSOR
from common.native_witness import partition_disagreement
from common.runtree.store import RunTree
from common.stage import stage_parser

ROOT = Path(__file__).resolve().parents[2]


def invoke(
    root: Path, run_id: str, scenario: str, program: str, **extra
) -> subprocess.CompletedProcess:
    command = [
        sys.executable,
        str(ROOT / program),
        "--run-root",
        str(root),
        "--run-id",
        run_id,
        "--scenario",
        scenario,
    ]
    for key, value in extra.items():
        command.extend((f"--{key.replace('_', '-')}", str(value)))
    return subprocess.run(command, cwd=ROOT, capture_output=True, text=True)


def through_perlector(root: Path, run_id: str, scenario: str) -> None:
    for program in (
        "pipeline/1_exemplar/door.py",
        "pipeline/1_exemplar/run.py",
        "pipeline/1_ink_map/run.py",
        "pipeline/2_designator/run.py",
        "pipeline/3_attestatores/run.py",
        "pipeline/4_perlector/run.py",
    ):
        result = invoke(root, run_id, scenario, program)
        assert result.returncode == 0, f"{program}: {result.stderr}"


def _load_recensor():
    path = ROOT / "pipeline/5_recensor/run.py"
    spec = importlib.util.spec_from_file_location("recensor_receipt_under_test", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _recensor_args(root: Path, run_id: str):
    return stage_parser("partition receipt test").parse_args(
        [
            "--run-root",
            str(root),
            "--run-id",
            run_id,
            "--scenario",
            "happy",
            "--fixture-root",
            str(ROOT / "proof"),
        ]
    )


def test_happy_recensor_pass_writes_a_complete_scoped_partition_receipt(tmp_path):
    root = tmp_path / "runs"
    through_perlector(root, "happy", "happy")
    result = invoke(root, "happy", "happy", "pipeline/5_recensor/run.py")
    assert result.returncode == 0, result.stderr

    receipt = RunTree(root, "happy").read_recensor_partition_receipt()
    assert receipt["scope"] == "proposal-acts-and-configured-witnesses"
    assert receipt["expected_act_count"] == 2
    assert receipt["recensor_status"] == "complete"
    assert receipt["by_partition_class"] == {"completed": 2, "unresolved": 0, "failed": 0}
    assert len({item["act_id"] for item in receipt["items"]}) == 2
    assert all(item["review_outcome"] == "accepted" for item in receipt["items"])
    assert {item["coverage"]["granularity_basis"] for item in receipt["items"]} == {
        NATIVE_GRANULARITY_BASIS
    }


@pytest.mark.parametrize("drift", ["stored-false", "geometry-false"])
def test_recensor_rederives_page_attachment_over_the_sealed_proposal_both_ways(
    tmp_path, monkeypatch, drift
):
    """Neither polarity of forged page attachment may move the witness floor."""
    root = tmp_path / "runs"
    through_perlector(root, "attachment-drift", "happy")
    recensor = _load_recensor()
    context = recensor.open_context(_recensor_args(root, "attachment-drift"), RECENSOR)
    act = next(act for act in recensor.expected_acts(context) if act["act_key"] == "a1")
    assert (
        recensor.validate_chair_coverage(context, act["act_id"], context.witness_floor)[
            "under_witnessed"
        ]
        is False
    )

    if drift == "stored-false":
        original = context.tree.read_artifact

        def forged_attachment(stage, kind, artifact_id):
            record = original(stage, kind, artifact_id)
            if (
                stage == ATTESTATORES
                and kind == "act-attachment"
                and record["subject_id"] == act["act_id"]
            ):
                record = copy.deepcopy(record)
                row = next(row for row in record["payload"]["attachments"] if row["page_witness"])
                assert row["attached"] is True
                row["attached"] = False
                row["attachment_basis"] = "unattached"
                row["span"] = None
            return record

        monkeypatch.setattr(context.tree, "read_artifact", forged_attachment)
    else:
        original = context.tree.read_artifact_reference

        def forged_geometry(reference, *, stage, kind, subject_id):
            record = original(reference, stage=stage, kind=kind, subject_id=subject_id)
            if kind == "page-testimonium" and record["payload"]["chair"] == "attestator_1":
                record = copy.deepcopy(record)
                # The sealed proposal begins at x=12. This reported box has no
                # positive-area overlap with it, while the stored attachment
                # still claims geometric-overlap.
                record["payload"]["observed"] = [
                    {
                        "ordinal": 0,
                        "bounds": {"x": 0, "y": 200, "w": 10, "h": 40},
                        "bounds_source": "native",
                        "span": None,
                    }
                ]
                proposal_boxes = record["payload"]["partition_disagreement"]["proposal_boxes"]
                partition_proposals = [
                    {
                        "payload": {
                            "origin": "proposal",
                            "transform": {
                                "source_page_id": record["payload"]["presented"]["source_page_id"],
                                "bounds": box,
                            },
                        }
                    }
                    for box in proposal_boxes
                ]
                record["payload"]["partition_disagreement"] = partition_disagreement(
                    record, partition_proposals
                )
            return record

        monkeypatch.setattr(context.tree, "read_artifact_reference", forged_geometry)

    with pytest.raises(FatalAccounting, match="reported geometry against the sealed proposal"):
        recensor.validate_chair_coverage(context, act["act_id"], context.witness_floor)


@pytest.mark.parametrize("drift", ["stored-false", "wrong-basis"])
def test_recensor_rederives_act_scoped_attachment_instead_of_trusting_its_label(
    tmp_path, monkeypatch, drift
):
    root = tmp_path / "runs"
    through_perlector(root, "act-attachment-drift", "happy")
    recensor = _load_recensor()
    context = recensor.open_context(_recensor_args(root, "act-attachment-drift"), RECENSOR)
    act = next(act for act in recensor.expected_acts(context) if act["act_key"] == "a1")
    original = context.tree.read_artifact

    def forged_attachment(stage, kind, artifact_id):
        record = original(stage, kind, artifact_id)
        if (
            stage == ATTESTATORES
            and kind == "act-attachment"
            and record["subject_id"] == act["act_id"]
        ):
            record = copy.deepcopy(record)
            row = next(row for row in record["payload"]["attachments"] if not row["page_witness"])
            assert row["attached"] is True
            if drift == "stored-false":
                row["attached"] = False
                row["attachment_basis"] = "unattached"
                row["span"] = None
            else:
                row["attachment_basis"] = "anchor-line"
        return record

    monkeypatch.setattr(context.tree, "read_artifact", forged_attachment)
    # Two independent protections can name this forgery: the re-derivation of
    # the act-scoped attachment, and the outcome-consistency check that refuses
    # an attachment disagreeing with the current Testimonium. Which one fires
    # first depends on the drift; either named refusal proves the label was
    # not trusted.
    with pytest.raises(
        FatalAccounting,
        match="act-scoped attachment|disagrees with the current Testimonium outcome",
    ):
        recensor.validate_chair_coverage(context, act["act_id"], context.witness_floor)


def test_page_attachment_merge_keeps_the_contributing_page_that_attached():
    recensor = _load_recensor()
    unattached = {
        "attached": False,
        "attachment_basis": "unattached",
        "anchor_basis": None,
    }
    attached = {
        "attached": True,
        "attachment_basis": "geometric-overlap",
        "anchor_basis": "act-anchor",
    }
    assert recensor._merge_page_attachment_fact(unattached, attached) is attached
    assert recensor._merge_page_attachment_fact(attached, unattached) is attached


def test_recensor_uses_the_page_attempt_outcome_for_page_geometry(tmp_path, monkeypatch):
    """A successful compatibility act row cannot turn a failed page attempt into coverage."""
    root = tmp_path / "runs"
    through_perlector(root, "page-outcome", "happy")
    recensor = _load_recensor()
    context = recensor.open_context(_recensor_args(root, "page-outcome"), RECENSOR)
    act = next(act for act in recensor.expected_acts(context) if act["act_key"] == "a1")
    original_artifact = context.tree.read_artifact
    original_reference = context.tree.read_artifact_reference

    def failed_attachment(stage, kind, artifact_id):
        record = original_artifact(stage, kind, artifact_id)
        if (
            stage == ATTESTATORES
            and kind == "act-attachment"
            and record["subject_id"] == act["act_id"]
        ):
            record = copy.deepcopy(record)
            entry = next(
                item
                for item in record["payload"]["attachments"]
                if item["chair"] == "attestator_3" and item["page_ordinal"] == 1
            )
            assert entry["attached"] is True
            # A coherent forgery: comparability implies attachment, so the
            # forged row must drop both or the comparable seam names it first
            # instead of the page-outcome check this test is aimed at.
            entry.update(attached=False, attachment_basis="unattached", span=None, comparable=False)
        return record

    def failed_page(reference, *, stage, kind, subject_id):
        record = original_reference(reference, stage=stage, kind=kind, subject_id=subject_id)
        if kind == "page-testimonium" and record["payload"]["chair"] == "attestator_3":
            record = copy.deepcopy(record)
            record["outcome"] = "failed"
        return record

    monkeypatch.setattr(context.tree, "read_artifact", failed_attachment)
    monkeypatch.setattr(context.tree, "read_artifact_reference", failed_page)

    coverage = recensor.validate_chair_coverage(context, act["act_id"], context.witness_floor)
    assert coverage["under_witnessed"] is True
    assert coverage["page_granularity_only"] == 1
    assert coverage["shortfalls"]["unaligned"] == 1


def test_recensor_refuses_a_native_capture_attributed_to_another_adapter(tmp_path, monkeypatch):
    root = tmp_path / "runs"
    through_perlector(root, "capture-adapter", "happy")
    recensor = _load_recensor()
    context = recensor.open_context(_recensor_args(root, "capture-adapter"), RECENSOR)
    act = next(act for act in recensor.expected_acts(context) if act["act_key"] == "a1")
    original = context.tree.read_artifact_reference

    def wrong_adapter(reference, *, stage, kind, subject_id):
        record = original(reference, stage=stage, kind=kind, subject_id=subject_id)
        if kind == "page-testimonium" and record["payload"]["chair"] == "attestator_3":
            record = copy.deepcopy(record)
            record["payload"]["native_capture"]["adapter"] = "another-adapter.v1"
        return record

    monkeypatch.setattr(context.tree, "read_artifact_reference", wrong_adapter)
    with pytest.raises(FatalAccounting, match="configured boundary"):
        recensor.validate_chair_coverage(context, act["act_id"], context.witness_floor)


def test_recensor_rederives_a_native_projection_from_the_retained_raw_response(
    tmp_path, monkeypatch
):
    root = tmp_path / "runs"
    through_perlector(root, "capture-projection", "happy")
    recensor = _load_recensor()
    context = recensor.open_context(_recensor_args(root, "capture-projection"), RECENSOR)
    act = next(act for act in recensor.expected_acts(context) if act["act_key"] == "a1")
    original = context.tree.read_artifact_reference

    def forged_projection(reference, *, stage, kind, subject_id):
        record = original(reference, stage=stage, kind=kind, subject_id=subject_id)
        if kind == "page-testimonium" and record["payload"]["chair"] == "attestator_3":
            record = copy.deepcopy(record)
            payload = record["payload"]
            text = payload["payload"]
            forged = ("X" if text[0] != "X" else "Y") + text[1:]
            payload["payload"] = forged
            payload["native_capture"]["parse"]["text"] = forged
            payload["content_health"]["characters"] = len(forged)
        return record

    monkeypatch.setattr(context.tree, "read_artifact_reference", forged_projection)
    with pytest.raises(FatalAccounting, match="parse.*retained raw response"):
        recensor.validate_chair_coverage(context, act["act_id"], context.witness_floor)


def test_recovery_replaces_the_current_partition_snapshot_without_erasing_history(tmp_path):
    root = tmp_path / "runs"
    through_perlector(root, "review", "review")
    first = invoke(root, "review", "review", "pipeline/5_recensor/run.py")
    assert first.returncode == 3, first.stderr
    tree = RunTree(root, "review")
    before = tree.read_recensor_partition_receipt()
    requested = next(item for item in before["items"] if item["act_key"] == "a1")
    assert before["recensor_status"] == "partial"
    assert requested["review_outcome"] == "recovery-requested"
    request = next(
        tree.read_artifact(RECENSOR, "recovery-request", entry["artifact_id"])
        for entry in tree.build_manifest(RECENSOR)["artifacts"]
        if entry["kind"] == "recovery-request" and entry["subject_id"] == requested["act_id"]
    )

    recrop = invoke(
        root,
        "review",
        "review",
        "pipeline/2_designator/run.py",
        operation="recover",
        act=requested["act_id"],
        recovery_request=request["artifact_id"],
    )
    assert recrop.returncode == 0, recrop.stderr
    reread = invoke(
        root, "review", "review", "pipeline/4_perlector/run.py", act=requested["act_id"]
    )
    assert reread.returncode == 0, reread.stderr
    second = invoke(root, "review", "review", "pipeline/5_recensor/run.py")
    assert second.returncode == 3, second.stderr

    after = tree.read_recensor_partition_receipt()
    current = next(item for item in after["items"] if item["act_key"] == "a1")
    # Recovery order cannot assign a marginal observation to the first accepted act.
    assert current["review_outcome"] == "accepted"
    assert current["review_ref"] != requested["review_ref"]
    assert before["self_hash"] != after["self_hash"]

    # The receipt is the one record replaced in place. The evidence it was
    # derived from is append-only (GOVERNANCE 4) and must still be on disk, or
    # the round that produced the recrop could no longer be reconstructed.
    assert tree.resolve(requested["review_ref"]["relative_path"]).exists()


def test_a_tampered_stored_manifest_cannot_become_a_partition_receipt_denominator(tmp_path):
    root = tmp_path / "runs"
    through_perlector(root, "manifest", "happy")
    assert invoke(root, "manifest", "happy", "pipeline/5_recensor/run.py").returncode == 0
    tree = RunTree(root, "manifest")
    tree.resolve(tree.manifest_path(RECENSOR)).write_text("{}", encoding="utf-8")
    recensor = _load_recensor()
    args = _recensor_args(root, "manifest")
    context = recensor.open_context(args, RECENSOR)

    with pytest.raises(FatalAccounting, match="manifest disagrees"):
        recensor.write_partition_receipt(context, context.recovery_policy)


def test_a_refused_partition_receipt_does_not_publish_a_completion_seal(tmp_path, monkeypatch):
    """Receipt reconciliation is part of closing, not work after the checkpoint."""
    root = tmp_path / "runs"
    through_perlector(root, "receipt-refusal", "happy")
    recensor = _load_recensor()

    def refuse_receipt(_context, _budget):
        raise FatalAccounting("partition receipt refused at close")

    monkeypatch.setattr(recensor, "write_partition_receipt", refuse_receipt)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "pipeline/5_recensor/run.py",
            "--run-root",
            str(root),
            "--run-id",
            "receipt-refusal",
            "--scenario",
            "happy",
            "--fixture-root",
            str(ROOT / "proof"),
        ],
    )

    with pytest.raises(FatalAccounting, match="receipt refused at close"):
        recensor.main()

    tree = RunTree(root, "receipt-refusal")
    assert not any(
        entry["kind"] == "stage-seal" for entry in tree.build_manifest(RECENSOR)["artifacts"]
    )


def test_a_tampered_partition_receipt_is_refused_by_its_self_hash(tmp_path):
    root = tmp_path / "runs"
    through_perlector(root, "tampered", "happy")
    assert invoke(root, "tampered", "happy", "pipeline/5_recensor/run.py").returncode == 0
    tree = RunTree(root, "tampered")
    path = tree.resolve(tree.recensor_partition_receipt_path())
    record = json.loads(path.read_text(encoding="utf-8"))
    record["self_hash"] = "0" * 64
    path.write_text(json.dumps(record), encoding="utf-8")

    with pytest.raises(SchemaRefusal, match="self-hash"):
        tree.read_recensor_partition_receipt()


def test_a_run_that_proposed_no_acts_gets_a_visibly_partial_receipt_not_a_refusal():
    """An empty denominator is a fact about the run, not a malformed receipt.

    The Designator proposing nothing at all is the silent-failure shape this whole
    pipeline exists to catch (GOALS 1), and the Armarium's own aggregate already
    treats a sealed page nobody marked out as a named partial rather than an
    error. Refusing to build the receipt would have turned that into a traceback
    at the one boundary whose job is making it visible -- neither lane noticed,
    because no shipped scenario produces a run with zero expected acts.
    """
    from common.recensor_receipt import (
        EMPTY_DENOMINATOR_REASON,
        build_recensor_partition_receipt,
        validate_recensor_partition_receipt,
    )

    receipt = build_recensor_partition_receipt(
        run_id="r",
        config_digest="a" * 64,
        proposal_seal_ref={
            "relative_path": "2_designator/artifacts/proposal-seal.json",
            "sha256": "b" * 64,
        },
        items=[],
    )
    assert receipt["expected_act_count"] == 0
    assert receipt["recensor_status"] == "partial"
    assert receipt["reasons"] == [EMPTY_DENOMINATOR_REASON]
    assert validate_recensor_partition_receipt(receipt) == receipt


def _valid_coverage() -> dict:
    """A self-consistent witness coverage record, the shape `common.contracts.
    outcomes.witness_coverage` actually returns -- one `read`, one
    `genuinely-empty` (both COMPLETED), one `not-run` (UNRESOLVED), against a
    floor of 3, so `under_witnessed` (2 completed < floor 3) is genuinely True
    and every derived field has real content to tamper with below."""
    return {
        "configured": 3,
        "floor": 3,
        "by_outcome": {"read": 1, "genuinely-empty": 1, "not-run": 1},
        "by_class": {"completed": 2, "unresolved": 1, "failed": 0},
        "under_witnessed": True,
        "unresolved_chairs": 1,
        "page_granularity_only": 0,
        "health_unrecorded": 1,
        "shortfalls": {"failed": 0, "truncated": 0, "unaligned": 1},
        "granularity_basis": INTERIM_GRANULARITY_BASIS,
    }


def _item_with_coverage(coverage: dict) -> dict:
    return {
        "act_id": "a1",
        "act_key": "a1",
        "designator_outcome": "proposed",
        "review_ref": {
            "relative_path": "5_recensor/artifacts/review/none.json",
            "sha256": "b" * 64,
        },
        "review_outcome": "held-for-review",
        "partition_class": "unresolved",
        "coverage": coverage,
    }


def _build_with_coverage(coverage: dict):
    from common.recensor_receipt import build_recensor_partition_receipt

    return build_recensor_partition_receipt(
        run_id="r",
        config_digest="a" * 64,
        proposal_seal_ref={
            "relative_path": "2_designator/artifacts/proposal-seal.json",
            "sha256": "b" * 64,
        },
        items=[_item_with_coverage(coverage)],
    )


def test_self_consistent_coverage_builds_a_receipt_cleanly():
    """Proves `_valid_coverage` is genuinely valid before the tampered variants
    below prove each mismatch it can be turned into is refused -- otherwise a
    refusal below could be firing on the wrong field entirely."""
    receipt = _build_with_coverage(_valid_coverage())
    assert receipt["items"][0]["coverage"] == _valid_coverage()


# Each of these three drives a genuinely different coverage fault, and each
# now pins the message for *its* fault rather than one sentence all three
# shared. While that sentence was common to every branch, any one of these
# tests would have passed on the wrong refusal firing -- which is the same
# false-green shape the receipt itself exists to refuse.
def test_a_by_class_that_disagrees_with_by_outcome_is_refused():
    # Self-consistent in every OTHER respect -- it totals `configured`, its
    # unresolved count matches `unresolved_chairs`, and `under_witnessed` still
    # follows from its completed count against the floor -- so the only thing
    # wrong with it is that classifying `by_outcome` derives
    # {completed: 2, unresolved: 1, failed: 0} instead. The earlier fixture here
    # tripped the `unresolved_chairs` check first and never reached this one,
    # which nobody could see while both raised the same sentence.
    coverage = dict(
        _valid_coverage(),
        by_class={"completed": 2, "unresolved": 0, "failed": 1},
        unresolved_chairs=0,
    )
    with pytest.raises(SchemaRefusal, match="does not fall out of its own per-outcome counts"):
        _build_with_coverage(coverage)


def test_an_under_witnessed_flag_disagreeing_with_the_floor_formula_is_refused():
    coverage = dict(_valid_coverage(), under_witnessed=False)  # 2 completed < floor 3 is True
    with pytest.raises(SchemaRefusal, match="claims under_witnessed=False"):
        _build_with_coverage(coverage)


def test_an_unresolved_chairs_count_disagreeing_with_by_class_is_refused():
    coverage = dict(_valid_coverage(), unresolved_chairs=0)  # by_class["unresolved"] is 1
    with pytest.raises(SchemaRefusal, match="unresolved chair\\(s\\) while its own by_class"):
        _build_with_coverage(coverage)


def test_an_unknown_witness_outcome_in_by_outcome_is_refused():
    coverage = dict(_valid_coverage(), by_outcome={"not-a-real-outcome": 3})
    with pytest.raises(SchemaRefusal, match="unknown witness outcome"):
        _build_with_coverage(coverage)


def test_a_missing_coverage_field_is_refused_as_malformed():
    coverage = {key: value for key, value in _valid_coverage().items() if key != "floor"}
    with pytest.raises(SchemaRefusal, match="malformed witness coverage"):
        _build_with_coverage(coverage)


def test_a_negative_coverage_count_is_refused():
    coverage = dict(_valid_coverage(), configured=-1)
    with pytest.raises(SchemaRefusal, match="invalid witness coverage counts"):
        _build_with_coverage(coverage)


def test_duplicate_act_identities_are_refused():
    """Spec 09 test 1: 'duplicate identities are errors.' Two items naming the
    same act_id is a fabricated denominator no real Recensor pass can produce --
    the receipt itself refuses it (via the strictly-sorted check, since a repeat
    is never `>` the one before it) rather than relying on every future caller
    to never construct one."""
    from common.recensor_receipt import build_recensor_partition_receipt

    item = _item_with_coverage(_valid_coverage())
    with pytest.raises(SchemaRefusal, match="strictly sorted"):
        build_recensor_partition_receipt(
            run_id="r",
            config_digest="a" * 64,
            proposal_seal_ref={
                "relative_path": "2_designator/artifacts/proposal-seal.json",
                "sha256": "b" * 64,
            },
            items=[item, dict(item)],
        )


def test_unsorted_items_are_refused_on_direct_validation():
    """`build_recensor_partition_receipt` always sorts before returning, so it
    cannot itself produce an unsorted receipt. The strict order is a receipt
    invariant checked again by `validate_recensor_partition_receipt`, not merely
    an artifact of the one builder that happens to sort today -- proven here by
    handing a validly-built receipt back with its items reversed and its
    self-hash recomputed over the tampered order, the same way a hand-edited
    file on disk would reach validation."""
    from common.contracts.canonical import self_hash
    from common.recensor_receipt import (
        build_recensor_partition_receipt,
        validate_recensor_partition_receipt,
    )

    first = _item_with_coverage(_valid_coverage())
    second = dict(_item_with_coverage(_valid_coverage()), act_id="a2", act_key="a2")
    receipt = build_recensor_partition_receipt(
        run_id="r",
        config_digest="a" * 64,
        proposal_seal_ref={
            "relative_path": "2_designator/artifacts/proposal-seal.json",
            "sha256": "b" * 64,
        },
        items=[first, second],
    )
    assert [item["act_id"] for item in receipt["items"]] == ["a1", "a2"]

    reversed_record = dict(receipt, items=list(reversed(receipt["items"])))
    reversed_record["self_hash"] = self_hash(reversed_record)
    with pytest.raises(SchemaRefusal, match="strictly sorted"):
        validate_recensor_partition_receipt(reversed_record)


@pytest.mark.parametrize(
    "review_outcome,expected_class",
    [
        ("accepted", "completed"),
        ("recovery-requested", "unresolved"),
        ("confirmed-blank", "completed"),
        ("held-for-review", "unresolved"),
        ("failed", "failed"),
    ],
)
def test_every_recensor_terminal_set_combination_builds_a_matching_receipt_item(
    review_outcome, expected_class
):
    """Spec 09 test 1: 'table-driven -- every terminal-set combination.' The
    Recensor's closed outcome vocabulary (`common/contracts/outcomes.py`) has
    exactly five members; this drives every one of them through the receipt and
    checks the partition class `classify` actually derives, not one asserted by
    the caller (`_validate_item` refuses a mismatch, so a wrong table entry here
    would fail loudly rather than pass silently)."""
    from common.recensor_receipt import build_recensor_partition_receipt

    item = dict(
        _item_with_coverage(_valid_coverage()),
        review_outcome=review_outcome,
        partition_class=expected_class,
    )
    receipt = build_recensor_partition_receipt(
        run_id="r",
        config_digest="a" * 64,
        proposal_seal_ref={
            "relative_path": "2_designator/artifacts/proposal-seal.json",
            "sha256": "b" * 64,
        },
        items=[item],
    )
    assert receipt["items"][0]["partition_class"] == expected_class
    assert receipt["by_partition_class"][expected_class] == 1


def test_a_receipt_item_refuses_a_partition_class_its_review_does_not_derive():
    """Pin the refusal whose wording was repaired after CodeRabbit found it."""
    from common.recensor_receipt import build_recensor_partition_receipt

    item = dict(
        _item_with_coverage(_valid_coverage()),
        review_outcome="accepted",
        partition_class="failed",
    )
    with pytest.raises(
        SchemaRefusal,
        match=("names partition_class 'failed', but review_outcome 'accepted' derives 'completed'"),
    ):
        build_recensor_partition_receipt(
            run_id="r",
            config_digest="a" * 64,
            proposal_seal_ref={
                "relative_path": "2_designator/artifacts/proposal-seal.json",
                "sha256": "b" * 64,
            },
            items=[item],
        )


def test_a_review_whose_stored_coverage_disagrees_with_disk_is_refused(tmp_path):
    """`write_partition_receipt` (`pipeline/5_recensor/run.py`) recomputes each
    act's witness coverage fresh from the testimonia on disk and refuses a
    review whose recorded `coverage` disagrees with it -- a check genuinely
    distinct from the earlier manifest-agreement loop
    (`test_a_tampered_stored_manifest_cannot_become_a_partition_receipt_
    denominator`, which trips on a blanked manifest before this one is ever
    reached) and the self-hash check (`test_a_tampered_partition_receipt_is_
    refused_by_its_self_hash`, which tampers the receipt after publication).
    This is the one that catches a testimonium edited after its review was
    written -- HANDOFF.md's own stated worry for what this receipt exists to
    make refutable."""
    from common.contracts.canonical import self_hash

    root = tmp_path / "runs"
    through_perlector(root, "coverage-drift", "happy")
    assert invoke(root, "coverage-drift", "happy", "pipeline/5_recensor/run.py").returncode == 0

    tree = RunTree(root, "coverage-drift")
    review_entry = next(
        entry for entry in tree.build_manifest(RECENSOR)["artifacts"] if entry["kind"] == "review"
    )
    review_path = tree.resolve(review_entry["relative_path"])
    record = json.loads(review_path.read_text(encoding="utf-8"))
    stored_coverage = record["payload"]["coverage"]
    record["payload"]["coverage"] = dict(
        stored_coverage, under_witnessed=not stored_coverage["under_witnessed"]
    )
    # A tampered payload with a stale self-hash would refuse earlier, at
    # envelope verification, and never reach the coverage-drift check this
    # test targets -- re-signed the same way `verify_self_hash` checks, to
    # isolate exactly the boundary named above.
    record["self_hash"] = self_hash(record)
    review_path.write_text(json.dumps(record), encoding="utf-8")
    # The manifest caches each artifact's digest; rewriting the file without
    # refreshing the manifest would trip the earlier manifest-agreement loop
    # instead of the coverage-drift check this test targets.
    tree.write_manifest(RECENSOR)

    recensor = _load_recensor()
    args = _recensor_args(root, "coverage-drift")
    context = recensor.open_context(args, RECENSOR)

    with pytest.raises(FatalAccounting, match="does not retain the act key"):
        recensor.write_partition_receipt(context, context.recovery_policy)


def test_a_recensor_review_for_an_act_nobody_proposed_is_a_fatal_imbalance(tmp_path):
    """Spec 09's first test: "a fabricated unit in no set is FATAL".

    The denominator this receipt reconciles against is the Designator's sealed
    proposal-act set. A Recensor review naming an act that set never named is a
    unit in no terminal set of the partition -- invariant #10's imbalance -- and
    the receipt has to refuse rather than quietly widen its own denominator to
    fit whatever it found on disk. Forged directly, the way the hard-failure cap
    tests forge theirs: no shipped scenario can produce an act the proposal seal
    does not name, which is exactly why the refusal needed a test of its own.
    """
    from common.contracts.canonical import canonical_bytes
    from common.contracts.envelope import build_envelope
    from common.contracts.identities import artifact_id, attempt_id

    root = tmp_path / "runs"
    through_perlector(root, "fabricated", "happy")
    assert invoke(root, "fabricated", "happy", "pipeline/5_recensor/run.py").returncode == 0

    tree = RunTree(root, "fabricated")
    run = tree.read_run()
    fabricated = "act_nobody_proposed"
    envelope = build_envelope(
        run_id=tree.run_id,
        artifact_id=artifact_id(
            RECENSOR, "review", fabricated, attempt_id(fabricated, "recense", 1)
        ),
        subject_id=fabricated,
        stage=RECENSOR,
        kind="review",
        outcome="accepted",
        config_digest=run["config_digest"],
        adapter_revision=run["adapter_recipes"][RECENSOR],
        inputs=[],
        payload={"act_key": "fabricated", "attempt_ordinal": 1},
        attempt=attempt_id(fabricated, "recense", 1),
    )
    path = tree.resolve(tree.artifact_path(RECENSOR, "review", envelope["artifact_id"]))
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(canonical_bytes(envelope))
    # Refreshed, so the receipt's own manifest-agreement loop does not fire
    # first and mask the denominator refusal this test is about.
    tree.write_manifest(RECENSOR)

    recensor = _load_recensor()
    args = _recensor_args(root, "fabricated")
    context = recensor.open_context(args, RECENSOR)

    with pytest.raises(FatalAccounting, match="outside the proposal-act denominator"):
        recensor.write_partition_receipt(context, context.recovery_policy)


def test_an_empty_receipt_may_not_claim_to_be_complete():
    """The status derives from the reasons, and the reason is not optional."""
    from common.contracts.canonical import self_hash
    from common.recensor_receipt import (
        build_recensor_partition_receipt,
        validate_recensor_partition_receipt,
    )

    receipt = build_recensor_partition_receipt(
        run_id="r",
        config_digest="a" * 64,
        proposal_seal_ref={
            "relative_path": "2_designator/artifacts/proposal-seal.json",
            "sha256": "b" * 64,
        },
        items=[],
    )
    forged = dict(receipt, recensor_status="complete", reasons=[])
    forged["self_hash"] = self_hash({k: v for k, v in forged.items() if k != "self_hash"})
    with pytest.raises(SchemaRefusal, match="does not derive from its items"):
        validate_recensor_partition_receipt(forged)


# --- Audit-and-repair regression (F-new-2, mutation-of-mechanisms pass) ----------
#
# Sonnet audit-and-repair seat 1, R0. Mutation check: `pipeline/5_recensor/run.py
# ::validate_chair_coverage` wires `act_attachment_facts(context, act_id)` into
# `witness_coverage(...)` as its `attachments=` argument -- the one production call
# site for D2/D3's act-granularity floor accounting (S3's audit question: "can any
# production caller reach the legacy path... and silently claim act-level coverage
# without attachment facts?"). Deleting that one keyword argument (falling back to
# the pre-R0 legacy arithmetic) left the FULL in-repo suite green: no test anywhere
# asserted a real run's receipt `shortfalls`/`health_unrecorded` values, and
# `under_witnessed`/`page_granularity_only` happen to come out identical either way
# for every outcome combination R0's interim (pre-R4-alignment) design can produce
# (verified: `attached` is definitionally `outcome in WITNESS_READING_OUTCOMES` in
# R0 today, so `page_granularity_only` is structurally always 0 regardless of
# whether attachment facts are wired in at all). Only the two host-only semantic
# pins would have caught it, and those are deselected from the in-chamber gate
# pending host remeasurement. This test closes that gap directly.


def test_a_failed_act_scoped_attempt_produces_a_real_failed_and_unaligned_shortfall(tmp_path):
    """D2/D3 wired end-to-end: a chair that fails act a1 specifically (while still
    contributing to its own page-1 testimony via act a2) must show up in the real
    Recensor receipt's `shortfalls`, not just in `under_witnessed`.

    `malformed-capabilities` declares attestator_3's response to act a1 with a
    non-object `format_capabilities`; the whole attempt fails
    (`prepared_response`), so attestator_3 is not attached to act a1 at all.
    """
    root = tmp_path / "runs"
    through_perlector(root, "malformed-capabilities", "malformed-capabilities")
    result = invoke(
        root, "malformed-capabilities", "malformed-capabilities", "pipeline/5_recensor/run.py"
    )
    assert result.returncode in (0, 3), result.stderr

    receipt = RunTree(root, "malformed-capabilities").read_recensor_partition_receipt()
    item = next(item for item in receipt["items"] if item["act_key"] == "a1")
    coverage = item["coverage"]
    assert coverage["under_witnessed"] is True
    assert coverage["shortfalls"] == {"failed": 1, "truncated": 0, "unaligned": 1}, (
        f"act a1's real coverage record is {coverage!r}; attestator_3's act-scoped attempt "
        "failed for this act specifically (a non-object format_capabilities), so the "
        "receipt's shortfalls must name it real -- not the all-zero shape a call to "
        "witness_coverage() with no attachments= argument would silently produce"
    )


def test_v2_receipt_refuses_zero_failed_shortfalls_for_a_failed_attempt(tmp_path):
    """A v2 label cannot turn an observed failed attempt into all-zero shortfalls."""
    root = tmp_path / "runs"
    through_perlector(root, "forged-shortfalls", "malformed-capabilities")
    result = invoke(
        root,
        "forged-shortfalls",
        "malformed-capabilities",
        "pipeline/5_recensor/run.py",
    )
    assert result.returncode in (0, 3), result.stderr
    receipt = RunTree(root, "forged-shortfalls").read_recensor_partition_receipt()
    item = next(item for item in receipt["items"] if item["act_key"] == "a1")
    item["coverage"]["shortfalls"] = {"failed": 0, "truncated": 0, "unaligned": 0}
    from common.contracts.canonical import self_hash

    receipt["self_hash"] = self_hash(receipt)
    with pytest.raises(SchemaRefusal, match="failed shortfall"):
        from common.recensor_receipt import validate_recensor_partition_receipt

        validate_recensor_partition_receipt(receipt)


def test_v2_receipt_cannot_omit_its_granularity_measurement_basis(tmp_path):
    """P1: zero is not an honest metric unless the receipt names how it was derived."""
    root = tmp_path / "runs"
    through_perlector(root, "missing-basis", "happy")
    result = invoke(root, "missing-basis", "happy", "pipeline/5_recensor/run.py")
    assert result.returncode == 0, result.stderr
    receipt = RunTree(root, "missing-basis").read_recensor_partition_receipt()
    del receipt["items"][0]["coverage"]["granularity_basis"]
    from common.contracts.canonical import self_hash

    receipt["self_hash"] = self_hash(receipt)
    with pytest.raises(SchemaRefusal, match="omits.*granularity"):
        from common.recensor_receipt import validate_recensor_partition_receipt

        validate_recensor_partition_receipt(receipt)


# --- Audit-and-repair regression (F-O4) -----------------------------------------
#
# Opus audit-and-repair seat 3, R0. `page_granularity_only` is subtracted from the
# completed count before the v2 block that typed it ever runs, so a non-integer
# value left `_validate_coverage` through a raw TypeError -- not a ContractError,
# and so not something a caller that refuses malformed evidence by name can catch.


@pytest.mark.parametrize("value", ["1", 1.0, None, [1]])
def test_a_non_integer_page_granularity_count_is_a_named_refusal(value):
    """Every other malformed coverage field in this validator is a named refusal."""
    from common.recensor_receipt import _validate_coverage

    coverage = dict(_valid_coverage(), page_granularity_only=value)
    with pytest.raises(SchemaRefusal, match="invalid page_granularity_only"):
        _validate_coverage(coverage)


# --- Audit-and-repair regression (F-O3) -----------------------------------------
#
# Opus audit-and-repair seat 3, R0. `witness_coverage` counts a chair toward the
# act floor only when its outcome IS a reading, but `_validate_coverage`
# rederived the same number from the ATTESTATORES COMPLETED class -- which is
# wider, because it also holds `excluded`, an approval-bound exclusion that never
# looked at the ink. Writer and validator therefore disagreed for any act with an
# excluded chair, and `build_recensor_partition_receipt` validates every item it
# builds, so no receipt could be written for such an act at all.


def _coverage_with_an_excluded_chair() -> dict:
    from common.contracts.outcomes import witness_coverage

    outcomes = {"attestator_1": "read", "attestator_2": "read", "attestator_3": "excluded"}
    attachments = {
        "attestator_1": {
            "attached": True,
            "comparable": True,
            "truncated": False,
            "health_unrecorded": False,
        },
        "attestator_2": {
            "attached": True,
            "comparable": True,
            "truncated": False,
            "health_unrecorded": False,
        },
        "attestator_3": {
            "attached": False,
            "comparable": False,
            "truncated": None,
            "health_unrecorded": True,
        },
    }
    return witness_coverage(outcomes, 3, attachments=attachments)


def test_an_approval_bound_exclusion_does_not_make_the_receipt_refuse_its_own_writer():
    """The floor arithmetic and its rederivation must count the same chairs.

    An `excluded` chair is COMPLETED class but is not a reading, so
    `witness_coverage` records `under_witnessed=True` for two reads against a
    floor of three. The rederivation must reach the same answer instead of
    reading three completed chairs off `by_class` and calling the record a liar.
    """
    coverage = _coverage_with_an_excluded_chair()
    assert coverage["under_witnessed"] is True
    assert coverage["by_class"]["completed"] == 3
    from common.recensor_receipt import _validate_coverage

    _validate_coverage(coverage)


def test_the_reading_outcome_set_has_exactly_one_definition():
    """A second literal spelling of this closed set beside the floor arithmetic
    that depends on it is a silent divergence waiting to happen: a member added
    to one and not the other would attach a chair that never read.
    """
    import common.contracts.outcomes as vocabulary
    import common.stage as stage

    assert stage.WITNESS_READING_OUTCOMES is vocabulary.WITNESS_READING_OUTCOMES
    assert vocabulary.WITNESS_READING_OUTCOMES < {
        outcome
        for outcome, klass in vocabulary.VOCABULARIES[vocabulary.ATTESTATORES].items()
        if klass is vocabulary.OutcomeClass.COMPLETED
    }, "a reading outcome is completed-class, but the completed class is wider"
