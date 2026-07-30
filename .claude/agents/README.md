# Claude agent roster

The main session is the accountable lead. Agents receive bounded work and return evidence,
proposals, or an isolated diff; they do not own scope, integration, decisions, or the user
conversation.

| Role | Model | Effort | Unit of work |
|---|---|---|---|
| `scout` | Haiku | `low` default; `low`–`high` sanctioned | locate files and references; extractive digests |
| `worker` | Sonnet | `medium` default; `low`–`high` sanctioned | implement one written, non-critical specification |
| `infra-worker` | Opus | `high` — floor | hooks, CI, accounting, seals, money paths |
| `auditor` | Opus pinned, set per seat in a reviewer pass | `high` — floor | read-only review |
| `consult` | inherited model | `xhigh` — floor; `max` sanctioned | read-only design objection |
| `rebuilder` | Opus | `high` — floor | rebuild one coherent legacy system in the autoclave |

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
  so. A request is never proof of what answered.

## Prompting the roster

- Ask Opus 5 for the work and the honest report — no added verification ceremony. It
  verifies its own work unprompted; told to verify, it over-verifies and gains nothing.
  Named, concrete checks — run this suite, paste this output — are not ceremony and
  always stay.
- A review prompt sets no severity floor and never says "only serious issues" — ask for
  everything and filter afterwards. The instrument may not constrain what it measures
  (GOVERNANCE 10).
- Cross-vendor seats live in `operations/codex/seats.conf` — read-only evidence seats
  today. If a cross-vendor writing seat returns (a recorded ruling allows one in
  principle), it arrives only through its own reviewed change and what it drafts lands in
  the autoclave. That routing is about that future seat, not the Claude writing roles,
  whose worktree and autoclave rules are their own files'.

## Bounds

- Every prompt names the objective, allowed paths/actions, deadline, deliverable, checks, and
  stop conditions. Long work writes results incrementally.
- `Agent` is denied to every role. Project settings also cap spawn depth at one, covering
  built-ins. Fan-out stays visible to the main session.
- Read-only roles have no write or shell tools. Writing roles need a caller-prepared,
  correct-base worktree and never push or merge.
- A spawned agent never edits the six governing documents. It proposes exact wording.
- Memory stays off: reviews remain blind and legacy knowledge does not cross sessions unseen.
- Model and effort fields are requests, not proof of what answered. Record the resolved
  release and effort when the runtime exposes them, and say when they are not exposed.
- `maxTurns` stays off. Give an agent a deadline it can plan against instead.

Agent teams are experimental, higher-cost, and self-coordinate. Do not enable them as a
default. Use a bounded read-only team only when teammates genuinely need to challenge one
another; otherwise use subagents that report to the lead.

`isolation: worktree` is not set because Claude Code creates that worktree from the default
branch rather than the parent session's current commit. Writing roles therefore require the
main session to prepare the correct base explicitly.
