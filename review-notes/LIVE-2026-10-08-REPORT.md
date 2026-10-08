# Live pod test, 2026-10-08: report

Engineering only: no register text, images or personal data. Evidence (run tree, reports,
transcripts, guard logs) is in the lead's `private/live-2026-10-08/` and, whole, as
`prep73-2026-10-08-final.tar` on network volume `kl8yg2jn8g` (EU-RO-1). Code at
`74599570ac` (PR #297) for every pod.

## Outcome

The two-card split ran end to end on 73 prepared bake-off pages (fool's gold: ballpark
timing only, never accuracy), run `prep73-2026-10-08`:

- Witness pod (RTX 4090, EU-RO-1): Door to Attestatores, exit 8, pod released by its guard.
- Big pod (RTX PRO 6000, US-NE-1, own disk): Perlector, then Coniector. The Coniector
  failed at shutdown (hand-off lock bug, below); rerun alone it sealed, exit 8.
- CPU pod (16 vCPU, EU-RO-1): Recensor ended **held, systemic** (73 of 73 pages held,
  186 items), as on 2026-10-07, so Archetypus and Armarium did not run.

No pod is left. Spend: $3.53 billed by 17:00 UTC with the last hour still lagging;
estimate about $5.50 for the day, plus the volume.

## Timeline and timings (UTC)

| Step | Time |
|---|---|
| Witness pod bootstrap, models already on the volume | 3 min (14:34 to 14:37) |
| Door / Exemplar / Ink map | 18 s / 3 s / 41 s |
| Designator (CPU, card idle) | 413 s, 5.7 s a page (11.5 s a page on whole spreads) |
| Attestatores (three witnesses, one 24 GB card) | 3070 s (51 min) |
| Hand-off: 7,323 files volume to big pod via a CPU relay | 6 min (network disk at both ends) |
| Big pod bootstrap: 27B download 52 GB in 3 min (~285 MB/s), then cache copy and smoke | 12 min |
| Perlector, 73 pages, one 27B launch, ready in 53 s | 1437 s (24 min), ~3 pages a minute |
| Coniector (failed at close) / rerun alone | 593 s / 551 s |
| Hand-off: big pod to CPU pod, 8,293 files | 5 min |
| Recensor (CPU pod, load ~2 of 16 cores) | 521 s |
| Whole run tree as one tar onto the volume | 14 s for 4.2 GB |

## Checks from the checklist

Verified:
- Guard armed within a minute on every GPU pod (`deadline none (off)`; 4 h on the
  own-disk pod); witness pod released and deleted by its guard after exit 8.
- PREFLIGHT `capacity_plan` with measured `vram_gib` (23.99 on the 4090, 95.59 on the PRO
  6000); Perlector planned at 7 on 96 GB, as designed.
- Measured vLLM KV pools (replace the 4 GiB overhead guess in `operations/serving/capacity.py`):

  | Chair (card) | Available KV | Concurrency at max length |
  |---|---|---|
  | attestator_1 Chandra (24 GB) | 10.49 GiB | 17.1x at 18,000 tokens |
  | attestator_2 DAI (24 GB) | 2.84 GiB | 6.5x at 8,192 (planned 4) |
  | attestator_3 Churro (24 GB) | 1.65 GiB | 1.46x at 32,768 (planned 2) |
  | perlector 27B (96 GB) | 30.74 GiB | 7.38x at 65,536 (planned 7) |

- One 27B launch for the Perlector; Coniector **adopted** the running server (no reload).
- CUDA probe passed on a driver-595.91.07 host (the 10-07 faulty host type); torch matmul OK.
- Per-stage volume syncs succeeded on the witness pod.

Not verified / not run: Archetypus, Armarium, export and backup (run held); Surya runner
count and DAI RecordGold CER (not read yet from the receipts at home); GPU use per stage
from `-timings.json` (at home, not yet analysed).

## Failures and fixes

1. **Witness pod disk full (60 GB).** Chair cache 33 GB + uv cache 12 GB + venv 8.4 GB +
   run tree + image. Fixed: PR #298 (100 GB default, merged).
2. **Coniector hand-off lock.** After adopting the Perlector's server, the Coniector's
   close stopped the service and then found the single-resident lease
   `/tmp/verbatus-pod-gpu.lock` still held by the original serving manager:
   `ServiceStopError` in `operations/serving/manager.py` `_prove_lease_free`, stage exit 1,
   unsealed after 98 reconstructions. To fix on a branch: the adopting process must not
   prove the lease free while the launching manager (pod_run's parent) still holds it, or
   the lease must transfer with the hand-off record (PR #294).
3. **Second pod refused at CUDA_COMPAT** reading the first pod's journal for the same run
   id ("GPU or driver differs from the completed CUDA compatibility receipt"). Worked
   around with a per-pod journal name. To fix: key the bootstrap journal by pod id.
4. **Chandra one page at a time** (KV use 3-6%, ~2 min a page on spreads). Fixed: PR #299
   (parallel window, records sealed in page order, merged); unmeasured on a card.
5. **Whole spreads were the wrong input.** The first attempt fed the 47 unsplit originals;
   DAI's detector found no records on most and Chandra was slow. Switched to the 73
   prepared pages (identical to the lead's `Bake-off set/Pages/*/Prepped`).
6. **rsync onto the own-disk pod's FUSE mount** fails `chown` (exit 23) and stopped the
   copy script half way; use `rsync -rt`.

## What the held run says (for the lead)

Perlector page readings: 13 read; 13 held as cut off at the reply bound (headroom 2.0 x
reserve, floor 4,096); about 45 held for `detection-range` findings (the reading's cited
line ranges do not fit the Designator's); 2 malformed (a continuation flag away from a
page edge). DAI read empty on many pages (its detector found no record); the lead rules
that this is expected input to the Perlector. Recensor then held every page.

## Idle and waste

- First witness pod lost to the full disk (~$0.62); the spread run on the second pod
  (~20 min) was stopped for the prepared pages.
- The 4090 idles through the Designator (7 min on 73 pages; Surya on CPU).
- Big pod waited ~13 min for an SSH host-key mix-up on the session's side and ~10 min for
  the witness stage to end; 6 min more for the FUSE-to-FUSE hand-off.
- The 16-vCPU CPU pod ran at load ~2: 4 vCPU would do for this tail.

## Decided today (lead)

- Weights download straight into the local chair cache; copying to network or global
  storage runs as a separate process. Measured: download 3 of the 12 setup minutes.
- A read-only Hugging Face token now exists on the Mac, for pods (sent over SSH stdin).
- Global volumes: console-only today; the REST, GraphQL and MCP create calls all refused a
  global volume id ("Network volume not found"). Revisit when RunPod exposes it.
- Cheaper witness card for next time: RTX 3090 (24 GB, 32 vCPU, $0.50/h); the RTX 4000
  Ada (20 GB) is below the 22 GiB tier floor.

## Recommended next step

Fix the Coniector hand-off lock (item 2), then look at the `detection-range` holds and the
reply bound with the lead before the next paid run: they, not throughput, decide whether a
run reaches the Armarium.
