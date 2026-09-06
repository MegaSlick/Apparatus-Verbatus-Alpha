# Paid infrastructure — pods, GPUs, and anything that bills

**Read this before invoking anything that can start a meter.** RunPod, any GPU host, any
hosted inference not already covered by the subscriptions, any storage or egress that is
charged. If your task appears to need one of these, this file is the whole rule, and
CLAUDE.md hard rules 1 and 2 and GOVERNANCE 8 are why.

## The rule

**Unless Tyrel has directed it in the current session, you do not invoke a billing
action.** Not a launch, not a resume, not a resize, not a persistent volume, not a "just
to check" call that provisions anything.

This is a gate in CLAUDE.md's sense: it authorizes **one exact action**, named with its
cost, never inferred from another permission and never carried forward from an earlier
session. Permission to run a pod on Tuesday is not permission to run one on Wednesday.
Permission to launch is not permission to resize.

**Reading costs nothing and is always allowed** — listing pods, reading status, checking
whether something is already running, reading billing. Do that freely. It answers "is
anything running right now", which is worth asking unprompted.

## Why this is stricter than every other rule here

Everything else this project guards against costs *work*. A bad diff is reviewed out, a
lost note is in a backup, a wrong branch is one command from right.

**A pod bills by the hour for as long as it exists** — awake or idle, watched or not —
and an unattended session that starts one and then dies leaves it running until a human
notices. That is the one failure here that costs money rather than time.

## Shutdown is verified, never inferred

**An acknowledgement is not a shutdown.** Nor is a command that returned zero, nor a log
line saying "terminated".

A close is green only when **provider state says the same pod is gone** (both exact-pod GET
and the pod list) and the provider returns non-empty, exact-pod billing records whose named
window begins no later than the instant the record is anchored on and reaches the cutoff
requested by close. "Provider-resolved" would overstate that cutoff: the shipped adapter
composes the capture from the values the runtime itself sent, so the window is a declared
echo until a live run proves otherwise (deferrals 04-7 and 04-9).

**That anchor is not proven to be pod creation, and this paragraph used to say it was.**
`PodRecord.created_at` is filled from RunPod's `lastStartedAt`, falling back to the
observation instant when that field is null, and the capture query is composed from the
same value — so the narrowed-window check compares a value against itself and cannot fire
for this narrowing. If RunPod posts any charge between creation and first start,
a close can read `verified` over a total that omits it. Deferral 04-7 carries this to the
first live run, where the relationship between the two instants can be observed instead of
assumed. Billing can lag; this instrument never claims “no future charges.” An empty,
unreachable, misattributed, narrowed, or stale billing response is *unverified*, not zero.

A shutdown that cannot be verified is not a tidy-up for next session. Say it now.

## What exists here

The fake-first runtime is now present. It has not started, inspected, or billed a live
pod. This build's no-live-call boundary applies even to the otherwise read-only provider
operations above: every adapter check used an in-memory fake transport, except the
redirect-refusal check, which measured urllib itself against two loopback HTTP servers
on this machine — still no provider contact. RunPod's public
*documentation* was fetched to settle which API version and which response shapes the
adapter targets — that is reading a web page, not calling the provider — and every page
is named with its date in `provider_runpod.py`. So the field names here are documented,
not observed, and the first authorised live run is what confirms the provider answers
the way its documentation says.

Before the first authorized live demonstration, use the
[first gated live-pod checklist](#first-gated-live-pod-checklist) below. It names the
observations the fake suite cannot establish and the first response-as-arrival durability
check.

- `provider.py` is the seven-verb provider seam; `provider_runpod.py` is the sole
  RunPod adapter and, as the code on this branch still stands, speaks **REST
  v1** (`rest.runpod.io/v1`) — the only route whose published pod-create schema
  proves the two facts this runtime must have before it can spend: an explicit
  `interruptible=false` and a primary `dockerStartCmd`. Every endpoint whose
  response *shape* the adapter parses is named, with its documentation URL and
  the date it was checked, in that file's module docstring; DELETE's response
  body is never parsed, only its status code, so it is not on that list.
  **This v1 posture is superseded, not settled.** The module docstring's "v2 is
  beta, so not the route" reasoning is dated 2026-08-09; Tyrel ruled two days
  later, quoted in `workbench/standing/TYREL_RULINGS_2026-08-10_SPEND.md:122-126`:
  *"V2 should be what we use."* The 2026-09-01 documentation research
  (`workbench/active/RESEARCH_RUNPOD_API_2026-09-01.md`) adds that RunPod's own
  docs banner now retires v1 on 2026-11-15. The v1 code in this file is
  therefore a session decision that **predates and contradicts** that ruling,
  left as-is by this record-only unit rather than migrated: no v2 field name is
  asserted anywhere in this repository, and writing one in without fetching
  v2's actual create-body schema would trade one unverified assumption for
  another on a money path. **That record now exists: `V2_MIGRATION.md` beside
  this file** maps every v1 endpoint, field, status code and lifecycle word the
  adapter uses to its v2 counterpart, each with the documentation page and the
  date it was read, names the ones with none, and ends with the plan the next
  unit executes. Two of its findings change the shape of that work rather than
  its schedule: the v2 create body has **no `interruptible` field and no
  `dockerStartCmd`** (only a single `args` string for the image's entrypoint),
  so the two facts the paragraph above says v1 proves are, under v2, facts the
  next unit must find another page for or put to Tyrel as a rule-9 conflict.
  No v2 code is in this tree; the v1 adapter is unchanged by the record.

  **The account-balance observer now exists, inside the adapter.**
  `GraphQLBalanceObserver` in `provider_runpod.py` POSTs the one documented
  query — `myself { clientBalance currentSpendPerHr }` — to RunPod's GraphQL
  endpoint through the same redirect-refusing, size-bounded urllib transport
  the REST verbs use, with the key placed as the `api_key` query parameter
  because that is the only form the GraphQL documentation publishes (the
  module docstring names the page and date); every error string and fixture
  record scrubs it. It is the **default** `balance_observer` whenever the
  provider is built over a live `UrllibRunPodTransport`, derived by
  `UrllibRunPodTransport.sibling` so the provider never handles the key; a
  fake transport still gets none, so the "balance source was not configured"
  refusal remains reachable offline. It refuses by name a non-200, a redirect
  (never followed), a body that is not a JSON object, a GraphQL `errors`
  array, a missing `data`, `myself`, `clientBalance` or `currentSpendPerHr`, a
  value that is not a JSON number, a negative balance, and any
  credential-shaped key anywhere in the answer — the query asked for two
  numbers. **Currency is a documented reading, not an observed one:** the
  GraphQL schema names none; the billing pages say USD and "a dollar amount";
  the v1 pod page says "Runpod credits per hour"; the observation's `source`
  says "US dollars per the vendor's billing documentation" and the first live
  run compares the figure against the console. The GraphQL route itself is
  now documented as retiring in early 2027 and neither v2 billing endpoint
  reports a balance — `V2_MIGRATION.md` §1 carries that. `fake_provider.py`
  has a fixed local price sheet, exact-token crash recovery, and injected
  failures only.
- `shutdown.py` is written before launch. Its only green close result requires a
  termination request, exact-pod GET-404, independent pod-list absence, and a
  non-empty provider billing capture through its named window and cutoff. The
  generic verifier binds all three returned observations to the requested pod,
  requires the capture to *declare* a window spanning creation through the
  requested cutoff, and refuses any dated record lying outside that declared
  window — allowing one hour of slack before the window start, for the billing
  bucket that contains the pod's creation. Whether the returned records
  actually *fill* the window is unproven — deferral 04-9 below. Empty/unreachable or mismatched billing is
  **UNVERIFIED**, never zero. Billing lags termination, so the capture is
  retried a bounded number of times — fixed in code (3 attempts, 15s apart),
  not reviewed policy — before that verdict; an adapter that can distinguish
  "not posted yet" from "nothing to post" reports `pending-reconciliation`
  instead. Neither state claims no future charge is possible.
- `lease.py`, `controllers.py`, `arming.py`, and `pod_timer.py` implement a pre-create
  atomic lease, restart recovery by exact token, laptop heartbeat/lifetime supervisor,
  mandatory bootstrap, and an independent pod-side hard-lifetime dead-man. A launch or
  adoption is green only after a supplied controller harness starts the laptop supervisor,
  observes a pod-timer acknowledgement, and that receipt is durably recorded and bound to
  the exact lease, pod, and hard deadline. The default is fail-closed: a launch that cannot
  arm both controllers closes its own pod at once, and an active lease still carrying no
  receipt once its launch owner has stopped heartbeating is closed by the supervisor. While
  that owner is still heartbeating the missing receipt is reported non-green rather than
  acted on, because the supervisor doing the looking is usually the one the launch has just
  started. Pod close makes no volume retention/deletion
  decision; either choice requires separate authorization, and the unchanged
  volume's ongoing price is in the close report. This runtime has no volume-delete operation.
- `supervise.py`, run as `python -m operations.pod.supervise`, is the tracked
  runtime for `controllers.LaptopSupervisor` — **deferral 04-1 is closed.**
  Ownership of a lease is decided by a non-blocking `fcntl.flock` on a
  dedicated lock file beside it (`supervisors/supervisor-<lease>.lock`), never
  by a recorded pid: a pid is reused by an unrelated process after a laptop
  reboot, which would make a bare `os.kill(pid, 0)` check find that unrelated
  process alive and refuse forever to supervise a pod still billing. The
  kernel releases the lock the instant the holding process dies, however it
  dies. A companion identity file (`supervisors/supervisor-<lease>.json`)
  carries the owner token a restart resumes once it holds the lock — the lock
  is the proof the prior holder is gone, so a crash and its restart never read
  as a rival to reconcile, and two live drivers can never both reach for the
  same pod. Every tick also re-reads `provider.status()` and closes on any
  lifecycle word other than `RUNNING`, naming the observed state in the close
  reason: this is the actual 04-4 fix (see the rewritten row below — the old
  "closes when 04-1 lands" line was wrong about the mechanism, not the
  outcome). A provider that cannot answer `status` this tick is read as
  neither `RUNNING` nor a reason to close; the heartbeat above still holds and
  the loop keeps ticking rather than crash-looping or guessing. The operator
  `status` verb gains a read-only supervisor block per open lease — running,
  absent, or unknown (by peeking the lock, never creating it and never
  trusting a recorded pid, and classifying only `BlockingIOError` as proof of
  a holder — exactly as `OperatorSurface._exclusive_paid_launch` already does
  for the paid-launch claim; any other lock-check failure prints an honest
  "unknown", never "running"), identity file age, last tick result, last
  close record, and the volume's ongoing hourly price — with no new verb.
  Exit status follows `cli.py`'s convention
  (0 guarded, 2 nothing touched, 3 go and look) with one addition: a durable
  lease this run confirmed active going missing or unreadable exits 3, not 2,
  since the pod it guarded may still be billing. `main()` catches
  `BaseException`, not only its own refusal type, so a bad `--provider-factory`
  reference or a malformed `spend.toml` still attempts a durable final record
  before exiting, and names a failed record write in its printed exit record
  rather than letting it vanish — a detached process's traceback on stderr
  goes unwatched otherwise. Seven offline drills against `FakeProvider` and an injected clock
  prove it: a crash mid-heartbeat resuming ownership with no close; a lost
  identity file reporting `BUSY` inside the timeout and closing after it,
  naming which; a provider unreachable on `status`/`verify_absent`/
  `capture_cost` reporting non-green without ever fabricating a verified
  close; an `EXITED` pod closing on a fresh heartbeat, naming the volume's
  ongoing rate; an already-`closed-verified` lease exiting without touching
  the provider; a second driver refused before it ever reaches the lease;
  and a foreign owner's deadline passing while the supervisor breaks rather
  than spinning.
  **A session that starts `supervise.py` is running a long-lived process
  while a pod bills — `~/.claude/WAKE_PLAYBOOK.md` applies to it exactly as to
  any other unattended long-running work.**
- `controller_armer.py` is the two-controller armer — **deferral 04-2 is
  partly closed.** `ChannelControllerArmer.arm` starts the laptop supervisor
  first, detached, and records its start *before* polling begins: a launcher
  that dies mid-poll then leaves a live supervisor over an `active` lease
  whose `controller_record` is `None`, which `controllers.run_once` closes
  once the launch owner's heartbeat goes stale. The reverse order — poll
  first, start the supervisor on success — would leave a billing pod with no
  laptop-side controller at all for the whole polling window.

  **The owner-token handover is this module's own decision, not something the
  spec dictated.** `supervise.establish_identity` resumes whatever token it
  finds in a lease's identity file and mints a fresh one only when none
  exists; if the armer let the supervisor it starts mint its own token, that
  supervisor would not match the lease's expected owner and could only ever
  reach the pod through `LeaseStore.claim_if_orphan` — closing, one heartbeat
  timeout after the launcher exits, the very pod it was started to guard. So
  the armer writes the identity file itself, carrying *this launch's* owner
  token, before starting the process (0600, `durable.exclusive_write`, never
  overwriting): the supervisor resumes it and becomes the lease's legitimate
  heartbeating owner. On `FileExistsError` the armer reads the existing file
  back and compares its `owner_token` to this launch's — a matching token (a
  retried arming attempt over the same lease) still arms exactly as before; a
  foreign or unreadable token refuses with `SUPERVISOR_FAILED` before the
  supervisor starts or any channel read happens. The token never rides in
  argv (`ps` is public) and never in a receipt.

  **The armer heartbeats the lease on every poll wait.** The lease's heartbeat
  was last refreshed when the pod was bound, and the supervisor above is now
  live over an unarmed lease: without this, the supervisor would close the pod
  mid-poll at the ordinary heartbeat timeout while the launcher was doing
  exactly what it is supposed to do — `controllers.run_once`'s own branch here
  says as much: *"its launch owner is still heartbeating."* A heartbeat that
  fails — the lease was claimed, closed, or damaged by something else — stops
  the poll at once and refuses. A preflight check refuses before any of this
  when `poll_seconds * 3 >= laptop_heartbeat_timeout_seconds`, since
  `SpendPolicy` accepts any positive heartbeat timeout and a misconfigured one
  would otherwise be a silent guarantee that every launch dies mid-arming; and
  when the configured supervisor command does not resolve on `PATH` or as a
  file, or its `--spend` file does not exist — both free reads before the
  paid `create`. It refuses before starting anything, too, when the request,
  the produced pod record, and the lease disagree about the pod id or hard
  deadline: no receipt could satisfy both `launch._validate_arming_binding`
  and `lease._validate_controller_record`, and finding that out after minutes
  of polling would waste the whole window. A pod clock a little ahead of the
  laptop's is waited out inside a small skew bound rather than papered over by
  back- or forward-dating either clock; past that bound the two clocks
  disagree by more than a safety receipt can absorb and the launch refuses.

  **The channel is documented, not observed — that is why 04-2 stays only
  partly closed.** The pod writes its report to its mounted volume; the
  laptop has no filesystem there and must read it back through RunPod's
  S3-compatible network-volume view — a `GetObject` read beside the existing
  `HeadObject` in `operations/operator/volume_s3.py`, reusing `_means_absent`.
  `TimerReportChannel.read` returns bytes, or `None` **only** when the object
  is proven absent; a refused credential or any answer the channel cannot
  classify raises instead — an unreachable channel must never read as "not
  yet", or a supervised pod could be closed for a report it actually wrote.
  Whether an object written through a pod's mount appears in that view, under
  which key, and after how long is **a designed path, not an observed one**:
  no offline test can measure it, only a live boot can. **New closes-when for
  04-2:** the first authorized boot observes an object written through the
  pod's mount appearing in the network volume's S3 view, and records the
  delay.

  **And the drill armer.** `ObservingControllerArmer` runs the identical
  procedure — same handover, same supervisor start, same read — and
  hard-codes `pod_timer_acknowledged` to `False`, so `ControllerArming.armed`
  is `False` by construction whatever it observes and `launch._arm_or_close`
  closes the pod at once. Its laptop-supervisor flag stays honest: the
  supervisor really did start, and a false statement in a durable record is
  not a safety property. What it actually saw, and how long it waited, goes
  to a local evidence file — the measurement the first authorized boot exists
  to take. It is what Boot A below runs.
- `bootstrap_main.py`, run as `python -m operations.pod.bootstrap_main`, is
  bootstrap-and-hold — **deferral 04-3 is closed**, and the sentence that
  matters is this: **the entrypoint holds.** `pod_timer.run_with_bootstrap`
  treats any child exit before the hard deadline, exit 0 included, as
  `completed-early` and closes the pod, so a script-shaped entrypoint that ran
  bootstrap and exited was precisely the artifact the runtime refuses. On
  green this process holds until the pod is destroyed, re-journaling a
  liveness line at the monitoring interval; a red step exits non-zero at once,
  the correct immediate close. `ChairCacheBootstrapAction` is constructed here
  for the first time in the tracked tree, using the real Hugging Face fetchers
  already in `common/chairs/registry.py` for both chair-cache verification and
  model-store materialization — **deferral 04-8 is only partly closed**: the
  class is constructed, but `refetch_same_pin=None` (the registry has no
  cache-clear verb), so the at-most-one same-pin re-fetch still does not ship,
  and no test exercises the action. See the 04-8 row below. The transfer
  target stays optional: no submission manifest on the volume is a vacuous
  success and this process needs no object-store client at all; a manifest
  present with no configured target is a refusal, never a silently skipped
  upload. `PREFLIGHT` is wired to the real things now: `RegistryChairCacheVerifier`
  runs `ChairRegistry.ensure` over the plan's roster for the cache half, and the
  smoke half is the serving package's own production seam
  (`assemble_serving_smoke_reader` around `ServingManager`), fed
  `operations/serving/smoke.py::VisionSmokeCall` with a witness the pod draws
  from the CSPRNG and renders onto a golden page under
  `<volume>/preflight/<report stem>/` moments before the read — so the value a
  chair must read back was never in a committed file or a prompt. The serving
  receipt, launch audit and evidence manifest each smoke publishes land
  content-addressed in that same directory (`PodPreflightReceiptPublisher`),
  because no run tree exists yet for a `StageContext` to own them; the
  catalogue is `--serving-recipes-config` (default the fixture-only
  `config/serving_recipes.toml`; a real launch names
  `config/serving_recipes_real.toml`), and `--fixture` with
  `--page-witness-file` lets an operator supply a rendered page instead.
  `test_bootstrap_main.py` proves the wiring green through the real registry
  over the committed model fixtures and the serving package's fakes, and red
  by chair name when a row is unproven. **One thing keeps the first real
  `PREFLIGHT` red, and it is not a wiring fault:** every vLLM row in
  `config/serving_recipes_real.toml` is `preflight_state = "unproven"`, which
  `ServingManager.start` refuses by name before it launches anything, so a
  reviewer must stamp rows proven first (the serving README says that happens
  after a real-silicon preflight — a circle this unit names rather than cuts).
  The stack those rows name is installable now, see
  "The serving stack, re-planned and locked" below. Both the ordinary hold and the `--hold-only` drill
  hold to `VERBATUS_HARD_DEADLINE` (the same spelling the RunPod pod-timer
  factory reads), so this process's own end approximately coincides with the
  pod-side timer closing the pod regardless. Refusals precede any action: a
  journal or report path outside the mounted volume, a lockfile that is not
  the checked-out repository's `uv.lock`, a volume that fails a real write
  probe (probes by writing and reading back, never by creating the mount
  point it is supposed to require), a missing hard deadline, and a
  credential-looking argv value. At startup the environment is scrubbed by
  the shared credential-shaped predicate, not by vendor name, except an
  explicit `--keep-env` allowlist. `--dry-run` resolves, validates, and prints
  the plan without running or holding — it does not mean run against fakes,
  since a fake-actions flag in a production entrypoint is a green journal
  waiting to happen. `--hold-only` is a named drill mode: no bootstrap steps,
  journal a `hold-only` record, hold to the deadline; it refuses if any plan
  argument is supplied and the README names it drill-only.

  **How the journal reaches the laptop: the same channel, and the same
  caveat.** The journal is written to the volume under the launch-bound name.
  No laptop-side reader for it exists yet — nothing in the tree calls
  `TimerReportChannel.read` on the journal key; the armer reads only the
  sealed pod-report path. Reading the journal back (`SPEC_POD.md` §4.5's
  `supervise --show-bootstrap`) is unbuilt, and the S3 view it would use is in
  any case **a designed path, not an observed one.**

  **Where this unit's tests differ from what the spec predicted, said plainly.**
  `test_bootstrap_main.py` states in its own module docstring that "no git,
  uv, Hugging Face, or GPU probe is ever invoked here" — every test drives
  `bootstrap_main` through a fakes-only `actions_factory`, never through
  `SubprocessBootstrapActions` or a real subprocess. `SPEC_POD.md`'s §4.5
  predicted this unit's tests would close three of deferral 04-5's five
  untested seams; verified against the code as it stands, they do not. See
  the rewritten 04-5 row below for what is and is not actually covered.
- `pod_run.py`, run as `python -m operations.pod.pod_run <run flags> -- <bootstrap_main
  argv>`, is the one tracked entrypoint that runs the pipeline on a pod. It is
  composed from `bootstrap_main` rather than beside it: the argv after `--` is
  prepared and run through that module's own `prepare`/`run_bootstrap`, so every
  refusal, the write probe, the credential scrub and the hard deadline are the
  same ones a plain bootstrap gets. After a green journal it starts
  `pipeline/orchestrator/run.py` as a subprocess of the pod's own interpreter
  over the volume — run root `<volume>/runs` (or `--run-root`, inside the
  volume), `--submission-folder`/`--submission-manifest` inside the volume,
  `--models-config` and `--serving-recipes-config` taken from the bootstrap plan
  (the roster `PREFLIGHT` measured is the roster the run serves; naming a
  different pair here is not offered), `--data-gate-policy` inside the
  repository — and writes a `pod-run-report.v1` at its launch-bound
  `--report-path` before, during and after: `bootstrapping`, `running`, then
  `complete`, `held`, `halted`, `failed`, `bootstrap-red` or `refused`. **Exit
  codes never read complete for a partial run:** 0 only when the orchestrator
  returned `EXIT_COMPLETE`; 3 held and 4 halted mirror the orchestrator's own;
  2 a named refusal before anything ran; 5 a red bootstrap step; 6 an
  orchestrator that could not start or exited outside its vocabulary. Refusals
  are by name for every missing input — no `--`, a `--hold-only` bootstrap
  plan, a run report path that is the bootstrap's or lacks the launch token, a
  run root or submission outside the volume, a submission folder or manifest
  that is not there, a policy outside the repository, a bad run id — and **the
  data gate is asked first**, before a model is fetched on a billing card:
  `config/data_handling_policy.json` now names the pod volume mount path
  (`operations/pod/boot_a_request.py`'s sealed `volume_mount_path`) beside the
  local `private/` root — that listing was a disclosure decision, Tyrel's
  under hard rule 1, made once rather than per-launch — so a submission
  folder under either listed root is approved and one outside every listed
  root is refused here, by name, before a model is fetched on a billing card.
  **The ruling that listing rests on, recorded here so it is readable inside
  the repository.** On 2026-09-01 Tyrel ruled that everything a pod produces
  is kept on the attached network volume until it is exported locally with the
  results, the Perlector training inputs included; the consequence taken from
  it is that the volume is an approved storage root for the duration of a run,
  and that `verbatus fetch-run` is the export path home. The verbatim ruling
  and its date are in `workbench/standing/TYREL_RULINGS_2026-09-01_BUILD_SESSION.md`,
  which is gitignored like the whole workbench — so the operative decision is
  written out here as well, in the project's voice, rather than resting on a
  citation nobody reading this tree can open.
  **The narrowing is recorded, not only the approval.** The shipped policy
  names two roots and almost no machine has both: a pod has no local
  `private/`, a laptop has no mounted volume. `gate.resolve_storage_roots`
  returns the roots that resolved *and* the ones that did not, and `pod_run`
  writes both into its run report (`approved_storage_roots` and
  `skipped_storage_roots`), on every run rather than only in the all-absent
  refusal, so a reader can tell which list was actually enforced (GOVERNANCE
  2).
  **There is no `--placement-tier` flag.** The consult that asked for this
  entrypoint named one; the orchestrator and the stage parser accept none and
  no stage reads a tier as the code stands, so `pod_run` records the tier the
  green `PREFLIGHT` receipt measured in its run report and refuses a green
  bootstrap whose receipt carries none. **It holds only for a run that
  finished.** After `complete` or `held` it holds to the shared hard deadline
  exactly as `bootstrap_main` does (a liveness line beside the run report,
  never over it), because `pod_timer.run_with_bootstrap` treats any child exit
  before the deadline as `completed-early` and closes the pod with a non-green
  timer report; that hold is paid idle time between a finished run and the
  deadline, and closing early on a complete run would be a `pod_timer` contract
  change this unit does not make. After `halted`, `failed`, or an orchestrator
  that could not start, it returns at once and lets the timer close the pod —
  the same cheap close the red-bootstrap branch already takes, because holding
  a rented card to the deadline for a run that produced nothing further is
  paying for nothing (GOVERNANCE 8, hard rule 2). Nothing is lost by leaving:
  the run tree, both reports, the journal and the preflight evidence are on the
  volume, which outlives the pod, and `verbatus fetch-run` reads it over S3 with
  no pod running. Which way it went is in the run report's
  `held_to_hard_deadline`. `test_pod_run.py` drives all of it
  against a fakes-only bootstrap and a recorded orchestrator: no chair is
  served, no model is called, no provider is reached.
  **The roster and the catalogue are named together or the plan is refused.**
  `bootstrap_main` defaults `--serving-recipes-config` to the fixture-only
  `config/serving_recipes.toml`, which is right only while `--models-config` is
  the shipped fixture roster; any other roster must name its catalogue
  explicitly, or the plan is refused before the boot. A real roster resolving
  against the fixture catalogue is the mismatch `pipeline/orchestrator/run.py`
  names and `operations/operator/surface._roster_argv` refuses, and on this path
  it would otherwise be discovered only after the pod had billed for the boot
  and the model fetch.
- `notify_hooks.py` is a small, vendor-neutral phone-notification seam, distinct from
  `notify_bridge.py`'s narrower spend-floor-warning one. It sends exactly one short line
  through `operations/notify/notify.sh` at each of three moments — launch (lease id,
  card, hourly ceiling), close (lease id, verified state, elapsed), and each account
  balance observation (balance and spend rate as the observer reports them) — never a
  secret, never a URL: every message is checked against the same credential-shape and
  URL markers before the shell call, and a message that fails is never sent. It refuses
  nothing else, and a failed ping never blocks or changes a launch or close decision
  (ruling (b): tracking plus notifications only, no new enforcement). `cli.py` wires
  all three behind the existing `--notify` flag. Launch and close it calls directly.
  The balance hook it installs through `set_balance_notify`, duck-typed exactly as
  `--record-fixture` reaches `record_exchanges`, because the provider itself comes
  from an untracked `--provider-factory` this repository never constructs — a named
  method on the returned object is the only place the host CLI can reach a vendor
  adapter. `RunPodProvider` also takes an explicit `balance_notify` constructor
  parameter, defaulted to `None`; both routes are opt-in, so a bare live-transport
  construction — including the one the pod's own `timer_context_from_environment`
  performs — carries no hook, `--notify` stays the single gate for every phone
  notification a launch can send, balance included, and a pod never pages a phone on
  its own. A provider with no such seam is *recorded*, never refused: the launch
  record's `balance_notification` says whether the hook was wired and carries one line
  per ping sent, delivered or not, and the observer folds a ping that did not land into
  the observation's own `source` so the spend record carries it too (GOVERNANCE 2).
  The close line says `billed Ns from creation` rather than `ran Ns`, because the
  number is pod creation to the verified billing cutoff and no stop time is observed
  anywhere on this path. `test_notify_hooks.py` proves the
  three messages, the no-secret and no-URL refusals, and that a failed ping is
  reported, never raised, offline against a fake runner; `test_pod_runtime.py` drives
  the balance half end to end through `cli.main` against `FakeProvider`.
- `spend.py` applies the same price display, ceiling calculation, and typed phrase to
  create and adopt. The phrase is **derived from the preview**: it names the action, the
  subject and both hourly rates just displayed, and carries a random single-use
  challenge only a preview issued in this process can supply, binding the
  acknowledgement to that preview. A refused confirmation neither reproduces the
  phrase nor spends the challenge — a typo does not burn the preview. It is not
  Tyrel's live-pod permission and does not claim a script cannot derive it. The hourly and
  lifetime ceiling checks include both pod and attached volume while the hard lifetime is
  running, and the ceiling is applied a second time to the price the provider *actually*
  returned — a created pod that bills above it is closed immediately rather than left
  running. **Which card may be rented is its own gate, and it is `config/pod_placement.toml`
  that says so.** A create refuses, before any provider call at all, a `gpu_type` that is
  not a reviewed row of the placement table the runtime was given (matched on the row's
  `gpu_type_id` alone: that is the only spelling this repository has ever sent to the API —
  `boot_a_request.py` renders `"gpu_type": card.gpu_type_id` — while the table's `name`
  column is prose for the operator, so accepting it too would allowlist a string no
  provider is known to take), and refuses again — after the estimate and still before anything is created — a
  row whose *reviewed* price exceeds `max_hourly_usd` net of the volume rate the provider
  quoted. That binds the reviewed price, which the returned-price ceiling above cannot do:
  a card quoted cheaply today is still the card the table priced. Both refusals name the
  row and the ceiling and are `refused-card`. `cli.py` passes `config/pod_placement.toml`
  unless `--placement` names another table, and an unreadable table refuses the launch
  rather than turning the gate off; a `PodRuntime` constructed with no table — an offline
  drill — enforces no allowlist. `adopt` is deliberately not gated this way: the pod
  already exists, and refusing to adopt it would leave it billing unguarded. `config/spend.toml` is deliberately unconfigured, so both paid paths refuse
  until Tyrel supplies a reviewed policy. A configured policy must set a
  `billing_cutoff_margin_seconds` value within the code-owned 0–3600-second envelope;
  the exact value is sealed into both shutdown controllers and the pending-create
  recovery record, while an out-of-bounds value refuses rather than being clamped. It
  also enforces an observed `account_balance_floor_usd`. Its source is an explicit
  provider seam; an unavailable source refuses paid actions, and the refusal names why
  the balance could not be read. The RunPod adapter bounds that read: a source that
  blocks rather than fails becomes a named timeout refusal, because one of these gates
  runs after `create` has returned a billing pod and before anything is armed to stop
  it, and a stall there would record no assessment and attempt no close. Observations more than 60 seconds old or dated in the
  future are unusable, never cached authority. The floor is a reserve that must survive
  the run, so it is tested against the observed balance net of this action's estimated
  cost to its hard deadline and every active or pending liability in the same durable
  lease root. Create/adopt serialize that assessment until the new lease is recorded, so
  two concurrent confirmations cannot each spend the same reserve. That same lock carries
  the single-live-pod invariant: inside it, and before this action arms a lease of its
  own, create and adopt refuse outright when any lease in the root is short of
  `closed-verified`, whoever armed it. Two individually affordable launches clear every
  ceiling and still end with two pods billing behind one record that can only name one of
  them, which is the harm GOVERNANCE 8 exists to prevent; enforcing it under the lock
  makes it a fact rather than a race between one process's check and another's write. The
  refusal is `refused-active-lease`, and it consumes no challenge, so an operator who
  closes the open pod can confirm the preview they were already shown. An unreadable,
  expired-but-unclosed, or unverified-close lease makes that liability unknowable and
  refuses through the same floor mechanism. The balance is read only at the create and
  adopt gates, never again while a pod is live. Above that hard floor,
  `account_balance_alert_usd` sends notification-only warnings through
  `operations/notify/`; delivery never alters a launch decision, and whether the phone
  got it is recorded either way. A delivered warning is suppressed for fifteen minutes
  while readings remain low; two consecutive safe readings establish recovery, so a new
  crossing sends a page immediately, while one-reading flaps do not re-arm it.
  `operations/pod/cli.py` sends nothing unless `--notify` is passed. The commented
  `$50.00` template value remains unverified and must be checked against RunPod before a
  live run. All paid actions for one provider account must use the same lease root; the
  first live checklist records that operational binding because the provider-neutral seam
  has no stable account identifier from which to derive it.
- `staged.py` is the per-stage layer above that gate: one collection stage, one
  independently authorized boot, then pod-down by default. It creates and closes; it
  does not adopt, and recovering a pod a crashed stage left running stays with the
  lease-backed controllers. Durable records land on the run volume, and between them no
  boot can happen without money evidence. A **claim**, keyed by the grant reference and
  written before the provider is touched, is what makes one grant unable to buy a second
  pod — durably, so a restart or a second lifecycle over the same volume is refused
  rather than allowed. It also means a create that refuses spends the *reference*: a
  retry records a fresh one, which is bookkeeping and not a second permission. An
  explicitly unknown **cost intent** is fsynced before the provider call; if any later
  write fails or the create response is lost, that durable liability can never read as
  zero. A **boot record** binds the pod id to its collection, stage and grant the moment
  the machine exists, which is the one thing the lease cannot say. A failure to land it
  triggers immediate pod-down rather than returning with avoidable spend. And one
  **close record** per boot: a cost record carrying the close report, or a
  separately-schema'd close failure
  when the close raised, returned nothing usable, or could not be attempted — the case
  where a pod is most likely still billing, and so the last one allowed to vanish behind
  an exception. `render_boot_schedule` prints the whole collection's expected boots
  before any of them is asked for, chairs named per stage from the real roster and
  reconciled against it by test, so a grant is never asked for one boot at a time.
- `transfer.py` carries Spec 03's sealed submission-manifest rows through a generic
  storage seam. It streams and verifies SHA-256/size before and after upload, persists
  verified rows, and refuses conflicting target bytes rather than overwriting them.
  `bootstrap.py` journals idempotent exact-commit, locked-`uv`, transfer, chair-cache,
  and preflight steps. A cache digest mismatch is *specified* to receive at most one
  same-pin re-fetch — see deferral 04-8: the class implementing it is now constructed
  once, in `bootstrap_main.py`, but wired with `refetch_same_pin=None` and covered by
  no test, so the at-most-one same-pin re-fetch itself still does not ship; another
  mismatch is red and names the chair.
- `preflight.py` measures or receives CUDA/driver/capability/VRAM/disk facts, selects a
  single-resident plan only from `config/pod_placement.toml`, verifies every configured
  chair, and validates a stochastic proof-page read by shape, non-emptiness, and format.
  **Serving is sequential** — one model at a time, as much of the card as stays stable,
  next model after — so every tier is single-resident and what a tier changes is the
  engine memory fraction, context cap, pixel cap and batch size that one model gets. The
  table ships prebuilt profiles for the cards this project actually rents and falls back
  to computed placement for anything else; the report names which plan it chose and why.
  Its utilization fields are measurements, not a claim that a card is "saturated". Its
  `assembly_proven` flag is **derived from what actually ran**, never declared: true only
  when the card was read by a real driver (`GpuProfile.measured`, set by `SystemGpuProbe`'s
  successful `nvidia-smi` path alone) *and* at least one chair read the golden page back
  through an engine that served it (`SmokeResult.served_by`, set by
  `operations/serving/preflight.py` from the service handle it started and stopped), and
  the note then names those chairs and that card. An invalid read proves nothing, and the
  colour of the report is not consulted: a red preflight that nonetheless served one chair
  on a real card proved that much. The fixture and planning paths stay `False` under the
  same sentence they always carried; a measured card with no served chair says so in its
  own words rather than calling a paid measurement fixture-only. The smoke adapter cannot
  pre-populate the runtime-owned `served_engine` receipt field, so a fake cannot assert its
  own proof. **Both halves are minted, not declared.** `PreflightRunner` takes the profile
  from its caller and the smoke reader from its constructor, so `measured` and `served_by`
  were fields anyone wanting the claim could simply set. Each now travels with an opaque
  module-private token created in exactly two places — the probe's successful `nvidia-smi`
  path and the serving module's service-evidence path, which names the engine off a
  service handle it started, proved fixture-bound and stopped — checked by type, never
  serialised into any receipt or record, and refused at construction when a caller
  supplies one. A caller-built `GpuProfile(measured=True)` and a caller-built
  `SmokeResult(served_by=…)` no longer exist, so `assembly_proven` cannot be reached from
  outside those two runtimes.

The local command surface is `python -m operations.pod.cli`, with three verbs: `create`,
`adopt`, and `close`. The first two require explicit untracked provider and
controller-armer factories plus a request file, so this repository contains neither a
credential, a personal provider default, nor an implicit controller process. They print
the previewed current price and ceilings before prompting for the exact typed
confirmation; EOF is a refusal. A `create` or `adopt` run without
`--controller-armer-factory` refuses in this surface's own record shape — one JSON object
naming the missing flag, exit 2 — rather than in argparse's usage text, so nothing reading
these records meets a second shape.

**`close --lease <id>` closes one live lease on purpose**, through
`supervise.close_lease_now`, which drives the same `_close_lease` a supervisor tick drives
on an `EXITED` pod — so the standard is the one standard above: exact-pod GET-404,
independent pod-list absence, non-empty exact-pod billing through the cutoff, and
`UNVERIFIED CLOSE` for anything short of it, which is reported as such and exits 3, never
0. It writes the lease's terminal phase, and with `--notify` sends the same close line a
create sends when it closes its own pod. It arms nothing, so it needs no
`--controller-armer-factory`; it previews nothing and asks for no typed phrase, because it
stops spending rather than starting it. It refuses, before any terminate, a `--lease` that is not 32
lowercase hexadecimal characters — checked before the id is interpolated into the lease,
lock, identity or record path; a lease this account does not hold, absent from the lease
root or armed under a different `--provider-name`; a lease file whose own recorded
`lease_id` is not the one asked for, which is a renamed or hand-edited file and would
close a pod under another lease's identity; and a lease some live supervisor still holds,
since two closers over one pod is what the lease's kernel lock exists to prevent. A lease
file that exists and **cannot be read** is exit 3, not 2: it is the durable record of a
paid action, the pod it names may be billing, and the refusal prints the exact path to go
and look at.

**Everything that fails on this laptop before the provider is reached is exit 3 as well**,
under one `CLOSE NOT ATTEMPTED:` prefix — the sibling of `UNVERIFIED CLOSE`, and a
different phrase because that one means a close ran and could not be verified while this
one means none was tried. It covers a `--provider-factory` that will not import, will not
resolve, raises when it is called, or returns something that is not the seam, and a
`--spend` policy that cannot be read or is unconfigured. None of those stops a pod, so
none of them may exit 2 and say "nothing was paid"; each leaves the durable record, names
what failed, and leaves the lease exactly as it was so whatever guards it keeps guarding
it. The optional attachments around the close are contained rather than fatal for the same
reason: an evidence recorder that cannot be opened (`OSError`) or a provider that raises
while attaching it is written into the close record, and a balance-notification seam that
raises while being wired is written into `balance_notification` — a phone that cannot be
reached has never been allowed to decide a launch, and it certainly may not abandon a
close. `create` and `adopt` keep the opposite disposition for the recorder: nothing is
paid yet, so a recorder that cannot be attached refuses by name with exit 2.

**`--provider-name` is a label, not a proof of account**, and one refusal exists because of
that. It is the string the operator typed, recorded in the lease; nothing reconciles it
against the credentials behind `--provider-factory`. So a factory pointing at the wrong
account passes every holding check above, and the close would then terminate nothing,
observe a perfectly genuine absence and no billing on an account that never held the pod,
and write `close-unverified` — which is not an active lease, so `run_supervisor` would stop
guarding a pod still running and still billing on the real account. A close whose provider
reports the pod **absent before any terminate was issued** therefore refuses with exit 3,
names that possibility, and leaves the lease exactly as it was for the supervisor to keep
guarding. Only a close that actually issued a terminate may reach `close-unverified`, and
`UNVERIFIED CLOSE` prefixes the detail on both branches that report one — the close just
attempted, and the already-terminal lease a returning operator asks about. Every outcome,
refusals included, also leaves the same durable per-run record beside the lease that
`supervise.py` writes, so a refusal read into a terminal that is about to be closed is not
the only copy. A configured `--spend` policy is
required and is not a spend gate here: the shutdown controller's poll interval, deadline
and billing-cutoff margin are reviewed values, and a close driven on invented timings is
not the close this repository verifies. Before this verb existed a live pod could be closed
only by its sealed hard lifetime, by a supervisor tick that happened to see a non-`RUNNING`
state, or by the provider's console. **`--record-fixture PATH`** routes every provider
exchange the launch sees — method, path, request body, status, response body, and a
raised transport failure — through `fixture.py`'s recorder, appended as JSON lines
(0600, fsynced per line, never truncated) so a drill boot leaves a replayable fixture
behind for deferral 04-6. Bodies are stored verbatim unless something in them is
credential-shaped by either of the two shared predicates — a key that names itself a
secret by `models.looks_like_credential_field`, or a string leaf under any key at all
that carries a credential-shaped word by `models.looks_like_credential_value` — in which
case the body is parsed, those values replaced, re-serialized with money still as
numbers, and the record says `verbatim: false` and names every scrubbed path; the launch
token is scrubbed too, by the name predicate, with no exemption. The flag is duck-typed on a provider's
`record_exchanges` method so the CLI names no vendor; the RunPod adapter wraps its REST
transport and its own balance observer's transport, and a provider without the method —
the fake — refuses the flag by name before any preview rather than recording nothing. The
exception is `close`: that verb exists for the moment a pod is billing and something has
already gone wrong, so an unhonoured flag is written into its record and the meter is
still stopped, rather than trading a live pod for a fixture nobody asked for in that
moment. The result record names the fixture path.

**The Boot A request is a tracked module, not a note.** `python -m
operations.pod.boot_a_request --spend config/spend.toml --placement
config/pod_placement.toml` renders, from the sealed spend policy and the reviewed card
table, the plain-language request Tyrel reads before the drill: what it does, the
cheapest reviewed card and its rate, the hard lifetime (900 seconds, or the policy's
ceiling if shorter), the ceilings the launch will enforce, the cost if it ran the full
lifetime, the expected immediate close, the exact command with `--record-fixture` on,
and the pod request JSON with his four values marked not yet supplied until passed as
`--image`, `--volume-id`, `--repository-commit`, `--hard-deadline`. `cli.py create`
refuses to load the JSON until `hard_deadline` is filled in by hand; the
`VERBATUS_BILLING_CUTOFF_MARGIN_SECONDS` placeholder alongside it needs no hand edit,
since the launch seals that value from the spend policy on every create or adopt. An
unconfigured policy renders a
**refusal** naming what is missing — no command, no request — and exits 2. The text
authorizes nothing; it is what the per-session gate needs in order to be given. A preview that is itself refused prints its price
and every reason but withholds the phrase, because a refusal spends no challenge and the
phrase in that report would still authorize the action if the condition cleared. Its exit status never reads as "nothing happened"
when something did: 0 is a guarded success, 2 a refusal whose result names no pod, lease,
or close, 3 a non-green outcome that observed or touched a real pod, wrote a lease, or
attempted a close — go and look, which includes a create refused because
another lease in the root is still open; an interrupt mid-action prints an `interrupted` record
naming the leases directory before dying. A request must make the provider-neutral timer the
primary command and include a
provider-owned timer factory, a structured mandatory bootstrap command, and a durable
report path under the attached volume. If bootstrap exits (successfully or not), or its
report cannot be retained, the timer attempts immediate verified close rather than leave
an idle meter running. A typed phrase is an operational guard; it is **not** Tyrel's
live-pod gate. Do not invoke a factory that could contact a provider without his
current-session authorization.

The provider-owned pod-timer factory requires ephemeral runtime facts and an untracked
termination capability. Its behavior is fake-proven, including both death drills after a
green fake launch handshake. Delivery of that capability, a real pod identity at boot,
the acknowledgement channel, and the provider's post-delete/billing behavior remain part
of Tyrel's gated live demonstration; this repository makes no claim that they have
occurred. **The tracked tree now does supply a durable laptop-supervisor driver
(`supervise.py`), a controller armer that performs the real read against a
documented-not-observed channel (`controller_armer.py`, both `ChannelControllerArmer`
and the never-arming `ObservingControllerArmer`), and a runnable bootstrap/service
entrypoint (`bootstrap_main.py`)** — see the three bullets above. What remains
before the gated demonstration is not their absence but their live observation:
whether the pod-report channel actually behaves the way `TimerReportChannel`'s
contract requires is unproven by any offline test, which is why the drill armer
and Boot A exist. A pod-side process may be destroyed by its own DELETE before
it can observe GET-404, list absence, or lagging billing;
the real controller design must leave final verification and reconciliation to surviving
laptop/restart machinery rather than infer it from the pod process. If `pod_timer.py`'s
own startup fails **before a provider-backed timer exists** (a missing environment value,
a deadline already passed) it can terminate nothing; the pod goes `EXITED`, which bills
volume disk at double the running rate, and the laptop supervisor above (`supervise.py`)
is the only *automatic* backstop for that case — the operator's own backstop is the `close`
verb below. A refusal raised *after* the timer is built now
spends that capability on an immediate close and files a receipt. A non-green close at
the hard deadline is re-attempted a small fixed number of times before the timer exits;
the durable report records the attempt count and the final close's evidence (not each
intermediate attempt). The deliberately still-running workload child is left to the
pod's destruction, since the timer is the container's primary process.

Run the fake checks with:

```sh
python3 -m pytest operations/pod
```

They include deliberately broken confirmation, ceiling, status, billing, transfer,
cache, smoke-read, laptop-controller, and pod-timer paths, plus (as of `supervise.py`,
`controller_armer.py`, and `bootstrap_main.py` landing) the seven `supervise` drills named
above, the arming states in `test_controller_armer.py`, and the hold/refusal/scrub paths
in `test_bootstrap_main.py`. `test_launch_drill.py` adds seven further offline drills that
run one real `PodRuntime.create` through the real armer, the pod's own report, and the
`supervise` driver together: a green launch, a launcher that dies mid-poll, a report that
never appears, an `EXITED` pod under a fresh heartbeat, both close-ordering directions
(supervisor-first and launch-side-first), and the observing armer -- against
`FakeProvider`, a directory standing in for the volume's network view, a fake clock, and
an in-process supervisor. A full real-chair preflight
is not demonstrated: the committed roster is still fixture-only and has no real GPU or
model-service measurement.

## The serving stack, re-planned and locked

The stack the real roster asks for is now a `pod` dependency group in
`pyproject.toml`, resolved into `uv.lock`. Every vLLM row in
`config/serving_recipes_real.toml` names the same three packages, and the group
carries those exact versions under
`sys_platform == 'linux' and platform_machine == 'x86_64'` markers:

| Package | Pin | Licence | Why this one |
|---|---|---|---|
| `vllm` | `0.27.1` | Apache-2.0 | The newest release that registers every architecture the roster declares **and** states no `huggingface_hub` floor of its own |
| `transformers` | `5.14.1` | Apache-2.0 | The version the vLLM 0.27 line's own requirements were bumped to; above vLLM's `transformers >= 5.5.3` floor and above the Perlector's stated `>= 5.8.0` |
| `qwen-vl-utils` | `0.0.14` | Apache-2.0 | Unchanged; latest, and it and its dependencies (`av`, `pillow`, `requests`) all publish linux x86_64 wheels, so nothing in this group compiles on the card |

**Licence record for the `pod` group**, per `cleanroom/README.md`'s rule that a
third-party dependency's source and licence are recorded beside the code (this
table, and the group's own comment in `pyproject.toml`):

- `vllm` 0.27.1 — Apache-2.0, per the `LICENSE` file at
  `github.com/vllm-project/vllm` (tag `v0.27.1`).
- `transformers` 5.14.1 — Apache-2.0, per the `LICENSE` file at
  `github.com/huggingface/transformers`.
- `qwen-vl-utils` 0.0.14 — Apache-2.0, per its packaging metadata on PyPI
  (`pypi.org/project/qwen-vl-utils`); the package is maintained under
  `github.com/QwenLM/Qwen2.5-VL`, itself Apache-2.0. This session has no
  network access to fetch either page directly and states the licence as
  each project's own published metadata, not as a byte fetched here.

**Why the old pins could not be locked.** The catalogue previously pinned
`vllm 0.10.1` / `transformers 4.57.1`, and `uv lock` refused the group outright:

> Because transformers==4.57.1 depends on huggingface-hub>=0.34.0,<1.0 and
> verbatus:pod depends on transformers==4.57.1, we can conclude that verbatus:pod
> depends on huggingface-hub>=0.34.0,<1.0. And because your project depends on
> huggingface-hub==1.26.0, we can conclude that your project and verbatus:pod are
> incompatible.

No `transformers` 4.57.x accepts `huggingface-hub` 1.x, so the pair itself had to
move rather than the project's hub pin.

**What the four chairs actually need, read from their own configuration.** Every
version below was read on 2026-09-02 from the cited page.

- Chandra-2 (`datalab-to/chandra-ocr-2`, Designator structure and Attestator 1)
  and the Perlector (`Qwen/Qwen3.8-27B`) both declare
  `"architectures": ["Qwen3_5ForConditionalGeneration"]`, `"model_type":
  "qwen3_5"` — a multimodal architecture, not the text-only Qwen3.5 — with
  `transformers_version` `5.2.0` and `5.8.0.dev0` respectively
  (`huggingface.co/datalab-to/chandra-ocr-2/raw/main/config.json`,
  `huggingface.co/Qwen/Qwen3.8-27B/raw/main/config.json`).
- The DAI fine-tune (`Teklia/Qwen2.5-VL-7B-DAI-CReTDHI-RecordGold-ATR`,
  Attestator 2) and Churro-3B (`stanford-oval/churro-3B`, Attestator 3) both
  declare `Qwen2_5_VLForConditionalGeneration` / `qwen2_5_vl`, saved by
  `transformers` `5.2.0` and `4.51.3` (their `raw/main/config.json`). Churro's
  card names `Qwen/Qwen2.5-VL-3B-Instruct` as its base.
- vLLM's own recipe page for `Qwen/Qwen3.8-27B` (`recipes.vllm.ai/Qwen/Qwen3.8-27B`)
  states **vLLM 0.17.0+** and **transformers >= 5.8.0**. Only its DFlash2
  speculative decoding wants `>= 0.28.0`, and no row here asks for that.
- vLLM v0.27.1's model registry
  (`raw.githubusercontent.com/vllm-project/vllm/v0.27.1/vllm/model_executor/models/registry.py`)
  lists `Qwen3_5ForConditionalGeneration` **and**
  `Qwen2_5_VLForConditionalGeneration` in `_MULTIMODAL_MODELS`, and the tag's
  `docs/models/supported_models.md` carries both rows. **One release covers all
  four chairs; no chair had to be split off onto transformers-direct serving.**

**Why 0.27.1 and not 0.28.0, the newest release.** vLLM 0.28.0 (PyPI upload
2026-08-26) declares `huggingface_hub>=1.27.0` directly in its metadata, which
collides with the project's `huggingface_hub==1.26.0`. vLLM 0.27.1 (PyPI upload
2026-08-11) declares no `huggingface_hub` requirement at all — its hub floor
arrives only through `transformers`, whose `5.14.1` metadata asks for
`huggingface-hub<2.0,>=1.5.0`. So 0.27.1 locks beside the project's pin and
0.28.0 cannot, and nothing in 0.28.0's notes is needed by any chair here.

**Why `flash-attn` was dropped rather than re-pinned.** The Perlector's row used
to carry `flash-attn 2.7.4.post1`. vLLM does not depend on the PyPI `flash-attn`
package: v0.27.1's `qwen2_5_vl.py` imports no `flash_attn`, selecting a backend
through vLLM's own `AttentionBackendEnum` registry instead, and vLLM ships its
own FlashAttention build. Meanwhile `flash-attn` publishes **no wheel** — its
latest release, `2.8.3.post1`, is an sdist only — so keeping the pin would have
meant an hours-long nvcc build against `torch 2.13.0` on a rented card, to
satisfy a pin the server never imports. Dropping it removes a failure mode and
costs nothing.

**What is proven, and what only a boot can prove.** Proven here: `uv lock`
resolves the group (`vllm 0.27.1`, `transformers 5.14.1`, `torch 2.13.0`, 173
packages added, no previously locked version changed), `uv lock --check` is
clean, and `uv sync --frozen --python 3.12 --group test --group audit` still
succeeds on macOS, resolving none of the pod group — the markers hold.
**Unproven, and only a boot proves it: that the wheels install on the pod image
and that the four sets of weights actually load and answer under this release.**
Vendor metadata says the architectures are registered; it does not say these
specific checkpoints run. Every row therefore stays `preflight_state =
"unproven"`, and `ServingManager.start` still refuses each by name until a
reviewer stamps it after a real-silicon preflight.

`bootstrap.py`'s `uv sync` now carries `--group pod`, and its journal records
`"groups": ["pod"]`. The wheel download (vLLM, torch and the CUDA libraries — on
the order of ten gigabytes) happens inside `UV_ENVIRONMENT` on the billing card,
into the container-local `UV_CACHE_DIR` `bootstrap.py` already names and warns
about; it is paid once per pod and never survives one.
`operations/pod/test_pod_run.py` holds the group and the catalogue to the same
bytes as a live test — it was a strict expected failure while no group could
exist, and it is now the guard against the two drifting apart.

## First gated live-pod checklist

This is a checklist for one authorized live demonstration, not authorization to create a
pod. Record the exact pod id, timestamps, provider response, and whether each item is
**verified**, **unverified**, or **not run**. The RunPod field names in this tree are
documented shapes, not observed behavior; no unchecked item may be reported as a pass.

- [ ] Record Tyrel's current-session authorization, the synthetic workload, the
  configured spend ceilings, and `account_balance_floor_usd`. Record the balance
  observation itself (see the dedicated balance-source row below) separately from the
  ceilings, including active obligations and the maximum additional liability of this
  run. Record the one durable lease root used by every paid action against this
  provider account; separate roots cannot see or reserve one another's future
  liability.
- [ ] Confirm whether the pod-scoped API key actually holds **delete** and **billing**
  rights. DELETE must be accepted for this exact pod, and the billing query must return
  usable, exact-pod records. Do not infer either right from successful creation or GET.
- [ ] Record whether the provider offers any pod-side TTL / maxRuntime field on create
  under the version actually used (none appears in the documented v1 create input, and
  `V2_MIGRATION.md` §3 records none found in the v2 create or update documentation
  either; 04-4 has no provider-side belt without one).
- [ ] Exercise **a pod that fails field validation and cannot then be auto-terminated**.
  Record any returned identity, the exact launch-token recovery result, whether the
  automatic close path could act, and the manual provider-console recovery if it could
  not. A create response can fail contract validation before the runtime has a
  `PodRecord` to close.
- [ ] Verify that REST-v1 creation accepts the selected real `gpuTypeIds` value and
  attaches the requested network volume at the requested mount path. Confirm the returned
  id, name, `desiredStatus`, `costPerHr`, `networkVolume` id, `volumeMountPath`,
  `machine.gpuTypeId`, image, `dockerStartCmd`, template, and explicit
  `interruptible=false` against the sealed request.
- [ ] Verify exact launch-token recovery from the pod list after a deliberately lost
  create response. The list must expose the token-bearing environment and must not confuse
  a same-name pod with the one to recover.
- [ ] Confirm whether `GET /pods` paginates on an account holding many pods. Both the
  list-absence proof and launch-token recovery read it as one unpaginated array; a
  truncated list would mean a false absence or a second POST for one authorised launch.
- [ ] Exercise the checksummed transfer end to end on the attached volume: upload the
  sealed submission-manifest rows, read at least one object back, and record that the
  post-upload digest verification actually ran against target-observed bytes.
- [ ] Verify that the network volume is mounted at the sealed path, receives the
  token-bound pod report, survives a process restart, and supports the run tree's
  immutable hard-link publication. Write a control report and one pipeline artifact
  there, read both back, and record the filesystem result.
- [ ] Run **Boot A, the drill**, before Boot B: the cheapest available card, a short
  `hard_lifetime_seconds` (roughly 900), `ObservingControllerArmer`, and
  `bootstrap_main --hold-only`. It closes its pod immediately by construction — the
  arming verdict is False whatever it observes — and, as with any close, the result is
  only green when GET-404, list absence and exact-pod billing all agree; a non-verified
  close is reported as such and the supervisor keeps guarding it. Record whether the
  pod-written object appears in the S3 view, under
  which key, after how long, and whether the pod-scoped key actually holds delete and
  billing rights. See "The boot plan" below for the full split and what it needs from
  Tyrel.
- [ ] Record the account-balance source and that it reports US dollars rather than
  credits or another currency. The GraphQL observer (`myself { clientBalance
  currentSpendPerHr }`) now exists and is the adapter's default over a live transport,
  so this item is runnable; what it must record is the figure the observer read beside
  the figure the console shows, because the currency in the observation's `source` is
  read from the vendor's billing pages, not returned by the query, and the v1 pod page
  calls `costPerHr` "Runpod credits per hour". Record also whether the pod-scoped key
  is accepted by the GraphQL endpoint at all — the documentation describes key
  permission tiers without saying which tier `myself` needs.
- [ ] Before the first real response, prove the durable laptop supervisor, controller
  armer, acknowledgement channel, and long-running bootstrap/service entrypoint work
  together. The tracked Stage 04 tree now supplies `supervise.py`, `controller_armer.py`,
  and `bootstrap_main.py`. `test_launch_drill.py` drives the supervisor and the armer
  together offline with the timer's first durable write; it names
  `bootstrap_main` only as the argv handed to a fake starter and never runs it.
  `bootstrap_main.py` is proven separately, by `test_bootstrap_main.py` against
  fakes-only actions. The three have not run together anywhere, and the channel
  they share is unobserved against a real pod — Boot A above is that proof.
- [ ] Verify the pod-side timer receives the real pod identity, its ephemeral termination
  capability, and the sealed billing-cutoff margin; verify it writes an acknowledgement to
  the token-bound report path. The laptop controller must retain a receipt bound to the
  exact lease, pod, and hard deadline without capability material.
- [ ] Demonstrate the timer-startup backstop. If required environment data prevents it
  from constructing a provider object, it cannot terminate the pod; verify that the laptop
  supervisor detects and reconciles that `EXITED` pod rather than leaving volume billing
  unobserved.
- [ ] Run the actual GPU, driver, capability, VRAM, disk, chair-cache, and chair smoke-read
  preflight. The committed preflight and roster are fixture and planning evidence, not a
  measured assembly. `bootstrap_main`'s `PREFLIGHT` is now the real wiring (registry
  cache verification and a served golden-page smoke through `ServingManager`); before
  it can go green on real silicon each real row must be stamped proven, and the
  locked serving stack (see "The serving stack, re-planned and locked") must
  actually install and load. Record `nvidia-smi`'s driver and CUDA version **before**
  `uv sync --group pod` runs: the locked stack resolved `torch 2.13.0`, which pulls the
  CUDA 13 `nvidia-*` wheels, so a pod image whose driver predates CUDA 13 is a refusal
  to make before paying for the ~10 GB download, not after. Then record whether
  `uv sync --group pod` completed, how long the wheel download took, and whether each
  chair's weights loaded under `vllm 0.27.1`, since no offline check can answer that.
  Record, per chair, whether the pod-rendered golden page's witness was read back and
  what `nvidia-smi` reported around the read.
- [ ] After the run, bring the tree back with `verbatus fetch-run --run-id <id> --into
  <local root> --network-volume DATACENTER:VOLUME_ID` and record whether every object
  under `runs/<id>/` listed, fetched and reconciled with the tree's own manifests. The
  listing and `GetObject` path has never run against a real endpoint.
- [ ] At the first real response, require Spec 05's harness to publish an immutable
  run-tree artifact on the attached network volume before requesting the next response;
  interrupt the harness and read it back. Stage 04 does not own this response path. Repeat
  the response-as-arrival check for every live Testimonium in Spec 07 and every live
  Perlectio in Spec 08. A final-only export or stage-boundary backup does not satisfy this
  item.
- [ ] Verify shutdown rather than its acknowledgement: exact-pod GET reaches 404, the
  independent pod list omits that id, and `GET /billing/pods` returns non-empty, exact-pod
  billing rows covering creation through the requested cutoff. Record lag, empty records,
  or malformed/misattributed records as **unverified**, never zero. Confirm that the close
  report names the network volume's continuing hourly price and that no volume deletion
  occurred.

## Stage 04 deferrals

Ten rows in all: six items Tyrel accepted as deferred on the express condition that the
record survives to the pull request, then two disclosed by the pre-push review, then one
disclosed by the 2026-08-12 independent audit, then one found while wiring the pod run
seam. The first six are accepted deferrals
carried by his ruling, not oversights — each closes on the named condition, not on being
noticed again. **This table now records this branch's work against them; rows are kept
even once closed, marked, so the history of what closed each one is not lost.**

| # | What is deferred | Status |
|---|---|---|
| 04-1 | No durable laptop-supervisor driver in the tracked tree | **Closed.** `supervise.py` is that driver: a kernel-lock-owned, restart-safe process per lease, the `EXITED`-closes provider-lifecycle check, and seven offline drills against `FakeProvider` (crash-mid-heartbeat resume, lost-identity `BUSY`-then-close, provider-unreachable non-green, `EXITED`-closes-on-fresh-heartbeat, already-closed-verified no-op, second-driver refusal, foreign-owner-past-deadline break). |
| 04-2 | No controller armer that observes the real timer report | **Partly closed.** `controller_armer.py`'s `ChannelControllerArmer` performs the real read, arms only on a complete observation, and is fake-proven against every refusal state; `ObservingControllerArmer` performs the identical read and never arms. **New closes-when:** the first authorized boot observes an object written through the pod's mount appearing in the network volume's S3 view, and records the delay. Until then the channel is a designed path, not an observed one. |
| 04-3 | No runnable bootstrap/service entrypoint — `bootstrap.py` is a library module | **Closed.** `bootstrap_main.py` is bootstrap-and-hold: on green it does not exit, because `pod_timer.run_with_bootstrap` treats any child exit before the hard deadline — exit 0 included — as `completed-early` and closes the pod. Holding, not exiting, is the fix. |
| 04-4 | `pod_timer.py` startup failure leaves nothing able to terminate; pod goes `EXITED` and bills volume disk at double rate. The laptop supervisor is the only backstop and does not exist | **Rewritten.** The old "closes when 04-1 lands" line was wrong about the mechanism. The actual chain: the armer refuses and `launch._arm_or_close` closes the pod; if the launcher itself dies mid-arming, the already-started supervisor closes the unarmed lease once it goes stale; if the pod reaches `EXITED` after arming, `supervise.py`'s every-tick `provider.status()` read now sees it, because `ProviderStatus` carries `provider_state`. **There is no provider-side belt** — no TTL or `maxRuntime` field appears in the documented v1 create input, and `V2_MIGRATION.md` §3 records that none was found in the v2 create or update documentation either; the checklist row above still asks the live run to confirm. |
| 04-5 | Five untested seams: `cli.main` success path, `pod_timer.main`/`load_timer_context`, `SubprocessBootstrapActions.checkout_commit`, `sync_uv_environment` success path, `UrllibRunPodTransport` ordinary success | **Mostly still open — verified against the code, not assumed from the plan.** `SPEC_POD.md` §4.5 predicted `bootstrap_main`'s tests would close three of these five; `test_bootstrap_main.py`'s own module docstring says otherwise ("no git, uv, Hugging Face, or GPU probe is ever invoked here" — every test runs through a fakes-only `actions_factory`). Checked against the tree as it stands: `SubprocessBootstrapActions.checkout_commit`'s success path *is* covered, pre-existing in `test_pod_runtime.py` (`test_production_bootstrap_uses_absolute_tools_and_an_explicit_environment`, an injected-subprocess success). The other four remain untested — `sync_uv_environment`'s success path, `pod_timer.main`/`load_timer_context`'s success path, `cli.main`'s success path through the real `module:callable` factory resolution (`test_cli.py` exercises `cli._controller_armer`'s module:callable resolution on its own; what is untested is `cli.main`'s green path end to end, where existing tests monkeypatch `cli._provider`/`cli._controller_armer` before the call), and `UrllibRunPodTransport`'s ordinary success (only its redirect-refusal is exercised against real loopback servers). Closes when: the live pieces this branch adds (`supervise.py`, `controller_armer.py`, `bootstrap_main.py`) now exist to test against; writing those tests is not yet done. |
| 04-6 | Every RunPod field name is documented, not observed — no live call has been made | Open. Closes when: the first authorised live run. The instrument for closing it now exists: `cli.py --record-fixture` writes every exchange Boot A sees as a replayable, scrubbed fixture, so the offline suite can be re-founded on observed shapes rather than documented ones once the drill has run. |

Two more, found by the pre-push review of this branch and disclosed here rather than
fixed on an assumption. Both are Tyrel's to accept or send back:

| # | What is deferred | Status |
|---|---|---|
| 04-7 | The verified-close billing window is anchored on `lastStartedAt`, not on pod creation, and the check meant to catch a narrowed window compares that value against itself. A close can read `verified` over a partial total. Whether RunPod bills between creation and first start is documented-only; changing the query now would swap one unverified assumption for another | **Amended to the v2 route and now recorded page by page**, not closed. Tyrel ruled on 2026-08-11: *"V2 should be what we use."* `V2_MIGRATION.md` §2.3–2.4 confirms from the v2 pages what the ruling named: the pod object carries `createdAt` and `startedAt` as separate instants, and the billing envelope carries `metadata.query`'s resolved window beside per-record `startTime`/`endTime` and a `totalAmount`/`gpuAmount`/`cpuAmount`/`diskAmount` breakdown. §5 step 4 is the plan that re-anchors the verifier on `createdAt` and compares the requested window against the resolved one. Until that unit lands, this row's original text still describes the code. |
| 04-8 | `ChairCacheBootstrapAction` is constructed nowhere and tested nowhere — the README describes its at-most-one same-pin re-fetch as though it ships. The equivalent rule in `preflight.py` **is** exercised | **Partly closed.** `bootstrap_main.py` constructs it for the first time in the tracked tree (`_build_cache`), closing the "constructed nowhere" half. It is still exercised by no test — `test_bootstrap_main.py` is fakes-only and never calls `_build_cache` — and the at-most-one same-pin re-fetch is deliberately left unwired (`refetch_same_pin=None`) because `ChairRegistry` has no cache-clear verb, so the behaviour the deferral is about still does not ship. |

One more, found by the 2026-08-12 independent audit of this package and disclosed here
rather than papered over with a check that guesses at unobserved provider behaviour.
Tyrel's to accept or send back:

| # | What is deferred | Status |
|---|---|---|
| 04-9 | The billing verifier binds the capture's *declared* window and refuses any dated record outside it (allowing one hour of slack before the start, for the bucket containing creation), but nothing proves the returned buckets actually **cover** the window — a single in-window bucket can still total as a verified close. Whether RunPod posts contiguous hour buckets, or omits late ones under lag, is documented-only; a coverage gate written now would guess, and a wrong guess turns every real close red | Open. Closes when: the first authorised live run observes real bucket posting, then the coverage check is written against observations. `V2_MIGRATION.md` §2.4 records that v2's billing envelope names the window the provider *resolved* (`metadata.query`) and the record count, so under v2 the "declared window" half stops being the runtime's own echo; whether buckets fill it is still the live observation. |

One more, found while wiring the pod run seam. Tyrel's to accept or send back:

| # | What is deferred | Status |
|---|---|---|
| 04-10 | The real serving stack cannot be installed on a pod: `config/serving_recipes_real.toml`'s `transformers==4.57.1` requires `huggingface-hub<1.0` and the project pins `huggingface_hub==1.26.0`, so no `pod` dependency group locks (now "The serving stack, re-planned and locked") | **Closed.** Every named condition is met: the pair was re-planned onto `vllm 0.27.1` / `transformers 5.14.1` (researched against the four chairs' own `config.json` files and vLLM's v0.27.1 registry, cited in "The serving stack, re-planned and locked"), the `pod` group carries exactly those pins under Linux/x86_64 markers, `uv lock` resolves, `bootstrap.py` syncs `--group pod`, and `test_pod_run.py`'s expected failure is now a live test binding the group to the catalogue. `flash-attn` was dropped rather than re-pinned, for the reasons given there. **What this does not close:** no wheel has been installed and no weight loaded — the rows stay `preflight_state = "unproven"`, and the first boot is what proves the stack runs. |

## The boot plan: Boot A, the drill, before Boot B, the real thing

Roadmap item 7 (the checklist above) is one authorized live boot as written. This
branch's recommendation is to split it into two, because the checklist's most
load-bearing item — the acknowledgement channel — is also the one item no offline test
can measure, and a single boot that turns out unarmed both wastes the image pull and
the session and teaches a human to relax the safety check that just did its job.

**Boot A, the drill.** Cheapest available card. `hard_lifetime_seconds` around 900.
`ObservingControllerArmer`, never `ChannelControllerArmer`. `bootstrap_main --hold-only`,
never a real bootstrap plan. It closes its pod immediately by construction — the
arming verdict is False whatever it observes — and, as with any close, the result is
only green when GET-404, list absence and exact-pod billing all agree; a non-verified
close is reported as such and the supervisor keeps guarding it. It buys the four facts
no offline test can buy:
does the pod-written object appear in the S3 view, under which key, after how long, and
does the pod-scoped key actually hold delete and billing rights. Cost is minutes of a
cheap card.

**Boot B, the real thing.** Roadmap item 7 as written: `ChannelControllerArmer` with its
poll bound set from Boot A's measured delay, materialize, preflight, the full checklist,
no reading yet.

The argument for the split costs nothing in the failure case: Boot B alone would have
ended in the same immediate close, having also wasted the image pull and the session.
It costs a few dollars in the success case, for the drill's own minutes on a cheap card.

**What this needs from Tyrel, named plainly, and nothing else in U1–U6 does:**

- `config/spend.toml` values and the GPU class for both boots.
- The S3 access/secret keys in the launching shell (needed before either boot's
  `TimerReportChannel` can be constructed).
- In-session permission for Boot A, and **separately**, in-session permission for
  Boot B — GOVERNANCE 8 and hard rule 2 read as one gate per exact action, not one
  gate for "the boot."
- Whether he accepts the two-boot split at all, rather than the single boot the
  roadmap names.
- Nothing else. The GraphQL account-balance observer that used to head this list is
  built and is the adapter's default; `boot_a_request.py` renders the drill request from
  his configured policy the moment `config/spend.toml` leaves `unconfigured`.

**One drill-specific finding, recorded and now closed.** `launch.py` used to bind
the launch token into the timer's `--report-path` at sealing time but not into the
`--report-path` inside the nested `--bootstrap-command-json`, and `bootstrap_main`
refuses a report path that lacks the token when `VERBATUS_LAUNCH_TOKEN` is in the pod's
environment — which it is. In Boot A that refusal was harmless in outcome:
`bootstrap_main --hold-only` exited non-zero at once, the timer read the child's exit as
`completed-early` and closed, which is the same immediate close the drill armer already
forces — but the hold-only journal was never written, so Boot A never observed the hold.
`_bind_report_path_to_launch` now folds the same sealed token into both the timer's own
`--report-path` and, when the nested bootstrap argv carries one, its `--report-path`
too — proven through the real plan/argv path in `test_bootstrap_main.py` (a `--hold-only`
plan built from a sealed request reaches the hold rather than refusing). `HANDOFF.md`
carries the closed finding.

**Alternatives considered and declined, one line each.** Push the acknowledgement out
of the pod over the notify topic — no; it puts a bearer secret in the pod and turns a
safety receipt into an unauthenticated message whose delivery this README already says
must never change a decision. SSH or exec into the pod to read the report — no; a
second transport and a second credential to prove a fact the volume already holds.
Keep `arming.FailClosedControllerArmer` and defer 04-2 past the first boot — no; the
boot cannot be green, and this README already names arming a blocker. Do the v2
migration inside this section — no; it buries ruling-compliance work under controller
work and contradicts the working style of larger sections done one at a time.

## If your task seems to need one

Stop and say so, with: what you would start, the hourly rate, roughly how long, what it
buys, and how it gets turned off. Then wait. That paragraph is what Tyrel needs in order
to give or refuse the gate, and assembling it is usually quick.

In an unattended session this is a **decision** notification the moment you discover it —
not when you stop — plus a row in `workbench/active/DEFERRED_ACTIONS.md`. Carry on with
everything that does not depend on it.
