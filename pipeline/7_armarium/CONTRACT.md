# Armarium: contract

The Armarium writes the run's output and reconciles its totals. It publishes one
`kind="manifest-entry"` per counted reading and one terminal `kind="export"` record under
`7_armarium/artifacts/`. The export record references a content-addressed ZIP blob, the
product bundle; `bundle.py` is how that bundle leaves the run tree. Every counted reading
lands in exactly one of five closed categories (`delivered`, `held-for-review`,
`refused-with-reason`, `excluded-with-approval`, `confirmed-blank`); a reading in none, or in
two, stops the export.

The Armarium projects established text; it never establishes, repairs, chooses or rewrites
it. The only text it delivers is each Archetypus record's `text`. Witness words,
reconstructions and model readings beside a correction travel labelled, apart from that
text, and never replace it.

**Exit codes.** `EXIT_COMPLETE` when the bundle's `claims.status` is `complete` (export
outcome `delivered`); `EXIT_HELD` when it is `partial` (outcome `held-for-review`). A refusal
anywhere before the export record is fatal.

## Stage-completion seal

Before its final manifest the stage publishes one `decode-environment` and one
`stage-seal` (or reuses both on a byte-identical retry). The seal witnesses this pass's disk
inventory and blob contents and binds the exact decode-environment bytes, the run's
`config_digest` and `register_digest`, and the `(kind, outcome)` census. A held exit after
publishing stage evidence seals it; a pass that never reaches its seal does not, so the
orchestrator refuses a missing final boundary. Seals are compared as the set the stored
inventory names: the producer refuses to re-seal, and a successor refuses to read, when any
named seal is missing from disk (ordinals run 1..N, so a removed latest seal would otherwise
leave a prefix that looks whole).

## Input boundary

The stage opens through `common.stage.open_stage_context`. Before publishing anything it:

- binds the run's sealed format selection (`config/formats.toml`) and refuses a run with
  none;
- reconciles every `run.json` source ordinal to exactly one Exemplar page outcome, verifies
  the Exemplar `corpus-seal`, and rechecks each sealed page's Door admission and pixel blob;
  a missing, duplicate, altered or unaccounted page is fatal, and a page the Exemplar
  refused stays in the census as an explicit refusal;
- refuses a run over a Recensor hold no person has passed, and a run whose stored operator
  review decisions are not the set the Recensor's last pass applied (re-run the Recensor).

**The denominator** is `common.stage.reading_denominator`'s page form; reviews are read only
through `common/page_review.py`. Rows of kind `act` are the act partition. Rows of kind
`other` are a separate labelled layer, never counted as acts and never in `acts.jsonl` or
`acts.sqlite`. A `page-unread` or `page-blank` row is an act-partition unit with no text:
`held-for-review` with its review's reason, or `confirmed-blank` (a `page-blank` row only)
when the Recensor confirms it. A `page-refused` row must name a page the census refused, and
is reported there and counted nowhere else.

**Categories.** A reading whose review is not `accepted` takes the review's terminal category
and must have no Archetypus record. An accepted reading must have exactly one, reconciled
against its row, review, reading, act-region (re-proven from the Exemplar) and any stored
edit, with its uncertainty layer and `text_status` recomputed
(`pipeline/6_archetypus/CONTRACT.md`). An accepted reading whose record lacks identity or
region provenance is `refused-with-reason`, visibly. `excluded-with-approval` comes only from
a current `approval-record.v1` that excludes the unit, and its rows carry the
`approval_ref`.

**Run identity** is one of two closed shapes: a fixture run carries `fixture_id`, a real
submission `submission_id` (the filename ledger's self-hash), never both, never neither.

**Canaries.** When the run seals a canary ledger, each canary reading still gets a text-free
`manifest-entry`, and the export record carries a `canary` block of page ordinals, act
identities and categories. The bundle contains only real pages and readings.

## The `export` record and manifest entries

Each `manifest-entry` carries the reading's identity, kind, class, page ordinal, category,
hold and flag codes, review priority, witness coverage, review notes and digest-checked
evidence references. A delivered reading adds its text, `text_status`, provenance, source
regions, uncertainty layer, `perlectio_ref`, `recensor_ref`, `dissent_ref`, and `witnesses`:
the page witnesses its feed showed, each `{chair, witness_label, outcome, testimonium_ref,
provenance}` under the label the reader saw (a pseudonym in a blinded run). A reading not
delivered adds its reason.

The `export` payload carries the run identity, `scenario`, `aggregate`, `expected_acts`,
`delivered` and `non_delivered` act entries, `other_readings`, `pages`, the optional `canary`
block, the witness roster and floor, and `bundle`: `{filename: "armarium-export.zip",
format, reference, sha256, manifest_member, manifest_self_hash, claims_status}`. The
reference is the export record's sole input.

Each `pages` row is one submitted source page or frame: `ordinal`, `outcome`, `reason`,
`declared_path`, `declared_sha256`, `page_id`, and when recorded `declared_bytes`,
`ledger_sha256` (real submission) and `container_page_index` (a page of a PDF, TIFF or
animation). Each delivered source region repeats that link for its exact crop, so an output
can be matched to its original file and frame directly.

## The product bundle

A ZIP with every member stored (never compressed), fixed metadata, and
`EXPORT_MANIFEST.json` first. It is deterministic for given inputs, except that bytes 96-99
of `acts.sqlite` hold the SQLite library version. The archive is bounded by
`MAX_EXPORT_ARCHIVE_BYTES` (`common/armarium_formats.py`) on every read and refused above it;
the Door refuses a run whose export is estimated above it.

**Formats** are the run's sealed selection (`config/formats.toml`, `armarium-formats.v2`):
any of `text-bundle`, `acts-database`, `jsonl`, `csv` and `review-items`, plus `embed_pixels`
and `lot`. The committed selection is all five; the plain text, SQLite, JSONL and CSV each
give the same reading of every act. The selection is in the run's `config_digest`, so a
product cannot be re-projected under another one.

**The lot.** With `lot = true` (the default) the manifest's `run` block and every row carry
`lot_<16 hex>`, derived from `run.json`'s self-hash (`common.contracts.identities.lot_id`),
and each `readings.txt` names it; with `false` each carries `null`. The lot traces a row to
its run, settings, models and commit. `bundle.py` recomputes it before publishing and refuses
a destination inside a git work tree that git does not ignore.

| Member | Present | Schema id |
|---|---|---|
| `EXPORT_MANIFEST.json` | always | `armarium-export-manifest.v13` |
| `sources.json` | always | `armarium-sources.v6` |
| `text/_source_folder/<folder>/readings.txt`, `text/_source_root/readings.txt` | `text-bundle`: one per source folder | — |
| `acts.sqlite` | `acts-database` | `armarium-acts-sqlite.v7` (`PRAGMA user_version` 7) |
| `acts.jsonl` | `jsonl` | `armarium-act.v7` |
| `acts.csv` | `csv` | its header row |
| `other.jsonl` | `jsonl` | `armarium-other-reading.v3` |
| `coniector.jsonl` | `jsonl`, when a reconstruction is shown | `armarium-coniector-reconstruction.v1` |
| `operator.jsonl` | `jsonl`, when an operator acted on a delivered reading | `armarium-operator-action.v1` |
| `model_readings.jsonl` | `jsonl`, when a person corrected a delivered reading | `armarium-model-reading.v1` |
| `review-items.jsonl` | `review-items` | `armarium-review-item.v3` |
| `flagged.jsonl` | `review-items`, when a reading is held or flagged | `armarium-flagged-reading.v1` |
| `pixels/pages/<ordinal>.img`, `pixels/crops/<region_id>.img` | `embed_pixels = true` | — |

Every id has a closed field set, and the verifier recognises only these. Rows are in reading
order (page, then reading number). Every row of one reading carries the same `act_id`,
`act_key`, `lot`, `category` and `reason`; a held or refused reading always carries a reason
(`"upstream recorded no reason"` when none was recorded).

**Pixels.** With `embed_pixels = false` every page and crop is cited by run-relative path and
digest, marked `requires-source-access`; with `true` the verified bytes are members. Every
other citation into the run tree is marked `requires-retained-run-access`; the bundle carries
paths and digests, never the evidence itself.

### `EXPORT_MANIFEST.json`

A closed, self-hashed object: `schema`, `canonical_text`, `run`, `formats`, `claims`,
`aggregate`, `aggregate_basis`, `witness_chairs`, `witness_floor`, `members` (every other
member's path, sha256 and byte count) and `self_hash`. `canonical_text` names the one text
field (`canonical_clean_text`), its hash (SHA-256 of its UTF-8 bytes), that derived columns
are marked derived, and the literal formats compared for identity.

`claims`:

- `status` and `partial_reasons`: the terminal ledger's (below); `status` is also the export
  record's outcome and the exit code.
- `terminal_ledger`: `armarium-terminal-ledger.v1`.
- `act_partition`: `{denominator: "page-read reading acts", expected_count, counted,
  reconciles, categories: [{category, count, act_ids}], act_keys}`.
- `submission_inventory`: one unit per page or frame ordinal bound in `run.json` (a
  multi-page file is several units).
- `page_census`, `pixels` (`{embedded, resolution_claim}`), `retained_run_references`.
- `uncertainty`: `{status, offset_unit: "unicode-code-point", carried_by}`.
- `ink_map`: `{denominator, held_pages, unmeasurable_pages}`, from `sources.json`'s
  `ink_map_pages`.
- `not_measured`, `other_readings`, `page_accounting`, `reask`: below.
- `doubt_share`: per delivered act and per page, `doubtful_or_unread` of `out_of`
  (`common.reading_annotations.doubt_count`: non-whitespace characters inside an uncertain
  span, each gap one unread character), or `not-applicable-no-literal-format`. Verification
  recounts it. The export refuses a delivered act over the sealed act limit that was never
  held `doubt-share-high`, and a page over the page limit with a reading not held
  `page-doubt-share-high` (`pipeline/4_perlector/CONTRACT.md`), so only a person's decision
  delivers one.

### `sources.json`

The text-free source graph every package carries: `pages`, `regions` (every cited crop),
`act_citations`, `act_outcomes` (`{act_id, act_key, category, reason, text_status,
approval_ref}`), `aggregate_basis`, `witness_chairs`, `witness_floor`, `ink_map_pages`,
`other_outcomes`, `other_citations`, `page_accounting`, `act_readings`, and when present
`continuation_joins`, `reconstructions`, `operator_actions`, `reading_hold_codes`,
`model_readings` and `flagged_readings`.

### The text bundle

One `readings.txt` per source folder, UTF-8, `\n`-separated, opening with:

```text
# Armarium text bundle — source folder: <folder>
run-status: complete | partial (EXPORT_MANIFEST.json claims.partial_reasons says why)
lot: <lot>                          (when the run has one)
folder-readings: <n> delivered, <m> not delivered
```

then one section per delivered act on the folder's pages (an act spanning folders appears
in each):

```text
## <act_key> (<act_id>)
act-id: <act_id>
reading: first reading | read on re-ask | read on operator re-read
source-page: <declared_path>        (one pair per cited region)
source-sha256: <declared_sha256>
canonical_text_sha256: <sha256>
canonical_clean_text:
<the text, as one JSON string>
diplomatic:
<the text as a reader is shown it, as one JSON string>
uncertainty:
<the uncertainty layer, as one JSON object>
text_status: established | partial
```

followed, when they apply, by continuation notes (`possible-continuation-on:`,
`possible-continuation-from:`), operator lines and reconstruction lines. After the acts come
join reconstructions, then each delivered other reading as `## OTHER <act_key> (not an act)`
with fields named apart from an act's (`other-id:`, `other-source-page:`, `other_text:`,
`other_diplomatic:`, `other_uncertainty:`, `other_text_status:`). The file ends with a
text-free section for every unresolved page and unsealed source, then every act or other
reading not delivered:

```text
## NOT DELIVERED page | source <ordinal>
not-delivered: page | source <category>
not-delivered-reason: <the reason, as one JSON string>

## NOT DELIVERED <act_key> (<act_id>)
not-delivered: act | other <category>
not-delivered-reason: <the reason, as one JSON string, or null>
not-delivered-approval: <the approval_ref, as one JSON string>   (an exclusion only)
```

A folder whose readings were all held still gets its file. Every value a model or person
wrote is one JSON line, so none can start a line a reader parses.

### `acts.sqlite`

- `acts`: one row per counted act: `act_id`, `act_key`, `category`, `lot`,
  `canonical_clean_text`, `canonical_text_sha256`, `provenance_json`, `source_regions_json`,
  `uncertainty_json`, `uncertainty_status`, `text_status`, `evidence_json`, `approval_ref`,
  `reason`, `reading`, `operator_label`. Text-derived columns are null for an act not
  delivered.
- `act_search`: one search key per delivered act, the `search_fold` of its literal (accents,
  case, ligatures and apostrophes folded; `textnorm.py`), with its hash, the literal's hash,
  the normalizer revision and `derived_kind: "search-fold"`.
- `acts_fts`: an FTS5 index over `act_search` (`unicode61 remove_diacritics 2`).
- `export_metadata`: `canonical_text_encoding`, `canonical_text_field`, `normalizer_revision`,
  `schema`, `unidata_version` (the fold depends on Python's Unicode database), and `run`,
  `run_status` and `partial_reasons`, so a reader of the database alone sees whose run it is
  and whether it is partial.

### `acts.jsonl`, `other.jsonl`, `review-items.jsonl`

Rows only; read them with `EXPORT_MANIFEST.json`, which inventories them by digest.

- `acts.jsonl`: one row per counted act (its row count is the act partition): `schema`,
  `act_id`, `act_key`, `lot`, `category`, `canonical_clean_text` and `canonical_text_sha256`
  (null unless delivered), `provenance`, `source_regions`, `uncertainty`,
  `uncertainty_status`, `text_status`, `witnesses`, `perlectio_ref`, `recensor_ref`,
  `dissent_ref`, `approval_ref`, `reason`, `evidence_refs`, `reading`.
- `other.jsonl`: one row per other reading (`kind: "other"`, `page_ordinal`, text only when
  delivered). It is a separate member so no row counter counts it as acts.
- `review-items.jsonl`: one row per held or refused reading, act or other: `schema`, `act_id`,
  `act_key`, `lot`, `kind`, `category`, `reason`, `evidence_refs`. A review queue, not a count.

### `acts.csv`

One row per counted act, in reading order: UTF-8 with a byte-order mark, CRLF rows, RFC 4180
quoting. Header: `act_key`, `act_id`, `lot`, `category`, `reason`, `reading`, `text_status`,
`canonical_clean_text`, `diplomatic_text`, `canonical_text_sha256`, `uncertainty_json`,
`doubtful_or_unread`, `out_of`. Null is an empty cell.

A cell starting with `=`, `+`, `-`, `@`, a tab, a carriage return or `'` is written with one
leading `'`, so a spreadsheet does not run it as a formula and removing one leading `'` undoes
it exactly. The hash column is the hash of the unescaped text; strip the `'` before checking
it. Verification re-renders the file from `sources.json` and requires the same bytes.

### Labelled layers beside a delivered reading

**Reconstructions** (`pipeline/4b_coniector`) are re-verified at export
(`common.reconstruction_records.verified_reconstructions`, from the sealed readings and each
call's retained reply). A reconstruction is shown only beneath a delivered act whose literal
is exactly what the Coniector was shown (a join only when every piece is delivered), as one
`armarium-coniector-reconstruction.v1` row: label, maker, diplomatic pieces with their doubt
marks, departures, flags, and why when not made. In the text bundle it follows its act as
`reconstruction_*` lines; a join is its own `## JOIN RECONSTRUCTION <keys> (not an act)`
section. A made reconstruction carries `with_reconstructions:`, with each departure shown as
`⟨word⟩` (`⟨⟩` appears nowhere else). No reconstruction enters the act count, ledger, review
items, database or aggregate. Beneath a corrected act it says `made_from: "model reading
(original)"`.

**The operator layer**: every delivered reading an operator decision released or corrected,
as one `armarium-operator-action.v1` row `{act_id, act_key, kind, label, cleared_codes,
reading_hold_codes, decisions}`, `label` being `released by operator` or `corrected by a
person`. Each decision names what was decided, scope, subject, `approver`, `timestamp`,
`reason`, `decision_hash` and the stored approval. The rows are in `sources.json`,
`operator.jsonl`, the text bundle (`operator_label:`) and `acts.sqlite`. With review decisions
in the run, `sources.json` also carries every delivered reading's own `reading_hold_codes`.

**The flagged layer** (`flagged_layer.py`): every counted reading the Recensor held or that
carries a review flag (`pipeline/5_recensor/CONTRACT.md`, "Review flags"), as one
`armarium-flagged-reading.v1` row `{act_id, act_key, lot, kind, page_ordinal, status,
category, review_priority, hold_codes, flag_codes, text, text_label, reason, perlectio_ref,
page_reading_ref, recensor_ref, evidence_refs}`. A delivered reading's row is
`established-with-flags` with the established text; every other row is `not-established`,
its `text` the model's reading labelled "model reading, not established" (or null). It is a
review queue with text, not an act count; a held reading's text appears here and nowhere
else. The rows are in `sources.json` and, with review items, `flagged.jsonl`.

**A person's correction** is delivered as the person's text with a fixed no-doubt layer,
counted like any accepted act and labelled `corrected by a person`. Its operator row adds
`note` and `model_reading`. The model's reading is shown beside it as one
`armarium-model-reading.v1` row `{act_id, act_key, kind, label, text, uncertainty,
text_status, perlectio_ref}` labelled `model reading (original)`, in `sources.json`,
`model_readings.jsonl` and the text bundle. A run is never partial for corrections alone.

## Accounting

**The terminal ledger** (`claims.terminal_ledger`) partitions every submitted source page or
frame, every sealed page, every counted act and every other reading into exactly one of the
five categories. The unit types overlap on purpose (an act, its page and its source describe
one piece of material), so `by_unit_type` is published beside `by_category`.

- A source inherits its sealed page's category; a refused source is `refused-with-reason`
  with the Door's reason.
- A sealed page is `delivered` when any act on it was, `excluded-with-approval` or
  `confirmed-blank` only when every act on it was, and `held-for-review` otherwise, including
  a page no reading accounts for (silence cannot tell a blank page from a detection
  failure). A page with only other readings is held until the Recensor confirms it holds no
  act. An unclaimed-edge-ink hold makes its page held.

`claims.status` is `complete` only when the ledger and aggregate have no unresolved unit or
reason. `claims.partial_reasons` names each unresolved fact once (`act <key> is <category>:
<reason>`, each unresolved other reading, and a page or source only when nothing above says
why).

**The aggregate** is `common.contracts.outcomes.run_aggregate` over the act categories,
witness coverage, page census and `aggregate_basis` (`coverage_records`,
`unaddressed_chairs`, `act_pages`, `act_text_status`, `continuation_flags`,
`page_witness_chairs`, and when they apply `routed_page_witness_chairs`, `review_decisions`
and `systemic_review`). A clearance, a held page, a systemic share, a damaged delivered act,
an unpaired continuation flag and every continuation join are each a named reason, so such a
run stays `partial`.

**Edge ink.** `ink_map_pages` has one row per sealed page: the Ink Map's finding and, for a
flagged page, its retained ink runs re-measured against every placed region, with the sealed
gate. The held set is derived from those counts on build and on verification, never stored. A
held page is a coverage finding, not a change to any text.

**Damage.** A delivered reading's `text_status` is recomputed from its uncertainty layer in
every format; a layer recording a gap makes it `partial`, and the aggregate names it.

**What a reader is shown.** The established text is diplomatic, with brackets only where the
ink is: `[illegible]` for a gap and `[word?]` for a doubtful reading. Informed guesses are
Coniector reconstructions, kept apart. The literal formats carry the text unbracketed with its
uncertainty layer beside it; the reader's views (`diplomatic:`, `other_diplomatic:`,
`diplomatic_text`) are rendered from that layer by
`common.reading_annotations.diplomatic_display`, and verification renders them again. A
person's correction and an unanchorable doubt report are shown as they are; a bracket the
scribe wrote is shown as written, and only the layer says which brackets are the reader's.

**Other readings.** `claims.other_readings` is `{layer, counted_as_acts: false, count,
by_category, act_ids, carried_by}`. On a page with acts, an undelivered other reading is a
reason, since it may be an act the reading did not establish.

**Page accounting and re-asks.** `claims.page_accounting` is `{denominator, pages,
held_pages, policy_sha256s}`, one row per real sealed page (`{ordinal, page_id, rules,
hold_codes, policy_sha256, accounting_ref}`). A page the accounting holds delivers no reading
unless an operator released every reading on it over all its hold codes. `claims.reask`
counts acts read on a re-ask apart from first readings, and the acts of operator re-reads;
each act's `reading` names which.

**Continuation joins.** Each Recensor continuation link becomes one text-free
`continuation_joins` row, `authoritative: false` and `not-reconstructed`, with its reason:
`no-code-join` when each side is exactly one delivered act, else `side-names-no-act`,
`flags-disagree`, `act-named-twice-on-one-side`, `several-acts-on-a-side`,
`head-not-delivered` or `tail-not-delivered` (`verbatus-page-join.v3`). Code never joins text
across a page break; only the Coniector reconstructs across one. Every join keeps the run
`partial`.

**What was not measured.** `claims.not_measured` (`armarium-not-measured.v2`) names every
instrument a `complete` run may still rest on, each `measured`, `not-measured` or
`declared-unproduced`, with what this run recorded: `perlector-uncertain-spans`,
`designator-geometry-calibration`, `page-accounting-thresholds`, `perlector-pass-c` and
`comparison-bounds`. `count` is how many did not measure.

## Verification and publication

`run.py` verifies the bundle from a clean extraction before sealing it.
`bundle.py --run-root <dir> --run-id <id> --out <dest>` reads the sealed blob, checks its
digest against the export record, verifies it again with
`armarium_export.verify_delivered_bundle` (plus the cross-format comparison of every literal),
compares its aggregate, run binding, manifest self-hash and status with the export record and
`run.json`, requires the Armarium's completion seal to verify and witness that export record,
and publishes `armarium-export.zip` and the verified extraction (`bundle/`) by atomic rename.
An existing destination is refused, and nothing is written unless everything verifies.

Verification refuses an unsafe ZIP (a compressed member, a link, a path outside the root, or
aliasing by case or Unicode normalization), a member that does not match its digest and size,
and any member or field outside its closed shape. It recomputes the ledger, aggregate, every
claim and each format's rows from `sources.json`, requires every format to agree on each
reading, and re-renders each `readings.txt` byte for byte. The search fold is recomputed only
under the Unicode database it was made with, so publish under the same Python as the build.

A self-hash proves a package internally consistent and closed, not that the run-derived facts
are authentic; `bundle.py` binds it to the retained run tree, and anything beyond that needs
an external trust root. There is no stand-alone verifier outside this repository.

## Not built

`rows.jsonl` is not written: index, table and ledger rows are in `other.jsonl` as kind
`other`. The Archetypus and Armarium records carry only the act class `kind`, not
`entry_kind`, `page_type` or `writing`, which stay on the Perlector's records.
