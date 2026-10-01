# The pipeline

The stages, numbered in flow order: `0_triage` (optional sorting before intake), the
Exemplar and its intake door, the ink map, then the stages drawn in
[ARCHITECTURE.md](../ARCHITECTURE.md), and `orchestrator/`, which runs them.

The numbers also make a direct statement such as `import 4_perlector` invalid Python.
That is a useful deterrent and keeps the flow visible, but it is not a complete
boundary: dynamic imports and path manipulation can still cross it. The repository
rule is that stages communicate only through the files declared in their
`CONTRACT.md`. Boundary tests must accompany the first implementation of each stage.

The whole-flow runner lives in `pipeline/orchestrator/`. A stage's own `run.py`
executes only that stage. The page re-ask budget is sealed in `config/recovery.toml`
and spent by the Perlector's page re-ask (`common/page_reask.py`).
