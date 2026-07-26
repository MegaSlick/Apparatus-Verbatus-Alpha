# The pipeline

Seven stages, numbered in flow order. A directory listing here reads top to bottom
exactly like the diagram in [ARCHITECTURE.md](../ARCHITECTURE.md).

The numbers do a second job. `4_perlector` is not a legal Python module name, so
`import 4_perlector` is a syntax error — one stage physically cannot import another
even by accident. Stages talk through files on disk, described in each stage's
`HANDOFF.md`, and never any other way.

`run.py` here runs the whole flow and owns the bounded recovery loop: the Recensor
writes a rework request, and this honours it up to the budget in
`config/recovery.toml`. Each stage's own `run.py` runs that stage.
