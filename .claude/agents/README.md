# Agents

Use agents where independent context or parallel work improves the result. The main
session remains accountable for the goal, engineering decisions, integrated diff, and
verification.

## Boundary

- **Repository-writing work runs in a chamber.** The agent gets its own clone and branch,
  a full shell, and tests. It cannot push. `operations/autoclave/README.md` owns the
  mechanism.
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

**Sonnet and Terra are the default seats; Opus and Sol are the audit and correction
seats. Fable is not dispatched unless Tyrel asks for it in the session** — it may hold
the main session, but as an agent seat it is his call, not the roster's. Effort runs at
`medium` for building and `high` for judging; `ultracode` and `ultra` are dispatched only
when Tyrel asks. The `consult` floor of `xhigh` stands (his 2026-08-01 ruling, enforced
by `test_roster.py`). Tyrel's ruling, 2026-08-13, in session.

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

## Decision rule

Agents escalate only a concrete governance conflict or an action reserved to Tyrel by an
applicable `CLAUDE.md` hard rule or `GOVERNANCE.md` reservation. Otherwise they make the
engineering call, record the reason, and continue. “Unsure” is an honest finding only
after reasonable investigation; it is not a route for handing routine work back to Tyrel.

## Integration

A chamber returns a branch or a report. Read the diff, verify load-bearing claims, and
run proportionate checks before integrating. Agent agreement is evidence, not authority.
Use several seats when they test genuinely independent hypotheses, not to satisfy a fixed
roster.

For consequential review, follow `operations/review/README.md`: every reviewer receives
the same full base and candidate SHAs, names the full candidate SHA in its report, and
reviews only that committed diff. Any fix invalidates every receipt for the older candidate.

Built-in host agents receive this preamble:

> You are working in Apparatus Verbatus. Read `GOALS.md`, `GOVERNANCE.md`,
> `ARCHITECTURE.md`, `GLOSSARY.md`, and `CLAUDE.md` from disk. Never edit a governed path,
> push, merge, open a pull request, notify anyone, or start paid infrastructure. Make and
> explain ordinary engineering decisions; stop only for a concrete governance conflict or
> an action reserved to Tyrel.
