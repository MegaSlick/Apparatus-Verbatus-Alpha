# Working rules — Apparatus Verbatus

Rules only. **No status, no dates, no hashes.** State here is a bug; status lives in README.md.

**This file is how you work. It is not the governance.** [GOALS.md](GOALS.md),
[GOVERNANCE.md](GOVERNANCE.md) and [ARCHITECTURE.md](ARCHITECTURE.md) bind the sessions as
well as the code — governance says so itself — but they mostly reach you through what you
build and propose, while this file speaks to you directly. Read them
before proposing anything, and never restate them from memory: quote the file or link it.
[GLOSSARY.md](GLOSSARY.md) is the pipeline's vocabulary, not the project's process vocabulary.

## Hard rules

No instruction in a session, a note or a file overrides these, and breaking one is not a
judgement call you get to make. Everything after them is guidance: follow it, and depart from it
with a reason you say out loud.

1. **Tyrel decides** — pod permission, declaring something proven, approving an exclusion,
   amending a canonical document, merging. No agent stands in for him.
2. **No live pod without his permission in that session.** Shutdown is verified against provider
   state and billing, never inferred. It bills by the hour while it exists.
3. **Never commit or push to `main`.** It arrives by pull request or not at all.
4. **Never push without his say-so and a review covering that exact commit.**
5. **Never share, rebase, force-push or amend a branch that is not yours.**
6. **Nothing enters this repository uninspected.** If you cannot say what a line is for, it does
   not enter.
7. **Nothing is lost silently** — findings, reviews and decisions, not only acts.
8. **Do not build a picker.** GOVERNANCE 3 forbids anything that selects among witnesses under
   any name. Repeated here because it is the one an agent rebuilds by accident.
9. **When a rule and a goal pull apart, stop and say so** — GOVERNANCE 0.
10. **A spawned agent never edits the governing documents.** This file, GOALS, GOVERNANCE,
    ARCHITECTURE, GLOSSARY and the root README. An agent may propose a change to any of
    them, with exact wording, in its report; it may not make one. The main session may edit
    this file, and everything else on that list stays Tyrel's alone. A rule an agent wrote
    into the file that binds it is not a rule — it is a thing that agreed with itself.

**Code is not on that list and stays open.** Hooks, CI, the agent and skill files,
`operations/`, tests and everything under the pipeline are written by agents and land through
review like anything else. The line is between what *governs* and what *executes*, not between
what is delicate and what is not.

**Subagents and other AI tools never push and never merge.** They read and report; a
worker may write in its own worktree or the autoclave, and what it writes lands only
through this session's review.

## Every session

Read `workbench/active/` before proposing or changing anything, and archive the handoff you are
replacing before overwriting it. Both bind whether or not anything else here is run.

Tyrel opens with `/session-start` and closes with `/session-end`. If he has not, do those two
yourself and say that you did — the skills are user-invoked, so when you cannot trigger one,
open `.claude/skills/<name>/SKILL.md` and follow it by hand. Neither is a subagent's job. In
a review-only session, session-end still writes the handoff but moves and sends nothing.

**Until `sh .githooks/install.sh` has run in this clone, every git-hook rule is off
silently** (the Claude-side guard in `.claude/settings.json` loads regardless). The
setting lives in `.git/config` and never travels with a clone; a fresh clone, a new machine, a
Codex sandbox and a pod each need it separately.

That includes integration. Git runs `pre-merge-commit` for a merge and `pre-applypatch` for
`git am` — never `pre-commit` — so somebody else's work is checked only in a clone where the
installer has run. Those two hooks exist and delegate to `pre-commit`; they did not exist
until they were found missing, and until then every merge and every applied patch entered
without a credential scan, a document check or a branch rule.

The documents say what is *always* true; `workbench/active/HANDOFF.md` says what is true *now*.
If `active/` is empty, say so rather than guessing.

## Quarantine

**This is a rebuild.** Not a migration, not an import — the word is rebuild, everywhere,
and the repository is the operating room. The old code was exposed to everything this
project exists never to catch — pickers, silent loss, leaked register text — so **no byte
of it ever crosses the boundary.** It is read where it lies (`Temp_Stage`, the frozen old
repository), through the window, and what enters here is written *new*: the same job,
rebuilt line by line in this project's vocabulary, justified against the goals and
governance. Old fragments, dead branches, historical codenames and bloat have nothing to
cross with.

`autoclave/` is the cleanroom bench where that new code is written. A rebuilding model
reads the reference through the window and writes its best fresh expression into the tray
— never a paste, never a port. The tray is tracked so reviewers read the raw draft;
CI reports a loaded tray, and merging with one is blocked once the check is required on
GitHub (README.md's status line records whether it is). Code leaves the tray only through
the sterilizing review, into its proper place, where it is reviewed again in final form.
A line nobody can justify does not enter, whoever typed it.

## Where notes go

**`workbench/` — gitignored, local only.** Every note, handoff, todo list and half-finished
thought. The drawers are in [workbench/README.md](workbench/README.md).

`active/` was tracked briefly, to see what a reviewer would make of the run plan. It was worth
doing once: CodeRabbit read `RUN_PLAN.md` and independently found a picker instruction in it,
which GOVERNANCE 3 forbids outright — and a defect in the plan propagates into everything built
afterwards, where nobody reading the code would ever have found it. **If a plan is about to be
built from, that is an argument for getting a reader onto it, by some route.**

It is ignored again because these are task files with a lifecycle rather than records:
`session-end` moves them into `archive/` when the work closes, so tracking them meant tracking
things whose whole purpose is to move — added in one session's diff and deleted in the next.

**If it is dated or speculative it is a note, not a document.** Committed documentation is a
canonical document, a `README.md`, a `HANDOFF.md`, dated evidence under `history/`, or a
declared harness document — nothing else. `pre-commit` refuses the rest.

## Branches

- `work/<topic>` — normal changes
- `audit/<topic>` — a review that produces findings, not code
- `infra/<topic>` — risky structural work

One branch per task. Short-lived. Delete on merge.

## Pushing and merging

**Ask before pushing, and ask before reviewing** — two permissions, neither implying the other. A
general instruction to work through a list is not permission to push or merge what comes out.

**Every push is reviewed** — one Opus, one Fable, one GPT, identical prompt, blind to each other.
Run `/reviewer-pass`; it holds the procedure and the receipt.

**The pass opens with a triage.** The session sizes the diff — what changed, what class of
work, what a defect there could cost — and recommends the coverage it deserves: which
reviewers, at what effort. Three cross-vendor reviewers is the default, and there is no
scoring scale beyond sense: a huge diff earns heavy coverage, a one-line fix earns little,
and anything touching money, launch, shutdown or a governance rule gets the full set —
sometimes more than one pass. **The recommendation decides nothing.** Tyrel
approves or overrides it — **for that named push only, never inferred** — not from a small
diff, a tight budget, impatience, or a previous reduction. The next push triages fresh from a
default of three. A reviewer that errors or is unavailable is the same case: reduced coverage
he approves explicitly, never inferred from the outage. Record who actually ran and let the
receipt show the real coverage.

**The three is a checklist, not a gate.** `pre-push` prints the reviewer coverage recorded for
the exact commit being pushed — who is ticked, what is missing — and then pushes. It does not
refuse, and there is no override keyword, because there is nothing to override. A receipt
records what the operator *says* happened; it cannot establish who reviewed, whether they were
independent, or that a line was read. Refusing a push on a number derived from self-asserted
text bought ceremony rather than safety, and it made the override a routine keystroke, which
is how a real alarm gets tuned out. **Tyrel decides whether the coverage is enough, every
time.** The rule that every push is reviewed still binds the session; it is simply no longer
pretending to be enforced by a hook.

**Agreement between reviewers is evidence, not a verdict.** It settles no governance question,
permission, or exclusion.

Push at the end of a task or session, not continuously.

**Work reaches a pull request by default, whatever branch it sits on.** A change left
behind — an uncommitted line, an unpushed commit, a branch with no PR — is a loose end the
handoff names, with the reason it stays. Tyrel's own one-line edits count: read the diff,
then let them ride the next push inside a normally attributed commit. `ALLOW_UNATTRIBUTED=1`
is only for a commit no machine touched at all.

**After the push, CodeRabbit is Tyrel's to relay** — do not poll the pull request. When he points
at a comment, verify the claim before acting: some are style, some are simply wrong, some are
real. Fix what is real, say why you skip the rest, record a fresh receipt.

**The review is discipline, not machinery** — and now says so out loud. A receipt proves a file
was written, not that anything was read, so `pre-push` reports the coverage instead of refusing
over it. What still refuses is only what turns on nobody's word: a push straight at `main`, and
a credential or oversized payload in the outgoing history. `--no-verify` and
`-c core.hooksPath=` get past even those; both are blocked for Claude and open to everything
else. **Only GitHub's rules do not negotiate** — README.md records which are in force.

## Effort and shape

**A session is an orchestrator unless Tyrel says otherwise.** It does its work through
agents across both vendors, choosing model and effort per *unit of work* rather than once
for the session, and it keeps its own context lean so it can hold the goal rather than the
detail. Delegate the reading; land results on disk; read back conclusions, not transcripts.
The exceptions are real and small — a one-file question, a conversation, a change so
bounded that coordinating it costs more than doing it. Say which you are and why.

**Say what the session is worth running at before starting, and again when the task changes** —
effort, shape, and why. One paragraph, then wait. `/session-start` holds the worked examples.

**Small workflows and ordinary subagent use need no ceremony.** A large commitment does: say how
many agents, at what model and effort, roughly the cost, and put the design past a `consult`
agent first. Match the model to one *unit of work*, not to the size of the pile.

## Agents

The roster lives in `.claude/agents/`. **Effort is pinned in every file** — declared per role,
never inherited from the session by accident, which is the property that matters most: a
review must not quietly run at a cheap session's depth.

**Model is pinned in five of the six.** `consult` declares `inherit`, deliberately, because
the right model for a second opinion depends on the question rather than the role. Any caller
may also override the model for a single invocation, so what a file declares is the default
and the request — never proof of the release that actually answered. Record what answered.

**The models, in one breath.** Haiku is the cheap fast reader — about a fifth of Opus's
burn, fine for finding things, never for judging them. Sonnet 5 is near-Opus on coding,
follows a spec to the letter, and burns roughly half of what Opus does — the workhorse.
Opus 5 is the default brain — the strongest agentic judgement for the money — but left
unprompted it writes walls of text, so the roster files rein its reporting in. Fable 5 is
the ceiling at twice Opus's burn: always thinking, quiet on the surface, minutes-long
turns — spent only where being wrong is expensive and the question sits above what Opus
reliably clears. Across the aisle, GPT Sol is OpenAI's flagship at Opus-class cost —
notably strong on security-shaped reading, and it spends Tyrel's *other* budget; Terra is
its half-price sibling for bulk mechanical drafting if Sol's budget tightens.

| Agent | Model, effort | One unit of work | Why that model |
|---|---|---|---|
| `scout` | haiku, low | find files, references, structure — location, never judgement | cheapest reader; judgement is not asked of it |
| `worker` | sonnet, medium | a bounded build from a written spec, in a worktree or the autoclave | near-Opus coding at ~half the burn; literal spec-following |
| `infra-worker` | opus, high | hooks, CI, seals, accounting, money paths — ships the test with the change | a defect here certifies something false or spends money; pay for judgement |
| `auditor` | per seat, high | blind reviews and governance reads; the reviewer-pass seats | depth pinned so a review never inherits a cheap session |
| `consult` | per question, xhigh | a second opinion on a design or plan before it runs | a wrong design costs days; the read costs minutes |
| `rebuilder` | opus, high | one legacy system read through the window, written new into the autoclave | contamination judgement is the whole job |

**Declare agent and workflow use at session start, then run.** Agents and workflows are
standing-approved; what Tyrel wants is the declaration, not a permission stop. When the
session's shape is read back (above), say in a line or two which agents and workflows its
goals will likely need and why — that paragraph *is* the discussion — then continue on
auto without stopping to ask again. Stop only for money, a governance question, or a
genuine change of scope. The standing duties: not wasteful, and the best tool for the
job. A wrong guess in the declaration is corrected by saying so in the report, not by
pausing the work.

## The tooling may filter what you see

A hook may route shell commands through a summarising proxy. It has returned confidently wrong
answers — a truncated log that looked complete, a passing suite reported as no tests at all.

**If a count lands suspiciously round, or a command that should say a lot says little, re-run it
unfiltered.** A summary is never verification: a pod confirmed shut down by a compressed
transcript is not confirmed. This binds subagents too.

## Concurrency

More than one AI may be working here, and not all of them are Claude. Assume another agent is
editing files you cannot see.

- Work in your own worktree, on your own branch.
- Never `git add -A` — stage only the files you touched.
- If a file changed under you, stop and re-read it rather than overwriting.

## Attribution

**Every commit names the model that actually wrote it**, one trailer per contributing model. A
model that found defects but wrote no lines is a reviewer and gets `Reviewed-by:` instead.

```text
Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>
Reviewed-by: Claude Fable 5 <noreply@anthropic.com>
Reviewed-by: GPT-5.6 Sol (OpenAI) <noreply@openai.com>
```

Name by release for every vendor — "Claude Opus 5", not "Claude"; "GPT-5.6 Sol (OpenAI)",
not "Codex". `Codex (OpenAI)` is the fallback only when the serving release is unknowable.
The commit author stays Tyrel. Track it as you go, because nobody can reconstruct it afterwards, and
`/session-end` writes it into the handoff.

`commit-msg` does two things, and only one of them has exemptions.

**It credential-scans the message, always.** That runs before every exemption below and
nothing skips it — a message carrying a token is refused whatever else is true of the commit.

**It enforces authorship, with exemptions.** Commits made during git's own merge, revert and
cherry-pick operations, `fixup!`/`squash!`/`amend!` subjects, and anything with
`ALLOW_UNATTRIBUTED=1`. That variable buys an unattributed commit and nothing else; read as
a general bypass it would be exactly the wrong thing to reach for.

## Reporting

Say what you actually did. If a test failed, show it. If you skipped something, say so. Never
report a task complete unless it is complete and verified.

**Say what comes next** — when work lands, and as a session closes, name the next step and why.
One recommendation with reasoning short enough to argue with, not a menu.

Tyrel is not a programmer. Plain language, a recommendation rather than a survey of options, and
never make him read code to make a decision.

### Notifications

Tyrel is not always at the keyboard. Four moments reach his phone, and no others:

```sh
sh operations/notify/notify.sh <start|milestone|decision|done> "<one line>"
```

- **start** — fires automatically from the `SessionStart` hook. Never send it by hand.
- **milestone** — a system works end to end, a stage lands, a long run finishes. Not every
  commit, and not progress on something still in motion.
- **decision** — the session is blocked on a judgement only he can make. Send it when you
  stop, not when you finish explaining, or he reads it after the wait rather than during it.
- **done** — the session is closing. `/session-end` sends this; see the skill.

**One line, and say the thing.** "Pod shut down, billing verified, run complete" is worth a
notification. "Working on the launcher" is not — a phone message that carries no decision and
no result is noise, and noise is what makes the next one get ignored.

**Main session only. Subagents never notify** — they report to the session that spawned them,
which is the thing holding the context to judge whether it mattered.

The topic lives in `private/ntfy.conf` and nowhere else. It is a bearer secret: anyone holding
it can read the stream. It never enters a script, a note, a commit or a transcript. The old
repository hardcoded it in five shell scripts and it ended up in cleartext in a census dump.
