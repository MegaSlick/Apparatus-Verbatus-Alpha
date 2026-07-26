# Apparatus Verbatus

Recovers the *ipsissima verba* — the very words themselves — from historical parish and
civil registers, using imperfect witnesses. Several vision models report on each act at
second hand; a trained reader, the **Perlector**, reads the ink itself and establishes
the text.

**Status — 2026-07-26:** alpha. Governance and architectural *direction* settled;
workspace not yet built. Implementation is discovered during alpha. **GitHub enforces
four things on `main`**, and only these four: a change arrives by pull request, the
automated checks must pass before it can be merged, `main` cannot be force-pushed or
deleted, and these apply to the owner as well. Everything else in this repository is a
local convention that a determined tool can step around. Note that no *approval* is
required, so anything holding the owner's credentials — including an agent — can merge a
passing pull request; "Tyrel merges" is a rule people follow, not one GitHub imposes.
This line is the only place status lives.

## Where to look

| If you want to know… | Read |
|---|---|
| what this project is for | [GOALS.md](GOALS.md) |
| what we are and aren't allowed to do | [GOVERNANCE.md](GOVERNANCE.md) |
| how the pipeline is shaped and why | [ARCHITECTURE.md](ARCHITECTURE.md) |
| what a word means | [GLOSSARY.md](GLOSSARY.md) |

## Scope

Source images in, established readings out. Import to export.

Training, research, search, and correction happen elsewhere. They are not this project.

## The three that bind

1. **A missed act is worse than a poorly read act.** Nothing is lost silently.
2. **The Perlector reads; it never picks.** Witnesses are clues, never options.
3. **Quality over speed.** More passes and slower runs are acceptable costs.

**Tyrel decides.** He is the only human in these rules — no agent may stand in for him,
and no session may amend these documents.

## Two conventions

**History is evidence, never instructions.** Dated documents record what happened. They
do not tell you what to do. Only the files above do that.

**Status lives in one place.** The line under the title. If you find a status claim
anywhere else in this repository, it is wrong by construction.

## Versions

**alpha** — a migration laboratory. Build the harness first; prove the workflow,
branches, rules and contracts. Import old code selectively, cleaning it as it enters.
Alpha does not need to be a finished pipeline.

**Nothing enters this repository uninspected.** Code arrives one piece at a time, read
line by line and justified, or it does not arrive.

**beta** — start again in a fresh, clean private environment using only what survived
alpha. Build there until the system works.

**1.0** — the public release, with personal and community-specific material removed.

**Distribution rule.** A private repository is never made public by changing its
visibility. Either beta is public-safe from its first commit, or 1.0 is a separate
clean, allowlisted export with fresh history.
