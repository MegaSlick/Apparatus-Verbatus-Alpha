# Paid infrastructure — pods, GPUs, and anything that bills

**Read this before invoking anything that can start a meter**: RunPod, any GPU host, any
hosted inference, any storage or egress that is charged. If your task appears to need one
of these, this file is the whole rule; `AGENTS.md` ("Who decides") is why.

## The rule

**Unless the project lead has directed it in the current session, you do not invoke a
billing action.** Not a launch, a resume, a resize, a persistent volume, or a "just to
check" call that provisions anything.

Permission covers **one exact action**, named with its cost. It is never inferred from
another permission and never carried forward from an earlier session: permission to run a
pod on Tuesday is not permission to run one on Wednesday, and permission to launch is not
permission to resize.

**Reading costs nothing and is always allowed**: listing pods, reading status, checking
whether something is running, reading billing. "Is anything running right now?" is worth
asking unprompted.

## Why this is stricter than every other rule here

Everything else here costs *work* when it goes wrong. **A pod bills by the hour for as long
as it exists**, awake or idle, watched or not, and a session that starts one and then dies
leaves it running until a human notices. That is the one failure that costs money rather
than time.

## Shutdown is verified, never inferred

**An acknowledgement is not a shutdown.** Nor is a command that returned zero, nor a log
line saying "terminated".

A close is green only when **provider state says the same pod is gone** (exact-pod GET-404
*and* absence from the pod list) and the provider returns non-empty billing records for
that exact pod, in a named window from the pod's anchor instant through the cutoff close
requested. An empty, unreachable, misattributed, narrowed or stale billing response is
*unverified*, never zero. Billing can lag, so this tool never claims "no future charges".
A shutdown that cannot be verified is not a tidy-up for next session: say so now.

Two known gaps in that proof wait on the first live run:

- **The anchor (04-7).** Under REST v2 `PodRecord.created_at` is the pod's own `createdAt`,
  so the close window starts at creation, not at first container start. Under v1 it is still
  `lastStartedAt` (or the observation instant when that is null), which can omit charges
  between creation and first start. What no offline test shows is that RunPod bills nothing
  before `createdAt`.
- **Coverage (04-9).** The verifier refuses records outside the declared window. Under v2 the
  declared window is the provider's own resolved `metadata.query` when it is present, not the
  runtime's echo; under v1 it is still the echo. Neither proves the returned buckets *fill*
  the window.

## What exists here

The runtime is fake-first. **It has never started, inspected or billed a live pod**: every
adapter test uses an in-memory transport (the redirect-refusal test uses two loopback
servers). The RunPod field names come from RunPod's documentation, cited page by page in
`provider_runpod.py`, not from observed responses. Before the first live run, work through
the [first gated live-pod checklist](#first-gated-live-pod-checklist).

### Provider seam and RunPod adapter

`provider.py` is the seven-verb provider seam; `provider_runpod.py` holds the only RunPod
adapters, one per REST route, behind that seam and one `HttpTransport`.

- **`RunPodV2Provider`** (`api.runpod.io/v2`) is the default. RunPod retires v1 on
  2026-11-15. `V2_MIGRATION.md` maps every v1 endpoint, field, status code and lifecycle word
  to v2, and says what is done and what waits on a live run.
- **`RunPodProvider`** (`rest.runpod.io/v1`) stays selectable until the first live run under
  v2 is green, then is deleted in its own commit.
- **Choosing a route.** An untracked `--provider-factory` calls
  `provider_runpod.live_runpod_provider(key, pod_price=..., volume_price=..., route=...)`;
  `route` defaults to `"v2"` and `"v1"` selects the old adapter. Each adapter refuses a live
  transport pointed at the other route's root.
- **The pod-side timer uses the route that created its pod.** Each adapter's create adds
  `VERBATUS_RUNPOD_ROUTE` (its own route) to the pod's `env`, refusing a request that names
  another, and `timer_context_from_environment` requires it: a timer that guessed a route
  could be the one controller unable to terminate its pod at the hard deadline.

**A v2 create is refused today, by name, before any POST.** v2 has no `interruptible` field
on create and no rental-type field on the pod, and no RunPod page read on 2026-09-24 says
what a v2 create produces, while v1 still documents spot pods. The runtime needs
`interruptible=false` proven before it spends, so until the project lead records a basis in
`provider_runpod.V2_ON_DEMAND_BASIS`, a paid create goes through v1. Every other v2 verb
works, and a v2 pod record without that proof carries no runtime contract, so `launch.py`
closes it on create and refuses it on adopt, naming the reason.

**The start command under v2** is sent as `args` in the documented exec-form JSON object,
`{"entrypoint": [interpreter], "cmd": [rest of argv]}`, so the argv is exact and the image's
own ENTRYPOINT cannot wrap the timer. The contract check reads `args` back and refuses
anything else, including the shell-string form the provider splits by undocumented rules.

**Other v2 behaviour:** a create answers `PROVISIONING` or `STARTING` before the pod runs
(`models.PRE_RUNNING_STATES`), which arming's bounded waits absorb; `ERROR` closes at once.
`402`, `400` and `422` on create are named refusals, never retried; `400` is a placement or
cross-field refusal, not a malformed body. `409` on terminate (a pod that belongs to a
cluster) is a `TerminateRefused`: `VerifiedShutdown.close` stops on it at once and reports
`failed-shutdown` with the console remedy instead of re-sending the DELETE for its whole
window, and the pod timer does not re-enter a close the provider refused. The pod list is asked
for cluster member pods too (`includeClusterPods=true`), paged and followed to its last
page, and a list that cannot be shown complete refuses. DELETE's body is never parsed.

**Account balance.** `GraphQLBalanceObserver` POSTs the one documented query,
`myself { clientBalance currentSpendPerHr }`, through the same redirect-refusing,
size-bounded transport, with the key as the `api_key` query parameter (the only form the
GraphQL docs publish); every error string and fixture record scrubs it. It is the default
over a live `UrllibRunPodTransport`; a fake transport gets none, so the "balance source was
not configured" refusal stays reachable offline. It refuses by name any malformed,
redirected, erroring, non-numeric, negative or credential-bearing answer.
**Currency is documented, not observed**: the schema names none, the billing pages say USD,
the v1 pod page says "Runpod credits per hour". The GraphQL route is documented as retiring
in early 2027, and no v2 billing endpoint reports a balance (`V2_MIGRATION.md` §1).

`fake_provider.py` has a fixed local price sheet, exact-token crash recovery and injected
failures only.

### Shutdown, leases and the two controllers

`shutdown.py`'s only green result needs a termination request, exact-pod GET-404,
independent pod-list absence, and a non-empty billing capture that *declares* a window from
creation through the requested cutoff with every dated record inside it (one hour of slack
before the start, for the bucket containing creation). The capture is retried 3 times, 15 s
apart, because billing lags termination; an adapter that can tell "not posted yet" from
"nothing to post" reports `pending-reconciliation`.

`lease.py`, `controllers.py`, `arming.py` and `pod_timer.py` implement a lease written
before create, restart recovery by exact launch token, the laptop heartbeat/lifetime
supervisor, the mandatory bootstrap, and an independent pod-side hard-lifetime dead-man. A
launch or adoption is green only when the laptop supervisor has started and a pod-timer
acknowledgement is durably bound to the exact lease, pod and hard deadline.

- **Fail closed.** A launch that cannot arm both controllers closes its own pod at once. An
  active lease with no receipt is closed by the supervisor once its launch owner stops
  heartbeating; while the owner still heartbeats it is only reported non-green, since that
  supervisor is usually the one the launch just started.
- **Closing a pod never touches the volume.** Keeping or deleting it needs separate
  authorization; the close report states the volume's ongoing price. There is no
  volume-delete operation.

### `supervise.py`: the laptop supervisor

`python -m operations.pod.supervise` runs `controllers.LaptopSupervisor` for one lease.

- **Ownership is a kernel lock, not a pid.** A non-blocking `fcntl.flock` on
  `supervisors/supervisor-<lease>.lock` decides who supervises; the kernel releases it the
  instant its holder dies. A recorded pid could be reused after a reboot and block
  supervision of a pod still billing. The identity file `supervisors/supervisor-<lease>.json`
  carries the owner token a restart resumes once it holds the lock.
- **Every tick re-reads `provider.status()`** and closes on any lifecycle word other than
  `RUNNING`, naming it. The exception is `PROVISIONING` or `STARTING` on a lease that is still
  unarmed while its launch owner heartbeats: that tick reports `provider-starting` and
  waits, bounded by the launch's own arming waits and its heartbeat. On an armed lease the
  same word is a restarted container and closes. A provider that cannot answer is neither
  `RUNNING` nor a reason to close; the heartbeat rule still holds and the loop keeps
  ticking.
- **The operator's `status` shows a supervisor block per open lease**: running, absent or
  unknown (it peeks the lock without creating it; only `BlockingIOError` counts as a
  holder), identity-file age, last tick, last close record and the volume's hourly price.
- **Exit status** follows `cli.py` (0 guarded, 2 nothing touched, 3 go and look); a lease
  this run confirmed active that goes missing or unreadable exits 3. `main()` catches
  `BaseException`, so even a bad `--provider-factory` or malformed `spend.toml` attempts a
  durable final record.

**Starting `supervise.py` means a long-lived process while a pod bills.** Treat it as
unattended long-running work and arrange to learn promptly if it dies.

### `controller_armer.py`: arming both controllers

`ChannelControllerArmer.arm`:

1. **Starts the laptop supervisor first**, detached, and records the start before polling,
   so a launcher that dies mid-poll leaves a supervisor that closes the unarmed lease once
   the launcher's heartbeat goes stale. Polling first would leave the pod unguarded.
2. **Hands over this launch's owner token** by writing the identity file itself (0600,
   exclusive) before starting the process. A supervisor that minted its own token would
   not own the lease and could only claim it as an orphan, closing the pod it was started
   to guard. The same token already on file (a retry) proceeds; a foreign or unreadable one
   refuses with `SUPERVISOR_FAILED`. The token never appears in argv (`ps` is public) or a
   receipt.
3. **Heartbeats the lease on every poll wait**, or the supervisor would close the pod at the
   ordinary heartbeat timeout. A failed heartbeat stops the poll and refuses.

It refuses before starting anything when `poll_seconds * 3 >= laptop_heartbeat_timeout_seconds`;
when the supervisor command or its `--spend` file is missing; or when request, pod record
and lease disagree on pod id or hard deadline. A pod clock slightly ahead of the laptop's is
waited out within a small skew bound; beyond it the launch refuses.

**The channel is designed, not observed (04-2).** The pod writes its report to the mounted
volume; the laptop reads it through RunPod's S3-compatible view (`GetObject` beside the
`HeadObject` in `operations/operator/volume_s3.py`). `TimerReportChannel.read` returns
`None` **only** when the object is proven absent; a refused credential or unclassifiable
answer raises, so an unreachable channel never reads as "not yet".

**`ObservingControllerArmer`** runs the identical procedure with `pod_timer_acknowledged`
hard-coded `False`, so it never arms and the launch closes its pod at once. It records what
it saw and how long it waited in a local evidence file. Boot A uses it.

### `bootstrap_main.py`: bootstrap and hold

`python -m operations.pod.bootstrap_main` runs the bootstrap and then **holds** to
`VERBATUS_HARD_DEADLINE`, re-journaling a liveness line each interval.
`pod_timer.run_with_bootstrap` treats any child exit before the deadline, exit 0 included,
as `completed-early` and closes the pod. A red step exits non-zero at once, which is the
correct immediate close.

- **Chair cache.** `ChairCacheBootstrapAction` copies each role's pinned bytes from a
  verified retained store and verifies them before publication. Only model-store
  materialization fetches from Hugging Face. The at-most-one same-pin re-fetch is not wired
  (`refetch_same_pin=None`, 04-8) because `ChairRegistry` has no cache-clear verb; a
  mismatch is red and names the chair.
- **Transfer is optional.** No submission manifest on the volume is a vacuous success; a
  manifest with no configured target is a refusal.
- **`PREFLIGHT`** runs `ChairRegistry.ensure` over the roster, then a smoke read through the
  serving package's production seam (`assemble_serving_smoke_reader` around
  `ServingManager`, fed `operations/serving/smoke.py::VisionSmokeCall`). The witness value
  is drawn from the CSPRNG on the pod and rendered onto a golden page under
  `<volume>/preflight/<report stem>/` just before the read, so it was never in a file or
  prompt. Serving receipts, launch audits and evidence manifests land content-addressed in
  the same directory. `--fixture` with `--page-witness-file` supplies a page instead.
  Ordinary serving refuses an unproven row; only this smoke assembly may launch one, for
  qualification, and its audit says so. After a green real-silicon report,
  `python -m operations.serving.qualify` renders review candidates for the measured tier;
  it never edits the catalogue.
- **Configuration is one selection.** `--serving-recipes-config` defaults to the
  fixture-only `config/serving_recipes.toml`. A real launch names `config/models-real.toml`,
  `config/serving_recipes_real.toml` and `config/witness_context-real.toml` together. The
  journaled `CONFIGURATION` step, after checkout and before anything is synced, fetched or
  served, matches each role's shipped witness declaration to its source and binds the four
  config paths and digests into its receipt. A resume with a changed selection fails there
  (restore it or start a new journal). A custom roster needs an operator-authored
  declaration.
- **Refusals come before any action**: a journal or report path outside the mounted volume;
  a lockfile that is not the checkout's `uv.lock`; a volume that fails a real write-and-read
  probe (it never creates the mount point it requires); a missing hard deadline; a
  credential-looking argv value. The environment is scrubbed by the shared credential-shaped
  predicate, except an explicit `--keep-env` allowlist.
- **`--dry-run`** validates and prints the plan without running; it does not mean "against
  fakes", because a fake-actions flag in a production entrypoint is a green journal waiting
  to happen. **`--hold-only`** is a drill: no bootstrap steps, a `hold-only` journal record,
  hold to the deadline; it refuses any plan argument.

The journal is written to the volume under the launch-bound name; nothing on the laptop
reads it yet. `test_bootstrap_main.py` runs only fakes: no git, uv, Hugging Face or GPU
probe.

### `pod_run.py`: running the pipeline on a pod

```sh
python -m operations.pod.pod_run <run flags> -- <bootstrap_main argv>
```

The argv after `--` goes through `bootstrap_main`'s own `prepare`/`run_bootstrap`, so every
bootstrap refusal, probe, scrub and deadline applies. After a green journal it runs
`pipeline/orchestrator/run.py` with the pod's interpreter: run root `<volume>/runs` (or
`--run-root`, inside the volume), submission inside the volume, the config trio the
bootstrap checked and measured, and `--data-gate-policy` inside the repository. Its
`pod-run-report.v1` at the launch-bound `--report-path` moves through `bootstrapping`,
`running`, then `complete`, `held`, `halted`, `failed`, `bootstrap-red` or `refused`.

| Exit | Meaning |
|---|---|
| 0 | the orchestrator returned `EXIT_COMPLETE` — never for a partial run |
| 2 | a named refusal before anything ran |
| 3 / 4 | held / halted (the orchestrator's own) |
| 5 | a red bootstrap step |
| 6 | the orchestrator could not start or exited outside its vocabulary |
| 7 | `--dry-run`: plan validated and printed, nothing ran |

It refuses by name: no `--`; a `--hold-only` plan; a report path that is the bootstrap's or
lacks the launch token; a run root or submission outside the volume or missing; a policy
outside the repository; a bad run id.

**The data gate is asked first**, before a model is fetched on a billing card.
`config/data_handling_policy.json` lists the pod volume's mount path (the
`volume_mount_path` `boot_a_request.py` seals) beside the local `private/` root. That
listing is the project lead's standing disclosure decision: a rented pod's volume is
accepted exposure for the duration of a run, everything a pod produces (Perlector training
inputs included) stays there until exported, and `verbatus fetch-run` is the way home. A
submission outside every listed root is refused. Almost no machine has both roots, so every
run report records which resolved and which did not (`approved_storage_roots`,
`skipped_storage_roots`).

**There is no `--placement-tier` flag.** No stage reads a tier; `pod_run` records the one
the green `PREFLIGHT` receipt measured and refuses a receipt with none.

**It holds only for a finished run.** After `complete` or `held` it holds to the hard
deadline (paid idle time), because the pod timer treats an early exit as non-green. After
`halted`, `failed` or a failed start it returns at once and lets the timer close the pod:
holding a card for a run that will produce nothing more is paying for nothing. Everything
stays on the volume. `held_to_hard_deadline` in the report says which way it went.

**Its own records** sit beside the report, named from its stem:

- `-transcript.log` — the orchestrator's and every stage's merged stdout and stderr (the
  container log still gets them). 8 MB of head written live, then the final 1 MB after a
  named truncation marker at exit.
- `-liveness.json` — pid, tick and last seen while the orchestrator lives. `alive: true`
  stamped long before the deadline means the supervisor stopped while the child ran (an OOM
  kill or teardown).
- `-timings.json` — one entry per stage invocation (member, operation, act, start, finish,
  duration, exit code, commit). It is outside the run tree because the tree is pinned
  byte-identical across reruns and restores and a clock is not. `run.json` names only the
  commit that created the run; a resume at another commit shows here. (Binding the commit
  into the run authority would refuse every resume after a fix.)
- `-hold.json` — the hold line after a finished run.

These are best-effort, so a lost stopwatch never abandons a running orchestrator. The
report audits them at close (`records_at_close`, `records_missing`); a completed run missing
any is reported `held` (exit 3), since the timings are what the first live run measures.

### `notify_hooks.py`: phone notifications

One short line through `operations/notify/notify.sh` at launch (lease, card, hourly
ceiling), at close (lease, verified state, `billed Ns from creation` — no stop time is
observed), and at each balance observation (taken only at the create and adopt gates). A
message with a credential shape or URL is
never sent. A failed ping never changes a launch or close decision. `--notify` gates every
notification, balance included; the balance hook is installed through the provider's
duck-typed `set_balance_notify` or `RunPodProvider(balance_notify=...)`, both off by
default, so a pod never pages a phone on its own. A provider without the seam is recorded
in the launch record's `balance_notification`, not refused.

### `spend.py`: prices, ceilings and the typed phrase

- **The phrase is derived from the preview**: action, subject, both hourly rates, and a
  single-use challenge only this process's preview can issue. A wrong phrase neither
  reveals it nor spends the challenge. It is an operational guard, **not** the project
  lead's permission, and it does not stop a script.
- **Ceilings** cover pod plus volume, hourly and over the hard lifetime, and apply again to
  the price the provider *actually* returned: a pod created above it is closed at once.
- **Card allowlist.** Create refuses, before any provider call, a `gpu_type` that is not a
  `gpu_type_id` in the placement table (`config/pod_placement.toml`, or `--placement`),
  and a row whose *reviewed* price exceeds `max_hourly_usd` net of the volume rate. Both
  are `refused-card`. An unreadable table refuses the launch. `adopt` is not gated: the pod
  exists, and refusing it would leave it billing unguarded.
- **Spend policy.** `config/spend.toml` ships unconfigured, so paid paths refuse until the
  project lead supplies a reviewed policy. `billing_cutoff_margin_seconds` must lie in
  0–3600 (never clamped) and is sealed into both shutdown controllers.
- **Balance floor.** `account_balance_floor_usd` is tested against the observed balance net
  of this action's cost to its deadline and every liability in the same lease root. An
  unavailable or stalled source refuses by name (one gate runs after `create` has returned
  a billing pod, before anything can stop it). Observations older than 60 s or future-dated
  are unusable. The balance is read only at the create and adopt gates, never again while a
  pod is live.
- **One live pod.** Create and adopt serialize under one lock and refuse
  (`refused-active-lease`) while any lease in the root is short of `closed-verified`:
  otherwise two affordable launches leave two pods billing behind one record. This refusal
  spends no challenge. An unreadable or unverified lease makes the liability unknowable and
  refuses.
- **Alerts.** `account_balance_alert_usd` sends notification-only warnings when a gate's
  reading is above the floor but below the alert line, suppressed for fifteen minutes after one lands; two safe readings re-arm it. The
  template's `$50.00` is unverified against RunPod.
- **One lease root per provider account.** Separate roots cannot see each other's
  liabilities, and nothing can enforce this without an account identifier.

### Per-stage boots, transfer and preflight

`staged.py` runs one collection stage per independently authorized boot, then takes the pod
down; it never adopts. Its durable records on the run volume: a **claim** keyed by the grant
reference, written before the provider is touched, so one grant cannot buy a second pod
(a retry after a refused create records a fresh reference); an explicitly unknown **cost
intent**, fsynced first, so a lost create response never reads as zero; a **boot record**
binding pod, collection, stage and grant, whose failure triggers immediate pod-down; and
one **close record** per boot, including a close-failure record when the close raised or
could not run. `render_boot_schedule` prints every expected boot before any is requested.

`transfer.py` carries sealed submission-manifest rows through a generic storage seam,
verifying SHA-256 and size before and after upload and never overwriting conflicting bytes.
`bootstrap.py` journals idempotent exact-commit, locked-`uv`, transfer, chair-cache and
preflight steps.

`preflight.py` measures CUDA, driver, capability, VRAM and disk, selects a plan from
`config/pod_placement.toml` (prebuilt profiles for rented cards, computed otherwise),
verifies every chair, and checks a stochastic proof-page read. **Serving is sequential**:
one model at a time, so tiers differ only in memory fraction, context cap, pixel cap and
batch size. **`assembly_proven` is derived, never declared**: true only when a real driver
read the card (`GpuProfile.measured`) *and* a chair read the golden page back through an
engine that served it (`SmokeResult.served_by`). Both carry an opaque module-private token
minted only by the `nvidia-smi` probe and the serving evidence path and refused from any
caller, so the claim cannot be set from outside.

## The pod CLI

`python -m operations.pod.cli` has `create`, `adopt` and `close`. Create and adopt need
explicit untracked provider and controller-armer factories plus a request file, so the
repository holds no credential, provider default or implicit controller. They print price
and ceilings and prompt for the phrase; EOF is a refusal. A refused preview prints its
reasons but withholds the phrase. **Do not invoke a factory that could contact a provider
without the project lead's current-session authorization.**

**Exit status never reads "nothing happened" when something did**: 0 a guarded success; 2 a
refusal naming no pod, lease or close; 3 anything that observed or touched a real pod,
wrote a lease or attempted a close — go and look (including a create refused because
another lease is open). An interrupt prints an `interrupted` record naming the leases
directory.

A request must make the provider-neutral timer the primary command, with a provider-owned
timer factory, a structured mandatory bootstrap command and a durable report path on the
volume. If bootstrap exits or its report cannot be kept, the timer closes the pod.

### `close --lease <id>`

Closes one live lease on purpose through `supervise.close_lease_now`, to the supervisor's
standard; anything short of verified is `UNVERIFIED CLOSE`, exit 3. It needs no armer,
preview or phrase, because it stops spending.

It refuses before any terminate: an id that is not 32 lowercase hex characters; a lease
this account does not hold (absent, or under another `--provider-name`); a lease file whose
`lease_id` differs from its name; a lease a live supervisor holds. An unreadable lease file
is exit 3 with its path.

**Failures before the provider is reached are exit 3**, prefixed `CLOSE NOT ATTEMPTED:` (as
opposed to `UNVERIFIED CLOSE`, where a close ran): a `--provider-factory` that will not
load, or an unreadable or unconfigured `--spend`. None of them stops a pod, so none may say
"nothing was paid"; the lease is left for its guards. A recorder or notification seam that
fails is written into the close record rather than abandoning the close.

**`--provider-name` is a label, not proof of account.** A factory for the wrong account
would observe a genuine absence and write `close-unverified`, and the supervisor would stop
guarding a pod still billing. So a pod reported **absent before any terminate was issued**
refuses, exit 3, and the lease is left for the supervisor.

`--spend` is required because the controller's poll interval, deadline and cutoff margin are
reviewed values.

### `--record-fixture PATH`

Appends every provider exchange (method, path, bodies, status, transport failure) through
`fixture.py`'s recorder as JSON lines (0600, fsynced, never truncated), so a drill leaves a
replayable fixture (04-6). Credential-shaped values under either shared predicate
(`models.looks_like_credential_field`, `models.looks_like_credential_value`) and the launch
token are replaced, and the record says `verbatim: false` with each scrubbed path. A
provider without `record_exchanges` — the fake — refuses the flag by name before any
preview, except under `close`, which records the unhonoured flag and still stops the meter.

### `boot_a_request.py`

```sh
python -m operations.pod.boot_a_request --spend config/spend.toml --placement config/pod_placement.toml
```

Renders the plain-language request the project lead reads before the drill: the cheapest
reviewed card and rate, the hard lifetime (900 s or the policy ceiling), the ceilings, the
full-lifetime cost, the expected immediate close, the exact command with
`--record-fixture`, and the request JSON with `--image`, `--volume-id`,
`--repository-commit` and `--hard-deadline` marked unsupplied until given. Only
`hard_deadline` is filled by hand; the `VERBATUS_BILLING_CUTOFF_MARGIN_SECONDS` placeholder is
sealed from the spend policy on every create or adopt. An unconfigured policy renders a
refusal, exit 2. The text authorizes nothing.

## The pod timer and its report

The timer factory needs ephemeral runtime facts and an untracked termination capability;
its behaviour is fake-proven only.

- **A pod-side process can be destroyed by its own DELETE**, so final verification belongs
  to the laptop.
- **If `pod_timer.py` fails before a provider-backed timer exists** (missing environment,
  deadline passed), nothing on the pod can terminate it. The pod goes `EXITED` and bills
  volume disk at double the running rate. The laptop supervisor is the automatic backstop,
  `close --lease` the manual one.
- A non-green close at the deadline is retried a small fixed number of times; the report
  records the count and the last evidence.

**A truncated pod-side report is normal.** The close runs inside the container it is
destroying, so the durable report usually reads `bootstrap: running`, `close: null`,
`green: false`. The `<report stem>-terminating.json` breadcrumb, written just before the
first DELETE, tells "never tried" from "destroyed mid-verification". **The laptop-side
close record is the authoritative verified close.**

## Tests

```sh
python3 -m pytest operations/pod
```

They include deliberately broken confirmation, ceiling, status, billing, transfer, cache,
smoke-read, controller and timer paths. `supervise` has seven drills (crash resume, lost
identity, unreachable provider, `EXITED` pod, already-closed lease, second driver, foreign
owner past deadline). `test_launch_drill.py` runs a real `PodRuntime.create` through the
real armer, pod report and supervisor in seven more. None makes a live call, and no
real-chair preflight has been demonstrated.

## What a launch writes on the volume, and how each part comes home

The volume outlives the pod but is destroyed under the retention decision, and anything
still only on it then is gone. `verbatus fetch-run` brings home two prefixes on its own;
everything else must be named with `--evidence-key`, and **this section is where that list
comes from**.

`<volume>` is `--volume-mount-path`, `<token>` the launch token, `<stem>` the bootstrap
report's filename stem. **A key is the volume path with `<volume>/` removed**:
`<volume>/pod-run-report-<token>.json` is `--evidence-key pod-run-report-<token>.json`.
A key that still starts with `/` is refused by name in the receipt's `refusals`.

**Fetched by prefix, no key needed:**

| Path on the volume | Written by | How it arrives |
|---|---|---|
| `<volume>/runs/<run-id>/` | the orchestrator's stages, through `RunTree` | `fetch-run`'s run prefix, every object checked against the tree's own digests |
| `<volume>/runs/<run-id>/<stage>/serving-logs/` | `SubprocessLauncher`, per started chair | the same prefix, but **unverified**: no manifest records an engine log, so each is listed under `unverified_serving_logs` with its arrival digest. A log still growing, or past the per-object bound, is refused by itself into `refused_serving_logs` and the verified tree still comes home |
| `<volume>/preflight/<stem>/` | `bootstrap_main`'s PREFLIGHT — golden page, serving logs, serving receipts, launch audits | `fetch-run`'s evidence prefix, into `<local root>/evidence/` |

**Named with `--evidence-key`, or they stay on the volume:**

| Path on the volume | Written by | How the key is derived |
|---|---|---|
| `<volume>/bootstrap-report-<token>.json` | `bootstrap_main --report-path`, rewritten every hold tick | the `--report-path` the launch request carried, mount prefix stripped |
| the bootstrap journal, `<volume>/…-<token>.json` | `bootstrap_main --journal` | the request's `--journal`, mount prefix stripped; it must be under the mount and carry the token |
| `<volume>/pod-run-report-<token>.json` | `pod_run --report-path` | the nested `--report-path` the launch request carried, mount prefix stripped |
| `<volume>/pod-run-report-<token>-hold.json` | `pod_run`'s hold after a `complete` or `held` run (`Plan.hold_path`) | the pod-run report key with `-hold` before its suffix. The only record that the pod stayed alive to the hard deadline |
| `<volume>/pod-runtime-report-<token>.json` | `pod_timer --report-path` | the request's outermost `--report-path`, mount prefix stripped |
| `<volume>/pod-transfer-journal.json` | `ChecksummedTransfer` | **a fixed name at the volume root**, no token. The only durable record of which submission rows were verified against target-observed bytes |

The pod-run report's `-liveness.json`, `-timings.json` and `-transcript.log` siblings and
the runtime report's `-terminating.json` breadcrumb are derived the same way.
`verbatus fetch-run --launch-receipt <path>` derives every key except the transfer journal
from the saved receipt's sealed `docker_start_cmd`.

**Not records, deliberately not fetched:** `<volume>/chair-cache/` (weights),
`<volume>/submission/` and `<volume>/submission-manifest.json` (page images and their
ledger, kept beside rather than inside the folder because the Door refuses pipeline records
among source images), `<volume>/pod-transfer/` (transferred bytes), and any other upload
batch at `<prefix>/` and `<prefix>-manifest.json`.

**The single-resident GPU lock is not on the volume.**
`operations.serving.residency.POD_RESIDENCY_LOCK_PATH` is `/tmp/verbatus-pod-gpu.lock` on
container-local disk: the boundary is the one rented card, not a run tree, and a network
mount is not known to honour advisory locks. It is opened `O_NOFOLLOW` and created 0600, so
a planted symlink is a named refusal.

## The pod image contract

What the bootstrap assumes about the machine it starts on. `bootstrap.verify_image_contract`
enforces it first in the `REPOSITORY` step — before `git fetch` and the ~10 GB environment
sync — so a bad image goes red with a named reason and remedy instead of a
`ModuleNotFoundError` or an invisible authentication prompt on a billing pod.

### Fresh Ubuntu 24.04 RunPod preparation

After cloning onto a fresh Ubuntu 24.04 RunPod container and **before** `uv sync`, run as
root:

```bash
bash operations/pod/prepare_runtime.sh
```

It installs the official `uv 0.12.1` at `/usr/local/bin/uv`, `ninja-build` at
`/usr/bin/ninja` (without it the Chandra and DAI vLLM warm-up fails), and a
Landlock-capable `setpriv` at `/usr/bin/setpriv` (stock Ubuntu 24.04's lacks
`--landlock-access`). A working `setpriv` is kept; otherwise it verifies pinned SHA-256
digests, builds only `setpriv` from util-linux 2.42.3, and keeps the old binary at
`/usr/local/lib/verbatus-runtime-prerequisites/setpriv.before-util-linux-2.42.3`. It is
idempotent, with bounded, retried downloads. Either `setpriv` must pass:

```bash
setpriv --no-new-privs --landlock-access fs:write-file -- /bin/true
```

A kernel without working Landlock is a refusal: choose another host, never bypass it.

Keep the repository, `.venv` and `UV_CACHE_DIR` on container-local disk. The serving stack
needs roughly 101 GB of model cache against a 200 GB container disk; keep inputs, outputs,
evidence and materialized models on the network volume.

### What the image must carry

- **A checkout; the bootstrap does not clone.** `checkout_commit` runs
  `git fetch --no-tags origin <sha>` and `git checkout --detach --force <sha>` in
  `--repository`. `boot_b_request.py` renders `/opt/verbatus` on container-local disk; the
  volume holds evidence, not code.
- **An `origin` reachable with no HOME.** `BOOTSTRAP_ENVIRONMENT` is only `PATH`, `LANG`,
  `LC_ALL` and `UV_CACHE_DIR`, so git sees no `~/.gitconfig` or global credential helper.
  A private fetch needs a repository-local credential helper, an `http.<url>.extraheader`
  token, credentials in the remote URL, or an SSH remote whose key the pod user can reach;
  an `http(s)` origin with none of these is refused.
- **Tools at absolute paths; PATH is never searched.** `git` at `/usr/bin/git`, `uv` at
  `/usr/local/bin/uv` (`BOOTSTRAP_EXECUTABLES`). The default uv installer writes
  `~/.local/bin`, which does not qualify.
- **A pre-built `<repository>/.venv` whose interpreter runs the primary process.**
  `bootstrap_main` imports PIL at module scope, so even `--hold-only` needs the environment
  before the bootstrap's own `uv sync`. And `ServingManager` launches vLLM as
  `sys.executable -m vllm.entrypoints.cli.main`: under the system `python`,
  `uv sync --group pod` fills a venv nothing uses and PREFLIGHT fails **after** the download
  is paid for.
- **A working directory where `python -m operations.pod.pod_timer` resolves** (the
  `dockerStartCmd` sets none): start inside `<repository>` with its `.venv` python.
- **Enough container disk.** The bootstrap fills it twice (wheel cache, then `.venv`), so
  every create states `container_disk_gb` and `sync_uv_environment` checks free space first.
  Both numbers are bounds until the first boot measures the real footprint.
- **No credential from this repository.** How the pod gets git credentials and the timer's
  provider capability are out-of-tree decisions, each with a checklist row below.

## The serving stack, re-planned and locked

The real roster's stack is the `pod` dependency group in `pyproject.toml`, locked in
`uv.lock` under `sys_platform == 'linux' and platform_machine == 'x86_64'` markers, with
exactly the versions `config/serving_recipes_real.toml` names; `test_pod_run.py` holds the
two together.

| Package | Pin | Licence | Why this one |
|---|---|---|---|
| `vllm` | `0.27.1` | Apache-2.0 | The newest release that registers every architecture the roster declares **and** states no `huggingface_hub` floor of its own |
| `transformers` | `5.14.1` | Apache-2.0 | The version the vLLM 0.27 line's requirements moved to; above vLLM's `>= 5.5.3` floor and the Perlector's `>= 5.8.0` |
| `qwen-vl-utils` | `0.0.14` | Apache-2.0 | Latest; it and its dependencies (`av`, `pillow`, `requests`) all publish linux x86_64 wheels, so nothing compiles on the card |

Licence sources: the `LICENSE` files at `github.com/vllm-project/vllm` (tag `v0.27.1`) and
`github.com/huggingface/transformers`; `qwen-vl-utils`'s PyPI metadata (maintained under
`github.com/QwenLM/Qwen2.5-VL`, Apache-2.0).

- **`transformers` 4.x cannot lock**: no 4.57.x accepts `huggingface-hub` 1.x, and the
  project pins `huggingface_hub==1.26.0`.
- **One vLLM release serves all four chairs.** Chandra-2 and the Perlector declare
  `Qwen3_5ForConditionalGeneration` (multimodal); DAI and Churro-3B declare
  `Qwen2_5_VLForConditionalGeneration` (each model's `raw/main/config.json`). vLLM v0.27.1's
  `registry.py` lists both in `_MULTIMODAL_MODELS`. The Perlector's vLLM recipe asks for
  vLLM 0.17.0+ and transformers >= 5.8.0.
- **Not vLLM 0.28.0**: it declares `huggingface_hub>=1.27.0`, which collides with the
  project's pin, and only its DFlash2 speculative decoding (unused here) needs it.
- **No `flash-attn`**: vLLM does not import it (it ships its own FlashAttention), and
  `flash-attn` 2.8.3.post1 is sdist-only, which would mean an hours-long nvcc build on a
  rented card.

**Proven offline:** `uv lock` resolves the group (with `torch 2.13.0`) and macOS syncs
without it. **Only a boot proves** the wheels install on the pod and the four checkpoints
load under this release, so every row stays `preflight_state = "unproven"` and
`ServingManager.start` refuses each until a reviewer stamps it after a real-silicon
preflight. `bootstrap.py` runs `uv sync --group pod`; the ~10 GB download happens on the
billing card, once per pod.

## First gated live-pod checklist

A checklist for one authorized live demonstration, not authorization to create a pod.
Record the pod id, timestamps, provider responses, and whether each item is **verified**,
**unverified** or **not run**. No unchecked item may be reported as a pass.

- [ ] Record the project lead's current-session authorization, the synthetic workload, the
  spend ceilings, `account_balance_floor_usd`, the balance observation with active
  obligations and this run's maximum liability, and the one lease root used for this
  account.
- [ ] Confirm the pod-scoped API key holds **delete** and **billing** rights for this exact
  pod. Do not infer either from a successful create or GET.
- [ ] Record whether the API version used offers any pod-side TTL / `maxRuntime` on create
  (none is documented in v1 or v2; `V2_MIGRATION.md` §3).
- [ ] **Before the first paid create under v2**, run the free catalogue read
  `RunPodV2Provider.cross_check_catalogue` with every `gpu_type_id` and `hourly_usd` from
  `config/pod_placement.toml`, and record its findings. It confirms the exact `gpu.id`
  strings and the reviewed Secure prices. A finding goes to the project lead; the sheet is
  not edited from it.
- [ ] **Confirm before early 2027 whether REST v2 has grown an account-balance field.** The
  balance observer reads GraphQL, which RunPod retires in early 2027; without a replacement
  the balance floor loses its live source (`V2_MIGRATION.md` §1).
- [ ] Exercise **a pod that fails field validation and cannot be auto-terminated**: record
  any returned identity, the launch-token recovery, whether automatic close could act, and
  the manual console recovery if not.
- [ ] On the route the run uses, verify create accepts the real GPU id, attaches the volume
  at the requested path, and returns what the contract check reads, matching the sealed
  request. v1: id, name, `desiredStatus`, `costPerHr`, `networkVolume`, `volumeMountPath`,
  `machine.gpuTypeId`, image, `dockerStartCmd`, template and `interruptible=false`. v2: id,
  name, `status`, `cost`, `createdAt`, `startedAt`, `cloud`, `gpu.id`, `gpu.count`,
  `mounts.network`, image, template, and **`args` exactly as sent** — record whether it
  comes back as the exec-form JSON object, re-serialized, or as a shell string, and whether
  the deconstructed `entrypoint` and `cmd` appear.
- [ ] Under v2, record every `status` word the pod passes through and how long each lasted,
  and whether `pod.cost` is non-zero while `PROVISIONING`.
- [ ] On the route the run uses (v1 as well as v2), record whether the pod-scoped
  `RUNPOD_API_KEY` is accepted by the routes the pod-side timer calls (status, terminate,
  list, billing), and that the pod's env carries `VERBATUS_RUNPOD_ROUTE` naming that route.
- [ ] Verify launch-token recovery from the pod list after a deliberately lost create
  response, without confusing a same-name pod.
- [ ] Confirm the pod list's paging: v1 documents none; v2 documents `pagination.nextCursor`
  and the adapter follows it. A truncated list would mean a false absence or a second POST
  for one launch.
- [ ] Rerun the checksummed transfer end to end. RunPod's S3 endpoint drops custom metadata
  on HeadObject and GetObject, so the adapter hashes target bytes under the manifest's size
  bound; that path is proven only by injected-client tests.
- [ ] Verify the volume is mounted at the sealed path, receives the token-bound report,
  survives a process restart, and supports the run tree's hard-link publication.
- [ ] **Settle how the pod-side timer gets its provider capability, and prove it, before
  Boot A is worth running.** `timer_context_from_environment` needs `RUNPOD_API_KEY` in the
  pod's environment; without it `pod_timer.main` prints "nothing can close this pod", exits
  2, and the pod stays `EXITED` and billing until the supervisor's next tick. The only pod
  environment a tracked launch sets is `PodCreateRequest.metadata`, which rightly refuses
  credential-shaped keys. Confirm the provider injects the key for this pod type and image,
  or put it in a provider template and require `template` on every real request. Do the
  same for `HF_TOKEN` / `HUGGING_FACE_HUB_TOKEN`. Record the route, never the value.
- [ ] **Read an `EXITED` pod's console log before closing it.** The pod has no ports or SSH,
  and many failures stop anything reaching the volume, so that log may be the only
  evidence and closing destroys it. Copy it beside the drill fixture, then close; an unread
  log is never a reason to keep a pod billing.
- [ ] Run Boot A before Boot B ([the boot plan](#the-boot-plan-boot-a-the-drill-before-boot-b-the-real-thing))
  and record its four facts.
- [ ] Record the balance the observer read beside the console's figure, and whether the
  pod-scoped key is accepted by the GraphQL endpoint at all.
- [ ] Prove supervisor, armer, acknowledgement channel and bootstrap together; offline they
  have never run as one. Verify the timer receives the real pod identity, its capability and
  the sealed cutoff margin, and acknowledges to the token-bound path; the laptop keeps a
  receipt bound to lease, pod and deadline with no capability material.
- [ ] Demonstrate the timer-startup backstop: with the timer unable to construct a
  provider, the laptop supervisor detects and closes the `EXITED` pod.
- [ ] Run the real preflight (GPU, driver, capability, VRAM, disk, chair cache, smoke read).
  The smoke preflight may launch the still-unproven rows for qualification; afterwards run
  `python -m operations.serving.qualify` on its report and evidence, review the candidates,
  and stamp only the measured tier's rows proven. **Record `nvidia-smi`'s driver and CUDA version
  before `uv sync --group pod`**: `torch 2.13.0` needs CUDA 13, so an older driver should
  be refused before the download. Record whether the sync completed and how long it took,
  whether each chair loaded under `vllm 0.27.1`, and per chair whether the witness was read
  back. **Record free container disk before and after the sync and the final `.venv` size**;
  they replace `models.DEFAULT_CONTAINER_DISK_GB`, `bootstrap.UV_CACHE_REQUIRED_BYTES` and
  `REPOSITORY_VENV_REQUIRED_BYTES`.
- [ ] Fetch the run: `verbatus fetch-run --run-id <id> --into <local root> --network-volume DATACENTER:VOLUME_ID --launch-receipt <saved launch receipt> --evidence-prefix preflight/<bootstrap report stem>`,
  plus `--evidence-key pod-transfer-journal.json` if the launch transferred. Record whether
  `runs/<id>/` reconciled, which records from
  [the volume section](#what-a-launch-writes-on-the-volume-and-how-each-part-comes-home)
  arrived, and `unverified_serving_logs`. This path has never met a real endpoint.
- [ ] At the first real response, require the harness to publish an immutable run-tree
  artifact on the volume before requesting the next; interrupt it and read it back. Repeat
  for every live witness and Perlector reading. A final export does not satisfy this.
- [ ] Verify shutdown: GET-404, list absence, and non-empty exact-pod `GET /billing/pods`
  rows from creation through the cutoff; lag or empty records are **unverified** (v2 may
  report `pending-reconciliation` inside a resolved window). Under v2, record whether
  `metadata.query` is present on the `podId`-filtered route and what window it resolved.
  Confirm the close report names the volume's hourly price and that no volume was deleted.

## Deferred items

Each open item closes on its named condition, not on being noticed again. The code cites
these IDs.

| # | Item | Status |
|---|---|---|
| 04-1 | No durable laptop-supervisor driver | **Closed** by `supervise.py`. |
| 04-2 | No controller armer that observes the real timer report | **Partly closed.** The read is real and fake-proven. Closes when the first boot sees a mount-written object appear in the S3 view and records the delay. |
| 04-3 | No runnable bootstrap entrypoint | **Closed** by `bootstrap_main.py`, which holds rather than exits. |
| 04-4 | A timer startup failure leaves nothing on the pod able to terminate it; the `EXITED` pod bills volume disk at double rate | **Mitigated.** The laptop supervisor closes it on its next status read. No provider-side TTL exists in the v1 or v2 documentation. |
| 04-5 | Untested seams | **Open**: the success paths of `sync_uv_environment`, `pod_timer.main`/`load_timer_context`, `cli.main` end to end through real `module:callable` factories (tests monkeypatch them), and `UrllibRunPodTransport`. |
| 04-6 | Every RunPod field name is documented, not observed | **Open** until the first live run on each route in use; `--record-fixture` captures its exchanges to rebuild the offline suite on observed shapes. |
| 04-7 | The close billing window was anchored on `lastStartedAt`, not creation | **Anchor closed under v2**: `created_at` is the pod's `createdAt`. **Still open**: that RunPod bills nothing before `createdAt` is unobserved, and v1 still anchors on `lastStartedAt` until it is deleted. |
| 04-8 | The at-most-one same-pin cache re-fetch does not ship | **Partly closed.** Constructed with `refetch_same_pin=None` (no cache-clear verb); `_build_cache` is untested. |
| 04-9 | Nothing proves billing buckets cover the declared window | **Window half closed under v2** when `metadata.query` is present: the declared window is the provider's resolved one, must cover the request, and an empty answer inside it reads `pending-reconciliation`. **Still open**: whether `metadata.query` appears on the `podId`-filtered route, and whether the buckets *fill* the window, wait on a live run; a coverage check written before that would guess, and a wrong guess turns every close red. |
| 04-10 | The real serving stack could not be locked | **Closed**; see "The serving stack, re-planned and locked". |

## The boot plan: Boot A, the drill, before Boot B, the real thing

The first live demonstration is split because its most load-bearing item, the
acknowledgement channel, is the one no offline test can measure; a single boot that ends
unarmed wastes the pull and invites relaxing the check that just worked. The split costs a
few dollars of a cheap card.

**Boot A, the drill.** The cheapest card, `hard_lifetime_seconds` around 900,
`ObservingControllerArmer`, `bootstrap_main --hold-only`, `--record-fixture` on. It closes
its pod immediately by construction, green only when GET-404, list absence and billing
agree. It buys four facts: does the pod-written object appear in the S3 view, under which
key, after how long, and does the pod-scoped key hold delete and billing rights.

**The arming wait is two waits, so the drill records two durations** (in
`pod-arming-drill.v2`): first until the provider reports the container started
(`CONTROLLER_CONTAINER_START_TIMEOUT_SECONDS`, 600 s), then the channel bound
(`CONTROLLER_ARMING_TIMEOUT_SECONDS`, 300 s), so the image pull does not eat the propagation
budget. The start signal is `ProviderStatus.started_at` from RunPod's `startedAt` (v2) or
`lastStartedAt` (v1), null until the pod first runs (v1's `desiredStatus` reads RUNNING as
soon as `create` returns).
The untracked armer factory supplies the probe (`started_at()` returning
`provider.status(pod_id).started_at`); without one the receipt says
`container_start_probe: none`.

**Decide Boot A's arithmetic before renting.** The defaults sum to 900 s, the whole drill
lifetime, leaving the close nothing. Pass smaller `container_timeout_seconds` and
`timeout_seconds` (300/300 is the obvious first try) or authorize a longer lifetime. Bounds
are clamped down to what the lease has left, so an oversized pair silently starves the
second wait, the one the drill measures. `boot_a_request.py` prints the sum.

**Boot B, the real thing.** `ChannelControllerArmer` with bounds from Boot A's measurements;
materialize, preflight, the full checklist; no reading yet. `boot_b_request.py` builds every
rendered request into a real `PodCreateRequest` before printing, so it cannot publish a
shape the create gate refuses. Its `docker_start_cmd` nests `pod_run`'s argv and, after a
literal `--`, `bootstrap_main`'s, each with its own `--report-path`; the gate binds the
launch token into both and the nested `--journal` and refuses two halves naming one file.

**What these boots need from the project lead:** `config/spend.toml` values and the GPU
class; the S3 access and secret keys in the launching shell; separate in-session permission
for Boot A and for Boot B; and confirmation of the two-boot split.

## If your task seems to need one

Stop and say so, with what you would start, the hourly rate, roughly how long, what it
buys, and how it gets turned off. Then wait: that is what the project lead needs to give or
refuse permission. In an unattended session, raise it as a decision the moment you find it,
and carry on with everything that does not depend on it.
