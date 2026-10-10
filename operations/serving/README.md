# Serving

This package starts one already-resolved chair as a loopback vLLM server, proves it
answers, publishes evidence of that serving moment, carries the stages' reading requests
one at a time, and stops the server with verified shutdown. It also holds the pod's
golden-page smoke, the offline qualifier, and the Designator's two CPU detector engines.
It never ranks chairs, substitutes a revision, falls back to another model, calls a
provider API, downloads a model or claims a GPU fit.

| Module | What it does |
|---|---|
| `config.py` | parses the serving catalogue (`config/serving_recipes*.toml`) into typed rows (`vllm`, `in-process`, `subprocess`, `fixture`, `unsupported`) and checks proof digests |
| `manager.py` | `ServingManager`: one vLLM chair's lifecycle, receipt, launch audit and evidence |
| `process.py`, `residency.py` | the child process in its own group, and the pod-wide `flock` lease that keeps one chair resident |
| `http.py` | transport (whole-call deadline from `operations/http_deadline.py`), request bodies, response parsers |
| `capacity.py` | the capacity plan: how many sequences each chair launches with on the measured card |
| `client.py` | `ChairClient`, the one client a stage reads a chair through, and `serving_mode_for` |
| `chat_request.py` | request helpers the Perlector and Coniector share |
| `assembly.py` | builds a stage's client and the pod's smoke reader from run-sealed configuration |
| `preflight.py`, `smoke.py`, `witness.py`, `recordgold_smoke.py` | the pod smoke |
| `qualify.py` | offline: turns a green preflight report into candidate proof digests |
| `detector.py`, `surya_detector.py`, `surya/` | the record detector (in-process) and Surya (its own environment); they never start a server |
| `fakes.py` | a scripted endpoint and stand-ins for tests |

## Which rows launch

`ServingManager.start(identity, tier)` takes exactly one row for `(serving_recipe, chair,
placement tier)`. `verify_recipes_cover_chairs` checks that lookup for every chair at every
tier offline, so a missing row fails in tests, not on a rented card.

- `fixture`, `in-process`, `subprocess` and `unsupported` rows are refused before any lease
  or process.
- A `vllm` row launches only when `preflight_state = "proven"` and its `preflight_digest`
  (the row) and `preflight_identity_digest` (the chair's checkpoint) still match.
  Repointing a chair leaves its row unchanged, so the identity digest catches it. Stamp the
  identity digest first; the row digest covers it.
- An `unproven` row launches only for qualification: the pod smoke
  (`preflight-qualification`) or a run with `--mechanics-qualification`. The launch audit
  records which.
- Only full checkpoints are served; a chair declared `adapter_of` another is refused.

## Scaling to the card

A row's `max_num_seqs` is written for the smallest card of its tier and is the floor. A row
that also states `weights_gib` and `kv_gib_per_seq` can launch wider on a card with room.
PREFLIGHT derives the capacity plan (`capacity-plan.v1`) from the measured card:

    n = clamp(floor((U x VRAM - W - A) / P), row max_num_seqs, C)

U is the row's `gpu_memory_utilization`, W `weights_gib`, P `kv_gib_per_seq`, A 4 GiB for
everything else the engine holds (not measured; size it from the KV pool vLLM reports), and
C the tier's `planned_batch_ceiling` (64 when unset). Only `max_num_seqs` moves; every field
that shapes a reading stays at the row, and proof marks are checked on the row.
`ServingManager.start` refuses a plan derived for another row, tier, sealed serving digest
or ceiling, or wider than C. A row with `shares_service_with` takes the width of the chair
whose service it shares, so both render the same launch.

The PREFLIGHT smoke launches at n, so each chair is proven at the width the stages use. The
launch audit's `capacity` block records the row's width, n, both ceilings, the card and the
plan digest. With no plan (no `--capacity-plan`, or an unmeasured card) every row launches
as written.

## Lifecycle

Before launch the manager checks exact package pins, the verified snapshot, the processor
geometry the row claims (`patch_size`, `merge_size`), and refuses a hybrid Mamba/attention
checkpoint with prefix caching on, an env-override file in the working directory, and an
endpoint that already answers. It handles any service handed off by an earlier process
(below) and takes the pod lease. The child is `sys.executable -m vllm.entrypoints.cli.main
serve` over the verified snapshot with the row's flags, `--revision` pins,
`--generation-config vllm` (no sampling value comes from a file),
`--no-enable-log-requests` and `--enable-prompt-tokens-details`.

**Ready** means `/health` 200, the exact served id in `/v1/models`, and a non-blank answer
to the row's readiness probe. A named fatal log line or an exited child ends the wait early;
a timeout says whether the engine was loading, refusing connections or not ready, with a
credential-scrubbed log tail.

On success it publishes three content-addressed blobs: `chair-serving-receipt.v1`,
`serving-launch-audit.v2` (profile, argv digest, pins, packages, identity, readiness, launch
purpose, sealed configuration digests, `capacity`) and `serving-evidence.v1` linking them.
`stop()` releases the lease only once the process group is gone and the endpoint refuses
connections; otherwise it keeps the lease and `recover()` retries.

## A shared service

The Perlector and the Coniector's reconstructor serve one checkpoint, so the
reconstructor's row names `shares_service_with = "perlector"`, and the catalogue requires
the two rows at a tier to have identical launch fields (`config.LAUNCH_FIELDS`: argv,
readiness probe, package pins, port and served id). Each stage is its own program, so the
service passes between processes:

1. The orchestrator passes `--hand-off-to-coniector` to the Perlector. After its seal, the
   Perlector calls `ServingManager.hand_off`: it writes a hand-off record
   (`/tmp/verbatus-pod-gpu.hand-off.json`: pid, the process's kernel start time, launch log,
   receipt, launch audit and run) and closes its lease descriptor without unlocking. The
   vLLM process inherited the lease, so the card stays leased while it lives.
2. The Coniector's `start` takes the service over only when every check passes: same run;
   the referenced launch audit; same sealed serving configuration; identities equal but for
   role and recipe; same tier; the reconstructor's row and snapshot render the same argv
   digest; same packages; the pid is still the process its start time names; `/health` and
   `/v1/models` answer. It publishes a receipt keeping the service's `started_at` and
   endpoint, and a launch audit with `launch_purpose = "adopted"` and an `adoption` block.
3. Stopping a taken-over service signals its process group and waits for the group to go and
   the endpoint to refuse, then up to the shutdown timeout for the lease to come free. A
   process that left the group can keep the lease after the group is gone; that does not
   fail the stop, but the lease stays held and every later start on the card is refused,
   naming its holders, until it is free.

Any refused check is recorded: the start stops the handed-off service and starts its own,
with `adoption_refused` and `displaced_service` in its audit. Any other start that finds a
hand-off record stops that service first (`displaced_service`); an unreadable record is
removed and named (`hand_off_discarded`). A Coniector that sends nothing stops the service
before its own seal (`ChairClient.reclaim_hand_off`). A run that stops between the two
stages leaves the service holding the card until the next start or pod shutdown. Because the
Perlector seals while its chair still serves, a failed shutdown of that service is reported
by the Coniector or the next start.

## Reading through `ChairClient`

A stage enters the client (which starts the chair and re-reads its receipt), then calls
`read(ChairRequest)`, which sends exactly one request:

1. Refuse an unbuildable request: a kind other than chat completions, a caller field outside
   `CALLER_GENERATION_FIELDS`, or image digests that do not match the images in order.
2. Build the body with the chair's sealed sampling row (`config/decoding.toml`) and seed, and
   POST it.
3. Retain the raw response bytes before checking anything.
4. Write a closed `chair-call-record` (`common/contracts/serving.py`): what was sent, the
   sampling, the response, usage, the request's capacity record, and
   `usage_reconciliation`, which sets the engine's token counts beside the admitted ones and
   names any disagreement (evidence, never a refusal).
5. Return the response; an unparseable body is `parse_problem`, not an exception. A
   transport failure writes a `chair-transport-failure` record with delivery and completion
   unknown.

A request carrying a `loop_guard` is streamed: the Perlector's page reading (guard in
`[perlector_generation]` of `config/decoding.toml`) and every witness reading except
Chandra's (guard in `[witness_generation]`). The client closes the connection, making vLLM
abandon the request, as soon as `common/repetition_loop.py` finds the same line or block
repeated the sealed number of times. The bytes received are retained as they arrived, in a
`chair-stream-call-record` (or `chair-stream-transport-failure`) whose `stream` names the
guard and the loop. Every other request is sent whole.

Chandra's retry loop uses `prepare_chandra_native` / `read_chandra_native`: one prepared
dispatch per attempt, bound to a durable intent record the stage publishes first.
`serving_mode_for` decides fixture or live per chair from the sealed rows.

## Pod smoke and qualification

`assemble_serving_smoke_reader` builds the pod preflight's reader. For each chair it starts
the service, has `VisionSmokeCall` send the golden page under the chair's sealed sampling,
and stops the service in `finally`. The page carries a 43-character random witness from
`witness.PAGE_WITNESS_ALPHABET` (no adjacent repeats) that appears nowhere in the prompt. A
checkpoint whose template can open in thinking mode is asked for a direct answer
(`common/chair_wire.py`). The receipt records digests, never the page, prompt, witness or
answer.

**The DAI chair** (`witness_adapter = "dai.v1"`, a handwriting record reader) misreads typed
code, so it reads one pinned public RecordGold record instead (`recordgold_smoke.py`), asked
what its run asks, and passes at a character error rate of 0.15 or under after
`graphemic-v1` normalisation. The repository holds only the record's identity and digests;
the pod fetches the crop from Teklia's IIIF server and the text from the Hugging Face dataset,
verifies both, and refuses by name (`recordgold-smoke-fetch-failed`, `-image-mismatch`,
`-text-mismatch`) before any chair starts. The receipt carries `smoke_page =
"recordgold-record"`, the pins, the edit count and the rate, never the text.

```sh
python -m operations.serving.qualify --report … --evidence-root … \
  --models-config … --serving-recipes-config … --placement-config …
```

`qualify` reads a green bootstrap report and its evidence and prints candidate digests for
every chair placed and smoked at the measured tier. Only an exact read of the witness (edit
distance zero, bound to the retained artifacts) proves a row; the DAI chair's candidate is
its re-scored RecordGold read. A receipt naming any other page, or a RecordGold receipt from
another chair, is refused. It never edits the catalogue.
