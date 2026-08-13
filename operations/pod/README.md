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
  RunPod adapter and speaks **REST v1** (`rest.runpod.io/v1`), the route spec 04
  names and the only one whose published pod-create schema proves the two facts
  this runtime must have before it can spend: an explicit `interruptible=false`
  and a primary `dockerStartCmd`. REST v2 (`api.runpod.io/v2`) is not used: its
  own overview still says it is in beta and may change before general
  availability. Every endpoint whose response *shape* the adapter parses is
  named, with its documentation URL and the date it was checked, in that file's
  module docstring; DELETE's response body is never parsed, only its status
  code, so it is not on that list. `fake_provider.py` has a fixed local price sheet, exact-token crash
  recovery, and injected failures only.
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
  running. `config/spend.toml` is deliberately unconfigured, so both paid paths refuse
  until Tyrel supplies a reviewed policy. A configured policy must set a
  `billing_cutoff_margin_seconds` value within the code-owned 0–3600-second envelope;
  the exact value is sealed into both shutdown controllers and the pending-create
  recovery record, while an out-of-bounds value refuses rather than being clamped. It
  also displays an `account_balance_floor_usd` manual reserve. The runtime does not
  observe account balance: the commented `$50.00` template value is unverified and must
  be checked against RunPod before a live run, not treated as evidence that the reserve
  is actually available.
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
typed confirmation; EOF is a refusal. Its exit status never reads as "nothing happened"
when something did: 0 is a guarded success, 2 a refusal whose result names no pod, lease,
or close, 3 a non-green outcome that observed or touched a real pod, wrote a lease, or
attempted a close — go and look; an interrupt mid-action prints an `interrupted` record
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
occurred. The tracked tree also does not yet supply the durable laptop-supervisor driver,
the controller armer that observes the real timer report, or a runnable bootstrap/service
entrypoint: `bootstrap.py` is a finite library module, while `pod_timer.py` deliberately
closes when its child exits. Those integration pieces are therefore blockers before the
gated demonstration, not capabilities this fake suite proves. A pod-side process may be
destroyed by its own DELETE before it can observe GET-404, list absence, or lagging billing;
the real controller design must leave final verification and reconciliation to surviving
laptop/restart machinery rather than infer it from the pod process. If `pod_timer.py`'s
own startup fails **before a provider-backed timer exists** (a missing environment value,
a deadline already passed) it can terminate nothing; the pod goes `EXITED`, which bills
volume disk at double the running rate, and the laptop supervisor above is the only
backstop for that case until it exists. A refusal raised *after* the timer is built now
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
cache, smoke-read, laptop-controller, and pod-timer paths. A full real-chair preflight
is not demonstrated: the committed roster is still fixture-only and has no real GPU or
model-service measurement.

## First gated live-pod checklist

This is a checklist for one authorized live demonstration, not authorization to create a
pod. Record the exact pod id, timestamps, provider response, and whether each item is
**verified**, **unverified**, or **not run**. The RunPod field names in this tree are
documented shapes, not observed behavior; no unchecked item may be reported as a pass.

- [ ] Record Tyrel's current-session authorization, the synthetic workload, the
  configured spend ceilings, and `account_balance_floor_usd`. The floor is a manual
  reserve policy, not a current-balance observation. Record any actual balance observation
  separately, including active obligations and the maximum additional liability of this
  run; the runtime has no account-balance provider verb.
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
- [ ] Before the first real response, prove the durable laptop supervisor, controller
  armer, acknowledgement channel, and long-running bootstrap/service entrypoint work
  together. The tracked Stage 04 tree does not currently supply those live integrations.
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
noticed again.

| # | What is deferred | Closes when |
|---|---|---|
| 04-1 | No durable laptop-supervisor driver in the tracked tree | live-integration pieces are built |
| 04-2 | No controller armer that observes the real timer report | same |
| 04-3 | No runnable bootstrap/service entrypoint — `bootstrap.py` is a library module | same |
| 04-4 | `pod_timer.py` startup failure leaves nothing able to terminate; pod goes `EXITED` and bills volume disk at double rate. The laptop supervisor is the only backstop and does not exist | 04-1 lands |
| 04-5 | Five untested seams: `cli.main` success path, `pod_timer.main`/`load_timer_context`, `SubprocessBootstrapActions.checkout_commit`, `sync_uv_environment` success path, `UrllibRunPodTransport` ordinary success | the live pieces above exist to test against |
| 04-6 | Every RunPod field name is documented, not observed — no live call has been made | the first authorised live run |

Two more, found by the pre-push review of this branch and disclosed here rather than
fixed on an assumption. Both are Tyrel's to accept or send back:

| # | What is deferred | Closes when |
|---|---|---|
| 04-7 | The verified-close billing window is anchored on `lastStartedAt`, not on pod creation, and the check meant to catch a narrowed window compares that value against itself. A close can read `verified` over a partial total. Whether RunPod bills between creation and first start is documented-only; changing the query now would swap one unverified assumption for another | the first authorised live run observes the two instants |
| 04-8 | `ChairCacheBootstrapAction` is constructed nowhere and tested nowhere — the README describes its at-most-one same-pin re-fetch as though it ships. The equivalent rule in `preflight.py` **is** exercised | the live bootstrap path is built, or the class is removed |

One more, found by the 2026-08-12 independent audit of this package and disclosed here
rather than papered over with a check that guesses at unobserved provider behaviour.
Tyrel's to accept or send back:

| # | What is deferred | Closes when |
|---|---|---|
| 04-9 | The billing verifier binds the capture's *declared* window and refuses any dated record outside it (allowing one hour of slack before the start, for the bucket containing creation), but nothing proves the returned buckets actually **cover** the window — a single in-window bucket can still total as a verified close. Whether RunPod posts contiguous hour buckets, or omits late ones under lag, is documented-only; a coverage gate written now would guess, and a wrong guess turns every real close red | the first authorised live run observes real bucket posting, then the coverage check is written against observations |

## If your task seems to need one

Stop and say so, with: what you would start, the hourly rate, roughly how long, what it
buys, and how it gets turned off. Then wait. That paragraph is what Tyrel needs in order
to give or refuse the gate, and assembling it is usually quick.

In an unattended session this is a **decision** notification the moment you discover it —
not when you stop — plus a row in `workbench/active/DEFERRED_ACTIONS.md`. Carry on with
everything that does not depend on it.
