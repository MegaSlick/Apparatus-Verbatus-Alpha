# Seat briefs

A writing seat is a linked worktree on this machine, pinned to a commit, under the
tool-call guard. The guard supplies the boundary, the repository supplies its governing
documents, and a dispatch supplies the task. Prepend `builder.md` to the task file for
ordinary work; the task names the objective, allowed paths and actions, deliverable,
checks, and stop conditions. Model and effort are dispatch arguments, not doctrine.
`.claude/agents/README.md` carries the seat table.

A seat cannot push, open a pull request, mark one ready, or merge — the guard refuses
those on the spawned-agent name (refusal 8) — and it never edits a governed path
(refusal 7). It returns a branch or a report; nothing merges automatically, and what
comes back is untrusted until the host reads it.

## Creating one

```sh
git fetch origin main
git worktree add -b work/<topic> .claude/worktrees/<topic> origin/main
(cd .claude/worktrees/<topic> && uv sync --frozen --group test --group audit)
```

`.claude/worktrees/` is gitignored and excluded from test collection by `pyproject.toml`;
a sibling directory outside the repository also works and is where a session may keep a
fleet of seats. Inside `.claude/worktrees/`, use paths relative to the seat's own root
when a shell command writes: the guard refuses a redirect, `tee`, `cp`, `mv`, `sed -i`,
`patch`, `install` or `dd` whose text contains `.claude/` anywhere, including inside an
absolute path. Each seat gets its own frozen environment, never the host's. Remove a seat
with `git worktree remove` only once its branch is merged or its work is recorded.

The container seat these briefs were written for (the autoclave, `operations/autoclave/`)
was retired on 2026-09-10; `history/2026-09-10_chamber-retired.md` records the ruling.
This directory holds the standing briefs and nothing else, one level deep, so it cannot
become a notes drawer.
