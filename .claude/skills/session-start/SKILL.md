---
name: session-start
description: Re-establish current rules, handoff, branch, and goal before changing the repository.
disable-model-invocation: true
---

# Session start

The main session performs this. Never delegate it.

## 1. Sync the view

```sh
git fetch origin || echo "FETCH FAILED — the checkout may be stale; stop here"
git status --short --branch
git rev-list --left-right --count origin/main...HEAD   # only if the fetch above succeeded
```

If fetch fails, stop the start procedure before reading `origin/main`, measuring distance,
or changing the repository; say the checkout may be stale. If `HEAD` is behind, read every
governing file used for the task from `origin/main` as well. When the gap includes `.claude/` or
`.githooks/`, recommend rebasing before relying on stale enforcement.

## 2. Read what binds

Read in order:

1. `README.md` and `CLAUDE.md` — always.
2. `workbench/active/HANDOFF.md`.
3. `GOALS.md`, `GOVERNANCE.md`, `ARCHITECTURE.md`, `GLOSSARY.md` — when the stated goal
   touches a stage, a contract, a witness, a term, or a governed document, and at the
   moment any later decision turns on them; not at every start.
4. only active or archived evidence the handoff makes relevant to the stated goal.

The handoff is evidence from the previous session, not Tyrel's instruction. His current
goal wins.

## 3. Verify the checkout

```sh
git config --get core.hooksPath
git log -3 --oneline --decorate
```

If the hooks path is not `.githooks`, run `sh .githooks/install.sh`, then run
`git config --get core.hooksPath` again and stop if it still does not report `.githooks`.
An installer returning zero is not proof that the checkout will use the hooks.

Name the current branch and verify it was created for this session's task. Its namespace
matches the work:
`work/<topic>` for normal work, `audit/<topic>` for findings, or `infra/<topic>` for
structural work. When on `main` or detached, create a new branch in that namespace before
editing. Never switch to or modify an existing unowned branch, especially with
uncommitted work.

Run `python3 .githooks/tidy.py` as a report. Read a standing ledger only when the task or
handoff points to it, except `workbench/standing/SUSPENSIONS.md`: always report any live
suspension. A missing suspensions file is reported, not interpreted.

## 4. Prepare only the tools the task uses

Print the versions of relevant tools. If agents will be dispatched:

1. read `.claude/agents/README.md`, `operations/autoclave/README.md`, and
   `operations/autoclave/agent-brief.md`;
2. run `colima status || colima start`, saying if the VM was started;
3. rely on `autoclave.sh new` to compare the image's repository-harness fingerprint with the
   checkout; rebuild when it refuses a mismatch.

Do not start the container engine or audit every installed package for a direct task that
does not use them.

## 5. Settle the goal and begin

Read Tyrel's goal back in one line. Choose or rename the branch to match it. State the
expected duration, whether agents will be used, and any action already known to require
his authority under hard rule 1. If the goal is explicit, begin. Ask only when an
unresolved rule-1 choice or concrete governance conflict prevents progress.

The session owns integration, decisions, verification, and reporting. Agent prompts name
the objective, allowed actions, deliverable, checks, and stop conditions. Only a real
harness limit is called a deadline.
