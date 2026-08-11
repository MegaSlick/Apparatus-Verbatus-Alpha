"""The Recensor's scoped partition receipt is rebuilt from real stage artifacts."""

from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from common.contracts.errors import FatalAccounting, SchemaRefusal
from common.contracts.stages import RECENSOR
from common.runtree.store import RunTree

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
    accepted = next(item for item in after["items"] if item["act_key"] == "a1")
    assert accepted["review_outcome"] == "accepted"
    assert accepted["review_ref"] != requested["review_ref"]
    assert before["self_hash"] != after["self_hash"]


def test_a_tampered_stored_manifest_cannot_become_a_partition_receipt_denominator(tmp_path):
    root = tmp_path / "runs"
    through_perlector(root, "manifest", "happy")
    assert invoke(root, "manifest", "happy", "pipeline/5_recensor/run.py").returncode == 0
    tree = RunTree(root, "manifest")
    tree.resolve(tree.manifest_path(RECENSOR)).write_text("{}", encoding="utf-8")
    recensor = _load_recensor()
    args = SimpleNamespace(
        run_root=root,
        run_id="manifest",
        scenario="happy",
        fixture_root=str(ROOT / "proof"),
        models_config=str(ROOT / "config/models.toml"),
        pdf_render_config=str(ROOT / "config/pdf_render.toml"),
        pdf_target_dpi=None,
        recovery_config=str(ROOT / "config/recovery.toml"),
        hard_failure_config=str(ROOT / "config/hard_failure.toml"),
    )
    context = recensor.open_context(args, RECENSOR)

    with pytest.raises(FatalAccounting, match="manifest disagrees"):
        recensor.write_partition_receipt(
            context, recensor.load_recovery_policy(args.recovery_config)
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


def test_a_by_class_that_disagrees_with_by_outcome_is_refused():
    coverage = dict(_valid_coverage(), by_class={"completed": 3, "unresolved": 0, "failed": 0})
    with pytest.raises(SchemaRefusal, match="does not reconcile"):
        _build_with_coverage(coverage)


def test_an_under_witnessed_flag_disagreeing_with_the_floor_formula_is_refused():
    coverage = dict(_valid_coverage(), under_witnessed=False)  # 2 completed < floor 3 is True
    with pytest.raises(SchemaRefusal, match="does not reconcile"):
        _build_with_coverage(coverage)


def test_an_unresolved_chairs_count_disagreeing_with_by_class_is_refused():
    coverage = dict(_valid_coverage(), unresolved_chairs=0)  # by_class["unresolved"] is 1
    with pytest.raises(SchemaRefusal, match="does not reconcile"):
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
        match="names a partition class its own review outcome does not derive",
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
    args = SimpleNamespace(
        run_root=root,
        run_id="coverage-drift",
        scenario="happy",
        fixture_root=str(ROOT / "proof"),
        models_config=str(ROOT / "config/models.toml"),
        pdf_render_config=str(ROOT / "config/pdf_render.toml"),
        pdf_target_dpi=None,
        recovery_config=str(ROOT / "config/recovery.toml"),
        hard_failure_config=str(ROOT / "config/hard_failure.toml"),
    )
    context = recensor.open_context(args, RECENSOR)

    with pytest.raises(FatalAccounting, match="does not retain the act key"):
        recensor.write_partition_receipt(
            context, recensor.load_recovery_policy(args.recovery_config)
        )


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
    args = SimpleNamespace(
        run_root=root,
        run_id="fabricated",
        scenario="happy",
        fixture_root=str(ROOT / "proof"),
        models_config=str(ROOT / "config/models.toml"),
        pdf_render_config=str(ROOT / "config/pdf_render.toml"),
        pdf_target_dpi=None,
        recovery_config=str(ROOT / "config/recovery.toml"),
        hard_failure_config=str(ROOT / "config/hard_failure.toml"),
    )
    context = recensor.open_context(args, RECENSOR)

    with pytest.raises(FatalAccounting, match="outside the proposal-act denominator"):
        recensor.write_partition_receipt(
            context, recensor.load_recovery_policy(args.recovery_config)
        )


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
