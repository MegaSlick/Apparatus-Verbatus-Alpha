# Decisions waiting on you — 2026-07-28

**How to use this.** Answer by ID. Type into the `ANSWER:` line, or just reply "1 yes, 2 my
own thing, 3 skip". Nothing here is urgent enough to lose sleep over and nothing is
irreversible. Where I have a recommendation I have said so plainly and given the reason,
so you can disagree with the reason rather than the conclusion.

**Read Part 1 and stop if you like.** It is fifteen questions and it is the whole critical
path. Parts 2–5 exist so nothing is lost, not because they need you today.

---

## The one-paragraph situation

Two independent reviewers — one Claude, one GPT, working blind to each other on your
repository — agree on the diagnosis. **The harness is built. The phase is not closing
because nothing has been pushed through it.** Your own RUN_PLAN says Phase 0 "ends in the
`infra/workspace-readiness` push", and that push has not happened, so three further nights
of work have piled onto a phase that already wrote its own finish line and walked past it.
There is no pipeline code yet — the seven stage folders hold a README each.

The pattern underneath it: **the work is an audit loop with no acceptance test.** 99
findings, then 3 more from re-verifying those, then 74, then 62, then a finding contesting
an earlier finding, then 231 extracted decisions. Each round is defensible. The finding rate
never drops because the harness is only ever being measured against itself. The only test
that closes this is putting real work through it.

**Both reviewers independently recommend the same next move: push it.**

---

# PART 1 — The fifteen that actually matter

### T1. Do I have your permission to push the branch?
**Plain meaning:** Nine commits sit on `infra/workspace-readiness`, unpushed. This is not
work, it is permission — you have kept push and merge separate from commit deliberately.
**Recommendation: yes, after T2.** Both reviewers say the harness cannot be validated any
other way, and everything else is downstream of this.
**ANSWER:**

### T2. Do I have your permission to run the reviewer pass on the exact final commit?
**Plain meaning:** Your rules require three blind reviewers on the precise commit being
pushed. This is the only real labour left before the pull request. Asking to push and asking
to review are two separate permissions in your rules; this is the second.
**Recommendation: yes, three reviewers, cross-vendor.** The diff is large and touches hooks
and CI, which your own rules say gets the full set.
**ANSWER:**

### T3. One pull request, or split it?
**Plain meaning:** The branch is roughly 3,900 lines against `main`. Your plan targets ~600
lines of substance per pull request, because a big diff gets a worse review.
**Recommendation: one.** It is a single coherent harness and splitting it now costs days of
rework for a review quality gain you have already bought by running three reviewers.
**ANSWER:**

### T4. Does GPT keep the ability to *write* code in alpha, or does it only read and judge?
**Plain meaning:** This is the big one, and it is where the two reviewers **disagree**.
Tonight I burned hours on where a GPT writing seat may safely save files, and a GPT-written
draft turned out to contain a bug that deletes files while reporting success.
- **Claude says cut it entirely.** GPT reads and judges; Claude writes. That single decision
  deletes the sandbox question, the temp-folder problem, the watcher, its spec, and the
  whole line of work I spent tonight's first hours on.
- **GPT says keep it but fix it** — move writing seats to a proper external worktree and
  harden the wrapper.
**Recommendation: cut it, per Claude.** Not because GPT writes badly, but because the entire
apparatus exists to save Claude budget on drafting, and it has cost several nights and
produced one dangerous draft. Reading and judging is where GPT has actually earned its keep
here — tonight's best findings all came from GPT reading.
**ANSWER:**

### T5. Replace RUN_PLAN, or keep repairing it?
**Plain meaning:** It is 605 lines, has a section marked do-not-follow, and has 57
unapplied findings against it. A newer, shorter plan drafted last night proposes a different
build order. Both reviewers say stop polishing it.
**Recommendation: replace it** with a short first-stage plan plus a decision ledger. Repairing
a document you are one decision away from superseding is the treadmill in miniature.
**ANSWER:**

### T6. D2 — personal data: prevent leakage from commit one, or tolerate-and-note for alpha?
**Plain meaning:** Whether the pipeline refuses to commit anything containing personal data
from the very first commit, or whether alpha tolerates some and notes it. **This is a
conflict with yourself**: you said earlier that "a little bit of personal data leaking in for
now is okay as long as we note it"; the plan now argues the opposite, because the old
repository became permanently unpublishable exactly that way. Neither has been withdrawn.
**Recommendation: prevent from commit one.** You have been burned by this precise failure,
it is far cheaper to enforce at the start than to unpick later, and "we'll clean it before
beta" is what the last repository also assumed.
**ANSWER:**

### T7. D1 — picker scope: narrow or broad?
**Plain meaning:** How widely the "nothing selects among witnesses" ban is applied when
reading old code — 8 files or 35. **The reviewers disagree on whether this is even a real
question**: GPT calls it a fake decision, because the governance rule is absolute regardless
of how many old files you inspect. Claude says answer it broad and move on.
**Recommendation: broad, and treat it as a reading-scope question rather than a rule
question.** The rule does not bend either way; this only decides how much old code gets
inspected for traces of it.
**ANSWER:**

### T8. What sentence marks the infrastructure phase finished?
**Plain meaning:** There is currently no agreed definition of done, which is the root cause
of everything above. Without one there is always another audit worth running.
**Recommendation: "Phase 0 ends when the harness pull request is merged."** Nothing after
the merge counts as infrastructure; it counts as the rebuild. Write it into the README
status line.
**ANSWER:**

### T16. Should any workbench documents be tracked in git so CodeRabbit reviews them?
**Plain meaning:** You asked whether `CLAUDE.md`, the agent files and maybe RUN_PLAN should
be in git so the automated reviewer sees them on the push. **The first two already are** —
`CLAUDE.md`, all six agent files, the roster README, all three skills, `guard.py`,
`settings.json`, the hooks and CI are all tracked today. The only gitignored area is
`workbench/`, which holds handoffs, notes and RUN_PLAN together.
**Recommendation: change nothing for this push.** RUN_PLAN is slated for replacement (T5),
carries a do-not-follow banner and 57 unapplied findings, would need a rule change to commit
at all (`pre-commit` refuses dated or speculative notes), and would add 605 lines to an
already-large diff. Instead: write its short replacement as a tracked document from its
first line, after the merge, and let the reviewer see that.
**Worth knowing:** CodeRabbit is strong on code, generic on prose. The valuable review of
CLAUDE.md and the agent files is your three-reviewer pass, which reads them against your
governance. Where CodeRabbit will earn its keep is `guard.py` — 1,067 lines plus 424 of
tests, named as the clearest over-engineering in the repository.
**ANSWER:**

### T17. Track `workbench/active/` in git for alpha?

> **Status, 2026-07-28: the branch already tracks it** — `git ls-files workbench/active`
> lists every file below, and the two edits named in this entry were made. Read what follows
> as the reasoning behind an act already taken, not as a pending question. What is still
> yours to answer: whether it stays tracked, and whether it rides in this push or a separate
> pull request. Everything in the drawer is a published claim while it is tracked.
**Your reasoning:** a spot check on new sessions and plans, cleaned regularly so nothing
goes stale, and recoverable if a session deletes something it shouldn't have.
**What I found when I tested it — with a correction from Tyrel:** the allowlist's
`*/HANDOFF.md` rule would let a session handoff through, but that rule was written for the
**pipeline stage** handoffs (`pipeline/1..7/HANDOFF.md`), which declare what a stage writes.
A session handoff is an admin continuity note — a different artefact that happens to share a
filename. Matching it would be exploiting a pattern, not satisfying a rule. **See T18.** The
other seven files are refused: RUN_PLAN, PRE_REBUILD_INTENT, NEXT_SESSION_BRIEF,
ORCHESTRATION_FINDINGS, CHANGES_TONIGHT, this file, and reviews-2026-07-27/DISPOSITION.
Tracking them needs two edits — `.githooks/doc-allowlist.sh`, and CLAUDE.md's rule that "if
it is dated or speculative it is a note, not a document". The secret scanner passes clean on
all of it.
**Recommendation: yes, with three adjustments.** And a stronger argument than the one you
gave: the entire workbench currently exists in one place on one disk. For a project whose
first principle is that nothing is lost silently, the notes recording *why* every decision
was made are the least protected thing here.
  1. **Land it as its own pull request, right after the harness one.** 148KB of prose mixed
     into the harness diff means reviewers read handoffs instead of hooks.
  2. **Secrets yes, personal data no.** Tyrel's ruling 2026-07-28: alpha tolerates personal
     data, beta cracks down on it, secrets stay strict throughout. So the secret scan is the
     gate here and it already passes. This no longer waits on T6.
  3. **`active/` only — never `raw/`.** 2MB of engine transcripts, huge churn, no review value.
**Also worth knowing:** your distribution rule says 1.0 is a fresh clean export with new
history, not a visibility flip — so committing working notes now does not mortgage the
public release.
**ANSWER:**

### T18. Rename one of the two things called "handoff"?
**Plain meaning:** Two unrelated artefacts share the name. `pipeline/<stage>/HANDOFF.md`
declares what a pipeline stage hands to the next stage — a permanent technical document.
`workbench/active/HANDOFF.md` is the admin note one working session leaves the next. The
collision already caused a real error: the document allowlist permits `*/HANDOFF.md` for the
pipeline sense, and it would silently admit the session sense too.
**Recommendation: rename the session one.** The pipeline sense is the older, more technical
meaning and it appears in seven stage folders; the session sense appears once and is read
only by sessions. Something like `SESSION_NOTE.md` or `CONTINUITY.md`. This touches CLAUDE.md,
both skills, `tidy.py` (which protects `HANDOFF.md` by name) and the allowlist — small, but
it must be done in one pass or the protection breaks.
**Do not do this before the harness merges.** It touches files in the pending pull request.
**ANSWER:**

### T19. The turn caps I added to the agents are too low — raise them or drop them?
**Plain meaning:** Last night I added a `maxTurns` limit to each of your six AI helper roles,
so none could run away and burn a night's budget in a loop. It sounded prudent. Today the
auditor's limit of 40 **silently truncated a full review** — the agent did 236,000 tokens of
real work across 54 steps, hit the cap, and returned one sentence. No error, no warning; it
looks exactly like an agent that finished and had nothing to say.
**What it affects:** every review, audit and second opinion. A cap that cuts off the report
at the end destroys the whole run rather than shortening it — the same shape as the timeout
problem you spotted earlier.
**Options:**
  - Raise the caps substantially (auditor 40 → 150, consult 30 → 100, scout 15 → 40).
  - Drop `maxTurns` entirely and rely on cost visibility instead.
  - Keep low caps but require every agent to write its output to a file incrementally, so a
    truncation degrades the result instead of deleting it.
**ANSWERED — Tyrel, 2026-07-28: drop turn limits; use a time limit the agent is told about.**
"I am not a fan of that. I think a time limit that the agent knows is better than a turn
limit." Rationale: a turn cap is invisible to the agent and cuts it off mid-thought; a
deadline it knows about lets it triage and reserve time to report.
**Constraint to know:** Claude agent files have no timeout field — only `maxTurns`. So the
deadline lives in the *prompt* for Claude agents, and in `seat.sh` for GPT seats. Being done:
`maxTurns` removed from all six roles, deadline stated in prompts, plus incremental
file-writing so any cut-off degrades rather than deletes.

### T20. `notify.sh`: your phone notifications — hold the change back, or set up the topic first?
**BOTH reviewers flagged this independently as the most consequential item.**
**Plain meaning:** Sol rewrote the notification script so it refuses to read
`private/ntfy.conf` and takes the topic only from the environment. Nothing on your machine
supplies it. Merged as-is, your `start` and `milestone` pings go dead **silently** — clean
exit code, no error. Opus also noticed Sol edited the CLAUDE.md rule saying the topic lives
in that file, so its own change would comply: the rule was moved to match the change.
The security reasoning is genuinely sound and the new client is well built (20 new tests, the
topic never reaches curl's arguments, delivery claimed only on a real 2xx).
**Options:**
  - Hold the whole notify group back for a second push; keep today's working notifications.
  - Rotate the topic and choose how it reaches the process (keychain, launcher, shell profile)
    **before** merging.
**ANSWERED — Tyrel, 2026-07-28:** "We can hold it back or put it in secrets or personal or
something where it is gitignored." A gitignored file is acceptable — `private/` already is one.
So Sol's premise was wrong: it refuses the gitignored config outright. **Keep its security
work, restore the file as the default source, env var as override. Ships only after one real
phone test.**

### T21. The push gate now hard-codes three exact model names — keep, or loosen?
**Plain meaning:** the pre-push check now only counts reviewer receipts labelled exactly
"Claude Opus 5", "Claude Fable 5", "GPT-5.6 Sol (OpenAI)". It also reversed your existing
rule that a receipt names *the model that actually answered* rather than the one requested.
**Why it matters:** today's product names are baked into a safety gate. At the next model
release a renamed or substituted reviewer forces either an override flag or a mislabelled
receipt — and the honest-label rule was your measurement-honesty principle, changed by an
agent rather than by you.
**Options:** keep exact labels · count any three distinct reviewers and record the resolved
release · revert the honest-label reversal only.
**ANSWERED — Tyrel, 2026-07-28:** "These should not be hard coded. We add the contribution or
reviews to the actual model names that do it and have Claude as co-author. It should be a
checklist at push." **Literal model strings come out of `pre-push`; it counts three distinct
reviewers and records the model that actually answered; a checklist at push time.**

### T22. A new hard rule 10 appeared in CLAUDE.md, written by an agent — bless it or strike it?
**Plain meaning:** Sol added a tenth hard rule: no secret in any file, including ignored notes
and evidence. The wording is good. But it was written by an agent, and it immediately
condemns two evidence logs in your own `workbench/raw/` that contain credential-shaped
strings — which means it silently decides the evidence-versus-secret question that Sol
elsewhere correctly reserved for you.
**Options:** adopt as written · adopt with an explicit carve-out for sealed evidence · strike
it and decide separately.
**ANSWERED — Tyrel, 2026-07-28:** "Agents can suggest changes to Claude files, to gov files
and other critical infrastructure files but never change them." **So hard rule 10 is NOT
adopted — it becomes a proposal, along with every other Sol edit to CLAUDE.md, which is now a
protected file.** **Scope ruled the same day: rules documents only** — CLAUDE.md plus the five canonical
documents. Hooks, CI, agent files, skills, operations and tests stay open to agents and land
through review. The line is what *governs* versus what *executes*.

---

# PART 2 — Where the two reviewers disagreed

Kept unblended on purpose — your rules say agreement between reviewers is evidence, not a
verdict, and averaging disagreement away destroys information.

**Is the repository honestly finishable this week?**
Claude: yes — "almost nothing" must be finished first; push this week.
GPT: no — "calling this finished today would be false", and it lists real defects first
(review-only session closing behaviour, audit-gate weaknesses, the installer reporting
success it did not achieve, tag deletion, remaining document-vs-mechanism contradictions),
plus workbench cleanup.
**My read: GPT is right about the defects and Claude is right about the priority.** None of
GPT's defects can cause harm before the merge, because they are all guards on a repository
that currently contains no pipeline code. Fix them in the first post-merge session.

**GPT writing seats** — covered at T4. This is the substantive fork.

**D1** — covered at T7. One reviewer thinks the question is malformed.

---

# PART 3 — Already answered. Do not decide these again.

**The eleven "queued rulings" are answered.** You answered all eleven in session on
2026-07-27. They are recorded in the same file that queues them, thirty lines below the
list — `workbench/active/reviews-2026-07-27/DISPOSITION.md`, queued at :73, answered at :137.

I got this wrong earlier tonight and told you they were open. So did a GPT review seat, and
so did a summarising agent. Three readers, same error: all read the question and none read
to the end of the file. That is roughly a third of the "outstanding" list evaporating, and
it is the clearest single example of why this phase feels endless — **effort was being spent
re-surfacing decisions you had already made.**

Also already handled, tonight, needing nothing from you: the Temp_Stage write-access hole is
closed; the naming and `rebuilder` corrections are applied; two fixes recorded as done but
missing are now genuinely done.

---

# PART 4 — The question bank: 231 items, and why you should not read them

Four readers extracted 231 decisions from your repository and the old-code analysis. Almost
all of them are **pipeline design** — how to count a hard failure when several models read
one act, whether iPhone photos are admitted, what happens to an official reading when the
system re-reads an act, where a human correction sits relative to the machine reading.

They are real questions. **None blocks closing infrastructure**, and most cannot be answered
well until the stage they concern is actually being built.

**Recommendation: answer them per stage, at the moment that stage is built — never in
advance.** Both reviewers independently said the same: stop trying to adjudicate hundreds of
old-code questions before their behaviour is relevant. They are filed in
`workbench/raw/2026-07-28_decisions/` and organised by stage, and they will be waiting.

**T9. Do you accept that policy — decide per stage, just in time?**
**Recommendation: yes.**
**ANSWER:**

---

# PART 5 — Things only you can do (not decisions, actions)

- **T10. Rotate the ntfy notification topic.** It is in cleartext in two files in the old
  staging area and hardcoded at five places in the old repository. Anyone holding the string
  can read your notification stream. Nothing was published — this is local — but it is a
  bearer secret and it is burned. **ANSWER:**
- **T11. Re-point the Desktop launcher.** `~/Desktop/Parish OCR Pipeline` shadows your real
  repository, so double-clicking the launcher runs a copy frozen 108 commits ago — missing
  the budget guard rewrite, the bad-config stop, and two other money-and-shutdown
  protections. It has been run at least once. **This is the one that can cost real money on
  the next pod launch.** Preserve its receipt file before changing anything; it is the only
  record tying that export to a commit. **ANSWER:**
- **T12. Flip `autoclave-empty` to a required check on GitHub** — after the push lands. One
  click. You already approved this in principle (ruling 3). **ANSWER:**
- **T13. The licensing email** to the handwriting-witness publisher. Costs nothing, takes
  weeks to come back. **ANSWER:**
- **T14. Delete two empty probe folders** at `/Users/tyrel/Temp_Stage/.verbatus-probe-plain`
  and `.verbatus-probe-repo`. I have no delete permission. **ANSWER:**

---

# The biggest risk nobody has been looking at

Not a decision — something both of us missed until tonight, and it deserves your attention
more than most of the above.

**The quarantine workflow has never been run once.** `autoclave/` contains one README. No
draft has ever been written into it, checked, and moved into the tree. The `rebuilder` agent
has never run. Yet roughly 62 old files are marked "worth rewriting with the old file open
beside you" — 62 passes through a procedure with zero trial runs, at the one seam where a
contaminated line is invisible afterwards, because it looks like new code.

Everything else in this harness has been tested hard. The seam the entire rebuild runs
through has not been tested at all.

**Recommendation: the first post-merge session rebuilds one small, boring file end to end** —
through the tray, through the check, into the tree — purely to prove the path works while
the stakes are low. Not the interesting file. The dullest one you have.

**T15. Agreed?**
**ANSWER:**

---

## If you only answer three

**T1** (push), **T4** (does GPT still write code), **T6** (personal data). Everything else
can follow those.
