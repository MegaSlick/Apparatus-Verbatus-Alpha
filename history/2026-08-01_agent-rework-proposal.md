# The agent roster, reworked around the chamber

Proposed, not applied. `.claude/agents/` is a governed path, so the capability
changes in §3 wait for Tyrel. Everything in §5 is already built.

## 1. What changes, in one sentence

Capability stops being a property of an agent's name and becomes a property of
**where it runs**: on this machine, read-only; in a chamber, everything.

## 2. Why that is worth doing

Three roles today — `worker`, `infra-worker`, `rebuilder` — hold `Write`, `Edit`
and `Bash` on Tyrel's machine. That is the same reach the session has, granted to
something running unattended against a prompt nobody reads twice.

The framework redesign's answer was a permit: a file naming in advance every path
an agent may write, checked by the guard on every write, plus the existing ~1,800
lines that try to recognise a write by how the command is spelled. Three review
seats independently found that spelling approach cannot be finished.

The chamber makes the whole question moot. If no agent on this machine can write,
there is nothing to police here — and inside the chamber there is nothing worth
policing, because the chamber is the boundary.

## 3. The change to the roster — needs his approval

**Host roles become read-only without exception.** `scout`, `auditor`, `consult`,
`Explore`, `Plan` already are. `worker`, `infra-worker` and `rebuilder` lose
`Write`, `Edit` and `Bash` **as host roles**, because writing work no longer runs
here at all.

Two ways to express that, and the second is better:

1. Strip the three capability lines and leave the roles in place. Small diff.
   Leaves three role files that describe writing work but cannot do it, which is
   the kind of half-true document this project keeps tripping on.
2. **Retire the three as host roles.** Their briefs move to
   `operations/autoclave/briefs/<role>.md` and are passed to `dispatch`. The role
   file's job — model, effort, standing instructions — is exactly what a brief
   file is. *Recommended.*

Either way `.claude/agents/README.md` gains one sentence: a role that writes is
dispatched into a chamber, never spawned here.

## 4. Where a reader's output goes

Tyrel's requirement: a reader must leave something that does not fill the
session's context and outlives the agent.

- **A reader answering a question in the reply** — "where is X", "does Y exist" —
  runs on the host. Cheap, fast, nothing to collect.
- **A reader producing a report** — a review pass, an audit, a survey — runs in a
  chamber and writes `/out/report.md`. The session reads the file when it wants
  it, not automatically into context on return.

The line is what the output *is*, not what the role is called. A survey that
returns three paragraphs belongs in the reply; one that returns a hundred lines
belongs in a file.

This is already built: `report <task>` prints it and the drawer survives `rm`.

## 5. What is already built

- `new` — a chamber pinned to a base commit, on its own branch.
- `dispatch <task> <claude|codex> <brief-file>` — runs a vendor CLI inside
  against a written brief.
- `collect` — the branch comes back as `agent/<task>`. Nothing merges.
- `report` — what a reader left.
- `login` — one sign-in per vendor, ever.

## 6. What it costs, honestly

**Dispatching a writer is three steps, not one tool call.** `new`, `dispatch`,
`collect`. Slower to start, and it cannot be done inside a single tool
invocation the way spawning a subagent can.

**A chamber agent gets a written brief, not the session's context.** It cannot
see the conversation. Everything it needs must be in the brief. That is a real
constraint and also an improvement — the brief is a file Tyrel can read before
the work starts, which is what the redesign's permit was reaching for.

**Metering is unknown.** A chamber agent is a separate CLI process signed in
under Tyrel's own subscription. How that counts against his plan compared with a
subagent spawned by this session has not been measured, and should be watched
during the first real dispatch rather than assumed.

**Startup is a few seconds** — a clone plus a container. Negligible for an
hour's work, real for a ten-second lookup, which is why quick reads stay on the
host.

## 7. What this deletes from the redesign

If the roster change lands, the confinement step of the framework redesign
mostly evaporates:

- The permit as an advance path allowlist checked on every write — **not needed**.
  No host agent writes. What survives is a declared scope checked once against
  the returned diff, which is a few lines.
- The guard's ~1,800 lines of shell-spelling grammar — **still deleted**, and now
  for a stronger reason than the redesign gave. Not "subagents will have no
  shell", but "no agent runs here that could use one".
- The ten adversarial confinement fixtures — **reduced**. Most of them tested that
  a permitted agent could not escape its path list. With no host writer there is
  no path list to escape.

That is a larger deletion than the redesign proposed, reached by a simpler rule.

## 8. Unproven

Nothing in §5 has been driven by an actual agent, because neither vendor is
signed in yet and neither can reach a model without it. The first real dispatch
is the test, and the thing to watch is whether an agent inside the chamber
prompts for something the chamber cannot give it — the failure that has cost two
unattended nights.
