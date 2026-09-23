# Seat briefs

A writing seat is a linked worktree on this machine, pinned to a commit, under the
tool-call guard. Prepend `builder.md` to the task file for ordinary work; the task names
the objective, allowed paths and actions, deliverable, checks and stop conditions. Model
and effort are dispatch arguments. `.claude/agents/README.md` has the rest.

The guard refuses a seat's governed-path write (refusal 7) and its push, pull request,
ready-for-review or merge (refusal 8). A seat returns a branch or a report; what comes
back is untrusted until the host checks it.

## Creating one

```sh
git fetch origin main
git worktree add -b work/<topic> .claude/worktrees/<topic> origin/main
(cd .claude/worktrees/<topic> && uv sync --frozen --group test --group audit)
```

`.claude/worktrees/` is gitignored and excluded from test collection; a sibling directory
outside the repository works too. Inside `.claude/worktrees/`, write with paths relative
to the seat's root: the guard refuses a redirect, `tee`, `cp`, `mv`, `sed -i`, `patch`,
`install` or `dd` whose text contains `.claude/`. Each seat gets its own frozen
environment. Remove a seat with `git worktree remove` once its branch is merged or its
work is recorded.

This directory holds the standing briefs and nothing else, one level deep.
