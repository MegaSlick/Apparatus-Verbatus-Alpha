# config

The knobs. One question per file, each answerable without reading code.

| File | The question it answers |
|---|---|
| `models.toml` | which model and revision fills each numbered role |
| `recovery.toml` | how many times rework may be asked for before review |
| `spend.toml` | money caps |
| `formats.toml` | which formats the Armarium writes |

`models.toml` is the cast list. It is the only place in the repository that names a
model, which is what makes swapping one a one-line change.
