# Pod runtime — handoff (U6's record, on `work/pod-runtime`, over units 1–5 and U4)

This branch's own job (U6) is the record, not new code: `README.md` above and this
file. Everything it describes was built and tested by earlier units on this branch;
this file's claims are checked against that code, not against the plan that preceded
it. Where `SPEC_POD.md` predicted something the code does not do, that is said below
rather than repeated as fact.

## What landed, unit by unit

**U1 — the provider seam learns lifecycle state.** `ProviderStatus` gained
`provider_state: str | None`. `RunPodProvider.status` parses `desiredStatus` from the
body it already fetches; `FakeProvider.status` returns `record.state` plus a
`set_pod_state` test control. No eighth verb. This is the fact 04-4's real fix
depends on: an `EXITED` pod is *present*, and presence alone cannot tell a supervisor
that.

**U2 — the timer's acknowledgement becomes a record.** Every durable report
`pod_timer.py` writes now carries `schema: "pod-report.v1"` and an identity block
(`lease_id`, `pod_id`, `hard_deadline` in `lease._stamp`'s exact spelling) plus
`acknowledged_at`, captured once when `TimerContext` wraps the constructed timer and
threaded through every later write. `models.validate_pod_report_identity` is the one
shared refusal every reader of a pod report — a restarting supervisor, an armer — runs
before trusting what it reads.

**U3 — `supervise.py`, the durable laptop driver (closes 04-1).** A restart-safe
process per lease: ownership by kernel `fcntl.flock`, never by a recorded pid, because
a pid is reused after a laptop reboot and a bare `os.kill(pid, 0)` check fails open.
Every tick also re-reads `provider.status()` and closes on any lifecycle word other
than `RUNNING` — the real 04-4 fix. Seven offline drills, all against `FakeProvider` and
an injected clock, prove: crash-mid-heartbeat resume, lost-identity `BUSY`-then-close,
provider-unreachable non-green, `EXITED`-closes-on-fresh-heartbeat, already-closed exit
with no provider call, a refused second driver, and a foreign owner's deadline passing
while the supervisor breaks rather than spins.

**U4 — `controller_armer.py`, the armer and a channel that cannot lie (closes 04-2
partly).** `ChannelControllerArmer` starts the supervisor first, detached, before
polling — so a launcher that dies mid-poll leaves a live supervisor over an unarmed
lease that the supervisor itself closes once it goes stale. It writes the identity
handover itself, carrying this launch's own owner token, before starting the process
(the armer's own decision — see the README's controller_armer.py bullet for why the
alternative closes the pod it was meant to guard). It heartbeats the lease on every
poll wait, so the supervisor it just started does not close the pod mid-poll while the
launcher is doing exactly what it is meant to do. `TimerReportChannel.read` returns
bytes or `None`-only-when-proven-absent; a bad credential or unreachable channel raises
rather than reading as "not yet." `ObservingControllerArmer` performs the identical
read and never arms — the drill armer, built specifically because nothing offline can
measure whether an object a pod writes through its volume mount appears in the
network volume's S3 view. Both are wired through `cli.py --controller-armer-factory`,
already present in the tree before this unit.

Later review closed five more findings against this unit specifically (see the commit
history for the reasoning on each): the supervisor's exit status is now checked on
every poll pass, not only before/after the loop; a stolen identity handover (a
foreign `owner_token` in the file) refuses rather than arms; a heartbeat timeout too
short relative to the poll interval is refused at preflight rather than discovered
mid-arming; an unstartable supervisor command is caught at preflight; and
`_read_bounded`'s accumulation is bounded to its declared `limit` even against an
over-serving body.

**U5 — `bootstrap_main.py`, bootstrap-and-hold (closes 04-3; partly closes 04-8).** On green this
process holds rather than exits, because `pod_timer.run_with_bootstrap` treats any
child exit before the hard deadline as `completed-early` and closes the pod.
`ChairCacheBootstrapAction` is constructed here for the first time in the tracked
tree. `PREFLIGHT` stays honestly red — no production `ChairCacheVerifier` or
`SmokeReader` exists anywhere in this repository; Spec 05 owns that. Its tests
drive hold survival, red-step immediate exit, every named refusal, env scrubbing, and
both no-action modes — **all against a fakes-only `actions_factory`**, per the test
file's own module docstring ("no git, uv, Hugging Face, or GPU probe is ever invoked
here"). `SPEC_POD.md` §4.5 predicted these tests would also close three of deferral
04-5's five untested seams; checked against the code, they do not — see the README's
rewritten 04-5 row for exactly what is and is not covered.

**U6 (this branch's own unit) — the record.** `README.md` rewritten to describe
`supervise.py`, `controller_armer.py`, and `bootstrap_main.py` as they actually
behave; the deferral table rewritten per unit above; the v1/v2 paragraph replaced to
name Tyrel's 2026-08-11 ruling and the 2026-11-15 v1 retirement date, with the current
v1 code named as a session decision that predates and contradicts that ruling; two new
checklist rows (the drill boot, and the account-balance-observer gap); a
`~/.claude/WAKE_PLAYBOOK.md` line under the supervisor description; and this file.

**After U6 — `test_launch_drill.py`.** Two further commits landed on this branch
after U6's record was written, adding `operations/pod/test_launch_drill.py`: seven
offline drills that drive the armer, the pod's own report, and the `supervise` driver
together — a green launch, a launcher that dies mid-poll, a report that never appears,
an `EXITED` pod, both close-ordering directions, and the observing armer — with no
network and no live pod. This file was not updated for it at the time; see the "No
code changed by this unit" note below, which is about U6's own diff, not the branch as
a whole.

## What each unit actually proved (not what it was asked to prove)

- U1: an `EXITED` pod is observably distinguishable from a `RUNNING` one through the
  seven-verb seam, on fakes.
- U2: a pod-timer report can carry evidence a restarting reader can mechanically
  refuse if it disagrees with the lease.
- U3: a laptop-side process can survive its own crash and keep guarding one lease
  without ever risking two drivers on the same pod, on fakes.
- U4: the full arm-or-refuse procedure runs end to end against an in-memory channel,
  including every named refusal state, and the drill armer structurally cannot arm.
- U5: the bootstrap-and-hold shape holds under a green run and closes immediately
  under a red one, and its refusals fire in isolation — **against fakes only**. It did
  not prove that `SubprocessBootstrapActions.checkout_commit`, `sync_uv_environment`,
  `pod_timer.main`, or `UrllibRunPodTransport`'s ordinary success path work against
  anything real. It did not prove that `cli.main`'s `--provider-factory`/
  `--controller-armer-factory` string resolution (via `importlib`) works end to end for
  a full green launch — the existing tests that drive `cli.main` inject
  `cli._provider`/`cli._controller_armer` directly.

**None of U1–U5 touched a real provider, a real pod, or a real network volume.**
Every claim above is fakes-and-loopback-servers only. The one thing no unit on this
branch could prove, and the one thing the whole package now waits on, is whether an
object a pod writes through its volume mount appears in the network volume's S3 view
— see the README's "designed path, not an observed one" language and the boot plan.

## What the first authorized boot must observe

In order, because each depends on the one before it existing to observe against:

1. **Boot A (the drill) must observe**: whether the S3 `GetObject` read in
   `volume_s3.py` actually returns bytes for an object the pod wrote through its
   mount; under what key; after how long (this sets `CONTROLLER_ARMING_TIMEOUT_SECONDS`
   / `CONTROLLER_ARMING_POLL_SECONDS`'s real-world basis, currently a code-owned bound
   with no measurement behind it); whether the pod-scoped API key actually holds
   delete and billing rights; and that `ObservingControllerArmer`'s evidence file is
   written and its contents match what actually happened. Boot A cannot leave a pod
   running by construction.
2. **Boot B must observe**: everything the existing "First gated live-pod checklist"
   in the README names — REST-v1 (or, if the v2 migration has landed by then, v2)
   create-response field names, exact launch-token recovery, `GET /pods` pagination,
   the checksummed transfer end to end, the network volume surviving a process
   restart, the pod-side timer's real acknowledgement write, the timer-startup
   backstop, real preflight, and verified shutdown against provider state and
   billing — with the poll bound tuned from what Boot A measured.
3. **Neither boot can happen at all** until a GraphQL account-balance observer
   exists — `RunPodProvider.balance_observer` has no default and
   `observe_account_balance` raises without one. This is engineering work, not a
   decision reserved for Tyrel, but it blocks both boots equally.

## The `supervise` drills, by name

All live in `operations/pod/test_supervise.py`, offline against `FakeProvider`
and an injected clock. The first six are U1's; the seventh was added later, closing
a spin CodeRabbit found in the run loop:

1. Crash mid-heartbeat — a second driver instance over the same identity file resumes
   ownership; no close.
2. Identity file lost — reports `BUSY` inside the heartbeat timeout, closes after it,
   and names why.
3. Provider unreachable — `inject_failure` on `status`/`verify_absent`/
   `capture_cost`; reports non-green, never crash-loops, never fabricates a verified
   close, keeps ticking.
4. Pod `EXITED` while the heartbeat is fresh — closes, and the report names the
   volume's continuing rate.
5. Lease already `closed-verified` — exits without touching the provider.
6. Two drivers — the second is refused before it ever reaches the lease; it never
   closes the first driver's pod.
7. Foreign owner past its own hard deadline — a lease owned by another, still
   heartbeating driver ends the run at that deadline (exit 3, go and look) instead
   of hot-looping identity rewrites forever.

## What this unit did not do, and why

- **No code changed by this unit.** U6's brief is the record; `models.py`,
  `supervise.py`, `controller_armer.py`, `bootstrap_main.py`, `pod_timer.py`,
  `cli.py`, and `provider_runpod.py` are read here, not edited.
- **No v2 migration, no balance observer.** `SPEC_POD.md` §4.0 draws the boundary
  explicitly: this section (U1–U6) is the controllers; the v2 migration and the
  GraphQL balance observer are the next section's work, kept separate so
  ruling-compliance work stays legible rather than buried under controller code. Both
  gaps are named plainly in the README and in this file rather than silently
  deferred.
- **No test was added or changed.** The one thing checked for was whether a README
  test exists that this unit had to keep green — `grep -rn "README" operations/pod/test_*.py`
  finds one match, in `test_pod_runtime.py`, and it checks `config/README.md`'s
  documentation, not `operations/pod/README.md`. No test in this package currently
  reads `operations/pod/README.md` or `HANDOFF.md`, so there was nothing to keep
  green by that route; `sh .githooks/check-documents.sh` was run instead and passed.

## Rule-13 decisions this unit made

- **04-5's status is written as "mostly still open," not "mostly closed," because
  that is what the code shows.** The brief for this unit named "mostly closed" as the
  expected wording; verifying against `test_bootstrap_main.py`'s own module docstring
  and a direct search of `test_pod_runtime.py` and `test_provider_runpod.py` for each
  of the five seams showed only one (`SubprocessBootstrapActions.checkout_commit`'s
  success path) is actually covered, and that coverage predates this branch. Hard
  rule 6 — "if the accountable session cannot justify a line, it does not enter" —
  and this unit's own instruction — "any README claim you cannot verify in code is
  not written" — both point the same way: write what is true, and name the spec's
  prediction as unrealized rather than silently matching its wording.
- **The two new checklist rows are placed where they causally belong**, not appended
  to the end of the list: the drill-boot row sits before the "prove the durable
  laptop supervisor... work together" row, since Boot A is how that item gets
  observed; the balance-observer row sits near the top, next to the ceilings and
  authorization row it was folded out of, since it blocks every later row on the
  checklist.
- **The v1/v2 paragraph is rewritten as "superseded, not settled," not migrated.**
  Writing v2 field names into `provider_runpod.py` without having fetched v2's actual
  create-body schema would trade one unverified assumption (v1 is current) for
  another (guessed v2 shapes are correct) on a money path — worse, not better. Naming
  the ruling and the retirement date, and leaving the code as the code, is the
  accurate record; the migration itself is out of scope per `SPEC_POD.md` §4.0's own
  section boundary.
- **`~/.claude/WAKE_PLAYBOOK.md` is referenced, not summarized.** The instruction was
  to add "one line under the supervisor" naming that the playbook applies; the README
  now says so once, in the `supervise.py` bullet, rather than duplicating the
  playbook's content into a governed path it does not belong in.

## Verification run

`.venv/bin/ruff format` and `.venv/bin/ruff check` are not applicable — no `.py` file
was touched by this unit. `sh .githooks/check-documents.sh` was run from the worktree
root (this unit touched `.md` files) and passed: "Ingress check passed for the
requested scope." / "Document check passed." No `pytest` was run: this unit's brief
scoped test iteration to `test_pod_runtime.py -k readme`, and no such test exists in
this tree to iterate against (see "What this unit did not do" above) — the final
full-package run named in the brief was accordingly not applicable either, since
nothing in `operations/pod` was changed by this unit that a test run could catch
regressions in. If the host wants the full `operations/pod` suite run anyway as a
sanity check on the branch as a whole (not on this unit's own diff), that is a
separate ask.

## Blockers before either boot

1. The GraphQL account-balance observer does not exist. No paid action can run
   without it, drill included.
2. Tyrel has not been asked for `config/spend.toml` values, the GPU class, the S3
   keys, or in-session permission for either boot. All are named explicitly in the
   README's boot-plan section.
3. The v1/v2 posture is unresolved in code — Boot A and Boot B can run against
   either v1 (as the code stands) or v2 (per the ruling), but building the balance
   observer against v1's adapter now and migrating later means building it twice.
   Whether to build the observer before or after the v2 migration is next-section
   engineering, not named here as a decision for Tyrel.

## Provenance correction

Four commits on this branch — `ffc9d4ebc9` (the U4 armer build),
`dda1a1776c`, `0e30c5ef7b` and `c83f96b79d` (the U4, U6 and U7 review-fix
commits) — carry a `Co-Authored-By: Claude Fable 5.1` trailer inherited from
the host harness's attribution line. The seats that wrote those lines were an
Opus 5 seat (the first) and Sonnet 5 seats (the three fixes); the Fable seat
in this session was the host orchestrator and wrote none of them. History
rewriting is reserved to Tyrel, so the record stands here rather than in the
trailers.
