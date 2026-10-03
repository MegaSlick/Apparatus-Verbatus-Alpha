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

**The budget to ask for.** Nothing has been timed on a real pod, so these are planning
figures (to confirm):

| Launch | Card | Window | Most it can cost |
|---|---|---|---|
| Guard drill | RTX A5000 | 1 h | about $0.55 (the backstop may add an hour) |
| Run 1: 4 prepared pages | RTX PRO 6000 | 4 h | about $8 + volume |
| Run 2: 2 original spreads | RTX PRO 6000 | 4 h | about $8 + volume |
| Run 3: a slightly larger set | RTX PRO 6000 | 6 h | about $12 + volume |

**How this sits against `config/spend.toml`.** Today it says `max_hourly_usd = "0.50"`,
`max_estimated_metered_cost_usd = "2.00"`, `hard_lifetime_seconds = 14400` (4 h). The
hand route below (`runpodctl` plus the pod guard) does not read that file, so the lead's
approval in the session is the only limit; the RTX PRO 6000 is above both money
ceilings. The soft and hard maximums being added to the policy (default soft 4 h / $2,
hard 6 h / $3) are **pending the lead's decision**; at $1.99/h a $2 soft maximum is
reached in about an hour.

**The account balance.** `account_balance_floor_usd = "50.00"` is a policy value the
file itself marks unverified. Check the RunPod balance (console, Billing) is above $50
plus the approved budget before each launch. `verbatus spend show` prints the policy's
numbers; it never reads the balance.

## 2. Set up the Mac (free)

In Terminal, from a fresh clone. uv must be exactly 0.12.1; the installer line is the
one uv publishes (to confirm).

```sh
curl -LsSf https://astral.sh/uv/0.12.1/install.sh | sh     # (to confirm)
git clone https://github.com/MegaSlick/Apparatus-Verbatus-Alpha
cd Apparatus-Verbatus-Alpha
uv sync --frozen --group test --group audit
sh .githooks/install.sh
sh .githooks/check-static.sh
source .venv/bin/activate          # once per Terminal window; puts `verbatus` on PATH
verbatus spend show
```

`runpodctl` must be installed and given the account's API key (to confirm the install
command for the Mac). The RunPod S3 keys go in the shell only, never in a file here:

```sh
read -rs RUNPOD_S3_ACCESS_KEY; export RUNPOD_S3_ACCESS_KEY
read -rs RUNPOD_S3_SECRET_KEY; export RUNPOD_S3_SECRET_KEY
```

## 3. Confirm three fixes on macOS (free)

These were fixed on Linux; only a Mac run proves them there. Each needs the test-gate
change (pull request #266) merged first (to confirm).

- **F10, the uv guard.** Expected: all pass, including the `sh` and `bash-posix` cases.
  `python -m pytest .githooks/test_ci_workflow.py -k "path_entry"`
- **F11, tests leave real state alone.** Expected: the `find` prints nothing.
  ```sh
  touch /tmp/before-tests && sh .githooks/check-all.sh
  find ~/.local/state ~/Library -newer /tmp/before-tests -iname '*verbatus*' 2>/dev/null
  ```
- **F12, the golden pins.** Expected: passes, so the HAPPY and REVIEW pins are the same
  on the Mac as on Linux. `python -m pytest pipeline/orchestrator/test_orchestrator_acceptance.py`

## 4. Check the pages on the Mac (free)

Put the four prepared RecordGold pages in their own folder under `private/`, for example
`private/rg-pages/`, and nothing else in it. The two spreads go in another, for example
`private/rg-spreads/`. Finder leaves `.DS_Store` files, which the Door refuses, so clear
them first.

```sh
find private/rg-pages private/rg-spreads -name .DS_Store -delete
verbatus upload --source private/rg-pages --manifest-out private/rg-pages-manifest.json
verbatus run --run-id local-rg-pages --submission-folder private/rg-pages \
  --submission-manifest private/rg-pages-manifest.json
```

The first line seals the folder (copied locally only). The second is **expected to stop
at the Designator, exit 2**, naming the record detector chair `'secondary_proposer'` as a
`'fixture' row`: the default roster has no real models. It proves the pages pass the data
gate, Door, Exemplar and ink map on this Mac. On Linux all six images passed: 8-bit
grey, none bilevel or 16-bit; pages 3112x4440 at 600 DPI, spreads 3864x3056 and 3672x2744.

## 5. Send the pages to the volume and prove the way home (free of GPU time)

```sh
verbatus upload --source private/rg-pages --sealed-manifest private/rg-pages-manifest.json \
  --network-volume DATACENTER:VOLUME_ID
verbatus fetch-run --run-id s3-path-check --into /tmp/verbatus-s3-check \
  --network-volume DATACENTER:VOLUME_ID
```

The upload writes `submission/` and `submission-manifest.json` on the volume. The fetch
should refuse, naming `nothing is stored under 'runs/s3-path-check/'`: that proves the
listing works. Any other error is fixed before renting. For the spreads later, seal
`private/rg-spreads` the same way and upload with `--prefix spreads`.

Pick the commit the pod will run: `git fetch origin && git rev-parse origin/main`. Use
the full 40 characters as `<sha>` below.

## 6. Guard drill: the cheapest card, one hour

Proves the pod guard arms from the start command and deletes the pod by itself.

```sh
runpodctl pod create --name verbatus-guard-drill \
  --image <RunPod Ubuntu 24.04 CUDA image> \
  --gpu-id "NVIDIA RTX A5000" --gpu-count 1 --cloud-type SECURE \
  --data-center-ids <DATACENTER> --network-volume-id <VOLUME_ID> \
  --volume-mount-path /workspace/private --container-disk-in-gb 20 --ports "22/tcp" \
  --docker-args "$(sh operations/pod/pod_start_command.sh 1 <sha>)"
runpodctl pod get <pod id>          # shows the SSH details
```

Over SSH, `tail /workspace/private/.pod_guard/guard.log` must show `armed for pod <id>`;
if not, `runpodctl pod delete <pod id>` and stop. Otherwise leave it: idle, it should
delete itself after about 30 minutes. Confirm as in step 9. (Drill disk size: to confirm.)

## 7. Launch a real run

Arm the phone ping first (from the Mac, using the pod's public-IP SSH):

```sh
sed -n 's/^NTFY_TOPIC=//p' private/ntfy.conf | tail -n 1 | tr -d "\"'" |
  ssh <pod ssh target> 'umask 077 && mkdir -p /workspace/private/.pod_guard &&
    cat > /workspace/private/.pod_guard/ntfy_topic'
```

Create the pod as in step 6, but with `--name verbatus-<run id>`,
`--gpu-id "NVIDIA RTX PRO 6000 Blackwell Server Edition"`, `--container-disk-in-gb 120`
and `pod_start_command.sh <approved hours> <sha>`. Then, on the pod over SSH, the checks
from `operations/pod/README.md` ("On the pod, over SSH"): `findmnt /workspace/private`,
the guard log, `echo "$RUNPOD_POD_ID"`, the deadline file, then

```sh
git clone https://github.com/MegaSlick/Apparatus-Verbatus-Alpha /opt/verbatus
cd /opt/verbatus && git checkout --detach <sha>
bash operations/pod/prepare_runtime.sh
UV_CACHE_DIR=/tmp/verbatus-uv-cache uv sync --frozen
```

and launch, detached:

```sh
V=/workspace/private RUN=<run id> R=/opt/verbatus
GUARD_DEADLINE=$(cat $V/.pod_guard/deadline-$RUNPOD_POD_ID) && [ -n "$GUARD_DEADLINE" ] &&
export VERBATUS_HARD_DEADLINE=$(date -u -d "@$(( GUARD_DEADLINE - 300 ))" +%Y-%m-%dT%H:%M:%SZ) &&
cd $R && setsid nohup $R/.venv/bin/python -m operations.pod.pod_run \
  --report-path $V/pod-run-report-$RUN.json --run-id $RUN \
  --submission-folder $V/submission --submission-manifest $V/submission-manifest.json \
  --mechanics-qualification --no-hold \
  --notify --hourly-usd <card price per hour plus the volume's> \
  -- \
  --volume-mount-path $V --report-path $V/bootstrap-report-$RUN.json \
  --repository $R --repository-commit <sha> --lockfile $R/uv.lock \
  --journal $V/bootstrap-journal-$RUN.json --store-root $V/model-store \
  --models-config $R/config/models-real.toml \
  --serving-recipes-config $R/config/serving_recipes_real.toml \
  > $V/pod-run-$RUN.out 2>&1 < /dev/null &
```

- **If the lead keeps three files, add
  `--witness-context-config config/witness_context-real.toml`** (as
  `$R/config/witness_context-real.toml`, after the serving recipes line). That decision is
  pending. The two-file form works only once the real-configuration change is merged; on
  today's `main` the third file is still needed (to confirm).
- `--hourly-usd` and the deadline-at-risk notice come with the deadline-estimate change,
  which is not merged yet (to confirm before launch).
- For the spreads, use `--submission-folder $V/spreads --submission-manifest $V/spreads-manifest.json`.
- Use the same `--store-root` every time, so later pods reuse the downloaded weights.

## 8. Watch it

On the pod (or by phone notifications):

```sh
cat $V/pod-run-report-$RUN.json            # bootstrapping, running, then the outcome
cat $V/pod-run-report-$RUN-liveness.json   # last_seen keeps moving while it runs
tail -f $V/pod-run-report-$RUN-transcript.log
tail -f $V/.pod_guard/guard.log
```

More time is the lead's decision: write a new deadline file before the old one passes
(`operations/pod/README.md`, "The pod guard").

## 9. Confirm the pod is gone

With `--no-hold` the pod deletes itself about a minute after the run's final report.
From the Mac:

```sh
runpodctl pod list                 # the pod must not be listed
runpodctl pod get <pod id>         # must say it is not found
```

Then open RunPod's console, Billing, and find this pod's charges. Billing can lag; until
it shows them, the shutdown is **unverified**, not done. The volume stays and keeps
billing.

## 10. Bring the results home and export (free)

```sh
verbatus fetch-run --run-id <run id> --into <local root> --network-volume DATACENTER:VOLUME_ID \
  --evidence-prefix preflight/bootstrap-report-<run id> \
  --evidence-key pod-run-report-<run id>.json \
  --evidence-key pod-run-report-<run id>-transcript.log \
  --evidence-key pod-run-report-<run id>-liveness.json \
  --evidence-key pod-run-report-<run id>-timings.json \
  --evidence-key bootstrap-report-<run id>.json \
  --evidence-key bootstrap-journal-<run id>.json \
  --evidence-key pod-run-<run id>.out --evidence-key .pod_guard/guard.log
verbatus review --run-root <local root> --run-id <run id>
verbatus export --run-id <run id> --run-root <local root>
verbatus backup --run-root <local root> --run-id <run id> --mac-directory <synced folder>
```

Add `--evidence-key pod-run-report-<run id>-estimate.json` once the deadline-estimate
change is merged. A run that holds pages exits 3; `review` says why, and each decision
is the lead's (`operations/operator/README.md`, "Recording a review decision").

## 11. Which pages, in order, and what each run proves

1. **The four prepared RecordGold single pages** (`private/` only). Proves on real pages:
   the guard, the bootstrap and weight download, preflight of every chair on the real
   configuration, every stage through to the Armarium, fetch-run, export and a verified
   shutdown. This is the first real export the project has had.
2. **Their two original spreads.** Proves the run on unsplit spreads, and lets the
   readings be compared with run 1's. Expect the ink map to report each spread's dark
   surround as one component spanning the whole frame.
3. **A slightly larger set**, for example 8 to 12 pages from `private/` or
   `scriptorium/`. Proves timing and the hold rate at a size where held pages show a
   pattern. Keep `embed_pixels = false` (the shipped default in `config/formats.toml`).
   With pixels embedded, pages like these add about 14 to 21 MB each to the archive,
   so the 192 MiB read ceiling is reached at roughly 9 to 14 pages (an estimate; F07).

## 12. Open live checks

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
- F07: peak memory for a real-size export archive, and the archive limit chosen from it.
