# Perlector — handoff

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
successor correctly refuses the missing boundary. Every difference in decoders,
platform, machine, `decode_paths_used`, and `produced_pixels` is reported by
field or decoder name. A valid difference is report-only and never refuses;
Unit 17 owns any fatal policy.

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
primes a reading, never the ink the reading is established from (ARCHITECTURE;
GOVERNANCE 3), so a second look by a witness does not make a second reading exist,
and re-reading an act because a witness spoke again is the re-roll GOVERNANCE 11
refuses. The consequence for the upstream stage is that an act's witness layer
closes when this stage reads it, enforced at the Attestatores' own entry
(`pipeline/3_attestatores/HANDOFF.md`, "The one attempt model") rather than
discovered here as an immutability refusal on a reading identity nothing can move.

The count comes from `common/stage.py::recovery_region_count` — the shared reader
the other three stages ask — and the regions are read once, before the ordinal is
derived from them. A private `origin == "recovery"` comparison scored every
unrecognized origin as zero, so a resealed Designator tree could be read and
published here at the wrong attempt and become fatal only at the next stage, over
a Perlectio that is already immutable (Sol-S5). An unplaceable origin is now
refused here, before any model call or publication, and named for what it is: a
region whose place in the recovery denominator is unknown.

**A reading whose witness basis has since been superseded is not reconciled.** The
Recensor, Archetypus and Armarium each pass the current reading back through
`common/stage.py::require_current_witness_basis` before accepting, establishing or
exporting it, so a Testimonium appended after the reading was established cannot
be structurally invisible at the point where the export decides to say `complete`
(GOVERNANCE 2; audit Opus-F2, 2d).

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
                    source, so editing the builder changes the record even when
                    its name and every other field stay identical
dissent          -- derived-comparison-view rows (see below)
truncation       -- {classification, signals}, present on every attempted
                    reading regardless of outcome (see below)
uncertain_spans  -- [{start, end, alternatives, confidence}, ...]
gaps             -- [{position, start, end, witness_evidence}, ...]
audit            -- {draft_ref, finding_ref, finding_digest, unresolved,
                    reproofs, request_digest}: the R5b Pass-C chain, and which
                    re-proof instrument was actually delivered (see below)
provenance
```

`basis` is unchanged from the first landing and is what the three downstream
consumers actually read (`common/stage.py::reading_basis_regions` walks
`basis.regions`). Nothing here may ever remove or repurpose it.

The envelope's direct inputs bind every full-resolution crop, every downscaled
page-context render and its sealed source page, and every Testimonium reference.
The dossier is therefore not the sole claim that the reader saw its page context.

**The field set above is closed and checked before publication**
(`run.py::validate_reading_payload`). Three of the four failures spec 08's
schema test names — missing identity, missing dissent, missing regime record —
are *absent* fields rather than wrong ones, which is the failure a per-field
type check never sees. Agreement is one row per witness with no departure spans;
an empty `dissent` list is valid only for Lectio nuda, which was shown no
testimony. Omitting primed rows would make agreement indistinguishable from an
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

**Page renders.** One layout-context render per distinct page an act's regions
touch, stored content-addressed under this stage's own blob store. The long edge
is capped at `dossier.PAGE_CONTEXT_MAX_EDGE` (1024) rather than divided by a
fixed factor: a divisor is not a bound, and halving a 6000-pixel archival scan
hands the reader the page again rather than an overview of it. `transform`
records `{operation, source_dimensions, target_dimensions, maximum_edge,
resampler}` and `source` names the sealed page it came from, so the render is
reproducible from the Exemplar plus the record (ARCHITECTURE invariant 3) by
someone who does not also have this module. A page already inside the bound
records `resampler: "identity"` rather than reporting a resize that did not
happen.

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

Computed strictly after the reading is fixed (`dissent.py`), over a
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
alignment starts. It is **not**, on its own, a wall-clock bound: `SequenceMatcher`'s
cost on text that differs in many scattered places — exactly the shape a
systematically-mistaken witness produces — runs close to the *cube* of the
length rather than the square the pair count assumes, so a comparison well
under the pair bound can still run for minutes. `dissent.MAX_COMPARISON_SECONDS`
is the real backstop: a `SIGALRM` deadline around the alignment itself, so a
comparison that has not finished by then is abandoned rather than awaited.
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
`dossier`. `builder_sha256` digests the whole prompt module's source — not one
function's, because builders render through helpers — so any edit to the
prompt-building code renames every Perlectio it prompts rather than hiding
behind an unchanged recipe name. Deliberately module-scoped: a byte changed
anywhere in `prompts.py` moves the claim, including edits that do not change
the rendered bytes.

### `truncation` — the instrument, not an assumption

```text
{classification: "complete" | "truncated" | "unknown",
 signals: {stop_reason_declared, unclosed_structure, length_suspicious, ends_abruptly}}
```

Computed by `truncation.py` for every attempted reading, primed or nuda,
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
name a reader would then have to go looking for (GOALS 5).
`position` is one of `leading | internal | trailing | whole-act`, each with its
own bound (leading starts at 0, internal is strictly inside the text, trailing
ends at `len(text)`, whole-act requires
`text == ""` and is the gap's only entry). **Bidirectional**: an outcome of
`no-readable-text` requires exactly this whole-act gap, and a whole-act gap
forces the outcome to be `no-readable-text` — an outcome of `read` may never
carry one, which would otherwise let an empty text flow onward as though
something had been established (`annotations.py::validate_whole_act_consistency`).

A held act or unavailable reader receives an explicit non-completed Perlectio
with its reason, not a fabricated text. `truncated-reading` and
`no-readable-text-reading` (declared reading failure) and
`engine-truncated-reading` (declared engine stop-reason, no reading-failure
declaration at all — the detector's own authority) exercise the three paths
end to end; Recensor treats every non-completed outcome as a visible hold.

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
(off) for every scenario that predates this build.
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
unknown design measures nothing (GOVERNANCE 10). The same reference is an
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

Every readable act now emits a `kind="lectio-prior"` Pass-A draft under the
`lectio-prior` attempt operation. It sees the images and no Testimonia; it is
not Lectio nuda and cannot establish text. The production `kind="perlectio"`
is explicitly `lectio_kind="primed-with-prior"`, carries equality-only
`self_revision` spans against that draft, and is the only R5a reading kind the
Archetypus accepts.

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

The Pass-B dossier contains a digest-checked reference to the Pass-A draft and
records whether its text was `fed` or `withheld`. The `--draft-fed` default is
fed; B5a remains Tyrel's routed production decision.

**Four reading kinds, three conditions.** `lectio-nuda` and `lectio-prior` are
built from identical dossier arguments — page context, no Testimonia, no prior
draft — so for one act they carry the same `dossier_digest` and the same
`rendered_sha256`. That is correct (they *are* the same condition) and it is
pinned by a test, because it is not visible from the kind names. With a real
chair, nuda against lectio-prior measures sampling variance; the
witness-dependence contrast is lectio-prior, or the sampled control, against
the production Perlectio. Whether the approval-gated nuda arm still earns its
second model call once Pass A is universal belongs to B4's three-condition
matrix and to Tyrel — this build claims no answer.

**One thing about nuda did change, and it is not in the list above.**
`common/hard_failure.py`'s `PERLECTOR_INSTRUMENT_KINDS` covers `lectio-nuda`
as well as the two new kinds, so a failed Lectio nuda no longer spends Tyrel's
ruled production hard-failure cap; before this branch it did, because the
policy is written per (stage, outcome) and nuda is a Perlector artifact. That
is the right disposition — the cap is a circuit breaker on the production
reading path, and an instrument arm tripping it would halt a run over a
measurement nothing downstream consumes — and the failures stay visible in the
tally's `instrument_by_kind` and on the orchestrator's checkpoint line. It is
recorded here rather than left to be rediscovered, because it is a change to
the meaning of a ruled threshold and Tyrel is the one who ruled it.

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
                       flags, change_record, uncertain_spans, unresolved}
payload.audit         {draft_ref, finding_ref, finding_digest, unresolved,
                       reproofs, request_digest}
```

The flags are computed once per page, before any re-proof result exists, so no
result can reopen the calculation. `change_record` attributes a changed span to
the *narrowest* flag containing it and refuses a change that escapes every
flagged location. Nothing in this pass selects among witnesses: the request
carries no witness identity, no witness text and no ranking, and the prompt is
byte-identical for every flag class. A `testimony-diff` flag's *location* is
witness-derived, though, and now that the instrument is actually delivered the
reader is directed to the exact spans where it disagreed with witnesses while
the tree measures movement toward them — whether that is compatible with
GOVERNANCE 3 ("never picks") and 10 ("the instrument may not constrain what it
measures") is an open interpretation question routed to Tyrel with the Tier-0
reproof change, not settled by this sentence.

**The re-proof plan is a delivered instrument, not a claim about one.** One
function, `perlector_audit.reproof_plan`, turns the frozen flags into one
neutral, location-only prompt each; `audit_request` wraps that plan into the
closed object the reader is handed:

```text
audit_request = {schema: "perlector-audit-request.v1", act_key, attempt_ordinal,
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
reader which way to argue (GOVERNANCE 10). And because the instrument travels
as input, a reader still may not condition generation on `pass_kind`: a
re-proof pass arriving with no request is refused by
`reader.validate_audit_delivery`, as is a request delivered to any other pass,
or one naming a different act than the dossier beside it. `FixtureReader`
branches on the request, never on the pass label.

This is the post-stack Tier-0 repair of audit finding **Sol-S2**. Before it, Pass C computed
the plan, sealed it under `payload.audit.reproofs`, and then called `read` with
the Pass-B dossier plus a spliced `semi_final_text` — no flags, no locations,
no prompts, and a `dossier_digest` that no longer covered the object carrying
it. A changed final text was published as the result of a measured, neutral,
span-scoped re-proof that was never presented. `test_audit_pass.py::
test_the_reader_receives_exactly_the_reproof_plan_the_perlectio_seals` captures
the real reader call and requires exact equality with the sealed plan; it is the
test that fails if the two ever part again.

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
`bound_serving_recipes` refuses a catalogue whose bytes are not the ones the run sealed,
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
and stops the pass with nothing published for that act. Nothing is lost: `ChairClient`
retains the raw response before it parses, so the bytes that stopped the pass are on
disk under their own digest and the refusal names them. The same refusal covers a body
that is not a reading at all (`parse_problem`): a Perlectio has no `failed` shape —
`outcome="failed"` is produced nowhere in `run.py` — and minting one here would invent a
record kind this section does not own.

**`engine_call`, and what it names.** A live reading's payload carries
`engine_call = {call_record_ref, raw_response_ref, response_sha256, finish_reason,
served_model_id}`, and the envelope binds both blobs as direct inputs, re-derived from
disk and compared to what the reader claimed. The field names *the call the published
text came from*: on an act whose Pass-C re-proof changed the text, it moves to the
re-proof's own call, beside `truncation` and `self_revision`, which move for the same
reason (audit finding H6). A re-proof reading that ran and changed nothing is still
bound as an input — it is the second thing that looked at this act's pixels and it is
what the `change_record` reports on — but it does not become the named call. The field
widens the closed field set for the record that carries it (`with_engine_call`, the
`_NOT_RUN_CAPACITY_FIELDS` precedent) rather than becoming optional inside one set. A
`FixtureReader` result never sets it, so fixture payloads and their envelopes are
byte-for-byte what they were, which is what leaves the acceptance pin where it is.

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
first. A `ServiceStopError` propagates — an unverified shutdown is fatal.

**The live resume rule.** An act whose `perlectio` already exists at
`perlegere:<ordinal>` is never asked again (`_reading_already_sealed`). Fixture readers
are deterministic, so a resumed fixture pass republishes identical bytes and the store
reuses them (`_next_attempt`'s docstring); a live chair cannot promise that, and the
store refuses the collision. Skipped acts are counted apart from `read`, because this
invocation did not read them.

**Two live-resume limits, named rather than hidden.** First, an act interrupted *between*
its `lectio-prior` publication and its Perlectio cannot resume: the resume rule looks at
the Perlectio, so the act is read again and Pass A is republished from a fresh live
reading, which the store refuses as an incompatible reuse. Extending the rule to skip on
a lone Pass A would leave the act permanently unread, which is worse; the forward path
from that refusal is the one the design already has, a Recensor recovery request.
Second, every re-invocation of a live pass starts and stops the service, so an `--act`
recovery loop pays a full model load per act
(`pipeline/orchestrator/run.py`'s per-act dispatch). Ruling 16 permits a server that
outlives one stage, but no cross-process handle exists; that is the next serving item.

**`max_tokens` is not sent, and that is a decision.** No output bound is sealed
anywhere, and this section does not invent one. vLLM bounds generation by
`max_model_len`, so an engine `"length"` then honestly means the context itself was
exhausted rather than that the harness cut the reading short. A sealed output bound
belongs with the variance-experiment section, which will need one too.

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
  one serving path is the drift this handoff exists to prevent. It is worth
  reading before that lane writes its own.
- Spec 10's `text_status` is now an Archetypus field, distinct from that record's
  fixed `status = "established"` literal. Archetypus re-derives it from the text,
  annotations, and uncertainty before accepting the record
  (`pipeline/6_archetypus/run.py:824-861`).
- **Pass-C can emit an `uncertain_span`, but only under a zero cap.** The predicate is
  `unresolved = bool(flags) and audit_policy["round_cap"] == 0`
  (`pipeline/4_perlector/run.py:3211`), so spans appear only when the sealed policy allows
  no re-proof round, not after a permitted round is spent. Each non-empty frozen flag
  location then becomes a low-confidence `audit-round-cap-exhausted` span on the finding
  and Perlectio (`:3357-3363`, sealed at `:3372` and `:3393`). A zero-width flag remains explicit in the
  frozen flags and `unresolved` state because it cannot become a span; Recensor routes it
  to review (`pipeline/4_perlector/test_audit_pass.py:1202`). **The committed policy
  cannot fire this path:**
  `config/perlector_audit.toml:12` sets `round_cap = 1`, so every reading carries an empty
  `uncertain_spans` list. Only a run sealed with `round_cap = 0` can produce one; the
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
