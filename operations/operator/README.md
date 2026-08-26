# Verbatus — the operator surface

Every rehearsal verb runs against fixtures and does not start, adopt, or bill a pod.
`upload --network-volume` is the explicitly named exception for transfer: it sends the
sealed submission record to the named RunPod network volume.

**You do not need Terminal, SSH, Python, or an AI assistant for a normal run.**
Double-click [Verbatus.command](Verbatus.command) and answer one question at a time.
Everything below explains what each word does and what it asks you before it does it —
read as much or as little as you like. The program itself always tells you what happened,
what it means, and what to do next.

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

That is deliberate, and it is why every screen says "fixture" where a real run would say
a real thing. The first real run is a separate, separately approved step.

## The ScanTailor seam

**ScanTailor Advanced is a separate desktop program; Verbatus does not pretend it is built in.**
Choose `scantailor`, give the saved project XML, and Verbatus tells you exactly which
project to open and what to do there. After you save it, give a geometry folder that
already exists to the same screen to import its split geometry — the console writes into a
folder, it never makes one, and it says so rather than failing at the boundary. The imported
document is immutable and bound to the exact project-file digest shown before the write. It records geometry only: it does
not choose a preferred page, apply a crop, or submit ScanTailor's output images. The original
submitted masters remain the Exemplar.

## The ten words

Ten things this tool can do, in the order a normal run uses them.

| Word | What the real run does | Real-run cost |
|---|---|---|
| `ingest` | Seals and checks a submitted folder, produces triage evidence, and accepts a cluster confirmation file. | No — it is podless and offline. |
| `scantailor` | Names the separate desktop handoff and records a saved ScanTailor project's geometry by digest. | No. It does not launch ScanTailor or use its output images. |
| `launch` | Rents a machine with a GPU to run the pipeline on. This build rehearses that gate with a fixture. | **Yes in a real run; no in this rehearsal.** It shows the price per hour and every limit, and makes you type a confirmation back first. |
| `boot` | Gets the rented machine ready and checks it over. This build checks fixture wiring only. | No new cost beyond a machine already running. |
| `upload` | Sends your images to storage. | No — and it needs no rented machine at all. Do it first if you like. |
| `run` | Processes the images through the pipeline. This build runs the declared synthetic fixture. | No new cost beyond the machine already running. |
| `export` | Brings the finished results back to this computer. This build makes a base Armarium evidence bundle. | No. |
| `backup` | Copies one completed or partial volume-hosted run tree to a local synced Mac directory. | No. It uses no provider credential, stores every file by SHA-256, and verifies every reused or copied byte. |
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
to the exact digests the preview just showed: if any of them changes underneath it — a
source file rewritten, a confirmation swapped for a different one, the instrument settings
edited, or the policy replaced — the write refuses rather than commit something other than
what was shown and approved on screen.

The output folder must be **beside** the submitted folder, never inside it. Records written
inside a submitted folder would be counted as submitted files by the next thing that reads
it, and the Door refuses a submission on exactly those grounds; ingest refuses first, before
writing anything.

Every submitted file must be an image the triage instrument can decode. A stray `.DS_Store`,
a text file, or a PDF makes ingest refuse the whole folder — and say how many files could not
be decoded and where they sit in the ledger's path order, so you can find and move them. A
PDF or other container reaches the Door through `upload`, which needs no triage pass.

**`upload` needs no rented machine**, so a normal order is: `upload` your images first
(zero machine cost while you do), then `launch` when you are ready to actually process
them, `boot`, `run`, `export`, `backup` your run tree to keep a local copy, and `close`
the moment you are done. Run `status` any time you are unsure what is happening or
costing money.

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
  line you typed is still good once the first window has finished and its machine is
  closed.

## `spend show`: inspect the reviewed guard

Choose **spend** in the double-click window, or run `verbatus spend show`. It shows a
configured policy's ceilings, hard-stop balance floor, and notification-only alert
threshold with the policy's SHA-256 digest. It also shows every recorded preview balance,
its source and present staleness, plus each saved notification delivery outcome with the
immutable receipt digest that recorded it. Where a receipt saved a different number of
alerts and delivery outcomes, the screen says the two cannot be paired and shows both
sides unattributed rather than guessing which outcome belongs to which alert. It does not
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
`$XDG_STATE_HOME/verbatus/` when set), outside the project checkout; `--state-dir` moves it.
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
4. **`run` processes the declared synthetic fixture**, not the files you uploaded. The two
   are not joined yet.
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
