# The chamber is retired

**Tyrel's ruling, 2026-09-10.** The container seat — the autoclave at
`operations/autoclave/`, with its Dockerfile, launcher, fingerprint, token refresh,
container brief and rebuilder brief — is removed, and the `workbench/autoclave/` drawer
is no longer created or reported. A writing seat is a linked worktree on this machine
under the tool-call guard.

## Why

- The two jobs it was kept for after R4 (2026-09-05) no longer exist. The window onto the
  old pipeline closed on 2026-08-20, and a dependency this machine should not trust is a
  reason to ask him, not to build a seat.
- The container engine had been off for weeks and no container existed. Isolation nobody
  is spending is not a control, and stale machinery in the tree misleads the next reader —
  including a Codex or Claude session dispatching the other vendor for a bounded task.
- The guard already carries what the container once carried by accident: refusal 8
  refuses a push, pull request, ready-for-review or merge on the spawned-agent name, and
  refusal 7 refuses a governed-path write.

## What moved, and what stays

- `operations/autoclave/briefs/builder.md` → `operations/seats/builder.md`, with a README
  that states the seat boundary. `test_roster.py` binds the briefs there.
- The 2026-08-20 window ruling is recorded in `cleanroom/README.md`, which now carries
  its date; `CLAUDE.md` points there.
- The `history/2026-08-*` autoclave records stay as evidence of what ran, and comments in
  pipeline code that say a measurement was taken "in this chamber" stay as provenance.
- The launcher's pre-dispatch check of model/effort pairs is retired with it. Nothing now
  refuses an unreachable effort before a dispatch; the table in
  `.claude/agents/README.md` is a reference of what each CLI last accepted, and the
  dispatcher records what actually answered. If the check is wanted back it is a small
  separate unit, not an implication of prose.
- The existing local drawer on this machine, 1.4 GB of past dispatch bundles, was moved
  to `workbench/archive/2026-09-10_chamber-bundles/`, not deleted, so the evidence of
  what ran is kept where finished work is filed.
- The gate, `tidy.py`, the document allowlist, the installer, the static-shell list and
  the CodeRabbit path instructions lose their chamber branches; `operations/test_autoclave.py`
  goes with the launcher it tested.

Not carried: no old-pipeline mount survives anywhere, and no chamber-shaped path is kept
as a placeholder.
