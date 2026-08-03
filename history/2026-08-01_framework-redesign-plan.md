# The plan

What is wrong with the framework, what to do about it, what it costs, and what only you
can decide.

> **Committed copy.** The original and every source it cites live in
> `workbench/design/system_coherence/`, which is gitignored and present only on the machine
> that produced it. In a fresh clone the `digest_*.md`, `design_*.md`, `SYNTHESIS.md`,
> `proposed_*.md` and `verify_*.md` files this document points at will not be there; this
> file and `2026-08-01_framework-redesign-session.md` are then the whole record.
>
> **Nothing here has been applied, and the verifiers did not endorse it as written.**
> Section 8 opens with the finding that the plan is too big, and a second finding that the
> five drafted artifacts contradict each other in six places — the same defect the design
> exists to end. The recommendation that survived verification is the short version in
> section 7, item 2, not the full programme.

Nothing in here has been applied. It is assembled from one night's work: three GPT
digests (`digest_gpt_a.md` diagnosis, `digest_gpt_b.md` enforcement, `digest_gpt_c.md`
operating model), two Claude designs (`design_opus.md`, `design_fable.md`), one merged
architecture (`SYNTHESIS.md`), five drafted artifacts (`proposed_CLAUDE.md`,
`proposed_governing_documents.md`, `proposed_enforcement.md`, `proposed_lifecycle.md`,
`proposed_migration.md`) and seven verification reports (`verify_*.md`).

Sections 1 and 2 are for you. Section 3 onward is for the session that builds it.
Section 7 is the list of things nobody can decide but you. Section 8 is where the
verifiers said this plan is wrong, and it opens with the one that says the plan is too
big — because it is the finding most likely to change what you do next.

---

## 1. What is wrong

Nothing here is a mistake anyone made. It is three properties of how the framework is
built, and each of them manufactures the loop you described.

**The same rule is written out by hand in many places, and nothing connects the copies.**
One decision gets written in the rulebook, then written again in the session checklist,
again in the job descriptions the assistants are given, again inside the program that
enforces it, again in the sentences that program prints when it refuses something, and
again in the tests that check the program. That is nine hand-made translations of one
sentence, and none of them is in charge of the others. Change one and the rest quietly
become false. Nobody finds out until a reviewer happens to read two of them in the same
hour. Then the repair changes the nearest copy — and usually adds another one. The tests
make it worse rather than better: several of them pin one copy against another copy,
which stops a careless one-sided edit but settles nothing about which side is right, and
turns every deliberate change into coordinated surgery across nine files.

**The program that enforces the rules works by recognising how a command is spelled, not
what it does.** For every door it closes there is a window with a different name: it can
stop an assistant editing a protected file through the normal editing tool, and not stop
the same assistant rewriting the same file with a text-processing command. Three
reviewers found that same hole independently. You cannot finish this job by adding
spellings, because the next spelling is always available, and each one you add costs more
program, more comment and more test.

**Work is preserved but never closed.** The project is very good at keeping raw material
and weak at turning it into one settled answer. A review happened this week that produced
about twelve thousand lines across three readers, and nothing outside the raw folder
records that it happened or what came of it. The queue of known-but-unfixed problems only
grows; nothing is ever taken out of it. So the same issues get rediscovered, and the pile
of things you are carrying in your head never gets smaller.

Those three together explain every one of the six failures from the single change last
night, and they explain why the week felt like circling. Each individual repair was
cheap, correct, and produced the next one.

There is a fourth thing, which is not a cause but is why the causes hurt so much: the
machinery around the project is now roughly one and three-quarter times the size of the
project itself. Every line of it is something that can disagree with another line.

---

## 2. The idea

**One home, one boundary, one close.** Three clauses, answering the three causes in
order.

**One home.** Every fact lives in exactly one place, and everything else points at that
place instead of repeating it. A pointer cannot go stale the way a copy can. Rules get
permanent labels that survive rewording, reordering and renumbering, so a reference to a
rule can never silently come to mean a different rule. The lists that both people and
software need — which files are protected, which branch names are legal, who the
reviewers are — become a few tiny plain-text lists that the software reads directly, so
changing the list changes the behaviour, with no second copy to forget. And there is a
one-line discipline that generalises the whole idea:

> A document may name a mechanism. It may never list what that mechanism contains.

A job description that lists the protected files goes stale the day a file is added. A
job description that says "never write outside your permit, and the permit is written
before you start" is true forever.

**One boundary.** Each rule is enforced at the single place that owns its effect, and
nowhere else. Where something can be made mechanically impossible, it is made impossible
rather than forbidden in prose. The assistant that writes code is given no shell at all,
so every change it makes must name the file it is changing, and that name is checked
against a permit you can read before the work starts. No permit, no writing. That is the
hard-coded thing you asked for, and it replaces about two thousand lines of guessing.

**One close.** Every piece of work ends in one visible outcome. A review is not finished
until every finding has exactly one disposition — fixed, declined with a reason, deferred
to a named task, or handed to you — written to one short file that travels with the
project.

Two things follow that you will feel directly. You stop being asked about models, effort,
which assistants to use, whether to run the review, filing, closing, and repeat pushes to
a pull request that is already open. And you get one command that turns any of the
machinery off, with the front page saying so — because a guard you cannot unwire is a
defect whatever it prevents.

---

## 3. The system

### 3.1 The documents

Nine tracked homes and four local ones, each owning something no other file may state
(`SYNTHESIS.md` §4). `README.md` owns the idea, the single status line, navigation, and a
generated Controls block naming every local protection and the command that turns it off.
`GOALS.md`, `GOVERNANCE.md`, `ARCHITECTURE.md` and `GLOSSARY.md` are unchanged in
substance and gain permanent identifiers. `CLAUDE.md` owns the hard rules, the four rule
levels stated once, the session loop in intent, the overridable defaults, and a routing
paragraph saying where any new fact belongs. A new `.claude/policy/` directory owns the
machine-readable lists. `pipeline/README.md` and per-stage READMEs become tracked, so the
build order survives a dead laptop. `history/<date>_<sha>_disposition.md` owns the
settled outcome of one review pass. Locally and gitignored: one `workbench/STATE.md`
screen of current state, an evidence drawer, a scratch drawer, and `private/`.

**The sources disagree here and the disagreement was not resolved by evidence.**
`design_opus.md` and Seat A both want every binding session rule consolidated into
`GOVERNANCE.md`, leaving `CLAUDE.md` purely overridable — two ladders with two numbering
schemes is itself an instance of the disease. `design_fable.md` refuses, and `SYNTHESIS.md`
§4 sides with fable: permanent identifiers, not consolidation, are what stop a citation
re-aiming, and consolidation costs a word-by-word rewrite of a document only you may
amend. This is a judgement call, not a finding.

### 3.2 The rules

Every rule carries a permanent identifier in its heading — `[HR-03]`, `[GOV-2]` — that
never denotes order, never gets reused, and survives rewording. Today's numbers become
today's identifiers, so the conversion is mechanical (`verify_claude_md.md` confirmed the
mapping holds against the twelve rules on disk). Every citation outside the rule's home
file is an exact marked token, `rule:HR-03`, never English prose that a tool has to parse.
Four of five sources proposed this independently, and `COUPLINGS_TRIAGE.md` reached it
from evidence: a scan that guesses at citations from punctuation found one real problem in
five reports, and a check at that precision gets switched off within a week.

Code never quotes a rule's sentence. A refusal names the action refused, the rule
identifier, the remedy and the file to read. Three positions existed — Seat B wanted to
keep the literal text in a policy module and test it against the source; `design_opus.md`
wanted the guard to read the current sentence at refusal time; `design_fable.md` wanted
neither. The synthesis took fable's, on the grounds that the smallest thing which removes
the drift wins.

The four levels — hard rule, gate, standard, preference — are kept. `design_opus.md` and
Seat A wanted them collapsed to three; `design_fable.md` and Seat C wanted them kept, and
no checker reads the level, so collapsing buys nothing mechanical.

Two checkers hold it together. `check.py` proves every marked citation resolves, rejects
ordinal citations, runs a **restatement scan** over the five families of values owned by
`.claude/policy/` and fails a commit that copies one elsewhere, greps for retired
vocabulary, and byte-compares every generated block against its source. The route matrix
is the second: for every promise the guard documents, a fixture exercises that promise on
every route it claims to cover, and a route deliberately left open is declared as such so
it is visible rather than assumed closed.

### 3.3 The lifecycle

Six phases (`proposed_lifecycle.md` §3). **Bootstrap** — reading, fetching refs, arming
the hooks, printing state — is explicitly *not work*, which dissolves the three-way
contradiction every reviewer found between "name the branch first", "read six documents
first" and "the first response is the plan". Work begins at the first write and requires
two things: a branch that is not `main`, and a goal you stated. **Open** is one command
printing one screen: branch, upstream, ahead/behind, tree state, whether the hooks are
armed read from real git config, whether anything is switched off read from real control
state, the digests of the documents as they stand on disk, what GitHub enforces queried
live and printed `UNKNOWN` when unreachable, live suspensions only, current state, and at
most three things waiting on you. **Agree** is conversation. **Work** checkpoints state
after each completed unit. **Verify** runs the review by default rather than asking, and
is not complete until every finding has one disposition. **Land** makes the first
publication of a branch and the pull request it becomes one gate asked once; later pushes
to that open pull request are not gated. **Close** is bookkeeping, not permission: it
verifies real git state, writes state atomically, archives evidence and sends the terminal
notification, and it never pushes, merges, switches branches or deletes anything.

Two deliberate changes: no branch deletion in the close path, because a close that can
prompt is a close that can hang; and preservation is automatic while ending is not, so a
night that finishes, dies or runs out of context still leaves a truthful morning state.

**The unattended night.** While the mode file says unattended, every "ask" becomes a
"deny": the action fails in about a second with its rule identifier and reason, is
recorded, triggers one notification, and the session carries on with independent work. A
session can no longer hang on a prompt. The whole-system verifier called this the best
part of the design and also found that **nobody owns the record it produces** — three
artifacts say the refused action is appended to your inbox automatically, and the only
named writer is a command the guard never invokes (`verify_whole_system.md` §3.7).

### 3.4 The agents

Capability is the role; model, effort and prompt are configuration. A **Reader** may read,
grep and glob, and nothing else. A **Builder** may read, grep, glob, write and edit, with
a live permit, and may not use a shell, the network, MCP tools, or spawn anything.

All five sources agreed on the shape of confinement — enumerate what is allowed, deny the
rest, put the allowed paths in a per-task permit rather than a job title, fail closed when
the permit is absent. That agreement is unanimous. They split on the substrate, and the
synthesis ruled **none of the four heavier substrates first**: not the operating-system
sandbox (`design_opus.md`), not a pinned container (Seat A), not copy-in/copy-out task
roots (Seat B), not a shell jail (`design_fable.md`). Withholding the shell reaches
"cannot" for the writing route at a cost of one line per role file.

**The cost is real and you will feel it.** A builder cannot run its own tests; it returns
a patch and the session verifies it. Today's worker role requires the opposite. The shell
jail buys that capability back later if the loss actually hurts.

### 3.5 The enforcement

The organising rule is Seat A's: enforce at the boundary that owns the effect, and nowhere
else. Credentials, payload ceilings and history belong to the ingress scanner, which is
already built the right way — one scanner, every caller, no copies — and is unchanged.
Which files may be committed belongs to the document allowlist, unchanged. Branch safety,
tag immutability and attribution belong to the git hooks, unchanged. First publication
becomes about fifteen lines in a hook that already carries every part of the mechanism.
Merge belongs to GitHub. Which files a subagent may write belongs to capability plus
permit. Money keeps its tripwire.

Everything else the guard does today is deleted, because a downstream boundary already
owns it. The guard lands at about 450 lines from 2,294.

### 3.6 How the parts hold each other

Prose points at mechanisms and never lists their contents. Code reads the lists at
runtime and never embeds a copy. Generated blocks are byte-compared against their source.
Tests assert shape, never values. The checker proves that every citation resolves and that
no owned value has been copied. Where a fact genuinely cannot be bound, the design says so
out loud — `SYNTHESIS.md` §9 names three such duplications and gives the reason for each.

---

## 4. What gets deleted, and what protection goes with it

| Deleted | Size | What is lost |
|---|---|---|
| The guard's shell grammar — command flattening, heredoc scanning, wrapper lists, git option parsing, HTTP/`gh` parsing, the blind-spot table | ~1,800 lines | For subagents, nothing — they will have no shell. For the main session, the guard stops recognising dangerous command spellings. See the warning below. |
| The guard's parser tests | ~1,400 lines | Coverage of code paths that no longer exist. Replaced by ~200 lines of adversarial confinement fixtures plus the route matrix, which are smaller and harder. |
| `.githooks/tidy.py` and its tests | 602 lines | Automatic policing of the workbench drawers, budgets and aging rules. One genuinely load-bearing piece — the raw-output size cap — must be moved somewhere before the rest goes. |
| Four of six role files and the shared restated prose in the survivors | ~180 lines | The per-role distinctions, notably the rebuilder's autoclave discipline. Answered by citing the quarantine rule rather than restating it — but see the runtime problem in §8. |
| The roster test's duplicated floor dictionary | The synthesis says ~83 lines; `verify_lifecycle.md` measured it at ~17 in a 133-line file | The cross-check that two copies of the effort floors agree. Replaced by having one copy. |
| The quoted rule constant in the guard, and the test pinning it | Small | Nothing. There is then nothing to go stale. |
| The guard's ask on every push | Small | Nothing, provided the publication gate lands in the same pull request. |
| `NEXT_SESSION_BRIEF.md`, `DEFERRED_ACTIONS.md` as an institution, `MASTER_PLAN.md`, `SECURITY_FINDINGS_OPEN.md`, four of seven workbench drawers, the archived handoffs | ~1,000 lines of notes | Real relief for whoever reads them, and **no effect at all on the size problem** — these are gitignored and were never counted in the 10,905. Both Claude designs implied otherwise; `SYNTHESIS.md` §10 corrects it. |

**The one deletion that needs a decision rather than a nod.** `verify_enforcement.md` F1
found that the drafted enforcement layer silently drops three protections the architecture
marks as kept: the guard's ask before an action that discards or makes work hard to
recover, its ask before a recursive deletion, and its ask before something changes GitHub
state other people can see. It also drops the refusal to switch onto `main`. Today's
guard asks on all four. `CLAUDE.md` currently states that the guard refuses the switch
onto main, so deleting it silently would leave a binding document asserting a mechanism
that no longer exists — the exact failure this whole exercise exists to end. Either those
rows come back, or each drop is stated as a decision with its consequence and the
document wording changes in the same commit.

---

## 5. The mechanisms

### 5.1 Stopping a subagent touching files outside its task

This is the thing you asked for by name. It is three layers in series, and the first two
mean the third has almost nothing to do.

**Layer one — the capability is not granted.** The role file's frontmatter says which
tools exist for that role. A builder gets read, grep, glob, write and edit. It does not
get a shell, the network, MCP tools, or the ability to spawn another agent. The Claude
Code runtime enforces this before the guard is ever consulted. With no shell, a builder
cannot spell a write: there is no wrapper to unwrap, no heredoc to scan, no in-place text
edit to have thought of. About 1,800 lines of pattern matching become unnecessary rather
than insufficient.

*Status: unproven.* The reader roles already work this way and no review seat found a hole
in them, but that is the absence of a finding, not a test. `proposed_enforcement.md` §12.1
is the fixture that must pass first: dispatch a builder with the shell withheld, have it
attempt a shell call, assert it never reaches the guard. **The entire confinement argument
rests on this one behaviour.**

**Layer two — the permit.** Before dispatch, the main session writes a small file naming
the task, the role, the base commit, the exact paths that may be written, and an expiry.
You can read it before the work starts. It lives in `private/`, which is gitignored and —
once the deny rules are proven — outside every agent's reach, so the holder cannot forge or
extend its own permit. A permit may not name the whole repository, a wildcard, a governed
path, the git directory, or `private/` itself; the writer refuses to create one that does.

**Layer three — the guard checks it.** On any structured write from an actor carrying an
agent identity: resolve the target path first, so symlinks and `../` collapse and anything
resolving outside the project root is refused before anything else; find the live permits
for that agent; **no permit, expired permit or malformed permit denies every write**;
refuse unless the resolved path sits under a declared entry, compared as resolved prefixes
with a separator so that one directory never admits a similarly named sibling; deny shell
and MCP outright even though layer one should have made them unreachable. About forty
lines, with no text parsing at any point.

That the guard can see which agent is acting is **verified**, not hoped: the existing
`subagent_name()` reads the agent type from the payload, and the decision log breaks 601
entries down by actor.

**Ten adversarial tests land in the same commit as the deletion they justify** — write to a
protected document when not permitted, write to the settings file, write through a symlink,
traverse with `../`, alter git configuration, open the network, read host credentials,
spawn a nested agent, export an undeclared file, and verify every out-of-scope file's hash
is unchanged.

**What this still cannot do, stated plainly.** It cannot stop bad content inside an allowed
file — review owns that. It cannot stop a bad integration decision by the session. It does
not confine *reading*: the tool matcher does not cover read, grep and glob today, so the
proposal closes that with deny rules in the settings file, and **those deny rules are
untested** — a note in this project's own memory records that path rules of the wrong form
silently match nothing, and an untested deny is not a guard. Two builders of the same role
running at once can each write the union of their permits. And none of it means anything in
a clone where the machinery was never installed.

**Two incompatible specifications of the permit exist in this corpus.**
`proposed_enforcement.md` builds it in Python, keyed by task, with five fields and the
forbidden roots living in code. `proposed_lifecycle.md` builds it in shell, keyed by role
profile, with eight fields including a different key, and the forbidden roots living in
policy files. Build one seat's writer against the other seat's guard and **every subagent
write is refused**, because the guard looks for a field the writer never wrote. It fails
safe, but the fence does not work. This must be settled before a line is written
(`verify_whole_system.md` §3.2).

### 5.2 The off switch

One command with `off`, `on` and `status`. `off` disables the guard through a launcher shim
the settings file consults — so a broken guard can be disabled without hand-editing JSON —
and unsets this clone's hook path only. `status` reads real git configuration and real
control state, never a flag, so no flag can lie. Every `off` writes a dated suspension that
open and close both print until it is resolved. The state lives outside every agent's
reach. README gains a generated block naming every protection and the exact command,
byte-compared so it cannot drift.

A hard rule currently claims this step already exists. It does not. The rule is either
deleted first and restored true, or corrected once in the commit that makes it true — the
two migration orders differ on which.

### 5.3 First publication

The push hook already parses the four-field input, already carries the null-SHA sentinels
and already uses the single-shot environment-variable idiom. So the gate is about fifteen
lines: a branch push whose remote SHA is null is blocked unless a named variable is set,
which the session sets only on your word. Later pushes have a non-null remote SHA and pass
in silence. This closes a live contradiction — today's rule gates opening the pull request
but not the first push, and the guard asks on every push, which contradicts the doctrine
that pushes to an open pull request are not gated. Both halves were broken in opposite
directions at once.

### 5.4 The unattended conversion

One function converts every "ask" to a "deny" while the mode file says unattended, and a
property test proves no check can grow a second conversion point. In the same mode, edits
to the guard's own code and the git hooks deny even for the main session — the guard is not
edited at 3am by the session it is judging. About ten lines for both.

*One correction to how this is sold.* The claim that a session can no longer hang on a
prompt is true of guard-originated asks only. Unclassified main-session shell commands still
pass to Claude Code's normal permission flow, and a prompt off the allowlist at 3am still
hangs the night (`verify_enforcement.md` F6).

### 5.5 What nobody has run

Five behaviours this design depends on that no seat executed. Each is a small fixture and
each is cheaper now than in production (`proposed_enforcement.md` §12).

1. **That withholding the shell in a role file actually withholds it.** The whole
   confinement argument rests on this. Reader roles work this way today and no seat found a
   hole, but that is the absence of a finding, not a test.
2. **That the settings deny rules match these path forms.** This project's own memory
   records that path rules of the wrong form silently match nothing. Until this passes, the
   claim that `private/` is beyond an agent's reach is an overclaim — and one artifact makes
   the same claim on the strength of the gitignore file, which untracks and reaches nothing
   (`verify_migration.md` D4).
3. **Whether the runtime supplies an identity unique per dispatch.** If it does, the
   two-builders-share-a-union problem disappears entirely.
4. **Whether the post-call payload carries enough to pair with a decision.** If it does not,
   the decision log's outcome column is a heuristic receipt and must say so in its own
   header. Its own verifier recommends cutting the mechanism.
5. **The operating-system sandbox.** Present in the installed binary, and the system tool
   exists on this machine — both established by reading, not by running.

---

## 6. How we get there

There is a precondition before any of it: **PR #14 is open and the rule is one open pull
request at a time.** Everything since — the rewritten working rules and the harness change
that three seats reviewed — is sitting on a local branch that has never been pushed. As
verified in `proposed_migration.md` §2 and re-verified in `verify_migration.md`: nothing of
this design exists anywhere but this laptop. Merging #14 costs you fifteen to thirty
minutes and unblocks everything.

**My recommendation is the short version.** Three pull requests, four to six sessions,
about five decisions — plus the rule-identity step, which is one mechanical session and is
the direct answer to the failure that started the week. Both the migration seat
(`proposed_migration.md` §6) and the proportion verifier (`verify_proportion.md` §7)
independently reached the same shape, and the proportion verifier's argument for it is the
strongest single piece of reasoning in the corpus.

| # | Step | What lands | Effort | Gates you own |
|---|---|---|---|---|
| A | Land what is in flight | Merge #14; rebase the working-rules branch onto the new main | Under an hour of session time; 15–30 min of yours | Merge; what happens to the parked `infra/` branch |
| 0 | Repairs and a measuring stick | The `.claude/` reach anchored to the project root (it currently matches your own global configuration and every other repository on this machine); the stale hook citation; the roster wording; a ten-line tracked size reporter so every later claim is measured rather than guessed | One short session, 1–2 hours including review. About +60 / −15 lines — **this step adds, and that is correct** | Push permission naming the branch; review permission and how many seats |
| 2 | The off switch | The controls command, the launcher shim, the generated README block, a test in a temporary repository | One session, 3–4 hours. About +300 / −5. **This step adds** | The exact README wording and the corrected hard rule — a governing document, so yours |
| 4 | Confinement | Writer roles lose the shell and gain the MCP denial; permits; the guard's subagent path rewritten to ~40 lines; the ten adversarial tests and the route matrix **in the same commit** as the deletion of the shell grammar; the quoted rule constant deleted | Two to three sessions, 8–12 hours, and the most expensive review of the programme. About +300 / −3,300. **This is the step that pays for the others** | May a builder ever draft harness code; do writing agents lose the shell; push, and a three-seat review with the third seat recommended rather than optional |
| — | Two ten-line slices lifted out of the dropped steps | The retired-vocabulary grep gate; ask-becomes-deny while unattended | Ride steps 2 and 4 | None new |
| 1 | Identity (recommended addition) | Permanent identifiers in both ladders; every citation becomes a marked token; the citation half of the checker in the same commit | One session, 3–4 hours, mostly mechanical. About +200 / −30 | The permanence sentence in two governing documents |

Three precautions on step 4, from `proposed_migration.md` §8. Tag the commit before it, so
the old guard is retrievable by name. Do not start the next step until it has survived one
full working session of real use — a confinement weakness shows up in use, not in fixtures.
And if any step's review returns a finding that the step's own tests should have caught,
that is evidence the step was too large: split it rather than patch it.

**The full programme, for comparison.** Seven steps, nine to thirteen sessions, roughly
three weeks, seven pull requests, seven review passes, about twenty-five decisions. It adds
three further steps: one source for every shared list, the publication gate and the
unattended rules as a step of their own, and the procedure-and-state rewrite. The
additional steps buy the restatement scan, generated blocks, a scripted session procedure,
tracked review dispositions and tracked build order. **Four of the seven steps make the
framework bigger, and under the designed order stopping halfway leaves more machinery than
today** — the migration seat says that in bold about its own programme.

**Two orders exist and they disagree.** `SYNTHESIS.md` §12 runs identity first;
`proposed_migration.md` §4 runs repairs, off switch and confinement first and argues that
the framework should visibly shrink before any coherence work. Two more artifacts state an
order as well, both following the synthesis. The moment you pick one, the other three must
be corrected or pointed at the winner, or the corpus ships disagreeing with itself about
its own plan (`verify_migration.md` D1, `verify_whole_system.md` §3.8).

---

## 7. What only you can decide

Grouped by whether you need to answer now. Nothing here is decided elsewhere in this
document.

### Needed before anything starts

1. **Merge PR #14.** Nothing else can be pushed while it is open.
2. **Short version or full programme.** Short: three pull requests, four to six sessions,
   about five decisions, roughly −3,000 lines, and you get the fence, the switch and a
   night that cannot hang. Full: seven pull requests, three weeks, about twenty-five
   decisions, and you additionally get the restatement scan, generated blocks, a scripted
   session procedure and tracked review outcomes. *Recommendation: short, plus the identity
   step.* Both the migration seat and the proportion verifier reached this independently.
3. **Which order**, if you take the full programme. Designed order buys citation safety
   earliest; proposed order buys a visibly smaller framework in week one and puts the off
   switch in your hands before anything depends on it. *Recommendation: proposed.*
4. **The parked branch** `infra/guard-sees-through-rtk-proxy` — four unpushed commits whose
   rules change is superseded. Cherry-pick the guard fix, keep it parked, or abandon it.
   Parked costs nothing except being one more thing to remember.

### Needed at the confinement step

5. **Do writing agents lose the shell?** Yes means an agent can no longer run the tests for
   the code it just wrote and the session verifies instead — and today's worker role
   requires the opposite. No means keeping a version of the guessing problem. A shell jail
   can buy the capability back later if the loss hurts.
6. **May a builder ever draft harness code?** Under this design a permit may not name a
   governed path, and `.claude/` is governed, so the infrastructure role cannot touch the
   guard, the hooks, the skills or the roster — most of what that role exists for. *This is
   a live disagreement between reviewers, not a settled question:* Opus called the widening
   a high-severity defect for exactly this reason; Fable and Sol read the same code and
   called it your decision correctly implemented. Yes keeps the role useful, with drafting
   confined to the autoclave and the session landing it. No means the session writes every
   line of its own machinery.
7. **One builder per role name at a time, or accept the union?** Two builders of the same
   role running concurrently can each write the union of their permits. The leak is between
   two of your own tasks, not out of the fence.

### Needed at the off-switch step

8. **The README wording and the corrected hard rule.** A governing document, so yours.
9. **May the off switch run during an unattended night?** Refusing protects against a 3am
   session disabling the thing judging it. Allowing means a night blocked by a broken guard
   can unblock itself. As drafted it refuses unless a confirmation variable is set — which
   the same session can set, so it is a recorded tripwire, not a gate, and should be sold
   as one.
10. **Is turning a protection off itself a gated action?** The drafted gate list does not
    include it, and the drafted list declares itself authoritative. The result is that the
    small bypass is refused while the sanctioned large one is not.
11. **Does `private/` become unreadable by tools?** It closes a real hole — the notification
    bearer topic, the permits, the control state and the decision log are readable today by
    any role holding read. It costs you ad hoc reading of your own drawer; everything there
    would be reached through named commands. *Recommendation: yes, after the fixture proves
    the deny rules actually match.*
12. **Is an unrecorded session mode treated as unattended?** Fail-closed means a compacted,
    resumed or crashed session can never assume you are in the room — at the cost of an
    occasional refusal in an ordinary attended session, fixed by one command.

### Needed only if you take the full programme

13. **The rule-permanence sentence** in two governing documents, and whether it is stated
    once and cited or restated in each file. Once removes three copies; four statements make
    each document readable standing alone.
14. **The mechanical review carve-out** — a push whose diff touches only policy lists or
    regenerates generated files byte-identically lands on checks plus the session's own diff
    read. This is a reduction in review coverage, which is yours by rule. *It is also
    specified nowhere:* two artifacts rely on a checker mode that no artifact builds or
    tests.
15. **Risk-tiered review, or the three-seat default?** Seat A recommends tiers; Fable refuses
    on the grounds that you decided the three-seat default recently and explicitly. Two
    agreeing seats are thinner evidence than three, and you should be told so either way.
16. **May a session post routine factual dispositions on a pull request in your name?**
    Without a grant an unattended night cannot settle a review at all. With one, something
    writes publicly as you.
17. **Where the suspension record lives**, given that the drawer it currently lives in is
    scheduled for deletion. Three artifacts send it to three different places.
18. **Do the six role profile names stay as they are?** Renaming re-teaches every session for
    no mechanical gain.
19. **Delete or archive the retired workbench notes** at the end.

### Governing-document questions raised by the documents seat

20. **Does the front-page aphorism move?** Under the proposal "a missed act is worse than a
    poorly read act" appears once, in the goals document, and the front page carries only the
    generated heading. One home, duller front page — or keep the sentence and accept a named
    copy.
21. **Does the distribution rule move from README into GOVERNANCE?** Moving it obeys "README
    may never contain a rule"; leaving it means the routing rule has an exception on its
    first day.
22. **May tracked code cite the previous project's numbered invariants at all?** Somewhere
    between eight and seventeen tracked files currently depend on that external numbering for
    their meaning — the artifact's hand count was wrong and the verifier found roughly twice
    as many. This is a quarantine question, which makes it yours.
23. **Is `DATA_CONTRACT.md` still coming?** It is promised by the architecture document,
    allowlisted by a hook, named by three role files and a guard test, and does not exist;
    the executable contracts already do the job. Keep the promise or withdraw it and clean up
    the dependents — of which there are about nine, not five, and one of them is `CLAUDE.md`.
24. **Is the reserved-phrase check worth its fifteen lines?** It is the only proposed
    mechanism that binds a sentence rather than a list, it cannot catch paraphrase, and its
    own author calls it the one most likely to annoy. The proportion verifier says cut it.

### Long-running, blocking nothing

25. **Prove and adopt the operating-system sandbox?** It exists in the installed binary and
    the system tool is present on this machine — verified by reading, **never run by anyone**.
    It is the only mechanism that would also close the main session's shell route. Two of its
    settings are hazardous to you: one turns an unavailable sandbox into a startup failure,
    the other removes an escape hatch you might need. *Recommendation: prove it with one
    fixture and decide after.*
26. **Merge credentials.** No code in this repository can tell whether you or an agent process
    performed a merge if both hold the same GitHub credentials. Keeping merge credentials out
    of agent processes is a real boundary; the current rule is an alarm. All five sources say
    the same and none can fix it from inside the repository.

---

## 8. What the verification found

### The finding that comes first: the plan is too big

`verify_proportion.md` was asked one question — has this made things worse for you — and
answered: **in your actual currency, yes, so far, and the full programme would make it more
so.** Its verdict is that the full programme should not be built. Not because the ideas are
wrong; it calls the confinement move, the off switch, ask-becomes-deny and the permanent
labels genuinely right. Because the programme wrapped around them fails your own test three
ways.

**It does not deliver the size it promises.** `SYNTHESIS.md` tells you it removes about
4,400 lines and adds about 700, landing near 7,200 — about 1.15× the pipeline. Summing what
the five artifacts actually specify, at their own optimistic estimates, gives 7,800–7,950;
the verifier's honest expectation is 8,300–8,700, or 1.25–1.40×. And **7,200 is not smaller
than the 6,234-line pipeline anyway**, so the plan's own acceptance test — "the framework is
smaller than the pipeline, measured" — can only pass if the pipeline grows about a thousand
lines. Nobody in the corpus noticed that.

**The prose does not shrink at all.** The synthesis promises 807 lines of governing prose
falling to about 630. The one document actually drafted came in at 277 lines against a
promised 150 — an 85% overshoot on the only estimate anyone tested. The other five documents
gain identifiers, anchors and retired tables while losing restatements, netting roughly even.
Proposed total: about 800 against today's 807.

**Every estimate that has been tested was wrong in the same direction.** The lifecycle seat
has already corrected the synthesis's script budget upward by 150–200 lines. The migration
seat says to treat every one of its own figures as a forecast that could be wrong by half.
There is no command in the tree that measures any of this; step 0 lands one.

**And reaching the steady state costs more attention than the current framework ever has.**
About twenty-five decisions, seven push permissions, seven review permissions, seven merges,
and the wording of five governing-document amendments. The design paperwork in this one
directory is 9,678 lines — **1.55 times the size of your entire pipeline**. You asked for a
guide, rules and some hard-coded limits, and the response was twenty model runs about the
framework.

Its rescue is that about a fifth of the corpus is exactly what you asked for, and that fifth
is separable, cheap and mostly deletion. That fifth is §6's short version.

**The steady state itself is genuinely lighter, and the same verifier says so.** Reading at
session open falls from roughly 1,700–2,300 lines to about 500. You stop being asked to
authorise the review, and stop being asked per push. What you produce per session is
comparable in count and scripted rather than hand-written. The disproportion is in the
transition and in the machinery to maintain, not in the daily experience.

### The second finding: the five artifacts are not yet one system

`verify_whole_system.md` read the five drafts against each other and found they describe
about ninety percent of one system and ten percent of two — and the ten percent is the fence,
the off switch and the document that governs every session. It recommends **not adopting the
five as the plan**, but as input to one merged plan, produced by a single reconciliation
session of a few hours. Its sixteen questions are choices between two stated answers, not
open design.

The worst of them, in its own ordering:

- **The same new rule number is given to two different rules** by two artifacts, both of
  which then cite it. That is the near-miss from `CONTEXT.md` — citations pointing at the
  wrong rule — reproduced inside the fix for it, before a line of code was written.
- **The fence is specified twice, incompatibly** (§5.1 above).
- **The new `CLAUDE.md` is written as if the whole programme is already built**, naming five
  things that do not exist, and **no migration step installs it at all.** It is the centre of
  the design and nobody scheduled it.
- **Two of its statements would be false on the day they landed** — it claims the restatement
  scan catches anyone copying the rule levels, which the scan explicitly cannot see, and it
  claims every protection comes off in one command, which another artifact makes refuse
  overnight. That is precisely the defect the plan condemns in today's rule 11.
- **The record of what an unattended night could not do has no writer** (§3.3 above).
- **There are four hand-typed copies of the "single-source register"**, the design's own
  answer to "what must move when a fact changes", and they already disagree with each other.

### What the per-artifact verifiers found

Each artifact was read by a seat instructed to refute it. None was passed as landable:

- `verify_claude_md.md` — the draft is terser and more followable than what it replaces and
  its identifier mapping holds, but it asserts an enforcement the checker does not perform,
  its own notification paragraph would fail the checker it mandates, and it never says which
  migration step it lands at. Five fixes, each a sentence or two.
- `verify_enforcement.md` — the factual base checked out in detail. But it silently drops
  three protections the architecture marks as kept (§4 above), and its off switch does not
  do what it says for one mechanism, breaking your third constraint there.
- `verify_governance.md` — the direction is right and most of it survives. But **every
  hand-counted enumeration in it was wrong**: five citations that are at least nine, eight
  files that are at least seventeen, five dependents that are about nine. Its own strongest
  lesson is that lists must be derived mechanically, not counted by hand.
- `verify_lifecycle.md` — the design content is sound and unusually honest, but its proposed
  edits were written against the **stale copy of `CLAUDE.md` injected into that seat's
  context** rather than the file on disk, so two of its three replacement targets do not
  exist. The artifact itself names that divergence as a live example of why all this is
  needed, and then acts on the stale copy anyway.
- `verify_migration.md` — refutation mostly failed: forty-odd line citations, eleven line
  counts, the full citation arithmetic and the git topology all verified correct, which is
  rare. Four real defects survive, all paragraph-scale, and one of them must be fixed the
  moment you pick an order.

### What is sound and should not be reopened

The whole-system verifier put this on the record deliberately. The diagnosis — three
mechanisms, accounting for every finding in the corpus. Permanent identifiers with marked
citation tokens, reached independently by four seats and by the coupling scanner from
evidence. Withholding the shell from a writing agent, with its cost stated plainly rather
than hidden. Failing closed on an absent permit. Landing the adversarial tests in the same
commit as the deletion they justify — called the single best process decision in the plan.
The route matrix with declared open rows. Separating preservation from ending, so a night
that dies leaves a truthful morning. Bootstrap not being work. And the discovery, which
nobody was looking for, that shipped pipeline code cites architectural invariants by
position — in the part of the tree everyone assumed was healthy.

### The honest sentence that is missing from all five artifacts

From `verify_whole_system.md` §6c, tracing what happens when you reword a rule six weeks
from now: labels protect you from renumbering; **nothing protects you from a rule's meaning
changing under its citations**, so a reword is still a review. The mechanical half of this
problem is solved by the design. The judgement half is not, and one of the six things that
went wrong this week — a rule's *consequence*, restated in a checklist — is in the class no
checker can see.

### What this corpus proves about itself

The design's thesis is that one fact written in several places, with nothing binding the
copies, drifts. This corpus is one night old and has already done it to itself: a duplicated
rule number, two incompatible fences, four disagreeing registers, three versions of the
arithmetic, two live migration orders, and an 85% overshoot on the one estimate anyone
tested. Five careful seats, one shared brief, one night.

That is not an argument against the diagnosis. It is the strongest available evidence *for*
it. But it is a strong argument that the half of the proposal which adds institutions —
more documents, more registers, more seats — reproduces the problem at a higher level, and
that only the half which deletes actually removes it.
