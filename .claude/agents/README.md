# Agents — read this before spawning one

Agent use is encouraged here. It is the normal shape of work past a few turns, not an
escalation, and small use needs no ceremony. What follows is the whole of what a session
needs to choose one well: the two kinds, the roles, what each seat actually costs
measured on this machine, and the bounds.

**Do not choose a model or an effort from memory.** The table below was measured; your
recollection was not.

## The two kinds, and the only line that matters

Capability is a property of **where** an agent runs, not of what it is called.

**A reader runs on this machine and is read-only.** Locating, reviewing, auditing,
objecting to a design. `Read`, `Grep`, `Glob` and nothing else — no write tool, no
shell. It leaves its report as a file rather than only in a reply, so the work outlives
the agent and does not fill the session's context.

**Anything that writes or edits runs in a container.** Its own clone, its own branch, a
full shell, its own tests. The repository goes in read-only, no pushing credential goes
in at all, and the branch comes back to be read before anything lands. How to build,
sign in, dispatch and collect: [operations/autoclave/README.md](../../operations/autoclave/README.md).
The standing briefs are in `operations/autoclave/briefs/`.

That is the whole boundary. A reader cannot damage anything because it holds no tool
that writes; a writer cannot damage anything because nothing it touches is real until a
human reads the diff.

## The roles

| Role | Where | Seat | Effort | For |
|---|---|---|---|---|
| `scout` | host | Haiku 4.5, or Codex Spark | `low` | where is X, what mentions Y — paths and line numbers, never a judgement |
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
  stays visible to the main session.
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
