# Attestatores — contract

The Attestatores retains one immutable `kind="page-testimonium"` for every page
the Exemplar sealed and every configured page witness chair of that page's roster,
on every attempted read. The schedule is the sealed pages: no act list decides what
a witness is shown. A page's roster is every configured page witness, unless the
roster routes one (below). It does not merge, rank, select, or turn a Testimonium into established
text. A missing artifact is never a witness outcome.

## Stage-completion seal

Before its final manifest the stage publishes one `decode-environment` and one
`stage-seal`, or reuses both on a byte-identical retry. The seal witnesses this
pass's disk inventory and blob contents, and binds the exact decode-environment
bytes, the run `config_digest` and `register_digest`, and the `(kind, outcome)`
census. A pass held after publishing stage evidence seals it (holds remain in
its census); a pass that never reaches its seal does not seal, so the successor
refuses the missing boundary.

Seals are compared as the set the stored inventory names, on both sides of the
boundary: the producer refuses to re-seal, and the successor refuses to read,
when any named seal is no longer on disk.

## Inputs

A witness is shown a sealed Exemplar page: Chandra and Churro the whole page,
DAI the record crops its own detector cut from it. A page the Exemplar refused
has no pixels and is not witnessed. The Designator's sealed detector regions are
read after its stage seal verifies, as DAI's units and nothing else. No act
decides whether a page is read.

`page_subject` answers "which page is ordinal N" on every route, from
`common.stage.exemplar_page_ids` over the Exemplar's own `page` artifacts. The
pages witnessed are those whose Exemplar `page` record is `sealed`.

## Witness routing

A models roster may seat a witness on some pages only: `[witness_routing]` names the
chair and its rule (`common/chairs/config.py`). Absent, which is every committed
roster, every configured witness reads every sealed page, nothing below is written,
and a run is byte for byte what it was before routing existed.

The one rule, `index-and-table.v1` (`common/witness_routing.py`), routes a page to
the chair when Surya's layout tags a block on it `Table` (`surya-table`), when the
record detector found no record on it (`zero-detector-records`), or both (`both`).
Both signals are read from the Designator's sealed `surya-page`, `surya-block` and
`detector-page` records, before any witness reads the page, so the decision never
depends on a witness or on the Perlector's page type. A roster that routes a
witness must configure both Designator chairs, may not route every witness, must
leave at least `witness_floor` configured witnesses reading every page, and names
only configured witness chairs; any other is refused when it loads. The preflight's
floor check counts only the witnesses that read every page.

**The record.** Before the first witness is asked, the pass publishes one
`kind="witness-routing"` per sealed page (subject the page id, outcome `recorded`,
schema `witness-routing.v1`): `page_id`, `page_ordinal`, `rule`, `routed_chairs`,
`routed`, `signal` (null when not routed), `surya_table_blocks` (the Table blocks'
subjects) and `detector_record_count`, bound as inputs to the census, the Table
blocks and the detector census it was read from. A resume makes the same records
from the same evidence. The tally re-derives every one and holds when a sealed page
has none. `run-health/witness-routing.json` (`witness-routing-summary.v1`) lists
each page's decision and counts pages by signal; it is a report for a person,
rebuilt on every pass.

**What it changes.** On a page the rule does not route, the routed chair has no
Testimonium, is not shown to the Perlector and is not counted against the witness
floor: the page is read, shown and counted as it is without the chair. On a routed
page it is one more page witness, held to every rule a witness is, and counts toward
the floor like any other (Recensor CONTRACT, "The witness floor"). Every reader of a
page's roster takes it from `common.page_testimonia.page_witness_chairs`.

## dots.mocr

dots.mocr (`dots-studio/dots.mocr` at `e539fbb`, adapter `dots-mocr.v1`) is a layout
reader whose vendor prompt asks for every layout element's box, category and text;
it reads index lists and tables row by row. It is meant to be seated by routing
alone, on index and table pages, and no committed roster seats it.

**What it is asked.** The vendor's own request (`dots_mocr/model/inference.py` at
`rednote-hilab/dots.mocr` `23f3e56`): one user turn, the image first, then one text
part carrying the vendor's image placeholder and `prompt_layout_all_en`, whose
SHA-256 is the one the bake-off recorded on every request to the real model; no
system turn. Its sampling row is the vendor command line's (temperature 0.1, top_p
1.0) and its answer bound 16,384 tokens, reserved whole like Churro's.

**What it is shown.** The sealed page itself (`presented.kind = "page"`); the
model's processor converts and resizes it. The vendor's command line first
re-renders the page through PyMuPDF at 200 dpi, which no sealed-page transform can
replay; this is the vendor's own `--no_fitz_preprocess` path, and not the one the
bake-off scored.

**Its grammar** (`common/dots_layout.py`, parser `layout-json`, text view
`dots-layout-text.v1`): a JSON list of cells `{bbox, category, text}` in reading
order. An answer that is not UTF-8, not JSON (a loop cut at the bound) or not a list
of objects is retained and not parsed, never salvaged as the vendor's cleaner would;
the attempt is `failed`. A cell with a malformed box, an unknown category or text
that is not a string is kept with the grammar's finding. The text view is the
bake-off's: a Table's HTML one line per row, a Formula's LaTeX without `$$`,
Markdown marks removed, a Picture contributing nothing. Each cell's box, in the
pixels of the image the processor made (Qwen2-VL's `smart_resize`, a 28-pixel grid
between 3,136 and 11,289,600 pixels), is mapped back to the sealed page, low edges
floored and far edges ceiled (`dots-smart-resize-floor-ceil.v1`); a box outside that
image places nothing. Each cell with text is one page-feed unit, `layout-block`,
labelled with its category.

**Fixture posture.** `[[dots_page_response]]` rows declare an answer in the vendor
grammar (`raw_json`, `transport_stop_reason` `stop` or `length`), captured and read
exactly as a served answer is; they are read only by a roster that seats a dots.mocr
chair. **Live posture.** Its prompt has no measured token count yet
(`common/request_capacity.py`), so a served request is refused by name as a capacity
refusal, never sent, until one is measured with the pinned tokenizer.

## Two postures, chosen by the sealed catalogue

Which writer runs is decided by each configured witness chair's sealed
serving-recipe row, resolved through `operations.serving.client.serving_mode_for`
(recipe, chair, measured placement tier) in the catalogue the run sealed, re-read
and digest-checked at the moment of use. Never by a configuration key, and never
by a fallback in either direction.

- **Fixture posture** (`kind = "fixture"`, the committed catalogue). Answers are
  declared in the fixture's TOML tables (`testimony`, `witness_failure`,
  `witness_empty`, `witness_not_run`, `witness_malformed`,
  `churro_page_response`, `dai_record_response`, `native_observation`). Their
  `fixture://` serving facts are declarations, not measurements.
- **Live posture** (`kind = "vllm"`). Each chair is served and read through
  `ChairClient`; the call record is `common/contracts/serving.py`'s
  `chair-call-record`, and the capture is `common/native_witness.py`'s retained
  model view.

A roster that mixes postures is refused by name. An absent chair names no mode.
`--placement-tier` is required to resolve a live row and is not sealed: it is a
measured fact of the card, and the receipt records the caps that bound the
serving moment.

**Real ingress.** On a real submission (`run.py::real_ingress`) every configured
witness must be served: `require_every_witness_served` refuses, by chair name and
before any page is read, a real run whose sealed catalogue gives a witness a
fixture row, or in which no witness is served. The fixture accessor refuses by
name if anything reaches it. A live pass prints how many declared fixture rows it
passed over, and prints nothing on a real run.

## The live pass

**Order.** One chair is resident at a time: for each chair with pages left, its
client is entered once and its pages are read in order, then it is stopped.
Churro is asked once per page, Chandra once per page plus its vendor retry loop
(below), and DAI once per record its own detector
found on the page. Every chair keeps up to the launched row's `max_num_seqs` (the
row's own, or the run's `--capacity-plan` width for the card)
pages in flight; Chandra's retry loop holds its records until its page's turn.
DAI's requests are its records, so a page's records go out side by side and the
next page's start while its last are out. Records are still sealed strictly in
page order, a DAI page once its last record has answered. Preflight consults no
chair and writes nothing.

**Resume.** A page record already sealed at this ordinal is kept and its page is
never asked again. A DAI page is sealed only once every one of its records has
been answered, so a pass interrupted inside a page asks that page's records
again. Chandra's retry loop resumes from its own sealed intent and terminal
records (below).

**What a live record carries.** `native_capture` (the adapter's retained model
view), a `provenance.receipt_ref` naming the receipt the chair's client re-read
at start, and its serving call: `serving_call_ref` on a Chandra or Churro page,
one `unit_call_refs` entry per record on a DAI page. Every call record is bound
as an input. A response kept unread (no capture) is named in
`raw_response_refs` and bound as an input itself, so its bytes are re-hashed
whenever the record is read. The writer and the tally (`verify_page_call_sampling`) hold each
call to the chair's sealed sampling row and its receipt's seed; a Chandra page
sends no seed and samples at its returned attempt's ordinal. A live record that
retains a response and names no serving call is refused.

**Truncation** comes from the engine's stop word: `"stop"` → `false`,
`"length"` → `true`, no word → `null` with `truncation_basis = "not-recorded"`.
A response whose stop word is anything else (`live_witness.unmeasured_stop_reason`)
is not read by any adapter: that request is a `failed` attempt whose reason
names the word, with its call record and response bytes retained and bound, and
the pass goes on.

**Capacity.** Before a request is built, `live_witness.request_capacity_or_refuse`
checks it against the sealed row (`common/request_capacity.py`: the row's pixel
bounds and patch geometry, the chair's measured prompt cost and its answer
budget at the scope asked). A request that does not fit is never sent and never
downscaled: `capacity_refusal_attempt` records it as that attempt's own `failed`
outcome, with no-response health, the refusal sentence as `reason`, no
response or call reference, and the chair's real receipt. A request that fits
carries its capacity record onto the call record.

**What is sent.** `common/request_capacity.py::sendable_max_tokens` sends
`max_tokens` only where the chair's declared upstream bound
(`DECLARED_ANSWER_BOUND_TOKENS`) is below what the row leaves; otherwise it
sends none and the engine bounds the answer by the context. The image part goes
before the text part for every chair. Sampling is the sealed
`config/decoding.toml` row for the chair, sent by `ChairClient`; a builder sends
only non-sampling fields (DAI's second EOS id as `stop_token_ids`, Chandra's
`chat_template_kwargs` from `common/chair_wire.py`). `generation_declared` keeps
each vendor's carried generation config as evidence. A vendor float is recorded
as the exact decimal text the request body carried (`wire-decimal.v1`).

**A wire refusal stops the stage, except on Chandra's route.** For Churro and
DAI an HTTP refusal from the engine is the chair's answer to a request that did
leave; its bytes are retained and the pass stops with a `ContractError` naming
it. On Chandra's route an HTTP error is an inference error under the pinned
recipe: it advances the retry loop (below), and exhausted inference errors are
a `failed` attempt.

**Churro framing.** `churro.FRAMINGS` declares two system prompts, both a vendor
artifact's own bytes; `[witness_framings]` in the models configuration names
which one a run asks in, and the name is written onto every Churro capture as
`view.framing`. It chooses the wording of a question before the page is read,
never among readings.

## Chandra

**Grammar.** A served Chandra page is read by `common/chandra_layout.py` under
the parser name `html`: top-level `div`s in reading order, each with `data-bbox`
(four integers on a 0–1000 scale) and `data-label`. Nothing is repaired: a
malformed box becomes `bbox_1000: None` with a `malformed-bbox` finding, a
`Blank-Page` block is kept, a nested `data-bbox` is recorded, and the block
count is reconciled against the raw HTML as a finding. A body in no shape the
reader can place is retained in the `unrecognized-shape` state.

**Geometry.** A block's box is quantized low edges floor, far edges ceil, and
converted to sealed-page pixels by `chandra_layout.to_page_bounds`, with the
sealed page as the denominator. Each observed box carries its span into the page
text. A body with no placed block derives no geometry; the record then carries
the presentation echo.

**Presentation.** `chandra.present` reproduces the vendor's own preprocessing
(`convert("RGB")` and `scale_to_fit`) and publishes the result as an
`adapter-crop` under `chandra-scale-to-fit.v1`, re-derivable from the sealed
page.

**Retry loop** (`chandra_native.py`). The decoding policy seals the vendor's
recipe: one request at `temperature=0, top_p=0.1`, then at most six retries at
0.2, 0.4, 0.6, 0.8, 0.8, 0.8 with `top_p=0.95`. Only the vendor's repeat
detector or an inference error advances the loop; an error waits 2, 4, 6, 8, 10
or 12 seconds before the next request, also after a resume. Every physical
request has a `chandra-native-attempt-intent`, made before HTTP and named by its
call record, and a `chandra-native-attempt` terminal after. Both are written
when the page's turn comes, intent before terminal, so pages read side by side
still seal their records in page order; a page whose loop stops on an error
first seals what it made. A sealed intent with no terminal is delivery-unknown
and refuses resume; it is never replayed. A pass killed while a page's loop is
still out seals nothing for that page, and a resume reads it again from the
sealed records, if any. A response the stage
refuses after it arrived gets a terminal first, so a resume repeats the named
refusal. The final returned attempt alone supplies the page text and geometry;
every request stays reachable through the page record's `native_inference`,
whose `physical_request_count` does not change the one-record-per-chair count.
Exhausted repetition is retained as failed with its text and capture; exhausted
inference errors are failed.

**Fixture placeholder.** The committed fixture's Chandra rows declare
`fixture-chandra-response.v1`, a JSON placeholder read only by
`chandra.parse_fixture_placeholder` under the parser name `json`, and recorded
with `chandra.FIXTURE_PROMPT`. A served Chandra response may never be retained
under `json` (`feeding.retain_model_view` refuses it).

## The capture's text view

A capture read under a vendor grammar records `text_view`, the rule its text and
findings were read under (`chandra-layout-text.v2` for Chandra under `html`,
`churro-historical-document-text.v2` for Churro under `xml`;
`common.native_witness.CAPTURE_TEXT_VIEWS`), whatever the parse state. Other
captures record none. `validate_capture_text_view` refuses a capture naming a
view this build does not read, at every resume point, at the writer, and in
every reader's re-derivation; the refusal says to re-run from the Door.

## Page Testimonium schema

`kind="page-testimonium"`, one per `(sealed page, page chair, attempt ordinal)`,
subject the page's Exemplar identity, attempt
`attempt_id(page_id, f"read:{chair}", ordinal)`. The payload is the closed shape
`common/native_witness.py` declares:

```text
chair, attempt_ordinal, provenance, format_capabilities
payload, witness_reported, content_health
presented, observed
scope = "page", page_ordinal
reason, page_edge_overshoots, raw_response_refs, adapter_metadata,
native_capture, native_inference, presentations, unit_captures, unit_call_refs,
serving_call_ref               only where the record has them
```

A page record names no act; a record carrying an act field is refused.
`serving_call_ref` and `unit_call_refs` are never both present. Before
publishing, a writer builds the exact envelope and checks it with
`common.page_testimonia.validate_page_testimonium_record`, the check every
reader applies, so a record no reader would accept is never sealed.

- `payload` is the witness's JSON-native output, retained as its own shape and
  never flattened. A float, or text that is not valid UTF-8, makes the attempt
  `failed` with `content_health.recordable=false`.
- `witness_reported` is the witness's own self-report, kept as evidence; it is
  never health and a confidence outside the closed ordinal set fails the
  attempt.
- `content_health` is computed here from the native output and a trusted
  response boundary: recordability, emptiness, blankness, character count and
  truncation.
- `format_capabilities` is what the adapter's output grammar can express at
  all, so a witness whose format cannot say "unsure" is not read as confident
  for having said something. It is read off the adapter; the fixture's
  `testimony` rows may declare it.

### Presentation and geometry

`presented` is `{}` or the closed description of exactly one `page`, `region`
or `adapter-crop` image: sealed page identity and ordinal, blob path and digest,
and an executable sealed-page transform (`whole`, `crop`, or
`crop-resize-preserve-aspect` with Pillow LANCZOS and floor rounding). Every
reader regenerates the image from the sealed page and refuses a different
digest.

`observed` is the witness-order list of integer sealed-page boxes, each with a
dense zero-based ordinal, `bounds_source` in `native | derived | presented`, and
an optional span into the record's text. Every box lies inside the presented
image's bounds. A `presented` box restates the image sent and is excluded from
routing and coverage. A native box past the sealed page edge is kept as
reported, never clamped, in `page_edge_overshoots`.

Where observations come from: for an adapter whose response carries page
geometry (Chandra), the partition is derived from the retained response, and
any declared fixture rows are added to it; otherwise declared
`[[native_observation]]` rows for the chair and page stand as `native` boxes;
otherwise the adapter's `observe` runs. The fixture declares a box for Churro,
which the vendor grammar cannot express; `proof/test_fixture_declaration_contract.py`
keeps that divergence visible.

### Adapters

A witness chair is configured with `witness_adapter` (an exact declared name,
no default) and `witness_scope = "page"`; both enter `config_digest`.
`witness_adapters.validate_runnable_adapter_bindings` refuses any other scope.
A declared name joins `common/witness_adapters.KNOWN_WITNESS_ADAPTER_NAMES` and
its binding joins `witness_adapters.RUNNABLE_ADAPTERS`; a configured chair with
no binding is refused by name.

Each binding carries five operations and the grammar's facts:

- `prompt` frames the request;
- `parse` reads one native response into its text (the fixture declaration
  check reads declared answers through it, or through `fixture_parse` where the
  fixture's bytes are not the vendor grammar);
- `retain` records the raw bytes and the exact model view, parsing them through
  `feeding.retain_model_view`;
- `present(context, presentation)` returns the closed `presented` block;
- `observe(presentation, native_payload)` derives the closed `observed` list
  from that exact image and response together;
- `format_capabilities`, `quantization` (the float-to-pixel rule, or `None`
  where the grammar has no coordinates), `takes_page_size`, `resolve_framing`.

The retained raw response is the authority and never loses a float; an adapter
quantizes inside `observe` under its declared rule, and the rule rides beside
the raw reference as `adapter_metadata`. An adapter never mints a region,
expresses a preference, or reports coverage; `primary`, `canonical`, `best`,
`preferred` and `superseded_by` are refused anywhere in a payload.

Which vendor sits in which chair is fixed in code as well as configuration:
per-vendor answer bounds, Chandra's retry loop and its wire flags are keyed by
chair or adapter (`live_witness`, `common/request_capacity.py`,
`common/decoding.py`). Repinning a revision of the same vendor is a
configuration change; moving a vendor to another chair is a code change.

## DAI: a page witness over its own detector's records

DAI was trained on crops of the records its project's detector finds, so a
`dai.v1` chair is shown its page that way
(`common.page_witness_units.reads_detector_records`); a roster with such a
chair and no configured `secondary_proposer` is refused before anything runs.

**Units.** A page's units are the Designator's `detector-region` crops of that
page, in the detector's order, read from its `detector-page` census. A missing
or out-of-order record, a census whose count disagrees with its records, or a
crop that does not re-derive from its sealed page refuses. A page with no census
refuses.

**One call per record.** Each unit is one request; every response is retained as
it arrives. A unit refused for capacity, or answered under an unmeasured stop
word, is a `failed` unit.

**A page the detector found nothing on.** When the census counts no record and
the detector's run facts state its cap (`max_det`), the page is sealed without a
request as `genuinely-empty`: `payload` `""`, empty-text health, `presented={}`,
no observed box, no receipt, one input (the page's `detector-page` census), and
the fixed reason that DAI's own detector found no record below its cap. Every
reader re-derives this (`common.page_testimonia.is_detector_blank_testimony`).
It counts toward the witness floor like any reading. A page with no crop that is
not such testimony (no cap stated, or records none of which enclosed a crop) is
sealed `not-run` with a reason naming which.

**The page record** adds `presentations` (every image shown, in unit order;
`presented` is the first), `unit_captures` (one model view per presentation;
`null` for a unit that never reached the chair, or whose answer arrived but did
not parse or was kept unread under an unmeasured stop word) and `unit_call_refs` (each
unit's call record, `null` for a unit not sent). `observed` has one `presented`
box per unit, with a span into the page text where that unit was read. Unit
readings join with `"\n"` in the detector's order; an empty reading adds no
separator. One failed unit fails the page, with a reason naming each failed
record; otherwise the page is `read`, or `genuinely-empty` when every unit was
read empty.

**Fixture posture.** A fixture row reads each record exactly as a served one is
read; only the answer is declared, one `[[dai_record_response]]` row per record,
retained under the stop word `fixture-complete`. A record with no declared
answer, or two, refuses by name.

## Outcomes and provenance

Every page witness chair has one outcome per sealed page per attempt:

- `read` and `genuinely-empty`: the chair was shown the page and a recordable
  response to that exact request was retained, with a serving receipt; they
  differ only in whether the text has characters. The one `genuinely-empty`
  with no response is DAI's blank testimony above.
- `failed`: an attempt reached the response boundary, or was refused for
  capacity, and produced no usable reading. It carries a reason.
- `not-run`: a configured chair was never attempted on the page. It keeps the
  resolved pin but no receipt, and every content-health fact is `null`.
- `excluded` is never produced by this writer.

An absent chair gets no record; later stages see it in the sealed roster and
count it against the witness floor. `provenance` holds the exact resolved
identity and revision and, for attempted outcomes, the digest-checked serving
receipt. A failed chair is never replaced by another.

The stage holds, and every hold stops orchestration, on an `UNKNOWN` attempt
tally or a pass refused by its own no-write preflight (bytes that differ from an
attempt already sealed at that ordinal, an ordinal past the next appendable one,
two fixture responses for one page and chair at one ordinal, a declaration
naming a page or chair no pass reads, a page whose witness layer is closed).

## Retention and current state

`--attempt-ordinal N` (default `1`) is the whole pass: every page witness chair
on every sealed page, at that one ordinal. For each `(page, chair)` the writer
permits only a byte-identical repeat of an ordinal already held, or the next
contiguous one, so the same command twice is a resume. The RunTree's publish
boundary refuses different bytes at an existing identity; there is no overwrite
path. Consumers take the current attempt per `(page, chair)` through
`latest_attempt()`, so a later `failed` attempt is current and the earlier
reading stays retained.

**The witness layer closes at the first reading.** Once the Perlector's page
feed for a page shows witnesses, an appending pass over that page is refused
before anything is written: asking a witness again because it spoke is a
re-roll. A pass that only repeats sealed attempts is a resume.

Fixture response declarations are bound to an ordinal: a row without
`attempt_ordinal` describes attempt 1 only, and with nothing declared at its
own ordinal the attempt is `not-run`. A row scoped to the run's scenario
replaces the unscoped rows for that page and chair.

## Attempt tally

The stage's manifest is rebuilt from the page Testimonia, compared to the stored
inventory, and each record is checked against the shared page-record validator,
the facts this stage computes, the page roster, its native capture's bytes and
every retained call record's sampling. `attempt_tally()` returns `KNOWN` only
when the inventory is whole. An absent, garbled, truncated or divergent
inventory returns `UNKNOWN`, `count=null`, `hold=true`, before anything is
written. A stored inventory whose attempts are gone still holds rather than
starting again at attempt 1. An accounting imbalance (`FatalAccounting`) is
fatal, never a hold, and no seal is published.

A `failed` attempt with `content_health.recordable=false` and a reason is a
counted record: the tally stays `KNOWN` and the page is visibly under-witnessed.
Two unrecordable shapes are `UNKNOWN`: a `read` or `genuinely-empty` record that
retains nothing it read, and a `failed` record with no reason.

Whether every sealed page and chair is accounted for is a closing check, not a
precondition: a pass killed part way leaves attempts and no stored manifest,
and the next pass supplies the missing pairs. A folder that lost its stored
manifest after a seal holds until `RunTree.write_manifest("attestatores")`
re-derives it from the immutable attempts. Over a folder whose attempts are
gone, that step discards the last record that they existed; read the manifest
first.

## Not built

- Witnesses do not use the Perlector's streamed loop detector. A witness reply is read
  to its end and scanned afterwards for repetition; it is not abandoned at the first
  looping line as the Perlector's is (`common/repetition_loop.py`, the guard in
  `config/decoding.toml`).
