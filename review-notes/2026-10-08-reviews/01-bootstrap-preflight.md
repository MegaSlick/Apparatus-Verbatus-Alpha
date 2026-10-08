# Review 1: bootstrap and preflight (Opus reviewer, read-only, 2026-10-08)

Outcome: setup time is dominated by reading the same model bytes 8-10 times, not by
one-core hashing. D, E, J, C8 open; C3 half done (selected roles only, no pool). #279
parallelised hashing within one snapshot; #282 touched run-tree manifests, not caches.
Disk churn: on a 120 GB container the Perlector (55.6 GB) and Reconstructor (55.6 GB)
caches cannot both fit beside the witnesses; LRU eviction forces re-copies, which likely
explains the 12.3 min Coniector re-copy. Rate assumed: 13.6 GB/min per pass.

Sizes (config/manifests): qwen3.8-27B 55.59 GB (32 files, perlector and reconstructor);
dai 16.60; chandra 10.61 (one 10.59 GB file); churro 7.53; surya 0.22; yolo 0.05.
Total 90.6 GB. Container disk: 60 GB default, 120 GB on the 80 GB+ tier
(operations/pod/models.py:283,310).

## Serial timeline today (bootstrap.py:721 runs steps strictly in order)

1. REPOSITORY, CONFIGURATION: seconds.
2. CUDA_COMPAT (bootstrap.py:996-1093): cuInit only when driver < 580.65.06 (:1029
   returns early). The failing 595.91.07 hosts pass this step.
3. UV_ENVIRONMENT (bootstrap.py:1095-1171): uv sync (~10 GB wheels), then each
   subprocess env (Surya ~14 GiB) serially; UV_CACHE_DIR on /tmp (bootstrap.py:75), so
   every pod re-downloads.
4. MODEL_STORE (bootstrap_main.py:1063 -> model_store.py:317-384): verify_store
   (model_store.py:1141-1229) hashes every artifact on the volume whatever the
   selection; artifact loop serial (:1150). 90.6 GB, ~6.7 min.
5. CHAIR_CACHE (bootstrap_main.py:1071-1088): local chairs copied, verify_snapshot
   (:1133), then registry.ensure (:1137) re-hashes. Loops over every chair, not
   preflight_roles.
6. PREFLIGHT (preflight.py:962-1072), serial per role: _verify_cache (:1293) -> ensure;
   on miss _make_room, StoreRoleFetcher.fetch serial copyfile (model_store.py:231-258),
   verify_snapshot re-reads (registry.py:493); then _smoke -> ServingManager.start ->
   ensure (manager.py:433) hashes a third time (registry.py:462) before vLLM loads.
   Witnesses 3x34.7 GB ~7.7 min; Perlector 3x55.6 ~12.3 min; Reconstructor same again
   into cache_root/reconstructor ~12.3 min.
7. Each stage subprocess (orchestrator run.py:483): ServingManager.start -> ensure full
   re-hash (2.6 + 4.1 + 4.1 min) plus LRU-forced re-copies (~12.3 each).

Setup reads ~530 GB (~39 min); stage starts add 150-390 GB. Matches the ~720 GB and
45 min of 2026-10-07.

## Findings, largest saving first

- F1 (~13 min setup + ~11 min stage starts): every ensure re-hashes the whole cache
  (registry.py:460-467; callers manager.py:433, bootstrap_main.py:318,
  2_designator/surya_detection.py:154,199, run.py:184,327). (a) in-process memo keyed
  by digest_manifest + root + per-file stat identity, filled only by a full verify:
  removes the smoke re-hash in the same process. (b) across processes: b1 sealed
  per-cache ledger bound to boot_id with files 0444 (changes the evidence claim: lead's
  call); b2 hash in parallel with vLLM weight load and refuse the first request until
  it matches (no policy change). Tests: test_chairs_verification.py:205-228 (asserts a
  hit re-digests; update deliberately), test_manager.py ~477, 499.
- F2 item D (~24 min single card): cache keyed by role (registry.py:457; role in the
  descriptor models.py:92-101; eviction by role name registry.py:523-546). Key by
  cache_root/by-digest/<digest_manifest>; role-free descriptor; role stays in
  VerifiedSnapshot.identity and receipts; _make_room on digests; per-digest lock
  (_promote backup name uses only pid, registry.py:641). Tests:
  test_chairs_verification.py:231-290 plus a new two-roles-one-copy test.
- F3 item E (~8 min): copy not hashed. Add copy_and_digest(source, target, row) in
  manifests.py (O_NOFOLLOW, size check first, 8 MiB chunks, hash while writing), run in
  a pool across files; ensure accepts the ledger; keep _inspect_snapshot's structural
  walk; _place_local_chair verifies once. Tests: test_chairs_verification.py:119,157;
  test_bootstrap_main.py:1108-1169.
- F4 item E second half (~6.7 min single; 4.1 witness pod, 2.6 big pod): boot
  verify_store hashes all 90.6 GB (model_store.py:364-366; real_roster_complete
  bootstrap.py:1240-1249). Keep structural checks; skip byte hashing when the copy will
  hash the same bytes against the same pin (plan already checks digest_manifest == pin,
  model_store.py:214-225). Receipt claim changes from "every byte verified at boot" to
  "bytes verified at copy, for roles X": lead's approval. Tests: test_model_store.py.
- F5 (saves a whole 30-45 min setup per bad host; 2 of 4 pods): CUDA_COMPAT returns
  before cuInit (bootstrap.py:1029 vs :1074-1092). Always cuInit(0) +
  cuDeviceGetCount via ctypes before uv sync. Tests: test_pod_runtime.py:5316-5486.
- F6 item J (~5 min): start the selected roles' copy-and-hash in a background thread at
  UV_ENVIRONMENT; MODEL_STORE and CHAIR_CACHE wait for it before recording completion.
  Tests: test_pod_runtime.py:5128,5207.
- F7: serial copies, per-artifact pools; one pool across all files of all needed
  artifacts, largest first (Chandra 10.59 GB single file).
- F8 item C8: only LRU on space (registry.py:536-555). Evict digests no later selected
  stage needs after each stage so Perlector + Reconstructor fit on 120 GB.
- F9 item C3: preflight serial (preflight.py:962); copy/verify next chair while the
  current smokes; order by stage need.
- F10 two-card leaks: MODEL_STORE fetches/verifies the whole roster; _build_cache loops
  every chair (bootstrap_main.py:1080); _local_bundles (:1632-1656) and
  _store_environments (:1572-1585) ignore the selection; _stage_environments respects it
  (:1619); preflight_roles only set by pod_run (pod_run.py:1933). Pass preflight_roles
  into all four. Tests: test_bootstrap_main.py:1025,2116; test_pod_run.py:1970-1996.
- F11 stability: MATERIALIZATION_LOCK_TIMEOUT_SECONDS = 60 (model_store.py:178); a
  second pod booting during a 7 min verify_store is refused; flock on a network mount
  may not hold.
- F12: _copy_existing_files re-copies during repair (registry.py:628-635; use os.link);
  uv cache on /tmp; README 207-209 predates #276.

## Cores and threads

_map_files_in_order (manifests.py:224-233): min(files, cpu_count, 16) per call;
effective parallelism = number of large files; barrier per artifact. Fetch/copy paths
are single-threaded. os.cpu_count returns the host count in a container; an
affinity-aware helper exists at operations/serving/surya_detector.py:503-505.
Recommend common usable_cpus() (sched_getaffinity, cgroup cpu.max), one pool across
all files largest-first, deterministic refusal order, workers clamp(usable, 2, 32),
I/O-bound reads up to min(32, 2x usable), env override recorded in the receipt.
