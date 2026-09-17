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

_(filled in as the review completes)_

## Gate result

**Green.** `check-fast.sh` (ingress checks on the current worktree, `check-static.sh`, then
`pytest -m "not full or scanner"`) ran to completion against HEAD (`485283b`) with a pinned
uv 0.12.1: **9485 passed, 46 skipped, 2 xfailed, 0 failed**, in 36m51s. No ingress or static
check reported a problem before the test run started (the script's `set -eu` would have
stopped it there if one had). This does not stand in for `check-all.sh`'s full frozen-audit
gate (the `--full`/`scanner`-marked tests and the dependency-audit group are out of scope
here, per the coverage gaps above), but it means the everyday gate this project runs on
every commit is clean at HEAD.
