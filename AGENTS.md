# Working in this repository

For AI sessions and the agents they start. [CONTRIBUTING.md](CONTRIBUTING.md) applies
too. Read [README.md](README.md) and [PRINCIPLES.md](PRINCIPLES.md) first, and
[ARCHITECTURE.md](ARCHITECTURE.md), [GLOSSARY.md](GLOSSARY.md) and the stage's
`CONTRACT.md` before changing a stage, a contract or a term.

This page is deliberately short. Use judgement and be careful rather than look for a
rule. For long or important sessions the lead gives a rule set for that session; it
governs that session.

## Who decides

The project lead does not write code. The lead decides:

- **Anything that costs money**: GPU pods, paid APIs, paid credits. Ask in the session;
  a standing approval with limits covers everything inside them. Every pod is started
  with its guard armed (`operations/pod/README.md`), and a pod is shut down only when
  RunPod's own listing and billing say so.
- **What the project claims or publishes**: calling a result proven, scaling up a small
  test, excluding material, publishing.
- **Anything hard to undo**: deleting branches or data, rewriting history.
- **The rules themselves**: changes to the documents CONTRIBUTING.md names and to what
  AI sessions may do (`.claude/`).

Everything else is engineering and the session decides it. Put the reason in the commit
message or pull request, not in a TODO or a handoff.

## RunPod

Before any RunPod work (a pod, a volume, a price, stock, the API), read
[operations/pod/RUNPOD.md](operations/pod/RUNPOD.md): the rules, which tool for what,
where results go, and the traps already met. For a bake-off day, also
`operations/bakeoff/RUNBOOK.md`.

## Data that never leaves

Real register images, transcriptions and personal data stay on the lead's machines and
pods, in the gitignored `private/`, `scriptorium/` and `workbench/` folders. Never commit
them or send them to an outside service. Before using a new tool on this repository,
check what it uploads.

## Git

- Work on a branch, never on `main`. Stage only the files you changed; never skip the
  commit hooks, which catch credentials before they leave the machine.
- Push and open pull requests freely for the session's goal. Merge your own pull request
  when it is up to date with `main`, CI is green on that head and review threads are
  resolved; report the number and head commit.
- Agents that write code use their own worktree and branch
  (`git worktree add -b work/<topic> ../verbatus-worktrees/<topic> origin/main`), and
  leave pushing and merging to the session.

## Reporting

The lead often reads on a phone. Lead with the outcome in plain words, say what was
checked and what was not, and recommend one next step. Ask only for the lead's
decisions, early, with a recommendation.
