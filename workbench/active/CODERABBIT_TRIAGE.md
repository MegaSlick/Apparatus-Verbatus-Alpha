# CodeRabbit triage — PR #10, branch `infra/workspace-readiness`

What CodeRabbit said about this branch, checked point by point against
`workbench/active/AUDIT_LEDGER.md` (106 findings from eleven audits run *before* CodeRabbit)
and against the repository itself.

Nothing CodeRabbit raised is dropped — hard rule 7. Every one of its 45 points appears below
with a verdict and a reason, including the ones I dismiss.

**The question this document answers:** we already had eleven audits. What did CodeRabbit see
that they did not?

## What I checked and what I did not

I opened the file or ran the command for every point classified NEW-REAL, NEW-WRONG or
CONTRADICTS. Each is marked **VERIFIED** or **UNVERIFIED** with the reason. Duplicates I
verified only far enough to confirm the ledger already covers them — the ledger's own
verification status carries forward and is cited by id.

I did **not** run the full test suite as part of this triage; it is a separate task the ledger
already names as the next session's first job.

**One thing the review file does not contain.** Its own opening note reads: *"Due to the large
number of review comments, Critical severity comments were prioritized as inline comments."*
That means CodeRabbit produced a **Critical** tier that was posted as inline comments on the
pull request and is **not in the file I was given**. Everything below is the Major, Minor and
Nitpick tiers only. Whatever CodeRabbit rated Critical is unread by this triage and must be
collected from the PR before this document is treated as complete. That is the single largest
gap in what follows.

---

## Counts

| Class | Count |
|---|---|
| DUPLICATE — already in the ledger | 22 |
| NEW-REAL — new, verified, genuine | 9 |
| NEW-WRONG — new, checked, mistaken | 6 |
| NEW-STYLE — new, real, cosmetic | 8 |
| CONTRADICTS — disagrees with a ledger conclusion | 0 |
| **Total points triaged** | **45** |

Plus the two pre-merge check warnings (description template, docstring coverage), handled at
the end.

---

# NEW-REAL — the nine findings the eleven audits did not have

These are the prize. Each was verified by me against the repository at HEAD.

## NR1. Two agent role files never tell the agent not to edit the governing documents

**Where:** `.claude/agents/rebuilder.md` (Constraints list) and `.claude/agents/infra-worker.md`.
CodeRabbit found the first; I found the second while checking it.

**VERIFIED.** `worker.md` carries the bound explicitly — "Do not touch: canonical documents".
`rebuilder.md` and `infra-worker.md` do not, and both hold Write and Edit tools.

**What it means in plain terms.** CLAUDE.md's rule that a spawned agent may never rewrite the
project's own rulebooks is written in CLAUDE.md and repeated to one of the three writing roles.
The other two are trusted to remember it. An agent reads its own role file far more carefully
than it reads the project's, so this is the wrong place for the rule to be missing.

**Why the audits missed it.** The ledger's T1 covers the same territory from a different angle —
it says the rule has no *mechanical* enforcement and that all three roles hold Write. It never
noticed that two of the three are not even *told*. T1 recommends building a guard; this is a
two-line text fix available today, and it does not need T1 to be answered first.

**Consequence:** low probability, high cost. Cheapest fix in this whole document.

## NR2. The Codex seat config contradicts itself about `ultra` in the same file

**Where:** `operations/codex/seats.conf` — the header comment at lines 15-18 against the
`fix-sol` seat at line 59.

**VERIFIED.** The header states plainly that "no tracked seat uses that combined mode until the
mechanism leaves external evidence." Line 59 declares `fix-sol` at effort `ultra`.

**What it means.** A reader of the header alone believes the project has not admitted `ultra`.
It has, under a specific exception with a specific condition attached. This is exactly the
class of stale claim the ledger's N5 and N1 are about — a document telling a session something
that stopped being true.

## NR3. The seat test file's comment says `ultra` is absent from the very line that includes it

**Where:** `operations/codex/test_seat.py:26-28`.

**VERIFIED.** The comment reads "`ultra` is deliberately absent because automatic delegation is
not externally evidenced." The next line is `SEAT_EFFORTS = {..., "ultra"}`.

**Note on NR2 and NR3 together.** Both live in commits that landed *after* the ledger's audit
base (`6b42def`). The eleven audits never saw this code. That is not a failure of the audits —
it is a reminder that the tree moved after them, and it is a genuine argument for a tool that
reviews at push time rather than at a frozen commit.

## NR4. The push hook reports "no receipt recorded" when it means "I could not look"

**Where:** `.githooks/pre-push` — lines 41-47 against line 187.

**VERIFIED by reading the code path.** When the hook cannot locate the git directory it prints
a note saying no checklist can be shown, then carries on. But it does not stop building the
receipt path: `receipts` becomes `/audit-receipts`, the lookup fails, and the checklist prints
"no reviewer recorded — no receipt recorded".

**What it means.** The hook makes a factual claim about review coverage — "nobody reviewed
this" — in the one situation where it has no idea. Absent evidence reads exactly like damaged
evidence. That is the distinction `infra-worker.md` is built around and the distinction
GOVERNANCE 2 turns on.

**One correction to CodeRabbit.** It says the path "resolves to `/<sha>`". It actually resolves
to `/audit-receipts/<sha>`. The substance is unaffected.

**Consequence:** it does not block a push either way — this section is a checklist, not a gate.
The harm is a misleading line at exactly the moment a human is being asked to judge coverage.

## NR5. The pre-commit tests do not strip the developer's bypass variables; the pre-push tests do

**Where:** `.githooks/test_hooks.py:787-796` (`run_isolated_pre_commit`) against line 261
(`run_isolated_pre_push`).

**VERIFIED.** `clean_hook_env()` exists specifically to remove any exported `ALLOW_*` variable
so a developer's shell cannot make a safety test pass without proving anything.
`run_isolated_pre_push` uses it. `run_isolated_pre_commit` passes the raw environment straight
through — and when no override is supplied it passes `env=None`, inheriting the shell entirely.

**What it means.** `pre-commit` honours three bypass variables (`ALLOW_MAIN_COMMIT`,
`ALLOW_STRAY_DOC`, `ALLOW_DETACHED_COMMIT`). If any of them is exported in the shell running
the suite, the pre-commit tests stop testing what they claim. Some would fail loudly; at least
one — the test asserting a commit is *allowed* — would pass for the wrong reason.

**Why this one matters more than its size.** The ledger's mode D produced twenty-eight findings
about tests and this is not among them. The whole point of mode D was false confidence in the
test suite, and this is a live instance of it in the suite's own plumbing. It is the single
best thing CodeRabbit found.

## NR6. The guard refuses a read-only command that modern Git spells differently

**Where:** `.claude/hooks/guard.py:818-835`.

**VERIFIED — I ran it.** `git config get core.hooksPath` is **denied**, with the message
"permanently disables the git hooks for every tool in this clone." It does nothing of the sort;
it prints the setting. `git config core.hooksPath` (the older spelling) is correctly allowed.

**What it means.** Git 2.46 introduced `git config get/set/list/unset` as subcommands. This
machine runs 2.55. The guard recognises the old flag spellings and the bare-key form, but not
the new subcommand — so a session checking whether the hooks are installed, which `install.sh`
actively invites it to do, gets refused and told it is doing something destructive.

**Why it matters out of proportion to its size.** The ledger says this better than I can (L7,
L59): an over-refusal is how protection actually gets lost, because a session that cannot do
legitimate work reaches for a bypass. The ledger has the *category*; it does not have this
instance, and it explicitly refuted a similar-sounding auditor claim about `--show-origin`
(R13), which makes a verified instance more valuable, not less.

## NR7. The credential scanner crashes with a traceback instead of a reasoned refusal

**Where:** `.githooks/check_ingress.py:744-774`.

**VERIFIED — I ran it.** `python3 .githooks/check_ingress.py --message-file ""` produces a raw
Python traceback (`TypeError: expected str, bytes or os.PathLike object, not NoneType`).

**Why.** The dispatcher tests each mode for truthiness rather than for presence. An empty string
is accepted by the argument parser but is falsy, so it falls past its own handler into the
history-scan branch with nothing to scan.

**What it means.** The scanner is built to distinguish three outcomes — clean, found something,
could not run — and the third has its own exit code and its own message. This path produces
none of them. A caller still sees a non-zero status and still blocks, so nothing unsafe gets
through; what is lost is the diagnosis. The ledger's L26 is the same *family* (the hook
diagnoses a scanner failure wrongly) but a different mechanism.

**Consequence:** low. No hook passes an empty string today. It is a robustness fix, not a hole.

## NR8. The push hook's receipt check is welded to two exact spaces

**Where:** `.githooks/pre-push:195` against `.githooks/record-audit.sh:147`.

**VERIFIED, and it is not broken today.** `record-audit.sh` writes `commit:  <sha>` with two
spaces; `pre-push` greps for `commit:  <sha>` with two spaces. They match.

**What it means.** The two files agree by coincidence of formatting rather than by contract. If
anyone ever tidies the whitespace in `record-audit.sh`, every valid receipt starts reporting
"receipt records a different commit" — and because this section never blocks, that misreport
appears as *reduced review coverage* rather than as a bug. The reviewer gate would quietly
start telling Tyrel his reviews had not happened.

**I am recording this as NEW-REAL rather than cosmetic, with the qualifier that nothing is
wrong right now.** It is a latent trap in the one mechanism that reports whether a push was
reviewed, and the fix is a one-line change to match the field loosely.

## NR9. The ledger's own headline counts do not add up to its own stated total

**Where:** `workbench/active/AUDIT_LEDGER.md:23` against `:1205`.

**VERIFIED by counting the labels myself:** 10 N-items, 7 T-items, 62 L-items, 14 R-items = 93.
Line 1205 says "Of the 106 distinct findings".

**And here is the reconciliation CodeRabbit did not find.** 106 is not a wrong number. The
ledger's own experiment table gives 93 Sol findings + 50 Terra findings − 37 raised by both =
106 distinct findings *in the eleven reports*. The 93 dispositions are what those 106 collapse
into after deduplication. Both numbers are right; nothing in the document says they are
different quantities.

**What it means.** The defect is real but it is one missing sentence, not an arithmetic error.
A reader — human or machine, and CodeRabbit is the proof — reads two totals for the same thing
and loses confidence in a document whose whole purpose is to be trusted. Worth one clause.

---

# CONTRADICTS

**None.** CodeRabbit did not overturn a single ledger conclusion.

The one place it came close is `doc-allowlist.sh` (see NW3 below), where the ledger's
"checked and found clean" section certifies the depth rule and CodeRabbit says a naming rule is
unenforced. On reading both, they are not in conflict: the ledger certified the rule the code
claims to enforce, and CodeRabbit misread an illustrative example as a rule. The ledger stands.

Worth stating positively, because it is evidence about the eleven audits: **an independent tool
reviewed the same tree and found nothing the ledger got wrong.**

---

# NEW-WRONG — six points I checked and dismissed

## NW1. "Remove the machine-specific path `/Users/tyrel/Temp_Stage` from RUN_PLAN.md"

**VERIFIED as deliberate, not a defect.** The line records where the frozen old repository
physically sits. CLAUDE.md's quarantine section is built on that arrangement: the old code "is
read where it lies (`Temp_Stage`, the frozen old repository), through the window". The path is
also a declared working directory of the environment. Making it "repository-relative" would
require the quarantined material to be inside this repository, which is the one thing the
quarantine forbids. CodeRabbit applied a general portability rule to a note that is deliberately
about one machine.

## NW2. "Mark RUN_PLAN §5 non-executable until the write-location question is resolved"

**VERIFIED — it already is, in the loudest terms available.** Line 145, directly above the
section, reads: **"§5 IS UNDER REVISION AND MUST NOT BE FOLLOWED AS WRITTEN. 2026-07-27."** It
then lists the three load-bearing claims that are wrong, including the write-location question,
and names it as Tyrel's and still open. CodeRabbit read the instructions below the banner and
asked for the banner. It is there.

## NW3. "The dated-subdirectory rule in `doc-allowlist.sh` is documented but not enforced"

**VERIFIED, and the premise is a misreading.** The comment says "Two levels at most: files
directly in `active/`, and one subdirectory for a dated set **such as** `reviews-YYYY-MM-DD/`."
"Such as" is an example of the intended use, not a naming constraint. The code enforces the
rule it states — depth — which is what the ledger's clean list certified. Tightening the name
pattern is available if Tyrel wants it, but nothing is currently broken or mis-documented.

## NW4. "Restore pytest's default recursion exclusions in `pyproject.toml`"

**VERIFIED — the suggested fix would break the test suite.** CodeRabbit asks to add back
pytest's defaults including `.*`, which excludes every directory whose name begins with a dot.
This project's tests live in `.githooks/` and `.claude/hooks/`. Adding `.*` would stop pytest
collecting the hook tests and the guard tests — the majority of the suite — and it would do so
silently, because uncollected tests do not fail. `pyproject.toml` already carries a comment
explaining that the key replaces rather than extends the defaults and naming the noise
directories individually, on purpose.

**This one is worth remembering.** It is a confident, well-formatted suggestion that would have
disabled most of the safety suite. It is the argument against applying CodeRabbit's fixes
without reading them.

## NW5. "Wire the stage-boundary check (`tach`) into CI unconditionally"

**VERIFIED — premature, and the gap is already disclosed.** `tach.toml`'s own comment states
that the numbered stage directories are not Python packages, are not represented, and that
"their first code must add an executable boundary test instead of treating this declaration as
proof that no stage imports another." `check-all.sh` prints "no shared implementation yet —
Tach boundary check deferred". There is no stage code to check yet. The disclosure is exactly
what GOVERNANCE 10 asks for.

## NW6. "The installer advertises the `--no-verify` bypass it forbids"

**VERIFIED as deliberate.** CLAUDE.md says the same thing about itself, in the same voice:
"**The gate is discipline, not machinery.** ... `ALLOW_UNAUDITED_PUSH=1`, `--no-verify` and
`-c core.hooksPath=` all get past it." The project's position is that a guard which conceals
its own limits is worse than one that states them, and the installer's closing line — "These
hooks stop accidents, not a determined tool" — is that position. CodeRabbit treated candour as
a leak. Tyrel may still prefer the wording to add "and it is prohibited here"; that is taste,
not a defect.

---

# NEW-STYLE — eight real but cosmetic points, grouped

None of these changes what anything does. Listed so nothing is lost, not expanded.

1. **`session-end/SKILL.md:203`** — a fenced code block has no language tag; add `text` to
   satisfy markdownlint MD040. VERIFIED.
2. **`test_build_wheel.py:57`** — a `pytest.raises(match=...)` pattern contains unescaped dots,
   so the assertion is looser than it looks. VERIFIED. It still catches the case it is for.
3. **`commit-msg:51`** — the credential scanner prints a success line, so every commit gains an
   unrelated line of output. `record-audit.sh` already redirects it. VERIFIED, cosmetic.
4. **`record-audit.sh:45-50`** — the check that rejects a carriage return in a receipt field
   contains a literal, invisible CR byte in the shell pattern. It works; it is fragile against
   any tool that normalises line endings. VERIFIED by reading the raw bytes.
5. **`tidy.py:145-153`** — a memory-index link pointing at a non-`.md` file is reported as
   dangling even when the file exists. VERIFIED. Every memory file is `.md` today.
6. **`reviewer-pass/SKILL.md:98-110`** — `umask 077` sits after the `mktemp` it was meant to
   protect (`mktemp` already creates the file private), and the final `mv` could in principle
   overwrite a report written by a concurrent pass between the existence check and the move.
   VERIFIED. Overlaps the ledger's **L50**, which already covers the trap and the byte-fidelity
   problems in the same block; the umask placement and the move race are the new parts.
7. **`test_tidy.py`** — tests exercise only the "wants attention" path, not the clean (0) or
   failure (2) exit codes callers actually branch on. UNVERIFIED in detail; the ledger's **L44**
   already records that four tidy tests would pass against an empty function, so this sits
   inside a known gap.
8. **`workbench/README.md:19`** — the `scratch/` row says material may be deleted "without
   asking, ever", which CodeRabbit reads against hard rule 7. VERIFIED that the line says that.
   I do not think it is a defect: the drawer is *defined* as "greps, dumps, fragments, one-off
   output", so by construction it holds nothing that rule 7 protects. If a finding lands there
   the mistake was putting it there. A wording tightening is available and costs nothing.

---

# DUPLICATE — 22 points the ledger already had

Given by ledger id, with nothing added.

**The tracked-`workbench/active/` confusion — ledger N2 and L52.** CodeRabbit raised this six
separate times: `CHANGES_TONIGHT.md:45-53`, `DECISIONS_FOR_TYREL.md:108-151` (T17 stale),
`PUSH_PLAN.md:44-55` and `:154-177`, `workbench/README.md:59-65`, `HANDOFF.md:350-358` (stale
file inventory), and `HANDOFF.md:17-26` (heading says "Two commits", three are listed — I
confirmed the contradiction; L52 names commit-count disagreement as one of its five strands).

**The self-orchestration / sandbox claim recorded as settled when it is contested — ledger
L55.** Raised six times: `DISPOSITION.md:169-176`, `PRE_REBUILD_INTENT.md:152-157`,
`NEXT_SESSION_BRIEF.md:141-150`, `NEXT_SESSION_BRIEF.md:81-106` (the watcher and sandbox
directives), `ORCHESTRATION_FINDINGS.md:31-50`, `ORCHESTRATION_FINDINGS.md:70-98` (the TMPTRAY
conclusion).

**The list of decisions still waiting on Tyrel is out of date — ledger L52.**
`PRE_REBUILD_INTENT.md:211-233` and `DECISIONS_FOR_TYREL.md:229-237`. CodeRabbit named a second
instance of the strand L52 opened; the class and the recommended fix (one cleanup pass over
`workbench/active/`, not ten corrections) are unchanged.

**RUN_PLAN's false claim of a register-text content filter — ledger N3.** `RUN_PLAN.md:38-43`.
Independent confirmation of the ledger's most consequential single finding.

**RUN_PLAN's porting language and the surviving winner-pick — ledger D1 and D2.**
`RUN_PLAN.md:431-454`. See the section on the plan below; this is the most important thing
CodeRabbit did.

**Conflicting test counts across the tracked notes — ledger L62 and L52.** `PUSH_PLAN.md:8-24`
(439 against 403) and `AUDIT_LEDGER.md:1178-1220` (403, 439 and 448 all present). L62 already
says the number should either cite a retained artefact or be dropped.

**The empty `permissions.deny` list in `.claude/settings.json` — ledger T1 and L9.** T1 records
the empty deny list as one of its three verified facts; L9 records that the guard launcher
fails open if the guard cannot start. CodeRabbit's point is the two of them combined, which is
a fair framing but not new information.

**`curl -K/--config` hides the request from the guard — ledger L1**, which names "curl config
files" in its list of indirections that defeat basename dispatch.

**`async: true` means a failed session-start notification is silent — ledger N8 and L59.** N8 is
the exit-code contract for `start` and `milestone`; L59 records that the fifteen-minute
suppression is undocumented.

**Confirm GitHub's branch protection is really configured — ledger "What nobody examined".**
The ledger names this explicitly as unverifiable from inside the repository, which is the same
conclusion.

---

# Pre-merge check warnings

**Description check (failed).** The PR body does not follow
`.github/pull_request_template.md`. Real and trivially fixable; nobody has claimed otherwise.

**Docstring coverage 6.19% against a required 80% (failed).** This is CodeRabbit's default
threshold, not this project's standard, applied to a tree of shell hooks and test files. It
should be switched off in configuration rather than satisfied. See the recommendation below.

---

# 1. What CodeRabbit visibly did NOT cover

An uncovered area is an unknown, not a pass. Three separate gaps:

**The Critical tier is missing from what I was given.** The review's own first line says
Critical comments were posted inline rather than in this summary. I have triaged Major, Minor
and Nitpick only. **This must be collected from the PR before anyone treats this triage as
complete.**

**Of the 82 files CodeRabbit listed as selected for processing, 43 drew no comment at all.**
Named, because silence is not a verdict:

- Every pipeline stage document: `pipeline/README.md` and the seven stage `HANDOFF.md` files
  (Exemplar, Designator, Attestatores, Perlector, Recensor, Archetypus, Armarium), the three
  attestator READMEs, and `pipeline/5_recensor/review/README.md`. These are where the ledger's
  L57 lives (unbuilt components described in the present tense) and where GOVERNANCE 3
  compliance will actually be decided.
- `GOVERNANCE.md`, `ARCHITECTURE.md`, `README.md` — the canonical documents. The ledger says
  the same thing about the eleven audits: they were "the ruler, never the thing measured."
  CodeRabbit did not measure them either.
- `.claude/hooks/test_guard.py` and `.githooks/test_ingress.py` — the two largest test files in
  the repository, and the ones that would show whether the guard's and scanner's tests assert
  anything. CodeRabbit commented on four *other* test files but not these two.
- `operations/codex/seat.sh` and `operations/notify/notify.sh` — the two scripts that spend
  money and reach the outside world. It commented on their config and their tests, never on
  them.
- `.githooks/pre-commit`, `.githooks/check-documents.sh`, `.githooks/build_wheel.py`,
  `.github/workflows/ci.yml`, `.gitignore`, `.gitattributes`, `proof/fixtures.toml`,
  `requirements-dev.txt`, `common/__init__.py`, `LICENSE`, and eight more READMEs.
- `.claude/agents/auditor.md`, `consult.md`, `scout.md`, `worker.md`, `README.md` and
  `.claude/skills/session-start/SKILL.md`.

**The incremental re-review covered one file.** The follow-up run (commits `cfce64b` to
`fafadca`) selected `operations/codex/test_seat.py` and then skipped it as "similar to previous
changes", returning "No actionable comments". Those two commits are effectively unreviewed by
CodeRabbit.

**The pattern in the silence.** CodeRabbit commented on shell hooks, config files and workbench
notes. It did not comment on a single one of the seven pipeline stage documents, and it did not
open the two biggest test files. Its coverage was wide and thin, and it thinned out exactly
where the project's subject matter begins.

---

# 2. Did the plan get reviewed properly?

`workbench/active/RUN_PLAN.md` is the specification for the alpha rebuild. Judging CodeRabbit
on that file specifically is the right test, and the answer is **partly, and better than I
expected on one point.**

It made four comments on the plan. Here is what each actually engaged with.

**It read the plan as a specification, once, and got the most important thing right.** On lines
431-454 it wrote:

> "Porting proven invariants" and "the crossref winner-pick ... survives" conflict with the
> rebuild boundary and the prohibition on selecting among witnesses. **These are not harmless
> terminology choices in an executable plan; they can direct a future session to preserve
> legacy code or implement picker behavior.**

That is a governance reading, not a prose reading. It identified the picker prohibition and the
quarantine boundary, applied both to the plan's own words, and stated the consequence in terms
of what a future session would be directed to build. It arrived independently at the ledger's
**D1 and D2** — which the ledger describes as a GOVERNANCE 3 question that goes to Tyrel with
the file and line. **An independent tool, with no knowledge of this project's history,
reproduced the night's highest-value finding from the text alone.** That is meaningful
corroboration, and it is the strongest evidence available that tracking the plan and putting it
through review works.

**It read the plan as a specification a second time, on a safety claim.** On lines 38-43 it
flagged the false register-text filter claim (ledger N3) and named the consequence correctly:

> This is a dangerous false assurance: future sessions may relax line-by-line review because
> they believe the hook provides that protection.

Again — the reasoning is about what the specification will cause someone to do, not about how
it reads.

**The other two comments were weaker, and one was simply wrong.** On lines 174-214 it asked for
a warning banner that is already there, in capitals, immediately above the text it was reading
(NW2). On lines 8-10 it applied a generic portability rule to the deliberate quarantine
arrangement (NW1).

**What it did not do at all — and this is the gap.** RUN_PLAN.md is roughly 570 lines covering
nine phases of rebuild. CodeRabbit engaged with two sentences of it. It said nothing about:

- **The build sequence.** Whether the phases are ordered correctly, whether anything is built
  before the thing it depends on, whether the stage order matches `ARCHITECTURE.md`.
- **Whether the plan actually delivers the goals.** GOALS.md 1 is near-100% capture and says a
  missed act is worse than a poorly read one; GOALS.md 3 requires every page accounted for.
  Nothing in the review asks whether the plan's phases produce that.
- **The bounded-recovery rule (GOVERNANCE 11)**, the immutability of evidence (4), one-text
  (5), or provenance (6) — none of which it tested the plan against.
- **The plan's own economics and the model assignments** in §5 and the cascade, beyond
  repeating the banner.
- **The dossier template in §9**, including the "every critical and high defect" clause that
  the ledger flagged as a possible severity floor under GOVERNANCE 10 (T5). CodeRabbit read
  near it and did not see it.

**The honest verdict.** CodeRabbit did not treat the plan purely as documentation to be tidied —
that would be unfair to it, and its 431-454 comment is the best thing in the whole review. But
it engaged with the specification only where a *sentence* contradicted a *rule* it could pattern
match. It never engaged with the plan as a plan: sequence, sufficiency, whether the thing
described would achieve GOALS.md if built exactly as written. **That is a serious coverage gap
in the most important file on the branch, and no amount of markdown-versus-code accounting
changes it.** The plan still needs a human-directed specification review; CodeRabbit did not
provide one and should not be counted as having done so.

---

# 3. Was CodeRabbit worth it on this PR?

**Yes, narrowly, and not for the reason anyone would have predicted.**

The honest accounting: 45 points, of which 22 were already in the ledger and 14 were wrong or
cosmetic. Nine were new and real. Of those nine, most are small — a stale comment, an
over-refusal, a fragile whitespace match.

**But two of the nine justify it.**

- **NR5**, the pre-commit tests inheriting the developer's bypass variables. Eleven audits ran
  a mode dedicated entirely to test integrity and false confidence, produced twenty-eight
  findings, and did not find this. It is a real hole in the hygiene of the safety suite and it
  sits in the suite's own helper functions.
- **NR6**, the guard refusing `git config get`. Small, but it is the exact failure shape the
  ledger argues is most dangerous — a guard blocking legitimate work teaches a session to
  switch the guard off — and the ledger had the category without the instance.

Add **NR2** and **NR3**: two self-contradicting comments in code that landed *after* the audits
finished. A tool that reviews the tree at push time catches what a frozen-commit audit
structurally cannot. That is a durable advantage, not a one-off.

And add the corroboration value. CodeRabbit independently reached the picker-and-port finding
in RUN_PLAN.md, and it overturned nothing in the ledger. Both facts increase confidence in a
document that eleven expensive audits produced.

**Against that:** it produced six confidently wrong recommendations, one of which
(**NW4**, the pytest exclusions) would have silently disabled most of the test suite if applied
as written. Its "Prompt for AI Agents" blocks are written to be pasted into an agent, and an
agent that pastes them without checking will damage this repository. The review is worth having
and its output must never be applied unread.

**Net:** cheap, fast, wide, shallow, and it found two things eleven targeted audits missed.
Keep it. Do not let it substitute for anything.

---

# 4. Recommendation on `.coderabbit.yaml`

**Add one.** Three things it should say, in priority order.

**First — and this is the whole point — tell it the planning documents are specifications.**
Path-specific instructions for `workbench/active/RUN_PLAN.md`, `PRE_REBUILD_INTENT.md` and
`ARCHITECTURE.md` saying: *review this as an executable specification, not as prose. Read
`GOALS.md` and `GOVERNANCE.md` first. For every instruction it gives a future session, ask
whether following it would build something the governance forbids — in particular GOVERNANCE 3
(no stage may select among witnesses, under any name), the quarantine (nothing is ported or
copied from the old repository; it is rebuilt), GOVERNANCE 2 (nothing lost silently) and
GOVERNANCE 10 (no claim beyond what was measured). Report sequencing gaps and anything the plan
does not cover that the goals require. Do not report formatting, stale dates or internal
inconsistency between notes.*

The evidence that this pays is already in: CodeRabbit's single best comment came from doing
exactly this unprompted on two sentences. Instructing it should extend that from two sentences
to the whole file — and it should stop the low-value output (four of its five weakest plan-side
comments were stale-status and portability noise).

**Second — raise the depth on the security-relevant code**, without touching the plan's budget.
Path instructions for `.githooks/**` and `.claude/hooks/**` saying these are safety mechanisms
where a wrong claim is worse than a missing feature: check that every message the code prints
is true of what it just did, that absent evidence is never reported as negative evidence, and
that no guard refuses ordinary legitimate work. **NR4 and NR6 are both instances of that
instruction, and both are the kinds of thing it found by accident rather than by design.**

**Third — turn off the two pre-merge checks that do not fit.** Disable the docstring-coverage
threshold: it is measuring shell hooks and pytest files against an 80% Python-library standard
and will fail every PR forever, which trains everyone to ignore the check block. Keep the
description-template check; that one is correct and the fix is to fill the template in.

**What I would explicitly NOT put in the config:** any path filter that de-prioritises
`workbench/`. The plan is the most valuable thing on the branch to review and the cheapest place
for a defect to be caught. If anything gets less attention it should be the archived reviews
under `workbench/active/reviews-*/`, which are historical records rather than instructions —
and even there the gain is small.

**One thing config cannot fix.** CodeRabbit skipped 43 of 82 files including every pipeline
stage document and the two largest test files. That is a size limit, not a settings problem. The
answer is smaller pull requests, not a better `.coderabbit.yaml`.

---

# What I would do next

1. **Collect the Critical inline comments from PR #10.** They are not in the file I triaged and
   they are, by CodeRabbit's own severity ordering, the ones most likely to matter. Nothing here
   is complete until they are read.
2. **Fix NR1, NR2, NR3 and NR9** — four stale or missing sentences, well under an hour together,
   same class as the ledger's N1/N5 and no new machinery.
3. **Fix NR5.** It is a two-line change to a test helper and it restores a safety property the
   suite currently only appears to have.
4. **Add the `.coderabbit.yaml` above** before the next push, so the next review spends itself
   on the plan rather than on stale dates.

NR4, NR6, NR7 and NR8 are real and belong on the follow-up backlog beside the ledger's FIX
LATER items. None of them blocks this branch.
