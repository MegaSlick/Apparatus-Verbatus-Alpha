# Verbatus — the operator surface

**You do not need Terminal, SSH, Python, or an AI assistant for a normal run.**
Double-click [Verbatus.command](Verbatus.command) and answer one question at a time. The
program always tells you what happened, what it means, and what to do next.

If you would rather type, `python3 -m operations.operator.entry <word>` from the project
folder, or `verbatus <word>` once the project is installed, runs the same code.
`verbatus <word> --help` lists every flag.

## Read this first: what this is today

This is a **rehearsal**. It refuses to start, inspect, or pay for a pod: the prices, the
pod, the boot checks and the default upload target are local stand-ins, so you can
practise the whole flow without a bill. Every screen says "fixture" where a real run
would name a real resource. The first real run needs the project lead's approval.

Two words really reach a RunPod network volume:

- `upload --network-volume` sends only the files named by the sealed submission record.
- `fetch-run` brings back a pod-written run tree and the launch evidence you name; it never
  fetches the uploaded images or their manifest.

## The fifteen words

Thirteen things this tool can do, in the order a normal run uses them, plus two you can run
any time to check on things.

| Word | What the real run does | Real-run cost |
|---|---|---|
| `ingest` | Seals and checks a submitted folder, produces triage evidence, and accepts a cluster confirmation file. | No — it is podless and offline. |
| `triage` | Shows the review queue `ingest` produced — each candidate with its evidence and proxy image — and records your accept or decline against it. | No — podless and offline. It shows and it records; it never opens a master and never decides for you. The double-click window shows the queue only; a decision is recorded from the command line. |
| `scantailor` | Names the separate desktop handoff and records a saved ScanTailor project's geometry by digest. | No. It does not launch ScanTailor or use its output images. |
| `launch` | Rents a machine with a GPU to run the pipeline on. This build rehearses that gate with a fixture. | **Yes in a real run; no in this rehearsal.** It shows the price per hour and every limit, and makes you type a confirmation back first. |
| `boot` | Gets the rented machine ready and checks it over. This build checks fixture wiring only. | No new cost beyond a machine already running. |
| `upload` | Sends your images to storage. | No rented machine is needed — do it first if you like. With `--network-volume`, the volume itself costs money for as long as it exists, pod or no pod. |
| `run` | Processes the images through the pipeline on this computer. Without a submission it runs the declared synthetic fixture; `--submission-folder` and `--submission-manifest` send a real approved submission to the Door. A real chair selection is the trio `--models-config config/models-real.toml`, `--serving-recipes-config config/serving_recipes_real.toml`, and `--witness-context-config config/witness_context-real.toml`; all three are sealed into the run and a partial trio is refused. | No new cost: it runs here, not on a pod. The pod's own run is `python -m operations.pod.pod_run` (`operations/pod/README.md`). |
| `fetch-run` | Brings one run tree back from the network volume a pod wrote it to, every object checked against the tree's own digests, into a local folder. | No — it reads storage only and needs no pod. You have to name the volume. |
| `export` | Brings the finished results back to this computer. This build makes a base Armarium evidence bundle. | No. |
| `review` | Opens one run tree read-only, before or after export, and says which stages ran, what each act's latest reading and review say, which acts are held and why, the page and crop images behind them, and the one supported next action. `--json` prints the whole projection instead. | No. It holds no writer and no provider credential, and the operating system refuses it every write. |
| `advance` | Appends the project lead's confirmed decision to pass one exact sealed stage boundary. | No. It shows you the seal digest and makes you type a line back naming this run, this stage and that digest. The record is permanent and never retracted. |
| `backup` | Copies one completed or partial volume-hosted run tree to a local synced Mac directory. | No. It uses no provider credential, stores every run-tree file by SHA-256, verifies every reused or copied byte, and records any excluded publication temporaries in the snapshot. |
| `close` | Shuts the rented machine down. This build closes its fixture pod only. | A real close is what **stops** the pod cost. Always safe to run. |
| `status` | Shows what is currently going on. | No — it only reads. It never starts, changes or spends anything. |
| `spend show` | Shows the reviewed ceilings and hard-stop floor, then saved balance observations and notification-only alert outcomes. | No — it reads the policy and immutable local receipts only; it does not contact a provider or edit the policy. |

**The normal order.** `ingest` and `upload` need no rented machine, so do them first:
`ingest` the submitted folder, work its queue with `triage`, `upload` the images.

- **On this computer:** `run`, then `export` and `backup`.
- **On a pod:** `launch`, `boot`; the pod runs `python -m operations.pod.pod_run`, which
  writes its tree to the volume; `fetch-run` brings that tree home (`export` reads only a
  local tree); then `export`, `backup`, and `close` the moment you are done. Use `review` to read
a run tree without changing it, `advance` only once you have decided to pass a sealed
boundary, and `status` whenever you are unsure what is happening or costing money.

`run`, `boot`, `ingest`, `triage`, `launch` and `spend` read configuration, stage code or
proof material from the workspace, and refuse (`not-a-checkout`) when the folder they run
in, or the one named with `--workspace`, lacks the `pipeline/`, `config/` or `proof/` they
need. The other words never read those and run from anywhere.

## `ingest`: prepare a folder before the Door

It asks for the submitted folder, an **existing empty output folder**, the corpus ID, the
triage mode and, only when you have made one, the cluster-confirmation file. It first
shows the sealed submission ledger, the data-gate result, the instrument candidates and
every file it will write; only then does it write the ledger, producer recipe, proxies,
candidate evidence, triage documents and a final `ingest-ready.json`.

- **The confirmation file is your act.** Verbatus never makes one and never promotes an
  instrument verdict on its own; it repeats the confirmation check's refusal word for
  word. A blank confirmation path is valid: no cluster is written.
- **The write is pinned to the preview.** Preview and write are two separate confined
  launches. If any source file, the confirmation, the instrument settings, the policy or
  the output folder changes in between, the write refuses rather than commit something
  other than what you approved.
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
sealed hold. Only a new authorized run over the same sealed source resolves it.

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
- **Held or unresolved acts** — every act the Designator or Recensor left unresolved, with
  its reason and source record. One act can give two rows (the Designator's hold and the
  Recensor's review of it); each row says which.
- **Pages** and **Acts** — every page counted against the pages the run declared, and
  every act the Designator's proposal seal expects, labelled by the stage that has not yet
  spoken or, once the Recensor has declined to accept, by that stage's own outcome word.
  Each act carries its Perlector reading, its witnesses, and every crop's file and digest.
- **Review queue** — only after an export, since the queue is part of the bundle.

Every image named is re-read and re-digested as the view is built; moved bytes, or a
record that changes mid-build, are refused by name. Opening a run changes nothing.

Two limits:

- **Long text is cut in the plain view** to 300 characters, and the line says
  `(first 300 characters as shown, of an N-character value)`. Use `--json` for the whole
  value.
- **It handles small runs only.** Every page and crop is read and digested in one pass
  under a 256 MiB allowance, so a parish-sized run is refused by name. A console for real
  volumes has to verify one image at a time as it renders.

## The ScanTailor seam

**ScanTailor Advanced is a separate desktop program; Verbatus does not pretend it is built
in.** Choose `scantailor`, give the saved project XML, and Verbatus tells you which project
to open and what to do there. After you save it, give an existing geometry folder (the
console never creates one) to import the split geometry. The imported document is
immutable and bound to the project-file digest shown before the write. It records geometry
only: no preferred page, no crop, no ScanTailor output images. The submitted masters remain
the Exemplar.

## Before anything bills, it asks

`launch` is the only word that starts a bill. Before it rents anything it shows the
machine's and the volume's price per hour, their total over the booked lifetime, every
configured spending limit, and **a line of text to type back exactly**. That line is built
from the prices just shown, so it cannot be typed from memory or pasted from an old note.
Get it wrong or close the window and nothing happened.

It refuses:

- **without a reviewed pod-request file and spending-policy file.** Do not invent a GPU
  class or a limit to get past this: those are the project lead's to set.
- **while another machine is recorded as open.** Run `close` for that one first.
- **after a launch that never came back.** If a launch reached the provider and lost the
  answer, no machine record exists but the safety lease armed before it does, and a
  machine may be billing. `launch` names that lease and `status` shows it. Do not start
  another machine: tell the project lead and check the provider's own console. The safety
  timers hold that machine only until its booked deadline.
- **in a second window** while the first is part-way through a paid launch. The second
  spent nothing. When the first finishes, run `verbatus status`: if it created a machine,
  close it (verified) before previewing again; if not, preview again so the price is
  current.

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
notification-only alert threshold with the policy's SHA-256, then every recorded preview
balance (source and staleness) and saved notification outcome with its receipt digest.
Where a receipt's alert and outcome counts differ, both sides are shown unpaired rather
than guessed; past 64 entries per receipt the rest are counted, not printed. It never
fetches a balance or edits `config/spend.toml`. The checked-in policy is deliberately
unconfigured and refuses rather than inventing values.

## Shutting down, and what "closed" actually means

`close` asks for its own confirmation, then (this rehearsal uses fixture evidence):

1. says whether the machine is **confirmed gone** — the provider saying so twice,
   independently, *and* non-empty billing records for that exact pod inside a declared
   window — and what it cost to that point. Those records do not yet prove the billing
   buckets fill the whole window; that stays unproven until real RunPod output is
   recorded. Anything short of this is **UNVERIFIED CLOSE**, with what to check yourself;
2. reminds you that **the storage volume keeps costing money** — closing the machine does
   not delete or stop it;
3. saves a record for `status`.

**UNVERIFIED CLOSE is the one message to stop and act on:** open the provider's console
and look. The tool never promises no future charge; it reports only what it could see.

Close timing comes from the workspace's `config/spend.toml`; if that is missing, unreadable
or still unconfigured (the checked-in state), close says so and uses the built-in
operational deadline. It never reads the
policy a `launch --spend` named, because no record keeps that path.

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

It also lists every **safety lease** with no verified close, because that is where a
machine can bill without a machine record. An unreadable lease is listed as such, never
counted as closed.

## Phone notifications

Off unless you add `--notify`. Then it sends one line when a `run` or `export` finishes and
one when a run is **held** for a decision, and nothing else. The terminal always says
whether the message arrived.

## Where it keeps its own records

`~/.local/state/verbatus/` by default, or `$XDG_STATE_HOME/verbatus/` when that is an
absolute path outside the checkout; `--state-dir` moves it. Each record is written once
and named by the checksum of its contents, so it cannot be edited and still read back.
Every reference inside is relative, so the directory can be copied, moved or restored
elsewhere and still read.

## Alpha shortcuts this surface ships

1. **No live pod provider path exists.** Every price, pod, volume and billing record is
   the in-memory fake's. The one live path is `upload --network-volume` (and `fetch-run`),
   which reach a real RunPod network volume over S3 (item 3).
2. **`boot` measures no real machine.** A green boot means the local wiring is sound, not
   that a GPU exists.
3. **`upload` writes to a local folder by default**, through the same checksum-verified,
   resumable transfer a network volume uses; `--network-volume DATACENTER:VOLUME_ID`
   selects the S3-compatible target. RunPod's S3 endpoint discards custom SHA-256
   metadata on upload, so objects without it are verified by streaming their bytes under
   the sealed size bound; that path has run against a live endpoint once.

   One immutable manifest owns each object prefix. The default `submission` writes images
   under `submission/` and the ledger as `submission-manifest.json`; `--prefix batch-02`
   writes `batch-02/` and `batch-02-manifest.json`, and its pod request must name matching
   submission paths. Re-sending the same manifest is idempotent; a different manifest at
   an occupied prefix is refused before any image is written.
4. **`run` runs on this computer, not on a pod**, so a real-roster run stops where a stage
   first needs a served chair. Use the shipped real trio together; a custom roster needs
   an operator-authored witness declaration. The pod's run is
   `python -m operations.pod.pod_run`, and `fetch-run` brings its tree home.
5. **`export` produces a base Armarium evidence bundle**, not the product export,
   and says so on screen.
6. **The fixture pod is given a fixed cost at close.** It measures nothing.

## For whoever maintains this tool

- `cli.py` parses; `surface.py` is the whole behaviour; `entry.py` is thin enough to turn
  even an import failure into the three-part message.
- `errors.py` holds every operator-facing state as a closed `ErrorCode` table;
  `test_errors.py` checks it against the modules that raise, so unused copy cannot pass as
  coverage.
- `records.py` owns the content-addressed receipts and the descriptor naming each verb's
  latest receipt. `status` uses its read paths only.
- `notify_bridge.py` allows exactly `milestone` and `decision` and never raises into the
  calling verb.
- `volume_cost.py` holds the storage-cost note and the documentation it came from.
- A drill that needs close to give up quickly injects a fast clock (`monotonic=`,
  `sleeper=`); it never shortens the shipped deadline.
- Nothing in this package's tests makes a live call.
