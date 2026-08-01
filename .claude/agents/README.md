# Claude agent roster

The main session is the accountable lead. Agents receive bounded work and return evidence,
proposals, or an isolated diff; they do not own scope, integration, decisions, or the user
conversation.

**Every role here is read-only.** A role that writes is dispatched into a chamber and
never spawned on this machine: `operations/autoclave/README.md` says how, and the
standing briefs live in `operations/autoclave/briefs/`. `worker`, `infra-worker` and
`rebuilder` were host roles until 2026-08-01 and are now briefs — what a role file held
is what a brief holds, and the chamber is a better boundary than a prompt.

| Role | Model | Effort | Unit of work |
|---|---|---|---|
| `scout` | Haiku | `low` default; `low`–`high` sanctioned | locate files and references; extractive digests |
| `auditor` | Opus pinned, set per seat in a reviewer pass | `high` — floor | read-only review |
| `consult` | inherited model | `xhigh` — floor; `max` sanctioned | read-only design objection |

**Medium is the default** (Tyrel, 2026-08-01). High or above is a deliberate choice,
not a resting state, and it is reserved for **planning and judging**. Building from a
written spec is medium work; raise it per dispatch when a unit earns it and say why.

Both remaining floors sit on roles that *judge* rather than build — a blind review seat
and a design objection. A cheap judgement does not look wrong until much later, which is
the whole reason a floor exists. `scout` has none because a scout that reads shallowly
is visibly wrong on the spot.

## The models, in one breath

Haiku is the cheap fast reader — about a fifth of Opus's burn, fine for finding things,
never for judging them. Sonnet 5 is near-Opus on coding, follows a spec to the letter,
at roughly half Opus's burn — the workhorse. Opus 5 is the default brain — the strongest
agentic judgement for the money; prompt its report shape and length explicitly (below).
Fable 5 is the ceiling at twice Opus's burn — always thinking, minutes-long turns —
spent only where being wrong is expensive and the question sits above what Opus reliably
clears. Across the aisle, GPT Sol is OpenAI's flagship at Opus-class cost, notably
strong on security-shaped reading, and it spends Tyrel's *other* budget; Terra is its
half-price sibling for bulk mechanical drafting when Sol's budget tightens.

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
