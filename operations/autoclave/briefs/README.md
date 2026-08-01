# Standing briefs for dispatched agents

A brief is what a chamber agent is told about **its job**. It is not told about the
chamber here — the image already carries that, as `/CLAUDE.md`, from
[../agent-brief.md](../agent-brief.md). Three documents reach a dispatched agent, and
they do not overlap:

| Document | Where it comes from | What it says |
|---|---|---|
| `/CLAUDE.md` | baked into the image | where you are, what you cannot do, how work leaves |
| `/work/CLAUDE.md` and the governed documents | the clone | what binds the code you write |
| the brief you are dispatched with | this directory, plus the task | what you are for |

These replaced `.claude/agents/worker.md`, `infra-worker.md` and `rebuilder.md`. Those
were host roles holding `Write`, `Edit` and `Bash` on Tyrel's machine — the same reach
the session has, granted to something running unattended. No agent writes on the host
now, so the roles moved to where the writing happens.

**Three became two.** `worker` and `infra-worker` were one job at two stakes, and
`builder` carries the infrastructure rules always: test-first, fail closed, no shortcut
through the gate you are editing. Those are good rules for ordinary code as well, and a
brief that only *sometimes* demands them teaches an agent to decide for itself which day
it is. What separated the two roles is now what it always really was — which model, at
what effort, chosen on the dispatch. `rebuilder` stays its own brief because it is a
different method rather than a different stake: never copying a byte is the whole job.

## Dispatching one

A brief is passed as a file, so it is readable before the work starts rather than
reconstructable afterwards from a transcript:

```sh
sh operations/autoclave/autoclave.sh new refactor-designator
cat operations/autoclave/briefs/builder.md task.md > /tmp/brief.md
sh operations/autoclave/autoclave.sh dispatch refactor-designator claude /tmp/brief.md
sh operations/autoclave/autoclave.sh collect refactor-designator
```

The vendor is the third argument, and a brief never names one — the same file is given
to `claude` or to `codex`. Which vendor and which model the job wants is the table in
[.claude/agents/README.md](../../../.claude/agents/README.md).

The standing brief goes first and the task second. The task names the objective, the
deliverable, the checks that must pass, and the stop conditions — the standing brief
never knows those and must not pretend to.

## Model and effort

Set them on the dispatch, not in the brief: a CLI in a container takes them as flags,
and a value written into prose is a value nothing enforces. **Medium is the default**
(Tyrel, 2026-08-01); high and above are for planning and judging, not for building from
a written spec. The reasoning is in [.claude/agents/README.md](../../../.claude/agents/README.md),
which remains the roster for the read-only roles that still run on the host.
