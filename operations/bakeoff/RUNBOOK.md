# Bake-off runbook: one day, two pods, every arm, no idle card

For a session with no memory of the preparation. Do the steps in order. Each step says
what to run, what you should see, and what to do if you see something else. When a step
has no "if" line for what you see, stop and tell the lead what you saw; do not improvise
on a paid pod.

Background, read once: `README.md` in this folder (the tool) and `operations/pod/README.md`
"The hand route" (why every pod starts with its guard armed). Page names are private:
never put one in git, a PR, a commit message or a public comment.

## 0. Decisions the lead has already made (2026-10-08) — do not ask again

| Question | Answer |
|---|---|
| Small card (pod W) | A40 48 GB; if none is free, RTX 3090 24 GB. Either with at least 16 vCPU (the CPU line arms need them; see 2.0). |
| Big card (pod R) | RTX PRO 6000 96 GB. |
| Where | Any Secure datacenter that has the cards in stock on the day. No region is fixed. A fresh network volume is made there (step 2.0); the old EU-RO-1 volume is gone. |
| Models | Every arm downloads its own weights on boot at pinned versions (the queue's `prepare` steps). Nothing has to be on the volume beforehand. |
| Spend | Standing approval up to **$15** for the whole day, both pods, one retry included. |
| When pod R starts | As soon as pod W's smoke runs pass (step 4.3), without waiting for pod W to finish. |
| Paid runs | All approved: DAI arm twice more, `qwen-vendor` per reader, the CPU line arms beside the GPU. |
| Pages | All 73 are real research pages and are all scored. None is a test page, none is excluded. Hard pages count in every number and also get their own table. |
| Models | Try every reasonable candidate. HunyuanOCR is dropped (licence excludes the EU/UK). dots.mocr and the Surya recogniser run for the bench. |

**Ask the lead only when:** the day would pass $15; none of the cards in this runbook is
free anywhere; something would delete data or a volume; or a step here says "stop".

## 1. On the Mac, before renting anything (free)

### 1.1 Code and tests

```sh
cd ~/verbatus_alpha
git switch main && git pull --ff-only
git rev-parse HEAD                # this 40-character commit is <sha> for the rest of the day
uv sync --frozen --group test --group audit
.venv/bin/python -m pytest -q -p xdist -n 4 operations/bakeoff
sh .githooks/check-static.sh
```

Expect: pytest ends with `passed` and no `failed`; the static check ends `All checks
passed!`. If anything fails: stop; do not rent with failing code.

### 1.2 Pages, lists, manifests

```sh
ls ~/bakeoff-pages | wc -l                    # 73
wc -l < private/bakeoff/hard-pages.txt        # 16
.venv/bin/python -m operations.bakeoff.queue_runner validate --manifest operations/bakeoff/queue/witness-24gb.toml
.venv/bin/python -m operations.bakeoff.queue_runner validate --manifest operations/bakeoff/queue/reader-96gb.toml
```

Expect: 73, 16, and two lines starting `ok:`. If `~/bakeoff-pages` is missing or short:
`mkdir -p ~/bakeoff-pages && cp "$HOME/Desktop/Bake-off set/Pages"/*/Prepped/*.tif ~/bakeoff-pages/`
(73 TIFs, about 250 MB). If `hard-pages.txt` is missing: it lists the page stems under
"Hard pages" in `private/bakeoff/page-manifest.md`, one per line.

### 1.3 RunPod tools and the account

```sh
runpodctl version                 # 2.x; v1.x has no `pod` command: brew install runpod/runpodctl/runpodctl
runpodctl pod list --all          # []  (nothing running before we start)
ls private/ntfy.conf              # the phone topic file exists
```

If `pod list` shows a pod: stop and tell the lead (an unknown pod is billing).

## 2. Pod W (witnesses)

### 2.0 Pick the datacenter and make the volume

Read the stock with the RunPod connector (free): `get-gpu-type` with availability, product
POD, cloud SECURE, for `NVIDIA A40`, `NVIDIA GeForce RTX 3090` and
`NVIDIA RTX PRO 6000 Blackwell Server Edition`. Each answer lists datacenters with stock.

1. Prefer a datacenter that has the PRO 6000 **and** the A40; else the PRO 6000 and the
   3090. Call it `<dc>`. Both pods then share one volume.
2. If no datacenter has both: put pod W where its card is and pod R where the PRO 6000
   is, each with its own volume (`<dc W>`, `<dc R>`). The reader arms do not use the
   witness results, so nothing has to move between volumes; the pages are copied to each.
3. If none of the cards is in stock anywhere: stop and tell the lead.

Create the volume with the connector's `create-network-volume`: name
`verbatus-bakeoff-<date>`, datacenter `<dc>`, size **300 GB** when both pods
share it, or 100 GB for a pod-W-only volume and 220 GB for a pod-R-only volume (the
witness arms download 43.7 GB of weights, the readers 130.5 GB; the rest is room for the
store's staging copy, pages and results; `queue_runner validate` prints each manifest's
download total). Write its id down as `<volume id>`. It bills for storage until deleted;
after the day's results are home, ask the lead whether to keep it.

### 2.1 Create it, guard armed

**Use the RunPod connector's create-pod tool (MCP)**, the route proven on 2026-10-07 and
10-08. First print the guard's start command on the Mac:

```sh
sh operations/pod/pod_start_command.sh off <sha>
```

Then call create-pod with: name `verbatus-bakeoff-w`, image
`runpod/pytorch:1.4.0-cu1300-torch2130-ubuntu2404`, GPU type `NVIDIA A40` (or the 3090
picked in 2.0), 1 GPU, `minVcpuCountPerGpu` 16, cloud SECURE, datacenter `<dc>`, network
volume `<volume id>` mounted at `/workspace/private`, container disk 100 GB, port
`22/tcp`, `startSsh` true, and the printed text as the container start command (`args`).
Never create a pod without that start command: it arms the guard.

Backup route, if the connector is unavailable: `runpodctl` 2.x on the Mac (the API key
goes in by hand, never on a command line):

```sh
read -rs RUNPOD_API_KEY; export RUNPOD_API_KEY
START=$(sh operations/pod/pod_start_command.sh off <sha>) &&
runpodctl pod create \
  --name verbatus-bakeoff-w \
  --image runpod/pytorch:1.4.0-cu1300-torch2130-ubuntu2404 \
  --gpu-id "NVIDIA A40" --gpu-count 1 \
  --cloud-type SECURE \
  --data-center-ids <dc> \
  --network-volume-id <volume id> \
  --volume-mount-path /workspace/private \
  --container-disk-in-gb 100 \
  --ports "22/tcp" \
  --docker-args "$START" \
  --wait
```

Expect: JSON for the new pod with its `id`. Write the id down as `<pod W id>`.

- If the create is refused for lack of capacity (stock moves by the minute): try the
  other small card in the same datacenter; if neither fits, read the stock again and go
  back to 2.0 (an empty volume in the wrong datacenter can simply be deleted, it holds
  nothing yet). If nothing is free anywhere: stop and tell the lead.
- The runpodctl route cannot ask for a minimum vCPU count; after creating, check the pod
  has at least 16 vCPU (`runpodctl pod get`), else delete it and use the connector.
- If `pod_start_command.sh` prints nothing and exits 2: nothing was created; read its
  message, fix, retry.

`off` means no deadline: the queue runner ends the pod itself; the guard's idle ladder
(warn at 15 min idle, urgent at 30) stays as the backstop.

### 2.2 Get the SSH address

```sh
runpodctl pod get <pod W id>
```

Find the public IP and the port mapped to 22; they are `<ip>` and `<port>` below. Use the
direct form `ssh -p <port> root@<ip>`; the `ssh.runpod.io` proxy cannot run commands or
`scp`.

### 2.3 Set up the pod

```sh
ssh -p <port> root@<ip>
# then, on the pod:
findmnt /workspace/private && tail -2 /workspace/private/.pod_guard/guard.log
```

Expect: the mount, and a guard line containing `armed for pod`. **If there is no "armed
for pod" line: stop.** Delete the pod (`runpodctl pod delete <pod W id>`), confirm with
`runpodctl pod list --all` that it is gone, and tell the lead.

Still on the pod:

```sh
git clone https://github.com/MegaSlick/Apparatus-Verbatus-Alpha /opt/verbatus
cd /opt/verbatus && git checkout --detach <sha>
bash operations/pod/prepare_runtime.sh
UV_CACHE_DIR=/tmp/verbatus-uv-cache uv sync --frozen --group pod
mkdir -p /workspace/private/bakeoff
read -rs NTFY_TOPIC; export NTFY_TOPIC     # paste the topic from the Mac's private/ntfy.conf
```

From a second terminal on the Mac, copy the pages and the manifest up (they go to the
volume, never to git):

```sh
scp -P <port> -r ~/bakeoff-pages root@<ip>:/workspace/private/bakeoff-pages
scp -P <port> operations/bakeoff/queue/witness-24gb.toml root@<ip>:/workspace/private/bakeoff/
```

### 2.4 Check, then launch the queue detached

On the pod, in `/opt/verbatus`, in the shell that has `NTFY_TOPIC`:

```sh
ls /workspace/private/bakeoff-pages | wc -l      # 73
.venv/bin/python -m operations.bakeoff.queue_runner validate --manifest /workspace/private/bakeoff/witness-24gb.toml
.venv/bin/python -m operations.bakeoff.queue_runner run --dry-run --manifest /workspace/private/bakeoff/witness-24gb.toml
setsid nohup .venv/bin/python -m operations.bakeoff.queue_runner run \
  --manifest /workspace/private/bakeoff/witness-24gb.toml \
  > /workspace/private/bakeoff/queue-witness-24gb.log 2>&1 < /dev/null &
```

Then follow the events until the first arm's smoke is done, and leave the pod alone:

```sh
tail -f /workspace/private/bakeoff/witness-cache/events.jsonl
```

Every arm first reads two pages (its smoke run). A failed smoke stops that arm only, not
the queue.

## 3. While pod W runs: watch, nothing else

On the Mac:

```sh
.venv/bin/python -m operations.bakeoff.queue_runner watch --ssh "ssh -p <port> root@<ip>" \
  --status /workspace/private/bakeoff/witness-cache/status.json
```

(or `watch --ntfy --queue witness-24gb` to follow the phone pings instead). It prints a line
per change and exits 0 when the queue writes `DONE.json`, 1 on failure.

Do nothing else on the pod: no `pgrep`, no `nvidia-smi` loops, no second launch. The
queue overlaps installs with the running arm, retries a failed arm once at the end,
copies and verifies the cache, pings and deletes the pod.

| You see | Do |
|---|---|
| An arm errors or its smoke fails | Nothing now. It is retried once at the end and reported in `status.json` (`errors`, `finished_arms`). Fix at home; rerun that arm alone on a later pod (cached pages are skipped). |
| `watch` warns the status is 10 minutes old | Look once over SSH: `tail /workspace/private/bakeoff/queue-witness-24gb.log` and `cat /workspace/private/bakeoff/witness-cache/status.json`. If the queue is still running an arm, leave it. If the process is gone, tell the lead. Do not restart things from the Mac. |
| The queue ends `state: failed` | The pod is left up on purpose. Do step 6 (fetch), then delete the pod by hand and confirm it is gone. |
| The pod is gone but there is no `DONE.json` | The guard or a crash ended it. The per-arm caches on the volume are what was synced; step 6 `fetch` reports what verifies. |
| The day's spend nears $15 | Tell the lead before it passes. |

## 4. Pod R (readers)

### 4.1 When to start it

Start pod R when pod W's smoke runs have passed: in pod W's `events.jsonl`, `chandra`,
`dai` and `churro` each have a `queue-command-start` event with `"phase": "run"` (smoke
passed, full run started), and there is no `queue-arm-error` for them. The lead has
approved the overlap; do not wait for pod W to finish.

### 4.2 Create and set up

Same as 2.1–2.3 with these changes: name `verbatus-bakeoff-r`, GPU type
`NVIDIA RTX PRO 6000 Blackwell Server Edition`, container disk 120 GB, no vCPU minimum
needed (the reader queue has no CPU arms), datacenter and volume from step 2.0 (the
shared `<dc>` and `<volume id>`, or `<dc R>` and its own volume). Call its id
`<pod R id>`. Copy `reader-96gb.toml` up instead of the witness manifest, and the pages
too if pod R has its own volume.

The queue downloads the three Qwen snapshots itself at the cards' pinned commits in its
`prepare` steps, so pod R needs internet during those and no token (the repos are not
gated). Do **not** export `HF_HUB_OFFLINE=1` in the shell that starts a queue: the queue
makes each arm's command offline by itself. On the pod, in `/opt/verbatus`, in the shell
that has `NTFY_TOPIC`:

```sh
.venv/bin/python -m operations.bakeoff.queue_runner validate --manifest /workspace/private/bakeoff/reader-96gb.toml
.venv/bin/python -m operations.bakeoff.queue_runner run --dry-run --manifest /workspace/private/bakeoff/reader-96gb.toml
setsid nohup .venv/bin/python -m operations.bakeoff.queue_runner run \
  --manifest /workspace/private/bakeoff/reader-96gb.toml \
  > /workspace/private/bakeoff/queue-reader-96gb.log 2>&1 < /dev/null &
```

Then `watch` as in step 3 with `--status /workspace/private/bakeoff/reader-cache/status.json`
(and `--queue reader-96gb` for the phone route). The same table applies.

## 5. The plan for each pod

Prices are RunPod Secure on-demand as seen 2026-10-08 (`config/pod_placement.toml`).

**Pod W.** GPU arms in order: chandra, dai, dai-conf10, dai-whole, churro, chandra-native,
churro-native, dots-mocr, surya-rec-surya, party-blla. CPU arms (the line sources, then
kraken, PyLaia) run beside them. `queue_runner run --dry-run` prints each arm's box and
lane. Cut rules if the day runs long: Party is cut first (`overrun`), the Surya recogniser
at 30 minutes behind schedule (`behind-schedule`), dots.mocr only if its install fails;
the baselines and the CTC line arms are never cut. Nothing is killed on a clock.

**Pod R.** qwen38-27b-vendor, qwen35-27b-vendor, qwen35-9b-vendor, then the two blind
arms only if their 2026-10-08 caches are not already in the reader cache (`cut =
"overrun"`; on a fresh volume they run again if time allows). About 2.8 hours including
the downloads.

Expected spend at the 2026-10-08 evening prices (A40 $0.59/h, RTX 3090 $0.50/h, PRO 6000
$2.49/h; read them again on the day): pod W 3.6 h with 32 vCPU or 7.2 h with 16
(`run --dry-run` prints the estimate for the pod it runs on), about $2–4.50; pod R about
2.8 h, about $7; about $9–12 in all plus the volume, inside the $15 approval. If the
dry run on pod W shows a day long enough to pass $15, stop and tell the lead before
launching.

## 6. Bring the results home and confirm both pods are gone

```sh
.venv/bin/python -m operations.bakeoff.queue_runner fetch --ssh "ssh -p <port W> root@<ip W>" \
  --remote /workspace/private/bakeoff/witness-cache --into private/bakeoff/witness-cache-$(date +%F)
.venv/bin/python -m operations.bakeoff.queue_runner fetch --ssh "ssh -p <port R> root@<ip R>" \
  --remote /workspace/private/bakeoff/reader-cache --into private/bakeoff/reader-cache-$(date +%F)
```

`fetch` checks every file against the pod's `DONE.json`. If a pod has already deleted
itself, its verified copy is on the volume under `witness-cache-home/` or
`reader-cache-home/`: tell the lead, who decides between a cheap CPU pod on the volume to
copy it home or the S3 route in `operations/pod/README.md`.

Then confirm, never assume:

```sh
runpodctl pod list --all          # []
runpodctl pod get <pod W id>      # an error: not found
runpodctl pod get <pod R id>      # an error: not found
```

and check that billing has stopped in the RunPod console. If a pod is still listed:
`runpodctl pod delete <id>`, then list again.

## 7. Score at home

```sh
.venv/bin/python -m operations.bakeoff.score --cache private/bakeoff/witness-cache-<date> \
  --gold "$HOME/Desktop/Bake-off set/Pages" --gold-glob '*/Prepped/*.txt' \
  --hard-pages private/bakeoff/hard-pages.txt \
  --out private/bakeoff/scores-<date>
.venv/bin/python -m operations.bakeoff.roster --cache private/bakeoff/witness-cache-<date> \
  --gold "$HOME/Desktop/Bake-off set/Pages" --gold-glob '*/Prepped/*.txt' \
  --baselines chandra,dai,churro --hard-pages private/bakeoff/hard-pages.txt \
  --out private/bakeoff/scores-<date>
```

Run the reader cache through `score` the same way, with its own `--out`. Every number is
"vs fool's gold (ballpark, not accuracy)" until the lead's checked gold exists.

Layout on the Mac:

```
private/bakeoff/
  witness-cache-<date>/<arm>/<stem>.json     every arm's raw cache (+ _records/, _lines/)
  reader-cache-<date>/<arm>/<stem>.json
  scores-<date>/scores.md, scores.jsonl, roster.md, roster.jsonl
  page-manifest.json, page-manifest.md
  hard-pages.txt
  comparison-<date>.md                       the one-page table below
```

## 8. The one-page comparison the session fills in

| arm | group scored | headline (group metric) | rescue rate | corr. with Chandra / DAI / Churro | s/page | VRAM | verdict |
|---|---|---|---|---|---|---|---|
| chandra | acts, index-list, … | | — | — | | | baseline |
| chandra-native | | | | | | | |
| dai / dai-conf10 / dai-whole | acts | act recall, unit CER | | | | | |
| churro / churro-native | | | | | | | |
| dots-mocr | | | | | | | |
| kraken-ppocrv6-blla / -surya | | | | | | | |
| kraken-mccatmus-blla / -surya | | | | | | | |
| kraken-mcfondue-blla / -surya | | | | | | | |
| pylaia-belfort-* / pylaia-popp-* | | | | | | | |
| party-blla | | | | | | | |
| surya-rec-surya | | | | | | | |
| qwen*-blind / qwen*-vendor | all (reference) | | | | | | reader |

Verdict words: `roster` (earns a 4th-witness arm on a type, plan 4.5), `keep as reference`,
`drop`, with the page type beside it. Hard pages are inside every number and also listed
on their own, so each model's weak spots show.

Report to the lead in plain words, phone-sized: which models did well on which page types,
which failed and how, what it cost, and that both pods are confirmed gone.
