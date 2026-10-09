# Bake-off runbook: one day, two pods, every arm, no idle card

For a Mac session with no memory of the preparation. Read `README.md` in this folder
first (the tool), then `operations/pod/README.md` "The hand route" (how a pod is started
with its guard armed). The lead approves every pod and every paid run; this file starts
nothing. Page names are private: every example here says `p001.tif`.

## 0. The day in one screen

| Step | Where | Time | Cost |
|---|---|---|---|
| 1. Merge the bake-off pull requests, pull `main`, `uv sync --frozen` | Mac | 20 min | 0 |
| 2. Copy the prepped pages and the two manifests to the volume | Mac → pod | 10 min | 0 |
| 3. Start the small pod (A40 48 GB or RTX 3090 24 GB), guard armed, queue launched | pod W | 5 min | starts billing |
| 4. The witness queue runs itself (table in section 2); the Mac runs `watch` | pod W | ~4–5 h | ~$2.50 |
| 5. Once the witness smoke runs are clean, start the big pod (RTX PRO 6000 96 GB) with the reader queue | pod R | ~2 h | ~$4 |
| 6. Each pod syncs its cache, verifies digests, pings, deletes itself | pods | — | billing stops |
| 7. `fetch` both caches home, confirm both pods are gone in the RunPod listing | Mac | 15 min | 0 |
| 8. Score and build the roster tables | Mac | 10 min | 0 |

Whole day: about $7 of pod time plus the volume. Nothing on the Mac has to notice the
end of a job: the queue runner does, and it pings the phone at each milestone.

## 1. Before renting (free, on the Mac)

```sh
cd ~/verbatus_alpha
git switch main && git pull
uv sync --frozen --group test --group audit
.venv/bin/python -m pytest -q -p xdist -n 4 operations/bakeoff
sh .githooks/check-static.sh
```

Then gather the pages flat, as the README says (73 prepped TIFs), and check the two
manifests against the paths you will use:

```sh
mkdir -p ~/bakeoff-pages && cp "$HOME/Desktop/Bake-off set/Pages"/*/Prepped/*.tif ~/bakeoff-pages/
ls ~/bakeoff-pages | wc -l        # 73
.venv/bin/python -m operations.bakeoff.queue_runner validate --manifest operations/bakeoff/queue/witness-24gb.toml
.venv/bin/python -m operations.bakeoff.queue_runner validate --manifest operations/bakeoff/queue/reader-96gb.toml
```

Private lists for the scorer (never in git): write `private/bakeoff/hard-pages.txt` and
`private/bakeoff/exclude.txt`, one page stem per line, from `handback/page-manifest.md`.

## 2. Card plan

Prices are RunPod Secure on-demand as seen 2026-10-08 (`config/pod_placement.toml`;
confirm in the console before renting): A40 48 GB $0.44/h, RTX 3090 24 GB about $0.50/h,
RTX PRO 6000 96 GB $1.99/h. The RTX 4000 Ada (20 GB) is below the project's 22 GiB floor.

**Pod W (witnesses), A40 or RTX 3090.** Every arm below fits 24 GB except where the VRAM
column says A40. Time boxes are planning figures from the 2026-10-08 measurements (A40:
Chandra 371 s and Churro 584 s wall for 73 pages at concurrency 64, DAI 75 s; server
starts 1–5 min; bootstrap 3 min with weights on the volume) plus the run cards' estimates
for the new arms. The runner never kills an arm on a clock; the cut rule says what is
skipped if the day runs over.

| order | arm | card | box (min) | VRAM | cut | basis |
|---|---|---|---|---|---|---|
| 1 | chandra | 24 GB ok | 15 | 10.6 GB weights | never | 371 s wall on the A40, 2026-10-08 |
| 2 | dai, dai-conf10, dai-whole | 24 GB ok | 10 + 10 + 15 | 15.5 GB weights | never | 75 s wall, 2026-10-08; detector on CPU |
| 3 | churro | 24 GB ok | 15 | 7.5 GB | never | 584 s wall, 2026-10-08 |
| 4 | chandra-native | 24 GB ok | 25 | 10.6 GB | never | our chandra plus the package's retry loop |
| 5 | churro-native | 24 GB ok | 20 | 7.5 GB | never | our churro, shorter context |
| 6 | dots-mocr | 24 GB ok | 25 | 6.1 GB | install-failed | no measurement; estimate 5-10 s/page |
| 7 | surya-rec-surya | 24 GB ok | 20 | 3-4 GB | behind-schedule | Datalab: 5 pages/s on a 5090; a VLM, not CTC |
| 8 | party-blla | 24 GB ok (est. < 8 GB) | 30 | not measured | overrun (cut first) | 98 s/page on 4 CPUs; GPU unmeasured |
| CPU, beside 1-8 | surya-lines, blla-lines, kraken-ppocrv6-{surya,blla}, pylaia-{belfort,popp}[-lm]-{surya,blla} | none | 10, 90, 30+30, 8 x 15 | 0 | never | kraken 2-3 s/line, blla 95-110 s/page, PyLaia ~10 s/page on 4 CPUs; installs kraken 279 s, PyLaia 204 s, Party 4-5 min cold |

GPU time boxes sum to about 3 h; the CPU arms run beside them and finish inside that (the
blla segmentation, 73 pages at about 100 s each on a few cores, is the long one: give it
8 threads). Expect 3.5-4.5 h for pod W. Everything fits 24 GB, so the choice between the
A40 and the 3090 is price and stock, not memory.

**Pod R (readers), RTX PRO 6000, last.** The three blind arms ran on 2026-10-08; tomorrow
adds the vendor arm per reader (`qwen-vendor`, see `cards/qwen3.*.md`). Bootstrap is
12 min when the 27B must be fetched, 3 min when it is on the volume; the 27B read 73
pages in 183–584 s on 2026-10-08.

| order | arm | box (min) | basis |
|---|---|---|---|
| 1 | qwen38-27b-vendor | 35 | the 27B read 73 pages in 183-584 s blind; the vendor preset samples at temperature 0.7 and may write longer |
| 2 | qwen35-27b-vendor | 40 | same size; a cold load from the volume adds 2-5 min |
| 3 | qwen35-9b-vendor | 20 | about a third of the 27B's time |
| 4-5 | qwen35-27b-blind, qwen35-9b-blind | 30 + 20 | only if the 2026-10-08 caches are not copied up; `cut = "overrun"` |

About 2 h including the 12-minute bootstrap when the Qwen3.5 snapshots must be fetched.

**Cut rules (plan 4.3):** Party is cut first (`cut = "overrun"`), the Surya recogniser at
the behind-schedule mark (`cut = "behind-schedule"`, 30 min behind the cumulative boxes),
dots.mocr only if its install fails (`cut = "install-failed"`); the three baselines and the
CTC line arms are never cut. The lead may set `hard_stop_min` in a manifest for the day;
off, nothing stops the queue but its own end.

## 3. Start pod W with its guard armed

Exactly as `operations/pod/README.md` "Create the pod with its guard armed", with the
witness card and the volume's datacenter. `<sha>` is the 40-character commit on `main`
after the merges. The session never runs a bare `runpodctl pod create`.

```sh
read -rs RUNPOD_API_KEY; export RUNPOD_API_KEY
START=$(sh operations/pod/pod_start_command.sh off <sha>) &&
runpodctl pod create \
  --name verbatus-bakeoff-w \
  --image runpod/pytorch:1.4.0-cu1300-torch2130-ubuntu2404 \
  --gpu-id "NVIDIA A40" --gpu-count 1 \
  --cloud-type SECURE \
  --data-center-ids EU-RO-1 \
  --network-volume-id kl8yg2jn8g \
  --volume-mount-path /workspace/private \
  --container-disk-in-gb 100 \
  --ports "22/tcp" \
  --docker-args "$START"
runpodctl pod get <pod id>          # SSH port and IP
```

Use `--gpu-id "NVIDIA GeForce RTX 3090"` for the 3090. `off` means no deadline: the
queue runner ends the pod; the guard's idle ladder (warn 15 min, urgent 30, back up 1 h,
delete at 2 h only with `ladder_delete = "on"`) stays as the backstop.

On the pod, over the direct SSH port (`ssh -p <port> root@<ip>`):

```sh
findmnt /workspace/private && tail -2 /workspace/private/.pod_guard/guard.log   # "armed for pod ..."
git clone https://github.com/MegaSlick/Apparatus-Verbatus-Alpha /opt/verbatus
cd /opt/verbatus && git checkout --detach <sha>
bash operations/pod/prepare_runtime.sh
UV_CACHE_DIR=/tmp/verbatus-uv-cache uv sync --frozen --group pod
mkdir -p /workspace/private/bakeoff
```

Stop here if there is no "armed for pod" line: delete the pod, confirm it is gone, find
out why before renting again.

Copy the pages and manifests up (from the Mac; the pages go to the volume, never to git):

```sh
scp -P <port> -r ~/bakeoff-pages root@<ip>:/workspace/private/bakeoff-pages
scp -P <port> operations/bakeoff/queue/witness-24gb.toml root@<ip>:/workspace/private/bakeoff/
```

The ntfy topic goes to the pod's environment over SSH stdin, never on a command line
(`read -rs NTFY_TOPIC; export NTFY_TOPIC` inside the SSH shell, pasted from
`private/ntfy.conf`); the queue runner reads it from the environment. The guard reads its
own copy from `/workspace/private/.pod_guard/ntfy_topic`.

## 4. Smoke, then launch the queue detached

Every arm runs two pages first inside the queue (`smoke_pages = 2`); a failed smoke stops
that arm, not the queue. Check the queue once by hand before leaving it alone:

```sh
cd /opt/verbatus
.venv/bin/python -m operations.bakeoff.queue_runner validate --manifest /workspace/private/bakeoff/witness-24gb.toml
.venv/bin/python -m operations.bakeoff.queue_runner run --dry-run --manifest /workspace/private/bakeoff/witness-24gb.toml
setsid nohup .venv/bin/python -m operations.bakeoff.queue_runner run \
  --manifest /workspace/private/bakeoff/witness-24gb.toml \
  > /workspace/private/bakeoff/queue-witness-24gb.log 2>&1 < /dev/null &
tail -f /workspace/private/bakeoff/witness-cache/events.jsonl    # until the first arm's smoke is done, then leave
```

## 5. What the Mac session does while the pod runs

Three things and nothing else on the pod:

1. Start the queue (section 4).
2. Run `watch` and read its lines; it exits when `DONE.json` appears:

   ```sh
   .venv/bin/python -m operations.bakeoff.queue_runner watch --ssh "ssh -p <port> root@<ip>" \
     --status /workspace/private/bakeoff/witness-cache/status.json
   ```

   Or follow the phone topic: `watch --ntfy --queue witness-24gb`.
3. Nothing else. No `pgrep`, no `nvidia-smi` loops, no second launch. The queue overlaps
   the next arm's install and preparation with the current arm, retries a failed arm once
   at the end, syncs, verifies, pings and deletes the pod.

Failure rules:

- **An arm errors:** the runner retries it once at the end of the queue, then reports it
  in `status.json` (`finished_arms`, `errors`) and the final ping. Fix at home; rerun that
  arm alone on the next pod (`witness_run run` resumes: cached pages are skipped).
- **No status update for 10 minutes** (`watch` prints a stale warning): inspect once over
  SSH (`tail /workspace/private/bakeoff/queue-witness-24gb.log`, `cat .../status.json`),
  then let the guard ladder decide. Do not restart things from the Mac.
- **The pod is gone but `DONE.json` is missing:** the guard's ladder or a crash ended it;
  the cache on the volume is whatever was synced per arm. `fetch` reports what verifies.
- **The queue ends with `state: failed`** (own-disk refusal or a sync mismatch): the pod is
  left up on purpose; fetch, then delete the pod by hand and confirm.

## 6. Pod R: the readers, last

Start when pod W's queue is past its smoke runs and running clean (the lead's call, see
`handback/open-questions.md` question 2). Same create line with
`--gpu-id "NVIDIA RTX PRO 6000 Blackwell Server Edition"`, `--container-disk-in-gb 120`
and, if no PRO 6000 is in EU-RO-1, a pod with its own disk (`mounts.persistent`) and
`own_disk = true` in the manifest, which makes the runner refuse to delete until the copy
home verified. The HF token goes over SSH stdin (`read -rs HF_TOKEN; export HF_TOKEN`) for
the fetch of the two Qwen3.5 snapshots, then `HF_HUB_OFFLINE=1` for the runs.

```sh
scp -P <port> operations/bakeoff/queue/reader-96gb.toml root@<ip>:/workspace/private/bakeoff/
# on the pod, after the clone and sync as in section 3:
# The 27B is on the volume at model-store/hf/qwen3.8-27B (the pipeline's store). The two
# Qwen3.5 snapshots are new: fetch them once at the cards' pinned commits, then go offline.
.venv/bin/python -c "from huggingface_hub import snapshot_download as s; \
  s('Qwen/Qwen3.5-27B', revision='fc05daec18b0a78c049392ed2e771dde82bdf654', local_dir='/workspace/private/model-store/hf/qwen3.5-27b'); \
  s('Qwen/Qwen3.5-9B', revision='c202236235762e1c871ad0ccb60c8ee5ba337b9a', local_dir='/workspace/private/model-store/hf/qwen3.5-9b')"
export HF_HUB_OFFLINE=1 VLLM_NO_USAGE_STATS=1 DO_NOT_TRACK=1
setsid nohup .venv/bin/python -m operations.bakeoff.queue_runner run \
  --manifest /workspace/private/bakeoff/reader-96gb.toml \
  > /workspace/private/bakeoff/queue-reader-96gb.log 2>&1 < /dev/null &
```

Then `watch` as in section 5 with `--status /workspace/private/bakeoff/reader-cache/status.json`.

## 7. What comes home, and where

```sh
.venv/bin/python -m operations.bakeoff.queue_runner fetch --ssh "ssh -p <port> root@<ip>" \
  --remote /workspace/private/bakeoff/witness-cache --into private/bakeoff/witness-cache-$(date +%F)
.venv/bin/python -m operations.bakeoff.queue_runner fetch --ssh "ssh -p <port> root@<ip>" \
  --remote /workspace/private/bakeoff/reader-cache --into private/bakeoff/reader-cache-$(date +%F)
```

If the pod is already gone (it ended itself), the verified copy is on the volume under
`<out>-home/`; fetch it with the S3 path (`verbatus fetch-run`-style keys) or a cheap CPU
pod, and verify against its `DONE.json`.

Confirm each pod is really gone: `runpodctl pod list --all` shows nothing, `runpodctl pod
get <pod id>` fails, and the RunPod console's billing has stopped. A shutdown is verified,
never assumed.

Layout on the Mac:

```
private/bakeoff/
  witness-cache-<date>/<arm>/<stem>.json     every arm's raw cache (+ _records/, _lines/)
  reader-cache-<date>/<arm>/<stem>.json
  scores-<date>/scores.md, scores.jsonl, roster.md, roster.jsonl
  page-manifest.json, page-manifest.md       from handback/
  hard-pages.txt, exclude.txt
  comparison-<date>.md                       the one-page table (template in section 9)
```

## 8. Scoring at home

```sh
.venv/bin/python -m operations.bakeoff.score --cache private/bakeoff/witness-cache-<date> \
  --gold "$HOME/Desktop/Bake-off set/Pages" --gold-glob '*/Prepped/*.txt' \
  --hard-pages private/bakeoff/hard-pages.txt --exclude private/bakeoff/exclude.txt \
  --out private/bakeoff/scores-<date>
.venv/bin/python -m operations.bakeoff.roster --cache private/bakeoff/witness-cache-<date> \
  --gold "$HOME/Desktop/Bake-off set/Pages" --gold-glob '*/Prepped/*.txt' \
  --baselines chandra,dai,churro --hard-pages private/bakeoff/hard-pages.txt \
  --out private/bakeoff/scores-<date>
```

Run the reader cache through `score` the same way (a second `--cache`). Every number is
"vs fool's gold (ballpark, not accuracy)" until the lead's gold exists.

## 9. The one-page comparison the session fills in

| arm | group scored | headline (group metric) | rescue rate | corr. with Chandra / DAI / Churro | s/page | VRAM | verdict |
|---|---|---|---|---|---|---|---|
| chandra | acts, index-list, … | | — | — | | | baseline |
| chandra-native | | | | | | | |
| dai / dai-conf10 / dai-whole | acts | act recall, unit CER | | | | | |
| churro / churro-native | | | | | | | |
| dots-mocr | | | | | | | |
| kraken-ppocrv6-blla / -surya | | | | | | | |
| pylaia-belfort-* / pylaia-popp-* | | | | | | | |
| party-blla | | | | | | | |
| surya-rec-surya | | | | | | | |
| qwen*-blind / qwen*-vendor | all (reference) | | | | | | reader |

Verdict words: `roster` (earns a 4th-witness arm on a type, plan 4.5), `keep as reference`,
`drop`, with the page type beside it. Test pages and hard pages are reported beside the
table, never inside it.
