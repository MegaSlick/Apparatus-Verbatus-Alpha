"""RunPod's own API — the only module in this package that knows it.

Two REST routes live here side by side, behind one seam: `RunPodV2Provider`
(the default, `live_runpod_provider`) and `RunPodProvider` (v1, kept until the
first live run under v2 is green, then deleted in its own commit).
`operations/pod/V2_MIGRATION.md` maps one to the other field by field and
records the documentation pages each field and refusal below rests on.

**REST v2** (`https://api.runpod.io/v2`), source pages read online:
- `api-reference-v2/pods/create-a-pod` (2026-09-02) — `CreatePodRequest`: no
  `interruptible`, bid or rental-type field.
- `https://api.runpod.io/v2/openapi.json` (2026-09-02) — confirms no
  rental-type field anywhere in the schema.
- `api-reference-v2/pods/list-pods` (2026-09-24) — paginated
  (`cursor`/`limit`, `nextCursor` null on the last page); pagination is new
  since the 2026-09-02 reading, so the list is followed to its last page.
- `api-reference-v2/pods/terminate-a-pod` (2026-09-02) — `204`/`404`/`409`
  ("Pod belongs to a cluster and cannot be terminated via the pod
  endpoints.").
- `api-reference-v2/billing/get-pod-billing-history` (2026-09-02) — records
  wrapped in `{"records": [...], "metadata": {...}}`.
- `api-reference-v2/migrate-from-v1` (2026-09-02) — maps no field for
  `interruptible`.
- `docs.runpod.io/pods/pricing` (2026-09-02) — only "On-demand" and "Savings
  plans"; no "spot" or "interruptible".
- `api-reference/pods/POST/pods` (v1, 2026-09-02) — still documents
  `interruptible`, so spot pods still exist and no v2 page says what a
  create without the field produces. **That is why `V2_ON_DEMAND_BASIS` is
  unset and a v2 create refuses** (`RunPodV2Provider`).

`operations/pod/V2_MIGRATION.md` records the full page-by-page reading;
this list is the terse form kept beside the code it settles.

**REST v1** (`https://rest.runpod.io/v1`), read online 2026-08-09 and
re-checked 2026-09-02: `api-reference/pods/POST/pods` carries `interruptible`
(default `false`); RunPod retires the route on 2026-11-15
(`V2_MIGRATION.md`). `GET /pods` and the billing endpoint each return a bare
JSON array rather than v2's envelope. The `runpod` PyPI package is not used:
it wraps the deprecating GraphQL API.

**Not the vendor SDK, a plain injected HTTP transport.** Every call goes
through `HttpTransport`, so the whole adapter is exercised offline against an
in-memory fake. **No live RunPod call has been made from this module, on
either route** — every field name here comes from published documentation,
never an observed response, so exact GPU id strings and post-DELETE timing
are confirmed at the first authorised live run, not here.

**Account balance is GraphQL**, not REST: only `myself { clientBalance
currentSpendPerHr }` publishes it, sent with the key as an `api_key` query
parameter (GraphQL documents no header form, so every error string and
fixture record here scrubs the query). Neither field's documentation names a
currency; `BALANCE_CURRENCY` records a documented reading from the billing
pages instead, and the first authorised live run checks it against the
console. GraphQL itself is deprecated, retiring in early 2027 in favour of
v2 -- a sunset of this observer's own, distinct from the REST v1 route's:
v2 publishes no balance, so this observer is not retired by that migration.

**Credential:** supplied to `UrllibRunPodTransport` explicitly at
construction. Nothing here reads a credential from a tracked file
(`operations/pod/README.md`). The GraphQL sibling transport is derived from
the REST one by `UrllibRunPodTransport.sibling`, so the provider never
handles the key itself.
"""

from __future__ import annotations

import contextlib
import http.client
import json
import os
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Callable, Final, Mapping, Protocol

from ..http_deadline import DeadlineExceeded, call_within_deadline, recording_opener
from . import notify_hooks
from .controllers import PodDeadmanTimer
from .fixture import FixtureRecorder, RecordingTransport
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
    TerminateRefused,
    as_decimal,
    looks_like_credential_field,
    parse_billing_cutoff_margin_seconds,
    require_utc,
    utc_now,
)
from .pod_timer import TimerContext
from .shutdown import VerifiedShutdown

RUNPOD_REST_ROOT = "https://rest.runpod.io/v1"
"""REST v1, `RunPodProvider`'s root. RunPod retires it on 2026-11-15."""

RUNPOD_V2_ROOT = "https://api.runpod.io/v2"
"""REST v2, `RunPodV2Provider`'s root; paths below it carry no ``/v2`` prefix."""

RUNPOD_ROUTES: Final = ("v1", "v2")
RUNPOD_DEFAULT_ROUTE: Final = "v2"
"""The route `live_runpod_provider` and the pod-side timer use unless told otherwise."""

RUNPOD_ROUTE_ENV: Final = "VERBATUS_RUNPOD_ROUTE"
"""The pod environment key that seals the route a pod was created through.

Each adapter's create adds it to the pod's ``env`` with its own ``ROUTE``, and
`timer_context_from_environment` requires it, so the pod-side timer closes its
pod through the same route -- and the same credential scope -- the launch used.
A timer that guessed a default could be the one controller that cannot
terminate its own pod at the hard deadline."""

V2_ON_DEMAND_BASIS: Final[str | None] = None
"""The documented basis on which a v2 pod is on-demand, or ``None`` while there is none.

REST v2 has no ``interruptible`` field on create and no rental-type field on the
pod (module docstring), so the adapter cannot request on-demand or read it
back. While this is ``None``, `RunPodV2Provider.create` refuses before any POST
and every v2 pod record carries no runtime contract. Setting it is the project
lead's decision, and it is set only in a reviewed commit that names the page or
vendor answer that settles it."""

V2_ON_DEMAND_REFUSAL: Final = (
    "RunPod REST v2 cannot show that a pod is on-demand: its create body has no "
    "interruptible field and its pod object reports no rental type, and a spot reclaim "
    "mid-run is a silent-loss machine. Launch through REST v1 "
    '(live_runpod_provider(..., route="v1")) until the project lead records a documented '
    "basis in provider_runpod.V2_ON_DEMAND_BASIS"
)

_V1_BILLING_SOURCE: Final = "RunPod REST v1 GET /billing/pods"
_V2_BILLING_SOURCE: Final = "RunPod REST v2 GET /billing/pods"

RUNPOD_GRAPHQL_ROOT = "https://api.runpod.io"
GRAPHQL_PATH = "/graphql"
"""Where the account balance lives. REST v1 and v2 both lack it (module docstring)."""

BALANCE_QUERY = "query { myself { clientBalance currentSpendPerHr } }"
"""Exactly the two fields the spend gate needs; nothing else is requested, so a
response carrying anything credential-shaped is refused rather than trusted."""

BALANCE_CURRENCY = "US dollars per the vendor's billing documentation"
"""Observed from the pages the module docstring names, never from the query."""

_CREDENTIAL_PLACEMENTS = frozenset({"header", "query"})

LAUNCH_TOKEN_ENV = "VERBATUS_LAUNCH_TOKEN"
"""The env key `create` correlates a recovery lookup against. It rides in the
pod's `env`, which `GET /pods` returns, so a crashed client can find the exact
pod its own POST may have created without guessing from the name alone."""

_POD_STATES = frozenset({"RUNNING", "EXITED", "TERMINATED"})
"""v1's ``desiredStatus`` vocabulary."""

_V2_POD_STATES = frozenset({"PROVISIONING", "STARTING", "RUNNING", "EXITED", "ERROR", "TERMINATED"})
"""v2's ``PodStatus`` enum. `models.PRE_RUNNING_STATES` names the two the runtime waits on."""

_MAX_POD_LIST_PAGES: Final = 20
"""v2 pages the pod list at up to 1000 pods a page. Twenty pages is far past
any account this runtime serves; beyond it the list refuses rather than stops."""

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
        credential_placement: str = "header",
    ) -> None:
        if not isinstance(capability, str) or not capability.strip():
            raise ValueError("RunPod API key must be supplied explicitly at runtime")
        if timeout_seconds <= 0:
            raise ValueError("RunPod HTTP timeout must be positive")
        if credential_placement not in _CREDENTIAL_PLACEMENTS:
            raise ValueError("RunPod credential placement must be 'header' or 'query'")
        self.capability = capability
        self.timeout_seconds = timeout_seconds
        self.root = root.rstrip("/")
        # "header" is REST's documented `Authorization: Bearer`; "query" is
        # GraphQL's documented `?api_key=` (module docstring). The query form
        # is used only where the documentation offers nothing else.
        self.credential_placement = credential_placement

    def sibling(self, *, root: str, credential_placement: str) -> "UrllibRunPodTransport":
        """The same capability and timeout, pointed at another documented root.

        The provider derives its GraphQL transport through this rather than by
        reading ``capability`` itself: the key stays inside transports.
        """

        return UrllibRunPodTransport(
            self.capability,
            timeout_seconds=self.timeout_seconds,
            root=root,
            credential_placement=credential_placement,
        )

    def request(
        self, method: str, path: str, body: dict[str, object] | None = None
    ) -> HttpResponse:
        if not path.startswith("/") or "//" in path[1:]:
            raise ProviderFailure("RunPod request path must be an absolute single-slash API path")
        encoded = None if body is None else json.dumps(body, separators=(",", ":")).encode("utf-8")
        url = f"{self.root}{path}"
        headers = {
            "Accept": "application/json",
            **({"Content-Type": "application/json"} if encoded is not None else {}),
        }
        if self.credential_placement == "query":
            if "?" in path:
                raise ProviderFailure(
                    "RunPod query-placed credential cannot share a path that already carries a "
                    "query string"
                )
            url = f"{url}?{urllib.parse.urlencode({'api_key': self.capability})}"
        else:
            headers["Authorization"] = f"Bearer {self.capability}"
        request = urllib.request.Request(url, data=encoded, method=method, headers=headers)
        # `timeout_seconds` bounds the whole call -- connect, headers and body
        # against one monotonic deadline -- rather than one blocking receive:
        # a loopback responder dribbling a byte at a time answered a 0.15 s
        # budget after 8.559 s. Every caller here is a money-path verb whose
        # controller checks its own deadline only between calls, so an
        # unbounded call is an unbounded controller.
        deadline = time.monotonic() + self.timeout_seconds
        # Environment proxy discovery is left ON for this opener, deliberately, and this is
        # the opposite decision from `operations/serving/http.py`, which disables
        # it with an explicit `ProxyHandler({})`. The two are different
        # boundaries. That one addresses 127.0.0.1 and a proxy there means the
        # request left the machine, which is the defect. This one addresses
        # `rest.runpod.io`/`api.runpod.io` — an external service by design — and
        # an operator on a network whose only route out is a proxy has to be
        # able to reach it or no pod can ever be closed. The capability is not
        # exposed to that proxy: both roots are HTTPS, so urllib issues
        # `CONNECT` and the bearer header (or the query-placed key) travels
        # inside TLS the proxy cannot read. A proxy that answers for the API
        # anyway is a machine-in-the-middle the certificate check already
        # refuses.
        opener, cancel = recording_opener(_RefuseRedirects)

        def exchange() -> HttpResponse:
            try:
                with opener.open(request, timeout=self.timeout_seconds) as response:
                    return HttpResponse(int(response.status), _bounded_read(response, deadline))
            except urllib.error.HTTPError as error:
                # Closed explicitly: `urlopen` hands an error response to its
                # caller rather than to the `with` above, so nothing closed it.
                with contextlib.closing(error):
                    return HttpResponse(int(error.code), _bounded_read(error, deadline))

        try:
            observed = call_within_deadline(
                exchange,
                budget_seconds=self.timeout_seconds,
                # The path, never the URL: in query placement the URL carries
                # the key, and this label reaches the refusal below and records.
                label=f"RunPod {method} {path}",
                cancel=cancel,
            )
        except DeadlineExceeded as error:
            # A mutating verb interrupted here has an *unknown* outcome, and
            # this refusal deliberately says nothing about whether the provider
            # acted. `RunPodProvider.create` is what preserves that: it
            # correlates the launch token before it ever POSTs, so a create
            # whose response was never seen is found rather than re-issued, and
            # `recovery_only` makes the recovery path a pure lookup. Nothing
            # here may retry a mutating call.
            raise ProviderFailure(f"RunPod HTTP request failed: {error}") from error
        except (urllib.error.URLError, OSError) as error:
            # `reason`, never `str(error)` with a URL in it: in query placement
            # the URL carries the key, and this message reaches records.
            raise ProviderFailure(
                f"RunPod HTTP request failed: {getattr(error, 'reason', error)}"
            ) from error
        if 300 <= observed.status < 400:
            raise ProviderFailure(
                f"RunPod answered {method} {path} with HTTP {observed.status}; the API root is "
                "fixed and a redirect was not followed"
            )
        return observed


class GraphQLBalanceObserver:
    """`myself { clientBalance currentSpendPerHr }`, refused by name on every doubt.

    The zero-argument callable `RunPodProvider.observe_account_balance` runs
    on its bounded thread. It refuses, naming the reason: a non-200 status; a
    3xx (the transport already refuses to follow one); a body that is not a
    JSON object; a GraphQL `errors` array; a missing `data`, `myself`,
    `clientBalance` or `currentSpendPerHr`; a value that is not a JSON number
    (`null`, a string, a boolean); a negative balance, since
    `AccountBalanceObservation` cannot carry one and a gate that read it
    as zero would be wrong in the unsafe direction; and any key anywhere in
    the response that looks credential-shaped, because the query asked for
    two numbers and a body carrying a key or token is not the answer to it.
    """

    def __init__(
        self,
        transport: HttpTransport,
        *,
        now: Callable[[], datetime] = utc_now,
        notify: Callable[[Decimal, Decimal], object] | None = None,
    ) -> None:
        self.transport = transport
        self.now = now
        # `None` by default -- exactly like `balance_observer` itself two
        # classes up -- so every offline test that builds this observer
        # directly, as most of this file's tests do, never touches
        # `operations/notify/notify.sh`. The real `notify_hooks.notify_balance`
        # arrives only through `RunPodProvider`: as its `balance_notify`
        # argument when *it* builds this observer as the default for a live
        # transport (below), or through `set_balance_notify`, which is what
        # `cli.py --notify` reaches. No offline test both builds and calls it.
        self.notify = notify

    def __call__(self) -> AccountBalanceObservation:
        response = self.transport.request("POST", GRAPHQL_PATH, {"query": BALANCE_QUERY})
        if response.status != 200:
            raise ProviderFailure(
                f"RunPod balance query returned HTTP {response.status}: "
                f"{_body_summary(response.body)}"
            )
        payload = _object(response.body, "RunPod balance")
        _refuse_credential_shaped(payload, "RunPod balance response")
        errors = payload.get("errors")
        if errors:
            raise ProviderFailure(
                f"RunPod balance query answered with errors: {_first_error(errors)}"
            )
        data = payload.get("data")
        if not isinstance(data, Mapping):
            raise ProviderFailure("RunPod balance response is missing field data")
        myself = data.get("myself")
        if not isinstance(myself, Mapping):
            raise ProviderFailure("RunPod balance response is missing field data.myself")
        balance = _money_field(myself, "clientBalance")
        spend_per_hour = _money_field(myself, "currentSpendPerHr")
        observed_at = self.now()
        source = (
            f"RunPod GraphQL myself.clientBalance ({BALANCE_CURRENCY}); "
            f"currentSpendPerHr={spend_per_hour}"
        )
        note = self._ping(balance, spend_per_hour)
        if note is not None:
            source = f"{source}; {note}"
        return AccountBalanceObservation(balance, observed_at, source)

    def _ping(self, balance: Decimal, spend_per_hour: Decimal) -> str | None:
        """Notify the phone; return a note when the ping did not land.

        Best-effort, never raised: a notification hook must never turn a
        successful observation into a failed one (spend machinery is tracking
        plus notifications only, no new enforcement). But a ping that was
        refused on sight, never delivered, or raised is itself a fact about
        this observation, and principle 2 does not let it disappear into a
        bare ``pass``. It comes back as a note appended to the observation's own
        ``source``, which every spend assessment and launch record already
        carries, so a phone that never rang says so where the money decision
        is written down. A delivered ping adds nothing: the caller that wired
        the hook records that outcome itself.
        """

        if self.notify is None:
            return None
        try:
            outcome = self.notify(balance, spend_per_hour)
        except Exception as error:  # noqa: BLE001 - a notification hook must never propagate
            detail = f"balance notification raised and was contained: {error!r}"
            if len(detail) > 160:
                detail = f"{detail[:160]} (reason truncated at 160 characters)"
            return detail
        if getattr(outcome, "delivered", False):
            return None
        line = getattr(outcome, "line", None)
        if callable(line):
            return f"balance notification: {line()}"
        return (
            "balance notification: the hook returned "
            f"{type(outcome).__name__}, which reports no outcome"
        )


def _money_field(row: Mapping[str, object], name: str) -> Decimal:
    if name not in row:
        raise ProviderFailure(f"RunPod balance response is missing field data.myself.{name}")
    value = row[name]
    if value is None or isinstance(value, bool) or not isinstance(value, (int, Decimal)):
        raise ProviderFailure(
            f"RunPod balance field {name} is not a number: {type(value).__name__}"
        )
    parsed = Decimal(value)
    if not parsed.is_finite() or parsed < 0:
        raise ProviderFailure(
            f"RunPod balance field {name} is {parsed}; a negative or non-finite value cannot "
            "clear a balance floor and is refused rather than read as zero"
        )
    return parsed


def _refuse_credential_shaped(value: object, label: str, where: str = "") -> None:
    if isinstance(value, Mapping):
        for key, item in value.items():
            name = str(key)
            if looks_like_credential_field(name):
                raise ProviderFailure(
                    f"{label} carries a credential-shaped field {where}{name}; the query asked "
                    "for two numbers and this answer is refused unread"
                )
            _refuse_credential_shaped(item, label, f"{where}{name}.")
    elif isinstance(value, list):
        for item in value:
            _refuse_credential_shaped(item, label, where)


def _first_error(errors: object) -> str:
    if isinstance(errors, list) and errors and isinstance(errors[0], Mapping):
        message = errors[0].get("message")
        if isinstance(message, str) and message.strip():
            return message.strip()[:300]
    return "unreadable error payload"


def _body_summary(body: bytes) -> str:
    text = body.decode("utf-8", "replace").strip()
    return text[:300] if text else "empty response body"


def _problem_summary(body: bytes) -> str:
    """v2's RFC 9457 problem body as one line, or the raw summary when it is not one."""

    try:
        problem = json.loads(body)
    except (UnicodeDecodeError, json.JSONDecodeError, RecursionError):
        return _body_summary(body)
    if not isinstance(problem, dict) or not isinstance(problem.get("title"), str):
        return _body_summary(body)
    text = str(problem["title"])
    if isinstance(problem.get("detail"), str) and problem["detail"].strip():
        text = f"{text}: {problem['detail'].strip()}"
    errors = problem.get("errors")
    if isinstance(errors, list) and errors:
        text = f"{text}; errors: {json.dumps(errors, separators=(',', ':'), default=str)}"
    return text[:300]


def _pod_environment(request: PodCreateRequest, route: str) -> dict[str, str]:
    """The request's metadata plus the sealed route, refusing a conflicting one."""

    environment = dict(request.metadata)
    stated = environment.get(RUNPOD_ROUTE_ENV)
    if stated is not None and stated != route:
        raise ProviderFailure(
            f"RunPod create request carries {RUNPOD_ROUTE_ENV}={stated!r} but is being sent "
            f"through REST {route}; the pod-side timer must close through the route that "
            "created it. No create request was issued"
        )
    environment[RUNPOD_ROUTE_ENV] = route
    return environment


class _RunPodAdapter:
    """What both REST routes share: prices, balance, fixture recording, token lookup.

    `pod_price` / `volume_price` are injected resolvers rather than a live quote
    call, because neither route prices a pod independent of creating it (v2's
    catalogue lists prices, but `config/pod_placement.toml`'s reviewed sheet
    stays the authority; `RunPodV2Provider.cross_check_catalogue` compares the
    two). `operations.pod.preflight.PlacementTable.price_for` is the price
    sheet. A stale sheet can drift: `launch.py` re-assesses the
    provider-observed hourly rate from `create`/`adopt` against the same
    ceilings before a launch is ever green.

    Each route subclass names its ``ROOT``, and a live transport pointed at a
    different root is refused at construction: v1 paths sent to the v2 root, or
    the reverse, would read documented-looking 404s as absence.
    """

    ROOT: str = ""
    ROUTE: str = ""
    _INCLUDE_QUERY = ""
    _STATE_FIELD = ""
    _STARTED_FIELD = ""
    _POD_LIST_LABEL = "RunPod pod list"
    _summary = staticmethod(_body_summary)

    def __init__(
        self,
        transport: HttpTransport,
        *,
        pod_price: Callable[[str], Decimal],
        volume_price: Callable[[str], Decimal],
        balance_observer: Callable[[], AccountBalanceObservation] | None = None,
        balance_timeout_seconds: float = BALANCE_OBSERVATION_TIMEOUT_SECONDS,
        now: Callable[[], datetime] = utc_now,
        balance_notify: Callable[[Decimal, Decimal | None], notify_hooks.NotifyOutcome]
        | None = None,
    ) -> None:
        if isinstance(transport, UrllibRunPodTransport) and transport.root != self.ROOT:
            raise ValueError(
                f"{type(self).__name__} speaks RunPod REST {self.ROUTE} at {self.ROOT}; this "
                f"transport points at {transport.root}. Build the transport with "
                f"root={self.ROOT!r}, or use live_runpod_provider, which pairs them"
            )
        self.transport = transport
        self.pod_price = pod_price
        self.volume_price = volume_price
        if balance_observer is None and isinstance(transport, UrllibRunPodTransport):
            # The default observer exists exactly when a live credential does:
            # a fake transport gets none, so the offline suite's "balance
            # source was not configured" refusal is still reachable, and the
            # provider never touches the key -- `sibling` carries it across.
            # `balance_notify` here and `set_balance_notify` below are the ONLY
            # two ways a phone notification reaches this observer, and both are
            # opt-in: the parameter defaults to `None`, so a bare live-transport
            # provider -- including the pod-side one `timer_context_from_
            # environment` builds, and any host call that omits `--notify`
            # -- carries no hook at all, never pinging a phone unasked. The
            # host CLI reaches the seam rather than this parameter, because the
            # provider comes from an untracked `--provider-factory` that this
            # tree never constructs: `cli.py`'s `_wire_balance_notify` calls
            # `set_balance_notify` under `args.notify`, duck-typed exactly as
            # `--record-fixture` reaches `record_exchanges`. So `--notify` is
            # the single gate for every phone notification a launch can send,
            # balance included.
            balance_observer = GraphQLBalanceObserver(
                transport.sibling(root=RUNPOD_GRAPHQL_ROOT, credential_placement="query"),
                now=now,
                notify=balance_notify,
            )
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

    def record_exchanges(self, recorder: FixtureRecorder) -> None:
        """Route every transport this adapter owns through the fixture recorder.

        `cli.py --record-fixture` calls this by duck type, so the CLI stays
        vendor-blind. The balance observer's transport is wrapped as well when
        it is one this adapter built, because the drill's evidence should show
        the balance answer beside the pod answers; an injected observer is an
        opaque callable and is left alone.
        """

        self.transport = RecordingTransport(self.transport, recorder)
        observer = self.balance_observer
        if isinstance(observer, GraphQLBalanceObserver):
            observer.transport = RecordingTransport(observer.transport, recorder)

    def set_balance_notify(
        self, notify: Callable[[Decimal, Decimal | None], notify_hooks.NotifyOutcome]
    ) -> None:
        """Wire the phone hook into the observer this adapter built, under ``--notify``.

        `cli.py` calls this by duck type, so that surface names no vendor -- the
        same shape `--record-fixture` uses for `record_exchanges`. Only the
        default `GraphQLBalanceObserver` this adapter constructed for a live
        transport can be wired: an injected observer is an opaque callable and
        is left alone, and a fake transport built no observer at all, so
        `--notify` can never conjure a balance ping where there is no balance
        source. Both of those refuse by name rather than silently doing
        nothing, because a caller that asked for balance pings and got none
        must be told which.
        """

        observer = self.balance_observer
        if observer is None:
            raise ValueError(
                "this provider has no balance source to notify from; nothing observes a "
                "balance, so no balance notification can be sent"
            )
        if not isinstance(observer, GraphQLBalanceObserver):
            raise ValueError(
                f"this provider's balance source is an injected {type(observer).__name__}; "
                "only the observer this adapter builds for a live transport can be wired"
            )
        observer.notify = notify

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

    def adopt(self, pod_id: str) -> PodRecord:
        response = self.transport.request("GET", f"/pods/{_path_id(pod_id)}{self._INCLUDE_QUERY}")
        if response.status == 404:
            raise ProviderFailure(
                f"RunPod cannot adopt pod {pod_id!r}: the provider reports it absent"
            )
        if response.status != 200:
            raise ProviderFailure(
                f"RunPod adopt returned HTTP {response.status}: {self._summary(response.body)}"
            )
        record = self._record(_object(response.body, "RunPod adopt"))
        if record.pod_id != pod_id:
            raise ProviderFailure("RunPod adopt response names a different pod id")
        if record.state != "RUNNING":
            raise ProviderFailure(
                f"RunPod cannot adopt pod {pod_id!r}: {self._STATE_FIELD} is {record.state!r}, "
                "not RUNNING"
            )
        return record

    def status(self, pod_id: str) -> ProviderStatus:
        """The exact-pod GET, reported verbatim: an observation, never a gate.

        An unfamiliar lifecycle word or an unparseable start instant is named in
        the detail rather than raised, because the shutdown path depends on this
        read; `_record` refuses unknown words only because it builds a record
        other code trusts. Only surrounding whitespace is stripped, since the
        word is compared downstream (`supervise.py`), never displayed.

        The start instant is surfaced apart from the state because v1's
        ``desiredStatus`` reads RUNNING from the moment create returns: only the
        start instant (null until the pod first runs) separates "still pulling
        the image" from "started and silent" for the armer's container wait
        (`controller_armer.ChannelControllerArmer`). A malformed one reads as
        absent, which every consumer already treats as "no start observed".
        """

        response = self.transport.request("GET", f"/pods/{_path_id(pod_id)}")
        observed = self.now()
        if response.status == 404:
            return ProviderStatus(
                pod_id, Presence.ABSENT, observed, "RunPod exact-pod GET returned 404", 404
            )
        if response.status != 200:
            raise ProviderFailure(
                f"RunPod status GET returned HTTP {response.status}: {self._summary(response.body)}"
            )
        row = _object(response.body, "RunPod status")
        if _text(row.get("id"), "RunPod status id") != pod_id:
            raise ProviderFailure("RunPod status response id does not equal the requested pod id")
        raw_state = row.get(self._STATE_FIELD)
        usable_state = isinstance(raw_state, str) and bool(raw_state.strip())
        provider_state = raw_state.strip() if isinstance(raw_state, str) and usable_state else None
        detail = "RunPod exact-pod GET returned 200"
        if raw_state is not None and not usable_state:
            detail = f"{detail}; unusable {self._STATE_FIELD} {raw_state!r}"
        raw_started = row.get(self._STARTED_FIELD)
        started_at: datetime | None = None
        if isinstance(raw_started, str) and raw_started.strip():
            try:
                started_at = _timestamp(raw_started, f"RunPod pod {pod_id} {self._STARTED_FIELD}")
            except ProviderFailure as error:
                detail = f"{detail}; unusable {self._STARTED_FIELD} ({error})"
        return ProviderStatus(
            pod_id,
            Presence.PRESENT,
            observed,
            detail,
            200,
            provider_state=provider_state,
            started_at=started_at,
        )

    def verify_absent(self, pod_id: str) -> AbsenceObservation:
        listed = any(row.get("id") == pod_id for row in self._pod_rows())
        return AbsenceObservation(
            pod_id,
            Presence.PRESENT if listed else Presence.ABSENT,
            self.now(),
            f"{self._POD_LIST_LABEL} still contains the exact pod id"
            if listed
            else f"{self._POD_LIST_LABEL} omits the exact pod id",
        )

    def _existing_launch(self, request: PodCreateRequest) -> PodRecord | None:
        """The pod this launch token already created, or ``None`` when a POST may follow.

        A POST whose response the client never saw may still have created a
        billing pod, so every create looks first; `recovery_only` stops there.
        """

        # Read through a local rather than assigning the call directly: the
        # repository's credential scanner reads `token = <20+ word characters>`
        # as a literal secret.
        metadata = request.metadata
        token = metadata.get(LAUNCH_TOKEN_ENV)
        if not isinstance(token, str) or not token:
            raise ProviderFailure(
                f"RunPod create requires a {LAUNCH_TOKEN_ENV} metadata value to stay recoverable"
            )
        existing = self._find_by_launch_token(request.name, token)
        if existing is None and request.recovery_only:
            raise ProviderFailure(
                "RunPod recovery lookup found no pod carrying this exact launch token; "
                "no create request was issued"
            )
        return existing

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

    def _pod_rows(self) -> list[dict[str, object]]:
        raise NotImplementedError

    def _record(self, payload: Mapping[str, object]) -> PodRecord:
        raise NotImplementedError


class RunPodProvider(_RunPodAdapter):
    """RunPod REST v1 implementation of the seven provider verbs.

    Kept beside `RunPodV2Provider` until the first live run under v2 is green,
    then deleted in its own commit (`V2_MIGRATION.md`). Selected by
    ``live_runpod_provider(..., route="v1")``.
    """

    ROOT = RUNPOD_REST_ROOT
    ROUTE = "v1"
    _INCLUDE_QUERY = "?includeMachine=true&includeNetworkVolume=true"
    _STATE_FIELD = "desiredStatus"
    _STARTED_FIELD = "lastStartedAt"

    def create(self, request: PodCreateRequest) -> PodRecord:
        """Correlate an existing launch token first, then POST — never both."""

        existing = self._existing_launch(request)
        if existing is not None:
            return existing
        response = self.transport.request("POST", "/pods", _create_payload(request, self.ROUTE))
        if response.status not in {200, 201}:
            raise ProviderFailure(
                f"RunPod create returned HTTP {response.status}: {_body_summary(response.body)}"
            )
        return self._record(_object(response.body, "RunPod create"))

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

    def capture_cost(self, pod_id: str, started_at: datetime, cutoff_at: datetime) -> CostCapture:
        """The provider's own billed amounts — never an estimate from elapsed time.

        Everything returned is bound to the exact pod and the requested window
        before it can total to a verified close; anything that cannot be bound
        is `UNAVAILABLE` with its reason, never zero.

        v1 returns a bare array with no metadata envelope, so this adapter can
        prove *attribution* and *containment* but not bucket contiguity — there
        is nothing in the response that says which window the endpoint actually
        resolved. `BillingState.PENDING_RECONCILIATION` is therefore never
        emitted here: v1 gives no signal distinguishing "not posted yet" from
        "nothing to post", and `shutdown.py`'s bounded reconciliation retry is
        what absorbs billing lag instead.
        """

        started, cutoff = _billing_window(started_at, cutoff_at)
        response = self.transport.request(
            "GET", _billing_path(pod_id, started, cutoff, grouping="podId")
        )
        if response.status != 200:
            raise ProviderFailure(
                f"RunPod pod billing returned HTTP {response.status}: {_body_summary(response.body)}"
            )
        rows = _array(response.body, "RunPod billing")

        def unavailable(reason: str) -> CostCapture:
            return _unavailable(pod_id, started, cutoff, reason)

        lines: list[CostLine] = []
        for row in rows:
            problem = _billing_row_problem(row, pod_id)
            if problem is not None:
                return unavailable(problem)
            try:
                bucket = _timestamp(row.get("time"), "billing record time")
                amount = as_decimal(row.get("amount"), "RunPod billing amount")
            except (ProviderFailure, ValueError) as error:
                return unavailable(f"RunPod billing record is structurally unverifiable: {error}")
            billed_ms = row.get("timeBilledMs")
            if not isinstance(billed_ms, int) or isinstance(billed_ms, bool) or billed_ms < 0:
                return unavailable("RunPod billing record has an invalid timeBilledMs")
            if _outside_requested_window(bucket, started, cutoff):
                return unavailable(_OUTSIDE_WINDOW)
            lines.append(
                CostLine(
                    amount,
                    f"RunPod pod billing bucket {_rfc3339(bucket)} ({billed_ms}ms billed)",
                    bucket,
                )
            )
        if not lines:
            return unavailable(
                "RunPod billing returned no records for a pod that ran; zero was not inferred"
            )
        return CostCapture(
            pod_id,
            BillingState.CAPTURED,
            cutoff,
            lines=tuple(lines),
            source=_V1_BILLING_SOURCE,
            window_start_at=started,
        )

    def _pod_rows(self) -> list[dict[str, object]]:
        # Recovery needs the same effective runtime facts as a create/adopt
        # response.  RunPod omits machine and network-volume objects from list
        # results unless they are requested explicitly; without these flags an
        # exact launch-token match cannot be bound back into a PodRecord.
        response = self.transport.request("GET", f"/pods{self._INCLUDE_QUERY}")
        if response.status != 200:
            raise ProviderFailure(
                f"RunPod pod-list GET returned HTTP {response.status}: {_body_summary(response.body)}"
            )
        return _pod_list_entries(_array(response.body, "RunPod pod-list"))

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
                # The two figures don't share one provenance: the pod rate is
                # this response's costPerHr, but v1 has no live volume-price
                # endpoint this adapter has found, so the volume rate is
                # still the injected estimate.
                "RunPod observed pod costPerHr; volume rate supplied at launch, not observed "
                "from the provider",
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
    _require_sealed_route(pod_id, payload, "v1")
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


REQUESTED_GPU_COUNT: Final = 1
"""The one GPU count this build ever requests.

`operations.pod.preflight.SystemGpuProbe.profile` reads it back as
`expected_gpu_count`, checking the on-pod measurement against the request
that provisioned the pod rather than leaving them independent.
"""


def _create_payload(request: PodCreateRequest, route: str = "v1") -> dict[str, object]:
    """The v1 `PodCreateInput` body. `interruptible` is always explicitly false.

    On-demand only: a spot reclaim mid-run is a silent-loss machine.
    `networkVolumeId` rides here because v1 attaches a volume only at creation
    and never afterwards. `containerDiskInGb` is sent because the bootstrap
    downloads its serving stack onto the container-local disk twice over
    (cache, then venv); leaving the size to the image or account default risks
    ENOSPC after the download is already paid for. Documented, not yet
    observed, like every other field here: the first live create confirms the
    provider accepts it. `PodCreateRequest.container_disk_gb` carries the
    number and the derivation.
    """

    payload: dict[str, object] = {
        "name": request.name,
        "cloudType": "SECURE",
        "computeType": "GPU",
        "imageName": request.image,
        "gpuTypeIds": [request.gpu_type],
        "gpuCount": REQUESTED_GPU_COUNT,
        "containerDiskInGb": request.container_disk_gb,
        "interruptible": False,
        "networkVolumeId": request.volume_id,
        "volumeMountPath": request.volume_mount_path,
        "dockerStartCmd": list(request.docker_start_cmd),
        "env": _pod_environment(request, route),
    }
    if request.template is not None:
        payload["templateId"] = request.template
    return payload


class RunPodV2Provider(_RunPodAdapter):
    """RunPod REST v2 implementation of the seven provider verbs.

    The route new launches use by default (``live_runpod_provider``). Every
    field, status code and lifecycle word here comes from the v2 pages the
    module docstring names; none has been observed from a live response.

    **A v2 create is refused until on-demand can be shown.** The v2 create
    body and pod object carry no rental-type field at all (module docstring),
    so this adapter cannot request `interruptible=false` or read it back.
    `create` therefore refuses before any POST while `V2_ON_DEMAND_BASIS` is
    ``None``, and every pod record it builds carries no runtime contract --
    which `launch.py` closes on a create and refuses on an adopt -- naming the
    reason. Correlation, status, terminate, absence and billing are unaffected,
    so a pod found or guarded through this route can always be closed.
    """

    ROOT = RUNPOD_V2_ROOT
    ROUTE = "v2"
    _STATE_FIELD = "status"
    _STARTED_FIELD = "startedAt"
    _POD_LIST_LABEL = "RunPod pod list (every page)"
    _summary = staticmethod(_problem_summary)

    def create(self, request: PodCreateRequest) -> PodRecord:
        """Correlate an existing launch token first, then POST — never both.

        The on-demand refusal comes after the lookup on purpose: a pod this
        launch already paid for is returned (without a runtime contract) so it
        can be bound and closed, and only a *new* POST is refused.
        """

        existing = self._existing_launch(request)
        if existing is not None:
            return existing
        if V2_ON_DEMAND_BASIS is None:
            raise ProviderFailure(V2_ON_DEMAND_REFUSAL + "; no create request was issued")
        response = self.transport.request("POST", "/pods", _v2_create_payload(request, self.ROUTE))
        if response.status == 201:
            return self._record(_object(response.body, "RunPod create"))
        # None of these is retried, here or by any caller: a create is never
        # re-issued, and each refusal says what the operator does instead.
        problem = _problem_summary(response.body)
        if response.status == 402:
            raise ProviderFailure(
                "RunPod refused create with HTTP 402, insufficient account balance -- the "
                "provider's own floor, beneath this runtime's. Nothing was created and the "
                f"request is not retried; add credit before launching again ({problem})"
            )
        if response.status == 400:
            raise ProviderFailure(
                "RunPod rejected a well-formed create with HTTP 400: a cross-field rule, or no "
                "placement for this GPU in the data centers allowed. The body was not malformed "
                f"(that is 422); not retried. Choose another card or retry later ({problem})"
            )
        if response.status == 422:
            raise ProviderFailure(
                "RunPod refused create with HTTP 422: the body does not match the v2 contract. "
                f"This adapter's create payload needs fixing; not retried ({problem})"
            )
        raise ProviderFailure(f"RunPod create returned HTTP {response.status}: {problem}")

    def terminate(self, pod_id: str) -> None:
        """Terminate, never stop: a stopped pod bills volume disk at double rate.

        ``204`` is the documented success and ``404`` an idempotent repeat of an
        earlier delete; neither is proof of absence, which only the later
        GET-404 plus list-absence pair establishes. ``409`` means the pod
        belongs to a cluster and the pod endpoint will never terminate it, so
        it is a named refusal with its remedy, never tolerated or retried.
        """

        response = self.transport.request("DELETE", f"/pods/{_path_id(pod_id)}")
        if response.status in {204, 404}:
            return
        if response.status == 409:
            raise TerminateRefused(
                f"RunPod refused to terminate pod {pod_id!r} with HTTP 409: it belongs to a "
                "cluster and cannot be terminated through the pod endpoint. It is still "
                "billing; terminate its cluster from the RunPod console now "
                f"({_body_summary(response.body)})"
            )
        # `_body_summary`, never `_problem_summary`: DELETE's body is never
        # parsed, so no answer to it can raise before the refusal is built.
        raise ProviderFailure(
            f"RunPod terminate returned HTTP {response.status}: {_body_summary(response.body)}"
        )

    def capture_cost(self, pod_id: str, started_at: datetime, cutoff_at: datetime) -> CostCapture:
        """The provider's own billed amounts for this pod, bound to a window it declares.

        v2 wraps the records in an envelope whose ``metadata.query`` is the
        window and granularity the provider says it resolved. When it is
        present it must name this pod, the ``hour`` bucket, and a window that
        covers the requested one (it may only be wider: the start snaps down
        and the end up to a bucket edge), and its start becomes the capture's
        declared window start -- the provider's statement, not this adapter's
        echo. Only then may an empty answer be ``PENDING_RECONCILIATION``:
        the provider resolved exactly the window asked for and has posted
        nothing in it yet, which `shutdown.py`'s bounded retry waits out.

        The field's documentation is not settled: the page describes it as
        "(routes without a filter)" while also marking it required and showing
        it in a ``podId``-filtered example. So its absence is not a refusal;
        the capture then falls back to the v1 standard (attribution and
        containment against the requested window) and never reports pending.
        A ``metadata.query`` that is present but carries no ``podId`` (the
        field is optional in ``PodBillingQuery``) is different: it does not
        fall back, it reports the cost unavailable, because a resolved window
        that does not name this pod cannot attribute the records to it.
        """

        started, cutoff = _billing_window(started_at, cutoff_at)
        response = self.transport.request("GET", _billing_path(pod_id, started, cutoff))
        if response.status != 200:
            raise ProviderFailure(
                f"RunPod pod billing returned HTTP {response.status}: "
                f"{_problem_summary(response.body)}"
            )
        envelope = _object(response.body, "RunPod billing")
        records = envelope.get("records")
        metadata = envelope.get("metadata")
        if not isinstance(records, list) or not isinstance(metadata, Mapping):
            raise ProviderFailure(
                "RunPod billing response is not the documented v2 envelope "
                "{records: [...], metadata: {...}}"
            )

        def unavailable(reason: str, window_start: datetime = started) -> CostCapture:
            return _unavailable(pod_id, window_start, cutoff, reason, source=_V2_BILLING_SOURCE)

        resolved = metadata.get("query")
        window_start = started
        window_end: datetime | None = None
        if resolved is not None:
            if not isinstance(resolved, Mapping):
                return unavailable("RunPod billing metadata.query is not an object")
            if resolved.get("podId") != pod_id:
                return unavailable(
                    "RunPod billing metadata.query does not name the requested podId filter; "
                    "cost attribution is unverifiable"
                )
            if resolved.get("bucketSize") != "hour":
                return unavailable(
                    "RunPod billing resolved a bucketSize other than the hour requested: "
                    f"{resolved.get('bucketSize')!r}"
                )
            try:
                resolved_start = _timestamp(resolved.get("startTime"), "billing resolved startTime")
                resolved_end = _timestamp(resolved.get("endTime"), "billing resolved endTime")
            except ProviderFailure as error:
                return unavailable(f"RunPod billing metadata.query is unreadable: {error}")
            if resolved_start > started or resolved_end < cutoff:
                return unavailable(
                    f"RunPod billing resolved the window {_rfc3339(resolved_start)} to "
                    f"{_rfc3339(resolved_end)}, narrower than the {_rfc3339(started)} to "
                    f"{_rfc3339(cutoff)} requested; the charge window is narrowed"
                )
            window_start, window_end = resolved_start, resolved_end
        record_count = metadata.get("recordCount")
        if record_count is not None and (
            not isinstance(record_count, int)
            or isinstance(record_count, bool)
            or record_count != len(records)
        ):
            return unavailable(
                f"RunPod billing metadata.recordCount {record_count!r} does not equal the "
                f"{len(records)} records returned",
                window_start,
            )
        lines: list[CostLine] = []
        for row in records:
            problem = _billing_row_problem(row, pod_id)
            if problem is not None:
                return unavailable(problem, window_start)
            try:
                bucket = _timestamp(row.get("startTime"), "billing record startTime")
                bucket_end = _timestamp(row.get("endTime"), "billing record endTime")
                amount = as_decimal(row.get("totalAmount"), "RunPod billing totalAmount")
            except (ProviderFailure, ValueError) as error:
                return unavailable(
                    f"RunPod billing record is structurally unverifiable: {error}", window_start
                )
            if bucket_end <= bucket:
                return unavailable(
                    "RunPod billing record ends at or before it starts", window_start
                )
            # A resolved window, when the provider declared one, bounds every record too.
            outside = _outside_requested_window(bucket, started, cutoff)
            if window_end is not None:
                outside = outside or bucket < window_start or bucket_end > window_end
            if outside:
                return unavailable(_OUTSIDE_WINDOW, window_start)
            lines.append(
                CostLine(
                    amount,
                    f"RunPod pod billing bucket {_rfc3339(bucket)} to {_rfc3339(bucket_end)}"
                    f"{_cost_breakdown(row)}",
                    bucket,
                )
            )
        if not lines:
            if window_end is not None:
                return CostCapture(
                    pod_id,
                    BillingState.PENDING_RECONCILIATION,
                    cutoff,
                    reason=(
                        "RunPod resolved the requested window for this exact pod and has posted "
                        "no records in it yet; zero was not inferred"
                    ),
                    source=_V2_BILLING_SOURCE,
                    window_start_at=window_start,
                )
            return unavailable(
                "RunPod billing returned no records for a pod that ran, and declared no resolved "
                "window that would tell not-yet-posted from nothing-to-post; zero was not inferred"
            )
        return CostCapture(
            pod_id,
            BillingState.CAPTURED,
            cutoff,
            lines=tuple(lines),
            source=_V2_BILLING_SOURCE
            + ("" if window_end is not None else " (no resolved window declared)"),
            window_start_at=window_start,
        )

    def cross_check_catalogue(self, reviewed: Mapping[str, Decimal]) -> tuple[str, ...]:
        """Compare the reviewed price sheet with RunPod's GPU catalogue; read-only, free.

        ``reviewed`` maps each `gpu_type_id` in `config/pod_placement.toml` to
        its reviewed ``hourly_usd``. Returns one finding per disagreement --
        an id the catalogue does not list, a card not offered on Secure cloud,
        or a Secure list price other than the reviewed one -- and an empty
        tuple when every row agrees. It changes nothing: the sheet stays the
        sealed authority, and a finding is for the project lead to review
        before the first paid create under v2 (the README's checklist).
        """

        response = self.transport.request("GET", "/catalog/gpus")
        if response.status != 200:
            raise ProviderFailure(
                f"RunPod GPU catalogue returned HTTP {response.status}: "
                f"{_problem_summary(response.body)}"
            )
        gpus = _object(response.body, "RunPod GPU catalogue").get("gpus")
        if not isinstance(gpus, list):
            raise ProviderFailure(
                "RunPod GPU catalogue response is not the documented {gpus: [...]} envelope"
            )
        listed: dict[str, Mapping[str, object]] = {}
        for index, entry in enumerate(gpus):
            if not isinstance(entry, Mapping):
                raise ProviderFailure(f"RunPod GPU catalogue entry {index} is not an object")
            listed[_text(entry.get("id"), f"RunPod GPU catalogue entry {index} id")] = entry
        findings: list[str] = []
        for gpu_id, reviewed_price in sorted(reviewed.items()):
            entry = listed.get(gpu_id)
            if entry is None:
                findings.append(f"{gpu_id!r} is not in the RunPod GPU catalogue")
                continue
            if entry.get("secure") is not True:
                findings.append(f"{gpu_id!r} is not offered on Secure cloud")
            price = entry.get("price")
            secure = price.get("secure") if isinstance(price, Mapping) else None
            try:
                listed_price = as_decimal(secure, f"{gpu_id} secure price")  # type: ignore[arg-type]
            except ValueError:
                findings.append(f"{gpu_id!r} has no readable Secure list price: {secure!r}")
                continue
            if listed_price != as_decimal(reviewed_price, f"{gpu_id} reviewed price"):
                findings.append(
                    f"{gpu_id!r} lists {listed_price} USD/h on Secure cloud; the reviewed sheet "
                    f"says {reviewed_price}"
                )
        return tuple(findings)

    def _pod_rows(self) -> list[dict[str, object]]:
        """Every page of the pod list, or a refusal: a partial list is not absence.

        A list cut short reads as a false absence at close, or as "no pod
        carries this launch token" before a second POST, so every page is
        followed, an inconsistent or repeated cursor refuses, and more pages
        than `_MAX_POD_LIST_PAGES` refuses rather than stopping early.
        """

        rows: list[dict[str, object]] = []
        cursor: str | None = None
        seen: set[str] = set()
        for _ in range(_MAX_POD_LIST_PAGES):
            # Cluster member pods are left out unless asked for. A pod this
            # runtime created is standalone, but a list that hides any pod in
            # the account is not one absence can be read from.
            query: dict[str, str] = {"includeClusterPods": "true"}
            if cursor is not None:
                query["cursor"] = cursor
            path = f"/pods?{urllib.parse.urlencode(query)}"
            response = self.transport.request("GET", path)
            if response.status != 200:
                raise ProviderFailure(
                    f"RunPod pod-list GET returned HTTP {response.status}: "
                    f"{_problem_summary(response.body)}"
                )
            page = _object(response.body, "RunPod pod-list")
            pods = page.get("pods")
            pagination = page.get("pagination")
            if not isinstance(pods, list):
                raise ProviderFailure(
                    "RunPod pod-list response is not the documented {pods: [...]} envelope"
                )
            if not isinstance(pagination, Mapping) or not isinstance(
                pagination.get("hasNextPage"), bool
            ):
                raise ProviderFailure(
                    "RunPod pod-list response carries no readable pagination.hasNextPage; this "
                    "page cannot be shown to be the last"
                )
            rows.extend(_pod_list_entries(pods))
            next_cursor = pagination.get("nextCursor")
            if not pagination["hasNextPage"]:
                if next_cursor is not None:
                    raise ProviderFailure(
                        "RunPod pod-list says there is no next page but still names a cursor"
                    )
                return rows
            if not isinstance(next_cursor, str) or not next_cursor or next_cursor in seen:
                raise ProviderFailure(
                    "RunPod pod-list says there is a next page but gives no new cursor to it"
                )
            seen.add(next_cursor)
            cursor = next_cursor
        raise ProviderFailure(
            f"RunPod pod list ran past {_MAX_POD_LIST_PAGES} pages; absence cannot be read "
            "from a list this adapter did not finish"
        )

    def _v2_rate(
        self, pod_id: str, state: str, payload: Mapping[str, object]
    ) -> tuple[Decimal, str, str | None]:
        """The hourly rate a record carries, its source, and a contract refusal if any.

        A RUNNING pod must report a positive ``cost``; anything else raises,
        as the v1 adapter does. Any other state never refuses an identified pod
        on its rate: v2 documents 0.0 for EXITED and TERMINATED and says
        nothing for PROVISIONING, STARTING or ERROR, and a record that raised
        here after a POST would leave a billing pod unbound. So a missing or
        non-positive rate there is replaced by the reviewed sheet's price for
        the reported GPU, with a source that says so; when even that cannot be
        resolved the rate is zero and the record carries a contract refusal,
        so the pod is bound and closed rather than launched on an unknown rate.
        """

        observed_source = (
            "RunPod observed pod cost; volume rate supplied at launch, not observed from the "
            "provider"
        )
        raw = payload.get("cost")
        if state == "RUNNING":
            hourly = as_decimal(raw, f"RunPod pod {pod_id} cost")  # type: ignore[arg-type]
            if hourly <= 0:
                raise ProviderFailure(
                    f"RunPod pod {pod_id} reports a non-positive cost while RUNNING"
                )
            return hourly, observed_source, None
        try:
            hourly = as_decimal(raw, f"RunPod pod {pod_id} cost")  # type: ignore[arg-type]
        except ValueError:
            hourly = Decimal("0")
        if hourly > 0:
            return hourly, observed_source, None
        gpu = payload.get("gpu")
        gpu_id = gpu.get("id") if isinstance(gpu, Mapping) else None
        try:
            if not isinstance(gpu_id, str) or not gpu_id:
                raise ValueError("the pod reports no gpu.id")
            reviewed = as_decimal(self.pod_price(gpu_id), "RunPod reviewed pod price")
        except Exception as error:  # noqa: BLE001 - any failure here is a named refusal below
            return (
                Decimal("0"),
                f"RunPod reported no pod rate while {state}, and no reviewed price resolved",
                f"RunPod pod {pod_id} reported no hourly rate while {state} and the reviewed "
                f"price sheet could not supply one ({error}); its cost cannot be bounded",
            )
        return (
            reviewed,
            f"RunPod reported no pod rate while {state}; the reviewed price sheet's rate for "
            f"{gpu_id} stands in; volume rate supplied at launch",
            None,
        )

    def _record(self, payload: Mapping[str, object]) -> PodRecord:
        """Identity, lifecycle, rate, volume and creation instant; then the contract.

        The first five must parse or this raises: without them no lease can
        be bound. The runtime contract is different -- a pod whose effective
        shape cannot be proven is still returned, with no contract and the
        reason, so `launch.py` binds it and closes it rather than leaving a
        pod the provider did create unbound and billing.
        """

        pod_id = _text(payload.get("id"), "RunPod pod id")
        state = payload.get("status")
        if state not in _V2_POD_STATES:
            raise ProviderFailure(f"RunPod pod {pod_id} reports an unrecognised status: {state!r}")
        volume_id, mount_path = _v2_network_mount(pod_id, payload)
        hourly, rate_source, rate_refusal = self._v2_rate(pod_id, str(state), payload)
        created_at = _timestamp(payload.get("createdAt"), f"RunPod pod {pod_id} createdAt")
        contract: PodRuntimeContract | None = None
        refusal: str | None = rate_refusal
        if refusal is None:
            try:
                contract = _v2_runtime_contract(pod_id, payload, volume_id, mount_path)
            except ProviderFailure as error:
                refusal = str(error)
        return PodRecord(
            pod_id=pod_id,
            name=_text(payload.get("name"), f"RunPod pod {pod_id} name"),
            estimate=PodEstimate(
                hourly,
                as_decimal(self.volume_price(volume_id), "RunPod volume price"),
                rate_source,
                self.now(),
            ),
            volume_id=volume_id,
            # v2's own creation instant, so the close window starts where the
            # provider's charges can (04-7). No fallback: a pod without one
            # cannot anchor a billing window, and an observation instant
            # would narrow it.
            created_at=created_at,
            state=str(state),
            runtime_contract=contract,
            contract_refusal=refusal,
        )


def _v2_network_mount(pod_id: str, payload: Mapping[str, object]) -> tuple[str, str]:
    mounts = payload.get("mounts")
    network = mounts.get("network") if isinstance(mounts, Mapping) else None
    if not isinstance(network, list) or len(network) != 1 or not isinstance(network[0], Mapping):
        raise ProviderFailure(
            f"RunPod pod {pod_id} reports no single attached network volume in mounts.network; "
            "volumes attach only at creation"
        )
    return (
        _text(network[0].get("volumeId"), f"RunPod pod {pod_id} mounts.network[0].volumeId"),
        _text(network[0].get("path"), f"RunPod pod {pod_id} mounts.network[0].path"),
    )


def _v2_runtime_contract(
    pod_id: str, payload: Mapping[str, object], volume_id: str, mount_path: str
) -> PodRuntimeContract:
    """The effective shape a v2 pod reports, refused by name on every doubt.

    Checked beyond what `PodRuntimeContract` carries: ``cloud`` must be the
    ``SECURE`` this adapter requests, and ``gpu.count`` the one GPU. The start
    command is read back from ``args``, the only form the pod object is
    documented to carry, and must be the exec-form object this adapter sends.
    The rental type comes last, because no field can show it.
    """

    if payload.get("cloud") != "SECURE":
        raise ProviderFailure(
            f"RunPod pod {pod_id} reports cloud {payload.get('cloud')!r}, not the SECURE requested"
        )
    gpu = payload.get("gpu")
    if not isinstance(gpu, Mapping):
        raise ProviderFailure(f"RunPod pod {pod_id} reports no gpu object to verify against")
    gpu_type = _text(gpu.get("id"), f"RunPod pod {pod_id} gpu.id")
    if gpu.get("count") != REQUESTED_GPU_COUNT:
        raise ProviderFailure(
            f"RunPod pod {pod_id} reports gpu.count {gpu.get('count')!r}, not the "
            f"{REQUESTED_GPU_COUNT} requested"
        )
    command = _v2_start_argv(pod_id, payload)
    margin = _billing_cutoff_margin_from_environment(pod_id, payload)
    _require_sealed_route(pod_id, payload, "v2")
    image = _text(payload.get("image"), f"RunPod pod {pod_id} image")
    template = payload.get("template")
    if V2_ON_DEMAND_BASIS is None:
        raise ProviderFailure(f"RunPod pod {pod_id}: {V2_ON_DEMAND_REFUSAL}")
    return PodRuntimeContract(
        interruptible=False,
        gpu_type=gpu_type,
        image=image,
        volume_id=volume_id,
        volume_mount_path=mount_path,
        docker_start_cmd=command,
        billing_cutoff_margin_seconds=margin,
        template=template if isinstance(template, str) and template else None,
    )


def _v2_start_argv(pod_id: str, payload: Mapping[str, object]) -> tuple[str, ...]:
    """The argv a v2 pod will run, only when ``args`` states it exactly.

    ``args`` is "The container's command, as a single raw string", returned
    "exactly as stored", and accepts two shapes: a bare shell string the
    provider splits into CMD (by rules the page does not give), or a JSON
    object ``{"entrypoint": [...], "cmd": [...]}``. This adapter sends only
    the second, so the argv is exact and the image's own ENTRYPOINT cannot
    wrap the pod timer; anything else read back refuses. The page also says
    responses carry the deconstructed ``entrypoint`` and ``cmd``; the pod
    schema has them only as optional inherited properties, in no ``required``
    list and no example, so they are checked when present and not required.
    """

    raw = payload.get("args")
    if not isinstance(raw, str) or not raw.strip():
        raise ProviderFailure(f"RunPod pod {pod_id} reports no args to verify its start command")
    try:
        stored = json.loads(raw)
    except json.JSONDecodeError:
        stored = None
    if not isinstance(stored, dict) or set(stored) != {"entrypoint", "cmd"}:
        raise ProviderFailure(
            f"RunPod pod {pod_id} reports args that are not the exec-form "
            '{"entrypoint": [...], "cmd": [...]} object this adapter sends; the pod timer '
            "cannot be shown to be its primary process"
        )
    parts: dict[str, list[str]] = {}
    for key in ("entrypoint", "cmd"):
        value = stored[key]
        if not isinstance(value, list) or not all(isinstance(part, str) and part for part in value):
            raise ProviderFailure(
                f"RunPod pod {pod_id} args.{key} is not a list of non-blank strings"
            )
        echoed = payload.get(key)
        if echoed is not None and echoed != value:
            raise ProviderFailure(
                f"RunPod pod {pod_id} reports a {key} that disagrees with its args"
            )
        parts[key] = value
    if not parts["entrypoint"]:
        raise ProviderFailure(f"RunPod pod {pod_id} args carries an empty entrypoint")
    return tuple(parts["entrypoint"] + parts["cmd"])


def _v2_start_args(command: tuple[str, ...]) -> str:
    """The exec-form ``args`` string for ``command``: interpreter as ENTRYPOINT, rest as CMD.

    Both halves are stated so neither the image's ENTRYPOINT nor its CMD can
    add to the argv `models._assert_pod_timer_is_primary_process` checked.
    """

    return json.dumps({"entrypoint": [command[0]], "cmd": list(command[1:])}, separators=(",", ":"))


def _v2_create_payload(request: PodCreateRequest, route: str = "v2") -> dict[str, object]:
    """The v2 `CreatePodRequest` body: nested where v1 was flat.

    No ``interruptible`` is sent because v2 has no such field; that is why
    `RunPodV2Provider.create` refuses while `V2_ON_DEMAND_BASIS` is unset.
    ``startJupyter`` and ``startSsh`` are sent false explicitly because a
    template's own defaults may enable both and body fields override the
    template; this pod has no ports or SSH.
    """

    payload: dict[str, object] = {
        "name": request.name,
        "cloud": "SECURE",
        "image": request.image,
        "gpu": {"id": request.gpu_type, "count": REQUESTED_GPU_COUNT},
        "disk": request.container_disk_gb,
        "mounts": {"network": [{"volumeId": request.volume_id, "path": request.volume_mount_path}]},
        "args": _v2_start_args(request.docker_start_cmd),
        "env": _pod_environment(request, route),
        "startJupyter": False,
        "startSsh": False,
    }
    if request.template is not None:
        payload["templateId"] = request.template
    return payload


def _cost_breakdown(row: Mapping[str, object]) -> str:
    """The per-component split v2 reports, as text on the line; never totalled."""

    parts = [
        f"{name}={row[name]}"
        for name in ("gpuAmount", "cpuAmount", "diskAmount")
        if isinstance(row.get(name), (int, Decimal)) and not isinstance(row.get(name), bool)
    ]
    return f" ({', '.join(parts)})" if parts else ""


def live_runpod_provider(
    capability: str,
    *,
    pod_price: Callable[[str], Decimal],
    volume_price: Callable[[str], Decimal],
    route: str = RUNPOD_DEFAULT_ROUTE,
    timeout_seconds: float = 30.0,
    **options: object,
) -> "RunPodProvider | RunPodV2Provider":
    """The adapter for ``route`` over a live transport at that route's root.

    The one place the route is chosen: ``"v2"`` by default, ``"v1"`` while v1
    is still kept (until the first live v2 run is green). An untracked
    ``--provider-factory`` calls this rather than pairing a class and a root
    by hand; ``options`` are passed to the adapter unchanged.
    """

    if route == "v1":
        adapter: type[RunPodProvider] | type[RunPodV2Provider] = RunPodProvider
    elif route == "v2":
        adapter = RunPodV2Provider
    else:
        raise ValueError(f"RunPod route must be one of {RUNPOD_ROUTES}, not {route!r}")
    return adapter(
        UrllibRunPodTransport(capability, timeout_seconds=timeout_seconds, root=adapter.ROOT),
        pod_price=pod_price,
        volume_price=volume_price,
        **options,  # type: ignore[arg-type]
    )


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
    # Required, never defaulted: the route is sealed into the pod's env by the
    # adapter that created it (`_pod_environment`).
    route = _required_environment(env, RUNPOD_ROUTE_ENV)
    if route not in RUNPOD_ROUTES:
        raise RuntimeError(
            f"RunPod pod timer is not armed: {RUNPOD_ROUTE_ENV} must be one of {RUNPOD_ROUTES}"
        )
    provider = live_runpod_provider(
        capability,
        # The timer never estimates or creates. Sealed launch-time rates are
        # only retained for the close report's ongoing-volume disclosure.
        pod_price=lambda gpu: pod_rate,
        volume_price=lambda volume: volume_rate,
        route=route,
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


def _require_sealed_route(pod_id: str, payload: Mapping[str, object], route: str) -> None:
    environment = payload.get("env")
    if not isinstance(environment, Mapping) or environment.get(RUNPOD_ROUTE_ENV) != route:
        raise ProviderFailure(
            f"RunPod pod {pod_id} env does not seal {RUNPOD_ROUTE_ENV}={route}; its pod-side "
            "timer could not close it through the route that created it"
        )


def _pod_list_entries(rows: list[object]) -> list[dict[str, object]]:
    for index, row in enumerate(rows):
        if not isinstance(row, dict):
            raise ProviderFailure(f"RunPod pod-list entry {index} is not an object")
        _text(row.get("id"), f"RunPod pod-list entry {index} id")
    return rows  # type: ignore[return-value]


def _billing_window(started_at: datetime, cutoff_at: datetime) -> tuple[datetime, datetime]:
    started = require_utc(started_at, "billing start")
    cutoff = require_utc(cutoff_at, "billing cutoff")
    if started >= cutoff:
        raise ProviderFailure("billing window start must precede its cutoff")
    return started, cutoff


def _billing_path(pod_id: str, started: datetime, cutoff: datetime, **extra: str) -> str:
    query = {
        "podId": pod_id,
        "startTime": _rfc3339(started),
        "endTime": _rfc3339(cutoff),
        "bucketSize": "hour",
        **extra,
    }
    return f"/billing/pods?{urllib.parse.urlencode(query)}"


def _billing_row_problem(row: object, pod_id: str) -> str | None:
    if not isinstance(row, dict):
        return "RunPod billing returned a non-object record"
    row_pod = row.get("podId")
    if not isinstance(row_pod, str) or row_pod != pod_id:
        return (
            "RunPod billing returned a record that does not name the requested pod; "
            "cost attribution is unverifiable"
        )
    return None


_OUTSIDE_WINDOW: Final = (
    "RunPod billing record lies outside the requested window by more than one "
    "bucket; cost attribution is unverifiable"
)


def _outside_requested_window(bucket: datetime, started: datetime, cutoff: datetime) -> bool:
    """A record's timestamp is its bucket start, so the hour containing the pod's
    creation may begin up to one bucket before the requested window; anything
    earlier, or after the cutoff, came from a window this call did not ask for."""

    return bucket < started - _BUCKET_WIDTH or bucket > cutoff


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


def _unavailable(
    pod_id: str,
    window_start: datetime,
    cutoff: datetime,
    reason: str,
    *,
    source: str = _V1_BILLING_SOURCE,
) -> CostCapture:
    return CostCapture(
        pod_id,
        BillingState.UNAVAILABLE,
        cutoff,
        reason=reason,
        source=source,
        window_start_at=window_start,
    )


def _bounded_read(
    stream: http.client.HTTPResponse | urllib.error.HTTPError, deadline: float | None = None
) -> bytes:
    """Refuse to buffer a response past ``_MAX_RESPONSE_BYTES``, never truncate it silently.

    ``HTTPResponse.read(amt)`` is documented as returning *up to* ``amt`` bytes,
    so one call may return a short read before EOF; a valid billing response
    under the cap would then reach ``_json`` truncated and be refused as
    malformed.  CPython's own implementation happens not to short-read here
    today -- this accumulates against the documented contract rather than
    against that implementation detail.

    ``deadline`` is the caller's whole-call monotonic deadline, checked between
    reads.  It is a refinement and not the bound: ``read`` blocks until it has
    the amount asked for, so a responder dribbling inside the socket timeout
    never returns control to this loop at all.  What actually bounds that case
    is the worker thread the caller joins with its budget
    (``operations/http_deadline.py``); this check exists so a response arriving
    in several complete-but-slow reads is refused here, by name, instead of
    becoming a cancelled thread.  ``None`` keeps the unbounded behaviour for the
    direct-call tests that hand this function a synthetic stream.
    """

    parts: list[bytes] = []
    total = 0
    while total <= _MAX_RESPONSE_BYTES:
        if deadline is not None and parts and time.monotonic() >= deadline:
            raise ProviderFailure(
                "RunPod response did not complete within the request's whole-call deadline"
            )
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
