# The pipeline

The stages, numbered in flow order: `0_triage` (optional sorting before intake), the
Exemplar and its intake door, the ink map, then the stages drawn in
[ARCHITECTURE.md](../ARCHITECTURE.md), and `orchestrator/`, which runs them.

The numbers also make a direct statement such as `import 4_perlector` invalid Python.
That is a useful deterrent and keeps the flow visible, but it is not a complete
boundary: dynamic imports and path manipulation can still cross it. The repository
rule is that stages communicate only through the files declared in their
`HANDOFF.md`. Boundary tests must accompany the first implementation of each stage.

The whole-flow runner and its bounded recovery loop live in `pipeline/orchestrator/`,
not at `pipeline/run.py` as this file once reserved. The budget it honours is declared
in a tracked recovery configuration, the Recensor writes a recorded coverage-recovery
request, and the runner acts only within that budget. A stage's own `run.py` is
reserved for executing only that stage.
