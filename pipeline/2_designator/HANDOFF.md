# Designator — handoff

# Stage-completion seal

Before this producer's final manifest it publishes one `decode-environment` and
one `stage-seal`. The seal witnesses this pass's disk inventory, blob contents,
run `config_digest` and `register_digest`, and `(kind, outcome)` census. An exit
held after publishing stage evidence seals it (holds remain in its census); a
pass held or refused before publishing stage evidence does not seal, so the
successor correctly refuses the missing boundary. Every difference in decoders,
platform, machine, `decode_paths_used`, and `produced_pixels` is reported by
field or decoder name. A valid difference is report-only and never refuses;
Unit 17 owns any fatal policy.

Seals are compared as the SET the stored inventory names, on both sides of the
boundary: the producer refuses to re-seal, and the successor refuses to read,
when any named seal is no longer on disk. Ordinals are the contiguous run 1..N,
so removing the latest leaves a prefix that still looks whole — and the earlier
statement would then answer for a boundary it never witnessed.
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

## Scope and input boundary

Before cutting anything, the Designator reconciles `run.json`'s submitted
filename ledger with every Exemplar page outcome and the one self-hashed corpus
seal. A sealed page's Door admission and pixel blob are checked again before its
pixels are cropped. The check is deliberately before the first region write.

The current structural proposer is the declared synthetic walking skeleton, and
it is a real visual pass rather than a fixture value standing in for one: every
sealed page is decoded and independently ink-scanned (`structure.py`), and the
result is grouped into acts by geometry and structural cues alone (`grouping.py`)
— no text, no election among candidates. On real ingress this program performs
the Exemplar boundary check and then stops with a refusal: it does not invent
proposals, holds, or text in place of a real structure-pass *model* (the ink
scan above stands in for the model call itself, not for the geometric reasoning
built on top of it, which runs for real either way).

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
`raw_bounds` is the *structural* rectangle the sealed synthetic fixture
declared and act identity is bound to (`common/contracts/identities.py::act_bindings`).
The real structure-model path is not built; there, this would instead be the
structure pass's own authoritative rectangle. The current visual grouping is
recorded separately as `act-group.detected_bounds` and must reconcile against
the fixture rectangle rather than silently replacing it. A
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
structure_evidence ("detected" | "fallback-tiles"), detected_bounds | null
body_member_count, anchor_count, rationale
continuation = {declared_bounds, structure_evidence, detected_bounds | null,
                body_member_count, anchor_count, rationale,
                geometric_corroboration} | null
```

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

One record per sealed page the structure pass found **no ink at all** on,
subject-keyed to the one act that page's predetermined crops belong to. Where a
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
act_key ("page-fallback:<ordinal>"), page_id, page_ordinal, page_bounds
fallback_ordinal (the reserved FALLBACK_PAGE_ACT_ORDINAL)
tile_count, tiles = [{bounds, rationale}]
reason
provenance (the resolved Designator chair)
```

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
Its identity is `act_bindings(page_id, FALLBACK_PAGE_ACT_ORDINAL, page_bounds)`,
using the one ordinal reserved in `common/stage.py` beside the residual space —
which is now bounded below (`RESIDUAL_ACT_ORDINAL_FLOOR`) so the two minted-act
ordinal spaces are disjoint by construction rather than by an argument about
which values are unlikely to be reached. `common/stage.py::_verify_page_fallback_act_row`
recomputes that identity and then reads this record's single input — the page's
own `structure-status`, through the digest-checked reference hop — to confirm
that page really does record `structure_evidence == "fallback-tiles"`. A
fallback act minted over a page the structure pass detected regions on is
refused there, so the extra denominator row proves its premise rather than
asserting it.

A fallback tile's region carries `padding: null`, like a recovery crop and for
the same reason: the tile *is* the final rectangle, computed from the page's own
dimensions with `grouping.DEFAULT_FALLBACK_OVERLAP_PX` already built into it.
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
(`pipeline/2_designator/run.py:1534`), and the counts and residual components are
published from that scan (`:1544-1570`). Ink that no crop covers is not merely counted:
on a successful pass every residual component becomes its own held act
(`_publish_residual_holds`, `pipeline/2_designator/run.py:1277-1330`), so a mark
structural grouping never proposed is visible rather than absent. If two components
share a bounding box and therefore cannot receive distinct act identities, the stage
refuses before minting either instead of collapsing their evidence (`:1300-1311`).

What this cannot prove is therefore narrower than "coverage of the cuts". Ink fainter
than `background - SECONDARY_MARGIN`, or a mark with no contrast against its background
at all, never entered the denominator, so the accounting cannot establish that such a
mark was not missed. A page whose background cannot be inferred is recorded
`ink_measurable: false` with its reason rather than counted at a substituted divider.

```text
page_ordinal, background_source, background_value | null
ink_measurable, reason | null
total_ink_pixel_count | null, claimed_pixel_count | null, residual_pixel_count | null
residual_components = [{bounds, pixel_count, review_priority}]
```

`claimed_pixel_count + residual_pixel_count == total_ink_pixel_count` always,
by construction, whenever `ink_measurable` is true.

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
(`_publish_residual_holds`, `common/stage.py::residual_act_ordinal` and
`_verify_residual_act_rows`), closing the gap this section used to name as
unimplemented. `expected_acts` no longer requires the seal's denominator to
equal the fixture's declared acts exactly — every fixture act is still a floor
that must appear, but the seal may also carry additional rows a residual
minted, each `held` from the moment it exists (never `proposed`: nothing
witnessed or read ink no structural pass claimed), each independently
recomputable from its own hold record's `residual_ordinal` and
`residual_bounds` rather than trusted because the seal names it. This
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
construction: `residual_act_ordinal(index) = -(index + 1)`, and a structure
pass's own proposal ordinal is always non-negative, so the two act-identity
spaces can never collide, present fixture or real one.

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
provenance (the resolved Designator chair)
```

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

A failure is declared per scenario by the fixture's `[[structure_failure]]`
rows, because the walking skeleton has no live structure model that can fail.
Everything downstream of the declaration is real: `structure_failures` refuses
two declarations for one page rather than taking either, and ignores a
declaration naming a page this run never sealed, since the Exemplar's own
refusal already accounts for that loss and a second hold would double-count it.

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
alongside `config/models.toml`, `config/pdf_render.toml` and
`config/recovery.toml`. Padding decides how many pixels a witness is actually
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
entry point. The request must be the exact current, digest-checked Recensor
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

**The residual denominator itself stays unbounded in the input, deliberately.**
Every residual component becomes one held act, one hold artifact and one seal
row, because "every residual region is accounted regardless of size" is spec
06's own sentence and a size floor in the accounting is GOVERNANCE 10's named
defect. A page whose ink is scattered rather than clustered therefore mints
acts in proportion to the scatter: measured on this build, a synthetic A4 page
at 300 dpi with 3% randomly scattered ink reconciles to ~60,000 residual
components in about 3 seconds of labeling, and would mint ~60,000 held acts
that every stage after this one carries to the Armarium's review list. That is
the correct accounting and the wrong ergonomics, and it is a real operating
limit rather than a bug to close by dropping regions: whatever bounds it must
bound the *page* (refuse a page this speckled, visibly and as a hold) rather
than the accounting over one. Named here rather than discovered at scale.

There is now one bound on it, and it is not that one: `residual_act_ordinal`
refuses an index past `RESIDUAL_ACT_ORDINAL_FLOOR` (-2^31), which exists so the
page-fallback act's reserved ordinal sits below the residual space and the two
are disjoint by construction. It is nine orders of magnitude past the ~60,000
above, so it bounds nothing anyone will reach; it is a proof of disjointness,
not an operating limit, and the paragraph above is still the real one.

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

**Captured structure text.** Spec 06's contracts section says the structure
pass's transcription is "captured and handed to the Attestatores stage as a
Testimonium rather than re-run", recorded at capture time with full provenance.
Nothing in this tree does that, and nothing here could: the walking skeleton's
structure pass is an ink scan with no transcription to capture, and the intake
contract it would hand to is spec 07's, which the spec's own exit criteria say
is verified "against the intake schema **as written in Spec 07's text**". This
is a named gap awaiting a real structure model and that intake schema, not an
oversight.

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

**Eight grouping and scanning thresholds ship with no recorded derivation.**
`grouping.DEFAULT_MARGIN_FRACTION`, `DEFAULT_CHAIN_GAP_PX`,
`DEFAULT_ANCHOR_REACH_PX`, `DEFAULT_BRACE_MIN_HEIGHT_PX`,
`DEFAULT_PAGE_EDGE_REACH_PX`, and `structure.DEFAULT_GAP_TOLERANCE_PX`,
`PRIMARY_MARGIN`/`SECONDARY_MARGIN` are tuned by eye against the synthetic
fixtures' own small pages. Five are absolute pixel counts; on a 300-dpi
register scan (~2480×3508) a 6-pixel chain gap or a 30-pixel brace threshold
is not the same approximation it is on a 200×300 synthetic page. The capture
padding in `config/designator_padding.toml` got a required
`[padding.provenance]` schema, a calibration harness, and an honest
"not calibrated for this corpus" flag; these eight constants got a comment.
Not a blocker while the structure pass is a synthetic ink scan with no real
page to calibrate against. Before the pod leg: either move them into a
`config/designator_grouping.toml` with the padding config's own provenance
schema, or express them as fractions of page dimensions rather than absolute
pixels — the padding entry is the model for how. Named here rather than left
uncalibrated and unremarked. Found in review, 2026-08-10.
