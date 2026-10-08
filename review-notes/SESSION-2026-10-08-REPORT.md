# Report for the lead: 2026-10-08 pod-efficiency session

Short version: four pull requests take the measured idle time out of a pod run. Two are
merged, two are in review. Nothing has run on a pod yet; the next pod is the real test.
No pod was started this session.

## What changed, and what it saves (estimates from the 2026-10-07 numbers)

| PR | What | Saves per run |
|---|---|---|
| #287 merged | Chair cache keyed by model digest (Perlector and Coniector share one 55 GB copy); files hashed while copied; one verification per process; boot no longer hashes bytes the copy will hash; each pod prepares only its stages' chairs | ~45 min of setup on one card; the witness pod skips the 27B model, the big pod skips 35 GB of witnesses |
| #288 merged | Budgets off by default (`pod_budget = "off"`); a pod can start with no deadline; the 30-minute idle delete becomes warn 15 min, urgent 30 min, verified backup 1 h, delete 2 h only when `ladder_delete = "on"` (default off); zero hours refused; stale deadline cleared on restart | No more pods deleted by mistake; the guard still warns and backs up |
| #289 merged | In-order window no longer blocks on a slow head page; Coniector calls in the window at 4 wide; Perlector loads its model while feeds build; one transcript line per stage start/end/sync/model load; lighter GPU sampler; serving spans in the journal | ~5-8 min Perlector, ~1 min Coniector on 6 pages (8-12 on 50), up to ~7 min overlap on big chunks |
| #290 merged | Each page decoded once per process; witness images derived once per pass across all cores; Recensor measures once; Ink map in worker processes; Surya sized by usable cores and cgroup quota; run-tree sync keeps a ledger and copies in parallel | Measured on a 3500x5000 fixture: 160 s -> ~64 s on 4 CPUs (Recensor 36 -> 10 s, Archetypus 23 -> 8 s, Armarium 26 -> 9 s). On a 50-page chunk the review put the removable CPU time near 30-40 min |
| #291 merged | Pod guard judges a running stage by its pages against a planned rate (`ok`/`slow`/`stalled`); keep-alive through bootstrap and final sync; `backup-<pod>` list written so the 1 h backup copies something; `slow` and `bootstrapping` never reach the delete rung; CUDA probed in a child with a 120 s timeout before setup; store lock waits 20 min | No more working pods deleted; a bad CUDA host is refused in 2 min instead of after a 45 min setup |
| #292 merged | Bootstrap copies the selected chairs into the cache while uv syncs; one copy pool across all chairs, largest file first; PREFLIGHT copies the next chair while the current one smokes, in stage order; cache repair moves files by rename; a stuck copy or prefetch fails its step with a named error instead of holding the pod | ~6 min of setup on a big-card pod plus the next chair's copy hidden behind each smoke (estimates; measure `prefill.seconds` on the next pod) |
| #293 open | DAI records go through the window one by one; DAI, Churro and reconstructor rows 4 -> 8 on 80 GB+; a Perlector reply is bounded by its page's reserve x headroom 2.0 (floor 4,096); re-asks sent as soon as a page's first reading is published | Attestatores batching several-fold on multi-record pages; each run-away reply ~3-5 min shorter; no idle gap before re-asks (estimates) |

The five review reports with file:line anchors are in `review-notes/2026-10-08-reviews/`.

## Your decisions, applied

- Budgets and automatic deletion are off by default; the ladder stays as the crash net
  with its delete step behind `ladder_delete`. Turn either on in `config/spend.toml`.
- Two-card split: a pod prepares and verifies only the chairs its stages need.
- RecordGold smoke image: unchanged from #280 (already authorised).

## Decisions still yours

1. `ladder_delete` default: off (as built). Turn on when you trust the backup step.
2. The MODEL_STORE receipt now says "bytes verified at copy, for roles ..." instead of
   implying every store byte was hashed at boot (hand-off item E; built as engineering).
3. Perlector `max_tokens` from the page reserve (review 4, F2): trades held pages for
   time; needs a headroom number from the 10-07 call records. Not built.
4. Chandra at width > 1 and wider 24 GB witness rows (review 4, F3/F4): throughput only,
   but changes batch composition; wants one cheap-pod KV check. Not built.
5. Keeping the Perlector server alive for the Coniector (review 2, F1, ~10 min per chunk)
   and witnesses sharing a big card (F2): both need a serving-layer change and one line
   of evidence wording. Not built.
6. Two integrity relaxations the builders declined: a per-stage verified-input set for
   `publish`, and skipping input checks in the hard-failure tally for sealed stages.

## What the next pod must check

- Time to green PREFLIGHT and the MODEL_STORE / CHAIR_CACHE receipts (one copy under
  `by-digest/`, `copied_files`, worker count).
- The Coniector no longer copies weights; its row now launches at 4 / 8192.
- Transcript shows stage lines; `guard.log` and `alert-<pod>` show the ladder; no deadline
  file unless you wrote one.
- Surya runner count equals usable cores // 8; sync ledger `.verbatus-sync-ledger.jsonl`
  in the local run tree; final sync near-instant.
- Per-stage GPU use and serving spans in `-timings.json`.

## Recommended next step

One cheap pod (witness card) through Attestatores, then the big card from Perlector to
Coniector, on the 47 bake-off pages, timed from the journal. Then build review 2's F1
(shared Perlector/Coniector server) and the progress-rate watcher (review 5, PR 2).
