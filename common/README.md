# common

Code shared by more than one stage, or by a stage and `operations/`. Code enters here
when a second user needs it.

## What may import what

- `common/` imports neither `pipeline/` nor, outside its tests, `operations/`.
  `common/chairs/test_chairs_import_boundary.py` checks this statically, through `ast`.
- A stage imports its own modules, `common/`, and three seams of `operations/`:
  `operations.serving` (the chair client, serving manager and the reader of retained
  replies), and, in the door only, `operations.submit` and `operations.triage`. The Ink
  Map and the orchestrator import nothing from `operations/`.
- No stage imports another. `pipeline/test_stage_import_boundaries.py` checks this; no
  test limits which `operations/` packages a stage imports.

## Modules

**Contracts and the run tree**

| Module | What it holds |
|---|---|
| `contracts/` | the schema label, envelope, canonical serialization, identities, outcome algebra, approval and stage vocabularies ([contracts/README.md](contracts/README.md)) |
| `runtree/` | the run's evidence store: immutable artifacts, atomic publication, derived manifests, run receipts ([runtree/README.md](runtree/README.md)) |
| `stage.py` | what every stage program shares: arguments, opening a run and its stage context, the sealed configuration bindings, stage seals, publishing with the envelope filled in, and the page-read denominator (below) |
| `sealed_config.py` | how a TOML configuration is sealed into a run and rechecked at its point of use |
| `durability.py` | publishing bytes under a name that survives a crash and a power cut |
| `checkout.py` | the check that the code runs from a source checkout, not an installed wheel |
| `credentials.py` | the one test of "this looks like a secret" every credential screen shares |
| `exemplar_boundary.py` | the check that a sealed page's admission and pixels are unchanged before any stage uses them |
| `corpus_register.py` | the corpus-scoped, append-only declaration register a run receives as a snapshot |
| `fixture_identity.py` | identity helpers for fixture rows |
| `calibration.py` | predicates over sealed calibration provenance |

**Run policy readers**

| Module | What it holds |
|---|---|
| `recovery.py` | the checked reader of the re-ask budget (`config/recovery.toml`) |
| `hard_failure.py` | the checked reader of the run-level hard-failure cap and its tally, recomputed from disk |
| `review_policy.py` | the checked reader of the held-page alarm (`config/review.toml`) |
| `decoding.py` | the run-sealed sampling and token bounds of every model reading, and the check that a call used them |
| `armarium_formats.py` | the sealed choice of Armarium export projections |
| `witness_adapters.py` | the declared witness-adapter names and scopes (the adapters themselves live in the Attestatores) |
| `witness_regime.py` | the named or blinded witness labels, re-derivable by any downstream verifier |

**Chairs and requests**

| Module | What it holds |
|---|---|
| `chairs/` | a named role resolved to one pinned model artifact, verified by digest ([chairs/README.md](chairs/README.md)) |
| `chair_wire.py` | request fields a chair carries because of the model in it |
| `request_capacity.py` | whether one reading request fits the sealed serving row it would be sent to |
| `chandra_native_retry.py` | Chandra's own revision-pinned retry recipe, ported |

**Images and ink**

| Module | What it holds |
|---|---|
| `image_sniff.py` | the byte-signature table for page sources and the "could be a page" test the submit door and the Exemplar door share |
| `imaging.py` | decoding, cropping and resizing, with out-of-bounds requests refused rather than clamped |
| `imaging_ports.py` | two vendors' resize rules as pure dimension arithmetic, pinned against the vendors' own functions |
| `chandra_presentation.py` | how Chandra's own pipeline presents a whole page |
| `background.py` | the paper value every stage that reads ink infers the same way |
| `components.py` | connected-component labelling over ink pixels |
| `residual_ink.py` | the ink measurement the Ink Map, the page accounting, the Recensor and the Armarium share |

**Witness reports**

| Module | What it holds |
|---|---|
| `native_witness.py` | the closed waist derived from a witness's raw response: what it was shown and the page boxes it reported |
| `chandra_layout.py` | Chandra's own prompt and a reader for its layout answer |
| `churro_document.py` | Churro's own request framing and a reader for its answer |
| `dots_layout.py` | dots.mocr's own prompt, a reader for its layout-cell answer, its text view and how its boxes map back to the page |
| `witness_routing.py` | which pages a routed witness reads (dots.mocr on index and table pages), decided from the Designator's evidence, and each page's roster under it |
| `alignment.py` | loss-accounted comparison views of witness text |
| `page_witness_units.py` | each witness's page broken into its own units, re-derived from retained bytes |
| `page_testimonia.py` | the page-witness roster and each page's current, validated Testimonia, as every consumer reads them |

**The page path (Perlector)**

| Module | What it holds |
|---|---|
| `page_feed.py` | the feed: what one whole-page reading is shown, under the sealed `[feed]` switches |
| `page_render.py`, `page_overlay.py` | the page render the reading sees, and the labelled copy of it the overlay draws ids on |
| `page_prompt.py` | the request text rendered from a feed, with the pinned instruction |
| `page_answer.py` | the page answer grammar, read and never repaired |
| `page_types.py` | page types and entry kinds: each kind's act class, which checks apply per type, the type cross-check facts and the `rows.jsonl` line |
| `page_accounting.py` | the page accounting: the model-free check that a reading accounted for everything on its page |
| `page_path.py` | everything the Perlector's page records derive, for the stage that writes them and every reader that checks them |
| `page_reask.py` | the plan of a page's one re-ask: which ids it names and what it shows |
| `page_reread.py` | which pages a person asked the Perlector to read again |
| `truncation.py` | the truncation instrument: `complete`, `truncated` or `unknown`, and `unknown` holds |
| `perlector_audit.py` | the audit declaration's schemas, the truncation verdict and a call-record decoder |
| `reading_annotations.py` | uncertain spans and gaps over one clean text, and the doubt-mark reading of an answer |
| `dissent.py` | where a reading departs from each witness, within a sealed step budget |
| `page_edges.py` | a page's edge acts and the page breaks between them |

**Review and establishment**

| Module | What it holds |
|---|---|
| `page_review.py` | the shape of the Recensor's reviews and continuation links, and how the Archetypus and Armarium read them |
| `review_decisions.py` | how operator review decisions apply on top of the Recensor's review |
| `correction.py` | a person's correction as the stages after the Recensor carry it |
| `recensor_receipt.py` | the Recensor's self-hashed partition receipt over the counted units |

**Coniector**

| Module | What it holds |
|---|---|
| `reconstruction.py` | the Coniector's model-free rules: what each call asks and how an answer applies |
| `reconstruction_prompt.py` | the text-only request |
| `reconstruction_answer.py` | the answer grammar, checked against its call |
| `reconstruction_records.py` | the Coniector's records, how they are derived and how the Armarium verifies them |

## Page-read denominator

What a run counts is decided once, in `stage.py`, for every stage after the Perlector
(the Recensor, Archetypus and Armarium call it; the orchestrator counts nothing).
`reading_denominator(context)` returns `{"pages": ..., "acts": ...}`;
`page_readings(context)` and `reading_acts(context)` give the two halves. Each verifies the
whole run, once per stage context. They read only the Perlector's page path
(`pipeline/4_perlector/CONTRACT.md`, "Page reading") behind its verified stage seal; any
other Perlector record kind is refused.

**`page_readings`** has one row per sealed Exemplar page (`PAGE_READING_ROW_FIELDS`):
`{page_id, page_ordinal, parse_state, disposition, finish_reason, reading_ref, feed_ref,
accounting_ref, reask_ref, trigger_accounting_ref, entry_count}`.

- Every sealed page must have its first `page-reading` (`page-read:1`) and its
  `page-accounting`; a page with none is `FatalAccounting`, never zero acts. Every
  submitted ordinal must have an Exemplar page.
- The machine's re-ask (`page-read:2`; `pipeline/4_perlector/CONTRACT.md`, "The re-ask")
  exists exactly when `page_reask.reask_plan`, run again over the verified first reading and
  accounting under the sealed `page_level_reread`, names ids.
- Operator re-reads (`page-read:3` on) are outside that budget: a current stored page re-ask
  decision requests each one. Each is verified like a first reading, has its own accounting,
  and supersedes every earlier reading; the latest supplies the page's counted entries.
  Earlier readings stay in the run tree as history and are not counted.
- Attempts of each kind must run 1..N with no gap; nothing chooses "the latest". A stray or
  missing attempt is `FatalAccounting`.
- `parse_state`, `disposition`, `finish_reason` and `reading_ref` are the current
  reading's; `accounting_ref` is the page's last accounting; `reask_ref` and
  `trigger_accounting_ref` name the re-ask and the accounting that planned it (else
  `None`); `entry_count` is the entries the last accounting counts (`None` when the first
  answer was not read).

**`reading_acts`** has one closed row (`READING_ACT_FIELDS`) per unit, in page order:

| Field | Value |
|---|---|
| `act_id` | the act identity (`contracts/identities.py`); `None` for `page-refused` |
| `act_key` | `p<ordinal>:<n>`, or `p<ordinal>:unread` / `:blank` / `:refused` |
| `page_id`, `page_ordinal` | the page |
| `n` | the entry's number in the page's last accounting (a re-ask's entry `j` after `k` first-answer entries is `k + j`); `None` for a page row |
| `reading_attempt`, `reading_n` | the reading the entry came from (1, 2 for the re-ask, 3 on for an operator re-read) and its number there |
| `kind` | `act` or `other`; `act` for `page-unread` and `page-blank`, `None` for `page-refused` |
| `class` | `reading`, `reading-unplaced`, `page-unread`, `page-blank` or `page-refused` |
| `disposition` | `read` only when nothing holds the unit, else `held`; `refused` for `page-refused` |
| `region_ref`, `perlectio_ref` | the entry's `act-region` and `perlectio.v3`; `None` for a page row |
| `reading_ref` | the entry's own `page-reading`; for a page row the first reading |
| `accounting_ref` | the page's last `page-accounting`; `None` for `page-refused` |
| `hold_codes` | sorted: the recomputed accounting's `holds`, the entry's own holds, the page row's hold |
| `flag_codes` | sorted: the accounting's `flags` and findings the sealed `[flags]` policy records without holding (`config/page_accounting.toml`) |
| `continues_from_previous_page`, `continues_to_next_page` | the answer's flags; `None` for a page row |

Every submitted page contributes at least one row:

- a first reading that is not a parsed, valid answer: one `page-unread` row, held;
- a read answer that counts no entry: one `page-blank` row, held
  (`page-blank-unconfirmed`) until the Recensor confirms the page blank;
- a read answer whose entries are all `other`: one row per entry, each held
  (`no-act-on-page-unconfirmed`) until the Recensor confirms the page holds no act;
- a page the Exemplar refused: one `page-refused` row, proven from its one `not-run`
  reading (problem `page-not-sealed`). It records the Door's refusal and is never counted as
  an act (`COUNTED_READING_CLASSES` names the counted classes).

The Recensor's partition receipt (`recensor-partition-receipt.v6`, `recensor_receipt.py`)
counts these units. A held unit whose review is completed with a named `release_reason` is
resolved; one with no completed review keeps the receipt `partial`.

**Nothing that decides what is counted or held is taken from a record.** Each is recomputed
with stage 4's own derivations (`page_path.py`), so the writer and the counter cannot read a
page two ways:

- the feed, rebuilt by `page_path.page_feed_of` from the sealed inputs; its render must be
  the bytes stage 4 retained;
- the reading, re-read with `page_path.read_reply` from the retained response bytes (parsed
  by the `stage.ServingReader` the context was opened with, since `common/` never imports
  the serving package) or a fixture's declared answer;
- the re-ask, rebuilt by `page_path.reask_record` and its reply re-read against the named
  ids;
- the accounting, re-measured by `page_accounting.page_accounting` from
  `page_path.accounting_inputs`; a re-asked page's two accountings are both re-measured;
- each entry (`page_path.entry_plans`): ids, region, act id, text, doubt marks, truncation
  and holds re-derived. Its `act-region` and `perlectio` must match, and every Perlectio
  field but dissent must equal `page_path.expected_perlectio`'s. Dissent is recomputed under
  the sealed `[dissent] max_comparison_steps` (`config/alignment.toml`), a counted budget,
  never a clock, so it aligns the same everywhere. A placed region's crop is proven from the
  Exemplar by `exemplar_boundary.verify_reading_region_lineage`.

What stage 4 recorded about serving the call (`request_digest`, `sampling`, `capacity`,
`audit`, `provenance`, and the named call record) is bound, not recomputed; the Perlectio
must repeat `engine_call` and `provenance` exactly. Fixture and real runs take the same path.

**Related readers.** `page_review.py` holds the shape of the Recensor's page-path records
and is their one reader for the Archetypus and Armarium (`current_page_reviews`,
`reviewed_rows`, `require_establishable`, `page_breaks`, `continuation_links`; breaks and
links use only each page's current reading's `act` entries). `page_testimonia.py` reads the
sealed page-witness roster, each page's current validated Testimonia, and
`shown_page_witnesses`, the custody check that every witness a feed showed is its chair's
current Testimonium.
