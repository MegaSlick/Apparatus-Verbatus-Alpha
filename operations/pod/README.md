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
window begins no later than pod creation and reaches the provider-resolved cutoff requested
by close. Billing can lag; this instrument never claims “no future charges.” An empty,
unreachable, misattributed, narrowed, or stale billing response is *unverified*, not zero.

A shutdown that cannot be verified is not a tidy-up for next session. Say it now.

## What exists here

The fake-first runtime is now present. It has not started, inspected, or billed a live
pod. This build's no-live-call boundary applies even to the otherwise read-only provider
operations above: every adapter check used an in-memory fake transport. RunPod's public
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
  availability. Every endpoint shape the adapter reads is named, with its
  documentation URL and the date it was checked, in that file's module
  docstring. `fake_provider.py` has a fixed local price sheet, exact-token crash
  recovery, and injected failures only.
- `shutdown.py` is written before launch. Its only green close result requires a
  termination request, exact-pod GET-404, independent pod-list absence, and a
  non-empty provider billing capture through its named window and cutoff. The
  generic verifier binds all three returned observations to the requested pod,
  and requires billing coverage from creation through the requested cutoff.
  Empty/unreachable or mismatched billing is **UNVERIFIED**, never zero. Billing
  lags termination, so the capture is retried a bounded, configured number of
  times before that verdict; an adapter that can distinguish "not posted yet"
  from "nothing to post" reports `pending-reconciliation` instead. Neither state
  claims no future charge is possible.
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
  subject and both hourly rates just displayed, binding the acknowledgement to that
  preview. It is not Tyrel's live-pod permission and does not claim a script cannot derive it. The hourly and
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
  and preflight steps. A cache digest mismatch receives at most one same-pin re-fetch;
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
typed confirmation; EOF is a refusal. A request must make the provider-neutral timer the
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
own startup fails (a missing environment value, a deadline already passed) it has no
provider object yet and can terminate nothing; the pod goes `EXITED`, which bills volume
disk at double the running rate, and the laptop supervisor above is the only backstop for
that case until it exists.

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

## If your task seems to need one

Stop and say so, with: what you would start, the hourly rate, roughly how long, what it
buys, and how it gets turned off. Then wait. That paragraph is what Tyrel needs in order
to give or refuse the gate, and assembling it is usually quick.

In an unattended session this is a **decision** notification the moment you discover it —
not when you stop — plus a row in `workbench/active/DEFERRED_ACTIONS.md`. Carry on with
everything that does not depend on it.
