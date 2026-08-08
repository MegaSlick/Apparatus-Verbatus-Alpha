# The autoclave — where agents work

A sealed chamber a writing agent works inside. It has a full shell, installs
what it needs, and runs its own tests. It cannot write this machine's repository
and it cannot push. Work comes back as a branch, and the session reads every
line before anything enters the repository. What it *can* touch on this machine
— `/out`, its vendor's credential volume, the refs the launcher writes — is
listed under Limits, and every one of them is untrusted until read.

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
| `/out` | scratch drawer shared with the host | yes — how work leaves |
| `/specs` | a frozen copy of `workbench/design/`, staged per chamber | yes — a copy, not the original |
| `/window` | the old repository, present only when `AUTOCLAVE_WINDOW` names one | **no** — read-only mount |

`/out` maps to `workbench/autoclave/<task>/`, which is gitignored, so nothing an
agent produces can appear in `git status` by accident.

The agent commits to `agent/<task>` inside its clone. `collect` turns that into
a git bundle, brings it through `/out`, and fetches it into this repository as a
local branch. Nothing merges. Nothing is pushed.

## Setting it up — three commands, once

```sh
brew install colima docker
colima start --cpu 6 --memory 12 --disk 60
sh operations/autoclave/autoclave.sh build
```

Then sign each vendor in, **once ever**:

```sh
sh operations/autoclave/autoclave.sh login claude
sh operations/autoclave/autoclave.sh login codex
```

That is interactive and it is Tyrel's to do — the exchange is between him and
the vendor and nothing about it is read, stored or logged by this tool.

It is once, not once per agent. The sign-in lands in a Docker named volume that
outlives every container and survives a Colima restart, and chambers mount it
read-write so the CLIs can refresh their own tokens. It only has to be redone if
the volume is deleted or a vendor forces a re-auth. Read-write and shared is a
real limit, not a detail — see Limits.

**Why not reuse the sign-in already on this Mac.** Claude Code keeps its
credentials in the macOS Keychain, which a Linux container cannot read, and
lifting the token out to inject it would mean handling a live credential in
plain text. Codex keeps a real file, but bind-mounting it would put the host
credential inside a chamber that has network egress. The chamber gets its own.

`doctor` asks each vendor's CLI whether it is signed in, inside a throwaway
container holding nothing but the volume. It keeps only the exit status, so
nothing the volume contains reaches its output — and it does not call a volume a
sign-in. `login` creates the volume before the sign-in runs, so an abandoned
attempt used to leave one behind that everything downstream read as a
credential. Where there is no engine or no image to ask with, `doctor` says
`unknown` rather than guessing in either direction.

## Running a task

```sh
sh operations/autoclave/autoclave.sh doctor            # state of everything
sh operations/autoclave/autoclave.sh new my-task       # chamber on a branch from HEAD
sh operations/autoclave/autoclave.sh new my-task <sha> # ...or from an exact commit
sh operations/autoclave/autoclave.sh shell my-task     # look inside
sh operations/autoclave/autoclave.sh exec my-task <cmd>  # run one command inside
sh operations/autoclave/autoclave.sh list              # every chamber
sh operations/autoclave/autoclave.sh collect my-task   # bring the branch back
sh operations/autoclave/autoclave.sh report my-task    # print what a reader left
sh operations/autoclave/autoclave.sh rm my-task        # destroy the chamber
```

A base given to `new` is resolved to a SHA on the host, so a chamber is pinned to
an exact tree rather than to whatever a branch name meant at clone time.

`collect` requires a clean tree inside — modified, staged **and untracked** —
because a bundle carries commits and anything loose beside them is left behind.
The refusal names the paths. A branch carrying no commits is not that failure:
it collects, so the report survives, and says `NO COMMITS` loudly. It verifies
that what is in the bundle slot is a bundle before git reads it, fetches the
branch, names any commit that came back with no `Co-Authored-By` trailer, and
prints the two commands to read it. **It never merges.**

**`collect` bundles `agent/<task>` and nothing else.** Work committed onto some
other branch inside a chamber does not come back — move or merge it onto
`agent/<task>` first. `rm` refuses a chamber holding any such commit, so it is
not silently destroyed, but nothing collects it for you.

`rm` reads the chamber before destroying it: uncommitted work, or a commit on
any branch that this repository does not have, is refused with the way out
named. "Does not have" means the object is absent here, not that the tips
differ — so re-authoring a collected branch, which is the next thing `collect`
tells you to do, does not lock the chamber. A stopped chamber cannot be read, so
it is refused too. `rm <task> force` is the word that says
the loss is intended, and it is also how to remove a chamber that will not
start. Either way the output drawer is **kept**, because the bundle is the only
surviving evidence that a dispatch happened.

## Dispatching an agent into one

```sh
sh operations/autoclave/autoclave.sh dispatch <task> <claude|codex> <brief> <model> [effort]
```

**The model is required.** Omitting it would run the vendor's default, and `codex
doctor` reports that default is `gpt-5.6-sol` — the most expensive seat OpenAI sells.
Effort defaults to `medium`, which is Tyrel's standing ruling. Both are validated
before Docker is touched, so a typo costs a line of output rather than a container.

The two vendors spell effort differently and neither spelling is guessable: `claude`
takes `--effort <level>`, while `codex exec` has no effort flag at all and needs the
config override `-c model_reasoning_effort=<level>`, exactly as
`operations/codex/seat.sh` has always done it. Both were checked against `--help`.

Which model a job wants is the table in
[.claude/agents/README.md](../../.claude/agents/README.md); the standing briefs are in
[briefs/](briefs/README.md), which also covers mass-spawning many small units at once.

The shape of a dispatch:

1. `new <task>` — the chamber, pinned to a base commit.
2. `dispatch <task> <vendor> <brief> <model>` — the brief travels as a file and
   becomes the CLI's standard input, so it is never an argument on either side of
   the boundary and never appears in the container's process list. Model and
   effort travel as environment variables. Effort is checked against the vendor
   and model, not against a vocabulary: `.claude/agents/README.md` measured which
   levels each seat can actually reach.
3. The agent works, runs its own tests, and commits to `agent/<task>`.
4. A reader writes `/out/report.md` instead of committing.
5. `collect <task>` — the branch arrives locally. Nothing is merged.
5a. **Re-author what came back.** A chamber commits as `autoclave
   <autoclave@localhost>`. `commit-msg` runs in there now, so each commit should
   already name the model that wrote it, and `collect` says so when one does not
   — but the *author* is still the container. Nothing merges until each commit
   names the model and belongs to Tyrel: squash, or re-author and keep the
   trailers.
6. The session reads every line of the diff, then decides.
7. `rm <task>`.

A branch collected from a chamber is merged locally on Tyrel's judgement. The
review ladder and the merge rules in `CLAUDE.md` govern what reaches GitHub;
they do not govern moving work out of a clone into the working repository.

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

**An agent in here may orchestrate, and should be told to when the job is large.** It
can spawn its own agents and fan out across its clone; `.claude/agents/README.md`'s
spawn-depth bound is about host agents and does not reach in here. That is what the
chamber is *for* — the expensive, wide, wreck-the-tree kind of work belongs where
wrecking the tree costs a rebuild. Hard rule 10 still holds inside: no agent of its may
edit a governed path.

**Network egress is open.** The agent CLIs must reach their model provider, so
the container is not run with `--network none`. An agent inside can therefore
reach the internet.

**`/src` cannot be written, and most of it can be read.** Read-only stops an
agent changing this machine and does nothing about it reading this machine, and
with egress open a readable secret is a sendable one. Three drawers are masked
with empty read-only tmpfs mounts — `private/`, which holds the notification
bearer topic; `workbench/`, which holds every handoff, note and reviewer
transcript, and which the guard's own `SECRET_DRAWERS` names as a place secrets
may live; and `scriptorium/`. Everything else in the repository is readable,
including `.claude/settings.local.json`. Say so rather than assuming otherwise.

**`workbench/design/` is the one documented exception to that mask,** and it is an
exception rather than an oversight: it arrives at `/specs` because the specs are the
thing a builder is asked to build from, and a chamber that cannot read its own spec
had every brief carrying one as prose. The whole subtree is copied, not only
`spec_NN_*.md` — so a note dropped in there reaches every chamber, and with egress
open, reaches a model provider. Nothing in it today is secret. Keep it that way:
anything dated, private or credential-bearing belongs in `workbench/active/` or
`workbench/standing/`, which stay masked.

**The masks cover the working tree, not `/src/.git`,** which the clone reads in
full. They hold because all three drawers are gitignored and nothing secret is
in reachable history; `pre-push`'s credential scan and the ingress check are
what keep that true, not the masks.

**`/out` is real, writable host state, and it has no quota.** The launcher opens
the brief and report slots once each, refusing to follow a link and refusing
anything that is not a regular file, so a chamber cannot redirect a host read or
write through them. That is not the same as a chamber being unable to fill the
disk with its own output drawer, which it can.

**One vendor's credential enters a chamber, or none.** `new <task> <base>
<vendor>` decides it, because a mount cannot be added to a running container.
`new` refuses a vendor that is not signed in rather than labelling a chamber for
a credential it never mounted, and `dispatch` refuses a vendor the chamber was
not built for and then asks the CLI *inside* the chamber whether that credential
still works. A chamber created
without a vendor — the default, and most of them — holds no credential at all
and is fine for running the suite or reading the tree. Before this, every
chamber received every sign-in that existed, read-write, so a Codex agent held
the Claude credential and the reverse.

**Claude keeps its configuration in two places and only one of them is a volume.**
The credential is inside the mounted directory and survives every container.
`.claude.json` sits *beside* that directory, outside the mount, and the CLI needs
both to refresh an OAuth token. For a while the launcher created a blank one in
every chamber, so the first refresh after a sign-in failed and the CLI blanked the
token fields in place — leaving a record whose refresh token was still weeks from
expiring and whose tokens were empty strings. It killed the sign-in twice, on
2026-08-05 and 2026-08-06, each time a few hours after a login that had visibly
worked, and both times it read like an expiry. The launcher now keeps that file in
the volume and copies it in and out around anything that runs the Claude CLI —
copied rather than symlinked, because the CLI writes it atomically and a rename
replaces a symlink with a regular file. Codex has never had this problem: its
whole configuration is already inside its own mount.

**That credential volume is shared between chambers and outlives them.** It is
mounted read-write — read-write is not optional, because both CLIs refresh their
own tokens — and every later chamber for the same vendor mounts the same volume. An agent has a
full shell and can read and rewrite every file in it, including anything the
next agent's CLI will read as instructions or hooks. So the isolation here is
against the *other* vendor's credential and against the host, not between one
chamber and the next, and this volume is the one thing `rm` does not destroy.
Closing that means giving each chamber a private copy, which is a change nobody
here can test: whether a token refreshed inside the copy invalidates the
original is a question about the vendor, and the local `auth status` check
cannot answer it. It is a stated limit until an engine and a real sign-in can
settle it.

**What comes back is the container's, not a person's.** The clone commits as
`autoclave <autoclave@localhost>`; `commit-msg` runs in there now, so a returned
commit should already name the model that wrote it, and `collect` says so when
one does not. The *author* is still the chamber, and re-authoring before merge
is step 5a above.

**No push, by construction.** There are no git credentials in the image. An
agent cannot push and cannot open a pull request. The session does both, after
reading the diff.

**Tests for this tool live at `operations/test_autoclave.py`.** They used to have
to: `pyproject.toml` listed `autoclave` in pytest's `norecursedirs` for the
cleanroom tray, which carried that name until 2026-08-01, and the pattern matched
a directory of that name anywhere. The tray is `cleanroom/` now and the skip entry
moved with it, so a test beside this script would be collected today. The file
stays where it is by convention, not by necessity. A recording stand-in for the
`docker` CLI drives the engine-facing branches against real disposable
repositories, so what argv reached the engine, what the chamber's shell was
handed, and which refs and files survived a failure are all outcome-tested. It
is not a daemon. **Nothing there says an image built, a mount behaved, a mask
held against an adversary, or that a real agent ran.** Those still need an
engine.

**What has and has not been measured**, with the numbers and the dates, is in
[history/2026-08-02_autoclave-measurements.md](../../history/2026-08-02_autoclave-measurements.md).
It is not repeated here: status lives in the root `README.md` and nowhere else, and the
numbers that used to sit in this file had gone stale where they stood.
