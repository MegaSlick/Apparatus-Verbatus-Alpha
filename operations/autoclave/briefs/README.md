# Chamber briefs

The image supplies the chamber boundary, the repository supplies its governing documents,
and a dispatch supplies the task. Prepend `builder.md` for ordinary work or `rebuilder.md`
when the old pipeline is being re-expressed.

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
