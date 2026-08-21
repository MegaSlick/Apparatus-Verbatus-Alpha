# Agents

Use agents where independent context or parallel work improves the result. The main
session remains accountable for the goal, engineering decisions, integrated diff, and
verification.

## Boundary

- **Repository-writing work runs in a chamber.** The agent gets its own clone and branch,
  a full shell, and tests. It cannot push. `operations/autoclave/README.md` owns the
  mechanism.
- **A chamber sees this repository and the design notes, not the old pipeline.** The
  window closed on 2026-08-20; `/specs` carries the notes, and the old code arrives only
  when `AUTOCLAVE_WINDOW` is set on `new` — mounts are fixed at container creation, so
  `dispatch` **refuses** the variable rather than accepting it as a no-op. Write briefs
  against what is actually mounted: an agent told to consult a mount that is absent fills
  the gap with invention more often than it reports it, so a rebuilder result is trusted
  only after the mount is confirmed in the chamber, never because the brief named it.
  `operations/autoclave/briefs/rebuilder.md` is the one brief that assumes the window and
  says so at the top.
- **Host agents are read-only.** The custom `scout`, `auditor`, and `consult` roles have no
  shell or write tools. Use them for lookup or advice when a chamber adds no value.
- **No agent edits a governed path, pushes, merges, opens a pull request, sends a
  notification, or starts paid infrastructure.** It proposes; the main session decides
  and acts where authorized.

Tell every agent to read `GOALS.md`, `GOVERNANCE.md`, `ARCHITECTURE.md`, `GLOSSARY.md`,
and `CLAUDE.md` from disk. Injected context may be older than the checkout.

## Choosing a seat

Choose the model and effort for the job, not from a permanent ceremony:

| Work | Default |
|---|---|
| quick path or reference lookup | `scout`, low |
| ordinary bounded build | Sonnet or Terra, medium |
| long repository read or large build | Terra |
| audit, correction, security, money, or quiet failure | Sol or Opus, high |
| design objection before a costly change | `consult`, xhigh |
| independent review | fresh readers; mix vendors when risk warrants it |

**Sonnet and Terra are the default build seats; Opus and Sol are the audit and correction
seats. There is no standing vendor ratio.** Choose the seat for the job and name it in the
dispatch. Vendor diversity is a real reason to reach for a seat — a Claude host plus
all-Claude chambers concentrates one vendor's blind spots — but it is a reason, not a
quota, and a seat Tyrel names for a run is simply the seat.

**Fable is not dispatched unless Tyrel asks for it in the session** — it may hold the
main session, but as an agent seat it is his call, not the roster's. Effort runs at
`medium` for building and `high` for judging; `ultracode` and `ultra` are dispatched only
when Tyrel asks. The `consult` floor of `xhigh` stands, enforced by `test_roster.py`.

Effort buys thinking time, not virtue. Raise it for hard judgement, not merely long work.
Model and effort are dispatch arguments; record what actually answered. A substitution is
reported, never silently treated as the requested seat. The launcher validates the
reachable model/effort pairs before it touches Docker.

The launcher's reachable effort values are:

| Vendor/model | Allowed effort |
|---|---|
| Claude, any model | `low medium high xhigh max ultracode` |
| Codex `gpt-5.3-codex-spark` | `low medium high xhigh` |
| Codex `gpt-5.6-sol` or `gpt-5.6-terra` | `none low medium high xhigh max ultra` |
| other Codex models | `none low medium high xhigh max` |

This is dispatch validation, not a model recommendation. Update the table and launcher in
the same change when a live CLI probe shows the accepted values changed.

## Prompting

Every prompt names:

- objective and allowed paths/actions;
- deliverable and checks;
- stop conditions;
- any real mechanical time limit.

Do not invent deadlines. Ask reviewers for every finding. The main session records each
finding as fixed or declined with a reason; it does not silently discard findings
below a presentation threshold. A chamber
may orchestrate internally for a large task, but its sub-agents use disjoint result paths
and do not touch git concurrently.

**Cap the report, never the findings.** Ask for conclusions with citations — file, line,
and the claim — not reproduced evidence. An uncapped verifier report costs the host
nearly what doing the work itself would have, which is the opposite of why the seat was
dispatched. Asking for every finding and capping
how each one is written up are different instructions, and GOVERNANCE 10's bar on an
instrument that constrains what it measures binds the first, not the second.

The provenance for every rule in this file — which ruling, whose words, what date — lives
in the standing ledgers under `workbench/`, not here. This file carries the rule that is in
force; the ledger carries why it is.

## Decision rule

Agents escalate only a concrete governance conflict or an action reserved to Tyrel by an
applicable `CLAUDE.md` hard rule or `GOVERNANCE.md` reservation. Otherwise they make the
engineering call, record the reason, and continue. “Unsure” is an honest finding only
after reasonable investigation; it is not a route for handing routine work back to Tyrel.

## Integration

**A chamber builds and audits; the host integrates.** A chamber's brief charges it with its own independent audit round, and a chamber
returns a branch or a report together with that audit's ledger. The host verifies the
load-bearing claims and the check results, runs the review loops and the gate,
integrates, and pushes. **The host does not re-read the returned diff line by line** —
that spends the context the chamber existed to save and adds a reader who is no longer
independent.

**This does not amend `CLAUDE.md` hard rule 6.** Rule 6 says nothing enters uninspected and
that the accountable session must be able to justify what lands. That holds exactly as
written; what this section settles is *where* the reading happens — in the chamber and its
audit round, and in the checks — not whether it happens. A remark in a chat is not an
amendment to a hard rule, and no session may make one (`GOVERNANCE.md`, "Who decides").

A load-bearing claim is still checked against the tree, a red gate still stops the work,
and agent agreement is evidence, not authority. Use several seats when they test genuinely
independent hypotheses, not to satisfy a fixed roster.

For consequential review, follow `operations/review/README.md`: every reviewer receives
the same full base and candidate SHAs, names the full candidate SHA in its report, and
reviews only that committed diff. Any fix invalidates every receipt for the older candidate.

Built-in host agents receive this preamble:

> You are working in Apparatus Verbatus. Read `GOALS.md`, `GOVERNANCE.md`,
> `ARCHITECTURE.md`, `GLOSSARY.md`, and `CLAUDE.md` from disk. Never edit a governed path,
> push, merge, open a pull request, notify anyone, or start paid infrastructure. Make and
> explain ordinary engineering decisions; stop only for a concrete governance conflict or
> an action reserved to Tyrel.
