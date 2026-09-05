# Attestatores — handoff

The Attestatores retains one immutable `kind="testimonium"` for every configured
chair and every Designator act, on every attempted read. It does not merge, rank,
select, or turn a Testimonium into established text. A missing artifact is never a
witness outcome.

## Stage-completion seal

Before this producer's final manifest it publishes one `decode-environment` and
one `stage-seal`, or reuses both on a byte-identical retry. The seal witnesses
this pass's disk inventory and blob contents, and binds the exact decode-environment
bytes, run `config_digest` and `register_digest`, and `(kind, outcome)` census. An exit
held after publishing stage evidence seals it (holds remain in its census); a
pass that never reaches its seal does not seal, whether it was held or refused
before publishing stage evidence or closed fatally after publishing it, so the
successor correctly refuses the missing boundary. Every difference in decoders,
platform, machine, `decode_paths_used`, and `produced_pixels` is reported by
field or decoder name. A valid difference is report-only and never refuses;
Unit 17 owns any fatal policy.

Seals are compared as the SET the stored inventory names, on both sides of the
boundary: the producer refuses to re-seal, and the successor refuses to read,
when any named seal is no longer on disk. Ordinals are the contiguous run 1..N,
so removing the latest leaves a prefix that still looks whole — and the earlier
statement would then answer for a boundary it never witnessed.

## Exact input boundary

For a proposed act, the stage accepts only Designator regions whose provenance and
Exemplar crop lineage verify. Each attempted Testimonium names precisely the
original proposal regions and their pixel blobs. A later recovery crop is not
silently substituted for what a chair saw.

This stage now has two writers, and which one runs is decided by the sealed
serving-recipe row for each configured witness chair — never by a new
configuration key, and never by a fallback in either direction. Under the
committed fixture catalogue every chair is `kind = "fixture"` and the writer is
the declared synthetic skeleton described here: its `fixture://` serving facts
are fixture declarations, not measurements of a live model, and its bytes are
the pinned acceptance path. Under a catalogue whose rows for those chairs are
`kind = "vllm"`, the live boundary below runs instead: the serving response and
body contract Spec 04 was waiting for is `common/contracts/serving.py`'s
`chair-call-record.v1` plus `operations/serving/http.py::parse_openai_reading`,
and the capture-as-Testimonium intake is `common/native_witness.py`'s retained
model view, which the live pass records on every act attempt whose bytes reached
an adapter parser.

## Live response boundary

**Selection.** `main` resolves one mode per configured witness chair through
`operations.serving.client.serving_mode_for` — a three-name lookup (recipe,
chair, measured placement tier) in the catalogue the run sealed, re-read and
digest-checked against `config_digest` at the moment it is used. A roster that
mixes postures is refused by name: one run reads its witnesses one way, or every
consumer comparing witnesses across an act is comparing two kinds of evidence
without being told. An absent chair names no mode and stays `dead`.
`--placement-tier` is required to resolve a live row and is deliberately not
sealed: it is a measured fact of the card, and the receipt records the caps that
actually bound the serving moment.

**Pass structure.** The live pass is chair-outer and publishes per response.
Preflight stays a no-write preflight and consults no chair: its resolver returns
a pending sentinel for every unsealed pair, and the pass fills each one in as its
own response arrives. Serving runs through `feeding.stage_major_schedule` and
`feeding.execute_stage_major_schedule` under a `SingleChairResidency`, one
schedule per chair concatenated, so one chair is resident at a time, no unit is
served twice, and a schedule that returned to an unloaded chair is refused. A
chair's unit of work is its own sealed scope: an act-scoped chair is asked once
per act, a page-scoped chair once per *page*, and that page response derives both
the page Testimonium and the act-scoped compatibility records of every act whose
primary page it is. A continuation page's response feeds no act record; the act's
own view belongs to its primary page.

**Resume.** A pair already sealed at this ordinal is reused from its retained
Testimonium and never asked again — a live chair cannot reproduce immutable
bytes. A page response is likewise never re-requested while a sealed record
describes it: the page Testimonium if the interrupted pass reached it, otherwise
the act-scoped record of an act whose primary page it is, which carries the same
capture. A page nothing sealed depends on — a continuation page after an
interruption — is asked for again, because no sealed record contradicts a new
answer. A record whose receipt says `fixture://`, or an *attempted* act record
naming no serving call, is refused rather than resumed over: a live pass cannot
continue a fixture-posture run. A `dead`/`not-run` record is not that evidence
— it names no serving call because no chair was ever shown pixels for it, which
is an independent fact from a fixture-posture response, so it is skipped rather
than raised on.

**Closed asymmetry: cut-off composition is shared.** A parse failure landing on
a response the provider itself cut off at its bound now reads as an exhausted
bound, not plain bad ink, on both paths alike. `_failed_parse_composition`
holds the one composition — the `cut_note`-prefixed `reason` suffix and the
`transport_stop_reason`-bearing `_unrecordable_health` basis — and both
`captured_page_attempt` and `live_attempt_from_response` call it on their
parse-failure branch, so the two cannot drift apart again. An act-scoped chair
is evidence of the same kind as a page-scoped one; a truncation fact the
provider actually reported has no reason to survive on one path's summary and
vanish from the other's; Recensor coverage reading a provider-truncated act as
plain "failed" with no truncation flag was the silent loss GOVERNANCE 2 rules out.
Regression coverage lives beside the page-path original:
`test_live_attempt_from_response_cut_off_and_parser_failure_names_both` and
`test_live_attempt_from_response_parser_failure_without_cut_off_keeps_verbatim_reason`
in `test_live_witness.py`.

Recovering a page's response is not the same as finishing every act view it
owes: an interruption between two of a *single* page's own act publications
(the happy fixture's `a1`/`a2`, both primary on page 1) used to leave the later
one sealed nowhere and unreachable, because the resumed pass rebuilt the page's
capture from the earlier act alone and never revisited the rest. `run.py`
factors the per-act publish loop into `publish_page_act_views`, shared by
`_serve_page_unit` (a response this pass just received) and `live_attempt_pass`
(a response `resumed_page_captures` recovered), and calls it for every recovered
page capture before the schedule is built — publishing exactly the pairs still
pending on that page, from the very response the interrupted pass already
retained, never asking the chair again. When more than one sealed act record
could supply a page's resumed capture, every one of them is checked to agree
(`raw_response_ref`, `native_capture`, `outcome`) rather than taking whichever
sorts first; a disagreement is a named refusal, not a silent choice.

**What a live record gains.** `native_capture` (the adapter's retained model
view), `serving_call_ref` (the `chair-call-record.v1` blob for the one request)
and `raw_response_kind` (which sort of bytes `raw_response_ref` names) are
admitted on an act Testimonium and written only in live mode, so a fixture
record is byte-for-byte what it was. `provenance.receipt_ref` names the receipt
the chair's own client re-read at start, never a declared `fixture://`
stand-in. `content_health.truncated` comes from the engine's stop word:
`"stop"` → `false`, `"length"` → `true`, and an unreported word → `null` with
`truncation_basis = "not-recorded"`, on the act record and the page record
alike. Both retained blobs are re-read and digest-checked by the attempt tally
rather than carried as envelope inputs, because the tally re-derives an act
record's inputs from its regions and presentation alone.

**Chandra is a served witness like the others, under a closed response
contract.** Tyrel's ruling (2026-09-02): every witness runs its own full pass,
Chandra reads the page for the Designator and separately as Attestator 1, and
nothing is captured from one call into another. The capture-as-Testimonium
intake the structure-chair design had half built (`feeding.chandra_capture_intake`,
the `chandra-capture.v1` name) is removed rather than left as dead surface.
What the served chair parses is `chandra_response.py`, described in its own
section below: the closed JSON shape `chandra.prompt` asks for, exactly as the
Designator's structure pass asks its Chandra call for
`verbatus-structure-answer.v1`. A body in that shape is a reading -- page text,
and block geometry in sealed-page pixels with a span per block into that text.
A body in any other shape lands as `failed` with its bytes retained and its
shape named (`unverified-response-schema` and the rest of that module's closed
set); the retained model view rides on the record beside those bytes in the
`unrecognized-shape` state, naming the shape in `outcome`. Chandra's *native*
output mode is still unverified -- the vendor publishes no specimen -- and this
contract does not pretend otherwise: it is the repository's question, and the
first real response either answers it or arrives as a named surprise.

**Fixture declarations a live pass does not read.** A live pass reads the
fixture's pages, acts, continuations and proposals — that is the corpus — and
reads none of its declared witness responses (`testimony`, `witness_failure`,
`witness_empty`, `witness_not_run`, `churro_page_response`) or declared
`native_observation` geometry, which are the offline posture's stand-in for a
model. The pass names on stderr how many such rows it passed over, so an
operator cannot mistake one posture's record for the other's. Fixture-declared
Chandra anchors are not read either: aligning real witness text against
declared act spans would place a reading on geometry nobody measured. The live
anchor is instead derived from the Chandra chair's own served page response
(the derived anchor, below), and a page that chair did not read as text has no
anchor, which its page witnesses say by name (`missing-chandra-page-anchor`).
`chandra_anchor` rows are counted on that
same stderr line, separately from the chair-keyed families above: an anchor
keys on `page_ordinal`, not `chair`, so it cannot ride the `chair in
live_chairs` filter the others share, and every anchor the scenario declares is
one the live pass discards regardless of which chair would have used it.

### The Chandra response contract

`chandra_response.py` closes what a served Chandra page response parses into.
Exactly two forms are accepted, both under `schema =
"verbatus-chandra-page-response.v1"`: a `blocks` list, each block `{box_1000,
text}` with the rectangle in normalized integer coordinates 0..1000 (text per
layout block with geometry), or a single `text` string (page text with no
geometry, for a model that can transcribe but not place). Exactly one of the
two is present; `blocks` may be empty. Every other body -- an unknown or
missing schema, an extra key at either level, a duplicate member, a malformed
box or text, either form's absence or both forms together, the byte and block
ceilings -- is refused by a name from that module's closed `PARSE_OUTCOMES`,
whole, with nothing repaired (GOVERNANCE 7) and its bytes already retained.
`chandra.parse` dispatches on the declared schema: the wire contract to that
module, the committed fixture's `fixture-chandra-response.v1` placeholder to
the validation it always had, and anything else to `unverified-response-schema`.
**The placeholder is offline only.** One parser derives the retained model
view in both postures, and it used to have no way to tell them apart, so a
served chair answering in the fixture's stand-in shape was read as a page of
text -- a reading whose wire shape nothing in this repository had verified,
published as though it had been (GOVERNANCE 10). `retain_model_view` now takes
a `served` flag, both live call sites in `live_witness.py` set it, and it
reaches exactly one parser: under it, `chandra.parse` refuses the placeholder
as `unverified-response-schema` like any other undeclared shape. The bytes are
retained before the parser runs, so the refusal names a surprise rather than
losing one. The fixture posture passes nothing and keeps the acceptance its
pinned bytes depend on. Both halves are pinned --
`test_live_witness.py::test_captured_page_attempt_refuses_the_fixture_placeholder_schema_from_a_served_chair`
for the live refusal and the flag at both call sites, `test_chandra_adapter.py`
and `test_attestatores_retention.py` for the offline acceptance.

**The prompt is split by posture.** `chandra.prompt()` asks the served chair
for the contract shape, in the repository's own words, stating no preference
and no confidence budget (GOVERNANCE 10). The fixture posture records
`chandra.FIXTURE_PROMPT` in its retained model view instead
(`run.py::resolve_attempt`): that view is sealed into the fixture's pinned
bytes, the fixture never asks a chair anything, and rewording the live
instruction may not move a fixture byte. Both are this repository's wording;
neither is a vendor line.

**Geometry converts once, the Designator's way.** A block's `box_1000` is
quantized low-edges-floor / far-edges-ceil in normalized space and converted to
sealed-page pixels by `common.structure_answer.to_page_bounds`, the same
conversion the Designator's structure pass applies to its own Chandra call, so
the two Chandra readings of one page share one page-pixel mapping. That
conversion clamps to the page, so a normalized box can never overshoot the
sealed page. `chandra.observe` takes a keyword `page_size` for it: a page
witness's act view presents one crop while restating page-level geometry, so
the presentation's bounds are never the denominator, and a body that needs the
size without one is refused rather than placed in the wrong space. Each
observed box carries the block's span into the retained page text, which is
the block texts joined with a newline between delivered (non-empty) blocks and
nowhere else -- `common/structure_answer.py`'s own join rule. A body that
reports no block geometry (the page-text form, or an empty blocks list)
derives none; the page record then carries the presentation echo `run.py`
gives every page with no reported geometry -- the same fact the fixture's
genuinely-empty rows record, excluded from routing and coverage by its
`bounds_source` -- and the adapter never hands an echo to the shared
page-edge check, which admits reported geometry only. The adapter's one
declared `geometry_quantization` rule covers both accepted shapes, each in its
own coordinate space.

**A live page's partition is derived from the page response itself.** The
fixture walks one declared response per compatibility act; a live page has one
response that every act view on the page also carries, so
`publish_page_testimonia_and_attachments` derives the live partition from the
page capture's bytes once rather than once per act (which appended the same
blocks per act). A resumed page capture rehydrates those bytes from the sealed
record's capture, read back and digest-checked (`_page_capture_from_record`),
so the republished page record derives the same geometry the interrupted pass
did.

### The derived anchor (R4 on the live path)

The fixture route aligns page-witness text against the fixture's declared
`[[chandra_anchor]]` rows. The live route aligns against an anchor derived from
the Chandra chair's OWN served response for the page
(`run.py::derived_chandra_anchor`): the anchor text is that chair's retained
page text, and an act's anchor lines are the reported blocks whose geometry
overlaps one of the act's sealed proposal regions on this page -- the same
positive-area rule attachment uses, applied per block -- so alignment attaches
text to acts by geometry and never by choosing among witnesses (hard rule 8).
The act's `anchor_span` is the hull, in the markup-stripped normalized view
`align_to_anchor` measures in, of those blocks' spans translated through
`markup_text_view`'s offset map; `line_geometry` carries every overlapping
block in reading order. Everything after that is the machinery the fixture
route already had, unchanged: one `align_to_anchor` per `(page, chair)`, the
clip to the act's range, the translation back to raw offsets, the trivial
zero-length attach for a genuinely-empty witness, `refuse_ambiguous_act_alignments`
for two acts one chair cannot tell apart (which is also what a block
overlapping two acts produces, named rather than resolved).

Only acts whose primary page is this one are anchored; a continuation's tail
has no anchor line by design. An act no reported block overlaps, or whose
overlapping blocks carry no normalizable text, is `act-anchor-line-not-located`:
the page's anchor exists and locates no line for it. A page the Chandra chair
did not read as text -- an unrecognized body, or a genuinely-empty page, which
has no text to anchor to -- derives no anchor, and its page witnesses come back
`missing-chandra-page-anchor` (or, for a genuinely-empty witness, the trivial
attach's `no-page-anchor`, the blank-confirmation path the fixture already
exercises). `declared_chandra_anchor_chair` names the anchor chair on both
routes.

**What the live alignment does not do, and why the e2e export is still held.**
Alignment supplies a span inside a witness's own text; attachment is the page
geometry that chair reported against the sealed proposal, and a chair with no
reported geometry is not attached. Churro publishes no native layout, so on
the live path its page text aligns to the derived anchor and it stays
`attached: false`, `comparable: false`, with its `aligned` alignment retained
beside it and no span -- the record says both facts. The fixture attaches
Churro only through a declared `[[native_observation]]` row, which a live pass
does not read. Deriving Churro's geometry from Chandra's anchor lines would be
one chair's geometry attributed to another (the "never chair against chair"
rule of the adapter contract below), so it is not done here; a Churro layout
channel is Unit 12's obligation, and until it lands a live run counts two
witnesses of a floor of three and holds for review.

**A page witness's geometry on a continuation page is a record the Perlector
now reads.** `attached` is derived from geometry alone on every contributing
page, so a served Chandra whose page-2 block overlaps an act's continuation
region publishes that act's page-2 entry as `attached: true`,
`attachment_basis: geometric-overlap`, alignment
`continuation-page-no-act-anchor`, `comparable: false`, no span
(`test_attestatores_live_pass.py` pins it). `pipeline/4_perlector/run.py::act_attachment_view`
used to refuse that twice over: it required `attached` to equal the witness's
geometric overlap with the act's sealed regions on that page (so `false` was
refused as not derived from geometry) and separately refused any
continuation-page entry that was attached (so `true` was refused as claiming an
anchor). No record satisfied both, so a page witness reporting geometry over a
continuation region could not pass the Perlector in either state. The
contradiction was unreachable until a served Chandra parsed -- the fixture
declares no geometry on a continuation page -- and it was fixed on the
Perlector's side, as this section predicted, by dropping the second rule:
`attached` says only that the chair's ink overlaps the act's, and the
`continuation-page-no-act-anchor` alignment beside it already says no
comparison view exists. What the continuation page genuinely lacks is an
*anchor*, and that is what the surviving rule now names. The derivation rule is
untouched, so an entry that discounts its own geometry is still refused
(`pipeline/4_perlector/test_live_perlector.py`).
`pipeline/test_live_reading_seam_e2e.py` still scripts Chandra's
continuation-page answer in the contract's page-text form, which is a
legitimate answer and one the Perlector reads; the geometry form is exercised
against the reader directly.

**No live reread.** `--operation reread` is refused by name under a live roster.
A reread asks one chair for one act again at a new ordinal; it needs its own
residency, its own per-response publication, and its own answer to what an
act-scoped reread of a page witness means. Run the whole pass at the next
ordinal, or reread under the fixture catalogue.

### The cross-file seams that let a live pass carry every chair

Four gaps once stood between the live boundary and the committed roster, each
of them a named refusal rather than a silent default, and each in a file the
unit that found it did not own. All four are closed, and how they were closed
is part of the record because each turned on a choice about what a record may
say.

1. **A vendor's float decoding value is recorded as the exact decimal the wire
   carried.** `feeding.dai_generation()` carries floats — DAI's shipped
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
   an act crop needs no resize — which is every act crop in the reference
   fixture — the model must be shown exactly the source image, and
   `feeding.dai_model_view` now requires the two references to name the same
   SHA-256 rather than to be the same reference dict. They legitimately differ:
   the source is the Designator's proposal crop under `2_designator/`, and every
   image a witness is shown is inventoried under `3_attestatores/`. Both are
   `crop_png` of the same sealed page at the same bounds, and
   `verify_exemplar_crop_lineage` already proves the first of them is, so equal
   digests are equal pixels. Held to the whole dict, the rule refused a genuine
   DAI act *after* its response had already come back.
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

**Churro's declared 24,000-token bound is a declaration, and only sometimes the
request.** `common/native_witness.py::CHURRO_OUTPUT_TOKENS` is Churro's carried
HuggingFace-generate `max_new_tokens`, and the live seam used to rename it
straight onto the wire as vLLM's `max_tokens`. Every Churro row in
`config/serving_recipes_real.toml` caps `max_model_len` at 2,048, 4,096 and
8,192, and vLLM refuses a request whose prompt plus `max_tokens` exceeds the
row's context — so the very first call on a real pod was a refusal, on a card
billing by the hour, from the one chair in the pass that sent a bound at all.
`live_witness.churro_generation_sent` now asks the sealed row the chair is
actually running under (`ChairClient.handle.profile`): the declared bound goes
on the wire only where `max_model_len` is strictly larger than it, and
otherwise nothing is sent and the engine bounds generation by `max_model_len`
itself — the same decision the Perlector and the Designator already record, and
the only answer budget measured by the component that holds the tokenizer and
the image. This seam estimates no prompt cost: a reservation nobody measured
would be a number the record could not defend and could still be refused by the
row. The declaration is untouched — `generation_declared` carries 24,000 on
every request, the retained Churro model view still requires it, and a row that
states no positive `max_model_len` is refused by name before the request is
built. `test_live_witness.py` walks every Churro row in the shipped catalogue at
every tier and asserts what this seam would send is a bound that row can take;
its counterfactual holds the old flat 24,000 against the same three rows.

Two further seams closed with them:

**A live record says which kind of bytes it retained.** `raw_response_ref` means
the adapter's own output on every branch where a parser ran, and the whole
transport body on the one branch where none could; those are different evidence,
and until `raw_response_kind` was added to the act Testimonium the record said
neither and a reader had to infer it from which other optional field happened to
be present. The field is written only in live mode, its vocabulary is closed
(`common/contracts/serving.py`), a retained model view must agree it describes
model output, and the attempt tally still re-reads and digest-checks the blob it
names.

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

One smaller note still open for whoever comes next: a page Testimonium cannot
name its `serving_call_ref` (the shared page contract's optional fields do not
admit it), so a continuation-page response's call record is an inventoried blob
no record links.

**A live Chandra page record names its response once, through its capture.**
`run.py::_named_once` de-duplicates a page record's published `inputs` by
`relative_path` before the envelope writer runs, because
`common/contracts/envelope.validate_input_refs` refuses any repeated path. But
`pipeline/4_perlector/run.py::validate_page_testimonium_record` reconstructs
the same record's *expected* `inputs` by concatenating `raw_response_refs` and
the capture's `raw_response_ref` with no de-duplication, then compares the two
lists for exact equality — so a page record that listed the capture's own
response under `raw_response_refs` as well would be publishable here and
refused one stage downstream. Now that a live Chandra page parses and its
partition is derived from the very bytes the capture names, this stage keeps
the two fields disjoint on the live path by construction: the partition list
stays empty, the capture names the bytes, the geometry is derived from them,
and the act views carry the quantization rule beside their own retained
reference (`adapter_metadata` is absent from the live page record because the
shared contract ties it to `raw_response_refs`). The one exception is a
page-edge overshoot finding, which the shared contract requires to be traceable
through `raw_response_refs`; there the reference is added and the Perlector's
arithmetic would refuse the record. That branch is unreachable for the wire
contract -- its page-pixel conversion clamps to the page -- and reachable only
for a live body wearing the fixture placeholder's pixel boxes. The
Perlector-side fix has since landed -- one entry per `relative_path` before the
sorted comparison -- so that branch is no longer a record this stage may
publish and the next one must refuse.

**Proved end to end.** `pipeline/test_live_reading_seam_e2e.py` runs this
stage's live pass as one link in a whole run: the real stage programs to the
Designator, this stage's three live witness chairs, a live Perlector, and then
the Recensor, Archetypus and Armarium over what both wrote — the first time any
stage after the Perlector has read a live tree. They read it: the run seals a
terminal export. It is **held for review, not delivered**, and the reason is
one named limit of this stage's rather than anything downstream. Each act
counts two witnesses of a floor of three: Chandra reads under its contract, is
attached by its own block geometry and aligned against the anchor derived from
its own response; DAI reads its crops; Churro's page text aligns to that same
anchor but Churro publishes no native layout, so on the live path its only
geometry is the presented echo, which routing excludes, and it stays
geometrically unattached with its alignment retained beside it. The fixture
posture attaches Churro through a declared `[[native_observation]]` row, and a
live pass reads none. That is the honest current measurement of a live roster,
and a Churro layout channel (Unit 12's obligation) is what will move it.

## Real ingress

`main` opens through `common.stage.open_stage_context`, which reads the run
authority once and decides the route from its ingress record. On a real
submission the context carries the registry, the sealed digest map this stage
requires (`alignment`, `decoding`, and the real-only names the Door seals),
the parsed serving inputs, `fixture=None` behind an accessor that refuses by
stage name, and `REAL_SCENARIO` -- never `--scenario`, which the real route
does not read. `run.py::real_ingress(context)` is this stage's one reading of
the route, off `context.run`; nothing here branches on `context.scenario` or on
the shape of a fixture.

**The only real posture is every witness served.** Tyrel's ruling
(2026-09-02): every witness runs its own full pass, with no capture and no
slicing, and a roster where every configured witness row is served is the only
real posture. `require_every_witness_served` refuses, by chair name and before
any act is read, a real run whose sealed catalogue gives a configured witness a
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
`common.stage.exemplar_page_ids` over the Exemplar's own `page` artifacts. On a
fixture run the index agrees with the old fixture-declared identity for every
sealed page, because a sealed page's identity is its admitted bytes' digest and
"sealed" means those bytes matched the declaration; a refused page is indexed
under the Door's `source-N` admission subject, and this stage only asks a
page-scoped chair about pages that carry proposed acts -- on every shipped
fixture family those are sealed pages, so no fixture byte moves -- but `by_page`
is built from a real run's own seal rows, not from this invariant, so
`presentation_for_page` checks the Exemplar page's `outcome` itself and refuses
by name rather than reading a refused page's absent `image_path`. The index is
built once per pass (`publish_page_testimonia_and_attachments` and
`live_attempt_pass` each call `exemplar_page_ids` once and thread the result
into every `page_subject` / `presentation_for_page` call as `page_ids`) rather
than walked again at every lookup: the Exemplar layer is sealed before this
stage opens, so the walk answers the same every time for the life of the
process, and a cache scoped to one pass cannot outlive the tree it described.
Review found this stage paying roughly seven such walks per page rather than
the three this note used to claim; a page-level bound on a large corpus is
still roadmap work.

**The declared witness tables have no real-mode counterpart.** `witness_failure`,
`witness_not_run`, `witness_malformed`, `witness_empty`, `native_observation`,
`chandra_anchor` and `churro_page_response` are the offline posture's stand-ins
for a model; the response boundary on a real run is the live pass above (Section
A), and what a chair returned is what the transport actually returned. On a real
run the pass's declaration set is `real_declarations` -- empty in every family,
in the exact shape `declarations_for` builds, and a test holds the two shapes
equal -- and the preflight's validation of declared Churro page responses is
skipped by `main`'s `fixture_declared=False`, because there is no fixture and
nothing to validate. The three-source order for native geometry (declared
observation, adapter observation, presented bounds) loses its first source on a
real run: geometry is the adapter's `observe` over the real payload or the
presentation, never a declaration. `refuse_unread_fixture_declarations` prints
nothing on a real run: there is no row to pass over. The readers that stay
fixture-only (`testimony_for`, `declared_response`, `churro_page_capture`,
`captured_churro_page_attempt`, `_fixture_native_observations`, the declared
anchor read) are not reached on a real run, because the served posture makes
every real pass a live pass; if one ever is, the accessor refuses by name rather
than handing back an empty table. `validate_declared_churro_page_responses`
keeps reading the fixture's own page ordinals for the fixture route, since it
runs only there.

**The continuation refusal.** `page_denominator` names an act's far page from
its verified regions; with no verified region and a recorded crop refusal it
used to fall back to the fixture's `[[continuation]]` declaration. On a real
run there is none, and the refusal says so and says what clears it: *act X's
proposal seal claims a continuation and real ingress carries no continuation
declaration; its far-page evidence cannot be addressed. The Designator must
publish the continuation region that names the far page.* This branch is
reached only past `expected_acts`, which on a real run already verified that
the far-page region exists and recomputes the act against it -- so what it
means is that the region existed and was refused at the crop boundary, and the
one thing that addresses the far page is the Designator's own region record for
it.

**What is proven offline.** `test_attestatores_real_ingress.py` carries a real submission
of the synthetic fixture's own two pages through the Door, the Exemplar and the
Ink Map as programs, hand-builds the Designator's regions and seal in the shape
`cut_minted_region` publishes them (because no real Designator exists), and
runs this stage's `main` with three served fake chairs: every act and page
record publishes, the page records name the Exemplar's own page subjects, no
declaration is read or reported, and the fixture accessor is never touched. The
same file holds the seal refusal over an unsealed Designator (context opened,
nothing written, no "sealed no digest"), the fixture-catalogue refusal, and the
shipped real catalogue's posture at every tier. A real Designator is still
roadmap work; what this stage is ready for is its seal.

## Testimonium schema

Every record payload has these fields:

```text
chair, act_key, attempt_ordinal
regions = [{region_id, image_path, image_sha256}, ...]
provenance, format_capabilities
payload, witness_reported, content_health
presented, observed, unpresented_regions
reason                         only when a named non-reading/failure needs one
```

`payload` is the witness's JSON-native output, retained as its own shape. An
object, array, integer, boolean, null, or text response is not flattened into a
common body schema. `witness_reported` is the witness's separate self-report — a
confidence/status claim remains evidence, but it is not health and cannot make
the stage treat a channel as complete. `content_health` is stage-computed from
native output and a trusted response-boundary fact: recordability, UTF-8 validity,
emptiness, blankness, character count for text, and truncation. It never reads
`witness_reported`; when the real serving boundary cannot supply completion,
truncation is `null`, not guessed from punctuation.

`format_capabilities` says what this witness's output format can express at all,
and it is the fact a later reader needs beside `witness_reported` to avoid a
specific mistake: a witness whose format cannot say "unsure" must not be read as
confident merely for having said something. The `witness-capabilities` scenario
declares both sides on act a1 — chair 1 cannot express uncertainty and claims
high confidence anyway, chair 2 can and reports doubt — so the distinction is
exercised rather than merely representable without blinding one chair in the
reference happy run's dissent record. Both claims are retained verbatim and
neither reaches an outcome, a coverage count, or `content_health`.

The synthetic fixture declares complete responses, so its retained text gets
`truncated=false`. A malformed or unrecordable provider response becomes a
`failed` Testimonium with an explicit reason; it is not decoded with replacement
characters, stringified, or turned into an empty report. Malformed witness
metadata never rewrites facts about a recordable native payload: an unavailable
`format_capabilities` record is retained as `null`, and `content_health` continues
to describe the native response. Arbitrary
binary response retention remains an explicit Spec 04 response-contract decision.
The current canonical artifact format can faithfully retain only float-free
JSON-native values: its shared canonical writer refuses floating-point numbers,
and this stage records that refusal as `failed` rather than coercing the number.
That is an unresolved gap against Spec 07's unexpected-but-parseable payload
requirement, not a claim that the float was retained verbatim.

### Native image/geometry waist

`presented` is either `{}` (this record has no image presentation) or the closed
description of exactly one `page`, `region`, or `adapter-crop` image: sealed page
identity and ordinal, blob path/digest, and an executable sealed-page transform.
A region presentation is re-derived from its unique Designator proposal. A page
presentation must name the whole sealed page and `operation="whole"`. An
adapter-crop is either an exact `operation="crop"`, or the closed
`operation="crop-resize-preserve-aspect"` recipe with Pillow LANCZOS, floor
rounding, and source/target dimensions. Both read seams regenerate its PNG bytes
from the sealed page and refuse a digest that differs. A resize or any other
adapter-owned recipe must extend this closed transform rather than ride as an
opaque operation string. DAI records the exact crop recipe when its proposal is
already inside every ceiling; it does not claim a resampler ran on Pillow's
identity-copy path.

`observed` is the witness-order list of integer sealed-page boxes, each with a
dense zero-based ordinal, `bounds_source` in `native | derived | presented`, and
an optional non-overlapping span into this Testimonium's own retained text. Every
box is contained by the exact presented image's page-space bounds; a witness
cannot report pixels its presentation did not include.

**One record kind is exempt, and only one.** A page chair's act view
(`page_witness: true`, never `scope: "page"`) presents a single crop while
restating the page-level geometry that chair actually reported, so its boxes may
legitimately exceed that one crop. They remain bounded by the sealed page, which
is the wall that does not move, and Unit 10C's coverage derivation is what
consumes that page-space geometry. The flag cannot buy the exemption on its own:
a `scope: "page"` record presents the chair's complete view and keeps the
containment rule, so the relaxation cannot be forged onto a record whose
presentation really was everything the witness saw. Every other record — act
chairs, and page chairs at page scope — is contained as above. `observed`
carries no act identity, preference, authority, or confidence field. `presented`
is an explicit no-geometry fallback: it restates the image sent and is excluded
from both unrouted-ink detection and Unit 10C coverage. Only `native` and
`derived` boxes report witness geometry.

`unpresented_regions` is computed after the adapter's final presentation and
re-derived at both later act read seams by page-space containment. With a real
presentation, `[]` means every bound proposal crop lies inside that one image;
with `presented={}`, the list is inapplicable and must be `[]`. Those states are
not ambiguous because `presented` distinguishes them, and a non-attempted record
is independently forbidden from binding any regions or image inputs.

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
boxes directly, ahead of the adapter. Those are reported geometry — they drive
attachment and routing exactly as an adapter's own layout would, which is what
makes the table a stimulus for the geometry paths rather than decoration. Only
when no row matches does `observe` run, and only when there is no adapter at all
does the `bounds_source="presented"` echo of the presentation stand in.

### What an adapter must produce (Units 11, 12, 13)

Unit 10's contract is finished. An adapter-owning unit needs this page and no
consult report.

**Configuration.** Two rows on the occupant's own `[chairs.<role>]` table in
`config/models.toml`: `witness_adapter` (an exact declared name — no default, no
near match; a default adapter is a picker with one candidate) and `witness_scope`
∈ `page | act`. Both enter `ChairIdentity.to_record()` and therefore
`config_digest`. `witness_scope` is invocation granularity only: it says nothing
about image kind, geometry, region identity, or coverage.

**Registries move together.** The declared name joins
`common/witness_adapters.KNOWN_WITNESS_ADAPTER_NAMES`; the callable joins
`pipeline/3_attestatores/witness_adapters.RUNNABLE_ADAPTERS`. A configured chair
whose adapter has no runnable binding is fatal by name; a declared name with no
configured occupant is reported on stderr and is not fatal, because an adapter
may land before its chair does.

**Five roles, never merged.** `prompt` frames the request; `parse` turns one
native response into its text; `retain` records the exact view and the raw bytes;
`present` binds the image; `observe` derives geometry. Two of them are the intake
contract:

* `present(context, presentation)` returns the closed `presented` block —
  `kind` ∈ `page | region | adapter-crop`, sealed page identity and ordinal, blob
  path and digest, and an executable transform in **sealed-page pixel space**
  (the only space anything downstream can verify: `verify_exemplar_crop_lineage`
  re-derives there and the Recensor reconciles there). `kind="region"` may name a
  Designator region whose `origin` is `proposal` and nothing else — a recovery
  crop may never be presented as a witness basis. An `adapter-crop` is an
  adapter-owned derivative, not a third scope: the current DAI occupant is
  act-scoped and publishes one from the proposal it was assigned. Both read
  seams regenerate its bytes from the sealed page and refuse a differing
  digest.
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

**Scope semantics.** A `page` occupant writes one page-scoped Testimonium per
(page, chair) carrying `partition_disagreement`, and reaches an act only by
**geometric overlap of its own reported `native`/`derived` geometry against the
sealed proposal** — never through an anchor, never chair against chair. A
`presented` box is an explicit no-geometry fallback and is excluded from both
routing and coverage. An `act` occupant writes one Testimonium per (act, chair)
with attachment basis `presented-region`. A page witness cannot be re-asked: a
targeted reread reaches act-scoped chairs only.

**What an adapter never does.** It never mints a region (crop lineage refuses a
stage that is not the Designator), never expresses a preference, and never
reports coverage. Ink it observed that no sealed proposal accounts for becomes a
named non-fatal `unrouted-observation` finding, retained in
`partition_disagreement.unclaimed_observations`; the Recensor alone may spend a
bounded fallback-recrop on it, against one absolute cap of three shared with
every other recovery origin.

**Evidence.** Published vendor specimens enter with their source and licence
recorded, exactly as `feeding.churro_prompt` cites stanford-oval/churro. A vendor
that publishes no response specimen is not represented by a synthetic fixture
wearing that status: the current DAI adapter records its published request
framing and generation values as named carries, while its fixture response
remains explicitly synthetic.

**Named obligations after Unit 10.** These are adapter/integration work, not
unfinished choices in this contract:

* **Unit 11 (Chandra)** — landed as the closed response contract
  (`chandra_response.py`, its own section above): the served chair is asked
  for a declared JSON shape, `parse` returns its page text, `observe` converts
  its normalized boxes to sealed-page pixels with spans into that text, and
  the adapter-metadata rule rides beside `raw_response_ref`. What the unit
  could not carry is a published vendor specimen, because none exists; the
  contract is this repository's question, and the first pod reading's retained
  bytes are the specimen.
* **Unit 12 (Churro)** replaces the fixture-only Churro serve with the real
  full-page XML boundary while keeping raw bytes, parse failure, truncation and
  post-capture repetition visible. It declares whether it has any native
  quantization to apply (rather than inheriting another adapter's rule) and
  carries its own published specimen evidence.
* **Unit 13 (DAI)** adds DAI's exact registry rows and extends the closed
  transform for its adapter-owned crop/resize so the shown pixels remain
  reproducible. The landed adapter begins from its assigned Designator proposal;
  it does not execute DAI's own detector, so it does not satisfy the staged
  pipeline's separate native-detector requirement. DAI publishes no native
  layout channel here: its honest
  `bounds_source="presented"` fallback is excluded from routing and coverage,
  while the separate secondary proposer remains Unit 9's chair and is not this
  adapter's native channel. Its carried prompts and nine generation values are
  named with source, digests, and the settled licence position; uncertainty
  tokens are retained unchanged.
* **Unit 14 (native-testimony integration)** removes the temporary
  `payload.reported` bridge below and teaches the Perlector/Recensor consumers to
  use each adapter's native retained text and partition facts without choosing a
  witness boundary. It also owns the explicit hold for an unproposed cross-page
  half act and the ink-map/proposal coverage reconciliation already assigned to
  that unit.

For Units 11--13, the declared-name set, runnable mapping, parser/retention
dispatch and occupant configuration move together. A special quantization
error in `_native_problem` is deliberately **not** a Unit 10 mechanism: a float
that leaks through `parse` has violated its adapter contract, and the current
generic unsupported-native-type failure is accurate. The adapters instead keep
the float in the raw blob and make the declared conversion in `observe`.

### Temporary textual bridge

The prohibited-to-edit Perlector still consumes `payload.reported` as a string.
Until its owner migrates that reader, a recordable *textual* native payload also
carries `reported` as a deprecated compatibility projection. It is never derived
from `witness_reported`, never used by Attestatores health, and no structured
native payload is coerced into it. A structured Testimonium therefore lands
verbatim here but the current Perlector visibly refuses it; that integration work
belongs to the Perlector/serving-contract owners.

## Outcomes and provenance

Every configured chair has one explicit outcome per act per attempt:

- `read` and `genuinely-empty` mean a chair actually read the exact regions and
  carry a serving receipt. `genuinely-empty` has native `payload=""`; it is never
  represented by an empty file. Both are **derived from a retained, recordable
  response to that exact request** — same boundary, same retention, and the only
  difference between them is whether the retained body has characters in it. No
  act, page, or identity reaches either outcome by being a particular kind of
  thing.
- `failed` means an attempt reached the response boundary but produced no usable
  Testimonium. It also carries the attempted region inputs and a receipt.
- `dead` means an `AbsentChair`: the chair was unavailable and no attempt reached
  the region. It retains the absence record, with no invented receipt.
- `not-run` means a configured chair was never attempted, including a held or
  refused proposal. It retains the resolved pin but no invented receipt.
- `excluded` is never produced by this writer. Generic envelope validation
  refuses a missing reference but checks only that the identifier is non-empty;
  Stage 3 does not yet resolve that identifier to verified Tyrel approval-record
  bytes. The positive approved-exclusion path is therefore not implemented.

A Designator page-fallback act is witnessed exactly like any other proposed act.
This used to be the one exception: the stage recognized the minted identity
(`_is_page_fallback`) and wrote `genuinely-empty` for every configured chair
before consulting any response boundary, then gave each record the proposal
regions, marked it attempted, minted a serving receipt and recorded
trusted-boundary health — three chairs on disk as having independently read a
page none of them was asked about, which the Recensor could then seal
`confirmed-blank` on (Sol-S1). The branch and its identity check are both gone;
nothing in this stage asks what kind of act it is reading.

So the fallback crop goes through the same response boundary as any other
proposed region, and a missing response is `not-run` (whole pass) or `failed`
(targeted reread) and holds the act. It is never an empty report: a `not-run`
record leaves every content-health fact `null`, because emptiness that nobody
measured is unknown rather than absent. `ink-free-page` declares one empty
witness response per chair for `page-fallback:3` and completes as a
`confirmed-blank`; `ink-free-page-unwitnessed` is the same page with those three
declarations removed and holds instead. Both are pinned end to end
(`test_an_ink_free_page_fallback_is_witnessed_and_read_end_to_end`,
`test_an_undeclared_fallback_witness_holds_the_act_instead_of_reporting_it_blank`),
and the resolution itself in
`pipeline/3_attestatores/test_page_fallback_witnessing.py`.

**A real implementation may make fallback pages cheap however it likes** — a
cheaper model, a coarser crop, a page-scoped call — but whatever it does has to
produce a response this stage retains, or the act holds. Skipping the provider
call entirely is the one thing it may not do, because the outcome it would skip
to is a positive claim about what a witness reported.

`provenance` holds the exact resolved identity/revision and, only for attempted
outcomes, the digest-checked serving receipt. A failed or absent chair cannot be
replaced by another chair.

A malformed proposal crop is isolated to its act: every chair receives its
explicit `not-run` or `dead` record, no chair is said to have read the refused
pixels, and other acts continue. Malformed native output or malformed capability
metadata similarly becomes one `failed` attempt with the remaining chair records
retained; neither case is silently repaired into a reading.

A refused crop completes this retention stage because each configured chair has
been accounted for, and the explicit non-reading records are what make the
shortfall visible downstream. It is worth being exact about how far that goes
today: the Perlector verifies the same crop lineage itself, so a crop this stage
refused for a broken lineage is refused there as a named fatal rather than
carried into a partial export. Retention completing is the guarantee here; a
partial export past a refused crop is not one this tree currently reaches.

Stage 3 holds, and every hold stops orchestration, in two shapes: an `UNKNOWN`
attempt tally, and a whole pass refused by its own no-write preflight. The
preflight refuses more than one thing — bytes that differ from an attempt
already sealed at that ordinal, an ordinal past the next appendable one, a
fixture declaring conflicting outcomes for one pair at one ordinal — and every
one of them writes nothing. Only the tally says the evidence channel is damaged.

## Retention and current state

Two write paths, and both append.

`--attempt-ordinal N` (default `1`) is the whole pass: every configured chair on
every expected act, at that one ordinal. For each `(act, chair)` pair the writer
permits only an exact byte-identical repeat of an ordinal that pair already holds,
or its next contiguous one — so the same command twice is a resume rather than a
second reading, and the whole pass still resumes over a folder in which one chair
has been reread past it. `current + 2` is refused: a gap means an attempt that
existed is no longer here.

`--operation reread --act <act_id> --chair <role>` moves exactly one chair on one
act, at the ordinal that chair's own history says comes next. This is the path a
real reread uses: a reread happens because one witness failed on one act, and
re-witnessing the other chairs to reach it would re-read ink nobody doubted and
spend a provider call per chair per act to do it. Every other chair's current
record stays the attempt it already was. It is refused, writing nothing, for an
act the proposal seal does not name, a chair the run is not sealed with, a
Designator-held act (no witness was shown a reading there), an absent chair
(a dead chair asked again is not a second attempt), a chair with no first attempt
to follow, a **page witness** (below), and an act whose **witness layer is
closed** (below). The orchestrator never invokes it, and that is a decision
rather than a gap: GOVERNANCE 11 gives recovery to *coverage* — a missed region,
a cut crop, a continuation — while a witness reread recovers *priming*, so
driving it from the recovery loop would make witness quality a loop variable.
`RECOVERY_KINDS` is unchanged. This is an operator repair with a documented
window.

A targeted reread re-derives that act's act-attachment as part of its own write,
through the `act_scoped_attachment_entry` the whole pass uses for the same
derivation. The attachment is a derived view of the per-`(act, chair)` attempt
stream and the reread appends to exactly that stream, so a reread that left it
alone wrote a Testimonium no later stage could consume: the very next Perlector
invocation refused the stale record, in the reread's own intended order. Only the
reread chair's entry is re-derived; the others are carried forward, and checked
against their chairs' current attempts on the way so a stale entry is refused
rather than laundered into a newer record.

## The one attempt model

**The reading attempt ordinal is a function of the act's crop history alone** —
one reading of the proposal, plus one for each recovery crop cut since
(`pipeline/4_perlector/run.py::_next_attempt`, and the identity the Recensor,
Archetypus and Armarium each enforce). Witness testimony never moves it.

That is a decision, not an omission. A Testimonium is a clue that primes a
reading, never the ink the reading is established from (ARCHITECTURE; GOVERNANCE
3), so a second look by a witness does not make a second reading exist — and
re-reading an act because a witness spoke again is the re-roll GOVERNANCE 11
refuses. The alternatives were weighed and rejected: advancing the ordinal on any
new current evidence makes witness quality a loop variable at the four stages that
decide whether text may be established, and deleting the reread outright leaves
the whole pass as the only retry, which costs every chair on every act its
currency to move one.

Two consequences follow, and both are enforced at entry rather than discovered
downstream.

**The reread has a window.** It is open until the Perlector establishes a reading
that cites this act's testimony, and closed afterwards. A new witness attempt on a
closed act — targeted reread *or* appending whole pass — is refused by name. The
deep reason is not the ordinal mechanics (a pending recovery reread means a new
reading can be pending even on a closed act): it is that a witness is only ever
shown the act's *original proposal crop* (`proposed_regions`; the Perlector
refuses testimony naming a recovery crop), so a second look can only ever add
priming, never coverage — and re-reading because a witness spoke again is
GOVERNANCE 11's re-roll. Mechanically, the Perlector would also recompute the
same ordinal, build a different payload, and meet its own immutable record. A held act's or an
absent chair's `not-run` reading cites no testimony and closes nothing. A pass
that only repeats attempts already sealed is a resume and is untouched.

**A targeted reread takes its act off the shared whole-pass ordinal.** The whole
pass is a run-level instrument at one ordinal and re-derives each act's attachment
there; after a reread that ordinal is already taken by a record describing a
different state. An appending whole pass on an act whose chairs no longer share
one current ordinal is therefore refused before anything is written. A partly-lost
attempt layer is not that case — its surviving pairs still share an ordinal — so
the repair pass still works.

**A page witness cannot be act-reread.** It reports one reading per page; its
act-level view is derived from the page join and that join's alignment against the
page anchor. An act-targeted reread would re-derive one act's view from an attempt
the page record does not describe, leaving the two disagreeing about the same
chair. No operation exists today to re-ask a page witness about anything —
building one would be new, page-scoped Attestatores work — and the refusal says
so rather than half-performing the act-scoped one. (The recovery vocabulary's
`page-level-reread` is a *Perlector* operation; that name is not borrowed here.)

One residual is left to the RunTree rather than checked at entry, deliberately:
reread *every* chair on one act up to the same ordinal and the act agrees again,
so an appending whole pass at that ordinal passes the shared-ordinal check and
meets the attachment collision at publication. Reaching it also needs each chair's
whole-pass attempt to be byte-identical to its reread attempt — otherwise
`_refuse_write_collision` stops the pass first — so the pass that survives is one
that had nothing to add. The outcome is a loud fatal refusal with
`RunTree.write_manifest` as the recorded one-step recovery, and closing it would
cost a second derivation of every attachment in preflight.

The end-to-end assertions for all of this are
`pipeline/orchestrator/test_attempt_model.py`.

Neither path accepts the other's arguments: `--attempt-ordinal` beside a reread,
or `--act`/`--chair` beside a whole pass, is refused rather than ignored. An
operation this stage does not implement is refused for the same reason — a
mistyped `reread` would otherwise run a whole pass and exit 0 over a witness it
never asked again.

Fixture response declarations are ordinal-bound. An older row without an
`attempt_ordinal` describes attempt 1 only; a successful reread therefore carries
the newly declared native response for its own ordinal rather than silently
reusing attempt 1's testimony. A reread for which no response is declared at its
own ordinal is `failed`, not `not-run`: the invocation named one chair on one
act, so it is an attempt that produced no usable Testimonium.

Each attempt identity binds the act, the operation `read:<chair>`, and the
ordinal — `attempt_id(act_id, f"read:{chair}", ordinal)`. The RunTree's
immutable publish boundary atomically creates it and refuses different bytes at
an existing identity. The stage has no pointer and no artifact overwrite path.

Act-scoped Testimonium consumers derive current per chair through
`common.stage.latest_per_chair()`. The Recensor also reads `page-testimonium`
records directly for content coverage, deriving current per `(page, chair)`
through the shared `latest_attempt()` discipline. Thus a later `failed` attempt
is current and visible, while the earlier successful attempt remains retained
history. A missing or gapped history is refused rather than repaired or selected
around.

### Closed page-continuation record

`kind="page-testimonium"` is a closed producer record. It carries the ordinary
Testimonium fields plus exactly `scope="page"`, `page_ordinal`, `page_role`, and
`unjoined_act_attempts`; `reason` and the textual `reported` projection remain
the only conditional fields. The writer validates that exact shape before it
publishes, refusing unknown fields at this producing boundary.

`page_role` is one of `primary`, `continuation`, or `mixed`. It describes the
relationship of the page's contributing proposal acts to their scalar primary
page: a page reached only by continuations is `continuation`; a page containing
only primary regions is `primary`; and a page containing both is `mixed`.
Every proposed act joins every page represented by its proposal regions. Thus a
continuation publishes one page Testimonium per contributing page and per page
chair, and its act attachment carries one explicitly page-ordinalled reference
to each. The primary scalar remains act identity, never a reduction of the
evidence denominator.

An act whose crop was refused has no proposal regions to read pages off, and its
pages come from the sealed proposal facts instead — its own `page_ordinal` plus
the fixture's declared continuation page. A refused crop was never shown to a
witness, but the page-level non-reading Testimonium is still published for every
page it covered: turning an isolated crop failure into a page that vanishes from
the denominator is the silent loss GOALS 1 is about.

`page_role` is written by a producer that holds one page's whole act list, and
read back by two stages that hold different amounts of it. The Perlector holds
one act, so it refuses only the two labels that act's own primary-page fact
contradicts. `mixed` contradicts no single act, so the **Recensor** re-derives
the role from every act attached to the page and refuses a claim the whole page
disproves (`pipeline/5_recensor/run.py::reconcile_page_roles`). Its denominator
is this stage's own published attachments, not a second walk of the Designator's
regions, so the two groupings cannot drift apart.

### The page record's own outcome

R0 has no live page-scoped witness. A page Testimonium is `page_join`'s
concatenation of one chair's own act attempts on that page, so its outcome comes
from the joined text and not from the shape of the list that produced it:
`failed` when no reading joined and at least one underlying attempt reached the
chair — or when the join could not carry every attempt and the carried ones were
all empty, because a completed absence may only be claimed over a page this
chair's join fully read (invariant 6); `not-run` when no underlying attempt
reached the configured chair;
`genuinely-empty` when every attempt joined and every one delivered an empty
body; `read` when the text carries a delivered character (delivered characters
beside disclosed omissions claim less, not more). Separators are placed only *between* delivered characters.
Joining every payload including the empty ones and calling the result `read`
whenever the list was non-empty gave a page of genuinely-empty acts
`payload="\n"` under a reading outcome — characters no act delivered, retained
as testimony to them (CodeRabbit W44). An act whose reading the join could not
carry is disclosed in `unjoined_act_attempts`; an act it carried as empty is not,
because it was carried.

The synthetic page presentation and receipt are fixture declarations of the
page-scoped invocation the skeleton is exercising, just as the act arm's
`fixture://` receipt is a declaration rather than a live serve. `page_join`
supplies that declared invocation's response fragments; it is not evidence that
a provider ran. A failed page record carries a presentation and receipt exactly
when at least one underlying act attempt reached the configured chair. If none
did, the page outcome is `not-run`, with `presented={}` and no receipt. Thus
`failed` never also means “never attempted,” and an absent or never-shown chair
is never forced to invent a serving moment.

## Act-attachment schema (R4)

Written by the same stage invocation that writes page testimony, one
`act-attachment` record per act in the proposal seal, held acts included
(`subject_id == act_id`). A held act carries one entry per chair with
`page_witness` false, `attached` false, `page_ordinal` null, and `alignment`
null. Its payload carries `attachments`: each entry with `chair`,
`attached` (bool), `comparable` (bool), `span` (`{start, end}`),
`content_health` (dict or null —
null is "health not recorded", a distinct fact), `page_witness` (bool,
strictly), `page_ordinal` (int for a page witness, **null** for an act-scoped
chair — the field is required either way, and the Perlector refuses a
page-scoped attachment that omits it as readily as an act-scoped one that
carries it), a `testimonium_ref` pointing at the chair's Testimonium or
page-Testimonium, an `attachment_basis` (`presented-region`, `anchor-line`,
`geometric-overlap`, or `unattached`), and
`alignment` — null for an act-scoped chair, and for a page witness exactly one
of:

**The denominator is `(chair, contributing page)`, not `chair`.** An act-scoped
chair contributes exactly one entry. A page witness contributes one entry per
page the act's proposal regions came from, so an act that runs across the page
break carries two — the primary page's entry holds the real comparison view, and
each continuation page's entry is explicitly unaligned with reason
`continuation-page-no-act-anchor` (a page anchor locates a line for an act it
begins, not for the tail that runs onto it). The Perlector reconciles that exact
pair set against the regions it actually read
(`pipeline/4_perlector/run.py::act_attachment_view`), so an attachment cannot
claim a page the ink does not support, or drop one the ink does.

- aligned: the closed key set `{status, anchor_basis, anchor_chair,
  anchor_span, witness_span, line_geometry, loss, offset_maps}`, with
  `anchor_chair` naming the sole configured Chandra witness
  (`declared_chandra_anchor_chair`) — a string exactly when `anchor_basis` is
  `act-anchor`, and null otherwise, refused either way round by both readers.
  On the live path the anchor IS that chair's retained page Testimonium: its
  page text, and the block spans its reported geometry carries into that text
  (the derived anchor, below). On the fixture path the anchor is the
  fixture's declared `[[chandra_anchor]]` stand-in, and the field identifies
  the configured association without claiming the declaration was re-derived
  from a retained Testimonium. The Designator is structurally out of reach
  because it is not a witness role, and the lectio prior is written after this
  stage runs, so nothing here could read it. `line_geometry` carries one
  rectangle per anchor line -- exactly one on the fixture path, and on the
  live path every reported block that overlaps the act. `anchor_basis` is one of
  `act-anchor` (computed through Chandra's located anchor line),
  `no-page-anchor` (a genuinely-empty witness's trivial zero-length attach on
  a page with no Chandra anchor at all — the ink-free/fallback path; blank
  confirmation stays open), or `act-line-not-located` (the page's anchor
  exists but locates no line for this act — the Recensor's
  `blank_corroboration` refuses to seal a terminal blank on it).
  `witness_span` and its top-level `span` mirror index the raw retained page
  reading. Alignment is computed over the markup-stripped,
  whitespace-collapsed view and translated back to raw character offsets before
  publication. `offset_maps` instead map normalized-text positions to raw
  offsets, with `None` for synthesized separators, while `loss` records what
  normalization changed. Never index an `offset_maps` entry with
  `witness_span` or `span`: they use different coordinate spaces.
- unaligned: `{status, reason}`, reasons among `missing-chandra-page-anchor`,
  `act-anchor-line-not-located`, `no-overlap-with-act-anchor`,
  `no-raw-counterpart-for-aligned-span`,
  `character-limit`, `character-pair-limit`, `timeout`,
  `no-common-anchor-text` (the aligner's own reasons pass through
  verbatim), `non-reading-page-testimonium-<outcome>` for a native page
  capture that produced no reading, `non-reading-act-attempt-<outcome>`
  where the page record is the legacy join and this act's own attempt is
  the non-reading fact (the page record itself may still read, on the
  strength of another act), and `continuation-page-no-act-anchor` for a
  contributing page that is not the act's primary one.

For a page witness, `attached` is derived from geometry alone since Unit 10C:
this chair's reported boxes overlap one of the act's sealed proposal regions and
its outcome is a reading. `comparable` is the separate question of whether text
exists to compare for THIS act — a page witness is comparable exactly when it is
attached, its alignment is `aligned`, and its page record retains a string; an
act-scoped chair, exactly when it is attached and its own retained derived
`payload` is a string. A structured native report is therefore retained, visible
and incomparable. `comparable` implies `attached`, never the reverse, and
`common/contracts/outcomes.py::witness_coverage` counts a chair toward the
witness floor only when BOTH hold — the guard that lets `dissent_against` record
a structured witness as `compared: "unknown"` instead of refusing the run
outright. Neither reader takes `comparable` on trust: the Perlector
(`act_attachment_view`) and the Recensor (`act_attachment_facts`) each re-derive
it from the Testimonium this entry names. `attached` is re-derived by the
Perlector for both scopes and by the Recensor for both scopes: page witnesses
from their geometry against the sealed proposal, and act-scoped witnesses from
the outcome of the exact current Testimonium the attachment must reference.
The Recensor derives act-scoped `comparable` from that same current record's
retained payload, not from the attachment row's own `attached` boolean; forging
both booleans false therefore cannot preserve the equation while removing a
completed chair from the floor. All three files pin the shapes above; a field
change here is an interface change and lands in all three in the same commit.

The Recensor takes an act's chair-level `attached` as the OR across that chair's
contributing pages (`act_attachment_facts`): the act-level floor asks whether the
chair delivered this act at all, and a continuation page with no anchor of its own
may not erase the primary page's valid attachment. Every page reference stays
separately checked by the page-scoped content denominator beside it.

## Attempt tally

The stage's derived manifest is rebuilt from immutable Testimonia, compared to its
stored inventory, and checked against the Testimonium schema, provenance, receipts,
and exact region inputs before a re-read may append. The full act/chair denominator
is reconciled at the close of a pass rather than before one — see the last section,
which says why. `attempt_tally()` returns `KNOWN` only when that inventory is
whole. An absent, garbled, truncated or divergent inventory returns `UNKNOWN`,
`count=null`, `hold=true`, and the check runs before anything is written, so a
re-read over a damaged inventory appends nothing. The stored inventory counts as
evidence that attempts existed even when the walk finds none left: a folder whose
whole Testimonium layer is gone but whose manifest still describes it holds, rather
than taking the first-run path and writing attempt 1 over a history that recorded
more. The closing tally can also hold *after* an attempt was appended — the append
happened and is retained; what the hold says is that the folder no longer
reconciles.

**This channel is the count of attempts, not a witness's own output.** A provider
response the stage could not retain is one witness's channel, and the `failed`
attempt naming it — with `content_health.recordable=false` and a reason — is a
counted, accounted record. It leaves the tally `KNOWN`, the act under-witnessed
and the run visibly partial, and it does not stop the Perlector reading ink that
was never in doubt. Two `recordable=false` shapes are still `UNKNOWN`, because
neither can be resolved in the run's favour: a record claiming `read` or
`genuinely-empty` while saying nothing could retain what it read, and a `failed`
record carrying no reason.

This check runs immediately after Stage 3 and before a later re-read; the
orchestrator stops at an Attestatores `UNKNOWN` hold, so an older complete export
cannot mask it. Direct invocation of a later owner stage still needs that owner's
own evidence-boundary check and is not simulated here.

**Whether every configured act/chair pair is accounted for is a closing check, not
a precondition.** A pass killed part way through leaves attempts on disk and no
stored manifest, and the pass that would supply the missing pairs may not be
refused for their being missing. The stored manifest is still required before a
re-read, per spec 07 test 5, so an interrupted pass holds until someone
re-derives it — `RunTree.write_manifest("attestatores")`, one step, losing
nothing because the manifest is derived from the immutable attempts. After that
the pass resumes: the attempts already written are byte-identical repeats and the
missing ones are created. If the denominator still does not reconcile once the
pass has run, the folder holds.

One thing to know before reaching for that step: it loses nothing *while the
attempts it describes are still on disk*. Over a folder whose attempts are gone,
re-deriving the manifest discards the last record that they existed, and the
pass that follows restarts the history at ordinal 1. That is a decision someone
may legitimately take; it is not one to take without reading the manifest first.

## Who wrote what

The live reading seam this stage sits in was built by several seats across eight
units. The record of which seat wrote which unit is the dispatch record — the
workflow scripts each seat was launched from (`seam-u1-*`, `seam-u2-*`,
`seam-u3-u5-u7p-*`, `seam-u4-u6-*`, `seam-u8-u7e-*`), which name the model each
seat was dispatched as. **The commit trailers on this branch are self-reported
and several are wrong**: some Opus and Sonnet seats copied the host's own
`Co-Authored-By` line. Where a trailer and this table disagree, this table is
the record. The Fable seat was the host orchestrator and wrote no unit code.

| unit | built by | verified by | fixed by |
|---|---|---|---|
| U1 contract and parser | Sonnet 5 | Opus 5 | Sonnet 5 |
| U2 client and fakes | Sonnet 5 | Opus 5 | Sonnet 5 |
| U3 Perlector live reader | Sonnet 5 | Opus 5 | Sonnet 5 |
| U5 Attestatores live boundary | Sonnet 5 | Opus 5 | Sonnet 5 |
| U7p placement-tier plumbing | Sonnet 5 | Opus 5 | Sonnet 5 |
| U4 Perlector wiring | Opus 5 | Opus 5 | Sonnet 5 |
| U6 Attestatores wiring | Opus 5 | Opus 5 | Sonnet 5 |
| U8 cross-file seams | Opus 5 | Opus 5 | Sonnet 5 |
| U7-e2e end to end | Opus 5 | Opus 5 | Sonnet 5 (host committed) |

This stage's own live boundary is U5, its wiring into the stage is U6, and the
cross-file seams described under "The cross-file seams that let a live pass carry
every chair" are U8. U7-e2e is the whole-run proof recorded at the end of that
section.
