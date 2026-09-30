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

Code enters here **when a second stage needs it** — not in anticipation. Moving
something in is its own pull request, because this is the one place two agents can
genuinely collide.

## Page-read denominator

What a run counts is decided once, in `stage.py`, for every stage after the
Perlector: `reading_denominator(context)` reads the sealed Perlector protocol's
`reading_unit` and returns either `{"reading_unit": "act", "acts":
expected_acts(context)}` (the Designator's proposal seal) or `{"reading_unit":
"page", "pages": ..., "acts": ...}` (the acts the Perlector established on each
page it read whole). The two are never mixed: an act-read tree holding a
`page-reading`, or a page-read tree holding a Perlectio other than `perlectio.v2`,
is refused. `page_readings(context)` and `reading_acts(context)` give the two
halves of the page form alone. The records they read are the Perlector's page
path (`pipeline/4_perlector/CONTRACT.md`, "Page reading").

`page_readings` has one row per sealed Exemplar page, by ordinal:
`{page_id, page_ordinal, parse_state, disposition, finish_reason, reading_ref,
feed_ref, accounting_ref, act_count}`. Each sealed page must have exactly one
`page-reading`, at attempt `page-read:1`, and its `page-accounting`; a sealed page
with none is `FatalAccounting`, never zero acts. A later attempt is refused until
its place is designed. `act_count` is the answer's entries (`act` and `other`),
`None` when the answer was not read. A page the Exemplar refused counts nothing
and may carry only a `not-run` reading.

`reading_acts` has one closed row (`READING_ACT_FIELDS`) per counted unit, in page
order:

| field | value |
|---|---|
| `act_id` | the act identity (`contracts/identities.py`) |
| `act_key` | `p<ordinal>:<n>`, or `p<ordinal>:unread` / `p<ordinal>:blank` |
| `page_id`, `page_ordinal` | the sealed page |
| `n` | the entry's number in the answer; `None` for a page row |
| `kind` | `act` or `other` as the answer gave it; `act` for a page row |
| `class` | `reading`, `reading-unplaced`, `page-unread` or `page-blank` |
| `disposition` | `read` only when nothing holds the unit, else `held` |
| `region_ref` | the entry's `act-region`; `None` for a page row |
| `reading_ref`, `accounting_ref` | the page's `page-reading` and `page-accounting` |
| `perlectio_ref` | the entry's `perlectio.v2`; `None` for a page row |
| `hold_codes` | sorted: the page accounting's `holds` and the Perlectio's own `holds` |
| `continues_from_previous_page`, `continues_to_next_page` | the answer's flags; `None` for a page row |

Every sealed page contributes at least one row. A page whose reading is not a
parsed, valid answer is one `page-unread` row (held: `page-unread`, the reading's
problem codes and the page's holds). A read answer with no entry is one
`page-blank` row, held (`page-blank-unconfirmed`) until the Recensor confirms the
page blank. Both bind the sealed page rectangle, as `page-fallback` does.

Nothing is trusted from the records it recomputes. For each page the answer is
read again against the sealed `page-feed` (`page_accounting.validate_answer` over
`feed_candidates`): the reading's problems and disposition must be exactly what
that gives. For each entry the cited ids are re-expanded (never read from
`cited_ids`), the union box is recomputed from `placement_boxes`, the act id is
re-derived from `{page_reading, n, union_box_px}`, and its `act-region` and
`perlectio` must match the entry, name this reading, accounting and feed, carry the
accounting's holds as `page_holds`, and number the entries `1..k` with no record
beyond them. A placed region's crop is proven from the Exemplar by
`exemplar_boundary.verify_reading_region_lineage`. Fixture and real runs take the
same path.

The reader lives here rather than in the orchestrator's contract because it is a
`common/` function three stages call (Recensor, Archetypus, Armarium); the
orchestrator counts nothing.
