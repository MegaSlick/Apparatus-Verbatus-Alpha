"""Per-stage fake-provider lifecycles and the honest collection boot schedule.

This module deliberately has no CLI and no provider factory.  A caller supplies
the already-gated :class:`~operations.pod.launch.PodRuntime`; this layer adds
the narrower rule that one explicit authorization can create at most one pod
for one collection stage.  It never turns a first confirmation into authority
for a second boot.

**That rule is durable, and its scope is stated rather than implied.** The claim
that a grant has been spent is a file on the run volume, written before the
provider is touched, so it survives the process and binds a later one; it is not
a set in memory that a restart empties.  What it establishes is exactly one
thing: this collection stage's grant reference has been used for a boot attempt.
It is not proof of Tyrel's permission -- nothing local can supply that, and
GOVERNANCE 8 continues to rest on his permission in the session.  The typed
confirmation in ``spend.py`` remains the money gate underneath, per-process and
single-use by design; this layer records what that gate was spent on.

Every path out of a boot leaves durable money evidence, because the one thing
GOVERNANCE 2 cannot tolerate here is a pod that billed and left nothing behind:
a grant claim and an explicitly unknown cost intent before the create, a boot
record the moment a pod exists, and then either a cost record or a named close
failure. A close that raised is the case where a pod is most likely to be
*still* billing, so it is the last case that may vanish behind an exception.

**Create and close, not adoption, and that is a decision rather than an
omission.** Recovering a pod a crashed stage left running goes through the
lease-backed controllers and ``PodRuntime.adopt``, which is already gated by the
same typed confirmation; wrapping it here would add a second paid seam with no
caller and an unsettled question -- whether re-adopting the machine your own
grant created spends that grant again -- decided in the abstract rather than
against a real recovery.  What a recovering operator actually needs from this
layer is the binding the lease cannot supply: which collection stage and which
grant a pod id belonged to.  That is on the volume from the moment the pod
exists, so an adoption performed elsewhere can still be reconciled to a stage.

The schedule starts with volume ingest because transfer needs no pod or GPU
hours.  GPU stages are closed in ``run``'s ``finally`` block: retaining a pod
between stages is not an option in this API.  A real lifecycle remains gated by
GOVERNANCE 8; the in-memory fake is the only provider used by this repository's
tests.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Protocol, Sequence, TypeVar, cast

from common.contracts.canonical import canonical_bytes, digest_bytes
from common.contracts.errors import SchemaRefusal

from .durable import exclusive_write
from .launch import LaunchResult
from .models import CloseState, PodCreateRequest, PodRecord
from .shutdown import CloseReport


class StageBootRefusal(RuntimeError):
    """A stage has no independent authorization for the requested boot."""


class StageCloseUnverified(RuntimeError):
    """A stage completed work but its mandatory pod-down close was non-green."""


class StageRuntime(Protocol):
    """The existing paid-action gate plus its verified-close controller."""

    shutdown: object

    def create(self, request: PodCreateRequest, *, confirmation: str | None) -> LaunchResult:
        """Create one guarded pod through the ordinary confirmation gate."""


@dataclass(frozen=True, slots=True)
class StageAuthorization:
    """One externally recorded GOVERNANCE 8 grant, scoped to one boot only.

    ``authorization_ref`` is an operator record reference, not a typed spend
    phrase and not a claim that software can grant permission.  The value lets
    the caller bind a cost record to the exact per-stage approval without
    storing the permission's contents in code.
    """

    collection_id: str
    stage: str
    authorization_ref: str

    def __post_init__(self) -> None:
        for label, value in (
            ("collection id", self.collection_id),
            ("stage", self.stage),
            ("authorization reference", self.authorization_ref),
        ):
            if not isinstance(value, str) or not value.strip() or value.strip() != value:
                raise ValueError(f"stage boot {label} must be a non-blank string")

    def to_record(self) -> dict[str, object]:
        """The durable claim: these three strings *are* the grant's identity."""

        return {
            "schema": "stage-boot-claim.v1",
            "collection_id": self.collection_id,
            "stage": self.stage,
            "authorization_ref": self.authorization_ref,
        }


@dataclass(frozen=True, slots=True)
class ScheduledChair:
    """One configured chair a stage's single pod serves, named by its roster key.

    The key rather than the prose, because the prose is for the operator and the
    key is what ``config/models-real.toml`` can be reconciled against. A chair
    the roster configures and this schedule does not name is a boot nobody was
    asked to authorize, and that is a test failure, not a documentation lapse.
    """

    chair: str
    note: str

    def render(self) -> str:
        return f"{self.chair} ({self.note})"


@dataclass(frozen=True, slots=True)
class ScheduledStage:
    """One ordered collection step, visibly podless or individually gated."""

    stage: str
    pod_required: bool
    chairs: tuple[ScheduledChair, ...] = ()


# This is stage order, not a model preference.  The one model-order ruling is
# represented only inside the Attestatores block: Chandra reaches its checkpoint
# before Churro, then DAI.  No model is co-resident in that block.
#
# The chairs named per stage are the real roster's, not the fixture roster's:
# the fixture roster resolves to local snapshots and boots nothing, so a
# schedule measured against it would under-report every real boot.
# `secondary_proposer` is the case that proves the point -- absent in the
# fixture roster, configured in the real one, and served by the Designator's
# own pod after the structure chair (`pipeline/2_designator/run.py:258`, `:282`).
COLLECTION_BOOT_SCHEDULE: tuple[ScheduledStage, ...] = (
    ScheduledStage("ingest-to-volume", False),
    ScheduledStage(
        "designator",
        True,
        (
            ScheduledChair("designator_structure", "Chandra, structure and crop authority"),
            ScheduledChair("secondary_proposer", "proposals only, after the structure chair"),
        ),
    ),
    ScheduledStage(
        "attestatores",
        True,
        (
            ScheduledChair("attestator_1", "Chandra, all work to checkpoint"),
            ScheduledChair("attestator_3", "Churro, after Chandra"),
            ScheduledChair("attestator_2", "DAI, after Churro"),
        ),
    ),
    ScheduledStage("perlector", True, (ScheduledChair("perlector", "Qwen3.8-27B"),)),
    ScheduledStage("recensor", False),
    ScheduledStage("archetypus", False),
    ScheduledStage("armarium", False),
)


def render_boot_schedule(collection_id: str) -> str:
    """Render the expected boot sequence that an operator must authorize.

    The string says exactly which items use no pod and which each need a fresh
    grant, so a collection does not conceal later boots behind an earlier one.
    """

    if not isinstance(collection_id, str) or not collection_id.strip():
        raise ValueError("collection id must be a non-blank string")
    lines = [f"Boot schedule for collection {collection_id}:"]
    for ordinal, item in enumerate(COLLECTION_BOOT_SCHEDULE, start=1):
        if not item.pod_required:
            lines.append(f"{ordinal}. {item.stage}: no pod (no GPU-hours).")
            continue
        chairs = "; ".join(chair.render() for chair in item.chairs)
        lines.append(
            f"{ordinal}. {item.stage}: one fresh GOVERNANCE 8 authorization, one pod, "
            f"then pod-down. Serving order: {chairs}."
        )
    return "\n".join(lines)


def print_boot_schedule(collection_id: str) -> None:
    """Print rather than silently retain the schedule an operator must see."""

    print(render_boot_schedule(collection_id))


@dataclass(frozen=True, slots=True)
class ActiveStageBoot:
    authorization: StageAuthorization
    record: PodRecord


@dataclass(frozen=True, slots=True)
class StageBootRecord:
    """One provider pod, bound to the stage grant that paid for it.

    Written the moment a pod exists and before any work runs, so a kill between
    the create and the close still leaves the collection, the stage and the
    grant named beside a pod id an operator can type into a provider console.
    The lease store already records the pod durably; what this adds is *whose
    stage boot it was*, which the lease does not know.
    """

    collection_id: str
    stage: str
    authorization_ref: str
    pod_id: str

    def to_record(self) -> dict[str, object]:
        return {
            "schema": "stage-pod-boot.v1",
            "collection_id": self.collection_id,
            "stage": self.stage,
            "authorization_ref": self.authorization_ref,
            "pod_id": self.pod_id,
        }


@dataclass(frozen=True, slots=True)
class StageCostRecord:
    """One close report, bound to the collection stage and its own authorization."""

    collection_id: str
    stage: str
    authorization_ref: str
    pod_id: str
    close: CloseReport

    def to_record(self) -> dict[str, object]:
        return {
            "schema": "stage-pod-cost.v1",
            "collection_id": self.collection_id,
            "stage": self.stage,
            "authorization_ref": self.authorization_ref,
            "pod_id": self.pod_id,
            "close": self.close.to_record(),
        }


@dataclass(frozen=True, slots=True)
class StageCostIntent:
    """Write-ahead liability for one provider call whose outcome is not known yet.

    This is deliberately a money record, not merely an authorization claim. It
    lands before the provider is touched, so a process death or a volume fault
    after create cannot make a boot disappear. It says neither that a pod
    existed nor that it cost zero: provider outcome, pod id, close, and cost are
    all explicitly unknown until later boot/close evidence settles them.
    """

    collection_id: str
    stage: str
    authorization_ref: str
    request_name: str

    def to_record(self) -> dict[str, object]:
        return {
            "schema": "stage-pod-cost-intent.v1",
            "collection_id": self.collection_id,
            "stage": self.stage,
            "authorization_ref": self.authorization_ref,
            "request_name": self.request_name,
            "provider_outcome": "unknown",
            "pod_id": None,
            "close_state": "unknown",
            "cost_state": "unknown",
        }


@dataclass(frozen=True, slots=True)
class StageCloseFailure:
    """A boot whose close produced no report at all -- a pod that may still bill.

    Deliberately a different schema rather than a cost record with a null close:
    a reader totalling spend must not be able to mistake "we do not know what
    this cost" for "this cost nothing", and GOVERNANCE 2 asks a partial result
    to look partial rather than to be a field away from looking complete.
    """

    collection_id: str
    stage: str
    authorization_ref: str
    pod_id: str
    detail: str

    def to_record(self) -> dict[str, object]:
        return {
            "schema": "stage-pod-close-failure.v1",
            "collection_id": self.collection_id,
            "stage": self.stage,
            "authorization_ref": self.authorization_ref,
            "pod_id": self.pod_id,
            "detail": self.detail,
        }


class StageCostStore:
    """Append-only, durable per-boot money evidence on the run volume.

    Two addressing schemes, because two different facts are being kept. A cost
    intent, cost, boot or close-failure record is *content*-addressed: writing the same
    evidence twice is a no-op, and different bytes at one address is a refusal,
    because GOVERNANCE 4 does not overwrite evidence. A claim is *key*-addressed
    by its grant, because there the file's existence is the fact: the exclusive
    create is what makes one grant unable to boot a second pod, in this process
    or in one that starts after a crash.
    """

    CLAIMS = "stage-boots/claims"
    BOOTS = "stage-boots"
    COSTS = "stage-costs"

    def __init__(self, root: str | Path) -> None:
        self.root = Path(root)

    def claim(
        self, authorization: StageAuthorization, request: PodCreateRequest
    ) -> tuple[Path, Path]:
        """Spend one grant, durably, before anything can bill against it.

        Called before the provider is touched: a claim written afterwards would
        be exactly the window this exists to close. The consequence is stated
        rather than hidden -- a create that refuses without reaching the
        provider still spends the *reference*, and a retry records a fresh one.
        Minting a second reference is bookkeeping, not a second permission, and
        the alternative is a released claim whose release can itself be lost.
        """

        data = canonical_bytes(authorization.to_record())
        target = self.root / self.CLAIMS / f"{digest_bytes(data)}.json"
        try:
            exclusive_write(target, data, strict=True)
        except FileExistsError:
            raise StageBootRefusal(
                f"authorization {authorization.authorization_ref!r} was already spent on a boot "
                f"for collection {authorization.collection_id!r} stage {authorization.stage!r} "
                f"(durable claim {target}); a second pod needs a new independent authorization"
            ) from None
        # This second write is the conservative money floor. If it fails, the
        # grant stays spent but the provider is never reached. Once it succeeds,
        # every later provider outcome has durable evidence even when the process
        # dies before it receives a pod id or the volume refuses every later write.
        intent = StageCostIntent(
            authorization.collection_id,
            authorization.stage,
            authorization.authorization_ref,
            request.name,
        )
        return target, self.write_intent(intent)

    def write_intent(self, record: StageCostIntent) -> Path:
        return self._append(self.COSTS, record.to_record(), "cost intent")

    def record_boot(self, record: StageBootRecord) -> Path:
        return self._append(self.BOOTS, record.to_record(), "boot record")

    def write(self, record: StageCostRecord) -> Path:
        return self._append(self.COSTS, record.to_record(), "cost record")

    def write_close_failure(self, record: StageCloseFailure) -> Path:
        return self._append(self.COSTS, record.to_record(), "close failure record")

    def _append(self, directory: str, value: dict[str, object], label: str) -> Path:
        data = canonical_bytes(value)
        target = self.root / directory / f"{digest_bytes(data)}.json"
        try:
            exclusive_write(target, data, strict=True)
        except FileExistsError:
            if target.read_bytes() != data:
                raise StageBootRefusal(
                    f"{label} path {target} exists with different bytes; "
                    "evidence is not overwritten"
                ) from None
        return target


WorkResult = TypeVar("WorkResult")
_MISSING_WORK_RESULT = object()


class PerStagePodLifecycle:
    """Create, run, verify-close, and record one independently authorized stage."""

    def __init__(self, runtime: StageRuntime, *, cost_store: StageCostStore) -> None:
        self.runtime = runtime
        self.cost_store = cost_store
        self.cost_records: list[StageCostRecord] = []
        self.close_failures: list[StageCloseFailure] = []

    def boot(
        self,
        authorization: StageAuthorization,
        request: PodCreateRequest,
        *,
        confirmation: str | None,
    ) -> ActiveStageBoot:
        """Use exactly one stage grant for one guarded pod creation.

        A non-green result is not the same as no pod. The launcher refuses
        several ways *after* the provider has created a billing machine -- an
        unbindable lease, a runtime contract the pod does not prove, a price
        that moved between preview and create -- and closes it itself. Those
        refusals carry the record and usually the close report, and this
        lifecycle lands both as money evidence before it raises. Dropping them
        was a real path to a pod that billed and left nothing on the volume.
        """

        self.cost_store.claim(authorization, request)
        result = self.runtime.create(request, confirmation=confirmation)
        record = result.record
        if record is not None:
            boot_record = StageBootRecord(
                authorization.collection_id,
                authorization.stage,
                authorization.authorization_ref,
                record.pod_id,
            )
            try:
                self.cost_store.record_boot(boot_record)
            except BaseException as error:
                # The write-ahead cost intent is already durable. Minimize the
                # now-known liability immediately; returning without attempting
                # the close would turn an evidence fault into an avoidable spend.
                active = ActiveStageBoot(authorization, record)
                try:
                    cost = self.close(
                        active,
                        reason="stage boot record could not be persisted; immediate pod-down",
                    )
                except BaseException as close_error:
                    raise StageBootRefusal(
                        f"stage {authorization.stage!r} created pod {record.pod_id!r} but its boot "
                        f"record could not be written to the volume: {error}; immediate close "
                        "did not produce durable verified evidence; the write-ahead cost intent "
                        "remains unknown, never zero"
                    ) from close_error
                raise StageBootRefusal(
                    f"stage {authorization.stage!r} created pod {record.pod_id!r} but its boot "
                    f"record could not be written to the volume: {error}; immediate close was "
                    f"{cost.close.state.value} and its cost evidence was recorded"
                ) from error
        if not result.green or record is None:
            if record is not None:
                self._record_launcher_close(authorization, record.pod_id, result)
            raise StageBootRefusal(
                f"stage {authorization.stage!r} did not boot a guarded pod: {result.state.value}; "
                f"{result.detail}"
            )
        return ActiveStageBoot(authorization, record)

    def close(self, active: ActiveStageBoot, *, reason: str) -> StageCostRecord:
        """Verified close is mandatory and leaves a durable per-stage cost record.

        Every exit below writes to the volume first and raises second. A close
        that raised, returned nothing usable, or could not be attempted at all
        is the case where the pod is most likely still running, so it is the
        last one that may leave the store empty.
        """

        shutdown = getattr(self.runtime, "shutdown", None)
        close = getattr(shutdown, "close", None)
        if not callable(close):
            detail = "stage runtime exposes no verified shutdown controller"
            self._record_close_failure(active, detail)
            raise StageCloseUnverified(detail)
        try:
            report = close(active.record, reason=reason)
        except BaseException as error:
            # If this write itself fails it raises *inside* the handler, so the
            # close error stays on the chain as `__context__`: a failing store
            # cannot swallow the fact that a pod may still be billing.
            self._record_close_failure(
                active, f"verified shutdown raised {type(error).__name__}: {error}"
            )
            raise
        if not isinstance(report, CloseReport):
            detail = "stage runtime returned no CloseReport from shutdown"
            self._record_close_failure(active, detail)
            raise StageCloseUnverified(detail)
        cost = StageCostRecord(
            active.authorization.collection_id,
            active.authorization.stage,
            active.authorization.authorization_ref,
            active.record.pod_id,
            report,
        )
        self.cost_store.write(cost)
        self.cost_records.append(cost)
        if report.state is not CloseState.VERIFIED:
            raise StageCloseUnverified(
                f"stage {active.authorization.stage!r} pod {active.record.pod_id!r} close is "
                f"{report.state.value}, not verified"
            )
        return cost

    def _record_launcher_close(
        self, authorization: StageAuthorization, pod_id: str, result: LaunchResult
    ) -> None:
        """Keep the money evidence from a refusal the launcher closed itself."""

        report = result.close_report
        if isinstance(report, CloseReport):
            cost = StageCostRecord(
                authorization.collection_id,
                authorization.stage,
                authorization.authorization_ref,
                pod_id,
                report,
            )
            self.cost_store.write(cost)
            self.cost_records.append(cost)
            return
        self._write_close_failure(
            StageCloseFailure(
                authorization.collection_id,
                authorization.stage,
                authorization.authorization_ref,
                pod_id,
                f"launcher refused with {result.state.value} and returned no close report; "
                f"{result.detail}",
            )
        )

    def _record_close_failure(self, active: ActiveStageBoot, detail: str) -> None:
        self._write_close_failure(
            StageCloseFailure(
                active.authorization.collection_id,
                active.authorization.stage,
                active.authorization.authorization_ref,
                active.record.pod_id,
                detail,
            )
        )

    def _write_close_failure(self, failure: StageCloseFailure) -> None:
        self.cost_store.write_close_failure(failure)
        self.close_failures.append(failure)

    def run(
        self,
        authorization: StageAuthorization,
        request: PodCreateRequest,
        *,
        confirmation: str | None,
        work: Callable[[PodRecord], WorkResult],
    ) -> tuple[WorkResult, StageCostRecord]:
        """Run work on one pod and always return it to pod-down before returning."""

        active = self.boot(authorization, request, confirmation=confirmation)
        work_error: BaseException | None = None
        result: WorkResult | object = _MISSING_WORK_RESULT
        try:
            result = work(active.record)
        except BaseException as error:
            work_error = error
        close_error: BaseException | None = None
        cost: StageCostRecord | None = None
        try:
            cost = self.close(
                active, reason=f"stage {authorization.stage} finished; default pod-down"
            )
        except BaseException as error:
            close_error = error
        if work_error is not None:
            if close_error is not None:
                raise work_error from close_error
            raise work_error
        if close_error is not None:
            raise close_error
        if (
            result is _MISSING_WORK_RESULT or cost is None
        ):  # pragma: no cover - defensive type narrowing
            raise StageBootRefusal("stage lifecycle ended without a work result or cost record")
        return cast(WorkResult, result), cost


def resolve_volume_inputs(tree: object, references: Sequence[dict[str, str]]) -> tuple[bytes, ...]:
    """Read exact input references from the shared volume-hosted run tree.

    A following stage gets only the tree and references from its predecessor;
    no source-pod path survives.  The tree's own resolver still rejects escape
    paths, while this seam verifies every retained digest before work starts.
    """

    read_bytes = getattr(tree, "read_bytes", None)
    if not callable(read_bytes):
        raise TypeError("volume input resolver needs a RunTree-like read_bytes method")
    resolved: list[bytes] = []
    for reference in references:
        if not isinstance(reference, dict) or set(reference) != {"relative_path", "sha256"}:
            raise SchemaRefusal("stage input reference must contain only relative_path and sha256")
        path = reference["relative_path"]
        expected = reference["sha256"]
        if not isinstance(path, str) or not isinstance(expected, str):
            raise SchemaRefusal("stage input reference fields must be strings")
        data = read_bytes(path)
        actual = digest_bytes(data)
        if actual != expected:
            raise SchemaRefusal(
                f"stage input {path!r} has digest {actual}, not the referenced digest {expected}"
            )
        resolved.append(data)
    return tuple(resolved)
