# The autoclave

A chamber gives an agent a writable clone, shell, and tests without giving it a path to
push or modify the host checkout. Work returns as a branch or report and is untrusted until
the main session reads it.

## Paths and limits

| Path | Purpose | Host writable |
|---|---|---|
| `/work` | chamber clone on `agent/<task>` | no |
| `/src` | host repository reference | read-only |
| `/out` | task output drawer | yes |
| `/specs` | frozen copy of `workbench/design/` | no original writes |
| `/window` | old pipeline reference, when configured | read-only |

`private/`, `workbench/`, and `scriptorium/` are masked from `/src`. Network egress is
open because agent CLIs and documentation need it. One vendor credential volume is mounted
read-write per chamber; it is shared with later chambers of that vendor. No Git credential
enters, so a chamber cannot push or open a pull request. `/out` has no quota and must be
treated as untrusted input on return.

The boundary protects the host checkout, not vendor quota, readable mounted data, network
egress, or the shared credential volume. Keep briefs bounded.

## Setup

```sh
brew install colima docker docker-buildx
colima start --cpu 6 --memory 12 --disk 60
sh operations/autoclave/autoclave.sh build
sh operations/autoclave/autoclave.sh login claude
sh operations/autoclave/autoclave.sh login codex
```

Login is interactive and Tyrel's. Credentials live in Docker volumes, not host credential
files. `doctor` asks each CLI whether its mounted sign-in works; it never treats a volume's
existence as proof. Before a Claude dispatch, the launcher refreshes an expired access token
inside that volume; the rotated refresh token is written back atomically and never reaches
the host.

## Commands

```sh
sh operations/autoclave/autoclave.sh doctor
sh operations/autoclave/autoclave.sh new <task> [base-sha] [claude|codex]
sh operations/autoclave/autoclave.sh dispatch <task> <vendor> <brief> <model> [effort]
sh operations/autoclave/autoclave.sh shell <task>
sh operations/autoclave/autoclave.sh exec <task> <command>
sh operations/autoclave/autoclave.sh list
sh operations/autoclave/autoclave.sh report <task>
sh operations/autoclave/autoclave.sh collect <task>
sh operations/autoclave/autoclave.sh rm <task>
```

`new` resolves the base to an exact commit. The vendor must already be signed in because a
credential mount cannot be added later. `dispatch` requires a model; effort defaults to
`medium` and is validated for that vendor/model. The brief travels through `/out/brief.md`,
not a process argument.

The agent commits above the exact requested base on `agent/<task>` and never rewrites
inherited commits. `collect` requires a clean chamber tree, validates the bundle and its
ancestry, fetches that branch into the host repository, and never merges it. Re-author or
squash only the new chamber commits so Tyrel is author and the actual model remains in
`Co-Authored-By` trailers.

`rm` refuses uncommitted or uncollected work. `rm <task> force` intentionally discards it;
the output drawer remains as evidence. Never use force merely to tidy a list.

## Agent instructions

`agent-brief.md` is baked at `/CLAUDE.md` and `/opt/autoclave/CLAUDE.md`; the launcher also
places it at `/work/AGENTS.md` and `/work/AUTOCLAVE.md`. Claude receives the same file as an
appended system prompt. This lets both supported CLIs see the chamber boundary while
`/work/CLAUDE.md` supplies repository rules.

The build labels the image with one SHA-256 fingerprint covering `.dockerignore`, the
Dockerfile, requirements, agent brief, refresh helper, and fingerprint implementation.
`new` refuses an image whose label differs from the current checkout. A chamber is pinned
to the instructions and code present when it was created; recreate it when either changes.

## Image

The image is Debian trixie on Node 22, with Python, git, pytest, Ruff, shellcheck, ripgrep,
a compiler, and both agent CLIs. The Docker build context denies everything and admits
only `requirements-dev.txt`, `agent-brief.md`, and the token refresh helper, keeping secret
drawers outside the build daemon.

The build requires Docker Buildx, uses BuildKit, and loads the result into the local engine.
It never falls back to the deprecated legacy builder that accumulated one dangling image
chain per rebuild. Both CLIs still install from their `latest` package tag, so a rebuild can
change agent behavior even when repository inputs did not. Record a rebuild and use
`doctor` afterward.

## Verification

`operations/test_autoclave.py` exercises launcher behavior against fake Docker and real
disposable Git repositories. It proves command, ref, and failure behavior; it does not
prove a real mount, credential refresh, image build, or agent run. Use the engine for those
checks when the change touches them.
