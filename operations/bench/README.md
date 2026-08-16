# R7b bench runners

`records.py` seals the v1 definitions for B0, B0.5, and B2–B6.  Each measure
names its numerator, denominator, and treatment of unknown output; no runner may
replace its definition digest with a later goalpost.  `runner.py` fixture-tests
those records only.  It has no model, serving, pod, or network import.

Every fixture exercise emits `state: "not-run"` until a later runner supplies
actual observations. B0 and B0.5 additionally require a separately authorized
live-pod session. `fixture_verified: true` means only that the local
record/runner path was exercised; it is not a green result and makes no feeding
or cost claim.

Run the cardinality exercise (no models) from the repository root:

```sh
python -c 'from pathlib import Path; from operations.bench.scale import run_scale; print(run_scale(Path("/out/r7b-runtree-scale")))'
```

It refuses any cardinality other than ten RunTrees of 1,000 pages, in keeping
with the sealed shard boundary.  Its target must not already exist.  Copy the
printed result into the task report before removing the scratch tree.
