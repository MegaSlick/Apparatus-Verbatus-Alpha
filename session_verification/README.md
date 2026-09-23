# Session verification harnesses

`chandra_native_isolation.py` is a private-pod research runner. It is not a
pipeline stage and writes no pipeline artifacts. It performs two separate,
explicit actions:

- `ensure-role-cache` resolves and verifies only the pinned `attestator_1`
  Chandra manifest through `ChairRegistry.ensure`.
- `run` invokes the pinned upstream `chandra.model.vllm.generate_vllm` on the
  one approved page. It defaults to upstream's documented `ocr_layout` prompt
  type and records it in `plan.json`; `--prompt-type` can name an alternate
  upstream mapping deliberately. `--served-model-id` identifies the reviewed
  serving row.
- `serve-and-run` creates a private diagnostic catalogue from
  `config/serving_recipes_real.toml`, changing only Attestator-1's
  `generic-80gb-plus` row to context 20480 and startup budget 600 seconds. It
  starts and stops that unproven row through `ServingManager` with its
  `MECHANICS_QUALIFICATION_PURPOSE`, private receipt publisher, loopback
  readiness POST, process ownership, and pod residency lease before calling
  `run`'s upstream client at the endpoint returned by that exact manager
  handle. The derived endpoint is retained in `plan.json`. This is lifecycle
  evidence, not a pipeline receipt or a claim that the catalogue row is proven.

`run` also requires `--deadline-utc` and `--minimum-attempt-seconds`. Before
each physical page reading, it reserves that minimum interval. If it cannot,
it retains a `reading-NN-hold.json` and stops before issuing that reading.

The runner writes private durable request intents before every page generation,
allows the upstream six-retry generation ladder, caps page readings at seven,
and forces the OpenAI SDK's transport retry count to zero. A timeout or other
ambiguous delivery escapes upstream's broad `Exception` handler and stops the
experiment rather than replaying it. The serving manager's readiness POST is
not a page reading; it is retained in `serving-launch-audit.json` separately
from the page-reading ledger.

A received HTTP body that the SDK cannot parse is retained as
`received-unparseable`; a parsed body whose result shape or repeat detector
cannot classify it is retained as `received-unclassifiable`. If the SDK reports
a response without original byte content, the ledger records
`received-unretained` without claiming a response digest. All three states stop
the native retry ladder after that single physical reading.

Use `config/models-real.toml` for the cache and lifecycle commands. The helper
expects an exact cold-pod checkout of upstream commit
`d4f7467435aa4137d9539f000ddf0b7ced3eb43f`; fetch it only on the pod, outside
Git, and do not install the vendor package or its GPU dependency closure.
`chandra_native_extra_requirements.txt` is the hash-locked import-only CPU
subset demonstrated missing from the frozen application environment. Its three
packages and wheel hashes come from that exact upstream commit's `uv.lock`;
install it after the frozen application sync with `--no-deps --require-hashes`
and `--only-binary=:all:` so an unhashed source archive cannot substitute.
The application already supplies the locked `six` and `typing_extensions`
dependencies. `ServingManager` still validates its configured exact runtime
package pins, while the live setup records the complete installed distribution
set as private research evidence. `serve-and-run` accepts no credentials and
does not put image/request/response bytes in the repository.

On the pod, the intended one-shot interface is:

```
python session_verification/chandra_native_isolation.py serve-and-run \
  --models-config /opt/verbatus/config/models-real.toml \
  --recipes-config /opt/verbatus/config/serving_recipes_real.toml \
  --placement-config /opt/verbatus/config/pod_placement.toml \
  --cache-root /workspace/private/model-cache \
  --diagnostic-root /workspace/private/worker-evidence/native-chandra/diagnostic \
  --evidence-root /workspace/private/worker-evidence/native-chandra/lifecycle \
  --residency-lock /tmp/verbatus-pod-gpu.lock \
  --upstream-root /opt/verbatus/workbench/native-isolation/upstream-chandra \
  --page /workspace/private/recordgold-prepared-pages/2adc37ec376f362ad102_1L.tif \
  --output /workspace/private/worker-evidence/native-chandra/readings \
  --deadline-utc <absolute-utc-deadline> --sdk-timeout-seconds <integer> \
  --minimum-attempt-seconds <SDK-timeout-plus-at-least-30>
```

The caller must use a fresh evidence directory for each invocation. The minimum
attempt interval reserves the explicit SDK timeout plus at least 30 seconds of
closeout. It is checked again immediately before every actual page-reading
request. Each run also retains the exact clean upstream commit receipt and the
resolved OpenAI/Pillow dependency versions; per-reading response files are
original HTTP bytes obtained through the OpenAI SDK raw-response seam.

Every evidence file is file-synced and its parent directory is synced after
exclusive publication or ledger replacement. A directory-sync refusal produces
a visible best-effort `durability-refusal.json` and aborts through a
non-`Exception` control path, so upstream cannot retry a request whose evidence
could not be made durable. `summary.json` records the returned physical attempt
and distinguishes `repeat-exhausted` (the pinned upstream detector still fires
after all seven readings) from a normal received response.

`test_chandra_native_isolation.py` has a compatibility test selected only when
`CHANDRA_UPSTREAM_TEST_ROOT` names a separately fetched clean checkout at the
pinned upstream commit. It installs a fake SDK before importing vendor code and
uses a synthetic image, so it exercises the actual native retry temperatures,
RGB conversion, ambiguous-delivery escape, raw-response retention, and
returned-attempt accounting without a GPU, server, private page, or network
request. A separate fake-manager test exercises endpoint binding, private
lifecycle publication, and unconditional manager stop at the `serve-and-run`
seam.
