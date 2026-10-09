# Contributing

Thank you for looking. This project is in alpha and the design is still settling, so
please open an issue to discuss a change before sending a large pull request.

## Before you write code

Read [PRINCIPLES.md](PRINCIPLES.md) for what the project is aiming for, and
[ARCHITECTURE.md](ARCHITECTURE.md) and [GLOSSARY.md](GLOSSARY.md) for the stages and
their vocabulary. Above all: nothing picks among the witnesses, no act is lost
silently, and uncertainty is flagged rather than fabricated. ARCHITECTURE's invariants
are binding.

## How code is written here

- **Plain code a stranger can trust.** Small functions, clear names, built the simplest
  way that works. Nothing goes in that its author cannot explain line by line.
- **Lean comments that explain intent.** A comment tells a newcomer, or a careless model
  reading the file, what the code is for and what it works with. It never narrates
  history, cites a rule or a date, or excuses a workaround: if a workaround needs an
  excuse, fix the workaround. Reasons for a change go in the commit message.
- **Checks where they protect something.** Add a check or a test where it protects an
  act, the evidence, an export, private material, credentials or money. Housekeeping,
  such as a leftover temporary file, is made harmless and easy to clear rather than
  watched and reported.

## Setting up

Follow *Getting started* in [README.md](README.md). The hooks refuse a commit on `main`
and scan what you commit, and again what you push, for credentials, private paths and
oversized payloads. Before you push, run the tests near your change and the fast checks:

```sh
.venv/bin/python -m pytest -p xdist -n 2 <paths>
sh .githooks/check-static.sh
```

CI runs the full suite (`sh .githooks/check-all.sh`) on every pull request; it takes
about 20 minutes, so locally run only what your change touches.

## Making a change

1. Branch from `main` for each focused task.
2. Keep the change focused.
3. Open a pull request. CI must pass, and every review comment is either fixed or
   answered with a reason.
4. A change to README, PRINCIPLES, ARCHITECTURE, GLOSSARY, CONTRIBUTING, AGENTS,
   CLAUDE, GOVERNANCE, SECURITY, CODE_OF_CONDUCT or `docs/AI_CONTRIBUTORS.md`, to
   anything under `.github/`, or to what AI sessions are allowed to do (`.claude/`),
   needs the project lead's approval.

## How rules are written

Working rules live here for everyone and in AGENTS.md for AI sessions; CLAUDE.md only
points Claude Code to AGENTS.md. Keep them few and give each its reason; code comments never cite rules.

## Rules for what enters the repository

- **Third-party code** needs a licence that permits its use here, with the source and
  licence recorded beside it. Code adapted from elsewhere is named as adapted in the
  commit message.
- **No credentials, real register material or personal data.** Keys and secrets never
  enter git. Real page images, transcriptions and personal information stay out; if you
  find some already here, remove it. Tests use the synthetic fixtures in `proof/`.
- **AI-written commits** name the model that wrote them with a `Co-Authored-By:` trailer,
  and any reviewing model with `Reviewed-by:`.
