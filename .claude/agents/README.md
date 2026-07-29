# Claude agent roster

The main session is the accountable lead. Agents receive bounded work and return evidence,
proposals, or an isolated diff; they do not own scope, integration, decisions, or the user
conversation.

| Role | Default | Unit of work |
|---|---|---|
| `scout` | Haiku, low | locate files and references |
| `worker` | Sonnet, medium | implement one written, non-critical specification |
| `infra-worker` | Opus, high | hooks, CI, accounting, seals, money paths |
| `auditor` | Opus, high | read-only review |
| `consult` | inherited model, xhigh | read-only design objection |
| `rebuilder` | Opus, high | rebuild one coherent legacy system in the autoclave |

## Bounds

- Every prompt names the objective, allowed paths/actions, deadline, deliverable, checks, and
  stop conditions. Long work writes results incrementally.
- `Agent` is denied to every role. Project settings also cap spawn depth at one, covering
  built-ins. Fan-out stays visible to the main session.
- Read-only roles have no write or shell tools. Writing roles need a caller-prepared,
  correct-base worktree and never push or merge.
- A spawned agent never edits the six governing documents. It proposes exact wording.
- Memory stays off: reviews remain blind and legacy knowledge does not cross sessions unseen.
- Model and effort fields are requests, not proof of what answered. Record the resolved release
  when the runtime exposes it.
- `maxTurns` stays off. Give an agent a deadline it can plan against instead.

Agent teams are experimental, higher-cost, and self-coordinate. Do not enable them as a
default. Use a bounded read-only team only when teammates genuinely need to challenge one
another; otherwise use subagents that report to the lead.

`isolation: worktree` is not set because Claude Code creates that worktree from the default
branch rather than the parent session's current commit. Writing roles therefore require the
main session to prepare the correct base explicitly.
