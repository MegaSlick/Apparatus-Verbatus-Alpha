# The pipeline

Seven stages, numbered in flow order. A directory listing here reads top to bottom
exactly like the diagram in [ARCHITECTURE.md](../ARCHITECTURE.md).

The numbers also make a direct statement such as `import 4_perlector` invalid Python.
That is a useful deterrent and keeps the flow visible, but it is not a complete
boundary: dynamic imports and path manipulation can still cross it. The repository
rule is that stages communicate only through the files declared in their
`HANDOFF.md`. Boundary tests must accompany the first implementation of each stage.

The path `pipeline/run.py` is reserved for the whole-flow runner and the bounded
recovery loop: the Recensor writes a rework request, and that runner honours it up to
the budget declared in `config/recovery.toml`. A stage's own `run.py` is reserved for
executing only that stage.
