# replay

Re-runs a saved run's Perlector, Coniector, Recensor, Archetypus and Armarium with
the current code, answering every model call from the reply the saved run recorded.
No model is called and no pod is needed. Use it to prove a change to how the
Perlector's answers are read or accounted on a real run that has already been read.

```sh
.venv/bin/python operations/replay/replay.py --source <runs>/<saved-run-id> \
    --run-root <dir> --run-id <new-run-id> -- <the saved run's orchestrator arguments>
.venv/bin/python operations/replay/compare.py <runs>/<saved-run-id> <dir>/<new-run-id>
```

- The **new run** gets its own run id. Its `run.json` says it replays the saved run
  (`replay`: the saved run's id, the self-hash of its `run.json` and its commit) and
  names the commit of the code that replayed in `repository_commit`. The saved run is
  only read.
- The new run holds the saved run's Door, Exemplar, Ink map, Designator and
  Attestatores records byte for byte and never runs those stages. The orchestrator
  runs it from the Perlector on.
- Each Perlector or Coniector request is answered only when the saved run sent
  exactly the same bytes; the reply, its call record and the serving receipt are then
  the saved run's own. A page whose first reading the current code would ask
  differently stops the replay: a change to what the Perlector is asked needs a live
  run. A re-ask or reconstruction call the saved run never sent is recorded as not
  asked (`not-replayed`), which holds the page or leaves the reconstruction unmade.
- The orchestrator arguments are the ones the saved run was driven with (the real
  configuration files, `--placement-tier`, `--capacity-plan`, `--mechanics-qualification`
  and so on). Configuration must be unchanged: every stage refuses sealed
  configuration that differs from the saved run's. The replay chooses the run root,
  run id and stage range itself.
- The code must be committed (the commit is recorded), or named with
  `--repository-commit`.
- `VERBATUS_POOL_WORKERS=<n>` caps the worker processes a stage starts, for a laptop
  that must not run every core flat out.

`compare.py` sets the two runs' records side by side, kind by kind (equal, different,
or in one run only, with each run's own id and the digests of its own records left
out), and prints each run's held pages, unread pages and hold codes from its Recensor
reviews. A replay with unchanged code on the same machine type reproduces every
record; on another machine only the `decode-environment` record, which names the
machine, differs.
