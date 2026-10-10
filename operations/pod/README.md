# Paid infrastructure: pods, GPUs and anything that bills

Read this before running anything that can start a meter: RunPod, any GPU host, hosted
inference, or charged storage or egress. [RUNPOD.md](RUNPOD.md) says which tool to use for
what and lists known traps.

## The rule

Who may start, switch or delete paid infrastructure is set in `AGENTS.md`, "Who decides":
no billing action without the project lead's permission in the current session, and a
permission never carries over from an earlier one.

Reading costs nothing and is always allowed: listing pods, reading status and billing.
"Is anything running right now?" is worth asking unprompted.

A pod bills by the hour for as long as it exists, idle or not, and a session that starts
one and then dies leaves it running. That is why these rules are stricter than any other.

## Shutdown is verified, never inferred

An acknowledgement, a zero exit code or a "terminated" log line is not a shutdown. A close
is green only when:

- the provider says the same pod is gone (exact-pod GET returns 404 **and** the pod is
  absent from the pod list), and
- the provider returns non-empty billing records for that exact pod, in a declared window
  from the pod's creation through the requested cutoff, with every dated record inside it.

An empty, unreachable, misattributed, narrowed or stale billing answer is *unverified*,
never zero. Billing lags, so nothing here claims "no future charges". A shutdown that
cannot be verified is reported now, not left for the next session.

Two gaps in that proof wait on the first live run through the pod CLI (see
[Open items](#open-items)): whether RunPod bills anything before `createdAt` (04-7), and
whether the returned billing buckets fill the declared window (04-9).

## Two ways to run a pod

| Route | What starts and ends the pod | Status |
|---|---|---|
| [The hand route](#the-hand-route-a-run-started-by-hand) | `runpodctl` and SSH from the laptop; the [pod guard](#the-pod-guard) ends it | Used for every paid run |
| [The pod CLI](#the-pod-cli) | `python -m operations.pod.cli` with a lease, laptop supervisor and pod-side timer | Fake-proven only; never run live |

Both run the pipeline on the pod with [`pod_run.py`](#pod_runpy-running-the-pipeline-on-a-pod)
on top of [`bootstrap_main.py`](#bootstrap_mainpy-bootstrap-and-hold).

## The pod guard

`pod_guard.sh` runs on the pod and watches that same pod, so a crashed session, a closed
app or a sleeping laptop cannot leave a pod billing unnoticed. It needs nothing from the
laptop. Two switches in `config/spend.toml`, both committed off, decide what it may delete:

- **`pod_budget`**: off, the pod has no deadline unless started with a number of hours.
  On, the deadline is the soft maximum and a backstop deletes the pod at the hard maximum.
- **`ladder_delete`**: whether the idle ladder's last step deletes the pod. Off, it only
  warns.

**The deadline.** When there is one, the guard deletes the pod when it passes, whatever
the run is doing. A deadline more than a week out is taken as a typo and ignored (the phone
hears once per ignored value). A guard started with `off` honours a deadline written to the
file later, by the lead or by `pod_run --no-hold`. A value already in the file at start is
left over from an earlier start and is not honoured.

**The idle ladder.** The pod is idle while it does no work: GPU under 5 % at every
one-minute sample (a GPU that cannot report counts as busy), container CPU under half a
core (from the container's own cgroup), download under 256 KB/s, and no touch of the
keep-alive file. While `pod_run` is running, its own progress line replaces those counters:
a `progress-<pod id>` line from the last five minutes that says `ok` is work; anything else
is idle time counted from its last `ok`. Only `stalled` can reach the delete step; `slow`
and `bootstrapping` get warnings and the backup but are never deleted. A line that is
older, garbled or missing leaves the counters to decide.

| Idle for | Step |
| --- | --- |
| 15 min | one warning notice |
| 30 min | an urgent notice (ntfy `Priority: urgent`), repeated every 10 min |
| 1 h | copies the paths listed in `backup-<pod id>` to `/workspace/private/runs-guard-backup/<name>-<epoch>/` and checks each with `diff -rq`; a listed path that is missing counts as a failed backup |
| 2 h | deletes the pod, only with `ladder_delete = "on"`, only when the backup verified or there was nothing to back up, and never while the progress line says `slow` or `bootstrapping`; otherwise one more urgent notice says why not |

Any work, or a touch of the keep-alive, restarts the ladder (with a "work resumed" notice
if a warning went out). The latest step is in `alert-<pod id>` as `<epoch> <step> <detail>`.
The step times are `POD_GUARD_WARN_SECONDS`, `POD_GUARD_URGENT_SECONDS`,
`POD_GUARD_URGENT_REPEAT`, `POD_GUARD_BACKUP_SECONDS` and `POD_GUARD_DELETE_SECONDS`;
`POD_GUARD_DELETE=on` is `ladder_delete`, passed in by the start command.

`pod_run` writes `backup-<pod id>` (the run's local tree and its volume run directory) and
removes it once the run and its final sync are done; a failed final sync leaves it so the
guard copies the local tree. It also writes the progress line as
`<epoch now> <epoch last ok> <ok|slow|stalled> <check> <detail>` on every liveness tick and
touches the keep-alive file when the check says `ok`. CPU time is not progress, because an
idle model server uses a little CPU on every tick. During the bootstrap and the final sync
a thread writes `bootstrapping bootstrap <step> for <seconds>` and `ok final-sync`; each
counts as work for an hour per step and never reaches the delete, so a long copy is never
cut off.

An unreadable CPU counter never deletes a pod: the guard counts the pod as busy, retries
every minute and sends one notice (and one more when the read returns; at most one an hour
for a counter that keeps dropping out). A single missed read between good ones is skipped.

To delete, the guard uses the pod's own `runpodctl` and pod-scoped `RUNPOD_API_KEY`. It
keeps asking until the pod is gone, falls back to stopping it, and never deletes a network
volume.

### Arming the guard at creation

Arm it through the pod's start command, so it runs even if SSH never comes up.
`pod_start_command.sh <hours|off> <sha>` prints that command: it fetches the guard from this
public repository at a pinned commit (ten tries, 30 s apart), starts it, then hands over to
the image's `/start.sh`. It reads `pod_budget` from the checkout's `config/spend.toml`, or
from `VERBATUS_POD_BUDGET` when set.

| Budget | Argument | Result |
|---|---|---|
| off | `off` | no deadline file (an old one is removed), no backstop |
| off | `<hours>` | a deadline `<hours>` from container start, and a backstop that deletes the pod an hour after the deadline on the volume (so extensions count), even if the guard never ran |
| on | `<hours>` | as above, and the backstop also deletes at the hard maximum; the guard's window is cut to end two minutes inside it |
| on | `off` | refused |

The hard maximum is the sealed `VERBATUS_HARD_MAX_SECONDS` when set, else `hard_max_seconds`
in `config/spend.toml`, counted from when the command is printed. Print a fresh command for
every pod, on a laptop whose clock is set automatically. The script also refuses `<hours>`
of zero, `<hours>` past the hard maximum, an unreadable hard maximum, and an unreadable
`pod_budget` or `ladder_delete`. It exits 2 and prints nothing when it refuses, so always
chain it with `&&`; an inline `--docker-args "$(...)"` would create the pod with no guard:

```sh
START=$(sh operations/pod/pod_start_command.sh <hours|off> <sha>) &&
runpodctl pod create ... --volume-mount-path /workspace/private --docker-args "$START"
```

`<sha>` is a full 40-character commit on `main` that carries the guard. The network volume
must be mounted at `/workspace/private`, the one path the bootstrap and data gate accept
(`models.POD_VOLUME_MOUNT_PATH`). The guard's files live in
`POD_GUARD_DIR=/workspace/private/.pod_guard`, so they survive the pod.

### Using the guard

- **A long quiet wait that is still wanted:** touch
  `/workspace/private/.pod_guard/keepalive-<pod id>` to restart the ladder.
- **A deadline, or more time:** write the deadline (epoch seconds) to a temporary file and
  move it over `/workspace/private/.pod_guard/deadline-<pod id>`. A deadline moved earlier
  ends the pod on the next tick. A pod restarted with `<hours>` keeps its old deadline file,
  so write a new one when the lead approves more time.
- **Records:** `.pod_guard/guard.log`. `pod_run --no-hold` writes `released-<pod id>` (run id
  and outcome) before moving the deadline, and the guard quotes it in its ping
  (`... pod_run reported: run <id> ended complete`). A ping without `pod_run reported` means
  the pod ended without `pod_run` finishing. The run's own state is in
  `pod-run-report-<run id>.json` on the volume.
- **Heartbeat:** the guard touches `.pod_guard/heartbeat-<pod id>` every tick.

### Arming the ping

The guard pings with a topic in `/workspace/private/.pod_guard/ntfy_topic`: at each ladder
step, each ignored deadline value, and when it deletes, fails to delete, or stops the pod
instead. The topic is the bearer secret described in
[`operations/notify/README.md`](../notify/README.md); it never enters git, a command line or
a note. Send it over SSH's standard input from the laptop's ignored `private/ntfy.conf`:

```sh
sed -n 's/^NTFY_TOPIC=//p' private/ntfy.conf | tail -n 1 | tr -d "\"'" |
  ssh -p <RUNPOD_TCP_PORT_22> root@<RUNPOD_PUBLIC_IP> 'umask 077 && mkdir -p /workspace/private/.pod_guard &&
    cat > /workspace/private/.pod_guard/ntfy_topic'
```

The guard reads it on every ping, so it can be written after the pod starts. It stays on the
volume for later pods; delete it with the volume or when the topic is rotated. Use the pod's
direct port (`$RUNPOD_PUBLIC_IP`, `$RUNPOD_TCP_PORT_22`), never `ssh.runpod.io`: that proxy
ignores the command and runs standard input as a shell, so the topic would be typed into it.

### Limits of the guard

- Not yet observed on a live pod: how RunPod passes `--docker-args` with an image
  ENTRYPOINT, whether `RUNPOD_POD_ID` is in PID 1's environment, whether the pod-scoped key
  may delete its own pod, and the cgroup and `nvidia-smi` readings inside the container.
  Network-volume reads may not show as network traffic, so a slow weight load at low CPU
  could look idle.
- The guard is a backstop, not the shutdown. Close pods yourself when work ends and verify
  against RunPod's state and billing. `session_end_pod_check.sh`, a Claude Code SessionEnd
  hook, pings the lead with every pod that still exists when a session closes. A missing
  `runpodctl`, a failed or unrecognised listing, an empty listing and one that takes over
  30 s are pinged too, never read as "no pods". The same report is not resent within two
  hours.

## The hand route: a run started by hand

The lead starts a run from the laptop with `runpodctl` and SSH, without the pod CLI. The pod
guard is then the only thing that ends the pod, so it is armed at creation. **The card, the
hours and the spend are the lead's decision**; this section says what the code needs.

**What a run needs.** The 27B Perlector needs the `generic-80gb-plus` tier in
`config/pod_placement.toml`; its one reviewed card is `NVIDIA RTX PRO 6000 Blackwell Server
Edition` (96 GB). Container disk at least 120 GB on that card
(`models.BIG_CARD_CONTAINER_DISK_GB`), 100 GB on a smaller one
(`models.DEFAULT_CONTAINER_DISK_GB`). The network volume mounted at `/workspace/private`.
A card serves one chair at a time. Work can be split: a 24 GB witness card runs Door through
Attestatores, the big card runs the Perlector and the Coniector, and Recensor onward needs
no GPU.

### Set up the Mac (free)

macOS 13 or later, Intel or Apple silicon (pypdfium2 ships wheels only for macOS 13+).
git needs the Xcode Command Line Tools (`xcode-select --install`). Their `python3` is too
old, so run Python only as `uv run …` or `.venv/bin/python …`. uv must be exactly the
version `pyproject.toml` requires (`[tool.uv] required-version`, 0.12.1).

```sh
curl -LsSf https://astral.sh/uv/0.12.1/install.sh | sh
git clone https://github.com/MegaSlick/Apparatus-Verbatus-Alpha
cd Apparatus-Verbatus-Alpha
uv sync --frozen --group test --group audit
sh .githooks/install.sh
sh .githooks/check-static.sh
source .venv/bin/activate          # once per Terminal window; puts `verbatus` on PATH
verbatus spend show
```

`runpodctl` 2.x (`brew install runpod/runpodctl/runpodctl`) needs the account's API key;
`upload` and `fetch-run` need the RunPod S3 keys. Keep them in the shell only, never in a
file here (`runpodctl config --apiKey` also leaves the key in shell history):

```sh
read -rs RUNPOD_API_KEY; export RUNPOD_API_KEY
read -rs RUNPOD_S3_ACCESS_KEY; export RUNPOD_S3_ACCESS_KEY
read -rs RUNPOD_S3_SECRET_KEY; export RUNPOD_S3_SECRET_KEY
runpodctl gpu list
```

### Before renting (free)

1. **Put the page images on the volume.** Clear Finder's `.DS_Store` files first (the Door
   refuses them), then seal and send them from `private/`:

   ```sh
   verbatus --state-dir private/verbatus-state upload --source private/<pages> \
     --manifest-out private/<pages>-manifest.json
   verbatus upload --source private/<pages> --sealed-manifest private/<pages>-manifest.json \
     --network-volume DATACENTER:VOLUME_ID
   ```

   The first seals the folder to a local copy, which a local `verbatus run` can check at
   the Door for free; the second writes `submission/` and `submission-manifest.json` at the
   volume root. A second set on the same volume needs `--prefix` (`--prefix spreads` writes
   `spreads/` and `spreads-manifest.json`). Prepared spreads travel with their triage
   documents (`operations/operator/README.md`, "Sending prepared scans to a pod").
2. **Prove the S3 path home.** With the two S3 key variables set:

   ```sh
   verbatus fetch-run --run-id s3-path-check --into /tmp/verbatus-s3-check \
     --network-volume DATACENTER:VOLUME_ID
   ```

   The expected answer is a refusal naming `nothing is stored under 'runs/s3-path-check/'`.
   Fix any other failure before renting.
3. **Pick `<sha>`**: the full 40-character commit on `main` to run. A short hash arms the
   guard and then fails the bootstrap.

### Create the pod

From a checkout at `<sha>`:

```sh
START=$(sh operations/pod/pod_start_command.sh <hours|off> <sha>) &&
runpodctl pod create \
  --name verbatus-<run id> \
  --image runpod/pytorch:1.4.0-cu1300-torch2130-ubuntu2404 \
  --gpu-id "NVIDIA RTX PRO 6000 Blackwell Server Edition" --gpu-count 1 \
  --cloud-type SECURE \
  --data-center-ids <DATACENTER of the volume; one id only> \
  --network-volume-id <VOLUME_ID> \
  --volume-mount-path /workspace/private \
  --container-disk-in-gb 120 \
  --ports "22/tcp" \
  --docker-args "$START"
```

- The image must carry CUDA 13.0: on a Blackwell card FlashInfer compiles a kernel at first
  engine start and needs `nvcc` 12.9 or newer. A `cu1281` image fails every vLLM chair with
  `FlashInfer requires GPUs with sm75 or higher`.
- `runpodctl` 1.x has no `pod` subcommand. `runpodctl pod get <pod id>` shows SSH details.
- The SSH proxy (`ssh.runpod.io`) ignores a command on the line and cannot carry `scp`. Use
  the direct port (`ssh -p $RUNPOD_TCP_PORT_22 root@$RUNPOD_PUBLIC_IP`), which works even
  when the pod record's `ssh.direct` is empty.
- **No card where the volume is?** A pod with its own disk (`mounts.persistent`) at
  `/workspace/private` passes the mount check, but **that disk is deleted with the pod**.
  Launch without `--no-hold`, copy results home the moment the run ends, then delete the
  pod. Or use a global volume (below).

### A global volume for results

A RunPod global volume is object storage any datacenter can mount, so a pod can go where its
card has stock and still leave its results behind. It has no permission bits, atomic rename
or locking, so it holds only a run's final copy; everything else stays on the pod's own disk
at `/workspace/private` (deleted with the pod). How to attach one, and what does not work, is
in [RUNPOD.md](RUNPOD.md), "Attaching a global volume". `operations/pod/create_pod.py
--global-volume <id> --global-mount <path>` checks the attachment after create and exits 3
when it is missing.

### On the pod, over SSH

```sh
findmnt /workspace/private                  # the network volume, not a plain directory
tail /workspace/private/.pod_guard/guard.log # "armed for pod <id>: deadline ..."
echo "$RUNPOD_POD_ID"                       # must print the pod id
cat /workspace/private/.pod_guard/deadline-$RUNPOD_POD_ID   # none with `off`

git clone https://github.com/MegaSlick/Apparatus-Verbatus-Alpha /opt/verbatus
cd /opt/verbatus && git checkout --detach <sha>
bash operations/pod/prepare_runtime.sh
UV_CACHE_DIR=/tmp/verbatus-uv-cache uv sync --frozen
```

**No "armed for pod" line, or no deadline file on a pod started with `<hours>`: stop.**
Delete the pod (`runpodctl pod delete <pod id>`), confirm it is gone, and find out why
before renting again. (Started with `off`, the line reads `deadline none (off)`.)

If `RUNPOD_POD_ID` is empty in the SSH shell, take it from the container's first process,
never by hand:
`export RUNPOD_POD_ID=$(tr '\0' '\n' </proc/1/environ | sed -n 's/^RUNPOD_POD_ID=//p')`.

Launch detached, so a dropped SSH session cannot kill it. The bootstrap's hard deadline
comes from the guard's; a pod started with `off` has none (`VERBATUS_HARD_DEADLINE=none`,
accepted only with `--no-hold`):

```sh
V=/workspace/private RUN=<run id> R=/opt/verbatus
GUARD_DEADLINE=$(cat $V/.pod_guard/deadline-$RUNPOD_POD_ID 2>/dev/null)
if [ -n "$GUARD_DEADLINE" ]; then
  VERBATUS_HARD_DEADLINE=$(date -u -d "@$(( GUARD_DEADLINE - 300 ))" +%Y-%m-%dT%H:%M:%SZ)
else
  VERBATUS_HARD_DEADLINE=none
fi && export VERBATUS_HARD_DEADLINE &&
cd $R && setsid nohup $R/.venv/bin/python -m operations.pod.pod_run \
  --report-path $V/pod-run-report-$RUN.json \
  --run-id $RUN \
  --submission-folder $V/submission \
  --submission-manifest $V/submission-manifest.json \
  --mechanics-qualification \
  --no-hold \
  --notify \
  --hourly-usd <the card's price per hour plus the volume's> \
  -- \
  --volume-mount-path $V \
  --report-path $V/bootstrap-report-$RUN.json \
  --repository $R \
  --repository-commit <sha> \
  --lockfile $R/uv.lock \
  --journal $V/bootstrap-journal-$RUN.json \
  --store-root $V/model-store \
  --models-config $R/config/models-real.toml \
  --serving-recipes-config $R/config/serving_recipes_real.toml \
  > $V/pod-run-$RUN.out 2>&1 < /dev/null &
```

- **No selection** runs the full sequence; it stops at a held Recensor.
- **`--store-root`** names the model store on the volume. A new root downloads the weights
  of the chairs this pod uses during the paid bootstrap.
- **Another prefix:** `--submission-folder $V/spreads --submission-manifest
  $V/spreads-manifest.json`; for prepared spreads also
  `--triage-decision-manifest $V/<prefix>-triage-decision-manifest.json
  --triage-producer-recipe $V/<prefix>-triage-producer-recipe.json`.
- **Two cards:** the same run id, submission and `--store-root` on both. The witness pod
  adds `--models small` before `--` (24 GB card, `--container-disk-in-gb 100`); the big pod
  adds `--from perlector --to coniector`. Recensor onward runs off the GPU from the fetched
  tree.
- **One card, released after the Coniector:** add `--stop-after-coniector` before `--`.
- A second pod on the same run id may reuse these paths: the journal records which pod
  wrote it, and another pod renames it to `bootstrap-journal-$RUN.pod-<old pod id>.json`
  and bootstraps afresh.
- A gated Hugging Face model needs its token in the environment and `--keep-env HF_TOKEN`
  in the bootstrap half, never on the command line.
- A refusal or red bootstrap leaves the pod up: read the report, fix, relaunch.
- **Nothing stops the run before the guard's deadline.** If the window runs out mid-run,
  the guard deletes the pod with the stage in flight and its work is lost. Size `<hours>`
  with margin, and write a new deadline file before the old one passes when the lead
  approves more time.

### Watching it

`verbatus watch` reads local copies of the report files, so copy fresh ones in a loop over
the direct SSH port:

```sh
mkdir -p ~/verbatus-watch
while :; do
  scp -q -P <RUNPOD_TCP_PORT_22> "root@<RUNPOD_PUBLIC_IP>:/workspace/private/pod-run-report-<run id>{.json,-liveness.json,-timings.json,-estimate.json,-progress.json}" ~/verbatus-watch/
  verbatus watch --run-id <run id> --receipts ~/verbatus-watch
  sleep 60
done
```

`operations/operator/README.md` explains each line. On the pod:

```sh
cat $V/pod-run-report-$RUN.json          # state: bootstrapping, running, then the outcome
cat $V/pod-run-report-$RUN-liveness.json # last_seen keeps moving while it runs
cat $V/pod-run-report-$RUN-estimate.json # this stage's finish; at_risk
cat $V/pod-run-report-$RUN-progress.json # ok, slow or stalled, and why
tail -f $V/pod-run-report-$RUN-transcript.log
tail -f $V/pod-run-$RUN.out
tail -f $V/.pod_guard/guard.log
```

**The hard-failure cap.** `config/hard_failure.toml` halts the run at the next stage
boundary once more than two distinct (stage, subject) incidents carry a counted failure.
Counted: Door refusals for `corrupt` or `unreadable` pages, Designator and Recensor
`failed`, Archetypus `refused`, Perlector `failed` (including a failed re-ask call). A page
that was cut off, refused for capacity or did not parse is `held`, not counted. A halted run
exits 4, and every stage refuses to start while the cap is breached.

### When it ends, and bringing results home

With `--no-hold`, `pod_run` writes its final report, then moves the guard's deadline to now;
the guard deletes the pod within about a minute. `guard_release.guard_alive: false` in the
report means nothing is known to be watching the deadline: delete the pod by hand. Confirm
it is gone with `runpodctl pod list --all` (without `--all` stopped pods are hidden) and
`runpodctl pod get <pod id>`, and check billing in the console. The volume remains.

Then on the laptop:

```sh
verbatus fetch-run --run-id <run id> --into <local root> \
  --network-volume DATACENTER:VOLUME_ID \
  --evidence-prefix preflight/bootstrap-report-<run id> \
  --evidence-key pod-run-report-<run id>.json \
  --evidence-key pod-run-report-<run id>-transcript.log \
  --evidence-key pod-run-report-<run id>-liveness.json \
  --evidence-key pod-run-report-<run id>-timings.json \
  --evidence-key pod-run-report-<run id>-estimate.json \
  --evidence-key pod-run-report-<run id>-progress.json \
  --evidence-key bootstrap-report-<run id>.json \
  --evidence-key bootstrap-journal-<run id>.json \
  --evidence-key pod-run-<run id>.out \
  --evidence-key .pod_guard/guard.log
```

A run that held also has `pod-run-report-<run id>-hold.json`. Then:

```sh
verbatus review --run-root <local root> --run-id <run id>
verbatus export --run-id <run id> --run-root <local root>
verbatus backup --run-root <local root> --run-id <run id> --mac-directory <synced folder>
```

**Finishing Recensor through Armarium on the Mac.** These stages serve no chair. Run them
from the fetched tree with the same sealed configuration
(`operations/operator/README.md`, "Finish a pod run on this computer"):

```sh
.venv/bin/python pipeline/orchestrator/run.py --run-id <id> --run-root <local root> \
  --models-config config/models-real.toml \
  --serving-recipes-config config/serving_recipes_real.toml \
  --mechanics-qualification --from recensor --to armarium
```

Add `--corpus-register` if the run sealed one. A run held at the Recensor is reviewed and
decided (`verbatus review`, `verbatus decide`) and the same range run again.

## `pod_run.py`: running the pipeline on a pod

```sh
python -m operations.pod.pod_run <run flags> -- <bootstrap_main argv>
```

The argv after `--` goes through `bootstrap_main`, so every bootstrap refusal, probe, scrub
and deadline applies. After a green bootstrap it runs `pipeline/orchestrator/run.py` with
the pod's interpreter. The run root is `/var/tmp/verbatus-runs` on container disk under
`--no-hold`, `<volume>/runs` otherwise; `--run-root` names another approved root. Its
`pod-run-report.v1` at `--report-path` moves through `bootstrapping`, `running`, then
`complete`, `held`, `halted`, `failed`, `bootstrap-red` or `refused`.

| Exit | Meaning |
|---|---|
| 0 | the orchestrator returned `EXIT_COMPLETE`; never for a partial run |
| 2 | a named refusal before anything ran |
| 3 / 4 | held / halted (the orchestrator's own) |
| 5 | a red bootstrap step |
| 6 | the orchestrator could not start or exited outside its vocabulary |
| 7 | `--dry-run`: plan validated and printed, nothing ran |
| 8 | selected stages completed before Armarium; the timer closes the pod |

It refuses by name: no `--`; a `--hold-only` plan; a report path that is the bootstrap's or
lacks the launch token; a run root outside approved storage; a submission outside the
volume or missing; a policy or `--perlector-protocol-config` outside the repository or
unparseable; a resume whose Perlector protocol or run policy differs from what `run.json`
sealed; a bad run id; and the `--no-hold` refusals below.

**The data gate is asked first**, before a model is fetched. `config/data_handling_policy.json`
lists the pod volume's mount path beside the local `private/` root: the lead's standing
decision that a rented pod's volume is accepted exposure for the duration of a run. It also
admits `/var/tmp/verbatus-runs` as the hand route's working root. After each stage the
orchestrator copies new run files to `<volume>/runs/<run id>`, fsyncs and checks them;
`pod_run` repeats the sync at exit. A resume first copies the volume run back and refuses a
differing file. The sync never removes volume evidence. A sync failure fails the run; a
failed final sync also leaves the guard unreleased. Every report records
`approved_storage_roots` and `skipped_storage_roots`.

**Card facts.** `pod_run` forwards the `--placement-tier` measured by green PREFLIGHT and
the receipt's `--capacity-plan` to the orchestrator and records both. Neither is sealed. A
plan whose digest, tier or serving digests do not match the receipt is refused.

**Selections.** `--stage` runs one boundary; `--from` and `--to` an inclusive range
(`--to` alone is refused); none runs the full sequence. `--models small` runs Door through
Attestatores; `--models big` runs Perlector through Armarium after checking this run's
sealed Attestatores stage on the volume. All go before `--`. A selection preflights only the
chairs its stages use (the Designator's, the witnesses, the Perlector, and the Coniector's
`reconstructor` when `config/reconstruction.toml` has `mode = "on"`), and is refused unless
each has green PREFLIGHT evidence. Any range is the orchestrator's semi mode, which stops at
the first held stage. In every mode a held Recensor stops the run before Archetypus
(`pipeline/orchestrator/CONTRACT.md`, "A held Recensor stops every mode").

**Flags:**

- **`--stop-after-coniector`** ends the selection at the Coniector, the last stage that
  needs the card, so the GPU is released before the Recensor's CPU work. No selection
  becomes `--from door --to coniector`; `--models big` becomes `--from perlector --to
  coniector`; a range past the Coniector ends there; one starting after it is refused. The
  run ends `selection-complete` and returns at once.
- **`--mechanics-qualification`** is needed for any real-roster run: every row in
  `config/serving_recipes_real.toml` is `preflight_state = "unproven"`, and the stages
  refuse an unproven row without it. It is sealed into the run (every later selection or
  resume must pass it too) and proves no row.
- **`--perlector-protocol-config <path>`** (inside the repository) is sealed into the run's
  config digest, so every later selection or resume must name the same file. `pod_run`
  validates it with the Perlector's own loader before the bootstrap.
- **A resume is checked against its seal before the bootstrap**: the protocol must match
  the sealed `perlector-protocol` digest, and on a real run `--mechanics-qualification`
  must match the sealed `run-policy` digest.
- **`--no-hold`** is for the hand route. After the final report of any run past a green
  bootstrap, it returns and moves this pod's guard deadline
  (`<volume>/.pod_guard/deadline-<pod id>`) to now, so the guard deletes the pod within a
  minute. It writes `released-<pod id>` first. The pod id is read only from the container's
  first process (`/proc/1/environ`), because every pod's deadline sits on the shared volume
  and a wrong id would delete another live pod. Before the bootstrap it is refused when the
  first process names no pod id, when the shell exports a different id, when that pod's
  `heartbeat-<pod id>` is missing or older than five minutes, or under a launch token. With
  no deadline file and a live guard it writes one. It never moves a deadline later.
  `guard_release.released` says the deadline was written; `guard_alive` whether the guard
  was still fresh. A refusal or red bootstrap leaves the guard alone.
- **`--notify`** sends phone notifications (see [`notify_hooks.py`](#notify_hookspy-phone-notifications)).
- **`--hourly-usd`** names the rented price, used by the deadline-at-risk notice.

**Holding.** Without `--no-hold`, `pod_run` holds toward the hard deadline only for a full
run that is `complete`, or `held` after reaching a sealed export in this invocation (from
its own `--stop-record`), because the pod timer treats an early exit as non-green. Every
other outcome (a selection, a run held before export, `halted`, `failed`) returns at once
so the pod closes. The hold does no work, so the guard's idle ladder applies. A run with
`VERBATUS_HARD_DEADLINE=none` cannot hold and is refused without `--no-hold`.
`held_to_hard_deadline` in the report says which way it went.

### Its records

Beside the report, named from its stem. They are best-effort: a lost stopwatch never
abandons a completed run, but a missing transcript or liveness record holds the run for
review (`records_at_close`, `records_missing`).

| Suffix | Contents |
|---|---|
| `-transcript.log` | the orchestrator's and every stage's merged output: 8 MB of head live, then the final 1 MB after a truncation marker |
| `-liveness.json` | pid, tick, last seen. `alive: true` long before the deadline means the supervisor died while the child ran (OOM, teardown) |
| `-timings.json` | JSON lines, one per stage invocation (member, start, finish, duration, exit code, GPU use, Perlector concurrency, commit). Kept outside the run tree, which must stay byte-identical across reruns |
| `-hold.json` | the hold line after a finished run |
| `-estimate.json` | the current page-counted stage's finish estimate (Door to Perlector), once five pages and ten minutes have passed |
| `-progress.json` | `ok`, `slow` or `stalled` for the stage in progress (`progress_watch.py`, `pod-run-progress.v1`), plus `stage_rates` |

**Deadline at risk.** When the estimated finish plus 20 minutes passes the deadline that
ends the pod (the guard's `deadline-<pod id>`, else the bootstrap's hard deadline, else the
timer's), `pod_run` records a `deadline-at-risk` notice and, with `--notify`, sends it as a
`decision`. It names the spend maximums, the gap, the extra cost at `--hourly-usd` (or the
pod-timer launch's `VERBATUS_POD_HOURLY_USD` plus `VERBATUS_VOLUME_ONGOING_HOURLY_USD`), and
for a guard deadline one command that moves it to the projected end but never past the hard
maximum. It is sent once per deadline value, retried up to three times. Nothing here moves
the deadline. The report's `deadline_watch` keeps every notice.

**Progress.** A page-counted stage is `slow` when its pages over the last ten minutes fall
below half its expected rate, and `stalled` when no page has come for max(10 min, 3
expected page times), or before the first page, for its engine's startup timeout plus three
page times (30 min if unknown). The expected rate is the Perlector's
`planned_seconds_per_page` over `max_num_seqs`, Surya's `seconds_per_page` over `workers`,
or for other stages its own pace once measured. A stage not counted in pages is `stalled`
after 15 minutes with no transcript output and no new record in its run tree outside
`serving-logs`.

## `notify_hooks.py`: phone notifications

`--notify` gates every notification; without it each line is recorded and nothing sent. A
message with a credential shape or URL is never sent, and a failed ping never changes a
launch or close decision.

- The pod CLI sends one line at launch (lease, card, hourly ceiling), at close (lease,
  verified state, `billed Ns from creation`) and at each balance observation (create and
  adopt gates only). The balance hook is off by default; a provider without it is recorded,
  not refused.
- `pod_run --notify` sends the systemic alarm as a `decision` when a run stops with more
  pages held than its sealed review policy allows, or exports past that stop on a person's
  advance (`notify_systemic`). If no usable stop record was left, the report names why in
  `stop_record_problem` and the run is never `complete`. It also sends the deadline-at-risk
  notice.
- On a pod, `pod_run` reads the guard's topic file (`/workspace/private/.pod_guard/ntfy_topic`)
  and passes it as `NTFY_TOPIC` to that one notification command, with only `PATH` and proxy
  and CA variables beside it. The topic is never an argument, a log line, or part of a
  stage's environment. With no usable topic file nothing runs, and the report says "not sent
  (no usable guard topic)".

## `bootstrap_main.py`: bootstrap and hold

`python -m operations.pod.bootstrap_main` runs the journaled bootstrap, then **holds** to
`VERBATUS_HARD_DEADLINE`, re-journaling a liveness line each interval. Under the pod timer
any exit before the deadline, exit 0 included, is `completed-early` and closes the pod; a
red step exits non-zero at once. The journal is written to the volume. Steps, in order:

- **`REPOSITORY`** checks the [image contract](#the-pod-image-contract) first, then checks
  out the exact commit.
- **`CONFIGURATION`** parses the roster, catalogue and placement table and binds the three
  config paths and their seals into its receipt. `--serving-recipes-config` defaults to the
  fixture-only `config/serving_recipes.toml`; a real launch names `config/models-real.toml`
  and `config/serving_recipes_real.toml` together. A resume with a changed selection fails
  here. The placement table is always the checkout's own `config/pod_placement.toml`.
- **`CUDA_COMPAT`** records `nvidia-smi`'s driver and GPUs. Drivers below 580.65.06 on
  professional RTX or data-center cards get the pinned `cuda-compat-13-0=580.178.04-1ubuntu1`
  from the image's NVIDIA apt repository (no apt list refresh on a billing pod; missing
  lists or pin are a named refusal), checked by `cuInit(0)`, and
  `/usr/local/cuda-13.0/compat` goes first in `LD_LIBRARY_PATH`. A GeForce card with an older
  driver is refused. On every driver it then calls `cuInit(0)` and `cuDeviceGetCount` in a
  child process killed after 120 s and refuses the host by name when either fails or no
  device is counted. A missing library or call is refused as the image's fault.
- **`UV_ENVIRONMENT`** syncs the locked environment (`uv sync --group pod`, about 10 GB,
  once per pod). When a chair this pod prepares runs as a subprocess (Surya), it also builds
  `uv sync --locked --project operations/serving/surya` and counts its 14 GiB in the disk
  check. Meanwhile `chair_prefill.py` starts copying Hugging Face chairs into the cache on a
  background thread, largest file first, never evicting; anything that does not fit is left
  for PREFLIGHT.
- **`MODEL_STORE`** fetches and checks the selected chairs' artifacts in the volume store
  (Surya's bundle via `operations/serving/surya/prefetch.py`, refused unless its manifest is
  the pinned one). A store written before the roster gained an artifact is upgraded: each
  new artifact is added as `pending-fetch` and fetched, provided every existing artifact
  still matches (`common/chairs/README.md`). Final verification checks every present
  artifact's structure but hashes only bytes no chair copy will hash; `store_bytes` names
  which were hashed at boot, at fetch and at copy. `selection_complete` and
  `real_roster_complete` say what was present.
- **`CHAIR_CACHE`** places each chair: Hugging Face chairs are copied from the volume store
  to `cache_root/by-digest/<digest_manifest>` on container disk, verified as they are
  written (one pool sized from usable CPUs or `VERBATUS_IO_WORKERS`), evicting least
  recently used digests when the next fill would not fit. Chairs pinned to one manifest
  share one copy. Surya's bundle is copied to `config/real-models/designator_surya`. A
  mismatch is red and names the chair.
- **Transfer** is optional: no submission manifest is a vacuous success; a manifest with no
  configured target is a refusal.
- **`PREFLIGHT`** runs `ChairRegistry.ensure` for the selected roles in the order stages
  need them, then a [smoke read](#preflight) of each.

A pod given a stage selection (`pod_run`'s `preflight_roles`) prepares only the chairs those
stages use; a bare `bootstrap_main` run prepares every chair.

**Refusals before any action:** a journal or report path outside the mounted volume; a
lockfile that is not the checkout's `uv.lock`; a volume that fails a real write-and-read
probe; a missing hard deadline; a credential-looking argv token; an unknown or unparseable
argument (named by flag, never by value). The environment is scrubbed of credential-shaped
variables except an explicit `--keep-env` allowlist.

**`--dry-run`** validates and prints the plan. **`--hold-only`** is a drill: no bootstrap
steps, a `hold-only` record, hold to the deadline. There is deliberately no fake-actions
flag. `test_bootstrap_main.py` runs only fakes.

### Preflight

`preflight.py` measures CUDA, driver, capability, VRAM and disk, selects a plan from
`config/pod_placement.toml`, verifies each selected chair, and reads a proof page.

- **Serving is sequential**: one model on a card at a time (the Coniector adopts the
  Perlector's running server). Tiers differ in memory fraction, context cap, pixel cap,
  batch size and `planned_batch_ceiling`. PREFLIGHT derives a capacity plan from the
  measured card (`operations/serving/capacity.py`): how many sequences each chair runs at
  once, never fewer than its row's `max_num_seqs` (`operations/serving/README.md`,
  "Scaling to the card"). The receipt records `environment.vram_gib`, `gpu_count`,
  `compute_capability` and `capacity_plan` (null when the card could not be measured).
- **The smoke read** goes through the production serving seam. The witness value is drawn
  from the CSPRNG on the pod and rendered onto a golden page under
  `<volume>/preflight/<report stem>/` just before the read, so it was never in a file or
  prompt. The DAI chair instead reads a pinned public RecordGold record, scored by character
  error rate (`operations/serving/recordgold_smoke.py`). While one chair smokes, a background
  thread fills the next chair's cache. Receipts, launch audits and evidence land in the same
  directory. `--fixture` with `--page-witness-file` supplies a golden page instead.
- **A subprocess chair** (Surya, `kind = "subprocess"`) is never served through an engine:
  preflight verifies its weights and runs its own runner once on the golden page on the CPU,
  recording versions, CPU instruction set and machine in `subprocess_receipts`. `pod_run`
  refuses a Designator selection with Surya configured unless that receipt is there.
- **`assembly_proven` is derived, never declared**: true only when a real driver read the
  card (`GpuProfile.measured`) and a chair read the golden page back through an engine that
  served it (`SmokeResult.served_by`). Both carry a module-private token minted only by the
  `nvidia-smi` probe and the serving evidence path.
- Ordinary serving refuses an unproven row; only this smoke assembly may launch one, and its
  audit says so. After a green real-silicon report, `python -m operations.serving.qualify`
  renders review candidates for the measured tier; it never edits the catalogue.

## The pod image contract

`bootstrap.verify_image_contract` checks this first in the `REPOSITORY` step, before the
~10 GB sync, so a bad image fails with a named reason instead of a `ModuleNotFoundError` on a
billing pod.

On a fresh Ubuntu 24.04 RunPod container, after cloning and **before** `uv sync`, run as
root `bash operations/pod/prepare_runtime.sh`. It installs `uv 0.12.1` at
`/usr/local/bin/uv` (checked against a pinned SHA-256) and `ninja-build` at
`/usr/bin/ninja` (the Chandra and DAI vLLM warm-up fails without it). It is idempotent.

The image must carry:

- **A checkout.** The bootstrap does not clone; it runs `git fetch --no-tags origin <sha>`
  and `git checkout --detach --force <sha>` in `--repository` (`/opt/verbatus`, on
  container-local disk).
- **An HTTPS `origin` reachable with no HOME.** Git runs with `GIT_CONFIG_NOSYSTEM=1` and
  `GIT_TERMINAL_PROMPT=0`; includes, credential helpers and other external credential
  routes in checkout config are refused. A private origin may use URL credentials or a
  repository-local `http.<url>.extraheader`.
- **Tools at absolute paths** (`BOOTSTRAP_EXECUTABLES`; PATH is never searched):
  `/usr/bin/git`, `/usr/local/bin/uv`, `/usr/bin/nvidia-smi`, `/usr/bin/apt-cache`,
  `/usr/bin/apt-get`, `/usr/bin/dpkg-query`. The default uv installer's `~/.local/bin` does
  not qualify.
- **A pre-built `<repository>/.venv` whose interpreter runs the primary process.** vLLM is
  launched as `sys.executable -m vllm.entrypoints.cli.main`, so under the system `python`
  PREFLIGHT fails after the download is paid for.
- **A working directory inside `<repository>`**, so `python -m operations.pod.pod_timer`
  resolves.
- **Enough container disk**: every create states `container_disk_gb`, and
  `sync_uv_environment` checks free space first.
- **No credential from this repository.**

Keep the repository, `.venv` and `UV_CACHE_DIR` on container-local disk; keep inputs,
outputs, evidence and the model store on the volume.

## The serving stack

The real roster's stack is the `pod` dependency group in `pyproject.toml`, locked in
`uv.lock` for Linux x86_64 only, with exactly the versions `config/serving_recipes_real.toml`
names; `test_pod_run.py` holds them together.

| Package | Pin | Licence | Why |
|---|---|---|---|
| `vllm` | `0.30.0` | Apache-2.0 | pip-audit reports no advisory against it, and it registers every architecture the roster declares |
| `transformers` | `5.14.1` | Apache-2.0 | Above vLLM's `>= 5.10.4` and the Perlector's `>= 5.8.0`; accepts `huggingface-hub` `>=1.5.0,<2.0` |
| `qwen-vl-utils` | `0.0.14` | Apache-2.0 | It and its dependencies publish Linux x86_64 wheels, so nothing compiles on the card |
| `huggingface_hub` | `1.31.0` | | vLLM 0.30.0's floor |

- One vLLM release serves all four chairs: Chandra-2 and the Perlector declare
  `Qwen3_5ForConditionalGeneration`, DAI and Churro-3B `Qwen2_5_VLForConditionalGeneration`.
- No `flash-attn`: vLLM ships its own FlashAttention, and `flash-attn` is sdist-only.
- `transformers` 4.x cannot lock against `huggingface-hub` 1.x.
- Licence sources: the `LICENSE` files of `vllm-project/vllm` (tag `v0.30.0`) and
  `huggingface/transformers`, and `qwen-vl-utils`'s PyPI metadata.

The full gate (`.githooks/check-all.sh`) audits this group from the lock without installing
it (`.githooks/serving_audit.py`). One advisory is accepted for its exact pin only:
`setuptools` 80.10.2, PYSEC-2026-3447 (CVE-2026-59890). vLLM 0.30.0 requires
`setuptools<81`, and the flaw affects building an sdist on a Unicode-normalizing (macOS)
filesystem, which the pod never does. A lock that moves `setuptools` ends the acceptance.

Only a boot proves the wheels install and the checkpoints load, so every row stays
`preflight_state = "unproven"` until a reviewer stamps it after a real-silicon preflight.

## What a launch writes on the volume

The volume outlives the pod but not the retention decision. `verbatus fetch-run` brings two
prefixes home on its own; everything else must be named with `--evidence-key`. A key is the
volume path with `<volume>/` removed (`<volume>/pod-run-report-<token>.json` is
`--evidence-key pod-run-report-<token>.json`); a key starting with `/` is refused.
`--launch-receipt <path>` derives every key except the transfer journal from the saved
receipt.

**Fetched by prefix:**

| Path | Written by | Notes |
|---|---|---|
| `<volume>/runs/<run-id>/` | the stages, through `RunTree` | every object checked against the tree's digests |
| `<volume>/runs/<run-id>/<stage>/serving-logs/` | each started chair | **unverified** (`unverified_serving_logs`); a log still growing or too big goes to `refused_serving_logs` |
| `<volume>/preflight/<stem>/` | PREFLIGHT: golden page, serving logs and receipts, launch audits | into `<local root>/evidence/` |

**Named with `--evidence-key`:**

| Path | Written by |
|---|---|
| `bootstrap-report-<token>.json` | `bootstrap_main --report-path` |
| the bootstrap journal `…-<token>.json` | `bootstrap_main --journal` |
| `pod-run-report-<token>.json` and its `-liveness`, `-timings`, `-transcript`, `-estimate`, `-progress`, `-hold` siblings | `pod_run` |
| `pod-runtime-report-<token>.json` and `-terminating.json` | `pod_timer --report-path` |
| `.pod_guard/guard.log` | `pod_guard.sh`; one log for every pod on the volume |
| `pod-transfer-journal.json` | `ChecksummedTransfer`; the only record of which submission rows were verified |

**Never fetched:** the model store, `submission/` and `submission-manifest.json` (kept
beside the folder because the Door refuses records among images), `pod-transfer/`, and other
upload batches.

The single-resident GPU lock is `/tmp/verbatus-pod-gpu.lock` on container-local disk
(`operations.serving.residency.POD_RESIDENCY_LOCK_PATH`), opened `O_NOFOLLOW` and created
0600: the boundary is the card, and a network mount may not honour advisory locks.

## The pod CLI

`python -m operations.pod.cli` has `create`, `adopt` and `close`, built around a lease, a
laptop supervisor and a pod-side timer. **It has never started, inspected or billed a live
pod**: every adapter test uses an in-memory transport, and the RunPod field names come from
RunPod's documentation (cited in `provider_runpod.py`). Its first live run follows
[the boot plan](#the-boot-plan-boot-a-the-drill-before-boot-b).

Create and adopt need explicit untracked provider and controller-armer factories plus a
request file, so the repository holds no credential or provider default. They print price
and ceilings and prompt for a typed phrase (derived from the preview and a single-use
challenge; an operational guard, not the lead's permission); EOF is a refusal. **Do not
invoke a factory that could contact a provider without the lead's current-session
permission.** A request must make the provider-neutral timer the primary command, with a
mandatory bootstrap command and a durable report path on the volume.

| Exit | Meaning |
|---|---|
| 0 | a guarded success |
| 2 | a refusal naming no pod, lease or close |
| 3 | anything that observed or touched a real pod, wrote a lease or attempted a close: go and look |

- **`close --lease <id>`** closes one live lease through `supervise.close_lease_now`;
  anything short of verified is `UNVERIFIED CLOSE`, exit 3. It needs no armer or phrase. It
  refuses before any terminate an id that is not 32 lowercase hex characters, a lease this
  account does not hold, a lease file whose `lease_id` differs from its name, and a lease a
  live supervisor holds. Failures before the provider is reached are `CLOSE NOT ATTEMPTED:`,
  exit 3. `--provider-name` is a label, not proof of account, so a pod reported absent before
  any terminate refuses (a wrong-account factory would see a genuine absence). `--spend` is
  required.
- **`--record-fixture PATH`** appends every provider exchange as JSON lines (0600, fsynced,
  never truncated), with credential-shaped values and the launch token replaced
  (`verbatim: false`), so a drill leaves a replayable fixture. A provider without
  `record_exchanges` refuses the flag by name before any preview, except under `close`.

### Guards on spending (`spend.py`)

- **Ceilings** cover pod plus volume, hourly and over the hard lifetime, and apply again to
  the price the provider actually returned: a pod created above it is closed at once.
- **Card allowlist.** Create refuses a `gpu_type` not in `config/pod_placement.toml`, or one
  whose reviewed price exceeds `max_hourly_usd` net of the volume rate (`refused-card`).
  `adopt` is not gated: refusing an existing pod would leave it billing unguarded.
- **Spend policy.** Paid paths refuse unless `config/spend.toml` is a configured policy the
  lead reviewed; `billing_cutoff_margin_seconds` must lie in 0–3600.
- **Balance floor.** `account_balance_floor_usd` is tested against the observed balance net of
  this action's cost and every liability in the lease root, at the create and adopt gates
  only. An unavailable source, or an observation older than 60 s or future-dated, refuses.
  `account_balance_alert_usd` sends a warning between floor and alert line (at most once per
  fifteen minutes).
- **One live pod.** Create and adopt serialize under one lock and refuse
  (`refused-active-lease`) while any lease in the root is short of `closed-verified`. Use one
  lease root per provider account.

### Leases and the two controllers

A lease is written before create and recovered by exact launch token after a restart. A
launch is green only when the laptop supervisor has started **and** a pod-timer
acknowledgement is durably bound to the exact lease, pod and hard deadline; one that cannot
arm both closes its own pod. Closing a pod never touches the volume (there is no
volume-delete operation). Billing capture at close is retried 3 times, 15 s apart; "not
posted yet" reports `pending-reconciliation`.

- **`supervise.py`** (`python -m operations.pod.supervise`) supervises one lease from the
  laptop. Ownership is a kernel `fcntl.flock` on `supervisors/supervisor-<lease>.lock`, not a
  pid. Every tick re-reads `provider.status()` and closes on any word but `RUNNING`, except
  `PROVISIONING`/`STARTING` while arming or up to 600 s after; `ERROR` closes at once. An
  unanswering provider is not a reason to close. It is a long-lived process while a pod
  bills: arrange to learn promptly if it dies.
- **`controller_armer.py`**: `ChannelControllerArmer.arm` starts the supervisor first, hands
  it the launch's owner token through a 0600 identity file (never argv), and heartbeats the
  lease while polling for the pod timer's report, which travels through the volume and is read
  through RunPod's S3 view (`None` only when proven absent). `ObservingControllerArmer` never
  arms, so its launch closes the pod at once.
- **The pod timer** (`pod_timer.py`) closes the pod at the hard deadline. It can be destroyed
  by its own DELETE, so final verification belongs to the laptop. If it fails before a
  provider-backed timer exists, the pod goes `EXITED` and bills volume disk at double rate
  until the supervisor (or `close --lease`) closes it. `<stem>-terminating.json` tells "never
  tried" from "destroyed mid-verification"; the laptop's close record is authoritative.

### Provider adapters

`provider.py` is the seven-verb provider seam; `provider_runpod.py` holds the RunPod adapters
behind one `HttpTransport`; `fake_provider.py` is the offline stand-in.

- **`RunPodV2Provider`** (`api.runpod.io/v2`) is the default; **`RunPodProvider`**
  (`rest.runpod.io/v1`) stays selectable until a live v2 run is green. RunPod retires v1 on
  2026-11-15. `provider_runpod.live_runpod_provider(key, pod_price=..., volume_price=...,
  route=...)` picks one. Each create adds `VERBATUS_RUNPOD_ROUTE` to the pod's `env`, so the
  pod timer uses the same route.
- **A v2 create is refused by name** until the lead records a basis in
  `provider_runpod.V2_ON_DEMAND_BASIS`, since v2 has no `interruptible` field; paid creates go
  through v1. Every other v2 verb works.
- Under v2 the start command is sent as exec-form `args` (`{"entrypoint": [...], "cmd":
  [...]}`), so the image's ENTRYPOINT cannot wrap the timer. `402`, `400` and `422` on create
  are named refusals; `409` on terminate (a cluster pod) is `TerminateRefused`. The pod list
  includes cluster pods and is followed to its last page.
- **Balance.** `GraphQLBalanceObserver` reads `myself { clientBalance currentSpendPerHr }`
  with the key as a query parameter, scrubbed from every error and fixture. USD is documented,
  not observed. RunPod documents GraphQL as retiring in early 2027, and no v2 endpoint reports
  a balance.

### The boot plan: Boot A, the drill, before Boot B

The first live run of the pod CLI is split in two, because the acknowledgement channel is the
one thing no offline test can measure. Each boot needs separate in-session permission, the GPU
class and the S3 keys in the launching shell.

- **Boot A** renders its request with `python -m operations.pod.boot_a_request --spend
  config/spend.toml --placement config/pod_placement.toml` (cheapest card, a 900 s lifetime,
  ceilings, cost, the exact command; it authorizes nothing). It uses `ObservingControllerArmer`,
  `bootstrap_main --hold-only` and `--record-fixture`, closes its pod at once, and learns four
  facts: does the pod-written object appear in the S3 view, under which key, after how long,
  and does the pod-scoped key hold delete and billing rights. The arming wait is two waits
  (`pod-arming-drill.v2`): container start (600 s) then the channel (300 s). They sum to the
  whole 900 s, so pass smaller bounds (for example 300/300) or authorize a longer lifetime.
- **Boot B** (`boot_b_request.py`) uses `ChannelControllerArmer` with Boot A's bounds, then
  runs the bootstrap and preflight. Its start command nests `pod_run`'s argv and, after `--`,
  `bootstrap_main`'s, with the launch token bound into both.

Before and during those boots, record and verify (marking each verified, unverified or not
run): the pod-scoped key's delete and billing rights; `RunPodV2Provider.cross_check_catalogue`
over the placement table before a v2 create; every v2 `status` word and whether `cost` is
non-zero while `PROVISIONING`; exactly how `args` comes back; launch-token recovery after a
lost create response; the pod list's paging; the checksummed transfer end to end; the volume
mount, restart survival and hard-link publication; how the pod timer gets `RUNPOD_API_KEY`
without the repository supplying it; an `EXITED` pod's console log before closing it; the real
preflight with free disk before and after and `.venv` size (to replace
`models.DEFAULT_CONTAINER_DISK_GB`, `bootstrap.UV_CACHE_REQUIRED_BYTES` and
`REPOSITORY_VENV_REQUIRED_BYTES`); and the close's GET-404, list absence and billing rows.

### Open items

The code cites these IDs.

| # | Item | Closes when |
|---|---|---|
| 04-2 | The timer-report channel is fake-proven only | the first boot sees a mount-written object appear in the S3 view and records the delay |
| 04-4 | A timer startup failure leaves nothing on the pod able to terminate it; the `EXITED` pod bills volume disk at double rate | mitigated: the laptop supervisor closes it; no provider-side TTL exists |
| 04-5 | Untested seams: the success paths of `sync_uv_environment`, `pod_timer.main`, `cli.main` through real factories, and `UrllibRunPodTransport` | each is exercised live |
| 04-6 | Every RunPod field name is documented, not observed | the first live run on each route, recorded with `--record-fixture` |
| 04-7 | Whether RunPod bills anything before `createdAt` (v1 still anchors on `lastStartedAt`) | observed on a live run, and v1 deleted |
| 04-9 | Whether `metadata.query` appears on the `podId`-filtered billing route, and whether buckets fill the window | observed on a live run |

## Tests

```sh
.venv/bin/python -m pytest operations/pod
```

They include deliberately broken confirmation, ceiling, status, billing, transfer, cache,
smoke-read, controller and timer paths, seven supervisor drills, and seven launch drills in
`test_launch_drill.py`. None makes a live call.

## If your task seems to need a pod

Stop and say so: what you would start, the hourly rate, roughly how long, what it buys, and
how it gets turned off. Then wait for the lead. In an unattended session, raise it as a
decision the moment you find it and carry on with everything that does not depend on it.
