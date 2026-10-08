# Next live pod test: checklist and hand-off

Written 2026-10-08 by a cloud session for the local session that runs the next live pod
test. Engineering only: no register text, images or personal data. AGENTS.md governs;
`operations/pod/README.md` is the runbook and `LIVE_READINESS.md` the first-run guide.
This page says **what to do, in what order, and what to check**; the runbook has the
full commands.

## Where things stand

- PRs #287-#295 (pod efficiency) are merged. None has run on a pod yet: this test is
  their first real check.
- A review before this test found and fixed (PR "Next-pod readiness", this branch):
  - the per-stage volume sync failed whenever the Perlector handed its running 27B
    server to the Coniector (the server's log kept growing under the copy), which also
    left the pod unreleased; per-stage syncs now skip `serving-logs/`, the final sync
    copies them once the server is stopped;
  - a relaunch of the same run id wrote the sync ledger onto the volume and
    `verbatus fetch-run` then refused the whole run; the hydrate ledger now stays on
    local disk and fetch-run sets the sync's own files aside;
  - the chair-cache prefill could turn a slow volume into a red MODEL_STORE; its
    deadline now assumes a tenth of the measured copy rate (floor 15 min);
  - `LIVE_READINESS.md` step 7 could not launch with the committed budget off; it now
    matches the runbook (`VERBATUS_HARD_DEADLINE=none` with `--no-hold`), describes the
    pod split, and arms the ping over the pod's direct SSH port only.
- Known and deferred (throughput only, not failures): each stage process re-hashes its
  chair once; Chandra reads one page at a time; witnesses do not share a card; no
  GPU/CPU/disk warn checks beyond page rates; no per-stage hand-off file between pods.

## The lead's decisions before anything bills

Ask these in the session, early, each with the recommendation:

1. **Spend approval** for this test, with limits. Recommended: one RTX A5000 (24 GB,
   $0.27/h) for up to 2 h, then one RTX PRO 6000 (96 GB, $1.99/h) for up to 3 h, plus the
   volume's hourly price. Roughly $6-7 in all if both run to their limit.
2. **Deadline or none.** Budget and auto-delete are off as committed
   (`config/spend.toml`: `pod_budget = "off"`, `ladder_delete = "off"`). Recommended:
   start each pod with `off` and launch with `--no-hold`, so the guard deletes it within
   a minute of its run ending; the session watches the idle ladder (warn 15 min, urgent
   30 min, backup 1 h) and deletes by hand on a stall. A pod on its own disk
   (`mounts.persistent`, used when the PRO 6000 has no stock in the volume's datacenter)
   must instead get a number of hours and **no** `--no-hold`, and is copied home before
   it is deleted.
3. **Pages.** Recommended: the 47 bake-off pages (ballpark timing only, never called
   accuracy), same run id across both pods.
4. Carry over unchanged unless the lead says otherwise: Perlector reply bound (headroom
   2.0 x reserve, floor 4,096), DAI/Churro/reconstructor width 8 on 80 GB+, capacity
   ceilings in `config/pod_placement.toml`, the two-card split.

## Order of work

### 0. Free, on the Mac (no pod)

- [ ] `git pull` on `main`; confirm the "Next-pod readiness" PR is merged; record the full
      `origin/main` sha as `<sha>`.
- [ ] Run the nearest tests only, niced (the Mac is shared): `nice -n 20 .venv/bin/python
      -m pytest operations/pod/test_run_tree_sync.py -q`. CI has run the rest.
- [ ] `runpodctl version` is current (v1.14.3 has no `pod` subcommand); else use the API.
- [ ] RunPod balance above $50 plus the approved spend (console, Billing).
- [ ] `verbatus spend show` prints the budget off.
- [ ] The network volume holds `submission/` and `submission-manifest.json` for the
      pages, `model-store/` from earlier pods, and `.pod_guard/ntfy_topic` if armed before.
- [ ] Pick the run id; write it down with `<sha>`.

### 1. Witness pod: Door to Attestatores (bills, ~$0.27/h)

- [ ] Create: RTX A5000, `--container-disk-in-gb 100` (60 filled up on 2026-10-08), same datacenter as the volume,
      `--docker-args "$(sh operations/pod/pod_start_command.sh off <sha>)"`.
- [ ] Within 5 min, over SSH: `findmnt /workspace/private`; `tail
      /workspace/private/.pod_guard/guard.log` shows `armed for pod <id>: deadline none
      (off)`; `echo $RUNPOD_POD_ID`. **No armed line: delete the pod and stop.**
- [ ] Note `echo $RUNPOD_PUBLIC_IP $RUNPOD_TCP_PORT_22`; use only that direct port for
      `ssh` commands, `scp` and arming the ping (never `ssh.runpod.io`).
- [ ] Clone, checkout `<sha>`, `prepare_runtime.sh`, `uv sync --frozen` (runbook, "On the
      pod, over SSH").
- [ ] Launch detached as in the runbook with `VERBATUS_HARD_DEADLINE=none`, `--no-hold`,
      `--notify`, `--hourly-usd`, the two real config files, and **`--models small`**.
- [ ] Watch every few minutes: `pod-run-report-<run>.json`, `-progress.json`,
      `-transcript.log`, `.pod_guard/guard.log`. Progress `ok` while pages arrive.
- [ ] Check while it runs (record each as verified / not seen):
  - bootstrap: CUDA probe passes in under 2 min; CHAIR_CACHE `prefill` record (chairs,
    seconds, pool size); only the witness chairs copied (no 27B);
  - PREFLIGHT receipt carries `vram_gib` and `capacity_plan`; DAI's RecordGold CER in
    its smoke receipt; **the vLLM KV-pool line in each smoke log** (this replaces the
    capacity plan's 4 GiB overhead guess); any OOM at the planned width means lowering
    `planned_batch_ceiling` for `generic-24gb`;
  - Surya runner count equals usable cores // 8 and its seconds per page;
  - one transcript line per stage start/end/sync; `volume sync after <stage> copied ...`.
- [ ] Ends with `exit_code` 8 (`selection-complete`). The guard log reads `requested
      deletion ... pod_run reported: run <id> ended ...`.
- [ ] Teardown check (section 4) before starting the big card.

**Stop rules.** A red bootstrap step (exit 5) or a refused run (exit 2): read the report,
delete the pod, fix on a branch, do not retry on the same pod. `stalled` in progress, or
GPU idle above 10 min in a GPU stage: tell the lead, back up, delete.

### 2. Big card: Perlector to Coniector (bills, ~$1.99/h)

- [ ] Create: RTX PRO 6000 Blackwell Server Edition, `--container-disk-in-gb 120`, the
      `cu1300` image, `pod_start_command.sh off <sha>`. No stock in the volume's
      datacenter: ask the lead before a `mounts.persistent` pod (see decision 2).
- [ ] Same SSH checks, checkout and sync as step 1.
- [ ] Launch with the **same run id, submission folder and `--store-root`**, and
      **`--from perlector --to coniector`** (not `--models big`, which runs on to the
      Armarium). `pod_run` checks the sealed Attestatores on the volume first.
- [ ] Check while it runs:
  - only the 27B chair copied, one copy under `by-digest/`; MODEL_STORE receipt says
    "bytes verified at copy";
  - PREFLIGHT `capacity_plan` plans the Perlector at 7 on 96 GB; launch audit's
    `capacity` block shows row and launched widths; the KV-pool line again;
  - one 27B launch for the whole run; the Coniector's receipt says **adopted** and it
    copies no weights;
  - Perlector `answer_reserve` carries headroom and floor; count pages held as cut off;
  - `volume sync after perlector` succeeds (the fixed bug) and the final sync is quick;
  - GPU use per stage and serving spans in `-timings.json`.
- [ ] Ends with exit 8; guard deletes the pod. Teardown check (section 4).

### 3. The Mac: Recensor to Armarium (free)

- [ ] `verbatus fetch-run` with the evidence keys in `LIVE_READINESS.md` step 11
      (includes `-progress.json`). It must not refuse; if it names a
      `.verbatus-sync-*` path, the fix did not reach the pod's commit.
- [ ] Orchestrator `--from recensor --to armarium` with the two real config files
      (`operations/operator/README.md`). Held pages: `verbatus review`, then the lead decides.
- [ ] `verbatus export`, `verbatus backup`.

### 4. Teardown, after each pod

- [ ] `runpodctl pod list --all` does not list it; `runpodctl pod get <id>` says not found.
- [ ] If the report has `guard_release.guard_alive: false`, delete by hand now.
- [ ] RunPod console, Billing: the pod's charges stop. Until billing shows it, the
      shutdown is **unverified**, not done. The volume keeps billing by design.

### 5. Afterwards (free)

- [ ] Timings from the journals against 2026-10-07 (45 min setup, 82 min pipeline for 6
      pages on one PRO 6000; GPU busy ~30%): setup, each stage, sync gaps, GPU use per
      stage, cost per page.
- [ ] Replace the 4 GiB overhead guess (`operations/serving/capacity.py`) with the
      measured KV-pool figures, on a branch.
- [ ] Tune the Perlector reply bound from the run's `completion_tokens`.
- [ ] Write `review-notes/LIVE-<date>-REPORT.md`: outcome first, each check above as
      verified / unverified / not run, one recommended next step.
