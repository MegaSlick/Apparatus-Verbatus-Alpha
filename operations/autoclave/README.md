# The autoclave — where agents work

A sealed chamber a writing agent works inside. It has a full shell, installs
what it needs, and runs its own tests. It cannot reach this machine, and it
cannot push. Work comes back as a branch, and the session reads every line
before anything enters the repository.

## Why

An agent that writes code needs a shell — to run the tests on what it just
wrote. An agent with a shell on this machine can change any file, and no amount
of pattern-matching on how a command is spelled closes that: for every spelling
refused there is another that works.

Both are true, so the answer is not to argue about capability but to move the
boundary. Inside the chamber the agent has everything. Outside it has nothing.
What crosses is a branch, and a branch is read before it lands.

## The shape

| Path | What it is | Writable |
|---|---|---|
| `/src` | this repository, mounted in | **no** — read-only mount |
| `/work` | the agent's own clone, on its own branch | yes, and it is the agent's |
| `/out` | scratch drawer shared with the host | yes — the only writable host path |

`/out` maps to `workbench/autoclave/<task>/`, which is gitignored, so nothing an
agent produces can appear in `git status` by accident.

The agent commits to `agent/<task>` inside its clone. `collect` turns that into
a git bundle, brings it through `/out`, and fetches it into this repository as a
local branch. Nothing merges. Nothing is pushed.

## Use

```sh
sh operations/autoclave/autoclave.sh doctor          # what is installed and running
sh operations/autoclave/autoclave.sh build           # build the image
sh operations/autoclave/autoclave.sh new my-task     # chamber on a branch from HEAD
sh operations/autoclave/autoclave.sh shell my-task   # look inside
sh operations/autoclave/autoclave.sh collect my-task # bring the branch back
sh operations/autoclave/autoclave.sh rm my-task      # destroy the chamber
```

`new` takes an optional second argument: the base commit. It is resolved to a
SHA on the host, so a chamber is pinned to an exact tree.

`rm` destroys the container and **keeps** the output drawer. The bundle is the
only surviving evidence that a dispatch happened.

## Requirements

A container engine. Colima is the recommendation: free, no GUI, starts in a few
seconds, exact resource limits, and lighter than Docker Desktop on this Intel
machine.

```sh
brew install colima docker
colima start --cpu 4 --memory 8 --disk 60
```

Colima and Dagger are not alternatives — they sit at different levels. Colima is
the engine that makes containers possible on macOS at all. Anything that drives
containers, including `dagger/container-use`, needs an engine underneath. If
container-use is adopted later it runs on top of this, not instead of it.

## The image

Debian trixie via `node:22-trixie-slim` — trixie because `pyproject.toml`
requires Python 3.12 or newer and Debian bookworm ships 3.11.

Carries git, Python 3.13 with `pytest` and `ruff` on the PATH, Node 22, a
compiler, and both agent CLIs — Claude Code and Codex. Both, because a task may
be given to either vendor and the image must not decide that.

Build context is the repository root, and `.dockerignore` there denies
everything and re-admits exactly two files. That form is deliberate: an
exclusion list has to be updated whenever a new drawer appears, and the day it
is forgotten `private/` — which holds the notification topic — travels to the
build daemon.

## Limits, stated rather than discovered

**Network egress is open.** The agent CLIs must reach their model provider, so
the container is not run with `--network none`. An agent inside can therefore
reach the internet. What it cannot reach is this machine: `/src` is a read-only
mount and no credentials are passed in.

**Model credentials are not wired up yet.** This is deliberate and it is the
next thing to settle. The options are an environment variable at `docker run`,
a named volume authenticated once, or a mount of the host's tool config — and
the third puts real credentials somewhere an agent can read them. Nothing is
mounted until that is decided.

**No push, by construction.** There are no git credentials in the image. An
agent cannot push and cannot open a pull request. The session does both, after
reading the diff.

**`autoclave` is in pytest's skip list.** `pyproject.toml` sets
`norecursedirs` to include `autoclave`, because the cleanroom tray holds
presumed-contaminated drafts and pytest imports what it collects. That pattern
matches any directory of that name, so a `test_*.py` placed in *this* directory
would be silently skipped. Tests for this tool belong outside it — put them at
`operations/test_autoclave.py`.

**The name is overloaded.** `autoclave/` at the repository root is the cleanroom
tray, and this is a container. `GLOSSARY.md` says one concept per word. In lab
terms this directory has the better claim — an autoclave is the chamber, and a
tray goes inside one — but renaming the tray touches governed documents and is
Tyrel's. Until he rules, both names stand and this note is the warning.

## What has not been proven

Nothing below has been run. Each is cheap to settle and each changes something
downstream if it fails.

1. Colima on this Intel host, and what it costs at idle.
2. Whether three or four chambers run at once without the machine labouring.
3. Claude Code running inside without prompting for something the chamber
   cannot give it — the failure that has cost two unattended nights.
4. Codex CLI running unattended inside a chamber.
5. Image build time and size.
6. The credential question above.
