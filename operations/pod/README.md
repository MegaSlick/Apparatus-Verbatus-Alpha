# Paid infrastructure — pods, GPUs, and anything that bills

**Read this before invoking anything that can start a meter**: RunPod, any GPU host, any
hosted inference, any storage or egress that is charged.

## The rule

Who may start, switch or delete paid infrastructure, and what a permission covers, is
`AGENTS.md`, "Who decides". It is stated there once and not restated here. In short: no
billing action without the project lead's permission in the current session, and a
permission never carries over from an earlier one.

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

The pod CLI's runtime (provider adapters, lease, supervisor and pod timer) is fake-first.
**It has never started, inspected or billed a live pod**: every adapter test uses an
in-memory transport (the redirect-refusal test uses two loopback servers). The RunPod field
names come from RunPod's documentation, cited page by page in `provider_runpod.py`, not from
observed responses. Every paid run so far took [the hand route](#the-hand-route-a-proof-run-started-by-hand)
instead: `runpodctl`, the pod guard, `pod_run` and its bootstrap. Before the first live run
through the pod CLI, work through the [first gated live-pod checklist](#first-gated-live-pod-checklist).

### Provider seam and RunPod adapter

`provider.py` is the seven-verb provider seam; `provider_runpod.py` holds the only RunPod
adapters, one per REST route, behind that seam and one `HttpTransport`.

- **`RunPodV2Provider`** (`api.runpod.io/v2`) is the default. RunPod retires v1 on
  2026-11-15. `V2_MIGRATION.md` maps every v1 endpoint, field, status code and lifecycle word
  to v2, and says what is done and what waits on a live run.
- **`RunPodProvider`** (`rest.runpod.io/v1`) stays selectable until the first live run under
  v2 is green, then is deleted in its own commit. The live runs so far created their pods
  with `runpodctl`, not through either adapter, so that run is still to come, and RunPod
  retires v1 on 2026-11-15 whether or not it has happened.
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
  same word is waited on for the container-start bound (600 s) after the arming receipt,
  because the timer arms from inside the container and RunPod may report RUNNING only once
  it is healthy; past that, it closes when two consecutive ticks still see it. `ERROR`
  closes at once. A provider that cannot answer is neither
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

- **Chair cache.** `CHAIR_CACHE` records the pinned source plan of each Hugging Face
  chair without copying its weights, and copies each local-repository chair (Surya's
  bundle) from the volume store to where the roster binds it on container-local disk,
  verified against its manifest. For the Hugging Face chairs, PREFLIGHT and each stage
  copy a chair's pinned snapshot from the volume store to the container-local cache at
  `cache_root/by-digest/<digest_manifest>`, verify the copy against its pinned manifest,
  and evict the least recently used other digests when the next fill would not fit.
  Chairs pinned to one manifest (the Perlector and the reconstructor) share one copy.
  Each copy hashes the bytes as it writes them, across files in one pool sized from the
  container's usable CPUs (or `VERBATUS_IO_WORKERS`), so a fresh copy is read once, not
  copied and then re-read; the receipt records the worker count. Within one process a
  verified, unchanged cache is not hashed again, so PREFLIGHT's smoke start reuses the
  verification its cache check just made; each stage process still verifies from the bytes.
  The Hugging Face chairs' copies start early: UV_ENVIRONMENT starts them on a background
  thread (`chair_prefill.py`) while uv downloads, every digest at once through one copy
  pool that takes the largest waiting file first (Chandra's single 10.6 GB file starts
  at once), and MODEL_STORE and CHAIR_CACHE wait for them before completing. A copy that
  refuses fails MODEL_STORE, whose receipt says those store bytes are verified at copy.
  The background copy never evicts a cache; a chair whose store artifact is not present
  yet, or that does not fit beside what uv will still write (chairs are taken in the
  order the stages first need them, each only if it still fits), is left for PREFLIGHT as
  before. CHAIR_CACHE's receipt lists under `prefill` what was filled and what was left,
  and PREFLIGHT takes over those verifications instead of hashing the bytes again.
  A pod given a stage selection (`pod_run`'s `preflight_roles`) prepares only the chairs
  those stages use: MODEL_STORE fetches and checks only their artifacts, CHAIR_CACHE plans
  and places only them (others read `not-selected`), and the disk and environment checks
  count only them. So the small card of a two-card split never reads the Perlector's model
  and the big card never places Surya. A bare `bootstrap_main` run prepares every chair.
  An adapter base remains available while its adapter is filled. The at-most-one same-pin
  re-fetch is not wired (04-8); a mismatch is red and names the chair.
- **Transfer is optional.** No submission manifest on the volume is a vacuous success; a
  manifest with no configured target is a refusal.
- **`PREFLIGHT`** runs `ChairRegistry.ensure` for the selected roles, in the order the
  stages first need them, then a smoke read through the
  serving package's production seam (`assemble_serving_smoke_reader` around
  `ServingManager`, fed `operations/serving/smoke.py::VisionSmokeCall`). The witness value
  is drawn from the CSPRNG on the pod and rendered onto a golden page under
  `<volume>/preflight/<report stem>/` just before the read, so it was never in a file or
  prompt. The DAI chair reads a pinned public RecordGold record instead, fetched and
  verified against its digests at preflight and scored by character error rate
  (`operations/serving/recordgold_smoke.py`); a fetch failure is a named refusal, not a
  chair failure. While one chair smokes, one background thread copies and verifies the
  next chair's cache (never evicting, so the cache being served stays put; a chair it
  finds no room for is filled after the smoke, as before). The card still serves one
  chair at a time. Serving receipts, launch audits and evidence manifests land
  content-addressed in the same directory. `--fixture` with `--page-witness-file` supplies
  a golden page instead.
  The receipt records the measured card (`environment.vram_gib`, `gpu_count`,
  `compute_capability`) and `capacity_plan`: how many sequences each selected chair
  is launched with on this card, never fewer than its row's `max_num_seqs`
  (`operations/serving/README.md`, "Scaling to the card"); the smoke launches each
  chair at that width. It is null when the probe could not measure the card, and the
  rows then launch as written.
  Ordinary serving refuses an unproven row; only this smoke assembly may launch one, for
  qualification, and its audit says so. After a green real-silicon report,
  `python -m operations.serving.qualify` renders review candidates for the measured tier;
  it never edits the catalogue.
- **Configuration is one selection.** `--serving-recipes-config` defaults to the
  fixture-only `config/serving_recipes.toml`. A real launch names `config/models-real.toml`
  and `config/serving_recipes_real.toml` together. The journaled `CONFIGURATION` step,
  after checkout and before anything is synced, fetched or served, parses the roster, the
  catalogue and the placement table and binds the three config paths and seals into its
  receipt. A resume with a changed selection fails there (restore it or start a new
  journal); a journal whose receipt is an earlier `pod-bootstrap-configuration` version is
  refused by schema. The placement table is always the checkout's own `config/pod_placement.toml`,
  the one the stages seal; `CONFIGURATION` refuses any other resolved path, a symlink out
  included.
- **CUDA compatibility.** Before the uv install, `CUDA_COMPAT` records `nvidia-smi`'s
  driver and GPU names. Drivers below 580.65.06 on professional RTX or data-center cards
  get the pinned `cuda-compat-13-0=580.178.04-1ubuntu1` from the image's NVIDIA apt
  repository. Missing apt lists or that pin cause a named refusal; the bootstrap does not
  refresh lists on a billing pod. It records the installed package version and requires
  `cuInit(0)` to succeed through the compatibility library before it puts
  `/usr/local/cuda-13.0/compat` first in `LD_LIBRARY_PATH` for preflight and `pod_run`'s
  orchestrator, whose serving children inherit it. A GeForce card with an older driver
  is refused before the serving stack download. On every driver, new or old, it then
  calls `cuInit(0)` and `cuDeviceGetCount` through `libcuda.so.1` (the compatibility copy
  when one is installed) and refuses the host by its hostname, driver and cards when
  either fails or no device is counted: a host that lists its cards but cannot initialise
  CUDA would otherwise spend the whole setup before its first GPU stage fails. The calls
  run in a child process killed after 120 s, so a hung driver is refused rather than
  holding the pod. A library or call that is missing is refused as the image's fault (the
  next host would fail the same way), not the host's. The receipt
  records the action and the device count, including each resume recheck when a restart
  has removed the container-local installation.
- **Refusals come before any action**: a journal or report path outside the mounted volume;
  a lockfile that is not the checkout's `uv.lock`; a volume that fails a real write-and-read
  probe (it never creates the mount point it requires); a missing hard deadline; a
  credential-looking argv token; an unknown or unparseable argument, named by flag only, never
  by value. The environment is scrubbed by the shared credential-shaped
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
`pipeline/orchestrator/run.py` with the pod's interpreter: the `--no-hold` hand route
uses `/var/tmp/verbatus-runs` on container disk by default, while the timer route keeps
`<volume>/runs`. `--run-root` can name another approved root. The submission stays inside
the volume, and the config pair is the one the
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
| 8 | selected stages completed before Armarium; the timer closes the pod |

It refuses by name: no `--`; a `--hold-only` plan; a report path that is the bootstrap's or
lacks the launch token; a run root outside approved storage or a submission outside the
volume or missing; a policy
outside the repository; a `--perlector-protocol-config` outside the repository, not a
file, or not one the seal reader parses; a resume whose Perlector protocol or (on a real
run) run policy differs from what `run.json` sealed, or whose `run.json` cannot be read;
`--no-hold` under a launch token, without a pod id on the container's first process, with
a shell pod id that is not the container's, or without a fresh guard heartbeat for that
pod; a bad run id.

**The data gate is asked first**, before a model is fetched on a billing card.
`config/data_handling_policy.json` lists the pod volume's mount path (the
`volume_mount_path` `boot_a_request.py` seals) beside the local `private/` root. That
listing is the project lead's standing disclosure decision: a rented pod's volume is
accepted exposure for the duration of a run. The policy also admits only
`/var/tmp/verbatus-runs` as the hand route's local working root. After each completed
stage, the orchestrator copies new run files to `<volume>/runs/<run id>`, fsyncs and
checks their bytes before starting the next stage. `pod_run` repeats the sync after the
orchestrator exits, before its final report or guard release. It copies an existing
volume run back to local disk before a resume and refuses a differing file. The sync
never removes volume evidence. A sync failure makes the run failed; a failed final sync
also leaves the guard unreleased for recovery. `verbatus fetch-run` is the way home. A
submission outside every listed root is refused. Every run report records which roots
resolved and which did not (`approved_storage_roots`,
`skipped_storage_roots`).

`pod_run` forwards the `--placement-tier` measured by green `PREFLIGHT` to the
orchestrator and records it in the report, and with it the receipt's capacity plan as
`--capacity-plan` (omitted when the receipt has none). Neither is sealed: both are
facts of the card. A plan whose digest, tier or serving digests do not match the
receipt is refused by name. `--stage` runs one boundary, `--from` and
`--to` run an inclusive range, and no selection runs the full sequence. `--models small`
selects Door through Attestatores on a cheap card; `--models big` resumes Perlector
through Armarium on a big card, after verifying this run's sealed Attestatores
stage on the volume before bootstrap. The big card sets the witness pod's bootstrap journal
aside as `bootstrap-journal-$RUN.pod-<witness pod id>.json` and bootstraps for its own
GPU; a replacement for a dead pod does the same. The two model toggles use the same range validation.
For the hand route that finishes model work on the pod, use `--from perlector --to
coniector` in a later invocation after a `--models small` run, or use `--from door --to
coniector` for an unsplit run.
Both flags go before the literal `--` and the bootstrap plan. `--to` alone is refused.
Fetch the sealed tree and run Recensor through Armarium on the laptop as described in
`operations/operator/README.md`.
A selection preflights only the chairs its stages use: the Designator's, the witnesses,
the Perlector, and the Coniector's `reconstructor` when `config/reconstruction.toml`
has the stage ask it (`mode = "on"`); the run is refused unless each has green
PREFLIGHT evidence.
Any range is the orchestrator's semi mode, which stops at the first held stage. In every
mode, auto included, a run whose Recensor holds anything stops there, before Archetypus
and Armarium. Coniector has already run when the selection includes it
(`pipeline/orchestrator/CONTRACT.md`, "A held Recensor stops every mode"). After a
selection through Coniector, the operator can decide and resume from Recensor off the
pod.

- **`--stop-after-coniector`** (off by default) ends the selection at the Coniector, the
  last stage that needs the card, so the GPU pod is released instead of paying for the
  Recensor's CPU work (25 min at 0% GPU on a $2.49/h card on the 2026-10-09 cold run).
  No selection becomes `--from door --to coniector`, `--models big` becomes `--from
  perlector --to coniector`, and a `--from`/`--to` range past the Coniector ends there;
  a selection that already ends earlier is unchanged, and one that starts after the
  Coniector is refused before the bootstrap (it would run nothing on the card). The run
  then ends `selection-complete` and returns at once, so the pod closes. The report
  records the flag (`plan.stop_after_coniector`). As any range it is the orchestrator's
  `semi` mode: a stage that holds before the Coniector stops the run there. Then fetch
  the tree and run Recensor through Armarium on the Mac, as below.

**Recensor on the Mac, from the fetched tree.** The Recensor, Archetypus and Armarium
serve no chair; on the Mac the Recensor of the 73-page cold run took about 27 minutes of
CPU. Fetch the run, then run the tail with the same sealed configuration
(`operations/operator/README.md`, "Finish a pod run on this computer"):

```sh
verbatus fetch-run --run-id <id> --into <local root> --network-volume DATACENTER:VOLUME_ID
.venv/bin/python pipeline/orchestrator/run.py --run-id <id> --run-root <local root> \
  --models-config config/models-real.toml \
  --serving-recipes-config config/serving_recipes_real.toml \
  --mechanics-qualification --from recensor --to armarium
```

Add `--corpus-register` if the run sealed one. A run held at the Recensor is reviewed and
decided there (`verbatus review`, `verbatus decide`) and the same range is run again.

- **`--mechanics-qualification`** is needed for any real-roster run today. Every row in
  `config/serving_recipes_real.toml` is `preflight_state = "unproven"`; PREFLIGHT may serve
  one to qualify it, but the stages refuse it by name, from the first stage that serves a
  chair, unless this flag is passed. It is forwarded to the orchestrator, sealed into the
  run (every later selection or resume of that run must pass it too), recorded in the
  report, and proves no row.
- **`--perlector-protocol-config <path>`**, inside the repository, is forwarded to the
  orchestrator, which seals its bytes into the run's config digest, so every later
  selection or resume of that run must name the same file; the report records it
  (`plan.perlector_protocol_config`, `null` for the orchestrator's default). pod_run
  reads it with the Perlector's own protocol loader before the bootstrap, closed schema
  included, so a protocol the Perlector would refuse is refused before anything is paid
  for.
- **A resume is checked against its seal before the bootstrap.** When the run already has
  a `run.json`, the protocol this launch hands the orchestrator (the named file, or the
  checkout's default) must have the `perlector-protocol` digest the run sealed, and on a
  real run `--mechanics-qualification` must give the sealed `run-policy` digest
  (recomputed under the orchestrator's defaults for the knobs pod_run never forwards). A
  fixture run seals it only inside its `config_digest`, so its stages still catch that
  mismatch, after the bootstrap.
- **`--no-hold`** is for a run started by hand, outside the pod timer
  ([the hand route](#the-hand-route-a-proof-run-started-by-hand)). After the final report
  of any run past a green bootstrap, whatever its outcome, it returns instead of holding
  and moves this pod's guard deadline (`<volume>/.pod_guard/deadline-$RUNPOD_POD_ID`) to
  now, so the guard deletes the pod on its next one-minute tick instead of leaving it to
  the idle ladder. The pod id is read only from the container's first process
  (`/proc/1/environ`), never from the shell: every pod's deadline sits on the shared
  volume, and an id exported by hand could name another live pod, whose guard would then
  delete it mid-stage. Before the bootstrap, `--no-hold` is refused when the first
  process names no pod id (then no guard armed for this pod, and there is nothing to
  release), when the shell exports a different id, or when that pod's
  `heartbeat-<pod id>` is missing or older than five minutes. Run without `--no-hold`
  then; the guard's idle ladder still applies. Before the deadline it writes
  `released-<pod id>` (run id and outcome), which the guard quotes in its ping. A guard
  started with no deadline (the budget off) honours one written later, so when there is
  no deadline file and the guard is alive the release writes one; with no file and no
  live guard, or a file it cannot read, `guard_release.released` is false and says why.
  It never moves a deadline later. `released` means the deadline was written; `guard_alive` says
  whether the guard's heartbeat was still fresh at the release (a guard can die during the
  run), and when it was not, the detail says to delete the pod by hand. The report records the flag
  (`plan.no_hold`) and the release (`guard_release`). A refusal or a red bootstrap leaves
  the guard alone, so the pod stays up for a fix and a rerun. It is
  refused under a launch token: the pod timer reads the early exit as `completed-early`.

**Without `--no-hold`, it holds only for a finished full run.** A selection ending before Armarium records
`selection-complete` and returns at once so the pod timer closes the card. A held
selection ending before Armarium also closes promptly, and so does a full run held
before its export (a held Attestatores or Recensor), since it waits for a person, not
for the card. A full `complete`, or a `held` run whose orchestrator says in this
invocation's own stop record (`--stop-record`, read by `read_stop_record`) that
it reached a sealed export, holds toward the hard deadline (paid idle time); an export
an earlier pass left in the run tree never counts. It holds
because the pod timer
treats an early exit as non-green. The hold does no work and touches no keep-alive, so the
pod guard's idle ladder warns, and with `ladder_delete = "on"` deletes the pod after two
idle hours, which ends the hold. A run with no hard deadline
(`VERBATUS_HARD_DEADLINE=none`) cannot hold, so it is refused without `--no-hold`;
`held_to_hard_deadline` records the choice to hold, and the last tick in the `-hold.json`
record below says when the hold ended. After
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
- `-timings.json` — append-only JSON lines, one entry per stage invocation (run id,
  member, start, finish, duration, exit code, GPU use, Perlector concurrency, commit). A
  torn final line is skipped when read. It is outside the run tree because the tree is pinned
  byte-identical across reruns and restores and a clock is not. `run.json` names only the
  commit that created the run; a resume at another commit shows here. (Binding the commit
  into the run authority would refuse every resume after a fix.)
- `-hold.json` — the hold line after a finished run.
- `-estimate.json` — the current stage's finish estimate, rewritten each liveness tick
  (`finish_estimate.py`): pages done of pages total for the first page-counted stage
  (Door to Perlector) with no seal, and its pace since `pod_run` first saw it, once at
  least five pages and ten minutes have passed. The Attestatores total counts only the
  roster's page witnesses. It is that stage's finish only, never the run's; the stages
  after it are not counted. When that finish plus 20 minutes to bring results home passes
  the deadline that ends the pod, it records a `deadline-at-risk` notice and, with
  `--notify`, sends it as a `decision`. That deadline is the guard's `deadline-<pod id>`,
  read each tick the way the guard reads it (a value that is not epoch seconds within a
  week is ignored, recorded, and the last valid one stands); with none ever read, the
  bootstrap's hard deadline; under the pod timer, the timer's hard deadline, which no
  file moves. The notice names the spend policy's soft and hard maximums as the launch sealed them
  into the pod's environment, or, for a pod launched without them, the checkout's
  `config/spend.toml` by its SHA-256, the finish
  and deadline with how far off they are, the extra time and its cost at the hourly price
  (`--hourly-usd`, or a pod-timer launch's `VERBATUS_POD_HOURLY_USD` plus
  `VERBATUS_VOLUME_ONGOING_HOURLY_USD`, the launch-time estimate before create, which the
  notice names as such; "unknown" without either), and, for a guard
  deadline, one command that moves it to the projected end but never past the hard
  maximum, counted from the instant the start command recorded as
  `.pod_guard/created-<pod id>` and stated as a clock time. When the projected end passes
  the hard maximum it says so and offers only the hard maximum; without that instant or a
  budget it says the command is unchecked. Under the pod timer it says
  the deadline cannot be extended by hand. It is sent once for each deadline value; a new
  value the lead writes re-arms it. A send that did not arrive (or found no guard topic
  yet) is recorded and retried on later ticks, three attempts in all. Nothing here moves
  the deadline. The run report's `deadline_watch` keeps every notice, every ignored
  deadline file value, and any failure to write or compute the estimate; a failed tick
  is also written to the estimate file.
- `-progress.json` — whether the stage in progress keeps its pace, rewritten each liveness
  tick (`progress_watch.py`, schema `pod-run-progress.v1`) from the same run-tree sample
  as the estimate. A page-counted stage is `slow` when its pages over the last ten
  minutes fall below half its expected rate, and `stalled` when no new page has come for
  max(10 min, 3 expected page times), or, before its first page, for its engine's startup
  timeout plus three page times (30 min when that is not known). The expected rate is a
  planning value, not a measurement: the Perlector's `planned_seconds_per_page` over the
  calls its row serves at once (`max_num_seqs`; a capacity plan's wider launch only makes
  it finish sooner than planned), Surya's `seconds_per_page` over its
  runner processes (`workers`); any other page stage is held to its own pace once it has
  five pages and ten minutes. The stage is the one the transcript says started and has
  not ended, or else the page stage the estimate counts. A stage not counted in pages is
  `stalled` after 15 minutes (not yet measured against a real stage) with no new
  transcript output and no record published in its run tree outside the `serving-logs`
  directories (judged by directory times, since every record is linked or renamed into
  place, so the files themselves are never stat'ed one by one). The record keeps each check, any finding, the last moment the run was
  `ok`, and `stage_rates`: each page stage's pages done and total, the seconds `pod_run`
  watched it and its pages a minute, which the final report keeps too.

These are best-effort, so a lost stopwatch never abandons or holds a completed run. The
report audits them at close (`records_at_close`, `records_missing`); a missing transcript
or liveness record still holds the run for review.

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

`pod_run --notify` adds a fourth, the one question among them: a run on the pod that
stops with more of its pages held than its sealed review policy allows, or exports past
that stop on a person's advance, sends the systemic alarm as a `decision`
(`notify_systemic`), the line `verbatus --notify run ...` sends for a run on this computer.
`pod_run` reads the alarm from its invocation's stop record (`systemic`) and records the
line and the notification outcome in its run report. If the orchestrator ran but left no
usable stop record (`read_stop_record`), whether it sounded the alarm is unknown. The
report then names why in `stop_record_problem` and `detail`, and such a run is never
`complete`: a complete exit is recorded as `held`, which returns at once like any run
that held before its export. The deadline-at-risk notice (`-estimate.json` above) is
another `decision` sent the same way (`notify_deadline_at_risk_from_guard`). Without
`--notify` each line is recorded and nothing is sent. `notify.sh` reads its topic only from `NTFY_TOPIC` or the
repository's `private/ntfy.conf`, which a pod does not have, so `pod_run` reads the
guard's topic file (`/workspace/private/.pod_guard/ntfy_topic`, below) and passes it as
`NTFY_TOPIC` in that one notification command's environment, beside only `PATH` and the
proxy and CA variables it needs (`notify_hooks.guard_topic`, `notify_environment`). Like
the guard, it removes every space, carriage return and newline before checking the topic. The
topic is never an argument, a log or report line, or part of the orchestrator's or a
stage's environment. With no readable topic file, a link or anything but a regular file there, nothing runs at all, so `notify.sh` never falls back to a topic of the checkout's; the report says "not sent (no usable guard topic)".

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
- **Spend policy.** Paid paths refuse unless `config/spend.toml` is a configured policy
  the project lead reviewed. `billing_cutoff_margin_seconds` must lie in
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

### Transfer, bootstrap and preflight

`transfer.py` carries sealed submission-manifest rows through a generic storage seam,
verifying SHA-256 and size before and after upload and never overwriting conflicting bytes.
`bootstrap.py` journals idempotent exact-commit, locked-`uv`, transfer, chair-cache and
preflight steps.

`preflight.py` measures CUDA, driver, capability, VRAM and disk, selects a plan from
`config/pod_placement.toml` (prebuilt profiles for rented cards, computed otherwise),
verifies each chair the pod's selected stages use, and checks a stochastic proof-page read.
**Serving is sequential**: one model at a time on a card (the Coniector adopts the
Perlector's running server instead of loading the 27B again). Tiers differ in memory
fraction, context cap, pixel cap, batch size and `planned_batch_ceiling`: PREFLIGHT derives
a capacity plan from the measured card (`operations/serving/capacity.py`), which sets how
many sequences each chair runs at once, up to the tier's ceiling, and smoke-reads each
chair at that width. **`assembly_proven` is derived, never declared**: true only when a real driver
read the card (`GpuProfile.measured`) *and* a chair read the golden page back through an
engine that served it (`SmokeResult.served_by`). Both carry an opaque module-private token
minted only by the `nvidia-smi` probe and the serving evidence path and refused from any
caller, so the claim cannot be set from outside.

A chair whose serving row is `kind = "subprocess"` (Surya, run by stage 2 on the CPU) is
never served through an engine: preflight verifies the chair's weights against the
pinned manifest, then runs the chair's own runner once on the golden page, on the CPU
(`preflight.check_subprocess_environment`). A broken environment or bundle goes red with
the sync command as the remedy, and the versions, CPU instruction set and machine the
run measured go in the report's `subprocess_receipts`. Bootstrap's UV_ENVIRONMENT step
builds that environment right after the project's own, with
`uv sync --locked --project operations/serving/surya`, when the checked-out catalogue
has a subprocess row for a chair the roster configures and the pod's selected roles
include, or whenever the model store still lacks Surya's bundle, and counts its 14 GiB,
with the bundle CHAIR_CACHE copies, in the container disk it checks first. The store fetches the bundle only when a chair the pod
prepares needs it, so a pod whose stages never run Surya neither syncs that environment
nor fetches the bundle (a bare `bootstrap_main` run prepares every chair). The MODEL_STORE step then fetches Surya's
weight bundle onto the network volume by running `operations/serving/surya/prefetch.py`
in that environment, and refuses it unless its measured manifest is the pinned one, so
MODEL_STORE needs that environment synced first. The CHAIR_CACHE step copies the
verified bundle to where the real roster binds it, `config/real-models/designator_surya`
on container-local disk (`operations/serving/surya/README.md`, "On the pod").
`pod_run` counts Surya among the Designator's chairs: a selection that runs the Designator with Surya configured is
refused unless the preflight report places Surya as a subprocess, verified its cache and
carries its golden-page run in `subprocess_receipts`.

## The pod guard: every pod watches itself

`pod_guard.sh` runs on the pod and watches that same pod, so a crashed session, a closed
app or a sleeping Mac cannot leave a pod billing unnoticed. It needs nothing from the
laptop or a Claude session. What it may delete is the lead's choice, in
`config/spend.toml`, and both switches are committed off:

- **`pod_budget`.** Off, the pod has no deadline unless the lead starts it with a number
  of hours, and no backstop. On, the deadline sits at the soft maximum and a backstop
  deletes the pod at the hard maximum (below).
- **`ladder_delete`.** Whether the idle ladder's last step deletes the pod. Off, the guard
  only warns.

**The deadline.** When there is one, the guard deletes the pod when it passes, whatever
the run is doing. A deadline more than a week out is taken as a typo and ignored; the
phone hears once of each value ignored. A guard started without a deadline (`off`)
honours one written to the deadline file later, by the lead or by `pod_run --no-hold`.
A value already in the file when it starts is left from an earlier start of the same pod
and is not honoured; the guard says so in its log and on the phone.

**The idle ladder.** The pod is idle while it does no work: no GPU use (under 5 % at
every one-minute sample; a GPU that cannot report counts as busy), no container CPU use
(under half a core, from the container's own cgroup, not the shared host's load), no
download (under 256 KB/s received), and no touch of the pod's keep-alive file. While a run
is going, `pod_run`'s own verdict replaces those counters: a `progress-<pod id>` line
written in the last five minutes (below) that says `ok` is work, even when the counters
read idle, and one that says anything else is idle time counted from its last `ok`, even
when the GPU is busy. Only a `stalled` line can reach the delete step: a `slow` stage is
still making pages, so it gets the warnings and the backup but is never deleted, and the
guard says so instead (a `bootstrapping` line likewise). A line that is older, garbled or
missing leaves the counters to decide, and they can reach the delete as before. As idle time grows the guard:

| Idle for | Step |
| --- | --- |
| 15 min | one warning notice |
| 30 min | an urgent notice (ntfy `Priority: urgent`), repeated every 10 min |
| 1 h | copies the paths named in `backup-<pod id>` to `/workspace/private/runs-guard-backup/<name>-<epoch>/` and checks each copy with `diff -rq`; with no such file it logs "nothing to back up", and a listed path that is missing counts as a failed backup |
| 2 h | deletes the pod, only with `ladder_delete = "on"`, only when the backup verified or there was nothing to back up, and never while the run's progress line says `slow` or `bootstrapping`; otherwise one more urgent notice says why it will not |

Any work, or a touch of the keep-alive, starts the ladder over, with a "work resumed"
notice if a warning had gone out. The latest step is in `alert-<pod id>` as one line,
`<epoch> <step> <detail>`. The steps are `POD_GUARD_WARN_SECONDS`,
`POD_GUARD_URGENT_SECONDS`, `POD_GUARD_URGENT_REPEAT`, `POD_GUARD_BACKUP_SECONDS` and
`POD_GUARD_DELETE_SECONDS` in the guard's environment; `POD_GUARD_DELETE=on` is
`ladder_delete`, which the start command passes in. `pod_run` writes `backup-<pod id>`
from its start: the run's local tree and its volume run directory, each listed once it
exists, and removes the file once the run has finished and its final sync, if any, went
through. A run whose final sync failed leaves it, so the guard copies the local tree that
never reached the volume.

`pod_run` touches the keep-alive file on every liveness tick on which its progress check
(`-progress.json` above) says `ok`, and writes the check's verdict to
`progress-<pod id>` as one line, `<epoch now> <epoch last ok> <ok|slow|stalled> <check>
<detail>`. CPU time is not progress, because an idle model server in the run's process
tree uses a little on every tick, and its engine log keeps growing too. The bootstrap and
the final volume sync have no liveness tick, so a thread writes the line through both:
`bootstrapping bootstrap <step> for <seconds>` and `ok final-sync`. Either counts as work
for an hour per step; after that the line stops moving its last `ok` (the sync's says
`slow`) and the ladder climbs through its warnings and backup, but neither line reaches
the delete, so a long copy is never cut off mid-way. When the run ends `pod_run` removes the line and the
counters decide again. `pod_run` sends no notice of its own about a slow or stalled run;
the guard's ladder does. An unreadable CPU counter never
deletes a pod. If it cannot be read from the start, or stays unreadable, the guard counts
the pod as busy, keeps trying the read every minute, and sends one notice ("CPU idle
detection unavailable on <pod>; held until its deadline <time>", or "counted as busy, and
no deadline is set"), and one more if the read comes back, after which idle counting
resumes. A counter that keeps dropping out and coming back reaches the phone at most once
an hour; every episode is still in `guard.log`. A single missed read between good ones is
skipped: it neither adds idle time nor resets it, and the next good read is judged over
both ticks.

To delete, the guard uses RunPod's documented self-stop route: every pod has `runpodctl`
and a pod-scoped `RUNPOD_API_KEY`. It keeps asking until the pod is gone, falls back to
stopping it, and never deletes a network volume.

Arm it at creation through the pod's start command, so it runs even if SSH never comes
up. `pod_start_command.sh <hours|off> <sha>` prints that command: it fetches the guard
from this public repository at a pinned commit (ten tries, 30 s apart), starts it, and
then hands over to the image's `/start.sh`. It reads `pod_budget` from the checkout's
`config/spend.toml`, or from `VERBATUS_POD_BUDGET` when that is set:

- **Budget off, `off`:** no deadline file (one left by an earlier start of the pod is
  removed) and no backstop.
- **Budget off, `<hours>`:** a deadline `<hours>` from container start, and a backstop that
  deletes the pod an hour after the deadline (the one the guard keeps on the volume, so
  extensions count) even if the guard never ran. The hard maximum is not read.
- **Budget on, `<hours>`:** as above, and the backstop also deletes the pod at the hard
  maximum. The hard maximum is the sealed `VERBATUS_HARD_MAX_SECONDS` when set, else
  `hard_max_seconds` in the checkout's `config/spend.toml`; it counts from when the
  command is printed, just before the create, so print a fresh command for every pod, on
  a laptop whose clock is set automatically. At container start the guard's window is cut
  to end two minutes inside the hard maximum, so the guard's orderly delete comes first
  and the backstop is the fallback. The command refuses `off`, `<hours>` past the hard
  maximum, and refuses outright when the hard maximum cannot be read.

It always refuses `<hours>` of zero, and a `pod_budget` or `ladder_delete` it cannot read:

```sh
START=$(sh operations/pod/pod_start_command.sh <hours|off> <sha>) &&
runpodctl pod create ... --volume-mount-path /workspace/private --docker-args "$START"
```

The `&&` matters: the script exits 2 and prints nothing when it refuses, and an inline
`--docker-args "$(...)"` would let the create run anyway, with no guard.

(`runpodctl create pod ... --args` in runpodctl releases before `pod create`.) `<hours>` is
the approved window, or `off` for none, and `<sha>` a commit on `main` that carries the
guard. The network volume must be mounted at `/workspace/private`, the one path the
bootstrap and the data gate accept (`models.POD_VOLUME_MOUNT_PATH`). The start command
exports `POD_GUARD_DIR=/workspace/private/.pod_guard` to the guard and reads it in the
backstop, so the guard's deadline and log sit on the volume and survive the pod, even when
the guard is fetched from an older commit.

- **A long quiet wait that is still wanted** (no GPU, CPU or network use) touches
  `/workspace/private/.pod_guard/keepalive-<pod id>`, which starts the ladder over.
- **A deadline, or more time:** write the deadline (epoch seconds) to a temporary file and
  move it over `/workspace/private/.pod_guard/deadline-<pod id>`; the guard and the
  backstop, when there is one, both read it. A deadline moved earlier ends the pod on the
  guard's next tick; that is what `pod_run --no-hold` does. A pod that was stopped and is
  started again with `<hours>` keeps its old deadline file, so write a new one when the
  lead approves more time; started with `off`, the start command removes it.
- **Records:** `/workspace/private/.pod_guard/guard.log`; with a topic in
  `/workspace/private/.pod_guard/ntfy_topic` it pings at each ladder step, for each
  ignored deadline value, and when it deletes, fails to delete, or has to stop the pod
  instead. Before `pod_run --no-hold` moves the deadline it writes `released-<pod id>`
  there with the run id and its outcome, and the guard quotes it:
  `... requested deletion (approved time is up; pod_run reported: run <id> ended complete)`.
  The guard clears that file when it starts, so a restarted pod never quotes an old run.
  A ping without `pod_run reported` means the pod ended without `pod_run` finishing: the
  window ran out, the ladder deleted an idle pod, or the run was lost mid-stage. Either
  way the run's own state is in `pod-run-report-<run id>.json` on the volume.
- **Heartbeat:** the guard touches `/workspace/private/.pod_guard/heartbeat-<pod id>` on
  every tick, so a reader can tell a running guard from a deadline file nobody watches.
- **Arming the ping.** The topic is the bearer secret `operations/notify/README.md`
  describes; it never enters git, a command line or a note. Send it over SSH's standard
  input from the laptop's ignored `private/ntfy.conf`:

  ```sh
  sed -n 's/^NTFY_TOPIC=//p' private/ntfy.conf | tail -n 1 | tr -d "\"'" |
    ssh -p <RUNPOD_TCP_PORT_22> root@<RUNPOD_PUBLIC_IP> 'umask 077 && mkdir -p /workspace/private/.pod_guard &&
      cat > /workspace/private/.pod_guard/ntfy_topic'
  ```

  The guard reads it on every ping, so it can be written after the pod starts. It stays
  on the volume for later pods; delete it with the volume, or by hand when the topic is
  rotated. Use the pod's direct port (TCP port 22 exposed; `$RUNPOD_PUBLIC_IP` and
  `$RUNPOD_TCP_PORT_22` inside the pod), never `ssh.runpod.io`: that proxy ignores the
  command and runs standard input as a shell, so the topic would be typed into it.
- **Not yet observed on a live pod:** how RunPod passes `--docker-args` (the start
  command) to the container (runpodctl 2.14.0 sends it as the pod's start command, which
  RunPod documents as replacing the image's CMD; what happens with an image ENTRYPOINT is
  not documented), whether `RUNPOD_POD_ID` is in PID 1's environment, the
  image's `/start.sh`, whether the pod-scoped key may delete its own pod, and the cgroup
  and `nvidia-smi` readings inside the container. The first pod after this change is
  created on the smallest card with a one-hour window, and its guard log and deletion are
  checked before any longer run relies on it.
- The guard is a backstop, not the shutdown: close pods yourself when work ends and verify
  the close against RunPod's own state and billing. `session_end_pod_check.sh`, a Claude
  Code SessionEnd hook, pings the lead with every pod that still exists, in any state, when
  a session closes. A missing `runpodctl`, a failed listing or a table it does not
  recognise is pinged too, never read as "no pods", and so are an empty listing and one
that does not answer within 30 seconds. The same report (pod ids and states)
  is not sent twice within two hours, and is recorded only once delivered.

## The hand route: a proof run started by hand

The project lead starts a run from the laptop with `runpodctl` and SSH, without the pod
CLI, lease, supervisor or pod timer. Every paid run so far (2026-10-06 to 2026-10-08) took
this route. The pod guard is then the only thing that ends the pod, so it is armed at
creation. **The card, the hours and the spend are the lead's decision**; this section names
what the code needs, not what to rent.

**What the run needs.** The 27B Perlector needs the `generic-80gb-plus` tier in
`config/pod_placement.toml`; its one reviewed card is `NVIDIA RTX PRO 6000 Blackwell
Server Edition` (96 GB; the price is read on the day, `operations/pod/RUNPOD.md`). Container disk at least 120 GB on that card
(`models.BIG_CARD_CONTAINER_DISK_GB`) and 100 GB on a smaller one
(`models.DEFAULT_CONTAINER_DISK_GB`). The network volume mounted at exactly
`/workspace/private`. A card serves its chairs one at a time, and the work is split across
cards: a 24 GB witness card runs Door through Attestatores, the big card runs the Perlector
and then the Coniector on the same server, and Recensor onward needs no GPU ("Two cards"
below). PREFLIGHT's capacity plan sizes each chair's width on the card it measured.

### Set up the Mac (free)

The Mac needs macOS 13 (Ventura) or later, Intel or Apple silicon: the PDF library
(pypdfium2) ships wheels only for macOS 13 and later (`sw_vers -productVersion; uname -m`).
git needs the Xcode Command Line Tools (`xcode-select --install`). Their `python3` is too
old for this repository, so run Python only as `uv run …` or `.venv/bin/python …`; uv
builds `.venv` on the interpreter `.python-version` names. uv must be exactly the version
`pyproject.toml` requires (`[tool.uv] required-version`, 0.12.1 today; `uv self update
0.12.1` moves an older one). From a fresh clone:

```sh
curl -LsSf https://astral.sh/uv/0.12.1/install.sh | sh
git clone https://github.com/MegaSlick/Apparatus-Verbatus-Alpha
cd Apparatus-Verbatus-Alpha
uv sync --frozen --group test --group audit
sh .githooks/install.sh
sh .githooks/check-static.sh
source .venv/bin/activate          # once per Terminal window; puts `verbatus` on PATH
verbatus spend show
```

`runpodctl` 2.x (`brew install runpod/runpodctl/runpodctl`; `runpodctl version`) needs the
account's API key, and `upload` and `fetch-run` need the RunPod S3 keys. They go in the
shell only, never in a file here:

```sh
read -rs RUNPOD_API_KEY; export RUNPOD_API_KEY
read -rs RUNPOD_S3_ACCESS_KEY; export RUNPOD_S3_ACCESS_KEY
read -rs RUNPOD_S3_SECRET_KEY; export RUNPOD_S3_SECRET_KEY
runpodctl gpu list
```

`runpodctl config --apiKey <key>` instead stores the key in `~/.runpod/config.toml`,
outside the repository, but also leaves it in the shell's history; prefer the variable.

### Before renting (free)

1. The page images are on the volume. Seal and send them from `private/` (clear Finder's
   `.DS_Store` files first: the Door refuses them):

   ```sh
   verbatus --state-dir private/verbatus-state upload --source private/<pages> \
     --manifest-out private/<pages>-manifest.json
   verbatus upload --source private/<pages> --sealed-manifest private/<pages>-manifest.json \
     --network-volume DATACENTER:VOLUME_ID
   ```

   The first seals the folder and copies it to a local folder only, which a local
   `verbatus --state-dir private/verbatus-state run --run-id <id>` can check at the Door for free
   (`operations/operator/README.md`, "`run`, `export` and holds"); the second writes
   `submission/` and `submission-manifest.json` at the volume root. A second sealed set on
   the same volume needs its own `--prefix` (`--prefix spreads` writes `spreads/` and
   `spreads-manifest.json`). Prepared spreads go with their triage documents
   (`operations/operator/README.md`, "Sending prepared scans to a pod").
2. **Prove the S3 path home.** With the two storage-key variables `upload --network-volume`
   uses set in the laptop shell, run

   ```sh
   verbatus fetch-run --run-id s3-path-check --into /tmp/verbatus-s3-check \
     --network-volume DATACENTER:VOLUME_ID
   ```

   It lists the volume and needs no pod. The expected answer is a refusal naming
   `nothing is stored under 'runs/s3-path-check/'`: the listing worked and the path is
   empty. Any other failure (credential, endpoint, datacenter) is fixed before renting.
   If an earlier run's tree is on the volume, fetch that run id instead for a full proof.
3. Pick `<sha>`: the full 40-character commit on `main` carrying this runbook,
   `pod_start_command.sh` and `pod_run --no-hold`. The pod checks it out, and the guard is
   fetched at it; a short hash arms the guard and then fails the bootstrap.

### Create the pod with its guard armed

From a checkout at `<sha>` on the laptop:

```sh
START=$(sh operations/pod/pod_start_command.sh <hours|off> <sha>) &&
runpodctl pod create \
  --name verbatus-<run id> \
  --image runpod/pytorch:1.4.0-cu1300-torch2130-ubuntu2404 \
  --gpu-id "NVIDIA RTX PRO 6000 Blackwell Server Edition" --gpu-count 1 \
  --cloud-type SECURE \
  --data-center-ids <DATACENTER of the volume; one id only> \
  --network-volume-id <VOLUME_ID> \
  --volume-mount-path /workspace/private \
  --container-disk-in-gb 120 \
  --ports "22/tcp" \
  --docker-args "$START"
```

With the budget off (`pod_budget = "off"`, as committed), `off` starts the pod with no
deadline: the guard only watches, the idle ladder warns, and someone deletes the pod by
hand when the run is home (or `--no-hold` releases it). `<hours>` instead sets a deadline
the guard deletes the pod at whatever the run is doing, with a backstop an hour later.
With the budget on, `<hours>` is required and is the approved window, at most the soft
maximum in `config/spend.toml` (2 h in the lead's budget), and the backstop also deletes
the pod at the hard maximum (3 h from creation) even if the guard never started.

The image must carry CUDA 13.0: on a Blackwell card FlashInfer compiles its sampling
kernel at the first engine start and needs `nvcc` 12.9 or newer. A `cu1281` image fails
every vLLM chair with `FlashInfer requires GPUs with sm75 or higher` (observed
2026-10-06). The `cu1300` image booted on a PRO 6000 with
`nvcc` 13.0 and needed nothing by hand (2026-10-07).
`runpodctl pod get <pod id>` shows the SSH details. The `runpodctl` lines here need a
current CLI: v1.14.3 has no `pod` subcommand. The RunPod API's pod create takes the same
fields (`args` for the start command, `mounts.network` for the volume, `startSsh`).

**Observed 2026-10-07:**

- **The SSH proxy** (`ssh <pod>-<suffix>@ssh.runpod.io`) ignores a command on the line
  and opens a shell; it takes commands only on stdin, and `scp` cannot use it. Inside the
  pod, `$RUNPOD_PUBLIC_IP` and `$RUNPOD_TCP_PORT_22` name the direct port, which takes
  commands and `scp` (`ssh -p <port> root@<ip>`), even when the pod record's
  `ssh.direct` is empty.
- **No card where the volume is.** A network volume lives in one datacenter, and the PRO
  6000 may have no stock there. A pod with its own disk (`mounts.persistent`, 200 GB, at
  `/workspace/private`) passes the bootstrap's mount check and fetched the models in about
  2 minutes. **That disk is deleted with the pod.** Launch such a pod *without*
  `--no-hold`, copy the results home the moment the run ends, then delete the pod: with
  `--no-hold` the guard deletes it, and every result, within a minute of the run ending
  (lost that way once, 2026-10-07).

### A global volume for the results (any datacenter)

A RunPod **global volume** is object storage that any datacenter can mount, so a pod can
go where its card has stock and still leave its results behind. Only the GraphQL create
(`podFindAndDeployOnDemand`, `objectMounts`) can attach one; `runpodctl`, the connector
and both REST routes cannot, and no API lists them (the id is on the console's Storage
page, in the lead's private note). Object storage has no permission bits, no atomic
rename and no locking, so **it holds only a run's final copy**: the guard's records, the
model store, caches, the clone and the venv stay on the pod's own disk at
`/workspace/private` (`--disk-gb`, deleted with the pod), which passes the same mount
check a network volume does. `operations/pod/create_pod.py` is the hand route's create
for this; `operations/bakeoff/RUNBOOK.md` step 2.1 is the worked example, with a
two-minute smoke on the cheapest card first. The managed route takes the same volume as
`global_volume` in its request file; its v1 adapter then creates through the same v1
GraphQL mutation (`provider_runpod`), and v2 refuses by name.

### On the pod, over SSH

```sh
findmnt /workspace/private                  # the network volume, not a plain directory
tail /workspace/private/.pod_guard/guard.log # "armed for pod <id>: deadline ..."
echo "$RUNPOD_POD_ID"                       # must print the pod id; see below if empty
cat /workspace/private/.pod_guard/deadline-$RUNPOD_POD_ID   # the deadline, epoch seconds; none with `off`

git clone https://github.com/MegaSlick/Apparatus-Verbatus-Alpha /opt/verbatus
cd /opt/verbatus && git checkout --detach <sha>
bash operations/pod/prepare_runtime.sh
UV_CACHE_DIR=/tmp/verbatus-uv-cache uv sync --frozen
```

**No "armed for pod" line for this pod, or no deadline file when the pod was started
with `<hours>`: stop.** At most the backstop is watching, an hour after the window, and
with `off` nothing is. (Started with `off`, the line reads `deadline none (off)` and there
is no deadline file.) Delete the pod now (`runpodctl pod delete <pod id>`),
confirm it is gone, and find out why before renting again. If `RUNPOD_POD_ID` is empty in
the SSH shell, take it from the container's first process, the same place the guard got it:
`export RUNPOD_POD_ID=$(tr '\0' '\n' </proc/1/environ | sed -n 's/^RUNPOD_POD_ID=//p')`.
Never type an id in by hand. `pod_run --no-hold` never trusts the shell's value (a
different one is refused) and releases only the first process's pod; when that has none, or this pod's guard heartbeat is stale, it
refuses before the bootstrap. Then launch without `--no-hold` (which needs a deadline):
the guard's idle ladder still watches the pod.
Arm the guard's completion ping now if wanted ("Arming the ping" above).

Then launch detached, so a dropped SSH session or a sleeping laptop cannot kill it. The
bootstrap's hard deadline is taken from the guard's own; a pod started with `off` has
none, which is said as `VERBATUS_HARD_DEADLINE=none` and accepted only with `--no-hold`:

```sh
V=/workspace/private RUN=<run id> R=/opt/verbatus
GUARD_DEADLINE=$(cat $V/.pod_guard/deadline-$RUNPOD_POD_ID 2>/dev/null)
if [ -n "$GUARD_DEADLINE" ]; then
  VERBATUS_HARD_DEADLINE=$(date -u -d "@$(( GUARD_DEADLINE - 300 ))" +%Y-%m-%dT%H:%M:%SZ)
else
  VERBATUS_HARD_DEADLINE=none
fi && export VERBATUS_HARD_DEADLINE &&
cd $R && setsid nohup $R/.venv/bin/python -m operations.pod.pod_run \
  --report-path $V/pod-run-report-$RUN.json \
  --run-id $RUN \
  --submission-folder $V/submission \
  --submission-manifest $V/submission-manifest.json \
  --mechanics-qualification \
  --no-hold \
  --notify \
  --hourly-usd <the card's price per hour plus the volume's> \
  -- \
  --volume-mount-path $V \
  --report-path $V/bootstrap-report-$RUN.json \
  --repository $R \
  --repository-commit <sha> \
  --lockfile $R/uv.lock \
  --journal $V/bootstrap-journal-$RUN.json \
  --store-root $V/model-store \
  --models-config $R/config/models-real.toml \
  --serving-recipes-config $R/config/serving_recipes_real.toml \
  > $V/pod-run-$RUN.out 2>&1 < /dev/null &
```

- **`--hourly-usd`** names the price the pod was rented at, so the deadline-at-risk notice
  can say what running past the deadline costs; `--notify` sends that notice (and the
  systemic alarm) to the phone once the guard's ping is armed (`-estimate.json` above).
  Drop `--notify` if no phone should be paged.
- **No selection: the full auto run.** It stops at a held Recensor, before Archetypus
  and Armarium, as `--models big` does; a first proof run should reach the Armarium.
- **`--store-root`** names the model store on the volume. If the weights were
  materialized under another root on this volume, name that one; a new root downloads
  the weights of the chairs this pod's stages use (every chair's, with no selection)
  during the paid bootstrap.
- Add `--perlector-protocol-config <path in the repository>` to choose the Perlector's
  protocol; omitted, the orchestrator's default.
- **Another prefix.** For a set uploaded with `--prefix spreads`, use
  `--submission-folder $V/spreads --submission-manifest $V/spreads-manifest.json`. For
  prepared spreads, also name their triage documents:
  `--triage-decision-manifest $V/<prefix>-triage-decision-manifest.json
  --triage-producer-recipe $V/<prefix>-triage-producer-recipe.json`; the Door then cuts
  each page from its original as `prepare` decided.
- **Two cards.** The split runs the same run id, submission and `--store-root` on both:
  the witness pod adds `--models small` before `--` (Door through Attestatores, on a 24 GB
  card with `--container-disk-in-gb 100`), and the big pod adds `--from perlector --to
  coniector` ([`pod_run.py`](#pod_runpy-running-the-pipeline-on-a-pod)). Recensor onward
  runs off the GPU, on the Mac or a CPU pod, from the fetched tree.
- **One card, released after the Coniector.** Add `--stop-after-coniector` before `--`:
  the run ends at the Coniector and the pod closes; then fetch the tree and run Recensor
  through Armarium on the Mac ([above](#pod_runpy-running-the-pipeline-on-a-pod)).
- The file names carry the run id so a second run on the same volume cannot overwrite
  them. A second pod on the same run id may reuse these exact paths: the journal records
  which pod wrote it, so the same pod resumes it, while another pod (a replacement for a
  dead one, or the big card after the witness) renames it to
  `bootstrap-journal-$RUN.pod-<old pod id>.json` and bootstraps afresh for its own GPU.
  A gated Hugging Face model needs its token in the environment and
  `--keep-env HF_TOKEN` in the bootstrap half, never on the command line.
- A refusal or a red bootstrap leaves the pod up: read the report, fix, and launch
  again. The guard's ladder warns after 15 idle minutes, and deletes the pod after two
  idle hours only with `ladder_delete = "on"`.
- **Nothing stops the run before the guard's deadline, when there is one.** Under
  `--no-hold` the hard deadline only satisfies the bootstrap. If the window runs out mid-run, the guard deletes
  the pod with the stage in flight: that stage's work is lost, the report still reads
  `running`, and the transcript keeps only its head. Size `<hours>` with margin, and when
  the lead approves more time, write the new deadline file before the old one passes.

### Watching it

From the Mac, `verbatus watch` reads copies of the report files; it fetches nothing, so
copy fresh ones in a loop over the pod's direct SSH port (the `ssh.runpod.io` proxy cannot
carry `scp`):

```sh
mkdir -p ~/verbatus-watch
while :; do
  scp -q -P <RUNPOD_TCP_PORT_22> "root@<RUNPOD_PUBLIC_IP>:/workspace/private/pod-run-report-<run id>{.json,-liveness.json,-timings.json,-estimate.json,-progress.json}" ~/verbatus-watch/
  verbatus watch --run-id <run id> --receipts ~/verbatus-watch
  sleep 60
done
```

`operations/operator/README.md` ("`watch`") says what each line means. On the pod:

```sh
cat $V/pod-run-report-$RUN.json          # state: bootstrapping, running, then the outcome
cat $V/pod-run-report-$RUN-liveness.json # last_seen should keep moving while it runs
cat $V/pod-run-report-$RUN-estimate.json # this stage finishes about ...; at_risk
cat $V/pod-run-report-$RUN-progress.json # ok, slow or stalled, and why
tail -f $V/pod-run-report-$RUN-transcript.log
tail -f $V/pod-run-$RUN.out              # the bootstrap's own output as well
tail -f $V/.pod_guard/guard.log
```

A long quiet wait that is still wanted (no GPU, CPU or network use) touches the
keep-alive file above, which starts the idle ladder over; otherwise the phone hears at
15 and 30 minutes, and with `ladder_delete = "on"` the guard deletes the pod after two
hours. Reads from the network
volume may not show as network traffic inside the container, so a slow weight load at
low CPU could look idle; how the guard reads such a phase is not yet observed. More time is the lead's
decision and a new deadline file.

**The hard-failure cap.** `config/hard_failure.toml` halts the run at the next stage
boundary once more than two distinct (stage, subject) incidents carry a counted failure
(one act failing at two stages counts twice); the stage in flight finishes. It counts Door refusals for `corrupt` or `unreadable`
pages, Designator and Recensor `failed`, Archetypus `refused`, and Perlector `failed`.
A page whose Perlector call failed (transport, endpoint or engine signal) is recorded
`failed` and counts toward the cap, once per page; that includes a failed re-ask call,
whose page still stands on its first reading. A page that was cut off, refused for
capacity or did not parse is `held` and is not counted; watch held pages in the
transcript and the run tree. A halted run exits 4 and stays
halted: every stage refuses to start while the cap is breached.

### When it ends, and how results come home

With `--no-hold`, `pod_run` writes its final report, leaves the run's outcome for the
guard's ping, then moves the guard's deadline to now (or writes it, when the guard
started with none); the guard deletes the pod within
about a minute (and pings, if armed). `guard_release.guard_alive: false` in the report
means the guard's heartbeat was stale: nothing is known to be acting on the deadline, so
delete the pod by hand. Confirm it is
gone with `runpodctl pod list --all` (without `--all` it hides stopped pods) and
`runpodctl pod get <pod id>`, and check the billing in
RunPod's console: a shutdown is verified, never assumed. The volume and everything on it
remain.

Then, on the laptop, no pod needed:

```sh
verbatus fetch-run --run-id <run id> --into <local root> \
  --network-volume DATACENTER:VOLUME_ID \
  --evidence-prefix preflight/bootstrap-report-<run id> \
  --evidence-key pod-run-report-<run id>.json \
  --evidence-key pod-run-report-<run id>-transcript.log \
  --evidence-key pod-run-report-<run id>-liveness.json \
  --evidence-key pod-run-report-<run id>-timings.json \
  --evidence-key pod-run-report-<run id>-estimate.json \
  --evidence-key pod-run-report-<run id>-progress.json \
  --evidence-key bootstrap-report-<run id>.json \
  --evidence-key bootstrap-journal-<run id>.json \
  --evidence-key pod-run-<run id>.out \
  --evidence-key .pod_guard/guard.log
```

There is no launch receipt on this route, so each key is named. A run that held (not
`--no-hold`) also has `pod-run-report-<run id>-hold.json`. Then read, export and back it
up on the laptop (`operations/operator/README.md`):

```sh
verbatus review --run-root <local root> --run-id <run id>
verbatus export --run-id <run id> --run-root <local root>
verbatus backup --run-root <local root> --run-id <run id> --mac-directory <synced folder>
```

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
(`common.credentials.looks_like_credential_field`, `looks_like_credential_value`) and the launch
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
| `<volume>/pod-run-report-<token>.json` | `pod_run --report-path`; a refused run argument is recorded here, never in the bootstrap report | the nested `--report-path` the launch request carried, mount prefix stripped |
| `<volume>/pod-run-report-<token>-hold.json` | `pod_run`'s hold after a `complete` or `held` run (`Plan.hold_path`) | the pod-run report key with `-hold` before its suffix. The only record that the pod stayed alive to the hard deadline |
| `<volume>/pod-runtime-report-<token>.json` | `pod_timer --report-path` | the request's outermost `--report-path`, mount prefix stripped |
| `<volume>/.pod_guard/guard.log` | `pod_guard.sh`: when it armed, each deadline change, why and when it deleted the pod | a fixed name, no token: `--evidence-key .pod_guard/guard.log`. One log for every pod on the volume |
| `<volume>/pod-transfer-journal.json` | `ChecksummedTransfer` | **a fixed name at the volume root**, no token. The only durable record of which submission rows were verified against target-observed bytes |

The pod-run report's `-liveness.json`, `-timings.json` and `-transcript.log` siblings and
the runtime report's `-terminating.json` breadcrumb are derived the same way.
`verbatus fetch-run --launch-receipt <path>` derives every key except the transfer journal
from the saved receipt's sealed `docker_start_cmd`.

**Not records, deliberately not fetched:** the model store at `--store-root` (weights;
`<volume>/model-store/` in Boot B and the hand route),
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

It installs the official `uv 0.12.1` at `/usr/local/bin/uv`, checked against its pinned
SHA-256, and `ninja-build` at `/usr/bin/ninja` (without it the Chandra and DAI vLLM
warm-up fails). It is idempotent, with bounded, retried downloads.

Keep the repository, `.venv` and `UV_CACHE_DIR` on container-local disk. The serving stack
keeps only the active model in its container-local cache. Keep inputs, outputs,
evidence and the materialized model store on the network volume. A store on the volume
written before the roster gained an artifact (the record detector, for one) is upgraded
at boot: materialization adds each new artifact to its record as `pending-fetch` and
fetches it (Surya's bundle included), provided every artifact the store already names still matches the roster;
any other record is refused (`common/chairs/README.md`). MODEL_STORE's final verification
checks every present artifact's structure (manifest pin, licence, required and carried
files, file list, sizes, links) but reads the bytes only of artifacts no configured chair
copies and that this boot did not fetch: an artifact fetched in the same call was measured
as it was promoted, and a chair's copy into the cache hashes each byte against the same
pinned manifest. Its receipt's `store_bytes` names which artifacts were hashed at boot,
at fetch, and at copy, with the statement "bytes verified at copy, for roles ...".
With a stage selection the receipt's `selection_complete` says whether the selected
chairs' artifacts are present, which is what the step requires; `real_roster_complete` is
true only when every roster artifact was present and checked by this boot, and
`store_bytes.not_verified` names the present artifacts outside the selection.

### What the image must carry

- **A checkout; the bootstrap does not clone.** `checkout_commit` runs
  `git fetch --no-tags origin <sha>` and `git checkout --detach --force <sha>` in
  `--repository`. `boot_b_request.py` renders `/opt/verbatus` on container-local disk; the
  volume holds evidence, not code.
- **An HTTPS `origin` reachable with no HOME.** `BOOTSTRAP_ENVIRONMENT` supplies `PATH`,
  `LANG`, `LC_ALL`, `GIT_CONFIG_NOSYSTEM=1`, `GIT_TERMINAL_PROMPT=0`, and `UV_CACHE_DIR`.
  Git reads no global or system config and cannot prompt. The image check refuses includes,
  credential helpers and other external credential routes in checkout config. A private
  origin may use URL credentials or a repository-local `http.<url>.extraheader`; the pinned
  fetch proves whether the configured route works before checkout or model work.
- **Tools at absolute paths; PATH is never searched.** `git` at `/usr/bin/git`, `uv` at
  `/usr/local/bin/uv`, `nvidia-smi` at `/usr/bin/nvidia-smi`, `apt-cache` at
  `/usr/bin/apt-cache`, `apt-get` at `/usr/bin/apt-get`, and `dpkg-query` at
  `/usr/bin/dpkg-query` (`BOOTSTRAP_EXECUTABLES`). The default uv installer writes
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
| `vllm` | `0.30.0` | Apache-2.0 | The lowest release that fixes all twelve advisories pip-audit reports against 0.27.1 (listed below), and it registers every architecture the roster declares |
| `transformers` | `5.14.1` | Apache-2.0 | Above vLLM 0.30.0's `>= 5.10.4` floor and the Perlector's `>= 5.8.0`; its metadata accepts `huggingface-hub` `>=1.5.0,<2.0` |
| `qwen-vl-utils` | `0.0.14` | Apache-2.0 | Latest; it and its dependencies (`av`, `pillow`, `requests`) all publish linux x86_64 wheels, so nothing compiles on the card |

The vLLM advisories are what `pip-audit --strict --no-deps --disable-pip -r` reports for
the `uv export --frozen --all-groups --no-hashes` requirements of a lock pinning 0.27.1,
with each advisory's first fixed version as pip-audit gives it:

- fixed in 0.28.0: PYSEC-2026-3985 (CVE-2026-90553), PYSEC-2026-3997 (CVE-2026-93592),
  CVE-2026-69147 (GHSA-8pw2-6jv3-mj5j);
- fixed in 0.29.0: PYSEC-2026-3998 (CVE-2026-93840);
- fixed in 0.30.0: PYSEC-2026-3996 (CVE-2026-93436), PYSEC-2026-3999 (CVE-2026-93841),
  PYSEC-2026-4000 (CVE-2026-93989), PYSEC-2026-4004 to -4008 (CVE-2026-94622 to -94626).

The same audit over this lock reports none against `vllm`.

The full gate (`.githooks/check-all.sh`) audits this group on every run, from the lock and
for the pod's Linux x86_64 target, without installing it (`.githooks/serving_audit.py`).
One advisory is accepted there, for its exact pin only: `setuptools` 80.10.2,
PYSEC-2026-3447 (CVE-2026-59890), fixed in 83.0.0. vLLM 0.30.0, the latest release,
requires `setuptools<81`, so the lock cannot reach the fix. The flaw is in building a
source distribution on a Unicode-normalizing (macOS) filesystem; the pod installs wheels
on Linux and builds no sdist, and the project's own build uses `setuptools` 84.0.0. A lock
that moves `setuptools` ends the acceptance.

Licence sources: the `LICENSE` files at `github.com/vllm-project/vllm` (tag `v0.30.0`; the
0.30.0 wheel carries an Apache-2.0 `LICENSE` and `License-Expression: Apache-2.0`) and
`github.com/huggingface/transformers`; `qwen-vl-utils`'s PyPI metadata (maintained under
`github.com/QwenLM/Qwen2.5-VL`, Apache-2.0).

- **`transformers` 4.x cannot lock**: no 4.57.x accepts `huggingface-hub` 1.x, and the
  project pins `huggingface_hub==1.31.0`.
- **One vLLM release serves all four chairs.** Chandra-2 and the Perlector declare
  `Qwen3_5ForConditionalGeneration` (multimodal); DAI and Churro-3B declare
  `Qwen2_5_VLForConditionalGeneration` (each model's `raw/main/config.json`). vLLM 0.30.0's
  `registry.py` lists both in `_MULTIMODAL_MODELS`. The Perlector's vLLM recipe asks for
  vLLM 0.17.0+ and transformers >= 5.8.0.
- **`huggingface_hub` follows vLLM's floor**: vLLM 0.30.0 declares `huggingface_hub>=1.31.0`,
  so the project pins 1.31.0, the lowest release that satisfies it. The two calls
  `common/chairs/registry.py` makes, `snapshot_download` (`repo_id`, `revision`,
  `allow_patterns`, `cache_dir`) and `metadata_load`, keep their signatures, return values
  and snapshot layout from 1.26.0; the per-call client cache is outside the evidence tree.
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

Not used live: it is for the pod CLI's lease route, and every paid run so far took the hand
route instead.

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
  whether `pod.cost` is non-zero while `PROVISIONING`, and whether RUNNING is reported
  before or after the pod timer's arming receipt.
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
  and stamp only the measured tier's rows proven. Record the `CUDA_COMPAT` receipt and
  `nvidia-smi`'s CUDA version before `uv sync --group pod`. Record whether the sync
  completed and how long it took,
  whether each chair loaded under `vllm 0.30.0`, and per chair whether the witness was read
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

These belong to the pod CLI's lease route, which no paid run has used yet; the hand route's
runs close none of them.

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
| 04-8 | The at-most-one same-pin cache re-fetch does not ship | **Superseded.** A chair cache is filled from its pinned volume store when needed; a mismatch is named and refused, with no automatic repair attempt. |
| 04-9 | Nothing proves billing buckets cover the declared window | **Window half closed under v2** when `metadata.query` is present: the declared window is the provider's resolved one, must cover the request, and an empty answer inside it reads `pending-reconciliation`. **Still open**: whether `metadata.query` appears on the `podId`-filtered route, and whether the buckets *fill* the window, wait on a live run; a coverage check written before that would guess, and a wrong guess turns every close red. |
| 04-10 | The real serving stack could not be locked | **Closed**; see "The serving stack, re-planned and locked". |

## The boot plan: Boot A, the drill, before Boot B, the real thing

Not used live: it plans the pod CLI's lease route, and every paid run so far took the
hand route instead.

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

**What these boots need from the project lead:** the GPU class (`config/spend.toml`
carries the lead's budget); the S3 access and secret keys in the launching shell; separate
in-session permission for Boot A and for Boot B; and confirmation of the two-boot split.

## If your task seems to need one

Stop and say so, with what you would start, the hourly rate, roughly how long, what it
buys, and how it gets turned off. Then wait: that is what the project lead needs to give or
refuse permission. In an unattended session, raise it as a decision the moment you find it,
and carry on with everything that does not depend on it.
