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
3. **Both boots now have a balance source.** U-C below built the GraphQL observer
   as `RunPodProvider`'s default over a live transport; what Boot A must observe
   about it is the figure it reads against the console's, and whether the pod-scoped
   key is accepted by the GraphQL endpoint at all.

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

## U-C — the money path to Boot A (on `work/pod-money-path`)

The consult of 2026-09-02 (`workbench/active/CONSULT_FIRST_REAL_RUN_2026-09-02.md`
§5 U-C) named this unit: build the balance observer the two blockers above turn on,
record the v2 migration before writing v2 code, add `--record-fixture`, and render
the Boot A request for Tyrel. Every line was proven offline; RunPod was never called
and no pod exists. RunPod's public documentation was read online, and every fact taken
from it is cited with its page and its date in the file that relies on it.

**What landed.**

- `provider_runpod.py` — `GraphQLBalanceObserver`, the default `balance_observer`
  whenever the provider is built over a live `UrllibRunPodTransport`. The transport
  gained a `credential_placement` of `"header"` (REST, unchanged) or `"query"`
  (GraphQL's documented `?api_key=`), a `sibling()` that derives the GraphQL transport
  so the provider never touches the key, and error messages that carry `reason` rather
  than a URL. The observer refuses by name every doubt the README lists, including any
  credential-shaped key anywhere in the answer. `record_exchanges` routes both its
  transports through a fixture recorder.
- `fixture.py` (new, vendor-neutral) — `FixtureRecorder` (append-only JSON lines, 0600,
  fsynced per line, money as numbers via `Decimal`), `RecordingTransport`, and
  `read_fixture`. Verbatim bodies unless a credential-shaped key forces a scrub, and
  then the record says so and names the paths.
- `cli.py --record-fixture PATH` — duck-typed on `record_exchanges`; refuses a provider
  without it before any preview; names the path in the result record.
- `boot_a_request.py` (new) — renders the drill request from the sealed policy and the
  reviewed card table; refuses on `unconfigured`; `python -m` entrypoint exits 2 on a
  refusal.
- `V2_MIGRATION.md` (new) — every v1 call mapped to v2 with a citation each; what v2
  adds; what has no counterpart; the plan the next unit executes. Admitted to the
  document allowlist by exact path (`.githooks/doc-allowlist.sh`), the same way the
  autoclave brief is.
- `README.md` — the v1/v2 and balance paragraphs rewritten to the code as it now
  stands; the `--record-fixture` and Boot A paragraphs; the balance checklist row made
  runnable; deferral rows 04-4, 04-6, 04-7 and 04-9 updated to what the migration
  record actually established.

**What this unit found that it did not fix, and why.**

- ~~The nested bootstrap `--report-path` is never token-bound.~~ Closed by the pod
  runtime integration unit: `_bind_report_path_to_launch` now folds the sealed
  `VERBATUS_LAUNCH_TOKEN` into the nested bootstrap argv's own `--report-path` as well
  as the timer's, so a real `bootstrap_main --hold-only` reaches its hold instead of
  refusing at plan time. Proven through the real plan/argv path in
  `test_bootstrap_main.py`.
- **v2 has no `interruptible` and no `dockerStartCmd`** (`V2_MIGRATION.md` §2.2, §4).
  The next unit either finds a v2 page stating the rental type and a start-command
  field (the template page was not read in this pass), or puts the conflict to Tyrel
  under rule 9: the ruling says v2, the goal says on-demand only with the timer as the
  primary process, and the documentation read cannot yet satisfy both.
- **GraphQL retires in early 2027** (`sdks/graphql/configurations`), and neither v2
  billing endpoint reports a balance. The observer is built on the only documented
  source and carries its own sunset.
- **`supervise.py` would close a v2 pod that is still `PROVISIONING`/`STARTING`**
  (`V2_MIGRATION.md` §2.5). Not a v1 problem; recorded so the migration plans for it.

**Rule-13 decisions this unit made.**

- The key goes in the GraphQL query string because that is the only placement the
  GraphQL documentation publishes. Sending an undocumented `Authorization` header, or
  trying one and falling back on 401, would make a credential-placement guess on a
  money path and mask a revoked key; the documented form, with the query scrubbed from
  every error string and fixture record and the redirect refusal measured on loopback
  in that placement, is the honest choice. If the first live run shows the header is
  accepted, switching is one constant.
- The recorder scrubs `VERBATUS_LAUNCH_TOKEN` with no exemption, unlike
  `PodCreateRequest`'s metadata scan. One predicate with no carve-outs is checkable by
  reading the predicate; replay of the recovery path re-substitutes the token from the
  lease, which the record's `scrubbed` list makes possible.
- The recorder is its own small module rather than living in `cli.py` or `models.py`:
  two modules that must not import each other (`cli.py`, `provider_runpod.py`) need the
  same seam.
- A scrubbed body is re-serialized with `Decimal` written back as the same digits (a NUL
  marker that no JSON body can contain), so a fixture never turns money into a float or
  a string. An unscrubbed body is stored verbatim.
- A negative `clientBalance` is refused by name rather than clamped or read as zero:
  `AccountBalanceObservation` cannot carry one, and the refusal denies paid actions,
  which is the safe direction.
- `boot_a_request.py` bounds the drill at `min(900, policy.hard_lifetime_seconds)`,
  takes the cheapest card by reviewed `hourly_usd`, and renders the coming preview
  refusals (card above `max_hourly_usd`, cost above the metered ceiling) as a section
  rather than hiding them or refusing to render.
- `V2_MIGRATION.md` is admitted to the allowlist by exact path, with its deletion
  condition written beside the admission, rather than widened to a glob.

**Verification run.** `ruff format` and `ruff check` on every `.py` touched, clean.
Tests through the shared test lock only, as the brief required. The three owned
files (`test_provider_runpod.py`, `test_pod_runtime.py`, `test_boot_a_request.py`)
passed through the lock: `PYTEST_EXIT=0`, 2026-09-02. The full `operations/pod` run
is still pending — the brief allows it once, and it was not spent verifying this
review pass.

## Blockers before either boot

1. ~~The GraphQL account-balance observer does not exist.~~ Built by U-C; it is the
   adapter's default over a live transport. What remains is the live observation the
   README's balance checklist row now describes.
2. Tyrel has not been asked for `config/spend.toml` values, the GPU class, the S3
   keys, or in-session permission for either boot. All are named explicitly in the
   README's boot-plan section, and `boot_a_request.py` renders the request the moment
   the policy is configured. The VRAM fact that bears on the card: a bf16 27B
   Perlector fits only the 96 GB tier (`config/pod_placement.toml`, RTX PRO 6000
   Blackwell at $1.99/h); Boot A itself needs only the cheapest card.
3. The v1/v2 posture is now recorded, not resolved: `V2_MIGRATION.md` is the plan,
   and its §4 names two facts (`interruptible`, the start command) the v2 pages read
   do not supply. The observer was built against the seam, not against v1's REST
   route, so the migration does not rebuild it.
4. ~~The nested bootstrap report path (above) must be token-bound before Boot B.~~
   Closed; see the finding above.

## U-A — the pod run seam (on `work/pod-run-seam`, over this branch)

Built from the Fable consult's finding that nothing could serve on a pod and nothing ran
the pipeline on one. What landed, and what the code as it stands made impossible:

**`PREFLIGHT` is wired, not stubbed.** `bootstrap_main.py` replaces
`_UnimplementedChairCacheVerifier`/`_UnimplementedSmokeReader` with
`RegistryChairCacheVerifier` (`ChairRegistry.ensure` per chair) and the serving package's
own `assemble_serving_smoke_reader` around `ServingManager`, fed
`operations/serving/smoke.py::VisionSmokeCall`. The pod is its own fixture author:
`fresh_page_witness` (CSPRNG) and `render_golden_page` put the witness into pixels under
`<volume>/preflight/<report stem>/` right before the read; `NvidiaSmiUtilization` is the
sampler; `PodPreflightReceiptPublisher` writes the receipt, launch audit and evidence
manifest content-addressed in that directory because no run tree exists at bootstrap.
`PreflightSeams` is the injection point every effect has, and `test_bootstrap_main.py`
proves green through the real registry over the committed model fixtures and the serving
fakes, and red by chair name for an unproven row. The measured dtype is `bfloat16` now —
every vLLM row is bfloat16 and the reader refuses a dtype mismatch, so the old `float16`
made every real smoke red before it launched. `main` is split into `prepare` /
`run_bootstrap` / `hold` with behaviour unchanged, so `pod_run` composes it.

**`pod_run.py` runs the pipeline on the pod.** Bootstrap through `bootstrap_main`'s own
steps, then the orchestrator as a subprocess of the pod's interpreter over the volume,
a `pod-run-report.v1` before/during/after, exit codes that never read complete for a
partial run, and the hold to the deadline unchanged. `test_pod_run.py` covers the green
run, held/halted/failed, a red bootstrap, every refusal by name, and the data gate asked
before any spend.

**`fetch-run` brings the tree home.** `S3VolumeObjectReader` (list + streamed
`GetObject`, fail-closed) beside the existing reads in `volume_s3.py`;
`surface.fetch_run` does the run-tree reconciliation (blob and receipt names, artifact
digests against manifests, `run.json` self-hash, manifests rebuilt and compared) and
never overwrites a differing local file; `verbatus fetch-run` and its interactive prompt.

**`run` accepts the roster pair.** `--models-config` and `--serving-recipes-config`,
forwarded together to the orchestrator (and to the crash drill's Door, which seals them);
one without the other is refused.

**What was not built, and why — rule-13 decisions and one finding:**

- **No `pod` dependency group, and `bootstrap.py`'s sync is unchanged.** `uv lock`
  refuses the recipe's pins beside the project's `huggingface_hub==1.26.0` (the README's
  "The serving stack cannot be locked yet" quotes it). Locking with `transformers` left
  free pairs `vllm 0.10.1` with `transformers 5.16.1`, which would install gigabytes on a
  billing card and then refuse at the manager's pin check. Overriding the constraint would
  be a lie on a money path. The honest deliverable is the finding, deferral 04-10, and a
  strict expected failure in `test_pod_run.py` that goes live when the group lands. The
  re-pin is a reviewed edit to `config/serving_recipes_real.toml`, outside this unit.
  **Superseded by U-B below**, which did that re-pin: the group exists, the lock resolves,
  and deferral 04-10 is closed.
- **No `--placement-tier` anywhere.** The consult named it; the orchestrator and the
  stage parser accept no such flag and no stage reads a tier. `pod_run` records the tier
  the green `PREFLIGHT` receipt measured and refuses a receipt without one; the console's
  `run` verb takes the two flags that exist.
- **The run tree is bound by run id, not by launch token.** The consult said "under the
  launch-bound name"; the run *report* is launch-bound (a second launch on a retained
  volume must not overwrite the first's evidence), but the tree itself is `runs/<run_id>`
  because the orchestrator's resume is by run id and the tree already refuses different
  bytes at the same identity. A token in the tree's path would make every resume across
  pods a fresh run.
- **The roster pair comes from the bootstrap plan.** `pod_run` takes `--models-config`
  and `--serving-recipes-config` from the bootstrap argv rather than accepting them
  again: `PREFLIGHT` measured that roster against that catalogue.
- **The data gate is asked before the bootstrap.** At the time U-A built this, the
  shipped policy named no volume root; refusing before a model is fetched saved the
  fetch. The pod runtime integration unit closed that: the shipped policy now names
  `operations/pod/boot_a_request.py`'s sealed `volume_mount_path` beside `private/`
  (Tyrel's ruling, `workbench/standing/TYREL_RULINGS_2026-09-01_BUILD_SESSION.md`), and
  `pod_run.py`'s own module docstring was corrected to match --
  `require_approved_submission_folder`'s refusal wording was already generic and needed
  no change. `pod_run` still refuses a submission outside every listed root, now against
  the real one.
- **`ErrorCode.FETCH_RUN_FAILED` was added to `errors.py`**, which the unit did not own:
  the table is closed and `test_errors.py` requires every code to be raised somewhere,
  so a new verb cannot register its failure state anywhere else.
- **The hold after a complete run stays.** `pod_timer` treats any early exit as
  `completed-early` with a non-green report; closing early on a complete run is a timer
  contract change, named in the README rather than made.
- **Unproven rows still refuse.** Every real row is `unproven`, and the manager refuses
  unproven rows before launch, so the first real `PREFLIGHT` is red by construction until
  a reviewer stamps rows proven — a circle the serving README describes and this unit
  names rather than cuts.

**Verification.** `ruff format` and `ruff check` on every touched `.py`;
`sh .githooks/check-documents.sh` for the `.md` files; the named test files through the
serialized test lock, then `operations/pod operations/operator` once. No acceptance pin was
run: nothing here touches a fixture path. `uv lock --check` is clean because the lock is
untouched.

## Pod runtime integration — closing three U-C/U-A seams (on `work/pod-runtime-2`)

Four deliverables over U-C and U-A as they stood: the storage-root gap the account
balance and Boot A work left open, a vendor-neutral phone-notification seam, the nested
report-path token binding U-C's own docstring flagged and left for a later unit, and
this record.

- **`config/data_handling_policy.json` names the pod volume.** `storage_roots` gains
  `/workspace/private` — `operations/pod/boot_a_request.py`'s `BOOT_A_VOLUME_MOUNT_PATH`,
  the one concrete `volume_mount_path` a real launch request in this tree seals — beside
  the existing `private/`. `storage_roots_note` states the export flow: the volume is a
  storage root for a run's duration (Tyrel's ruling (a)), `verbatus fetch-run` brings the
  tree home, and the volume itself is destroyed only per `retention_and_deletion`, never
  as a side effect of export. `pod_run.py`'s own docstring, which said the shipped policy
  named no volume root, is corrected to match; `pod_run` still refuses a submission
  outside every listed root (unchanged behavior, now against the real one) —
  `test_pod_run.py`'s own policy fixture already substitutes its own `storage_roots`
  list per test, so nothing there needed to change.

  **Rule-13 decision, corrected by review.** The first cut of this unit left
  `operations/submit/gate.py`'s `approved_storage_roots` untouched and all-or-nothing:
  it resolved *every* listed root before returning any of them, so the unmodified
  two-root shipped policy refused outright on any machine without the pod volume
  mounted — every host laptop, CI, this test machine included — breaking the
  previously-working `private/` root along with it. A lock run proved the blast
  radius directly: 23 failures in `operations/corpus/test_submission.py`, all through
  the same "does not exist" refusal, plus every default-policy real-submission path
  (`operations/submit/submit.py`, `pipeline/1_exemplar/door.py`,
  `operations/operator/ingest_worker.py`, `operations/corpus/submission.py`). That is
  not an acceptable cost for naming a volume path in a file most callers never touch
  a pod through, so `approved_storage_roots` now resolves each listed root
  independently and refuses only when *none* of them resolve — an absent root is
  named in the refusal detail and skipped, never silently dropped. A host with no pod
  mounted gets exactly the local `private/` root back from the unmodified shipped
  policy; a pod with the volume mounted gets both. `operations/submit/test_gate.py`
  covers both new shapes (one absent root beside one present one; every root absent)
  and the two-root shipped-policy case now proves the local-root result instead of
  pinning the refusal. `pod_run` still refuses a submission outside every listed
  root — unchanged behavior — and `test_pod_run.py`'s own policy fixture still
  substitutes its own `storage_roots` list per test, so nothing there needed to
  change. Every host-side real-submission path (`submit.py`, `door.py`,
  `ingest_worker.py`, `corpus/submission.py`) now resolves the shipped policy exactly
  as it did before this policy file named a pod volume at all.

- **`operations/pod/notify_hooks.py`.** Covered in the module list above. `cli.py` wires
  it behind the existing `--notify` flag (a green create/adopt prints
  `launch_notification`; any result carrying a `close_report` prints
  `close_notification`, green or not). `RunPodProvider` takes an explicit
  `balance_notify` parameter, defaulted to `None`; only a caller that supplies it gets
  the hook wired into the default `GraphQLBalanceObserver` it builds for a live
  transport. A bare live-transport construction — including the one the pod's own
  `timer_context_from_environment` performs, which has no way to receive `--notify`
  at all — carries no hook, so a plain `verbatus pod create` with no `--notify` can
  never page a phone from a balance observation, and `--notify` stays the single gate
  for every phone notification a launch can send. Both hook points are the minimal
  wiring, not a rebuild of either file: `notify_bridge.py`'s existing
  spend-floor-warning seam is untouched.

- **The nested bootstrap report-path token binding, closed.**
  `launch._bind_report_path_to_launch` bound the launch token into the timer's own
  `--report-path` only; the same token now also folds into the `--report-path` inside
  `--bootstrap-command-json` when that nested argv carries one, so a real
  `bootstrap_main --hold-only` (rendered by `boot_a_request.pod_request`) reaches its
  hold instead of refusing at plan time for a missing token. Proven through the real
  plan/argv path in `test_bootstrap_main.py`'s
  `test_hold_only_reaches_the_hold_through_the_real_launch_binding` — it drives
  `boot_a_request.pod_request` and `launch._bind_report_path_to_launch` themselves, not a
  hand-written argv standing in for them, and asserts `bootstrap_main.main` reaches
  `hold-only` and holds to the deadline. `boot_a_request.py`'s own docstring (which named
  this as unbound) and `README.md`/`HANDOFF.md`'s U-C blocker list are updated to match.
  Review found the binding itself best-effort by design, with nothing re-validating its
  result: a nested `--report-path` shape the binder cannot handle (or one carried over
  unbound from a stale template) reached `PodCreateRequest` unbound and would have
  refused only at pod-side plan time, after billing had started. `models._required_
  timer_arguments` — the same money-path gate that already required the outer
  `--report-path` to carry the launch token and stay inside the volume — now requires
  the same of a nested one, in either argparse spelling, when one is present; a nested
  argv naming none is left alone, as before.

**Verification.** `ruff format` and `ruff check` clean on every `.py` touched. Tests
through the shared test lock only, iterated per this unit's own budget; the full
`operations/pod operations/operator/test_surface.py common/test_data_gate.py` sweep (or
wherever the data-gate tests actually live, since no `common/test_data_gate.py` exists —
`operations/submit/test_gate.py`) run once at the end. `sh .githooks/check-documents.sh`
on every `.md` touched.

## U-B — the serving-stack re-pin (on `work/pod-run-seam`, over U-A)

**What it closes.** Deferral 04-10. U-A found that no `pod` dependency group could be
locked and left the re-pin to a reviewed config edit; this is that edit, plus the group,
the lock, the sync flag and the test that binds them.

**The research, in one paragraph.** Read on 2026-09-02 from the pages cited in the pod
README's "The serving stack, re-planned and locked". The four ruled chairs declare two
architectures between them: Chandra-2 and the Perlector (`Qwen/Qwen3.8-27B`) are both
`Qwen3_5ForConditionalGeneration` / `qwen3_5` — the multimodal Qwen3.5 architecture, not
the text-only one — while the DAI fine-tune and Churro-3B are both
`Qwen2_5_VLForConditionalGeneration` / `qwen2_5_vl`. vLLM v0.27.1's model registry lists
all of those in `_MULTIMODAL_MODELS`, and vLLM's own recipe page for `Qwen/Qwen3.8-27B`
asks for vLLM 0.17.0+ and transformers >= 5.8.0. **One release serves all four; no chair
had to be split off onto transformers-direct serving.**

**Rule-13 decisions, each with its reason:**

- **`vllm 0.27.1`, not 0.28.0.** 0.28.0 declares `huggingface_hub>=1.27.0` in its own
  metadata and so cannot lock beside the project's `huggingface_hub==1.26.0`; 0.27.1
  declares no hub requirement at all and inherits only `>=1.5.0,<2.0` from
  `transformers`. Nothing any chair needs is 0.28.0-only. The project's hub pin was not
  touched — moving it to suit a serving pin would have been the money path bending the
  laptop's.
- **`transformers 5.14.1`.** The version the vLLM 0.27 line's own requirements were
  bumped to, above vLLM's `>= 5.5.3` floor and above the Perlector's stated `>= 5.8.0`,
  and its metadata accepts `huggingface-hub 1.26.0`.
- **`flash-attn` dropped, not re-pinned.** vLLM imports no external `flash_attn` in its
  Qwen vision path — it selects through its own `AttentionBackendEnum` and ships its own
  FlashAttention — and the package publishes no wheel, so the pin would have bought an
  hours-long nvcc build against `torch 2.13.0` on a rented card for something the server
  never loads.
- **The tripwire became a live test rather than being deleted.** `test_pod_run.py` now
  holds the `pod` group and every catalogue row to the same bytes, both directions, and
  still requires the Linux/x86_64 marker on each requirement — that marker is what keeps
  a laptop `uv sync` from resolving torch.
- **Two files outside the named ownership were corrected, not left lying.**
  `operations/serving/test_manager.py` asserted `vllm == "0.10.1"` against the real
  catalogue, and `bootstrap_main.py`'s module docstring stated that no `uv sync` could
  put vLLM on a pod. Both became false the moment the pins moved; leaving either would
  have been a false statement on a money path.

**Verification.** `uv lock` resolves (173 packages added — `vllm 0.27.1`,
`transformers 5.14.1`, `torch 2.13.0` among them — with **no** previously locked version
changed and nothing removed); `uv lock --check` clean; `uv sync --frozen --python 3.12
--group test --group audit` succeeds on macOS and resolves none of the pod group;
`ruff format` and `ruff check` on every touched `.py`; `sh .githooks/check-documents.sh`
for the `.md` files; the named test files through the serialized test lock, then
`operations/pod operations/serving` once.

**The one thing only a boot proves.** That the wheels install on the pod image and that
the four sets of weights actually load and answer under `vllm 0.27.1`. Vendor metadata
says the architectures are registered; it does not say these checkpoints run. Every row
stays `preflight_state = "unproven"` and the manager still refuses each by name.
Confidence, named honestly: high that the group installs, moderate that all four chairs
load unmodified on the first attempt — Chandra-2 and the Perlector are the risk, because
vLLM's supported-models row for their architecture names Qwen's own `Qwen3.5-*` repos,
not a fine-tune and not a `Qwen3.8` checkpoint.

## Provenance correction

Four commits on this branch — `ffc9d4ebc9` (the U4 armer build),
`dda1a1776c`, `0e30c5ef7b` and `c83f96b79d` (the U4, U6 and U7 review-fix
commits) — carry a `Co-Authored-By: Claude Fable 5.1` trailer inherited from
the host harness's attribution line. The seats that wrote those lines were an
Opus 5 seat (the first) and Sonnet 5 seats (the three fixes); the Fable seat
in this session was the host orchestrator and wrote none of them. History
rewriting is reserved to Tyrel, so the record stands here rather than in the
trailers.
