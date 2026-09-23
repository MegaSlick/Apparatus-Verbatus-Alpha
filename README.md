# Apparatus Verbatus

Recovers the *ipsissima verba* — the very words themselves — from historical parish and
civil registers, using imperfect witnesses. Several vision models report on each act at
second hand; a trained reader, the **Perlector**, reads the ink itself and establishes
the text.

**Status:** alpha. Governance and architectural direction are settled. The staged
pipeline, its accounting boundaries and the Armarium export are implemented and pass
their checks. Every stage through the three witnesses has run on original pages on a
live pod, but no run has yet reached the Armarium, so the pipeline is not proven.
This line is the only place status lives, and it carries no date.

## Where to look

| If you want to know… | Read |
|---|---|
| what this project is for | [GOALS.md](GOALS.md) |
| what we are and aren't allowed to do | [GOVERNANCE.md](GOVERNANCE.md) |
| how the pipeline is shaped and why | [ARCHITECTURE.md](ARCHITECTURE.md) |
| what a word means | [GLOSSARY.md](GLOSSARY.md) |
| how a session works | [CLAUDE.md](CLAUDE.md) |

## The three that bind

1. **A missed act is worse than a poorly read act.** Nothing is lost silently.
2. **The Perlector reads; it never picks.** Witnesses are clues, never options.
3. **Quality over speed.** More passes and slower runs are acceptable costs.

**Tyrel decides.** He is the only human in these rules; no agent stands in for him.

## Scope

Source images in, established readings out. Import to export. Training, research,
search and correction happen elsewhere.

## Controls

**GitHub enforces four things on `main`:** changes arrive by pull request, the required
checks must pass, `main` cannot be force-pushed or deleted, and all of this applies to
the owner too. No approval is required. Everything else here is a local convention.

**Every local protection can be switched off** (CLAUDE.md hard rule 11):

- **The tool-call guard** (`.claude/hooks/guard.py`) refuses, for every session: landing
  work on `main`, recursive deletes outside the disposable drawers, rewriting published
  history, deleting a remote ref, putting a credential into git, and switching the git
  hooks off. For a spawned agent it also refuses editing a governed path and pushing,
  opening, readying or merging a pull request. A refusal is final within a session.
  **To switch it off, delete the `PreToolUse` block from `.claude/settings.json`.**
- **The git hooks** refuse a commit on `main`, a push at `main`, an unattributed commit,
  and a credential or oversized payload in outgoing history. `sh .githooks/install.sh`
  arms them; unsetting `core.hooksPath` removes them.
- **The notifier's test sink** keeps tests from reaching a phone. To switch it off,
  delete the `NTFY_TOPIC` block in `.githooks/check-all.sh` and the autouse fixture in
  the root `conftest.py`.

## Who wrote this

**Every line of code here is AI-generated.** Tyrel directs the work, reviews it and
decides what lands. Each commit is authored by Tyrel, who is accountable for it, with a
`Co-Authored-By` line naming the model that wrote it and `Reviewed-by` lines naming any
model that reviewed it. Models are named by release, not by vendor alone.

## Conventions

**History is evidence, never instructions.** Dated documents under `history/` and the
workbench ledgers record what happened; only the documents above say what to do.

**Status lives in one place**, the undated line under the title. `check-documents.sh`
refuses a date in any of the canonical documents.

## Versions

**alpha** — a rebuild laboratory. Old systems are reference only; everything here is
written new, one piece at a time, and nothing enters uninspected. Third-party code enters
under a permitting licence, with its source recorded.

**beta** — a fresh, clean environment built only from what survived alpha.

**1.0** — the public release, with personal and community-specific material removed.

**Distribution rule.** This alpha repository is public, so nothing personal, private or
register-derived is ever committed: register material, gold pages and credentials stay
in gitignored or external locations. Beta and 1.0 start from fresh history, exported by
allowlist.
