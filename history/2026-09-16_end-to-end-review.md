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
kept). Results are appended below once it completes.

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
