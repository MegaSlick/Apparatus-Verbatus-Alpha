> **Working note — waiting for:** Tyrel's approval. Until he approves it, this is a draft,
> not a decision (workbench rule 5). Once approved it is executed session by session, and
> each session updates the "state" line in its phase. It supersedes
> `workbench/design/REBUILD_PLAN_PROMPT.md`.

# Rebuild run plan — Apparatus Verbatus alpha

Built from the completed audit in `/Users/tyrel/Temp_Stage` (airlock README, KEEP_LIST,
KILL_LIST, DISAGREEMENTS, OPEN_QUESTIONS, seven gap notes with their sweep revisions, and
the 93-invariant test harvest). Drafted 2026-07-27 by Claude Fable 5.

**The audit's own warning binds this plan: build any count from the underlying files, never
from a summary — including this one.** Where this plan states a number, the executing
session verifies it against the source before building on it.

---

## 1. The shape of the rebuild, in one page

Six verbs: **launch, boot, upload, run, export, close down.** Nothing else is in scope.

- **A rebuild, not a migration.** The old code was exposed to everything this project
  exists never to catch; every byte of it is presumed contaminated. It is read for its
  knowledge, and no line enters the sterile tree without a review that can say why it
  exists, what it serves, that it wears this project's names, and that it squares with the
  goals and governance. Outside-written drafts land only in `autoclave/` — tracked, so the
  reviewers read them raw; required empty at every merge, so `main` never carries a draft —
  and leave the tray only through that review.
- **System by system, from cold launch.** Each system is one working session (occasionally
  two), and each session ends in a pull request. Ten phases, roughly fifteen sessions.
- **Two engines, matched to budget.** GPT Sol (≈70% of its weekly budget free) is the
  **heavy reader and triage tool**: before each build session it digests the gap note,
  verdicts and staged files for that system and produces a triage dossier. Claude
  Opus 5 at medium effort (≈20% of budget left this week, resets weekly) is the
  **orchestrator and builder**: it writes the triage prompts, audits the dossiers, builds
  the system, and runs the checks. Claude Fable 5 is reserved for the two or three
  design-heavy sessions where the contract being written is the product.
- **Tests first, from named invariants.** Every session starts by writing the tests for its
  invariants (by number, from `20_sweeps/test-harvest.md`) and ends with them green.
- **Everything a stage writes is private by default.** Output goes under `private/`, and
  nothing is gated on file extension. **A content filter for register text is planned and
  does not exist yet** — see §3 below. Today `pre-commit` refuses credentials, undeclared
  binaries and oversized payloads only; transcribed prose is ordinary text to it, and
  `autoclave/README.md` says so. Until that filter is built, the line-by-line review is the
  only thing standing between register text and a commit. Do not read this bullet as a
  machine that is already watching.
- **Every PR is small enough to review.** CodeRabbit reviews each; the reviewer pass runs
  per push; Tyrel merges. No agent pushes or merges, ever.

The plan front-loads decisions to the session where they bite (§4), names the pivot rules
before anything goes off the rails (§8), and ends with an end-to-end proof on a small real
corpus before anything scales (Governance 9).

---

## 2. Standing constraints — bind every session, no exceptions

1. **The picker rule (pending decision D1).** Recommended reading: *broad* — anything that
   ratifies, seals, transports or publishes a picked reading is picker architecture. Under
   it, up to 35 airlock files carry contamination and every triage dossier must trace what
   its files import, execute or publish, not just what they compare.
2. **`config/models.toml` is the only place a model is named.** The five pinned identities
   and revisions are in `20_sweeps/docs-facts.md`, cross-confirmed three ways. 47 of 104
   audited files violate this today; every rewrite strips names as it goes.
3. **Gate on directory and provenance, never on suffix.** The old repo lost 24 files of
   register text through an extension rule. **To be built:** a pre-commit content filter
   refusing staged files that carry two or more formulaic act phrases, or `verbatim_text` /
   `gold_text` / `rows[].name` fields. It is not written, and nothing in the harness does
   this today. It belongs to the first stage that can produce register text, and no session
   may describe it as existing before it has a test that proves it refuses a real sample.
4. **Every export, crop, render and reading lives in `private/`.** What may be committed
   about them is counts, ids, hashes and seeds.
5. **Counts come from files, never summaries.** The audit corrected its own roll-ups five
   separate times.
6. **Retired vocabulary never enters**: pilot_*, Stage-W, consolidator, picker, witness
   codenames, `*_v2/_v3` as living version markers. GLOSSARY.md is the vocabulary.
7. **Attribution trailers on every commit**; GPT Sol appears as `GPT-5.6 Sol (OpenAI)`.
   `Codex (OpenAI)` is the fallback only when the serving release is unknowable. Models
   that shaped a commit by reading get `Reviewed-by`.
8. **Subagents and outside AI tools never push and never merge.** GPT Sol reads, reports,
   and (where designated, §5) drafts code that an Opus session reviews line by line before
   it is staged.
9. **Tyrel decides**: every exclusion, every schema, every governance question, every merge.
10. **The autoclave rule — a cleanroom bench, not a landing zone.** No old byte ever
    crosses the boundary. The rebuilding model reads the reference where it lies
    (Temp_Stage, the frozen repo — through the window) and writes NEW code into
    `autoclave/<system>/`, committed on the work branch, where CodeRabbit and the
    reviewer pass read the raw draft. It reaches the pipeline tree only through the
    line-by-line sterilizing pass, is reviewed again in its final place, and **the pull
    request may not merge until the autoclave is empty** — the `autoclave-empty` CI job
    holds that line. Branch history keeps what the tray held: acceptable in alpha, whose
    history never ships, with the ingress check walking every commit so no secret rides
    along.

---

## 3. What the audit settled, so no session re-litigates it

- **One file of 436 was judged fit to carry across nearly as written** (`textnorm.py`,
  with its harvested test suite) — and Tyrel has settled the philosophy split the two
  readers had over it: even this file enters through the full line-by-line review, like
  everything else. Nothing is copied on trust. Everything else is
  rewrite-with-the-old-file-open, harvest-a-lesson, or archive.
- **Stage 6 (Archetypus) has no predecessor.** The established text was decided twice, by
  two pickers, in two stages. Stage 6 is a data contract to be designed, not migrated.
- **Stage 2 (Designator) has no predecessor either** — nothing in the old pipeline marks
  acts; the only segmentation logic is welded to the forbidden vote. Fresh build.
- **Stage 5 (Recensor) is the strongest inheritance** and its biggest risk is bringing too
  much across — the picker sits in the same folder.
- **The witnesses' formats are incompatible and stay verbatim per witness.** Health is
  computed, never self-reported: a real repetition collapse shipped looking exactly like a
  legitimately blank page, and 218 of 29,950 pages once returned nothing under `error=False`.
- **The old repository is permanently unpublishable** and is treated as a read-only archive.
  Alpha inherits knowledge, never bytes of history.

---

## 4. The decision queue — Tyrel's calls, asked when they bite

Each decision is asked at the *start* of the session that cannot proceed without it, with
the audit's recommendation restated from OPEN_QUESTIONS. Two are asked now, at plan
approval, because they shape every dossier:

| # | Decision | Asked at | Blocks | Audit's recommendation |
|---|---|---|---|---|
| D1 | Picker scope: narrow (8 files) or broad (35)? | **Plan approval** | how every dossier reads the airlock | Broad (OQ 3) |
| D2 | Personal data: prevent from commit one, or tolerate-and-note? | **Plan approval** | the Phase 1 hook design | **Prevent by construction** — see below |
| D3 | The three backup-only G8 scripts (watchdog, approve_launch, verify_backup): commit them? | Phase 2 start | pod sessions | Yes, under `operations/pod` (OQ 9) |
| D4 | Page identity (path/content/both); PDFs rendered at the door; `.heic`/`.gif` | Phase 3 start | door + seal | Both ids, content decides; render at door; admit `.heic` (OQ 12–14) |
| D5 | Designator shape (own layout pass / testimonium-first / declared tool) **and** page-vs-act accounting | Phase 4 start | all of stage 2 | Own pass if affordable; act-keyed accounting (OQ 2, 10) |
| D6 | Retention when a witness re-reads; single-act re-reads | Phase 5 start | all of stage 3 | Keep all + current-pointer (OQ 1b) |
| D7 | Archetypus schema (4a–4d) **and** where the human approve gate lives (5, 6 or 7) | Phase 6 — the session *is* this decision | stages 4, 6, 7 | See OQ 4; gate upstream of 7 |
| D8 | Hard-failure counting under multi-witness; bulk mode's fate; who reads grid cells | Phase 8 start | recensor scope | Keep "stop at 3", redefine one failure; drop bulk unless used; attestatores read (OQ 11, 8, 5.3) |
| D9 | Obsidian target; per-act region refs in exports; flat search columns | Phase 9 start | armarium scope | Tyrel's product call (OQ 15, 16) |

**D2, argued plainly (the brief demanded a stance):** prevent leakage outright from commit
one. The audit priced tolerance: the old repository is *permanently* unpublishable over
material a rule was supposed to stop. Prevention here is cheap — `private/` already exists,
output-by-directory is already the design, and the content-based pre-commit is a Phase 1
deliverable. Tolerating "a little" leakage buys nothing except the same trap again.

**Standing, not blocking:** the licence email to the handwriting-witness publisher costs
nothing and may take weeks — send it now (OQ 20). The Indigenous surname index moves to
`private/` if it moves at all, and nothing derived from it is published without the
community-governance review the old repo's own ruling requires (OQ 21). The old ntfy topic
is burned; this repo already runs a fresh one.

---

## 5. The two-engine working model

> **§5 IS UNDER REVISION AND MUST NOT BE FOLLOWED AS WRITTEN. 2026-07-27.**
>
> Three of its load-bearing claims are known wrong or unsettled, and a session that builds
> from this section will build the wrong shape:
>
> - **The write location below is unsafe.** It gives a Sol seat write access "confined to
>   `autoclave/<system>/`". Nothing measured has shown a Codex `workspace-write` sandbox
>   can be confined to a folder *inside this repository*, and the one probe that would
>   settle it has not been run. See `workbench/raw/2026-07-27_worktree-sandbox/FINDING.md`,
>   which also corrects the earlier claim that the boundary is the enclosing git repository.
>   **The write-location question is Tyrel's and is still open.**
> - **The cascade cannot work as written.** A Codex child inherits its parent's sandbox, so
>   a read-only orchestrating seat cannot drive writing builders. Either the parent writes
>   or the children cannot.
> - **Self-orchestration is no longer unverified** — it was demonstrated
>   (`ORCHESTRATION_FINDINGS.md`), and the collaboration tools do not require `ultra`. But
>   a delegate's model cannot be verified from inside the loop, so a run that needs the
>   model actually pinned must use one `seat.sh` call per reader.
>
> The GPT prices quoted further down are still **unconfirmed against OpenAI's published
> pricing by anyone in this repository**. A previous session reported checking them; the
> page has not been read directly.
>
> A full defect list for this document — 62 findings from a Sol re-review, of which 23 tie
> back to Governance 3, Governance 10 or the quarantine — is at
> `workbench/raw/2026-07-27_night-reviews/RUNPLAN_DEFECTS.md`. **It is an extraction of what
> a model said, not a verified finding list.** Each entry needs checking before it is acted
> on.

**Before each build session — the GPT Sol triage dossier.** The session calls Sol
directly — `codex exec` with the template in §9, write access confined to
`autoclave/<system>/` (standing permission from Tyrel, 2026-07-27, for the opening
sessions; he can also paste the template into Codex by hand). Sol also writes the **first
round of code** for designated units (see GPT-led drafts below) straight into the tray in
the same call, so a build session opens with the dossier and the raw draft both waiting.
The dossier inputs, per system: the gap note, its HANDOFF draft, the
verdict files for its staged files, the KEEP/KILL rows, and the relevant test-harvest
section. GPT Sol produces one dossier per system:

1. What this system must do, in the plan's vocabulary, in ten lines.
2. File-by-file: keep-the-knowledge / keep-the-shape / leave — with the *challenge block*
   consulted on every file, and disagreements surfaced rather than averaged.
3. The knowledge-not-to-lose list for this system, deduplicated, each item traced to its
   source line.
4. **Every critical and high defect** recorded in this system's verdict files, each mapped
   to a named invariant/test in the new build or explicitly marked not-applicable with a
   reason. (This retires the audit's untriaged 5-critical/57-high backlog system by system.)
5. Under the picker rule chosen in D1: what this system's files import, execute, seal or
   publish, and whether any of it touches picked output.
6. Proposed build order within the session, and the questions only Tyrel can answer.

**The Opus audit of the dossier.** Opus 5 spot-checks three to five load-bearing claims
against the actual files before building on them — the audit's own error rate is the
argument. A dossier claim that fails its spot-check sends the dossier back, with the
failure named.

**Build sessions.** Opus 5 medium, single context, no fan-out by default. Tests first from
the named invariants; small commits; stage only touched files; receipts as it goes.

**GPT-led drafts — allowed, bounded, autoclaved.** Mechanical, well-specified units may be
drafted by GPT Sol in Codex to save Claude budget: the textnorm port, notify/doctor-class
scripts, format converters, test scaffolds from already-written invariant lists. Three
conditions: the unit is not governance-adjacent (nothing in stages 4–6, nothing touching
accounting, holds, seals or money); every draft lands in `autoclave/<system>/` and nowhere
else, committed on the work branch so CodeRabbit and the reviewers read it raw — a
draft-only push triages to one reviewer, the placement push to the full set; and it
reaches the pipeline tree only through an Opus session's line-by-line sterilizing pass,
reviewed again in its final place, with the tray emptied before the pull request merges.
"Nothing enters uninspected" applies to Codex exactly as to anyone. Governance-adjacent
systems are Claude-led, always.

**Model economics, verified against current price sheets (2026-07-27).** Anthropic API
rates per million tokens: Fable 5 $10/$50 · Opus 5 $5/$25 (unchanged from Opus 4.8 — so
"Sonnet 5 costs what Opus 4.8 did" is false) · Sonnet 5 $3/$15, with **introductory
$2/$10 through 2026-08-31 — the entire rebuild window** · Haiku 4.5 $1/$5. Two prints
beyond the stickers: Sonnet 5's new tokenizer spends ~30% more tokens on the same text,
so its effective gap to Opus is narrower than the sticker suggests (still decisively
cheaper); and subscription weekly limits are cost-weighted, so these ratios are the
right proxy for how fast each model burns the week. OpenAI GPT-5.6 family: **Sol
$5/$30** (flagship — long-horizon agentic work, coding, and notably the security-shaped
reading our reviews need), **Terra $2.50/$15** (balanced, ~half Sol — the fallback for
bulk mechanical drafting if Sol's budget ever tightens), Luna $1/$6 (not needed here).
Consequences the roster already encodes: Sonnet workers at roughly half Opus burn for
the whole rebuild; Opus on the judgement seats; Fable reserved for the two design
sessions and the questions above Opus's reliable ceiling; Sol carries triage and one
review seat on Tyrel's separate budget.

**The cascade, as ruled.** Terra is Sonnet-grade building; Sol is Opus-grade judgement;
medium/high are the near-universal efforts (scout-low and consult-xhigh are the
deliberate edges). Preferred shape per system: the Opus session hands Sol a task and a
target; Sol drives Terra builders with Sol inspectors in a mini-loop on the tray; Sol
reports back; the session then runs Sonnet checkers and Opus auditors over the result;
a final Sol + Opus audit closes the system. Whether Codex can self-orchestrate — Sol
spawning Terra inside one `codex` run — is **unverified**; until confirmed, the session
orchestrates the same loop itself with per-call `codex exec -m gpt-5.6-terra` /
`-m gpt-5.6-sol` (and `-c model_reasoning_effort=<level>` to override Sol's global
xhigh), which works today and produces the identical review chain.

**Review.** CodeRabbit on every PR (§7). Reviewer pass per push under the triage-scaled
rule (pending in CLAUDE.md — until approved, the standing three-reviewer rule applies).
GPT Sol is one of the reviewers, so the pass spends mostly non-Claude budget.

---

## 6. Phases and session cards

Order of verbs: launch → boot → upload → run → export → close down, with foundations
first. **Dependencies are stated honestly; anything not listed as depending can run in
parallel.** Phase 2 and Phase 3 are independent of each other after Phase 1. Phases 4–9
are sequential in their decisions but their *test-writing* can lead their builds.

Each card: goal → orchestrator → inputs → invariants (by test-harvest number) → PR shape →
done means → watch for. Session prompts are assembled from §9 plus the card.

**Sizing for queueing** — honest estimates of unattended run time once the session's
decision is answered at queue time. S ≈ an hour or two; M ≈ an evening; L ≈ a full
overnight run. Tyrel queues overnight sessions from this table; under-queueing an L as a
coffee break wastes the night.

| Session | Size | Overnight unattended? |
|---|---|---|
| S1a | M | Yes, once D1/D2 are answered |
| S1b | M | Yes |
| S2a | L | Yes, once D3 is answered; queue the full reviewer set for morning |
| S2b | M | Yes |
| S3a | L | Yes, once D4 is answered |
| S3b | M | Yes |
| S4a | L | No — D5 plus design conversation; attend the design, let the build tail run |
| S4b | M | Yes |
| S5a | M | Yes, once D6 is answered |
| S5b | M–L | Yes |
| S6 | S–M | No — this session *is* a decision, with Tyrel present |
| S7a | M | No — design worth attending |
| S7b | L | Yes |
| S8a | L | Yes, once D8 is answered |
| S8b | M | Yes |
| S9 | M | Yes, once D9 is answered |
| S10 | L | Starts attended — a live pod needs G8 permission in-session and money watched; the export/audit tail can run |

### Phase 0 — Harness (this session; closing)
Hooks, skills, notifications, settings, this plan. Ends in the `infra/workspace-readiness`
push.

### Phase 1 — Foundations

**S1a — Skeleton, config, gates.** *Opus 5 medium.*
Repository layout (`pipeline/1..7`, `common/`, `config/`, `operations/`, `proof/`,
`private/`); `config/models.toml` carried from `20_sweeps/docs-facts.md` with revisions
pinned and retired stage names left behind; the content-based register-text pre-commit
check (constraint 3); CI wiring for `check-all.sh`; test-spine conventions written as a
short committed document (invariants 86–93: real producers over real artifacts, anti-toy
tripwires, collection tripwire, no success over an empty population, drift checks over
agreement surfaces, rulings quoted verbatim in docstrings).
PR: small, mostly structure and config. Done: check-all green in CI; a seeded
register-text fixture is *refused* by the new hook. Watch for: hook false-positives on
legitimate docs — tune the two-phrase threshold, never disable.

**S1b — The accounting spine.** *Opus 5 medium. Depends: S1a.*
`common/`: the run ledger (partition engine), seal/receipt primitives, atomic-write
helpers. This is the machinery every stage boundary calls, built once.
Invariants: 8, 10–17, 22–24, 59. Sources: `common/run_ledger.py` (REWRITE/REWRITE),
`common/seal.py`, `common/receipt.py`, `common/provenance.py` rows in KEEP_LIST; recensor
gap note §completeness.
PR: one system, with its tests. Done: partition imbalance is fatal in a test; evidence
cannot read zero; unknown holds. Watch for: porting the page-keyed accounting shape — the
ledger is unit-kind-agnostic from day one (recensor OQ 10).

### Phase 2 — Pod and money (verbs: launch, boot, close down)

**S2a — Launch contract, budget guard, account.** *Opus 5 medium. Depends: S1. Decision D3
at start.* The most review-hungry code in the project; schedule inside the CodeRabbit
window and give it the full three-reviewer pass.
Invariants: 73–75, 78–80, 85 + Governance 8. Sources: `operations/pod/launch_contract.py`,
`runpod_budget_guard.py`, `runpod_account.py`, `budget_policy.py`, `safe_template.py`
rows; the three backup-only scripts if D3 approves. The provider sits behind **one seam**
(a `provider.py` boundary file) — no multi-provider abstraction, just no RunPod spelled
through the codebase.
Done: a bad config stops on the Mac loudly; shutdown is verified against provider state in
a test with a mocked provider; no forwarded value can carry `$( )` (fix the transport, not
just the guard — invariant 79).

**S2b — Boot, doctor, close, notify, backup.** *Opus 5 medium; notify/doctor drafts may be
GPT-led. Depends: S2a.*
Invariants: 75–77, 81–84. Sources: `boot.sh`, `bootstrap*.sh`, `doctor.sh`, `pod_close.py`,
`ntfy_notify.*` (fold into the existing `operations/notify/`), `offsite_backup.sh`,
`incremental_backup.py` rows.
Done: boot markers mirror to stdout; a failed doctor check cannot reach BOOT_DONE; close
polls provider state; every optional channel no-ops safely unconfigured.

### Phase 3 — The door (verb: upload)

**S3a — Upload door and admission.** *Opus 5 medium. Depends: S1. Decision D4 at start.*
Invariants: 1–9, 62–65, 72. Sources: `operations/submit/upload_door.py` (4,447 lines,
REWRITE/REWRITE — the biggest single rewrite in the plan; take two sessions if it fights),
`upload_door_process_identity.py`, `submit_folder.sh`, exemplar gap note "the door".
Done: decode-verified admission; per-file rejection, every refusal named; bombs bounded
from the header; post-READY immutability; no absolute path in any HTTP response.
Watch for: the door's hard-won constants (truncation via `load()` not `verify()`; the
header-first pixel bound; `LOAD_TRUNCATED_IMAGES` never set) — carry them verbatim.

**S3b — Seal and page identity.** *Opus 5 medium. Depends: S3a, D4.*
Invariants: 8, 9, 61 + exemplar gap note (seal on destination bytes; re-stat before and
after; four-step atomic write; never re-render a PDF downstream).
Sources: `seal_exemplar.py`, `exemplar_contract.py`, `page_pixels.py` (re-sourced from
`remote/source_page.py` per KILL_LIST), `seal_page_inventory.py` rows. The witness
dependency in the old routing files does **not** come across — sealing depends on bytes
alone.
Done: sealed input verifies byte-for-byte; re-seal over control files refused; page
identity implemented exactly as D4 decided.

### Phase 4 — Designator (verb: run) — **the riskiest build, see §10**

**S4a — Design and geometry core.** *Opus 5 **high**. Depends: S3, D5 at start.*
Fresh build. The dossier for this phase is the largest GPT Sol task: the designator gap
note, the schema-designator sweep, and the five staged files' verdicts, with every
geometry lesson extracted (bbox rescale, inverted boxes, asymmetric pad with the corrected
54/50 figures, never-pad-twice, ONE LANE, X-blind demotion, overlap merge, residual
crops).
Invariants: 34–38. Done: the geometry-invariance harness (34) and the one-lane guard (37)
exist and run against a sealed canary page set *before* the cutter is trusted; every
threshold lands in config with a recorded derivation or an explicit "ratified as-is,
unproven" marker.

**S4b — Coverage accounting, residuals, index bands.** *Opus 5 medium. Depends: S4a.*
Invariants: 36, 37, 39, 40 + conservation as an exit code. Sources: `index_row_bands.py`,
`act_ownership_guard.py` rows; the brace-linked-acts case is the acceptance fixture — a
Designator that loses the second act of a braced pair fails its own session.

### Phase 5 — Attestatores (verb: run)

**S5a — Testimonium envelope and the write path.** *Opus 5 medium. Depends: S1b; D6 at
start. Schema work can precede Phase 4's completion using fixture crops.*
Invariants: 41–43 + the sweep's envelope rules: payload verbatim per witness in its own
field; `content_health` computed by deterministic counting, never self-reported;
provenance attached by this stage from `config/models.toml`, never passed through;
`not_run` stub records; absence-as-record for excluded witnesses.
Done: retention implemented exactly as D6 decided; a repetition-collapse fixture is
flagged unhealthy by computation; a testimonium with no resolved identity refuses to
write (fail closed — OQ 7).

**S5b — Witness runner, batch driver, chains.** *Opus 5 medium. Depends: S5a.*
Invariants: 44, 18–26 (resume health, crash evidence, denominators). Sources:
`witness_run.py`, `attestator_batch_driver.py`, `collect_testimonia.*`,
`testimonium_integrity.py` rows — the crash-evidence machinery migrates close to intact.
Done: resume keyed to provenance fingerprint, not existence; two-channel crash evidence;
config provably reaches the pod link by link (44).

### Phase 6 — Archetypus contract (design session)

**S6 — The established reading, decided.** *Claude Fable 5, high/extra effort. Tyrel
present — this session is decision D7.* Small in code, large in decision; nothing exists
to inherit.
The session drafts and Tyrel ratifies: the Archetypus record schema (text, act id, region
of ink, resolved identity+revision, flag state); human correction above the Archetypus,
not inside it; written once, new record on re-run; an unreadable act *has a record* (4c is
the most important line in the schema); "read and empty" distinguishable from "never
read" by type; whether dissent travels; where the human approve gate lives. Every record
in `private/`.
Deliverable: the schema + validator (a few hundred lines) and the contract document.
Invariants: 45, 53, 55 + G4/G5. Done: Tyrel has answered 4a–4d in writing in the contract.

### Phase 7 — Perlector (verb: run)

**S7a — Dossier and Perlectio design.** *Claude Fable 5, high effort. Depends: S6.*
Weigh `workbench/design/iterative_reader.md` here, before the Perlector contract is
settled — the experiment lives in the design drawer by ruling, not in ARCHITECTURE.
The dossier schema is the stage's real contract: crop pixels + every testimonium verbatim
+ geometry, nothing selected, witnesses as anonymous slots (blinded arms — the old repo's
own training-side rule). The G3 tests are designed here: no arm choice establishes text; a
reading constrained to a witness's offering is a pick with extra steps; the partitioner
trap from `act_dossier.py` is a named anti-pattern with a test.
Invariants: 45–51. Done: contract documents + failing tests that the build must satisfy.

**S7b — Build: dossier assembly, serving, run path.** *Opus 5 medium. Depends: S7a.*
Sources: `act_dossier.py` (rewrite against, not port — its seven listed defects are the
regression suite), `serve_perlector.sh` (the pod serving knowledge: flashinfer/vLLM cache
pins, readiness by parsed exact id, teardown trap before launch),
`promote_sealed_upstream.py` sealing discipline.
Done: a dossier round-trips; the reader runs end to end on fixtures. **The adapter is
known-untrained (modality mismatch) — this phase proves wiring, not reading quality**, and
says so in its receipt (Tyrel's own ruling: pipeline first, train properly later).

### Phase 8 — Recensor (verb: run)

**S8a — Boundary, receipts, caps.** *Opus 5 medium. Depends: S1b (engine), S5 (units).
Decision D8 at start.*
The best return on effort in the project: porting proven invariants into new names.
Invariants: 10, 11, 18–25 + hold-at-final-boundary, per-release cap re-arm, Tyrel's
verbatim hard-failure definition and "PURE ABSOLUTE, STOP AT 3".
Sources: `boundary.py`, `run_receipt.py`, `coverage_receipt.py`, `verify_coverage.py`,
`page_health.py` rows. **`consolidate.py` is read for its completeness gates and its
picking never crosses** — the four airlock files in KEEP_LIST's must-not-migrate table are
pinned in the dossier.
Done: the `pages_without_chunks` report exists (OQ 18 — the 218-page defect's test);
partition proofs at every boundary.

**S8b — Recovery ladder, blank confirmation, held/release.** *Opus 5 medium. Depends: S8a.*
Invariants: 26–33. Sources: `grid_recovery.py`, `blank_confirmation.py`,
`held_with_evidence.py`, `release_held_job.py` rows.
Done: deleting the recovered-text namespace changes no act and no page output (the test
the sweep said to write first); recovery crops kept; blank needs two independent completed
reads; unavailability is an answer.

### Phase 9 — Armarium (verb: export)

**S9 — Exports.** *Opus 5 medium; textnorm port GPT-led. Depends: S6 (contract), S8.
Decision D9 at start.*
Invariants: 52–61. Sources: `derived_views.py`, `vault_markdown.py`,
`export_acts_database.py` (the four-way pick dies; the crossref winner-pick with retained
losers survives — do not collapse the two), `textnorm.py` + its harvested test suite,
`fetch_results.sh` (its sibling `fetch_outputs.sh` is archived for silent-swallow).
Done: exporter is a read-only projection of a sealed job; exactly one established-text
field read from stage 6; every undelivered page named in every profile; absence
distinguishable from accounted absence in the export itself; all output in `private/`.

### Phase 10 — Proof (Governance 9: prove before scale)

**S10 — End to end, small, honest.** *Opus 5 medium; adversarial review pass at high.*
A small real corpus (Tyrel picks; the sealed canary pages are candidates) through all six
verbs on a real pod, with the acceptance audit rebuilt in its own home under `proof/`
(OQ 17 — independent of the stages it audits, its model pins deliberately literal).
Done: one run, import to export, every page accounted for, receipts verified by rebuild,
pod shutdown verified against provider state and billing. Then the defect-triage closeout:
every dossier's item-4 table rolled up, confirming the 5 critical / 57 high defects each
died in a named test or was ruled not-applicable.

---

## 7. The CodeRabbit loop, and the trial window

- **Small PRs, more of them, concurrently where independent.** A bigger diff gets a worse
  review, not a better one. Target roughly ≤600 changed lines of substance per PR; a
  session that overruns splits its PR rather than growing it.
- **The review-hungriest code lands earliest inside the 14-day Pro+ window**: money and
  launch (S2a), the door (S3a), the accounting spine (S1b). Phases 1–3 are the window's
  work; if the trial clock has not started, start it when S1a's PR opens.
- **Autoclaved drafts get two reads.** A PR carries the raw draft in
  `autoclave/<system>/` for CodeRabbit's first read; the sterilizing pass moves survivors
  into the tree for its second read, in final form; the `autoclave-empty` check gates the
  merge so a draft can be reviewed but never land.
- **The loop per PR**: CodeRabbit reviews → Tyrel relays comments (nobody polls) → the
  session verifies each claim before acting (some are style, some are wrong, some are
  real) → fix the real, answer the rest with reasons, fresh receipt → repeat. **Two
  rounds**; if a third is needed, the disagreement goes to Tyrel rather than another lap.
- Reviewer pass per push, as CLAUDE.md rules at that time. Agreement between reviewers is
  evidence, not a verdict.
- **Reviewer effort is declared, never inherited by accident.** The agent roster in
  `.claude/agents/` pins model and effort per role (scout/worker/infra-worker/auditor/
  consult/rebuilder — table in CLAUDE.md); the auditor seats run at high regardless of
  the spawning session, and Sol brings his own effort from Codex config (currently
  xhigh). The triage still names each seat's model explicitly per pass.
- **Overnight runs can carry review permission granted at queue time** — "build, then run
  the full pass" is an explicit per-request grant, so a night session ends with findings
  waiting rather than a blocked gate. The push itself still waits for morning.

## 8. Pivot rules — decided now, while nothing is on fire

1. **Scope trip-wire.** A session 50% past its card's PR-size target, or fighting a file
   the card called moderate: stop, split the card, update this plan, continue in a fresh
   session. Never "push through".
2. **An unplanned blocking decision** surfaces mid-build: stop at the point of ambiguity,
   send Tyrel a `decision` notification when stopping (not after), record the question in
   the decision queue, park the branch cleanly.
3. **A dossier proves wrong under spot-check**: the build does not start on it. Back to
   GPT Sol with the failed claim named; two failures on one dossier means Opus reads the
   sources directly and the dossier is demoted to an index.
4. **Claude budget exhausted mid-week**: build sessions pause; GPT Sol continues dossier
   preparation for the next two phases so the pipeline of *decisions* keeps moving; builds
   resume on reset. GPT-led drafting may proceed only for the units §5 already designates.
5. **A session fails its done-check twice**: the card is wrong, not the effort. Re-plan
   the card (usually: split, or a missing decision surfaces) rather than re-running it.
6. **Any evidence of picking** — in a dossier, a rewrite, a review comment — stops the
   line it is on. G3 questions are never resolved locally; they go to Tyrel with the file
   and line.
7. **Governance and a goal pull apart**: stop and say so (Governance 0). Never resolved
   in-session.

## 9. Prompt templates

**GPT Sol triage dossier** (Opus fills the slots, Tyrel pastes into Codex):

> You are triaging one system of a pipeline rebuild. Read, in this order:
> `40_gap_notes/<stage>.md` (body AND its revision section), `40_gap_notes/HANDOFF_<stage>.md`,
> the verdict files for these staged files: <list from KEEP_LIST>, including every
> `challenge` block, and `20_sweeps/test-harvest.md` §<letters>.
> Produce the six-part dossier: (1) the system's job in ten lines; (2) file-by-file
> keep-knowledge/keep-shape/leave with both readers' verdicts shown, never averaged;
> (3) the deduplicated knowledge-not-to-lose list, each item with its source line;
> (4) every CRITICAL and HIGH defect in these verdict files, each mapped to a named
> invariant or test in the new build, or marked not-applicable with a reason;
> (5) under the picker rule — <D1 answer> — what these files import, execute, seal or
> publish, and whether any path touches picked output; (6) proposed build order and the
> questions only Tyrel can answer.
> Rules: use the glossary vocabulary and no synonyms. Never average a disagreement. Say
> "I don't know" rather than guess — an honest unknown routed to Tyrel beats a confident
> answer. You are reading and reporting; any code you draft lands under
> `autoclave/<system>/` and nowhere else, and you never commit, push or merge.

**Opus build-session opening** (after `/session-start`):

> This session builds <card id> per `workbench/active/RUN_PLAN.md`. Read the card, the
> dossier at <path>, and spot-check three load-bearing dossier claims against sources
> before building. Decision <Dn> is answered: <answer>. Write the tests for invariants
> <numbers> first, then build to green. PR target: <shape from card>. Stage only touched
> files; receipts as you go; stop conditions are RUN_PLAN §8.

**Session close**: PR open with attribution trailers — work reaches a PR by default, and
any change staying behind is named with its reason; card's done-list checked off in the
receipt; RUN_PLAN state line updated; handoff updated **with the next session's brief:
goal, model and effort to open with, chunk size, honest duration, overnight-capable or
not**; anything unresolved goes to the decision queue, not into the void.

## 10. The riskiest thing, and its de-risking

**The Designator.** It is a fresh build with no working predecessor, its geometry
thresholds have no recorded derivation, and it owns the coverage denominator — the number
Goal 1 lives or dies by. A quietly wrong Designator loses acts *silently and forever*,
which is the project's stated worst outcome; a wrong Perlector merely reads badly, visibly,
and is retrained later.

De-risking, in order: D5 answered before a line is written; the geometry-invariance
harness and one-lane guard built and run against sealed canary pages *before* the cutter
is trusted (the harness catches the class of defect planner-level tests provably cannot);
the brace-linked-acts scan as the acceptance fixture; every threshold in config carrying
its derivation or an explicit "unproven" marker; and the Recensor's coverage accounting
(Phase 8) treated as the Designator's adversary — the stage that exists to catch its
misses — rather than its rubber stamp.

## 11. What alpha deliberately does not build

Named deferrals, each with where it goes: **training and evaluation** (beta; the L-section
invariants are recorded for that day) — alpha proves wiring with the known-untrained
adapter. **Search and correction tooling** (out of scope by the six verbs; the flat-fold
columns ship only if D9 keeps them). **A multi-provider abstraction** (one seam file only).
**The review/correction webUI beyond the minimal operator surface** (door + held/release
+ progress only). **Obsidian niceties** (wikilinks, hubs — D9). **The bulk mode** (dropped
unless D8 keeps it as a declared separate product). **Polish** (beta, deliberately).
An unnamed gap is worse than a named one; if a session finds itself building something not
on a card, that is pivot rule 1.

## 12. First week, concrete

Assuming approval today and a Monday start, at roughly a session per working evening:

- **Mon** — S1a (skeleton, config, gates). Before it: Tyrel answers D1 and D2, sends the
  licence email, starts/schedules the CodeRabbit trial to open with S1a's PR.
- **Tue** — S1b (accounting spine). GPT Sol dossier for Phase 2 prepared today (its first
  dossier — treat it as the template-calibration run and expect one spot-check bounce).
- **Wed** — S2a (launch + money), D3 answered at start. Full three-reviewer pass.
- **Thu** — S2b (boot/doctor/close). GPT Sol dossiers for Phase 3 prepared.
- **Fri** — S3a (the door), D4 answered at start. If the door fights, S3a becomes two
  sessions and the week ends there — that is the plan working, not slipping.

Weekly rhythm after that: Claude budget resets weekly; Phases 4–5 in week 2 (D5, D6),
6–8 in week 3 (D7, D8), 9–10 in week 4. A phase finishing early pulls the next dossier
forward; nothing else changes order.

---

*Written 2026-07-27 by Claude Fable 5 from the Temp_Stage audit corpus. Approval,
amendment and every decision in §4: Tyrel.*
