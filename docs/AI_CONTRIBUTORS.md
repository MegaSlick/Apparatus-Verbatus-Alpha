# Rules for AI contributors

These rules apply to any AI agent that opens an issue or a pull request here, and to
the person who runs it. Everything in [CONTRIBUTING.md](../CONTRIBUTING.md) and
[GOVERNANCE.md](../GOVERNANCE.md) applies as well. At beta this file becomes the public
`AGENTS.md`.

## Say what you are

- State in the issue or pull request that it was written by an AI agent, and name the
  model and version that wrote it.
- Every AI-written commit carries a `Co-Authored-By:` trailer naming that model.
- A person who runs the agent is responsible for what it sends.

## Bring evidence

Every issue and pull request includes:

- the exact commit it was made against, from `git rev-parse HEAD`;
- the exact commands that were run;
- the full output, or a minimal reproduction that shows the problem;
- for a pull request, the tests that were run and the exit code of each.

A claim with no evidence is not acted on. Do not summarise output in place of showing
it, and do not say a test passed without its exit code.

## Keep it small

- One focused change per pull request.
- No edits outside that change: no drive-by renames, reformatting or comment rewrites.
- No generated churn, such as regenerated files or bulk formatting.
- No dependency or lockfile changes without an agreed issue first.
- No changes to the rules documents: README, PRINCIPLES, ARCHITECTURE, GLOSSARY,
  CONTRIBUTING, AGENTS, CLAUDE, GOVERNANCE, SECURITY, CODE_OF_CONDUCT, this file, or
  anything under `.github/` or `.claude/`.

## Never include

- Register material: page images, crops, transcriptions or anything read from a real
  register.
- Personal data of any kind.
- Credentials, keys or tokens, real or test ones, or the contents of an environment
  file.

Tests use the synthetic fixtures in `proof/`.

## Closed unread

The maintainer closes these without reading them:

- an issue or pull request with no evidence;
- mass edits across many files;
- refactors nobody asked for;
- anything that touches credentials, GPU pods, paid services or money without an agreed
  issue first.

Anything that includes register material, personal data or credentials is removed as
soon as it is seen.
