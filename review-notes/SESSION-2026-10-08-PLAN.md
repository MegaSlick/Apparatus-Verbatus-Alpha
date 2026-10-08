# Plan for the 2026-10-08 cloud session: pod efficiency review

Goal (the lead's words): limit idle time on the pod, use every core and the GPU whenever
possible on whatever card the run lands on, remove repeated checks and reads, and stay
stable. Input: `review-notes/LIVE-2026-10-07-TODO.md` (the live session's hand-off).
Nothing here launches a pod; everything is tested with fakes and on this CPU box, and
the next live pod is the real test.

## Working method

- A reviewing subagent (Opus) per area reads the code and the live measurements and
  reports findings with file:line anchors; the session turns findings into small PRs.
- One branch and PR per topic, each with the test file nearest the change run locally
  and CI for the rest. Commit after every green test run so a hit limit loses nothing.
- Branches are `work/<topic>`; this session's own branch carries these notes.

## Phase 1: review (parallel subagents, read only)

1. **Bootstrap and preflight** (`operations/pod/bootstrap_main.py`, `preflight.py`,
   `common/chairs/model_store.py`, `registry.py`): serial steps that could overlap
   (uv sync, model pull, GPU probe), the boot-time full store hash, per-role verify
   repeated by PREFLIGHT and again by each stage, chair caches keyed by role so the
   Coniector re-copies the Perlector's weights.
2. **Model loading and unloading** (`operations/serving/manager.py`, `residency.py`,
   orchestrator stage boundaries): the gap between one stage's last call and the next
   stage's first call; two-model residency on a 96 GB card (decision 3); evicting caches
   no later stage needs; warm start of the Perlector while witnesses finish.
3. **CPU stages** (`1_exemplar`, `1_ink_map`, `2_designator`, `5_recensor`): single-core
   page loops, Surya slices, the Recensor's manifest rebuild and repeated reads of the
   same records, small-file hand-offs on FUSE disks.
4. **GPU stages** (`3_attestatores`, `4_perlector`, `4b_coniector`): concurrency against
   `max_num_seqs`, serial Coniector calls, `max_tokens` from the page's reserve,
   witness batching.
5. **Guard, spend and monitoring** (`pod_guard.sh`, `pod_start_command.sh`,
   `config/spend.toml`, `spend.py`, `pod_run.py` liveness): "no deadline" support, the
   idle/stall ladder (15 min warn, 30 min urgent, 1 h back up, 2 h delete), rate-based
   "working" checks per stage, one line per stage in the transcript.

Each report ranks findings by minutes saved per run on the 2026-10-07 numbers.

## Phase 2: build (ordered by minutes saved, cheapest first)

Expected order; the review may reorder it:

- E  Hash while copying, verify once; drop the boot-time full `verify_store` hash
     (~45 min of setup on 2026-10-07 was dominated by this).
- D  Chair cache keyed by `digest_manifest`, so the Coniector reuses the Perlector copy
     (12 min and 52 GB per run).
- C8 Evict caches no later selected stage needs; C3 preflight verifies in a thread pool
     and smokes only the selected roles.
- Recensor: no quadratic manifest rebuild; read each record once per pass (24 min at 0 % GPU).
- C7 Ink map and Exemplar page loops in a process pool, publishing in page order.
- I  Coniector calls through `in_order_window` of width `max_num_seqs`.
- J  Overlap uv sync, model pull and the GPU probe in bootstrap.
- Perlector `max_tokens` sized from the page's reserve; one transcript line per stage.
- Decision 5: `spend.toml` and `pod_start_command.sh` accept "no deadline"; guard ladder
  with rate checks replacing the single 30-minute idle delete.
- Two-pod hand-off file and `--submission-folder`; `--no-hold` default for streamed pods.
- CUDA test at pod launch; `/workspace-global` detection and `StorageLayout`.

Not this session unless time remains: the pod-side supervisor and chunk scheduler on a
CPU pod (decision 6), Surya on GPU (declined), decoding changes (lead's call).

## Phase 3: report

A short note for the lead (phone-length): what merged, what each saves on the
2026-10-07 numbers, what the next live pod must check, and one recommended next step.
