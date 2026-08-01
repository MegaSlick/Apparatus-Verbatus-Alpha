# The agent environment — containers as the working bench

Tyrel's direction, 2026-08-01: agents get an isolated environment with full capability, work
freely, and hand back a change set that is inspected before it becomes a pull request. More
than one at a time, on separate branches. Both Claude and Codex must run in it.

This plan is not built. It needs a reader before anything is built from it.

## The machine

- Intel i9-9980HK, 16 cores, **32 GB RAM**, 1.3 TB free.
- No container runtime installed. Clean start.
- Intel means a Linux VM under any container runtime, but no ARM/x86 translation cost.
- 32 GB comfortably holds a VM at 16 GB plus three or four agent containers.

## The shape

Two classes of agent, and the split is the whole design.

**Readers** — auditor, scout, consult. Run on the host. Read, grep, glob. No write tools,
no shell. They never change anything, so they need no container and cost nothing to start.

**Writers** — worker, infra-worker, rebuilder. Run only inside a container. Full shell,
full capability, their own git branch, their own clone. They can install what they need and
run their own tests. Nothing they do touches the host.

**The rule that makes it exact: no writing agent ever runs on the host.** Not "should not" —
cannot, because no host-side role is granted a write tool.

## Why this is better than the fence the design drafted

The redesign's answer was a permit: a file naming, in advance, the exact paths an agent may
write, checked by the guard on every write. It works, and it costs an agent the ability to
test its own code.

The container answer is better on both counts:

- **The 1,800 lines of shell-spelling guesswork still get deleted**, because no host-side
  agent has a shell *or* a write tool. The guard is not guessing at command spellings; there
  is nothing on the host for it to guess about.
- **The writer keeps full capability.** It runs its own tests before handing anything back,
  which the permit design explicitly gave up.
- **Inspection moves to the boundary, where it belongs.** What comes back is a branch. A
  branch is inspected by reading its diff — which is the sterilizing review this project
  already does for the autoclave. The discipline exists; this gives it a wall.

The permit is not discarded. It shrinks to one job: naming which paths the returned branch is
allowed to have touched, checked against the diff at the boundary. That is a few lines, after
the fact, against a change set that actually exists — rather than a guess made in advance.

## Route

**Build it rather than adopt a tool.** `dagger/container-use` implements almost exactly this
— an MCP server giving each agent its own container and its own git branch. It is worth
reading. It is also badged **Experimental** and "in early development and actively evolving",
it names Claude Code and Cursor but not Codex, and this project cannot carry a load-bearing
dependency on machinery it does not control (hard rule 11 — anything mechanical is removable
in one documented step).

What we build is small:

1. **A container runtime.** Colima, not Docker Desktop — free, no GUI, starts in under five
   seconds, exact resource limits, and lighter on an Intel host. Docker Desktop's microVM
   sandboxes give a hypervisor boundary we do not need: the threat is an agent wandering out
   of its task, not hostile code.
2. **One image.** Debian base, Node, Python, git, the repository's toolchain, Claude Code and
   Codex CLI both installed and pinned. Built once, rebuilt when a pin moves.
3. **A launch script in `operations/`.** Takes a task name, a base commit and a role. Starts
   a container, clones the repository into it at that commit — **a clone, not a mount**, so
   nothing on the host filesystem is writable from inside — cuts the branch, runs the agent.
4. **A return path.** The agent commits to its branch inside the container. The script fetches
   that branch into the host repository as `agent/<task>`. Nothing is merged; the session
   reads the diff and decides.
5. **A boundary check.** Compare the returned diff against the task's declared paths. Anything
   outside is reported, not silently accepted.

Steps 3 and 4 are the whole mechanism, and they are ordinary git: a container can be a remote,
or the work returns as a bundle.

## Both vendors

Codex CLI reads MCP and its own config from `~/.codex/config.toml`; Claude Code reads
`.claude/`. Both install into the same image and both run against a clone. The image is the
uniform environment; each tool keeps its own configuration inside it.

This is the reason to build a container rather than use the operating-system sandboxes both
tools already ship. Claude Code has a Seatbelt-based sandbox and a whole-process sandbox
runtime; Codex has its own OS-level sandbox with network off and writes limited to the
workspace. Both are real, and both are *different mechanisms with different configuration*.
One environment that both vendors run inside is worth more than two sandboxes that have to be
kept in agreement.

## Unproven — nothing below has been run

1. Colima on this Intel host, and what it costs at idle.
2. Whether three or four agent containers run concurrently without the machine labouring.
3. Codex CLI running unattended inside a container against a clone.
4. Claude Code running inside a container without prompting on something the container cannot
   satisfy — the failure that has now cost two nights.
5. Image build time and size, which decides whether this is pleasant or a tax.
6. Whether the returned-branch path is as simple as it looks once credentials are involved.
   The container needs model API access and must **not** hold git push credentials — the
   session pushes, agents never do.

## What it changes in the decisions already recorded

- **O1 is answered differently than the corpus framed it.** The question was "does a writing
  agent lose the shell". Under this plan: on the host, yes, entirely — it loses every write
  tool as well. In the container, no — it has full rein. Both halves are stricter and more
  useful than the drafted permit.
- **S3 stands but shrinks.** The permit stops being an advance path allowlist checked on every
  write, and becomes a declared scope checked once against the returned diff.
- **The guard shrinks further than the plan projected.** With no host-side writing agent, the
  subagent write path has almost nothing left to police.

## Sequencing

This does not block the three pull requests already planned, and it should not be folded into
them. It is its own line of work with its own branch. The one interaction: PR 2 deletes the
guard's shell grammar, and that deletion is *more* clearly correct under this plan, not less.

Recommended: prove steps 1 and 2 first, with one throwaway task, before writing any of the
script. If Colima and the image are unpleasant on this hardware, everything downstream
changes and it is cheap to find out now.
