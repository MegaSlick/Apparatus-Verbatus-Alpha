# config

The knobs. One question per planned file, each answerable without reading code.

| File | Status and question |
|---|---|
| `models.toml` | which model and revision fills each numbered role |
| `recovery.toml` | how many times rework may be asked for before review |
| `hard_failure.toml` | how many accounted hard failures one run may carry before it stops; the threshold and the outcome taxonomy are both Tyrel's rulings (see the file's own header) |
| `pdf_render.toml` | what whole-page PDF resolution the next run targets |
| `designator_padding.toml` | how far a proposal crop is expanded past its structural bounds before it is cut |
| `data_handling_policy.json` | how real material is stored, logged, retained and disposed of |
| `spend.toml` | deliberately unconfigured — Tyrel's pod-plus-attached-volume money caps; both paid paths refuse it until configured |
| `pod_placement.toml` | planning-only single-resident GPU resource tiers, dtype capability floors, and the reviewed price sheet for the cards this project rents |
| `serving_recipes.toml` | the fixture-only default serving catalogue; it stays untouched unless `--serving-recipes-config` selects another file |
| `serving_recipes_real.toml` | unproven, locked real-chair vLLM profiles; selected only with `--models-config config/models-real.toml --serving-recipes-config config/serving_recipes_real.toml` |
| `formats.toml` | which Armarium product projections are written and whether verified pixels are embedded |
| `perlector_protocol.toml` | the sealed prior-draft protocol: Pass-B neutral fragment, page-shared-prefix policy, and control selection-rule name |
| `alignment.toml` | sealed character, pair, and wall-clock ceilings for witness-to-Chandra alignment |
| `corpus_frame.toml` | R0's sealed shard boundary: how many pages one bounded failure and accounting unit may hold |
| `designator_geometry.toml` | the sealed tiling and crop-policy geometry the Designator's proposal adapters are built against |
| `perlector_audit.toml` | the sealed Pass-C audit policy: flag classes and the round cap the audit refuses to exceed |
| `witness_context.toml` | the factual per-witness context the Perlector's dossier may carry: identity, provenance, training domain, and nothing evaluative |
| `triage_modes.toml` | the three pipeline-wide triage modes and their closed-ordinal review thresholds |
| `decoding.toml` | temperature-zero record readings and the labelled variance experiment's seed and pass count |

## R4 toggle register

| Knob | Default | Who changes it | What retires it |
|---|---|---|---|
| alignment character/pair/deadline limits | 100,000 / 100,000,000 / 5 seconds | ordinary engineering with recorded measurement | a replacement bounded aligner with recorded benchmark evidence |

## R5a toggle register

| Knob | Default | Who changes it | What retires it |
|---|---|---|---|
| `--draft-fed` | fed | Tyrel through B5a | a recorded B5a decision replacing draft-feeding |
| `--perlector-instrument-per-mille` | 0 | Tyrel, with `--perlector-instrument-approval-ref` | a replacement approved instrument design |
| Perlector protocol selection-rule name | `digest-threshold-over-frame-page-seed-act.v1` | ordinary engineering with recorded evidence | a replacement rule recorded with its coverage evidence |
| Perlector protocol Pass-B fragment | the neutral form (iterative_reader.md:49-50) | **not a knob** — pinned to `protocol.PASS_B_FRAGMENT`; rewording is a reviewed two-file change | a B5a prompt-framing ablation Tyrel records, which retires the pin rather than edits around it |

The Pass-B fragment sits in `perlector_protocol.toml` so its exact bytes seal
into every run, not so a run may choose them. It is pinned in code because a
free-text field there would leave GOVERNANCE 3 and GOVERNANCE 10's "the
instrument may not constrain what it measures" resting on a phrase blacklist
— measured before the pin, one that accepted "The prior reading contains
errors. Find and fix them." and "Rate your confidence no higher than medium."

Decoder routing is deliberately not configuration. Tyrel ruled that an uncorrupted
image is never declined by policy, and there is exactly one valid route map: every
raster gets a decoder attempt and is sealed unchanged or fanned out when it has more
than one frame; PDF is always painted page by page. `admission.py` derives that map
from the formats the byte sniffer can name, so a new format cannot route by omission.
A format/variant the installed readers cannot yet decode is a named pipeline alarm
carried with its filename, rather than a routine rejection.

PDF alone uses `render-pages`, and the loader refuses any other format given that
action. PDF is full-page PDFium rasterisation, which paints text, vectors,
annotations, and images together; it is never embedded-image extraction. TIFF is
`admit-or-fan-out`: a single-directory scan seals its own bytes untouched, and a
multi-page one — including the LZW, Deflate, PackBits and CCITT compressions real
flatbed scanners produce — fans out to one ordinal per page. JPEG suffix bytes after
EOI are not called corruption.

`pdf_render.toml` supplies the documented default target for whole-page PDF
rasterisation. `--pdf-target-dpi` overrides it for one run. The run authority records
the configured target and the code-bounded target, and every rendered PDF page records
those beside its `effective_dpi`. The 72-DPI floor, pixel ceiling, and decoded-byte
ceiling remain in code; configuration cannot weaken them. The default is **unmeasured**:
making it adjustable does not prove it suitable, and it should be checked against a
real sample of real material (GOVERNANCE 9).

The door reads this file exactly once and parses and hashes the same bytes
(`render_config.load_pdf_render_binding`). It used to resolve the settings and then
let the binding step open the file again, so a rewrite between the two reads left a
run whose `render_settings` recorded one target while its `config_digest` bound
another — a run claiming a configuration it did not execute.

`designator_padding.toml` is the asymmetric capture-padding policy
`pipeline/2_designator/geometry.py` applies to a structural proposal before
cutting it: top/bottom/left/right, in integer basis points of the crop's own
width or height, clamped to the page edge. It is bound into `run.json`'s
`config_digest` exactly as `pdf_render.toml` and `recovery.toml` are, so
reusing a run id across a padding change is refused before anything is
written — the crops would otherwise be different pixels under the same run's
name. Every crop's own payload additionally carries the exact fraction and
pixel amount applied, the file's digest, and the file's declared provenance,
so a padding change is traceable per artifact as well as per run.

`data_handling_policy.json` names the storage roots real material may occupy, and
`operations/submit/gate.py` refuses a submission folder, run root or ledger outside
them before a byte is read. **It no longer names an approval**: Tyrel's ruling of
2026-08-09 cut the per-run approval record, and with it the policy-version hash that
made an approval stale when the policy changed.

**The run does bind the policy that governed its admission.** The Exemplar door
reads the caller-named policy once, gates the submission on that record, and seals
the digest of those same bytes into `config_digest` and into the run authority's
`sealed_config_digests` under `data-handling`. So a run can be reconciled against
the exact policy document that admitted it, rather than against whichever file now
sits at the default path, and reusing a run id across a policy change is refused
before a byte is written. This is provenance and tamper-evidence, not a revived
approval: nothing refuses a submission for want of a sign-off, and the per-run
approval record stays cut.

Both entry points expose the policy's path as a flag, so "the current policy" is
whichever file the invoker names — a documented limit, and the reason this is
tamper-evidence rather than access control. What the run now settles is *which*
file that was.

For real submissions, the local submit door writes a self-hashed filename ledger
before any transfer. The Exemplar door requires that ledger and binds its filename,
digest, byte-count, and fanned-page-index rows into `run.json`; an export carries
the same linkage back out. The policy permits no per-stage deletion: retain the whole run until it is
dead/broken or complete/exported, then its lifecycle owner may destroy the whole
volume. See `operations/submit/README.md` for the package being handed to Tyrel;
the transfer and pod runtime live under `operations/`, while UI work is not built.

`models.toml` is the operational cast list. Model assignments belong there rather
than in stage code or stage documentation, which keeps a swap to one configuration
change. It also owns the three things a run is bound to that follow from the
roster: the witness floor, the adapter recipes, and — with the fixture and the
scenario — the run's configuration digest. `common/chairs/README.md` describes how
it is read and what a malformed pin earns.

`serving_recipes.toml` does not name a model, choose a chair, or estimate that a
model will fit. `models.toml`'s `serving_recipe` is only a family key; the serving
manager requires exactly one profile for the triple `(serving_recipe, chair,
measured placement tier)`. There is no nearest-tier or healthy-chair fallback.
Every capacity value is a planning value until that exact identity/revision/profile
has completed a real pod preflight. Its exact bytes are included in a run's
`config_digest`, alongside the exact `pod_placement.toml` bytes, so changing a
serving flag, tier threshold, or cap cannot silently reuse a run authority. The
serving assembly receives those two run-sealed digests only through the active
stage context and records them in its launch audit.

Every row declares its `kind`. A `fixture` row is the offline walking skeleton's
stand-in: it holds only its recipe, chair, tier and a reason, because a chair
that is never launched has no flags, and the serving manager refuses one by that
name rather than by a version pin it could not satisfy. Today every committed
row is a fixture row, and a `vllm` row appearing here would mean a real chair had
been configured to serve. That is not a config edit: it needs the real roster
uncommented in `models.toml`, a verified manifest per chair, and reviewed,
locked vLLM and model-stack versions proven on real silicon.
`operations/serving/config.py::verify_recipes_cover_chairs` reconciles the selected file
against `models.toml` and `pod_placement.toml` offline, so a chair, recipe or
tier that nothing could resolve fails in the test suite rather than on a pod.

The two files use `pixel_cap` and `max_pixels` for different things and must not
be compared directly: `pod_placement.toml` caps a longest edge in pixels, while
a serving profile's `max_pixels` is a total pixel count passed to vLLM. Both
files say so where the value is defined.

`spend.toml` is intentionally a refusal, not a placeholder default. A configured version
must name `currency = "USD"`, `max_hourly_usd` and `max_estimated_metered_cost_usd`
ceilings for the combined metered pod and attached-volume hourly price and cost through
the hard lifetime, the `hard_lifetime_seconds` itself, plus a bounded
`billing_cutoff_margin_seconds`, laptop heartbeat, and shutdown polling/deadline. It also
names an `account_balance_floor_usd` manual reserve: the runtime does not observe account
balance, and the documented `$50.00` default is unverified until checked against RunPod
before a live run. The loader refuses any key it does not know and any policy missing one
of these.
It does not authorize retaining or deleting a volume after close: that is a separately
named decision, and every close report states the volume's own ongoing price. The file
itself carries the full key list as comments, so filling it in needs no code reading.

`pod_placement.toml` is planning, not permission. Serving is **sequential** — one model
at a time, as much of the card as stays stable, next model after — so every tier is
single-resident, and what a tier changes is the engine memory fraction, context cap,
pixel cap and batch size that one model gets. Its `card_profile` rows are prebuilt plans
for the cards this project actually rents; an unknown card falls back to computed
placement from the generic tiers. Those rows also carry the reviewed hourly price the
launch gate estimates against, because RunPod publishes no endpoint that quotes a GPU's
price without creating a pod. Naming a card here does not choose one, and no number here
has been benchmarked on real silicon.

Two directories sit beside `models.toml` because they are resolved relative to it,
and could not be pinned by it from anywhere else:

- `manifests/` — one digest-manifest artifact per configured chair: the sorted
  `{path, sha256, size}` rows whose canonical bytes a chair's `digest_manifest`
  names.
- `model-fixtures/` — the tiny local-repository snapshots the offline walking
  skeleton resolves. **These are not models.** They stand in for a model
  repository exactly as `proof/fixtures/synthetic-two-page-v0/*.png` stand in for
  a scanned register. `proof/build_model_fixtures.py` regenerates both directories
  and prints the pins; a test refuses any drift between them.

## Sealed configuration

A policy that shapes a run is **read once as bytes, parsed and hashed from those same
bytes, sealed into the run, and required by digest at every point of use.** Sealing
means two things together: the digest goes into `run.json`'s `config_digest`, so
reusing a run id across a change is refused before anything is written; and it is
recorded by name in the run authority's `sealed_config_digests`, so a reader holding
only the tree can *name* the policy bytes that governed the run instead of merely
testing a candidate file against one hash of everything.

`common/stage.py::require_sealed_config` is the point-of-use comparison. A stage asks
through its `StageContext`; the orchestrator, which is not a stage, asks the run
authority directly. A name that is sealed has a point of use that requires it, and a
policy a stage needs the *values* of is carried already parsed rather than reopened —
`recovery.toml` travels as `StageContext.recovery_policy`, `formats.toml` as
`StageContext.armarium_formats`.

Sealed names today: `designator-padding`, `designator-geometry`, `alignment`, `decoding`,
`corpus-frame-shard`, `perlector-protocol`, `perlector-audit`, `pdf-render`,
`recovery`, `hard-failure`, and — on real ingress only, because the fixture route is
not gated — `data-handling`. `triage-modes` is likewise sealed into every run; Unit 6's
pre-door producer/door seam must call `require_triage_modes` before using its vocabulary.

## Pre-door triage instrument

`operations/triage/instrument.toml` is the deterministic producer's own declaration,
not a run configuration. Its proxy edges, grids, offsets, candidate window, prefilter,
and every comparison threshold are each marked `UNMEASURED`; no value authorizes an
automatic link. The producer writes the complete `triage-producer-recipe.v1` record,
including this file's digest and imaging library versions, beside its decision manifest.
The real Door binds that recipe document under `triage_document_digests`; it must not be
added to `run_config_bindings`, because there is no run when the producer executes.

A `near-duplicate` verdict is agreement between two 64×48 mean+ink signatures, never a
claim that two frames show one physical page — and on this corpus the difference is
load-bearing. A parish register is a printed ruled form, so two frames of *different*
openings of one book share their rules, their columns, and their blank space, and can
agree in every cell; two blank frames of one form are indistinguishable from one frame
shot twice. The sealed recipe therefore carries `known_blindness`, naming the cases the
signature provably cannot separate, and every evidence record carries both ink totals and
their integer distance — the magnitude that cell agreement, being a thresholded boolean,
throws away. The record-local `near_duplicate_reason` says signature agreement is not page
identity, and `measurement_status: UNMEASURED` travels with its threshold snapshot; neither
fact requires the reader to find the separate recipe first. An ink count is within-cell
contrast, not an amount of ink: a cell of uniform tone counts zero whether it holds blank
paper or a solid dark insert. A confirmation reads those numbers, the blindness statement,
and the frames; it may not read the verdict as identity.

Two units the reader has to keep straight. A recorded `offset` is in signature cells on the
reduced proxy, not pixels on the master: it says which alignment the comparison chose and
is not a measurement of anything, so it may never seed a crop, a split, or a rotation. And
the recipe identifies the *instrument*, not the pass — two passes over different frames
share one recipe — so a pass is named by its evidence manifest, which carries the frame
digests it saw.

Each pass also closes its own books. `cluster-candidate-evidence-manifest.v1` counts the
pairs the candidate rule reached, the records emitted, and the pairs the equal-dimension
precondition refused — the last named by frame digest rather than tallied, because on a
multi-reel corpus "which frames could not be compared" is the operator's question. A
reader holding the frame digests and the recipe can recompute the selection and find a
pair the pass failed to emit, instead of taking a shorter list at face value. Frame source
digests are unique within one pass and each pair is canonical by sorted digest; duplicate
frame identity is refused before index pairs can masquerade as distinct evidence pairs.

`hard-failure` is the family's fourth member and the last to be sealed. It is the one
policy the orchestrator must read *before* the run exists — the tally threshold has to
be known to decide whether a resumed run may re-enter a stage at all — and then holds
for the whole run, so it is read once, held, and proved against the run authority at
the first moment such an authority exists: the resume preflight. On a first run there
is nothing to prove it against until the Door creates the authority, and the Door seals
these digests from the same bytes. What the file *says* remains Tyrel's: the threshold and
the `[[kind]]` list are both his rulings. Sealing the file is engineering; changing its
content is not.
