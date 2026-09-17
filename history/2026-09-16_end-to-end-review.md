# 2026-09-16 — end-to-end review

Requested by Tyrel in-session: a general review of the project for errors and issues,
"especially end to end." This is the working record, committed early and updated as the
review proceeds, so that partial results survive a session interruption. Nothing in this
section below "Scope" is a finished finding until it has its own `### F` heading with a
file:line citation, a scenario, and a verification note; anything short of that is a lead,
not a claim.

## Why this scope, not a repeat of the last one

`history/2026-09-14_prelaunch-review-findings.md` already ran a ten-perspective review two
days ago (112 findings, dispositions recorded in the commits that followed). Re-reading the
same ~500 files from zero would mostly reproduce that record. The part of the tree that
record could not have covered is what landed **after** it: three commits,
`540c005..3a7dd80..485283b`, diffing at 175 files / +26,825 / -1,461 against the last
reviewed commit (`d6b3c42`) — including today's HEAD, which by construction has had zero
review. That diff is the primary target here. Recording what was in-scope and what was not
is itself required by GOVERNANCE 10 (no silent caps) and hard rule 7.

## Scope

1. The diff `d6b3c42..HEAD` (`pipeline/orchestrator/run.py`, `pipeline/7_armarium/*`,
   `pipeline/6_archetypus/*`, `pipeline/5_recensor/*` test changes, `pipeline/4_perlector`
   doubt-channel changes, `proof/build_fixture.py` and `proof/skeleton_fixture.toml`,
   `pyproject.toml`) against the ARCHITECTURE.md invariants and GOVERNANCE.md rules 2, 3,
   10, and 11 in particular (nothing lost silently, the Perlector never picks, honest
   measurement, bounded recovery).
2. Cross-document consistency: README.md's single status line against what the three new
   commits and `history/2026-09-15_runpod-first-live-trial.md` actually record: has a real
   trial happened, and does the status line still describe it accurately.
3. A spot-check of a sample of the 112 findings in the 2026-09-14 ledger against current
   HEAD, to check that claimed dispositions actually hold in the tree (hard rule 7:
   nothing is lost silently, including a finding marked fixed that regressed).
4. The local gate: `check-fast.sh` run against current HEAD (this session pinned uv 0.12.1
   locally to match `pyproject.toml`'s `required-version`, since the sandbox's stock uv was
   0.8.17).

## Coverage gaps (named up front, not discovered at the end)

- This is not a repeat of the full ten-perspective audit; anything the 09-14 ledger already
  covered and this diff did not touch is out of scope here.
- No live pod, no real submission material, no network-volume transfer exercised (hard rule
  2/GOVERNANCE 8: no live pod without Tyrel's permission in-session, not sought here).
- `check-all.sh`'s full frozen-environment verification (its uv-symlink and audit-group
  checks) is heavier than this session's remaining budget can safely carry alongside the
  code review; `check-fast.sh` plus targeted tests on the changed files stand in for it,
  per CLAUDE.md's own review-proportionality table for "tests, documents, configuration
  pins, cleanup."

## Findings

Ten independent readers, each scoped to one part of the `d6b3c42..HEAD` diff or a
cross-cutting check; every medium/high finding was then adversarially re-verified by a
second, skeptical reader against the current tree before being written up here (four held,
two were refuted — both refutations are recorded below, not dropped, per hard rule 7).

### Confirmed

**1. [high] The operator's own run screen still contradicts itself on the first run a new
operator makes.** `operations/operator/surface.py:1312,1608` — `_declared_work` prints
every page `proof/skeleton_fixture.toml` declares (page 1, 2, 3) regardless of which
scenario actually ran, while the closing total is the real, scenario-correct count.
Verified by running `run --run-id auditcheck1` with no other flags: `Checking page 1,
page 2, page 3.` … `Pages accounted for: page 1, page 2, page 3 (2 total).` This is not a
new discovery — it is F001 (high) and F106 (low) in `2026-09-14_prelaunch-review-findings.md`
— but it is confirmed still genuinely live in the diff under review, in the very function
(`run()`) that commit 3a7dd80 rewrote to fix the adjacent F003-class problem in the same
pass. It is not silently lost: `operations/operator/test_g21_fixture_rehearsal_narration.py`
(added in that same commit) carries two strict `xfail` regression tests naming this exact
defect, which is where this session's `check-fast.sh` run's "2 xfailed" comes from. Worth
prioritizing precisely because it is the first thing anyone running this pipeline for the
first time sees.

**2. [medium] The Armarium export doesn't cross-check the sealed noise-floor fields it
trusts per page.** `pipeline/7_armarium/armarium_export.py:1370-1494` — `minimum_ink_pixels`
and `minimum_fraction_outside_bp` are, by `common/residual_ink.py`'s own design, the raw
sealed `[coverage_audit.noise_floor]` value passed through unchanged for every page of one
run (unlike `substantial_ink_pixels`, which legitimately scales per page). The per-row
schema check and the independent clean-machine verifier (`verify_export_bundle` /
`verify_delivered_bundle`) both validate each row in isolation and never compare these two
fields across rows or against a top-level sealed citation. An honest run always writes
matching values (confirmed in the producer, `run.py:735-820`), so this is not reachable by
correct code — but a producer bug or a tampered/corrupted bundle could inflate one page's
noise floor and silently release that page's edge-ink hold, with neither check catching it.
**Proposed fix:** assert cross-row equality for these two fields (or cite the sealed value
once at manifest level and check every row against it), matching the "recompute the hold
from recorded counts alone, trusting nothing from elsewhere" principle the module already
states for itself.

**3. [medium] README's one status line conflates "no trial happened" with "a trial
happened and didn't prove the pipeline."** `README.md:8-11` — unchanged since commit
f0572f2, predating any live run: "...has not been proven on a Tyrel-approved small
real-material trial." But `history/2026-09-15_runpod-a100-qualification.md` and commits
3a7dd80/485283b record that a Tyrel-approved live run against real, RecordGold-derived
material *did* happen, reached the Perlector, and sealed acts — just with red optical
quality (DAI and Churro both mis-transcribed characters, the Perlector changed one
letter's case, two of four pages were held rather than read) and deliberately sealed as
mechanics-only so "the qualifier refuses such an audit as proof of a qualified profile."
The line's underlying conclusion (unproven, under GOVERNANCE 9) is still correct; the
single sentence just doesn't distinguish "untried" from "tried and inconclusive," which is
the exact failure mode `history/2026-08-01_repository-audit.md` already named once before
on this same document. **README.md is governed (CLAUDE.md hard rule 10); no wording is
proposed here — this is reported for Tyrel and the governed-edit procedure.**

**4. [medium] On real (non-fixture) ingress, five of the Recensor's six documented recovery
dispositions are currently unreachable.** `pipeline/5_recensor/run.py:3612,3853,3861-3876`
— this diff adds `recrop_dispatchable = not real_route` as a required condition for the one
recovery request this stage can ever issue (`fallback-recrop`), because "on a real
submission it cannot" be answered (the Designator's recovery pass still reads a fixture's
declared rectangle). Page-level reread is separately unimplemented regardless of route
(`run.py:4158`). So on the first real submission, every completeness finding routes
straight to `held-for-review` — only "accept" and "hold" of ARCHITECTURE.md's six listed
dispositions are reachable. ARCHITECTURE.md's Recensor section and diagram don't name this
split. **ARCHITECTURE.md is governed; reported, not edited, for Tyrel.**

**5. [medium, narrowed on verification] The Perlector's new self-assessed doubt channel
has no name in the governed vocabulary.** `pipeline/4_perlector/annotations.py:225-323`
(via commit 540c005) — the tri-state `uncertainty_assessment` record
(`ASSESSMENT_ASSESSED`/`_NOT_ASSESSED`/`_MALFORMED`) is genuinely new: a self-assessed
confidence/gap layer, recorded even on a witness-free Lectio nuda, that is structurally
distinct from dissent (dissent is witness-comparative; this isn't) and that
`validate_reading_payload` now requires on every published Perlectio. (The original finding
also cited `uncertain_spans`/`gaps` as new; verification found those existed since #69 and
are not part of this diff — that part is withdrawn.) ARCHITECTURE.md's Perlectio definition
and "On dissent" section, and GLOSSARY.md's Perlectio entry, don't mention it.
**Governed docs; reported, not edited.**

### Leads (not independently adversarially verified — low severity, per this run's policy
of only re-verifying medium/high findings)

**6. [low] `proof/build_fixture.py:52-`, the hand-computed reader-doubt span offsets
(`READER_DOUBTS`/`READER_GAPS`) aren't self-checked against `ACTS`' text the way
`CHANDRA_ANCHORS` is (lines ~780-800 do check the latter). An edit to a1's declared text
without updating the offsets in lockstep would drift silently past the generator; only a
separately hand-written test (`test_the_declared_reader_doubt_reports_anchor_to_the_texts_they_are_declared_over`)
would catch it, and only because it re-hardcodes the same offsets by hand. Proposed fix:
add a `str.find`-based self-check at generation time, as `CHANDRA_ANCHORS` already does.

**7. [low] F040 ("gate refuses this checkout's uv, pin duplicated in two files") is only
half-closed.** `pyproject.toml:48` now carries `[tool.uv] required-version = "==0.12.1"` (a
real improvement — this session hit exactly the gap F040 described, independently, before
finding the finding: the sandbox's stock uv was 0.8.17). But `.githooks/check-all.sh`
(lines 70, 128, 132, 135) and `.github/workflows/ci.yml:70` still separately hardcode
`0.12.1`; nothing reads it from `pyproject.toml`, and the reconciliation test F040 proposed
for `.githooks/test_ci_workflow.py` was never added. Bumping one still silently diverges
from the other two.

### Checked and found sound

**Orchestrator (commit 485283b's two headline claims).** Traced, not taken on faith. "A
rejected re-proof holds its act instead of ending the run": `common/perlector_audit.py:327-389`
derives `EXAMINATION_REPROOF_REJECTED` when a completed re-proof's change span escapes every
flagged location; `pipeline/4_perlector/run.py:4478` gates the publish so a rejected rewrite
never overwrites the published text and never reaches the uncaught-refusal path the old
behavior risked; `pipeline/5_recensor/run.py:3257-3270` routes it to `held-for-review`
(never `recovery-requested`, so it cannot loop or spend a recovery round); `outcomes.py:397`
maps it to `HELD_FOR_REVIEW`, never `DELIVERED`. "An unread page is held rather than
miscalled a cut-off": `pipeline/2_designator/structure_pass.py:991-1016` shows this is a
diagnostic relabeling (`HELD_DEGENERATE` vs `HELD_CUT_OFF`, both in `STRUCTURE_HELD_CODES`,
both already producing `DISPOSITION_HELD`) — a held page was never marked read before this
change and still isn't after it; no downstream code branches on the specific reason code.
One **pre-existing, not-a-defect** interaction worth naming rather than silently passing
over: in `pipeline/5_recensor/run.py`, the coverage-recovery branch (`:3861-4024`) can
`continue` — consuming an act's first recovery attempt — before the branch that would seal
`held-for-review` for a reproof-rejected act ever runs. It only fires once per act, never
discards the audit fact (carried onto the recovery-requested review's own payload), and
matches GOVERNANCE 11's stated priority (recovery is for coverage, not content quality) — so
it reads as intended, but no test exercises this exact co-occurrence (an act simultaneously
reproof-rejected *and* wanting coverage recovery) end-to-end. Worth a test, not a fix.

Independent readers also covered the Recensor and Archetypus test-file diffs, the base of
the Perlector doubt-channel work, and the dependency additions in `pyproject.toml` (each
carries the required provenance comment). None returned a defect.

### Ledger disposition spot-check (2026-09-14 findings, against current HEAD)

The 09-14 ledger's own findings text doesn't carry dispositions; those live in PR #117's
body (fixed-by-group G1-G24, declined-with-reason, refuted-with-reason, Tyrel-ruled-and-
queued), which this pass fetched and checked claims against directly rather than trusting
the squashed merge commit's summary. Roughly 20 findings were verified against the actual
code, spread across severities (critical: F013, F049, F050/F053, F068/F083, F082, F006/F060;
high: F014, F015, F017, F021, F031/F096, F084, F002/F032/F095; medium: F040, F088, F089,
F092, F007/F039/F078/F086; low: F079, F090, F111).

- **F021** ("Perlector re-reads a sealed page blob with no digest check") was reopened as a
  candidate finding, then **refuted on verification**: the real call path
  (`run.py`'s per-act loop calls `verify_region` → `verify_exemplar_crop_lineage` →
  `_read_checked`, which does convert `OSError` to a named `ContractError`/`SchemaRefusal`)
  reads the same blob and fails cleanly *before* `dossier.py:173`'s unguarded read is ever
  reached. The original panel's one-line disposition named the wrong function but its
  substance holds. `dossier.py:173`'s own read is real but currently dead-in-practice
  redundancy, not a live crash path — not re-opening it, noted for awareness only.
- **F040** — see finding 7 above: the ledger's "fixed in this branch" disposition
  overstates what actually landed.
- Every other checked finding's fix was confirmed genuinely present in the code and closing
  the described scenario by direct read (e.g. the bbox digit bound in
  `common/chandra_layout.py`, the truncation floor in `pipeline/4_perlector/truncation.py`,
  the `real_route` gate in `pipeline/5_recensor/run.py` citing F068/F083 in its own
  docstring, `POD_RESIDENCY_LOCK_PATH`/`SERVING_LOGS_DIR` in `common/runtree/store.py`).
- Two Tyrel rulings (Stage-1-witnesses-only for F006/F060; the glitch-vs-pattern threshold
  for F084) are honestly still unimplemented in the current tree — confirmed by reading
  `config/models-real.toml` and `pipeline/4_perlector/live_reader.py` — but both are
  explicitly disclosed in the ledger/PR as ruled-and-queued rather than silently dropped, so
  this is not a hard-rule-7 violation and isn't reported as a new finding.
- **Not checked**, for lack of remaining budget in that pass: roughly two-thirds of the 112
  findings (F003-F004, F016, F019-F020, F025-F027, F033-F038, F041-F048, F051, F054-F067,
  F070-F078, F080-F081, F085-F087, F091, F093, F097-F112) and whether the "declined for
  follow-up" items are actually tracked as tasks somewhere (`workbench/` is gitignored and
  unavailable to a read-only session, so only their presence in the PR's own record could be
  confirmed, not their downstream tracking). A full disposition audit would need to cover
  these; naming the gap rather than implying full coverage.

## Gate result

**Green, independently confirmed twice.** This session ran `check-fast.sh` (ingress checks
on the current worktree, `check-static.sh`, then `pytest -m "not full or scanner"`) to
completion against HEAD (`485283b`) with a pinned uv 0.12.1: **9485 passed, 46 skipped, 2
xfailed, 0 failed**, in 36m51s. A separate reader in the review workflow independently ran
`check-static.sh` plus the same pytest selection again from a fresh process and got the same
result — no failing test, nothing to root-cause. The 2 xfails are finding 1 above's known
narration defect, not a surprise. This does not stand in for `check-all.sh`'s full
frozen-audit gate (the `--full`/`scanner`-marked tests and the dependency-audit group are out
of scope here, per the coverage gaps above), but the everyday gate this project runs on
every commit is clean at HEAD.

## Dispositions (hard rule 13 — a decision recorded for every non-governed finding, not a
deferral)

Tyrel gave this session more budget mid-review and asked it to keep going, so findings 1, 2,
6, and 7 moved from "declined for now" to fixed in this same PR. CodeRabbit's CLI is not
reachable in this sandbox (no credentials/network route to it here); substituted with an
independent second reader (Opus, high effort) on the two pipeline-stage-tier changes (1, 2)
per the proportionality table, plus each touched suite run individually. **Correction, caught
by CodeRabbit's own review of this PR:** the sentence originally here claimed a full
`check-all.sh` gate had already run on the complete diff at this point. It had not — the
paragraph two below this one already said, correctly, that the full gate was still in
progress as this section was written. `check-fast.sh` plus the touched suites, run above and
matching this project's own review-proportionality table for this tier of change, is the
substitution actually made here; the full `.githooks/check-all.sh` gate is a merge
precondition (hard rule 14), run once on the head that actually merges, not on every
disposition round.

| # | Finding | Disposition |
|---|---|---|
| 1 | Operator run-screen page mismatch | **Fixed, in two rounds.** Round 1 (`b80746d`): `_declared_work` filters pages/acts through `pipeline/1_exemplar/door.py::fixture_pages_for_scenario`; both G21 xfail tests pass for real. An independent reader (Opus) then found the fix only covered the two scenarios its own tests exercised — under `ink-free-page` and `refused-page` (both part of the original F030, not new discoveries) the closing accounting line still mismatched its own total, for reasons the static fixture declaration structurally cannot know (a runtime-minted act; a page refused after being declared). Round 2 (`b80746d`'s direct follow-through, same PR): added `_exported_work`, which reads the completed run's real Armarium export instead of the static declaration for the closing line only (the opening line still correctly uses the static declaration, since no real record exists yet); verified by running the CLI directly against both scenarios, not just by reading the diff. A second independent review of *that* round found five further real defects — an unguarded `sys.modules` leak in `_door_module`, three unguarded dict reads that could crash with a confusing "unclassified error" message on a malformed fixture row, and `_exported_work` silently dropping a malformed record from its printed names while the caller still printed the raw count as the total (reopening the same class of mismatch this whole fix exists to close) — all fixed (`6a89f69`), each with a new test, full `operations/operator/` suite green throughout. |
| 2 | Armarium cross-row noise-floor check | **Fixed**, commit `daaa153`. `_validate_ink_map_pages` now refuses a bundle whose flagged pages disagree on the sealed noise floor. Full `pipeline/7_armarium/` suite green. |
| 3 | README status line | **Reported to Tyrel, not edited.** Governed path (hard rule 10); the main session applies a change only once he approves substance, through the governed-edit procedure. |
| 4 | ARCHITECTURE.md Recensor real-ingress gap | **Reported to Tyrel, not edited.** Governed path, same as above. |
| 5 | Perlector doubt channel undocumented | **Reported to Tyrel, not edited.** Governed path, same as above. |
| 6 | `build_fixture.py` self-check | **Fixed**, commit `f330033`. Reader-doubt/gap offsets now self-check against the actual source text at generation time. Output byte-identical; full `proof/` suite green. |
| 7 | F040 half-fixed (uv pin duplication) | **Fixed**, commit `16bae29`. `check-all.sh` and `ci.yml` now read the version from `pyproject.toml` once; added a reconciliation test. Full `.githooks/test_ci_workflow.py` suite green (33 tests). |

Finding 2's independent review (Opus) held on first pass: the Armarium cross-row check is
correct as landed, no follow-up needed. **Correction, same catch as above:** this paragraph
originally said a full `check-all.sh` gate run on the complete diff was "in progress" and
that this file would be updated once it landed — it never was; no result for that run is
recorded anywhere in this file. The full gate was never confirmed for this disposition
round; only `check-fast.sh`-equivalent checks and the touched suites were, as corrected two
paragraphs above. A second, broader pass re-verifies the rest of PR #117's own disposition
claims below. Nothing above is a TODO left in the diff itself — each line is a decision with
its reason, recorded here as this project's convention requires.

**What two rounds of independent review on finding 1 says, worth recording rather than
letting pass unremarked:** every round caught something real. The first review didn't just
rubber-stamp a plausible fix; it found the fix solved the two cases its own tests happened to
cover and quietly left the rest of the same finding open. The second review, on the fix for
*that*, found the fix for the fix had its own new problems — a resource leak and three
unguarded dict reads a hostile or merely malformed fixture row would hit. Fixing a bug is not
the same activity as confirming a fix is complete, and this project's own history (F030 and
F040 both marked "fixed" in PR #117 without actually being closed) says that confusing the two
is a standing risk, not a one-off. Budget for the second pass, not just the first.

## Second pass: re-verifying the rest of PR #117's own disposition claims

Two of PR #117's fourteen "fixed" groups turned out incomplete when actually checked (F030/G21
was honestly declined in the PR body, not falsely claimed — see below; F040/G15 was falsely
claimed fixed). Given that hit rate, the remaining twelve "fixed" groups and the five "refuted"
claims in the same PR body warrant the same treatment: read the actual current code against
the specific claim, not the commit message's summary of it, and run what can be run quickly.
A second workflow was launched for this (twelve fix-groups plus the five refutations, each
independently verified, each finding adversarially re-checked by a second reader before being
kept). **Twelve of the twelve "fixed" groups checked out clean at current HEAD** (G1, G2, G3,
G4, G5, G6-engineering, G7, G8, G20-part, G23-part — real tests actually run and green for
each, not just code read) — those claims genuinely hold and are not relitigated here. Four of
the five refutations also held on independent re-check (one, the RecursionError/`read_artifact`
one, had already been checked in the first pass); the fifth ("verified under `-O`") is a
precision note about the *ledger's wording*, not a code defect — no test actually runs
`python -O`, though the guard it describes is immune to `-O` by construction (it is
`SpendPolicy.__post_init__`'s explicit `raise`, not a bare `assert`), so the substance holds
and only the phrase overclaims.

**Three real gaps did surface, all fixed in this same PR** (commits `fa4c162`, `6d0e243`):

- **G13's own claim wasn't fully true.** "Every untrusted-parse boundary... refuses by name...
  instead of escaping" covered the JSON-parsing step but not `_verify_retained_references`,
  which walks the already-parsed structure with its own separate recursion; 5 of its 6 call
  sites had no `RecursionError` guard. Reproduced directly (a row shallow enough to parse but
  with one deeply nested value inside it reached callers as a bare `RecursionError`). Fixed with
  a single wrapper, `_verify_retained_references_bounded`, that every external call site now
  goes through — so a future added call site inherits the guard rather than needing to
  remember it, unlike the five that didn't.
- **"Export refuses an ambiguous --run-id" was never implemented at all.** Grepped the whole
  tree and the original PR diff for "ambiguous": zero hits related to run-id resolution.
  `export()` took the latest of every receipt matching a run_id unconditionally, with no check
  that they agreed on `run_root` — two genuinely different runs colliding on the same run_id
  under different roots would resolve silently to whichever was recorded most recently. Fixed:
  a new refusal (`ErrorCode.EXPORT_AMBIGUOUS`) fires when matching receipts disagree on
  `run_root`, naming every candidate, and `export` gained an optional `--run-root` (mirroring
  `review`'s existing one) to name the intended one.
- **`--evidence-prefix` retyping was only half-eliminated.** `--evidence-key` deriving from a
  saved `--launch-receipt` was genuinely fixed (confirmed), but `--evidence-prefix` still
  defaulted to the whole `preflight/` tree even with a receipt named — the identical
  "retype a 32-hex token by hand" problem the receipt-derivation feature exists to eliminate,
  just still present for the other flag. Fixed with `launch_evidence_prefixes`, reusing the
  same data `launch_evidence_keys` already reads, wired into fetch-run's dispatch the same way.

All three touch pipeline-stage or pod/evidence-fetching code, so an independent review (general-
purpose agent with a real shell this time, not a read-only one — the first review round in this
session found that gap the hard way) was dispatched before this section was first written.

**This session pushed a real bug, and the independent review caught it before any harm.** The
review found the `--evidence-prefix` fix above was itself wrong: `launch_evidence_prefixes`
derived `relative.parent / relative.stem` from every bound report path, an inference from a
single, unrelated grep hit ("CSPRNG witness under `<volume-mount-path>/preflight/`") rather than
from reading how a real request is actually built. Every real request
(`boot_a_request.py`/`boot_b_request.py`) writes its report paths at the **volume root** — never
under `preflight/` at all. Only `bootstrap_main.Plan.preflight_root` computes a `preflight/`
path, from `<mount>/preflight/<bootstrap_main's own --report-path stem>` specifically, not the
pod timer's outer report and not (for a full run launch) `pod_run`'s own nested report. The
pushed derivation matched none of the three, and because a *derived-but-wrong* prefix silently
**overrode** the surface's correct whole-tree default, `fetch-run --launch-receipt X` with no
explicit `--evidence-prefix` would have fetched **nothing** — where it previously fetched
everything. On a paid, non-repeatable pod run that is exactly the GOALS 4 / GOVERNANCE 2 harm
this whole finding exists to prevent, caused by the fix meant to prevent a smaller version of it.
Both test fixtures for the wrong fix happened to put report paths under
`/workspace/preflight/…` — the same "two covered cases, a third real one missed" shape this
session has now found repeatedly in *other* people's code, reproduced in its own.

Corrected within the hour (commit `5c6bdcb`, same PR, before any run could hit it): derives from
bootstrap_main's own nested report path specifically, found by splitting the nested argv at the
first literal `--` (`models._nested_argv_halves`, the same split `pod_run.split_argv` performs
for real, at boot); verified empirically against both a full Boot B shape and a hold-only Boot A
shape, matching the real `preflight_root` formula exactly, not just a plausible-looking string.
The same review also found a smaller issue in the export-ambiguity fix (a malformed, unrelated
receipt for the same run_id could block export of a sound one) — tightened in the same commit.
Full `operations/operator/`, `operations/pod/`, and `pipeline/7_armarium/` suites: green
throughout, with the corrected fixtures.

**Recorded plainly rather than quietly folded into a clean-looking history, because this file's
own point is that a fix is not verified until an independent read confirms it, no matter who
wrote it** — including this session's own second-order fixes, which is exactly the case here.

**Correction to the record above:** F030/G21 was not a false "fixed" claim. PR #117's own body
lists it under "Declined for this pull request, with reasons (each queued as a task)": "G16,
G17, G14, G18 copy, G21 ... share the operator files with this PR's receipt changes and were
held for a follow-up to keep this diff reviewable. The G21 test is committed here marked xfail
naming the finding." That is exactly what this session found and exactly what hard rule 7
requires — a known gap, disclosed, not silently dropped. Picking it up this session was
legitimate follow-up work, not catching a lie, and it is worth being precise about the
difference: F040 was PR #117 saying something was fixed when it wasn't; F030 was PR #117
correctly saying something was not fixed yet.

## Bottom line

Ten independent finders covered the full `d6b3c42..HEAD` diff (175 files, +26,825/-1,461,
including today's HEAD commit) plus README/ARCHITECTURE consistency and a ledger spot-check;
every medium/high finding was adversarially re-verified against the current tree, not taken
on the first reader's word. **The gate is green (9485 passed, 0 failed, confirmed twice
independently). No correctness regression and no silently-lost result was found in the new
diff.** Five real findings survived verification — one already-known, deliberately-tracked
UX defect (finding 1) worth prioritizing next; one defense-in-depth gap in the Armarium
export (finding 2); and three governed-document accuracy gaps (findings 3-5) that need
Tyrel's decision, not a session's edit. Two lower-severity leads and a ledger-disposition
correction (F040) round it out. **Recommended next action: Tyrel reviews findings 3-5 (the
governed-doc discrepancies) and decides wording; the next working session picks up findings
1, 2, 6, and 7 as ordinary engineering, each already scoped and cited above.**

## Fourth pass — spot-checking the 2026-09-14 ledger's remaining findings

Continuing the same session, under Tyrel's standing instruction to keep working and pushing
to this same pull request until either usage runs out or nothing more is left to find. This
pass returns to the task named at the top of "Coverage gaps" above and in the second pass's
own "Not checked" list: roughly 75 of the 2026-09-14 ledger's 112 findings had never been
individually checked against current HEAD at all (as distinct from the ~37 PR #117 claimed
to have fixed, which the second and third passes above already audited).

### Fixed in this pass

**F098 — a laptop-driven run's receipt named its own commit; the orchestrator invocation
running under it did not** (`operations/operator/surface.py`). `run()` already read
`_repository_commit_or_reason` and recorded the commit on the receipt, but never passed it
to the orchestrator subprocess — `pod_run` already does this for a pod-driven run. Fixed by
extending the subprocess command with `--repository-commit` whenever the commit is readable
(mirroring the receipt's own `commit_unreadable` case when it is not). Test added to
`test_every_run_receipt_carries_identity_configuration_commit_and_output`. Commit `31acf0d`.

**F004 — a top-level flag typed after the verb failed with no hint of the fix**
(`operations/operator/cli.py`). `--workspace`/`--state-dir`/`--notify` live only on the
top-level parser; argparse rejects them as "unrecognized arguments" once the verb token is
consumed, with nothing pointing at the actual cause. `PlainParser.error` now recognizes this
one specific message shape and appends a sentence naming which flag(s) belong before the
verb; every other argparse message, genuine typos included, is untouched. New
`operations/operator/test_cli.py`. Commit `222fbd9`.

**F085 — every no-picker screen walked only `dict` and `list`; a `tuple` hid everything
beneath it** (`common/corpus_register.py`, `pipeline/4_perlector/dossier.py`,
`common/physical_act_partition.py`, `common/cross_capture_autopsia.py`,
`common/cross_capture_dissent.py`). `common.contracts.canonical.canonical_bytes` serializes
a tuple exactly like a list, so a forbidden preference field wrapped in one reached a sealed
artifact looking like an ordinary array member — reproduced directly before the fix:
`refuse_capture_preference({"a": ({"preferred": "cap1"},)})` returned without refusing.
This is the same runtime half of GOVERNANCE 3 (hard rule 8, "do not build a picker") the
whole `common/test_preference_screen_walks.py` family exists to guard, so all five
independent walks were fixed, not only the two the ledger entry happened to name (the other
two screens in that family, `physical_act_partition._refuse_preference` and
`triage._refuse_preference_named`, both delegate to `refuse_capture_preference` and needed
no separate change). A `set` is deliberately not addressed: unlike a tuple it cannot reach
`canonical_bytes` silently — `json.dumps` raises `TypeError` on one — so it already fails
loudly by an existing path rather than smuggling anything through; named here rather than
silently scoped out (hard rule 7). New regression test drives all seven screens in the
family with the reproduction above. Commit `46aa896`.

**F087 — the reader's own record could not say whether a wall-clock deadline was actually in
force** (`pipeline/3_attestatores/run.py`, plus `pipeline/4_perlector/run.py` and
`pipeline/5_recensor/run.py`). `common/alignment.py::align_to_anchor` already answers
whether its SIGALRM backstop was actually armed for a given match (a prior round of this
same session added that); `pipeline/3_attestatores/run.py` called it and republished a
subset of its result into the published `alignment` record, but dropped that one field, so a
bounded alignment and one that ran fully unbounded and happened to finish both published
`{"status": "aligned", ...}` indistinguishable from each other. Threaded through the two
sites that actually publish an `aligned` record from a real `align_to_anchor` result (or,
for the "genuinely-empty" trivial attach, disclose `False` because no witness text means the
matcher never ran at all) — deliberately not added to any `unaligned` record, since a fired
deadline already names itself via `reason: "alignment-deadline-exceeded"` and every other
unaligned branch here never called `align_to_anchor` at all. Perlector and Recensor each
carry their own independent closed-shape check on the published `aligned` record (a
documented, deliberate duplication against exactly this kind of drift); both were updated
to require and type-check the new field. Commit `644cb70`.

**A mistake this session made and caught before it shipped, recorded plainly rather than
folded away:** the first version of the F087 fix also added `deadline_in_force` to every
`unaligned` alignment dict, on the reasoning that GOVERNANCE 2's "the measurement is
recorded all the same" argued for uniform presence. That broke Perlector's and Recensor's
exact-key-set schema checks for the *other* `unaligned` shapes those same dicts cover (a
non-reading page-outcome refusal, a continuation-page mirror, an ambiguous-overlap
downgrade) — both stages have a hard-coded `set(alignment) != {"status", "reason"}` check
for the unaligned case, uniform across every reason code, so adding a key to only *some*
unaligned constructions broke that equality for the whole shape at once. This was caught by
running the full `pipeline/3_attestatores` + `pipeline/4_perlector` + `pipeline/5_recensor`
suite before pushing — not a targeted selection — which failed real end-to-end orchestrator
runs with `SchemaRefusal: an attached page witness has no computed alignment` across 14
tests and 8 fixture errors. The fix was corrected to scope `deadline_in_force` to the
`aligned` shape only, the two consumer schemas were updated to match, and the full
three-stage suite was re-run clean (all green, confirmed twice from separate invocations)
before the corrected version was pushed. The lesson already recorded earlier in this file —
that a change touching a shared record shape needs the full consuming suite, not just the
producer's own tests, before it ships — held again, and held because it was actually
followed this time.

### Ten more findings closed, spot-checked from the same 2026-09-14 ledger

Continuing the fourth pass: a dedicated workflow independently re-verified 26 of the
ledger's remaining unchecked findings against current HEAD (seven agents, each reading the
full original claim, re-deriving it from the current code, and recommending fix/decline
with reasoning — not taken on the first reader's word). Four came back already fixed by
earlier work (F003, F016, F102, F107 — confirmed by re-reading the current code and, for
F016, by re-running its own regression test); the rest are dispositioned below and in the
next section. Ten were judged genuinely fixable now and were:

**F041 — the held-acts header counted hold records, not acts** (`operations/operator/review_text.py`).
Contradicted README.md's own documented account of the header and the distinct-act count
`review.py:1467` already computes for the summary sentence directly above it. Now derives
the same count. Commit `66b11e5`.

**F109 — notify.sh depended on a `python3` on PATH rather than this checkout's own
interpreter** (`operations/notify/notify.sh`). A pod image, minimal Linux install, or a Mac
with only the Command Line Tools' stub `python3` would fail loudly exactly where
`.venv/bin/python` would have worked. Now prefers `$root/.venv/bin/python` when executable.
Commit `ddf31ba`.

**F020 — the outbound notification message had no length ceiling** (`operations/operator/surface.py`).
A held run with hundreds of unresolved pages/acts could build a message of unbounded size,
passed as one argv element to a script that posts it to a third-party service with no size
check anywhere in that chain. Capped at 500 characters, mirroring the 160-character cap
`notify_bridge` already applies to its own failure-detail string. Commit `57a72b0`.

**F104 — backup admitted OS-generated residue as run-tree evidence** (`operations/operator/backup.py`).
A `.DS_Store` or AppleDouble `._*` sidecar was silently copied and inventoried exactly like
a real run-tree member. Now excluded before being read at all; deliberately not recorded in
the snapshot the way a publication temporary's exclusion is, since that would need the same
schema-version bump F103 (below) already names as due its own review. Commit `79de3e6`.

**F035 — a mistyped run id was reported as damaged evidence to preserve and investigate**
(`operations/operator/review.py`). `RunTree.__init__` only validates the id's shape, so a
name that names nothing reached the catch-all meant for a tree that exists and failed
verification. Now checked at the exact path `RunTree` itself reads, before that catch-all,
and raised as `INVALID_COMMAND` instead. Commit `2d7ebae`.

**F025 — a ScanTailor project could name an absolute or traversing source path unchecked**
(`operations/operator/scantailor_worker.py`). `(project_path.parent / file_paths[fileId]).resolve()`
never validated either untrusted attribute it was built from; an absolute `directory path`
discards `project_path.parent` entirely (`Path.__truediv__`'s documented behavior) and `..`
was accepted outright, then `.resolve()`d against the real filesystem — a project naming
`<directory path="/Users/tyrel/.ssh"/><file name="id_ed25519"/>` would have resolved
straight to that real path. Fixed with the same checks `operations/pod/transfer.py::_under`
already applies elsewhere, plus a final containment check as defense against a symlink
crossed during resolution. Commit `c1ae01d`.

**F026 — the submission walk's aggregate-byte bound only ever counted retained bytes, always
zero on every production path** (`operations/submit/inventory.py`). `_Budget.admit`'s own
`size` parameter — the true bytes read, whatever `max_bytes` said — went unread by its body;
both production callers ask for `max_bytes=0`, so the aggregate-byte refusal was dead code,
and 100,000 files each just under the door's own 64 MiB per-file bound could stream
terabytes through the hasher before the file-count bound finally tripped. New
`MAX_SUBMITTED_READ_BYTES` now bounds the bytes actually read. Commit `114215a`.

**F027 — the upload receipt's top-level state could disagree with its own transfer record**
(`operations/operator/surface.py`). Hardcoded `"state": "complete"` beside an embedded
transfer record that can independently read `"nothing-to-transfer"`. Not reachable today
through `upload()` itself (the manifest snapshot it writes always exists by the time the
transfer checks for one) but asserted independently rather than derived from one fact —
exactly the landmine for a future caller of the same receipt shape. Now both derive from
`report.submission_manifest_present`. Commit `c89bd0f`.

**F063 — the volume's hourly rate was labelled observed from the provider when it never
is** (`operations/pod/provider_runpod.py`, `operations/operator/volume_cost.py`). RunPod's
v1 API publishes no network-volume price endpoint; every `PodEstimate` this codebase builds
for the volume side carries the injected `volume_price` resolver's figure, never a live
quote, yet the combined estimate source string and the close-report's own docstring both
claimed "observed." Both corrected to say what is actually true. Two independent reviews
confirmed this touches nothing requested, billed, computed, or provisioned, and that no
consumer depends on the old exact string by equality. Commit `d9626f3`.

**F108 — status had no arm for an advance, so the operator's own sequence could not be
fully reconstructed** (`operations/operator/cli.py`, `operations/operator/surface.py`).
`_backup_in_custody` already wrote a receipt `status` could read back; `_advance_with_confirmation`
did not, and `_status_projection` had nothing to read even if it had. New
`OperatorSurface.record_advance` mirrors `record_backup`'s pattern (success only — every
refusal here already raises `OperatorError` directly, so there is no failure state to
capture), wired through a `surface` parameter that defaults to `None` rather than forcing
all eighteen existing test call sites for this function to construct one. Commit `0b7c1e8`.

Three independent review passes (one per subsystem cluster: review/backup, notify/submission,
scantailor/upload/advance) were dispatched against all ten of these commits before they were
considered closed, each with real Bash access to re-run the actual test suites rather than
read-only inspection.

### Confirmed still valid, declined for now — with the specific reason each time

Per hard rule 13, a decline is a decision, not a deferral, and needs a real reason rather
than "ran out of time." Each of these was independently re-verified against current HEAD
(still reproducible, still the file:line the ledger named or its current equivalent) and
then declined for the reason stated:

- **F019** [medium, security] — a submitted image's decoder-error text (chunk names, tag
  numbers — attacker-chosen bytes) still reaches a push notification to a third-party
  service (`ntfy.sh`) through `common/contracts/outcomes.py`'s per-page reason string,
  unfiltered. A concrete fix exists (build the notification from the already-computed
  closed-vocabulary counts `aggregate.get("by_page_outcome")`/`by_category"` instead of the
  raw joined reason strings, leaving the full text in the console and receipt) but this is a
  security/data-handling-boundary change to this project's own "keep submitted material off
  operational channels" rule, and correctness here means proving no other `reasons.append(...)`
  call in `outcomes.py` — present or future — can smuggle file-derived text the same way.
  Per the review table that warrants one independent reader before it ships; queued rather
  than rushed in the same pass as ten other fixes.
- **F037** [medium] — two concurrent `verbatus run --run-id X` invocations both proceed
  unblocked and both report "Run complete"; nothing holds a lock for a run's duration the
  way `launch` already refuses a second in-flight window. The fix is a real behavior change
  to the `run` execution path (an OS-level advisory lock spanning the orchestrator subprocess,
  with a new refusal on contention) that must not deadlock or wrongly refuse a genuine
  solo resume — a correctness/concurrency change to sealed-evidence integrity, squarely
  "a pipeline stage or a contract" tier.
- **F038** [medium] and **F103** [medium] — an unaccounted file in a sealed stage is
  invisible to `review` and copied by `backup` as if it were evidence, and the backup
  snapshot's own schema (`mac-run-backup.v2`) carries no timestamp, source, host or
  completeness marker. Both need a versioned-schema change (a new field, a version bump,
  updates to `_verify_backup_snapshot`'s and `BackupReport`'s exact-field-set checks and
  every test that constructs one) — real work, not a same-day patch, and F104 above was
  deliberately kept schema-free specifically to avoid colliding with whichever version bump
  eventually lands both of these together.
- **F100** [medium] — a partial copy of a complete run reads as an interrupted one and
  `review` recommends resuming it, because no run-tree schema element records "this run
  reached the Armarium" independent of which stage directories a given copy happens to
  contain. The real fix is a new sealed run-level end record referenced from the Armarium
  boundary — a run-tree contract addition touching the orchestrator, `common/runtree/store.py`,
  and `review.py`'s stage-state derivation together.
- **F105** [medium] — macOS Finder residue inside a run tree (`.DS_Store`, `._*`) is
  reported as damaged evidence by the stage-seal verification path (`common/stage.py`'s
  `_stage_blob_inventory`), not merely by backup. Splits into a low-risk half (name the
  residue pattern in the refusal message) and a higher-risk half (skip it during seal
  verification, changing what a seal's own inventory digest is computed over) — the second
  half touches the same hardened, symlink/case-collision-aware walk `common/stage.py` and
  `common/runtree/store.py` share across every stage boundary, and should not ship as a
  solo cosmetic patch.
- **F034** [medium] — the one supported resume command omits the sealed roster trio
  (`--models-config`/`--serving-recipes-config`/`--witness-context-config`) when a run was
  sealed under a non-default one. Closing this properly needs `_next_action` to read
  `adapter_recipes` from the run tree and compare it against a shared "is this the fixture
  roster" reference the way `operations/pod/bootstrap_main.py` already does by path — but
  `review`'s read-only boundary never sees the external state-directory receipt that holds
  the literal config paths, so the best available fix is a diagnostic sentence, not a
  reconstructed command, and needs the interface change (threading `adapter_recipes` and a
  shared fixture-roster reference into `_next_action`) done deliberately rather than as a
  same-day guess at the naming convention.
- **F112** [low] — a Landlock/Seatbelt confinement backend failure is discovered only when
  `review`/`backup`/`advance` actually run it, never by `status`. The fix means `status`
  itself spawning a confinement probe subprocess, which changes `status`'s own documented
  contract ("read descriptors, receipts and leases without writes or provider calls") and
  touches the same custody module this project treats as safety-critical — a small idea
  with a security-adjacent surface that wants an independent reader, not a solo add.
- **F065** and **F066** [low] — no ports/datacenter are requested on pod creation (so a
  failed first pod cannot be inspected), and `pod_run` holds a completed run to the full
  lease as billed idle time rather than closing early. Both are live-paid-infrastructure
  behavior questions: F065 would change the literal HTTP body sent to a billed RunPod
  create call (opening a network port, pinning a datacenter) with no live account in this
  sandbox to verify the accepted shape against; F066's durable fix is explicitly named in
  `pod_run.py`'s own comments as a `pod_timer` contract change shared by every boot type
  that this unit deliberately does not make unilaterally. Hard rule 1 reserves "paid or
  live infrastructure" for Tyrel; both are named to him rather than guessed at here.
- **F081** [info] and **F080** [info] — every published run-tree file is mode 0600 with no
  widening (confirmed accurate, not a defect the ledger asks to fix outright), and a
  decode-environment difference after transfer is reported as one flat, undifferentiated
  stderr line mixing genuinely-expected fields (platform, machine) with real ones (decoder
  version). The first is a confidentiality-posture choice this codebase treats as
  deliberate elsewhere (least-privilege defaults for security-sensitive files); the second
  is explicitly deferred in `common/stage.py`'s own comments to a not-yet-landed policy
  decision ("Unit 17") about when a decoder difference becomes fatal — better decided
  alongside that than twice.

**Already fixed, confirmed by re-reading current code (not touched this pass):** F003
(`RUN_FAILED`/`UPLOAD_PARTIAL` already carry the captured reason, not just a receipt path),
F016 (pipeline stage subprocesses already use `credential_free_environment()`, confirmed by
re-running `test_pipeline_children_do_not_receive_any_provider_credential`), F102 (the
fetch-run receipt already records `datacenter_id`/`volume_id`/`endpoint_url` via
`_volume_record`), F107 (`record_unexpected` already writes a bounded receipt with
exception type, message, traceback, argv and cwd, and `status` already shows it).

## Fifth pass — CodeRabbit's automatic review on PR #119

The pull request opened for this review (`#119`) sat in draft, which the GitHub App reads
as "skip" — CodeRabbit's automatic review never ran until the PR was marked ready and
`@coderabbitai review` requested explicitly. Its pre-merge checks passed three
(`Title check`, `Carried Code Is Named As Carried`, `No Witness Picker`) and raised two;
its full walkthrough then posted 8 inline findings across the diff (2 major, 5 minor, 1
medium).
Every finding below was independently re-verified by reading the current code before being
accepted, not taken on the bot's word — several turned out narrower or wider than its own
one-line description, recorded under each entry.

### Fixed in this pass

- **F113** [medium] — `Nothing Is Lost Silently`. `_armarium_export`
  (`operations/operator/surface.py`) validated `pages`/`delivered`/`non_delivered` only
  when the key was present: `if member in payload and not isinstance(...)`. A record
  honestly missing one of the three — the shape a mismatched schema produces, not
  something the current producer (`pipeline/7_armarium/run.py`) ever writes, which always
  includes all three together — passed through unchallenged. `run()` then read only
  `aggregate["status"]` to decide `state`, and `_exported_work` defaulted the missing
  list to `[]` and printed the generic "the recorded acts" instead of naming or refusing
  the gap, so a run could be reported `complete` with its act partition unaccounted for.
  This is the same bug class already fixed once in this file's `_write_base_armarium_bundle`
  (that function's own comment cites the identical CodeRabbit precedent). Fixed by requiring
  presence, not just type, for all three members — closing the gap at the one place every
  caller of `_armarium_export` already goes through, so `run()`'s existing (and already
  correctly tested) `armarium-record-unreadable` path now catches it too, with no change
  needed to `_exported_work`'s own defensive fallback. Regression coverage: a new
  `run()`-level integration test (`test_run_refuses_a_complete_aggregate_with_no_act_partition`)
  drives the real `_armarium_export` — only `RunTree.read_artifact` is stubbed — with a
  `complete` aggregate and a `pages`/`non_delivered` payload missing `delivered`, and asserts
  the receipt lands as `armarium-record-unreadable`, never `complete`; a parametrized sibling
  (`test_the_export_reader_refuses_a_member_missing_entirely`) covers the same gap for all
  three members, `pages`, `delivered`, and `non_delivered` (the pre-existing non-list
  parametrize test was widened to cover `delivered` too, and its payloads rebuilt so each
  case corrupts only the one member under test, not left implicitly relying on iteration
  order once presence became required).

  **Independent review (Opus, high effort, read-only) held this correct and safe to push**,
  and found four further points, all addressed: (A) [medium, arguable] a `complete` record
  whose partition is *present but empty* still passed — `expected_acts` says otherwise, but
  nothing reconciled the two, so a foreign record could still print "Run complete" beside a
  false count. Fixed with one additional check in `run()`: when `state == "complete"` and
  `expected_acts` is an int, `len(delivered) + len(non_delivered)` must equal it or the run
  is refused the same way, with a new regression test
  (`test_run_refuses_a_complete_aggregate_whose_partition_undercounts_expected_acts`).
  (B) [low] four existing test doubles in `test_surface.py` described records the real
  reader would now refuse (stubbing `_armarium_export` directly, so they still passed) —
  given `"delivered": []`/`"non_delivered": []` so they stay realistic. (C) [low] the
  validation loop's own comment read backwards ("a missing member is required" instead of
  "presence is required") — reworded. (D) both addressed above: this entry now says what
  the parametrized sibling actually covers, and names the non-list test's own repair.

- **F114** [major] — a residue *directory* was walked and its contents backed up.
  `sync_run_tree` (`operations/operator/backup.py`) checked `_is_os_residue(name)` only
  after already deciding an entry was a regular file (`stat.S_ISREG`) — but `.Trashes`,
  `.fseventsd` and `.Spotlight-V100` are directories on macOS, so they were never reached by
  that check at all: they were walked like any other run-tree directory and their ordinary
  contents hashed, copied, and published into the backup snapshot. The one existing test for
  this exclusion (F104) covered only the file-form residue (`.DS_Store`, `._*`), so the gap
  was untested. Fixed by moving the residue check immediately after the symlink refusal and
  before the directory branch, so it applies uniformly regardless of entry kind. New test:
  `test_a_residue_directory_is_never_walked_into`, a `.Trashes/` directory containing a
  regular file, asserting the file never reaches the snapshot.

- **F115** [minor] — `operations/notify/test_notify.py`'s generated shell wrapper for
  `exec {sys.executable} "$@"` left `sys.executable` unquoted; a Python path containing a
  space would split into multiple words and fail to start, before the test could verify
  anything. Fixed with `shlex.quote(sys.executable)`.

- **F116** [minor] — `proof/build_fixture.py`'s `reader_gap` self-check compared
  `source_text[:row["offset"]]` against the declared `before` text, but Python slicing
  clamps an out-of-range stop index, so an `offset` past the end of the source text could
  not be told apart from one landing exactly at it if `before` happened to equal the whole
  source. Fixed with an explicit `row["offset"] > len(source_text)` bounds check first,
  mirroring the out-of-bounds check `READER_DOUBTS`'s own self-check already makes a few
  lines above it.

- **F117** [minor] — `_status_projection`'s `advance` status arm printed a state-relative
  `run_root` straight from the receipt instead of rejoining it against `state_root` the way
  the `run` arm already does with `_display_path`; an operator whose state directory held a
  run root under it would see a path relative fragment that does not exist from their
  current directory. Fixed to match the `run` arm exactly. New test:
  `test_status_rejoins_a_state_relative_run_root_for_an_advance_record` — none of the
  existing advance-record tests happened to use a state-relative run root, so the gap was
  untested.

- **F118** [minor] — `.githooks/test_ci_workflow.py`'s `test_the_pinned_uv_version_has_one_
  source_of_truth` only asserted the *current* pinned version string was not duplicated; a
  future version bump that left a stale hardcoded value in the actual install/compare
  commands, while some unrelated line still mentioned "pyproject.toml"/"required-version" in
  passing, would not have been caught. Strengthened with direct assertions that the exact
  command shapes — `check-all.sh`'s `case "$uv_version" in "uv $required_uv_version"|...`
  and `ci.yml`'s `pip install "uv==$required_uv_version"` — use the extracted shell
  variable, not a literal.

- **F119** [minor] — `test_created_records_own_the_pod_rate_alone_not_the_volume_rate_too`
  (F063's own regression test, `operations/pod/test_provider_runpod.py`) asserted
  `pod_hourly_usd` and the source string but never the injected `volume_hourly_usd` itself,
  so a regression that corrupted the volume rate specifically would still pass. Added
  `assert record.estimate.volume_hourly_usd == Decimal("0.05")`.

- **F120** [major, heavy lift] — a launch receipt for one run could be supplied to
  `fetch-run --run-id` naming a *different* run on the same volume. `_read_launch_command`
  (`operations/operator/cli.py`) already refused a receipt recorded for the wrong network
  volume, but never compared the sealed command's own `--run-id` (`pod_run`'s required flag,
  sealed inside the nested `--bootstrap-command-json` argv — not a top-level request field
  the way `volume_id` is) against the run being fetched. On the same volume this is quieter
  than the volume mismatch: every derived evidence key and prefix would still resolve to
  real objects, just another launch's, stored beside the fetched run and misstating their
  provenance rather than merely failing to find them. Fixed with a new public derivation,
  `launch.launch_run_id(docker_start_cmd)` (mirroring `launch_evidence_keys`/
  `launch_evidence_prefixes`'s own existing pattern — reads `pod_run`'s own argv half of the
  nested bootstrap command specifically, `None` for a hold-only launch which starts no run),
  threaded through `_read_launch_command`/`_derived_evidence_keys`/
  `_derived_evidence_prefixes` and the `fetch-run` verb's own call site (`args.run_id`), with
  a refusal matching the volume check's own shape when both the receipt and the request name
  a run id and they differ. New tests: `launch_run_id`'s own unit coverage
  (`test_launch_run_id_reads_pod_runs_own_run_id_flag`,
  `test_launch_run_id_is_none_for_a_hold_only_launch`,
  `test_launch_run_id_is_none_with_no_bootstrap_command_at_all`) and a CLI-level regression,
  `test_a_launch_receipt_for_another_run_is_refused_rather_than_used`, which also confirms
  fetching the run the receipt actually started still derives normally.

- The PR description didn't follow this repository's `.github/pull_request_template.md`
  headings. Rewritten to match exactly (`What changed` / `What it touches` / `Why it is
  here` / `Rebuild record, when applicable` / `How to undo it` / `What proves it works` /
  `Review candidate` / `Review findings`, plus the checklist).

- Two self-contradictions in this file's own "Dispositions" section, caught by CodeRabbit's
  review of this PR itself: it claimed a full `check-all.sh` gate had already run on the
  complete diff, while two paragraphs later (and the earlier "Gate result" section) already
  said, correctly, that only `check-fast.sh` plus the touched suites had run and the full
  gate was still in progress / out of scope for that round. Corrected both passages in place
  rather than papering over them, per hard rule 7.

### Independent review of F113 and F114–F120 (Opus, high effort, read-only, two rounds)

Both fix commits were independently reviewed before push, per this repo's review-
proportionality table (F120 in particular touches `operations/pod/`, the "pod, money,
credentials, serving" tier). Both reviews returned "correct and safe to push"; the second
found four further points, all addressed here rather than left for later:

- **A crash risk in the highest-risk file.** `_read_launch_command` (`operations/operator/
  cli.py`) called the new `launch_run_id(command)` *outside* the block that converts a
  malformed receipt into the named `FETCH_RUN_FAILED` refusal. `launch_run_id` does its own
  JSON decode of the nested `--bootstrap-command-json` value, which can recurse out on the
  same pathologically-nested input the *outer* receipt read is already guarded against
  (`_UNREADABLE_RECEIPT` names `RecursionError` for exactly that). Moved the call inside the
  guarded block so this failure shape gets the same clean refusal every other one does,
  rather than an uncaught traceback.
- **A test that didn't prove its own claim.** `test_launch_run_id_reads_pod_runs_own_run_id_
  flag`'s fixture put `--run-id r1` only in `pod_run`'s own argv half, so it would have
  passed identically if `launch_run_id` read the whole nested command flatly instead of the
  `pod_run` half specifically — the thing its own docstring says it must not do. Given the
  bootstrap half a conflicting `--run-id r2` (synthetic; `bootstrap_main` has no such flag in
  reality) so the test actually discriminates between the two readings.
- **A bool/int inconsistency.** The new `expected_acts` reconciliation check in `run()`
  used `isinstance(expected_acts, int)`, which is also true for `True`/`False` in Python — a
  foreign record with `expected_acts: true` would reconcile against the number 1. This file
  already excludes bool from an int check elsewhere (`_exported_work`'s page-ordinal
  validation); made the new check consistent with that precedent.
- **A documentation gap.** `operations/operator/README.md`'s `fetch-run --launch-receipt`
  section said only that an unreadable receipt refuses by name; it did not say a receipt for
  a different volume (already true before this round) or a different run (F120, this round)
  does too. Extended the same sentence to name both.
- **A leftover word from an earlier edit**, caught in the same pass: a stray "A" survived a
  previous rewording of the `_armarium_export` validation comment ("...different code). A
  Presence is required..."). Removed.

Three more points the reviewer raised were read and explicitly declined, not silently
dropped: a theoretical case where one pod boot legitimately runs two different run ids (the
code does not support that today — `pod_run` takes exactly one sealed run id per boot — and
the refusal already names the manual workaround, so there is nothing to change); the
`"--run-id"` flag name being a second, unreconciled literal copy of `pod_run`'s own flag
(true, but consistent with this file's existing `--report-path` copies, which have the same
gap — not new debt this round introduces); and the generated shell scripts in
`operations/notify/test_notify.py` having other `{tmp_path}` interpolations beside the
`sys.executable` one F115 quoted — those are already inside literal double quotes in the
heredoc, which already prevents the word-splitting a bare `sys.executable` was exposed to,
so there was nothing left to fix there.

### Sixth pass — CodeRabbit's re-review of `33d90c8..d81034a`

CodeRabbit's automatic re-review (triggered by the `@coderabbitai review` request after that
push) marked all 8 of the fifth pass's findings "✅ Addressed" and passed 4 of 5 pre-merge
checks (Title, Description, Carried Code, No Witness Picker). It raised 5 new inline review
comments and kept one pre-merge check failing. Four F-numbers cover them, not five: F123
below is one rewrite closing two of the five comments together (require `expected_acts`,
and reconcile distinct act identities rather than a raw count) — both landed on the same
few lines of the same reconciliation block, so one fix and one entry cover both. No number
between F122 and F124 is skipped by omission; F123 is simply two comments wide.

**Fixed:**

- **F121** [minor] — this file's own walkthrough said the fifth pass found "2 major, 6
  minor"; the actual detailed entries are 2 major, 5 minor, and 1 medium (F113). Corrected.
- **F122** [major] — `_read_launch_command`'s new run-id check (F120) only refused when
  *both* the receipt's and the request's run ids were known and differed; a receipt whose
  run id could not be derived at all (`recorded_run_id is None` — a hold-only launch, or a
  nested command that cannot be decoded) passed through untouched even when a specific run
  was requested. A hold-only launch still has its own real, derivable evidence keys (its own
  report path), just none that belong to any run — so this was reachable, not theoretical:
  `fetch-run --run-id r1 --launch-receipt <a hold-only boot's receipt>` would derive that
  boot's own keys and apply them to run `r1`'s fetch. Fixed by refusing whenever a run id is
  requested and the receipt does not *prove* it belongs to that run (`recorded_run_id !=
  run_id`, treating `None` as never proving anything), with a distinct message naming that
  the receipt proves no run at all versus proving a different one. New test:
  `test_a_hold_only_launch_receipt_is_refused_when_a_run_id_is_requested`, which also
  confirms the same receipt still derives its own keys normally when no run id is requested.
- **F123** [major, heavy lift] — the `expected_acts` reconciliation added for F113 (see
  above) only checked a raw `len(delivered) + len(non_delivered)` against `expected_acts`,
  and skipped the check entirely when `expected_acts` was missing or malformed rather than
  refusing. A raw length cannot tell a duplicated or malformed act entry from a genuine one
  ("Reconcile unique valid acts, not raw list entries" — CodeRabbit, grounded in this repo's
  own coding guideline, "a fault that drops, skips or silently substitutes one act is not a
  small bug"), and a missing `expected_acts` is exactly the same "mismatched schema" shape
  F113 already treats `pages`/`delivered`/`non_delivered` as -- the real producer
  (`pipeline/7_armarium/run.py`) writes `expected_acts` in the same unconditional payload
  literal as those three. Rewritten to: require `expected_acts` to be a present,
  non-boolean, non-negative int (refusing `complete` outright otherwise, the same as a
  missing `pages`/`delivered`/`non_delivered`); walk `delivered`+`non_delivered` together,
  refusing on the first entry that is not a dict with a string `act_key`; and require the
  *distinct* `act_key` count to equal both the raw record count (no duplicates) and
  `expected_acts` (no drops). This changes one existing test's own expected outcome —
  `test_a_missing_expected_act_total_is_named_on_screen_and_in_the_milestone` asserted a
  `complete` record with no `expected_acts` displayed "total not recorded" and *succeeded*;
  under the tightened reading that is no longer a legitimate shape, so it now asserts refusal
  (renamed `test_a_complete_aggregate_with_no_expected_acts_is_refused_not_displayed_as_
  unknown`). "total not recorded" is not dead code, though: a *held* run can still
  legitimately lack `expected_acts` (reconciliation is a precondition for claiming
  `complete`, not for every state), so a new sibling test
  (`test_a_held_runs_missing_expected_act_total_is_named_on_screen`) covers that display path
  going forward. Two more new tests cover the specific gaps CodeRabbit named:
  `test_run_refuses_a_complete_aggregate_whose_partition_double_counts_one_act` (the same
  `act_key` in both `delivered` and `non_delivered`) and
  `test_run_refuses_a_complete_aggregate_with_a_malformed_act_record` (an entry with no
  readable `act_key`). The shared `_complete_export` test fixture's own `delivered` entry
  was `[{}]` — no `act_key` at all — which every test built on top of it inherited without
  noticing, since nothing reconciled identities before this pass; given `"act_key": "a1"`.
- **F124** [minor] — the same slice-clamping gap F116 closed for an offset *past* the end of
  the source text is also open for a *negative* offset: Python accepts a negative slice
  bound by counting from the end, so `source_text[:row["offset"]]` with `offset = -1` reads
  as "all but the last character," which a `before` value could coincidentally match.
  Tightened the bound to `0 <= row["offset"] <= len(source_text)`.
- **F127** [minor] — `_notify`'s own 500-character ceiling (F020, Fourth pass) sliced the
  message to the full `MAX_NOTIFY_MESSAGE_CHARACTERS` and then appended a ~57-character
  truncation suffix, so a long notification actually sent exceeded the limit it announced by
  the suffix's own length. Caught by CodeRabbit's outside-diff-range comment on
  `surface.py`'s notify path, in the same review as F121-F124 but not itself one of the five
  inline findings addressed above. Fixed by reserving space for the suffix before slicing.
  The existing F020 regression test had the identical bug in its own assertion — it asserted
  `len(sent) <= MAX_NOTIFY_MESSAGE_CHARACTERS + len(suffix)`, tolerating exactly the
  oversized message instead of catching it; corrected to assert the real ceiling.

**Declined, with reason:**

- **F125** [major, pre-merge check: "Nothing Is Lost Silently"] — the backup snapshot
  records which files it excludes as publication temporaries (`excluded_publication_
  temporaries`), but not which it excludes as OS residue (F104, F114 above): "a later reader
  cannot distinguish an intentional exclusion from a missing source member." Confirmed
  accurate — `_verify_backup_snapshot`'s exact-field-set check
  (`{"schema", "run_id", "files", "excluded_publication_temporaries"}`) has no residue field,
  and this session's own new test for F114 proves the asymmetry directly. Declined for two
  reasons, not one: first, the fuller resolution CodeRabbit itself proposes — "extend and
  version the backup schema, record every excluded relative path... include that exclusion
  set in both pre-copy and post-copy reconciliation" — is exactly the class of change F103
  (Fourth pass, declined) already named as out of scope for a same-day patch: a version bump
  plus updates to `_verify_backup_snapshot`'s exact-field-set check and every test in
  `test_backup.py` that constructs a snapshot record by hand. Second, and more load-bearing:
  the *simpler* alternative resolution CodeRabbit also offers — "prefer refusing the backup
  when OS-residue entries are present" — would not be a smaller version of the same fix, it
  would undo F104's own reason for existing. F104 (already merged, before this PR) was
  written specifically so that ordinary Finder/Explorer droppings left by a person simply
  opening a run tree in a Finder window do not fail an otherwise-good backup; refusing
  whenever residue is present would make backup fail on exactly the routine case F104 exists
  to tolerate. Recording an exclusion is real, deliberate schema work belonging with F103's
  disposition, not a same-day patch riding on this pass's fixes; named to Tyrel as a
  reconciliation option for when that schema-version work happens, not silently dropped.

`operations/operator/test_surface.py` (283 tests, up from 279 — four net-new reconciliation/
refusal tests, including one covering the `export()` gap named below; one existing test kept
its `complete` scenario but had its expected outcome changed from success to refusal per
F123 above, and a new sibling test covers the same missing-`expected_acts` gap for a *held*
run instead), `operations/pod/test_pod_run.py` (up two tests for F120/F122), `operations/
operator/test_backup.py`, `operations/notify/test_notify.py`, `operations/pod/
test_provider_runpod.py`, `operations/operator/test_cli.py`, `proof/` (64 tests, up one for
the negative-offset regression named below), and
`.githooks/test_ci_workflow.py` (33 tests) are all green individually; the full
`operations/`, `proof/`, and `.githooks/` directories together are green with no failures.
`ruff format --check` and `ruff check` pass across the whole repository.

`check-fast.sh` (ingress checks, document check, `ruff`, and — with `shellcheck` installed
mid-session, previously absent from this sandbox — the shell-script lint) passes apart from
two pre-existing `shellcheck` warnings in `.githooks/check-all.sh`, a file untouched by this
round; confirmed identical on base commit `485283b`'s own copy of that file, so not a
regression from this diff, and not run by GitHub Actions CI at all (no workflow invokes
`shellcheck`).

### Independent review of `db576b4` (Opus, high effort, read-only, round three)

A third independent read-only review, dispatched on the same commit CodeRabbit's sixth-pass
re-review (above) also covered, returned "correct" on all three code changes and found two
further points worth acting on plus two record-accuracy slips in this file's own sixth-pass
section, both from this same pass's own earlier writing. All four addressed here.

**Fixed:**

- **F128** [major in effect] — the same "complete" claim this pass's F123 taught `run()` to
  refuse could still reach `verbatus export` unreconciled. `run()` and `export()` both read
  the Armarium record independently and both decide "complete" from it, but the F113/F123
  reconciliation lived only in `run()`. Reachable in the ordinary sequence this project's own
  verbs support, not only in theory: `verbatus run` reads a record that claims complete but
  does not reconcile, refuses it, and writes a run receipt recording that refusal; a later
  `verbatus export --run-id` for the same run selects receipts by run id alone, re-reads the
  *same* Armarium record fresh, and — with nothing in `export()` to catch it — would publish
  it as complete: receipt, exit 0, and the "landed" phone milestone, exactly the shape
  GOVERNANCE 2 forbids. Fixed by extracting the reconciliation into a shared method,
  `_require_reconciled_act_partition`, called from both `run()` (unchanged in effect) and
  `export()` (new — checked immediately after reading the record, before any bundle is
  written, so a reconciliation failure joins export's existing "record could not be read"
  refusals as `EXPORT_MISSING` rather than reaching the bundle-write stage at all). Three
  existing export tests whose stub payloads claimed `complete` with an empty partition and no
  `expected_acts` (testing bundle-write-stage refusals, not reconciliation) needed
  `"expected_acts": 0` added so they still reach the code path they were written to test.
  New regression: `test_export_refuses_a_complete_record_whose_partition_does_not_reconcile`.
- **README wording** — `operations/operator/README.md`'s `fetch-run --launch-receipt`
  section named "a different network volume or a different run" as refused; extended to name
  a receipt that "proves no run at all" (a hold-only boot's receipt, asked for while fetching
  a named run — F122's own refusal shape), matching what the code actually refuses.
- **Negative-offset test coverage** — F124's bounds check (`0 <= row["offset"] <=
  len(source_text)`) had no test proving its negative branch; the existing declared fixture
  data has no such row to exercise it incidentally. Added
  `test_a_negative_reader_gap_offset_is_refused` in `proof/test_proof_fixture_build.py`,
  driving the real `build_skeleton_fixture` with `READER_GAPS` monkeypatched to one row whose
  offset is `-1`, everything else left real.
- **Two slips in this file's own sixth-pass writing, caught by the same review**: the opening
  paragraph above said CodeRabbit "raised 5 new inline findings" while the Fixed list below it
  names four F-numbers (F121-F124) — not a lost finding; F123 is one fix covering two of
  CodeRabbit's five review comments (require `expected_acts`, and reconcile distinct
  identities), both on the same lines of the same block. The sentence now says so explicitly
  rather than leaving an unexplained gap that reads as a dropped finding. Separately, the test
  totals paragraph said the renamed F123 test's "scenario changed from a held run to a
  complete one" — backwards; the renamed test's scenario was `complete` throughout, only its
  expected outcome changed (success to refusal), and the *new* sibling test is the one that
  covers a held run. Corrected in place, per hard rule 7, the same way this file's own
  fourth-pass self-contradiction about `check-all.sh` was corrected earlier in this document.

**Declined, with reason:**

- **A defensive-coding tightening in `_require_reconciled_act_partition`** — a non-list
  `delivered`/`non_delivered` is currently treated as empty (contributing zero act records)
  rather than refused outright, unlike a malformed *entry* inside one of those lists, which is
  refused. The reviewer would raise on a non-list for consistency with the entry check beside
  it. Left as is: `_armarium_export` already proves both are lists for every payload that goes
  through the real reader, so this is defense-in-depth for a test double that bypasses that
  reader entirely, not a path any real caller reaches — and the current behavior already fails
  closed (a non-list partition can only ever make reconciliation fail, since `expected_acts`
  is never `0` for a genuine complete export per `pipeline/7_armarium/run.py`'s own conservation
  check). Worth revisiting if this method's own lines are touched again, not on its own.
- **An empty-string `act_key`** technically satisfies the entry check's `isinstance(..., str)`
  test. The real producer can never emit one (`common/stage.py` refuses an empty act key at
  the seal). Cosmetic; not worth a same-day line for a case the producer cannot reach.
  **Superseded below (F130): this reasoning was inconsistent with why the rest of this same
  method exists.**

**Verified, not re-litigated:** the review re-confirmed F125's decline (the backup
OS-residue audit-trail finding) as "defensible engineering... not a rationalization," reading
the same F103/F104 precedent this file already cites, and found the residual risk it still
carries — residue is matched by filename only, at any depth, so a real artifact that happened
to be named like one would be silently dropped with nothing in the snapshot to say so — worth
naming plainly rather than treating F125's decline as closing the question. Recorded here so
it is not lost: no pipeline stage names its artifacts this way today (they are digest- and
id-derived), so there is no known live exposure, and this is not a new decision, just F125's
own residual risk stated once more in the open.

Two full test rounds after these fixes: `operations/operator/test_surface.py` (283 tests) and
`proof/` (64 tests) are green individually; the full `operations/`, `proof/`, and
`.githooks/` directories together are green with no failures. `ruff format`/`check` clean
repo-wide.

## Seventh pass — CodeRabbit's re-review of `f9b18af`

CodeRabbit's next review pass, on the commit that landed F128, kept all five pre-merge
checks green (Title, Description, Carried Code, No Witness Picker, and — newly present
this round — Nothing Is Lost Silently) and raised one further inline finding on the
reconciliation check itself.

**Fixed:**

- **F129** [minor, classification only — no change to what is refused] —
  `_require_reconciled_act_partition`'s three refusals (F113/F123/F128) raised a bare
  `ValueError`, so both callers caught it with their generic `except Exception` and filed
  it under the same code and receipt state as an export record that could not be read at
  all: `run()` wrote `"armarium-record-unreadable"` and `export()` raised
  `ErrorCode.EXPORT_MISSING`, whose own copy says "There is no completed Armarium export
  record for that run" — false for this case, since the record exists and was read; only
  its "complete" claim does not hold up. Fixed exactly as suggested: a dedicated exception,
  `UnreconciledActPartitionError(ValueError)`, defined at module level next to the file's
  existing `FetchRunRefusal` convention; `_require_reconciled_act_partition`'s three raises
  now use it. `run()` gains a branch ahead of its generic fallthrough that writes
  `"armarium-record-unreconciled"` (kept under `ErrorCode.RUN_FAILED`, per CodeRabbit's own
  instruction — this is still a run-level failure) with a summary naming that the record was
  read, not missing. `export()` gains `except UnreconciledActPartitionError` ahead of its
  generic `except Exception`, raising the new `ErrorCode.EXPORT_UNRECONCILED` with copy that
  says the record was found and read, its acts do not reconcile, and directs the operator to
  `verbatus review` and Tyrel rather than to re-running `verbatus run` (`EXPORT_MISSING`'s
  own advice, wrong here). The five existing reconciliation tests that asserted the old
  shared state or code — `test_run_refuses_a_complete_aggregate_whose_partition_undercounts_
  expected_acts`, `..._double_counts_one_act`, `..._with_a_malformed_act_record`,
  `test_a_complete_aggregate_with_no_expected_acts_is_refused_not_displayed_as_unknown`, and
  `test_export_refuses_a_complete_record_whose_partition_does_not_reconcile` — now assert
  the distinct state/code; two other tests, which fail before reconciliation is ever
  reached, correctly keep asserting `"armarium-record-unreadable"`, because that failure
  mode is unchanged: `test_run_refuses_a_complete_aggregate_with_no_act_partition` drives
  the real `_armarium_export`, which refuses a record missing `delivered` outright, and
  `test_a_non_list_pages_record_is_a_named_run_failure_not_a_character_count` is refused
  earlier still, by `run()`'s own inline page-list check, before reconciliation is called at
  all.

Full `operations/`, `proof/`, and `.githooks/` directories together green (no new tests
added — this round reclassifies five existing refusals rather than adding a new one).
`ruff format`/`check` clean on every touched file.

**Independent review of `afbb72b` (Opus, high effort, read-only)** confirmed the code
correct on every point checked — handler ordering in both `run()` and `export()`, every
updated test's actual code path, the new copy's accuracy, and that no other caller or
string match was missed — and caught two record-accuracy slips in this section's own first
draft: it said "six" reconciliation tests where five actually exist, and misattributed
*which* of the two unchanged tests fails inside `_armarium_export` itself (it is
`test_run_refuses_a_complete_aggregate_with_no_act_partition`; the page-record test fails
earlier, inside `run()`'s own check, never reaching the reader). Both corrected above, per
hard rule 7, the same way earlier passes in this document corrected their own slips. Also
raised, not requiring a code change: `operations/operator/README.md`'s export section did
not yet name the new `export-unreconciled` outcome (added, see below), and the new copy's
middle sentence describes the count-mismatch refusal precisely but is a little loose for
the other two reconciliation refusals (missing `expected_acts`, an unreadable act entry) —
reworded for accuracy.

## Eighth pass — CodeRabbit's re-review of `79e7192`, and reversing an earlier decline

CodeRabbit's review of the F129 commits kept the "No Witness Picker" and every other
carried-forward check green, confirmed F129 itself ("✅ Addressed in commits afbb72b to
79e7192"), and raised two more items: the standing F125 decline (unchanged, restated below)
and one genuinely new finding this pass fixes.

**Fixed:**

- **F130** [major — reverses this document's own earlier "cosmetic" disposition] — an empty
  string satisfies `isinstance(record.get("act_key"), str)`, so `delivered: [{"act_key":
  ""}]` with `expected_acts: 1` reconciled cleanly: one record, one distinct "identity", a
  run or export reported complete over an act nothing actually names. The sixth pass's own
  "Declined, with reason" section (in the independent review of `db576b4`) called this
  cosmetic because `common/stage.py` refuses an empty `act_key` at the Designator's seal and
  "the real producer can never emit one" — true, but beside the point:
  `_require_reconciled_act_partition` exists specifically to catch a record that never went
  through today's seal at all (an older build, a pod running different code — the same
  foreign-record rationale F113 and F123 already used to justify requiring a valid
  `expected_acts` and counting distinct identities rather than trusting a raw count). Relying
  on the producer to never emit an empty key was exactly the reasoning already rejected for
  the other two checks in this same method; applying it selectively to this one check was the
  actual defect, not the empty string itself. CodeRabbit's review cited this repository's own
  `.coderabbit.yaml` path instruction — a repo-wide instruction (`path: "**"`), not one
  specific to this file: "A fault that drops, skips or silently substitutes one act is not a
  small bug." Fixed exactly as suggested — the entry check now also rejects a falsy
  `act_key` alongside a non-dict record or a non-string one, before it can be added to
  `act_keys`. New regression: `test_run_refuses_a_complete_aggregate_with_an_empty_act_key`.
  The sixth pass's *other* declined item beside this one (a non-list `delivered`/
  `non_delivered` treated as empty rather than refused) named its own revisit trigger --
  "worth revisiting if this method's own lines are touched again" -- and this fix does touch
  them. Considered, not revisited: the fail-closed argument that decline gave still holds
  unchanged by this fix (a non-list partition contributes zero keys, which reconciles only
  if `expected_acts == 0`, and the real producer never writes `0` for a genuine complete
  export) -- this fix narrows what counts as a *readable* entry, it doesn't touch how a
  non-list `delivered`/`non_delivered` is handled before entries are even walked.

**Verified, not re-litigated:** the F125 backup OS-residue decline (recorded and
independently re-confirmed in the sixth pass, above) was flagged again by the same automated
"Nothing Is Lost Silently" pre-merge check, which re-evaluates fresh each round with no
memory of a prior round's decline. Restated on the PR itself rather than in code, since
nothing about the reasoning changed: the fuller fix duplicates F103's already-declined
schema-version work, and the simpler fix would undo F104's actual purpose.

**Independent review of `96a7eec` (Fable, read-only)** confirmed the fix's short-circuit
safety, that the new test genuinely fails without it and passes with it, and that it applies
symmetrically to both `run()` and `export()` through the shared method. Caught two slips in
this section's own first draft, both corrected above: it misattributed the earlier "cosmetic"
decline to the seventh pass (it is the sixth's), and it described the quoted
`.coderabbit.yaml` sentence as a path instruction specific to this file, when it is a
repo-wide instruction (`path: "**"`). Also surfaced, and now addressed above: the sixth
pass's other declined item (a non-list partition) had named its own "revisit if this
method's lines are touched again" trigger, which this fix's edit to the same method fired
without the eighth pass acknowledging it.

`operations/operator/test_surface.py` (284 tests, one new) green; `ruff format`/`check`
clean on every touched file.

## Ninth pass — a broad Sonnet-driven survey of subsystems this review had not yet reached

Everything above covered `operations/operator/`, `operations/pod/` (launch-receipt), `operations/
notify/`, `pipeline/orchestrator/`, `pipeline/7_armarium/`, `.githooks/`, and `proof/
build_fixture.py`. This pass surveys what was still untouched: every other pipeline stage
(`pipeline/1_exemplar/`, `1_ink_map/`, `2_designator/`, `3_attestatores/`, `4_perlector/`,
`5_recensor/`, `6_archetypus/`, `0_triage/`), `common/` (the shared foundation every stage
depends on), and every other `operations/` subdirectory (`review/`, `submit/`, `triage/`,
`serving/`, `bench/`, `corpus/`, `data/`, `seats/`, `spike_perlector/`), plus `config/` and
`.github/workflows/`. Ten parallel Sonnet auditors, one per area, each briefed with this
review's own established invariants (GOVERNANCE 2, hard rule 8's no-picker rule, the
foreign-record rationale F113/F123 already established) and told not to re-flag anything
already covered above. Every finding below was independently re-verified against the actual
code before acting — the same discipline this review has applied to every CodeRabbit finding
all along, now applied to the survey's own output.

**Fixed:**

- **F131** [major] — `pipeline/3_attestatores/run.py::_attempt_from_retained_testimonium`'s
  guard against handing a resumed live pass geometry the interrupted pass withheld used
  `content_health.recordable is True` as its test. `live_witness._content_health` sets
  `recordable: True` unconditionally on every parsed branch, including the parsed-but-
  unconfirmed-blank `failed` outcome (a response cut off, or with an unrecognized stop
  word) — exactly the branch `captured_page_attempt` deliberately withholds
  `observation_payload` for. The sibling function `_page_capture_from_record` had already
  closed the identical gap by gating on `record["outcome"] in WITNESS_READING_OUTCOMES`
  instead of parse state alone; this function still used the wider, incorrect test.
  Verified real by reverting the fix and confirming the new regression test failed
  (rehydrated bytes the interrupted pass never published) before re-applying it. Fixed the
  same way as the sibling. New regression:
  `test_a_resumed_parsed_but_unconfirmed_blank_act_carries_no_observation_payload`. An
  independent Opus review of the fix confirmed it correct on every point checked (handler
  ordering, every producer/outcome pairing, test fidelity, no missed call site) and caught
  two test-docstring slips the fix's own commit introduced or made stale — a "two tests"
  count that should have read "three" once a new one was inserted, and a claim that a
  branch reads `content_health.recordable` when it now reads `record["outcome"]` instead —
  both corrected in the same commit as the fix (`pipeline/3_attestatores/test_attestatores_
  live_pass.py`).

- **F132** [major, latent — confirmed reachable but not on any fixture this suite runs] —
  a Recensor review's identity (`attempt_id(act_id, "recense", used_total + 1)`) was a
  function only of the act's own recovery-request count, but `page_coverage_for` is
  deliberately page-wide by design (HANDOFF.md: "a flagged page holds every act that
  touches it... a successful recovery crop that reaches the missed ink clears the finding
  on the very next Recensor pass"). Two acts sharing a page, neither of which ever
  requests its own recovery, both publish at recense ordinal 1 on every Recensor pass
  (`pipeline/orchestrator/run.py::drive_recovery` re-invokes the Recensor over the whole
  run, unscoped, once per recovery round) — so if the page's residual-ink finding changes
  between passes for a reason unrelated to either act's own recovery, the second pass
  republishes different content under the same identity, and the immutable writer's
  `IncompatibleReuse` refusal kills the whole run. Reproduced directly (no monkeypatched
  internals): built a real tree through the Perlector, substituted `page_coverage_findings`
  to flag every page for one Recensor pass, then removed the substitute and ran the
  Recensor again — the second pass raised `IncompatibleReuse` exactly as predicted, before
  any fix landed. A first design (mint the ordinal from "how many reviews already exist
  for this act") was rejected by an independent Opus design review: it breaks a *different*
  existing invariant (`recovery_state` assumes a recovery-requested review's ordinal
  exactly equals its request's 1-indexed position) and fails the suite's own rerun-
  idempotency test. The landed design instead gives the review its own attempt model,
  mirroring this codebase's existing `StageContext.seal_boundary` pattern: a new
  `current_review` reader finds the act's current review; `publish_review` tries the prior
  review's own ordinal first (an unrelated page-wide fact usually leaves this act's content
  unchanged, so the store's own byte-for-byte reuse makes that a no-op) and mints a fresh
  ordinal only when the store proves the content actually differs, catching
  `IncompatibleReuse` and retrying at `prior + 1`. The recovery-request's own ordinal
  (`request_ordinal`, renamed from the reused `ordinal`) and the review's own ordinal are
  now independent numbers, carried separately in the review's payload
  (`recovery_request_ordinal` beside `attempt_ordinal`); `recovery_state`'s cross-check and
  `common/stage.py::current_recovery_request` (a second file with the identical coupling,
  found only by the design review, not by the first pass) both updated to read the request
  ordinal from its own field rather than assuming it equals the review's. New regression:
  `test_a_second_recensor_pass_that_clears_a_flag_does_not_collide_with_the_first`
  (`pipeline/5_recensor/test_residual_ink_wiring.py`), which reproduces the two-pass
  collision, proves the fix resolves it, and proves a third identical pass adds no new
  artifact (rerun idempotency). Four existing hand-seeded tests updated for the new payload
  field (`test_coverage_inputs.py`, `test_coverage_recovery_origin.py`,
  `test_recovery_absolute_cap.py`, `test_recovery_kind_distinction.py`). The full blast-
  radius run surfaced one further, expected consequence:
  `pipeline/orchestrator/test_orchestrator_acceptance.py`'s
  `test_repeating_the_review_scenario_also_changes_nothing` pins a digest of the entire run
  tree for the `review` scenario (`REVIEW_RUN_TREE_DIGEST`) and failed, because the new
  `recovery_request_ordinal` field is real, additional payload content on the one
  recovery-requested review that scenario produces — exactly the class of change this
  file's own established convention already documents pinning through once before (the
  comment above the constant). Verified directly rather than assumed: ran the scenario
  standalone, confirmed the file count is unchanged (111), confirmed by reading the actual
  review artifacts that exactly one field changed on exactly one file (only the recovery-
  requested review carries `recovery_request_ordinal`; the other two acts' reviews are
  unaffected), and confirmed a second orchestrator invocation over the same tree reproduces
  the identical snapshot and digest — the full-orchestrator proof of the same rerun-
  idempotency property the dedicated Recensor regression test proves in isolation. Pinned
  digest and its comment updated to record why. Full `pipeline/5_recensor/`,
  `pipeline/6_archetypus/`, `common/`, `pipeline/orchestrator/`, and `pipeline/2_designator/`
  suites green.

- **F133** [major, money/serving tier] — `operations/serving/README.md` claimed three
  preflight primitives "close hostile review item A's static-assertion gap... each is
  wired by the adapter/request-shape units whose files actually render a request." A
  repo-wide grep found every call to all three exists only inside their own unit tests;
  none is reachable from any production code path. Confirmed independently by a second
  design review before touching anything, because the first review's "wire all three"
  framing was itself wrong: only `assert_image_before_text_on_wire` is correctly wirable
  today. The other two would each become a tautology or a stale claim if wired as
  written — `assert_resized_pixels_within_trained_geometry` has no vendor-declared trained
  range carried anywhere in this tree to check against (only our own `--mm-processor-kwargs`
  values, which our own resize already clamps into by construction), and
  `assert_generation_config_key_coverage`'s one wirable chair (DAI) has serving rows now
  configured `generation_config = "auto"` while the sent/withheld reasoning in
  `pipeline/3_attestatores/feeding.py` was written against `"vllm"` — the two have drifted,
  and wiring the check now would stamp a stale account of the wire as a passing assertion.
  Fixed only what is real: moved `assert_image_before_text_on_wire` into `operations/
  serving/http.py` (forced by an import-cycle constraint stated backwards in this
  section's own first draft — corrected below — `preflight.py` imports `manager.py`,
  which imports `http.py`; neither `http.py` nor `manager.py` can import back from
  `preflight.py` without closing that cycle, and `http.py` already owns wire-shape
  rules); added a new narrow walker,
  `assert_wire_part_order`, that checks only `role=user` content lists that actually carry
  an image (skipping the readiness probe's bare string content, Churro's non-`user`
  system-turn preamble, and text-only user lists — all real, legitimate shapes the naive
  "every message's first part must be an image" version would have wrongly refused); wired
  it into `http.request_body`, the one seam every request this package renders already
  passes through, so a future call site cannot route around it. This is also the seam that
  exposed the actual bug the whole gap existed to catch:
  `AdapterCalibration.from_image_fixture` (the golden-page smoke and both adapter-
  calibration probes' payload builder) was itself text-first, image-second — the opposite
  of every production request builder in this repository, and the one request that decides
  whether a chair earns `proven`. Fixed to image-first, matching
  `pipeline/3_attestatores/HANDOFF.md`'s own record that this exact defect was already
  found and corrected in every other builder. Seven positional test indices updated
  (`operations/serving/test_manager.py`). README rewritten to say what is actually true —
  one primitive wired and how, the other two explicitly not wired and why, so the next
  reader does not have to grep to find out. Full `operations/serving/` suite (405 tests)
  green.

**Declined, verified not a real vulnerability:**

- **`operations/submit/submit.py`'s default private-refusal-report path** was flagged as
  bypassing `gate.require_approved_storage_location` (an explicit symlink/redirect check)
  for the one code path where the report path is derived from an already-validated
  manifest path via `.with_suffix(...)` rather than re-validated directly. Checked the
  actual write mechanism rather than trusting the gap's existence alone:
  `atomic_create` writes via `os.link`, which fails outright (`FileExistsError`) if
  anything — file, symlink, or directory — already exists at the destination; it can
  never silently write through a planted symlink the way `open(path, "wb")` could.  The
  fallback read path for an existing entry, `_existing_record_state`, is documented and
  built to use `O_NOFOLLOW` and treats any symlink there as "unknown, refuse" rather than
  following it. Both protections are independent of whether
  `require_approved_storage_location`'s own symlink check ran against this exact derived
  filename. Not a same-day-worthy fix; not touched.

**Leads (not independently adversarially verified beyond a single reading — recorded per
hard rule 7, not silently dropped, lower severity than the fixed findings above):**

- `pipeline/1_exemplar/run.py:784` — the Exemplar's render-contract verifier tests
  `"A" in source_bands` for alpha detection, but Pillow spells premultiplied alpha in
  lower case (`La`/`RGBa`); the same mistake already fixed in two sibling copies of this
  logic (`image_formats.py:1479`, `common/imaging.py:1230`), left unfixed in the verifier.
  Reachability (whether a real multi-frame container ever presents Pillow mode `La`/`RGBa`)
  not confirmed by execution.
- `pipeline/1_exemplar/image_formats.py:183` — the HEIC/AVIF brand sniffer builds a set
  from every 4-byte slice up to a length the submitted file's own first four bytes name,
  so a crafted 64 MiB file could drive ~16 million iterations before any decoder or bound
  runs; the existing guard test's payload happens not to exercise this (repeats one
  4-byte sequence, so the set never grows past two entries).
- `pipeline/2_designator/` — four findings, all minor: stale HANDOFF.md conservation-
  section prose, dead-but-undeclared code, one narrow duplicate-crop path, two small
  schema-closure gaps. No collision or silent-loss defect in the identity/accounting
  paths.
- `pipeline/3_attestatores/run.py:1949` — the evidence-boundary check accepts a re-sealed
  record claiming a reading (`read`/`genuinely-empty`) whose health shape says no response
  ever arrived, which the HANDOFF's own stated invariant says should never validate.
- `pipeline/3_attestatores/run.py:1850` — a resumed live pass reuses any act-scoped
  Testimonium sealed at its ordinal without checking it was written by a live pass (the
  exact check the page-chair path makes by name); latent, since both shipped serving
  catalogues use one posture per tier today.
- `pipeline/3_attestatores/feeding.py:591` — `dai_dimensions`'s docstring guarantee (a
  view within the sealed pixel ceiling) is not unconditionally true for a sufficiently
  extreme aspect ratio; no sealed page can currently produce one.
- `common/stage.py:1457` — the final Armarium boundary check rebuilds its inventory with
  lineage verification switched off, the one seal check in the pipeline that does not
  re-prove the stage's own claim.
- `common/chairs/receipts.py:102`, `common/chairs/model_store.py:1411`,
  `common/chairs/manifests.py:63` — a serving receipt accepts an all-zero "not yet
  measured" sentinel as a valid pin; manifest/record writes are not flushed despite a
  docstring claiming they reuse the run tree's own publication rule (which does flush);
  the digest-manifest reader has no size ceiling unlike its neighbors.
- `common/runtree/store.py:686,792`, `common/README.md:12` — two code comments citing a
  function's behavior or name that no longer matches reality; the module index lists nine
  entries against roughly forty production modules.
- `operations/submit/submit.py:461`, `operations/triage/recordgold_midpoint_pilot.py`
  (four findings: schema-label drift, a basename-collision crash, a duplicate-check
  keyed on the wrong identity, unguarded traceback on malformed input),
  `operations/triage/reconcile.py:506,479` — a refusal-count message that doesn't say
  the folder was empty; a publication path that overwrites without comparison unlike
  every sibling; a file descriptor leak on read failure.
- `operations/corpus/evaluate.py:599`, `compare.py:195,599`, `holdout.py:83` — run-wide
  counts copied into every page's own record; an unobviously-bounded Hungarian-matcher
  sentinel; an undocumented sort-order dependency for tie-break determinism; the
  strongest hold-out layer is unbuildable if one row's URL is refused.
- `operations/serving/http.py:461,256` — `_usage` silently discards the whole usage
  object if any one of three fields is malformed; `request_body` treats an explicit
  `null` model the same as absent and `temperature=False` the same as `0`.
- `operations/bench/README.md:3` — names cells B0/B0.5/B2–B6 but the sealed matrix also
  defines B5a.
- `config/build_model_fixtures.py` (destructive-cleanup finding) — **fixed separately as
  F134**, see below.
- `config/README.md` — stale in several places since `spend.toml` was configured on
  2026-09-06 (still describes it as unconfigured), an incomplete "sealed names today"
  list, a vendor name used where the project's own vocabulary requires a chair name, and
  two more minor drift points against `designator_grouping.toml` and the manifests
  directory's real contents.
- `config/hard_failure.toml:117` — a hard-failure kind's `reason` field is never checked
  against the Door's closed refusal-code list, so a mistyped code loads cleanly and
  counts nothing.
- `config/serving_recipes_real.toml:212` — every real serving row sets
  `trust_remote_code = true`, letting vendor Python execute on the rented pod; the file
  otherwise documents nearly everything else about what it configures.
- `pipeline/0_triage/manifest.py` — cites a planning document that does not exist in this
  repository; ~130 lines of XML machinery reachable only from its own test; a comment
  describing an older, now-wrong version of a canonical-encoding exception list.
- `operations/triage/scantailor_bridge.py:185` — stamps a fixed `revision: "v4"` (the
  project-file format version) into a field the contract defines as the ScanTailor
  version.
- `common/exemplar_boundary.py:406` — re-spells the triage colour-mode/actor-kind value
  lists as literals; the drift test that exists for exactly this pins field names, not
  these value sets.
- `pipeline/orchestrator/run.py:184` — `resolve_caller_paths` absolutizes only some of
  the caller-supplied paths the orchestrator forwards to subprocesses (`--fixture-root`
  and roughly a dozen `--*-config` flags are not among them), so a relative path there
  could name a different file depending on the subprocess's own working directory.

**Fixed, separately from the leads above:**

- **F134 — `proof/build_model_fixtures.py`** — the documented command (`python3 proof/
  build_model_fixtures.py`) called `shutil.rmtree` on the whole `config/manifests/`
  directory before rewriting it, but that directory is shared with the real serving
  roster's own digest manifests (`chandra-ocr-2.json`, `churro-3B.json`,
  `dai-recordgold-atr.json`, `qwen3.8-27B.json`) — confirmed by listing the directory
  directly, not merely by reading the code. Running the fixture generator as documented
  would silently delete real, checked-in production manifests. `config/model-fixtures/`
  (the sibling directory) is genuinely exclusive to this generator and stays a full wipe;
  `config/manifests/` cleanup narrowed to remove only the exact files this generator is
  about to rewrite (`f"{chair}.json"` for the five declared fixture chairs), leaving
  everything else untouched. The existing test asserting blanket removal of an
  arbitrarily-named stale file was split: `model_root`'s full-wipe behavior kept its own
  test, and a new test proves a non-fixture-named file in `manifest_root` (standing in for
  a real-roster manifest) survives a rebuild. `proof/test_proof_model_fixtures.py` (9
  tests) green.

Full `operations/serving/` (405), `pipeline/5_recensor/`, `pipeline/3_attestatores/`, and
`proof/test_proof_model_fixtures.py` suites green individually; a combined run of
`pipeline/`, `operations/`, `proof/`, and `.githooks/` together confirms no regression
across the wider tree.

### Independent panel review of F133 (`a0155a9`) — Opus and Fable, high effort, read-only

The serving tier's own review-proportionality table requires an independent panel, not one
reader, before the first push. Dispatched two independent reviewers, given only the
commit's diff and told nothing about each other's brief, and treated every finding the same
way this whole review has treated CodeRabbit's: re-verified against the actual code before
acting, nothing taken on the reviewer's word alone. Both converged on the same core defects
by different reasoning paths; Opus additionally traced every production call site of
`assert_wire_part_order` (including the Perlector, which neither the original brief nor
Fable's review named) and confirmed no real request is wrongly refused.

**Fixed:**

- **The new walker had zero test coverage.** `assert_wire_part_order` — the function the
  whole commit exists to add — was reachable from exactly one existing test, and that test
  called the older inner primitive directly on a hand-built list; it would have stayed
  green if the new walker had been deleted outright. For the money/serving tier this is
  precisely the failure this commit was written to repair (three functions that looked
  wired and were not). Added eleven tests to `operations/serving/test_http_reading.py`:
  seven direct against `assert_wire_part_order` (each of its skip cases — non-list
  messages, non-user role, string content, text-only content — plus the catch case with its
  label prefix, plus a tuple-typed variant proving the very next finding is actually fixed),
  and four integration-level against `request_body` itself (refuses a text-before-image
  user message with the model-id label; accepts image-before-text; accepts the readiness
  probe's bare string content; accepts a system preamble ahead of an image-first user turn).
- **A tuple-typed `content` or `messages` value walked past the guard unseen.** The walker's
  two `isinstance(x, list)` checks do not match a tuple, but `json.dumps` serializes a tuple
  onto the wire as a JSON array exactly like a list — so a caller handing over tuple-shaped
  parts would read image-first-or-not to a human but skip the check entirely.  Not
  hypothetical: `ChairRequest.messages` (`client.py`) is itself a tuple, frozen at
  construction, and survives today only because `client.py` wraps it in `list(...)` before
  calling `request_body` — remove that one wrap and the guard goes silent while requests
  keep flowing, the opposite of what its own docstring claims about a future seam not being
  able to route around it. Both `isinstance` checks in `assert_wire_part_order`
  (`operations/serving/http.py`) now accept `(list, tuple)`.
- **The import-cycle justification recorded above was stated backwards.** Corrected in
  place (see the F133 entry above, now reading `preflight.py` imports `manager.py`, which
  imports `http.py`) rather than left for a future reader to inherit the wrong arrow. The
  code decision itself — moving the function to `http.py` — was already right; only the
  recorded reasoning was inverted.
- **A stale comment.** `operations/serving/manager.py`'s `--chat-template-content-format`
  justification still named `preflight.assert_image_before_text_on_wire` after the function
  moved to `http.py` in the same commit; corrected to `http.assert_image_before_text_on_wire`.
- **The `preflight.py` re-export shim.** `assert_image_before_text_on_wire` was re-exported
  from `preflight.py` "for backward compatibility," but a repository-wide search found
  exactly one importer of that path — this package's own test file — and no production
  code. A private repository's own test importing from the wrong module is not backward
  compatibility; it is one file that never got updated. Decided under hard rule 13 (an
  ordinary engineering call, recorded here rather than parked): removed the re-export from
  `preflight.py` and repointed `test_manager.py`'s one import to `http.py`, the function's
  actual home.
- **The README's route-around claim overstated one door.** `request_reading`
  (`ServiceHandle`/`ServingManager`) POSTs an already-built body verbatim and is never
  itself checked; the README's "a future call site cannot route around it" was true only
  because `request_reading`'s sole caller, `ChairClient.read`, happens to always build
  through `request_body` first. Softened to name `request_reading` as the one door that
  must keep doing so, rather than implying the check applies unconditionally everywhere
  downstream of `request_body`.
- **The docstring did not name its own blind spot.** `assert_wire_part_order` skips every
  non-`user` role before it ever looks for an image, which is correct for every builder in
  this repository today but was not stated as a deliberate, bounded gap. Added a paragraph
  naming it: no production builder places an image outside a `role=user` turn, and
  `chat_image_bytes_all` separately refuses one on every path that calls it, but the
  readiness probe and a `requires_image=False` calibration never call
  `chat_image_bytes_all`, so a hypothetical future builder on one of those two paths would
  pass both checks unnoticed.
- **`pipeline/5_recensor/HANDOFF.md`** — F132's own follow-up, not this commit's: added a
  paragraph to the `kind="review"` section documenting that `attempt_ordinal` is now minted
  from the review's own content rather than counted from recovery requests, and that
  `recovery_request_ordinal` answers a different question (a recovery request's position,
  not the review's identity) and must not be read for the other.

`operations/serving/` (416 tests, up from 405) and `pipeline/5_recensor/` green. Both
reviewers' remaining points (the non-`user`-role gap, the route-around softening) were
resolved above rather than left as leads, because both sit inside this same review round's
own commit and this tier's table requires the panel's findings actually closed, not
deferred.

### Verifying the panel-review fixes — Fable, second pass

Before pushing, re-dispatched a fresh reader against the uncommitted fix diff itself
(not the original commit) to check each of the eight fixes above actually does what it
claims, independently of the reasoning that produced it — the same "verify the fix, not
just the finding" discipline this review has applied throughout. All eight passed. Five
smaller issues surfaced, one worth fixing before push:

- **The tuple fix stopped one function short.** `assert_wire_part_order`'s two
  `isinstance` checks were widened to accept a tuple, but `chat_image_bytes_all` and its
  private helper `_all_image_url_candidates` (`operations/serving/http.py`) still checked
  `list` alone. Not the same failure mode — a tuple-shaped image content list would pass
  the order check but then vanish from *both* `active_candidates` and `all_candidates`
  in `chat_image_bytes_all`, so their length-match guard would see zero and zero and
  raise nothing, silently returning no image bytes for a request that carried one. Same
  defect class the walker fix was written to close, one function over. Reproduced first —
  stashed the fix and confirmed the new test fails with `VLLM`-style
  `ServingConfigurationError: ... must contain a non-empty chat messages list` on a
  tuple-shaped payload — then widened all three remaining `isinstance` checks to
  `(list, tuple)` and re-ran to confirm green. One new test,
  `test_chat_image_bytes_all_finds_an_image_in_tuple_shaped_messages_and_content`.
- **This entry's own test count didn't add up.** "416 tests, up from 376" was wrong on
  both ends: recounting `operations/serving/` directly at `a5f7352` (immediately before
  F133) gives 405, not 376 — a0155a9 added no test functions itself, only reworded seven
  existing index assertions, so the two counts should have matched and didn't; the 376
  recorded above was simply a mistaken figure, not a different scope. Corrected both
  occurrences to 405, and this round's own count to 417 (405 + 11 in the panel-review
  fixes + 1 more from the tuple fix just above).
- **An inline comment beside the `request_body` call site** (`operations/serving/http.py`,
  directly above `assert_wire_part_order(value, ...)`) still read "so a future seam cannot
  route a rendered request around it" without the `request_reading` qualification the
  docstring and README now carry ten lines below. Reworded to "so a request rendered here
  cannot route around it," scoped to what the comment can actually see.
- **The original F133 entry corrects its import-cycle sentence in place but left its own
  "so a future call site cannot route around it" standing**, relying on this panel
  section to walk it back three hundred lines later. A reader stopping at the first entry
  would carry the overstated claim. Left as-is deliberately: the import-cycle sentence
  was a factual error (named the wrong module), corrected as one; the route-around
  sentence was accurate when a0155a9 landed and became an overstatement only once this
  panel round found the `request_reading` gap — that is what the panel section exists to
  record, not a second error to retract in place. Named here so a reader who stops at the
  F133 entry alone knows the panel section revises it.
- **A third unchecked door, informational.** `ServiceHandle.request` /
  `ServingManager.request` also renders through `request_body` without calling
  `chat_image_bytes_all`; production traffic is safe because its only caller,
  `request_fixture_image`, checks the image first, but the method is public and two tests
  call it directly. `assert_wire_part_order`'s docstring names "two paths" that skip
  `chat_image_bytes_all` as a statement about production reachability, not a claim to
  enumerate the whole public API surface — true as scoped, so left as written rather than
  reworded to a claim about surface area the docstring was never making.

`operations/serving/` (417 tests, up from 405) green after the tuple-in-`chat_image_bytes_all`
fix and its new test.

## Tenth pass — two of the Ninth pass's own recorded leads, independently fixed

The Ninth pass's leads list named these but did not fix them at the time (recorded per hard
rule 7, not silently dropped). Picked up as the next round of the standing find-and-fix work.
Both independently reviewed before push (Fable for F135, Opus for F136), each producing a
targeted, self-contained diff plus a regression test reproduced failing-then-passing against
the actual bug before landing — this review's standing discipline, not relaxed for a smaller
fix.

**F135** — `pipeline/1_exemplar/run.py::_verify_render_contract`'s Pillow-renderer branch
tested `"A" in source_bands` for alpha detection, the same case-sensitive mistake already
fixed in its two sibling copies (`image_formats.py`'s renderer, `common/imaging.py`'s crop
converter). Pillow spells premultiplied alpha in lower case (`La`/`RGBa`); a page the
renderer correctly converted to `LA`/`RGBA` would read as carrying no alpha at all and be
refused for "changing its mode conversion" — the verifier disagreeing with the very renderer
it exists to check. Fixed to mirror the already-shipped logic exactly: check
`{"La": "LA", "RGBa": "RGBA"}.get(source_mode)` first, fall back to the case-insensitive
band scan only on a miss. Two new tests in `pipeline/1_exemplar/test_render_config.py`
(hand-built contracts, since no currently-reachable production path decodes a file into
Pillow mode `La`/`RGBa` — confirmed by an independent reviewer, who additionally confirmed
this is a producer/verifier consistency fix, not a closure of an observed live refusal) prove
both directions: the correct conversion is accepted, a wrong one is still refused. Reproduced
first: reverting just this fix made both new "accepts" tests raise `ContractError` and the
"refuses" test raise nothing — the old logic wrongly refused the right answer and wrongly
accepted a specific wrong one in the same breath.

**F136** — `pipeline/1_exemplar/image_formats.py::_iso_bmff_image_format`, the HEIC/AVIF/HEIF
brand sniffer that `sniff()` calls on every submitted source file before any size or decoder
bound has run, built its brand set by walking `range(16, readable_end, 4)` where
`readable_end` was capped only by the file's own declared `ftyp` box size and the buffer's own
length — both attacker-controlled up to `MAX_SOURCE_BYTES` (64 MiB). A crafted ~64 MiB file
whose header claims a matching box size drives roughly sixteen million 4-byte slice-and-set-
insert iterations. An existing test already guarded the memory dimension, but by repeating one
4-byte value: every duplicate is hashed, found already present, and its transient object freed
immediately by CPython's refcounting, so peak memory stays flat regardless of how many
iterations actually ran — it does not, and was never exercised to, bound the iteration count
or the case of an attacker filling the file with unique 4-byte values, which would blow past
that same memory ceiling too. Fixed by capping the walk to a fixed ceiling
(`_FTYP_BRAND_SCAN_CEILING`, 256 compatible-brand slots past the 16-byte header) independent of
both the declared box size and the file's length — real `ftyp` boxes never carry remotely that
many brands. New test constructs a payload with unique filler and places a genuine `heic`
brand one slot past the ceiling with both the declared box size and the file's own length
claiming far more is readable; empirically confirmed against the pre-fix code (loaded
standalone, called directly) that the old logic actually returned `"heic"` for this exact
payload before fixing it, then confirmed the new code returns `None`.

`pipeline/1_exemplar/` (348 tests, up from 344) green.

**F137** — CodeRabbit's automatic re-review of `2f68441` found a gap in `request_body`
(`operations/serving/http.py`) that survived the panel-review round above: `assert_wire_part_order`
was checked against `value`, the still-mutable Python object about to be serialized, not against
what `_canonical_json` actually renders. Demonstrated, not asserted: a `dict` subclass whose
`get("type")` answers `"image_url"` while its *stored* value is `"text"` passes the check
(`.get` is all the walker reads) but serializes with `"text"` on the wire (`json.dumps` reads
the real stored value, never `.get`) — proved end to end against the actual current code, not
just the isolated `checked_type`/`wire_type` divergence CodeRabbit's own script showed: a real
call to `request_body` with such a part did not raise, and the returned bytes opened with text.
Fixed exactly as proposed: serialize once, decode that exact rendered snapshot with `json.loads`,
check the decode, return the same already-rendered bytes — closing the gap by re-reading what
was actually written rather than trusting the object that wrote it, which also makes the earlier
`(list, tuple)` `isinstance` widening (F133's panel-review round) no longer load-bearing for this
call site specifically (a JSON decode is always a plain `list`), though it stays correct and
necessary for `assert_wire_part_order`'s other, non-round-tripped callers -- the new direct unit
tests among them. New regression test reproduces the exact failure this finding demonstrated
(unpatched: does not raise; patched: raises with the model-id label), reproduced first against
the pre-fix code to confirm it actually fails the way the finding claims.

`operations/serving/` (418 tests, up from 417) green.

**Declined, verified reachable only in theory (F137's own independent review, Fable):**
`ChairClient.read` (`operations/serving/client.py:459-461`) digests images from
`request.messages` — the caller's own pre-render objects — and compares against the caller's
claimed `request.image_sha256s` *before* `request_body` renders anything, the same
`.get`-vs-serialized-value gap F137 just closed for `request_body` itself. Verified real in
principle (`_decode_image_url` reads a candidate's `"url"` key by both `.get` and `[]`
depending on the path), but every message builder in this repository constructs plain `dict`
literals; nothing carries a `dict` subclass into a `ChairRequest`. Not fixed here: the correct
fix (verify digests against `chat_image_bytes_all(json.loads(body))`, after `body =
request_body(...)`) reorders `read`'s own carefully-documented "refuse an unbuildable request
... before anything is built or sent" sequence — an invariant this exact PR already fought to
get right once (the retention-before-refusal fix recorded earlier in this document) — for a
gap with no live attacker-controlled path today. A theoretical, same-class defect worth a
future look if this call site's caller set ever grows past this repository's own trusted
builders; not worth reordering a hard-won control-flow invariant for today.

## Eleventh pass — another of the Ninth pass's own recorded leads

**F138** — `common/chairs/manifests.py::read_manifest` (the digest-manifest reader every
model verification goes through) read the whole manifest file into memory unconditionally
before checking anything about it — `data = source.read_bytes()`, no bound. The Ninth pass's
own lead named this as inconsistent with its sibling module: `model_store.py` already has an
established, hardened pattern for exactly this class of read (a small structured control
artifact, not model weight bytes) — `_read_limited_bytes`: `O_NOFOLLOW` refuses a
symlink-redirected read, an `fstat`+`S_ISREG` check refuses a FIFO or device *before any
blocking read*, and reading `limit + 1` bytes detects an oversized file without loading all
of it. `read_manifest` had none of the three. Fixed by adding the same pattern to
`manifests.py` itself as `_read_limited_manifest_bytes`, with `MAX_MANIFEST_BYTES =
16_777_216` matching `model_store.py`'s own `MAX_SHARD_INDEX_BYTES` — real shipped manifests
run from ~100 bytes to ~4 KB (`config/manifests/*.json`), so this ceiling is generous by
several orders of magnitude, not tight.

Reproduced, not just reasoned about: reverted the fix and ran the new FIFO test, which hung
the test process indefinitely on the old code's unbounded blocking read (killed after
confirming) — exactly the failure `model_store.py`'s own equivalent test is named to guard
against. Separately confirmed the symlink test fails without the fix: the old code silently
follows the symlink and returns the real target's manifest successfully, `DID NOT RAISE`.
Verified by reading `O_NOFOLLOW`'s actual semantics (not assumed) that a symlinked path and a
FIFO path fail at two different points — `os.open` itself raises before any `fstat` for a
symlink, while a FIFO opens successfully and is caught by the regular-file check — so the two
new tests match two different error messages on purpose, not by mistake.

Independent Opus review found the design and control flow correct, both `O_NOFOLLOW`/FIFO
error messages right, and the 16 MiB ceiling safely generous (real shipped manifests are
~100-byte single rows; the ceiling covers roughly 130,000 rows), and one real problem before
push: the FIFO test called `read_manifest` on the main thread with no deadline, so a
regression would hang the whole suite silently rather than fail — the exact failure mode this
session had to kill by hand while writing it, and precisely what this file's own established
`..._instead_of_hanging_the_boot` pattern (a daemon-thread worker joined with a 15-second
timeout) exists to prevent. Rewritten to use that pattern. Also fixed: the symlink test's
target was an empty manifest `_validate_manifest` rejects on its own, so the unfixed code
failed for the wrong reason (wrong error text, not "followed the symlink"); rebuilt against a
genuinely valid, digest-pinned one-row manifest so the only way to reach the assertion is
through the symlink protection. A docstring note now says why `_read_limited_manifest_bytes`
duplicates ~25 lines of `model_store.py`'s helper rather than importing it: `model_store.py`
already imports this module, so the reverse import would close a cycle — verified directly
against `model_store.py`'s own import block, not taken on the reviewer's word. The review also
named the symlink protection as defense-in-depth, not the closing of a live path: both current
production callers already resolve or refuse a symlinked manifest path before `read_manifest`
ever sees it (`registry.py`'s `.resolve()`, `model_store.py`'s own symlink-component refusal),
so stated here as such rather than implied otherwise. Two further unbounded, same-class reads
were found and named, not fixed in this round: `model_store.py:1217`'s `read_derived_inventory`
and `registry.py:627`'s `_verify_cache_descriptor`, both control-artifact reads with the same
theoretical FIFO-hang and unbounded-size exposure — recorded per hard rule 7, a lead for a
future pass, not silently dropped.

`common/chairs/` (327 tests, up from 324) and `proof/test_proof_model_fixtures.py` green.
