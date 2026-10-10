# Verbatus: the operator surface

**A normal run needs no Terminal, SSH, Python or AI assistant.** Double-click
[Verbatus.command](Verbatus.command) and answer one question at a time. The program always
says what happened, what it means, and what to do next.

To type instead, run `verbatus <word>` (or `.venv/bin/python -m operations.operator.entry
<word>` from the project folder). `verbatus <word> --help` lists every flag.

**On a Mac:** macOS 13 or later, Intel or Apple silicon (pypdfium2 has no wheels for older
versions). git needs the Xcode Command Line Tools (`xcode-select --install`). Their
`python3` is too old, so run Python only as `uv run …` or `.venv/bin/python …`;
`uv sync --frozen --group test --group audit` builds `.venv`.

Verbatus never starts, inspects or pays for a pod. Pods are started and closed outside it
(`operations/pod/README.md`), only with the project lead's permission. Two words reach a
RunPod network volume: `upload --network-volume` (sends only the files the sealed
submission record names) and `fetch-run` (brings back a run tree and the evidence you name;
never the uploaded images).

## The words

| Word | What it does | Costs money? |
|---|---|---|
| `prepare` | Prepares page images from a folder of scans with pagekit, and writes the triage documents that have the Door cut the same pages from the original scans | No; runs here |
| `ingest` | Seals and checks a submitted folder, produces triage evidence, accepts a cluster confirmation file | No; offline |
| `triage` | Shows `ingest`'s review queue with evidence and proxy images, and records your accept or decline (from the command line; the double-click window only shows the queue) | No; it never decides for you |
| `upload` | Sends images to a local folder, or with `--network-volume` to a volume | No pod needed; a volume bills for as long as it exists |
| `run` | Processes images through the pipeline on this computer | No; runs here |
| `fetch-run` | Brings one run tree back from a network volume, every object checked | No; reads storage only |
| `export` | Writes a base Armarium evidence bundle | No |
| `review` | Shows one run tree read-only: stages, readings, holds and why, images, the next supported action (`--json` for everything) | No |
| `decide` | Records the lead's confirmed review decision about one held unit or page | No |
| `advance` | Records the lead's confirmed decision to pass one sealed stage boundary; permanent | No |
| `backup` | Copies a volume-hosted run tree to a local synced Mac folder, verifying every byte | No |
| `status` | Shows what this tool has done, from its records | No |
| `watch` | Shows a pod run's stage, pages, finish estimate, deadline and spend from local copies of its report files | No; local files only |
| `clear-leftovers` | Lists what an interrupted publication left in one folder (`.<name>.tmp-<id>` files, `.<name>.publishing-<id>` folders); `--apply` removes them | No |
| `spend show` | Shows the reviewed pod spending policy | No |

**The normal order.** `prepare`, `ingest` (and `triage`) and `upload` need no rented
machine, so do them first. Then either `run` here, or run on a pod with
`python -m operations.pod.pod_run` and `fetch-run` the tree home. Then `export` and
`backup`. Use `review` to look, `advance` only once you have decided to pass a boundary,
and `status` whenever you are unsure what has happened.

**Exit codes.** Every word exits 0 when it did what was asked and 2 otherwise (a refusal, a
failure, a held or halted run, a partial export); the `What happened:` line says which.
`pod_run` and the orchestrator keep their own codes (`operations/pod/run_exits.py`), so the
same held run exits 3 on a pod and 2 under `verbatus run`.

`run`, `ingest`, `triage` and `spend` read configuration, stage code or proof material and
refuse (`not-a-checkout`) outside a checkout (or `--workspace`) with `pipeline/`, `config/`
and `proof/`. The other words run from anywhere.

## `prepare`: page images and the Door's geometry from scans

```sh
verbatus prepare --scans private/parish-a/scans --out private/parish-a/prepared \
    [--overrides private/parish-a/fixes.json] [--corpus-id parish-a] \
    [--crop none|page|content] [--cache DIR | --no-cache]
```

It runs pagekit (`pagekit/README.md`, "Preparing pages") and writes in the output folder:

- one lossless TIFF per page, `pagekit-prepare.json`, pagekit's project file and its
  review sheet `review.html`;
- `triage-decision-manifest.json`: the same decisions as triage rows over the **original**
  scans (actor `producer`, identity `pagekit`, colour mode `keep`), and
  `triage-producer-recipe.json` (`pagekit-producer-recipe.v1`), which the Door requires
  with producer rows;
- `triage-notes.txt`: each page the Door will cut differently from pagekit, and why.

The prepared TIFFs are for looking at; they never enter a run. The Door keeps each scan
whole as the page's `parent_frame` and cuts the page from it, so every reading traces to
the scan.

- **What the Door reproduces.** A quarter turn, skew, crop and paper margin: exactly with no
  skew, within half a pixel with one. A leaning cut, or overlap past the cut, becomes a
  straight split, and each page keeps the facing page's sliver so nothing pagekit kept is
  dropped. A shrunk page keeps the scan's resolution. An orientation tag that mirrors
  (values 2, 4, 5, 7) is refused before anything is written. A page made grey by luminance
  becomes triage colour mode `grayscale`; one made grey from a single channel is refused.
  Padding beyond the Door's limit is refused. A nominal density is not carried.
- **Cropping** is off by default; `--crop page` or `--crop content` turns it on. pagekit's
  stage cache goes beside `--out` unless `--cache DIR` or `--no-cache`; never inside the
  scans folder, and never uploaded.
- **Corrections are kept.** Run again over the same output folder: pagekit keeps every value
  set by hand and recomputes only what changed. Give corrections with `--overrides FILE`
  (`pagekit/README.md`, "Corrections").
- **Ctrl-C leaves the output folder as it was.**
- A step whose detector cannot decide takes a neutral default and flags the page for
  review.
- **Keep both folders under `private/`**, the only approved storage; the Door also refuses a
  scans folder holding a file the manifest does not cover, such as `.DS_Store`.
- Scans are decoded in a separate process that holds no credential.

To check the result at the Door on this computer (the run is expected to stop at the first
stage that needs a served chair):

```sh
verbatus upload --source private/parish-a/scans \
    --manifest-out private/parish-a/prepared/submission-manifest.json \
    --triage-decision-manifest private/parish-a/prepared/triage-decision-manifest.json \
    --triage-producer-recipe private/parish-a/prepared/triage-producer-recipe.json
verbatus --state-dir private/verbatus-state run --run-id prepared-check \
    --submission-folder private/parish-a/scans \
    --submission-manifest private/parish-a/prepared/submission-manifest.json \
    --triage-decision-manifest private/parish-a/prepared/triage-decision-manifest.json \
    --triage-producer-recipe private/parish-a/prepared/triage-producer-recipe.json
```

The upload refuses a triage manifest without a row for every sealed scan, before anything
is sent.

### Sending prepared scans to a pod

Send the sealed record with its triage documents under a prefix of its own:

```sh
P=private/parish-a/prepared
verbatus upload --source private/parish-a/scans \
    --sealed-manifest $P/submission-manifest.json --prefix prepared-spreads \
    --network-volume DATACENTER:VOLUME_ID \
    --triage-decision-manifest $P/triage-decision-manifest.json \
    --triage-producer-recipe $P/triage-producer-recipe.json
```

That writes `prepared-spreads/`, `prepared-spreads-manifest.json`,
`prepared-spreads-triage-decision-manifest.json` and
`prepared-spreads-triage-producer-recipe.json`. The pod's run names all four
(`operations/pod/README.md`). Triage sent with a submission is never replaced: after a
correction, upload again under a new `--prefix`.

## `ingest`: prepare a folder before the Door

It asks for the submitted folder, an **existing empty output folder** beside it (never
inside: anything written there would count as submitted), the corpus ID, the triage mode
and, optionally, a cluster-confirmation file. It prints the sealed ledger, the data-gate
result, the instrument candidates and every file it will write, then writes exactly that:
ledger, producer recipe, proxies, candidate evidence, triage documents and
`ingest-ready.json`.

- **The confirmation file is your act.** Verbatus never makes one or promotes an instrument
  verdict on its own. A blank confirmation path is valid.
- **A confirmed re-shoot cluster cannot go to the Door**: no later stage links two captures
  of one leaf (`pipeline/1_exemplar/CONTRACT.md`). Submit one capture per leaf.
- **Every submitted file must be a decodable image.** A stray `.DS_Store`, text file or PDF
  refuses the whole folder. Send PDFs through `upload`, which needs no triage.
- **Limits:** at most 1,500 masters and 20,000 candidate pairs per ingest; split larger
  material.
- **Images are decoded in a separate process with no credential, but not sandboxed**: it can
  read and write whatever your user account can. On Linux, Verbatus first makes itself
  non-dumpable so the child cannot read its environment. If the process dies after it
  starts writing, do not reuse the folder.

## `run`, `export` and holds

**A real submission needs its run tree under `private/`**, the only approved storage root
(`config/data_handling_policy.json`). `run` keeps its tree under the state directory, so put
the state directory there and keep it for every later word about that run:

```sh
verbatus --state-dir private/verbatus-state run --run-id <run> \
  --submission-folder private/<folder> --submission-manifest private/<folder>-manifest.json
```

Otherwise the Door refuses at once (`the run root is outside every approved storage root`).
The synthetic fixture (no submission) needs none of this. A real chair selection is the pair
`--models-config config/models-real.toml` and `--serving-recipes-config
config/serving_recipes_real.toml`; both are sealed and one without the other is refused.
`run` runs here, so a real-roster run stops where a stage first needs a served chair.
Every `run` ends by printing the `verbatus review` line for its tree.

**`export`** names the run (with no `--run-id`, the most recent) and succeeds only when the
run is `complete`. Over a held or partial run it copies what was delivered, prints every
reason, and exits `export-partial`. A record that claims `complete` but does not reconcile
is `export-unreconciled`; an unreadable one `export-missing`; a missing, invalid or changing
Armarium completion seal `export-unsealed`. None of these writes a bundle. The bundle is a
base Armarium evidence bundle, not the product export, and says so.

**A hold is not cleared by running the same run again**; that republishes the same sealed
hold. Record a review decision (below), then resume from the Recensor
(`verbatus run --run-id <run> --from recensor --to armarium`), which applies every stored
decision (`pipeline/5_recensor/CONTRACT.md`, "Operator review decisions"). A run whose
Recensor holds anything stops before the Archetypus in every mode; it exports with holds
remaining only after `advance` passes the Recensor's current seal.

**Many held pages means a problem with the run.** When the share of pages held after the
Recensor exceeds `max_held_page_share` and at least `min_systemic_held_pages` are held
(`config/review.toml`, sealed into the run; canary pages not counted), the report says the
run has a systemic problem, and so do `--notify`, `advance` and the export. Look for the
cause before deciding pages one by one.

### Finish a pod run on this computer

When a pod run ended at the Coniector, fetch its tree and run the CPU-only tail with the
same sealed configuration:

```sh
verbatus fetch-run --run-id <id> --into <local root> --network-volume DATACENTER:VOLUME_ID
.venv/bin/python pipeline/orchestrator/run.py --run-id <id> --run-root <local root> \
  --models-config config/models-real.toml \
  --serving-recipes-config config/serving_recipes_real.toml \
  --mechanics-qualification --from recensor --to armarium
```

Add `--corpus-register` if the run sealed one. If the Recensor holds a reading, `review` the
fetched tree, record the decision, and rerun the same range. A page re-ask or re-read must
resume from the Perlector with its chair available. `verbatus run --from` resumes only a run
`verbatus run` started, so use the orchestrator for a fetched tree.

## `decide`: recording a review decision

```sh
verbatus decide <decision> --run-root <folder> --run-id <run> \
  (--unit <key> | --page <ordinal>) --reason "<why>" [--finding <finding>]
verbatus decide edit --run-root <folder> --run-id <run> \
  --unit <key> --text-file <file> [--note "<note>"] --reason "<why>"
```

A unit is named by the key `review` shows (`p2:1`), a page by its ordinal. `decide` shows
the review it binds to and makes you type a line back naming the decision, subject, run and
that review's digest.

| Decision | Effect |
|---|---|
| `release` (unit), `no-missed-act` (page) | Send it to export as read. The export labels it "released by operator" with who, when, why and the codes cleared. Refused for a reading the export cannot carry (unplaced, unreadable doubt marks, no text) |
| `edit` (unit) | Correct it: your text (from `--text-file`, exact UTF-8; one final line ending dropped) becomes the reading, labelled "corrected by a person", with the model's reading exported beside it as "model reading (original)". The confirmation names the text's digest. Not possible for an unplaced reading. Two different edits of one unit leave it held as conflicting |
| `exclude` (unit) | Keep it out as not an act; exported as `excluded-with-approval` |
| `hold` (unit or page, with `--finding`), `missed-act` (page) | Keep it held. Splitting and merging readings are `hold` findings |
| `re-ask` (page), `re-shoot` (page) | Read the page again, or ask for a new image. Resume from the Perlector (`--from perlector --to armarium`): the re-read (attempt 3, 4, …) becomes the page's current reading, labelled "read on operator re-read"; earlier readings stay, marked superseded. It is outside the machine's re-ask budget. On a pod it is paid GPU work and needs the lead's permission. A unit `re-ask` only holds the unit |

`decide` refuses a subject the review does not name, a decision the subject does not allow,
and a run whose Archetypus has established a reading or whose Armarium has exported. It
writes one permanent record under the run's `receipts/sha256/` and names the next step.

## `review`

```sh
verbatus review --run-root <folder> --run-id <run> [--json]
```

It works on unfinished runs too, which is when it matters most. It shows:

- **Stages**: `sealed`, `unsealed` (interrupted or still running), `not-run`, or
  `seal-invalid` (a stored seal that no longer verifies).
- **Export**: complete, partial, or an export record under an Armarium that never sealed.
  Before export, nothing shown is a delivered result.
- **What you can do next**: the one supported continuation, a warning not to resume while a
  writer may be active, or, for an invalid seal, that this is evidence to preserve.
- **Held or unresolved acts**, with reasons and source records.
- **Pages** and **Acts**: every page against the declared count, and every act with its
  Perlector reading, witnesses, and each crop's file and digest.
- **Review queue**, after an export.

Every image is re-read and re-digested as the view is built; moved bytes or a changing
record are refused by name. Opening a run changes nothing. Long text is cut to 300
characters in the plain view (`--json` has it whole). Review flags (findings flagged rather
than held; `pipeline/5_recensor/CONTRACT.md`, "Review flags") are not shown; they appear in
the Recensor's review summary and the flagged export.

## `fetch-run`: bring a pod's run tree home

```sh
verbatus fetch-run --run-id <id> --into <local root> --network-volume DATACENTER:VOLUME_ID
```

It fetches everything under `runs/<id>/` into `<local root>/<id>/`, using the two S3 key
variables `RUNPOD_S3_ACCESS_KEY` and `RUNPOD_S3_SECRET_KEY` and no pod or API key.

- **Every object is checked as the run tree checks itself**: blobs and receipts against
  their names, artifacts against their stage manifest, `run.json` against its self-hash,
  and each manifest against what its artifacts rebuild. An unaccounted object is refused; a
  publication temporary is skipped and named. A stage with no `manifest.json` gives
  `"state": "verified-partial"`.
- **Engine logs** under `<stage>/serving-logs/` are recorded in no manifest, so each is
  digested on arrival and listed under `unverified_serving_logs`. A log that grew since an
  earlier fetch, or passed 256 MiB, is refused on its own (`refused_serving_logs`) without
  failing the tree.
- **Local files are compared, never replaced.** Different bytes refuse and leave the local
  run untouched; a refused attempt removes what it fetched (check `--into` if a removal
  itself failed).

### The launch's evidence

- **`preflight/`** is fetched into `<local root>/evidence/`. The volume holds one subtree per
  launch, so pass `--evidence-prefix preflight/<bootstrap report stem>` to fetch only this
  run's.
- **Other records** (bootstrap report and journal, pod-timer report and
  `-terminating.json`, the pod-run report and its siblings, `pod-transfer-journal.json`) lie
  under neither prefix; listing the whole volume would touch the page images. Name each with
  `--evidence-key <key>` (volume-root-relative, no leading `/`), or pass `--launch-receipt
  <path>` to derive the token-bound keys from a saved launch receipt (plus `--evidence-key
  pod-transfer-journal.json` when the launch transferred). `operations/pod/README.md`, "What
  a launch writes on the volume", lists every key.

A receipt for another volume or run, or one that proves no run, is refused. An evidence
object that cannot be fetched is named in the receipt and never fails the run tree.

## `watch`: follow a pod run from this computer

```sh
verbatus watch --run-id <run id> --receipts <folder> [--lease <lease file>] [--interval 60]
```

It reads local copies of `pod_run`'s report and its `-liveness`, `-timings`, `-estimate` and
`-progress` siblings (`--report` names the report if it is called something else). It
fetches nothing and writes nothing; copy fresh files in yourself (`operations/pod/README.md`,
"Watching it", has a loop). It shows:

- **STALE**, first, when the liveness or estimate copy is older than `--stale-minutes`
  (default 2) by this computer's clock. An ended run is never stale.
- The state, the stage and pages done of total, and when this stage finishes (this stage
  only), or `unknown` and why.
- The progress check: `ok`, `slow` or `stalled`.
- The deadline, its source, time left, and `AT RISK` when the estimate passes it; ignored
  deadline-file values. It cannot tell whether the guard is armed, and says so.
- The soft and hard maximums, and spend against them: from the verified lease with
  `--lease`, else `at least` `--hourly-usd` since `pod_run` started. An ended run whose pod
  is kept up (`held_to_hard_deadline`) is still billing.
- The last notice and whether it was delivered, each stage's duration, and liveness.

With `--interval N` it re-reads every N seconds, prints only on change, and stops when the
run ends or after `--timeout`. A missing or wrong report is refused (`watch-unreadable`,
exit 2); a missing sibling is a note.

## `upload`

`upload` writes to a local folder by default, or with `--network-volume
DATACENTER:VOLUME_ID` to the S3-compatible target, through the same checksum-verified,
resumable transfer. RunPod's S3 endpoint drops custom SHA-256 metadata, so objects are
verified by streaming their bytes under the sealed size bound.

One immutable manifest owns each prefix: the default writes `submission/` and
`submission-manifest.json`; `--prefix batch-02` writes `batch-02/` and
`batch-02-manifest.json`. Re-sending the same manifest is idempotent; a different manifest
at an occupied prefix is refused before any image is written. With
`--triage-decision-manifest` and `--triage-producer-recipe` it sends them as
`<prefix>-triage-decision-manifest.json` and `<prefix>-triage-producer-recipe.json`.

## `spend show`

Shows `config/spend.toml`'s ceilings, hard-stop balance floor and alert threshold, with the
policy's SHA-256 and hours beside each time limit. `Launch lifetime` is the deadline a
launch sets at creation; the soft maximum is where the guard's deadline sits; the hard
maximum is as far as the lead may extend it. An `unconfigured` policy is refused.

## `status`, failures and records

`status` never starts, spends or changes anything. It repeats saved records exactly as
recorded: each run's id, root, state, reasons, last output lines and `verbatus review` line;
and what exports, fetches, uploads and backups touched.

Every failure says **what happened**, **what it means** (including what was and was not
started or spent) and **what to do next**. A raw error without that is a defect: save the
text and pass it on. A failed `run` names its cause and its saved record (output, exit
status, arguments, times, commit, and every configuration file's path and SHA-256). An
interrupted run writes `interrupted-recoverable`: run it again with the same name.

Records live in `~/.local/state/verbatus/`, or `$XDG_STATE_HOME/verbatus/` when that is an
absolute path outside the checkout; `--state-dir` moves them. Each is written once and
named by its checksum, with only relative references, so the directory can be moved.

**Phone notifications** are off unless you add `--notify`: one line when a `run` or `export`
finishes and one when a run is held, including the systemic-problem line. The terminal says
whether it arrived.

## For maintainers

- `entry.py` turns even an import failure into the three-part message; `cli.py` parses each
  word; `surface.py` holds `upload`, `run`, `fetch-run`, `export` and `status`.
- `review.py` builds the read-only projection and `review_text.py` reads it out;
  `decide.py` and `advance.py` are the only modules that write an approval record.
  `ingest.py`, `triage.py` and `backup.py` each hold one word.
- `errors.py` holds every operator-facing state as a closed `ErrorCode` table, checked by
  `test_errors.py`.
- `records.py` owns the content-addressed receipts; `status` uses its read paths only.
- `notify_bridge.py` allows exactly `milestone` and `decision` and never raises into the
  calling word.
- Nothing in this package's tests makes a live call.
