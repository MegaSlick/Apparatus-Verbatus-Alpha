# Attestatores — contract

The Attestatores retains one immutable `kind="page-testimonium"` for every page
the Exemplar sealed and every configured page witness chair, on every attempted
read. The schedule is the sealed pages: no act list decides what a witness is
shown. It does not merge,
rank, select, or turn a Testimonium into established text. A missing artifact is
never a witness outcome.

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
## Exact input boundary

A witness is shown a sealed Exemplar page: Chandra and Churro the whole page,
DAI the record crops its own detector cut from it. A page the Exemplar refused
has no pixels and is not witnessed. The Designator's sealed detector regions are
read after its stage seal verifies, as DAI's units and nothing else. No act
decides whether a page is read.

This stage has two writers, and which one runs is decided by the sealed
serving-recipe row for each configured witness chair — never by a new
configuration key, and never by a fallback in either direction. Under the
committed fixture catalogue every chair is `kind = "fixture"` and the writer is
the declared synthetic skeleton described here: its `fixture://` serving facts
are fixture declarations, not measurements of a live model, and its bytes are
the pinned acceptance path. Under a catalogue whose rows for those chairs are
`kind = "vllm"`, the live boundary below runs instead: the serving response and
body contract is `common/contracts/serving.py`'s `chair-call-record.v3` plus
`operations/serving/http.py::parse_openai_reading`, and the capture-as-Testimonium
intake is `common/native_witness.py`'s retained model view, which the live pass
records on every page attempt whose bytes reached an adapter parser.

## Live response boundary

**Selection.** `main` resolves one mode per configured witness chair through
`operations.serving.client.serving_mode_for` — a three-name lookup (recipe,
chair, measured placement tier) in the catalogue the run sealed, re-read and
digest-checked against `config_digest` at the moment it is used. A roster that
mixes postures is refused by name: one run reads its witnesses one way, or every
consumer comparing witnesses across a page is comparing two kinds of evidence
without being told. An absent chair names no mode and stays `dead`.
`--placement-tier` is required to resolve a live row and is deliberately not
sealed: it is a measured fact of the card, and the receipt records the caps that
actually bound the serving moment.
**Pass structure.** The live pass is chair-outer and publishes per response.
Preflight stays a no-write preflight and consults no chair. Serving runs through
`feeding.stage_major_schedule` and `feeding.execute_stage_major_schedule` under a
`SingleChairResidency`, one schedule per chair concatenated, so one chair is
resident at a time, no unit is served twice, and a schedule that returned to an
unloaded chair is refused. A schedule unit is one sealed page
(`{unit_id, page_ordinal}`, the unit being the page's Exemplar identity): Chandra
and Churro are asked once per page, and DAI once per record its own detector
found on the page, all inside that page's unit.

**Resume.** A page record already sealed at this ordinal is kept and its page is
never asked again — a live chair cannot reproduce immutable bytes. A DAI page is
sealed only once every one of its records has been answered, so a pass
interrupted inside a page asks that page's records again. Chandra's native retry
loop resumes from its own sealed intent and terminal records (below). A sealed
capture read under a text view this build no longer produces is refused by name
at every point a resumed pass reuses one (the page record and a Chandra terminal
record), before its bytes are read; see "The capture's text view" below.

**Closed asymmetry: cut-off composition is shared.** A parse failure landing on
a response the provider itself cut off at its bound now reads as an exhausted
bound, not plain bad ink, on both paths alike. `_failed_parse_composition`
holds the one composition — the `cut_note`-prefixed `reason` suffix and the
`transport_stop_reason`-bearing `_unrecordable_health` basis — and both
`captured_page_attempt` and `live_attempt_from_response` call it on their
parse-failure branch, so the two cannot drift apart again. A DAI record
response is evidence of the same kind as a whole-page one; a truncation fact
the provider actually reported survives on both paths alike.

**What a live record gains.** `native_capture` (the adapter's retained model
view) is admitted on a page Testimonium and written only in live mode or for a
fixture Churro page declared as raw bytes, so every other fixture record is
byte-for-byte what it was. `provenance.receipt_ref` names the receipt the
chair's own client re-read at start, never a declared `fixture://` stand-in.
`content_health.truncated` comes from the engine's stop word: `"stop"` →
`false`, `"length"` → `true`, and an unreported word → `null` with
`truncation_basis = "not-recorded"`. Every retained call record is linked
from its page record and bound as an input: DAI's page record names each
record's call in `unit_call_refs`, and a Chandra or Churro page record names
its one request's call in `serving_call_ref`. The writer and the tally
(`verify_page_call_sampling`) hold each call to the chair's sealed sampling
row and its receipt's seed; a Chandra native page sends no seed and samples at
its returned attempt's ordinal. A live page record that retains a response and
names no serving call is refused, so no response's sampling goes unchecked.

**Chandra is a served witness like the others, reading its vendor's own
grammar.** Every witness runs its own full pass, and nothing is captured
from one call into another. What the served chair parses is the vendor's own layout HTML, read by
`common/chandra_layout.py::parse_layout_html` under the parser name `html`
(U9 of the vendor systems design). A body in that grammar is a reading -- page
text, and block geometry from each `data-bbox` in sealed-page pixels with a span
per block into that text. A body in no shape the reader can place lands with its
bytes retained and its shape named in `outcome`, in the `unrecognized-shape`
state. The premise the retired JSON contract rested on -- that the vendor
publishes no output specimen -- was true of the model card and false of the
repository, which ships both the prompt every caller sends and the parser for
the answer it asks for; both are carried and digest-pinned now, so what this
chair is asked and what it is read as are the vendor's, not this repository's.
**Fixture declarations a live pass does not read.** A live pass reads the
fixture's pages — that is the corpus — and reads none of its declared witness
responses (`testimony`, `witness_failure`, `witness_empty`, `witness_not_run`,
`witness_malformed`, `churro_page_response`, `dai_record_response`) or declared
`native_observation` geometry, which are the offline posture's stand-in for a
model. The pass names on stderr how many such rows it passed over, so an
operator cannot mistake one posture's record for the other's.

### The Chandra layout grammar

`common/chandra_layout.py` re-expresses `chandra/output.py::parse_layout` over
the standard library and is what a served Chandra page response is read by,
under the parser name `html`. An answer is a sequence of top-level `div`s in
reading order, each carrying `data-bbox` (four integers normalized to
`BBOX_SCALE = 1000`) and `data-label` from the vendor's own label vocabulary.
Nothing is repaired: a malformed or out-of-range `data-bbox` becomes
`bbox_1000: None` plus a `malformed-bbox` finding where the vendor prints a
line and substitutes `[0, 0, 1, 1]`; a `Blank-Page` block is retained where the
vendor drops it, with no geometry and with its text kept like any other
block's; a nested `data-bbox` is recorded
rather than stripped; and the block count the reader returns is reconciled
against the raw HTML's own top-level `div` count as a finding. Those findings
travel on the capture beside the reading, because they are the
whole of what the vendor's parser would have printed to a stdout nobody
retains. A body the reader can place in no shape at all lands in the
`unrecognized-shape` state naming what it saw in `outcome`, with its bytes
already retained.

### The capture's text view

A capture read under a vendor grammar records `text_view`, the named rule its
parse text and findings were read under: `chandra-layout-text.v2` for Chandra
under `html`, `churro-historical-document-text.v2` for Churro under `xml`
(`common.native_witness.CAPTURE_TEXT_VIEWS`). The writer takes it from the
parser's own result where the parser reports one and from that table where it
does not (a Churro body that failed to parse, a Chandra body in no shape), so
it is recorded whatever the parse state. A capture of any other adapter and
parser — DAI's `text`, the fixture's `json`, no parse — records none, and one
that names a view anyway is refused.

`v1` of each view is retired: Chandra's dropped a `Blank-Page` block's text and
Churro's did not report document text outside every page, so a capture read
under either differs from what this build reads from the same bytes.
`validate_capture_text_view` refuses a capture naming a retired view, or none
where its grammar has one, by that name and whatever its parse state, before
anything re-derives it: at the resume points above, in
`verify_native_capture_bytes` (every reader's re-derivation) and at the writer
itself. The refusal says to re-run the submission from the Door. A new
Attestatores pass over the same run tree would resume the sealed capture and
meet the same refusal, and no stage starts a run tree past the Door, so a fresh
run is the only route that reads the page again.

**Attestator 1 carries Chandra's pinned native inference loop.** New runs seal
the decoding policy's exact `datalab-to/chandra@d4f7467` recipe: the initial
`temperature=0, top_p=0.1` request and at most six retries at temperatures
0.2, 0.4, 0.6, 0.8, 0.8, 0.8 with `top_p=0.95`. Only the vendor's literal
repeat detector (including its cut-last-50 probe) or an inference error advances
the loop; only errors wait 2, 4, 6, 8, 10, then 12 seconds. This capability is
restricted to the page-scoped `attestator_1`/`chandra.v1` route.

Every physical request has an immutable `chandra-native-attempt-intent` artifact
(`chandra-native-attempt-intent.v1`) before HTTP and a terminal
`chandra-native-attempt` artifact (`chandra-native-attempt.v1`) afterward. Page
Testimonia bind both through `native_inference`; their validators require
the referenced evidence rather than treating the compact trace as a free-standing
claim. A crash that leaves an intent without terminal evidence is delivery-unknown
and refuses manual-free resume; it is never replayed blindly. A retained response
that this stage cannot publish receives a terminal record first, so resume repeats
the named refusal instead of misreporting known delivery as unknown. The final
vendor-returned attempt alone supplies Testimonium text and geometry. Earlier
requests, responses/errors, captures, parameters and triggers remain reachable
through `native_inference`, whose `physical_request_count` does not alter the
one-Testimonium-per-chair denominator. Exhausted repetition is retained as
failed/partial with its text and capture; exhausted inference errors are failed.
An HTTP error is an inference error under the pinned recipe: attempts one through
six wait 2, 4, 6, 8, 10 and 12 seconds respectively before the next request (42
seconds total on full exhaustion), including after crash/resume. The older
chair-neutral post-hoc repetition finding remains a diagnostic over the returned
capture, but it does not schedule a request and is not the vendor trigger.

**The placeholder is offline only.** `proof/skeleton_fixture.toml`'s Chandra
rows still declare `fixture-chandra-response.v1`, a JSON placeholder this
repository invented for a fixture that asks nothing of anybody, and their bytes
are pinned into the fixture's own digests until U16 re-declares those rows in
the vendor grammar. It keeps its own parser name, `json`, and its own reader
(`chandra.parse_fixture_placeholder`) -- retained as history, never parsed as
the live grammar. The refusal that keeps the two apart is at the retention seam
rather than inside the parser: `feeding.retain_model_view` takes a `served`
flag, both live call sites in `live_witness.py` set it, and a served
`chandra.v1` response may not be retained under `json` at all. The parser name
is what the record will carry, so a live capture written under `json` could
never be re-derived as the grammar it was actually asked in. Bytes are retained
before any parser runs, so the refusal names a surprise rather than losing one.

**The prompt is split by posture.** `chandra.prompt()` sends
`chandra_layout.OCR_LAYOUT_PROMPT`'s carried bytes -- the vendor's own
`ocr_layout` prompt, digest-checked at import -- as one `user` turn with no
system message, which is the shape `chandra/model/vllm.py` builds. The fixture
posture records `chandra.FIXTURE_PROMPT` in its retained model view instead
(`run.py::_fixture_chandra_attempt`): that view is sealed into the fixture's pinned
bytes, the fixture never asks a chair anything, and changing what a served
chair is asked may not move a fixture byte.

**Geometry converts once.** A block's `bbox_1000` is quantized
low-edges-floor / far-edges-ceil in normalized space and converted to
sealed-page pixels by `common.chandra_layout.to_page_bounds`. The
denominator is the *sealed page*, not the resized view the chair was shown,
because the vendor's own denominator is the same one. That conversion clamps
the far edges to the page, and a component outside [0, 1000] is malformed
before it reaches the conversion, so a normalized box can never overshoot the
sealed page. `chandra.observe` takes a keyword `page_size` for it, so the
presentation's bounds are never the denominator, and a body that needs the
size without one is refused rather than placed in the wrong space. Each
observed box carries the block's span into the retained page text. A body whose
blocks all report no rectangle -- `Blank-Page`, or a `data-bbox` the reader
refused -- and a body with no block at all derive none; the page record then
carries the presentation echo `run.py` gives every page with no reported
geometry, excluded from routing and coverage by its `bounds_source`, and the
adapter never hands an echo to the shared page-edge check, which admits
reported geometry only.

**The presented page is the vendor's own pixels.** `chandra.present` reproduces
what the vendor's inference path does to a page before its model sees it --
`chandra/input.py::load_image`'s `convert("RGB")` and
`chandra/model/util.py::scale_to_fit` -- and publishes the result as an
`adapter-crop` under the operation name `chandra-scale-to-fit.v1` with
`colour_mode = "rgb"`, so the exact image the chair saw re-derives from the
sealed Exemplar (ARCHITECTURE invariant 3). The colour step runs after the
resize where the vendor runs it before, because the replay in
`validate_presented_page_binding` is colour-last for every adapter and the two
orders are the same pixels on every mode a sealed crop can arrive in. Only a
whole-page presentation is transformed; any other presentation is refused.
**A live page's partition is derived from the page response itself.** The page
capture's bytes are retained once, and the partition (`observed`) is derived
from those bytes by the adapter's `observe`. The fixture's Chandra page is
declared as its own retained responses (`raw_responses`), and its partition is
derived from them the same way.

### The cross-file seams that let a live pass carry every chair

Four gaps once stood between the live boundary and the committed roster, each
of them a named refusal rather than a silent default, and each in a file the
unit that found it did not own. All four are closed, and how they were closed
is part of the record because each turned on a choice about what a record may
say.

1. **A vendor's float decoding value is recorded as the exact decimal the wire
   carried.** DAI's sampling values are floats — its shipped
   `repetition_penalty` 1.05 and `top_p` 0.001 — and the shared canonical
   writer refuses floats outright, so a live `dai.v1` request could not be
   recorded and was therefore never made. The canonical refusal stands: a
   float's JSON form is not stable enough to hash against. What the call record
   holds instead is the decimal *text* the request body itself contains, tagged
   `wire-decimal.v1` so it cannot be confused with a string the vendor really
   declared, and `ChairClient.read` proves on every call that the recorded view
   re-encodes to exactly the JSON that went on the wire before it writes the
   record. Nothing is rounded, and a value that could not be transcribed —
   `NaN`, `Infinity`, or a vendor value shaped like the tagged form itself — is
   a named refusal before the request is built, not a discovery afterwards.
2. **The DAI identity transform is a claim about bytes, not about paths.** When
   a record crop needs no resize — which is every record crop in the reference
   fixture — the model must be shown exactly the source image, and
   `feeding.dai_model_view` now requires the two references to name the same
   SHA-256 rather than to be the same reference dict. They legitimately differ:
   the source is the Designator's record crop under `2_designator/`, and every
   image a witness is shown is inventoried under `3_attestatores/`. Both are
   `crop_png` of the same sealed page at the same bounds, and
   `_verify_detector_region` already proves the first of them is, so equal
   digests are equal pixels. Held to the whole dict, the rule refused a genuine
   DAI reading *after* its response had already come back.
3. **The truncation the page contract re-derives has three states.** A Churro
   page record's health is re-derived from its capture, and the question asked
   was two-valued — "is this a cut-off word" — so an engine that reported
   nothing answered "no" and the record published `truncated: false` over a
   boundary nobody observed. `common/native_witness.py` measures the third
   state: unknown, with `truncation_basis = "not-recorded"`, the same shape the
   live boundary already derived. The two measured states reconcile exactly as
   before. An empty reading is a confirmed blank only when the boundary
   positively said the model finished, so "cut off" and "never said" both owe
   the record a failed-attempt reason.
4. **A parser may say it read the whole body and could place no shape it
   knows.** `unrecognized-shape` is a distinct state from a parse failure — the
   parser ran and refused nothing — and `chandra.py` produces it for every body
   outside its two declared shapes, because the vendor publishes no response
   specimen to parse a native mode against. The shared capture contract admits
   it, naming the shape in `outcome`, so a live Chandra record carries the
   adapter's own account of its bytes beside the bytes themselves instead of
   dropping the view for want of a state name.

**Every chair derives a generation-bound decision from the sealed row, sending
`max_tokens` only where the declared bound requires it, and the image part
goes before the text part.** Two corrections to what this seam feeds, landed
together.

*Part order.* All three occupants were fine-tuned with the vision block before
the instruction — DAI's model-card snippet and this project's own old
`pilot_crops_dai.py`, Chandra's `model/vllm.py`, Churro's provider — and each
chat template emits a message's content parts in list order, so the order *is*
the token sequence the model sees. Every builder here sent text first; all
three now send the image first (`live_witness._user_content`), and one test
pins it at all three builders together, because the defect was that they agreed
with each other and disagreed with every upstream. No measured token count
moves: the sealed prompt constants are taken over the message texts and
digested over those texts in order.

*The bound.* `common/request_capacity.py::sendable_max_tokens` decides
`min(the chair's declared upstream bound, max_model_len − image tokens − prompt
tokens)` from this request's own capacity record, and **expresses the row term
by sending no `max_tokens` at all** — which is the same quantity, measured by
the component that holds the tokenizer. Our prompt count is a measured floor
vLLM's assembly has never been observed to agree with, so putting it on the
wire would turn a one-token undercount into an HTTP 400 before generation, on a
billing card. A value goes out only where the *declared* bound is strictly
smaller than what the row leaves, and the gap between the two is then also the
margin against an undercount.
`DECLARED_ANSWER_BOUND_TOKENS` carries the four bounds with their sources —
Chandra 12,384 (`chandra/settings.py::MAX_OUTPUT_TOKENS`), DAI 1,024 (its model
card's own `model.generate`), Churro 25,000 (the vendor's `DEFAULT_OCR_MAX_TOKENS`,
`src/churro_ocr/providers/specs.py:77` at v0.3.0). Every Churro row carries a
32,768-token context that holds it, so Churro sends its bound on every tier and
`request_capacity_or_refuse` reserves the whole 25,000 for a Churro page: a row
that could not hold it refuses the request instead of letting the engine cut
the answer short. Churro alone used to send a bound,
and only where the row could hold the whole declared value beside the prompt;
Chandra and DAI sent **nothing**, which is not the same as being unbounded —
with no `max_tokens` the engine sets the answer budget to `max_model_len −
prompt` itself, so a DAI crop could generate some 7,700 tokens against a
1,024-token upstream bound on a card billing by the hour. On every row this
catalogue ships that is the case for DAI alone; Chandra's 12,384 is above what its
24/48 GB rows leave, so it sends none there and behaves as before. A `"length"` stop means the vendor's bound
wherever one was sent and the context wherever none was, and the retained
chair-call record's `generation_sent` says which.

*Unknowns to verify on the next pod.* (1) Whether vLLM 0.27.1 honours
`--mm-processor-kwargs` `min_pixels`/`max_pixels` for Qwen3-VL (Chandra, the
Perlector): compare `usage.prompt_tokens_details` image-token counts with
`request_capacity`'s count for the same image. (2) vLLM's default
`--limit-mm-per-prompt` against the Perlector's requests of up to 32 images.

The declaration is untouched: `generation_declared` still
carries Churro's `max_new_tokens` and DAI's whole carried
`generation_config.json`, and the retained Churro model view still requires the
bound. `test_live_witness.py` walks every Churro row in the shipped catalogue at
every tier and asserts the sum this seam would send is one that row can take.

*Sampling is the sealed table's, never a builder's.* Each chair reads at its
makers' recommended sampling values, sealed per chair in
`config/decoding.toml`'s `chair_decoding` table with the source and revision
they were read from: Chandra's own first request (`temperature` 0.0, `top_p`
0.1), DAI's and Churro's `generation_config.json` (the makers publish nothing
beyond it, and their own pipelines send nothing else). Each row also names every
other sampling field vLLM would fill from a generation config, at the value the
maker's own pipeline runs under: vLLM's defaults for Chandra, which Chandra's code
serves with vLLM, and transformers' defaults for DAI and Churro, whose makers run
`model.generate`. Every serving row is `generation_config = "vllm"`, so no file
fills a field. `ChairClient` sends exactly that row with the seed and refuses a
builder that names any sampling field. The tally holds every Testimonium's serving
call record to its chair's row and seed (`common.stage.verify_retained_call_sampling`):
the serving receipt's seed, or none for a Chandra native page read, whose record
names its vendor-returned attempt. The builders send only non-sampling fields: DAI's second EOS id 151643
as `stop_token_ids` (`feeding.dai_wire_stop_token_ids`, derived from the
carried config rather than re-typed), and `chat_template_kwargs:
{"enable_thinking": false}` on both Chandra chairs (`common/chair_wire.py`,
which carries the evidence that the revision ships two disagreeing chat
templates and why the flag is safe under either).

**Churro is asked in a named framing, and the name is on the record.**
`churro.FRAMINGS` declares two, and both are a vendor artifact's own bytes —
`registry-v0.3.0`, the string `providers/specs.py::resolve_ocr_profile` returns
for this model id at tag `v0.3.0`, and `paper-harness-ed09bc7`, the
`SYSTEM_MESSAGE` the paper's own benchmark harness sent — and
`config/models-real.toml`'s `[witness_framings]` names which one a run asks in.
The default is the registry's current answer; which of the two the fine-tuning
itself saw is stated nowhere in the paper, the model card or the code, so the
comparison is a Stage 2 arm rather than a guess made here.
`witness_adapters.framing_for` resolves it
once per pass from the sealed roster, `run.py` hands it to both live seams, and
the resolved name is written onto every Churro capture as `view.framing`. This
is not a picker: it chooses the wording of a question before the
page is read, never among readings, and it is recorded rather than inferred.
Both framings carry their own measured prompt cost (27 and 29), because a
framing whose cost nobody measured would be refused at the capacity check —
which would make the selector a choice between one option and an error.

**Whether a request *fits* is a different question, and it is now asked of both
page chairs and of DAI.** The bound above governs what may be *sent*; it cannot
say whether the request the engine receives is admissible at all. A whole
300-dpi page costs Chandra 1,715 prompt tokens and Churro 2,280 at the smallest
tier's `max_pixels`, before a word of prompt is counted. `live_witness.
request_capacity_or_refuse` computes that arithmetic from the sealed row's own
`min_pixels`/`max_pixels`/`patch_size`/`merge_size` (`common/request_capacity.py`)
plus the chair's measured prompt cost and its measured answer budget at the
scope it was asked at — a page's answer for Chandra and Churro, one record's for
DAI, so that reserving a page's answer never refuses an ordinary record crop that
measurably works. A request that does not fit is refused by name before it is
built, and the refusal carries the whole record. One that does fit carries the
record onto the request, and the client copies it onto the retained call
record. Nothing is ever downscaled to make a request fit.

**A refused request costs its own attempt, not the pass.** The refusal above is
a fact about one request -- these pixels, at this row's `max_pixels`, against
this row's `max_model_len` -- so `run.py::capacity_refusal_attempt` records it
as this attempt's own `outcome="failed"`, in the same shape an empty or
malformed response takes, and `_serve_page_unit` or `_serve_detector_page` moves
to the next unit, as the Designator holds a single page and publishes the rest. A
missed act is worse than a poorly read one, and one page's arithmetic
is no reason to lose another page's reading.

What that record says, and what it refuses to say: the **no-response** health,
because nothing arrived and there is no channel to call unrecordable; no
`raw_response_ref`, `serving_call_ref` or `native_capture`, because there was
no call and no bytes; the refusal's own sentence as `reason`, which is the
capacity record in words -- every image's token cost, the prompt, the reserved
answer, the need, the row, and by how much it overran; and the chair's real
serving receipt, because the chair did start and this pass entered its client
before the arithmetic refused it. `arrived` in the page-record loop is decided
by retained bytes rather than by the presence of a capture, so a refused page
cannot borrow the health of a body nobody received. A resumed pass reads that
pair -- no serving call, no-response health, live receipt -- and lets the record
stand for the page it already described rather than refusing it as a
fixture-posture record.

**A wire refusal is still the stage's refusal.** Only the *pre-send* arithmetic
became a per-attempt failure. An HTTP 400 is the engine refusing a request that
did leave, from a chair that was asked; its bytes are retained and the stage
stops and says so, rather than publishing a Testimonium about a response it
declined to read. `test_attestatores_live_pass.py` drives both through
`_serve_page_unit` and asserts the two different endings.
**The client normalizes the receipt reference, and both stage-side converters
are gone.** `ServiceHandle.receipt_reference` is a read-only mapping proxy and
`RunTree.read_run_receipt` requires its own reference type or a plain `dict`;
both boundaries are right and neither is loosened, so `ChairClient.__enter__`
copies on the way in (`operations/serving/client.py`, asserted by
`operations/serving/test_client.py::test_the_tree_receipt_reader_is_wired_bare_with_no_stage_side_converter`,
whose stand-in reader refuses exactly what the real one refuses). Each stage's
own converter was a `dict()` over an already-plain `dict` from that moment on,
and both are now removed: this stage passes `read_receipt=context.tree.read_run_receipt`
at its construction site, and `pipeline/4_perlector/run.py::_read_receipt_through`
is deleted along with the one line in `pipeline/4_perlector/test_live_perlector.py`
that named it. The client's `__enter__` is the sole caller of `read_receipt`, so
both removals are behaviour-identical, and the stale claim that the bare wiring
"refuses every live start" went with the comments that carried it. One converter
of the same shape survives in this stage's own `test_attestatores_live_pass.py`,
where it is a local test fixture rather than a stage seam; it is equally
redundant and equally harmless, and whoever next edits that file may drop it.
**A live Chandra page record names its response once, through its capture.**
`run.py::_named_once` de-duplicates a page record's published `inputs` by
`relative_path` before the envelope writer runs, because
`common/contracts/envelope.validate_input_refs` refuses any repeated path. On
the live path the two fields stay disjoint by construction: the partition list
`raw_response_refs` stays empty, the capture names the bytes, and the geometry
is derived from them (`adapter_metadata` is absent from the live page record
because the shared contract ties it to `raw_response_refs`). The one exception
is a page-edge overshoot finding, which the shared contract requires to be
traceable through `raw_response_refs`; the Perlector compares one entry per
`relative_path`, so naming the capture's bytes there as well is accepted.

**The offline Churro observation is declared, not measured.**
`proof/skeleton_fixture.toml`'s `[[native_observation]]` for `attestator_3`
states a page box outright, and `run.py::_fixture_native_observations` publishes
it as `bounds_source "native"` without consulting the adapter, over geometry the
live chair cannot produce (`HistoricalDocument` has no coordinate vocabulary).
`proof/test_fixture_declaration_contract.py` pins `attestator_3` as the one
chair declaring a box its own adapter reports it cannot express, so the
divergence stays visible.

## Real ingress

`main` opens through `common.stage.open_stage_context`, which reads the run
authority once and decides the route from its ingress record. On a real
submission the context carries the registry, the sealed digest map this stage
requires (`decoding`, and the real-only names the Door seals), the parsed
serving inputs, `fixture=None` behind an accessor that refuses by stage name,
and `REAL_SCENARIO` -- never `--scenario`, which the real route does not read.
`run.py::real_ingress(context)` is this stage's one reading of the route, off
`context.run`; nothing here branches on `context.scenario` or on the shape of a
fixture.

**The only real posture is every witness served.** Every witness runs its own
full pass, with no capture and no slicing, and a roster where every configured
witness row is served is the only real posture. `require_every_witness_served` refuses, by chair name and before
any page is read, a real run whose sealed catalogue gives a configured witness a
fixture row, and a real run in which no witness serves at all; there is no
fixture to answer for a chair on a real submission, so a fixture row there is
not a second posture but a chair nothing can ask. The mixed-posture refusal in
`witness_serving_modes` stays as a guard for the fixture-live seam; it does not
fire on the shipped `config/serving_recipes_real.toml`, whose witness rows are
live at every tier in `config/pod_placement.toml`, and
`test_attestatores_real_ingress.py` holds that.
**`page_identity` is the Exemplar page index on both routes.** Every "which
page is ordinal N" -- the whole-page presentation, the page Testimonium's
subject, the live schedule's page unit -- goes through `page_subject`, which is
`common.stage.exemplar_page_ids` over the Exemplar's own `page` artifacts. The
pages witnessed are those whose Exemplar `page` record is `sealed`
(`sealed_pages`). The index is built once per pass and threaded into every
`page_subject` / `presentation_for_page` call as `page_ids`: the Exemplar layer
is sealed before this stage opens, so the walk answers the same every time for
the life of the process.

**The declared witness tables have no real-mode counterpart.** The tables named
under "Fixture declarations a live pass does not read" are the offline
posture's stand-ins for a model; on a real run every witness is served, so every
real pass is a live pass and none of their readers (`declared_page_response`,
`fixture_page_attempt`, `captured_churro_page_attempt`,
`_fixture_native_observations`) is reached. If one ever is, the fixture accessor
refuses by name rather than handing back an empty table. Native geometry on a
real run is the adapter's `observe` over the real payload or the presentation,
never a declaration. `refuse_unread_fixture_declarations` prints nothing on a
real run: there is no row to pass over.

**What is proven offline.** `test_attestatores_real_ingress.py` carries a real
submission of the synthetic fixture's own two pages through the Door, the
Exemplar and the Ink Map as programs, hand-builds the Designator's regions and
seal in the shape `cut_minted_region` publishes them, and runs this stage's
`main` with three served fake chairs: every page record publishes, the page
records name the Exemplar's own page subjects, no declaration is read or
reported, and the fixture accessor is never touched. The same file holds the
seal refusal over an unsealed Designator (context opened, nothing written), the
fixture-catalogue refusal, and the shipped real catalogue's posture at every
tier.

## Page Testimonium schema

`kind="page-testimonium"`, one per `(sealed page, page chair, attempt ordinal)`,
subject the page's Exemplar identity, attempt
`attempt_id(page_id, f"read:{chair}", ordinal)`. Its payload is the closed shape
`common/native_witness.py` declares (`PAGE_TESTIMONIUM_REQUIRED_FIELDS` and its
optional fields):

```text
chair, attempt_ordinal, provenance, format_capabilities
payload, witness_reported, content_health
presented, observed
scope = "page", page_ordinal
reason, page_edge_overshoots, raw_response_refs, adapter_metadata,
native_capture, native_inference, presentations, unit_captures, unit_call_refs,
serving_call_ref               only where the record has them
```

A page record names no act: a record carrying an act field (`act_key`,
`regions`, `page_role`, `unjoined_act_attempts`) is not the closed schema and is
refused. `serving_call_ref` and `unit_call_refs` are never both present. The
writer validates the exact shape, and the facts this stage computes
(`validate_page_record_facts`), before it publishes.

`payload` is the witness's JSON-native output, retained as its own shape. An
object, array, integer, boolean, null, or text response is not flattened into a
common body schema. `witness_reported` is the witness's separate self-report — a
confidence/status claim remains evidence, but it is not health and cannot make
the stage treat a channel as complete; a confidence outside the closed ordinal
set fails the attempt. `content_health` is stage-computed from native output and
a trusted response-boundary fact: recordability, UTF-8 validity, emptiness,
blankness, character count for text, and truncation. It never reads
`witness_reported`; when the real serving boundary cannot supply completion,
truncation is `null`, not guessed from punctuation.

`format_capabilities` says what this witness's output format can express at all,
and it is the fact a later reader needs beside `witness_reported` to avoid a
specific mistake: a witness whose format cannot say "unsure" must not be read as
confident merely for having said something. The `witness-capabilities` scenario
declares it on page 1: the Chandra chair cannot express uncertainty and claims
high confidence anyway. The claim is retained verbatim and reaches no outcome,
coverage count, or `content_health`.

The synthetic fixture declares complete responses, so its retained text gets
`truncated=false`. A malformed or unrecordable provider response becomes a
`failed` Testimonium with an explicit reason; it is not decoded with replacement
characters, stringified, or turned into an empty report. Malformed witness
metadata never rewrites facts about a recordable native payload: an unavailable
`format_capabilities` record is retained as `null`, and `content_health` continues
to describe the native response. The canonical artifact format can faithfully
retain only float-free JSON-native values: its shared canonical writer refuses
floating-point numbers, and this stage records that refusal as `failed` rather
than coercing the number.

### Native image/geometry waist

`presented` is either `{}` (this record has no image presentation) or the closed
description of exactly one `page`, `region`, or `adapter-crop` image: sealed page
identity and ordinal, blob path/digest, and an executable sealed-page transform.
A region presentation names one Designator detector region. A page
presentation must name the whole sealed page and `operation="whole"`. An
adapter-crop is either an exact `operation="crop"`, or the closed
`operation="crop-resize-preserve-aspect"` recipe with Pillow LANCZOS, floor
rounding, and source/target dimensions. Both read seams regenerate its PNG bytes
from the sealed page and refuse a digest that differs. A resize or any other
adapter-owned recipe must extend this closed transform rather than ride as an
opaque operation string. DAI records the exact crop recipe when its record crop
is already inside every ceiling; it does not claim a resampler ran on Pillow's
identity-copy path.

`observed` is the witness-order list of integer sealed-page boxes, each with a
dense zero-based ordinal, `bounds_source` in `native | derived | presented`, and
an optional non-overlapping span into this Testimonium's own retained text. Every
box is contained by the exact presented image's page-space bounds; a witness
cannot report pixels its presentation did not include.
`observed` carries no act identity, preference, authority, or confidence field.
A `presented` box is an explicit no-geometry fallback: it restates the image
sent and is excluded from coverage. Only
`native` and `derived` boxes report witness geometry.

A native box that runs past the sealed page edge is kept as reported, never
clamped, in `page_edge_overshoots`, present only on a record with a real
presentation.

The runnable adapter registry resolves exact configured names with no default.
`present(context, presentation)` may retain an adapter-owned crop through the run
tree; `observe(presentation, native_payload)` must derive geometry from the exact
presentation and retained response together. Churro's fixture response has no
layout, so its adapter returns only the excluded `bounds_source="presented"`
fallback. A future layout adapter cannot be wired to an observation callable
that never receives its own response.

**Observations reach a record from three sources, in this order.** The fixture's
`[[native_observation]]` table is consulted first: rows matching the chair and
page ordinal (and the scenario, where one is named) become `bounds_source="native"`
boxes directly, ahead of the adapter. Those are reported geometry, exactly as
an adapter's own layout would be, which is what makes the table a stimulus for
the geometry paths rather than decoration. Only
when no row matches does `observe` run, and only when there is no adapter at all
does the `bounds_source="presented"` echo of the presentation stand in.

### What an adapter must produce (Units 11, 12, 13)

Unit 10's contract is finished. An adapter-owning unit needs this page and no
consult report.

**Configuration.** Two rows on the occupant's own `[chairs.<role>]` table in
`config/models.toml`: `witness_adapter` (an exact declared name — no default, no
near match; a default adapter is a picker with one candidate) and `witness_scope
= "page"`. Both enter `ChairIdentity.to_record()` and therefore `config_digest`.
`witness_adapters.validate_runnable_adapter_bindings` refuses a configured witness
chair scoped any other way before the run opens: every witness reads whole pages.

**Registries move together.** The declared name joins
`common/witness_adapters.KNOWN_WITNESS_ADAPTER_NAMES`; the callable joins
`pipeline/3_attestatores/witness_adapters.RUNNABLE_ADAPTERS`. A configured chair
whose adapter has no runnable binding is fatal by name.

**Five roles, never merged.** `prompt` frames the request; `parse` turns one
native response into its text; `retain` records the exact view and the raw bytes;
`present` binds the image; `observe` derives geometry. Two of them are the intake
contract:

* `present(context, presentation)` returns the closed `presented` block —
  `kind` ∈ `page | region | adapter-crop`, sealed page identity and ordinal, blob
  path and digest, and an executable transform in **sealed-page pixel space**
  (the only space anything downstream can verify). `kind="region"` names a
  Designator detector region, the source of a DAI unit. An `adapter-crop` is an
  adapter-owned derivative: DAI publishes one from each detector record crop it
  is shown. Both read seams regenerate its bytes from the sealed page and refuse
  a differing digest.
* `observe(presentation, native_payload)` returns the closed `observed` list from
  that exact image and response together — dense, unique, zero-based ordinal;
  integer `x/y/w/h` in the pixel space of `presented.source_page_id`;
  `bounds_source` ∈ `native | derived | presented`; and a span into this
  Testimonium's own retained text, or null. No act identity, no confidence, no
  authority, and no preference-shaped key anywhere in the payload — `primary`,
  `canonical`, `best`, `preferred` and `superseded_by` are refused recursively.

**The quantization rule — where a float goes.** The native layer is open and
verbatim; the derived layer is integer. `retain` writes the raw response
content-addressed as `raw_response_ref`, and **that blob is the authority: it
never loses a float.** Real layout detectors emit float or normalized boxes, so
an adapter quantizes them to integer sealed-page pixels **inside `observe`**, and
the quantization rule is a property of the adapter, declared with it, with the
raw digest beside it in the same record. Two things it may not be: a coercion
nobody recorded, and a failed attempt. Note the wall this implies for `parse`:
`_native_problem` refuses any float in the retained native payload and turns the
attempt into a `failed` Testimonium with `content_health.recordable=false`, which
would report a working layout model as a broken witness. Return text or
integer-only structures from `parse`; put the geometry through `observe` and the
floats in the blob.
**Scope semantics.** Every occupant writes one page-scoped Testimonium per
(page, chair). No witness is asked about an
act, and this stage attaches no reading to one: the Perlector reads the page
records. A `presented` box is an explicit no-geometry fallback and is excluded
from both routing and coverage.

**What an adapter never does.** It never mints a region (crop lineage refuses a
stage that is not the Designator), never expresses a preference, and never
reports coverage.

**Evidence.** Published vendor specimens enter with their source and licence
recorded, exactly as `common/churro_document.py` cites stanford-oval/Churro at
the commit each carried string was taken from. A vendor
that publishes no response specimen is not represented by a synthetic fixture
wearing that status: the current DAI adapter records its published request
framing and generation values as named carries, while its fixture response
remains explicitly synthetic.

**Named obligations after Unit 10.** These are adapter/integration work, not
unfinished choices in this contract:

* **Unit 11 (Chandra)** — landed, and the closed JSON response contract it
  landed as is retired by U9 of the vendor systems design. What stands is the
  boundary: `parse` returns a page text, `observe` converts normalized boxes to
  sealed-page pixels with spans into that text, and the adapter-metadata rule
  rides beside `raw_response_ref`. What is gone is the shape:
  `chandra_response.py`, `verbatus-chandra-page-response.v1` and the
  `_LIVE_INSTRUCTION` that asked for them. The clause said the unit could carry
  no published vendor specimen because none exists; that was true of the model
  card and false of `datalab-to/chandra`, which ships both the prompt every
  caller sends and the parser for the answer it asks for under Apache-2.0. Both
  are carried and digest-pinned now (the layout grammar, its own section
  above), so the specimen is the vendor's and not this repository's question.
* **Unit 12 (Churro)** — landed, and its layout channel is retired by U10 of the
  vendor systems design. The fixture-only serve it replaced with the real
  full-page boundary stands: raw bytes, parse failure, truncation and
  post-capture repetition are all still visible, and
  `pipeline/3_attestatores/churro.py` owns the chair's five operations. What is
  gone is the channel: `common/churro_response.py` and the modified-carry prompt
  that asked for it. Both halves of the original clause, answered as they now
  stand:
  - **Native quantization: none to inherit and none declared.** Churro's
    `HistoricalDocument` grammar carries no coordinate vocabulary anywhere in
    the vendor's guide or its XSD, and Churro-DS carries none either, so there
    is nothing for a float-to-pixel rule to convert. The adapter's
    `quantization` is `None`, which is what keeps it from acquiring another
    adapter's rule by omission, and its `takes_page_size` is `False` for the
    same fact at the other seam.
  - **Published specimen evidence: the vendor's own grammar is the specimen.**
    The guide (`docs/guides/historical-document-xml.md`) and the XSD
    (`evaluation/historical_doc.xsd`) say what the answer looks like, and
    `common/churro_document.py` reads it. Three shapes parse — the grammar, the
    plain reading-order text the paper-era harness expected, and the retired
    `<output>` envelope kept as retained history with a finding that says so —
    and a well-formed body rooted anywhere else reaches the capture as
    `unrecognized-shape` naming which root arrived.

  Two things a later reader should not have to rediscover. There is **one
  parser name for one grammar**, `xml`, in both postures: Unit 12's second name
  existed only for the JSON contract, and `verify_native_capture_bytes`
  re-derives under the name the record carries against the system string the
  record itself retained. And the **grammar lives in `common/`, not beside the
  stage**, because three stages re-derive a Churro capture from its retained
  blob and neither reader may import an Attestatores module; a parser under the
  stage would leave both re-deriving through a branch they cannot reach.

For Units 11--13, the declared-name set, runnable mapping, parser/retention
dispatch and occupant configuration move together. A special quantization
error in `_native_problem` is deliberately **not** a Unit 10 mechanism: a float
that leaks through `parse` has violated its adapter contract, and the current
generic unsupported-native-type failure is accurate. The adapters instead keep
the float in the raw blob and make the declared conversion in `observe`.

### Perlector testimony input
The Perlector reads the retained `payload.payload` of every current page
record; structured nontext payloads remain retained and are represented as
incomparable rather than coerced into text.

## Outcomes and provenance

Every page witness chair has one explicit outcome per sealed page per attempt:

- `read` and `genuinely-empty` mean the chair was shown the page and its
  response was retained, with a serving receipt. `genuinely-empty` has native
  `payload=""`; it is never represented by an empty file. Both are **derived
  from a retained, recordable response to that exact request**; the only
  difference between them is whether the retained body has characters in it.
  The one `genuinely-empty` with no response is a record reader's page on which
  its own detector looked and found nothing (DAI, below): the detector's sealed
  census is the testimony, and the record binds it.
- `failed` means an attempt reached the response boundary but produced no usable
  Testimonium. It carries the presentation and a receipt.
- `not-run` means a configured chair was never attempted on the page. It
  retains the resolved pin but no invented receipt, and every content-health
  fact is `null`, because emptiness that nobody measured is unknown rather than
  absent.
- `excluded` is never produced by this writer. Generic envelope validation
  refuses a missing approval reference but checks only that the identifier is
  non-empty; the positive approved-exclusion path is not implemented.

An `AbsentChair` is not a page witness and gets no record: the Perlector and
the Recensor see it in the sealed roster and count it against the witness floor.

`ink-free-page` declares an empty response from each whole-page chair for page 3
and completes; `ink-free-page-unwitnessed` is the same page with those
declarations removed, so those chairs are `not-run` there and the page is held.

`provenance` holds the exact resolved identity/revision and, only for attempted
outcomes, the digest-checked serving receipt. A failed chair cannot be replaced
by another chair. Malformed native output or malformed capability metadata
becomes one `failed` attempt with the other records retained; neither case is
silently repaired into a reading, and neither holds the pass.

Stage 3 holds, and every hold stops orchestration, in two shapes: an `UNKNOWN`
attempt tally, and a whole pass refused by its own no-write preflight. The
preflight refuses more than one thing — bytes that differ from an attempt
already sealed at that ordinal, an ordinal past the next appendable one, a
fixture declaring two responses for one page and chair at one ordinal, a
declaration naming a page or chair no pass reads, a page whose witness layer is
closed — and every one of them writes nothing. Only the tally says the evidence
channel is damaged.

## Retention and current state

One write path, and it appends.

`--attempt-ordinal N` (default `1`) is the whole pass: every page witness chair
on every sealed page, at that one ordinal. For each `(page, chair)` pair the
writer permits only an exact byte-identical repeat of an ordinal that pair
already holds, or its next contiguous one — so the same command twice is a
resume rather than a second reading. `current + 2` is refused: a gap means an
attempt that existed is no longer here.

The RunTree's immutable publish boundary atomically creates each attempt
identity and refuses different bytes at an existing one. The stage has no
pointer and no artifact overwrite path. Consumers derive current per
`(page, chair)` through the shared `latest_attempt()` discipline, so a later
`failed` attempt is current and visible while the earlier reading remains
retained history. A missing or gapped history is refused rather than repaired or
selected around.

**The witness layer closes at the first reading.** Once the Perlector's
page-feed for a page shows witnesses, an appending whole pass over that page is
refused by name before anything is written: re-asking a witness because it
spoke again is a re-roll. A pass that only repeats attempts already sealed is a
resume and is untouched.

Fixture response declarations are ordinal-bound. A row without an
`attempt_ordinal` describes attempt 1 only, so a pass at a later ordinal never
silently reuses attempt 1's testimony; with nothing declared at its own ordinal
the attempt is `not-run`. Each row is keyed by `page_ordinal` and `chair`; a
row scoped to the run's scenario replaces the unscoped rows for that page and
chair, across every table.

### DAI: a page witness over its own detector's records

DAI was trained on crops of the records its own project's detector finds, so a
page-scoped `dai.v1` chair is shown its page that way. The adapter declares it (`RunnableAdapter.page_units =
"detector-records"`), and a roster that scopes such a chair `page` without a
configured `secondary_proposer` is refused before anything runs. The chair
runs on the fixture pass as on a live one: its detector's fixture row answers
from the fixture's `[[detector_record]]` rows (see the fixture posture below).
**Units.** A page's units are the Designator's `detector-region` crops of that
page, in the detector's own order, read from its `detector-page` census. A
missing or out-of-order record, a census whose count disagrees with its
records, or a crop that does not re-derive from its sealed page at its recorded
bounds refuses. A record the Designator could not cut is not a unit. A page
with no census refuses: the chair cannot be shown the page as
it was trained to read it.

**One call per record.** Each unit is one request, with the unit crop as the
presentation `dai.v1` resizes and sends; every response is retained as it
arrives. A unit refused for capacity before it is sent is a `failed` unit with
no capture. The page record is sealed only once every unit on the page has
been answered, so a pass interrupted inside a page asks that page's units
again, and a page record already sealed is resumed and never asked again.
**A page the detector found nothing on.** When the census counts no record and
the detector's retained run facts are complete, its cap (`max_det`) included, as
the page accounting's rule (i) reads them: DAI's own detector looked and the page
holds nothing for it. That is testimony. The page is sealed without a
request as `genuinely-empty`: native `payload` `""`, content health of empty
text (blank, not truncated), `presented={}`, no observed box, no receipt, and
one input, the page's `detector-page` census, with reason "DAI's own record
detector looked at this page and found no record below its cap, so the page
holds nothing for DAI". Every reader re-derives it
(`common.page_testimonia.is_detector_blank_testimony`,
`common.page_path.empty_detector_page`): such a record whose text is not empty,
whose health or reason is not the fixed one above, whose census names a record
or states no cap, or which binds anything but that census is refused. It counts
toward the witness floor like any reading, and the page accounting records it
`witness-read-blank` rather than holding it unread. On a page whose reading
establishes acts, the detector's silence contradicts the reading, and the page
accounting's rule (i) holds the page (`no-detector-record-on-act-page`). A page
with no record crop that is not such testimony is sealed `not-run` without a
request: a detector whose run facts state no cap (reason naming them
incomplete), or a census that counts records none of which enclosed a crop (a
reason naming that count). A sealed page with no census is refused.

**The fixture posture.** A fixture row reads each record exactly as a served
one is read -- the same presentation, the same closed model view and the same
retained capture under the `text` parser -- and only the answer is declared: one
`[[dai_record_response]]` row `{page_ordinal, detector_ordinal, chair, text}`
per record, optionally scoped to a scenario, retained under the stop word
`fixture-complete` with its receipt saying `fixture://`. A record with no
declared answer, or with two, refuses by name.

**The page record.** One `page-testimonium` per page, in the closed page shape
plus three fields. `presentations` lists every image the chair was shown, in
unit order, each an `adapter-crop` of the one sealed page, and `presented` is
its first. `unit_captures` holds one retained model view per presentation
(`null` for a unit that never reached the chair), each naming a response the
record retains in `raw_response_refs`. `unit_call_refs` holds each unit's
retained call record (`null` for a unit refused before it was sent), held to
the chair's sealed sampling row and its receipt's seed when the record is
written and again when a resumed pass reads it back. `observed` has one `presented` box per
unit: DAI reports no geometry, so each box is exactly the detector crop that
unit was shown, checked against that unit's own presentation, with a `span`
into the page text when that unit delivered a reading. Every
presented image, retained response and unit call record is digest-bound in
`inputs`. Both later
readers take every presentation from `presentations`
(`common/native_witness.py::record_presentations`).
**The page text.** Unit readings join with `"\n"` in the detector's own order.
An empty reading adds no separator.

**The page outcome.** One failed unit fails the page, with a reason naming each
failed record: the page then goes visibly under-witnessed by DAI rather than read
from part of what DAI was shown. Otherwise the page is `read`, or
`genuinely-empty` when every unit delivered an empty reading.
## Attempt tally

The stage's derived manifest is rebuilt from immutable page Testimonia, compared
to its stored inventory, and each record is checked against the page schema, the
facts this stage computes, the page roster, the shared page-record validator,
its native capture's bytes and every retained call record's sampling. `attempt_tally()`
returns `KNOWN` only when that inventory is whole. An absent, garbled, truncated
or divergent inventory returns `UNKNOWN`, `count=null`, `hold=true`, and the
check runs before anything is written, so a later pass over a damaged inventory
appends nothing. The stored inventory counts as evidence that attempts existed
even when the walk finds none left: a folder whose whole Testimonium layer is
gone but whose manifest still describes it holds, rather than taking the
first-run path and writing attempt 1 over a history that recorded more. The
closing tally can also hold *after* an attempt was appended — the append
happened and is retained; what the hold says is that the folder no longer
reconciles. An accounting imbalance (`FatalAccounting`) is never turned into a
hold: it is fatal, and no completion seal is published.

**This channel is the count of attempts, not a witness's own output.** A provider
response the stage could not retain is one witness's channel, and the `failed`
attempt naming it — with `content_health.recordable=false` and a reason — is a
counted, accounted record. It leaves the tally `KNOWN`, the page under-witnessed
and the run visibly partial, and it does not stop the Perlector reading the
page. Two `recordable=false` shapes are still `UNKNOWN`, because neither can be
resolved in the run's favour: a record claiming `read` or `genuinely-empty`
while saying nothing could retain what it read, and a `failed` record carrying
no reason.

**Whether every sealed page/chair pair is accounted for is a closing check, not
a precondition.** A pass killed part way through leaves attempts on disk and no
stored manifest, and the pass that would supply the missing pairs may not be
refused for their being missing. Once a pass has sealed the stage, its stored
manifest is required before a later pass, so a folder that lost it holds until
someone re-derives it — `RunTree.write_manifest("attestatores")`, one step,
losing nothing because the manifest is derived from the immutable attempts.
After that the pass resumes: the attempts already written are byte-identical
repeats and the missing ones are created. If the denominator still does not
reconcile once the pass has run, the folder holds.

One thing to know before reaching for that step: it loses nothing *while the
attempts it describes are still on disk*. Over a folder whose attempts are gone,
re-deriving the manifest discards the last record that they existed, and the
pass that follows restarts the history at ordinal 1. That is a decision someone
may legitimately take; it is not one to take without reading the manifest first.
