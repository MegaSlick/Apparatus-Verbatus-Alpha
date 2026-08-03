# config

The knobs. One question per planned file, each answerable without reading code.

| File | The question it answers |
|---|---|
| `models.toml` | which model and revision fills each numbered role |
| `recovery.toml` | how many times rework may be asked for before review |
| `spend.toml` | money caps |
| `formats.toml` | which formats the Armarium writes |

`models.toml` is the operational cast list. Model assignments belong there rather
than in stage code or stage documentation, which keeps a swap to one configuration
change. It also owns the three things a run is bound to that follow from the
roster: the witness floor, the adapter recipes, and — with the fixture and the
scenario — the run's configuration digest. `common/seats/README.md` describes how
it is read and what a malformed pin earns.

Two directories sit beside it because they are resolved relative to it, and could
not be pinned by it from anywhere else:

- `manifests/` — one digest-manifest artifact per configured seat: the sorted
  `{path, sha256, size}` rows whose canonical bytes a seat's `digest_manifest`
  names.
- `model-fixtures/` — the tiny local-repository snapshots the offline walking
  skeleton resolves. **These are not models.** They stand in for a model
  repository exactly as `proof/fixtures/synthetic-two-page-v0/*.png` stand in for
  a scanned register. `proof/build_model_fixtures.py` regenerates both directories
  and prints the pins; a test refuses any drift between them.
