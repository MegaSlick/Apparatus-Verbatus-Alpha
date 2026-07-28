# The Claude roster

Six agents, one file each. CLAUDE.md holds the table of what each is *for* and which model
it runs on; this file holds what the files themselves cannot say — what is deliberately
switched off, and why, so nobody turns it on later thinking they found an oversight.

**Fixed-role agents declare model and effort in their own files.** `auditor` and `consult`
deliberately select a model per invocation, while keeping effort fixed. The caller records the
model that actually answered; an alias in a file is a request, not proof of the resolved
release.

## What is pinned, and what it buys

| Field | Why it is set |
|---|---|
| `model`, `effort` | fixed roles declare both; `auditor`/`consult` select model per call while keeping role effort |
| `tools` | the allowlist — an auditor with no write tools cannot fix what it is reviewing |
| `disallowedTools` | a second declaration of the tools the role must not use |

These frontmatter fields express the repository's intended role bounds. Their runtime
enforcement is Claude Code behavior outside this repository and must not be described as
locally proven merely because the fields exist. In particular,
`CLAUDE_CODE_SUBAGENT_MODEL` and `CLAUDE_CODE_EFFORT_LEVEL` can override the requested model
and effort; the reviewer-pass procedure requires a manual preflight that refuses those
ambient overrides before dispatch and still records what actually answered.

`Agent` is denied to every role: an agent that spawns agents makes a fan-out nobody
declared and nobody can cost. `WebFetch` and `WebSearch` are denied to every role too — the
roster reads *this* repository and the reference through the window, and a reader that can
reach the open internet is a route around the quarantine that no diff would show.

## Deliberately off

**`maxTurns` — off, and not coming back.** Tyrel's ruling: *"I think a time limit that the
agent knows is better than a turn limit."* A turn cap is invisible to the agent wearing it.
It cannot triage against a budget it cannot see, so it plans work it will not be allowed to
finish and never reserves anything for its report — and because the report is written last,
the cap does not shorten the answer, it deletes it. Measured here: an Opus review spent
236,000 tokens across 54 tool calls, hit a 40-turn ceiling, and returned a single sentence.
The work was done and unrecoverable.

**What replaces it is a stated deadline.** Agent frontmatter has no timeout field, so the
deadline belongs in the prompt: the hard limit, a target below it, and the instruction to
stop and report if it runs short. Any agent expected to run long writes its output to a file
as it goes, so a cut-off degrades the result instead of erasing it. Where a wrapper imposes
the timeout it injects the deadline itself — `operations/codex/seat.sh` knows the number, so
it tells the seat rather than trusting whoever wrote the prompt to remember.

**`memory` — off, and it stays off.** Tyrel's ruling. It is a supported field and it would
work; it is not an oversight. Two reasons, both structural: a reviewer that remembers the
last review is not blind, so the reviewer pass stops meaning what it claims to mean; and an
agent that carries knowledge of the old code between sessions is a way for that code to
cross the quarantine inside a model's head, where no diff and no review can catch it.

**`isolation: worktree` — off, pending Tyrel.** The writing roles therefore require the
caller to prepare and identify a correct-base worktree before they write. They must stop if
that precondition is absent; the role file does not create isolation. The built-in field is
left off for a measured reason rather than an aesthetic one:

> A worktree agent is branched from the **default branch**, not from the parent session's
> `HEAD`.

Everything in this repository is currently built on long-lived branches off `main`, so an
isolated agent could start from a tree with none of the work in progress in it, silently
build against the wrong base, and report success.

Turning it on is a real improvement and should happen — but it needs the branching question
answered first, and it is Tyrel's call, not a session's.

**`hooks`, `permissionMode`, `mcpServers` — not configured here.** The role needs no
role-specific hook or MCP server, and permission mode remains a property of the invoking
session.

**`background` — deliberately left to the caller and runtime.** Omission does not mean
foreground: current Claude Code may schedule a subagent in the background. Scheduling is
not a safety boundary, and the caller must wait for the result before acting on it.

**`skills` — unavailable rather than declined.** The three skills in this repository all set
`disable-model-invocation: true`, and a skill in that state cannot be preloaded into an
agent. `session-start` and `session-end` are the main session's own work and must never run
in a subagent anyway; `reviewer-pass` is summoned by the session, not held by a reviewer.

## The one rule that is not a setting

**Subagents never push and never merge**, whatever their tools allow. A worker may write in
its own worktree, including that worktree's autoclave when required; it never writes in the
main checkout's live tree. What it wrote reaches the repository only through the session
that spawned it, reviewed. No frontmatter field enforces this. It is discipline, like most
of the guards here, and it is written down so that it is at least a rule somebody broke
rather than a rule nobody knew.
