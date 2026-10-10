# Bake-off runbook: one day, two pods, every arm, no idle card

For a Claude Code session with no memory of the preparation and no terminal of its own:
every command here is complete and runs from the Mac in a fresh shell, over `ssh` for the
pod; nothing waits for a keyboard. Do the steps in order. Each step says what to run, what
you should see, and what to do if you see something else. When a step has no "if" line
for what you see, stop and tell the lead what you saw; do not improvise on a paid pod.

Background, read once: `README.md` in this folder (the tool), `operations/pod/RUNPOD.md`
(the RunPod cheat sheet: prices, tools, traps) and `operations/pod/README.md` "The hand
route" (why every pod starts with its guard armed). Page names are private: never put one
in git, a PR, a commit message or a public comment.

Long-running commands (`watch`, `fetch`, anything over a minute) run in the background
(Bash `run_in_background`, or the `long-running-work` skill) and the session reads their
output file; a foreground `sleep`, `tail -f` or `watch` blocks the session.

## 0. Decisions for the 2026-10-09 bake-off (sections 1–8) — do not ask again

The lead set these in the session for 2026-10-09; they cover every paid create in
sections 1–8 on that day. Any other day's session sets its own budget (section 9).

| Question | Answer |
|---|---|
| Small card (pod W) | A40 48 GB; if none is free, RTX 3090 24 GB. Either with as many vCPU as stock allows: try 32, fall back to 16 (the CPU line arms need them; the dry run's estimate decides whether the day fits). |
| Big card (pod R) | RTX PRO 6000 96 GB. |
| Where | Any Secure datacenter that has the cards in stock on the day. No region is fixed: each pod keeps its working files and its results on its own disk (deleted with the pod) and the session fetches them home over SSH before deleting it (step 6). The API attaches no global volume (`operations/pod/RUNPOD.md`), so none is used. No network volume is made unless the lead says so (2.1, fallback). |
| Models | Every arm downloads its own weights on boot at pinned versions (the queue's `prepare` steps); nothing has to be on a volume beforehand. Try every reasonable candidate. HunyuanOCR is dropped (licence excludes the EU/UK). dots.mocr and the Surya recogniser run for the bench. |
| Spend | Standing approval up to **$15** for the whole day: both pods, the two-minute smoke pod, any fetch pod, one retry included. |
| Pod windows | Pods start with an hours window, not `off`: pod W `12`, pod R `6`. The guard deletes the pod at the deadline whatever is running; that is the backstop if the session dies. The queue releases the guard earlier when it is done. |
| When pod R starts | As soon as chandra's smoke run on pod W has passed (step 4.1), without waiting for pod W to finish. |
| Paid runs | All approved: DAI arm twice more, `qwen-vendor` per reader, the CPU line arms beside the GPU. |
| Pages | All 73 are real research pages and are all scored. None is a test page, none is excluded. Hard pages count in every number and also get their own table. |

**Ask the lead only when:** the day would pass $15; none of the cards in this runbook is
free anywhere; something would delete data or a volume; or a step here says "stop".

## 1. On the Mac, before renting anything (free)

### 1.1 Code and tests

Both PRs (#318, the cheat sheet; #319, the pod tool) are merged before the
day. `<sha>` is `main`'s head and must exist on GitHub: the guard fetches
`pod_guard.sh` from `raw.githubusercontent.com` at that commit.

```sh
cd ~/verbatus_alpha
git switch main && git pull --ff-only
git rev-parse HEAD                # this 40-character commit is <sha> for the rest of the day
git fetch origin && git branch -r --contains "$(git rev-parse HEAD)" | grep -c origin/main   # 1
uv sync --frozen --group test
.venv/bin/python -m pytest -q -p xdist -n 4 operations/bakeoff operations/pod/test_create_pod.py
sh .githooks/check-static.sh
```

Expect: `1`, pytest with no `failed`, the static check ending `All checks passed!`. If
anything fails: stop; do not rent with failing code.

### 1.2 Pages, lists, manifests

```sh
ls ~/bakeoff-pages | wc -l                    # 73
wc -l < private/bakeoff/hard-pages.txt        # 16
ls private/bakeoff/witness-cache-all/qwen35-27b-blind private/bakeoff/witness-cache-all/qwen35-9b-blind | grep -c json   # 150
.venv/bin/python -m operations.bakeoff.queue_runner validate --manifest operations/bakeoff/queue/witness-24gb.toml
.venv/bin/python -m operations.bakeoff.queue_runner validate --manifest operations/bakeoff/queue/reader-96gb.toml
```

Expect: 73, 16, 150, and two `ok:` lines; on the Mac each validate also prints `pages
folder /workspace/private/bakeoff-pages: not found here`, which is right (the pages are
on the pod). If `~/bakeoff-pages` is missing or short:
`mkdir -p ~/bakeoff-pages && cp "$HOME/Desktop/Bake-off set/Pages"/*/Prepped/*.tif ~/bakeoff-pages/`
(73 TIFs, about 250 MB). If `hard-pages.txt` is missing: it lists the page stems under
"Hard pages" in `private/bakeoff/page-manifest.md`, one per line.

### 1.3 RunPod tools and the account

```sh
runpodctl version                 # 2.x; v1.x has no `pod` command: brew install runpod/runpodctl/runpodctl
runpodctl pod list --all          # []  (nothing running before we start)
ls private/ntfy.conf              # the phone topic file exists
.venv/bin/python -m operations.pod.create_pod --help | head -3
```

The account key is never typed: `create_pod` reads it from `~/.runpod/config.toml`
(`runpodctl`'s own config) when `RUNPOD_API_KEY` is not set, and never prints it. If
`pod list` shows a pod: stop and tell the lead (an unknown pod is billing).

## 2. Pod W (witnesses)

### 2.0 Pick the card

Read the stock with the RunPod connector (free): `get-gpu-type` with availability, product
POD, cloud SECURE, for `NVIDIA A40`, `NVIDIA GeForce RTX 3090`, `NVIDIA RTX A5000` and
`NVIDIA RTX PRO 6000 Blackwell Server Edition`. Each answer lists datacenters with stock
and the hourly price; write the prices down for step 5.

1. Pod W takes the A40 if any Secure datacenter has one, else the 3090. Pod R takes the
   PRO 6000 wherever it is. The two pods need not share a datacenter.
2. If none of the cards is in stock anywhere: stop and tell the lead.

Nothing is created in this step. The datacenter is left to RunPod unless a card is in
stock in exactly one place (then `--datacenter <dc>` pins it).

### 2.1 Create it, guard armed, on its own disk

**The project's pod tool is the route**: `operations/pod/create_pod.py` sends the v1
GraphQL mutation (`podFindAndDeployOnDemand`) with the guard's start command. Each pod
gets its **own disk at `/workspace/private`** (`--disk-gb`: the guard's records, the
clone, the model store, the pages, the cache and the queue's copy of it live there; it
is deleted with the pod). RunPod's API attaches no global volume
(`operations/pod/RUNPOD.md`, "Attaching a global volume"), so none is asked for: the
results come home over SSH (step 6) before the pod is deleted. `--cuda 13.0` keeps the
pod off 12.8 hosts, which the `cu1300` image cannot use. The hours window arms the
guard's deadline.

**First, the two-minute smoke on the cheapest card** (approved 2026-10-08, §0): prove
the create, the guard and SSH on the cheapest card in stock before renting the real
one. About 5 cents.

```sh
START=$(sh operations/pod/pod_start_command.sh 1 <sha>) &&
.venv/bin/python -m operations.pod.create_pod \
  --name verbatus-smoke --gpu "NVIDIA RTX A5000" \
  --image runpod/pytorch:1.4.0-cu1300-torch2130-ubuntu2404 \
  --container-disk-gb 20 --disk-gb 20 --cuda 13.0 \
  --start-command "$START"
```

(`--gpu "NVIDIA GeForce RTX 3090"` if no A5000 is in stock.) Expect: exit 0 and the
pod's JSON with `volumeMountPath` `/workspace/private`. Write its id down. Get its
address (step 2.2), then one command, and the delete:

```sh
ssh -o ConnectTimeout=20 -p <port> root@<ip> 'findmnt -o TARGET,FSTYPE /workspace/private;
  grep -c "armed for pod" /workspace/private/.pod_guard/guard.log'
runpodctl pod delete <smoke pod id>
```

Expect: the mounted target and a count of at least 1. Then `runpodctl pod list --all`
until it prints `[]` (a few tries, half a minute apart, in the background).

| You see | Do |
|---|---|
| exit 2, `nothing was created` | First `runpodctl pod list --all`: a transport timeout after the POST can still have created the pod (delete it if so, confirm). Then read the message (a GraphQL error names the field or the stock), fix and retry. |
| exit 3, `does not report the mounts` | The pod exists without what was asked. `runpodctl pod delete <id>`, confirm with `pod list --all`, and stop: tell the lead with the printed JSON. |
| `findmnt` shows no `/workspace/private`, or the count is 0 | Delete the pod, confirm it is gone, stop, tell the lead. |
| `pod get` shows no public ip/port, or `ssh` never connects | Delete it, confirm with `pod list --all`, create once more; a second miss is a stop. |

**Then pod W** (approved 2026-10-08, §0), the same way with the real card:

```sh
START=$(sh operations/pod/pod_start_command.sh 12 <sha>) &&
.venv/bin/python -m operations.pod.create_pod \
  --name verbatus-bakeoff-w --gpu "NVIDIA A40" \
  --image runpod/pytorch:1.4.0-cu1300-torch2130-ubuntu2404 \
  --container-disk-gb 100 --disk-gb 100 --min-vcpu 32 --cuda 13.0 \
  --start-command "$START"
```

(`--gpu "NVIDIA GeForce RTX 3090"` if that is the card from 2.0; `--datacenter <dc>` only
when 2.0 found stock in one place.) Expect: exit 0, JSON with `id` and `vcpuCount` 32 or
more. Write the id down as `<pod W id>`. The `&&` means a refused start command creates
nothing. Never create a pod without that start command: it arms the guard, with a
deadline 12 hours out.

- Refused for stock (a GraphQL error about instances): `pod list --all` first (above),
  then retry with `--min-vcpu 16`; then the other small card; if neither fits, read the
  stock again. If nothing is free anywhere: stop and tell the lead.
- `vcpuCount` below what was asked: `minVcpuCount` was not honoured. Delete the pod,
  confirm it is gone, and create again; a second miss is a stop.
- No public ip/port or no SSH: as in the smoke table.
- If `pod_start_command.sh` prints nothing and exits 2: nothing was created; read its
  message, fix, retry.

**Fallback: a network volume (one datacenter).** Only when the lead says so. Create a
network volume with the connector's `create-network-volume` (name
`verbatus-bakeoff-<date>`, datacenter `<dc>` from the stock reading, 100 GB for pod W,
300 GB for pod R; it bills for storage until deleted, so ask the lead afterwards whether
to keep it), then the same create with `--network-volume <volume id> --datacenter <dc>`
in place of `--disk-gb`. The queues run the same way.

### 2.2 Get the SSH address

```sh
runpodctl pod get <pod W id>
```

Find the public IP and the port mapped to 22; they are `<ip>` and `<port>` below. Use the
direct form `ssh -p <port> root@<ip>`; the `ssh.runpod.io` proxy cannot run commands or
`scp`. The pod may take a minute or two to answer; retry the first `ssh` a few times.

### 2.3 Check, then set up the pod (one command each, from the Mac)

```sh
ssh -p <port> root@<ip> 'findmnt -o TARGET,FSTYPE /workspace/private;
  grep -c "armed for pod" /workspace/private/.pod_guard/guard.log;
  nproc; df -h /workspace/private | tail -1'
```

Expect: the mount, a count of at least 1, `nproc` 32 (or 16), and about 100G free.
**If the count is 0 or the mount is missing: stop.** Delete the pod
(`runpodctl pod delete <pod W id>`), confirm with `runpodctl pod list --all` that it is
gone, and tell the lead.

Arm the guard's phone pings (the topic goes over stdin, never a command line), then the
clone and the runtime (about 5 minutes; run in the background and read its output file):

```sh
sed -n 's/^NTFY_TOPIC=//p' private/ntfy.conf | tail -n 1 | tr -d "\"'" |
  ssh -p <port> root@<ip> 'umask 077 && mkdir -p /workspace/private/.pod_guard && cat > /workspace/private/.pod_guard/ntfy_topic && wc -c < /workspace/private/.pod_guard/ntfy_topic'
ssh -p <port> root@<ip> 'set -e; git clone -q https://github.com/MegaSlick/Apparatus-Verbatus-Alpha /opt/verbatus &&
  cd /opt/verbatus && git checkout -q --detach <sha> && bash operations/pod/prepare_runtime.sh &&
  UV_CACHE_DIR=/tmp/verbatus-uv-cache uv sync --frozen --group pod && mkdir -p /workspace/private/bakeoff && echo SETUP-OK'
```

Expect: a byte count (the topic's length plus one) and `SETUP-OK` as the last line. Then
the pages and the manifest (to the pod's disk, never to git; the
`/.` form does not nest on a retry):

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

Expect: `ok: witness-24gb, …`, then the dry run with its estimate for this pod's CPUs and
a last line naming the copy from `/workspace/private/bakeoff/witness-cache` to the
manifest's `sync_to` and ending `end pod: none`. If the estimate shows a day long enough
to pass $15 (step 5): stop and tell the lead before launching.

The manifest's `sync_to` is a second copy on the pod's own disk, made and verified once
at the end. `--keep-pod` keeps the pod at the end (`end_pod = none`) so the results come
home over SSH from that disk (step 6) before the session deletes it. Without it the
queue would delete the pod once the copy verified, taking the results with it.

Launch, with the phone topic on stdin and the pod id checked first (the queue cannot end
a pod without `RUNPOD_POD_ID`; it is in PID 1's environment when the shell lacks it):

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

Expect: `launched pod <pod W id>`. If it says `not launched`: nothing runs; fix the
cause (a pod without `RUNPOD_POD_ID` in PID 1's environment is a stop: tell the lead).
Then one look at the first minutes, and leave the pod alone:

```sh
ssh -p <port> root@<ip> 'tail -n 20 /workspace/private/bakeoff/witness-cache/events.jsonl; tail -n 5 /workspace/private/bakeoff/queue-witness-24gb.log'
```

Every arm first reads two pages (its smoke run). A failed smoke stops that arm only, not
the queue.

## 3. While pod W runs: watch, nothing else

On the Mac, in the background (it runs for hours; read its output file):

```sh
.venv/bin/python -m operations.bakeoff.queue_runner watch --ssh "ssh -p <port> root@<ip>" \
  --status /workspace/private/bakeoff/witness-cache/status.json
```

It prints a line per change and exits 0 when the queue writes `DONE.json` (with
`--keep-pod` the expected last line says the pod is `kept (end_pod = none)`), 1 on failure. `watch --ntfy --queue witness-24gb` follows the phone pings
instead. Do nothing else on the pod: no `pgrep`, no `nvidia-smi` loops, no second launch.

| You see | Do |
|---|---|
| An arm errors or its smoke fails (phone: `Milestone`, `arm failed: ...`) | Nothing now. It is retried once at the end (a CPU arm in the CPU lane with the lane's whole thread share) and reported in `status.json` (`errors`, `finished_arms`). Arm failures never stop the day. Fix at home; rerun that arm alone on a later pod (cached pages are skipped). |
| An arm ends with `failed_pages` (a request timeout or a loop stop) | Nothing. Those pages are failed with their reason and not retried at the same settings; the reading is the finding. |
| The phone says `Needs a decision` | The queue cannot go on by itself (no pages, SIGTERM, or a pod it did not end): act on the message. |
| The phone says `Queue finished` | The queue is done, not the session: fetch (step 6), then delete the pod. |
| `watch` warns the status is 10 minutes old | Look once: `ssh -p <port> root@<ip> 'tail -n 5 /workspace/private/bakeoff/queue-witness-24gb.log; cat /workspace/private/bakeoff/witness-cache/status.json'`. If the queue is still running an arm, leave it. If the process is gone, tell the lead. Do not restart things from the Mac. |
| The queue ends `state: failed` | The queue found no pages (it returns before any pod handling: pod left up, no `end_action`), or the end copy did not verify. The pod is up: do step 6 (fetch from its disk), then delete it by hand and confirm it is gone. |
| The pod is gone but there is no `DONE.json` | The guard's deadline or a crash ended it, and the pod's disk went with it: nothing was copied anywhere else. Tell the lead. |
| The day's spend nears $15 | Tell the lead before it passes. |

## 4. Pod R (readers)

### 4.1 When to start it

Start pod R when chandra's smoke on pod W has passed: in pod W's `events.jsonl`, `chandra`
has a `queue-command-start` event with `"phase": "run"` and no `queue-arm-error`:

```sh
ssh -p <port> root@<ip> 'grep -c "\"arm\": \"chandra\".*\"phase\": \"run\"" /workspace/private/bakeoff/witness-cache/events.jsonl; grep -c "queue-arm-error.*chandra" /workspace/private/bakeoff/witness-cache/events.jsonl'
```

Expect `1` then `0`. The lead has approved the overlap; do not wait for pod W to finish.

### 4.2 Create and set up

As 2.1–2.3 (approved 2026-10-08, §0) with these changes: name `verbatus-bakeoff-r`,
`--gpu "NVIDIA RTX PRO 6000 Blackwell Server Edition"`, a 6-hour window
(`pod_start_command.sh 6 <sha>`), `--container-disk-gb 120 --disk-gb 300` (the 27B goes
through the store as a download cache, a staging copy and the final copy, about 167 GB
for one model, and the readers download 130.5 GB of weights in all), no `--min-vcpu`
(the reader queue has no CPU arms), the same `--cuda 13.0`. No smoke again: pod W's
create covered the route. Call its id `<pod R id>`; its address `<ip R>`, `<port R>`. Copy
`reader-96gb.toml` up instead of the witness manifest, and the pages too (each pod has
its own disk). The 2.3 expect line reads about 300G free; below 250G, stop and tell the
lead.

The blind arms resume from the 2026-10-08 caches; without them they run again (about
50 minutes of PRO 6000, about $2). Copy them up before launching:

```sh
ssh -p <port R> root@<ip R> 'mkdir -p /workspace/private/bakeoff/reader-cache'
scp -P <port R> -r private/bakeoff/witness-cache-all/qwen35-27b-blind private/bakeoff/witness-cache-all/qwen35-9b-blind root@<ip R>:/workspace/private/bakeoff/reader-cache/
ssh -p <port R> root@<ip R> 'ls /workspace/private/bakeoff/reader-cache/qwen35-27b-blind /workspace/private/bakeoff/reader-cache/qwen35-9b-blind | grep -c json'   # 150
```

The queue downloads the three Qwen snapshots itself at the cards' pinned commits in its
`prepare` steps, so pod R needs internet during those and no token (the repos are not
gated). Never put `HF_HUB_OFFLINE=1` in the launch command: the queue makes each arm's
command offline by itself. Validate, dry-run and launch exactly as 2.4 with

```sh
Q="--manifest /workspace/private/bakeoff/reader-96gb.toml --keep-pod"
```

and the log `/workspace/private/bakeoff/queue-reader-96gb.log`. Then `watch` as in step
3 with `--status /workspace/private/bakeoff/reader-cache/status.json` (and `--queue
reader-96gb` for the phone route). The same table applies.

## 5. The plan for each pod, and the tally

**Pod W.** GPU arms in order: chandra, dai, dai-conf10, dai-whole, churro, chandra-native,
churro-native, dots-mocr, surya-rec-surya, party-blla. CPU arms (the line sources, then
kraken, PyLaia) run beside them. `queue_runner run --dry-run` prints each arm's box and
lane. Cut rules if the day runs long: Party is cut first (`overrun`), the Surya recogniser
at 30 minutes behind schedule (`behind-schedule`), dots.mocr only if its install fails;
the baselines and the CTC line arms are never cut. Nothing is killed on a clock.

**Pod R.** qwen38-27b-vendor, qwen35-27b-vendor, qwen35-9b-vendor, then the two blind arms
from the copied caches (pages already cached are skipped). About 2.8 hours including the
downloads.

**The tally.** Spend is price per hour × hours, per pod, added up; the pods overlap in
time but each bills on its own. Prices are the ones read in 2.0 (the cheat sheet
`operations/pod/RUNPOD.md` carries the last seen: A40 $0.59/h, RTX 3090 $0.50/h, PRO 6000
$2.49/h on 2026-10-08; `config/pod_placement.toml`'s figures are older and not for
this). Pod W runs about 3.6 h with 32 vCPU or 7.2 h with 16 (the dry run says which):
$2.10–4.25 on the A40. Pod R about 2.8 h: about $7. Smoke and fetch pods: cents. A pod W
rerun at 16 vCPU would add about $4.25. The disks bill with their pods and end with them.
**If the tally would pass $15, stop and tell the lead.**

## 6. Bring the results home, then delete the pods, then confirm

With `--keep-pod` each queue ends with `DONE.json` written and the pod kept. When `watch`
has exited 0, fetch from the pod's own disk (in the background; minutes), which checks
every file against the pod's `DONE.json`:

```sh
.venv/bin/python -m operations.bakeoff.queue_runner fetch --ssh "ssh -p <port> root@<ip>" \
  --remote /workspace/private/bakeoff/witness-cache --into private/bakeoff/witness-cache-$(date +%F)
.venv/bin/python -m operations.bakeoff.queue_runner fetch --ssh "ssh -p <port R> root@<ip R>" \
  --remote /workspace/private/bakeoff/reader-cache --into private/bakeoff/reader-cache-$(date +%F)
```

Expect: each ends with every file verified. Only then delete that pod, and confirm, never
assume:

```sh
runpodctl pod delete <pod W id>
runpodctl pod delete <pod R id>
runpodctl pod list --all          # []
runpodctl pod get <pod W id>      # an error: not found
runpodctl pod get <pod R id>      # an error: not found
```

and check that billing has stopped in the RunPod console. If a pod is still listed:
`runpodctl pod delete <id>`, then list again.

A pod that died before the fetch took its disk with it, and the queue's copy under
`witness-cache-home/` or `reader-cache-home/` is on that same disk. Nothing else holds
the results, which is why the fetch comes before the delete.

Once both caches are home, tell the lead where they are. A copy on a global volume
needs a pod the lead deploys from the console with the volume attached
(`operations/pod/RUNPOD.md`); the session then copies over SSH.

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
| kraken-* (ppocrv6, mccatmus, mcfondue; -blla / -surya) | | | | | | | |
| pylaia-belfort-* / pylaia-popp-* | | | | | | | |
| party-blla | | | | | | | |
| surya-rec-surya | | | | | | | |
| qwen*-blind / qwen*-vendor | all (reference) | | | | | | reader |

Verdict words: `roster` (earns a 4th-witness arm on a type, plan 4.5), `keep as reference`,
`drop`, with the page type beside it. Hard pages are inside every number and also listed
on their own, so each model's weak spots show.

Report to the lead in plain words, phone-sized: which models did well on which page types,
which failed and how, what it cost, and that both pods are confirmed gone.

## 9. Perlector build bake-off

Question: does a smaller or faster build of the Perlector's model (FP8, FP8 with MTP,
NVFP4, optionally Qwen3.5-27B) read pages as well as the bf16 build that the pipeline
serves? One fresh pipeline run supplies the requests; every build is sent those same
requests and scored at home. **This costs money: the session that runs it sets the budget
with the lead; the figures here are estimates.** The queue alone plans 6.5 hours on the
96 GB card (`queue_runner run --dry-run` prints it), on top of the fresh run.

1. **Fresh run.** Create and set up a 96 GB pod as in 4.2, but with a guard window long
   enough for the whole day: the fresh run, the 6.5-hour queue, the downloads and the
   fetch, about 10 hours (`pod_start_command.sh 10 <sha>`; the session sets the window with
   the budget). The 6-hour window of 4.2 would delete the pod partway through the queue.
   Then `pod_run --from door --to perlector` with the Perlector served bf16
   (`operations/pod/README.md`, "`pod_run.py`"). Do not use an older run: its requests
   were served under other settings. Keep the whole run folder intact, named by
   its run id, at `/workspace/private/runs/<run id>` (copy it there with `cp -a` if the
   run wrote it elsewhere): the stage seals are read through that name. Keep the pod.
2. **Check the replay.** Put the run's page images in `/workspace/private/bakeoff-pages`
   (same file stems as the run's pages), and the manifest on the pod with the run id in
   it: `sed 's/RUN_ID/<run id>/' operations/bakeoff/queue/perlector-fed-96gb.toml >
   /workspace/private/bakeoff/perlector-fed-96gb.toml`. Then
   `.venv/bin/python -m operations.bakeoff.fed_arm prompts --run-tree /workspace/private/runs/<run id>`
   must print `whole requests byte-identical N of N` with N the run's page count and no
   `config differs` line. Anything else: stop and tell the lead.
3. **Run the fed queue**, validate, dry-run and launch exactly as in 2.4, with
   `Q="--manifest /workspace/private/bakeoff/perlector-fed-96gb.toml --keep-pod"`
   and the log `/workspace/private/bakeoff/queue-perlector-fed-96gb.log`; watch as in
   section 3 with `--status /workspace/private/bakeoff/fed-cache/status.json`. Arms run in
   this order: bf16-a, bf16-b, fp8, fp8-mtp3, nvfp4, qwen35. Each asks for the model name
   the run recorded, sealed sampling, on the pipeline's serving row for this card. A
   build that will not start (MTP, NVFP4 are unproven here) fails alone and the rest go on.
4. **Fetch, delete, confirm** as in section 6: `queue_runner fetch --remote
   /workspace/private/bakeoff/fed-cache --into private/bakeoff/fed-cache-<date>`, and
   bring the run folder home as well (`scp -r`, or `verbatus fetch-run`), because scoring
   reads it. Then delete the pod and confirm it is gone.
5. **Score at home**, with `fed_score` (README, "The Perlector scorecard"). First bf16-a
   against bf16-b (`--answers .../bf16-a --compare .../bf16-b --names bf16-a,bf16-b`): that
   is the noise floor. Then each build against bf16-a. A difference no larger than the
   bf16-a-versus-bf16-b difference is not a finding. Read answer health beside the reading: malformed pages,
   pages repaired before parsing, loop stops, errors.

What this does not measure: the pipeline sends a page one more time (a re-ask) when its
first reading is held, and the replay sends the first reading only. And bf16 A against B
uses the same seed, so it measures how much the engine varies from run to run, not how
much a different sampling draw would change the reading.
