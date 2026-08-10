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
act_key, declared_bounds, detected_bounds
body_member_count, anchor_count, rationale
continuation = {declared_bounds, detected_bounds, rationale, geometric_corroboration} | null
```

`rationale` is one of a small set of code-generated strings naming which
grouping rule fired (a single anchor, a brace linking two acts, an isolated
marginal note, a leading fragment with no anchor) — never a reading of the ink.

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
ownership itself is unresolved between specs 06 and 09** — see "What this
handoff does not settle" below.

## `kind="conservation"`

One record per sealed page this run reached, subject-keyed to the page identity,
no attempt binding. The independent coverage proof: every ink pixel at
`structure.PRIMARY_MARGIN` sensitivity — not, despite this section's own past
phrasing, every ink pixel the page's decoded bytes actually contain; see "what
this handoff does not settle" below — reconciled against the *final* (padded)
proposal crops actually cut on it — never against what grouping *claims* to have
found, which is the gap an independent second read of the old pipeline's own
conservation logic named precisely (`/stage/70_gpt_review/ASSESSMENT.md:172-173`
in the window: it "proves coverage of units already emitted by a structural
model. It cannot prove that the model did not miss ink entirely." An earlier
draft of this sentence cited `MISSING.md`, which carries the same idea in
different words at line 319 but is not where this exact sentence lives;
corrected here after a second window read).

```text
page_ordinal
total_ink_pixel_count, claimed_pixel_count, residual_pixel_count
residual_components = [{bounds, pixel_count, review_priority}]
```

`claimed_pixel_count + residual_pixel_count == total_ink_pixel_count` always,
by construction. **Every residual region is accounted regardless of size**:
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
`residual_bounds` rather than trusted because the seal names it. This is a
strictly additive change to the shared function: for every existing scenario
in this fixture, no page has any residual ink today, so the new code path
never fires and every prior digest is unchanged — proven by the full test
suite, not merely argued (`pipeline/orchestrator/test_orchestrator_acceptance.py`'s
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
the optional *seat* possible without a mandatory *code path* ever being
skippable. This build's reading: the seat is optional, its resolution is not,
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
written. Configuring an optional, explicitly non-authoritative seat therefore
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
provenance (the resolved Designator chair)
```

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

`EXIT_COMPLETE` (0) only when the seal holds nothing, no page was held, and no
secondary rescue awaits review. Anything held — an act, a page, ink no
authoritative crop claimed, or a non-authoritative rescue — exits `EXIT_HELD` (3).
The exit code is the one signal an operator reads without opening the tree, and
a 0 over a hold is a partial result wearing "complete" (GOVERNANCE 2). Act
holds are computed from the seal's own rows; secondary holds are computed from
the rescue records published in the same pass because that evidence deliberately
does not enter the authority. A recovery invocation cuts one requested crop and
exits 0 or fails; it publishes no holds.

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

## Consumers

Attestatores reads proposal regions only and records which pixels each witness
saw. Perlector may read recovery regions but marks them witness-uncovered unless
a Testimonium actually names them. Recensor, Archetypus, and Armarium use the
proposal seal as the conserved act denominator; none may manufacture a new act
or choose among competing crops.

`act-group`, `secondary-provenance`, `secondary-proposal`, `rescue-crop` and
`structure-status` have no consumer downstream of this stage today. The two
secondary kinds are explicitly held for review and make the Designator exit
held rather than being mistaken for accepted authority. No other stage reads
them, and every
other stage's own reader of this stage's manifest already filters to the one or
two kinds it actually wants (`entry["kind"] == "region"`, `== "hold"`), so a new
kind appearing here changes nothing for them by construction.

**`conservation` is the one exception**, and only indirectly: no stage reads a
`kind="conservation"` record itself, but every residual it finds now also
produces a `kind="hold"` record and an expected-act row (see above), and *that*
is read exactly like any other hold — by Recensor's `designator_hold`, and
downstream of it by every stage that already knows how to carry a held act to
its terminal category. The conservation artifact remains what it always was,
an audit trail nothing reads directly; what changed is that it is no longer the
only trace a residual leaves.

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

## What this handoff does not settle

**Continuation ownership.** Spec 06's own test 3 and spec 09 both say the
Recensor's link is "the authoritative relation" for a continuation. In this
tree, this stage's own proposal-seal `has_continuation` flag — derived from
which regions were actually cut, per the module docstring above — is the only
continuation fact that exists anywhere; `pipeline/5_recensor/run.py` only ever
checks a *shortfall* against it (`continuation_shortfall`), it does not itself
establish or override the relation. Closing this gap means changing which
stage's record is authoritative, which is a decision spanning two specs and two
stages' contracts, not something owned here. It is named, not silently
resolved: `grouping.find_continuation_candidate`'s independent geometric check
is recorded on `act-group` as `continuation.geometric_corroboration` precisely
so the fact is visible for whoever does settle it, without this build changing
`pipeline/5_recensor/run.py` to act on it.

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

**`infer_background`'s majority-paper assumption is unvalidated, not enforced.**
`structure.infer_background` takes a decoded page's single most common pixel
value as its paper colour, and `run.py` caches that one value and feeds it,
unchanged, into both `structure.primary_scan`/`secondary_scan` *and*
`conservation.reconcile`. A page where ink genuinely is the numeric majority —
heavy staining, bleed-through, an inverted or badly under-exposed scan — gets
its ink classified as background: the structure pass finds no components, and
conservation, sharing the same wrong background, reconciles to
`total_ink_pixel_count == 0` and mints no residual. On a page with a declared
act, `_match_structural_group` happens to catch this as a hard `ContractError`
(no detected group covers the declared bounds); on a page with **no** declared
act — the exact case conservation exists to cover — nothing catches it, and
the run can exit `EXIT_COMPLETE` having silently accounted for zero ink on a
visibly inked page. This build found the gap (2026-08-10 review) and did not
close it: a real fix needs either a background heuristic that does not depend
solely on global modal frequency (border-sampling was considered, but is
itself an uncalibrated guess for a photographed register page) or a
cross-check reconciling the two independent scans use of it, and this walking
skeleton's ink-scan is explicitly a stand-in for a real structure model, not
a hardened production detector — calibrating one is the same kind of decision
`padding_calibration.py` already declines to make without a gold set. Named
here rather than fixed quietly or left undiscovered.

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

**Conservation's own denominator is `PRIMARY_MARGIN`-sensitive, not the page's
whole ink.** `_publish_conservation_and_secondary` calls `conservation.reconcile`
with no `margin` override, so it defaults to `structure.PRIMARY_MARGIN` — the
same threshold `primary_scan` uses, and strictly less sensitive than
`SECONDARY_MARGIN`. Ink in the band between the two margins (closer to the
page's background value than `PRIMARY_MARGIN` but past `SECONDARY_MARGIN`) is
outside `total_ink_pixel_count` and can never become a residual: with the
secondary chair absent (the shipped default), such ink is recorded nowhere at
all, and a run can exit `EXIT_COMPLETE` over it; with the chair configured, it
can only surface as a held rescue crop, outside the conservation arithmetic
entirely. This is bounded by how faint the synthetic fixtures' ink is today
(high-contrast, well past `PRIMARY_MARGIN`), so it changes no digest and no
test outcome in this build — but faded ink and pencil marginalia are exactly
what a real register page carries, and this is the accounting layer built
specifically so faint marks are not lost silently (GOALS 1, GOVERNANCE 2).
Closing it means a decision about which margin conservation should reconcile
at, which changes review volume and is not this build's to make quietly; named
here rather than left for a reader to discover against a real page. Found in
review, 2026-08-10.
