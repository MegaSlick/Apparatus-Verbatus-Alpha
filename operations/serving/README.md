# Serving

This package starts one already-resolved chair as a loopback vLLM server, proves
that it answers, publishes evidence of that serving moment, carries the stages'
reading requests one at a time, and stops the server with verified shutdown. It
also holds the pod's golden-page smoke, the offline qualifier that turns a green
smoke into proof digests, and the Designator's two CPU detector engines. It never
ranks chairs, substitutes a revision, falls back to another model, calls a
provider API, downloads a model or claims a GPU fit.

## Map

| Module | What it does |
|---|---|
| `config.py` | Parses the serving catalogue (`config/serving_recipes*.toml`) into typed rows (`vllm`, `in-process`, `subprocess`, `fixture`, `unsupported`), checks proof digests, and freezes JSON values. |
| `manager.py` | `ServingManager`: the lifecycle of one vLLM chair, its receipt, launch audit and evidence blobs. |
| `process.py`, `residency.py` | The child process in its own group (or one handed over by another process), and the pod-wide `flock` lease that keeps one chair resident. |
| `http.py` | The transport (whole-call deadline from `operations/http_deadline.py`), request bodies, and the OpenAI response parsers. |
| `capacity.py` | The capacity plan: how many sequences each chair is launched with on the measured card, never fewer than its row's. |
| `client.py` | `ChairClient`, the one client a stage reads a chair through, and `serving_mode_for`. |
| `chat_request.py` | Request helpers the Perlector and Coniector share. |
| `assembly.py` | Builds the client a stage uses and the pod's smoke reader from run-sealed configuration. |
| `preflight.py`, `smoke.py`, `witness.py` | The pod smoke: start a chair, ask it to read the golden page, record the evidence, stop it. |
| `qualify.py` | Offline: turn a green preflight report into candidate proof digests for review. |
| `detector.py`, `surya_detector.py`, `surya/` | The Designator's record detector (in-process) and Surya (its own environment). They never start a server. |
| `fakes.py` | A scripted endpoint and the stand-ins a `ServingManager` needs, for tests. |

## Which rows launch

`ServingManager.start(identity, tier)` takes exactly one row for
`(serving_recipe, chair, placement tier)`. `verify_recipes_cover_chairs` checks
that lookup for every configured chair at every tier offline, so a misspelt
recipe or a missing row fails in tests, not on a rented card.

- `fixture`, `in-process`, `subprocess` and `unsupported` rows are refused by
  name before any lease or process: fixture rows answer only the offline
  skeleton, and the in-process and subprocess engines run inside their stage.
- A `vllm` row launches only when `preflight_state = "proven"` and its
  `preflight_digest` (the row) and `preflight_identity_digest` (the chair's
  checkpoint) still match. Repointing a chair leaves its row unchanged, so the
  identity digest is what catches it. Stamp the identity digest first; the row
  digest covers it.
- An `unproven` row launches only under a qualification purpose: the pod smoke
  (`preflight-qualification`) or a run started with
  `--mechanics-qualification`. The launch audit records which.
- Only full checkpoints are served: the roster refuses a chair declared as an
  adapter of another (`adapter_of`) when it is parsed.

## Scaling to the card

A row's `max_num_seqs` is written for the smallest card of its tier, and it is the
floor. A row that also states `weights_gib` and `kv_gib_per_seq` (the weights on the
card and the KV one full-length sequence holds, from the catalogue's notes) can be
launched wider on a card with room. PREFLIGHT derives the capacity plan
(`capacity.py`, schema `capacity-plan.v1`) from the card `SystemGpuProbe` measured:

    n = clamp(floor((U x VRAM - W - A) / P), row max_num_seqs, C)

with U the fraction the row launches with (its `gpu_memory_utilization`, at or under
the tier's `engine_memory_fraction`), W `weights_gib`, P `kv_gib_per_seq` and A 4 GiB
for everything else the engine holds (activations, the vision encoder, CUDA graphs),
and C the tier's `planned_batch_ceiling` in `config/pod_placement.toml` (24 GB 8,
48 GB 48, 80 GB+ 64; 64 when a tier sets none). `batch_size` still bounds the row;
PREFLIGHT checks the row against it and the planned width against C, and
`ServingManager.start` refuses a plan derived under another ceiling or wider than C.
A is not measured: a pod's launch log reports the KV pool vLLM allocated, which is
what to size it from. Only `max_num_seqs` moves; `gpu_memory_utilization`,
`max_num_batched_tokens` and every field that shapes a reading stay at the row, and
proof marks and `profile_preflight_digest` are checked on the row.

A row with `shares_service_with` is planned once with the chair whose service it
shares: the pair gets the width derived from that chair's row, so both render the
same launch, the catalogue's identical-launch-fields rule still describes what runs,
and the take-over's argv check holds.

`ServingManager(capacity_plan=...)` launches `replace(row, max_num_seqs=n)` after
checking the plan was derived from this row, tier and sealed serving digests and that
n is not below the row. The handle's profile is the launched row, so the stage
windows (`handle.profile`, or `assembly.launch_row` before a chair starts) take n.
The PREFLIGHT smoke launches at n, so each chair is proven on the card at the width
the stages use. The launch audit's `profile` is the launched shape and its `capacity`
block records the row's width, n, both ceilings (`row_ceiling`, `planned_ceiling`),
the card and the plan digest. No plan (no
`--capacity-plan`, or a card the probe could not measure) launches every row as
written, and the audit carries no `capacity` block.

## Lifecycle

Before launch: exact package pins, the verified snapshot, the processor geometry
the row claims (`patch_size`, `merge_size`), a hybrid Mamba/attention checkpoint
with prefix caching on (refused, keyed by repository), an env-override file in
the working directory (refused), a service handed off by an earlier process
(taken over or stopped; see "A shared service"), the pod lease, and an
endpoint that already answers (refused). The child is `sys.executable -m vllm.entrypoints.cli.main
serve` over the verified snapshot, with the row's flags, `--revision` pins for a
Hugging Face chair, `--generation-config vllm` (so no sampling value is filled
from a file), `--no-enable-log-requests` and `--enable-prompt-tokens-details`.

Ready means `/health` 200, the exact served id in `/v1/models`, and a non-blank
answer from that id to the row's readiness probe. A named fatal line in the
launch log or an exited child ends the wait early; a timeout says whether the
engine was still loading, refusing connections or answering but not ready, with
a bounded, credential-scrubbed log tail.

On success the manager publishes three content-addressed blobs: the closed
`chair-serving-receipt.v1`, the `serving-launch-audit.v2` (profile, argv digest,
pins, observed packages, identity, readiness, launch purpose, the sealed
configuration digests and, under a capacity plan, `capacity`) and `serving-evidence.v1` linking them. `stop()` releases
the lease only once the process group is gone and the endpoint refuses
connections; otherwise it keeps the lease and `recover()` retries the same
cleanup.

## A shared service

The Perlector and the Coniector's reconstructor serve one checkpoint, so the
reconstructor's row names `shares_service_with = "perlector"`. The catalogue
then requires the two rows at a tier to have identical launch fields
(`config.LAUNCH_FIELDS`: the argv, the readiness probe and the package pins,
port and served model id included), and only such a pair may share an endpoint
and a served model id.

Each stage is its own program, so the service passes between two processes:

1. The orchestrator tells the Perlector the Coniector runs next
   (`--hand-off-to-coniector`). After its seal, a Perlector whose chair is up and
   whose reconstructor row shares its service calls `ServingManager.hand_off`:
   it writes a hand-off record beside the lease (`/tmp/verbatus-pod-gpu.hand-off.json`:
   pid, the process's kernel start time, the launch log, the receipt, the launch
   audit and its references, and the run it belongs to) and closes its lease
   descriptor without unlocking. The vLLM process inherited the lease, so the
   card stays leased while it lives, and the Perlector exits.
2. The Coniector's `ServingManager.start` finds the record and, because its row
   shares that chair's service, takes it over when every check passes: the same
   run; the launch audit is the one its reference names; the same sealed serving
   configuration; the two identities equal but for role and recipe; the same
   tier; this row and the reconstructor's verified snapshot render the argv the
   service was launched with (its digest); the same installed packages; the pid
   is still the process the start time names; `/health` answers 200 and
   `/v1/models` the exact served id. It then consumes the record and publishes a
   receipt for the reconstructor that keeps the service's `started_at` and
   endpoint, and a launch audit with `launch_purpose = "adopted"`, the original
   readiness evidence, and an `adoption` block naming the Perlector's receipt,
   audit and evidence, the purpose each side ran under, the moment and what
   `/health` and `/v1/models` answered.
3. Stopping a taken-over service signals its process group (it is not this
   process's child, so its exit status is unknown), waits for the group to go
   and the endpoint to refuse, and then waits up to the shutdown timeout for
   the lease to come free. It never held that lease: the launching manager's
   descriptor went to the service's processes. One that left the group can
   keep it after the group is gone (seen live on 2026-10-08), so a lease
   still held does not fail the stop; it is left held, which refuses every
   later start on the card until it is free, and its holders are named on
   stderr and in that refusal. Stopping a handed-off service nobody took over
   (`displaced_service`, `reclaim_hand_off`) still requires the lease free, and
   keeps the record until it is.

Any refused check is recorded, never hidden: the start stops the handed-off
service and starts its own, and that launch audit carries `adoption_refused`
(why) and `displaced_service` (what was stopped). Any other start that finds a
hand-off record stops that service first, and says so in `displaced_service`;
an unreadable record is removed and named in `hand_off_discarded`. A Coniector
that sends nothing stops a service left for it before its own seal
(`ChairClient.reclaim_hand_off`). A run that stops between the two stages
(a halt at the Perlector's checkpoint, a failed sync) leaves the service
running, holding the card, until the next start on the pod stops it or the pod
is shut down.

The Perlector's seal is written while its chair is still serving, so a failed
shutdown of that service is reported by the Coniector, or by the next start,
not by the Perlector.

## Reading through `ChairClient`

A stage enters the client (which starts the chair and re-reads its published
receipt), then calls `read(ChairRequest)`, which sends exactly one request:

1. Refuse an unbuildable request before building anything: a kind other than
   chat completions, a caller field outside `CALLER_GENERATION_FIELDS`, image
   digests that do not match the request's images in order.
2. Build the body with the chair's sealed sampling row from
   `config/decoding.toml` and the row's seed, and POST it.
3. Retain the raw response bytes before checking anything.
4. Write a closed `chair-call-record` (`common/contracts/serving.py`): what was
   sent, what the engine samples under, the response, its usage, the request's
   capacity record, and `usage_reconciliation`, which sets the engine's own
   image-token and prompt counts beside the counts the request was admitted on
   and names any disagreement. A page read at another scale than the row's pixel
   bounds shows up there; it is evidence, never a refusal.
5. Return the response; a body that does not parse is `parse_problem`, not an
   exception. A transport failure writes a `chair-transport-failure` record
   instead, with delivery and completion recorded as unknown.

A request carrying a `loop_guard` (the Perlector's page reading, under the sealed
guard of `[perlector_generation]` in `config/decoding.toml`; no other chair has
one) is streamed instead: the client watches the reply's server-sent events as
they arrive and closes the connection, which makes vLLM abandon the request, as
soon as `common/repetition_loop.py` finds the same line or block of lines
repeated the sealed number of times in a row. The bytes received up to the stop
are retained exactly as they arrived, and the call is written as a
`chair-stream-call-record` (or `chair-stream-transport-failure`) whose `stream`
names the guard and the loop that stopped it. Every other request is sent whole,
exactly as before.

Chandra's retry loop uses `prepare_chandra_native` / `read_chandra_native`: one
prepared dispatch per attempt, bound to a durable intent record the stage
publishes first. `serving_mode_for` decides fixture or live per chair from the
sealed rows.

## Pod smoke and qualification

`assemble_serving_smoke_reader` builds the reader the pod preflight runs. For
each chair it starts the service, has `VisionSmokeCall` send the golden page
under the chair's sealed sampling row, and stops the service in `finally`. The
page carries a page witness, a 43-character random string from
`witness.PAGE_WITNESS_ALPHABET` with no adjacent repeats, which appears nowhere
in the prompt. A checkpoint whose template can open in thinking mode is asked
for a direct answer, as its run calls are (`common/chair_wire.py`). The receipt
records digests, never the page, prompt, witness or answer.

The DAI chair (`witness_adapter = "dai.v1"`, a handwriting record reader) does
not read the golden page: it misreads the typed code about half the time while
reading real records well. Its smoke reads one pinned record of the public
RecordGold corpus it was trained on (`recordgold_smoke.py`), asked what its run
asks, and is scored by character error rate against the record's expert
transcription after the corpus's `graphemic-v1` normalisation, passing at CER
0.15 or under. The repository holds only the record's identity and digests; the
pod fetches the crop from Teklia's IIIF server and the transcription from the
Hugging Face dataset at preflight, verifies both against the pins and refuses by
name -- `recordgold-smoke-fetch-failed` for the network, `-image-mismatch` or
`-text-mismatch` for a changed record -- before any chair starts. Its receipt
carries `smoke_page = "recordgold-record"`, the record's pins, the page digest,
the edit count and the rate; never the text.

`python -m operations.serving.qualify --report … --evidence-root …
--models-config … --serving-recipes-config … --placement-config …` reads a
green bootstrap report and its evidence and prints candidate digests for every
chair the preflight placed and smoked at the measured tier. Each candidate
needs its own exact read of the witness, bound to the retained artifacts; only
edit distance zero proves a row. The DAI chair's candidate is instead its read
of the pinned RecordGold record: the receipt's pins must be the repository's,
and the retained answer is re-scored against the committed transcription and
must agree with the receipt and pass the pinned rate. A receipt that names any
other page, or a RecordGold receipt from any other chair, is refused. The
command never edits the catalogue.
