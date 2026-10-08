# Report for the lead: 2026-10-08 pod-efficiency session

Short version: nine pull requests take the measured idle time out of a pod run and stop
the guard deleting working pods. Six are merged (#287-#292), one is in review (#293) and
two are queued to open (shared 27B server; card capacity plan). Nothing has run on a pod
yet; the next pod is the real test. No pod was started this session.

## What changed, and what it saves (estimates from the 2026-10-07 numbers)

| PR | What | Saves per run |
|---|---|---|
| #287 merged | Chair cache keyed by model digest (Perlector and Coniector share one 55 GB copy); files hashed while copied; one verification per process; boot no longer hashes bytes the copy will hash; each pod prepares only its stages' chairs | ~45 min of setup on one card; the witness pod skips the 27B model, the big pod skips 35 GB of witnesses |
| #288 merged | Budgets off by default (`pod_budget = "off"`); a pod can start with no deadline; the 30-minute idle delete becomes warn 15 min, urgent 30 min, verified backup 1 h, delete 2 h only when `ladder_delete = "on"` (default off); zero hours refused; stale deadline cleared on restart | No more pods deleted by mistake; the guard still warns and backs up |
| #289 merged | In-order window no longer blocks on a slow head page; Coniector calls in the window at 4 wide; Perlector loads its model while feeds build; one transcript line per stage start/end/sync/model load; lighter GPU sampler; serving spans in the journal | ~5-8 min Perlector, ~1 min Coniector on 6 pages (8-12 on 50), up to ~7 min overlap on big chunks |
| #290 merged | Each page decoded once per process; witness images derived once per pass across all cores; Recensor measures once; Ink map in worker processes; Surya sized by usable cores and cgroup quota; run-tree sync keeps a ledger and copies in parallel | Measured on a 3500x5000 fixture: 160 s -> ~64 s on 4 CPUs (Recensor 36 -> 10 s, Archetypus 23 -> 8 s, Armarium 26 -> 9 s). On a 50-page chunk the review put the removable CPU time near 30-40 min |
| #291 merged | Pod guard judges a running stage by its pages against a planned rate (`ok`/`slow`/`stalled`); keep-alive through bootstrap and final sync; `backup-<pod>` list written so the 1 h backup copies something; `slow` and `bootstrapping` never reach the delete rung; CUDA probed in a child with a 120 s timeout before setup; store lock waits 20 min | No more working pods deleted; a bad CUDA host is refused in 2 min instead of after a 45 min setup |
| #292 merged | Bootstrap copies the selected chairs into the cache while uv syncs; one copy pool across all chairs, largest file first; PREFLIGHT copies the next chair while the current one smokes, in stage order; cache repair moves files by rename; a stuck copy or prefetch fails its step with a named error instead of holding the pod | ~6 min of setup on a big-card pod plus the next chair's copy hidden behind each smoke (estimates; measure `prefill.seconds` on the next pod) |
| #293 merged | DAI records go through the window one by one; DAI, Churro and reconstructor rows 4 -> 8 on 80 GB+; a Perlector reply is bounded by its page's reserve x headroom 2.0 (floor 4,096); re-asks sent as soon as a page's first reading is published | Attestatores batching several-fold on multi-record pages; each run-away reply ~3-5 min shorter; no idle gap before re-asks (estimates) |
| #294 open | The Coniector takes over the Perlector's running 27B (hand-off record, full identity checks, cold-start fallback); Coniector chair starts while calls are drawn; volume sync overlaps the next stage; reconstructor row back to 4 to match the live server | Most of the ~10 min per chunk of 27B reload; several minutes per stage boundary on FUSE hosts (estimates) |

The five review reports with file:line anchors are in `review-notes/2026-10-08-reviews/`.

## Your decisions, applied

- Budgets and automatic deletion are off by default; the ladder stays as the crash net
  with its delete step behind `ladder_delete`. Turn either on in `config/spend.toml`.
- Two-card split: a pod prepares and verifies only the chairs its stages need.
- RecordGold smoke image: unchanged from #280 (already authorised).

## Decisions still yours

1. `ladder_delete` default: off (as built). Turn on when you trust the backup step. A
   `slow` or `bootstrapping` pod is never deleted, only a `stalled` one (#291).
2. The MODEL_STORE receipt now says "bytes verified at copy, for roles ..." instead of
   implying every store byte was hashed at boot (hand-off item E; built as engineering).
3. Perlector reply bound (#293): headroom 2.0 x the page's reserve, floor 4,096 tokens.
   Set wide because the 10-07 notes have no completion-token ratios. A reply at the bound
   is held unread, never guessed. Recommended: keep for the next pod, then tune from its
   `completion_tokens`.
4. Widths (#293): DAI, Churro and the reconstructor at 8 on 80 GB+. The Perlector stays at
   4 (8 full pages need ~32 GiB), Chandra stays at 4 (no KV figure), 24/48 GB rows
   unchanged pending one cheap-pod KV check.
5. Shared 27B server (queued PR): the Perlector now seals while its server is still up,
   the Coniector's receipt says an "adopted" service served it, and the volume sync trails
   by one stage (a stage's files reach the volume by the end of the next stage). All three
   are honest receipts of what happened; recommended: accept. Witness overlap (F2a) is not
   built: every 80 GB+ witness row asks 0.88 of the card, so nothing pairs until fractions
   are measured.
6. Card capacity plan (queued PR): widths derived from measured VRAM with the row as the
   floor and a 4 GiB engine-overhead guess (unmeasured). Planned on 80 GB: DAI 64, Churro
   54, Perlector 4; on 96 GB the Perlector 7; on 141 GB the Perlector 17. Recommended: one
   cheap 24 GB PREFLIGHT run first to replace the 4 GiB guess with the vLLM KV-pool line,
   and decide whether 64 is an acceptable ceiling.
7. One 120 GB pod running witnesses, Perlector and Coniector together still re-copies
   ~6 min of cache (#292 notes). Either ~160 GB disk (costs money) or keep witnesses off
   the big card (the two-card split; recommended).
8. Two integrity relaxations the builders declined: a per-stage verified-input set for
   `publish`, and skipping input checks in the hard-failure tally for sealed stages.

## What the next pod must check

- Time to green PREFLIGHT and the MODEL_STORE / CHAIR_CACHE receipts (one copy under
  `by-digest/`, `copied_files`, worker count).
- The Coniector no longer copies weights; its row now launches at 4 / 8192.
- Transcript shows stage lines; `guard.log` and `alert-<pod>` show the ladder; no deadline
  file unless you wrote one.
- Surya runner count equals usable cores // 8; sync ledger `.verbatus-sync-ledger.jsonl`
  in the local run tree; final sync near-instant.
- Per-stage GPU use and serving spans in `-timings.json`; sync durations in the journal.
- `pod-run-progress.json` and `.pod_guard/progress-<pod>` show `ok` while pages arrive;
  `backup-<pod>` lists the run trees; a bad CUDA host is refused within ~2 min.
- CHAIR_CACHE `prefill` record (chairs filled during uv sync, seconds, pool size).
- Perlector `answer_reserve` carries headroom and floor; count pages held as cut off.
- With the shared server: one 27B launch per run; the Coniector's receipt says adopted.
- With the capacity plan: PREFLIGHT receipt carries `vram_gib` and `capacity_plan`; the
  launch audit's `capacity` block shows row and launched widths.

## Recommended next step

One cheap pod (witness card) through Attestatores, then the big card from Perlector to
Coniector, on the 47 bake-off pages, timed from the journal, comparing against the 45 min
setup and 82 min pipeline of 2026-10-07. Read the PREFLIGHT smoke's vLLM KV-pool line on
each card to replace the capacity plan's 4 GiB guess before trusting the wide widths.
