# Perlector — contract

The Perlector reads every sealed Exemplar page whole, in one model call, and
establishes the acts on it. The reader is shown the page image, each witness's page
broken into that witness's own units, and Surya's detected lines and blocks, all as
clues; it never chooses among witnesses, and dissent from each witness is measured
only after the reading is fixed.

Successors consume the records below, never this stage's code. The Recensor reviews
each unit this stage established (`pipeline/5_recensor/CONTRACT.md`), and the
Archetypus, the Coniector and the Armarium follow the references it records.

Every record is a `skeleton.v1` envelope under `4_perlector/artifacts/` with a derived
identity, an attempt binding, a self-hash and checked direct inputs. Every box is the
repository's `bounds` `{x, y, w, h}` in sealed-page pixels.

## Inputs

- Every sealed Exemplar page; a page the Exemplar refused still gets a `page-reading`.
- Each page's current page Testimonium per chair of the page's roster: the sealed
  roster's page witnesses, less a routed witness its rule does not route to the page
  (Attestatores CONTRACT, "Witness routing"), so an act page's feed is the one it was
  without the routed chair. A roster chair missing beside others that testified
  refuses the pass.
  A page no witness testified to is not refused: it is fed with no witness row and
  held `no-witness-testimony`. Each witness's units are re-derived from the bytes its
  Testimonium retains, never taken from fields the record merely states.
- The Designator's Surya census and detections, and its record-detector records. A
  census and its detections must agree exactly, or the pass refuses by name. A run
  with no Surya census shows none and says so on the feed.
- The Ink Map's runs for each page.
- Sealed configuration: `perlector-protocol` (the `[feed]` switches, among them
  `page_types`, the page render size, the truncation floor), `decoding` (the Perlector's sampling row, page
  answer cap and repetition-loop guard), `page-accounting`, `alignment` (the dissent step budget), `recovery`
  (whether a page may be re-asked) and the serving catalogue. `perlector-audit` is
  sealed and recorded on every reading as not run.

The sealed serving row picks the reader. A `kind = "vllm"` row for the Perlector chair
reads live; any other row reads the synthetic fixture's declared answers, which prove
wiring only. A real submission on a row that is not live is refused before anything is
published. An absent Perlector chair reads nothing: every page is `not-run`
(`chair-absent`). `--act` is refused: the Perlector names its own acts.

## Records, per page, in publication order

Every page's feed is published before any page is read, so a live pass knows exactly
which pages it will send before its first call. Its chair loads meanwhile, on a
background thread, so the cold start overlaps the feeds (see the pass rules below).

**`page-feed`** (subject `page_id`, outcome `read`): everything one call is shown —
the sealed switches, the page render (or none), each shown witness's units with ids
`A1`, `B1`…, Surya's lines `L1…` and blocks `S1…` with how the blocks were sequenced,
the answer measure the output reserve is sized on, the rendered prompt (null when
nothing is asked) and `feed_digest`. It defines every id a reading may cite. Its
inputs are every page witness's Testimonium (hidden ones included, since the
accounting measures them), every Surya record, the render and the sealed page.

## Page types and entry kinds

With the `[feed]` switch `page_types = "named"` the reading names its page's type
and each entry's kind (`common/page_types.py`; the answer's `entries` shape below):

- page types: `register-acts`, `index`, `table`, `ledger`, `instrument`, `prose`,
  `blank`, with `writing` one of `handwritten`, `typed`, `printed`, `mixed`;
- entry kinds: `act`, `index-row`, `table-row`, `ledger-entry`, `instrument`,
  `paragraph`, `other`.

Each kind has an act class: `act` for an act or instrument, `other` for every other
kind. Every record after the answer -- act-region, Perlectio, page accounting entries,
the page-read denominator, the Recensor and the Coniector -- carries the class as its
`kind`, so an index row is never counted, paired across a page break or reconstructed
as an act; the kind the reading named travels beside it as `entry_kind`. A table
without the switch is `off`: the reading is asked for the older `acts` shape, its kinds
are already act classes, and nothing below changes for it. So the saved answers of a run
sealed before page types replay as they were read (`operations/replay/`).

What the page type changes, all recorded on the page accounting's `page_type`:

- **Rule (i), the record detector**, applies only on a page whose type is not stated
  and on `handwritten` or `mixed` `register-acts`. On every other stated page it is
  still measured and recorded, but its findings are listed in
  `page_type.recorded_not_held` instead of `holds` or `flags`.
- **Rule (h), duplicate regions**: two entries of a row kind (`index-row`,
  `table-row`, `ledger-entry`) placed by one witness unit they both cite -- a whole
  table a witness gave as one unit -- are compared without that unit's box, and the
  unit and its rows are recorded `rows-share-unit`. Two rows naming the same line are
  still a duplicate.
- **The truncation length signal** judges only `act` and `instrument` entries, the
  kinds it was measured on; on any other kind of an `entries` answer it is not judged
  and the measure records `length_exempt_kind`. The engine's stop word and the other
  two signals still decide.
- **A model-free cross-check** sets the stated type beside what the detectors found:
  `page_type.facts` (`surya_table_blocks`, `witness_table_units` per chair,
  `detector_records`) and `page_type.agreement`, one `{check, agrees, detail}` per check
  (`table-evidence`; `detector-records` on handwritten register acts, index and table
  pages), plus `page_type.kinds`, the entry kinds unexpected on the stated type. These
  are facts only: none holds here, and whether a disagreement flags or holds a page is
  the review configuration's decision.

Whether a check that applies holds a page or only flags it is the sealed `[flags]`
table of `config/page_accounting.toml`; the page type decides only which checks apply.

**`reader-sent`** (subject `page_id`, live only), published before the call leaves:

```
{schema: "perlector-reader-sent.v1", act_key: "page-<ordinal>", attempt_ordinal,
 pass: "page-reading" | "page-reask" | "page-reread", send, receipt_ref,
 concurrency, image_sha256s}
```

`send` numbers the sends of one reading from 1, and each later send inputs the
earlier ones, so a call sent again after an interruption is on the record.
`concurrency` is the width of the window the call was sent in; a batched reply may
differ from an unbatched one in low-order bits.

**`page-reading`** (subject `page_id`, attempt `attempt_id(page_id, "page-read", n)`;
`n` = 1 for the first reading, 2 for its re-ask, 3 on for operator re-reads):

```
{schema: "perlector-page-reading.v2", page_id, page_ordinal, attempt_ordinal,
 feed_ref, request_digest, engine_call | null, sampling | null, capacity | null,
 finish_reason, stop_reason, parse_state, answer | null, problems: [{code, detail}],
 failure | null, disposition: "read" | "held", reask: null | {...},
 operator_reread?: {...}, answer_repairs?: [{code, keys, detail}], audit, provenance}
```

- `parse_state`: `parsed` (`answer` is the object exactly as given); `malformed` (not
  the answer grammar, `common/page_answer.py`: either the `entries` shape `{page_type,
  writing, entries, set_aside}`, a re-ask's `{entries, set_aside}`, or the older `acts`
  shape `{acts, set_aside}` whose kinds are `act` and `other` only); `cut-off` (the engine stopped at the output cap; never
  parsed); `repetition-loop` (the reply repeated the same line or block of lines
  over and over and the call was stopped; never parsed, `stop_reason`
  `repetition-loop`, `finish_reason` the engine's word if one had arrived, as a
  rule none); `refused-capacity` (the request does not fit the sealed serving row;
  nothing sent, nothing trimmed); `call-failed` (an engine or transport failure,
  `failure` naming what was observed and its retained bytes as inputs); `not-run`
  (nothing asked: `page-not-sealed`, `chair-absent`, `no-witness-testimony` or
  `nothing-to-show`, every reason that applies; on a replay's re-ask, `not-replayed`,
  see "A replay").
- `disposition` is `read` only for `parsed` with no problem. Any problem holds the
  whole answer with it: an id the feed does not define, a range over Surya ids that
  could name ink the entry did not read (a range of lines is read only when every line
  lies inside the witness units the same entry cites, and a range of blocks, which place
  nothing, is read; `common/page_accounting.py`, `_covered_detection_ranges`), an id
  both cited and set aside, a set-aside without a reason, a missing finish reason. No
  answer is trimmed or split.
- A continuation flag set on an entry that is not at its page's edge (only the first
  entry of the act class, an `act` or `instrument`, may continue from the page before,
  only the last onto the page after, and any other entry only as the answer's first or
  last entry) is not a problem of
  the answer: the page keeps every entry, and the flag stays on its entry's Perlectio
  exactly as given. It is on no page break, so it joins nothing, and the Recensor
  holds that `act` entry `continuation-off-page-edge` or notes it on an `other` entry
  (`pipeline/5_recensor/CONTRACT.md`). A re-ask may still set no flag at all.
- One repair is made, and recorded: a reply that is not JSON only because some of the
  grammar's own keys are written bare (`{acts: [`) is parsed after quoting those keys
  outside strings and nothing else (`common.page_answer.parse_page_answer_repaired`).
  The reading then carries `answer_repairs`, one `{code: "unquoted-keys-quoted", keys,
  detail}`; a reading that needed no repair carries no such field. The repaired text
  must be one JSON value with no other decode problem, or the reply stays `malformed`
  (`not-json`) as it came, and the repaired answer meets the grammar and the feed like
  any other. The raw response blob is never changed.
- The envelope outcome is the disposition, except `call-failed`, whose outcome is
  `failed` so that the run-level hard-failure cap counts it.
- `engine_call` (live only) is `{call_record_ref, raw_response_ref, response_sha256,
  finish_reason, served_model_id}`; both blobs are direct inputs, re-derived from disk
  wherever the reading is bound, and the call record is held to the sealed sampling row
  and the serving receipt's seed. The reply is streamed, so the call record is a
  `chair-stream-call-record` (`common/contracts/serving.py`): the response blob is
  the server-sent event bytes exactly as received, up to any stop, and `stream`
  names the sealed guard the reply was watched under and the loop that stopped it,
  or none. Wherever the reading is bound, the reply is scanned again from those
  bytes and must show exactly that loop. `sampling` records that row as sent and as the
  pinned engine applies it.
- `capacity` is the admitted `{capacity, answer_reserve, max_tokens}`; the call sends
  that `max_tokens` with thinking off. It is the sealed page cap or the context the
  prompt leaves, whichever is smaller; the answer reserve decides only whether the
  page fits, never how long its reply may run, since a dense index page answers far
  past any estimate of it. A reply that reaches the cap stops on `length` and is
  held as cut off. A re-ask is bounded the same way.
- A reply that falls into a degenerate loop is stopped long before the cap: the
  call is streamed and abandoned as soon as the same whitespace-trimmed line has
  come `loop_line_repeats` times in a row, or the same block of 2 to
  `loop_block_max_lines` lines `loop_block_repeats` times in a row (sealed in
  `[perlector_generation]` of `config/decoding.toml`; `common/repetition_loop.py`).
  Only exact repeats count, so many similar rows in a row (one surname over and
  over) never stop a reply. The page is held `repetition-loop`, like a cut-off.
- `provenance` names the chair, its resolved identity and revision, the serving
  receipt of what answered (the live receipt, never a declared one beside a real
  reading), the witness regime and the adapter revision.

**`page-accounting`** (subject `page_id`, the reading's own attempt), published for
every page with a feed, after its reading and before any act record:
`page-accounting.v3` from `common/page_accounting.py` under the sealed policy. It
measures the reading against every witness unit, every Surya line, every detector
record and the page's ink, rule by rule, and lists its `holds`; outcome `held` when
there are any. A finding whose code the policy's `[flags] codes` names is a review
flag: listed in `flags` instead, recorded and reported but holding nothing, and a rule
whose every finding is one has status `flag`. A rule the page type switches off is
still measured, and its findings are listed in `page_type.recorded_not_held`, neither
held nor flagged. Rule (e) names a cited witness unit of at
most `short_unit_characters` normalized characters read differently
`witness-short-unit-not-read` (a signature or initials: a dissent about a few letters),
and any other unit `witness-text-not-read`. Its `entries` carry `kind` (the act class)
and `entry_kind`, and its `page_type` block `{grammar, stated, writing, facts,
agreement, applicability, kinds, recorded_not_held}` says what the reading stated and
which rules applied ("Page types and entry kinds" above). Its inputs are the feed, the
reading, every page witness's Testimonium and every detection and ink record it
measured.

Then, per entry of a `read` answer, in answer order, two records. Each names the
page's last accounting as `page_accounting_ref` and carries its holds as
`page_holds`; either record is held when `page_holds` or its own `holds` is non-empty,
so every act on a held page is held.

**`act-region`** (subject `act_id`, attempt `attempt_id(act_id, "reading-region", 1)`):

```
{schema: "perlector-act-region.v2", page_id, page_ordinal, n, kind, label, cites,
 cited_ids, act_class, page_reading_attempt, reading_attempt?, reading_n?,
 region_boxes_px, union_box_px | null, region_id, image_path, image_sha256,
 transform, transform_digest, page_reading_ref, page_accounting_ref, feed_ref,
 holds, page_holds}
```

The region is exactly the ink the entry names: the boxes of its placing ids, each
once, in first-cited order. A Surya line places; so does a witness unit shown in its
own units whose text vouches for its box. A Surya block, a textless unit or a unit of
a witness shown flat places nothing. The union box only crops the act from the sealed
Exemplar and names it in `act_id = act_id(page_id, act_class, {page_reading, n,
union_box_px})`. An entry that places nothing is published with class
`reading-unplaced`, no crop and hold `reading-unplaced`. Two entries claiming mostly
the same ink are both held `duplicate-region`. A run shown no page image holds every
act `no-autopsia`.

**`perlectio`** (subject `act_id`, attempt `perlector_attempt_id(act_id, "perlegere", 1)`):

```
{schema: "perlectio.v3", page_id, page_ordinal, act_region_ref, page_reading_ref,
 page_accounting_ref, feed_ref, n, kind, label, text, uncertain_spans, gaps,
 uncertainty_assessment, dissent, truncation | null, autopsia,
 continues_from_previous_page, continues_to_next_page, holds, page_holds,
 engine_call, provenance, reading_attempt?, reading_n?, entry_kind?}
```

- `kind` is the entry's act class; `entry_kind` is present exactly when the answer
  named the kind (the `entries` shape), and its class is `kind`.

- `text` is the entry's text with the reader's doubt marks split out: `[[?]]` is a
  zero-width gap, `[[reading|other]]` an uncertain span with its alternatives. The
  doubt layer meets `common.contracts.uncertainty.validate`. Marks that do not parse
  keep the text as returned and hold `doubt-marks-malformed`. An entry with no
  readable text holds `entry-no-readable-text`.
- The doubt share (`common.reading_annotations.doubt_count`) is the share of an
  entry's non-whitespace characters inside an uncertain span, each gap counted as one
  unread character (a provisional weight). Marks that do not parse count every
  character unread, and an entry with nothing read counts as one unread character.
  An entry over the sealed `[doubt] max_act_doubt_share_bp` of
  `config/page_accounting.toml` holds `doubt-share-high`. When the share over all
  the entries the page publishes together, its first reading's and a counted
  re-ask's, with their counts summed, is over `max_page_doubt_share_bp`, each entry
  holds `page-doubt-share-high`, a page-wide hold, decided before any act record is
  published. Both limits are provisional and
  unmeasured, so a reading cannot pass by marking everything doubtful.
- `truncation` classifies the entry `complete`, `truncated` or `unknown` from the
  engine's stop word and three computed signals, with every term of the length signal
  recorded so a reader can judge it again. `truncated` and `unknown` hold
  `reading-incomplete`; an unplaced entry has no classification, which the
  accounting holds as not measured.
- `dissent` has one row per shown witness, in feed order: the cited units of that
  witness compared with the fixed text (a doubt-marking witness's own markers removed
  first), as departure spans with no score; `compared: false` with a reason when the
  witness has no reading or no cited unit; `compared: "unknown"` with a reason when
  the comparison did not run: past the character-pair bound with the reason alone,
  or past the sealed step budget with that budget as well. Dissent records; it never
  holds.

## The re-ask

A page is asked at most once more, and only when the sealed recovery policy allows it,
its first reading is a parsed, read answer that finished on `stop`, and its accounting
found witness units, Surya lines or detector records that no entry accounts for and
that the feed placed by a box. A unit lying mostly inside an entry's region was read
and not cited; the page holds it rather than asking again. A failed, cut-off or
malformed reading is held, never re-asked. A truncated entry is held and never
re-read; its page may still be re-asked about other ids.

The re-ask sends the same images and feed, the first reading's entries by number,
kind, label and cites (never their text), and the named ids with their boxes, under
the same sampling row and seed. Its `page-reading` (attempt 2) records:

```
reask: {trigger_reading_ref, trigger_accounting_ref, named: [{id, code, box_1000}],
        prior_entries: [{n, kind, label, cites}], budget, prompt: {serving_recipe,
        builder_sha256, rendered_sha256, instruction_sha256}}
```

Its answer may cite or set aside only the named ids and may set no continuation flag;
otherwise it is held whole. The page's last accounting then measures both readings
together: the first reading's entries exactly as they were, then the re-ask's numbered
on after them. Rule (j) holds a named id the re-ask set aside, an entry of it that
places nothing or gives no text, an entry duplicating a first-reading entry, and a
re-ask that is not a parsed, valid answer. The act records of the first reading are
always published; the re-ask's only when the accounting counts it, each carrying
`reading_attempt: 2` and `reading_n`. Nothing of the first reading is changed,
dropped or out-counted.

## An operator re-read

A person's page `re-ask` decision, current against the Recensor's latest review,
makes the next pass read that page again: the first reading's request over the same
feed, attempt 3, then 4, without a gap, outside the re-ask allowance and never
re-asked by the machine. Its `page-reading` records

```
operator_reread: {decisions: [{decision_hash, approval_ref}], supersedes: [reading refs]}
```

and inputs both. It has its own accounting and act records, minted from its own
attempt, and becomes the page's current reading: the page-read denominator counts its
entries alone. The earlier readings and their records stay as read, marked
superseded.

A re-read is planned against the entries the page counted before it: its first
reading with any the re-ask added, or the last re-read that read something and kept
every act it replaced. A re-read that dropped an act never becomes that baseline, so a
later re-read cannot launder the drop. Each `act` entry of the baseline is followed by
its own ids, the ones no other act of it cites: they must all be cited by exactly one
`act` entry of the re-read, which cites no other replaced act's own ids, and the
re-read must name at least as many acts citing an id. While any replaced act has no id of its own,
the re-read's acts may cite no id the baseline's acts did not. Otherwise every entry of
the re-read holds `superseded-act-not-read`, a page-wide hold a page decision clears.
The rule follows ids, never text, with or without a record detector: an act may not
leave the count by being left out, set aside, merged, split, or read as `other`. It is
strict on purpose: a re-read that moves act boundaries, splits an act or cites
different ids for one is held for a page decision too. A re-read with no entry at all
carries no entry to hold; its page accounting measures the page like any reading's.
The re-read is also accounted against the page's evidence like any reading.

## Resume

- A page with a `page-reading` is never asked again. It is adopted only when it was
  made from this page's feed, under this run's configuration, for the same re-ask or
  operator decisions, and for a live reading only when its call record still holds to
  the sealed sampling row; otherwise the pass refuses by name.
- A sealed accounting or Perlectio is adopted only when it was measured from exactly
  the inputs the page has now; a sealed dissent must be valid under the sealed
  budget. Anything missing is computed and published. Act-regions are deterministic.
- A reading with `reader-sent` records and no `page-reading` is sent again only when
  no retained reply could be its answer; otherwise the pass refuses, so no page is
  read twice and no reply goes unrecorded.
- A page holding records its plan does not account for — an attempt past its re-ask
  that is no operator re-read, a re-ask record on a page with no re-ask planned, act
  records published ahead of their accounting — is refused (`FatalAccounting`).
- A fixture pass republishes identical bytes.

## Live reading

- One chair per pass. A live pass with a page that has no sealed first reading, none
  an interrupted pass already sent, and a reading deadline (if any) that admits the
  start-up timeout and every such page, starts it on a background thread before the
  feeds are built and waits for it before the first call; a failed start stops the
  pass on the main thread. A pass that stops while the chair is still loading does not
  wait: the start's thread stops the chair as soon as its load returns, and the
  process exits only after that. Otherwise (a resumed pass with earlier sends, or only
  re-asks and re-reads left) it starts on the first page actually sent, so a resumed
  pass with nothing left to send loads no model. A pass whose unread pages all turn
  out not to be sent (refused by the Exemplar, over capacity) has started a chair it
  does not use. The chair is stopped before the stage seal, so a failed shutdown is
  never reported over a sealed stage, with one exception: when the orchestrator runs
  the Coniector next (`--hand-off-to-coniector`) and the reconstructor's sealed row
  shares this chair's service (`shares_service_with = "perlector"`), a chair that is
  up is left serving after the seal, for the Coniector to take over and stop
  (`operations/serving/README.md`, "A shared service"). A failed shutdown of it is
  then reported by the Coniector, not here. A pass that fails before its seal stops
  its chair as always.
- `--perlector-concurrency` keeps up to that many calls in flight (ceiling and default:
  the launched row's `max_num_seqs`, the row's own or the run's `--capacity-plan` width); records are still written strictly in the order
  the calls were drawn. A page's re-ask is drawn as soon as its first reading is
  published, ahead of the next page's first reading, so the card is not idle
  between first readings and re-asks. It joins only when the reading deadline holds
  it with every call the window has still to finish (each unfinished first reading
  and each re-ask already joined); otherwise it waits for the re-ask phase, which
  checks the deadline as before. A re-ask an interrupted pass already sent also
  waits until every first reading is finished, so that no reply of this pass is out
  when its earlier send is judged (see Resume).
  An error finishes every page already sent before it stops the pass; an interrupt
  first records every reply that has arrived.
- `--reading-deadline` refuses to start, or to send another page, when the chair's
  start-up time plus the pages left would run past it. After the feeds it is checked
  against what is left of a background start's timeout and every page to send.
- The engine's `stop` and `length` are the reading's own words; anything else is a
  `call-failed` reading with its retained bytes named.

## A replay

A replay run (`common/replay.py`, made by `operations/replay/replay.py`) reads a saved
run's pages again with the current code and calls no model. Its `run.json` carries
`replay` (`run-replay.v1`: the source run's id, its authority's self-hash and commit,
the stages it imports, `replies: "recorded"`), and its `repository_commit` is the
code that replayed. It holds the source's Door, Exemplar, Ink map, Designator and
Attestatores records byte for byte, under the source's run id, and never runs those
stages. This stage then runs as in a live pass, through a real `ChairClient`, but the
chair's handle answers each request with the reply the source run retained for the
same request bytes (`operations/serving/replay.py`), so each call record, reply and
receipt is the source's own. The client keeps no bytes the source did not keep.

- A first reading or an operator re-read whose request the source did not send in
  exactly these bytes and get answered refuses the pass: its page could not be read.
- A re-ask the source did not send in exactly these bytes is not asked: its
  `page-reading` (attempt 2) is `not-run` with one problem, `not-replayed`, and no
  request digest, call or receipt; the accounting holds it `reask-unread` and the
  page stands on its first reading. The Recensor accepts that state only in a
  replay run.
- A source re-ask the current plan does not make is not read.

## Consumer obligations

- Count acts by `kind == "act"` only. Export an entry of a row kind
  (`entry_kind` `index-row`, `table-row` or `ledger-entry`) as a row, never as an act:
  `common.page_types.row_record` gives its `rows.jsonl` line (`armarium-row.v1`:
  `act_key, act_id, page_id, page_ordinal, page_type, n, entry_kind, label, text,
  uncertain_spans, gaps, holds`, and `review` when the caller passes one), the page
  type read from the entry's page accounting (`page_type.stated`). A Perlectio with
  no `entry_kind` is of an `acts` answer and has no row.

- Recompute every attempt id from (subject, operation, ordinal), and require
  ordinals 1..N without a gap: a gap is an attempt that is no longer there.
- A page's current reading is its last operator re-read, else its first reading with
  its re-ask. Read superseded readings as history, never as current units.
- Treat `page_holds` and `holds` as binding: a held act is a review item, never
  delivered text.
- Follow the exact Perlectio reference a review names, never whichever reading now
  sorts latest.
- Read `dissent` as a record of departure, never as a quality score or a vote.

## Stage-completion seal

Before its final manifest the stage publishes one `decode-environment` and one
`stage-seal`, or reuses both on a byte-identical retry. The seal binds the pass's
inventory and blob contents, the decode-environment bytes, the run's `config_digest`
and `register_digest`, and the `(kind, outcome)` census. A pass that never reaches
its seal leaves none, and the successor refuses the missing boundary.

## Not built

- Live serving has been proven only against the serving fakes, not on a card.
- Pass C, the audit that would flag and re-prove spans of a reading, is recorded as
  not run on every reading.
- A truncated or unknown entry is held, never re-read automatically; a person can ask
  for an operator re-read.
