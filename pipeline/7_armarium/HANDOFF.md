# Armarium — handoff

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

That outside verification is `verify_delivered_bundle`, which asks two questions
rather than one: is the package internally whole (`verify_export_bundle`), and do
its literal-text formats carry the identical reading of every act
(`verify_projection_identity`'s comparison, over the members the first pass already
extracted). The two are separate functions so each refusal names its own defect, but
`EXPORT_MANIFEST.json` states `canonical_text.identity_verified_across` as a fact
about the package, so the last gate before a recipient makes that comparison rather
than asserting it. The published summary reports what each check actually did,
including the search-fold recomputation's own honest "not run under a different
Unicode database" — a check that declined to run must not read like one that ran.

## Stage-completion seal

Before this producer's final manifest it publishes one `decode-environment` and
one `stage-seal`, or reuses both on a byte-identical retry. The seal witnesses
this pass's disk inventory and blob contents, and binds the exact decode-environment
bytes, run `config_digest` and `register_digest`, and `(kind, outcome)` census. An exit
held after publishing stage evidence seals it (holds remain in its census); a
pass that never reaches its seal does not seal, whether it was held or refused
before publishing stage evidence or closed fatally after publishing it, so the
orchestrator correctly refuses a missing final boundary. Every difference in
decoders, platform, machine, `decode_paths_used`, and `produced_pixels` is
reported by field or decoder name. A valid difference is report-only and never
refuses; Unit 17 owns any fatal policy.

Seals are compared as the SET the stored inventory names, on both sides of the
boundary: the producer refuses to re-seal, and the successor refuses to read,
when any named seal is no longer on disk. Ordinals are the contiguous run 1..N,
so removing the latest leaves a prefix that still looks whole — and the earlier
statement would then answer for a boundary it never witnessed.

## Export contract

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
  requires the canonical value back exactly. Tyrel has not chosen a convention, and
  `claims.display.status` says so on the face of every bundle.
- `acts.sqlite` — an `acts` table with the literal Archetypus field, and a
  separate `act_search` / FTS5 layer whose search fold is visibly derived and
  revision-marked. Metadata schema `armarium-acts-sqlite.v2`
  (`PRAGMA user_version=2`): v2 covers R8's `annotations_json` →
  `uncertainty_json` rename (CR W15, which kept v1 — a real versioning miss)
  and this change's damage-record columns.
- `acts.jsonl` — one record per expected act, with canonical text only for a
  delivered act, provenance, source regions, its established-text status and
  transcription annotation layer, and the explicit pending claim for the separate
  semantic annotation layer. Record schema `armarium-act.v2`: v1's bare
  `annotations`/`annotation_status` pair is renamed apart into
  `semantic_annotations`/`semantic_annotation_status`, and `text_status`/
  `transcription_annotations` join the row — a consumer keying on the schema id
  must never read a v1 shape out of a v2 row. `sources.json` is
  `armarium-sources.v3` for the same reason twice over: at v2 its act-outcome
  rows began to REQUIRE `text_status` under exact-field-set validation, and at
  v3 `ink_map_pages` joins the source graph, so a v2 file cannot answer a v3
  reader's question at all. The manifest is `armarium-export-manifest.v3` for
  an image-local run and `armarium-export-manifest.v4` for a clustered one: v2
  renamed the annotation claims apart, v3 adds the required `ink_map` claim to
  the closed claim set, and v4 is the clustered act-partition claim — the
  denominator names logical acts and `local_proposal_rows`/`logical_membership`
  join the claim, so a v3 reader can never misread `expected_count` as
  proposal-seal rows. A clustered bundle also carries a `logical_accounting`
  block in `sources.json`, and `verify_export_bundle` recomputes the clustered
  claim from it instead of believing the self-hashed manifest.
- `review-items.jsonl` — held and refused act records with reasons and
  digest-checked evidence references.
- `salvage/items.jsonl` — a structurally separate salvage namespace. It has no
  act identifiers or canonical-text fields; promotion requires recorded approval
  and pipeline re-entry, never an export-time act.
- `sources.json` — cited source-page/frame rows with filename and digest, plus
  text-free per-act citation/outcome records, the non-text accounting basis, and
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
reason list — GOVERNANCE 2 failing at the last boundary in the case Tyrel expects to
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
layer is `annotation_boundary.py`'s unbuilt person/date/kinship apparatus; the
*transcription* layer is the Archetypus's own `uncertain`/`illegible` marks. Every row
used to carry `annotations: []` with `annotation_status: not-produced` — true of the
first, written over an act whose record had sealed a real mark of the second. The row
fields are now `semantic_annotations` / `semantic_annotation_status` and
`transcription_annotations`, and the manifest carries `claims.semantic_annotations`
(the fixed not-produced claim) beside `claims.transcription_annotations` (a measured
carriage claim, like `claims.uncertainty`). Neither takes the bare word.

**What this deliberately does not do is render the damage.** Whether a gap is shown
inside the `display:` reading remains Tyrel's choice of convention (spec 11), and
`claims.display.renders_canonical_uncertainty` still says `false` on the face of every
bundle. Counting damage is this stage's business; showing it is not.

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

The *semantic* annotation boundary in `annotation_boundary.py` — a different layer
from the transcription annotations above, and the reason neither of them keeps the
bare word — is not wired into this
stage, configuration, or orchestrator, and is built only as the contract a future
`annotator` chair would occupy — spec 11 gates the build itself on Tyrel approving
the ARCHITECTURE wording that gives the layer its home. It carries the five fields
spec 11 names (`act_type`, `date` with a normalized form, `person` spans with roles,
`kinship` edges, flags), whose semantic values are drawn from closed vocabularies fixed
in that file. Record and producer identifiers remain strings, but no writer maps them
into established text. Every annotation must anchor to a real span of the established
text, and one that does not is refused at the schema.

**What that refusal cannot yet do is be *recorded*.** Spec 11 test 7 asks for a
hallucinated person to be "refused at the schema and recorded"; the recording half
belongs in the terminal ledger's `refused-with-reason` set, and the ledger has no
annotation unit type because nothing in this repository produces an annotation to
account for. The refusal exists and is tested; the accounting for it does not.

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
