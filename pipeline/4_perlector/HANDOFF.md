# Perlector — handoff

The Perlector writes one append-only `kind="perlectio"` record for each reading
attempt under `4_perlector/artifacts/`, plus one append-only `kind="lectio-nuda"`
record for each sampled unprimed instrument reading. This walking-skeleton writer
takes its established text from the declared synthetic fixture solely to exercise
the evidence shape; it does not claim a real model reading. Its artifacts are
`skeleton.v1` envelopes with derived identities, attempt bindings, self-hashes, and
checked direct inputs.

**No other stage reads this one's code.** `pipeline/5_recensor/run.py`,
`pipeline/6_archetypus/run.py` and `pipeline/7_armarium/run.py` consume exactly the
fields named below, unchanged in shape from the walking skeleton's first landing;
everything added since is additive.

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
                    rendered_sha256}: the declared prompt this reading was
                    actually produced through (invariant #49, see below)
dissent          -- derived-comparison-view rows (see below)
truncation       -- {classification, signals}, present on every attempted
                    reading regardless of outcome (see below)
uncertain_spans  -- [{start, end, alternatives, confidence}, ...]
gaps             -- [{position, start, end, witness_evidence}, ...]
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

```text
act_id, act_key, witness_regime
regions       = [{region_id, image_path, image_sha256, witness_covered}, ...]
page_renders  = [{source_page_id, source_page_ordinal, source, image_path,
                   image_sha256, transform}, ...]
testimonia    = [{witness_label, model_name, resolved_provenance,
                   training_domain, outcome, reported}, ...]
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
sentence. Every configured witness must have an entry or the dossier build
refuses by name.

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
{serving_recipe, chair_identity_sha256, dossier_digest, rendered_sha256}
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
`dossier`.

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
(off) for every scenario that predates this build. **A non-zero rate refuses
without `--nuda-approval-ref`**: spec 08 requires a predeclared, Tyrel-approved
sampling design, and hard rule 1 makes that a refusal rather than a note. Each
record carries `sampling = {nuda_per_mille, selection_rule, approval_ref}`,
because a sample of unknown design measures nothing (GOVERNANCE 10).

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

## Not built here

- Real serving (vLLM, LoRA-unmerged adapter, revision pinning, readiness
  polling, service receipts) is spec 04's territory and needs a live pod.
  `reader.py`'s `Reader` protocol and `prompts.py`'s per-recipe registry are the
  seam a real implementation occupies later without `run.py`'s orchestration
  changing.
- Reconciliation across several *independently produced* readings of one
  unrecovered attempt (spec_08's general "where it produces several readings, it
  reconciles them itself, against the image") is not modeled: nothing upstream
  produces two independent readings of one attempt today, only re-reads driven
  by Recensor's own bounded recovery loop (already never a pick).
- The closed-schema check above is producer-local. `validate_serving_provenance`
  already refuses a wrong-schema provenance wherever a Perlectio is *consumed*,
  but no consumer refuses a Perlectio that carries no `dissent` record at all —
  a reading with the instrument missing could still be established. Closing that
  belongs in `6_archetypus`, whose file another lane owns this round.
- A real vLLM launch declaration (pinned `--revision` and `--tokenizer-revision`,
  an explicit `--chat-template` rather than an ambient tokenizer default,
  unmerged `--enable-lora` with its base verified separately, a bounded
  readiness probe with named failure signatures) was built during this stage's
  second lane and is deliberately **not** carried here: it is spec 04's
  territory and the serving-manager branch's file, and two implementations of
  one serving path is the drift this handoff exists to prevent. It is worth
  reading before that lane writes its own.
- Spec 10's `text_status` (a closed `established | partial | no_readable_text`
  enum on the Archetypus payload) has not landed — `pipeline/6_archetypus/run.py`
  still writes a hardcoded, unvalidated `"status": "established"` string. This
  stage's `outcome`/`gaps`/`truncation` give Archetypus everything it would need
  to build that enum; wiring it is Archetypus's own file, owned by another lane
  this round.
- **No producer emits an `uncertain_span`.** `run.py` writes `uncertain_spans: []` on
  every reading, primed and nuda. Spec 08's output contract asks for "`uncertain` spans —
  read, with alternatives and confidence noted"; what exists here is the validator and
  its schema, which a real reader can populate without this stage's shape changing. The
  `FixtureReader` has nothing to be uncertain *about*, and emitting one anyway would be
  manufactured evidence.
- **`gaps` and `uncertain_spans` reach no consumer.** Neither word appears in
  `pipeline/6_archetypus/run.py` or `pipeline/7_armarium/run.py`. Spec 08's own test 8 —
  "spans and gaps round-trip; export-layer projection renders them without touching the
  text" — is therefore not exercisable anywhere yet, and a gap's witness evidence, which
  spec 08 wants displayable as "⟨illegible — witnesses agree: …⟩", stops at this stage.
  The projection is the export layer's (spec 11); the carrying is Archetypus's file.
- **Spec 08's contextual-suggestion flag is not built.** "A contextual suggestion (a year
  that must be 1805) may ride as a flag while the text stays what the pixels support" —
  the closed `_PERLECTIO_FIELDS` set has no field that could carry one, and nothing here
  produces one.
- **A truncated or unknown reading is held, not retried.** Spec 08 asks that such an
  attempt be "recorded, retried within the recovery budget, never accepted"; the Recensor
  routes it to `held-for-review` instead. Nothing is lost and no stale text is
  established — the safe half of the requirement holds — but the bounded retry the spec
  names is the Recensor's own file and has not been built.
