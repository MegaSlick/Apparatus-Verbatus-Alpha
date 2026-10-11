# Bake-off runbook: one day, two pods, every arm

For a Claude Code session with no memory of the preparation and no terminal of its own.
Every command is complete and runs from the Mac in a fresh shell, over `ssh` for the pod;
nothing waits for a keyboard. Do the steps in order. Each step says what to run, what you
should see, and what to do otherwise. When a step has no line for what you see, stop and
tell the lead; do not improvise on a paid pod.

Read once first: [README.md](README.md) (the tool), `operations/pod/RUNPOD.md` (tools,
traps) and `operations/pod/README.md` (why every pod starts with its guard armed). Page
names are private: never put one in git, a PR, a commit message or a public comment.

Long-running commands (`watch`, `fetch`, anything over a minute) run in the background and
the session reads their output file; a foreground `sleep`, `tail -f` or `watch` blocks the
session.

## 0. The day's decisions

The lead sets these with the session before the first paid create; they then cover every
paid create in sections 1–8 that day. Section 9 sets its own.

| Question | Plan |
|---|---|
| Small card (pod W) | A40 48 GB; if none is free, RTX 3090 24 GB. As many vCPU as stock allows: try 32, fall back to 16 (the CPU line arms need them; the dry run's estimate decides whether the day fits) |
| Big card (pod R) | RTX PRO 6000 96 GB |
| Where | Any Secure datacenter with the card in stock. Each pod keeps its files and results on its own disk (deleted with the pod), and the session fetches them home over SSH before deleting it (step 6). No network volume unless the lead says so |
| Models | Every arm downloads its own pinned weights on boot (the queue's `prepare` steps). HunyuanOCR is excluded: its licence excludes the EU and UK |
| Spend | A standing approval for the whole day (both pods, the smoke pod, any fetch pod, one retry) |
| Pod windows | An hours window, never `off`: pod W `12`, pod R `6`. The guard deletes the pod at the deadline; the queue releases it earlier when done |
| When pod R starts | As soon as chandra's smoke on pod W has passed (step 4.1) |
| Pages | All 73 pages are scored; hard pages count in every number and also get their own table |

**Ask the lead only when:** the day would pass the approved spend; none of the cards here is
free anywhere; something would delete data or a volume; or a step says "stop".

## 1. On the Mac, before renting anything (free)

### 1.1 Code and tests

`<sha>` is `main`'s head and must exist on GitHub: the guard fetches `pod_guard.sh` from
`raw.githubusercontent.com` at that commit.

```sh
cd ~/verbatus_alpha
git switch main && git pull --ff-only
git rev-parse HEAD                # this 40-character commit is <sha> for the rest of the day
git fetch origin && git branch -r --contains "$(git rev-parse HEAD)" | grep -c origin/main   # 1
uv sync --frozen --group test
.venv/bin/python -m pytest -q -p xdist -n 4 operations/bakeoff operations/pod/test_create_pod.py
sh .githooks/check-static.sh
```

Expect `1`, no `failed`, and the static check ending `All checks passed!`. Otherwise stop:
do not rent with failing code.

### 1.2 Pages, lists, manifests

```sh
ls ~/bakeoff-pages | wc -l                    # 73
wc -l < private/bakeoff/hard-pages.txt        # 16
ls private/bakeoff/witness-cache-all/qwen35-27b-blind private/bakeoff/witness-cache-all/qwen35-9b-blind | grep -c json   # 150
.venv/bin/python -m operations.bakeoff.queue_runner validate --manifest operations/bakeoff/queue/witness-24gb.toml
.venv/bin/python -m operations.bakeoff.queue_runner validate --manifest operations/bakeoff/queue/reader-96gb.toml
```

Expect 73, 16, 150 and two `ok:` lines (on the Mac each validate also prints `pages folder
/workspace/private/bakeoff-pages: not found here`, which is right). If `~/bakeoff-pages` is
missing or short: `mkdir -p ~/bakeoff-pages && cp "$HOME/Desktop/Bake-off set/Pages"/*/Prepped/*.tif ~/bakeoff-pages/`
(73 TIFs, about 250 MB). If `hard-pages.txt` is missing, it lists the stems under "Hard
pages" in `private/bakeoff/page-manifest.md`, one per line.

### 1.3 RunPod tools and the account

```sh
runpodctl version                 # 2.x: brew install runpod/runpodctl/runpodctl
runpodctl pod list --all          # []  (nothing running before we start)
ls private/ntfy.conf              # the phone topic file exists
.venv/bin/python -m operations.pod.create_pod --help | head -3
```

`create_pod` reads the account key from `RUNPOD_API_KEY`, else from `~/.runpod/config.toml`,
and never prints it. If `pod list` shows a pod: stop and tell the lead (an unknown pod is
billing).

## 2. Pod W (witnesses)

### 2.0 Pick the card

Read stock with the RunPod connector (free): `get-gpu-type` with availability, product POD,
cloud SECURE, for `NVIDIA A40`, `NVIDIA GeForce RTX 3090`, `NVIDIA RTX A5000` and `NVIDIA
RTX PRO 6000 Blackwell Server Edition`. Write the prices down for step 5. Pod W takes the
A40 if any Secure datacenter has one, else the 3090; pod R takes the PRO 6000 wherever it
is. If none is in stock anywhere: stop and tell the lead. Pin `--datacenter <dc>` only when a
card is in stock in exactly one place.

### 2.1 Create it, guard armed, on its own disk

`operations/pod/create_pod.py` sends the v1 GraphQL create with the guard's start command.
Each pod gets its own disk at `/workspace/private` (`--disk-gb`), deleted with the pod;
results come home over SSH (step 6) before the pod is deleted. `--cuda 13.0` keeps the pod
off hosts the `cu1300` image cannot use.

**First, a two-minute smoke on the cheapest card**, to prove the create, the guard and SSH:

```sh
START=$(sh operations/pod/pod_start_command.sh 1 <sha>) &&
.venv/bin/python -m operations.pod.create_pod \
  --name verbatus-smoke --gpu "NVIDIA RTX A5000" \
  --image runpod/pytorch:1.4.0-cu1300-torch2130-ubuntu2404 \
  --container-disk-gb 20 --disk-gb 20 --cuda 13.0 \
  --start-command "$START"
```

(`--gpu "NVIDIA GeForce RTX 3090"` if no A5000 is in stock.) Expect exit 0 and JSON with
`volumeMountPath` `/workspace/private`. Get its address (2.2), check, and delete:

```sh
ssh -o ConnectTimeout=20 -p <port> root@<ip> 'findmnt -o TARGET,FSTYPE /workspace/private;
  grep -c "armed for pod" /workspace/private/.pod_guard/guard.log'
runpodctl pod delete <smoke pod id>
```

Expect the mounted target and a count of at least 1. Then run `runpodctl pod list --all`
until it prints `[]`.

| You see | Do |
|---|---|
| exit 2, `nothing was created` | First `runpodctl pod list --all`: a timeout after the POST can still have created the pod (delete it if so). Then read the message, fix, retry |
| exit 3, `does not report the mounts` | The pod exists without what was asked. Delete it, confirm, stop, and tell the lead with the printed JSON |
| no `/workspace/private`, or a count of 0 | Delete the pod, confirm, stop, tell the lead |
| no public ip/port, or `ssh` never connects | Delete it, confirm, create once more; a second miss is a stop |

**Then pod W**, the same way with the real card:

```sh
START=$(sh operations/pod/pod_start_command.sh 12 <sha>) &&
.venv/bin/python -m operations.pod.create_pod \
  --name verbatus-bakeoff-w --gpu "NVIDIA A40" \
  --image runpod/pytorch:1.4.0-cu1300-torch2130-ubuntu2404 \
  --container-disk-gb 100 --disk-gb 100 --min-vcpu 32 --cuda 13.0 \
  --start-command "$START"
```

Expect exit 0 and JSON with `id` and `vcpuCount` 32 or more; the id is `<pod W id>`. The
`&&` means a refused start command creates nothing. Never create a pod without it.

- Refused for stock: `pod list --all` first, then retry with `--min-vcpu 16`, then the
  other small card. If nothing is free anywhere: stop.
- `vcpuCount` below what was asked: delete, confirm, create again; a second miss is a stop.
- `pod_start_command.sh` prints nothing and exits 2: nothing was created; read its message.

**Fallback, only when the lead says so: a network volume.** Create one with the connector's
`create-network-volume` (name `verbatus-bakeoff-<date>`, the datacenter from the stock
reading, 100 GB for pod W, 300 GB for pod R; it bills until deleted, so ask the lead
afterwards whether to keep it), then create with `--network-volume <volume id> --datacenter
<dc>` in place of `--disk-gb`.

### 2.2 Get the SSH address

`runpodctl pod get <pod W id>` shows the public IP and the port mapped to 22: `<ip>` and
`<port>` below. Use `ssh -p <port> root@<ip>`, never the `ssh.runpod.io` proxy. The pod may
take a minute or two to answer.

### 2.3 Check, then set up the pod

```sh
ssh -p <port> root@<ip> 'findmnt -o TARGET,FSTYPE /workspace/private;
  grep -c "armed for pod" /workspace/private/.pod_guard/guard.log;
  nproc; df -h /workspace/private | tail -1'
```

Expect the mount, a count of at least 1, `nproc` 32 (or 16), and about 100G free. **If the
count is 0 or the mount is missing: stop**, delete the pod, confirm it is gone, tell the
lead.

Arm the guard's phone pings (the topic goes over stdin, never on a command line), then
clone and set up (about 5 minutes, in the background):

```sh
sed -n 's/^NTFY_TOPIC=//p' private/ntfy.conf | tail -n 1 | tr -d "\"'" |
  ssh -p <port> root@<ip> 'umask 077 && mkdir -p /workspace/private/.pod_guard && cat > /workspace/private/.pod_guard/ntfy_topic && wc -c < /workspace/private/.pod_guard/ntfy_topic'
ssh -p <port> root@<ip> 'set -e; git clone -q https://github.com/MegaSlick/Apparatus-Verbatus-Alpha /opt/verbatus &&
  cd /opt/verbatus && git checkout -q --detach <sha> && bash operations/pod/prepare_runtime.sh &&
  UV_CACHE_DIR=/tmp/verbatus-uv-cache uv sync --frozen --group pod && mkdir -p /workspace/private/bakeoff && echo SETUP-OK'
```

Expect a byte count and `SETUP-OK`. Then the pages and the manifest (the `/.` form does not
nest on a retry):

```sh
ssh -p <port> root@<ip> 'mkdir -p /workspace/private/bakeoff-pages'
scp -P <port> -r ~/bakeoff-pages/. root@<ip>:/workspace/private/bakeoff-pages/
scp -P <port> operations/bakeoff/queue/witness-24gb.toml root@<ip>:/workspace/private/bakeoff/
ssh -p <port> root@<ip> 'ls /workspace/private/bakeoff-pages | wc -l'     # 73
```

### 2.4 Check, then launch the queue detached

```sh
Q="--manifest /workspace/private/bakeoff/witness-24gb.toml --keep-pod"
ssh -p <port> root@<ip> "cd /opt/verbatus && .venv/bin/python -m operations.bakeoff.queue_runner validate $Q &&
  .venv/bin/python -m operations.bakeoff.queue_runner run --dry-run $Q"
```

Expect `ok: witness-24gb, …`, then the dry run's estimate for this pod's CPUs, ending with
the copy to `sync_to` and `end pod: none`. If the estimate would pass the day's spend
(step 5): stop and tell the lead.

`sync_to` is a second copy on the pod's own disk, made and verified at the end.
`--keep-pod` keeps the pod so the results come home over SSH (step 6) before the session
deletes it; without it the queue would delete the pod, and the results with it.

Launch with the phone topic on stdin and the pod id checked first (the queue cannot end a
pod without `RUNPOD_POD_ID`):

```sh
sed -n 's/^NTFY_TOPIC=//p' private/ntfy.conf | tail -n 1 | tr -d "\"'" |
  ssh -p <port> root@<ip> "read -r NTFY_TOPIC; export NTFY_TOPIC;
    [ -n \"\$RUNPOD_POD_ID\" ] || export RUNPOD_POD_ID=\$(tr '\\0' '\\n' </proc/1/environ | sed -n 's/^RUNPOD_POD_ID=//p');
    [ -n \"\$RUNPOD_POD_ID\" ] || { echo 'no RUNPOD_POD_ID; not launched' >&2; exit 2; };
    [ -n \"\$NTFY_TOPIC\" ] || { echo 'no topic on stdin; not launched' >&2; exit 2; };
    cd /opt/verbatus && setsid nohup .venv/bin/python -m operations.bakeoff.queue_runner run $Q \
      > /workspace/private/bakeoff/queue-witness-24gb.log 2>&1 < /dev/null &
    sleep 5; echo launched pod \$RUNPOD_POD_ID"
```

Expect `launched pod <pod W id>`. `not launched` means nothing runs; a pod without
`RUNPOD_POD_ID` in PID 1's environment is a stop. Look once at the first minutes, then leave
the pod alone:

```sh
ssh -p <port> root@<ip> 'tail -n 20 /workspace/private/bakeoff/witness-cache/events.jsonl; tail -n 5 /workspace/private/bakeoff/queue-witness-24gb.log'
```

Every arm first reads two pages. A failed smoke stops that arm only.

## 3. While a pod runs: watch, nothing else

On the Mac, in the background:

```sh
.venv/bin/python -m operations.bakeoff.queue_runner watch --ssh "ssh -p <port> root@<ip>" \
  --status /workspace/private/bakeoff/witness-cache/status.json
```

It exits 0 when the queue writes `DONE.json` (with `--keep-pod` the last line says the pod
is `kept (end_pod = none)`), 1 on failure. Do nothing else on the pod: no `pgrep`, no
`nvidia-smi` loops, no second launch.

| You see | Do |
|---|---|
| An arm errors or its smoke fails (`arm failed: ...`) | Nothing. It is retried once at the end and reported in `status.json`. Rerun it alone on a later pod if needed; cached pages are skipped |
| An arm ends with `failed_pages` | Nothing. Those pages failed with their reason; the reading is the finding |
| `Needs a decision` | Act on the message |
| `Queue finished` | Fetch (step 6), then delete the pod |
| `watch` warns the status is 10 minutes old | Look once (`tail` the queue log, `cat` `status.json`). If an arm is still running, leave it; if the process is gone, tell the lead |
| `state: failed` | No pages were found, or the end copy did not verify. The pod is up: fetch (step 6), delete it, confirm |
| The pod is gone with no `DONE.json` | The deadline or a crash ended it and its disk went with it. Tell the lead |
| The day's spend nears the approval | Tell the lead before it passes |

## 4. Pod R (readers)

### 4.1 When to start it

When chandra's smoke on pod W has passed:

```sh
ssh -p <port> root@<ip> 'grep -c "\"arm\": \"chandra\".*\"phase\": \"run\"" /workspace/private/bakeoff/witness-cache/events.jsonl; grep -c "queue-arm-error.*chandra" /workspace/private/bakeoff/witness-cache/events.jsonl'
```

Expect `1` then `0`. Do not wait for pod W to finish.

### 4.2 Create and set up

As 2.1–2.3, with: name `verbatus-bakeoff-r`, `--gpu "NVIDIA RTX PRO 6000 Blackwell Server
Edition"`, `pod_start_command.sh 6 <sha>`, `--container-disk-gb 120 --disk-gb 300` (the 27B
passes through the store as download cache, staging copy and final copy, about 167 GB for
one model, and the readers download 130.5 GB of weights in all), no `--min-vcpu`, the same
`--cuda 13.0`, and no second smoke. Its address is `<ip R>`, `<port R>`. Copy
`reader-96gb.toml` and the pages up. Expect about 300G free; below 250G, stop.

The blind arms resume from the earlier caches; copy them up before launching:

```sh
ssh -p <port R> root@<ip R> 'mkdir -p /workspace/private/bakeoff/reader-cache'
scp -P <port R> -r private/bakeoff/witness-cache-all/qwen35-27b-blind private/bakeoff/witness-cache-all/qwen35-9b-blind root@<ip R>:/workspace/private/bakeoff/reader-cache/
ssh -p <port R> root@<ip R> 'ls /workspace/private/bakeoff/reader-cache/qwen35-27b-blind /workspace/private/bakeoff/reader-cache/qwen35-9b-blind | grep -c json'   # 150
```

The queue downloads the Qwen snapshots itself in its `prepare` steps (internet needed, no
token). Never put `HF_HUB_OFFLINE=1` in the launch command. Validate, dry-run and launch as
2.4 with `Q="--manifest /workspace/private/bakeoff/reader-96gb.toml --keep-pod"` and the log
`/workspace/private/bakeoff/queue-reader-96gb.log`; watch as in step 3 with `--status
/workspace/private/bakeoff/reader-cache/status.json`.

## 5. The plan for each pod, and the tally

**Pod W.** GPU arms in order: chandra, dai, dai-conf10, dai-whole, churro, chandra-native,
churro-native, dots-mocr, surya-rec-surya, party-blla. CPU arms (the line sources, then
kraken, PyLaia) run beside them. If the day runs long, Party is cut first (`overrun`), the
Surya recogniser at 30 minutes behind schedule (`behind-schedule`), dots.mocr only if its
install fails; the baselines and the CTC line arms are never cut. Nothing is killed on a
clock.

**Pod R.** qwen38-27b-vendor, qwen35-27b-vendor, qwen35-9b-vendor, then the two blind arms
(cached pages are skipped). About 2.8 hours including downloads.

**The tally.** Spend is price per hour × hours, per pod, using the prices read in 2.0 (not
`config/pod_placement.toml`). Pod W runs about 3.6 h with 32 vCPU or 7.2 h with 16 (the dry
run says which); pod R about 2.8 h; smoke and fetch pods cost cents. Disks bill with their
pods. **If the tally would pass the approval, stop and tell the lead.**

## 6. Bring the results home, then delete the pods, then confirm

When `watch` has exited 0, fetch from the pod's own disk (in the background); it checks
every file against the pod's `DONE.json`:

```sh
.venv/bin/python -m operations.bakeoff.queue_runner fetch --ssh "ssh -p <port> root@<ip>" \
  --remote /workspace/private/bakeoff/witness-cache --into private/bakeoff/witness-cache-$(date +%F)
.venv/bin/python -m operations.bakeoff.queue_runner fetch --ssh "ssh -p <port R> root@<ip R>" \
  --remote /workspace/private/bakeoff/reader-cache --into private/bakeoff/reader-cache-$(date +%F)
```

Only when every file verified, delete that pod, and confirm:

```sh
runpodctl pod delete <pod W id>
runpodctl pod delete <pod R id>
runpodctl pod list --all          # []
runpodctl pod get <pod W id>      # an error: not found
runpodctl pod get <pod R id>      # an error: not found
```

Then check in the console that billing has stopped. The queue's own copy is on the pod's
disk, so nothing else holds the results until the fetch is done.

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

Score the reader cache the same way with its own `--out`. Every number is "vs fool's gold
(ballpark, not accuracy)" until the lead's checked gold exists.

```
private/bakeoff/
  witness-cache-<date>/<arm>/<stem>.json     every arm's raw cache (+ _records/, _lines/)
  reader-cache-<date>/<arm>/<stem>.json
  scores-<date>/scores.md, scores.jsonl, roster.md, roster.jsonl
  page-manifest.json, page-manifest.md
  hard-pages.txt
  comparison-<date>.md                       the one-page table below
```

## 8. The one-page comparison

| arm | group scored | headline (group metric) | rescue rate | corr. with Chandra / DAI / Churro | s/page | VRAM | verdict |
|---|---|---|---|---|---|---|---|
| chandra | acts, index-list, … | | — | — | | | baseline |
| chandra-native | | | | | | | |
| dai / dai-conf10 / dai-whole | acts | act recall, unit CER | | | | | |
| churro / churro-native | | | | | | | |
| dots-mocr | | | | | | | |
| kraken-* (ppocrv6, mccatmus, mcfondue; -blla / -surya) | | | | | | | |
| pylaia-belfort-* / pylaia-popp-* | | | | | | | |
| party-blla | | | | | | | |
| surya-rec-surya | | | | | | | |
| qwen*-blind / qwen*-vendor | all (reference) | | | | | | reader |

Verdicts: `roster` (earns a 4th-witness arm on a page type), `keep as reference`, or `drop`,
with the page type beside it. Report to the lead, phone-sized: which models did well on which
page types, which failed and how, what it cost, and that both pods are confirmed gone.

## 9. Perlector build bake-off

Does a smaller or faster build of the Perlector's model (FP8, FP8 with MTP, NVFP4,
optionally Qwen3.5-27B) read pages as well as the bf16 build the pipeline serves? One fresh
pipeline run supplies the requests; every build is sent those same requests and scored at
home. **The session that runs it sets the budget with the lead.** The queue alone plans
6.5 hours on the 96 GB card (`queue_runner run --dry-run` prints it), on top of the fresh
run.

1. **Fresh run.** Create and set up a 96 GB pod as in 4.2, with a guard window for the whole
   day (about 10 hours: `pod_start_command.sh 10 <sha>`). Then `pod_run --from door --to
   perlector` with the Perlector served bf16 (`operations/pod/README.md`, "`pod_run.py`").
   Do not reuse an older run: its requests were served under other settings. Keep the run
   folder whole at `/workspace/private/runs/<run id>` (the stage seals are read through that
   name). Keep the pod.
2. **Check the replay.** Put the run's page images in `/workspace/private/bakeoff-pages`
   (same stems), and write the manifest with the run id on the pod:
   `ssh -p <port> root@<ip> 'cd /opt/verbatus && mkdir -p /workspace/private/bakeoff && sed
   "s/RUN_ID/<run id>/" operations/bakeoff/queue/perlector-fed-96gb.toml >
   /workspace/private/bakeoff/perlector-fed-96gb.toml'`.
   Then, also on the pod,
   `ssh -p <port> root@<ip> 'cd /opt/verbatus && .venv/bin/python -m operations.bakeoff.fed_arm
   prompts --run-tree /workspace/private/runs/<run id>'` must print `whole requests byte-identical N of N` (N the run's page count) and no `config
   differs` line. Anything else: stop.
3. **Run the fed queue** as in 2.4 with
   `Q="--manifest /workspace/private/bakeoff/perlector-fed-96gb.toml --keep-pod"`, the log
   `/workspace/private/bakeoff/queue-perlector-fed-96gb.log`, and `--status
   /workspace/private/bakeoff/fed-cache/status.json`. Arms: bf16-a, bf16-b, fp8, fp8-mtp3,
   nvfp4, qwen35, each with the run's model name, sealed sampling and the pipeline's serving
   row. A build that will not start fails alone.
4. **Fetch, delete, confirm** as in section 6 (`--remote /workspace/private/bakeoff/fed-cache
   --into private/bakeoff/fed-cache-<date>`), and bring the run folder home too (`scp -r` or
   `verbatus fetch-run`): scoring reads it.
5. **Score at home** with `fed_score` (README, "The Perlector scorecard"). First bf16-a
   against bf16-b (`--answers .../bf16-a --compare .../bf16-b --names bf16-a,bf16-b`): the
   noise floor. Then each build against bf16-a. A difference no larger than the noise floor
   is not a finding. Read answer health beside the reading.

What this does not measure: the re-ask (the replay sends the first reading only), and
sampling variation (bf16-a and bf16-b share a seed, so they measure engine variation only).
