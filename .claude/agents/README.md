# The agent roster

The main session is the accountable lead. Agents receive bounded work and return
evidence, proposals, or an isolated diff; they do not own scope, integration, decisions,
or the user conversation.

**There are five jobs, and where an agent runs decides what it can do.** On this
machine, read-only, no exceptions. In a chamber, everything — its own clone, its own
branch, a full shell, its own tests, and no way back out except a branch the session
reads. `operations/autoclave/README.md` is that route.

| Job | Where | Default seat | Effort | What it is for |
|---|---|---|---|---|
| `scout` | host | Haiku 4.5, or Codex Spark | `low`; up to `high` | where is X, what mentions Y — paths and line numbers, never a judgement |
| `auditor` | host | set per seat, below | `high` — floor | blind review of a diff or a tree |
| `consult` | host | Fable 5 or Sol | `xhigh` — floor; `max` sanctioned | object to a design before it is built |
| `builder` | chamber | Terra or Sonnet 5; **Opus 5 when a defect would be quiet** | `medium`; `high` when the unit earns it | build what a written spec says |
| `rebuilder` | chamber | Opus 5 | `medium`; `high` sanctioned | read the old system through the window and write its replacement new |

That is three host seats and two briefs, down from six role files. `worker` and
`infra-worker` were one job at two stakes and are now `builder`, which carries the
infrastructure rules always — test-first, fail closed, no shortcut through the gate you
are editing — because those are good rules for ordinary code too. What used to be the
difference between them is now what it always really was: **which model, at what
effort**, decided on the dispatch. `rebuilder` stays separate because it is a different
*method*, not a different stake — never copying a byte is the whole job.

**Medium is the default** (Tyrel, 2026-08-01). High or above is a deliberate choice, not
a resting state, and it is reserved for **planning and judging**. Both floors sit on the
seats that judge; a cheap judgement does not look wrong until much later, which is the
whole reason a floor exists. `scout` has none because a shallow scout is visibly wrong on
the spot.

## Choosing a model, across both vendors

**The GPT seats carry the token-heavy work** (Tyrel, 2026-08-01). Luna, Terra and Sol
are to be treated as **free until the usage cap is reached** — not cheap, free — so
anything bulky goes to them by default and Claude's budget is spent on what only Claude
is doing. This is the first question to ask about a unit of work, before which model is
marginally better at it: *is this heavy? then it is theirs.*

That is a budget ruling, not a quality claim. It is also not a licence to be wasteful —
free until the cap means the cap is the thing being spent, and a seat left running on a
question nobody needed answered still costs the next one.

**The three GPT tiers are not three sizes of the same thing.** Sol is the flagship for
genuinely hard reasoning. Terra is the balanced default and matches the previous
flagship's class at half its price. Luna is the fast, high-volume tier — and it carries
one sharp, specific weakness that decides where it may be used here.

**Luna cannot hold a long context.** Published long-context recall is about 41%, against
roughly 90% for Terra and Sol, while on agentic benchmarks it sits level with Terra. It
is not a weaker Terra; it is a Terra that forgets. Almost everything this project
dispatches is long-context by nature — read the governing documents, then a tree, then
report — so **Luna is the wrong seat for the work this repository does most**, however
cheap it is. Give it short, bounded, mechanical passes: counting, listing, classifying
one file at a time, a sweep whose whole input fits in a page. Never a review, never an
audit, never a rebuild, never anything that must recall what it read an hour ago.

| The work is… | Reach for | Because |
|---|---|---|
| **long and bulky — a whole tree, a long document** | **Terra**, then **Sol** | free until the cap, and both hold a long context. This is where the volume belongs |
| short, mechanical, repetitive — count, list, classify | **Luna**, or **Codex Spark**, or **Haiku 4.5** | near-free at volume; Luna only while each pass stays small |
| drafting from a tight spec | **Terra**, then **Sonnet 5** | matches the previous flagship's class at half its price; Sonnet when the chamber wants a Claude seat |
| security-shaped reading | **Sol** | notably stronger there than its price suggests |
| a whole-system design question | **Sol at `max`**, or **Fable 5** | the design seats already exist in `seats.conf` at max and the full hour |
| building where a defect would be **quiet** | **Opus 5** | hooks, CI, seals, accounting, money paths — where being subtly wrong is invisible until it matters |
| reading old code for contamination | **Opus 5** | the judgement *is* what crosses the boundary, and it does not run shallow |
| a blind review seat | **mixed, always** | see below — this is the one place vendor mix is a rule |

The two Claude-only rows are Claude-only for a reason and not by habit: both are
judgement about what is *allowed* to enter the repository, made against governing
documents, and that is the thing this project has watched Opus do well. Everything above
them is volume, and volume is the GPT suite's.

**These tier facts have a shelf life.** Prices and capabilities moved twice in the three
weeks before this was written. Treat the *shape* as durable — a flagship, a balanced
default, a volume tier that cannot hold a long context — and re-check the numbers before
leaning on one, rather than trusting this paragraph a year from now.

**Luna has no line in `operations/codex/seats.conf` yet** and therefore cannot be
dispatched by name. It needs its model id, effort and timeout added there before it is
reachable — a one-line reviewed change to that file.

**A review pass is the one place vendor mix is a rule rather than a preference.** Three
seats, two vendors minimum, blind, identical prompts — CLAUDE.md's Pushing and merging
section, and the reviewer-pass skill holds the procedure. Agreement between seats from
one vendor is the weakest kind of agreement.

**A brief does not name a vendor.** The chamber signs both in and takes the same brief
either way, so `dispatch <task> claude|codex <brief>` is where the choice is made and
`operations/codex/seats.conf` governs only the read-only Codex seats that run on the
host.

## Effort semantics

- A **floor** never moves down without Tyrel's override, given per instance: a review must
  not quietly run at a cheap session's depth. Raising above a floor needs no override —
  it is still a cost event, declared like any other (CLAUDE.md, "Effort and shape"). The
  floor values are duplicated in `test_roster.py`, which enforces them; change both
  together.
- A **default** is what the file requests; the caller chooses within the role's sanctioned
  range per unit of work, where the dispatch mechanism can set it — a `worker` at `high`
  for a spec hiding a tricky adapter. A cross-vendor seat's tier is not a dispatch-time
  choice: it lives in `operations/codex/seats.conf`, and changing it is a reviewed change.
- The ranges around defaults are **guides, not walls** — inside the effort and cost
  envelope Tyrel agreed for the session. A departure staying inside that envelope is
  asked of him when he is present and is the session's recorded judgement call when he is
  not. **A judgement floor is never in that class**: no session judgement lowers one,
  attended or unattended, and no budget, deadline or outage does either — only Tyrel's
  per-instance override. A departure that changes cost beyond the agreed envelope follows
  CLAUDE.md's permission rules; unattended, hold the work rather than self-authorize.
- Every dispatch records what was **requested** — model and effort — and preserves the
  **resolved** values when the runtime exposes them; when it does not, the record says
  so. A request is never proof of what answered. The record lives with the session and
  lands in its handoff at close. A review seat whose resolved effort is under its floor
  is non-qualifying coverage — redispatch or Tyrel's per-instance override, never a
  trailer (the reviewer-pass skill states the same rule). When the resolved value is
  not exposed, record that it was not exposed: the request stands and the pass is
  reported as unverified on that point, never silently assumed to qualify.

## Prompting the roster

- Ask Opus 5 for the work and the honest report — no added verification ceremony. It
  verifies its own work unprompted; told to verify, it over-verifies and gains nothing.
  Named, concrete checks — run this suite, paste this output — are not ceremony and
  always stay.
- A review prompt sets no severity floor and never says "only serious issues" — ask for
  everything and filter afterwards. The instrument may not constrain what it measures
  (GOVERNANCE 10).
- Effort controls thinking volume, never visible response length — official guidance is
  explicit that changing effort does not reliably shorten responses. Prompt for length
  and shape directly, and for files on disk say what the document needs and no more.
- Cross-vendor seats live in `operations/codex/seats.conf` — read-only evidence seats
  today. A cross-vendor writing seat is now the same thing as any other writing agent:
  it is dispatched into a chamber, which signs both vendors in and takes a brief either
  way. `operations/autoclave/README.md` is the whole of that route.

## Bounds

- Every prompt names the objective, allowed paths/actions, deadline, deliverable, checks, and
  stop conditions. Long work writes results incrementally.
- `Agent` is denied to every role. Project settings also cap spawn depth at one, covering
  built-ins. Fan-out stays visible to the main session.
- No role has a write or shell tool. That is the bound, not a property of the current
  roster: a role added here with `Write`, `Edit`, `NotebookEdit` or `Bash` fails
  `test_roster.py`, because writing work belongs in a chamber.
- **Spawn only the three roles named in this file.** Claude Code ships built-in agent
  types that this repository does not define and `test_roster.py` cannot see, and
  **not one of them is read-only**:

  | Built-in | What it holds | Why it is not used here |
  |---|---|---|
  | `general-purpose`, `claude` | every tool | unbounded write and shell on this machine |
  | `Explore`, `Plan` | everything except `Write`, `Edit`, `NotebookEdit` — **so `Bash`** | a shell writes files; withholding `Edit` does not make a role read-only |
  | `claude-code-guide` | `Bash`, `Read`, `WebFetch`, `WebSearch` | same, plus it reaches the network |
  | `statusline-setup` | `Read`, `Edit` | writes, and configures the harness |

  `Explore` and `Plan` are the tempting ones — they look read-only and are not. A role
  holding `Bash` can write anything a shell can write, which is why `test_roster.py`
  counts `Bash` as a write tool and always has.

  Nothing mechanical stops a session reaching for one of these, so "every host agent is
  read-only" is true of this roster and **not** true of the runtime. The rule is what
  closes that, which is the ordinary state of things here (hard rule 11). Anything that
  needs to write is dispatched into a chamber.
- A spawned agent never edits a governed path. It proposes exact wording.
- Memory stays off: reviews remain blind and legacy knowledge does not cross sessions unseen.
- Model and effort fields are requests, not proof of what answered. Record the resolved
  release and effort when the runtime exposes them, and say when they are not exposed.
- `maxTurns` stays off. Give an agent a deadline it can plan against instead.

Agent teams are experimental, higher-cost, and self-coordinate. Do not enable them as a
default. Use a bounded read-only team only when teammates genuinely need to challenge one
another; otherwise use subagents that report to the lead.

`isolation: worktree` is not set on any role, and no longer could matter: Claude Code
creates that worktree from the default branch rather than from the parent session's
current commit, which is why writing roles used to need a caller-prepared base. A
chamber is pinned to a named commit by `autoclave.sh new`, which is the same problem
solved where it can actually be solved.
