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

### `dossier` — the input contract, persisted as evidence (spec 08)

```text
act_id, act_key, witness_regime
regions       = [{region_id, image_path, image_sha256, witness_covered}, ...]
page_renders  = [{source_page_id, source_page_ordinal, image_path, image_sha256,
                   transform, width, height}, ...]
testimonia    = [{witness_label, training_domain, outcome, reported}, ...]
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
Under `blinded`, `witness_label` is a stable per-run pseudonym
(`pipeline/4_perlector/regime.py::pseudonym_for`) and `training_domain` is
withheld entirely — a training-domain sentence can identify a witness as surely
as its name, so both leave together. No resolved model identity (repo, revision)
ever appears in the dossier under either regime; that already travels on the
Testimonium's own provenance, one step downstream. The pseudonym has no stored
reversible map: reversal is recomputing the same deterministic digest over the
public roster in `run.json["witness_chairs"]`.

**Page renders.** One downscaled (factor 2, box-filtered) render per distinct
page an act's regions touch, with its transform recorded — reproducible from the
Exemplar plus the recorded transform (ARCHITECTURE invariant 3), stored
content-addressed under this stage's own blob store.

**Training-domain context.** `config/witness_context.toml`, a new
Perlector-owned declaration (not part of `common/chairs`/`ChairIdentity`),
mapping each configured chair to a factual, non-evaluative training-domain
sentence. Every configured witness must have an entry or the dossier build
refuses by name.

### `dissent` — derived comparison views, never raw-string voting

```text
[{chair, compared: true, departed, departed_raw, comparison_loss}, ...]
[{chair, compared: false, reason}, ...]                 -- did not report
[{chair, compared: "unknown", reason}, ...]              -- format not yet comparable
```

Computed strictly after the reading is fixed (`dissent.py`), over a
whitespace-collapsed comparison view of both sides. **Pinned forever: equality
only, never a distance metric** — no per-chair parameter, no similarity
threshold. `departed` is the view comparison; `departed_raw` is the untouched
raw-string comparison, kept alongside because a normalization that dropped
characters on either side can otherwise hide whether the raw strings actually
agreed. A witness whose declared format cannot yet be reduced to a comparison
view (`format_capabilities.can_express_uncertainty`) is recorded `"unknown"` —
never guessed, and never dropped from the list.

### `truncation` — the instrument, not an assumption

```text
{classification: "complete" | "truncated" | "unknown",
 signals: {stop_reason_declared, unclosed_structure, length_suspicious, ends_abruptly}}
```

Computed by `truncation.py` for every attempted reading, primed or nuda,
regardless of what outcome it ends up producing — so the record is never
optional detail dropped exactly when it would matter most. An engine-declared
`stop_reason_declared == "length"` is authoritative for `truncated`; otherwise the
three computed signals vote, and a split vote holds as `"unknown"` — never
resolved toward complete. Both `truncated` and `unknown` classifications map to
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
`position` is one of `leading | internal | trailing | whole-act`, each with its
own bound (leading starts at 0, trailing ends at `len(text)`, whole-act requires
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
attempt with an establishing one). Same payload shape as a Perlectio, except its
`dossier.testimonia` is always `[]` and its `dissent` is always `[]` — nuda
withholds testimony, never sight, so its dossier still carries the same regions
and page renders a primed pass would.

Sampled by a predeclared, run-sealed design: `--nuda-per-mille` (0–1000,
`nuda.py`), a deterministic hash-threshold rule over `(run_id, act_id)` — never
`random`, so the identical command samples the identical acts. Default `0`
(off) for every scenario that predates this build.

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
- Spec 10's `text_status` (a closed `established | partial | no_readable_text`
  enum on the Archetypus payload) has not landed — `pipeline/6_archetypus/run.py`
  still writes a hardcoded, unvalidated `"status": "established"` string. This
  stage's `outcome`/`gaps`/`truncation` give Archetypus everything it would need
  to build that enum; wiring it is Archetypus's own file, owned by another lane
  this round.
