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

A pod is off when **provider state says it is gone and billing has stopped** — two
separate observations, both made after the fact. Until both are true, the session reports
it as *possibly still running* and says so plainly, on his phone if he is away, because
this is exactly the "decision" class of notification.

A shutdown that cannot be verified is not a tidy-up for next session. Say it now.

## What exists here

Nothing yet — no launcher, no shutdown client, no billing verifier. When they are built:

- Shutdown is written and proven **before** launch is, and the launcher's first act is to
  prove the shutdown path works.
- Every launch records what was started, when, at what rate, and on whose permission.
- A verifier that cannot reach the provider reports failure, never success: a check that
  cannot run is a failure, not a pass (GOVERNANCE 10).
- Nothing here reads a credential from a tracked file.

## If your task seems to need one

Stop and say so, with: what you would start, the hourly rate, roughly how long, what it
buys, and how it gets turned off. Then wait. That paragraph is what Tyrel needs in order
to give or refuse the gate, and assembling it is usually quick.

In an unattended session this is a **decision** notification the moment you discover it —
not when you stop — plus a row in `workbench/active/DEFERRED_ACTIONS.md`. Carry on with
everything that does not depend on it.
