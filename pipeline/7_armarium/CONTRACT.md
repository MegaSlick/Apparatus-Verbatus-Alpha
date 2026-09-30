# Armarium — contract

The Armarium's two boundary records carry the same non-terminal `sealed` and
`recorded` outcomes as every other stage. They are completed bookkeeping, never
`delivered` output; only the `export` record may make that terminal claim.
The Armarium publishes the terminal `kind="export"` record and one
`kind="manifest-entry"` per expected act. Both are ordinary artifacts under
`7_armarium/artifacts/`; the stage manifest is derived inventory, never a competing
output file. The `export` record's `bundle.reference` is a digest-checked input
reference to the content-addressed Armarium ZIP blob. That ZIP is the product
which leaves the pipeline; the internal artifact is only its accounting record.

`bundle.py` is how it leaves. It reads the sealed blob, checks it against the digest
the `export` artifact recorded, verifies it from the outside exactly as a recipient
with no run tree would, and publishes `armarium-export.zip` plus its verified
extraction to an operator-chosen destination — all of it or none of it. An existing
destination is refused rather than merged into. It writes no text and projects
nothing: every byte it publishes came out of the run tree already sealed.

The standalone verifier checks package schema, closure, and internal consistency:
`verify_export_bundle` verifies the package and `verify_projection_identity` compares
its literal formats. A self-hash does not authenticate run-derived facts. Publication
adds the independent retained-run check: `bundle.py` compares the exact ZIP digest,
run binding, aggregate, and manifest identity to the immutable export artifact before
writing a destination. Authenticity beyond the retained-run immutability contract
requires an external trust root. The published summary reports what each check did.

## Stage-completion seal

Before this producer's final manifest it publishes one `decode-environment` and
one `stage-seal`, or reuses both on a byte-identical retry. The seal witnesses
this pass's disk inventory and blob contents, and binds the exact decode-environment
bytes, run `config_digest` and `register_digest`, and `(kind, outcome)` census. An exit
held after publishing stage evidence seals it (holds remain in its census); a
pass that never reaches its seal does not seal, whether it was held or refused
before publishing stage evidence or closed fatally after publishing it, so the
orchestrator correctly refuses a missing final boundary.

Seals are compared as the SET the stored inventory names, on both sides of the
boundary: the producer refuses to re-seal, and the successor refuses to read,
when any named seal is no longer on disk. Ordinals are the contiguous run 1..N,
so removing the latest leaves a prefix that still looks whole — and the earlier
statement would then answer for a boundary it never witnessed.

## Real ingress

The Armarium opens through `open_stage_context`, which decides the fixture or
real route from one read of `run.json` and hands this stage the context that
route produces; it no longer calls `open_context` directly. `expected_acts`,
`page_census`, and every other reader that walks sealed upstream artifacts by
stage and kind already work unchanged on either route — nothing about how the
Armarium accounts for pages and acts is fixture-shaped.

**The one thing that is fixture-shaped is the manifest's run identity, and it is
now two closed shapes rather than one.** A fixture run's export names
`fixture_id`; a real run's names `submission_id` — `common.stage.submission_identity`'s
filename-ledger self-hash, which every real source row carries and which
`pipeline/1_exemplar/run.py`'s ledger check already proves reproduces the
ledger that admitted the submission. `ArmariumProjection` carries both fields,
exactly one of them non-blank; `_validate_projection` refuses a projection
naming both or neither, and the resealed-manifest verifier
(`_verify_manifest_field_closure`) refuses the same shape a second time from
package-supplied JSON that never went through the projection at all. **The
field is never overloaded**: stamping a submission's identity into the field
named `fixture_id` would travel in every export forever and read as a fixture
run, which is exactly the corpus-identity confusion GLOSSARY's "one concept per
word" rule exists to prevent. `run.py` computes which shape applies once, from
`submission_identity(context.run)`, before it ever touches `context.fixture` —
so the refusing fixture accessor is never asked a question on the route where
it would refuse.

**The gap above is closed.** `bundle.py::_expected_run_binding` now reads
whichever identity key the sealed `export` artifact's payload actually carries
— `fixture_id` or `submission_id`, refusing by name if it carries both or
neither — instead of hardcoding `fixture_id`. `test_bundle_publish.py` pins a
real-shaped export payload publishing clean and a both-named payload being
refused by name.

**The real-run identity union does not have a separate manifest schema id.**
It remains part of the current image-local `armarium-export-manifest.v7` and
clustered `armarium-export-manifest.v8` shapes. Those two ids distinguish the
act-partition denominator: a reader must know whether `expected_count` counts
proposal-seal rows or logical acts before interpreting the claims.

Run identity is a separate closed union within either schema. A fixture package
carries `run.fixture_id`; a real-submission package carries `run.submission_id`,
with exactly one of those keys present. Consumers of v7 or v8 must branch on
that key before reading it. An unconditional `manifest["run"]["fixture_id"]`
read is invalid for a real-run package, and the schema id alone intentionally
does not distinguish which run-identity shape the package carries. The producer
and recipient both enforce this union; the publisher additionally checks it
against the retained run authority described above.

## Export contract

When the run seals a canary ledger, the Armarium still publishes one
`manifest-entry` per expected canary act. Its terminal `export` artifact carries
a text-free `canary` block of page ordinals and act identities/categories. The
ZIP projection, its page census, source manifest, act list, and aggregate count
contain only real pages and acts. An act spanning canary and real pages refuses
export rather than dropping the real side. A run without a sealed canary ledger
has no `canary` block and retains the prior byte-identical export shape.
Membership uses the Designator proposal seal's primary page ordinal together
with every verified region page. A conservation hold with no crop still belongs
to its sealed page, so it cannot leak into the real export or aggregate.

The export payload contains the aggregate result, the expected-act count, `delivered`
entries, `non_delivered` entries (every act that was not delivered, including
`confirmed-blank` and `excluded-with-approval`, not only `held-for-review` and
`refused-with-reason`), witness coverage, `pages`, and the bundle reference. Every
`pages` row is one submitted source ordinal and retains:

```text
ordinal
declared_path
declared_sha256
declared_bytes              when the filename ledger recorded it
ledger_sha256               for a real submission
container_page_index        for a fanned container page or animation frame
outcome and reason
```

This is the final citation link: an output can be matched to the original filename
and source digest, and a PDF/TIFF/animation page can be matched to its zero-based
source page/frame without guessing from the pipeline ordinal.

Each delivered entry's `source_regions` repeats that link for the exact crop used
by its text. A source-region row carries `source_page_ordinal`,
`source_page_id`, `declared_path`, `declared_sha256`, and any applicable byte
count, ledger hash, and `container_page_index`, alongside the crop digest. A
continuation therefore names both original pages it used rather than relying on a
reader to search intermediate artifacts.

## Product bundle

`EXPORT_MANIFEST.json` is the first ZIP member and self-hashes its own contents.
It inventories every other member by digest, names the exact `canonical_clean_text`
field and its UTF-8 SHA-256 identity, and reports the selected `formats.toml`
projection configuration. The bundle may contain these plainly specified formats:

- `text/_source_folder/<source-folder>/readings.txt` (or
  `text/_source_root/readings.txt` for the source root) — readable sections with a source page and
  source digest, retaining the literal `canonical_clean_text` value, and beside it a
  `display:` rendering under the **proposed** convention named on the line above it.
  The rendering never replaces the canonical field: the clean verifier strips it and
  requires the canonical value back exactly. No convention has been chosen, and
  `claims.display.status` says so on the face of every bundle.
- `acts.sqlite` — an `acts` table with the literal Archetypus field, and a
  separate `act_search` / FTS5 layer whose search fold is visibly derived and
  revision-marked. Metadata schema `armarium-acts-sqlite.v3`
  (`PRAGMA user_version=3`): v2 covers R8's `annotations_json` →
  `uncertainty_json` rename (which kept v1 — a real versioning miss)
  and the damage-record columns. V3 marks the nullable withheld-draft uncertainty.
- `acts.jsonl` — one record per expected act, with canonical text only for a
  delivered act, provenance, source regions, its established-text status and
  transcription annotation layer, and the explicit pending claim for the separate
  semantic annotation layer. Record schema `armarium-act.v3`: v1's bare
  `annotations`/`annotation_status` pair is renamed apart into
  `semantic_annotations`/`semantic_annotation_status`, and `text_status`/
  `transcription_annotations` join the row — a consumer keying on the schema id
  must never read a v1 shape out of a v2 row. V3 requires `lectio_kind` and
  permits null self-revisions when Pass A was withheld. `sources.json` is
  `armarium-sources.v3` for the same reason twice over: at v2 its act-outcome
  rows began to REQUIRE `text_status` under exact-field-set validation, and at
  v3 `ink_map_pages` joins the source graph, so a v2 file cannot answer a v3
  reader's question at all. The manifest is `armarium-export-manifest.v7` for
  an image-local run and `armarium-export-manifest.v8` for a clustered one: v2
  renamed the annotation claims apart, v3 added the required `ink_map` claim to
  the closed claim set, v4 was the clustered act-partition claim — the
  denominator names logical acts and `local_proposal_rows`/`logical_membership`
  join the claim, so a v3 reader can never misread `expected_count` as
  proposal-seal rows — and v5/v6 add the required `not_measured` claim to both
  shapes at once, so a stale reader cannot present a bundle that names five
  unmeasured instruments as one that names none. V7/v8 add the required
  `ink_map.unmeasurable_pages` census to both shapes, so a complete bundle cannot
  hide a page on which that distinct audit took no measurement. A clustered bundle also carries a `logical_accounting`
  block in `sources.json`, and `verify_export_bundle` recomputes the clustered
  claim from it instead of believing the self-hashed manifest.
- `review-items.jsonl` — held and refused act records with reasons and
  digest-checked evidence references.
- `continuation_joins` in `sources.json` and `reconstructions.jsonl` — present only when
  the Designator published a `continuation-candidate`, and refused unless every act it
  names has a review citing it. Each join row is text-free and `authoritative: false`:
  `reconstructed` when each side names exactly one delivered act and `jsonl` or
  `text-bundle` is selected, else `not-reconstructed` with a named reason and no text. The
  head and tail pages must be adjacent and among the pages each named act was marked
  out on. A reconstructed join is the head literal, one U+000A, then the tail literal
  (`verbatus-page-join.v2`, nothing added, removed or normalised), labelled
  `RECONSTRUCTED … not an act`, and carries each half's `text_status`, its reader
  assessment state and a count of its uncertain spans, gaps and self-revisions (the
  offsets stay on each half's own literal). A `primed-draft-withheld` reading carries
  `self_revisions: null` and its lectio kind: the reader did not see Pass A, so a
  self-revision count was not measured. V2 permits null head/tail doubt counts
  and carries each half's `lectio_kind` in `armarium-reconstructed-join.v2`.
  It is written to `reconstructions.jsonl` (with `jsonl`) and as a
  `## RECONSTRUCTED <join_id> (not an act)` section, with mirrored
  `possible-continuation-on/-from` notes in each named act's own section (with
  `text-bundle`). Every join keeps the run `partial` with a reason named from its
  status; no reconstruction enters the act count, the ledger's units, review items, the
  database or its search index. The clean verifier recomputes every row, every
  reconstruction record, every section line for line in its head act's folder and
  every act's notes from the packaged literals.
- `salvage/items.jsonl` — a structurally separate salvage namespace. It has no
  act identifiers or canonical-text fields; promotion requires recorded approval
  and pipeline re-entry, never an export-time act.
- `sources.json` — cited source-page/frame rows with filename and digest, plus
  text-free per-act citation/outcome records, the non-text accounting basis, the
  text-free `continuation_joins` rows when any exist, and
  one `ink_map_pages` row per sealed page: what Unit 9's pre-proposal map found,
  and what this stage re-measured its retained runs to once the Designator's
  verified crops were known (`remeasured: null` for a page the map never
  flagged, because writing zeros would record a measurement nobody took).
  The `unclaimed-edge-ink` held set is DERIVED from those counts by the ink
  map's own gate, on both sides — never carried beside them as a boolean, and
  never read back out of the manifest claim it produced. `sources.json` itself
  therefore differs between a held and a released page — it carries the counts
  the hold is derived from — and so does the manifest claim derived from them;
  `test_armarium_export.py`'s
  `test_a_dropped_edge_hold_cannot_be_verified_away_on_a_clean_machine` asserts
  exactly that difference. The hold changes no established text: it is a
  coverage finding about a page, not a reading. Before those counts entered the
  source graph a manifest built with the hold dropped verified clean, which is
  the hole that derivation closed.
  The clean verifier uses these to require every selected projection to retain
  the exact delivered provenance, every continuation region, and every held or
  refused reason; it does not treat a merely nonempty replacement as equivalent.

If `embed_pixels = true`, verified page and crop bytes are included beneath
`pixels/` and clean-machine verification opens them. If it is false, source and
crop references remain valid and digest-named, but the manifest says plainly that
pixel resolution requires retained-source access.

### The damage record: `text_status` and the two annotation layers

**A delivered act is not necessarily a whole one.** `delivered` says where the act
ended; the Archetypus's `text_status` (`established | partial | no_readable_text`)
says whether the reading that left carries ink the Perlector knew was there and
could not read. Neither that field nor the record's `annotations` layer used to be
read here at all, so an act the pipeline itself knew was damaged was exported and
aggregated exactly like a whole one, and the run reported `complete` with an empty
reason list — silent loss at the last boundary in a case expected to
be ordinary ("many of our records are damaged").

Both now travel, and neither is taken on trust:

- `verify_established_record` validates and normalises both annotation layers
  through the shared `validate_annotations` and requires the validated forms to be
  identical (raw equality would refuse a correct record, since the sealed copy is
  normalised and the reading's raw one may omit `witness_evidence`), then
  **recomputes** `text_status` from that layer and the canonical `uncertainty`
  beside it (`common/contracts/outcomes.py::derive_record_text_status`,
  the one spelling both stages share). A record claiming `established` over its own
  recorded gap is fatal here.
- The manifest entry, the projection, `sources.json`'s text-free `act_outcomes`, and
  every selected literal format carry the status; the transcription annotation layer
  rides in the literal formats beside the text it marks up, exactly as the canonical
  uncertainty layer does. Cross-format projection identity compares both, so two
  deliverables cannot disagree about whether the same act is damaged.
- Every product verifier re-derives the status from the row's own layers on a clean
  machine rather than reading it back. A single-literal-format package is covered too,
  where cross-format identity would catch nothing.
- `run_aggregate` takes the per-act status through `aggregate_basis.act_text_status`,
  so a damaged act contributes its own named reason and the run reports `partial`. The
  basis is packaged, so the clean verifier recomputes that verdict instead of believing
  it. A run whose acts are all delivered but damaged therefore reports `partial` and
  exits `EXIT_HELD`: the acts are delivered, and the run did not read all of them.

**Two annotation layers, two names, because they are two things.** The *semantic*
layer is spec 11's person/date/kinship apparatus, which no code produces; the
*transcription* layer is the Archetypus's own `uncertain`/`illegible` marks. Every row
used to carry `annotations: []` with `annotation_status: not-produced` — true of the
first, written over an act whose record had sealed a real mark of the second. The row
fields are now `semantic_annotations` / `semantic_annotation_status` and
`transcription_annotations`, and the manifest carries `claims.semantic_annotations`
(the fixed not-produced claim) beside `claims.transcription_annotations` (a measured
carriage claim, like `claims.uncertainty`). Neither takes the bare word.

**What this deliberately does not do is render the damage.** Whether a gap is shown
inside the `display:` reading remains a choice of convention not yet made (spec 11), and
`claims.display.renders_canonical_uncertainty` still says `false` on the face of every
bundle. Counting damage is this stage's business; showing it is not.

### `claims.not_measured` — what this run did not measure

Required on every bundle (it is what took the manifest to `.v5`/`.v6`) and
derived, never constant. `DELIVERED` and `aggregate.status == "complete"` are
reachable over five things this build does not fully measure, each recorded somewhere
and none of them, before this, qualifying the word on the deliverable:

| instrument | what is unmeasured | where the record lives |
|---|---|---|
| `page-testimony-content-coverage` | a page whose coverage was recorded `shortfall: null` — a continuation page, most often | each act's Recensor review, `testimony_content_coverage` and `testimony_content_coverage_continuation` |
| `page-ink-conservation` | a page whose `ink_measurable: false` was never reconciled | the Designator's per-page conservation records |
| `act-visibility-survey` | the Designator occlusion instrument, which no stage publishes, so every capture row carries a named absence code | each act's Recensor review, `cross_capture_coverage` |
| `perlector-uncertain-spans` | `round_cap = 1` blocks exhausted-cap audit spans but still permits reader-supplied doubt spans; a cap of zero can add exhausted-cap spans. Status follows the recorded assessment and spans | `config/perlector_audit.toml` and each act's uncertainty layer |
| `designator-geometry-calibration` | every crop's `calibrated_for_this_corpus = false`, the grouping thresholds' `sample_count = 0`, and the truncation instrument's length floor, reasoned and never measured against real ink | the `provenance` blocks of the three sealed Designator configurations and of `config/perlector_protocol.toml`'s `[truncation]` table. The instrument's name is older than its list: the truncation floor is not Designator geometry, and it is surveyed here because this row is the only surface on which a bundle discloses a threshold nobody calibrated |

Every instrument appears on every bundle with its own `status`
(`measured` | `not-measured` | `declared-unproduced`), because an omitted row and
a measured row would otherwise read alike. `declared-unproduced` is the
contract's word for an instrument with no producer at all, and it is
deliberately not a softer `not-measured`: saying only "not measured" there
invites the reading that a measurement was attempted and came back empty.
`count` is how many instruments did not measure, and the verifier recomputes it.

`pipeline/7_armarium/run.py::not_measured_basis` derives the basis from retained-run
records and sealed configurations before the export is sealed. The standalone verifier
checks the packaged block's closure and internal consistency only; the publisher then
binds its exact ZIP to the immutable export artifact and run. A self-hash alone is not
an external authenticity proof.

### The terminal ledger

`claims.terminal_ledger` is the honesty ledger's total partition: every submitted
source page or frame, every sealed page, and every proposed act lands in exactly one
of the five closed categories, and a unit in none of them — or in two — stops the
export. The three populations overlap on purpose, so `by_unit_type` is published
beside `by_category`: an act, the page it was cut from, and the source that sealed
that page are three units describing one piece of material.

A source unit inherits the category of the page it sealed into, and a refused source
is `refused-with-reason` with the door's own reason. A sealed page is `delivered` when
any act on it was delivered, `excluded-with-approval` or `confirmed-blank` only when
every act on it was, and `held-for-review` otherwise — including when no act was
marked out on it at all, because silence cannot tell a blank page from a detection
failure and nothing here can prove one blank.

**`excluded-with-approval` remains projection-only.** It arises only from a
Designator `excluded` outcome, which no stage emits — the Recensor refuses an
unhandled Designator terminal before the Armarium is ever reached, so `run.py`'s own
`exclusion_approval_ref` guard, whose docstring states "this refuses every
exclusion today, approved or not," is unreachable rather than merely strict.
Recensor produces `confirmed-blank` only when the Perlector found `no-readable-text` and
each eligible witness independently corroborates that absence against the configured
witness floor. The gate also requires no continuation shortfall, flagged pages, or
findings route
(`pipeline/5_recensor/run.py:2891-2920`, over `blank_corroboration` at `:830-949`) — a
`confirmed-blank` is COMPLETED-class and terminal, so its gate checks every ordinary hold
cause before sealing. Both categories are exercised correctly and
adversarially at this projection layer
(`test_excluded_act_requires_and_carries_its_approval_reference`,
`test_page_ledger_category_inherits_confirmed_blank_and_excluded_when_every_act_agrees`);
the exclusion path remains projection-only, while confirmed blank is available end to
end when its evidence conditions are met.

**The denominator counts pages or frames, not source containers.** `run.json` binds one
ordinal per submitted source *page or frame*, so a multi-page PDF or TIFF has one unit per
page rather than one for the file. Every submitted file is represented, but this ledger's
units are pages. `claims.submission_inventory.limit` says exactly that.

`claims.status` is the ledger's own measured status, not a constant: a run that loses
nothing says `complete`, and every unresolved unit appears by name in
`claims.partial_reasons`. The clean verifier recomputes the whole ledger from the
package's `sources.json` rather than reading it out of the manifest — a self-hash
proves the manifest was not edited afterwards, never that what it says was true.

**`claims.status` is also what the stage reports**, in the `export` artifact's outcome
and in the exit code, rather than the run aggregate's status. The ledger folds the
aggregate's own reasons into its own and accounts two unit types the aggregate does
not, so it is never the less partial of the two — and it is the more partial one for a
sealed page whose acts all reached a completed category but disagree about which
(`_page_ledger_category` errs toward "a human must look"). Reporting the aggregate
there would exit 0 and record `delivered` over a bundle whose own face said `partial`
and named the held page. The aggregate remains a separate published measurement. Its
disagreement with the ledger requires the Designator `excluded` path, which no current
stage emits; the projection boundary nevertheless proves that accounting path.

Non-pixel references to receipts, Testimonia, and intermediate artifacts are
labelled `requires-retained-run-access`; the product carries their paths and
digests, not an invented claim that it contains the separate evidence package.
No stage in this repository produces a sealed salvage inventory today — the whole
salvage path is contract-only, exercised end to end only by synthetic projections in
this stage's own tests. So, when selected, every real run's salvage member is present
but the manifest says `not-produced-no-sealed-salvage-inventory`, rather than claiming
a measured zero.

The *semantic* annotation layer — a different layer from the transcription
annotations above, and the reason neither of them keeps the bare word — has no code
in this repository. Spec 11 gates its build on the project lead approving the
ARCHITECTURE wording that gives the layer its home; until then every export states
`claims.semantic_annotations` as not produced.

## Page-read runs

A run sealed with `reading_unit = "page"` exports through `_main_page` in
`run.py`; the act path is unchanged. The denominator is
`common.stage.reading_denominator`'s page form, and the Recensor's records are
read only through `common/page_review.py`.

**What is counted.** Rows of kind `act` are the act partition, one category
each: the manifest's `act_partition` names the denominator `page-read reading
acts`, and `expected_count` is that number. Rows of kind `other` are a separate,
labelled layer that is never counted as an act, never enters `act_partition`,
`acts.jsonl`, `acts.sqlite` or `review-items.jsonl`, and may not share an identity
with an act. Its readings still reach the aggregate as reasons: on a page with no
act row, until every one is delivered; on a page with acts, one not delivered is a
reason too, since a held `other` reading may be an act the reading did not
establish, as the Recensor's page-read receipt (v3 or v4) also counts it. A `page-unread` or `page-blank` row is an act
partition unit with no text: `held-for-review` with the review's reason and the
row's hold codes, or `confirmed-blank` (a `page-blank` row only) when the
Recensor confirms it. A `page-refused` row must be a page the census refused; it
is reported there with the Door's reason and counted nowhere else. An accepted
row must be one `page_review.require_establishable` allows (a `read` row, or one
held only by `no-act-on-page-unconfirmed` whose review releases exactly that
code) and has exactly one Archetypus record, verified against its row, its
accepted review, its reading (`read`, of the row's kind, with no `holds` or
`page_holds`) and its act-region (re-proven from the Exemplar), with the damage
layers recomputed: `annotations` must equal the reading's own layer validated
(`[]`, since a page reading records none) and `uncertainty` its
`from_page_perlectio` layer. `confirmed-blank` is refused on any row but a
`page-blank` one.

**Witnesses.** Each delivered reading's witnesses are
`page_testimonia.shown_page_witnesses` over the page's validated current
Testimonia (`current_page_testimonia`), the Archetypus's custody check, each
exported as `{chair, witness_label, outcome, testimonium_ref, provenance}`: the
chair read from the Testimonium the feed row names, and the label the reader saw
it under (a pseudonym in a blinded run, the chair in a named one). Only the
sealed page witnesses read a page, so the Recensor's coverage records count
exactly them: `aggregate_basis.page_witness_chairs` (page path only) names them,
sorted, as part of `witness_chairs`; every coverage record's `configured` is
their number, and a delivered reading's witnesses are a non-empty part of them.
Each manifest entry carries its review's `notes` as `review_notes` (a
continuation flag on an `other` entry, which holds nothing).

**Pages with no act.** A read page whose entries are all `other` carries
`no-act-on-page-unconfirmed` on each. Until the Recensor confirms the page holds
no act, its other readings are held and the aggregate and ledger name the page as
read with no act, its other readings held until that confirmation; once every
one is delivered the page is a confirmed no-act page: `delivered` in the ledger,
with that reason, and no aggregate reason. Page-path reasons never speak of acts
marked out or Designator crops: a page no reading accounts for is named as such,
and an edge hold names the reading regions.

**Regions and the ink map.** Source regions are the Archetypus's, each linked to
the original filename ledger as on the act path. The rectangles that may release
unclaimed edge ink are every placed act and other reading's `act-region`, each
verified by `verify_reading_region_lineage` in the function that uses it.

**Continuation joins** come from the Recensor's `recensor-continuation-link.v1`
records, read by `page_review.continuation_links` (one per flagged page break
`page-break:<p>:<p+1>`, each named side a counted row on its side of the break
carrying its own flag, `agreed` exactly when both flags are raised, `accepted`
exactly when agreed). Every link becomes a join row (its `candidate_ref` is the
link): agreed with both sides delivered, it is reconstructed exactly as on the
act path; a side with no `act` entry is `not-reconstructed`
(`side-names-no-act`); a link whose flags disagree is `not-reconstructed`
(`flags-disagree`). A link naming an `other` reading is fatal. Every join keeps
the run `partial` with its reason, as on the act path. Each delivered act's raised
flags travel in `aggregate_basis.continuation_flags` (`{act_key: [flag, ...]}`,
page path only), and a flag no join has as a side is a named partial reason, so a
flag is never dropped.

**Acts read on a re-ask.** An act a page's one re-ask recovered is counted in
the act partition like any other, and labelled with the reading it came from:
each counted row's `reading` is `first reading` or `read on re-ask`, from the
`reading_attempt` of its row in the verified denominator (1 or 2), and `null` for
a `page-unread` or `page-blank` row, which stands for no entry. The other layer
carries no such label: it is never counted as acts.

**Manifest `armarium-export-manifest.v10`**, its own id because its denominator
differs (a v7/v8 reader must not read reading acts as proposal-seal rows), with
three more required claims:

- `claims.other_readings` -- `{layer, counted_as_acts: false, count,
  by_category, act_ids, carried_by}`, derived from `sources.json`'s
  `other_outcomes`. The terminal ledger gains a fourth unit type, `other`: a held
  other reading keeps `claims.status` partial, and other readings decide a page's
  category only on a page with no act ("Pages with no act" above).
- `claims.page_accounting` -- `{denominator, pages, held_pages,
  policy_sha256s}`, one row per real sealed page: `{ordinal, page_id, rules
  (letter -> status), hold_codes, policy_sha256, accounting_ref}`, read from the
  page's `page-accounting` under the policy this run sealed.
- `claims.reask` -- `{label: "read on re-ask", first_reading_acts,
  read_on_reask_acts, read_on_reask_act_ids, pages}`, the acts read on a re-ask
  counted apart from those of first readings, in total and per real sealed page
  (`pages`: `{ordinal, first_reading_acts, read_on_reask_acts}`, one per
  `page_accounting` row), derived from `sources.json`'s `act_readings`. A row
  standing for no entry counts in neither.

`claims.not_measured` names the page path's instruments, in order (every
threshold must be an integer, and one that is not is fatal rather than left out):
`perlector-uncertain-spans` and `designator-geometry-calibration` as on the act
path, then `page-accounting-thresholds` (every threshold of the sealed
`config/page_accounting.toml`; all are starting values, so `not-measured`),
`perlector-pass-c` (from each real sealed page's reading `audit`, `pages_read`
bound to that page count; the page path never runs Pass C, so
`declared-unproduced`) and `lectio-nuda` (the sealed
`nuda_per_mille`, which the page path refuses above zero, and the Perlector's
`lectio-nuda` records). The act path's testimony-content, page-ink-conservation
and act-visibility instruments read act-path records a page-read run does not
make; the page accounting measures what they did, and it is claimed above.

**Formats.** `sources.json` is `armarium-sources.v5`: v3 plus `other_outcomes`,
`other_citations`, `page_accounting` and `act_readings` (`{act_id, act_key,
page_ordinal, reading}` per counted act, in act-id order). A page reading's
uncertainty layer names its own lectio kind, `page-read` (`self_revisions:
null`), a value the v3 act shapes do not know, and each page-path act carries its
`reading`, so page-path act rows are `armarium-act.v5` and the acts database
`armarium-acts-sqlite.v5` (`user_version` 5, a `reading` column on `acts`), each
otherwise the v3 shape; a delivered act's text-bundle section carries a
`reading:` line after its `act-id:`. The act path's ids and bytes are unchanged.
The other layer is carried by:

- `other.jsonl` (with `jsonl`) -- one `armarium-other-reading.v1` row per other
  reading, text only when delivered. A separate member rather than a `kind` field
  in `acts.jsonl`, because `acts.jsonl` is one row per counted act and its row
  count is the partition every consumer reconciles against; a second population
  in it would be counted as acts by any reader counting rows.
- the text bundle -- a `## OTHER <act_key> (not an act)` section per delivered
  other reading, after its folder's acts, whose fields (`other-id:`,
  `other-source-page:`, `other_text:`, ...) are named apart from an act's so no
  act parser reads one as an act.

`acts.sqlite` carries no other reading; `claims.other_readings.carried_by` says
which formats do.

**Verification.** `verify_export_bundle` recomputes the three claims from
`sources.json`, requires `act_readings` to name exactly the act partition's acts
and every act row, database row and text-bundle act section to carry its
source reading (and no act-path row or section to carry one), requires every other reading apart from the act partition, reads
`other.jsonl` and every OTHER section against the source rows and requires the
formats carrying the layer to agree on each reading's text, uncertainty and
status, recomputes the ledger with its `other` units, requires the acts
database's schema id to be the one its reading unit writes, binds Pass C's
`pages_read` to the real sealed pages, requires `aggregate_basis.act_pages` to
name every page a delivered act's cited regions were cut from, recomputes each
join (a `flags-disagree` join only on the page path), and refuses a page the
accounting holds that delivered any reading.

## Boundary checks

Before the Armarium publishes any artifact, it reconciles every `run.json`
source-manifest ordinal to exactly one Exemplar page outcome. It independently reads
the one Exemplar `corpus-seal`, verifies its self-hash, page census, and input
references, then compares each row against the source manifest and page artifact.
For every sealed page it also rechecks the Door admission and content-addressed
pixel blob before export. A missing, duplicate, altered, or unaccounted page is
fatal; an Exemplar-refused page remains explicit evidence and contributes to a
visibly partial export rather than disappearing from the page set.

The act-level proposal seal remains the authority for expected acts. The Armarium
places each one in exactly one terminal category and retains a review reason where a
text cannot be delivered. An accepted act must have exactly one Archetypus record;
a non-accepted terminal act must have none, so the stage never selects one record
from an ambiguous or orphaned set. The Armarium does not choose among witness
readings or put witness text in output.

**Every sealed page must have had an act marked out on it, and that is checked per
page rather than per run.** The stage derives each act's page coverage from the
Designator regions actually cut -- not from the proposal seal's primary
`page_ordinal`, because an act running over a page break is cut on both sides and
examines both -- and hands it to the run aggregate, which names any sealed page no
act reached. Silence is not `confirmed-blank` evidence, and a check that asked only
whether the *run* produced any acts let every busy page discharge a silent page's
proof obligation. Nothing here diagnoses a blank page; that is the Recensor's, and
what artifact will eventually prove a page-level `confirmed-blank` is open -- the
category algebra is act-oriented and has no way to say "this page was examined and
held nothing".
