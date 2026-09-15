# 2026-09-14 — pre-launch review: findings ledger

Evidence from the ten-perspective review workflow run in the session of 2026-09-14 (finders on Sonnet, Opus and Fable seats; a reproduce/context/impact panel per finding). This is the record of what was found; dispositions (fixed or declined, with reason) are appended in the same session's later commits. Nothing here is an instruction.

Raw findings: 112; after dedup: 112. Panel verdicts recorded at checkpoint: 237.

## Finders

- **FAIL LOUDLY — sweep of common/, pipeline/, and operations/ for swallowed failures, default** — 0 findings.
  Method: Read GOALS.md, GOVERNANCE.md, ARCHITECTURE.md, GLOSSARY.md, CLAUDE.md. Enumerated candidates with ripgrep across common/, pipeline/, operations/ for 'except Exception', 'except:', bare 'pass' after except, '.get(' with literal defaults, 'or {}'/'or []', 'continue', logging/warning calls, and subprocess.run/Popen call sites, then read matches in context: pipeline/5_recensor/run.py (first ~1220 of 4156 lines), pipeline/orchestrator/run.py (full), pipeline/1_exemplar/pdf_render.py and image_formats.py exception blocks, common/chairs/registry.py fetch/promote paths, operations/pod/shutdown.py status/absence/billing-capture handling, operations/pod/launch.py spend-notification path, operations/po
  Coverage gaps: This was a targeted grep-driven sweep, not an exhaustive line-by-line read of all ~488 python files under common/, pipeline/, and operations/. Confirmed clean by direct reading: pipeline/5_recensor/run.py (lines 1-1220 of 4156), pipeline/orchestrator/run.py (full), pipeline/1_exemplar/pdf_render.py and image_formats.py exception boundaries, common/chairs/registry.py fetch/cache-promotion paths, operations/pod/shutdown.py status/absence/billing-capture paths, operations/pod/launch.py spend-alert 

- **Troubleshootability and operator error messages: for each of Verbatus's fifteen words, whe** — 4 findings.
  Method: Read GOALS.md, GOVERNANCE.md, ARCHITECTURE.md, GLOSSARY.md, CLAUDE.md, operations/operator/{surface.py,entry.py,cli.py,errors.py,Verbatus.command,README.md}, operations/pod/README.md, operations/operator/custody.py. Ran the frozen interpreter (.venv/bin/python) against operations/operator/cli.py with a scratch --state-dir under the approved scratchpad for: review with a nonexistent run-root, review with an invalid run-id, status against an empty state dir, run with a nonexistent --submission-folder/--submission-manifest, launch with a nonexistent --request file, export with no completed run, run with a missing --run-id, and a full synthetic `run --run-id fixt1` (default fixture/scenario) fol
  Coverage gaps: Did not exercise: launch/close/boot against the fixture provider end-to-end (money-path verbs), triage's accept/decline flows, scantailor's project-import flow, ingest's full ledger/data-gate/triage pipeline, fetch-run/backup against a real or simulated network volume, or the confined console/backup-worker code paths themselves (blocked in this sandbox by the same Landlock/setpriv gap reported as a finding, which also prevented directly exercising review's plain-language rendering, advance, and 

- **Configuration and pins for the real run — internal consistency of the real trio (config/mo** — 2 findings.
  Method: Read GOALS.md, GOVERNANCE.md, ARCHITECTURE.md, GLOSSARY.md, CLAUDE.md. Read every file under config/, pyproject.toml, uv.lock (grep), requirements-dev.txt, common/chairs/ (config.py, models.py, registry.py, model_store.py plus its tests), relevant sections of common/stage.py, operations/pod/bootstrap.py (full file), operations/pod/pod_run.py / bootstrap_main.py (targeted greps), operations/pod/preflight.py and operations/serving/preflight.py, operations/pod/test_card_allowlist.py, and config/spend.toml. Ran with the frozen interpreter: pytest -k models-or-serving-or-chairs-or-recipes-or-witness_context-or-pod_placement (all passed, 1 skipped), plus targeted runs of test_serving_catalogue_cap
  Coverage gaps: Did not deep-review designator_geometry.toml, designator_grouping.toml, designator_padding.toml, pdf_render.toml, formats.toml, decoding.toml, alignment.toml, hard_failure.toml, recovery.toml, triage_modes.toml, perlector_protocol.toml, perlector_audit.toml, corpus_frame.toml, or config/data_handling_policy.json beyond a line-count skim — these are not real/fixture-trio specific and were out of scope for the time spent. Did not exhaustively read common/chairs/model_store.py, registry.py, manifes

- **test quality and end-to-end gaps** — 6 findings.
  Method: Read GOALS.md, GOVERNANCE.md, ARCHITECTURE.md, GLOSSARY.md, CLAUDE.md. Ran the full suite with `.venv/bin/python -m pytest -p xdist -n 4 --dist loadfile -q`, PYTEST_DISABLE_PLUGIN_AUTOLOAD=1, NTFY_TOPIC=<the test-sink topic from conftest.py>, as instructed, twice (the first run's log — /tmp/.../scratchpad/review/test-quality/pytest_run.log — was cut short by my own accidental kill at ~50%; the second full attempt — pytest_run2.log — was still in progress at ~71% collected/reported (567s elapsed) when this review's time budget was exhausted, having advanced past the point of the reproducible failure with no further failures observed and only 's' (skip) markers besides). Both runs independently hit the exact same
  Coverage gaps: Did not obtain one complete, uninterrupted full-suite run with a final failure/skip summary within the session's time budget (see the last finding); the census beyond ~71% of the run is unobserved, though the one failure found is reproducible and I have high confidence no new failures appear given two independent runs agreed on every observed segment. Did not read pipeline/orchestrator/test_orchestrator_acceptance.py or operations/operator/test_surface.py (7307 and 5280 lines respectively) beyon

- **Hostile input and trust boundaries: every submitted folder, PDF, image, ScanTailor project** — 15 findings.
  Method: Read GOALS.md, GOVERNANCE.md, ARCHITECTURE.md, GLOSSARY.md, README.md, CLAUDE.md first, then the named scope: operations/submit/{inventory,submit,gate,cleanup}.py, pipeline/1_exemplar/{door,admission,image_formats,pdf_render}.py, pipeline/0_triage/manifest.py, common/{exemplar_boundary,imaging,chair_wire,witness_adapters,decoding,structure_answer,chandra_layout,chandra_custody,corpus_register}.py, common/runtree/store.py, common/contracts/{canonical,errors}.py, operations/pod/{transfer,bootstrap_main}.py, operations/operator/{surface,cli,custody,errors,review,review_text,notify_bridge,backup,scantailor,scantailor_worker,volume_s3}.py, operations/serving/http.py, operations/notify/notify.sh, 
  Coverage gaps: Not covered or only skimmed: pipeline/1_exemplar/door.py past the source-read boundary (the ~2,600-line expansion, triage-derivative and refusal-report logic — I read the document read, the ledger reconciliation and the source reopen, not the whole file); operations/operator/backup.py and backup_worker.py (surveyed function names and the O_NOFOLLOW/dir-fd discipline, did not read the inventory walk in full); common/chairs/model_store.py and registry.py (the Hugging Face fetch and materialization

- **END-TO-END EXECUTION — I ran the operator surface against the synthetic fixture, the real ** — 21 findings.
  Method: All runs used /home/user/Apparatus-Verbatus-Alpha/.venv/bin/python with `--state-dir` under /tmp/claude-0/-home-user-Apparatus-Verbatus-Alpha/db242c6b-a35a-5922-a1c0-ea5b0ac9eb56/scratchpad/review/e2e-run (state1..state23). (1) Fixture run: `python -m operations.operator.cli --state-dir $S/state1 run --run-id r1` (exit 0, "Run complete"), then `export --run-id r1` (exit 0, 2 pages / 2 expected / 2 delivered), `status` (matches the export table), and backup. The CLI `review` verb could not run at all on this host: `custody.LandlockConfinement` requires `/usr/bin/setpriv --landlock-access`, and this host has util-linux 2.39.3 with `landlock_create_ruleset` returning ENOSYS (I probed the syscal
  Coverage gaps: What I could not exercise as a real operator would. (1) The confined-child path of `review`, `backup`, `advance`, `ingest` and `scantailor` never ran on this host: no kernel Landlock (syscall 444 returns ENOSYS) and setpriv 2.39.3. I substituted the parent-side projection and the backup worker's own `sync_run_tree`, which is the same code the children execute, but I did not exercise `operations/operator/console.py`, the JSON pipe between parent and child, `review --json` through the CLI, the cus

- **THE FIRST REAL POD RUN — reading operations/pod/ (README, bootstrap, bootstrap_main, pod_r** — 19 findings.
  Method: Read GOALS/GOVERNANCE/ARCHITECTURE/GLOSSARY/CLAUDE.md and README.md first, then the pod and serving packages in full or by targeted section. Ran the frozen interpreter (.venv/bin/python) for four demonstrations: (a) constructed a PodCreateRequest whose --bootstrap-command-json is a realistic `pod_run` argv — refused at construction with "pod bootstrap command must carry at most one nested --report-path value"; (b) evaluated operations.pod.models.looks_like_credential_field over the env names the runtime needs — RUNPOD_API_KEY, HF_TOKEN, HUGGING_FACE_HUB_TOKEN all True, i.e. all refused as pod metadata keys; (c) evaluated RunTree.inventory_scope() and is_publication_temporary() against the ex
  Coverage gaps: I did not read lease.py, controllers.py, supervise.py, arming.py, fake_provider.py, notify_hooks/notify_bridge, cli.py beyond _request, volume_s3.py, or the pipeline stage bodies in depth — so lease/heartbeat/orphan reconciliation, the S3 volume adapter's key shaping and credential handling, and the notification path are essentially unreviewed by me. I verified no vLLM 0.27.1 CLI flag against a real wheel (no vllm installed here, and 0.27.1 postdates my knowledge), so render_vllm_argv's flag spe

- **Run-tree contracts, seals, and stage boundaries: the run tree modelled as a state machine ** — 14 findings.
  Method: Read GOALS/GOVERNANCE/ARCHITECTURE/GLOSSARY/CLAUDE/README, then common/runtree/store.py, common/contracts/{canonical,envelope,stages,errors,identities}.py, common/durability.py, common/hard_failure.py, common/recovery.py, the seal/open/resume sections of common/stage.py (StageContext, _stage_seal_payload, _stage_blob_inventory, _verify_stage_seal, _refuse_deleted_seal, open_context/_open_real_context, latest_attempt, current_recovery_request, expected_acts), pipeline/orchestrator/run.py in full, and the main()/seal/resume paths of every stage run.py (1_exemplar, 1_ink_map, 2_designator incl. live_initial_pass and structure_pass.live_chair_record, 3_attestatores preflight_appendable_ordinals,
  Coverage gaps: Not exercised by running: the real-ingress route (Door with a submission folder, data-gate policy and ledger), any live chair, and the orchestrator's recovery dispatch on real ingress -- the two highest findings rest on reading the gating code and its pinned unit test, not on a live run. Not tested: case-insensitive or Unicode-normalising filesystems (macOS/APFS), exFAT/network mounts, concurrent writers on one tree, power-loss durability (only read the fsync sequence). The orchestrator acceptan

- **Governance and ARCHITECTURE invariants in code: for each of the eight ARCHITECTURE invaria** — 12 findings.
  Method: Read GOALS.md, GOVERNANCE.md, ARCHITECTURE.md, GLOSSARY.md, CLAUDE.md and README.md from disk first. Then traced each invariant to its enforcing code: pipeline/4_perlector/{run,dissent,combined,truncation,live_reader,dossier}.py, common/{alignment,cross_capture_dissent,reshoot_delta,corpus_register,recovery,exemplar_boundary,stage,residual_ink,capture_comparability,calibration,durability}.py, common/runtree/store.py, common/chairs/receipts.py, operations/serving/{client,manager}.py, operations/pod/shutdown.py, pipeline/2_designator/run.py, pipeline/5_recensor/run.py, pipeline/6_archetypus/run.py, pipeline/7_armarium/{run,armarium_export,display,bundle}.py, pipeline/orchestrator/run.py, commo
  Coverage gaps: Per-invariant ledger for what I judged HELD, with the enforcing code named, so the host can see what I did and did not accept.

(a) Perlector never picks / GOVERNANCE 3 — HELD, with finding 4 as the one crack. Enforcers: common/alignment.py (attaches bytes to anchors, never chooses a reading; the RapidFuzz refusal at :80-104 is a real defence of this); pipeline/4_perlector/dissent.py (equality only, no metric, no threshold — pinned in the module docstring); common/cross_capture_dissent.py and co

- **Transfer and diagnosis from records alone: can a failed run on a pod be diagnosed later, o** — 19 findings.
  Method: Read GOALS/GOVERNANCE/ARCHITECTURE/GLOSSARY/CLAUDE.md, the root and operator/runtree READMEs, and the code: operations/operator/{surface,records,cli,entry,console,custody,review,review_text,backup,advance,notify_bridge,_run_tree_paths}.py, common/runtree/store.py, common/checkout.py, common/durability.py, operations/pod/{transfer,pod_run,pod_timer,launch,notify_bridge,notify_hooks,spend}.py, operations/notify/notify.sh, Verbatus.command, pipeline/orchestrator/run.py, the Attestatores hold exits, common/contracts/approval.py, and the relevant tests. Ran, with the frozen interpreter and NTFY_TOPIC=<the test-sink topic from conftest.py>, under the scratch directory: `verbatus run --run-id fixture-run` (built a com
  Coverage gaps: Did not run fetch-run against any S3 reader (no fake reader wired from the CLI in this session) or exercise the pod-side bootstrap, launch or close paths beyond reading them; did not run the custody child on a Landlock-capable host or on macOS, so Seatbelt behaviour, case-insensitive APFS behaviour and Finder xattr handling on a real Mac are concluded from reading and from the store's own casefold check, not observed. Did not read the Door/Exemplar real-ingress path end to end for absolute-path 


## Findings

### F001 [high] Progress lines name a page the scenario never touches, contradicting the total

`operations/operator/surface.py:3510` — correctness — from Troubleshootability and operator error m; verified by running: True

**Claim.** `_declared_work` always reads and returns the whole page/act list out of the single fixed fixture declaration file `proof/skeleton_fixture.toml` (via `load_fixture(workspace/"proof")`), independent of which `--fixture`/`--scenario` is actually running. `run()` uses that same fixed list twice: once at start ("Checking page 1, page 2, page 3.") and once at the end ("Pages accounted for: page 1, page 2, page 3 (N total)"), where N is the real count of sealed page records from this scenario's own Armarium export. For the shipped default fixture and the default "happy" scenario, the declaration lists 3 pages but the happy scenario only ever touches pages 1 and 2 (page 3 exists solely for other scenarios such as ink-free-page).

**Scenario.** Run `.venv/bin/python -m operations.operator.cli --state-dir <dir> run --run-id fixt1` with no other flags (exactly the first thing the README tells a new operator to try). The terminal prints "Checking page 1, page 2, page 3." then finishes with "Pages accounted for: page 1, page 2, page 3 (2 total)." Nothing on screen says page 3 was never part of this scenario; an operator reading GOALS.md's "every page is accounted for" promise has no way to tell, from this line alone, whether page 3 was silently dropped, refused, or is simply cosmetic noise -- confirmed independently by reading the sealed Armarium export artifact for this run, whose `aggregate.by_page_outcome` is `{"sealed": 2}` and which contains no reference to page 3 at all.

**Proposed fix.** Derive the printed page/act lists from the scenario actually selected (or, after the run, from the real export's page/act records) rather than from the fixture's full static declaration; where the two diverge, say so explicitly rather than printing a number that silently disagrees with the preceding list.


### F002 [high] Five verbs need a very recent, undocumented util-linux/setpriv feature

`operations/operator/custody.py:315` — operability — from Troubleshootability and operator error m; verified by running: True

**Claim.** `review`, `backup`, `ingest`, `scantailor`, and `advance` all route their confined child through `run_confined`, which on Linux requires `setpriv --landlock-access` (LandlockConfinement.command). The code's own comment says this was "verified against util-linux 2.41" -- a very recent release. Neither README.md nor operations/operator/README.md nor operations/pod/README.md states any minimum util-linux/setpriv version, and nothing in `boot`'s fixture-only checks probes for Landlock support before an operator reaches for `review`, `advance`, `backup`, `ingest`, or `scantailor`.

**Scenario.** On this machine (util-linux 2.39.3, whose setpriv has no --landlock-access option at all), running `.venv/bin/python -m operations.operator.cli --state-dir <dir> review --run-root <root> --run-id fixt1` against a run that completed successfully moments before fails immediately with CONSOLE_CUSTODY_REFUSED: "Linux Landlock (setpriv) could not be established, so nothing ran inside it: /usr/bin/setpriv: unrecognized option '--landlock-access'". `review` is the surface the README names as exactly what a person needs when a real run stops early or is held; an operator on any Linux box whose distribution ships an older util-linux (common on LTS/stable releases) will find this and four other words unusable on their first real run, discover it only at the point of highest need, and get a fix instruction ("fix Landlock or Seatbelt") that names no concrete remedy (e.g. which util-linux version to install).

**Proposed fix.** Document the minimum util-linux/setpriv version this build requires in operations/operator/README.md (and ideally check it, or setpriv's actual flag support, once during `boot` so the gap is caught before a held run rather than during one), and have the CONSOLE_CUSTODY_REFUSED copy for this specific failure name the required version/upgrade path rather than the generic "fix Landlock or Seatbelt".


### F003 [medium] RUN_FAILED/UPLOAD_PARTIAL show only a receipt path, dropping the captured reason

`operations/operator/surface.py:1179` — operability — from Troubleshootability and operator error m; verified by running: True

**Claim.** When the pipeline subprocess run by `run()` exits with an unrecognized code, the concrete cause (`completed.stderr or completed.stdout`) is written into the receipt's `detail` field, but the `OperatorError` raised to the operator is built with `detail=f"Saved run receipt: {receipt}"` -- the real diagnostic never reaches the terminal. `upload()`'s `TransferFailure`/`VolumeTransferRefusal`/`OSError`/`ValueError` handler (line 761) does the same thing with `str(error)`. This is inconsistent with sibling paths in the same file (e.g. RUN_HELD prints every hold reason via `self.present(...)` before raising, BOOT_RED prints its remediation before raising) and with review's own CONSOLE_TREE_UNREADABLE, which inlines the full detail.

**Scenario.** Run `verbatus run --run-id t1 --submission-folder <folder-outside-every-approved-root> --submission-manifest <manifest>`. The terminal shows the generic three-part RUN_FAILED message plus only a receipt file path as "Saved detail". The actual reason -- "the submitted folder is outside every approved storage root ['/home/user/Apparatus-Verbatus-Alpha/private']" -- is present only inside the JSON receipt at that path, which the operator must separately locate and open to learn what to fix, despite the perspective's own promise that a failure message names "the exact file, path, run id, stage or digest involved."

**Proposed fix.** Include the captured `detail` text (already written to the receipt) directly in the OperatorError's detail alongside the receipt path, the same way RUN_HELD and BOOT_RED already surface their concrete reasons before raising.


### F004 [low] Top-level flags fail unhelpfully when typed after the verb

`operations/operator/cli.py:137` — operability — from Troubleshootability and operator error m; verified by running: True

**Claim.** `build_parser()` defines `--workspace`, `--state-dir`, and `--notify` on the top-level parser, before the `verb` subparsers are added. Argparse subparsers do not accept parent-parser options once the subcommand token has been consumed, so any of these flags placed after the verb (a very natural ordering for a CLI, and the only ordering shown for verb-specific flags in every documented example) is rejected as an unrecognized argument rather than being accepted or specifically explained.

**Scenario.** Running `.venv/bin/python -m operations.operator.cli review --run-root <root> --run-id <id> --state-dir <dir>` (flag after the verb) produces INVALID_COMMAND: "unrecognized arguments: --state-dir <dir>" with no indication that the same flag placed before `review` would have worked; an operator unfamiliar with argparse's subparser ordering rules has no way to infer the fix from the message alone.

**Proposed fix.** Either duplicate --workspace/--state-dir/--notify onto every subparser so they work in either position, or have the INVALID_COMMAND copy for an "unrecognized arguments" case that matches one of these three flag names say explicitly that top-level options must precede the verb.


### F005 [high] DAI's real-roster 24gb row keeps a gpu fraction its own file says is insufficient

`config/serving_recipes_real.toml:506` — config — from Configuration and pins for the real run ; verified by running: False

**Claim.** config/pod_placement.toml's own U15 comment (generic-24gb tier) states, citing CORRECTION_PLAN_2026-09-06.md's VRAM arithmetic, that DAI (attestator_2) needs gpu_memory_utilization of at least 0.78 (0.90 to match the old serve_dai.sh) to be servable at all on a 24 GiB card, and raises the tier's engine_memory_fraction ceiling to 0.90 for exactly that reason. But the actual attestator_2 profile row for tier generic-24gb in serving_recipes_real.toml still carries gpu_memory_utilization = "0.58" — serving_recipes_real.toml's own header comment says only Chandra's generic-24gb row was raised (to 0.85), and that every other row's fraction is unmoved because '0.78/0.88 at the larger tiers were already ample', a justification that says nothing about attestator_2's own generic-24gb row. operations/serving/preflight.py only refuses gpu_memory_utilization > engine_memory_fraction (a ceiling violation); nothing checks a row against the VRAM floor the project's own arithmetic already computed for it, so a too-low value like 0.58 passes every existing gate silently.

**Scenario.** Tyrel rents a 24 GiB card (the RTX A5000, the only card affordable under config/spend.toml's max_hourly_usd=0.40 per operations/pod/test_card_allowlist.py's Stage-1 constant) and boots the real roster. Preflight and config validation both pass. When ServingManager launches attestator_2 (DAI) with gpu_memory_utilization=0.58 (13.9 GiB of a 24 GiB card), it is launched against a VRAM budget the project's own prior arithmetic already found insufficient (18.7 GiB needed) — a live-testing session spends pod time discovering a config defect that was already known and simply never propagated into the file that matters.

**Proposed fix.** Either update attestator_2's generic-24gb row to gpu_memory_utilization="0.90" (matching the tier comment and old serve_dai.sh), or add a review note if 0.58 is now believed sufficient for a different reason, and add a test pinning each real-roster row's gpu_memory_utilization against any VRAM-need figure the config comments assert.


### F006 [critical] Stage 1's only affordable card cannot serve the real Perlector chair at all

`config/spend.toml:91` — config — from Configuration and pins for the real run ; verified by running: False

**Claim.** config/spend.toml's max_hourly_usd is "0.40", and per config/pod_placement.toml's card_profile table the only reviewed card at or under that price is the RTX A5000 (generic-24gb tier, $0.27/hr); the A40 ($0.44), RTX 6000 Ada ($0.77) and RTX PRO 6000 Blackwell ($1.99) all exceed it. operations/pod/test_card_allowlist.py confirms the RTX A5000 is deliberately the named Stage-1 card. But serving_recipes_real.toml documents in-line that the Perlector's Qwen3.8-27B weights measure 51.7 GiB of bf16 — more than either the generic-24gb or generic-48gb tier holds before a token of KV cache or vision peak is counted — so 'only generic-80gb-plus can ever serve it', with its generic-24gb/48gb rows kept 'for catalogue coverage, not as a claim that a 24 or 48 GiB card could run them.' generic-80gb-plus is served only by the $1.99/hr Blackwell card in the reviewed table, which the current spend ceiling refuses outright.

**Scenario.** Given the real roster (models-real.toml) has the Perlector chair configured (not absent), any real run launched under the currently configured spend policy is mechanically restricted to a card that cannot serve the Perlector under any row this catalogue ships — the pod would either be refused the card class that could work, or (if a real pipeline pass is attempted with the real trio on the A5000) the Perlector's launch would be attempted against a profile the project's own documentation already calls unservable, wasting rented GPU time on a failure that was knowable from the committed configuration alone.

**Proposed fix.** Before the first live test that exercises the real Perlector chair, resolve this named conflict explicitly with Tyrel (hard rule 1: he decides spend/live infrastructure) — either scope Stage 1 to witness-only real serving with the Perlector kept absent/fixture, or raise max_hourly_usd to admit a generic-80gb-plus card, and record the decision rather than letting it surface as a pod-time failure.


### F007 [high] Permission-refusal test fails when the suite runs as root

`common/runtree/test_runtree_store.py:2322` — test-gap — from test quality and end-to-end gaps; verified by running: True

**Claim.** test_the_shared_snapshot_fails_loudly_on_a_descendant_it_cannot_read chmods a directory to 0 and unconditionally asserts pytest.raises(OSError) from tree_snapshot(root), with no guard for a process (root, or any uid with CAP_DAC_OVERRIDE) that can read through the mode-000 bit. Four other tests in the very same file guarding the identical premise (e.g. lines ~1470-1485) first check `os.access(blocked, os.R_OK)` and `pytest.skip(...)` if the process can read the locked path anyway — this test alone omits that guard.

**Scenario.** Run `.venv/bin/python -m pytest -p xdist -x -q` as the container's root user, as this environment's frozen interpreter is invoked here and as CI containers commonly run: chmod(0) has no effect for root, os.walk succeeds, and the test fails with `Failed: DID NOT RAISE OSError` — a false-red result that has nothing to do with the change under review. Verified by running: reproduced identically across two independent full-suite runs (both stopped at the same position) and isolated with a targeted -x run.


### F008 [critical] No test carries a real submission past the Designator

`pipeline/test_real_ingress_contexts_e2e.py:491` — e2e-gap — from test quality and end-to-end gaps; verified by running: False

**Claim.** The suite's own docstrings state plainly that a real submission is refused by the fixture structure chair at the Door/Designator boundary, and that the Recensor is 'the first stage to ask the Designator for something a hand-built structural layer cannot honestly supply' — so the run stops there by design, writing nothing, and the Archetypus and Armarium are only ever proven to refuse cleanly on a stopped run, never to actually establish or export a real act. The whole of Recensor's completeness/recovery logic, Archetypus's establishment logic, and Armarium's export/backup/review logic are therefore proven only against the fixture 'synthetic-two-page-v0' catalogue, never against a real page.

**Scenario.** Tyrel submits a real scanned register folder for live testing. Ingest, upload, Designator (fixture-catalogue mode) and Attestatores/Perlector complete, but the Recensor refuses the whole run with 'Designator conservation pages 1, 2 carry non-held expected acts but have no conservation records' the first time it is asked for the real per-page conservation denominator — exactly the failure this test file predicts and pins as today's honest boundary. This is not a hidden defect (the repo names it as roadmap item 4), but it means the very first live run on real material cannot reach Recensor, Archetypus, or Armarium at all unless the real structural pass has since landed; no test in the suite would catch a regression in that boundary once it does land, because none exercises it end to end.


### F009 [high] Acceptance suite never exercises a real model or real pod provider

`pipeline/orchestrator/test_orchestrator_acceptance.py:4751` — e2e-gap — from test quality and end-to-end gaps; verified by running: False

**Claim.** test_the_run_used_no_network_and_no_model explicitly asserts every configured chair's adapter revision starts with 'fake-' and every chair source is 'local-repository' — by the suite's own design, nothing in it can ever have reached Hugging Face or a live endpoint. config/models.toml confirms the real roster is commented out and 'does not participate in resolution... or the fixture run.' Likewise operations/pod/test_provider_runpod.py drives a hand-scripted ScriptedTransport whose JSON bodies encode the author's belief about RunPod's v1 API shape, never the real service, and operations/operator/test_surface.py exercises only OperatorFakeProvider. There is a real Protocol seam (operations/pod/provider.py) but no shared contract-conformance suite that runs the same test cases against both the fake and the real RunPod adapter, so nothing guards the two from silently diverging in behavior for the same PodCreateRequest.

**Scenario.** The real RunPod API returns a field, status code, or error shape the hand-authored ScriptedTransport fixtures never modeled (e.g. a v1 endpoint deprecated in favor of v2, a renamed lifecycle field, a different balance-observation payload) — the live pod session in the upcoming real test is the first and only place this would be discovered, with money and time already spent. Similarly, a real vLLM server's response shape drifting from what operations/serving/fakes.py encodes would only surface during the first live model run, not in the gate that is meant to catch it beforehand.


### F010 [medium] Vendor byte-fidelity check is excluded from every default run

`common/test_vendor_parity.py:20` — e2e-gap — from test quality and end-to-end gaps; verified by running: False

**Claim.** Test 5 ('Vendor equality') is the only test that would catch the carried vendor prompt/resize bytes drifting from the real upstream chandra/churro/dai sources, and it is marked `vendor_network` and self-skips unless `-m vendor_network` is explicitly passed — neither check-fast.sh, check-all.sh, nor a plain `pytest` invocation without `-m` selects it (confirmed by reading pyproject.toml's marker definition and the corresponding self-skip logic the docstring describes). Every other run only compares the repo's carried bytes against a digest recorded beside them, which proves internal consistency, not fidelity to the vendor.

**Scenario.** A vendor patches OCR_LAYOUT_PROMPT or a resize routine upstream after the pins were last taken; the gate stays green forever because it only ever compares the repository to itself. This is a deliberate, governed design choice (CLAUDE.md's settled ruling on vendor-licence analysis), so it is not a defect, but it is a genuine untested seam worth naming to whoever schedules live testing: nothing routine will notice vendor drift.


### F011 [low] Acceptance file dominates full-gate wall-clock time

`pipeline/orchestrator/test_orchestrator_acceptance.py:1` — operability — from test quality and end-to-end gaps; verified by running: True

**Claim.** test_orchestrator_acceptance.py is 7307 lines and, because each acceptance test shells out to one or more real stage subprocesses, a single xdist worker holding this file (under `--dist loadfile`) becomes the long pole of the whole run: in two independent runs in this environment, four workers processing every other file finished quickly while the acceptance file's worker dominated wall-clock time from roughly 30% to past 70% of the reported total, taking several minutes on its own. CLAUDE.md's hard rule 14 requires the full local gate (`check-all.sh`) to exit 0 on the exact merged head before a merge; a gate this slow to run to completion is the kind of friction that produces exactly the 'never chain a push behind piped output' and 'never merge on a partial gate' failure modes the same document already warns about.

**Scenario.** A session under time pressure runs the full gate, sees it apparently hang at ~50-70% for minutes, and is tempted to background it, redirect it to a file and move on without confirming it actually reached exit 0 — precisely the anti-pattern CLAUDE.md's 'never chain a push behind piped test output' rule was written to prevent, just applied to patience rather than a pipe.


### F012 [info] This review's own full-suite run did not finish inside its time budget

`conftest.py:268` — test-gap — from test quality and end-to-end gaps; verified by running: True

**Claim.** Two full attempts at the exact specified command each got most of the way through (the first was accidentally killed by this reviewer at ~50%; the second reached ~71% before the review's time budget ran out) without producing any additional failures beyond the one confirmed above, and with only skip markers ('s') besides. This is a limitation of this review's execution budget, not a claim that the rest of the suite is clean.

**Scenario.** n/a — disclosed for honesty about what was and was not measured, per this project's own Governance 10.


### F013 [high] A model's data-bbox with a huge integer crashes the Designator instead of refusing

`common/chandra_layout.py:501` — correctness — from Hostile input and trust boundaries: ever; verified by running: True

**Claim.** parse_bbox_attribute accepts any component matching `[+-]?[0-9]+` with no length bound and then calls int(part). CPython 3.11+ refuses str->int conversion above 4300 digits with a plain ValueError, which is not a json.JSONDecodeError and is caught nowhere on this path. parse_layout_html — whose whole documented contract is "Chandra's layout answer, read whole, or one named refusal" — therefore raises ValueError instead of returning a `malformed-bbox` finding, and pipeline/2_designator/structure_pass.py:1116 calls it with no try/except. common/stage.py::run_stage catches only RunHalted and ContractError, so the Designator dies with a traceback and exit 1: the page, and every page after it in the run, produce no artifact at all. The same missing bound means the refusal-message path `_quoted` (MAX_QUOTED_ATTRIBUTE_CHARACTERS) never gets to apply, because int() runs first.

**Scenario.** A Chandra response containing `<div class="Text" data-bbox="999...9 1 2 3">hi</div>` with 5000 digits in the first component. Ran: `parse_layout_html(html)` -> ValueError: Exceeds the limit (4300 digits) for integer string conversion. Expected: a ParsedLayout carrying a `malformed-bbox` finding for that block.

**Proposed fix.** Bound the component before converting — e.g. `_BBOX_COMPONENT = re.compile(r"[+-]?[0-9]{1,10}")` (BBOX_SCALE is 1000, so ten digits is already absurdly generous) — and additionally wrap `int(part)` in `except ValueError: return None, "components are not plain decimal integers"` so no future regex change can reopen it. Add a regression test at 5000 digits beside the existing malformed-bbox tests.


### F014 [high] fetch-run escapes unclassified on a hostile manifest.json and writes no receipt

`operations/operator/surface.py:3451` — silent-failure — from Hostile input and trust boundaries: ever; verified by running: True

**Claim.** _fetched_manifest catches (UnicodeDecodeError, ValueError, OSError) around json.loads, and the sibling index/receipt arm at line ~3224 catches (UnicodeDecodeError, ValueError). json.loads raises RecursionError — not a ValueError — for deeply nested but otherwise valid JSON; common/runtree/store.py::_read_json_with_bytes catches it for exactly this reason, and this module does not. The RecursionError then escapes _fetch_run_tree (whose `except BaseException` only unwinds staged files and re-raises) and escapes OperatorSurface.fetch_run's handler at line 905, which catches only (FetchRunRefusal, VolumeTransferRefusal, ContractError, OSError). No fetch-run receipt is written at all, and operations/operator/cli.py's catch-all reports ErrorCode.UNEXPECTED ("a problem it could not classify"). GOVERNANCE 2's durable record of the failure does not exist; only the terminal line does.

**Scenario.** A volume under runs/<id>/ holding a valid run.json, its Door register blob, and 1_exemplar/manifest.json containing 200,000 nested `[`. Ran with a fake RunObjectReader (scratchpad t6.py): `_fetch_run_tree(...)` -> ESCAPED UNCLASSIFIED: RecursionError: maximum recursion depth exceeded while decoding a JSON array from a unicode string. Expected: FetchRunRefusal naming the object, and a saved partial fetch-run receipt.

**Proposed fix.** Add RecursionError to both except tuples in _fetch_run_tree/_fetched_manifest (raising FetchRunRefusal), and widen fetch_run's handler at line 905 to also catch RecursionError and MemoryError so a receipt is always written before the refusal is raised.


### F015 [high] upload escapes unclassified on a bad sealed submission manifest and writes no receipt

`operations/operator/surface.py:748` — silent-failure — from Hostile input and trust boundaries: ever; verified by running: True

**Claim.** OperatorSurface.upload wraps ChecksummedTransfer.resume() in `except (TransferFailure, VolumeTransferRefusal, OSError, ValueError)`. resume() calls operations.submit.submit.load_manifest, which raises SubmitRefusal for a manifest that is unreadable, non-canonical, or structurally wrong. SubmitRefusal derives from ContractError, which derives from Exception and NOT from ValueError (common/contracts/errors.py:10). The refusal therefore escapes the handler: no upload receipt is written, the ErrorCode.UPLOAD_PARTIAL path with its "can be resumed from its verified files" instruction never runs, and the operator gets cli.main's UNEXPECTED catch-all. This is the one verb that precedes paid work, on a file an operator names on the command line and that a corrupt transfer can have damaged.

**Scenario.** `verbatus upload --sealed-manifest <path>` where the file is valid JSON but not a sealed manifest (e.g. `{"schema":"nope"}`) — a truncated copy, a wrong file, or bytes damaged in transit. Ran (scratchpad t7.py): ChecksummedTransfer(...).resume() -> ESCAPES UPLOAD HANDLER: SubmitRefusal: submission manifest has an unexpected shape. Expected: a saved upload receipt and OperatorError(UPLOAD_PARTIAL).

**Proposed fix.** Add ContractError to upload()'s except tuple (it is already imported in this module and is what every other verb here catches), so the receipt is written and the named refusal is raised.


### F016 [medium] Pipeline stage subprocesses inherit every provider credential except the two S3 keys

`operations/operator/surface.py:2730` — security — from Hostile input and trust boundaries: ever; verified by running: True

**Claim.** _stage_environment() copies os.environ and removes only _TRANSFER_CREDENTIAL_ENV = {RUNPOD_S3_ACCESS_KEY, RUNPOD_S3_SECRET_KEY}. It is the environment handed to both stage subprocesses (line 1162, the orchestrator run; line 2122, the Door). Those are the processes that decode attacker-supplied PDFs, TIFFs, HEICs and PNGs through Pillow, pypdfium2 and pillow_heif, and that talk to the serving endpoint. RUNPOD_API_KEY (pod creation = money), HF_TOKEN, AWS_*, GITHUB_TOKEN and ANTHROPIC_API_KEY all survive. The repository already owns the correct predicate: operations/operator/custody.credential_free_environment() strips all of them and is used for the console, backup, advance and ScanTailor children — the children that handle *less* hostile material than these two do. The docstring's reasoning ("pipeline stages neither upload nor inspect a network volume") argues for removing the S3 keys but never argues for keeping the rest.

**Scenario.** With RUNPOD_API_KEY=fake-secret-abc and HF_TOKEN=hf_fake exported, ran `_stage_environment()`: returns {'RUNPOD_API_KEY': 'fake-secret-abc', 'HF_TOKEN': 'hf_fake', 'RUNPOD_S3_ACCESS_KEY': None}; `credential_free_environment()` on the same environment returns None for all three. A decoder RCE (Pillow/PDFium CVEs are routine) in a stage reached by a submitted page therefore lands with the key that can create billable pods.

**Proposed fix.** Build _stage_environment on credential_free_environment(), re-adding by name only the variables a stage genuinely needs (HF_TOKEN only if a stage actually fetches from the Hub; if it does, that fetch belongs in the model-store step, not in the page-decoding step). Add a test asserting that no name satisfying looks_like_credential_field survives _stage_environment().


### F017 [medium] A witness response with a huge integer escapes the closed parse-outcome boundary

`pipeline/3_attestatores/chandra.py:539` — correctness — from Hostile input and trust boundaries: ever; verified by running: True

**Claim.** _decode catches only (UnicodeDecodeError, json.JSONDecodeError) around json.loads, plus RecursionError separately. CPython's int-digit limit raises a bare ValueError, which is neither, so a witness response carrying an integer literal longer than 4300 digits escapes a function whose declared contract is "a value or one closed parse outcome" and whose PARSE_OUTCOMES set exists precisely so one bad response is never permission to crash the stage. operations/serving/http.py::_json_object and parse_openai_reading already use the correct catch set (UnicodeDecodeError, ValueError, RecursionError) for the same wire; this boundary does not.

**Scenario.** Raw response bytes `{"blocks":[{"bbox":[999...9,1,2,2]}]}` with 5000 digits. Ran: `chandra._decode(payload)` -> ValueError: Exceeds the limit (4300 digits) for integer string conversion. Expected: (None, 'invalid-json').

**Proposed fix.** Change the arm to `except (UnicodeDecodeError, ValueError)` (json.JSONDecodeError is a ValueError, so this is a strict widening), keeping the RecursionError arm above it.


### F018 [medium] RunTree.read_bytes is unbounded, so the fetch path reads a 256 MB manifest whole

`common/runtree/store.py:791` — correctness — from Hostile input and trust boundaries: ever; verified by running: False

**Claim.** read_bytes is `self.resolve(relative_path).read_bytes()` with no size ceiling, and _read_json_with_bytes (line 1671) is the same. The module bounds its manifest *walk* at _MAX_MANIFEST_ARTIFACT_BYTES = 64 MB with the stated reason that "one malformed artifact must not be able to allocate the process out of existence" — but every direct read (read_artifact_snapshot, read_artifact_reference, read_run_receipt, read_approval_record, and via them common/exemplar_boundary._read_checked and common/chandra_custody._read_custody_bytes) bypasses that bound. operations/operator/review.py:78 already works around this locally with _budgeted_image_bytes, documenting the same gap. On a fetched tree the practical ceiling is fetch-run's MAX_FETCH_OBJECT_BYTES = 256 MB per object, so a hostile volume can make the fetch verb read and json.loads a 256 MB manifest.json in one go.

**Scenario.** A run tree on the volume with 1_exemplar/manifest.json at 256 MB of valid JSON. _fetch_or_compare accepts it (inside max_bytes), then _fetched_manifest calls tree.read_bytes(relative).decode('utf-8') and json.loads on it — roughly a 1–2 GB peak. On a laptop the OOM killer ends the verb with no receipt (same handler gap as the RecursionError finding). Concluded by reading the code paths; the 256 MB ceiling and the absent bound were both read directly.

**Proposed fix.** Give RunTree a bounded reader (stat + read(limit+1) under the existing _MAX_MANIFEST_ARTIFACT_BYTES, or a per-call max_bytes) and route read_bytes/_read_json_with_bytes through it; have _fetched_manifest ask for the manifest-sized bound explicitly.


### F019 [medium] File-chosen bytes from a submitted image reach a push notification to ntfy.sh

`pipeline/1_exemplar/image_formats.py:1306` — security — from Hostile input and trust boundaries: ever; verified by running: True

**Claim.** decode_raster and _decoder_only interpolate Pillow's own exception text into the refusal detail (`...could not open these bytes ({error})`), and this module's own walkers interpolate values read out of the file (`PNG: chunk {chunk_type!r} fails its own CRC`, `TIFF: tag {tag} ...`). admission.inspect_source turns that into the page's alarm reason; the Exemplar page census carries it; common/contracts/outcomes.py:1060 folds it into `reasons` as `page {ordinal} was {outcome}: {reason}`; and operations/operator/surface.py:1256 joins every reason into a `decision` notification that operations/notify/notify.sh posts to https://ntfy.sh. So bytes chosen by whoever produced the submitted file are transmitted to a third-party service on a bearer topic, in a project whose data-handling rule exists to keep submitted material off operational channels. The quantity is small (a 4-byte PNG chunk name, a TIFF tag number) but it is attacker-chosen and it leaves the machine.

**Scenario.** Submit a PNG whose second chunk is named `LEAK` with a bad CRC. Ran admission.inspect_source on it: reason = "corrupt: corrupt PNG: chunk b'LEAK' fails its own CRC". Follow the chain by reading: that string becomes a hold reason and is sent verbatim in the `decision` ping. Any four bytes the submitter picks arrive on the phone and in ntfy's server-side message history.

**Proposed fix.** Either strip file-derived interpolations from the *published* reason (keep them only in the private refusal report, as operations/submit/inventory.py already does for names), or make _notify send the reason codes and counts rather than the full reason text, with the text left on the console and in the receipt.


### F020 [medium] The decision notification's message is unbounded and derived from run-tree reason strings

`operations/operator/surface.py:2307` — operability — from Hostile input and trust boundaries: ever; verified by running: False

**Claim.** _notify collapses whitespace but applies no length ceiling, and notify_bridge.shell_notifier passes the message straight into `["sh", notify.sh, event, message]` as an argv element; notify.sh puts it in a JSON body posted to ntfy.sh. The `decision` message is built by joining every hold reason in the Armarium aggregate, one per unresolved act and per unsealed page. A run held on hundreds of pages produces a message of hundreds of kilobytes, which the notification service will reject (or the exec will fail with E2BIG). The failure is reported honestly — notify.sh exits 1 and the bridge says NOT DELIVERED — but the outcome is that the ping does not arrive in exactly the case it exists for: a semi-attended run that stopped and needs Tyrel.

**Scenario.** A 400-page volume where the Door refuses 300 pages (a bad scan batch). aggregate['reasons'] holds ~300 strings of ~80 characters; _notify builds a ~24 KB single-line message; the post is refused and the operator, away from the terminal, hears nothing about a run that is waiting on them. Concluded by reading _notify, shell_notifier and notify.sh; no length check exists in any of the three.

**Proposed fix.** Bound the notification body in _notify — send the count and the first few distinct reason codes, plus "the receipt lists all N" — so the ping is always small enough to deliver and the full text stays on the console and in the saved receipt.


### F021 [medium] The Perlector re-reads the sealed page blob by path with no digest check and no named refusal

`pipeline/4_perlector/dossier.py:173` — correctness — from Hostile input and trust boundaries: ever; verified by running: False

**Claim.** build_page_render takes `image_path` out of the Exemplar page artifact's payload and calls context.tree.read_bytes(source_path) directly. Everywhere else this repository reads sealed pixels through common/exemplar_boundary._read_checked, which verifies the bytes against the recorded sha256 before use; here nothing does, even though the digest is right there in the same payload as `source_sha256`. Worse, the read is outside the try/except that follows it, so a FileNotFoundError (a removed or never-fetched blob) escapes as a bare OSError — and common/stage.py::run_stage catches only ContractError, so the Perlector ends with a traceback rather than the named provenance refusal RunTree.read_run_receipt's own docstring says this case must get. The page is verified earlier in the stage through verify_exemplar_crop_lineage, so this is a re-read of already-checked evidence rather than an unverified first read, which is what keeps it medium rather than high.

**Scenario.** A run tree fetched from a pod in which one Door blob did not come home (fetch-run names it in `refusals` and carries on). The Perlector reaches _page_renders_for for an act on that page and dies with FileNotFoundError and exit 1, losing every act after it in the stage, instead of holding that page and continuing. Concluded by reading dossier.py:163-190, run.py:2114, and run_stage at common/stage.py:4551.

**Proposed fix.** Read through common.exemplar_boundary._read_checked (or an equivalent that passes {'relative_path': image_path, 'sha256': payload['source_sha256']}) and move the read inside the existing try, raising SchemaRefusal for both the missing file and the digest mismatch.


### F022 [low] structure_answer's declared refusal boundary lets a huge integer escape as ValueError

`common/structure_answer.py:186` — correctness — from Hostile input and trust boundaries: ever; verified by running: True

**Claim.** decode_json_body catches only (UnicodeDecodeError, json.JSONDecodeError) plus RecursionError and DuplicateJsonMember; a bare ValueError from CPython's int-digit limit escapes, so parse() raises instead of returning one of PARSE_OUTCOMES. The module's docstring and _refuse() go to some length to guarantee "a closed record — never a code outside the set", and this breaks that guarantee. Severity is low only because pipeline/3_attestatores/chandra_response.py no longer exists and pipeline/2_designator/structure_prompt.py:26 records this contract as retired, so parse() has no live caller today — but unique_json_object and validate_box_1000 are still documented as shared, and a module kept as a contract boundary should not have a hole its live twin also has.

**Scenario.** parse(b'{"schema":"verbatus-structure-answer.v1","acts":[{"box_1000":[111...1,1,2,2],"text":"x"}]}') with 5000 digits. Ran: ValueError: Exceeds the limit (4300 digits) for integer string conversion. Expected: {'parse_outcome': 'invalid-json'}.

**Proposed fix.** Widen to `except (UnicodeDecodeError, ValueError)` after the RecursionError and DuplicateJsonMember arms.


### F023 [low] Chandra custody blobs are parsed without a RecursionError arm

`common/chandra_custody.py:167` — correctness — from Hostile input and trust boundaries: ever; verified by running: False

**Claim.** Both json.loads sites here (line 167 in the binding read, line 221 in _is_custody_binding) catch (UnicodeDecodeError, ValueError) only. json.loads raises RecursionError for deeply nested valid JSON, and the digest check that precedes line 167 proves only that the bytes are the ones the reference names — on a run tree fetched from a pod the attacker controls both the blob and the reference, so a matching digest is no protection at all. The refusal escapes this boundary as a RecursionError rather than the SchemaRefusal every other path here raises.

**Scenario.** A fetched run tree whose Chandra custody binding blob is 100,000 nested arrays, with the reference digest updated to match. verify/read of that binding raises RecursionError out of a stage whose only handler is run_stage's ContractError arm, so the stage exits 1 with a traceback rather than refusing the custody record. Concluded by reading; the underlying json.loads behaviour was confirmed by running (the same RecursionError was reproduced against chandra._decode).

**Proposed fix.** Add RecursionError to both except tuples, raising SchemaRefusal, matching common/corpus_register.py:502 which already separates the two causes for the same reason.


### F024 [low] Armarium JSONL readers catch only JSONDecodeError, so ValueError and RecursionError escape

`pipeline/7_armarium/armarium_export.py:3392` — correctness — from Hostile input and trust boundaries: ever; verified by running: False

**Claim.** The four JSONL row readers (lines 3011, 3038, 3076, 3118) and the two act-record readers (3392, 4417) all use `except json.JSONDecodeError`, which is narrower than the ValueError family json.loads actually raises: the int-digit-limit ValueError and RecursionError both pass straight through the named SchemaRefusal boundary. _package_lines (line 2912) additionally reads the whole member with path.read_bytes() and no ceiling. These readers run over an export package that can have travelled home from a pod, so the bytes are not necessarily ones this machine wrote.

**Scenario.** A verify/read of an export package whose acts.jsonl carries one row containing a 5000-digit JSON number, or a row nested 100,000 deep. The reader raises ValueError/RecursionError instead of SchemaRefusal("an acts JSONL row is not JSON"), so the caller's refusal handling is skipped. Concluded by reading; I did not construct a producer that emits such a row from model output, so the reachability from the pipeline's own writers is unproven — the hostile-package path is the one that matters.

**Proposed fix.** Use `except ValueError` (JSONDecodeError is a subclass) plus a RecursionError arm at all six sites, and bound _package_lines with an explicit member ceiling.


### F025 [low] A ScanTailor project can name absolute paths that are resolved and recorded unchecked

`operations/operator/scantailor_worker.py:131` — security — from Hostile input and trust boundaries: ever; verified by running: False

**Claim.** source_path is built as `(project_path.parent / file_paths[fileId]).resolve()` where file_paths comes from the project's own `<directory path=...>` joined with `<file name=...>`. Neither is validated: an absolute `path` makes the `/` join discard project_path.parent entirely, and `..` components are accepted. The result is .resolve()d against the real filesystem — which follows symlinks and therefore probes paths outside the workspace — and is then written verbatim into the published scantailor-geometry.v1 document. The worker never opens the file, so this is not a read primitive today; it is an unvalidated attacker-chosen absolute path entering a sealed record, and a symlink-resolution probe of arbitrary paths. Every other path boundary in this repository (inventory._source_components, gate.require_approved_storage_location, transfer._under, RunTree.resolve) refuses exactly this shape.

**Scenario.** A project file containing `<directory id="1" path="/Users/tyrel/.ssh"/><file id="1" dirId="1" name="id_ed25519"/>` plus matching image and page-split records. The confined import succeeds and writes a geometry document whose source_path is /Users/tyrel/.ssh/id_ed25519 (or its symlink target). Concluded by reading parse() at lines 104-135; the confinement means no bytes are read, so I did not construct the file.

**Proposed fix.** Refuse a directory path or file name that is absolute, empty, or contains a `..` / `.` component, and require the resolved source_path to be inside project_path.parent — the same rule transfer._under already spells out.


### F026 [low] The submission walk's aggregate byte bound is dead for both production callers

`operations/submit/inventory.py:561` — correctness — from Hostile input and trust boundaries: ever; verified by running: False

**Claim.** The module docstring says "The walk is bounded in four directions" and names aggregate retained bytes as one of them, but _Budget.admit is given `len(data) if data is not None else 0`, and _read_once returns data=None whenever size > max_bytes. Both production callers (submit.walk_folder and door.read_bytes) pass max_bytes=0, so retained is always 0 and MAX_SUBMITTED_BYTES can never trip. What actually binds a production submission is the file count, the depth, and the entries per directory — not the total bytes read, which is unbounded because _read_once streams the whole file to the hash regardless of size. The comment at line 68 half-acknowledges this ("their later source reads are bounded separately"), but the bound the docstring advertises is not one of the four in force.

**Scenario.** A submitted folder holding 100 sparse files of 1 TB each. read_submission hashes 100 TB — hours of I/O with no refusal and no progress output — and returns successfully, because every retained-byte charge was zero. Expected on a project that refuses "rather than reading until it stops": a named refusal on total bytes. Concluded by reading _read_once and _Budget.admit.

**Proposed fix.** Charge `size`, not `len(data)`, against a separate aggregate-read ceiling (keeping MAX_SUBMITTED_BYTES for retained bytes if the distinction is wanted), and correct the docstring to name the bounds that are actually in force for max_bytes=0 callers.


### F027 [low] upload's receipt says "complete" even when the transfer report says nothing-to-transfer

`operations/operator/surface.py:771` — silent-failure — from Hostile input and trust boundaries: ever; verified by running: False

**Claim.** The success receipt hardcodes `"state": "complete"` and the console line "Upload complete. Every file in the sealed record was verified.", while the nested `report.to_record()` can say `"state": "nothing-to-transfer"` — the state TransferReport.to_record was deliberately given so that "a partial result is visibly partial" (GOVERNANCE 2). Two fields in one receipt then disagree, and the sentence a person reads is the wrong one. It is unreachable through this verb today only because upload writes a manifest snapshot before constructing ChecksummedTransfer, so `submission_manifest.is_file()` is always true; the guarantee lives in the construction order rather than in the reporting, and the same TransferReport is produced by the pod-side caller (operations/pod/bootstrap_main._build_transfer) where the branch is live.

**Scenario.** Any future refactor that passes the operator-named manifest path straight through — or a copy of this reporting block reused for the pod-side report — produces a receipt whose top-level state is "complete" for a transfer that moved nothing. Concluded by reading upload() at lines 737-780 against transfer.TransferReport.to_record.

**Proposed fix.** Derive the receipt's top-level state and the printed sentence from report.submission_manifest_present rather than hardcoding them, so the two halves of the receipt cannot disagree.


### F028 [high] A failed run never names its cause on any screen

`operations/operator/surface.py:1179` — operability — from END-TO-END EXECUTION — I ran the operato; verified by running: True

**Claim.** When the orchestrator exits non-zero, surface.py:1175 writes the child's stderr into the receipt's `detail` field, but surface.py:1179 raises RUN_FAILED with `detail=f"Saved run receipt: {receipt}"` — so the screen shows a file path where the reason should be. The error copy then sends the operator to `verbatus status`, and `_status_projection` (surface.py:2416-2422) renders only `state` and `last_observed_work` for a run action, deliberately never `detail`. The result is that every distinct failure produces the same four opaque lines, and the documented next step shows nothing new. This is not hypothetical breadth: I reproduced it for six different causes, each of which had a perfect one-line explanation sitting unread in the receipt. The same function shows the reason on its other failure path (surface.py:1192 passes `detail=str(error)`), so the asymmetry is inside one method. README.md's operator section promises "You will never see a raw error with no explanation"; here the explanation exists and is withheld.

**Scenario.** `.venv/bin/python -m operations.operator.cli --state-dir $S/state2 run --run-id rreal --models-config config/models-real.toml --serving-recipes-config config/serving_recipes_real.toml --witness-context-config config/witness_context-real.toml` prints "What happened: The run could not reach its recorded end state. … Next step: Run `verbatus status`". `verbatus status` then prints "- run record 1: Run ended before its Armarium record was available. Saved run state: failed." and nothing else. The receipt holds `SERVING_MODE_UNRESOLVED: a live serving profile needs the measured placement tier; pass --placement-tier`. Identical opaque screens were produced by: a resume with the wrong roster (`IncompatibleReuse: … bound to different config_digest, adapter_recipes`), a resume with the wrong scenario (same class), a real submission without a manifest (`a real submission requires --submission-manifest`), a malformed run id (`IdentityRefusal: run_id '../escape' is not a plain lowercase name…`), and running from a non-checkout directory (`can't open file '…/elsewhere/pipeline/orchestrator/run.py'`).

**Proposed fix.** Pass the recorded reason into the operator error at surface.py:1179 (e.g. `detail=f"{first_line_of(completed.stderr)} Saved run receipt: {receipt}"`), and add `detail` to the `run` branch of `_status_projection` the way the `boot` branch already renders `remediation`.


### F029 [high] RUN_HELD's next step is a closed loop the operator cannot escape

`operations/operator/errors.py:228` — operability — from END-TO-END EXECUTION — I ran the operato; verified by running: True

**Claim.** The RUN_HELD copy says "Run `verbatus status` to read the hold reasons, resolve them, then run `verbatus run` again with the same run name; this is safe." Neither half works. The hold reasons are not written into the run receipt at all (only `state: partial`), so `status` before an export prints two lines and no reason; the reasons only reach `status` once an `export` receipt exists, which the message does not mention. And re-running the same run id over a sealed Armarium simply republishes and re-reports the identical hold, with no path out. `review.py`'s own next-action text states the correct answer — "A hold is resolved only by a new authorized run over the same sealed source" — so the operator-facing copy contradicts the projection's copy on the same subject.

**Scenario.** `run --run-id rref --scenario refused-page` exits 2 with four named hold reasons on screen. `verbatus status` then prints only "- run record 1: Run finished with recorded state: partial. Saved run state: partial." Following the message's second half, `run --run-id rref --scenario refused-page` again prints the same four hold reasons and the same message, indefinitely. Only after `export --run-id rref` does `status` show the reasons.

**Proposed fix.** Record the aggregate's `reasons` list in the run receipt and render it in `_status_projection`'s run branch, and rewrite the RUN_HELD next step to match review.py's own wording (a hold is cleared by a new authorized run over the same sealed source, not by re-running the same run id).


### F030 [high] The run's completeness line names pages and acts that contradict its own counts

`operations/operator/surface.py:1213` — correctness — from END-TO-END EXECUTION — I ran the operato; verified by running: True

**Claim.** The accounting line is built from two unrelated sources: the names come from `_declared_work` (surface.py:3520-3521), which reads every `[[page]]` and `[[act]]` row in `proof/skeleton_fixture.toml` regardless of scenario, while the parenthesised counts come from the Armarium export's actual records. The two disagree whenever the scenario's real extent differs from the fixture's full declaration, and they disagree in both directions — naming pages that were never processed, and omitting acts that were. This is the one line an operator reads to decide whether everything was accounted for, so a silent mismatch here is exactly the class GOVERNANCE 2 ("a partial result is visibly partial") and GOALS 1 exist to prevent. `review` gets this right from the run authority (`pages_declared`, "Pages (2 of 2 declared)"), so the correct denominator is already computed elsewhere in the same codebase.

**Scenario.** `run --run-id r1` (default `happy` scenario, 2 pages) prints "Pages accounted for: page 1, page 2, page 3 (2 total)." — page 3 is named as accounted for and was never in the run. `run --run-id inkfree --scenario ink-free-page` (3 pages, 3 acts including the minted `page-fallback:3`) prints "Acts accounted for: act a1, act a2 (3 total)." — the third act, which is the one that is held for review, is never named on the accounting line at all. `run --run-id rref --scenario refused-page`, where page 2 was refused at the door, still prints "Pages accounted for: page 1, page 2, page 3 (2 total)."

**Proposed fix.** Build the line from the export/run-authority records only (the page rows and the act partition it already reads), or drop the names entirely and print the counts plus a pointer to `review`. Do not mix a static fixture declaration with a measured count in one sentence.


### F031 [high] Moving the operator state directory makes every intact receipt read as damaged

`operations/operator/records.py:447` — transferability — from END-TO-END EXECUTION — I ran the operato; verified by running: True

**Claim.** `_record_unlocked` stores `str(receipt.resolve())` — an absolute path — in `operator-surface.json`, and `RecordStore.read` (records.py:178) refuses any path not inside the current receipt directory. Receipts are content-addressed and verify against their own filename digest, so nothing about them is machine-bound; only the descriptor is. After a copy or move of `~/.local/state/verbatus` (or of a `--state-dir`) to a different absolute path, `status` reports byte-identical, digest-verifying receipts as "UNREADABLE; it was not treated as success" and tells the operator to "repair or replace" evidence that is perfectly sound, and `export` falls through to the UNEXPECTED code whose copy tells them to photograph the message and ask for help. The run tree itself is fully portable (I grepped the copied tree: zero absolute paths, zero hostname or username occurrences, and review output was byte-identical from the new path), so the descriptor is the only thing standing between this system and a clean transfer.

**Scenario.** `cp -a $S/state1 $S/transfer/state1`, then `verbatus --state-dir $S/transfer/state1 status` → "- export record 1: UNREADABLE; it was not treated as success. - run record 1: UNREADABLE… Saved detail: operator receipt path is outside the receipt directory", exit 2. `verbatus --state-dir $S/transfer/state1 export --run-id r1` → "What happened: Verbatus met a problem it could not classify. … Copy or photograph this message … and ask for help", exit 2. I confirmed both receipts still hash to their own filenames.

**Proposed fix.** Store receipt basenames in the descriptor and resolve them against the live receipt directory on read; the content-addressed filename already carries the integrity claim the absolute path was standing in for.


### F032 [high] Five verbs are unusable on a host without kernel Landlock or util-linux 2.40+

`operations/operator/custody.py:317` — transferability — from END-TO-END EXECUTION — I ran the operato; verified by running: True

**Claim.** `LandlockConfinement.command` pins `/usr/bin/setpriv --landlock-access`, which util-linux only gained in 2.40, and the kernel must expose `landlock_create_ruleset`. On a host missing either, all five confined verbs (review, backup, advance, ingest, scantailor) refuse outright — including `review` and `backup`, which are the only troubleshooting and transfer surfaces this system has. The refusal itself is well made (three parts, names the exact missing option), but it names no remedy an operator can act on: not the required util-linux version, not the kernel requirement, and nowhere in operations/operator/README.md is a host requirement stated. Ubuntu 24.04 LTS ships util-linux 2.39.3, Debian 12 ships 2.38, and many container kernels have the Landlock LSM absent or disabled, so "another machine" is quite likely to be such a host. The consequence for live testing is that a run tree fetched home to a Linux box cannot be opened or backed up at all.

**Scenario.** On this host (`setpriv from util-linux 2.39.3`; a direct `syscall(444, NULL, 0, 1)` probe returns -1/ENOSYS): `verbatus --state-dir $S/state1 review --run-root $S/state1/runs --run-id r1` → exit 2, "Linux Landlock (setpriv) could not be established, so nothing ran inside it: /usr/bin/setpriv: unrecognized option '--landlock-access'". Same for `backup`. Running the identical projection code outside the confinement produced a complete, correct review, so the evidence was always readable — only the boundary was missing.

**Proposed fix.** State the host requirement (util-linux >= 2.40 and a kernel with Landlock enabled, or macOS with sandbox-exec) in operations/operator/README.md, and put the version and the install hint into the refusal detail so the operator can act on the message rather than on the source.


### F033 [medium] A signal-killed run leaves no operator record and status reports the machine empty

`operations/operator/surface.py:1027` — silent-failure — from END-TO-END EXECUTION — I ran the operato; verified by running: True

**Claim.** `run` writes its receipt only on an orderly end state. A SIGKILL, SIGTERM or SIGINT during a stage leaves a half-written run tree on disk with no receipt and no descriptor entry, so `status` — the verb every failure message and the README point at, and the one an operator reaches for after a crash — raises STATUS_EMPTY: "There are no saved operator records to show." The half-written tree is real and `review` reads it correctly, but nothing on any screen says it exists or where it is. SIGTERM is worse still: the process dies with no output whatsoever. And the one error code written for this case, RUN_INTERRUPTED (errors.py:214, whose copy correctly says "Run `verbatus run` again with the same run name to resume; this is safe"), is reachable only through `faults.laptop_crash`, which nothing outside the tests ever sets — so the right message is dead code in production.

**Scenario.** Kill a run mid-attestatores (`kill -KILL` / `kill -TERM` / `kill -INT` on the process group with default signal dispositions), then `verbatus --state-dir $S/state4 status` → exit 2, "There are no saved operator records to show. … Run the relevant Verbatus step first". SIGTERM produced no terminal output at all (exit -15). SIGINT produced the correct INTERRUPTED three-part message, but its "Run `verbatus status`" advice then hit the same empty result. `review --run-root $S/state4/runs --run-id rkill` showed the true state ("designator: sealed … attestatores: not-run") correctly, and resuming was byte-exact.

**Proposed fix.** Write an `interrupted-recoverable` receipt before starting the orchestrator child (or install a SIGTERM/SIGINT handler in surface.run that writes one), naming the run id and run root, so `status` can point at the tree and RUN_INTERRUPTED's existing copy becomes reachable.


### F034 [medium] The one supported resume command omits the sealed roster trio and is refused

`operations/operator/review.py:1401` — correctness — from END-TO-END EXECUTION — I ran the operato; verified by running: True

**Claim.** `_next_action` builds `verbatus run --run-id <id>` and appends `--scenario` when it can recover one, but it never mentions `--models-config` / `--serving-recipes-config` / `--witness-context-config`. A run sealed under the real trio binds those bytes into `config_digest` and `adapter_recipes`, so resuming without them is refused at the door as IncompatibleReuse — and that refusal is itself invisible because of the RUN_FAILED finding above. The evidence needed to get this right is in the tree the projection already opens: `run.json`'s `adapter_recipes` reads `unproven-real-*` for a real-roster run and `fake-*` for a fixture one. Since the first live test is precisely a real-roster run that will stop where a chair is needed, this is the resume path that matters most.

**Scenario.** After `run --run-id rreal --models-config config/models-real.toml --serving-recipes-config config/serving_recipes_real.toml --witness-context-config config/witness_context-real.toml` fails at the Designator, review prints "The supported continuation is to resume this run with `verbatus run --run-id rreal` … adding `--scenario` if this run did not use the default". Running exactly that produces `IncompatibleReuse: run 'rreal' already exists and is bound to different config_digest, adapter_recipes; a run id names one set of inputs and one configuration, so this is a different run wearing an old name. Nothing was written` — visible only inside the receipt.

**Proposed fix.** Read `adapter_recipes` from `run.json` in `_next_action`; when they are not the fixture recipes, append the trio to the printed resume command (or say that the run was sealed under a non-default roster and the same three flags must be repeated).


### F035 [medium] A mistyped run id is reported as damaged evidence to preserve and investigate

`operations/operator/review.py:323` — operability — from END-TO-END EXECUTION — I ran the operato; verified by running: True

**Claim.** `ReviewProjection.projection` wraps every `ContractError` — including `IncompatibleReuse: no run.json under <path>`, which means "nothing is here" — into CONSOLE_TREE_UNREADABLE, whose copy reads "The operator console could not read the selected run tree safely … Preserve the run tree unchanged and investigate the named evidence problem … never edit the damaged evidence in place." So pointing `--run-root` at the state directory instead of its `runs/` subdirectory, or typing a run name that does not exist, tells the operator their register evidence may be damaged. `cli._bound_run_tree`'s own docstring (cli.py:586-598) says this must not happen — "A tree-unreadable code would send the operator to preserve and investigate register evidence that was never opened" — but its guard only covers what `RunTree.__init__` raises, and the absent-run refusal is raised later, inside the projection.

**Scenario.** `verbatus review --run-root $S/state1 --run-id r1` (one directory level off) and `verbatus review --run-root $S/state1/runs --run-id nope` both print the damaged-evidence message with detail `IncompatibleReuse: no run.json under …: there is no run here to read`. By contrast `--run-id My-Run` is correctly classified as INVALID_COMMAND, which is the shape the missing-run case should also have.

**Proposed fix.** Detect the absent-run case explicitly (no `run.json` at the resolved path) before the projection's catch-all and raise INVALID_COMMAND with the path it looked in, reserving CONSOLE_TREE_UNREADABLE for a tree that exists and fails verification.


### F036 [medium] require_checkout validates the module's own directory, not the run's workspace

`operations/operator/entry.py:30` — operability — from END-TO-END EXECUTION — I ran the operato; verified by running: True

**Claim.** `entry.main` calls `require_checkout()` with its default argument, which is `common/checkout.py`'s own `REPOSITORY_ROOT` — the directory the code was imported from. But every run resolves its stage programs, config and proof material from `self.workspace`, which defaults to the current directory (`--workspace … defaults to the current directory`). So the check passes whenever the code is importable, and never sees the directory that matters. The module docstring in common/checkout.py says the whole point is that "an operator who has somehow started outside one is told that in one sentence, before a run begins, instead of meeting a missing-file error part way through" — which is exactly what happens instead. `verbatus` is an installed console script (pyproject.toml:34), so running it from the wrong folder is an ordinary mistake, not an exotic one.

**Scenario.** `cd /tmp/.../elsewhere && /home/user/Apparatus-Verbatus-Alpha/.venv/bin/verbatus --state-dir $S/state14 run --run-id fromelsewhere` prints "The declared fixture could not be read; naming pages and acts generically. Run started. Checking the declared pages." and then the opaque RUN_FAILED screen; the receipt holds `can't open file '…/elsewhere/pipeline/orchestrator/run.py': [Errno 2] No such file or directory`. The NOT_A_CHECKOUT copy written for this exact case (errors.py:340-346) is never shown.

**Proposed fix.** Call `require_checkout(workspace)` on the resolved `--workspace` in cli.main after argument parsing, and stop the run rather than continuing past "The declared fixture could not be read".


### F037 [medium] Two concurrent runs on the same run id both report "Run complete"

`common/runtree/store.py:1476` — correctness — from END-TO-END EXECUTION — I ran the operato; verified by running: True

**Claim.** `_run_creation_lock` serialises only the creation of `run.json`; nothing holds a lock for the duration of a run, and no writer marker is recorded. Two `verbatus run --run-id X` invocations therefore execute all nine stages concurrently over the same tree and both print "Run complete. Its pages and acts reached the Armarium record." With the deterministic fixture the content-addressed store absorbs the race, but with real chairs the two processes produce different artifacts for the same acts and whichever seals last defines the run, while the other still reports success. The surface already knows this hazard elsewhere — `launch` explicitly refuses a second in-flight window and says so — and review's own next-action text warns "Do not resume while a writer may still be active", but nothing enforces it. The double-click `Verbatus.command` flow the README promotes makes a second window the easy mistake.

**Scenario.** Two simultaneous `verbatus --state-dir $S/state10 run --run-id conc` processes: both exit 0, both print "Run complete", two separate run receipts are written, and `review` afterwards shows all nine stages sealed with no indication that two writers produced the tree.

**Proposed fix.** Take an exclusive advisory lock on the run directory for the life of the run (or write and check a writer lease in the tree), and refuse the second invocation with a named message the way `launch` refuses a second paid window.


### F038 [medium] An unaccounted file in a sealed stage is invisible to review and copied by backup

`operations/operator/backup.py:104` — silent-failure — from END-TO-END EXECUTION — I ran the operato; verified by running: True

**Claim.** `sync_run_tree` copies every regular file it walks, excluding only RunTree publication temporaries; a file no stage manifest accounts for is stored and counted as ordinary published evidence. `review` likewise derives stage state from the stage-seal artifact and never reconciles the manifest against what is on disk, so the stage still reads "sealed — completed and sealed". The same system's third transfer path already refuses exactly this: the operator README states that for `fetch-run`, "An object under the prefix that no stage of a run tree accounts for is refused by name." So the three ways evidence moves or is inspected disagree about whether an unaccounted object is admissible, and two of them propagate it silently. Mutating an existing artifact is caught loudly (the self-hash refusal names the file and produces a correct three-part message), so the gap is specifically addition, not alteration.

**Scenario.** Copy a completed tree, `touch 7_armarium/.manifest.json.tmp-abc123` and write `7_armarium/stray.txt`, then back it up: "Mac backup complete: 99 copied, 2 reused" (98 before), and the snapshot's `files` list contains `{"relative_path": "7_armarium/stray.txt", "sha256": "38cc8145…"}` while only the `.tmp-` name appears under `excluded_publication_temporaries`. `review` on the same tree prints "armarium: sealed — completed and sealed; 4 record(s)" and says nothing.

**Proposed fix.** Reconcile each stage directory against its manifest in both `sync_run_tree` and the review projection, and report an unaccounted file by name — refusing in backup as `fetch-run` does, or at minimum listing it as an unaccounted object in the snapshot and on the review screen.


### F039 [medium] The full gate cannot be green as root, which is how a pod container runs

`common/runtree/test_runtree_store.py:2322` — test-gap — from END-TO-END EXECUTION — I ran the operato; verified by running: True

**Claim.** `test_the_shared_snapshot_fails_loudly_on_a_descendant_it_cannot_read` asserts that `tree_snapshot` raises OSError over a directory it chmods to 0. Under uid 0 — this container, and the default in a RunPod container — mode bits do not deny root, the walk succeeds, and the test fails rather than skipping. The test immediately above it in the same file uses `pytest.skip("… the guarded path is unreachable here and this test proves nothing")` for precisely this kind of host-dependent unmeasurability, and operations/operator/conftest.py does the same for the Landlock gap, so the codebase's own convention was not applied here. The effect is that `sh .githooks/check-all.sh` can never exit 0 on the machine class the pipeline is meant to run on, which under hard rule 14 blocks the merge path and, during live testing, makes a red gate the normal state and therefore uninformative.

**Scenario.** `id -u` → 0. `NTFY_TOPIC=<the test-sink topic from conftest.py> PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 .venv/bin/python -m pytest -p xdist -n 4 --dist loadfile` over the whole tree: ~7,400 tests, one failure — `common/runtree/test_runtree_store.py:2335: Failed: DID NOT RAISE OSError`. Re-running that file alone reproduces it.

**Proposed fix.** Skip the test with a named host gap when `os.geteuid() == 0` (or drop privileges for the probe), matching the pattern used by the neighbouring test and by operations/operator/conftest.py.


### F040 [medium] The gate refuses this checkout's uv, and the version pin is duplicated in two files

`.githooks/check-all.sh:132` — config — from END-TO-END EXECUTION — I ran the operato; verified by running: True

**Claim.** `check-all.sh` hard-codes `uv 0.12.1` as a shell case pattern at line 132 and repeats the string in three recovery messages (lines 70, 128, 135); `.github/workflows/ci.yml:70` independently pins `python -m pip install uv==0.12.1`. Nothing declares the version once, and `.githooks/test_ci_workflow.py` asserts several other gate/CI correspondences but not this one, so a bump in either place silently diverges. On this checkout the installed uv is 0.8.17, so the gate refuses before running a single check. The refusal itself is exemplary — it names the observed version, the required version and the exact recovery command — but the combination means the local gate is unavailable on the machine the repository ships on, and the transfer story for a second machine includes an undeclared toolchain pin.

**Scenario.** `sh .githooks/check-all.sh --parallel` → "check-all: the frozen environment cannot be verified with uv 0.8.17; this gate requires uv 0.12.1", exit 1, zero checks executed. `uv --version` → `uv 0.8.17`.

**Proposed fix.** Declare the required uv version once (a `[tool.uv] required-version` pin or a single constant file), have check-all.sh and ci.yml both read it, and add a reconciliation assertion to .githooks/test_ci_workflow.py.


### F041 [low] The held-acts header counts records, not acts, contradicting the README

`operations/operator/review_text.py:448` — correctness — from END-TO-END EXECUTION — I ran the operato; verified by running: True

**Claim.** The renderer prints `Held or unresolved acts ({len(holds)})`, where `holds` is one row per hold record. One act held by the Designator and reviewed by the Recensor produces two rows, so the header reads 2 for one act — while the summary sentence directly above correctly says "1 act(s) are held or unresolved, listed below as 2 record(s)". operations/operator/README.md states the opposite of what the code does: "the count is of acts, and the sentence above says how many records they came from." The screen therefore overstates how many acts need a decision, on the screen used to make that decision.

**Scenario.** `review --run-root $S/state7/runs --run-id rref` (the `refused-page` scenario, one held act a2) prints "1 act(s) are held or unresolved, listed below as 2 record(s)" and then the header "Held or unresolved acts (2)", followed by two rows both naming act a2 (`act_adce1d27d6bf2d60`), one labelled [Designator hold] and one [Recensor review of that hold].

**Proposed fix.** Count distinct `act_id`s in the header (`len({row['act_id'] for row in holds})`), or change the header to say "records" and keep the act count in the sentence.


### F042 [low] No verb ever prints the run root that review and backup require

`operations/operator/surface.py:1225` — operability — from END-TO-END EXECUTION — I ran the operato; verified by running: True

**Claim.** `review`, `backup` and `advance` all require `--run-root`, but no verb ever prints it. `run` prints the receipt path and the export bundle path; `status` prints neither the run root nor the run id. The run root is `<state-dir>/runs`, and the state dir defaults to `$XDG_STATE_HOME/verbatus` or `~/.local/state/verbatus` with several documented exceptions — so after a run, an operator wanting to open it read-only must either know that layout or open the JSON receipt, which is the thing `status` exists to spare them.

**Scenario.** `run --run-id r1` prints "Run complete…" and "Saved run receipt: <path>/receipts/run-adee3c1b….json"; `status` prints "- run record 1: Run finished with recorded state: complete. Saved run state: complete." Neither output contains the string `runs/`, and the README's review example is `review --run-root <folder> --run-id <run>` with no hint what `<folder>` is.

**Proposed fix.** Print the ready-to-paste review command (run root and run id) at the end of every run and in each `status` run row.


### F043 [low] status renders every run record identically and never names the run

`operations/operator/surface.py:1544` — operability — from END-TO-END EXECUTION — I ran the operato; verified by running: True

**Claim.** `status` renders a run record as `- run record {n}: {summary}` plus `Saved run state: {state}`, dropping the `run_id` the payload carries. A state directory that has seen several runs, or one run that failed for two different reasons, produces a list of indistinguishable lines. This is the verb documented as "the one you can run any time" to find out "what is currently going on", and it is the verb every failure message routes to.

**Scenario.** After two failed attempts on run `rreal` (one missing placement tier, one roster mismatch), `verbatus --state-dir $S/state2 status` prints "- run record 1: Run ended before its Armarium record was available. Saved run state: failed." twice, verbatim.

**Proposed fix.** Include `run_id` (and, for run rows, the run root) in the rendered line.


### F044 [low] export exits 0 on a partial held run while run exits 2 for the same state

`operations/operator/surface.py:1284` — operability — from END-TO-END EXECUTION — I ran the operato; verified by running: True

**Claim.** `run` raises RUN_HELD (exit 2) when the aggregate is not `complete`, but `export` over the same run prints the partial reconciliation and the hold reasons and exits 0. Any script or wrapper that gates on `export`'s status — and the README's own suggested order ends with export and backup — treats a delivery of one act out of two as success. The screen is loud and honest; only the exit status is not.

**Scenario.** `run --run-id rref --scenario refused-page` exits 2. `export --run-id rref` on the same run prints "| Delivered acts | 1 |", "| Acts held for review | 1 |", "| Recorded run state | partial |" and four "Recorded reason:" lines, then exits 0.

**Proposed fix.** Exit non-zero (a distinct EXPORT_PARTIAL code) when the exported bundle claims anything other than complete, matching run's treatment of the same aggregate.


### F045 [low] The plain review view omits the model provenance the JSON projection carries

`operations/operator/review_text.py:460` — transferability — from END-TO-END EXECUTION — I ran the operato; verified by running: True

**Claim.** Each act row in the projection carries a full `provenance` block — chair, chair state, adapter revision, resolved identity (repo, revision, digest manifest, licence note), resolved revision and witness regime — but the plain-language renderer prints only the delivered text, doubts, review record, witnesses by chair name, and crops. An operator reading a run on the machine it was fetched to therefore cannot tell from the review screen which model produced a reading, which is the fact GOVERNANCE 6 says travels with every stored reading. It is recoverable with `--json` or by opening the named artifact, but the surface documented as "says in plain words … what each act's latest reading and review say" does not say it.

**Scenario.** `review --run-root $S/state1/runs --run-id r1` prints for a1: "witnesses: attestator_1 read, attestator_2 read, attestator_3 read". The same act in `--json` carries `provenance.resolved_identity.serving_recipe = "fake-perlector-v0"` and `resolved_revision.value = "f20a475b…"`. On a real-roster run this is the difference between knowing and not knowing which checkpoint read the ink.

**Proposed fix.** Print one provenance line per act (chair, resolved revision or digest-manifest prefix, witness regime) in the plain view.


### F046 [low] run continues after announcing it could not read the declared fixture

`operations/operator/surface.py:1067` — silent-failure — from END-TO-END EXECUTION — I ran the operato; verified by running: True

**Claim.** When `_declared_work` cannot read the fixture, `run` prints "The declared fixture could not be read; naming pages and acts generically" and then proceeds to launch the orchestrator anyway, which fails a moment later for a reason the operator never sees (see the RUN_FAILED finding). The message is honest about what it could not read but is presented as a naming inconvenience rather than as the precondition failure it is; the same condition is exactly what NOT_A_CHECKOUT was written for.

**Scenario.** Running the installed `verbatus` console script from a directory that is not a checkout prints the generic-naming warning, then "Run started. Checking the declared pages.", then the opaque RUN_FAILED screen whose receipt holds the real FileNotFoundError.

**Proposed fix.** Treat an unreadable declared fixture as a refusal before the orchestrator is launched, with the NOT_A_CHECKOUT or a fixture-unreadable code naming the path it looked in.


### F047 [low] export with no --run-id silently exports whichever run was recorded last

`operations/operator/surface.py:1270` — operability — from END-TO-END EXECUTION — I ran the operato; verified by running: True

**Claim.** `--run-id` is optional on `export`; omitted, the verb reads the descriptor's last `run` action and exports that run without naming it in advance. In a state directory holding several runs — which is the normal case, since one state dir serves all of them — an operator who omits the flag gets a bundle for whatever ran most recently, and only learns which from the bundle filename in the output. The README describes `--run-id` as "the explicitly recorded run to export", which reads as though it were required.

**Scenario.** `verbatus --state-dir $S/state1 export` (no `--run-id`) produced a full reconciliation and a new receipt for run `r1` without ever printing a confirmation of which run it had chosen before doing the work.

**Proposed fix.** Either require `--run-id`, or print the run id being exported as the first line, before the reconciliation table.


### F048 [info] pod_run --help refuses instead of printing usage

`operations/pod/pod_run.py:1` — operability — from END-TO-END EXECUTION — I ran the operato; verified by running: True

**Claim.** The pod's own run entry point refuses `--help` with "pod_run refused: pod_run needs the bootstrap_main argv after a literal --; a run with no bootstrap plan has nothing green to run after". The invocation is documented in operations/pod/README.md:353, but on a live pod the operator is at a shell on a rented machine that is billing, and the first thing anyone types at an unfamiliar command is `--help`. There is no way to see the flag list from the program itself.

**Scenario.** `.venv/bin/python -m operations.pod.pod_run --help` prints the refusal and nothing else; no usage line, no flag list.

**Proposed fix.** Handle `-h`/`--help` before the argv-shape check and print the parser's usage.


### F049 [critical] A pod that runs the pipeline cannot be created: pod_run's argv is refused

`operations/pod/models.py:456` — correctness — from THE FIRST REAL POD RUN — reading operati; verified by running: True

**Claim.** `_required_timer_arguments` refuses any `--bootstrap-command-json` whose decoded argv carries more than one `--report-path` value. `pod_run` — described in its own module docstring as "the one tracked entrypoint that runs the pipeline on a pod" — necessarily carries exactly two: its own `--report-path` (required by pod_run.build_parser, pod_run.py:270) and the bootstrap half's `--report-path` after the literal `--` (required by bootstrap_main.build_parser, bootstrap_main.py:393), and resolve_run_plan explicitly refuses them being the same path (pod_run.py:340). The nested-flag reader collects both `--flag value` and `--flag=value` spellings (models.py:365-380), so there is no spelling that passes. The check was added to close a gap in `_bind_report_path_to_launch`; it also makes Boot B — the first real pipeline run on a pod — unconstructible. No test composes a pod_run docker_start_cmd, which is why the suite is green.

**Scenario.** Build the Boot B request JSON the README's boot plan calls for: docker_start_cmd = ["python","-m","operations.pod.pod_timer","--timer-factory","operations.pod.provider_runpod:timer_context_from_environment","--bootstrap-command-json", json.dumps(["python","-m","operations.pod.pod_run","--report-path","/workspace/private/pod-run-report-TOKEN.json","--run-id",...,"--","--volume-mount-path","/workspace/private","--report-path","/workspace/private/bootstrap-report-TOKEN.json",...]),"--report-path","/workspace/private/pod-runtime-report-TOKEN.json"]. `cli._request` raises ValueError: "pod bootstrap command must carry at most one nested --report-path value" before any preview, lease or provider call. I ran exactly this and got that refusal. The only launchable shape left is `bootstrap_main --hold-only` (Boot A), which runs no pipeline.

**Proposed fix.** Make the nested check count only the outermost command's report path, or teach it the pod_run shape: split the nested argv at the literal `--` and allow at most one `--report-path` on each side, binding the launch token into both (rebind_nested_flag already rewrites every occurrence). Add a test that constructs a PodCreateRequest from a real pod_run argv so the Boot B shape is covered offline.


### F050 [critical] fetch-run refuses the whole run tree: stages write logs and a lock inside it

`operations/operator/surface.py:3162` — operability — from THE FIRST REAL POD RUN — reading operati; verified by running: True

**Claim.** `_fetch_run_tree` refuses the entire fetch — "no stage of a run tree accounts for an object at that path; nothing was fetched past it" — for any key under `runs/<id>/` outside `RunTree.inventory_scope()` (common/runtree/store.py:1462) that is not a `.tmp-` publication temporary. Three pipeline stages write two classes of object inside the run tree that the scope does not cover: the single-residency lock `pod-gpu.lock` at the tree root (structure_pass.py:463, 3_attestatores/run.py:4558, 4_perlector/run.py:1921) and the vLLM launch logs under `<stage>/serving-logs/` (structure_pass.py:462, 3_attestatores/run.py:4557, 4_perlector/run.py:1914). The scope covers only run.json, receipts/, run-health/, and per-stage artifacts/ blobs/ manifest.json index.json. The run whose evidence the first live test exists to produce cannot be brought home by the verb written for it, and the failure is all-or-nothing rather than partial.

**Scenario.** A real pod run serves any chair through ServingManager. `FileResidencyLease.acquire` creates `<volume>/runs/<id>/pod-gpu.lock` (residency.py:104-105) and `SubprocessLauncher.launch` creates `<volume>/runs/<id>/3_attestatores/serving-logs/vllm-attestator_1-<uuid>.log`. Afterwards `verbatus fetch-run --run-id <id> --into <local> --network-volume DC:VOL` lists those keys under the prefix and raises FetchRunRefusal on the first one; zero objects are fetched and the operator gets no run tree at all. I verified by running that both paths are outside inventory_scope() and are not publication temporaries.

**Proposed fix.** Either move both outside the run tree (serving logs beside the preflight evidence, the residency lock onto container-local disk — see the residency finding), or extend inventory_scope() with `<stage>/serving-logs/` and the lock name and teach fetch-run to fetch them as unverified side evidence. Either way, add a fetch-run test whose fixture tree contains what a served stage actually leaves behind.


### F051 [high] The pod dead-man needs RUNPOD_API_KEY; the create path forbids delivering it

`operations/pod/provider_runpod.py:1076` — correctness — from THE FIRST REAL POD RUN — reading operati; verified by running: True

**Claim.** `timer_context_from_environment` refuses to construct unless `RUNPOD_API_KEY` is in the pod's environment — without it there is no close capability at all, and pod_timer.main prints "nothing can close this pod" and exits 2 (pod_timer.py:276-277). The only environment the tracked launch path can set on a pod is `PodCreateRequest.metadata`, copied verbatim into the v1 create body's `env` (provider_runpod.py:1059), and `PodCreateRequest.__post_init__` refuses any metadata key that `looks_like_credential_field` matches (models.py:266) — which I verified returns True for RUNPOD_API_KEY, HF_TOKEN and HUGGING_FACE_HUB_TOKEN. Nothing in the tracked tree supplies the key by another route: `templateId` is passed through but no template is defined here, and the README only says the capability is "an ephemeral runtime environment value" without naming who puts it there. The same gap defeats bootstrap_main's own documented remedy for gated Hugging Face repositories (`--keep-env HF_TOKEN`, bootstrap_main.py:70-77): keeping a variable that was never set keeps nothing.

**Scenario.** Authorize Boot A, fill in the image digest and volume id, run `python -m operations.pod.cli ... create --request boot-a.json`. The pod is created and starts billing. Its primary process resolves the timer factory, finds no RUNPOD_API_KEY (assuming RunPod does not inject one for this account and image), writes a `bootstrap: unstarted` report naming the failure, and exits 2. The container is dead, the pod is EXITED and still exists, and the only thing that can close it is the laptop supervisor's next status tick — a launch that bought an image pull and no facts.

**Proposed fix.** Decide and write down the one route: confirm RunPod injects RUNPOD_API_KEY for this pod type (and make it a checklist item that must pass before Boot A), or create a RunPod template holding the key and require `template` on every real request, or add a narrow, explicit allowlist in PodCreateRequest for the two capability names the pod genuinely needs, with the value never logged, recorded in a fixture, or written to a receipt.


### F052 [high] Three 24 GiB rows still pass gpu_memory_utilization 0.58 the header says was retired

`config/serving_recipes_real.toml:363` — config — from THE FIRST REAL POD RUN — reading operati; verified by running: True

**Claim.** The file's header states that U15 retired 0.58 ("0.58 has no derivation anywhere in this tree or the old pipeline") and that "every other row's fraction is unmoved — 0.78 / 0.88 at the larger tiers", implying no row still carries it. Three do: attestator_2 (DAI) at line 363, attestator_3 (Churro) at line 456 and perlector at line 555, all `generic-24gb`, all `gpu_memory_utilization = "0.58"`. The same header's VRAM arithmetic says DAI needs 18.7 GiB of 24 — "0.78 minimum, 0.90 matches the old serve_dai.sh" — and config/pod_placement.toml raised that tier's engine_memory_fraction to 0.90 for exactly that reason. Nothing catches the contradiction because the placement check is a ceiling only: operations/serving/preflight.py:565 refuses a row above the tier fraction, never one below. The value at line 363 is what render_vllm_argv passes to `--gpu-memory-utilization` (manager.py:1728).

**Scenario.** Stage 1 rents the RTX A5000 (24 GiB — the only reviewed card under the configured max_hourly_usd of $0.40). PREFLIGHT reaches attestator_2 and launches vLLM with `--gpu-memory-utilization 0.58`, about 13.9 GiB for a model the file's own arithmetic sizes at 18.7 GiB. vLLM aborts during engine init (insufficient memory for the KV cache, or a plain allocation failure), the child exits, `_assert_process_live` raises VLLM_PROCESS_EXITED, PREFLIGHT is red, the bootstrap is red and the pod closes — after the ~10 GB wheel download and the model fetch have already been paid for.

**Proposed fix.** Reconcile the three rows with the header: set generic-24gb gpu_memory_utilization to the value the arithmetic supports (0.90 for DAI per its own note) or delete the rows and state the chair is not servable at that tier, as the header already does for the Perlector. Consider requiring a recorded derivation per fraction so a row that contradicts the file's reasoning fails a test rather than sitting under a comment.


### F053 [high] Single-resident GPU lease is not pod-wide, and the pipeline's lock sits on the volume

`operations/pod/bootstrap_main.py:389` — correctness — from THE FIRST REAL POD RUN — reading operati; verified by running: False

**Claim.** `PreflightSeams.residency_lock` is `/tmp/verbatus-pod-gpu.lock`, chosen with the explicit comment "on container-local disk, because an advisory lock on a network volume is not something the mount is known to honour". The three serving stages ignore that reasoning and put their lock inside the run tree — `context.tree.resolve("pod-gpu.lock")` (structure_pass.py:463, 3_attestatores/run.py:4558, 4_perlector/run.py:1921) — which on a pod resolves under `<volume>/runs/<id>/`, on the very network volume the comment distrusts. Two consequences: the preflight and the run never share a lock, so FileResidencyLease's stated contract ("Callers must choose a path scoped to one pod/GPU", residency.py:93-94) is not met; and if flock is advisory-only or unsupported over the RunPod mount, the stages' lock silently grants itself to everyone, which is the co-residency the single-resident rule exists to prevent.

**Scenario.** Two pipeline stages resumed concurrently under different run ids on one pod each acquire their own run-tree lock, both succeed, and two vLLM servers at 0.85 utilisation contend for one card — the second start fails on allocation, or both degrade unpredictably, with no named refusal. If the mount does not honour flock at all, even two stages sharing one run tree co-reside.

**Proposed fix.** Give every serving caller on a pod the same container-local lock path (a module constant in operations/serving/residency.py, not three call-site literals), and have the stages accept it from configuration rather than deriving it from the run tree. If a run-tree path is kept for local runs, prove the lock actually excludes on the volume during Boot A before relying on it.


### F054 [high] No container disk size is requested while the bootstrap downloads ~10 GB onto it

`operations/pod/provider_runpod.py:1048` — config — from THE FIRST REAL POD RUN — reading operati; verified by running: False

**Claim.** `_create_payload` sends name, cloudType, computeType, imageName, gpuTypeIds, gpuCount, interruptible, networkVolumeId, volumeMountPath, dockerStartCmd and env — nothing about container disk. The bootstrap then runs `uv sync --locked --group pod` with `UV_CACHE_DIR=/tmp/verbatus-uv-cache` (bootstrap.py:56), which the code's own comment sizes at "on the order of ten gigabytes of wheels" (bootstrap.py:620-622), and installs them again into `<repository>/.venv`, also on container-local disk (the repository cannot live on the volume: _require_contained forces the lockfile and configs inside it, and the volume paths are separately constrained). Two copies of a torch+CUDA stack is roughly 15-20 GB of container disk on a pod whose disk size the request never states, so it takes whatever the image or account default is.

**Scenario.** Boot B on a pod with the common 20 GB container-disk default. UV_ENVIRONMENT downloads the pod group, fills /tmp, and `uv sync` fails with ENOSPC. `SubprocessBootstrapActions._command` turns the non-zero exit into a BootstrapStepFailure, the journal goes red, pod_run returns EXIT_BOOTSTRAP_RED and the pod closes — after paying for a full wheel download that could never have fit.

**Proposed fix.** Add an explicit container-disk field to PodCreateRequest and the create payload (v1 containerDiskInGb), sized from a recorded measurement of the pod group's on-disk footprint, and refuse a request that does not name one. Failing that, have the bootstrap check free space on the container disk (not only on the volume — SystemGpuProbe measures disk_path=volume_mount_path, preflight.py:185) before starting the sync.


### F055 [high] The pod image contract is unwritten, and REPOSITORY assumes a clone nothing makes

`operations/pod/bootstrap.py:583` — transferability — from THE FIRST REAL POD RUN — reading operati; verified by running: False

**Claim.** `checkout_commit` runs `git fetch --no-tags origin <sha>` and `git checkout --detach --force <sha>` with cwd = `--repository`. It never clones, contradicting operations/README.md:29 ("The pod bootstrap clones the repository at a pinned commit"). The image must therefore already contain a clone with an `origin` remote and credentials for a private repository, reachable from the explicit BOOTSTRAP_ENVIRONMENT which supplies no HOME, no GIT_* variable and no credential helper. Four more unwritten requirements ride along: `python` on PATH resolving to an interpreter that can already import operations.pod.pod_timer and (for the bootstrap child) operations.serving.smoke, which imports PIL at module scope, before uv has run; a working directory that makes `python -m operations.pod.pod_timer` resolve (the dockerStartCmd at boot_a_request.py:191 sets none); git at exactly /usr/bin/git and uv at exactly /usr/local/bin/uv (bootstrap.py:37 — the default uv installer puts it in ~/.local/bin); and that interpreter being the same environment `uv sync` targets (`<repository>/.venv`), because ServingManager launches vLLM as `sys.executable -m vllm.entrypoints.cli.main` (manager.py:565) and verifies the pinned versions with importlib.metadata on this interpreter (InstalledPackages, manager.py:611). No Dockerfile, image spec, or image-contract section exists anywhere in the tree.

**Scenario.** A stock RunPod PyTorch image is used. The pod starts, `python -m operations.pod.pod_timer` fails with ModuleNotFoundError because there is no checkout on PYTHONPATH and no cwd was set; the primary process exits immediately; the pod is EXITED and billing until the laptop supervisor's status tick closes it. If the image does carry a checkout but `python` is the system interpreter rather than `<repository>/.venv/bin/python`, the run gets further and fails later and more expensively: `uv sync --group pod` populates `<repository>/.venv`, then PREFLIGHT's `_assert_runtime` asks importlib.metadata on the system python for vllm 0.27.1, does not find it, and raises RuntimePinError after the 10 GB download.

**Proposed fix.** Write the image contract into operations/pod/README.md and enforce what can be enforced before spending: have bootstrap_main refuse at plan time if --repository is not a git worktree with an origin remote, if the configured git/uv executables are absent, and if sys.executable is not inside `<repository>/.venv` (or the caller has not supplied a matching PackageInspector). Correct operations/README.md:29 to say 'fetches and checks out', since nothing clones.


### F056 [high] The 300-second arming bound must cover the image pull, and expiry closes the pod

`operations/pod/controller_armer.py:92` — operability — from THE FIRST REAL POD RUN — reading operati; verified by running: False

**Claim.** `CONTROLLER_ARMING_TIMEOUT_SECONDS = 300.0` is the whole window in which the pod must be scheduled, pull its image, start the container, construct the timer, write its first durable report to the mounted volume, and have that object become visible in the volume's S3 view (ChannelControllerArmer._poll, controller_armer.py:767-832). On expiry the attempt state is BOUND_EXPIRED, ControllerArming.armed is False, and launch._arm_or_close terminates the pod at once. The docstring is candid that 300 is "a bound, not a measurement" and that the S3 propagation delay is what Boot A exists to measure — but nothing in it accounts for the image pull, which for a CUDA/vLLM-capable image is commonly several gigabytes and several minutes on a cold host, and which begins only after create returns.

**Scenario.** Boot A on a fresh host with an uncached 15 GB image. create returns in seconds; the armer starts its supervisor and polls. Six minutes later the container finally starts and writes its report — a minute after the armer gave up, terminated the pod, and reported a non-green launch. The drill's four target facts (does the object appear, under which key, after how long, does the key hold delete and billing rights) are all unmeasured, and the next attempt costs another pull.

**Proposed fix.** Separate the two waits: a generous, separately recorded 'pod reached RUNNING' wait driven by provider.status() (which costs nothing and distinguishes 'still pulling' from 'started and silent'), then the 300-second channel bound measured from the moment the pod is RUNNING. Record both durations in the arming evidence so Boot A returns a number for each.


### F057 [high] startup_timeout_seconds is 300 for every real row, including a 51.7 GiB Perlector

`config/serving_recipes_real.toml:632` — config — from THE FIRST REAL POD RUN — reading operati; verified by running: False

**Claim.** Every vLLM row in the real catalogue carries `startup_timeout_seconds = 300` and `poll_interval_seconds = 2`. ServingManager._wait_until_ready (manager.py:986-1075) treats that as the whole budget from process launch to a successful health check, an exact /v1/models id match, and a completed inference probe; on expiry it raises VLLM_WATCHDOG_TIMEOUT, the smoke read is red, and the bootstrap is red. Five minutes must cover reading the weights off a network volume, capturing CUDA graphs (`enforce_eager = false` on every row) and warming the engine. The file's own header measures the Perlector's weights at 51.7 GiB of bf16; at a plausible 200-400 MB/s from a network volume the read alone is two to four minutes before a graph is captured. The same 300 is applied to a 3B Churro and a 27B Perlector without distinction.

**Scenario.** Boot B on an 80 GB+ card with the real roster. PREFLIGHT smokes chairs in sorted order; when it reaches `perlector` the server is still loading safetensors at t=300s. The watchdog fires, _attempt_cleanup kills the process, PREFLIGHT reports red with VLLM_WATCHDOG_TIMEOUT, the journal goes red, pod_run returns EXIT_BOOTSTRAP_RED and the pod closes — with the model fetch and the wheel install already paid for and no reading produced.

**Proposed fix.** Size startup_timeout_seconds per row from the weights' measured size and the volume's measured read rate (Boot A can measure the latter), and record the derivation in the row's comment the way the context and pixel budgets already are. Consider emitting a progress line from the launch tail so a long load is visibly a load rather than a hang.


### F058 [high] TRANSFER refuses a submission manifest on the volume with no transfer target

`operations/pod/bootstrap_main.py:895` — correctness — from THE FIRST REAL POD RUN — reading operati; verified by running: False

**Claim.** `_build_transfer`'s closure returns a vacuous success only when the submission manifest is absent; when the file exists and `--transfer-target-factory` is None it raises a BootstrapStepFailure — "submission manifest is present but no transfer target was configured". The bootstrap's `--submission-manifest` defaults to `<volume>/submission/manifest.json`, which is where `verbatus upload` puts a real submission and what pod_run.resolve_run_plan then requires to exist on the volume (pod_run.py:368-372). So the default configuration for a real pipeline run makes TRANSFER a refusal, and TRANSFER runs after UV_ENVIRONMENT in ORDERED_STEPS (bootstrap.py:63-69) — after the ~10 GB wheel download has been paid for. The step's purpose is also inverted on a pod: it re-uploads the submission the pod already has.

**Scenario.** Upload a submission to the volume, then launch a real run whose bootstrap argv does not name --transfer-target-factory (the Boot A template does not, and nothing in the tree renders a Boot B template). REPOSITORY and CONFIGURATION pass, UV_ENVIRONMENT downloads ten gigabytes, TRANSFER refuses by name, the journal goes red, the pod closes. The remedy — supply a factory so the pod uploads the submission back to the object store it came from — is work the run does not need.

**Proposed fix.** Make TRANSFER's direction explicit: default --submission-manifest to None for a pod that is consuming rather than producing a submission (refusing only when a target is configured without a manifest), or move TRANSFER after PREFLIGHT so a misconfiguration is found before the expensive steps. Either way, add a rendered Boot B request template beside boot_a_request.py so the flag set for a real run is generated by the code that validates it.


### F059 [medium] A crashed pod_run leaves its report saying 'running', with no liveness tick

`operations/pod/pod_run.py:638` — silent-failure — from THE FIRST REAL POD RUN — reading operati; verified by running: False

**Claim.** `main` writes the run report with `state: "running"` once, immediately before launching the orchestrator, and does not touch it again until the orchestrator returns. The liveness `hold()` loop that re-journals a tick every interval_seconds runs only after a complete or held run (pod_run.py:689). For the whole duration of the run — potentially hours — the only durable statement on the volume is a timestamped 'running' record with exit_code null and no heartbeat. If pod_run is SIGKILLed (OOM killer, container teardown at the hard deadline, a host fault) that record is the final state and says nothing about having stopped, which is the shape GOVERNANCE 2 forbids: a partial result that does not look partial.

**Scenario.** A four-hour lease; the orchestrator is killed by the OOM killer at t=90 minutes. The pod-side timer sees a non-zero child exit and closes the pod, but the run report on the volume still reads state: running, exit_code: null. An operator fetching the volume later sees a report claiming the run is in progress, with no way from that file to tell a crash from a run the pod was destroyed during. The timer's own report is the only counter-evidence, and it is under a token-derived name fetch-run cannot discover (surface.py:857-871).

**Proposed fix.** Have pod_run re-journal a liveness line beside the run report on the same interval while the orchestrator child is alive (the `hold` helper is already that shape), recording the child's pid and last-seen timestamp so a stale record reads as stale. Cross-reference the pod-runtime report's path in the run report so one fetched file names the other.


### F060 [medium] The affordable card cannot pass a real-roster preflight, which is all-or-nothing

`config/spend.toml:97` — governance — from THE FIRST REAL POD RUN — reading operati; verified by running: False

**Claim.** `max_hourly_usd = "0.40"` for pod plus attached volume admits exactly one reviewed card: the RTX A5000 at $0.27/h, 24 GiB (config/pod_placement.toml card_profile rows; the A40 is $0.44). `PreflightRunner.run` iterates every non-absent chair in the roster and requires a cache verification and a smoke read from each, turning any per-chair failure into a red report (operations/pod/preflight.py:847-925). config/models-real.toml configures five chairs including `perlector`, and config/serving_recipes_real.toml's own header records that the Perlector's weights measure 51.7 GiB — "only generic-80gb-plus can ever serve it", the smaller rows "kept for catalogue coverage, not as a claim that a card of that class could run them". Because preflight is all-or-nothing over the roster, a green real-roster bootstrap is unreachable on any card the current ceiling permits.

**Scenario.** Tyrel raises the spend gate for Stage 1, authorises an A5000, and launches with --models-config config/models-real.toml. Even with every row stamped proven and every other chair loading, the perlector smoke fails for want of VRAM, PREFLIGHT is red, the bootstrap is red, and the pod closes. The only outcomes reachable at $0.40/h are the hold-only drill and a fixture-roster boot.

**Proposed fix.** Decide explicitly what a partial roster means to preflight — a per-chair skip recorded as 'not servable at this tier' that does not redden the report, versus a roster narrowed for a small-card run — and say so in the README's boot plan, so Stage 1's card and Stage 1's roster are chosen together rather than discovered incompatible on a billing pod.


### F061 [medium] The pod-side close report races its own destruction, so a green one is unreachable

`operations/pod/pod_timer.py:214` — operability — from THE FIRST REAL POD RUN — reading operati; verified by running: False

**Claim.** At the hard deadline (or on a failed bootstrap child) the timer calls `_close_with_retries`, which issues DELETE against its own pod and then polls status and verify_absent until both report absence (shutdown.py:205-260), then writes the close record to the volume and returns 0 only if the capture verified. Every one of those steps runs inside the container the DELETE is destroying. In practice the process is killed between the DELETE and the report write, so the durable artefact left on the volume is the pre-close record (`green: false`, `close: null`) and the exit status nobody reads is never produced. Nothing in the runtime or the README warns a reader that this is the expected shape.

**Scenario.** Boot A closes at its 900-second deadline. An operator later fetches /workspace/private/pod-runtime-report-<token>.json and finds bootstrap: {state: running}, close: null, green: false — which reads like a timer that never closed anything, when the DELETE in fact succeeded and the pod is gone. Telling the two apart requires the laptop supervisor's own close record, which is on the laptop, not the volume.

**Proposed fix.** Write an 'about to terminate' record to the volume immediately before the DELETE, naming the reason and the requested cutoff, so the durable trail distinguishes 'never tried' from 'tried and was destroyed mid-verification'. Document in the README that the laptop-side close is the authoritative verified close and a truncated pod-side report is the normal outcome, not a fault.


### F062 [medium] Preflight evidence publishes by hard link onto a volume that may not support links

`operations/pod/durable.py:95` — correctness — from THE FIRST REAL POD RUN — reading operati; verified by running: False

**Claim.** `exclusive_write` publishes by `os.link(temporary, path)` and is called with strict=True by PodPreflightReceiptPublisher._write (bootstrap_main.py:359) for every serving receipt, launch audit and evidence manifest a preflight produces — all under `<volume>/preflight/<stem>/`. The run tree's immutable publication uses the same primitive. The README's live checklist asks the first boot to verify that the volume "supports the run tree's immutable hard-link publication", but the preflight publisher is a second, unlisted site that runs earlier than the run tree does, and its failure mode is a red PREFLIGHT carrying an OSError rather than a named refusal an operator can act on.

**Scenario.** The RunPod network volume's filesystem refuses link() (EPERM/EOPNOTSUPP, common on distributed and object-backed mounts). The first chair smoke completes, the manager calls receipt_publisher.publish, exclusive_write raises OSError, and it surfaces through ReceiptPublicationError as a serving start failure — so the preflight reports a serving problem for what is actually a filesystem capability problem, and the pod closes without the operator learning which.

**Proposed fix.** Probe link support once beside the existing write_probe (bootstrap_main.py:836) and refuse at startup with a named reason if hard links are unavailable on the mount — before any model is fetched. Name hard-link support explicitly in the Boot A checklist's volume row rather than only via the run-tree wording.


### F063 [medium] The volume's hourly price in the ceiling and every close report is never observed

`operations/pod/provider_runpod.py:574` — governance — from THE FIRST REAL POD RUN — reading operati; verified by running: False

**Claim.** `RunPodProvider.estimate` takes the volume rate from the injected `volume_price` callable, and `_record` does the same when building a PodRecord from a real create or adopt response (provider_runpod.py:1087-1092) — the pod's own costPerHr is read from the provider, the volume's is not. That callable comes from the untracked --provider-factory. The combined figure is what assess_spend tests against max_hourly_usd and max_estimated_metered_cost_usd (spend.py:452-476), and the same number is printed on every close as "Its recorded ongoing price is $X per hour" (operations/operator/volume_cost.py:59). GOVERNANCE 10 asks that a claim be made only about what was measured; this one is an echo of a constant the launcher was handed, presented beside a genuinely observed pod rate.

**Scenario.** The provider factory returns $0.02/h for a 1 TB volume whose real charge is nearer $0.10/h. A launch at $0.27/h pod passes the $0.40 combined ceiling comfortably when the true combined rate is $0.37, and after close the operator is told the retained volume costs $0.02/h. Nothing in the record marks either figure as unobserved.

**Proposed fix.** Label the volume rate's provenance in the estimate and the close report the way AccountBalanceObservation.source already labels the balance ('supplied at launch, not observed from the provider'), and have volume_cost_lines say so before it quotes the figure. If RunPod exposes a network-volume price or size anywhere readable, observe it and record the comparison on the first live run.


### F064 [low] SystemGpuProbe reads only the first nvidia-smi line

`operations/pod/preflight.py:199` — correctness — from THE FIRST REAL POD RUN — reading operati; verified by running: False

**Claim.** `profile()` runs nvidia-smi with --query-gpu over four fields, which prints one line per visible GPU, and unconditionally takes `splitlines()[0]`. The measured VRAM is therefore one card's, and that is what PlacementTable.choose uses to select the tier and what the preflight report records as the machine's profile. `_create_payload` currently hard-codes gpuCount: 1 (provider_runpod.py:1054), so this is latent rather than active — but nothing ties the two together, and render_vllm_argv passes no --tensor-parallel-size, so a multi-GPU pod would silently use GPU 0 while the report described a single card.

**Scenario.** A future request asks for two cards (or the provider supplies a two-GPU machine and the runtime-contract check passes, since it compares machine.gpuTypeId and not a count). nvidia-smi prints two lines; the probe records 48 GiB on a 96 GiB machine, places the run in generic-48gb, and vLLM at --gpu-memory-utilization 0.78 uses one card while the second sits idle and billed.

**Proposed fix.** Parse every line, refuse (or record explicitly) when the visible cards are not identical, and carry the count on GpuProfile so the placement decision and the receipt both say how many cards were measured. Tie that count to the create request's gpuCount rather than leaving the two independent.


### F065 [low] No ports and no datacenter are requested, so a failed first pod cannot be inspected

`operations/pod/provider_runpod.py:1049` — operability — from THE FIRST REAL POD RUN — reading operati; verified by running: False

**Claim.** The v1 create body names no exposed ports and no datacenter. Without ports there is no SSH or web terminal into the pod, so every fact about a failed boot must arrive through the volume — and the volume is exactly what several of the predicted failures (an unmounted mount, a refused write, an unsupported hard link) prevent from being written. Without a datacenter constraint the pod's placement is left to the provider while the network volume is pinned to one region, so an incompatible pairing (the requested GPU type unavailable where the volume lives) surfaces as an opaque create failure rather than a named refusal.

**Scenario.** Boot A's container exits before the timer writes anything (missing RUNPOD_API_KEY, a missing checkout, a bad module path). The volume holds no report, the console shows EXITED, and the operator has no route into the machine and no log — the drill's four target facts are all unmeasured and there is nothing to diagnose from.

**Proposed fix.** For the first boots only, request an SSH port so a failed boot can be inspected, and state in the request template that this is a deliberate, temporary exception. Send the network volume's datacenter explicitly on create so a mismatch is refused by name rather than as an opaque HTTP error.


### F066 [low] pod_run pays the rest of the lease as idle GPU time after a complete run

`operations/pod/pod_run.py:155` — governance — from THE FIRST REAL POD RUN — reading operati; verified by running: False

**Claim.** `_HOLD_AFTER_EXITS` holds to the hard deadline after a complete or held run, because pod_timer.run_with_bootstrap reads any earlier child exit — exit 0 included — as `completed-early` and closes with a non-green timer report. The reasoning is written out honestly at pod_run.py:142-155 and the alternative is called a pod_timer contract change this unit does not make. The cost is real: with hard_lifetime_seconds = 14400, a twenty-minute first run bills roughly three hours and forty minutes of an idle card to avoid a non-green record.

**Scenario.** A first small real trial on the A5000 finishes its two-page submission in twenty minutes. The pod holds until t=4h, re-journaling a liveness line every fifteen seconds, and the run costs about $1.00 instead of about $0.09 — GOVERNANCE 9's 'small, reasonable test' costing eleven times what it needed to.

**Proposed fix.** Give the timer a way to distinguish 'the bootstrap child finished its declared work' from 'the bootstrap child died' — a completion marker the child writes to the report path that run_with_bootstrap reads before deciding — so a clean finish closes the pod instead of holding. Until then, set the hard lifetime per run from the expected work rather than at the policy ceiling.


### F067 [low] Records the pod writes outside runs/ and preflight/ have no route home

`operations/operator/surface.py:137` — operability — from THE FIRST REAL POD RUN — reading operati; verified by running: False

**Claim.** `fetch_run` fetches `runs/<id>/` and `preflight/`, plus whatever exact keys the operator names with --evidence-key. The docstring is candid that the launch-token-named bootstrap report, run report and journal cannot be derived. Not mentioned at all are `<volume>/pod-transfer-journal.json` (bootstrap_main.py:~910), the only durable record of which submission rows were verified against target-observed bytes, and the `-hold` liveness report pod_run writes beside its run report (pod_run.py:211). Neither is under either prefix, and neither is named anywhere as something to pass to --evidence-key.

**Scenario.** After the first live run the operator runs fetch-run with the two report keys the README's checklist prompts for. The transfer journal and the post-run liveness record stay on the volume; when the volume is later released under the retention decision both are gone, and the question 'did the transfer actually verify every row' has no surviving answer.

**Proposed fix.** Either place every launch-scoped record under one derivable prefix (for example `<volume>/launches/<token>/`) so one prefix brings the whole launch home, or list the complete set of volume paths a launch writes in operations/pod/README.md so the --evidence-key list can be assembled from a document rather than from memory.


### F068 [critical] Real-ingress recovery request kills the run; no export can ever be produced

`pipeline/2_designator/run.py:3358` — correctness — from Run-tree contracts, seals, and stage bou; verified by running: False

**Claim.** On a real submission the Recensor can and will publish a `fallback-recrop` recovery-request whenever a witness observation points at ink the Ink Map confirms outside the crop union (pipeline/5_recensor/run.py:3767-3877; `declared_recovery` at 3512 says outright that on a real submission the only producer of a request is measured outside ink). The orchestrator then dispatches the Designator with `--operation recover` (pipeline/orchestrator/run.py:588-596), and the Designator refuses every real-ingress recovery by name at 2_designator/run.py:3358-3367 ("bounded recovery from a real submission is not built"), exiting 2, which `invoke` turns into a ContractError and an orchestrator exit 2. There is no fallback: the Archetypus leaves the act unresolved (EXIT_HELD) and the Armarium raises FatalAccounting on an outstanding recovery request (7_armarium/run.py:1456-1460), so a real run in this state can never reach an export by any sequence of stage invocations. The refusal is loud, but GOVERNANCE 2/11 want a bounded loop that ends in a review item, not a run that cannot be closed out; on real material this is likely to fire on the first page where a Chandra/Churro box extends past the Designator's crop.

**Scenario.** Real submission, live chairs; on page 7 one witness reports an unclaimed box that the Ink Map confirms as ink outside every cut region -> Recensor publishes recovery-request + review `recovery-requested` (exit 3) -> orchestrator recovery step invokes Designator recover -> `ContractError: bounded recovery from a real submission is not built`, exit 2 -> orchestrator exits 2 with `pipeline/2_designator/run.py exited 2`. Running `--from archetypus --to armarium` afterwards: Archetypus exits 3, Armarium exits 2 (FatalAccounting on the outstanding request). Hours of pod time produce a tree with no export and no route to one.

**Proposed fix.** Until real recovery is built, do not publish a request the pipeline cannot answer on real ingress: gate the observation-origin request on `not is_real_ingress(context.run)` (or on a sealed policy flag) and publish `held-for-review` with the ink observation as the reason instead, so the act ends as a visible review item and the Armarium can still export partial. Alternatively make the Designator's real recrop real (it needs geometry from the request, not the fixture). Add an orchestrator acceptance test that runs a real-ingress submission through a Recensor that requests recovery and asserts the run ends partial-with-export rather than exit 2.


### F069 [high] Live Designator pass cannot be resumed: republished answers carry a new receipt

`pipeline/2_designator/run.py:2983` — correctness — from Run-tree contracts, seals, and stage bou; verified by running: False

**Claim.** `live_initial_pass` asks the served structure chair for every sealed page (2983-3000) and only then publishes one `structure-answer` per page (3004-3010). Nothing checks for an already-published answer, and every answer's payload embeds `provenance.receipt_ref` (structure_pass.py:416, 1012), the content address of this serving session's receipt, which changes on every chair start. The Attestatores (`sealed_pairs`, run.py:1803-1892) and Perlector (`_reading_already_sealed`, run.py:2075) both guard against exactly this; the Designator does not. So the orchestrator's documented resume path (`Resume. ... Every stage republishes what it already published`, orchestrator/run.py:24-28) is dead for a real run the moment the Designator has published: the re-run re-asks the chair for every page, builds different bytes under the same artifact identity, and `_publish_bytes` refuses. Also, because publication is deferred until all pages are answered, a crash after 900 of 1000 model calls leaves nothing on disk and the whole pass is repeated (GOVERNANCE 2's partial-visibility intent, and paid GPU time).

**Scenario.** Real run, live structure chair, Designator completes and seals; pod restarts during the Attestatores; operator resumes with `--all` (or with `--from designator`). Designator re-runs, starts the chair (new receipt digest), answers page 1 again, `context.publish(kind='structure-answer', ...)` -> `IncompatibleReuse: 2_designator/artifacts/structure-answer/art_....json already holds different bytes` -> exit 2 -> orchestrator exit 2. The only workaround is knowing to use `--from attestatores`, which nothing tells the operator.

**Proposed fix.** Before asking the chair for a page, check `context.tree.has_artifact(DESIGNATOR, STRUCTURE_ANSWER_KIND, artifact_id(...))` and reuse the sealed answer (as the Perlector does), and publish each page's answer as soon as it arrives instead of after the whole loop. Add a resumed-live-Designator test alongside the Attestatores/Perlector resume tests.


### F070 [medium] Seals witness only config/register digests; an edited run.json passes every verifier

`common/stage.py:1125` — correctness — from Run-tree contracts, seals, and stage bou; verified by running: True

**Claim.** `_stage_seal_payload` binds `config_digest` and `register_digest` and nothing else from the run authority, and `read_run` (store.py:336) accepts any run.json whose self-hash recomputes. The self-hash is computable by anyone, so editing `source_manifest`, `witness_chairs`, `adapter_recipes`, `render_settings` or `corpus_frame_membership` and recomputing `self_hash` leaves a tree that every predecessor-seal check, the final-seal check and every manifest walk accept, with the export still reporting `delivered`. Only a *re-run* of a stage whose open recomputes bindings notices, and only for the fields it compares: `--stage archetypus` and `--stage perlector` exit 0 over a run.json with a third source page added.

**Scenario.** Probe (verified): copy of the finished happy run; run.json gets a third source page and a fourth witness chair, self_hash recomputed. verify_predecessor_seal for all 9 stages, verify_final_seal and build_manifest for all stages: 19/19 ACCEPTED, final `delivered expected_acts=2`. With only source_manifest+membership edited: `--stage archetypus` exit 0, `--stage perlector` exit 0; Armarium/Designator/Exemplar re-runs refuse. A run tree restored from a partial backup or hand-edited run.json is therefore verifiable-looking but describes a different run.

**Proposed fix.** Bind the run authority's own self_hash (or digest_of(run.json bytes)) into every stage-seal payload and check it in `_verify_stage_seal`, so an edited authority invalidates every seal at once.


### F071 [medium] No seal chain across stages: upstream append after export leaves 'delivered' unrefused

`common/stage.py:1430` — correctness — from Run-tree contracts, seals, and stage bou; verified by running: True

**Claim.** `_verify_stage_seal` proves the producer's *latest* seal matches disk now; no consumer records which predecessor seal ordinal or digest it read, and `_stage_seal_payload` carries no predecessor binding. Appending one valid artifact to the Designator after the Armarium exported and honestly re-sealing (seal ordinal 2) yields a tree where every verifier accepts and the export still says delivered over the old denominator. The immutable artifacts prevent overwriting, but the tree cannot tell a reader that stages 3-7 were computed against an inventory the Designator no longer has.

**Scenario.** Probe (verified): finished happy run; a `structure-status` artifact is published under the Designator via StageContext and `seal_boundary()` writes seal 2. verify_predecessor_seal (all stages), verify_final_seal, build_manifest: 19/19 ACCEPTED, export `delivered expected_acts=2`. In practice this is reachable through a manual `--stage designator` reread or any future append-only operation on an upstream stage.

**Proposed fix.** Record the consumed predecessor stage-seal artifact id and sha256 in each stage's seal payload (or in the first artifact the stage publishes) and have `_verify_stage_seal`/`verify_final_seal` walk the chain back to the Door, refusing when a producer's latest seal is not the one its consumer cites.


### F072 [medium] A hard-link backup of the tree (cp -al, rsync --link-dest) makes the ORIGINAL unverifiable

`common/stage.py:1372` — transferability — from Run-tree contracts, seals, and stage bou; verified by running: True

**Claim.** `_digest_regular_file_at` refuses any blob whose `st_nlink` exceeds 1 plus the publisher's own temporaries. A hard link is a property of the inode, not of the run tree, so taking a hard-link snapshot of a finished run (the cheapest and most common backup pattern on Linux: `cp -al`, `rsync --link-dest`, Btrfs/ZFS-style dedup tools, some sync clients) raises the link count of every blob in the original and every boundary that has blobs refuses with a message accusing the evidence ("reachable under a name this store did not publish it under"). The operator backup verb (operations/operator/backup.py:808) links only its own temporaries into its own object store and is safe; the hazard is any generic tool.

**Scenario.** Probe (verified): `cp -al runs/happy1 backup` then verify the original: exemplar, ink-map, attestatores, perlector, recensor and the final Armarium boundary all REFUSED (`door blob '...' is not one contained regular file: it is reachable under a name this store did not publish it under`). Deleting the backup restores nlink=1 and the tree verifies again; the operator has no way to know that.

**Proposed fix.** Either drop the nlink rule (the digest-equals-name check already proves content, and the no-follow open plus inode identity checks cover the replacement race) or make the refusal name the actual cause and remedy ("blob has N links; a hard-link copy of this tree exists; break the links with cp -a"). Document in operations/pod/README that run trees must be copied, never hard-linked.


### F073 [medium] Crash between decode-environment and stage-seal makes the stage unsealable elsewhere

`common/stage.py:760` — correctness — from Run-tree contracts, seals, and stage bou; verified by running: True

**Claim.** `seal_boundary` publishes `decode-environment` for attempt N and then `stage-seal` N (760-778). If the process dies between the two, the next `seal_boundary` recomputes ordinal N, republishes decode-environment N, and `_publish_bytes` refuses if the bytes differ. The decode-environment payload embeds `platform.system()`, `platform.machine()` and five library versions (1049-1090), so a resume on a different machine, a different container image, or after a `pip install -U pillow` can never seal that stage: every attempt hits IncompatibleReuse on an artifact the operator has no legitimate way to remove.

**Scenario.** Probe (verified): Designator context with one new artifact; `_decode_environment` patched to report machine x86_64; `publish` made to raise on kind=stage-seal (simulated SIGKILL) after decode-environment 2 landed. Resume with machine reported as arm64: `IncompatibleReuse: 2_designator/artifacts/decode-environment/art_1fef80eed4dfad59.json already holds different bytes`. Resume with the same environment seals fine. A pod that dies at that instant and is replaced by a different image cannot continue the run.

**Proposed fix.** On resume, if decode-environment N exists without stage-seal N, treat the orphan as the interrupted attempt: either reuse its bytes for the seal (the seal witnesses `decode_environment_sha256` of whatever is on disk) or mint attempt N+1 for the pair, leaving the orphan visible. At minimum make the refusal name the crash window and the remedy.


### F074 [medium] Every stage resume re-walks and re-hashes the whole tree dozens of times; superlinear in acts

`common/stage.py:985` — performance — from Run-tree contracts, seals, and stage bou; verified by running: True

**Claim.** `_stage_records`, `expected_acts` (2703), `current_recovery_request` (4642), `exemplar_page_ids` (4462), `_refuse_deleted_seal`, `tally_hard_failures` and the per-act helpers in the Recensor each call `tree.build_manifest`, and `build_manifest` validates every artifact of the stage and, with `verify_inputs=True`, re-reads and re-hashes every input blob of every artifact (store.py:754-800, 1329-1341). Because several of these are called inside per-act loops, the number of full walks grows with the act count and each walk's cost grows with the artifact count and page bytes. Measured on the 2-page, 2-act fixture (75 artifacts, 0.93 MB of blobs): a Recensor resume performs 104 `build_manifest` walks and reads 27.7 MB of blob bytes and 4.4 MB of artifact bytes; Attestatores 39 walks / 8.3 MB; Perlector 33 walks / 11.3 MB. On a 1000-page shard with ~10 acts per page and multi-megabyte page images, the Designator's regions alone reference the page blob once per act, so a single input-verifying walk hashes each page ~10 times, and the Recensor performs that walk per act: hours of pure hashing per stage and per orchestrator checkpoint, before any model runs.

**Scenario.** Recensor over the finished review fixture: `build_manifest` called 104 times, read_bytes blob=1415 calls/27,719,330 bytes, artifact=874 calls/4,354,079 bytes for a tree whose blobs total 930,760 bytes (measure_stage.py). Scale by 500x pages and 10x acts and the per-stage walk cost is the dominant term of a live run; the `checkpoint` in the orchestrator adds five more input-verifying walks after every stage and every recovery round.

**Proposed fix.** Cache one verified manifest per stage per process (StageContext-held, invalidated on publish), verify each input path's bytes once per process rather than once per referencing artifact, and pass the cached manifest through `_stage_records`/`expected_acts`. Add a measured budget test that fails if a stage resume walks a stage more than a small constant number of times.


### F075 [low] Seal-deletion trigger is the rewritable manifest; a rollback passes at the producer

`common/stage.py:1000` — correctness — from Run-tree contracts, seals, and stage bou; verified by running: True

**Claim.** `_refuse_deleted_seal` establishes which seals existed from `manifest.json`, which is derived and rewritable, and returns silently when the file is absent (1015-1017). Removing a stage's latest seal, its decode-environment, every artifact and blob that seal witnessed, and the manifest leaves seals 1..N-1 contiguous and seal N-1 matching disk, so the producer's own boundary verifies as if the later pass never happened. Downstream stages catch it only through dangling input references.

**Scenario.** Probe (verified): review run, Designator seal 2 + its decode-environment + the recovery region and its crop blob + 2_designator/manifest.json removed. `verify_predecessor_seal(tree, 'attestatores')` (the Designator boundary) ACCEPTED; refusals appear only at perlector/recensor/archetypus via `artifact input '2_designator/blobs/sha256/7669...' could not be read`. A stage with no dependent downstream artifacts (a trailing Armarium, or a Designator re-seal nothing has consumed yet) rolls back silently.

**Proposed fix.** Give the deletion trigger an immutable anchor: have each seal N record seal N-1's artifact id and digest, and have consumers record the seal they consumed (see the chain finding), so a missing later seal is detectable from immutable records rather than from manifest.json.


### F076 [low] Non-.json files inside an artifact kind directory are invisible to the seal

`common/runtree/store.py:934` — silent-failure — from Run-tree contracts, seals, and stage bou; verified by running: True

**Claim.** `_walk_artifact_json` yields only names ending in `.json` below the kind level; any other regular file (a `.json.bak`, an editor swap file, a leftover `.art_x.json.tmp-...` temporary, an operator's notes) is skipped without a word, whereas the blob directory refuses every unrecognised name (`_stage_blob_inventory`, stage.py:1240-1256). The artifact inventory the seal digests therefore does not describe the directory's contents, and a stale copy of an artifact under a different suffix sits beside the evidence unaccounted for.

**Scenario.** Probe (verified): `notes.txt`, `.art_deadbeef.json.tmp-abc` and `art_0000000000000000.json.bak` placed in 4_perlector/artifacts/perlectio: all 19 verifier checks ACCEPTED, no warning.

**Proposed fix.** Refuse (or at least report on stderr) any regular file below the kind level that is not a `.json` artifact or a recognised publisher temporary, matching the blob directory's rule.


### F077 [low] An interrupted second pass and evidence tampering produce the same refusal sentence

`common/stage.py:1505` — operability — from Run-tree contracts, seals, and stage bou; verified by running: True

**Claim.** When a stage that already sealed is re-entered and dies after publishing one artifact but before its new seal, the next consumer refuses with exactly the text used for altered evidence: `<consumer> refuses <producer> stage-seal: its named inventory no longer matches disk`. The operator cannot tell "re-run the producer to finish its pass" from "someone changed the tree", which are opposite actions.

**Scenario.** Probe (verified): a testimonium published under the Attestatores after its seal 1 (simulating an interrupted reread pass); `verify_predecessor_seal(tree, 'perlector')` -> `perlector refuses attestatores stage-seal: its named inventory no longer matches disk`, with no mention that the extra artifacts are newer than the seal or that a producer re-run would resolve it.

**Proposed fix.** In `_verify_stage_seal`, when the recomputed inventory is a strict superset of the sealed one, say so ("N artifact(s) published after seal K; the producer's pass did not seal; re-run <stage>") and reserve the current sentence for a subset or content change.


### F078 [low] Test suite is not root-hermetic: a store test fails when the gate runs as root

`common/runtree/test_runtree_store.py:2322` — test-gap — from Run-tree contracts, seals, and stage bou; verified by running: True

**Claim.** `test_the_shared_snapshot_fails_loudly_on_a_descendant_it_cannot_read` relies on `chmod 0` making a directory unreadable; root ignores mode bits, so the expected OSError never arises and the test fails. `.githooks/check-all.sh` run on a pod (root) or in this container is therefore red for an environmental reason, which either blocks a merge or teaches the operator to ignore a red gate.

**Scenario.** Ran `pytest common/runtree/test_runtree_store.py` as uid 0 here: `FAILED ... DID NOT RAISE OSError`; every other test in the file passes.

**Proposed fix.** Skip the test when `os.geteuid() == 0` (or simulate the unreadable descendant by monkeypatching `os.scandir` to raise PermissionError) so the gate is green on root pods.


### F079 [low] Export member order is lexicographic on act_key, so block 10 precedes block 2

`pipeline/7_armarium/armarium_export.py:2399` — correctness — from Run-tree contracts, seals, and stage bou; verified by running: False

**Claim.** Real-ingress act keys are `proposal:<page>:<block>` with integer block ordinals (structure_pass.py:1284); the text bundle (2399), the JSONL/act records (2796, 2876) and the export payload's `delivered`/`non_delivered` lists (7_armarium/run.py:2031-2037) sort by the key string, so once a page has more than ten blocks the emitted order is `:1, :10, :11, :2, ...`, not reading order. The established texts are correct; their sequence in every deliverable is not the page's.

**Scenario.** A real page with 12 detected blocks; the text bundle for that page lists `proposal:7:1`, `proposal:7:10`, `proposal:7:11`, `proposal:7:2`, ... A reviewer reading the bundle against the page sees acts out of order and may read it as a missed act.

**Proposed fix.** Sort by `(page_ordinal, block_ordinal)` parsed from the key (or carried as integers on the act row) everywhere the export orders acts, and pin it with a >10-block fixture.


### F080 [info] Decode-environment differences after transfer are only a stderr note that reads like a fault

`common/stage.py:1511` — operability — from Run-tree contracts, seals, and stage bou; verified by running: False

**Claim.** After a run tree moves from a pod (Linux/x86_64, one Pillow build) to a workstation (Darwin/arm64), every stage boundary prints `decode environment differs by name from <producer>: ['platform', 'machine', ...]` and continues. This is the documented posture ("Unit 17 decides"), but on a transferred tree it fires at all eight boundaries, indistinguishable from a real decoder regression, and nothing records the observation in the tree itself.

**Scenario.** Copy a finished pod run to a Mac and run `--stage armarium`: eight warnings on stderr, exit 0; nothing in the tree records that the consumer's decoders differed.

**Proposed fix.** Distinguish platform/machine fields (expected to differ after transfer) from decoder-version fields in the message, and consider publishing the consumer's observation as a run receipt so the record travels with the tree.


### F081 [info] Every published file is mode 0600 via mkstemp; the tree is owner-only readable after transfer

`common/runtree/store.py:1653` — transferability — from Run-tree contracts, seals, and stage bou; verified by running: True

**Claim.** `_write_temporary` uses `tempfile.mkstemp`, which creates files 0600, and the hard link/replace preserves that mode, so every artifact, blob, manifest and run.json is readable only by the creating user. A tree written as root on a pod and copied with ownership preserved is unreadable to the reviewing user until chmod'd; a second local user (or a review tool running under another account) cannot read the evidence.

**Scenario.** `stat -c %a` on the fixture tree: every blob and artifact is 600. `rsync -a` from a root pod to a workstation account keeps 600/root; `RunTree.read_run` then fails with PermissionError for the operator.

**Proposed fix.** Apply the process umask after mkstemp (os.fchmod(fd, 0o666 & ~umask)) or document the copy command that resets modes.


### F082 [critical] Truncation length heuristic is calibrated to fixture pages and holds real-scale acts

`pipeline/4_perlector/truncation.py:70` — correctness — from Governance and ARCHITECTURE invariants i; verified by running: True

**Claim.** MIN_PIXELS_PER_CHARACTER = 2000 is a module constant whose own comment says it was "set high enough that this repository's tiny synthetic fixture pages never trip it by accident of scale". The scaling runs the wrong way: a real photographed leaf has far MORE pixels per character than a 200x260 fixture page, so raising the constant to clear the fixture makes it trip on real ink rather than clear it. is_length_suspicious returns True whenever region_pixels/len(text) > 2000, and classify's vote requires all three computed signals clean for `complete`; one suspicious signal downgrades an engine-`stop` reading to `unknown`, holds_as_failure(`unknown`) is True, _resolve_outcome returns "truncated", and pipeline/6_archetypus/run.py:643 refuses to establish any Perlectio whose outcome is not "read". A perfectly complete reading of a normal register act therefore ends held-for-review. The identical fixture-scale artefact was already found and repaired once in this repository for the residual-ink gate (common/residual_ink.py:154-162, "a flat count is 384 basis points of this repository's 200x260 fixture page and 1.6 of a 12.6-megapixel leaf"); this constant was not given the same treatment and is still an absolute ratio rather than a fraction of anything the page itself supplies.

**Scenario.** Verified by running the shipped module against realistic geometry: an act crop of 2400x420 px (a normal entry band on a 300-DPI leaf) carrying a complete 380-character reading gives 2653 px/char; is_length_suspicious -> True; classify(text, region_pixels=1008000, stop_reason="stop") -> {"classification": "unknown"}; holds_as_failure -> True. 2400x900 with 700 characters gives 3086 px/char and the same result. Only crops at or below roughly 1200x300 px stay under the threshold. On the first live run this holds most or all acts, produces an export with status `partial` and no delivered text, and costs a pod-hour to learn nothing.

**Proposed fix.** Make the length signal a fraction of the crop's own measured ink or of the page's resolution rather than an absolute pixel-per-character ratio (the shape common/residual_ink.py already moved to), and move the resolved value into a sealed config table so the run's config_digest records which instrument measured it. Add a test that exercises truncation.classify over a photographed-scale crop, as common/test_designator_recensor_ink_calibration.py already does for the ink thresholds.


### F083 [critical] Bounded recovery on a real submission aborts the run and cannot be re-run past

`pipeline/2_designator/run.py:3364` — operability — from Governance and ARCHITECTURE invariants i; verified by running: False

**Claim.** The Designator refuses `--operation recover` outright on real ingress ("bounded recovery from a real submission is not built; a recovery still reads the fixture's declared rectangle"). The Recensor, however, can and does publish a fallback-recrop request on a real submission: pipeline/5_recensor/run.py:3758-3761 sets wants_recovery from `bool(outside_ink_requests)`, and unclaimed_ink_observations (same file, line 2016) is a live measurement over the sealed Ink Map with no fixture dependency; the COVERAGE_OBSERVATION_ORIGIN branch at line 3796 is explicitly the real-submission producer. The orchestrator then dispatches the Designator, which exits EXIT_FATAL (2); pipeline/orchestrator/run.py's invoke() raises ContractError for any code outside (0, 3, 4), so run_stage converts it to EXIT_FATAL and the run ends with no Archetypus and no Armarium export. Because the recovery-request artifact is immutable and pending_recoveries re-reads it from the tree on every invocation, re-running the orchestrator hits the same refusal forever: the run is permanently stuck with no operator escape (`operator advance` passes a stage boundary, not a recovery request). The same file's own reasoning at pipeline/5_recensor/run.py:3846-3848 states the correct rule for the page-level-reread kind — "this stage does not request an operation nothing downstream can honor, because a request the orchestrator can only refuse turns a graceful hold into a hard failure for no gain" — and fallback-recrop on real ingress is exactly that case, unguarded.

**Scenario.** Real submission; the Designator's crops leave >= MINIMUM_INK_PIXELS of measured ink outside every cut on some page (near-certain on a first live run), and a page Testimonium carries text outside the act attachments. The Recensor publishes a recovery-request with origin `coverage-observation`. The orchestrator reaches the `recovery` sequence member, dispatches pipeline/2_designator/run.py --operation recover, which exits 2. The orchestrator raises `ContractError: pipeline/2_designator/run.py exited 2` and the run terminates fatally. Every act already read is stranded in the run tree, no export is produced, and every re-run repeats the abort.

**Proposed fix.** Either gate the Recensor's fallback-recrop request on ingress route the same way it already gates page-level-reread (hold the act with a named reason instead of requesting an operation nothing can answer), or have drive_recovery treat an unanswerable request as a per-act hold rather than a run abort. Add a test that drives a real-ingress run in which an outside-ink observation fires and asserts the act is held and the export still produced.


### F084 [high] One unparseable engine response ends the Perlector stage and the whole run

`pipeline/4_perlector/live_reader.py:385` — operability — from Governance and ARCHITECTURE invariants i; verified by running: False

**Claim.** The live reader raises EngineSignalRefusal (a ContractError) when ChairClient could not parse the response body (line 385) or when the engine's finish_reason is outside {"stop"} / {"length"} (line 137). Nothing in pipeline/4_perlector/run.py catches EngineSignalRefusal, so it propagates to run_stage and the stage exits EXIT_FATAL, which the orchestrator turns into a run abort. The class's own docstring names the reason ("A Perlector reading has no `failed` shape today"), but the consequence is that one bad body out of N acts ends the run, in direct contrast with the Attestatores, which retain an honestly `failed` attempt per witness and continue (pipeline/3_attestatores/run.py:1997-2005: "an unrecordable response is accounted inside an honestly `failed` attempt that says why ... rather than ending a stage that was reading ink nobody doubts because one witness of three failed"). The Perlector is resumable, so previously sealed readings survive a re-run, but the offending act will fail again identically; a persistently odd finish_reason (vLLM emits `abort` on cancellation and `tool_calls` under some configurations) stalls the run the same way finding 2 does.

**Scenario.** Live run over 200 real pages. At act 137 the served engine returns a body whose `choices` is malformed, or a finish_reason of `abort`. live_reader raises EngineSignalRefusal, the Perlector exits 2, the orchestrator raises, the run ends with no export. The raw bytes are retained and the refusal names them, so nothing is silently lost — but a whole pod-hour of reading produces no deliverable and the operator must repair the cause before any of the remaining 63 pages can be read.

**Proposed fix.** Give the Perlectio a retained `failed` outcome shape mirroring the Testimonium's, so one act's unrecordable response becomes a held act with its raw_response_ref as evidence and the pass continues. Failing that, catch EngineSignalRefusal in the act loop, publish a not-run/failed record naming the retained bytes, and let the Recensor hold the act.


### F085 [medium] Preference screens walk only dicts and lists; a tuple hides every field beneath it

`common/corpus_register.py:652` — governance — from Governance and ARCHITECTURE invariants i; verified by running: True

**Claim.** refuse_capture_preference (common/corpus_register.py:627) and assert_no_order_bearing_field (pipeline/4_perlector/dossier.py:601) are described in their own docstrings and in common/test_preference_screen_walks.py as the mechanical enforcement of GOVERNANCE 3 over untrusted, model-derived payloads — the runtime half that the shapes-1 AST guards complement. Both worklists descend only into dict and list (`isinstance(current, (dict, list))`); a tuple, set or any other container is treated as a leaf and its whole subtree goes unexamined. This is not academic, because common.contracts.canonical.canonical_bytes serialises a tuple to a JSON array, so the hidden fields reach the sealed artifact looking exactly like list members that the screen would have refused. The screen family's own guard suite (common/test_preference_screen_walks.py) drives deep nesting, cycles and shared siblings but never a tuple, so the gap is invisible to the tests that exist to prove these walks complete.

**Scenario.** Verified by running: refuse_capture_preference({"a": ({"preferred": "cap1"},)}) returns without refusing, while refuse_capture_preference({"a": {"preferred": "cap1"}}) raises SchemaRefusal; assert_no_order_bearing_field({"a": ({"trust_order": 1},)}) and ({"a": [({"preferred_witness": 1},)]}) both pass, while the plain-dict form refuses. canonical_bytes({"a": ({"preferred": "cap1"},)}) returns b'{"a":[{"preferred":"cap1"}]}'. Any future producer that builds a payload fragment with `tuple(...)` — an ordinary Python idiom, and already used in this tree for frozen records — takes everything under it outside the screen and into a sealed record.

**Proposed fix.** Descend into tuples (and sets, refusing a set outright since it cannot be canonicalised) in both walks, or refuse any non-dict/list container as an unwalkable value; add a tuple case to every row of common/test_preference_screen_walks.py's DRIVEN_SCREENS.


### F086 [medium] The local gate cannot go green on a machine where the session runs as root

`common/runtree/test_runtree_store.py:2335` — transferability — from Governance and ARCHITECTURE invariants i; verified by running: True

**Claim.** test_the_shared_snapshot_fails_loudly_on_a_descendant_it_cannot_read chmods a directory to 0 and asserts tree_snapshot raises OSError. Under CAP_DAC_OVERRIDE (root, which is the default in most containers and in this very session) the chmod does nothing to the walker and no OSError is raised, so the test fails with "DID NOT RAISE OSError" and gives the reader no hint that the cause is the effective uid rather than a real regression in the walker. This makes `.githooks/check-all.sh` — the gate hard rule 14 requires to exit 0 before a merge — unable to pass on a root container, and it is the only failure in the whole suite here. The neighbouring test in the same file (line 2310) shows the house pattern for exactly this situation: when the guarded path is unreachable on this interpreter it calls pytest.skip with a sentence saying so rather than failing.

**Scenario.** Verified by running: `.venv/bin/python -m pytest -q -p no:randomly` over the full tree in this root container produces exactly one failure, common/runtree/test_runtree_store.py::test_the_shared_snapshot_fails_loudly_on_a_descendant_it_cannot_read, "Failed: DID NOT RAISE OSError". CI runs as a non-root user so the same commit is green there; a person moving the checkout to a root container reads a red gate and no explanation.

**Proposed fix.** Guard the test the way its neighbour at line 2310 is guarded: probe whether the chmod actually denies this process (open the child and catch PermissionError) and pytest.skip with a sentence naming the effective uid when it does not, so an unreachable guard reports "proves nothing here" instead of failing.


### F087 [medium] The alignment deadline can be silently absent and the record cannot say so

`common/alignment.py:366` — silent-failure — from Governance and ARCHITECTURE invariants i; verified by running: True

**Claim.** The SIGALRM backstop that bounds SequenceMatcher is armed only when SIGALRM and ITIMER_REAL exist, the caller is on the main thread, and no interval timer is already running; otherwise alarm_armed stays False and the alignment runs with no wall-clock bound at all. The module takes great care elsewhere to keep an instrument's silence distinguishable from its measurement — DEADLINE_REASON exists precisely so "the backstop fired" is not read as a fact about the witness (GOVERNANCE 10) — but the success return at line 420 carries only status/witness/anchor/spans and says nothing about whether a deadline was in force. The module's own docstring records that the slowest input the sealed pair bound admits takes 283.9 s with this matcher, so the unbounded case is a real multi-minute stall on a card that bills by the hour, and it leaves no trace in the retained record.

**Scenario.** Any caller that reaches align_witness_to_anchor from a worker thread, or while some other component holds an ITIMER_REAL (a profiler, a supervisor watchdog, a future concurrent page-alignment pass), silently loses the bound. A pathological page pair then runs for minutes with no deadline; the produced record reads `{"status": "aligned", ...}`, indistinguishable from an alignment that completed inside the configured timeout, so nothing downstream and no later reader can tell that config/alignment.toml's timeout_seconds did not apply.

**Proposed fix.** Record the fact: add a deadline-in-force field (or a named `unbounded-alignment` reason code) to the returned record whenever alarm_armed is False, so a reader can tell a bounded alignment from an unbounded one; and consider refusing outright rather than running unbounded when the sealed limits say a timeout is required.


### F088 [medium] Instrument thresholds live in source, outside the run's config_digest

`common/residual_ink.py:89` — governance — from Governance and ARCHITECTURE invariants i; verified by running: False

**Claim.** The numbers ARCHITECTURE and GOVERNANCE 9/10 expect alpha testing to settle — MINIMUM_INK_PIXELS (common/residual_ink.py:98), MINIMUM_FRACTION_OUTSIDE_COVERAGE (:146) and MIN_PIXELS_PER_CHARACTER (pipeline/4_perlector/truncation.py:70) — are module constants, not sealed configuration. run_sealed_config_digests and the run authority's config_digest are computed only over the named TOML files (common/stage.py:1956-1978 and the surrounding digest block), so changing one of these constants between two live runs leaves the two runs' provenance byte-identical. GOVERNANCE 6 says configuration protects reproducibility going forward and the record protects the past; here neither does, because the instrument that decided whether an act was held is not named anywhere in the act's record. The residual-ink constants at least carry a "PROPOSED, NOT YET MEASURED" banner (line 89), which is honest, but honesty in a comment is not provenance in the run.

**Scenario.** Tyrel runs 50 pages, sees every act held as truncated, edits MIN_PIXELS_PER_CHARACTER to 20000, re-runs the same 50 pages, and gets a clean export. The two runs' run.json sealed_config_digests are identical; nothing in either run tree, export manifest or Archetypus record distinguishes the instrument that held the acts from the one that passed them, and a later reader comparing the two runs cannot tell what changed.

**Proposed fix.** Move the three thresholds into a sealed config table (a new config/instruments.toml, or the existing coverage_audit table for the ink pair) read through the same require_sealed_config point-of-use path the recovery and audit policies use, so the resolved values enter config_digest and are re-checked where they are applied.


### F089 [medium] No test exercises any pixel-scale instrument at real page scale

`proof/synthetic_pages.py:50` — test-gap — from Governance and ARCHITECTURE invariants i; verified by running: True

**Claim.** Every shipped fixture page is 200x260 px (proof/synthetic_pages.py:50-51, 67-68, 88-89; measured on disk at proof/fixtures/synthetic-two-page-v0/*.png as 52,000 px each). A real 300-DPI leaf is around 8.4 million pixels, roughly 160x larger, and three instruments key off absolute pixel geometry: truncation's px/char ratio, residual ink's pixel floor, and the ink-map area fractions. The repository has already been bitten by this once and built exactly the right answer for one instrument — common/test_designator_recensor_ink_calibration.py::test_the_containment_is_not_vacuous_on_a_photographed_page runs the ink thresholds over a photographed-shaped page precisely because a fixture-scale test was vacuous — but nothing equivalent exists for the truncation instrument or for an end-to-end pass over a real-scale page. The whole suite therefore reports green over behaviour that inverts at production scale (finding 1).

**Scenario.** The full suite passes; the first live run over real 300-DPI leaves holds nearly every act. No test in the tree would have caught that, because the only page geometry any test ever presents to truncation.classify is one where region_pixels/len(text) stays well under 2000.

**Proposed fix.** Add a photographed-scale case to pipeline/4_perlector/test_truncation.py (a crop of realistic pixel dimensions with a realistic character count, asserting `complete`), and a real-scale page to the fixture set — or at minimum a scale-sweep test that fails when a fixture-calibrated constant inverts its verdict between fixture and photographed geometry.


### F090 [low] The projection-identity test's new-format guard cannot fire

`pipeline/orchestrator/test_projection_identity.py:96` — test-gap — from Governance and ARCHITECTURE invariants i; verified by running: False

**Claim.** This module presents itself as "GOVERNANCE 5's executable half" and promises that when the Armarium gains a second export format "this test is where it must be added, and its failure mode should be a missing format rather than a silent pass over the one that exists". The guard it relies on asserts that the Armarium's produced artifact KINDS are exactly {export, manifest-entry}. But the three literal-text formats (text-bundle, acts-database, jsonl) all ship as members inside the single `export` blob, not as new artifact kinds, so the guard never fired when they landed — the docstring still says "the one export format the Armarium actually produces today" while three are produced. Cross-format identity is in fact checked, by armarium_export._compare_literal_projections at build and verify time with unit coverage in test_armarium_export.py, so GOVERNANCE 5 is not unenforced; what is wrong is that the file the project points at as the executable half of GOVERNANCE 5 is stale and its stated tripwire is inert.

**Scenario.** A fourth literal format is added as a member of the export ZIP (the pattern the last three followed). test_projection_identity.py passes unchanged, its produced_kinds assertion never sees the addition, and its docstring continues to claim the test covers "the one export format" — so a reviewer reading it concludes cross-format identity is covered end to end when what the test actually proves is one field of the export payload against the Archetypus record.

**Proposed fix.** Make the guard enumerate the packaged member paths (or the manifest's `formats` list) rather than the artifact kinds, and either extend the test to open the ZIP and compare the literal projections end to end or rewrite the docstring to point at _compare_literal_projections as the place the claim is actually proven.


### F091 [low] Invariant 7 is carried nowhere on the product that leaves the pipeline

`pipeline/7_armarium/armarium_export.py:3667` — governance — from Governance and ARCHITECTURE invariants i; verified by running: False

**Claim.** ARCHITECTURE invariant 7 and GOALS both hold that the pipeline's output is a machine reading, not truth. That statement exists only in source docstrings (pipeline/6_archetypus/run.py:3). The export manifest's claim set is closed and enumerated (armarium_export.py:872-885) and contains status, partial_reasons, terminal_ledger, act_partition, submission_inventory, page_census, pixels, retained_run_references, semantic_annotations, transcription_annotations, uncertainty, display, salvage, ink_map, not_measured; `canonical_text` carries authority/field/hash/derived_columns_are_marked/identity_verified_across. Nothing anywhere in the packaged bundle says the delivered text is a machine reading rather than a transcription anyone should treat as authoritative. Every other invariant this stage holds is published on the bundle's own face precisely so a reader does not have to infer it; this one is not.

**Scenario.** A researcher receives armarium-export.zip, opens acts.jsonl, and reads `canonical_clean_text` with `authority: archetypus` beside it. Nothing in the product tells them the text is a machine reading; the only place the project says so is a Python docstring they will never see. The distinction GOALS 2 and invariant 7 exist to protect is lost at the exact moment the material leaves the pipeline.

**Proposed fix.** Add a fixed `canonical_text.nature` (or a top-level `disclosure`) field to the export manifest stating that the established text is a machine reading and not truth, and repeat it as a header line in the human-readable text bundle; validate it in _verify_canonical_text_claim so it cannot be dropped.


### F092 [low] Spend and shutdown safety checks in the operator path are bare asserts

`operations/pod/cli.py:463` — config — from Governance and ARCHITECTURE invariants i; verified by running: False

**Claim.** operations/pod/cli.py:463-465 and operations/operator/spend.py:58-62 use bare `assert` to establish that shutdown_deadline_seconds, shutdown_poll_interval_seconds, billing_cutoff_margin_seconds, max_hourly_usd, account_balance_floor_usd and hard_lifetime_seconds are not None before they are used. Python strips assert statements under -O / PYTHONOPTIMIZE, and unlike the stage subprocesses — which the orchestrator launches with `-I` (pipeline/orchestrator/run.py:182), so PYTHON* startup controls are ignored — the operator CLI is invoked directly by a person with whatever environment they have. .githooks/check-all.sh explicitly unsets PYTHONOPTIMIZE with the comment that inherited overrides "can remove assertions", so the project already knows this failure mode exists; the money and shutdown paths, which GOVERNANCE 8 and hard rule 2 make the most consequential in the tree, are not protected from it.

**Scenario.** An operator with PYTHONOPTIMIZE=1 exported (common on machines tuned for other Python work) runs `operator close` or `operator launch`. The asserts vanish; a policy row with a missing shutdown_deadline_seconds flows onward as None into the shutdown deadline arithmetic instead of being refused at the door, and the failure surfaces later as a TypeError or an unbounded wait rather than as a named refusal — on a pod that is billing.

**Proposed fix.** Replace these asserts with explicit refusals (ContractError / SchemaRefusal naming the missing policy field), or refuse at the CLI entry point when sys.flags.optimize is nonzero, the way check-all.sh already refuses an unverified environment.


### F093 [info] Fewer than two literal formats turns off the cross-format text-identity check

`pipeline/7_armarium/armarium_export.py:1296` — governance — from Governance and ARCHITECTURE invariants i; verified by running: False

**Claim.** _compare_literal_projections is the mechanical enforcement of GOVERNANCE 5 across products, and both its call sites (line 445 at build and line 1266 at verify) run it only when two or more of text-bundle/acts-database/jsonl are selected. config/formats.toml ships all three, so the default posture is sound, and the manifest is honest about the alternative: canonical_text.identity_verified_across is [] and verify_projection_identity records status "not-applicable-fewer-than-two-literal-formats". Recorded here only so the per-invariant ledger is complete: with a single literal format configured, "one text, everywhere" is disclosed as unverified rather than verified, and nothing prevents such a configuration from being sealed into a run.

**Scenario.** An operator configures formats = ["jsonl", "review-items"] for a lightweight run. The bundle builds and verifies; the manifest says identity_verified_across = [] and status "not-applicable-fewer-than-two-literal-formats". No divergence check runs anywhere in that run. The disclosure is present and correct, so this is not a silent failure — but a reader who does not open the manifest will assume GOVERNANCE 5 was checked because it is checked on every default run.

**Proposed fix.** None required; optionally refuse a format selection with fewer than two literal-text formats unless an explicit flag says the operator wants an unverifiable projection, so the choice is made deliberately rather than by editing a TOML.


### F094 [high] Crashed or held stage stderr is never written to the volume; pod diagnostics die with the pod

`operations/pod/pod_run.py:534` — operability — from Transfer and diagnosis from records alon; verified by running: False

**Claim.** pod_run runs the orchestrator with inherited streams (`subprocess.run(argv, cwd=cwd, env=..., check=False)`, comment: 'buffering an unbounded stage transcript ... would be a second, weaker copy'), pipeline/orchestrator/run.py:266 runs each stage the same way, and pod_timer.py:146 launches its child with a bare Popen. Every stage's refusal text, every traceback behind an EXIT_FATAL, and the Attestatores' hold reasons (pipeline/3_attestatores/run.py:5737, 5768, 5834, 5878 print the reason to stderr and return EXIT_HELD; the orchestrator at run.py:599 says 'its reason is on stderr above') go only to the container's stdout/stderr. Nothing writes a transcript under the volume. The run report itself says 'read its transcript' (pod_run.py:655-659) but no transcript exists anywhere fetch-run can reach, and RunPod's container log is gone once the pod timer destroys the pod. GOVERNANCE 2 and hard rule 7: the one record that says why a stage stopped is lost silently.

**Scenario.** On a real pod the Perlector stage raises a ContractError (or the Attestatores hold with 'attempt tally UNKNOWN: ...'). pod_run writes state 'failed'/'held' with detail 'read its transcript', returns, the pod timer closes the pod. At home, fetch-run brings back a tree whose 4_perlector is unsealed and a report that says to read a transcript; there is no transcript on the volume, and the reason is unrecoverable.

**Proposed fix.** Tee the orchestrator's (and each stage's) stderr/stdout into a bounded, launch-token-named file beside the run report on the volume (or under runs/<id>/ as an out-of-inventory log the store's scope names), and name it in the run report; have the fetch-run receipt fetch it with the evidence.


### F095 [high] review, advance and backup refuse on Linux with util-linux < 2.40; requirement undocumented

`operations/operator/custody.py:313` — transferability — from Transfer and diagnosis from records alon; verified by running: True

**Claim.** LandlockConfinement builds `setpriv --no-new-privs --landlock-access fs:...`, which util-linux added in 2.40. Ubuntu 24.04 LTS ships 2.39.3 (this host), Ubuntu 22.04 ships 2.37, and RunPod's stock images are one of those, so every custody-backed verb (review, advance, backup, ingest, triage, scantailor import) refuses on the most common Linux a fresh session will be handed. The refusal is loud (CONSOLE_CUSTODY_REFUSED) but operations/operator/README.md never mentions Linux, Landlock, setpriv or a util-linux version, and the test suite marks this host class as a skip (test_permission_boundary.py:762), so a green gate on such a machine says nothing about review working there.

**Scenario.** Verified by running: from /tmp, `verbatus review --run-root <scratch>/state/runs --run-id fixture-run` on this Linux host exits 2 with 'setpriv: unrecognized option --landlock-access'. A fresh Linux session handed a run tree for diagnosis cannot open it with the shipped verb at all.

**Proposed fix.** State the host requirement (macOS with sandbox-exec, or Linux with util-linux >= 2.40 and a Landlock-capable kernel) in the operator README and in the CONSOLE_CUSTODY_REFUSED copy; consider a documented read-only fallback (`review --json` through the parent-side projection with the confinement refusal stated on screen) for diagnosis-only hosts.


### F096 [high] A moved or restored operator state directory breaks status, export and run

`operations/operator/records.py:447` — transferability — from Transfer and diagnosis from records alon; verified by running: True

**Claim.** DescriptorStore stores each receipt as `str(receipt.resolve())` (an absolute path) and ReceiptStore.read (records.py:172-178) refuses any path not under the current receipts directory. The README says the state directory is where the tool 'keeps its own records' and `--state-dir` moves it, but a state directory copied to another machine, restored from a backup at a different path, or moved is unreadable through the descriptor, and the STATUS_UNREADABLE copy tells the operator to 'repair or replace' a record that is intact. export (surface.py:1271) and run (`_prior_run_state`, surface.py:2017) read the same receipt outside any RecordError handler and fall through to UNEXPECTED, whose copy says 'this terminal message is the only record'.

**Scenario.** Verified by running: `cp -a state state-copy`, then `verbatus --state-dir state-copy status` prints 'run record 1: UNREADABLE; it was not treated as success ... operator receipt path is outside the receipt directory' and exits 2; `export` and `run --run-id fixture-run` against the copy exit 2 with 'Verbatus met a problem it could not classify'.

**Proposed fix.** Record receipts in the descriptor by basename (they are content-addressed, so the name is the identity) and resolve them against the current receipts directory; wrap the receipt reads in export/_prior_run_state with the RecordError handler status already has.


### F097 [medium] The run receipt for a run that exited 0 or 3 keeps no stderr, argv, commit or config digests

`operations/operator/surface.py:1175` — operability — from Transfer and diagnosis from records alon; verified by running: True

**Claim.** The local `run` verb captures the orchestrator's output but records `completed.stderr or completed.stdout` only when the exit code is outside {0, 3} (surface.py:1157-1178). A held run (exit 3), the case that most needs a reason, discards the stderr that carries the Attestatores' hold reason, and every run receipt records only run_root, run_id, scenario, fixture and state: no repository commit (`_repository_commit` exists at surface.py:2996 and is used only by boot), no --models-config/--serving-recipes-config/--witness-context-config paths or digests, no argv, and no start time (recorded_at is the end).

**Scenario.** `verbatus run --run-id r1 --models-config ... --serving-recipes-config ... --witness-context-config ...` on a laptop is held at the Attestatores. The receipt says state 'held' with no reason; the stderr that named the reason is gone once the terminal closes; a later session cannot tell which trio or which commit the run used from the receipt alone.

**Proposed fix.** Record commit, the three config paths with their SHA-256, the full argv, started_at, and a bounded tail of stderr on every run receipt regardless of exit code.


### F098 [medium] The run tree carries no code commit and no timings beyond serving receipts' started_at

`common/runtree/store.py:89` — operability — from Transfer and diagnosis from records alon; verified by running: False

**Claim.** run.json binds source, config_digest, adapter recipes, chairs, register and sealed_config_digests, but nothing in the tree names the commit the code ran at, and stage artifacts are deliberately deterministic (common/stage.py:2455-2480 refuses endpoint/started_at in provenance), so the only clock in a run tree is `started_at` inside chair serving receipts. Stage start/finish times and durations exist nowhere. The commit exists only in the pod-run report (via the bootstrap plan) and in the launch receipt on the operator's machine; neither travels with the tree (see the fetch-run finding).

**Scenario.** A tree is handed to a fresh session by itself. It can prove the config bytes by digest but cannot say which commit produced them or how long the Perlector took; the timings needed to judge a stall or a timeout are absent.

**Proposed fix.** Seal `repository_commit` into run.json at creation (the orchestrator can read it or take it from the pod plan), and publish a per-stage timing receipt under receipts/ (already the home for non-deterministic records) that the seal references.


### F099 [medium] review never reads serving receipts: a tree that lost its serving provenance reviews as intact

`operations/operator/review.py:1730` — silent-failure — from Transfer and diagnosis from records alon; verified by running: True

**Claim.** review walks receipts/sha256 only for approval records (review.py:1724-1770) and the projection carries per-act resolved identity/revision from stage artifacts, but the chair serving receipts (the record of the endpoint, engine, revision kind, digest manifest, dtype and started_at that actually answered) are never verified or rendered, and review_text shows no run-authority fact at all (no config_digest, no sealed_config_digests). GOVERNANCE 6 says provenance travels with the record; here the surface built to read the record does not check that the provenance is still there.

**Scenario.** Verified by running: deleting the whole receipts/ directory from a copy of the fixture tree produces review output byte-identical to the intact tree. A fetched tree whose receipts were lost or tampered (a receipt's bytes changed under its digest name) is reported as sealed and complete.

**Proposed fix.** Have the projection resolve every receipt_ref cited by Attestatores/Perlector artifacts through RunTree.read_run_receipt and render, per act, the engine/revision/endpoint kind that served it; refuse (or mark seal-invalid) when a cited receipt is missing or fails its digest.


### F100 [medium] A partial copy of a complete run reads as an interrupted run, and review recommends resuming it

`operations/operator/review.py:1383` — correctness — from Transfer and diagnosis from records alon; verified by running: True

**Claim.** The run tree has no run-level terminal record; completion is inferred from the presence of stage directories. A copy that lost its last stages therefore reads as a run that stopped, and next_action says to resume it with `verbatus run --run-id ...`. Nothing in the tree lets a later session tell 'this run reached the Armarium and the copy is short' from 'this run stopped at the Archetypus'.

**Scenario.** Verified by running: a copy of the complete fixture tree without 6_archetypus and 7_armarium renders every earlier stage sealed, 'archetypus: not-run', 'Export: none', and 'The supported continuation is to resume this run'. On a real run a resume against such a copy would re-run stages whose sealed results exist elsewhere, and the two trees would then differ under one run id.

**Proposed fix.** Seal a run-level end record (state, last stage, exit vocabulary, finished_at) under receipts/ when the orchestrator returns, reference it from the Armarium seal, and have review distinguish 'ended, records absent here' from 'not run'.


### F101 [medium] fetch-run cannot find the pod-run report though the launch receipt already holds its key

`operations/operator/surface.py:3353` — operability — from Transfer and diagnosis from records alon; verified by running: False

**Claim.** fetch-run refuses to derive the bootstrap/pod-run report keys and requires `--evidence-key` for each, yet the launch receipt written by this same surface records `docker_start_cmd` with the report path already bound to the launch token (surface.py:2507-2521; launch.py:167-170 binds `<stem>-<token><suffix>`), and the lease record carries `launch_token`. The pod-run report is the only record that names the run's final state, the placement tier, the orchestrator argv, the commit, and the pod's approved storage roots; making the operator transcribe a 32-hex token by hand from a JSON receipt is exactly the step that gets skipped on a phone.

**Scenario.** Operator runs fetch-run without --evidence-key; the receipt records that none of the reports came home; a later session has the tree and the preflight evidence but not the report that says why the run stopped or which commit ran. Or the operator mistypes the key and the refusal is recorded as a missing object.

**Proposed fix.** When the state directory holds a launch receipt whose docker_start_cmd names the same volume, offer (and record) the bound report keys as defaults; at minimum print the exact keys from the launch receipt in the fetch-run screen.


### F102 [low] The fetch-run receipt does not record which volume the tree came from

`operations/operator/surface.py:936` — operability — from Transfer and diagnosis from records alon; verified by running: False

**Claim.** The receipt records prefix, into, counts, stages and evidence, but not the DATACENTER:VOLUME_ID the operator named (it is presented on screen only via `volume.describe()`). A fetched tree's origin, the one identifier needed to go back to the volume for a missing object, is not in the operator's own record.

**Scenario.** Two volumes hold runs named the same id (a re-run); the receipt for the fetched tree cannot say which one it came from.

**Proposed fix.** Record datacenter_id, volume_id and endpoint_url (not the keys) in both the partial and verified fetch-run receipts.


### F103 [medium] Backup snapshot has no timestamp, source, host or completeness state, and no restore path

`operations/operator/backup.py:155` — transferability — from Transfer and diagnosis from records alon; verified by running: True

**Claim.** The snapshot record is {schema, run_id, files, excluded_publication_temporaries}. Several snapshots of the same run (partial, then complete) are distinguishable only by digest; nothing says which is later, from which machine or run root it was taken, or whether the source was verified-partial. The fifteen verbs include no restore, and the README describes the layout in one table cell, so a later session handed a synced `objects/sha256` + `snapshots/sha256` folder has to rebuild the tree by hand from the inventory with no documented procedure.

**Scenario.** Verified by running: backing up the intact tree and a copy missing the last two stages both publish ordinary snapshots; the two snapshots cannot be ordered or told apart by anything but their file lists.

**Proposed fix.** Add recorded_at, source run root, the fetch-run receipt digest (when the source was a fetched tree), and hostname to the snapshot; ship a restore/rehydrate verb (or document the exact reconstruction) and record backups in the operator descriptor so status shows them.


### F104 [low] Backup copies and inventories files outside the run tree's scope (e.g. .DS_Store) as run-tree files

`operations/operator/backup.py:533` — correctness — from Transfer and diagnosis from records alon; verified by running: True

**Claim.** `_inventory_descriptor` uses the store's managed paths only to recognise publication temporaries; every other regular file is copied and listed in the snapshot's `files` without a mark. fetch-run refuses an out-of-scope object by name (surface.py:3167); backup silently admits one. A synced Mac directory is the destination, so Finder residue is the realistic case.

**Scenario.** Verified by running: a copy of the tree carrying .DS_Store at the root and inside 2_designator/blobs/sha256 backs up with 102 files inventoried instead of 100, both .DS_Store entries recorded as run-tree members.

**Proposed fix.** Classify inventory entries against inventory_scope(): refuse or list separately (as `unmanaged`) anything the store never writes.


### F105 [medium] macOS Finder residue inside a run tree is reported as damaged evidence

`common/runtree/store.py:987` — transferability — from Transfer and diagnosis from records alon; verified by running: True

**Claim.** The artifact walk treats every `*.json` under an artifacts kind directory as an artifact, and the blob walk expects only digest-named regular files. Copying a run tree through Finder to an exFAT/SMB/USB volume and back leaves `._<name>` AppleDouble files (when xattrs cannot be stored) and `.DS_Store` files. A `._art_x.json` makes review refuse the whole tree with the CONSOLE_TREE_UNREADABLE copy ('preserve the run tree and investigate the named evidence problem'); a `.DS_Store` in a blobs directory makes review report the stage's seal as `seal-invalid` ('a completion seal ... no longer verifies'). Both are Finder's doing, not damage, but the operator is told to open an evidence investigation.

**Scenario.** Verified by running: placing `._art_b6e5f72165341c67.json` and `.DS_Store` in 4_perlector/artifacts/audit-draft refuses the tree ('could not be read as an artifact: Expecting value'); placing `.DS_Store` in 2_designator/blobs/sha256 yields 'designator: seal-invalid ... its named inventory no longer match'.

**Proposed fix.** Name Finder residue explicitly in the refusal (`._*`, `.DS_Store`, `.Spotlight-V100`, `.fseventsd`) with the instruction that removing it is safe and does not touch evidence; consider ignoring `._*`/`.DS_Store` in inventories while still refusing every other foreign entry.


### F106 [low] The run screen names pages the run did not process

`operations/operator/surface.py:3510` — correctness — from Transfer and diagnosis from records alon; verified by running: True

**Claim.** `_declared_work` lists every `[[page]]` in proof/skeleton_fixture.toml regardless of the `scenarios` gate on page 3, while the Armarium count comes from the run. The default happy run therefore prints 'Pages accounted for: page 1, page 2, page 3 (2 total)'.

**Scenario.** Verified by running: `verbatus run --run-id fixture-run` prints 'Pages accounted for: page 1, page 2, page 3 (2 total). Acts accounted for: act a1, act a2 (2 total).', a self-contradicting line on the very screen that says the run is complete.

**Proposed fix.** Filter declared pages by the run's scenario (or read the page list from run.json's source_manifest after the run) before printing.


### F107 [medium] UNEXPECTED failures leave no record and drop the traceback

`operations/operator/cli.py:583` — silent-failure — from Transfer and diagnosis from records alon; verified by running: True

**Claim.** cli.py:583-586 and entry.py:41-43 turn any uncaught exception into UNEXPECTED with `detail=str(error)`; no receipt is written and the traceback is discarded. The registered copy admits it: 'this terminal message is the only record of what happened'. For a semi-attended operator on a phone that is the one failure class with nothing to hand to a later session.

**Scenario.** Verified by running: export against a copied state directory prints only 'Saved detail: operator receipt path is outside the receipt directory', no file, no stack, and the next step says to photograph the screen.

**Proposed fix.** Write an `unexpected` receipt (exception type, message, bounded traceback, argv, cwd, commit) into the state directory before rendering, and name its path in the message; keep the three-part copy.


### F108 [medium] status shows nothing for backup, review or advance; the operator's sequence is not reconstructible

`operations/operator/cli.py:685` — operability — from Transfer and diagnosis from records alon; verified by running: True

**Claim.** `_backup_in_custody`, `_review_in_custody` and `_advance_with_confirmation` write no operator receipt and no descriptor entry; `_status_projection` (surface.py:2382-2449) has no arm for fetch-run, backup or advance. An advance leaves its approval record inside the run tree (approver, reason, target hash, timestamp, which is sufficient), but a backup leaves only a snapshot on the destination, and neither appears in `verbatus status`. The sequence 'launched, ran, fetched, backed up, advanced, exported' cannot be read from the operator's own records without the terminal.

**Scenario.** Operator backs up a run to a synced directory, later cannot remember which run root was backed up where; status lists launch/run/fetch-run/export only.

**Proposed fix.** Write a receipt for backup (destination, snapshot digest, counts, source run root) and advance (stage, seal digest, approval record path), and add status projections for fetch-run, backup and advance.


### F109 [low] notify.sh depends on a python3 on PATH rather than the frozen interpreter

`operations/notify/notify.sh:219` — transferability — from Transfer and diagnosis from records alon; verified by running: False

**Claim.** The payload is encoded by `python3 -c ...` found on PATH. A fresh Mac without the Command Line Tools has a `python3` stub that pops the installer dialog; a minimal pod image may have none. The bridge then reports 'NOT DELIVERED (could not encode payload)' even though the checkout's `.venv/bin/python` is present. Not a leak: `set +x`, `curl 2>/dev/null` and fixed `fail` strings keep the topic out of stderr, and the only topic ever printed is the reserved test-sink constant.

**Scenario.** Decision notification on a machine with no usable `python3` on PATH: every send fails at payload encoding; the terminal says so, the phone never hears the hold.

**Proposed fix.** Prefer `$root/.venv/bin/python` when it exists, falling back to python3; keep the encoding failure loud.


### F110 [low] fetch-run's evidence folder mixes every launch's preflight tree and cannot say which is this run's

`operations/operator/surface.py:3376` — operability — from Transfer and diagnosis from records alon; verified by running: False

**Claim.** Evidence is fetched from the volume-wide `preflight/` prefix into `<into>/evidence/`, shared by every run fetched into that root. Preflight trees are keyed by the bootstrap report stem, which carries the launch token (bootstrap_main.py:239), while the run tree carries no launch token and the pod-run report that joins run_id to token is not fetched by default. After two launches on one volume the receipt lists both preflight trees with no statement of which measured the chairs for this run.

**Scenario.** Second real launch reuses the volume; fetch-run for run B fetches preflight/<A-token> and preflight/<B-token>; a later session reading evidence/ has to guess which serving logs and receipts belong to B.

**Proposed fix.** Record the launch token in the run tree (run.json or a receipt) or in the fetch-run receipt from the launch receipt, and name the matching preflight tree explicitly.


### F111 [low] A single object over 256 MiB refuses the whole fetch-run, and retries re-download verified files

`operations/operator/surface.py:156` — operability — from Transfer and diagnosis from records alon; verified by running: False

**Claim.** MAX_FETCH_OBJECT_BYTES is 256 MiB and `_fetch_or_compare` passes it to every object. Exemplar page blobs are the submission's masters (a large-format 600 dpi TIFF can exceed this); one such blob makes fetch-run refuse the tree as a whole rather than name the object as too large and continue with the rest as verified-partial. Also, when a local file already exists the object is downloaded again into a staging file to compare (surface.py:3436-3446), so a retry after a refusal re-transfers the entire tree.

**Scenario.** A run whose page 17 master is 300 MiB: fetch-run stops at 1_exemplar/blobs/sha256/<digest> with a size refusal, nothing fetched this attempt is kept, and every retry re-downloads the pages already verified.

**Proposed fix.** Raise or make the bound configurable per object class, report an oversized object by name in a verified-partial receipt, and compare existing files by digest before re-fetching.


### F112 [low] Custody backend availability is discovered per verb rather than reported by status

`operations/operator/surface.py:1503` — operability — from Transfer and diagnosis from records alon; verified by running: True

**Claim.** `status`, the verb every failure message sends the operator to, does not say whether this machine can run the custody-backed verbs. On a Linux host with an old setpriv or a Mac without sandbox-exec the operator learns it only when review/backup/advance refuses.

**Scenario.** A fresh session on Ubuntu 24.04 runs status (ok), fetches a tree (ok), then review refuses on the Landlock launcher.

**Proposed fix.** Have status (or a doctor-style line at the start of every verb) report the confinement backend and whether its probe passes.


## Rulings made on the findings (Tyrel, 2026-09-15)

- **Stage 1 is witnesses only** (G6). The Perlector chair is absent for Stage 1 and its
  real-roster catalogue rows are removed so an affordable pod can pass preflight.
- **One failure is a glitch, two is a pattern** (G7). A single bad engine response holds
  that act and the run continues to export; more than one failure per 1000 pages stops
  the run at the stage boundary for investigation before the next stage. The threshold
  is sealed configuration. Queued as its own change after this pull request.
