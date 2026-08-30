"""Fake-provider acceptance tests for Unit 17's per-stage pod lifecycle."""

from __future__ import annotations

import errno
import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from common.chairs import ChairIdentity, load_models_toml
from common.contracts.stages import DESIGNATOR
from common.runtree.store import RunTree

from .fake_provider import FakeProvider
from .launch import LaunchResult, LaunchState
from .models import BILLING_CUTOFF_MARGIN_ENV, PodCreateRequest
from .shutdown import CloseReport, VerifiedShutdown
from .staged import (
    COLLECTION_BOOT_SCHEDULE,
    ActiveStageBoot,
    PerStagePodLifecycle,
    StageAuthorization,
    StageBootRecord,
    StageBootRefusal,
    StageCloseFailure,
    StageCloseUnverified,
    StageCostStore,
    print_boot_schedule,
    resolve_volume_inputs,
)

START = datetime(2026, 8, 23, 12, tzinfo=UTC)


class Clock:
    def __init__(self) -> None:
        self.seconds = 0.0

    def now(self) -> datetime:
        return START + timedelta(seconds=self.seconds)

    def monotonic(self) -> float:
        return self.seconds

    def sleep(self, seconds: float) -> None:
        self.seconds += seconds


def request(clock: Clock, *, name: str = "unit-17-stage") -> PodCreateRequest:
    return PodCreateRequest(
        name=name,
        gpu_type="fake-48gb",
        image="registry.example/verbatus@sha256:" + "a" * 64,
        template="pinned-template",
        volume_id="shared-run-volume",
        volume_mount_path="/workspace/private",
        docker_start_cmd=(
            "python",
            "-m",
            "operations.pod.pod_timer",
            "--timer-factory",
            "untracked.timer:factory",
            "--bootstrap-command-json",
            '["service"]',
            "--report-path",
            "/workspace/private/stage-report.json",
        ),
        hard_deadline=clock.now() + timedelta(minutes=5),
        repository_commit="b" * 40,
        metadata={BILLING_CUTOFF_MARGIN_ENV: "0"},
    )


class FakeStageRuntime:
    """A deterministic post-gate runtime: no provider action happens at import."""

    def __init__(self, provider: FakeProvider, clock: Clock) -> None:
        self.provider = provider
        self.calls = 0
        self.shutdown = VerifiedShutdown(
            provider,
            timeout_seconds=5,
            poll_seconds=1,
            billing_cutoff_margin_seconds=3600,
            monotonic=clock.monotonic,
            sleeper=clock.sleep,
            now=clock.now,
        )

    def create(self, supplied: PodCreateRequest, *, confirmation: str | None) -> LaunchResult:
        assert confirmation == "separate confirmed stage grant"
        self.calls += 1
        record = self.provider.create(supplied)
        self.provider.bill(record.pod_id, Decimal("0.13"))
        return LaunchResult(LaunchState.CREATED_GUARDED, record=record)


def lifecycle(tmp_path: Path) -> tuple[Clock, FakeProvider, FakeStageRuntime, PerStagePodLifecycle]:
    clock = Clock()
    provider = FakeProvider({"fake-48gb": (Decimal("0.77"), Decimal("0.05"))}, now=clock.now)
    runtime = FakeStageRuntime(provider, clock)
    return (
        clock,
        provider,
        runtime,
        PerStagePodLifecycle(runtime, cost_store=StageCostStore(tmp_path / "volume")),
    )


def test_each_stage_boot_needs_its_own_authorization_and_the_same_one_cannot_boot_twice(
    tmp_path: Path,
) -> None:
    clock, provider, runtime, subject = lifecycle(tmp_path)
    first = StageAuthorization("parish-17", "designator", "grant-designator")

    _, first_cost = subject.run(
        first,
        request(clock, name="designator"),
        confirmation="separate confirmed stage grant",
        work=lambda _: "designator evidence",
    )

    with pytest.raises(StageBootRefusal, match="second pod needs a new independent authorization"):
        subject.run(
            first,
            request(clock, name="attempted-second-pod"),
            confirmation="separate confirmed stage grant",
            work=lambda _: None,
        )

    second = StageAuthorization("parish-17", "attestatores", "grant-witnesses")
    _, second_cost = subject.run(
        second,
        request(clock, name="attestatores"),
        confirmation="separate confirmed stage grant",
        work=lambda _: "witness evidence",
    )

    # The old message read "the first confirmation did not create a second pod",
    # which describes the opposite of the failure this file exists to catch: a
    # refused second boot leaking through makes the count 3, and an engineer
    # reading that during a billing incident goes looking for a missing pod
    # rather than an extra one.
    assert runtime.calls == 2, (
        "expected exactly one paid create per authorization; a different count means either "
        "the second authorization did not create its pod or a refused boot reached the provider"
    )
    assert [item.pod_id for item in provider.pods.values()] == [
        first_cost.pod_id,
        second_cost.pod_id,
    ]
    assert first_cost.pod_id != second_cost.pod_id
    for cost in (first_cost, second_cost):
        assert cost.close.verified
        assert cost.close.pod_get_absent and cost.close.pod_list_absent
        assert cost.close.cost_capture is not None and cost.close.cost_capture.lines
    assert len(cost_records(tmp_path, "stage-pod-cost-intent.v1")) == 2
    assert len(cost_records(tmp_path, "stage-pod-cost.v1")) == 2


def test_stage_one_writes_a_volume_run_tree_input_that_stage_two_reads_on_another_pod(
    tmp_path: Path,
) -> None:
    clock, _, _, subject = lifecycle(tmp_path)
    tree = RunTree.create(
        tmp_path / "volume" / "runs",
        "r",
        source_manifest=[],
        config_digest="c" * 64,
        adapter_recipes={},
        witness_chairs=[],
    )

    def write_on_first_pod(_: object) -> dict[str, str]:
        digest, published = tree.put_blob(DESIGNATOR, b"stage-one-volume-evidence")
        return {"relative_path": published.relative_path, "sha256": digest}

    reference, first_cost = subject.run(
        StageAuthorization("parish-17", "designator", "grant-designator"),
        request(clock, name="designator"),
        confirmation="separate confirmed stage grant",
        work=write_on_first_pod,
    )
    received, second_cost = subject.run(
        StageAuthorization("parish-17", "attestatores", "grant-witnesses"),
        request(clock, name="attestatores"),
        confirmation="separate confirmed stage grant",
        work=lambda _: resolve_volume_inputs(tree, [reference]),
    )

    assert received == (b"stage-one-volume-evidence",)
    assert first_cost.pod_id != second_cost.pod_id


def test_default_stage_stop_is_pod_down_even_when_stage_work_fails(tmp_path: Path) -> None:
    clock, provider, _, subject = lifecycle(tmp_path)
    with pytest.raises(RuntimeError, match="work failed"):
        subject.run(
            StageAuthorization("parish-17", "perlector", "grant-perlector"),
            request(clock, name="perlector"),
            confirmation="separate confirmed stage grant",
            work=lambda _: (_ for _ in ()).throw(RuntimeError("work failed")),
        )

    assert provider.terminate_calls == ["fake-pod-1"]
    assert subject.cost_records[0].close.verified


def test_non_green_billing_close_is_not_reported_as_a_completed_stage(tmp_path: Path) -> None:
    clock, provider, runtime, subject = lifecycle(tmp_path)

    def unbilled_create(supplied: PodCreateRequest, *, confirmation: str | None) -> LaunchResult:
        assert confirmation == "separate confirmed stage grant"
        runtime.calls += 1
        return LaunchResult(LaunchState.CREATED_GUARDED, record=provider.create(supplied))

    runtime.create = unbilled_create  # type: ignore[method-assign]
    with pytest.raises(StageCloseUnverified, match="not verified"):
        subject.run(
            StageAuthorization("parish-17", "perlector", "grant-perlector"),
            request(clock, name="perlector"),
            confirmation="separate confirmed stage grant",
            work=lambda _: "perlectio",
        )

    assert provider.terminate_calls == ["fake-pod-1"]
    assert subject.cost_records[0].close.cost_capture is not None
    assert len(cost_records(tmp_path, "stage-pod-cost-intent.v1")) == 1
    assert len(cost_records(tmp_path, "stage-pod-cost.v1")) == 1


def test_lifecycle_refuses_construction_without_a_durable_cost_store(tmp_path: Path) -> None:
    """A required store keeps cost evidence from dying with the process."""

    _, _, runtime, _ = lifecycle(tmp_path)
    with pytest.raises(TypeError):
        PerStagePodLifecycle(runtime)  # type: ignore[call-arg]


def test_printed_schedule_starts_podless_and_states_the_ruled_witness_order(capsys) -> None:
    print_boot_schedule("parish-17")
    schedule = capsys.readouterr().out.rstrip("\n")
    assert schedule.splitlines()[1] == "1. ingest-to-volume: no pod (no GPU-hours)."
    witness_line = next(line for line in schedule.splitlines() if "attestatores:" in line)
    assert witness_line.index("Chandra") < witness_line.index("Churro") < witness_line.index("DAI")
    assert witness_line.count("fresh GOVERNANCE 8 authorization") == 1


def costs(tmp_path: Path) -> list[dict[str, object]]:
    """Every money record on the volume, whatever its schema."""

    return [
        json.loads(path.read_text(encoding="utf-8"))
        for path in sorted((tmp_path / "volume" / "stage-costs").glob("*.json"))
    ]


def cost_records(tmp_path: Path, schema: str) -> list[dict[str, object]]:
    return [record for record in costs(tmp_path) if record["schema"] == schema]


def boots(tmp_path: Path) -> list[dict[str, object]]:
    return [
        json.loads(path.read_text(encoding="utf-8"))
        for path in sorted((tmp_path / "volume" / "stage-boots").glob("*.json"))
    ]


def test_a_grant_spent_before_a_restart_cannot_boot_a_second_pod_after_one(
    tmp_path: Path,
) -> None:
    """The volume, not one lifecycle process, enforces one boot per grant."""

    clock, provider, _, subject = lifecycle(tmp_path)
    grant = StageAuthorization("parish-17", "designator", "grant-designator")
    subject.run(
        grant,
        request(clock, name="designator"),
        confirmation="separate confirmed stage grant",
        work=lambda _: "designator evidence",
    )

    _, _, restarted_runtime, restarted = lifecycle(tmp_path)
    with pytest.raises(StageBootRefusal, match="second pod needs a new independent authorization"):
        restarted.run(
            grant,
            request(clock, name="attempted-second-pod"),
            confirmation="separate confirmed stage grant",
            work=lambda _: None,
        )

    assert restarted_runtime.calls == 0, "the restarted lifecycle reached the paid gate"
    assert len(provider.pods) == 1


def test_one_authorization_reference_cannot_boot_again_under_a_different_scope(
    tmp_path: Path,
) -> None:
    """Changing collection or stage cannot give one external grant a new address."""

    clock, _, runtime, subject = lifecycle(tmp_path)
    subject.run(
        StageAuthorization("parish-17", "designator", "grant-shared"),
        request(clock, name="designator"),
        confirmation="separate confirmed stage grant",
        work=lambda _: None,
    )

    with pytest.raises(StageBootRefusal, match="already spent"):
        subject.boot(
            StageAuthorization("another-collection", "perlector", "grant-shared"),
            request(clock, name="perlector"),
            confirmation="separate confirmed stage grant",
        )

    assert runtime.calls == 1


@pytest.mark.parametrize("stage", ("ingest-to-volume", "unknown-stage"))
def test_podless_or_unknown_stage_cannot_reach_the_paid_runtime(tmp_path: Path, stage: str) -> None:
    clock, provider, runtime, subject = lifecycle(tmp_path)

    with pytest.raises(StageBootRefusal, match="not a scheduled pod-backed stage"):
        subject.boot(
            StageAuthorization("parish-17", stage, f"grant-{stage}"),
            request(clock, name=stage),
            confirmation="separate confirmed stage grant",
        )

    assert runtime.calls == 0
    assert provider.pods == {}
    assert costs(tmp_path) == []


def test_a_claim_is_durable_before_the_provider_is_touched(tmp_path: Path) -> None:
    """A claim written after the create would be the window it exists to close."""

    clock, _, runtime, subject = lifecycle(tmp_path)
    grant = StageAuthorization("parish-17", "perlector", "grant-perlector")
    claims = tmp_path / "volume" / "stage-boots" / "claims"

    def refuse(supplied: PodCreateRequest, *, confirmation: str | None) -> LaunchResult:
        assert list(claims.glob("*.json")), "the provider was reached before the grant was claimed"
        raise AssertionError("provider create must not be reached in this test")

    runtime.create = refuse  # type: ignore[method-assign]
    with pytest.raises(AssertionError, match="must not be reached"):
        subject.boot(grant, request(clock), confirmation="separate confirmed stage grant")

    claimed = [json.loads(path.read_text(encoding="utf-8")) for path in claims.glob("*.json")]
    assert claimed == [
        {
            "schema": "stage-boot-claim.v1",
            "collection_id": "parish-17",
            "stage": "perlector",
            "authorization_ref": "grant-perlector",
        }
    ]
    assert costs(tmp_path) == [
        {
            "schema": "stage-pod-cost-intent.v1",
            "collection_id": "parish-17",
            "stage": "perlector",
            "authorization_ref": "grant-perlector",
            "request_name": "unit-17-stage",
            "provider_outcome": "unknown",
            "pod_id": None,
            "close_state": "unknown",
            "cost_state": "unknown",
        }
    ]


def test_a_refusal_that_still_created_a_billing_pod_lands_its_close_report(
    tmp_path: Path,
) -> None:
    """Non-green is not the same as no pod, and the launcher says which.

    `CREATE_UNLEASED`, a runtime contract the pod cannot prove, and a price that
    moved between preview and create all refuse *after* a machine exists and
    bills; the launcher closes it itself and hands the report back on the
    result. Discarding that report was a path to a real charge with nothing on
    the volume naming it -- the exact shape GOVERNANCE 2 refuses.
    """

    clock, provider, runtime, subject = lifecycle(tmp_path)
    grant = StageAuthorization("parish-17", "designator", "grant-designator")

    def refuse_after_creating(
        supplied: PodCreateRequest, *, confirmation: str | None
    ) -> LaunchResult:
        runtime.calls += 1
        record = provider.create(supplied)
        provider.bill(record.pod_id, Decimal("0.13"))
        report = runtime.shutdown.close(record, reason="created pod could not be bound to a lease")
        return LaunchResult(
            LaunchState.CREATE_UNLEASED,
            record=record,
            detail="created pod could not be bound to its lease",
            close_report=report,
        )

    runtime.create = refuse_after_creating  # type: ignore[method-assign]
    with pytest.raises(StageBootRefusal, match="did not boot a guarded pod: create-unleased"):
        subject.boot(grant, request(clock), confirmation="separate confirmed stage grant")

    assert [record["pod_id"] for record in boots(tmp_path)] == ["fake-pod-1"]
    recorded = cost_records(tmp_path, "stage-pod-cost.v1")[0]
    assert recorded["authorization_ref"] == "grant-designator"
    assert recorded["close"]["cost_capture"]["lines"]


def test_a_refusal_with_a_pod_and_no_close_report_records_a_named_failure(
    tmp_path: Path,
) -> None:
    """A pod nobody could close is the one that must not be silent."""

    clock, provider, runtime, subject = lifecycle(tmp_path)

    def refuse_without_closing(
        supplied: PodCreateRequest, *, confirmation: str | None
    ) -> LaunchResult:
        runtime.calls += 1
        return LaunchResult(
            LaunchState.CONTROLLERS_UNARMED,
            record=provider.create(supplied),
            detail="controllers did not arm and the close raised",
        )

    runtime.create = refuse_without_closing  # type: ignore[method-assign]
    with pytest.raises(StageBootRefusal, match="controllers-unarmed"):
        subject.boot(
            StageAuthorization("parish-17", "attestatores", "grant-witnesses"),
            request(clock),
            confirmation="separate confirmed stage grant",
        )

    failure = cost_records(tmp_path, "stage-pod-close-failure.v1")[0]
    assert failure["pod_id"] == "fake-pod-1"
    assert "controllers-unarmed" in failure["detail"]
    assert subject.close_failures[0].pod_id == "fake-pod-1"


def test_launcher_close_evidence_for_another_pod_cannot_settle_the_created_pod(
    tmp_path: Path,
) -> None:
    clock, provider, runtime, subject = lifecycle(tmp_path)

    def mismatched_close(supplied: PodCreateRequest, *, confirmation: str | None) -> LaunchResult:
        created = provider.create(supplied)
        other = provider.create(request(clock, name="other-pod"))
        provider.bill(other.pod_id, Decimal("0.13"))
        other_report = runtime.shutdown.close(other, reason="close unrelated pod")
        return LaunchResult(
            LaunchState.CREATE_UNLEASED,
            record=created,
            detail="created pod could not be bound",
            close_report=other_report,
        )

    runtime.create = mismatched_close  # type: ignore[method-assign]
    with pytest.raises(StageBootRefusal, match="not created pod.*may still bill"):
        subject.boot(
            StageAuthorization("parish-17", "designator", "grant-designator"),
            request(clock),
            confirmation="separate confirmed stage grant",
        )

    failures = cost_records(tmp_path, "stage-pod-close-failure.v1")
    assert failures[0]["pod_id"] == "fake-pod-1"
    assert "fake-pod-2" in failures[0]["detail"]
    assert not any(
        record["schema"] == "stage-pod-cost.v1" and record["pod_id"] == "fake-pod-1"
        for record in costs(tmp_path)
    )


def test_launcher_refusal_names_unpersisted_close_evidence_and_keeps_its_cause(
    tmp_path: Path,
) -> None:
    clock, provider, runtime, subject = lifecycle(tmp_path)

    def refuse_after_closing(
        supplied: PodCreateRequest, *, confirmation: str | None
    ) -> LaunchResult:
        created = provider.create(supplied)
        provider.bill(created.pod_id, Decimal("0.13"))
        report = runtime.shutdown.close(created, reason="launcher refusal")
        return LaunchResult(
            LaunchState.CREATE_UNLEASED,
            record=created,
            detail="created pod could not be bound",
            close_report=report,
        )

    def unwritable(_record: object) -> Path:
        raise OSError(errno.ENOSPC, "close evidence path is full")

    runtime.create = refuse_after_closing  # type: ignore[method-assign]
    subject.cost_store.write = unwritable  # type: ignore[method-assign]
    with pytest.raises(StageBootRefusal) as refusal:
        subject.boot(
            StageAuthorization("parish-17", "designator", "grant-designator"),
            request(clock),
            confirmation="separate confirmed stage grant",
        )

    detail = str(refusal.value)
    assert "create-unleased" in detail
    assert "fake-pod-1" in detail
    assert "cost intent remains unknown" in detail
    assert "do not retry" in detail
    assert isinstance(refusal.value.__cause__, OSError)


def test_a_close_that_raises_still_names_the_pod_that_may_still_be_billing(
    tmp_path: Path,
) -> None:
    """The provider failing at shutdown is when durable evidence matters most."""

    clock, provider, runtime, subject = lifecycle(tmp_path)

    def raise_on_close(record: object, *, reason: str) -> CloseReport:
        raise ConnectionError("provider unreachable at shutdown")

    runtime.shutdown.close = raise_on_close  # type: ignore[method-assign]
    with pytest.raises(ConnectionError, match="provider unreachable"):
        subject.run(
            StageAuthorization("parish-17", "perlector", "grant-perlector"),
            request(clock, name="perlector"),
            confirmation="separate confirmed stage grant",
            work=lambda _: "perlectio",
        )

    failure = cost_records(tmp_path, "stage-pod-close-failure.v1")[0]
    assert failure["pod_id"] == "fake-pod-1"
    assert "provider unreachable at shutdown" in failure["detail"]
    assert failure["stage"] == "perlector"


def test_a_stage_runtime_with_no_shutdown_controller_records_before_it_refuses(
    tmp_path: Path,
) -> None:
    clock, _, runtime, subject = lifecycle(tmp_path)
    active = subject.boot(
        StageAuthorization("parish-17", "perlector", "grant-perlector"),
        request(clock),
        confirmation="separate confirmed stage grant",
    )
    runtime.shutdown = object()

    with pytest.raises(StageCloseUnverified, match="no verified shutdown controller"):
        subject.close(active, reason="stage finished")

    failure = cost_records(tmp_path, "stage-pod-close-failure.v1")[0]
    assert failure["pod_id"] == active.record.pod_id


def test_close_report_for_another_pod_cannot_verify_the_active_boot(
    tmp_path: Path,
) -> None:
    clock, provider, runtime, subject = lifecycle(tmp_path)
    active = subject.boot(
        StageAuthorization("parish-17", "designator", "grant-designator"),
        request(clock, name="designator"),
        confirmation="separate confirmed stage grant",
    )
    other = subject.boot(
        StageAuthorization("parish-17", "perlector", "grant-perlector"),
        request(clock, name="perlector"),
        confirmation="separate confirmed stage grant",
    )
    other_cost = subject.close(other, reason="close the other pod")
    runtime.shutdown.close = lambda _record, *, reason: other_cost.close  # type: ignore[method-assign]

    with pytest.raises(StageCloseUnverified, match="not active pod.*may still bill"):
        subject.close(active, reason="stage finished")

    assert active.record.pod_id in provider.pods
    failure = cost_records(tmp_path, "stage-pod-close-failure.v1")[0]
    assert failure["pod_id"] == active.record.pod_id
    assert other.record.pod_id in failure["detail"]


def test_a_boot_abandoned_before_its_close_has_unknown_cost_and_names_its_pod_on_the_volume(
    tmp_path: Path,
) -> None:
    """The kill-between-create-and-close case is already a cost liability.

    The lease store already knows the pod; what it cannot say is which
    collection stage and which grant bought it. This record is that binding,
    written before any work runs, so an operator recovering a run has the pod id
    to type into a provider console and the grant to reconcile it against.
    """

    clock, _, _, subject = lifecycle(tmp_path)
    active = subject.boot(
        StageAuthorization("parish-17", "attestatores", "grant-witnesses"),
        request(clock),
        confirmation="separate confirmed stage grant",
    )

    assert boots(tmp_path) == [
        {
            "schema": "stage-pod-boot.v1",
            "collection_id": "parish-17",
            "stage": "attestatores",
            "authorization_ref": "grant-witnesses",
            "pod_id": active.record.pod_id,
        }
    ]
    assert cost_records(tmp_path, "stage-pod-cost-intent.v1") == [
        {
            "schema": "stage-pod-cost-intent.v1",
            "collection_id": "parish-17",
            "stage": "attestatores",
            "authorization_ref": "grant-witnesses",
            "request_name": "unit-17-stage",
            "provider_outcome": "unknown",
            "pod_id": None,
            "close_state": "unknown",
            "cost_state": "unknown",
        }
    ]


def test_the_store_refuses_to_overwrite_a_cost_record_with_different_bytes(
    tmp_path: Path,
) -> None:
    """GOVERNANCE 4 at the money record: identical evidence re-lands, other bytes do not."""

    store = StageCostStore(tmp_path / "volume")
    failure = StageCloseFailure("parish-17", "perlector", "grant-perlector", "fake-pod-1", "why")
    target = store.write_close_failure(failure)
    assert store.write_close_failure(failure) == target

    target.write_bytes(b"{}")
    with pytest.raises(StageBootRefusal, match="evidence is not overwritten"):
        store.write_close_failure(failure)


def test_the_printed_schedule_names_every_chair_the_real_roster_configures() -> None:
    """The schedule is what an operator authorizes against, so it is measured.

    A hand-written chair list drifts the moment a roster changes, and the drift
    is invisible in the direction that matters: a chair added to a stage the
    schedule calls podless is a boot nobody was asked to authorize. The real
    roster is the one a pod ever serves -- the fixture roster resolves to local
    snapshots -- and `secondary_proposer` is configured there and absent here,
    which is how this list was wrong when it was written.
    """

    root = Path(__file__).resolve().parents[2]
    real = load_models_toml(root / "config/models-real.toml")
    configured = {role for role, chair in real.chairs.items() if isinstance(chair, ChairIdentity)}
    assert configured, "the real roster configures no chair; this test would prove nothing"

    scheduled = [chair.chair for item in COLLECTION_BOOT_SCHEDULE for chair in item.chairs]
    assert sorted(scheduled) == sorted(set(scheduled)), "a chair is scheduled onto two pods"
    assert set(scheduled) == configured, (
        "the boot schedule and the real roster disagree about which chairs are served; "
        "a configured chair the schedule omits is a pod boot nobody was asked to authorize"
    )
    for item in COLLECTION_BOOT_SCHEDULE:
        assert item.pod_required == bool(item.chairs), (
            f"scheduled stage {item.stage!r} claims pod_required={item.pod_required} "
            f"while serving {len(item.chairs)} chair(s)"
        )


def test_a_volume_that_refuses_the_boot_record_still_names_the_billing_pod(
    tmp_path: Path,
) -> None:
    """A store fault does not un-create the machine, so the refusal names it."""

    clock, provider, _, subject = lifecycle(tmp_path)

    def refuse(_record: StageBootRecord) -> Path:
        raise OSError("no space left on the volume")

    subject.cost_store.record_boot = refuse  # type: ignore[method-assign]
    with pytest.raises(StageBootRefusal) as refusal:
        subject.boot(
            StageAuthorization("parish-17", "designator", "grant-designator"),
            request(clock),
            confirmation="separate confirmed stage grant",
        )

    assert list(provider.pods) == ["fake-pod-1"], "the fixture did not actually create a pod"
    assert "fake-pod-1" in str(refusal.value)
    assert provider.terminate_calls == ["fake-pod-1"]
    assert "immediate close was verified" in str(refusal.value)
    assert len(cost_records(tmp_path, "stage-pod-cost-intent.v1")) == 1
    assert len(cost_records(tmp_path, "stage-pod-cost.v1")) == 1
    assert isinstance(refusal.value.__cause__, OSError)


def test_an_interrupt_while_persisting_a_boot_record_propagates_as_itself(
    tmp_path: Path,
) -> None:
    """Ctrl-C during a boot must reach the operator, never a swallowed refusal.

    ``StageBootRefusal`` is an ordinary exception. A caller stepping through a
    collection's stages under a broad ``except Exception`` -- logging one
    failed stage and moving on to the next -- would otherwise absorb the
    operator's stop request and boot a following stage under a fresh grant.
    The immediate close still runs and lands its evidence; only the exception
    identity must survive.
    """

    clock, provider, _, subject = lifecycle(tmp_path)

    def refuse(_record: StageBootRecord) -> Path:
        raise KeyboardInterrupt()

    subject.cost_store.record_boot = refuse  # type: ignore[method-assign]
    with pytest.raises(KeyboardInterrupt):
        subject.boot(
            StageAuthorization("parish-17", "designator", "grant-designator"),
            request(clock),
            confirmation="separate confirmed stage grant",
        )

    assert list(provider.pods) == ["fake-pod-1"], "the fixture did not actually create a pod"
    assert provider.terminate_calls == ["fake-pod-1"]
    assert len(cost_records(tmp_path, "stage-pod-cost-intent.v1")) == 1
    assert len(cost_records(tmp_path, "stage-pod-cost.v1")) == 1


def test_an_interrupt_while_persisting_launcher_close_evidence_propagates_as_itself(
    tmp_path: Path,
) -> None:
    """The same masking risk, at the other refusal-composition site in ``boot``."""

    clock, provider, runtime, subject = lifecycle(tmp_path)

    def refuse_after_closing(
        supplied: PodCreateRequest, *, confirmation: str | None
    ) -> LaunchResult:
        created = provider.create(supplied)
        provider.bill(created.pod_id, Decimal("0.13"))
        report = runtime.shutdown.close(created, reason="launcher refusal")
        return LaunchResult(
            LaunchState.CREATE_UNLEASED,
            record=created,
            detail="created pod could not be bound",
            close_report=report,
        )

    def unwritable(_record: object) -> Path:
        raise KeyboardInterrupt()

    runtime.create = refuse_after_closing  # type: ignore[method-assign]
    subject.cost_store.write = unwritable  # type: ignore[method-assign]
    with pytest.raises(KeyboardInterrupt):
        subject.boot(
            StageAuthorization("parish-17", "designator", "grant-designator"),
            request(clock),
            confirmation="separate confirmed stage grant",
        )


def test_an_unwritable_cost_intent_refuses_before_the_provider_is_touched(
    tmp_path: Path,
) -> None:
    """A durable record fault before create is a refusal, never a spend."""

    clock, provider, runtime, subject = lifecycle(tmp_path)

    def unwritable(_record: object) -> Path:
        raise PermissionError(errno.EACCES, "cost record path is unwritable")

    subject.cost_store.write_intent = unwritable  # type: ignore[method-assign]
    with pytest.raises(PermissionError, match="unwritable"):
        subject.boot(
            StageAuthorization("parish-17", "designator", "grant-designator"),
            request(clock),
            confirmation="separate confirmed stage grant",
        )

    assert runtime.calls == 0
    assert provider.pods == {}
    assert costs(tmp_path) == []


def test_a_crash_after_provider_boot_but_before_the_first_post_boot_write_keeps_unknown_cost(
    tmp_path: Path,
) -> None:
    """A lost create response cannot erase the write-ahead liability."""

    clock, provider, runtime, subject = lifecycle(tmp_path)

    def lose_response(supplied: PodCreateRequest, *, confirmation: str | None) -> LaunchResult:
        assert confirmation == "separate confirmed stage grant"
        runtime.calls += 1
        record = provider.create(supplied)
        provider.bill(record.pod_id, Decimal("0.13"))
        raise ConnectionError("connection died after provider accepted create")

    runtime.create = lose_response  # type: ignore[method-assign]
    with pytest.raises(ConnectionError, match="after provider accepted"):
        subject.boot(
            StageAuthorization("parish-17", "designator", "grant-designator"),
            request(clock),
            confirmation="separate confirmed stage grant",
        )

    assert list(provider.pods) == ["fake-pod-1"], "the adversary did not create a billing pod"
    assert boots(tmp_path) == []
    assert len(cost_records(tmp_path, "stage-pod-cost-intent.v1")) == 1
    intent = cost_records(tmp_path, "stage-pod-cost-intent.v1")[0]
    assert intent["provider_outcome"] == intent["close_state"] == intent["cost_state"] == "unknown"
    assert intent["pod_id"] is None, "a lost response must not invent an exact provider id"


def test_a_crash_after_confirmation_but_before_provider_boot_spends_no_money(
    tmp_path: Path,
) -> None:
    """The write-ahead record survives, but no provider create can be inferred."""

    clock, provider, runtime, subject = lifecycle(tmp_path)

    def crash_before_create(
        supplied: PodCreateRequest, *, confirmation: str | None
    ) -> LaunchResult:
        assert supplied.name == "designator"
        assert confirmation == "separate confirmed stage grant"
        runtime.calls += 1
        raise RuntimeError("process died after confirmation and before provider create")

    runtime.create = crash_before_create  # type: ignore[method-assign]
    with pytest.raises(RuntimeError, match="before provider create"):
        subject.boot(
            StageAuthorization("parish-17", "designator", "grant-designator"),
            request(clock, name="designator"),
            confirmation="separate confirmed stage grant",
        )

    assert provider.pods == {}
    assert not any(verb == "create" for verb, _subject in provider.calls)
    intent = cost_records(tmp_path, "stage-pod-cost-intent.v1")[0]
    assert intent["provider_outcome"] == "unknown"
    assert boots(tmp_path) == []


@pytest.mark.parametrize(
    "write_error",
    (
        OSError(errno.ENOSPC, "no space left on device"),
        PermissionError(errno.EACCES, "record path became unwritable"),
        RuntimeError("unexpected exception during record write"),
    ),
)
def test_every_post_create_write_failure_closes_immediately_and_retains_unknown_liability(
    tmp_path: Path, write_error: BaseException
) -> None:
    """Disk-full, permission, and unexpected write failures share one safe shape."""

    clock, provider, _, subject = lifecycle(tmp_path)

    def fail_boot(_record: object) -> Path:
        raise write_error

    def fail_cost(_record: object) -> Path:
        raise write_error

    subject.cost_store.record_boot = fail_boot  # type: ignore[method-assign]
    subject.cost_store.write = fail_cost  # type: ignore[method-assign]
    with pytest.raises(StageBootRefusal, match="cost intent remains unknown, never zero"):
        subject.boot(
            StageAuthorization("parish-17", "designator", "grant-designator"),
            request(clock),
            confirmation="separate confirmed stage grant",
        )

    assert provider.terminate_calls == ["fake-pod-1"]
    assert cost_records(tmp_path, "stage-pod-cost.v1") == []
    assert subject.cost_records == [], "an in-memory list must not claim a failed write was durable"
    assert len(cost_records(tmp_path, "stage-pod-cost-intent.v1")) == 1


def test_a_close_record_write_failure_leaves_unknown_not_zero(
    tmp_path: Path,
) -> None:
    clock, provider, _, subject = lifecycle(tmp_path)
    active = subject.boot(
        StageAuthorization("parish-17", "perlector", "grant-perlector"),
        request(clock),
        confirmation="separate confirmed stage grant",
    )

    def full(_record: object) -> Path:
        raise OSError(errno.ENOSPC, "no space left while writing close")

    subject.cost_store.write = full  # type: ignore[method-assign]
    with pytest.raises(OSError, match="no space left while writing close"):
        subject.close(active, reason="stage finished")

    assert provider.terminate_calls == ["fake-pod-1"]
    assert subject.cost_records == []
    intent = cost_records(tmp_path, "stage-pod-cost-intent.v1")[0]
    assert intent["close_state"] == intent["cost_state"] == "unknown"


def test_a_restarted_lifecycle_closes_a_recovered_pod_against_its_original_grant(
    tmp_path: Path,
) -> None:
    """A pod recovered after a crash settles against the grant that booted it.

    The adoption itself is a stub: this layer has no adoption path (see
    `staged.py`'s module docstring), so the closure below only produces the
    record a real `PodRuntime.adopt` would return. Asserting on that closure's
    own refusal proved nothing -- no change to production code could fail it --
    and the test's old name read as coverage of a confirmation gate, which would
    have stayed green after such a gate was added and broken. What is real is
    below: a restarted lifecycle closes the recovered pod id against the
    original stage grant, verifies it, and writes exactly one cost record, while
    no second pod is created.
    """

    clock, provider, _, before_crash = lifecycle(tmp_path)
    grant = StageAuthorization("parish-17", "attestatores", "grant-witnesses")
    active = before_crash.boot(
        grant,
        request(clock, name="attestatores"),
        confirmation="separate confirmed stage grant",
    )
    create_calls = sum(verb == "create" for verb, _subject in provider.calls)

    def adopt(confirmation: str | None) -> LaunchResult:
        if confirmation != "fresh confirmed adoption":
            return LaunchResult(
                LaunchState.REFUSED_CONFIRMATION, detail="fresh confirmation required"
            )
        return LaunchResult(
            LaunchState.ADOPTED_GUARDED, record=provider.adopt(active.record.pod_id)
        )

    adopted = adopt("fresh confirmed adoption")
    assert adopted.record is not None and adopted.record.pod_id == active.record.pod_id
    assert sum(verb == "create" for verb, _subject in provider.calls) == create_calls

    restarted_runtime = FakeStageRuntime(provider, clock)
    restarted = PerStagePodLifecycle(
        restarted_runtime, cost_store=StageCostStore(tmp_path / "volume")
    )
    settled = restarted.close(
        ActiveStageBoot(grant, adopted.record), reason="recovered after crash"
    )

    assert settled.pod_id == active.record.pod_id
    assert settled.close.verified
    closes = cost_records(tmp_path, "stage-pod-cost.v1")
    assert len(closes) == 1
    # The record itself must settle against the ORIGINAL grant — a close that
    # persisted a different authorisation reference, collection, or stage would
    # have kept this test green while billing the wrong ledger line.
    assert closes[0]["collection_id"] == "parish-17"
    assert closes[0]["stage"] == "attestatores"
    assert closes[0]["authorization_ref"] == "grant-witnesses"
    # And recovery through close must create no second pod.
    assert sum(verb == "create" for verb, _subject in provider.calls) == create_calls
