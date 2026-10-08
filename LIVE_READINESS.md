# Live readiness: the first paid run

The project lead's first paid run, from the Mac. Detail lives in
[operations/operator/README.md](operations/operator/README.md) and
[operations/pod/README.md](operations/pod/README.md). **(to confirm)** marks what has not
been checked against the real tool or account. Nothing here is permission to spend.

## 1. What to decide before anything bills

**The card.** The 27B Perlector needs the `generic-80gb-plus` tier: one
`NVIDIA RTX PRO 6000 Blackwell Server Edition` (96 GB), $1.99/h on the price sheet in
`config/pod_placement.toml`, plus the network volume's own hourly price (to confirm in
the RunPod console). The guard drill (step 6) uses the cheapest card, `NVIDIA RTX A5000`,
$0.27/h.

**The pod budget** is the lead's, in `config/spend.toml` (`pod-spend.v5`). It is
committed off (`pod_budget = "off"`): a pod then has no guard deadline or backstop unless
started with a number of hours, and the guard's idle ladder warns without deleting unless
`ladder_delete = "on"` (`operations/pod/README.md`). With the budget on: at most
$2.10/h for pod plus volume, a soft maximum of 2 h or $5.00 and a hard maximum of 3 h or
$7.00, whichever comes first. A pod's window is 2 h: the guard's deadline sits at the
soft maximum. Going past it is an extension only the lead makes, and the hard maximum
bounds it at 3 h. The hourly cap admits the RTX PRO 6000 ($1.99/h) beside a volume of up
to $0.11/h, so 2 h costs about $4.10 and 3 h about $6.15 at a $0.06/h volume. On the hand
route below (`runpodctl` plus the pod guard) the file bounds time only, and only while the
budget is on: the start command then refuses a window past the hard maximum and its
backstop deletes the pod at the hard maximum from creation. **With the budget off, as
committed**, `pod_start_command.sh off` means no deadline (the guard warns and backs up,
never deletes unless `ladder_delete = "on"`), and `pod_start_command.sh <hours>` is a hard
delete at that time whatever the run is doing, backstop an hour later, with no 3 h cap.
Nothing on that route checks the hourly price or the cost, so the lead's approval in the
session is the limit that counts there.

**The plan inside it.** Nothing has been timed on a real pod, so these are planning
figures. Costs are the card's hourly price ($1.99/h; the drill's A5000 $0.27/h) plus the
volume's hourly price (to confirm in the console). The windows run from container start,
so the image pull before it adds a few minutes to each. The backstop deletes a pod an
hour after its deadline and never later than 3 h from creation, so only a drill whose
deadline was moved out could reach the 3 h cap ($0.81 + volume).

| Launch | Card | Window | Most it can cost |
|---|---|---|---|
| Guard drill | RTX A5000 | 1 h | $0.27 + volume if the guard works; at most $0.54 + volume (2 h) if it fails, when the backstop deletes an hour after the 1 h deadline |
| Run 1: 4 prepared pages | RTX PRO 6000 | 2 h, extendable to 3 h | about $3.98 + volume; $5.97 + volume extended |
| Run 2: 2 original spreads | RTX PRO 6000 | 2 h, extendable to 3 h | about $3.98 + volume; $5.97 + volume extended |
| Run 3: a slightly larger set | RTX PRO 6000 | 2 h, extendable to 3 h | about $3.98 + volume; $5.97 + volume extended |

The earlier plan allowed 4 h for runs 1 and 2 and 6 h for run 3. Whether runs 1 and 2
finish in 2 h, or in 3 h with the extension, is to confirm on run 1; nothing measured
says they will. Run 3 is sized from run 1's timing to fit the same window, so it may hold
fewer pages than planned (to confirm). **Any run that needs more than 3 h needs a new
budget decision from the lead**, and `config/spend.toml` changed to match, before it
starts.

**The account balance.** `account_balance_floor_usd = "50.00"` is a policy value the
file itself marks unverified. Check the RunPod balance (console, Billing) is above $50
plus the run's budget (at most $7.00) before each launch. `verbatus spend show` prints
the policy, ending with the `Soft maximum` and `Hard maximum` lines; it never reads the
balance.

## 2. Set up the Mac (free)

**The Mac needs macOS 13 (Ventura) or later**, Intel or Apple silicon. The PDF library
(pypdfium2) ships wheels only for macOS 13 and later; on an older macOS the install falls
back to an untested build from source. Check first:

```sh
sw_vers -productVersion; uname -m    # 13.0 or later; x86_64 (Intel) or arm64 (Apple silicon)
```

git needs the Xcode Command Line Tools (`xcode-select --install`; the first `git clone`
offers them too). Their `python3` is 3.9, too old for this repository: run Python only
as `uv run …` or `.venv/bin/python …`, never a bare `python` or `python3`. uv builds
`.venv` on Python 3.12 (`.python-version`), downloading it if the Mac has none.

In Terminal, from a fresh clone. uv must be exactly 0.12.1; if an older uv is already
installed, `uv self update 0.12.1` does the same.

```sh
curl -LsSf https://astral.sh/uv/0.12.1/install.sh | sh
git clone https://github.com/MegaSlick/Apparatus-Verbatus-Alpha
cd Apparatus-Verbatus-Alpha
uv sync --frozen --group test --group audit
sh .githooks/install.sh
sh .githooks/check-static.sh
source .venv/bin/activate          # once per Terminal window; puts `verbatus` on PATH
verbatus spend show
verbatus watch --help              # present only on code new enough for this runbook
```

`runpodctl` must be installed and given the account's API key. Commands here are
written for runpodctl 2.x (checked against 2.14.0); `runpodctl version` shows which you
have. The API key and the RunPod S3 keys go in the shell only, never in a file here:

```sh
brew install runpod/runpodctl/runpodctl
read -rs RUNPOD_API_KEY; export RUNPOD_API_KEY
read -rs RUNPOD_S3_ACCESS_KEY; export RUNPOD_S3_ACCESS_KEY
read -rs RUNPOD_S3_SECRET_KEY; export RUNPOD_S3_SECRET_KEY
runpodctl gpu list    # must list "NVIDIA RTX A5000" and "NVIDIA RTX PRO 6000 Blackwell Server Edition"
```

`runpodctl config --apiKey <key>` instead stores the key in `~/.runpod/config.toml`, outside
the repository; it also lands in the shell's history, so prefer the variable.

## 3. Confirm three fixes on macOS (free)

Linux CI runs these; only a Mac run proves them on macOS.

Each command below exits 0 when it passes and non-zero when anything fails.

- **F10, the uv guard.** Expected: all pass, including the `sh` and `bash-posix` cases.
  `.venv/bin/python -m pytest .githooks/test_ci_workflow.py -k "path_entry"`
- **F11, tests leave real state alone.** Expected: the `find` prints nothing.
  ```sh
  touch /tmp/before-tests && sh .githooks/check-all.sh
  find ~/.local/state ~/Library -newer /tmp/before-tests -iname '*verbatus*' 2>/dev/null
  ```
- **F12, the golden pins.** Expected: passes, so the HAPPY and REVIEW pins are the same
  on the Mac as on Linux. `.venv/bin/python -m pytest pipeline/orchestrator/test_orchestrator_acceptance.py`

## 4. Check the pages on the Mac (free)

Put the four prepared RecordGold pages in their own folder under `private/`, for example
`private/rg-pages/`, and nothing else in it. The two spreads go in another, for example
`private/rg-spreads/`. Finder leaves `.DS_Store` files, which the Door refuses, so clear
them first.

```sh
find private/rg-pages private/rg-spreads -name .DS_Store -delete
verbatus --state-dir private/verbatus-state upload --source private/rg-pages \
  --manifest-out private/rg-pages-manifest.json
verbatus --state-dir private/verbatus-state upload --source private/rg-spreads \
  --manifest-out private/rg-spreads-manifest.json --prefix spreads
verbatus --state-dir private/verbatus-state run --run-id local-rg-pages \
  --submission-folder private/rg-pages --submission-manifest private/rg-pages-manifest.json
verbatus --state-dir private/verbatus-state run --run-id local-rg-spreads \
  --submission-folder private/rg-spreads --submission-manifest private/rg-spreads-manifest.json
```

The two uploads seal the folders and copy them to a local folder only; each exits 0. The
spreads need `--prefix spreads`: the local folder, like the volume, keeps one sealed
manifest under each name, and the pages already hold the default `submission`, so
without it the second upload is refused (exit 2, `target 'submission-manifest.json'
exists but differs`).

`--state-dir private/verbatus-state` goes before the word, on every command here. A run
keeps its tree under the state directory, and the Door's data gate accepts real pages
only under an approved storage root (`private/` on the Mac,
`config/data_handling_policy.json`). Without it the tree is under
`~/.local/state/verbatus/` and the Door refuses at once: `the run root is outside every
approved storage root`. Give the same `--state-dir` to `status`, `review` and `export`
for these runs, since their records are kept there.

Each `run` is **expected to stop at the Designator, exit 2**, naming the record detector
chair `'secondary_proposer'` as a `'fixture' row`: the default roster has no real models.
It proves the pages pass the data gate, Door, Exemplar and ink map on this Mac. On Linux
all six images passed: 8-bit grey, none bilevel or 16-bit; pages 3112x4440 at 600 DPI,
spreads 3864x3056 and 3672x2744.

`verbatus` gives only two exit codes, whatever the word: 0 when it did what was asked, and
2 for everything else, a refusal, a failure, a held or halted run and a partial export
alike; the `What happened:` line says which. Only `pod_run` and the orchestrator (step 7)
tell held (3) from halted (4) by the exit code.

### Prepared pages from the spreads (free)

To have the spreads split, levelled and cropped before any model reads them, prepare
them first and let the Door cut each page from its original:

```sh
verbatus --state-dir private/verbatus-state prepare --scans private/rg-spreads \
  --out private/rg-spreads-prepared
```

It prints how many pages need review and where the review sheet is
(`private/rg-spreads-prepared/review.html`). Check each flagged page there, put
corrections in an overrides file and run it again with `--overrides FILE` until the
pages are right. Read `triage-notes.txt` for the pages the Door will cut differently from
pagekit. Then seal the scans with the triage documents and check them at the Door. The
prefix `prepared-spreads` keeps them apart from the unprepared spreads above:

```sh
P=private/rg-spreads-prepared
verbatus --state-dir private/verbatus-state upload --source private/rg-spreads \
  --manifest-out $P/submission-manifest.json --prefix prepared-spreads \
  --triage-decision-manifest $P/triage-decision-manifest.json \
  --triage-producer-recipe $P/triage-producer-recipe.json
verbatus --state-dir private/verbatus-state run --run-id local-rg-prepared \
  --submission-folder private/rg-spreads --submission-manifest $P/submission-manifest.json \
  --triage-decision-manifest $P/triage-decision-manifest.json \
  --triage-producer-recipe $P/triage-producer-recipe.json
```

The upload refuses a triage manifest without a row for every sealed scan, before
anything is sent. The run is expected to stop at the Designator, as above, with one
page per prepared page. Step 5 sends the same three things to the volume, and step 7
hands the two triage documents to the pod's run. Triage sent with a submission is never
replaced: after a later correction, upload again under a new `--prefix`.

## 5. Send the pages to the volume and prove the way home (free of GPU time)

```sh
verbatus upload --source private/rg-pages --sealed-manifest private/rg-pages-manifest.json \
  --network-volume DATACENTER:VOLUME_ID
verbatus fetch-run --run-id s3-path-check --into /tmp/verbatus-s3-check \
  --network-volume DATACENTER:VOLUME_ID
```

The upload writes `submission/` and
`submission-manifest.json` on the volume and exits 0. The fetch should refuse with exit
2, naming `nothing is stored under 'runs/s3-path-check/'`: that proves the listing works.
Any other error is fixed before renting. For the spreads later, upload
`private/rg-spreads` with `--sealed-manifest private/rg-spreads-manifest.json --prefix
spreads`, as in step 4. For the prepared spreads, send the record sealed with their
triage documents:

```sh
P=private/rg-spreads-prepared
verbatus --state-dir private/verbatus-state upload --source private/rg-spreads \
  --sealed-manifest $P/submission-manifest.json --prefix prepared-spreads \
  --network-volume DATACENTER:VOLUME_ID \
  --triage-decision-manifest $P/triage-decision-manifest.json \
  --triage-producer-recipe $P/triage-producer-recipe.json
```

This writes `prepared-spreads/`, `prepared-spreads-manifest.json`,
`prepared-spreads-triage-decision-manifest.json` and
`prepared-spreads-triage-producer-recipe.json` on the volume.

Pick the commit the pod will run: `git fetch origin && git rev-parse origin/main`. Use
the full 40 characters as `<sha>` below.

## 6. Guard drill: the cheapest card, one hour

Proves the pod guard arms from the start command and deletes the pod by itself.

```sh
START=$(sh operations/pod/pod_start_command.sh 1 <sha>) &&
runpodctl pod create --name verbatus-guard-drill \
  --image runpod/pytorch:1.4.0-cu1300-torch2130-ubuntu2404 \
  --gpu-id "NVIDIA RTX A5000" --gpu-count 1 --cloud-type SECURE \
  --data-center-ids <DATACENTER> --network-volume-id <VOLUME_ID> \
  --volume-mount-path /workspace/private --container-disk-in-gb 20 --ports "22/tcp" \
  --docker-args "$START"
runpodctl pod get <pod id>          # shows the SSH details
```

`pod_start_command.sh` exits 0 and prints the start command, or exits 2 and prints
nothing when the hours are zero or `pod_budget` cannot be read from `config/spend.toml`,
and, with the budget on, when the hours are past the hard maximum (3 h) or the hard
maximum cannot be read. The `&&` keeps a refusal from creating a pod: written inline as
`--docker-args "$(...)"`, the create would still run, with no guard. The backstop counts
the hard maximum from when the command is printed, so print it afresh for every pod. That
moment is read from the laptop's clock, so keep it set automatically (System Settings,
General, Date & Time). Give `--data-center-ids` exactly one id, the volume's: for a GPU pod
runpodctl uses only the first.

**The image must carry CUDA 13.0.** Observed 2026-10-06 on an RTX PRO 4500 (Blackwell,
the PRO 6000's family): with `runpod/pytorch:1.0.2-cu1281-torch280-ubuntu2404` every vLLM
witness died at start with `FlashInfer requires GPUs with sm75 or higher`. FlashInfer
compiles its sampling kernel on first use and needs `nvcc` 12.9 or newer for Blackwell;
that image's `/usr/local/cuda` is 12.8. With CUDA 13.0 in its place, the same bootstrap
went green and Door through Attestatores ran. The `cu1300` image above ships 13.0 with its
headers. The first engine start compiles the kernel (about 75 s); later starts reuse it.

**Size the volume for the model store.** The bootstrap's `MODEL_STORE` step fetches every
chair in the real catalogue, whichever roster is chosen: about 85 GB (the Perlector 52 GB).
A 40 GB volume ran out mid-fetch; 200 GB held it with room to spare. Fetching took about
8 minutes in EU-RO-1.

Over SSH, `tail /workspace/private/.pod_guard/guard.log` must show `armed for pod <id>`;
if not, `runpodctl pod delete <pod id>` and stop. Otherwise leave it: idle, the phone
hears at 15 and 30 minutes, and the guard deletes the pod at its one-hour deadline (the
idle ladder itself deletes only with `ladder_delete = "on"`, and only after two hours). Confirm as in step 10. (Drill disk size: to confirm.)

## 7. Launch a real run

Arm the phone ping first, from the Mac, over the pod's **direct** SSH port (read
`echo $RUNPOD_PUBLIC_IP $RUNPOD_TCP_PORT_22` in a pod shell). Never pipe the topic through
`ssh.runpod.io`: that proxy ignores the command and runs standard input as a shell, so
the secret would be typed into it.

```sh
sed -n 's/^NTFY_TOPIC=//p' private/ntfy.conf | tail -n 1 | tr -d "\"'" |
  ssh -p <RUNPOD_TCP_PORT_22> root@<RUNPOD_PUBLIC_IP> 'umask 077 && mkdir -p /workspace/private/.pod_guard &&
    cat > /workspace/private/.pod_guard/ntfy_topic'
```

Create the pod as in step 6, but with `--name verbatus-<run id>`,
`--gpu-id "NVIDIA RTX PRO 6000 Blackwell Server Edition"`, `--container-disk-in-gb 120`
and `pod_start_command.sh <hours or off> <sha>` (with the committed budget off, `off` is
no deadline and needs `--no-hold` below; with the budget on, `2` is the soft-maximum
window and more than the 3 h hard maximum is refused). Then, on the
pod over SSH, the checks from `operations/pod/README.md` ("On the pod, over SSH"):
`findmnt /workspace/private`, the guard log, `echo "$RUNPOD_POD_ID"`, the deadline file,
then

```sh
git clone https://github.com/MegaSlick/Apparatus-Verbatus-Alpha /opt/verbatus
cd /opt/verbatus && git checkout --detach <sha>
bash operations/pod/prepare_runtime.sh
UV_CACHE_DIR=/tmp/verbatus-uv-cache uv sync --frozen
```

and launch, detached:

```sh
V=/workspace/private RUN=<run id> R=/opt/verbatus
GUARD_DEADLINE=$(cat $V/.pod_guard/deadline-$RUNPOD_POD_ID 2>/dev/null)
if [ -n "$GUARD_DEADLINE" ]; then
  VERBATUS_HARD_DEADLINE=$(date -u -d "@$(( GUARD_DEADLINE - 300 ))" +%Y-%m-%dT%H:%M:%SZ)
else
  VERBATUS_HARD_DEADLINE=none   # a pod started with `off`; accepted only with --no-hold
fi && export VERBATUS_HARD_DEADLINE &&
cd $R && setsid nohup $R/.venv/bin/python -m operations.pod.pod_run \
  --report-path $V/pod-run-report-$RUN.json --run-id $RUN \
  --submission-folder $V/submission --submission-manifest $V/submission-manifest.json \
  --mechanics-qualification --no-hold \
  --notify --hourly-usd <card price per hour plus the volume's, e.g. 2.05> \
  -- \
  --volume-mount-path $V --report-path $V/bootstrap-report-$RUN.json \
  --repository $R --repository-commit <sha> --lockfile $R/uv.lock \
  --journal $V/bootstrap-journal-$RUN.json --store-root $V/model-store \
  --models-config $R/config/models-real.toml \
  --serving-recipes-config $R/config/serving_recipes_real.toml \
  > $V/pod-run-$RUN.out 2>&1 < /dev/null &
```

- **Two pods, then the Mac.** The planned topology splits the run: a cheap witness pod
  (A5000, `--container-disk-in-gb 100`) adds `--models small` before `--` (Door through
  Attestatores); the big card (PRO 6000, 120 GB disk) adds `--from perlector --to
  coniector` with the **same run id, submission folder and `--store-root`**, and checks
  the sealed Attestatores on the volume before its boot. The launch paths stay the same:
  the big card sets the witness's bootstrap journal aside as
  `bootstrap-journal-$RUN.pod-<witness pod id>.json` and bootstraps for its own GPU, and
  a replacement for a dead pod does the same. Do not use `--models big` for
  the split: it runs on to the Armarium. The Mac then runs Recensor to Armarium
  (`operations/operator/README.md`). With `--no-hold` the guard deletes each pod within
  a minute of its run ending; a pod on its own disk (`mounts.persistent`) instead runs
  without `--no-hold`, needs a deadline, and is copied home before it is deleted.
- The real configuration is these **two files, always together**. Here, on the pod,
  `pod_run` always needs `--models-config`, and refuses any roster other than the
  fixture `config/models.toml` when `--serving-recipes-config` is missing; it does
  so while reading the plan, before the boot or any model fetch. Only the fixture
  roster is given a catalogue by default, the fixture `config/serving_recipes.toml`.
  `verbatus run` and the orchestrator itself refuse either file given without the
  other.
- `--hourly-usd` must be a positive decimal. It lets the deadline-at-risk notice say
  what running past the deadline costs; `--notify` sends that notice (and the systemic
  alarm) to the phone. Drop `--notify` if no phone should be paged.
- **Deadline at risk.** Each tick `pod_run` estimates when the current stage finishes,
  reading the guard's deadline file the way the guard does. If that finish plus 20
  minutes passes the deadline, one notice goes to the phone, naming the soft and hard
  maximums, the extra time and its cost, and the command that moves the deadline. Nothing
  moves the deadline by itself; extending is the lead's call.
- For the spreads, use `--submission-folder $V/spreads --submission-manifest $V/spreads-manifest.json`.
- For the prepared spreads, use `--submission-folder $V/prepared-spreads
  --submission-manifest $V/prepared-spreads-manifest.json --triage-decision-manifest
  $V/prepared-spreads-triage-decision-manifest.json --triage-producer-recipe
  $V/prepared-spreads-triage-producer-recipe.json`. The Door then cuts each page from its
  original spread as `prepare` decided. (A request rendered with
  `python -m operations.pod.boot_b_request --triage` names the same two files for the
  default `submission` prefix.)
- `pod_run` runs detached, so its exit code is read from its report (`exit_code`, and
  `verbatus watch` shows it on the run line once the run ends): 0 complete, 2 refused
  before the orchestrator ran, 3 held for review, 4 halted, 5 a red bootstrap step, 6 the
  orchestrator failed or refused (an oversized export, step 9, is this), 7 a dry run, 8 a
  selected range completed before the Armarium.
- Use the same `--store-root` every time, so later pods reuse the downloaded weights.

## 8. Watch it from the Mac

`verbatus watch` reads copies of four report files from a folder on the Mac. It contacts
nothing and writes nothing, so copy fresh files in a loop (`scp` from the pod's
direct SSH port, `scp -P $RUNPOD_TCP_PORT_22 root@$RUNPOD_PUBLIC_IP:...`, read inside
the pod; the `ssh.runpod.io` proxy cannot carry `scp`). Ctrl+C stops it.

```sh
mkdir -p ~/verbatus-watch
while :; do
  scp -q -P <RUNPOD_TCP_PORT_22> "root@<RUNPOD_PUBLIC_IP>:/workspace/private/pod-run-report-<run id>{.json,-liveness.json,-timings.json,-estimate.json}" ~/verbatus-watch/
  verbatus watch --run-id <run id> --receipts ~/verbatus-watch
  sleep 60
done
```

`verbatus watch` exits 0 when it shows the run, and 2 when the report copy is missing or
names another run (`watch-unreadable`). A healthy run reads like this (synthetic files,
rendered by the real code):

```text
Verbatus works on this computer. It will not contact a cloud provider.
As of 2026-10-04 12:37 UTC (this computer's clock):
Run rg-pages-1: pod_run report says running.
Stage attestatores: 2/4 pages.
This stage finishes about 2026-10-04 13:16 UTC (in 39 min); later stages are not counted.
Deadline 2026-10-04 13:36 UTC (in 59 min), the pod guard's deadline; extendable by hand: yes.
Guard: watch cannot tell whether the guard is armed; it shows only the deadline pod_run read from the guard's file.
Budget: soft max 2 h / $5.00, hard max 3 h / $7.00.
Spend to now: at least $2.03 of soft $5.00 / hard $7.00 (59 min at $2.05/h since pod_run started; the pod was created earlier).
Last notice: none recorded.
Stage runs: door 0 min, exemplar 1 min, ink-map 2 min, designator 10 min.
Liveness: orchestrator running, last seen 0 min ago (by this computer's clock).
```

The same copies 46 minutes old open with two STALE lines, one for each copy, and every
estimate line says when it was true:

```text
Verbatus works on this computer. It will not contact a cloud provider.
As of 2026-10-04 12:37 UTC (this computer's clock):
STALE liveness: last written 46 min ago (limit 2 min). Copy fresh files from the volume, or check the pod.
STALE estimate: written 46 min ago (limit 2 min); its stage, finish and deadline are as of then, not now.
Run rg-pages-1: pod_run report says running.
Stage attestatores: 2/4 pages (as of 2026-10-04 11:50 UTC, 46 min old).
This stage finishes about 2026-10-04 13:16 UTC (in 39 min) (as of 2026-10-04 11:50 UTC, 46 min old); later stages are not counted.
Deadline 2026-10-04 13:36 UTC (in 59 min) (as of 2026-10-04 11:50 UTC, 46 min old), the pod guard's deadline; extendable by hand: yes.
...
Liveness: orchestrator running, last seen 46 min ago (by this computer's clock).
```

What the other lines mean:

- **`STALE liveness: …` and `STALE estimate: …`.** The copies are old, or the pod has
  stopped writing. Each copy is judged on its own time, so one can be stale without the
  other; the stage, finish and deadline lines then say the time they were true (`as of …,
  46 min old`).
- **`Estimated finish: unknown; the estimate is failing (12 ticks): <error>.`** The run
  may be fine, but there is no finish time; the deadline line then falls back to `the
  bootstrap's hard deadline from the report … the guard's own deadline may be earlier`.
- **`Pod still billing until <time>: pod_run keeps it up to the hard deadline after the
  run ended.`** The run is over but the pod is not. With `--no-hold` this should not
  appear; if it does, delete the pod (`runpodctl pod delete <pod id>`) and confirm.
- **`Guard: watch cannot tell whether the guard is armed`** is always shown. Only the
  guard log on the pod (step 6) shows that.
- `Spend to now: at least …` counts from `pod_run`'s start, so the true figure is higher.
- `AT RISK` on the deadline line means the deadline-at-risk notice applies.

On the pod itself, `tail -f $V/.pod_guard/guard.log` and
`tail -f $V/pod-run-report-$RUN-transcript.log` show the guard and the run. More time is
the lead's decision: a new deadline file (`operations/pod/README.md`, "The pod guard").

## 9. If the run stops early

- **Export too large** (only when `embed_pixels = true` in `config/formats.toml`): the
  Door refuses before any reading (`pod_run` exit 6; `verbatus run` exit 2), `… is <n> bytes, above the 201326592-byte export
  archive limit, so it could never be sealed. Nothing was dropped: …`. Start smaller
  runs, or keep `embed_pixels = false` (the shipped setting).
- **Halted** (`pod_run` exit 4; `verbatus run` exit 2): more than two counted failures;
  see `review` (step 11).

## 10. Confirm the pod is gone

With `--no-hold` the pod deletes itself about a minute after the run's final report.
From the Mac:

```sh
runpodctl pod list --all           # the pod must not be listed; without --all, stopped pods are hidden
runpodctl pod get <pod id>         # must say it is not found
```

Then open RunPod's console, Billing, and find this pod's charges. Billing can lag; until
it shows them, the shutdown is **unverified**, not done. The volume stays and keeps
billing.

## 11. Bring the results home and export (free)

```sh
verbatus fetch-run --run-id <run id> --into <local root> --network-volume DATACENTER:VOLUME_ID \
  --evidence-prefix preflight/bootstrap-report-<run id> \
  --evidence-key pod-run-report-<run id>.json \
  --evidence-key pod-run-report-<run id>-transcript.log \
  --evidence-key pod-run-report-<run id>-liveness.json \
  --evidence-key pod-run-report-<run id>-timings.json \
  --evidence-key pod-run-report-<run id>-estimate.json \
  --evidence-key pod-run-report-<run id>-progress.json \
  --evidence-key bootstrap-report-<run id>.json \
  --evidence-key bootstrap-journal-<run id>.json \
  --evidence-key pod-run-<run id>.out --evidence-key .pod_guard/guard.log
verbatus review --run-root <local root> --run-id <run id>
verbatus export --run-id <run id> --run-root <local root>
verbatus backup --run-root <local root> --run-id <run id> --mac-directory <synced folder>
```

`fetch-run`, `review` and `backup` exit 0 when they finish and 2 when they refuse. A run
that holds pages ended with `pod_run` exit 3 (the same run under `verbatus run` on the
Mac exits 2); `review` says why, and each decision is the lead's
(`operations/operator/README.md`, "Recording a review decision"). `export` exits 0 only
for a complete run; over a held run it copies what was delivered and exits 2
(`export-partial`). It refuses, exit 2 and no bundle written, a run whose Armarium export
was never sealed (`export-unsealed`:
"The run has an Armarium export record, but its completion seal is missing or does not
verify."); open it with `review` and bring it to the lead before running the Armarium
again.

## 12. Which pages, in order, and what each run proves

1. **The four prepared RecordGold single pages** (`private/` only). Proves on real pages:
   the guard, the bootstrap and weight download, preflight of every chair on the real
   configuration, every stage through to the Armarium, fetch-run, export and a verified
   shutdown. This is the first real export the project has had.
2. **Their two original spreads.** Proves the run on unsplit spreads, and lets the
   readings be compared with run 1's. Expect the ink map to report each spread's dark
   surround as one component spanning the whole frame.
3. **A slightly larger set**, for example 8 to 12 pages from `private/` or
   `scriptorium/`. Proves timing and the hold rate at a size where held pages show a
   pattern. Keep `embed_pixels = false`. With pixels embedded the Door estimates each
   page like these at about 34 MiB (its stored bytes, two whole-page crops and 1 MiB for
   the text members), so the four pages come to 135.7 MiB, all six images to 182.2 MiB,
   and a seventh page of this size is refused (the pixel sums checked on Linux, plus
   1 MiB a page).

## 13. Open live checks

None of these can be proved without a paid pod. Record each as verified, unverified or
not run.

- The pod guard: the keepalive and the stall notice; the hard deadline; and shutdown
  confirmed in RunPod's own listing and billing.
- The hand route `pod_run`, from boot to fetching the run.
- The detector: determinism, 16-bit pages, and the offline Ultralytics import.
- Surya detection with the patched Pillow 12.3.0.
- Serving with the real configuration.
- RunPod's S3 view: Range reads, and reading a file while it is being appended to.
- pagekit thresholds on real crops. (On the two spreads, a split even at the detected
  gutter is flagged for ink crossing the edge; all thresholds are still uncalibrated.)
- F07: peak memory for a real-size export archive, against the 192 MiB archive limit.
