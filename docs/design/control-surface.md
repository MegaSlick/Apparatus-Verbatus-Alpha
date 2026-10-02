# Control surface: design

This is the design for one way to drive Apparatus Verbatus, whether a person at a terminal,
a person in a browser, or an AI model with no prior context is doing the driving. Nothing
here is built yet. A later session builds it in the order given in "Build order", and
changes this document when something it builds turns out differently.

It builds on what exists once the cleanup pull requests have merged:

- the `verbatus` command (`operations/operator/`): `ingest`, `triage`, `upload`, `run`
  (including `--from <stage> --to armarium`, which resumes a run under the settings it was
  started with), `fetch-run`, `export`, `status`, `spend show`, `review` (with `--json`),
  `decide`, `advance`, `backup` and `clear-leftovers`;
- the closed error table in `operations/operator/errors.py`, where every code carries
  what happened, what it means and what to do next;
- the run tree (`common/runtree/`): `run.json`, each stage's records and completion seal,
  and the receipts;
- the orchestrator's exit codes: 0 complete, 2 fatal, 3 held, 4 halted;
- the hand route for pods (`operations/pod/README.md`): `runpodctl`, the guard armed at
  creation, `pod_run` on the pod, and `fetch-run` to bring results home;
- the run-progress estimate and the deadline-extension request, which the pod half of
  the cleanup train builds before the first live run.

## What it is for

- **One source of truth.** Every action is one `verbatus` command. The local web page,
  the local JSON API and the MCP server for AI models all call those same commands and
  return the same JSON. None of them has logic of its own.
- **Readable by a machine, and by a person.** Every command takes `--json` and then prints
  exactly one JSON object. Without `--json` it prints the same facts in plain English.
- **Safe to hand to an AI.** An agent that has never seen the project can learn every
  command, state, event and error from `verbatus help --json`, follow a run from start
  to finish, and is never able to spend money without the lead's yes.

## Principles the surface keeps

1. **The run tree is the truth.** A run's state is read from what the run tree records,
   each time it is asked for. No state file of the surface's own can disagree with the
   evidence.
2. **Reading is free and always allowed.** Anything that only reads never asks for
   confirmation and never contacts a paid service except to read.
3. **A paid action needs a quote and a yes.** Starting a pod and extending its deadline
   are the only paid actions. Each is done in two steps: a quote that shows the card, the
   hourly price and the total, then a confirmation bound to that exact quote.
4. **The guard is always on.** No path creates a pod without the pod guard armed in its
   start command, and the surface checks that the guard really armed.
5. **Register material stays private.** Page images and transcriptions never go to a
   hosted service unless the user turned that on for that provider.
6. **Every refusal says what to do.** Every non-success result carries a stable code, the
   three plain sentences, and where possible a remedy command an agent can offer.

## The commands

### How every command answers

With `--json`, every command prints one object to standard output and nothing else.
Progress lines and warnings meant for a person go to standard error, so the JSON is never
mixed with them.

```json
{
  "schema": "verbatus.result.v1",
  "command": "status",
  "ok": true,
  "exit": 0,
  "data": { "schema": "verbatus.status.v1" },
  "warnings": [],
  "error": null,
  "next_action": {
    "command": "verbatus watch --run-id demo",
    "why": "The run is still going; watch it until it stops.",
    "cost": "free"
  }
}
```

- `data` has its own `schema` per command (`verbatus.<command>.v1`).
- `warnings` is a list of warning objects (see "Error and warning codes").
- `error` is null on success, or one error object with `code`, `what_happened`,
  `what_it_means`, `next_step`, `remedy` and `detail`.
- `next_action` is the one supported next step, or null. It names its cost: `free`,
  `local` (uses this computer only) or `paid`. A paid next action is always a quote,
  never the confirmation itself.

### Exit codes, the same for every command

| Exit | Meaning |
|---|---|
| 0 | Done, as asked. |
| 2 | Refused or failed. Nothing was claimed done; the error code says why. |
| 3 | Needs a person: a held run, a partial export, pages flagged for review. |
| 4 | Halted: the run hit the hard-failure cap and stays stopped. |
| 5 | Waiting for a confirmation: a quote or preview was shown and nothing was done. |
| 6 | Unverified: something may have happened that could not be confirmed. Go and look. |

0, 2, 3 and 4 mean what they already mean for the orchestrator. Exit 6 is the
"never read as nothing happened" rule the pod tools already keep: it is used, for
example, when a pod delete was sent but the provider has not yet confirmed the pod is
gone. Today the operator returns 2 for every failure, including a held run and a partial
export; those move to 3 when this is built.

### The command list

Every command below also takes `--json`, `--state-dir` and `--workspace` as today.
"Never" lists what the command will not do, whatever it is asked.

#### `verbatus help`

- **Inputs:** optional command name.
- **Output:** `verbatus.help.v1`: every command with its arguments, cost class, whether
  it needs a confirmation, its exit codes and its output schema name; every error and
  warning code with its copy and remedy; every run state; every event type; the
  surface's version.
- **Never:** reads a run or contacts anything.

#### `verbatus submit` (today's `ingest`)

The brief calls this "stage". It is named `submit` because "stage" already means a
pipeline stage everywhere else (`--from <stage>`, the glossary), and one word with two
meanings would confuse a person and an agent alike.

- **Inputs:** the folder of page images, an empty output folder beside it, the corpus id,
  the triage mode, an optional cluster-confirmation file. `--preview` shows what would be
  written; without it the write happens, pinned to the preview as today.
- **Output:** `verbatus.submit.v1`: submission id (the ledger's digest), file count,
  data-gate result, triage candidates by kind, the files written, and the path of
  `ingest-ready.json`.
- **Exit:** 0 written; 5 preview only; 2 refused (for example a file that is not an
  image).
- **Never:** changes or moves a submitted file; writes inside the submitted folder;
  uploads anything.

#### `verbatus preflight`

Local checks on a submission before any pod is rented, so a bad page costs nothing.

- **Inputs:** the submission (its output folder), optional `--for-pod` to add the checks
  a pod run needs.
- **Checks:** every file decodes; page count and size limits; resolution (short side and
  DPI); the pagekit crop check on every declared crop or split (ink thrown away, ink cut
  at an edge, split distance from the gutter); the data gate. With `--for-pod`: the
  model, serving and witness configuration trio is present and consistent; the spend
  policy is configured; the storage keys are set and the volume answers a list (the
  `fetch-run` path check the hand route already uses, which is a read); the guard's
  commit is on `main`.
- **Output:** `verbatus.preflight.v1`: one row per page with its measurements, flags and
  reasons; totals; the pagekit report digest per page; whether thresholds are calibrated
  (`thresholds_measured`). Flagged pages are listed in the order a person should look.
- **Exit:** 0 nothing flagged; 3 pages flagged for review; 2 the checks could not run.
- **Never:** changes a page; rents or contacts a pod; sends an image anywhere. While the
  pagekit thresholds are uncalibrated, "no flags" is reported as "nothing found", never
  as "the crops are right".

#### `verbatus preprocess`

Proposals for page geometry from pagekit (deskew, gutter split, content box), applied
only with the user's consent. It depends on pagekit's second slice; until that exists the
command answers with `preprocess-unavailable`.

- `preprocess propose`: writes proposals into the triage manifest. Output:
  `verbatus.preprocess.v1` with one proposal per page, each with a digest, the before and
  after geometry, and the paths of a before/after preview image.
- `preprocess show --page <n>`: the before/after view for one page.
- `preprocess apply --proposal <digest> [--proposal ...]`: records consent and writes
  versioned processed copies with their provenance. It shows the list and asks for a yes
  first (exit 5 until confirmed with `--yes`). A proposal whose page or settings changed
  since it was shown is refused (`proposal-stale`).
- **Never:** changes an original; applies a proposal nobody accepted; drops a page (a
  dropped page is already a hard failure).

#### `verbatus start`

- **Inputs:** `--run-id`, the submission (or a fixture), the configuration trio, and
  where: `--local` (the default) or `--pod`. Resume uses the same command with `--from
  <stage>` and always ends at the Armarium, as `verbatus run` does today.
- **Local:** today's `verbatus run`, with the run lock and event log described below. It
  returns at once with `--detach`, or runs in the foreground until the run stops.
- **On a pod:** needs a live pod started with `verbatus pod start`, and sends the run
  request to it (see "Pods"). Until the run-on-start entry exists, `--pod` answers with
  the exact hand-route commands to run over SSH, filled in for this run.
- **Output:** `verbatus.start.v1`: run id, run root, where it runs, the bindings recorded,
  the event log path, the first state.
- **Exit:** 0 started (detached) or complete; 3 held; 4 halted; 2 refused (for example
  `run-already-running`).
- **Never:** rents a pod; resumes a run under settings other than the ones it started
  with; starts a second writer on a run that has a live one.

#### `verbatus status`

- **Inputs:** none for everything, or `--run-id` for one run.
- **Output:** `verbatus.status.v1`: each run's derived state (see "Run states"), its
  stage table, pages done per stage, the estimate when one exists, holds, the last
  event's sequence number, the `review` line; every live pod with card, price per hour,
  time used, guard deadline and guard heartbeat age; every lease or launch whose close is
  not verified.
- **Never:** starts, changes or spends anything. Reading the pod list and the volume is a
  read.

#### `verbatus pause` and `verbatus resume`

- `pause --run-id`: asks the run to stop at the next stage boundary. The orchestrator
  already stops cleanly between stages; it now also checks a pause request there. The
  answer is `pause-pending` (exit 0) until the boundary is reached, then the state reads
  `paused`.
- `resume --run-id`: continues a paused or interrupted run from its first stage that is
  not sealed, under its recorded settings. It is `start --from <stage>` with the stage
  worked out for you.
- **Never:** stops a stage half-way (its work would be lost); clears a hold (that is
  `decide` or `advance`).

#### `verbatus inspect`

The `review` projection, one piece at a time, so it works on a run of any size.

- `inspect run --run-id`: the stage table, holds and next action.
- `inspect page --run-id --page <n>`: one page's image digests, its reading, its acts,
  witnesses, holds and decisions.
- `inspect act --run-id --act <key>`: one act's reading, region, crops, witnesses, holds
  and decisions.
- **Output:** `verbatus.inspect.v1`, the same fields `review --json` has for that page or
  act. Each image is re-read and re-digested as it is shown, one at a time, which removes
  today's 256 MiB limit on `review`.
- **Never:** writes anything.

#### `verbatus decide` and `verbatus advance`

As today. With `--json` they print the binding they will record (review digest or seal
digest) and the line to type back, and exit 5; the record is written only when that line
comes back (`--confirm "<line>"`). Neither costs money, so neither needs the lead's paid
yes, but both are the lead's decisions and say so in their output.

#### `verbatus export`

- **Inputs:** `--run-id` (default: the most recent, named in the output), `--run-root`.
- **Output:** `verbatus.export.v1`: bundle path and digest, run state, lot, formats
  written, counts (acts delivered, held, excluded), and every reason a partial export is
  partial.
- **Exit:** 0 complete; 3 partial; 2 refused (`export-missing`, `export-unreconciled`).
- **Never:** exports a run whose record does not reconcile; hides that an export is
  partial.

#### `verbatus watch`

Follows a run's events (see "Events"). It prints one event per line and returns when the
run stops or waits for a person.

#### `verbatus pod quote | start | extend | stop | status`

See "Pods".

#### `verbatus lot`

`verbatus lot <lot>` looks up a lot number in the local ledger and answers with the run,
its `run.json` digest, commit, settings used, run root and exports. See "Lot numbers and
settings".

#### Words that stay as they are

`upload`, `fetch-run`, `backup`, `spend show`, `clear-leftovers` and `triage` keep their
behaviour and gain `--json`. `review` stays as the whole-run view for small runs, and
`inspect` is the paged view.

## Run states

### Where a state comes from

The state of a run is worked out each time it is asked for, from these records, in this
order of authority:

1. **The run tree**: `run.json` (does the run exist, and what is it); each stage's seal
   (sealed, written but unsealed, not run, or seal no longer valid; the same four
   states `review` already reads); the Recensor's holds and any advance record that
   passes its current seal; the hard-failure tally; the Armarium's export record
   (complete or partial).
2. **The last invocation's record**: the operator's run receipt (local) or `pod_run`'s
   report and the orchestrator's stop record (pod). They hold the one fact the tree
   cannot: how the last invocation ended (its exit code) and the settings it was started
   with.
3. **Liveness**: whether a writer is active now. Locally that is the run lock (an
   operating-system lock on `<state dir>/locks/<run id>.lock`, held by the process running
   the orchestrator and released when it dies). On a pod it is `pod_run`'s liveness file
   being fresh (seen within two minutes) on a pod that still exists.
4. **A pause request**, if one is waiting.

When the tree and a record disagree, the tree wins and the answer carries the warning
`record-disagrees-with-tree`. For example, a receipt that says complete over an Armarium
with no seal reads as `interrupted`, with the warning. The event log is never read to work
out a state.

### The states

| State | What it means | Read from |
|---|---|---|
| `absent` | No run by this id. | No `run.json`, no receipt. |
| `running` | A writer is active. | Liveness, and some stage not yet sealed. |
| `paused` | Stopped at a boundary because someone asked. | Pause request, no writer, last exit clean, next stage not run. |
| `interrupted` | Stopped mid-way with no writer: a crash, a closed laptop, a pod that ran out of time. | A stage written but unsealed (or the next stage not run after a clean one) and no writer, with no pause or hold to explain it. |
| `held` | Waiting for a person's decision. | Recensor holds with no advance passing its seal, or an Attestatores hold; last exit 3. |
| `halted` | The hard-failure cap was passed; the run stays stopped. | Hard-failure tally over the cap; last exit 4. |
| `failed` | The last invocation failed and named its cause. | Last exit 2, no writer. |
| `complete` | Exported in full. | Armarium sealed, export complete. |
| `partial` | Exported with acts held or missing; the reasons are listed. | Armarium sealed, export partial. |
| `damaged` | A stored seal no longer verifies. The tree is evidence to preserve, not to resume. | Any stage `seal-invalid`. |

`damaged` outranks every other state. `complete`, `partial`, `halted` and `damaged` are
final. `held`, `paused`, `interrupted` and `failed` wait for something.

### Transitions

| From | Command or event | To |
|---|---|---|
| `absent` | `start` | `running` |
| `running` | a stage holds and the selection stops there | `held` |
| `running` | the hard-failure cap is passed | `halted` |
| `running` | a stage refuses fatally | `failed` |
| `running` | the writer dies, the laptop sleeps, the pod is deleted | `interrupted` |
| `running` | `pause`, at the next boundary | `paused` |
| `running` | the Armarium seals | `complete` or `partial` |
| `held` | `decide`, then `resume` (from the Recensor, or the Perlector for a page re-read) | `running` |
| `held` | `advance` past the Recensor's seal, then `resume` | `running` |
| `paused`, `interrupted` | `resume` | `running` |
| `failed` | fix the cause, then `resume` under the same settings | `running` |
| any | a seal stops verifying | `damaged` |

A submission has its own short life before any run: `submitted` (ledger sealed),
`preflighted` (a preflight report exists for this ledger digest), `prepared` (every
accepted proposal applied, or none proposed). These are read from `ingest-ready.json`,
the preflight report and the triage manifest, keyed by the submission's digest.

## Events

### Where they are kept

Each run has one append-only event log of JSON lines:

- for a local run, `<state dir>/events/<run id>.jsonl`;
- for a pod run, `events/<run id>.jsonl` at the network volume's root, beside the
  `pod_run` report. `fetch-run` brings it home as an evidence key.

It is kept outside the run tree on purpose. The run tree holds only stage evidence, and
`fetch-run` refuses any object no stage accounts for.

**One writer per log**: the process that launched the orchestrator (the operator's `run`
locally, `pod_run` on the pod). It learns what happened by looking at the run tree on
each tick (every 10 seconds) and by the orchestrator's exit, not by parsing printed text.
Each line is written whole with one append and flushed. A reader that finds a torn last
line ignores it and reads it on the next pass.

The log is a record of what was seen and when. It is not authority: if it is lost, the
state is still worked out from the tree, and `watch` starts with a `resync` event giving
the current state.

### The event record

```json
{"schema": "verbatus.event.v1", "seq": 41, "at": "<UTC time>", "run_id": "demo",
 "type": "page-done", "data": {"stage": "perlector", "page": 12, "seconds": 74.2}}
```

`seq` counts up from 1 with no gaps within a log. Events carry ordinals, counts, codes,
digests and times only, never a transcription, a name read from a page, or an image.

### Event types

| Type | When |
|---|---|
| `resync` | `watch` starts; carries the current derived state. |
| `run-started`, `run-resumed` | The orchestrator was launched, with the stage range. |
| `stage-started` | The first record of a stage appears. |
| `page-done` | A stage's per-page record for one more page appears; carries the seconds since that stage's previous page. |
| `stage-sealed` | A stage's completion seal appears. |
| `hard-failure-warning` | The tally reached the warning line. |
| `estimate` | The finish estimate moved by more than 10 minutes, or every 15 minutes while running. |
| `deadline-at-risk` | The projected finish plus the margin passed the pod deadline. |
| `pause-requested`, `paused` | A pause was asked for; the boundary was reached. |
| `held`, `halted`, `failed`, `interrupted`, `complete`, `partial` | The run stopped; carries the exit and the reasons. |
| `decision-recorded`, `advance-recorded` | A person's record was written. |
| `export-written` | An export bundle exists; carries its digest and lot. |
| `pod-created`, `guard-armed`, `guard-not-armed`, `pod-released`, `pod-gone`, `close-unverified` | Pod life. |
| `extension-requested`, `extension-applied`, `extension-refused` | Deadline extension. |
| `notification-sent`, `notification-failed` | A phone notification was or was not delivered. |

Each stage's "per-page record" is named once, in one table in the progress module, with a
test that every stage in the orchestrator's sequence has an entry.

### `verbatus watch`

- **Inputs:** `--run-id`, optional `--after <seq>` to continue where a previous watch
  left off, optional `--timeout <seconds>` to return even if nothing changes.
- **Output:** one event per line (JSON lines, not the result envelope), then, as the last
  line, a result envelope with the run's state and `next_action`.
- **Exit:** the state's exit: 0 complete, 3 held, partial or paused, 4 halted, 2 failed,
  6 interrupted (the writer vanished and nobody asked it to), and 0 with state `running`
  on a timeout.
- For a pod run, `watch` reads the log from the volume over the same storage path as
  `fetch-run`, fetching only the bytes added since its last read. Reading costs nothing.

### How an agent follows a run from start to finish

1. `verbatus help --json`: learn the commands, codes and states.
2. `verbatus submit <folder> ... --json`, then `verbatus preflight ... --json`. On exit 3,
   show the flagged pages to the person and wait for their answer.
3. `verbatus start --run-id X --detach --json`.
4. Loop: `verbatus watch --run-id X --after <last seq> --timeout 600`. Report progress
   and the estimate to the person in plain words. Stop the loop when the last line's
   state is not `running`.
5. Act on the final envelope's `next_action`: on `held`, `inspect` the holds and ask the
   person what to decide; on `complete` or `partial`, `export`.

## Error and warning codes

### What each code carries

The existing `ErrorCode` table stays the one home for errors. Each entry keeps its three
sentences and gains:

- **`remedy`**: an optional structured next step: a command template with its arguments
  named, and its cost class (`free`, `local`, `paid`) and whether it is a person's
  decision (`lead`). An agent may run a `free` or `local` remedy itself when the person
  asked it to drive; it only offers a `paid` or `lead` remedy.
- **`retryable`**: whether running the same command again unchanged can succeed (a busy
  lock: yes; an unreadable policy: no).

A parallel `WarningCode` table has the same shape. A warning never changes the exit code.

### Versioning

- A code is a stable string. Its meaning never changes. A different meaning gets a new
  code.
- A code that is no longer raised moves to a `RETIRED` list with the code that replaced it,
  if any, and stays answerable by `verbatus help <code> --json`. It is never reused.
- The catalogue has a version number. Adding a code raises the minor number; retiring one
  raises the major number. `help --json` reports it, so an agent can tell when its
  knowledge is stale.
- The existing import-time check stays and is extended: every code has complete copy, every
  remedy names a real command, and every retired code names an existing replacement or
  none. A test compares the catalogue with a checked-in list of codes, so a removal that
  skips `RETIRED` fails.

### New codes

The existing codes cover submission, upload, run, export, review, decisions, backup,
fetch, status and spend. These are added. The remedies are given as commands.

| Code | Plain explanation | Remedy |
|---|---|---|
| `preflight-flagged` (exit 3) | Some pages may have lost writing at a crop edge, or are too small to read well. | Look at each listed page (`verbatus preprocess show --page <n>`); fix the crop and submit again, or accept it. |
| `preflight-unreadable` | The checks could not run on a page (it does not decode, or its image type is not handled). | Replace the page with a readable copy and run preflight again. |
| `preprocess-unavailable` | Page-geometry proposals are not built yet. | Prepare pages by hand, then `verbatus preflight`. |
| `proposal-stale` | The page or its settings changed after the proposal was shown. | `verbatus preprocess propose`, look again, then apply. |
| `run-already-running` | Another process is writing this run now. | `verbatus watch --run-id X`. |
| `pause-pending` | The run will pause at the next stage boundary. | `verbatus watch --run-id X`. |
| `nothing-to-resume` | The run is complete, partial, halted or damaged. | `verbatus status --run-id X`. |
| `record-disagrees-with-tree` (warning) | A saved record says something the run tree does not show; the tree is believed. | Keep both; run `verbatus inspect run`. |
| `quote-required` (exit 5) | A paid action was asked for without a quote. | `verbatus pod quote ...`. |
| `quote-expired` | The quote is more than five minutes old, or prices or limits changed since. | Get a new quote and show it to the lead. |
| `quote-mismatch` | The confirmation does not match the quote shown. | Get a new quote. |
| `card-not-allowed` | The card is not in the reviewed placement table, or costs more than the spend policy allows. | Choose a listed card, or ask the lead to change the policy. |
| `pod-already-live` | A pod is already running for this account. | `verbatus pod status`; stop it first. |
| `guard-commit-not-on-main` | The guard would be fetched from a commit that is not on `main`. | Use a commit on `main`. |
| `guard-not-armed` | The pod started but its guard did not report armed in time, so the pod was deleted. | `verbatus pod status`; check the guard log before trying again. |
| `extension-over-policy` | The extra time would pass the spend policy's lifetime or cost limit. | The lead decides whether to raise the policy. |
| `extension-not-applied` | The extension was approved but the pod has not confirmed the new deadline. | `verbatus pod status`; if the deadline did not move, the pod ends at the old one. |
| `close-unverified` (exit 6) | The pod delete was sent but the provider has not shown it gone, or the billing could not be read. | Check the RunPod console now. |
| `volume-unreachable` | The network volume did not answer a read. | Check the storage keys and datacenter, then try again. |
| `lot-not-found` | No ledger record for that lot on this machine. | Look on the machine or pod that ran the export. |
| `settings-invalid` | The settings file has an unknown key or a bad value. | `verbatus settings show` names the line. |
| `private-path-in-git-tree` | A ledger, state or settings path is inside a git work tree, where it could be committed. | Move it outside any checkout. |
| `api-not-local` | The API was asked to listen on an address other than this computer. | Run it on `127.0.0.1` only. |
| `withheld-from-hosted-ai` (warning) | Page text or images were left out because this provider is not allowed to see them. | The user can turn it on for that provider in settings. |
| `estimate-unknown` (warning) | Too little has run to estimate a finish time. | Wait for a few pages. |
| `thresholds-unmeasured` (warning) | The pagekit thresholds have not been calibrated; no flag is not proof. | None; look at a sample of pages. |

The existing `run-held`, `export-partial` and `canary-alarm` codes become exit 3.

## Local API, web page and MCP server

### One dispatch table

The commands are listed once, in a catalogue: name, arguments (with types), cost class,
confirmation rule, output schema and the function that does the work. The command line,
the API and the MCP server are three readers of that catalogue. `help --json` prints it.
A test fails if a command exists in one surface and not in another, or if two surfaces
give different JSON for the same call.

### `verbatus serve`: the local JSON API

- Listens on `127.0.0.1` only. Asking for any other address is refused
  (`api-not-local`). There is no option to change that.
- `POST /v1/<command>` with a JSON body of the same arguments returns the same result
  envelope. `GET /v1/help` is `help --json`. `GET /v1/runs/<id>/events?after=<seq>` streams
  events (server-sent events), ending the same way `watch` ends.
- Each start writes a fresh random token to `<state dir>/api-token` (readable by this user
  only). Every request must carry it. Requests whose `Host` or `Origin` header is not this
  computer's own address are refused, so a web page in the browser cannot reach the API
  by tricking the browser.
- Built on the Python standard library's HTTP server: one user, one machine, no
  dependency.
- A long command (a local run) is started detached and followed by events. The API never
  holds a request open for the length of a run.

### The local web page

- `verbatus serve` also serves one page: static HTML and JavaScript that call the API and
  show the envelopes. It has no logic of its own: it shows states, tables, the estimate,
  the holds and the `next_action` as a button.
- It shows page images from the local run tree through the API, so it works on a phone
  only if the lead chooses to expose it (an open question below).
- A paid action shows the quote and needs the confirmation line typed in, exactly as the
  command line does.

### `verbatus mcp`: the MCP server

- Runs over standard input and output, started by the AI client on the same computer.
- Each command in the catalogue is one tool, with an input schema made from its
  arguments and a description made from its help text. Paid actions are split into a
  quote tool (free) and a confirm tool (`pod_start_confirm`, `pod_extend_confirm`), so the
  AI client asks the person before the confirm tool runs. The setup notes tell the user
  never to allow those two tools automatically.
- It needs only the JSON-RPC requests MCP tools use (`initialize`, `tools/list`,
  `tools/call`). The session that builds it reads the current MCP specification first and
  uses the standard library unless the specification needs more.
- It applies the hosted-AI rule below to every answer before it leaves.

## Pods

### What is fixed

- **RunPod only.** There is no provider option.
- **Through `runpodctl`.** The surface calls `runpodctl` as a child process. The RunPod
  key stays in `runpodctl`'s own configuration; the surface never reads it, prints it or
  stores it. The storage keys for the volume stay in the environment, as `upload` and
  `fetch-run` use them today.
- **The guard is always on.** `pod start` builds the start command only from
  `operations/pod/pod_start_command.sh` with the approved hours and a commit on `main`.
  There is no way to pass a start command of your own.
- **One live pod at a time.** `pod start` refuses while `runpodctl pod list` shows any pod.

### `verbatus pod quote`

- **Inputs:** `--hours`, optional `--card` (default: the cheapest card in
  `config/pod_placement.toml` whose tier fits the run's model roster), the volume.
- **Output:** `verbatus.pod-quote.v1`: card id and memory, the card's hourly price from
  the placement table and the price RunPod reports now, the volume's hourly price, hours,
  the total to the deadline, the spend policy's limits, whether the quote is within
  them, the guard commit, a `quote_id` (a digest of everything shown plus a one-time
  value), its expiry (five minutes), and the confirmation line.
- **Exit:** 5 (a quote is a preview); 2 when the card or total is outside the policy
  (`card-not-allowed`).
- When the live price is above the table's price, the quote shows both and is refused;
  the table is the reviewed figure. Which `runpodctl` command or RunPod API call reads the
  live price is checked against their documentation when this is built.
- **Never:** creates anything.

### `verbatus pod start`

- **Inputs:** `--quote <quote_id>` and the confirmation line, typed back exactly (the
  command line asks for it; the API and MCP take it as an argument).
- **What it does:** checks the quote is fresh and unchanged; creates the pod with the
  guard armed, the volume at `/workspace/private` and the card and hours of the quote;
  then reads `.pod_guard/guard.log` and `deadline-<pod id>` from the volume until the
  guard reports armed for this pod. If that does not happen within ten minutes, it deletes
  the pod and answers `guard-not-armed`.
- **Output:** `verbatus.pod.v1`: pod id, card, price per hour, created time, guard
  deadline, guard heartbeat age, SSH details.
- **Exit:** 0 created and guard armed; 2 refused before anything was created; 6 created
  but its state is unknown (go and look).
- **Never:** starts without a matching quote and confirmation; starts a second pod; starts
  without the guard; retries a create on its own.

### `verbatus pod stop`

- Deletes the pod with `runpodctl`, then checks that RunPod's own state shows it gone
  (the pod lookup fails and it is absent from the list) and reads the billing records for
  that pod. Exit 0 only when both agree; otherwise exit 6 `close-unverified`, with what
  to check in the console. Stopping spends nothing, so it needs no quote, but it does need
  a yes, because it loses whatever the pod is doing.
- If the pod tooling's billing read does not survive the cleanup, `pod stop` answers
  exit 6 every time, saying the billing was not read, until that read is built again.
  It never claims a verified close it did not make.

### `verbatus pod status`

Free. The pod list, the pod's card and price, time used, cost so far (price times time;
the billing figure when RunPod returns one), the guard deadline, the guard heartbeat age,
and the run on it with its state and estimate.

### Progress and the finish estimate

The estimate is built on the pod side (in `pod_run`'s liveness loop, by the pod half of
the cleanup train) and read by the surface. The surface does not compute a second one. It
expects these facts, and adapts to the names that work settles on:

- pages in the run, from `run.json`;
- for each stage: pages done, the median seconds per page so far, and the fixed time
  before its first page (loading the model);
- for stages not yet started: the rate measured on earlier runs on the same card, kept in
  the local ledger, or no estimate for that stage;
- the projected finish time with a low and a high bound, and what the estimate rests on.

Until a stage has done three pages, its rate is unknown and the answer says so
(`estimate-unknown`). The estimate is written as an `estimate` event and shown by
`status`, `watch`, the web page and the MCP tools.

### When the run will outlast the pod

When the projected finish plus a margin (20 minutes, to bring results home and end
cleanly) passes the guard's deadline:

1. A `deadline-at-risk` event is written.
2. One `decision` notification goes to the lead: the run id, the projected finish, the
   deadline, the extra hours needed (rounded up to the next half hour) and their cost at
   the quoted rate, and the command to approve it. It is sent again only if the projected
   finish moves 15 minutes later still.
3. Nothing else changes. The guard's deadline stays where it is.

### `verbatus pod extend`

- The lead approves an extension from the command line, the web page, or by telling an AI
  driver. It is two steps, like `start`: `pod extend --hours N` gives a quote (new
  deadline, extra cost, the pod's total time and cost against the spend policy); the
  confirmation with its line makes it happen.
- **Within the spend policy only.** If the new total time passes `hard_lifetime_seconds`,
  or the total cost passes the policy's limit, it is refused with
  `extension-over-policy`, which shows the values the policy would need. Changing the
  policy is the lead's act (an open question below asks how).
- **How it reaches the pod:** the confirmed extension is written as a request file under
  `control/<run id>/` on the volume, over the same storage path `upload` uses. `pod_run`
  on the pod checks it against the policy it started with and moves the guard's deadline
  file the way the guard expects (a new file moved into place). It then writes back an
  acknowledgement and an `extension-applied` event. The surface reads the guard's
  deadline file back and answers 0 only when it shows the new deadline; otherwise
  `extension-not-applied`. SSH stays the hand fallback.
- **Never:** moves the deadline without a confirmed quote; moves it past the policy;
  extends on its own because a run is late.

### Pause on a pod

A pause on a pod stops at the next boundary, and then `pod_run` releases the pod, as it
already does at the end of a run, so an idle pod does not keep billing. Resuming needs a
new `pod start`, with a new quote and yes. (This is an open question below, because it
trades money against the time to boot again.)

## Lot numbers and settings

### Lot numbers

The lot number is built by the lot pull request, which this surface wraps and does not
change:

- `lot_<16 hex>`, worked out from the run's `run.json` digest; the same run always gets
  the same lot. It is on by default and written into every export row.
- The ledger record behind it (run id, `run.json` digest, commit, settings used, run root,
  exports) is kept only in the operator's state directory on the lead's machine, or on
  the pod's volume.

The surface adds:

- `verbatus lot <lot> --json`: the ledger record, marked `"local_only": true`;
- `lot` in `export`, `status` and the `export-written` event (the lot itself is only a
  digest).

Rules the surface keeps: ledger details are never put in an event, a notification, the
web page's address, or anything bound for GitHub. The MCP tool description for `lot`
says the answer must not be pasted into an issue, a pull request or a chat with a hosted
service. Tests and CI use synthetic lots only.

### Settings

Private settings live at `$XDG_CONFIG_HOME/verbatus/settings.toml` (or
`~/.config/verbatus/settings.toml`), outside the checkout, one file per operating-system
account. The schema is closed: an unknown key is refused (`settings-invalid`).

- **What it may hold:** defaults for existing options (configuration trio, formats, run
  root, state directory), whether to notify, the API port, the hosted-AI permissions per
  provider, and which provider the AI driver uses.
- **What it may not hold:** anything that spends (the spend policy stays the reviewed
  `config/spend.toml`), anything that changes how pages are read, and no user id.
- **Order:** a command-line flag beats the settings file, which beats the repository
  default.
- `verbatus settings show --json` prints every effective value and where it came from.
  Every run records which settings file it used, and its digest.
- A settings, state or ledger path inside any git work tree is refused
  (`private-path-in-git-tree`), with a test. The commit hook refuses files shaped like a
  ledger or settings file.

## Driving it with an AI

### What an agent with no context needs

- `verbatus help --json`: commands, arguments, costs, confirmations, exit codes, states,
  events, codes and remedies, and the catalogue version. Nothing else needs reading
  first.
- Every result has `next_action`. An agent that only ever follows `next_action`, asks the
  person whenever its cost is `paid` or its decision is the lead's, and reports what each
  result says, drives a whole run correctly.
- Every error has a remedy marked with its cost, so the agent knows which it may run and
  which it may only offer.

### Safety rails

- **No paid action without the lead's yes.** Paid actions need a quote and a matching
  confirmation, and the MCP confirm tools are separate so the AI client asks the person.
  The surface cannot prove a person said yes; what it can do is make the yes a separate,
  visible step bound to a price the person saw, and keep every paid action inside the
  spend policy and under the guard whatever it is told.
- **No automatic spending.** Nothing extends, restarts or retries a paid action by itself.
- **Register material is not sent to hosted services.** Every MCP answer and every API
  answer marked for an AI driver passes through one filter. The settings name the
  driver's provider (for example `anthropic`, `openai`, or `local` for a model on this
  computer or the pod). For a hosted provider, unless the user has turned it on for that
  provider:
  - transcriptions, readings and witness text are replaced by
    `{"withheld": "hosted-ai-not-allowed"}`, with their length and digest kept;
  - images and image paths are left out;
  - folder paths outside the checkout are shown as short handles (`run:demo`,
    `submission:1`), which the commands accept in place of the path;
  - the answer carries the warning `withheld-from-hosted-ai`.

  Counts, states, codes, ordinals, digests, prices and estimates always pass, so the
  agent can drive a run without seeing what is on the pages. The permissions are per
  provider and per kind (`images`, `text`), off by default, and turning one on shows a
  warning that the material will leave this computer.
- **No secret passes through the surface.** The RunPod key, the storage keys and the
  notification topic are never in a result, an event or a log.

## Build order

Each phase leaves something usable on its own.

### Phase 0: already planned in the cleanup train

The pod half of the cleanup train builds the finish estimate and the extension request on
the pod side, so the first live run has them. The surface reads them; it does not wait
for anything below.

### Phase 1: the first slice, for the first live run

Small and read-mostly:

1. The result envelope, `--json` and the unified exit codes on `status`, `run`, `export`,
   `fetch-run` and `review`.
2. `remedy` and `retryable` on every existing error code, the `WarningCode` table, and
   `verbatus help --json`.
3. Run state worked out from the tree (the table above), shown by `status --run-id`.
4. `verbatus watch` for a pod run: it reads `pod_run`'s report, liveness and estimate
   from the volume over the storage path, so the lead (or an AI on the lead's laptop)
   can follow the first live run and see its finish estimate without SSH.
5. `verbatus pod extend` with quote and confirmation, over whatever request route the
   pod half builds.

This is enough for the first live run: the lead starts the pod by the hand route, and
follows and extends it from the laptop.

### Phase 2: events, inspect, the API and MCP

- The event log, written by the operator's `run` and by `pod_run`, and `watch` on it.
- `pause` and `resume`, with the orchestrator checking for a pause request at each
  boundary.
- `inspect page` and `inspect act`, one image at a time.
- The catalogue as the single dispatch table; `verbatus serve`; `verbatus mcp` with the
  hosted-AI filter.
- `lot` and `settings show`, once the lot pull request has merged.

### Phase 3: before the pod

- `submit` (today's `ingest`, renamed), `preflight` with the pagekit crop check, and
  `preflight --for-pod`.
- `preprocess` once pagekit's proposals exist.

### Phase 4: pod start and stop from the surface

- `pod quote`, `pod start` (with the guard check), `pod stop` (with the verified close),
  `pod status`.
- If the lead agrees (open question), a small run-on-start entry on the pod that reads a
  run request from the volume, so `start --pod` no longer needs SSH.

### Phase 5: the web page

The static page over the API: runs, states, estimate, holds, inspect views, decisions,
and the paid quote and confirmation.

### Deliberately left out

- Any provider other than RunPod, and any second live pod.
- Reaching the API or page from another machine, and any login other than the local token.
- More than one user, and any user id.
- Stopping a stage half-way, and a pause that keeps a pod billing.
- Automatic extension, restart or retry of anything paid.
- Editing readings in the web page beyond what `decide edit` already records.
- Native Windows (WSL is supported like Linux).
- A hosted dashboard, and a published MCP server listing.

## Questions for the lead

Each needs the lead's answer because it is about money, privacy or what the project is
for. Each has a recommendation.

1. **May an AI driver pass on the lead's yes for paid actions?** The pod ruling covers
   extensions told to an AI driver. Should the same hold for starting a pod?
   *Recommendation:* yes, for both, but only through the separate confirm tools bound to
   a quote the lead was shown, and the MCP setup tells the user never to allow those tools
   automatically.
2. **Hosted AI seeing transcriptions.** The ruling allows page images to go to a hosted
   model when the user turns it on per provider. Should a hosted AI driver see the
   transcriptions too? *Recommendation:* treat text exactly like images: a separate
   per-provider permission, off by default, with the same warning. Until it is on, the
   driver sees counts, states and codes only.
3. **Going over the spend policy for one pod.** When an extension would pass the policy's
   lifetime or cost limit, should the lead be able to approve it on the spot?
   *Recommendation:* no. The lead changes `config/spend.toml` (a deliberate, reviewed
   act); the refusal shows the exact values needed so that takes a minute.
4. **Pause on a pod.** Should pausing a pod run release the pod (no idle billing, but a
   new start and a new model load to resume), or keep it until the guard's idle limit?
   *Recommendation:* release it.
5. **Starting the run without SSH.** Starting a pod run from the surface needs a small
   entry on the pod that reads a run request from the volume and starts `pod_run`. That
   brings back a small part of the retired managed route. *Recommendation:* build it in
   phase 4, but only after the first hand-route live run has shown the guard working.
6. **The web page on a phone.** It listens only on this computer. Should it ever be
   reachable from the lead's phone? *Recommendation:* not in this version. Phone
   notifications carry the decisions, and the approval commands work from the laptop.
7. **Publishing the MCP server.** Should it be listed for others to install at beta?
   *Recommendation:* no, not until beta is public and the hosted-AI filter has been
   reviewed by a fresh reader.
