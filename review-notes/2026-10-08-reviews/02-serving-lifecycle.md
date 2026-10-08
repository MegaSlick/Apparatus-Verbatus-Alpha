# Review 2: model serving lifecycle (Opus reviewer, read-only, 2026-10-08)

Idle time is in loading models, not reading. Each witness, the Perlector and the
Coniector take the same cold path: re-hash the whole snapshot, launch vLLM, wait 1-6
min to ready, read, stop. The Coniector also copies 52 GB again (role-keyed cache) then
cold-starts the model the Perlector just stopped. Estimated on 2026-10-07: ~8-9 of 14.0
Attestatores min, ~7 of 23.6 Perlector min, ~10 of 12.3 Coniector min were loading.
Nothing scales to the card: nvidia-smi picks one of three fixed tiers; max_num_seqs and
gpu_memory_utilization are literals per tier. Polling (2 s readiness, stop, 5 s GPU
sampler) costs < 1 min per run. vLLM not installed here; cold-start figures from the
recipe header (4090 witnesses, 375 s Perlector).

## Start and stop path (shared)

ChairClient.__enter__ (client.py:358) -> ServingManager.start (manager.py:404):
assert_no_discoverable_local_env (:416); _launchable_profile/_assert_runtime (:431-432);
registry.ensure (:433) -> _ensure_huggingface (registry.py:438) ->
inspect_snapshot_for_repair (manifests.py:127) re-hashes every file (16 threads) though
PREFLIGHT verified it; possibly _make_room + copy + re-hash; assert_processor_geometry;
lease flock (residency.py:107-138) + /health refused probe (manager.py:644);
launcher.launch (process.py:297); _wait_until_ready (manager.py:657-742) polls every
poll_interval_seconds = 2 (log tail scan, /health, /v1/models, READY probe); receipt,
audit, blobs (:450-478). Stop: __exit__ (client.py:408) -> stop (manager.py:579):
SIGTERM group + wait 10 s scanning /proc every 20 ms (process.py:241-257,331-362), then
SIGKILL + 10 s; _assert_endpoint_absent polls 0.25 s up to 10 s (:954-973); release.
No smoke between stages (only PREFLIGHT, preflight.py:150-235, one extra cold start per
role).

Witness A->B->C: live_pass loops sorted chairs one `with serving_factory` at a time
(3_attestatores/run.py:2519-2578): stop 3-20 s; re-hash 5-15 s each on NVMe (minutes on
FUSE); copy if evicted; ready Chandra ~220 s, DAI ~60 s, Churro ~210 s. ~9 of 14 min.
Witnesses->Perlector: stage exit; orchestrator stage_sync (run.py:1025-1032; three
hashes and two fsyncs per new file, sync.py:54-121) NOT in the timing journal (written
inside invoke at :491); Perlector re-verifies the predecessor seal (stage.py:3770-3778);
read_the_pages prepares every feed (page_run.py:1262) before the server starts lazily in
the first job (:434-435 -> live_calls.py:54-64): re-hash 51.8 GiB + ~375 s cold start.
Perlector->Coniector: service.close() before the seal by design (4_perlector/run.py:
118-127); Coniector chair starts lazily (4b/run.py:251-252,133-143); reconstructor cache
keyed by role (models.py:93) so copy 51.8 GiB (2-5 min) + hash + ~375 s cold start with
a different launch shape (2/4096 vs 4/8192) so no compile-cache hit (unverified); calls
serial (:404).

## Findings, ranked

- F1 keep the Perlector server for the Coniector (~10 min per chunk). Blocked today by:
  catalogue forbids two chairs sharing endpoint/served id (serving/config.py:833-852;
  ports 8106/8107, recipes 385-386, 446-447); ChairClient requires the receipt to name
  its role (client.py:362-389); Perlector stops before sealing; stage-per-subprocess
  (orchestrator run.py:483); lease fd inherited by the vLLM child (residency.py:35-41,
  manager.py:446) so a surviving server makes the next acquire fail. Design: row field
  shares_service_with = "perlector" (catalogue requires identical launch fields, exempts
  the pair from the collision rule); ServingManager.adopt(handle, identity) checks
  identities equal except role, argv digest equals the live launch audit (manager.py:
  801-803), process alive and /v1/models answers; publishes a new receipt for role
  reconstructor keeping started_at and endpoint with launch_purpose = "adopted". Process
  structure: run the Coniector pass inside the Perlector process when the selection
  covers both and rows share a service (both mains take serving_factory seams,
  4_perlector/run.py:97, 4b/run.py:379,399): Perlector seals, Coniector opens its own
  StageContext, adopts, reads, stops. Risk: seal written while the server is up
  (reverses close-before-seal, run.py:121-124); receipt says an adopted service served
  (one line to the lead). Fallback: item D + identical launch shapes (~4-7 min). Tests:
  test_manager.py 1579, 1606 + adopt tests; test_serving_catalogue_capacity.py:103;
  test_residency.py; test_coniector.py 301, 766, 878; test_live_perlector.py;
  orchestrator test_run_modes.py, test_page_read_run.py.
- F2 witnesses share the card with overlapped cold starts (~4-5 min loading + 1-3 min
  reading on 80/96 GB). Today: 80gb-plus rows give each witness 0.88 (recipes 172, 263,
  350); single exclusive flock (residency.py:126); tiers must be "single"
  (pod/preflight.py:350); live_pass one chair at a time. Weights 9.9+15.5+7.0 = 32.4 GiB
  fit together on 80/96; on 48 GB Chandra+Churro or DAI+Churro; 24 GB one at a time.
  Design: budgeted lease (one lock file per slot + fraction ledger under a guard lock),
  tier residency "budgeted", fractions from the derivation, staggered starts (launch B
  once A's log shows the "gpu kv cache size" marker, manager.py:1307), concurrent page
  windows. F2a lower risk: at most two resident, start the next witness while the
  current reads (hides DAI 60 s and most of Churro 210 s, ~3-4 min). Risks: StageContext
  thread-safety unverified (Chandra publishes intents while reading, run.py:2574-2576);
  vLLM memory profilers racing unverified. Tests: test_attestatores_live_pass.py 1397,
  1463; test_residency.py:116; test_manager.py 1579, 1606, 1874;
  test_preflight_assembly.py; test_serving_catalogue_capacity.py:313.
- F3 start the server while pages are prepared (min(prep, ~7 min) per 27B stage):
  live_calls.start_chair on a background thread once the pass is live with unsealed
  pages; join before the first window; adjust _refuse_past_phase_deadline (page_run.py:
  1228-1235). Coniector starts when plan["calls"] is non-empty.
- F4 full re-hash on every start (1-3 min NVMe, more on FUSE): belongs with item E;
  ledger (inode, size, mtime_ns, digest) in container-local /tmp written by the verifying
  copy; the hash currently warms the page cache, so a sequential read-ahead could
  replace it and overlap the previous stage.
- F5 head-of-line blocking in in_order_window (2-4 min of the Perlector's idle): same as
  review 4 F1.
- F6 derive concurrency from the card: 24 GB rows DAI/Churro max_num_seqs 1 with room for
  several sequences (DAI ~5 GB free at 0.90 / ~0.47 GB per 8192-token seq; Churro ~6 GB
  at ~1.1 GiB per seq -> 3-4); Perlector 4 at 0.88 for every card >= 64 GB (96 GB leaves
  ~11 GB; 141/180 GB stay at 4). Derived values must land in the launch audit.
- F7 volume sync between stages serial, unjournaled, unoverlapped (10+ min on FUSE
  hosts): journal its duration; run it on a background thread over a frozen file list,
  join before the next sync and at run end; start the next stage at once so the cold
  start overlaps the sync. A stage failure still waits for its sync.
- F8 readiness/stop polling: < 1 min per run; no change (poll_interval must be a
  positive integer, config.py:659).
- F9 GPU sampler: negligible overhead; blind to sync, bootstrap and gaps; add serving
  spans per chair (launch -> ready -> stop; manager.py:448,726,817,864) and sync
  duration to the timing journal so the watcher can tell loading from stalled.

## Scale to the card (design)

Detected today: SystemGpuProbe.profile (pod/preflight.py:194-263) -> GpuProfile
(vram_gib, compute_capability, gpu_count); PlacementTable.choose (:401) maps to three
bands (pod_placement.toml:44-87); receipt placement_tier forwarded as --placement-tier
(pod_run.py:1095-1113,2046); PREFLIGHT caps rows at engine_memory_fraction, context_cap,
batch_size (serving/preflight.py:236-261); Perlector concurrency from the row
(4_perlector/run.py:220-232), witnesses from the handle profile (:2577). Nothing reads
free memory, SM count or the KV pool vLLM allocated (log marker at manager.py:1307).

Design: the recipe row is the floor; PREFLIGHT derives a capacity plan from the measured
card and publishes it in its receipt beside placement_tier; pod_run forwards
--capacity-plan (unsealed runtime fact like the tier, stage.py:1419-1431); only capacity
knobs scale (max_num_seqs, max_num_batched_tokens, gpu_memory_utilization, co-residency
groups); reading-shaping fields stay fixed and profile_preflight_digest (config.py:527)
binds the floor. Inputs: weights W (registry.py:534), per-sequence KV P (full-attention
layers only for hybrids, cf. recipes 390-404), non-KV peak A; P and A best measured from
vLLM log lines at the PREFLIGHT smoke (strings unverified) with a config.json estimate
as fallback. Budget U*M with U = tier engine_memory_fraction (0.88 on A100 per decision
3; ~0.92 for >= 96 GB only after measurement). Group chairs per stage while
sum(W + A + n_floor*P) <= U*M (96/80 GB all three witnesses; 48 GB {Chandra, Churro}
then {DAI}; 24 GB one at a time); Perlector + reconstructor one group, one service (F1).
n = clamp(floor(share/P), row max_num_seqs, min(pages, adapter cap, 64));
gpu_memory_utilization = ceil((W+A+nP)/M + margin) in 0.01 steps;
max_num_batched_tokens = floor * min(2, M/80 GiB). Rough Perlector: 96 GB 5-6, 141 GB
~15, 180 GB ~24. ServingManager.start checks derived >= floor and the group sum, renders
argv from replace(profile, ...), audit records row and derived values plus plan digest;
the PREFLIGHT smoke runs at the derived shape. Code: PlacementTier residency check
(pod/preflight.py:350), caps (serving/preflight.py:250-257) become ceilings,
FileResidencyLease -> budgeted lease, _reading_concurrency reads the handle, live_pass
grouping, shared reconstructor service. Tests: test_serving_catalogue_capacity.py 227,
313 (becomes plan >= floor), test_preflight_assembly.py, test_residency.py,
test_manager.py 819, 1579, 1606.

Recommended: F1 first with F9's serving spans in the same PR so the next pod measures
PRO 6000 cold starts before F2 and the derivation are sized.
