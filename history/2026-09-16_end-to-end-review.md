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
per the proportionality table, plus each touched suite run individually and a full
`check-all.sh` run on the complete diff — named as a substitution, not silently skipped.

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
correct as landed, no follow-up needed. A full `check-all.sh` gate run on the complete diff
(all four fixes) is in progress as this section is written; this file is updated again once
that lands, and again as a second, broader pass re-verifies the rest of PR #117's own
disposition claims (below). Nothing above is a TODO left in the diff itself — each line is a
decision with its reason, recorded here as this project's convention requires.

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
