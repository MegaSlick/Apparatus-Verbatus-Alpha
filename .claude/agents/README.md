# Agents — read this before spawning one

Agent use is encouraged here. It is the normal shape of work past a few turns, not an
escalation, and small use needs no ceremony. What follows is the whole of what a session
needs to choose one well: the two kinds, the roles, what each seat actually costs
measured on this machine, and the bounds.

**Do not choose a model or an effort from memory.** The table below was measured; your
recollection was not.

## The two kinds, and the only line that matters

Capability is a property of **where** an agent runs, not of what it is called.

**A container is where an agent works, and it is the default** (Tyrel, 2026-08-02).
Anything whose subject is this repository goes into one: building, rebuilding, auditing
a tree, reviewing a branch. It gets its own clone, its own branch, a full shell and its
own tests. `/src` is a read-only mount with `private/`, `workbench/` and `scriptorium/`
masked; no pushing credential goes in; at most one vendor's model credential does. Work
returns as a branch and is read before anything lands. How to build, sign in, dispatch
and collect: [operations/autoclave/README.md](../../operations/autoclave/README.md).
The standing briefs are in `operations/autoclave/briefs/`.

**It is also the more capable seat, which is why it is the default rather than the
cautious choice.** A chamber agent has git, so it can read commit messages, diff two
revisions and run the suite. A host reader cannot: on 2026-08-02 two host review seats
reported that the question "is any claim in a commit message false?" was unanswerable
by them, and one of the two mistook the stale copy of `CLAUDE.md` in its own context for
the file on disk. Both failures are shell-shaped.

**A host reader is the exception, and it is read-only.** `Read`, `Grep`, `Glob` and
nothing else — no write tool, no shell. Reach for it when the question is *not* about
this repository's tree: a design argument, a second opinion, a consult, or reading
something outside the folder. Short results come back in its reply; a report that must
outlive the agent belongs in a chamber, which can write one to `/out`.

That is the boundary, and it is narrower than "cannot damage anything". A host reader
cannot write, because it holds no tool that does. **A chamber agent cannot modify this
machine's checkout** — nothing it writes is real until a human reads the diff. It can
still spend vendor quota, read whatever is mounted, and reach the network, and no
later review undoes any of those. Keep what a chamber is given bounded, and read the
diff before it lands.

**Whichever seat, tell it to read the governing documents from disk.** Every agent boots
with a copy of `CLAUDE.md` injected by the harness, and that copy can be older than the
checkout. One review seat filed two false findings from its stale copy on 2026-08-02.

## The roles

| Role | Where | Seat | Effort | For |
|---|---|---|---|---|
| `scout` | host | Codex Spark or Sonnet 5; Haiku only for a genuinely enormous sweep | `low` | where is X, what mentions Y — paths and line numbers, never a judgement |
| `auditor` | host | set per seat, mixed vendors | `high` — floor | blind review of a diff or a tree |
| `consult` | host | Fable 5 or Sol | `xhigh` — floor; `max` sanctioned | object to a design before it is built |
| `builder` | chamber | Sonnet 5 or Terra; **Opus 5 where a defect would be quiet** | `medium` | build what a written spec says |
| `rebuilder` | chamber | Opus 5 | `medium` | read the old system through the window and write its replacement new |

Three host seats, two chamber briefs. `worker` and `infra-worker` were one job at two
stakes and are now `builder`, which carries the infrastructure rules always — test
first, fail closed, never shortcut the gate you are editing. `rebuilder` is separate
because it is a different *method*: never copying a byte is the whole job.

## What the seats actually cost

Measured 2026-08-01, forty-eight cells, one chamber each, scored by a held-out suite.
Full data and method: [history/2026-08-01_model-matrix.md](../../history/2026-08-01_model-matrix.md).

**Forty-two of forty-eight cells scored full marks and none scored in between.** So this
is a speed and reachability ranking, not a capability one. It says which seat to reach
for on ordinary bounded work. It says nothing about hard work, because nothing in the
matrix was hard enough to separate the seats.

**Fastest seats for a bounded unit**, all scoring identically:

| Seat | Effort | Time |
|---|---|---|
| `gpt-5.3-codex-spark` | `low` | **14s** |
| `sonnet` | `low` / `medium` | **15s** |
| `sonnet` | `high` | 19s |
| `fable` | `medium` | 23s |
| `opus` | `low` | 25s |
| `gpt-5.6-terra` | `none` | 31s |
| `haiku` | any | 43–53s |
| `gpt-5.6-luna` | `low` | 71s |
| `opus` | `max` | 228s |

Three things follow, and each contradicts something obvious:

- **Effort buys time, not correctness.** `opus` at `max` spent nine times the wall clock
  of `opus` at `low` for the identical score. Raising effort on well-specified work is
  spending for nothing. Raise it when the *question* is hard — a design, a review — not
  when the work is merely long.
- **Sonnet is the fastest Claude seat and Haiku is not.** Sonnet finished in 15s; Haiku
  never beat 43s. The cheap seat was three times slower for the same result, so reach
  for Sonnet by default and Haiku only when a sweep is genuinely enormous.
- **Luna is slower than its price suggests**, and this task played to its strengths — one
  page of context. Its published long-context weakness is real and untested here.

**Reachability, which is not negotiable:** `minimal` is rejected by every Codex model.
`gpt-5.3-codex-spark` also rejects `none` and `max`; its usable range is `low`–`xhigh`.
Claude accepts `low`, `medium`, `high`, `xhigh`, `max` and nothing else.

## Choosing between the vendors

**The GPT seats carry the token-heavy work** (Tyrel, 2026-08-01). Luna, Terra and Sol
are free until the usage cap, so anything bulky is theirs by default and Claude's budget
goes to what only Claude is doing. This is the first question about a unit of work,
before which model is marginally better at it: *is this heavy? then it is theirs.*

Free until the cap still means the cap is what is being spent. A seat left running on a
question nobody needed answered still costs the next one.

| The work is… | Reach for |
|---|---|
| long and bulky — a whole tree, a long document | **Terra**, then **Sol** — both hold a long context |
| short, mechanical, repetitive | **Spark** or **Sonnet** at `low` — the two fastest seats measured |
| drafting from a tight spec | **Terra**, then **Sonnet** |
| security-shaped reading | **Sol** |
| a whole-system design question | **Sol at `max`**, or **Fable 5** |
| building where a defect would be **quiet** | **Opus 5** — hooks, CI, seals, accounting, money paths |
| reading old code for contamination | **Opus 5** — the judgement *is* what crosses the boundary |
| a blind review seat | **mixed vendors, always** |

The two Opus rows are Opus for a reason rather than by habit: both are judgement about
what is *allowed* to enter the repository, made against the governing documents.

**A review pass is the one place vendor mix is a rule.** Three seats, two vendors
minimum, blind, identical prompts. Agreement between seats from one vendor is the
weakest kind of agreement.

**These facts have a shelf life.** Prices and tiers moved twice in the three weeks
before this was written. Treat the shape as durable and re-check the numbers before
leaning on one.

## Effort rules

- A **floor** never moves down without Tyrel's per-instance override: a review must not
  quietly run at a cheap session's depth. The floor values are duplicated in
  `test_roster.py`, which enforces them; change both together.
- **Medium is the default.** High or above is a deliberate choice reserved for planning
  and judging — and the matrix says why: on building work it buys nothing.
- Effort is set on the dispatch, never written into a brief, because a value in prose is
  a value nothing enforces. `dispatch` requires a model and defaults effort to `medium`.
- **An orchestrating seat gets no time limit.** Tyrel's ruling, 2026-08-02: a seat sent
  in as ultracode or at `max` to fan out across a large job is being asked for depth,
  and a stated deadline is an instruction to attempt less. Give it the objective and
  the stop conditions and let it run. Ordinary bounded units keep their deadline —
  a unit that runs forever is a different failure, and a seat blind to a real kill
  cannot triage before one.

  There is no mechanical kill on a chamber dispatch: `docker exec` runs until the CLI
  exits. So a deadline in a brief shapes what the agent chooses to attempt and nothing
  else, which is exactly why naming one for an orchestrator is a mistake.
- **A Claude chamber cannot be watched.** The CLI buffers its output until it exits, so
  the dispatch log stays empty for the whole run; the only live progress signal is
  `git log` inside the chamber. Codex streams as it goes. Plan an unattended run around
  that rather than discovering it at three in the morning.
- Record what was **requested** and what **answered**. A request is not proof. A review
  seat whose resolved effort is under its floor is non-qualifying coverage — redispatch,
  or take his override, never a trailer. When the runtime does not expose the resolved
  value, say it was not exposed rather than assuming it qualified.

## Prompting

- Ask Opus 5 for the work and the honest report — no added verification ceremony. It
  verifies unprompted; told to verify, it over-verifies and gains nothing. Named checks
  — run this suite, paste this output — are not ceremony and always stay.
- A review prompt sets no severity floor and never says "only serious issues". Ask for
  everything and filter afterwards; the instrument may not constrain what it measures
  (GOVERNANCE 10).
- Effort controls thinking volume, never visible response length. Prompt for length and
  shape directly.
- Every prompt names the objective, the allowed paths and actions, the deadline, the
  deliverable, the checks, and the stop conditions. Long work writes results
  incrementally.

## Bounds

- `Agent` is denied to every role, and project settings cap spawn depth at one. Fan-out
  stays visible to the main session. **Both of those are bounds on *host* agents and
  neither applies inside a chamber.**
- **A chamber agent may orchestrate freely, and that is the point of the boundary
  rather than a hole in it.** It can spawn its own agents, fan out across its clone,
  and spend as much as the job takes — because nothing it *writes* is real until a human
  reads the diff, and a clone it wrecks is destroyed and rebuilt in seconds. A session
  that hesitates to let a chamber agent orchestrate has misread what the container is
  for: the bounds above exist because a host agent runs on Tyrel's machine, and a
  chamber agent does not.

  **What the container does not bound is spend, reading and egress.** Fanning out wide
  consumes vendor quota that no review returns, the chamber can read everything mounted
  into it, and the network is open because the CLIs need their provider. "As much as the
  job takes" is a statement about the tree, not about the bill.

  Two things still hold in there, and neither is about caution. The chamber agent is
  the only integrator of its own work — a sub-agent's claim is evidence, not a finding.
  And **hard rule 10 does not stop at the container wall**: no agent of its may edit a
  governed path either, because the wording that binds later sessions is Tyrel's to
  approve wherever it was written.
- No role in this directory has a write or shell tool. That is a bound on the roster,
  not a fact about today's entries: a role added here with `Write`, `Edit`,
  `NotebookEdit` or `Bash` fails `test_roster.py`.
- **The built-in agent types may be used, and none of them is read-only.**
  `general-purpose` and `claude` hold every tool; `Explore` and `Plan` hold everything
  except `Write`/`Edit`/`NotebookEdit`, which still leaves `Bash`, and a shell writes
  whatever a shell writes. What makes them affordable is that the guard judges a
  subagent's tool calls as it judges the session's, plus one refusal that applies to
  agents alone: **no spawned agent writes a governed path**, by tool or by shell.
- Every spawned agent gets this preamble, because a built-in carries no project
  instruction at all:

  > You are working in Apparatus Verbatus. Read `CLAUDE.md`, `GOVERNANCE.md` and
  > `GLOSSARY.md` before concluding anything, and use the glossary's words rather than
  > synonyms. Never edit a governed path — the root documents or anything under
  > `.claude/` — propose exact wording instead. Never push, merge, or open a pull
  > request. Report what you did **not** do as plainly as what you did; unsure is a
  > legitimate answer. If a rule and your task pull apart, stop and say so.

- Memory stays off: reviews remain blind and legacy knowledge does not cross sessions
  unseen. `maxTurns` stays off — give an agent a deadline it can plan against instead.
- Smallest useful roster, fan-out visible, results verified, session the only
  integrator. An agent team whose members must challenge one another is exceptional —
  say why before building one.
