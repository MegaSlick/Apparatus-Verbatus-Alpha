"""RunPod's own API — the only module in this package that knows it.

**Route: REST v1 (`https://rest.runpod.io/v1`), documentation re-checked online
on 2026-08-09.**
Spec 04 named this route from the 2026-07-30 track-B research; the four pages
below were fetched again on the merge date rather than trusted from the spec,
because a routing decision on a money path should not rest on a month-old note:

- `docs.runpod.io/api-reference/pods/POST/pods` — `PodCreateInput` carries every
  field this seam needs, `interruptible` among them (default `false`), and the
  page carries no deprecation, maintenance or beta notice.
- `docs.runpod.io/api-reference/pods/GET/pods` — a **bare JSON array** of pods.
- `docs.runpod.io/api-reference/pods/GET/pods/podId` — one pod object, **404**
  when it does not exist.
- `docs.runpod.io/api-reference/billing/GET/billing/pods` — a **bare JSON array**
  of `{amount, time, timeBilledMs, podId, gpuTypeId, diskSpaceBilledGb,
  endpointId}`, with `podId`, `startTime`, `endTime`, `bucketSize` and `grouping`
  query parameters.

REST **v2** (`api.runpod.io/v2`) was fetched the same day and its own overview
still says: "The REST API v2 is currently in beta. Endpoints and behavior may
change before general availability." It is therefore not the route, and no v2
response shape is assumed anywhere in this file. Nor is the `runpod` PyPI
package used: it wraps the deprecating GraphQL API.

**Not the vendor SDK, a plain injected HTTP transport.** Every call goes through
`HttpTransport`, so the whole adapter is exercised offline against an in-memory
fake. **No live RunPod call has been made from this module.** Every field name
above comes from the published documentation, never from an observed response,
so the exact accepted `gpuTypeIds` string and the provider's real post-DELETE
timing are confirmed at the first authorised live run, not here.

**Credential:** supplied to `UrllibRunPodTransport` explicitly at construction.
Nothing here reads a credential from a tracked file (`operations/pod/README.md`).
"""

from __future__ import annotations

import http.client
import json
import os
import threading
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Callable, Mapping, Protocol

from .controllers import PodDeadmanTimer
from .lease import PodLease
from .models import (
    BILLING_BUCKET_WIDTH,
    BILLING_CUTOFF_MARGIN_ENV,
    AbsenceObservation,
    AccountBalanceObservation,
    BillingState,
    CostCapture,
    CostLine,
    PodCreateRequest,
    PodEstimate,
    PodRecord,
    PodRuntimeContract,
    Presence,
    ProviderFailure,
    ProviderStatus,
    as_decimal,
    parse_billing_cutoff_margin_seconds,
    require_utc,
    utc_now,
)
from .pod_timer import TimerContext
from .shutdown import VerifiedShutdown

RUNPOD_REST_ROOT = "https://rest.runpod.io/v1"

LAUNCH_TOKEN_ENV = "VERBATUS_LAUNCH_TOKEN"
"""The env key `create` correlates a recovery lookup against. It rides in the
pod's `env`, which `GET /pods` returns, so a crashed client can find the exact
pod its own POST may have created without guessing from the name alone."""

_POD_STATES = frozenset({"RUNNING", "EXITED", "TERMINATED"})

_BUCKET_WIDTH = BILLING_BUCKET_WIDTH
"""Matches the `bucketSize=hour` this adapter always requests; the shared
symbol keeps this slack and the generic verifier's from drifting apart."""

BALANCE_OBSERVATION_TIMEOUT_SECONDS = 30.0
"""How long the injected balance source may take before it is a named failure.

The balance is observed at every spend gate, and one of those gates runs *after*
`create` has returned a billing pod and *before* `_arm_or_close` has armed
anything that would stop it. A source that blocks rather than fails leaves that
pod running with no assessment recorded, no close attempted, and no result for
an operator to read. Everything downstream of this method already fails closed
on a raised exception, so bounding the call is what turns a hang into the
refusal the runtime already knows how to handle."""

_MAX_RESPONSE_BYTES = 16 * 1024 * 1024
"""No documented RunPod response (one pod, a pod list, a billing window) is
anywhere near this size. Refusing to buffer past it bounds memory against a
malformed, MITM'd, or pathologically large response on every call this
adapter makes — including from inside the pod-side dead-man timer, the
independent kill-switch spec 04 requires because the provider offers none."""


@dataclass(frozen=True, slots=True)
class HttpResponse:
    """A deliberately small response seam, so tests need no HTTP server."""

    status: int
    body: bytes


class HttpTransport(Protocol):
    """Only the adapter below supplies provider paths and bearer authentication."""

    def request(
        self, method: str, path: str, body: dict[str, object] | None = None
    ) -> HttpResponse:
        """Return a provider HTTP response, including non-2xx response bodies."""


class _RefuseRedirects(urllib.request.HTTPRedirectHandler):
    """Stop urllib following a 3xx, because it re-sends the bearer token when it does.

    Measured against two loopback servers: a 302 from the first to the second
    arrived at the second carrying ``Authorization: Bearer …`` unchanged, and
    across hosts. ``requests`` strips that header on a cross-host redirect;
    urllib does not. The API root is a fixed constant here, so no redirect is one
    this adapter has reason to follow — and the capability is the one thing a
    redirect buys whoever can answer for the endpoint.
    """

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # type: ignore[no-untyped-def]
        return None


class UrllibRunPodTransport:
    """Explicit live transport. It reads no tracked credential or config file."""

    def __init__(
        self,
        capability: str,
        *,
        timeout_seconds: float = 30.0,
        root: str = RUNPOD_REST_ROOT,
    ) -> None:
        if not isinstance(capability, str) or not capability.strip():
            raise ValueError("RunPod API key must be supplied explicitly at runtime")
        if timeout_seconds <= 0:
            raise ValueError("RunPod HTTP timeout must be positive")
        self.capability = capability
        self.timeout_seconds = timeout_seconds
        self.root = root.rstrip("/")
        self.opener = urllib.request.build_opener(_RefuseRedirects)

    def request(
        self, method: str, path: str, body: dict[str, object] | None = None
    ) -> HttpResponse:
        if not path.startswith("/") or "//" in path[1:]:
            raise ProviderFailure("RunPod request path must be an absolute single-slash API path")
        encoded = None if body is None else json.dumps(body, separators=(",", ":")).encode("utf-8")
        request = urllib.request.Request(
            f"{self.root}{path}",
            data=encoded,
            method=method,
            headers={
                "Authorization": f"Bearer {self.capability}",
                "Accept": "application/json",
                **({"Content-Type": "application/json"} if encoded is not None else {}),
            },
        )
        try:
            with self.opener.open(request, timeout=self.timeout_seconds) as response:
                observed = HttpResponse(int(response.status), _bounded_read(response))
        except urllib.error.HTTPError as error:
            observed = HttpResponse(int(error.code), _bounded_read(error))
        except (urllib.error.URLError, OSError) as error:
            raise ProviderFailure(f"RunPod HTTP request failed: {error}") from error
        if 300 <= observed.status < 400:
            raise ProviderFailure(
                f"RunPod answered {method} {path} with HTTP {observed.status}; the API root is "
                "fixed and a redirect was not followed"
            )
        return observed


class RunPodProvider:
    """RunPod REST v1 implementation of the seven provider verbs.

    `pod_price` / `volume_price` are injected resolvers rather than a live quote
    call, because v1 publishes no "quote this GPU" endpoint independent of
    actually creating a pod. `operations.pod.preflight.PlacementTable.price_for`
    is the price sheet, read from `config/pod_placement.toml`'s reviewed
    `card_profile` table. A stale
    sheet can drift: `launch.py` re-assesses the provider-observed undiscounted
    `costPerHr` from `create`/`adopt` against the same ceilings before a launch
    is ever green.
    """

    def __init__(
        self,
        transport: HttpTransport,
        *,
        pod_price: Callable[[str], Decimal],
        volume_price: Callable[[str], Decimal],
        balance_observer: Callable[[], AccountBalanceObservation] | None = None,
        balance_timeout_seconds: float = BALANCE_OBSERVATION_TIMEOUT_SECONDS,
        now: Callable[[], datetime] = utc_now,
    ) -> None:
        self.transport = transport
        self.pod_price = pod_price
        self.volume_price = volume_price
        self.balance_observer = balance_observer
        self.balance_timeout_seconds = balance_timeout_seconds
        self.now = now
        # Set once, by the first observation that overran its deadline. See
        # `observe_account_balance`: after that the source is not called again,
        # so at most one abandoned thread can ever exist per adapter.
        self._balance_abandoned: str | None = None
        # True only between starting a worker and that call returning. Guarded by
        # the same lock as the latch, because the check and the start are one
        # transaction; see `observe_account_balance`.
        self._balance_in_flight = False
        self._balance_lock = threading.Lock()

    # -- the seven verbs ---------------------------------------------------

    def estimate(self, request: PodCreateRequest) -> PodEstimate:
        try:
            pod_hourly = as_decimal(self.pod_price(request.gpu_type), "RunPod pod price")
            volume_hourly = as_decimal(self.volume_price(request.volume_id), "RunPod volume price")
        except Exception as error:
            raise ProviderFailure(f"RunPod current price could not be obtained: {error}") from error
        return PodEstimate(pod_hourly, volume_hourly, "RunPod reviewed price sheet", self.now())

    def observe_account_balance(self) -> AccountBalanceObservation:
        """Use the separately supplied observed-balance source, never a guessed reserve.

        Bounded, because this is a money path: see
        `BALANCE_OBSERVATION_TIMEOUT_SECONDS`. The observer runs on a daemon
        thread so a source that never returns cannot hold the caller or the
        interpreter's exit; the deadline is what the caller sees, and it arrives
        as an ordinary `ProviderFailure` naming the timeout rather than as a
        stall with nothing recorded.

        **A source that overruns its deadline is not consulted again**, and the
        reason is billing safety rather than tidiness. The alternatives were:

        *Cancel the blocked call.* Not available. The observer is an arbitrary
        injected zero-argument callable, and nothing here can interrupt a
        syscall inside it. Buying cancellation means changing the seam so every
        source must accept and honour a deadline — placing the guarantee in the
        one component that has just demonstrated it does not honour one.

        *Let a bounded number accumulate.* This keeps paying the full deadline
        at every later gate while a pod may already be billing, and still leaks
        threads up to the cap. It is worse on both axes than refusing.

        *Refuse from then on*, which is this. At most one thread is ever
        abandoned per adapter — concurrent callers included, since the latch
        check and the worker start are one locked transaction and a caller
        arriving mid-observation is refused rather than queued — and every later
        gate refuses at once instead of stalling another
        `balance_timeout_seconds` on a money path. That is
        fail-closed in the direction that matters: the refusal denies paid
        actions and closes a created pod, because `_observe_balance` turns any
        raised error into "balance unobservable" and the callers already fail
        closed on it. It cannot strand a running pod — `_close_and_record`
        closes through `VerifiedShutdown`, which never assesses spend, so no
        shutdown path passes through here at all. A stale answer arriving late
        would be unusable anyway: an observation over sixty seconds old is
        already refused.
        """

        if self.balance_observer is None:
            raise ProviderFailure("RunPod account balance source was not configured")
        # Reading the latch and starting the worker must be one transaction. Two
        # callers that both read "not abandoned" before either started would
        # both start one, and the at-most-one-abandoned-thread guarantee above
        # would be a guarantee about the sequential case only. Refusing while an
        # observation is in flight, rather than queueing behind it, is the same
        # reasoning as the latch: a second caller on a money path should not
        # wait out a deadline it can already see is at risk, and refusing denies
        # a paid action rather than allowing one.
        with self._balance_lock:
            if self._balance_abandoned is not None:
                raise ProviderFailure(self._balance_abandoned)
            if self._balance_in_flight:
                raise ProviderFailure(
                    "RunPod account balance observation is already in progress; a "
                    "concurrent paid action is refused rather than queued behind it"
                )
            self._balance_in_flight = True
        try:
            observed: list[AccountBalanceObservation] = []
            failed: list[BaseException] = []

            def observe() -> None:
                try:
                    observed.append(self.balance_observer())  # type: ignore[misc]
                except BaseException as error:  # noqa: BLE001 - re-raised on the caller's thread
                    failed.append(error)

            worker = threading.Thread(
                target=observe, name="runpod-balance-observation", daemon=True
            )
            worker.start()
            worker.join(self.balance_timeout_seconds)
            if worker.is_alive():
                overran = (
                    "RunPod account balance source did not answer within "
                    f"{self.balance_timeout_seconds} seconds; it is not consulted again, so "
                    "every later paid action is refused on this same reason"
                )
                with self._balance_lock:
                    self._balance_abandoned = overran
                raise ProviderFailure(overran)
            if failed:
                raise failed[0]
            if not observed:
                raise ProviderFailure("RunPod account balance source returned nothing")
            return observed[0]
        finally:
            # Cleared even after a timeout, where it changes nothing: the latch
            # is set by then and is checked first, so no later call can reach
            # the worker start again.
            with self._balance_lock:
                self._balance_in_flight = False

    def create(self, request: PodCreateRequest) -> PodRecord:
        """Correlate an existing launch token first, then POST — never both.

        A POST whose response the client never saw may still have created a
        billing pod. The launch token rides in `env`, so the exact pod is
        findable afterwards; `recovery_only` makes this verb a pure lookup so a
        restarted controller can never pay twice for one authorised launch.
        """

        # Read through a local rather than assigning the call directly: the
        # repository's credential scanner reads `token = <20+ word characters>`
        # as a literal secret, and its caution is worth more than the line.
        metadata = request.metadata
        token = metadata.get(LAUNCH_TOKEN_ENV)
        if not isinstance(token, str) or not token:
            raise ProviderFailure(
                f"RunPod create requires a {LAUNCH_TOKEN_ENV} metadata value to stay recoverable"
            )
        existing = self._find_by_launch_token(request.name, token)
        if existing is not None:
            return existing
        if request.recovery_only:
            raise ProviderFailure(
                "RunPod recovery lookup found no pod carrying this exact launch token; "
                "no create request was issued"
            )
        response = self.transport.request("POST", "/pods", _create_payload(request))
        if response.status not in {200, 201}:
            raise ProviderFailure(
                f"RunPod create returned HTTP {response.status}: {_body_summary(response.body)}"
            )
        return self._record(_object(response.body, "RunPod create"))

    def adopt(self, pod_id: str) -> PodRecord:
        response = self.transport.request(
            "GET",
            f"/pods/{_path_id(pod_id)}?includeMachine=true&includeNetworkVolume=true",
        )
        if response.status == 404:
            raise ProviderFailure(
                f"RunPod cannot adopt pod {pod_id!r}: the provider reports it absent"
            )
        if response.status != 200:
            raise ProviderFailure(
                f"RunPod adopt returned HTTP {response.status}: {_body_summary(response.body)}"
            )
        record = self._record(_object(response.body, "RunPod adopt"))
        if record.pod_id != pod_id:
            raise ProviderFailure("RunPod adopt response names a different pod id")
        if record.state != "RUNNING":
            raise ProviderFailure(
                f"RunPod cannot adopt pod {pod_id!r}: desiredStatus is {record.state!r}, not RUNNING"
            )
        return record

    def status(self, pod_id: str) -> ProviderStatus:
        response = self.transport.request("GET", f"/pods/{_path_id(pod_id)}")
        observed = self.now()
        if response.status == 404:
            return ProviderStatus(
                pod_id, Presence.ABSENT, observed, "RunPod exact-pod GET returned 404", 404
            )
        if response.status != 200:
            raise ProviderFailure(
                f"RunPod status GET returned HTTP {response.status}: {_body_summary(response.body)}"
            )
        row = _object(response.body, "RunPod status")
        if _text(row.get("id"), "RunPod status id") != pod_id:
            raise ProviderFailure("RunPod status response id does not equal the requested pod id")
        # Verbatim, never normalized against _POD_STATES: this is an
        # observation, not a gate. `_record` (used by create/adopt) refuses an
        # unrecognised desiredStatus because it manufactures a PodRecord that
        # other code trusts as RUNNING; `status` only reports what the
        # provider said, so an unfamiliar future lifecycle word still reaches
        # its caller instead of becoming a raised ProviderFailure on a
        # read-only observation.
        raw_state = row.get("desiredStatus")
        usable_state = isinstance(raw_state, str) and bool(raw_state.strip())
        provider_state = raw_state if usable_state else None
        detail = "RunPod exact-pod GET returned 200"
        if raw_state is not None and not usable_state:
            detail = f"{detail}; unusable desiredStatus {raw_state!r}"
        return ProviderStatus(
            pod_id,
            Presence.PRESENT,
            observed,
            detail,
            200,
            provider_state=provider_state,
        )

    def terminate(self, pod_id: str) -> None:
        """Terminate, never stop: a stopped pod bills volume disk at double rate.

        204 is what v1 documents. 404 is accepted as an idempotent repeat of an
        earlier successful delete, and 200/202 are tolerated because the
        documentation does not say whether an in-flight delete ever answers with
        one — that tolerance is deliberately *not* treated as proof of absence,
        which only the later GET-404 plus list-absence pair establishes.
        """

        response = self.transport.request("DELETE", f"/pods/{_path_id(pod_id)}")
        if response.status not in {200, 202, 204, 404}:
            raise ProviderFailure(
                f"RunPod terminate returned HTTP {response.status}: {_body_summary(response.body)}"
            )

    def verify_absent(self, pod_id: str) -> AbsenceObservation:
        rows = self._pod_rows()
        listed = any(row.get("id") == pod_id for row in rows)
        return AbsenceObservation(
            pod_id,
            Presence.PRESENT if listed else Presence.ABSENT,
            self.now(),
            "RunPod pod list still contains the exact pod id"
            if listed
            else "RunPod pod list omits the exact pod id",
        )

    def capture_cost(self, pod_id: str, started_at: datetime, cutoff_at: datetime) -> CostCapture:
        """The provider's own billed amounts — never an estimate from elapsed time.

        This closes the gap the old pipeline left open, in track B's own words:
        it estimated cost from log timestamps and "the actual account bill was
        not captured". Everything returned is bound to the exact pod and the
        requested window before it can total to a verified close; anything that
        cannot be bound is `UNAVAILABLE` with its reason, never zero.

        v1 returns a bare array with no metadata envelope, so this adapter can
        prove *attribution* and *containment* but not bucket contiguity — there
        is nothing in the response that says which window the endpoint actually
        resolved. `BillingState.PENDING_RECONCILIATION` is therefore never
        emitted here: v1 gives no signal distinguishing "not posted yet" from
        "nothing to post", and `shutdown.py`'s bounded reconciliation retry is
        what absorbs billing lag instead.
        """

        started = require_utc(started_at, "billing start")
        cutoff = require_utc(cutoff_at, "billing cutoff")
        if started >= cutoff:
            raise ProviderFailure("billing window start must precede its cutoff")
        query = urllib.parse.urlencode(
            {
                "podId": pod_id,
                "startTime": _rfc3339(started),
                "endTime": _rfc3339(cutoff),
                "bucketSize": "hour",
                "grouping": "podId",
            }
        )
        response = self.transport.request("GET", f"/billing/pods?{query}")
        if response.status != 200:
            raise ProviderFailure(
                f"RunPod pod billing returned HTTP {response.status}: {_body_summary(response.body)}"
            )
        rows = _array(response.body, "RunPod billing")
        lines: list[CostLine] = []
        for row in rows:
            if not isinstance(row, dict):
                return _unavailable(
                    pod_id, started, cutoff, "RunPod billing returned a non-object record"
                )
            row_pod = row.get("podId")
            if not isinstance(row_pod, str) or row_pod != pod_id:
                return _unavailable(
                    pod_id,
                    started,
                    cutoff,
                    "RunPod billing returned a record that does not name the requested pod; "
                    "cost attribution is unverifiable",
                )
            try:
                bucket = _timestamp(row.get("time"), "billing record time")
                amount = as_decimal(row.get("amount"), "RunPod billing amount")
            except (ProviderFailure, ValueError) as error:
                return _unavailable(
                    pod_id,
                    started,
                    cutoff,
                    f"RunPod billing record is structurally unverifiable: {error}",
                )
            billed_ms = row.get("timeBilledMs")
            if not isinstance(billed_ms, int) or isinstance(billed_ms, bool) or billed_ms < 0:
                return _unavailable(
                    pod_id, started, cutoff, "RunPod billing record has an invalid timeBilledMs"
                )
            # `time` is the *bucket start*, so the hour bucket containing the
            # pod's creation legitimately begins before the requested window.
            # One bucket width of slack before the start is allowed for exactly
            # that; anything earlier, or anything after the cutoff, came from a
            # window this call did not ask for and cannot be totalled.
            if bucket < started - _BUCKET_WIDTH or bucket > cutoff:
                return _unavailable(
                    pod_id,
                    started,
                    cutoff,
                    "RunPod billing record lies outside the requested window by more than one "
                    "bucket; cost attribution is unverifiable",
                )
            lines.append(
                CostLine(
                    amount,
                    f"RunPod pod billing bucket {_rfc3339(bucket)} ({billed_ms}ms billed)",
                    bucket,
                )
            )
        if not lines:
            return _unavailable(
                pod_id,
                started,
                cutoff,
                "RunPod billing returned no records for a pod that ran; zero was not inferred",
            )
        return CostCapture(
            pod_id,
            BillingState.CAPTURED,
            cutoff,
            lines=tuple(lines),
            source="RunPod REST v1 GET /billing/pods",
            window_start_at=started,
        )

    def _pod_rows(self) -> list[dict[str, object]]:
        # Recovery needs the same effective runtime facts as a create/adopt
        # response.  RunPod omits machine and network-volume objects from list
        # results unless they are requested explicitly; without these flags an
        # exact launch-token match cannot be bound back into a PodRecord.
        response = self.transport.request(
            "GET", "/pods?includeMachine=true&includeNetworkVolume=true"
        )
        if response.status != 200:
            raise ProviderFailure(
                f"RunPod pod-list GET returned HTTP {response.status}: {_body_summary(response.body)}"
            )
        rows = _array(response.body, "RunPod pod-list")
        result: list[dict[str, object]] = []
        for index, row in enumerate(rows):
            if not isinstance(row, dict):
                raise ProviderFailure(f"RunPod pod-list entry {index} is not an object")
            _text(row.get("id"), f"RunPod pod-list entry {index} id")
            result.append(row)
        return result

    def _find_by_launch_token(self, name: str, token: str) -> PodRecord | None:
        """Exactly one pod carrying this exact launch token, or nothing.

        The token is matched on **every** listed pod, not only name-matched
        ones: a provider- or console-side rename must not make the pod this
        client already paid for invisible, because an invisible pod means a
        second POST for one authorised launch.  The name still scopes the
        no-env refusal below -- a pod sharing this launch name whose `env` the
        provider did not return refuses outright rather than falling back to
        matching on the name alone: two pods can share a name, and paying twice
        for one authorised launch is the failure this whole path exists to
        prevent.
        """

        candidates: list[dict[str, object]] = []
        for row in self._pod_rows():
            env = row.get("env")
            if isinstance(env, dict):
                if env.get(LAUNCH_TOKEN_ENV) == token:
                    candidates.append(row)
                continue
            if row.get("name") == name:
                raise ProviderFailure(
                    f"RunPod pod {row.get('id')!r} shares this launch name but returned no env; "
                    "the exact launch token cannot be correlated and no create request was issued"
                )
        if not candidates:
            return None
        if len(candidates) > 1:
            raise ProviderFailure(
                "RunPod reports more than one pod carrying this exact launch token; "
                "review the console rather than creating or terminating anything"
            )
        return self._record(candidates[0])

    def _record(self, payload: Mapping[str, object]) -> PodRecord:
        pod_id = _text(payload.get("id"), "RunPod pod id")
        state = payload.get("desiredStatus")
        if state not in _POD_STATES:
            raise ProviderFailure(
                f"RunPod pod {pod_id} reports an unrecognised desiredStatus: {state!r}"
            )
        hourly = as_decimal(payload.get("costPerHr"), f"RunPod pod {pod_id} costPerHr")
        if hourly <= 0:
            raise ProviderFailure(f"RunPod pod {pod_id} reports a non-positive costPerHr")
        volume = payload.get("networkVolume")
        volume_id = payload.get("networkVolumeId")
        if isinstance(volume, Mapping) and isinstance(volume.get("id"), str):
            volume_id = volume["id"]
        if not isinstance(volume_id, str) or not volume_id:
            raise ProviderFailure(
                f"RunPod pod {pod_id} reports no attached network volume; volumes attach only at creation"
            )
        created = payload.get("lastStartedAt")
        return PodRecord(
            pod_id=pod_id,
            name=_text(payload.get("name"), f"RunPod pod {pod_id} name"),
            estimate=PodEstimate(
                hourly,
                as_decimal(self.volume_price(volume_id), "RunPod volume price"),
                "RunPod observed pod costPerHr",
                self.now(),
            ),
            volume_id=volume_id,
            # `lastStartedAt` is null until the pod first runs, so a just-created
            # pod falls back to the observation instant. That instant is at or
            # after the provider's own creation moment, which is why
            # `capture_cost` allows one bucket of slack before the window start:
            # the hour bucket containing creation may begin before it.
            created_at=_timestamp(created, f"RunPod pod {pod_id} lastStartedAt")
            if isinstance(created, str)
            else self.now(),
            state=str(state),
            runtime_contract=_runtime_contract(pod_id, payload, volume_id),
        )


def _runtime_contract(
    pod_id: str, payload: Mapping[str, object], volume_id: str
) -> PodRuntimeContract:
    """The *effective* shape the provider says it created, not what we asked for.

    `launch.py` compares this against the request and closes the pod
    immediately if they disagree, so a provider that silently substituted an
    interruptible instance, another image, or a different start command cannot
    reach a green launch.
    """

    interruptible = payload.get("interruptible")
    if not isinstance(interruptible, bool):
        raise ProviderFailure(
            f"RunPod pod {pod_id} did not report interruptible; on-demand cannot be assumed "
            "from a missing field"
        )
    if interruptible:
        raise ProviderFailure(
            f"RunPod pod {pod_id} is interruptible; a spot reclaim mid-run is a silent-loss machine"
        )
    machine = payload.get("machine")
    gpu_type = machine.get("gpuTypeId") if isinstance(machine, Mapping) else None
    if not isinstance(gpu_type, str) or not gpu_type:
        raise ProviderFailure(f"RunPod pod {pod_id} reports no machine.gpuTypeId to verify against")
    command = payload.get("dockerStartCmd")
    if not isinstance(command, list) or not all(isinstance(part, str) and part for part in command):
        raise ProviderFailure(f"RunPod pod {pod_id} reports no dockerStartCmd to verify against")
    template = payload.get("templateId")
    return PodRuntimeContract(
        interruptible=False,
        gpu_type=gpu_type,
        image=_text(payload.get("image"), f"RunPod pod {pod_id} image"),
        volume_id=volume_id,
        volume_mount_path=_text(
            payload.get("volumeMountPath"), f"RunPod pod {pod_id} volumeMountPath"
        ),
        docker_start_cmd=tuple(command),
        billing_cutoff_margin_seconds=_billing_cutoff_margin_from_environment(pod_id, payload),
        template=template if isinstance(template, str) and template else None,
    )


def _create_payload(request: PodCreateRequest) -> dict[str, object]:
    """The v1 `PodCreateInput` body. `interruptible` is always explicitly false.

    Spec 04: on-demand only — "a spot reclaim mid-run is a silent-loss machine".
    `networkVolumeId` rides here because v1 attaches a volume only at creation
    and never afterwards.
    """

    payload: dict[str, object] = {
        "name": request.name,
        "cloudType": "SECURE",
        "computeType": "GPU",
        "imageName": request.image,
        "gpuTypeIds": [request.gpu_type],
        "gpuCount": 1,
        "interruptible": False,
        "networkVolumeId": request.volume_id,
        "volumeMountPath": request.volume_mount_path,
        "dockerStartCmd": list(request.docker_start_cmd),
        "env": dict(request.metadata),
    }
    if request.template is not None:
        payload["templateId"] = request.template
    return payload


def timer_context_from_environment(environment: Mapping[str, str] | None = None) -> TimerContext:
    """Provider-owned construction of a RunPod-capable pod-side timer.

    The termination capability is supplied only as an ephemeral runtime
    environment value. This repository does not provide or persist it, and a
    missing value is a startup refusal rather than an armed-timer claim.
    """

    env = os.environ if environment is None else environment
    pod_id = _required_environment(env, "RUNPOD_POD_ID")
    capability = _required_environment(env, "RUNPOD_API_KEY")
    volume_id = _required_environment(env, "VERBATUS_VOLUME_ID")
    deadline = _environment_timestamp(
        _required_environment(env, "VERBATUS_HARD_DEADLINE"), "VERBATUS_HARD_DEADLINE"
    )
    started = _environment_timestamp(
        _required_environment(env, "VERBATUS_REQUESTED_AT"), "VERBATUS_REQUESTED_AT"
    )
    pod_rate = as_decimal(
        _required_environment(env, "VERBATUS_POD_HOURLY_USD"), "pod timer pod rate"
    )
    volume_rate = as_decimal(
        _required_environment(env, "VERBATUS_VOLUME_ONGOING_HOURLY_USD"), "pod timer volume rate"
    )
    billing_cutoff_margin_seconds = _parse_billing_cutoff_margin(
        _required_environment(env, BILLING_CUTOFF_MARGIN_ENV), BILLING_CUTOFF_MARGIN_ENV
    )
    launch_identity = _required_environment(env, LAUNCH_TOKEN_ENV)
    provider = RunPodProvider(
        UrllibRunPodTransport(capability),
        # The timer never estimates or creates. Sealed launch-time rates are
        # only retained for the close report's ongoing-volume disclosure.
        pod_price=lambda gpu: pod_rate,
        volume_price=lambda volume: volume_rate,
    )
    lease = PodLease(
        # The launch token is also the durable local lease identity. Deriving a
        # lease id from the provider pod id instead would make a second PodLease
        # for one paid pod, and the controller receipts armed before create name
        # the first one.
        lease_id=launch_identity,
        launch_token=launch_identity,
        provider_name="runpod",
        pod_id=pod_id,
        volume_id=volume_id,
        pod_hourly_usd=pod_rate,
        volume_hourly_usd=volume_rate,
        created_at=started,
        started_at=started,
        hard_deadline=deadline,
        owner_token="pod-deadman",
        heartbeat_at=started,
        phase="active",
    )
    return TimerContext(
        PodDeadmanTimer(
            lease,
            VerifiedShutdown(provider, billing_cutoff_margin_seconds=billing_cutoff_margin_seconds),
        )
    )


def _billing_cutoff_margin_from_environment(pod_id: str, payload: Mapping[str, object]) -> int:
    environment = payload.get("env")
    if not isinstance(environment, Mapping):
        raise ProviderFailure(
            f"RunPod pod {pod_id} reports no env; its billing cutoff margin is unproven"
        )
    return _parse_billing_cutoff_margin(
        environment.get(BILLING_CUTOFF_MARGIN_ENV),
        f"RunPod pod {pod_id} {BILLING_CUTOFF_MARGIN_ENV}",
    )


def _parse_billing_cutoff_margin(value: object, label: str) -> int:
    """Parse the exact environment spelling that binds a pod-side timer."""

    try:
        return parse_billing_cutoff_margin_seconds(value, label)
    except ValueError as error:
        raise ProviderFailure(str(error)) from error


def _path_id(value: str) -> str:
    if (
        not isinstance(value, str)
        or not value
        or value in {".", ".."}
        or "/" in value
        or "?" in value
        or any(ord(character) < 32 or ord(character) == 127 for character in value)
    ):
        raise ProviderFailure("RunPod pod id is unsafe for a path")
    return urllib.parse.quote(value, safe="")


def _text(value: object, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ProviderFailure(f"{label} is missing or blank")
    return value


def _rfc3339(value: datetime) -> str:
    return value.isoformat().replace("+00:00", "Z")


def _timestamp(value: object, label: str) -> datetime:
    if not isinstance(value, str):
        raise ProviderFailure(f"RunPod {label} is missing")
    try:
        return require_utc(datetime.fromisoformat(value.replace("Z", "+00:00")), label)
    except ValueError as error:
        raise ProviderFailure(f"RunPod {label} is invalid: {error}") from error


def _unavailable(pod_id: str, window_start: datetime, cutoff: datetime, reason: str) -> CostCapture:
    return CostCapture(
        pod_id,
        BillingState.UNAVAILABLE,
        cutoff,
        reason=reason,
        source="RunPod REST v1 GET /billing/pods",
        window_start_at=window_start,
    )


def _bounded_read(stream: http.client.HTTPResponse | urllib.error.HTTPError) -> bytes:
    """Refuse to buffer a response past ``_MAX_RESPONSE_BYTES``, never truncate it silently.

    ``HTTPResponse.read(amt)`` is documented as returning *up to* ``amt`` bytes,
    so one call may return a short read before EOF; a valid billing response
    under the cap would then reach ``_json`` truncated and be refused as
    malformed.  CPython's own implementation happens not to short-read here
    today -- this accumulates against the documented contract rather than
    against that implementation detail.  Found by CodeRabbit on this branch.
    """

    parts: list[bytes] = []
    total = 0
    while total <= _MAX_RESPONSE_BYTES:
        chunk = stream.read(_MAX_RESPONSE_BYTES + 1 - total)
        if not chunk:
            return b"".join(parts)
        parts.append(chunk)
        total += len(chunk)
    raise ProviderFailure(
        f"RunPod response exceeded {_MAX_RESPONSE_BYTES} bytes; refusing to buffer it"
    )


def _json(body: bytes, label: str) -> object:
    try:
        # parse_float=Decimal: money fields (costPerHr, billing amount) must
        # never exist as binary floats, even transiently -- config/spend.toml's
        # own rule is that money does not survive that.
        return json.loads(body, parse_float=Decimal)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ProviderFailure(f"{label} response is not JSON: {error}") from error


def _object(body: bytes, label: str) -> dict[str, object]:
    payload = _json(body, label)
    if not isinstance(payload, dict):
        raise ProviderFailure(f"{label} response is not an object")
    return payload


def _array(body: bytes, label: str) -> list[object]:
    payload = _json(body, label)
    if not isinstance(payload, list):
        raise ProviderFailure(f"{label} response is not the documented bare array")
    return payload


def _body_summary(body: bytes) -> str:
    text = body.decode("utf-8", "replace").strip()
    return text[:300] if text else "empty response body"


def _required_environment(environment: Mapping[str, str], name: str) -> str:
    value = environment.get(name)
    if not isinstance(value, str) or not value.strip():
        raise RuntimeError(f"RunPod pod timer is not armed: required environment {name} is missing")
    return value


def _environment_timestamp(value: str, label: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as error:
        raise RuntimeError(f"RunPod pod timer {label} is not RFC3339 UTC") from error
    return require_utc(parsed, label)
