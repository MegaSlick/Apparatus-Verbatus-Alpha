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
| `process.py`, `residency.py` | The child process in its own group, and the pod-wide `flock` lease that keeps one chair resident. |
| `http.py` | The transport (whole-call deadline from `operations/http_deadline.py`), request bodies, and the OpenAI response parsers. |
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

## Lifecycle

Before launch: exact package pins, the verified snapshot, the processor geometry
the row claims (`patch_size`, `merge_size`), a hybrid Mamba/attention checkpoint
with prefix caching on (refused, keyed by repository), an env-override file in
the working directory (refused), the pod lease, and an endpoint that already
answers (refused). The child is `sys.executable -m vllm.entrypoints.cli.main
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
pins, observed packages, identity, readiness, launch purpose and the sealed
configuration digests) and `serving-evidence.v1` linking them. `stop()` releases
the lease only once the process group is gone and the endpoint refuses
connections; otherwise it keeps the lease and `recover()` retries the same
cleanup.

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

`python -m operations.serving.qualify --report … --evidence-root …
--models-config … --serving-recipes-config … --placement-config …` reads a
green bootstrap report and its evidence and prints candidate digests for every
chair the preflight placed and smoked at the measured tier. Each candidate
needs its own exact read of the witness, bound to the retained artifacts; only
edit distance zero proves a row. The command never edits the catalogue.
