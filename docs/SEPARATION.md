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
  harness's own. Session handoffs, plans and run reports live in `workbench/`, not in git.
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
| `.claude/` | HARNESS | AI session skills and permissions |
| `.coderabbit.yaml` | HARNESS | AI review app configuration |
| `.gitattributes` | PRODUCT | |
| `.githooks/check-all.sh` | PRODUCT | full gate, run by CI |
| `.githooks/check-documents.sh` | PRODUCT | drop the separation check at the copy |
| `.githooks/check-fast.sh` | PRODUCT | |
| `.githooks/check-static.sh` | PRODUCT | drop `session_end_pod_check.sh` from its list |
| `.githooks/check_ingress.py` | PRODUCT | credential and path scan |
| `.githooks/commit-msg` | PRODUCT | |
| `.githooks/find-python.sh` | PRODUCT | the interpreter every hook runs its checks under |
| `.githooks/install.sh` | PRODUCT | stop creating the `workbench/` folders |
| `.githooks/pre-commit` | PRODUCT | |
| `.githooks/pre-merge-commit` | PRODUCT | |
| `.githooks/pre-push` | PRODUCT | |
| `.githooks/serving_audit.py` | PRODUCT | GPU serving inventory audit |
| `.githooks/test_ci_workflow.py` | PRODUCT | |
| `.githooks/test_hooks.py` | PRODUCT | |
| `.githooks/test_ingress.py` | PRODUCT | |
| `.githooks/test_serving_audit.py` | PRODUCT | |
| `.github/` | PRODUCT | CI, Dependabot, pull request template, code owners |
| `.gitignore` | PRODUCT | drop the harness lines |
| `.python-version` | PRODUCT | the interpreter uv picks on a Mac; one CI tests |
| `.graphifyignore` | HARNESS | |
| `AGENTS.md` | HARNESS | working rules for AI sessions |
| `ARCHITECTURE.md` | PRODUCT | |
| `CLAUDE.md` | HARNESS | |
| `CODE_OF_CONDUCT.md` | PRODUCT | |
| `CONTRIBUTING.md` | PRODUCT | rewritten as a public guide, without the harness rules |
| `GLOSSARY.md` | PRODUCT | |
| `GOVERNANCE.md` | PRODUCT | |
| `LICENSE` | PRODUCT | |
| `PRINCIPLES.md` | PRODUCT | |
| `README.md` | PRODUCT | |
| `SECURITY.md` | PRODUCT | private vulnerability reporting |
| `common/` | PRODUCT | |
| `config/` | PRODUCT | |
| `conftest.py` | PRODUCT | |
| `docs/AI_CONTRIBUTORS.md` | PRODUCT | guide for outside contributors who use AI |
| `docs/SEPARATION.md` | HARNESS | this page |
| `docs/design/` | PRODUCT | design of the operator control surface |
| `gold/` | PRODUCT | human gold sampling |
| `operations/README.md` | PRODUCT | |
| `operations/__init__.py` | PRODUCT | |
| `operations/bakeoff/` | HARNESS | witness bake-off bench |
| `operations/conftest.py` | PRODUCT | |
| `operations/corpus/` | PRODUCT | RecordGold evaluation |
| `operations/http_deadline.py` | PRODUCT | |
| `operations/maintenance/` | PRODUCT | watches the pinned vendor revisions |
| `operations/notify/` | PRODUCT | the operator and pod notices use it |
| `operations/operator/` | PRODUCT | |
| `operations/pod/` | PRODUCT | |
| `operations/pod/V2_MIGRATION.md` | HISTORY | |
| `operations/pod/session_end_pod_check.sh` | HARNESS | Claude Code session-end hook |
| `operations/replay/` | PRODUCT | re-run a saved run from its recorded model replies (no model calls) |
| `operations/serving/` | PRODUCT | |
| `operations/submit/` | PRODUCT | |
| `operations/test_http_deadline.py` | PRODUCT | |
| `operations/training/` | HARNESS | Perlector training-data exporter (images + messages JSONL, loss spans) |
| `operations/triage/` | PRODUCT | |
| `pagekit/` | PRODUCT | becomes its own Apache-2.0 repository at beta |
| `pagekit/cleanroom/` | HISTORY | the clean-room record; travels with pagekit as provenance |
| `pipeline/` | PRODUCT | |
| `private/` | PRIVATE | |
| `proof/` | PRODUCT | synthetic fixtures and their tests |
| `pyproject.toml` | PRODUCT | |
| `scriptorium/` | PRIVATE | |
| `uv.lock` | PRODUCT | |
| `workbench/` | PRIVATE | the harness's notes, session handoffs and run reports; stays in alpha |

## Decisions

- **`.coderabbit.yaml` is HARNESS.** Its instructions speak to the lead and to the
  AI-written workflow. If beta wants the app, it gets a fresh, short configuration.
The cleanup train has landed: the spike, the bench, the review-candidate code and the
code-size metrics tool are out of git (the metrics tool is kept in the local
`workbench/tools/`). When a later deletion lands, the check fails on the row that no
longer matches anything, and that row goes. The inventory is final only once it is taken
against the `main` that beta is copied from.

## Harness ties inside product files

These product files mention the harness, and are trimmed when beta is copied:
`.gitignore` (`.claude/`, `workbench/`, `graphify-out/`), `pyproject.toml` (test collection
settings naming `.claude/` and `workbench`), `config/data_handling_policy.json` (a
ledger under `workbench/standing/`), `.githooks/install.sh` (the `workbench/` folders),
`.githooks/check-static.sh` (the session-end hook), `.githooks/check_ingress.py`
(`workbench/` among the local-only areas), `CONTRIBUTING.md`, `README.md` and
`PRINCIPLES.md` (mentions of `AGENTS.md` or AI sessions).
