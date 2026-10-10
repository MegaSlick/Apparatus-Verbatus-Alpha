# Armarium — contract

The Armarium is where the run's output is written and where its totals must reconcile.
It publishes one `kind="manifest-entry"` per counted reading and one terminal
`kind="export"` record under `7_armarium/artifacts/`. The export record references a
content-addressed ZIP blob, the product bundle; `bundle.py` is how that bundle leaves
the run tree. Every counted reading lands in exactly one of the five closed categories
(`delivered`, `held-for-review`, `refused-with-reason`, `excluded-with-approval`,
`confirmed-blank`); a reading in none, or in two, stops the export.

The Armarium projects established text; it never establishes, repairs, chooses or
rewrites it. The only text it delivers is each Archetypus record's `text`. Witness
words, reconstructions and model readings beside a correction travel labelled, apart
from that text, and never replace it.

**Exit codes.** `EXIT_COMPLETE` when the bundle's `claims.status` is `complete`, with the
export record's outcome `delivered`. `EXIT_HELD` when it is `partial`, with the outcome
`held-for-review`. A refusal anywhere before the export record is fatal.

## Stage-completion seal

Before this producer's final manifest it publishes one `decode-environment` and
one `stage-seal`, or reuses both on a byte-identical retry. The seal witnesses
this pass's disk inventory and blob contents, and binds the exact decode-environment
bytes, run `config_digest` and `register_digest`, and `(kind, outcome)` census. An exit
held after publishing stage evidence seals it (holds remain in its census); a
pass that never reaches its seal does not seal, whether it was held or refused
before publishing stage evidence or closed fatally after publishing it, so the
orchestrator correctly refuses a missing final boundary.

Seals are compared as the set the stored inventory names, on both sides of the
boundary: the producer refuses to re-seal, and the successor refuses to read,
when any named seal is no longer on disk. Ordinals are the contiguous run 1..N,
so removing the latest leaves a prefix that still looks whole, and the earlier
statement would then answer for a boundary it never witnessed.

## Input boundary

The stage opens through `common.stage.open_stage_context`, which decides the fixture or
real route from one read of `run.json`. Before publishing anything it:

- binds the run's sealed format selection (`config/formats.toml`, below) and refuses a
  run with none;
- reconciles every `run.json` source ordinal to exactly one Exemplar page outcome,
  verifies the Exemplar `corpus-seal` and rechecks each sealed page's Door admission
  and pixel blob; a missing, duplicate, altered or unaccounted page is fatal, and a
  page the Exemplar refused stays in the census as an explicit refusal;
- refuses to run over a Recensor hold no person has passed, and refuses a run whose
  stored operator review decisions are not the set the Recensor's last pass applied,
  saying to re-run the Recensor.

**The denominator** is `common.stage.reading_denominator`'s page form; reviews are
read only through `common/page_review.py`. Rows of kind `act` are the act partition.
Rows of kind `other` are a separate, labelled layer: never counted as an act, never in
the act partition, `acts.jsonl` or `acts.sqlite`. A `page-unread` or `page-blank` row
is an act-partition unit with no text: `held-for-review` with its review's reason and
hold codes, or `confirmed-blank` (a `page-blank` row only) when the Recensor confirms
it. A `page-refused` row must name a page the census refused; it is reported there
with the Door's reason and counted nowhere else.

**Categories.** A reading whose review is not `accepted` takes the review's terminal
category and must have no Archetypus record. An accepted reading must have exactly one,
which the stage reconciles against its row, its accepted review, its reading, its
act-region (re-proven from the Exemplar) and any stored edit, recomputing its
uncertainty layer and `text_status` (`pipeline/6_archetypus/CONTRACT.md`). An accepted
reading whose record lacks identity or region provenance is `refused-with-reason`,
visibly, never dropped. `excluded-with-approval` comes only from a current
`approval-record.v1` that excludes the unit, and its rows carry the `approval_ref`.

**Run identity** is one of two closed shapes: a fixture run carries `fixture_id`, a real
submission carries `submission_id` (the filename ledger's self-hash,
`common.stage.submission_identity`), never both and never neither. The field is never
overloaded; consumers branch on which key is present.

**Canaries.** When the run seals a canary ledger, each canary reading still gets a
text-free `manifest-entry`, and the export record carries a `canary` block of page
ordinals and act identities and categories. The bundle, its page census, act list and
aggregate contain only real pages and readings.

## The `export` record and manifest entries

Each `manifest-entry` carries the reading's identity, kind, class, page ordinal,
category, hold codes, witness coverage, review notes and digest-checked evidence
references; a delivered reading adds its text, `text_status`, provenance, source
regions, witnesses, uncertainty layer and its `perlectio_ref`, `recensor_ref` and
`dissent_ref`; a reading not delivered adds its reason. A delivered reading's
`witnesses` are the page witnesses its feed showed, each `{chair, witness_label,
outcome, testimonium_ref, provenance}`, the label being what the reader saw it under (a
pseudonym in a blinded run).

The `export` payload carries the run identity, `scenario`, the run `aggregate`,
`expected_acts`, `delivered` and `non_delivered` act entries (every act not
delivered, including `confirmed-blank` and `excluded-with-approval`), `other_readings`,
`pages`, the optional `canary` block, the witness roster and floor, and `bundle`:
`{filename: "armarium-export.zip", format, reference, sha256, manifest_member,
manifest_self_hash, claims_status}`. The reference is the export record's sole input.

Each `pages` row is one submitted source page or frame: `ordinal`, `outcome`,
`reason`, `declared_path`, `declared_sha256`, `page_id`, and when recorded
`declared_bytes`, `ledger_sha256` (a real submission) and `container_page_index` (a
page or frame of a PDF, TIFF or animation). Each delivered source region repeats that
link for the exact crop its text was read from, so an output can be matched to the
original file and frame without searching intermediate artifacts.

## The product bundle

The bundle is a ZIP written with every member stored (never compressed) and fixed
metadata, with `EXPORT_MANIFEST.json` first. It is deterministic for given inputs,
except that bytes 96-99 of `acts.sqlite` hold the writing library's SQLite version.

The whole archive is bounded by its own limit, `MAX_EXPORT_ARCHIVE_BYTES`
(`common/armarium_formats.py`), not by the single-page blob ceiling: every run-tree
read of it (input verification, the completion seal, `bundle.py`) uses that limit, and
an archive above it is refused before it is stored. The Door refuses a run whose embedded export
is estimated above it (`pipeline/1_exemplar/CONTRACT.md`).

The formats are the run's sealed selection, `config/formats.toml`
(`common/armarium_formats.py`, `armarium-formats.v2`): any of `text-bundle`,
`acts-database`, `jsonl`, `csv` and `review-items`, `embed_pixels`, and `lot`. The
committed selection is all five, one format set: the plain-text readings, SQLite, JSONL
and CSV each give the same reading of every act. The selection is
sealed into the run's `config_digest`, so a run's product cannot be re-projected under
another selection.

**The lot.** With `lot = true` (the committed default) the manifest's `run` block and
every row (`acts.jsonl`, `other.jsonl`, `review-items.jsonl`, `acts.csv`, the `acts`
table) carry the
run's lot, `lot_<16 hex>` derived from `run.json`'s self-hash
(`common.contracts.identities.lot_id`), and each `readings.txt` names it under the run's
status; with `false` each carries `null` and `readings.txt` has no lot line. The lot
traces a row to its run, settings, models and commit. It is written only into the
product, which stays with the run tree on the lead's machine or the pod: `bundle.py`
recomputes it from `run.json` before publishing and refuses a destination inside a git
work tree that git does not ignore.

| Member | Present | Schema id |
|---|---|---|
| `EXPORT_MANIFEST.json` | always | `armarium-export-manifest.v13` |
| `sources.json` | always | `armarium-sources.v6` |
| `text/_source_folder/<folder>/readings.txt`, `text/_source_root/readings.txt` | `text-bundle`: one per source folder | — |
| `acts.sqlite` | `acts-database` | `armarium-acts-sqlite.v7` (`PRAGMA user_version` 7) |
| `acts.jsonl` | `jsonl` | `armarium-act.v7` |
| `acts.csv` | `csv` | its header row (below) |
| `other.jsonl` | `jsonl` | `armarium-other-reading.v3` |
| `coniector.jsonl` | `jsonl`, when a reconstruction is shown | `armarium-coniector-reconstruction.v1` |
| `operator.jsonl` | `jsonl`, when an operator acted on a delivered reading | `armarium-operator-action.v1` |
| `model_readings.jsonl` | `jsonl`, when a person corrected a delivered reading | `armarium-model-reading.v1` |
| `review-items.jsonl` | `review-items` | `armarium-review-item.v3` |
| `pixels/pages/<ordinal>.img`, `pixels/crops/<region_id>.img` | `embed_pixels = true` | — |

Every id moves with its closed field set, and the verifier recognises only these, so a
consumer keying on an id never reads an older shape out of a newer record. Rows are in
reading order (page, then reading number). Every row of one reading carries the same
`act_id`, `act_key`, `lot`, `category` and `reason`; a held or refused reading always carries a
reason (`"upstream recorded no reason"` when none was recorded).

**Pixels.** With `embed_pixels = false` every page and crop is cited by run-relative path
and digest, marked `requires-source-access`, and the manifest says resolving it needs
source access. With `true` the verified bytes are members and verification opens them.
Every other citation into the run tree (reviews, Perlectiones, receipts, approvals) is
marked `requires-retained-run-access`; the bundle carries paths and digests, never the
evidence itself.

### `EXPORT_MANIFEST.json`

A closed, self-hashed object: `schema`, `canonical_text`, `run`, `formats`, `claims`,
`aggregate`, `aggregate_basis`, `witness_chairs`, `witness_floor`, `members` (every
other member's path, sha256 and byte count) and `self_hash`.

`canonical_text` names the one text field (`canonical_clean_text`), its hash (SHA-256
of its UTF-8 bytes), that derived columns are marked as derived, and the literal formats
compared for identity (`text-bundle`, `acts-database`, `jsonl`, `csv`; empty below
two).

`claims` is the closed set:

- `status` and `partial_reasons`: the terminal ledger's (below). `status` is also what
  the stage reports in the export record's outcome and its exit code.
- `terminal_ledger`: `armarium-terminal-ledger.v1`.
- `act_partition`: `{denominator: "page-read reading acts", expected_count, counted,
  reconciles, categories: [{category, count, act_ids}], act_keys}`.
- `submission_inventory`: the source denominator is one unit per page or frame ordinal
  bound in `run.json`, so a multi-page file is several units, and its `limit` says the
  file's own category is not represented.
- `page_census`: `{denominator, counted, status}`.
- `pixels`: `{embedded, resolution_claim}`.
- `retained_run_references`: the fixed `requires-retained-run-access` claim.
- `uncertainty`: `{status, offset_unit: "unicode-code-point", carried_by}`; the status
  is `canonical-unicode-codepoint-offsets` when a literal format carries the layer.
- `ink_map`: `{denominator, held_pages, unmeasurable_pages}`, derived from
  `sources.json`'s `ink_map_pages`.
- `not_measured`, `other_readings`, `page_accounting`, `reask`: below.
- `doubt_share`: `{denominator, status, acts: [{act_id, act_key, page_ordinal,
  doubtful_or_unread, out_of}], pages: [{ordinal, doubtful_or_unread, out_of}]}`, in
  reading order, over each delivered act's established text
  (`common.reading_annotations.doubt_count`: its non-whitespace characters inside an
  uncertain span, each gap counted as one unread character, and a reading with nothing
  read as one unread character) and each page's delivered
  acts together. `status` is `measured`, or `not-applicable-no-literal-format` with
  empty lists when no literal format carries the text. Verification recounts it from
  the package's own literals. The Perlector holds a reading, and every reading of a
  page, over the sealed `[doubt]` limits (`pipeline/4_perlector/CONTRACT.md`); the
  export refuses a delivered act over the act limit that was never held
  `doubt-share-high`, and recounts each page over its counted readings'
  Perlectiones as the Perlector did, refusing a page over the page limit with a
  reading not held `page-doubt-share-high`, so only a person's decision delivers one. The page record
  counts delivered acts only, so it can be lower than the share the page was held on.

### `sources.json`

The text-free source graph every package carries, whatever its formats: `pages`,
`regions` (every cited crop), `act_citations` (each delivered act's provenance, source
regions and evidence), `act_outcomes` (`{act_id, act_key, category, reason,
text_status, approval_ref}` per act, `approval_ref` exactly for an exclusion), `aggregate_basis`, `witness_chairs`, `witness_floor`,
`ink_map_pages`, `other_outcomes` and `other_citations` (the same for other readings,
with `page_ordinal`), `page_accounting` and `act_readings`. When present:
`continuation_joins`, `reconstructions` (the act ids of each shown reconstruction),
`operator_actions`, `reading_hold_codes` and `model_readings`.

### The text bundle

One `readings.txt` per source folder, UTF-8, `\n`-separated. Each file opens with:

```text
# Armarium text bundle — source folder: <folder>
run-status: complete | partial (EXPORT_MANIFEST.json claims.partial_reasons says why)
lot: <lot>                          (when the run has one)
folder-readings: <n> delivered, <m> not delivered
```

then one section per delivered act on the folder's pages (an act whose regions span
folders appears in each):

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

followed, when they apply, by continuation notes (`possible-continuation-on:` and
`possible-continuation-from:`), operator lines and reconstruction lines (below). After
the acts come join reconstructions, then each delivered other reading as its own
`## OTHER <act_key> (not an act)` section with fields named apart from an act's
(`other-id:`, `other-source-page:`, `other_text:`, `other_diplomatic:`,
`other_uncertainty:`, `other_text_status:`), so no act reader reads one as an act. The file ends with one
text-free section for every unresolved sealed page and every unsealed source in the
folder, and then for every act or other reading on its pages that was not delivered:

```text
## NOT DELIVERED page | source <ordinal>
not-delivered: page | source <category>
not-delivered-reason: <the reason, as one JSON string>

## NOT DELIVERED <act_key> (<act_id>)
not-delivered: act | other <category>
not-delivered-reason: <the reason, as one JSON string, or null>
not-delivered-approval: <the approval_ref, as one JSON string>   (an exclusion only)
```

A folder whose readings were all held still gets its file. Every value a model or a
person wrote is one JSON line, so none can start a line a reader parses.

### `acts.sqlite`

- `acts`: one row per counted act, with `act_id`, `act_key`, `category`, `lot`,
  `canonical_clean_text`, `canonical_text_sha256`, `provenance_json`,
  `source_regions_json`, `uncertainty_json`, `uncertainty_status`, `text_status`,
  `evidence_json`, `approval_ref`, `reason`, `reading` and `operator_label` (the act's
  operator row's label, null when no operator acted on it). Text-derived columns are
  null for an act not delivered.
- `act_search`: one derived search key per delivered act, the `search_fold` of its
  literal (accents, case, ligatures and apostrophes folded; `textnorm.py`), with its
  hash, the literal's hash, the normalizer revision and `derived_kind: "search-fold"`.
- `acts_fts`: an FTS5 index over `act_search` (`unicode61 remove_diacritics 2`).
- `export_metadata`: `canonical_text_encoding`, `canonical_text_field`,
  `normalizer_revision`, `schema` and `unidata_version`; and the run, as the manifest
  states it: `run` (the manifest's `run` binding, canonical JSON, carrying its
  `fixture_id` or `submission_id`), `run_status` (`claims.status`) and
  `partial_reasons` (`claims.partial_reasons`, canonical JSON), so a reader of the
  database alone sees whose run it is and that a partial run is partial. The
  manifest's own self-hash cannot be among them, since the manifest hashes this
  member. The fold depends on the Unicode database, which differs between Python
  versions, so the version it was made under is recorded.

The database carries no other reading.

### `acts.jsonl`, `other.jsonl`, `review-items.jsonl`

These files are rows only: they carry no run status and are read with
`EXPORT_MANIFEST.json`, which inventories them by digest.

- `acts.jsonl`: one row per counted act, its row count the act partition: `schema`,
  `act_id`, `act_key`, `lot`, `category`, `canonical_clean_text` and `canonical_text_sha256`
  (null unless delivered), `provenance`, `source_regions`, `uncertainty`,
  `uncertainty_status`, `text_status`, `witnesses`, `perlectio_ref`, `recensor_ref`,
  `dissent_ref`, `approval_ref`, `reason`, `evidence_refs` and `reading`.
- `other.jsonl`: one row per other reading, `kind: "other"` and `page_ordinal`, text
  only when delivered. A separate member, because a second population in `acts.jsonl`
  would be counted as acts by any reader counting its rows.
- `review-items.jsonl`: one row per held or refused reading, act or other: `schema`,
  `act_id`, `act_key`, `lot`, `kind` (`act` | `other`), `category`, `reason` and
  `evidence_refs`. It is a review queue, not a count of acts.

### `acts.csv`

One flat row per counted act, in reading order, for a spreadsheet: UTF-8 with a
byte-order mark, CRLF rows, RFC 4180 quoting. Its header row is `act_key`, `act_id`,
`lot`, `category`, `reason`, `reading`, `text_status`, `canonical_clean_text`,
`diplomatic_text` (the text as a reader is shown it, below), `canonical_text_sha256`,
`uncertainty_json` (the layer as one canonical JSON object), and `doubtful_or_unread`
and `out_of`, the act's doubt share (below). Null is an empty cell; the text columns are empty for an act not delivered.

A cell starting with `=`, `+`, `-`, `@`, a tab or a carriage return, which a spreadsheet
would run as a formula, is written with one leading `'`, and so is a cell already
starting with `'`, so the escape is undone exactly by removing one leading `'`. The
hash column is the hash of the unescaped text, and every other format carries the text
unescaped. To check `canonical_text_sha256` against an escaped `canonical_clean_text`
cell, a reader strips its one leading `'` first. Verification reads each delivered act's text and layer back, compares them
with every other literal format, and renders the file again from `sources.json`, the
manifest's lot and those readings, requiring the same bytes.

### Labelled layers beside a delivered reading

**The Coniector's reconstructions** (`pipeline/4b_coniector`) are verified at export by
`common.reconstruction_records.verified_reconstructions`, which recomputes each record
from the Perlector's sealed readings and each call's retained reply. A reconstruction is
shown only beneath a delivered act whose literal is exactly the reading the Coniector
was shown (a join only when every piece is delivered), as one
`armarium-coniector-reconstruction.v1` row: its label, who made it, its diplomatic pieces
with their doubt marks, its departures, its flags and, when not made, why. In the text
bundle it follows its act's section as `reconstruction_*` lines ending with the whole
row, and a join is its own `## JOIN RECONSTRUCTION <keys> (not an act)` section. A made
reconstruction's block carries the "with reconstructions" view, `with_reconstructions:`
(`coniector_layer.with_reconstructions`): its diplomatic pieces with each departure
shown as `⟨word⟩` and every other doubt mark bracketed as in the diplomatic text, beside
the model that made it and each departure's reason. `⟨⟩` appears nowhere else. No
reconstruction or flag enters the act count, the ledger, review items, the database or
the aggregate. Beneath an act a person corrected, the row is held to the model's
reading and says `made_from: "model reading (original)"`.

**The operator layer**: every delivered reading an operator review decision released or
corrected, act or other, as one `armarium-operator-action.v1` row `{act_id, act_key,
kind, label, cleared_codes, reading_hold_codes, decisions}`. `label` is `released by
operator` or `corrected by a person`; `cleared_codes` are every hold the decisions
cleared, and `reading_hold_codes` those the reading's own Perlectio held it on. Each
decision names what was decided, its scope and subject, `approver`, `timestamp`,
`reason`, `decision_hash` and the stored approval. `sources.json` carries every row, so
the label travels in every package; `operator.jsonl` carries them with `jsonl`, the
text bundle shows each beneath its reading's section as an `operator_label:` line and
the row, and `acts.sqlite` names each act's label. Whenever the run has review
decisions, `sources.json` also carries `reading_hold_codes`: every delivered reading's
own hold codes, empty for one that carried none.

**A person's correction** is delivered as the person's text with the fixed no-doubt
layer, counted like any accepted act, its provenance labelled `corrected by a person`
(`pipeline/6_archetypus/CONTRACT.md`). Its operator row adds `note` and `model_reading`.
The model's reading itself is shown beside the person's, labelled `model reading
(original)`, as one `armarium-model-reading.v1` row `{act_id, act_key, kind, label,
text, uncertainty, text_status, perlectio_ref}`: in `sources.json` (`model_readings`),
in `model_readings.jsonl` with `jsonl`, and in the text bundle beneath the reading's
section. A correction is no reason: a run is never partial for corrections alone.

## Accounting

**The terminal ledger** (`claims.terminal_ledger`) is the total partition: every
submitted source page or frame, every sealed page, every counted act and every other
reading lands in exactly one of the five categories. The unit types overlap on purpose
(an act, its page and the source that sealed the page describe one piece of material),
so `by_unit_type` is published beside `by_category`. A source inherits its sealed page's
category; a refused source is `refused-with-reason` with the Door's reason. A sealed
page is `delivered` when any act on it was, `excluded-with-approval` or
`confirmed-blank` only when every act on it was, and `held-for-review` otherwise,
including a page no reading accounts for, because silence cannot tell a blank page from
a detection failure. A page with no act row is decided by its other readings: held
until the Recensor confirms the page holds no act, then a confirmed no-act page,
`delivered`. An unclaimed-edge-ink hold makes its page `held-for-review`.

`claims.status` is `complete` only when the ledger and the run aggregate have no
unresolved unit or reason. `claims.partial_reasons` names each unresolved fact once:
the aggregate's reasons, each unresolved act's line carrying its recorded reason
(`act <key> is <category>: <reason>`), each unresolved other reading by key, and a page
or refused source only when nothing above already says why.

**The aggregate** is `common.contracts.outcomes.run_aggregate` over the act categories,
witness coverage, page census and `aggregate_basis`: `coverage_records`,
`unaddressed_chairs`, `act_pages` (each act's own page and every page its regions were
cut from), `act_text_status` (each delivered act's status), `continuation_flags` and
`page_witness_chairs`; `routed_page_witness_chairs` (the chairs seated on routed pages
only, a proper part of `page_witness_chairs`) on a run that routes a witness, so a
coverage record counts either every page witness or every one but the routed;
`review_decisions` (`{clearances, page_holds, corrections}`) on a
run with operator review decisions; and `systemic_review` (`{held_pages, pages,
max_held_page_share}`) when a person advanced the run past a held share above its
sealed limit. A clearance, a held page, a systemic share, a damaged delivered act, an
unpaired continuation flag and every continuation join are each a named reason, so such
a run stays `partial`.

**Edge ink.** `sources.json`'s `ink_map_pages` has one row per sealed page: the Ink
Map's finding and, for a page it flagged, its retained ink runs re-measured against
every placed reading region (act or other), with the sealed gate they are judged
under; `remeasured` is null for a page the map never flagged. The held set is derived
from those counts by the Ink Map's own gate, on build and on verification, and never
stored beside them. A held page is a coverage finding, not a change to any text.

**Damage.** A delivered reading's `text_status` is recomputed from its uncertainty
layer in every format; a reading whose layer records a gap is `partial`, and the
aggregate names it.

**The lead's rulings on what a reader is shown.** The established text is diplomatic,
with brackets only where the ink is: `[illegible]` for a gap and `[word?]` for a
doubtful reading. Informed guesses are Coniector reconstructions, kept in their own
field and never in the established text. No Obsidian vault ships. The literal formats
carry the established text unbracketed with its uncertainty layer beside it; the
reader's views (`diplomatic:` and `other_diplomatic:` in `readings.txt`, the
`diplomatic_text` column of `acts.csv`) show it as
`common.reading_annotations.diplomatic_display` renders it from that layer: each gap
as `[illegible]` and each uncertain span as `[word?]`. A person's correction, which
carries no machine doubt, and a doubt report that could not be anchored are shown as
they are. The rendering is derived, so verification renders each view again; a
bracket the scribe wrote is shown as written, and only the layer says which brackets
are the reader's.

**Other readings.** `claims.other_readings` is `{layer, counted_as_acts: false, count,
by_category, act_ids, carried_by}`. On a page with acts, an other reading not delivered
is a reason, since it may be an act the reading did not establish.

**Page accounting and re-asks.** `claims.page_accounting` is `{denominator, pages,
held_pages, policy_sha256s}`, one row per real sealed page (`{ordinal, page_id, rules,
hold_codes, policy_sha256, accounting_ref}`) under the policy the run sealed. A page the
accounting holds delivers no reading unless an operator released every reading on it
over all of that page's hold codes. `claims.reask` counts the acts read on a re-ask
apart from first readings, in total and per page, and, when the run has any, the acts
of an operator re-read; each act's `reading` names which.

**Continuation joins.** Each Recensor continuation link becomes one text-free
`continuation_joins` row, `authoritative: false` and `not-reconstructed`, with its
reason: `no-code-join` when each side is exactly one delivered act, else
`side-names-no-act`, `flags-disagree`, `act-named-twice-on-one-side`,
`several-acts-on-a-side`, `head-not-delivered` or `tail-not-delivered`
(`verbatus-page-join.v3`). Code never joins text across a page break; only the
Coniector reconstructs across one. Every join keeps the run `partial`; none enters the
act count. A raised continuation flag no join pairs is a named reason.

**What was not measured.** `claims.not_measured` (`armarium-not-measured.v2`) names, in
order, every instrument a `DELIVERED`, `complete` run may still rest on, each with its
status (`measured`, `not-measured`, or `declared-unproduced` when nothing in this build
produces it) and what this run recorded: `perlector-uncertain-spans` (the sealed audit
`round_cap` and each delivered act's doubt assessment), `designator-geometry-calibration`
(the calibration provenance of the sealed Designator geometry and Perlector truncation
configurations), `page-accounting-thresholds` (every threshold of the sealed page
accounting policy, all starting values), `perlector-pass-c` (each sealed page's reading
audit; no reading runs Pass C) and `comparison-bounds` (each delivered act's dissent
rows against the sealed comparison budgets). `count` is how many did not measure.

## Verification and publication

`run.py` verifies the bundle it built from a clean extraction before sealing it.
`bundle.py --run-root <dir> --run-id <id> --out <dest>` reads the sealed blob, checks
its digest against the export record, verifies it again with
`armarium_export.verify_delivered_bundle` (the same checks, plus the cross-format
comparison of every literal), compares the package's aggregate, run binding, manifest
self-hash and status with the export record and `run.json`, requires the Armarium's
completion seal (`common.stage.verify_final_seal`) to verify and to witness that same
export record, and publishes `armarium-export.zip` and the verified extraction
(`bundle/`) by atomic rename. An existing destination is refused, and nothing is
written unless everything verifies.

Verification refuses an unsafe ZIP (a member that is compressed, a link, outside the
root, or aliased by case or Unicode normalization), a member that does not match its
manifest digest and byte count, and any member or field outside its closed shape. It
recomputes the ledger, the aggregate, every claim and each format's rows from
`sources.json`, and requires every format to carry the same text, uncertainty layer,
status, reason, provenance and labelled layers for each reading. Each `readings.txt`
is rendered again by the writer from what has already been checked and must be the
same bytes, so no line, title or section can be added, edited or moved. The search fold is
recomputed only under the Unicode database it was made with; `bundle.py` refuses to
publish when it could not be, so publish under the same Python as the build.

Verification proves a package is internally consistent and closed. A self-hash does not
authenticate the run-derived facts; the publisher adds the binding to the retained run
tree, and authenticity beyond that needs an external trust root. There is no
stand-alone verifier for a recipient without this repository.
