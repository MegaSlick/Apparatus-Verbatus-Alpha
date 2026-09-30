# common

The only project code a stage may import besides its own.

It knows nothing about stages. Stages import it; it never imports back. That is
now checked rather than declared: `common/chairs/test_chairs_import_boundary.py`
reads every `.py` file under `common/` through `ast` and fails on an import of
`pipeline`, wherever in the file it sits. Static, because numbering the stage
directories only makes `import 4_perlector` invalid — a dynamic import would
still cross.

| Module | What a stage gets from it |
|---|---|
| `contracts/` | the envelope, the one canonical serialization, identities, the outcome algebra |
| `runtree/` | the run's evidence: immutable artifacts, atomic publication, run receipts |
| `chairs/` | a named role resolved to one pinned model artifact, verified by digest |
| `stage.py` | argument shape, opening a run, publishing with the envelope filled in |
| `imaging.py` | decoding and cropping, with bounds refused rather than clamped |
| `image_sniff.py` | the one byte-signature table for page sources and the "could be a page" test the submit door and the Exemplar door share |
| `imaging_ports.py` | the two vendor resize rules a witness's own preprocessing applies, as pure dimension arithmetic — carried third-party logic, cited to its pinned commit, with `common/test_vendor_parity.py` pinning both against the vendors' own functions. Its designed consumers are the Designator's structure chair and the Attestatores' Chandra and Churro adapters; both adapters now size their presented page through it, and the structure chair follows. The pixels still move through `imaging.py`'s sealed recipe |
| `request_capacity.py` | whether one reading request fits the sealed serving row it would be sent to: the chair's own image-token arithmetic, the measured prompt and answer costs, and a closed record naming the headroom. Three stages ask it the same question before they send. A request is admitted on a measured constant or a measured **upper** bound and never on a floor; where a chair has both, the record carries both with their bases named |
| `credentials.py` | one reading of "this looks like a secret": the name markers, provider prefixes and the value shape test every credential screen shares, operational and pipeline alike |
| `armarium_formats.py` | the sealed Armarium projection choices — the door binds them into the run, the Armarium reads them back |
| `chandra_custody.py` | one-receipt Chandra custody: the Designator's live structure pass writes it; the read half has no served caller since the capture intake was removed and is the half that defines what the binding admits. It deliberately names the Designator's blob root and serving chair — those constants come from `contracts/`, `runtree/` and `stage.py`, never from a stage module, so the import boundary above holds |
| `page_path.py` | the Perlector's page path as derived data: an answer's problems, each entry's plan and holds, and every input the page accounting measures. Stage 4 publishes from it; the page-read denominator recomputes stage 4's records with it |
| `page_witness_units.py` | each witness's page broken into its own units, re-derived from the Testimonium's retained bytes: what the page feed shows and the page accounting measures |
| `truncation.py`, `reading_annotations.py` | the truncation instrument and the doubt-mark reading of a Perlector answer: stage 4 applies them, the page-read denominator re-derives an entry's holds with them |

Code enters here **when a second stage needs it** — not in anticipation. Moving
something in is its own pull request, because this is the one place two agents can
genuinely collide.

## Page-read denominator

What a run counts is decided once, in `stage.py`, for every stage after the
Perlector: `reading_denominator(context)` reads the sealed Perlector protocol's
`reading_unit` and returns either `{"reading_unit": "act", "acts":
expected_acts(context)}` (the Designator's proposal seal) or `{"reading_unit":
"page", "pages": ..., "acts": ...}` (the acts the Perlector established on each
page it read whole). The two are never mixed: an act-read tree holding any
page-path record (`page-feed`, `page-reading`, `page-accounting`, `act-region`
or a `perlectio.v2`), or a page-read tree holding a Perlectio other than
`perlectio.v2`, is refused. `page_readings(context)` and `reading_acts(context)`
give the two halves of the page form alone; each verifies the whole run, so a
caller needing both takes `reading_denominator`, and one stage context is
verified once however often it asks. The records they read are the Perlector's
page path (`pipeline/4_perlector/CONTRACT.md`, "Page reading"), behind its
verified stage seal.

`page_readings` has one row per sealed Exemplar page, by ordinal
(`PAGE_READING_ROW_FIELDS`): `{page_id, page_ordinal, parse_state, disposition,
finish_reason, reading_ref, feed_ref, accounting_ref, entry_count}`. Each sealed
page must have exactly one `page-reading`, at attempt `page-read:1`, and its
`page-accounting`; a sealed page with none is `FatalAccounting`, never zero acts.
A later attempt is refused until its place is designed. `entry_count` is the
answer's entries, `act` and `other` alike, `None` when the answer was not read.
Every submitted ordinal must have an Exemplar page.

`reading_acts` has one closed row (`READING_ACT_FIELDS`) per unit, in page order:

| field | value |
|---|---|
| `act_id` | the act identity (`contracts/identities.py`); `None` for `page-refused` |
| `act_key` | `p<ordinal>:<n>`, or `p<ordinal>:unread` / `:blank` / `:refused` |
| `page_id`, `page_ordinal` | the page |
| `n` | the entry's number in the answer; `None` for a page row |
| `kind` | `act` or `other` as the answer gave it; `act` for `page-unread` and `page-blank`, `None` for `page-refused` |
| `class` | `reading`, `reading-unplaced`, `page-unread`, `page-blank` or `page-refused` |
| `disposition` | `read` only when nothing holds the unit, else `held`; `refused` for `page-refused` |
| `region_ref` | the entry's `act-region`; `None` for a page row |
| `reading_ref` | the page's `page-reading` (a refused page's `not-run` reading) |
| `accounting_ref` | the page's `page-accounting`; `None` for `page-refused` |
| `perlectio_ref` | the entry's `perlectio.v2`; `None` for a page row |
| `hold_codes` | sorted: the recomputed page accounting's `holds`, the entry's own holds, and the page row's hold |
| `continues_from_previous_page`, `continues_to_next_page` | the answer's flags; `None` for a page row |

Every submitted page contributes at least one row. A page whose reading is not a
parsed, valid answer is one `page-unread` row (held: `page-unread`, the reading's
problem codes and the page's holds). A read answer with no entry is one
`page-blank` row, held (`page-blank-unconfirmed`) until the Recensor confirms the
page blank. Both bind the sealed page rectangle, as `page-fallback` does. A read
answer whose entries are all `other` keeps one row per entry, each also held
(`no-act-on-page-unconfirmed`) until the Recensor confirms the page holds no
act. A page the Exemplar refused is one `page-refused` row: the Door's refusal
recorded, not an act and never counted as one (`COUNTED_READING_CLASSES` names
the classes that are), proven from its one `not-run` reading (attempt
`page-read:1`, problem `page-not-sealed`, the Exemplar's refused page its only
input), with no accounting or act record naming the page.

Nothing is trusted from the records it recomputes; each is recomputed with
stage 4's own derivations (`page_path.py`), so the writer and the counter
cannot read a page two ways:

- the reading: a parsed answer's problems are exactly
  `page_path.answer_problems` (its ids against the sealed `page-feed`, and
  `no-stop-reason` when the engine gave no finish reason), its disposition
  `read` exactly when there are none, and a parsed answer never has stop
  reason `length` (the Perlector makes that `cut-off`); a problem without a
  non-empty string code is refused;
- the accounting: measured again by `page_accounting.page_accounting` from the
  same sealed inputs stage 4 measured it from (`page_path.accounting_inputs`:
  the feed, every current page Testimonium of the page shown or hidden, the
  Designator's Surya and detector records, the Ink Map's runs, the entries'
  truncations, under the sealed policy); its payload and inputs must be the
  sealed ones exactly, and every hold code and disposition the rows carry is
  this recomputed verdict;
- each entry (`page_path.entry_plans`): cited ids re-expanded, union box and
  act id re-derived, text and doubt marks re-read, truncation re-classified,
  and its own holds recomputed. Its `act-region` and `perlectio` must match
  the entry, name this reading, accounting and feed, carry the page's holds,
  and number the entries `1..k` with no record beyond them; the Perlectio's
  holds, text, uncertainty, truncation, autopsia, `engine_call` and
  `provenance` must be exactly the recomputed ones. A placed region's crop is
  proven from the Exemplar by `exemplar_boundary.verify_reading_region_lineage`.

The run tree binds every record read to this run's configuration. Rule (e) of
the accounting is bounded by its sealed work budget (`max_alignment_steps`),
counted rather than timed, so the same inputs measure the same way here as in
stage 4 on any machine. Fixture and real runs take the same path.

The reader lives here rather than in the orchestrator's contract because it is a
`common/` function three stages call (Recensor, Archetypus, Armarium); the
orchestrator counts nothing.
