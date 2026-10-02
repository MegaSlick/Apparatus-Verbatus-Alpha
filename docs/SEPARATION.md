# Separation inventory

Beta will be a fresh repository: a copy of a working alpha, with no history and no
development harness. This page says, for every tracked path, whether it goes to beta.
Nothing has moved yet; this page only classifies.

## The classes

- **PRODUCT** goes to beta: the pipeline, common code, configuration, the product's
  tests, the user-facing documents, and plain developer tools such as the Git hooks
  and CI.
- **HARNESS** stays in alpha: tooling for AI sessions and for building alpha, such as
  agent settings, session notes, spikes and benches.
- **HISTORY** stays in alpha: records of how alpha was built.
- **PRIVATE** is local and gitignored; only the folder's README is tracked. Beta keeps
  the folders the product uses (`private/`, `scriptorium/`); `workbench/` is the
  harness's own.
- **AMBIGUOUS** marks a path whose class is still to be decided, with the question
  under [Decisions](#decisions). None is open today.

## Inventory

A row names a top-level path, or a path inside a folder that mixes classes (such a
folder has no row of its own). A folder row ends in `/` and covers everything under it;
a more specific row inside it overrides it.

`.githooks/check-documents.sh` reads this table and fails when a tracked path is in no
row, or a row matches no tracked path. The table is the only list: a separate data file
would be a second copy for the doc to drift from, and the table is already plain enough
for a script to read.

| Path | Class | Note |
|---|---|---|
| `.claude/` | HARNESS | AI agent roles, skills and permissions |
| `.coderabbit.yaml` | HARNESS | AI review app configuration |
| `.gitattributes` | PRODUCT | |
| `.githooks/check-all.sh` | PRODUCT | full gate, run by CI |
| `.githooks/check-documents.sh` | PRODUCT | drop the separation check at the copy |
| `.githooks/check-fast.sh` | PRODUCT | |
| `.githooks/check-static.sh` | PRODUCT | drop `session_end_pod_check.sh` from its list |
| `.githooks/check_ingress.py` | PRODUCT | credential and path scan |
| `.githooks/commit-msg` | PRODUCT | |
| `.githooks/frozen_audit_requirements.py` | PRODUCT | |
| `.githooks/install.sh` | PRODUCT | stop creating the `workbench/` folders |
| `.githooks/pre-commit` | PRODUCT | |
| `.githooks/pre-merge-commit` | PRODUCT | |
| `.githooks/test_ci_workflow.py` | PRODUCT | |
| `.githooks/test_hooks.py` | PRODUCT | |
| `.githooks/test_ingress.py` | PRODUCT | |
| `.githooks/test_r0_contract_ci_matrix.py` | PRODUCT | |
| `.githooks/test_tidy.py` | HARNESS | |
| `.githooks/tidy.py` | HARNESS | reports on `workbench/` for sessions |
| `.github/` | PRODUCT | CI, Dependabot, pull request template |
| `.gitignore` | PRODUCT | drop the harness lines |
| `.graphifyignore` | HARNESS | |
| `AGENTS.md` | HARNESS | working rules for AI sessions |
| `ARCHITECTURE.md` | PRODUCT | |
| `CLAUDE.md` | HARNESS | |
| `CONTRIBUTING.md` | PRODUCT | rewritten as a public guide, without the harness rules |
| `GLOSSARY.md` | PRODUCT | |
| `LICENSE` | PRODUCT | |
| `PRINCIPLES.md` | PRODUCT | |
| `README.md` | PRODUCT | |
| `common/` | PRODUCT | |
| `config/` | PRODUCT | |
| `conftest.py` | PRODUCT | |
| `docs/SEPARATION.md` | HARNESS | this page |
| `gold/` | PRODUCT | human gold sampling |
| `operations/README.md` | PRODUCT | |
| `operations/__init__.py` | PRODUCT | |
| `operations/bench/` | HARNESS | bench tooling |
| `operations/conftest.py` | PRODUCT | |
| `operations/corpus/` | PRODUCT | RecordGold evaluation; imports from the spike |
| `operations/data/` | PRODUCT | |
| `operations/http_deadline.py` | PRODUCT | |
| `operations/maintenance/` | PRODUCT | watches the pinned vendor revisions |
| `operations/metrics/` | HARNESS | code-size counts for cleanups |
| `operations/metrics/baseline-2026-09.md` | HISTORY | |
| `operations/notify/` | PRODUCT | the operator and pod notices use it |
| `operations/operator/` | PRODUCT | |
| `operations/pod/` | PRODUCT | |
| `operations/pod/HANDOFF.md` | HISTORY | |
| `operations/pod/V2_MIGRATION.md` | HISTORY | |
| `operations/pod/session_end_pod_check.sh` | HARNESS | Claude Code session-end hook |
| `operations/review/` | HARNESS | review-candidate receipts |
| `operations/serving/` | PRODUCT | |
| `operations/spike_perlector/` | HARNESS | Perlector spike |
| `operations/submit/` | PRODUCT | |
| `operations/test_http_deadline.py` | PRODUCT | |
| `operations/triage/` | PRODUCT | |
| `operations/triage/measured/` | HISTORY | results of one measured pass |
| `pipeline/` | PRODUCT | |
| `private/` | PRIVATE | |
| `proof/` | PRODUCT | synthetic fixtures and their tests |
| `pyproject.toml` | PRODUCT | |
| `requirements-dev.txt` | PRODUCT | |
| `scriptorium/` | PRIVATE | |
| `uv.lock` | PRODUCT | |
| `workbench/` | PRIVATE | the harness's notes; stays in alpha |

## Decisions

- **`.coderabbit.yaml` is HARNESS.** Its instructions speak to the lead and to the
  AI-written workflow. If beta wants the app, it gets a fresh, short configuration.
- **`operations/review/` is HARNESS.** It pins a review to one commit for the
  multi-reviewer process in `AGENTS.md`, which beta does not carry.
- **`operations/spike_perlector/` is HARNESS.** `operations/corpus/` imports its scoring,
  so a copy taken today would have to move those parts into product code first.
- **`operations/triage/measured/` is HISTORY.** It records one measured pass on seven
  real register frames. No code reads it, and beta starts without it.

The cleanup train deletes several of these paths (the spike, the corpus, the bench and
the metrics in its PR 8; the review-candidate code in its PR 6). When a deletion lands,
the check fails on the row that no longer matches anything, and that row goes. The
inventory is final only once it is taken against the `main` that beta is copied from.

## Harness ties inside product files

These product files mention the harness, and are trimmed when beta is copied:
`.gitignore` (`.claude/`, `workbench/`, `graphify-out/`), `pyproject.toml` (test collection
settings naming `.claude/` and `workbench`), `config/data_handling_policy.json` (a
ledger under `workbench/standing/`), `.githooks/install.sh` (the `workbench/` folders),
`.githooks/check-static.sh` (the session-end hook), `CONTRIBUTING.md`, `README.md` and
`PRINCIPLES.md` (mentions of `AGENTS.md` or AI sessions), and the notification
`SessionStart` wording in `operations/README.md` and `operations/notify/README.md`.

## Future entries

- **Pagekit** becomes its own Apache-2.0 repository at beta. It is not on `main` yet
  (pull request #249); it gets a row here when it lands.
