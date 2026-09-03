# Designator — handoff

The Designator turns sealed Exemplar pages into the act denominator for the
walking skeleton. It writes only ordinary `skeleton.v1` artifacts below
`2_designator/artifacts/`; each envelope has a derived artifact identity,
attempt binding where applicable, a self-hash, and digest-checked direct inputs.
The derived manifest is inventory, not a second authority.

Spec 01 fixed the run-tree shape as `<stage>/artifacts/<kind>/<artifact-id>.json`
for every stage; there is no per-page `crops/`, `acts/` or `conservation/`
directory anywhere in this tree. Spec 06's contracts section names those as
concepts — a crop, an act-to-crop grouping, a coverage reconciliation — and
this handoff expresses each as an artifact *kind* below rather than a path.

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

## Scope and input boundary

Before cutting anything, the Designator reconciles `run.json`'s submitted
filename ledger with every Exemplar page outcome and the one self-hashed corpus
seal. A sealed page's Door admission and pixel blob are checked again before its
pixels are cropped. The check is deliberately before the first region write.

That reconciliation includes the merged page — two byte-identical files deriving
one `page_id` — which this stage refuses because `page_records` keys on the
submitted ordinal and would otherwise mint each act on such a page twice. **It
is no longer where an operator meets that case.** The Door now refuses the whole
submission when two submitted files would merge, naming their ordinals, while it
still has the filenames and before it seals its own boundary
(`pipeline/1_exemplar/door.py::require_no_duplicate_sources`). This check stays
as the second line of defence over the sealed shape itself, rather than over one
route into it; what it no longer has to be is the first thing that tells an
operator their export wrote one scan twice.

Two structural proposers exist, and the sealed serving catalogue says which one
a run gets (`structure_pass.structure_serving_mode`, never a flag and never the
ingress route). Under a `fixture` row for `designator_structure` the proposer is
the declared synthetic walking skeleton: every sealed page is decoded and
independently ink-scanned (`structure.py`), and the result is grouped into acts
by geometry and structural cues alone (`grouping.py`) — no text, no election
among candidates — reconciled against the fixture's declared rectangles. Under a
`vllm` row the proposer is the served structure chair itself, asked once per
sealed page through the live reading seam (`structure_pass.py`,
`run.py::live_initial_pass`; see "The structure chair" below), and the ink scan
becomes corroboration rather than ground truth. A real submission under the
fixture catalogue is refused by name after the Exemplar boundary check — it may
not be marked out by an ink scan standing in for a model, and no proposals or
holds are fabricated for it.

## `kind="region"`

There is one append-only region record for each original proposal crop, and a
second original record for a continuation on another page. Its subject is the
stable act identity; its attempt is `crop:<ordinal>`. The payload carries:

```text
region_id, act_key, attempt_ordinal, origin
transform = {operation, source_page_ordinal, source_page_id, bounds}
transform_digest
raw_bounds, padding = {applied_px, configured_bp, config_sha256, provenance} | null
image_path, image_sha256
provenance (resolved Designator chair identity, revision, and serving receipt)
```

`transform.bounds` is always the *final* rectangle actually cut — the one
`common/exemplar_boundary.py::verify_exemplar_crop_lineage` reproduces the crop
from, and the sole reason `transform` keeps exactly its historical four fields.
`raw_bounds` is the *structural* rectangle act identity is bound to
(`common/contracts/identities.py::act_bindings`): on the fixture path the
rectangle the sealed synthetic fixture declared, on the live path the structure
chair's own rectangle, converted from its normalized answer to page pixels once,
at the edge (`common/structure_answer.py::to_page_bounds`). Either way it is the
denominator `common/stage.py` recomputes the act's identity against, and on the
live path it is also the rectangle the page's retained `structure-answer` must
list. The visual grouping is recorded separately as `act-group.detected_bounds`
and reconciles against `raw_bounds` rather than replacing it. A
proposal crop's `raw_bounds` differs from `transform.bounds` by the configured
capture padding (`config/designator_padding.toml`), asymmetric and clamped to
the page edge, with `padding.applied_px` recording the exact pixels actually
added after clamping. A recovery crop carries `padding: null` — the Recensor's
request already names the exact rectangle it wants, and structural pad and
capture pad "must never be conflated" (`geometry.py`'s module docstring).
`padding.provenance` is the config's own `[padding.provenance]` table, copied
onto every proposal region so a reviewer sees on the evidence itself, not only
in a repository file, that these four fractions are carried forward from a
third-party corpus and have not been calibrated against this project's own
pages (`geometry.load_padding_config`, `pipeline/2_designator/padding_calibration.py`
is the ready-to-run harness for doing so once a gold set exists).

`origin="proposal"` is the evidence the witnesses saw. A later
`origin="recovery"` record is a new crop for the same act, never a replacement
for the proposal. Its direct inputs are exactly the sealed Exemplar pixel blob
and the Recensor recovery-request it answers; a proposal crop inputs only the
sealed Exemplar pixel blob. Consumers recompute the crop from that page and
transform, so a same-sized crop from another page does not pass as this one.

## `kind="act-group"`

One record per act reaching `outcome="proposed"`, subject-keyed to the act
identity, no attempt binding. This is the `acts/` contract: how geometry and
structural cues grouped the raw candidate regions into this act, reconciled
against (never the source of) the act's identity-bound bounds. **It carries no
text, enforced at the schema boundary** by `_refuse_text_fields` — each
`act-group` payload is walked for a closed set of forbidden content-bearing
keys (`text`, `reported`, `transcription`, `transcript`, `content`, `reading`,
`literal`, `token`, `tokens`), case-insensitively, before it is sent to
`context.publish`; this is the kind the spec names the rule for. The same set also
refuses `chosen` and `pivot`: neither is text, but both are the retired
picker's own words for the witness it elected (GLOSSARY, "Retired terms"), and
a Designator payload growing either field would be a picker announcing itself
at the one boundary already positioned to refuse it. The enclosing
`_validate_act_group_payload` also requires the exact top-level and continuation
field sets, so an unanticipated synonym such as `ocr_text` is refused as an
unknown field rather than passing because a denylist did not predict its name.

```text
act_key, declared_bounds
structure_evidence ("detected" | "fallback-tiles" | "shared-detection" | "model-only")
detected_bounds | null, body_member_count, anchor_count, rationale
continuation = {declared_bounds, structure_evidence, detected_bounds | null,
                body_member_count, anchor_count, rationale,
                geometric_corroboration} | null
```

On the live path `declared_bounds` means declared by the structure chair, the
field set does not change, and `continuation` is always `null` (a per-page call
has no cross-page knowledge; the relation is the Recensor's). The two extra
evidence values exist only there (`structure_pass.model_evidence_blocks`, which
never raises): `shared-detection` carries a real scanned region and its counts,
like `detected`, but says the same region covers at least half of another
proposed act too — the merged-boundary case the fixture path *refuses* at
`_claim_structural_group` because there the declared rectangles are ground
truth, recorded here on both acts as *not* independent corroboration
(GOVERNANCE 10). `model-only` is a rectangle no scanned region covers half of:
null bounds, zero counts, the rectangle resting on the chair's proposal alone.
Neither gates anything; the fixture path never emits either.

`rationale` is one of a small set of code-generated strings naming which
grouping rule fired (a single anchor, a brace linking two acts, an isolated
marginal note, a leading fragment with no anchor) — never a reading of the ink.

**`structure_evidence` is what tells a measurement from a fallback, structurally.**
`detected` means the structure pass genuinely found a region covering at least
half of this act's declared bounds. `fallback-tiles` means it found no ink on
the page at all, the page was cut into the predetermined grid instead (see
`kind="page-fallback"` below), and *nothing detected this act*: `detected_bounds`
is `null` and both counts are zero. This is a field rather than a sentence
because a consumer must not have to parse `rationale` to tell the two apart.

Recording a computed band as `detected_bounds` was the defect this closes, and
it had two heads. The bands span the whole page by construction, so every
declared act on a fallback-tiled page "matched" one — which published a
rectangle nothing measured with a zero member count beside it (GOVERNANCE 10),
and silently disabled `_match_structural_group`'s missed-act refusal on exactly
the pages where the structure pass found nothing. The refusal is unchanged
wherever detection actually ran, which is the property
`test_structure_failure.py::test_the_missed_act_refusal_still_fires_where_detection_actually_ran`
holds down. `continuation.geometric_corroboration` is likewise `false` whenever
either page was tiled: `find_continuation_candidate`'s test is that the trailing
group touches the page's bottom edge and the leading group its top, and a
full-page grid satisfies both on every page ever cut.

**One detected group corresponds to at most one act** (`_claim_structural_group`).
`_match_structural_group` answers "which detected group best covers this
declared act" one act at a time, so two acts whose declared rectangles both sit
inside a single detected group both matched it, and each act-group recorded the
merged rectangle as its own `detected_bounds` and the merged run as its own
`body_member_count` — a claim that detection corroborated each act separately
when detection found the boundary between them not at all. That is refused:
the structure pass merging two acts is a real finding about the detector, and
GOVERNANCE 10 does not allow it to be reported as two independent
corroborations. A brace-linked pair is unaffected — `grouping.group_page`
returns two distinct groups sharing one anchor, so each act claims its own.
`continuation.geometric_corroboration` is `grouping.find_continuation_candidate`'s
independent, page-edge-based check for whether the *geometry itself* looks like
a page-break continuation; it is recorded, never gating, because a declared
continuation whose crops do not happen to touch either page's edge (as in this
stage's own synthetic fixture) is still a genuine continuation. **Continuation
ownership is settled and is the Recensor's** — see "Continuation ownership"
below; this record is corroboration, never the relation itself.

## `kind="page-fallback"`

One record per sealed page the structure pass is cutting into predetermined
crops rather than a chair-drawn rectangle. On the fixture path (`initial`, a
fixture catalogue) that page is one the declared fixture rows say has **no ink
at all**, and the payload's `reason` names that premise. On the live path
(`structure_pass.ask_page`) it is one the *chair* answered with zero acts —
which is not the same fact as a blank page: the page's own ink scan may well
have found and grouped ink, and that measurement is recorded independently on
the page's `structure-status`, not asserted or denied by this record. The two
call sites pass different `reason` text (`_publish_page_fallback`'s `reason`
keyword in `run.py`) so the record never claims a measurement the live path
did not make. Subject-keyed to the one act that page's predetermined crops
belong to. Where a
declared act or continuation already has a proposal crop on that page, the
fallback bands are clipped around its final padded bounds before they are cut:
the union still sends the whole page downstream, but no pixel reaches readers
under both a declared identity and the page-fallback identity. Tyrel
ruled this on 2026-08-11: *"If the designator sees no text it should default to
predetermined crops with a small margin of overlap and send the crops down
stream to be read by everything. If all the witnesses and the perlector see no
text on any of the crops then it's likely a true blank."* Deciding blankness
here, from one threshold on one page, decides it with the weakest instrument in
the pipeline; the readers only get a say if the crops reach them. In this
walking skeleton the witnesses' empty reports are declared fixture receipts,
while the Perlector now observes the delivered pixels before it reports empty.

```text
subject_id = act_bindings(page_id, "page-fallback", page_bounds)
act_key ("page-fallback:<ordinal>" — a label, never the identity)
page_id, page_ordinal, page_bounds
tile_count, tiles = [{bounds, rationale}]
reason
provenance (the resolved Designator chair)
```

The subject line is not a payload field: it is what the record is keyed to, and
it is written here because the payload's `act_key` is the one field that looks
like an identity and is not. `_verify_page_fallback_act_row` recomputes the
subject from `page_id` and `page_bounds` and *separately* requires `act_key` to
equal `fallback_page_act_key(page_ordinal)`, so an implementation that derived
either one from the other refuses there. Before this branch the block carried
`fallback_ordinal`, the reserved ordinal the identity was then built from;
removing it with the ordinal identity left the block with no identity ingredient
at all, which is the gap this line closes.

`grouping.fallback_tiles` computed this grid before, and nothing cut it: the
tiles were handed to `_match_structural_group` as match candidates only, so the
*second half* of the ruling — send the crops downstream — did not exist, and a
sealed page with no ink and no declared act sent nothing anywhere. Now each tile
becomes an ordinary `origin="proposal"` region of the minted act, cut by the same
`cut_minted_region` a declared act's crop goes through, so a crop still has
exactly one author.

**One minted act per page, one region per tile**, never one act per tile. The
structure pass found nothing, so it has no opinion about how many acts are on
this page and must not manufacture one by counting bands. Every consumer already
reads *all* of an act's proposal regions — the Attestatores witness each one,
the Perlector reads through every region of the act — so one act with N regions
delivers the page whole, and N acts would be an act count invented from a grid.

The act is **`proposed`, not `held`**. A held act is terminal and is never read
(`recovery_pass`; and `_publish_residual_holds`'s own "never witnessed and never
read"), and crops nobody reads are precisely what the ruling forbids producing.
Its identity is `act_bindings(page_id, "page-fallback", page_bounds)` — the
`"page-fallback"` act class is its own namespace in the identity ladder, beside
the `"residual"` class, so the two minted-act identity spaces are disjoint by
construction rather than by an argument about which values are unlikely to be
reached. `common/stage.py::_verify_page_fallback_act_row`
recomputes that identity and then reads this record's single input — the page's
own `structure-status`, through the digest-checked reference hop — to confirm
that page really does record `structure_evidence == "fallback-tiles"`. A
fallback act minted over a page the structure pass detected regions on is
refused there, so the extra denominator row proves its premise rather than
asserting it.

A fallback tile's region carries `padding: null`, like a recovery crop and for
the same reason: the tile *is* the final rectangle, computed from the page's own
dimensions with this page's own resolved `fallback_overlap_px` already built
into it.
Expanding it again by the capture padding would conflate a structural pad with a
capture pad, which `geometry.py`'s docstring says must never happen.

A page the structure pass was **held** on (`structure_failures`) is not tiled:
its acts are held and no crop is cut on it at all. "We could not mark this page
out" and "we marked it out and found nothing" are the two different facts Tyrel
drew apart on 2026-08-05, and they get different records.

## `kind="conservation"`

One record per sealed page this run reached, subject-keyed to the page identity,
no attempt binding. The independent coverage proof: every ink pixel at the
stage's most sensitive declared threshold, `structure.SECONDARY_MARGIN`, reconciled
against the *final* (padded)
proposal crops actually cut on it — never against what grouping *claims* to have
found. The cut rectangles are what is passed as `claimed_bounds`
(`pipeline/2_designator/run.py:1535`), and the counts and residual components are
published from that scan (`:1546-1572`). Ink that no crop covers is not merely counted:
on a successful pass every residual component becomes its own held act
(`_publish_residual_holds`, `pipeline/2_designator/run.py:1279-1332`), so a mark
structural grouping never proposed is visible rather than absent. If two components
share a bounding box and therefore cannot receive distinct act identities, the stage
refuses before minting either instead of collapsing their evidence (`:1302-1313`).

The accounting cannot detect ink fainter than `background - SECONDARY_MARGIN` or a mark
with no contrast against its background; neither enters the denominator. A page whose
background cannot be inferred is recorded
`ink_measurable: false` with its reason rather than counted at a substituted divider.

```text
page_ordinal, background_source, background_value | null
page_width, page_height, reconciliation_thresholds | null
ink_measurable, reason | null
total_ink_pixel_count | null, claimed_pixel_count | null, residual_pixel_count | null
residual_component_count, residual_ink_fraction_bp | null, max_residual_components
residual_enumeration = "complete" | "withheld-page-held"
residual_components = [{bounds, pixel_count, review_priority}]   # key OMITTED when withheld
```

`claimed_pixel_count + residual_pixel_count == total_ink_pixel_count` always,
by construction, whenever `ink_measurable` is true — on a withheld page too.

`residual_enumeration` is a closed pair and is on every record. It is what lets
a consumer tell a page with no unclaimed ink from a page whose unclaimed ink was
counted and not listed; when it is `withheld-page-held` the `residual_components`
key is **omitted rather than emptied**, because an empty list is the first of
those two claims and the key's absence is what makes every existing consumer
fail loudly instead of reading absence as none. `residual_component_count`
equals `len(residual_components)` on an enumerated page and is the count the
reconciliation took on a withheld one. `residual_ink_fraction_bp` is
`residual_pixel_count` over `total_ink_pixel_count` in basis points, recorded and
gating nothing. `max_residual_components` is the sealed bound the page was judged
against, published whether or not it was crossed.

`page_width`, `page_height` and `reconciliation_thresholds` are what this scan
actually executed on and under. The thresholds are exactly the two
`conservation.reconcile` is given — `gap_tolerance_px` and
`review_priority_min_dimension_px` — never the whole `GroupingThresholds`: this
record answers for its own measurement, not for the structure pass. They are
`null` when `ink_measurable` is false, where no reconciliation ran to have
executed under anything. This is the record that keeps a structure-held page
honest: its `structure-status` says null for a pass that never ran, and this one
says what did.

The background fields belong to this reconciliation, not to the structure
pass. This distinction matters on a page whose structure pass was held before
analysis: its `structure-status.background_source` correctly remains `null`,
while conservation's later independent scan records the inferred modal value it
actually measured against. `background_value` is `null` exactly when
`ink_measurable` is false and no threshold could be inferred.

**`ink_measurable: false` is a reconciliation that could not happen, published
rather than skipped.** A page whose background `infer_background` refuses has no
threshold that separates ink from paper, so there is nothing honest to count:
the three counts are `null`, `residual_components` is empty, `reason` says why,
and the record's outcome is `held`. The stage used to substitute the page's own
mean as a divider so the accounting "had something defensible" — but on the
inverted scan `test_structure.py` uses (80% of the page at 30, 20% at 220) that
divider is 68, so every pixel of the *dark paper* counts as ink: four fifths of
the page reconciles as unclaimed ink and mints a held act over the background,
at scale. A count taken at a guessed divider is a guess reported as a
measurement, which is exactly GOVERNANCE 10's "a metric that cannot be measured
is a failure, not a pass". The page's crops are still cut and still go
downstream (`kind="page-fallback"`); what is refused is the claim to have
measured them. The secondary scan is skipped on such a page for the same reason
— it is the same threshold at a more sensitive margin, so at a guessed divider
it publishes rescue crops over paper. **Every residual region is accounted regardless of size**:
`review_priority` ("high"/"low") orders which residual a reviewer looks at
first and never decides whether one is recorded at all — deleting the priority
threshold would only reorder the list, never shorten it.

**Every residual is also now minted as its own held act**
(`pipeline/2_designator/run.py::hold_residual_act` via `_publish_residual_holds`,
verified by `common/stage.py::_verify_every_conservation_residual_is_accounted`),
closing the gap this section used to name as unimplemented. `expected_acts` no longer requires the seal's denominator to
equal the fixture's declared acts exactly — every fixture act is still a floor
that must appear, but the seal may also carry additional rows a residual
minted, each `held` from the moment it exists (never `proposed`: nothing
witnessed or read ink no structural pass claimed), each independently
recomputable from its own hold record's `residual_bounds` through
`act_id(page_id, "residual", bounds)` rather than trusted because the seal
names it. This
additive denominator path is not evidence that prior artifact digests remain
unchanged: `run_config_bindings` now seals
`designator_padding_config_sha256` into every run's `config_digest`, and later
fixture additions also enter that digest. The HAPPY and REVIEW pins were
therefore re-measured from fresh orchestrator runs rather than inferred from
which scenarios exercise residual minting
(`pipeline/orchestrator/test_orchestrator_acceptance.py`'s
`test_losing_the_first_page_holds_every_act_and_delivers_nothing` is the one
existing scenario that *does* produce a residual, incidentally: page 2 seals in
`refused-first-page` but never receives a cut region, so its own real ink
reconciles as 100% residual and now mints `residual:2:0` alongside `a1` and
`a2`, flowing unmodified through Attestatores, Perlector, Recensor, Archetypus
and Armarium — real end-to-end proof that a held act needs no fixture-specific
handling anywhere downstream). The five other stages this build does not
restructure needed no changes at all: a held act's outcome, whichever stage
minted it, already flows through them generically.

The identity a residual receives is disjoint from every real proposal's by
construction: it derives from `act_id(page_id, "residual", bounds)` -- the
`"residual"` act class is its own namespace in the identity ladder -- so a
structure pass's `"proposal"`-class identities can never collide with it,
present fixture or real one.

**The `page-residual` hold is the fourth act class and the second hold shape.**
When a page's reconciliation counts more residual components than the sealed
`max_residual_components` allows, no per-component act is minted for that page
at all; one act is, bound to `act_id(page_id, "page-residual", page_rectangle)`
and held. Its hold record carries `act_key`, `page_id`, `page_ordinal`,
`page_bounds`, `residual_component_count`, `max_residual_components`,
`grouping_config_sha256`, `blocking_page_ordinal`, `reason_code` and `reason`,
and names exactly one input: that page's own `conservation` record, which is
the independent premise saying the count exceeded the bound.
`common/stage.py::_verify_page_residual_act_row` recomputes every one of those
rather than reading it — the rectangle from the sealed page bytes, the identity
from the reserved class, the bound against the run's own sealed
`designator-grouping` digest — and
`_verify_every_conservation_residual_is_accounted` checks the other direction, so
a withheld record with no such row is a refusal rather than a silent loss.
`reason_code = "residual-components-over-page-bound"` names the reconciliation
and never the paper: nothing in this artifact says a page is speckled, foxed or
bad, only that this many components were counted against this bound.

The Recensor consumes the same decision in `geometry_coverage_inputs`, with its
own refusals and its own finding shape: `residual_act_count` is 0 on a withheld
page, and the count, the bound, the enumeration and the one page-residual act
are named beside it so that zero cannot read as a loss. The check it genuinely
loses there is the per-component pixel sum, because the list it summed is the
thing deliberately not carried.

## `kind="secondary-provenance"`, `kind="secondary-proposal"`, and `kind="rescue-crop"`

`secondary-provenance` is published exactly once per run, subject
`"secondary-provenance"`: the resolved `secondary_proposer` chair, absent or
configured, in the same shape Perlector's `provenance_for` uses for its own
optional chair. Every run resolves this role for real, whether or not one is
configured — the day the roster is enabled, `common/stage.py::unaddressed_chairs`
must already know to expect it, and only a real resolution keeps that claim
honest.

**Reconciling this against spec 06's "optional seat" wording, plainly, because a
reconnaissance pass flagged the two as if they might disagree: they do not.**
"Optional" describes the *configuration* — `config/models.toml` may leave
`secondary_proposer` absent, and an absence is a valid, recorded decision like
any other chair's (`AbsentChair`, not a missing entry). It has never described
the *resolution path*: something must ask the registry for this role on every
run, present or absent, or `unaddressed_chairs` cannot stay accurate the day
the roster flips it on — a misspelt or newly-added role that nothing resolves
is exactly the silent-drift shape invariant #2 forbids. `secondary_provenance()`
is that resolution path, and it is unconditional by construction (called once
in `initial_pass` regardless of scenario or configuration), which is what makes
the optional *chair* possible without a mandatory *code path* ever being
skippable. This build's reading: the chair is optional, its resolution is not,
and the two were never actually in tension — a reader who takes "optional"
to mean "the resolution call may be skipped when nothing is configured" is
reading a word choice as an implementation instruction it was never meant to
carry.

`secondary-proposal` exists only when the chair is configured, one held record
per rescue candidate the secondary scan finds outside authoritative coverage —
`authoritative: false`, always, at the schema level and in fact. A candidate
wholly contained by one claimed act is ordinary coverage and is not published;
merely overlapping one does not discard the additional area outside that claim.
Every published candidate carries `overlapping_claimed_act_count`: how many
already-claimed acts its box touches. That number is **recorded and never acted
on** — the P0-incident-shaped rule is that a detector may add recall and never
decide between two acts or refine either, and a held, page-subject,
`authoritative: false` crop that enters no act, no act-group and no proposal
seal decides nothing whichever count it carries.

A count of two or more used to be a hard refusal instead. The second review
pass of 2026-08-10 measured what that cost: act a1's and act a2's *padded*
capture rectangles abut at exactly one row of the shipped fixture page, so a
single ordinary pen mark in the blank band between the two entries produced a
candidate touching both, and `initial_pass` raised before the proposal seal was
written. Configuring an optional, explicitly non-authoritative chair therefore
turned a complete run into a fatal one with no denominator at all — the exact
inverse of spec 06's test 5, "removing the proposer changes no authority
decision (it adds recall, never verdicts)".

**Bounded per page, like the residual enumeration beside it.** Each published
candidate costs a cropped PNG blob and two records, so a page speckled enough to
trip `max_residual_components` would rebuild the unopenable run here — on the
one path that bound does not cover. Past `max_secondary_proposals` the page's
secondary pass is a single held `secondary-proposal` instead: the page
rectangle, `secondary_candidate_count`, the bound it was judged against, the
run's own sealed grouping digest, and no crop cut at all. Nothing is filtered
out of the scan itself (GOVERNANCE 10) — `structure.secondary_scan` still
returns everything it finds — and the candidates stay recomputable from the
sealed page bytes. `secondary_enumeration` is `complete` or `withheld-page-held`
on every one of these records, exactly the closed pair `residual_enumeration`
is, so "this page had no unclaimed candidate" and "this page's candidates were
counted and not cut" cannot be read as each other.

Each proposal directly references a `rescue-crop`: the exact unpadded source
pixels inside the secondary box, with its origin, null padding, transform,
image digests and the same `overlapping_claimed_act_count`. Both
records have `outcome="held"`; the crop says `authority_effect="review-only"`,
and neither enters an act, a structure region, or the proposal seal. This is the
terminal review disposition that prevents an additive proposal from existing
only as inert metadata while the stage exits complete. Removing the proposer
changes no `region`, `act-group`, or `proposal-seal` outcome; only the secondary
evidence and held exit disappear.

## `kind="structure-status"`

One record per sealed page, subject-keyed to the page identity: whether the
structure pass scanned that page or was held on it, and if held, the reason
code. Published for every sealed page rather than only the failing ones, because
a page nothing scanned and a page nothing *tried* to scan would otherwise
look identical — a reader could only infer the structural outcome from whether
crops happen to exist, which is exactly the inference GOVERNANCE 2 refuses.
`state` says "scanned", deliberately not "marked-out": GLOSSARY's Designator
entry already owns that verb for the stage as a whole, and a page can be
scanned by the structure pass while marking out no act on it at all (no
declared act touches that page) — the Recensor's own "marked out" is a
different fact about the *act*, not this per-page structural pass record.

```text
page_id, page_ordinal, state ("scanned" | "held"), reason_code | null
background_source | null, structure_evidence | null
page_width | null, page_height | null
resolved_thresholds | null (every field of GroupingThresholds)
provenance (the resolved Designator chair)
structure_answer_ref            # live path only: the page's retained answer
```

On the live path every page's status carries `structure_answer_ref`, the
digest-checked reference to that page's `structure-answer` record, and
`structure_evidence` is the *answer's* fact rather than the scan's — `detected`
when the chair returned at least one act, `fallback-tiles` when it returned
none, `null` on a held page. The scan still ran on every page and its
background and resolved thresholds are recorded beside it; what decides which
crops a page gets is what the chair returned, and the field says so.
`reason_code` on a live-held page is one of `structure_pass.STRUCTURE_HELD_CODES`
(the table under "The structure chair"). `common/stage.py::
_verify_proposal_act_row` follows `structure_answer_ref` for every structural
row of a served seal, and `_verify_page_fallback_act_row` reads
`structure_evidence` for every page-fallback row, so both facts are consumed
rather than merely recorded.

`background_source` and `structure_evidence` are the structure pass's own audit
trail for *how* this page was read, and they are published here because this is
the one per-page record that already exists. `background_source` says where the
ink threshold came from — `inferred-modal` when the page's own modal pixel was
taken as its paper — so a page whose threshold did not come from its own mode is
visibly different on disk rather than identical to an ordinary scan.
`structure_evidence` says whether the crops on this page came from detection or
from the predetermined grid. Both were computed in an in-process dict that
nothing published, which meant neither fact survived the run. Both are `null` on
a page held before it was analysed at all: the structure pass produced no
background and no evidence there, and saying `inferred-modal` of a pass that
never ran would be the same defect these fields exist to close.

`page_width`/`page_height` and `resolved_thresholds` answer the next question
out: not how the page was read, but **what geometry the run executed on it**.
The sealed grouping policy is in basis points of a page dimension, so the pixel
numbers a page runs under are a function of the policy and of that page's own
size, and until now they lived only in `_analyze_page`'s cache. A calibration
session reading a finished run back can now say, per page, what the structure
pass actually ran at instead of re-deriving it from the seal and the pixels —
and a re-derivation is what stops matching the run the day the resolution rule
changes. The whole of `GroupingThresholds` is published rather than a chosen
subset, `max_residual_components` included: it is part of what the page ran
under, and a subset boundary would be a second judgment about which of one
dataclass's fields matter. These fields are a recording and decide nothing; they
are `null` on a page held before analysis for the same reason the two above are.

**That null answers for the structure pass, not for the page.** Conservation
scans a structure-held page all the same — the ink is real and no crop claims
it — so the geometry that reconciliation ran under is published on that page's
own `conservation` record instead. Neither record speaks for the other, and
neither leaves a null standing beside a computation that did happen.
This is the cheap honest half of the 300-DPI question — there is no physical
page size on disk for a photographed master, so there is no DPI to probe, and a
probe that guessed one would be a measurement invented rather than taken.

On the fixture path a failure is declared per scenario by the fixture's
`[[structure_failure]]` rows, because the walking skeleton has no live structure
model that can fail. Everything downstream of the declaration is real:
`structure_failures` refuses two declarations for one page rather than taking
either, and ignores a declaration naming a page this run never sealed, since the
Exemplar's own refusal already accounts for that loss and a second hold would
double-count it. On the live path the failures are the chair's own — a page
whose answer was cut off, did not parse, could not be read, or touched none of
the page's ink — and they reach exactly the same `failures` map, so everything
downstream of a hold (no crop cut, ink reconciled as residual, `EXIT_HELD`) is
the one code path the fixture scenario already proves.

## The structure chair

The live pass (`structure_pass.py` for what faces the chair,
`run.py::live_initial_pass` for the wiring) is selected when the sealed
serving-recipe row for `designator_structure` says `kind = "vllm"` and a
`--placement-tier` names the measured card. It shares with the fixture pass
every piece that is not the proposer: the Exemplar boundary, the sealed
padding, geometry and grouping policies, the per-page ink analysis,
`cut_minted_region`, `publish_structure_status`, the page-fallback tiling,
conservation, the residual holds, the once-only seal and the exit rule. What
replaces the fixture's declared acts is one call per sealed page through
`operations/serving/client.py::ChairClient`, built by
`structure_pass.default_serving_factory` in production and injected by a test
(`main(serving_factory=...)`) exactly as the Attestatores and the Perlector
inject theirs.

**What is sent.** One `chat-completions` request per sealed page, the whole
page, the exact sealed PNG bytes as one `data:image/png;base64` block, with
`image_sha256s=(source_sha256,)` so the client's digest check binds the request
to the Exemplar. The prompt is code, sealed by digest
(`structure_prompt.py`, `verbatus-structure-prompt.v1`); it asks for every act as
one rectangle in normalized 0–1000 coordinates of the image as shown, its
transcription as written, an optional label, in reading order, and states no
preference, severity floor or confidence budget. No `max_tokens` and no
generation knobs of the stage's own: the engine bounds generation by
`max_model_len`, and a `"length"` stop then honestly means the answer did not
fit.

**What comes back** is parsed with `common/structure_answer.py`'s closed
contract — one accepted wire shape, every other key a named refusal, floats
quantized under a declared rule, coordinates converted to page pixels once by
this repository's own arithmetic. Nothing is repaired, reordered, or re-asked.

**What each answer does to the page** (`structure_pass.ask_page`):

| Answer | Page | Acts |
|---|---|---|
| parsed, complete stop (or unreported), ≥1 act | `scanned`, `structure_evidence="detected"` | minted, one per distinct rectangle |
| parsed, complete stop, zero acts | `scanned`, `structure_evidence="fallback-tiles"` | one page-fallback act over the predetermined grid |
| `finish_reason ∈ {"length"}`, parsed or not | `held`, `structure-answer-cut-off` | none; ink → residual holds |
| parse refused | `held`, `structure-answer-<parse_outcome>` | as above |
| the client could not read the body (`parse_problem`) | `held`, `structure-call-unusable` | as above |
| parsed, the scan found ink, and no rectangle touches any ink pixel | `held`, `structure-answer-no-ink-overlap` | as above |
| serving or transport refusal | **fatal**, nothing published for the page | — |
| an engine stop word outside the closed vocabulary | **fatal** | — |

A cut-off answer is held even though it parsed: a truncated act list is a
missed act (GOALS 1). The no-ink-overlap row is the coordinate-space tripwire
for a chair whose geometry is in a space this stage never sees: it is a pixel
test against the components the page's own scan counted, not a threshold, and
it fires only when the scan itself found ink and nothing the chair drew touches
any of it. A held page is not tiled, its ink reconciles as conservation
residual, and the run exits `EXIT_HELD`; the retained bytes, `parse_outcome`
and `prompt_sha256` are the evidence.

**Minting.** For each rectangle in reading order: validated against the page,
minted once per distinct rectangle (a repeat is a `duplicate-rectangle` finding
on the answer record naming both ordinals — the class-and-bounds identity has
no ordinal namespace, so the second is the same crop), `act_id = act_id(page_id,
"proposal", raw_bounds)`, `act_key = "proposal:<page>:<ordinal>"` (a label,
never identity), one proposal region through `cut_minted_region` with the
sealed capture padding, one `act-group` with the scan's corroboration, one
seal row with `has_continuation = false`. **No continuation is proposed by the
live pass**; the unproposed cross-page half act stays the named evidence defect
below.

**`kind="structure-answer"`**, one per sealed page, subject the page identity,
text-free (`_refuse_text_fields` runs over it; `text_digest` and `text_length`
are what let a later reader prove it derived the same text from the same
retained bytes):

```text
schema = "designator-structure-answer.v1"
page_id, page_ordinal, page_w, page_h
prompt_version, prompt_sha256, answer_schema = "verbatus-structure-answer.v1"
call_record_ref, raw_response_ref, custody_ref, receipt_ref, request_sha256
finish_reason (verbatim | null), served_model_id, call_problem | null
parse_state ("parsed" | "refused"), parse_outcome | null
disposition ("detected" | "fallback-tiles" | "held"), reason_code | null
act_count, acts = [{ordinal, box_1000, raw_bounds, text_digest, text_length, label}]
findings = [{kind, ...}]
quantization, page_text_rule
decoding = {policy = "structure", temperature, decoding_config_sha256}
provenance (the served chair, its real receipt, and `engine_call`)
```

The raw response is retained twice under one digest: by the client before it
is parsed, and under `common/chandra_custody.py`'s one-receipt binding
(`custody_ref`), the read half of which is the Attestatores' intake. The text
itself lives only in that blob. `common/stage.py::_verify_proposal_act_row`
holds every structural row of a served seal to this record: the page's status
must say `scanned` and name it, it must have parsed under the same
`engine_call` the seal records, its `call_record_ref` must resolve to a genuine
`chair-call-record.v1` for this chair under the run's sealed decoding digest,
and it must list the row's exact rectangle — no nearest match.

**Provenance on the live path** is `structure_pass.live_chair_record`: the
chair's real serving receipt (never `_configured_chair_record`'s `fixture://`
value — a declared moment on a path that called the chair would be a fabricated
one, GOVERNANCE 6) plus `engine_call`, the closed `structure-chair-call.v1`
posture `{schema, call_kind, decoding_policy = "structure",
decoding_config_sha256}` that `validate_serving_provenance` binds to the run's
sealed decoding digest. The secondary proposer is resolved on this path too and
must be absent (Tyrel, 2026-08-12); a configured row is refused by name before
any chair starts, because nothing serves it and no fixture receipt may be
written for it.

**Decoding.** The pass runs under `config/decoding.toml`'s `[structure]`
section and never under `reading_of_record` (Tyrel, 2026-09-02: the
Attestatores keep the fixed posture; the structure pass may vary, sealed and
recorded per run, so its re-run variance is a clue beside the witnesses). The
value is read from the sealed bytes, rechecked by digest at the point of use,
and recorded on every page's answer record. **The limit, stated plainly:** the
live reading seam records the reading-of-record temperature and puts 0 on the
wire for every call (`ChairClient`, `request_body(deterministic=True)`), so
today a sealed `[structure]` temperature other than 0 is refused by name before
any chair starts (`structure_pass.executable_temperature`) — running at 0 under
a record that says otherwise would be a posture reported rather than executed.
Widening the seam to carry a per-call temperature is what unlocks a non-zero
value; the section, the loader, the recheck and the record are already in
place for it.

**Every witness runs its own pass.** SPEC_D §3's "captured" kind — filing the
structure chair's transcription as Attestator 1's Testimonium instead of
serving Chandra a second time — was retired by ruling on 2026-09-02: the three
witnesses are three independently trained systems and each runs end to end.
The live pass therefore captures nothing for the witnesses; what it retains
under custody is its own evidence. Nothing here is a picker: the chair
proposes, the scan corroborates, and nothing selects among witnesses.

**Proved end to end.** `pipeline/test_structure_chair_e2e.py` runs this pass as
the *denominator* of a whole run: the Door, Exemplar and Ink Map as real
programs, this stage's live pass against a scripted structure chair, then the
three live witness chairs, a live Perlector, and the Recensor, Archetypus and
Armarium as real programs over acts no fixture declared. It asserts that each
minted region's `raw_bounds` are the chair's own rectangle and its `act_id`
recomputes from them, that the seal verifies at the Attestatores' own
boundary, that the sealed `[structure]` temperature is both on the wire and on
every answer record, that no Designator artifact carries a byte of the chair's
transcription, and that a second attempt whose rectangles moved is an ordinary
run — different acts on the page that changed, the same act on the page that
did not, because identity is content-addressed rather than positional. The
zero-act and cut-off answers, and 7 of the 11 named parse refusals
(`_STRUCTURE_REFUSALS` in `operations/serving/fakes.py`), are exercised there
over the real chain as well as in this stage's own suite; the remaining four
refusal codes are exercised only at the parser level
(`common/test_structure_answer.py`). The export it reaches
is *held*, for the reason the live seam suite measures over declared acts:
Churro publishes no native layout, so two witnesses of a floor of three count
(`pipeline/3_attestatores/HANDOFF.md`). That is a witness-coverage fact, not a
fact about this stage — every act the chair proposed was read.

**Named risks.** The real `designator_structure` rows' `max_model_len` is a
planning value, and a whole-page transcription plus geometry may not fit it;
`structure-answer-cut-off` on every page of the first real run is the
measurement that says so. The engine resizes the page internally, so the exact
image the model saw is not the sealed page (ARCHITECTURE invariant 3): the
request binds the sealed bytes, the receipt records `pixel_cap`, and normalized
coordinates keep geometry resolution-independent — a residual gap, named, not
closed. Bounded recovery from a structural hold stays unbuilt (below).
`excluded` stays unproduced: it exists only with a Tyrel approval reference,
and no Designator path resolves one.

**The fixture path is unchanged and re-pinned once.** Under the committed
catalogue `initial_pass` runs as before: no answer record, no `engine_call`, no
`structure_answer_ref`, the same `fixture://` receipt. The one fixture-visible
change of this unit is the `[structure]` section itself — its bytes move
`config/decoding.toml`'s digest and therefore every fixture run's
`config_digest` — so the acceptance pins are re-measured by the host on `main`,
not inferred here.

## `kind="hold"` and `kind="proposal-seal"`

If an act's own page or necessary continuation was not sealed, or the structure
pass could not mark that page out, the Designator publishes one `held` record
rather than omitting the act. Its direct input is the relevant Exemplar page
outcome; its payload names the act key, the page whose state blocked the act,
the reason as a sentence, and the reason as a closed code:

```text
act_key, blocking_page_ordinal, reason_code, reason
```

`reason_code` is one of `exemplar-page-not-sealed`,
`exemplar-continuation-not-sealed`, `structure-pass-held`, or
`structure-pass-held-on-continuation` (`HOLD_REASON_CODES`), so a consumer can
branch on the cause without parsing prose and a new cause has to be declared
rather than appear as an unexpected sentence. The page field is
`blocking_page_ordinal` rather than the `unsealed_page_ordinal` this payload
carried while an unsealed page was the only way to reach here: a page the
structure pass failed on **is** sealed, and the old name would have said
something false about it.

A structure-pass hold does not suppress that page's ink. The page sealed, so
its ink exists; no crop claims any of it, so all of it reconciles as
conservation residual and each residual component becomes its own held act.
That is the difference Tyrel drew on 2026-08-05 between "there was nothing to
read" and "we could not read it", carried through structurally rather than by
convention.

The once-only `proposal-seal` is the downstream denominator. Its self-hashed
payload contains `count`, Designator provenance, and one `expected_acts` row per
synthetic act:

```text
act_id, act_key, page_id, page_ordinal, has_continuation, outcome, evidence
```

`evidence` is the exact sorted set of region and/or hold references for that act,
and the seal's direct input set is their exact union. Consumers reject a shorter
denominator, an unaccounted Designator record, a duplicate, a claimed
continuation with no supporting proposal, or a mismatch between the row and its
evidence.

## Real ingress: what the real structural pass must publish

On a real submission this stage refuses: it proves its Ink Map boundary,
reconciles the Exemplar filename ledger, and then says that real structural
proposal/model work is outside System 03 rather than fabricating proposals or
holds. Everything below is the contract the pass that replaces that refusal has
to meet, because it is what consumers already recompute rather than believe.

**Publish `raw_bounds` equal to the rectangle the act identity was minted
from.** On the fixture route the proposal seal is checked against an
independent sealed declaration, which is the stronger check; a real run has no
declaration, so the producer's own crop rectangle is the only independent thing
left to recompute against. `common.stage.expected_acts` therefore takes each
structural row's proposal-origin `region` on that row's own page and requires
`act_bindings(page_id, "proposal", region["payload"]["raw_bounds"])` to
reproduce the row's `act_id`. `cut_minted_region` already publishes exactly
that rectangle, so no change is needed today — but a pass that padded, rounded
or renormalised `raw_bounds` on the way out would break the real denominator
while leaving the fixture route green, which is why it is written down here.

Three more fields on that same row are recomputed beside the identity, and each
is a refusal by name when it disagrees with the region's own evidence:
`act_key`, `page_ordinal` (both published beside every region, as `act_key` and
`transform.source_page_ordinal`), and `has_continuation` — which is checked in
both directions, so a claimed continuation with no far-page proposal region and
a denied one with such a region are equally refused. A row's class is decided by
which evidence record exists for it, never by trying identities until one
verifies; two minted-class records on one row refuse as ambiguous.

**Publish the conservation denominator too, per sealed page.** This is the
first thing a real run cannot get past today, and it is measured rather than
predicted: `pipeline/test_real_ingress_contexts_e2e.py` carries a real
submission through the Door, the Exemplar, the Ink Map, a full served
Attestatores roster and a served Perlector, and the Recensor then stops the run
with *"Designator conservation pages 1, 2 carry non-held expected acts but have
no conservation records"*. `geometry_coverage_inputs` requires one
`kind="conservation"` record for every sealed page carrying a non-held expected
act, and independently reconciles its residual components against the held
residual acts in the seal. Those are measurements of the real page — total ink,
claimed ink, the unclaimed remainder and its components — so nothing but this
stage's own pass can supply them, and no test may compose them on its behalf
(GOVERNANCE 10). Until the real pass exists, that refusal is the honest end of
a real run, and the e2e pins it there.

## Exit code

`EXIT_COMPLETE` (0) only when the seal holds nothing, no page was held, no
secondary rescue awaits review, and every sealed page's ink was actually
measured. Anything held — an act, a page, ink no authoritative crop claimed, or
a non-authoritative rescue — exits `EXIT_HELD` (3), and so does a page whose
background could not be inferred. The exit code is the one signal an operator
reads without opening the tree, and a 0 over a hold is a partial result wearing
"complete" (GOVERNANCE 2). Act holds are computed from the seal's own rows;
secondary holds are computed from the rescue records published in the same pass
because that evidence deliberately does not enter the authority. A recovery
invocation cuts one requested crop and exits 0 or fails; it publishes no holds.

**An unmeasured page is not a held page, and the distinction is load-bearing.**
Nothing is pulled out: no act is held, every declared act on it is still cut,
and its predetermined crops are cut and sent downstream, which is what Tyrel's
2026-08-11 ruling requires ("everything gets read every time nothing gets pulled
out or held"). What is withheld is the *run's* claim to have completed, because
conservation — the reconciliation GOVERNANCE 2 means by "unless everything
reconciles" — could not run on that page. Cutting a page into predetermined
crops because the structure pass found nothing on it does **not** by itself hold
the run: there the reconciliation ran and honestly found no ink, which is a
complete result.

## Run binding

`config/designator_padding.toml` is sealed into `run.json`'s `config_digest`
alongside `config/models.toml`, `config/pdf_render.toml`,
`config/recovery.toml` and `config/decoding.toml` (whose `[structure]` section
the live pass reads, rechecked under the sealed `decoding` name at the point of
use). Padding decides how many pixels a witness is actually
shown, so two runs under different padding cut different crop bytes; without the
binding, one run id could be reused across a padding change and produce a second
geometry under the first run's name. The stage reads the policy from the run's
own `--designator-padding-config` argument, so the bytes it pads with and the
bytes the run sealed are the same file by construction.

The same file, however, is not the same *bytes*: `open_context` reads it to
check the run's binding and `initial_pass` reads it again to get the values it
pads with, and a rewrite landing between those two reads passed everything —
the binding comparison saw the old bytes, every crop was cut from the new ones,
and the run exited complete having captured every act under a policy `run.json`
never sealed. `StageContext.require_sealed_config` closes that window at the
point of use: `run_config_bindings` now hands back the digest each configuration
file's binding was taken over, and the stage refuses unless the bytes it read
are the bytes that were bound. The precondition is local write access to the
configuration path during a run — the same class of precondition as the sealed-
page-pixel re-check above, and closed the same way.

## Recovery boundary

`--operation recover --act <id> --recovery-request <id>` is the only recovery
entry point, and it is real-ingress-blind by construction: a recrop's geometry
comes from the fixture's own declared rectangle (`context.fixture["act"]`),
which a real submission does not carry. `main` refuses `--operation recover`
against a real submission by name, before touching `--act` or
`--recovery-request`, rather than let the generic fixture-accessor refusal
(`common/stage.py`) stand in for it. Bounded recovery from a real submission
is not built; when it is, this stage will need a source for a recrop's
geometry that a real page can supply. The request must be the exact current, digest-checked Recensor
request for that act, its next ordinal, its Perlectio evidence, and the
run-bound `config/recovery.toml` policy, including its reconciled total and
per-kind budget counters. This stage fulfils `fallback-recrop` only; it refuses a
`page-level-reread` rather than treating it as another crop, because a different
recovery kind names a different owning stage and not a substitute crop. A command
without that exact request does not cut a crop. The orchestrator, not this stage,
decides whether such a request is outstanding and invokes this program.

**A recrop must add coverage, and that is checked over pixels rather than over
transform identity.** The requested rectangle is validated against its page
first, so a degenerate or off-page one is refused as a rectangle rather than
measured as one. Then two refusals in order: the rectangle already cut for this
act under the same transform is a duplicate (identical crop bytes, identical
`region_id`, a re-read rather than a recovery); and a rectangle every one of
whose pixels already lies inside the union of the act's existing regions *on
that page* recovers nothing at all. `_uncovered_area` computes that union
exactly, through the same `_subtract_rectangle` fold the fallback tiling already
uses, rather than by pairwise containment — two regions of one act can jointly
cover a rectangle neither covers alone, and the guard and the tiling must not
come to disagree about what "already covered" means. The
union is taken over each region's final `transform["bounds"]` — the capture
rectangle actually cut and shown — and not over `raw_bounds`, or a recrop back
inside a proposal's own padding would count as recovery. It is scoped to the
page being recropped, because a continuation region shares the act's identity
and none of its geometry.

The second refusal is what GOVERNANCE 11 ("Recovery exists for **completeness
and coverage**") and ARCHITECTURE's "fallback or **expanded** recrop" have
always said. Until it existed, `proof/skeleton_fixture.toml` declared act a1's
recovery rectangle as `16,16,168,88` against a padded proposal capture rect of
`12,15,188,99` — strictly inside it. The `review` scenario, the walking
skeleton's single proof that bounded recovery works, spent its whole
`fallback_recrop` budget on a crop that recovered not one pixel, and the export
then carried a `witness_covered: false` caveat ("ink a recovery uncovers was
never shown to them") over pixels every witness had already seen. Refused rather
than flagged: a spent recovery budget does not come back, so accepting the
recrop costs the act its one recorded chance to widen its crop.

Two consequences worth stating plainly. **The refusal is fatal, like every other
refusal in `recovery_pass`** — a held act, a wrong recovery kind, a stale
ordinal, an off-page rectangle — so a Recensor that asks for a recrop it cannot
have stops the run rather than holding the act. That is this stage's established
failure mode for an impossible request and not a new severity class; a recovery
invocation publishes no holds. **And an act whose capture rectangle has already
clamped to all four page edges can never satisfy this check**, because there is
no page pixel left for it to recover. That is the honest answer rather than a
defect: the recrop it was asked for does not exist. It is reachable on a real
page — the shipped right/bottom padding clamps a large act to the page edge, and
in the synthetic fixture a1's capture rectangle already reaches `x=200` — so the
first real corpus should expect it.

## Consumers

Attestatores reads proposal regions only and records which pixels each witness
saw. Perlector may read recovery regions but marks them witness-uncovered unless
a Testimonium actually names them. Recensor, Archetypus, and Armarium use the
proposal seal as the conserved act denominator; none may manufacture a new act
or choose among competing crops.

A **page-fallback** act is read exactly like any other proposed act: its tiles
are ordinary `origin="proposal"` regions of one act in the proposal seal, so the
Attestatores witness each one and the Perlector reads through all of them,
without any of them needing to treat the grid as a detection. That is the point —
the crops exist so the strong instruments can decide whether the page is blank.
The scenario-only ink-free fixture page proves this path through a real orchestrator
run. All configured witnesses publish completed empty Testimonia over its tiles;
their emptiness is declared by the fixture (`fixture://` receipts), not measured.
The fixture Perlector separately decodes every delivered tile and returns
`no-readable-text` only when none contains a pixel at the conservation margin
below the page's inferred background. The Recensor confirms the blank only after
those declared witness reports and the Perlector's observed-empty reading exist.

`act-group`, `secondary-provenance`, `secondary-proposal`, `rescue-crop` and
`structure-status` have no consumer downstream of this stage today.
`structure-status` is the exception in one direction only: it is not *read* by a
later stage, but `common/stage.py::_verify_page_fallback_act_row` reads it back
within this stage's own denominator check, as the independent evidence that a
page-fallback act's premise is true. The two
secondary kinds are explicitly held for review and make the Designator exit
held rather than being mistaken for accepted authority. No other stage reads
them, and every
other stage's own reader of this stage's manifest already filters to the one or
two kinds it actually wants (`entry["kind"] == "region"`, `== "hold"`), so a new
kind appearing here changes nothing for them by construction.

**`conservation` is the one exception**: Recensor's
`geometry_coverage_inputs` reads those records directly and refuses a page that
carries a non-held expected act but no conservation record. Every residual the
record names also produces a `kind="hold"` record and an expected-act row (see
above), and *that* is read exactly like any other hold — by Recensor's
`designator_hold`, and downstream of it by every stage that already knows how
to carry a held act to its terminal category.

## Cost, and where it is unbounded

Two of this stage's per-page reads were once-per-page walks of the whole
artifact tree, which made ordinary input quadratic in itself: conservation
asked for the claimed regions on each page separately (pages × regions) and
`common/stage.py`'s residual-row check walked the tree once per extra seal row
(residual acts × artifacts). Both are one pass now
(`_claimed_regions_by_page`, `_designator_holds_by_subject`). Neither changed
what is computed.

**The residual denominator is unbounded in the accounting and bounded on the
page.** Every residual component still enters the reconciliation regardless of
size — "every residual region is accounted regardless of size" is spec 06's own
sentence and a size floor in the accounting is GOVERNANCE 10's named defect, so
`conservation.reconcile` returns all of them and no ink leaves the measurement.
What is bounded is how many of them become *separate review items*. A page whose
reconciliation counts more components than the sealed
`max_residual_components` allows becomes one `page-residual` held act instead of
that many, and its record says so in `residual_enumeration`.

The number this closes is measured on this build: a synthetic A4 page at 300 dpi
with 3% scattered ink reconciles to ~60,000 residual components (this tree now
measures ~254,000 at a 33px pitch), each of which used to mint its own held act,
hold artifact and seal row. `operations/operator/review.py` refuses a run past
`MAX_REVIEW_ITEMS` by name, so one such page made every *other* page's findings
unreadable on the only surface a person uses.

**The bound is per page and the console's ceiling is per run — a named
remainder, not a thing this bound does.** What it buys is that no single page
can make a run unopenable on its own. Thirty pages each sitting just inside
`max_residual_components` still carry a run past the console's 50,000 items and
still meet that refusal, by name, at the console. Nothing in the pipeline counts
the run-wide total while a run is produced, and the Designator deliberately does
not: the queue an operator opens is assembled in the Armarium's export from
every stage's review items, so a total counted in this stage would be a fraction
of the run's presented as the whole of it, which GOVERNANCE 10 forbids more
firmly than it wants the check. A run-wide accounting belongs where the queue is
assembled if it is wanted; until then the ceiling is enforced at the console
against the queue it actually reads.

**The secondary rescue pass is bounded the same way, on the same page.**
`max_secondary_proposals` caps how many rescue candidates one page cuts and
holds separately; past it the pass becomes one held `secondary-proposal` record
naming the count, the bound and the sealed grouping digest, and cuts no crop.
That path mints a PNG blob per candidate as well as two records, so it is the
more expensive of the stage's two per-page enumerations, and leaving it
unbounded would have rebuilt the unopenable run by the one route the residual
bound does not cover. `secondary_enumeration` is a closed pair on both shapes,
for the reason `residual_enumeration` is one on every conservation record.
`pipeline/2_designator/test_page_residual_bound.py` carries that case, marked
`full` because the pure-Python structure pass takes ~100 seconds at that size.

**The honest cost, stated as a cost.** On a withheld page the per-component
rectangles are not in any artifact. They stay recomputable from the sealed page
bytes under the sealed conservation policy — the same reasoning the pipeline
already uses for the exact image a model was shown — and both the count and the
bound are on the hold and on the record. But a reviewer cannot open that page's
evidence and read off where the unclaimed ink was. The alternative is a payload
of the order of megabytes per page that nobody reads and that makes the run
unopenable for a different reason.

**The retirement condition, written down now.** The bound counts review items,
and the number of review items is a property of how well the structure pass
performed as much as of the page. Today's pass is one modal-background ink scan
at a 20-level margin, so a run in which *every* page carries a `page-residual`
hold is the legible first-run signal that the structure pass does not work on
this corpus — a true finding delivered on run one. But if roadmap item 4 lands a
real structural Designator and real pages still trip the bound, the bound is
measuring the wrong thing and must be revisited rather than raised. The first
real run's `page-residual` count is the measurement that settles it.

There is no ordinal arithmetic left to bound: residual identities are
class-namespaced (`act_id(page_id, "residual", bounds)`), so disjointness from
proposals and the page-fallback act is by construction rather than by a
reserved ordinal floor, and the paragraph above is still the real operating
limit.

## Continuation ownership

**The relation is the Recensor's, and this stage proposes.** Spec 06's own test 3
and spec 09 both name the Recensor's link "the authoritative relation" for a
continuation, and that is what the tree does.
`pipeline/5_recensor/run.py::recensor_continuation_link` derives the Recensor's
own continuation fact from the proposal regions' page ordinals alone, and
`reconcile_continuation` refuses a seal that denies a continuation its own
evidence already shows — "the Recensor's own reconciliation is the authoritative
continuation fact and may not be overridden by the seal". The reverse direction,
a seal claiming a continuation the Recensor's link cannot corroborate, is a
recorded `continuation_shortfall` that holds the act rather than establishing it.

This stage's proposal-seal `has_continuation` flag is therefore a proposal, and
`grouping.find_continuation_candidate`'s independent geometric check is recorded
on `act-group` as `continuation.geometric_corroboration` — evidence for whoever
reads the act, never a gate here.

## What this handoff does not settle

**RecordGold cannot close padding calibration, and `calibrated_for_this_corpus`
stays `false` regardless of how many of its pages are fetched.**
`padding_calibration.py` needs `(detected, true_content)` pairs on this
project's own material; RecordGold supplies `true_content` only: the live
structure pass below can now mark out a real page, but no structure chair has
been served over fetched RecordGold pages, so no `detected` half of this
project's own exists to pair with it. Running `structure.py` /
`grouping.py` offline over those pages and calibrating against that output is
an honest route, but the result it would produce describes the walking
skeleton's ink-scan stand-in, not this project's own structure model, and
must be recorded as such rather than folded into the flag silently. The flag
means "calibrated against this project's own pages" — swapping one
third-party corpus's fractions (the config's current French-register
provenance) for another third-party corpus's fractions, however cleaner or
more plentiful, does not make that true. Val supplies 769 usable reference
rectangles across 113 pages (784 records less the 15 refused for
`rotation=180`); the ~120 preferred floor counts `(detected, true_content)`
rectangle pairs, not pages, so sample size clears it comfortably. Sample size
was never the obstacle here, provenance is, and provenance is not a number a
larger sample can fix.

**A page-fallback tile's per-tile `rationale` still says "no ink to group"
even on the live path.** The record-level `reason` on `page-fallback` now
tells the live and fixture premises apart (see `kind="page-fallback"` above),
but each tile's own `rationale` string is a `grouping.fallback_tiles`
constant (`grouping.py:395-399`) written for the fixture premise and unaware
of which call site produced it. `grouping.py` is not this unit's owned path.
Whoever owns it should give `fallback_tiles` (or its caller) the same
live/fixture distinction this unit gave `_publish_page_fallback`'s `reason`.

**An unproposed cross-page half act — an ACCEPTED EVIDENCE DEFECT, not a benign
limitation.** The Recensor reconciles only continuations this stage *proposed*, so
an act split across a page break that was never declared produces no finding in
any stage: grouping's geometric detector only corroborates declared
continuations, residual ink misses it whenever the cut covers the visible half,
and truncation signals are single-act.

**State the consequence plainly, because a consumer of this contract must not
read it as acceptable.** Such an act is lost with **no hold and no review item**,
which means a downstream reader cannot distinguish "this act was not there" from
"this act was missed" — the exact discrimination `GOALS.md`'s "a missed act is
worse than a poorly read act" exists to preserve, and the one failure mode
`GOVERNANCE.md` 2 refuses by name. Nothing in this stage's output marks the page
as suspect, so no recovery loop can be aimed at it either.

**This stage did not create the defect and does not close it here.** Unit 9's
Ink Map now records bounded `unclaimed-edge-ink` evidence before proposals, but
does not hold an act: **Unit 14 owns the explicit hold or review outcome** for
the unproposed cross-page half act. The Designator must retain that evidence
path rather than treating an absent proposal as a clean page.

**Until it is closed, no run over real material may be described as having
accounted for every act on a page.** The accounting is honest about what it
measured; it does not measure this.

**Recovery from a structural hold.** Spec 06's test 4 asks for three things: the
page held with a named reason, no silent gap downstream, and "the recovery
operation proposes a replacement region on request". The first two are built and
proven end to end (`test_structure_failure.py`). The third is not, and nothing
here makes it: `recovery_pass` refuses any act the seal holds — "a held act is
terminal and may not be recropped back to life" — which is the landed recovery
contract this stage shares with the Recensor (spec 09), reached through
`common.stage.current_recovery_request`. Making a *structural* hold recoverable
while an *unsealed-page* hold stays terminal means distinguishing the two in a
contract owned across two stages, and that is a decision for Tyrel rather than a
distinction to introduce quietly here.

**Captured structure text — settled, not a gap.** Spec 06's contracts section
said the structure pass's transcription is "captured and handed to the
Attestatores stage as a Testimonium rather than re-run". Tyrel ruled otherwise
on 2026-09-02: every witness runs its own pass, and Attestator 1 is served and
read in its own call. The live pass retains the chair's transcription under
custody as its own evidence (`structure-answer.raw_response_ref`,
`custody_ref`) and hands nothing to the witnesses; the Attestatores stage is
untouched by the live Designator and reads a served seal under its own rows.

**`infer_background`'s majority-paper assumption is now checked from both
sides, and a page that fails the check is read but not counted.** The premise is
that a scanned register page is overwhelmingly paper, so its modal pixel is the
paper colour. Two shapes break it and both are refusals now. A page where ink is
the numeric majority — heavy staining, bleed-through, an inverted or
under-exposed scan — has a mode *darker than its own mean*, caught by
`mode * count >= total`. A uniformly dark page has `mode == mean`, so that
comparison passes exactly; it is caught instead by requiring the mode to be
light enough to express an ink threshold at all (`mode >= PRIMARY_MARGIN`),
because below that no 8-bit sample could ever be counted as ink and "zero ink"
would be arithmetic rather than a measurement. Solid black used to infer a
background of 0, threshold -20, and reconcile to zero ink on a visibly black
page — and on a page with no declared act, nothing caught it and the run exited
`EXIT_COMPLETE`.

What follows a refusal is deliberately *not* a substituted threshold. The page
is cut into predetermined crops and sent downstream to be read, its
`structure-status` records `background_source: "not-inferable"`, its
`conservation` record carries `ink_measurable: false` with null counts, and the
run exits `EXIT_HELD`. **The cost, stated plainly:** on such a page this stage
contributes no ink accounting at all, so the independent coverage proof that
would catch a missed region there does not run, and the only thing standing
between that page and a missed act is that every part of it was cut and sent to
be read. That is a real reduction in this stage's own instrumentation, accepted
because the alternative — the page-mean substitution that shipped before — was
worse in a way that is hard to see: it does not fail, it reports. On the
inverted scan above it classifies dark paper as ink and mints held acts over
background at page scale, and a reviewer reading `residual_pixel_count` has no
way to know the number is meaningless.

What would close it properly is a background heuristic that does not depend
solely on global modal frequency, or explicit inverted-scan handling that scans
for pixels *lighter* than the paper. Border-sampling was considered and is
itself an uncalibrated guess for a photographed register page; inversion
handling is a real feature with real calibration risk, and this walking
skeleton's ink-scan is explicitly a stand-in for a real structure model rather
than a hardened production detector — calibrating one is the same kind of
decision `padding_calibration.py` already declines to make without a gold set.
Named here rather than decided by a guess.

**A conservation residual's reported bounds can span claimed territory.**
`conservation.reconcile` labels connected components over the *residual*
pixel set alone, using `structure.label_components`'s ordinary gap-tolerant
adjacency (a few pixels, meant to bridge a pen stroke's own gaps). That
adjacency test knows nothing about `claimed_bounds`: two residual patches
separated by a claimed rectangle narrower than the gap tolerance are unioned
into one component, and the resulting `bounds` (the member pixels' own
min/max extent) is a bounding box that encloses the claimed rectangle
sitting between them, even though every pixel actually inside that claimed
rectangle is correctly excluded from the residual set and from the pixel
count. `claimed_pixel_count + residual_pixel_count == total_ink_pixel_count`
still holds exactly — no ink is lost or double-counted — and no crop is ever
cut from a residual's bounds (a residual is `held`, never `proposed`), so
this is a review-clarity and `review_priority` defect rather than an
accounting one: a reviewer reading a residual's `bounds` can see a rectangle
that visually overlaps an already-claimed act's own crop, and a merge across
claimed territory can inflate a residual over `review_priority`'s size
threshold that would otherwise sit below it. Closing this properly means a
claimed-aware residual labeling pass rather than a change to the shared
`label_components` that `structure.py`'s own full-page scan also depends on
and has no notion of "claimed" to give — a change worth its own design and
test pass rather than folding into this build's repair commits. Named here
rather than fixed quietly or left undiscovered.

**Conservation uses `SECONDARY_MARGIN`, not the primary proposer's threshold.**
The secondary proposer is optional, but its declared sensitivity is still the
most inclusive threshold this stage has. Reconciliation therefore counts the
faint band that `primary_scan` does not propose and mints it as residual held
evidence when no crop claims it. A configured secondary chair may additionally
publish a review-only rescue crop over the same area; that changes no authority
decision and no pixel escapes the conservation denominator when the chair is
absent. This closes the silent `EXIT_COMPLETE` path found in review on
2026-08-10; the remaining calibration limit is the unmeasured derivation of both
thresholds recorded below.

**The grouping and scanning thresholds are sealed policy now, and the inventory
has been wrong twice.** `config/designator_grouping.toml` carries them with the
padding config's own provenance schema and the same honest
`calibrated_for_this_corpus = false`; `grouping_config.py` loads it,
`run.py::_analyze_page` resolves it against *each page's own* width and height,
and the modules take the resolved pixel integers as required keyword arguments
with no defaults left to fall back on. The digest is sealed as
`designator-grouping` and rechecked at that point of use.

The count went eight → nine → eleven, and both corrections are worth keeping
here because both were found by reading rather than by a test. The ninth was
`conservation.DEFAULT_REVIEW_PRIORITY_MIN_DIMENSION_PX`, which orders review
rather than filtering it, but at 2480×3508 every residual over 6px on either
axis is "high", so the ordering silently stopped working. The tenth and
eleventh were `grouping.DEFAULT_FALLBACK_BANDS` (4) and
`DEFAULT_FALLBACK_OVERLAP_PX` (8), which the sweep left behind while `run.py`'s
own docstring claimed no module in the stage carried a threshold any more. They
are the two that decide the crop rectangles which actually reach the
Attestatores and the Perlector on a page nothing was found on — an 8px overlap
is a comfortable band on a 260px fixture and a hairline on a 3508px scan — so
they were the worst two to have missed. `fallback_overlap_bp` is a basis point
of the page's own height like the others; `fallback_bands` is a bare count,
because a page twice as tall gets bands twice as tall rather than twice as many.

**A twelfth candidate was found and deleted rather than sealed, and the count
stays eleven.** `grouping.find_continuation_candidate` also carried a
`column_overlap_px` slack defaulting to `0` — horizontal tolerance on the test
that decides whether page A's trailing group and page B's leading group share a
column, and so whether an act is judged to run across a page break. No caller
ever passed it, so every real continuation decision ran at 0px under a value in
no config, on no `structure-status` record and in no inventory, which is exactly
the shape of the ninth/tenth/eleventh above. The parameter is gone. What was
being defaulted to zero is not a threshold but the absence of one: with no
tolerance the test is plain interval intersection, which — unlike an absolute
8px overlap — means the same thing on a 3508px scan as on a 260px fixture,
because there is no length in it to scale. Zero is also the strict end, and this
check is recorded rather than gating, so the direction of a miss is a `false` on
an act-group record, never a lost continuation. Slack here would be a page-width
proportion (`margin_bp`'s basis, not the six height-based fields'), and if a real
corpus ever shows consecutive pages need it, it enters the config as a basis
point and arrives as a required keyword.
`test_grouping.py::test_find_continuation_candidate_shares_a_column_with_no_slack_at_all`
holds all three halves down: touching x-ranges do not corroborate, one shared
pixel does, and the keyword is refused. `_x_range` reports the half-open pixel
span `[x, x+w)`, so two ranges that meet exactly at an endpoint share no pixel
column at all — a review-round correction to this same test (`grouping.py`'s
`find_continuation_candidate`) after it was first found asserting the reverse,
because `_intervals_overlap`'s boundary-inclusive comparison is right for the
*tolerance* call sites (a gap up to the reach is meant to still attach) and
wrong for this zero-tolerance one, which now tests non-empty intersection
directly instead of going through that shared helper.

Seven fields are integer basis points of a page dimension — `margin_bp` of the
page's **width**, the other six of its **height** — and each resolves
bit-identically to its retired constant on the 200×260 fixture pages, so no
fixture geometry changed. Three are bare counts: `max_residual_components`,
`max_secondary_proposals` and `fallback_bands`.

Two do **not** move and never will. `structure.PRIMARY_MARGIN` and
`SECONDARY_MARGIN` are 8-bit ink-intensity offsets, not geometry;
`common/test_designator_recensor_ink_calibration.py` is an AST pin that reads
`SECONDARY_MARGIN` as a source literal and cross-checks it against the
Recensor's own contrast constant, and a per-run value would make that
cross-stage invariant unenforceable statically. The config's closed schema
refuses both names wherever they are written.

**`gap_tolerance_px` stays absolute at 3, and must never be scaled — do not
"fix" this by reflex.** It is a stroke-connectivity radius, not a page-layout
proportion: scaled to a 3508-tall page it becomes ~41px, which bridges
inter-word gaps and changes what "connected" means, and
`structure.label_components` builds a Chebyshev offset list of radius
`gap + 1`, so the labeller's cost is quadratic in it and would grow with the
cube of page scale. Its correct value is a function of scan resolution, not of
page dimension, and no measurement of that relationship exists here yet. It is
the one threshold this build cannot honestly set, and it is set by decision
rather than by oversight. Related and separate: `structure.ink_pixels` builds a
Python set over every pixel (8.7 million on an A4 page at 300 dpi) before
labelling starts, which roadmap item 4 replaces rather than optimises.

Original finding: review, 2026-08-10.

**This stage builds occlusion geometry and publishes none of it.**
`geometry_layer.occlusion_envelope` derives an occlusion envelope, and
`run.py` seals no `kind="occlusion"` artifact — no stage in the pipeline does.
The Recensor's cross-capture visibility survey reads exactly that kind
(`pipeline/5_recensor/run.py::occlusion_records_by_page`), so on every run today
every capture row records `act-visibility-survey-absent` and **no run has
measured whether an act's surface was visible in the captures that show it.**

The absence is named rather than silent, which is the difference that matters
for a consumer: the code is on every capture row, and the Recensor deliberately
routes an absent instrument like a clean one rather than holding an act on an
instrument that never ran — absence is not a measured shortfall, and inventing
one would be a metric that was not measured passing as one. What a consumer may
not do is read a present `cross_capture_coverage` field as evidence that
visibility was checked.

Closing it is stage integration, not a Recensor change: it needs a producer here
and a settled contract for what an occlusion record seals (page identity,
polygon in that page's own coordinate space, and the `z_relationship` the
Recensor already refuses to infer). Named in review of PR #78, 2026-08-31.

## Who wrote what (from the dispatch record, not the trailers)

Derived from the session's workflow scripts (realpage-ua-ub-*, realpage-uc-sealing-*,
realpage-ue-verifier-*, realpage-unit-d-*, realpage-unit-f-*), which record the model
each seat was dispatched as. Commit trailers are self-reported by the seats and some
are wrong (seats copied the host's "Fable 5.1" line); this table is authoritative.
The Fable seat was the host orchestrator and wrote no unit code.

| unit | built by | verified by | fixed by |
|---|---|---|---|
| A config + loader | Sonnet 5 | Opus 5 | Sonnet 5 |
| B pure modules lose defaults | Sonnet 5 | Opus 5 | Sonnet 5 |
| C sealing wiring | Opus 5 | Opus 5 | Sonnet 5 |
| E act class + consumer verifier | Opus 5 | Opus 5 | Sonnet 5 |
| D Designator behaviour + Recensor withheld branch | Opus 5 | Opus 5 | Sonnet 5 |
| F Door refusal, geometry, pins | Opus 5 | Opus 5 | Sonnet 5 |
