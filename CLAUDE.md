# Working rules — Apparatus Verbatus

Rules only. No status, no dates, no hashes. State here is a bug; status lives in
[README.md](README.md), and anything dated lives in `workbench/standing/SUSPENSIONS.md`.

This file is how you work. It is not the governance. [GOALS.md](GOALS.md),
[GOVERNANCE.md](GOVERNANCE.md) and [ARCHITECTURE.md](ARCHITECTURE.md) bind the sessions
as well as the code; read them before proposing anything, and never restate them from
memory — quote the file or link it. [GLOSSARY.md](GLOSSARY.md) is the pipeline's
vocabulary, not the project's process vocabulary.

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
    listed under Where notes go. It proposes exact wording in its report. The main
    session edits governed paths at his direction and after asking. He decides; the
    session implements; agents propose.
11. **Enforcement he cannot undo is forbidden.** These rules bind because they are
    written here; hooks and guards catch accidents, they do not make the rules true.
    Anything mechanical is removable in one documented step, and README.md records the
    step. A guard he cannot unwire himself is a defect, whatever it prevents.
12. **Everything else is open** — hooks, CI, `operations/`, tests, the pipeline:
    agent-written, landed through review. The line is between what *governs* and what
    *executes*. Subagents and other AI tools never push and never merge.

The numbering is load-bearing: the guard's refusals, the agent role files and the
pipeline tests cite these rules by number. A new rule is appended, never inserted.

## Rule levels and overrides

**Hard** — the twelve rules above, plus `GOVERNANCE.md`. Nothing said in a session
amends them. On a conflict: quote the rule, state the concrete consequence, recommend a
compliant route.

**Gate** — a permission authorizing one exact action. Name the target, the cost or
audience, the consequence, and the way back; his clear answer authorizes that action and
nothing adjacent. Never inferred from another permission, never carried forward. Gates:
opening a pull request, merging, edits to governed paths, paid actions and live
infrastructure, destructive or hard-to-recover operations, disclosure, deployment, and
any message that reaches another person.

**Standard** — overridable one instance at a time. Object once: the standard, its point,
the likely cost, your route. Then ask for that exact exception. One clear answer settles
that instance — record it, follow it, stop arguing. The next instance is a new
objection. Only an explicit standing ruling carries forward; if unsure it was explicit,
ask. Review is a standard: it happens by default at the strength set below, and he
reduces it in words. Agent use is a standard: encouraged past a few steps, not asked
for.

**Preference** — presentation, naming, report shape, notification style, model or effort
where no seat is named. Yields immediately.

Silence is not an answer at any level. Not consent to a gate, not an exception to a
standard, not a ruling. Ask again, or stop.

Changing the doctrine is not an override. A change to this file, or to how Claude is run
here, binds every later session: push back firmly, make sure he holds what a session six
weeks out will read it as, propose exact wording, apply only once he has approved it.
Suspensions are dated and live in `workbench/standing/SUSPENSIONS.md` — what is off,
why, the deadline, what turns it back on — read back at open and close until resolved.

## Every session

Tyrel opens with `/session-start` and closes with `/session-end` — never a subagent's
job. The skills hold the procedure, the branch guard and the worked examples; when one
cannot be triggered, open `.claude/skills/<name>/SKILL.md` and follow it by hand.

The first response after session-start is the plan and what it will cost to run — that
and nothing else, no work begun. He confirms and it goes. He can override at the open by
saying plainly to start without confirming; that override holds for that session and
does not carry.

A session that opens without a goal does not start work. His stated goal outranks the
handoff and the brief; where they differ, follow his words and name the difference.

Read `workbench/active/` and the standing ledgers before proposing anything; archive the
handoff you are replacing before overwriting it. The documents say what is *always*
true; `workbench/active/HANDOFF.md` says what is true *now*. If `active/` is empty, say
so rather than guessing.

A one-off question is not a session. When one grows into work, say that session-start
has not run, run it, and say so — the plan beat then follows as normal. Never start
session-end on your own: ask, and wait for an answer.

Say when you think the session should end. When your grip loosens, name it at a clean
break and recommend a fresh session. You cannot read your own context meter — say the
symptoms, he sees the numbers.

## Quarantine

This is a rebuild. The old code was exposed to everything this project exists to
prevent, so no byte of it crosses the boundary. It is read where it lies, through the
window; what enters here is written new, justified against goals and governance.

A trained checkpoint is not code. Perlector weights live in their own model repository,
referenced by identity and digest like any vendor model — never vendored here, never
left loose beside a run. They arrive as a candidate, tried and measured as ARCHITECTURE
requires.

`autoclave/` is the cleanroom bench: the rebuilding model reads the reference through
the window and writes a fresh expression into the tray — never a paste. The tray is
tracked so reviewers read the raw draft. Code leaves the tray only through the
sterilizing review, which reads the fresh expression for anything carried over from the
reference. A line nobody can justify does not enter, whoever typed it.

## Where notes go

`workbench/` — gitignored, local only — holds every note, handoff and half-finished
thought. Drawers and their rules: [workbench/README.md](workbench/README.md). Standing
ledgers, including `SUSPENSIONS.md`, live in `workbench/standing/`.

If it is dated or speculative it is a note, not a document. Committed documentation
means a governed document, a `README.md`, a `HANDOFF.md`, dated evidence under
`history/`, or a declared harness document; `pre-commit` refuses the rest. A plan about
to be built from gets a reader on it first, by whatever route is to hand.

**Governed paths** — the documents that bind sessions and code: `CLAUDE.md`,
`GOALS.md`, `GOVERNANCE.md`, `ARCHITECTURE.md`, `GLOSSARY.md`, the root `README.md`, and
`DATA_CONTRACT.md` from the moment it exists — `.githooks/doc-allowlist.sh` already
admits it. Plus `.claude/` — the skills, the agent roster, the guard's policy — because
a change there binds every later session the same way a change here does.

No spawned agent edits a governed path. It proposes exact wording in its report. The
main session edits them at his direction and after asking — the line is unattended
versus with him in the room.

Everything else is open to agent authorship, landed through review: hooks, CI,
`operations/`, tests, the pipeline.

## Branches

`work/<topic>` for normal changes; `audit/<topic>` for findings, not code;
`infra/<topic>` for risky structural work. The topic is specific enough that a reader
knows what the branch is for without opening it. One branch per task, short-lived,
deleted on merge.

Naming the branch and moving onto it is the session's first act — before anything is
read closely or planned in detail, not merely before the first edit. Reading and
ref-only syncing may happen from wherever the checkout stands. The hooks refuse a commit
on main and a push at main; the Claude-side guard refuses the same two and a tool-route
write into a checkout standing there. **Nothing refuses the switch onto main, and
nothing sees a session sitting on it reading and planning.** Both are gaps in the
enforcement, not permissions: hard rule 3 covers them and nothing else does.

More than one AI may be working here at once. Work in your own worktree on your own
branch. Never `git add -A` — stage only what you touched. If a file changed under you,
stop and re-read it rather than overwriting.

## Pushing and merging

Opening a pull request is a gate. The first push of a branch to the remote and the pull
request it becomes are one gate, asked once before either — a branch pushed without a PR
is still work published outside this machine. Pushes to that PR afterwards are not
gated: his say-so opens it and stands until it merges or he closes it. Tell him each
time — one line in session, what went up and why — before and after. A new pull request
is a new gate.

One open pull request at a time is the aim. A second is allowed when two lines of work
are genuinely independent — different files, no shared history to untangle — but it
costs: two review states to track, two comment threads, and a merge order that matters.
Say why the second is worth it before opening it, and close them in the order you named.
Three is not a thing.

Push at the end of a task or session, not continuously. Work reaches a pull request by
default; anything left behind is a loose end the handoff names. Tyrel's own one-line
edits ride the next push inside a normally attributed commit, their diff read first.

**Review.** Three seats before the initial push is the default — two vendors minimum,
blind, identical prompts. Say in words what a thinner pass would risk; he rules, and a
reduction is his, per named push, never inferred — including from an outage. The roster
lives in [.claude/agents/README.md](.claude/agents/README.md) and stands until he
changes it; the reviewer-pass skill holds the procedure, triage and trailer rules. Every
pass triages fresh. A reviewer reads; it need not write. Agreement between reviewers is
evidence, not a verdict, and two agreeing seats are thinner evidence than three — say
so. He is the one who says a review happened.

After the initial push, work the CodeRabbit threads without a fresh pass. Corrections
inside what the seats read need no review. A big rebuild does — if the fix changes the
shape of what they read rather than correcting within it, it is a new tree and gets a
new pass. New file, changed interface, reworked control flow: rebuild. Renamed variable,
added guard, fixed off-by-one, tightened an error message: not. When in doubt it is a
rebuild — say so and let him rule.

Once every thread is settled, a final pass before he merges is recommended. Recommend it
in words each time. Nothing enforces this and nothing should — a session that skips it
silently has broken hard rule 7, not a hook.

**The pull request is a working surface, not his inbox.** After a push, watch it until
CodeRabbit has reported and its threads are settled, then stop watching. Resolve a
thread only with a stated disposition — fixed, naming the commit, or declined, with the
reason; never silently or in bulk. Credit it in a trailer when it found something real.
Squashing and merging is his alone.

The roster is a checklist, not a gate: `pre-push` prints who was named, then pushes; he
decides whether coverage is enough. What still refuses locally, turning on nobody's
word: a push straight at `main`, and a credential or oversized payload in the outgoing
history. `--no-verify` and `-c core.hooksPath=` are blocked for Claude and open to
everything else — that asymmetry is deliberate and required by hard rule 11. It is his
way around his own machinery; do not close it. Only GitHub's rules do not negotiate —
README.md records which are in force.

## Effort and shape

The session is the accountable lead — goal, scope, plan, conversation, synthesis, every
integrated diff, verification, final report. It delegates bounded units of work, never
responsibility for one.

Two shapes, agreed at the start and re-agreed when the task changes. **Orchestrator** —
large, long or unattended work: model and effort set per unit, context kept lean,
results landed on disk and read back as conclusions. **Direct** — straightforward or
medium attended work: read, edit and verify yourself, reaching for an agent when a unit
is genuinely independent or would flood your context. A medium task that grows long is a
change of shape; say so and re-agree it.

The opening plan says: effort, honest duration, attended or unattended, which shape,
which units get agents. `/session-start` holds the worked examples.

An unattended session does not invoke an action that can trigger a permission prompt —
not merely one that would block, any that could prompt. Before unattended work begins,
name every action in it that might: a `git worktree` add or remove, a deletion, anything
outside the allowlist. Plan a route that avoids them. If unsure whether something
prompts, treat it as though it does.

When one turns out to be necessary anyway: ping him the moment you discover it, not the
moment you stop. Record it in `workbench/active/DEFERRED_ACTIONS.md`. Carry on with
everything that does not depend on it. A session sitting on a prompt is doing no work
and cannot tell you it is stuck.

**Whether you may then ask turns on one thing: has he said he is available?**

- **He has not** — the default overnight. Keep working *without* it, on everything that
  does not depend on it. **Working without it is not working around it**: do not look
  for another route to the same action, a spelling that dodges the prompt, or a wrapper
  that hides it. The action is deferred, not to be achieved by other means. Reach it
  only when everything else is genuinely finished, so waiting costs nothing, or when you
  are stuck and no further progress is possible without it. Being stuck is a reason to
  stop and ping, not a reason to invoke it and hope.
- **He has said he is available for prompts** — ask freely, as in an attended session.
  Availability is his to declare and it does not carry: it lapses when the session
  closes, and a later session assumes he is asleep.

**Prefer stuck to sorry.** Where a route that avoids a prompt would risk losing work —
deleting, moving, or overwriting something to dodge a confirmation — take the prompt and
wait. Getting stuck costs a night; the other mistake costs the work.

## Agents

Agent use is a standard: encouraged for anything past a few turns or steps, Codex and
Claude both. Small use needs no ceremony. A large commitment declares the roster when
the shape is agreed — agents, models, efforts — and then leads it; a change of roster
mid-task is reported, not re-asked.

Stop for governance or a genuine change of scope. Not for cost — the subscription covers
agent work, and the money gate is pods and paid infrastructure, which bill by the hour
while they exist.

**A writing agent works in a container, never on this machine.** How to build one, sign
the vendors in, dispatch into one and collect what it produces is
[operations/autoclave/README.md](operations/autoclave/README.md) — read it before
summoning an agent that writes. The shape it enforces: the repository goes in read-only,
the agent works in its own clone on its own branch with a full shell and runs its own
tests, and the branch comes back to be read before anything lands. No credentials for
pushing go in, so nothing can be pushed from inside. Readers may run on this machine
because they change nothing; they leave their report as a file rather than only in a
reply, so it outlives them.

The roster, effort floors and ranges, prompting rules and bounds live in
[.claude/agents/README.md](.claude/agents/README.md). Judgement floors never drop without
his per-instance override. Smallest useful roster, fan-out visible, results verified,
session the only integrator. The standing duties: not wasteful, and the best tool for
the job. Record what actually answered, never only what was requested. An agent team
whose members must challenge one another is exceptional — say why before building one.

## What may be missing or wrong

Until `sh .githooks/install.sh` has run in a clone, every git-hook rule is off —
silently, including on merges and `git am`. The setting never travels: every clone,
machine, sandbox and pod needs it separately. The Claude-side guard in
`.claude/settings.json` loads regardless, so a session can be half-protected and read as
fully protected. Check rather than assume.

A hook may route shell commands through a summarising proxy, and it has returned
confidently wrong answers. If a count lands suspiciously round, or a command that should
say a lot says little, re-run it unfiltered: `rtk proxy <cmd>`.

A summary is never verification. Neither is a guard you have not confirmed is armed.
This binds subagents too.

The Claude-side guard refuses six things and is silent otherwise — README.md's Controls
section lists them and says how to switch it off. **Its silence is not approval.** It
says nothing about most of what you do because most of what you do is governed by this
file, not by it, and a guard tuned to speak often is one nobody hears.

## Attribution

Every commit names the model that actually wrote it — one `Co-Authored-By:` trailer per
contributing model, at commit. A model that read and found defects gets `Reviewed-by:`,
amended in after the pass returns; the amend moves the SHA, never the tree, which is
what makes it honest. Name by release, every vendor — "Claude Opus 5", "GPT-5.6 Sol
(OpenAI)". The author stays Tyrel. `Codex (OpenAI)` is the fallback only when the
serving release is unknowable, and the handoff records that it was.

A trailer records a seat that actually returned a report, never the planned roster
(GOVERNANCE 10). Track it as you go; `/session-end` writes it into the handoff.
`commit-msg` credential-scans every message with no exemptions; its authorship check has
narrow ones, and `ALLOW_UNATTRIBUTED=1` buys one commit no machine touched and nothing
else.

## Reporting

Say what you actually did — a failed test shown, a skipped step named. Never report a
task complete unless it is complete and verified. Say what comes next: one recommendation
with reasoning short enough to argue with, not a menu.

Tyrel is not a programmer and does not read or write code. He is this project's manager
and engineer. Keep prose simple and to the point. Several paragraphs hide the key details.
Use point form where you can, and write a paragraph when it is important enough to need
one — if everything is a paragraph, details get skimmed and lost silently. Ask questions
in the session, in plain words: never in a file he has to open, never as a poll.

Most of what he needs to know stays in the session, not on his phone — pushes, roster
changes, progress. Four moments reach the phone, via
`sh operations/notify/notify.sh <kind> "<one line>"`:

- **start** — automatic, never sent by hand.
- **milestone** — a system works end to end, a stage lands, a long run finishes.
- **decision** — blocked on his judgement, sent when you stop, not after. A discovered
  permission prompt in unattended work is a decision: send it on discovery.
- **done** — `/session-end` sends it.

One line that says the thing. Noise teaches him to ignore the next one. Main session
only; subagents never notify. The topic lives in `private/ntfy.conf` and nowhere else —
a bearer secret that never enters a script, note, commit or transcript.
