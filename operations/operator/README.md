# Verbatus — the operator surface

The pod-lifecycle rehearsal verbs use fixtures and do not start, adopt, or bill a real
pod; local verbs may read and write operator-supplied files. `upload --network-volume`
is the explicitly named transfer exception: it sends only the files named by the sealed
submission record to the named RunPod network volume
(`operations/pod/transfer.py:94-150`).

**You do not need Terminal, SSH, Python, or an AI assistant for a normal run.**
Double-click [Verbatus.command](Verbatus.command) and answer one question at a time.
The sections below document each word and its prompts. The program always tells you what
happened, what it means, and what to do next.

If you would rather type, `python3 -m operations.operator.entry <word>` from the project
folder does exactly the same thing, and so does `verbatus <word>` once the project is
installed. All three run the same code.

## Read this first: what this is today

This is a **rehearsal**. It refuses to start, inspect, or pay for a pod — the prices, the
pod, the boot checks and the default upload target are local stand-ins, so you can
practise the whole flow without a bill. Nothing here has ever started, inspected or paid
for a real machine. The one exception is the explicitly named
`upload --network-volume`, which really does send files to a RunPod network volume (and
only that): see the upload section below.

Every screen says "fixture" where a real run would name a real resource. The first real
run requires separate approval.

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
| `upload` | Sends your images to storage. | No — and it needs no rented machine at all. Do it first if you like. |
| `run` | Processes the images through the pipeline on this computer. Without a submission it runs the declared synthetic fixture; `--submission-folder` and `--submission-manifest` send a real approved submission to the Door, and `--models-config` with `--serving-recipes-config` seal the real chair roster and its serving catalogue into the run (always the pair; one without the other is refused). | No new cost: it runs here, not on a pod. The pod's own run is `python -m operations.pod.pod_run` (`operations/pod/README.md`). |
| `fetch-run` | Brings one run tree back from the network volume a pod wrote it to, every object checked against the tree's own digests, into a local folder. | No — it reads storage only and needs no pod. It is the one word besides `upload --network-volume` that talks to the volume, so you have to name the volume. |
| `export` | Brings the finished results back to this computer. This build makes a base Armarium evidence bundle. | No. |
| `review` | Opens one run tree read-only and shows its sealed boundaries, page and act images, review queue and recorded decisions. | No. It holds no writer and no provider credential, and the operating system refuses it every write. |
| `advance` | Appends Tyrel's confirmed decision to pass one exact sealed stage boundary. | No. It shows you the seal digest and makes you type a line back naming this run, this stage and that digest. The record it appends is permanent and is never retracted. |
| `backup` | Copies one completed or partial volume-hosted run tree to a local synced Mac directory. | No. It uses no provider credential, stores every published run-tree file by SHA-256, verifies every reused or copied byte, and records any excluded RunTree publication temporaries in the snapshot. |
| `close` | Shuts the rented machine down. This build closes its fixture pod only. | A real close is what **stops** the pod cost. Always safe to run. |
| `status` | Shows what is currently going on. | No — it only reads. It never starts, changes or spends anything. |
| `spend show` | Shows the reviewed ceilings and hard-stop floor, then saved balance observations and notification-only alert outcomes. | No — it reads the policy and immutable local receipts only; it does not contact a provider or edit the policy. |

## `ingest`: prepare a folder before the Door

Choose **ingest** in the double-click window to prepare source masters before any pod
exists. It asks for the submitted folder, an **existing empty approved output folder**,
the corpus ID, triage mode, and (only when you have made one) the canonical Unit 6B
cluster-confirmation file. First it shows the sealed submission ledger, data-gate result,
instrument candidates, and every file it will write. Only then does it write the ready
folder: the ledger, producer recipe, proxies, candidate evidence, triage documents, and a
final `ingest-ready.json` handoff record.

The confirmation file is the operator act. Verbatus never makes one and never promotes an
instrument verdict on its own. It repeats a Unit 6B refusal exactly, including evidence or
membership failures. Leaving the confirmation path blank is valid: no cluster is written
and every cluster field stays null. This is podless; no provider credential enters either
confined child and no pod is started, confirmed, or billed.

The preview and the write are two separate confined launches, so each reads the submitted
folder, the confirmation file, the triage instrument settings, and the caller-selected
data-handling policy fresh. The write is pinned
to the exact digests and output-folder identity the preview just showed: if any of them
changes underneath it — a source file rewritten, a confirmation swapped for a different
one, the instrument settings edited, the policy replaced, or the selected empty folder
exchanged for another directory at the same path — the write refuses rather than commit
something other than what was shown and approved on screen.

The output folder must be **beside** the submitted folder, never inside it. Records written
inside a submitted folder would be counted as submitted files by the next thing that reads
it, and the Door refuses a submission on exactly those grounds; ingest refuses first, before
writing anything.

Every submitted file must be an image the triage instrument can decode. A stray `.DS_Store`,
a text file, or a PDF makes ingest refuse the whole folder — and say how many files could not
be decoded and where they sit in the ledger's path order, so you can find and move them. A
PDF or other container reaches the Door through `upload`, which needs no triage pass.
One ingest accepts at most 1,500 masters and 20,000 reached candidate pairs. Those ceilings
sit above the instrument suite's 1,200-frame corpus-order case and turn a larger or unusually
dense pass into a named refusal before proxy retention or full comparisons can grow without
a bound; prepare that material as smaller submitted folders.

**Neither `ingest` nor `upload` needs a rented machine**, so a normal order is: `ingest`
the submitted folder if you are preparing source masters, work its queue with `triage`,
`upload` your images (zero machine cost while you do any of that), then `launch` when you
are ready to actually process
them, `boot`, `run`, `export`, `fetch-run` to bring a pod-written run tree home,
`backup` your run tree to keep a local copy, and `close`
the moment you are done. Use `review` to read one run tree without changing anything in
it, and `advance` only once you have decided to pass a sealed boundary. Run `status` any
time you are unsure what is happening or costing money.

## The ScanTailor seam

**ScanTailor Advanced is a separate desktop program; Verbatus does not pretend it is built in.**
Choose `scantailor`, give the saved project XML, and Verbatus tells you exactly which
project to open and what to do there. After you save it, give a geometry folder that
already exists to the same screen to import its split geometry — the console writes into a
folder, it never makes one, and it says so rather than failing at the boundary. The imported
document is immutable and bound to the exact project-file digest shown before the write. It records geometry only: it does
not choose a preferred page, apply a crop, or submit ScanTailor's output images. The original
submitted masters remain the Exemplar.

## Before anything bills, it asks

`launch` is the only word that starts a bill. Before it rents anything it shows you:

- the machine's price per hour, and the attached volume's price per hour,
- what those two add up to over the whole booked lifetime,
- every configured spending limit,
- and **a line of text to type back exactly, character for character.**

That line is built out of the prices you were just shown, on purpose: it cannot be typed
from memory or pasted from an old note by someone who has not read what is about to bill.
Get it wrong, or close the window, and nothing happened — run `launch` again.

`launch` will not proceed at all without a reviewed pod-request file and a reviewed
spending-policy file. Do not invent a GPU class or a limit to get past that message: those
are Tyrel's to set, and the refusal is the tool working.

It also refuses to start or adopt a second machine while one is still recorded as open.
Run `close` for that one first.

It refuses in two more cases, both of which mean a machine may be running that this tool
cannot see:

- **A launch that never came back.** If a launch reached the provider and then lost the
  answer — the network dropped, the window was closed — no machine record was saved, but
  the safety lease that was armed just before it is still on file. A machine may be
  billing. `launch` refuses and names that lease; `status` shows it. The safety timers
  keep that machine until its booked deadline, which is what they are for. Do not start
  another one on top of it: tell Tyrel, and check the provider's own console.
- **Two windows at once.** Only one window may be part-way through a paid launch. The
  second is told so straight away rather than left waiting, and it spent nothing: the
  challenge remains unspent. Wait for the first window to finish, then run `verbatus
  status` to see whether it created a machine. If it did, a verified close is required
  before you preview again. If it did not — the first launch simply refused or failed —
  nothing needs closing; preview again so the price and request are current.

## `fetch-run`: bring a pod's run tree home

`verbatus fetch-run --run-id <id> --into <local root> --network-volume DATACENTER:VOLUME_ID`
lists everything under `runs/<id>/` on the named volume (where the pod's own run writes
its tree) and fetches each object into `<local root>/<id>/`. It needs the same two
storage-key environment variables as `upload --network-volume`, and nothing else: no
pod, no GPU-hours, no provider API key.

Every object is checked the way the run tree checks itself before it counts as fetched: a
blob must hash to its own name, a receipt to its own name, an artifact to the digest its
stage manifest recorded, `run.json` to its own self-hash, and each stage manifest must
equal the one the fetched artifacts rebuild. The authority is fetched first and the
inventories second, so a bad object stops the fetch at itself rather than after a folder
of them. An object under the prefix that no stage of a run tree accounts for is refused
by name. A publication temporary a crashed pod left beside a manifest is skipped and its
name is in the receipt. A stage that never reached a `manifest.json` cannot be checked
against a stored manifest at all; its artifacts are checked by envelope alone, and the
receipt records `"state": "verified-partial"` instead of `"verified"` — a partial run
never appears complete.

A file that already exists locally is compared, never replaced: identical bytes are reused
and counted, different bytes refuse by name and leave the local run untouched. Nothing an
attempt fetched is kept when it refuses; only files an earlier fetch already verified
survive to be reused. Run it again after a refusal — it safely reuses those files. The
receipt records counts, the stages verified and the excluded temporaries; `status` shows
it. The listing and `GetObject` path has never run against a real endpoint.

## `spend show`: inspect the reviewed guard

Choose **spend** in the double-click window, or run `verbatus spend show`. It shows a
configured policy's ceilings, hard-stop balance floor, and notification-only alert
threshold with the policy's SHA-256 digest. It also shows every recorded preview balance,
its source and present staleness, plus each saved notification delivery outcome with the
immutable receipt digest that recorded it. Where a receipt saved a different number of
alerts and delivery outcomes, the screen says the two cannot be paired and shows both
sides unattributed rather than guessing which outcome belongs to which alert. One receipt
puts at most 64 saved alert or delivery entries on the screen; anything beyond that is
counted on a final line against the same receipt digest rather than printed or dropped. A
name in the receipt folder that is a link rather than a file this tool wrote is named as
unreadable and lends its name to no digest. It does not
fetch a fresh balance and it never changes `config/spend.toml`. The deliberately
unconfigured checked-in policy refuses through the ordinary three-part console message
rather than inventing values.

## Shutting down, and what "closed" actually means

In a live-capable build, `close` asks for its own separate confirmation and then does
three things, whether or not the shutdown could be confirmed. This rehearsal exercises
the same report shape with fixture provider and billing evidence:

1. It tells you whether the machine is **confirmed gone** — proved by the provider saying
   so twice, independently, *and* by non-empty, exact-pod billing records inside a
   declared window through the requested cutoff — and what it cost through that point.
   Those records do **not** yet prove that returned billing buckets fill the whole
   declared window; that remains unproven until real RunPod lifecycle output is
   recorded. If it could not prove the observations it does require, it says
   **UNVERIFIED CLOSE** and tells you exactly what to go and check yourself.
2. It reminds you that **the storage volume keeps costing money on its own**. Closing the
   machine does not delete your storage and does not stop that charge.
3. It saves a record, so `status` can show it to you later.

If you ever see **UNVERIFIED CLOSE**, that is the one message in this whole tool to stop
and act on: open the provider's console and look. The tool never tells you there will be
no future charge — it only ever tells you what it could actually see.

## When something goes wrong

Every failure message says three things, always in the same order:

1. **What happened.**
2. **What it means** — including what was and was not started or spent.
3. **What to do next**, and whether it is safe to just do it.

You will never see a raw error with no explanation. If you ever do, that is a defect in
this tool and not something you did — save the text and pass it on.

## `status`: the one you can run any time

`status` never starts, spends or changes anything. It reads records this tool already
saved and repeats them **exactly as recorded** — it does not recalculate anything, so what
it shows you and what is on file cannot drift apart. Run it whenever you are unsure.

It also lists any **safety lease** with no verified close recorded against it, because that
is the one place a machine can be billing without a machine record to show you. A lease it
cannot read is listed as unreadable and never counted as closed.

## Phone notifications

Off unless you ask. Add `--notify` and this tool will send you one line when a `run` or an
`export` finishes, and one line when a run is **held** and needs you to decide something.
Those are the only two moments it may ever send. The terminal always tells you whether the
message actually arrived — a notification is an extra, never the only place a result
appears.

## Where it keeps its own records

Everything this tool writes for itself lives in `~/.local/state/verbatus/` by default (or
`$XDG_STATE_HOME/verbatus/` when that variable holds an absolute path outside the
project checkout; a relative value, or an absolute one that lands inside the
checkout, is ignored), outside the project checkout; `--state-dir` moves it.
Each record is written once and named after a
checksum of its own contents, so a record cannot be quietly edited afterwards and still
read back. You do not need to look in there — `status` shows you what matters.

## Alpha shortcuts this surface ships

Named here because they are real, and because
`workbench/standing/ALPHA_SHORTCUTS.md` — where they are logged — is local-only and
cannot travel in a commit.

1. **No live provider path exists.** The surface refuses any provider that is not the
   in-memory fake. Every price, pod, volume and billing record below is a stand-in.
2. **`boot` measures no real machine.** The cache check, the proof-page read and the GPU
   profile are fixtures. A green boot means the local wiring is sound, not that a GPU
   exists.
3. **`upload` writes to a local folder by default** through the same checksum-verified,
   resumable transfer a network volume uses. `--network-volume DATACENTER:VOLUME_ID`
   selects the S3-compatible target; it has never been run against a real endpoint.
4. **`run` runs on this computer, not on a pod.** With no submission it processes the
   declared synthetic fixture; with `--submission-folder` and `--submission-manifest` it
   sends a real approved submission to the Door, and with the `--models-config` /
   `--serving-recipes-config` pair it seals the real roster — but no chair is served
   here, so a real-roster run on this computer stops where a stage first needs one.
   The pod's run is `python -m operations.pod.pod_run`, and `fetch-run` is how its tree
   comes back.
5. **`export` produces a base Armarium evidence bundle**, not Spec 11's product export,
   and says so on screen every time.
6. **The fixture pod is given a fixed cost at close** so the captured-cost line has
   something real to show. It is not a measurement of anything.

## For whoever maintains this tool

- `cli.py` parses; `surface.py` is the whole behaviour; `entry.py` is a boundary thin
  enough to turn even an import failure into the three-part message.
- `errors.py` holds every operator-facing state as a closed `ErrorCode` table, checked at
  import time for the three parts, and checked by `test_errors.py` against the modules
  that actually raise — so a code with copy but no caller cannot pass as coverage.
- `records.py` owns the receipts (content-addressed, verified against their own filename
  digest on read) and the descriptor that names which receipt each verb last wrote.
  `status` uses the read paths only.
- `notify_bridge.py` allows exactly `milestone` and `decision`, and can never raise out of
  the verb that called it.
- `volume_cost.py` holds the ongoing-storage note, with the documentation it was read from
  and the date it was read.
- Close timing comes from the workspace's own `config/spend.toml`, and falls back to the
  pod runtime's operational defaults if that file is missing, unreadable, or
  unconfigured. When the file cannot be read, close says that the reviewed spend policy
  could not be read and that it is using its built-in operational deadline instead; the
  fallback is not silent. This is always the workspace default, **not** whatever path a `launch
  --spend` used — nothing records which policy path a launch was given, so close has no
  way to read it back even if it wanted to. A drill that needs a close to give up
  quickly injects a fast clock (`monotonic=`, `sleeper=`); it never shortens the shipped
  deadline.
- Nothing in this package's tests makes a live call of any kind.
