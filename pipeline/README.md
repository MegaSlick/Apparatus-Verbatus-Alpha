# The pipeline

Seven stages, numbered in flow order. A directory listing here reads top to bottom
exactly like the diagram in [ARCHITECTURE.md](../ARCHITECTURE.md).

The numbers also make a direct statement such as `import 4_perlector` invalid Python.
That is a useful deterrent and keeps the flow visible, but it is not a complete
boundary: dynamic imports and path manipulation can still cross it. The repository
rule is that stages communicate only through the files declared in their
`HANDOFF.md`. Boundary tests must accompany the first implementation of each stage.

The path `pipeline/run.py` is reserved for the future whole-flow runner and bounded
recovery loop. Before that runner is accepted, a tracked recovery configuration must
declare the finite budget: the Recensor will write a recorded coverage-recovery request,
and the runner will honour it only within that budget. A stage's own `run.py` is reserved
for executing only that stage.
