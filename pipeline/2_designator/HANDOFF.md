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
`raw_bounds` is the *structural* rectangle grouping proposed, the one act
identity is bound to (`common/contracts/identities.py::act_bindings`); a
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
text, enforced at the schema boundary** by `_refuse_text_fields` — every payload
this stage publishes is walked for a closed set of forbidden content-bearing
keys (`text`, `reported`, `transcription`, `content`, `reading`) before it is
sent to `context.publish`, and this is the one kind the spec names the rule for
by name.

```text
act_key, declared_bounds, detected_bounds
body_member_count, anchor_count, rationale
continuation = {declared_bounds, detected_bounds, rationale, geometric_corroboration} | null
```

`rationale` is one of a small set of code-generated strings naming which
grouping rule fired (a single anchor, a brace linking two acts, an isolated
marginal note, a leading fragment with no anchor) — never a reading of the ink.
`continuation.geometric_corroboration` is `grouping.find_continuation_candidate`'s
independent, page-edge-based check for whether the *geometry itself* looks like
a page-break continuation; it is recorded, never gating, because a declared
continuation whose crops do not happen to touch either page's edge (as in this
stage's own synthetic fixture) is still a genuine continuation. **Continuation
ownership itself is unresolved between specs 06 and 09** — see "What this
handoff does not settle" below.

## `kind="conservation"`

One record per sealed page this run reached, subject-keyed to the page identity,
no attempt binding. The independent coverage proof: every ink pixel this page's
own decoded bytes actually contain, reconciled against the *final* (padded)
proposal crops actually cut on it — never against what grouping *claims* to have
found, which is the gap an independent second read of the old pipeline's own
conservation logic named precisely (`/stage/70_gpt_review/ASSESSMENT.md:172-173`
in the window: it "proves coverage of units already emitted by a structural
model. It cannot prove that the model did not miss ink at all." An earlier draft
of this sentence cited `MISSING.md`, which carries the same idea in different
words at line 319 but is not where this exact sentence lives; corrected here
after a second window read).

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

## `kind="secondary-provenance"` and `kind="secondary-proposal"`

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

`secondary-proposal` exists only when the chair is configured, one record per
rescue candidate the secondary scan finds that no already-claimed proposal
region touches at all — `authoritative: false`, always, at the schema level and
in fact. A candidate touching exactly one claimed act is ordinary coverage, not
a find, and is not published. A candidate touching two or more claimed acts at
once is refused outright rather than published (`_secondary_rescue_candidates`)
— the P0-incident-shaped rule: a detector may add recall, never decide between
two acts or refine either. Removing the proposer changes no `region`,
`act-group`, or `proposal-seal` outcome; only these two kinds disappear.

## `kind="hold"` and `kind="proposal-seal"`

If an act's own page or necessary continuation was not sealed, the Designator
publishes one `held` record rather than omitting the act. Its direct input is the
relevant Exemplar page outcome and its payload names the act key, unsealed page
ordinal, and reason.

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

`act-group`, `secondary-provenance`, and `secondary-proposal` have no consumer
downstream of this stage today. They are evidence, filed the same way a `hold`
is filed — nothing is lost silently — but no other stage reads them, and every
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
