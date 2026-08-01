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

A brief names no vendor and no model — the same file is given to `claude` or to
`codex`, at whatever seat the job deserves. Which seat that is lives in the table in
[.claude/agents/README.md](../../../.claude/agents/README.md).

```sh
sh operations/autoclave/autoclave.sh dispatch check-exporter codex /tmp/brief.md gpt-5.6-luna low
```

**The model is required and the effort defaults to `medium`.** Required because
omitting it runs the vendor's own default, and `codex doctor` reports that default is
`gpt-5.6-sol` — the most expensive seat OpenAI sells. Left optional, every chamber
would quietly have been a flagship chamber and the tier table would have described a
choice nothing ever made.

Known-good model names, checked against Codex's own model list rather than guessed:
`gpt-5.6-sol`, `gpt-5.6-terra`, `gpt-5.6-luna`, `gpt-5.3-codex-spark`. For a Claude
chamber the aliases `opus`, `sonnet`, `haiku` and `fable` work. Effort is one of
`none`, `minimal`, `low`, `medium`, `high`, `xhigh`, `max`.

Both are validated before anything is started, so a typo costs a line of output rather
than a container.

## Mass-spawning bounded units

A chamber is a container, and several run at once — three finished a full test suite
in seventeen seconds of wall clock on a 2019 laptop. That makes a particular shape of
work cheap: **many small, self-contained units, each fully described in its own brief**
— build this one thing, run this check, confirm it works, stop.

That shape is what a volume seat is for. Give each unit its own task name, its own
chamber and its own brief, and let them run together:

```sh
for unit in exporter-guard seal-count page-order; do
  sh operations/autoclave/autoclave.sh new "$unit"
  cat operations/autoclave/briefs/builder.md "tasks/$unit.md" > "/tmp/$unit.md"
  sh operations/autoclave/autoclave.sh dispatch "$unit" codex "/tmp/$unit.md" gpt-5.6-luna low &
done
wait
```

**The unit has to genuinely fit.** A volume seat is chosen because each brief is short
and self-contained, and the moment a unit needs to hold a whole subsystem in mind it
belongs on Terra, Sol or Opus instead — see the long-context note in the roster. Split
the work until each piece fits the seat, or use a bigger seat. Do not give a small seat
a big job and hope.

Each chamber is collected and read separately. Nothing merges on its own.

The standing brief goes first and the task second. The task names the objective, the
deliverable, the checks that must pass, and the stop conditions — the standing brief
never knows those and must not pretend to.

## Model and effort

Set them on the dispatch, not in the brief: a CLI in a container takes them as flags,
and a value written into prose is a value nothing enforces. **Medium is the default**
(Tyrel, 2026-08-01); high and above are for planning and judging, not for building from
a written spec. The reasoning is in [.claude/agents/README.md](../../../.claude/agents/README.md),
which remains the roster for the read-only roles that still run on the host.
