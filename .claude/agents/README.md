# The Claude roster

Six agents, one file each. CLAUDE.md holds the table of what each is *for* and which model
it runs on; this file holds what the files themselves cannot say — what is deliberately
switched off, and why, so nobody turns it on later thinking they found an oversight.

**Every setting is pinned in the agent's own file.** Model and effort especially: an agent
that inherits effort from whichever session happened to spawn it gives a different answer on
a cheap night than on an expensive one, and neither the caller nor the record can tell which
one they got.

## What is pinned, and what it buys

| Field | Why it is set |
|---|---|
| `model`, `effort` | depth is a property of the role, never of the calling session |
| `tools` | the allowlist — an auditor with no write tools cannot fix what it is reviewing |
| `disallowedTools` | applied *before* `tools` resolves, so it survives someone widening the allowlist. Belt and braces on purpose |
| `maxTurns` | a bounded agent. An unbounded one can spend a night's budget on a loop nobody is watching |

`Agent` is denied to every role: an agent that spawns agents makes a fan-out nobody
declared and nobody can cost. `WebFetch` and `WebSearch` are denied to every role too — the
roster reads *this* repository and the reference through the window, and a reader that can
reach the open internet is a route around the quarantine that no diff would show.

## Deliberately off

**`memory` — off, and it stays off.** Tyrel's ruling. It is a supported field and it would
work; it is not an oversight. Two reasons, both structural: a reviewer that remembers the
last review is not blind, so the reviewer pass stops meaning what it claims to mean; and an
agent that carries knowledge of the old code between sessions is a way for that code to
cross the quarantine inside a model's head, where no diff and no review can catch it.

**`isolation: worktree` — off, pending Tyrel.** This is the one the roster most obviously
wants, because `worker`, `infra-worker` and `rebuilder` all *say* in prose that they work in
their own worktree, and prose is not enforcement. It is left off for a measured reason
rather than an aesthetic one:

> A worktree agent is branched from the **default branch**, not from the parent session's
> `HEAD`.

Everything in this repository is currently built on long-lived branches off `main`, so an
isolated agent would start from a tree with none of the work in progress in it, silently
build against the wrong base, and report success. That is worse than the current
arrangement, where the rule is followed by hand and visibly.

Turning it on is a real improvement and should happen — but it needs the branching question
answered first, and it is Tyrel's call, not a session's.

**`hooks`, `permissionMode`, `background`, `mcpServers` — off, unused.** Supported, but
nothing in the current workflow needs them. Left out rather than set to a default, so the
file says only what is true.

**`skills` — unavailable rather than declined.** The three skills in this repository all set
`disable-model-invocation: true`, and a skill in that state cannot be preloaded into an
agent. `session-start` and `session-end` are the main session's own work and must never run
in a subagent anyway; `reviewer-pass` is summoned by the session, not held by a reviewer.

## The one rule that is not a setting

**Subagents never push and never merge**, whatever their tools allow. A worker may write in
its own worktree or in the autoclave; what it wrote reaches the repository only through the
session that spawned it, reviewed. No frontmatter field enforces this. It is discipline,
like most of the guards here, and it is written down so that it is at least a rule somebody
broke rather than a rule nobody knew.
