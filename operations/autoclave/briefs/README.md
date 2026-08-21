# Chamber briefs

The image supplies the chamber boundary, the repository supplies its governing documents,
and a dispatch supplies the task. Prepend `builder.md` for ordinary work.

`rebuilder.md` is for re-expressing the old pipeline and **is not the ordinary case any
more**: since 2026-08-20 a chamber gets no window onto the old code unless
`AUTOCLAVE_WINDOW` is set on **`new`**, so that brief only makes sense alongside it.

`new` is the command that matters, not `dispatch`: mounts are fixed when the container is
created and cannot be added to a running one. `AUTOCLAVE_WINDOW=/path sh ... dispatch ...`
is accepted and does nothing, which is the silent kind of wrong.

This block is one lifecycle and is meant to be copied whole, so it fails closed: without
`set -eu` a failed `new` or an unreadable task file does not stop it, `dispatch` receives a
missing or partial brief, and `collect` then publishes a result from a chamber that never
got its constraints.

```sh
set -eu

task=my-task
vendor=claude
task_file=workbench/active/my-task.md
sh operations/autoclave/autoclave.sh new "$task" HEAD "$vendor"
cat operations/autoclave/briefs/builder.md "$task_file" > "/tmp/$task-brief.md"
sh operations/autoclave/autoclave.sh dispatch "$task" "$vendor" "/tmp/$task-brief.md" sonnet medium
sh operations/autoclave/autoclave.sh collect "$task"
```

Model and effort are dispatch arguments, not doctrine. The task names the objective,
allowed paths and actions, deliverable, checks, and stop conditions. Each chamber returns
one branch or report; nothing merges automatically.
