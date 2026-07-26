# Working rules — Apparatus Verbatus (alpha)

Rules only. **No status, no dates, no pod IDs, no hashes.** If you find state in this
file, it is a bug — status lives in README.md and nowhere else.

Read [GOALS.md](GOALS.md), [GOVERNANCE.md](GOVERNANCE.md) and
[ARCHITECTURE.md](ARCHITECTURE.md) before proposing anything. They are short and they
are binding. [GLOSSARY.md](GLOSSARY.md) defines the vocabulary — use those words and no
synonyms.

## Quarantine

**Nothing enters this repository uninspected.** Alpha is a migration laboratory, not a
copy job. Code arrives one piece at a time, read line by line, understood, justified,
and checked against the goals and governance before it lands. Old fragments, dead
branches, historical codenames and bloat do not come with it.

If you cannot say what a line is for, it does not enter.

## Where notes go

**`workbench/` — gitignored, local only.** Session notes, handoffs, todo lists, grep
dumps, design scribbles, half-finished thinking. All of it. None of it in the repository.

- `workbench/active/` — work in play. Small enough to read in one sitting.
- `workbench/archive/<date>_<topic>/` — filed when the work finishes. Then, not later.
- `workbench/scratch/` — disposable. Anyone may delete anything here without asking.

The `pre-commit` hook refuses stray markdown, so a note cannot leak into the repository
by accident. A committed `.md` is a canonical document, a `README.md`, a `HANDOFF.md`,
or dated evidence under `history/` — nothing else.

**If it is dated or speculative it is not a document, it is a note.** The previous
repository accumulated 320 markdown files for want of this distinction.

## Branches

`main` is protected by local hooks — never commit or push to it directly. Work happens
on a branch and reaches `main` only by pull request.

- `work/<topic>` — normal changes
- `audit/<topic>` — a review that produces findings, not code
- `infra/<topic>` — risky structural work

One branch per task. Short-lived. Delete on merge. Two agents must never share a branch.

## Concurrency

More than one AI may be working here at once, and not all of them are Claude. Assume
another agent is editing files you cannot see.

- Work in your own git worktree, on your own branch.
- Never `git add -A` across the whole repo — stage only the files you touched.
- Never rebase, force-push, or amend a branch that is not yours.
- If a file changed under you, stop and re-read it rather than overwriting.

## Hard rules

These come from GOVERNANCE.md and are repeated because breaking them is expensive.

1. **Tyrel decides.** No agent stands in for him — not for pod permission, not for
   declaring something proven, not for approving an exclusion, and not for amending the
   canonical documents.
2. **No live pod without his explicit permission in that session.** Shutdown is
   verified, never assumed.
3. **The Perlector reads; nothing picks among witnesses.** A picker rebuilt under
   another name is still a picker.
4. **Nothing is lost silently.** Uncertain, held, or flagged is fine. Missing is not.
5. **Quality over speed.** More passes and slower runs are acceptable costs.

## Reporting

Say what you actually did. If a test failed, show it. If you skipped something, say so.
Do not report a task complete unless it is complete and verified.

Tyrel is not a programmer. Explain in plain language, give a recommendation rather than
a survey of options, and do not make him read code to make a decision.
