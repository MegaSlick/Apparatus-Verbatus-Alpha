# Perlector — contract

The Perlector writes one append-only `kind="perlectio"` record for each reading
attempt under `4_perlector/artifacts/`, plus one append-only `kind="lectio-nuda"`
record for each sampled unprimed instrument reading. This walking-skeleton writer
takes its established text from the declared synthetic fixture solely to exercise
the evidence shape; it does not claim a real model reading. Its artifacts are
`skeleton.v1` envelopes with derived identities, attempt bindings, self-hashes, and
checked direct inputs.

**Successors consume this stage's artifacts, not its implementation.** Recensor reviews
the Perlectio it names, Archetypus establishes only that exact accepted reading
(`pipeline/6_archetypus/run.py:522-582`), and Armarium rechecks the established record
against it (`pipeline/7_armarium/run.py:1045-1219`). No successor imports Perlector code.

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

For every proposed act, the stage reads every Designator region currently in the
act's history, recomputes its crop from the sealed Exemplar page and transform,
checks the crop bytes, and validates the region's own resolved Designator
identity/revision and serving receipt. It reads and validates every Testimonium
for the act, then derives one current record per chair by unique attempt ordinal;
all superseded records remain immutable history. It does not select a preferred
witness or use witness agreement to choose its text.

Recovery regions are readable evidence for a later attempt. They remain marked
`witness_covered=false` unless a completed Testimonium actually names that exact
region; a recrop never rewrites what a witness saw.

### Native witness intake

The consumer validates the same closed `presented`/`observed` waist the
Attestatores writes. Page ids and ordinals are reconciled to the sealed Exemplar;
whole-page and adapter-crop transforms are executable; region presentations are
looked up physically and must resolve to one Designator proposal; observed boxes
are bounded integer sealed-page coordinates; spans address retained text; and
preferences, floats, unknown fields, malformed ordinals, and overlapping spans
are refused. The page-Testimonium read additionally applies the shared full
payload allowlist and validates Attestatores provenance/receipt requirements,
not only geometry. A page outcome in `read | genuinely-empty | failed` is
attempted and receipt-backed; `not-run` is explicitly unpresented and receipt-free.

`unpresented_regions` is re-derived for region, page, and adapter-crop
presentations by the common page-space-containment function. An empty list beside
a real presentation means all bound proposal crops lie inside it; beside
`presented={}` it is inapplicable, and the non-attempted record is independently
forbidden to bind regions. A continuation crop on another page therefore cannot
be hidden by changing presentation kind or deleting the field's member.

Before routing any observation, `sealed_proposal_regions` validates every
Designator region's provenance and full Exemplar crop lineage, including regions
belonging to an act outside a narrowed `--act` invocation. The shared routing
derivation then asks the same question of current act and page Testimonia:
whether each `native`/`derived` box has positive-area overlap with any sealed
proposal on its presented page. The rule records
`{rule="positive-area", status="unmeasured"}`; `bounds_source="presented"` is
excluded because it reports no witness geometry. Routing is overlap; Unit 10C
coverage is containment, and the two must remain separately named.

The Perlector's own derived `unrouted-observation` is deduplicated within the
invocation and printed before sealing; it is not retained by this stage. It does
not need to be. The Attestatores page Testimonium already stores the same common
derivation in `partition_disagreement.unclaimed_observations`, and the Recensor
re-derives the finding independently from the observed geometry and the current
sealed proposal denominator rather than trusting that retained snapshot. The
print here is an operator-visible echo of a fact that is retained elsewhere, so
no second artifact vocabulary is introduced.

## The one attempt model

**Which reading attempt this is, is a function of the act's crop history alone.**
`_next_attempt` is `recovery_region_count(act_id, regions) + 1`: one reading of the
proposal, plus one for each recovery crop cut since. Witness testimony never moves
it, and the same identity is enforced downstream by the Recensor, the Archetypus
and the Armarium — `len(readings) == recovery_regions + 1` — so a recovery crop
must be reread before any text is established and a reading may not appear
unrequested.

Deriving it from the act's state rather than from how many times the stage has
been invoked is what makes a rerun of an unchanged run recompute the same ordinal,
produce the same bytes, and be reused rather than rewritten.

Testimony is deliberately absent from the derivation. A Testimonium is a clue that
primes a reading, never the ink the reading is established from (ARCHITECTURE),
so a second look by a witness does not make a second reading exist,
and re-reading an act because a witness spoke again is a re-roll, and
recovery restores coverage, never quality. The consequence for the upstream stage is that an act's witness layer
closes when this stage reads it, enforced at the Attestatores' own entry
(`pipeline/3_attestatores/CONTRACT.md`, "The one attempt model") rather than
discovered here as an immutability refusal on a reading identity nothing can move.

The count comes from `common/stage.py::recovery_region_count` — the shared reader
the other three stages ask — and the regions are read once, before the ordinal is
derived from them. A private `origin == "recovery"` comparison scored every
unrecognized origin as zero, so a resealed Designator tree could be read and
published here at the wrong attempt and become fatal only at the next stage, over
a Perlectio that is already immutable. An unplaceable origin is now
refused here, before any model call or publication, and named for what it is: a
region whose place in the recovery denominator is unknown.

**A reading whose witness basis has since been superseded is not reconciled.** The
Recensor, Archetypus and Armarium each pass the current reading back through
`common/stage.py::require_current_witness_basis` before accepting, establishing or
exporting it, so a Testimonium appended after the reading was established cannot
be structurally invisible at the point where the export decides to say `complete`.

## `kind="perlectio"`

The subject is the stable act identity and the attempt is `perlegere:<ordinal>`.
A successful or attempted reading payload contains:

```text
act_key, attempt_ordinal, text
basis = {
  regions = [{region_id, image_path, image_sha256, transform,
              verified_dimensions, source_page_ordinal, source_page_id,
              structure_provenance, witness_covered}, ...],
  testimonia = [{chair, artifact_id, outcome, reference}, ...]
}
dossier          -- the full spec_08 input contract, persisted as evidence
                    (see below); a superset of `basis`, never a replacement
prompt           -- {serving_recipe, chair_identity_sha256, dossier_digest,
                    rendered_sha256, builder_sha256}: the declared prompt this
                    reading was actually produced through (invariant #49, see
                    below). `builder_sha256` digests the prompt builder's own
                    code, so editing the builder changes the record even when
                    its name and every other field stay identical
dissent          -- derived-comparison-view rows (see below)
truncation       -- {classification, signals, measure}, present on every
                    attempted reading regardless of outcome (see below)
uncertain_spans  -- [{start, end, alternatives, confidence}, ...]: the exhausted-cap
                    projection first (cap 0 only), then the reader's own assessed
                    doubts over the published text. NOTHING is dropped, including
                    an exact repeat: the layer records that those characters were
                    doubted twice. It does NOT record by what -- no artifact in
                    the run names the instrument behind any one span, and two
                    audit flags of different classes may share one location, so a
                    repeat is not by itself an agreement between the audit and
                    the reader. The operator console shows an identical pair once
                    with the count beside it and claims nothing more; consumers
                    that count doubts should do the same
gaps             -- [{position, start, end, witness_evidence}, ...]: the whole-act
                    gap of a `no-readable-text` outcome, or the reader's own
                    zero-width gaps (empty witness_evidence)
uncertainty_assessment -- {state, problem}: the reader's doubt-report state for
                    the call whose text is published -- `assessed`,
                    `not-assessed` (no doubt channel) or
                    `malformed` (a report the annotation schema could not anchor,
                    retained as its problem; the Recensor holds). An empty
                    `uncertain_spans` under `not-assessed` is an absence, never
                    confidence. Travels
                    into the canonical uncertainty layer as `assessment`.
                    Carried on the three instrument records too (Lectio nuda,
                    `lectio-prior`, `primed-without-prior`): a reader answers the
                    same way on an instrument call, and a doubt reported on a
                    nuda reading is a measurement.
audit            -- {draft_ref, finding_ref, finding_digest, unresolved,
                    examination, reproofs, request_digest}: the R5b Pass-C
                    chain, which re-proof instrument was actually delivered,
                    and what became of the re-examination (see below)
provenance
```

`basis` is unchanged from the first landing and is what the three downstream
consumers actually read (`common/stage.py::reading_basis_regions` walks
`basis.regions`). Nothing here may ever remove or repurpose it.

The envelope's direct inputs bind every full-resolution crop, every page render
and its sealed source page, and every Testimonium reference. The dossier is
therefore not the sole claim that the reader saw its page context. An established
Perlectio's inputs also bind the neighbouring acts' Testimonia it was shown as
clues; `dossier.neighbours` names each of them as a clue, and `basis.testimonia`
alone names this act's own witnesses (see "Neighbour clues").

**Two named limits of the doubt report, stated rather than implied.**

*The live doubt grammar is one level deep.* The pinned instruction asks the reader
to write `[[?]]` where ink cannot be read and `[[reading]]` or
`[[reading|other|...]]` where it is unsure; `annotations.read_doubt_marks` turns
those into zero-width gaps and `low` spans with their alternatives over the clean
text. The grammar carries no level of doubt, so every marked span is `low`. A
`[[` or `]]` that is not a closed mark, or a mark whose reading is `?`, publishes the
raw answer unchanged under `malformed`. Gap marks over an answer that is otherwise
blank add nothing: the `no-readable-text` outcome's whole-act gap already says it.
By default there is no Pass A; `--blind-read fed` opts in to Pass A and to Pass B seeing its
clean text (`--blind-read saved` makes Pass A but never shows it to Pass B). When the
draft is fed, `self_revision` offsets index it: `reading_span` in the final text,
`testimonium_span` in the draft. When it is withheld, `self_revision` is not measured.
When a fed draft's comparison would pass the sealed dissent step budget,
`self_revision` is the explicit non-verdict `{measured: false, reason:
"comparison-step-limit", max_comparison_steps}`, never an empty list, and the
canonical `self_revisions` is null. Pass A's marks stay on its own
record. Truncation is measured on the clean text. The re-proof answers in JSON and
reports no doubts; a replacement carrying a mark, or a replacement over text Pass B
marked, publishes `malformed`, because the marks cannot be re-anchored through the
edit. Whether a real reader uses the marks is measured on the first live run.

*The assessed tail is not bound to a reader.* Where the state is `assessed`,
`common/perlector_audit.py::validate_chain` proves the exhausted-cap projection
leads `uncertain_spans` and stops there; the annotation layer proves the
remaining offsets anchor to the exact text, but no artifact holds the reader's
report separately, so nothing proves the tail is what a reader actually said.
Under every other state both layers are constrained exactly (no span at all, and
no gap but the whole-act gap the `no-readable-text` outcome owes), because a
reader with no channel has nothing of its own to publish. A live reader's marks
sit in its retained raw response, so the tail can be re-derived from those bytes;
no validator re-derives it yet.

**The field set above is closed and checked before publication**
(`run.py::validate_reading_payload`). Three of the four failures spec 08's
schema test names — missing identity, missing dissent, missing regime record —
are *absent* fields rather than wrong ones, which is the failure a per-field
type check never sees. Agreement is one row per witness with no departure spans;
an empty `dissent` list is required for unprimed Lectio nuda and `lectio-prior`,
which were shown no testimony. Omitting primed rows would make agreement indistinguishable from an
instrument that never ran.

### `dossier` — the input contract, persisted as evidence (spec 08)

#### Unit 14A native-testimony seam

The dossier consumes retained **derived** testimony, never a raw vendor blob.
`reported` is this chair's text for this act or null, and `reported_basis` is
exactly `own-report`, `page-slice`, or `none`. A structured derived payload is
visible as `reported: null` / `reported_basis: none`; it is not coerced and it
cannot satisfy the witness floor because coverage requires both `attached` and
`comparable`.

`presented` is what this chair was shown for this act — `region`, `page`,
`adapter-crop`, or `none` — and it is a fact about the presentation, not about
where the text came from: a page witness under a native capture legitimately
records `presented: region` for its act-scoped channel beside
`reported_basis: page-slice`. `observed` is that chair's own boxes for this act,
`{ordinal, bounds, bounds_source}` sorted by ordinal.

`edge_deltas` are four signed offsets from one chair's native/derived observed
box to a **sealed proposal** region: `{ordinal, region_id, offsets:{left, top,
right, bottom}}`, one row per overlapping sealed region, ordered by
`(ordinal, region_id)`. They are never chair-vs-chair, they are never ranked,
and nothing thresholds them. `test_edge_delta_evidence.py` combines behavioral
derivation checks with a narrow AST guard against direct ordering expressions
that still name `edge_delta` or `offsets`; aliases remain a code-review concern.
A `presented` box contributes none: only reported geometry counts. Page-scoped
unclaimed/unobserved/ambiguous partition facts remain on the page Testimonium
for the Recensor; the dossier carries only act-scoped correspondences.

`unpresented` lists the act's region ids this chair was not shown. Its two empty
spellings are different facts and stay distinguishable through `presented`:
`[]` beside a real presentation means every bound region was presented; `[]`
beside `presented: none` means no presentation speaks for any region at all.

```text
act_id, act_key, witness_regime
regions       = [{region_id, image_path, image_sha256, witness_covered}, ...]
                # a Lectio nuda dossier's region rows carry no witness_covered:
                # coverage is a witness-derived fact, and the baseline saw none
page_renders  = [{source_page_id, source_page_ordinal, source, image_path,
                   image_sha256, transform}, ...]
testimonia    = [{witness_label, model_name, resolved_provenance,
                   training_domain, outcome, reported, reported_basis,
                   presented, observed, edge_deltas, unpresented}, ...]
neighbours    = {preceding: null | neighbour, following: null | neighbour}
                # primed dossiers only; see "Neighbour clues" below
dossier_digest
```

Built and validated by `dossier.py`. Deterministic and shuffle-invariant: the
same evidence in any input order produces identical bytes (`test_dossier.py`).
Carries **no order-bearing, trust-bearing, or preference-bearing field anywhere**
— `dossier.assert_no_order_bearing_field` sweeps every key by name, and testimonia
sort by their *displayed* label so presentation order is deterministic but
meaningless.

**Witness regime.** `witness_regime` is `named` or `blinded`, sealed as a real
run-level flag (`--witness-context`, `common/stage.py`) rather than a constant.
Under `named`, each row carries the witness's resolved model name and the exact
validated provenance from its Testimonium, so factual context shown in the
prompt is recorded rather than inferred. Under `blinded`, `witness_label` is a
stable per-run pseudonym
(`pipeline/4_perlector/regime.py::pseudonym_for`) and `training_domain` is
withheld entirely — a training-domain sentence can identify a witness as surely
as its name, so model name, provenance, and domain all leave together. The pseudonym has no stored
reversible map: reversal is recomputing the same deterministic digest over the
public roster in `run.json["witness_chairs"]`.

**Page renders.** One whole-page render per distinct page an act's regions
touch, stored content-addressed under this stage's own blob store, sized by the
run's sealed `config/perlector_protocol.toml` `[page_context]` rule and recorded
with its `reason`. A page is rendered with its long edge capped at
`maximum_edge` (2,560), `reason: "legible-ink"`, so its ink is legible to the
reader and not only its layout; 2,560 keeps a 300-DPI letter or A4 page inside
the 27B Perlector row's `max_pixels`, so the chair sees exactly the rendered
pixels. A page the act's own crops cover whole (their union is the page) already reaches
the reader at full resolution through those crops, so it is rendered at
`covered_page_edge` (1,024), `reason: "covered-by-crop"`, as layout only; every
page of an act spanning more than one page is rendered at `covered_page_edge`
too, `reason: "multi-page-act"`, so the page render never refuses an act the
row held with layout renders. The rule is decided from the act's crops, never
from whether a request fits. The edge is a bound, not a
divisor. `transform` records `{operation, source_dimensions, target_dimensions,
maximum_edge, resampler}` and `source` names the sealed page it came from, so
the render is reproducible from the Exemplar plus the record (ARCHITECTURE
invariant 3) by someone who does not also have this module. A page already
inside the bound records `resampler: "identity"` rather than reporting a resize
that did not happen.

Every one of the 4,572 RecordGold gold acts, each on one page, fits the row
under this rule with its neighbour clues, as it did at 1,024
(`operations/corpus/perlector_request_fit.py`), and no pinned shape needs more
under this rule than at 1,024. The shapes the row does not hold, refused at
1,024 as well, are pinned in
`operations/serving/test_serving_catalogue_capacity.py`: an act over three
pages with whole-page crops (34,967 tokens against 32,768), and a dense act over
a page turn with a whole-page recovery crop, three witnesses reporting two pages
of text each and a fed prior draft charged at the reading cap (36,617). The live
reader refuses each before sending; the act is published failed with the
capacity record, never read with a page dropped or downscaled to fit.

**Neighbour clues.** A primed dossier names the acts just before and just after
this one in the Designator's `expected_acts` sequence, across page breaks, and
null at either end. Each neighbour is `{act_id, act_key, same_page,
witnesses, unavailable}`, where `same_page` says whether it touches a page this
act's regions touch, and each witness row is `{witness_label, outcome, reported,
reported_basis, shown, testimonium_ref}` for every configured witness, built
through the same `dossier.witness_rows` path, regime labels and page-witness
slices as the act's own testimonia. `reported` is cut to the sealed
`[neighbours] characters_per_row`: a preceding act's tail, a following act's
head, with `shown` = `whole`, `tail`, `head`, or null where the witness
reported no text. A neighbour whose readings cannot be carried -- the Designator
held it, or its sealed regions or witness records do not validate -- carries no
witness rows and says why in `unavailable`; its defect is its own act's to
answer for when that act is read, and this act is still read. Building a clue
prints nothing: a neighbour's unrouted observations are reported when it is
read. The Perlector validates the closed shape before publication, refuses a
neighbour that is this act, and requires every `testimonium_ref` to name a
sealed Attestatores Testimonium.

They are clues only. They come solely from sealed Attestatores records, never
from another act's Perlectio, so an act's dossier and prompt are the same at any
reading width and whether or not its siblings have been read
(`test_neighbour_clues.py`). They never enter `basis.testimonia`, `dissent`, a
gap's `witness_evidence`, or Pass C's draft and finding; they join only the
established Perlectio's envelope inputs, as lineage, and no consumer reads them
as this act's evidence. Unprimed arms (Lectio nuda, `lectio-prior`) carry none.
The prompt renders them after the testimonia under `neighbouring_acts:` (a
witness with no text as "no reading", a withheld clue as "witness readings
unavailable"), followed by the sealed neighbour fragment: context only,
transcribe only this act's ink, never copy a neighbour's text. The live reader
charges the neighbour block one token per UTF-8 byte.

**Ink past the crop.** The pinned transcription instruction, sent to every arm,
asks the reader to read the act's ink through to its end within the crop and,
where the ink continues past the crop's edge, to stop there and write `[[?]]`.
That mark becomes a zero-width `trailing` gap -- a mark followed only by
whitespace or closing punctuation still ends the reading -- so the act is
established `partial` and the export is partial (`pipeline/7_armarium/
armarium_export.py`, a delivered act with a recorded gap). Nothing yet consumes
the gap to recrop: a recrop from a trailing gap is not built.

**Training-domain context.** `config/witness_context.toml`, a new
Perlector-owned declaration (not part of `common/chairs`/`ChairIdentity`),
mapping each configured chair to a factual, non-evaluative training-domain
sentence. Every configured witness must have an entry: `common/stage.py`
refuses at run creation — before any stage writes — an entry that is missing,
unaddressed (a chair the roster does not configure), or whose
`training_domain` is not a non-blank string. The full per-entry schema is
still the dossier build's, which refuses by name when it loads the
declaration.

### `dissent` — derived comparison views, never raw-string voting

```text
[{chair, compared: true, departed, departed_raw, departures, comparison_loss}, ...]
[{chair, compared: false, reason}, ...]                 -- did not report
[{chair, compared: "unknown", reason}, ...]              -- format not yet comparable
```

Computed strictly after the reading is fixed (`common/dissent.py`), over a
Unicode-NFC-normalized, whitespace-collapsed comparison view of both sides —
NFC first, so a precomposed accented character and the same character spelled
as a base letter plus a combining mark compare equal, which matters for
diacritic-heavy parish-register text where an OCR engine and a witness model
are not guaranteed to agree on normalization form for the same ink. **Pinned
forever: equality only, never a distance metric** — no per-chair parameter, no
similarity threshold. `departed` is the view comparison; `departed_raw` is the untouched
raw-string comparison, kept alongside because a normalization that dropped
characters on either side can otherwise hide whether the raw strings actually
agreed. A witness whose declared format cannot yet be reduced to a comparison
view (`format_capabilities.can_express_uncertainty`) is recorded `"unknown"` —
never guessed, and never dropped from the list. So is one whose report is too
long to align against this reading at all: a witness's report is a model's own
output that nothing upstream bounds, and a repetition loop running to a
32k-token cap would hold the stage for tens of minutes per act.
`dissent.MAX_COMPARISON_CHARACTER_PAIRS` refuses that case cheaply, before any
alignment starts. It does **not**, on its own, bound the matcher's work:
`SequenceMatcher`'s cost on low-entropy or scattered-difference text can run far
past the square the pair count assumes, so a comparison well under the pair
bound can still run for minutes. The sealed `[dissent] max_comparison_steps` in
`config/alignment.toml` is the real backstop: the matcher's work counted in
`common.alignment.StepCountedMatcher` steps and charged before it is done, so a
comparison that would pass it stops before the work, and whether a row is
compared depends only on the two texts, never on the machine that ran it. A row
it stopped carries the budget as `max_comparison_steps` beside its reason, and a
reader recomputing a sealed dissent under the same budget requires it exactly.
Either bound is on the **comparison**, never the text — nothing is clipped, no
reading changes, and the row says in words which bound stopped it and that it
did not run.

`departures` says *where*: `[{reading_span: {start, end}, testimonium_span:
{start, end}}, ...]`, an alignment from `difflib.SequenceMatcher.get_opcodes`
over the raw strings, so `reading_span` indexes this Perlectio's own `text`. A
boolean per chair cannot tell one wrong letter from wholesale disagreement, and
that distinction is the instrument's entire purpose. An alignment is not the
distance metric this module refuses: it carries no number and nothing to
threshold. `SequenceMatcher.ratio()` is that metric, is not called, and is the
thing to refuse if it ever appears here.

### `prompt` — invariant #49, on the record

```text
{serving_recipe, chair_identity_sha256, dossier_digest, rendered_sha256,
 builder_sha256}
```

Built by `prompts.py` from the resolved chair's own declared serving recipe,
*before* the reader is called, from the dossier the reader is then shown. A
recipe with no registered builder refuses outright rather than falling back to
some other chair's template — the silent fallback is the exact harness failure
invariant #49 exists to prevent. The identity digest travels beside the recipe
because a chair is a role and a role can be occupied by a stock model, a vendor
model, a local checkpoint or an unmerged adapter in turn; two Perlectiones can
therefore be compared for whether they were prompted the same way rather than
assumed to have been. The rendered bytes are recorded by digest only: they
contain every testimonium the reader was shown, which already travels once on
`dossier`. `builder_sha256` digests the whole prompt module's code — not one
function's, because builders render through helpers — so any edit to prompt
text or builder logic renames every Perlectio it prompts rather than hiding
behind an unchanged recipe name. It is the module's syntax tree with comments
and docstrings stripped (`common.contracts.canonical.code_digest`): a code
change anywhere in `prompts.py` moves the claim, even one that does not change
the rendered bytes; a comment or docstring edit does not. A Python upgrade that adds
AST fields also moves it, and `common/request_capacity.py` refuses loudly until
it is re-pinned: an interpreter upgrade is a digest event.

### `truncation` — the instrument, not an assumption

```text
{classification: "complete" | "truncated" | "unknown",
 signals: {stop_reason_declared, unclosed_structure, length_suspicious | null, ends_abruptly},
 measure: {region_pixels, page_pixels, smallest_page_pixels, characters,
           length_floor_characters_per_page, legible_page_pixels, length_judged}}
```

`measure` is what the length signal was judged from, and it is closed:
`common/perlector_audit.py::validate_truncation_record` refuses a record
without it. The region's page-space area (the union of an act's crops per page,
summed over the pages it spans), the sealed area of those pages, the area of
the smallest single page the act spans, the reading's character count, and the
floor and legible page size from the run's own sealed
`config/perlector_protocol.toml` `[truncation]` table — every term of
`characters * page_pixels < floor * region_pixels`, judged only when
`smallest_page_pixels >= legible_page_pixels`, so a reader holding the
record and nothing else re-derives `length_suspicious` instead of trusting it
(configuration protects reproducibility going forward, the record
protects the past). The shared validator does re-derive it, and refuses a record
whose signal disagrees with its own geometry; where the caller also holds the
reading the record was measured over it binds `characters` to that text as well,
and where it holds the sealed table it binds the floor and the legible size.
When the smallest page is under the legible size, `length_judged` is false and
`length_suspicious` is `null`: the length was not consulted, and it counts
neither as a clean nor as a suspicious vote — the verdict comes from the other
signals.
The floor is dimensionless on purpose — an absolute pixels-per-character ratio
held every ordinary 300-DPI act as truncated while clearing this repository's
fixture pages — and it is sealed rather than a module
constant so a change between two runs moves their `config_digest`.

Computed by `common/truncation.py` for every attempted reading, primed or nuda,
regardless of what outcome it ends up producing — so the record is never
optional detail dropped exactly when it would matter most. An engine-declared
`stop_reason_declared == "length"` is authoritative for `truncated`; three
suspicious computed signals are `truncated` on their own; and `complete`
requires **both** an engine that said it stopped of its own accord and three
clean computed signals. A split vote holds as `"unknown"`, and so does silence
from the engine — three clean signals say a reading does not *look* cut off,
which is not the claim that it ran to its own end. Neither is ever resolved
toward complete. (A serving adapter that drops the engine's stop-reason
therefore holds every reading until it declares a second engine signal of its
own; the old pipeline's Chandra adapter did exactly that and derived truncation
from `completion_tokens >= cap` instead.) Both `truncated` and `unknown` classifications map to
the existing outcome `"truncated"` (already `FAILED`-class): **`outcome ==
"truncated"` therefore means "not established complete," which covers an honest
ambiguity as well as a confirmed cut-off** — the payload's `truncation` field is
where the two are told apart.

### `uncertain_spans` and `gaps` — the establishment firewall

`uncertain_spans` are read text held with less confidence — real characters,
because that is what "read, with alternatives" means; validated only for shape
(bounds inside `text`, a closed `confidence` vocabulary).

`gaps` are where sight failed. **The firewall is structural**: every gap's
`start` must equal its `end` — zero-width inside `text` — so a gap cannot carry
characters regardless of what `witness_evidence` says. `witness_evidence`
attaches witness variants as linked, displayable evidence, never as text.
Each evidence row is `{chair, testimonium_id, reference, variant}` — the
digest-checked reference to the witness's own sealed record, not just a chair
name a reader would then have to go looking for.
`position` is one of `leading | internal | trailing | whole-act`, each with its
own bound (leading starts at 0, internal is strictly inside the text, trailing
ends at `len(text)`, whole-act requires
`text == ""` and is the gap's only entry). **Bidirectional**: an outcome of
`no-readable-text` requires exactly this whole-act gap, and a whole-act gap
forces the outcome to be `no-readable-text` — an outcome of `read` may never
carry one, which would otherwise let an empty text flow onward as though
something had been established (`common/reading_annotations.py::validate_whole_act_consistency`).

A held act or unavailable reader receives an explicit non-completed Perlectio
with its reason, not a fabricated text. `truncated-reading` and
`no-readable-text-reading` (declared reading failure) and
`engine-truncated-reading` (declared engine stop-reason, no reading-failure
declaration at all — the detector's own authority) exercise the three paths
end to end; Recensor treats every non-completed outcome as a visible hold.

**Acts on a registered re-shoot are held, not read.** The physical-act partition is
built once per run from the sealed corpus-register snapshot, with no capture
alignments: no alignment producer and no cross-capture read (Unit 19C/19D) is built.
An act whose capture the register records as a member of a physical page therefore
gets the partition finding `capture-page-alignment-unresolved`, and an act in a
resolved multi-capture group has no finding but still cannot be read one capture at a
time. Both are published as `not-run` with the closed shape `{act_key,
attempt_ordinal, reason, hold, provenance}`, where `hold = {code:
"cross-capture-read-not-built", partition_finding}` and the published partition blob is
the record's input. Today every such hold carries `partition_finding =
"capture-page-alignment-unresolved"`: a group forms only through a capture alignment,
so the resolved-group branch (`partition_finding = null`) is unreachable until an
alignment producer is built, and then holds a confirmed re-shoot until the
cross-capture read replaces it. The attempt ordinal is derived like any reading's, so a
re-asked act never collides with its earlier hold. The Recensor treats the hold as
terminal like a Designator hold: it never requests recovery for the act and holds it for
review with `cross-capture-read-not-built` and its remedy in the reason, which the
export entry carries. The rest of the run is read, and the held acts reach the
Archetypus and Armarium as held. Reading one capture would establish that capture's text for the physical act,
which is a pick; refusing the run would lose every other act's reading.
Any other partition finding is a defect in the denominator and still refuses the run
before any Perlectio is published (`logical_reading.cross_capture_holds`).

## `kind="lectio-nuda"`

**Never `kind="perlectio"`.** The subject is the act identity and the attempt is
`lectio-nuda:<ordinal>` (never `perlegere:<ordinal>` — the two operations occupy
disjoint identity spaces by construction, so nothing can ever confuse a nuda
attempt with an establishing one). Same payload shape as a Perlectio, except it
carries no `basis` (there is no witness basis to record), it carries a
`sampling` record, its `dossier.testimonia` is always `[]` and its `dissent` is
always `[]` — nuda withholds testimony, never sight, so its dossier still
carries the same regions and page renders a primed pass would.

Sampled by a predeclared, run-sealed design: `--nuda-per-mille` (0–1000,
`nuda.py`), a deterministic hash-threshold rule over `(run_id, act_id)` — never
`random`, so the identical command samples the identical acts. Default `0`
(off) for every scenario that predates it.
`lectio-nuda-sampling-design.v1` denotes this exact experimental condition:
an act-level, unprimed Lectio with no testimony or prior draft, selected by
`digest-threshold-over-run-id-and-act-id.v1`. Changing the condition or rule
requires a new subject; the record's `target_version_hash` separately binds the
exact run configuration, including the rate and selector, that executed it.
**A non-zero rate refuses without that sealed selector and exactly one typed
approval record** whose sole subject is the selector, whose action is `other`,
and whose target version is the run's own sealed `config_digest`. The resolved
subject travels with its typed reference to `nuda.sampling_design`, which refuses
an approval for the other arm before publication. Each record carries
`sampling = {nuda_per_mille, selection_rule, approval_ref}`, with
`approval_ref` as the approval artifact's path and digest, because a sample of
unknown design measures nothing. The same reference is an
envelope input, so an ordinary artifact read compares the approval digest to
the retained receipt bytes instead of merely displaying an unchecked hash.

**Module boundary, not convention.** Every real consumer (`5_recensor`,
`6_archetypus`, `7_armarium`, the orchestrator's own recovery dispatch) filters
`entry["kind"] == "perlectio"` before reading anything and derives `attempt_id`
from the `perlegere` operation; a `lectio-nuda` record is structurally outside
both filters, and a reference naming one as a Perlectio is refused by
`RunTree.read_artifact_reference`'s own `kind` check
(`test_lectio_nuda.py::test_a_forged_review_naming_a_nuda_artifact_as_its_perlectio_is_refused`).

## Consumer obligations

Recensor derives the latest reading by unique attempt ordinal, refuses a tie, and
writes the exact Perlectio reference it reviewed into every review/request. The
ordinal is not taken on the payload's word: every consumer names the operation it
is collapsing attempts of (`perlegere`, `recense`, `read:<chair>`) and the sealed
`attempt_id` is recomputed from (subject, operation, ordinal), because the envelope
binds `artifact_id` to that token without ever re-deriving the token itself.
Ordinals must also be the contiguous run 1..N -- attempts are append-only and never
reused, so a gap is an attempt that is no longer here, and a manufactured far
ordinal cannot leapfrog the attempt that happened.
Archetypus and Armarium follow that reference rather than independently looking up
whatever reading now sorts latest. This prevents an unreviewed recovery reading
from becoming established text.

**A `lectio-nuda` record is never a valid Perlectio reference for any consumer,**
by construction: its `kind` differs, and its `attempt_id` derives from a
different operation than any reference a Recensor review or Archetypus would
ever have recorded for a real reading.

## R5a prior-draft protocol

`--blind-read` sets the Pass A blind read: `off` (default), `fed` or `saved`. Pass A, the
image-only draft, runs under `fed` and `saved`. Then every readable act
emits a `kind="lectio-prior"` draft under the `lectio-prior` attempt operation. It sees
the images and no Testimonia; it is not Lectio nuda and cannot establish text. By
default (`off`) no Pass A is read: the reader sees the image and every witness
in one call, and the act makes no `lectio-prior` record. Under `saved` the draft is
made and kept as a training witness: the `lectio-prior` record's `protocol.blind_read` reads
`saved`, it is found under stage `4_perlector`, kind `lectio-prior`, it is never exported as a
reading, and no establishing reading, dossier or input references it. The production
`kind="perlectio"` is `lectio_kind="primed-with-prior"` only when the draft was fed
and then carries equality-only `self_revision` spans against it and the Pass-A
reference. When the draft was withheld (`off` or `saved`), it is `lectio_kind="primed-draft-withheld"`
with an empty `self_revision` and no prior reference. Both production kinds can
establish text; the kind records what the reader saw.

The optional `kind="primed-without-prior"` control is gated by the run-sealed
Perlector instrument rate and typed approval record.
`perlector-prior-draft-instrument-design.v1` denotes this exact experimental
condition: an act-level primed control that sees testimony but not the Pass-A
draft, selected by `digest-threshold-over-frame-page-seed-act.v1`. Its digest
draw uses corpus frame, page, seed, and act identity; it never uses a run
identifier. Changing that condition or rule requires a new subject; the record's
`target_version_hash` separately binds the run's exact sealed configuration,
including the rate, selector, and Perlector protocol bytes. The resolver and
publisher apply the same sole-subject/action/version and cross-arm refusals as
the nuda arm. It is a control artifact and cannot establish. The control and
prior are separately tallied when failed; they do not consume the ruled
production hard-failure cap. Its approval reference is likewise an envelope
input and is digest-checked whenever the control artifact is read.

The Pass-B dossier records whether the draft was `fed` or `withheld`. A fed dossier
carries a digest-checked reference to the Pass-A draft; a withheld dossier (under `off` or
`saved`) carries no `prior_draft` at all, and one that does is refused. The withheld dossier
of a `saved` run is the one an `off` run builds. The `--blind-read` default is `off` because a
fed draft anchors the reader; `blind_read` is sealed in the run's policy digest and in every
reading's `protocol` record, and Pass A counts in `calls_per_act` under `fed` and `saved`.

**Four reading kinds, three conditions.** `lectio-nuda` and `lectio-prior` are
built from identical dossier arguments — page context, no Testimonia, no prior
draft — so for one act they carry the same `dossier_digest` and the same
`rendered_sha256`. That is correct (they *are* the same condition) and it is
pinned by a test, because it is not visible from the kind names. Because they are
the same request, each arm is drawn under its own seed from `config/decoding.toml`'s
`[variance_experiment]` (`common.decoding.variance_arm_seed`: lectio-prior sends
`seed`, Lectio nuda `seed + 1`), recorded on its call record's `generation_sent`;
under one seed the two would be one draw and measure no variance. The seed is the
only thing the arm changes, and the only thing `VLLMReader` reads `pass_kind` for
beyond its membership check and the audit hand-off.

What each contrast measures depends on the mode. In a **fed** run (`--blind-read fed`),
nuda against lectio-prior measures sampling variance; lectio-prior (or nuda) against
the sampled control measures witness dependence, because the control sees witnesses
and no draft; the control against the production Perlectio measures anchoring on the
draft. In an **off** run there is no lectio-prior. In a **saved** run the lectio-prior is
an unprimed reading too, kept as a training witness and never shown to the production
reading, so nuda against it still measures sampling variance. In both, the control would
be byte-identical to production, so there is none (`--perlector-instrument-per-mille`
is refused unless `--blind-read fed`). In an off run the approval-gated sampled Lectio
nuda is the only unprimed reading, and nuda against the production Perlectio measures
witness dependence.

Under `saved`, a Pass A that fails on an engine, chair-response, transport or capacity
failure never costs the production reading: it is retained as its own failed record of
kind `lectio-prior` (the failed-Perlectio payload shape, with its call evidence as
inputs) and the establishing reading goes on, withheld as usual. Under `fed` a Pass A
failure fails the act as the production reading would, because the reading depends on it.
A contract or schema defect is fatal in every mode.

**Instrument arms and the hard-failure cap.**
`common/hard_failure.py`'s `PERLECTOR_INSTRUMENT_KINDS` covers `lectio-nuda`,
`lectio-prior` and `primed-without-prior`, so a failed instrument arm does not spend
the ruled production hard-failure cap. The cap is a circuit breaker on the production
reading path; an instrument arm must not halt a run over a measurement nothing
downstream consumes. Such failures stay visible in the tally's `instrument_by_kind`
and on the orchestrator's checkpoint line.

## R5b Pass-C audit, and the request the reader actually receives

Pass C is one deterministic flag pass over a page's frozen Pass-B semi-finals,
followed by at most one re-proof per act, scoped to the flagged locations —
which for the `within-crop`, `date-sequence`, `numbering` and `order` classes
is the whole act (`audit.py` emits `[0, len(text))` for those), so "span-scoped"
without that caveat would overclaim. The chain is three
records, and `common/perlector_audit.py::validate_chain` is the single
cross-record validation the producer and the Recensor both run:

```text
kind="audit-draft"    {act_key, attempt_ordinal, semi_final_text, page_ids,
                       round_cap, policy, flags, flag_location_basis}
kind="audit-finding"  {act_key, attempt_ordinal, page_ids, round_cap, policy,
                       flags, change_record, uncertain_spans, unresolved,
                       examination, reproof_truncation, reproof_call, reproof_edits}
payload.audit         {draft_ref, finding_ref, finding_digest, unresolved,
                       examination, reproofs, request_digest}
```

The sealed policy schema is `perlector-audit.v3`. `examination` is one of
`not-due` (no flag), `cap-exhausted` (flags, cap 0), `complete` (a re-proof was
delivered and its call ran to completion) or `incomplete` (delivered and the truncation
instrument did not classify its call complete -- the engine reported `length`, gave no
stop word, or the returned text carried all three of the instrument's own cut-off signals). `reproof_truncation`
is the truncation instrument run over the re-proof's own text and stop word, or
`None` where none was delivered; it carries the same closed `measure` block every
truncation record does, its classification is re-derived from its four sealed
signals by every validator (`common/perlector_audit.py::truncation_classification`, the
one rule `truncation.classify` also decides with), its `length_suspicious` is re-derived
from that block by the same surface (`length_signal`), and its character count is bound
to the re-proof text the finding is validated against. `reproof_call` names the retained
call record and raw response that termination was measured over (`None` for the fixture
chamber, which has no engine), so the verdict can be checked against the response
itself. `unresolved` is derived from `examination` alone:
`cap-exhausted` and `incomplete` are unresolved, and the Recensor holds on either,
naming which. **Text equality plays no part.** A v1 record equated "unresolved" with
"flags and a zero cap", so a re-proof cut off by its engine that returned the frozen
text byte for byte was sealed as resolved and its act delivered under a `complete`
aggregate. Consumers refuse a v1 record by
name (`RETIRED_SCHEMAS`) rather than read it forward; the act is re-read in a new
run and the old bytes stay as written.

The flags are computed once per page, before any re-proof result exists, so no
result can reopen the calculation. Each response edit must repeat one exact
requested location and its frozen original text; `change_record` retains one
row per changed edit, with that edit's flag class. An edit outside the requested
locations is refused before any final text is assembled. Nothing in this pass
selects among witnesses: the request carries no witness identity, no witness
text and no ranking, and the prompt is
byte-identical for every flag class. A `testimony-diff` flag's *location* is
witness-derived, though, and now that the instrument is actually delivered the
reader is directed to the exact spans where it disagreed with witnesses while
the tree measures movement toward them — whether that is compatible with no
step picking among witnesses and with nothing in the prompt steering the
reader's answer is an open interpretation question routed to the project lead
with the Tier-0 reproof change, not settled by this sentence.

**The re-proof plan is a delivered instrument, not a claim about one.** One
function, `perlector_audit.reproof_plan`, turns the frozen flags into one
neutral, location-only prompt each; `audit_request` wraps that plan into the
closed object the reader is handed:

```text
audit_request = {schema: "perlector-audit-request.v2", act_key, attempt_ordinal,
                 draft_ref, semi_final_text, reproofs}
reproofs      = [{class, location: {start, end}, prompt}, ...]   # non-empty
```

`draft_ref` is the published audit draft's reference *and* the digest of its
bytes, so the request names exactly the frozen semi-final its offsets index
into. `reader.read` takes it as its own `audit_request` argument beside the
unmodified Pass-B dossier and the act's delivered pixels; `payload.audit.
request_digest` is `digest_of` that request, and `validate_chain` rebuilds the
request from the draft it reads back and requires the digest to match. Sealed
plan, delivered instrument and later recomputation are therefore the same
function over the same frozen flags.

`request_digest` is `None` **exactly** when no request was delivered — an act
with no flags, or one whose sealed `round_cap` is spent (which still seals its
plan, because the exhausted-cap `uncertain_spans` point at those locations).
"No re-proof ran" and "a re-proof ran and confirmed this span" are different
recorded facts, and `reproofs` alone could not tell them apart. The rendered
request is deliberately derivation-only — not stored as a fourth artifact —
because every field re-derives from the published draft (`validate_chain` does
exactly that), and a second copy of the frozen text would be a second thing to
drift.

**Neutrality and the `pass_kind` rule both hold, in the same mechanism.** Every
prompt in a request and in the sealed copy must equal `neutral_prompt` for its
location exactly — not merely avoid forbidden words — so nothing can tell the
reader which way to argue. And because the instrument travels
as input, a reader still may not condition generation on `pass_kind`: a
re-proof pass arriving with no request is refused by
`reader.validate_audit_delivery`, as is a request delivered to any other pass,
or one naming a different act than the dossier beside it. `FixtureReader`
branches on the request, never on the pass label.

This is the Tier-0 repair of a finding in the re-proof plan. Before it, Pass C computed
the plan, sealed it under `payload.audit.reproofs`, and then called `read` with
the Pass-B dossier plus a spliced `semi_final_text` — no flags, no locations,
no prompts, and a `dossier_digest` that no longer covered the object carrying
it. A changed final text was published as the result of a measured, neutral,
span-scoped re-proof that was never presented. `test_audit_pass.py::
test_the_reader_receives_exactly_the_reproof_plan_the_perlectio_seals` captures
the real reader call and requires exact equality with the sealed plan; it is the
test that fails if the two ever part again.

**The current re-proof response is a set of exact edits.** Each edit names one
requested flag class and location, repeats the original text at those frozen
offsets, and supplies its replacement. The validator assembles the final text
from those edits and re-derives `change_record` directly from the changed rows.
It refuses missing, extra, overlapping or wrongly anchored edits. A fixture's
older whole-text proposal is converted to this protocol only when its change
fits one requested location; otherwise the response is refused.

## Live reader

The stage reads through `VLLMReader` (`live_reader.py`) behind one `ChairClient`
(`operations/serving/client.py`) whenever the sealed serving-recipe row for the
resolved Perlector chair is a `kind = "vllm"` row. Everything below is offline-proven
against `operations/serving/fakes.py` in `test_live_perlector.py`; none of it has met a
card.

**The selector is the sealed catalogue, never a flag.** `perlector_serving_mode` asks
`serving_mode_for` for the `(serving_recipe, chair, tier)` row in the catalogue named by
`--serving-recipes-config`, whose digest is already inside `config_digest` through
`serving_config_inputs`. No new configuration key was added and none is planned:
`operations/serving/assembly.py` refuses catalogue or placement bytes the run did not seal,
so the posture cannot be moved after the run was bound. `--placement-tier` must be
supplied beside a live catalogue and is deliberately *not* sealed — it is a measured
runtime fact of the card, and the receipt records the caps that actually bound the
serving moment. An absent chair is fixture without consulting the catalogue: an absence
has no resolved identity to look a row up by. `main(registry_factory=…,
serving_factory=…)` are dependency seams only; neither makes a run live or fixture.

**Stop reason, verbatim, and where it stops the pass.** `"stop"` and `"length"` reach
`truncation.classify` as the reader protocol's own two words; an absent `finish_reason`
arrives as `None` and classifies `unknown`, which holds. Any other engine string —
`"abort"`, a vendor's own vocabulary — raises `EngineSignalRefusal` from `live_reader`
and stops the pass with no Perlectio published for that act. Nothing is lost: `ChairClient`
retains the raw response before it parses, so the bytes that stopped the pass are on
disk under their own digest and the refusal names them. The same refusal covers a body
that is not a reading at all (`parse_problem`): a Perlectio has no `failed` shape —
`outcome="failed"` is produced nowhere in `run.py` — and minting one here would invent a
record kind this section does not own. Whichever arm the refusal lands on, the arms that
already published stay on disk as that attempt's evidence, and the act cannot be resumed —
see the live-resume section below.

**A declared reading failure never reaches a live chair's answer.** `declared_failure`
stands in for a real engine's own report exactly once, before there is one — the
fixture's own docstring says so. In live mode a real report is always coming, so the
establishing pass refuses before it ever asks: the check
(`serving_mode == "live" and declared_failure is not None`) sits immediately after the
live-resume skip, ahead of every reader call, chair start, and publication, leaving the
tree exactly as the invocation found it — no orphaned `lectio-prior`, no engine call
spent on a reading that would be discarded, and no opaque `IncompatibleReuse` on the
operator's retry. It refuses this way rather than letting a declared `no-readable-text`
blank real transcribed ink, or a declared `truncated` overwrite a real `complete`, which
would be a declared value standing where a measurement belongs. The
guard is one branch that never executes in fixture mode, so it does not move the
acceptance pin. Proven end to end in
`test_live_perlector.py::test_a_live_pass_refuses_a_fixture_declared_reading_failure`
against a run tree sealed under `no-readable-text-reading` throughout — a live pass
cannot honestly mix a `happy`-sealed run tree with a different Perlector-only scenario,
because `config_digest` binds the scenario too, so the test builds its own chain rather
than pointing a `happy` run at a different `--scenario`.

**Real ingress.** The stage opens through `common.stage.open_stage_context`, which
decides the route from one read of the run authority and, on a real submission, carries
the registry, the sealed digest map and the serving configuration inputs this stage
requires before its first line of work (`decoding`, `perlector-protocol`,
`perlector-audit`, and `bound_serving_recipes`). The route is read off `context.run`
(`real_ingress`), the same reading `common.stage` makes for `expected_acts`. Two fixture
concepts have no real-mode counterpart:

- `reading_failure` -- `declared_reading_failure` answers `None` on a real run, by name,
  not by an empty table. A real reading's non-completion is the engine's own stop reason,
  reaching `truncation.classify` through the Section A mapping above; no declaration
  stands in for it, and the live guard that refuses a declared outcome beside a live
  answer is therefore unreachable there.
- the fixture reader -- `fixture_reader_for` refuses a real submission whose sealed
  serving-recipe row for a configured Perlector chair is not live, because a declared
  text cannot stand in for a reading of real ink, and the catalogue is sealed at the Door
  so the repair is a new run. An absent chair reads nothing and needs no reader: every act
  publishes the same explicit `not-run` record it does on the fixture route. The selector
  is still the sealed row, never a flag, and the fixture route constructs its reader
  exactly as before.

The context's fixture slot is `None` behind a refusing accessor and is not touched on
the real route. No real Attestatores seal exists yet, so a real run refuses at
`predecessor attestatores has no stage-seal` with its context already opened, writing
nothing; `pipeline/5_recensor/test_recensor_real_ingress.py` pins that for this stage beside the
Recensor and the Archetypus.

**`engine_call`, and what it names.** A live reading's payload carries
`engine_call = {call_record_ref, raw_response_ref, response_sha256, finish_reason,
served_model_id}`, and the envelope binds both blobs as direct inputs, re-derived from
disk and compared to what the reader claimed. The field names *the call the published
text came from*: on an act whose Pass-C re-proof changed the text, it moves to the
re-proof's own call, beside `truncation` and, when the draft was fed, `self_revision`.
A re-proof reading that ran and changed nothing is still
bound as an input — it is the second thing that looked at this act's pixels and it is
what the `change_record` reports on — but it does not become the named call. The field
widens the closed field set for the record that carries it (`with_engine_call`, the
`_NOT_RUN_CAPACITY_FIELDS` precedent) rather than becoming optional inside one set. A
`FixtureReader` result never sets it.
`engine_call_inputs` refuses a malformed `engine_call` by name — the wrong key set, or
`response_sha256` disagreeing with `raw_response_ref["sha256"]` — rather than raising a
bare `KeyError` or publishing two digests for one response.

**`distinct_refs` narrows what this stage *expects*; it never widens what a record
may claim.** The envelope refuses a repeated path outright, even at an identical digest
(`validate_input_refs`) — that is the double-count guard that keeps one reading per act
in every export, and nothing here touches it: a duplicate inside a published `inputs` list still
reaches that refusal unchanged. It is used at exactly two seams, both of them places
where one content-addressed blob is honestly reachable by two names.

- *The Pass-C publication path* dedups only `reproof_inputs`, and only against
  `row["inputs"]` (the image, testimonia, attachment and prior references, which can
  never legitimately repeat). A live re-proof answering with the same bytes as the
  establishing call content-addresses to the same path twice.
- *`validate_page_testimonium_record`* dedups the expectation it builds from a page
  record's `raw_response_refs` and its `native_capture`. A page whose partition was
  derived from the very bytes its own capture describes reaches one blob through both
  fields, and a page-edge overshoot finding is *required* to be traceable through
  `raw_response_refs` while the capture still names it (`common/native_witness.py`). The
  producer names it once (`pipeline/3_attestatores/run.py::_named_once`), so
  concatenating the two fields here built an expectation no publishable record could
  meet — a correct record, correctly published, refused one stage later. Unreachable
  while no page witness parsed live; reachable the moment one did.

**A page witness attaches on one of two bases, and the label is derived, not read.**
`act_attachment_view` re-derives both through the one shared rule the producer used
(`common/contracts/outcomes.py::page_attachment_basis`): `geometric-overlap` where the
chair's reported observations cover the act's sealed regions on that page, and —
only where it reported no such ink — `anchor-line`, where the chair's page text carries
an alignment that located this act's own anchor line. *Located* is measured, not
inferred from an aligned status: the alignment's `anchor_line_match` must show a
contiguous run of this act's anchor line at least `ANCHOR_LINE_RUN_FLOOR` characters
long (or the whole line, where the line is shorter). A positive-length span was the
earlier test and was not one — the aligner keeps every matching block of a single
character, so a witness whose text had nothing to do with the page attached on two
coincidental characters and counted toward the floor.
The second basis is what a grammar that publishes no coordinates at all (Churro's, by
vendor design) can reach; without it every such chair was unattached at every act and
every act stood one witness under a floor of three on a shortfall that had not happened.
Geometry takes precedence when both hold, because the label is evidence about
independence: `anchor-line` says this chair counts here only because *another* chair's
anchor located its text. The exact derived label is required, never membership in the two
admissible ones, and an entry disagreeing with the derivation is refused on every
contributing page.

**A page witness on a continuation page is judged on its alignment, not on `attached`.**
The anchor-line basis cannot arise there — the anchor is derived from the act's own
primary page and the producer forces `continuation-page-no-act-anchor` before geometry is
consulted — so a continuation entry attaches by geometry or not at all. This reader used
*also* to refuse any continuation-page entry that was attached, and the two rules
contradicted each other for
a served page witness whose block really does cover an act's continuation half:
`false` was refused as not derived from geometry, `true` as claiming an anchor, and no
honest record existed in either state. The second rule is gone. What a continuation page
genuinely lacks is an *anchor* — it is derived from the act's own primary page, so there
is no comparison view to compute here even when the geometry attaches — and the
surviving rule says exactly that, refusing any alignment other than
`{"status": "unaligned", "reason": "continuation-page-no-act-anchor"}`. An
attached-but-unaligned entry is a shape the branches below already admit: a
`geometric-overlap` basis, a null span, an explicit unaligned reason. The contradiction
was unreachable while the fixture declared no geometry on a continuation page
(`pipeline/3_attestatores/CONTRACT.md`).

**The receipt is the live one.** `provenance_for(..., receipt_ref=…)` takes the receipt
the serving manager published and `ChairClient.__enter__` re-read through the tree and
matched to this chair and revision. Fixture mode passes nothing and writes the declared
`fixture_serving_details` receipt exactly as before; minting one of those beside a
reading a real engine produced would put a declared value (`fixture://`, dtype
`fixture`) where a measurement belongs.

**One chair, started late, stopped before the seal.** The client is entered on the first
act that actually needs a reading, so a resumed pass whose acts are all sealed never
loads a model onto a card that bills by the hour. `ResidentChair` owns the shutdown:
`_read_the_acts` closes it before `seal_boundary`, so a failed shutdown is never
reported over a sealed stage, and `main`'s `finally` catches every path that raised
first. A `ServiceStopError` propagates — an unverified shutdown is fatal. This ordering
is pinned by `test_a_failed_chair_shutdown_stops_the_pass_before_the_seal_is_written`,
which wraps the injected client so `__exit__` raises after really shutting the fake
service down, and asserts the run tree's `stage-seal` directory stays empty — a
mutation probe deleting the `service.close()` call ahead of the seal left the rest of
the module green before this test existed.

**The live resume rule.** An act whose `perlectio` already exists at
`perlegere:<ordinal>` is never asked again (`_reading_already_sealed`). Fixture readers
are deterministic, so a resumed fixture pass republishes identical bytes and the store
reuses them (`_next_attempt`'s docstring); a live chair cannot promise that, and the
store refuses the collision. Skipped acts are counted apart from `read`, because this
invocation did not read them.

**Live resume never asks again about a reply it received.** A live pass keeps two
records that exist for resume, beside everything else it publishes:

```text
kind="reader-sent"  {schema: "perlector-reader-sent.v1", act_key, attempt_ordinal,
                     pass, send, receipt_ref, concurrency, image_sha256s}
kind="semi-final"   the act's Pass-B payload: the Perlectio field set without `audit`
```

A `reader-sent` record is published on the main thread before an act's calls leave:
`pass` is `reading` for the main pass (Pass A, the sampled arms and Pass B, sent as one
job) and `audit-reproof` for its re-proof. `send` numbers the sends of one pass of one
attempt from 1, and a later send binds the earlier ones as inputs, so a call re-sent
after an interruption is on the record, never silent. `receipt_ref` names the serving
session that sent it (also bound as an input), `concurrency` the width of the window it
was sent in (below), and `image_sha256s` the images every call about the act carries, in
the order sent. The outcome is `read`, as on the audit records: the envelope's closed
vocabulary has no word for a request.

The `semi-final` is published, in act order, as soon as the act's main-pass calls
return, after its sampled arms, at `perlegere:<ordinal>`, with the outcome Pass B
resolved. It binds the act's reading inputs and every `reading` send; the audit draft,
the Perlectio and a re-proof failure then bind the `semi-final` in turn, so every
reading reaches its sends through its own inputs. A failed Perlectio after it must bind
it and must be the re-proof's (`common/perlector_failure.py`). Both records are live-only:
a fixture resume republishes identical bytes, so the fixture tree is unchanged.

Before any chair starts, `_acts_left_to_read` sorts every act that has no Perlectio:

- with a `semi-final` and no audit artifact, it is **adopted**: `_adopted_row` rebuilds
  its audit row from the record without a call, after re-deriving the record's inputs
  from the act's current evidence and refusing any difference. Only its re-proof, if
  due, is sent. An adopted re-proof sent in a later session names that session's
  receipt on its own call record; the Perlectio's `provenance` stays Pass B's, and a
  re-proof failure carries the failing session's provenance and binds the `semi-final`
  that carries Pass B's;
- with nothing, it is untouched and read;
- with `reader-sent` records and no reply on record, the call was in flight when the
  pass stopped. It is sent again, as the next `send`, only if no retained reply could be
  its answer. The client retains a reply's raw bytes first and then the call record that
  names them, so a reply is looked for in both forms (`_unrecorded_replies`): a call
  record that carries a reply (`raw_response_ref` set) and that no record of this stage
  binds, from one of those sends' sessions and for the act's images, refuses the act;
  and any stage blob that is not a call record, not a reply a call record names, not
  serving evidence, not a page render (a PNG; a chat reply is never an image) and not an
  input of some record is a reply nobody can attribute, and
  refuses every act with an unanswered send. A reply some record binds, of this act or
  of another act with the same crops, is on record and refuses nothing;
- with sampled arms but no `semi-final`, or with an audit draft or finding but no
  Perlectio, a reply was received and only partly recorded; the pass refuses. That
  includes a `saved` or `fed` blind read stopped during its Pass B: its `lectio-prior`
  is already published, and those pages are read in a new run.

An adopted `semi-final` must also have been made under this run's `config_digest` and
reading protocol (the blind-read setting included), and name exactly the act's current
region basis; the run policy itself is compared with the sealed run when the stage
opens. A refused act's records stay as that attempt's evidence and its pages are read in
a new run. The page flags of Pass C are computed over every act's semi-final, as in an
uninterrupted pass: the semi-finals of acts this pass found sealed are read back from
their records and validated, and a sealed live reading with no `semi-final` is refused
as `FatalAccounting`, because its page's flags could not be computed over every act. A per-act failure the pass can name (`_ACT_LOCAL_READING_FAILURES`)
publishes a failed Perlectio and is not half-read; that includes a request the capacity
check refuses before sending (`request-capacity`), and a transport failure such as the
pod going away. Pinned in `test_live_perlector.py` by
`test_a_resume_adopts_every_main_pass_reply_on_record_and_asks_nothing_again` (stopped
by an interrupt or by the deadline, at widths 1 and 2, the resumed tree is byte-identical
to the uninterrupted one),
`test_a_call_interrupted_in_flight_is_sent_again_and_the_second_send_names_the_first`,
`test_a_reply_retained_but_named_by_no_record_refuses_the_resume`,
`test_a_reply_retained_before_its_call_record_refuses_the_resume`,
`test_a_reproof_interrupted_in_flight_is_sent_again_only_if_no_reply_came_back`,
`test_a_reply_another_record_binds_answers_no_send`,
`test_a_semi_final_made_from_other_evidence_is_refused_not_adopted`,
`test_each_record_names_the_session_that_made_it_after_a_resume` and
`test_a_live_pass_refuses_to_resume_an_act_it_left_half_read`, and downstream by
`pipeline/test_live_reading_seam_e2e.py::`
`test_a_pass_stopped_mid_reading_resumes_without_asking_again_and_the_tail_accepts_it`.

**The reading deadline.** `--reading-deadline <UTC ISO time>` makes a live pass refuse to
start when the chair's `startup_timeout_seconds` plus every call left
(`calls_per_act` = the one establishing call + one for Pass A when the draft is fed
+ the audit round cap + one per instrument arm enabled for the run, at
`PLANNED_SECONDS_PER_CALL`) would run past it. It also refuses to begin
another act, or another re-proof, when the calls left would. It stops between calls,
never inside one; every act already sent is finished and its `semi-final` published, so
a resumed pass adopts them (see above). Pinned by
`::test_a_launch_the_reading_deadline_cannot_cover_is_refused_before_the_chair_starts`.

**Concurrent calls.** A live pass keeps up to `--perlector-concurrency` acts unfinished
at once (default and ceiling: the served row's `max_num_seqs`), so the engine can batch
their reader calls; the orchestrator forwards the flag and journals it, it is not sealed,
and the stage prints the width it used. Each act is prepared on the main thread, and every
record is written there strictly in act order, exactly as a serial pass writes it: not-run
records, arms, audit drafts, findings and Perlectiones alike. Only the calls overlap, and
no act's request carries another act's reading. A failed call is that act's failed
Perlectio alone. If preparing an act, a call or a publication raises, every act already
sent is still finished in order before the error stops the pass, so no reply is left
without its record. An interrupt stops waiting at once so the chair can be shut down; it
first finishes every act whose reply has already arrived, even behind an earlier call
still out, since a record's bytes do not depend on the order it was written in. A
`reader-sent` record is written when its call leaves, so in a batch it precedes an
earlier act's records. Pass-C re-proofs share the same window. A blind-read (`fed` or
`saved`) or fixture pass reads one act at a time: Pass A, or its failure, is published
inline before the establishing call. The deadline is checked before each act is prepared,
against the serial estimate, which stays conservative for batched calls. Because the check
runs before an act is prepared, up to `width` calls may already be in flight when it fires;
they are finished, not cut off (the finish-sent-acts rule above). Work past the deadline is
therefore at most those calls, which overlap: about the slowest one (40 s on the planning
estimate, `PLANNED_SECONDS_PER_CALL`), and never longer than one request's hard limit,
`request_timeout_seconds` in the serving recipe (600 s on the real rows). That time is
billed. A batched reply
can differ from an unbatched one in low-order bits, so its sampled tokens can too; each record holds
the reply its call received, and each `reader-sent` record carries the width its call
was sent under as `concurrency`, so two runs' readings can be told apart from the tree
alone. It is the window's width, the most calls this pass kept in flight; the batch the
engine actually decoded a call in was at most that. It sits on the send rather than in
`provenance` because provenance names one serving session while an adopted act's
re-proof may run in another; each send names its own session and width.

**One live-resume limit remains, named rather than hidden.** Every re-invocation of a live
pass starts and stops the service, so an `--act` recovery loop pays a full model load per
act (`pipeline/orchestrator/run.py`'s per-act dispatch). Serving policy permits a server that
outlives one stage, but no cross-process handle exists; that is the next serving item.

**A response refusal exits in this stage's own vocabulary.** `ChairResponseRefusal` is a
`ServingError`, which is a `RuntimeError` and not a `ContractError`, so `run_stage` does
not catch it: vLLM's 400 explaining a context overflow — the likeliest first answer from a real
card — would otherwise end in a Python traceback and exit 1 rather than a named refusal and
`EXIT_FATAL`. `main` translates it at the stage boundary and nowhere earlier: `ChairClient` still
retains before it refuses, `live_reader` keeps its posture, and the refusal's code and the
engine's own sentence travel verbatim into the message the stage exits on. The clause takes
the whole class, so the wrong-model refusal raised before any parse arrives the same way —
every `CHAIR_RESPONSE_*` code is one way an answer failed to be a reading. Its sibling
`ChairRequestRefusal` is deliberately not caught: that one says this stage built a request
that may not go on the wire, which is a defect in this code rather than an account of the
run, and a traceback naming the construction site is worth more there than a named exit.
Pinned by `::test_a_non_200_from_the_engine_stops_the_pass_in_this_stage_s_exit_vocabulary`.

**Decoding.** Every Perlector request samples at the Perlector's row of
`config/decoding.toml`'s `chair_decoding`: Qwen3.8-27B's model card values for
non-thinking mode (`temperature` 0.7, `top_p` 0.8, `top_k` 20, `min_p` 0,
`presence_penalty` 1.5, `repetition_penalty` 1.0), with the thinking switch off
(`chat_template_kwargs = {enable_thinking: false}`). `ChairClient` selects the row
by its own chair from the sealed policy `main` loaded, and sends it with a seed: the
serving row's seed for Perlectio, `primed-without-prior` and the audit re-proof,
and the arm's own seed for lectio-prior and Lectio nuda (above). Seed, row and
`sampling_effective` (what the pinned vLLM samples under; these values pass through
unchanged) are on every call record. Every reading's call record is held to the
sealed row and its pass's seed as the reading is bound (`run.engine_call_inputs`,
through `common.stage.verify_retained_call_sampling`), the audit rebuild holds the
re-proof's to the row and the receipt's seed, and the failed-Perlectio contract
holds a failed call to the row and to the receipt's or an arm's seed, since a
failure does not name its pass (`common.decoding.verify_call_sampling`). A call
record from before this decoding is refused by its schema's name, including on a
resume, where it is never counted as an unattributed reply. A seeded request is reproducible in
intent, not bit for bit: a batched step can differ in low-order bits.

**`max_tokens` is sent, from the sealed decoding policy.** `perlector_generation` holds
one cap for every reading pass, so the passes stay one condition, and one for the
audit re-proof, which answers in JSON. The value sent is the smaller of the cap and the
context the admitted prompt leaves, and it rides `generation_sent` on the retained call
record; the cap itself is visible through the decoding digest. A reply that reaches it
comes back as an engine `"length"`, which the truncation classifier holds as a visible
failure of the act; nothing re-asks. A cut re-proof usually fails to assemble first and
is published as a failed Perlectio. The cap sits far above any honest reading and stops
a reply that has begun to loop from filling the context. Admission still reserves only
the act's or the dense page's measured answer, so the cap refuses no act that fits.

**Proved end to end.** `pipeline/test_live_reading_seam_e2e.py` reads a tree the
Attestatores wrote through three *live* witness chairs — every record on it
carrying `native_capture`, `serving_call_ref` and `raw_response_kind`, which no
fixture record has — and then hands what this stage publishes to the Recensor,
Archetypus and Armarium. Nothing here refused it: every act reaches a Perlectio
whose `engine_call` names a retained blob holding exactly the bytes the engine
put on the wire, the live receipt is on every reading's provenance, and the run
seals a terminal export. The export is held for review rather than delivered,
for a witness-coverage reason recorded in `pipeline/3_attestatores/CONTRACT.md`
and not for anything this stage did. The same driver in fixture mode reproduces
the orchestrator's own tree byte for byte, `--placement-tier` supplied, which is
the fixture-path claim `with_engine_call` and the mode selector rest on.

## Page reading

A run sealed with `reading_unit = "page"` in `config/perlector_protocol.toml` reads
whole pages instead of Designator acts (`page_run.py`, called from `run.py`'s
`_read_the_acts` before the act loop, which it replaces). Stages 2 and 3 run as
usual; this stage reads every Exemplar page (`common.stage.exemplar_page_ids`),
not only pages with a Designator act, once, and the Perlector establishes the acts
on it. The Recensor reviews each unit it established
(`pipeline/5_recensor/CONTRACT.md`, "Page-read review").

**Refused at stage open, by name:** a sealed `blind_read` other than `off`, and a
non-zero `nuda_per_mille` or `perlector_instrument_per_mille`. **Recorded, not
refused:** the sealed Pass-C audit policy. Pass C flags and re-proves acts read one
at a time, so it does not run here; every `page-reading` carries
`audit: {state: "not-run", round_cap, policy_sha256, reason}`, so an ordinary sealed
run (committed `round_cap = 1`) can read pages.

### Inputs

- Each page's current `page-testimonium` per chair (`latest_per_chair`), every one
  validated as the act path validates it (`validate_page_testimonium_record`, and the
  native capture's blob and adapter). Every chair the sealed roster scopes `page`
  must have one; a chair it does not scope `page` must not. Act-scoped witnesses
  have no page Testimonium and are not shown. A page with no page Testimonium at all
  (the Attestatores serve only pages with a proposed Designator act) is not refused:
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
  `block_sequence_reason`; named so because the dossier sweep refuses any key naming
  an order), and for a raster fallback the prompt says the blocks are in raster
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
pixels: the feed's `box_px`, an act-region's `union_box_px`, and every box the
accounting takes or reports. `common/page_accounting.py` reads corners from them
internally and nowhere else.

### Records, per page, in publication order

Every sealed page's feed is built and published before any page is read, so a live
pass counts exactly the pages it will send before its chair starts.

`kind="page-feed"` (subject page_id, no attempt, outcome `read`): the
`perlector-page-feed.v1` payload exactly as `page_feed.build_page_feed` returns it,
with `witness_testimony` (`present` or `none`) and `prompt` null when the Perlector
chair is absent or the feed shows nothing (`page_feed.shows_nothing`). Its inputs are
every Testimonium of the sealed page-witness roster -- a witness the `witnesses`
switch hides included, since the accounting measures it -- every Surya record, the
page render and the sealed page it names, each re-derived from the bytes on disk.

`kind="reader-sent"` (subject page_id, live only): the existing closed record with
`act_key = "page-<ordinal>"`, `attempt_ordinal = 1`, `pass = "page-reading"`, and
`image_sha256s` the page render then the overlay, in the order sent.

`kind="page-reading"` (subject page_id, attempt `attempt_id(page_id, "page-read", 1)`):

```
{schema: "perlector-page-reading.v1", page_id, page_ordinal, reading_unit: "page",
 feed_ref, request_digest, engine_call | null, sampling | null, capacity | null,
 finish_reason, stop_reason, parse_state, answer | null, problems: [{code, detail}], failure | null,
 disposition: "read" | "held", audit, provenance}
```

- `sampling` (live calls only; null on the fixture pass or when nothing was sent):
  `{chair: "perlector", sent, effective}`, the Perlector's sealed `chair_decoding`
  row that `ChairClient` put on the wire and the values the pinned engine samples
  under (`common.decoding.engine_effective_sampling`), in the call record's form
  (`common.decoding.recorded_wire_decimals`). The page call goes through the same `ChairClient` as an
  act reading, attempt 1, no variance arm, with the serving receipt's seed; its call
  record is held to that row and seed (`verify_retained_call_sampling`) wherever
  stage 4 binds the reading's `engine_call`: when the reading is published, when a
  resumed pass adopts it, and when its act records are published. The row samples
  (Qwen's non-thinking values, temperature 0.7), so a second call would be a
  second draw; nothing on the page path asks twice.
- `parse_state`: `parsed` (the grammar read; `answer` is the object as given),
  `malformed` (`common.page_answer.parse_page_answer`'s problems), `cut-off` (engine
  `length`; `answer` null, never parsed), `refused-capacity` (nothing sent),
  `call-failed` (a page-local engine or transport failure; `failure` is the act
  path's failure record and its retained response and call record are inputs),
  `not-run` (nothing asked; `problems` names every reason: `page-not-sealed` -- the
  Exemplar refused the page, and `feed_ref` is null since there is no feed --
  `chair-absent`, `no-witness-testimony`, `nothing-to-show`).
- `disposition` is `read` only for `parsed` with no problem; outcome is `read` or
  `held` accordingly. A parsed answer is read by `common/page_accounting.py`'s
  `validate_answer` against `feed_candidates`, the feed's ids placed by
  `placement_boxes` -- the one placement map, which the accounting measures against
  too. The answer grammar is `common/page_answer.py`'s alone (one label rule: absent,
  null, or non-blank text of at most 80 characters); the accounting calls it rather
  than keeping its own. Any problem but `duplicate-region` holds the page with its
  answer and problems (`unknown-id`, `malformed-range`, `cited-and-set-aside`,
  `set-aside-twice`, `set-aside-without-reason`, ...). A parsed answer whose engine
  gave no finish reason (`stop_reason` null) is kept and held with
  `no-stop-reason`. Two entries sharing a union box are published, both held.
- `request_digest` = digest of `{image_sha256s, text_sha256}` of what was (or, in
  fixture mode, would be) sent; null when nothing was.
- `capacity`: live only, `common.request_capacity.page_request_capacity`'s
  `{capacity, answer_reserve, max_tokens}`, checked against the sealed serving row
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
act record: `common.page_accounting.page_accounting`'s `page-accounting.v1` payload
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
{schema: "perlector-act-region.v1", page_id, page_ordinal, reading_unit, n, kind,
 label, cites (as given), cited_ids (expanded, first-cited order), act_class,
 page_reading_attempt, union_box_px | null, region_id, image_path, image_sha256,
 transform, transform_digest, page_reading_ref, page_accounting_ref, feed_ref, holds,
 page_holds}
```

- `act_id = act_id(page_id, act_class, {page_reading: <attempt>, n, union_box_px})`
  (`common/contracts/identities.py`, classes `reading` and `reading-unplaced`).
- `union_box_px` is the union of the cited ids' sealed-page boxes as
  `placement_boxes` gives them (a witness shown `flat` places nothing), unpadded. The
  crop is cut from the sealed Exemplar by the Designator's own crop path
  (`common.exemplar_boundary.cut_exemplar_crop`): `transform` is the closed crop
  transform, `region_id = region_id(act_id, transform)`, and the crop blob is an
  input.
- `holds`: `reading-unplaced` (no cited id places: no crop, every crop field null,
  class `reading-unplaced`), `duplicate-region` (another entry has the same union
  box; both held), `no-autopsia` (no page image was shown).

`kind="perlectio"` (subject act_id, attempt `perlector_attempt_id(act_id, "perlegere", 1)`):

```
{schema: "perlectio.v2", page_id, page_ordinal, reading_unit, act_region_ref,
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
- `truncation` is `truncation.classify` over the union box's pixels against the page's;
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
adopted dissent under that budget and requires it exactly. Act-regions are deterministic and re-published
byte-identical. Before a live chair starts, a page it will send with `reader-sent`
records and no `page-reading` is sent again only when no retained reply could be its
answer (`_unrecorded_replies`, `_answers_a_send`); otherwise the pass refuses by
name. A fixture pass republishes identical bytes.

`--act` is refused under `reading_unit = "page"`: the Perlector names its own acts,
so there is no Designator act to read alone.

A later page-reading attempt (`page-read:2`, the re-ask Train 3 plans) is a new
attempt of the same page, so every act it establishes gets new act ids: `act_id`
binds the page-reading attempt, and the attempt has its own `page-accounting`, which
its act records name. Attempt 1's accounting and act records stay sealed beside them.
Whether a later attempt supersedes attempt 1 -- and how a consumer tells which
attempt's acts are current -- is not decided here; the Train 3 design must state it.

## Not built here

- Real serving on real silicon. What is proven offline: reader selection by sealed row
  kind, the stop-reason mapping and its refusals, the call record and its retained
  bytes, the live receipt on the record, the resume rule, and one chair started and
  stopped per pass — all against `operations/serving/fakes.py`. What still needs a card:
  readiness against a real vLLM process, the real builder's byte fidelity against a real
  chat template (`prompts.py` renders a declared template, and no tokenizer files are
  fetched here), whether vLLM emits an omitted `finish_reason` key or an explicit
  `null`, and every timing value in `config/serving_recipes_real.toml`, which is
  labelled UNMEASURED in the file for that reason.
- Reconciliation across several *independently produced* readings of one
  unrecovered attempt (spec_08's general "where it produces several readings, it
  reconciles them itself, against the image") is not modeled: nothing upstream
  produces two independent readings of one attempt today, only re-reads driven
  by Recensor's own bounded recovery loop (already never a pick).
- **No consumer refuses a Perlectio without a `dissent` record.** Perlector's closed
  `_PERLECTIO_FIELDS` requires it only when this stage seals a reading
  (`pipeline/4_perlector/run.py:1678`, sealed at `:2349` and `:3424`). Archetypus sets
  `dissent_ref` to the accepted reading's reference (`pipeline/6_archetypus/run.py:1633`)
  and checks that it equals `perlectio_ref` (`:873-876`); that proves reference
  identity, not the Perlectio payload. `accepted_primed_perlectio` checks the reading kind,
  explicit `primed` flag, salvage tier, regions, retained Testimonium basis,
  act-attachment view, and prior draft, but not `dissent`
  (`pipeline/6_archetypus/run.py:605-806`). The logical-act path validates a sibling
  cross-capture dissent artifact, not this record (`:1290-1345`). A reading without the
  dissent instrument could therefore still be established; closing that gap belongs in
  `6_archetypus`.
- A real vLLM launch declaration (pinned `--revision` and `--tokenizer-revision`,
  an explicit `--chat-template` rather than an ambient tokenizer default,
  unmerged `--enable-lora` with its base verified separately, a bounded
  readiness probe with named failure signatures) was built during this stage's
  second lane and is deliberately **not** carried here: it is spec 04's
  territory and the serving-manager branch's file, and two implementations of
  one serving path is the drift this contract exists to prevent. It is worth
  reading before that lane writes its own.
- Spec 10's `text_status` is now an Archetypus field, distinct from that record's
  fixed `status = "established"` literal. Archetypus re-derives it from the text,
  annotations, and uncertainty before accepting the record
  (`pipeline/6_archetypus/run.py:824-861`).
- **Pass-C can emit an audit `uncertain_span` under a zero cap.** Audit spans are minted
  exactly when `examination == "cap-exhausted"` (`common/perlector_audit.py::
  examination_state`), so they appear only when the sealed policy allows no re-proof
  round, not after a permitted round is spent -- and never for a re-proof that was
  delivered and did not complete, which is recorded as `examination = "incomplete"`
  with the call's own `reproof_truncation` beside it and routed to review on that
  fact (F1, above). Each non-empty frozen flag
  location then becomes a low-confidence `audit-round-cap-exhausted` span on the finding
  and Perlectio (the `if examination == audit.EXAMINATION_CAP_EXHAUSTED` loop in `run.py`'s
  audit pass, sealed into `finding_payload["uncertain_spans"]` and projected onto
  `payload["uncertain_spans"]`). A zero-width flag remains explicit in the
  frozen flags and `unresolved` state because it cannot become a span; Recensor routes it
  to review (`pipeline/4_perlector/test_audit_pass.py:1202`). **The committed policy
  cannot fire this path:**
  `config/perlector_audit.toml:12` sets `round_cap = 1`, so this exhausted-cap path
  cannot mint an audit span under the committed policy. Reader-supplied doubt spans
  can still populate `uncertain_spans` at cap one. A run sealed with `round_cap = 0`
  can also produce exhausted-cap audit spans; the
  focused assertions are in `test_raised_cap_needs_tyrels_reference_and_exhaustion_routes_review`
  (`pipeline/4_perlector/test_audit_pass.py:1170-1199`).
- **`gaps` and `uncertain_spans` have downstream consumers.** Archetypus validates the
  uncertainty and annotations before deriving `text_status`
  (`pipeline/6_archetypus/run.py:832-839`). Armarium independently re-derives it before
  projection, carries the checked status and transcription annotations into delivered
  entries, and carries the status into the aggregate
  (`pipeline/7_armarium/run.py:1186-1203`, `:1365-1376`, `:1413-1421`). The product still
  does not render canonical uncertainty inside `display:`; that presentation convention
  remains outside the implemented export contract.
- **Spec 08's contextual-suggestion flag is not built.** "A contextual suggestion (a year
  that must be 1805) may ride as a flag while the text stays what the pixels support" —
  the closed `_PERLECTIO_FIELDS` set has no field that could carry one, and nothing here
  produces one.
- **A truncated or unknown reading is held, not retried.** Spec 08 asks that such an
  attempt be "recorded, retried within the recovery budget, never accepted"; the Recensor
  routes it to `held-for-review` instead. Nothing is lost and no stale text is
  established — the safe half of the requirement holds — but the bounded retry the spec
  names is the Recensor's own file and has not been built.
- **The audit request is a structure, not yet rendered prompt bytes.** `request_digest`
  digests the canonical request object, which is the honest claim available offline:
  invariant #49's `rendered_sha256` needs a declared per-recipe builder, and
  `prompts.py` has none for a re-proof. The chair, serving recipe and builder digest a
  Pass-C call runs under are unchanged from the same act's Pass B and already recorded
  once on `payload.prompt`, so what was missing — and what `request_digest` now names —
  is the instrument's *content*. A real serving path registers a re-proof builder and
  binds its rendered bytes at this same seam; nothing about the record's shape has to
  move for it.
