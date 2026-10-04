# Verbatus — the operator surface

**You do not need Terminal, SSH, Python, or an AI assistant for a normal run.**
Double-click [Verbatus.command](Verbatus.command) and answer one question at a time. The
program always tells you what happened, what it means, and what to do next.

If you would rather type, `python3 -m operations.operator.entry <word>` from the project
folder, or `verbatus <word>` once the project is installed, runs the same code.
`verbatus <word> --help` lists every flag.

## Read this first: what this is today

Verbatus never starts, inspects or pays for a pod. A pod is started and closed outside
it (`operations/pod/README.md`), and only with the project lead's permission. Without a
named network volume, `upload` sends to a local folder.

Two words reach a RunPod network volume:

- `upload --network-volume` sends only the files named by the sealed submission record.
- `fetch-run` brings back a pod-written run tree and the launch evidence you name; it never
  fetches the uploaded images or their manifest.

## The fourteen words

Ten things this tool can do, in the order a normal run uses them, plus three you can run
any time to check on things and one that tidies up.

| Word | What the real run does | Real-run cost |
|---|---|---|
| `ingest` | Seals and checks a submitted folder, produces triage evidence, and accepts a cluster confirmation file. | No — it is podless and offline. |
| `triage` | Shows the review queue `ingest` produced — each candidate with its evidence and proxy image — and records your accept or decline against it. | No — podless and offline. It shows and it records; it never opens a master and never decides for you. The double-click window shows the queue only; a decision is recorded from the command line. |
| `upload` | Sends your images to storage. | No rented machine is needed — do it first if you like. With `--network-volume`, the volume itself costs money for as long as it exists, pod or no pod. |
| `run` | Processes the images through the pipeline on this computer. Without a submission it runs the declared synthetic fixture; `--submission-folder` and `--submission-manifest` send a real approved submission to the Door. A real chair selection is the trio `--models-config config/models-real.toml`, `--serving-recipes-config config/serving_recipes_real.toml`, and `--witness-context-config config/witness_context-real.toml`; all three are sealed into the run and a partial trio is refused. | No new cost: it runs here, not on a pod. The pod's own run is `python -m operations.pod.pod_run` (`operations/pod/README.md`). |
| `fetch-run` | Brings one run tree back from the network volume a pod wrote it to, every object checked against the tree's own digests, into a local folder. | No — it reads storage only and needs no pod. You have to name the volume. |
| `export` | Brings the finished results back to this computer. This build makes a base Armarium evidence bundle. | No. |
| `review` | Opens one run tree read-only, before or after export, and says which stages ran, what each act's latest reading and review say, which acts are held and why, the page and crop images behind them, and the one supported next action. `--json` prints the whole projection instead. | No. It only reads the run tree. |
| `decide` | Appends the project lead's confirmed review decision about one held unit or page of a run: release it to export, correct its text, exclude it, hold it with a finding, or ask for it to be read again. | No. It shows the review it binds to and makes you type a line back naming the decision, the subject, the run and that review's digest. The Recensor applies it when the run resumes. |
| `advance` | Appends the project lead's confirmed decision to pass one exact sealed stage boundary. | No. It shows you the seal digest and makes you type a line back naming this run, this stage and that digest. The record is permanent and never retracted. |
| `backup` | Copies one completed or partial volume-hosted run tree to a local synced Mac directory. | No. It uses no provider credential, stores every run-tree file by SHA-256, verifies every reused or copied byte, and records any excluded publication temporaries in the snapshot. |
| `status` | Shows what is currently going on. | No — it only reads. It never starts, changes or spends anything. |
| `watch` | Shows a pod run's stage, pages, finish estimate, deadline and spend from copies of its report files on this computer, once or every N seconds. | No — it reads local files only; it contacts no provider or volume and writes nothing. |
| `clear-leftovers` | Lists what an interrupted publication left under one folder you name (a run tree, a volume mount or an export folder): `.<name>.tmp-<id>` files and `.<name>.publishing-<id>` folders. `--apply` removes them; run it only when nothing is writing to that folder. | No. It follows no symbolic link inside the folder, and refuses one as the folder's own last name; it touches no other name, and leaves alone any file, or any folder whose content, changed within the last hour. |
| `spend show` | Shows the reviewed pod spending policy: its ceilings, hard-stop balance floor and alert threshold. | No — it reads the policy only; it does not contact a provider or edit the policy. |

**The normal order.** `ingest` and `upload` need no rented machine, so do them first:
`ingest` the submitted folder, work its queue with `triage`, `upload` the images.

- **On this computer:** `run`, then `export` and `backup`.
- **On a pod:** the pod runs `python -m operations.pod.pod_run`, which writes its tree to
  the volume; `fetch-run` brings that tree home (`export` reads only a local tree); then
  `export` and `backup`.

Use `review` to read a run tree without changing it, `advance` only once you have decided
to pass a sealed boundary, and `status` whenever you are unsure what this tool has done.

`run`, `ingest`, `triage` and `spend` read configuration, stage code or
proof material from the workspace, and refuse (`not-a-checkout`) when the folder they run
in, or the one named with `--workspace`, lacks the `pipeline/`, `config/` or `proof/` they
need. The other words never read those and run from anywhere.

## `ingest`: prepare a folder before the Door

It asks for the submitted folder, an **existing empty output folder**, the corpus ID, the
triage mode and, only when you have made one, the cluster-confirmation file. It prints
the sealed submission ledger, the data-gate result, the instrument candidates and every
file it will write, then writes them in the same run: the ledger, producer recipe,
proxies, candidate evidence, triage documents and a final `ingest-ready.json`.

- **The confirmation file is your act.** Verbatus never makes one and never promotes an
  instrument verdict on its own; it repeats the confirmation check's refusal word for
  word. A blank confirmation path is valid: no cluster is written.
- **A confirmed re-shoot cluster cannot go to the Door.** The Door refuses, whole, any
  submission whose triage names a re-shoot cluster, since no later stage links two
  captures of one leaf (`pipeline/1_exemplar/CONTRACT.md`). Submit one capture per leaf.
- **What is written is what was printed.** The plan and the write come from one
  preparation; the write first checks that the output folder is the one prepared and
  still empty.
- **The images are decoded in a separate process that holds no credential, but it is
  not sandboxed.** OS confinement was removed by the project lead's decision, so that
  process can read and write whatever your user account can. On Linux, Verbatus makes
  itself non-dumpable first, so the child cannot read Verbatus's own environment; on
  macOS and elsewhere a process of the same user can still read it. If the process dies
  before it starts writing, nothing was written and the ingest is refused; if it dies
  after, the folder may hold records and must not be reused.
- **The output folder goes beside the submitted folder, never inside it.** Anything
  written inside would count as a submitted file, and the Door would refuse the
  submission.
- **Every submitted file must be a decodable image.** A stray `.DS_Store`, text file or
  PDF refuses the whole folder, and the message says how many files failed and where they
  sit in the ledger order. Send PDFs and other containers through `upload`, which needs
  no triage.
- **Size ceilings:** at most 1,500 masters and 20,000 candidate pairs per ingest, so a
  large or dense pass refuses by name before memory grows without bound. Split larger
  material into smaller folders.

## `run`, `export` and holds

Every `run` ends by printing the exact `verbatus review --run-root … --run-id …` line for
its tree, whatever its end state; `status` prints the same line under every run.

`export` names the run first (with no `--run-id` it takes the most recent and says so) and
succeeds only when the run's recorded state is `complete`. Over a held or partial run it
copies what was delivered, prints every reason, and exits `export-partial`. A record that
claims `complete` but whose acts do not reconcile to its own total is refused with no
bundle written (`export-unreconciled`, distinct from an unreadable record,
`export-missing`); use `review` to see why.

**A hold is not cleared by running the same run name again**: that republishes the same
sealed hold. An operator review decision recorded in the run (`approval-record.v1`,
`pipeline/5_recensor/CONTRACT.md`, "Operator review decisions") resolves it when the run
resumes from the Recensor (`verbatus run --run-id <run> --from recensor --to armarium`),
which applies every decision stored; a new run over the same
sealed source is the other way. A run whose Recensor holds anything stops there, before
the Archetypus, in every mode; it exports with holds remaining only after `advance`
passes the Recensor's current seal. `review` shows what each stored decision did.

**More than a few pages held is a problem with the run.** When the share of a run's
pages held after the Recensor is above `max_held_page_share` and at least
`min_systemic_held_pages` pages are held (`config/review.toml`, sealed into the run as
`review`; canary pages are not counted), the run stops as any hold does, its report says the held share points to a systemic problem,
and with `--notify` the held notification says so. An `advance` may still pass it; the
advance check says the line again, the export carries it as a reason, and the run's and
the export's notifications lead with it. Look for the cause in the run before deciding
pages one by one.

## Recording a review decision

```sh
.venv/bin/python -m operations.operator.cli decide <decision> --run-root <folder> \
  --run-id <run> (--unit <key> | --page <ordinal>) --reason "<why>" [--finding <finding>]
.venv/bin/python -m operations.operator.cli decide edit --run-root <folder> --run-id <run> \
  --unit <key> --text-file <file> [--note "<note>"] --reason "<why>"
```

A unit decision names the unit by the key `review` shows (`p2:1`); a page decision names
the page by its ordinal. The decisions, by what the lead ruled can be done with a held
item:

- **Send it to export as read.** `release` a unit clears its own holds, the reading's own
  Perlectio holds included; `no-missed-act` on its page clears the page's. When they clear
  everything, the Archetypus establishes the model's reading exactly as read and the
  export labels it "released by operator" with who, when, why and the codes cleared. A
  reading the export cannot carry (unplaced, with unreadable doubt marks, or with no text)
  cannot be released this way: `decide` refuses to record its release, and a release
  stored by any other means leaves it held under `review-reading-held`, with nothing
  reported as cleared.
- **Correct it and send it to export.** `edit` a held unit with the corrected text, read
  from `--text-file` exactly (UTF-8; one final line ending is not part of it), and an
  optional `--note`. The confirmation line you type names the text's digest, so you
  confirm the exact text stored. Your text is taken as the truth: once nothing else holds
  the unit (its page's own holds still need `no-missed-act`), the Archetypus establishes
  it as the reading, with no machine doubt layer, labelled "corrected by a person" with
  who, when, why and your note. The model's reading stays in the run as read and goes to
  export beside yours, labelled "model reading (original)", in every package. A
  correction is delivered and counted like any accepted act and never by itself makes a
  run partial. A reading with no readable text, or with doubt marks that could not be
  read, can be corrected though it cannot be released; an unplaced reading cannot, since
  no text gives it a region on its page. Two edits of one unit agree only when they name
  the same text and note; otherwise the unit stays held as conflicting.
- **Keep it out.** `exclude` a unit as not an act; the export lists it as
  `excluded-with-approval`, citing the decision.
- **Keep it held.** `hold` a unit or page with a `--finding`, or `missed-act` on a page.
- **Send it through the stage again.** `re-ask` a page asks for it to be read again;
  `re-shoot` a page asks for a new image. Resume the run from the Perlector
  (`verbatus run --run-id <run> --from perlector --to armarium`): the Perlector reads the page again as its next
  operator re-read (attempt 3, then 4, ...), bound to your decision. That reading becomes
  the page's current one: the Recensor reviews it, the counts and the export use it, and
  its acts are labelled "read on operator re-read". The page's earlier readings and their
  records stay in the run tree as read, marked superseded by the re-read that names them.
  A re-read is outside `config/recovery.toml`'s re-ask budget and is never re-asked by the
  machine; record another `re-ask` to read it again. On a pod the re-read is paid GPU work
  and runs through the same route (`pod_run --from perlector --to armarium`), which needs
  the lead's permission like any pod start. A unit `re-ask` is recorded and holds the
  unit; the Perlector reads whole pages, so re-ask its page to read it again.

The command reads the review the decision binds to from the run tree: the Recensor's
latest published review of the unit, or of every unit on the page. It refuses a unit or
page that review does not name, a decision its subject does not allow, and a run whose
Archetypus has established a reading or whose Armarium has published its export, where a
decision recorded now could reach nothing. It writes one permanent record under the
run's `receipts/sha256/`, prints what it recorded, and names the next step: resume the
run from the Recensor (`verbatus run --run-id <run> --from recensor --to armarium`, or
`pod_run --from recensor --to armarium` on its pod), which applies every decision stored,
or from the Perlector for a page `re-ask`.

What is built: a reading goes to export as read, corrected by a person beside the
model's original, is kept out, is kept held, or is read again. Splitting and merging
readings are not decisions: each is a `hold` finding and the unit stays held.

## `review` on a run that has not finished

A run stops before the Armarium for ordinary reasons — a manual boundary, a hold, an
interruption — and that is exactly when you need to see the images, readings and reasons.

```sh
.venv/bin/python -m operations.operator.cli review --run-root <folder> --run-id <run>
```

It shows, in order:

- **Stages** — each as `sealed`, `unsealed` (records but no completion seal: interrupted
  or still running), `not-run` (nothing written; not damage), or `seal-invalid` (a stored
  seal that no longer verifies against the disk).
- **Export** — `present and complete`, `present but partial` (a real export of some acts,
  not a finished result), or an export record under an Armarium that never sealed. Before
  export, nothing shown is a delivered result.
- **What you can do next** — the one supported continuation (`verbatus run --run-id <run>`
  and the stage it resumes from), or a warning not to resume while a writer may be active,
  or, for an invalid seal, that this is evidence to preserve, not a run to resume.
- **Held or unresolved acts** — every act the Recensor left unresolved, with its reason
  and source record.
- **Pages** and **Acts** — every page counted against the pages the run declared, and
  every act the Perlector's page readings named, labelled by the stage that has not yet
  spoken or, once the Recensor has declined to accept, by that stage's own outcome word.
  Before the Perlector has read, there are no acts to list, and the view says so.
  Each act carries its Perlector reading, its witnesses, and every crop's file and digest.
- **Review queue** — only after an export, since the queue is part of the bundle.

Every image named is re-read and re-digested as the view is built; moved bytes, or a
record that changes mid-build, are refused by name. Opening a run changes nothing.

- **Long text is cut in the plain view** to 300 characters, and the line says
  `(first 300 characters as shown, of an N-character value)`. Use `--json` for the whole
  value.

## `fetch-run`: bring a pod's run tree home

```sh
verbatus fetch-run --run-id <id> --into <local root> --network-volume DATACENTER:VOLUME_ID
```

It lists everything under `runs/<id>/` on the volume and fetches it into
`<local root>/<id>/`. It needs the same two storage-key environment variables as
`upload --network-volume`, and no pod or provider API key.

**Every object is checked as the run tree checks itself**: blobs and receipts hash to
their own names, artifacts to their stage manifest, `run.json` to its self-hash, and each
manifest must equal the one its fetched artifacts rebuild. An object no stage accounts for
is refused by name; a publication temporary left by a crashed pod is skipped and named in
the receipt. A stage with no `manifest.json` is checked by envelope only, and the receipt
says `"state": "verified-partial"`, so a partial run never looks complete.

**Engine logs are the one unverifiable object.** A stage that served a chair leaves its
engine log under `<stage>/serving-logs/`. No manifest records it, so each is fetched,
digested on arrival, listed under `unverified_serving_logs`, and left out of every
verification claim. Because a serving chair may still be appending to it, a log that has
grown since an earlier fetch, or passed the 256 MiB per-object bound, is refused **on its
own** (listed under `refused_serving_logs`) without taking the verified tree down. Fetch
into a fresh `--into` for the longer log, or read an oversized one on the volume.

**Local files are compared, never replaced.** Identical bytes are reused; different bytes
refuse by name and leave the local run untouched. A refused attempt removes what it
fetched, so running it again is safe — unless a removal itself failed, which today stops
the cleanup and can leave staged files behind; check `--into` before retrying. The listing and `GetObject` path has not yet run
against a real endpoint.

### The launch's evidence

A run that billed a card has more record than its run tree:

- **`preflight/`** on the volume says which chairs were preflighted, against which
  catalogue digests, at what measured tier. It is fetched into `<local root>/evidence/`
  in the same call. The volume is reused across launches, so `preflight/` holds one
  subtree per launch; pass `--evidence-prefix preflight/<bootstrap report stem>`
  (repeatable) to fetch only this run's.
- **Ten records lie under neither prefix** — the bootstrap report and journal, the
  pod-timer runtime report and its `-terminating.json` breadcrumb, the pod-run report and
  its `-hold.json`, `-liveness.json`, `-timings.json` and `-transcript.log` siblings, and
  `pod-transfer-journal.json` at the volume root (the only durable record of which
  submission rows were verified against bytes on the target). Finding them otherwise
  would mean listing the whole volume, which holds the page images.
  `operations/pod/README.md` §"What a launch writes on the volume, and how each part comes
  home" lists each key and its derivation.

Name them with `--evidence-key <key>` (repeatable; volume-root-relative, no leading `/`),
or pass `--launch-receipt <path>` to derive the nine token-bound keys from the saved launch
receipt's sealed `docker_start_cmd`; the keys are printed before the fetch. The receipt
cannot derive `pod-transfer-journal.json`: when the launch transferred a submission, also
pass `--evidence-key pod-transfer-journal.json`. The double-click route
prompts for all ten. A receipt for a different volume or run, or one that proves no run (a
hold-only boot), is refused by name. An evidence object that cannot be fetched is named in
the receipt and never fails the run tree. The receipt records the prefixes and keys the
call used and which arrived.

**Reading the evidence:**

- `pod-runtime-report-<token>.json` — the pod timer's `bootstrap`, `close` and `green`. If
  its `-terminating.json` breadcrumb exists and the report says `close: null`, the close
  was attempted and the container was destroyed mid-verification: the normal shape.
- `pod_run`'s report — `state`, `exit_code`, `detail`, `orchestrator_argv`, the measured
  `placement_tier`, and the paths of its siblings.
- `-liveness.json` — `alive: true` stamped long before the hard deadline means the
  supervisor stopped while its child was still running.
- `-hold.json` — the paid idle time after a finished run, and the only proof the pod stayed
  alive to the hard deadline.
- `-timings.json` — one entry per stage invocation with duration, exit code and commit.
  Two commits means a resume at a different commit (`run.json` names only the creating
  commit).
- `-transcript.log` — merged orchestrator and stage output, with a named truncation marker
  if it outgrew its bound.

## `spend show`: inspect the reviewed guard

`verbatus spend show` shows the policy's ceilings, hard-stop balance floor and
notification-only alert threshold, each with the policy's SHA-256. It never fetches a
balance or edits `config/spend.toml`. The checked-in policy is deliberately unconfigured
and refuses rather than inventing values.

## When something goes wrong

Every failure message says **what happened**, **what it means** (including what was and
was not started or spent) and **what to do next**. A raw error with no explanation is a
defect in this tool: save the text and pass it on.

A failed `run` names its cause (the pipeline's last line) and its saved run record, which
keeps output, exit status, arguments, times, commit, and the path and SHA-256 of every
configuration file named. An interrupted run (Ctrl+C or a signal) writes
`interrupted-recoverable`: run it again with the same name. A killed run still leaves the
`started` record, so `status` can name it. An unclassified failure writes an `unexpected`
record with the trace, command and directory; if even that cannot be written, the message
says the screen is the only record.

## `status`: the one you can run any time

`status` never starts, spends or changes anything. It repeats saved records **exactly as
recorded**, so it cannot drift from what is on file. Each run shows its id, root, state,
failure or hold reasons, last output lines, the `verbatus review` line and its record path;
exports, fetches, uploads, backups and `unexpected` records show what they touched.

## `watch`: follow a pod run from this computer

```sh
verbatus watch --run-id <run id> --receipts <folder> [--lease <lease file>] [--interval 60]
```

`watch` reads copies of `pod_run`'s report and its `-liveness`, `-timings` and `-estimate`
siblings (`pod-run-report-<run id>.json` and so on, the hand route's names; `--report` names
the report when it is called something else). It reads only the folder you name: it does
not fetch them, contact the volume or a provider, or write anything. Getting fresh copies
onto this computer is a separate step. After the run, `fetch-run`'s evidence keys bring
them home into `<local root>/evidence/`. During the run, `fetch-run` compares rather than
replaces, so copy the four files yourself (for example `scp` from the pod) into a folder
each time; reading them over S3 from `watch` itself is the next step.

It shows, in a few short lines:

- **STALE** first, loudly, while the run is still going: the liveness copy and the
  estimate copy are each judged on their own time (`last_seen`, `updated_at`) against
  `--stale-minutes` (default 2) by this computer's clock. A stale estimate's stage, finish
  and deadline lines say the time they were true. An ended run is never called stale.
- The report's state (and exit code, hold and detail once it ended), the stage and pages
  done of total, and "this stage finishes about …" — the current stage only; later stages
  are not counted. With no estimate it says `unknown` and why: a failing estimate says
  "the estimate is failing" with its last error, and a failed estimate write is counted.
  An ended run shows no stage estimate.
- **An ended run whose pod is kept up** (`held_to_hard_deadline`) says the pod is still
  billing until the hard deadline, and counts spend to now.
- The deadline and the time left, with its source, whether it can be extended by hand,
  and `AT RISK` when the estimate passes it. With no deadline in the estimate it shows the
  bootstrap's hard deadline from the report and says why. It cannot tell whether the guard
  is armed, and says so; deadline-file values the pod ignored are listed.
- The soft and hard maximums, from the estimate file (the budget the pod read).
- Spend, set against the soft and hard maximums: with `--lease`, the pod and volume rate
  from the verified lease since the pod was created; without it, `at least` pod_run's
  `--hourly-usd` since pod_run started. A lease whose seal does not verify is named and
  not used.
- The last notice `pod_run` recorded, and whether it was delivered; each finished stage
  invocation's duration, with unreadable timings lines counted; the liveness age.

Without `--interval` it shows once. With `--interval N` it reads again every N seconds,
prints only when a copy changed or turned stale, reads once more after a pause when a
copy did not parse (it may have been caught mid-copy), and stops when the report says the run
ended or after `--timeout`. Ctrl+C stops it. A missing or another run's report is refused
(`watch-unreadable`); a missing or unreadable sibling is named as a note.

Not built yet: reading the files over S3 itself, `--json`, and the design's run states and
exit codes (`docs/design/control-surface.md`).

## Phone notifications

Off unless you add `--notify`. Then it sends one line when a `run` or `export` finishes and
one when a run is **held** for a decision, and nothing else. A run held on more than its
sealed share of pages says in that line that it has a systemic problem, and so does the
notice of a run or export a person's advance let past that hold. A run on the pod sends
the same systemic line as a `decision` from `pod_run` (`operations/pod/notify_hooks.py`),
read from the orchestrator's stop record. The terminal always says whether the message
arrived.

## Where it keeps its own records

`~/.local/state/verbatus/` by default, or `$XDG_STATE_HOME/verbatus/` when that is an
absolute path outside the checkout; `--state-dir` moves it. Each record is written once
and named by the checksum of its contents, so it cannot be edited and still read back.
Every reference inside is relative, so the directory can be copied, moved or restored
elsewhere and still read.

## Alpha shortcuts this surface ships

1. **`upload` writes to a local folder by default**, through the same checksum-verified,
   resumable transfer a network volume uses; `--network-volume DATACENTER:VOLUME_ID`
   selects the S3-compatible target. RunPod's S3 endpoint discards custom SHA-256
   metadata on upload, so objects without it are verified by streaming their bytes under
   the sealed size bound; that path has run against a live endpoint once.

   One immutable manifest owns each object prefix. The default `submission` writes images
   under `submission/` and the ledger as `submission-manifest.json`; `--prefix batch-02`
   writes `batch-02/` and `batch-02-manifest.json`, and its pod request must name matching
   submission paths. Re-sending the same manifest is idempotent; a different manifest at
   an occupied prefix is refused before any image is written.
2. **`run` runs on this computer, not on a pod**, so a real-roster run stops where a stage
   first needs a served chair. Use the shipped real trio together; a custom roster needs
   an operator-authored witness declaration. The pod's run is
   `python -m operations.pod.pod_run`, and `fetch-run` brings its tree home.
3. **`export` produces a base Armarium evidence bundle**, not the product export,
   and says so on screen.

## For whoever maintains this tool

- `entry.py` is thin enough to turn even an import failure into the three-part message;
  `cli.py` parses each word and runs it; `surface.py` holds `upload`, `run`, `fetch-run`,
  `export` and `status`.
- `review.py` builds the read-only projection of a run tree and `review_text.py` reads it
  out; `decide.py` and `advance.py` are the only modules that write an approval record.
  `ingest.py`, `triage.py` and `backup.py` each hold one word.
- `errors.py` holds every operator-facing state as a closed `ErrorCode` table;
  `test_errors.py` checks it against the modules that raise, so unused copy cannot pass as
  coverage.
- `records.py` owns the content-addressed receipts and the descriptor naming each verb's
  latest receipt. `status` uses its read paths only.
- `notify_bridge.py` allows exactly `milestone` and `decision` and never raises into the
  calling verb.
- Nothing in this package's tests makes a live call.
