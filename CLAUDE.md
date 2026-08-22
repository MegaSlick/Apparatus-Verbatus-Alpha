# Working rules — Apparatus Verbatus

Read [GOALS.md](GOALS.md), [GOVERNANCE.md](GOVERNANCE.md),
[ARCHITECTURE.md](ARCHITECTURE.md), and [GLOSSARY.md](GLOSSARY.md) before changing the
project. They bind the work; this file only says how the work is done.

Use the procedure named here only when its trigger fires:

| Trigger | Procedure |
|---|---|
| opening or closing a session | `.claude/skills/session-start` or `session-end` |
| changing a governed path | `.claude/skills/governed-edit` |
| using agents or chambers | `.claude/agents/README.md`, then `operations/autoclave/README.md` |
| rebuilding old behavior | `cleanroom/README.md` |
| notes and handoffs | `workbench/README.md` |
| live pods or paid infrastructure | `operations/pod/README.md` |
| phone notifications | `operations/notify/README.md` |

## Hard rules

These numbers are cited by hooks, tests, and agent briefs. Append; never insert or reuse.

1. **Tyrel decides** governance, governed-document changes, paid or live infrastructure,
   exclusions, declarations that the pipeline is proven, disclosure, deployment, opening
   a pull request, destructive or hard-to-recover operations, and merging.
2. **No live pod without his permission in that session.** Verify shutdown against
   provider state and billing.
3. **A session never works from `main`.** Work reaches it through a pull request.
4. **Never open a pull request without his say-so.** The first push and its pull request
   are one permission; later pushes to that open pull request are part of the same work.
5. **Never share, rebase, force-push, or amend a branch that is not yours.**
6. **Nothing enters uninspected.** If the accountable session cannot justify a line, it
   does not enter.
7. **Nothing is lost silently.** Record findings, failures, decisions, and partial work.
   This is a visibility rule, not an escalation rule.
8. **Do not build a picker.** The Perlector reads; nothing selects among witnesses.
9. **When a rule and a goal pull apart, stop and say so.** Quote the concrete conflict.
10. **A spawned agent never edits a governed path.** It proposes wording; the main
    session applies an approved change.
11. **Every enforcement can be removed by Tyrel.** Hooks and guards catch accidents;
    they do not outrank him.
12. **Everything else is open.** Agents may build and review code, tests, CI, hooks, and
    operations inside the chamber boundary. Agents never push or merge.
13. **The session decides ordinary engineering.** Unless rule 1 or GOVERNANCE.md reserves
    an action for Tyrel, choose the implementation, structure, names, thresholds, tests,
    configuration, and disposition of findings. Use the goals, governance, prior rulings,
    source, and measurement; record the decision and reason. A hard question does not
    become Tyrel's by being hard. Do not park an engineering choice in a TODO, deferred
    list, handoff, or pull request. Rule 7 requires a visible decision, not a deferral.

**Settled permanently (his ruling; the dated record is in the standing ledger):**
vendor-licence analysis (non-commercial
research; vendor repos fetched at boot, never stored — only new carries into our tree
are findings) and cryptographic trust roots for approval records (integrity-only
records are the design). Neither is ever raised again.

## Where notes go

`workbench/` is local and gitignored. Current notes live in `active/`; durable task state
in `standing/`; raw machine evidence in `raw/`; completed work in `archive/`; disposable
output in `scratch/`. A note never becomes an instruction by surviving a session.

**Record what he means, not how he typed it.** When Tyrel gives direction in chat,
capture the principle and write it clean, in the project's own voice, with the
consequences worked out and the collisions named. Do not paste his message in as a quoted
block — he types fast while thinking aloud, and a quotation of that reads as a document he
authored and stands behind. The write-up is meant to be better than the message, not a copy
of it. **The exception is a ruling whose exact wording could later be disputed or
over-read**: those go in a standing ledger, quoted, because there the words themselves are
the evidence, and a verbatim record is what lets a later session tell his ruling apart from
someone's reading of it. Design notes and plans are written clean; ledgers may quote.

**Governed paths:** `CLAUDE.md`, `GOALS.md`, `GOVERNANCE.md`, `ARCHITECTURE.md`,
`GLOSSARY.md`, the root `README.md`, `DATA_CONTRACT.md` once it exists, and all of
`.claude/`. Tyrel approves their substance; the main session makes the edit through the
governed-edit procedure.

## Branches

Use `work/<topic>` for normal changes, `audit/<topic>` for findings, and
`infra/<topic>` for structural work. One short-lived branch per task. Name the branch
before editing. Never switch onto an existing branch while carrying uncommitted work.
Stage only files touched for the task; never `git add -A`.

## Agents

Repository-writing agents work in chambers. The host session remains accountable for the
goal, decisions, integrated diff, and verification. A chamber is pinned to a commit,
cannot push, returns a branch, may not edit governed paths, and carries no window onto the
old pipeline. Use agents for bounded work that benefits from independent context; do not
create ceremony merely to satisfy a roster.

**A chamber builds from this repository and its design notes, not from the old system.**
The notes reach it at `/specs`; the old code reaches it only if `AUTOCLAVE_WINDOW` is set
on `new`, which is a deliberate act and needs a reason in the brief. A brief that tells a
chamber to read the old pipeline without that is describing a mount that is not there, and
the chamber will invent rather than report the gap.

**A chamber builds and audits; the host integrates.** Charge the chamber with its own
independent audit round, then spend the host's attention on the load-bearing claims, the
check results, the review loops, the gate, and the push — not on reading every returned
line a second time. A host that re-reads the whole diff has spent the context the chamber
existed to save. **Hard rule 6 is untouched by this.** What enters is still inspected, and
the session that lands it must still be able to justify it; this says where the reading
happens, not whether it happens.

**Choose the seat for the job and name it in the dispatch. There is no standing vendor
ratio.** Sonnet and Terra are ordinary build seats at medium; Opus and Sol are the audit
and correction seats at high. Vendor diversity is a reason to reach for a seat, not a quota
to satisfy. A Fable seat, and the `ultracode`/`ultra` effort levels, are dispatched only
when Tyrel asks in the session. `.claude/agents/README.md` carries the full seat table and
the rulings' provenance.

## Quarantine

Understanding crosses from the old system; bytes cross only when they are the best option,
understood line by line, and named as carried in the commit and report. Third-party code
must have a permitting licence and a citation. `cleanroom/README.md` owns the procedure.

**The chamber window onto the old pipeline is closed.** The rebuild is planned from the
design notes now, so a chamber sees no old code at all unless `new` was run with
`AUTOCLAVE_WINDOW` deliberately set. This narrows where the rule above applies; it does
not soften it.
Nothing here licenses reading the old tree on the host and carrying a line in silently —
an unnamed carry is a finding at review wherever the reading happened.
`operations/autoclave/README.md` carries the ruling and its date.

## Pushing and merging

The first push and pull request need Tyrel's clear approval. Push the finished task, not a
stream of checkpoints. Later pushes to the same open pull request need no repeat approval,
but say when they happen. Never push directly to `main`; never force-push work you do not
exclusively own. Tyrel alone merges.

**A standing push grant is per-queue and dies with its queue.** It covers the branches it
was asked about and nothing after them; never read a past queue's grant as covering a new
one. Ask again.

**Never chain a push, pull request, or merge behind piped test output.** A pipeline's exit
status is its last command's, so `gate | tail && push` runs the push after a *failed* gate
— that is how a red candidate once reached an open pull request. Redirect the gate's output
to a file, echo `$?`, read it, and push in a separate command.

Review is proportional to risk. Before the first push, run the local gate and use fresh,
independent review where a defect would be expensive or quiet. Consequential review targets
one clean candidate commit through `operations/review/README.md`, never a moving index. A
fix creates a new candidate and invalidates earlier reviews; the pushed tip is the exact
candidate the final reviewers read. Pre-push CodeRabbit uses the CLI against `origin/main`;
after the push, wait for the automatic GitHub review before replying. Fix or decline every
real finding with a reason. A commit records both halves of its provenance, separately:
`Co-Authored-By:` names the model that wrote the lines, and `Reviewed-by:` names the model
that reviewed them. A pull request records decisions and rationale; it does not carry
open engineering questions to Tyrel.

Local hooks refuse direct-main pushes and scan outgoing history for credentials and large
payloads. The Claude guard also blocks disabling those hooks. `--no-verify` and
`-c core.hooksPath=` are Tyrel's escape hatches, not the session's.

## Reporting

Lead with the outcome. Say which checks actually ran, what remains blocked, and the one
recommended next action. Ask only when rule 1 reserves the choice, governance genuinely
conflicts, or progress cannot continue after reasonable investigation. Otherwise decide.

**Sessions are usually semi-attended.** Tyrel is around but not watching every reply, often
from a phone. Put questions at the start of a session or while he is clearly engaging;
mid-task, once he has gone quiet, decide and keep working — a question posted into silence
stalls the whole run until he happens to look. Keep replies scannable.

**Finish the task before reporting.** A progress report is not a stopping point. While
work already named as remaining is unblocked, carry on in the same reply instead of
handing back a status. Stop when the work is done, when Tyrel names a checkpoint or says
stop, or when a rule-1 gate blocks what is left — and then say plainly that you are
stopping and why. Never close a reply with an intention to continue: nothing runs between
replies, so "I will keep going" ends the work until Tyrel notices it stopped.
