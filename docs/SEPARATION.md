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
- **AMBIGUOUS** marks a path whose class is still to be decided. Each one is explained
  under [Open questions](#open-questions).

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
| `.coderabbit.yaml` | AMBIGUOUS | AI review app configuration |
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
| `operations/review/` | AMBIGUOUS | review-candidate receipts |
| `operations/serving/` | PRODUCT | |
| `operations/spike_perlector/` | AMBIGUOUS | Perlector spike |
| `operations/submit/` | PRODUCT | |
| `operations/test_http_deadline.py` | PRODUCT | |
| `operations/triage/` | PRODUCT | |
| `operations/triage/measured/` | AMBIGUOUS | results of one measured pass |
| `pipeline/` | PRODUCT | |
| `private/` | PRIVATE | |
| `proof/` | PRODUCT | synthetic fixtures and their tests |
| `pyproject.toml` | PRODUCT | |
| `requirements-dev.txt` | PRODUCT | |
| `scriptorium/` | PRIVATE | |
| `uv.lock` | PRODUCT | |
| `workbench/` | PRIVATE | the harness's notes; stays in alpha |

## Open questions

Each has a recommendation; the lead decides.

- **`.coderabbit.yaml`.** Is an AI review app part of beta's plain developer tools? Its
  instructions speak to the lead and the AI-written workflow. Recommendation: HARNESS.
  If beta wants the app, write a fresh, short configuration there.
- **`operations/review/`.** Is pinning a review to one commit a plain developer tool or
  part of the AI review workflow? It exists for the multi-reviewer process in
  `AGENTS.md`. Recommendation: HARNESS.
- **`operations/spike_perlector/`.** The spike is harness, but `operations/corpus/`
  imports its scoring, normalization and output status. Recommendation: before the copy,
  move those shared parts into product code (beside the corpus scoring), then leave the
  spike in alpha as HARNESS.
- **`operations/triage/measured/`.** Facts about one measured pass on seven real register
  frames, kept as a record; no code reads them. Recommendation: HISTORY, and leave it
  out of beta, since it describes real register pages.

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
