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

## Real-runner schema obligations

The later branch that introduces real bench execution must extend the result
schema deliberately, rather than relaxing the fixture validator:

- A measured B0 or B0.5 result carries the serving profile's
  `preflight_state`, and validation refuses measured output from an unproven
  profile.
- Every real cell defines and validates visible failed, partial, and
  interrupted states. A runner may not omit such a cell or present it as
  complete; the exact state fields are settled with that runner's result
  schema, when its retained evidence is known.
