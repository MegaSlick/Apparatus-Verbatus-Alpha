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
  another on a money path. The migration is its own section of work — record
  each v2 page and its check date the way the four v1 pages are recorded here
  today, then touch code — not something this unit does.

  **A second, independent gap the ruling does not close on its own: there is no
  account-balance observer anywhere in this tree.** `RunPodProvider.balance_observer`
  is an injected callable with no default; `observe_account_balance` raises
  "RunPod account balance source was not configured" until one is supplied, and
  `spend.py`'s balance-floor gate then refuses every paid action. This is
  GraphQL-only under both REST versions (`myself { clientBalance currentSpendPerHr }`)
  and the version move does not touch it. No live pod — not even the Boot A
  drill below — can be created until this observer exists. `fake_provider.py`
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
  `status` verb gains a read-only supervisor block per open lease — running or
  absent (by peeking the lock, exactly as `OperatorSurface._exclusive_paid_launch`
  already does for the paid-launch claim, never by trusting a pid), identity
  file age, last tick result, last close record, and the volume's ongoing
  hourly price — with no new verb. Exit status follows `cli.py`'s convention
  (0 guarded, 2 nothing touched, 3 go and look) with one addition: a durable
  lease this run confirmed active going missing or unreadable exits 3, not 2,
  since the pod it guarded may still be billing. `main()` catches
  `BaseException`, not only its own refusal type, so a bad `--provider-factory`
  reference or a malformed `spend.toml` still writes a durable final record
  before exiting — a detached process's traceback on stderr goes unwatched
  otherwise. Six offline drills against `FakeProvider` and an injected clock
  prove it: a crash mid-heartbeat resuming ownership with no close; a lost
  identity file reporting `BUSY` inside the timeout and closing after it,
  naming which; a provider unreachable on `status`/`verify_absent`/
  `capture_cost` reporting non-green without ever fabricating a verified
  close; an `EXITED` pod closing on a fresh heartbeat, naming the volume's
  ongoing rate; an already-`closed-verified` lease exiting without touching
  the provider; and a second driver refused before it ever reaches the lease.
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
  model-store materialization — **deferral 04-8 is closed.** The transfer
  target stays optional: no submission manifest on the volume is a vacuous
  success and this process needs no object-store client at all; a manifest
  present with no configured target is a refusal, never a silently skipped
  upload. `PREFLIGHT` stays honestly red: no production `ChairCacheVerifier` or
  `SmokeReader` exists anywhere in this repository, only fixture adapters in
  tests and the operator's offline surface — Spec 05 owns that real assembly,
  and this file wires named stand-ins rather than borrow a fixture pass into a
  live pod's preflight. Both the ordinary hold and the `--hold-only` drill
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
  caveat.** The journal is written to the volume under the launch-bound name;
  the laptop reads it with `TimerReportChannel.read`. Until the first boot
  proves the S3 view, this is **a designed path, not an observed one.**

  **Where this unit's tests differ from what the spec predicted, said plainly.**
  `test_bootstrap_main.py` states in its own module docstring that "no git,
  uv, Hugging Face, or GPU probe is ever invoked here" — every test drives
  `bootstrap_main` through a fakes-only `actions_factory`, never through
  `SubprocessBootstrapActions` or a real subprocess. `SPEC_POD.md`'s §4.5
  predicted this unit's tests would close three of deferral 04-5's five
  untested seams; verified against the code as it stands, they do not. See
  the rewritten 04-5 row below for what is and is not actually covered. the same price display, ceiling calculation, and typed phrase to
  create and adopt. The phrase is **derived from the preview**: it names the action, the
  subject and both hourly rates just displayed, and carries a random single-use
  challenge only a preview issued in this process can supply, binding the
  acknowledgement to that preview. A refused confirmation neither reproduces the
  phrase nor spends the challenge — a typo does not burn the preview. It is not
  Tyrel's live-pod permission and does not claim a script cannot derive it. The hourly and
  lifetime ceiling checks include both pod and attached volume while the hard lifetime is
  running, and the ceiling is applied a second time to the price the provider *actually*
  returned — a created pod that bills above it is closed immediately rather than left
  running. `config/spend.toml` is deliberately unconfigured, so both paid paths refuse
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
  same-pin re-fetch — see deferral 04-8: the class implementing it is constructed nowhere
  and tested nowhere, so this describes the design rather than a shipped behaviour;
  another mismatch is red and names the chair.
- `preflight.py` measures or receives CUDA/driver/capability/VRAM/disk facts, selects a
  single-resident plan only from `config/pod_placement.toml`, verifies every configured
  chair, and validates a stochastic proof-page read by shape, non-emptiness, and format.
  **Serving is sequential** — one model at a time, as much of the card as stays stable,
  next model after — so every tier is single-resident and what a tier changes is the
  engine memory fraction, context cap, pixel cap and batch size that one model gets. The
  table ships prebuilt profiles for the cards this project actually rents and falls back
  to computed placement for anything else; the report names which plan it chose and why.
  Its utilization fields are measurements, not a claim that a card is "saturated". It
  always labels this stage fixture/planning-only; Spec 05 owns a real-assembly claim.

The local command surface is `python -m operations.pod.cli`. It requires explicit
untracked provider and controller-armer factories plus a request file, so this repository
contains neither a credential, a personal provider default, nor an implicit controller
process. It prints the previewed current price and ceilings before prompting for the exact
typed confirmation; EOF is a refusal. A preview that is itself refused prints its price
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
is the only backstop for that case. A refusal raised *after* the timer is built now
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
`controller_armer.py`, and `bootstrap_main.py` landing) the six `supervise` drills named
above, the arming states in `test_controller_armer.py`, and the hold/refusal/scrub paths
in `test_bootstrap_main.py`. A full real-chair preflight
is not demonstrated: the committed roster is still fixture-only and has no real GPU or
model-service measurement.

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
  `bootstrap_main --hold-only`. It is designed to end in an immediate close and cannot
  leave a pod up. Record whether the pod-written object appears in the S3 view, under
  which key, after how long, and whether the pod-scoped key actually holds delete and
  billing rights. See "The boot plan" below for the full split and what it needs from
  Tyrel.
- [ ] Record the account-balance source and that it reports US dollars rather than
  credits or another currency. **This item cannot be run at all yet**: no GraphQL
  balance observer (`myself { clientBalance currentSpendPerHr }`) exists anywhere in
  this tree, so `RunPodProvider.observe_account_balance` raises and every paid action
  refuses before either boot can happen. Building that observer is next-section work,
  not something either boot can substitute for.
- [ ] Before the first real response, prove the durable laptop supervisor, controller
  armer, acknowledgement channel, and long-running bootstrap/service entrypoint work
  together. The tracked Stage 04 tree now supplies `supervise.py`, `controller_armer.py`,
  and `bootstrap_main.py`; what remains unproven is whether the channel they share
  behaves the way `TimerReportChannel` requires against a real pod — Boot A above is
  that proof.
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
  measured assembly.
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

Nine rows in all: six items Tyrel accepted as deferred on the express condition that the
record survives to the pull request, then two disclosed by the pre-push review, then one
disclosed by the 2026-08-12 independent audit. The first six are accepted deferrals
carried by his ruling, not oversights — each closes on the named condition, not on being
noticed again. **This table now records this branch's work against them; rows are kept
even once closed, marked, so the history of what closed each one is not lost.**

| # | What is deferred | Status |
|---|---|---|
| 04-1 | No durable laptop-supervisor driver in the tracked tree | **Closed.** `supervise.py` is that driver: a kernel-lock-owned, restart-safe process per lease, the `EXITED`-closes provider-lifecycle check, and six offline drills against `FakeProvider` (crash-mid-heartbeat resume, lost-identity `BUSY`-then-close, provider-unreachable non-green, `EXITED`-closes-on-fresh-heartbeat, already-closed-verified no-op, second-driver refusal). |
| 04-2 | No controller armer that observes the real timer report | **Partly closed.** `controller_armer.py`'s `ChannelControllerArmer` performs the real read, arms only on a complete observation, and is fake-proven against every refusal state; `ObservingControllerArmer` performs the identical read and never arms. **New closes-when:** the first authorized boot observes an object written through the pod's mount appearing in the network volume's S3 view, and records the delay. Until then the channel is a designed path, not an observed one. |
| 04-3 | No runnable bootstrap/service entrypoint — `bootstrap.py` is a library module | **Closed.** `bootstrap_main.py` is bootstrap-and-hold: on green it does not exit, because `pod_timer.run_with_bootstrap` treats any child exit before the hard deadline — exit 0 included — as `completed-early` and closes the pod. Holding, not exiting, is the fix. |
| 04-4 | `pod_timer.py` startup failure leaves nothing able to terminate; pod goes `EXITED` and bills volume disk at double rate. The laptop supervisor is the only backstop and does not exist | **Rewritten.** The old "closes when 04-1 lands" line was wrong about the mechanism. The actual chain: the armer refuses and `launch._arm_or_close` closes the pod; if the launcher itself dies mid-arming, the already-started supervisor closes the unarmed lease once it goes stale; if the pod reaches `EXITED` after arming, `supervise.py`'s every-tick `provider.status()` read now sees it, because `ProviderStatus` carries `provider_state`. **There is no provider-side belt** — no TTL or `maxRuntime` field appears in the documented v1 create input — and the first-boot checklist should ask whether v2 offers one. |
| 04-5 | Five untested seams: `cli.main` success path, `pod_timer.main`/`load_timer_context`, `SubprocessBootstrapActions.checkout_commit`, `sync_uv_environment` success path, `UrllibRunPodTransport` ordinary success | **Mostly still open — verified against the code, not assumed from the plan.** `SPEC_POD.md` §4.5 predicted `bootstrap_main`'s tests would close three of these five; `test_bootstrap_main.py`'s own module docstring says otherwise ("no git, uv, Hugging Face, or GPU probe is ever invoked here" — every test runs through a fakes-only `actions_factory`). Checked against the tree as it stands: `SubprocessBootstrapActions.checkout_commit`'s success path *is* covered, pre-existing in `test_pod_runtime.py` (`test_production_bootstrap_uses_absolute_tools_and_an_explicit_environment`, an injected-subprocess success). The other four remain untested — `sync_uv_environment`'s success path, `pod_timer.main`/`load_timer_context`'s success path, `cli.main`'s success path through the real `module:callable` factory resolution (existing tests inject `cli._provider`/`cli._controller_armer` directly, bypassing that resolution), and `UrllibRunPodTransport`'s ordinary success (only its redirect-refusal is exercised against real loopback servers). Closes when: the live pieces this branch adds (`supervise.py`, `controller_armer.py`, `bootstrap_main.py`) now exist to test against; writing those tests is not yet done. |
| 04-6 | Every RunPod field name is documented, not observed — no live call has been made | Open. Closes when: the first authorised live run. |

Two more, found by the pre-push review of this branch and disclosed here rather than
fixed on an assumption. Both are Tyrel's to accept or send back:

| # | What is deferred | Status |
|---|---|---|
| 04-7 | The verified-close billing window is anchored on `lastStartedAt`, not on pod creation, and the check meant to catch a narrowed window compares that value against itself. A close can read `verified` over a partial total. Whether RunPod bills between creation and first start is documented-only; changing the query now would swap one unverified assumption for another | **Amended to the v2 route**, not closed on this branch. Tyrel ruled on 2026-08-11: *"V2 should be what we use"* — REST v2 documents `createdAt`, a `startTime` snap, and a per-pod cost breakdown, which the ruling itself names as closing this finding. The migration is next-section work (see the v1/v2 paragraph above); until it lands, this row's original text still describes the code, and the first authorised live run still observes the two instants under whichever version is live at the time. |
| 04-8 | `ChairCacheBootstrapAction` is constructed nowhere and tested nowhere — the README describes its at-most-one same-pin re-fetch as though it ships. The equivalent rule in `preflight.py` **is** exercised | **Closed.** `bootstrap_main.py` constructs it for the first time in the tracked tree. |

One more, found by the 2026-08-12 independent audit of this package and disclosed here
rather than papered over with a check that guesses at unobserved provider behaviour.
Tyrel's to accept or send back:

| # | What is deferred | Closes when |
|---|---|---|
| 04-9 | The billing verifier binds the capture's *declared* window and refuses any dated record outside it (allowing one hour of slack before the start, for the bucket containing creation), but nothing proves the returned buckets actually **cover** the window — a single in-window bucket can still total as a verified close. Whether RunPod posts contiguous hour buckets, or omits late ones under lag, is documented-only; a coverage gate written now would guess, and a wrong guess turns every real close red | the first authorised live run observes real bucket posting, then the coverage check is written against observations |

## The boot plan: Boot A, the drill, before Boot B, the real thing

Roadmap item 7 (the checklist above) is one authorized live boot as written. This
branch's recommendation is to split it into two, because the checklist's most
load-bearing item — the acknowledgement channel — is also the one item no offline test
can measure, and a single boot that turns out unarmed both wastes the image pull and
the session and teaches a human to relax the safety check that just did its job.

**Boot A, the drill.** Cheapest available card. `hard_lifetime_seconds` around 900.
`ObservingControllerArmer`, never `ChannelControllerArmer`. `bootstrap_main --hold-only`,
never a real bootstrap plan. It is designed to end in an immediate close and cannot
leave a pod up, whatever it observes. It buys the four facts no offline test can buy:
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
- The GraphQL account-balance observer described above must exist before either boot
  can create anything — that is engineering, not a decision reserved for him, but no
  boot is possible until it is built.

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
