# Control surface: design

This is the design for one way to drive Apparatus Verbatus, whether a person at a terminal
or an AI model with no prior context is doing the driving. Only part of it is built so far
(see "Build order"). A later session builds the rest in the order given in "Build order", and changes this document
when something it builds turns out differently.

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
  creation, `pod_run` on the pod, and `fetch-run` to bring results home. `pod_run` writes
  a report on the volume with three side files beside it: liveness, timings (each stage's
  clock) and a transcript.

**Phase 0, in part.** `pod_run` keeps the current stage's finish estimate in
`pod-run-report-<run id>-estimate.json` beside its report and sends the deadline-at-risk
notice (`operations/pod/finish_estimate.py`); the spend policy carries the soft and hard
maximums. The deadline-extension request route does not exist yet.

## What it is for

- **One source of truth.** Every action is one `verbatus` command. Anything built later on
  top (an API, an MCP server for AI models) calls those same commands and returns the same
  JSON, with no logic of its own.
- **Readable by a machine and by a person.** With `--json` a command prints exactly one
  JSON object. Without it, it prints the same facts in plain English.
- **Safe to hand to an AI.** An agent that has never seen the project can follow a run from
  start to finish, and can never spend money without the lead's yes.
- **Local only.** Nothing here is published. The MCP server and every specific (endpoints,
  keys, provider details) stay on the lead's machine and never enter the public
  repository; this document describes them only in general terms.

## Principles the surface keeps

1. **The run tree is the truth.** A run's state is worked out from its run tree each time
   it is asked for. The surface keeps no state file that could disagree with the evidence.
2. **Reading is free and always allowed.** Anything that only reads never asks for
   confirmation and never contacts a paid service except to read.
3. **A paid action needs a quote, a yes and a code.** Starting a pod and extending it past
   its soft maximum are the only paid actions. Each is a quote showing the card, the
   hourly price and the budget, then a confirmation bound to that quote and to a short
   code sent to the lead's phone (see "Quote and confirm"). Nothing goes past a pod's hard
   maximum without the lead's new permission (see "Budgets: soft and hard maximum").
4. **The guard is always on.** No path creates a pod without the pod guard armed in its
   start command, and the surface checks that the guard really armed.
5. **Register material stays private.** What an AI driver may see is a fixed list of
   fields; page images, transcriptions and anything else not on the list stay on this
   computer. Driving a run needs only states and counts.
6. **Every refusal says what to do.** Every non-success result carries a stable code and
   the three plain sentences.

## How every command answers

With `--json`, a command prints one object to standard output and nothing else. Progress
lines and warnings for a person go to standard error. The one exception is `watch
--json`, which prints JSON Lines: one progress object per change, and the result
envelope below as its last line.

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
- `error` is null on success, or an object with `code`, `what_happened`, `what_it_means`,
  `next_step` and `detail`.
- `next_action` is the one supported next step, or null. It names its cost: `free`,
  `local` (uses this computer only) or `paid`. A paid next action is always a quote, never
  the confirmation.

### Exit codes, the same for every command

| Exit | Meaning |
|---|---|
| 0 | Done, as asked. |
| 2 | Refused or failed. Nothing was claimed done; the error code says why. |
| 3 | Needs a person: a held or paused run, a partial export, pages flagged for review. |
| 4 | Halted: the run hit the hard-failure cap and stays stopped. |
| 5 | Waiting for a confirmation: a quote or preview was shown and nothing was done. |
| 6 | Unverified: something may have happened that could not be confirmed. Go and look. |

0, 2, 3 and 4 mean what they already mean for the orchestrator. Exit 6 is the "never read
as nothing happened" rule the pod tools already keep, for example when a pod delete was
sent but its absence could not be confirmed. Today the operator returns 2 for every
failure, including a held run and a partial export; those move to 3 when this is built.

These are the surface's codes. `pod_run` keeps its own (`operations/pod/run_exits.py`,
where 6 means failed), and the surface translates them.

**A pause exit.** The orchestrator and `pod_run` gain one new code, 9 (paused), unused in
both today. A run that stopped at a boundary because someone asked must never exit 0, or
it would read as complete.

## The commands

Every command takes `--json`, `--state-dir` and `--workspace` as today. "Never" lists what
the command will not do, whatever it is asked.

### `verbatus status`

- **Inputs:** none for everything, or `--run-id` for one run (and `--run-root` to pick one
  when the id is ambiguous).
- **Output:** `verbatus.status.v1`: each run's state (see "Run states"), its stage table,
  pages done per stage, holds, and the `review` line; for a pod run also the liveness age,
  the per-stage timings and, once phase 0 exists, the estimate; every live pod with card,
  price per hour, time used and guard deadline.
- Works on a local run tree and on a pod run's tree on the volume (read over S3, as
  `fetch-run` reads it).
- **Never:** starts, changes or spends anything.

### `verbatus watch`

Follows a pod run from the laptop without SSH.

- **Inputs:** `--run-id`, optional `--interval <seconds>` (default 30) and
  `--timeout <seconds>` to return even if nothing changes.
- **How it reads:** on each pass it fetches `pod_run`'s report, liveness, timings and
  estimate files from the volume over S3, each as a whole object (they are small and rewritten in place,
  so a partial read would be meaningless). It prints a line when something changed.
- **What it shows:** the stage running, pages done in it, how long each finished stage
  took, the liveness age, and the run's state. Once phase 0 exists it also shows the
  estimate. The estimate is for the current stage only and is labelled "this stage
  finishes about …", never as the run's finish.
- **Output:** with `--json`, JSON Lines (the exception named under "How every command
  answers"): one `verbatus.watch.v1` object per change, then the result envelope as the
  last line with the run's state and `next_action`. A client reads lines until one has
  `"schema": "verbatus.result.v1"`.
- **Exit:** the state's exit: 0 complete, 3 held, partial or paused, 4 halted, 2 failed or
  never-started, 6 interrupted (the writer vanished and nobody asked it to), and 0 with
  state `running` on a timeout.
- **Never:** writes to the volume. Reading costs nothing beyond the storage reads.

Later, `watch` also follows a local run and reads the event log (see "Events").

### `verbatus start`, `pause` and `resume`

- **`start`:** today's `verbatus run`, with the run lock and event log described below,
  `--detach` to return at once, and `--pod` to send the run to a live pod. Until a
  run-on-start entry exists on the pod (phase 4), `--pod` answers with the hand-route
  commands to run over SSH, filled in for this run. Never rents a pod, resumes under other
  settings, or starts a second writer on a run that has one.
- **`pause --run-id`:** a soft pause. The run stops at the next stage boundary so the lead
  can look at what happened before going on; the orchestrator checks for a pause request
  there and exits 9. On a pod, the pod stays up (see "Pause and stop on a pod").
- **`pause --hard --run-id`:** a hard stop. The run stops at the next boundary in the same
  way, and nothing is left running or planned; on a pod, the idle limit then deletes the
  pod if no one acts. On a soft-paused run it turns the soft pause into a hard stop.
  Neither kind of pause stops a stage half-way.
- **`resume --run-id`:** `start --from <stage>` with the first unsealed stage worked out
  for you, under the run's recorded settings. Never clears a hold (that is `decide` or
  `advance`). For a soft-paused pod run it writes a resume request to `control/<run id>/`
  on the volume, and the `pod_run` waiting there carries on, so there is still one
  writer. Otherwise it never trusts the liveness age alone, since that compares two
  clocks: it starts a new writer on a live pod only when `pod_run`'s report records that
  the last one exited, and refuses with `run-already-running` while it does not.

### `verbatus inspect`

The `review` projection one piece at a time, so it works on a run of any size:
`inspect run`, `inspect page --page <n>` and `inspect act --act <key>`, with the same
fields `review --json` has. Images are read and digested one at a time, which removes
today's 256 MiB limit on `review`. Never writes anything.

### `verbatus decide`, `advance` and `export`

As today, with `--json`. `decide` and `advance` print the binding they will record and
the line to type back, and exit 5; the record is written only when that line comes back
(`--confirm "<line>"`). They cost nothing but are the lead's decisions, and say so.
`export` exits 0 complete, 3 partial, 2 refused, and never hides that an export is
partial.

### `verbatus submit`, `preflight` and `preprocess`

- **`submit`** is today's `ingest`, renamed because "stage" already means a pipeline
  stage. `--preview` shows what would be written (exit 5).
- **`preflight`** checks a submission locally before any pod is rented: every file
  decodes, size and resolution, the pagekit crop check, the data gate. `--for-pod` adds
  the configuration pair, the spend policy, the storage keys (a read of the volume) and
  the guard's commit being on `main`. Exit 3 when pages are flagged. While the pagekit
  thresholds are uncalibrated, "no flags" is reported as "nothing found", never as "the
  crops are right".
- **`preprocess`** offers pagekit's page-geometry proposals and applies only the ones the
  user accepts, as versioned copies. Until pagekit's second slice exists it answers
  `preprocess-unavailable`.

None of them changes or moves an original page, or sends an image anywhere.

### Words that stay as they are

`upload`, `fetch-run`, `backup`, `spend show`, `clear-leftovers`, `triage` and `review`
keep their behaviour and gain `--json`.

## Run states

### How the surface finds a run

A run id alone does not say where a run's tree is. A **receipt** does: locally, the
operator's run record in its state directory; for a pod run, `pod_run`'s report on the
volume. Each names the run root.

1. The surface looks up every receipt for the run id.
2. If they name more than one run root, it refuses and lists the candidates, as `export`
   already does with `export-ambiguous`; `--run-root` picks one. It never guesses.
3. It opens the one tree. From there the tree decides the state; the receipt adds only
   what the tree cannot hold: how the last invocation ended (its exit code) and whether it
   ever started.

### What decides the state

In this order of authority:

1. **The run tree:** `run.json`; each stage's seal (sealed, written but unsealed, not
   run, or seal no longer valid, the four states `review` already reads); the Recensor's
   holds and any advance record that passes its seal; the hard-failure tally; the
   Armarium's export record.
2. **The receipt:** the last invocation's exit code, and `pod_run`'s report for a pod run.
3. **Liveness:** whether a writer is active now. Locally, an operating-system lock on
   `<state dir>/locks/<run id>.lock`, held by the process running the orchestrator and
   released when it dies. On a pod, `pod_run`'s liveness file seen within the last two
   minutes, on a pod that still exists. That freshness check compares the time the pod
   wrote into the file (the pod's clock) with the laptop's clock, so a skewed clock on
   either side shifts it; the answer gives the age as "by this computer's clock".
4. **A pause request**, if one is waiting.

When the tree and a receipt disagree, the tree wins and the answer carries the warning
`record-disagrees-with-tree`. A receipt that says complete over an Armarium with no seal
reads as `interrupted`, with the warning.

### The states

| State | What it means | Read from |
|---|---|---|
| `absent` | No run by this id. | No receipt, no `run.json`. |
| `never-started` | A pod run was launched and billed, but the orchestrator never wrote `run.json` (the bootstrap failed, or the pod died first). | A pod receipt or `pod_run` report for the id, no `run.json`. |
| `running` | A writer is active. | Liveness, and some stage not yet sealed. |
| `paused` | Stopped at a boundary because someone asked; the answer says whether soft or hard. | The orchestrator's last exit 9 and the next stage not run. In a soft pause on a pod, `pod_run` stays alive, waiting. |
| `interrupted` | Stopped mid-way with no writer: a crash, a closed laptop, a pod that ran out of time. | A stage unsealed, or the next stage not run, with no writer and no pause or hold to explain it. |
| `held` | Waiting for a person's decision. | Holds with no advance passing their seal; last exit 3. |
| `halted` | The hard-failure cap was passed; the run stays stopped. | Tally over the cap; last exit 4. |
| `failed` | The last invocation failed and named its cause. | Last exit 2, no writer. |
| `complete` | Exported in full. | Armarium sealed, export complete. |
| `partial` | Exported with acts held or missing; the reasons are listed. | Armarium sealed, export partial. |
| `damaged` | A stored seal no longer verifies. The tree is evidence to preserve, not to resume. | Any stage `seal-invalid`. |

`damaged` outranks every other state. `complete`, `partial`, `halted` and `damaged` are
final. `held`, `paused`, `interrupted`, `failed` and `never-started` wait for something; a
never-started run is started again with `start`, since it has nothing to resume.

### Transitions

| From | Command or event | To |
|---|---|---|
| `absent` | `start` | `running`, or `never-started` if a pod launch never wrote `run.json` |
| `running` | a stage holds and the selection stops there | `held` |
| `running` | the hard-failure cap is passed | `halted` |
| `running` | a stage refuses fatally | `failed` |
| `running` | the writer dies, the laptop sleeps, the pod is deleted | `interrupted` |
| `running` | `pause` or `pause --hard`, at the next boundary | `paused` |
| `running` | the Armarium seals | `complete` or `partial` |
| `held` | `decide` or `advance`, then `resume` | `running` |
| `paused`, `interrupted` | `resume` | `running` |
| `failed` | fix the cause, then `resume` under the same settings | `running` |
| `never-started` | fix the cause, then `start` | `running` |
| any | a seal stops verifying | `damaged` |

## Events

Each run has one append-only log of JSON lines, beside the receipt and outside the run
tree (`fetch-run` refuses any object no stage accounts for): `<state dir>/events/<run
id>.jsonl` locally, `events/<run id>.jsonl` at the volume's root for a pod run.

**One writer: the launcher**, the process that started the orchestrator (the operator's
`run` locally, `pod_run` on the pod). It writes only what it can see for itself: the run
tree on each tick, the orchestrator's exit, a pause request at a boundary, and (once
built) the extension handler it runs. Every other command, such as `decide`, `advance`,
`export` or `pod start`, writes a receipt of its own, not an event. Each line is written
whole with one append; a reader ignores a torn last line and reads it next time. A
launcher that starts on an existing log first checks that the log ends with a newline;
if it does not, the launcher cuts the torn fragment off before appending, and takes the
next `seq` from the last whole line, so a crash in mid-write leaves no joined lines and
no gap.

The log is a record, not authority. If it is lost, the state still comes from the tree.

```json
{"schema": "verbatus.event.v1", "seq": 41, "at": "<UTC time>", "run_id": "demo",
 "type": "page-done", "data": {"stage": "perlector", "page": 12, "seconds": 74.2}}
```

`seq` counts up from 1 with no gaps. Events carry ordinals, counts, codes, digests and
times only.

| Type | When |
|---|---|
| `run-started`, `run-resumed` | The orchestrator was launched, with the stage range. |
| `stage-started`, `page-done`, `stage-sealed` | A stage's first record, one more page's record, its seal. |
| `hard-failure-warning` | The tally reached the warning line. |
| `pause-seen`, `paused` | The launcher found a pause request; the boundary was reached. Both say soft or hard. |
| `held`, `halted`, `failed`, `interrupted`, `complete`, `partial` | The run stopped; carries the exit. |
| `estimate`, `deadline-at-risk` | Pod side, after phase 0. `deadline-at-risk` names the soft and hard maximum. |
| `extension-applied`, `extension-refused` | Pod side, once the extension handler exists. |

Each stage's per-page record is named once, in one table, with a test that every stage in
the orchestrator's sequence has an entry.

## Pods

### What is fixed

- **RunPod only, through `runpodctl`.** The surface calls `runpodctl` as a child process.
  The RunPod key stays in `runpodctl`'s own configuration; the surface never reads,
  prints or stores it. The storage keys stay in the environment, as `upload` and
  `fetch-run` use them today. Both are the lead's own credentials, kept on the lead's
  machine or in the provider's secret store, never in the repository or anything
  published.
- **The guard is always on.** `pod start` builds the start command only from
  `operations/pod/pod_start_command.sh` with the approved hours, the idle limit and a
  commit on `main`. There is no way to pass a start command of your own.
- **One live pod at a time.** `pod start` refuses while `runpodctl pod list` shows any pod.

### Budgets: soft and hard maximum

> **Superseded 2026-10-07.** The lead set no automatic spend limits: the budget below is
> off by default (`pod_budget = "off"` in `config/spend.toml`), so a pod gets no deadline
> unless one is given in hours at start (`operations/pod/pod_start_command.sh`). With
> `pod_budget = "on"` the soft and hard maximums in that file apply as described here.

Every pod has a budget with two limits, each in both time and cost:

- **Up to the soft maximum** the run simply proceeds. The guard's deadline is set at the
  soft maximum's time.
- **From the soft to the hard maximum** needs an extension (`pod extend`), confirmed with
  the code that reaches the lead's phone. An AI driver may prepare and request it but
  cannot complete it without that code.
- **Past the hard maximum** always needs the lead's new, explicit permission, which sets
  a new hard maximum. No code, no AI driver and no shortcut on the spot can do it:
  `pod extend` refuses with `past-hard-max`, whoever asks. The lead sets a new hard
  maximum with `pod budget`, which asks for the values at an interactive terminal along
  with a fresh code, refuses `--json`, and is not offered through the API or the MCP
  server. The interactive check catches accidents; the lead's own words are the
  permission.

Cost is the quoted price times time, so either limit can be shown as the other; whichever
is reached first counts. The spend policy (`config/spend.toml`) sets the outer default
soft and hard values, and the idle limit below; a pod's own budget is fixed at its quote.

### Quote and confirm

Both paid actions, `pod start` and `pod extend`, work the same way.

1. **The quote** (`pod quote`, or `pod extend --hours N`) is free and exits 5. It shows
   the card, the price per hour from `config/pod_placement.toml` and the live price, and
   the budget. A live price above the table's is refused. For a start, the quote asks
   whether the budget and timeline are firm or flexible (`--firm`, or `--flexible` with
   soft and hard values; the spend policy's defaults fill in what is not given). A firm
   budget has its soft and hard maximum equal. An extension's quote shows the new
   deadline against the hard maximum and is refused at once if it would pass it. Each
   quote gets a `quote_id` and is saved as `<state dir>/quotes/<quote_id>.json`, readable
   by this user only (0600); it expires after five minutes.
2. **The code.** With the quote, the surface sends a short random code to the lead's
   phone through `operations/notify/notify.sh decision`, with the budget it approves. The
   code never appears in any output: not on the screen, not in JSON, not in a log or
   event, and the quote file holds only its digest. An AI driver therefore cannot
   complete a confirmation from its own context; the lead has to read the code off the
   phone and pass it on.
3. **The confirm** takes the `quote_id` and the code. It runs under the same paid-launch
   lock whether it comes from the command line, the API or MCP (the existing exclusive
   spend lock in `operations/pod/launch.py`), so two confirms can never race. The first
   attempt uses the quote up, whether it succeeds or not; a wrong code means a new quote.

### `verbatus pod start`

After a confirm: creates the pod with the guard armed (deadline at the soft maximum, idle
limit from the spend policy), the volume at `/workspace/private` and the quoted card;
then reads `.pod_guard/guard.log` and `deadline-<pod id>` from the volume until the guard
reports armed for this pod. If that does not happen within ten minutes, it deletes the
pod and checks that it is gone the same way `pod stop` does (`runpodctl pod get` fails
and `runpodctl pod list` does not show it). Only when both checks pass does it answer
`guard-not-armed`; if either cannot confirm the delete, it exits 6 (state unknown) and
says to check the RunPod console. It
writes a receipt naming the pod and its budget.

- **Exit:** 0 created and guard armed; 2 refused before anything was created; 6 created
  but its state is unknown.
- **Never:** starts without a used-up quote and the code; starts a second pod; starts
  without the guard; retries a create on its own.

**Starting the run without SSH (phase 4).** A small entry on the pod reads a run request
from the volume and starts `pod_run`, so the lead never has to log in. The request is
written by the surface, from the command line, the local interface or an AI driver
through them, using the lead's own credentials on the lead's machine. It is built only
after the first hand-route live run has shown the guard working; until then, `start
--pod` prints the hand-route commands.

### `verbatus pod stop`

Deletes the pod with `runpodctl pod delete`, then checks twice that it is gone (`runpodctl
pod get` fails and `runpodctl pod list` does not show it). Even then it exits 6
`close-unverified` with "check billing in the RunPod console", because `runpodctl` gives
no billing record and a shutdown is verified, never assumed. It needs a yes, because it
loses whatever the pod is doing, but no code, because it spends nothing.

### `verbatus pod status`

Free. The pod list, the pod's card and price, time used, cost so far (price times time),
the soft and hard maximum, the guard deadline, the guard heartbeat age, and the run on it
with its state.

### When the run will outlast the soft maximum (needs phase 0)

When the projected finish plus a margin (20 minutes, to bring results home) passes the
guard's deadline, the pod side writes a `deadline-at-risk` event and sends one `decision`
notification: the run, the projected finish, the deadline, the extra hours needed, their
cost, and whether they fit under the hard maximum. Answers about the run carry the
warning `past-soft-max`. Nothing else changes; the deadline stays where it is.

### `verbatus pod extend`

Real enforcement needs two things that do not exist yet:

- **`pod_run` gets a budget input** (today it takes no spend input): the soft and hard
  maximum from the confirmed quote, with the spend policy's digest, recorded in its
  report.
- **A pod-side handler** in `pod_run` reads each confirmed extension request from
  `control/<run id>/` on the volume, checks it against the hard maximum the pod started
  with (time and cost), and only then moves the guard's deadline file the way the guard
  expects (a new file moved into place). It writes back an acknowledgement and an
  `extension-applied` or `extension-refused` event. The surface answers 0 only when the
  guard's deadline file shows the new deadline; otherwise `extension-not-applied`.

The laptop also checks the request against the hard maximum before sending it
(`past-hard-max`), but the pod's check is the one that counts.

**Until both exist, extension is the manual route:** over SSH, the lead writes the new
epoch second to a temporary file and moves it over `.pod_guard/deadline-<pod id>`, as
`pod_guard.sh` describes. That is the lead's own act: no agent or AI driver ever writes
that file. Nothing checks the edit against the budget, so the edit itself is the lead's
permission, and a deadline past the hard maximum is the lead's new hard maximum. The guard
ignores a deadline more than a week away, but that only catches a typo; it is not a
limit.

### Pause and stop on a pod

> **Superseded 2026-10-07.** The guard no longer deletes an idle pod at a fixed limit. It
> climbs an idle ladder (`operations/pod/pod_guard.sh`): a warning at 15 minutes, urgent
> notices from 30 minutes, a verified backup of the run tree at 1 hour, and deletion at 2
> hours only when `ladder_delete = "on"` in `config/spend.toml` (off by default).

The pod guard (`operations/pod/pod_guard.sh`) already deletes a pod that has done no
work for its idle limit: no GPU, container CPU or network use at any one-minute sample,
and no touch of the pod's keep-alive file. The limit is a guard argument;
`pod_start_command.sh` passes a fixed 30 minutes today. The guard watches the machine; it
cannot tell a pod the lead is looking at from one nobody needs. The two kinds of pause
tell it:

- **Soft pause:** the orchestrator exits 9 at the boundary and `pod_run` stays up,
  waiting for a resume request in `control/<run id>/`. While it waits it touches the
  keep-alive file, so the idle rule does not delete a pod the lead is inspecting. The
  guard's deadline still applies, so a soft pause never outlasts the soft maximum without
  an extension. It sends one `decision` notification saying the run is paused and why.
- **Hard stop:** nothing runs and nothing is planned. `pod_run` exits and stops touching
  the keep-alive file, so the guard's idle rule deletes the pod once the idle limit
  passes with no action. A command that plans work on the pod (a resume or a new run)
  touches the keep-alive file and restarts that clock.

So the hard-stop timer is the guard's existing idle rule, not a second timer. Its length
becomes a spend-policy value (`idle_shutdown_minutes`, 30 by default) that `pod start`
passes to `pod_start_command.sh` in place of the fixed 30. A paused local run has no pod,
so the two kinds of pause differ only on a pod.

## Driving it with an AI

- Every result has `next_action` with its cost. An agent that follows `next_action`, asks
  the person whenever the cost is `paid` or the decision is the lead's, and reports what
  each result says, drives a whole run correctly.
- **No paid action without the lead.** The code on the lead's phone is what makes the
  yes the lead's: the agent never sees it unless the lead gives it. An agent may quote a
  start or an extension up to the hard maximum, but never complete one without the code,
  and never reach past the hard maximum at all. The surface keeps every paid action
  inside the pod's budget and under the guard whatever it is told.
- **No automatic spending.** Nothing extends, restarts or retries a paid action by itself.
- **No secret passes through the surface.** The RunPod key, the storage keys and the
  notification topic are never in a result, an event or a log.

### What a hosted AI may see

Every answer bound for an AI driver passes through one filter. The driver's provider is
`hosted` by default; only a provider marked `local` (a model on this computer or the pod)
sees answers unfiltered.

The filter is an **allowlist of fields**, not a list of things to remove. A field passes
only if it is named on the list; everything else is dropped, including fields added
later that nobody thought to classify. The list holds: schema and command names, `ok` and
`exit`, run ids and states, stage names, counts and page ordinals, codes and their fixed
three sentences, digests, prices, times, estimates, and `next_action` commands. Left out
in particular: `detail`, hold reasons, stderr tails, filenames and paths, transcriptions,
readings, witness text and images. An answer that lost fields carries the warning
`withheld-from-hosted-ai`. Paths are replaced by short handles (`run:demo`) that the
commands accept.

In this version a hosted AI never sees transcriptions or images; driving a run needs
only states and counts. An AI that needed an image would have to download it into its
own session, which is costly and would be a separate permission, not yet given.

Checking that pages were processed correctly is the operator's job, done locally: a
before-and-after view of each page, planned with pagekit's before/after view. It is a
feature for a person on this computer, not for an AI driver.

## Error codes

The existing `ErrorCode` table stays the one home for errors, each with its three
sentences. The existing `run-held`, `export-partial` and `canary-alarm` become exit 3, and
`export-ambiguous` is used by every command that takes a run id. These are added:

| Code | Plain explanation |
|---|---|
| `run-already-running` | Another process is writing this run now. |
| `pause-pending` | The run will pause (soft) or stop (hard) at the next stage boundary. |
| `nothing-to-resume` | The run is complete, partial, halted or damaged. |
| `run-never-started` | The pod billed but the run never wrote `run.json`; start it again. |
| `record-disagrees-with-tree` (warning) | A receipt says something the tree does not show; the tree is believed. |
| `liveness-stale` (warning) | The pod's liveness file is older than two minutes by this computer's clock. |
| `estimate-unavailable` (warning) | No estimate yet: phase 0 is not built, or too few pages have run. |
| `quote-required` (exit 5) | A paid action was asked for without a quote. |
| `quote-expired` | The quote is more than five minutes old, used up, or prices or budget changed. |
| `confirm-code-wrong` | The code does not match the one sent to the lead's phone; the quote is used up. |
| `card-not-allowed` | The card is not in the placement table, or costs more per hour than the spend policy allows. |
| `pod-already-live` | A pod is already running for this account. |
| `guard-not-armed` | The guard did not report armed in time, so the pod was deleted. |
| `past-soft-max` (warning) | The run is projected to pass its soft maximum; going on needs an extension confirmed with the lead's code. |
| `past-hard-max` | The request would pass the pod's hard maximum in time or cost; only the lead's new permission (`pod budget`) can raise it. |
| `budget-needs-lead` | `pod budget` was called non-interactively or with `--json`; only the lead sets a new hard maximum, at a terminal. |
| `extension-not-applied` | The pod has not confirmed the new deadline; it ends at the old one. |
| `extension-manual-only` | The pod-side extension handler is not built; extend over SSH. |
| `close-unverified` (exit 6) | The pod looks gone, but billing must be checked in the RunPod console. |
| `volume-unreachable` | The network volume did not answer a read. |
| `withheld-from-hosted-ai` (warning) | Fields were left out because the AI driver is hosted. |
| `preflight-flagged` (exit 3), `preflight-unreadable`, `preprocess-unavailable`, `proposal-stale` | Before the pod; see `preflight` and `preprocess`. |

## Build order

Each phase leaves something usable on its own.

### Phase 0: the pod-side estimate and extension request

Built: the per-stage finish estimate in `pod_run`'s liveness loop, written to
`-estimate.json`, and the deadline-at-risk check and notification, with the soft and hard
maximums in `config/spend.toml`. Not built: the extension request route. The notice is
written to the estimate file and the run report, not yet to an event log (phase 2), and
`pod_run` takes the hourly price as `--hourly-usd` until it gets a budget input.

### Phase 1: what the first live run needs

1. `status --run-id`, with the state worked out from the tree, for a local tree and a pod
   run's tree on the volume.
2. `watch` for a pod run: polls the report, liveness and timings files over S3 with
   whole-object reads. Its estimate line depends on phase 0; until then it shows stage
   timings and says no estimate exists.

   Built so far: `watch` over copies of those four files already on this computer, with
   the estimate, deadline, soft and hard maximums, spend (from a lease when given) and the
   last notice, once or every `--interval` seconds; stale copies are said to be stale.
   Not yet built: the S3 reads, `--json` and the state exit codes.
3. `--json`, the result envelope and the exit codes on both.

The lead starts the pod by the hand route, follows it with `watch`, and extends it, if
needed, by the manual route.

### Phase 2: the run itself

- The event log, written by the launcher, and `watch` on it, for local runs too.
- `pause` (soft) and `pause --hard`, and `resume`, with exit 9 in the orchestrator and
  `pod_run`; the soft pause's wait and keep-alive in `pod_run`; `idle_shutdown_minutes`
  in the spend policy, passed through `pod_start_command.sh` to the guard.
- `inspect`, `help --json`, and `--json` on the remaining commands.
- `pod extend`, once `pod_run` takes a budget input and runs the handler (and phase 0's
  request route exists).

### Phase 3: before the pod

`submit`, `preflight` (with `--for-pod`) and, once pagekit's proposals exist,
`preprocess`.

### Phase 4: pod start and stop from the surface

`pod quote` (with the firm-or-flexible budget), `pod start` (with the guard check and the
out-of-band code), `pod budget`, `pod stop` and `pod status`; and the run-on-start entry
on the pod so `start --pod` needs no SSH, built only after the first hand-route live run
has shown the guard working. The local JSON API (`verbatus serve`, `127.0.0.1` only, a
fresh token per start, `Host` and `Origin` checked) comes here too.

### Beta

Kept short on purpose; each is designed in full when it is built.

- **Code catalogue versioning.** Codes are stable strings; a retired one moves to a
  `RETIRED` list with its replacement and is never reused; the catalogue has a semver
  number that `help --json` reports.
- **`remedy` and `retryable`** on every error code, and a `WarningCode` table of the same
  shape, so an agent can tell which next steps it may run itself.
- **`settings.toml`** at `$XDG_CONFIG_HOME/verbatus/`, outside any checkout, with a closed
  schema (an unknown key is refused). It never holds anything that spends or changes how
  pages are read; the spend policy stays `config/spend.toml`.
- **The lot ledger** and `verbatus lot <lot>`, local only, never in an event, a
  notification or anything bound for GitHub.
- **Server-sent events** on the API for `watch`.
- **The web page**, static, over the API, with no logic of its own.
- **The MCP server** over standard input and output, one tool per command, with the
  paid confirms as separate tools and the hosted-AI filter on every answer. It is kept on
  the lead's machine, with its endpoints, keys and provider details, and never enters
  the public repository.

### Deliberately left out

- Any provider other than RunPod, and any second live pod.
- Reaching the API or the web page from another machine, the lead's phone included (a
  possible later feature), and any login other than the local token.
- More than one user, and any user id.
- Stopping a stage half-way.
- Automatic extension, restart or retry of anything paid.
- Publishing anything: the MCP server, an install listing, or any endpoint, key or
  provider detail.
- Hosted AI access to transcriptions or images.
- Native Windows (WSL is supported like Linux).

## Lead's rulings (2026-10-02)

> **Superseded 2026-10-07, rulings 1, 3 and 4.** Pod budgets are off by default and there
> are no automatic spend limits (`pod_budget` in `config/spend.toml`); an idle pod is
> warned about, backed up at 1 hour, and deleted at 2 hours only with `ladder_delete =
> "on"` (`operations/pod/pod_guard.sh`). See the notes under "Budgets" and "Pause and stop
> on a pod".

The lead answered the seven open questions. The body above follows them.

1. **Pod budgets have a soft and a hard maximum, in time and in cost.** At a pod's quote
   the surface asks whether the budget and timeline are firm or flexible, and records
   both limits. Up to the soft maximum the run proceeds. Going from the soft to the hard
   maximum needs the code that reaches the lead's phone; an AI driver may prepare and
   request that extension but cannot complete it. Going past the hard maximum always
   needs the lead's new, explicit permission, which sets a new hard maximum; no AI driver
   and no shortcut on the spot can do it. The spend policy keeps the outer default soft
   and hard values. Starting a pod keeps its quote and code.
2. **A hosted AI sees no transcriptions or images in this version.** Driving needs only
   states and counts. Giving an AI an image would mean downloading it into its session,
   which costs tokens and would be a separate, later permission. Checking that images
   were processed correctly is a local feature for a person, planned with pagekit's
   before/after view.
3. **Going over budget for one pod** is settled by ruling 1; editing the spend policy is
   no longer the route.
4. **There are two kinds of pause.** A soft pause stops the run at a stage boundary so
   the lead can look at a problem, and the pod stays up. A hard stop leaves nothing
   running or planned, and the pod is deleted after a spend-policy idle limit (30 minutes
   by default) with no action; that limit is the existing pod guard's idle rule.
5. **The pod may start its run without the lead logging in over SSH.** The command line,
   the local interface or an AI driver through them triggers it with the lead's own
   credentials, which stay on the lead's machine or in the provider's secret store and
   never in the repository or anything published. It is built only after the first
   hand-route live run has shown the guard working.
6. **A web page on the phone** is a possible future feature, not needed now.
7. **Nothing is published for now.** The MCP server and every specific, such as
   endpoints, keys and provider details, stay local and never enter the public
   repository.
