# Framework redesign — what is settled

The reconciliation record for `2026-08-01_framework-redesign-plan.md`. That plan was not
endorsed by either verification seat; this file records what has been decided since, so the
sixteen open questions stop being re-derived from five drafts that disagree.

Three states, and nothing here blurs them:

- **Ruled** — Tyrel decided it, in his words, in session.
- **Session decision** — the session decided it on stated reasoning, standing until he
  objects. Not his ruling and never recorded as one.
- **Open** — his, unanswered.

The five `proposed_*.md` drafts in `workbench/design/system_coherence/` are input, not law.
Where this file and a draft disagree, this file is later and wins; where this file is silent,
nothing is settled.

---

## Ruled

### R1 — Agents keep `operations/`; they never touch the rules or the guard

Agents may write to `operations/`. The autoclave is the staging ground for agent-written
code today and remains so.

Agents may not touch `CLAUDE.md`, the guards, or the governing documents, and do not open
pull requests. That is the main session's work.

*Consequence.* `proposed_enforcement.md` F4 would have added `operations/` to the roots no
permit may name, which would have locked every agent out of the notify script, the Codex
wrappers and the off switch. That is struck. Hard rule 12 stands as written: what governs is
closed, what executes is open.

*This also closes the split from the `fd41c84` review pass.* Three seats read the guard's
`.claude/` widening and divided — Opus called it a high-severity defect because it locks the
infrastructure role out of the guard's own code; Fable and Sol called it Tyrel's decision
correctly implemented. Fable and Sol were reading it right. The lockout is intended.

*Live question this leaves.* The infrastructure role exists to build hooks, CI and the guard.
Under R1 it cannot write the guard, so either that work is the session's own from now on, or
the role's purpose narrows to `operations/` and the pipeline. Named in the open list as O5.

### R2 — Agents work inside a container, and readers hand back files

Writing agents get an isolated environment with full capability — a shell, the
ability to install what a task needs, and the ability to run their own tests. Work
returns as a change set that is inspected before it becomes a pull request. More than
one at a time, on separate branches. Both Claude and Codex must run in it.

Readers must be able to leave output somewhere that does not fill the main session's
context and that outlives the agent. Answered by the same chamber: a reader writes to
`/out/report.md` and the file is collected. No change to any governed path is needed
for it, which is why it was done this way rather than by granting readers a write tool.

*Consequence for O1.* The question "does a writing agent lose the shell" was the wrong
shape. On the host it loses every write tool as well, so there is nothing left to police
by guessing at command spellings. Inside the chamber it loses nothing. Both halves are
stricter and more useful than the permit the corpus drafted.

*Consequence for S3.* The permit survives but shrinks. It stops being an advance list of
paths checked on every write, and becomes a declared scope checked once against a diff
that actually exists.

*Local merges are not GitHub merges.* A branch collected from a chamber is merged
locally on his judgement. The review ladder and the merge rules govern what reaches
GitHub; they do not govern moving work from a clone into the working repository.

### R3 — `dagger/container-use` is not ruled out

Hard rule 11 was cited against it and the citation was wrong: that rule is about
enforcement he cannot undo, not about tooling. container-use is a working tool.

Vetted: 3,924 stars, 199 forks, Apache 2.0, not archived. Last code push seven weeks
before this was written, 84 open issues, and still self-badged Experimental.

Not adopted first, on those last three facts alone — not on the rule. It needs a
container engine underneath exactly as the hand-built chamber does, so installing that
engine serves both routes and nothing is wasted by starting simple.

---

## Session decisions

### S1 — The short version, and the labels step is not optional

Three pull requests, four to six sessions, roughly five decisions — the shape both the
migration seat and the proportion seat reached independently, plus the permanent-label step,
which the plan floated as a droppable addition.

*Why the label step cannot be dropped.* The corpus found that shipped pipeline code already
cites architectural invariants by position — five places in the part of the tree everyone
assumed was healthy, and the governance seat then found at least four more that a hand count
had missed. Insert one invariant and working code silently re-aims. That class of coupling
scales with the pipeline, not the framework, and the pipeline is heading for a hundred
thousand lines with three vision models and a self-assembling deployment. Hand-counting was
already failing at 6,234 lines.

*Correction to what the session said earlier in this sitting.* It argued the labels should
therefore move early. Within a three-pull-request sequence that is wrong: the proportion seat
puts labels last because the guard rewrite in PR 2 deletes 28 of the 46 position-citations
first, so labelling afterwards is materially less work, and the pipeline will not have grown
between two pull requests days apart. Scale makes the step **mandatory**, not early. Order
unchanged.

### S2 — The acceptance test is struck

The design's own criterion — "the framework is smaller than the pipeline, measured" — is
withdrawn wherever it appears (`SYNTHESIS.md` §15 and the "1.75× the building" framing in
`INVENTORY.md` and the handoff).

Against today's walking skeleton it cannot pass; against the intended pipeline it passes
automatically without anyone doing anything. It measures nothing in either direction and
would read as reassurance.

*What replaces it.* Three things that track what is actually paid: decisions demanded of
Tyrel per session, lines read before work begins, and whether a rule and its restatements can
drift apart without anyone noticing. The ten-line size reporter in PR 1 still lands — a
measured number beats a guessed one — but it reports, it does not certify.

*What this does not rescue.* Two size findings survive untouched, because neither is a ratio
argument. The plan promises to add ~700 lines where the drafts specify 1,900–2,300, landing
honestly at 8,300–8,700. And the governing prose does not shrink at all: 807 lines today
against ~800 proposed, where 630 was promised, with the one document anyone actually drafted
overshooting its estimate by 85%. Every estimate that has been tested was wrong in the same
direction. The choice of the short version does not rest on size — the two routes land within
about 400 lines of each other — it rests on twenty-five decisions against five.

### S3 — One permit specification: the enforcement draft's, amended by R1

`proposed_enforcement.md`'s version stands. Python, keyed by task, at
`private/permits/<task>.json`, five fields: task, agent, base commit, writable paths, expiry.
`proposed_lifecycle.md`'s shell, profile-keyed, eight-field version is withdrawn.

*Why.* The drafted hard rule already sides with it without knowing it was adjudicating; it is
the version with an implementation behind it; and profile-keying contradicts that rule. Build
one draft's writer against the other's guard and every subagent write is refused, because the
guard looks for a field the writer never wrote — fail-safe, but the fence does not work.

*The one amendment.* The roots no permit may name are the governed paths, `.git/`,
`private/`, wildcards, and anything resolving outside the project root. **Not `operations/`**
— R1. The list is read from the single `governed-paths` file rather than embedded in code, so
the permit writer and the document allowlist cannot disagree.

### S4 — The citation checker is repository-wide, and checks that cited paths exist

Not harness-only. Two of the three drafts scoped it to the harness, which would have written
down the pipeline-code finding and then left it unfixed.

A path-existence check is added as a seventh item. The scanner built for this design found 37
path couplings and five defects, and two of the five were paths that no longer exist —
roughly forty percent of the real defects in the only measurement anyone made fall outside
the detector the design was building.

### S5 — Six role files, not two

`proposed_enforcement.md`'s two-file collapse is not implementable: the runtime binds one
model to one agent file, so two files cannot express six model-and-effort pairs, and its own
test byte-compares frontmatter against a generation that is undefined. The lifecycle seat
caught this. Six files stay; the mechanical benefit is one frontmatter line per writer role
and the deletion of the path-list prose, which is a small diff to files that already exist.

### S6 — Twelve hard rules; the two candidates do not become rules

The duplicate-identifier defect — two drafts assigning the same new number to two different
rules, both then citing it — is dissolved rather than resolved. Neither candidate becomes a
numbered rule.

"History is evidence, never instructions" already stands in `README.md` under Two
conventions. "His stated goal outranks the handoff and the brief" already stands in
`CLAUDE.md` under Every session. Both are true where they are. Promoting them lengthens a
ladder the design is trying to shorten, and creates the identifier collision that the
whole-system seat called the finding to show Tyrel first — the exact defect the design exists
to end, committed inside the cure.

The citations to the non-existent rule are deleted from both drafts.

### S7 — The suspension record does not move

`workbench/standing/SUSPENSIONS.md` stays where it is. Three drafts sent it to three
different places on the assumption that the workbench collapse was happening; the short
version does not collapse the workbench, so the question is moot.

---

## Open — Tyrel's, unanswered

**O1 — Does a writing agent lose the shell?** This is the move the whole fence rests on. Yes
means an agent cannot run the tests for code it just wrote; it returns a patch and the session
verifies. Today's worker role requires the opposite. A shell jail can buy the capability back
later if the loss actually hurts. *Recommendation: yes* — it converts about 1,800 lines of
guessing at command spellings into a capability that is simply not granted, and it is the
only part of the design that reaches "cannot" rather than "must not".

**O2 — May the off switch run during an unattended night?** Refusing protects against a 3am
session disabling the thing judging it. Allowing means a night blocked by a broken guard can
unblock itself — which is exactly what happened on 2026-07-31, when a half-applied guard edit
failed closed and refused every attempt to repair it. As drafted it refuses unless a
confirmation variable is set, which the same session can set, so it is a recorded tripwire
rather than a gate and should be described as one.

**O3 — Is turning a protection off itself a gated action?** The drafted gate list does not
include it and declares itself authoritative, so the small bypass is refused while the
sanctioned large one is not: `--no-verify` denies, while running the documented off command
and then committing passes without a gate and without asking.

**O4 — The README wording and the corrected hard rule 11.** Hard rule 11 currently promises a
one-step way to switch the machinery off, and `README.md` does not record the step. Either
the rule is corrected in the same commit that makes it true, or it is corrected first and
restored. A governing document, so his wording.

**O5 — What the infrastructure role is for, under R1.** It cannot write the guard, the hooks
or the skills. Either the session writes every line of its own machinery from now on, or the
role narrows to `operations/` and the pipeline and is renamed to say so.

**O6 — The word "autoclave" now names two things.** `autoclave/` at the repository root is
the cleanroom tray; `operations/autoclave/` is the container. `GLOSSARY.md` says one concept
per word and that if two words mean the same thing, one of them is wrong — this is the
inverse, one word for two concepts, and it is the same defect.

In lab terms the container has the better claim: an autoclave is the sterilising chamber, and
a tray goes inside one. So the honest fix is to rename the *tray*, not the chamber. That
touches `CLAUDE.md`, `GLOSSARY.md` and `ARCHITECTURE.md`, which makes it his.

There is a live mechanical consequence in the meantime. `pyproject.toml` puts `autoclave` in
pytest's skip list — deliberately, because the tray holds presumed-contaminated drafts and
pytest imports what it collects. That pattern matches a directory of that name anywhere, so
any test file placed inside `operations/autoclave/` is skipped in silence. The chamber's own
tests are therefore at `operations/test_autoclave.py`, outside the trap, and both the README
and the test file say why.

**O7 — Model credentials inside the chamber.** Deliberately not wired up, because the choices
differ in what they expose and he was not present. An environment variable passed at start, a
named volume authenticated once, or a mount of the host's tool configuration — and the third
puts real credentials somewhere an agent can read them. Nothing is mounted until he picks.

---

## Still to work

The whole-system seat listed sixteen questions. Six are moot under the short version — the
single-source register, the policy-directory contents, the migration order across seven
steps, the mechanical review carve-out, and the two that depended on the workbench collapse.
Of the remaining ten, S3 through S7 settle five, R1 settles one, and O1 through O5 are his.

Not yet worked: who writes the record of what an unattended night could not do (three drafts
say it happens automatically, the only named writer is a command the guard never invokes);
which single boundary closes `private/`, and whether it is a governed path; whether a
citation is a bare token or a link with an anchor; and what an absent `private/` means on a
fresh clone, which is the first branch a new machine takes and has no stated answer.

**Before any of it is built:** nothing on `work/claude-md-revision` has been pushed, and PR
#14 is open. Merging #14 unblocks everything downstream of it.

---

**O1 is closed, and R2 is what closed it. Recorded 2026-08-03.** As written, this record
carried two incompatible execution contracts: R2 above says a writing agent gets a
container with full capability — a shell, installs, its own tests — while O1 below lists
the shell as an open question and *recommends removing it*. A session reading this to
govern a dispatch could take either.

The container is what settled it, and it settled it the other way from O1's
recommendation. The shell is granted, because the boundary moved instead: an agent has
everything inside the chamber and nothing outside it, so "cannot run the tests for code
it just wrote" was a cost nobody had to pay. O1's own framing —
"a shell jail can buy the capability back later" — is what the autoclave turned out to
be. The measured consequence, on 2026-08-02: two *host* review seats returned "is any
claim in a commit message false?" unanswered because they could not run `git log`, while
the chamber seats that replaced them found four guard bypasses by executing the thing
rather than reading it.

Nothing above is amended in place — this record is evidence, and O1 recommending the
opposite of what was built is part of what it has to show. `ARCHITECTURE.md`,
`.claude/agents/README.md` and `operations/autoclave/README.md` are the current
authorities on where an agent runs.
