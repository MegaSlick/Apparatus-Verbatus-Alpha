# config

The knobs. One question per file, each answerable without reading code.

| File | Question it answers |
|---|---|
| `models.toml` | which model and revision fills each chair on the offline fixture roster of small local stand-ins; also the witness floor and the adapter recipes |
| `models-real.toml` | the same for the real models (`--models-config config/models-real.toml`) |
| `recovery.toml` | how many times one page may be asked again before review; spent only by the Perlector's page re-ask. An operator re-read is a person's act, outside it |
| `hard_failure.toml` | how many counted hard failures a run may carry before it stops, and which outcomes count (the header says why) |
| `review.toml` | what share of pages may stay held after the Recensor before the run is called systemic (`max_held_page_share`) |
| `pdf_render.toml` | the target resolution for rendering a PDF page |
| `data_handling_policy.json` | where real material may be stored, and how it is logged, retained and disposed of |
| `spend.toml` | the money caps for a pod and its volume, and the lead's pod-guard switches |
| `pod_placement.toml` | planning-only GPU tiers, dtype floors and the reviewed price sheet for rented cards |
| `serving_recipes.toml` | the default serving catalogue: fixture rows only |
| `serving_recipes_real.toml` | locked but unproven vLLM profiles for the real chairs, the Designator's two CPU detectors (the record detector in-process, Surya as a subprocess), and `unsupported` rows where no engine fits |
| `serving_recipes_real_variants.toml` | unproven alternative shapes for a real chair (the Perlector on FP8 with MTP or FP8 KV cache, and on a mixed NVFP4/FP8 checkpoint for Blackwell); read by recipe name (`--recipe`), never as a run catalogue |
| `formats.toml` | which Armarium export projections are written, whether verified pixels are embedded, whether rows carry the run's lot |
| `perlector_protocol.toml` | what a whole-page reading is shown (`[feed]`), the render's edges (`[page_context]`), and the truncation instrument (`[truncation]`) |
| `alignment.toml` | the step budget of the Perlector's dissent comparisons |
| `corpus_frame.toml` | how many pages one run (one shard of a corpus) may hold |
| `designator_geometry.toml` | the crop policy for the record detector's records |
| `ink_map.toml` | the ink measurement policy: background, page-spanning bound, connectivity, and the coverage audit's gates |
| `perlector_audit.toml` | the Perlector audit's round cap |
| `page_accounting.toml` | the page accounting's policy: `[inside]`, `[witness_text]`, `[alignment]`, `[identity]`, `[region]`, `[doubt]`, and `[flags]` (review-flag codes that record a finding without holding the page) |
| `reconstruction.toml` | whether the Coniector runs, whether pages are consecutive leaves of one register, and departure bounds |
| `triage_modes.toml` | the triage modes (`manual`, `semi`, `auto`) and their review thresholds |
| `decoding.toml` | each reading chair's maker-recommended sampling, with source and revision; the Perlector's page output cap and repetition-loop guard; the reconstructor's answer cap; Chandra's native recipe |

Beside them:

- `manifests/`: one digest manifest per configured chair (sorted `{path, sha256, size}` rows
  whose canonical bytes a chair's `digest_manifest` names).
- `model-fixtures/`: tiny local snapshots the fixture roster resolves. **These are not
  models**; they stand in for a model repository as `proof/fixtures/` stands in for a
  register. `proof/build_model_fixtures.py` regenerates them, and a test refuses drift.
- `real-models/`: where `models-real.toml` binds Surya's weight bundle. Never committed; on a
  pod it is copied from the model store and verified against
  `manifests/surya2-detection.json` (`operations/serving/surya/README.md`).

## Sealed configuration

A policy that shapes a run is **read once, parsed, sealed into the run, and required by its
seal at every point of use.** A TOML file's seal is the SHA-256 of its parsed table as
sorted-key JSON (`common/sealed_config.py::read_sealed_toml`), so comments, blank lines and
key order can change freely and any value change moves the seal. `run.json` records
`sealed_config_method = "toml-sorted-json.v1"`; a run without it is refused as a seal-method
change. `data-handling` (JSON) is a digest of raw bytes; the real-ingress `models` and
`run-policy` are canonical digests of parsed records.

Every seal is recorded by name in the run authority's `sealed_config_digests`, and most also
enter `run.json`'s `config_digest` (`review` does not). Reopening a run id refuses a change to
either, before anything is written. `common/sealed_config.py::require_sealed_config` is the
point-of-use check; a stage asks through its `StageContext`, the orchestrator through the run
authority. Policies a stage needs the values of arrive parsed (`StageContext.recovery_policy`,
`StageContext.armarium_formats`).

The sealed names (`common/stage.py::run_config_bindings`, `real_run_bindings`):

- **every run:** `designator-geometry`, `alignment`, `page-accounting`, `reconstruction`,
  `ink-map`, `corpus-frame-shard`, `decoding`, `perlector-protocol`, `perlector-audit`,
  `pdf-render`, `recovery`, `hard-failure`, `review`, `triage-modes`;
- **real ingress adds:** `data-handling`, `serving-recipes`, `pod-placement`, `models`,
  `armarium-formats`, `run-policy`, and `canary-ledger` when the run has canary pages. A real
  run's `config_digest` binds the submission ledger and the door's decoders, so stages
  recheck these names individually.

`spend.toml` and the triage instrument declaration are not sealed into a run; the spend
display and triage records carry a digest of their raw bytes. `hard-failure` is read before
the run exists (the orchestrator needs it to decide whether a resumed run may re-enter a
stage) and proved against the run authority as soon as one exists.

## Witness routing

A roster may seat a witness on some pages only, with a `[witness_routing]` table
(`attestator_4 = "index-and-table.v1"`: dots.mocr on pages Surya tags a table or the record
detector finds no record on; `pipeline/3_attestatores/CONTRACT.md`, "Witness routing").
Neither committed roster has one, so routing is off. Seating dots.mocr on the real roster
needs all of:

- a fetched, verified `dots-studio/dots.mocr` at `e539fbb52280393adc081b289ec597430a0f9031`
  with its digest manifest, an `[chairs.attestator_4]` row (`witness_adapter =
  "dots-mocr.v1"`, `witness_scope = "page"`) and the `[witness_routing]` line in
  `models-real.toml`;
- an `attestator_4` entry in `REQUIRED_ARTIFACTS` (`common/chairs/model_store.py`), which the
  pod's MODEL_STORE step fetches and verifies;
- an `attestator_4` row at every tier in `serving_recipes_real.toml` (vLLM with
  `trust_remote_code`, room for 16,384 answer tokens);
- a `[chair_decoding.attestator_4]` row in `decoding.toml` (temperature 0.1, top_p 1.0);
- the prompt's token count measured with the pinned tokenizer in
  `common/request_capacity.py`. Until then a served dots.mocr request is refused by name.

The root `conftest.py` (`dots_models_config`) builds the fixture version for the tests.

## Notes on particular files

**`models*.toml`.** Model assignments live here, never in stage code or stage documents. A
roster also sets the witness floor, the adapter recipes and, with the fixture and scenario,
the run's configuration digest. `common/chairs/README.md` describes how a roster is read.

**`serving_recipes*.toml`.** A catalogue does not name a model, choose a chair or estimate
fit. A roster's `serving_recipe` is a family key; the serving manager requires exactly one
profile for `(serving_recipe, chair, measured placement tier)`, with no fallback. Every
capacity value is a planning value until that identity, revision and profile pass a real pod
preflight. Every row declares its `kind`; a `fixture` row holds only recipe, chair, tier and
a reason, and is never launched, so no real chair serves by default.
`operations/serving/config.py::verify_recipes_cover_chairs` reconciles a catalogue with its
roster and `pod_placement.toml` offline. A `vllm` row may add reviewed engine options:
`quantization` (`"fp8"`, or `"modelopt_mixed"`; the snapshot's `config.json` must declare the
matching method), `kv_cache_dtype` (`"fp8"`), and `speculative_config` (`method = "mtp"`,
`num_speculative_tokens` 1 to 8). They are appended after every other flag and count as
launch fields for a shared service.

**`pod_placement.toml`.** Planning, not permission. Serving is sequential, so every tier is
single-resident and sets the engine memory fraction, context cap, pixel cap and batch size.
`card_profile` rows are prebuilt plans for rented cards with the reviewed hourly price the
launch gate estimates against; an unknown card falls back to computed placement. A tier's
`planned_batch_ceiling` caps the capacity plan: PREFLIGHT may widen a row's `max_num_seqs` up
to it (64 when unset), never below the row's own value. `pixel_cap` is a longest edge; a
serving profile's `max_pixels` is a total pixel count; the two are never compared. No number
here has been benchmarked on real hardware. Its digest is sealed into run trees, so do not
edit prices casually.

**`spend.toml`.** `currency = "USD"`; ceilings for the combined pod and volume hourly price
(`max_hourly_usd`) and the estimated cost through the hard lifetime
(`max_estimated_metered_cost_usd`); `hard_lifetime_seconds`; `billing_cutoff_margin_seconds`
(0–3600); the laptop heartbeat and shutdown polling and deadline; the
`account_balance_floor_usd` reserve and higher `account_balance_alert_usd`. The loader
refuses an unknown or missing key, and a configured policy is not permission to launch. Two
switches are the lead's, both committed `"off"`:

- `pod_budget = "on"` arms `soft_max_seconds`/`soft_max_cost_usd` and
  `hard_max_seconds`/`hard_max_cost_usd` (whichever of time or cost comes first). The guard's
  deadline sits at the soft maximum, so a soft value above its hard one, or a lifetime or
  cost ceiling above the soft maximum, is refused. Going past the soft maximum is an
  extension only the lead makes, bounded by the hard maximum. Off, the maximums bind nothing
  and a pod gets no deadline unless started with a number of hours.
- `ladder_delete = "on"` lets the pod guard delete a pod idle for two hours once its run is
  backed up; off, the idle ladder only warns (`operations/pod/README.md`).

The balance floor is a policy value, not an observation. The policy does not decide whether
a volume is kept after a close.

**`data_handling_policy.json`.** Names the storage roots real material may occupy;
`operations/submit/gate.py` refuses a submission folder, run root or ledger outside them
before a byte is read. The Exemplar door seals the digest of the policy it read under
`data-handling`. Both entry points take the policy path as a flag, so this is
tamper-evidence, not access control. For a real submission the submit door writes a
self-hashed filename ledger before any transfer; the Exemplar door binds its rows into
`run.json`, and the export carries the linkage out. No per-stage deletion: a run is kept
whole until abandoned or exported.

**`pdf_render.toml`.** Full-page PDFium rasterisation (text, vectors, annotations and images
together), never embedded-image extraction. `--pdf-target-dpi` overrides it for one run. The
72-DPI floor, pixel ceiling and decoded-byte ceiling are in code. Every rendered page records
the configured and bounded targets beside its `effective_dpi`.

**Admission and decoding are not configuration.** An uncorrupted image is never declined by
policy: every raster gets a decoder attempt and is sealed unchanged (or fanned out when it has
several frames), and a PDF is always painted page by page. `pipeline/1_exemplar/admission.py`
derives that route map from what the byte sniffer can name. An undecodable format variant is
a named pipeline alarm, not a routine rejection.

**`alignment.toml`.** The dissent budget counts the matcher's work instead of timing it, so a
comparison's outcome depends only on its texts and the budget. A comparison past it is
recorded as not measured.

**The Perlector's instruction is not a knob.** It is pinned in `common/page_prompt.py`
(`TRANSCRIBE_SENTENCE`, `DOUBT_SENTENCE`, `ANSWER_FORM`, with the `[[?]]` and
`[[reading|other]]` doubt marks). What a page call is shown is `perlector_protocol.toml`'s
`[feed]`.

**`ink_map.toml`.** Sealed as `ink-map`: `[background]` (`common/background.py`),
`[page_spanning]`, `[connectivity]`, and `[coverage_audit]` with its `noise_floor`
(`common/residual_ink.py`). Every record measured under it names its digest. The
coverage-audit gates are not calibrated (`calibrated_for_this_corpus = false`).

**The triage instrument.** `operations/triage/instrument.toml` is the triage producer's own
declaration, not run configuration. Every threshold is `UNMEASURED`, and no value authorizes
an automatic link. Its digest goes into the producer's recipe, which the door binds under
`triage_document_digests` (`pipeline/0_triage/CONTRACT.md`).
