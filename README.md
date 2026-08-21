# Apparatus Verbatus

Recovers the *ipsissima verba* — the very words themselves — from historical parish and
civil registers, using imperfect witnesses. Several vision models report on each act at
second hand; a trained reader, the **Perlector**, reads the ink itself and establishes
the text.

**Status:** alpha. Governance and architectural *direction* settled; the Spec 01 walking
skeleton is landed and is not yet proven, and agents build inside a sealed container
rather than on this machine. Implementation is discovered during alpha. **GitHub enforces
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

## Controls

Every local protection can be switched off, and how is written here — a guard the
owner cannot unwire is a defect, whatever it prevents (CLAUDE.md hard rule 11).

**The tool-call guard refuses six things for this session, and a seventh only for a
spawned agent.** Landing work on `main`, deleting recursively outside the drawers that
exist to be emptied, rewriting published history, deleting a remote ref, putting a
credential into git, and switching the git hooks off. The seventh is a spawned agent
editing a governed path — the one refusal that is not the same for both audiences, and
the reason built-in agent types can be used here at all. It cannot ask — a refusal is final within a session, and the way
past one is Tyrel. The predecessor asked 503 times in three days and approval became
reflexive, which is worse than no guard. **To switch it off, delete the `PreToolUse`
block from `.claude/settings.json`** — one step, no other file needs touching.

Still in force and not suspended: the git hooks refuse a commit on `main`, a push at
`main`, and a credential or oversized payload in outgoing history — `sh
.githooks/install.sh` arms them in a clone, and unsetting `core.hooksPath` removes
them. GitHub's own rules on `main`, listed above, are outside this repository's reach
and no local change affects them.

## Scope

Source images in, established readings out. Import to export.

Training, research, search, and correction happen elsewhere. They are not this project.

## The three that bind

1. **A missed act is worse than a poorly read act.** Nothing is lost silently.
2. **The Perlector reads; it never picks.** Witnesses are clues, never options.
3. **Quality over speed.** More passes and slower runs are acceptable costs.

**Tyrel decides.** He is the only human in these rules — no agent may stand in for him,
and no session may amend these documents.

## Who wrote this

**Every line of code here is AI-generated.** Tyrel directs the work, reviews it, and
decides what lands; he does not write the lines, and the repository does not pretend
otherwise.

The history records which machine did what, and separates writing from reading. Each
commit is authored by Tyrel, who is accountable for it, and carries a `Co-Authored-By`
line naming the model that wrote it. An agent that audited the work and found defects
without writing lines is recorded as `Reviewed-by` instead — so a commit can say it was
written by one model and adversarially read by two others, which is worth knowing.

A `commit-msg` hook refuses a commit that names no author, in any clone where the hooks
have been installed — CLAUDE.md says how, and until it is done the check does not run at
all. Even then it is an alarm rather than a lock: the messages git writes itself are
exempt, and it can be skipped deliberately. The merge commit GitHub creates when a pull
request lands is made on their servers, where no local hook runs, so that one is outside
its reach entirely.

Models are named by release, not by vendor alone, because "an AI wrote it" ages badly and
"Claude Opus 5 wrote it" does not.

## Two conventions

**History is evidence, never instructions.** Dated documents record what happened. They
do not tell you what to do. Only the files above do that.

**Status lives in one place.** The line under the title. If you find a status claim
anywhere else in this repository, it is wrong by construction.

**And it carries no date, by Tyrel's ruling.** This document states no date at all, and
`check-documents.sh` refuses one. A dated status line goes stale in silence: the line
above once named a day that had already passed the thing it described, in the very
document claiming to be the only place status lives. Undated, it can only be wrong about
the substance — and substance is what a reader notices. Dated state belongs in `history/`
and in the standing ledgers under `workbench/`, both of which are read as records rather
than as instructions. **Provenance is a different thing** and survives in the documents
that carry procedure — when a ruling was made, when something was measured — because the
attribution discipline in CLAUDE.md depends on it and a ruling's date never goes stale.

## Versions

**alpha** — a rebuild laboratory. Build the harness first; prove the workflow,
branches, rules and contracts. Old code was the reference, read through a window that
Tyrel closed once the rebuild could be planned from its own design notes — closed by
default, not sealed: one chamber can still be given it deliberately, and
`cleanroom/README.md` governs what may cross. Its systems are written new here, one piece
at a time. Alpha does not need to be a finished pipeline.

**Nothing enters this repository uninspected.** Code is written new, read line by line
and justified, or it does not arrive. An old byte crosses only where it is the best
option available and is named as carried; third-party code enters under a licence that
permits it, recorded with its source.

**beta** — start again in a fresh, clean private environment using only what survived
alpha. Build there until the system works.

**1.0** — the public release, with personal and community-specific material removed.

**Distribution rule.** A private repository is never made public by changing its
visibility. Either beta is public-safe from its first commit, or 1.0 is a separate
clean, allowlisted export with fresh history.
