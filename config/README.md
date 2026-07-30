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
change.
