# Contributing

Thank you for looking. This project is in alpha and the design is still settling, so
please open an issue to discuss a change before sending a large pull request.

## Before you write code

Read [PRINCIPLES.md](PRINCIPLES.md) for what the project is aiming for, and
[ARCHITECTURE.md](ARCHITECTURE.md) and [GLOSSARY.md](GLOSSARY.md) for the stages and
their vocabulary. Above all: nothing picks among the witnesses, no entry is lost
silently, and uncertainty is flagged rather than fabricated.

## How code is written here

- **Plain code a stranger can trust.** Small functions, clear names, built the simplest
  way that works. Nothing goes in that its author cannot explain line by line.
- **Lean comments that explain intent.** A comment tells a newcomer, or a careless model
  reading the file, what the code is for and what it works with. It never narrates
  history, cites a rule or a date, or excuses a workaround: if a workaround needs an
  excuse, fix the workaround. Reasons for a change go in the commit message.
- **Checks where they protect something.** Add a check or a test where it protects an
  entry, the evidence, an export or money. Housekeeping, such as a leftover temporary
  file, is made harmless and easy to clear rather than watched and reported.

## Setting up

Follow *Getting started* in [README.md](README.md). The hooks refuse a commit on `main`
and scan staged files for credentials and oversized payloads. Run the tests near your
change with `.venv/bin/python -m pytest <path>`; CI runs the full suite
(`.githooks/check-all.sh`) on every pull request.

## Making a change

1. Branch from `main` for each focused task.
2. Keep the change focused.
3. Open a pull request. CI must pass, and every review comment is either fixed or
   answered with a reason.
4. A change to README, PRINCIPLES, ARCHITECTURE or GLOSSARY, or to what AI sessions are
   allowed to do (AGENTS.md, CLAUDE.md, `.claude/`), needs the project lead's approval.

## Rules for what enters the repository

- **Third-party code** needs a licence that permits its use here, with the source and
  licence recorded beside it. Code adapted from elsewhere is named as adapted in the
  commit message.
- **No credentials, real register material or personal data.** Keys and secrets never
  enter git. Real page images, transcriptions and personal information stay out; if you
  find some already here, remove it. Tests use the synthetic fixtures in `proof/`.
- **AI-written commits** name the model that wrote them with a `Co-Authored-By:` trailer,
  and any reviewing model with `Reviewed-by:`.
