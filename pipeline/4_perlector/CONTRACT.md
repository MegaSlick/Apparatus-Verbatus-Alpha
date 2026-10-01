# Perlector — contract

The Perlector reads every sealed Exemplar page whole, once, and establishes the
acts on it (`page_run.py`, opened by `run.py`). Per page it writes a `page-feed`,
a `page-reading` and a `page-accounting` under `4_perlector/artifacts/`, and on a
live pass a `reader-sent` record before the call leaves; per entry of a valid
answer it writes one `act-region` and one `perlectio` (`perlectio.v3`). The
records are `skeleton.v1` envelopes with derived identities, attempt bindings,
self-hashes and checked direct inputs. On the fixture route the answers come from
the declared synthetic fixture and prove wiring only; they claim no model reading.

**Successors consume this stage's artifacts, not its implementation.** The Recensor
reviews each unit this stage established (`pipeline/5_recensor/CONTRACT.md`,
"Page-read review"), and the Archetypus and Armarium follow the references it
records. No successor imports Perlector code.

## Stage-completion seal

Before this producer's final manifest it publishes one `decode-environment` and
one `stage-seal`, or reuses both on a byte-identical retry. The seal witnesses
this pass's disk inventory and blob contents, and binds the exact decode-environment
bytes, run `config_digest` and `register_digest`, and `(kind, outcome)` census. An exit
held after publishing stage evidence seals it (holds remain in its census); a
pass that never reaches its seal does not seal, whether it was held or refused
before publishing stage evidence or closed fatally after publishing it, so the
successor correctly refuses the missing boundary.

Seals are compared as the SET the stored inventory names, on both sides of the
boundary: the producer refuses to re-seal, and the successor refuses to read,
when any named seal is no longer on disk. Ordinals are the contiguous run 1..N,
so removing the latest leaves a prefix that still looks whole — and the earlier
statement would then answer for a boundary it never witnessed.

## Input boundary

The stage reads every sealed Exemplar page, each page's current page Testimonium per
chair, Surya's stage-2 records, the Designator's detector records and the Ink Map's
runs ("Page reading", below). It does not select a preferred witness or use witness
agreement to choose its text: every witness is shown to the reader as a clue, and
dissent is measured after the reading is fixed.

### Native witness intake

The consumer validates the same closed `presented`/`observed` waist the
Attestatores writes. Page ids and ordinals are reconciled to the sealed Exemplar;
whole-page and adapter-crop transforms are executable; observed boxes
are bounded integer sealed-page coordinates; spans address retained text; and
preferences, floats, unknown fields, malformed ordinals, and overlapping spans
are refused. The page-Testimonium read additionally applies the shared full
payload allowlist and validates Attestatores provenance/receipt requirements,
not only geometry. A page outcome in `read | genuinely-empty | failed` is
attempted and receipt-backed; `not-run` is explicitly unpresented and receipt-free.

A native box that runs past the sealed page edge is kept as reported, never
clamped, as a `page_edge_overshoots` finding on the page Testimonium
(`common/native_witness.py`, `split_page_edge_overshoots`).

## Page reading

This stage reads every Exemplar
page (`common.stage.exemplar_page_ids`), not only pages with a Designator act, once,
and the Perlector establishes the acts on it.

**Recorded, not run:** the sealed Pass-C audit policy (`config/perlector_audit.toml`,
read by `audit.load`). Every `page-reading` carries
`audit: {state: "not-run", round_cap, policy_sha256, reason}`.

### Inputs

- Each page's current `page-testimonium` per chair (`latest_per_chair`), every one
  validated (`validate_page_testimonium_record`, and the native capture's blob and
  adapter). Every chair the sealed roster scopes `page`
  must have one; a chair it does not scope `page` must not. A page with no page
  Testimonium at all (the sealed roster seats no page witness, or the Attestatores
  recorded none for it) is not refused:
  it is fed with `witness_testimony: "none"` and no witness row, and held by name
  (`no-witness-testimony`). A roster chair missing beside others that testified is a
  shortened roster and refuses.
- Each shown witness's units are re-derived from its capture's retained bytes
  (`common/page_witness_units.py`, `_checked_capture`), never taken from the
  capture's own fields. A capture read under a vendor grammar names the `text_view`
  its parse was read under (`pipeline/3_attestatores/CONTRACT.md`, "The capture's
  text view"); one naming a retired view, or none where its grammar has one, is
  refused by that name, whatever its parse state, before it is re-derived, here and
  in `common/page_testimonia.py`'s `verify_page_native_capture` alike, and the
  refusal says to re-run the submission from the Door. Past that, a Chandra
  capture's parsed text and its grammar findings (every finding but the repetition
  scan's) must be what a fresh parse of its bytes gives, as a Churro capture's
  parse, findings and stop reason must be.
- Surya's stage-2 records, read in one place (`common/page_path.py`,
  `sealed_surya_census`): each
  `surya-page` census (`page_id`, `line_count`, `block_count`, `line_subjects`,
  `block_subjects`, `reading_order`, `reading_order_reason`) and every `surya-line`
  and `surya-block` it names (`n`, `bounds`, `confidence_bp`; a block also `label`,
  `reading_order_position` and the page's `reading_order`). The census and its
  detections must agree exactly -- counts, subjects, `n`, page, reading order -- and
  every sealed detection must be named by its page's census, or the stage refuses by
  name. A run with no `surya-page` record at all shows none and records it on the
  feed: `surya: {census_ref: null, absent: "no Surya page census was sealed in this
  run", block_sequence: null, block_sequence_reason: null, lines: [], blocks: []}`.
  A run with censuses but none for a page refuses. A layout Surya failed on is refused
  in stage 2, so every census has its blocks. The feed carries how they were
  sequenced as `block_sequence` (`surya-order-head`, or `raster-fallback` with
  `block_sequence_reason`; named so because the feed's preference test refuses any key
  naming an order), and for a raster fallback the prompt says the blocks are in raster
  order, not a reading order. Each line's and block's `confidence_bp` is recorded on the feed and never
  rendered into the prompt.
- The page image at the sealed `[feed] page_image`: `legible` is
  `common.page_render.build_page_render` at `[page_context] maximum_edge` (reason
  `legible-ink`); `full` is the sealed page at its own size (reason `full-page`,
  resampler `identity`); `off` is none.
- On a synthetic run only, a Chandra page joined from the fixture's act placeholders
  (no native capture) is read one unit per placeholder, box = its bbox widened to
  whole pixels (`page_witness_units._fixture_chandra_reading`).

Every box on these records is the repository's `bounds` `{x, y, w, h}` in sealed-page
pixels: the feed's `box_px`, an act-region's `region_boxes_px` and `union_box_px`, and every box the
accounting takes or reports. `common/page_accounting.py` reads corners from them
internally and nowhere else.

### Records, per page, in publication order

Every sealed page's feed is built and published before any page is read, so a live
pass counts exactly the pages it will send before its chair starts.

`kind="page-feed"` (subject page_id, no attempt, outcome `read`): the
`perlector-page-feed.v2` payload exactly as `page_feed.build_page_feed` returns it,
with `witness_testimony` (`present` or `none`) and `prompt` null when the Perlector
chair is absent or the feed shows nothing (`page_feed.shows_nothing`). Its inputs are
every Testimonium of the sealed page-witness roster -- a witness the `witnesses`
switch hides included, since the accounting measures it -- every Surya record, the
page render and the sealed page it names, each re-derived from the bytes on disk.

`kind="reader-sent"` (subject page_id, live only): the closed record ("Live reading", below) with
`act_key = "page-<ordinal>"`, `attempt_ordinal = 1`, `pass = "page-reading"`, and
`image_sha256s` the page render then the overlay, in the order sent.

`kind="page-reading"` (subject page_id, attempt `attempt_id(page_id, "page-read", 1)`):

```
{schema: "perlector-page-reading.v2", page_id, page_ordinal,
 feed_ref, request_digest, engine_call | null, sampling | null, capacity | null,
 finish_reason, stop_reason, parse_state, answer | null, problems: [{code, detail}], failure | null,
 disposition: "read" | "held", audit, provenance}
```

- `sampling` (live calls only; null on the fixture pass or when nothing was sent):
  `{chair: "perlector", sent, effective}`, the Perlector's sealed `chair_decoding`
  row that `ChairClient` put on the wire and the values the pinned engine samples
  under (`common.decoding.engine_effective_sampling`), in the call record's form
  (`common.decoding.recorded_wire_decimals`). The page call goes through `ChairClient`, attempt 1, no
  variance arm, with the serving receipt's seed; its call
  record is held to that row and seed (`verify_retained_call_sampling`) wherever
  stage 4 binds the reading's `engine_call`: when the reading is published, when a
  resumed pass adopts it, and when its act records are published. The row samples
  (Qwen's non-thinking values, temperature 0.7), so a second call would be a
  second draw; nothing on the page path asks twice.
- `parse_state`: `parsed` (the grammar read; `answer` is the object as given),
  `malformed` (`common.page_answer.parse_page_answer`'s problems), `cut-off` (engine
  `length`; `answer` null, never parsed), `refused-capacity` (nothing sent),
  `call-failed` (a page-local engine or transport failure; `failure` is
  `run._failure_record`'s record of it, and its retained response and call record
  are inputs),
  `not-run` (nothing asked; `problems` names every reason: `page-not-sealed` -- the
  Exemplar refused the page, and `feed_ref` is null since there is no feed --
  `chair-absent`, `no-witness-testimony`, `nothing-to-show`).
- `disposition` is `read` only for `parsed` with no problem; outcome is `read` or
  `held` accordingly. A parsed answer is read by `common/page_accounting.py`'s
  `validate_answer` against `feed_candidates`, the feed's ids placed by
  `placement_boxes` under the sealed `page-accounting` policy -- the one placement
  map, which the accounting measures against too. The answer grammar is
  `common/page_answer.py`'s alone (one label rule: absent, null, or non-blank text of
  at most 80 characters); the accounting calls it rather than keeping its own. Any
  problem holds the page with its
  answer and problems (`unknown-id`, `malformed-range`, `detection-range`, `cited-and-set-aside`,
  `set-aside-twice`, `set-aside-without-reason`, ...). A parsed answer whose engine
  gave no finish reason (`stop_reason` null) is kept and held with
  `no-stop-reason`. Two entries on one region are published, both held
  (`page_accounting.duplicate_regions`): regions are compared as ink, and two are
  one when the area both claim exceeds the policy's `max_shared_share_bp` of the
  smaller, so one inside the other, or the same ink named by other ids, holds.
- An entry's region is exactly the ink it names, id by id. A Surya line places an
  entry, and so does a witness unit shown in its own units whose box its text
  vouches for: non-empty normalized text, each character claiming at most the
  policy's `max_unit_area_per_character_bp` of the page. A Surya block places nothing
  (one block can be the whole page or hold several acts), nor does a textless unit, a
  short text on a large box, or any unit of a witness shown `flat`. Citing a line is
  checked no further than rule (d), which asks only that the line lie inside some
  entry's region, the truncation length signal (rule g), and rule (e) where a witness
  unit covers the same ink. The region is the list of the placing boxes, and
  every "inside" test and region area reads their union, never the rectangle around
  them. A range may name witness units only (`A2-A5`); Surya's lines and blocks are
  numbered by the detector, which interleaves the columns of a two-column page, so
  they are cited one by one, and a range over `L` or `S` ids, cited or set aside, is
  `detection-range`, which holds the reading whole. The prompt says so, and says the
  blocks are "in the reading order that detector predicted"; Surya's ids and order
  are shown exactly as recorded.
- The feed's `answer_measure` is `{longest_witness_characters, act_entries,
  surya_lines}`: each shown Surya line is reserved one cite of its own.
- `request_digest` = digest of `{image_sha256s, text_sha256}` of what was (or, in
  fixture mode, would be) sent; null when nothing was.
- `capacity`: live only, `common.request_capacity.page_request_capacity`'s
  `{capacity, answer_reserve, max_tokens}`, `answer_reserve` carrying the feed's
  `answer_measure` (so `surya_lines` too, which is what v2 adds), checked against the sealed serving row
  before the chair starts; on a refusal `{capacity: <record>, answer_reserve: null,
  max_tokens: null}`. The request sends that `max_tokens` with
  `chat_template_kwargs: {enable_thinking: false}`.
- On the fixture pass the answer is the fixture's `[[page_answer]]` row for the
  scenario and page; a row naming `witnesses` answers only a run whose page
  witnesses are exactly those chairs, and replaces a row that names none, since
  the ids an answer cites are lettered from the page witnesses.
- `finish_reason` is the engine's word (fixture: the declared `stop_reason`,
  default `stop`); `stop_reason` its mapping (`stop`, `length`, null). An
  unrecognized word is `call-failed` with code `ENGINE_FINISH_REASON_UNRECOGNIZED`.
- `provenance` is `provenance_for`, attempted for a sent (or fixture-answered) page.

`kind="page-accounting"` (subject page_id, attempt the page reading's own
`attempt_id(page_id, "page-read", n)`, so each reading of a page has its own
accounting), published for every page
that has a feed, whatever its reading's disposition, after the reading and before any
act record: `common.page_accounting.page_accounting`'s `page-accounting.v2` payload
under the sealed `page-accounting` policy (read at stage open through
`require_page_accounting_policy`). Outcome `held` when its `holds` is non-empty, else
`read`. Its inputs are the feed, the page reading, every page witness's Testimonium
(hidden ones included) and every sealed detection and ink-map record it measured.
Each entry's truncation classification is computed before it, without publishing
anything (`common/page_path.py`, `entry_plans`). It is given:

- the feed as published (boxes `{x, y, w, h}`), whose `switches.witness_units` it
  places entries by through the same `placement_boxes` the act-regions are cut from:
  under `witness_units = "flat"` a witness's units place nothing in either;
- every page witness: a shown one as its feed row, a hidden one read by
  `page_witness_units.witness_reading` and lettered with the next letter the feed did not use,
  in sorted `witness_label` order; `blank` is whether its retained page text is
  blank, measured from that text, when it read (`read` or `genuinely-empty`). A
  witness that read and gave no unit is recorded `witness-read-blank` when its text
  is blank, and held `witness-read-no-units` otherwise; only a witness that did not
  read is held
  `witness-not-read`. So DAI's page on which its own detector found nothing below
  its cap (`genuinely-empty`, Attestatores CONTRACT) is recorded, not held, by
  rule (c); on a page whose reading establishes acts, rule (i) holds it as
  `no-detector-record-on-act-page`;
- Surya's census (`null` when the run has none), with feed ids on what the feed
  showed; the record detector as `configured` when the sealed
  `secondary_proposer` is a chair, its page's `detector-record` boxes -- a record whose
  corners enclose no crop is given with no box, and rule (i) reports it
  `detector-record-not-measured` (held) and counts them as `records_not_measured`
  (always present: 0 with no detector, null when there are no records to count) --
  each record carrying the feed id of the DAI unit with its box, so a set-aside DAI
  unit is a set-aside record (`set-aside-record`); and census `{detection_count,
  max_det, max_det_reached}` with `max_det` from the detector's retained run facts,
  both `null` when the detector published no page record or its run facts state no
  `max_det` (the `detector-page` read is an input either way). The census and its
  records must agree -- count, subjects in detector order, page -- and every sealed
  record of the page must be named by its census, or the stage refuses by name; more
  than one ink map for a page refuses too;
- the reading's `parse_state`, `finish_reason` and `answer`, and each placed entry's
  truncation classification by `n`;
- the Ink Map's retained runs and the coverage policy resolved for the page, or
  `null` when the page's ink was not measurable.

Per entry `n` of a `read` page's answer, in answer order, each naming the page's
accounting (`page_accounting_ref`, also an input) and carrying its hold codes as
`page_holds`; either record is held when `page_holds` or its own `holds` is
non-empty, so every act on a held page -- by rule (e), rule (i) or any other -- is
held:

`kind="act-region"` (subject act_id, attempt `attempt_id(act_id, "reading-region", 1)`):

```
{schema: "perlector-act-region.v2", page_id, page_ordinal, n, kind,
 label, cites (as given), cited_ids (expanded, first-cited order), act_class,
 page_reading_attempt, region_boxes_px, union_box_px | null, region_id, image_path,
 image_sha256, transform, transform_digest, page_reading_ref, page_accounting_ref,
 feed_ref, holds, page_holds}
```

- `act_id = act_id(page_id, act_class, {page_reading: <attempt>, n, union_box_px})`
  (`common/contracts/identities.py`, classes `reading` and `reading-unplaced`).
- `region_boxes_px` is the entry's region: the sealed-page boxes of its placing ids as
  `placement_boxes` gives them, each box once, in first-cited order. The page
  accounting, the Recensor's residual-ink check and the corpus exactly-once measure
  read it. `union_box_px` is the bounding box of those boxes, unpadded, and only crops
  the act and names it in `act_id`. The crop is cut from the sealed Exemplar by the
  Designator's own crop path
  (`common.exemplar_boundary.cut_exemplar_crop`): `transform` is the closed crop
  transform, `region_id = region_id(act_id, transform)`, and the crop blob is an
  input.
- `holds`: `reading-unplaced` (no cited id places: `region_boxes_px` empty, no crop,
  every crop field null, class `reading-unplaced`), `duplicate-region` (another entry
  claims mostly the same ink, `page_accounting.duplicate_regions`; both held),
  `no-autopsia` (no page image was shown).

`kind="perlectio"` (subject act_id, attempt `perlector_attempt_id(act_id, "perlegere", 1)`):

```
{schema: "perlectio.v3", page_id, page_ordinal, act_region_ref,
 page_reading_ref, page_accounting_ref, feed_ref, n, kind, label, text,
 uncertain_spans, gaps, uncertainty_assessment, dissent, truncation | null, autopsia,
 continues_from_previous_page, continues_to_next_page, holds, page_holds, engine_call,
 provenance}
```

- `text` and the doubt layers come from `annotations.read_doubt_marks`; a mark that
  does not parse keeps the raw text and adds hold `doubt-marks-malformed`.
- `autopsia`: whether the page image was shown. A reading made from the witnesses
  alone (`page_image = "off"`) cannot be established from the ink, so every act of
  such a run holds `no-autopsia`.
- `dissent`: one row per shown witness, `{letter, witness_label, cited_units, ...}`
  with `dissent_against`'s fields against that witness's cited units joined by
  newlines in its own order -- with its own doubt markers removed
  (`common.alignment.bracket_marker_view`) when its Testimonium's
  `format_capabilities.can_express_uncertainty` is true, so a witness's own doubt is
  never counted as departure; a witness with no cited unit, or whose page outcome is
  not `read`, is a row with `compared: false` and its reason. Each comparison runs
  under the run's sealed `[dissent] max_comparison_steps`; a row it stopped is
  `compared: "unknown"` and carries that budget, and `page_path.validate_page_dissent`
  refuses a record that loses a shown witness or names a budget the run never sealed.
- `truncation` is `truncation.classify` over the region's pixels (the area of the union
  of `region_boxes_px`, each pixel once) against the page's;
  null for an unplaced entry. A `truncated` or `unknown` classification adds hold
  `reading-incomplete`; the page accounting's rule (g) records an entry with no
  classification (an unplaced one) as `truncation-not-classified`, not measured,
  which holds.
- An entry whose text is empty, or only `[[?]]` and whitespace, holds
  `entry-no-readable-text`.
- Outcome: `held` with any hold in `holds` or `page_holds`, else `read`. `holds`
  repeats the act-region's plus the reading's own.

### Resume

A page with a `page-reading` is never asked again: it is read back, refused unless it
was made under this run's configuration from this page's feed and, for a live
reading, its call record still holds to the sealed Perlector row and the reading
names that row as its `sampling`. Its `page-accounting`
and each entry's `perlectio`, when already sealed, are adopted rather than measured
again, and refused by name when they name other inputs than the page has now
(another feed, reading, region, accounting, policy, configuration or input set) or
when a sealed dissent is not a valid page dissent under this run's sealed budget; a
missing one is computed and published. The page-read denominator recomputes each
adopted dissent under that budget and requires it exactly
(`page_path.dissent_holds`). Act-regions are deterministic and re-published
byte-identical. Before a live chair starts, a page it will send with `reader-sent`
records and no `page-reading` is sent again only when no retained reply could be its
answer (`_unrecorded_replies`, `_answers_a_send`); otherwise the pass refuses by
name. A fixture pass republishes identical bytes.

`--act` is refused before anything is published: the Perlector names its own acts,
so there is no Designator act to read alone.

A later page-reading attempt (`page-read:2`, the re-ask Train 3 plans) is a new
attempt of the same page, so every act it establishes gets new act ids: `act_id`
binds the page-reading attempt, and the attempt has its own `page-accounting`, which
its act records name. Attempt 1's accounting and act records stay sealed beside them.
Whether a later attempt supersedes attempt 1 -- and how a consumer tells which
attempt's acts are current -- is not decided here; the Train 3 design must state it.

## Live reading

A page is read live through `live_reader.send_page_request` behind one `ChairClient`
(`operations/serving/client.py`) whenever the sealed serving-recipe row for the
resolved Perlector chair is a `kind = "vllm"` row. Everything below is offline-proven
against `operations/serving/fakes.py` (`test_live_perlector.py`,
`test_page_reading.py`); none of it has met a card.

**The selector is the sealed catalogue, never a flag.** `perlector_serving_mode` asks
`serving_mode_for` for the `(serving_recipe, chair, tier)` row in the catalogue named by
`--serving-recipes-config`, whose digest is already inside `config_digest` through
`serving_config_inputs`. `operations/serving/assembly.py` refuses catalogue or placement
bytes the run did not seal, so the posture cannot be moved after the run was bound.
`--placement-tier` must be supplied beside a live catalogue and is deliberately *not*
sealed — it is a measured runtime fact of the card, and the receipt records the caps
that actually bound the serving moment. An absent chair is fixture without consulting
the catalogue: an absence has no resolved identity to look a row up by.
`main(registry_factory=…, serving_factory=…)` are dependency seams only; neither makes a
run live or fixture.

**Stop reason, verbatim.** The engine's `"stop"` and `"length"` are the reading's own
two words; an absent `finish_reason` arrives as `None`. Any other engine string, or a
body that is not a reading at all (`parse_problem`), raises `EngineSignalRefusal`, and
the page's reading is published `call-failed` with the retained bytes named: `ChairClient`
retains the raw response before it parses, so nothing is lost.

**Real ingress.** The stage opens through `common.stage.open_stage_context`, which
decides the route from one read of the run authority and, on a real submission, carries
the registry, the sealed digest map and the serving configuration inputs this stage
requires before its first line of work (`decoding`, `perlector-protocol`,
`perlector-audit`, and `bound_serving_recipes`). `refuse_unlive_real_reading` refuses a real
submission whose sealed serving-recipe row for a configured Perlector chair is not live,
before anything is published: a declared answer cannot stand in for a reading of real
ink, and the catalogue is sealed at the Door, so the repair is a new run. An absent chair
reads nothing: every page is published `not-run` (`chair-absent`). The context's fixture
slot is `None` behind a refusing accessor and is not touched on the real route.

**`engine_call`, and what it names.** A live reading's payload carries
`engine_call = {call_record_ref, raw_response_ref, response_sha256, finish_reason,
served_model_id}`, and the envelope binds both blobs as direct inputs, re-derived from
disk and compared to what the record claims (`engine_call_inputs`). The call record is
held to the Perlector's sealed decoding row and to the serving receipt's seed
(`common.stage.verify_retained_call_sampling`). A fixture reading carries no call.

**The receipt is the live one.** `provenance_for(..., receipt_ref=…)` takes the receipt
the serving manager published and `ChairClient.__enter__` re-read through the tree and
matched to this chair and revision. Fixture mode passes nothing and writes the declared
`fixture_serving_details` receipt; minting one of those beside a reading a real engine
produced would put a declared value (`fixture://`, dtype `fixture`) where a measurement
belongs.

**One chair, started late, stopped before the seal.** The client is entered on the first
page that is actually sent, so a resumed pass whose pages are all read never loads a
model onto a card that bills by the hour. `ResidentChair` owns the shutdown: the pass
closes it before `seal_boundary`, so a failed shutdown is never reported over a sealed
stage, and `main`'s `finally` catches every path that raised first. A
`ServiceStopError` propagates — an unverified shutdown is fatal. Pinned by
`test_a_failed_chair_shutdown_stops_the_pass_before_the_seal_is_written`.

**A call is recorded before it leaves.** A `reader-sent` record
(`{schema: "perlector-reader-sent.v1", act_key, attempt_ordinal, pass, send,
receipt_ref, concurrency, image_sha256s}`, subject the page id, `act_key` the page's
key) is published on the main thread before a page's call leaves. `send` numbers the
sends from 1, and a later send binds the earlier ones as inputs, so a call re-sent after
an interruption is on the record, never silent. `receipt_ref` names the serving session
that sent it (also bound as an input), `concurrency` the width of the window it was sent
in, and `image_sha256s` the images the call carries, in the order sent. The outcome is
`read`: the envelope's closed vocabulary has no word for a request. A reply's raw bytes
are retained first and then the call record that names them, so a resume looks for an
unanswered send's reply in both forms (`_unrecorded_replies`, `_answers_a_send`; see
"Resume", above).

**The reading deadline.** `--reading-deadline <UTC ISO time>` makes a live pass refuse to
start when the chair's `startup_timeout_seconds` plus every page left, at
`throughput.planned_seconds_per_page` for the sealed page answer cap, would run past it,
and refuse to begin another page when the pages left would. It stops between calls,
never inside one.

**Concurrent calls.** A live pass keeps up to `--perlector-concurrency` pages unfinished
at once (default and ceiling: the served row's `max_num_seqs`), so the engine can batch
their calls; the orchestrator forwards the flag and journals it, it is not sealed, and
the stage prints the width it used. A fixture pass reads one page at a time. Each page is
prepared on the main thread, and every record is written there strictly in page order,
exactly as a serial pass writes it (`_in_order_window`); only the calls overlap. If
preparing a page, a call or a publication raises, every page already sent is still
finished in order before the error stops the pass, so no reply is left without its
record. An interrupt stops waiting at once so the chair can be shut down; it first
finishes every page whose reply has already arrived. A batched reply can differ from an
unbatched one in low-order bits, so each `reader-sent` record carries the width its call
was sent under as `concurrency`.

**A response refusal exits in this stage's own vocabulary.** `ChairResponseRefusal` is a
`ServingError`, which is a `RuntimeError` and not a `ContractError`, so `run_stage` does
not catch it; `main` translates one that escapes the page's own failure record into a
named refusal at the stage boundary. Its sibling `ChairRequestRefusal` is deliberately
not caught: that one says this stage built a request that may not go on the wire, which
is a defect in this code, and a traceback naming the construction site is worth more
there than a named exit.

**Decoding.** Every Perlector request samples at the Perlector's row of
`config/decoding.toml`'s `chair_decoding`: Qwen3.8-27B's model card values for
non-thinking mode (`temperature` 0.7, `top_p` 0.8, `top_k` 20, `min_p` 0,
`presence_penalty` 1.5, `repetition_penalty` 1.0), with the thinking switch off
(`chat_template_kwargs = {enable_thinking: false}`). `ChairClient` selects the row by its
own chair from the sealed policy `main` loaded, and sends it with the serving row's
seed. Seed, row and `sampling_effective` are on every call record. A call record from
before this decoding is refused by its schema's name, including on a resume, where it
is never counted as an unattributed reply. A seeded request is reproducible in intent,
not bit for bit: a batched step can differ in low-order bits.

**`max_tokens` is sent, from the sealed decoding policy.** The page answer cap
(`common.decoding.perlector_page_max_tokens`) bounds every page call; the value sent is
the one the page's capacity record admitted, and it rides `generation_sent` on the
retained call record. A reply that reaches it comes back as an engine `"length"`, and the
page is held `cut-off`; nothing re-asks.

## Consumer obligations

Every consumer names the operation it is collapsing attempts of and recomputes the
sealed `attempt_id` from (subject, operation, ordinal), because the envelope binds
`artifact_id` to that token without ever re-deriving the token itself. Ordinals must be
the contiguous run 1..N -- attempts are append-only and never reused, so a gap is an
attempt that is no longer here, and a manufactured far ordinal cannot leapfrog the
attempt that happened. The Recensor writes the exact Perlectio reference it reviewed
into its review, and the Archetypus and Armarium follow that reference rather than
independently looking up whatever reading now sorts latest.

## Not built here

- Real serving on real silicon. What is proven offline: reader selection by sealed row
  kind, the stop-reason mapping and its refusals, the call record and its retained
  bytes, the live receipt on the record, the resume rule, and one chair started and
  stopped per pass — all against `operations/serving/fakes.py`. What still needs a card:
  readiness against a real vLLM process, the page prompt's byte fidelity against a real
  chat template (no tokenizer files are fetched here), whether vLLM emits an omitted
  `finish_reason` key or an explicit `null`, the planned page time in `throughput.py`,
  and every timing value in `config/serving_recipes_real.toml`, which is labelled
  UNMEASURED in the file for that reason.
- Pass C. The sealed audit policy is recorded on every `page-reading` as not run; no
  span of a page reading is flagged or re-proved.
- **Spec 08's contextual-suggestion flag is not built.** "A contextual suggestion (a year
  that must be 1805) may ride as a flag while the text stays what the pixels support" —
  `perlectio.v3` has no field that could carry one, and nothing here produces one.
- **A truncated or unknown reading is held, not retried.** Spec 08 asks that such an
  attempt be "recorded, retried within the recovery budget, never accepted"; an entry
  whose classification is `truncated` or `unknown` holds `reading-incomplete`, and a page
  cut off at the answer cap is held whole. Nothing is lost and no stale text is
  established — the safe half of the requirement holds — but the bounded re-ask is not
  built here.
