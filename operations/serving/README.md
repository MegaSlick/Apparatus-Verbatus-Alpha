# Serving manager

This package starts one already-resolved chair, proves that its loopback vLLM
endpoint answered, and publishes evidence for that actual serving moment. It
does not rank chairs, substitute a revision, fall back to a base model, touch a
provider API, download a model, or claim a GPU fit.

## Lifecycle

`ServingManager.start(identity, tier)` requires exactly one profile for
`(identity.serving_recipe, identity.role, measured placement tier)` from
`config/serving_recipes.toml`. Zero or multiple matches refuse before launch.
A real row marked `preflight_state = "proven"` must also carry the
`preflight_digest` of all its other canonical fields. A stale digest names and
refuses the edited profile during catalogue load; the manager independently
retains its launch-time refusal of every state other than `proven`.
A preflight proves a flag profile *against a checkpoint*, so a proven row also
carries `preflight_identity_digest`, the digest of that chair's cache
descriptor (`chair_preflight_identity_digest`). Repointing the chair in
`config/models.toml` leaves the catalogue row byte-identical, so the row digest
cannot see it and the manager refuses the mismatch at launch instead.
`verify_recipes_cover_chairs` performs that same chair-identity reconciliation
offline, so a repointed chair fails in the test suite without a GPU launch.
Both halves are stamped after a green preflight and removed together when a row
returns to `unproven`. Stamp the identity digest *first*: `preflight_digest`
covers every field of the row except `preflight_state` and itself, and that
includes `preflight_identity_digest`, so a row digest taken before the identity
digest is written is stale the moment it lands. `chair_preflight_identity_digest`
and `profile_preflight_digest` are exported for exactly that stamping.
`verify_recipes_cover_chairs` proves that lookup offline for every configured
chair at every configured tier and refuses extra stale rows, so a misspelt
`serving_recipe`, an unconfigured chair profile, or a newly added placement tier
is a test failure here rather than a refusal on a rented GPU.
A profile whose `kind` is `fixture` is the walking skeleton's stand-in and is
refused *by that name* before any lease, probe or process — it carries no
vLLM flags to be refused by, and blocking it with an unsatisfiable version pin
would report the wrong cause.
A `vllm` row also carries `preflight_state`, and `unproven` refuses launch at
that same door, by that same name. A real row is written from reviewed, locked
vLLM and model-stack versions, a verified manifest, and a real-silicon
preflight; the field records whether that preflight has happened for *this
exact profile*. It is a declaration in a reviewed config file and not a
measurement — nothing in this package can observe a preflight that ran
elsewhere — so `proven` means a reviewer asserted it, exactly as a profile's
GPU figures are planning values rather than a measured fit (GOVERNANCE 10).
There is deliberately no default: a row written before the field existed
refuses at parse time rather than reading as proven.
The recipe and `config/pod_placement.toml` byte digests are both part of the
run configuration digest. Production assembly requires the `StageContext` that
`open_context()` revalidated and the `StageContextReceiptPublisher` for that
same context, and the registry must be that context's own registry. This keeps
one authority behind construction and publication of the identity-bearing
receipt. Assembly parses each supplied TOML from the hashed bytes and refuses a
path substitution before any probe, lease, or subprocess action.

Before launch it asserts every exact profile package pin, re-verifies the
named snapshot, acquires a non-blocking pod/GPU-scoped `flock` lease supplied
by the caller, and refuses an already-answering loopback endpoint. There is no
log-directory default: the pod assembler must give every manager for the same
card the same stable lock path. The launcher passes that lock descriptor to
the exact vLLM child, so a controller crash cannot release the lease while its
child remains resident. The default command is
`sys.executable -m vllm serve`: the executable and the inspected installed
distribution therefore come from the same Python environment. That equality is
enforced, not merely defaulted — a supplied `command_prefix` naming a different
interpreter is refused at construction unless the caller also supplies the
`PackageInspector` for the environment it launches. Otherwise the exact package
pin would pass against distributions the engine never imports, and the launch
audit's `runtime_packages.observed` would measure the wrong Python.

The command uses the verified base snapshot; gives the API a stable
`--served-model-name`; and passes the typed profile flags. For a Hugging Face
chair it duplicates the exact commit in `--revision` and
`--tokenizer-revision`, which exist to stop vLLM resolving a *mutable* Hub ref.
A local-repository chair gets neither: it has no Git revision by contract, its
pin is the digest manifest the snapshot was verified against byte for byte, and
naming a revision it does not have would be inventing provenance. That case is
not hypothetical — ARCHITECTURE requires a locally trained checkpoint to be
"*called* like any other model, from its own model repository," which is the
Perlector chair. Adapters are static:
one `--lora-modules` entry, `--max-loras 1`, a configured supported
`--max-lora-rank`, and the verified base source reference. It never calls a
dynamic adapter-update endpoint.

The manager also passes vLLM's `--no-enable-log-requests` hard safety flag.
Golden-page bytes and transcriptions are not serving diagnostics, so a recipe
cannot quietly turn request logging on.

The lease spans endpoint probing, launch, and verified shutdown. It is released
only after the child exits and a bounded `/health` poll observes a definite TCP
connection refusal; a timeout or other ambiguous loopback failure is not proof
of absence. If cleanup cannot prove the owned child and endpoint are gone, it reports
`VLLM_STOP_FAILED` and retains the lease. `recover_failed_start()` can retry only
that same owned cleanup; it cannot launch another chair around it.

## Readiness and adapter proof

Readiness is a bounded poll of the exact child and its fresh launch log. It fails
on an exited child and named `CUDA out of memory`, `EngineDeadError`,
`LORA_UNSUPPORTED` (`does not support LoRA`), `UNKNOWN_MODEL`, or `VLLM_ERROR`
signatures. Of those, `VLLM_ERROR` has no producer today: vLLM never prints it
— it was the old pipeline *wrapper script's* own echo, and this poll reads only
the child's log. It is reserved for a future launch wrapper that writes there,
and claims nothing about vLLM's output. Spec 04 requires a red preflight to carry useful
remediation, and spec 12 requires an operator-facing error to say what happened;
these two adapter failures therefore receive named refusals instead of collapsing
into a watchdog timeout. The old pipeline's launch scripts are historical evidence
for the two strings, not the authority for keeping them. Their broader
`RuntimeError|ValueError` grep is deliberately not carried — this poll re-reads
the whole tail every interval, so one benign line naming either word would abort
a start that was going to succeed. Success
requires all of:

- `/health` HTTP 200;
- a parsed `/v1/models` `data[]` containing the exact served ID; and
- a non-streaming OpenAI-compatible response with that exact model ID and a
  non-blank output.

A bare HTTP 200, a substring such as `reader-api-shadow`, or a routing stub
cannot publish a receipt.

An adapted chair must supply deterministic calibration. The manager sends the
same manager-owned (temperature zero, fixed seed) request to the base and adapter
IDs, then refuses when their semantic-output digests are equal.
`AdapterCalibration.from_image_fixture()` builds a visual calibration only from
local fixture bytes: one non-empty `data:image/...;base64,...` URI with its
SHA-256. Remote, `file:`, blank, malformed, and digest-mismatched images refuse
before launch. `ServingSmokeReader` hashes the local golden fixture again, so the
embedded request bytes and smoke fixture cannot drift. Merely advertising an
adapter in `/v1/models` is never treated as activity proof. A tower/connector
profile additionally requires this image-bearing calibration; a text-only
difference cannot certify that visual path. The sealed calibration request is
rebuilt from canonical bytes, so a later nested-object mutation cannot replace
its image URL. A visual request must place its one image in an actual OpenAI
`chat-completions` `role=user` `messages[].content[]` image block; an ignored
extension field called `image_url` is not visual evidence.

**What this proves, and what it does not.** A base/adapter digest difference is
necessary evidence, not attribution: nothing here runs a base-versus-base control to
establish that the calibration is deterministic on the serving engine at all before a
difference is read as adapter activity, so vLLM-level nondeterminism (continuous
batching, chunked prefill, batch composition) could in principle produce a difference
neither request caused. Symmetrically, a genuinely active adapter that happens to
answer the calibration identically to its base is refused — a real, uncorrupted chair
declined, loudly, never silently. Neither the calibration prompt nor the fixture is
constrained to be discriminative; `calibration_for` is a free caller seam. **Whoever
wires the real rollout must choose a calibration that is known to differ between the
configured base and adapter before launch**, not merely one that is well-formed.

## Receipt and launch audit

`chair-serving-receipt.v1` remains closed. It holds what answered: identity,
revision/manifest, tokenizer revision, seed, context/pixel caps, engine/version,
dtype, base identity when applicable, endpoint, and observed launch time. Its
`pixel_cap` is the **total pixel count** the profile gave vLLM — the third
place this one word appears in two units, after `config/pod_placement.toml`'s
longest-edge cap and a serving profile's `max_pixels`. A receipt's `pixel_cap`
and a placement plan's are not comparable directly; see the capacity check
below for the relation that is sound. It has
no stable API alias, PID, profile/tier, command, full package map, readiness
evidence, or adapter-output proof.

Those fields are a separate `serving-launch-audit.v1`: producer, launch and ready
times, endpoint, served ID, typed profile, PID, explicit model/tokenizer pins,
argv digest, required-and-observed runtime package maps, primary/base identities
and manifests, readiness digests, and adapter activation.
It also carries the exact serving-recipe and placement-file digests that the
sealed context authorized.
`StageContextReceiptPublisher` uses `StageContext.write_serving_receipt` and
writes this audit as a content-addressed stage blob through
`write_serving_launch_audit`. It then writes `serving-evidence.v1`, a
content-addressed manifest linking the receipt and audit references. A manager
start succeeds only when all three immutable references return; a successful
handle cannot silently drop the audit or its linkage. They are exposed on the
`ServiceHandle` and copied as references beside pod smoke evidence.

## Pod seam

`assemble_serving_smoke_reader()` is the narrow production assembly seam. It
reads and validates the local recipe and placement catalogues while constructed;
it does not start a process, open a socket, contact a provider, or load weights.
The caller supplies the run-sealed `StageContext`, its same-context receipt
publisher, the existing registry, page-specific smoke call, calibration function,
the measured `GpuProfile`, and explicit pod/GPU lease. The plain factory binds
that profile into the returned reader, so the documented seam is ready to read
without a caller mutating it afterward. `assemble_serving_preflight_callback()`
builds the `Callable[[], dict[str, object]]` that the existing
`SubprocessBootstrapActions` already accepts, using the existing `PreflightRunner`.
When that callback executes, it creates and verifies the exact local log root
before its default GPU probe measures disk there; construction itself does not
create the directory.

The reader starts one named chair, runs the page-specific smoke request, records
the service evidence, and stops the exact child in `finally`; no healthy-chair
fallback exists. A nominally green smoke result is refused unless the callable
completed at least one `ServiceHandle.request_fixture_image()` during its run:
that method requires the one active OpenAI chat image to hash to the golden-page
fixture. The callback must put the returned response's SHA-256 in
`SmokeResult.receipt["fixture_response_sha256"]`; the reader accepts only if it
names the final successful fixture request, then records manager-owned response
and output digests alongside the fixture SHA-256 and request counts. It retains
no page bytes or response text. The image helper canonicalizes one plain JSON
snapshot before validation and POSTing, so a mutable mapping cannot show the
guard an image then serialize text-only content. The reader refuses, before it can launch, a
serving profile whose dtype is not exactly the one preflight measured (no floor exists for any
other dtype, so this is an exact-match requirement, not a ceiling) or whose capacity —
memory fraction, context length, pixel budget, batch size — exceeds the measured placement
plan.

A smoke result with no GPU/CPU utilization samples makes preflight red with
`utilization-missing`; an empty instrument cannot leave as a green measurement.
The sampler remains the injected responsibility of the page-specific smoke
callable, and no threshold here claims a card is saturated.

**The capacity check knows that `pixel_cap` and `max_pixels` are not the same
unit.** `config/pod_placement.toml` caps a longest edge in pixels; a serving
profile's `max_pixels` is a total count going straight to vLLM. Compared
directly — which is how this arrived — every realistic profile is refused for
busting a plan it fits, since the old pipeline's own proven 2359296 (1536x1536)
is larger than a 1792 side cap. The sound relation is the square, and that is
what is checked. One word carrying two meanings is the real defect; renaming the
placement field belongs to whoever owns that file.

The committed catalogue holds **fixture rows only**, and they carry no flags:
no port, no memory fraction, no context or pixel cap. A fixture profile cannot
be launched, so writing plausible planning numbers for a chair this package
refuses to start would put unbenchmarked figures into a reviewed config file.

The assembly remains caller-injected:
`assemble_serving_preflight_callback` produces the callable
`SubprocessBootstrapActions` accepts. Spec 04's utilization readings remain the
page-specific smoke callable's responsibility; preflight is red when that
callable supplies no samples.

## The golden-page vision smoke callable

`VisionSmokeCall` is the production page-specific callable that seam expects.
It asks the chair for a **page witness** — an unguessable string printed on the
golden page and deliberately absent from the prompt — and requires the answer to
be exactly `PAGE-WITNESS: <witness>`. The lifecycle already proves the request
carried the exact local fixture bytes; this adds the semantic half, that
something on the far side of the wire read them. An answer assembled from the
prompt alone yields the literal `PAGE-WITNESS: <the page witness string>`, is
marked format-invalid, and makes preflight refuse the chair. The receipt records
the resolved identity and revision, the
`served_model_id` the *response body itself* named (`parse_openai_answer` refuses
a response whose `model` is not the exact served alias, so this is per-answer and
not per-connection state), the response digest that binds the receipt to the one
request just made, and `sha256(witness)` — never the witness, the prompt, or the
answer text. The durable smoke record therefore carries only the witness digest.

**Whose job the witness is.** The witness proves a page read only because the
fixture author rendered it into the page's pixels, so that author owns its
entropy, lifetime, and rotation. The caller draws it from a CSPRNG over the
URL-safe ASCII token alphabet. This callable refuses only values that cannot do
the job whatever their origin: outside the 32-to-128-character bound, blank values,
whitespace, non-token characters, or a value present in the prompt. It cannot
measure how a supplied string was generated. A weak or reused witness can be
guessed or memorized and make the smoke falsely green without a page read;
entropy and rotation are preconditions supplied by the fixture author.

The request declares `image/png` and the sealed request bytes are checked as a
complete, decodable PNG under both a 64 MiB encoded-byte ceiling and the
measured placement's pixel bound, because
nothing else on this path inspects them for format — `AdapterCalibration` binds
their digest and `ServingSmokeReader` re-hashes the local fixture, and
`PreflightRunner` takes any `Path`. Checking
the request snapshot matters: reopening the path could validate replacement
bytes after the request was assembled. A non-PNG golden page is refused by name
rather than travelling under a declaration the bytes do not support.

The callable accepts at most 1,024 typed utilization samples for its one request.
That is far beyond the ordinary instrument's needs while keeping a broken sampler
from amplifying one smoke result into an unbounded durable record.

`SmokeResult.nonempty` reports the parsed outputs independently of
`shape_valid`: multiple nonempty choices fail shape and format without falsely
claiming that their text was empty. `parse_openai_answer` already refuses a
blank choice, so a blank answer reaches the runner as `smoke-read-failed`,
earlier and louder than a format flag would be.

**One call at a time.** `ServiceHandle` records its last fixture request on
itself and `ServingSmokeReader` corroborates the returned receipt against that
record after the callable returns, so a handle carries one smoke call at a time.
Two concurrent calls on one handle would cross those records. Nothing reaches
this seam concurrently today — `ServingManager.start` refuses a second start
while a handle is active, and `PreflightRunner` reads its chairs in sequence — so
the precondition is stated rather than locked: a lock inside the handle would not
make the reader's read-after-write atomic, and would advertise a concurrency this
seam does not support.

## Client

`client.ChairClient` is the one client a stage holds for one chair, across
every reading in a pass. It composes an already-built `ServingManager`; it
never starts a pod, never picks between chairs, and never retries, re-samples,
or edits a response (GOVERNANCE 7). Enter it as a context manager — `__enter__`
calls `manager.start` and then re-reads the published receipt back through the
tree (`read_receipt`, production `context.tree.read_run_receipt`), refusing
with `ReceiptDriftRefusal` (`CHAIR_RECEIPT_DRIFT`) and stopping the service
unless the receipt still names this exact chair and revision — nothing is
read from a start whose own record has already drifted. `__exit__` always
stops the handle; a `ServiceStopError` from an unverified shutdown propagates
rather than being swallowed.

`ChairClient.read(ChairRequest) -> ChairResponse` issues exactly one request,
in this order: refuse an unbuildable request (a `kind` other than
`chat-completions`; `generation_sent` naming `model`, `stream`, `temperature`,
`seed`, or `n` — those are the manager's and the decoding policy's alone; an
image whose digest does not match the claimed `image_sha256s`, exactly and in
order) before anything is built or sent; build the body deterministically
(`request_body(..., deterministic=True)` forces temperature 0 and the
profile's seed — this is why the client refuses at construction unless its
caller's `record_temperature` is already 0, so a policy that disagrees is a
named refusal, not a silent override); POST through
`ServiceHandle.request_reading`; refuse before retention if the response is
non-200 or names another model (bytes from the wrong source are not this
chair's evidence); retain the raw response through the caller's `retain`
callable; only then parse content — a content/choices problem becomes
`parse_problem` on the returned `ChairResponse`, never a raised exception,
because a malformed body from a witness or reader is retained evidence, not a
stage abort; and finally write one `chair-call-record.v1` blob (the closed
field set in `common/contracts/serving.CHAIR_CALL_RECORD_FIELDS`, canonical
bytes) before returning.

**The reading parser, against the probe parser.** `http.parse_openai_reading`
is not `parse_openai_answer` reused: a readiness probe must prove the engine
can answer at all, so it refuses blank content. A witness or reader's
legitimate output can be the empty string (`genuinely-empty`), so the reading
parser accepts one choice with `content == ""` and refuses only a missing or
non-string content, never an empty one. Its `finish_reason` is always the
engine's own word, verbatim — missing or `null` become `None`, and nothing
here maps an unrecognized string to anything; that mapping is the stages' job
(§1.6 of the seam spec), not this parser's.

**`request_timeout_seconds`** is a per-profile field (`config.py`,
`config/serving_recipes_real.toml`) because a non-streaming generation of
real length returns nothing until it is done, and the manager's own
readiness/adapter probes stay on their own short, hardcoded budgets — a slow
reading must never be able to stretch those.

`serving_mode_for(recipes, identity, tier)` is the live/fixture selector: a
three-name lookup (`recipe`, `chair`, `tier`) in the sealed serving-recipe
catalogue, never a ranking. Every row for one `(recipe, chair)` is collected
first — if all of them are fixture rows, the chair is fixture regardless of
tier. Otherwise a tier is required (`SERVING_MODE_UNRESOLVED` without one);
the profile at that exact tier decides, with an `UnsupportedProfile` refusing
by its own recorded reason and a fixture row at that tier refused when the
same chair is live at another tier — a catalogue may not be half live for one
chair.

`fakes.py` (`ScriptedAnswer`, `FakeEndpoint`, `FakeLauncher`, `FakeProcess`,
`FakePackages`, `FakeRegistry`, `FakeBlobStore`, `fake_serving_factory`) is a
shared fake endpoint for stage tests built against `ChairClient` — mirrors of
this package's own `test_manager.py` fakes, not moved from there, so that
suite stays untouched. `ScriptedAnswer.finish_reason` takes an explicit
`ABSENT` sentinel distinct from `None`: `None` scripts a JSON `null`, `ABSENT`
omits the key from the wire entirely, and the reading parser treats both the
same way — verbatim absence, never a default. `FakeEndpoint` auto-answers a
manager's one readiness POST (recognized structurally: it is always the first
POST a fresh instance sees, made inside `ServingManager.start` before any
reading is possible) without consuming a scripted answer, so every
`ScriptedAnswer` a test schedules is consumed by an actual `ChairClient.read`
call. Its optional `assert_retained_before_next_request` flag proves
response-as-arrival by construction: when set, the fake refuses a reading
request until the previous one's raw bytes are already on disk in the shared
`FakeBlobStore` — the durability the client's own ordering (retain before
parse) is supposed to guarantee, checked from outside the client rather than
by reading its source.
