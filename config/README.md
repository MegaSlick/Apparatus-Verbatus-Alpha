# config

The knobs. One question per file, each answerable without reading code.

| File | Question it answers |
|---|---|
| `models.toml` | which model and revision fills each chair, on the offline fixture roster of small local stand-ins; also the witness floor and the adapter recipes |
| `models-real.toml` | the same for the real models, selected with `--models-config config/models-real.toml` |
| `recovery.toml` | how many times one page may be asked again before review; sealed into every run and spent only by the Perlector's page re-ask. An operator re-read is outside it: it is a person's act, numbered from attempt 3, and is never re-asked by the machine |
| `hard_failure.toml` | how many counted hard failures one run may carry before it stops, and which stage outcomes count; the file's header says why each is counted or not |
| `review.toml` | what share of a run's pages may stay held after the Recensor before the run is called systemic (`max_held_page_share`, 1 in 50); the held-Recensor stop, its notification and an export a person advanced past it say so when the share is exceeded |
| `pdf_render.toml` | the target resolution for rendering a PDF page |
| `data_handling_policy.json` | where real material may be stored, and how it is logged, retained and disposed of |
| `spend.toml` | the money caps for a pod and its attached volume; every paid action refuses an unconfigured policy, and a configured one is not permission to launch |
| `pod_placement.toml` | planning-only GPU resource tiers, dtype floors, and the reviewed price sheet for the cards this project rents |
| `serving_recipes.toml` | the default serving catalogue: fixture rows only, used unless `--serving-recipes-config` selects another file |
| `serving_recipes_real.toml` | locked but unproven vLLM profiles for the real chairs, the CPU rows of the Designator's two detectors (the record detector in-process, Surya as a subprocess), and explicit `unsupported` rows where no engine fits; selected with `--models-config config/models-real.toml --serving-recipes-config config/serving_recipes_real.toml` |
| `formats.toml` | which Armarium export projections are written, whether verified pixels are embedded, and whether rows carry the run's lot |
| `perlector_protocol.toml` | what one whole-page reading is shown (`[feed]`), the page render's edges (`[page_context]`), and the truncation instrument's length floor and legibility gate (`[truncation]`) |
| `alignment.toml` | the step budget of the Perlector's dissent comparisons |
| `corpus_frame.toml` | how many pages one run (one shard of a corpus) may hold |
| `designator_geometry.toml` | the crop policy for the record detector (`secondary_proposer`): the Designator cuts every detector record's crop under it, and the record reader reads those crops |
| `ink_map.toml` | the ink measurement's policy: background inference, the page-spanning bound and connectivity radius, and the outside-coverage audit's gates |
| `perlector_audit.toml` | the Perlector audit's round cap; every page reading records the audit as not run |
| `page_accounting.toml` | the page accounting's policy: when a box counts as inside the reading regions (`[inside]`), how much witness text a reading may leave unaccounted for or set aside (`[witness_text]`), the text alignment's anchors and bounds (`[alignment]`), what makes a unit's text distinctive (`[identity]`), and when two entries claim one region or a unit's box is too large for its text (`[region]`) |
| `reconstruction.toml` | whether the Coniector runs, whether the submitted pages are consecutive leaves of one register, and the bounds past which a departure is not applied |
| `triage_modes.toml` | the three triage modes (`manual`, `semi`, `auto`) and their review thresholds |
| `decoding.toml` | each reading chair's sampling values as its makers recommend them, with source and revision; the Perlector's whole-page output cap and repetition-loop guard, and the reconstructor's answer cap; and Chandra's native recipe |

Beside the rosters:

- `manifests/` — one digest manifest per configured chair: the sorted
  `{path, sha256, size}` rows whose canonical bytes a chair's `digest_manifest` names.
- `model-fixtures/` — the tiny local-repository snapshots the offline fixture roster
  resolves. **These are not models.** They stand in for a model repository exactly as
  `proof/fixtures/synthetic-two-page-v0/*.png` stand in for a scanned register.
  `proof/build_model_fixtures.py` regenerates them and their manifests; a test refuses
  any drift between them.
- `real-models/` — where `models-real.toml` binds its local-repository chair, Surya's
  weight bundle. Never committed: on a pod, the bundle is copied in from the model store
  and verified against `manifests/surya2-detection.json`
  (`operations/serving/surya/README.md`).

## Sealed configuration

A policy that shapes a run is **read once, parsed, sealed into the run, and required
by its seal at every point of use.** The seal of a TOML file is the SHA-256 of its
parsed table written as sorted-key JSON (`common/sealed_config.py::read_sealed_toml`):
what the file says, not how it is written. Comments, blank lines and key order are
free to edit and move no seal; any value change moves it. `run.json` records the
scheme as `sealed_config_method = "toml-sorted-json.v1"`. `data-handling` (JSON) is a
digest of raw bytes, and the real-ingress `models` and `run-policy` are canonical
digests of parsed records. A run without the tag is refused by name as a seal-method
change, never as drift.

Every seal is recorded by name in the run authority's `sealed_config_digests`, so a
reader holding only the run tree can name the policy that governed the run. Most seals
also enter `run.json`'s `config_digest` (`review` does not). Reopening a run id refuses a
change to either, before anything is written.

`common/sealed_config.py::require_sealed_config` is the point-of-use comparison. A stage
asks through its `StageContext`; the orchestrator, which is not a stage, asks the run
authority directly. A policy a stage needs the values of is carried already parsed
rather than reopened: `recovery.toml` as `StageContext.recovery_policy` (read through
`common/page_reask.py::reask_budget`), `formats.toml` as
`StageContext.armarium_formats`.

The sealed names (`common/stage.py::run_config_bindings`, `real_run_bindings`, and the
door's real bindings):

- **every run:** `designator-geometry`, `alignment`, `page-accounting`,
  `reconstruction`, `ink-map`, `corpus-frame-shard`, `decoding`, `perlector-protocol`,
  `perlector-audit`, `pdf-render`, `recovery`, `hard-failure`, `review` and
  `triage-modes`;
- **real ingress adds:** `data-handling`, `serving-recipes`, `pod-placement`, `models`,
  `armarium-formats` and `run-policy`, and `canary-ledger` when the run has canary
  pages. A fixture run's `config_digest` covers these facts and a later stage can
  recompute it; a real run's `config_digest` binds the submission ledger and the door
  machine's decoders, so stages recheck these names instead.

`spend.toml` and the triage instrument declaration are not sealed into a run: the spend
display and the triage records carry a digest of their raw bytes instead.

`hard-failure` is read before the run exists, because the orchestrator needs the
threshold to decide whether a resumed run may re-enter a stage. It is held for the whole
invocation and proved against the run authority at the first moment one exists: the
resume preflight, or, on a first run, the Door, which seals it from the same read.

## Notes on particular files

**`alignment.toml`.** The dissent budget (100,000,000 steps) counts the matcher's work
instead of timing it, so whether a comparison finishes depends only on its texts and
the budget, never on the machine. It is sized for act-length text; a comparison past it
is recorded as not measured.

**The Perlector's instruction is not a knob.** The page instruction
(`common/page_prompt.py`: `TRANSCRIBE_SENTENCE`, `DOUBT_SENTENCE` and `ANSWER_FORM`,
with the `[[?]]` / `[[reading|other]]` doubt marks) is pinned in code and carried in
every request a page reading binds. What one page call is shown is the `[feed]` table of
`perlector_protocol.toml`.

**Admission and decoding are not configuration.** An uncorrupted image is never
declined by policy, and there is one route map: every raster gets a decoder attempt and
is sealed unchanged, or fanned out when it has more than one frame; a PDF is always
painted page by page. `pipeline/1_exemplar/admission.py` derives that map from the
formats the byte sniffer can name, so a new format cannot route by omission. A format
variant the installed readers cannot decode is a named pipeline alarm carried with its
filename, not a routine rejection.

**`pdf_render.toml`.** PDF rendering is full-page PDFium rasterisation, which paints
text, vectors, annotations and images together; it is never embedded-image extraction.
`--pdf-target-dpi` overrides the target for one run. The run authority records the
configured and the code-bounded target, and every rendered page records them beside its
`effective_dpi`. The 72-DPI floor, the pixel ceiling and the decoded-byte ceiling are in
code; configuration cannot weaken them. The default target is unmeasured against real
material. The door reads the file once and seals that one read.

**`data_handling_policy.json`.** It names the storage roots real material may occupy,
and `operations/submit/gate.py` refuses a submission folder, run root or ledger outside
them before a byte is read. The Exemplar door reads the caller-named policy once, gates
the submission on it, and seals the digest of those bytes under `data-handling`, so a
run can be reconciled against the exact policy that admitted it. Both entry points take
the policy's path as a flag, so this is tamper-evidence, not access control. For a real
submission the local submit door writes a self-hashed filename ledger before any
transfer; the Exemplar door requires it and binds its rows into `run.json`, and the
export carries the same linkage out. The policy permits no per-stage deletion: a run is
kept whole until it is abandoned or exported, then its volume may be destroyed whole.

**`models.toml` and `models-real.toml`.** Model assignments live here, never in stage
code or stage documentation. A roster also sets what follows from it: the witness floor,
the adapter recipes, and, with the fixture and scenario, the run's configuration digest.
`common/chairs/README.md` describes how a roster is read and what a malformed pin earns.

**`serving_recipes*.toml`.** A catalogue does not name a model, choose a chair or
estimate that a model will fit. A roster's `serving_recipe` is only a family key; the
serving manager requires exactly one profile for `(serving_recipe, chair, measured
placement tier)`, with no nearest-tier or healthy-chair fallback. Every capacity value is
a planning value until that exact identity, revision and profile has passed a real pod
preflight. Every row declares its `kind`. A `fixture` row holds only its recipe, chair,
tier and a reason, and the serving manager refuses to launch one. Every row in
`serving_recipes.toml` is a fixture row, so no real chair serves by default.
`operations/serving/config.py::verify_recipes_cover_chairs` reconciles a catalogue with
its roster and `pod_placement.toml` offline, so a chair, recipe or tier nothing could
resolve fails in the test suite rather than on a pod.

`pod_placement.toml`'s `pixel_cap` caps a longest edge in pixels, while a serving
profile's `max_pixels` is a total pixel count passed to vLLM; the two are never compared
directly.

**`spend.toml`.** It names `currency = "USD"`, ceilings for the combined pod and volume
hourly price (`max_hourly_usd`) and for the estimated cost through the hard lifetime
(`max_estimated_metered_cost_usd`), the `hard_lifetime_seconds`, a bounded
`billing_cutoff_margin_seconds`, the laptop heartbeat and the shutdown polling and
deadline, an observed `account_balance_floor_usd` hard reserve and a higher
`account_balance_alert_usd` notification threshold. Two switches, each `"on"` or
`"off"`, are the lead's: `pod_budget` and `ladder_delete`, both committed `"off"`.
`pod_budget = "on"` arms a pod's budget: `soft_max_seconds` and `soft_max_cost_usd`,
`hard_max_seconds` and `hard_max_cost_usd`, fixed values in time from creation and in
metered cost, whichever is reached first. The guard's deadline then sits at the soft
maximum, so the loader refuses a soft value above its hard one and a
`hard_lifetime_seconds` or `max_estimated_metered_cost_usd` above the soft maximum.
`pod_run` sends one `deadline-at-risk` notice ahead of time, when a stage's projected
finish passes the deadline that ends the pod; going on past the soft maximum is an
extension only the lead makes, and the hard maximum bounds it. With `pod_budget = "off"`
the four maximums may stay in the file but bind nothing and are not required: a pod gets
no guard deadline and no backstop unless the lead starts it with a number of hours, and
`pod_run` reports "budget off (lead's choice)". `ladder_delete = "on"` lets the pod
guard delete a pod that has shown no work for two hours, once its run tree is backed up
on the volume; off, the guard's idle ladder only warns (`operations/pod/README.md`). The
hourly, metered-cost, balance and lifetime ceilings govern every launch either way, and
the lease route's pod timer stays bounded by `hard_lifetime_seconds`. The committed values
are the lead's: `max_hourly_usd` $2.10, `max_estimated_metered_cost_usd` $5.00, and, for
when the budget is on, a soft maximum of 2 h or $5.00 and a hard maximum of 3 h or $7.00.
A paid action reads the available
balance through the provider's explicitly configured source and refuses when that source
is unavailable or the action would breach the reserve. The `$50.00` floor is a policy
value, not a balance observation, until checked against RunPod before a live run. The loader refuses an unknown or missing key. The policy does not
decide whether a volume is kept or deleted after close; every close report states the
volume's ongoing price.

**`pod_placement.toml`.** Planning, not permission. Serving is sequential — one model at a
time, with as much of the card as stays stable — so every tier is single-resident, and a
tier sets the engine memory fraction, context cap, pixel cap and batch size that model
gets. Its `card_profile` rows are prebuilt plans for the cards this project rents, with
the reviewed hourly price the launch gate estimates against; an unknown card falls back
to computed placement. Naming a card here does not choose one, and no number here has
been benchmarked on real hardware.

**`ink_map.toml`.** Sealed as `ink-map`: `[background]` (`common/background.py`),
`[page_spanning]` and `[connectivity]`, and `[coverage_audit]` with its `noise_floor`
(`common/residual_ink.py`). Each table carries its own provenance. The Ink Map, the page
accounting, the Recensor and the Armarium read it, and every record measured under it
names its digest. The coverage-audit gates are not calibrated for this corpus
(`calibrated_for_this_corpus = false`).

**The triage instrument.** `operations/triage/instrument.toml` is the triage producer's
own declaration, not a run configuration: there is no run when the producer executes.
Every threshold in it is marked `UNMEASURED`, and no value authorizes an automatic link.
The producer writes its recipe, including this file's digest, beside its decision
manifest, and the door binds that recipe under `triage_document_digests`. What a
near-duplicate verdict does and does not mean is in `pipeline/0_triage/CONTRACT.md`.
