# Perlector: contract

The Perlector reads every sealed Exemplar page whole, in one model call, and establishes
the acts on it. The reader is shown the page image, each witness's page broken into that
witness's own units, and Surya's lines and blocks, all as clues; it never chooses among
witnesses, and dissent from each witness is measured only after the reading is fixed.

Successors consume the records below, never this stage's code: the Recensor reviews each
unit (`pipeline/5_recensor/CONTRACT.md`), and the Archetypus, Coniector and Armarium follow
the references it records. Every record is a `skeleton.v1` envelope under
`4_perlector/artifacts/` with a derived identity, an attempt binding, a self-hash and
checked direct inputs. Every box is `bounds` `{x, y, w, h}` in sealed-page pixels.

## Inputs

- Every sealed Exemplar page; a page the Exemplar refused still gets a `page-reading`.
- Each page's current page Testimonium per chair of the page's roster: the sealed roster's
  page witnesses, less a routed witness its rule does not route to the page
  (`pipeline/3_attestatores/CONTRACT.md`, "Witness routing"). A roster chair missing beside
  others that testified refuses the pass. A page no witness testified to is fed with no
  witness row and held `no-witness-testimony`. Each witness's units are re-derived from the
  bytes its Testimonium retains.
- The Designator's Surya census and detections (which must agree exactly) and its
  record-detector records. A run with no Surya census says so on the feed.
- The Ink Map's runs for each page.
- Sealed configuration: `perlector-protocol` (`[feed]` switches including `page_types`, the
  render size, the truncation floor), `decoding` (sampling row, page answer cap,
  repetition-loop guard), `page-accounting`, `alignment` (dissent step budget), `recovery`
  (whether a page may be re-asked) and the serving catalogue. `perlector-audit` is sealed
  and recorded on every reading as not run.

The sealed serving row picks the reader: a `kind = "vllm"` row reads live; any other row
reads the synthetic fixture's declared answers, which prove wiring only. A real submission
on a row that is not live is refused before anything is published. An absent Perlector chair
reads nothing: every page is `not-run` (`chair-absent`). There is no act selection on the
command line: the Perlector names its own acts.

## Page reading

Every page's feed is published before any page is read, so a live pass knows exactly which
pages it will send; its chair loads meanwhile on a background thread.

**`page-feed`** (subject `page_id`, outcome `read`): everything one call is shown: the sealed
switches, the page render (or none), each shown witness's units with ids `A1`, `B1`…, Surya's
lines `L1…` and blocks `S1…` with their sequencing, the answer measure the output reserve is
sized on, the rendered prompt (null when nothing is asked) and `feed_digest`. It defines
every id a reading may cite. Its inputs are every page witness's Testimonium (hidden ones
too, since the accounting measures them), every Surya record, the render and the sealed page.

**`reader-sent`** (subject `page_id`, live only), published before the call leaves:

```
{schema: "perlector-reader-sent.v1", act_key: "page-<ordinal>", attempt_ordinal,
 pass: "page-reading" | "page-reask" | "page-reread", send, receipt_ref,
 concurrency, image_sha256s}
```

`send` numbers the sends of one reading from 1, each later send inputting the earlier ones,
so a call sent again after an interruption is on the record. `concurrency` is the batch
window width (a batched reply may differ from an unbatched one in low-order bits).

**`page-reading`** (subject `page_id`, attempt `attempt_id(page_id, "page-read", n)`: 1 for
the first reading, 2 for its re-ask, 3 on for operator re-reads):

```
{schema: "perlector-page-reading.v2", page_id, page_ordinal, attempt_ordinal,
 feed_ref, request_digest, engine_call | null, sampling | null, capacity | null,
 finish_reason, stop_reason, parse_state, answer | null, problems: [{code, detail}],
 failure | null, disposition: "read" | "held", reask: null | {...},
 operator_reread?: {...}, answer_repairs?: [{code, keys, detail}], audit, provenance}
```

`parse_state` is one of:

| State | Meaning |
|---|---|
| `parsed` | `answer` is the object exactly as given |
| `malformed` | not the answer grammar (`common/page_answer.py`: `{page_type, writing, entries, set_aside}`, or a re-ask's `{entries, set_aside}`) |
| `cut-off` | the engine stopped at the output cap; never parsed |
| `repetition-loop` | the reply repeated the same line or block and the call was stopped; never parsed (`stop_reason` `repetition-loop`) |
| `refused-capacity` | the request does not fit the sealed serving row; nothing sent, nothing trimmed |
| `call-failed` | an engine or transport failure; `failure` names what was observed, with its retained bytes as inputs |
| `not-run` | nothing asked: `page-not-sealed`, `chair-absent`, `no-witness-testimony`, `nothing-to-show`, or on a replay's re-ask `not-replayed` |

- `disposition` is `read` only for `parsed` with no problem. Any problem holds the whole
  answer: an id the feed does not define, a range over Surya ids that could name ink the
  entry did not read (a range of lines is read only when every line lies inside the witness
  units the same entry cites; a range of blocks places nothing and is read), an id both
  cited and set aside, a set-aside without a reason, a missing finish reason. No answer is
  trimmed or split.
- A continuation flag on an entry that is not at its page's edge (only the first entry of
  the act class may continue from the page before, only the last onto the next; any other
  entry only as the answer's first or last) is not a problem: the flag stays on its
  Perlectio as given, joins nothing, and the Recensor holds that `act` entry
  `continuation-off-page-edge` or notes it on an `other` entry. A re-ask may set no flag.
- **One repair is made, and recorded.** A reply that is not JSON only because some of the
  grammar's own keys are bare (`{entries: [`) is parsed after quoting those keys outside
  strings (`common.page_answer.parse_page_answer_repaired`), and the reading carries
  `answer_repairs: [{code: "unquoted-keys-quoted", keys, detail}]`. The repaired text must be
  one JSON value with no other decode problem, or the reply stays `malformed` (`not-json`).
  The raw response blob is never changed.
- The envelope outcome is the disposition, except `call-failed`, whose outcome is `failed`
  so the run-level hard-failure cap counts it.
- `engine_call` (live only) is `{call_record_ref, raw_response_ref, response_sha256,
  finish_reason, served_model_id}`; both blobs are direct inputs, re-derived wherever the
  reading is bound, and the call record is held to the sealed sampling row and the serving
  receipt's seed. The reply is streamed, so the call record is a `chair-stream-call-record`:
  the response blob is the server-sent event bytes exactly as received, and `stream` names
  the guard and any loop that stopped it; the reply is re-scanned from those bytes wherever
  bound and must show exactly that loop. `sampling` records the row as sent and as applied.
- `capacity` is the admitted `{capacity, answer_reserve, max_tokens}`; `max_tokens` is the
  sealed page cap or the context the prompt leaves, whichever is smaller, sent with thinking
  off. The answer reserve decides only whether the page fits. A reply reaching the cap stops
  on `length` and is held as cut off.
- **Loop guard.** The call is abandoned as soon as the same whitespace-trimmed line has come
  `loop_line_repeats` times in a row, or the same block of 2 to `loop_block_max_lines` lines
  `loop_block_repeats` times (`[perlector_generation]` in `config/decoding.toml`;
  `common/repetition_loop.py`). Only exact repeats count, so many similar rows never stop a
  reply. The page is held `repetition-loop`.
- `provenance` names the chair, its resolved identity and revision, the live serving receipt
  of what answered, the witness regime and the adapter revision.

**`page-accounting`** (subject `page_id`, the reading's attempt), published for every page
with a feed, after its reading and before any act record: `page-accounting.v3` from
`common/page_accounting.py` under the sealed policy. It measures the reading against every
witness unit, Surya line, detector record and the page's ink, rule by rule, and lists its
`holds` (outcome `held` when any). A finding whose code is in the policy's `[flags] codes` is
a review flag, listed in `flags` and holding nothing. A rule the page type switches off is
still measured, its findings listed in `page_type.recorded_not_held`. Rule (e) names a cited
witness unit of at most `short_unit_characters` read differently
`witness-short-unit-not-read` (a signature or initials), and any other
`witness-text-not-read`. Its `entries` carry `kind` (the act class) and `entry_kind`, and its
`page_type` block is `{stated, writing, facts, agreement, applicability, kinds,
recorded_not_held}`.

Then, per entry of a `read` answer, in answer order, an `act-region` and a `perlectio`. Each
names the page's last accounting as `page_accounting_ref` and carries its holds as
`page_holds`; either record is held when `page_holds` or its own `holds` is non-empty, so
every act on a held page is held.

**`act-region`** (subject `act_id`, attempt `attempt_id(act_id, "reading-region", 1)`):

```
{schema: "perlector-act-region.v2", page_id, page_ordinal, n, kind, label, cites,
 cited_ids, act_class, page_reading_attempt, reading_attempt?, reading_n?,
 region_boxes_px, union_box_px | null, region_id, image_path, image_sha256,
 transform, transform_digest, page_reading_ref, page_accounting_ref, feed_ref,
 holds, page_holds}
```

The region is exactly the ink the entry names: the boxes of its placing ids, each once, in
first-cited order. A Surya line places; so does a witness unit shown in its own units whose
text vouches for its box. A Surya block, a textless unit or a unit of a witness shown flat
places nothing. The union box crops the act from the sealed Exemplar and names it:
`act_id = act_id(page_id, act_class, {page_reading, n, union_box_px})`. An entry that places
nothing has class `reading-unplaced`, no crop and hold `reading-unplaced`. Two entries
claiming mostly the same ink are both held `duplicate-region`. A run shown no page image holds
every act `no-autopsia`.

**`perlectio`** (subject `act_id`, attempt `perlector_attempt_id(act_id, "perlegere", 1)`):

```
{schema: "perlectio.v3", page_id, page_ordinal, act_region_ref, page_reading_ref,
 page_accounting_ref, feed_ref, n, kind, entry_kind, label, text, uncertain_spans,
 gaps, uncertainty_assessment, dissent, truncation | null, autopsia,
 continues_from_previous_page, continues_to_next_page, holds, page_holds,
 engine_call, provenance, reading_attempt?, reading_n?}
```

- `kind` is the act class; `entry_kind` the kind the answer named.
- `text` is the entry's text with the doubt marks split out: `[[?]]` is a zero-width gap,
  `[[reading|other]]` an uncertain span with alternatives. The layer meets
  `common.contracts.uncertainty.validate`. Unparseable marks keep the text as returned and
  hold `doubt-marks-malformed`; an entry with no readable text holds
  `entry-no-readable-text`.
- **Doubt share** (`common.reading_annotations.doubt_count`): non-whitespace characters inside
  an uncertain span, each gap one unread character; unparseable marks count every character
  unread, and an entry with nothing read counts one. An entry over `[doubt]
  max_act_doubt_share_bp` (`config/page_accounting.toml`) holds `doubt-share-high`. When the
  share over all entries the page publishes (first reading and counted re-ask, counts summed)
  exceeds `max_page_doubt_share_bp`, every entry holds `page-doubt-share-high`. Both limits
  are unmeasured, so a reading cannot pass by marking everything doubtful.
- `truncation` classifies the entry `complete`, `truncated` or `unknown` from the engine's
  stop word and three computed signals, recording every term of the length signal.
  `truncated` and `unknown` hold `reading-incomplete`. An unplaced entry has no
  classification, which the accounting holds as not measured.
- `dissent` has one row per shown witness, in feed order: the witness's cited units compared
  with the fixed text (its own doubt markers removed first), as departure spans with no
  score; `compared: false` with a reason when there is nothing to compare; `compared:
  "unknown"` when the comparison passed the character-pair bound or the sealed step budget.
  Dissent records; it never holds.

## Page types and entry kinds

The reading names its page's type and each entry's kind (`common/page_types.py`; `[feed]
page_types = "named"` is required):

- page types: `register-acts`, `index`, `table`, `ledger`, `instrument`, `prose`, `blank`,
  with `writing` one of `handwritten`, `typed`, `printed`, `mixed`;
- entry kinds: `act`, `index-row`, `table-row`, `ledger-entry`, `instrument`, `paragraph`,
  `other`.

Each kind has an act class: `act` for an act or instrument, `other` for every other kind.
Every record after the answer carries the class as `kind`, so an index row is never counted,
paired across a page break or reconstructed as an act; the named kind travels as
`entry_kind`. What the page type changes (all recorded in the accounting's `page_type`):

- **Rule (i), the record detector**, applies only on a page with no stated type and on
  `handwritten` or `mixed` `register-acts`; elsewhere its findings are recorded, not held.
- **Rule (h), duplicate regions**: two row-kind entries (`index-row`, `table-row`,
  `ledger-entry`) placed by one witness unit they both cite (a table given as one unit) are
  compared without that unit's box, recorded `rows-share-unit`. Two rows naming the same line
  are still a duplicate.
- **The truncation length signal** judges only `act` and `instrument` entries; others record
  `length_exempt_kind`. The stop word and the other two signals still decide.
- **A model-free cross-check** records `page_type.facts` (`surya_table_blocks`,
  `witness_table_units` per chair, `detector_records`), `page_type.agreement` (one `{check,
  agrees, detail}` per check) and `page_type.kinds` (kinds unexpected on the stated type).
  These are facts only; whether a check holds or flags is the sealed `[flags]` table.

## The re-ask

A page is asked at most once more, only when the sealed recovery policy allows it, its first
reading is a parsed, read answer that finished on `stop`, and its accounting found witness
units, Surya lines or detector records with a box that no entry accounts for. A unit lying
mostly inside an entry's region was read and not cited; the page holds it rather than asking
again. A failed, cut-off or malformed reading is held, never re-asked. A truncated entry is
held and never re-read.

The re-ask sends the same images and feed, the first reading's entries by number, kind, label
and cites (never their text), and the named ids with their boxes, under the same sampling row
and seed. Its `page-reading` (attempt 2) records:

```
reask: {trigger_reading_ref, trigger_accounting_ref, named: [{id, code, box_1000}],
        prior_entries: [{n, kind, label, cites}], budget, prompt: {serving_recipe,
        builder_sha256, rendered_sha256, instruction_sha256}}
```

Its answer may cite or set aside only the named ids and may set no continuation flag, or it
is held whole. The page's last accounting measures both readings together: the first
reading's entries exactly as they were, then the re-ask's numbered on after them. Rule (j)
holds a named id the re-ask set aside, an entry that places nothing or gives no text, an entry
duplicating a first-reading entry, and a re-ask that is not a parsed, valid answer. The first
reading's act records are always published; the re-ask's only when the accounting counts
them, each with `reading_attempt: 2` and `reading_n`. Nothing of the first reading is changed,
dropped or out-counted.

## An operator re-read

A person's page `re-ask` decision, current against the Recensor's latest review, makes the
next pass read that page again: the first reading's request over the same feed, attempt 3,
then 4, without a gap, outside the re-ask allowance and never re-asked by the machine. Its
`page-reading` records `operator_reread: {decisions: [{decision_hash, approval_ref}],
supersedes: [reading refs]}` and inputs both. It has its own accounting and act records and
becomes the page's current reading; earlier readings stay as read, marked superseded.

**No act may leave the count.** A re-read is planned against the entries the page counted
before it (its first reading with any re-ask entries, or the last re-read that kept every act
it replaced). Each `act` entry of that baseline is followed by its own ids (those no other act
cites): each must be cited by exactly one `act` entry of the re-read, which cites no other
replaced act's own ids, and the re-read must name at least as many acts citing an id. While
any replaced act has no id of its own, the re-read's acts may cite no id the baseline's acts
did not. Otherwise every entry of the re-read holds `superseded-act-not-read`, a page-wide
hold a page decision clears. The rule follows ids, never text, so an act cannot leave by being
omitted, set aside, merged, split or read as `other`; a re-read that moves act boundaries is
held for a decision too.

## Resume

- A page with a `page-reading` is never asked again. It is adopted only when it was made from
  this page's feed, under this run's configuration, for the same re-ask or operator
  decisions, and (live) when its call record still holds to the sealed sampling row;
  otherwise the pass refuses by name.
- A sealed accounting or Perlectio is adopted only when measured from exactly the page's
  current inputs; a sealed dissent must be valid under the sealed budget. Anything missing is
  computed and published.
- A reading with `reader-sent` records and no `page-reading` is sent again only when no
  retained reply could be its answer; otherwise the pass refuses, so no page is read twice and
  no reply goes unrecorded.
- Records the plan does not account for (an attempt past the re-ask that is no operator
  re-read, a re-ask record with no re-ask planned, act records ahead of their accounting) are
  `FatalAccounting`.
- A fixture pass republishes identical bytes.

## Live reading

- **One chair per pass.** A pass with pages still to send starts the chair on a background
  thread before the feeds are built (when the reading deadline admits the start-up timeout and
  every page) and waits for it before the first call; a failed start stops the pass. A pass
  that stops while the chair loads stops it as soon as its load returns. A resumed pass with
  earlier sends, or only re-asks and re-reads left, starts the chair on the first page
  actually sent, so a pass with nothing to send loads no model.
- **Shutdown.** The chair is stopped before the stage seal, so a failed shutdown is never
  reported over a sealed stage, except with `--hand-off-to-coniector` when the
  reconstructor's row shares this chair's service: the chair is left serving for the
  Coniector to take over and stop (`operations/serving/README.md`, "A shared service"), and
  the Coniector reports a failed shutdown.
- **`--perlector-concurrency`** keeps up to that many calls in flight (default and ceiling:
  the launched row's `max_num_seqs`); records are written in the order calls were drawn. A
  page's re-ask is drawn as soon as its first reading is published, when the reading deadline
  admits it with every unfinished call; otherwise it waits for the re-ask phase. A re-ask an
  interrupted pass already sent waits until every first reading is finished. An error
  finishes every page already sent before stopping; an interrupt first records every reply
  that has arrived.
- **`--reading-deadline`** refuses to start, or to send another page, when start-up time plus
  the pages left would run past it.
- The engine's `stop` and `length` are the reading's own words; anything else is
  `call-failed` with its retained bytes named.

## A replay

A replay run (`common/replay.py`, made by `operations/replay/replay.py`) reads a saved run's
pages again with the current code and calls no model. Its `run.json` carries `replay`
(`run-replay.v1`: the source run's id, its authority's self-hash and commit, the stages it
imports, `replies: "recorded"`), and its `repository_commit` is the code that replayed. It
holds the source's Door, Exemplar, Ink map, Designator and Attestatores records byte for byte
and never runs those stages. This stage runs as live, through a real `ChairClient`, but the
handle answers each request with the reply the source retained for the same request bytes
(`operations/serving/replay.py`).

- A first reading or operator re-read whose request the source did not send in exactly these
  bytes refuses the pass.
- A re-ask the source did not send in exactly these bytes is not asked: its `page-reading` is
  `not-run` with problem `not-replayed`; the accounting holds it `reask-unread` and the page
  stands on its first reading. The Recensor accepts that state only in a replay run.
- A source re-ask the current plan does not make is not read.

## Consumer obligations

- Count acts by `kind == "act"` only. A row-kind entry (`index-row`, `table-row`,
  `ledger-entry`) is a row. Rows reach the export today in `other.jsonl`;
  `common.page_types.row_record` defines the planned `rows.jsonl` line (`armarium-row.v1`),
  with the page type from the entry's accounting.
- Recompute every attempt id from (subject, operation, ordinal), and require ordinals 1..N
  without a gap.
- A page's current reading is its last operator re-read, else its first reading with its
  re-ask. Superseded readings are history, never current units.
- Treat `page_holds` and `holds` as binding: a held act is a review item, never delivered
  text.
- Follow the exact Perlectio reference a review names, never whichever reading sorts latest.
- Read `dissent` as a record of departure, never as a score or a vote.

## Stage-completion seal

Before its final manifest the stage publishes one `decode-environment` and one `stage-seal`
(or reuses both on a byte-identical retry), binding the pass's inventory and blob contents,
the decode-environment bytes, the run's `config_digest` and `register_digest`, and the
`(kind, outcome)` census. A pass that never reaches its seal leaves none, and the successor
refuses the missing boundary.

## Not built

- Live serving is proven only against the serving fakes, not on a card.
- `rows.jsonl` is not written; index, table and ledger rows reach the export as `other.jsonl`
  readings. `entry_kind`, `page_type` and `writing` stay on the Perlector's records.
- Pass C, the audit that would flag and re-prove spans of a reading, is recorded as not run.
- A truncated or unknown entry is held, never re-read automatically; a person can ask for an
  operator re-read.
