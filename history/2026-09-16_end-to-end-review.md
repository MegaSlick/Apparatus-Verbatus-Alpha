# 2026-09-16 — end-to-end review: in progress

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

Independent readers covered the orchestrator's recovery/hold control flow (the "rejected
re-proof holds its act" and "unread page held, not miscalled a cut-off" claims in commit
485283b, against GOVERNANCE 2/11), the Recensor and Archetypus test-file diffs, the base of
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

**Green.** `check-fast.sh` (ingress checks on the current worktree, `check-static.sh`, then
`pytest -m "not full or scanner"`) ran to completion against HEAD (`485283b`) with a pinned
uv 0.12.1: **9485 passed, 46 skipped, 2 xfailed, 0 failed**, in 36m51s. No ingress or static
check reported a problem before the test run started (the script's `set -eu` would have
stopped it there if one had). This does not stand in for `check-all.sh`'s full frozen-audit
gate (the `--full`/`scanner`-marked tests and the dependency-audit group are out of scope
here, per the coverage gaps above), but it means the everyday gate this project runs on
every commit is clean at HEAD.
