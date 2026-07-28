# Everything this night changed — the tracking list

> Written 2026-07-27/28 at Tyrel's request: "make sure you know what is changed so we can
> track it later." Every change, in-repo and out, committed and not. Nothing was pushed and
> nothing was merged.
>
> This pushes `active/` past its budget of 6 files. Deliberate — it is a live document
> for the next conversation, and it gets archived the moment we have talked it through.

**Correction, 2026-07-28.** This file is a snapshot of one night and the repository has moved
since. Three claims below were true when written and are not now, so they are corrected in
place rather than left to mislead: the drawer's file count, the commit count, and `maxTurns`.
Counts that change hourly are given as the command that answers them, not as a number.

## Committed — three commits on `infra/workspace-readiness`

Nothing is pushed. These three were tonight's; the branch has grown since, and
`git log --oneline origin/main..HEAD` is the only trustworthy count.

### `9292273` — seat.sh announces where a writing seat actually ran
- `operations/codex/seat.sh` — prints the resolved workdir; strips the trailing slash
  `$TMPDIR` carries on macOS.
- `operations/codex/test_seat.py` — two new assertions.
- **Why**: a `TMPTRAY` seat's tray has a random name and was printed nowhere outside
  dry-run, so its drafts could not be found afterwards. **This fix paid off the same night**
  — it is how the writing seat's output was located and carried in.

### `555d9ff` — the roster's bounds, and what is off on purpose
- All six `.claude/agents/*.md` — added `maxTurns` and `disallowedTools`. No behaviour of
  an agent's *instructions* changed; only its limits. **`maxTurns` did not survive the
  night**: T19 removed it from all six files again, with the deadline stated in the prompt
  instead (`PUSH_PLAN.md`, row 2). The roster files carry no `maxTurns` today.
- `.claude/agents/README.md` — **new file.** Records that `memory` is off by your ruling
  and `isolation: worktree` is off pending your decision, so neither looks like an oversight.
- `.claude/skills/session-end/SKILL.md` — two fixes: hand edits no longer routed through
  `ALLOW_UNATTRIBUTED=1` (contradicted CLAUDE.md); "then stop" no longer sits above two
  mandatory steps.
- `.claude/skills/reviewer-pass/SKILL.md` — added your instruction that a reviewer of a
  replacement file must be shown the new file whole.

### `1001db7` — two fixes recorded as done that were not
- `.githooks/tidy.py` — memory-index parser now reads every link on a line, not just the
  first. Plus one pre-existing over-long line reformatted (unrelated, cosmetic).
- `.github/workflows/ci.yml` — the autoclave check is NUL-delimited end to end.

## Changed but NOT committed, and invisible to anyone reading the repository

- **`.claude/settings.local.json`** — gitignored, so this exists on this machine only.
  `Write(//Users/tyrel/Temp_Stage/**)` and `Edit(...)` moved from `allow` to `deny`.
  Your documents call Temp_Stage a read-only window; the permissions granted write across
  all of it. **Any other machine, clone, pod or sandbox needs this done again.**

## Workbench (local only when this was written — `active/` is tracked now)

**New notes for you to read:**
- `workbench/design/gpt_output_watcher.md` — the watcher, specified not built, with three
  open questions at the end.
- `workbench/design/dispatch_record.md` — the cross-vendor dispatch record the brief called
  the highest-value thing the night could produce. A design plus tonight's own register.
- `workbench/active/CHANGES_TONIGHT.md` — this file.

**Evidence drawers created** (`workbench/raw/`):
- `2026-07-27_worktree-sandbox/` — 5 probes, prompts, logs, and `FINDING.md`.
- `2026-07-27_night-reviews/` — 2 Sol reviews + `RUNPLAN_DEFECTS.md`, `DISPOSITION_VERIFY.md`.
- `2026-07-28_rebuild-plan/` — the ordered rebuild plan draft.
- `2026-07-28_gpt-experiments/` — 3 experiment seats, plus `tray_output/` holding the code
  a GPT seat wrote, carried in and hash-verified.

**Moved:** a stray `.DS_Store` from `active/` to `scratch/`. **Nothing was deleted, anywhere.**

**Edited:** `workbench/active/RUN_PLAN.md` — three edits only: two naming corrections
(`Codex (OpenAI)` → `GPT-5.6 Sol (OpenAI)`, `importer` → `rebuilder`) and a
**DO-NOT-FOLLOW banner on §5**. 57 further findings recorded but not applied.

## Outside the repository

- Two empty probe directories remain at `/Users/tyrel/Temp_Stage/.verbatus-probe-plain` and
  `.verbatus-probe-repo`. The session has no `rm` permission. **Yours to delete.**
- A git worktree was created for a probe and **removed** (`git worktree list` is clean).
- Nothing in `ocr_pipeline` was touched, read-only or otherwise modified.
- Temporary trays under `$TMPDIR` hold GPT drafts, already copied into `raw/`.

## What was deliberately NOT changed

- No canonical document — GOALS, GOVERNANCE, ARCHITECTURE, GLOSSARY, root README — was
  touched, as instructed.
- `seats.conf` unchanged. `seat.sh`'s in-repo guard unchanged.
- `isolation: worktree` not enabled.
- The watcher not built.
- 57 of 62 RUN_PLAN findings not applied.
- Nothing pushed, nothing merged, no branch deleted (`work/t`, `work/t2` still present).

## How to undo any of it

The three commits were the only tracked changes that night. `git revert <sha>` handles each
independently; they touch disjoint files. `workbench/` was gitignored throughout, so deleting
a folder under it reverted that part with no git involvement — **that is no longer true of
`workbench/active/`, which this branch now tracks (T17).** Deleting a note there is a tracked
deletion like any other. The permission change is two
lines in `.claude/settings.local.json` that can be moved back to `allow`.
