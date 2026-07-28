# Next session brief — the overnight orchestrator run

> **Working note.** Written 2026-07-27 by Claude Opus 5 at the close of the harness
> session, from Tyrel's own description of what the overnight run is for. It is a brief,
> not a decision: the canonical documents bind, and where this note and they disagree,
> they win.

## What the night is for

The session runs **as an orchestrator**. It does its work through agents, across both
vendors, choosing model and effort per unit of work rather than per session, and it keeps
its own context lean so it can hold the goal rather than the details. Proving that shape
works *is* the point — the three tasks below are the load it carries while being tested.

**Unattended.** Tyrel is asleep. Nothing may wait on him. A question that cannot be
answered is recorded as a question and the work routes around it.

## Standing constraints for this run

- **No push. No merge.** Both are still denied at the permission layer and that stays.
- **Commit freely, on this branch only.** Commits landed for the first time this session;
  an unattended run that can commit is new, so: `infra/workspace-readiness` or a fresh
  `work/<topic>` branch off it, never `main`, and never a branch another agent holds.
- **GPT-heavy.** Claude's weekly budget is tight for the rest of the week. The default
  worker is Terra, the default judge is Sol. Claude seats are spent where the judgement is
  load-bearing or where the work touches Claude-side machinery only Claude can test.
- **Keep the main session lean.** Delegate the reading. Results land on disk in
  `workbench/raw/<date>_<topic>/` and are read back as findings, not as transcripts.
- **Nothing is lost silently.** Every delegated unit's raw output is filed before its
  findings are acted on — GOVERNANCE 2, and the reviewer-pass skill says it outright.

## The budget, and surviving it

**About 15% of the weekly Claude allowance remains** (Max 20×) at the start of the night.
That is enough for an Opus session at **medium** effort managing GPT agents, and not much
more. Treat Claude as the scarce resource and GPT as the abundant one.

- **Main session: Opus 5, medium.** Not high, not xhigh. The orchestrator's job is
  choosing, dispatching and judging returns — not doing the reading.
- **A Claude subagent is a considered expense, not a default.** Spend one only where GPT
  genuinely cannot do the job: exercising Claude-side machinery (`guard.py`, agent
  frontmatter behaviour, the skills), or a judgement seat where a Claude voice is the
  point. Everything else is Terra and Sol.
- **Context length is the spend.** Every turn re-sends the whole conversation, so a fat
  main context is a recurring charge, not a one-off. Never read a raw transcript into the
  main session. Delegate the reading; have the agent write to
  `workbench/raw/` and report counts and conclusions only.

**Survival rule — the handoff is written continuously, not at the end.** If the allowance
runs out mid-run the session stops wherever it is, and whatever is not on disk is gone.
So: commit after each coherent unit of work, and update `workbench/active/HANDOFF.md`
after each of the three tasks — not once at 6am. A night that dies at 4am with three
commits and a current handoff has lost nothing. The same night with everything held in
context has lost all of it.

**If the allowance starts running low:** stop starting new units, finish the one in
flight, write the handoff, and stop cleanly. Do not attempt the full sweep on fumes — a
half-run sweep that reports "all checks passed" is worse than no sweep, and GOVERNANCE 10
forbids claiming what was not measured.

**The same rule covers the power going out**, which is a live possibility the night this
was written. A power cut is an allowance running out with no warning at all. Two things
follow, beyond committing often:

- **Nothing of value spends the night in a temporary directory.** macOS clears them on
  reboot. See the section below — Tyrel has since relaxed this, and the tray should stop
  being the answer.
- **The scratchpad is not storage either.** Findings, logs and notes go to
  `workbench/raw/` inside the repository, which survives a reboot. `/tmp` does not.

## Where GPT is allowed to write — solve this early

**Tyrel's ruling, 2026-07-27:** GPT may write inside the repository in *designated areas*
when a session is driving it. When he drives it himself he lets it write freely; the extra
care is because a session has less control than he does. His requirement is that **the
prompt or the permissions clearly label where output goes** — and, where a temporary
folder is the only option, that something watches it and clones the output somewhere
durable, or that we follow git to see what it touched. He explicitly left the mechanism
to this session to work out and test.

The constraint that forced the temporary tray was that `-C` does not bound a
`workspace-write` sandbox. That is still true, so **prevention is not available** — but
detection is, and detection is testable in a way a hoped-for sandbox boundary is not.

Recommended, in order:

1. **Write into the repository, into a designated folder, and verify afterwards.** Give
   the seat `autoclave/<system>/` in the prompt, and have `seat.sh` snapshot `git status`
   before and after the call. Anything touched outside the designated folder is a loud
   failure, immediately, naming the paths. This also kills the power-cut exposure, since
   drafts land somewhere that survives a reboot, and it makes every byte GPT wrote
   visible in the diff Tyrel reviews.
2. **Test whether a dedicated git worktree confines the sandbox.** A linked worktree is a
   separate directory whose `.git` is a file pointing back at the main repository — the
   sandbox may well root at the worktree rather than the parent. Untested, roughly twenty
   minutes to find out, and if it holds it gives genuine confinement plus durability plus
   git visibility together. Test it before relying on it, the way `-C` should have been.
3. **The tray plus a watcher.** Tyrel has since asked for this to be **built and tested**,
   not held in reserve — so build it. A script that watches the tray, moves each new
   output into its designated landing area inside the repository as it appears, and
   announces what landed. Building and testing it is not the same as depending on it
   unattended: whichever of the three proves solid under test becomes the operational
   path for the night, and the others stay as tested alternatives rather than theories.
   Ship it with tests, the way `seat.sh` was. Watch for the two failure modes that matter
   — a partially-written file moved mid-write, and the window between write and move
   where a power cut eats the draft.

Whatever is chosen, the seat's designated write location goes **in the seat file**, so a
reviewer reads it rather than inferring it from a prompt.

## Routing — the shape Tyrel described

| Tier | Claude | GPT | For |
|---|---|---|---|
| cheap reader | Haiku 4.5 | Spark | finding things, locating, inventory. Never judgement. |
| general worker | Sonnet 5 | Terra | bounded builds from a written spec, mechanical passes, drafting |
| judgement | Opus 5 | Sol | audits, planning, reconciliation, anything where being wrong is expensive |
| ceiling | Fable 5 | — | rare. See below. |

**Effort is chosen per unit, never inherited.** Claude roles pin it in
`.claude/agents/*.md`; GPT seats pin it in `operations/codex/seats.conf` and
`seat.sh` passes `--ignore-user-config` so the desktop's `xhigh` cannot leak in.

**On Fable.** Tyrel's read — Opus 5 benchmarks in the same band at half the price, so
Fable is not a default. Two exceptions worth the money: a session where the *design is the
deliverable* and nothing exists to inherit (the Archetypus contract is the live example),
and a review seat where Opus already holds one — a second Opus adds little, Fable
disagrees differently. Revisit if Fable is updated.

## What this session learned that the night must respect

Measured tonight, all of it written up in `ORCHESTRATION_FINDINGS.md`:

1. **`codex exec` hangs forever on an open stdin.** It prints "Reading additional input
   from stdin…", spends nothing, and looks exactly like deep reasoning. Two calls were
   lost to this. Always `</dev/null`; always `timeout`. `seat.sh` does both — use it
   rather than hand-typing codex calls.
2. **`-C` does not bound a `workspace-write` sandbox.** The boundary is an ancestor — the
   enclosing git repository. A writing seat inside the tree can write all of it. Writing
   seats run outside the repository (`TMPTRAY`) and the session carries drafts in.
3. **Sol self-orchestrates**, via `collaboration.spawn_agent` and friends, at every
   effort — `ultra` is not required for it. Verified: Sol drove two concurrent Terra
   delegates whose output checked out against ground truth.
4. **A delegate's model cannot be verified from inside the loop.** Sol reports
   `MODEL_CHOICE_HONORED=unknown`. When a run needs the model actually pinned, use
   separate `seat.sh` calls, one per reader, and let the tracked seat line be the record.
5. **Roughly three concurrent delegates** (four agents including the parent). Reported by
   Sol, consistently, but never actually hit — treat as likely, not proven.
6. **The CLI does not validate effort against the model.** Luna accepted `ultra`, which
   its own catalog does not list. Wrong efforts fail quietly; the seat file is the guard.

## The three tasks, in order

### 1. Harden the working repository until it is push-ready

Everything that makes a day-to-day session work correctly: the hooks, the agent files,
`CLAUDE.md`, the skills, CI, the seat wrapper. Bring it to the state where the only thing
left is Tyrel's word — **and stop there.**

Run this as **several looping reviews, mostly GPT-side**, across all the agent types. Two
purposes at once: fix the repository, and find out where the orchestration control
actually breaks. Record the breakages; they are half the value of the night.

The audit design `consult` recommended this session, and which the night should follow:

- **Per-seat calls, not one self-orchestrating Sol.** One tracked seat line and one raw
  log per reader; nothing filtered inside a process where the discards die.
- **Slice so each reader holds a mechanism AND the sentences that claim what it does.**
  Four of the last pass's real findings were documents overclaiming what a hook enforces;
  splitting mechanism from claim puts that whole class in the seam.
- **Add a pass that checks each FIXED row in `DISPOSITION.md` actually landed.** "Did the
  fix survive" is the highest-yield question a re-audit can ask.
- Tee every call to `workbench/raw/<date>_<seat>/` before any disposition is written.

### 2. Re-review and rebuild the rebuild roadmap

Same looping process, same GPT weighting, applied to `RUN_PLAN.md`. The quarantine holds
throughout: old code is read through the window, nothing crosses.

Known-stale in RUN_PLAN already, before the night starts:

- §2.7 attributes Sol as `Codex (OpenAI)`; ruling 6 supersedes it with `GPT-5.6 Sol (OpenAI)`.
- §7 still names `importer`; the agent is `rebuilder`.
- §5 calls Codex self-orchestration unverified; it is verified.
- §5's cascade — a read-only `orchestrate` seat driving Terra *builders* — cannot work as
  written, because a Codex child inherits the parent's sandbox. Either the parent writes
  or the children cannot.
- §5's GPT prices were confirmed against OpenAI's published pricing during this session's
  review; the "unverified" flag on them can be lifted, but confirm it directly.
- §4 decisions **D1 (picker scope) and D2 (personal data)** are still unanswered and gate
  the first rebuild session's dossiers. Do not answer them; surface them.

### 3. Full sweep, then a report in the chat

Re-run everything, confirm the repository still works end to end, then write **a report in
the chat** — not a file, not a pointer to a file. What changed and why, in plain language
for a non-programmer. Then the limitations, then the list of decisions that need Tyrel.

## Workflow templates to design and leave behind

Worked examples for later sessions, not theory.

**A. Converging audit.** Several audit types and several models, run as independent
passes, converging into one output that can be trusted. The hard part is the convergence:
agreement between reviewers is evidence, not a verdict (CLAUDE.md), disagreements are kept
rather than blended (reviewer-pass), and no instrument may set a severity floor
(GOVERNANCE 10) — ask for everything, filter afterwards, in a separate step.

**B. Bulk mapping at scale.** The shape of the 2026-07-26 legacy read, where nearly every
line of the old codebase was read into the staging area. That run went out at Opus high
effort for essentially everything, which is where the optimisation is: cheap readers doing
the bulk, checkpoints so a failure costs one chunk rather than the night, and Opus or Sol
spot-checking and consolidating rather than reading everything themselves. Large but easy
is a different problem from small but hard, and it wants a different template.

Both templates state their own stop conditions, their own budget, and what they cost.

**C. The GPT output watcher** — Tyrel asked for this by name. Not a template but a tool:
the script described under "Where GPT is allowed to write", built and tested rather than
proposed. It belongs in `operations/codex/` beside the seat wrapper, with its own tests,
and it is the piece that makes a confined writing seat usable rather than theoretical.

## What I would have done next in this session

Left undone here, carried into the night:

1. **Harden the six Claude agent files.** The frontmatter surface is much wider than the
   roster uses — `isolation: worktree` (which would make the worker/rebuilder/infra-worker
   worktree rule real rather than asked-for), `maxTurns`, `permissionMode`,
   `disallowedTools`, `skills`, `hooks`, `background`. `memory` stays **off** — Tyrel
   ruled on it this session: it breaks blind review and gives old-code knowledge a way
   across the quarantine inside an agent's head.
2. **Fix the contradictions the GPT review found between the skills and CLAUDE.md** —
   session-start telling the session to wait where CLAUDE.md says to proceed;
   session-end's `ALLOW_UNATTRIBUTED=1` guidance contradicting CLAUDE.md's; session-end's
   "then stop" sitting before two mandatory steps.
3. **Fold tonight's findings into RUN_PLAN** — the stale items listed under task 2 above.
4. **Correct one overclaim in my own findings file:** it says the collaboration tools are
   present at every effort; only `high` and `ultra` were actually tested.
5. **Design the cross-vendor dispatch record.** The gap underneath everything else: there
   is no shared paperwork for a delegated unit — what was sent, what came back, what it
   cost, whether it passed, why that model. Without it "an orchestrator" is a manner of
   speaking. This is the single highest-value thing the night could produce.

## Decisions waiting for Tyrel

Surface these; do not answer them.

1. **D1 and D2** in RUN_PLAN §4 — picker scope, and the personal-data stance.
2. Whether the eventual pull request ships as one review or is split. The branch is
   already ~3,900 lines added against `main`, and the plan's own target is ~600 lines of
   substance per pull request because a bigger diff gets a worse review.
3. Whether `autoclave-empty` becomes a required check on GitHub — his to click, at push
   time, and README's status line only updates once it is actually in force.
