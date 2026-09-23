# Contributing

Thank you for looking. This project is in alpha and the design is still settling, so
please open an issue to discuss a change before sending a large pull request.

## Before you write code

Read [PRINCIPLES.md](PRINCIPLES.md). Every change is held to it, in particular:

- **Nothing picks.** No code may choose between witness readings.
- **Nothing is lost silently.** Partial results stay visibly partial.
- **Flag, never fabricate.** Uncertainty is recorded and passed on.
- **Plain code.** Small functions, clear names, and comments that explain *why*, not
  what changed or who asked for it.

[ARCHITECTURE.md](ARCHITECTURE.md) and [GLOSSARY.md](GLOSSARY.md) explain the stages and
their vocabulary.

## Setting up

Follow *Getting started* in [README.md](README.md). The hooks refuse a commit on `main`
and scan staged files for credentials and oversized payloads. Run the tests near your
change with `.venv/bin/python -m pytest <path>`; CI runs the full suite
(`.githooks/check-all.sh`, about 10,000 tests) on every pull request.

## Making a change

1. Branch from `main`: `work/<topic>`, `audit/<topic>` for review findings, or
   `infra/<topic>` for tooling.
2. Keep the change focused, and add or update tests alongside it.
3. Open a pull request. CI must pass, and every review comment is either fixed or
   answered with a reason.
4. A change to README, PRINCIPLES, ARCHITECTURE, GLOSSARY, CONTRIBUTING, AGENTS,
   CLAUDE.md or `.claude/` needs the project lead's approval.

## Rules for what enters the repository

- **Third-party code** needs a licence that permits its use here, with the source and
  licence recorded beside it. Code adapted from elsewhere is named as adapted in the
  commit message.
- **No credentials, real register material or personal data.** Keys and secrets never
  enter git. Real page images, transcriptions and personal information stay out; if you
  find some already here, remove it. Tests use the synthetic fixtures in `proof/`.
- **AI-written commits** name the model that wrote them with a `Co-Authored-By:` trailer,
  and any reviewing model with `Reviewed-by:`.
