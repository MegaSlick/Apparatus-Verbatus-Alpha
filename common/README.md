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

What a run counts is decided once, in `stage.py`, for every stage after the
Perlector: `reading_denominator(context)` returns `{"pages": ..., "acts": ...}`,
the acts the Perlector established on each page it read whole. A Perlector record
of any kind but the page path's (`page-feed`, `page-reading`, `page-accounting`,
`act-region`, a `perlectio.v3`, a live call's `reader-sent` and the stage boundary
records) is refused. `page_readings(context)` and `reading_acts(context)`
give the two halves of the page form alone; each verifies the whole run, so a
caller needing both takes `reading_denominator`, and one stage context is
verified once however often it asks. The records they read are the Perlector's
page path (`pipeline/4_perlector/CONTRACT.md`, "Page reading"), behind its
verified stage seal.

`page_readings` has one row per sealed Exemplar page, by ordinal
(`PAGE_READING_ROW_FIELDS`): `{page_id, page_ordinal, parse_state, disposition,
finish_reason, reading_ref, feed_ref, accounting_ref, reask_ref,
trigger_accounting_ref, entry_count}`. Each sealed page must have its first
`page-reading` (attempt `page-read:1`) and that reading's `page-accounting`; a
sealed page with none is `FatalAccounting`, never zero acts. The page's re-ask
(`page-read:2`, `pipeline/4_perlector/CONTRACT.md`, "The re-ask") and the
accounting of both readings exist exactly when `page_reask.reask_plan`, run
again over the verified first reading and accounting under the sealed
`page_level_reread`, names ids. Each kind's attempts must run 1..N without a
gap (`attempt_ordinals`, the check `latest_attempt` makes), and nothing
chooses the latest: a stray or missing attempt is `FatalAccounting`. Each
operator re-read (attempt `page-read:3` on, without a gap) is verified like a
first reading, with its own accounting; it must answer stored page re-asks of
the page and supersede every earlier reading, and the last one is the page's
current whole-page reading, whose entries are counted. `parse_state`,
`disposition`, `finish_reason` and `reading_ref` are the current whole-page
reading's (the first reading, or the last operator re-read); `accounting_ref`
is the page's last accounting (the re-ask's on a re-asked page);
`reask_ref` and `trigger_accounting_ref` name the re-ask and the first
accounting that planned it, both `None` on a page not re-asked or whose
current reading is an operator re-read. `entry_count` is the entries the last
accounting counts, `act` and `other` alike, `None` when the first answer was
not read. Every submitted ordinal
must have an Exemplar page.

`reading_acts` has one closed row (`READING_ACT_FIELDS`) per unit, in page order:

| field | value |
|---|---|
| `act_id` | the act identity (`contracts/identities.py`); `None` for `page-refused` |
| `act_key` | `p<ordinal>:<n>` with the accounting's `n`, or `p<ordinal>:unread` / `:blank` / `:refused` |
| `page_id`, `page_ordinal` | the page |
| `n` | the entry's number in the page's last accounting: its number in the first answer, or `k + j` for the re-ask's entry `j` after the first answer's `k`; `None` for a page row |
| `reading_attempt`, `reading_n` | the reading the entry came from (1, 2 for the re-ask, 3 on for an operator re-read) and its number there; `None` for a page row |
| `kind` | `act` or `other` as the answer gave it; `act` for `page-unread` and `page-blank`, `None` for `page-refused` |
| `class` | `reading`, `reading-unplaced`, `page-unread`, `page-blank` or `page-refused` |
| `disposition` | `read` only when nothing holds the unit, else `held`; `refused` for `page-refused` |
| `region_ref` | the entry's `act-region`; `None` for a page row |
| `reading_ref` | the entry's own `page-reading` (the re-ask's for a recovered entry); for a page row the first reading (a refused page's `not-run` reading) |
| `accounting_ref` | the page's last `page-accounting`; `None` for `page-refused` |
| `perlectio_ref` | the entry's `perlectio.v3`; `None` for a page row |
| `hold_codes` | sorted: the recomputed page accounting's `holds`, the entry's own holds, and the page row's hold |
| `flag_codes` | sorted: the recomputed page accounting's `flags`, the findings the sealed `[flags]` policy records without holding (`config/page_accounting.toml`); empty for `page-refused` |
| `continues_from_previous_page`, `continues_to_next_page` | the answer's flags; `None` for a page row |

On a page an operator re-read, the last re-read stands for the first reading and its
re-ask in everything below: its entries are the page's rows, its accounting is the
page's last, and "first reading" and "first answer" mean that re-read. The superseded readings' act
records stay in the run tree and are not counted.

Every submitted page contributes at least one row. A page whose first reading is
not a parsed, valid answer is one `page-unread` row (held: `page-unread`, the
reading's problem codes and the page's holds); the re-ask never changes that,
since only a read first answer is re-asked. A read first answer whose readings
count no entry -- none of its own and none from a re-ask the accounting counts
-- is one `page-blank` row, held (`page-blank-unconfirmed`) until the Recensor
confirms the page blank. Both bind the sealed page rectangle. A read
answer whose entries are all `other` keeps one row per entry, each also held
(`no-act-on-page-unconfirmed`) until the Recensor confirms the page holds no
act. A page the Exemplar refused is one `page-refused` row: the Door's refusal
recorded, not an act and never counted as one (`COUNTED_READING_CLASSES` names
the classes that are), proven from its one `not-run` reading (attempt
`page-read:1`, problem `page-not-sealed`, the Exemplar's refused page its only
input), with no accounting or act record naming the page.

The Recensor's v5 receipt (`recensor_receipt.py`; v4 is still read) counts
these units, and binds each page's first reading, re-ask and last accounting
with what the re-ask did. A held
unit whose review is completed with a named `release_reason` is resolved and
adds no reason; a held unit with no completed review keeps the receipt
`partial`. A run whose genuinely blank page the Recensor confirmed can
therefore be `complete`.

Nothing that decides what is counted or how it is held is taken from a record;
each is recomputed with stage 4's own derivations (`page_path.py`), so the
writer and the counter cannot read a page two ways:

- the feed: built again by `page_path.page_feed_of` from the sealed page, the
  protocol's `[feed]` switches, the witness roster, each chair's current page
  Testimonium, the Surya census and the Perlector chair; its page render must
  be the bytes stage 4 retained, and the sealed feed (`feed_digest` included)
  and its inputs must be exactly these;
- the reading: its parse state, answer, problems, finish and stop reason are
  read again with `page_path.read_reply` from what the engine said -- a live
  reading's retained response bytes (`page_path.retained_reply`, parsed by the
  `stage.ServingReader` the stage opened its context with, since `common/`
  never imports the serving package), a fixture run's declared page answer --
  and must be the recorded ones. A page not asked
  has exactly `page_path.not_run_problems`; a refused or failed call records
  only that, with no call and no answer. The disposition is `read` exactly when
  the answer parsed and nothing holds it; a problem without a non-empty string
  code is refused;
- the re-ask: its `reask` must be `page_path.reask_record` over the recomputed
  plan, the first reading's entries (`page_reask.render_reask`) and the re-ask
  prompt builder, bound to the first reading and accounting; its reply is read
  again against the named ids (a fixture run's from its `[[page_reask_answer]]`
  row), and its request digest, capacity (`page_path.reask_request_capacity`)
  and engine call are the re-ask's;
- the accounting: measured again by `page_accounting.page_accounting` from the
  same sealed inputs stage 4 measured it from (`page_path.accounting_inputs`:
  the feed, every current page Testimonium of the page shown or hidden, the
  Designator's Surya and detector records, the Ink Map's runs, the entries'
  truncations, under the sealed policy); its payload and inputs must be the
  sealed ones exactly, and every hold code and disposition the rows carry is
  this recomputed verdict. A re-asked page's two accountings are both
  measured again, the combined one restating the first one's entries
  exactly, and its entries must be exactly the ones the rows count
  (`page_path.reask_act_plans`): the first reading's, then the re-ask's
  only when the accounting counts it;
- each entry (`page_path.entry_plans`, a re-ask's against its named ids and
  numbered on after the first reading's): cited ids re-expanded, its region
  (`region_boxes_px`, the boxes every rule measures), union box and act id
  re-derived, text and doubt marks re-read, truncation re-classified,
  and its own holds recomputed. Its `act-region` and `perlectio` must match
  the entry, name its own reading and the page's last accounting and feed,
  carry the page's holds (a recovered entry's also `reading_attempt: 2` and
  `reading_n`), and number the entries `1..k` with no record beyond them;
  every field of the
  Perlectio but its dissent must be `page_path.expected_perlectio`'s, the
  function stage 4 publishes and adopts it by, and its dissent is computed
  again (`page_path.dissent_holds`) under the run's sealed `[dissent]
  max_comparison_steps` (`config/alignment.toml`) and must be exactly the
  sealed one: the budget counts the matcher's work, never a clock, so the same
  texts and budget align the same way everywhere. Neither record
  may carry a field beyond its schema. A placed region's crop is proven from
  the Exemplar by `exemplar_boundary.verify_reading_region_lineage`.

What stage 4 recorded about serving the call is bound, not recomputed: the
reading's `request_digest`, `sampling`, `capacity`, `audit` and `provenance`,
and the call record its `engine_call` names (stage 4 held it to the sealed
sampling row), are read as recorded; the Perlectio must repeat `engine_call`
and `provenance` exactly.

The run tree binds every record read to this run's configuration. Rule (e) of
the accounting is bounded by its sealed work budget (`max_alignment_steps`),
counted rather than timed, so the same inputs measure the same way here as in
stage 4 on any machine. Fixture and real runs take the same path.

The reader lives here rather than in the orchestrator's contract because it is a
`common/` function three stages call (Recensor, Archetypus, Armarium); the
orchestrator counts nothing.

`page_review.py` holds the shape of the Recensor's page-path records (the
closed page-review field set, the release and note shapes, the releasable holds
and the `recensor-continuation-link.v1` fields), which the Recensor writes, and
is the one reader of them for the two stages after it: `current_page_reviews`
(one current review per row, each in the closed shape and naming its row's key,
class, kind, page and records, held exactly when it names a hold code),
`reviewed_rows` (every counted row, so not a refused page's),
`require_establishable` (an accepted review stands over a `read` row, or over a
row whose only hold is `no-act-on-page-unconfirmed` and whose review names it
in its `release`), `page_breaks` and `continuation_links` over each page's
current whole-page reading's `act` entries only (its first reading, or the
operator re-read that superseded it), since a recovered entry's place in page
order is not established (one per break, each inputting its named
sides' readings), and the review's reason, coverage and notes.

`page_testimonia.py` reads the page witnesses: the sealed page roster and its
check that a page carries exactly the roster (both defined in `page_path.py`,
whose feed applies them), each page's current validated page Testimonia, and
`shown_page_witnesses`, the custody check that every witness a page reading's
feed showed is its chair's current page Testimonium under this run's label.
