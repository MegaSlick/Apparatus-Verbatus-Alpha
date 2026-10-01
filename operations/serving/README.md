# Serving manager

This package starts one already-resolved chair, proves that its loopback vLLM endpoint
answered, and publishes evidence for that actual serving moment. It does not rank chairs,
substitute a revision, fall back to a base model, touch a provider API, download a model,
or claim a GPU fit.

## Lifecycle

`ServingManager.start(identity, tier)` requires exactly one profile for
`(identity.serving_recipe, identity.role, measured placement tier)` from the serving-recipe
catalogue (`config/serving_recipes.toml`, or `config/serving_recipes_real.toml` for the real
roster). Zero or several matches refuse before launch. `verify_recipes_cover_chairs` checks
that lookup offline for every configured chair at every tier and refuses stale rows, so a
misspelt `serving_recipe`, a missing profile or a new tier fails in the test suite rather
than on a rented GPU.

**Which rows can launch.**

- A `kind = "fixture"` row is the walking skeleton's stand-in and is refused *by that name*
  before any lease, probe or process.
- A `vllm` row carries `preflight_state`, and anything but `proven` refuses at launch.
  There is no default: a row without the field refuses at parse time. `proven` is a
  reviewer's declaration in a reviewed file, not a measurement this package can make, just
  as a profile's GPU figures are planning values.
- A proven row carries `preflight_digest` (of every field except `preflight_state` and
  itself) and `preflight_identity_digest` (the chair's cache descriptor,
  `chair_preflight_identity_digest`), because a preflight proves flags *against a
  checkpoint*: repointing the chair in `config/models.toml` leaves the row byte-identical,
  so only the identity digest catches it, at launch and in `verify_recipes_cover_chairs`. A
  stale row digest refuses at catalogue load. **Stamp the identity digest first**: the row
  digest covers it. Both are removed together when a row returns to `unproven`;
  `chair_preflight_identity_digest` and `profile_preflight_digest` are exported for
  stamping.
- The one exception is the package's private smoke-preflight assembly, which may launch an
  `unproven` row for qualification. Its launch audit records
  `launch_purpose = "preflight-qualification"` and the row's state, and every other check
  still runs. Callers cannot select this purpose through `ServingManager`.

**Qualifying a row.** After a smoke preflight, render review candidates from the retained
report and its content-addressed evidence; this never edits the catalogue:

```console
python -m operations.serving.qualify \
  --report <volume>/bootstrap-report-<token>.json \
  --evidence-root <volume>/preflight/bootstrap-report-<token> \
  --models-config config/models-real.toml \
  --serving-recipes-config config/serving_recipes_real.toml \
  --placement-config config/pod_placement.toml \
  --output <volume>/serving-qualification-<token>.json
```

It requires a green completed preflight, exact cache and placement coverage, one
fixture-bound served read per chair, positive request counts, matching recipe and placement
file digests, all three serving artifacts with matching digests, and the page-witness token,
whose digest and expected semantic output it recomputes. It emits candidate digests for the
measured tier only; a reviewer writes the identity digest, then the row digest, and leaves
every other tier unproven. It refuses adapter chairs: an adapter's identity names a base
role without binding that base's checkpoint, so proving a base cannot prove the pair. The
real roster uses full checkpoints.

**Assembly.** Production assembly takes the `StageContext` that `open_context()`
revalidated, the `StageContextReceiptPublisher` for that context, and that context's own
registry, so one authority builds and publishes the identity-bearing receipt. The recipe and
placement files are parsed from the bytes that were hashed into the run configuration
digest, and a path substitution refuses before any probe, lease or subprocess.

**Launch.** Before launch the manager asserts every exact package pin, re-verifies the
snapshot, takes the caller's non-blocking pod/GPU `flock` lease (no default path: every
manager for one card must share it), and refuses an endpoint that already answers. The lock
descriptor is passed to the vLLM child, so a controller crash cannot release the lease while
the child is resident.

The command is `sys.executable -m vllm.entrypoints.cli.main serve`. **Not `python -m vllm`**:
vLLM 0.27.1's wheel has no `vllm/__main__.py`, and `vllm.entrypoints.cli.main` is what its
`vllm` console script points at. The launching interpreter must be the inspected one: a
`command_prefix` naming another interpreter is refused unless the caller also supplies a
`PackageInspector` for that environment, or the pin check and the audit's
`runtime_packages.observed` would measure the wrong Python.

This package asserts pins; it never installs them. For the real catalogue the pod installs
them: the `pod` dependency group (`vllm 0.27.1`, `transformers 5.14.1`,
`qwen-vl-utils 0.0.14`) is locked in `uv.lock` and synced by `operations/pod/bootstrap.py`,
and `operations/pod/test_pod_run.py` fails if it drifts from the catalogue's
`required_packages` (a drifted group would download ~10 GB and then be refused on a billing
card). `operations/pod/README.md`, "The serving stack, re-planned and locked", explains the
versions. No real row has been served yet.

The command uses the verified base snapshot, a stable `--served-model-name`, and the typed
profile flags. A Hugging Face chair gets its exact commit in `--revision` and
`--tokenizer-revision`, which stop vLLM resolving a mutable Hub ref. A local-repository
chair (the Perlector, a locally trained checkpoint "called like any other model") gets
neither: its pin is the digest manifest, and naming a revision it lacks would invent
provenance. Adapters are static: one `--lora-modules` entry, `--max-loras 1`, a supported
`--max-lora-rank`; the dynamic adapter-update endpoint is never called. The manager always
passes `--no-enable-log-requests`, because golden-page bytes and transcriptions are not
diagnostics and a recipe must not turn request logging on.

**Shutdown.** The lease spans probing, launch and verified shutdown. It is released only
after the child exits and a bounded `/health` poll sees a definite TCP connection refusal; a
timeout or other ambiguous failure is not proof of absence. Otherwise the stop reports
`VLLM_STOP_FAILED` and keeps the lease. `recover_failed_start()` retries only that same
cleanup; it cannot launch another chair around it.

## Readiness and adapter proof

Readiness is a bounded poll of the exact child and its fresh launch log. It fails early on
an exited child or a named log signature: `CUDA out of memory`, `EngineDeadError`,
`LORA_UNSUPPORTED` (`does not support LoRA`), `UNKNOWN_MODEL`, or `VLLM_ERROR` (reserved for
a launch wrapper that writes to this log; vLLM never prints it). Broad words like
`RuntimeError` are deliberately not matched: the poll re-reads the whole tail, so one benign
line would abort a good start. Success requires all of:

- `/health` HTTP 200;
- a parsed `/v1/models` `data[]` containing the exact served ID; and
- a non-streaming OpenAI-compatible response with that exact model ID and non-blank output.

A bare HTTP 200, a substring such as `reader-api-shadow`, or a routing stub cannot publish a
receipt.

**A 4xx on the readiness probe is deterministic** and stops the loop at once: the engine is
up and rejecting the exact body every poll resends, so waiting to `startup_timeout_seconds`
would spend GPU time to learn nothing. A 5xx or connection failure retries.

**A timeout says which kind of not-ready it was**: **still loading** (quoting the newest
progress line), **connection refused** (nothing in the log shows loading), or **answered but
never ready**, followed by a bounded, credential-scrubbed log tail. These call for opposite
responses: raise the row's `startup_timeout_seconds`, or find what is broken. Every row
still ships the unmeasured 300 s; the header of `config/serving_recipes_real.toml` says how
to derive each value once the first boot measures a volume read rate.

**Adapter proof.** An adapted chair must supply deterministic calibration: the manager sends
the same request (temperature zero, fixed seed) to the base and adapter IDs and refuses when
their semantic-output digests are equal. `AdapterCalibration.from_image_fixture()` accepts
only local fixture bytes: one non-empty `data:image/...;base64,...` URI with its SHA-256;
remote, `file:`, blank, malformed or mismatched images refuse before launch. A
tower/connector profile requires this image calibration, since text cannot certify the
visual path. The image must sit in a real `role=user` `messages[].content[]` image block;
an extension field named `image_url` is not visual evidence. The sealed request is rebuilt
from canonical bytes, so later mutation cannot swap its image. Listing an adapter in
`/v1/models` is never proof of activity.

**What this does not prove.** A digest difference is necessary, not sufficient: with no
base-versus-base control, engine nondeterminism (continuous batching, chunked prefill) could
produce a difference. And an active adapter that happens to answer like its base is refused,
loudly. **Whoever wires the real rollout must choose a calibration known to differ between
base and adapter.**

## Receipt and launch audit

`chair-serving-receipt.v1` is closed. It holds what answered: identity, revision/manifest,
tokenizer revision, seed, context and pixel caps, engine and version, dtype, base identity
where applicable, endpoint and launch time. Its `pixel_cap` is the **total pixel count**
given to vLLM, unlike `config/pod_placement.toml`'s longest-edge cap; the two are not
directly comparable (see the capacity check below).

Everything else — producer, times, served ID, typed profile, PID, explicit pins, argv
digest, required and observed package maps, identities and manifests, readiness digests,
adapter activation, and the serving-recipe and placement digests the sealed context
authorized — is in `serving-launch-audit.v1`, a content-addressed stage blob written by
`StageContextReceiptPublisher` through `write_serving_launch_audit`. A third blob,
`serving-evidence.v1`, links receipt and audit. A start succeeds only when all three
references return; they are exposed on the `ServiceHandle` and copied beside pod smoke
evidence.

## `generation_config`: `"vllm"` only

Every row is `"vllm"`, and the catalogue parser refuses `"auto"`. Under `"auto"`, vLLM 0.27.1
(`ModelConfig.get_diff_sampling_param`) fills any of `temperature`, `top_p`, `top_k`, `min_p`,
`repetition_penalty` and `max_tokens` a request leaves out from the model's
`generation_config.json`, unseen on the wire. Under `"vllm"` nothing is filled from the file,
and every chair's sealed decoding row names every one of those sampling fields
(`common.decoding.ENGINE_FILLED_SAMPLING_FIELDS`), so what a reading samples under is on its
call record. vLLM still reads the file's `eos_token_id`s under `"vllm"` and adds them as stop
tokens; the file's bytes are pinned by the chair's verified snapshot manifest.

## Hybrid-attention prefix caching

Chandra-2 (`datalab-to/chandra-ocr-2`, serving `attestator_1`)
and the Perlector (`Qwen/Qwen3.8-27B`) are hybrid Mamba/attention (`qwen3_5`) checkpoints.
`manager.start` refuses either with `enable_prefix_caching` on: prefix caching over
recurrent state costs memory, and this catalogue's rows run up to four sequences. vLLM
v0.27.1 itself keeps it opt-in for hybrid models (`arg_utils.py`: `not
model_config.is_hybrid`). The check keys on the exact `repo`, never the role, because test
fixtures reuse role names under `example/...` repositories.
`config/serving_recipes_real.toml` sets `enable_prefix_caching = false` for those three
rows, so the refusal guards against a future edit.

`manager.assert_processor_geometry` also checks a row's `patch_size`/`merge_size` against
the chair's own `processor_config.json`/`preprocessor_config.json` at launch, offline.

## Prompt-token accounting

Every real profile launches with `--enable-prompt-tokens-details`, which adds
`usage.prompt_tokens_details.multimodal_tokens` beside the always-present
`usage.prompt_tokens`. Without it, a silently dropped `mm_processor_kwargs`
(vllm-project/vllm#49015, #54527) would read a page at the wrong scale with no error.

`preflight.reconcile_usage_against_capacity` compares observed `prompt_tokens` with the
laptop's own image and text token arithmetic within a per-chair tolerance. A mismatch is
published through `.to_finding()` as `usage-capacity-mismatch`, never raised, because what
it means is a judgement for review. With the `multimodal_tokens` breakdown, `localized_to`
names whether the image or text half moved even on a mixed request (every real chair sends
both); without it, it names a half only when the other half expects no tokens, and
otherwise says `"unlocalized"`.

## Env-override files and static preflight assertions

`manager.assert_no_discoverable_local_env` refuses a real launch when an env-override file
(`local.env`, or `.env`/`.env.*` other than the tracked `.env.example`) is in the working
directory the vLLM child inherits. Nothing sealed or audited would see what such a file
injects (a Hub token enabling a forbidden fetch, a proxy, an engine flag). It runs inside
`ServingManager.start`, the one door every real launch passes, whether through
`ServingSmokeReader.read` or `ChairClient.__enter__`.

Three static assertions keep `proven` from resting on a smoke string alone:

- `assert_image_before_text_on_wire(content)` (`http.py`) — **wired.** `request_body` runs
  it, through `assert_wire_part_order`, on every rendered `role=user` content list that
  carries an image (the smoke, readiness probe, calibration probes and every reading). It is
  meaningful because `render_vllm_argv` pins `--chat-template-content-format openai`; under
  vLLM's `string` format images are hoisted ahead of text regardless of order
  (vllm-project/vllm#14047). `ServiceHandle.request_reading` POSTs a body verbatim, so a
  future caller that did not build its body through `request_body` would bypass the check;
  today's only caller, `ChairClient.read`, does.
- `assert_resized_pixels_within_trained_geometry(...)` — **not wired.** It needs each
  vendor's declared training pixel range, which no chair's data carries yet. Wiring it to
  the rows' own `min_pixels`/`max_pixels` would only check our own resize arithmetic
  (`common/request_capacity.py`'s `smart_resize` already clamps into them), which measures
  nothing. Carry the vendor ranges as cited data first (`cleanroom/README.md`).
- `assert_generation_config_key_coverage(...)` — refuses a vendor `generation_config.json`
  key neither sent nor named as withheld with a reason. **Wired only for DAI**
  (`attestator_2`), the one chair whose full vendor file is carried. `repetition_penalty`,
  `temperature`, `top_k` and `top_p` are sent verbatim from the sealed decoding table's DAI
  row; the engine reads `eos_token_id` from the pinned file (the secondary EOS is also sent);
  `bos_token_id`, `pad_token_id` and `do_sample = true` have no request field, and the
  nonzero temperature is what `do_sample` does; `transformers_version` is metadata. This
  accounts for configuration and request construction, not for what the engine applied.

## Pod seam

`assemble_serving_smoke_reader()` is the production assembly seam. Construction reads and
validates the recipe and placement catalogues and does nothing else: no process, socket,
provider or weights. The caller supplies the run-sealed `StageContext`, its receipt
publisher, the registry, the page-specific smoke call, the calibration function, the
measured `GpuProfile` and the pod/GPU lease. `assemble_serving_preflight_callback()` builds
the callable `SubprocessBootstrapActions` accepts, around `PreflightRunner`; it creates and
verifies its log root only when called.

The reader starts one named chair, runs the smoke request, records the evidence and stops
the child in `finally`; there is no fallback chair. A green smoke result is refused unless
the call completed a `ServiceHandle.request_fixture_image()`, which requires the single
image to hash to the golden-page fixture, and put that response's SHA-256 in
`SmokeResult.receipt["fixture_response_sha256"]`. The reader records response and output
digests, the fixture SHA-256 and request counts, never page bytes or response text. The
image helper validates one canonical JSON snapshot, so a mutable mapping cannot pass the
check and then serialize differently.

Before launch the reader refuses a profile whose dtype differs from the one preflight
measured (an exact match, not a floor), or whose memory fraction, context length, pixel
budget or batch size exceeds the measured placement plan. **`pixel_cap` and `max_pixels`
are different units**: the placement caps a longest edge, the profile's `max_pixels` is a
total count, so the check compares `max_pixels` with the square of the edge cap. The
overloaded word is a naming defect in the placement file.

A smoke result with no GPU/CPU utilization samples makes preflight red
(`utilization-missing`); sampling is the smoke callable's job, and no threshold here claims
a card is saturated.

The committed fixture catalogue has **fixture rows only**, with no port, memory, context or
pixel figures: a row this package refuses to launch should not carry unbenchmarked numbers.

## The golden-page vision smoke callable

`VisionSmokeCall` asks the chair for a **page witness**, an unguessable string printed on
the golden page and absent from the prompt. The smoke requires the exact
`PAGE-WITNESS: ` marker and allows up to two code edits and internal whitespace.
The lifecycle already proves the request carried the fixture bytes; this proves something
read them, under the chair's own sealed sampling row and the profile seed, as its run will
read (`chair_sampling`, built by the pod bootstrap from the pinned checkout's
`config/decoding.toml`; a chair without a row is refused before anything is sent). An answer built from the prompt alone yields the literal
`PAGE-WITNESS: <the page witness string>`, is format-invalid, and fails preflight. The
receipt records the code edit distance, identity and revision, the `served_model_id` the response body itself named
(`parse_openai_answer` refuses any other alias), the response digest, and `sha256(witness)`
— never the witness, prompt or answer. The witness token is retained separately under the
preflight evidence root. The qualifier checks the retained raw response against its
response digest, output digest, and witness distance. Only distance zero can prove a row.

**The fixture author owns the witness's entropy, lifetime and rotation**; the pod draws it
from a CSPRNG over the URL-safe token alphabet. The callable refuses only what cannot work
whatever its origin: under 32 or over 128 characters, blank, whitespace, non-token
characters, or present in the prompt. A weak or reused witness could be guessed and turn the
smoke falsely green.

The request declares `image/png`, and the sealed request bytes must be a complete, decodable
PNG under a 64 MiB encoded ceiling and the measured placement's pixel bound; nothing else on
this path checks the format. The snapshot is checked rather than the path, which could be
replaced after the request was built.

At most 1,024 utilization samples are accepted per request, so a broken sampler cannot
inflate the durable record. `SmokeResult.nonempty` is reported independently of
`shape_valid`; a blank answer is already refused by `parse_openai_answer` and reaches the
runner as `smoke-read-failed`.

**One call at a time.** `ServiceHandle` records its last fixture request on itself and the
reader checks the receipt against it afterwards, so two concurrent calls on one handle would
cross records. Nothing is concurrent today (`ServingManager.start` refuses a second start
while a handle is active, and `PreflightRunner` reads chairs in sequence); a lock in the
handle would not make the reader's read-after-write atomic.

## Client

`client.ChairClient` is the one client a stage holds for one chair across a pass. It
composes a built `ServingManager` and never starts a pod, picks a chair, retries, re-samples
or edits a response. Use it as a context manager: `__enter__` calls
`manager.start`, then re-reads the published receipt through the tree
(`read_receipt=context.tree.read_run_receipt` in production) and refuses with
`ReceiptDriftRefusal` (`CHAIR_RECEIPT_DRIFT`), stopping the service, unless it still names
this exact chair and revision. `__exit__` always stops the handle; a `ServiceStopError`
propagates. If the drift stop itself fails, the drift refusal is raised with the stop
failure chained as its `__cause__`.

**Request shape, decided by the stages.** The stages build the messages and this package
sends them unchanged.

- **The image part comes before the text part** in a chair's user turn: each chair's chat
  template emits parts in list order, and each was fine-tuned with the vision block first
  (`pipeline/3_attestatores/live_witness.py::_user_content`).
- **`max_tokens` is `min(the chair's declared upstream bound, max_model_len − the request's
  image and prompt cost)`**, with the second term expressed by sending no field
  (`common/request_capacity.py::sendable_max_tokens`): our own count of the remainder could
  earn an HTTP 400 the engine's count would not, while the declared bound, sent only when
  smaller, stops a chair generating far past its publisher's length.
- A caller's `generation_sent` may carry only `CALLER_GENERATION_FIELDS`
  (`common/contracts/serving.py`), one allow-list for every chair: `max_tokens`,
  `chat_template_kwargs` and `stop_token_ids`. Today the builders send `max_tokens` (every
  chair), `chat_template_kwargs` (both Chandra chairs and the Perlector) and
  `stop_token_ids` (DAI's redundant second EOS). The sampling
  values are the chair's sealed row (below). The call record shows exactly what went out.

**`ChairClient.read(ChairRequest) -> ChairResponse` issues exactly one request:**

1. Refuse an unbuildable request before building anything: a `kind` other than
   `chat-completions`; a `generation_sent` field outside the caller allow-list, named in the
   refusal (vLLM's request model ignores an unknown field silently, so a misspelt one would
   otherwise vanish); image digests that do not match `image_sha256s` exactly and in order.
2. Build the body with the chair's sealed sampling row and the seed, and POST through
   `ServiceHandle.request_reading`. The client takes the sealed decoding policy the stage
   loaded (refused unless it seals to the recorded digest) and selects the row by its own
   `identity.role`: `config/decoding.toml`'s `chair_decoding`, the chair's makers'
   recommendation. Every request sends the profile's seed. Both are on the call
   record's `generation_sent`, and `sampling_effective` beside them holds what the pinned
   engine samples under (`common.decoding.engine_effective_sampling`: vLLM 0.27.1 raises a
   temperature in (0, 0.01) to 0.01, as for Churro's 1e-06, and a greedy request runs at
   `top_p` 1, `top_k` 0, `min_p` 0, as for Chandra's first request). vLLM samples a seeded
   request from that request's own generator, so the same request on the same engine build,
   model and hardware draws the same samples; batching and kernels can still move low-order
   logits, so a repeat is reproducible in intent, not guaranteed bit for bit.
3. **Retain the raw response through the caller's `retain` callable before checking
   anything**: when vLLM refuses a request it says why in the body of a non-200, and that
   sentence must reach disk before any refusal can discard it.
4. Classify a non-200 or wrong-model response, write a closed `chair-call-record.v3` (HTTP
   status, requested and observed model), then raise with both retained references.
5. Otherwise parse. A content or choices problem becomes `parse_problem` on the returned
   `ChairResponse`, never an exception, because a malformed witness body is evidence, not a
   stage abort. Write one `chair-call-record.v3` (fields in
   `common/contracts/serving.CHAIR_CALL_RECORD_FIELDS`) and return. Every stage reader of a
   call record (the Designator's structure attempts and minted acts, every Testimonium's
   serving call, every Perlector page reading) holds its
   generation fields, sampling and seed to the sealed row through one check,
   `common.decoding.verify_call_sampling`; a reader holding only the stage context calls it
   through `common.stage.verify_retained_call_sampling`. A call record written under a
   retired schema (`common/contracts/serving.RETIRED_CALL_RECORD_SCHEMAS`) is refused by
   name.

`parse_openai_reading` cannot tell "no model named" from "wrong model named" (both are
`CHAIR_RESPONSE_MODEL_MISMATCH`), so `read` reports a body with no `model` field as
`CHAIR_RESPONSE_INVALID` rather than claim a foreign source it never observed.

**Transport failure.** If the POST raises before a complete response exists, the client
retains `chair-transport-failure.v2` instead: the same request, identity, receipt, decoding,
generation and capacity facts, every response field null, delivery and completion recorded
as `unknown`. `ChairTransportFailure` names it so a stage can keep one terminal attempt
rather than repeat a request whose engine-side completion is unknown.

**Chandra native calls.** `prepare_chandra_native` / `read_chandra_native` is the one narrow
exception, for page-scoped `attestator_1` with adapter `chandra.v1` under the exact
`decoding.v6` recipe. Each prepared dispatch sends Attestator 1's sealed row with one of
seven declared temperature/top-p pairs (`common.decoding.chair_attempt_decoding`, from the
client's one decoding policy), and the stage must publish and pass a durable attempt-intent reference before the
POST; `chandra-native-call-record.v2` binds it, so the three identical 0.8/0.95 attempts stay
distinct. These calls send no per-request seed because the pinned upstream client sends
none (the launch seed stays on the serving receipt).
`chat_template_kwargs.enable_thinking=false` is a local compatibility field for vLLM 0.27
and this template, recorded as such, not attributed to the upstream vLLM 0.17 recipe. Each
call is still one HTTP request; the Attestatores stage owns the vendor retry loop and its
evidence.

**Capacity.** `ChairRequest.capacity` is the caller's `common.request_capacity` record of
whether the request fits the sealed row; only the caller knows the prompt and answer shape,
so the client neither computes nor checks it. `read` copies it into the call record (`null`
where none is stated, as for the readiness probe and `smoke.py`). `ChairRequest` takes a
detached, canonicalized, deep-frozen snapshot, because the record is nested and builders
keep their own reference; a record `canonical_bytes` cannot hold is refused at construction
as `CHAIR_REQUEST_INVALID`.

**Reading parser versus probe parser.** A readiness probe must prove the engine answers, so
`parse_openai_answer` refuses blank content. A witness can legitimately return the empty
string (`genuinely-empty`), so `http.parse_openai_reading` accepts `content == ""` and
refuses only missing or non-string content. Its `finish_reason` is the engine's own word;
missing or `null` becomes `None`, and mapping unknown words is the stages' job.

**`request_timeout_seconds`** is per profile, because a non-streaming generation returns
nothing until it is done; the manager's own readiness and adapter probes keep their own
short budgets, which a slow reading must never stretch.

**`serving_mode_for(recipes, identity, tier)`** selects live or fixture by a three-name
lookup (`recipe`, `chair`, `tier`), never a ranking. If every row for the chair is fixture,
the chair is fixture at any tier. Otherwise a tier is required (`SERVING_MODE_UNRESOLVED`
without one), and the row at that tier decides: an `UnsupportedProfile` refuses with its
reason, and a fixture row beside non-fixture rows for the same chair refuses, naming what
the other tiers hold. A tier with no row is re-raised as
`ServingModeRefusal("SERVING_MODE_UNRESOLVED", ...)`, so callers see one refusal vocabulary.

## Fakes for stage tests

`fakes.py` holds a fake endpoint and builders for stage tests against `ChairClient`
(`ScriptedAnswer`, `FakeEndpoint`, `FakeLauncher`, `FakeProcess`, `FakePackages`,
`FakeRegistry`, `FakeBlobStore`, `fake_serving_factory`, and the structure-chair builders).
They mirror `test_manager.py`'s own fakes rather than sharing them.

- `ScriptedAnswer.finish_reason` distinguishes `None` (a JSON `null`) from the `ABSENT`
  sentinel (key omitted); the parser treats both as absence.
- `FakeEndpoint` answers a manager's one readiness POST (always the first POST it sees)
  without consuming a scripted answer, so each `ScriptedAnswer` is consumed by a real
  `ChairClient.read`.
- `assert_retained_before_next_request` refuses a reading request until the exact bytes it
  served last time are in the shared `FakeBlobStore` by digest (a count would be satisfied
  by the call record alone). The strongest proof of retain-before-parse is in
  `test_client.py`, which makes `parse_openai_reading` raise and shows the bytes were
  already kept.
- `sticky_after_stop` keeps `/health` answering after the process is told to exit, for tests
  that need `handle.stop()` to fail.

The structure-chair builders (`structure_box_1000`, `structure_layout_block`,
`structure_answer_body`, `structure_blank_page_body`, `scripted_structure_answer`,
`scripted_structure_refusal`, `scripted_structure_cut_off`) take rectangles in the sealed
page's pixels and return **Chandra's layout HTML**, whose `data-bbox` values are found by
search over the 0–1000 grid and checked through `common.chandra_layout.to_page_bounds`
itself, so the builder cannot agree with a converter that changed. Each builder reads its
body back through `common/chandra_layout.py::parse_layout_html` and checks the rectangles,
so a drifted builder fails in the builder, not three stages later.

- `structure_layout_block` writes one top-level `<div>`, for answers rectangles cannot
  express: a bad or missing `data-bbox`, a label outside the vendor's nineteen,
  `Blank-Page`.
- `structure_blank_page_body` is a page the chair reports blank; this grammar has no
  empty-list shape, and a body with no `<div>` is a refusal.
- `scripted_structure_refusal` is keyed by `PARSE_OUTCOMES` code and covers the two
  outcomes a body's shape can reach; the other four are wire-byte properties tested in
  `common/test_chandra_layout.py`.
- `scripted_structure_cut_off` truncates an answer before its first block closes and sets
  the `length` stop word, as an overrun of `max_model_len` looks. The truncated body still
  parses (with an `unclosed-block` finding), so the page is held on the stop word alone.

## End to end

`pipeline/test_live_reading_seam_e2e.py` runs the whole seam: the real stage programs carry
a tree through the Designator, three live witness chairs, a live Perlector, the Recensor,
Archetypus and Armarium. Every chair answers through `fakes.py`; no pod starts. The run is
live the same way it would be on a card — a temporary catalogue with `kind = "vllm"` rows for
all four chairs at all three tiers — so the selector under test is the sealed one.

- **A live run reaches a sealed terminal export**, with every reading naming the exact bytes
  its engine sent. The export is **held for review**, and that is the rules working: Churro
  reads its vendor's `HistoricalDocument` grammar, which has no coordinates, so it does not
  attach and two witnesses fall short of the floor of three. Attaching a witness without
  geometry is the Perlector's `anchor-line` basis, not yet built. Even then, one scripted run
  over a fixture is not a proven pipeline.
- **Two independent drivers reach the same fixture tree byte for byte**: the orchestrator's
  subprocess chain, and this suite's driver pointed at the committed fixture catalogue with
  `--placement-tier` and in-process stage `main`s. Whether the fixture tree itself moved is
  `pipeline/orchestrator/test_orchestrator_acceptance.py`'s `HAPPY_RUN_TREE_DIGEST` to say.
