# 4b — Coniector

*Conicere*, to conjecture. A text-only stage on a side branch after the Perlector:
beneath each diplomatic reading it proposes a labelled, unconfirmed reconstruction
from the text around the act — the page's other entries and, on a run sealed
`pages_are_consecutive`, the neighbouring pages' edge acts and the other pieces of
an act that crosses a page break. It never sees the page image. The Perlector stays
diplomatic only; the Armarium alone reads what this stage writes.

The chair (`reconstructor`) is the Perlector's model asked text only. Whether it
runs is one sealed switch, `mode` in `config/reconstruction.toml`, and it is on
by default; a run sealed `mode = "off"` plans no call and loads no model.

```sh
.venv/bin/python pipeline/4b_coniector/run.py --run-root <dir> --run-id <id> \
  --reconstruction-config config/reconstruction.toml
```

The records and what the Armarium checks are in [CONTRACT.md](CONTRACT.md).
