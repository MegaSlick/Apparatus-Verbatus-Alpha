# Audit ledger — infra/workspace-readiness @ 6b42def

Assembled from the eleven-report audit pass in `workbench/raw/2026-07-28_audit-pass/`.
Five audit modes, each run twice on an identical prompt by two models blind to each other
(GPT-5.6 Sol and GPT-5.6 Terra). Mode B ran twice: once attack-framed
(`report_B_bypass_*`, whose Terra seat was killed by OpenAI's safety classifier, leaving a
single seat) and once defensively framed (`report_B2_robustness_*`, both seats completed).

## How to read a finding

Every finding raised by any seat appears here, including the ones I rejected — hard rule 7,
"**Nothing is lost silently** — findings, reviews and decisions, not only acts." A rejected
finding is recorded with the reason.

- **Seats** — which reports raised it. Where both models raised the same thing
  independently that is noted; where only one did, that is noted too, because a finding one
  model saw alone is the more interesting kind.
- **VERIFIED** — I opened the file or ran the check myself. **UNVERIFIED** — I am relaying
  an auditor's claim without confirming it. **REFUTED** — I checked and the auditor was
  wrong.
- **Disposition** — FIX NOW, TYREL DECIDES, FIX LATER, REJECT.

**Counts.** 10 FIX NOW · 7 TYREL DECIDES · 62 FIX LATER · 14 REJECT.

**A note on method that changed two findings.** The Claude-side guard signals a refusal by
printing a JSON `deny` decision on standard output and *exiting 0*. Exit status alone tells
you nothing. I nearly recorded three findings wrongly on that basis before catching it, and
at least one auditor appears to have made exactly that mistake (R13). Every guard result
below marked VERIFIED was checked by reading the decision, not the exit code.

---

# FIX NOW

Ten items. Every one of them is either a document that misleads a reader about what protects
them, or a guard that does not do what its own words say. None needs new machinery, and all
are within reach of this branch. Eight of the ten I verified against the repository myself.

## N1. Three documents say the notifier refuses `private/ntfy.conf`. It reads it.

**Where:** `operations/README.md:22-23`, `private/README.md:13-14`,
`.claude/skills/session-end/SKILL.md` — against `operations/notify/notify.sh:60-79`.

**Seats:** raised independently in three separate modes by four seats — A-Sol, A-Terra,
C-Sol, D-Sol, D-Terra. The strongest cross-mode convergence in the whole pass.

**VERIFIED.** `operations/README.md` says the client "accepts its bearer topic only from the
`NTFY_TOPIC` process environment and refuses the retired `private/ntfy.conf` without reading
it." `notify.sh` sets `conf="$root/private/ntfy.conf"` and then, when the environment is
empty, reads it with `sed`. Its own header comment states the opposite of the README: "The
bearer topic comes from NTFY_TOPIC if the environment sets it, and otherwise from
private/ntfy.conf." CLAUDE.md agrees with the code, not the READMEs: "The topic lives in
`private/ntfy.conf` and nowhere else."

**What it means.** Three documents describe a design that was tried, found wrong, and
reversed — and were never updated. The `notify.sh` comment explains exactly why it was
reversed: requiring the environment alone meant "every `start` and `milestone` would exit 0
announcing 'notifications are off' and the phone would simply go quiet." A future session
that trusts those READMEs deletes `private/ntfy.conf` as dead weight, and the phone stops
working with no error anyone sees — because `start` and `milestone` are deliberately
non-fatal. This is precisely the silent loss GOVERNANCE 2 forbids.

**Fix:** correct the three documents to match the code and CLAUDE.md — the environment
variable wins, the gitignored file is the default. **Test that would prove it:** a test that
greps the three files for the word "refuses" applied to `ntfy.conf` and fails if it appears,
paired with the existing `test_notify.py` case that already proves config-only delivery
invokes curl.

## N2. `workbench/README.md` says the whole drawer is local-only. `workbench/active/` is tracked.

**Where:** `workbench/README.md:3-4` against `.gitignore:28-41`; also
`.githooks/pre-commit:116` guidance text.

**Seats:** A-Sol, A-Terra, B2-Sol, B-bypass-Sol.

**VERIFIED.** `workbench/README.md` opens: "**Local only. Everything here except this file is
gitignored and never reaches the repository.**" `.gitignore` says the opposite for one
drawer, and says so deliberately: "Except `workbench/active/`, which is tracked for alpha.
Tyrel's ruling: the live drawer is a spot check on what sessions are actually planning."

**What it means.** A session reads the workbench README, believes anything it writes into
the workbench stays on the machine, and writes a note into `active/`. That note is now
tracked, pushed, and read by CodeRabbit and three reviewers. This is not hypothetical — it
is how the RUN_PLAN.md problems below (N3, D1) reached the repository. The README is the
first thing a session reads about where notes go, and it is wrong about the one drawer that
publishes.

**Fix:** amend `workbench/README.md` to carry the same exception `.gitignore` already
carries, in the same voice — the rest of the drawer is local, `active/` is tracked on
purpose. **Test:** a check that the README names `active/` as tracked, so the two facts
cannot drift apart again (this is the same class of stale-duplicate that
`doc-allowlist.sh` already documents having suffered once).

## N3. `RUN_PLAN.md` claims a content filter for register text that does not exist.

**Where:** `workbench/active/RUN_PLAN.md:41` — "the pre-commit hook refuses register text by
content, and nothing is gated on file extension."

**Seats:** A-Terra and E-Sol — found independently in two different modes, by *different
models*. A-Sol and E-Terra both missed it. This is the clearest single case in the pass of
the mirrored design paying for itself: neither model would have caught it alone across both
its modes, and the cross-pairing did.

**VERIFIED.** I searched `.githooks/check_ingress.py` at 6b42def for any register-text or
transcribed-prose matcher: there is none. The scanner matches credential shapes and payload
sizes. `autoclave/README.md:22-23` states the truth plainly and contradicts the plan: the
scanner "does not recognise **register text**: transcribed prose is ordinary text to a
scanner, and only the line-by-line review catches it."

**What it means.** This is the most consequential kind of untrue sentence this project can
contain. The quarantine's whole purpose is that no old manuscript text or old code crosses
into this repository, and the plan tells a future session that a machine is watching for it.
No machine is. Only human review catches it. A session that believes the plan will relax
exactly where it should be tightest.

**Fix:** delete or correct the clause in RUN_PLAN.md so it matches `autoclave/README.md` —
placement is caught by review, not by content scanning. **Test:** none needed for the doc
edit; the durable protection would be a check that the phrase "refuses register text" does
not appear outside `autoclave/README.md`'s denial of it.

## N4. The guard lets `git push origin main` through inside a `cat` heredoc.

**Where:** `.claude/hooks/guard.py` — heredoc handling (docstring claim at the file head;
the cat/tee exemption).

**Seats:** **B-bypass-Sol only** — the attack-framed run. The defensively framed B2-Sol run
examined the same code and found only the *opposite* defect (heredocs refused too often).

**VERIFIED — I ran it.** Feeding the guard a Bash tool call whose command is a `cat <<EOF`
heredoc with `$(git push origin HEAD:main)` in the body returns exit 0, allow. The guard
denies the same push written plainly.

**What it means.** The guard treats a heredoc attached to `cat` or `tee` as inert data. An
unquoted heredoc delimiter is not inert — the shell still expands `$(...)` inside the body
before `cat` ever sees it. So the one construct the guard's own docstring names as safe is
the construct that carries a command past it. Hard rule 3 is "Never commit or push to
`main`", and the Claude-side guard is the mechanism that is supposed to catch a session
drifting toward it. An accidental session is unlikely to write this, but the guard's
docstring makes a claim GOVERNANCE 10 does not permit: "Claims are made only about what was
actually measured."

**Fix:** treat a heredoc whose delimiter is *unquoted* as command text and classify its body,
regardless of whether it is attached to `cat` or `tee`; keep the quoted-delimiter case inert.
**Test:** the exact input above must return a deny decision, and `cat <<'EOF'` with the same
body must still be allowed.

## N5. CLAUDE.md says every agent's model is pinned in its file. One is not.

**Where:** `CLAUDE.md:171` — "each with model and effort **pinned in its file**" — against
`.claude/agents/consult.md:6`, which reads `model: inherit`.

**Seats:** **Sol only** (mode A).

**VERIFIED.** Both lines read as quoted at 6b42def. `.claude/agents/README.md` states the
accurate version: "`auditor` and `consult` deliberately select a model per invocation, while
keeping effort fixed."

**What it means.** A small sentence, in the one file that binds every session. It tells a
session that `consult` runs at a known model when in fact it inherits whatever the calling
session happens to be. The `consult` role exists to be the highest-effort read in the roster;
a cheap session spawning it would get a cheap second opinion and CLAUDE.md would have told
the session otherwise. The agents README already says the right thing, so this is a
one-clause correction to bring the governing file into line with its own subordinate doc.

**Fix:** change the CLAUDE.md clause to say fixed roles pin both, and `auditor`/`consult`
pin effort while selecting model per call. Note that rule 10 permits the main session to edit
CLAUDE.md; it does not permit a spawned agent to. **Test:** none mechanical; this is prose.

## N6. `pre-push`'s old-Git compatibility fallback does not fire, and Terra called it clean.

**Where:** `.githooks/pre-push:29-35`; same shape in `.githooks/record-audit.sh:67-76`.

**Seats:** **C-Sol only.** C-Terra examined the same lines and listed them in its
*checked-and-found-clean* section — a direct contradiction between the pair.

**VERIFIED — I ran it, and Sol is right.** The code assumes that an old Git which does not
understand `--path-format=absolute` will produce empty output, and falls back only on empty.
Running `git rev-parse --bogus-option-xyz --git-common-dir` on this machine returns exit 0
and prints the unrecognised option back as a literal line followed by the real answer. So on
an old Git the variable would hold two lines of junk, the emptiness test would pass, and the
fallback would never run.

**What it means.** The comment above those lines explains what the guard is for — "If this
came back empty the path would become /audit-receipts, every push would be refused, and
nobody would guess why." The guard misses the case it was written for. The failure is safe
in direction (the receipt lookup fails, the push is refused) but the refusal message would
be "0 of 3 reviewers" rather than anything that names the real cause, so somebody debugging
it on a fresh machine with an older Git would chase the wrong thing for an hour.

**Fix:** validate the result — require it to be a single line that begins with `/` — rather
than only testing for emptiness. **Test:** a case that feeds the resolver a stub `git` which
echoes the option back, and asserts the fallback path is taken.

**This finding is also the single best argument in the pass for running two models:** one
seat found a real defect that the other seat looked straight at and certified sound.

## N7. `record-audit.sh` binds a receipt to whatever HEAD is now, not to the commit reviewed.

**Where:** `.githooks/record-audit.sh:29,90`; consumed at `.githooks/pre-push:167,182`.

**Seats:** **B2-Sol only.**

**UNVERIFIED (relayed).** I read the consuming side in `pre-push` and it does bind strictly to
the peeled pushed commit, as the auditors agree; I did not trace `record-audit.sh`'s own SHA
resolution end to end.

**What it means.** The review-receipt gate is the mechanism behind hard rule 4, "Never push
without his say-so and a review covering that exact commit." If a reviewer reads commit X and
the operator then makes one more commit before running `record-audit.sh`, the receipt is
written honestly — the right reviewer name, the right finding — but stamped against commit Y,
which nobody read. The gate then passes on an unreviewed commit while producing a receipt
that looks perfect. CLAUDE.md already warns that "A receipt proves a file was written, not
that anything was read"; this is a way for it to prove less than that.

**Fix:** have `record-audit.sh` take the reviewed SHA as a required argument and refuse if it
does not match HEAD, rather than silently reading HEAD. **Test:** record a receipt, make a
commit, attempt to record a second reviewer, and assert refusal.

## N8. Failed `start` and `milestone` notifications report success.

**Where:** `operations/notify/notify.sh:50-56` — `fail()` exits 0 for `start` and `milestone`.

**Seats:** A-Sol, D-Sol.

**VERIFIED.** The `fail()` function exits 1 only for `decision` and `done`; every other event
exits 0 after printing to stderr. The design is deliberate and documented in the file header.

**What it means.** This one is a genuine GOVERNANCE 0 case — a rule and a goal pulling apart —
and it is included as FIX NOW only because it compounds N1 exactly. Taken alone the design is
defensible: a `start` ping that fails should not abort a session. Taken with N1, the chain is
that a session deletes `ntfy.conf` on a document's bad advice, `milestone` pings then fail
silently, and the failure surfaces only when Tyrel notices his phone has been quiet for a
week. Fixing N1 removes most of the risk.

**Fix:** the minimal honest change is that `start`/`milestone` still exit 0 but print a
distinguishable, greppable line, and that `operations/README.md` states the exit-code
contract plainly so a caller cannot read exit 0 as "delivered". **Test:** assert the stderr
line is emitted on every failure path, not just the missing-topic one.

## N9. `ALLOW_FORCE_PUSH=1` is documented everywhere and does not work.

**Where:** `.claude/hooks/guard.py` — the force-push refusal names the variable in its own
message; `CLAUDE.md` and the guard docstring both present it as a working escape hatch.

**Seats:** **B2-Sol only.**

**VERIFIED — I ran it.** `ALLOW_FORCE_PUSH=1 git push --force origin work/x` is refused, and
the refusal text is the one that advertises the hatch: "force-push destroys work another
agent may be holding (ALLOW_FORCE_PUSH=1 if you truly mean it)." The guard strips the
variable assignment from the front of the command before classifying, and never consults it.

**What it means.** A session doing legitimate work on its own branch — the one case
CLAUDE.md's rule 5 explicitly permits, since the rule forbids force-pushing a branch that is
*not yours* — hits a wall, reads the guard's own instructions for getting past it, follows
them exactly, and hits the same wall. There is no worse shape for a guardrail. The mode B
prompt says why: a guard that refuses legitimate work "teaches people to switch the whole
mechanism off, which is how protection actually gets lost." The session's next move is
`--no-verify` or disabling the hook, and now nothing is guarded at all.

**Fix:** consult the variable when it is present in the command's leading assignments, or —
better, since an env var in argv is easy to set by accident — remove the promise from the
message and say what to do instead. Either is fine; advertising a hatch that does not open is
not. **Test:** `ALLOW_FORCE_PUSH=1 git push --force origin work/x` must be allowed and
`ALLOW_FORCE_PUSH=1 git push --force origin main` must still be denied.

## N10. The guard's docstring claims coverage it does not have, on four counts.

**Where:** `.claude/hooks/guard.py` — the "what it blocks" docstring at the head of the file.

**Seats:** E-Sol (five live probes) and **E-Terra independently** (a different bypass vector —
Terra never saw Sol's probes and found its own).

**VERIFIED — I ran all of them.** Of Sol's and Terra's claims:

| Command | Guard's decision | Docstring says |
|---|---|---|
| `git config --remove-section core` | **allow** | it blocks disabling git hooks |
| `git send-pack origin HEAD:main` | **allow** | it blocks direct pushes to main |
| `python3 -c` with `urllib.request` POST to the RunPod API | **allow** | it blocks RunPod API writes |
| `node -e` with `fetch(..., {method:'POST'})` to the RunPod API | **allow** | it blocks RunPod API writes |

**What it means.** GOVERNANCE 10: "Claims are made only about what was actually measured."
The guard names four protections in its own docstring and does not deliver any of them
completely. `git send-pack` is the plumbing command behind `git push` and reaches `main`
without the word "push" appearing; `--remove-section core` disables hooks without the string
`core.hooksPath` appearing. The RunPod gap matters most in money terms — the guard dispatches
Python only through a `requests`/`httpx` pattern and does not dispatch `node` at all, so the
two most obvious ways to write a small script that starts a billed pod are both invisible to
it. GOVERNANCE 8 is the rule this is protecting.

I checked the surrounding claims too, and the guard is *not* generally broken: `git push
origin main`, `git push --force origin main`, `git -c core.hooksPath=/dev/null push origin
main` and the `GIT_CONFIG_COUNT` environment form are all correctly denied with clear
reasons. The problem is four specific unhandled forms, plus a docstring that promises
completeness.

**Fix:** two separable pieces. First and immediately, soften the docstring to describe the
class of phrasing it catches rather than claiming the operations — that is a documentation
fix and it discharges GOVERNANCE 10 today. Second, add the four forms: `send-pack`,
`remove-section core`, and dispatching `node` and bare `urllib` alongside `requests`/`httpx`.
**Test:** each of the four rows above must return a deny decision, asserted on the decision
payload and not on the exit code.

---

# TYREL DECIDES

Seven items. Each is a governance, cost or policy judgement that CLAUDE.md hard rule 1
reserves to him, or a case where a rule and a goal pull apart, which GOVERNANCE 0 says must
be handed to him rather than resolved in the moment.

## T1. Hard rule 10 has no mechanical enforcement.

**Where:** `.claude/settings.json` (whole file); `.claude/agents/worker.md`,
`infra-worker.md`, `rebuilder.md` frontmatter.

**Seats:** A-Sol and A-Terra, both as their top or near-top finding. The mode prompt asked
for it directly, so agreement here is weaker evidence than it looks.

**VERIFIED — I read all five files at 6b42def.**

CLAUDE.md hard rule 10, added in this very diff:

> 10. **A spawned agent never edits the governing documents.** This file, GOALS, GOVERNANCE,
>     ARCHITECTURE, GLOSSARY and the root README. An agent may propose a change to any of
>     them, with exact wording, in its report; it may not make one.

Three facts, each checked:

1. `.claude/settings.json` runs the guard hook only on `"matcher": "Bash|mcp__.*"`. The
   `Write` and `Edit` tools are not matched, so the guard never sees a file edit at all.
2. The file ends with `"permissions": { "deny": [] }` — an empty deny list. Nothing is denied.
3. `worker.md`, `infra-worker.md` and `rebuilder.md` each declare
   `tools: Read, Write, Edit, Grep, Glob, Bash`. All three can write any file in the repo.

**What it means.** The rule that says an agent may not rewrite the rules is written down and
nothing enforces it. A worker asked to "tidy the docs" could edit CLAUDE.md, GOVERNANCE.md or
GOALS.md today, and the only thing that would catch it is a human reading the diff. The rule
is a convention, not a guard.

**Why this is his call and not a FIX NOW.** Hard rule 1 reserves "amending a canonical
document" to him, so how tightly to wire that is his decision. The rule also contains a
deliberate exception — "The main session may edit this file" — that a blanket path deny would
break. And an untested deny rule is worse than none: a path deny that silently matches
nothing would leave everyone believing the hole is closed.

**Recommendation to put to him:** wire it, as its own small piece of `infra-worker` work
after this branch — add `Write` and `Edit` to the guard's `PreToolUse` matcher and have the
guard refuse writes to the six named documents when the caller is a spawned agent, with a
test that proves the refusal actually fires. Not a drive-by fix on this branch.

## T2. `ALLOW_UNAUDITED_PUSH=1` is available to any agent that holds Bash.

**Where:** `.githooks/pre-push` — the env-var escape hatch; `README.md` acknowledges the
credential weakness.

**Seats:** A-Sol, A-Terra.

**VERIFIED by reading `pre-push`.** The gate is `[ "$ALLOW_UNAUDITED_PUSH" != "1" ] && [
"$reviewers" -lt 3 ]`. Any process that can set the variable is past it.

**What it means.** CLAUDE.md already says this out loud and does not pretend otherwise: "The
gate is discipline, not machinery. A receipt proves a file was written, not that anything
was read. `ALLOW_UNAUDITED_PUSH=1`, `--no-verify` and `-c core.hooksPath=` all get past it."
So the auditors have not found a hidden hole; they have found a documented one. The
question — whether the hatch should be narrowed now that spawned agents hold Bash, given
that CLAUDE.md separately forbids subagents from pushing at all — is a policy call.

**Recommendation:** leave it, and rely on the existing rule that subagents never push. The
alternative (removing the hatch) would strand a legitimate reduced-coverage push, which
CLAUDE.md explicitly provides for.

## T3. The guard blocks pod launch unconditionally, with no way to represent granted permission.

**Where:** `.claude/hooks/guard.py` — RunPod create/start classification.

**Seats:** A-Sol (high), A-Terra (medium). Both raised it independently.

**UNVERIFIED (relayed) — I read the classification exists but did not exercise a permitted
launch path.**

**What it means.** GOVERNANCE 8 says "A live pod requires Tyrel's explicit permission **for
that session**." The guard has no notion of a session in which permission has been granted,
so it refuses the launch even after he has said yes. This is a textbook GOVERNANCE 0 case:
obeying the guard defeats the purpose the guard serves, because a session that cannot launch
a pod he authorised will reach for a wrapper or an env-var bypass, and the next launch happens
outside the guard entirely. Both seats named that consequence.

**Why his call:** any mechanism that represents "permission granted this session" is a
mechanism that can be forged by the thing it constrains, and GOVERNANCE 8 says shutdown is
"verified against provider state and billing, never inferred from an acknowledgement." He may
well prefer the current hard block plus a manual out-of-band launch.

**Recommendation:** leave the hard block, and add one line to the guard's refusal text telling
the session what to do instead (ask him to launch it himself), so the refusal does not read as
a bug to be routed around.

## T4. The Codex seat wrapper tells every seat to "triage the highest-value work first".

**Where:** `operations/codex/seat.sh:239`, applied to the `audit-sol` and `audit-terra` seats
via `operations/codex/seats.conf`.

**Seats:** **Sol only** (mode A) — and Sol noted the instruction applied to the very run
producing its own report.

**VERIFIED.** Line 239 reads "Triage the highest-value work first, and do not begin anything
you cannot finish". Line 219 gives the reason: "A seat blind to its own deadline cannot triage
against it."

**What it means.** GOVERNANCE 10: "**The instrument may not constrain what it measures.** A
grading prompt that sets a severity floor, budgets a confidence level, or tells a reader which
way to argue reports the instruction rather than the finding. Ask for everything and filter
afterwards." Telling a time-boxed seat to work highest-value-first is, under pressure, a soft
severity floor — the small and cosmetic findings are the ones that get dropped when the clock
runs out. Against that stands a real operational fact recorded in the user's own notes: an
agent blind to its own deadline cannot triage, and a timeout deletes the report rather than
degrading it. Both concerns are true and they pull opposite ways.

**Recommendation to put to him:** keep the deadline, change the triage sentence to bound
*scope* rather than *severity* — "finish and report what you have started before the deadline;
do not leave a finding unwritten because it seemed small." That preserves the anti-timeout
purpose without telling the instrument which findings matter.

## T5. RUN_PLAN's dossier prompt asks for "every critical and high defect".

**Where:** `workbench/active/RUN_PLAN.md:189` and the dossier prompt at `:533`.

**Seats:** **Sol only** (mode A), rated high.

**VERIFIED — text confirmed, but I read it differently from Sol.** The clause is item 4 of a
six-part dossier: "**Every critical and high defect** recorded in this system's verdict
files, each mapped to a named invariant/test in the new build or explicitly marked
not-applicable with a reason. (This retires the audit's untriaged 5-critical/57-high backlog
system by system.)"

**What it means.** This is not an instrument being told what to report. It is a *work-scoping*
instruction about an already-existing backlog of already-classified defects: retire the 5
critical and 57 high items first. GOVERNANCE 10's prohibition is on a grading prompt that sets
a floor before the finding exists. Nothing here stops a dossier from recording a medium
defect. I would have called this REJECT.

**Why it is here rather than in REJECT:** the wording is a hair from the thing GOVERNANCE 10
actually forbids, and RUN_PLAN is now a tracked, published plan that future sessions will read
literally. Whether to reword it is a judgement about his own plan, not mine.

**Recommendation:** add four words — "at minimum, every critical and high defect" — and the
ambiguity disappears at no cost.

## T6. The Perlector is planned to see witnesses as anonymous slots.

**Where:** `workbench/active/RUN_PLAN.md:411` — "witnesses as anonymous slots (blinded arms —
the old repo's own training-side rule)".

**Seats:** **Sol only** (mode A), rated medium and explicitly marked arguable.

**VERIFIED — text confirmed.**

**What it means.** GOVERNANCE 7 says the pipeline's obligation is to "**feed it completely and
measure it honestly**", and GOVERNANCE 6 says every stored reading carries the identity of the
model that produced it. Sol reads deliberately withholding witness identity from the Perlector
as incomplete feeding. The countervailing reading is right there in the same sentence: blinding
the arms is how you stop the Perlector learning "witness 2 is usually right", which is
precisely the picker GOVERNANCE 3 forbids — and the plan says as much two lines later, "a
reading constrained to a witness's offering is a pick with extra steps".

So this is two governance rules genuinely pulling against each other, which is exactly what
GOVERNANCE 0 says to hand upward: identity travels with the *record* (rule 6) while the
*reader* is kept blind (rule 3). Both can hold at once — the stored testimonium keeps its
provenance, the Perlector's prompt does not show it — but somebody has to say so.

**Recommendation:** confirm that reading, and write the reconciliation into the plan in one
sentence so no future session re-litigates it.

## T7. The document allowlist accepts, and refuses, the wrong things at the edges.

**Where:** `.githooks/doc-allowlist.sh:32-69`.

**Seats:** B2-Sol, B-bypass-Sol (accepted-that-should-not-be); B2-Sol also found the
refusals; C-Sol found the archived-HANDOFF case.

**UNVERIFIED (relayed).** The claims: `*/README.md` and `*/HANDOFF.md` match at any depth, so
a note named `README.md` in an ignored drawer can be force-added; file types outside the
enumerated list (`.org`, `.html`, stray `.txt`) pass as ordinary data rather than being judged
as documents; and in the other direction `CHANGELOG.md` and a genuine skill-reference document
were refused.

**Why his call:** CLAUDE.md defines what counts as a committed document — "a canonical
document, a `README.md`, a `HANDOFF.md`, dated evidence under `history/`, or a declared
harness document — nothing else." Widening or narrowing that list is amending a rule, not
fixing a bug. The over-refusals matter more than the over-acceptances, for the reason the mode
prompt gives: a guard that refuses legitimate work teaches people to switch the mechanism off.

**Recommendation:** treat the over-refusals as a FIX LATER bug list and leave the definition
alone.

---

# FIX LATER

Real, not blocking this branch. Grouped by area, with seats and verification status. Where an
auditor's fix is obvious it is given in one clause.

## The guard (`.claude/hooks/guard.py`)

- **L1. Shell indirection defeats the classifier broadly.** Variables, `$()` and backticks,
  git aliases, `find -exec`, `xargs`, `env -S`, curl config files, a renamed or symlinked
  binary, ANSI-C quoting (`g$'\x69't`), glob paths (`/usr/bin/g[i]t`), and `python -m` all get
  past basename dispatch. *Seats: B2-Sol, B2-Terra, B-bypass-Sol — all three, with tables of
  probed strings.* **UNVERIFIED (relayed), but I confirmed one member of the class myself
  (N4).** This is a category, not a bug: a text classifier over shell cannot be made complete.
  The honest fix is to the docstring, not the code — say what class of phrasing it catches.
- **L2. Destructive git cleanup is unclassified.** *Seat: B2-Sol only.* **VERIFIED — I ran all
  three.** `git reset --hard HEAD`, `git restore .` and `git worktree remove --force` are each
  allowed with no decision emitted; there is no branch for reset, restore, checkout, switch,
  worktree-remove, `branch -D` or stash drop. **Of everything below the FIX NOW line, this is
  the one an ordinary unattended session is most likely to hit by accident, and it destroys
  work that is not recoverable.** It sits here only because it is a new capability rather than
  a broken promise — the guard never claimed to catch these. If any single FIX LATER item gets
  promoted, promote this one.
- **L3. Hooks can be disabled without naming `core.hooksPath` — partly.** *Seats: B2-Sol
  (chmod/mv/rm of the hook files), B2-Terra (the `GIT_CONFIG_COUNT/KEY/VALUE` environment
  override).* **PARTLY REFUTED — I ran the env-override case and the guard denies it**, with
  the correct reason. Terra's specific claim is wrong. Sol's remaining cases (removing the
  exec bit, moving or deleting a hook file) are **UNVERIFIED** and plausible, and
  `git config --remove-section core` is verified allowed — that one is promoted into N10.
- **L4. Generic MCP tool names bypass classification entirely** because `tool_input` is never
  inspected — a tool called `mcp__runpod__request` carrying a pod-create payload passes.
  *Seat: **B2-Terra only**.* **UNVERIFIED.** Terra's mirror-image of Sol's L5 and the better
  half of the pair: Sol looked at names that say too much, Terra at names that say nothing.
- **L5. MCP names containing "start" or "create" are refused even when read-only** — e.g. a
  hypothetical `get_started_guide`. *Seat: B2-Sol.* **UNVERIFIED.** Over-refusal.
- **L6. RunPod host detection is substring-based**, so an unrelated request whose *body*
  mentions a RunPod host is wrongly refused. *Seat: B2-Terra only.* **UNVERIFIED.**
- **L7. Over-refusals that push a session toward a broader bypass:** implicit `git push` to a
  configured safe upstream; heredocs attached to anything but cat/tee; heredocs with a
  descriptive quoted delimiter (`END-JSON`); `cd` and `sed` misattributed as a heredoc's owner.
  *Seats: B2-Sol (full set), B-bypass-Sol (the delimiter and misattribution cases).*
  **PARTLY VERIFIED, and I hit one myself while assembling this ledger** — a shell loop of mine
  was refused with "a heredoc attached to 'cd' is opaque to the command guard", which is
  exactly the misattribution B-bypass-Sol described. **B2-Terra's separate claim that
  `git config --show-origin` reads are refused is REFUTED: I ran it and it is allowed.** The
  mode prompt ranks over-refusals highly on purpose and so do I — they are the mechanism by
  which protection actually gets lost, and unlike most findings here I have first-hand
  evidence this one fires in ordinary work.
- **L8. Promoted to N9** — verified, and the hatch genuinely does not work.
- **L9. The guard launcher fails open if the guard cannot start.** `settings.json` invokes
  `python3 guard.py`; a missing interpreter or an import error returns 127, and Claude's hook
  protocol treats anything but 0/2 as "proceed". *Seats: B2-Sol, C-Sol.* **UNVERIFIED
  end-to-end (neither seat ran a live session, nor did I).** If confirmed this is the worst
  defect class in the repository by the project's own standard, and it should be re-checked
  before dismissing.
- **L10. Denial reasons are never asserted by any test**, so the guard could give a
  misleading reason for a correct refusal and stay green. *Seat: D-Sol.* **UNVERIFIED.**

## The push gate and receipts

- **L11. Three "distinct" reviewer names can be three spellings of one.** Case, whitespace and
  Unicode look-alikes all survive `sort -u`; no vendor structure is required. *Seats: B2-Sol,
  B2-Terra, B-bypass-Sol — all three, and B2-Sol ran the pipeline and observed 3.*
  **UNVERIFIED by me, but the code confirms the mechanism** — `sed | sort -u | wc -l` over the
  `auditor:` lines. The code's own comment defends counting names rather than matching them,
  and CLAUDE.md agrees the gate "is discipline, not machinery". Normalising case and
  whitespace before `sort -u` is a cheap improvement that costs nothing.
- **L12. The reviewer count fails open if `wc` or `tr` fails.** A non-numeric `$reviewers`
  makes `[ "$reviewers" -lt 3 ]` error rather than evaluate true, and the block is skipped.
  *Seats: B2-Sol, B-bypass-Sol, C-Sol — Sol tested it directly.* **VERIFIED by reading** — the
  shape is real; the trigger (a missing coreutils binary) is not something an ordinary session
  meets. Fix by defaulting to a refusal when the value is not a number.
- **L13. `pre-push` exits 0 on empty or malformed stdin.** An empty ref list, or a final
  record with no trailing newline, skips every check. *Seats: B2-Sol, B2-Terra, B-bypass-Sol,
  C-Sol.* **UNVERIFIED.** Git supplies well-formed input, so likelihood is low.
- **L14. `git replace` refs let the scanned object differ from the pushed object.** Nothing
  sets `GIT_NO_REPLACE_OBJECTS=1` or `--no-replace-objects`, so a replacement ref could show
  reviewers and scanners a clean substitute while the real object graph ships. *Seat:
  **B-bypass-Sol only** — the attack-framed run, which called it the most consequential thing
  it found.* **VERIFIED that the flag appears nowhere in `.githooks/`, `.claude/` or
  `operations/`.** The mechanism is real Git behaviour and it defeats the receipt, the outgoing
  scan and the ancestry check at once. It is purely adversarial — no accident produces a
  replace ref — which is why it sits here rather than in FIX NOW. The fix is one environment
  variable in each scanning call.
- **L15. Merges and `git am` bypass `pre-commit` entirely.** `install.sh` installs
  `pre-commit`, `pre-push` and `commit-msg`; Git runs `pre-merge-commit` and
  `pre-applypatch` for those paths instead, and none exists. *Seat: **B-bypass-Sol only**.*
  **VERIFIED — `install.sh:43` chmods exactly three hooks.** `pre-push` still gates the
  result, and hard rule 3 forbids committing to main anyway, so the exposure is small.
- **L16. Commit author and committer header fields are never scanned** — the scanner reads
  `%B`, the message body, only. *Seat: **B-bypass-Sol only**.* **UNVERIFIED.**
- **L17. Plain `--force` can still win a race** against the ancestry check, which compares the
  remote-advertised SHA at invocation time. *Seat: B-bypass-Sol only.* **UNVERIFIED**; this is
  an inherent client-side race, not a defect in the hook.
- **L18. Attribution is checked for syntax, not for machinehood.** Any name plus an
  email-shaped trailer passes, including a human's. *Seats: B2-Sol, B-bypass-Sol.*
  **UNVERIFIED.** CLAUDE.md says `commit-msg` "enforces authorship only", so this is arguably
  the documented behaviour rather than a defect — but the pass rate for a human name is worth
  knowing.
- **L19. `record-audit.sh` installs the receipt before a later step that can fail**, so a
  failure report can follow a permanently installed receipt and invite a duplicate retry.
  *Seat: C-Sol only.* **UNVERIFIED.**

## The credential scanner (`.githooks/check_ingress.py`)

- **L20. Realistic credential shapes are not recognised.** AWS secret and session keys, Slack
  webhook URLs, Stripe restricted and webhook keys, PyPI tokens, JWT bearer headers, database
  URIs with embedded passwords, Google OAuth secrets, RunPod API keys — the generic fallback
  pattern needs a word boundary that vendor-prefixed names defeat. *Seats: B2-Sol, B2-Terra,
  B-bypass-Sol — all three, each tested with synthetic values.* **UNVERIFIED by me** (I did
  not construct credential-shaped test inputs). This is the largest single agreed gap in the
  pass and the one most likely to bite by accident: pasting a Slack webhook into a note is an
  ordinary mistake.
- **L21. FIFOs, sockets and devices in the worktree are skipped without a word.** The scanner
  branches on symlink, file and directory; anything else falls through unclassified and the run
  still reports passed. *Seats: C-Sol, C-Terra.* **VERIFIED — I read `working_tree()` at
  6b42def and the fall-through is exactly as described.** GOVERNANCE 2 wants a partial result
  to be visibly partial; this one is invisible. Low impact, cheap fix: count and report skips.
- **L22. A FIFO at a scanned or receipt path hangs the process indefinitely.** `[ -f ]` is
  false for a FIFO so validation is skipped, but the later `grep`/`read_bytes()` opens it
  anyway and blocks with no writer and no timeout. *Seats: C-Sol, C-Terra.* **VERIFIED by
  reading** `pre-push`'s `grep -qx ... "$receipt"`. Exotic trigger; real hang.
- **L23. TOCTOU: a regular file swapped for a FIFO between the type check and the read.**
  *Seat: **C-Terra only**.* **UNVERIFIED.** Narrow race, same hang.
- **L24. Two unbounded-memory paths** — an unbounded `lru_cache` on blob data, and a full
  worktree file read into memory *before* the size limit is enforced. *Seat: C-Sol only.*
  **UNVERIFIED.** A very large file crashes the scanner instead of producing the clean
  oversize diagnosis it was built to produce.
- **L25. Findings past the hundredth cannot be retrieved.** The report slices to 100 and prints
  a remainder count with no way to see the rest. *Seat: A-Sol only.* **UNVERIFIED.** Visibly
  partial, so GOVERNANCE 2 is technically satisfied; still a dead end for whoever has 150.
- **L26. `commit-msg` reports every scanner failure as "credential match"**, including a
  scanner that could not run. *Seat: C-Sol only.* **UNVERIFIED.** Blocks correctly, diagnoses
  wrongly.
- **L27. The attribution exemption checks only that a Git state file exists**, not that it is
  a valid regular file, so stale state could exempt a hand-written commit. *Seat: C-Sol only.*
  **UNVERIFIED.**

## The notifier

- **L28. The start-stamp check does not validate what it found.** A directory, FIFO, symlink or
  future-dated object at the stamp path counts as evidence of a recent send and suppresses the
  real notification. *Seat: C-Sol only.* **VERIFIED by reading** — `find "$stamp" -mmin -15`
  with no type test.
- **L29. The config check follows symlinks despite a comment saying it will not.** The comment
  explains that `-f` was chosen over `-r` so a named pipe cannot block a session; it does not
  stop a valid symlink pointing elsewhere. *Seat: C-Sol only.* **VERIFIED — the comment and
  the test are both as quoted.** The comment is narrower than it reads, which makes it a small
  truthfulness defect too.
- **L30. The one-line message check rejects a newline but not a carriage return.** *Seat: C-Sol
  only.* **UNVERIFIED.** Cosmetic.

## The Codex seat wrapper

- **L31. A hard-killed seat is not reported as a timeout.** The code tests for exit 124; a
  process that ignores TERM and is escalated to KILL returns 137, so the "hit the ceiling"
  explanation is skipped and it looks like an unexplained crash. *Seat: **C-Terra only**.*
  **UNVERIFIED.** This is a good catch and directly relevant — this very audit pass ran seats
  under a ceiling.
- **L32. An empty temporary tray is left behind when `codex` is absent**, because the tray is
  created before the dependency check. *Seat: **C-Terra only**.* **UNVERIFIED.** Debris.
- **L33. The wrapper's bounds are weaker than they look:** no maximum timeout, no model
  allowlist, `timeout` and `codex` resolved from ambient PATH and therefore shimmable, and a
  `--kill-after=10` grace beyond the stated hard deadline. *Seats: B2-Sol, B2-Terra,
  B-bypass-Sol.* **UNVERIFIED.**

## Tests

The mode D reports are the largest single block of findings and the least surprising: almost
all of them are *absent tests* rather than wrong ones. They are listed compactly because each
is the same recommendation — write the missing case.

- **L34. Everything the seat tests assert about `codex` is an assertion about a stub.** A
  misspelled flag passes the whole suite and kills every live seat at startup. *Seats: D-Sol,
  D-Terra, agreeing.* Sol names the concrete instance: `--skip-git-repo-check` misspelled is
  uncaught. **UNVERIFIED.** The stated remedy — a live smoke test that spends a few tokens
  against the real binary — is the single highest-value test in this list.
- **L35. Everything the notify tests assert is an assertion about a fake `curl`.** `-sS`
  mutated to `--silentt` passes. No test proves DNS, TLS, proxy behaviour, that ntfy accepts
  the payload, or that a phone rings. *Seats: D-Sol, D-Terra.* **UNVERIFIED.** The component's
  failure mode is silence, which is exactly what a fake cannot detect.
- **L36. The guard's live wiring is untested.** Changing the `settings.json` matcher from
  `"Bash|mcp__.*"` to `"Bash"` disables all MCP blocking, and misspelling the command path
  disables the guard entirely, while all 249 `guard.py` tests stay green. *Seats: D-Sol,
  D-Terra.* **VERIFIED that the matcher is the only wiring** (I read `settings.json`).
- **L37. Two CI gates can be disabled without failing a test** — commenting out the
  annotated-tag scan line, or changing the autoclave stray-count condition to `if false`, both
  leave the asserted substrings intact. *Seat: **D-Sol only**.* **UNVERIFIED.** These are
  substring locks masquerading as behaviour tests.
- **L38. CI's full-history secret scan has no test at all** — neither the `--history HEAD`
  invocation nor the `fetch-depth: 0` it depends on. *Seat: **D-Terra only**.* **UNVERIFIED.**
  Terra's counterpart to L37, and a different command entirely.
- **L39. Most credential patterns have no regression test.** Roughly thirteen provider patterns
  are declared; two are covered. Deleting or corrupting the OpenAI rule is uncaught. *Seats:
  D-Sol, D-Terra.* **UNVERIFIED.** Directly compounds L20.
- **L40. Nothing locks the declared-secret-fixture set at empty**, though the source declares
  that as its intended resting state. *Seat: D-Sol only.* **UNVERIFIED.** Relates to L46.
- **L41. The three-reviewer test's name claims more than its body proves.** A test named
  `..._requires_the_three_exact_reviewers` is contradicted by a sibling test showing any three
  distinct labels pass — which matches production, so the name is the defect. *Seats: D-Sol,
  D-Terra.* **UNVERIFIED.** Exactly the "false confidence" class the mode prompt asked for.
- **L42. Several load-bearing branches have no test**: pre-push gating restricted to branches
  only; tag pushes always bypassing with the env hatch; force-push ancestry with a real
  divergent history (reversing the two arguments to `merge-base --is-ancestor` would be
  uncaught); a malformed `ALLOW_DETACHED_COMMIT=0` slipping past the exact-`1` check; the
  installer's executable-bit wiring; the detached-HEAD tests invoking the hook directly rather
  than through a real commit. *Seats: D-Sol (the first four), **D-Terra (the last two)**.*
  **UNVERIFIED.**
- **L43. The paired audit seats have no equivalence test.** The seat resolution test uses the
  same config file as its own oracle, so `audit-terra`'s effort, sandbox or ceiling could drift
  away from `audit-sol`'s and the suite would stay green. *Seats: D-Sol, D-Terra.*
  **UNVERIFIED.** This one is self-referential: it is the invariant that makes *this* mirrored
  pass a fair comparison.
- **L44. Four tidy tests would pass against an empty function** — each asserts only an absence.
  *Seat: D-Sol only.* **UNVERIFIED.**
- **L45. The executable Markdown procedures are essentially untested.** The session-start,
  session-end and reviewer-pass skills are procedures a session executes; one test checks three
  literal shell strings. *Seats: D-Sol, D-Terra; Terra names the specific missing cases.*
  **UNVERIFIED.**
- **L46. Exact-prose and YAML locks are unevenly intentional.** Both seats judge the tag-scan
  substring lock accidental (innocent reformatting fails it; disabling the command passes it)
  and the autoclave and reviewer-pass locks deliberate but fragile. *Seats: D-Sol, D-Terra.*
  **UNVERIFIED.**
- **L47. A grab-bag of untested smaller branches** — merge/revert/cherry-pick exemptions, the
  `ALLOW_MAIN_COMMIT=1` hatch, installer `mkdir -p` failure, many manifest validations, notify
  extra-argument handling, no schema validation of the workflow file. *Seat: D-Sol only.*
  **UNVERIFIED.**
- **L48. The wheel build's offline guarantee is not proven end to end** — the offline test
  calls a helper directly, so replacing the caller with an online command would leave the
  helper test green on dead code. *Seats: D-Sol, D-Terra.* **UNVERIFIED.**
- **L49. One redundant seat test** — a "no seat spends ultra" case already subsumed by the
  effort-set it draws from. *Seat: **D-Terra only**.* **UNVERIFIED.** Cosmetic, recorded
  because nothing is dropped.

## Documentation truthfulness (mode E)

Mode E's biggest findings were promoted above: N1 (the notifier docs), N3 (the register-text
claim), N5 (the pinned-model claim) and N10 (the guard docstring) all came from or were
corroborated by mode E. What remains:

- **L52. The tracked `workbench/active/` notes contradict each other and the repository.**
  Commit counts disagree across documents and against the audited commit; file counts disagree
  (7 vs 6 vs the actual 10 tracked); "byte-identical to 1001db7" and "all gitignored" claims
  went stale when CLAUDE.md and `.gitignore` changed in this same commit; a CLAUDE.md
  line-count claim and a `maxTurns` claim are simply wrong; `DECISIONS_FOR_TYREL.md` lists a
  question about whether to track `active/` as unanswered, when tracking it is what this
  commit does. *Seats: E-Sol and E-Terra, jointly, across five separate findings.*
  **UNVERIFIED in detail; the shape is certain** since these were private notes last week and
  are published claims now. Judge them as the mode prompt says — by whether they mislead a
  reader, not by whether they are tidy. **Recommend a single cleanup pass over
  `workbench/active/` before the push, not ten separate corrections.**
- **L53. `README.md` says a status claim anywhere else is "wrong by construction" — and this
  commit tracks ten files full of status claims.** *Seat: **E-Terra only**, and it is the
  sharpest framing of the tracked-notes problem anyone produced.* **UNVERIFIED.** Sol circled
  the same territory repeatedly without ever isolating this one line. The resolution is
  probably one sentence in README.md exempting the working drawer, which is the same shape of
  fix as N2.
- **L54. It is unclear whether the `autoclave-empty` check is a required merge gate.**
  `DECISIONS_FOR_TYREL.md` says "flip after push lands", `PRE_REBUILD_INTENT.md` says "still
  unflipped", and `RUN_PLAN.md` plus `check-all.sh` describe it as a current merge gate.
  *Seat: **E-Sol only**.* **UNVERIFIED, and unverifiable from inside the repository** — which
  is the point. CLAUDE.md says README.md is the one place this is recorded; four other files
  record it too, differently.
- **L55. `ORCHESTRATION_FINDINGS.md` contradicts its own warning label.** It says to treat
  everything in a section as unverified until re-tested, and elsewhere that everything in it
  "was run, not inferred"; it then concludes self-orchestration is "now known-possible", and
  three other tracked documents repeat that as settled. `HANDOFF.md` records that the
  underlying sandbox result was not reproduced on a later attempt. *Seats: E-Sol, E-Terra.*
  **UNVERIFIED.** The `seat.sh` and `seats.conf` comments are honest about the result being
  contested; the notes are not.
- **L56. `CLAUDE.md`'s claim that `commit-msg` "enforces authorship only" is false** — the hook
  credential-scans every message before any attribution exemption applies. *Seats: E-Sol,
  E-Terra.* **UNVERIFIED, but the code shape supports it** (I read the scan-before-exempt
  ordering, which every B seat also certified as correct behaviour). The behaviour is right;
  the sentence understates it. One clause.
- **L57. Unbuilt components described in the present tense.** `pipeline/5_recensor/review/`
  describes a queue; the directory holds only the README. Other stage READMEs share the
  pattern but read as role descriptions rather than status claims. *Seat: E-Sol only.*
  **UNVERIFIED.** This diff rewrote about twenty such passages into the future tense; this is
  what the sweep missed.
- **L58. `doc-allowlist.sh` misidentifies its own callers** — it names `pre-commit` and
  `ci.yml`, but CI reaches it through `check-documents.sh`, and `check-all.sh` calls it locally
  with a wider scope. *Seat: E-Sol only.* **UNVERIFIED.** Notable because the file carries its
  own note that a duplicated fact went stale once before.
- **L59. Reverse defects — the code refuses things no document mentions.** A bare `git push`
  with no refspec is denied whether or not it targets main (E-Sol, confirmed live);
  destructive AWS S3 operations are denied and AWS appears nowhere in the guard's docstring
  (E-Sol and E-Terra, both); `start` notifications are silently suppressed for fifteen minutes
  and CLAUDE.md's notification section does not say so (E-Sol and E-Terra, both);
  `check-documents.sh` includes untracked files in its local scope, contradicting
  `doc-allowlist.sh`'s description (E-Sol). **UNVERIFIED by me.** The mode prompt's reasoning
  applies: "An undocumented refusal is how a session ends up fighting its own tooling."
- **L60. `agents/README.md`'s "what is pinned" table omits `name` and `description`**, which
  every one of the six agent files carries. *Seats: E-Sol, E-Terra.* **UNVERIFIED.** Cosmetic.
- **L61. `auditor.md` says the receipt "uses that seat's fixed label"; the reviewer-pass skill
  requires the receipt to name the model that actually answered.** *Seat: E-Sol only.*
  **UNVERIFIED.** These may be reconcilable, but as written they point different ways about the
  one record that proves a review happened — worth ten minutes.
- **L62. Measurement claims with no retained artefact.** Both seats catalogued comments in this
  diff that assert a probe result or a timing experiment: "236,000 tokens across 54 tool
  calls"; "439 tests passed" (the current collection is 448, and no run log is tracked either
  way); "four rounds of adversarial audit found a new way past every version"; "open stdin hung
  forever"; "stale variables ran the wrong policy"; "environment-only was tried"; "spent money
  through an external API for a whole session before anything linted it"; an "~80k token"
  investigation cost. *Seats: E-Sol (eight items), E-Terra (two more).* **UNVERIFIED — and
  unverifiable, which is the finding.** GOVERNANCE 10 does not forbid a comment recording why
  a thing was built; it forbids claiming a measurement that was not made. Most of these read as
  honest institutional memory. The two that state numbers ("236,000 tokens", "439 tests")
  should either cite a retained artefact or lose the number.

## Everything else

- **L50. The reviewer-pass cleanup trap does not exit.** It deletes the temporary file on
  HUP/INT/TERM and then the script resumes. Separately, command substitution strips trailing
  newlines so the saved evidence is not byte-identical to what the reviewer produced, and
  `[ ! -e ]` does not protect a dangling symlink from being overwritten. *Seat: C-Sol only.*
  **VERIFIED by reading the skill** — the trap and the `$(...)` capture are as described.
  Evidence fidelity matters here more than usual: this is the file that proves a review
  happened.
- **L51. Assorted small correctness items:** `tidy.py`'s markdown link parser mis-splits an
  angle-bracketed path containing a space; `build_wheel.py` strips general whitespace from the
  repo root path and lets non-`RuntimeError` exceptions escape as raw tracebacks; the CI
  document-policy step runs Python before `setup-python` pins the version;
  `check-documents.sh` follows a symlink where it means to test a regular file; an archived
  `HANDOFF.md` can be force-added past the allowlist; the guard's `tidy.py` race window
  between digest recheck and move. *Seats: C-Sol (most), C-Terra (the tidy race, independently,
  plus a second target-existence race Sol missed).* **UNVERIFIED.**

---

# REJECT

Recorded, with the reason. Nothing here is dropped — hard rule 7 — but none of it should
consume effort.

- **R1. "RUN_PLAN.md contains byte-identical legacy strings, proving quarantine breach"**
  *(A-Sol, rated critical — its top finding).* **VERIFIED and REJECTED as stated.** Sol is
  factually right that `PURE ABSOLUTE, STOP AT 3` appears verbatim in both
  `RUN_PLAN.md:433` and three files under `/Users/tyrel/ocr_pipeline/`, and I confirmed that
  myself. But the string is a quotation of *Tyrel's own ruling* about how many hard failures a
  run may accumulate — a human decision, not old code. `LOAD_TRUNCATED_IMAGES` is the name of a
  public library setting, and the plan's sentence says never to set it. The quarantine forbids
  old *code* crossing; it does not forbid recording what the human decided. Sol's severity
  rating is the error, not its observation. **The related concern about "port" language is real
  and appears below as D1.**
- **R2. "`workbench/active/` should not be tracked."** Implied by several seats.
  **VERIFIED and REJECTED.** `.gitignore:31-41` records the decision and the reason at length,
  attributed to Tyrel. It is a ruling, not an oversight. The defect is that
  `workbench/README.md` does not say so (N2).
- **R3. "The autoclave gives `rebuilder` an unreviewed route for old code into git history"**
  *(A-Sol only, rated high).* **REJECTED as a finding; it is the design.** CLAUDE.md describes
  exactly this and defends it: "The tray is tracked so reviewers read the raw draft." The
  `autoclave/README.md` already states the scanner cannot recognise register text. Sol has
  restated the documented trade-off as a defect. The real residue — that history retains the
  raw draft after the tray is cleared — is inherent to a tracked tray and was chosen knowingly.
- **R4. "A single suspected secret destroys an entire GPT review"** *(A-Sol only, rated high).*
  **REJECTED — the behaviour is correct and the finding misreads it.** I read
  `reviewer-pass/SKILL.md` at 6b42def. On a failed credential scan the script unsets the
  variable and exits 1 with an explanatory message, and on a *successful* scan it writes the
  full output to a temporary file and moves it into place. Refusing to write a report that
  failed credential scanning is the correct fail-closed choice; writing it would put a possible
  credential on disk. The related genuine defect — that the trap does not exit on a signal, and
  that the capture is not byte-exact — is real and recorded as L50.
- **R5. "The secret-exclusion mechanism lets an agent approve an exclusion"** *(A-Sol only).*
  **REJECTED as currently constituted.** The exclusion set is empty and any addition to it is a
  tracked source change that lands through review — the same path as any other code. The
  underlying worry (that nothing *mechanically* reserves the decision to Tyrel) is the same
  worry as T1 and is answered there.
- **R6. "`route around it` in session-start invites routing past a reserved decision"**
  *(A-Sol only, marked ambiguous by Sol itself).* **REJECTED.** Read in context the sentence
  means "record what he cannot answer now and continue with what does not depend on it", which
  is what a session must do when he is asleep. The alternative reading Sol worries about is
  already forbidden three times over by hard rule 1 and GOVERNANCE 0.
- **R7. "The word 'migration' survives in session-start/SKILL.md"** *(A-Terra only, marked
  small).* **REJECTED as a defect, noted as taste.** The quarantine rule governs the
  *vocabulary of the rebuild* — "not a migration, not an import — the word is rebuild". Using
  "a migration" as a generic example of a large open-ended task in a skill file is not a claim
  about this project's method. Worth changing if the file is edited for another reason; not
  worth a commit.
- **R8. "Fresh clones have no hook protection until bootstrapped"** *(B2-Terra only).*
  **REJECTED — documented, not a defect.** CLAUDE.md states it in bold: "**Until
  `sh .githooks/install.sh` has run in this clone, every git-hook rule is off silently.**"
  Terra found the document, not a hole. Recorded because the fact is worth re-reading.
- **R9. "The stated test count is 439; the real count is 448"** *(D-Sol only, self-labelled
  cosmetic).* **REJECTED.** The number came from the audit prompt, not the repository.
- **R10. "`pre-push` cannot prove reviewer identity or independence"** *(raised in several
  shapes).* **REJECTED — the code says so itself**, in a comment, and CLAUDE.md says it twice.
  A mechanism that discloses its own limit accurately is not defective.
- **R11. "The receipt gate should match reviewer names against a pinned list."** **REJECTED.**
  The code's own comment gives the reasoning: pinning today's releases into a safety hook dates
  it at the next rename, and "a gate that rewards a false label is worse than no gate." The
  narrower fix (normalising case and whitespace) is L11.
- **R12. C-Terra's clean-list entry certifying the old-Git fallback sound.** **REFUTED** — see
  N6. Recorded here as a finding *about a report*, because a wrong all-clear is exactly the
  thing the pass exists to catch, and hard rule 7 covers reviews as well as defects.
- **R13. Two auditor bypass claims that are wrong, and one methodological warning.**
  **REFUTED, by running them.** B2-Terra claimed Git's `GIT_CONFIG_COUNT/KEY/VALUE`
  environment override defeats the guard's hook-disabling check — it does not; the guard denies
  it with the correct reason. B2-Terra also claimed `git config --show-origin` reads are
  wrongly refused — they are allowed. Separately, E-Sol reported five commands as "ALLOWED
  (exit 0)"; two of the five (`git -c core.hooksPath=/dev/null push origin main`, and an
  alias-expanded force push) are in fact **denied**. The likely cause is the one noted at the
  top of this ledger: the guard emits its refusal as JSON on stdout and exits 0 regardless, so
  a seat reading the exit code sees every refusal as an allow. Three of Sol's five probes were
  nonetheless real holes and are recorded as N10. **The lesson generalises beyond this pass:
  when an auditor reports a guard as bypassed, check how it read the guard's answer.**
- **R14. "Every commit in the range is correctly authored and co-authored"** *(E-Sol,
  clean-list).* Not a finding, recorded here so it is not lost: this is the one attribution
  check a seat performed and it passed.

## Deferred: the two RUN_PLAN items that need his eye

**D1. "Port" language in the tracked run plan.** `RUN_PLAN.md:205` and `:450` both say "the
textnorm port"; `:431` says "porting proven invariants into new names"; `:454` names
"`textnorm.py` + its harvested test suite". **VERIFIED — all four lines read as quoted.**
CLAUDE.md's quarantine says "never a paste, never a port". Against that, `:419` says "rewrite
against, not port", and `:93` records that Tyrel has already ruled on the one file this
concerns: "even this file enters through the full line-by-line review, like everything else.
Nothing is copied on trust." So the plan's *decision* is compliant and its *vocabulary* is
not. Because the plan is now tracked and published, the word will be read literally by a
future session. **Recommend: reword to "textnorm rebuild" and "rebuilding proven invariants
under new names", and let him confirm that is what he meant.** Filed here rather than in FIX
NOW because it edits his plan, not the code.

**D2. "The crossref winner-pick with retained losers survives."** `RUN_PLAN.md:453-454`.
*Seats: A-Sol (low, uncertain) and A-Terra (high).* **VERIFIED — the sentence reads as
quoted.** GOVERNANCE 3: "No stage selects a winner among witnesses," and CLAUDE.md hard rule 8
calls this "the one an agent rebuilds by accident." The sentence explicitly preserves something
called a winner-pick. Two things make this less alarming than it sounds: the plan's own
standing constraint 6 says "**Any evidence of picking** — in a dossier, a rewrite, a review
comment — stops the line it is on. G3 questions are never resolved locally; they go to Tyrel
with the file and line," and the same sentence kills the four-way pick beside it. Whether
"crossref" candidates are witness readings at all is not determinable from inside this
repository — there is no `crossref` entry in GLOSSARY.md. **This is a G3 question, and the plan
itself says a G3 question goes to Tyrel with the file and line. That is what this entry is.**

---

# The experiment

## Did Sol's extra volume buy extra findings?

Sol wrote 195KB across its reports, Terra 70KB — a 2.8× volume ratio. The finding counts are:

| Mode | Sol findings | Terra findings | Both | Sol-only | Terra-only |
|---|---|---|---|---|---|
| A governance | 17 | 9 (7 as written, 2 compound) | 7 | 10 | 2 |
| B2 robustness | 14 | 10 | 8 | 6 | 2 |
| C correctness | 22 | 6 | 3 | 19 | 3 |
| D tests | 24 | 14 | 10 | 14 | 4 |
| E truthfulness | 16 | 11 | 9 | 7 | 2 |
| **Total** | **93** | **50** | **37** | **56** | **13** |

So Sol's 2.8× volume bought roughly 1.9× the findings. The extra length is *mostly* extra
findings, not extra words — but not entirely. Sol writes each finding in a fixed six-part
format (what / where / observed / why it matters / confidence / what I would do) with quoted
code, which runs two to three times longer than Terra's single dense paragraph carrying the
same fields. Terra's findings-per-word ratio is materially higher in every mode.

**The honest summary: Sol is more exhaustive, Terra is more efficient, and the gap in findings
is about two-thirds the gap in bytes.** Roughly a third of Sol's extra volume is format, not
content. Neither model is a substitute for the other, because of the next section.

There is one quality asymmetry worth naming, and it does not favour volume. Sol ran more live
probes than Terra, which produced its best findings — and also produced the pass's only set of
**wrong** findings, because it misread the guard's exit code (R13). Terra's read-only style
produced fewer findings and no false ones, except a single wrong all-clear (N6) and a wrong
bypass claim. Both models made errors of the kind this ledger exists to catch, at roughly the
same rate per finding.

## What each model found alone

This is where the experiment paid, and it paid in both directions.

**Terra found, Sol missed:**
- **The false register-text filter claim in RUN_PLAN.md** (N3) — in mode A. Sol found the same
  thing independently in mode E. Neither model caught it in both of its own modes; the
  cross-pairing did. It is the most consequential single finding in the pass.
- **Bare `urllib` and `node -e` reach the RunPod API past the guard** (N10) — a bypass vector
  entirely different from the five Sol probed, and unlike two of Sol's, Terra's is real. I
  confirmed both.
- **The `README.md` "status lives in one place" contradiction** (L53) — the sharpest framing
  anyone produced of the newly-tracked-notes problem, and Sol circled it repeatedly in mode E
  without ever isolating the line.
- **Generic MCP tools bypass the guard because `tool_input` is never inspected** (L4) — the
  mirror image of a defect Sol found pointing the other way. Sol looked at names that say too
  much; Terra asked what happens when the name says nothing.
- **CI's full-history secret scan has no test at all** (L38) — a different command from the two
  CI gates Sol examined.
- **The installer's executable-bit wiring is untested, and the detached-HEAD tests bypass Git's
  own hook path** (L42) — two wiring gaps that Sol's much longer test audit never reached.
- **A hard-killed seat returns 137, not 124, and is not reported as a timeout** (L31) — found
  while auditing the very mechanism this audit pass ran under.
- Plus two smaller ones: an empty temp tray left behind when `codex` is missing (L32), a second
  TOCTOU window in `tidy.py` (L51), and a redundant seat test (L49).

**Sol found, Terra missed:** the long tail — 56 findings across five modes, of which the ones
that matter are the old-Git fallback that Terra explicitly certified clean (N6), the
`ALLOW_FORCE_PUSH` hatch that does not open (N9, verified), `git send-pack` and
`config --remove-section core` reaching past the guard (N10, verified), the receipt binding to
HEAD rather than to the reviewed commit (N7), `CLAUDE.md`'s pinned-model claim (N5),
destructive git cleanup being unclassified (L2, verified), the guard launcher failing open
(L9), and the two CI gates that can be disabled without failing a test (L37).

**The decisive data point is N6.** Sol found a real defect in `pre-push`'s compatibility
fallback and reproduced it. Terra read the same lines and listed them in its
*checked-and-found-clean* section. I re-ran the check and Sol is right. A single seat would
have shipped that as verified-sound. That is not a marginal gain from the second seat; it is
the entire justification for one.

## Was running the same prompt on both models informative?

**Yes — but not for the reason the design assumes.** The design's stated logic is that
agreement between two blind seats is corroboration. It mostly is not: the 28 agreed findings
are, with few exceptions, the ones the mode prompt pointed at directly. Mode A told both seats
to check whether hard rule 10 is enforced; both did; their agreement carries almost no
independent information. Corroboration on a directed question is close to worthless.

The value came from the **disagreements and the non-overlap** — the 60 findings only one seat
saw, and the one place where the seats actively contradicted each other and one of them was
wrong. That matches what is already in the session's own notes: two models agreeing on a
verdict is not agreement; what matters is findings overturned.

Mode E is the counter-example that saves the design. There, the two seats overlapped on nine
findings and each brought its own — because the mode's question has a surface too large for
one model to walk. **The rule that falls out: mirror the modes whose search space is large and
undirected; run a single seat on the modes whose prompt already names what to look for.**

**Recommendation: do it again, with three changes.**
1. **Keep two seats. Drop the expectation that agreement means anything.** Score the pass by
   non-overlap and contradiction, not by consensus.
2. **Make the mode prompts less directive.** Every question the prompt names produces agreement
   that proves nothing. The unprompted findings — Terra's register-text catch, Sol's old-Git
   fallback — are what justified the spend, and neither prompt asked for them.
3. **Assign the seats asymmetrically.** Terra is the better value per token and caught things
   Sol's greater volume did not reach. Running Terra on all five modes and Sol on the two
   highest-stakes ones would have found most of this at roughly half the cost.

## The B-mode framing experiment

This is the sharpest result in the whole pass and it was almost incidental.

The same model, Sol, audited the same guards twice: once told to *defeat* them
(`B_bypass`), once told to *review* them defensively (`B2_robustness`). The attack framing
found five things the defensive framing did not: the heredoc injection (N4, which I
reproduced), `git replace` refs (L14), unscanned commit header fields (L16), merges bypassing
`pre-commit` (L15), and the force-push race (L17). The defensive framing found three the
attack framing did not — but all three are *reliability* defects (launcher fail-open, receipt
binding, unclassified cleanup commands), not holes.

Most tellingly: the defensive run examined heredoc handling and reported only that it was too
*tight*. The attack run examined the same code and found it was also too *loose*, in the exact
construct the guard's docstring advertises as safe. **The framing determined what was found,
not the model or the code.** A defensive reviewer catalogues over-refusals; an adversarial one
builds working chains.

The cost of the attack framing is that OpenAI's safety classifier killed the Terra seat, which
is why B_bypass has no pair. **Recommendation: keep both framings for the guard mode, accept
that the adversarial one will lose seats to classifiers, and run the adversarial pass
single-seat rather than trying to mirror it.** It found more per report than anything else in
the pass.

## Which modes earned their seat

**A (governance) — earned it, narrowly.** Its two best findings (rule 10 unenforced, the false
register-text claim) are the two the ledger leads with. But most of its output was the
directed questions answering themselves, and several of its high-rated findings are restatements
of documented trade-offs (R3, R5, R10). Run it again with less direction.

**B (guard robustness) — earned it twice over, and the adversarial framing earned it most.**
Highest density of real, reproducible defects. This is the mode to keep if only one survives.

**C (correctness) — earned it, on one finding.** N6 alone justifies the seat: a defect one
model found and the other certified clean. The rest of mode C is a long tail of exotic edge
cases (FIFOs at receipt paths, TOCTOU races) that are real but that no ordinary session will
meet. Sol's 22-to-6 finding ratio here is the widest in the pass and also the least
load-bearing.

**D (tests) — did not earn a second seat, and barely earned the first.** Twenty-eight findings,
of which twenty-six are "there is no test for X". That is a coverage report, and a coverage
tool produces it for nothing. The two findings worth the spend are the ones that could not come
from tooling: that the seat tests and notify tests are assertions about stubs, and that the
three-reviewer test's *name* claims more than its body proves. Next time ask mode D only for
mutation thinking and false-confidence names, and get coverage from a coverage tool.

**E (truthfulness) — earned both its seats, and was the best-value mode in the pass.** Nine of
the ten FIX NOW items are truthfulness defects, and mode E either found or corroborated five
of them, including the two most serious (N3 and N10). It is also the mode where the two seats
were most complementary rather than redundant: nine shared findings, seven Sol-only, two
Terra-only, and the two Terra-only ones are both good. The reason is structural — mode E's
question ("does this sentence match that code?") has an enormous surface and no model can walk
all of it, so two models genuinely cover more ground rather than confirming each other. Run
this one twice again.

**A general note on the modes.** The pass found far more *documentation* defects than code
defects, and that is not an artefact of mode E. Modes A, B, C and D each independently arrived
at the same top finding — three documents describing a notifier that works differently (N1) —
and the two most serious things in this ledger are both sentences claiming a protection that
does not exist. For a repository whose governance says "Claims are made only about what was
actually measured," that is the finding behind the findings.

---

# Checked and found clean

Collated from every seat's own coverage statement, so a reader can tell "audited and sound"
from "nobody looked".

**Examined by multiple seats and found sound:**

- **`guard.py`'s decision harness** — fails closed on malformed JSON, an array at top level, a
  null tool input, and non-string command payloads, returning a structured deny every time.
  Correctly denies literal `git push origin main`, `git -C . push origin main`, explicit
  RunPod creation, `env`/`xargs`/`sudo`-wrapped pushes, `rm` on protected paths, writes to
  protected endpoints via curl/wget/HTTPie, literal `--no-verify`, and deletion of workbench
  evidence; correctly *allows* `git clean -ndx` and RunPod stop/terminate/delete, which
  protects shutdown rather than blocking it. *(A-Sol, B2-Sol, B2-Terra, B-bypass-Sol, D-Terra)*
- **`pre-commit`** — refuses an unresolved or detached branch and `main`; scans the index before
  the document allowlist can matter; treats a document-check error as a failure.
  *(B2-Sol, B2-Terra, C-Sol)*
- **`commit-msg`** — scans the message for credentials *before* any attribution exemption
  applies; the merge, revert, cherry-pick and fixup exemptions key off Git state rather than
  subject text alone. *(B2-Sol, B-bypass-Sol)*
- **`pre-push`** — peels annotated tags correctly, checks force-push ancestry, rejects unknown
  refs, binds the receipt to the exact peeled pushed commit with a dedicated passing test for
  the mismatch case, and scans the complete outgoing history. Its comment honestly discloses
  what it cannot prove. *(A-Sol, B2-Sol, B2-Terra, B-bypass-Sol)*
- **`record-audit.sh`** — rejects CR/LF injection, serialises writers with a lock, detects a
  stale lock, builds the new receipt atomically beside the old one, and validates its shape
  before replacing. Refuses an occupied target rather than overwriting.
  *(A-Sol, A-Terra, B2-Sol, B2-Terra, C-Sol, C-Terra)*
- **`check_ingress.py`** — fails closed on operational errors (shallow history, malformed
  protocol, missing message file all exit 2); rejects symlinks, submodules and private paths;
  fingerprints rather than echoes a suspected secret; hash-binds declared binary fixtures;
  handles NUL-framed staged/history/tag protocols, size limits and non-UTF-8 bytes. Every
  shell caller found was verified to treat a `ScanFailure` as blocking.
  *(A-Sol, B2-Sol, B2-Terra, B-bypass-Sol, C-Sol, C-Terra, D-Terra)*
- **`doc-allowlist.sh`** — correctly enforces the one-subdirectory depth limit on
  `workbench/active/`. *(B2-Sol, B-bypass-Sol)*
- **`seat.sh`** — dry-run resolves the pinned model, effort, sandbox, work root and timeout
  from `seats.conf` correctly; closes stdin (which is what stops `codex exec` hanging forever);
  fails closed if no timeout implementation exists; requires a positive timeout; propagates a
  non-zero status; rejects an in-repo workspace-write root. *(A-Sol, B2-Sol, B2-Terra, C-Sol,
  C-Terra, D-Sol, D-Terra)*
- **`notify.sh`** — requires an HTTP 2xx before reporting delivery; keeps the topic out of
  curl's argv and out of the URL; disables inherited curl config and xtrace so the topic cannot
  leak to stderr; refuses an ambient `NTFY_SERVER` override; rejects a named pipe at the config
  path; validates topic characters and length; `decision` and `done` genuinely fail on
  non-delivery. *(A-Sol, A-Terra, C-Sol, C-Terra, D-Sol, D-Terra)*
- **`tidy.py`** — the documented race is genuinely narrowed by the digest recheck; the active
  HANDOFF is correctly excluded from filing; duplicate moves and dangling-memory reporting
  behave. *(C-Sol, D-Sol, D-Terra)*
- **The reviewer-pass skill's governance shape** — no severity or confidence floor, an identical
  prompt across seats, disagreements preserved rather than averaged, receipts bound to exact
  commits, agreement treated as evidence rather than verdict. *(A-Sol, A-Terra)*
- **CI** — actions are pinned; `persist-credentials: false` on checkout; the autoclave job
  checks for a `git ls-files` failure before interpreting an empty result. *(A-Sol, C-Sol)*
- **The read-only agents** — `auditor`, `consult` and `scout` are configured without Write,
  Edit, Bash, Agent or web tools. *(A-Sol)*
- **No picker exists in the code.** Both mode A seats searched for executable
  witness-selection logic in the pipeline directories and found none; the Attestatores handoff
  makes non-runs explicit and the Recensor review queue self-describes as not a picker. **The
  picker concern in this pass is entirely about planning vocabulary, not about code.**
- **No old code was found copied into the diff.** Both mode A seats state this explicitly. The
  quarantine findings are about language and unverified provenance, not about copied bytes.
- **Static checks** — `sh -n` and `dash -n` on every changed shell script, ShellCheck on all
  ten, Python AST parse on eleven files, `ruff check` and `ruff format --check`, and
  `git diff --check` across the range: all clean, run independently by both C seats and both B2
  seats.

**Verified against the real world (not stubs):** `codex --version` reported 0.145.0 and
`codex exec --help` was confirmed to document every flag `seat.sh` uses — *by three separate
seats*. `curl --version` reported 8.7.1 and was confirmed to document the delivery flags used.
Given that L34 and L35 are the largest test-integrity findings in the pass, these three
independent live checks are the most reassuring line in this whole section.

## What nobody examined

Named plainly, because the point of this section is the difference between audited and
untouched.

- **The test suite was never run by any seat.** All ten seats attempted `pytest` and all ten
  were blocked by the read-only sandbox's temporary-directory restriction. Every statement in
  mode D about what the tests do is a statement about reading them. *The claim that 439 (in
  fact 448) tests pass is, in this pass, unverified.* I did not run it either — this ledger
  reports what the audit found, and running the suite is the first thing the next session
  should do.
- **GitHub's live branch-protection configuration.** README.md's status line says which rules
  are in force on `main`; no seat could check it, and mode E's prompt asks explicitly whether
  the claim is verifiable from inside the repository at all. It is not.
- **Real Codex seat execution and real ntfy delivery.** Both were exercised only against stubs.
  This is L34 and L35 restated as a coverage gap rather than a test defect.
- **The Codex sandbox's actual enforcement.** All three B seats list this as out of scope; the
  wrapper's *requests* were verified, its *enforcement* was not.
- **`.claude/skills/session-start/SKILL.md` and `session-end/SKILL.md` as procedures.** Read for
  wording by mode E and mode A; never executed, and L45 records that nothing tests them.
- **`ARCHITECTURE.md` and `GLOSSARY.md` as subjects.** Every seat read them as *standards* to
  judge against. No seat audited them for internal consistency or truthfulness. They were the
  ruler, never the thing measured.
- **The pipeline directories themselves** — `pipeline/`, `autoclave/`, `history/`,
  `operations/pod/`, `operations/submit/`, `operations/data/`. Their READMEs changed in this
  diff and were read for prose; there is almost no code there yet, which is why no seat spent
  time on them, but it should be said rather than assumed.

---

# What I verified myself, and what I did not

All eleven reports were read and every finding in them appears above. Of the 106 distinct
findings, I opened the file or ran the check for 31 — chosen as the ones that were material,
the ones where the two seats disagreed, and the ones a wrong answer would be expensive. The
rest are relayed and marked UNVERIFIED.

**Ran directly:** eleven guard classifications through the real `guard.py` interface, reading
the decision payload rather than the exit code; `git rev-parse` with an unknown option, which
settled N6 against a seat's clean-list; searches of `check_ingress.py` for a register-text
matcher and of the hooks for `GIT_NO_REPLACE_OBJECTS`; and direct reads of `settings.json`,
all six agent files, `notify.sh`, `pre-push`, `record-audit.sh`, `working_tree()` in
`check_ingress.py`, `install.sh`, `reviewer-pass/SKILL.md`, `workbench/README.md`,
`.gitignore` and `RUN_PLAN.md` at 6b42def.

**Did not run:** the test suite. No seat could run it either, so the claim that this branch's
448 tests pass is, as of this ledger, unverified by anyone. **That is the first thing the next
session should do**, and it costs a minute.

**Where I departed from the auditors:** four claims are refuted (R12, R13 — three separate
ones), two severity ratings are rejected as overstated (R1, and T5 where I would have said
REJECT), and two findings the auditors rated high are recorded as documented design rather
than defects (R3, R8).

# What I would do next

In order, and none of it is large:

1. **Run the suite.** Nobody has.
2. **Fix N1, N2, N3 and N5** — four documents saying four things that are not true. They are
   prose edits, they take under an hour together, and three of them are the kind of untruth
   that makes a future session relax exactly where it should not.
3. **Fix N9 and N10's docstring half.** The force-push hatch that does not open is the single
   most likely thing here to make somebody switch the guard off; the docstring correction
   discharges GOVERNANCE 10 today without waiting for the four missing classifications.
4. **Put T1 and T4 to Tyrel** — rule 10's enforcement, and whether the seat wrapper's triage
   sentence is an instrument constraining what it measures. Both are his by hard rule 1.
5. **Then push**, with the rest of this ledger as the follow-up backlog. Nothing below the FIX
   NOW line blocks this branch, with the single exception of L2 (destructive git commands
   unclassified), which is worth promoting if there is any appetite for one more fix.
