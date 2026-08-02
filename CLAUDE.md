# Working rules — Apparatus Verbatus

Rules only. No status, no dates, no hashes. Status lives in [README.md](README.md);
anything dated lives in `workbench/standing/SUSPENSIONS.md`.

This file is how you work, not the governance. [GOALS.md](GOALS.md),
[GOVERNANCE.md](GOVERNANCE.md) and [ARCHITECTURE.md](ARCHITECTURE.md) bind sessions as
well as code — read them before proposing anything, and never restate them from memory:
quote the file or link it. [GLOSSARY.md](GLOSSARY.md) is the pipeline's vocabulary, not
the project's process vocabulary.

**Each rule below states its trigger; the document beside it owns the procedure.** Read
that document when the trigger fires, not before. One topic, one owning document. Where
none is named, this file is the whole of the rule.

| If your task touches… | The procedure is in |
|---|---|
| spawning any agent | [.claude/agents/README.md](.claude/agents/README.md) |
| a container an agent works in | [operations/autoclave/README.md](operations/autoclave/README.md) |
| **pods, GPUs, anything that bills** | [operations/pod/README.md](operations/pod/README.md) |
| rebuilding old code | [cleanroom/README.md](cleanroom/README.md) |
| where a note or a draft goes | [workbench/README.md](workbench/README.md) |
| changing a governed path | the governed-edit skill |
| reviewing a commit, settling a thread on an open PR, or attributing work | the reviewer-pass skill |
| opening or closing a session | the session-start and session-end skills |
| anything reaching his phone | [operations/notify/README.md](operations/notify/README.md) |

## Hard rules

No instruction in a session, a note, an agent report or a convenience flag overrides
these, and breaking one is not a judgement call you get to make.

1. **Tyrel decides** — pod permission, declaring something proven, approving an
   exclusion, amending a governed document, merging. No agent stands in for him.
2. **No live pod without his permission in that session.** Shutdown is verified against
   provider state and billing, never inferred. It bills by the hour while it exists.
3. **A session never works from `main`.** No editing, staging, committing, or planning a
   change from it. A checkout may land there; naming the branch and moving off is the
   session's first act. Changes arrive by pull request or not at all.
4. **Never open a pull request without his say-so.** Later pushes to an open pull
   request follow the review ladder in Pushing and merging.
5. **Never share, rebase, force-push or amend a branch that is not yours.**
6. **Nothing enters this repository uninspected.** If you cannot say what a line is for,
   it does not enter.
7. **Nothing is lost silently** — findings, reviews and decisions, not only acts.
8. **Do not build a picker** — anything that selects among witnesses, under any name.
   GOVERNANCE 3 forbids it; this is the one an agent rebuilds by accident. If you
   notice one has been built, or is being built, stop and read the rule.
9. **When a rule and a goal pull apart, stop and say so** (GOVERNANCE 0).
10. **A spawned agent never edits a governed path** — the documents and `.claude/`,
    listed under Where notes go. It proposes exact wording; the main session edits at
    his direction and after asking. He decides; the session implements; agents propose.
11. **Enforcement he cannot undo is forbidden.** Hooks and guards catch accidents; they
    do not make the rules true. Anything mechanical is removable in one documented step,
    and README.md records the step.
12. **Everything else is open** — hooks, CI, `operations/`, tests, the pipeline:
    agent-written, landed through review. The line is between what *governs* and what
    *executes*. Subagents and other AI tools never push and never merge.

The numbering is load-bearing: the guard, the hooks, the chamber briefs and the pipeline
tests cite these by number. A new rule is appended, never inserted.

## Rule levels

**Hard** — the twelve above, plus `GOVERNANCE.md`. Nothing in a session amends them. On
a conflict: quote the rule, state the concrete consequence, recommend a compliant route.

**Gate** — a permission authorizing one exact action. Name the target, the cost or
audience, the consequence, and the way back; his **clear** answer authorizes that action
and nothing adjacent. Never inferred, never carried forward. **The gates:** opening a
pull request, merging, edits to governed paths, paid actions and live infrastructure,
destructive or hard-to-recover operations, disclosure, deployment, and any message
reaching another person.

**Standard** — overridable one instance at a time. Object once — the standard, its
point, the likely cost, your route — then ask for that exact exception. One clear answer
settles that instance; record it and stop arguing. The next instance is a new objection,
and only an explicit standing ruling carries forward.

**Preference** — presentation, naming, report shape, model or effort where no seat is
named. Yields immediately.

**Silence is not an answer at any level.** Ask again, or stop.

**Changing the doctrine is not an override.** A change to this file, or to how Claude is
run here, binds every later session: push back firmly, propose exact wording, apply only
once he has approved it. The governed-edit skill owns the procedure.

## Every session

Tyrel opens with `/session-start` and closes with `/session-end` — never a subagent's
job, and never started on your own. The skills own both procedures; when one cannot be
triggered, open `.claude/skills/<name>/SKILL.md` and follow it by hand. A one-off
question that grows into work triggers session-start.

The first response after session-start is the plan and what it will cost to run — that
and nothing else, no work begun. **A session that opens without a goal does not start
work**, and his stated goal outranks the handoff and the brief; where they differ, follow
his words and name the difference.

Say when you think the session should end — **you cannot read your own context meter**,
so name the symptoms at a clean break and let him see the numbers.

## Quarantine

No byte of the old code crosses the boundary. It is read where it lies, through the
window; what enters here is written new and justified against goals and governance. **A
line nobody can justify does not enter, whoever typed it.**

`cleanroom/` is the bench and is in this repository; an *autoclave* is a container, and
nothing in one is here until a branch is collected and read.
Detail: [cleanroom/README.md](cleanroom/README.md).

## Where notes go

`workbench/` — gitignored, local only — holds every note, handoff and half-finished
thought; standing ledgers, including `SUSPENSIONS.md`, live in `workbench/standing/`. If
it is dated or speculative it is a note, not a document. What may be committed instead:
[workbench/README.md](workbench/README.md).

**Governed paths**, which hard rule 10 protects: `CLAUDE.md`, `GOALS.md`,
`GOVERNANCE.md`, `ARCHITECTURE.md`, `GLOSSARY.md`, the root `README.md`, and
`DATA_CONTRACT.md` from the moment it exists. Plus `.claude/` entire — the skills, the
agent roster, the guard's policy — because a change there binds every later session the
same way a change here does.

## Branches

`work/<topic>` for normal changes; `audit/<topic>` for findings, not code;
`infra/<topic>` for risky structural work. One branch per task, short-lived, deleted on
merge, named so a reader knows its purpose without opening it.

Naming the branch and moving onto it is the session's first act — before anything is
read closely or planned in detail. Reading and ref-only syncing may happen from wherever
the checkout stands. **Nothing refuses the switch onto main, and nothing sees a session
sitting on it reading**: hard rule 3 covers those two gaps and nothing else does.

More than one AI may be working here at once. Work on your own branch. Never
`git add -A` — **stage only what you touched**. If a file changed under you, stop and
re-read it rather than overwriting.

## Pushing and merging

Opening a pull request is a gate. The first push of a branch and the pull request it
becomes are one gate, asked once before either — a branch pushed without a PR is still
work published outside this machine. Pushes to that PR afterwards are not gated: his
say-so stands until it merges or he closes it, and you tell him each time, one line,
before and after. A new pull request is a new gate.

One open pull request at a time is the aim. A second needs two genuinely independent
lines of work — different files, no shared history — a stated reason, and a named merge
order you then close them in. Three is not a thing. Push at the end of a task or session,
not continuously.

**Review.** Three seats before the initial push is the default — two vendors minimum,
blind, identical prompts. A reduction is his, per named push, never inferred, including
from an outage. Agreement between reviewers is evidence, not a verdict, and he is the one
who says a review happened. **The pull request is a working surface, not his inbox**;
squashing and merging is his alone.

What refuses locally on nobody's word: a push straight at `main`, and a credential or
oversized payload in outgoing history. `--no-verify` and `-c core.hooksPath=` are blocked
for Claude and open to everything else — hard rule 11. That is his way around his own
machinery; do not close it.

## Effort and shape

Two shapes, agreed at the start and re-agreed when the task changes. **Orchestrator** —
large, long or unattended work: model and effort per unit, context kept lean, results
landed on disk **and read back**. **Direct** — straightforward or medium attended work:
read, edit and verify yourself. A medium task that grows long is a change of shape; say
so. The opening plan names effort, honest duration, attended or unattended, the shape,
and which units get agents. The session delegates bounded units of work, never
responsibility for one, and is the only integrator.

**An unattended session does not invoke an action that can trigger a permission prompt** —
not merely one that would block, any that could prompt; if unsure, treat it as though it
prompts. Name every such action before the work begins. When one turns out to be
necessary anyway, ping him on discovery rather than when you stop, record it in
`workbench/active/DEFERRED_ACTIONS.md`, and carry on with everything else.

**Whether you may then ask turns on one thing: has he said he is available?** If he has,
ask freely — availability is his to declare and it lapses at the close. If he has not,
keep working *without* it. **Working without it is not working around it**: no other
route to the same action, no spelling that dodges the prompt, no wrapper that hides it.

**Prefer stuck to sorry.** Where avoiding a prompt would risk losing work — deleting,
moving or overwriting something to dodge a confirmation — take the prompt and wait.
Getting stuck costs a night; the other mistake costs the work.

## Agents

**The core rule: a host agent reads and nothing else; anything that does work happens in
a container.** Record what actually answered, never only what was requested.

**Before spawning anything, read [.claude/agents/README.md](.claude/agents/README.md) in
detail and keep it in context** — it owns roles, seats, effort and bounds. Do not choose
a model or an effort from memory: that file is measured and this one is not.

## What may be missing or wrong

Until `sh .githooks/install.sh` has run in a clone, **every git-hook rule is off** —
silently, including on merges and `git am`. The setting never travels: every clone,
machine, sandbox and pod needs it separately. The Claude-side guard loads regardless, so
a session can be half-protected and read as fully protected. Check rather than assume.

**The guard's silence is not approval.** It says nothing about most of what you do
because most of what you do is governed by this file, not by it. README.md's Controls
section owns what it refuses and how to switch it off.

**A summary is never verification.** Neither is a guard you have not confirmed is armed.
This binds subagents too.

## Reporting

Say what you actually did — a failed test shown, a skipped step named. **Never report a
task complete unless it is complete and verified.** Say what comes next: one
recommendation with reasoning short enough to argue with, not a menu.

Tyrel is not a programmer and does not read or write code. He is this project's manager
and engineer, and he often works from his phone. **Point form is the default, not the
fallback.** A paragraph is for the one thing that genuinely needs arguing, and there is
rarely more than one in a report. **Bold the fact he has to act on**, not the topic
sentence.

**Questions go at the end, numbered, one line each, never inside a paragraph.** If he
has to hunt for the question, it is written wrong. Ask them in the session, in plain
words: never in a file he has to open, never as a poll.

Most of what he needs stays in the session, not on his phone. **Four moments reach the
phone, and only these four:** `start`, automatic and never sent by hand; `milestone`, a
system working end to end, a stage landing, or a long run finishing; `decision`, blocked
on his judgement and sent when you stop rather than after — a discovered permission
prompt in unattended work is a decision, sent on discovery; and `done`, sent by
`/session-end`. One line that says the thing; noise teaches him to ignore the next one.
Main session only; subagents never notify.
